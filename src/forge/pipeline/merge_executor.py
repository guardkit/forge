"""The merge-and-deploy executor — code, never an AI session, runs the press.

Make-merge-work build spec (2026-08-24). Two halves:

* :class:`MergeApprovalConsumer` — a CORE NATS subscription (never JetStream;
  the AGENTS stream is no_ack) on ``agents.approval.forge.merge-*.response``.
  It refuses everything except: a durable pending offer whose ``request_id``
  matches, a ``decided_by`` that string-equals the deployment's expected
  approver VERBATIM, a matching correlation, and no decision yet on record
  (the durable decision row is written FIRST, so a restart can never
  double-run).
* :func:`execute_merge_deploy` — the executor coroutine. STEP candidate: the
  feature branch's tree is laid out inside the checkout and the deploy stage's
  candidate leg builds it, brings it up in the Docker Sandbox and runs the
  registered live checks against it; STEP merge+verify through the frozen
  guardkit subprocess boundary, only if every check passed; STEP tree check:
  the merged commit's tree must be the tree that was checked; STEP promote:
  the deploy stage's promote leg re-tags the candidate image as live (never a
  rebuild) and tears the candidate down; STEP report as one additive
  ``pipeline.stage-complete.{feature_id}`` publish. Per-step durable receipts
  land under ``receipts_root()/merge-<build_id>/`` and a stage row is written
  BEFORE each irreversible act, probed on restart.

PROTECT MAIN (the rewrite-on-refusal spec, Part J, 2026-09-07, on Rich's yes).
FEAT-8388 and FEAT-39F6 both reached api_test main with a defect the sandbox
gate found afterwards; the mission says main is the boundary. So the candidate
check moved in front of the merge. The merge word stays one touch; what
happens inside it changed order: (1) candidate built from the branch's exact
tree and checked; (2) merge, pinned to main's commit exactly as before; (3) the
merge's own post-merge test run; (4) promote the image that was checked;
(5) candidate torn down. A red check means no merge, no promote: the branch is
kept, a repair row is filed, and the report says so with the new result word
``candidate-refused``.

A MAIN THAT MOVED DURING THE BUILD is refused before the merge, not after it.
The pinned main commit is read when the offer is made — after the build — so a
main that moved WHILE the feature was building still matches the pin, the
merge command would land a merge commit carrying main's new work, its tree
could never equal the tree that was checked, and the promote would be refused
with the merge already on main (the coach's finding, 2026-09-07). So after a
green check and before the merge step is claimed the executor asks git one
question: is the pinned main commit in the branch? A "no" is ``merge-refused``
at the merge step with nothing claimed, nothing merged, the candidate torn
down and the branch kept. The tree comparison after the merge (rule 37) stays
as the belt for anything else.

Any refusal, conflict, or verify failure stops the run with nothing
half-done: the branch is always kept, the candidate is torn down and its tree
removed on every ending, and the report says plainly which step failed and why.

WHERE THE DEPLOY RAN (2026-09-06 decision): a repository whose deploy profile
carries a ``sandbox`` block is deployed into its own Docker Sandbox. When it
does, the report carries ``deployed_in: "docker-sandbox"``, which is how the
line Rich reads after a successful press comes to say the feature is running in
its Docker Sandbox. No sandbox block ⇒ no such field ⇒ the report is exactly
what it was.

A REPOSITORY WHOSE FACTORY LIVES IN ITS SANDBOX is pressed there, whole
(sandbox first, rules 62, 85 and 89). Its build's branch is made in the
sandbox's own clone, so every git operation of the press — the branch
look-up, main's commit, the ancestry guards, the candidate tree's lay-out and
removal, the tree-equality read — happens in there, through that sandbox's
deploy sidecar, beside the merge command, the deploy leg and the live gate
that already run in it. The press asks for those five operations through one
small surface (:class:`~forge.deploy.candidate_tree.CandidateGit`) and is
TOLD which venue it has: ``deps.git_surface`` builds the sandbox one for a
repository named in ``planning.sandboxes``, and for every other repository —
which is every repository until an operator gives one a sandbox — the venue
is :class:`~forge.deploy.candidate_tree.InContainerCandidateGit`, this file's
own git functions called in the order they have always been called. Part J's
order does not change; only where its git runs does.
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

from forge.deploy.candidate_tree import (
    CandidateGit,
    CandidateTreeError,
    InContainerCandidateGit,
)
from forge.lifecycle.persistence import StageLogEntry
from forge.pipeline.fix_row_producer import candidate_refused_sentence
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_STAGE_LABEL,
    MERGE_OFFER_TARGET_IDENTIFIER,
    branch_to_merge,
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
    "MERGE_STEP_CANDIDATE_TARGET_IDENTIFIER",
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
    "deployed_in_for",
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

#: The candidate check's own row — written when it ran, for the record. It is
#: not a claim: a check is torn down after itself and may safely run again.
MERGE_STEP_CANDIDATE_TARGET_IDENTIFIER: str = "merge_deploy_candidate"

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

    result: str  # merged-and-running | merged-deploy-reverted | merged-deploy-failed | merged-verify-failed | merge-refused | candidate-refused | rejected
    status: str  # PASSED | FAILED | SKIPPED
    detail: str
    merged_sha: str | None = None
    failed_step: str | None = None
    verdict: str | None = None
    checks_passed: int | None = None
    checks_total: int | None = None
    #: Where the deploy ran, when it ran somewhere worth naming.
    #: ``"docker-sandbox"`` when the repository's profile carries a sandbox
    #: block; None otherwise, which is every case that behaved as it always
    #: did. Jarvis reads it to say "running in its Docker Sandbox".
    deployed_in: str | None = None
    #: What the post-merge checks did, in guardkit's own word: "failed" when
    #: they ran and something went red, "unverified" when they could not run at
    #: all. ``None`` for every ending that is not about the checks.
    verify_status: str | None = None
    #: What the candidate check found BEFORE the merge (protect-main, rule
    #: 40): ``verdict``, ``checks_passed``, ``checks_total``, ``failed_checks``
    #: (names), ``candidate_sha``, ``candidate_tree``, ``merged_tree`` and
    #: ``trees_match``. None when the run stopped before the check began.
    gate_before_merge: dict[str, Any] | None = None
    #: The branch this press merged, or would have (Part M, rule 55): the
    #: build row's recorded journey branch for a repair, else the feature's
    #: own ``autobuild/<feature id>``. Set on every ending by the executor.
    branch: str | None = None


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
            production binds :func:`build_in_daemon_deploy_dispatcher`. The
            executor calls it once per leg with ``leg`` set to
            ``"candidate_check"`` (with ``candidate_cwd``, the branch's
            laid-out tree), ``"promote"`` (with ``prior_events``) or
            ``"candidate_down"``, plus the shared ``deploy_run_id`` and
            ``task_id`` so both legs are one deploy run.
        clock: Wall-clock seam.
        receipts_root_fn: Receipts-root seam (env-steered in production).
        git_surface: WHERE this repository's git happens (sandbox first,
            rule 89) — ``(repo, repo_root) -> CandidateGit | None``. The
            composition sets it only when some repository has a sandbox, and
            it answers ``None`` for a repository that has none. Left unset —
            the default, and every estate with an empty ``planning.sandboxes``
            — the press runs the very git functions it always ran, here.
    """

    config: Any
    pool: Any
    pipeline_publisher: Any
    guardkit_run: Callable[..., Awaitable[Any]]
    deploy_dispatcher: Callable[..., Awaitable[Any]]
    clock: Callable[[], datetime] = field(default=_utcnow)
    receipts_root_fn: Callable[[], Path] = field(default=_default_receipts_root)
    git_surface: Callable[[str, Path], "CandidateGit | None"] | None = None


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


