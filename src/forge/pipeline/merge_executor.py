"""The merge-and-deploy executor — code, never an AI session, runs the press.

Make-merge-work build spec (2026-08-24). Two halves:

* :class:`MergeApprovalConsumer` — a CORE NATS subscription (never JetStream;
  the AGENTS stream is no_ack) on ``agents.approval.forge.merge-*.response``.
  It refuses everything except: a durable pending offer whose ``request_id``
  matches, a ``decided_by`` that string-equals the deployment's expected
  approver VERBATIM, a matching correlation, and no decision yet on record
  (the durable decision row is written FIRST, so a restart can never
  double-run).
* :func:`execute_merge_deploy` — the executor coroutine. STEP join: the
  recorded target branch is fetched from the remote named ``origin`` (that is
  G), a working folder of its own is made at G, and the build system's merge
  runs in THERE, through the frozen guardkit subprocess boundary, onto
  ``factory-integration/<feature>`` pinned to G; its ``--no-ff`` stays, so the
  joined commit J is a new commit of exactly G and the build's tip. STEP
  merge-checks: the build system's own post-merge checks run on J inside that
  same command. STEP candidate check: J's exact tree is laid out inside the
  checkout and the deploy stage's candidate leg builds it, brings it up and
  runs the registered live checks against it — on J and only on J. STEP
  report as one additive ``pipeline.stage-complete.{feature_id}`` publish.
  Per-step durable receipts land under ``receipts_root()/merge-<build_id>/``
  and a stage row is written BEFORE each irreversible act, probed on restart.

THE PRESS STOPS AT "PUBLISHED" (the one-true-copy design, 2026-09-21, and its
publisher stage, 2026-09-22). With publication switched off — which is the
default, and what every forge does until section G's five conditions hold —
nothing is sent to the remote and nothing is deployed: the result is
"publication pending" and the sentence says why publication is off. With it
on, the press asks the PUBLISHER — a separate process holding the one
credential that can write to a remote — to send the joined commit to the
recorded target branch, reads the answer, and stops at "published, deployment
pending". It never deploys and never says anything is running: the deploy,
the identity that cannot be reused and the deployment lock are the stage
after this one, and the third result name, "merged into the remote and
running", stays unreachable and pinned so. None of the three names a hosting
provider: this module knows only "the remote named origin".

THE SEND, AND THE THREE ATTEMPTS. The coordinator writes "about to send" with
the attempt before asking, and "done send" with the answer after. On
{published, contains_j} it says "published, deployment pending". On the ONE
refusal worth trying again — the remote moved under the send — the join is
set aside under the name its own attempt gave it, the branch is fetched
afresh for a new G, a new join is made on a name of its own and BOTH kinds of
check run again on it; at most three attempts, then "publication pending"
with the reason. Every other refusal, a missing credential and a publisher
that cannot be reached all stop at once with "publication pending": nothing
was sent. PICKING UP a send reads the remote FIRST and asks whether the
target branch CONTAINS J, never whether it IS J.

THE PROJECT'S MAIN COPY IS NEVER TOUCHED. It is not switched, not reset and
not merged into: everything happens in the worktree made at G, and the join
lands on a branch of the factory's own. What used to protect main — the
candidate check in front of the merge (Part J, 2026-09-07), the pin to main's
commit, the ancestry guard and the tree comparison after the merge — is
subsumed by this shape: there is one tree, J's, and the check and the
comparison are both about it.

THE PUBLICATION RECORD carries the press across a restart. One row per build:
who gave the merge word and when, the recorded target branch, G, J, and every
step written as an "about to" (attempt number and exact inputs) before acting
and a "done" (the result) after. A lease and a turn number keep one worker on
it; every write is conditional on the stored turn equalling the writer's, in
the same statement, and a write that changes no row means the worker was
replaced, so it stops without tidying up. Builds pressed before the record
existed read as "not recorded".

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

AND BECAUSE IT LANDED IN THERE, the words after a green press say so and give
the one command that brings the merge to the operator's checkout (sandbox
first, rule 79): "This merge landed in the factory's own copy of the
repository, inside the sandbox <name> — not in your checkout at <path>. To
bring it to your checkout, run: git -C <path> fetch sandbox-<name> main && git
-C <path> merge --ff-only sandbox-<name>/main". The same words ride the report
as ``sandbox_merge`` (the sandbox, the remote, the checkout, the command and
the sentence), so a thread can print them verbatim rather than compose words of
its own, and the merge's own receipt records them beside the merge. A
repository without a sandbox says and records nothing extra: its sentence, its
report and its receipts are byte for byte what they were.

THE PRESS WRITES THE BUILD'S ENDING (2026-09-10), because it is the only
thing that knows how the merge ended. Until now it wrote its receipts, its
report and its stage rows and never touched the build's own row, and the
conductor's close-out deliberately leaves the merge-card path alone, so
nothing closed the row at all: a build that merged and was promoted still
said RUNNING hours later, and so did every honestly refused one, until a
person ran ``forge cancel`` and wrote CANCELLED over a journey that had
merged. Now every ending of the press closes the row through the lifecycle's
own transition seam: a press that joined and checked closes it COMPLETE, and
every other ending — refused at the join, at the branch, at a conflict, or red
at either kind of check — closes it FAILED
carrying the very sentence the report and the card carry. A row that
something else has already closed is left exactly as it is, so the write is
safe to repeat and a routine feature build, whose row the live build feed
closes COMPLETE before the card is ever offered, is untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
from forge.deploy.stage import assertion_in_words
from forge.lifecycle.persistence import StageLogEntry
from forge.pipeline.fix_row_producer import candidate_refused_sentence
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_STAGE_LABEL,
    MERGE_OFFER_TARGET_IDENTIFIER,
    branch_to_merge,
)
from forge.pipeline.digest_conformance import run_digest_conformance
from forge.pipeline.merge_join import (
    integration_branch,
    join_inputs,
    look_at_a_join,
    look_at_the_leftover_join,
    make_the_working_folder,
    target_branch_now,
    working_folder_path,
)
from forge.pipeline.deployment_identity import (
    declared_identity,
    fixed_identity,
    identity_reported_by,
    the_identities_differ,
)
from forge.pipeline.deployment_lock import (
    DeploymentLockStore,
    deployment_target_name,
)
from forge.pipeline.only_forwards import what_to_do_about_j
from forge.pipeline.publication_record import (
    LINE_DONE,
    RESULT_MERGED_AND_RUNNING,
    RESULT_PUBLICATION_PENDING,
    RESULT_PUBLISHED_DEPLOYMENT_PENDING,
    STEP_CANDIDATE_CHECK,
    STEP_DEPLOY,
    STEP_JOIN,
    STEP_MERGE_CHECKS,
    STEP_SEND,
    PublicationRecordStore,
)
from forge.pipeline.publication_switch import (
    publication_is_switched_on,
    why_publication_is_off,
)
from forge.pipeline.publisher_client import ask_the_publisher, the_remote_moved
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
    "MERGE_SEND_ATTEMPTS_DEFAULT",
    "MERGE_VERIFY_TIMEOUT_DEFAULT_SECONDS",
    "MERGE_WALL_CAP_SECONDS",
    "MERGE_WALL_MERGE_ALLOWANCE_SECONDS",
    "MergeApprovalConsumer",
    "RESULT_WORD_MERGED_AND_RUNNING",
    "RESULT_WORD_PUBLICATION_PENDING",
    "RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING",
    "MergeDeployOutcome",
    "MergeExecutorDeps",
    "RED_MERGE_ENDINGS",
    "build_in_daemon_deploy_dispatcher",
    "close_build_row",
    "execute_merge_deploy",
    "deployed_in_for",
    "merge_wall_seconds",
    "sandbox_merge_words",
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


#: How many times one merge word may join and send before it stops. Three by
#: the design (first revision, item 2: "at most three attempts, spaced out;
#: after that it stays publication pending and says so").
MERGE_SEND_ATTEMPTS_DEFAULT: int = 3


def _how_many_attempts(config: Any) -> int:
    """``publication.send_attempts``, falling back plainly to three."""
    value = getattr(getattr(config, "publication", None), "send_attempts", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return MERGE_SEND_ATTEMPTS_DEFAULT
    return value


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

#: THE THREE RESULT WORDS of the design's second revision, section B, as the
#: report and the card carry them. Only the first is reachable in this
#: version, because the publisher does not exist; the other two are named now
#: so that the stages that make them reachable do not invent words of their
#: own. The plain sentences they stand for are in
#: :mod:`forge.pipeline.publication_record`.
#:
#: NONE OF THE THREE NAMES A HOSTING PROVIDER. The third used to read
#: ``merged-into-github-and-running``, which put one provider's name into
#: central orchestration; the factory knows only "the remote named origin",
#: and a project whose remote is somewhere else must not be told it was
#: merged into a service it has never heard of. Renamed 22 September 2026
#: together with its plain sentence in
#: :mod:`forge.pipeline.publication_record`. Still unreachable, and pinned so.
RESULT_WORD_PUBLICATION_PENDING: str = "publication-pending"
RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING: str = "published-deployment-pending"
RESULT_WORD_MERGED_AND_RUNNING: str = "merged-into-the-remote-and-running"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_receipts_root() -> Path:
    return receipts_root()


@dataclass
class MergeDeployOutcome:
    """The executor's one-line truth, mirrored onto the report payload."""

    # publication-pending | merged-verify-failed | merge-refused |
    # candidate-refused | rejected. The two words after "publication-pending"
    # in the design's vocabulary — published-deployment-pending and
    # merged-into-the-remote-and-running — are defined but NOT reachable while
    # publication is switched off, and a test pins that.
    result: str
    status: str  # PASSED | FAILED | SKIPPED | GATED (a reused join whose checks have not all run: not a pass, not a failure, the build row is left open)
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
    #: What to do after a merge that landed in a sandbox's own clone
    #: (sandbox first, rule 79): ``sandbox`` (its name), ``remote``,
    #: ``checkout``, ``fetch_command`` and the plain ``sentence`` that says
    #: both. Set only on a green press of a repository that has a sandbox;
    #: ``None`` for every repository that has none, whose report carries no
    #: such field at all.
    sandbox_merge: dict[str, Any] | None = None


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
    #: Where the publication record lives — ``() -> PublicationRecordStore |
    #: None``. Left unset, the press takes the ledger's own connection off
    #: ``pool.connection``; a press driven against a persistence facade that
    #: has none runs with no record at all and says so in the log, which is
    #: the same fact as a build whose record reads "not recorded".
    publication_store: Callable[[], Any] | None = None
    #: HOW THE SEND IS ASKED FOR — ``async (config, request) -> answer``.
    #: Left unset it is :func:`~forge.pipeline.publisher_client.ask_the_publisher`,
    #: which posts to the address in the settings. The coordinator never holds
    #: a credential: the request carries a project, a build, a turn number, a
    #: commit and a branch, and the answer carries whether the remote's branch
    #: now contains that commit.
    publisher: Callable[..., Awaitable[dict[str, Any]]] | None = None
    #: WHERE THE DEPLOYMENT LOCK LIVES — ``() -> DeploymentLockStore | None``
    #: (the design's F and I). Left unset, the press takes the ledger's own
    #: connection off ``pool.connection``. A press with no lock store DOES NOT
    #: DEPLOY: it stops at "published, deployment pending" and says why,
    #: because a deploy with nothing holding the target is the very thing
    #: sections F, H, I and J exist to prevent.
    deployment_lock: Callable[[], Any] | None = None
    #: WHICH DEPLOYMENT TARGET THIS PROJECT HAS, and how it wants the identity
    #: handed over — ``(repo, repo_root) -> (target, IdentityDeclaration)``.
    #: Left unset it is read from the project's own deploy profile, which is
    #: where a project declares what it deploys and what an identity is for
    #: it. Central code never invents either.
    deployment_target: Callable[[str, Path], Any] | None = None
    #: WHAT SOMEBODY WHO LOOKED AT THE MACHINE REPORTS — the stand-in for the
    #: three conditions of section G that no settings file can establish
    #: (:class:`~forge.pipeline.publication_activation.WhatTheMachineSays`).
    #: Left unset, those three questions answer "nobody has looked", the
    #: activation check refuses, and publication stays off. That is where
    #: publication stands today, and it is the safe side.
    what_the_machine_says: Any = None


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


def sandbox_merge_words(
    config: Any, repo: str, repo_root: Path | str
) -> dict[str, Any] | None:
    """What to say after a merge that landed in a sandbox's own clone (rule 79).

    A repository whose factory lives in its sandbox is merged in there, on the
    factory's own copy of the repository — so the operator's checkout does not
    have the merge until he fetches it. This composes, once, the plain sentence
    that says that and the exact command that brings it over, from the two
    facts the settings already carry: the sandbox's name
    (``planning.sandboxes[repo].name``, which is also the name of the git
    remote ``sbx`` leaves on the host) and the checkout's path
    (``planning.target_repo_paths[repo]``, which is the ``repo_root`` every
    caller of the executor passes).

    Returns ``None`` for a repository with no sandbox — which is every
    repository until an operator gives one a sandbox — so nothing about that
    repository's words or receipts changes by a byte. Never raises: this only
    decides what a report says, and a settings object of an unexpected shape
    must not be the thing that fails a merge.
    """
    try:
        from forge.config.sandboxes import sandbox_for

        entry = sandbox_for(config, repo)
    except Exception:  # noqa: BLE001 — words on a report, never a failure
        return None
    if entry is None:
        return None
    name = getattr(entry, "name", None)
    if name is None and isinstance(entry, dict):
        name = entry.get("name")
    name = str(name or "").strip()
    if not name:
        return None
    checkout = str(repo_root)
    remote = f"sandbox-{name}"
    command = (
        f"git -C {checkout} fetch {remote} main && "
        f"git -C {checkout} merge --ff-only {remote}/main"
    )
    sentence = (
        "This merge landed in the factory's own copy of the repository, inside "
        f"the sandbox {name} — not in your checkout at {checkout}. To bring it "
        f"to your checkout, run: {command}"
    )
    return {
        "sandbox": name,
        "remote": remote,
        "checkout": checkout,
        "fetch_command": command,
        "sentence": sentence,
    }


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


#: WHAT THE GATE SAW, ON THE SENTENCE A PERSON READS (2026-09-12). A refusal
#: that costs a merge has to say what it saw, but the owner's sentence stays
#: one or two lines: it names the check and the FIRST failing assertion, and
#: says how many more are in the report.
MAX_FAILED_ASSERTIONS_IN_THE_SENTENCE: int = 1
#: One reported value (what was expected, what was seen) is trimmed to this
#: many characters on that sentence. The report on disk keeps it in full.
MAX_ASSERTION_VALUE_CHARS_IN_THE_SENTENCE: int = 120
#: What a refused check adds to the report's ``gate_before_merge`` block. Set
#: only by a gate that ran and did not pass, so a green press's report is
#: exactly the report it always was.
GATE_ASSERTION_KEYS: tuple[str, ...] = (
    "failed_assertions",
    "failed_assertions_left_out",
    "assertion_detail_reported",
)


