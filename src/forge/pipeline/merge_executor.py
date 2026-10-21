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
default, and what every forge does until the activation check's three conditions hold —
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
import contextlib
import copy
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, Callable

from nats_core.events import ApprovalResponsePayload, StageCompletePayload
from pydantic import ValidationError

from forge.deploy.candidate_tree import (
    CandidateGit,
    CandidateTreeError,
    InContainerCandidateGit,
)
from forge.deploy.stage import assertion_in_words
from forge.gating.identity import is_accepted_approver
from forge.lifecycle.persistence import StageLogEntry
from forge.pipeline.check_join import (
    CheckJoinAnswer,
    ask_the_build_system_to_check_the_join,
    the_gated_sentence,
)
from forge.pipeline.fix_row_producer import candidate_refused_sentence
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_STAGE_LABEL,
    MERGE_OFFER_TARGET_IDENTIFIER,
    branch_to_merge,
)
from forge.pipeline.digest_conformance import run_digest_conformance
from forge.pipeline.merge_check_reading import names_for_record, names_from_record
from forge.pipeline.merge_card import REBUILD_NOTE, decide_previewed_offer, describable
from forge.pipeline.merge_join import (
    integration_branch,
    join_inputs,
    look_at_a_join,
    look_at_the_leftover_join,
    make_the_working_folder,
    target_branch_now,
    working_folder_path,
)
from forge.sandbox_gate.gate import SandboxNotReady
from forge.pipeline.deployment_identity import (
    declared_identity,
    fixed_identity,
    identity_reported_by,
    the_identities_differ,
    what_the_target_says,
    why_the_target_could_not_say,
)
from forge.pipeline.deployment_lock import (
    DeploymentLockStore,
    deployment_target_name,
)
from forge.pipeline.only_forwards import what_to_do_about_j
from forge.deploy.profile_selection import (
    FirstDeploymentProfile,
    ProfileSelectionRefusedBeforeDispatch,
    confirmed_profile_reference,
    parse_profile_reference,
    read_profile_approval,
    selection_from_record,
    validate_committed_profile_path,
)


from forge.pipeline.publication_record import (
    LINE_DONE,
    RESULT_MERGED_AND_RUNNING,
    RESULT_PUBLICATION_PENDING,
    RESULT_PUBLISHED_DEPLOYMENT_PENDING,
    RESULT_PUBLISHED_TO_MISSION_BRANCH,
    STEP_CANDIDATE_CHECK,
    STEP_DEPLOY,
    STEP_JOIN,
    STEP_MERGE_CHECKS,
    STEP_SEND,
    PublicationRecordStore,
    REPORT_PRESS_TURN_KEY,
    a_report_names_the_press,
    default_lease_seconds,
)
from forge.pipeline.publication_switch import (
    publication_is_switched_on,
    the_machine_now,
    why_publication_is_off,
)
from forge.pipeline.publisher_client import ask_the_publisher, the_remote_moved
from forge.receipts import receipts_root_for

logger = logging.getLogger(__name__)

__all__ = [
    "MERGE_DECISION_TARGET_IDENTIFIER",
    "MERGE_EXECUTION_ATTEMPT_VERSION",
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
    "MERGE_WALL_RUNS",
    "MERGE_LEASE_MARGIN_SECONDS",
    "MergeApprovalConsumer",
    "RESULT_WORD_MERGED_AND_RUNNING",
    "RESULT_WORD_PUBLICATION_PENDING",
    "RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING",
    "STOPPED_BY_A_TAKEOVER",
    "MergeDeployOutcome",
    "MergeExecutorDeps",
    "RED_MERGE_ENDINGS",
    "build_in_daemon_deploy_dispatcher",
    "close_build_row",
    "execute_merge_deploy",
    "deployed_in_for",
    "merge_wall_seconds",
    "sandbox_merge_words",
    "why_a_previewed_offer_is_refused",
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
#: A press turned away by a live hold leaves this note, and only this note,
#: when it carries an approved decision's attempt: it is not a merge report
#: (nothing reads it as an ending), and it is what tells the router that
#: decision's press is not still to run.
MERGE_TURNED_AWAY_TARGET_IDENTIFIER: str = "merge_deploy_turned_away"
#: Its own stage label, so a reader of the last ``merge-deploy`` row (the
#: dashboard) never sees the note in place of the merge report.
MERGE_TURNED_AWAY_STAGE_LABEL: str = "merge-deploy-turned-away"
#: The plain refusal when a stopped press's ending is still to be reported.
_AN_ENDING_IS_OWED: str = (
    "{named}'s previous merge stopped after recording its result, and its "
    "ending has not been reported yet; the factory reports that first, so "
    "this one left it alone. Nothing was merged. Try again once that ending "
    "is reported."
)

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

#: Seconds allowed for the merge itself, on top of the four install-or-check
#: runs, when sizing the wall around the whole command.
MERGE_WALL_MERGE_ALLOWANCE_SECONDS: int = 180

#: How many runs, each held to one check run's limit, the merge command may
#: make: the project's declared install and the checks on the target branch,
#: then the install and the checks again on the merged tree (13 October 2026,
#: the merge-install design, item 8).
MERGE_WALL_RUNS: int = 4

#: How much longer than the merge command's wall the press holds the build's
#: record while that command runs: the HTTP wait beyond the wall, and the time
#: to read and record the answer.
MERGE_LEASE_MARGIN_SECONDS: int = 300

#: The longest wall the deploy sidecar will accept (an hour). Written out here
#: rather than imported so this module does not depend on the sidecar; a test
#: pins the two together (``deploy_sidecar.service.MERGE_TIMEOUT_MAX``).
MERGE_WALL_CAP_SECONDS: int = 3600


def merge_wall_seconds(verify_timeout_seconds: int) -> int:
    """How long the whole merge command may take, given one check run's limit.

    The merge command may install and check TWICE — on the target branch to
    see what was already failing, and on the merged tree — and merges in
    between. The build system holds each install to the shorter of the
    project's own install limit and one check run's limit, so the wall around
    the whole thing holds four such runs plus three minutes, and never more
    than the deploy sidecar will accept. Sized any smaller, the outer wall
    fires first and the merge is killed mid-flight: the branch is joined and
    nothing says so.
    """
    wall = (
        MERGE_WALL_RUNS * int(verify_timeout_seconds)
        + MERGE_WALL_MERGE_ALLOWANCE_SECONDS
    )
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

#: Versioned attribution joining one durable approved decision to the one
#: executor report allowed to settle it.  A direct CLI press has no consumer
#: decision token and therefore cannot close queued approved work.
MERGE_EXECUTION_ATTEMPT_VERSION: int = 1
MERGE_EXECUTION_ATTEMPT_ID_KEY: str = "execution_attempt_id"
MERGE_EXECUTION_ATTEMPT_VERSION_KEY: str = "execution_attempt_version"

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

#: A MISSION FEATURE'S LANDING (missions design, 17 October 2026, section 6):
#: the joined commit was checked and is on the mission's branch, and the press
#: stopped there on purpose (``deploy=False``). It is a finished ending, not a
#: deploy still owed: only the mission's own merge into main deploys. The
#: record's plain sentence is
#: :data:`~forge.pipeline.publication_record.RESULT_PUBLISHED_TO_MISSION_BRANCH`.
RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH: str = "published-to-the-mission-branch"

#: A mission feature that could not land at all (finished with failed tasks,
#: an unknown repository, or a press that kept raising): its FAILED landing
#: report, so the mission closes the attempt. Nothing was published.
RESULT_WORD_MISSION_LANDING_FAILED: str = "mission-landing-failed"

#: One stage row per landing press STARTED for a mission feature (written
#: before the press runs, so a press that takes the coordinator down with it
#: still counts). At ``publication.send_attempts`` started presses with no
#: report, the build gets a FAILED landing report instead of another press.
MISSION_LANDING_ATTEMPT_TARGET_IDENTIFIER: str = "mission_landing_attempt"
#: One stage row when a clean mission feature is asked to land, written before
#: the landing waits for the repository's lock (the decision row is made
#: inside it, section 11): the durable proof of a clean finish a restart reads.
MISSION_LANDING_REQUEST_TARGET_IDENTIFIER: str = "mission_landing_requested"
MISSION_LANDING_STAGE_LABEL: str = "mission landing"

#: One stage row per RESUMED press of an approved mission merge into main
#: (the coordinator stopped after the decision and before the report). At
#: ``publication.send_attempts`` resumed presses with no report, the build
#: gets a FAILED merge report with the word below, so the mission settles.
MISSION_MERGE_RESUME_TARGET_IDENTIFIER: str = "mission_merge_resume"
MISSION_MERGE_STAGE_LABEL: str = "mission merge"
RESULT_WORD_MISSION_MERGE_UNFINISHED: str = "mission-merge-unfinished"

#: THE EXECUTOR'S WORD FOR A COMMAND A TAKEOVER STOPPED. Written out here
#: rather than imported, like the sidecar's own wall above, so this module does
#: not depend on the sidecar; a test pins the two together
#: (``deploy_sidecar.deploy_executor.STOPPED_BY_A_TAKEOVER``). A deploy command
#: that was stopped part-way is NOT a failed deploy and is certainly not a
#: deploy: it is no deploy at all, and the press says so.
STOPPED_BY_A_TAKEOVER: str = "the-deploy-command-was-stopped-by-a-takeover"
#: Every executor refusal after which nothing was started (the executor's own
#: list, repeated here by name because the press does not import the sidecar's
#: module; a test pins the two lists equal).
NOTHING_WAS_STARTED: tuple[str, ...] = (
    STOPPED_BY_A_TAKEOVER,
    "the-counter-has-moved-on",
    "that-counter-belongs-to-another-build",
    "the-slot-is-occupied",
    "a-command-is-already-running",
    "the-old-command-could-not-be-confirmed-stopped",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_receipts_root() -> Path:
    return receipts_root_for(None)


def _receipts_root_of(deps: Any, repo: str | None) -> Path:
    """Where this press writes its receipts: the build's own project's folder
    (one sandbox per project, design section 2.2a), or the seam a test put
    in ``receipts_root_fn``."""
    if deps.receipts_root_fn is _default_receipts_root:
        return receipts_root_for(repo)
    return deps.receipts_root_fn()


@dataclass(frozen=True)
class _TargetSaid:
    """What the project answered when asked what it is running.

    ``word`` is one of ``"identity"`` (``identity`` carries the project's own
    token for what is running), ``"nothing"`` (nothing is running there) or
    ``"no-answer"`` (the project said nothing this press can read, which is
    never taken as "nothing is running"). ``why`` is the sentence for the last
    of those, and ``said`` is what the step printed, kept for the record.
    """

    word: str
    identity: str | None
    why: str = ""
    said: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "identity": self.identity,
            "why": self.why or None,
        }


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
    #: TURNED AWAY by a live hold (press lease recovery, 18 October 2026):
    #: another press, or the recovery, is working on this build's record, so
    #: this press did nothing and its ending is not the build's. It is never
    #: reported, never closes the build row and never touches the record; the
    #: person who asked is told, and nothing else. Never on the report.
    turned_away: bool = False


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
    #: Says one line in a mission's thread: ``async (mission_id, sentence,
    #: delivery_id) -> delivered``. Installed at boot from the mission finisher (press lease
    #: recovery); ``None`` delivers nothing, and the finishing routine then
    #: leaves a mission landing's warning for the next pass.
    tell_mission: Callable[..., Awaitable[bool]] | None = None
    feature_routing_gate: Any | None = None
    feature_routing_required: bool = False
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
    #: WHAT THE PUBLISHER SAYS ABOUT ITS START-UP SELF-CHECK
    #: (:class:`~forge.pipeline.publication_activation.WhatTheMachineSays`).
    #: Left unset, nobody asked, the activation check refuses, and
    #: publication stays off — the safe side. It may be a zero-argument
    #: reader, called afresh on EVERY merge word; production passes
    #: :func:`~forge.pipeline.publisher_client.the_publishers_self_check`.
    what_the_machine_says: Any = None
    #: THE REVIEW-PREVIEW SERVICE (web-readiness W7), or ``None``. Read only
    #: for an offer whose details carry ``review_evidence``: its approval is
    #: refused unless the preview is usable, and the preview is released once
    #: the decision has ended. Every other offer never touches it.
    review_preview: Any = None
    #: KEEPING THE POSTED CARD TRUE (16 October 2026) —
    #: ``async (build_id) -> bool``, the offer service's
    #: :meth:`~forge.pipeline.merge_offer.MergeOfferService.describe_card`.
    #: Called after a previewed card's decision or refused approval, so the
    #: card shows the truth at once. ``None`` leaves it to the sweep.
    describe_card: Callable[[str], Awaitable[Any]] | None = None
    #: THE REBUILD PRESS (16 October 2026) — ``async (*, build_id) -> str``,
    #: the offer service's
    #: :meth:`~forge.pipeline.merge_offer.MergeOfferService.rebuild_preview`.
    #: ``None`` refuses every ``defer``, as before.
    rebuild_preview: Callable[..., Awaitable[Any]] | None = None


# ---------------------------------------------------------------------------
# Report parsing helpers (defensive — the guardkit merge verb is parallel-built)
# ---------------------------------------------------------------------------


def _parse_merge_report(result: Any) -> dict[str, Any] | None:
    """Extract the merge verb's ``--json`` report from stdout_tail/artefacts.

    Every string in it is shown as a person reads it (coach review 3, 15
    October 2026): a file name the report carries may hold a line break, a
    control character or a byte that is not UTF-8, and none of those may add
    a line to the outcome or stop it being written.
    """
    from forge.pipeline.merge_check_reading import shown_report

    parsed = _parse_merge_report_as_printed(result)
    return shown_report(parsed) if parsed is not None else None


def _parse_merge_report_as_printed(result: Any) -> dict[str, Any] | None:
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
    from forge.pipeline.merge_check_reading import shown_path

    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return ""
    last = shown_path(lines[-1])
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
    from forge.pipeline.merge_check_reading import shown_path

    files = [
        shown_path(f) for f in (report.get("conflict_files") or []) if str(f).strip()
    ]
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
) -> str:
    """File one repair row for a press that found the code wrong. Never raises.

    Answers "skipped" when this ending files none on purpose, "attempted"
    otherwise: the filer swallows its own failures, so the caller reads the
    queue to know whether the row is there (Codex round 2, R7).

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

    A MISSION'S BUILD FILES NONE (missions design, 17 October 2026, section
    6, round 1 R3): its failure is recorded on the mission and named on the
    mission's card. A repair row would sit outside the mission and be refused
    anyway, because its target is not the default branch.
    """
    from forge.lifecycle.mission_builds import mission_of_build

    belongs = mission_of_build(pool, build_id)
    if belongs is not None:
        logger.info(
            "merge-executor: %s belongs to mission %s, so no repair was filed — "
            "the failure is reported on the mission's card",
            feature_id or build_id,
            belongs.mission_id,
        )
        return "skipped"
    gate = outcome.gate_before_merge or {}
    if outcome.result == "merged-verify-failed" and outcome.verify_status != "failed":
        logger.info(
            "merge-executor: the checks for %s could not run, so no repair was "
            "filed — a person must look at the check itself",
            feature_id or build_id,
        )
        return "skipped"
    if outcome.result == "candidate-refused" and not gate.get("ran"):
        logger.info(
            "merge-executor: the sandbox check for %s could not run, so no "
            "repair was filed — a person must look at the check itself",
            feature_id or build_id,
        )
        return "skipped"
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
        return "skipped"
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
            return "attempted"
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
    return "attempted"


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


#: The ending written down for a press whose result was recorded but whose
#: exact ending was not (press lease recovery, 18 October 2026). It is the one
#: case the recovery cannot report word for word, so it says so, plainly, and
#: changes nothing else: no repair row, nothing sent, nothing deployed.
RESULT_WORD_ENDING_NOT_RECORDED: str = "merge-ending-not-recorded"
ENDING_NOT_RECORDED_SENTENCE: str = (
    "The merge stopped after recording its result, and its exact ending was "
    "not recorded; check the build before retrying."
)

_OUTCOME_FIELDS: tuple[str, ...] = (
    "result",
    "status",
    "detail",
    "merged_sha",
    "failed_step",
    "verdict",
    "checks_passed",
    "checks_total",
    "deployed_in",
    "verify_status",
    "gate_before_merge",
    "branch",
    "sandbox_merge",
)


def stored_outcome(
    outcome: "MergeDeployOutcome",
    *,
    feature_id: str,
    correlation_id: str,
    started: datetime,
    receipts_dir: Path,
    digest_warning: str | None,
    join_as: str,
) -> dict[str, Any]:
    """The press's ending as it is written down before it is reported.

    Everything :func:`finish_the_press` needs, so the recovery reports
    exactly what the press would have, and derives nothing.
    """
    return {
        "outcome": {name: getattr(outcome, name) for name in _OUTCOME_FIELDS},
        "feature_id": feature_id,
        "correlation_id": correlation_id,
        "started_at": started.isoformat(),
        "receipts_dir": str(receipts_dir),
        "digest_warning": digest_warning,
        "join_as": join_as,
    }


def outcome_from_stored(said: dict[str, Any]) -> "MergeDeployOutcome":
    """The ending :func:`stored_outcome` wrote down, exactly as it was."""
    fields = dict(said.get("outcome") or {})
    return MergeDeployOutcome(**{name: fields.get(name) for name in _OUTCOME_FIELDS})


def landing_left_standing_sentence(
    feature_id: str, record: Any, kept: str, *, handed: str | None = None
) -> str:
    """The one plain line for a mission landing that left something standing.

    It names the feature and the candidate the check stood up, by the
    identity handed to the check (``handed``, else the record's), and says
    how it is removed. It says the feature LANDED only when the record says
    it was published to the mission branch (Codex round 3, R9): a press that
    stopped before publishing — a failed check among them — landed nothing.
    """
    if not handed:
        checked = dict(getattr(record, "checked", None) or {})
        identity = checked.get("identity") or {}
        handed = identity.get("handed_to_the_check") if isinstance(identity, dict) else None
    named = f"identity {handed}" if handed else "no identity was recorded for it"
    lost = "answer was lost" in kept
    if getattr(record, "result", None) == RESULT_PUBLISHED_TO_MISSION_BRANCH:
        if lost:
            return (
                f"{feature_id} landed, but a deploy-stage step for its check got no "
                f"answer, so its candidate ({named}) may still be standing; remove "
                "it with the project's own teardown step."
            )
        return (
            f"{feature_id} landed, but its check's candidate ({named}) may still "
            "be standing; remove it with the project's own teardown step."
        )
    if lost:
        return (
            f"{feature_id}'s check got no answer from a deploy-stage step, so its "
            f"candidate ({named}) may still be standing; remove it with the "
            "project's own teardown step."
        )
    return (
        f"{feature_id}'s check left its candidate standing ({named}); remove it "
        "with the project's own teardown step."
    )


def the_identity_handed_to_the_check(written: Any) -> str | None:
    """The identity the press handed to its check, as its row keeps it: with
    the result (``standing_json``), else in the ending it wrote down."""
    for said in (getattr(written, "standing", None), getattr(written, "outcome", None)):
        handed = (said or {}).get("identity_handed_to_the_check")
        if handed:
            return str(handed)
    return None


def press_kept_on_purpose(record: Any, written: Any) -> str | None:
    """Why a stopped press's hold is kept on purpose, or ``None``.

    Press lease recovery (coach round, item 2): the press keeps its hold
    after a deploy-stage leg whose answer was lost or with a candidate that
    may be standing (15 October 2026), and the router reads a kept holder as
    active or ambiguous for exactly that reason; a recovery must not put it
    down. Read from the press's own row: its decision after the clean-up;
    before the clean-up (it stopped part-way), what might still be standing
    then; with no ending written down, what might be standing when its result
    was written. A record with no holder keeps nothing.
    """
    if not getattr(record, "lease_holder", None):
        return None
    said = dict(getattr(written, "outcome", None) or {})
    if said.get("cleanup_done"):
        kept = said.get("kept_because")
        return str(kept) if kept else None
    flags = said if said.get("outcome") else dict(getattr(written, "standing", None) or {})
    if flags.get("unknown"):
        return "what the press had standing could not be recorded"
    if flags.get("leg_unanswered"):
        return "a deploy-stage leg's answer was lost"
    if flags.get("candidate_may_stand"):
        return "the press stopped before its clean-up, and a candidate may still be standing"
    return None


def _a_press_is_owed_its_finishing(record: Any, written: Any) -> bool:
    """A result this press wrote, on a record that still has it, not finished."""
    return bool(
        getattr(record, "recorded", False)
        and getattr(record, "result", None)
        and written is not None
        and written.result_written
        and not written.finished_at
    )


def the_stopped_presss_ending(
    deps: Any, row: Any, record: Any, written: Any, *, kept: str | None
) -> PressEnding:
    """The ending a stopped press wrote down, exactly as written; or, when it
    stopped between its result and writing it down, the plain FAILED ending
    :data:`ENDING_NOT_RECORDED_SENTENCE` (no repair row). Never guessed from
    the record's result, which covers several endings."""
    said = dict(getattr(written, "outcome", None) or {})
    standing_note = (
        "The factory stopped before its clean-up finished, so the candidate "
        "it checked may still be standing; the hold is kept until a person "
        "looks."
        if kept and not said.get("cleanup_done")
        else None
    )
    if said.get("outcome"):
        outcome = outcome_from_stored(said)
        if standing_note:
            outcome.detail = f"{outcome.detail}\n{standing_note}"
        return PressEnding(
            build_id=row.build_id,
            feature_id=str(said.get("feature_id") or row.feature_id),
            correlation_id=str(said.get("correlation_id") or row.correlation_id),
            outcome=outcome,
            started=datetime.fromisoformat(str(said.get("started_at"))),
            receipts_dir=Path(str(said.get("receipts_dir"))),
            attempt_id=written.attempt_id,
            digest_warning=said.get("digest_warning"),
        )
    if getattr(record, "result", None) == RESULT_PUBLISHED_TO_MISSION_BRANCH:
        # THE ONE RECORD RESULT WITH ONLY ONE ENDING: a mission feature's
        # landing that was published to its mission's branch (missions,
        # section 11). Said as that, PASSED, as the landing recovery always has.
        detail = (
            f"{row.feature_id} landed on {record.target_branch}: the record says "
            "its joined commit was published there, and the press stopped before "
            "its own report."
        )
        if standing_note:
            detail = f"{detail}\n{standing_note}"
        return PressEnding(
            build_id=row.build_id,
            feature_id=row.feature_id,
            correlation_id=row.correlation_id,
            outcome=MergeDeployOutcome(
                result=RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH,
                status="PASSED",
                detail=detail,
                branch=branch_to_merge(
                    row.feature_id, _merge_branch_of_record(deps.pool, row.build_id)
                ),
            ),
            started=deps.clock(),
            receipts_dir=_receipts_root_of(deps, row.repo) / f"merge-{row.build_id}",
            attempt_id=written.attempt_id,
            digest_warning=None,
            mint_repair=False,
        )
    detail = ENDING_NOT_RECORDED_SENTENCE
    if standing_note:
        detail = f"{detail}\n{standing_note}"
    return PressEnding(
        build_id=row.build_id,
        feature_id=row.feature_id,
        correlation_id=row.correlation_id,
        outcome=MergeDeployOutcome(
            result=RESULT_WORD_ENDING_NOT_RECORDED,
            status="FAILED",
            detail=detail,
            branch=branch_to_merge(
                row.feature_id, _merge_branch_of_record(deps.pool, row.build_id)
            ),
        ),
        started=deps.clock(),
        receipts_dir=_receipts_root_of(deps, row.repo) / f"merge-{row.build_id}",
        attempt_id=written.attempt_id,
        digest_warning=None,
        mint_repair=False,
    )


def _an_ending_is_owed(pool: Any, build_id: str) -> bool:
    """Is this build's latest press's result written and not yet finished?"""
    cx = _ledger_of(pool)
    if cx is None:
        return False
    store = PublicationRecordStore(cx)
    return _a_press_is_owed_its_finishing(store.read(build_id), store.read_outcome(build_id))


