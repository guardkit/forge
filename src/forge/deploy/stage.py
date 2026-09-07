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
      → [O-32] on verdict != "pass": REVERT runbook (re-deploy the kept
        :rollback-* tag via the same seam) → DeployReverted; outcome="reverted"
    reservation.release   (always, in finally)

O-32 (the endpoint's word "verified", enforced): a FAILED post-deploy live-gate
means the current build is NOT verified, so the runner rolls back — it does not
return ``outcome="complete"`` regardless of the verdict. If the profile carries
no rollback image ref, the revert is a LOUD terminal failure (``outcome="failed"``,
``failed_step="revert"``) — never a silent keep-serving of the failed build.

Config-gated: the DeployStageRunner is only *constructed and driven* when
``deploy.enabled`` is true (default False — inert in production until V1). The
runner itself carries a ``dry_run`` flag so the fleet-memory exemplar can be
dry-run with zero blast radius (the B8 gate).

DEPLOYING INTO A DOCKER SANDBOX (2026-09-06 decision): when the target repo's
profile carries a ``sandbox`` block, every step that runs a script — the
candidate deploy, the promote, the revert, the candidate teardown and the
health checks — carries the sandbox's five settings (``SANDBOX_NAME``,
``SANDBOX_MEMORY``, ``SANDBOX_CPUS``, ``SANDBOX_PUBLISH``,
``SANDBOX_ALLOW_NETWORK``) in its environment, alongside the ``CANDIDATE`` /
``PROMOTE`` / ``CANDIDATE_DOWN`` mode signal and the candidate addressing
overlay this runner already threads. The repository's vetted wrapper reads them
and runs the repository's own unchanged deploy script inside that sandbox. The
threading itself lives one file over, in
:mod:`forge.deploy.runbook_builder` (:func:`~forge.deploy.runbook_builder.sandbox_env`),
because that is where the steps are minted. No ``sandbox`` block ⇒ nothing is
added ⇒ every runbook is byte-identical to what it was.

Irreversible-edge escalation reuses the EXISTING approval-gate machinery
(Gate G1-proven) — not re-implemented here: a profile step that needs approval
emits an ``awaiting_approval`` outcome, which the executor already routes to the
same phone-approval loop (`gate_check`/`maybe_gate_build`). The runner surfaces
that escalation as a non-failed, non-complete pause.

PROTECT MAIN (the rewrite-on-refusal spec, Part J, 2026-09-07, rule 39): the
stage is two callable legs so the merge word can put the merge BETWEEN them —

* :meth:`DeployStageRunner.candidate_check` — the candidate up, healthy and
  through the live gate. On a pass the candidate is LEFT STANDING (its image
  is what the promote re-tags); on a fail it is torn down. It accepts a
  working directory for the candidate (the feature branch's laid-out tree,
  rule 38), so the candidate is built from the exact commit the merge will
  land, never from the checkout's main.
* :meth:`DeployStageRunner.promote` — ``PROMOTE=1`` (a re-tag, never a
  rebuild), the live gate on the live name, the O-32 revert if that fails, and
  the candidate torn down.
* :meth:`DeployStageRunner.candidate_down` — the teardown on its own, for a
  run that stops between the two legs (a refused merge after a green check).

:meth:`DeployStageRunner.run_deploy` — today's one-call shape — is the two
legs in a row and is kept for the attended ``forge deploy`` command and every
existing caller; its events, runbooks and results are what they were.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from nats_core.events import (
    AssertionResult,
    DeployCompletePayload,
    DeployFailedPayload,
    DeployQueuedPayload,
    DeployRevertedPayload,
    DeployStartedPayload,
    LiveGateResultPayload,
    QAVerdictPayload,
)

from forge.adapters.nats.deploy_publisher import DeployPublisher
from forge.adapters.nats.runbook_publisher import RunbookPublisher
from forge.config.models import DeployStageConfig
from forge.deploy.demotion_event import write_demotion_event
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
    build_candidate_teardown_runbook,
    build_deploy_runbook,
    build_live_gate_runbook,
    build_revert_runbook,
)
from forge.deploy.sidecar_runner import SidecarScriptRunner
from forge.deploy.steps import SecretPresenceResolver, register_deploy_handlers
from forge.executor.executor import RunbookExecutor, RunResult
from forge.executor.registry import StepTypeRegistry
from forge.executor.shell_steps import ScriptRunner
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import Runbook

logger = logging.getLogger(__name__)

