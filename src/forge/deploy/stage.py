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

...UNLESS THE DEPLOY ITSELF RUNS INSIDE THAT SANDBOX (2026-09-09). Those
settings, and the six beside them that make a sandbox carry the factory's own
services, all say how to CREATE the sandbox, and the only program that reads
them is the host wrapper that creates it. When the factory lives inside the
sandbox this stage runs the repository's own deploy script directly (see
:meth:`DeployStageRunner._profile_for_run`), so there is nothing to create and
nobody to read them: every step this stage builds for that venue is told so,
and carries none of them. What the deploy actually reads is untouched — the
candidate's own environment, the live gate's, and the mode signals. The first
real merge press found this: the deploy sidecar inside the sandbox refused
``SANDBOX_SIDECAR_PUBLISH`` as a key it does not allow, the candidate leg
ended in 0.16 seconds without a container ever starting, and the reason was
written down only in a ledger row.

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

THE CANDIDATE'S GATE RUNS IN THE CANDIDATE'S TREE (coach finding on the lane,
2026-09-07). The compose and health steps already run in the laid-out tree;
the live gate did not — the driver ran in the checkout, at main, and checked
the branch's build against main's gate registry and main's Hurl twins, when
the per-feature gate is registered ON the branch and only reaches main with
the merge. Now the candidate leg hands the same working directory to
:meth:`DeployStageRunner._run_live_gate`, which moves the invoker into the
tree (``with_repo_path``) before it overlays the candidate's addresses
(``with_extra_env``). The promote leg and a plain deploy pass no directory,
so their gate runs in the checkout exactly as before.

Where the candidate gate's evidence goes: the driver writes it under the
tree's ``qa/gates/evidence/`` and ``qa/gates/history/``, and it is removed
with the tree when the run ends. It stays there on purpose — the merge that
follows a green check refuses a dirty checkout, and the tree is the one
place under the checkout that is excluded from that check. What a person or
a repair needs leaves the tree before the teardown: the verdict, the number
of checks, the number passed and the failing checks' names ride the gate
step's own result in the runbook record, the candidate summary this leg
returns, the executor's receipt and the merge report — all read from the
candidate run, none from the tree.
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
from forge.deploy.live_gate import (
    BrokerInspector,
    LiveGateInvoker,
    RefusingLiveGateInvoker,
)
from forge.deploy.profile import DeployProfile, wrapper_inner_script
from forge.deploy.reservation import (
    ReservationError,
    ReservationHandle,
    ReservationLease,
)
from forge.deploy.runbook_builder import (
    build_candidate_teardown_runbook,
    build_deploy_runbook,
    build_live_gate_runbook,
    build_read_only_runbook,
    build_revert_runbook,
)
from forge.deploy.sidecar_runner import SidecarScriptRunner
from forge.deploy.steps import SecretPresenceResolver, register_deploy_handlers
from forge.executor.executor import RunbookExecutor, RunResult
from forge.executor.registry import StepTypeRegistry
from forge.executor.shell_steps import ScriptRunner
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import Runbook, Step

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ASSERTION_VALUE_CHARS_IN_THE_LOG",
    "MAX_FAILED_ASSERTIONS_IN_THE_LOG",
    "MAX_FAILED_ASSERTIONS_ON_THE_RECEIPT",
    "DeployStageRunner",
    "DeployStageResult",
    "assertion_in_words",
    "failed_assertions",
    "gate_summary",
    "refusal_assertion_clause",
    "sidecar_refusal",
]


def deploy_step_output(executed: Any) -> str:
    """What the project's own deploy step printed, out of an executed runbook.

    The caller needs it for one reason: the identity the step reported is in
    there, and it has to be compared with the identity the step was handed.
    Everything else on the runbook is already recorded. An empty answer is an
    honest "the step said nothing this side can read", which the comparison
    treats as a mismatch rather than a pass.
    """
    if executed is None:
        return ""
    said: list[str] = []
    for step in getattr(executed, "steps", ()) or ():
        if getattr(step, "step_type", "") != "deploy_compose":
            continue
        result = getattr(step, "result", None)
        payload = getattr(result, "payload", None)
        if isinstance(payload, dict):
            captured = payload.get("captured_output")
            if isinstance(captured, str) and captured:
                said.append(captured)
    return "\n".join(said)


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: What a teardown that was told nothing is answered with. One build's cleanup
#: must never touch another build's candidate, and the only thing that tells
#: them apart is the identity the CHECK was handed — so a teardown without one
#: is refused here rather than left to the project's step to guess at.
A_TEARDOWN_WITH_NO_NAME: str = (
    "this teardown was handed no identity, so there is no one candidate it "
    "could name; nothing was taken down. A teardown that names nothing has to "
    "go looking for candidates, and what it finds can belong to another "
    "build's check. Hand it the same identity the check was handed, or remove "
    "the candidate by hand with the project's own teardown step."
)