def _last_sentence(text: str | None, limit: int = 300) -> str:
    """The last non-empty line of ``text``, whole words only.

    A command's stderr ends with the sentence that matters (the stopper's
    "stopped after N seconds", the missing-runner sentence) after however
    many log lines came before it. A raw character slice of the tail cut
    that sentence mid-word on Rich's merge card (seam coach, 2026-09-06);
    the last line, trimmed at a word boundary, reads as a sentence.
    """
    if not text:
        return ""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1]
    if len(last) <= limit:
        return last
    cut = last[-limit:]
    # Drop the leading partial word so the sentence starts on a whole one.
    _first_space, _sep, rest = cut.partition(" ")
    return rest or cut


def _conflict_sentence(report: dict[str, Any]) -> str | None:
    """One plain sentence for a report that stopped on a merge conflict."""
    if str(report.get("outcome", "")).strip().lower() != "conflict" and not report.get(
        "conflict_files"
    ):
        return None
    files = [str(f) for f in (report.get("conflict_files") or []) if str(f).strip()]
    if files:
        shown = ", ".join(files[:6]) + (f" and {len(files) - 6} more" if len(files) > 6 else "")
        return (
            f"the merge stopped on a conflict in {shown}; nothing was merged and "
            "the branch is kept"
        )
    return "the merge stopped on a conflict; nothing was merged and the branch is kept"


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


def deployed_in_for(repo_root: Path) -> str | None:
    """Where a deploy of this repository runs — a Docker Sandbox, or nowhere named.

    Returns ``"docker-sandbox"`` when the repository's ``deploy/profile.yaml``
    carries a sandbox block, and None otherwise — including when there is no
    profile, or it cannot be read. This only decides a word on a card, so an
    unreadable profile must never be the thing that fails a merge.
    """
    try:
        from forge.deploy.profile import load_deploy_profile

        profile = load_deploy_profile(Path(repo_root) / "deploy" / "profile.yaml")
    except Exception:  # noqa: BLE001 — a word on a card, never a failure
        return None
    return "docker-sandbox" if profile.sandbox is not None else None


def _mint_repair_row(
    pool: Any,
    build_id: str,
    outcome: "MergeDeployOutcome",
    *,
    feature_id: str | None = None,
) -> None:
    """File one repair row for a press that found the code wrong. Never raises.

    Three endings file one: the merge landed and the checks after it went
    red (the three ``RED_MERGE_ENDINGS``), and — protect-main — the branch
    failed its sandbox check BEFORE the merge (``candidate-refused``, with
    the failing checks named). A repair is only worth filing when a check
    RAN and something came back red — there is code to fix then. When the
    checks could not run at all, the thing that is broken is the check
    itself, and no amount of building will mend it; the first real press of
    a merge card filed exactly such a repair for a failure no code could fix.
    So that case files nothing and says so in one line instead. The same
    goes for a promote refused because main had moved under the check (the
    trees did not match): the remedy is to send the sentence again, not a
    repair.

    The producer already swallows everything; this call site catches too,
    because the merge report is the only durable record of what the merge
    did and a queue row must never be able to cost it.
    """
    gate = outcome.gate_before_merge or {}
    if outcome.result == "merged-verify-failed" and outcome.verify_status != "failed":
        logger.info(
            "merge-executor: the checks for %s could not run, so no repair was "
            "filed — a person must look at the check itself",
            feature_id or build_id,
        )
        return
    if outcome.result == "candidate-refused" and not gate.get("ran"):
        logger.info(
            "merge-executor: the sandbox check for %s could not run, so no "
            "repair was filed — a person must look at the check itself",
            feature_id or build_id,
        )
        return
    if (
        outcome.result == "merged-deploy-failed"
        and outcome.failed_step == "promote"
        and gate.get("trees_match") is False
    ):
        logger.info(
            "merge-executor: the promote of %s was refused because main had "
            "moved under the check, so no repair was filed — the sentence "
            "must be sent again",
            feature_id or build_id,
        )
        return
    try:
        from forge.pipeline.fix_row_producer import (
            SOURCE_CANDIDATE_REFUSED,
            SOURCE_MERGE_REPORT,
            maybe_mint_fix_row,
        )

        if outcome.result == "candidate-refused":
            total = gate.get("checks_total")
            passed = gate.get("checks_passed")
            failed_count = (
                total - passed
                if isinstance(total, int) and isinstance(passed, int)
                else None
            )
            maybe_mint_fix_row(
                pool=pool,
                build_id=build_id,
                source=SOURCE_CANDIDATE_REFUSED,
                detail=gate.get("refusal"),
                checks_failed=failed_count,
                checks_total=total if isinstance(total, int) else None,
                failing_checks=gate.get("failed_checks") or None,
            )
            return
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