__all__ = ["DeployStageRunner", "DeployStageResult", "gate_summary"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DeployStageResult:
    """The outcome of one deploy-stage run.

    Attributes:
        outcome: "complete" (deploy + optional gate PASSED), "reverted" (the
            live-gate verdict != pass and the deploy was rolled back to the kept
            :rollback-* image — O-32), "failed" (a deploy or revert step failed,
            including a gate-fail with no rollback ref to revert to), or
            "escalated" (an irreversible-edge approval pause).
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


@dataclass(frozen=True, slots=True)
class _LiveGateRun:
    """What one run of the live gate said, as the stage reads it back.

    ``verdict`` is None when the gate produced no verdict at all (an
    instrument problem). ``gate_ids`` names every check that ran;
    ``assertions`` carries each check's own results when the invoker reports
    them, so the stage can say which checks failed by name.
    """

    verdict: str | None
    runbook_id: str
    failing_verdict_ref: str | None
    gate_ids: tuple[str, ...] = ()
    assertions: tuple[dict[str, Any], ...] = ()


def gate_summary(
    *,
    verdict: str | None,
    gate_ids: tuple[str, ...] | list[str],
    assertions: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    live_gate_runbook_id: str | None = None,
) -> dict[str, Any]:
    """The candidate check in numbers and names: how many checks, how many
    passed, and which failed.

    Counts are read from the per-check results when the invoker reported
    them; a verdict of pass with no per-check results counts every check as
    passed; a verdict that is not pass with no per-check results leaves the
    count and the names unknown (None) rather than guessing — the sentence a
    person reads then says the failing checks were not reported.
    """
    names = [str(g) for g in gate_ids if str(g).strip()]
    per_gate: dict[str, bool] = {}
    for entry in assertions:
        if not isinstance(entry, dict):
            continue
        gate = str(entry.get("gate_id") or "").strip()
        if not gate:
            continue
        status = str(entry.get("status") or "").strip().lower()
        per_gate[gate] = per_gate.get(gate, True) and status == "pass"
    if not names:
        names = list(per_gate)
    total: int | None = len(names) if names else None
    failed: list[str] | None
    passed: int | None
    if per_gate:
        failed = [n for n in names if per_gate.get(n) is False]
        if verdict != "pass" and not failed:
            failed, passed = None, None
        else:
            passed = (total - len(failed)) if total is not None else None
    elif verdict == "pass" and total is not None:
        failed, passed = [], total
    else:
        failed, passed = None, None
    return {
        "verdict": verdict,
        "checks_total": total,
        "checks_passed": passed,
        "failed_checks": failed,
        "gate_ids": names,
        "live_gate_runbook_id": live_gate_runbook_id,
    }


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
        target_repo_root: The target repo's filesystem root. When set, the [MG-5]
            live-gate-failure demotion edge writes its DF-021 demotion event under
            ``<root>/qa/`` (beside the gates) for the trust ledger; None ⇒ no-op.
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
        target_repo: str | None = None,
        target_repo_root: str | None = None,
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
        self._target_repo = target_repo
        # [MG-5] The target repo's filesystem root — the demotion-event emission
        # writes under ``<root>/qa/`` (beside the live-gate gates), where the
        # DF-021 trust ledger reads it. None (older callers/tests) → the emission
        # is a no-op, since it cannot name the qa/ tree.
        self._target_repo_root = target_repo_root

    def _resolve_script_runner(self) -> ScriptRunner | None:
        """The docker-touching-step execution seam for this stage.

        ``None`` (deploy.execution_surface='local', the default) keeps every
        step on the in-process subprocess core — byte-identical to before the
        seam existed. 'sidecar' builds a :class:`SidecarScriptRunner` bound to
        the target repo, so ``deploy_compose``/``health_check`` execute on the
        docker-capable host without a docker socket in the container (S1).
        """
        if self._config.execution_surface != "sidecar":
            return None
        if not self._target_repo:
            # Deny by default: a sidecar surface with no target repo cannot name
            # the repo the sidecar must resolve. Fail loud rather than silently
            # fall back to the (docker-less) local surface.
            raise ValueError(
                "deploy.execution_surface='sidecar' requires a target_repo "
                "(the org/name key the sidecar resolves via "
                "planning.target_repo_paths); none was threaded into the "
                "DeployStageRunner"
            )
        return SidecarScriptRunner(
            base_url=self._config.sidecar_url, repo=self._target_repo
        )

    def _build_registry(
        self, *, live_gate_invoker: LiveGateInvoker | None = None
    ) -> StepTypeRegistry:
        registry = StepTypeRegistry()
        register_deploy_handlers(
            registry,
            dry_run=self._dry_run,
            live_gate_invoker=live_gate_invoker or self._live_gate_invoker,
            broker_inspector=self._broker_inspector,
            presence_resolver=self._presence_resolver,
            script_runner=self._resolve_script_runner(),
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
        """Run the DEPLOY (+ optional LIVE_GATE) stage for ``profile`` in one call.

        The two legs in a row: :meth:`candidate_check` when the profile has a
        candidate section (a candidate that fails is torn down and the run
        ends here — the live name is never touched), then :meth:`promote`.
        Without a candidate section this is the direct-live flow. Returns a
        :class:`DeployStageResult`. Never raises past its boundary — a
        reservation or step failure is recorded and published as an honest
        DeployFailed, never a silent success.
        """
        prior_events: tuple[str, ...] = ()
        if profile.candidate is not None:
            checked = await self.candidate_check(
                profile,
                correlation_id=correlation_id,
                deploy_run_id=deploy_run_id,
                feature=feature,
                feat_id=feat_id,
                task_id=task_id,
                deploy_profile_ref=deploy_profile_ref,
            )
            if checked.outcome != "complete":
                return checked
            prior_events = checked.events
        return await self.promote(
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            feature=feature,
            feat_id=feat_id,
            task_id=task_id,
            deploy_profile_ref=deploy_profile_ref,
            deployer=deployer,
            prior_events=prior_events,
        )

    async def candidate_check(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        feature: str | None = None,
        feat_id: str | None = None,
        task_id: str | None = None,
        deploy_profile_ref: str | None = None,
        candidate_cwd: str | None = None,
    ) -> DeployStageResult:
        """Leg one: the candidate up, healthy, and through the live gate.

        ``candidate_cwd`` is the working directory the candidate's steps run
        in — the feature branch's laid-out tree (protect-main, rule 38) — so
        the repository's own deploy script, found relative to it, builds that
        tree. ``None`` runs from the profile's ``cwd`` as before.

        Returns ``outcome="complete"`` with ``verdict="pass"`` and the
        candidate LEFT STANDING (the promote re-tags its image), and
        ``detail["gate_summary"]`` saying how many checks ran and passed.
        Returns ``outcome="failed"`` when the candidate could not start or
        its gate did not pass — the candidate is then torn down and the live
        name was never touched — or when the profile has no candidate
        section at all (``detail["reason"] == "no_candidate_section"``).
        """
        events: list[str] = []
        profile_ref = deploy_profile_ref or profile.source_ref
        if profile.candidate is None:
            result = await self._fail_before_start(
                profile,
                correlation_id=correlation_id,
                deploy_run_id=deploy_run_id,
                feat_id=feat_id,
                task_id=task_id,
                profile_ref=profile_ref,
                failed_step="candidate",
                failure_reason=(
                    "the deploy profile has no candidate section, so a "
                    "candidate cannot be checked before the merge"
                ),
                events=events,
            )
            return replace(result, detail={"reason": "no_candidate_section"})

        handle: ReservationHandle | None = None
        reservation_resource = profile.reservation_resource
        if reservation_resource is not None:
            try:
                handle = self._reservation.acquire(
                    reservation_resource, holder=correlation_id
                )
            except ReservationError as exc:
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
            await self._publish_queued(
                profile,
                correlation_id=correlation_id,
                deploy_run_id=deploy_run_id,
                feat_id=feat_id,
                task_id=task_id,
                profile_ref=profile_ref,
                events=events,
            )
            terminal, summary = await self._run_candidate_leg(
                profile,
                correlation_id=correlation_id,
                deploy_run_id=deploy_run_id,
                feature=feature or (feat_id or profile.env_id),
                feat_id=feat_id,
                task_id=task_id,
                profile_ref=profile_ref,
                events=events,
                candidate_cwd=candidate_cwd,
            )
            if terminal is not None:
                return replace(
                    terminal,
                    detail={**terminal.detail, "gate_summary": summary},
                )
            return DeployStageResult(
                outcome="complete",
                deploy_run_id=deploy_run_id,
                verdict=summary.get("verdict"),
                events=tuple(events),
                deploy_runbook_id=f"deploy-cand-{deploy_run_id}",
                live_gate_runbook_id=summary.get("live_gate_runbook_id"),
                dry_run=self._dry_run,
                detail={"gate_summary": summary, "candidate": "standing"},
            )
        finally:
            if handle is not None:
                self._reservation.release(handle)

    async def candidate_down(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
    ) -> DeployStageResult:
        """Tear the standing candidate down on its own — for a run that stops
        between the two legs. Never raises; ``outcome="failed"`` with
        ``failed_step="candidate_down"`` when the teardown did not complete."""
        if profile.candidate is None:
            return DeployStageResult(
                outcome="complete",
                deploy_run_id=deploy_run_id,
                dry_run=self._dry_run,
                detail={"reason": "no_candidate_section", "candidate": "absent"},
            )
        torn_down = await self._teardown_candidate(
            profile, correlation_id=correlation_id, deploy_run_id=deploy_run_id
        )
        return DeployStageResult(
            outcome="complete" if torn_down else "failed",
            deploy_run_id=deploy_run_id,
            failed_step=None if torn_down else "candidate_down",
            dry_run=self._dry_run,
            detail={"candidate": "torn-down" if torn_down else "standing"},
        )

    async def promote(
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
        prior_events: tuple[str, ...] = (),
    ) -> DeployStageResult:
        """Leg two: the live name comes up on the image the candidate built.

        With a candidate section the deploy runs ``PROMOTE=1`` — the
        repository's script re-tags the candidate image as the live image and
        brings the live project up without rebuilding — then the candidate is
        torn down (unless the profile asks to keep it), the live gate runs on
        the live name, and a verdict that is not pass rolls back (O-32).
        Without a candidate section this is the direct-live deploy.

        ``prior_events`` are the events the candidate leg already published
        for this run; DeployQueued is published here only when it is not
        among them, so one run is queued once. The result's ``detail``
        carries ``candidate``: ``"torn-down"``, ``"kept"``, ``"standing"``
        (the promote stopped before the teardown) or ``"absent"``.
        """
        events: list[str] = list(prior_events)
        profile_ref = deploy_profile_ref or profile.source_ref
        deployer = deployer or deploy_run_id
        reservation_resource = profile.reservation_resource
        handle: ReservationHandle | None = None
        candidate_word = "absent" if profile.candidate is None else "standing"

        def _with_candidate(result: DeployStageResult) -> DeployStageResult:
            return replace(result, detail={**result.detail, "candidate": candidate_word})

        # --- reservation.acquire -------------------------------------------
        if reservation_resource is not None:
            try:
                handle = self._reservation.acquire(
                    reservation_resource, holder=correlation_id
                )
            except ReservationError as exc:
                # Loud, honest failure — never proceed unprotected. A standing
                # candidate is not left behind by a promote that never began.
                if profile.candidate is not None:
                    torn = await self._teardown_candidate(
                        profile,
                        correlation_id=correlation_id,
                        deploy_run_id=deploy_run_id,
                    )
                    candidate_word = "torn-down" if torn else "standing"
                failed = await self._fail_before_start(
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
                return _with_candidate(failed)

        try:
            if "DeployQueued" not in events:
                await self._publish_queued(
                    profile,
                    correlation_id=correlation_id,
                    deploy_run_id=deploy_run_id,
                    feat_id=feat_id,
                    task_id=task_id,
                    profile_ref=profile_ref,
                    events=events,
                )

            # With a candidate section the live leg re-tags-and-promotes the
            # candidate-built image (PROMOTE=1, no overlay: promote must NOT
            # rebuild — it re-tags + brings the live project up --no-build,
            # snapshotting the previous live image as the rollback tag).
            promote_extra_env: dict[str, str] | None = (
                {"PROMOTE": "1"} if profile.candidate is not None else None
            )
            deploy_runbook = build_deploy_runbook(
                profile,
                runbook_id=f"deploy-{deploy_run_id}",
                target=profile.env_id,
                now=self._clock(),
                compose_extra_env=promote_extra_env,
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
                return _with_candidate(
                    await self._on_deploy_not_complete(
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

            # --- [candidate leg] post-promote teardown ---------------------
            # The promote succeeded (the candidate image is now the live image),
            # so the ``-cand`` project is redundant. Tear it down unless the
            # profile asked to keep it up for manual poking. Best-effort: a
            # leftover candidate is not a live-deploy failure, so a teardown
            # hiccup is logged, never fails the (already-live) deploy.
            if profile.candidate is not None:
                if profile.candidate.keep:
                    candidate_word = "kept"
                else:
                    torn = await self._teardown_candidate(
                        profile,
                        correlation_id=correlation_id,
                        deploy_run_id=deploy_run_id,
                    )
                    candidate_word = "torn-down" if torn else "standing"

            # --- LIVE_GATE (optional) --------------------------------------
            verdict: str | None = None
            live_gate_runbook_id: str | None = None
            failing_verdict_ref: str | None = None
            if self._config.run_live_gate:
                gate = await self._run_live_gate(
                    profile,
                    correlation_id=correlation_id,
                    deploy_run_id=deploy_run_id,
                    feature=feature or (feat_id or profile.env_id),
                    feat_id=feat_id,
                    task_id=task_id,
                    events=events,
                )
                verdict = gate.verdict
                live_gate_runbook_id = gate.runbook_id
                failing_verdict_ref = gate.failing_verdict_ref

            # --- [O-32] revert-on-gate-fail --------------------------------
            # A live-gate verdict that is not "pass" means the current build is
            # NOT verified. The endpoint's word "verified" is enforced, not
            # decorative: roll back to the kept :rollback-* image rather than
            # keep serving the failed build. (instrument_fail/environment_fail
            # are also != "pass".) A gate that produced NO verdict at all
            # (verdict=None: unconfigured/raising invoker) is an un-run gate,
            # and an un-run gate is not a verified deploy — it reverts as
            # "instrument_fail" rather than silently keeping the build serving.
            if self._config.run_live_gate and verdict != "pass":
                # [MG-5] Demotion edge (H-A Stage 3): a post-merge live-gate that
                # did not pass demotes the lane back to attended. Emit the
                # file-based demotion event into the target repo's qa/ tree BEFORE
                # the revert — it rides the existing O-32 path unconditionally and
                # never alters it (a demotion event with no ledger present is inert
                # data). The revert behaviour below is byte-for-byte untouched.
                self._emit_demotion_event(
                    profile,
                    feature=feature or (feat_id or profile.env_id),
                    feat_id=feat_id,
                    failing_verdict=verdict if verdict is not None else "instrument_fail",
                    failing_verdict_ref=failing_verdict_ref,
                    deploy_run_id=deploy_run_id,
                )
                return _with_candidate(
                    await self._run_revert(
                        profile,
                        correlation_id=correlation_id,
                        deploy_run_id=deploy_run_id,
                        feat_id=feat_id,
                        task_id=task_id,
                        profile_ref=profile_ref,
                        deployer=deployer,
                        failing_verdict=verdict if verdict is not None else "instrument_fail",
                        failing_verdict_ref=failing_verdict_ref,
                        deploy_runbook_id=deploy_runbook.runbook_id,
                        live_gate_runbook_id=live_gate_runbook_id,
                        events=events,
                    )
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
                detail={"candidate": candidate_word},
            )
        finally:
            if handle is not None:
                self._reservation.release(handle)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _publish_queued(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        feat_id: str | None,
        task_id: str | None,
        profile_ref: str | None,
        events: list[str],
    ) -> None:
        """DeployQueued — once per deploy run, whichever leg comes first."""
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
                reservation_resource=profile.reservation_resource,
                queued_at=self._clock(),
            ),
        )
        events.append("DeployQueued")

    async def _run_runbook(
        self,
        runbook: Runbook,
        correlation_id: str,
        *,
        live_gate_invoker: LiveGateInvoker | None = None,
    ) -> RunResult:
        """Persist a runbook and run it through the shipped FMDR executor.

        ``live_gate_invoker`` overrides the injected invoker for this run only
        (the candidate-leg live gate threads a candidate.env-overlaid invoker so
        its driver addresses the ``-cand`` instance). ``None`` = the injected
        invoker unchanged.
        """
        self._repo.create_runbook(runbook, correlation_id=correlation_id)
        executor = RunbookExecutor(
            self._repo,
            self._build_registry(live_gate_invoker=live_gate_invoker),
            self._runbook_publisher,
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
        runbook_id: str | None = None,
        driver_env_overlay: dict[str, str] | None = None,
        publish_domain_events: bool = True,
    ) -> _LiveGateRun:
        """Run the LIVE_GATE runbook and publish QAVerdict + LiveGateResult.

        Returns a :class:`_LiveGateRun` — the verdict, the runbook id, the
        evidence ref (F5 index, falling back to the run id) that lets the O-32
        revert receipt cite the failing gate, and the checks that ran with
        their own results, so the candidate leg can say which failed by name.

        Candidate-then-promote sequencing (S2F): the candidate-leg call passes a
        ``-cand``-suffixed ``runbook_id``, a ``driver_env_overlay`` (candidate.env,
        merged into the driver env so the gate hits the candidate instance), and
        ``publish_domain_events=False`` — the candidate gate is an INTERNAL gate,
        so it emits the FMDR runbook step/receipt events (an honest audit trail)
        but NOT the deploy-domain QAVerdict/LiveGateResult, which stay reserved
        for the ONE live deploy. The promote/direct-live leg keeps the defaults.
        """
        gate_runbook = build_live_gate_runbook(
            profile,
            runbook_id=runbook_id or f"live-gate-{deploy_run_id}",
            target=profile.env_id,
            feature=feature,
            now=self._clock(),
        )
        invoker_override: LiveGateInvoker | None = None
        if driver_env_overlay:
            with_overlay = getattr(self._live_gate_invoker, "with_extra_env", None)
            if callable(with_overlay):
                invoker_override = with_overlay(driver_env_overlay)
            else:
                # The injected invoker cannot carry an env overlay (a dry-run /
                # fixed-verdict test seam). The overlay is best-effort only here;
                # the candidate.env already reaches deploy_compose + health_check
                # (which is what physically addresses the -cand instance).
                logger.debug(
                    "live-gate invoker has no with_extra_env; candidate driver "
                    "overlay not applied"
                )
        run_result = await self._run_runbook(
            gate_runbook, correlation_id, live_gate_invoker=invoker_override
        )
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
            return _LiveGateRun(
                verdict=None, runbook_id=gate_runbook.runbook_id, failing_verdict_ref=None
            )

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
        if publish_domain_events:
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
        failing_verdict_ref = common["evidence_index_ref"] or common["run_id"]
        return _LiveGateRun(
            verdict=verdict,
            runbook_id=gate_runbook.runbook_id,
            failing_verdict_ref=failing_verdict_ref,
            gate_ids=tuple(str(g) for g in payload.get("gate_ids", []) or []),
            assertions=tuple(
                a for a in (payload.get("assertions", []) or []) if isinstance(a, dict)
            ),
        )

    async def _run_candidate_leg(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        feature: str,
        feat_id: str | None,
        task_id: str | None,
        profile_ref: str | None,
        events: list[str],
        candidate_cwd: str | None = None,
    ) -> tuple[DeployStageResult | None, dict[str, Any]]:
        """Stand the candidate up under ``-cand``, gate it, leave-standing-or-teardown.

        Returns ``(None, summary)`` when the candidate PASSED — it is left
        standing for the promote to re-tag, and ``summary`` (see
        :func:`gate_summary`) says how many checks ran and passed. Returns
        ``(terminal, summary)`` with a terminal ``DeployStageResult``
        (outcome="failed", detail reason ``candidate_failed`` /
        ``candidate_deploy_failed``) when the candidate deploy or gate failed —
        in which case the candidate has been torn down and the LIVE name was
        NEVER touched (no DeployStarted, no revert). Emits the FMDR runbook
        step/receipt events for its runbooks; the deploy-domain
        QAVerdict/LiveGateResult stay reserved for the live leg.

        ``candidate_cwd`` — protect-main (rule 38): the working directory of
        every candidate step, the feature branch's laid-out tree; ``None`` is
        the profile's ``cwd``.
        """
        assert profile.candidate is not None  # caller-guarded
        cand_env = dict(profile.candidate.env)
        compose_extra = {"CANDIDATE": "1", **cand_env}
        summary: dict[str, Any] = gate_summary(verdict=None, gate_ids=(), assertions=())
        summary["candidate_cwd"] = candidate_cwd

        # --- candidate deploy (separate -cand project) ---
        cand_runbook = build_deploy_runbook(
            profile,
            runbook_id=f"deploy-cand-{deploy_run_id}",
            target=profile.env_id,
            now=self._clock(),
            compose_extra_env=compose_extra,
            check_extra_env=cand_env,
            cwd_override=candidate_cwd,
        )
        run_result = await self._run_runbook(cand_runbook, correlation_id)
        executed = self._repo.load_runbook(
            cand_runbook.runbook_id, correlation_id=correlation_id
        )
        if run_result.status != "complete":
            failed_step = "candidate_deploy"
            if executed is not None and run_result.stopped_at_index is not None:
                idx = run_result.stopped_at_index
                if 0 <= idx < len(executed.steps):
                    failed_step = executed.steps[idx].step_type
            await self._teardown_candidate(
                profile,
                correlation_id=correlation_id,
                deploy_run_id=deploy_run_id,
            )
            summary["failed_step"] = failed_step
            failed = await self._candidate_failed_result(
                profile,
                correlation_id=correlation_id,
                deploy_run_id=deploy_run_id,
                feat_id=feat_id,
                task_id=task_id,
                profile_ref=profile_ref,
                failed_step=failed_step,
                reason="candidate_deploy_failed",
                failing_verdict=None,
                events=events,
            )
            return failed, summary

        # --- candidate live gate (candidate.env overlay, no domain events) ---
        if self._config.run_live_gate:
            gate = await self._run_live_gate(
                profile,
                correlation_id=correlation_id,
                deploy_run_id=deploy_run_id,
                feature=feature,
                feat_id=feat_id,
                task_id=task_id,
                events=events,
                runbook_id=f"live-gate-cand-{deploy_run_id}",
                driver_env_overlay=cand_env,
                publish_domain_events=False,
            )
            verdict = gate.verdict
            summary = {
                **summary,
                **gate_summary(
                    verdict=verdict,
                    gate_ids=gate.gate_ids,
                    assertions=gate.assertions,
                    live_gate_runbook_id=gate.runbook_id,
                ),
            }
            if verdict != "pass":
                await self._teardown_candidate(
                    profile,
                    correlation_id=correlation_id,
                    deploy_run_id=deploy_run_id,
                )
                failed = await self._candidate_failed_result(
                    profile,
                    correlation_id=correlation_id,
                    deploy_run_id=deploy_run_id,
                    feat_id=feat_id,
                    task_id=task_id,
                    profile_ref=profile_ref,
                    failed_step="candidate_gate",
                    reason="candidate_failed",
                    failing_verdict=verdict if verdict is not None else "instrument_fail",
                    events=events,
                )
                return failed, summary
        else:
            # No live gate configured: the candidate came up and answered its
            # health checks, and that is the whole check.
            summary["verdict"] = "pass"

        return None, summary  # candidate passed → left standing for the promote

    async def _teardown_candidate(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
    ) -> bool:
        """Tear the ``-cand`` compose project down (best-effort, never raises).

        True when the teardown runbook completed; False when it did not, or
        could not be run at all — the ``-cand`` project may then still be up.
        """
        assert profile.candidate is not None
        teardown_env = {"CANDIDATE_DOWN": "1", **dict(profile.candidate.env)}
        teardown_runbook = build_candidate_teardown_runbook(
            profile,
            runbook_id=f"teardown-cand-{deploy_run_id}",
            target=profile.env_id,
            extra_env=teardown_env,
            now=self._clock(),
        )
        try:
            run_result = await self._run_runbook(teardown_runbook, correlation_id)
            if run_result.status != "complete":
                logger.warning(
                    "candidate teardown for %s did not complete (status=%s); "
                    "the -cand project may still be up (manual cleanup)",
                    profile.env_id,
                    run_result.status,
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            logger.warning("candidate teardown raised (continuing): %s", exc)
            return False

    async def _candidate_failed_result(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        feat_id: str | None,
        task_id: str | None,
        profile_ref: str | None,
        failed_step: str,
        reason: str,
        failing_verdict: str | None,
        events: list[str],
    ) -> DeployStageResult:
        """Publish DeployFailed for a candidate that failed its leg (LIVE intact).

        A loud, honest failure that names the candidate as the cause and records
        that the live name was untouched — recoverable (retry the deploy), never
        a revert (there is nothing live to roll back to).
        """
        when = self._clock()
        detail_verdict = (
            f" (candidate live-gate verdict {failing_verdict!r} != 'pass')"
            if failing_verdict is not None
            else ""
        )
        failure_reason = (
            f"candidate leg failed at {failed_step!r}{detail_verdict}; the "
            f"candidate ('{profile.env_id}-cand') was torn down and the LIVE "
            f"name '{profile.env_id}' was never touched (no promote, no revert)"
        )
        logger.error("candidate-then-promote gate refused promote: %s", failure_reason)
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
                runbook_ref=f"deploy-cand-{deploy_run_id}",
                hosts=profile.host_names or None,
                reservation_resource=profile.reservation_resource,
                failed_at=when,
            ),
        )
        events.append("DeployFailed")
        return DeployStageResult(
            outcome="failed",
            deploy_run_id=deploy_run_id,
            verdict=failing_verdict,
            failed_step=failed_step,
            events=tuple(events),
            deploy_runbook_id=f"deploy-cand-{deploy_run_id}",
            dry_run=self._dry_run,
            detail={"reason": reason, "failing_verdict": failing_verdict},
        )

    def _emit_demotion_event(
        self,
        profile: DeployProfile,
        *,
        feature: str,
        feat_id: str | None,
        failing_verdict: str,
        failing_verdict_ref: str | None,
        deploy_run_id: str,
    ) -> None:
        """[MG-5] Write the DF-021 live-gate demotion event (Stage 3).

        Rides the O-32 verdict-fail branch unconditionally: whenever the deploy
        stage reverts an unverified build, the auto-merged lane must be demoted,
        so this drops a file-based demotion event into the target repo's ``qa/``
        tree for the trust ledger to read. Best-effort and side-only — it never
        alters the revert and never raises past its boundary (a demotion-event
        write failure must not turn a clean revert into a crash). When no target
        repo root was threaded (older callers / unit fixtures) it is a no-op: the
        emission cannot name the qa/ tree, and an un-emitted event is inert.
        """
        if not self._target_repo_root:
            logger.debug(
                "MG-5: no target_repo_root threaded; demotion event not emitted "
                "(run=%s)",
                deploy_run_id,
            )
            return
        lane = self._target_repo or profile.env_id
        try:
            path = write_demotion_event(
                Path(self._target_repo_root) / "qa",
                feature_id=feat_id or feature,
                lane=lane,
                verdict=failing_verdict,
                timestamp=self._clock().isoformat(),
                receipt_ref=failing_verdict_ref,
                run_id=deploy_run_id,
            )
            logger.info(
                "MG-5: wrote live-gate demotion event for lane %r (%s)", lane, path
            )
        except Exception as exc:  # noqa: BLE001 — demotion emission is best-effort
            logger.warning(
                "MG-5: failed to write demotion event for lane %r (run=%s): %s",
                lane,
                deploy_run_id,
                exc,
            )

    async def _run_revert(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        feat_id: str | None,
        task_id: str | None,
        profile_ref: str | None,
        deployer: str,
        failing_verdict: str,
        failing_verdict_ref: str | None,
        deploy_runbook_id: str,
        live_gate_runbook_id: str | None,
        events: list[str],
    ) -> DeployStageResult:
        """[O-32] Roll back a build whose live-gate verdict was not "pass".

        Re-deploys the kept ``:rollback-*`` image through the SAME deploy seam and
        publishes DeployReverted (``outcome="reverted"``). Two loud terminal
        failures guard against a silent keep-serving of the unverified build:
        a profile with no rollback ref, and a revert re-deploy that itself fails
        — both return ``outcome="failed"`` with ``failed_step="revert"`` and a
        DeployFailed naming the cause.
        """
        rollback_ref = profile.rollback_ref
        when = self._clock()

        # No rollback ref → cannot revert. LOUD terminal failure naming the
        # missing ref; never silently keep serving the failed build.
        if not rollback_ref:
            reason = (
                f"live-gate verdict {failing_verdict!r} != 'pass' but the deploy "
                f"profile for {profile.env_id!r} carries NO rollback image ref "
                "(rollback_image_ref); cannot revert — refusing to keep serving "
                "the unverified build"
            )
            logger.error("O-32 revert impossible: %s", reason)
            await self._safe_publish(
                self._deploy_publisher.publish_deploy_failed,
                DeployFailedPayload(
                    correlation_id=correlation_id,
                    env_id=profile.env_id,
                    deploy_run_id=deploy_run_id,
                    failed_step="revert",
                    failure_reason=reason,
                    recoverable=False,
                    feat_id=feat_id,
                    task_id=task_id,
                    deploy_record_ref=None,
                    deploy_profile_ref=profile_ref,
                    runbook_ref=live_gate_runbook_id,
                    hosts=profile.host_names or None,
                    reservation_resource=profile.reservation_resource,
                    failed_at=when,
                ),
            )
            events.append("DeployFailed")
            return DeployStageResult(
                outcome="failed",
                deploy_run_id=deploy_run_id,
                verdict=failing_verdict,
                failed_step="revert",
                events=tuple(events),
                deploy_runbook_id=deploy_runbook_id,
                live_gate_runbook_id=live_gate_runbook_id,
                dry_run=self._dry_run,
                detail={
                    "reason": "missing_rollback_ref",
                    "failing_verdict": failing_verdict,
                },
            )

        # Re-deploy the kept rollback image through the same deploy seam.
        revert_runbook = build_revert_runbook(
            profile,
            runbook_id=f"revert-{deploy_run_id}",
            target=profile.env_id,
            rollback_image_ref=rollback_ref,
            now=self._clock(),
        )
        run_result = await self._run_runbook(revert_runbook, correlation_id)
        executed = self._repo.load_runbook(
            revert_runbook.runbook_id, correlation_id=correlation_id
        )

        # The revert re-deploy itself failed → the loudest failure (the target is
        # now in an unknown serving state). DeployFailed, outcome="failed".
        if run_result.status != "complete":
            reason = (
                f"O-32 revert of {profile.env_id!r} to {rollback_ref!r} FAILED "
                f"(revert runbook status={run_result.status!r}); the target may be "
                "serving an unverified build — manual intervention required"
            )
            logger.error(reason)
            record_ref: str | None = None
            try:
                record_ref = self._write_record(
                    profile,
                    executed=executed,
                    deploy_run_id=deploy_run_id,
                    deployer=deployer,
                    profile_ref=profile_ref,
                    task_id=task_id,
                    status="revert_failed",
                    when=when,
                )
            except Exception as exc:  # noqa: BLE001 — record is best-effort
                logger.info("no F7 record for failed revert: %s", exc)
            await self._safe_publish(
                self._deploy_publisher.publish_deploy_failed,
                DeployFailedPayload(
                    correlation_id=correlation_id,
                    env_id=profile.env_id,
                    deploy_run_id=deploy_run_id,
                    failed_step="revert",
                    failure_reason=reason,
                    recoverable=False,
                    feat_id=feat_id,
                    task_id=task_id,
                    deploy_record_ref=record_ref,
                    deploy_profile_ref=profile_ref,
                    runbook_ref=revert_runbook.runbook_id,
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
                verdict=failing_verdict,
                failed_step="revert",
                events=tuple(events),
                deploy_runbook_id=deploy_runbook_id,
                live_gate_runbook_id=live_gate_runbook_id,
                dry_run=self._dry_run,
                detail={
                    "reason": "revert_failed",
                    "rollback_image_ref": rollback_ref,
                },
            )

        # Revert succeeded → honest F7 record + DeployReverted receipt.
        reverted_at = self._clock()
        record_ref = self._write_record(
            profile,
            executed=executed,
            deploy_run_id=deploy_run_id,
            deployer=deployer,
            profile_ref=profile_ref,
            task_id=task_id,
            status="reverted",
            when=reverted_at,
        )
        await self._safe_publish(
            self._deploy_publisher.publish_deploy_reverted,
            DeployRevertedPayload(
                correlation_id=correlation_id,
                env_id=profile.env_id,
                deploy_run_id=deploy_run_id,
                reverted_to_image_ref=rollback_ref,
                failing_verdict=failing_verdict,
                feat_id=feat_id,
                task_id=task_id,
                failing_verdict_ref=failing_verdict_ref,
                deploy_record_ref=record_ref,
                deploy_profile_ref=profile_ref,
                revert_runbook_ref=revert_runbook.runbook_id,
                hosts=profile.host_names or None,
                reservation_resource=profile.reservation_resource,
                reverted_at=reverted_at,
            ),
        )
        events.append("DeployReverted")
        return DeployStageResult(
            outcome="reverted",
            deploy_run_id=deploy_run_id,
            deploy_record_ref=record_ref,
            verdict=failing_verdict,
            events=tuple(events),
            deploy_runbook_id=deploy_runbook_id,
            live_gate_runbook_id=live_gate_runbook_id,
            dry_run=self._dry_run,
            detail={"reverted_to": rollback_ref},
        )

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