def _names_one_candidate(identity_env: "dict[str, str] | None") -> bool:
    """Does this overlay actually name a candidate? Both halves must be there.

    A setting with an empty value names nothing, and an empty overlay names
    nothing; either way there is no single candidate to take down.
    """
    for name, value in dict(identity_env or {}).items():
        if str(name).strip() and str(value).strip():
            return True
    return False


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


#: How the deploy sidecar's own answers begin. When the sidecar refuses a
#: request nothing is run, so what comes back is not a script's output at all:
#: it is the sidecar client's own sentence (:mod:`forge.deploy.sidecar_runner`)
#: — "sidecar refused (HTTP 400): env key ... is not allowlisted", "sidecar
#: unreachable at ...", "the deploy sidecar did not run in the candidate tree
#: ...". Those sentences are the only thing that says WHY a step never
#: started, so they are carried into the line a person reads.
SIDECAR_ANSWER_OPENINGS: tuple[str, ...] = ("sidecar ", "the deploy sidecar ")

#: How much of the sidecar's sentence travels into a failure line. Long enough
#: for the whole of a refusal and the list of keys it allows; short enough that
#: one failure stays one readable line.
SIDECAR_SENTENCE_CAP: int = 400


def sidecar_refusal(step: Step | None) -> str | None:
    """The sidecar's own sentence for a step it never ran, or ``None``.

    Reads the step's recorded result the way the two script steps write it: a
    ``deploy_compose`` step keeps one ``captured_output``, a ``health_check``
    step keeps one entry per check and the last one is the check that failed.
    Anything that is not one of the sidecar's own sentences — a script's real
    output, an empty result, a step that never ran — is ``None``, so an
    ordinary failure reads exactly as it always did.
    """
    if step is None or step.result is None:
        return None
    payload = step.result.payload
    if not isinstance(payload, dict):
        return None
    output = payload.get("captured_output")
    if output is None:
        ran = payload.get("ran")
        if isinstance(ran, list) and ran and isinstance(ran[-1], dict):
            output = ran[-1].get("captured_output")
    if not isinstance(output, str) or not output.strip():
        return None
    first_line = output.strip().splitlines()[0].strip()
    if not any(
        first_line.lower().startswith(opening)
        for opening in SIDECAR_ANSWER_OPENINGS
    ):
        return None
    if len(first_line) > SIDECAR_SENTENCE_CAP:
        return first_line[: SIDECAR_SENTENCE_CAP - 1].rstrip() + "…"
    return first_line


@dataclass(frozen=True, slots=True)
class _LiveGateRun:
    """What one run of the live gate said, as the stage reads it back.

    ``verdict`` is None when the gate produced no verdict at all (an
    instrument problem). ``gate_ids`` names every check that ran;
    ``assertions`` carries each check's own results when the invoker reports
    them, so the stage can say which checks failed by name.
    ``evidence_index_ref`` is the evidence index the driver reported, as the
    driver wrote it (relative to the directory the gate ran in); empty when
    it reported none.
    """

    verdict: str | None
    runbook_id: str
    failing_verdict_ref: str | None
    gate_ids: tuple[str, ...] = ()
    assertions: tuple[dict[str, Any], ...] = ()
    evidence_index_ref: str = ""


#: HOW MANY FAILED CHECKS RIDE ON EACH SURFACE (2026-09-12). A refusal that
#: costs a merge has to say what the gate saw, and each surface can hold a
#: different amount of it. The merge report on disk is the long one: it keeps
#: every failing assertion up to this many, which is far more than any real
#: gate produces.
MAX_FAILED_ASSERTIONS_ON_THE_RECEIPT: int = 50
#: The operator's log line names them all, up to this many — enough for every
#: check a repository runs, bounded so one broken gate cannot fill the log.
MAX_FAILED_ASSERTIONS_IN_THE_LOG: int = 20
#: One reported value (what was expected, what was seen) is trimmed to this
#: many characters on the log line. The receipt keeps the value in full.
MAX_ASSERTION_VALUE_CHARS_IN_THE_LOG: int = 200


def _trimmed_value(value: Any, *, cap: int) -> str:
    """One reported value as one readable line, trimmed to ``cap``."""
    text = " ".join(str(value).split())
    if len(text) > cap:
        return text[: cap - 1].rstrip() + "…"
    return text