def _record_the_report(
    deps: Any, build_id: str, payload: StageCompletePayload, completed: datetime
) -> bool:
    """Put the merge report on the build's own record, not only on the bus.

    The published report is a message and the receipt is a file, and
    neither can be read back by a query. So the same report is also
    appended to ``stage_log``, with its outcome word at the top level of
    the details as ``result`` — that is what the self-closed defect rate
    reads to tell a repair that merged, deployed and stayed green from
    one that stopped at a red step (``lifecycle/metrics.py``).

    A dry run never gets here: it leaves no durable rows on purpose.
    Never raises; answers whether the row was written — one that was not is
    written by the next finishing pass (its check is the row itself).
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
            "(%s: %s) — the next finishing pass writes it",
            build_id,
            type(exc).__name__,
            exc,
        )
        return False
    return True


def _write_the_receipt(
    receipts_dir: Path, build_id: str, name: str, data: dict[str, Any]
) -> None:
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


@dataclass
class PressEnding:
    """Everything the finishing routine needs to report one press's ending.

    Made by the press from its own outcome, or by the recovery from the
    press's row (:func:`the_stopped_presss_ending`) — never re-derived from
    the record's result.
    """

    build_id: str
    feature_id: str
    correlation_id: str
    outcome: "MergeDeployOutcome"
    started: datetime
    receipts_dir: Path
    attempt_id: str | None
    digest_warning: str | None
    #: False only for the ending-not-recorded fallback: not a finding about
    #: the code, so no repair row.
    mint_repair: bool = True
    #: A mission landing that left something standing: the one plain line,
    #: and the mission it is said in.
    warning: str | None = None
    mission_id: str | None = None


def _the_report_payload(
    ending: PressEnding,
    *,
    completed: datetime,
    dry_run: bool,
    press_turn: int | None,
) -> StageCompletePayload:
    outcome = ending.outcome
    return StageCompletePayload(
        feature_id=ending.feature_id,
        build_id=ending.build_id,
        stage_label=MERGE_REPORT_STAGE_LABEL,
        target_kind="local_tool",
        target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
        status=outcome.status,
        gate_mode=None,
        coach_score=None,
        duration_secs=max(0.0, (completed - ending.started).total_seconds()),
        completed_at=completed.isoformat(),
        correlation_id=ending.correlation_id,
        # Additive fields — StageCompletePayload is extra="allow".
        result=outcome.result,
        merged_sha=outcome.merged_sha,
        failed_step=outcome.failed_step,
        verdict=outcome.verdict,
        checks_passed=outcome.checks_passed,
        checks_total=outcome.checks_total,
        verify_status=outcome.verify_status,
        detail=outcome.detail,
        digest_conformance_warning=ending.digest_warning,
        dry_run=dry_run,
        # Additive (Part M, rule 55): the branch this press merged, or
        # would have — truthful for a repair, the feature's own otherwise.
        branch=outcome.branch,
        # A consumer-approved press carries the exact durable decision
        # attempt.  Direct CLI presses deliberately carry no such block,
        # so their later report cannot settle queued approval work merely
        # because it shares the build id and happened later.
        **(
            {
                MERGE_DECISION_DETAILS_KEY: {
                    MERGE_EXECUTION_ATTEMPT_VERSION_KEY: (
                        MERGE_EXECUTION_ATTEMPT_VERSION
                    ),
                    MERGE_EXECUTION_ATTEMPT_ID_KEY: ending.attempt_id,
                }
            }
            if ending.attempt_id is not None
            else {}
        ),
        # Additive (press lease recovery): the press whose ending this report
        # is — the finishing routine's own check for its first step.
        **({REPORT_PRESS_TURN_KEY: int(press_turn)} if press_turn else {}),
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


def report_a_dry_run(deps: Any, ending: PressEnding) -> "MergeDeployOutcome":
    """A dry run's report: its receipt, and nothing durable on purpose."""
    payload = _the_report_payload(ending, completed=deps.clock(), dry_run=True, press_turn=None)
    logger.info(
        "merge-executor: dry run — outcome kept to receipts only, "
        "no stage-complete published for %s",
        ending.build_id,
    )
    _write_the_receipt(
        ending.receipts_dir, ending.build_id, "merge_deploy_report.json",
        payload.model_dump(mode="json"),
    )
    return ending.outcome


# ---------------------------------------------------------------------------
# THE FINISHING ROUTINE (press lease recovery, Codex round 1, 18 October 2026)
#
# One blunt rule for every crash or race between two bookkeeping steps: the
# press and the recovery both call ONE idempotent routine, keyed by the
# press's turn, under a live lease they own. It runs every step in order and
# each step first asks whether it is already done for that turn; only after
# all of them does it set the row's one ``finished_at``. A stop between any
# two steps leaves the rest to the next pass, which redoes only those.
# ---------------------------------------------------------------------------

#: How a finishing run ends the hold: put it down, keep it for a person (and
#: finish), or keep it until its expiry without finishing (a mission landing
#: that left something standing: the recovery puts it down after expiry).
HOLD_RELEASE = "release"
HOLD_KEEP = "keep"
HOLD_KEEP_UNTIL_EXPIRY = "keep-until-expiry"


def _finish_the_report_row(
    deps: Any, store: Any, ending: PressEnding, press_turn: int | None,
    payload: StageCompletePayload, completed: datetime,
) -> bool:
    """Step 1: the receipt and the report row, unless a report names this
    turn. Answers whether the report row is there now."""
    if store is not None and press_turn and a_report_names_the_press(
        store._cx, build_id=ending.build_id, turn=press_turn
    ):
        return True
    _write_the_receipt(
        ending.receipts_dir, ending.build_id, "merge_deploy_report.json",
        payload.model_dump(mode="json"),
    )
    return _record_the_report(deps, ending.build_id, payload, completed)


async def _finish_the_bus_message(
    deps: Any, store: Any, ending: PressEnding, press_turn: int | None,
    written: Any, payload: StageCompletePayload,
) -> None:
    """Step 2: the bus message, unless already sent for this turn.

    A stop between the send and its mark sends it again on the next pass;
    Jarvis drops a repeat of the same correlation id and stage label within
    five minutes, so only a repeat after that shows twice."""
    if written is not None and written.bus_sent_at:
        return
    try:
        await deps.pipeline_publisher.publish_stage_complete(payload)
    except Exception as exc:  # noqa: BLE001 — the report is derived truth
        logger.error(
            "merge-executor: outcome publish failed for %s (%s) — the "
            "receipts on disk remain the record",
            ending.build_id,
            exc,
        )
    if store is not None and press_turn:
        store.mark_outcome(
            build_id=ending.build_id, turn=press_turn, bus_sent_at=deps.clock().isoformat()
        )


#: What a finishing step answers (Codex round 2, R7): done, nothing to do
#: (intentionally skipped), or not done and to be tried again next pass. A
#: step is never taken as done on its own say-so: its durable result is read.
STEP_DONE = "done"
STEP_SKIPPED = "skipped"
STEP_RETRY = "retry"

_TERMINAL_BUILD_STATES: frozenset[str] = frozenset({"COMPLETE", "FAILED", "CANCELLED", "SKIPPED"})


def _the_build_rows_state(pool: Any, build_id: str) -> str | None:
    try:
        row = pool.get_build_row(build_id)
    except Exception:  # noqa: BLE001 — unknown is not terminal
        return ""
    if row is None:
        return None
    status = getattr(row, "status", None)
    return str(getattr(status, "value", status) or "")


def _finish_the_build_row(deps: Any, ending: PressEnding) -> str:
    """Step 3: the build row's ending, unless the row is already terminal.

    THE BUILD'S ENDING, written by the only thing that knows it. After the
    report, so a row that cannot be written can never cost the report;
    before the repair row, because the journey ends before its follow-on
    begins. :func:`close_build_row` leaves a terminal row exactly as it is,
    and can degrade without saying so, so the row itself is read after it:
    terminal is done; an ending this seam does not write, or no row at all,
    is nothing to do; anything else is tried again next pass."""
    if _the_build_rows_state(deps.pool, ending.build_id) in _TERMINAL_BUILD_STATES:
        return STEP_DONE
    if str(ending.outcome.status or "").upper() not in ("PASSED", "FAILED"):
        return STEP_SKIPPED
    close_build_row(deps.pool, ending.build_id, ending.outcome)
    state = _the_build_rows_state(deps.pool, ending.build_id)
    if state is None:
        return STEP_SKIPPED
    return STEP_DONE if state in _TERMINAL_BUILD_STATES else STEP_RETRY


def _a_repair_row_is_filed(pool: Any, build_id: str) -> bool:
    from forge.pipeline.fix_row_producer import fix_correlation_id

    cx = _ledger_of(pool)
    if cx is None:
        return False
    try:
        return (
            cx.execute(
                "SELECT 1 FROM work_queue WHERE correlation_id = ? AND kind = 'fix' LIMIT 1",
                (fix_correlation_id(build_id),),
            ).fetchone()
            is not None
        )
    except Exception:  # noqa: BLE001 — unknown is not filed
        return False


def _finish_the_repair_row(
    deps: Any, store: Any, ending: PressEnding, press_turn: int | None, written: Any,
) -> str:
    """Step 4: the repair row for a red ending, unless done for this turn.

    A PRESS THAT FOUND THE CODE WRONG BECOMES A REPAIR JOB (conductor rewire
    rule 1): the three red endings, and a candidate the check refused. A
    refused merge is not one of them: nothing changed, so there is nothing
    to repair. The filer and its producer swallow their failures, so the
    queue itself is read after them: a repair row for this build is done;
    one the filer decided not to file (or a repair journey's own build) is
    nothing to do; otherwise it is tried again next pass, and ``repair_at``
    is written only once it is done or not needed."""
    outcome = ending.outcome
    if not ending.mint_repair or not (
        outcome.result in RED_MERGE_ENDINGS or outcome.result == "candidate-refused"
    ):
        return STEP_SKIPPED
    if written is not None and written.repair_at:
        return STEP_DONE
    filed = _mint_repair_row(deps.pool, ending.build_id, outcome, feature_id=ending.feature_id)
    if filed == "skipped" or _a_repair_row_is_filed(deps.pool, ending.build_id):
        answer = STEP_SKIPPED if filed == "skipped" else STEP_DONE
    elif _a_repair_journey(deps.pool, ending.build_id):
        answer = STEP_SKIPPED  # a repair of a repair would go round in a circle
    else:
        return STEP_RETRY
    if store is not None and press_turn:
        store.mark_outcome(
            build_id=ending.build_id, turn=press_turn, repair_at=deps.clock().isoformat()
        )
    return answer


def _a_repair_journey(pool: Any, build_id: str) -> bool:
    try:
        from forge.pipeline.fix_row_producer import _is_fix_journey

        row = pool.get_build_row(build_id)
        return bool(row is not None and _is_fix_journey(row))
    except Exception:  # noqa: BLE001 — unknown is not a repair journey
        return False


def landing_warning_delivery_id(build_id: str, press_turn: int | None) -> str:
    """The stable identity of one landing's warning, carried on every delivery
    of it (Codex round 2, R4), so a consumer that drops repeats of one id can
    drop a repeat of this warning."""
    return f"{build_id}:{int(press_turn or 0)}:landing-warning"


async def _finish_the_warning(
    deps: Any, store: Any, ending: PressEnding, press_turn: int | None,
    written: Any, tell: Any,
) -> str:
    """Step 5: a mission landing's warning in its thread, until delivered.

    The sentence is kept on the row first (the mission's card reads it),
    then delivered with its stable delivery id; a delivery that fails, or no
    way to deliver here, is tried again on the next pass. AT LEAST ONCE, on
    purpose: a stop between the delivery and its mark delivers it again,
    because a repeated line is far better than a lost warning."""
    if not ending.warning:
        return STEP_SKIPPED
    # TWO SUB-STEPS, each retried until done (Codex round 3, R8): the
    # sentence kept on the row (the mission's card reads it), and its
    # delivery. Neither is redone once done.
    kept_on_the_row = store is None or not press_turn or bool(written and written.warning)
    if not kept_on_the_row:
        kept_on_the_row = bool(
            store.mark_outcome(
                build_id=ending.build_id,
                turn=press_turn,
                warning_json={"sentence": ending.warning, "mission_id": ending.mission_id},
            )
        )
    if written is not None and written.warning_delivered_at:
        return STEP_DONE if kept_on_the_row else STEP_RETRY
    if tell is None:
        return STEP_RETRY
    try:
        delivered = bool(
            await tell(
                ending.mission_id,
                ending.warning,
                landing_warning_delivery_id(ending.build_id, press_turn),
            )
        )
    except Exception as exc:  # noqa: BLE001 — tried again next pass
        logger.warning(
            "merge-executor: %s's warning could not be said in its mission's "
            "thread (%s: %s)",
            ending.build_id,
            type(exc).__name__,
            exc,
        )
        delivered = False
    if not delivered:
        return STEP_RETRY
    if store is not None and press_turn:
        if not store.mark_outcome(
            build_id=ending.build_id,
            turn=press_turn,
            warning_delivered_at=deps.clock().isoformat(),
        ):
            return STEP_RETRY
    return STEP_DONE if kept_on_the_row else STEP_RETRY


def _finish_the_release(
    deps: Any, store: Any, build_id: str, press_turn: int, lease_turn: int,
    holder: str, *, release: bool,
) -> bool:
    """Step 6, last: the hold put down (unless kept on purpose) AND
    ``finished_at`` set, in one transaction, while this worker still owns the
    record (Codex round 2, R6)."""
    return bool(
        store.finish_outcome(
            build_id=build_id,
            press_turn=press_turn,
            lease_turn=lease_turn,
            holder=holder,
            now=deps.clock(),
            release=release,
        )
    )


def _put_down_a_lease_taken_for_nothing(
    deps: Any, store: Any, build_id: str, lease_turn: int, holder: str, hold: str
) -> None:
    """The press was already finished: the lease this worker took for it is
    put down again, so it is never left stranded (R6) — unless the finished
    press's hold is one kept on purpose, which stays held."""
    if hold == HOLD_KEEP:
        return
    store.release_lease(build_id=build_id, turn=lease_turn, holder=holder, now=deps.clock())


async def finish_the_press(
    deps: Any,
    ending: PressEnding,
    *,
    store: Any,
    press_turn: int | None,
    lease_turn: int | None,
    holder: str | None,
    hold: str = HOLD_RELEASE,
    tell: Any = None,
) -> str:
    """Finish one press's bookkeeping, idempotently, under its own live lease.

    ``store`` is ``None`` only where there is no ledger to keep marks on (a
    stand-in pool): every step then runs once, unconditionally. Otherwise
    the caller holds the record's lease on ``lease_turn`` as ``holder``
    (the press on its own turn; a recovery on the turn it took over), and
    ``press_turn`` keys the press's row. The lease is renewed first: a
    caller that no longer owns the record does nothing at all.

    Every step answers done, skipped or retry, read from its durable result;
    ``finished_at`` is set only when every step is done or skipped. A step to
    retry leaves the row unfinished, the hold put down as usual, so the next
    pass does it (and nothing already done).

    Answers "finished", "kept" (finished, the hold kept for a person),
    "waiting" (not finished: a step to retry, a warning still to deliver, or
    a hold kept until its expiry), or "replaced".
    """
    written = None
    if store is not None:
        if not store.renew_lease(
            build_id=ending.build_id, turn=int(lease_turn or 0), now=deps.clock()
        ):
            return "replaced"
        written = store.read_outcome(ending.build_id)
        if written is None or written.turn != press_turn:
            return "replaced"
        if written.finished_at:
            _put_down_a_lease_taken_for_nothing(
                deps, store, ending.build_id, int(lease_turn or 0), str(holder), hold
            )
            return "finished"
    completed = deps.clock()
    payload = _the_report_payload(
        ending, completed=completed, dry_run=False, press_turn=press_turn
    )
    # A STEP THAT COULD NOT BE DONE NEVER COSTS THE OTHERS: every step still
    # runs (each its own check), and finishing is left unmarked, so the next
    # pass does that step and redoes nothing else.
    steps = [STEP_DONE if _finish_the_report_row(
        deps, store, ending, press_turn, payload, completed
    ) else STEP_RETRY]
    await _finish_the_bus_message(deps, store, ending, press_turn, written, payload)
    steps.append(_finish_the_build_row(deps, ending))
    steps.append(_finish_the_repair_row(deps, store, ending, press_turn, written))
    told = await _finish_the_warning(deps, store, ending, press_turn, written, tell)
    if store is None:
        return "finished"
    if told == STEP_RETRY or hold == HOLD_KEEP_UNTIL_EXPIRY:
        return "waiting"
    if STEP_RETRY in steps:
        if hold == HOLD_RELEASE:
            store.release_lease(
                build_id=ending.build_id, turn=int(lease_turn or 0), holder=str(holder),
                now=deps.clock(),
            )
        return "waiting"
    if not _finish_the_release(
        deps, store, ending.build_id, int(press_turn or 0), int(lease_turn or 0),
        str(holder), release=hold == HOLD_RELEASE,
    ):
        return "replaced"
    return "kept" if hold == HOLD_KEEP else "finished"


def the_hold_after_finishing(kept: str | None, *, a_mission_landing: bool, by_recovery: bool) -> str:
    """How finishing ends the hold. Not kept: put down. Kept on purpose: an
    ordinary build's is a person's (kept, finished); a mission landing's is
    said in the mission's thread and put down after its usual expiry, so the
    mission still drains (Rich's coordinator, 18 October 2026)."""
    if not kept:
        return HOLD_RELEASE
    if not a_mission_landing:
        return HOLD_KEEP
    return HOLD_RELEASE if by_recovery else HOLD_KEEP_UNTIL_EXPIRY


def git_venue(git: Any, repo_root: Path | str) -> str:
    """Where this press's git happened, as a phrase a sentence can carry.

    ``in <the checkout path>`` when it happened in this container, ``in the
    sandbox that holds <org>/<repo>`` when it happened where the repository
    lives.
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
    # The project's recorded fetch address (Forge git hardening, 14 October
    # 2026): the starting point is fetched from it, never the clone's origin.
    planning = getattr(getattr(deps, "config", None), "planning", None)
    addresses = getattr(planning, "fetch_addresses", None) or {}
    return InContainerCandidateGit(
        repo_root, fetch_address=addresses.get(str(repo)), repo=str(repo)
    )


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


#: The project's own deploy declaration file, by the one name this factory
#: reads it under. A path, not a layout: what is inside it is the project's.
DEPLOY_PROFILE_PATH: str = "deploy/profile.yaml"


def _the_profile_at(repo_root: Path | str, commit: str | None) -> tuple[Any, str | None]:
    """A project's deploy profile AS IT IS AT ``commit``, and why not if not.

    A DECLARATION IS A COMMITTED LINE ON THIS SIDE TOO (23 September 2026, the
    seventh review, which drove it). The helper that launches the project's own
    commands had already been moved onto the committed file; this side — which
    composes what that helper is sent — was still reading the working copy. So
    an ``identity: setting:`` line written into the checkout and never
    committed became the name this side handed over, and the name the project
    really declares at the bound commit was the one refused. The two now read
    the same file, at the same commit, through the same reader
    (:func:`forge.deploy.candidate_tree.read_file_at_commit_sync`).

    ``commit`` empty ⇒ the COMMITTED HEAD of the copy this coordinator has,
    never its working tree. Returns ``(profile, None)`` or ``(None, why)``;
    never raises — a file out of a project this factory did not write is input.
    """
    from forge.deploy.candidate_tree import read_file_at_commit_sync

    at = str(commit or "").strip() or "HEAD"
    try:
        answer = read_file_at_commit_sync(Path(repo_root), at, DEPLOY_PROFILE_PATH)
    except Exception as exc:  # noqa: BLE001 — a read, never a crash
        return None, f"{type(exc).__name__}: {exc}"
    return _the_profile_out_of(answer, at)


def _the_profile_out_of(answer: Any, at: str) -> tuple[Any, str | None]:
    """The profile in one read's answer, or why not. Never raises."""
    import yaml

    from forge.deploy.profile import parse_deploy_profile

    if answer.refusal:
        return None, str(answer.refusal)
    if not answer.found or answer.content is None:
        return None, f"there is no {DEPLOY_PROFILE_PATH} at {at}"
    try:
        return parse_deploy_profile(
            yaml.safe_load(answer.content) or {}, source_ref=DEPLOY_PROFILE_PATH
        ), None
    except Exception as exc:  # noqa: BLE001 — a project's own file is input
        return None, f"{DEPLOY_PROFILE_PATH} at {at} cannot be used: {exc}"


async def _the_profile_where_it_lives(
    git: Any, repo_root: Path | str, commit: str | None
) -> tuple[Any, str | None]:
    """The deploy profile at ``commit``, read where the repository lives.

    SANDBOX FIRST FOR THE PROFILE TOO (3 October 2026, FEAT-E592). A venue that
    can read a file at a commit — the sandbox's, whose clone is the only copy
    of a sandboxed repository — is asked, through the same reader and with the
    same "committed HEAD when no commit is recorded" rule. Every other venue
    is this container, and the copy here is read exactly as before
    (:func:`_the_profile_at`). Returns ``(profile, None)`` or ``(None, why)``;
    never raises.
    """
    reader = getattr(git, "read_file_at_commit", None)
    if reader is None:
        return _the_profile_at(repo_root, commit)
    at = str(commit or "").strip() or "HEAD"
    try:
        answer = await reader(at, DEPLOY_PROFILE_PATH)
    except Exception as exc:  # noqa: BLE001 — a read, never a crash
        return None, f"{type(exc).__name__}: {exc}"
    return _the_profile_out_of(answer, at)


# ---------------------------------------------------------------------------
# The publication record, for a press and for a refused approval
# ---------------------------------------------------------------------------


def _publication_store_for(deps: MergeExecutorDeps, build_id: str) -> Any:
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


