"""DEPLOY + LIVE_GATE stage orchestration (WS2-B8, scope-design §4).

:class:`DeployStageRunner` ties the deploy machinery together and drives the
scope-§4 event flow for one feature, end to end:

    reservation.acquire
      → DeployQueued → DeployStarted
      → DEPLOY runbook (broker_preflight … deploy_compose … health_check)
        via the SHIPPED FMDR RunbookExecutor (never a second executor)
      → write F7 deploy record
      → DeployComplete   [or DeployFailed + record addendum on step failure]
      → LIVE_GATE runbook (run_live_gate → guardkit qa live-gate via the seam)
      → QAVerdict + LiveGateResult
    reservation.release   (always, in finally)

Config-gated: the DeployStageRunner is only *constructed and driven* when
``deploy.enabled`` is true (default False — inert in production until V1). The
runner itself carries a ``dry_run`` flag so the fleet-memory exemplar can be
dry-run with zero blast radius (the B8 gate).

Irreversible-edge escalation reuses the EXISTING approval-gate machinery
(Gate G1-proven) — not re-implemented here: a profile step that needs approval
emits an ``awaiting_approval`` outcome, which the executor already routes to the
same phone-approval loop (`gate_check`/`maybe_gate_build`). The runner surfaces
that escalation as a non-failed, non-complete pause.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from nats_core.events import (
    AssertionResult,
    DeployCompletePayload,
    DeployFailedPayload,
    DeployQueuedPayload,
    DeployStartedPayload,
    LiveGateResultPayload,
    QAVerdictPayload,
)

from forge.adapters.nats.deploy_publisher import DeployPublisher
from forge.adapters.nats.runbook_publisher import RunbookPublisher
from forge.config.models import DeployStageConfig
from forge.deploy.deploy_record import (
    DeployClaim,
    DeployRecord,
    write_deploy_record,
)
from forge.deploy.live_gate import BrokerInspector, LiveGateInvoker
from forge.deploy.profile import DeployProfile
from forge.deploy.reservation import (
    ReservationError,
    ReservationHandle,
    ReservationLease,
)
from forge.deploy.runbook_builder import (
    build_deploy_runbook,
    build_live_gate_runbook,
)
from forge.deploy.steps import SecretPresenceResolver, register_deploy_handlers
from forge.executor.executor import RunbookExecutor, RunResult
from forge.executor.registry import StepTypeRegistry
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import Runbook

logger = logging.getLogger(__name__)

__all__ = ["DeployStageRunner", "DeployStageResult"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DeployStageResult:
    """The outcome of one deploy-stage run.

    Attributes:
        outcome: "complete" (deploy + optional gate ran), "failed" (a deploy
            step failed), or "escalated" (an irreversible-edge approval pause).
        deploy_run_id: The raw forge run id for this DEPLOY execution.
        deploy_record_ref: Path of the written F7 record (None if not written).
        verdict: The live-gate verdict (None if the gate did not run).
        failed_step: The step type at failure (None unless outcome == failed).
        events: Ordered names of the deploy-domain events published (for the
            gate assertion — the full lifecycle sequence).
        deploy_runbook_id: The DEPLOY runbook id.
        live_gate_runbook_id: The LIVE_GATE runbook id (None if not run).
        dry_run: True when this was a dry-run deploy.
    """

    outcome: str
    deploy_run_id: str
    deploy_record_ref: str | None = None
    verdict: str | None = None
    failed_step: str | None = None
    events: tuple[str, ...] = ()
    deploy_runbook_id: str | None = None
    live_gate_runbook_id: str | None = None
    dry_run: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


class DeployStageRunner:
    """Drives the DEPLOY and LIVE_GATE stages for one feature.

    Args:
        repository: The runbook persistence repository (SQLite).
        runbook_publisher: Publishes the FMDR step-lifecycle events (reused).
        deploy_publisher: Publishes the B7 deploy-domain events.
        reservation: The reservation-lease backend (scope Q2, swappable).
        live_gate_invoker: Shells ``guardkit qa live-gate`` (frozen seam).
        broker_inspector: Diffs live broker vs the F6 contract.
        config: The deploy-stage config (``dry_run`` is passed separately so a
            production-config run can still be dry-run for validation).
        deploy_record_root: Root dir for F7 records (config ``deploy_record_dir``).
        dry_run: When True, deploy steps record intent instead of acting.
        clock: Injected ``() -> datetime`` (UTC).
        presence_resolver: Secret-ref presence check (never returns values).
    """

    def __init__(
        self,
        *,
        repository: RunbookRepository,
        runbook_publisher: RunbookPublisher,
        deploy_publisher: DeployPublisher,
        reservation: ReservationLease,
        live_gate_invoker: LiveGateInvoker,
        broker_inspector: BrokerInspector,
        config: DeployStageConfig,
        deploy_record_root: str,
        dry_run: bool = False,
        clock: Callable[[], datetime] = _utcnow,
        presence_resolver: SecretPresenceResolver | None = None,
    ) -> None:
        self._repo = repository
        self._runbook_publisher = runbook_publisher
        self._deploy_publisher = deploy_publisher
        self._reservation = reservation
        self._live_gate_invoker = live_gate_invoker
        self._broker_inspector = broker_inspector
        self._config = config
        self._deploy_record_root = deploy_record_root
        self._dry_run = dry_run
        self._clock = clock
        self._presence_resolver = presence_resolver

    def _build_registry(self) -> StepTypeRegistry:
        registry = StepTypeRegistry()
        register_deploy_handlers(
            registry,
            dry_run=self._dry_run,
            live_gate_invoker=self._live_gate_invoker,
            broker_inspector=self._broker_inspector,
            presence_resolver=self._presence_resolver,
        )
        return registry

    async def _safe_publish(self, method, payload) -> None:
        """Publish a deploy event; a publish failure never rolls back state."""
        try:
            await method(payload)
        except Exception as exc:  # noqa: BLE001 — event stream is derived
            logger.warning("deploy stage publish failed (continuing): %s", exc)

    async def run_deploy(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        feature: str | None = None,
        feat_id: str | None = None,
        task_id: str | None = None,
        deploy_profile_ref: str | None = None,
        deployer: str | None = None,
    ) -> DeployStageResult:
        """Run the DEPLOY (+ optional LIVE_GATE) stage for ``profile``.

        Returns a :class:`DeployStageResult`. Never raises past its boundary —
        a reservation or step failure is recorded and published as an honest
        DeployFailed, never a silent success.
        """
        events: list[str] = []
        profile_ref = deploy_profile_ref or profile.source_ref
        deployer = deployer or deploy_run_id
        reservation_resource = profile.reservation_resource
        handle: ReservationHandle | None = None

        # --- reservation.acquire -------------------------------------------
        if reservation_resource is not None:
            try:
                handle = self._reservation.acquire(
                    reservation_resource, holder=correlation_id
                )
            except ReservationError as exc:
                # Loud, honest failure — never proceed unprotected.
                return await self._fail_before_start(
                    profile,
                    correlation_id=correlation_id,
                    deploy_run_id=deploy_run_id,
                    feat_id=feat_id,
                    task_id=task_id,
                    profile_ref=profile_ref,
                    failed_step="reservation",
                    failure_reason=str(exc),
                    events=events,
                )

        try:
            # --- DeployQueued / DeployStarted ------------------------------
            queued_at = self._clock()
            await self._safe_publish(
                self._deploy_publisher.publish_deploy_queued,
                DeployQueuedPayload(
                    correlation_id=correlation_id,
                    env_id=profile.env_id,
                    deploy_run_id=deploy_run_id,
                    feat_id=feat_id,
                    task_id=task_id,
                    target_repo=None,
                    deploy_profile_ref=profile_ref,
                    hosts=profile.host_names or None,
                    reservation_resource=reservation_resource,
                    queued_at=queued_at,
                ),
            )
            events.append("DeployQueued")

            deploy_runbook = build_deploy_runbook(
                profile,
                runbook_id=f"deploy-{deploy_run_id}",
                target=profile.env_id,
                now=self._clock(),
            )
            await self._safe_publish(
                self._deploy_publisher.publish_deploy_started,
                DeployStartedPayload(
                    correlation_id=correlation_id,
                    env_id=profile.env_id,
                    deploy_run_id=deploy_run_id,
                    feat_id=feat_id,
                    task_id=task_id,
                    deploy_profile_ref=profile_ref,
                    runbook_ref=deploy_runbook.runbook_id,
                    hosts=profile.host_names or None,
                    reservation_resource=reservation_resource,
                    started_at=self._clock(),
                ),
            )
            events.append("DeployStarted")

            # --- DEPLOY runbook (shipped FMDR executor) --------------------
            run_result = await self._run_runbook(deploy_runbook, correlation_id)
            executed = self._repo.load_runbook(
                deploy_runbook.runbook_id, correlation_id=correlation_id
            )

            if run_result.status != "complete":
                return await self._on_deploy_not_complete(
                    profile,
                    run_result=run_result,
                    executed=executed,
                    correlation_id=correlation_id,
                    deploy_run_id=deploy_run_id,
                    feat_id=feat_id,
                    task_id=task_id,
                    profile_ref=profile_ref,
                    deployer=deployer,
                    events=events,
                )

            # --- F7 deploy record + DeployComplete -------------------------
            completed_at = self._clock()
            record_ref = self._write_record(
                profile,
                executed=executed,
                deploy_run_id=deploy_run_id,
                deployer=deployer,
                profile_ref=profile_ref,
                task_id=task_id,
                status="complete",
                when=completed_at,
            )
            await self._safe_publish(
                self._deploy_publisher.publish_deploy_complete,
                DeployCompletePayload(
                    correlation_id=correlation_id,
                    env_id=profile.env_id,
                    deploy_run_id=deploy_run_id,
                    feat_id=feat_id,
                    task_id=task_id,
                    deploy_record_ref=record_ref,
                    deploy_profile_ref=profile_ref,
                    runbook_ref=deploy_runbook.runbook_id,
                    hosts=profile.host_names or None,
                    reservation_resource=reservation_resource,
                    completed_at=completed_at,
                ),
            )
            events.append("DeployComplete")

            # --- LIVE_GATE (optional) --------------------------------------
            verdict: str | None = None
            live_gate_runbook_id: str | None = None
            if self._config.run_live_gate:
                verdict, live_gate_runbook_id = await self._run_live_gate(
                    profile,
                    correlation_id=correlation_id,
                    deploy_run_id=deploy_run_id,
                    feature=feature or (feat_id or profile.env_id),
                    feat_id=feat_id,
                    task_id=task_id,
                    events=events,
                )

            return DeployStageResult(
                outcome="complete",
                deploy_run_id=deploy_run_id,
                deploy_record_ref=record_ref,
                verdict=verdict,
                events=tuple(events),
                deploy_runbook_id=deploy_runbook.runbook_id,
                live_gate_runbook_id=live_gate_runbook_id,
                dry_run=self._dry_run,
            )
        finally:
            if handle is not None:
                self._reservation.release(handle)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_runbook(self, runbook: Runbook, correlation_id: str) -> RunResult:
        """Persist a runbook and run it through the shipped FMDR executor."""
        self._repo.create_runbook(runbook, correlation_id=correlation_id)
        executor = RunbookExecutor(
            self._repo, self._build_registry(), self._runbook_publisher
        )
        return await executor.run(runbook.runbook_id, correlation_id=correlation_id)

    def _claims_from_runbook(
        self, runbook: Runbook | None, *, when: datetime
    ) -> list[DeployClaim]:
        """Build one F7 claim per executed step (the step result IS the artifact)."""
        claims: list[DeployClaim] = []
        if runbook is None:
            return claims
        for step in runbook.steps:
            payload = step.result.payload if step.result else None
            dry = bool(payload.get("dry_run")) if isinstance(payload, dict) else False
            exit_code = payload.get("exit_code") if isinstance(payload, dict) else None
            artifact = (
                f"runbook:{runbook.runbook_id}#step-{step.sequence_index}"
                f" status={step.status.value}"
            )
            if exit_code is not None:
                artifact += f" exit_code={exit_code}"
            if dry:
                artifact += " (dry-run projection)"
            claim = f"{step.step_type} {step.status.value}" + (
                " [dry-run]" if dry else ""
            )
            claims.append(
                DeployClaim(
                    runtime_claim=claim,
                    evidence_artifact=artifact,
                    committed_at=when,
                )
            )
        return claims

    def _write_record(
        self,
        profile: DeployProfile,
        *,
        executed: Runbook | None,
        deploy_run_id: str,
        deployer: str,
        profile_ref: str | None,
        task_id: str | None,
        status: str,
        when: datetime,
    ) -> str:
        claims = self._claims_from_runbook(executed, when=when)
        record = DeployRecord(
            env=profile.env_id,
            date=when,
            deployer=deployer,
            runbook_ref=f"deploy-{deploy_run_id}",
            deploy_profile_ref=profile_ref,
            claims=tuple(claims),
            status=status,
            dry_run=self._dry_run,
            task_id=task_id,
        )
        return write_deploy_record(record, root=self._deploy_record_root)

    async def _run_live_gate(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        feature: str,
        feat_id: str | None,
        task_id: str | None,
        events: list[str],
    ) -> tuple[str | None, str | None]:
        """Run the LIVE_GATE runbook and publish QAVerdict + LiveGateResult."""
        gate_runbook = build_live_gate_runbook(
            profile,
            runbook_id=f"live-gate-{deploy_run_id}",
            target=profile.env_id,
            feature=feature,
            now=self._clock(),
        )
        run_result = await self._run_runbook(gate_runbook, correlation_id)
        executed = self._repo.load_runbook(
            gate_runbook.runbook_id, correlation_id=correlation_id
        )
        payload = None
        if executed is not None and executed.steps:
            step = executed.steps[0]
            payload = step.result.payload if step.result else None

        if run_result.status != "complete" or not isinstance(payload, dict):
            # The gate step failed (e.g. unconfigured invoker raised). Honest:
            # an instrument problem, not a SUT verdict — no QA verdict published.
            logger.warning(
                "live-gate step did not produce a verdict (run=%s)",
                run_result.status,
            )
            return None, gate_runbook.runbook_id

        verdict = str(payload.get("verdict", "environment_fail"))
        assertions = tuple(
            AssertionResult(**a) if isinstance(a, dict) else a
            for a in payload.get("assertions", [])
        )
        decided_at = self._clock()
        common = {
            "correlation_id": correlation_id,
            "run_id": str(payload.get("run_id") or f"{feature}-{profile.env_id}"),
            "env_id": profile.env_id,
            "verdict": verdict,
            "gate_ids": list(payload.get("gate_ids", [])),
            "evidence_index_ref": str(payload.get("evidence_index_ref") or ""),
            "attempt": 1,
            "feat_id": feat_id,
            "task_id": task_id,
            "app_url": payload.get("app_url"),
            "leak_sweep_findings": payload.get("leak_sweep_findings"),
        }
        await self._safe_publish(
            self._deploy_publisher.publish_qa_verdict,
            QAVerdictPayload(
                **common,
                assertions=list(assertions),
                dispositions_ref=payload.get("dispositions_ref"),
                attempts_ledger_ref=payload.get("attempts_ledger_ref"),
                decided_at=decided_at,
            ),
        )
        events.append("QAVerdict")
        await self._safe_publish(
            self._deploy_publisher.publish_live_gate_result,
            LiveGateResultPayload(
                **common,
                assertions=list(assertions),
                screenshot_refs=list(payload.get("screenshot_refs", [])),
                trace_refs=list(payload.get("trace_refs", [])),
                finished_at=decided_at,
            ),
        )
        events.append("LiveGateResult")
        return verdict, gate_runbook.runbook_id

    async def _on_deploy_not_complete(
        self,
        profile: DeployProfile,
        *,
        run_result: RunResult,
        executed: Runbook | None,
        correlation_id: str,
        deploy_run_id: str,
        feat_id: str | None,
        task_id: str | None,
        profile_ref: str | None,
        deployer: str,
        events: list[str],
    ) -> DeployStageResult:
        """Handle a DEPLOY runbook that escalated (step failure or approval pause)."""
        failed_step = "unknown"
        if executed is not None and run_result.stopped_at_index is not None:
            idx = run_result.stopped_at_index
            if 0 <= idx < len(executed.steps):
                failed_step = executed.steps[idx].step_type

        # An awaiting_approval pause is an irreversible-edge escalation handled
        # by the EXISTING approval-gate loop — not a failed deploy.
        if run_result.reason == "awaiting_approval":
            return DeployStageResult(
                outcome="escalated",
                deploy_run_id=deploy_run_id,
                failed_step=failed_step,
                events=tuple(events),
                deploy_runbook_id=f"deploy-{deploy_run_id}",
                dry_run=self._dry_run,
                detail={"reason": "awaiting_approval"},
            )

        when = self._clock()
        # Best-effort F7 addendum: a failed run still leaves a record IF it has
        # evidenced claims (at least one step ran). A pre-step failure has none,
        # so the record is omitted and deploy_record_ref stays None (honest).
        record_ref: str | None = None
        try:
            record_ref = self._write_record(
                profile,
                executed=executed,
                deploy_run_id=deploy_run_id,
                deployer=deployer,
                profile_ref=profile_ref,
                task_id=task_id,
                status="failed",
                when=when,
            )
        except Exception as exc:  # noqa: BLE001 — record is best-effort on failure
            logger.info("no F7 record written for failed deploy: %s", exc)

        await self._safe_publish(
            self._deploy_publisher.publish_deploy_failed,
            DeployFailedPayload(
                correlation_id=correlation_id,
                env_id=profile.env_id,
                deploy_run_id=deploy_run_id,
                failed_step=failed_step,
                failure_reason=f"deploy runbook escalated: {run_result.reason}",
                recoverable=True,
                feat_id=feat_id,
                task_id=task_id,
                deploy_record_ref=record_ref,
                deploy_profile_ref=profile_ref,
                runbook_ref=f"deploy-{deploy_run_id}",
                hosts=profile.host_names or None,
                reservation_resource=profile.reservation_resource,
                failed_at=when,
            ),
        )
        events.append("DeployFailed")
        return DeployStageResult(
            outcome="failed",
            deploy_run_id=deploy_run_id,
            deploy_record_ref=record_ref,
            failed_step=failed_step,
            events=tuple(events),
            deploy_runbook_id=f"deploy-{deploy_run_id}",
            dry_run=self._dry_run,
        )

    async def _fail_before_start(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        feat_id: str | None,
        task_id: str | None,
        profile_ref: str | None,
        failed_step: str,
        failure_reason: str,
        events: list[str],
    ) -> DeployStageResult:
        """Publish DeployFailed for a failure before the runbook started."""
        await self._safe_publish(
            self._deploy_publisher.publish_deploy_failed,
            DeployFailedPayload(
                correlation_id=correlation_id,
                env_id=profile.env_id,
                deploy_run_id=deploy_run_id,
                failed_step=failed_step,
                failure_reason=failure_reason,
                recoverable=True,
                feat_id=feat_id,
                task_id=task_id,
                deploy_record_ref=None,
                deploy_profile_ref=profile_ref,
                hosts=profile.host_names or None,
                reservation_resource=profile.reservation_resource,
                failed_at=self._clock(),
            ),
        )
        events.append("DeployFailed")
        return DeployStageResult(
            outcome="failed",
            deploy_run_id=deploy_run_id,
            failed_step=failed_step,
            events=tuple(events),
            dry_run=self._dry_run,
        )