def git_venue(git: Any, repo_root: Path | str) -> str:
    """Where this press's git happened, as a phrase a sentence can carry.

    ``in /home/rich/Projects/.../api_test`` when it happened in this
    container, ``in the sandbox that holds appmilla/api_test`` when it
    happened where the repository lives.
    """
    return str(getattr(git, "venue", "") or f"in {repo_root}")


def git_surface_for(
    deps: Any, repo: str, repo_root: Path | str
) -> CandidateGit:
    """The venue this repository's git happens in (sandbox first, rule 89).

    ``deps.git_surface`` is a factory the composition sets when SOME
    repository has a sandbox; it answers the sandbox's surface for a
    repository that has one and ``None`` for a repository that has not. With
    no factory at all — the default, and every estate whose
    ``planning.sandboxes`` is empty — the venue is this container, running
    the very functions the press has always run.
    """
    factory = getattr(deps, "git_surface", None)
    if factory is not None:
        try:
            surface = factory(repo, Path(repo_root))
        except Exception as exc:  # noqa: BLE001 — never let composition stop a press
            logger.error(
                "merge-executor: choosing where %s's git runs raised (%s: %s) — "
                "falling back to this container",
                repo,
                type(exc).__name__,
                exc,
            )
            surface = None
        if surface is not None:
            return surface
    return InContainerCandidateGit(repo_root)


async def merged_after_all_sha(
    repo_root: Path,
    feature_id: str,
    expect_main_sha: str,
    branch: str | None = None,
    *,
    git: CandidateGit | None = None,
) -> str | None:
    """Did the merge land even though the command gave no answer?

    When the merge command is killed — a timeout, or a crash after git had
    already done the merge — it exits without printing its report. Calling
    that "the merge was refused" is a lie the first small-scale drive caught:
    the branch was on main and the report said nothing had happened.

    So ask git itself, the same way the merge card pins its target commit
    (:func:`forge.pipeline.merge_offer.git_rev_parse_main`). If main has moved
    off the commit the merge was pinned to, AND main now contains the tip of
    the branch the press was merging (``branch`` when given — a repair's own
    journey branch, Part M rule 54 — else ``autobuild/<feature>``), the merge
    landed: return main's new commit. Anything else — main unmoved, main moved
    for some other reason, git not answering — returns ``None`` and the merge
    is reported as refused, which is then the truth.

    ``git`` is the venue (rule 89). Without one the questions are asked here,
    against ``repo_root``, exactly as before; with one they are asked wherever
    that repository's git lives.
    """
    if git is None:
        new_main = await git_rev_parse_main(repo_root)
    else:
        new_main = await git.rev_parse("main")
    pinned = (expect_main_sha or "").strip().lower()
    if not new_main or new_main.strip().lower() == pinned:
        return None
    branch = branch_to_merge(feature_id, branch)
    venue = git if git is not None else InContainerCandidateGit(repo_root)
    contains = await venue.is_ancestor(branch, new_main)
    if contains is not True:
        return None
    return new_main


async def pinned_main_in_branch(
    repo_root: Path,
    expect_main_sha: str,
    candidate_sha: str,
    *,
    git: CandidateGit | None = None,
) -> bool | None:
    """Is the pinned main commit an ancestor of the branch tip?

    The venue answers yes, no, or "could not say" (a pin it does not know, or
    git not running at all). Only a plain "no" is returned as ``False`` — that
    is the case where the merge command would land a merge commit carrying
    work the candidate never saw. ``None`` leaves the decision to the merge
    command's own pin check, which refuses a main that is not at the pin.
    """
    pin = (expect_main_sha or "").strip()
    tip = (candidate_sha or "").strip()
    if not pin or not tip:
        return None
    venue = git if git is not None else InContainerCandidateGit(repo_root)
    answer = await venue.is_ancestor(pin, tip)
    if answer is None:
        logger.warning(
            "merge-executor: git could not say whether main's pinned commit %s "
            "is in the branch at %s — the merge command's own pin check decides",
            pin[:10],
            tip[:10],
        )
    return answer


def moved_main_refusal_sentence(feature_id: str, expect_main_sha: str) -> str:
    """The plain sentence when main moved during the build (before the merge)."""
    return (
        f"{feature_id} passed its sandbox check, but main had moved since this "
        f"was built ({expect_main_sha[:10]} is not in the branch); nothing was "
        "merged and the branch is kept. Send the sentence again."
    )


