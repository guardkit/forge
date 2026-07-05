"""CLI runtime wiring for the cancel/skip wrappers (TASK-PSM-011).

Splits :class:`CliRuntime` and :func:`build_cli_runtime` out of
:mod:`forge.cli.main` so the cancel/skip subcommand modules can import
the runtime helper without a circular import (``main`` imports
``cancel`` / ``skip`` to register them on the Click group).

The runtime owns no behavioural rules — it only constructs the SQLite
adapters that satisfy the seven Protocols
:class:`~forge.pipeline.cli_steering.CliSteeringHandler` is composed
against. Async-task / synthetic-injector seams default to no-ops
because a short-lived CLI process cannot reach a live LangGraph
runtime; tests inject explicit fakes via the kwargs.

TASK-JNB-102: the ``cancelled_notifier`` seam is the exception to the
no-op rule — a short-lived CLI process CAN publish to NATS (the
``forge queue`` command established the sync one-shot pattern), so
``forge cancel`` emits ``pipeline.build-cancelled`` for real,
best-effort, via :class:`SqliteRowCancelledNotifier`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from forge.adapters.sqlite.connect import connect_writer
from forge.lifecycle.persistence import (
    AsyncTaskCanceller,
    AsyncTaskUpdater,
    SqliteBuildCanceller,
    SqliteBuildResumer,
    SqliteBuildSnapshotReader,
    SqliteLifecyclePersistence,
    SqlitePauseRejectResolver,
    SqliteStageSkipRecorder,
)
from forge.pipeline.cli_steering import CancelledNotifier, CliSteeringHandler

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CliRuntime:
    """Bundle of wired-up dependencies the cancel/skip wrappers consume.

    Attributes:
        persistence: The lifecycle facade used to resolve
            ``identifier → build_id`` via
            :meth:`SqliteLifecyclePersistence.find_active_or_recent`.
        cli_steering_handler: The executor-layer handler that owns the
            cancel/skip behavioural rules.
    """

    persistence: SqliteLifecyclePersistence
    cli_steering_handler: CliSteeringHandler


def _noop_async_call(*_args: object, **_kwargs: object) -> None:
    """No-op pass-through for the AsyncTask{Canceller,Updater} seams."""
    return None


def _noop_synthetic_injector(_payload: object) -> None:
    """No-op synthetic-injector for :class:`SqlitePauseRejectResolver`."""
    return None


class SqliteRowCancelledNotifier:
    """Production :class:`CancelledNotifier` — row lookup + NATS publish.

    TASK-JNB-102 (ASSUM-010 closure): the CLI cancel path emits
    ``pipeline.build-cancelled`` so the phone loop sees terminal
    confirmation. The handler's :class:`BuildSnapshot` carries neither
    ``correlation_id`` nor (on OTHER_RUNNING) ``feature_id``, so this
    notifier enriches from the builds row by ``build_id`` via
    :meth:`SqliteLifecyclePersistence.get_build_row`.

    Publish transport: the injected sync ``publish(subject, body)``
    seam — production default is :func:`forge.cli.queue.publish` (the
    established one-shot connect/publish/flush/close over
    ``FORGE_NATS_URL``). Exceptions propagate to the handler's DDR-007
    guard, which logs WARNING and keeps the SQLite transition.

    Emit-authority note: this notifier is the only emit site on the CLI
    cancel path today, for TWO independent reasons — (a) the lifecycle
    bridge (sole build-cancelled emitter for SSE-observed terminals)
    can never fire for CLI cancels because the sidecar runner never
    surfaces a cancelled/interrupted lifecycle; and (b) the
    PAUSED_AT_GATE branch cannot reach the gating reject-emit
    (wrappers._dispatch_response) because ``build_cli_runtime`` wires a
    NO-OP synthetic injector — no synthetic reject ever lands on the
    response subject from a standalone CLI process. If a future task
    wires a REAL synthetic injector into the CLI runtime, reason (b)
    breaks and a CLI cancel of a paused gated build could double-emit
    (this notifier + the daemon's reject branch) — that task must pick
    a single emit owner before wiring it.
    """

    def __init__(
        self,
        persistence: SqliteLifecyclePersistence,
        *,
        publish: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self._persistence = persistence
        if publish is None:
            # Late import — queue.py pulls in click; keep the runtime
            # module import-light for non-CLI consumers.
            from forge.cli.queue import publish as queue_publish

            publish = queue_publish
        self._publish = publish

    def notify_cancelled(
        self, *, build_id: str, reason: str, cancelled_by: str
    ) -> None:
        from nats_core.envelope import EventType, MessageEnvelope
        from nats_core.events import BuildCancelledPayload

        row = self._persistence.get_build_row(build_id)
        if row is None or not row.feature_id:
            logger.warning(
                "cancelled_notifier: no builds row (or empty feature_id) "
                "for build_id=%s — skipping build-cancelled publish "
                "(cannot derive subject)",
                build_id,
            )
            return
        payload = BuildCancelledPayload(
            feature_id=row.feature_id,
            build_id=build_id,
            reason=reason,
            cancelled_by=cancelled_by,
            cancelled_at=datetime.now(timezone.utc).isoformat(),
            correlation_id=row.correlation_id,
        )
        envelope = MessageEnvelope(
            source_id="forge",
            event_type=EventType.BUILD_CANCELLED,
            correlation_id=row.correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        subject = f"pipeline.build-cancelled.{row.feature_id}"
        self._publish(subject, envelope.model_dump_json().encode("utf-8"))
        logger.info(
            "cancelled_notifier: published build-cancelled build_id=%s "
            "feature_id=%s cancelled_by=%s",
            build_id,
            row.feature_id,
            cancelled_by,
        )


def build_cli_runtime(
    db_path: Path,
    *,
    synthetic_injector: Callable[[object], object] | None = None,
    async_task_canceller: Callable[[str], object] | None = None,
    async_task_updater: Callable[..., object] | None = None,
    cancelled_notifier: "CancelledNotifier | None" = None,
) -> CliRuntime:
    """Wire :class:`CliRuntime` against a real SQLite database.

    The seven Protocol implementations the
    :class:`CliSteeringHandler` is composed against are constructed
    here so the cancel/skip wrappers stay under the 60-line ceiling
    declared by TASK-PSM-011 AC-007.

    Args:
        db_path: Path to ``forge.db``. Must already exist.
        synthetic_injector: Optional override for the synthetic-reject
            injector (tests use this to capture the synthetic payload).
        async_task_canceller: Optional override for the
            ``cancel_async_task`` seam.
        async_task_updater: Optional override for the
            ``update_async_task`` seam.
        cancelled_notifier: Optional override for the TASK-JNB-102
            build-cancelled notifier. ``None`` (production) wires
            :class:`SqliteRowCancelledNotifier` over the same
            persistence facade — ``forge cancel`` emits for real,
            best-effort; tests inject spies.

    Returns:
        A populated :class:`CliRuntime`.
    """
    connection = connect_writer(db_path)
    persistence = SqliteLifecyclePersistence(
        connection=connection,
        db_path=db_path,
    )
    handler = CliSteeringHandler(
        snapshot_reader=SqliteBuildSnapshotReader(persistence),
        pause_reject_resolver=SqlitePauseRejectResolver(
            persistence,
            synthetic_injector=synthetic_injector or _noop_synthetic_injector,
        ),
        async_task_canceller=AsyncTaskCanceller(
            async_task_canceller or _noop_async_call
        ),
        async_task_updater=AsyncTaskUpdater(async_task_updater or _noop_async_call),
        build_canceller=SqliteBuildCanceller(persistence),
        skip_recorder=SqliteStageSkipRecorder(persistence),
        build_resumer=SqliteBuildResumer(persistence),
        cancelled_notifier=(
            cancelled_notifier
            if cancelled_notifier is not None
            else SqliteRowCancelledNotifier(persistence)
        ),
    )
    return CliRuntime(persistence=persistence, cli_steering_handler=handler)


__all__ = [
    "CliRuntime",
    "SqliteRowCancelledNotifier",
    "build_cli_runtime",
]