def what_the_gate_saw(summary: dict[str, Any]) -> str:
    """The one plain line that says what the failed check actually saw.

    Names the first thing that failed — the check it belongs to, what the
    gate said it expected and what it said it saw — and then how many more
    the report holds. When the gate said nothing about what failed inside
    the check, that is what the line says, because a check that refuses a
    merge without saying what it saw is itself something to know.

    Plain words on purpose: this is the line the owner reads, so it says
    what the machine saw and asks him for nothing.

    Empty when the summary carries no such block (a gate that never ran, a
    passing check, or a summary written before this existed), so every other
    refusal reads exactly as it did.
    """
    if "failed_assertions" not in summary:
        return ""
    entries = [e for e in (summary.get("failed_assertions") or []) if isinstance(e, dict)]
    if not entries:
        return "The check did not say which part of it failed or what it saw."
    shown = entries[:MAX_FAILED_ASSERTIONS_IN_THE_SENTENCE]
    left_out = len(entries) - len(shown)
    left_out += int(summary.get("failed_assertions_left_out") or 0)
    words = "; ".join(
        assertion_in_words(e, value_cap=MAX_ASSERTION_VALUE_CHARS_IN_THE_SENTENCE)
        for e in shown
    )
    line = f"The first thing that failed was {words}."
    if left_out:
        line += f" The report lists {left_out} more."
    return line


#: ``builds.error`` is one line, and ``forge status`` renders it in a table
#: cell. A refusal sentence is one sentence, but an advisory warning line can
#: ride along behind it, so the reason is collapsed to a line and capped —
#: the same rule the conductor's own close-out uses, so the two writers of a
#: build's ending never disagree about what that column holds.
_ERROR_LINE_LIMIT: int = 500


