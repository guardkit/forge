"""``forge serve`` daemon body — JetStream durable consumer (TASK-F009-003 / TASK-FW10-001).

This module owns the long-running side of ``forge serve``. It connects to
NATS (or accepts an injected client from :mod:`forge.cli.serve` —
ASSUM-011), attaches to the **shared** JetStream durable consumer named
:data:`forge.cli._serve_config.DEFAULT_DURABLE_NAME` ("forge-serve") on
subject ``pipeline.build-queued.*``, pulls payloads, and hands them to
the orchestrator dispatcher.

Behaviour summary (see TASK-F009-003 + TASK-FW10-001 acceptance
criteria):

* ``run_daemon`` returns only on cancellation / signal — otherwise loops
  forever (AC: starts and stays running until SIGTERM).
* The pull subscription uses ``durable=DEFAULT_DURABLE_NAME`` exactly,
  on the shared :data:`PIPELINE_STREAM_NAME` stream, with explicit ack
  policy and ``max_ack_pending=1`` so the broker never holds more than
  one un-acked envelope per durable (ADR-ARCH-014, TASK-FW10-001 §2).
  Editing this field on a live consumer is rejected by JetStream — the
  rollout note in TASK-FW10-001 calls for ``nats consumer rm`` of the
  existing durable before deploying.
* :class:`SubscriptionState.live` flips to ``True`` only after the
  pull subscription is bound; it flips to ``False`` on broker loss
  and again at clean shutdown.
* Broker-outage recovery is JetStream-defaults only (ASSUM-007): we
  reconnect with bounded backoff and re-attach the same durable. The
  reconnect path opens a fresh client via the :data:`nats_connect`
  seam (only the **first** attach uses the injected ``client`` from
  ``_run_serve`` — see TASK-FW10-001 AC-006).
* SIGTERM / SIGINT are wired via :func:`asyncio.AbstractEventLoop.add_signal_handler`
  when available; the daemon also exits cleanly on
  :class:`asyncio.CancelledError`.
* Unacked messages stay pending on the consumer for redelivery (E2.1).
* A failed dispatch acks the message itself (releases the queue slot)
  and the daemon continues with the next payload (E3.1). On the
  **success** path the dispatcher (or the state machine via
  :func:`forge.adapters.nats.pipeline_consumer.handle_message`'s
  ``ack_callback``) owns terminal-only ack — ``_process_message`` does
  not ack on success (TASK-FW10-001 §2 + AC-002).

Module-level seams (rebindable by tests):

- :data:`nats_connect` — ``async (servers: str) -> client``.
- :data:`dispatch_payload` — ``async (msg: _MsgLike) -> None``.

Tests monkey-patch these to drive the daemon without a real broker.
The new ``DispatchFn`` signature is ``(_MsgLike) -> Awaitable[None]``
(TASK-FW10-001 AC-001) — the dispatcher receives the whole message so
it can ack on terminal completion through ``ack_callback`` semantics
rather than the daemon acking unconditionally.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from forge.adapters.nats.pipeline_consumer import ACK_WAIT_SECONDS
from forge.cli._serve_config import ServeConfig
from forge.cli._serve_state import SubscriptionState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants pinned to the integration contract
# ---------------------------------------------------------------------------

#: Subject filter for the durable consumer. The trailing ``*`` matches a
#: single ``feature_id`` token — same shape as the producer-side queue
#: subject in :mod:`forge.cli.queue`.
BUILD_QUEUED_SUBJECT_FILTER: str = "pipeline.build-queued.*"

#: Stream name shared with the existing pipeline consumer. Configured
#: by ``nats-infrastructure``; carries both the inbound build queue and
#: the outbound lifecycle events.
PIPELINE_STREAM_NAME: str = "PIPELINE"

#: How many messages to fetch per pull. Kept at 1 to mirror the
#: sequential-build constraint (ADR-ARCH-014) — a fanned-out batch would
#: defeat the work-queue semantics that make D2 safe.
PULL_BATCH_SIZE: int = 1

#: Timeout (seconds) for one ``fetch`` call. Short so the loop returns
#: control to the stop-event check between message arrivals.
PULL_TIMEOUT_SECONDS: float = 1.0

#: Initial reconnect backoff after a broker error.
RECONNECT_INITIAL_BACKOFF: float = 1.0

#: Cap on the exponential reconnect backoff.
RECONNECT_MAX_BACKOFF: float = 30.0

#: Hard ceiling on the final ``client.close()`` await. Keeps the SIGTERM
#: budget under the documented 10 s AC even if the broker is hung.
SHUTDOWN_TIMEOUT_SECONDS: float = 5.0

#: Maximum number of un-acked messages JetStream is allowed to hold for
#: this durable at any time (ADR-ARCH-014; TASK-FW10-001 §2). With
#: ``max_ack_pending=1`` the broker enforces strict serial processing
#: per replica, which is the precondition for the in-flight crash-
#: recovery rules in :mod:`forge.adapters.nats.pipeline_consumer`.
MAX_ACK_PENDING: int = 1


# ---------------------------------------------------------------------------
# Type surface
# ---------------------------------------------------------------------------


@runtime_checkable
class _MsgLike(Protocol):
    """Minimal slice of :class:`nats.aio.msg.Msg` used by the loop."""

    data: bytes

    async def ack(self) -> None:  # pragma: no cover - protocol stub
        ...


NatsConnectFn = Callable[[str], Awaitable[Any]]
"""``async (servers: str) -> client`` — lazy NATS connect seam."""

DispatchFn = Callable[["_MsgLike"], Awaitable[None]]
"""``async (msg: _MsgLike) -> None`` — orchestrator dispatch seam.