def failed_assertions(
    assertions: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every assertion the gate reported as not passing, in the gate's words.

    The entries are copied through as the gate sent them — nothing is added
    and nothing is renamed — so a person reading them is reading the gate.
    An entry with no status at all counts as failed: a check that says
    nothing about itself is never treated as green.
    """
    failed: list[dict[str, Any]] = []
    for entry in assertions:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "").strip().lower() == "pass":
            continue
        failed.append(dict(entry))
    return failed


def assertion_in_words(
    entry: dict[str, Any], *, value_cap: int = MAX_ASSERTION_VALUE_CHARS_IN_THE_LOG
) -> str:
    """One failed assertion as a plain sentence fragment.

    Names the check and the assertion inside it, then what the gate said it
    expected and what it said it saw. Only fields the gate actually sent are
    used: when it sent neither an expected nor an observed value, the words
    say exactly that, because a gate that reports a failure without saying
    what it saw is itself something a person needs to know.
    """
    gate_id = str(entry.get("gate_id") or "").strip()
    assertion_id = str(entry.get("id") or "").strip()
    if gate_id and assertion_id and assertion_id != gate_id:
        named = f"{gate_id} ({assertion_id})"
    else:
        named = gate_id or assertion_id or "an unnamed check"
    expected = entry.get("expected")
    observed = entry.get("observed")
    if expected is not None and observed is not None:
        saw = (
            f"expected {_trimmed_value(expected, cap=value_cap)}, "
            f"saw {_trimmed_value(observed, cap=value_cap)}"
        )
    elif expected is not None:
        saw = (
            f"expected {_trimmed_value(expected, cap=value_cap)}; the gate did "
            "not say what it saw"
        )
    elif observed is not None:
        saw = (
            f"saw {_trimmed_value(observed, cap=value_cap)}; the gate did not "
            "say what it expected"
        )
    else:
        saw = "the gate did not say what it expected or what it saw"
    return f"{named}: {saw}"


def refusal_assertion_clause(
    summary: dict[str, Any] | None,
    *,
    cap: int = MAX_FAILED_ASSERTIONS_IN_THE_LOG,
    value_cap: int = MAX_ASSERTION_VALUE_CHARS_IN_THE_LOG,
) -> str:
    """What the gate saw, for the operator's line — or that it said nothing.

    Empty when the summary carries no assertion block at all (the gate never
    ran, or it passed), so every other refusal reads exactly as it did.
    """
    if not summary or "failed_assertions" not in summary:
        return ""
    entries = list(summary.get("failed_assertions") or [])
    if not entries:
        return (
            "the gate reported no assertion detail, so it did not say which "
            "assertion failed or what it saw"
        )
    shown = entries[:cap]
    left_out = len(entries) - len(shown)
    left_out += int(summary.get("failed_assertions_left_out") or 0)
    words = "; ".join(assertion_in_words(e, value_cap=value_cap) for e in shown)
    if left_out:
        words += (
            f"; and {left_out} more failing "
            f"{'assertion' if left_out == 1 else 'assertions'} not named here "
            "(the merge report has them all)"
        )
    return f"what the gate saw: {words}"


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

    A GATE THAT RAN AND DID NOT PASS also carries what it saw (2026-09-12):
    ``failed_assertions`` is every failing assertion the gate reported, in
    the gate's own words and up to
    :data:`MAX_FAILED_ASSERTIONS_ON_THE_RECEIPT` of them, with
    ``failed_assertions_left_out`` saying how many did not fit and
    ``assertion_detail_reported`` saying whether the gate reported any at
    all. Those three keys are added ONLY for a gate that ran and did not
    pass, so a passing check's summary — and every receipt built from it —
    is exactly what it was.
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
    summary: dict[str, Any] = {
        "verdict": verdict,
        "checks_total": total,
        "checks_passed": passed,
        "failed_checks": failed,
        "gate_ids": names,
        "live_gate_runbook_id": live_gate_runbook_id,
    }
    if verdict is not None and verdict != "pass":
        reported = failed_assertions(assertions)
        kept = reported[:MAX_FAILED_ASSERTIONS_ON_THE_RECEIPT]
        summary["failed_assertions"] = kept
        summary["failed_assertions_left_out"] = len(reported) - len(kept)
        summary["assertion_detail_reported"] = bool(reported)
    return summary


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
        sandbox: Any | None = None,
        build_id: str | None = None,
        start_commit: str | None = None,
        by_hand: bool = False,
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
        # SANDBOX FIRST (2026-09-07, rule 85). The repository's sandbox entry
        # from ``planning.sandboxes``, or None for a repository that has none —
        # which is every repository until an operator fills that mapping in,
        # and which keeps this stage byte for byte what it was. When it is set,
        # two things change and nothing else: the scripts go to the sidecar
        # INSIDE that sandbox rather than the host one, and the script they run
        # is the repository's own deploy script rather than the host wrapper
        # that would put it in a sandbox (see :meth:`_profile_for_run`).
        self._sandbox = sandbox
        # [MG-5] The target repo's filesystem root — the demotion-event emission
        # writes under ``<root>/qa/`` (beside the live-gate gates), where the
        # DF-021 trust ledger reads it. None (older callers/tests) → the emission
        # is a no-op, since it cannot name the qa/ tree.
        self._target_repo_root = target_repo_root
        # THE STAMP THIS STAGE'S REQUESTS CARRY (23 September 2026). The build
        # and the commit the coordinator's ledger records it as starting from,
        # read off that ledger by whoever composed this stage and bound here
        # once. Every request the script runner below sends carries the pair,
        # so the far side never has to take a commit on a request's own word.
        self._build_id = str(build_id or "").strip() or None
        self._start_commit = str(start_commit or "").strip() or None
        # AND AN ATTENDED RUN SAYS IT IS ONE (23 September 2026). With no
        # build to stamp, the far side refuses a request that asks the project
        # to widen its environment door unless the request claims the run is
        # by hand. Only a caller that knows a person asked for this sets it;
        # it is never derived from "there is no build here", because that
        # would turn a dropped stamp into a claim.
        self._by_hand = bool(by_hand)

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
        base_url = self._config.sidecar_url
        if self._sandbox is not None:
            # The sidecar that runs this repository's scripts is the one inside
            # its own sandbox, where the clone, the toolchain and the Docker
            # engine are (rule 85). The global address stays for every
            # repository that has no sandbox.
            base_url = str(getattr(self._sandbox, "sidecar_url", "") or base_url)
        return SidecarScriptRunner(
            base_url=base_url,
            repo=self._target_repo,
            build=self._build_id,
            start_commit=self._start_commit,
            by_hand=self._by_hand,
        )

    def _runs_inside_the_sandbox(self) -> bool:
        """Does this stage's work happen inside the repository's own sandbox?

        True when the repository has a sandbox entry, which is what puts the
        factory's services — and so this stage's scripts — in there (rule 85).
        The steps built for that venue are not sent the settings that say how
        to make the sandbox: it is already made, and the wrapper that would
        read them is not what runs.

        The question is about the repository alone, not about which script the
        profile names. Every script this stage runs for such a repository runs
        inside the sandbox; _profile_for_run below does the separate, narrower
        job of swapping a host wrapper for the inner script it would have run
        in there, and leaves any other script alone.
        """
        return self._sandbox is not None

    def _profile_for_run(self, profile: DeployProfile) -> DeployProfile:
        """The profile this stage actually runs — the inner script in a sandbox.

        A repository that deploys into a Docker Sandbox names a HOST wrapper as
        its ``compose.script`` (``deploy/sandbox-deploy.sh``): the wrapper puts
        the sandbox in place with ``sbx`` and then runs the repository's own
        ``deploy/deploy.sh`` inside it. When the factory itself lives in that
        sandbox (rule 85) the deploy stage's scripts already run in there, so
        the wrapper would be asking ``sbx`` to make a sandbox from inside one —
        which cannot work and must never be tried. So for a repository with a
        sandbox this stage runs the wrapper's own inner script instead, and the
        wrapper goes back to being what rule 86 says it is: an attended,
        host-side command an operator runs to create the sandbox.

        The inner script is the wrapper's name without its ``sandbox-`` prefix,
        in the same directory — the shared template's own pairing
        (``deploy/sandbox-deploy.sh`` runs ``deploy/deploy.sh``). A profile
        whose script is not a wrapper by that name is left exactly as it is,
        and so is every repository without a sandbox.
        """
        if self._sandbox is None:
            return profile
        script = profile.compose.script or ""
        inner_path = wrapper_inner_script(script)
        if not inner_path:
            return profile
        logger.info(
            "deploy stage: %s runs inside its own sandbox, so the deploy step "
            "runs %s directly instead of the host wrapper %s",
            self._target_repo or profile.env_id,
            inner_path,
            script,
        )
        return replace(
            profile, compose=replace(profile.compose, script=inner_path)
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
        identity_env: dict[str, str] | None = None,
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
        profile = self._profile_for_run(profile)
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
                identity_env=identity_env,
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
            identity_env=identity_env,
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
        identity_env: dict[str, str] | None = None,
        memory_project: str | None = None,
        launch_settings: tuple[str, ...] = (),
    ) -> DeployStageResult:
        """Leg one: the candidate up, healthy, and through the live gate.

        ``candidate_cwd`` is the working directory the candidate's steps run
        in — the feature branch's laid-out tree (protect-main, rule 38) — so
        the repository's own deploy script, found relative to it, builds that
        tree. ``None`` runs from the profile's ``cwd`` as before.

        ``identity_env`` (23 September 2026) is what the CHECK is handed so it
        can pin what it checked: names the project declared, values the caller
        made. It rides the candidate step's own env overlay. The step's output
        comes back in ``detail["gate_summary"]["candidate_output"]``, because
        the caller has to read out of it the artifact the check says it checked
        — and record THAT, rather than letting the deploy resolve a name of its
        own later, which is the whole of the hole this closes. ``None`` ⇒ every
        caller written before it is byte for byte what it was.

        Returns ``outcome="complete"`` with ``verdict="pass"`` and the
        candidate LEFT STANDING (the promote deploys the artifact it reported),
        and ``detail["gate_summary"]`` saying how many checks ran and passed.
        Returns ``outcome="failed"`` when the candidate could not start or
        its gate did not pass — the candidate is then torn down and the live
        name was never touched — or when the profile has no candidate
        section at all (``detail["reason"] == "no_candidate_section"``).
        """
        events: list[str] = []
        profile = self._profile_for_run(profile)
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
                identity_env=identity_env,
                memory_project=memory_project,
                launch_settings=tuple(launch_settings),
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
        identity_env: dict[str, str] | None = None,
    ) -> DeployStageResult:
        """Tear the standing candidate down on its own — for a run that stops
        between the two legs. Never raises; ``outcome="failed"`` with
        ``failed_step="candidate_down"`` when the teardown did not complete.

        ``identity_env`` is the same setting the CHECK was handed, so a project
        whose candidate belongs to one check rather than to a shared name can
        take down the right one. It is REQUIRED: with none, this leg refuses
        and removes nothing (:data:`A_TEARDOWN_WITH_NO_NAME`)."""
        profile = self._profile_for_run(profile)
        if profile.candidate is None:
            return DeployStageResult(
                outcome="complete",
                deploy_run_id=deploy_run_id,
                dry_run=self._dry_run,
                detail={"reason": "no_candidate_section", "candidate": "absent"},
            )
        if not _names_one_candidate(identity_env):
            logger.warning(
                "candidate teardown for %s was refused: %s",
                profile.env_id,
                A_TEARDOWN_WITH_NO_NAME,
            )
            return DeployStageResult(
                outcome="failed",
                deploy_run_id=deploy_run_id,
                failed_step="candidate_down",
                dry_run=self._dry_run,
                detail={
                    "candidate": "standing",
                    "refusal": A_TEARDOWN_WITH_NO_NAME,
                },
            )
        torn_down = await self._teardown_candidate(
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id,
            identity_env=identity_env,
        )
        return DeployStageResult(
            outcome="complete" if torn_down else "failed",
            deploy_run_id=deploy_run_id,
            failed_step=None if torn_down else "candidate_down",
            dry_run=self._dry_run,
            detail={"candidate": "torn-down" if torn_down else "standing"},
        )

    async def what_is_running(
        self,
        profile: DeployProfile,
        *,
        correlation_id: str,
        deploy_run_id: str,
        ask_env: dict[str, str],
        memory_project: str | None = None,
        launch_settings: tuple[str, ...] = (),
    ) -> DeployStageResult:
        """ASK THE TARGET what it is running. Read-only; nothing is changed.

        Added 23 September 2026, after the second review of the executor stage.
        The only-forwards rule (the design's B) was being applied to what the
        LEDGER said was running, and a run that deploys and then stops before
        its ledger line leaves the ledger wrong. A later pick-up then read a
        stale row, decided it was moving forwards, and put an OLDER result over
        a newer one while answering "merged into the remote and running". That
        was driven through the real merge entry point.

        So before it decides anything, the press asks the project itself. The
        project declares how it wants to be asked (a setting) and what its
        answer looks like (a marker); ``ask_env`` carries the first, and the
        answer comes back in ``detail["deploy_output"]`` for the caller to read
        the second out of.

        This is deliberately NOT the full deploy leg: one step, no queue event,
        no deploy record, no live gate, no rollback. It is a question. A
        project's step that changes anything when asked it has broken its own
        contract, and the press cannot tell — which is said plainly rather than
        guarded against, because guarding would mean knowing what the project
        does.
        """
        profile = self._profile_for_run(profile)
        runbook = build_read_only_runbook(
            profile,
            runbook_id=f"ask-{deploy_run_id}",
            target=profile.env_id,
            extra_env=dict(ask_env),
            now=self._clock(),
            inside_sandbox=self._runs_inside_the_sandbox(),
            memory_project=memory_project,
            launch_settings=launch_settings,
        )
        try:
            run_result = await self._run_runbook(runbook, correlation_id)
        except Exception as exc:  # noqa: BLE001 — a question, never a crash
            logger.warning(
                "what-is-running: %s could not be asked what it is running "
                "(%s: %s)",
                profile.env_id,
                type(exc).__name__,
                exc,
            )
            return DeployStageResult(
                outcome="failed",
                deploy_run_id=deploy_run_id,
                failed_step="deploy_compose",
                dry_run=self._dry_run,
                detail={"deploy_output": "", "why": f"{type(exc).__name__}: {exc}"},
            )
        # WHAT THE STEP SAID IS WANTED EITHER WAY (23 September 2026, the third
        # review). A step that could not find out exits non-zero AND says why
        # on its last line, and that sentence is the whole use of asking. It
        # used to be thrown away with the run: the load was inside the same
        # guard, so a failed ask answered with an empty output and the caller
        # could only report that the step "did not finish".
        said = ""
        try:
            executed = self._repo.load_runbook(
                runbook.runbook_id, correlation_id=correlation_id
            )
            said = deploy_step_output(executed)
        except Exception as exc:  # noqa: BLE001 — a question, never a crash
            logger.warning(
                "what-is-running: %s answered and the answer could not be read "
                "back (%s: %s)",
                profile.env_id,
                type(exc).__name__,
                exc,
            )
        return DeployStageResult(
            outcome="complete" if run_result.status == "complete" else "failed",
            deploy_run_id=deploy_run_id,
            failed_step=None if run_result.status == "complete" else "deploy_compose",
            dry_run=self._dry_run,
            detail={"deploy_output": said},
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
        deploy_ownership: dict[str, Any] | None = None,
        memory_project: str | None = None,
        launch_settings: tuple[str, ...] = (),
        identity_env: dict[str, str] | None = None,
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

        ``deploy_ownership`` (23 September 2026) is who owns the deployment
        target this leg changes: the target, that target's own counter, the
        build the counter was granted to, and the IDENTITY this step must
        deploy under the setting name the PROJECT declared. It rides the
        deploy step's own params, and its presence is what sends the step
        through the executor rather than straight to a runner. Absent ⇒ every
        existing caller is byte for byte what it was.

        The result's ``detail`` also carries ``deploy_output`` — what the
        project's own deploy step printed — because the caller has to read the
        identity the step reported back out of it and compare it, as text,
        with the identity it handed over.
        """
        profile = self._profile_for_run(profile)
        events: list[str] = list(prior_events)
        profile_ref = deploy_profile_ref or profile.source_ref
        deployer = deployer or deploy_run_id
        # THE SAME SETTING THE CHECK WAS HANDED, for the teardown that follows
        # (23 September 2026). A project whose candidate belongs to one check
        # rather than to a shared name cannot be told which one to take down
        # without it. Both names are the project's own and carried as text.
        # The press under the lock carries it in the ownership block; a caller
        # that is not the press hands it directly. Either way the teardown
        # NAMES ONE CANDIDATE or does not run at all.
        identity_env_for_teardown: dict[str, str] = dict(identity_env or {})
        if deploy_ownership:
            setting = str(deploy_ownership.get("identity_setting") or "").strip()
            identity_text = str(deploy_ownership.get("identity") or "").strip()
            if setting and identity_text:
                identity_env_for_teardown = {setting: identity_text}
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
                        identity_env=identity_env_for_teardown,
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
                inside_sandbox=self._runs_inside_the_sandbox(),
                deploy_ownership=deploy_ownership,
                memory_project=memory_project,
                launch_settings=launch_settings,
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
                        identity_env=identity_env_for_teardown,
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
                detail={
                    "candidate": candidate_word,
                    # WHAT THE PROJECT'S OWN DEPLOY STEP SAID. The caller reads
                    # the identity of what is now running out of this and
                    # compares it, as text, with the identity it handed over.
                    # A step that reported nothing is a mismatch, not a pass.
                    "deploy_output": deploy_step_output(executed),
                },
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

    def _stopped_at(
        self, executed: Runbook | None, run_result: RunResult, *, default: str
    ) -> tuple[str, str | None]:
        """Which step stopped the runbook, and why if the sidecar said why.

        Returns the step's type (``default`` when the run stopped before any
        step, or the runbook could not be read back) and, when the step never
        ran because the deploy sidecar refused it or could not be reached, the
        sidecar's own sentence (:func:`sidecar_refusal`) — ``None`` for every
        ordinary failure, which therefore reads exactly as it always did.
        """
        if executed is None or run_result.stopped_at_index is None:
            return default, None
        idx = run_result.stopped_at_index
        if not (0 <= idx < len(executed.steps)):
            return default, None
        step = executed.steps[idx]
        return step.step_type, sidecar_refusal(step)

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
        driver_cwd_override: str | None = None,
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

        ``driver_cwd_override`` (protect-main, rule 38): the directory the
        gate's driver runs in — the candidate leg passes the feature branch's
        laid-out tree, so the driver reads that tree's gate registry and Hurl
        twins and writes its evidence there. The invoker is moved into it
        (``with_repo_path``) BEFORE the env overlay is applied, since the
        overlay keeps whatever directory its invoker has. An invoker that
        cannot be moved does not run the gate at all: the runbook step fails
        with the reason on record, because a gate that ran in the checkout
        instead would check main's registry against the branch's build — the
        defect the candidate check exists to catch. ``None`` (the promote leg,
        a plain deploy) leaves the invoker where it was composed: the checkout.
        """
        gate_runbook = build_live_gate_runbook(
            profile,
            runbook_id=runbook_id or f"live-gate-{deploy_run_id}",
            target=profile.env_id,
            feature=feature,
            now=self._clock(),
        )
        invoker_override: LiveGateInvoker | None = None
        if driver_cwd_override is not None:
            with_cwd = getattr(self._live_gate_invoker, "with_repo_path", None)
            if callable(with_cwd):
                invoker_override = with_cwd(driver_cwd_override)
            else:
                logger.error(
                    "live-gate invoker %s cannot be moved into the candidate tree "
                    "%s (no with_repo_path); the candidate gate is refused rather "
                    "than run in the checkout",
                    type(self._live_gate_invoker).__name__,
                    driver_cwd_override,
                )
                invoker_override = RefusingLiveGateInvoker(
                    f"the live-gate invoker "
                    f"({type(self._live_gate_invoker).__name__}) cannot run in "
                    f"the candidate tree {driver_cwd_override}, and a gate run "
                    "in the checkout would check main's registry, not the "
                    "branch's"
                )
        if driver_env_overlay and not isinstance(
            invoker_override, RefusingLiveGateInvoker
        ):
            base = invoker_override or self._live_gate_invoker
            with_overlay = getattr(base, "with_extra_env", None)
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
            evidence_index_ref=str(common["evidence_index_ref"]),
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
        identity_env: dict[str, str] | None = None,
        memory_project: str | None = None,
        launch_settings: tuple[str, ...] = (),
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
        the profile's ``cwd``. The live gate runs there too, so it checks the
        tree's own registry and twins, and its evidence is written under the
        tree (``summary["evidence_index_ref"]`` names the index as the driver
        reported it, relative to that tree). The evidence goes when the tree
        goes; the verdict, the counts and the failing names are in this
        summary and in the gate step's runbook record before that.
        """
        assert profile.candidate is not None  # caller-guarded
        cand_env = dict(profile.candidate.env)
        # WHAT THE CHECK IS HANDED SO IT CAN PIN WHAT IT CHECKED. The names are
        # the project's own; the values the caller's. It goes on last so a
        # project cannot lose it to its own candidate overlay.
        compose_extra = {"CANDIDATE": "1", **cand_env, **(identity_env or {})}
        summary: dict[str, Any] = gate_summary(verdict=None, gate_ids=(), assertions=())
        summary["candidate_cwd"] = candidate_cwd
        summary["evidence_index_ref"] = None
        summary["candidate_output"] = ""

        # --- candidate deploy (separate -cand project) ---
        cand_runbook = build_deploy_runbook(
            profile,
            runbook_id=f"deploy-cand-{deploy_run_id}",
            target=profile.env_id,
            now=self._clock(),
            compose_extra_env=compose_extra,
            check_extra_env=cand_env,
            cwd_override=candidate_cwd,
            inside_sandbox=self._runs_inside_the_sandbox(),
            memory_project=memory_project,
            launch_settings=launch_settings,
        )
        run_result = await self._run_runbook(cand_runbook, correlation_id)
        executed = self._repo.load_runbook(
            cand_runbook.runbook_id, correlation_id=correlation_id
        )
        # WHAT THE CHECK SAID, carried out whole. The caller reads the artifact
        # the check reports out of this and records it; the deploy is then
        # handed that artifact rather than resolving a name of its own.
        summary["candidate_output"] = deploy_step_output(executed)
        if run_result.status != "complete":
            failed_step, refusal = self._stopped_at(
                executed, run_result, default="candidate_deploy"
            )
            await self._teardown_candidate(
                profile,
                correlation_id=correlation_id,
                deploy_run_id=deploy_run_id,
                identity_env=identity_env,
            )
            # This is the words the merge report says the candidate stopped
            # at, so when the step never ran the sidecar's own reason travels
            # with it. Before this, the reason existed only in a ledger row
            # and the report said "stopped at deploy_compose" and no more.
            summary["failed_step"] = (
                f"{failed_step} — {refusal}" if refusal else failed_step
            )
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
                failure_detail=refusal,
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
                driver_cwd_override=candidate_cwd,
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
                "evidence_index_ref": gate.evidence_index_ref or None,
            }
            if verdict != "pass":
                await self._teardown_candidate(
                    profile,
                    correlation_id=correlation_id,
                    deploy_run_id=deploy_run_id,
                    identity_env=identity_env,
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
                    gate_summary_for_words=summary,
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
        identity_env: dict[str, str] | None = None,
    ) -> bool:
        """Tear the candidate down (best-effort, never raises).

        True when the teardown runbook completed; False when it did not, or
        could not be run at all — the candidate may then still be up.

        ``identity_env`` (23 September 2026) is the same setting the CHECK was
        handed, carrying the same identity. A project whose candidate belongs
        to one check rather than to a shared name needs it to know which one to
        take down, and both names are the project's own, carried here as text.

        IT IS REQUIRED, and a teardown handed none is NOT RUN (the fifth
        review, same day). Without a name the only thing a project's teardown
        step can do is find candidates by looking, and what it finds can
        belong to another build's check — which was driven: one build's ending
        removed three builds' candidates and their data. So with no identity
        the candidate is left standing and a person removes it by hand.
        """
        assert profile.candidate is not None
        if not _names_one_candidate(identity_env):
            logger.warning(
                "candidate teardown for %s was NOT run: it was handed no "
                "identity, so there is no one candidate it could name. A "
                "teardown that names nothing has to go looking, and what it "
                "finds can belong to another build's check — so the candidate "
                "is left standing for a person to remove by hand",
                profile.env_id,
            )
            return False
        teardown_env = {
            "CANDIDATE_DOWN": "1",
            **dict(profile.candidate.env),
            **(identity_env or {}),
        }
        teardown_runbook = build_candidate_teardown_runbook(
            profile,
            runbook_id=f"teardown-cand-{deploy_run_id}",
            target=profile.env_id,
            extra_env=teardown_env,
            now=self._clock(),
            inside_sandbox=self._runs_inside_the_sandbox(),
        )
        try:
            run_result = await self._run_runbook(teardown_runbook, correlation_id)
            if run_result.status != "complete":
                # Say why, not just that. When the sidecar refused the request
                # the teardown never ran at all, and that sentence is the only
                # thing that explains the warning below.
                try:
                    executed = self._repo.load_runbook(
                        teardown_runbook.runbook_id, correlation_id=correlation_id
                    )
                except Exception:  # noqa: BLE001 — the warning matters more
                    executed = None
                _, refusal = self._stopped_at(
                    executed, run_result, default="deploy_compose"
                )
                logger.warning(
                    "candidate teardown for %s did not complete (status=%s)%s; "
                    "the -cand project may still be up (manual cleanup)",
                    profile.env_id,
                    run_result.status,
                    f" — {refusal}" if refusal else "",
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
        failure_detail: str | None = None,
        gate_summary_for_words: dict[str, Any] | None = None,
    ) -> DeployStageResult:
        """Publish DeployFailed for a candidate that failed its leg (LIVE intact).

        A loud, honest failure that names the candidate as the cause and records
        that the live name was untouched — recoverable (retry the deploy), never
        a revert (there is nothing live to roll back to).

        ``failure_detail`` is the sidecar's own sentence when the step never
        ran because the sidecar refused it; it goes into the failure line so a
        person reads why, not just where.

        ``gate_summary_for_words`` is the check's own summary when a gate
        actually ran: the failure line then names every failing assertion and
        what the gate said it expected and saw (up to
        :data:`MAX_FAILED_ASSERTIONS_IN_THE_LOG`), or says plainly that the
        gate reported none. Before this, a refusal that cost a merge named
        the check and nothing else, and the candidate — with its evidence —
        was already gone by the time anyone read it.
        """
        when = self._clock()
        detail_verdict = (
            f" (candidate live-gate verdict {failing_verdict!r} != 'pass')"
            if failing_verdict is not None
            else ""
        )
        detail_clause = f" — {failure_detail}" if failure_detail else ""
        saw = refusal_assertion_clause(gate_summary_for_words)
        saw_clause = f" — {saw}" if saw else ""
        failure_reason = (
            f"candidate leg failed at {failed_step!r}{detail_verdict}"
            f"{detail_clause}{saw_clause}; the "
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
            inside_sandbox=self._runs_inside_the_sandbox(),
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
        failed_step, refusal = self._stopped_at(
            executed, run_result, default="unknown"
        )

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

        if refusal:
            # The step never ran: say so where a person is looking, not only
            # in the ledger row the sidecar's answer is stored in.
            logger.error(
                "deploy step %s did not run — %s", failed_step, refusal
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
                failure_reason=(
                    f"deploy runbook escalated: {run_result.reason}"
                    + (f" — {refusal}" if refusal else "")
                ),
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
            # WHAT THE STEP SAID, on the failure path too (23 September 2026).
            # The complete path has always carried it. A caller that has to
            # tell a deploy step which went red from one the executor refused
            # or STOPPED PART-WAY needs the same line — the executor's own
            # word rides in it — and withholding it leaves that caller
            # guessing from an exit code.
            detail={"deploy_output": deploy_step_output(executed)},
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
