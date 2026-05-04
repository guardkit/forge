"""Boot-time crash-recovery reconciliation (TASK-PSM-007).

On agent-runtime startup the pipeline must reconcile every build that
the previous process left in a non-terminal state. The previous process
may have crashed mid-transition; without a deterministic reconciliation
pass, builds would silently stall — RUNNING rows would never advance,
PAUSED rows would never re-issue their approval request, and
FINALISING rows could leave a half-published PR record.

This module is the **single boot-time entry point** for that pass:

* :func:`reconcile_on_boot` is called exactly once from the agent
  runtime startup hook.
* The per-state action matrix is taken verbatim from
  ``docs/design/contracts/API-sqlite-schema.md §6``.
* Every state transition flows through
  :func:`forge.lifecycle.state_machine.transition` →
  :meth:`SqliteLifecyclePersistence.apply_transition` (concern
  ``sc_001``). This module never issues raw ``UPDATE`` against the
  ``builds.status`` column; the seam test
  ``test_recovery_uses_state_machine_transitions`` static-greps for that.
* PAUSED-recovery re-publishes the original
  ``ApprovalRequestPayload.request_id`` **verbatim** (concern
  ``sc_004``). A fresh UUID would orphan any approval response that the
  human reviewer (or Rich/Jarvis) had already posted before the crash —
  the responder correlates by ``request_id``.

Per-state action matrix (API-sqlite-schema.md §6)
--------------------------------------------------

+--------------+-----------------------------------------------------+
| Boot status  | Action                                              |
+==============+=====================================================+
| QUEUED       | No-op — JetStream redelivers if message was unacked |
+--------------+-----------------------------------------------------+
| PREPARING    | Mark INTERRUPTED, publish ``pipeline.build-failed`` |
|              | with ``recoverable=True``; JetStream redelivers and |
|              | the build re-enters the lifecycle                   |
+--------------+-----------------------------------------------------+
| RUNNING      | Mark INTERRUPTED; the next pull-consumer message    |
|              | for the feature re-enters the lifecycle (per-build  |
|              | retry-from-scratch, not per-pipeline)               |
+--------------+-----------------------------------------------------+
| PAUSED       | Re-issue the original approval request with the     |
|              | preserved ``request_id`` (sc_004)                   |
+--------------+-----------------------------------------------------+
| FINALISING   | Mark INTERRUPTED with a warning recorded in the     |
|              | ``error`` field — PR may have been created on       |
|              | GitHub; operator reconciles manually via            |
|              | ``forge history``                                   |
+--------------+-----------------------------------------------------+
| INTERRUPTED  | No-op — already awaiting re-pickup                  |
+--------------+-----------------------------------------------------+
| Terminal     | Filtered out by                                     |
|              | :meth:`read_non_terminal_builds`                    |
+--------------+-----------------------------------------------------+

Idempotency
-----------

Running :func:`reconcile_on_boot` twice in a row produces no additional
state changes (Group D crash-recovery scenarios):

* QUEUED stays QUEUED, INTERRUPTED stays INTERRUPTED — both no-ops.
* PREPARING / RUNNING / FINALISING become INTERRUPTED on the first run;
  on the second run they are INTERRUPTED, which is a no-op.
* PAUSED stays PAUSED on both runs (PAUSED → PAUSED is *not* a
  transition; the re-publish is a wire-level action, not a state
  change). The second run re-publishes the approval request with the
  same ``request_id``; downstream responders dedupe by ``request_id``,
  so a duplicate publish is harmless.

Failure isolation
-----------------

Recovery never aborts on a single per-build failure. Each build is
reconciled inside its own ``try``/``except``; failures are recorded on
:attr:`RecoveryReport.failures` so the operator sees them in the boot
log, but other builds are still reconciled. This avoids the
"one wedged build blocks the entire pipeline restart" failure mode.

References
----------

* TASK-PSM-007 — this task brief.
* TASK-PSM-002 — ``schema.sql`` / ``builds.pending_approval_request_id``
  column populated on the PAUSED transition.
* TASK-PSM-004 — :func:`forge.lifecycle.state_machine.transition`,
  the sole producer of :class:`Transition` value objects.
* TASK-PSM-005 — :class:`SqliteLifecyclePersistence.apply_transition`,
  the sole writer of ``builds.status``.
* TASK-CGCP-006 — :class:`ApprovalPublisher`, the consumer of the
  re-issued approval envelope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from forge.lifecycle.persistence import (
    Build,
    BuildRow,
    SqliteLifecyclePersistence,
)
from forge.lifecycle.state_machine import (
    BuildState,
    transition as compose_transition,
)
from forge.pipeline import attach_correlation_id

if TYPE_CHECKING:  # pragma: no cover — import-time only
    pass

logger = logging.getLogger(__name__)


__all__ = [
    "ApprovalRepublisher",
    "PipelineFailurePublisher",
    "RecoveryReport",
    "reconcile_on_boot",
]


# ---------------------------------------------------------------------------
# Recovery report
# ---------------------------------------------------------------------------


@dataclass
class RecoveryReport:
    """Per-state counts plus warnings/failures from one recovery pass.

    The report is the operator's audit trail for what the boot-time
    reconciliation did. It is intentionally a plain dataclass (not a
    Pydantic model) so the agent-runtime startup hook can mutate
    ``failures`` and ``finalising_warnings`` in place during the pass
    without a fresh model copy per per-build update.

    Attributes:
        interrupted_count: Number of PREPARING / RUNNING / FINALISING
            builds that were transitioned to INTERRUPTED.
        paused_reissued_count: Number of PAUSED builds whose original
            approval request was re-published.
        finalising_warnings: Operator-facing warnings emitted for
            FINALISING crashes; each entry is
            ``"<build_id>: finalising-interrupted: PR may exist at <pr_url>"``
            (or "PR creation status unknown" when ``pr_url`` is null).
            The operator must manually verify whether a GitHub PR was
            opened before the crash.
        failures: Per-build handler failures. Each entry is
            ``(build_id, exception)``. A non-empty ``failures`` list
            does NOT abort the pass — every other build is still
            reconciled.
        skipped_count: Number of builds that required no action
            (QUEUED, INTERRUPTED). Useful for boot-log diagnostics: a
            very high value indicates the previous process was largely
            idle when it crashed.
    """

    interrupted_count: int = 0
    paused_reissued_count: int = 0
    finalising_warnings: list[str] = field(default_factory=list)
    failures: list[tuple[str, Exception]] = field(default_factory=list)
    skipped_count: int = 0


# ---------------------------------------------------------------------------
# Publisher protocols
# ---------------------------------------------------------------------------


class PipelineFailurePublisher(Protocol):
    """Narrow Protocol the recovery pass uses to emit ``build-failed``.

    Production wiring: :class:`forge.adapters.nats.pipeline_publisher.PipelinePublisher`.
    Test wiring: a duck-typed mock with a ``publish_build_failed``
    coroutine. Keeping the recovery module's dependency a Protocol keeps
    the unit test surface free of NATS plumbing.
    """

    async def publish_build_failed(
        self, payload: Any
    ) -> None:  # pragma: no cover - protocol stub
        """Publish ``pipeline.build-failed.{feature_id}`` for ``payload``."""
        ...


class ApprovalRepublisher(Protocol):
    """Narrow Protocol the recovery pass uses to re-issue PAUSED approvals.

    Production wiring: :class:`forge.adapters.nats.approval_publisher.ApprovalPublisher`.
    Test wiring: a duck-typed mock with a ``publish_request`` coroutine
    that records the last envelope for assertion.

    The envelope passed to ``publish_request`` carries an
    :class:`nats_core.events.ApprovalRequestPayload` whose ``request_id``
    is the **verbatim** ``builds.pending_approval_request_id`` (sc_004).
    """

    async def publish_request(
        self, envelope: Any
    ) -> None:  # pragma: no cover - protocol stub
        """Publish the approval request carried by ``envelope``."""
        ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_failed_payload(build: BuildRow) -> Any:
    """Build the ``BuildFailedPayload`` for a PREPARING-recovery emit.

    The recovery emit signals that the previous process interrupted the
    build mid-PREPARING. ``recoverable=True`` instructs downstream
    consumers (notification adapters, dashboards) to render the failure
    as transient and surface a "will retry" badge rather than a
    terminal failure card.

    Args:
        build: The PREPARING build being marked INTERRUPTED.

    Returns:
        :class:`nats_core.events.BuildFailedPayload` ready for
        :meth:`PipelinePublisher.publish_build_failed`.
    """
    from nats_core.events import BuildFailedPayload

    return BuildFailedPayload(
        feature_id=build.feature_id,
        build_id=build.build_id,
        failure_reason=(
            "recoverable: pipeline restart during PREPARING — "
            "build will be re-picked up via JetStream redelivery"
        ),
        recoverable=True,
    )


# ---------------------------------------------------------------------------
# Per-state handlers
# ---------------------------------------------------------------------------


async def _handle_preparing(
    build: BuildRow,
    persistence: SqliteLifecyclePersistence,
    publisher: PipelineFailurePublisher,
    report: RecoveryReport,
) -> None:
    """Mark PREPARING build INTERRUPTED and emit ``build-failed``."""
    transition = compose_transition(
        Build(build_id=build.build_id, status=BuildState.PREPARING),
        BuildState.INTERRUPTED,
        error="recoverable: pipeline restart during PREPARING",
    )
    persistence.apply_transition(transition)
    report.interrupted_count += 1

    # DDR-029 (TASK-FORGE-FRR-F010D): thread the originating correlation_id
    # onto the v1 BuildFailedPayload so jarvis's forge_subscriber can route
    # the recovery notification back to the originating chat session. The
    # publisher's _publish_envelope reads correlation_id off the payload via
    # ``getattr(payload, "correlation_id", None)``; without this the outbound
    # envelope carries ``correlation_id: null`` (same shape as F010C's bug,
    # different surface). Pattern mirrors forge.pipeline.__init__.emit_failed.
    payload = _build_failed_payload(build)
    attach_correlation_id(payload, build.correlation_id)

    # Emit build-failed AFTER the SQL transition committed — if the wire
    # publish raises, the SQL is still in the recovered state and a
    # follow-up boot will be a clean no-op against INTERRUPTED.
    await publisher.publish_build_failed(payload)


async def _handle_running(
    build: BuildRow,
    persistence: SqliteLifecyclePersistence,
    report: RecoveryReport,
) -> None:
    """Mark RUNNING build INTERRUPTED; re-pickup via NACK on next pull."""
    transition = compose_transition(
        Build(build_id=build.build_id, status=BuildState.RUNNING),
        BuildState.INTERRUPTED,
        error="recoverable: pipeline restart during RUNNING",
    )
    persistence.apply_transition(transition)
    report.interrupted_count += 1


async def _handle_paused(
    build: BuildRow,
    approval_publisher: ApprovalRepublisher,
    report: RecoveryReport,
) -> None:
    """Re-publish the original approval request verbatim (sc_004)."""
    request_id = build.pending_approval_request_id
    if not request_id:
        # Schema invariant: PAUSED implies pending_approval_request_id is
        # non-NULL (TASK-PSM-005 / mark_paused). Surface the violation
        # as a recovery failure so the operator notices the corrupt row
        # without aborting the rest of the pass.
        msg = (
            f"PAUSED build {build.build_id!r} has no "
            "pending_approval_request_id; schema invariant violated — "
            "manual operator intervention required"
        )
        raise RuntimeError(msg)

    # AC-008 single-ownership: the eleven-key ``details`` dict shape is
    # constructed exclusively in
    # :mod:`forge.adapters.nats.approval_publisher`. Late-import keeps
    # the lifecycle module free of an eager NATS dependency.
    from forge.adapters.nats.approval_publisher import (
        build_recovery_approval_envelope,
    )

    envelope = build_recovery_approval_envelope(build)
    await approval_publisher.publish_request(envelope)
    report.paused_reissued_count += 1


async def _handle_finalising(
    build: BuildRow,
    persistence: SqliteLifecyclePersistence,
    report: RecoveryReport,
) -> None:
    """Mark FINALISING build INTERRUPTED with a PR-warning."""
    if build.pr_url:
        msg = f"finalising-interrupted: PR may exist at {build.pr_url}"
    else:
        msg = "finalising-interrupted: PR creation status unknown"

    transition = compose_transition(
        Build(build_id=build.build_id, status=BuildState.FINALISING),
        BuildState.INTERRUPTED,
        error=msg,
    )
    persistence.apply_transition(transition)
    report.finalising_warnings.append(f"{build.build_id}: {msg}")
    report.interrupted_count += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def reconcile_on_boot(
    persistence: SqliteLifecyclePersistence,
    publisher: PipelineFailurePublisher,
    approval_publisher: ApprovalRepublisher,
) -> RecoveryReport:
    """Reconcile every non-terminal build per ``API-sqlite-schema.md §6``.

    Single entry point called from the agent-runtime startup hook. The
    full per-state matrix is documented at module level. Each per-build
    handler runs inside an isolated ``try``/``except`` so a single
    misbehaving row does not abort the rest of the pass.

    Args:
        persistence: The SQLite lifecycle persistence facade. Reads
            non-terminal rows and routes every state transition through
            :meth:`SqliteLifecyclePersistence.apply_transition` (concern
            ``sc_001``). The recovery pass never opens its own
            connection — it inherits the writer connection from the
            facade.
        publisher: Pipeline-event publisher used to emit
            ``pipeline.build-failed`` for PREPARING-recovery (Protocol-
            typed so the unit tests can substitute a mock).
        approval_publisher: Approval-request publisher used to re-issue
            PAUSED-recovery approval envelopes. The envelope's
            ``request_id`` is the **verbatim** original
            ``builds.pending_approval_request_id`` (concern ``sc_004``).

    Returns:
        :class:`RecoveryReport` with per-state counts, FINALISING
        warnings, and any per-build failures. The operator should log
        the report and surface non-empty ``failures`` and
        ``finalising_warnings`` to the boot console.
    """
    report = RecoveryReport()
    builds = persistence.read_non_terminal_builds()

    for build in builds:
        try:
            await _reconcile_one(
                build,
                persistence,
                publisher,
                approval_publisher,
                report,
            )
        except Exception as exc:  # noqa: BLE001 — blanket catch is intentional
            # Failure isolation (AC): record + continue. The exception
            # is preserved on the report rather than re-raised so the
            # caller sees every non-terminal build outcome in one pass.
            logger.warning(
                "recovery handler failed build_id=%s status=%s error=%s",
                build.build_id,
                build.status.value,
                exc,
            )
            report.failures.append((build.build_id, exc))

    logger.info(
        "recovery complete: interrupted=%d paused_reissued=%d "
        "skipped=%d warnings=%d failures=%d",
        report.interrupted_count,
        report.paused_reissued_count,
        report.skipped_count,
        len(report.finalising_warnings),
        len(report.failures),
    )
    return report


async def _reconcile_one(
    build: BuildRow,
    persistence: SqliteLifecyclePersistence,
    publisher: PipelineFailurePublisher,
    approval_publisher: ApprovalRepublisher,
    report: RecoveryReport,
) -> None:
    """Dispatch a single non-terminal build to its per-state handler."""
    status = build.status

    if status is BuildState.QUEUED:
        # JetStream redelivers if the original message was unacked; no
        # local action required.
        report.skipped_count += 1
        return

    if status is BuildState.INTERRUPTED:
        # Already in the post-recovery state — second-run idempotency
        # depends on this branch being a strict no-op.
        report.skipped_count += 1
        return

    if status is BuildState.PREPARING:
        await _handle_preparing(build, persistence, publisher, report)
        return

    if status is BuildState.RUNNING:
        await _handle_running(build, persistence, report)
        return

    if status is BuildState.PAUSED:
        await _handle_paused(build, approval_publisher, report)
        return

    if status is BuildState.FINALISING:
        await _handle_finalising(build, persistence, report)
        return

    # Defensive: terminal states are filtered by
    # read_non_terminal_builds, so reaching this branch means the schema
    # gained a new state without the recovery matrix being updated.
    msg = (
        f"reconcile_on_boot: build {build.build_id!r} has unexpected "
        f"non-terminal status {status.value!r}; recovery matrix needs "
        "updating in forge.lifecycle.recovery"
    )
    raise RuntimeError(msg)