TASK-FW10-001 AC-001: the dispatcher receives the whole message rather
than just its bytes payload so it (or the state machine via
:func:`forge.adapters.nats.pipeline_consumer.handle_message`'s
``ack_callback``) can ack on terminal completion. ``_process_message``
no longer acks on the success path — the dispatcher owns that
lifecycle.
"""


# ---------------------------------------------------------------------------
# Default seam implementations (production)
# ---------------------------------------------------------------------------


async def _default_nats_connect(servers: str) -> Any:
    """Production NATS connect — opens a fresh client.

    The ``nats`` import is lazy so importing this module does not pull
    the network stack into ``forge --help``.

    Args:
        servers: NATS URL (defaults to ``nats://127.0.0.1:4222``).

    Returns:
        A connected ``nats.NATS`` client.

    Raises:
        RuntimeError: When ``nats-py`` is not installed.
    """
    try:
        import nats  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - covered via the seam
        raise RuntimeError("nats client not installed — `pip install nats-py`") from exc
    return await nats.connect(servers=servers)


async def _default_dispatch(msg: _MsgLike) -> None:
    """Default dispatcher: log + ack the message.

    This is the test-seam default. Production wiring (TASK-FW10-007)
    rebinds :data:`dispatch_payload` to a closure that runs the full
    orchestrator chain and acks via the state machine's
    ``ack_callback`` only on terminal completion.

    The default still acks the message itself because it is the
    reachable seam in two scenarios:

    1. A unit test that does **not** rebind ``dispatch_payload``. Without
       ack here, the fake broker would loop forever on the same message.
    2. Pre-FW10-007 boot of the daemon — until the orchestrator wiring
       lands, treating the envelope as observed-and-released matches the
       daemon's "available" property; not acking would jam the durable.

    A malformed envelope is logged at WARNING and still acked — leaving
    it unacked would block the queue's single ack slot
    (``max_ack_pending=1``) and the daemon would stop making progress.

    The function MUST NOT raise — a failure here would propagate into
    :func:`_process_message`'s ``except Exception`` branch which would
    then ack the message itself. Logging + early-return preserves the
    invariant that only one ack runs per message.
    """
    try:
        from nats_core.envelope import MessageEnvelope

        envelope = MessageEnvelope.model_validate_json(msg.data)
    except Exception as exc:  # noqa: BLE001 — log + ack + drop, never raise
        logger.warning("forge-serve: dropping unparseable envelope (%s)", exc)
        await msg.ack()
        return

    feature_id: str | None = None
    raw_payload = envelope.payload if isinstance(envelope.payload, dict) else {}
    candidate = raw_payload.get("feature_id")
    if isinstance(candidate, str) and candidate:
        feature_id = candidate

    logger.info(
        "forge-serve: received build-queued envelope feature_id=%s "
        "correlation_id=%s",
        feature_id,
        envelope.correlation_id,
    )
    await msg.ack()


# Module-level rebindable seams. Tests substitute these with AsyncMock
# fakes; production keeps the defaults until TASK-FW10-007 rebinds
# ``dispatch_payload`` to the orchestrator chain.
nats_connect: NatsConnectFn = _default_nats_connect
dispatch_payload: DispatchFn = _default_dispatch

#: The composed WS2-B8 deploy-stage runner (Lane C1a), or ``None`` when
#: ``deploy.enabled`` is False (the default). Stashed at serve boot by
#: ``bind_production_dispatch_chain`` so the post-review deploy trigger (Lane
#: C4) can reach it. ``None`` = the deploy stage is inert (DEPLOY / LIVE_GATE
#: never dispatch) — the byte-for-byte no-op flag-off state.
deploy_stage_runner: Any | None = None


# ---------------------------------------------------------------------------
# Subscription wiring
# ---------------------------------------------------------------------------


async def _attach_consumer(client: Any, durable_name: str) -> Any:
    """Bind the shared durable pull subscription on ``client``.

    Imports :mod:`nats.js.api` lazily so this module's import surface
    stays small. The :class:`~nats.js.api.ConsumerConfig` mirrors the
    one used by :mod:`forge.adapters.nats.pipeline_consumer` for explicit
    ack semantics; differences are scoped to the durable name and the
    subject filter. Critically, ``max_ack_pending`` is set to
    :data:`MAX_ACK_PENDING` (= 1) so the broker enforces strict serial
    processing per replica (ADR-ARCH-014; TASK-FW10-001 §2). This is
    the precondition for the in-flight crash-recovery rules implemented
    in :func:`forge.adapters.nats.pipeline_consumer.reconcile_on_boot`:
    if the broker were allowed to hold multiple un-acked messages the
    "redelivery on restart" guarantee could silently widen into a
    multi-build replay storm.
    """
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    js = client.jetstream()
    config = ConsumerConfig(
        durable_name=durable_name,
        deliver_policy=DeliverPolicy.ALL,
        ack_policy=AckPolicy.EXPLICIT,
        filter_subject=BUILD_QUEUED_SUBJECT_FILTER,
        max_ack_pending=MAX_ACK_PENDING,
        # TASK-GATE-D659 (contract §2.2 / plan "Broker posture"): pin
        # ``ack_wait`` to 1h. Without it nats-py falls back to the 30s
        # server default, so a build paused at the pre-dispatch approval
        # gate would have its un-acked build-queued message redelivered
        # every 30s (a redelivery storm against the held queue slot).
        # The pause holds the slot un-acked for the full approval window
        # per FEAT-FORGE-010; ``ack_wait`` MUST exceed
        # ``approval.max_wait_seconds`` (operator guidance).
        ack_wait=ACK_WAIT_SECONDS,
    )
    return await js.pull_subscribe(
        subject=BUILD_QUEUED_SUBJECT_FILTER,
        durable=durable_name,
        stream=PIPELINE_STREAM_NAME,
        config=config,
    )


# ---------------------------------------------------------------------------
# Inner loops
# ---------------------------------------------------------------------------


async def _process_message(msg: _MsgLike) -> None:
    """Hand the whole message to :data:`dispatch_payload` (TASK-FW10-001).

    The dispatcher (or the state machine via
    :func:`forge.adapters.nats.pipeline_consumer.handle_message`'s
    ``ack_callback``) owns terminal-only ack on the success path. We
    deliberately do **not** ack here on success — that would defeat the
    "ack only on terminal state" property the lifecycle pipeline relies
    on (TASK-FW10-001 AC-002).

    Failure handling:

    * :class:`asyncio.CancelledError` re-raises without acking — the
      pending message is left for JetStream redelivery (E2.1: a crash
      mid-build leaves the message pending so the next replica picks it
      up).
    * Any other exception is the E3.1 isolation path: the dispatcher
      raised before completing terminal-state handling, so we ack the
      message ourselves to release the durable's single ack slot
      (``max_ack_pending=1``), then log. Logging happens **after** the
      ack — see TASK-FW10-001 AC-003 — because the queue-slot release
      is the load-bearing side effect; the log line is observability.

    Args:
        msg: The pulled JetStream message. ``msg.ack`` is called only
            on the failure path; on success the dispatcher acks
            terminally.
    """
    try:
        await dispatch_payload(msg)
    except asyncio.CancelledError:
        # Crash-safety: do NOT ack. JetStream redelivers after ack_wait.
        raise
    except Exception as exc:  # noqa: BLE001 — E3.1 isolation
        # Ack first so the queue slot is released even if the logger
        # configuration is broken; log second so the operator sees what
        # failed. This ordering is asserted by tests in
        # ``test_cli_serve_daemon.py::TestProcessMessageFailurePath``.
        try:
            await msg.ack()
        except Exception as ack_exc:  # noqa: BLE001 — best-effort
            logger.debug(
                "forge-serve: ack-on-failure failed (%s); JetStream will "
                "redeliver after ack_wait",
                ack_exc,
            )
        logger.warning(
            "forge-serve: dispatch failed (%s); acked to release queue slot",
            exc,
        )


async def _consume_forever(
    sub: Any,
    state: SubscriptionState,
    stop_event: asyncio.Event,
) -> None:
    """Pull loop — fetches one message at a time until ``stop_event``.

    A bare ``asyncio.TimeoutError`` from ``fetch`` is the no-message
    signal and is silently absorbed. Any other exception (broker loss,
    network blip) propagates to the caller, which marks the state
    not-live and triggers reconnect.
    """
    while not stop_event.is_set():
        try:
            msgs = await sub.fetch(PULL_BATCH_SIZE, timeout=PULL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # No messages this tick — yield to let stop_event flip.
            await asyncio.sleep(0)
            continue
        for msg in msgs:
            if stop_event.is_set():
                # Leave un-acked messages for redelivery (E2.1).
                return
            await _process_message(msg)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGTERM / SIGINT to flip ``stop_event``.

    Falls back silently when the running loop does not support
    ``add_signal_handler`` (Windows ProactorEventLoop, non-main-thread
    test runners). On those platforms we still react to
    :class:`asyncio.CancelledError` — :func:`run_daemon` treats both
    cancellation paths as "stop now".
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop yet — caller will register handlers later.
        return

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.debug(
                "forge-serve: cannot install handler for %s "
                "(not in main thread or unsupported loop)",
                sig.name,
            )


# ---------------------------------------------------------------------------
# Top-level daemon coroutine
# ---------------------------------------------------------------------------


async def run_daemon(
    config: ServeConfig,
    state: SubscriptionState,
    *,
    client: Any | None = None,
) -> None:
    """Run the JetStream durable consumer until SIGTERM / cancellation.

    TASK-FW10-001 AC-006: when ``client`` is provided the daemon uses
    it for the **first** attach instead of opening its own — the caller
    (:func:`forge.cli.serve._run_serve`) opens exactly one NATS client
    and shares it with the dispatcher, the deps factory, the publisher,
    and this daemon, so all components share the connection (ASSUM-011).
    The reconnect path still calls :data:`nats_connect` for subsequent
    attaches; the AC restricts the **startup** path to a single connect.

    Control flow:

    1. Install signal handlers (best-effort — see
       :func:`_install_signal_handlers`).
    2. Outer loop: use ``client`` for the first iteration, then attach
       consumer, run the inner pull loop until either the stop event
       fires or a broker error escapes.
    3. On broker error: mark state not-live, sleep with bounded backoff,
       open a fresh client via :data:`nats_connect`, retry. Repeats
       until ``stop_event`` is set.
    4. On clean exit: mark state not-live, unsubscribe, close client.

    Args:
        config: Daemon configuration (NATS URL, durable name, …).
        state: Shared readiness flag — flipped True once the durable
            is bound and False on broker loss / shutdown.
        client: Optional pre-opened NATS client to use for the first
            attach. When ``None`` the daemon opens its own via
            :data:`nats_connect` (legacy behaviour, retained so
            existing direct callers and tests do not break).
    """
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    # Track whether the next iteration should reuse the injected client.
    # Once consumed (or once the broker drops it), subsequent iterations
    # open a fresh one via the seam.
    pending_client: Any | None = client
    backoff = RECONNECT_INITIAL_BACKOFF
    try:
        while not stop_event.is_set():
            iteration_client: Any = None
            sub: Any = None
            try:
                if pending_client is not None:
                    iteration_client = pending_client
                    pending_client = None
                else:
                    iteration_client = await nats_connect(config.nats_url)
                sub = await _attach_consumer(iteration_client, config.durable_name)
                await state.set_live(True)
                # Successful attach resets the backoff so the next outage
                # starts from RECONNECT_INITIAL_BACKOFF rather than the
                # capped ceiling.
                backoff = RECONNECT_INITIAL_BACKOFF
                try:
                    await _consume_forever(sub, state, stop_event)
                finally:
                    await state.set_live(False)
            except asyncio.CancelledError:
                stop_event.set()
                await state.set_live(False)
                raise
            except Exception as exc:  # noqa: BLE001 — D3 reconnect path
                logger.warning(
                    "forge-serve: broker error (%s); reconnecting in %.1fs",
                    exc,
                    backoff,
                )
                await state.set_live(False)
                # Wake early if a SIGTERM arrives during the backoff sleep.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)
            finally:
                # Best-effort tear-down — broker may already be gone.
                if sub is not None:
                    try:
                        await sub.unsubscribe()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("forge-serve: unsubscribe error (%s)", exc)
                if iteration_client is not None:
                    try:
                        await asyncio.wait_for(
                            iteration_client.close(),
                            timeout=SHUTDOWN_TIMEOUT_SECONDS,
                        )
                    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                        logger.debug("forge-serve: client.close error (%s)", exc)
    finally:
        await state.set_live(False)


__all__ = [
    "BUILD_QUEUED_SUBJECT_FILTER",
    "DispatchFn",
    "MAX_ACK_PENDING",
    "NatsConnectFn",
    "PIPELINE_STREAM_NAME",
    "PULL_BATCH_SIZE",
    "PULL_TIMEOUT_SECONDS",
    "RECONNECT_INITIAL_BACKOFF",
    "RECONNECT_MAX_BACKOFF",
    "SHUTDOWN_TIMEOUT_SECONDS",
    "dispatch_payload",
    "nats_connect",
    "run_daemon",
]