def _one_line(text: str) -> str:
    """Collapse ``text`` to one trimmed line for ``builds.error``."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > _ERROR_LINE_LIMIT:
        return collapsed[: _ERROR_LINE_LIMIT - 1] + "\u2026"
    return collapsed


def close_build_row(
    pool: Any,
    build_id: str,
    outcome: "MergeDeployOutcome",
    *,
    log: logging.Logger = logger,
) -> str | None:
    """Write the build's ending, the one thing only the press knows. Never raises.

    The ledger is the estate's record of what happened, and for the whole of
    the first production journey it recorded a lie: the press merged and
    promoted, and the build's row still said RUNNING hours later; a refused
    press left it saying RUNNING too, and each one was cleared by hand with
    ``forge cancel``, which writes CANCELLED over a journey that had merged
    or been honestly refused. Nothing else can write that ending. The
    conductor's close-out steps aside for the merge-card path on purpose (it
    would be racing the press), the rest of the executor writes receipts, a
    report and stage rows and never the build row, so the row had no writer
    at all.

    How the ending is read off the press's own outcome, and nothing else:

    * ``PASSED`` — the join was made and what it produced was checked —
      closes the row
      COMPLETE, with nothing written to ``builds.error`` (that column is the
      failure text ``forge status`` renders, and prose in it on a good row
      reads as a failure to every human and every dashboard).
    * ``FAILED`` — every refusal (a branch that is not there, a remote that
      could not be read, a conflict) and every red ending after a
      join that was made — closes the row FAILED with the press's own
      sentence as the reason: the same sentence the report carries and the
      same one Rich reads on the card.
    * Anything else — today only the ``SKIPPED`` shape, which the press never
      produces — writes nothing and says so, because a word this seam has
      not met is not grounds for inventing an ending.

    The write goes through :func:`forge.cli._conductor_outcome.finish_mode_c_build`,
    the estate's one careful terminal writer, rather than a second one of our
    own: it composes legal hops with
    :func:`~forge.lifecycle.state_machine.transition_chain` so
    ``apply_transition`` stays the sole writer of ``builds.status`` and an
    illegal move is still refused by the state machine; it leaves an
    already-terminal row exactly as it found it; and it never raises. Its own
    log lines are prefixed "conductor" — a cost of reusing it rather than
    growing a second writer that could drift — but the phrase they name is
    this one, so the seam is still legible in the log.

    NO TRANSITION HAD TO BE ADDED. Every state a build can be in when its
    merge word arrives already reaches both endings under today's table: a
    fix journey's row is RUNNING (RUNNING → FINALISING → COMPLETE, or RUNNING
    → FAILED), and QUEUED, PREPARING, PAUSED, FINALISING and INTERRUPTED all
    reach both as well. A routine feature build's row is already COMPLETE
    before the card is offered — the live build feed closes it when the build
    finishes, and the card is offered after that write — so the press finds a
    terminal row and leaves it alone. That is the honest answer to "does this
    change a routine feature build": it does not, and a test proves it.

    Args:
        pool: The lifecycle persistence facade.
        build_id: The build whose row this press is ending.
        outcome: The press's own outcome — its ``status`` decides the ending
            and its ``detail`` is the reason.
        log: The logger to name this seam in.

    Returns:
        The reason recorded, annotated by the writer when the row write
        degraded (no row, an already-terminal row, an unwritable row), or
        ``None`` when this press had no ending to write.
    """
    from forge.cli._conductor_outcome import finish_mode_c_build
    from forge.lifecycle.state_machine import BuildState

    status = str(getattr(outcome, "status", "") or "").strip().upper()
    if status == "PASSED":
        to_state = BuildState.COMPLETE
    elif status == "FAILED":
        to_state = BuildState.FAILED
    else:
        log.info(
            "merge-executor: the press for %s ended %r, which is not an "
            "ending this seam writes — the build row is left to its own "
            "writer",
            build_id,
            status or None,
        )
        return None

    summary = _one_line(getattr(outcome, "detail", "") or "") or (
        f"the merge press ended {getattr(outcome, 'result', None) or 'without a word'}"
    )
    return finish_mode_c_build(
        pool,
        build_id,
        to_state=to_state,
        summary=summary,
        what="the merge press's ending",
        log=log,
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


# ---------------------------------------------------------------------------
# GONE, AND WHY, SO THE PUBLISHER STAGE DOES NOT BRING THEM BACK
# ---------------------------------------------------------------------------
# Three helpers used to live here and were removed on 22 September 2026:
#
#   merged_after_all_sha       — "did the merge land after all?", asked by
#                                reading the branch literally named ``main``
#                                in the factory's own copy;
#   pinned_main_in_branch      — "is the pinned commit of that same local
#                                ``main`` an ancestor of the branch tip?";
#   moved_main_refusal_sentence — the sentence said when that local ``main``
#                                had moved during the build.
#
# All three read a branch by the name ``main``, which the factory no longer
# does anywhere: the branch a piece of work is aimed at is the one written
# down when the work started, and it may be called anything. All three were
# also about a merge INTO the project's own copy, which is the shape this
# design removed — the join is made onto the factory's own integration branch
# in a working folder of its own, and a remote that moved during the build is
# the ordinary case rather than a refusal.
#
# What replaced each: "did the join land after all?" is
# :func:`forge.pipeline.merge_join.look_at_a_join`, which asks about the
# commit's two parents rather than about a branch name; the pin is now the
# recorded target branch's commit G, fetched by
# :func:`forge.pipeline.merge_join.target_branch_now`; and the refusal is now
# one of that function's own plain sentences.
#
# They had no callers in ``src`` when they were removed. They are named here
# so the publisher stage, which needs "where is the remote's branch now" and
# "did the send land", reaches for the join's own helpers instead of writing
# ``main`` into central code again.


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
    expected_candidate_sha: str | None = None,
    expected_candidate_tree: str | None = None,
    expected_candidate_branch: str | None = None,
    worktree_retention: dict[str, Any] | None = None,
) -> MergeDeployOutcome:
    """Run join -> checks -> report for one press, and stop before publishing.

    The order inside the merge word (the one-true-copy design, 2026-09-21):

    1. the recorded target branch is fetched from the remote named ``origin``
       and where it is now is G;
    2. a working folder of its own is made at G (a git worktree, through the
       same venue every other git operation of the press uses) and the build
       system's merge runs IN THERE, onto ``factory-integration/<feature>``,
       pinned to G. Its ``--no-ff`` stays, so the joined commit J is a new
       commit of exactly G and the build's tip. The project's main copy is
       never switched, reset or merged into;
    3. the build system's own post-merge checks run on J, inside that same
       command;
    4. the factory's live candidate check runs on J's exact tree, laid out
       with the candidate-tree operation, and ONLY there;
    5. with publication switched off, the record stops at "checked", the
       result is "publication pending" and the sentence says WHY publication
       is off — either that no setting turns it on, or which of section G's
       five conditions does not hold;
    6. with publication switched on, the publisher is asked to send J to the
       recorded target branch. It is a separate process holding the one
       credential that can write to a remote; this request carries none. On
       a send that landed, read back and confirmed to CONTAIN J, the result
       is "published, deployment pending" — and the press stops there,
       because the deploy is the stage after this one. Nothing is deployed
       and nothing is said to be running.

    THE REMOTE MOVING UNDER A SEND is the one refusal a new attempt answers:
    the branch is fetched afresh, a new join is made on a name of its own,
    and BOTH kinds of check run again on the new joined result. At most three
    attempts, then "publication pending" with the reason, every joined commit
    kept under its own name. Every other refusal — a publisher that cannot be
    reached, one that holds no credential, a record it would not accept — is
    "publication pending" at once, with nothing sent.

    A conflict or a refusal at (2) is reported as it always was: nothing is
    merged, nothing is published, the branch is kept. The laid-out tree is
    removed on every ending. Never raises past its boundary: every result
    class lands as an honest :class:`MergeDeployOutcome`, one additive
    ``stage-complete`` publish, and per-step JSON receipts under
    ``receipts_root()/merge-<build_id>/``.

    PICKING UP. Every step is written to the build's publication record as an
    "about to" before it happens and a "done" after, so a press that died
    part-way is carried on rather than started again. Two rules govern what a
    later press may reuse, and both are about the world as it is NOW, never
    about what an earlier press wrote down:

    * **a join is reused only when it is a join of what is true now.** Git is
      asked whether that commit is a merge of exactly G *and the build's
      current tip* — both halves. A build's branch can gain a fix between two
      presses, and a join made onto the older tip has a tree without it, so
      reusing it would check one tree and publish another. Anything else is
      set aside under the name its own attempt gave it, the record's joined
      commit is cleared, and a fresh join is made on the next attempt's name.
      The same question settles an "about to join" that was never answered;
    * **a step counts as done only when it PASSED on exactly this joined
      commit.** The existence of a "done" line says the step finished, not
      that it was green: a red run writes one too. So a press that picks up a
      join whose post-join checks went red says so — "the checks after the
      join did not pass" stays the result until a new attempt's checks pass —
      and a person never reads "checked" for a red run. The factory's own
      live check is re-run on every press and never inherited at all.

    A record held by a live lease is left alone; a takeover needs the lease to
    have expired and raises the turn number by one.

    A MERGE WORD REFUSED BEFORE ANY OF THAT still leaves a row. A build with
    no recorded target branch, or one whose remote has renamed its default
    branch since, is refused before the lease would otherwise be taken — and
    the lease is taken anyway, so the record carries who gave the merge word,
    when, and the refusal with its reason. Without it, "nobody pressed this
    build" and "it was pressed and refused" would read the same afterwards.

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
    # WHAT TO SAY AFTERWARDS when the merge lands in a sandbox's own clone
    # (sandbox first, rule 79) is NOT said by this version, and the reason is
    # worth writing down. That sentence told an operator to fast-forward their
    # own checkout onto the sandbox's main. Since the merge word joins onto a
    # branch of the FACTORY's own and publication is switched off, there is
    # nothing on anybody's main to fetch, and saying so would be false.
    # :func:`sandbox_merge_words` is left exactly as it is for the publisher
    # stage, which is where a true version of that sentence belongs.
    # WHO IS HOLDING THIS BUILD'S RECORD. One worker per build: the name goes
    # on the lease so that a second worker finding a live lease leaves the
    # build alone, and so that a takeover says who it took over from.
    #
    # THE NAME IS THE PROCESS, not this one press. A press that runs again in
    # the same coordinator — the ordinary "it refused, fix the cause, press it
    # again" — is the same worker coming back, and it takes its own record up
    # rather than being told somebody else holds it. A worker in ANOTHER
    # process is a different name and is left alone until the lease runs out,
    # which is exactly the rule. Nothing about the name is a credential.
    worker_name = f"merge-press:{os.getpid()}"

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
        failing check names and whether the trees matched.

        A check that RAN AND DID NOT PASS adds what it saw as well — every
        failing assertion the gate reported, in the gate's own words. This is
        the long record: the candidate is torn down by the time anyone reads
        the refusal, so if the detail is not written here it is gone. No such
        key is added by any other ending, so those reports are unchanged.
        """
        block = {
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
        for key in GATE_ASSERTION_KEYS:
            if key in gate:
                block[key] = gate[key]
        return block

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
            # Additive (sandbox first, rule 79): where this merge landed and
            # the exact command that brings it to the operator's checkout —
            # only for a repository that has a sandbox, so every other
            # report is byte for byte what it was.
            **(
                {"sandbox_merge": outcome.sandbox_merge}
                if outcome.sandbox_merge
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
        # THE BUILD'S ENDING, written by the only thing that knows it. After
        # the report, so a row that cannot be written can never cost the
        # report; before the repair row, because the journey ends before its
        # follow-on begins. A dry run never reaches here — it leaves no
        # durable rows on purpose — and a row something else already closed
        # is left exactly as it is.
        close_build_row(deps.pool, build_id, outcome)
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
            # ...and what it saw: the first failing assertion in ordinary
            # words, or that the gate reported none. One added line, never a
            # dump — the whole list is on the report.
            saw = what_the_gate_saw(summary)
            return _refused_before_merge(f"{sentence} {saw}" if saw else sentence)
        gate["refusal"] = (
            f"failed its checks (the check verdict was {verdict or 'missing'}; "
            "which of the checks failed was not reported)"
        )
        sentence = candidate_refused_sentence(feature_id, detail=gate["refusal"])
        # The names were not reported, but the gate may still have said what
        # it saw; when it did, that is the only thing there is to go on.
        saw = what_the_gate_saw(summary) if summary.get("failed_assertions") else ""
        return _refused_before_merge(f"{sentence} {saw}" if saw else sentence)

    def _replaced_here() -> MergeDeployOutcome:
        """A write changed no row: this worker has been replaced. Stop at once.

        Nothing is tidied up on the way out, and that is deliberate: tidying
        up is itself a change to the world, and the worker that took this
        build over is the one entitled to make it. The candidate and the
        laid-out tree are left exactly where they are, because the new holder
        settles them by looking.
        """
        nonlocal candidate_standing, tree_path
        logger.error(
            "merge-executor: %s's publication record has moved on — this "
            "worker has been replaced and stops without tidying up",
            build_id,
        )
        candidate_standing = False
        tree_path = None
        return MergeDeployOutcome(
            result="merge-refused",
            status="FAILED",
            failed_step="record",
            detail=(
                f"this worker was replaced part-way through {named}'s merge "
                "word — another worker holds the record now, and this one "
                "changed nothing further."
            ),
        )

    def _the_identity_the_deploy_uses_today() -> dict[str, Any]:
        """What identifies the thing that was checked — as it stands TODAY.

        The design's section C says this identity must be one that cannot be
        reused, and that the project's deploy step must be handed it and made
        to deploy exactly it. That is the NEXT stage. What is recorded here is
        what actually exists now, and the gap is named in the record rather
        than papered over: today the deploy promotes a shared name, which
        another build can overwrite between this check and that deploy.
        """
        return {
            "as_it_stands": gate.get("checked_identity"),
            "how_the_deploy_identifies_it_today": (
                "a shared name the project's own deploy step promotes"
            ),
            "gap": (
                "the thing that was checked has no identity that cannot be "
                "reused, so another build could overwrite the shared name "
                "between this check and a deploy. Section C of the design "
                "closes this and belongs to the next stage."
            ),
        }

    async def _lay_the_tree_out(sha: str) -> MergeDeployOutcome | None:
        """Lay out the exact tree of ``sha`` for the live check. None = it is there."""
        nonlocal tree_path
        excluded_now: bool | None = None
        try:
            excluded_now = await git.ensure_candidate_trees_excluded()
            laid_out = await git.materialise_candidate_tree(feature_id, str(sha))
            tree_path = Path(laid_out.path)
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
                    "candidate_sha": sha,
                    "candidate_tree": gate["candidate_tree"],
                    "tree_path": None,
                    "error": str(exc),
                },
            )
            return _could_not_check(
                f"its tree could not be laid out for the check ({exc})"
            )
        gate["exclude_written_now"] = excluded_now
        return None

    async def _run_the_candidate_check(sha: str) -> MergeDeployOutcome | None:
        """Run the registered live checks against the laid-out tree of ``sha``.

        ``None`` when every check passed; an honest outcome otherwise. This is
        the factory's own live check, and since the merge word's join it runs
        on the JOINED commit and only there: checking the build's own branch
        was not enough once the remote could have moved under it.
        """
        nonlocal candidate_standing
        try:
            checked = await _dispatch("candidate_check", candidate_cwd=str(tree_path))
        except Exception as exc:  # noqa: BLE001 — the sidecar-surface ValueError crack
            _write_receipt(
                "merge_deploy_candidate.json",
                {
                    "step": "candidate",
                    "dry_run": dry_run,
                    "candidate_sha": sha,
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
        for key in GATE_ASSERTION_KEYS:
            if key in summary:
                gate[key] = summary[key]
        # WHAT THE DEPLOY WOULD IDENTIFY, as the check itself reported it.
        gate["checked_identity"] = c_detail.get("deploy_record_ref") or c_detail.get(
            "candidate"
        )
        reason = str(c_detail.get("reason") or "")
        gate["ran"] = checked is not None and reason != "no_candidate_section"
        _write_receipt(
            "merge_deploy_candidate.json",
            {
                "step": "candidate",
                "dry_run": dry_run,
                "branch": branch,
                "checked_commit": sha,
                "candidate_tree": gate["candidate_tree"],
                "tree_path": str(tree_path),
                "exclude_written_now": gate.get("exclude_written_now"),
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
                        "checked_commit": sha,
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
        return None

    def _publication_store() -> Any:
        """The publication record's store, or None when there is no ledger here.

        A press driven against a faked persistence facade — every unit test
        written before the record existed — has no connection to write to. It
        runs without a record and says so in the log, exactly as a build with
        no record reads as "not recorded".
        """
        if deps.publication_store is not None:
            return deps.publication_store()
        connection = getattr(deps.pool, "connection", None)
        if connection is None:
            return None
        try:
            return PublicationRecordStore(connection)
        except Exception as exc:  # noqa: BLE001 — a record never costs a press
            logger.warning(
                "merge-executor: no publication record for %s (%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )
            return None

    def _cannot_join(sentence: str) -> MergeDeployOutcome:
        """Nothing could be joined, so nothing happened. The branch is kept."""
        return MergeDeployOutcome(
            result="merge-refused",
            status="FAILED",
            failed_step="join",
            detail=sentence,
            gate_before_merge=_gate_for_report() if gate_began else None,
        )

    def _write_the_refusal_down(
        *, store: Any, target_branch: str | None, refusal: str
    ) -> None:
        """One row for this build, saying the merge word was given and refused.

        Used where the press stops BEFORE it would otherwise have taken the
        lease — no branch of the remote was ever written down for this build,
        or the remote's default branch has been renamed since. Taking the
        lease is what creates the row, so it is taken here too; it carries
        who gave the merge word and when, and the refusal is written onto the
        record as the result with its reason beside it.

        It never costs the press anything: a record that cannot be taken or
        written is logged and the refusal is returned either way. A build
        whose record somebody else is holding is left alone, which is the
        same rule as everywhere else.
        """
        if store is None:
            return
        try:
            grant = store.take_lease(
                build_id=build_id,
                holder=worker_name,
                now=deps.clock(),
                feature_id=feature_id,
                repo=repo,
                decided_by=decided_by,
                target_branch=target_branch,
            )
            if grant is None:
                logger.warning(
                    "merge-executor: %s's merge word was refused (%s) and the "
                    "refusal could not be written down — another worker holds "
                    "the record",
                    build_id,
                    refusal,
                )
                return
            store.done(
                build_id=build_id,
                turn=grant.turn,
                now=deps.clock(),
                step=STEP_JOIN,
                attempt=0,
                result={
                    "joined": False,
                    "refused_before_the_join": True,
                    "recorded_target_branch": target_branch,
                    "refusal": refusal,
                },
            )
            store.record(
                build_id=build_id,
                turn=grant.turn,
                now=deps.clock(),
                result=RESULT_PUBLICATION_PENDING,
                # AND THE LEASE IS PUT DOWN AGAIN. Nothing is in progress:
                # the press stopped before it did anything, so holding the
                # record would only make the next merge word — from another
                # process, once somebody has fixed the cause — wait out the
                # lease for no reason.
                lease_holder=None,
                lease_expires_at=None,
            )
        except Exception as exc:  # noqa: BLE001 — a record never costs a press
            logger.warning(
                "merge-executor: %s's refusal could not be written onto its "
                "publication record (%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )

    # -----------------------------------------------------------------------
    # THE SEND, and the three endings it can have
    # -----------------------------------------------------------------------

    async def _ask_for_the_send(asked: dict[str, Any]) -> dict[str, Any]:
        """Ask the publisher to send. Never raises; a failure IS an answer.

        The coordinator holds no credential and this request carries none:
        a project, a build, a turn number, a commit and a branch. Anything
        that goes wrong on the way — no publisher configured, one that
        cannot be reached, one that took too long, one that answered with
        something that is not an answer — comes back as "not published" with
        a sentence, which is the honest end of all of them.
        """
        asker = deps.publisher or ask_the_publisher
        try:
            answered = await asker(deps.config, dict(asked))
        except Exception as exc:  # noqa: BLE001 — a refusal, never a crash
            logger.error(
                "merge-executor: asking the publisher to send %s for %s raised "
                "(%s: %s) — nothing is known to have been sent",
                str(asked.get("j_commit"))[:10],
                build_id,
                type(exc).__name__,
                exc,
            )
            return {
                "published": False,
                "remote_now": None,
                "contains_j": False,
                "refusal": (
                    f"asking the publisher to send ended in an error "
                    f"({type(exc).__name__}), so nothing is known to have "
                    "been sent"
                ),
                "refusal_kind": "the-publisher-could-not-be-reached",
            }
        if not isinstance(answered, dict):
            return {
                "published": False,
                "remote_now": None,
                "contains_j": False,
                "refusal": (
                    "the publisher answered with something that is not an "
                    "answer, so nothing is known to have been sent"
                ),
                "refusal_kind": "the-publisher-could-not-be-reached",
            }
        return answered

    def _the_checks_sentence() -> str:
        if isinstance(gate.get("checks_passed"), int) and isinstance(
            gate.get("checks_total"), int
        ):
            return f" — checks {gate['checks_passed']}/{gate['checks_total']}"
        return ""

    def _published_deployment_pending(
        *,
        j_commit: str,
        target_branch: str,
        g_commit: str,
        remote_now: str,
        checks_passed: int | None,
        checks_total: int | None,
        what_was_checked: dict[str, Any],
        attempt: int,
        turn: int,
        store: Any,
        why_not_deployed: str = "",
        already_running: bool = False,
    ) -> MergeDeployOutcome:
        """The remote has it. The deploy has not run, and IT IS NOT CLAIMED TO.

        The second result of the design's three-name vocabulary. It is never
        called a merge that is running: what is running is not this, and
        saying so would be the very claim this whole lane removed.

        ``why_not_deployed`` says WHY in the sentence a person reads. There is
        always a reason now that the deploy exists — the lock is held by
        somebody else, the project declares no target, or the only-forwards
        rule said not to — and a result with "pending" in its name that does
        not say what it is pending on is a result nobody can act on.

        ``already_running`` is the one ending that is not waiting for
        anything: the design's section B, a later result that already includes
        this one. Nothing more will be deployed for this build, and the
        sentence says that rather than leaving somebody waiting.
        """
        if store is not None and not store.record(
            build_id=build_id,
            turn=turn,
            now=deps.clock(),
            result=RESULT_PUBLISHED_DEPLOYMENT_PENDING,
        ):
            return _replaced_here()
        _write_receipt(
            "merge_deploy_publication.json",
            {
                "step": "publication",
                "switched_on": True,
                "result": RESULT_PUBLISHED_DEPLOYMENT_PENDING,
                "target_branch": target_branch,
                "g_commit": g_commit,
                "build_tip": what_was_checked.get("build_tip"),
                "j_commit": j_commit,
                "remote_now": remote_now,
                "contains_j": True,
                "attempt": attempt,
                "turn": turn,
                "checked": what_was_checked,
                "deployed": False,
                "already_running": already_running,
                "why_nothing_was_deployed": (
                    why_not_deployed
                    or "the press did not reach the deploy"
                ),
            },
        )
        if already_running:
            return MergeDeployOutcome(
                result=RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING,
                # PASSED: nothing is waiting. The work is on the remote and a
                # later result that contains it is already running, so there
                # is no step left for anything in this estate to take.
                status="PASSED",
                merged_sha=j_commit,
                detail=(
                    f"{named} was joined onto {target_branch} at "
                    f"{g_commit[:10]} in a working folder of its own, the "
                    f"joined result {j_commit[:10]} was checked"
                    f"{_the_checks_sentence()}, and it was published: the "
                    f"branch {target_branch} on the remote named origin is at "
                    f"{remote_now[:10]} and contains it. It was NOT deployed, "
                    f"and it will not be: {why_not_deployed} Nothing more is "
                    "waiting for this build."
                ),
                checks_passed=checks_passed,
                checks_total=checks_total,
                deployed_in=None,
                gate_before_merge=_gate_for_report() if gate_began else None,
            )
        return MergeDeployOutcome(
            result=RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING,
            # PASSED, AND WHY, because it is a fair question with the word
            # "pending" in the result. PASSED is what closes the build's row,
            # and the row is closed on "the press finished what this version
            # of it does" — which it has: the work is joined, checked and on
            # the remote, and there is no further step in the running system
            # to wait for. When the deploy exists, the ending belongs to it
            # and this becomes an intermediate state like any other. Nothing
            # here says anything is deployed or running: the result word and
            # the sentence both say the opposite.
            status="PASSED",
            merged_sha=j_commit,
            detail=(
                f"{named} was joined onto {target_branch} at {g_commit[:10]} "
                f"in a working folder of its own, the joined result "
                f"{j_commit[:10]} was checked{_the_checks_sentence()}, and it "
                f"was published: the branch {target_branch} on the remote "
                f"named origin is at {remote_now[:10]} and contains it. "
                "Nothing has been deployed, so this is published, deployment "
                f"pending: {why_not_deployed or 'the deploy did not run'}."
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
            deployed_in=None,
            # A PICK-UP NEVER RAN A CHECK, so it has no gate to report and
            # says so by sending no block at all rather than a block of
            # nothings.
            gate_before_merge=_gate_for_report() if gate_began else None,
        )

    # -----------------------------------------------------------------------
    # THE DEPLOY: under the lock, only forwards, by an identity that cannot be
    # reused, and confirmed from what the running thing itself reports.
    # -----------------------------------------------------------------------

    def _deployment_lock_store() -> Any:
        if deps.deployment_lock is not None:
            return deps.deployment_lock()
        connection = getattr(deps.pool, "connection", None)
        if connection is None:
            return None
        try:
            return DeploymentLockStore(connection)
        except Exception as exc:  # noqa: BLE001 — a missing lock is a refusal
            logger.warning(
                "merge-executor: no deployment lock for %s (%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )
            return None

    def _the_target_and_how_it_wants_the_identity() -> tuple[str, Any] | None:
        """This project's deployment target, and its identity declaration.

        Both come from the PROJECT: the target is the project and the
        environment its own deploy profile declares, and the two names — the
        setting the identity is handed in, the marker the step reports it
        after — are the project's to choose. A project whose profile cannot be
        read has no target this press can take a lock on, and the press says
        so rather than guessing one.
        """
        if deps.deployment_target is not None:
            try:
                return deps.deployment_target(repo, repo_root)
            except Exception as exc:  # noqa: BLE001 — a refusal, never a crash
                logger.warning(
                    "merge-executor: %s's deployment target could not be "
                    "worked out (%s: %s)",
                    repo,
                    type(exc).__name__,
                    exc,
                )
                return None
        try:
            from forge.deploy.profile import load_deploy_profile

            profile = load_deploy_profile(repo_root / "deploy" / "profile.yaml")
        except Exception as exc:  # noqa: BLE001 — a refusal, never a crash
            logger.warning(
                "merge-executor: %s's deploy profile could not be read (%s: "
                "%s), so there is no deployment target to take a lock on",
                repo,
                type(exc).__name__,
                exc,
            )
            return None
        return (
            deployment_target_name(repo, getattr(profile, "env_id", None)),
            declared_identity(profile),
        )

    def _merged_and_running(
        *,
        j_commit: str,
        target_branch: str,
        g_commit: str,
        remote_now: str,
        target: str,
        identity: str,
        checks_passed: int | None,
        checks_total: int | None,
    ) -> MergeDeployOutcome:
        """The third result word, and the ONLY path that can produce it.

        It is said when, and only when, the joined commit is on the remote's
        recorded branch AND the thing that was checked for it has been
        deployed AND the running thing reported back the very identity it was
        handed. Anything short of all three is one of the two words before it.
        """
        return MergeDeployOutcome(
            result=RESULT_WORD_MERGED_AND_RUNNING,
            status="PASSED",
            merged_sha=j_commit,
            detail=(
                f"{named} was joined onto {target_branch} at {g_commit[:10]} "
                f"in a working folder of its own, the joined result "
                f"{j_commit[:10]} was checked{_the_checks_sentence()}, it was "
                f"published — the branch {target_branch} on the remote named "
                f"origin is at {remote_now[:10]} and contains it — and what "
                f"was checked is now running on {target}, which reported back "
                f"the identity it was handed ({identity}). Merged into the "
                "remote and running."
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
            deployed_in=deployed_in_for(repo_root),
            gate_before_merge=_gate_for_report() if gate_began else None,
        )

    async def _deploy_what_was_checked(
        *,
        j_commit: str,
        j_tree: str | None,
        target_branch: str,
        g_commit: str,
        remote_now: str,
        checks_passed: int | None,
        checks_total: int | None,
        what_was_checked: dict[str, Any],
        attempt: int,
        turn: int,
        store: Any,
    ) -> MergeDeployOutcome:
        """Published. Now deploy exactly what was checked, or say why not.

        The whole of the design's C, B, F, I and the coordinator's half of H
        and J happens here, in this order and under one lock:

        1. work out the project's own deployment target and how it wants the
           identity handed over and reported back;
        2. TAKE THE LOCK in the ledger, which raises the TARGET'S own counter
           and binds it to this build. Everything after this is done against
           that counter, and a takeover cancels it;
        3. read what is running, R, off the lock's own row — and if the
           identity recorded there is already the identity for this joined
           commit, the deploy has already happened and nothing is done again
           (the pick-up after a crash between "about to deploy" and its
           "done");
        4. apply ONLY FORWARDS: nothing running or R part of J ⇒ deploy; J
           part of R ⇒ do not deploy, a later result that includes it is
           already running; neither ⇒ stop and say so for a person;
        5. hand the project's declared deploy step the fixed identity, under
           the setting name the project declared, with the target's counter
           and this build beside it so the executor can enforce ownership;
        6. read back the identity the step says is now running and compare it
           with the one it was handed, AS TEXT. A mismatch is a FAILED deploy;
        7. record R and release the lock.
        """
        lock = _deployment_lock_store()
        if lock is None:
            return _published_deployment_pending(
                j_commit=j_commit,
                target_branch=target_branch,
                g_commit=g_commit,
                remote_now=remote_now,
                checks_passed=checks_passed,
                checks_total=checks_total,
                what_was_checked=what_was_checked,
                attempt=attempt,
                turn=turn,
                store=store,
                why_not_deployed=(
                    "there is no deployment lock here to hold the target "
                    "with, and nothing is deployed without one"
                ),
            )
        known = _the_target_and_how_it_wants_the_identity()
        if known is None:
            return _published_deployment_pending(
                j_commit=j_commit,
                target_branch=target_branch,
                g_commit=g_commit,
                remote_now=remote_now,
                checks_passed=checks_passed,
                checks_total=checks_total,
                what_was_checked=what_was_checked,
                attempt=attempt,
                turn=turn,
                store=store,
                why_not_deployed=(
                    f"{repo} does not declare a deployment target this press "
                    "can read, so there was nothing to take a lock on and "
                    "nothing was deployed"
                ),
            )
        target, declaration = known
        # WHAT AN IDENTITY IS BELONGS TO THE PROJECT, and so do the two names
        # it travels under: the setting the deploy step is handed it in, and
        # the marker the step reports what is running after. A project that
        # declares neither has not been asked this question yet, and handing
        # its step a name the factory picked and then failing it for not
        # reporting one back is a deploy run blind. It is said before the lock
        # is taken, because there is nothing here to hold a lock for.
        if not getattr(declaration, "declared", False):
            return _published_deployment_pending(
                j_commit=j_commit,
                target_branch=target_branch,
                g_commit=g_commit,
                remote_now=remote_now,
                checks_passed=checks_passed,
                checks_total=checks_total,
                what_was_checked=what_was_checked,
                attempt=attempt,
                turn=turn,
                store=store,
                why_not_deployed=(
                    f"{repo}'s deploy profile does not say how it wants the "
                    "identity of what was checked handed to its deploy step, "
                    "or what the step reports back — both are the project's "
                    "own to declare, in an identity block in "
                    "deploy/profile.yaml — so nothing was deployed rather "
                    "than deployed blind"
                ),
            )
        identity = fixed_identity(j_commit=j_commit, content=j_tree)

        grant = lock.grant(
            target=target,
            build_id=build_id,
            turn=turn,
            holder=worker_name,
            now=deps.clock(),
        )
        if grant is None:
            current = lock.read(target)
            return _published_deployment_pending(
                j_commit=j_commit,
                target_branch=target_branch,
                g_commit=g_commit,
                remote_now=remote_now,
                checks_passed=checks_passed,
                checks_total=checks_total,
                what_was_checked=what_was_checked,
                attempt=attempt,
                turn=turn,
                store=store,
                why_not_deployed=(
                    f"another build ({current.holder_build or 'unnamed'}) "
                    f"holds the deployment lock on {target}, so this press "
                    "deployed nothing and left it alone"
                ),
            )

        try:
            # 3. THE PICK-UP, BY IDENTITY. If what is running already reports
            # the identity made for this joined commit, this deploy happened
            # and its "done" line was lost. Nothing is done twice.
            if (
                grant.running_identity
                and str(grant.running_identity).strip() == identity.text
            ):
                logger.info(
                    "merge-executor: %s is already running on %s by the very "
                    "identity made for it (%s) — it was found by looking, not "
                    "deployed again",
                    j_commit[:10],
                    target,
                    identity.text,
                )
                if store is not None and not store.done(
                    build_id=build_id,
                    turn=turn,
                    now=deps.clock(),
                    step=STEP_DEPLOY,
                    attempt=attempt,
                    result={
                        "ran_on": j_commit,
                        "deployed": True,
                        "found_by_looking": True,
                        "target": target,
                        "target_counter": grant.counter,
                        "identity": identity.to_wire(),
                        "detail": (
                            "the deploy had already been made when the run "
                            "stopped; what is running reported this very "
                            "identity, so it was not deployed again"
                        ),
                    },
                ):
                    return _replaced_here()
                if store is not None and not store.record(
                    build_id=build_id,
                    turn=turn,
                    now=deps.clock(),
                    result=RESULT_MERGED_AND_RUNNING,
                ):
                    return _replaced_here()
                return _merged_and_running(
                    j_commit=j_commit,
                    target_branch=target_branch,
                    g_commit=g_commit,
                    remote_now=remote_now,
                    target=target,
                    identity=identity.text,
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                )

            # 4. ONLY FORWARDS.
            forwards = await what_to_do_about_j(
                git,
                j_commit=j_commit,
                running_commit=grant.running_commit,
                running_identity=grant.running_identity,
                target=target,
            )
            if not forwards.go:
                if store is not None and not store.done(
                    build_id=build_id,
                    turn=turn,
                    now=deps.clock(),
                    step=STEP_DEPLOY,
                    attempt=attempt,
                    result={
                        "ran_on": j_commit,
                        "deployed": False,
                        "target": target,
                        "target_counter": grant.counter,
                        "only_forwards": forwards.to_wire(),
                    },
                ):
                    return _replaced_here()
                return _published_deployment_pending(
                    j_commit=j_commit,
                    target_branch=target_branch,
                    g_commit=g_commit,
                    remote_now=remote_now,
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                    what_was_checked=what_was_checked,
                    attempt=attempt,
                    turn=turn,
                    store=store,
                    why_not_deployed=forwards.sentence,
                    already_running=forwards.already,
                )

            # 5. HAND THE IDENTITY OVER, with the ownership beside it.
            ownership = {
                "target": target,
                "target_counter": grant.counter,
                "build": build_id,
                "identity": identity.text,
                "identity_setting": declaration.setting,
                # WHETHER ANYTHING IS RUNNING THERE AT ALL, read off the
                # target's own row under this lock a moment ago. The executor
                # uses it for one thing: when its own note for this target is
                # gone and nobody can be asked who owns it, something already
                # running means a deploy has happened before, so a note should
                # have existed and its absence is a LOSS — and the executor
                # refuses rather than treating a missing note as an empty slot.
                # It can only make the executor stricter, never more
                # permissive.
                "something_is_running": bool(
                    grant.running_commit or grant.running_identity
                ),
            }
            if store is not None and not store.about_to(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                step=STEP_DEPLOY,
                attempt=attempt,
                inputs={
                    "j_commit": j_commit,
                    "target": target,
                    "target_counter": grant.counter,
                    "identity": identity.to_wire(),
                    "declared": declaration.to_wire(),
                    "what_is_running_now": forwards.to_wire(),
                },
            ):
                return _replaced_here()

            try:
                deployed = await _dispatch("promote", deploy_ownership=ownership)
            except Exception as exc:  # noqa: BLE001 — a refusal, never a crash
                why = (
                    f"the deploy of {identity.text} to {target} raised "
                    f"({type(exc).__name__}: {exc}), so what is running there "
                    "is not known to have changed"
                )
                return _deploy_failed(
                    j_commit=j_commit,
                    target=target,
                    why=why,
                    identity=identity.text,
                    reported=None,
                    attempt=attempt,
                    turn=turn,
                    store=store,
                    counter=grant.counter,
                )

            outcome_word = getattr(deployed, "outcome", None)
            detail = getattr(deployed, "detail", None) or {}
            said = str(detail.get("deploy_output") or "")
            reported = identity_reported_by(said, marker=declaration.marker)
            if deployed is None or outcome_word != "complete":
                return _deploy_failed(
                    j_commit=j_commit,
                    target=target,
                    why=(
                        f"the project's own deploy step did not finish "
                        f"({outcome_word or 'the deploy stage answered nothing'})"
                    ),
                    identity=identity.text,
                    reported=reported,
                    attempt=attempt,
                    turn=turn,
                    store=store,
                    counter=grant.counter,
                )

            # 6. WHAT IS RUNNING HAS TO BE WHAT WAS HANDED OVER.
            if the_identities_differ(identity.text, reported):
                return _deploy_failed(
                    j_commit=j_commit,
                    target=target,
                    why=(
                        f"the deploy step was handed {identity.text} and "
                        + (
                            f"reported {reported} as what is now running"
                            if reported
                            else (
                                "reported no identity at all, so what is "
                                "running has not been shown to be what was "
                                "checked"
                            )
                        )
                    ),
                    identity=identity.text,
                    reported=reported,
                    attempt=attempt,
                    turn=turn,
                    store=store,
                    counter=grant.counter,
                )

            # 7. R IS WRITTEN DOWN, AND ONLY THEN IS THE LOCK PUT DOWN.
            if not lock.record_running(
                target=target,
                counter=grant.counter,
                now=deps.clock(),
                commit=j_commit,
                identity=identity.text,
                build_id=build_id,
            ):
                return _replaced_here()
            if store is not None and not store.done(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                step=STEP_DEPLOY,
                attempt=attempt,
                result={
                    "ran_on": j_commit,
                    "deployed": True,
                    "target": target,
                    "target_counter": grant.counter,
                    "identity": identity.to_wire(),
                    "reported": reported,
                    "verify_ok": True,
                },
            ):
                return _replaced_here()
            if store is not None and not store.record(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                result=RESULT_MERGED_AND_RUNNING,
            ):
                return _replaced_here()
            _write_receipt(
                "merge_deploy_deployment.json",
                {
                    "step": "deploy",
                    "target": target,
                    "target_counter": grant.counter,
                    "build_id": build_id,
                    "turn": turn,
                    "attempt": attempt,
                    "j_commit": j_commit,
                    "identity": identity.to_wire(),
                    "declared": declaration.to_wire(),
                    "reported": reported,
                    "only_forwards": forwards.to_wire(),
                    "result": RESULT_MERGED_AND_RUNNING,
                },
            )
            return _merged_and_running(
                j_commit=j_commit,
                target_branch=target_branch,
                g_commit=g_commit,
                remote_now=remote_now,
                target=target,
                identity=identity.text,
                checks_passed=checks_passed,
                checks_total=checks_total,
            )
        finally:
            # THE LOCK IS PUT DOWN AFTER THE CONFIRMATION IS RECORDED, on
            # every ending. A release that changes no row means this holder
            # was taken over while it worked, which is exactly the case the
            # counter exists for, and it is left alone.
            try:
                lock.release(
                    target=target, counter=grant.counter, now=deps.clock()
                )
            except Exception as exc:  # noqa: BLE001 — never costs a result
                logger.warning(
                    "merge-executor: the deployment lock on %s could not be "
                    "put down (%s: %s) — it expires on its own",
                    target,
                    type(exc).__name__,
                    exc,
                )

    def _deploy_failed(
        *,
        j_commit: str,
        target: str,
        why: str,
        identity: str,
        reported: str | None,
        attempt: int,
        turn: int,
        store: Any,
        counter: int,
    ) -> MergeDeployOutcome:
        """The deploy did not put what was checked live. Said, not softened."""
        logger.error(
            "merge-executor: the deploy of %s to %s FAILED — %s",
            j_commit[:10],
            target,
            why,
        )
        if store is not None:
            store.done(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                step=STEP_DEPLOY,
                attempt=attempt,
                result={
                    "ran_on": j_commit,
                    "deployed": False,
                    "verify_ok": False,
                    "target": target,
                    "target_counter": counter,
                    "identity": identity,
                    "reported": reported,
                    "why": why,
                },
            )
            store.record(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                result=RESULT_PUBLISHED_DEPLOYMENT_PENDING,
            )
        _write_receipt(
            "merge_deploy_deployment.json",
            {
                "step": "deploy",
                "target": target,
                "target_counter": counter,
                "j_commit": j_commit,
                "identity": identity,
                "reported": reported,
                "deployed": False,
                "why": why,
            },
        )
        return MergeDeployOutcome(
            # One of the three RED endings the estate already knows: the work
            # landed on the remote and what came after it went red, which is
            # worth a repair job.
            result="merged-deploy-failed",
            status="FAILED",
            merged_sha=j_commit,
            failed_step="deploy",
            detail=(
                f"{named}'s joined result {j_commit[:10]} is on the remote, "
                f"but it is NOT running: {why}. Nothing claims to be running "
                "that has not been shown to be."
            ),
            gate_before_merge=_gate_for_report() if gate_began else None,
        )

    def _publication_pending(
        *,
        j_commit: str | None,
        target_branch: str,
        g_commit: str,
        why_not: str,
        answer: dict[str, Any],
        checks_passed: int | None,
        checks_total: int | None,
        what_was_checked: dict[str, Any],
        attempt: int,
        attempt_round: int,
        turn: int,
        store: Any,
    ) -> MergeDeployOutcome:
        """It was checked and it was not sent. The reason is said; J is kept."""
        if store is not None and not store.record(
            build_id=build_id,
            turn=turn,
            now=deps.clock(),
            result=RESULT_PUBLICATION_PENDING,
        ):
            return _replaced_here()
        _write_receipt(
            "merge_deploy_publication.json",
            {
                "step": "publication",
                "switched_on": True,
                "result": RESULT_PUBLICATION_PENDING,
                "target_branch": target_branch,
                "g_commit": g_commit,
                "j_commit": j_commit,
                "attempt": attempt,
                "attempts_made": attempt_round,
                "turn": turn,
                "checked": what_was_checked,
                "published": False,
                "why_not": why_not,
                "refusal_kind": answer.get("refusal_kind"),
                "remote_now": answer.get("remote_now"),
                "every_join_is_kept": True,
            },
        )
        return MergeDeployOutcome(
            result=RESULT_WORD_PUBLICATION_PENDING,
            # Not a pass and not a failure: the work joined and checked, and
            # the send did not happen. The build's row is left open so that
            # the next merge word, or the plain command, picks it up.
            status="GATED",
            merged_sha=j_commit,
            detail=(
                f"{named} was joined onto {target_branch} at {g_commit[:10]} "
                f"in a working folder of its own and the joined result "
                f"{str(j_commit)[:10]} was checked"
                f"{_the_checks_sentence()}, but it was not published: "
                f"{why_not}. Nothing is on the remote that was not there "
                "before and nothing was deployed. Every joined commit is kept "
                "under its own name, so this can be picked up where it "
                "stopped."
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
            gate_before_merge=_gate_for_report(),
        )

    def _not_wholly_checked(
        *,
        j_commit: str,
        target_branch: str,
        g_commit: str,
        checks_passed: int | None,
        checks_total: int | None,
    ) -> MergeDeployOutcome:
        """One of the two kinds of check has not run on this J, so nothing is sent."""
        logger.warning(
            "merge-executor: %s's joined result %s has had only the factory's "
            "own live check run on it — the build system's checks after a "
            "join were not re-run, so nothing is sent",
            feature_id,
            j_commit[:10],
        )
        return MergeDeployOutcome(
            result=RESULT_WORD_PUBLICATION_PENDING,
            status="GATED",
            merged_sha=j_commit,
            detail=(
                f"{named} was joined onto {target_branch} at {g_commit[:10]} "
                f"in a working folder of its own; the factory's own live check "
                f"ran on the joined result {j_commit[:10]}"
                f"{_the_checks_sentence()}, but the build system's checks "
                "after a join have not run on it, because this press picked "
                "up a join an earlier one had already made and does not run "
                "the merge command again. It is NOT yet checked, so nothing "
                "was sent to the remote and nothing was deployed. The branch "
                "and the join are kept."
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
            gate_before_merge=_gate_for_report(),
        )

    async def _the_send_may_already_have_happened(
        *,
        record: Any,
        store: Any,
        turn: int,
        g_commit: str,
        target_branch: str,
    ) -> MergeDeployOutcome | None:
        """Read the remote FIRST. ``None`` = nothing was sent; carry on.

        Two shapes of record bring the press here, and both are settled by
        looking at the world rather than by believing a line:

        * the last line is an "about to send" nothing answered — the send may
          have landed a moment before the coordinator stopped;
        * a "done send" says it was published — and this press must not then
          join and send again, which is exactly what it would do, because a
          landed send moves the remote's branch to J and the join's own rule
          would find the recorded join is no longer a join of what is true
          now.

        In both cases the question asked of the remote is "does the target
        branch CONTAIN J", never "is it J": somebody else may add to the
        branch in the seconds between.
        """
        if record is None or not getattr(record, "recorded", False):
            return None
        j = getattr(record, "j_commit", None)
        if not j:
            return None
        unfinished = record.unfinished()
        # ANY LINE ABOUT A SEND IS ENOUGH TO GO AND LOOK, and the reason is
        # worth writing down. A send has three endings on the record and only
        # one of them is a fact: "done, published" says it landed; "about to"
        # with no answer says nobody knows; and "done, not published" says
        # only that THE ANSWER said so — and the commonest of those answers is
        # "the publisher could not be reached", which is precisely the case
        # where the send may have landed and the reply was lost. So the
        # remote is read whenever a send was so much as attempted, and it is
        # the remote that decides. A record that never reached a send is left
        # alone, so nothing about a press that has not sent yet changes.
        a_send_was_attempted = any(
            line.step == STEP_SEND for line in getattr(record, "lines", ())
        )
        about_to_send = unfinished is not None and unfinished.step == STEP_SEND
        if not a_send_was_attempted:
            return None
        contains = await git.is_ancestor(str(j), g_commit)
        if contains is not True:
            if about_to_send:
                logger.info(
                    "merge-executor: %s's last line said a send was about to "
                    "be made and the remote's branch does not contain %s — "
                    "nothing was sent, so this press sends",
                    feature_id,
                    str(j)[:10],
                )
            return None
        logger.info(
            "merge-executor: %s's joined result %s is already on the remote's "
            "%s — it was found by looking, not sent again",
            feature_id,
            str(j)[:10],
            target_branch,
        )
        if store is not None and about_to_send:
            if not store.done(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                step=STEP_SEND,
                attempt=int(getattr(unfinished, "attempt", 0) or 0),
                result={
                    "ran_on": str(j),
                    "published": True,
                    "contains_j": True,
                    "remote_now": g_commit,
                    "found_by_looking": True,
                    "detail": (
                        "the send had already been made when the run stopped; "
                        "the remote was read and it was there, so it was not "
                        "sent again"
                    ),
                },
            ):
                return _replaced_here()
        # IT IS PUBLISHED. The deploy is a separate decision and it is made
        # here too, because a press that picked a landed send up is exactly
        # the press that has to settle whether what was checked is running —
        # which it does by looking at the target, not by believing a line.
        checked_here = dict(getattr(record, "checked", {}) or {})
        return await _deploy_what_was_checked(
            j_commit=str(j),
            j_tree=str(checked_here.get("j_tree") or "") or None,
            target_branch=target_branch,
            g_commit=str(getattr(record, "g_commit", None) or g_commit),
            remote_now=g_commit,
            checks_passed=None,
            checks_total=None,
            what_was_checked=checked_here,
            attempt=int(getattr(record, "attempt", 0) or 0),
            turn=turn,
            store=store,
        )

    async def _start_another_attempt(
        *, recorded_branch: str | None, store: Any, turn: int
    ) -> tuple[str, str, Any] | MergeDeployOutcome:
        """The remote moved: fetch it again, and give the next round a new G.

        The laid-out tree of the attempt that was refused is taken down
        first, so that the next attempt lays its own out cleanly. Nothing
        else is removed: the joined commit and the working folder of every
        attempt stay under the names their own attempt gave them.
        """
        nonlocal tree_path
        if candidate_standing:
            await _tear_down_candidate()
        if tree_path is not None:
            await git.remove_candidate_tree(feature_id, str(tree_path))
            tree_path = None
        where = await target_branch_now(git, recorded_branch=recorded_branch)
        if not where.ok:
            return _cannot_join(
                f"{named} could not be joined onto the remote again after it "
                f"moved: {where.refusal} The joined commit of every attempt "
                "so far is kept."
            )
        # THE RECORD'S G MOVES WITH THE ATTEMPT. The publisher checks, for
        # itself, that the joined commit is a merge of exactly the RECORDED G
        # and the recorded build tip — so a record still carrying the last
        # attempt's G would make the next attempt's join look forged. It is
        # written here, before anything is joined onto it.
        if store is not None and not store.record(
            build_id=build_id,
            turn=turn,
            now=deps.clock(),
            g_commit=str(where.commit),
        ):
            return _replaced_here()
        fresh = store.read(build_id) if store is not None else None
        return str(where.commit), str(where.branch), fresh

    async def _press() -> MergeDeployOutcome:
        nonlocal tree_path, candidate_standing, gate_began

        # IS THIS A PICK-UP? A build whose record already holds a join that
        # was made is not being merged again; it is being carried on, and
        # continuing is not a new decision (second revision A). The guard
        # against a repeated merge STEP stays exactly as it is for everything
        # else — a fresh press on a build that already merged still answers
        # it and nothing else.
        store = None if dry_run else _publication_store()
        already = store.read(build_id) if store is not None else None
        # A JOIN THAT WAS ONLY EVER STARTED COUNTS TOO. The merge step is
        # claimed on the build's stage log BEFORE the merge command is run,
        # and the merge command is the longest thing the press does, so a
        # press that is killed while it runs leaves an "about to join" line
        # and a claimed merge step and nothing else. Before this was
        # allowed for, that build could never be pressed again: every later
        # merge word fell into the repeated-merge-step refusal below and
        # nothing in the codebase ever released the claim on that path, so
        # the join sitting on the integration branch could not be picked up
        # and the build was stuck for good. An "about to join" with no
        # answer is exactly the case the pick-up was built for: the factory
        # looks at the world (is that branch a join of G and the build's
        # tip?) rather than assuming, so it is a pick-up, not a new merge.
        unfinished_already = already.unfinished() if already is not None else None
        picking_up = bool(
            already is not None
            and already.recorded
            and (
                (already.j_commit and already.is_done(STEP_JOIN))
                or (
                    unfinished_already is not None
                    and unfinished_already.step == STEP_JOIN
                )
            )
        )

        if not picking_up and _has_step(MERGE_STEP_MERGE_TARGET_IDENTIFIER):
            logger.error(
                "merge-executor: %s already has a merge step on record — refusing "
                "to run the merge twice",
                build_id,
            )
            # THE ROW IS WRITTEN EVEN THOUGH NOTHING HAPPENED (carried from
            # stage 4b's list). This is one of the two refusals that happen
            # BEFORE the lease, and it used to leave no publication record at
            # all — so "nobody ever pressed this build" and "it was pressed
            # and refused because it had already merged" read the same
            # afterwards. The merge word was given; the record says so.
            _write_the_refusal_down(
                store=store,
                target_branch=None,
                refusal=(
                    "a merge step is already on record for this build — "
                    "refusing to run it twice"
                ),
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

        if expected_candidate_branch and branch != expected_candidate_branch:
            return _could_not_check(
                f"the durable offer named branch {expected_candidate_branch}, "
                f"but the current build row selects {branch}"
            )

        # ------------------------------------------------------------------
        # The build's own tip. Everything after this joins THIS commit onto
        # the remote's, so it is read once and pinned against the offer.
        # ------------------------------------------------------------------
        gate_began = True
        candidate_sha = await git.rev_parse(branch)
        if not candidate_sha:
            # THE SECOND PRE-LEASE REFUSAL, and it leaves a row too (carried
            # from stage 4b's list). A branch that is not there is a merge
            # word that was given and refused, and the record has to say so.
            not_found = (
                f"the branch {branch} was not found {git_venue(git, repo_root)}"
            )
            _write_the_refusal_down(
                store=store, target_branch=None, refusal=not_found
            )
            return _could_not_check(not_found)
        gate["candidate_sha"] = candidate_sha
        offered_tree = await git.rev_parse(f"{candidate_sha}^{{tree}}")
        if expected_candidate_sha and candidate_sha != expected_candidate_sha:
            return _could_not_check(
                f"the offered branch moved from {expected_candidate_sha} to "
                f"{candidate_sha}; nothing was merged and its retained "
                "worktree is kept"
            )
        if expected_candidate_tree and offered_tree != expected_candidate_tree:
            return _could_not_check(
                f"the offered candidate tree moved from {expected_candidate_tree} "
                f"to {offered_tree}; nothing was merged and its "
                "retained worktree is kept"
            )

        # ------------------------------------------------------------------
        # A DRY RUN joins nothing and leaves no durable rows. It proves the
        # plumbing — the venue, the lay-out, the deploy stage's own dry mode —
        # against the build's own commit, and says so in its own words.
        # ------------------------------------------------------------------
        if dry_run:
            gate["candidate_tree"] = offered_tree
            laid = await _lay_the_tree_out(candidate_sha)
            if laid is not None:
                return laid
            checked_outcome = await _run_the_candidate_check(candidate_sha)
            if checked_outcome is not None:
                return checked_outcome
            _write_receipt(
                "merge_deploy_merge.json",
                {
                    "step": "join",
                    "dry_run": True,
                    "branch": branch,
                    "skipped": (
                        "dry run — nothing was joined and nothing was written "
                        "down; a real press would fetch the recorded target "
                        f"branch and join {branch} onto it in a working folder "
                        "of its own"
                    ),
                },
            )
            return MergeDeployOutcome(
                result=RESULT_WORD_PUBLICATION_PENDING,
                status="PASSED",
                detail=(
                    f"dry run — {named} was checked, nothing was joined and "
                    "nothing was written down."
                ),
                checks_passed=gate.get("checks_passed"),
                checks_total=gate.get("checks_total"),
                deployed_in=deployed_in_for(repo_root) if gate.get("ran") else None,
                gate_before_merge=_gate_for_report(),
            )

        # ------------------------------------------------------------------
        # WHICH BRANCH OF THE REMOTE, AND WHERE IT IS NOW. One target branch,
        # decided once: the name was written down when the work started, and
        # this press uses that name and no other. G is where it is now.
        # ------------------------------------------------------------------
        recorded_branch: str | None = None
        try:
            start_point = deps.pool.read_start_point(build_id)
            if getattr(start_point, "recorded", False):
                recorded_branch = getattr(start_point, "target_branch", None)
        except Exception as exc:  # noqa: BLE001 — an unreadable record is a refusal
            logger.warning(
                "merge-executor: the start point of %s could not be read (%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )
        where = await target_branch_now(git, recorded_branch=recorded_branch)
        if not where.ok:
            _write_receipt(
                "merge_deploy_join.json",
                {
                    "step": "join",
                    "refusal": where.refusal,
                    "recorded_target_branch": recorded_branch,
                    "nothing_was_joined": True,
                },
            )
            # THE ROW IS WRITTEN EVEN THOUGH NOTHING HAPPENED. The design
            # wants one row per build carrying who gave the merge word and
            # when, and this is a merge word: somebody pressed it and was
            # refused. Writing the refusal down before returning is what
            # makes the record a record of the DECISIONS as well as of the
            # steps — without it a build refused here has no row at all, and
            # "nobody ever pressed it" and "it was pressed and refused" read
            # the same afterwards.
            _write_the_refusal_down(
                store=None if dry_run else _publication_store(),
                target_branch=recorded_branch,
                refusal=str(where.refusal or "the remote could not be read"),
            )
            return _cannot_join(
                f"{named} could not be joined onto the remote: {where.refusal} "
                "Nothing was merged and the branch is kept."
            )
        g_commit = str(where.commit)
        target_branch = str(where.branch)

        # ------------------------------------------------------------------
        # THE RECORD. One worker per build: the lease and the turn number are
        # taken in one transaction before anything is done, and every write
        # after this carries that turn.
        # ------------------------------------------------------------------
        turn = 0
        record: Any = None
        if store is not None:
            grant = store.take_lease(
                build_id=build_id,
                holder=worker_name,
                now=deps.clock(),
                feature_id=feature_id,
                repo=repo,
                decided_by=decided_by,
                target_branch=target_branch,
            )
            if grant is None:
                current = store.read(build_id)
                return _cannot_join(
                    f"another worker ({current.lease_holder or 'unnamed'}) is "
                    f"already working on {named}'s publication record, so this "
                    "one left it alone. Nothing was merged."
                )
            turn = grant.turn
            record = store.read(build_id)

            # --------------------------------------------------------------
            # A SEND THAT MAY ALREADY HAVE HAPPENED IS SETTLED FIRST, BY
            # LOOKING — and BEFORE any field of the record is rewritten.
            #
            # It has to come first for two reasons. The join's own rule would
            # otherwise undo it: a send that landed moves the remote's branch
            # to the joined commit, so a join made onto the older commit is
            # no longer a join of what is true now, and the press would set
            # its own published join aside and make another one. And this
            # press's G — where the branch is NOW, which for a published
            # build is the joined commit itself — must not be written over
            # the G the join was really made onto, or the record would stop
            # saying what happened. Reading the remote first is the design's
            # own rule for the send ("read the remote FIRST; if it contains
            # J, mark published and carry on").
            # --------------------------------------------------------------
            settled = await _the_send_may_already_have_happened(
                record=record,
                store=store,
                turn=turn,
                g_commit=g_commit,
                target_branch=target_branch,
            )
            if settled is not None:
                return settled

            if not store.record(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                g_commit=g_commit,
                build_tip=candidate_sha,
                decided_by=decided_by,
                target_branch=target_branch,
                feature_id=feature_id,
                repo=repo,
            ):
                return _replaced_here()

        # ------------------------------------------------------------------
        # AT MOST THREE ATTEMPTS, and a new one only for one reason: the
        # remote moved under the send. Then the whole of it happens again —
        # the branch is fetched afresh, so there is a new G; the join is made
        # onto that, under a name of its own, so every joined commit of every
        # attempt is kept; and BOTH kinds of check run again on the new joined
        # result, because it is a different result. Every other refusal stops
        # at once: retrying it would only fail the same way.
        # ------------------------------------------------------------------
        for attempt_round in range(1, _how_many_attempts(deps.config) + 1):
            # ------------------------------------------------------------------
            # THE JOIN. A working folder of its own at G; the build system's
            # merge run in THERE, onto a branch of the factory's own. The
            # project's main copy is never switched, reset or merged into.
            # ------------------------------------------------------------------
            attempt = int(getattr(record, "attempt", 0) or 0)
            j_commit: str | None = None
            # THE JOIN THIS RECORD ALREADY HOLDS IS REUSED ONLY WHEN IT IS A JOIN
            # OF WHAT IS TRUE NOW. Not "the record says the join is done", and not
            # "the remote is still where it was": git is asked whether that commit
            # is a merge of exactly G **and the build's CURRENT tip**. Both halves
            # matter, and the second is the one that was missing: the build's
            # branch can gain a fix between two presses, and a join made onto the
            # older tip has a tree without it. Reusing it would check one tree and
            # publish another, and the record would then hold the new tip beside
            # the old join — breaking the very invariant the publisher is told to
            # verify ("J is a merge of G and the recorded build tip").
            #
            # Anything else is SET ASIDE: it keeps the branch and the folder its
            # own attempt gave it, nothing is deleted, the record's ``j_commit``
            # is cleared so no later reader can mistake it for this press's join,
            # and a fresh join is made on the next attempt's own name.
            if record is not None and record.is_done(STEP_JOIN) and record.j_commit:
                kept = await look_at_a_join(
                    git,
                    ref=str(record.j_commit),
                    g_commit=g_commit,
                    build_tip=candidate_sha,
                )
                if kept.is_the_join:
                    j_commit = record.j_commit
                    logger.info(
                        "merge-executor: %s's join (%s) is a merge of %s and the "
                        "build's tip %s — it is used, not made again",
                        feature_id,
                        str(j_commit)[:10],
                        g_commit[:10],
                        candidate_sha[:10],
                    )
                else:
                    why = kept.why or (
                        f"the recorded join {str(record.j_commit)[:10]} is not a "
                        f"merge of {g_commit[:10]} and {candidate_sha[:10]}"
                    )
                    logger.warning(
                        "merge-executor: %s's recorded join is not a join of what "
                        "is true now (%s) — it is set aside under its own name "
                        "(%s) and the join is made afresh",
                        feature_id,
                        why,
                        integration_branch(feature_id, attempt),
                    )
                    if store is not None and not store.done(
                        build_id=build_id,
                        turn=turn,
                        now=deps.clock(),
                        step=STEP_JOIN,
                        attempt=attempt,
                        result={
                            "set_aside": True,
                            "branch_kept": integration_branch(feature_id, attempt),
                            "commit_kept": record.j_commit,
                            "why": why,
                        },
                        # The record must not carry a join that is not this
                        # press's. It is cleared here and written again only when
                        # a fresh join is really made.
                        j_commit=None,
                    ):
                        return _replaced_here()
            # The MERGE COMMAND's own check counts — what the build system ran on
            # the joined result. They are not the live check's counts, which ride
            # the report as ``gate_before_merge``; a press that picked up a join
            # somebody else made has none of its own and says None.
            checks_passed: int | None = None
            checks_total: int | None = None
            # Did the build system's own post-merge checks run on THIS attempt's
            # joined commit? True when this press ran them; a press that picked a
            # join up does not, and says so.
            merge_checks_ran_here = False

            def _the_recorded_line(step: str) -> Any | None:
                """The last ``done`` line for this step, on THIS attempt and THIS J.

                Both halves of "on exactly this J" are checked: the attempt the
                line belongs to, and the joined commit the line says it ran on. A
                line from another attempt, or one that ran on a different joined
                commit, is somebody else's answer to somebody else's question.

                A LINE THAT NAMES NO COMMIT AT ALL IS NOT ABOUT THIS ONE EITHER
                (22 September 2026, the stage's reviewer). This used to let such
                a line through, and the publisher never did: it requires the
                line to say it ran on exactly this joined commit before it will
                count the step as passed. Two readers of one record have to read
                it the same way, or the press can call a join checked and the
                publisher then refuse to send it — the same record, two answers.
                A line with no commit on it is evidence about no commit, and the
                press now says so too.
                """
                if record is None or not j_commit:
                    return None
                found = None
                for line in getattr(record, "lines", ()):
                    if line.kind != LINE_DONE or line.step != step:
                        continue
                    if int(getattr(line, "attempt", -1)) != attempt:
                        continue
                    ran_on = str(
                        line.detail.get("ran_on") or line.detail.get("j_commit") or ""
                    )
                    if ran_on != str(j_commit):
                        continue
                    found = line
                return found

            def _the_recorded_step_passed(step: str) -> bool:
                """Did that step PASS on exactly this J? Not "was it written down".

                The existence of a ``done`` line says the step finished, not that
                it was green. A red run writes a ``done`` line too — with
                ``verify_ok`` false — and counting it as "checked" launders a red
                set of checks into a word a person trusts.
                """
                line = _the_recorded_line(step)
                if line is None:
                    return False
                return line.detail.get("verify_ok") is True

            def _why_the_recorded_step_failed(step: str) -> str | None:
                """The plain reason a recorded step did not pass, or ``None``."""
                line = _the_recorded_line(step)
                if line is None or line.detail.get("verify_ok") is not False:
                    return None
                return str(
                    line.detail.get("verify_detail")
                    or line.detail.get("verify_status")
                    or "verification failed"
                )

            def _the_build_systems_checks_ran_on_j() -> bool:
                if merge_checks_ran_here:
                    return True
                return _the_recorded_step_passed(STEP_MERGE_CHECKS)

            unfinished = record.unfinished() if record is not None else None
            if j_commit is None and unfinished is not None and unfinished.step == STEP_JOIN:
                # The last line says a join was about to happen and never said
                # what came of it. Look, do not assume — and look with the commits
                # that are true NOW, never the ones that line was written with.
                # Skipped when the recorded join above was already confirmed to be
                # a join of what is true now: that question has been answered.
                leftover = await look_at_the_leftover_join(
                    git,
                    feature_id=feature_id,
                    attempt=unfinished.attempt,
                    g_commit=g_commit,
                    build_tip=candidate_sha,
                )
                if leftover.is_the_join:
                    j_commit = leftover.commit
                    attempt = unfinished.attempt
                    if store is not None and not store.done(
                        build_id=build_id,
                        turn=turn,
                        now=deps.clock(),
                        step=STEP_JOIN,
                        attempt=attempt,
                        result={
                            "j_commit": j_commit,
                            "found_by_looking": True,
                            "detail": (
                                "the join had already been made when the run "
                                "stopped; it was found, not made again"
                            ),
                        },
                        j_commit=j_commit,
                    ):
                        return _replaced_here()
                    logger.info(
                        "merge-executor: %s's join was already made (%s) — found by "
                        "looking, not made again",
                        feature_id,
                        str(j_commit)[:10],
                    )
                else:
                    # Anything else is a leftover: it keeps the name its own
                    # attempt gave it, nothing is deleted, and the join is made
                    # afresh on the next attempt's own name.
                    attempt = int(unfinished.attempt)
                    logger.warning(
                        "merge-executor: %s's attempt %s left something behind (%s) "
                        "— it is set aside under its own name and the join is made "
                        "afresh",
                        feature_id,
                        attempt,
                        leftover.why,
                    )
                    if store is not None and not store.done(
                        build_id=build_id,
                        turn=turn,
                        now=deps.clock(),
                        step=STEP_JOIN,
                        attempt=attempt,
                        result={
                            "set_aside": True,
                            "branch_kept": integration_branch(feature_id, attempt),
                            "why": leftover.why,
                        },
                    ):
                        return _replaced_here()

            if j_commit is None:
                attempt = attempt + 1
                # WHERE THE FOLDER WILL BE, AS THIS SIDE WORKS IT OUT. The "about
                # to" line has to be written before the folder is made, so this is
                # the only path it can carry — and for a repository that lives in
                # a sandbox it is this side's path, not the one the folder really
                # has in there. The venue's own answer replaces it below and goes
                # on the "done" line; the field on the "about to" line is named
                # ``working_folder_expected`` so the two are not read as one fact.
                #
                # LEFT FOR THE EXECUTOR STAGE, and named here rather than found
                # then: this folder is NEVER REMOVED. Every attempt makes one and
                # every one of them stays, because the design keeps each joined
                # commit under a name of its own until the build's record reaches
                # its end — and its end is a publication this version cannot
                # perform. The venue already has ``remove_working_folder``; the
                # stage that can finish a record is the stage that may call it,
                # and it has to leave the branch alone when it does.
                folder = working_folder_path(repo_root, feature_id, attempt)
                if store is not None and not store.about_to(
                    build_id=build_id,
                    turn=turn,
                    now=deps.clock(),
                    step=STEP_JOIN,
                    attempt=attempt,
                    inputs=join_inputs(
                        feature_id=feature_id,
                        attempt=attempt,
                        target_branch=target_branch,
                        g_commit=g_commit,
                        build_tip=candidate_sha,
                        branch_to_merge=branch,
                        folder=folder,
                    ),
                ):
                    return _replaced_here()

                made = await make_the_working_folder(
                    git,
                    repo_root=repo_root,
                    feature_id=feature_id,
                    attempt=attempt,
                    at_commit=g_commit,
                )
                if not made.ok:
                    _write_receipt(
                        "merge_deploy_join.json",
                        {
                            "step": "join",
                            "attempt": attempt,
                            "refusal": made.refusal,
                            "nothing_was_joined": True,
                        },
                    )
                    return _cannot_join(
                        f"{named} could not be joined onto {target_branch}: "
                        f"{made.refusal}. Nothing was merged and the branch is kept."
                    )
                folder = str(made.path)
                join_branch = str(made.branch)

                _claim_step(
                    MERGE_STEP_MERGE_TARGET_IDENTIFIER,
                    {
                        "merge_step": {
                            "expect_main_sha": expect_main_sha,
                            "decided_by": decided_by,
                            "dry_run": dry_run,
                            "candidate_sha": candidate_sha,
                            "branch": branch,
                            "target_branch": target_branch,
                            "g_commit": g_commit,
                            "integration_branch": join_branch,
                            "working_folder": folder,
                            "attempt": attempt,
                        }
                    },
                )

                verify_timeout = _verify_timeout_from(deps.config)
                merge_wall = merge_wall_seconds(verify_timeout)
                args = [
                    "merge",
                    feature_id,
                    # The join goes onto the factory's own branch, at G, and never
                    # into the project's own trunk.
                    "--target",
                    join_branch,
                    # ...pinned to where that branch is, which is G.
                    "--expect-main-sha",
                    g_commit,
                    # ...in the working folder made for it, so the project's main
                    # copy is never switched.
                    "--in-worktree",
                    folder,
                    # How long ONE run of the checks may take. The wall below holds
                    # the whole command: two such runs plus the merge between them.
                    "--verify-timeout",
                    str(verify_timeout),
                    "--json",
                ]
                # Part M, rule 54: the merge command is told the branch ONLY when
                # the build row recorded one (a repair's own journey branch).
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
                merged_in_report = bool(report and report.get("outcome") == "merged")
                refusal: str | None = None
                result_status = getattr(result, "status", "failed")
                stderr = (getattr(result, "stderr", None) or "").strip()
                tail = (getattr(result, "stdout_tail", "") or "").strip()
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
                        spoken = report.get("refusal_reason")
                        if isinstance(spoken, str) and spoken.strip():
                            refusal = spoken.strip()
                        else:
                            refusal = (
                                _conflict_sentence(report)
                                or _report_refusal(report)
                                or generic
                            )
                    else:
                        refusal = generic

                # THE JOIN MAY HAVE BEEN MADE ANYWAY. A command that was killed,
                # or that died before it could print its report, leaves no answer
                # at all — but git knows. Ask git before calling a made join a
                # refusal.
                landed = await look_at_the_leftover_join(
                    git,
                    feature_id=feature_id,
                    attempt=attempt,
                    g_commit=g_commit,
                    build_tip=candidate_sha,
                )
                joined_but_unchecked = False
                if refusal and landed.is_the_join:
                    j_commit = landed.commit
                    refusal = None
                    joined_but_unchecked = True
                    logger.warning(
                        "merge-executor: %s's merge command gave no usable answer "
                        "(%s), but the join was made (%s) — it is not made again",
                        feature_id,
                        own_sentence,
                        str(j_commit)[:10],
                    )
                elif not refusal:
                    j_commit = _report_sha(report) or landed.commit

                merge_receipt: dict[str, Any] = {
                    "step": "join",
                    "attempt": attempt,
                    "status": result_status,
                    "exit_code": getattr(result, "exit_code", None),
                    "branch": branch,
                    "target_branch": target_branch,
                    "integration_branch": join_branch,
                    "working_folder": folder,
                    "g_commit": g_commit,
                    "build_tip": candidate_sha,
                    "j_commit": j_commit,
                    "refusal": refusal,
                    "report": report,
                    "stdout_tail": (getattr(result, "stdout_tail", "") or "")[-4000:],
                    "baseline_file": str(baseline_path) if baseline_path else None,
                }
                _write_receipt("merge_deploy_merge.json", merge_receipt)

                if refusal or not j_commit:
                    # Nothing was joined — a conflict, a refusal, a command that
                    # would not run. It is reported as it always was, the branch
                    # is kept, and nothing else happens.
                    sentence = refusal or (
                        "the merge command reported no joined commit, so there is "
                        "nothing to check"
                    )
                    _release_step(MERGE_STEP_MERGE_TARGET_IDENTIFIER, sentence)
                    if store is not None:
                        store.done(
                            build_id=build_id,
                            turn=turn,
                            now=deps.clock(),
                            step=STEP_JOIN,
                            attempt=attempt,
                            result={"joined": False, "refusal": sentence},
                        )
                    return MergeDeployOutcome(
                        result="merge-refused",
                        status="FAILED",
                        failed_step="merge",
                        detail=sentence,
                    )

                # THE JOIN WAS MADE BUT ITS CHECKS NEVER FINISHED. The command
                # was killed, or died before it could say anything: the joined
                # commit is there, and the build system's own checks on it are
                # not. That is not a pass, and it is not a refusal either. J is
                # kept under its own name, the record says the checks did not
                # finish, and nothing is published.
                if joined_but_unchecked:
                    if store is not None:
                        store.done(
                            build_id=build_id,
                            turn=turn,
                            now=deps.clock(),
                            step=STEP_JOIN,
                            attempt=attempt,
                            result={"j_commit": j_commit, "checks_finished": False},
                            j_commit=j_commit,
                        )
                        store.record(
                            build_id=build_id,
                            turn=turn,
                            now=deps.clock(),
                            result=RESULT_PUBLICATION_PENDING,
                        )
                    return MergeDeployOutcome(
                        result="merged-verify-failed",
                        status="FAILED",
                        merged_sha=j_commit,
                        failed_step="verify",
                        detail=(
                            f"{feature_id} joined ({(j_commit or '')[:10]}), but the "
                            f"checks after the join could not finish: "
                            f"{own_sentence}. Nothing was published."
                        ),
                        verify_status="unverified",
                    )

                # The build system's own checks after the merge ran on J, in the
                # working folder, as part of the same command.
                #
                # THIS STEP HAS NO "ABOUT TO" LINE, and that is on purpose here
                # and worth naming for the executor stage. The checks are not a
                # step this press starts: they are inside the merge command, so
                # the "about to join" line already says everything about to
                # happen, and a second "about to" written after the command
                # returned would be a line saying "about to" about something that
                # had already finished. The cost is that a press killed inside the
                # command cannot tell, from the record alone, whether the checks
                # ran — which is exactly why the pick-up asks git instead. Any
                # stage that moves these checks OUT of the merge command owes them
                # their own "about to" line.
                checks_passed = _report_int(report, "checks_passed")
                checks_total = _report_int(report, "checks_total")
                if store is not None and not store.done(
                    build_id=build_id,
                    turn=turn,
                    now=deps.clock(),
                    step=STEP_JOIN,
                    attempt=attempt,
                    result={
                        "j_commit": j_commit,
                        "integration_branch": join_branch,
                        "working_folder": folder,
                        "verify_status": (report or {}).get("verify_status"),
                        "verify_ok": (report or {}).get("verify_ok"),
                    },
                    j_commit=j_commit,
                ):
                    return _replaced_here()
                if store is not None and not store.done(
                    build_id=build_id,
                    turn=turn,
                    now=deps.clock(),
                    step=STEP_MERGE_CHECKS,
                    attempt=attempt,
                    result={
                        "ran_on": j_commit,
                        "verify_ran": (report or {}).get("verify_ran"),
                        "verify_ok": (report or {}).get("verify_ok"),
                        "verify_status": (report or {}).get("verify_status"),
                        "verify_detail": (report or {}).get("verify_detail"),
                        "checks_passed": checks_passed,
                        "checks_total": checks_total,
                    },
                ):
                    return _replaced_here()
                merge_checks_ran_here = True

                # Advisory: does the joined tree keep the promises in the feature's
                # spec digest? Deterministic and never blocking.
                try:
                    conformance = run_digest_conformance(
                        repo_root=repo_root, feature_id=feature_id
                    )
                except Exception as exc:  # noqa: BLE001 — advisory must never stop a join
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
                    could_not_run = (
                        str(report.get("verify_status") or "").strip().lower()
                        == "unverified"
                    )
                    why = str(
                        report.get("verify_detail")
                        or report.get("verify_status")
                        or "verification failed"
                    )
                    if could_not_run:
                        detail = (
                            f"{feature_id} was joined onto {target_branch} "
                            f"({(j_commit or '')[:10]}), but the checks after the "
                            f"join could not run: {why}. Nothing was published."
                        )
                    else:
                        detail = (
                            f"{feature_id} was joined onto {target_branch} "
                            f"({(j_commit or '')[:10]}), but the checks after the "
                            f"join did not pass: {why}"
                            + (f" — {len(charged)} charged failure(s)" if charged else "")
                            + ". Nothing was published."
                        )
                    if store is not None:
                        store.record(
                            build_id=build_id,
                            turn=turn,
                            now=deps.clock(),
                            result=RESULT_PUBLICATION_PENDING,
                        )
                    return MergeDeployOutcome(
                        result="merged-verify-failed",
                        status="FAILED",
                        merged_sha=j_commit,
                        failed_step="verify",
                        detail=detail,
                        checks_passed=checks_passed,
                        checks_total=checks_total,
                        verify_status="unverified" if could_not_run else "failed",
                    )

            # ------------------------------------------------------------------
            # A RED SET OF CHECKS STAYS RED. This press may have picked up a join
            # an earlier one made; if the earlier press ran the build system's
            # checks on exactly this joined commit and they went red, that is
            # still the answer. It is not re-run here — the checks live inside the
            # merge command, and running that command again would try to merge
            # again — so there is nothing that could have changed it. The result
            # says what happened, word for word as the press that ran them said
            # it, and it stays the result until a NEW attempt's checks pass.
            # ------------------------------------------------------------------
            recorded_why = _why_the_recorded_step_failed(STEP_MERGE_CHECKS)
            if recorded_why is not None and not merge_checks_ran_here:
                detail = (
                    f"{feature_id} was joined onto {target_branch} "
                    f"({(j_commit or '')[:10]}), but the checks after the join "
                    f"did not pass: {recorded_why}. Nothing was published."
                )
                logger.warning(
                    "merge-executor: %s's join (%s) was checked by an earlier "
                    "press and went red (%s) — this press re-runs nothing and "
                    "says so; it is not 'checked'",
                    feature_id,
                    str(j_commit)[:10],
                    recorded_why,
                )
                if store is not None:
                    store.record(
                        build_id=build_id,
                        turn=turn,
                        now=deps.clock(),
                        result=RESULT_PUBLICATION_PENDING,
                    )
                recorded_line = _the_recorded_line(STEP_MERGE_CHECKS)
                return MergeDeployOutcome(
                    result="merged-verify-failed",
                    status="FAILED",
                    merged_sha=j_commit,
                    failed_step="verify",
                    detail=detail,
                    checks_passed=(
                        recorded_line.detail.get("checks_passed")
                        if recorded_line is not None
                        else None
                    ),
                    checks_total=(
                        recorded_line.detail.get("checks_total")
                        if recorded_line is not None
                        else None
                    ),
                    verify_status="failed",
                )

            # ------------------------------------------------------------------
            # THE FACTORY'S OWN LIVE CHECK, ON J AND ONLY ON J. Its exact tree is
            # laid out with the same operation the branch's tree used to be, and
            # the registered live checks run against that.
            #
            # IT IS RUN ON EVERY PRESS, and never inherited from the record. That
            # is what makes the same rule as the one above unnecessary here: there
            # is no path on which a recorded verdict for this step is read and
            # believed, so a red one cannot be laundered into "checked" either.
            # The guard is the ``done`` line below being written from THIS run's
            # verdict, and a test pins that a red recorded check is not inherited.
            # ------------------------------------------------------------------
            j_tree = await git.rev_parse(f"{j_commit}^{{tree}}")
            gate["candidate_sha"] = j_commit
            gate["candidate_tree"] = j_tree
            # What landed IS what is checked: there is one tree, J's, and both
            # the comparison and the check are about it.
            gate["merged_tree"] = j_tree
            gate["trees_match"] = bool(j_tree)

            if store is not None and not store.about_to(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                step=STEP_CANDIDATE_CHECK,
                attempt=attempt,
                inputs={
                    "j_commit": j_commit,
                    "j_tree": j_tree,
                    "feature_id": feature_id,
                },
            ):
                return _replaced_here()

            laid = await _lay_the_tree_out(str(j_commit))
            if laid is not None:
                return laid
            checked_outcome = await _run_the_candidate_check(str(j_commit))
            if checked_outcome is not None:
                return checked_outcome

            what_was_checked = {
                "j_commit": j_commit,
                "j_tree": j_tree,
                "build_tip": candidate_sha,
                "tree_path": str(tree_path) if tree_path else None,
                "verdict": gate.get("verdict"),
                # THE VERDICT, AS A PLAIN YES OR NO, under the same name the
                # build system's own checks are recorded with. The publisher
                # reads the record for itself and asks one question of both
                # kinds of check — "did it PASS on exactly this joined
                # commit?" — and a line it has to interpret differently for
                # each is a line it can get wrong. This press only reaches
                # here when the check passed (a refusal returns above), so it
                # is always True where it is written; it is written anyway,
                # because the publisher's rule is "verify_ok is exactly
                # True", and a step with no verdict must not read as a pass.
                "verify_ok": gate.get("verdict") == "pass",
                "checks_passed": gate.get("checks_passed"),
                "checks_total": gate.get("checks_total"),
                # THE IDENTITY THE EXISTING DEPLOY USES TODAY, recorded as it is.
                # It is a shared name that another build can overwrite, which is
                # exactly why the design's section C replaces it with an identity
                # that cannot be reused — in the NEXT stage. Here it is recorded
                # and the gap is named, not papered over.
                "identity": _the_identity_the_deploy_uses_today(),
            }
            if store is not None and not store.done(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                step=STEP_CANDIDATE_CHECK,
                attempt=attempt,
                result=what_was_checked,
                checked=what_was_checked,
            ):
                return _replaced_here()

            # ------------------------------------------------------------------
            # IS PUBLICATION SWITCHED ON? Two things have to be true: a setting
            # says so, and the activation check's five conditions all hold
            # (the design's section G). With either missing, nothing is sent
            # anywhere, nothing is deployed, and the record stops at "checked"
            # with the reason said in plain words.
            # ------------------------------------------------------------------
            if not publication_is_switched_on(
                deps.config, deps.what_the_machine_says
            ):
                if store is not None and not store.record(
                    build_id=build_id,
                    turn=turn,
                    now=deps.clock(),
                    result=RESULT_PUBLICATION_PENDING,
                ):
                    return _replaced_here()
                checks = (
                    f" — checks {gate['checks_passed']}/{gate['checks_total']}"
                    if isinstance(gate.get("checks_passed"), int)
                    and isinstance(gate.get("checks_total"), int)
                    else ""
                )
                # SAY WHICH CHECKS RAN. Two different things check the joined
                # result: the build system's own checks, which run inside the
                # merge command, and the factory's live check on J's tree. A
                # press that picked an already-made join up never re-runs the
                # first of those, and the counts in the sentence are the live
                # check's alone — so the sentence must not call that "checked"
                # flatly. The record is the thing a publisher reads; the
                # sentence is the thing a person reads, and it says the same.
                both_kinds_ran = _the_build_systems_checks_ran_on_j()
                # WHY IT IS OFF, in the sentence a person reads. "Publication
                # is switched off" on its own tells somebody nothing they can
                # act on; the reason names the setting that is not set, or the
                # one of section G's five conditions that does not hold.
                why_off = why_publication_is_off(
                    deps.config, deps.what_the_machine_says
                )
                if both_kinds_ran:
                    detail = (
                        f"{named} was joined onto {target_branch} at "
                        f"{g_commit[:10]} in a working folder of its own, and "
                        f"the joined result {str(j_commit)[:10]} was checked{checks}. "
                        "It is checked and ready to publish; publication is "
                        f"switched off ({why_off}), so nothing was sent to the "
                        "remote and nothing was deployed. The branch is kept."
                    )
                else:
                    # NOT READY (22 September 2026, the second reviewer's first
                    # finding). This press picked up a join an earlier one made;
                    # reuse means the merge command is not run again, and the build
                    # system's own checks after a join live inside it, so they have
                    # NEVER run on this J. One kind of check is not "checked". The
                    # result must not read as a pass: the record already says so
                    # (no done merge-checks line), and the sentence and the status
                    # say the same. Running those checks on a J the press did not
                    # just make is the executor stage's to build.
                    detail = (
                        f"{named} was joined onto {target_branch} at "
                        f"{g_commit[:10]} in a working folder of its own; the "
                        f"factory's own live check ran on the joined result "
                        f"{str(j_commit)[:10]}{checks}, but the build system's "
                        "checks after a join have not run on it, because this "
                        "press picked up a join an earlier one had already made "
                        "and does not run the merge command again. It is NOT yet "
                        "checked and not ready to publish. Nothing was sent to the "
                        "remote and nothing was deployed. The branch and the join "
                        "are kept."
                    )
                _write_receipt(
                    "merge_deploy_publication.json",
                    {
                        "step": "publication",
                        "switched_on": False,
                        "why_publication_is_off": why_off,
                        "result": RESULT_PUBLICATION_PENDING,
                        "target_branch": target_branch,
                        "g_commit": g_commit,
                        "build_tip": candidate_sha,
                        "j_commit": j_commit,
                        "attempt": attempt,
                        "turn": turn,
                        "checked": what_was_checked,
                        "both_kinds_of_check_ran_on_j": both_kinds_ran,
                        "ready_to_publish": both_kinds_ran,
                    },
                )
                return MergeDeployOutcome(
                    result=RESULT_WORD_PUBLICATION_PENDING,
                    # A pass only when BOTH kinds of check ran and passed on J.
                    status="PASSED" if both_kinds_ran else "GATED",
                    merged_sha=j_commit,
                    detail=detail,
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                    deployed_in=deployed_in_for(repo_root) if gate.get("ran") else None,
                    gate_before_merge=_gate_for_report(),
                )

            # ------------------------------------------------------------------
            # NOTHING GOES TO THE REMOTE THAT WAS NOT WHOLLY CHECKED. A press
            # that picked a join up does not re-run the build system's own
            # checks — they live inside the merge command — so one of the two
            # kinds has never run on this joined result. That is not
            # "checked", and it is certainly not something to publish. The
            # publisher would refuse it anyway, reading the record for itself;
            # this refuses it before it is ever asked, and says the same thing.
            # ------------------------------------------------------------------
            if not _the_build_systems_checks_ran_on_j():
                return _not_wholly_checked(
                    j_commit=str(j_commit),
                    target_branch=target_branch,
                    g_commit=g_commit,
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                )

            # ------------------------------------------------------------------
            # THE SEND. Said before it is done, then done, then said again.
            # The publisher is a separate process holding the one credential
            # that can write to the remote; the coordinator holds none and
            # this request carries none. What it carries is the turn number,
            # so that a worker which has been replaced cannot send: the
            # publisher reads the record's current turn for itself and refuses
            # anything older.
            # ------------------------------------------------------------------
            asked = {
                "project": repo,
                "build_id": build_id,
                "turn": turn,
                "j_commit": str(j_commit),
                "target_branch": target_branch,
            }
            if store is not None and not store.about_to(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                step=STEP_SEND,
                attempt=attempt,
                inputs=dict(asked),
            ):
                return _replaced_here()

            answer = await _ask_for_the_send(asked)
            published = bool(answer.get("published")) and bool(
                answer.get("contains_j")
            )
            _write_receipt(
                "merge_deploy_send.json",
                {
                    "step": "send",
                    "attempt": attempt,
                    "attempt_round": attempt_round,
                    "asked": asked,
                    "answer": answer,
                    "published": published,
                },
            )
            if store is not None and not store.done(
                build_id=build_id,
                turn=turn,
                now=deps.clock(),
                step=STEP_SEND,
                attempt=attempt,
                result={
                    "ran_on": str(j_commit),
                    "published": published,
                    "remote_now": answer.get("remote_now"),
                    "contains_j": bool(answer.get("contains_j")),
                    "refusal": answer.get("refusal"),
                    "refusal_kind": answer.get("refusal_kind"),
                    "attempt_round": attempt_round,
                },
            ):
                return _replaced_here()

            if published:
                # PUBLISHED IS NOT PERMISSION TO DEPLOY (the design's section
                # B), so the deploy is its own decision, made under the
                # deployment lock and only forwards.
                return await _deploy_what_was_checked(
                    j_commit=str(j_commit),
                    j_tree=j_tree,
                    target_branch=target_branch,
                    g_commit=g_commit,
                    remote_now=str(answer.get("remote_now") or ""),
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                    what_was_checked=what_was_checked,
                    attempt=attempt,
                    turn=turn,
                    store=store,
                )

            # -- it was not sent. Is this the ONE refusal worth trying again? --
            why_not = str(
                answer.get("refusal") or "the publisher gave no reason"
            )
            allowed = _how_many_attempts(deps.config)
            if the_remote_moved(answer) and attempt_round >= allowed:
                # THE LAST ATTEMPT, and the remote moved again. The reason a
                # person reads has to say that as well as what the publisher
                # said, or it reads as one unlucky send rather than as the
                # end of what this merge word was allowed to try.
                why_not = (
                    f"{why_not} The remote's branch moved under every one of "
                    f"the {allowed} attempts this merge word is allowed, so "
                    "the work has not been joined onto where the branch is "
                    "now."
                )
            if the_remote_moved(answer) and attempt_round < allowed:
                logger.warning(
                    "merge-executor: %s's send was refused because the remote "
                    "moved (%s) — attempt %s of %s: the join is set aside "
                    "under its own name and a new one is made onto where the "
                    "branch is now",
                    feature_id,
                    why_not,
                    attempt_round + 1,
                    _how_many_attempts(deps.config),
                )
                again = await _start_another_attempt(
                    recorded_branch=recorded_branch, store=store, turn=turn
                )
                if isinstance(again, MergeDeployOutcome):
                    return again
                g_commit, target_branch, record = again
                continue

            return _publication_pending(
                j_commit=str(j_commit),
                target_branch=target_branch,
                g_commit=g_commit,
                why_not=why_not,
                answer=answer,
                checks_passed=checks_passed,
                checks_total=checks_total,
                what_was_checked=what_was_checked,
                attempt=attempt,
                attempt_round=attempt_round,
                turn=turn,
                store=store,
            )

        # ------------------------------------------------------------------
        # THE LOOP DOES NOT FALL THROUGH. Every round ends in a return: the
        # last attempt's refusal is returned by the branch above, which says
        # the attempts ran out as well as what the publisher said. This is
        # here because Python needs a value, and because a press must never
        # answer nothing at all — not because anything reaches it.
        # ------------------------------------------------------------------
        return _publication_pending(
            j_commit=str(j_commit) if j_commit else None,
            target_branch=target_branch,
            g_commit=g_commit,
            why_not=(
                f"this merge word ran out of the {_how_many_attempts(deps.config)} "
                "attempts it is allowed without reaching an answer"
            ),
            answer={"refusal_kind": "the-attempts-ran-out"},
            checks_passed=checks_passed,
            checks_total=checks_total,
            what_was_checked=what_was_checked,
            attempt=attempt,
            attempt_round=_how_many_attempts(deps.config),
            turn=turn,
            store=store,
        )

    async def _retire_the_joins_working_folders(record: Any) -> list[dict[str, Any]]:
        """Remove every attempt's working folder — and ONLY at the record's end.

        Carried from stage 4a, which made these folders and never removed one,
        because the design keeps each joined commit under a name of its own
        until the build's record reaches its end, and no earlier version could
        reach it. The end is reachable now: the joined commit is on the remote
        and what was checked is running.

        THE BRANCH IS LEFT ALONE. Only the working FOLDER goes. Every joined
        commit of every attempt stays exactly where it is, on the branch its
        own attempt named, which is what "kept until the record's end" was
        protecting — a folder is scaffolding, a commit is the work.
        """
        retired: list[dict[str, Any]] = []
        highest = int(getattr(record, "attempt", 0) or 0) if record is not None else 0
        for attempt_number in range(1, max(highest, 1) + 1):
            folder = working_folder_path(repo_root, feature_id, attempt_number)
            try:
                removed = await git.remove_working_folder(folder)
            except Exception as exc:  # noqa: BLE001 — never costs a result
                retired.append(
                    {
                        "attempt": attempt_number,
                        "folder": folder,
                        "removed": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            retired.append(
                {
                    "attempt": attempt_number,
                    "folder": folder,
                    "removed": bool(removed),
                    "branch_kept": integration_branch(feature_id, attempt_number),
                }
            )
        return retired

    try:
        outcome = await _press()
    finally:
        # Every ending, the normal ones and a crash alike: the candidate down
        # if it is still standing, and its laid-out tree removed — BEFORE the
        # report goes out, so the report never says "torn down" ahead of time.
        await _cleanup()
    # THE RECORD'S END, and the only place anything the press made is removed.
    # It is reachable now: the joined commit is on the remote's recorded
    # branch and what was checked is running, confirmed from the identity the
    # running thing reported. The JOIN'S working folders go here — every
    # attempt's, the branches left exactly as they are — and the build's own
    # retained folder is retired below on the same word.
    if not dry_run and outcome.result == RESULT_WORD_MERGED_AND_RUNNING:
        try:
            store_now = _publication_store()
            record_now = store_now.read(build_id) if store_now is not None else None
        except Exception:  # noqa: BLE001 — a tidy-up never costs a result
            record_now = None
        _write_receipt(
            "merge_deploy_join_folders.json",
            {
                "step": "retire-the-join-folders",
                "why_now": (
                    "the build's record has reached its end: the joined "
                    "commit is on the remote and what was checked is running"
                ),
                "branches_kept": True,
                "folders": await _retire_the_joins_working_folders(record_now),
            },
        )
    if (
        not dry_run
        and outcome.result == RESULT_WORD_MERGED_AND_RUNNING
        and isinstance(worktree_retention, dict)
    ):
        current_sha = await git.rev_parse(branch)
        current_tree = (
            await git.rev_parse(f"{current_sha}^{{tree}}") if current_sha else None
        )
        if (
            current_sha != expected_candidate_sha
            or current_tree != expected_candidate_tree
        ):
            retired = {
                "status": "kept",
                "build_id": build_id,
                "path": worktree_retention.get("path"),
                "detail": (
                    "candidate ref/tree diverged after the successful press; "
                    "the retained worktree was preserved"
                ),
                "offered_candidate_sha": expected_candidate_sha,
                "current_candidate_sha": current_sha,
                "offered_candidate_tree": expected_candidate_tree,
                "current_candidate_tree": current_tree,
            }
        else:
            retired = await git.retire_autobuild_worktree(
                build_id,
                str(worktree_retention.get("path") or ""),
                worktree_retention,
            )
        _write_receipt("autobuild_worktree_cleanup.json", retired)
        if retired.get("status") != "removed":
            logger.warning(
                "merge-executor: retained autobuild worktree for %s was kept: %s",
                build_id,
                retired.get("detail"),
            )
    return await _publish_report(outcome)


# ---------------------------------------------------------------------------
# The in-daemon deploy dispatcher (mirrors cli/_deploy_run.py exactly)
# ---------------------------------------------------------------------------


def _the_builds_declarations(
    db_path: Any, build_id: str
) -> tuple[str | None, tuple[str, ...]]:
    """This build's memory name and its project's declared setting NAMES.

    Read straight off the ledger, on a connection of its own that is closed
    again, because the deploy dispatch is handed a path rather than a facade.
    Nothing here guesses: a ledger that recorded neither answers ``(None, ())``,
    which is the factory's own launch list and memory explicitly off — the
    honest state, and never somebody else's project name.
    """
    if db_path is None:
        return None, ()
    try:
        from forge.adapters.sqlite.connect import connect_writer
        from forge.lifecycle.persistence import SqliteLifecyclePersistence

        connection = connect_writer(db_path)
        try:
            facade = SqliteLifecyclePersistence(connection=connection, db_path=db_path)
            name = facade.read_memory_project(build_id)
            names = tuple(str(entry) for entry in facade.read_launch_settings(build_id))
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 — a launch detail never stops a press
        logger.warning(
            "merge-deploy: what %s's project declared could not be read off "
            "the ledger (%s: %s) — the live check's driver runs with the "
            "factory's own list and memory off",
            build_id,
            type(exc).__name__,
            exc,
        )
        return None, ()
    return (str(name or "").strip() or None), names


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
        deploy_ownership: dict[str, Any] | None = None,
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
        # WHAT THE PROJECT DECLARED FOR THIS BUILD, read once. The live check
        # needs it, and since 23 September 2026 so does the deploy step: its
        # environment is built from the factory's named list plus these, never
        # copied from whatever this process holds.
        build_memory, build_declared = _the_builds_declarations(db_path, build_id)
        if spec is not None and sandbox is not None:
            # WHAT THE PROJECT DECLARED FOR THIS BUILD, off the ledger and
            # sent with the gate's request (22 September 2026). The live check
            # runs the project's OWN driver inside its sandbox, so it is the
            # project's own declarations that decide what that driver needs —
            # and until this they were stripped on the way through: a driver
            # needing a toolchain setting the project declared did not get it,
            # and one that launches the build system got memory off. Nothing
            # recorded reads as "the factory's own list and memory off", which
            # is the honest state rather than a guessed name.
            memory_name, declared_names = build_memory, build_declared
            invoker = SidecarLiveGateInvoker(
                base_url=str(sandbox.sidecar_url),
                repo=repo,
                repo_path=repo_root,
                driver_argv=list(spec.driver),
                timeout_seconds=spec.timeout_seconds,
                extra_env=dict(spec.env),
                memory_project=memory_name,
                launch_settings=declared_names,
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
            # WHO OWNS THE TARGET THIS LEG CHANGES (the design's H, I and J),
            # and what the project declared, so the deploy step is handed the
            # identity it must deploy and an environment that was built rather
            # than copied.
            deploy_ownership=deploy_ownership,
            memory_project=build_memory,
            launch_settings=build_declared,
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
        expected_candidate_sha = str(offer.get("candidate_sha") or "") or None
        expected_candidate_tree = str(offer.get("candidate_tree") or "") or None
        candidate_identity_version = offer.get("candidate_identity_version")
        offered_candidate_branch = str(offer.get("branch") or "").strip() or None
        expected_candidate_branch = (
            offered_candidate_branch if candidate_identity_version == 1 else None
        )
        if candidate_identity_version == 1 and (
            not expected_candidate_sha
            or not expected_candidate_tree
            or not expected_candidate_branch
        ):
            logger.error(
                "merge-executor: %s approved but its versioned offer carries "
                "no exact candidate branch/sha/tree — refusing an unpinned candidate",
                request_id,
            )
            return
        worktree_retention = offer.get("worktree_retention")
        if worktree_retention is not None and not isinstance(worktree_retention, dict):
            logger.error(
                "merge-executor: %s approved but its retained worktree identity "
                "is malformed",
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
                expected_candidate_sha=expected_candidate_sha,
                expected_candidate_tree=expected_candidate_tree,
                expected_candidate_branch=expected_candidate_branch,
                worktree_retention=worktree_retention,
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
        expected_candidate_sha: str | None = None,
        expected_candidate_tree: str | None = None,
        expected_candidate_branch: str | None = None,
        worktree_retention: dict[str, Any] | None = None,
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
                    expected_candidate_sha=expected_candidate_sha,
                    expected_candidate_tree=expected_candidate_tree,
                    expected_candidate_branch=expected_candidate_branch,
                    worktree_retention=worktree_retention,
                )
            except Exception as exc:  # noqa: BLE001 — the task must not die silent
                logger.error(
                    "merge-executor: executor raised (%s) for build %s — see "
                    "the receipts under merge-%s",
                    exc,
                    build_id,
                    build_id,
                )