def _write_the_refusal_down_for(
    store: Any,
    *,
    deps: MergeExecutorDeps,
    build_id: str,
    holder: str,
    feature_id: str,
    repo: str,
    decided_by: str,
    target_branch: str | None,
    refusal: str,
    starts_a_press: bool = False,
    refuse_if_an_ending_is_owed: bool = False,
    attempt_id: str | None = None,
) -> int | None:
    """One row for this build, saying the merge word was given and refused.

    Answers the turn the refusal was written on; ``0`` when the record could
    not be taken (another press holds it, or a stopped press's ending is still
    owed: the caller is then turned away); ``None`` when nothing was written. ``starts_a_press``
    is the press's own refusal (it is reported, so its ending is written down
    and its report marked on that turn, press lease recovery); an approval the
    consumer refused before any press is not, and is never reported.

    Used where the press stops BEFORE it would otherwise have taken the
    lease — no branch of the remote was ever written down for this build,
    or the remote's default branch has been renamed since — and, for an offer
    with a review preview, where the approval itself is refused because the
    preview is not usable (web-readiness W7). Taking the
    lease is what creates the row, so it is taken here too; it carries
    who gave the merge word and when, and the refusal is written onto the
    record as the result with its reason beside it.

    It never costs the press anything: a record that cannot be taken or
    written is logged and the refusal is returned either way. A build
    whose record somebody else is holding is left alone, which is the
    same rule as everywhere else.
    """
    if store is None:
        return None
    try:
        grant = store.take_lease(
            build_id=build_id,
            holder=holder,
            now=deps.clock(),
            feature_id=feature_id,
            repo=repo,
            decided_by=decided_by,
            target_branch=target_branch,
            starts_a_press=starts_a_press,
            refuse_if_an_ending_is_owed=refuse_if_an_ending_is_owed,
            attempt_id=attempt_id,
        )
        if grant is None:
            logger.warning(
                "merge-executor: %s's merge word was refused (%s) and the "
                "refusal could not be written down — another worker holds "
                "the record",
                build_id,
                refusal,
            )
            return 0
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
        if starts_a_press:
            # THE PRESS'S OWN REFUSAL KEEPS ITS LEASE (Codex round 1, R3):
            # its report is written by the finishing routine under a live
            # lease it owns, which puts the lease down last.
            store.record(
                build_id=build_id,
                turn=grant.turn,
                now=deps.clock(),
                result=RESULT_PUBLICATION_PENDING,
            )
        else:
            store.record(
                build_id=build_id,
                turn=grant.turn,
                now=deps.clock(),
                result=RESULT_PUBLICATION_PENDING,
                # AND THE LEASE IS PUT DOWN AGAIN. Nothing is in progress and
                # nothing is reported (an approval the consumer refused), so
                # holding the record would only make the next merge word wait
                # out the lease for no reason.
                lease_holder=None,
                lease_expires_at=None,
            )
        return grant.turn
    except Exception as exc:  # noqa: BLE001 — a record never costs a press
        logger.warning(
            "merge-executor: %s's refusal could not be written onto its "
            "publication record (%s: %s)",
            build_id,
            type(exc).__name__,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# The executor coroutine
# ---------------------------------------------------------------------------


async def retire_join_working_folders(
    git: Any, record: Any, repo_root: Path, join_as: str
) -> tuple[list[dict[str, Any]], bool]:
    """Remove every join attempt's working folder; ``(what happened, an answer was lost)``.

    THE BRANCH IS LEFT ALONE. Only the working FOLDER goes. Every joined
    commit of every attempt stays exactly where it is, on the branch its own
    attempt named, which is what "kept until the record's end" was protecting
    — a folder is scaffolding, a commit is the work.

    WHERE EACH JOIN'S FOLDER REALLY IS (15 October 2026, FEAT-D267): the
    join's "done" line carries the path the venue answered with, which for a
    sandboxed repository is the sandbox's own path. An attempt with no such
    line falls back to this side's path. A folder the venue ANSWERED it did
    not remove is a folder left behind; only a removal whose answer was lost
    (``None``, or an exception) counts as an answer lost.
    """
    retired: list[dict[str, Any]] = []
    lost = False
    highest = int(getattr(record, "attempt", 0) or 0) if record is not None else 0
    joined_at: dict[int, str] = {}
    for line in getattr(record, "lines", ()) or ():
        where = str((line.detail or {}).get("working_folder") or "").strip()
        if line.kind == LINE_DONE and line.step == STEP_JOIN and where:
            joined_at[int(line.attempt)] = where
    for attempt_number in range(1, max(highest, 1) + 1):
        folder = joined_at.get(attempt_number) or working_folder_path(
            repo_root, join_as, attempt_number
        )
        try:
            removed = await git.remove_working_folder(folder)
        except Exception as exc:  # noqa: BLE001 — never costs a result
            lost = True
            retired.append(
                {
                    "attempt": attempt_number,
                    "folder": folder,
                    "removed": False,
                    "answered": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if removed is None:
            lost = True
        retired.append(
            {
                "attempt": attempt_number,
                "folder": folder,
                "removed": bool(removed),
                "answered": removed is not None,
                "branch_kept": integration_branch(join_as, attempt_number),
            }
        )
    return retired, lost


class MissionMergeRefused(RuntimeError):
    """A mission's own merge was asked to run after a stop was requested."""


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
    merge_execution_attempt_id: str | None = None,
    expected_preview_tree: str | None = None,
    deploy: bool = True,
    join_name: str | None = None,
    sandbox_not_ready: str | None = None,
) -> MergeDeployOutcome:
    """Run join -> checks -> report for one press, and stop before publishing.

    ``sandbox_not_ready`` (one sandbox per project, stage S2, Codex S2
    implementation review 1, R3): the project's own sandbox did not become
    ready, with this reason. The press then makes no git, sandbox or deploy
    call at all: it writes its report — refused, nothing merged, the branch
    kept — and ends.

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
       three conditions does not hold;
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

    ``expected_preview_tree`` is the tree an offer with a review preview
    previewed (web-readiness W7). When it is set, a joined tree that differs
    from it is refused after the join — so after the build system's own checks
    inside the merge command have run on it — and before the factory's live
    check, any publication or any deploy; nothing reaches the target branch.
    When it is ``None`` — every offer without a preview — nothing here
    changes.

    ``deploy=False`` is a mission feature's landing (missions design, 17
    October 2026, section 6): the same join and the same two kinds of check
    on J, the send to the build's recorded target (its mission's branch), and
    then the press STOPS after a confirmed publish, recording
    "published to the mission branch". Nothing is deployed and no preview
    tree is pinned, because a feature inside a mission has no card and no
    preview of its own.

    ``join_name`` names the integration branches and working folders
    (``factory-integration/<join_name>[-attempt-N]``); the feature id when it
    is not given. A mission's merge into main passes ``mission-<id>``, so it
    never meets the last feature's own integration names (round 2, R1). The
    feature id stays the build system's argument.
    """
    # Permanent administrative retirement is checked before routing, clocks,
    # receipts, leases, publishers, git, model-bearing commands, or deploy.
    # A malformed retirement-looking row raises just as early and fail-closed.
    from forge.lifecycle.merge_retirement import refuse_retired_build

    refuse_retired_build(deps.pool.connection, build_id)
    # A MISSION'S BUILD IS PRESSED BY ITS OWN RULES, WHOEVER CALLS (missions
    # design, 17 October 2026, sections 6 and 7). A feature built inside a
    # mission is never deployed: it lands on its mission's branch only, so
    # ``deploy`` is forced off whatever the caller passed. The mission's own
    # merge into main always joins under ``mission-<id>`` (round 2, R1).
    from forge.lifecycle.mission_builds import mission_join_name, mission_of_build

    belongs_to = mission_of_build(deps.pool, build_id)
    if belongs_to is not None and belongs_to.is_feature and deploy:
        logger.info(
            "merge-executor: %s is a feature of mission %s — it is landed on "
            "the mission's branch and never deployed",
            build_id,
            belongs_to.mission_id,
        )
        deploy = False
    if belongs_to is not None and belongs_to.is_merge:
        join_name = mission_join_name(belongs_to.mission_id)
        # DEFENSIVE (section 11): a mission's own merge is never pressed once
        # a stop was asked for. The request is refused once a merge build
        # exists, so this combination should never happen.
        ledger = _ledger_of(deps.pool)
        stop = (
            ledger.execute(
                "SELECT stop_requested_at, stop_requested_by FROM missions WHERE id = ?",
                (belongs_to.mission_id,),
            ).fetchone()
            if ledger is not None
            else None
        )
        if stop is not None and stop[0] is not None:
            raise MissionMergeRefused(
                f"{build_id} is the merge of mission {belongs_to.mission_id}, which "
                f"was stopped by {stop[1]} at {stop[0]}, so it is not pressed"
            )
    feature_routing_receipt: dict[str, object] | None = None
    if deps.feature_routing_required:
        if deps.feature_routing_gate is None:
            raise RuntimeError("required merge execution has no feature routing gate")
        committed = deps.feature_routing_gate.require_committed_success(correlation_id)
        feature_routing_receipt = committed.to_wire()
    started = deps.clock()
    receipts_dir = _receipts_root_of(deps, repo) / f"merge-{build_id}"
    # The branch of record for this press (Part M, rules 54 and 55).
    merge_branch = str(merge_branch or "").strip() or None
    branch = branch_to_merge(feature_id, merge_branch)
    # The subject as the thread line names it: the branch is said only when it
    # is not the feature's own, so a feature build's words are unchanged.
    named = f"{feature_id} (branch {branch})" if merge_branch is not None else feature_id
    # The name the join's folders and branches are made under (missions,
    # round 2 R1): the feature id unless the caller named another.
    join_as = str(join_name or "").strip() or feature_id
    # A mission feature's landing has no card and so no previewed tree.
    if not deploy:
        expected_preview_tree = None
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
    # One runbook group per execution. A takeover must ask the target afresh;
    # the durable checked artifact and selection carry identity across presses.
    deploy_run_id = str(uuid.uuid4())
    task_id = _deploy_task_id(feature_id)
    tree_path: Path | None = None
    # The commit the laid-out tree is a tree OF, and the commit the standing
    # candidate was checked at — so a later leg knows which scripts belong to it.
    tree_commit: str | None = None
    candidate_commit: str | None = None
    candidate_standing = False
    first_profile: FirstDeploymentProfile | None = None
    selected_profile: Any = None
    # THE PROJECT'S OWN STEPS RUN FROM A TREE AT THE RIGHT COMMIT, in a sandbox
    # (3 October 2026). A repository with a sandbox is deployed out of the
    # sandbox's own clone, and nothing keeps that clone's working copy up to
    # date: merges are joined in working folders of their own and published to
    # the remote, and the copy stays wherever it was. The candidate check
    # always ran from a tree laid out at the commit being checked, but every
    # other leg — the "what is running" question, the promote with its health
    # checks, live gate and revert, and the candidate's teardown — ran the
    # project's scripts out of that working copy. On 3 October it held a deploy
    # script from July that did not know the question, took it for a plain
    # deploy, and put September's code live, which crashed. So for a
    # repository with a sandbox every leg runs from a tree of the commit it is
    # about, laid out the way the check's is. Every other repository's legs run
    # exactly where they always have.
    from forge.config.sandboxes import sandbox_for

    scripts_from_the_commit = sandbox_for(deps.config, repo) is not None

    def _deployed_in() -> str | None:
        """Where this repository's checks and deploys run, as a card says it.

        A repository with a sandbox runs all of them in there (3 October
        2026). The path this coordinator was given for it holds nothing, so
        the profile cannot be read there and the card used to say nowhere at
        all. Every other repository is decided from its profile, as before.
        """
        if scripts_from_the_commit:
            return "docker-sandbox"
        return deployed_in_for(repo_root)
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
    # THE NAME IS THIS ONE PRESS (3 October 2026), not the process. It used
    # to be the process alone, and in the container the coordinator is
    # always process 1, so the coordinator's next press counted as the same
    # worker and took over a hold that had been kept on purpose because the
    # last press's merge might still be running. So each press has a name of
    # its own: the process and a short random part. A press that ended
    # settled puts its hold down (below), so pressing again after a refusal
    # is not made to wait; one whose ending is unknown keeps it until the
    # lease runs out. Nothing reads the name back, and it is not a credential.
    worker_name = f"merge-press:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    # THE HOLD IS PUT DOWN WHEN THE PRESS HAS REALLY FINISHED (3 October
    # 2026). The lease used to be taken and never put down, so every press
    # held its build for the whole lease however it ended, and a retry of
    # FEAT-E592 at 15:15 was refused because of it. The turn number fences
    # writes to the record, not a merge already sent into a sandbox.
    #
    # SETTLED BY DEFAULT (15 October 2026, after three review rounds of
    # naming endings one at a time). Every ending the press RETURNS is a
    # finished press, and the hold is put down — unless something it started
    # may still be running: a leg or a step that raised, got no answer or was
    # stopped at its time limit, or a merge command, check-join or send whose
    # answer was lost (``a_leg_did_not_complete``); something left standing
    # (``something_is_left_standing``, ``candidate_standing``); or a record
    # this worker could no longer write (``failed_step == "record"``, a
    # replaced worker). A cancellation or a crash never reaches the decision,
    # so the hold is kept for those too. Whatever is kept lapses on its own.
    # ``held_turn`` is the turn this press took the lease on; 0 = none.
    held_turn = 0
    # Set when this press's own refusal could not take the record (a live
    # hold, or a stopped press's ending still owed): the press is then turned
    # away, a no-op for the build, like a press whose main take was refused.
    turned_away_by: list[str] = []
    # Set when a deploy-stage leg got no answer (it raised, or the stage says
    # a step's request got none), or a candidate tree's removal was not
    # confirmed done, or a join's working folder's removal or the retained
    # worktree's retirement got no answer back; never cleared. An ANSWER, of
    # any outcome, does not set it (15 October 2026): a red or could-not-start
    # candidate check, a removal or retirement answered "not done", have all
    # finished. The final clean-up counts too: the hold is decided after it.
    a_leg_did_not_complete = False
    # Set while a leg's answer says something it started is still standing
    # (the candidate), and cleared when a teardown answers that it took it
    # down. A candidate check that passed leaves its candidate standing on
    # purpose for the promote, so that one answer does not set it.
    something_is_left_standing = False

    # WHICH JUDGE FILES THE MERGE CHECKED WITH MAIN'S COPIES (15 October 2026,
    # the merge-check design, item 8): the build system's report names them,
    # and the merge-checks record line keeps them, so a press that picks the
    # join up can say the same. Those the target does not have
    # (``judge_files_not_on_target``) were left out, not checked with main's
    # version, and are said so (review R4), as the build system's own receipt
    # says it. At most two sentences, added to every ending's detail in one
    # place (``_settled``). A list, so a nested reader can set it.
    _main_copies_said: list[str] = []

    def _remember_main_copies(paths: Any, not_on_target: Any = None) -> None:
        from forge.pipeline.merge_check_reading import shown_path

        if _main_copies_said:
            return

        def names_of(values: Any) -> list[str]:
            if not isinstance(values, (list, tuple)):
                return []
            return [str(p) for p in values if str(p or "").strip()]

        def listed(names: list[str]) -> str:
            shown = [shown_path(n) for n in names]
            if len(shown) == 1:
                return shown[0]
            return ", ".join(shown[:-1]) + " and " + shown[-1]

        absent = set(names_of(not_on_target))
        every = names_of(paths)
        kept = [n for n in every if n not in absent]
        left_out = [n for n in every if n in absent]
        if kept:
            many = len(kept) > 1
            _main_copies_said.append(
                f"The merge used main's {'versions' if many else 'version'} of "
                f"{listed(kept)} and did not run this build's changes to "
                f"{'them' if many else 'it'}."
            )
        if left_out:
            many = len(left_out) > 1
            _main_copies_said.append(
                f"The merge left out {listed(left_out)}, which main does not "
                f"have, and did not run this build's "
                f"{'copies of them' if many else 'copy of it'}."
            )

    def _with_main_copies_said(outcome: MergeDeployOutcome) -> MergeDeployOutcome:
        """Every ending says which judge files the merge checked with main's
        copies, when the merge said so (one place, for every ending)."""
        for said in _main_copies_said:
            if said not in (outcome.detail or ""):
                outcome.detail = f"{outcome.detail} {said}".strip()
        return outcome
    # THE COMMIT THIS BUILD STARTS FROM, off this coordinator's own record,
    # read once and kept (23 September 2026). It is what the project's own
    # declarations are read at on BOTH sides of the deploy: this side composes
    # the deploy block from the profile at this commit, and the helper is
    # stamped with it and confirms it before it reads a line. A list of one, so
    # a nested reader can fill it without a nonlocal.
    _start_commit_memo: list[str | None] = []
    # THE SETTING NAMES THIS BUILD'S PROJECT DECLARED, off the same record and
    # kept the same way (13 October 2026, the merge-install design, item 7).
    _launch_settings_memo: list[tuple[str, ...]] = []

    def _write_receipt(name: str, data: dict[str, Any]) -> None:
        _write_the_receipt(receipts_dir, build_id, name, data)

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

    def _the_reports_words(outcome: MergeDeployOutcome) -> MergeDeployOutcome:
        """The words this press adds to its ending before it is reported.

        Applied ONCE to the ending that is reported (the last guard escapes,
        so it is not applied twice), and to a copy for the ending written
        down before the clean-up.
        """
        conformance_warning = digest_conformance.get("warning")
        if conformance_warning:
            outcome.detail = f"{outcome.detail}\nWARNING: {conformance_warning}"
        # A CANDIDATE THIS PRESS COULD NOT NAME IS SAID IN THE REPORT, not only
        # in a log: something is still standing and a person has to remove it.
        # The cleanup runs before this, in the caller's ``finally``, so by here
        # the answer is known.
        left_standing = str(gate.get("candidate_left_standing") or "").strip()
        if left_standing:
            outcome.detail = f"{outcome.detail}\n{left_standing}"
        if gate_began and outcome.gate_before_merge is None:
            outcome.gate_before_merge = _gate_for_report()
        outcome.branch = branch
        # THE LAST GUARD (review R7): the report's words keep their own line
        # breaks and nothing else of Unicode categories Cc, Cf, Zl or Zp.
        from forge.pipeline.merge_check_reading import card_body

        outcome.detail = card_body(outcome.detail)
        return outcome

    def _why_the_hold_is_kept(outcome: MergeDeployOutcome) -> str | None:
        """Why this press keeps its hold after its clean-up, or ``None``.

        The 15 October 2026 rules: a deploy-stage leg with no answer, a
        candidate a teardown did not confirm down, a candidate a passed check
        left standing, or a record that could not be written. One place, for
        the press's own release and for the ending it writes down.
        """
        if a_leg_did_not_complete:
            return "a deploy-stage leg's answer was lost"
        if something_is_left_standing:
            return "a candidate's teardown did not confirm it down"
        if candidate_standing:
            return "a candidate the check stood up may still be standing"
        if outcome.failed_step == "record":
            return "the publication record could not be written"
        return None

    def _write_the_ending_down(outcome: MergeDeployOutcome, *, cleanup_done: bool) -> None:
        """Keep this press's ending on its own row (press lease recovery, 18
        October 2026), so a coordinator that stops before the report can be
        reported for, word for word, through the same path — with whether
        the clean-up had run, and what the hold's decision is or would be."""
        turn = held_turn
        if dry_run or not turn:
            return
        try:
            store_here = _publication_store()
            if store_here is None:
                return
            said = stored_outcome(
                outcome,
                feature_id=feature_id,
                correlation_id=correlation_id,
                started=started,
                receipts_dir=receipts_dir,
                digest_warning=digest_conformance.get("warning"),
                join_as=join_as,
            )
            said["cleanup_done"] = cleanup_done
            said["kept_because"] = (
                _why_the_hold_is_kept(outcome) if held_turn else None
            )
            said.update(_what_may_be_standing())
            store_here.record_outcome(
                build_id=build_id,
                turn=turn,
                holder=worker_name,
                now=deps.clock(),
                outcome=said,
                attempt_id=merge_execution_attempt_id,
            )
        except Exception as exc:  # noqa: BLE001 — never costs a result
            logger.warning(
                "merge-executor: %s's ending could not be written down (%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )

    def _turned_away(outcome: MergeDeployOutcome) -> MergeDeployOutcome:
        """A press a live hold turned away: a no-op for this build.

        Not reported (no bus message, no report receipt, no report row), the
        build row and the publication record left exactly as they are, no
        repair row: the press that holds the record, or the recovery, owns
        the build's ending. Its own receipt says what happened; the caller
        tells the person who asked. A consumer press carrying an approved
        decision's attempt leaves a note under that attempt, so the router
        does not wait for a press that will never run.
        """
        outcome = _the_reports_words(outcome)
        logger.warning(
            "merge-executor: %s's merge word was turned away — %s",
            build_id,
            outcome.detail,
        )
        _write_receipt(
            "merge_deploy_turned_away.json",
            {
                "step": "turned-away",
                "detail": outcome.detail,
                "attempt_id": merge_execution_attempt_id,
                "at": deps.clock().isoformat(),
            },
        )
        if not dry_run and merge_execution_attempt_id is not None:
            now = deps.clock()
            try:
                deps.pool.record_stage(
                    StageLogEntry(
                        build_id=build_id,
                        stage_label=MERGE_TURNED_AWAY_STAGE_LABEL,
                        target_kind="local_tool",
                        target_identifier=MERGE_TURNED_AWAY_TARGET_IDENTIFIER,
                        status="SKIPPED",
                        gate_mode=None,
                        started_at=now,
                        completed_at=now,
                        duration_secs=0.0,
                        details={
                            "detail": outcome.detail,
                            MERGE_DECISION_DETAILS_KEY: {
                                MERGE_EXECUTION_ATTEMPT_VERSION_KEY: (
                                    MERGE_EXECUTION_ATTEMPT_VERSION
                                ),
                                MERGE_EXECUTION_ATTEMPT_ID_KEY: merge_execution_attempt_id,
                            },
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a note never costs anything
                logger.error(
                    "merge-executor: %s's turned-away note could not be written (%s: %s)",
                    build_id,
                    type(exc).__name__,
                    exc,
                )
        return outcome

    async def _finish_this_press(outcome: MergeDeployOutcome) -> MergeDeployOutcome:
        """The ending, in its report's words, through the finishing routine
        the recovery runs too (:func:`finish_the_press`), on this press's own
        turn and under its own lease."""
        ending = PressEnding(
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            outcome=outcome,
            started=started,
            receipts_dir=receipts_dir,
            attempt_id=merge_execution_attempt_id,
            digest_warning=digest_conformance.get("warning"),
        )
        if dry_run:
            return report_a_dry_run(deps, ending)
        store_here = _publication_store() if held_turn else None
        kept = _why_the_hold_is_kept(outcome) if held_turn else None
        landing = bool(belongs_to is not None and belongs_to.is_feature)
        if kept and landing and store_here is not None:
            ending.warning = landing_left_standing_sentence(
                feature_id,
                store_here.read(build_id),
                kept,
                handed=gate.get("identity_handed_to_the_check"),
            )
            ending.mission_id = belongs_to.mission_id
        said = await finish_the_press(
            deps,
            ending,
            store=store_here,
            press_turn=held_turn or None,
            lease_turn=held_turn or None,
            holder=worker_name,
            hold=the_hold_after_finishing(kept, a_mission_landing=landing, by_recovery=False),
            tell=getattr(deps, "tell_mission", None),
        )
        if said == "replaced":
            logger.warning(
                "merge-executor: %s's press was taken over after its result; "
                "the recovery finishes it",
                build_id,
            )
        elif kept:
            logger.info(
                "merge-executor: %s's hold is kept — this press ended where what "
                "it started may still be running (%s); %s",
                build_id,
                kept,
                said,
            )
        return outcome

    async def _dispatch(leg: str, **extra: Any) -> Any:
        nonlocal a_leg_did_not_complete, something_is_left_standing
        before = a_leg_did_not_complete
        # Until it answers; a raise leaves it set.
        a_leg_did_not_complete = True
        try:
            result = await deps.deploy_dispatcher(
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
                **({"first_deployment_profile": {"selection": first_profile.to_wire(), "turn": held_turn}} if first_profile is not None else {}),
                **extra,
            )
        except ProfileSelectionRefusedBeforeDispatch:
            a_leg_did_not_complete = before
            raise
        # AN ANSWER, WHATEVER IT SAYS, IS A LEG THAT HAS FINISHED (15 October
        # 2026, the rule a986459c set for the retained worktree). An ordinary
        # red candidate check, or one whose candidate could not start, kept
        # the hold, and the release idle check then waited on a build with
        # nothing running. ``None`` is no stage at all (deploy switched off:
        # nothing ran). Two answers still keep the hold: one whose step's
        # request got no answer (the stage says ``answer_lost``: the script
        # may still be running in the sandbox), and one saying the candidate
        # is still standing — until a teardown answers that it is down.
        detail = (getattr(result, "detail", None) or {}) if result is not None else {}
        # CONFIRMED OR NOT (15 October 2026): a leg answer the stage did not
        # confirm as complete keeps the hold, whatever else it says. ``None``
        # is no stage at all: nothing ran.
        if detail.get("answer_lost") or (
            result is not None and getattr(result, "confirmed", False) is not True
        ):
            return result
        outcome_word = getattr(result, "outcome", None)
        standing = detail.get("candidate") == "standing"
        if leg == "candidate_down":
            something_is_left_standing = not (outcome_word == "complete" and not standing)
        elif standing and not (leg == "candidate_check" and outcome_word == "complete"):
            something_is_left_standing = True
        a_leg_did_not_complete = before
        return result

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
            deployed_in=_deployed_in() if gate.get("ran") else None,
            gate_before_merge=_gate_for_report(),
        )

    def _could_not_check(why: str) -> MergeDeployOutcome:
        gate["refusal"] = f"could not be checked: {why}"
        return _refused_before_merge(
            f"{feature_id} could not be checked in the sandbox before merging: "
            f"{why}; nothing was merged and the branch is kept."
        )

    async def _the_identity_a_teardown_must_name() -> tuple[dict[str, str] | None, str]:
        """``({setting: identity}, "")`` for a teardown, or ``(None, why not)``.

        ONE BUILD'S CLEANUP MUST NEVER TOUCH ANOTHER'S CANDIDATE (25 September
        2026, the fifth review). The cleanup used to ask for the candidate to
        come down and name nothing, and a project whose candidate belongs to
        one check rather than to a shared name then had to go LOOKING for
        candidates to remove — so one build's ending removed every other
        build's standing candidate, and their data with them. It was driven:
        three builds checked, the first ended without publishing, and all
        three candidates went.

        The name a teardown may use is the identity the CHECK was handed, and
        nothing else: it is the one thing recorded on the publication record
        that the project itself made its candidate out of. Both the setting
        and the value belong to the project; this carries them as text.
        """
        declaration = await _how_the_project_wants_the_identity()
        if declaration is None:
            return None, (
                "this project declares no identity for the thing that is "
                "checked, so nothing was handed to the check and there is no "
                "name a teardown could use"
            )
        setting = str(getattr(declaration, "setting", "") or "").strip()
        if not setting:
            return None, (
                "this project's identity declaration names no setting for the "
                "identity to be handed over in"
            )
        handed = str(gate.get("identity_handed_to_the_check") or "").strip()
        if not handed:
            return None, (
                "no identity was handed to the check, so nothing recorded "
                "says which candidate this build stood up"
            )
        return {setting: handed}, ""

    async def _tear_down_candidate() -> None:
        """Best-effort: the candidate that a stopped run left standing.

        BY NAME OR NOT AT ALL. With no recorded identity the candidate is left
        exactly where it is and the report says so: a teardown that names
        nothing would have to find candidates by looking, and what it found
        could belong to another build's check.
        """
        nonlocal candidate_standing
        identity_env, why_not = await _the_identity_a_teardown_must_name()
        if identity_env is None:
            sentence = (
                f"the candidate for {feature_id} was left in place because its "
                f"identity is not recorded ({why_not}); remove it by hand with "
                f"this project's own teardown step, handing it the identity of "
                f"the candidate you mean. Nothing was removed automatically, "
                f"because a teardown that names nothing has to go looking, and "
                f"what it finds can belong to another build."
            )
            gate["candidate_left_standing"] = sentence
            logger.warning("merge-executor: %s", sentence)
            return
        # BY THE SCRIPT THAT STOOD IT UP (3 October 2026): for a repository
        # with a sandbox, the teardown runs from the tree of the commit the
        # candidate was checked at, never from the clone's working copy — whose
        # step may not know the teardown at all and do something else instead.
        scripts_cwd, no_scripts = await _the_scripts_at(candidate_commit)
        if no_scripts:
            sentence = (
                f"the candidate for {feature_id} was left in place because "
                f"{no_scripts}; remove it by hand with this project's own "
                f"teardown step, handing it the identity "
                f"{next(iter(identity_env.values()))}. Nothing was removed "
                f"automatically, because the teardown step in the working copy "
                f"may be older than the candidate and not know how."
            )
            gate["candidate_left_standing"] = sentence
            logger.warning("merge-executor: %s", sentence)
            return
        try:
            result = await _dispatch(
                "candidate_down",
                identity_env=identity_env,
                **({"candidate_cwd": scripts_cwd} if scripts_cwd else {}),
            )
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
        nonlocal tree_path, a_leg_did_not_complete
        if candidate_standing:
            await _tear_down_candidate()
        removed: bool | None = None
        if tree_path is not None:
            removed = await git.remove_candidate_tree(feature_id, str(tree_path))
            # Only a removal with no answer may still be running; one the
            # venue answered "not removed" is a leftover folder (15 October
            # 2026).
            if removed is None:
                a_leg_did_not_complete = True
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
                # Only when there is something to say: the candidate that was
                # left standing because nothing recorded its identity.
                **(
                    {"candidate_left_standing": gate["candidate_left_standing"]}
                    if gate.get("candidate_left_standing")
                    else {}
                ),
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
        nonlocal candidate_standing, tree_path, tree_commit
        logger.error(
            "merge-executor: %s's publication record has moved on — this "
            "worker has been replaced and stops without tidying up",
            build_id,
        )
        candidate_standing = False
        tree_path = None
        tree_commit = None
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
        """What identifies the thing that was checked.

        REWRITTEN 23 September 2026, after the second review of the executor
        stage. This used to record a gap and name it honestly: the check knew
        nothing about identity, and the deploy resolved a name of its own at
        the moment it deployed. That gap was then driven — a second build
        replaced the name in between and its work went live under the first
        build's sentence — so the gap is closed here, where it was.

        What is recorded now is the artifact the CHECK itself reported, under
        the marker the project declared for it: the project's own unrepeatable
        name for the thing that was checked, captured at the moment it was
        checked. The deploy is handed THAT and must deploy exactly it.

        ``artifact`` is ``None`` when the project's check said nothing this
        side can read. That is recorded as it is, and the deploy refuses on it
        rather than falling back to a name the check never confirmed.
        """
        return {
            "as_it_stands": gate.get("checked_identity"),
            # THE ONE FIELD THE DEPLOY READS BACK.
            "artifact": gate.get("checked_artifact"),
            "handed_to_the_check": gate.get("identity_handed_to_the_check"),
            "how_the_deploy_identifies_it": (
                "the artifact the check itself reported, captured when it was "
                "checked and handed back to the deploy by that same name"
            ),
        }

    async def _how_the_project_wants_the_identity() -> Any:
        """This project's identity declaration, or ``None``.

        The same answer the deploy leg works from, asked earlier: the check has
        to be handed the identity so it can pin what it checked, and it has to
        be read back under the marker the project declared.
        """
        known = await _the_target_and_how_it_wants_the_identity()
        if known is None:
            return None
        _, declaration = known
        return declaration if getattr(declaration, "declared", False) else None

    async def _profile_bytes_at(commit: str) -> Any:
        answer = await git.read_file_at_commit(commit, DEPLOY_PROFILE_PATH, raw=True)
        parent = None
        if not answer.found and answer.mode is None and not answer.refusal:
            parent = await git.read_file_at_commit(commit, "deploy", raw=True)
        validate_committed_profile_path(answer, parent)
        return answer

    async def _select_profile_for_join(
        *, target_commit: str, candidate_commit: str, candidate_tree: str,
        joined_commit: str, joined_tree: str, attempt: int, store: Any,
    ) -> None:
        nonlocal first_profile, selected_profile
        import hashlib

        first_profile, selected_profile = None, None
        if not scripts_from_the_commit:
            return
        start = _the_recorded_start_commit()
        if not start:
            return  # Existing dispatcher refuses a missing profile; no new authority.
        started = await _profile_bytes_at(start)
        if started.found:
            profile, why = _the_profile_out_of(started, start)
            if profile is None:
                raise ValueError(why)
            return  # Existing instructions always govern this build.
        if dry_run or store is None or not held_turn:
            raise ValueError("a first deployment profile needs a real approved publication")
        cx = _ledger_of(deps.pool)
        if cx is None:
            raise ValueError("the first deployment has no readable approval record")
        offer, decision = read_profile_approval(cx, build_id)
        if (offer.get("repo") != repo or offer.get("candidate_sha") != candidate_commit
                or offer.get("candidate_tree") != candidate_tree
                or expected_candidate_sha != candidate_commit
                or expected_candidate_tree != candidate_tree
                or decision.get("execution_attempt_id") != merge_execution_attempt_id):
            raise ValueError("the first deployment does not match the exact approved candidate")
        record = store.read(build_id)
        if (record.turn != held_turn or record.lease_holder != worker_name
                or record.attempt != attempt or record.j_commit != joined_commit
                or record.build_tip != candidate_commit or record.g_commit != target_commit):
            raise ValueError("the first deployment publication changed before selection")
        target = await _profile_bytes_at(target_commit)
        if target.found:
            raise ValueError("the fetched target already has deployment instructions; the first profile cannot replace them")
        candidate = await _profile_bytes_at(candidate_commit)
        joined = await _profile_bytes_at(joined_commit)
        if not candidate.found or not joined.found or candidate.content != joined.content:
            raise ValueError("the joined deployment profile must equal the approved candidate's ordinary profile")
        profile, why = _the_profile_out_of(candidate, candidate_commit)
        if profile is None:
            raise ValueError(why)
        identity = declared_identity(profile)
        if (profile.candidate is None or profile.live_gate is None
                or not profile.live_gate.driver or not identity.declared
                or not identity.asked_with):
            raise ValueError("the first deployment profile needs a candidate, live driver and verified deployment identity")
        first_profile = FirstDeploymentProfile.from_wire({
            "version": 1, "repo": repo, "build": build_id,
            "offer_id": offer["request_id"],
            "decision_id": decision["execution_attempt_id"],
            "candidate_commit": candidate_commit, "candidate_tree": candidate_tree,
            "start_commit": start, "target_commit": target_commit,
            "joined_commit": joined_commit, "joined_tree": joined_tree,
            "profile_sha256": hashlib.sha256(candidate.content.encode("utf-8")).hexdigest(),
            "attempt": attempt, "candidate_cwd": str(tree_path),
        })
        selected_profile = profile

    async def _recover_profile_selection(record: Any) -> None:
        nonlocal first_profile, selected_profile
        import hashlib

        selected = selection_from_record(record)
        if selected is None:
            return
        cx = _ledger_of(deps.pool)
        if cx is None:
            raise ValueError("the recorded first deployment has no readable authority")
        reference = confirmed_profile_reference(cx, build_id, _the_recorded_start_commit() or "")
        if reference is None or reference["turn"] != held_turn:
            raise ValueError("the recorded first deployment no longer belongs to this publication")
        answer = await _profile_bytes_at(selected.joined_commit)
        if not answer.found or hashlib.sha256(answer.content.encode("utf-8")).hexdigest() != selected.profile_sha256:
            raise ValueError("the recorded first deployment profile no longer matches its digest")
        profile, why = _the_profile_out_of(answer, selected.joined_commit)
        if profile is None:
            raise ValueError(why)
        first_profile, selected_profile = selected, profile

    async def _lay_the_tree_out(sha: str) -> MergeDeployOutcome | None:
        """Lay out the exact tree of ``sha`` for the live check. None = it is there."""
        nonlocal tree_path, tree_commit, a_leg_did_not_complete
        excluded_now: bool | None = None
        try:
            excluded_now = await git.ensure_candidate_trees_excluded()
            laid_out = await git.materialise_candidate_tree(feature_id, str(sha))
            # A tree the venue did not confirm laid out in full (15 October
            # 2026) may still be being written: keep the hold.
            if getattr(laid_out, "confirmed", False) is not True:
                a_leg_did_not_complete = True
                return _could_not_check("the sandbox did not confirm the candidate archive layout")
            if scripts_from_the_commit and laid_out.tree != await git.rev_parse(f"{sha}^{{tree}}"):
                return _could_not_check("the sandbox candidate archive has a different tree")
            tree_path = Path(laid_out.path)
            tree_commit = str(sha)
            if excluded_now is None:
                excluded_now = laid_out.exclude_written
            if not gate["candidate_tree"]:
                gate["candidate_tree"] = laid_out.tree
        except CandidateTreeError as exc:
            # A lay-out whose answer was lost may still be writing the tree in
            # the sandbox: keep the hold. One the venue refused in its own
            # words has finished (15 October 2026).
            if not exc.confirmed:
                a_leg_did_not_complete = True
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

    async def _the_scripts_at(sha: str | None) -> tuple[str | None, str | None]:
        """Where a leg about ``sha`` runs the project's own steps.

        ``(None, None)`` for a repository without a sandbox: its legs run
        where they always have, and nothing is laid out for them. For one with
        a sandbox, ``(path, None)`` — the tree of ``sha``, laid out with the
        very operation the check uses (the one already there when it is of
        ``sha``) — or ``(None, why not)``, and the caller then runs nothing
        rather than fall back to a working copy nobody keeps up to date.
        """
        nonlocal tree_path, tree_commit, a_leg_did_not_complete
        if not scripts_from_the_commit:
            return None, None
        wanted = str(sha or "").strip()
        if not wanted:
            return None, (
                "no commit is recorded for it, so there is no tree to run the "
                "project's own steps from"
            )
        if tree_path is not None and tree_commit == wanted:
            return str(tree_path), None
        try:
            await git.ensure_candidate_trees_excluded()
            laid_out = await git.materialise_candidate_tree(feature_id, wanted)
        except CandidateTreeError as exc:
            # A lay-out whose answer was lost may still be writing the tree in
            # the sandbox: keep the hold. A refusal has finished.
            if not exc.confirmed:
                a_leg_did_not_complete = True
            return None, (
                f"the tree of {wanted[:10]} could not be laid out to run the "
                f"project's own steps from ({exc})"
            )
        if getattr(laid_out, "confirmed", False) is not True:
            a_leg_did_not_complete = True
            return None, "the sandbox did not confirm the candidate archive layout"
        if laid_out.tree != await git.rev_parse(f"{wanted}^{{tree}}"):
            return None, "the sandbox candidate archive has a different tree"
        tree_path = Path(laid_out.path)
        tree_commit = wanted
        return str(tree_path), None

    async def _run_the_candidate_check(sha: str) -> MergeDeployOutcome | None:
        """Run the registered live checks against the laid-out tree of ``sha``.

        ``None`` when every check passed; an honest outcome otherwise. This is
        the factory's own live check, and since the merge word's join it runs
        on the JOINED commit and only there: checking the build's own branch
        was not enough once the remote could have moved under it.
        """
        nonlocal candidate_standing, candidate_commit
        # THE IDENTITY IS HANDED TO THE CHECK, not only to the deploy (24
        # September 2026). The check is the only moment at which the thing that
        # was checked certainly still exists, so it is the only moment at which
        # its identity can honestly be captured. Handing it here lets the
        # project pin what it checked under a name nothing else can be given,
        # and report the artifact's own identity back.
        declaration = await _how_the_project_wants_the_identity()
        identity_env: dict[str, str] | None = None
        if declaration is not None:
            handed = fixed_identity(
                j_commit=sha, content=gate.get("candidate_tree")
            ).text
            gate["identity_handed_to_the_check"] = handed
            identity_env = {declaration.setting: handed}
        try:
            checked = await _dispatch(
                "candidate_check",
                candidate_cwd=str(tree_path),
                identity_env=identity_env,
            )
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
        # THE ARTIFACT THE CHECK SAYS IT CHECKED, read out of what the
        # project's own step printed, under the marker the project declared.
        # This is the thing the deploy is handed; it is never worked out again
        # later, because "later" is exactly when another build can have taken
        # the name it would be worked out from.
        if declaration is not None:
            gate["checked_artifact"] = identity_reported_by(
                str(summary.get("candidate_output") or ""),
                marker=declaration.checked_as,
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
        candidate_commit = str(sha)
        return None

    def _publication_store() -> Any:
        """The publication record's store (:func:`_publication_store_for`).

        Each one knows what this press might still have standing, so a result
        it writes is marked with it in the same transaction (press lease
        recovery): a press that stops right after its result is never
        released by a recovery while a candidate may be standing."""
        made = _publication_store_for(deps, build_id)
        if made is not None:
            try:
                made.result_context = _what_may_be_standing
            except Exception:  # noqa: BLE001 — a stand-in store that refuses attributes
                pass
        return made

    def _what_may_be_standing() -> dict[str, Any]:
        return {
            "candidate_may_stand": bool(candidate_standing or something_is_left_standing),
            "leg_unanswered": bool(a_leg_did_not_complete),
            # THE IDENTITY HANDED TO THE CHECK, written with the result (Codex
            # round 4, R10): a check that failed saves no ``checked`` block,
            # so this is the one place a recovery can read it to name the
            # candidate that may still be standing.
            "identity_handed_to_the_check": gate.get("identity_handed_to_the_check"),
        }

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
        """This press's refusal, on its record (:func:`_write_the_refusal_down_for`).

        The press keeps the lease it took for it, like any press, so its
        finishing runs under a live lease it owns (Codex round 1, R3)."""
        nonlocal held_turn
        refused_on = _write_the_refusal_down_for(
            store,
            deps=deps,
            build_id=build_id,
            holder=worker_name,
            feature_id=feature_id,
            repo=repo,
            decided_by=decided_by,
            target_branch=target_branch,
            refusal=refusal,
            starts_a_press=True,
            refuse_if_an_ending_is_owed=True,
            attempt_id=merge_execution_attempt_id,
        )
        if refused_on:
            held_turn = refused_on
        elif refused_on == 0:
            turned_away_by[:] = [str(getattr(store, "last_refusal", None) or "held")]

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
        # A clean ending. If a deploy-stage leg on the way here did not
        # complete (a question that went unanswered, a promote that was
        # stopped), that leg keeps the hold, not this mark.
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

    def _published_to_the_mission_branch(
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
    ) -> MergeDeployOutcome:
        """A mission feature landed: J is on its mission's branch. Nothing more.

        Missions design, section 6. The record's result is "published to the
        mission branch", which no reader takes as a deploy still owed; the
        mission's own merge into main is the only thing that deploys.
        """
        if store is not None and not store.record(
            build_id=build_id,
            turn=turn,
            now=deps.clock(),
            result=RESULT_PUBLISHED_TO_MISSION_BRANCH,
        ):
            return _replaced_here()
        _write_receipt(
            "merge_deploy_publication.json",
            {
                "step": "publication",
                "switched_on": True,
                "result": RESULT_PUBLISHED_TO_MISSION_BRANCH,
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
                "why_nothing_was_deployed": (
                    "this feature is part of a mission: it is merged into the "
                    "mission's branch, and only the mission's own merge into "
                    "main deploys"
                ),
            },
        )
        return MergeDeployOutcome(
            result=RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH,
            status="PASSED",
            merged_sha=j_commit,
            detail=(
                f"{named} was joined onto its mission's branch {target_branch} "
                f"at {g_commit[:10]} in a working folder of its own, the joined "
                f"result {j_commit[:10]} was checked{_the_checks_sentence()}, "
                f"and it was published: {target_branch} on the remote named "
                f"origin is at {(remote_now or j_commit)[:10]} and contains it. "
                "Nothing was deployed: only the mission's own merge into main "
                "deploys."
            ),
            checks_passed=checks_passed,
            checks_total=checks_total,
            deployed_in=None,
            gate_before_merge=_gate_for_report() if gate_began else None,
        )

    def _the_missions_branch() -> str | None:
        """The branch of the mission this build belongs to, or ``None``.

        Read off the mission record the build's ``mission_id`` names; the
        join accepts it as the target only when it is also the build's
        recorded target and the remote has it (``target_branch_now``).
        """
        from forge.lifecycle.mission_builds import mission_of_build

        belongs = mission_of_build(deps.pool, build_id)
        if belongs is None:
            return None
        try:
            row = deps.pool.connection.execute(
                "SELECT branch FROM missions WHERE id = ?", (belongs.mission_id,)
            ).fetchone()
        except Exception:  # noqa: BLE001 — no mission read is no mission branch
            return None
        return str(row[0]) if row is not None and row[0] else None

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

    async def _what_the_target_says(
        *, target: str, declaration: Any, scripts_cwd: str | None = None
    ) -> Any:
        """Ask the project, read-only, what it is running on ``target``.

        Added 23 September 2026, after the second review of the executor stage.
        The only-forwards rule was being applied to the LEDGER'S row, and a run
        that deploys and stops before writing that row leaves it wrong — so a
        later pick-up read a stale row, believed it was moving forwards, and put
        an older result over a newer one. The cure is to ask the thing itself,
        under the lock, before deciding anything.

        The answer is the project's own: a token it uses to name what is
        running, the word that means nothing is running there, or no
        established answer at all. Nothing here knows what a token means;
        placing one is the caller's job, and a token that places nowhere stops
        the deploy.

        Corrected 23 September 2026, after the third review. A step whose own
        query FAILED used to arrive here as "nothing is running": it suppressed
        the error, exited zero, and printed an empty value. Four things are
        read as "not established" now, and every one of them deploys nothing —
        the step exiting non-zero, no line at all, an empty value, and the
        step's own ``<marker>_UNKNOWN=`` line, whose reason is carried into the
        sentence a person reads.
        """
        why = ""
        said = ""
        try:
            asked = await _dispatch(
                "what_is_running",
                ask_env={declaration.asked_with: "1"},
                **({"candidate_cwd": scripts_cwd} if scripts_cwd else {}),
            )
        except Exception as exc:  # noqa: BLE001 — a question, never a crash
            logger.warning(
                "merge-executor: %s could not be asked what it is running "
                "(%s: %s)",
                target,
                type(exc).__name__,
                exc,
            )
            return _TargetSaid(
                word="no-answer",
                identity=None,
                why=f"the question raised {type(exc).__name__}: {exc}",
                said="",
            )
        if asked is None:
            return _TargetSaid(
                word="no-answer",
                identity=None,
                why="the deploy stage is switched off, so nothing could be asked",
                said="",
            )
        detail = getattr(asked, "detail", None) or {}
        said = str(detail.get("deploy_output") or "")
        # THE STEP'S OWN REASON, when it gave one. A step that exits non-zero
        # has still usually said why on its last line, and that sentence is far
        # more use to whoever reads the result than "it did not finish".
        the_steps_reason = why_the_target_could_not_say(
            said, marker=declaration.running_as
        )
        if getattr(asked, "outcome", None) != "complete":
            why = (
                "the project's own read-only step did not finish "
                f"({getattr(asked, 'outcome', None) or 'it answered nothing'})"
            )
            if the_steps_reason:
                why = f"{why}: {the_steps_reason}"
            return _TargetSaid(
                word="no-answer", identity=None, why=why, said=said
            )
        word, value = what_the_target_says(said, marker=declaration.running_as)
        if word == "no-answer":
            if value:
                # The step said, in its own words, why it could not tell.
                why = (
                    "the project's own read-only step could not establish what "
                    f"is running: {value}"
                )
            else:
                why = (
                    "the project's own read-only step printed no "
                    f"{declaration.running_as} line this press can read, so "
                    "what is there was not established. An empty answer is not "
                    "read as 'nothing is running'"
                )
            value = None
        return _TargetSaid(word=word, identity=value, why=why, said=said)

    def _which_commit_is(
        identity: str | None, *, grant: Any, target: str
    ) -> str | None:
        """Which joined commit is the thing the target says it is running?

        The target's own row first, because when it still agrees it is the
        cheapest and most direct answer; then the publication records, which
        hold identity → joined commit for every build and are what place a
        result whose own ledger line was lost. ``None`` = nowhere, and the
        caller then deploys nothing.
        """
        wanted = str(identity or "").strip()
        if not wanted:
            return None
        if (
            grant.running_identity
            and str(grant.running_identity).strip() == wanted
            and grant.running_commit
        ):
            return str(grant.running_commit)
        store = _publication_store()
        finder = getattr(store, "which_commit_was_checked_as", None)
        if finder is None:
            return None
        try:
            return finder(wanted, repo=repo)
        except Exception as exc:  # noqa: BLE001 — a lookup, never a crash
            logger.warning(
                "merge-executor: what %s is running (%s) could not be placed "
                "(%s: %s)",
                target,
                wanted,
                type(exc).__name__,
                exc,
            )
            return None

    def _the_recorded_start_commit() -> str | None:
        """The commit THIS COORDINATOR'S LEDGER records this build as starting
        from, or ``None`` when it recorded none.

        The same fact the helper is stamped with and asks back for, read from
        the same row, so the two sides are bound to one commit.
        """
        if _start_commit_memo:
            return _start_commit_memo[0]
        recorded: str | None = None
        try:
            point = deps.pool.read_start_point(build_id)
            if getattr(point, "recorded", False):
                recorded = str(getattr(point, "start_commit", None) or "").strip() or None
        except Exception as exc:  # noqa: BLE001 — an unreadable record is not a crash
            logger.warning(
                "merge-executor: the commit %s starts from could not be read "
                "off the record (%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )
        _start_commit_memo.append(recorded)
        return recorded

    def _the_recorded_launch_settings() -> tuple[str, ...]:
        """The setting NAMES this coordinator's ledger records this build's
        project as declaring, or ``()`` when it recorded none.

        Names only, off the record, never out of this process's own
        environment. The join's command runs the project's declared install
        and checks, and the helper that runs it passes only the factory's own
        list unless it is told these; it checks each name against the
        project's declaration at the recorded commit before it passes one. An
        unreadable record is logged and sends nothing, which is how the join
        ran before.
        """
        if _launch_settings_memo:
            return _launch_settings_memo[0]
        names: tuple[str, ...] = ()
        try:
            reader = getattr(deps.pool, "read_launch_settings", None)
            if reader is not None:
                names = tuple(
                    str(name).strip()
                    for name in (reader(build_id) or ())
                    if str(name).strip()
                )
        except Exception as exc:  # noqa: BLE001 — an unreadable record is not a crash
            logger.warning(
                "merge-executor: the settings %s's project declared could not "
                "be read off the record (%s: %s), so the join is sent none",
                build_id,
                type(exc).__name__,
                exc,
            )
            names = ()
        _launch_settings_memo.append(names)
        return names

    async def _the_target_and_how_it_wants_the_identity() -> tuple[str, Any] | None:
        """This project's deployment target, and its identity declaration.

        Both come from the PROJECT: the target is the project and the
        environment its own deploy profile declares, and the two names — the
        setting the identity is handed in, the marker the step reports it
        after — are the project's to choose. A project whose profile cannot be
        read has no target this press can take a lock on, and the press says
        so rather than guessing one.

        READ AT THE COMMIT THIS BUILD STARTS FROM (23 September 2026, the
        seventh review). This is the only place the deploy block's setting
        names are composed, and it was composing them off the WORKING COPY
        while the helper checked them against the committed file — so an
        uncommitted ``identity:`` line in the checkout became what this side
        sent, and the project's real declaration was refused. Both sides read
        the one committed file at the one recorded commit now. A copy with no
        recorded commit falls back to its own committed HEAD, never its
        working tree.
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
        at = _the_recorded_start_commit()
        if first_profile is not None:
            profile, why_not = selected_profile, None
        else:
            profile, why_not = await _the_profile_where_it_lives(git, repo_root, at)
        if profile is None:
            logger.warning(
                "merge-executor: %s's %s could not be read at %s (%s), so "
                "there is no deployment target to take a lock on",
                repo,
                DEPLOY_PROFILE_PATH,
                at or "the committed HEAD of the copy this coordinator has",
                why_not,
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
            deployed_in=_deployed_in(),
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

        A MISSION FEATURE'S LANDING STOPS HERE (``deploy=False``): published
        to its mission's branch is the whole of what it does.
        """
        nonlocal candidate_standing
        if not deploy:
            return _published_to_the_mission_branch(
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
            )
        if scripts_from_the_commit and first_profile is None and store is not None:
            try:
                await _recover_profile_selection(store.read(build_id))
            except ValueError as exc:
                return _published_deployment_pending(
                    j_commit=j_commit, target_branch=target_branch,
                    g_commit=g_commit, remote_now=remote_now,
                    checks_passed=checks_passed, checks_total=checks_total,
                    what_was_checked=what_was_checked, attempt=attempt,
                    turn=turn, store=store, why_not_deployed=str(exc),
                )
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
        known = await _the_target_and_how_it_wants_the_identity()
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
        # THE PROJECT HAS TO BE ASKABLE (23 September 2026). Everything below
        # turns on knowing what is ACTUALLY running on the target at the moment
        # of deciding, and only the project can say. A project that declares no
        # read-only "what are you running" step cannot be asked, so its target's
        # state cannot be established — and an unestablished target is never
        # deployed over. Said before the lock, like the rule above it.
        if not getattr(declaration, "can_be_asked", False):
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
                    f"{repo}'s deploy profile does not say how to ask it what "
                    "it is running right now — a read-only step, named in the "
                    "identity block in deploy/profile.yaml as asked_with and "
                    "running_as — so what is on the target could not be "
                    "established and nothing was deployed over it"
                ),
            )
        identity = fixed_identity(j_commit=j_commit, content=j_tree)
        # THE ARTIFACT THAT WAS CHECKED, as the check itself reported it. It is
        # read off the record rather than worked out here, because "here" is
        # after the check and a name worked out after the check is a name
        # another build can have taken. No artifact ⇒ nothing to deploy BY.
        artifact = str(what_was_checked.get("artifact") or "").strip()
        if not artifact:
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
                    f"the check of {j_commit[:10]} did not report, under "
                    f"{repo}'s own {declaration.checked_as}, which artifact it "
                    "checked — so there is no recorded thing for the deploy to "
                    "run, and it will not work one out from a name that "
                    "another build can have taken since. Nothing was deployed"
                ),
            )

        # THE STEPS THAT ASK AND DEPLOY ARE THE ONES THAT SHIP WITH J (3
        # October 2026). For a repository with a sandbox, the question and the
        # promote both run from J's own tree — the one the check ran in, still
        # laid out, or laid out again on a pick-up — never from the clone's
        # working copy. A tree that cannot be laid out is a deploy not made.
        scripts_cwd, no_scripts = await _the_scripts_at(j_commit)
        if no_scripts:
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
                    f"{repo}'s own deploy steps run from a tree of the commit "
                    f"being deployed, and {no_scripts}; so {target} was not "
                    "asked what it is running and nothing was deployed"
                ),
            )

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
            # 3. ASK THE TARGET WHAT IT IS RUNNING, under the lock, BEFORE any
            # of this is decided (23 September 2026). Steps 3 and 4 used to
            # read the LEDGER'S row and nothing else, and a run that deploys
            # and stops before writing its row leaves that row wrong: a later
            # pick-up read it, believed it was moving forwards, and put an
            # OLDER result over a newer one while answering "merged into the
            # remote and running". The ledger is a record of what this press
            # did; only the project can say what is there now.
            observed = await _what_the_target_says(
                target=target, declaration=declaration, scripts_cwd=scripts_cwd
            )
            if observed.word == "no-answer":
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
                        f"{target} could not be asked what it is running "
                        f"({observed.why}), so what is there was not "
                        "established and nothing was deployed over it"
                    ),
                )

            # 3b. THE PICK-UP, BY IDENTITY — off the TARGET'S own answer now,
            # not off the ledger. If the thing running there is the very
            # identity made for this joined commit, this deploy happened and
            # its "done" line was lost. Nothing is done twice.
            if observed.identity == identity.text:
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

            # 3c. PLACE WHAT THE TARGET SAID. The only-forwards rule compares
            # COMMITS and the project answers in identities, so the answer has
            # to be mapped back to the commit it was recorded for — off the
            # target's own row when that row still agrees, and otherwise out of
            # the publication records, which hold identity → joined commit for
            # every build. An answer that places nowhere is a target this press
            # cannot account for, and an unaccounted target is NEVER deployed
            # over: that is the whole of the cure.
            running_commit: str | None = None
            if observed.word == "nothing":
                running_commit = None
            else:
                running_commit = _which_commit_is(
                    observed.identity, grant=grant, target=target
                )
                if running_commit is None:
                    logger.warning(
                        "merge-executor: %s says it is running %s, which no "
                        "record here was checked as — nothing was deployed",
                        target,
                        observed.identity,
                    )
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
                            f"{target} says it is running {observed.identity}, "
                            "and no record here says which result that is. An "
                            "unresolved target is never deployed over, so "
                            "nothing was deployed and somebody has to look"
                        ),
                    )

            # 4. ONLY FORWARDS, against what the TARGET says is running.
            forwards = await what_to_do_about_j(
                git,
                j_commit=j_commit,
                running_commit=running_commit,
                running_identity=observed.identity,
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

            # 5. HAND THE IDENTITY AND THE ARTIFACT OVER, with the ownership
            # beside them. The artifact is the thing the CHECK said it checked;
            # the deploy step must run exactly it and nothing it resolves for
            # itself. (``something_is_running`` used to ride here too and was
            # taken off on 23 September 2026: the executor consulted it to let a
            # note-less first deploy through, and a delayed request carrying a
            # stale copy of it was accepted over a newer build. A request cannot
            # establish its own freshness, so it no longer carries a claim about
            # the target at all.)
            ownership = {
                "target": target,
                "target_counter": grant.counter,
                "build": build_id,
                "identity": identity.text,
                "identity_setting": declaration.setting,
                "artifact": artifact,
                "artifact_setting": declaration.artifact_setting,
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
                    "artifact": artifact,
                    "declared": declaration.to_wire(),
                    "what_the_target_said": observed.to_wire(),
                    "what_is_running_now": forwards.to_wire(),
                },
            ):
                return _replaced_here()

            try:
                where = {"candidate_cwd": scripts_cwd} if scripts_cwd else {}
                deployed = await _dispatch("promote", deploy_ownership=ownership, **where)
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
            # THE PROMOTE TOOK THE CANDIDATE DOWN ITSELF (15 October 2026,
            # FEAT-D267): then there is nothing left for the press's clean-up
            # to tear down. Asking again under the same run is turned away by
            # the stage, reads as a teardown that did not complete, and kept
            # the hold after a merge that went live.
            if detail.get("candidate") in ("torn-down", "absent"):
                candidate_standing = False
            said = str(detail.get("deploy_output") or "")
            reported = identity_reported_by(said, marker=declaration.marker)
            # A COMMAND A TAKEOVER STOPPED IS NOT A FAILED DEPLOY. It is NO
            # deploy: a later holder of the target stopped this command
            # part-way, is deploying its own newer result, and this build's
            # writes are about to be refused anyway. Calling it "the deploy
            # failed" would put a red ending on a build whose work is joined,
            # checked and published, and would say the target is in an unknown
            # state when in fact somebody else owns it. It stops at
            # "published, deployment pending" with the reason said.
            nothing_started = next((w for w in NOTHING_WAS_STARTED if w in said), None)
            if nothing_started is not None:
                logger.warning(
                    "merge-executor: %s's deploy on %s did not start or was stopped "
                    "(%s) — nothing was deployed by this press, and it says so "
                    "rather than calling it a failure",
                    build_id,
                    target,
                    nothing_started,
                )
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
                        f"this press's deploy command on {target} was stopped "
                        "part-way by a later holder of the target, so it did "
                        "not run to an end and nothing was deployed by it"
                        if nothing_started == STOPPED_BY_A_TAKEOVER
                        else (
                            f"this press deployed nothing on {target}: the "
                            f"executor refused to start a command "
                            f"('{nothing_started}'), so the target is as it was"
                        )
                    ),
                )
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

    def _nothing_was_published(
        refused: MergeDeployOutcome, *, store: Any, turn: int
    ) -> MergeDeployOutcome:
        """The press stopped after taking the hold and before anything was
        sent: the record says so.

        Used for the refusals at the candidate check and, since FEAT-7F19
        (15 October 2026: a merge that stopped on a conflict left its record's
        result empty, and the release idle check then waited on it), for a
        join that could not start and a join the merge command refused.

        THE RECORD GETS ITS RESULT (15 October 2026). A press that took the
        lease and then stopped at the candidate check used to return without
        writing any result, so its record said neither "published" nor "not
        published" — only that somebody had held it — and anything that reads
        an empty result as work still in progress waited on it for good. The
        joined commit never left this machine, so the result is the same one
        the other "checked and not sent" endings write: publication pending.

        Only the result is written. The hold is left exactly as the end of
        the press decides it (a candidate check that did not complete keeps
        it, and it lapses on its own). A write refused because the record
        has moved on is a replaced worker, and stops like every other one.
        """
        if store is not None and turn and not store.record(
            build_id=build_id,
            turn=turn,
            now=deps.clock(),
            result=RESULT_PUBLICATION_PENDING,
        ):
            return _replaced_here()
        return refused

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
        nonlocal a_leg_did_not_complete
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
        pending = MergeDeployOutcome(
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
        # A publisher that refused in its own words sent nothing. One that
        # could not be reached may have sent anyway, so the hold is kept.
        if answer.get("refusal_kind") in (
            "the-publisher-could-not-be-reached",
            "the-attempts-ran-out",
        ):
            a_leg_did_not_complete = True
        return pending

    def _not_wholly_checked(
        *,
        j_commit: str,
        target_branch: str,
        g_commit: str,
        checks_passed: int | None,
        checks_total: int | None,
        why: str = "",
    ) -> MergeDeployOutcome:
        """One of the two kinds of check has not run on this J, so nothing is sent.

        ``why`` is what the build system said when it was asked to check this
        already-joined commit — and today that is "I do not have that
        sub-command", named exactly, because naming it is the whole of what
        somebody has to build for this ending to stop happening.
        """
        logger.warning(
            "merge-executor: %s's joined result %s has had only the factory's "
            "own live check run on it — the build system's checks after a "
            "join were not re-run, so nothing is sent (%s)",
            feature_id,
            j_commit[:10],
            why or "no reason was given",
        )
        return MergeDeployOutcome(
            result=RESULT_WORD_PUBLICATION_PENDING,
            status="GATED",
            merged_sha=j_commit,
            detail=(
                f"{named} was joined onto {target_branch} at {g_commit[:10]} "
                f"in a working folder of its own; the factory's own live check "
                f"ran on the joined result {j_commit[:10]}"
                f"{_the_checks_sentence()}, but "
                + (why or the_gated_sentence(feature_id, j_commit))
                + ". It is NOT yet checked, so nothing was sent to the remote "
                "and nothing was deployed. The branch and the join are kept."
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
        nonlocal tree_path, tree_commit, a_leg_did_not_complete
        if candidate_standing:
            await _tear_down_candidate()
        if tree_path is not None:
            if await git.remove_candidate_tree(feature_id, str(tree_path)) is None:
                a_leg_did_not_complete = True
            tree_path = None
            tree_commit = None
        where = await target_branch_now(
            git,
            recorded_branch=recorded_branch,
            mission_branch=_the_missions_branch(),
        )
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
        nonlocal tree_path, candidate_standing, gate_began, held_turn
        nonlocal a_leg_did_not_complete, deploy_run_id

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
                deployed_in=_deployed_in() if gate.get("ran") else None,
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
        where = await target_branch_now(
            git,
            recorded_branch=recorded_branch,
            mission_branch=_the_missions_branch(),
        )
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
                starts_a_press=True,
                refuse_if_an_ending_is_owed=True,
                attempt_id=merge_execution_attempt_id,
            )
            if grant is None:
                current = store.read(build_id)
                if getattr(store, "last_refusal", None) == "an-ending-is-owed":
                    turned = _cannot_join(_AN_ENDING_IS_OWED.format(named=named))
                else:
                    turned = _cannot_join(
                        f"another worker ({current.lease_holder or 'unnamed'}) is "
                        f"already working on {named}'s publication record, so this "
                        "one left it alone. Nothing was merged, and that press's "
                        "ending is the build's."
                    )
                turned.turned_away = True
                return turned
            turn = grant.turn
            held_turn = grant.turn
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
                        integration_branch(join_as, attempt),
                    )
                    if store is not None and not store.done(
                        build_id=build_id,
                        turn=turn,
                        now=deps.clock(),
                        step=STEP_JOIN,
                        attempt=attempt,
                        result={
                            "set_aside": True,
                            "branch_kept": integration_branch(join_as, attempt),
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
            # WHAT THE BUILD SYSTEM SAID WHEN IT WAS ASKED TO CHECK A JOIN IT
            # DID NOT JUST MAKE (23 September 2026). ``None`` = it was never
            # asked, which is every press that made its own join. Anything else
            # is the answer to `<the build system> check-join <FEAT> --joined
            # <J>`, including the ordinary one today: that sub-command does not
            # exist on the installed build system, so the build stays GATED and
            # the sentence names it.
            check_join_said: CheckJoinAnswer | None = None

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
                if check_join_said is not None and check_join_said.ran:
                    # The build system was asked about the join this press
                    # picked up, and it answered. A green answer is the
                    # missing kind of check, run on exactly this J.
                    return check_join_said.passed
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
                    join_name=join_as,
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
                            "branch_kept": integration_branch(join_as, attempt),
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
                folder = working_folder_path(repo_root, join_as, attempt)
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
                        join_name=join_as,
                    ),
                ):
                    return _replaced_here()

                made = await make_the_working_folder(
                    git,
                    repo_root=repo_root,
                    feature_id=feature_id,
                    attempt=attempt,
                    at_commit=g_commit,
                    join_name=join_as,
                )
                # CONFIRMED OR NOT (15 October 2026): a folder whose making was
                # not confirmed — made or refused, in a complete answer — may
                # still be being made: keep the hold.
                if not made.confirmed:
                    a_leg_did_not_complete = True
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
                    # SETTLED (15 October 2026): no leg was sent. A folder the
                    # sandbox answered it could not make is not running work;
                    # one with no answer keeps the hold through the flag above.
                    return _nothing_was_published(
                        _cannot_join(
                                f"{named} could not be joined onto {target_branch}: "
                                f"{made.refusal}. Nothing was merged and the "
                                "branch is kept."
                            ),
                        store=store,
                        turn=turn,
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
                    # the whole command: four check-or-install runs plus the merge.
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

                merge_run_kwargs: dict[str, Any] = dict(
                    subcommand="autobuild",
                    args=args,
                    repo_path=repo_root,
                    read_allowlist=[repo_root],
                    timeout_seconds=merge_wall,
                    with_nats_streaming=False,
                    # WHOSE WORK THIS COMMAND IS, AND WHERE ITS DECLARATIONS
                    # WERE SAID (23 September 2026). When this command goes to
                    # the helper inside the repository's sandbox, the helper
                    # reads that project's own declaration files at a commit
                    # before it launches anything. It used to be told this was
                    # somebody running a command BY HAND, which it is not, and
                    # so it read them at the committed HEAD of the copy it had
                    # — the one way left for a build to widen its own door by
                    # committing a line and then pressing merge. The press
                    # knows its build, and the commit this coordinator's own
                    # ledger records that build as starting from is read here,
                    # off the record, never guessed. The in-container runner
                    # takes both and ignores them: it reads nothing at a
                    # commit.
                    build=build_id,
                    start_commit=_the_recorded_start_commit(),
                    # WHAT ELSE THIS BUILD'S PROJECT DECLARED IT NEEDS (13
                    # October 2026). The join runs the project's own install
                    # and checks; the names recorded for this build travel
                    # with it, and the helper checks each against the
                    # project's declaration at the commit above.
                    launch_settings=_the_recorded_launch_settings(),
                )
                if deps.feature_routing_required:
                    merge_run_kwargs["feature_routing_id"] = correlation_id
                    merge_run_kwargs["feature_routing_required"] = True
                    merge_run_kwargs["feature_routing_receipt"] = (
                        feature_routing_receipt
                    )
                # THE HOLD OUTLASTS THE COMMAND (13 October 2026). The merge
                # command may now install and check twice, so its wall can be
                # longer than the hold this press took when it started. The
                # hold is pushed out to cover the whole wall and a margin
                # before the command is sent, so no second press can take the
                # build over while the join runs, and put back to the
                # ordinary length once it has answered. A renewal that changes
                # no row means this press has already been replaced.
                if store is not None and not store.renew_lease(
                    build_id=build_id,
                    turn=turn,
                    now=deps.clock(),
                    seconds=max(
                        default_lease_seconds(),
                        merge_wall + MERGE_LEASE_MARGIN_SECONDS,
                    ),
                ):
                    return _replaced_here()
                result = await deps.guardkit_run(**merge_run_kwargs)
                if store is not None:
                    store.renew_lease(
                        build_id=build_id, turn=turn, now=deps.clock()
                    )
                # TIMEOUT FIRST (15 October 2026): a merge command stopped at
                # its time limit, or one whose answer was lost, may still be
                # running whatever report survived, so the hold is kept —
                # decided before any report is read.
                if (
                    getattr(result, "status", None) == "timeout"
                    or getattr(result, "answer_lost", False) is True
                    # CONFIRMED OR NOT (15 October 2026): only a complete
                    # answer from the merge command lets the hold go down.
                    or getattr(result, "confirmed", False) is not True
                ):
                    a_leg_did_not_complete = True
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
                    join_name=join_as,
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
                    joined_line: dict[str, Any] = {"joined": False, "refusal": sentence}
                    # A CONFLICT IN A MISSION LANDING SAYS SO (missions design,
                    # section 6): the mission step files the one rebuild on
                    # exactly this, so it is a flag, never a sentence to parse.
                    if not deploy and report is not None and _conflict_sentence(report):
                        joined_line["conflict"] = True
                    if store is not None:
                        store.done(
                            build_id=build_id,
                            turn=turn,
                            now=deps.clock(),
                            step=STEP_JOIN,
                            attempt=attempt,
                            result=joined_line,
                        )
                    refused = MergeDeployOutcome(
                        result="merge-refused",
                        status="FAILED",
                        failed_step="merge",
                        detail=sentence,
                    )
                    # No report from the merge command and no refusal before
                    # it ran — killed, or an answer that could not be read —
                    # is an ending whose outcome nobody knows, and the hold is
                    # kept. A refusal before anything ran is an answer.
                    # Either way nothing was sent, and the record says so.
                    if report is None and not getattr(
                        result, "refused_before_running", False
                    ):
                        a_leg_did_not_complete = True
                    return _nothing_was_published(
                        refused,
                        store=store,
                        turn=turn,
                    )

                # THE JOIN WAS MADE BUT ITS CHECKS NEVER FINISHED. The command
                # was killed, or died before it could say anything: the joined
                # commit is there, and the build system's own checks on it are
                # not. That is not a pass, and it is not a refusal either. J is
                # kept under its own name, the record says the checks did not
                # finish, and nothing is published.
                if joined_but_unchecked:
                    # Killed or died before it could say anything: what it
                    # started may still be running, so the hold is kept.
                    a_leg_did_not_complete = True
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
                        # Shown names for a person, and each name exactly
                        # (review R5), so a resumed press decides as this
                        # one did.
                        **names_for_record(
                            "judged_with_main_copies",
                            (report or {}).get("judged_with_main_copies") or [],
                        ),
                        **names_for_record(
                            "judge_files_not_on_target",
                            (report or {}).get("judge_files_not_on_target") or [],
                        ),
                    },
                ):
                    return _replaced_here()
                merge_checks_ran_here = True
                _remember_main_copies(
                    (report or {}).get("judged_with_main_copies"),
                    (report or {}).get("judge_files_not_on_target"),
                )

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
            if not merge_checks_ran_here:
                # A join an earlier press made: what it recorded about main's
                # copies is said again, exactly as it was.
                earlier = _the_recorded_line(STEP_MERGE_CHECKS)
                if earlier is not None:
                    said = getattr(earlier, "detail", None)
                    _remember_main_copies(
                        names_from_record(said, "judged_with_main_copies"),
                        names_from_record(said, "judge_files_not_on_target"),
                    )
            recorded_why = _why_the_recorded_step_failed(STEP_MERGE_CHECKS)
            if recorded_why is not None and not merge_checks_ran_here:
                recorded_line = _the_recorded_line(STEP_MERGE_CHECKS)
                # "COULD NOT RUN" STAYS "COULD NOT RUN" (13 October 2026, the
                # merge-install design, item 9). The record keeps the status
                # the first press was given. When that was "unverified" — an
                # install that failed, a judge file the build changed, a
                # declaration nobody could read — the checks never ran, so this
                # is the first press's wording, not a red result, and no repair.
                recorded_could_not_run = (
                    recorded_line is not None
                    and str(recorded_line.detail.get("verify_status") or "")
                    .strip()
                    .lower()
                    == "unverified"
                )
                if recorded_could_not_run:
                    detail = (
                        f"{feature_id} was joined onto {target_branch} "
                        f"({(j_commit or '')[:10]}), but the checks after the "
                        f"join could not run: {recorded_why}. Nothing was "
                        "published."
                    )
                else:
                    detail = (
                        f"{feature_id} was joined onto {target_branch} "
                        f"({(j_commit or '')[:10]}), but the checks after the join "
                        f"did not pass: {recorded_why}. Nothing was published."
                    )
                logger.warning(
                    "merge-executor: %s's join (%s) was checked by an earlier "
                    "press and %s (%s) — this press re-runs nothing and says "
                    "so; it is not 'checked'",
                    feature_id,
                    str(j_commit)[:10],
                    "its checks could not run" if recorded_could_not_run else "went red",
                    recorded_why,
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
                    verify_status=(
                        "unverified" if recorded_could_not_run else "failed"
                    ),
                )

            # ------------------------------------------------------------------
            # A JOIN THIS PRESS DID NOT MAKE: ASK THE BUILD SYSTEM TO CHECK IT.
            #
            # The build system's own checks after a join live inside its merge
            # command, and this press does not run that command again on a join
            # somebody else made — so those checks have never run on this J.
            # There is exactly one thing that would run them without merging
            # again: a sub-command of the build system's own, `check-join`. If
            # the installed build system has it, its answer is used and this
            # ending closes by itself. If it has not — which is the case today,
            # and the case inside the frozen image the factory runs it from —
            # NOTHING IS INVENTED: the build stays GATED and the sentence names
            # the sub-command, so whoever adds it knows what to add and nothing
            # here has to change again.
            # ------------------------------------------------------------------
            if (
                j_commit is not None
                and not merge_checks_ran_here
                and not _the_recorded_step_passed(STEP_MERGE_CHECKS)
                and _why_the_recorded_step_failed(STEP_MERGE_CHECKS) is None
            ):
                check_join_kwargs: dict[str, Any] = dict(
                    repo_root=repo_root,
                    feature_id=feature_id,
                    j_commit=str(j_commit),
                    # ONE CHECK RUN'S WALL, not the merge command's: this
                    # command merges nothing, so the runs on both sides of a
                    # merge that the merge wall allows for are not its to
                    # spend.
                    timeout_seconds=float(_verify_timeout_from(deps.config)),
                )
                if deps.feature_routing_required:
                    check_join_kwargs["feature_routing_id"] = correlation_id
                    check_join_kwargs["feature_routing_required"] = True
                    check_join_kwargs["feature_routing_receipt"] = (
                        feature_routing_receipt
                    )
                check_join_said = await ask_the_build_system_to_check_the_join(
                    deps.guardkit_run, **check_join_kwargs
                )
                # A check-join whose answer was lost, that raised after it may
                # have been sent, or that timed out may still be running: keep
                # the hold. One that answered (its checks could not run, or a
                # refusal before running) has finished.
                if check_join_said.answer_lost or not check_join_said.confirmed:
                    a_leg_did_not_complete = True
                if check_join_said.ran:
                    checks_passed = check_join_said.checks_passed
                    checks_total = check_join_said.checks_total
                    # The build system may say its checks on the join could
                    # not run (13 October 2026, item 9). That is "could not
                    # run", not a red result, and files no repair; the record
                    # says so in the same words, so a later press reads it
                    # back exactly as this one says it.
                    joined_could_not_run = not check_join_said.passed and (
                        str(check_join_said.verify_status or "").strip().lower()
                        == "unverified"
                    )
                    why_not = str(
                        (check_join_said.report or {}).get("verify_detail")
                        or check_join_said.sentence
                    )
                    if store is not None and not store.done(
                        build_id=build_id,
                        turn=turn,
                        now=deps.clock(),
                        step=STEP_MERGE_CHECKS,
                        attempt=attempt,
                        result={
                            "ran_on": j_commit,
                            "verify_ran": not joined_could_not_run,
                            "verify_ok": check_join_said.passed,
                            "verify_status": check_join_said.verify_status,
                            "verify_detail": (
                                why_not
                                if joined_could_not_run
                                else check_join_said.sentence
                            ),
                            "checks_passed": check_join_said.checks_passed,
                            "checks_total": check_join_said.checks_total,
                            "asked_the_build_system_about_a_reused_join": True,
                        },
                    ):
                        return _replaced_here()
                    if not check_join_said.passed:
                        if store is not None:
                            store.record(
                                build_id=build_id,
                                turn=turn,
                                now=deps.clock(),
                                result=RESULT_PUBLICATION_PENDING,
                            )
                        if joined_could_not_run:
                            said = (
                                "the checks after the join could not run: "
                                f"{why_not}"
                            )
                        else:
                            said = check_join_said.sentence
                        return MergeDeployOutcome(
                            result="merged-verify-failed",
                            status="FAILED",
                            merged_sha=j_commit,
                            failed_step="verify",
                            detail=(
                                f"{feature_id} was joined onto {target_branch} "
                                f"({str(j_commit)[:10]}) by an earlier press, and "
                                f"{said}. Nothing was published."
                            ),
                            checks_passed=check_join_said.checks_passed,
                            checks_total=check_join_said.checks_total,
                            verify_status=(
                                "unverified" if joined_could_not_run else "failed"
                            ),
                        )
                    logger.info(
                        "merge-executor: the build system checked %s's reused "
                        "join (%s) and it passed, so both kinds of check have "
                        "now run on it",
                        feature_id,
                        str(j_commit)[:10],
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

            # THE TREE THAT WAS PREVIEWED IS THE ONLY TREE THIS APPROVAL MAY
            # MERGE (web-readiness W7, R3). The offer previewed the candidate
            # joined onto the target branch as fetched then; if the branch has
            # moved since, the join made now is a different tree that nobody
            # tried. It is refused like a candidate that moved: the build
            # system's checks inside the join have already run on it, but the
            # live check, publication and deploy never happen.
            if expected_preview_tree and j_tree != expected_preview_tree:
                logger.warning(
                    "merge-executor: refusing %s — main moved since the "
                    "preview (previewed tree %s, joined tree %s)",
                    build_id,
                    expected_preview_tree,
                    j_tree,
                )
                # SETTLED (15 October 2026, FEAT-A9DA): this refusal comes
                # after a join that answered and before any leg is sent, so
                # nothing this press started can still be running. Unsettled,
                # it kept the hold and the release idle check waited on it.
                # A leg whose answer was lost earlier still keeps the hold,
                # through ``a_leg_did_not_complete``.
                return _nothing_was_published(
                    _could_not_check(
                            "main moved since the preview: previewed tree "
                            f"{expected_preview_tree}, joined tree {j_tree}"
                        ),
                    store=store,
                    turn=turn,
                )

            # Record the archive path only after the venue confirmed its layout.
            laid = await _lay_the_tree_out(str(j_commit))
            if laid is not None:
                return _nothing_was_published(laid, store=store, turn=turn)
            try:
                await _select_profile_for_join(
                    target_commit=g_commit, candidate_commit=candidate_sha,
                    candidate_tree=offered_tree, joined_commit=str(j_commit),
                    joined_tree=str(j_tree), attempt=attempt, store=store,
                )
            except ValueError as exc:
                return _nothing_was_published(
                    _could_not_check(str(exc)), store=store, turn=turn,
                )
            # A changed-target retry executes a new candidate check. Its
            # runbooks need a fresh group, after cleanup of the preceding one.
            deploy_run_id = str(uuid.uuid4())
            if store is not None and not store.about_to(
                build_id=build_id, turn=turn, now=deps.clock(),
                step=STEP_CANDIDATE_CHECK, attempt=attempt,
                inputs={
                    "j_commit": j_commit, "j_tree": j_tree,
                    "feature_id": feature_id,
                    "deploy_run_id": deploy_run_id,
                    **({"candidate_cwd": str(tree_path)} if scripts_from_the_commit else {}),
                    **({"first_deployment_profile": first_profile.to_wire()} if first_profile is not None else {}),
                },
            ):
                return _replaced_here()

            checked_outcome = await _run_the_candidate_check(str(j_commit))
            if checked_outcome is not None:
                return _nothing_was_published(
                    checked_outcome, store=store, turn=turn
                )

            what_was_checked = {
                "j_commit": j_commit,
                "j_tree": j_tree,
                "deploy_run_id": deploy_run_id,
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
                # THE IDENTITY OF WHAT WAS CHECKED, as the check itself
                # reported it (section C). The artifact inside it is the
                # project's own unrepeatable name for the thing that was
                # checked, captured at the check; the deploy is handed exactly
                # that and never works one out again later.
                "identity": _the_identity_the_deploy_uses_today(),
                # THE SAME ARTIFACT, at the top level, because this is where
                # the deploy leg reads it from — on this run and on a pick-up
                # that reads this record back off the ledger.
                "artifact": gate.get("checked_artifact"),
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
            # says so, and the activation check's three conditions all hold
            # (the design's section G). With either missing, nothing is sent
            # anywhere, nothing is deployed, and the record stops at "checked"
            # with the reason said in plain words.
            # ------------------------------------------------------------------
            # THE PUBLISHER'S ANSWER, ASKED NOW, ONCE FOR THIS MERGE WORD
            # (in a thread: it is a network call). The switch and the reason
            # below are asked of the same answer.
            machine_now = await asyncio.to_thread(
                the_machine_now, deps.what_the_machine_says
            )
            if not publication_is_switched_on(deps.config, machine_now):
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
                # one of the activation check's three conditions that does not hold.
                why_off = why_publication_is_off(deps.config, machine_now)
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
                        f"{str(j_commit)[:10]}{checks}, but "
                        + (
                            check_join_said.sentence
                            if check_join_said is not None
                            else the_gated_sentence(feature_id, str(j_commit))
                        )
                        + ". It is NOT yet checked and not ready to publish. "
                        "Nothing was sent to the remote and nothing was "
                        "deployed. The branch and the join are kept."
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
                switched_off = MergeDeployOutcome(
                    result=RESULT_WORD_PUBLICATION_PENDING,
                    # A pass only when BOTH kinds of check ran and passed on J.
                    status="PASSED" if both_kinds_ran else "GATED",
                    merged_sha=j_commit,
                    detail=detail,
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                    deployed_in=_deployed_in() if gate.get("ran") else None,
                    gate_before_merge=_gate_for_report(),
                )
                # Nothing was sent, and the press has finished.
                return switched_off

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
                    why=(
                        check_join_said.sentence
                        if check_join_said is not None
                        else ""
                    ),
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
        and what was checked is running. The work is
        :func:`retire_join_working_folders`, shared with the recovery of a
        settled record whose hold was never put down.
        """
        nonlocal a_leg_did_not_complete
        retired, an_answer_was_lost = await retire_join_working_folders(
            git, record, repo_root, join_as
        )
        if an_answer_was_lost:
            a_leg_did_not_complete = True
        return retired

    if sandbox_not_ready:
        outcome = MergeDeployOutcome(
            result="merge-refused",
            status="FAILED",
            failed_step="sandbox",
            detail=(
                f"{named} was not merged: its project's sandbox did not start "
                f"({sandbox_not_ready}). Nothing was joined, checked, published or "
                "deployed, and the branch is kept."
            ),
        )
    else:
        try:
            outcome = await _press()
            # THE ENDING IS WRITTEN DOWN FIRST (press lease recovery, 18 October
            # 2026), before the clean-up, in its report's words, on this press's
            # own turn: a coordinator that stops from here on is reported for
            # from it, word for word (:meth:`MergeApprovalConsumer.finish_a_settled_press`).
            # A copy, so the ending itself is put into words exactly once.
            try:
                _write_the_ending_down(
                    _the_reports_words(_with_main_copies_said(copy.copy(outcome))),
                    cleanup_done=False,
                )
            except Exception as exc:  # noqa: BLE001 — never costs a result
                logger.warning(
                    "merge-executor: %s's ending could not be put into words "
                    "before the clean-up (%s: %s)",
                    build_id,
                    type(exc).__name__,
                    exc,
                )
        finally:
            # Every ending, the normal ones and a crash alike: the candidate down
            # if it is still standing, and its laid-out tree removed — BEFORE the
            # report goes out, so the report never says "torn down" ahead of time.
            await _cleanup()
    if turned_away_by and not outcome.turned_away:
        outcome.turned_away = True
        outcome.detail = (
            f"{outcome.detail} "
            + (
                _AN_ENDING_IS_OWED.format(named=named)
                if turned_away_by[0] == "an-ending-is-owed"
                else "Another press is working on this build's record, so "
                "nothing was written down and its ending is that press's."
            )
        ).strip()
    if outcome.turned_away:
        return _turned_away(outcome)
    # THE RECORD'S END, and the only place anything the press made is removed.
    # It is reachable now: the joined commit is on the remote's recorded
    # branch and what was checked is running, confirmed from the identity the
    # running thing reported. The JOIN'S working folders go here — every
    # attempt's, the branches left exactly as they are — and the build's own
    # retained folder is retired below on the same word.
    if not dry_run and outcome.result in (
        RESULT_WORD_MERGED_AND_RUNNING,
        RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH,
    ):
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
                    if outcome.result == RESULT_WORD_MERGED_AND_RUNNING
                    else "the build's record has reached its end: the joined "
                    "commit is on its mission's branch, and a feature inside "
                    "a mission deploys nothing of its own"
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
                # Decided here, without touching anything: an answer.
                "confirmed": True,
            }
        else:
            retired = await git.retire_autobuild_worktree(
                build_id,
                str(worktree_retention.get("path") or ""),
                worktree_retention,
            )
        _write_receipt("autobuild_worktree_cleanup.json", retired)
        # CONFIRMED OR NOT, whatever the status says (15 October 2026).
        if retired.get("confirmed") is not True:
            a_leg_did_not_complete = True
        if retired.get("status") != "removed":
            # A retained worktree that was ANSWERED as not retired is a folder
            # left behind, not work still running (15 October 2026, FEAT-6AC9:
            # a read-only cache folder stopped Git's removal, the press kept
            # its hold, and the release idle check then waited on a build that
            # had finished). The receipt and the warning say it was kept.
            # Nothing retries it on its own: retirement runs once, after a
            # successful merge, or when someone calls the sidecar's retire
            # route by hand. Only a retirement whose answer was not
            # confirmed may still be running, so only that keeps the hold
            # (decided just above).
            logger.warning(
                "merge-executor: retained autobuild worktree for %s was kept: %s",
                build_id,
                retired.get("detail"),
            )
    outcome = _the_reports_words(_with_main_copies_said(outcome))
    # Written down again now its words are final (what the clean-up left
    # standing is known), then reported, and only then is the hold put down:
    # every stop from here on is finished by the recovery from this ending.
    _write_the_ending_down(outcome, cleanup_done=True)
    # EVERY BOOKKEEPING STEP, AND THE HOLD LAST, through the one finishing
    # routine the recovery runs too: each step asks whether it is already done
    # for this press's turn, so a stop anywhere from here on is finished by the
    # recovery, step for step, and nothing is done twice.
    return await _finish_this_press(outcome)


# ---------------------------------------------------------------------------
# The in-daemon deploy dispatcher (mirrors cli/_deploy_run.py exactly)
# ---------------------------------------------------------------------------


def _the_builds_declarations(
    db_path: Any, build_id: str
) -> tuple[str | None, tuple[str, ...], str | None]:
    """This build's memory name, its declared setting NAMES, and where they were said.

    Read straight off the ledger, on a connection of its own that is closed
    again, because the deploy dispatch is handed a path rather than a facade.
    Nothing here guesses: a ledger that recorded none of them answers
    ``(None, (), None)``, which is the factory's own launch list, memory
    explicitly off and no commit named — the honest state, and never somebody
    else's project name.

    The third answer is the recorded commit the work STARTS from (25 September
    2026). The helper that launches the project's own commands cannot see the
    ledger, so it is sent this and reads the project's declarations there —
    rather than off the working copy, where a build could have written a line
    of its own.
    """
    if db_path is None:
        return None, (), None
    try:
        from forge.adapters.sqlite.connect import connect_writer
        from forge.lifecycle.persistence import SqliteLifecyclePersistence

        connection = connect_writer(db_path)
        try:
            facade = SqliteLifecyclePersistence(connection=connection, db_path=db_path)
            name = facade.read_memory_project(build_id)
            names = tuple(str(entry) for entry in facade.read_launch_settings(build_id))
            start = facade.read_start_point(build_id)
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
        return None, (), None
    started_at = str(getattr(start, "start_commit", None) or "").strip() or None
    return (str(name or "").strip() or None), names, started_at


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
        identity_env: dict[str, str] | None = None,
        ask_env: dict[str, str] | None = None,
        issued_env: Any = None,
        first_deployment_profile: dict[str, Any] | None = None,
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
        from forge.deploy.profile import DeployProfileError, load_deploy_profile
        from forge.deploy.sidecar_git import SidecarCandidateGit
        from forge.persistence.migrations import runbook as runbook_migration
        from forge.persistence.repositories.runbook import RunbookRepository

        if db_path is None:
            raise RuntimeError(
                "merge-deploy: no forge DB path was threaded into the deploy "
                "dispatcher — the deploy stage cannot persist its runbooks"
            )
        repo_root = Path(repo_root)
        # SANDBOX FIRST (2026-09-07, rule 85). A repository that has a sandbox
        # is deployed and gated INSIDE it: the stage's scripts go to the deploy
        # sidecar in there, and the live gate's driver goes with them, because
        # the candidate's port is on that sandbox's own loopback and a driver
        # run in the forge container could reach nothing. A repository with no
        # sandbox — every repository until an operator fills the map in — takes
        # exactly the path it took before this lane.
        sandbox = sandbox_for(config, repo)
        try:
            profile = (
                load_deploy_profile(repo_root / "deploy" / "profile.yaml")
                if sandbox is None else None
            )
        except DeployProfileError as exc:
            raise ProfileSelectionRefusedBeforeDispatch(
                f"no deployment request was sent: {exc}"
            ) from exc
        # WHAT THE PROJECT DECLARED FOR THIS BUILD, read once. The live check
        # needs it, and since 23 September 2026 so does the deploy step: its
        # environment is built from the factory's named list plus these, never
        # copied from whatever this process holds.
        build_memory, build_declared, build_started_at = _the_builds_declarations(
            db_path, build_id
        )
        if first_deployment_profile is not None:
            # Only ledger-confirmed publication inputs may select a new source.
            # Everything here precedes construction or dispatch of any stage.
            import hashlib
            from forge.record_answer.service import TheCoordinatorsRecord

            try:
                selected, _turn = parse_profile_reference(first_deployment_profile)
                if (sandbox is None or dry_run or leg not in (
                        "candidate_check", "promote", "what_is_running", "candidate_down")
                        or selected.repo != repo or selected.build != build_id
                        or selected.start_commit != build_started_at
                        or selected.candidate_cwd != candidate_cwd):
                    raise ValueError("the first profile is outside its approved deployment work")
                answer = TheCoordinatorsRecord(db_path).what_build_starts_from(build_id)
                if answer.get("first_deployment_profile") != first_deployment_profile:
                    raise ValueError("the current record does not confirm the first deployment profile")
                file = await SidecarCandidateGit(str(sandbox.sidecar_url), repo=str(repo)).read_file_at_commit(
                    selected.joined_commit, DEPLOY_PROFILE_PATH, raw=True,
                )
                if (file.refusal or not file.found or file.mode not in ("100644", "100755")
                        or file.content is None
                        or hashlib.sha256(file.content.encode("utf-8")).hexdigest() != selected.profile_sha256):
                    raise ValueError("the selected committed deployment profile differs from the approved file")
                profile, why = _the_profile_out_of(file, selected.joined_commit)
                if profile is None:
                    raise ValueError(why)
            except (ValueError, RuntimeError) as exc:
                raise ProfileSelectionRefusedBeforeDispatch(
                    f"no deployment request was sent: {exc}"
                ) from exc
        if profile is None:
            # THE PROFILE IS READ IN THE SANDBOX TOO (3 October 2026). Its
            # clone is the only copy of this repository; the path this
            # coordinator was given for it holds nothing, and reading there
            # stopped FEAT-E592's merge at the candidate check with "deploy
            # profile not found". It is read the way the press reads it — the
            # committed file at the commit this build is recorded as starting
            # from, which is also where the helper reads the project's own
            # declarations — through the sandbox's sidecar.
            profile, why_not = await _the_profile_where_it_lives(
                SidecarCandidateGit(str(sandbox.sidecar_url), repo=str(repo)),
                repo_root,
                build_started_at,
            )
            if profile is None:
                raise ProfileSelectionRefusedBeforeDispatch(
                    "no deployment request was sent: "
                    f"{repo}'s {DEPLOY_PROFILE_PATH} could not be read in the "
                    f"sandbox {getattr(sandbox, 'name', '')}: {why_not}"
                )
        spec = profile.live_gate
        invoker = None
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
                # The same stamp the deploy steps carry: this build, and the
                # commit this coordinator's ledger records it as starting
                # from. The gate goes through the same environment door.
                build=build_id,
                start_commit=build_started_at,
                **({"first_deployment_profile": first_deployment_profile} if first_deployment_profile is not None else {}),
            )
        elif spec is not None:
            invoker = RepoDriverLiveGateInvoker(
                repo_path=repo_root,
                driver_argv=list(spec.driver),
                timeout_seconds=spec.timeout_seconds,
                extra_env=dict(spec.env),
                # The driver's environment is built from nothing now (13
                # October 2026), so the names the project declared are handed
                # over by name, as they are to the sandbox's helper.
                launch_settings=build_declared,
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
        # WHERE THE DEPLOY RECORDS ARE WRITTEN. For a repository with a
        # sandbox, the path this coordinator was given for it holds nothing
        # (the only copy is the sandbox's own clone), and writing records
        # under it made a pretend project folder in the coordinator's state
        # (3 October 2026). Those records go into a folder of the
        # coordinator's own instead, beside its ledger, named for the
        # repository. Every other repository's records go where they always
        # have.
        if sandbox is not None:
            deploy_record_root = Path(db_path).parent / "deploy-records" / Path(str(repo)).name
        else:
            deploy_record_root = repo_root / deploy_cfg.deploy_record_dir
        return await dispatch_deploy_stage(
            deploy_cfg,
            profile,
            correlation_id=correlation_id,
            deploy_run_id=deploy_run_id or str(uuid.uuid4()),
            repository=repository,
            runbook_publisher=runbook_publisher,
            deploy_publisher=deploy_publisher,
            live_gate_invoker=invoker,
            deploy_record_root=str(deploy_record_root),
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
            # WHICH BUILD THIS IS, AND WHERE ITS DECLARATIONS WERE SAID: the
            # recorded commit this build starts from, both read off THIS
            # coordinator's own ledger just above. They are stamped onto every
            # request THE DEPLOY STAGE sends the helper for this build, in one
            # place, so the helper reads the project's own two files THERE
            # rather than off the working copy it runs out of — and can ask
            # this coordinator whether that commit really is the one recorded
            # for this build before it reads a line of them. The merge word's
            # own command and the fix journey's legs go through a different
            # door and carry neither; see the note beside these two parameters
            # in forge.deploy.composition.
            build_id=build_id,
            declared_at=build_started_at,
            # WHAT THE CHECK IS HANDED so it can pin what it checked, and the
            # question the project declared it wants "what are you running"
            # asked with. Both are names the project chose; this carries them.
            identity_env=identity_env,
            ask_env=ask_env,
            # THE VALUES THE REVIEW PREVIEW ISSUED (13 October 2026), carried
            # apart from ``identity_env`` all the way to the helper, which
            # checks them again before a program is given them.
            issued_env=issued_env,
            **({"first_deployment_profile": first_deployment_profile} if first_deployment_profile is not None else {}),
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


def why_a_previewed_offer_is_refused(
    service: Any, offer: dict[str, Any], *, now: datetime
) -> tuple[str | None, bool]:
    """Why an offer with a review preview may not be approved now (web-readiness W7).

    ``(None, False)`` when it may. Otherwise ``(reason, definitive)``:
    ``definitive`` is True when the preview itself answers no (not ready,
    expired, its lease ended, its evidence gone, no previewed tree on the
    offer), and False when the check could not be made at all (no service
    here, or a check that raised). It fails closed either way; only a
    definitive refusal should end the preview. Shared by the card's press and
    ``forge merge-deploy``.
    """
    if not str(offer.get("preview_tree") or "").strip():
        return "the offer does not say which tree was previewed", True
    if service is None:
        return (
            "the preview service is not running here, so the preview "
            "cannot be confirmed"
        ), False
    try:
        refusal = service.approval_refusal(offer.get("review_evidence"), now=now)
    except Exception as exc:  # noqa: BLE001 — a refusal, never a pass
        return f"the preview could not be checked ({type(exc).__name__})", False
    return refusal, refusal is not None


def _decision_entry(
    *,
    build_id: str,
    decision: str,
    decided_by: str,
    request_id: str,
    now: datetime,
    attempt_id: str | None,
) -> StageLogEntry:
    """The durable decision row — the restart / duplicate fence."""
    return StageLogEntry(
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
                "decided_by": decided_by,
                "request_id": request_id,
                **(
                    {
                        MERGE_EXECUTION_ATTEMPT_VERSION_KEY: (
                            MERGE_EXECUTION_ATTEMPT_VERSION
                        ),
                        MERGE_EXECUTION_ATTEMPT_ID_KEY: attempt_id,
                    }
                    if attempt_id is not None
                    else {}
                ),
            }
        },
    )


def _a_landing_has_reported(pool: Any, build_id: str) -> bool:
    """Is a merge report on this build's record?"""
    return any(
        s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
        for s in pool.read_stages(build_id)
    )


def _ledger_of(pool: Any) -> Any | None:
    """The ledger's writer connection, or None for a pool without one."""
    import sqlite3

    cx = getattr(pool, "connection", None)
    return cx if isinstance(cx, sqlite3.Connection) else None


async def release_the_offers_own_preview(
    service: Any, offer: dict[str, Any], *, reason: str
) -> None:
    """After a definitive refusal, release ONLY the preview this offer names.

    Matched by run id (16 October 2026), so an Approve pressed during a
    rebuild — refused, because the committed offer still names the old,
    ended preview — can never tear the rebuild's new preview down. A service
    without ``release_run`` releases nothing. Never raises.
    """
    release_run = getattr(service, "release_run", None)
    evidence = offer.get("review_evidence")
    run_id = str(evidence.get("run_id") or "") if isinstance(evidence, dict) else ""
    if not callable(release_run) or not run_id:
        return
    try:
        await release_run(run_id=run_id, reason=reason)
    except Exception as exc:  # noqa: BLE001 — never costs the refusal
        logger.warning(
            "merge-executor: the refused offer's preview could not be released "
            "(%s: %s)",
            type(exc).__name__,
            exc,
        )


class MergeApprovalConsumer:
    """Consumes merge-card presses and runs the executor on a genuine approve.

    Refusals are quiet on the wire but loud in the log — a forged or stale
    response never runs anything, and the honest reason is one grep away.
    """

    def __init__(self, deps: MergeExecutorDeps) -> None:
        self._deps = deps
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        # Mission feature builds this consumer is landing right now, so a
        # resumption asked for while the first landing still waits for the
        # repository's lock is not queued behind it a second time.
        self._landing_now: set[str] = set()
        # Builds whose approved press this consumer has started and not yet
        # finished — from the moment the decision row is committed — so a
        # resume never presses one a second time.
        self._pressing_now: set[str] = set()

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

    def install_mission_teller(self, tell: Callable[..., Awaitable[bool]] | None) -> None:
        """How the finishing routine says a line in a mission's thread
        (``MergeExecutorDeps.tell_mission``), installed at boot."""
        self._deps.tell_mission = tell

    @contextlib.asynccontextmanager
    async def _pressing(
        self, repo: str, purpose: str, *, needs_deployment: bool = False
    ) -> AsyncIterator[None]:
        """The repository's press lock, with its sandbox started first.

        One sandbox per project, stage S2 (design 2.3a): a press reaches the
        project's own sandbox, which may have been stopped while idle, so it
        takes the sandbox's gate before the lock and waits for start-up rather
        than failing. A sandbox that does not become ready raises
        :class:`~forge.sandbox_gate.gate.SandboxNotReady` before the body runs
        (Codex S2 implementation review 1, R3). With no sandbox gate installed
        this is exactly the lock.
        """
        from forge.sandbox_gate.gate import using

        async with using(repo, purpose=purpose, needs_deployment=needs_deployment):
            async with self._lock_for(repo):
                yield

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
        additional = getattr(self._deps.config.approval, "additional_approvers", ())
        if expected is not None and not is_accepted_approver(
            payload.decided_by, expected, additional
        ):
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
        # THE REBUILD PRESS (16 October 2026): ``defer`` with the note
        # ``rebuild-preview``, on an offer from this release that has a
        # preview, after every check above. It writes no decision row and
        # publishes no new approval request; the rebuild runs as its own task
        # and keeps the candidate the offer pinned. Any other ``defer`` is
        # refused as before.
        if (
            decision == "defer"
            and (payload.notes or "").strip() == REBUILD_NOTE
            and self._deps.rebuild_preview is not None
            and describable(offer)
        ):
            logger.info(
                "merge-executor: %s asked to rebuild the preview for %s",
                payload.decided_by,
                build_id,
            )
            task = asyncio.create_task(self._deps.rebuild_preview(build_id=build_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        if decision not in ("approve", "reject"):
            logger.warning(
                "merge-executor: refusing %s — decision %r is not one of the "
                "offer's resume options (approve/reject)",
                request_id,
                decision,
            )
            return

        # A CARD FROM THIS RELEASE WITH A PREVIEW (16 October 2026): its
        # preview check and its decision row are written in ONE ledger write
        # transaction, against the latest committed offer, so a rebuild's
        # amended row and another card's slot-taking are each wholly before
        # or wholly after it. Every other offer is pressed exactly as before.
        recorded: tuple[dict[str, Any], datetime, str | None] | None = None
        if describable(offer) and _ledger_of(self._deps.pool) is not None:
            recorded = await self._decide_previewed(
                payload=payload,
                build_id=build_id,
                request_id=request_id,
                decision=decision,
            )
            if recorded is None:
                return
            offer = recorded[0]
        # A PREVIEWED OFFER IS APPROVED ONLY WHILE ITS PREVIEW IS USABLE
        # (web-readiness W7, 12 October 2026). Asked before the decision row,
        # so a refused approval is written down as a refusal and never as a
        # decision, and the card can still be rejected. Offers without
        # ``review_evidence`` never reach this.
        elif decision == "approve" and "review_evidence" in offer:
            refusal, definitive = why_a_previewed_offer_is_refused(
                self._deps.review_preview, offer, now=self._deps.clock()
            )
            if refusal is not None:
                # Known limitation (recorded 12 October 2026): Jarvis has
                # already shown the press as recorded, as it does today for a
                # refused moved branch, and nothing here tells the card
                # otherwise. From Slack the way on is Reject, or running the
                # feature again.
                logger.warning(
                    "merge-executor: refusing %s — %s; nothing was merged and "
                    "no decision was recorded. The card in Slack may still show "
                    "the approval as recorded; from Slack the way on is Reject, "
                    "or running the feature again",
                    request_id,
                    refusal,
                )
                _write_the_refusal_down_for(
                    _publication_store_for(self._deps, build_id),
                    deps=self._deps,
                    build_id=build_id,
                    holder=f"merge-press:{os.getpid()}:{uuid.uuid4().hex[:8]}",
                    feature_id=str(offer.get("feature_id") or ""),
                    repo=str(offer.get("repo") or ""),
                    decided_by=payload.decided_by,
                    target_branch=None,
                    refusal=f"the approval was refused: {refusal}",
                )
                # Only a definitive refusal ends the preview. One that came
                # from a check that could not be made (a service that is not
                # here, a check that raised) leaves it standing, so the same
                # card can be approved once the check can be made again.
                # And only the preview this offer names (16 October 2026).
                if definitive:
                    await release_the_offers_own_preview(
                        self._deps.review_preview,
                        offer,
                        reason=f"approval refused: {refusal}",
                    )
                return

        # Durable decision row FIRST — the restart / duplicate fence.  An
        # approved decision gets a fresh unguessable executor-attribution
        # token in the same committed row, before its async task exists.
        # (A previewed card from this release has written its row already,
        # in the same transaction as its preview check.)
        if recorded is not None:
            _offer, now, merge_execution_attempt_id = recorded
        else:
            now = self._deps.clock()
            merge_execution_attempt_id = (
                uuid.uuid4().hex if decision == "approve" else None
            )
            self._deps.pool.record_stage(
                _decision_entry(
                    build_id=build_id,
                    decision=decision,
                    decided_by=payload.decided_by,
                    request_id=request_id,
                    now=now,
                    attempt_id=merge_execution_attempt_id,
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
            await self._release_the_preview(offer, build_id=build_id, reason="rejected")
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
        self._pressing_now.add(build_id)
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
                merge_execution_attempt_id=str(merge_execution_attempt_id),
                **({"previewed_offer": offer} if "review_evidence" in offer else {}),
                **(
                    {"expected_preview_tree": str(offer["preview_tree"])}
                    if offer.get("preview_tree")
                    else {}
                ),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _describe(self, build_id: str) -> None:
        describe = self._deps.describe_card
        if describe is None:
            return
        try:
            await describe(build_id)
        except Exception as exc:  # noqa: BLE001 — the sweep describes it again
            logger.warning(
                "merge-executor: %s's card could not be described (%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )

    async def _decide_previewed(
        self,
        *,
        payload: Any,
        build_id: str,
        request_id: str,
        decision: str,
    ) -> tuple[dict[str, Any], datetime, str | None] | None:
        """A previewed card's press: check and decision in ONE transaction.

        Returns ``(the latest offer, when, the attempt id)`` once the decision
        row is committed, or None when nothing was recorded (a refusal, a
        decision already there, an offer that changed). The card is described
        either way.
        """
        now = self._deps.clock()
        attempt_id = uuid.uuid4().hex if decision == "approve" else None

        def check(latest: dict[str, Any]) -> tuple[str | None, bool]:
            return why_a_previewed_offer_is_refused(
                self._deps.review_preview, latest, now=now
            )

        attempt = decide_previewed_offer(
            _ledger_of(self._deps.pool),
            build_id=build_id,
            request_id=request_id,
            decision=decision,
            now=now,
            decision_entry=lambda _latest: _decision_entry(
                build_id=build_id,
                decision=decision,
                decided_by=payload.decided_by,
                request_id=request_id,
                now=now,
                attempt_id=attempt_id,
            ),
            approval_check=check,
        )
        if attempt.outcome == "recorded" and attempt.offer is not None:
            await self._describe(build_id)
            return attempt.offer, now, attempt_id
        if attempt.outcome == "refused" and attempt.offer is not None:
            refusal = str(attempt.refusal)
            logger.warning(
                "merge-executor: refusing %s — %s; nothing was merged and no "
                "decision was recorded. The card gets its buttons back.",
                request_id,
                refusal,
            )
            _write_the_refusal_down_for(
                _publication_store_for(self._deps, build_id),
                deps=self._deps,
                build_id=build_id,
                holder=f"merge-press:{os.getpid()}:{uuid.uuid4().hex[:8]}",
                feature_id=str(attempt.offer.get("feature_id") or ""),
                repo=str(attempt.offer.get("repo") or ""),
                decided_by=payload.decided_by,
                target_branch=None,
                refusal=f"the approval was refused: {refusal}",
            )
            if attempt.definitive:
                await release_the_offers_own_preview(
                    self._deps.review_preview,
                    attempt.offer,
                    reason=f"approval refused: {refusal}",
                )
            await self._describe(build_id)
            return None
        logger.warning(
            "merge-executor: refusing %s — %s",
            request_id,
            {
                "already-decided": "a decision is already on record",
                "no-offer": "no durable merge offer is on record",
                "changed": "the latest offer carries a different request_id",
            }.get(attempt.outcome, attempt.outcome),
        )
        await self._describe(build_id)
        return None

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
        merge_execution_attempt_id: str,
        previewed_offer: dict[str, Any] | None = None,
        expected_preview_tree: str | None = None,
    ) -> None:
        # Per-repo single-flight: an asyncio lock per repo key PLUS the
        # executor's own durable step probes.
        # A MISSION'S MERGE INTO MAIN joins under ``mission-<id>`` (missions
        # design, round 2 R1), so it never meets the last feature's own
        # integration names; every other press joins under its feature id.
        from forge.lifecycle.mission_builds import mission_join_name, mission_of_build

        belongs = mission_of_build(self._deps.pool, build_id)
        join_name = (
            mission_join_name(belongs.mission_id)
            if belongs is not None and belongs.is_merge
            else None
        )
        self._pressing_now.add(build_id)
        try:
            await self._run_approved_locked(
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
                merge_execution_attempt_id=merge_execution_attempt_id,
                previewed_offer=previewed_offer,
                expected_preview_tree=expected_preview_tree,
                join_name=join_name,
            )
        finally:
            self._pressing_now.discard(build_id)

    async def _run_approved_locked(
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
        merge_branch: str | None,
        expected_candidate_sha: str | None,
        expected_candidate_tree: str | None,
        expected_candidate_branch: str | None,
        worktree_retention: dict[str, Any] | None,
        merge_execution_attempt_id: str,
        previewed_offer: dict[str, Any] | None,
        expected_preview_tree: str | None,
        join_name: str | None,
    ) -> None:
        press = dict(
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
            merge_execution_attempt_id=merge_execution_attempt_id,
            previewed_offer=previewed_offer,
            expected_preview_tree=expected_preview_tree,
            join_name=join_name,
        )
        try:
            async with self._pressing(
                repo, f"the merge press for {build_id}", needs_deployment=True
            ):
                await self._press_approved(**press)
            return
        except SandboxNotReady as exc:
            not_ready = str(exc)
        # THE SANDBOX DID NOT START (stage S2, Codex S2 implementation review
        # 1, R3): the press reports it, under the repository's lock, and
        # touches nothing in the sandbox.
        async with self._lock_for(repo):
            await self._press_approved(**press, sandbox_not_ready=not_ready)

    async def _press_approved(
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
        merge_branch: str | None,
        expected_candidate_sha: str | None,
        expected_candidate_tree: str | None,
        expected_candidate_branch: str | None,
        worktree_retention: dict[str, Any] | None,
        merge_execution_attempt_id: str,
        previewed_offer: dict[str, Any] | None,
        expected_preview_tree: str | None,
        join_name: str | None,
        sandbox_not_ready: str | None = None,
    ) -> None:
        ended = "approval failed: the merge press raised"
        try:
            outcome = await execute_merge_deploy(
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
                merge_execution_attempt_id=merge_execution_attempt_id,
                **(
                    {"expected_preview_tree": expected_preview_tree}
                    if expected_preview_tree
                    else {}
                ),
                **({"join_name": join_name} if join_name else {}),
                **(
                    {"sandbox_not_ready": sandbox_not_ready}
                    if sandbox_not_ready
                    else {}
                ),
            )
            ended = f"approval ended: {getattr(outcome, 'result', None)}"
            if getattr(outcome, "turned_away", False):
                # Another press holds the record: its ending, and the
                # preview's release, are that press's (18 October 2026).
                logger.warning(
                    "merge-executor: the approved press of %s was turned "
                    "away by a live hold; nothing was done and the preview "
                    "is left to the press that holds the record",
                    build_id,
                )
                return
        except Exception as exc:  # noqa: BLE001 — the task must not die silent
            logger.error(
                "merge-executor: executor raised (%s) for build %s — see "
                "the receipts under merge-%s",
                exc,
                build_id,
                build_id,
            )
        # A cancelled press (a restart) does not get here: its preview is
        # left to its own expiry and the start-up reconcile. A press whose
        # sandbox did not start leaves it to the sweep too: releasing it
        # would reach into the sandbox.
        if previewed_offer is not None and not sandbox_not_ready:
            await self._release_the_preview(
                previewed_offer, build_id=build_id, reason=ended
            )

    async def resume_mission_merge(self, build_id: str) -> bool:
        """Carry on an approved mission merge into main that never reported.

        Missions design, section 7, and implementation review round 2, R6:
        the decision row is committed before the press runs, and the
        duplicate guard then refuses any later answer, so a coordinator that
        stopped in between would leave the mission MERGING for ever. The
        finishing step calls this for a mission-merge build with an approved
        decision, no merge report and no live publication lease.

        It presses through the same locked entry as an approved merge
        (:meth:`_run_approved`), with the ORIGINAL decision's approver and
        execution attempt id and the durable offer's pinned candidate (the
        frozen ``forge/source/<build_id>`` at the mission's tip, which the
        caller puts back first), deploy on, and ``join_name mission-<id>``.
        No new card is asked for. The press's own pick-up reconciles a merge
        that was already published: it reads the remote first, finds the
        joined commit there, and never sends it again.

        Bounded durably: one ``mission_merge_resume`` row per resumed press,
        written before it runs. At ``publication.send_attempts`` resumed
        presses with no report, a FAILED merge report
        (``mission-merge-unfinished``) is written instead, carrying the
        decision's attempt id, so the mission settles.

        Returns True when a press was run or the bound was reached; False
        when nothing was done (not a mission merge, already reported, already
        being pressed here, no approved decision or no offer on record).
        """
        from forge.lifecycle.mission_builds import mission_of_build

        belongs = mission_of_build(self._deps.pool, build_id)
        if belongs is None or not belongs.is_merge:
            return False
        if build_id in self._pressing_now:
            return False
        stages = self._deps.pool.read_stages(build_id)
        if any(s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER for s in stages):
            return False
        if _an_ending_is_owed(self._deps.pool, build_id):
            return False  # its finishing routine reports it; no resume is spent
        decisions = [
            s for s in stages if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
        ]
        offers = [s for s in stages if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER]
        if not decisions or decisions[-1].status != "PASSED" or not offers:
            return False
        said = (decisions[-1].details or {}).get(MERGE_DECISION_DETAILS_KEY) or {}
        attempt_id = str(said.get(MERGE_EXECUTION_ATTEMPT_ID_KEY) or "")
        decided_by = str(said.get("decided_by") or "")
        offer = offers[-1].details.get(MERGE_OFFER_DETAILS_KEY) or {}
        row = self._deps.pool.get_build_row(build_id)
        repo = str(offer.get("repo") or (row.repo if row is not None else ""))
        repo_root_raw = self._deps.config.planning.target_repo_paths.get(repo)
        allowed = _how_many_attempts(self._deps.config)
        resumed = sum(
            1 for s in stages if s.target_identifier == MISSION_MERGE_RESUME_TARGET_IDENTIFIER
        )
        if resumed >= allowed or not attempt_id or not repo_root_raw or row is None:
            why = (
                f"the approved merge of mission {belongs.mission_id} was "
                f"carried on {resumed} times after the factory stopped and "
                "never reported"
                if resumed >= allowed
                else f"the approved merge of mission {belongs.mission_id} "
                "cannot be carried on: its decision, offer or repository is "
                "not readable"
            )
            self._merge_unfinished(build_id, offer, why, attempt_id or None)
            return True
        now = self._deps.clock()
        self._deps.pool.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=MISSION_MERGE_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MISSION_MERGE_RESUME_TARGET_IDENTIFIER,
                status="SKIPPED",
                gate_mode=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={
                    "mission_merge_resume": {"attempt": resumed + 1, "allowed": allowed}
                },
            )
        )
        logger.info(
            "merge-executor: carrying on the approved merge of mission %s (%s), "
            "resume %d of %d",
            belongs.mission_id,
            build_id,
            resumed + 1,
            allowed,
        )
        baseline = offer.get("baseline_failing")
        await self._run_approved(
            build_id=build_id,
            feature_id=str(offer.get("feature_id") or row.feature_id),
            repo=repo,
            repo_root=Path(repo_root_raw),
            expect_main_sha=str(offer.get("expect_main_sha") or ""),
            correlation_id=str(offer.get("correlation_id") or row.correlation_id),
            decided_by=decided_by,
            baseline_failing=(
                [str(x) for x in baseline] if isinstance(baseline, list) else None
            ),
            merge_branch=_merge_branch_of_record(self._deps.pool, build_id, offer),
            expected_candidate_sha=str(offer.get("candidate_sha") or "") or None,
            expected_candidate_tree=str(offer.get("candidate_tree") or "") or None,
            expected_candidate_branch=(
                str(offer.get("branch") or "").strip() or None
                if offer.get("candidate_identity_version") == 1
                else None
            ),
            merge_execution_attempt_id=attempt_id,
            **({"previewed_offer": offer} if "review_evidence" in offer else {}),
            **(
                {"expected_preview_tree": str(offer["preview_tree"])}
                if offer.get("preview_tree")
                else {}
            ),
        )
        return True

    def _merge_unfinished(
        self, build_id: str, offer: dict[str, Any], reason: str, attempt_id: str | None
    ) -> None:
        """A FAILED merge report for a mission merge that could not be carried on."""
        now = self._deps.clock()
        logger.warning("merge-executor: %s", reason)
        self._deps.pool.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=MERGE_REPORT_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
                status="FAILED",
                gate_mode=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={
                    "feature_id": offer.get("feature_id"),
                    "build_id": build_id,
                    "correlation_id": offer.get("correlation_id"),
                    "status": "FAILED",
                    "result": RESULT_WORD_MISSION_MERGE_UNFINISHED,
                    "detail": reason,
                    **(
                        {
                            MERGE_DECISION_DETAILS_KEY: {
                                MERGE_EXECUTION_ATTEMPT_VERSION_KEY: (
                                    MERGE_EXECUTION_ATTEMPT_VERSION
                                ),
                                MERGE_EXECUTION_ATTEMPT_ID_KEY: attempt_id,
                            }
                        }
                        if attempt_id
                        else {}
                    ),
                },
            )
        )

    async def land_mission_feature(
        self, build_id: str, *, tasks_failed: int | None = 0
    ) -> MergeDeployOutcome | None:
        """Merge a finished mission feature into its mission's branch. No card.

        Missions design, section 6. Called when a feature built inside a
        mission finishes, from the place a routine build is offered its merge
        card (:meth:`MergeOfferService.maybe_offer`, which offers no card for
        it), and again by the finishing step for a landing that never
        reported (a restart). It runs the ordinary press:

        * UNDER THE SAME PER-REPOSITORY LOCK as an approved merge, so two
          features finishing together join, check and publish one after the
          other (round 1, R4);
        * onto the build's recorded target, its mission's branch;
        * with ``deploy=False``: after a confirmed publish it stops and
          records "published to the mission branch";
        * with no preview tree pinned.

        The decision row says ``decided_by: "mission <id> (approved by
        <who>)"``, and its execution attempt id comes from the build, so a
        landing picked up after a restart is the same attempt. One decision
        per request still holds: each feature build is its own request.

        A BUILD THAT CANNOT LAND GETS A FAILED LANDING REPORT, so the mission
        can close its attempt and nothing waits for ever: a build that
        finished with failed tasks (``tasks_failed``), or whose failed-task
        count is not known (``tasks_failed=None``: unknown counts as failed),
        a repository this factory does not know, and a build whose press was
        started as many times as a merge word may try a send
        (``publication.send_attempts``, three by default) without reporting.
        Each started press is one durable stage row, written before it runs,
        so the count survives a press that takes the coordinator down.

        Idempotent: a build whose landing has already reported is never
        pressed again (``None``), and one whose decision is recorded but whose
        press never reported is carried on.
        """
        from forge.lifecycle.mission_builds import mission_of_build
        from forge.lifecycle.missions import MissionStore

        belongs = mission_of_build(self._deps.pool, build_id)
        if belongs is None or not belongs.is_feature:
            logger.warning(
                "merge-executor: %s is not a mission feature — not landed on "
                "any mission branch",
                build_id,
            )
            return None
        row = self._deps.pool.get_build_row(build_id)
        if row is None:
            logger.error("merge-executor: no builds row for %s — not landed", build_id)
            return None
        if _a_landing_has_reported(self._deps.pool, build_id):
            logger.info(
                "merge-executor: %s's landing has already reported — not pressed again",
                build_id,
            )
            return None
        if build_id in self._landing_now:
            logger.info(
                "merge-executor: %s is being landed by this coordinator right now — "
                "not twice",
                build_id,
            )
            return None
        if tasks_failed is None:
            return self._landing_failed(
                row,
                f"{row.feature_id} was not merged into its mission's branch: the "
                "factory stopped before it could tell whether every task "
                "passed, and a build it cannot vouch for is not landed",
            )
        if tasks_failed:
            return self._landing_failed(
                row,
                f"{row.feature_id} finished with {tasks_failed} failed task(s), so "
                "it was not merged into its mission's branch",
            )
        mission = MissionStore(self._deps.pool.connection).get(belongs.mission_id)
        if mission is None:
            return self._landing_failed(
                row,
                f"{row.feature_id} names mission {belongs.mission_id}, which is not "
                "on record, so it was not landed",
            )
        repo_root_raw = self._deps.config.planning.target_repo_paths.get(row.repo)
        if not repo_root_raw:
            return self._landing_failed(
                row,
                f"{row.feature_id}'s repository {row.repo} is not one this factory "
                "knows, so it was not landed",
            )
        decided_by = (
            f"mission {mission.id} (approved by {mission.approved_by or 'nobody on record'})"
        )
        request_id = f"{REQUEST_ID_PREFIX}{build_id}"
        if build_id in self._landing_now:
            logger.info(
                "merge-executor: %s is already being landed on %s — not twice",
                build_id,
                mission.branch,
            )
            return None
        # THE CLEAN FINISH IS WRITTEN DOWN BEFORE WAITING FOR THE LOCK: the
        # decision row is now made inside the lock (section 11), and a landing
        # can wait there behind a sibling's press. This row is the durable
        # fact that the build was asked to land with every task passed, so a
        # restart meanwhile carries the landing on rather than failing it.
        self._record_landing_requested(row)
        self._landing_now.add(build_id)
        try:
            async with self._pressing(row.repo, f"the mission landing of {build_id}"):
                if _a_landing_has_reported(self._deps.pool, build_id):
                    return None
                # A PRESS THAT STOPPED AFTER ITS RESULT IS FINISHED, NOT
                # PRESSED AGAIN (press lease recovery): its finishing routine
                # reports it, and no landing attempt is spent on it.
                if _an_ending_is_owed(self._deps.pool, build_id):
                    return None
                # ONE ATOMIC BOUNDARY (section 11, Codex round 2 R1): inside
                # the repository's lock, one transaction re-reads the
                # mission's stop request and creates or reuses the decision
                # row. A stop that commits first means no new decision and no
                # press; a decision that commits first goes ahead.
                attempt_id = self._decide_landing(
                    build_id,
                    mission_id=mission.id,
                    decided_by=decided_by,
                    request_id=request_id,
                )
                if attempt_id is None:
                    self._landing_failed(
                        row,
                        f"{row.feature_id} was not merged into its mission's "
                        f"branch {mission.branch}: mission stopped",
                    )
                    return None
                allowed = _how_many_attempts(self._deps.config)
                started = self._landing_attempts(build_id)
                if started >= allowed:
                    await self._give_up_landing(
                        row,
                        mission,
                        Path(repo_root_raw),
                        attempt_id,
                        f"{row.feature_id} could not be landed on "
                        f"{mission.branch}: its press was started {started} "
                        f"times and never reported",
                    )
                    return None
                started += 1
                self._record_landing_attempt(row, started)
                try:
                    outcome = await execute_merge_deploy(
                        deps=self._deps,
                        build_id=build_id,
                        feature_id=row.feature_id,
                        repo=row.repo,
                        repo_root=Path(repo_root_raw),
                        # Informational only on the merge step's row: the
                        # press joins onto where the mission branch is NOW (G).
                        expect_main_sha=str(row.start_commit or ""),
                        correlation_id=row.correlation_id,
                        decided_by=decided_by,
                        baseline_failing=None,
                        merge_branch=_merge_branch_of_record(self._deps.pool, build_id, {}),
                        merge_execution_attempt_id=attempt_id,
                        deploy=False,
                    )
                except Exception as exc:  # noqa: BLE001 — bounded, then reported
                    logger.error(
                        "merge-executor: landing %s on %s raised (%s), attempt %d "
                        "of %d — see the receipts under merge-%s",
                        build_id,
                        mission.branch,
                        exc,
                        started,
                        allowed,
                        build_id,
                    )
                    if started >= allowed:
                        await self._give_up_landing(
                            row,
                            mission,
                            Path(repo_root_raw),
                            attempt_id,
                            f"{row.feature_id} could not be landed on "
                            f"{mission.branch}: the press stopped with "
                            f"{type(exc).__name__} on all {allowed} attempts "
                            f"({_one_line(str(exc))[:200]})",
                        )
                    return None
                return outcome
        except SandboxNotReady as exc:
            # Nothing was decided or pressed: the landing request stays on
            # record and the finishing step carries it on (stage S2, R3).
            logger.warning(
                "merge-executor: %s's landing waits for its sandbox — %s", build_id, exc
            )
            return None
        finally:
            self._landing_now.discard(build_id)

    def _decide_landing(
        self, build_id: str, *, mission_id: str, decided_by: str, request_id: str
    ) -> str | None:
        """Create or reuse the landing's decision row; ``None`` if the mission stopped first.

        One ``BEGIN IMMEDIATE`` transaction (section 11, round 2 R1): an
        existing decision row is reused with its attempt id, whatever the
        stop says — a landing already decided goes on to its end. With no
        decision row, the mission's ``stop_requested_at`` is read in the same
        transaction, and a stop means no decision is created.
        """
        from forge.lifecycle.card_revisions import immediate_transaction, insert_stage_row
        from forge.lifecycle.mission_builds import landing_attempt_id

        cx = self._deps.pool.connection
        attempt_id = landing_attempt_id(build_id)
        with immediate_transaction(cx):
            decided = cx.execute(
                "SELECT details_json FROM stage_log WHERE build_id = ? "
                "AND target_identifier = ? ORDER BY id DESC LIMIT 1",
                (build_id, MERGE_DECISION_TARGET_IDENTIFIER),
            ).fetchone()
            if decided is not None:
                try:
                    details = json.loads(decided[0] or "{}")
                except ValueError:
                    details = {}
                said = (details.get(MERGE_DECISION_DETAILS_KEY) or {}) if isinstance(details, dict) else {}
                return str(said.get(MERGE_EXECUTION_ATTEMPT_ID_KEY) or attempt_id)
            stop = cx.execute(
                "SELECT stop_requested_at FROM missions WHERE id = ?", (mission_id,)
            ).fetchone()
            if stop is not None and stop[0] is not None:
                return None
            insert_stage_row(
                cx,
                _decision_entry(
                    build_id=build_id,
                    decision="approve",
                    decided_by=decided_by,
                    request_id=request_id,
                    now=self._deps.clock(),
                    attempt_id=attempt_id,
                ),
            )
        return attempt_id

    async def _give_up_landing(
        self,
        row: Any,
        mission: Any,
        repo_root: Path,
        attempt_id: str | None,
        reason: str,
    ) -> None:
        """The landing's bound was reached: reconcile with the remote BEFORE failing.

        Section 11, Codex round 2 R4, for every mission: a send may have
        reached the remote on the last allowed attempt before its answer was
        recorded. So for a build with a publication record the remote is read
        first: if the mission branch contains the joined commit, the record is
        settled as published and the landing reports PASSED; if it does not,
        the record is settled "publication pending" (lease released) and the
        landing reports FAILED; if the remote cannot be read, nothing is
        written and the next pass tries again — an uncertain outcome is never
        turned into FAILED. A build with no record had no send: FAILED.
        """
        settled = await self._settle_by_looking(row, mission, repo_root)
        if settled == "landed":
            self._landing_reconciled(row, attempt_id, mission)
        elif settled in ("not landed", "no record"):
            self._landing_failed(row, reason, attempt_id=attempt_id)
        else:
            logger.warning(
                "merge-executor: %s's landing could not be settled by looking at "
                "the remote — nothing is written; tried again on the next pass",
                row.build_id,
            )

    async def reconcile_mission_landing(self, build_id: str) -> str:
        """Settle a reported landing whose publication record is still unsettled.

        Section 11: recovery re-checks a mission feature whose landing has a
        report but whose record has no result yet (a press that reported and
        never settled, from before this rule). Under the repository's lock;
        answers "landed", "not landed", "unknown", or "nothing to do".
        """
        from forge.lifecycle.mission_builds import mission_of_build
        from forge.lifecycle.missions import MissionStore

        belongs = mission_of_build(self._deps.pool, build_id)
        row = self._deps.pool.get_build_row(build_id)
        if belongs is None or not belongs.is_feature or row is None:
            return "nothing to do"
        mission = MissionStore(self._deps.pool.connection).get(belongs.mission_id)
        repo_root_raw = self._deps.config.planning.target_repo_paths.get(row.repo)
        if mission is None or not repo_root_raw:
            return "nothing to do"
        try:
            async with self._pressing(row.repo, f"settling the landing of {build_id}"):
                return await self._settle_by_looking(row, mission, Path(repo_root_raw))
        except SandboxNotReady as exc:
            logger.warning("merge-executor: settling %s waits — %s", build_id, exc)
            return "unknown"

    async def finish_a_settled_landing(self, build_id: str) -> str:
        """Finish the bookkeeping of a mission feature's landing whose outcome is settled.

        Missions design section 11 (Codex implementation rounds 2 and 3, R2).
        The press settles its result, then cleans up, writes its report and
        puts its lease down, in that order; a coordinator that stops anywhere
        after the result leaves the report missing, the hold kept, or both —
        and a stopped mission then never drains, with the router and rollout
        blocked on it. One rule finishes it, in the press's own order, under
        the repository's lock and the record's lease rules:

        1. **The report**, if missing, is written from the settled outcome:
           PASSED for "published to the mission branch", FAILED with the
           recorded reason for anything else; with the decision's own
           execution-attempt identity; nothing is sent. A report that exists
           is left alone.
        2. **The hold**, if its lease has expired, is taken over on a new
           turn, the press's after-result clean-up is run again (the join's
           working folders, :func:`retire_join_working_folders`), and the
           hold is put down on that turn. A removal whose answer was lost
           keeps the new hold, which lapses and is tried again.

        A result a PRESS wrote (its row's ``result_written``) is not this
        path's: :meth:`finish_a_settled_press` finishes it for that press's
        turn. A live, unexpired lease means a press may still be working:
        nothing at all is done. Answers "finished", "held", "kept" or
        "nothing to do".
        """
        from forge.lifecycle.mission_builds import mission_of_build

        belongs = mission_of_build(self._deps.pool, build_id)
        row = self._deps.pool.get_build_row(build_id)
        if belongs is None or not belongs.is_feature or row is None:
            return "nothing to do"
        repo_root_raw = self._deps.config.planning.target_repo_paths.get(row.repo)
        if not repo_root_raw:
            return "nothing to do"
        store = PublicationRecordStore(self._deps.pool.connection)
        try:
            return await self._finish_a_settled_landing_gated(
                row, store, build_id, repo_root_raw
            )
        except SandboxNotReady as exc:
            logger.warning("merge-executor: finishing %s waits — %s", build_id, exc)
            return "held"

    async def _finish_a_settled_landing_gated(
        self, row: Any, store: Any, build_id: str, repo_root_raw: str
    ) -> str:
        async with self._pressing(row.repo, f"finishing the landing of {build_id}"):
            record = store.read(build_id)
            if not record.recorded or not record.result:
                return "nothing to do"
            # A RESULT A PRESS WROTE IS THE FINISHING ROUTINE'S (Codex round
            # 1): it reports, warns and releases for that press's turn. This
            # path is left with results a reconciliation settled.
            written = store.read_outcome(build_id)
            if written is not None and written.result_written:
                return "nothing to do"
            if record.lease_is_live(self._deps.clock()):
                return "held"
            # 1. THE REPORT, from the settled outcome.
            if not _a_landing_has_reported(self._deps.pool, build_id):
                self._report_the_settled_landing(row, record)
            # 2. THE HOLD.
            if not record.lease_holder:
                return "finished"
            holder = f"settled-landing:{os.getpid()}:{uuid.uuid4().hex[:8]}"
            grant = store.take_lease(build_id=build_id, holder=holder, now=self._deps.clock())
            if grant is None:
                return "held"
            git = git_surface_for(self._deps, row.repo, Path(repo_root_raw))
            retired, lost = await retire_join_working_folders(
                git, store.read(build_id), Path(repo_root_raw), row.feature_id
            )
            logger.info(
                "merge-executor: %s's settled record kept its hold after the "
                "coordinator stopped; the join folders were looked at again (%s)",
                build_id,
                retired,
            )
            if lost:
                return "kept"
            if not store.release_lease(
                build_id=build_id, turn=grant.turn, holder=holder, now=self._deps.clock()
            ):
                return "held"
            return "finished"

    def _report_the_settled_landing(self, row: Any, record: Any) -> None:
        """The landing report the press did not get to write, from its settled record."""
        attempt_id: str | None = None
        for stage in self._deps.pool.read_stages(row.build_id):
            if stage.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER:
                said = (stage.details or {}).get(MERGE_DECISION_DETAILS_KEY) or {}
                attempt_id = said.get(MERGE_EXECUTION_ATTEMPT_ID_KEY) or attempt_id
        if record.result == RESULT_PUBLISHED_TO_MISSION_BRANCH:
            self._landing_reconciled(row, attempt_id, SimpleNamespace(branch=record.target_branch))
            return
        reason = next(
            (
                str((line.detail or {}).get("refusal"))
                for line in reversed(record.lines)
                if line.kind == LINE_DONE and (line.detail or {}).get("refusal")
            ),
            f"the merge into {record.target_branch} was settled as {record.result}",
        )
        self._landing_failed(
            row, f"{row.feature_id} did not land: {reason}", attempt_id=attempt_id
        )

    async def finish_a_settled_press(self, build_id: str) -> str:
        """:meth:`_finish_a_settled_press`, with the project's sandbox started
        first: its clean-up reaches the sandbox (one sandbox per project,
        stage S2). A sandbox that is not ready answers "held", and the work
        queue's tick tries again; nothing is touched meanwhile."""
        try:
            return await self._finish_a_settled_press(build_id)
        except SandboxNotReady as exc:
            logger.warning("merge-executor: finishing %s's press waits — %s", build_id, exc)
            return "held"

    async def _finish_a_settled_press(self, build_id: str) -> str:
        """Finish a press that stopped after its result: the recovery's half
        of the one finishing routine (Codex round 1, 18 October 2026).

        Picked by the work queue's tick for any build whose press row has a
        result written, no ``finished_at`` and no live lease. Under the
        repository's lock:

        1. **Ownership by compare and swap**: the record is taken over only
           while it is still on the turn just read (``expected_turn``), so a
           recovery that read it and lost a race does nothing.
        2. **Re-read after taking it**: the press's row, its mark and the
           record must still be what was read — the same press's turn, its
           result, not finished — or the lease is put down and nothing done.
        3. The clean-up after the result, as the press does it (the join's
           working folders, only once the record has reached its end).
        4. :func:`finish_the_press` for that press's turn, from the press's own
           row (:func:`the_stopped_presss_ending`): each step done only if not
           already done, the hold last, then ``finished_at``.

        Answers what finishing answered, "held", or "nothing to do".
        """
        if build_id in self._pressing_now:
            return "held"
        row = self._deps.pool.get_build_row(build_id)
        if row is None:
            return "nothing to do"
        store = PublicationRecordStore(self._deps.pool.connection)
        record = store.read(build_id)
        repo = str(record.repo or row.repo or "")
        async with self._pressing(repo, f"finishing the press of {build_id}"):
            record = store.read(build_id)
            written = store.read_outcome(build_id)
            if not _a_press_is_owed_its_finishing(record, written):
                return "nothing to do"
            if record.lease_is_live(self._deps.clock()):
                return "held"
            holder = f"settled-press:{os.getpid()}:{uuid.uuid4().hex[:8]}"
            grant = store.take_lease(
                build_id=build_id,
                holder=holder,
                now=self._deps.clock(),
                expected_turn=record.turn,
            )
            if grant is None:
                return "held"
            again = store.read_outcome(build_id)
            if not _a_press_is_owed_its_finishing(store.read(build_id), again) or (
                again.turn != written.turn
            ):
                store.release_lease(
                    build_id=build_id, turn=grant.turn, holder=holder, now=self._deps.clock()
                )
                return "nothing to do"
            from forge.lifecycle.mission_builds import mission_of_build

            belongs = mission_of_build(self._deps.pool, build_id)
            landing = bool(belongs is not None and belongs.is_feature)
            kept = press_kept_on_purpose(record, again)
            ending = the_stopped_presss_ending(self._deps, row, record, again, kept=kept)
            if landing and kept:
                ending.warning = (again.warning or {}).get("sentence") or (
                    landing_left_standing_sentence(
                        row.feature_id,
                        record,
                        kept,
                        handed=the_identity_handed_to_the_check(again),
                    )
                )
                ending.mission_id = belongs.mission_id
            repo_root_raw = self._deps.config.planning.target_repo_paths.get(repo)
            if record.result == RESULT_MERGED_AND_RUNNING and repo_root_raw:
                join_as = str((again.outcome or {}).get("join_as") or row.feature_id)
                git = git_surface_for(self._deps, repo, Path(repo_root_raw))
                retired, lost = await retire_join_working_folders(
                    git, store.read(build_id), Path(repo_root_raw), join_as
                )
                logger.info(
                    "merge-executor: %s's press stopped after its result; the "
                    "join folders were looked at again (%s)",
                    build_id,
                    retired,
                )
                if lost:
                    return "kept"
            return await finish_the_press(
                self._deps,
                ending,
                store=store,
                press_turn=again.turn,
                lease_turn=grant.turn,
                holder=holder,
                hold=the_hold_after_finishing(kept, a_mission_landing=landing, by_recovery=True),
                tell=getattr(self._deps, "tell_mission", None),
            )

    async def _settle_by_looking(self, row: Any, mission: Any, repo_root: Path) -> str:
        """Settle this build's publication record from the remote. Its answer.

        "no record" (nothing was ever sent), "landed" (the record says, or the
        remote shows, the joined commit on the mission branch), "not landed"
        (settled "publication pending", lease released), or "unknown" (held by
        a live press, the remote could not be read, or a write was refused
        because another worker took the record over: nothing claimed). The
        press's own rule: the question is whether the branch CONTAINS the
        joined commit, and only a record that tried a send is read for one.
        """
        store = PublicationRecordStore(self._deps.pool.connection)
        record = store.read(row.build_id)
        if not record.recorded:
            return "no record"
        if record.result:
            return "landed" if record.result == RESULT_PUBLISHED_TO_MISSION_BRANCH else "not landed"
        now = self._deps.clock()
        if record.lease_is_live(now):
            return "unknown"
        holder = f"landing-reconcile:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        grant = store.take_lease(build_id=row.build_id, holder=holder, now=now)
        if grant is None:
            return "unknown"
        turn = grant.turn
        settled = False

        def settle(result: str) -> bool:
            # The result and the release are ONE write (Codex implementation
            # round 1, R1): a crash can never leave a settled record held.
            nonlocal settled
            settled = store.settle(
                build_id=row.build_id,
                turn=turn,
                holder=holder,
                now=self._deps.clock(),
                result=result,
            )
            return settled

        try:
            record = store.read(row.build_id)
            send_lines = [line for line in record.lines if line.step == STEP_SEND]
            # Every write is on this worker's turn; one that is refused means
            # another worker took the record over, so nothing is claimed here.
            if not send_lines or not record.j_commit:
                return "not landed" if settle(RESULT_PUBLICATION_PENDING) else "unknown"
            git = git_surface_for(self._deps, row.repo, repo_root)
            where = await target_branch_now(
                git,
                recorded_branch=record.target_branch or mission.branch,
                mission_branch=mission.branch,
            )
            if not where.ok:
                return "unknown"
            contains = await git.is_ancestor(str(record.j_commit), str(where.commit))
            if contains is None:
                return "unknown"
            if contains:
                published = any(
                    line.kind == LINE_DONE and line.detail.get("published") is True
                    for line in send_lines
                )
                if not published and not store.done(
                    build_id=row.build_id,
                    turn=turn,
                    now=self._deps.clock(),
                    step=STEP_SEND,
                    attempt=int(send_lines[-1].attempt or 0),
                    result={
                        "ran_on": str(record.j_commit),
                        "published": True,
                        "contains_j": True,
                        "remote_now": str(where.commit),
                        "found_by_looking": True,
                        "detail": (
                            "the send's answer was never recorded; the remote "
                            "was read and its branch contains the joined commit"
                        ),
                    },
                ):
                    return "unknown"
                return "landed" if settle(RESULT_PUBLISHED_TO_MISSION_BRANCH) else "unknown"
            return "not landed" if settle(RESULT_PUBLICATION_PENDING) else "unknown"
        finally:
            if not settled:
                # Nothing was settled: the lease this worker took is put down,
                # so the next pass may look again.
                store.release_lease(
                    build_id=row.build_id, turn=turn, holder=holder, now=self._deps.clock()
                )

    def _landing_reconciled(self, row: Any, attempt_id: str | None, mission: Any) -> None:
        """A PASSED landing report for a landing found on the remote by looking."""
        if _a_landing_has_reported(self._deps.pool, row.build_id):
            return
        now = self._deps.clock()
        self._deps.pool.record_stage(
            StageLogEntry(
                build_id=row.build_id,
                stage_label=MERGE_REPORT_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
                status="PASSED",
                gate_mode=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={
                    "feature_id": row.feature_id,
                    "build_id": row.build_id,
                    "correlation_id": row.correlation_id,
                    "status": "PASSED",
                    "result": RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH,
                    "detail": (
                        f"{row.feature_id}'s joined commit is on "
                        f"{mission.branch}, so it landed"
                    ),
                    **(
                        {
                            MERGE_DECISION_DETAILS_KEY: {
                                MERGE_EXECUTION_ATTEMPT_VERSION_KEY: (
                                    MERGE_EXECUTION_ATTEMPT_VERSION
                                ),
                                MERGE_EXECUTION_ATTEMPT_ID_KEY: attempt_id,
                            }
                        }
                        if attempt_id is not None
                        else {}
                    ),
                },
            )
        )

    def _record_landing_requested(self, row: Any) -> None:
        """One durable row saying the landing was asked for after a clean finish."""
        if any(
            s.target_identifier
            in (MISSION_LANDING_REQUEST_TARGET_IDENTIFIER, MERGE_DECISION_TARGET_IDENTIFIER)
            for s in self._deps.pool.read_stages(row.build_id)
        ):
            return
        now = self._deps.clock()
        self._deps.pool.record_stage(
            StageLogEntry(
                build_id=row.build_id,
                stage_label=MISSION_LANDING_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MISSION_LANDING_REQUEST_TARGET_IDENTIFIER,
                status="SKIPPED",
                gate_mode=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={"mission_landing_requested": {"tasks_failed": 0}},
            )
        )

    def _landing_attempts(self, build_id: str) -> int:
        """How many landing presses were started for this build, durably."""
        return sum(
            1
            for s in self._deps.pool.read_stages(build_id)
            if s.target_identifier == MISSION_LANDING_ATTEMPT_TARGET_IDENTIFIER
        )

    def _record_landing_attempt(self, row: Any, number: int) -> None:
        """One durable row per landing press started, before it runs."""
        now = self._deps.clock()
        self._deps.pool.record_stage(
            StageLogEntry(
                build_id=row.build_id,
                stage_label=MISSION_LANDING_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MISSION_LANDING_ATTEMPT_TARGET_IDENTIFIER,
                status="SKIPPED",
                gate_mode=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={
                    "mission_landing_attempt": {
                        "attempt": number,
                        "allowed": _how_many_attempts(self._deps.config),
                    }
                },
            )
        )

    def _landing_failed(
        self, row: Any, reason: str, *, attempt_id: str | None = None
    ) -> None:
        """Write a FAILED landing report for a mission feature that cannot land.

        A merge report row like the press's own, with the result word
        ``mission-landing-failed`` and the reason, so the mission step closes
        the attempt and the finishing step never presses the build again.
        When a decision row was written, the report carries its attempt id, so
        the router's "approved and pending" blocker clears.
        """
        stages = self._deps.pool.read_stages(row.build_id)
        if any(s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER for s in stages):
            return None
        if attempt_id is None:
            for stage in stages:
                if stage.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER:
                    said = (stage.details or {}).get(MERGE_DECISION_DETAILS_KEY) or {}
                    attempt_id = said.get(MERGE_EXECUTION_ATTEMPT_ID_KEY) or attempt_id
        now = self._deps.clock()
        logger.warning("merge-executor: %s", reason)
        self._deps.pool.record_stage(
            StageLogEntry(
                build_id=row.build_id,
                stage_label=MERGE_REPORT_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
                status="FAILED",
                gate_mode=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={
                    "feature_id": row.feature_id,
                    "build_id": row.build_id,
                    "correlation_id": row.correlation_id,
                    "status": "FAILED",
                    "result": RESULT_WORD_MISSION_LANDING_FAILED,
                    "detail": reason,
                    **(
                        {
                            MERGE_DECISION_DETAILS_KEY: {
                                MERGE_EXECUTION_ATTEMPT_VERSION_KEY: (
                                    MERGE_EXECUTION_ATTEMPT_VERSION
                                ),
                                MERGE_EXECUTION_ATTEMPT_ID_KEY: attempt_id,
                            }
                        }
                        if attempt_id is not None
                        else {}
                    ),
                },
            )
        )
        return None

    async def _release_the_preview(
        self, offer: dict[str, Any], *, build_id: str, reason: str
    ) -> None:
        """Tear this offer's preview down once its decision has ended.

        Only for an offer that carried ``review_evidence``. Best effort: a
        release that fails is logged and never costs the decision.
        """
        service = self._deps.review_preview
        if service is None or "review_evidence" not in offer:
            return
        try:
            await service.release(
                feature_id=str(offer.get("feature_id") or ""),
                build_id=build_id,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — never costs the decision
            logger.warning(
                "merge-executor: the preview for %s could not be released "
                "(%s: %s)",
                build_id,
                type(exc).__name__,
                exc,
            )
