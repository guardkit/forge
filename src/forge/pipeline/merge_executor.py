"""The merge-and-deploy executor — code, never an AI session, runs the press.

Make-merge-work build spec (2026-08-24). Two halves:

* :class:`MergeApprovalConsumer` — a CORE NATS subscription (never JetStream;
  the AGENTS stream is no_ack) on ``agents.approval.forge.merge-*.response``.
  It refuses everything except: a durable pending offer whose ``request_id``
  matches, a ``decided_by`` that string-equals the deployment's expected
  approver VERBATIM, a matching correlation, and no decision yet on record
  (the durable decision row is written FIRST, so a restart can never
  double-run).
* :func:`execute_merge_deploy` — the executor coroutine. STEP merge+verify
  through the frozen guardkit subprocess boundary; STEP deploy mirroring the
  attended ``forge deploy`` dispatch in-daemon (execution_surface forced to
  ``sidecar`` — the daemon container has no docker); STEP report as one
  additive ``pipeline.stage-complete.{feature_id}`` publish. Per-step durable
  receipts land under ``receipts_root()/merge-<build_id>/`` and a stage row is
  written BEFORE each irreversible act, probed on restart.

Any refusal, conflict, or verify failure stops the run with nothing
half-done: the branch is always kept, and the report says plainly which step
failed and why.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from nats_core.events import ApprovalResponsePayload, StageCompletePayload
from pydantic import ValidationError

from forge.lifecycle.persistence import StageLogEntry
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_STAGE_LABEL,
    MERGE_OFFER_TARGET_IDENTIFIER,
    git_rev_parse_main,
)
from forge.pipeline.digest_conformance import run_digest_conformance
from forge.receipts import receipts_root

logger = logging.getLogger(__name__)

__all__ = [
    "MERGE_DECISION_TARGET_IDENTIFIER",
    "MERGE_REPORT_STAGE_LABEL",
    "MERGE_REPORT_TARGET_IDENTIFIER",
    "MERGE_RESPONSE_SUBJECT_FILTER",
    "MERGE_STEP_DEPLOY_TARGET_IDENTIFIER",
    "MERGE_STEP_MERGE_TARGET_IDENTIFIER",
    "MERGE_VERIFY_TIMEOUT_DEFAULT_SECONDS",
    "MERGE_WALL_CAP_SECONDS",
    "MERGE_WALL_MERGE_ALLOWANCE_SECONDS",
    "MergeApprovalConsumer",
    "MergeDeployOutcome",
    "MergeExecutorDeps",
    "RED_MERGE_ENDINGS",
    "build_in_daemon_deploy_dispatcher",
    "execute_merge_deploy",
    "merge_wall_seconds",
    "merged_after_all_sha",
]

#: CORE subscription filter — one token per feature (``merge-FEAT-X``).
# NATS wildcards match WHOLE tokens only — the original "merge-*" partial
# matched nothing, and the first real press (FEAT-7CEA, 2026-08-24 22:21)
# published into silence and proved it live. Subscribe to every forge
# approval response instead; the handler skips non-merge request ids quietly.
MERGE_RESPONSE_SUBJECT_FILTER: str = "agents.approval.forge.*.response"

#: ``request_id`` prefix — the remainder is the REAL build_id.
REQUEST_ID_PREFIX: str = "merge-"

#: Durable decision row (written BEFORE any act — the double-run fence).
MERGE_DECISION_TARGET_IDENTIFIER: str = "merge_deploy_decision"

#: Durable per-step claim rows, written BEFORE each irreversible act.
MERGE_STEP_MERGE_TARGET_IDENTIFIER: str = "merge_deploy_merge"
MERGE_STEP_DEPLOY_TARGET_IDENTIFIER: str = "merge_deploy_deploy"

#: The outcome report's identity on ``pipeline.stage-complete.{feature_id}``.
MERGE_REPORT_STAGE_LABEL: str = "merge-deploy"
MERGE_REPORT_TARGET_IDENTIFIER: str = "merge_deploy_executor"

#: How long one run of the post-merge checks may take when the configuration
#: does not say (``merge_executor.verify_timeout_seconds``).
MERGE_VERIFY_TIMEOUT_DEFAULT_SECONDS: int = 600

#: Seconds allowed for the merge itself, on top of the two check runs, when
#: sizing the wall around the whole command.
MERGE_WALL_MERGE_ALLOWANCE_SECONDS: int = 180

#: The longest wall the deploy sidecar will accept. Written out here rather
#: than imported so this module does not depend on the sidecar; a test pins
#: the two together (``deploy_sidecar.service.MERGE_TIMEOUT_MAX``).
MERGE_WALL_CAP_SECONDS: int = 1800


def merge_wall_seconds(verify_timeout_seconds: int) -> int:
    """How long the whole merge command may take, given one check run's limit.

    The merge command may run the checks TWICE — once on main to see what was
    already failing, once on the merged tree — and merges in between. So the
    wall around the whole thing holds two check runs plus three minutes, and
    never more than the deploy sidecar will accept. Sized any smaller, the
    outer wall fires first and the merge is killed mid-flight: the branch is
    on main and nothing says so.
    """
    wall = 2 * int(verify_timeout_seconds) + MERGE_WALL_MERGE_ALLOWANCE_SECONDS
    return min(wall, MERGE_WALL_CAP_SECONDS)


def _verify_timeout_from(config: Any) -> int:
    """Read ``merge_executor.verify_timeout_seconds``, falling back plainly."""
    value = getattr(
        getattr(config, "merge_executor", None), "verify_timeout_seconds", None
    )
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return MERGE_VERIFY_TIMEOUT_DEFAULT_SECONDS
    return value


#: ``details_json`` key on the decision row.
MERGE_DECISION_DETAILS_KEY: str = "merge_decision"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_receipts_root() -> Path:
    return receipts_root()


@dataclass
class MergeDeployOutcome:
    """The executor's one-line truth, mirrored onto the report payload."""

    result: str  # merged-and-running | merged-deploy-reverted | merged-deploy-failed | merge-refused | rejected
    status: str  # PASSED | FAILED | SKIPPED
    detail: str
    merged_sha: str | None = None
    failed_step: str | None = None
    verdict: str | None = None
    checks_passed: int | None = None
    checks_total: int | None = None
    #: What the post-merge checks did, in guardkit's own word: "failed" when
    #: they ran and something went red, "unverified" when they could not run at
    #: all. ``None`` for every ending that is not about the checks.
    verify_status: str | None = None