def _merge_branch_of_record(
    pool: Any, build_id: str, offer: dict[str, Any] | None = None
) -> str | None:
    """The build row's recorded ``merge_branch``, or ``None`` (Part M, rule 54).

    The row is the record; the durable offer's ``merge_branch`` (what the
    card promised) is the fallback when the row cannot be read. ``None``
    means "the feature's own branch" and every reader derives
    ``autobuild/<feature id>`` from it, exactly as before the column existed.
    """
    try:
        row = pool.get_build_row(build_id)
    except Exception as exc:  # noqa: BLE001 — the offer's copy is the fallback
        logger.warning(
            "merge-executor: could not read the builds row for %s to learn its "
            "merge branch (%s: %s) — using the offer's copy",
            build_id,
            type(exc).__name__,
            exc,
        )
        row = None
    if row is not None:
        recorded = str(getattr(row, "merge_branch", None) or "").strip()
        return recorded or None
    recorded = str((offer or {}).get("merge_branch") or "").strip()
    return recorded or None


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
    merge_branch: str | None = None,
) -> MergeDeployOutcome:
    """Run candidate check -> merge -> tree check -> promote -> report for one press.

    The order inside the merge word (protect-main, rule 36):

    1. the candidate is built from the feature branch's exact tree, brought up
       in the sandbox, and the registered live checks run against it;
    2. only if every check passed does the merge land — ``guardkit autobuild
       merge`` pinned to main's commit, exactly as before;
    3. the merge's own post-merge test run stays;
    4. the merged commit's tree must be the tree that was checked (rule 37),
       else the promote is refused and nothing live changes;
    5. the promote re-tags the candidate image that was checked — never a
       rebuild — and the candidate is torn down.

    A red check at (1) means no merge and no promote: the candidate is torn
    down, the branch is kept, the repair row is filed, and the report says so
    (``candidate-refused``). The candidate's laid-out tree is removed on every
    ending. Never raises past its boundary: every result class lands as an
    honest :class:`MergeDeployOutcome`, one additive ``stage-complete``
    publish, and per-step JSON receipts under ``receipts_root()/merge-<build_id>/``.

    ``merge_branch`` is the build row's recorded journey branch (Part M, rule
    54) — a repair's ``fix/<task id>-<build8>`` — and ``None`` for a feature
    build. The branch checked, laid out, merged and reported is that branch
    when it is set, else ``autobuild/<feature id>``; the merge command is
    given ``--branch`` only when it is set, so a feature build's argv is byte
    for byte what it always was.
    """
    started = deps.clock()
    receipts_dir = deps.receipts_root_fn() / f"merge-{build_id}"
    # The branch of record for this press (Part M, rules 54 and 55).
    merge_branch = str(merge_branch or "").strip() or None
    branch = branch_to_merge(feature_id, merge_branch)
    # The subject as the thread line names it: the branch is said only when it
    # is not the feature's own, so a feature build's words are unchanged.
    named = f"{feature_id} (branch {branch})" if merge_branch is not None else feature_id
    # Advisory digest-conformance state — filled in after a landed merge,
    # read by the report step. It can add a warning line; it can never
    # block anything.
    digest_conformance: dict[str, Any] = {}
    # What the candidate check found — on every report once the check began.
    # ``ran`` says the deploy stage really drove the check (a repair row is
    # only worth filing then); ``refusal`` is the middle clause of the
    # refusal sentence, reused word for word on the repair row.
    gate: dict[str, Any] = {
        "verdict": None,
        "checks_passed": None,
        "checks_total": None,
        "failed_checks": None,
        "candidate_sha": None,
        "candidate_tree": None,
        "merged_tree": None,
        "trees_match": None,
        "ran": False,
        "refusal": None,
    }
    gate_began = False
    # One deploy run for both legs, so their runbooks and events belong together.
    deploy_run_id = str(uuid.uuid4())
    task_id = _deploy_task_id(feature_id)
    tree_path: Path | None = None
    candidate_standing = False
    # WHERE this repository's git happens (sandbox first, rule 89): inside its
    # sandbox when it has one, in this container when it has not. Chosen once,
    # used by every git operation the press makes, so they cannot disagree.
    git = git_surface_for(deps, repo, repo_root)

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
        """Is this step on the build's record as claimed or done?

        The LAST row for the step decides. A merge that refused before it
        touched anything releases its claim with a SKIPPED row (2026-09-06:
        Rich's press refused over a dirty tree and the build could never be
        pressed again, although nothing had merged), so a released step is
        free to run once more; a claimed or completed step is not.
        """
        rows = [
            s
            for s in deps.pool.read_stages(build_id)
            if s.target_identifier == target_identifier
        ]
        if not rows:
            return False
        return str(rows[-1].status or "").upper() != "SKIPPED"

    def _record_step(
        target_identifier: str, status: str, details: dict[str, Any]
    ) -> None:
        now = deps.clock()
        deps.pool.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=MERGE_REPORT_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=target_identifier,
                status=status,
                gate_mode="MANDATORY_HUMAN_APPROVAL",
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details=details,
            )
        )

    def _claim_step(target_identifier: str, details: dict[str, Any]) -> None:
        _record_step(target_identifier, "GATED", details)

    def _release_step(target_identifier: str, reason: str) -> None:
        """Give a claimed step back: nothing happened, so the build may be
        pressed again. Written as a SKIPPED row carrying the reason, after
        the claim, so the record says both that it was claimed and why it
        was let go."""
        _record_step(
            target_identifier,
            "SKIPPED",
            {"merge_step": {"released": True, "reason": reason}},
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

    def _gate_for_report() -> dict[str, Any]:
        """The ``gate_before_merge`` block: the spec's six fields plus the
        failing check names and whether the trees matched."""
        return {
            key: gate.get(key)
            for key in (
                "verdict",
                "checks_passed",
                "checks_total",
                "failed_checks",
                "candidate_sha",
                "candidate_tree",
                "merged_tree",
                "trees_match",
                "ran",
                "refusal",
            )
        }

    async def _publish_report(outcome: MergeDeployOutcome) -> MergeDeployOutcome:
        completed = deps.clock()
        conformance_warning = digest_conformance.get("warning")
        if conformance_warning:
            outcome.detail = f"{outcome.detail}\nWARNING: {conformance_warning}"
        if gate_began and outcome.gate_before_merge is None:
            outcome.gate_before_merge = _gate_for_report()
        outcome.branch = branch
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
            # Additive (Part M, rule 55): the branch this press merged, or
            # would have — truthful for a repair, the feature's own otherwise.
            branch=branch,
            # Additive, and only when there is something to say: a deploy that
            # ran nowhere special sends no field at all, so every payload that
            # was written before Docker Sandboxes existed is unchanged.
            **({"deployed_in": outcome.deployed_in} if outcome.deployed_in else {}),
            # Additive (protect-main, rule 40): what the sandbox check found
            # before the merge, once the check began.
            **(
                {"gate_before_merge": outcome.gate_before_merge}
                if outcome.gate_before_merge is not None
                else {}
            ),
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
        # A PRESS THAT FOUND THE CODE WRONG BECOMES A REPAIR JOB (conductor
        # rewire rule 1). The three red endings are the ones where the merge
        # itself landed and what came after it went red — the live checks,
        # the deploy, or the revert — and, since protect-main, the branch
        # that failed its sandbox check before the merge. A refused merge is
        # not one of them: nothing changed, so there is nothing to repair.
        # The dry run is not one either: it changed nothing on purpose.
        if not dry_run and (
            outcome.result in RED_MERGE_ENDINGS or outcome.result == "candidate-refused"
        ):
            _mint_repair_row(deps.pool, build_id, outcome, feature_id=feature_id)
        return outcome

    async def _dispatch(leg: str, **extra: Any) -> Any:
        return await deps.deploy_dispatcher(
            repo=repo,
            repo_root=repo_root,
            feature_id=feature_id,
            build_id=build_id,
            correlation_id=correlation_id,
            decided_by=decided_by,
            dry_run=dry_run,
            leg=leg,
            deploy_run_id=deploy_run_id,
            task_id=task_id,
            **extra,
        )

    def _refused_before_merge(
        sentence: str, *, failed_step: str = "candidate"
    ) -> MergeDeployOutcome:
        return MergeDeployOutcome(
            result="candidate-refused",
            status="FAILED",
            failed_step=failed_step,
            detail=sentence,
            verdict=gate.get("verdict"),
            checks_passed=gate.get("checks_passed"),
            checks_total=gate.get("checks_total"),
            # Where the check ran, when it ran: the sandbox, or nowhere named.
            deployed_in=deployed_in_for(repo_root) if gate.get("ran") else None,
            gate_before_merge=_gate_for_report(),
        )

    def _could_not_check(why: str) -> MergeDeployOutcome:
        gate["refusal"] = f"could not be checked: {why}"
        return _refused_before_merge(
            f"{feature_id} could not be checked in the sandbox before merging: "
            f"{why}; nothing was merged and the branch is kept."
        )

    async def _tear_down_candidate() -> None:
        """Best-effort: the candidate that a stopped run left standing."""
        nonlocal candidate_standing
        try:
            result = await _dispatch("candidate_down")
        except Exception as exc:  # noqa: BLE001 — cleanup never costs a report
            logger.warning(
                "merge-executor: tearing the candidate for %s down raised "
                "(%s: %s) — the -cand project may still be up",
                feature_id,
                type(exc).__name__,
                exc,
            )
            return
        outcome_word = getattr(result, "outcome", None)
        if result is not None and outcome_word != "complete":
            logger.warning(
                "merge-executor: the candidate for %s was not torn down "
                "(%s) — the -cand project may still be up",
                feature_id,
                outcome_word,
            )
            return
        candidate_standing = False

    async def _cleanup() -> None:
        """On every ending: the candidate down if still standing, the tree gone."""
        nonlocal tree_path
        if candidate_standing:
            await _tear_down_candidate()
        removed: bool | None = None
        if tree_path is not None:
            removed = await git.remove_candidate_tree(feature_id, str(tree_path))
            if removed:
                logger.info(
                    "merge-executor: removed the candidate tree for %s at %s",
                    feature_id,
                    tree_path,
                )
        _write_receipt(
            "merge_deploy_cleanup.json",
            {
                "step": "cleanup",
                "dry_run": dry_run,
                "candidate_torn_down": not candidate_standing,
                "tree_path": str(tree_path) if tree_path else None,
                "tree_removed": removed,
            },
        )

    def _candidate_refusal(
        checked: Any, summary: dict[str, Any]
    ) -> MergeDeployOutcome:
        """The candidate came up and failed, or never came up: the sentence."""
        reason = str((getattr(checked, "detail", None) or {}).get("reason") or "")
        verdict = summary.get("verdict")
        total = summary.get("checks_total")
        passed = summary.get("checks_passed")
        names = summary.get("failed_checks") or []
        if reason == "candidate_deploy_failed" or (
            verdict is None and reason != "candidate_failed"
        ):
            stopped_at = (
                summary.get("failed_step")
                or getattr(checked, "failed_step", None)
                or "the candidate deploy"
            )
            gate["refusal"] = (
                f"could not be started (the candidate deploy stopped at {stopped_at})"
            )
            sentence = candidate_refused_sentence(feature_id, detail=gate["refusal"])
            return _refused_before_merge(sentence)
        if names and isinstance(total, int) and isinstance(passed, int):
            gate["refusal"] = None
            sentence = candidate_refused_sentence(
                feature_id,
                checks_failed=total - passed,
                checks_total=total,
                failing_checks=list(names),
            )
            return _refused_before_merge(sentence)
        gate["refusal"] = (
            f"failed its checks (the check verdict was {verdict or 'missing'}; "
            "which of the checks failed was not reported)"
        )
        return _refused_before_merge(
            candidate_refused_sentence(feature_id, detail=gate["refusal"])
        )

    async def _press() -> MergeDeployOutcome:
        nonlocal tree_path, candidate_standing, gate_began

        if _has_step(MERGE_STEP_MERGE_TARGET_IDENTIFIER):
            logger.error(
                "merge-executor: %s already has a merge step on record — refusing "
                "to run the merge twice",
                build_id,
            )
            return MergeDeployOutcome(
                result="merge-refused",
                status="FAILED",
                failed_step="merge",
                detail=(
                    "a merge step is already on record for this build — "
                    "refusing to run it twice"
                ),
            )

        # A press replayed on a build that already merged answers the
        # double-merge refusal above and nothing else — that guard comes
        # FIRST so the answer to "did this already happen?" never changes
        # because of anything this lane added (L3b's coach, 2026-09-08).

        # ------------------------------------------------------------------
        # STEP candidate: the branch is checked in the sandbox BEFORE the merge
        # ------------------------------------------------------------------
        gate_began = True
        candidate_sha = await git.rev_parse(branch)
        if not candidate_sha:
            return _could_not_check(
                f"the branch {branch} was not found {git_venue(git, repo_root)}"
            )
        gate["candidate_sha"] = candidate_sha
        gate["candidate_tree"] = await git.rev_parse(f"{candidate_sha}^{{tree}}")
        excluded_now: bool | None = None
        try:
            excluded_now = await git.ensure_candidate_trees_excluded()
            laid_out = await git.materialise_candidate_tree(
                feature_id, candidate_sha
            )
            tree_path = Path(laid_out.path)
            # A venue that keeps the trees excluded as part of laying one out
            # says so in its answer rather than in a call of its own, and one
            # that reads the tree id while it is there saves the second ask.
            if excluded_now is None:
                excluded_now = laid_out.exclude_written
            if not gate["candidate_tree"]:
                gate["candidate_tree"] = laid_out.tree
        except CandidateTreeError as exc:
            _write_receipt(
                "merge_deploy_candidate.json",
                {
                    "step": "candidate",
                    "dry_run": dry_run,
                    "branch": branch,
                    "candidate_sha": candidate_sha,
                    "candidate_tree": gate["candidate_tree"],
                    "tree_path": None,
                    "error": str(exc),
                },
            )
            return _could_not_check(
                f"its tree could not be laid out for the check ({exc})"
            )

        try:
            checked = await _dispatch("candidate_check", candidate_cwd=str(tree_path))
        except Exception as exc:  # noqa: BLE001 — the sidecar-surface ValueError crack
            _write_receipt(
                "merge_deploy_candidate.json",
                {
                    "step": "candidate",
                    "dry_run": dry_run,
                    "candidate_sha": candidate_sha,
                    "candidate_tree": gate["candidate_tree"],
                    "tree_path": str(tree_path),
                    "error": str(exc),
                },
            )
            return _could_not_check(f"the candidate check raised ({exc})")

        c_outcome = getattr(checked, "outcome", None)
        c_detail = getattr(checked, "detail", None) or {}
        summary = dict(c_detail.get("gate_summary") or {})
        gate["verdict"] = summary.get("verdict")
        gate["checks_passed"] = summary.get("checks_passed")
        gate["checks_total"] = summary.get("checks_total")
        gate["failed_checks"] = summary.get("failed_checks")
        reason = str(c_detail.get("reason") or "")
        gate["ran"] = checked is not None and reason != "no_candidate_section"
        _write_receipt(
            "merge_deploy_candidate.json",
            {
                "step": "candidate",
                "dry_run": dry_run,
                "branch": branch,
                "candidate_sha": candidate_sha,
                "candidate_tree": gate["candidate_tree"],
                "tree_path": str(tree_path),
                "exclude_written_now": excluded_now,
                "deploy_run_id": deploy_run_id,
                "task_id": task_id,
                "outcome": c_outcome,
                "verdict": summary.get("verdict"),
                "gate_summary": summary,
                "failed_step": getattr(checked, "failed_step", None),
                "reason": reason or None,
                "events": list(getattr(checked, "events", ()) or ()),
            },
        )
        if not dry_run and checked is not None:
            _record_step(
                MERGE_STEP_CANDIDATE_TARGET_IDENTIFIER,
                "PASSED" if c_outcome == "complete" else "FAILED",
                {
                    "candidate_step": {
                        "candidate_sha": candidate_sha,
                        "candidate_tree": gate["candidate_tree"],
                        "verdict": summary.get("verdict"),
                        "checks_passed": summary.get("checks_passed"),
                        "checks_total": summary.get("checks_total"),
                        "failed_checks": summary.get("failed_checks"),
                        "deploy_run_id": deploy_run_id,
                    }
                },
            )
        if checked is None:
            return _could_not_check(
                "the deploy stage is disabled (deploy.enabled=false)"
            )
        if reason == "no_candidate_section":
            return _could_not_check(
                "the repository's deploy profile has no candidate section"
            )
        if c_outcome != "complete":
            return _candidate_refusal(checked, summary)
        candidate_standing = str(c_detail.get("candidate") or "standing") == "standing"
        prior_events = tuple(getattr(checked, "events", ()) or ())

        # ------------------------------------------------------------------
        # Has main moved since this was built? Asked BEFORE the merge step is
        # claimed, in a dry run too (it reads, it writes nothing), because the
        # pin is read after the build and cannot see a main that moved during
        # it. A "no" merges nothing: the candidate comes down on the way out.
        # ------------------------------------------------------------------
        main_in_branch = await pinned_main_in_branch(
            repo_root, expect_main_sha, candidate_sha, git=git
        )
        if main_in_branch is False:
            sentence = moved_main_refusal_sentence(feature_id, expect_main_sha)
            _write_receipt(
                "merge_deploy_merge.json",
                {
                    "step": "merge",
                    "dry_run": dry_run,
                    "refusal": sentence,
                    "expect_main_sha": expect_main_sha,
                    "candidate_sha": candidate_sha,
                    "pinned_main_in_branch": False,
                    "skipped": "main moved during the build — the merge was not run",
                },
            )
            logger.warning(
                "merge-executor: %s passed its sandbox check but main had moved "
                "since it was built (%s is not in the branch) — nothing was "
                "merged; the candidate comes down and the sentence must be sent again",
                feature_id,
                expect_main_sha[:10],
            )
            return MergeDeployOutcome(
                result="merge-refused",
                status="FAILED",
                failed_step="merge",
                detail=sentence,
            )

        # ------------------------------------------------------------------
        # STEP merge + verify (through the frozen guardkit boundary)
        # ------------------------------------------------------------------
        if dry_run:
            # A dry run merges NOTHING and leaves NO durable step rows — a claimed
            # step would make a later real press refuse "already on record". It
            # proves the plumbing end to end and exercises the deploy stage's own
            # dry mode; the receipts on disk are its only record.
            _write_receipt(
                "merge_deploy_merge.json",
                {
                    "step": "merge",
                    "dry_run": True,
                    "branch": branch,
                    "skipped": (
                        "dry run — nothing merged; a real press would merge "
                        f"{branch} into main at {expect_main_sha}"
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
                        "candidate_sha": candidate_sha,
                        "branch": branch,
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
            # Part M, rule 54: the merge command is told the branch ONLY when
            # the build row recorded one (a repair's own journey branch).
            # Without the flag guardkit derives autobuild/<feature id> exactly
            # as it always has, so a feature build's argv is byte-identical.
            if merge_branch is not None:
                args += ["--branch", merge_branch]
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
                _last_sentence(stderr)
                or _last_sentence(tail)
                or f"the merge command did not succeed (status={result_status})"
            )
            if not merged_in_report:
                generic: str | None = None
                if result_status != "success":
                    generic = (
                        f"the merge command did not succeed (status={result_status})"
                        + (
                            f": {_last_sentence(stderr)}"
                            if stderr
                            else (f": {_last_sentence(tail)}" if tail else "")
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
                        # A conflict report carries no refusal sentence of its
                        # own, only the files; say those in words rather than
                        # the last 400 characters of the JSON (seam coach,
                        # 2026-09-06 — a slice cut mid-word on Rich's card).
                        refusal = (
                            _conflict_sentence(report)
                            or _report_refusal(report)
                            or generic
                        )
                else:
                    refusal = generic

            # THE MERGE MAY HAVE LANDED ANYWAY. A command that was killed, or that
            # died before it could print its report, leaves no answer at all — but
            # git knows. Ask git before calling a landed merge refused.
            landed_sha: str | None = None
            if refusal and report is None and result_status != "success":
                landed_sha = await merged_after_all_sha(
                    repo_root, feature_id, expect_main_sha, branch=branch, git=git
                )

            _write_receipt(
                "merge_deploy_merge.json",
                {
                    "step": "merge",
                    "status": result_status,
                    "exit_code": getattr(result, "exit_code", None),
                    "branch": branch,
                    "refusal": refusal,
                    "report": report,
                    "landed_sha": landed_sha,
                    "stdout_tail": (getattr(result, "stdout_tail", "") or "")[-4000:],
                    "baseline_file": str(baseline_path) if baseline_path else None,
                },
            )
            if refusal and landed_sha:
                return MergeDeployOutcome(
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
            if refusal:
                # Nothing landed (main did not move), so the merge step goes
                # back: the next press may run it again once the cause is gone.
                # The candidate that passed its check is torn down on the way
                # out; nothing is promoted.
                _release_step(MERGE_STEP_MERGE_TARGET_IDENTIFIER, refusal)
                return MergeDeployOutcome(
                    result="merge-refused",
                    status="FAILED",
                    failed_step="merge",
                    detail=refusal,
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
                return MergeDeployOutcome(
                    result="merged-verify-failed",
                    status="FAILED",
                    merged_sha=merged_sha,
                    failed_step="verify",
                    detail=detail,
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                    verify_status="unverified" if could_not_run else "failed",
                )

            # --------------------------------------------------------------
            # STEP tree check: what landed must be what was checked (rule 37)
            # --------------------------------------------------------------
            merged_tree = (
                await git.rev_parse(f"{merged_sha}^{{tree}}") if merged_sha else None
            )
            gate["merged_tree"] = merged_tree
            gate["trees_match"] = bool(
                merged_tree and gate["candidate_tree"] and merged_tree == gate["candidate_tree"]
            )
            _write_receipt(
                "merge_deploy_tree_check.json",
                {
                    "step": "tree-check",
                    "merged_sha": merged_sha,
                    "merged_tree": merged_tree,
                    "candidate_sha": candidate_sha,
                    "candidate_tree": gate["candidate_tree"],
                    "trees_match": gate["trees_match"],
                },
            )
            if not gate["trees_match"]:
                if not merged_sha:
                    why = "the merge report names no merged commit, so its tree could not be compared with the tree that was checked"
                elif merged_tree is None:
                    why = (
                        f"the merged commit's tree could not be read "
                        f"(git rev-parse {merged_sha[:10]}^{{tree}} gave no answer)"
                    )
                else:
                    why = (
                        f"the merged commit's tree ({merged_tree[:10]}) is not the "
                        f"tree that was checked in the sandbox "
                        f"({str(gate['candidate_tree'] or '')[:10]}): main had "
                        "moved since this was built in a way the pinned main "
                        "commit did not catch"
                    )
                return MergeDeployOutcome(
                    result="merged-deploy-failed",
                    status="FAILED",
                    merged_sha=merged_sha,
                    failed_step="promote",
                    detail=(
                        f"{feature_id} merged ({(merged_sha or '')[:10]}), but {why}. "
                        "The promote was refused and nothing live changed; the "
                        "candidate was torn down. Send the sentence again."
                    ),
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                    gate_before_merge=_gate_for_report(),
                )

        # ------------------------------------------------------------------
        # STEP promote (the image that was checked, re-tagged — never rebuilt)
        # ------------------------------------------------------------------
        if not dry_run:
            if _has_step(MERGE_STEP_DEPLOY_TARGET_IDENTIFIER):
                logger.error(
                    "merge-executor: %s already has a deploy step on record — "
                    "refusing to dispatch the promote twice",
                    build_id,
                )
                return MergeDeployOutcome(
                    result="merged-deploy-failed",
                    status="FAILED",
                    merged_sha=merged_sha,
                    failed_step="deploy",
                    detail=(
                        "the merge landed but a deploy step is already on "
                        "record — refusing to dispatch the promote twice"
                    ),
                )
            _claim_step(
                MERGE_STEP_DEPLOY_TARGET_IDENTIFIER,
                {
                    "deploy_step": {
                        "merged_sha": merged_sha,
                        "dry_run": dry_run,
                        "deploy_run_id": deploy_run_id,
                    }
                },
            )

        try:
            deploy_result = await _dispatch("promote", prior_events=prior_events)
        except Exception as exc:  # noqa: BLE001 — the sidecar-surface ValueError crack
            _write_receipt(
                "merge_deploy_deploy.json",
                {"step": "deploy", "error": str(exc), "dry_run": dry_run},
            )
            return MergeDeployOutcome(
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
                    + f"the promote dispatch raised: {exc}"
                ),
                checks_passed=checks_passed,
                checks_total=checks_total,
            )

        d_outcome = getattr(deploy_result, "outcome", None)
        verdict = getattr(deploy_result, "verdict", None)
        d_detail = getattr(deploy_result, "detail", None) or {}
        # The promote leg tears the candidate down itself (unless the profile
        # keeps it); only a promote that stopped short leaves it standing.
        candidate_standing = str(d_detail.get("candidate") or "torn-down") == "standing"
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
            return MergeDeployOutcome(
                result="merged-deploy-failed",
                status="FAILED",
                merged_sha=merged_sha,
                failed_step="deploy",
                detail=(
                    "the merge landed but the deploy stage is disabled "
                    "(deploy.enabled=false) — nothing was promoted"
                ),
                checks_passed=checks_passed,
                checks_total=checks_total,
            )
        if d_outcome == "complete":
            deployed_in = deployed_in_for(repo_root)
            checks = (
                f" — checks {checks_passed}/{checks_total}"
                if checks_passed is not None and checks_total is not None
                else ""
            )
            sandbox_checks = (
                f"checked in the sandbox ({gate['checks_passed']} of "
                f"{gate['checks_total']}), "
                if isinstance(gate.get("checks_passed"), int)
                and isinstance(gate.get("checks_total"), int)
                else "checked in the sandbox, "
            )
            return MergeDeployOutcome(
                result="merged-and-running",
                status="PASSED",
                merged_sha=merged_sha,
                verdict=str(verdict) if verdict is not None else None,
                detail=(
                    f"{named} {sandbox_checks}merged and running{checks}. "
                    "Rollback is one command; the branch is kept."
                ),
                checks_passed=checks_passed,
                checks_total=checks_total,
                deployed_in=deployed_in,
            )
        if d_outcome == "reverted":
            return MergeDeployOutcome(
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
                deployed_in=deployed_in_for(repo_root),
            )
        return MergeDeployOutcome(
            result="merged-deploy-failed",
            status="FAILED",
            merged_sha=merged_sha,
            failed_step="deploy",
            verdict=str(verdict) if verdict is not None else None,
            detail=(
                (
                    "dry run — nothing merged; the promote ended "
                    if dry_run
                    else f"{feature_id} merged, but the promote ended "
                )
                + f"{d_outcome or 'without an outcome'} — nothing further was "
                "touched"
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
            deployed_in=deployed_in_for(repo_root),
        )

    try:
        outcome = await _press()
    finally:
        # Every ending, the normal ones and a crash alike: the candidate down
        # if it is still standing, and its laid-out tree removed — BEFORE the
        # report goes out, so the report never says "torn down" ahead of time.
        await _cleanup()
    return await _publish_report(outcome)


# ---------------------------------------------------------------------------
# The in-daemon deploy dispatcher (mirrors cli/_deploy_run.py exactly)
# ---------------------------------------------------------------------------


def build_in_daemon_deploy_dispatcher(
    *, config: Any, nats_client: Any, db_path: Any
) -> Callable[..., Awaitable[Any]]:
    """Bind the production deploy dispatch for the executor's deploy legs.

    Mirrors ``forge.cli._deploy_run`` but in-daemon: profile + live-gate
    invoker from the target repo, runbook DDL ensured idempotently (the C4
    ``no such table: runbooks`` lesson), and ``execution_surface`` forced to
    ``sidecar`` on a config COPY — the daemon container has no docker, and
    ``_resolve_script_runner`` reads only the passed config. The executor
    dispatches one LEG at a time (protect-main, rule 39): ``candidate_check``
    with the branch's laid-out tree as the candidate's working directory,
    then — after the merge — ``promote``, or ``candidate_down`` when the run
    stopped between them; ``deploy`` (the default) is the whole stage in one
    call, as before. ``deploy_run_id`` and ``task_id`` are shared by the legs
    of one press so their runbooks and events belong to one deploy run. The
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
        leg: str = "deploy",
        candidate_cwd: str | None = None,
        prior_events: tuple[str, ...] = (),
        deploy_run_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        from forge.adapters.nats.deploy_publisher import DeployPublisher
        from forge.adapters.nats.runbook_publisher import RunbookPublisher
        from forge.adapters.sqlite.connect import connect_writer
        from forge.config.sandboxes import sandbox_for
        from forge.deploy.composition import dispatch_deploy_stage
        from forge.deploy.live_gate import (
            RepoDriverLiveGateInvoker,
            SidecarLiveGateInvoker,
        )
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
        # SANDBOX FIRST (2026-09-07, rule 85). A repository that has a sandbox
        # is deployed and gated INSIDE it: the stage's scripts go to the deploy
        # sidecar in there, and the live gate's driver goes with them, because
        # the candidate's port is on that sandbox's own loopback and a driver
        # run in the forge container could reach nothing. A repository with no
        # sandbox — every repository until an operator fills the map in — takes
        # exactly the path it took before this lane.
        sandbox = sandbox_for(config, repo)
        spec = profile.live_gate
        invoker = None
        if spec is not None and sandbox is not None:
            invoker = SidecarLiveGateInvoker(
                base_url=str(sandbox.sidecar_url),
                repo=repo,
                repo_path=repo_root,
                driver_argv=list(spec.driver),
                timeout_seconds=spec.timeout_seconds,
                extra_env=dict(spec.env),
            )
        elif spec is not None:
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
            deploy_run_id=deploy_run_id or str(uuid.uuid4()),
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_publisher,
            live_gate_invoker=invoker,
            deploy_record_root=str(repo_root / deploy_cfg.deploy_record_dir),
            dry_run=dry_run,
            target_repo=repo,
            target_repo_root=str(repo_root),
            sandbox=sandbox,
            feature=feature_id,
            feat_id=feature_id,
            # Distinct per run AND TASK-shaped — DeployQueuedPayload validates
            # ^TASK-[A-Z0-9]{3,12}$ (the first dry fire caught this live);
            # the time suffix also keeps the date-granular F7 record filename
            # distinct across same-day runs.
            task_id=task_id or _deploy_task_id(feature_id),
            deployer=f"merge-word:{decided_by}",
            leg=leg,
            candidate_cwd=candidate_cwd,
            prior_events=tuple(prior_events),
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
        merge_branch = _merge_branch_of_record(self._deps.pool, build_id, offer)
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
                merge_branch=merge_branch,
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
        merge_branch: str | None = None,
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
                    merge_branch=merge_branch,
                )
            except Exception as exc:  # noqa: BLE001 — the task must not die silent
                logger.error(
                    "merge-executor: executor raised (%s) for build %s — see "
                    "the receipts under merge-%s",
                    exc,
                    build_id,
                    build_id,
                )