@dataclass
class MergeExecutorDeps:
    """Injected collaborators — every seam a test can fake offline.

    Args:
        config: The validated ``ForgeConfig`` (reads
            ``approval.expected_approver`` and ``planning.target_repo_paths``).
        pool: The shared ``SqliteLifecyclePersistence`` facade.
        pipeline_publisher: Owns ``publish_stage_complete`` — publish ONLY,
            never a PIPELINE consumer (workqueue stream; jarvis owns those
            subjects).
        guardkit_run: The frozen subprocess boundary
            (:func:`forge.adapters.guardkit.run.run`); tests fake it.
        deploy_dispatcher: ``async (**kw) -> DeployStageResult | None`` —
            production binds :func:`build_in_daemon_deploy_dispatcher`.
        clock: Wall-clock seam.
        receipts_root_fn: Receipts-root seam (env-steered in production).
    """

    config: Any
    pool: Any
    pipeline_publisher: Any
    guardkit_run: Callable[..., Awaitable[Any]]
    deploy_dispatcher: Callable[..., Awaitable[Any]]
    clock: Callable[[], datetime] = field(default=_utcnow)
    receipts_root_fn: Callable[[], Path] = field(default=_default_receipts_root)


# ---------------------------------------------------------------------------
# Report parsing helpers (defensive — the guardkit merge verb is parallel-built)
# ---------------------------------------------------------------------------


def _parse_merge_report(result: Any) -> dict[str, Any] | None:
    """Extract the merge verb's ``--json`` report from stdout_tail/artefacts."""
    stdout_tail = getattr(result, "stdout_tail", "") or ""
    start = stdout_tail.find("{")
    end = stdout_tail.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stdout_tail[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    for ref in getattr(result, "artefacts", None) or []:
        try:
            path = Path(ref)
            if path.suffix == ".json" and path.is_file():
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    return parsed
        except (OSError, ValueError):
            continue
    return None


_FAILURE_STATUS_WORDS = frozenset(
    {
        "refused",
        "refusal",
        "conflict",
        "failed",
        "error",
        "verify-failed",
        "verify_failed",
    }
)


def _report_refusal(report: dict[str, Any]) -> str | None:
    """Return a plain-words refusal when the report says the merge did not land."""
    detail = (
        report.get("detail")
        or report.get("reason")
        or report.get("message")
        or report.get("error")
    )
    status_val = str(report.get("status", "")).strip().lower()
    if status_val in _FAILURE_STATUS_WORDS:
        return str(detail or f"the merge reported {status_val}")
    if report.get("ok") is False:
        return str(detail or "the merge report says ok=false")
    if report.get("refused") or report.get("conflict"):
        return str(detail or "the merge was refused")
    return None


def _report_int(report: dict[str, Any] | None, key: str) -> int | None:
    if not report:
        return None
    value = report.get(key)
    return value if isinstance(value, int) else None


#: The endings where the merge LANDED and what followed it went red. These
#: are the ones worth a repair job: the branch is on main and the estate is
#: not healthy. ``merge-refused`` is deliberately absent — a refused merge
#: changed nothing.
RED_MERGE_ENDINGS: frozenset[str] = frozenset(
    {"merged-verify-failed", "merged-deploy-reverted", "merged-deploy-failed"}
)


def _mint_repair_row(
    pool: Any,
    build_id: str,
    outcome: "MergeDeployOutcome",
    *,
    feature_id: str | None = None,
) -> None:
    """File one repair row for a merge whose checks went red. Never raises.

    A repair is only worth filing when the checks RAN and something came back
    red — there is code to fix then. When the checks could not run at all, the
    thing that is broken is the check itself, and no amount of building will
    mend it; the first real press of a merge card filed exactly such a repair
    for a failure no code could fix. So that case files nothing and says so in
    one line instead.

    The producer already swallows everything; this call site catches too,
    because the merge report is the only durable record of what the merge
    did and a queue row must never be able to cost it.
    """
    if outcome.result == "merged-verify-failed" and outcome.verify_status != "failed":
        logger.info(
            "merge-executor: the checks for %s could not run, so no repair was "
            "filed — a person must look at the check itself",
            feature_id or build_id,
        )
        return
    try:
        from forge.pipeline.fix_row_producer import (
            SOURCE_MERGE_REPORT,
            maybe_mint_fix_row,
        )

        maybe_mint_fix_row(
            pool=pool,
            build_id=build_id,
            source=SOURCE_MERGE_REPORT,
            detail=f"{outcome.result} — {outcome.detail}",
        )
    except Exception as exc:  # noqa: BLE001 — a queue row never costs a report
        logger.warning(
            "merge-executor: filing a repair row for %s raised (%s: %s); "
            "the merge report stands",
            build_id,
            type(exc).__name__,
            exc,
        )


async def _git_exit_code(repo_root: Path, *args: str) -> int | None:
    """Run one git command in ``repo_root`` and return its exit code.

    ``None`` when git could not be run at all. The same guardkit-free path the
    offer uses to read main's sha: plain git, no shell, nothing written.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    except Exception as exc:  # noqa: BLE001 — best-effort probe, honest None
        logger.warning(
            "merge-executor: git %s could not be run in %s (%s)",
            " ".join(args),
            repo_root,
            exc,
        )
        return None
    return proc.returncode


async def merged_after_all_sha(
    repo_root: Path, feature_id: str, expect_main_sha: str
) -> str | None:
    """Did the merge land even though the command gave no answer?

    When the merge command is killed — a timeout, or a crash after git had
    already done the merge — it exits without printing its report. Calling
    that "the merge was refused" is a lie the first small-scale drive caught:
    the branch was on main and the report said nothing had happened.

    So ask git itself, the same way the merge card pins its target commit
    (:func:`forge.pipeline.merge_offer.git_rev_parse_main`). If main has moved
    off the commit the merge was pinned to, AND main now contains the tip of
    ``autobuild/<feature>``, the merge landed: return main's new commit.
    Anything else — main unmoved, main moved for some other reason, git not
    answering — returns ``None`` and the merge is reported as refused, which
    is then the truth.
    """
    new_main = await git_rev_parse_main(repo_root)
    pinned = (expect_main_sha or "").strip().lower()
    if not new_main or new_main.strip().lower() == pinned:
        return None
    branch = f"autobuild/{feature_id}"
    contains = await _git_exit_code(
        repo_root, "merge-base", "--is-ancestor", branch, new_main
    )
    if contains != 0:
        return None
    return new_main


def _report_sha(report: dict[str, Any] | None) -> str | None:
    if not report:
        return None
    # "post_sha" is the key the guardkit merge verb actually emits (the first
    # real fire's receipt proved the happy-path sha was being dropped).
    for key in ("merged_sha", "post_sha", "merge_sha", "merge_commit", "sha"):
        value = report.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# The executor coroutine
# ---------------------------------------------------------------------------


async def execute_merge_deploy(
    *,
    deps: MergeExecutorDeps,
    build_id: str,
    feature_id: str,
    repo: str,
    repo_root: Path,
    expect_main_sha: str,
    correlation_id: str,
    decided_by: str,
    baseline_failing: list[str] | None = None,
    dry_run: bool = False,
) -> MergeDeployOutcome:
    """Run merge -> deploy -> report for one approved (or attended) press.

    Never raises past its boundary: every result class lands as an honest
    :class:`MergeDeployOutcome`, one additive ``stage-complete`` publish, and
    per-step JSON receipts under ``receipts_root()/merge-<build_id>/``.
    """
    started = deps.clock()
    receipts_dir = deps.receipts_root_fn() / f"merge-{build_id}"
    # Advisory digest-conformance state — filled in after a landed merge,
    # read by the report step. It can add a warning line; it can never
    # block anything.
    digest_conformance: dict[str, Any] = {}

    def _write_receipt(name: str, data: dict[str, Any]) -> None:
        try:
            receipts_dir.mkdir(parents=True, exist_ok=True)
            (receipts_dir / name).write_text(
                json.dumps(data, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error(
                "merge-executor: could not write receipt %s for %s (%s)",
                name,
                build_id,
                exc,
            )

    def _has_step(target_identifier: str) -> bool:
        return any(
            s.target_identifier == target_identifier
            for s in deps.pool.read_stages(build_id)
        )

    def _claim_step(target_identifier: str, details: dict[str, Any]) -> None:
        now = deps.clock()
        deps.pool.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=MERGE_REPORT_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=target_identifier,
                status="GATED",
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details=details,
            )
        )

    def _record_report(payload: StageCompletePayload, completed: datetime) -> None:
        """Put the merge report on the build's own record, not only on the bus.

        The published report is a message and the receipt is a file, and
        neither can be read back by a query. So the same report is also
        appended to ``stage_log``, with its outcome word at the top level of
        the details as ``result`` — that is what the self-closed defect rate
        reads to tell a repair that merged, deployed and stayed green from
        one that stopped at a red step (``lifecycle/metrics.py``).

        A dry run never gets here: it leaves no durable rows on purpose.
        Never raises — the bus report and the receipt on disk are the record,
        and a row that cannot be written must not cost them.
        """
        try:
            deps.pool.record_stage(
                StageLogEntry(
                    build_id=build_id,
                    stage_label=MERGE_REPORT_STAGE_LABEL,
                    target_kind="local_tool",
                    target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
                    status=payload.status,
                    gate_mode=None,
                    started_at=completed,
                    completed_at=completed,
                    duration_secs=float(payload.duration_secs or 0.0),
                    details=payload.model_dump(mode="json"),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a row never costs a report
            logger.error(
                "merge-executor: could not record the merge report for %s "
                "(%s: %s) — the published report and the receipt stand",
                build_id,
                type(exc).__name__,
                exc,
            )

    async def _publish_report(outcome: MergeDeployOutcome) -> MergeDeployOutcome:
        completed = deps.clock()
        conformance_warning = digest_conformance.get("warning")
        if conformance_warning:
            outcome.detail = f"{outcome.detail}\nWARNING: {conformance_warning}"
        payload = StageCompletePayload(
            feature_id=feature_id,
            build_id=build_id,
            stage_label=MERGE_REPORT_STAGE_LABEL,
            target_kind="local_tool",
            target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
            status=outcome.status,
            gate_mode=None,
            coach_score=None,
            duration_secs=max(0.0, (completed - started).total_seconds()),
            completed_at=completed.isoformat(),
            correlation_id=correlation_id,
            # Additive fields — StageCompletePayload is extra="allow".
            result=outcome.result,
            merged_sha=outcome.merged_sha,
            failed_step=outcome.failed_step,
            verdict=outcome.verdict,
            checks_passed=outcome.checks_passed,
            checks_total=outcome.checks_total,
            verify_status=outcome.verify_status,
            detail=outcome.detail,
            digest_conformance_warning=conformance_warning,
            dry_run=dry_run,
        )
        if dry_run:
            logger.info(
                "merge-executor: dry run — outcome kept to receipts only, "
                "no stage-complete published for %s",
                build_id,
            )
            _write_receipt("merge_deploy_report.json", payload.model_dump(mode="json"))
            return outcome
        try:
            await deps.pipeline_publisher.publish_stage_complete(payload)
        except Exception as exc:  # noqa: BLE001 — the report is derived truth
            logger.error(
                "merge-executor: outcome publish failed for %s (%s) — the "
                "receipts on disk remain the record",
                build_id,
                exc,
            )
        _write_receipt("merge_deploy_report.json", payload.model_dump(mode="json"))
        _record_report(payload, completed)
        # A MERGE THAT DID NOT STAY GREEN BECOMES A REPAIR JOB (conductor
        # rewire rule 1). The three red endings below are the ones where the
        # merge itself landed and what came after it went red — the live
        # checks, the deploy, or the revert. A refused merge is not one of
        # them: nothing changed, so there is nothing to repair. The dry run
        # is not one either: it changed nothing on purpose.
        if not dry_run and outcome.result in RED_MERGE_ENDINGS:
            _mint_repair_row(deps.pool, build_id, outcome, feature_id=feature_id)
        return outcome

    # ------------------------------------------------------------------
    # STEP merge + verify (through the frozen guardkit boundary)
    # ------------------------------------------------------------------
    if _has_step(MERGE_STEP_MERGE_TARGET_IDENTIFIER):
        logger.error(
            "merge-executor: %s already has a merge step on record — refusing "
            "to run the merge twice",
            build_id,
        )
        return await _publish_report(
            MergeDeployOutcome(
                result="merge-refused",
                status="FAILED",
                failed_step="merge",
                detail=(
                    "a merge step is already on record for this build — "
                    "refusing to run it twice"
                ),
            )
        )
    if dry_run:
        # A dry run merges NOTHING and leaves NO durable step rows — a claimed
        # step would make a later real press refuse "already on record". It
        # proves the plumbing end to end and exercises the deploy stage's own
        # dry mode below; the receipts on disk are its only record.
        _write_receipt(
            "merge_deploy_merge.json",
            {
                "step": "merge",
                "dry_run": True,
                "skipped": (
                    "dry run — nothing merged; a real press would merge "
                    f"autobuild/{feature_id} into main at {expect_main_sha}"
                ),
            },
        )
        merged_sha = None
        checks_passed = None
        checks_total = None
    else:
        _claim_step(
            MERGE_STEP_MERGE_TARGET_IDENTIFIER,
            {
                "merge_step": {
                    "expect_main_sha": expect_main_sha,
                    "decided_by": decided_by,
                    "dry_run": dry_run,
                }
            },
        )

        verify_timeout = _verify_timeout_from(deps.config)
        merge_wall = merge_wall_seconds(verify_timeout)
        args = [
            "merge",
            feature_id,
            "--target",
            "main",
            "--expect-main-sha",
            expect_main_sha,
            # How long ONE run of the checks may take. The wall below holds
            # the whole command: two such runs plus the merge between them.
            "--verify-timeout",
            str(verify_timeout),
            "--json",
        ]
        baseline_path: Path | None = None
        if baseline_failing is not None:
            baseline_path = (
                repo_root / ".guardkit" / "tmp" / f"merge-baseline-{build_id}.json"
            )
            try:
                baseline_path.parent.mkdir(parents=True, exist_ok=True)
                baseline_path.write_text(
                    json.dumps(
                        {"failing_node_ids": baseline_failing},
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                args += ["--baseline-json", str(baseline_path)]
            except OSError as exc:
                logger.warning(
                    "merge-executor: could not write the baseline file for %s "
                    "(%s) — the merge runs without a pre-merge baseline",
                    build_id,
                    exc,
                )
                baseline_path = None

        result = await deps.guardkit_run(
            subcommand="autobuild",
            args=args,
            repo_path=repo_root,
            read_allowlist=[repo_root],
            timeout_seconds=merge_wall,
            with_nats_streaming=False,
        )
        report = _parse_merge_report(result)
        # The merge verb exits non-zero for "merged but the checks after it
        # did not pass" (exit 4) — the first real fire (FEAT-7CEA) proved that
        # calling a LANDED merge "merge-refused" is a lie. Trust the report's
        # own outcome over the exit code.
        merged_in_report = bool(report and report.get("outcome") == "merged")
        refusal: str | None = None
        result_status = getattr(result, "status", "failed")
        stderr = (getattr(result, "stderr", None) or "").strip()
        tail = (getattr(result, "stdout_tail", "") or "").strip()
        # Whatever the sidecar or guardkit itself said about the trouble, in
        # its own words — the timeout sentence, the missing-command sentence.
        own_sentence = (
            stderr[-400:]
            or tail[-400:]
            or f"the merge command did not succeed (status={result_status})"
        )
        if not merged_in_report:
            generic: str | None = None
            if result_status != "success":
                generic = (
                    f"the merge command did not succeed (status={result_status})"
                    + (
                        f": {stderr[-400:]}"
                        if stderr
                        else (f": {tail[-400:]}" if tail else "")
                    )
                )
            if report is not None:
                # A REPORT THAT PARSED SPEAKS FOR ITSELF. Guardkit writes one
                # plain sentence saying why it would not merge (a dirty tree,
                # a target that moved, a missing branch); passing that through
                # verbatim beats wrapping it in words of our own.
                spoken = report.get("refusal_reason")
                if isinstance(spoken, str) and spoken.strip():
                    refusal = spoken.strip()
                else:
                    refusal = generic or _report_refusal(report)
            else:
                refusal = generic

        # THE MERGE MAY HAVE LANDED ANYWAY. A command that was killed, or that
        # died before it could print its report, leaves no answer at all — but
        # git knows. Ask git before calling a landed merge refused.
        landed_sha: str | None = None
        if refusal and report is None and result_status != "success":
            landed_sha = await merged_after_all_sha(
                repo_root, feature_id, expect_main_sha
            )

        _write_receipt(
            "merge_deploy_merge.json",
            {
                "step": "merge",
                "status": result_status,
                "exit_code": getattr(result, "exit_code", None),
                "refusal": refusal,
                "report": report,
                "landed_sha": landed_sha,
                "stdout_tail": (getattr(result, "stdout_tail", "") or "")[-4000:],
                "baseline_file": str(baseline_path) if baseline_path else None,
            },
        )
        if refusal and landed_sha:
            return await _publish_report(
                MergeDeployOutcome(
                    result="merged-verify-failed",
                    status="FAILED",
                    merged_sha=landed_sha,
                    failed_step="verify",
                    detail=(
                        f"{feature_id} merged ({landed_sha[:10]}), but the "
                        f"post-merge checks could not finish: {own_sentence}. "
                        "The deploy was not dispatched."
                    ),
                    verify_status="unverified",
                )
            )
        if refusal:
            return await _publish_report(
                MergeDeployOutcome(
                    result="merge-refused",
                    status="FAILED",
                    failed_step="merge",
                    detail=refusal,
                )
            )

        merged_sha = _report_sha(report)
        checks_passed = _report_int(report, "checks_passed")
        checks_total = _report_int(report, "checks_total")

        # Advisory: does the merged tree keep the promises in the feature's
        # spec digest? Deterministic and never blocking — the receipt lands
        # beside the other merge receipts and any failure rides the merge
        # report as one plain warning line. Built after FEAT-EF8D
        # (2026-08-26), where every test was green but the built endpoint
        # did not do what the approved digest promised.
        try:
            conformance = run_digest_conformance(
                repo_root=repo_root, feature_id=feature_id
            )
        except Exception as exc:  # noqa: BLE001 — advisory must never stop a merge
            conformance = {
                "advisory": True,
                "feature_id": feature_id,
                "conformant": None,
                "checks": [],
                "warning": None,
                "skipped": (
                    "the digest conformance check itself failed "
                    f"({exc}) — nothing was checked"
                ),
            }
        digest_conformance.update(conformance)
        _write_receipt("digest_conformance.json", conformance)

        if merged_in_report and report.get("verify_ok") is False:
            charged = report.get("charged_failures") or []
            # Guardkit says "unverified" when the checks could not START at
            # all — a missing interpreter, a command that is not there. That
            # is not a failing test, and calling it one sent Rich looking for
            # a red test that did not exist. Say which of the two happened.
            could_not_run = (
                str(report.get("verify_status") or "").strip().lower() == "unverified"
            )
            why = str(
                report.get("verify_detail")
                or report.get("verify_status")
                or "verification failed"
            )
            if could_not_run:
                detail = (
                    f"{feature_id} merged ({(merged_sha or '')[:10]}), but the "
                    f"post-merge checks could not run: {why}. "
                    "The deploy was not dispatched."
                )
            else:
                detail = (
                    f"{feature_id} merged ({(merged_sha or '')[:10]}), but the "
                    f"post-merge checks did not pass: {why}"
                    + (f" — {len(charged)} charged failure(s)" if charged else "")
                    + ". The deploy was not dispatched."
                )
            return await _publish_report(
                MergeDeployOutcome(
                    result="merged-verify-failed",
                    status="FAILED",
                    merged_sha=merged_sha,
                    failed_step="verify",
                    detail=detail,
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                    verify_status="unverified" if could_not_run else "failed",
                )
            )

    # ------------------------------------------------------------------
    # STEP deploy (mirrors the attended dispatch, in-daemon)
    # ------------------------------------------------------------------
    if not dry_run:
        if _has_step(MERGE_STEP_DEPLOY_TARGET_IDENTIFIER):
            logger.error(
                "merge-executor: %s already has a deploy step on record — "
                "refusing to dispatch the deploy twice",
                build_id,
            )
            return await _publish_report(
                MergeDeployOutcome(
                    result="merged-deploy-failed",
                    status="FAILED",
                    merged_sha=merged_sha,
                    failed_step="deploy",
                    detail=(
                        "the merge landed but a deploy step is already on "
                        "record — refusing to dispatch it twice"
                    ),
                )
            )
        _claim_step(
            MERGE_STEP_DEPLOY_TARGET_IDENTIFIER,
            {"deploy_step": {"merged_sha": merged_sha, "dry_run": dry_run}},
        )

    try:
        deploy_result = await deps.deploy_dispatcher(
            repo=repo,
            repo_root=repo_root,
            feature_id=feature_id,
            build_id=build_id,
            correlation_id=correlation_id,
            decided_by=decided_by,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — the sidecar-surface ValueError crack
        _write_receipt(
            "merge_deploy_deploy.json",
            {"step": "deploy", "error": str(exc), "dry_run": dry_run},
        )
        return await _publish_report(
            MergeDeployOutcome(
                result="merged-deploy-failed",
                status="FAILED",
                merged_sha=merged_sha,
                failed_step="deploy",
                detail=(
                    (
                        "dry run — nothing merged; "
                        if dry_run
                        else "the merge landed but "
                    )
                    + f"the deploy dispatch raised: {exc}"
                ),
                checks_passed=checks_passed,
                checks_total=checks_total,
            )
        )

    d_outcome = getattr(deploy_result, "outcome", None)
    verdict = getattr(deploy_result, "verdict", None)
    _write_receipt(
        "merge_deploy_deploy.json",
        {
            "step": "deploy",
            "outcome": d_outcome,
            "verdict": verdict,
            "record": getattr(deploy_result, "deploy_record_ref", None),
            "dry_run": dry_run,
        },
    )
    if checks_passed is None or checks_total is None:
        m = re.search(r"(\d+)\s*/\s*(\d+)", str(verdict or ""))
        if m:
            checks_passed, checks_total = int(m.group(1)), int(m.group(2))

    if deploy_result is None:
        outcome = MergeDeployOutcome(
            result="merged-deploy-failed",
            status="FAILED",
            merged_sha=merged_sha,
            failed_step="deploy",
            detail=(
                "the merge landed but the deploy stage is disabled "
                "(deploy.enabled=false) — nothing was deployed"
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
        )
    elif d_outcome == "complete":
        checks = (
            f" — checks {checks_passed}/{checks_total}"
            if checks_passed is not None and checks_total is not None
            else ""
        )
        outcome = MergeDeployOutcome(
            result="merged-and-running",
            status="PASSED",
            merged_sha=merged_sha,
            verdict=str(verdict) if verdict is not None else None,
            detail=(
                f"{feature_id} merged and running{checks}. Rollback is one "
                "command; the branch is kept."
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
        )
    elif d_outcome == "reverted":
        outcome = MergeDeployOutcome(
            result="merged-deploy-reverted",
            status="FAILED",
            merged_sha=merged_sha,
            failed_step="deploy",
            verdict=str(verdict) if verdict is not None else None,
            detail=(
                f"{feature_id} merged, but the live checks failed and the "
                "deploy was rolled back — live is untouched; the merge stands "
                "and the branch is kept."
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
        )
    else:
        outcome = MergeDeployOutcome(
            result="merged-deploy-failed",
            status="FAILED",
            merged_sha=merged_sha,
            failed_step="deploy",
            verdict=str(verdict) if verdict is not None else None,
            detail=(
                (
                    "dry run — nothing merged; the deploy ended "
                    if dry_run
                    else f"{feature_id} merged, but the deploy ended "
                )
                + f"{d_outcome or 'without an outcome'} — nothing further was "
                "touched"
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
        )
    return await _publish_report(outcome)


# ---------------------------------------------------------------------------
# The in-daemon deploy dispatcher (mirrors cli/_deploy_run.py exactly)
# ---------------------------------------------------------------------------


def build_in_daemon_deploy_dispatcher(
    *, config: Any, nats_client: Any, db_path: Any
) -> Callable[..., Awaitable[Any]]:
    """Bind the production deploy dispatch for the executor's deploy step.

    Mirrors ``forge.cli._deploy_run`` but in-daemon: profile + live-gate
    invoker from the target repo, runbook DDL ensured idempotently (the C4
    ``no such table: runbooks`` lesson), and ``execution_surface`` forced to
    ``sidecar`` on a config COPY — the daemon container has no docker, and
    ``_resolve_script_runner`` reads only the passed config. ONE dispatch runs
    candidate -> gates -> promote -> live-gate -> auto-revert internally. The
    boot-time stash ``_serve_daemon.deploy_stage_runner`` is NEVER touched
    (its seams raise).
    """

    async def _dispatch(
        *,
        repo: str,
        repo_root: Path,
        feature_id: str,
        build_id: str,
        correlation_id: str,
        decided_by: str,
        dry_run: bool,
    ) -> Any:
        from forge.adapters.nats.deploy_publisher import DeployPublisher
        from forge.adapters.nats.runbook_publisher import RunbookPublisher
        from forge.adapters.sqlite.connect import connect_writer
        from forge.deploy.composition import dispatch_deploy_stage
        from forge.deploy.live_gate import RepoDriverLiveGateInvoker
        from forge.deploy.profile import load_deploy_profile
        from forge.persistence.migrations import runbook as runbook_migration
        from forge.persistence.repositories.runbook import RunbookRepository

        if db_path is None:
            raise RuntimeError(
                "merge-deploy: no forge DB path was threaded into the deploy "
                "dispatcher — the deploy stage cannot persist its runbooks"
            )
        repo_root = Path(repo_root)
        profile = load_deploy_profile(repo_root / "deploy" / "profile.yaml")
        spec = profile.live_gate
        invoker = None
        if spec is not None:
            invoker = RepoDriverLiveGateInvoker(
                repo_path=repo_root,
                driver_argv=list(spec.driver),
                timeout_seconds=spec.timeout_seconds,
                extra_env=dict(spec.env),
            )
        connection = connect_writer(db_path)
        # Boot-idempotent DDL — the production DB predates it (C4 live-caught:
        # the first dispatch died on `no such table: runbooks`).
        runbook_migration.apply(connection)
        repository = RunbookRepository(connection=connection)
        runbook_publisher = RunbookPublisher(nats_client=nats_client)
        deploy_publisher = DeployPublisher(nats_client=nats_client)
        # The daemon container has no docker; force the sidecar surface on a
        # COPY (the stage reads only the config it is passed).
        deploy_cfg = config.deploy.model_copy(update={"execution_surface": "sidecar"})
        return await dispatch_deploy_stage(
            deploy_cfg,
            profile,
            correlation_id=correlation_id,
            deploy_run_id=str(uuid.uuid4()),
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_publisher,
            live_gate_invoker=invoker,
            deploy_record_root=str(repo_root / deploy_cfg.deploy_record_dir),
            dry_run=dry_run,
            target_repo=repo,
            target_repo_root=str(repo_root),
            feature=feature_id,
            feat_id=feature_id,
            # Distinct per run AND TASK-shaped — DeployQueuedPayload validates
            # ^TASK-[A-Z0-9]{3,12}$ (the first dry fire caught this live);
            # the time suffix also keeps the date-granular F7 record filename
            # distinct across same-day runs.
            task_id=_deploy_task_id(feature_id),
            deployer=f"merge-word:{decided_by}",
        )

    return _dispatch


def _deploy_task_id(feature_id: str) -> str:
    """A TASK-shaped, per-run-distinct id for the deploy leg.

    DeployQueuedPayload validates ``^TASK-[A-Z0-9]{3,12}$``. Compose it from
    the feature's own suffix plus a UTC HHMMSS stamp so two same-day runs
    never collide on the date-granular deploy-record filename.
    """
    from datetime import datetime, timezone

    suffix = (
        "".join(ch for ch in feature_id.upper().removeprefix("FEAT-") if ch.isalnum())[
            :6
        ]
        or "MERGE"
    )
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    return f"TASK-{(suffix + stamp)[:12]}"


# ---------------------------------------------------------------------------
# The consumer
# ---------------------------------------------------------------------------


class MergeApprovalConsumer:
    """Consumes merge-card presses and runs the executor on a genuine approve.

    Refusals are quiet on the wire but loud in the log — a forged or stale
    response never runs anything, and the honest reason is one grep away.
    """

    def __init__(self, deps: MergeExecutorDeps) -> None:
        self._deps = deps
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._subscription: Any = None

    async def attach(self, envelope_client: Any) -> Any:
        """Subscribe (CORE NATS) via an envelope-aware client.

        ``envelope_client`` follows the ``EnvelopeSubscribeClient`` contract:
        ``subscribe(topic, callback)`` where the callback receives a validated
        :class:`~nats_core.envelope.MessageEnvelope`.
        """
        self._subscription = await envelope_client.subscribe(
            MERGE_RESPONSE_SUBJECT_FILTER, self.handle_envelope
        )
        return self._subscription

    def _lock_for(self, repo: str) -> asyncio.Lock:
        return self._repo_locks.setdefault(repo, asyncio.Lock())

    async def handle_envelope(self, envelope: Any) -> None:
        """The full authz chain — every arm refuses quietly-but-loudly."""
        try:
            payload = ApprovalResponsePayload.model_validate(envelope.payload)
        except ValidationError as exc:
            logger.warning(
                "merge-executor: dropping a malformed approval response (%s)",
                exc,
            )
            return
        request_id = payload.request_id
        if not request_id.startswith(REQUEST_ID_PREFIX) or len(request_id) <= len(
            REQUEST_ID_PREFIX
        ):
            # Expected traffic, not a refusal: the wildcard subscription sees
            # EVERY forge approval response (gate taps included) — skip the
            # non-merge ones quietly.
            logger.debug(
                "merge-executor: ignoring %r — not a merge request id",
                request_id,
            )
            return
        build_id = request_id[len(REQUEST_ID_PREFIX) :]
        try:
            stages = self._deps.pool.read_stages(build_id)
        except Exception as exc:  # noqa: BLE001 — trust boundary
            logger.warning(
                "merge-executor: refusing %s — could not read its stage log (%s)",
                request_id,
                exc,
            )
            return
        offers = [
            s for s in stages if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
        ]
        if not offers:
            logger.warning(
                "merge-executor: refusing %s — no durable merge offer is on "
                "record for build %s",
                request_id,
                build_id,
            )
            return
        offer = offers[-1].details.get(MERGE_OFFER_DETAILS_KEY) or {}
        if offer.get("request_id") != request_id:
            logger.warning(
                "merge-executor: refusing %s — the durable offer carries a "
                "different request_id (%r)",
                request_id,
                offer.get("request_id"),
            )
            return
        expected = getattr(self._deps.config.approval, "expected_approver", None)
        if expected is not None and payload.decided_by != expected:
            logger.warning(
                "merge-executor: refusing %s — decided_by %r is not the "
                "expected approver",
                request_id,
                payload.decided_by,
            )
            return
        if envelope.correlation_id != offer.get("correlation_id"):
            logger.warning(
                "merge-executor: refusing %s — envelope correlation %r does "
                "not match the offer's",
                request_id,
                envelope.correlation_id,
            )
            return
        if any(s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER for s in stages):
            logger.warning(
                "merge-executor: refusing %s — a decision is already on "
                "record (restart can never double-run)",
                request_id,
            )
            return
        decision = payload.decision
        if decision not in ("approve", "reject"):
            logger.warning(
                "merge-executor: refusing %s — decision %r is not one of the "
                "offer's resume options (approve/reject)",
                request_id,
                decision,
            )
            return

        # Durable decision row FIRST — the restart / duplicate fence.
        now = self._deps.clock()
        self._deps.pool.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=MERGE_OFFER_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MERGE_DECISION_TARGET_IDENTIFIER,
                status="PASSED" if decision == "approve" else "SKIPPED",
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={
                    MERGE_DECISION_DETAILS_KEY: {
                        "decision": decision,
                        "decided_by": payload.decided_by,
                        "request_id": request_id,
                    }
                },
            )
        )

        feature_id = str(offer.get("feature_id") or "")
        correlation_id = str(offer.get("correlation_id") or "")
        if decision == "reject":
            report = StageCompletePayload(
                feature_id=feature_id,
                build_id=build_id,
                stage_label=MERGE_REPORT_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
                status="SKIPPED",
                gate_mode=None,
                coach_score=None,
                duration_secs=0.0,
                completed_at=now.isoformat(),
                correlation_id=correlation_id,
                result="rejected",
                detail=(
                    f"{payload.decided_by} rejected the merge — nothing "
                    "changed; the branch is kept."
                ),
            )
            try:
                await self._deps.pipeline_publisher.publish_stage_complete(report)
            except Exception as exc:  # noqa: BLE001 — the report is derived
                logger.error(
                    "merge-executor: reject report publish failed for %s (%s)",
                    build_id,
                    exc,
                )
            return

        # approve — resolve what the executor needs from the durable offer.
        repo = str(offer.get("repo") or "")
        repo_root_raw = self._deps.config.planning.target_repo_paths.get(repo)
        if not repo_root_raw:
            logger.error(
                "merge-executor: %s approved but repo %r has no entry in "
                "planning.target_repo_paths — the executor cannot run",
                request_id,
                repo,
            )
            return
        expect_main_sha = str(offer.get("expect_main_sha") or "")
        if not expect_main_sha:
            logger.error(
                "merge-executor: %s approved but the offer carries no "
                "expect_main_sha — refusing an unpinned merge",
                request_id,
            )
            return
        baseline = offer.get("baseline_failing")
        baseline_failing = (
            [str(x) for x in baseline] if isinstance(baseline, list) else None
        )
        task = asyncio.create_task(
            self._run_approved(
                build_id=build_id,
                feature_id=feature_id,
                repo=repo,
                repo_root=Path(repo_root_raw),
                expect_main_sha=expect_main_sha,
                correlation_id=correlation_id,
                decided_by=payload.decided_by,
                baseline_failing=baseline_failing,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_approved(
        self,
        *,
        build_id: str,
        feature_id: str,
        repo: str,
        repo_root: Path,
        expect_main_sha: str,
        correlation_id: str,
        decided_by: str,
        baseline_failing: list[str] | None,
    ) -> None:
        # Per-repo single-flight: an asyncio lock per repo key PLUS the
        # executor's own durable step probes.
        async with self._lock_for(repo):
            try:
                await execute_merge_deploy(
                    deps=self._deps,
                    build_id=build_id,
                    feature_id=feature_id,
                    repo=repo,
                    repo_root=repo_root,
                    expect_main_sha=expect_main_sha,
                    correlation_id=correlation_id,
                    decided_by=decided_by,
                    baseline_failing=baseline_failing,
                )
            except Exception as exc:  # noqa: BLE001 — the task must not die silent
                logger.error(
                    "merge-executor: executor raised (%s) for build %s — see "
                    "the receipts under merge-%s",
                    exc,
                    build_id,
                    build_id,
                )
