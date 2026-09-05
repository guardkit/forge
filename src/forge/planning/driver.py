"""Mode P planning chain driver (TASK-MP-012).

The production driver that walks one planning run through the chain

    QUEUED → RUNNING → PRODUCT_OWNER dispatch → product_docs checkpoint
    (PAUSED) → approve/reject/defer/escalate/timeout → planned handoff
    (PLANNED_HANDOFF)

by consulting the pure planner (:func:`forge.planning.planner.plan_next_step`)
over history **translated from durable planning_run_events rows** — this is
what makes the driver re-entrant: after a crash, :meth:`PlanningRunDriver.drive`
resumes at exactly the step the durable history implies (post-merge review
finding: ``ExecuteHandoff`` was unreachable from durable history because
nothing wrote planner-shaped events).

Design notes
------------

* **Domain module, injected collaborators** — no NATS / SQLite / git types
  are imported here; ``cli/_serve_planning.py`` is the composition root.
* **Structured wait, not a poller** — escalation phase and remaining time
  are recomputed from the durable ``paused_at`` / ``escalated_at`` anchors
  on every loop iteration, so a daemon restart neither resets nor
  double-fires a window (rearm re-enters the same loop).
* **Arm-before-post** — re-publishes (rearm, escalation, defer rounds)
  happen only after the response waiter's subscription is armed. The one
  deliberate exception is the *initial* checkpoint publish (the tested
  ``checkpoint_product_docs`` contract owns pause+publish); the waiter
  arms milliseconds later and the escalation rounds are the self-healing
  backstop for a theoretically lost first response.
* **Task supervision** — the composition wraps ``drive`` tasks so an
  unhandled exception is logged loudly; a stalled run is recovered by the
  next boot's sweep/rearm (documented v1 limitation, exercised by
  TASK-MP-010 live validation).

References: TASK-MP-012, FEAT-SPL-002, DF-009, RT-04, RT-08, DDR-007.
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import hashlib
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

import yaml

from forge.gating.identity import derive_request_id, parse_request_id
from forge.lifecycle.identifiers import validate_feature_id
from forge.pipeline.stage_names import plain_stage_name
from forge.pipeline.stage_taxonomy import StageClass
from forge.planning.checkpoint import (
    PlanningEscalationContext,
    _dispatch_approval_response,
    build_planning_approval_envelope,
    checkpoint_product_docs,
)
from forge.planning.escalation import (
    EscalationOutcome,
    EscalationPolicy,
    evaluate_escalation_phase,
)
from forge.planning.failure import fail_run, mark_run_failed
from forge.planning.handoff import (
    PlannedHandoffHandler,
    PreCommitResult,
    build_feature_spec_input_content,
)
from forge.planning.planner import (
    BoundaryViolation,
    DispatchProductOwner,
    ExecuteHandoff,
    Fail,
    PauseAtCheckpoint,
    plan_next_step,
)
from forge.planning.revision import (
    CYCLE_CAP,
    REVISION_STAGE_LABEL,
    assemble_enrichment_batch,
    dialogue_cycle,
    normalize_assumptions,
    parse_dispositions,
)
from forge.planning.run_store import SqlitePlanningRunStore, TransitionRefused
from forge.planning.spec_digest import (
    DIGEST_ERROR_PREFIX,
    check_digest_consistency,
)
from forge.planning.states import PlanningState
from forge.planning.target_repos import format_known_repos

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge.config.models import PlanningConfig
    from forge.gating.wrappers import GateRepository, StateMachine
    from forge.preflight import ResourcePreflightResult
    from forge.planning.checkpoint import SecondOpinionProvider
    from forge.planning.target_terminal_tools import (
        NormalizeFeatureSpecFn,
        NormalizeStampsFn,
        StampNormalizerOutcome,
        ValidateFeaturePlanFn,
        ValidateGateRegistryFn,
        ValidatePassBarFn,
    )

logger = logging.getLogger(__name__)

__all__ = ["BuildTriggerResult", "PlanningDriverDeps", "PlanningRunDriver"]

#: Stage label used for the product docs checkpoint (pinned by checkpoint.py).
_PRODUCT_DOCS_STAGE = "product_docs"

#: Upper bound on how long we wait for a response subscription to arm
#: before a re-publish (mirrors _REARM_ARM_TIMEOUT_SECONDS).
_ARM_TIMEOUT_SECONDS = 10.0

_TERMINAL_STATES = {
    PlanningState.FAILED,
    PlanningState.CANCELLED,
    PlanningState.TIMED_OUT,
    PlanningState.PLANNED_HANDOFF,
    # Target terminal (Lane B). Only reachable when the flag is on; adding it
    # here is a byte-for-byte no-op with the flag off (unreachable state) and
    # makes a re-drive of a completed BUILD_QUEUED run return immediately.
    PlanningState.BUILD_QUEUED,
}

#: Durable stage labels for the target-terminal legs (Lane B / Phase E1 B2).
#: A completed leg records ``status="approved"`` under these labels; their
#: presence makes the legs idempotent on a re-drive (crash between the leg's
#: artifact write and the state transition never re-dispatches the specialist).
_FEATURE_SPEC_STAGE = "feature-spec"
_FEATURE_PLAN_STAGE = "feature-plan"
#: Durable stage label for the B3 build trigger. Its presence makes the trigger
#: idempotent on a re-drive (crash between the build-queued publish and the
#: BUILD_QUEUED transition never re-queues the build).
_BUILD_QUEUED_STAGE = "build-queued"
#: Durable stage label for the per-task QA pass-bar registration leg (B4 round-19,
#: Rich-ratified). Its presence makes the leg idempotent on a re-drive: a crash
#: between the bars commit and the BUILD_QUEUED transition never re-mints them.
_QA_PASS_BARS_STAGE = "qa-pass-bars"
#: Durable stage label for the per-feature live-gate REGISTRATION leg (F2 —
#: sibling of the pass-bar leg). Its presence makes the leg idempotent on a
#: re-drive: a crash between the gate commit and the BUILD_QUEUED transition
#: never re-fills the gate. An HONEST skip (non-endpoint feature, or a repo that
#: has not adopted the qa/gates/ surface) ALSO records this label (a skipped
#: detail), so a re-drive of a legitimately-skipped run is a clean no-op too.
_QA_FEATURE_GATE_STAGE = "qa-feature-gate"

#: Durable stage label for the AUTH-CONFIRMATION DOOR — the owner's one-tap
#: answer to a pass-bar seed flagged ``auth_surface_bearing`` (SPL-007 §A.2's
#: own words: the case "requires human confirmation"). Live run dff0cd00
#: (2026-07-31) proved the flag is OFTEN a FALSE POSITIVE — a spec PROVING its
#: own authlessness trips the keyword detector — and forge simply DIED. The door
#: asks instead. A durable ``approved`` event under this label is the
#: idempotency sentinel: a confirmed run NEVER re-asks on a re-drive (a crash
#: between the owner's tap and the bars commit drives straight through).
_AUTH_CONFIRM_STAGE = "qa-pass-bars-auth-confirm"

#: Durable stage label for the SPEC DRAFT — the spec is written and committed
#: but the owner has NOT yet said yes to it. Deliberately NOT the leg's
#: ``approved`` sentinel: it is the EARLIER sentinel that makes the digest door
#: restart-survivable. A re-drive that finds a draft re-opens the door with the
#: persisted card instead of re-dispatching the spec-writer and rewriting the
#: spec underneath a card the owner is still reading.
_SPEC_DRAFT_STAGE = "feature-spec-draft"

#: Durable stage label for the SPEC DIGEST REVIEW DOOR — the one pause of the
#: machine chain (the product-docs pause, absorbed and moved to where there is
#: something a person can actually check). A durable ``approved`` event under
#: this label is the door's idempotency sentinel; a ``revise`` event is a round
#: the owner answered with a note.
_DIGEST_REVIEW_STAGE = "feature-spec-digest-review"

#: The door-event statuses that mean A CARD IS STILL LIVE IN FRONT OF THE OWNER.
#: ``GATED`` is the first opening; ``reopened`` is a recovery re-emission after a
#: daemon restart (the checkpoint's ``republish_pending`` mechanic, mirrored).
#: When the LAST event for a door's stage label carries one of these statuses
#: the door is OPEN, and its persisted ``request_id`` is re-emitted VERBATIM —
#: minting a fresh id would orphan the card the owner can still see and silently
#: drop the tap they are about to give it.
_AUTH_DOOR_OPEN_STATUSES = frozenset({"GATED", "reopened"})

#: The ``checkpoint_type`` discriminator on the door's envelope — the same role
#: ``product_docs`` / ``product_docs_escalated`` play for the assumptions
#: checkpoint. The card asks for exactly the two answers a consumer that has
#: never seen this type can already offer (approve / reject), so a
#: jarvis-side renderer for it is polish, never a precondition.
_AUTH_CONFIRM_CHECKPOINT_TYPE = "auth_surface_confirmation"

#: The one-line pause rationale carried on the door's envelope.
_AUTH_CONFIRM_RATIONALE = (
    "The quality checklist's seed was flagged as sitting behind a sign-in; the "
    "owner confirms whether that is real before the checklist registers "
    "automatically."
)

#: What the honest terminal adds AFTER the unchanged refusal text, naming which
#: way the door closed. Plain language — the owner reads these.
#: (Vocabulary refreshed 2026-07-31: "bars" → the plain-name noun for the
#: ``qa-pass-bars`` stage, "the quality checklist" — see
#: :mod:`forge.pipeline.stage_names`.)
_AUTH_DOOR_TERMINAL_SUFFIX = {
    "rejected": (
        "The owner read the flagged lines and confirmed this IS a sign-in "
        "surface, so the quality checklist must be registered attended."
    ),
    "timed_out": (
        "Nobody answered the confirmation card inside the wait window, so the "
        "run stopped rather than register the quality checklist unattended."
    ),
    "undeliverable": (
        "The confirmation card could not be delivered, so nobody could answer "
        "it; the quality checklist must be registered attended."
    ),
    # An ANSWER that decided nothing (the generic approval consumer's "later" /
    # defer round). Never reported as silence — the owner DID touch the card.
    "deferred": (
        "The owner set the confirmation card aside instead of deciding it "
        "either way, so the run stopped rather than register the quality "
        "checklist unattended."
    ),
}

#: The ``checkpoint_type`` on the SPEC DIGEST card's envelope. The
#: ``product_docs`` prefix is not a trick — it is the messenger's actual
#: discriminator for a planning card, and this card genuinely IS the product-docs
#: checkpoint, absorbed and moved to where there is a spec to read. A planning
#: card WITHOUT that prefix is never posted at all (its body is discarded at
#: capture and its pause mirror is suppressed), so the prefix is what makes the
#: one front door work.
_DIGEST_REVIEW_CHECKPOINT_TYPE = "product_docs_spec_digest"

#: The one-line pause rationale carried on the digest card's envelope.
_DIGEST_REVIEW_RATIONALE = (
    "The spec is written. The owner reads one plain sentence per worked example "
    "— checked by ordinary code against the examples themselves — and says "
    "whether that is what they want built."
)

#: What the honest terminal adds after a digest review that did not end in a
#: yes. Plain language — the owner reads these.
_DIGEST_DOOR_TERMINAL_SUFFIX = {
    "rejected": (
        "The owner said no to the spec without leaving a note, so there was "
        "nothing to rewrite from and the run stopped."
    ),
    "timed_out": (
        "Nobody answered the card inside the wait window, so the run stopped "
        "rather than build a spec no one had read."
    ),
    "undeliverable": (
        "The card could not be delivered, so nobody could read the spec; the "
        "run stopped rather than build it unread."
    ),
    "deferred": (
        "The owner set the card aside instead of deciding it either way, so the "
        "run stopped rather than build a spec no one had said yes to."
    ),
}

#: The id the SIGN-IN question rides under when it is folded onto the digest
#: card (§2.6 — one tap, two questions).
#:
#: The owner's answer comes back on the SAME approval response as their answer
#: to the spec, in the wire's existing structured per-item field
#: (``ApprovalResponsePayload.dispositions``, ``nats-core``
#: ``events/_agent.py:208-215``) — never by reading their free-text note. A note
#: is prose, and guessing "there is a sign-in" out of prose is exactly the kind
#: of judgement this lane forbids; a disposition is a value, and reading it is
#: ordinary code.
#:
#: The card states the machine's assumption ("Nothing in this feature involves
#: signing in…") and the owner decides it like any other assumption on the card:
#:
#: * ``accepted`` — agreed, there is no sign-in: the build carries on and the
#:   quality checklist registers automatically (this is also what silence on
#:   this one item means, which is the 2026-08-14 §2.6 ruling unchanged);
#: * ``rejected`` — disagreed, this DOES involve signing in: the run takes the
#:   attended-registration terminal the 2026-07-31 ruling guarantees;
#: * anything else (``deferred`` / ``modified`` / ``undecided``) — an answer
#:   that decided nothing, which stops the run and NAMES itself rather than
#:   silently registering the checklist either way.
_SIGN_IN_ASSUMPTION_ID = "sign-in"

#: The digest artifact's suffix in the 007 native map. It rides beside the
#: three-file contract and is committed as a fourth file, so the planning branch
#: carries the complete record of what the owner approved.
_SPEC_DIGEST_SUFFIX = "_digest.yaml"

#: The target repo's OWN feature-behaviour gate TEMPLATE + gate registry, read
#: off the planning branch (never fabricated forge-side). Their ABSENCE is an
#: honest skip (the repo has not adopted the F4 gate surface), never a failure.
_FEATURE_GATE_TEMPLATE_REL = "qa/gates/feature_behaviour_gate.py"
_GATE_REGISTRY_REL = "qa/gates/registry.yaml"

#: The two placeholder literals the forge fill substitutes in the target repo's
#: OWN template SPEC block (guardkit api_test ``feature_behaviour_gate.py``). Each
#: must appear EXACTLY once; a count mismatch is a loud fill failure (the repo's
#: template drifted from the shape forge fills — never a silent wrong gate). The
#: ``/REPLACE_ME`` guard line elsewhere in the template is a DIFFERENT literal and
#: is deliberately left intact so an unedited copy still fails honestly at runtime.
_GATE_TEMPLATE_GATE_ID_LITERAL = '"gate_id": "feature-behaviour",'
_GATE_TEMPLATE_REQUEST_LITERAL = (
    '"request": {"method": "GET", "path": "/REPLACE_ME"},'
)

#: F2 endpoint-derivation grammar (deterministic, conservative — the design law).
#: Parse ONLY machine-class criterion text: a capital ``A``, an uppercase HTTP
#: verb, ``request to``, then an explicit ``/``-rooted path. Case-sensitive (no
#: IGNORECASE) so mid-sentence prose ("you can get request info") and mixed-case
#: never match; the leading ``\bA `` anchors the SPL criterion phrasing. Only a
#: GET yields a v1 gate (expect_status 200); any other verb, a missing path, or a
#: non-match yields None → an honest skip (never a guessed success status).
_FEATURE_GATE_ENDPOINT_RE = re.compile(
    r"\bA (?P<method>GET|POST|PUT|PATCH|DELETE) request to "
    r"(?P<path>/[A-Za-z0-9_/{}-]*)\b"
)


class _FeatureGateFillError(Exception):
    """A derivable feature gate could not be filled / its registry appended.

    Raised by the F2 fill helpers on template drift or an unparseable/empty
    registry — the caller maps it to a LOUD ``_fail_leg`` (SKIP-vs-FAIL law (c):
    template present + derivable but the fill fails is a failure, never a skip).
    """

#: The 007 native artifact-map key convention for the feature-grain quality-bar
#: SEED (specialist-agent product_owner/modes/feature_spec.py; guardkit
#: ``/feature-spec`` §"Quality-bar seed emission"): ``pass-bar-seed-*.yaml``. It
#: is a tolerated extra in the spec map — NEVER a committed spec-triple file —
#: that forge CAPTURES at the spec commit and specialises into per-task bars at
#: plan-commit (the guardkit ``/feature-plan`` "consumes this seed" contract,
#: done forge-side because the machine 008 leg does not emit the per-task bars).
_PASS_BAR_SEED_PREFIX = "pass-bar-seed-"
_PASS_BAR_SEED_SUFFIX = ".yaml"

#: The negative path mandatory for EVERY pass bar, auth-surface-bearing or not
#: (guardkit PB-14 / :data:`guardkit.qa.formats.pass_bar.UNIVERSAL_NEGATIVE_PATHS`).
#: An authless per-task bar (``auth_surface_bearing: false`` — either by
#: construction or by the owner's confirmation at the auth door, see
#: :data:`_AUTH_CONFIRM_STAGE`) needs exactly this one to satisfy guardkit's own
#: schema — the seed itself carries no ``negative_paths``, so forge supplies the
#: universal minimum.
_UNIVERSAL_NEGATIVE_PATH = "dependency_down_degradation"

#: The pass-bar schema version forge mints (guardkit
#: ``PassBar.CURRENT_FORMAT_VERSION``). Carried from the seed when present.
_PASS_BAR_FORMAT_VERSION = "2.0"

#: Task types whose guardkit quality-gate profile asks for NO tests
#: (``guardkit/models/task_types.py`` DEFAULT_PROFILES: DOCUMENTATION has
#: ``tests_required=False`` and ``plan_audit_required=False``). ``research`` is
#: guardkit's own alias for ``documentation`` (TASK_TYPE_ALIASES in that file),
#: so a plan that uses the alias lands in the same place. The plan states the
#: type itself in each task file's front matter, and that front-matter value is
#: the SAME one guardkit reads at build time
#: (``FeatureOrchestrator._read_task_type_from_frontmatter``) — so forge keys off
#: the plan's own word, never a second guess of its own.
_DOCS_TASK_TYPES = frozenset({"documentation", "research"})

#: Evidence kind forge writes on a criterion it derives from a task's own
#: "## Acceptance Criteria" section. Mirrors guardkit's own machine minter
#: (``guardkit/orchestrator/pass_bar_mint.py`` ``_DERIVED_EVIDENCE_KIND``): a
#: checker loop produces a log, and the two LIVE kinds (``screenshot``,
#: ``operator_signoff``) would arm the feature-complete runtime-surface gate off
#: a documentation task — a fabricated gate.
_DERIVED_EVIDENCE_KIND = "log"

#: The marker every narrowed documentation bar carries in its leading comment
#: block. ``PassBar`` is ``extra="forbid"``, so a note has nowhere to live as a
#: field; guardkit's own minter uses the same leading-comment form, which the
#: schema ignores and a reader cannot miss.
_DOCS_BAR_NOTE_MARKER = "DOCUMENTATION TASK — NOT A COPY OF THE FEATURE CHECKLIST"

#: One "## Acceptance Criteria" bullet in a task file. Both the plain-bullet
#: form the 008 planner writes (``- text``) and the checkbox form
#: (``- [ ] text``) count: the checkbox is optional, and reading only the
#: checkbox form is the same bug that blocked a feature close on 2026-08-14.
_TASK_CRITERION_RE = re.compile(r"^\s*[-*]\s*(?:\[[ xX]?\]\s*)?(?P<text>\S.*?)\s*$")

#: A criterion that names its own id (``AC-1: ...``) keeps it, exactly as
#: guardkit's minter does; anything else is numbered in order.
_TASK_CRITERION_ID_RE = re.compile(
    r"^(?P<id>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\s*:\s*(?P<text>\S.*)$"
)

#: A markdown heading of any level, used to find (and end) the criteria section.
_TASK_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")

#: Where a target repository keeps its own written architecture rules, when it
#: keeps any. api_test does: twelve rules, each one quoting the sentence in
#: docs/architecture/ it comes from. Most repositories have no such file, and
#: for those the descriptor carries no rules key and planning is unchanged.
_ARCHITECTURE_RULES_REL = "docs/architecture-rules.yaml"

#: Bounds on what travels in the descriptor, so a long rules file cannot swell
#: the planning request. api_test's file has 12 rules and its longest quoted
#: sentence is around 200 characters, so neither bound bites there.
_MAX_ARCHITECTURE_RULES = 60
_MAX_ARCHITECTURE_RULE_CHARS = 400

#: The contract reference the auth door's card and its honest terminal name
#: verbatim (the clause whose OWN words are "requires human confirmation").
_SPL_007_AUTH_CLAUSE = "SPL-007 §A.2"

#: Filesystem-safe feature slug allowlist (mirrors the identifier boundary):
#: a specialist-supplied slug is only trusted when it matches, else forge falls
#: back to a deterministic ``feature-{cid}``.
_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")

#: The three suffix conventions the 007 (po_feature_spec) native artifact map
#: keys carry — the contract of record (specialist-agent product_owner/modes/
#: feature_spec.py ``_ARTIFACT_SUFFIXES``). forge projects the committed triple
#: from these; anything else in the map (a ``pass-bar-seed-*.yaml``,
#: ``validation.json``) is a tolerated extra, never part of the committed triple.
_SPEC_FEATURE_SUFFIX = ".feature"
_SPEC_ASSUMPTIONS_SUFFIX = "_assumptions.yaml"
_SPEC_SUMMARY_SUFFIX = "_summary.md"

#: Keys that appear in a specialist artifact map (or a wrap_role_output envelope)
#: that are NOT committable target-repo files: the out-of-band validation-as-data
#: channels (``validation.json`` / ``seed_errors.json``) plus any envelope
#: scalars. ``_plan_tree_files`` excludes these so only real repo paths commit.
_NON_ARTIFACT_KEYS = frozenset(
    {
        "validation.json",
        "seed_errors.json",
        "feature_id",
        "slug",
        "role_id",
        "coach_score",
        "criterion_breakdown",
        "detection_findings",
        "role_output",
    }
)

PublishNotificationFn = Callable[..., Awaitable[None]]
"""``async (correlation_id, message, level) -> None`` — best-effort notify.

A publisher MAY also accept ``mention: bool = True`` as a keyword; the driver
passes ``mention=False`` (a plain line, no @mention — the stamp normalizer's
un-enforced line) only to a publisher whose signature takes it, and the
three-positional form otherwise."""

ResourcePreflightFn = Callable[[], "ResourcePreflightResult"]
"""``() -> ResourcePreflightResult`` — a zero-arg pre-run resource check.

Bound (e.g. ``functools.partial(run_resource_preflight, config.resource_preflight)``)
so the driver stays ignorant of ``/proc`` and ``shutil``. Consulted once at the
QUEUED→RUNNING run-start boundary (O-27/O-29); ``None`` = no preflight wired."""

SubscriberFactory = Callable[[str | None, "asyncio.Event | None"], Any]
"""``(expected_approver, armed_event) -> subscriber`` — the returned object
exposes ``await_response(build_id, *, stage_label, attempt_count,
timeout_seconds)`` (the ApprovalSubscriber surface); ``armed_event`` is set
the moment the underlying subscription is active (arm-before-post)."""

DispatchProductOwnerFn = Callable[..., Awaitable[Any]]
"""``async (*, plan_run_id, correlation_id, enrichment=None) -> StageDispatchResult``.

``enrichment`` is the EnrichmentBatch-shaped revision delta on an
assumption-dialogue re-invoke (``None`` on the first dispatch)."""

DispatchFeatureSpecFn = Callable[..., Awaitable[Any]]
"""``async (*, plan_run_id, correlation_id, spec_input, revision_of=None,
validate_feedback=None) -> StageDispatchResult``.

Lane B (B2): dispatch the ``po_feature_spec`` (007) leg with the committed
feature-spec-input content; the result's ``role_output`` carries the spec
contract — the ``.feature``, the assumptions manifest, the summary and the SPEC
DIGEST (the plain-language list a person reads).

On a REWRITE round the two optional arguments carry the owner's note VERBATIM
(``validate_feedback``) and the prior artifact set (``revision_of``). A
first-round dispatch passes neither."""

DispatchFeaturePlanFn = Callable[..., Awaitable[Any]]
"""``async (*, plan_run_id, correlation_id, feature_id, spec_feature,
spec_summary, target_repo_descriptor, spec_assumptions=None,
spec_feature_paths=None) -> StageDispatchResult``.

Lane B (B2): dispatch the ``architect_feature_plan`` (008) leg. Forge supplies
the SUPPLIED minted ``feature_id`` (RV-1: the plan leg asserts it), the 007 spec
triple CONTENTS (``spec_feature`` = the committed .feature, ``spec_summary`` =
the committed _summary.md, optional ``spec_assumptions`` = the committed
_assumptions.yaml), and the structured ``target_repo_descriptor`` — the exact
argument shape ``architect_feature_plan`` requires (specialist-agent
roles/architect/modes/feature_plan.py). The result's ``role_output`` carries the
plan tree.

2026-08-22 (the specification-location lane): ``spec_feature_paths`` carries
WHERE the spec ``.feature`` sits on the planning branch, beside
``spec_feature``, which carries WHAT it says. The plan YAML has to declare that
location under ``feature_files:``, and until now forge never told the plan-writer
what it was — so the writer built a folder name out of the feature's title, and
six of the ten plans captured on 2026-08-22 that wrote the key named a folder
that does not exist. Forge already knew: it committed those files itself one leg
earlier, and reads them back off the branch two statements above this dispatch.
OPTIONAL on the wire, so an older specialist that does not know the argument is
unaffected."""


def _reject_word_split(note: str) -> tuple[bool, str]:
    """Split a typed reply that STARTS with the word "reject" from its reason.

    The digest card's note box is the owner's only typed channel, and every
    note rides the wire as ``decision="reject"`` — so the word the reply
    STARTS with is the only way a typed "reject" (stop this run) can be told
    apart from a revision note. A first word of "reject" — any
    capitalisation, with or without more words, trailing punctuation on the
    word tolerated ("reject:", "reject -") — counts. Everything after the
    word, minus any leading separator, is the owner's reason.

    Returns ``(starts_with_reject, reason)``; ``reason`` is ``""`` for a
    bare "reject" and whenever the first word is anything else.
    """
    parts = str(note or "").strip().split(None, 1)
    if not parts:
        return False, ""
    first = parts[0].rstrip(".,:;!?-\u2013\u2014")
    if first.lower() != "reject":
        return False, ""
    rest = parts[1] if len(parts) > 1 else ""
    return True, rest.lstrip(" \t.,:;!?-\u2013\u2014").strip()


def _person_words(identity: str | None, display_name: str | None = None) -> str:
    """The person, in words a reader recognises — never a raw chat id.

    A card and its notifications are read by the person who was asked, and on
    2026-09-05 one of them read "U03QR8WKT29 sent a note". That string is the
    chat system's internal member id: it identifies nobody to a human eye, and
    it is the only identity the approval payload carries today (``decided_by``
    and the run's ``expected_approver`` are both that id).

    So: a display name if one ever travels with the answer, and otherwise the
    word "you" — the person reading the line IS the person who was asked, since
    the card is threaded to them. The raw id never reaches a sentence a person
    reads; it stays on the durable row, where it belongs.
    """
    name = str(display_name or "").strip()
    if name:
        return name
    return "you"


def _responder_display_name(response: Any) -> str | None:
    """The answerer's display name, when the response carries one.

    The approval payload does not declare such a field today and its model
    ignores undeclared ones, so this is ``None`` on every live response — and
    the sentences fall back to "you". It is read rather than assumed absent so
    that the day a name does travel, the card and the pings use it without
    another change here.
    """
    for attr in ("decided_by_name", "display_name", "decided_by_display_name"):
        value = getattr(response, attr, None)
        if value:
            return str(value)
    return None


def _card_sentences(card: Mapping[str, Any] | None) -> list[str]:
    """The plain sentences a digest card lists, in order — the list itself."""
    out: list[str] = []
    for entry in (card or {}).get("what_it_will_do") or []:
        if isinstance(entry, Mapping):
            out.append(str(entry.get("sentence") or ""))
    return out


def _card_assumptions(card: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """The assumptions a digest card lists, in order, with their reasons."""
    out: list[tuple[str, str]] = []
    for entry in (card or {}).get("what_the_machine_assumed") or []:
        if isinstance(entry, Mapping):
            out.append((str(entry.get("assumption") or ""), str(entry.get("why") or "")))
    return out


def _list_change_phrases(
    before: list[Any], after: list[Any], *, singular: str, plural: str
) -> list[str]:
    """How one list changed, counted in plain words ("2 examples changed").

    An ordinary line-diff, nothing clever: a run of replaced entries is
    "changed" as far as it goes and "added"/"removed" for the remainder. The
    first phrase carries the noun and the rest do not, so the phrases join into
    a sentence a person reads once ("2 examples changed, 1 removed").
    """
    changed = added = removed = 0
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_n, new_n = i2 - i1, j2 - j1
        if tag == "replace":
            changed += min(old_n, new_n)
            added += max(0, new_n - old_n)
            removed += max(0, old_n - new_n)
        elif tag == "insert":
            added += new_n
        elif tag == "delete":
            removed += old_n
    phrases: list[str] = []
    for count, word in ((changed, "changed"), (added, "added"), (removed, "removed")):
        if not count:
            continue
        if phrases:
            phrases.append(f"{count} {word}")
        else:
            phrases.append(f"{count} {singular if count == 1 else plural} {word}")
    return phrases


def _plain_card_changes(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> str:
    """What changed between two digest cards, in plain words; "" when nothing did."""
    examples = _list_change_phrases(
        _card_sentences(previous),
        _card_sentences(current),
        singular="example",
        plural="examples",
    )
    assumptions = _list_change_phrases(
        _card_assumptions(previous),
        _card_assumptions(current),
        singular="assumption",
        plural="assumptions",
    )
    parts = [", ".join(group) for group in (examples, assumptions) if group]
    return "; ".join(parts)


def _cards_say_the_same_thing(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    """True when a rewrite came back with the same list the owner just rejected."""
    return _card_sentences(previous) == _card_sentences(current) and _card_assumptions(
        previous
    ) == _card_assumptions(current)


#: What the card says when the rewrite changed nothing the owner can see. The
#: 2026-09-05 defect in one sentence: the second card was identical line for
#: line to the first and said nothing about it, so a note that did nothing
#: looked exactly like a note that worked.
_SAME_LIST_CARD_TEXT = (
    'The rewrite came back with the same list. Your note was: "{note}". '
    "Approve anyway, send another note, or reject."
)

#: The same fact as ONE sentence, for the notification that opens that round.
_SAME_LIST_NOTIFICATION = (
    "Planning run {correlation_id}: the rewrite came back with the same list "
    '— your note was "{note}" — so approve anyway, send another note, or reject.'
)

#: The one line added to an otherwise unchanged card when the rewrite DID
#: change something: what changed, counted, in the words of the list itself.
_CHANGED_LIST_CARD_LINE = "What changed since your note: {changes}."


@dataclass(frozen=True)
class _DoorAnswer:
    """What came back through an inline confirmation door.

    ``outcome`` is the caller's own vocabulary (``confirmed`` for the sign-in
    door, ``approved`` / ``revise`` for the spec-digest door, plus the shared
    ``rejected`` / ``deferred`` / ``timed_out`` / ``undeliverable``).
    ``decision`` is the literal wire answer and ``notes`` the owner's own words
    — the channel their red pen rides. Both are ``None`` when the door closed
    without anybody answering it.

    ``item_answers`` carries the per-item answers that rode the SAME response
    (the wire's ``dispositions`` field), keyed by item id — the channel the
    sign-in question folded onto the digest card is answered through. Empty
    when the response carried none, which is every response today that is not a
    per-item one.
    """

    outcome: str
    request_id: str
    decided_by: str | None = None
    #: The answerer's DISPLAY NAME when the response carried one — the words a
    #: person reads, as opposed to ``decided_by``, which is the chat system's
    #: internal member id. ``None`` for every response today (the payload model
    #: ignores fields it does not declare), and the sentences a person reads
    #: fall back to "you", never to the id.
    decided_by_name: str | None = None
    decision: str | None = None
    notes: str | None = None
    item_answers: Mapping[str, str] = field(default_factory=dict)


def _item_answers(response: Any) -> dict[str, str]:
    """The per-item answers on an approval response, keyed by item id.

    Reads ``ApprovalResponsePayload.dispositions`` — the wire's own structured
    per-item channel, whose values are already normalised to the canonical
    vocabulary (``accepted`` / ``modified`` / ``rejected`` / ``deferred`` /
    ``undecided``) by the payload model itself, so nothing here interprets
    anything. A response carrying none (every whole-card answer today) yields an
    empty map, and an entry too malformed to have both an id and a value is
    dropped rather than guessed at.
    """
    answers: dict[str, str] = {}
    for item in getattr(response, "dispositions", None) or []:
        item_id = str(getattr(item, "assumption_id", "") or "")
        disposition = str(getattr(item, "disposition", "") or "")
        if item_id and disposition:
            answers[item_id] = disposition
    return answers


@dataclass(frozen=True)
class BuildTriggerResult:
    """Outcome of the B3 build trigger (Lane B / Phase E1 B3).

    ``queued`` True means the feature was accepted onto forge's OWN Mode B
    build intake (``pipeline.build-queued.{feature_id}``) — the canonical
    dispatcher whose pre-dispatch approval gate then pauses the build for the
    human tap. ``build_id`` is the build identifier when the trigger seam
    surfaces one (``None`` for the fire-and-forget publish seam). ``reason``
    carries the loud-failure detail when ``queued`` is False.
    """

    queued: bool
    build_id: str | None = None
    reason: str | None = None


DispatchBuildTriggerFn = Callable[..., Awaitable["BuildTriggerResult"]]
"""``async (*, plan_run_id, correlation_id, feature_id, target_repo, branch,
plan_files, originating_user) -> BuildTriggerResult``.

Lane B (B3): queue the validated feature onto forge's OWN Mode B build
dispatcher (``dispatch_autobuild_async`` reached via the build-queued intake,
NOT the local guardkit CLI) so the pre-dispatch approval gate pauses it for the
human tap. This is a fire-and-forget publish — it does NOT wait on a specialist
round-trip, so it introduces no new unbounded wait (rule 5)."""


def _extract_assumptions(result: Any) -> list[dict[str, Any]]:
    """Best-effort projection of PO-surfaced assumptions off a dispatch result.

    The current :class:`StageDispatchResult` shape does not carry assumptions
    as a first-class field, so this reads an ``assumptions`` attribute when the
    PO result exposes one and falls back to ``criterion_breakdown['assumptions']``.
    Empty when neither is present (the checkpoint degrades to a no-assumptions
    prompt — never a crash).
    """
    raw = getattr(result, "assumptions", None)
    if raw is None:
        breakdown = getattr(result, "criterion_breakdown", None)
        if isinstance(breakdown, Mapping):
            raw = breakdown.get("assumptions")
    return normalize_assumptions(raw)


def _accepts_keyword(fn: Any, name: str) -> bool:
    """Whether calling ``fn(..., name=...)`` is accepted by its signature —
    an explicit keyword parameter or a ``**kwargs`` catch-all. ``False`` when
    the signature cannot be read (a builtin / C callable): the caller then
    uses the positional form it always used."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def task_type_from_front_matter(task_text: str) -> str | None:
    """Read a task file's declared ``task_type``, lower-cased, or ``None``.

    The plan writes each task as a markdown file whose YAML front matter states
    the task's own type (``task_type: documentation``). That declared value is
    the SAME one guardkit reads when it decides which quality gates to run
    (``FeatureOrchestrator._read_task_type_from_frontmatter``), so it is the one
    honest answer to "will tests run for this task" available at plan-commit.

    A deliberate line scan rather than a YAML parse: this runs over files a
    model wrote, and front matter that does not parse must not take the
    planning run down. No front matter, no key, or an unreadable file ⇒
    ``None`` ⇒ the caller behaves exactly as it did before this existed.
    """
    lines = (task_text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        if name.strip() != "task_type":
            continue
        cleaned = value.strip().strip('"').strip("'").lower()
        return cleaned or None
    return None


def task_acceptance_criteria(task_text: str) -> list[dict[str, str]]:
    """The criteria written ON a task, as pass-bar criterion mappings.

    Reads the task file's ``## Acceptance Criteria`` section — the same section
    guardkit's own machine minter reads
    (``guardkit/orchestrator/pass_bar_mint.py`` ``read_acceptance_criteria``)
    and the same list the Coach holds the task to. Returns ``[]`` when the
    section is absent or empty.

    Class and evidence kind mirror that minter's judgements, for its reasons:
    ``machine`` because there is no operator runbook to route an operator
    criterion to (a criterion with nowhere to route is a silently dropped one),
    and ``log`` because the two live evidence kinds would arm the
    feature-complete runtime-surface gate off a documentation task.
    """
    in_section = False
    raw_items: list[str] = []
    for line in (task_text or "").splitlines():
        heading = _TASK_HEADING_RE.match(line)
        if heading:
            title = heading.group("title").strip().lower()
            if in_section:
                break
            in_section = title.startswith("acceptance criteria")
            continue
        if not in_section:
            continue
        item = _TASK_CRITERION_RE.match(line)
        if item:
            raw_items.append(item.group("text"))

    criteria: list[dict[str, str]] = []
    used: set[str] = set()
    for ordinal, raw in enumerate(raw_items, start=1):
        named = _TASK_CRITERION_ID_RE.match(raw)
        if named:
            candidate, text = named.group("id"), named.group("text")
        else:
            candidate, text = f"AC-{ordinal}", raw
        unique, suffix = candidate, 2
        while unique in used:
            unique = f"{candidate}-{suffix}"
            suffix += 1
        used.add(unique)
        criteria.append(
            {
                "id": unique,
                "text": text,
                "class": "machine",
                "evidence_kind": _DERIVED_EVIDENCE_KIND,
                "runbook_ref": None,
            }
        )
    return criteria


@dataclass
class PlanningDriverDeps:
    """Injected collaborators for :class:`PlanningRunDriver`."""

    store: SqlitePlanningRunStore
    repository: "GateRepository"
    state_machine: "StateMachine"
    approval_publisher: Any  # publish_request(envelope) — pause-mirroring wrapper
    subscriber_factory: SubscriberFactory
    dispatch_product_owner: DispatchProductOwnerFn
    second_opinion_provider: "SecondOpinionProvider"
    git_runner: Any  # forge.planning.handoff.GitRunner
    planning_config: "PlanningConfig"
    clock: Callable[[], datetime]
    publish_notification: PublishNotificationFn | None = None
    # O-27/O-29 (E2-S4) — pre-run resource-headroom preflight. Optional / default
    # None: unwired = no preflight (byte-for-byte no-op). Wired, it is consulted
    # exactly ONCE at the fresh QUEUED→RUNNING run-start boundary; a breach fails
    # the run LOUDLY before any seat-holding dispatch (never a mid-run kill, and
    # never re-run on a crash re-drive of an already-RUNNING run).
    resource_preflight: ResourcePreflightFn | None = None
    # Lane B / Phase E1 (B2) — the target-terminal spec/plan legs. All optional
    # and default None: with the target-terminal flag OFF they are never
    # consulted (byte-for-byte no-op). With the flag ON they are required; a
    # missing collaborator fails the run LOUDLY (never a silent skip).
    dispatch_feature_spec: DispatchFeatureSpecFn | None = None
    dispatch_feature_plan: DispatchFeaturePlanFn | None = None
    normalize_feature_spec: "NormalizeFeatureSpecFn | None" = None
    validate_feature_plan: "ValidateFeaturePlanFn | None" = None
    # THE STAMP NORMALIZER (Rich's condition 1, 2026-08-16) — ``guardkit qa
    # normalize-stamps`` run against the planning worktree immediately BEFORE
    # the plan-commit ``feature validate``, so the rule-minted ``verifier:``
    # stamps are WRITTEN on the planning branch and ride the plan commit.
    # Optional / default None: unwired = the plan leg proceeds exactly as
    # before and the plan receipts say ``not-wired`` (never silent). Wired, a
    # refusal (undecidable titles) or a failure STOPS the run with a card
    # naming the titles verbatim; an older guardkit without the subcommand
    # continues (backward compatible until the rebake) and is receipted.
    normalize_stamps: "NormalizeStampsFn | None" = None
    # Lane B / Phase E1 (B4 round-19, Rich-ratified) — the guardkit ``qa validate
    # pass-bar`` oracle. Optional / default None: with the flag OFF it is never
    # consulted. With the flag ON the per-task QA pass-bar registration leg
    # requires it; a missing collaborator fails the run LOUDLY (never a silent
    # skip — the same posture as the other target-terminal oracles).
    validate_pass_bar: "ValidatePassBarFn | None" = None
    # Lane B / Phase E1 (F2 — the per-feature live-gate REGISTRATION leg, sibling
    # of the pass-bar leg) — the guardkit ``qa validate gate-registry`` oracle.
    # Optional / default None: with the flag OFF it is never consulted. With the
    # flag ON the per-feature gate-registration leg requires it; a missing
    # collaborator fails the run LOUDLY (never a silent skip — the same posture
    # as the other target-terminal oracles).
    validate_gate_registry: "ValidateGateRegistryFn | None" = None
    # Lane B / Phase E1 (B3) — the build trigger. Optional / default None: with
    # the target-terminal flag OFF it is never consulted; with the flag ON it is
    # required and a missing collaborator fails the run LOUDLY (never silent).
    dispatch_build_trigger: DispatchBuildTriggerFn | None = None


@dataclass(frozen=True)
class _HistoryEvent:
    """Planner-shaped view of a planning_run_events row."""

    stage: StageClass
    status: str
    details: Mapping[str, Any]


class PlanningRunDriver:
    """Re-entrant chain driver for one planning run at a time.

    ``drive`` may be called for a run in ANY state — it resumes from the
    durable state and history, which is exactly what the boot sweep
    (QUEUED / RUNNING / FEATURE_SPEC / FEATURE_PLAN) and rearm (PAUSED,
    ``republish_pending=True``) need. Every machine-chain leg is idempotent
    on its own durable event, so a re-drive of an interrupted chain run
    completes the remaining legs instead of redoing the finished ones —
    including re-opening an auth-confirmation door the crash left live.
    """

    def __init__(self, deps: PlanningDriverDeps) -> None:
        self._deps = deps

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def drive(
        self, correlation_id: str, *, republish_pending: bool = False
    ) -> None:
        """Drive ``correlation_id`` forward from its durable state.

        Args:
            correlation_id: The planning run to advance.
            republish_pending: True on the rearm path — the persisted
                ``pending_approval_request_id`` is re-emitted VERBATIM
                after the response waiter arms (ASSUM-015's compensating
                half; spec: "re-issued exactly once by the recovery
                process").
        """
        deps = self._deps
        row = deps.store.get_run(correlation_id)
        if row is None:
            logger.error("planning driver: run %s not found", correlation_id)
            return

        state = PlanningState(row["state"])
        if state in _TERMINAL_STATES:
            logger.info(
                "planning driver: run %s already terminal (%s); nothing to do",
                correlation_id,
                state.value,
            )
            return

        plan_run_id = f"plan-{correlation_id}"

        if state is PlanningState.QUEUED:
            refused = deps.store.transition(
                correlation_id=correlation_id,
                to_state=PlanningState.RUNNING,
                actor_identity="planning-driver",
                stage_label="planning-start",
            )
            if isinstance(refused, TransitionRefused):
                logger.warning(
                    "planning driver: QUEUED→RUNNING refused for %s "
                    "(current=%s); another driver won — backing off",
                    correlation_id,
                    refused.current_state,
                )
                return

            # O-27/O-29 — resource-headroom preflight at the fresh run-start
            # boundary. We are now RUNNING (FAILED is a legal edge) and nothing
            # has dispatched yet, so a starved box refuses CLEANLY here rather
            # than risk a mid-run kernel OOM-kill / ENOSPC. Only on this fresh
            # QUEUED→RUNNING path: a crash re-drive of an already-RUNNING run
            # never re-preflights (we never kill work in flight).
            if deps.resource_preflight is not None:
                preflight = deps.resource_preflight()
                if not preflight.ok:
                    logger.error(
                        "planning driver: %s refused at run start — %s",
                        correlation_id,
                        preflight.summary,
                    )
                    await self._fail_leg(
                        correlation_id,
                        stage_label="resource-preflight",
                        reason=preflight.summary,
                    )
                    return

        needs_republish = republish_pending
        checkpoint_failures = 0
        while True:
            row = deps.store.get_run(correlation_id)
            if row is None:  # pragma: no cover - defensive
                return
            state = PlanningState(row["state"])

            if state in _TERMINAL_STATES:
                return

            if state is PlanningState.PAUSED:
                outcome = await self._await_approval(
                    row, plan_run_id, needs_republish=needs_republish
                )
                needs_republish = False
                if outcome != "approved":
                    return
                continue  # re-read: now RUNNING, planner advances the chain

            # Target-terminal chain (Lane B / Phase E1). These states are
            # forge-machine states, not Mode-P-planner states — the driver
            # advances them directly (the pure planner never sees them; its
            # forbidden-stage guard only inspects the product_owner /
            # checkpoint_cleared history rows, which use different labels).
            if state is PlanningState.FEATURE_SPEC:
                if not await self._feature_spec_leg(row, correlation_id):
                    return
                continue  # re-read: now FEATURE_PLAN
            if state is PlanningState.FEATURE_PLAN:
                # B2: dispatch 008 + write + validate the plan tree (idempotent
                # on a re-drive). Then (B4 round-19): fan the captured 007 seed
                # out into per-task ``qa/pass-bar-<TASK-ID>.yaml`` bars and commit
                # them BEFORE the build trigger — the WS2 B2 precondition demands
                # the bars be registered before implementation. B3: on validate
                # green, queue the feature onto forge's own Mode B dispatcher and
                # advance to BUILD_QUEUED.
                if not await self._feature_plan_leg(row, correlation_id):
                    return
                if not await self._register_pass_bars_leg(row, correlation_id):
                    return
                if not await self._register_feature_gate_leg(row, correlation_id):
                    return
                if not await self._build_trigger_leg(row, correlation_id):
                    return
                continue  # re-read: now BUILD_QUEUED (terminal) — loop returns

            # state is RUNNING — consult the pure planner over durable history
            history = self._load_history(correlation_id)
            decision = plan_next_step(history)

            if isinstance(decision, BoundaryViolation):
                self._fail(
                    correlation_id,
                    stage_label="planning-boundary",
                    reason=decision.rationale,
                )
                return

            if isinstance(decision, Fail):
                self._fail(
                    correlation_id,
                    stage_label="planning-dispatch",
                    reason=decision.reason,
                )
                # The planner's ``reason`` is the MACHINE string (it embeds the
                # internal stage label) and stays verbatim on the durable row
                # above. The owner is told which stage stopped in PLAIN words,
                # with the dispatch's own error detail after it.
                await self._notify(
                    correlation_id,
                    self._dispatch_failure_message(correlation_id, decision),
                    level="error",
                )
                return

            if isinstance(decision, DispatchProductOwner):
                ok = await self._dispatch_po(correlation_id, plan_run_id)
                if not ok:
                    return
                continue

            if isinstance(decision, PauseAtCheckpoint):
                # THE ONE PAUSE, MOVED. On the machine chain the brief-stage
                # card does not open: its question — "is this what you want?" —
                # is asked later in the same run, in front of the SPEC, where
                # there is something a person can actually check. The pause
                # count per feature does not change: it was one, it stays one.
                #
                # This is not an auto-approve. ``checkpoint_product_docs`` is
                # byte-unchanged and still always pauses; the machine chain
                # simply does not call it, and puts a REAL human pause later in
                # the same run. The row written here says WHY, in the actor
                # identity, so nobody reading the log later mistakes an
                # absorbed pause for a skipped one.
                if self._target_terminal_enabled():
                    if not self._absorb_product_docs_checkpoint(correlation_id):
                        # The row is already there and the planner still wants
                        # to pause: something upstream is refusing to read it.
                        # Stop loudly rather than spin a no-yield loop writing
                        # the same row forever.
                        self._fail(
                            correlation_id,
                            stage_label=_PRODUCT_DOCS_STAGE,
                            reason="the brief-stage checkpoint is already "
                            "recorded as absorbed but the chain still asks to "
                            "pause there",
                        )
                        await self._notify(
                            correlation_id,
                            f"Planning run {correlation_id} stopped at "
                            f"{plain_stage_name(_PRODUCT_DOCS_STAGE)}: the run "
                            "could not move past a step it has already been "
                            "through. Nothing was built.",
                            level="error",
                        )
                        return
                    continue
                paused = await self._checkpoint(correlation_id, plan_run_id)
                if not paused:
                    # The pause did NOT reach durable state (pre-pause store
                    # failure) — retrying instantly would spin a no-yield
                    # loop and starve the event loop (TASK-MP-012 review
                    # finding). Back off, and fail terminally after
                    # repeated failures.
                    checkpoint_failures += 1
                    if checkpoint_failures >= 3:
                        self._fail(
                            correlation_id,
                            stage_label=_PRODUCT_DOCS_STAGE,
                            reason="checkpoint pause failed 3 times "
                            "(store unavailable?)",
                        )
                        return
                    await asyncio.sleep(1.0)
                continue

            if isinstance(decision, ExecuteHandoff):
                if self._target_terminal_enabled():
                    # Flag ON: write the handoff file (the 007 input) and enter
                    # the machine chain instead of terminating. PLANNED_HANDOFF
                    # stays the reachable fallback (flag OFF), never removed.
                    if not await self._enter_target_terminal(row, correlation_id):
                        return
                    continue  # re-read: now FEATURE_SPEC
                await self._handoff(row, correlation_id)
                return

            logger.error(  # pragma: no cover - exhaustive union
                "planning driver: unknown planner decision %r for %s",
                decision,
                correlation_id,
            )
            return

    # ------------------------------------------------------------------ #
    # Chain steps
    # ------------------------------------------------------------------ #

    async def _dispatch_po(
        self,
        correlation_id: str,
        plan_run_id: str,
        *,
        enrichment: dict[str, Any] | None = None,
    ) -> bool:
        """Dispatch the PRODUCT_OWNER specialist stage; record the outcome.

        On an assumption-dialogue revision, ``enrichment`` carries the
        EnrichmentBatch-shaped delta (prior assumptions + human dispositions);
        the PO re-invoke is stateless (propose-never-elicit — forge assembles
        the delta). The delta is threaded to the dispatch collaborator and
        recorded on the PO event for durability.
        """
        deps = self._deps
        # Pass ``enrichment`` only on a revision re-invoke so first-dispatch
        # collaborators keep their ``(*, plan_run_id, correlation_id)`` shape.
        extra: dict[str, Any] = {"enrichment": enrichment} if enrichment else {}
        try:
            result = await deps.dispatch_product_owner(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 — driver never crashes the run silently
            logger.exception(
                "planning driver: PO dispatch raised for %s", correlation_id
            )
            self._fail(
                correlation_id,
                stage_label="product_owner",
                reason=f"PO dispatch raised {type(exc).__name__}: {exc}",
            )
            return False

        outcome = getattr(result, "outcome", None)
        outcome_value = str(getattr(outcome, "value", outcome or "error")).lower()
        coach_score = getattr(result, "coach_score", None)
        reason = getattr(result, "reason", None)

        if outcome_value in ("completed", "degraded"):
            # M10: the product-doc content is the REAL role_output document
            # the specialist produced — NOT criterion_breakdown, which is
            # Coach *evidence* about that document. Sourcing docs_summary
            # from criterion_breakdown delivered an empty doc to the phone
            # checkpoint + PLANNED_HANDOFF even after a successful PO run.
            role_output = getattr(result, "role_output", None) or {}
            criterion_breakdown = getattr(result, "criterion_breakdown", None) or {}
            po_output: dict[str, Any] = {
                "coach_evidence": {
                    "coach_score": coach_score,
                    "criterion_breakdown": criterion_breakdown,
                },
                "docs_summary": role_output,
                "structured_findings": [
                    str(f) for f in (getattr(result, "detection_findings", ()) or ())
                ],
                # Structured assumptions surfaced by the PO (best-effort — the
                # result shape carries them when the PO emits confidence-tagged
                # assumptions; empty otherwise, and the checkpoint degrades to a
                # no-assumptions prompt). This is the source the checkpoint
                # projects into details.summary.assumptions (TASK-SPL003F-001).
                "assumptions": _extract_assumptions(result),
                "degraded": outcome_value == "degraded",
                "reason": reason,
            }
            deps.store._record_event(
                correlation_id=correlation_id,
                stage_label="product_owner",
                status="approved",
                coach_score=coach_score,
                actor_identity="planning-driver",
                details_json=json.dumps({"po_output": po_output}, default=str),
            )
            logger.info(
                "planning driver: PRODUCT_OWNER %s for %s (coach_score=%s)",
                outcome_value,
                correlation_id,
                coach_score,
            )
            return True

        # soft_timeout / error → terminal failure
        self._fail(
            correlation_id,
            stage_label="product_owner",
            reason=f"PO dispatch {outcome_value}: {reason or 'no reason supplied'}",
        )
        # The owner reads the PLAIN name of the leg, never the enum member name
        # (stage-names ruling); the internal label stays on the durable row and
        # in the log line above.
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id} stopped at "
            f"{plain_stage_name('product_owner')} ({outcome_value}).",
            level="error",
        )
        return False

    def _absorb_product_docs_checkpoint(self, correlation_id: str) -> bool:
        """Record the brief-stage checkpoint as cleared BY ABSORPTION.

        Returns False when such a row already exists — the caller stops loudly
        rather than writing it again, because a second write would mean the
        chain is not reading the first one and the loop would never yield.

        The machine chain asks its one question later, in front of the spec
        digest — so the brief card never opens and this row stands in its place,
        in exactly the shape an approval writes (the planner reads any
        ``checkpoint_cleared`` row the same way, and force-labels it
        ``product_docs``). The actor identity names the reason so the durable
        record cannot be misread as a pause that was skipped: it was MOVED.

        Nothing here auto-approves anything. The run still carries exactly one
        mandatory human approval; it simply happens where the person can see
        what they are approving.
        """
        for event in self._deps.store.list_events(correlation_id):
            if (
                event["stage_label"] == _PRODUCT_DOCS_STAGE
                and event["status"] == "checkpoint_cleared"
            ):
                logger.error(
                    "planning driver: run %s already carries a cleared "
                    "brief-stage checkpoint but the chain asked to pause there "
                    "again — refusing to write a second row",
                    correlation_id,
                )
                return False
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_PRODUCT_DOCS_STAGE,
            status="checkpoint_cleared",
            actor_identity="planning-driver:absorbed-into-spec-review",
            details_json=json.dumps(
                {
                    "stage_label": _PRODUCT_DOCS_STAGE,
                    "outcome": "absorbed",
                    "absorbed_into": _DIGEST_REVIEW_STAGE,
                }
            ),
        )
        logger.info(
            "planning driver: run %s brief-stage checkpoint absorbed into the "
            "spec digest review — one pause, asked where the spec is",
            correlation_id,
        )
        return True

    async def _checkpoint(self, correlation_id: str, plan_run_id: str) -> bool:
        """Pause at the product docs checkpoint (DF-009: always pauses).

        Returns True when the PAUSED state reached durable storage (even
        if the subsequent publish failed — escalation rounds / rearm
        re-emit), False when the pause itself did not persist.
        """
        deps = self._deps
        try:
            await checkpoint_product_docs(
                plan_run_id=plan_run_id,
                feature_id=plan_run_id,
                repository=deps.repository,
                state_machine=deps.state_machine,
                publisher=deps.approval_publisher,
                second_opinion_provider=deps.second_opinion_provider,
                coach_evidence=self._latest_po_output(correlation_id).get(
                    "coach_evidence"
                ),
                clock=deps.clock,
            )
        except Exception:  # noqa: BLE001 — publish failure keeps the durable pause
            logger.exception(
                "planning driver: checkpoint raised for %s; verifying whether "
                "the pause reached durable state (DDR-007)",
                correlation_id,
            )
        row = deps.store.get_run(correlation_id)
        return row is not None and row["state"] == PlanningState.PAUSED.value

    async def _await_approval(
        self, row: Any, plan_run_id: str, *, needs_republish: bool
    ) -> str:
        """Structured wait on a PAUSED run until a decision or timeout.

        Returns ``"approved"`` (chain continues), ``"cancelled"``,
        ``"timed_out"`` or ``"externally_resolved"``.
        """
        deps = self._deps
        correlation_id = row["correlation_id"]
        cfg = deps.planning_config

        while True:
            row = deps.store.get_run(correlation_id)
            if row is None:  # pragma: no cover - defensive
                return "externally_resolved"
            state = PlanningState(row["state"])
            if state is not PlanningState.PAUSED:
                # A concurrent actor resolved the pause (approve via
                # another path, cancel, timeout) — surface it.
                if state is PlanningState.RUNNING:
                    return "approved"
                return "externally_resolved"

            if row["paused_at"] is None and row["escalated_at"] is None:
                # Corrupt/legacy PAUSED row with no anchor: stamp the
                # window start ONCE durably — recomputing "starts now" on
                # every iteration would wait forever (TASK-MP-012 review
                # finding).
                deps.store.update_escalation(
                    correlation_id=correlation_id,
                    paused_at=deps.clock().isoformat(),
                    expected_state=PlanningState.PAUSED,
                )
                row = deps.store.get_run(correlation_id) or row

            phase, remaining = self._phase_remaining(row, cfg)

            if remaining <= 0:
                if phase == 1 and cfg.escalation_approver:
                    outcome = await evaluate_escalation_phase(
                        store=deps.store,
                        correlation_id=correlation_id,
                        policy=self._policy(cfg),
                        clock=deps.clock,
                        publisher=None,  # persist re-target only; publish
                        plan_run_id=plan_run_id,  # happens AFTER arming below
                        feature_id=plan_run_id,
                    )
                    if outcome is EscalationOutcome.ESCALATED:
                        needs_republish = True
                    continue
                # Phase-2 ceiling (or no escalation approver configured):
                # durable TIMED_OUT via CAS from PAUSED.
                refused = deps.store.transition(
                    correlation_id=correlation_id,
                    to_state=PlanningState.TIMED_OUT,
                    actor_identity="planning-driver",
                    stage_label="planning-escalation-timeout",
                    error="approval wait ceiling reached",
                    expected_from_state=PlanningState.PAUSED,
                )
                if isinstance(refused, TransitionRefused):
                    continue  # a concurrent decision won; re-read state
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id} timed out awaiting approval.",
                    level="warning",
                )
                return "timed_out"

            expected_approver = row["expected_approver"]
            pending_request_id = row["pending_approval_request_id"]
            attempt_count = self._attempt_from(pending_request_id)

            armed: asyncio.Event = asyncio.Event()
            subscriber = deps.subscriber_factory(expected_approver, armed)
            wait_started = time.monotonic()
            wait_task = asyncio.create_task(
                subscriber.await_response(
                    plan_run_id,
                    stage_label=_PRODUCT_DOCS_STAGE,
                    attempt_count=attempt_count,
                    timeout_seconds=max(1, int(remaining)),
                )
            )
            try:
                await asyncio.wait_for(armed.wait(), timeout=_ARM_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(
                    "planning driver: response subscription failed to arm "
                    "for %s within %.0fs; retrying",
                    correlation_id,
                    _ARM_TIMEOUT_SECONDS,
                )
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 — surface the root cause
                    logger.exception(
                        "planning driver: response waiter failed before "
                        "arming for %s (root cause of the arm timeout)",
                        correlation_id,
                    )
                await asyncio.sleep(1.0)  # anti-spin: never tight-loop arming
                continue

            if needs_republish and pending_request_id:
                # Arm-before-post: the subscription is live, now re-emit
                # the persisted request_id VERBATIM (rearm / escalation /
                # defer rounds).
                await self._republish_pending(row, plan_run_id)
                needs_republish = False

            try:
                response = await wait_task
            except Exception:  # noqa: BLE001 — a waiter defect must not kill the run
                logger.exception(
                    "planning driver: response waiter raised for %s; retrying",
                    correlation_id,
                )
                await asyncio.sleep(1.0)
                continue
            if response is None:
                # Window expired — the durable anchors drive escalation /
                # timeout on the next iteration. Anti-spin: if the waiter
                # returned instantly (defective subscriber, empty fake
                # script) while wall-clock time remains, back off so a
                # broken wire cannot hot-loop the daemon.
                if (
                    time.monotonic() - wait_started < 1.0
                    and self._phase_remaining(
                        deps.store.get_run(correlation_id) or row, cfg
                    )[1]
                    > 0
                ):
                    await asyncio.sleep(1.0)
                continue

            # Stale-round guard: a late response to a superseded
            # request_id is ignored; the wait continues.
            current = deps.store.get_run(correlation_id)
            current_pending = (
                current["pending_approval_request_id"] if current else None
            )
            if current_pending and response.request_id != current_pending:
                logger.warning(
                    "planning driver: stale response request_id=%s for %s "
                    "(current=%s); ignoring",
                    response.request_id,
                    correlation_id,
                    current_pending,
                )
                continue

            outcome = await _dispatch_approval_response(
                response=response,
                repository=deps.repository,
                state_machine=deps.state_machine,
                clock=deps.clock,
                escalation_context=PlanningEscalationContext(
                    store=deps.store,
                    policy=self._policy(cfg),
                    # publisher=None: the defer/at-cap round is persisted by
                    # the escalation module but published HERE, after the
                    # next iteration's waiter is armed (arm-before-post).
                    publisher=None,
                    feature_id=plan_run_id,
                ),
            )

            if outcome in ("approved", "overridden"):
                deps.store._record_event(
                    correlation_id=correlation_id,
                    stage_label=_PRODUCT_DOCS_STAGE,
                    status="checkpoint_cleared",
                    actor_identity=response.decided_by,
                    details_json=json.dumps(
                        {"stage_label": _PRODUCT_DOCS_STAGE, "outcome": outcome}
                    ),
                )
                return "approved"
            if outcome == "revise":
                # Assumption-dialogue revision: assemble the EnrichmentBatch,
                # cap-3 → escalate, else re-invoke the PO statelessly. The
                # checkpoint is NOT cleared; the chain re-pauses next cycle.
                handled = await self._handle_revision(
                    correlation_id, plan_run_id, response
                )
                if handled == "escalated":
                    # Escalated to Rich — keep waiting on the escalated
                    # approver (re-emit the escalated request once armed).
                    needs_republish = True
                    continue
                if handled == "revising":
                    # RUNNING with a fresh PO output — hand back to drive()'s
                    # main loop, which re-checkpoints with the new cycle.
                    return "approved"
                # revision could not be applied (store/dispatch failure) — the
                # run stays PAUSED; the wait ceiling / rearm recovers it.
                return "externally_resolved"
            if outcome == "rejected":
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id} was rejected by "
                    f"{response.decided_by}.",
                    level="warning",
                )
                return "cancelled"
            if outcome == "deferred":
                # The new round's request_id is already persisted; re-emit
                # it once the next waiter is armed (arm-before-post).
                needs_republish = True
                continue
            # refused / unknown → keep waiting
            continue

    async def _handle_revision(
        self, correlation_id: str, plan_run_id: str, response: Any
    ) -> str:
        """Apply an assumption-dialogue revision (cap-3 → escalate, else re-invoke).

        Returns ``"escalated"`` (cap reached — escalated to Rich, run stays
        PAUSED), ``"revising"`` (RUNNING with a fresh PO output — drive()
        re-checkpoints) or ``"failed"`` (the revision could not be applied).
        """
        deps = self._deps
        dispositions = parse_dispositions(response)

        # Current dialogue cycle = 1 + durable count of recorded revisions.
        current_cycle = self._dialogue_cycle(correlation_id)

        # Cap-3: a revision that would open a 4th cycle escalates to Rich via
        # the existing escalation path (durable expected_approver re-target,
        # checkpoint_type=product_docs_escalated) instead of another round.
        if current_cycle >= CYCLE_CAP:
            logger.info(
                "planning driver: dialogue cap (%d) reached for %s; escalating "
                "instead of a %dth cycle",
                CYCLE_CAP,
                correlation_id,
                current_cycle + 1,
            )
            from forge.planning.escalation import escalate_planning_run

            await escalate_planning_run(
                store=deps.store,
                correlation_id=correlation_id,
                policy=self._policy(deps.planning_config),
                clock=deps.clock,
                publisher=None,  # arm-before-post: drive() re-emits once armed
                plan_run_id=plan_run_id,
                feature_id=plan_run_id,
            )
            return "escalated"

        # Assemble the EnrichmentBatch delta from the prior assumptions + the
        # human dispositions (forge assembles the delta; the PO does no
        # elicitation — propose-never-elicit).
        prior_assumptions = normalize_assumptions(
            self._latest_po_output(correlation_id).get("assumptions")
        )
        next_cycle = current_cycle + 1
        batch = assemble_enrichment_batch(
            correlation_id=correlation_id,
            cycle=next_cycle,
            prior_assumptions=prior_assumptions,
            dispositions=dispositions,
        )

        # Record the revision durably (increments the dialogue-cycle count so
        # the next checkpoint projects cycle=next_cycle) BEFORE re-dispatching.
        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=REVISION_STAGE_LABEL,
            status="REVISION",
            actor_identity=response.decided_by,
            details_json=json.dumps({"cycle": next_cycle, "enrichment_batch": batch}),
        )

        # PAUSED → RUNNING so the main loop re-dispatches the PO, then re-invoke
        # the PO statelessly with the assembled delta.
        try:
            await deps.state_machine.transition_to_running(build_id=plan_run_id)
            await deps.repository.mark_resumed(
                build_id=plan_run_id, stage_label=_PRODUCT_DOCS_STAGE
            )
        except Exception:  # noqa: BLE001 — a transition defect must not crash the run
            logger.exception(
                "planning driver: revise transition failed for %s", correlation_id
            )
            return "failed"

        ok = await self._dispatch_po(correlation_id, plan_run_id, enrichment=batch)
        return "revising" if ok else "failed"

    def _dialogue_cycle(self, correlation_id: str) -> int:
        """1-based dialogue cycle from the durable revision-event count.

        Delegates to :func:`revision.dialogue_cycle` — the same arithmetic the
        checkpoint projects, so the projected ``cycle`` and this cap gate can
        never diverge.
        """
        return dialogue_cycle(self._deps.store.list_events(correlation_id))

    async def _handoff(self, row: Any, correlation_id: str) -> None:
        """Execute the planned-handoff terminal (idempotent, RT-08)."""
        deps = self._deps
        po_output = self._latest_po_output(correlation_id)
        run_data: dict[str, Any] = {
            "correlation_id": correlation_id,
            "state": row["state"],
            "request_text": row["request_text"],
            "originating_user": row["originating_user"],
            "target_repo": row["target_repo"],
            "product_docs": po_output.get("docs_summary") or {},
        }

        handler = PlannedHandoffHandler(deps.planning_config, deps.git_runner)
        result = await handler.handle(run_data)

        if result.get("state") == PlanningState.PLANNED_HANDOFF.value:
            refused = deps.store.transition(
                correlation_id=correlation_id,
                to_state=PlanningState.PLANNED_HANDOFF,
                actor_identity="planning-driver",
                stage_label="planned-handoff",
                handoff_branch=result.get("handoff_branch"),
                handoff_path=result.get("handoff_path"),
            )
            if isinstance(refused, TransitionRefused):
                logger.warning(
                    "planning driver: PLANNED_HANDOFF transition refused for "
                    "%s (current=%s)",
                    correlation_id,
                    refused.current_state,
                )
                return
            payload = result.get("notification_payload") or {}
            message = payload.get("message", f"Planning complete for {correlation_id}.")
            command = payload.get("command")
            if command:
                message = f"{message} Next: `{command}`"
            await self._notify(correlation_id, message, level="info")
            logger.info(
                "planning driver: run %s reached PLANNED_HANDOFF (branch=%s)",
                correlation_id,
                result.get("handoff_branch"),
            )
            return

        reason = result.get("failure_reason", "handoff failed")
        self._fail(correlation_id, stage_label="planned-handoff", reason=reason)
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id} handoff failed: {reason}",
            level="error",
        )

    # ------------------------------------------------------------------ #
    # Target terminal (Lane B / Phase E1 — the machine chain after
    # PLANNED_HANDOFF). Gated on planning.target_terminal.enabled; every
    # method below is unreachable with the flag off.
    # ------------------------------------------------------------------ #

    def _target_terminal_enabled(self) -> bool:
        """True iff the ``planning.target_terminal.enabled`` flag is on."""
        tt = getattr(self._deps.planning_config, "target_terminal", None)
        return bool(getattr(tt, "enabled", False))

    async def _enter_target_terminal(self, row: Any, correlation_id: str) -> bool:
        """Write the handoff file (the 007 input) and transition RUNNING → FEATURE_SPEC.

        The flag-ON analogue of :meth:`_handoff`'s branch write: it commits the
        SAME ``feature_spec_inputs/{cid}.md`` the fallback terminal would (so
        the 007 input is byte-identical to the fallback's handoff), but enters
        the machine chain instead of terminating. Returns True on success
        (drive() re-reads FEATURE_SPEC), False on a loud terminal failure.
        """
        deps = self._deps
        resolved = await self._resolve_repo(row, correlation_id, stage_label="target-terminal-enter")
        if resolved is None:
            return False
        target_repo, repo_path = resolved
        branch = f"planning/{correlation_id}"
        handoff_path = f"feature_spec_inputs/{correlation_id}.md"
        content = build_feature_spec_input_content(self._run_data(row, correlation_id))

        try:
            result = await deps.git_runner.prepare_branch_and_write(
                repo_path=repo_path,
                branch=branch,
                file_path=handoff_path,
                content=content,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary, never crash the run
            return await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                f"handoff write raised {type(exc).__name__}: {exc}",
            )
        if result.status == "failed":
            return await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                f"handoff write failed: {result.stderr}",
            )

        refused = deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.FEATURE_SPEC,
            actor_identity="planning-driver",
            stage_label="target-terminal-enter",
            expected_from_state=PlanningState.RUNNING,
            details_json=json.dumps(
                {
                    "target_repo": target_repo,
                    "repo_path": repo_path,
                    "branch": branch,
                    "handoff_path": handoff_path,
                }
            ),
        )
        if isinstance(refused, TransitionRefused):
            logger.warning(
                "planning driver: RUNNING→FEATURE_SPEC refused for %s (current=%s)",
                correlation_id,
                refused.current_state,
            )
            return False
        logger.info(
            "planning driver: run %s entered the target terminal (branch=%s)",
            correlation_id,
            branch,
        )
        return True

    async def _feature_spec_leg(self, row: Any, correlation_id: str) -> bool:
        """FEATURE_SPEC leg: write the spec, show it to a person, then advance.

        The chain's ONE pause lives here. The brief-stage checkpoint is absorbed
        (see :meth:`_absorb_product_docs_checkpoint`) and its question moves to
        the only place there is something a person can actually check: right
        after the spec is written, in front of the SPEC DIGEST — one plain
        sentence per worked example, mechanically checked against the examples
        themselves.

        The sequence, and the order matters because getting it wrong makes the
        door un-restartable:

        1. a durable ``feature-spec`` approved row → already said yes; advance;
        2. a durable ``feature-spec-draft`` row → the spec is written and
           committed but unanswered; skip straight to the door and re-open it
           with the persisted card VERBATIM (never re-dispatch — that would
           rewrite the spec underneath a card the owner is still reading);
        3. otherwise dispatch the spec-writer, commit the spec, and record the
           draft row;
        4. open the digest door;
        5. yes → the ``feature-spec`` approved row (unchanged in shape) and on
           to the plan leg. A NOTE → the note is recorded, the draft is
           superseded, and the spec-writer is re-invoked with the note VERBATIM
           and the prior artifact set. Anything else → an honest terminal.

        Returns True to keep driving (now FEATURE_PLAN), False on a loud
        terminal failure.
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _FEATURE_SPEC_STAGE):
            return self._advance_after_spec(correlation_id)

        if deps.dispatch_feature_spec is None or deps.normalize_feature_spec is None:
            return await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                "target terminal ON but the spec leg collaborators "
                "(dispatch_feature_spec / normalize_feature_spec) are not wired",
            )
        resolved = await self._resolve_repo(row, correlation_id, stage_label=_FEATURE_SPEC_STAGE)
        if resolved is None:
            return False
        target_repo, repo_path = resolved
        branch = f"planning/{correlation_id}"
        plan_run_id = f"plan-{correlation_id}"

        # The owner's notes so far, oldest first. They are the revision channel
        # AND, past the bound, the receipt the escalation quotes back.
        notes = self._spec_review_notes(correlation_id)
        draft = self._open_spec_draft(correlation_id)

        while True:
            if draft is None:
                drafted = await self._draft_spec(
                    row,
                    correlation_id,
                    target_repo=target_repo,
                    repo_path=repo_path,
                    branch=branch,
                    plan_run_id=plan_run_id,
                    notes=notes,
                )
                if drafted is None:
                    return False  # the failure is already loud and terminal
                draft = drafted

            # THE ONE PAUSE. A run configured to skip the card on a thin
            # feature skips it here — mechanically decidable, no judgement.
            skip = self._digest_review_skip_reason(draft)
            if skip is not None:
                logger.info(
                    "planning driver: run %s spec digest review skipped (%s)",
                    correlation_id,
                    skip,
                )
                deps.store._record_event(
                    correlation_id=correlation_id,
                    stage_label=_DIGEST_REVIEW_STAGE,
                    status="skipped",
                    actor_identity="planning-driver",
                    details_json=json.dumps({"digest_review": {"skipped": skip}}),
                )
                return self._record_spec_approved(correlation_id, draft, branch)

            answer = await self._spec_digest_review_door(row, correlation_id, draft)

            if answer.outcome == "approved":
                return self._record_spec_approved(
                    correlation_id, draft, branch, answer=answer
                )

            if answer.outcome == "cancelled":
                return await self._cancel_run_at_digest_door(correlation_id, answer)

            if answer.outcome == "revise":
                note = str(answer.notes or "")
                notes = [*notes, note]
                deps.store._record_event(
                    correlation_id=correlation_id,
                    stage_label=_SPEC_DRAFT_STAGE,
                    status="superseded",
                    actor_identity=answer.decided_by or "planning-driver",
                    details_json=json.dumps(
                        {"spec_draft": {"superseded_by_note": note}}
                    ),
                )
                if len(notes) >= CYCLE_CAP:
                    # PAST THE BOUND — loud, and it says what was asked for.
                    # Three cards is the whole budget (the first plus two
                    # rewrites); a fourth would be the machine insisting it can
                    # get there when three rounds say it cannot.
                    return await self._escalate_spec_review(correlation_id, notes)
                logger.info(
                    "planning driver: run %s spec digest returned with a note "
                    "(round %d of %d) — rewriting the spec from it",
                    correlation_id,
                    len(notes) + 1,
                    CYCLE_CAP,
                )
                draft = None
                continue

            # rejected with no note / deferred / timed out / undeliverable —
            # each names itself, to the owner and on the durable row.
            return await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"spec digest review closed as {answer.outcome} — the spec was "
                f"not approved, so nothing was planned and nothing was built",
                owner_message=(
                    f"Planning run {correlation_id} stopped at "
                    f"{plain_stage_name(_DIGEST_REVIEW_STAGE)}. "
                    f"{_DIGEST_DOOR_TERMINAL_SUFFIX.get(answer.outcome, '')} "
                    "Nothing was built."
                ).strip(),
            )

    async def _cancel_run_at_digest_door(
        self, correlation_id: str, answer: "_DoorAnswer"
    ) -> bool:
        """The owner typed "reject" at the spec-review card: end the run CANCELLED.

        Their own stop, not a machine failure — so the run takes the CANCELLED
        terminal (the same terminal a reject takes at the product-docs
        checkpoint), with whatever followed the word kept as the reason, and
        the Slack line says plainly that the run is over and a fresh sentence
        starts a new one.
        """
        _starts, reason = _reject_word_split(str(answer.notes or ""))
        recorded_reason = reason or "no reason was given beyond the reject itself"
        refused = self._deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.CANCELLED,
            actor_identity=answer.decided_by or "planning-driver",
            stage_label=_DIGEST_REVIEW_STAGE,
            error=recorded_reason,
            details_json=json.dumps(
                {
                    "digest_review": {
                        "outcome": "cancelled",
                        "reason": recorded_reason,
                        "decided_by": answer.decided_by,
                    }
                }
            ),
        )
        if isinstance(refused, TransitionRefused):
            logger.warning(
                "planning driver: CANCELLED transition refused for %s "
                "(current=%s, reason=%s)",
                correlation_id,
                refused.current_state,
                recorded_reason,
            )
        logger.info(
            "planning driver: run %s cancelled at the spec digest review by "
            "%s: %s",
            correlation_id,
            answer.decided_by,
            recorded_reason,
        )
        await self._notify(
            correlation_id,
            (
                f"Planning run {correlation_id} is cancelled — you said "
                "reject on the spec review"
                + (f": {reason}" if reason else "")
                + ". Nothing was built. Send a fresh sentence in the "
                "planning channel whenever you want to start again."
            ),
            level="info",
        )
        return False

    async def _draft_spec(
        self,
        row: Any,
        correlation_id: str,
        *,
        target_repo: str,
        repo_path: str,
        branch: str,
        plan_run_id: str,
        notes: list[str],
    ) -> dict[str, Any] | None:
        """Dispatch the spec-writer, commit the spec, record the DRAFT row.

        ``notes`` carries the owner's plain-English notes from earlier rounds;
        the newest is threaded back as ``validate_feedback`` VERBATIM together
        with the prior artifact set as ``revision_of`` — discharging the C5
        channel that has been vocabulary since it was designed. A first-round
        dispatch passes neither and is byte-identical to the call that shipped.

        Returns the draft record (the same dict the door and the approved row
        read), or ``None`` when the leg has already failed loudly.
        """
        deps = self._deps
        spec_input = build_feature_spec_input_content(
            self._run_data(row, correlation_id)
        )
        prior = (
            await self._prior_spec_artifacts(
                correlation_id, repo_path=repo_path, branch=branch
            )
            if notes
            else None
        )

        try:
            result = await deps.dispatch_feature_spec(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                spec_input=spec_input,
                # C5, discharged: the owner's own words drive the rewrite. They
                # go through VERBATIM — never summarised, never reworded by an
                # intermediate step. He said it once; the machine reads what he
                # said.
                revision_of=prior,
                validate_feedback=notes[-1] if notes else None,
            )
        except Exception as exc:  # noqa: BLE001 — dispatch boundary
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"007 dispatch raised {type(exc).__name__}: {exc}",
            )
            return None
        ok, reason = self._dispatch_ok(result)
        if not ok:
            await self._fail_leg(
                correlation_id, _FEATURE_SPEC_STAGE, f"007 dispatch {reason}"
            )
            return None

        role_output = self._role_output_of(result)
        slug = self._slug_of(role_output, correlation_id)
        files = self._spec_triple_files(role_output, slug)
        if not files:
            # A DEGRADED dispatch means no specialist was resolvable and NOTHING RAN — reporting
            # that as "invalid artifacts" blames the model for output it was never asked to
            # produce. Measured 2026-08-24: a missing NATS credential made the PO specialist
            # unreachable, forge logged `dispatch.degraded ... no_specialist_resolvable` one line
            # earlier, and the operator-facing failure still read "007 returned no spec contract
            # (invalid artifacts)" — pointing every debugging instinct at the wrong layer.
            # `_dispatch_ok` deliberately treats `degraded` as ok (it can legitimately carry
            # output elsewhere), so the distinction has to be drawn HERE, where the emptiness is.
            outcome_value = str(
                getattr(getattr(result, "outcome", None), "value", "") or ""
            ).lower()
            if outcome_value == "degraded":
                degraded_reason = (
                    getattr(result, "reason", None) or "no specialist resolvable"
                )
                await self._fail_leg(
                    correlation_id,
                    _FEATURE_SPEC_STAGE,
                    f"007 never ran — dispatch degraded ({degraded_reason}); "
                    "no specialist was reachable, so no spec was produced",
                )
                return None
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                "007 returned no spec contract (invalid artifacts)",
            )
            return None

        # VALIDATION CHANNEL (C5): the 007 native map ships a validation.json
        # self-check alongside the artifacts. TWO POSTURES, each honest about
        # what it is.
        #
        # Every gate but one is ADVISORY: the B2 spec of record's REAL oracles
        # run next (the normalizer + ``guardkit feature validate``), and a
        # self-flagged spec that passes them is good enough by the estate's own
        # bar (the gold hermetic run shipped accepted:false on a minor count
        # note while the coach scored 0.985). So those are surfaced LOUDLY,
        # verbatim, and the oracles decide.
        #
        # The DIGEST gate is the exception, and the reason is structural: there
        # is no oracle downstream of it. The only thing after the digest is a
        # person's eyes, and this check is the whole reason that read can be
        # trusted to be complete. Asking someone to approve a digest we cannot
        # prove is a compression is asking them to approve a lie — so a digest
        # error STOPS the run here.
        spec_val_errors = self._validation_failures(role_output)
        digest_errors = [e for e in spec_val_errors if e.startswith(DIGEST_ERROR_PREFIX)]
        other_errors = [
            e for e in spec_val_errors if not e.startswith(DIGEST_ERROR_PREFIX)
        ]
        if other_errors:
            logger.warning(
                "007 validation.json self-check reported failures for %s "
                "(ADVISORY — proceeding to the normalizer/validate oracles): %s",
                correlation_id,
                "; ".join(other_errors),
            )
        if digest_errors:
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                "the spec-writer's own digest check failed and the digest is "
                "what a person reads instead of the spec: "
                + "; ".join(digest_errors),
                owner_message=(
                    f"Planning run {correlation_id} stopped at "
                    f"{plain_stage_name(_FEATURE_SPEC_STAGE)}: the plain-language "
                    "summary of the spec did not match the spec, so there was "
                    "nothing safe to show you. Nothing was built."
                ),
            )
            return None

        feature_rel = self._feature_file_rel(files)
        normalize = deps.normalize_feature_spec

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            if feature_rel is None:
                return PreCommitResult(
                    ok=False, detail="spec contract has no .feature file to normalize"
                )
            outcome = await normalize(worktree, feature_rel)
            return PreCommitResult(ok=outcome.ok, detail=outcome.detail)

        try:
            gitres = await deps.git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=f"planning: feature spec for {correlation_id} (Lane B 007)",
                pre_commit=_pre_commit,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"spec write raised {type(exc).__name__}: {exc}",
            )
            return None
        if gitres.status == "failed":
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"spec write / normalizer failed: {gitres.stderr}",
            )
            return None

        # THE DIGEST IS RE-PROVEN AGAINST THE COMMITTED SPEC. The spec-writer
        # checked its digest against its own text; the normalizer then rewrote
        # the ``.feature`` IN PLACE at pre-commit (collapsing wrapped steps,
        # commenting out box-drawing dividers), and the collapsed file is "the
        # committed content of record" the build is checked against. A digest
        # proven only against the pre-normalization text is a digest about an
        # artifact nobody builds from — so the same pure, model-free check runs
        # again here, on what actually landed on the branch.
        #
        # CAPTURE the 007 feature-grain pass-bar SEED first (a tolerated extra
        # in the native map, NEVER a committed spec-triple file): it is
        # persisted on the durable draft event so it survives to plan-commit —
        # where it is specialised into per-task bars (B4 round-19) — AND it
        # carries the sign-in flag the card folds in, so the run pauses once
        # rather than twice. ``None`` when this reply shipped no seed (an older
        # specialist); the plan-commit leg fails loudly on that rather than
        # silently skipping the bars the B2 gate demands.
        pass_bar_seed = self._capture_pass_bar_seed(role_output)
        seed: dict[str, Any] | None = None
        if pass_bar_seed:
            try:
                parsed_seed = yaml.safe_load(pass_bar_seed)
            except yaml.YAMLError:
                parsed_seed = None
            if isinstance(parsed_seed, Mapping):
                seed = dict(parsed_seed)

        digest_text = self._capture_digest(role_output, files)
        proof = await self._prove_digest_against_branch(
            correlation_id,
            target_repo=target_repo,
            repo_path=repo_path,
            branch=branch,
            files=files,
            digest_text=digest_text,
            slug=slug,
            seed=seed,
        )
        if proof is None:
            return None
        digest_obj, card = proof

        # DID THE NOTE ACTUALLY CHANGE ANYTHING? On a rewrite round the owner
        # has already read one of these lists and said what was wrong with it.
        # On 2026-09-05 the rewrite came back with the same six sentences and
        # the second card said nothing about it, so a note that did nothing
        # looked exactly like a note that worked. The card now says which it
        # was, and it says so on the card the owner is about to read — the
        # comparison is made HERE, before the draft row is written, so a
        # restart replays the same words.
        rewrite: dict[str, Any] | None = None
        if notes:
            previous = self._previous_digest_card(correlation_id)
            if previous is not None:
                note = str(notes[-1])
                if _cards_say_the_same_thing(previous, card):
                    rewrite = {"repeat": True, "note": note, "changes": ""}
                    card = dict(card)
                    card["what_happened"] = _SAME_LIST_CARD_TEXT.format(note=note)
                else:
                    changes = _plain_card_changes(previous, card)
                    rewrite = {"repeat": False, "note": note, "changes": changes}
                    card = dict(card)
                    card["what_happened"] = (
                        f"{card.get('what_happened', '')} "
                        + _CHANGED_LIST_CARD_LINE.format(changes=changes)
                    ).strip()

        draft: dict[str, Any] = {
            "slug": slug,
            "spec_files": sorted(files),
            "target_repo": target_repo,
            "repo_path": repo_path,
            "branch": branch,
            "sha": gitres.sha,
            "pass_bar_seed": pass_bar_seed,
            "digest": digest_text,
            "card": card,
            "scenario_count": len(digest_obj.get("scenarios") or []),
            "assumption_count": len(digest_obj.get("assumptions") or []),
            "cycle": len(notes) + 1,
        }
        if rewrite is not None:
            # Persisted on the draft row, not on the card: the door's opening
            # notification reads it, and a restart that re-opens the draft
            # re-reads it rather than re-deriving it from a branch that has
            # moved on. A renderer never sees it.
            draft["rewrite"] = rewrite
        # Status ``drafted``, deliberately NOT ``approved``: this row is the
        # door's restart sentinel, never the leg's.
        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_SPEC_DRAFT_STAGE,
            status="drafted",
            actor_identity="planning-driver",
            details_json=json.dumps({"spec_draft": draft}),
        )
        logger.info(
            "planning driver: run %s feature-spec drafted (slug=%s, %d files, "
            "%d worked example(s), pass-bar seed %s) — showing the digest",
            correlation_id,
            slug,
            len(files),
            draft["scenario_count"],
            "captured" if pass_bar_seed is not None else "ABSENT",
        )
        return draft

    async def _prove_digest_against_branch(
        self,
        correlation_id: str,
        *,
        target_repo: str | None = None,
        repo_path: str,
        branch: str,
        files: Mapping[str, str],
        digest_text: str | None,
        slug: str,
        seed: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Re-run the digest check against the COMMITTED spec, then build the card.

        Returns ``(digest, card)`` or ``None`` when the leg has already failed
        loudly. A missing digest is a failure, not a degrade: the run must never
        publish an unproven digest and must never fall back to a "summary
        unavailable" card — that posture is right for a supplementary summary
        and wrong for the thing an approval rests on.
        """
        if not digest_text or not str(digest_text).strip():
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                "the 007 reply shipped no spec digest (<slug>_digest.yaml); "
                "there is no checked plain-language summary to show, and an "
                "unchecked one must never be shown in its place",
                owner_message=(
                    f"Planning run {correlation_id} stopped at "
                    f"{plain_stage_name(_FEATURE_SPEC_STAGE)}: the spec-writer "
                    "produced no plain-language summary to show you, so there "
                    "was nothing you could safely say yes to. Nothing was built."
                ),
            )
            return None

        try:
            digest_obj = yaml.safe_load(str(digest_text))
        except yaml.YAMLError as exc:
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"the spec digest is not parseable YAML: {exc}",
            )
            return None

        feature_rel = self._feature_file_rel(files)
        assumptions_rel = next(
            (rel for rel in files if rel.endswith(_SPEC_ASSUMPTIONS_SUFFIX)), None
        )
        committed_feature: str | None = None
        committed_assumptions: str | None = None
        if feature_rel:
            committed_feature = await self._deps.git_runner.read_file_from_branch(
                repo_path=repo_path, branch=branch, file_path=feature_rel
            )
        if assumptions_rel:
            committed_assumptions = await self._deps.git_runner.read_file_from_branch(
                repo_path=repo_path, branch=branch, file_path=assumptions_rel
            )
        if committed_feature is None:
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                "could not read the committed .feature back off "
                f"{branch} to re-prove the spec digest against it",
            )
            return None

        manifest: dict[str, Any] = {}
        if committed_assumptions:
            try:
                parsed = yaml.safe_load(committed_assumptions)
            except yaml.YAMLError:
                parsed = None
            if isinstance(parsed, Mapping):
                manifest = dict(parsed)

        errors = check_digest_consistency(
            digest_obj, committed_feature, manifest, slug
        )
        if errors:
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                "the spec digest does not match the committed spec: "
                + "; ".join(errors),
                owner_message=(
                    f"Planning run {correlation_id} stopped at "
                    f"{plain_stage_name(_FEATURE_SPEC_STAGE)}: the plain-language "
                    "summary of the spec did not match the spec it was supposed "
                    "to summarise, so there was nothing safe to show you. "
                    "Nothing was built."
                ),
            )
            return None

        card = self._digest_review_card(
            correlation_id,
            digest_obj=digest_obj if isinstance(digest_obj, Mapping) else {},
            feature_text=committed_feature,
            seed=seed,
            target_repo=target_repo,
        )
        return dict(digest_obj) if isinstance(digest_obj, dict) else {}, card

    def _record_spec_approved(
        self,
        correlation_id: str,
        draft: Mapping[str, Any],
        branch: str,
        *,
        answer: "_DoorAnswer | None" = None,
    ) -> bool:
        """Write the leg's durable ``approved`` row and advance to the plan leg.

        The row keeps the shape every downstream leg already reads (slug, files,
        repo, branch, sha, pass-bar seed) and ADDS the owner's act: who said yes
        and, when the sign-in question rode the same card, that they answered it.
        """
        record: dict[str, Any] = {
            "slug": draft.get("slug"),
            "spec_files": list(draft.get("spec_files") or []),
            "target_repo": draft.get("target_repo"),
            "repo_path": draft.get("repo_path"),
            "branch": draft.get("branch") or branch,
            "sha": draft.get("sha"),
            "pass_bar_seed": draft.get("pass_bar_seed"),
            # Carried from the draft so the gate leg can read the digest's
            # OPTIONAL endpoint field: the spec author states the method and
            # path outright, which beats re-deriving them from criterion prose.
            # Absent on the draft (no digest in the reply) simply stores None,
            # and the gate leg falls back to the prose regex exactly as before.
            "digest": draft.get("digest"),
        }
        if answer is not None:
            # The one tap answered the sign-in question too, when the card
            # carried it — the pass-bars leg reads this instead of opening a
            # second door an hour later with no spec attached. All THREE
            # answers are recorded, not just the yes: "there IS a sign-in" is
            # the answer the 2026-07-31 attended-registration guarantee exists
            # for, and a run that could not record it could not reach it.
            sign_in = self._sign_in_answer(draft, answer)
            record["spec_review"] = {
                "decided_by": answer.decided_by,
                "request_id": answer.request_id,
                "cycle": draft.get("cycle"),
                **({"sign_in_answer": sign_in} if sign_in else {}),
                # The pre-existing key, kept BYTE-IDENTICAL in meaning: present
                # and true exactly when the owner confirmed there is no
                # sign-in. A run already in flight when this landed is read by
                # the same fallback in :meth:`_spec_review_auth_answer`.
                **({"auth_confirmed": True} if sign_in == "confirmed" else {}),
            }
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_FEATURE_SPEC_STAGE,
            status="approved",
            actor_identity=(answer.decided_by if answer else None) or "planning-driver",
            details_json=json.dumps(record),
        )
        logger.info(
            "planning driver: run %s feature-spec approved (slug=%s, %d files)",
            correlation_id,
            record["slug"],
            len(record["spec_files"]),
        )
        return self._advance_after_spec(correlation_id)

    def _advance_after_spec(self, correlation_id: str) -> bool:
        """Transition FEATURE_SPEC → FEATURE_PLAN (idempotent-resume safe)."""
        refused = self._deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.FEATURE_PLAN,
            actor_identity="planning-driver",
            stage_label="feature-spec-complete",
            expected_from_state=PlanningState.FEATURE_SPEC,
        )
        if isinstance(refused, TransitionRefused):
            # A concurrent driver may already have advanced it — treat
            # FEATURE_PLAN (or a later target-terminal state) as success.
            current = self._deps.store.get_run(correlation_id)
            if current and PlanningState(current["state"]) in {
                PlanningState.FEATURE_PLAN,
                PlanningState.BUILD_QUEUED,
            }:
                return True
            logger.warning(
                "planning driver: FEATURE_SPEC→FEATURE_PLAN refused for %s (current=%s)",
                correlation_id,
                refused.current_state,
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    # The spec digest review — the machine chain's ONE pause
    # ------------------------------------------------------------------ #

    def _open_spec_draft(self, correlation_id: str) -> dict[str, Any] | None:
        """The spec draft still waiting for an answer, or ``None``.

        A draft is OPEN exactly when the LAST ``feature-spec-draft`` row is a
        ``drafted`` one — a ``superseded`` row means the owner sent a note and
        the spec is being rewritten, so there is nothing live to re-open.
        """
        latest: dict[str, Any] | None = None
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] != _SPEC_DRAFT_STAGE:
                continue
            if event["status"] != "drafted":
                latest = None
                continue
            try:
                details = json.loads(event["details_json"] or "{}") or {}
            except (json.JSONDecodeError, ValueError):
                latest = None
                continue
            record = details.get("spec_draft")
            latest = dict(record) if isinstance(record, Mapping) else None
        return latest

    def _previous_digest_card(self, correlation_id: str) -> dict[str, Any] | None:
        """The card the owner read LAST ROUND, or ``None`` on the first round.

        Read off the durable draft rows rather than held in memory, which is
        what makes it right after a restart: the row for round n-1 is still
        there whether or not this process wrote it.
        """
        latest: dict[str, Any] | None = None
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] != _SPEC_DRAFT_STAGE:
                continue
            try:
                details = json.loads(event["details_json"] or "{}") or {}
            except (json.JSONDecodeError, ValueError):
                continue
            record = details.get("spec_draft")
            if isinstance(record, Mapping) and isinstance(record.get("card"), Mapping):
                latest = dict(record["card"])
        return latest

    @staticmethod
    def _sign_in_answer(
        draft: Mapping[str, Any], answer: "_DoorAnswer"
    ) -> str | None:
        """What the owner said about the sign-in question, or ``None``.

        ``None`` means the question was never asked — the card carried no
        sign-in line, because the spec's own flag was down when it was
        rendered. Otherwise one of the sign-in door's OWN outcome words, so the
        pass-bars leg's terminal reads identically whichever door answered:

        * ``confirmed`` — agreed there is no sign-in. This is also what an
          answer that says nothing about this one item means: the 2026-08-14
          §2.6 ruling is that saying yes to the spec confirms it, and that
          ruling is unchanged here;
        * ``rejected`` — disagreed: this DOES involve signing in, so the
          quality checklist must be registered attended;
        * ``deferred`` — an answer that decided nothing (set aside, or edited
          rather than decided). Never read as agreement: the run stops and
          names it.

        Read from the response's per-item ``dispositions`` — a value, not
        prose. Nothing here parses a note.
        """
        if not (draft.get("card") or {}).get("sign_in_check"):
            return None
        given = (answer.item_answers or {}).get(_SIGN_IN_ASSUMPTION_ID)
        if given is None:
            return "confirmed"
        if given == "accepted":
            return "confirmed"
        if given == "rejected":
            return "rejected"
        return "deferred"

    def _spec_review_auth_answer(
        self, correlation_id: str
    ) -> dict[str, Any] | None:
        """The sign-in answer given on the spec digest card, or ``None``.

        Present exactly when the card carried the sign-in question (the spec's
        own flag was up when it was rendered) and the owner ended the review.
        The pass-bars leg reads this instead of opening a second door, which is
        what makes the run pause ONCE.

        Shaped like the sign-in door's own record — same ``outcome`` vocabulary,
        so the leg's receipt and its terminal read the same either way. A reader
        of the durable log should not have to know which door answered the
        question, only that a person did, who, and what they said.

        ``auth_confirmed`` is the pre-thaw key and is still honoured, so a run
        already in flight when the three-answer record landed reads correctly.
        """
        review = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE).get(
            "spec_review"
        )
        if not isinstance(review, Mapping):
            return None
        outcome = str(review.get("sign_in_answer") or "")
        if not outcome and review.get("auth_confirmed"):
            outcome = "confirmed"
        if not outcome:
            return None
        return {
            "outcome": outcome,
            "decided_by": review.get("decided_by"),
            "request_id": review.get("request_id"),
            "answered_on": "the spec digest card",
        }

    def _spec_review_notes(self, correlation_id: str) -> list[str]:
        """Every note the owner has sent about this spec, oldest first."""
        notes: list[str] = []
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] != _DIGEST_REVIEW_STAGE:
                continue
            if event["status"] != "revise":
                continue
            try:
                details = json.loads(event["details_json"] or "{}") or {}
            except (json.JSONDecodeError, ValueError):
                continue
            record = details.get("digest_review")
            if isinstance(record, Mapping) and record.get("notes"):
                notes.append(str(record["notes"]))
        return notes

    async def _prior_spec_artifacts(
        self, correlation_id: str, *, repo_path: str, branch: str
    ) -> dict[str, str] | None:
        """The artifact set of the spec being revised, read off the branch.

        Threaded to the spec-writer as ``revision_of`` so the rewrite starts
        from what it actually wrote, not from a paraphrase of it. Read off the
        branch rather than carried in memory, which is what makes it correct on
        an idempotent re-drive. ``None`` when nothing readable is there.
        """
        draft = None
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] != _SPEC_DRAFT_STAGE:
                continue
            try:
                details = json.loads(event["details_json"] or "{}") or {}
            except (json.JSONDecodeError, ValueError):
                continue
            record = details.get("spec_draft")
            if isinstance(record, Mapping) and record.get("spec_files"):
                draft = dict(record)
        if draft is None:
            return None
        prior: dict[str, str] = {}
        for rel in draft.get("spec_files") or []:
            content = await self._deps.git_runner.read_file_from_branch(
                repo_path=repo_path, branch=branch, file_path=str(rel)
            )
            if content is not None:
                prior[str(rel).rsplit("/", 1)[-1]] = content
        return prior or None

    def _digest_review_skip_reason(self, draft: Mapping[str, Any]) -> str | None:
        """Why this run may skip the digest card, or ``None`` (it always asks).

        The default is ALWAYS ASK, and that is the recommendation: a feature
        with no assumptions still has worked examples, and it is the examples —
        not the assumptions — that say what will be built. Auto-approving them
        would mean the machine can specify and queue a build no person ever saw,
        which is the exact hole this pause exists to close.

        The alternative is built because a binding rule says a person's taps go
        DOWN and never up, and on a thin feature the brief card used to
        auto-approve itself. Turning ``always_ask`` off skips the card ONLY when
        the spec has no assumptions AND no more than ``skip_max_scenarios``
        worked examples — mechanically decidable, no judgement, and recorded
        durably when it happens. One setting, both paths tested, so the ruling
        costs a value in a config file rather than a rebuild.
        """
        cfg = self._deps.planning_config.digest_review
        if cfg.always_ask:
            return None
        if (draft.get("card") or {}).get("sign_in_check"):
            # A flagged feature is never thin enough to skip: the card is
            # carrying a question only a person can answer, and skipping it
            # would push that question onto a later door — which is the second
            # pause this whole design exists to remove.
            return None
        scenarios = int(draft.get("scenario_count") or 0)
        assumptions = int(draft.get("assumption_count") or 0)
        if assumptions == 0 and scenarios <= int(cfg.skip_max_scenarios):
            return (
                f"the spec makes no assumptions and has {scenarios} worked "
                f"example(s), and this run is set not to ask about thin features"
            )
        return None

    async def _spec_digest_review_door(
        self, row: Any, correlation_id: str, draft: Mapping[str, Any]
    ) -> "_DoorAnswer":
        """Show the spec digest and wait for the owner's word.

        A THIN CALLER of :meth:`_inline_confirmation_door` — the same mechanism
        the sign-in door rides, so the two can never behave differently.

        The five answers, and what each means:

        * approve / override → ``approved``: write the plan and the quality
          checklist, then come back for the go-ahead. Nothing is built yet.
        * reject WITH a note → ``revise``: the machine rewrites the spec from
          the note and comes back with a fresh digest. ``reject`` is the wire's
          own literal and the door branches on it here rather than dispatching
          it, so a note does not by itself mean "cancel this run" the way a
          reject does at the product-docs checkpoint.
        * a note whose FIRST WORD is "reject" (any capitalisation, with or
          without more words) → ``cancelled``: the owner is calling the run
          off, not redrafting. The run ends CANCELLED and whatever followed
          the word is kept as their reason.
        * reject with NO note → ``rejected``: there is nothing to rewrite from,
          so the run stops and says so. Only reachable from a command line: the
          note box on the card is required.
        * anything else (a "later") → ``deferred``: an answer that decided
          nothing is still an ANSWER, so the run stops and names it rather than
          reporting that nobody replied.

        WHEN THE CARD ALSO CARRIED THE SIGN-IN QUESTION, that question is
        answered SEPARATELY, on the same response, in the wire's per-item
        ``dispositions`` field (:data:`_SIGN_IN_ASSUMPTION_ID`) — never by
        reading the note. The two channels do not overlap: the note says what
        the SPEC should say, the item answer says whether there is a sign-in.
        The item answer is read only on the round that ENDS the review (an
        approve); on a revise round the spec is about to change underneath it,
        so the question is re-asked on the fresh card rather than carried
        forward against a spec the owner has not seen. It is still recorded on
        the door's durable row either way.
        """
        card = dict(draft.get("card") or {})

        def _rehydrate(persisted: Mapping[str, Any], wait_seconds: int) -> dict[str, Any]:
            # The persisted card is replayed VERBATIM after a restart — the
            # owner's card is still in their Slack and a re-render off
            # possibly-drifted source would not be the same card.
            stored = persisted.get("card")
            return dict(stored) if isinstance(stored, Mapping) else card

        def _decide(response: Any) -> str:
            if response.decision in ("approve", "override"):
                return "approved"
            if response.decision == "reject":
                note = str(getattr(response, "notes", "") or "").strip()
                if not note:
                    return "rejected"
                # A typed reply whose FIRST WORD is "reject" is the owner
                # calling the run off, not a revision note. Every typed note
                # rides the wire as decision="reject", so the word is the only
                # signal (2026-08-24: "reject I typed the wrong sentence"
                # became a revision note and the machine redrafted the same
                # bad sentence).
                starts_with_reject, _reason = _reject_word_split(note)
                if starts_with_reject:
                    return "cancelled"
                return "revise"
            return "deferred"

        async def _closed(answer: "_DoorAnswer") -> None:
            if answer.outcome == "approved":
                sign_in = self._sign_in_answer(draft, answer)
                if sign_in is not None and sign_in != "confirmed":
                    # They said yes to the spec AND told us the sign-in is real
                    # (or set that question aside). The run does not quietly
                    # carry on as if they had not: say so NOW, at the tap, so
                    # nobody learns an hour later that the run was always going
                    # to stop.
                    await self._notify(
                        correlation_id,
                        f"Planning run {correlation_id}: {_person_words(answer.decided_by, answer.decided_by_name)} "
                        "said yes to the spec, and did not confirm that this "
                        "feature is free of signing in. The machine will write "
                        "the task plan and then stop, so a person can register "
                        "the quality checklist. Nothing will be built.",
                        level="info",
                    )
                    return
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id}: "
                    f"{_person_words(answer.decided_by, answer.decided_by_name)} said yes "
                    "to the spec. Writing the task plan and the quality "
                    "checklist next — nothing is built until you give the "
                    "go-ahead.",
                    level="info",
                )
            elif answer.outcome == "revise":
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id}: "
                    f"{_person_words(answer.decided_by, answer.decided_by_name)} "
                    "sent a note. Rewriting the spec from it and coming back "
                    "with a fresh list.",
                    level="info",
                )

        return await self._inline_confirmation_door(
            row,
            correlation_id,
            stage_label=_DIGEST_REVIEW_STAGE,
            details_key="digest_review",
            checkpoint_type=_DIGEST_REVIEW_CHECKPOINT_TYPE,
            rationale=_DIGEST_REVIEW_RATIONALE,
            sentinel_outcome="approved",
            answered_outcome="approved",
            persisted={"card": card},
            # The card carries the whole ``.feature`` under "show me", so it is
            # written ONCE per round, on the opening row a recovery replays
            # from — never again on the verdict.
            receipt_keys=(),
            rehydrate=_rehydrate,
            decide=_decide,
            open_message=lambda approver, wait: self._digest_door_open_message(
                correlation_id,
                expected_approver=approver,
                wait_seconds=wait,
                rewrite=draft.get("rewrite"),
            ),
            log_noun="spec digest review door",
            on_close=_closed,
        )

    def _digest_review_card(
        self,
        correlation_id: str,
        *,
        digest_obj: Mapping[str, Any],
        feature_text: str,
        seed: Mapping[str, Any] | None = None,
        target_repo: str | None = None,
    ) -> dict[str, Any]:
        """The card the owner reads — PLAIN language, one sentence per example.

        Composed from the digest that has just been PROVEN against the committed
        spec, so every sentence on this card has a worked example behind it and
        every example has a sentence. The worked examples themselves ride along
        for the "show me" view, one click deeper; they are never the ask.

        The labels travel VERBATIM as the spec wrote them. Turning a label into
        the words a person reads is the card renderer's job, and it renders only
        the labels it has a plain word for — so an internal label can never
        surprise a reader by appearing raw.

        ONE THING THE RENDERER MUST DECIDE, NAMED HERE RATHER THAN DISCOVERED
        LIVE: ``worked_examples`` is the WHOLE committed ``.feature``, verbatim
        and UNSCRUBBED, and it rides to the messenger inside the approval
        envelope's ``summary_data``. Real specs in this estate carry text the
        plain-name fence forbids on a user surface — ``features/`` scenarios in
        this very repo are tagged ``@task:TASK-MP-008`` (jarvis's forbidden
        task-id pattern) and spec bodies routinely name the internal tools.

        Every OTHER thing a person is asked about here is composed from the
        digest and is fence-safe by construction, and the raw labels are
        already ruled on (see the paragraph above: whitelisted at render). This
        one field is neither, because its whole value is being the spec's own
        words. So the surface that renders the "show me" view owns the choice —
        scrub it, or exempt that view deliberately — and it cannot make that
        choice by accident, because
        ``test_the_show_me_text_is_the_raw_spec_unscrubbed`` pins exactly what
        is in the field and what is not. Note that jarvis's fence suite renders
        with neutral fixtures, so it will NOT catch this on its own.
        """
        wait_seconds = max(1, int(self._deps.planning_config.originator_wait_seconds))
        examples: list[dict[str, Any]] = []
        for entry in digest_obj.get("scenarios") or []:
            if not isinstance(entry, Mapping):
                continue
            examples.append(
                {
                    "sentence": str(entry.get("sentence") or ""),
                    "tags": [str(tag) for tag in (entry.get("tags") or [])],
                }
            )
        assumptions: list[dict[str, str]] = []
        for entry in digest_obj.get("assumptions") or []:
            if not isinstance(entry, Mapping):
                continue
            assumptions.append(
                {
                    "assumption": str(entry.get("text") or ""),
                    "why": str(entry.get("basis") or ""),
                }
            )
        card: dict[str, Any] = {
            "checkpoint": _DIGEST_REVIEW_CHECKPOINT_TYPE,
            "title": "The spec is ready — here's what will be built",
            "what_happened": (
                "The spec-writer has written the worked examples this build "
                "will be checked against. Below is one sentence per example, in "
                "the order they appear. This list is checked by ordinary code "
                "against the examples themselves — every example is here, none "
                "has been left out."
            ),
            "what_it_will_do": examples,
            "what_the_machine_assumed": assumptions,
            "approve_means": (
                "Yes — this is what I want built: the machine writes the task "
                "plan and the quality checklist, then comes back for your "
                "go-ahead before any build starts. Nothing is built yet."
            ),
            "note_means": (
                "Send a note and the machine rewrites the spec from what you "
                f"say and shows you a fresh list. Up to {CYCLE_CAP - 1} "
                "rewrites; past that it stops and says it needs you."
            ),
            "show_means": (
                "Show the worked examples to read the examples themselves. You "
                "never have to."
            ),
            "no_answer_means": (
                f"No answer within {self._plain_wait(wait_seconds)}: the run "
                "stops and says so — nothing is built."
            ),
            # One click deeper. Never the ask, never in front of the reader.
            "worked_examples": feature_text,
        }
        feature = str(digest_obj.get("feature") or "").strip()
        if feature:
            card["feature"] = feature

        # WHICH REPOSITORY this will be built in, on the card the owner
        # approves (2026-09-05 rule 5). A sentence that named no repository
        # went to the default, and the default is named here too, so the
        # reader never has to guess. A renderer that does not know the field
        # shows the card exactly as it did before.
        repo = target_repo or self._deps.planning_config.default_target_repo
        if repo:
            card["target_repo"] = repo

        # THE ONE TAP ANSWERS BOTH QUESTIONS. The sign-in flag is already known
        # here — it was raised by the same spec this card is about — so the
        # question is asked HERE, with the spec in front of the reader, instead
        # of an hour later on a second card with no spec attached. A run that
        # pauses twice has failed; this is how it pauses once.
        #
        # It is written as an ASSUMPTION the reader decides, not as an
        # instruction to write prose, and that is the whole point: a decided
        # assumption comes back as a VALUE on the wire
        # (:func:`_item_answers`), so both answers — "agreed, no sign-in" and
        # "no, this really does involve signing in" — are read by ordinary
        # code. The earlier wording asked for the second answer "in a note",
        # which the note channel cannot carry: a note means REWRITE THE SPEC at
        # this door, and no amount of reading the prose could tell the two
        # apart without the machine judging what the words meant.
        if seed is not None and bool(seed.get("auth_surface_bearing")):
            card["sign_in_check"] = {
                "title": "One thing to confirm",
                "answer_id": _SIGN_IN_ASSUMPTION_ID,
                "statement": (
                    "Nothing in this feature involves signing in or checking "
                    "who someone is."
                ),
                "body": (
                    "The spec checker thinks this feature might involve "
                    "signing in or checking who someone is. Say whether that "
                    "is right here, with the spec in front of you — it is the "
                    "only time you will be asked."
                ),
                "why_we_ask": (
                    "The check that spots this is a keyword scan, and it fires "
                    "most often on a spec that is explaining it does not need a "
                    "sign-in."
                ),
                "agree_means": (
                    "Agree — nothing here touches signing in: the build "
                    "carries on and the quality checklist is registered "
                    "automatically."
                ),
                "disagree_means": (
                    "Disagree — this really does involve signing in: the "
                    "machine writes the task plan and then STOPS, so a person "
                    "registers the quality checklist by hand. Nothing is built."
                ),
                "no_answer_means": (
                    "Say nothing about this one and saying yes to the spec is "
                    "taken as agreeing there is no sign-in here."
                ),
                "flagged_lines": self._auth_basis_lines(
                    str(seed.get("auth_surface_basis") or "no basis supplied")
                ),
            }
        return card

    def _digest_door_open_message(
        self,
        correlation_id: str,
        *,
        expected_approver: str | None,
        wait_seconds: int,
        rewrite: Mapping[str, Any] | None = None,
    ) -> str:
        """The plain-language ping that says the run is WAITING, not broken.

        ``rewrite`` is the draft row's record of the round: present from the
        second card onward, and when it says the rewrite came back with the
        same list the ping says THAT instead, in one sentence. A ping that read
        the same as the first one is how a note that did nothing stayed
        invisible.
        """
        if rewrite is not None and rewrite.get("repeat"):
            return _SAME_LIST_NOTIFICATION.format(
                correlation_id=correlation_id, note=str(rewrite.get("note") or "")
            )
        who = _person_words(expected_approver)
        subject, verb = ("You", "have") if who == "you" else (who, "has")
        return (
            f"Planning run {correlation_id}: the spec is written. {subject} "
            f"{verb} a card listing, in one sentence each, everything this "
            "build will be checked against. Say yes and the machine writes the "
            "task plan and the quality checklist; send a note and it rewrites "
            f"the spec from what you say. No answer within "
            f"{self._plain_wait(wait_seconds)} stops the run — nothing is built "
            "either way until you give the go-ahead."
        )

    async def _escalate_spec_review(
        self, correlation_id: str, notes: list[str]
    ) -> bool:
        """Past the revision bound: stop loudly, and say what was asked for.

        The bound is the estate's existing frozen ``CYCLE_CAP`` — three cards
        total, the first plus two rewrites — reused rather than a second number
        minted beside it. The brief-stage dialogue this pause absorbs ran on
        exactly that cap, so a person's budget per feature is unchanged.

        This is the loud stop, not a quiet one: the run FAILS, the durable row
        keeps every note verbatim, and the owner's sentence quotes back what
        they said each time so the next person picks it up knowing what was
        actually wanted.

        HONEST DEVIATION FROM THE DESIGN, recorded rather than papered over: the
        design named ``escalate_planning_run`` as the mechanism here. That
        function re-targets the approver under a compare-and-set pinned to the
        PAUSED state (``escalation.py`` — ``expected_from_state=PAUSED``), and
        this run is at FEATURE_SPEC, which has no PAUSED edge. Calling it would
        be refused at the CAS, log a misleading "a concurrent transition won"
        warning, and overwrite the run's pending product-docs request id on the
        way out. So the rung is built as what the design's own sentence asks
        for — "the run stops with a terminal that says plainly … this one needs
        a person" — rather than as a call that cannot do it.
        """
        quoted = "; ".join(f"“{note}”" for note in notes)
        return await self._fail_leg(
            correlation_id,
            _FEATURE_SPEC_STAGE,
            f"spec digest review reached the revision bound ({CYCLE_CAP} cards) "
            f"without an approval; notes given: {quoted}",
            owner_message=(
                f"Planning run {correlation_id} stopped at "
                f"{plain_stage_name(_DIGEST_REVIEW_STAGE)}: the machine tried "
                f"{CYCLE_CAP} times and could not write a spec that matched your "
                f"notes. Here is what you said each time: {quoted}. This one "
                "needs a person. Nothing was built."
            ),
        )

    @staticmethod
    def _capture_digest(
        role_output: Mapping[str, Any], files: Mapping[str, str]
    ) -> str | None:
        """The spec digest's content, from the committed files or the native map.

        Prefers the committed set (the digest is a fourth committed file, so the
        planning branch carries the complete record of what was approved), and
        falls back to the reply's own map for the alternate ``files`` shape.
        ``None`` when the reply shipped no digest at all — a loud failure at the
        caller, never a card with an unchecked summary on it.
        """
        for rel, content in files.items():
            if str(rel).endswith(_SPEC_DIGEST_SUFFIX):
                return str(content)
        for name, content in role_output.items():
            if str(name).endswith(_SPEC_DIGEST_SUFFIX):
                return str(content)
        return None

    async def _feature_plan_leg(self, row: Any, correlation_id: str) -> bool:
        """FEATURE_PLAN leg: mint the FEAT id, dispatch 008, write + validate.

        Returns True once the plan tree is validated + committed (the caller
        proceeds to the B3 build trigger), False on a loud terminal failure. A
        durable ``feature-plan`` approved event short-circuits a re-drive
        (no re-dispatch — returns True so the build trigger still runs, which is
        what makes a crash between the plan commit and BUILD_QUEUED recover).
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _FEATURE_PLAN_STAGE):
            logger.info(
                "planning driver: run %s feature-plan already complete "
                "(idempotent re-drive — proceeding to the B3 build trigger)",
                correlation_id,
            )
            return True

        if deps.dispatch_feature_plan is None or deps.validate_feature_plan is None:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "target terminal ON but the plan leg collaborators "
                "(dispatch_feature_plan / validate_feature_plan) are not wired",
            )
            return False
        resolved = await self._resolve_repo(row, correlation_id, stage_label=_FEATURE_PLAN_STAGE)
        if resolved is None:
            return False
        target_repo, repo_path = resolved
        branch = f"planning/{correlation_id}"
        plan_run_id = f"plan-{correlation_id}"
        spec_details = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE)
        slug = str(spec_details.get("slug") or self._slug_of({}, correlation_id))
        feature_id = self._mint_feature_id(correlation_id)

        # The 008 contract needs the CONTENTS of the committed spec triple, not
        # paths. Read them back off the planning branch (they are no longer in
        # memory after the spec leg returned — and on an idempotent re-drive the
        # spec leg never ran this drive at all). The .feature is the committed,
        # normalizer-collapsed content of record.
        spec_files = spec_details.get("spec_files") or []
        spec_feature, spec_summary, spec_assumptions = await self._read_spec_triple(
            repo_path, branch, spec_files
        )
        if not spec_feature or not spec_summary:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "could not read the committed spec triple contents "
                f"(.feature / _summary.md) off branch {branch} "
                f"(spec_files={sorted(spec_files)})",
            )
            return False
        target_repo_descriptor = self._build_target_repo_descriptor(
            target_repo, repo_path
        )
        # WHERE the specification sits, beside WHAT it says (2026-08-22). These
        # are the same paths the stamp normalizer uses further down; they are
        # computed HERE, before the dispatch, because the plan-writer needs them
        # to fill in the plan YAML's ``feature_files:`` — and before this it was
        # never told them. Six of the ten plans captured on 2026-08-22 that wrote
        # that key named a folder that does not exist, every one of them a folder
        # name built out of the feature's title. Forge is the party that knows:
        # it committed these files one leg earlier and has just read them back.
        spec_feature_paths = [
            str(rel) for rel in spec_files if str(rel).endswith(".feature")
        ]

        try:
            result = await deps.dispatch_feature_plan(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                feature_id=feature_id,
                spec_feature=spec_feature,
                spec_summary=spec_summary,
                target_repo_descriptor=target_repo_descriptor,
                spec_assumptions=spec_assumptions,
                spec_feature_paths=spec_feature_paths,
            )
        except Exception as exc:  # noqa: BLE001 — dispatch boundary
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"008 dispatch raised {type(exc).__name__}: {exc}",
            )
            return False
        ok, reason = self._dispatch_ok(result)
        if not ok:
            await self._fail_leg(
                correlation_id, _FEATURE_PLAN_STAGE, f"008 dispatch {reason}"
            )
            return False

        role_output = self._role_output_of(result)
        # RV-1: assert the SUPPLIED feature id, not filename self-consistency.
        declared = role_output.get("feature_id")
        if declared is not None and str(declared) != feature_id:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"feature id mismatch (RV-1): forge supplied {feature_id}, "
                f"the plan declares {declared}",
            )
            return False

        files = self._plan_tree_files(role_output)
        if not files:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "008 returned no plan tree (invalid artifacts)",
            )
            return False

        # VALIDATION CHANNEL (C5): advisory self-check data, not an oracle —
        # same posture as the 007 leg above. The REAL oracle for the plan tree
        # is ``guardkit feature validate`` in the pre-commit step right below:
        # a self-flagged plan that validates green is good enough by the
        # estate's own bar. Surface the self-reported errors LOUDLY, verbatim,
        # then let the oracle decide. TODO(C5 follow-on): the bounded
        # revision_of re-invoke consumes this channel properly.
        plan_val_errors = self._validation_failures(role_output)
        if plan_val_errors:
            logger.warning(
                "008 validation.json self-check reported failures for %s "
                "(ADVISORY — proceeding to the guardkit validate oracle): %s",
                correlation_id,
                "; ".join(plan_val_errors),
            )

        validate = deps.validate_feature_plan
        # THE STAMP NORMALIZER (Rich's condition 1): the committed spec
        # ``.feature`` path(s) are the scenario universe forge can name when
        # the plan-writer omitted ``feature_files:``. ``spec_feature_paths`` is
        # computed above the dispatch since 2026-08-22 — the SAME list is now
        # also sent to the plan-writer, so what forge checks here and what the
        # writer was told cannot drift apart.
        stamp_state: dict[str, Any] = {}

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            # Rich's condition 1 (2026-08-16): the verifier stamps are WRITTEN
            # on the planning branch BEFORE validate — the normalizer runs
            # here, against the materialised worktree, so what it writes
            # rides the plan commit and validate reads the stamped YAML.
            # Coordinator condition 5 (same day): the STOP is gated on the
            # routing law's ENFORCEMENT for this repo/feature (feature-level
            # flag → repo config → off). Not enforced, a partial / refused /
            # failed normalizer never kills the plan — the decided stamps it
            # wrote ride the commit, and everything is receipted below.
            stamps = await self._stamp_normalizer_step(
                worktree, feature_id, spec_feature_paths
            )
            stamp_state["outcome"] = stamps
            if stamps.stops_the_run:
                return PreCommitResult(
                    ok=False, detail=f"stamp normalizer {stamps.status}: {stamps.detail}"
                )
            outcome = await validate(worktree, feature_id)
            return PreCommitResult(ok=outcome.ok, detail=outcome.detail)

        try:
            gitres = await deps.git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=(
                    f"planning: feature plan {feature_id} for {correlation_id} "
                    "(Lane B 008)"
                ),
                pre_commit=_pre_commit,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"plan write raised {type(exc).__name__}: {exc}",
            )
            return False
        if gitres.status == "failed":
            stamps = stamp_state.get("outcome")
            if stamps is not None and stamps.stops_the_run:
                # The normalizer stopped the run — the card names the refused
                # titles VERBATIM (or the reason it could not run); the
                # machine record keeps the internal reason.
                await self._fail_leg(
                    correlation_id,
                    _FEATURE_PLAN_STAGE,
                    f"stamp normalizer {stamps.status} for {feature_id}: "
                    f"{stamps.detail}"
                    + (
                        f"; refused titles: {list(stamps.refused_titles)!r}"
                        if stamps.refused_titles
                        else ""
                    ),
                    owner_message=self._stamp_normalizer_card(
                        correlation_id, feature_id, stamps
                    ),
                )
                return False
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"plan write / feature validate failed: {gitres.stderr}",
            )
            return False

        stamps = stamp_state.get("outcome")
        # The plan receipts carry the normalizer's outcome whichever way it
        # went — written / nothing-to-do / partial / refused / failed /
        # unavailable / not-wired — so a plan committed WITHOUT (all its)
        # stamps is never silent about it. (An idempotent RT-08 short-circuit
        # that never ran the hook leaves the key absent, honestly.)
        stamp_receipt = stamps.receipt() if stamps is not None else None
        if stamps is not None and stamps.disagreements:
            # ADVISORY disagreements (Rich's ruling 08-18): named to the owner
            # on their own plain line WHATEVER the status / enforcement — the
            # stamps are never changed, so visibility is the whole mechanism.
            stamp_receipt = dict(stamp_receipt or {})
            dline = self._stamp_disagreements_line(stamps)
            stamp_receipt["disagreements_line"] = dline
            dsent = await self._notify(correlation_id, dline, level="info", mention=False)
            stamp_receipt["disagreements_line_sent"] = (
                "sent" if dsent == "sent" else "line not sent (no notifier)"
            )
        if stamps is not None and stamps.is_failure and not stamps.enforced:
            # Coordinator condition 5: NOT ENFORCED → the plan PROCEEDED past
            # a partial / refused / failed normalizer. The owner gets ONE
            # plain, un-@mentioned line in the same thread naming every
            # example that has no verification home (when there are titles
            # to name); the receipt says whether it went out.
            stamp_receipt = dict(stamp_receipt or {})
            stamp_receipt["proceeded_unenforced"] = True
            line = self._stamp_normalizer_unenforced_line(stamps)
            if line is None:
                stamp_receipt["owner_line"] = (
                    "no owner line: no refused titles to name "
                    f"(status {stamps.status}; see detail)"
                )
            else:
                stamp_receipt["owner_line"] = line
                sent = await self._notify(
                    correlation_id, line, level="info", mention=False
                )
                stamp_receipt["owner_line_sent"] = (
                    "sent"
                    if sent == "sent"
                    else "line not sent (no notifier)"
                    if sent == "no-notifier"
                    else "line not sent (publish failed)"
                )
        details: dict[str, Any] = {
            "feature_id": feature_id,
            "slug": slug,
            "plan_files": sorted(files),
            "target_repo": target_repo,
            "branch": branch,
            "sha": gitres.sha,
        }
        if stamp_receipt is not None:
            details["stamp_normalizer"] = stamp_receipt
        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_FEATURE_PLAN_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(details),
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: machine spec + plan complete and "
            f"validated (feature {feature_id}, branch {branch})"
            f"{self._stamp_normalizer_clause(stamps)}; queueing the build.",
            level="info",
        )
        logger.info(
            "planning driver: run %s feature-plan validated (feature_id=%s); "
            "proceeding to the B3 build trigger",
            correlation_id,
            feature_id,
        )
        return True

    async def _stamp_normalizer_step(
        self,
        worktree: Path,
        feature_id: str,
        spec_feature_paths: list[str],
    ) -> "StampNormalizerOutcome":
        """Run THE STAMP NORMALIZER against the planning worktree (pre-validate).

        Rich's condition 1 (2026-08-16): the ``verifier:`` stamps are WRITTEN
        on the planning branch before validate. Two things happen here, in
        order, both loud:

        1. if the plan YAML declares no ``feature_files:`` at all, forge writes
           the committed spec ``.feature`` path(s) into it — the universe the
           normalizer reads is explicit, and forge is the party that knows it
           (it committed that file one leg earlier; live 008 plans omit the
           key — api_test FEAT-F924);
        2. ``guardkit qa normalize-stamps --feature <id> --repo <worktree>``
           via the frozen guardkit seam. Written / nothing-to-do → proceed to
           validate; unavailable (older guardkit, no such subcommand) → log
           ``normalizer unavailable`` and proceed, receipted; not wired →
           proceed, receipted. Partial / refused / failed → the routing
           law's ENFORCEMENT for this repo/feature decides (coordinator
           condition 5): ENFORCED → the leg stops with a card naming the
           titles verbatim; NOT ENFORCED → the plan proceeds (the decided
           stamps already written ride the commit), a WARNING (partial /
           refused) or ERROR (failed) is logged here, and the caller
           receipts every title and tells the owner in one plain line.

        The enforcement is resolved AFTER the normalizer ran, from the
        worktree: the feature YAML's own ``routing_law:`` wins, then the
        repo's ``.guardkit/config.yaml``, else off — the same two places and
        the same precedence guardkit's plan-load half reads. Forge only READS
        the flag; it never writes ``routing_law`` (pinned by test).

        Never raises.
        """
        from forge.pipeline.routing_stamps import resolve_routing_law
        from forge.planning.target_terminal_tools import (
            StampNormalizerOutcome,
            declare_feature_files_if_absent,
        )

        normalize = self._deps.normalize_stamps
        if normalize is None:
            logger.info(
                "stamp normalizer hook: not wired for %s — the plan is "
                "committed as the plan-writer wrote it (receipted)",
                feature_id,
            )
            return StampNormalizerOutcome(
                status="not-wired",
                detail=(
                    "no normalize_stamps collaborator wired; verifier stamps were "
                    "NOT minted by forge for this plan"
                ),
            )
        fill = declare_feature_files_if_absent(worktree, feature_id, spec_feature_paths)
        if fill.inconsistent:
            # Coordinator condition 4: refuse LOUD, do not run the normalizer on
            # a plan whose feature_files: contradicts forge's own spec commit.
            logger.error("stamp normalizer hook: %s", fill.reason)
            outcome = StampNormalizerOutcome(status="refused", detail=fill.reason)
        else:
            try:
                outcome = await normalize(worktree, feature_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — collaborator boundary
                logger.exception("stamp normalizer hook raised for %s", feature_id)
                outcome = StampNormalizerOutcome(
                    status="failed",
                    detail=f"normalize_stamps raised {type(exc).__name__}: {exc}",
                )
        if fill.fired:
            outcome = dataclasses.replace(
                outcome, feature_files_filled=tuple(fill.feature_files)
            )
        # Coordinator condition 5: the STOP is gated on enforcement.
        try:
            law = resolve_routing_law(worktree, feature_id)
        except Exception as exc:  # noqa: BLE001 — a resolver defect is "off", said aloud
            logger.warning(
                "stamp normalizer hook: routing-law resolver raised %s: %s for %s "
                "— enforcement read as off (the law is opt-in)",
                type(exc).__name__,
                exc,
                feature_id,
            )
            outcome = dataclasses.replace(
                outcome,
                enforcement="off",
                enforcement_source="default",
                enforcement_detail=f"resolver raised {type(exc).__name__}: {exc}",
            )
        else:
            outcome = dataclasses.replace(
                outcome,
                enforcement=law.enforcement,
                enforcement_source=law.source,
                enforcement_detail=law.detail,
            )
        if outcome.status == "unavailable":
            logger.warning(
                "stamp normalizer hook: normalizer unavailable for %s — %s",
                feature_id,
                outcome.detail,
            )
        elif outcome.is_failure and not outcome.enforced:
            if outcome.status == "failed":
                logger.error(
                    "stamp normalizer hook: %s FAILED for %s — %s; the routing law "
                    "is NOT enforced here (%s), so the plan PROCEEDS unstamped "
                    "(a broken normalizer must not kill an un-enforced chain)",
                    outcome.status,
                    feature_id,
                    outcome.detail,
                    outcome.enforcement_detail,
                )
            else:
                logger.warning(
                    "stamp normalizer hook: %s for %s — %s; refused titles: %s; the "
                    "routing law is NOT enforced here (%s), so the plan PROCEEDS "
                    "with the decided stamps written",
                    outcome.status,
                    feature_id,
                    outcome.detail,
                    list(outcome.refused_titles),
                    outcome.enforcement_detail,
                )
        elif outcome.is_failure:
            logger.error(
                "stamp normalizer hook: %s for %s — %s; the routing law IS "
                "enforced here (%s): the plan leg STOPS",
                outcome.status,
                feature_id,
                outcome.detail,
                outcome.enforcement_detail,
            )
        return outcome

    @staticmethod
    def _stamp_normalizer_unenforced_line(
        stamps: "StampNormalizerOutcome",
    ) -> str | None:
        """The ONE plain line the owner gets when the plan proceeded past a
        partial / refused normalizer in a repo that does not enforce the
        routing law — the refused titles named verbatim, one per line, no
        rule ids, no @mention. ``None`` when there are no titles to name (a
        cannot-run failure, or forge's own condition-4 refusal): those are
        logged + receipted, and the plan-complete line carries the clause.
        """
        if not stamps.refused_titles:
            return None
        n = len(stamps.refused_titles)
        m = stamps.total_scenarios
        titles = "\n".join(f"  - {t}" for t in stamps.refused_titles)
        return (
            f"{n} of {m} examples could not be given a verification home by rule —\n"
            f"{titles}\n"
            "— the plan proceeds; this repo does not enforce the routing law yet"
        )

    @staticmethod
    def _stamp_disagreements_line(stamps: "StampNormalizerOutcome") -> str | None:
        """The ONE plain line the owner gets when the rules DISAGREE with a
        stamp already on the plan (Rich's ruling 08-18, drive-19 datum): the
        stamp is left exactly as written — this is visibility at zero
        authority — so it goes out WHATEVER the status and whether or not the
        repo enforces the law (a legal-but-wrong stamp passes the law). Plain
        words, no rule ids, no @mention."""
        if not stamps.disagreements:
            return None
        n = len(stamps.disagreements)
        rows = "\n".join(
            f"  - {d.get('title', '?')} — stamped {d.get('stamped', '?')}, "
            f"the rules say {d.get('rule_home', '?')}"
            for d in stamps.disagreements
        )
        return (
            f"{n} example(s) carry a verification home the rules would not have chosen —\n"
            f"{rows}\n"
            "— the stamps stand as written (nothing was changed); worth a look before this feature graduates"
        )

    @staticmethod
    def _stamp_normalizer_clause(stamps: "StampNormalizerOutcome | None") -> str:
        """The owner-facing clause on the plan-complete line — plain words."""
        if stamps is None:
            return ""
        if stamps.status == "written":
            n = len(stamps.stamped) or (stamps.stamps_on_branch or 0)
            return f"; {n} verifier stamp(s) minted by rule and committed with the plan"
        if stamps.status == "nothing-to-do":
            return "; every scenario already carried its verifier stamp"
        if stamps.status == "unavailable":
            return (
                "; verifier stamps NOT minted — this guardkit predates the stamp "
                "normalizer (rebake pending), so the plan is unstamped"
            )
        if stamps.is_failure and not stamps.enforced:
            k = len(stamps.refused_titles)
            n = len(stamps.stamped) or (stamps.stamps_on_branch or 0)
            if stamps.status == "partial" and k:
                return (
                    f"; {n} verifier stamp(s) minted by rule and committed with the "
                    f"plan, {k} example(s) left without one (named above)"
                )
            if stamps.status == "partial":
                return (
                    f"; {n} verifier stamp(s) minted by rule and committed with the "
                    "plan, some example(s) left without one (the normalizer's list "
                    "could not be read back)"
                )
            if stamps.status == "refused" and k:
                return (
                    f"; verifier stamps NOT minted — {k} example(s) had no rule to "
                    "decide a verifier (named above)"
                )
            return (
                "; verifier stamps NOT minted — the stamp normalizer "
                f"{'refused' if stamps.status == 'refused' else 'could not run'} "
                "and this repo does not enforce the routing law yet"
            )
        return ""

    @staticmethod
    def _stamp_normalizer_card(
        correlation_id: str, feature_id: str, stamps: "StampNormalizerOutcome"
    ) -> str:
        """The owner's card when THE STAMP NORMALIZER stops the run — which
        it does ONLY where the routing law is enforced for the repo/feature
        (coordinator condition 5).

        Names every refused title VERBATIM (the rule could not decide which
        verifier proves it and there is no fallback home; partial and refused
        alike — no rule ids on the face), says nothing was stamped on the
        branch and nothing was built, says the repo enforces the law, and
        says what a person does next — in plain words, the vocabulary named
        once. A cannot-run failure names the reason instead.
        """
        from forge.pipeline.routing_stamps import VERIFIER_HOMES

        head = (
            f"Planning run {correlation_id} stopped at "
            f"{plain_stage_name(_FEATURE_PLAN_STAGE)}"
        )
        if stamps.status in ("refused", "partial") and stamps.refused_titles:
            titles = "\n".join(f"  - {t}" for t in stamps.refused_titles)
            n = len(stamps.refused_titles)
            recovered = (
                " (titles read back from the normalizer's wrapped console echo; "
                "compare against the spec)"
                if stamps.titles_recovered_from_console_echo
                else ""
            )
            return (
                f"{head}: the verifier stamps could not all be minted by rule for "
                f"feature {feature_id}. {n} scenario(s) had no rule to decide "
                f"which verifier proves them, and there is no fallback home, so "
                f"nothing was stamped and nothing was built{recovered}:\n"
                f"{titles}\n"
                f"This repo enforces the routing law: every scenario needs a "
                f"verifier before its plan can be committed. "
                f"What to do: give each of these scenarios a verifier by hand in "
                f".guardkit/features/{feature_id}.yaml under scenarios: — one of "
                f"{', '.join(VERIFIER_HOMES)} (operator only for attended human "
                f"work, never as a default) — then re-run the request."
            )
        return (
            f"{head}: the verifier-stamp normalizer could not run for feature "
            f"{feature_id}, so the plan was not stamped and nothing was built "
            f"(this repo enforces the routing law). Reason: {stamps.detail}"
        )

    async def _build_trigger_leg(self, row: Any, correlation_id: str) -> bool:
        """B3 build trigger: queue the validated feature, advance to BUILD_QUEUED.

        On validate green (a durable ``feature-plan`` event exists) this queues
        the feature onto forge's OWN Mode B build dispatcher via the injected
        ``dispatch_build_trigger`` collaborator — the canonical MODE_B path
        (NOT the local guardkit CLI) whose pre-dispatch approval gate then
        pauses the build for the human tap and whose build-paused lifecycle
        event jarvis renders (the existing build-notification surface).

        The trigger is a fire-and-forget publish (no specialist round-trip), so
        it introduces no new unbounded wait (rule 5). Idempotent: a durable
        ``build-queued`` event means the feature was already queued (crash
        before the BUILD_QUEUED transition) — the leg just re-attempts the
        transition without re-publishing. Returns True on success (drive()
        re-reads BUILD_QUEUED, the target terminal), False on a loud failure.
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _BUILD_QUEUED_STAGE):
            return self._advance_to_build_queued(correlation_id)

        if deps.dispatch_build_trigger is None:
            return await self._fail_leg(
                correlation_id,
                _BUILD_QUEUED_STAGE,
                "target terminal ON but the build trigger collaborator "
                "(dispatch_build_trigger) is not wired",
            )

        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        feature_id = str(plan_details.get("feature_id") or "")
        if not feature_id:
            return await self._fail_leg(
                correlation_id,
                _BUILD_QUEUED_STAGE,
                "no feature id recorded on the feature-plan leg — cannot queue "
                "the build",
            )
        target_repo = str(plan_details.get("target_repo") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        plan_files = plan_details.get("plan_files") or []
        plan_run_id = f"plan-{correlation_id}"

        try:
            result = await deps.dispatch_build_trigger(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                feature_id=feature_id,
                target_repo=target_repo,
                branch=branch,
                plan_files=list(plan_files),
                originating_user=row["originating_user"],
            )
        except Exception as exc:  # noqa: BLE001 — trigger boundary, never crash
            return await self._fail_leg(
                correlation_id,
                _BUILD_QUEUED_STAGE,
                f"build trigger raised {type(exc).__name__}: {exc}",
            )

        queued = bool(getattr(result, "queued", False))
        if not queued:
            reason = str(getattr(result, "reason", None) or "no reason supplied")
            return await self._fail_leg(
                correlation_id,
                _BUILD_QUEUED_STAGE,
                f"build trigger did not queue the feature: {reason}",
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_BUILD_QUEUED_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": feature_id,
                    "build_id": getattr(result, "build_id", None),
                    "target_repo": target_repo,
                    "branch": branch,
                }
            ),
        )
        if not self._advance_to_build_queued(correlation_id):
            return False
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: feature {feature_id} queued for "
            f"build on the pipeline's planning chain (branch {branch}); paused at "
            "the build approval gate for your tap.",
            level="info",
        )
        logger.info(
            "planning driver: run %s reached BUILD_QUEUED (feature_id=%s, "
            "branch=%s) — the target terminal is complete",
            correlation_id,
            feature_id,
            branch,
        )
        return True

    def _advance_to_build_queued(self, correlation_id: str) -> bool:
        """Transition FEATURE_PLAN → BUILD_QUEUED (idempotent-resume safe)."""
        refused = self._deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.BUILD_QUEUED,
            actor_identity="planning-driver",
            stage_label=_BUILD_QUEUED_STAGE,
            expected_from_state=PlanningState.FEATURE_PLAN,
        )
        if isinstance(refused, TransitionRefused):
            current = self._deps.store.get_run(correlation_id)
            if current and PlanningState(current["state"]) is PlanningState.BUILD_QUEUED:
                return True
            logger.warning(
                "planning driver: FEATURE_PLAN→BUILD_QUEUED refused for %s "
                "(current=%s)",
                correlation_id,
                refused.current_state,
            )
            return False
        return True

    async def _register_pass_bars_leg(self, row: Any, correlation_id: str) -> bool:
        """Register per-task QA pass bars from the 007 seed (B4 round-19).

        Runs at plan-commit — AFTER ``validate_feature_plan`` passed and the plan
        tree committed, BEFORE the B3 build trigger — because the WS2 B2
        precondition demands a ``qa/pass-bar-<TASK-ID>.yaml`` registered for
        every task BEFORE implementation, and the machine plan leg (008) does not
        emit them. Forge fans the captured feature-grain seed out per task,
        mirroring the Factory-2 registered shape, and commits the bars as ONE
        commit so tap-2's artifacts include them.

        Auth door (Rich's §5 call, cured 2026-07-31): a seed flagged
        ``auth_surface_bearing`` PAUSES here for the owner's one-tap
        confirmation instead of killing the run
        (:meth:`_auth_surface_confirmation_door`). Confirm ⇒ the leg proceeds
        exactly as the unflagged path (the flag was the false positive it
        usually is); reject / a set-aside ("later") answer / no answer /
        undeliverable card ⇒ the honest terminal that shipped before — SPL-007
        §A.2, the seed's own basis verbatim, no bars, no build queued — with a
        closing sentence naming WHICH of those actually happened.

        Idempotent: a durable ``qa-pass-bars`` event short-circuits a re-drive
        (a crash between the bars commit and BUILD_QUEUED never re-mints them).
        Returns True to keep driving (the caller proceeds to the B3 trigger),
        False on a loud terminal failure.
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _QA_PASS_BARS_STAGE):
            logger.info(
                "planning driver: run %s pass bars already registered "
                "(idempotent re-drive — proceeding to the B3 build trigger)",
                correlation_id,
            )
            return True

        if deps.validate_pass_bar is None:
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                "target terminal ON but the pass-bar validate collaborator "
                "(validate_pass_bar) is not wired",
            )

        # The seed was captured at the spec commit and persisted on the durable
        # feature-spec event (it survives an idempotent re-drive where the spec
        # leg never ran this drive).
        spec_details = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE)
        raw_seed = spec_details.get("pass_bar_seed")
        if not raw_seed:
            # (c) no seed shipped (older specialist). Fail LOUDLY at this cheaper
            # layer — the WS2 B2 precondition would refuse the build anyway.
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                "the 007 reply shipped no pass-bar seed (pass-bar-seed-*.yaml); "
                "cannot register the per-task QA pass bars the B2 precondition "
                "demands — the specialist must emit the feature-grain seed",
            )

        try:
            seed = yaml.safe_load(raw_seed)
        except yaml.YAMLError as exc:
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"the captured pass-bar seed is not parseable YAML: {exc}",
            )
        if not isinstance(seed, Mapping):
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                "the captured pass-bar seed is not a YAML mapping",
            )

        # AUTH DOOR (SPL-007 §A.2, Rich-ratified §5 call — CURED 2026-07-31 off
        # live run dff0cd00): an auth-surface-bearing seed no longer KILLS the
        # run. The contract's own words are "requires human confirmation", and
        # the flag is a keyword detector that fires on a spec PROVING its own
        # authlessness (dff0cd00 died exactly that way). So the run PAUSES for
        # the owner's one-tap confirmation — the assumptions-checkpoint
        # mechanics, mirrored — and only takes the honest terminal below
        # (the refusal text VERBATIM, plus which way the door closed) when the
        # owner rejects, never answers, or the card cannot be delivered.
        auth_confirmation: dict[str, Any] | None = None
        if bool(seed.get("auth_surface_bearing")):
            basis = str(seed.get("auth_surface_basis") or "no basis supplied")
            # ONE PAUSE, COUNTED END TO END. The sign-in flag was raised by the
            # SPEC, and the spec digest card already carried the question — with
            # the spec itself in front of the reader, which is strictly better
            # context for answering it than a card arriving here with no spec
            # attached. Their one tap answered both, so this leg opens no door.
            #
            # The door below is NOT dead code: it is still the door for a run
            # that reached here without a digest-review answer — the flag-OFF
            # path and any run already in flight when this landed.
            already = self._spec_review_auth_answer(correlation_id)
            if already is not None:
                # ALL THREE of the digest card's answers land here, not just
                # the yes. "There IS a sign-in" takes the SAME terminal below
                # that the sign-in door's own reject takes — the 2026-07-31
                # attended-registration guarantee, reached through the one
                # pause instead of a second door. There is one guarantee and
                # one place it is enforced; the digest card only changes where
                # the person was asked.
                door = str(already.get("outcome") or "confirmed")
                logger.info(
                    "planning driver: run %s sign-in question was answered on "
                    "the spec digest card by %s (%s); opening no second door",
                    correlation_id,
                    already.get("decided_by"),
                    door,
                )
                auth_confirmation = dict(already)
            else:
                door = await self._auth_surface_confirmation_door(
                    row, correlation_id, seed=seed, basis=basis
                )
            if door != "confirmed":
                # TWO AUDIENCES (the 2026-07-31 stage-names ruling). The
                # MACHINE reason — durable FAILED row + logs — keeps the clause
                # reference, the flag name and the seed's basis VERBATIM: that
                # is the receipt an operator greps. The OWNER's sentence names
                # NONE of them; it says which stage stopped, in plain words,
                # why, and that nothing was built.
                #
                # A door word with no sentence of its own would be a terminal
                # that says nothing, which is worse than a clumsy one — so an
                # unknown word (only reachable from a durable row written by
                # some later version) still names ITSELF.
                suffix = _AUTH_DOOR_TERMINAL_SUFFIX.get(
                    door,
                    f"The sign-in question closed as {door}, which is not a "
                    "yes, so the quality checklist must be registered attended.",
                )
                return await self._fail_leg(
                    correlation_id,
                    _QA_PASS_BARS_STAGE,
                    f"pass-bar seed is auth_surface_bearing — pass bars need "
                    f"attended registration per {_SPL_007_AUTH_CLAUSE}; refusing "
                    f"machine registration. Seed auth_surface_basis: {basis} "
                    f"{suffix}",
                    owner_message=(
                        f"Planning run {correlation_id} stopped at "
                        f"{plain_stage_name(_QA_PASS_BARS_STAGE)}: the spec "
                        "checker flagged this feature as sitting behind a "
                        f"sign-in. {suffix} Nothing "
                        "was registered and nothing was built."
                    ),
                )
            # Confirmed: from here the leg is BYTE-IDENTICAL to the unflagged
            # path. The owner's act is carried onto the leg's durable receipt.
            confirmed = self._leg_event_details(
                correlation_id, _AUTH_CONFIRM_STAGE
            ).get("auth_confirmation")
            if isinstance(confirmed, Mapping):
                auth_confirmation = dict(confirmed)

        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        feature_id = str(plan_details.get("feature_id") or "")
        plan_sha = str(plan_details.get("sha") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        plan_files = [str(f) for f in (plan_details.get("plan_files") or [])]
        if not feature_id or not plan_sha:
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                "no feature id / plan commit sha recorded on the feature-plan "
                "leg — cannot register per-task pass bars",
            )
        resolved = await self._resolve_repo(
            row, correlation_id, stage_label=_QA_PASS_BARS_STAGE
        )
        if resolved is None:
            return False
        _target_repo, repo_path = resolved

        # The per-task bar ids ARE the validated plan's task ids — the SAME
        # source ``guardkit feature validate`` reads: the committed feature YAML.
        plan_tasks = await self._read_plan_tasks(
            repo_path, branch, feature_id, plan_files
        )
        if plan_tasks is None:
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"could not read the validated plan's feature YAML for "
                f"{feature_id} off branch {branch} to enumerate task ids "
                f"(plan_files={sorted(plan_files)})",
            )

        run_date = deps.clock().date().isoformat()
        if not plan_tasks:
            # A validated plan with zero tasks: no per-task bars to register.
            # Record the leg complete (idempotent) and advance — B2 is vacuous.
            logger.info(
                "planning driver: run %s plan lists no tasks; no per-task pass "
                "bars to register",
                correlation_id,
            )
            deps.store._record_event(
                correlation_id=correlation_id,
                stage_label=_QA_PASS_BARS_STAGE,
                status="approved",
                actor_identity="planning-driver",
                details_json=json.dumps(
                    {
                        "feature_id": feature_id,
                        "bar_files": [],
                        "registered_at_sha": plan_sha,
                        "branch": branch,
                        **(
                            {"auth_confirmation": auth_confirmation}
                            if auth_confirmation
                            else {}
                        ),
                    }
                ),
            )
            return True

        # Fan the seed out — one bar per task, registered_at.sha = the PLAN
        # commit sha, date = the run date from the driver's clock,
        # auth_surface_bearing false on this path either by construction (an
        # unflagged seed) or by the owner's explicit confirmation at the door.
        #
        # One task type is not a plain fan-out (2026-09-04, off build-FEAT-44A8):
        # a task the PLAN ITSELF types ``documentation`` gets the checklist it
        # can actually be held to instead of the feature's machine criteria,
        # because guardkit runs no tests for that type and the task could never
        # produce that evidence. Forge reads the type from the same place
        # guardkit reads it — the task file's front matter, committed with the
        # plan — and a task file it cannot read simply mints as it always did.
        bars: dict[str, str] = {}
        docs_bars: list[str] = []
        for task in plan_tasks:
            task_id = task["id"]
            task_file = task.get("file_path")
            task_text = (
                await self._read_task_declaration(repo_path, branch, task_file)
                if task_file
                else None
            )
            task_type = task_type_from_front_matter(task_text or "")
            if (task_type or "") in _DOCS_TASK_TYPES:
                docs_bars.append(task_id)
            bars[f"qa/pass-bar-{task_id}.yaml"] = self._mint_pass_bar_yaml(
                task_id=task_id,
                seed=seed,
                sha=plan_sha,
                date=run_date,
                task_type=task_type,
                task_file=task_file,
                task_text=task_text,
            )
        if docs_bars:
            logger.info(
                "planning driver: run %s — %s are documentation tasks, so their "
                "quality checklists list what those tasks deliver instead of the "
                "feature's machine criteria (guardkit runs no tests for that type)",
                correlation_id,
                ", ".join(sorted(docs_bars)),
            )

        validate = deps.validate_pass_bar

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            # Run guardkit's OWN ``qa validate pass-bar`` on every minted bar so a
            # malformed forge-minted bar fails the leg before it lands (never a
            # bar the B2 gate would later reject).
            for rel in sorted(bars):
                outcome = await validate(worktree, rel)
                if not outcome.ok:
                    return PreCommitResult(
                        ok=False, detail=f"{rel}: {outcome.detail}"
                    )
            return PreCommitResult(ok=True)

        try:
            gitres = await deps.git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=bars,
                message=(
                    f"planning: register {len(bars)} per-task QA pass bar(s) for "
                    f"{correlation_id} ({feature_id}, Lane B seed fan-out)"
                ),
                pre_commit=_pre_commit,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"pass-bar write raised {type(exc).__name__}: {exc}",
            )
        if gitres.status == "failed":
            return await self._fail_leg(
                correlation_id,
                _QA_PASS_BARS_STAGE,
                f"pass-bar write / qa validate failed: {gitres.stderr}",
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_PASS_BARS_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": feature_id,
                    "bar_files": sorted(bars),
                    "registered_at_sha": plan_sha,
                    "sha": gitres.sha,
                    "branch": branch,
                    # Present only when the plan had documentation tasks, whose
                    # bars list their own criteria rather than the feature's
                    # machine ones — on the receipt so a reader can see which
                    # bars were narrowed without opening them.
                    **(
                        {"documentation_tasks": sorted(docs_bars)}
                        if docs_bars
                        else {}
                    ),
                    # Present ONLY when the auth door was walked: the owner's
                    # act is part of the leg's receipt, never inferred later.
                    **(
                        {"auth_confirmation": auth_confirmation}
                        if auth_confirmation
                        else {}
                    ),
                }
            ),
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: registered {len(bars)} per-task QA "
            f"pass bar(s) for {feature_id} on branch {branch} (seeded from the "
            "machine spec) before queueing the build.",
            level="info",
        )
        logger.info(
            "planning driver: run %s registered %d per-task pass bar(s) "
            "(feature_id=%s, plan_sha=%s)",
            correlation_id,
            len(bars),
            feature_id,
            plan_sha,
        )
        return True

    # ------------------------------------------------------------------ #
    # THE INLINE CONFIRMATION DOOR — ONE mechanism, two callers
    #
    # Born as the auth-confirmation door (the cure for live run dff0cd00,
    # 2026-07-31) and extracted here when the machine chain's spec-digest review
    # needed the same pause. A pause mechanism that exists twice drifts, and two
    # doors that behave differently are a promise the estate cannot keep — so
    # there is ONE door, parameterised, and both callers get every mechanic:
    # durable-before-wire bookkeeping, arm-before-post, the stale request-id
    # guard, per-run approver pinning, verbatim re-open after a restart, and the
    # idempotency sentinel that means an answered door is never re-asked.
    # ------------------------------------------------------------------ #

    async def _inline_confirmation_door(
        self,
        row: Any,
        correlation_id: str,
        *,
        stage_label: str,
        details_key: str,
        checkpoint_type: str,
        rationale: str,
        sentinel_outcome: str,
        answered_outcome: str,
        persisted: dict[str, Any],
        receipt_keys: tuple[str, ...],
        rehydrate: Callable[[Mapping[str, Any], int], dict[str, Any]],
        decide: Callable[[Any], str],
        open_message: Callable[[str | None, int], str],
        log_noun: str,
        on_close: Callable[[_DoorAnswer], Awaitable[None]] | None = None,
    ) -> _DoorAnswer:
        """Put ONE card in front of the run's approver and wait for the tap.

        MIRRORS the cycle-1 assumptions checkpoint one-for-one — durable
        bookkeeping BEFORE the wire, ONE approval-request envelope built by the
        SAME :func:`build_planning_approval_envelope` (so it is a WIRE-VALID
        ``ApprovalRequestPayload`` and the response rides the same
        ``agents.approval.forge.{plan_run_id}`` subject the checkpoint's
        answers ride), arm-before-post, per-run approver pinning, the stale
        ``request_id`` guard, and the wait window off
        ``PlanningConfig.originator_wait_seconds``.

        The ONE deliberate difference from the checkpoint: no PAUSED state
        transition. Both callers run mid-machine-chain, in states whose
        transition table has no PAUSED edge (``planning/states.py`` —
        deliberately additive-only), so the door waits INLINE instead of
        stranding a half-paused row.

        SURVIVING A RESTART (the mechanic that makes the inline wait safe): the
        boot sweep enumerates FEATURE_SPEC / FEATURE_PLAN alongside QUEUED /
        RUNNING (``cli/_serve_planning.sweep_interrupted_planning_runs``), so a
        daemon killed with the card live re-drives its leg on the next boot;
        and a door that is STILL OPEN on the durable record is RE-OPENED with
        its persisted ``request_id``, ``attempt_count`` and CARD CONTENT
        VERBATIM — exactly as :meth:`_republish_pending` re-emits the
        checkpoint's — so the card the owner can still see stays the card their
        tap answers, word for word. The durable ``approved`` event written on a
        yes is the idempotency sentinel: an answered door never re-asks.

        Parameters carry everything that differs between the two callers and
        nothing else:

        * ``stage_label`` / ``details_key`` — where the door's rows live and
          under which key its record sits inside ``details_json``;
        * ``checkpoint_type`` / ``rationale`` — the envelope's discriminator and
          its one-line reason;
        * ``sentinel_outcome`` — the outcome whose durable row is written as
          ``approved`` (the idempotency sentinel);
        * ``answered_outcome`` — what an ALREADY-answered door returns on a
          re-drive without re-asking;
        * ``persisted`` — the caller's own fields carried verbatim on the open
          row, and replayed into ``rehydrate`` on a recovery re-open;
        * ``receipt_keys`` — which of those fields ALSO belong on the verdict
          row. The card lives on the opening row, which is the only row a
          recovery reads, so a caller carrying a large card names no keys here
          and the event log stops storing the same bytes once per verdict;
        * ``rehydrate(persisted, wait_seconds)`` — builds the card dict from
          those fields, so a re-emission is the SAME card and never a re-render
          off possibly-drifted source;
        * ``decide(response)`` — maps a wire answer onto the caller's outcome;
        * ``open_message(approver, wait_seconds)`` — the plain-language ping
          that says the run is WAITING, not broken;
        * ``on_close`` — the caller's own closing act (a notification, a log).

        Returns a :class:`_DoorAnswer` carrying the outcome, who decided it, the
        wire decision literal and any free-text note they left.
        """
        deps = self._deps
        plan_run_id = f"plan-{correlation_id}"

        # Idempotent: an answered door is NEVER re-asked (a crash between the
        # owner's tap and the leg's own commit drives straight through).
        if self._has_leg_event(correlation_id, stage_label):
            logger.info(
                "planning driver: run %s %s already answered (idempotent "
                "re-drive — carrying on)",
                correlation_id,
                log_noun,
            )
            # Replay the answer that was ACTUALLY given rather than a bare
            # "yes". Who decided it is part of the receipt, and the per-item
            # answers are load-bearing: a run that crashed between the owner's
            # tap and the leg's own commit re-drives through here, and if the
            # replay dropped their "yes, there IS a sign-in" the re-drive would
            # read as agreement and register the checklist unattended. The
            # durable row carries them; this reads them back.
            given = self._leg_event_details(correlation_id, stage_label).get(
                details_key
            )
            if isinstance(given, Mapping):
                replayed = given.get("item_answers")
                return _DoorAnswer(
                    outcome=answered_outcome,
                    request_id=str(given.get("request_id") or ""),
                    decided_by=(
                        str(given["decided_by"]) if given.get("decided_by") else None
                    ),
                    decision=(
                        str(given["decision"]) if given.get("decision") else None
                    ),
                    item_answers=(
                        {str(k): str(v) for k, v in replayed.items()}
                        if isinstance(replayed, Mapping)
                        else {}
                    ),
                )
            return _DoorAnswer(outcome=answered_outcome, request_id="")

        current = deps.store.get_run(correlation_id) or row
        expected_approver = current["expected_approver"]
        wait_seconds = max(1, int(deps.planning_config.originator_wait_seconds))

        # RECOVERY vs FRESH OPENING. A door left OPEN on the durable record is
        # one the daemon died holding: the owner's card is still in their Slack.
        # Re-emit it VERBATIM (persisted request_id + attempt_count + card) —
        # the checkpoint's :meth:`_republish_pending` mechanic — so their tap
        # lands on THIS run instead of being dropped by the stale guard below.
        # Only a genuinely new door mints a new id (and bumps the attempt, so a
        # later round is never deduped away by first-response-wins idempotency).
        open_door = self._open_door(correlation_id, stage_label, details_key)
        if open_door is None:
            attempt_count = self._door_attempt(correlation_id, stage_label)
            request_id = derive_request_id(
                build_id=plan_run_id,
                stage_label=stage_label,
                attempt_count=attempt_count,
            )
            door_status = "GATED"
            door_outcome = "opened"
        else:
            request_id = str(open_door["request_id"])
            attempt_count = int(open_door.get("attempt_count") or 0)
            # The card's own words as first published — a re-emission must be
            # the SAME card, not a re-render of possibly-drifted source.
            persisted = {
                key: open_door[key] for key in persisted if key in open_door
            } or persisted
            door_status = "reopened"
            door_outcome = "reopened"
            logger.info(
                "planning driver: run %s re-opening the %s left live by a "
                "restart — re-emitting card %s (attempt=%d) verbatim so the "
                "owner's existing card still answers it",
                correlation_id,
                log_noun,
                request_id,
                attempt_count,
            )

        card = rehydrate(persisted, wait_seconds)

        # Durable-before-wire (the checkpoint's SQLite-before-wire discipline):
        # the open door is on the record before the card can be answered.
        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=stage_label,
            status=door_status,
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    details_key: {
                        "outcome": door_outcome,
                        "request_id": request_id,
                        "attempt_count": attempt_count,
                        "expected_approver": expected_approver,
                        **persisted,
                        "wait_seconds": wait_seconds,
                    }
                }
            ),
        )

        envelope = build_planning_approval_envelope(
            request_id=request_id,
            plan_run_id=plan_run_id,
            feature_id=plan_run_id,
            stage_label=stage_label,
            summary_data=card,
            expected_approver=expected_approver,
            attempt_count=attempt_count,
            rationale=rationale,
            checkpoint_type=checkpoint_type,
            # Thread the card into the run's own Slack thread (the durable
            # anchor on the row — never re-derived), like every planning card.
            parent_request_id=current["parent_request_id"],
        )

        deadline = time.monotonic() + float(wait_seconds)
        published = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not published:
                    # The window closed without the card EVER reaching the wire
                    # (the subscription never armed, so arm-before-post never
                    # posted). "Nobody answered" would be a falsehood: nobody
                    # was ever ASKED. Take the undeliverable terminal instead.
                    logger.error(
                        "planning driver: run %s %s window (%ds) closed with "
                        "the card never published (the response subscription "
                        "never armed) — nobody was ever asked",
                        correlation_id,
                        log_noun,
                        wait_seconds,
                    )
                    answer = _DoorAnswer(
                        outcome="undeliverable", request_id=request_id
                    )
                    self._record_door_outcome(
                        correlation_id,
                        stage_label=stage_label,
                        details_key=details_key,
                        sentinel_outcome=sentinel_outcome,
                        persisted=persisted,
                        receipt_keys=receipt_keys,
                        answer=answer,
                    )
                    if on_close is not None:
                        await on_close(answer)
                    return answer
                logger.warning(
                    "planning driver: run %s %s window (%ds) closed with no "
                    "answer",
                    correlation_id,
                    log_noun,
                    wait_seconds,
                )
                answer = _DoorAnswer(outcome="timed_out", request_id=request_id)
                self._record_door_outcome(
                    correlation_id,
                    stage_label=stage_label,
                    details_key=details_key,
                    sentinel_outcome=sentinel_outcome,
                    persisted=persisted,
                    receipt_keys=receipt_keys,
                    answer=answer,
                )
                if on_close is not None:
                    await on_close(answer)
                return answer

            armed: asyncio.Event = asyncio.Event()
            subscriber = deps.subscriber_factory(expected_approver, armed)
            wait_started = time.monotonic()
            wait_task = asyncio.create_task(
                subscriber.await_response(
                    plan_run_id,
                    stage_label=stage_label,
                    attempt_count=attempt_count,
                    timeout_seconds=max(1, int(remaining)),
                )
            )
            try:
                await asyncio.wait_for(armed.wait(), timeout=_ARM_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(
                    "planning driver: %s subscription failed to arm for %s "
                    "within %.0fs; retrying",
                    log_noun,
                    correlation_id,
                    _ARM_TIMEOUT_SECONDS,
                )
                await self._cancel_waiter(wait_task, correlation_id)
                await asyncio.sleep(1.0)  # anti-spin: never tight-loop arming
                continue

            if not published:
                # Arm-before-post: the subscription is live, NOW put the card in
                # front of the owner (a response cannot outrun the waiter).
                try:
                    await deps.approval_publisher.publish_request(envelope)
                except Exception:  # noqa: BLE001 — an undeliverable card is honest
                    logger.exception(
                        "planning driver: %s card publish failed for %s "
                        "(request_id=%s); taking the honest terminal",
                        log_noun,
                        correlation_id,
                        request_id,
                    )
                    await self._cancel_waiter(wait_task, correlation_id)
                    answer = _DoorAnswer(
                        outcome="undeliverable", request_id=request_id
                    )
                    self._record_door_outcome(
                        correlation_id,
                        stage_label=stage_label,
                        details_key=details_key,
                        sentinel_outcome=sentinel_outcome,
                        persisted=persisted,
                        receipt_keys=receipt_keys,
                        answer=answer,
                    )
                    if on_close is not None:
                        await on_close(answer)
                    return answer
                published = True
                await self._notify(
                    correlation_id,
                    open_message(expected_approver, wait_seconds),
                    level="info",
                )

            try:
                response = await wait_task
            except Exception:  # noqa: BLE001 — a waiter defect must not kill the run
                logger.exception(
                    "planning driver: %s waiter raised for %s; retrying inside "
                    "the window",
                    log_noun,
                    correlation_id,
                )
                await asyncio.sleep(1.0)
                continue

            if response is None:
                # This waiter's own window expired; the door stays open until
                # OUR deadline. Anti-spin: a defective/empty subscriber that
                # returns instantly must not hot-loop the daemon.
                if time.monotonic() - wait_started < 1.0:
                    await asyncio.sleep(1.0)
                continue

            # Stale-round guard: anything that is not THIS card is ignored (a
            # late product-docs response, a superseded door round).
            if response.request_id != request_id:
                logger.warning(
                    "planning driver: response request_id=%s is not the %s card "
                    "%s for %s; ignoring",
                    response.request_id,
                    log_noun,
                    request_id,
                    correlation_id,
                )
                await asyncio.sleep(1.0)
                continue

            # Per-run approver pinning (verbatim equality, JNB-101/104).
            if expected_approver and response.decided_by != expected_approver:
                logger.warning(
                    "planning driver: %s responder mismatch for %s (got %s, "
                    "expected %s); the door stays open",
                    log_noun,
                    correlation_id,
                    response.decided_by,
                    expected_approver,
                )
                await asyncio.sleep(1.0)
                continue

            answer = _DoorAnswer(
                outcome=decide(response),
                request_id=request_id,
                decided_by=response.decided_by,
                decided_by_name=_responder_display_name(response),
                decision=str(response.decision),
                notes=(str(response.notes) if getattr(response, "notes", None) else None),
                item_answers=_item_answers(response),
            )
            self._record_door_outcome(
                correlation_id,
                stage_label=stage_label,
                details_key=details_key,
                sentinel_outcome=sentinel_outcome,
                persisted=persisted,
                receipt_keys=receipt_keys,
                answer=answer,
            )
            if on_close is not None:
                await on_close(answer)
            return answer

    async def _auth_surface_confirmation_door(
        self, row: Any, correlation_id: str, *, seed: Mapping[str, Any], basis: str
    ) -> str:
        """Ask the owner to confirm a flagged seed is authless; wait for the tap.

        A THIN CALLER of :meth:`_inline_confirmation_door` — every mechanic
        (durable-before-wire, arm-before-post, the stale guard, approver
        pinning, verbatim re-open, the idempotency sentinel) lives there and is
        shared with the spec-digest door, so the two can never drift apart.

        Returns ``"confirmed"`` (the caller proceeds exactly as the unflagged
        path), ``"rejected"``, ``"deferred"``, ``"timed_out"`` or
        ``"undeliverable"`` — each of the last four mapping to the caller's
        honest terminal, which NAMES which of them happened.
        """
        basis_lines = self._auth_basis_lines(basis)

        def _rehydrate(persisted: Mapping[str, Any], wait_seconds: int) -> dict[str, Any]:
            lines = [
                str(line) for line in (persisted.get("basis_lines") or []) if str(line)
            ] or basis_lines
            return self._auth_confirmation_card(
                seed=seed, basis_lines=lines, wait_seconds=wait_seconds
            )

        def _decide(response: Any) -> str:
            if response.decision in ("approve", "override"):
                return "confirmed"
            if response.decision == "reject":
                return "rejected"
            # The door asks for two answers, but it rides the SAME generic
            # approval consumer as the product-docs checkpoint — whose
            # ``decision`` literal also carries ``defer``. An answer that
            # decides nothing is still an ANSWER: swallowing it and later
            # reporting "nobody answered" would be a falsehood told to the very
            # person who answered. So the door closes on it, the durable row
            # records WHICH answer it was, and the terminal names it.
            return "deferred"

        async def _closed(answer: _DoorAnswer) -> None:
            if answer.outcome == "confirmed":
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id}: {answer.decided_by} "
                    "confirmed there is no sign-in here — registering the "
                    "quality checklist as authless and carrying on with the build.",
                    level="info",
                )
                logger.info(
                    "planning driver: run %s auth surface confirmed authless by "
                    "%s; the pass-bar leg proceeds as the unflagged path",
                    correlation_id,
                    answer.decided_by,
                )
            elif answer.outcome == "rejected":
                logger.info(
                    "planning driver: run %s auth surface CONFIRMED REAL by %s; "
                    "attended registration it is",
                    correlation_id,
                    answer.decided_by,
                )
            elif answer.outcome == "deferred":
                logger.info(
                    "planning driver: auth-confirmation answer %r from %s for %s "
                    "decided nothing (a 'later' round); the door closes and the "
                    "run takes the honest terminal naming it",
                    answer.decision,
                    answer.decided_by,
                    correlation_id,
                )

        answer = await self._inline_confirmation_door(
            row,
            correlation_id,
            stage_label=_AUTH_CONFIRM_STAGE,
            details_key="auth_confirmation",
            checkpoint_type=_AUTH_CONFIRM_CHECKPOINT_TYPE,
            rationale=_AUTH_CONFIRM_RATIONALE,
            sentinel_outcome="confirmed",
            answered_outcome="confirmed",
            persisted={"basis_lines": basis_lines},
            # The flagged lines stay on the verdict: the pass-bars leg copies
            # this row onto its own receipt, and a receipt that did not say
            # WHAT was confirmed would be a worse receipt.
            receipt_keys=("basis_lines",),
            rehydrate=_rehydrate,
            decide=_decide,
            open_message=lambda approver, wait: self._auth_door_open_message(
                correlation_id, expected_approver=approver, wait_seconds=wait
            ),
            log_noun="auth-surface confirmation door",
            on_close=_closed,
        )
        return answer.outcome

    @staticmethod
    async def _cancel_waiter(wait_task: "asyncio.Task[Any]", correlation_id: str) -> None:
        """Cancel a response waiter and swallow its unwind (never mask a bug)."""
        wait_task.cancel()
        try:
            await wait_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — surface the root cause, keep driving
            logger.exception(
                "planning driver: response waiter failed while cancelling for %s",
                correlation_id,
            )

    def _door_attempt(self, correlation_id: str, stage_label: str) -> int:
        """How many doors this run has already opened at ``stage_label``.

        Counts ``GATED`` rows ONLY: a ``reopened`` row is the SAME door
        re-emitted after a restart, never a new round, so it must not bump the
        attempt (that is what keeps the recovered card's ``request_id``
        identical to the one already in front of the owner).
        """
        return sum(
            1
            for event in self._deps.store.list_events(correlation_id)
            if event["stage_label"] == stage_label and event["status"] == "GATED"
        )

    def _open_door(
        self, correlation_id: str, stage_label: str, details_key: str
    ) -> dict[str, Any] | None:
        """The still-OPEN door's persisted details, or ``None`` if none is open.

        The durable event log is a door's row of record (the checkpoint keeps
        its pending id on the run row; a machine-chain leg keeps its own on its
        leg events). A door is OPEN exactly when the LAST event for
        ``stage_label`` is an opening (:data:`_AUTH_DOOR_OPEN_STATUSES`) — every
        verdict status closes it. Callers re-emit the returned ``request_id`` /
        ``attempt_count`` and the persisted card VERBATIM; a row too corrupt to
        carry a ``request_id`` reads as no open door (a fresh, answerable card
        beats a card nobody can identify).
        """
        latest: dict[str, Any] | None = None
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] != stage_label:
                continue
            if event["status"] not in _AUTH_DOOR_OPEN_STATUSES:
                latest = None  # a verdict — the door is closed
                continue
            try:
                details = json.loads(event["details_json"] or "{}") or {}
            except (json.JSONDecodeError, ValueError):
                latest = None
                continue
            record = details.get(details_key)
            latest = (
                dict(record)
                if isinstance(record, Mapping) and record.get("request_id")
                else None
            )
        return latest

    def _record_door_outcome(
        self,
        correlation_id: str,
        *,
        stage_label: str,
        details_key: str,
        sentinel_outcome: str,
        persisted: Mapping[str, Any],
        receipt_keys: tuple[str, ...],
        answer: "_DoorAnswer",
    ) -> None:
        """Write a door's durable verdict row.

        The caller's ``sentinel_outcome`` records ``status="approved"`` — the
        idempotency sentinel :meth:`_has_leg_event` reads. Every other verdict
        records its own status so the audit log says WHICH way the door closed.
        None of these statuses is ``checkpoint_cleared``, so the pure planner's
        history projection is untouched (these are machine-chain legs, not
        Mode-P checkpoints).

        The owner's own free-text NOTE is persisted whenever they left one, and
        so are the per-item answers that rode the same response. The sign-in
        door used to discard the note; one door means one behaviour, and a note
        somebody took the trouble to write is not something to throw away.

        ``receipt_keys`` is the subset of the caller's ``persisted`` fields that
        belongs on a VERDICT row, and it exists to keep the event log from
        carrying the same bytes over and over. The card the owner read is
        replayed from the OPENING row (:meth:`_open_door` reads openings only),
        so re-writing it onto every verdict bought nothing while writing the
        whole ``.feature`` text into SQLite once per verdict on top of once per
        opening. What a verdict row is FOR — who decided, which way, in their
        own words, plus whatever the leg's later receipt quotes — is unchanged.
        """
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=stage_label,
            status=(
                "approved" if answer.outcome == sentinel_outcome else answer.outcome
            ),
            actor_identity=answer.decided_by or "planning-driver",
            details_json=json.dumps(
                {
                    details_key: {
                        "outcome": answer.outcome,
                        "request_id": answer.request_id,
                        "decided_by": answer.decided_by,
                        **{
                            key: persisted[key]
                            for key in receipt_keys
                            if key in persisted
                        },
                        # The literal wire answer, when the owner gave one — so
                        # the record says what they actually tapped, never just
                        # "no answer".
                        **({"decision": answer.decision} if answer.decision else {}),
                        **({"notes": answer.notes} if answer.notes else {}),
                        **(
                            {"item_answers": dict(answer.item_answers)}
                            if answer.item_answers
                            else {}
                        ),
                    }
                }
            ),
        )

    @staticmethod
    def _auth_basis_lines(basis: str) -> list[str]:
        """The seed's OWN words for why it flagged, one entry per line."""
        lines = [line.strip() for line in str(basis).splitlines()]
        return [line for line in lines if line] or ["no basis supplied"]

    def _auth_confirmation_card(
        self, *, seed: Mapping[str, Any], basis_lines: list[str], wait_seconds: int
    ) -> dict[str, Any]:
        """The owner's card — PLAIN language, no jargon, no internal ids.

        Rich reads this in Slack. It says what tripped the check (his spec's own
        words, quoted verbatim), what CONFIRM does, what REJECT does, and what
        happens if he never answers. Nothing here is a stage label, a task id or
        a clause reference dressed up as a question.
        """
        card: dict[str, Any] = {
            "checkpoint": _AUTH_CONFIRM_CHECKPOINT_TYPE,
            "title": "Does this feature sit behind a sign-in?",
            "what_happened": (
                "The spec checker flagged this feature as sitting behind a "
                "sign-in, so its quality checklist — the checks each task has to "
                "pass — were not registered automatically. That check is a "
                "keyword scan of the spec text, so it fires just as readily on "
                "a spec that proves the feature needs NO sign-in at all."
            ),
            "flagged_lines": list(basis_lines),
            "confirm_means": (
                "Confirm — there is no sign-in here: register the quality checklist "
                "as authless and let the build carry on, exactly as it would "
                "for an unflagged feature."
            ),
            "reject_means": (
                "Reject — this really is behind a sign-in: stop the run so the "
                "bars get registered attended, by hand."
            ),
            "later_means": (
                "Setting this aside for later stops the run too — nothing gets "
                "registered, and it says so rather than pretending nobody "
                "answered. Start the feature again when you want to decide it."
            ),
            "no_answer_means": (
                f"No answer within {self._plain_wait(wait_seconds)}: the run "
                "stops, the same as Reject."
            ),
        }
        feature = str(seed.get("feature_slug") or "").strip()
        if feature:
            card["feature"] = feature
        return card

    def _auth_door_open_message(
        self, correlation_id: str, *, expected_approver: str | None, wait_seconds: int
    ) -> str:
        """The plain-language ping that says the run is WAITING, not broken."""
        who = expected_approver or "the run's approver"
        return (
            f"Planning run {correlation_id}: the spec checker flagged this "
            "feature as sitting behind a sign-in, which is often a false alarm "
            "— nothing has failed. "
            f"{who} has a card to decide: confirm there is no sign-in and the "
            "quality checklist registers as authless and the build carries on; "
            "reject and the run stops so they can be registered attended. No "
            f"answer within {self._plain_wait(wait_seconds)} stops the run too."
        )

    @staticmethod
    def _plain_wait(seconds: int) -> str:
        """Humanise a wait window (the owner reads minutes and hours)."""
        total = max(1, int(seconds))
        if total < 60:
            return f"{total} second" if total == 1 else f"{total} seconds"
        minutes = total // 60
        if minutes < 60:
            return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
        hours = minutes / 60
        text = f"{hours:.1f}".rstrip("0").rstrip(".")
        return f"{text} hour" if text == "1" else f"{text} hours"

    async def _register_feature_gate_leg(
        self, row: Any, correlation_id: str
    ) -> bool:
        """Register a per-feature live GATE from the 007 seed at plan-commit (F2).

        The exact sibling of :meth:`_register_pass_bars_leg`. The pass-bar leg
        registers the pass BARS the B2 precondition demands; this leg closes the
        matching gap on the OTHER side — at plan-commit the machine flow
        registered bars but no live GATE, so the post-deploy live-gate only
        proved "the deployment passes health+stats", never "the NEW endpoint
        passes a REGISTERED gate". Forge derives the endpoint from the seed's
        machine criteria, fills the target repo's OWN feature-behaviour gate
        TEMPLATE, appends a mirrored GateEntry to its OWN gate registry, and
        commits both as ONE commit AFTER the bars commit and BEFORE the build
        trigger.

        SKIP-vs-FAIL law (BINDING):
          (a) no machine criterion yields a GET ``{method,path}`` → honest
              ``skipped`` leg event ("no derivable endpoint — no gate
              registered") and CONTINUE to the build (non-endpoint features are
              legitimate);
          (b) the target repo carries no ``qa/gates/feature_behaviour_gate.py``
              template or no ``qa/gates/registry.yaml`` on the branch → the same
              honest skip (the repo has not adopted the F4 gate surface);
          (c) template + derivable but the fill / validate / commit fails → LOUD
              :meth:`_fail_leg` (same posture as the bars leg);
          (d) an auth-flagged seed only reaches this leg CONFIRMED authless —
              the bars leg's confirmation door is the single place that
              question is asked, and a rejected / unanswered door fails the
              bars leg first — so no auth handling is re-implemented here.

        Idempotent: a durable ``qa-feature-gate`` event (approved, whether a real
        registration OR an honest skip) short-circuits a re-drive. Returns True
        to keep driving (skip AND success both proceed to the B3 trigger), False
        on a loud terminal failure.
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _QA_FEATURE_GATE_STAGE):
            logger.info(
                "planning driver: run %s feature gate already resolved "
                "(idempotent re-drive — proceeding to the B3 build trigger)",
                correlation_id,
            )
            return True

        if deps.validate_gate_registry is None:
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                "target terminal ON but the gate-registry validate collaborator "
                "(validate_gate_registry) is not wired",
            )

        # The seed the bars leg consumed (persisted on the durable feature-spec
        # event). It is authless here — either by construction or by the owner's
        # confirmation at the bars leg's auth door, which runs BEFORE this leg
        # and fails the run on a reject/no-answer, so we never re-ask that
        # question or re-implement the refusal. A missing/unparseable seed can only
        # reach here if the bars leg already failed (it guards the same seed), so
        # treat it defensively as an honest skip: nothing derivable, no gate.
        spec_details = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE)
        raw_seed = spec_details.get("pass_bar_seed")
        seed: Any = None
        if raw_seed:
            try:
                seed = yaml.safe_load(raw_seed)
            except yaml.YAMLError:
                seed = None
        criteria = (
            seed.get("criteria") if isinstance(seed, Mapping) else None
        ) or []

        # The digest's optional endpoint field first (the spec author stated it
        # outright); the criterion-prose regex stays as the fallback so every
        # feature that registers a gate today still registers one.
        endpoint = self._feature_gate_endpoint_from_digest(
            spec_details.get("digest")
        ) or self._derive_feature_gate_endpoint(criteria)
        if endpoint is None:
            return self._skip_feature_gate(
                correlation_id,
                "no derivable endpoint — no gate registered",
            )

        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        feature_id = str(plan_details.get("feature_id") or "")
        plan_sha = str(plan_details.get("sha") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        if not feature_id or not plan_sha:
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                "no feature id / plan commit sha recorded on the feature-plan "
                "leg — cannot register the per-feature live gate",
            )

        # The first minted pass bar is the gate's ``pass_bar_ref`` (the gate and
        # its bar share the feature's first task). No bars ⇒ nothing to reference
        # ⇒ honest skip (a validated plan with zero tasks registers no gate).
        bars_details = self._leg_event_details(correlation_id, _QA_PASS_BARS_STAGE)
        bar_files = sorted(str(f) for f in (bars_details.get("bar_files") or []))
        if not bar_files:
            return self._skip_feature_gate(
                correlation_id,
                "the plan registered no pass bars — no gate pass_bar_ref to "
                "anchor; no gate registered",
            )
        pass_bar_ref = bar_files[0]

        resolved = await self._resolve_repo(
            row, correlation_id, stage_label=_QA_FEATURE_GATE_STAGE
        )
        if resolved is None:
            return False
        _target_repo, repo_path = resolved

        # (b) The repo's OWN gate surface must exist on the branch — never
        # fabricated forge-side. Absent ⇒ the repo has not adopted the F4 gate
        # surface ⇒ honest skip.
        template = await deps.git_runner.read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=_FEATURE_GATE_TEMPLATE_REL
        )
        if not template:
            return self._skip_feature_gate(
                correlation_id,
                f"the target repo carries no {_FEATURE_GATE_TEMPLATE_REL} "
                "template on the branch — the F4 gate surface is not adopted; "
                "no gate registered",
            )
        registry_raw = await deps.git_runner.read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=_GATE_REGISTRY_REL
        )
        if not registry_raw:
            return self._skip_feature_gate(
                correlation_id,
                f"the target repo carries no {_GATE_REGISTRY_REL} on the branch "
                "— the F4 gate surface is not adopted; no gate registered",
            )

        # Derive the slug + snake filename deterministically from the seed.
        raw_slug = str(seed.get("feature_slug") or "").strip()
        slug = self._sanitise_slug(raw_slug) or feature_id.lower()
        slug_snake = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
        if not slug_snake:
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                f"could not derive a gate filename from feature_slug={raw_slug!r} "
                f"/ feature_id={feature_id}",
            )
        gate_rel = f"qa/gates/{slug_snake}_gate.py"

        # (c) Fill the template + append the registry entry. A drift in the
        # template's fillable literals, or a malformed registry, is a LOUD fail.
        try:
            filled_gate = self._fill_feature_gate(template, slug, endpoint)
            new_registry = self._append_gate_registry_entry(
                registry_raw,
                gate_id=slug,
                gate_path=gate_rel,
                pass_bar_ref=pass_bar_ref,
            )
        except _FeatureGateFillError as exc:
            return await self._fail_leg(
                correlation_id, _QA_FEATURE_GATE_STAGE, str(exc)
            )

        validate = deps.validate_gate_registry

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            # Run guardkit's OWN ``qa validate gate-registry`` on the appended
            # registry so a malformed forge-appended entry fails the leg BEFORE
            # it lands (never an entry the post-deploy live-gate would reject).
            outcome = await validate(worktree, _GATE_REGISTRY_REL)
            return PreCommitResult(ok=outcome.ok, detail=outcome.detail)

        files = {gate_rel: filled_gate, _GATE_REGISTRY_REL: new_registry}
        try:
            gitres = await deps.git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=(
                    f"planning: register the per-feature live gate for "
                    f"{correlation_id} ({feature_id}, GET {endpoint['path']} — "
                    "F2 seed derivation)"
                ),
                pre_commit=_pre_commit,
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                f"feature-gate write raised {type(exc).__name__}: {exc}",
            )
        if gitres.status == "failed":
            return await self._fail_leg(
                correlation_id,
                _QA_FEATURE_GATE_STAGE,
                f"feature-gate write / qa validate gate-registry failed: "
                f"{gitres.stderr}",
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_FEATURE_GATE_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "feature_id": feature_id,
                    "gate_file": gate_rel,
                    "registry_file": _GATE_REGISTRY_REL,
                    "gate_id": slug,
                    "endpoint": endpoint,
                    "pass_bar_ref": pass_bar_ref,
                    "registered_at_sha": plan_sha,
                    "sha": gitres.sha,
                    "branch": branch,
                }
            ),
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: registered a live gate for "
            f"{feature_id} (GET {endpoint['path']} → {gate_rel}) on branch "
            f"{branch} before queueing the build.",
            level="info",
        )
        logger.info(
            "planning driver: run %s registered feature gate %s (endpoint=GET "
            "%s, feature_id=%s, plan_sha=%s)",
            correlation_id,
            gate_rel,
            endpoint["path"],
            feature_id,
            plan_sha,
        )
        return True

    def _skip_feature_gate(self, correlation_id: str, reason: str) -> bool:
        """Record an HONEST skipped ``qa-feature-gate`` leg event and CONTINUE.

        A non-endpoint feature (or a repo that has not adopted the qa/gates/
        surface) is a legitimate no-gate outcome, not a failure: record the
        durable label (so a re-drive is a clean no-op) with a ``skipped`` detail
        and return True so the caller proceeds to the B3 build trigger. Zero
        target-repo writes.
        """
        logger.info(
            "planning driver: run %s feature gate skipped — %s",
            correlation_id,
            reason,
        )
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_FEATURE_GATE_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps({"skipped": True, "reason": reason}),
        )
        return True

    @staticmethod
    def _feature_gate_endpoint_from_digest(digest_text: Any) -> dict[str, str] | None:
        """The spec digest's OPTIONAL ``endpoint`` field, or None.

        The digest is a machine-readable artifact of the four-file contract and
        the spec author knows the method and path first-hand, so when the field
        is present it beats re-deriving them from criterion prose. The field is
        optional by design: a feature that is not an HTTP endpoint omits it and
        falls through to the prose path, then to an honest skip.

        Applies the SAME v1 restriction as :meth:`_derive_get_endpoint` — GET
        only, the sole verb whose happy-path status forge knows (200). A digest
        naming any other verb is not a wider gate, it is no gate: forge would
        have to guess a success status, which it never does.
        """
        try:
            obj = yaml.safe_load(str(digest_text or ""))
        except yaml.YAMLError:
            return None
        if not isinstance(obj, Mapping):
            return None
        endpoint = obj.get("endpoint")
        if not isinstance(endpoint, Mapping):
            return None
        method = str(endpoint.get("method") or "").strip().upper()
        path = str(endpoint.get("path") or "").strip()
        if method != "GET" or not path.startswith("/"):
            return None
        return {"method": "GET", "path": path}

    def _derive_feature_gate_endpoint(
        self, criteria: Any
    ) -> dict[str, str] | None:
        """First machine criterion that yields a v1 (GET) gate endpoint, or None.

        Iterates the seed's criteria in order; for each ``class: machine``
        criterion applies :meth:`_derive_get_endpoint` to its text and returns
        the first match. Non-machine criteria, non-GET verbs, missing paths and
        prose all yield nothing — an honest skip rather than a guessed gate.
        """
        if not isinstance(criteria, list):
            return None
        for crit in criteria:
            if not isinstance(crit, Mapping):
                continue
            if str(crit.get("class")) != "machine":
                continue
            endpoint = self._derive_get_endpoint(str(crit.get("text") or ""))
            if endpoint is not None:
                return endpoint
        return None

    @staticmethod
    def _derive_get_endpoint(text: str) -> dict[str, str] | None:
        """Conservative v1 endpoint derivation from one criterion's free text.

        Returns ``{"method": "GET", "path": "/…"}`` ONLY when the strict grammar
        matches AND the verb is GET (the sole verb whose happy-path status forge
        knows: 200). Any other verb, a missing path, wrong case, or mid-sentence
        prose returns None (skip — forge never guesses a success status).
        """
        match = _FEATURE_GATE_ENDPOINT_RE.search(text)
        if match is None or match.group("method") != "GET":
            return None
        return {"method": "GET", "path": match.group("path")}

    @staticmethod
    def _fill_feature_gate(
        template: str, gate_id: str, endpoint: Mapping[str, str]
    ) -> str:
        """Fill the target repo's OWN feature-behaviour gate template (F2).

        Substitutes ONLY the two fillable SPEC literals (gate_id + the request
        method/path), leaving the rest of the template — including the
        ``/REPLACE_ME`` runtime guard elsewhere — byte-untouched. v1 derives NO
        json_assertions from free text (the filled gate asserts status + valid
        JSON body + the template's own header defaults). Each literal must appear
        EXACTLY once; a mismatch means the repo's template drifted from the shape
        forge fills — raise so the caller fails the leg loudly (never a silently
        wrong gate).
        """
        if template.count(_GATE_TEMPLATE_GATE_ID_LITERAL) != 1:
            raise _FeatureGateFillError(
                "the target repo's feature-behaviour gate template does not "
                f"carry the fillable gate_id literal exactly once "
                f"({_GATE_TEMPLATE_GATE_ID_LITERAL!r}) — template drift; "
                "refusing to fill a wrong gate"
            )
        if template.count(_GATE_TEMPLATE_REQUEST_LITERAL) != 1:
            raise _FeatureGateFillError(
                "the target repo's feature-behaviour gate template does not "
                f"carry the fillable request literal exactly once "
                f"({_GATE_TEMPLATE_REQUEST_LITERAL!r}) — template drift; "
                "refusing to fill a wrong gate"
            )
        method = endpoint["method"]
        path = endpoint["path"]
        filled = template.replace(
            _GATE_TEMPLATE_GATE_ID_LITERAL, f'"gate_id": "{gate_id}",'
        )
        filled = filled.replace(
            _GATE_TEMPLATE_REQUEST_LITERAL,
            f'"request": {{"method": "{method}", "path": "{path}"}},',
        )
        return filled

    @staticmethod
    def _append_gate_registry_entry(
        registry_raw: str,
        *,
        gate_id: str,
        gate_path: str,
        pass_bar_ref: str,
    ) -> str:
        """Append one mirrored GateEntry to the repo's OWN gate registry (F2).

        Reads the existing registry, MIRRORS a sibling entry's ``target
        {base_url_env, environment_id}``, ``preconditions``, ``preflight`` and
        ``evidence_dir_pattern`` (never hardcoding ``API_TEST_*``), points the new
        entry at ``gate_path`` with the first minted bar as ``pass_bar_ref``, and
        TEXTUALLY appends the rendered block so the existing entries + header
        comments stay byte-untouched. Raises when the registry is unparseable or
        carries no sibling entry to mirror (a loud-fail signal).
        """
        try:
            data = yaml.safe_load(registry_raw)
        except yaml.YAMLError as exc:
            raise _FeatureGateFillError(
                f"the target repo's {_GATE_REGISTRY_REL} is not parseable "
                f"YAML: {exc}"
            ) from exc
        gates = data.get("gates") if isinstance(data, Mapping) else None
        if not isinstance(gates, list) or not gates:
            raise _FeatureGateFillError(
                f"the target repo's {_GATE_REGISTRY_REL} carries no gate entry "
                "to mirror — cannot copy the sibling's base_url_env / "
                "preconditions / preflight / evidence_dir_pattern"
            )
        sibling = next((g for g in gates if isinstance(g, Mapping)), None)
        if sibling is None:
            raise _FeatureGateFillError(
                f"the target repo's {_GATE_REGISTRY_REL} gates list carries no "
                "mapping entry to mirror"
            )
        sib_target = sibling.get("target") if isinstance(sibling, Mapping) else {}
        base_url_env = (
            str(sib_target.get("base_url_env"))
            if isinstance(sib_target, Mapping) and sib_target.get("base_url_env")
            else None
        )
        if not base_url_env:
            raise _FeatureGateFillError(
                f"the sibling gate entry in {_GATE_REGISTRY_REL} carries no "
                "target.base_url_env to mirror"
            )
        environment_id = (
            str(sib_target.get("environment_id"))
            if isinstance(sib_target, Mapping) and sib_target.get("environment_id")
            else "local"
        )
        entry: dict[str, Any] = {
            "id": gate_id,
            "path": gate_path,
            "target": {
                "base_url_env": base_url_env,
                "environment_id": environment_id,
            },
            "preconditions": list(sibling.get("preconditions") or []),
            "preflight": list(sibling.get("preflight") or []),
            "pass_bar_ref": pass_bar_ref,
            "evidence_dir_pattern": str(
                sibling.get("evidence_dir_pattern")
                or "qa/gates/evidence/{run_id}"
            ),
        }
        block = yaml.safe_dump(
            [entry], sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        # Indent the top-level list block by two spaces so it nests under the
        # existing ``gates:`` key, and append after the last existing entry.
        indented = "".join(
            ("  " + line if line.strip() else line)
            for line in block.splitlines(keepends=True)
        )
        return registry_raw.rstrip("\n") + "\n" + indented

    # -- target-terminal helpers ---------------------------------------- #

    def _run_data(self, row: Any, correlation_id: str) -> dict[str, Any]:
        """Assemble the run_data dict the handoff-content builder consumes."""
        po_output = self._latest_po_output(correlation_id)
        return {
            "correlation_id": correlation_id,
            "state": row["state"],
            "request_text": row["request_text"],
            "originating_user": row["originating_user"],
            "target_repo": row["target_repo"],
            "product_docs": po_output.get("docs_summary") or {},
        }

    async def _resolve_repo(
        self, row: Any, correlation_id: str, *, stage_label: str
    ) -> tuple[str, str] | None:
        """Resolve ``(target_repo, repo_path)`` or fail the run loudly."""
        cfg = self._deps.planning_config
        named = row["target_repo"]
        target_repo = named or cfg.default_target_repo
        if target_repo is None:
            await self._fail_leg(
                correlation_id, stage_label, "no target repository configured"
            )
            return None
        if not named:
            # The assumed repository is said OUT LOUD (2026-09-05 rule 6). A
            # sentence that named no repository still lands somewhere, and the
            # log is where that "somewhere" has to be readable afterwards.
            logger.info(
                "planning driver: run %s named no repository; building in the "
                "default %s",
                correlation_id,
                target_repo,
            )
        repo_path = cfg.target_repo_paths.get(target_repo)
        if repo_path is None:
            await self._fail_leg(
                correlation_id,
                stage_label,
                f"target repo {target_repo} not in target_repo_paths; "
                f"known repos: {format_known_repos(cfg.target_repo_paths)}",
            )
            return None
        return target_repo, repo_path

    @staticmethod
    def _dispatch_failure_message(correlation_id: str, decision: Fail) -> str:
        """The owner's sentence for a planner-reported dispatch failure.

        The planner's ``reason`` is machine text ("Dispatch failed for
        feature-spec: <detail>"). The owner reads the stage's PLAIN name and the
        dispatch's own detail — never the internal label. The machine string is
        untouched on the durable row and in the logs.
        """
        stage = getattr(decision, "stage", None)
        reason = str(decision.reason or "").strip()
        if stage is None:
            return f"Planning run {correlation_id} stopped: {reason}"
        prefix = f"Dispatch failed for {stage.value}"
        detail = reason[len(prefix) :] if reason.startswith(prefix) else reason
        detail = detail.lstrip(":").strip()
        stopped = (
            f"Planning run {correlation_id} stopped at {plain_stage_name(stage.value)}"
        )
        return f"{stopped}: {detail}" if detail else f"{stopped}."

    async def _fail_leg(
        self,
        correlation_id: str,
        stage_label: str,
        reason: str,
        *,
        owner_message: str | None = None,
    ) -> bool:
        """Move the run to FAILED, notify, and return False (loud terminal).

        TWO AUDIENCES, deliberately split (the 2026-07-31 stage-names ruling):

        * the MACHINE record — the durable FAILED row and the logs — keeps
          ``stage_label`` and ``reason`` VERBATIM. Grep, correlation and every
          receipt depend on them being the internal strings;
        * the OWNER's message — what lands in Slack — names the stage by its
          PLAIN NAME (:func:`plain_stage_name`, the noun's single source), never
          the internal label.

        ``owner_message`` lets a leg with something genuinely human to say
        compose its own sentence (the auth door does). When it is omitted the
        default sentence is composed here: the plain noun plus the leg's reason.
        """
        async def _notify_owner(cid: str, message: str) -> str:
            return await self._notify(cid, message, level="error")

        return await fail_run(
            self._deps.store,
            correlation_id,
            stage_label=stage_label,
            reason=reason,
            owner_message=owner_message
            or (
                f"Planning run {correlation_id} stopped at "
                f"{plain_stage_name(stage_label)}: {reason}"
            ),
            notify=_notify_owner,
            log=logger,
        )

    def _mint_feature_id(self, correlation_id: str) -> str:
        """Mint a deterministic ``FEAT-XXXX`` id for the plan (rule 6).

        Deterministic in ``correlation_id`` so a re-drive mints the SAME id,
        and validated through the identifier security boundary before it is
        threaded to the architect (008) and asserted on the returned plan.
        """
        digest = hashlib.sha1(correlation_id.encode("utf-8")).hexdigest()[:4].upper()
        return validate_feature_id(f"FEAT-{digest}")

    @staticmethod
    def _dispatch_ok(result: Any) -> tuple[bool, str]:
        """Map a StageDispatchResult onto ``(ok, reason)`` (M10 outcome shape)."""
        outcome = getattr(result, "outcome", None)
        value = str(getattr(outcome, "value", outcome or "error")).lower()
        if value in ("completed", "degraded"):
            return True, value
        reason = getattr(result, "reason", None) or "no reason supplied"
        return False, f"{value}: {reason}"

    @staticmethod
    def _role_output_of(result: Any) -> dict[str, Any]:
        """Project the specialist's ``role_output`` document as a dict (M10).

        The deployed reply nests the role's NATIVE artifact map one level down:
        the specialist wraps every reply via ``wrap_role_output`` (specialist-
        agent adapters/result_wrapper.py) into
        ``{role_id, coach_score, criterion_breakdown, detection_findings,
        role_output: <native map>}``, and forge's reply parser
        (``_extract_role_output``) already unwraps that envelope so the driver
        receives the NATIVE map directly at ``result.role_output``.

        This projection is belt-and-braces against BOTH nesting levels: if the
        value handed through is itself a ``wrap_role_output`` envelope (a doubly
        wrapped payload — the ``role_output`` key still carries a Mapping), it
        descends one level so the caller always gets the bare native artifact
        map. A native map never carries a ``role_output`` key (its keys are
        artifact filenames / repo paths), so the descent is unambiguous.
        """
        ro = getattr(result, "role_output", None)
        if not isinstance(ro, Mapping):
            return {}
        inner = ro.get("role_output")
        if isinstance(inner, Mapping):
            ro = inner
        # The native map is a SessionResult.model_dump() (specialist-agent
        # session/types.py): the artifact map itself sits under its
        # ``artifacts`` key ({filename: content}), beside run_id/success/
        # final_score/etc. Descend to it when present (B4 run 5392685a — the
        # wire truth; fixture wire_reply.json is generated by the specialist's
        # own wrap chain). A bare artifact map (no ``artifacts`` key) still
        # passes through unchanged.
        arts = ro.get("artifacts")
        if isinstance(arts, Mapping) and arts:
            return dict(arts)
        return dict(ro)

    @staticmethod
    def _sanitise_slug(raw: str) -> str | None:
        """Sanitise a raw token to a filesystem-safe slug, or ``None``.

        Collapses any run of disallowed characters to a single hyphen and trims
        leading/trailing hyphens; returns ``None`` when nothing usable remains.
        """
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw.strip()).strip("-")
        return cleaned if cleaned and _SLUG_RE.fullmatch(cleaned) else None

    @staticmethod
    def _slug_of(role_output: Mapping[str, Any], correlation_id: str) -> str:
        """Feature slug from the 007 result, or a deterministic fallback.

        Resolution order: an explicit ``role_output['slug']`` (sanitised); else
        the stem of the ``*.feature`` key in the native suffix-keyed artifact map
        (the DEPLOYED 007 shape — the filename carries the slug, e.g.
        ``uptime-endpoint.feature`` → ``uptime-endpoint``); else a deterministic
        ``feature-{cid}`` (WS1's semantic-slug emitter is §9 follow-on).
        """
        candidate = str(role_output.get("slug") or "").strip()
        if candidate and _SLUG_RE.fullmatch(candidate):
            return candidate
        for name in role_output:
            key = str(name)
            if key.endswith(_SPEC_FEATURE_SUFFIX):
                slug = PlanningRunDriver._sanitise_slug(
                    key[: -len(_SPEC_FEATURE_SUFFIX)]
                )
                if slug:
                    return slug
                break
        return f"feature-{correlation_id}"

    @staticmethod
    def _project_native_spec_triple(
        role_output: Mapping[str, Any], slug: str
    ) -> dict[str, str] | None:
        """Project the committed triple from the 007 NATIVE suffix-keyed map.

        The deployed 007 ``role_output`` is an artifact map keyed by BARE
        filename with the contract suffixes (``.feature`` /
        ``_assumptions.yaml`` / ``_summary.md``, plus ``_digest.yaml``) PLUS
        extras (a
        ``pass-bar-seed-*.yaml`` and the ``validation.json`` data channel).
        Requires EXACTLY one file per suffix; extras are tolerated but never
        committed. The committed paths are the canonical ``features/<slug>/``
        triple (``slug`` already resolved via ``_slug_of``). ``None`` when the
        map does not carry exactly one of each suffix.

        THE SPEC DIGEST IS COMMITTED TOO, and additively: when the reply carries
        exactly one ``<slug>_digest.yaml`` it lands beside the triple, so the
        planning branch holds the complete record of what a person approved —
        the plain-language list AND the examples it summarises. Its ABSENCE
        never breaks the projection: an older spec-writer still yields its
        three files, and the leg is the place that decides a missing digest is
        a loud stop (there is nothing safe to show without one).
        """
        by_suffix: dict[str, list[str]] = {
            _SPEC_FEATURE_SUFFIX: [],
            _SPEC_ASSUMPTIONS_SUFFIX: [],
            _SPEC_SUMMARY_SUFFIX: [],
            _SPEC_DIGEST_SUFFIX: [],
        }
        # Longest suffix first so ``_assumptions.yaml`` never loses to a broader
        # match; the four are mutually exclusive but order-independence is cheap.
        for name, content in role_output.items():
            key = str(name)
            for suffix in (
                _SPEC_ASSUMPTIONS_SUFFIX,
                _SPEC_DIGEST_SUFFIX,
                _SPEC_SUMMARY_SUFFIX,
                _SPEC_FEATURE_SUFFIX,
            ):
                if key.endswith(suffix):
                    by_suffix[suffix].append(str(content))
                    break
        required = (
            _SPEC_FEATURE_SUFFIX,
            _SPEC_ASSUMPTIONS_SUFFIX,
            _SPEC_SUMMARY_SUFFIX,
        )
        if any(len(by_suffix[suffix]) != 1 for suffix in required):
            return None
        base = f"features/{slug}"
        files = {
            f"{base}/{slug}{suffix}": by_suffix[suffix][0] for suffix in required
        }
        if len(by_suffix[_SPEC_DIGEST_SUFFIX]) == 1:
            files[f"{base}/{slug}{_SPEC_DIGEST_SUFFIX}"] = by_suffix[
                _SPEC_DIGEST_SUFFIX
            ][0]
        return files

    @staticmethod
    def _spec_triple_files(
        role_output: Mapping[str, Any], slug: str
    ) -> dict[str, str] | None:
        """Project the committed spec contract from the 007 role_output.

        Shape resolution (belt-and-braces):
          1. An explicit ``files`` mapping (specialist-authored repo-relative
             paths — the legacy/alternate shape), committed verbatim.
          2. The DEPLOYED native suffix-keyed artifact map (one ``*.feature`` /
             ``*_assumptions.yaml`` / ``*_summary.md`` plus tolerated extras),
             projected onto the canonical ``features/<slug>/`` triple.
          3. Field-based ``feature`` / ``assumptions`` / ``summary`` fallback.
        ``None`` when no shape yields a triple (the invalid-artifacts fail path).
        """
        files = role_output.get("files")
        if isinstance(files, Mapping) and files:
            return {str(k): str(v) for k, v in files.items()}
        native = PlanningRunDriver._project_native_spec_triple(role_output, slug)
        if native is not None:
            return native
        feature = role_output.get("feature")
        assumptions = role_output.get("assumptions")
        summary = role_output.get("summary")
        if feature and assumptions and summary:
            base = f"features/{slug}"
            return {
                f"{base}/{slug}.feature": str(feature),
                f"{base}/{slug}_assumptions.yaml": str(assumptions),
                f"{base}/{slug}_summary.md": str(summary),
            }
        return None

    @staticmethod
    def _plan_tree_files(role_output: Mapping[str, Any]) -> dict[str, str] | None:
        """Project the plan tree (feature/task/qa files) from the 008 role_output.

        Prefers an explicit ``files`` mapping; otherwise projects the DEPLOYED
        native 008 artifact map, whose keys are ALREADY repo-relative paths
        (``.guardkit/features/<id>.yaml``, ``tasks/backlog/**``, ``qa/*``) — the
        contract of record (specialist-agent architect/modes/feature_plan.py).
        The out-of-band validation channel and any envelope scalars
        (``_NON_ARTIFACT_KEYS``) are excluded so only real repo files commit.
        ``None`` when neither shape yields any committable file.
        """
        files = role_output.get("files")
        if isinstance(files, Mapping) and files:
            return {str(k): str(v) for k, v in files.items()}
        tree = {
            str(k): str(v)
            for k, v in role_output.items()
            if isinstance(v, str) and str(k) not in _NON_ARTIFACT_KEYS
        }
        return tree or None

    @staticmethod
    def _validation_failures(role_output: Mapping[str, Any]) -> list[str]:
        """Return the specialist's decidable-gate failures (C5), or ``[]``.

        The 007/008 modes ship the artifacts PLUS a machine-readable
        ``validation.json`` (a JSON STRING in the native map) carrying
        ``{accepted, errors, gates_run}`` — the validation-as-data channel: a
        decidable gate FAILURE returns the artifacts AND the error list so the
        leg holds both. VALIDATION HONESTY: a reply that reports gate failures
        (``accepted: false`` / a non-empty ``errors`` list) must NOT proceed
        silently — the caller fails the leg loudly naming these errors. A clean
        (``accepted: true``) or absent channel returns ``[]`` (proceed).
        """
        raw = role_output.get("validation.json")
        data: Any = None
        if isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return [
                    "the specialist's validation.json is present but not "
                    "parseable JSON"
                ]
        elif isinstance(raw, Mapping):
            data = raw
        else:
            alt = role_output.get("validation")
            if isinstance(alt, Mapping):
                data = alt
        if not isinstance(data, Mapping):
            return []
        accepted = data.get("accepted")
        errors = data.get("errors")
        error_list = [str(e) for e in errors] if isinstance(errors, list) else []
        if accepted is True:
            return []
        if accepted is False or error_list:
            return error_list or [
                "the specialist reported the artifact set as not accepted "
                "(no error detail supplied)"
            ]
        return []

    @staticmethod
    def _feature_file_rel(files: Mapping[str, str]) -> str | None:
        """The repo-relative ``.feature`` path in a spec triple (or None)."""
        for rel in files:
            if rel.endswith(".feature"):
                return rel
        return None

    async def _read_spec_triple(
        self, repo_path: str, branch: str, spec_files: Any
    ) -> tuple[str | None, str | None, str | None]:
        """Read the committed spec triple CONTENTS back off the planning branch.

        Returns ``(spec_feature, spec_summary, spec_assumptions)`` classified by
        suffix (``.feature`` / ``_summary.md`` / ``_assumptions.yaml``); any role
        whose file is absent or unreadable comes back ``None``. Threading the
        CONTENTS (not paths) is the 008 contract — and reading them off the
        branch (rather than carrying them in memory) is what makes the plan leg
        correct on an idempotent re-drive.
        """
        feature = summary = assumptions = None
        for rel in spec_files:
            rel = str(rel)
            if rel.endswith(".feature"):
                feature = await self._deps.git_runner.read_file_from_branch(
                    repo_path=repo_path, branch=branch, file_path=rel
                )
            elif rel.endswith("_summary.md"):
                summary = await self._deps.git_runner.read_file_from_branch(
                    repo_path=repo_path, branch=branch, file_path=rel
                )
            elif rel.endswith("_assumptions.yaml"):
                assumptions = await self._deps.git_runner.read_file_from_branch(
                    repo_path=repo_path, branch=branch, file_path=rel
                )
        return feature, summary, assumptions

    @staticmethod
    def _capture_pass_bar_seed(role_output: Mapping[str, Any]) -> str | None:
        """The 007 feature-grain pass-bar seed content, or ``None`` (B4 round-19).

        Finds the ``pass-bar-seed-*.yaml`` extra in the 007 native artifact map
        (a tolerated extra, never a committed spec-triple file) and returns its
        raw content verbatim so the plan-commit leg can specialise it into
        per-task bars. ``None`` when the reply shipped no seed (an older
        specialist — the plan-commit leg then fails loudly rather than silently
        skipping the bars the B2 precondition demands).
        """
        for name, content in role_output.items():
            key = str(name)
            if key.startswith(_PASS_BAR_SEED_PREFIX) and key.endswith(
                _PASS_BAR_SEED_SUFFIX
            ):
                return str(content)
        return None

    async def _read_plan_tasks(
        self, repo_path: str, branch: str, feature_id: str, plan_files: Any
    ) -> list[dict[str, str]] | None:
        """Enumerate the validated plan's tasks from its committed feature YAML.

        The feature YAML (``.guardkit/features/<feature_id>.yaml``) is the SAME
        source ``guardkit feature validate`` reads; forge reads it back off the
        planning branch (not from memory, so this is correct on an idempotent
        re-drive) and returns one record per task in plan order: its ``id``, and
        its ``file_path`` when the plan states one (the task's own markdown file,
        which is where the plan declares that task's type). Returns ``None`` when
        the feature YAML cannot be located among the committed plan files, read,
        or parsed — a loud-fail signal for the caller.
        """
        # Locate the feature YAML among the committed plan files: the entry whose
        # path ends with ``<feature_id>.yaml``, preferring the canonical
        # ``.guardkit/features/`` location when several match.
        candidates = [
            str(f) for f in plan_files if str(f).endswith(f"{feature_id}.yaml")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (".guardkit/features/" not in p, p))
        feature_rel = candidates[0]

        raw = await self._deps.git_runner.read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=feature_rel
        )
        if not raw:
            return None
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            return None
        if not isinstance(data, Mapping):
            return None
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            # A feature YAML with no ``tasks`` key (or a null/absent list) has no
            # tasks to register bars for — an empty enumeration, not a read error.
            return []
        records: list[dict[str, str]] = []
        for task in tasks:
            if isinstance(task, Mapping):
                tid = task.get("id")
                if tid:
                    record: dict[str, str] = {"id": str(tid)}
                    file_path = task.get("file_path")
                    if file_path:
                        record["file_path"] = str(file_path)
                    records.append(record)
        return records

    async def _read_task_declaration(
        self, repo_path: str, branch: str, file_path: str
    ) -> str | None:
        """The text of one task file off the planning branch, or ``None``.

        Read so the bar minter can see the type the plan gave the task and the
        criteria the plan wrote on it. A file that is missing or unreadable is a
        logged warning and ``None``: the bar is then minted exactly as it was
        before this existed. Reading a task file must never be able to stop a
        planning run.
        """
        try:
            return await self._deps.git_runner.read_file_from_branch(
                repo_path=repo_path, branch=branch, file_path=file_path
            )
        except Exception as exc:  # noqa: BLE001 — read boundary, never fatal
            logger.warning(
                "planning driver: could not read task file %s off %s (%s); "
                "minting its pass bar from the feature seed unchanged",
                file_path,
                branch,
                exc,
            )
            return None

    @staticmethod
    def _documentation_bar_criteria(
        *,
        task_id: str,
        task_file: str,
        seed_criteria: list[Any],
        task_text: str | None,
    ) -> tuple[list[Any], list[str]]:
        """The criteria a DOCUMENTATION task can honestly be held to, + the note.

        Why this exists (build-FEAT-44A8, 4 September 2026). The seed is
        FEATURE-grain, and forge used to copy it onto every task's bar. For
        TASK-44A8-004, a documentation task, that put two criteria about how the
        running endpoint behaves onto a bar for a task that only edits
        ``docs/API.md`` — and whose type tells guardkit to run no tests at all
        (``DOCUMENTATION`` profile: ``tests_required=False``). A checklist that
        asks for evidence the task can never produce is a false record.

        What replaces them, in order:

        1. The criteria written ON the task itself ("## Acceptance Criteria" in
           its task file) — the list the Coach actually checks that task
           against, and the same source guardkit's own machine minter uses.
        2. Failing that, the seed's non-machine criteria only.
        3. Failing that, the seed unchanged — because ``PassBar.criteria``
           requires at least one entry, and a bar that will not validate stops
           the whole plan. In that case the note says the bar could not be
           narrowed, so nobody reads it as a claim about this task.

        Returns ``(criteria, note_lines)``; the note is written into the file as
        a leading comment block (``PassBar`` forbids extra fields, so a comment
        is the only place a note can live — guardkit's minter does the same).
        """
        own = task_acceptance_criteria(task_text or "")
        if own:
            note = [
                f"The plan types {task_id} as a documentation task, and guardkit runs",
                "no tests for that type (documentation profile: tests_required =",
                "false). So the feature's machine criteria — how the running",
                "endpoint behaves — are left off this bar: this task edits",
                "documents and could never produce that evidence. They are still",
                "carried by the feature's other tasks and answered by the live gate.",
                "The criteria below are the ones written on the task itself,",
                f"in {task_file} under '## Acceptance Criteria' — what the build",
                "actually holds this task to.",
                "They are classed 'machine' with evidence kind 'log' because there is",
                "no operator runbook to route an operator criterion to, and the live",
                "evidence kinds would arm the feature-complete runtime-surface gate",
                "off a documentation task.",
            ]
            return own, note

        non_machine = [
            c
            for c in seed_criteria
            if not (isinstance(c, Mapping) and str(c.get("class")) == "machine")
        ]
        if non_machine:
            note = [
                f"The plan types {task_id} as a documentation task, and guardkit runs",
                "no tests for that type. The task file carries no '## Acceptance",
                "Criteria' section, so this bar keeps only the checklist items that",
                "do not ask for machine evidence; the machine ones stay with the",
                "feature's other tasks and the live gate.",
            ]
            return non_machine, note

        note = [
            f"The plan types {task_id} as a documentation task, and guardkit runs",
            "no tests for that type — so the feature's machine criteria below are",
            "NOT work this task can evidence. They could not be narrowed away: the",
            "task file lists no acceptance criteria of its own and the feature",
            "checklist has no non-machine items, and a bar with no criteria at all",
            "is invalid and would stop the plan. Read them as the feature's, not",
            "as this task's.",
        ]
        return list(seed_criteria), note

    @staticmethod
    def _mint_pass_bar_yaml(
        *,
        task_id: str,
        seed: Mapping[str, Any],
        sha: str,
        date: str,
        task_type: str | None = None,
        task_file: str | None = None,
        task_text: str | None = None,
    ) -> str:
        """Mint one ``qa/pass-bar-<TASK-ID>.yaml`` from the seed (F2 shape).

        Mirrors the Factory-2 registered bar shape EXACTLY (format_version 2.0,
        task_id, registered_at{sha,date}, auth_surface_bearing, preconditions,
        criteria, negative_paths) so guardkit's own ``qa validate pass-bar``
        accepts it. ``registered_at.sha`` is the PLAN commit sha; the seed's
        ``preconditions``/``criteria`` are carried verbatim; ``auth_surface_bearing``
        is false on every bar that reaches this path — either by construction
        (an unflagged seed) or because the owner answered the auth-confirmation
        door with "there is no sign-in here" (a flagged seed reaches minting ONLY
        through that confirmation); ``negative_paths`` supplies the universal
        minimum the seed omits.

        ONE exception to "carried verbatim", added 2026-09-04: when the plan
        itself types the task ``documentation`` (or its alias ``research``), the
        criteria come from :meth:`_documentation_bar_criteria` and the file
        carries a leading note saying why. Every other task type — and every
        task whose type forge could not read — mints byte-for-byte what it
        minted before.
        """
        negative_paths = sorted(
            {str(p) for p in (seed.get("negative_paths") or [])}
            | {_UNIVERSAL_NEGATIVE_PATH}
        )
        criteria: list[Any] = [
            dict(c) if isinstance(c, Mapping) else c
            for c in (seed.get("criteria") or [])
        ]
        note_lines: list[str] = []
        if (task_type or "").strip().lower() in _DOCS_TASK_TYPES:
            criteria, note_lines = PlanningRunDriver._documentation_bar_criteria(
                task_id=task_id,
                task_file=task_file or "the task file",
                seed_criteria=criteria,
                task_text=task_text,
            )
        bar: dict[str, Any] = {
            "format_version": str(
                seed.get("format_version") or _PASS_BAR_FORMAT_VERSION
            ),
            "task_id": task_id,
            "registered_at": {"sha": sha, "date": date},
            "auth_surface_bearing": False,
            "preconditions": list(seed.get("preconditions") or []),
            "criteria": criteria,
            "negative_paths": negative_paths,
        }
        body = yaml.safe_dump(
            bar, sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        if not note_lines:
            return body
        rule = "# " + "-" * 74
        header = [rule, f"# {_DOCS_BAR_NOTE_MARKER}", "#"]
        header += [f"# {line}" for line in note_lines]
        header += ["#", f"# Written by forge's planning driver on {date}.", rule]
        return "\n".join(header) + "\n" + body

    @staticmethod
    def _read_architecture_rules(repo_path: str) -> dict[str, Any] | None:
        """Read the target repo's own written architecture rules, if it has any.

        A repository may keep its rules in one file — api_test keeps
        ``docs/architecture-rules.yaml``, twelve rules, each quoting the sentence
        in ``docs/architecture/`` it comes from. The planning seat cannot open
        files, so the rules travel as text: the rule id, the rule in one plain
        sentence, and the sentence it was taken from. The rest of that file is
        machinery for the conformance checker (``checked_by``, ``signals``,
        ``expected_current_finding``) and tells a plan-writer nothing, so it
        stays behind.

        No file ⇒ ``None`` ⇒ the descriptor carries no rules key and planning is
        byte-for-byte what it was before this existed. A file that cannot be read
        or is the wrong shape is a logged warning and ``None`` as well: a rules
        file must never be able to stop a planning run.
        """

        def _clip(text: str) -> str:
            text = " ".join(text.split())
            if len(text) <= _MAX_ARCHITECTURE_RULE_CHARS:
                return text
            return text[: _MAX_ARCHITECTURE_RULE_CHARS - 1].rstrip() + "\u2026"

        path = Path(repo_path) / _ARCHITECTURE_RULES_REL
        try:
            if not path.is_file():
                return None
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — never fail a plan over this
            logger.warning(
                "target_repo_descriptor: could not read the target repo's "
                "architecture rules at %s (%s); planning without them",
                path,
                exc,
            )
            return None

        if not isinstance(data, Mapping) or not isinstance(data.get("rules"), list):
            logger.warning(
                "target_repo_descriptor: %s does not have a top-level 'rules' "
                "list; planning without the architecture rules",
                path,
            )
            return None

        rules: list[dict[str, str]] = []
        for entry in data["rules"]:
            if len(rules) >= _MAX_ARCHITECTURE_RULES:
                logger.warning(
                    "target_repo_descriptor: %s lists more than %d rules; "
                    "sending the first %d",
                    path,
                    _MAX_ARCHITECTURE_RULES,
                    _MAX_ARCHITECTURE_RULES,
                )
                break
            if not isinstance(entry, Mapping):
                continue
            rule_id = str(entry.get("id") or "").strip()
            rule_text = str(entry.get("rule") or "").strip()
            # A rule with no id or no sentence says nothing a plan can be held
            # to, so it is left out rather than sent half-formed.
            if not rule_id or not rule_text:
                continue
            item: dict[str, str] = {"id": rule_id, "rule": _clip(rule_text)}
            document = str(entry.get("source_document") or "").strip()
            if document:
                item["source_document"] = _clip(document)
            sentence = str(entry.get("source_sentence") or "").strip()
            if sentence:
                item["source_sentence"] = _clip(sentence)
            rules.append(item)

        if not rules:
            logger.warning(
                "target_repo_descriptor: %s lists no readable rules; planning "
                "without the architecture rules",
                path,
            )
            return None
        return {"source_file": _ARCHITECTURE_RULES_REL, "rules": rules}

    @staticmethod
    def _build_target_repo_descriptor(
        target_repo: str, repo_path: str
    ) -> dict[str, Any]:
        """Build the 008 ``target_repo_descriptor`` honestly from what forge knows.

        Schema of record (specialist-agent roles/architect/modes/feature_plan.py
        ``TARGET_REPO_DESCRIPTOR_SCHEMA``): required = ``repo`` + ``test_roots``;
        optional = ``default_branch`` / ``sibling_repos`` / ``stack`` /
        ``architecture_rules``. forge NEVER invents an undefined field: ``repo``
        is the configured target repo name; ``sibling_repos`` is omitted — forge
        does not cheaply know siblings; ``architecture_rules`` is present only
        when the target repo actually keeps a rules file (see
        :meth:`_read_architecture_rules`), and the schema defines the field as of
        2026-08-31.

        ``test_roots`` is the EXACT ``tests/<name>`` set the downstream
        ``guardkit feature validate`` pre-commit oracle enforces, discovered by
        REUSING guardkit's OWN ``discover_test_roots``
        (:func:`forge.planning.target_terminal_tools.discover_target_test_roots`)
        — never a shallow re-guess. For api_test this is
        ``["tests/health", "tests/users"]`` (EMPTY ⇒ the plan may emit no smoke
        gate, ASSUM-010). B4 run 36629c5a round 10: the old shallow
        checkout-root discovery returned ``["tests"]``, so 008 invented
        ``tests/smoke`` — a prefix of ``tests`` that the in-session containment
        gate passed but the real validate rejected (repo has ``tests/health``,
        ``tests/users``, no ``tests/smoke``). Handing the exact roots makes
        prefix-containment == membership so the invention is caught in-session.
        """
        # Local import: keep the guardkit-boundary reuse out of the module
        # import graph (target_terminal_tools imports the guardkit run seam).
        from forge.planning.target_terminal_tools import (
            TargetTestRootsUnresolved,
            discover_target_test_roots,
            shallow_discover_test_roots,
        )

        try:
            test_roots = discover_target_test_roots(repo_path)
        except TargetTestRootsUnresolved as exc:
            # Degraded path: guardkit absent from the interpreter. Production
            # images always ship guardkit (the Dockerfile asserts the import),
            # so this only fires in a guardkit-less env where the real
            # ``feature validate`` oracle cannot run either. Fall back to
            # forge's own discovery so the descriptor is still built and the run
            # reaches the oracle (the last line of defense) rather than crashing
            # — log LOUDLY.
            #
            # The old fallback returned bare ``["tests"]`` / ``["test"]``, which
            # is the ROUND-10 DEFECT SHAPE: a bare ``tests`` root is a prefix of
            # every ``tests/<x>`` path, so the in-session containment gate waves
            # through an invented subdirectory. The replacement reproduces
            # guardkit's per-suite Python shape AND the TypeScript shapes, so the
            # degraded answer has the same geometry as the healthy one.
            logger.warning(
                "target_repo_descriptor: guardkit test-root discovery "
                "unavailable (%s); falling back to forge's own shallow "
                "discovery — the roots are shape-correct but are not the "
                "oracle's own answer",
                exc,
            )
            test_roots = shallow_discover_test_roots(repo_path)
        descriptor: dict[str, Any] = {"repo": target_repo, "test_roots": test_roots}
        # The repo's own architecture rules, when it keeps a rules file. Before
        # this, the plan-writer was never shown them, and two features built the
        # week of 2026-08-24 drifted from rules nobody had told it about.
        architecture_rules = PlanningRunDriver._read_architecture_rules(repo_path)
        if architecture_rules is not None:
            descriptor["architecture_rules"] = architecture_rules
        return descriptor

    def _has_leg_event(self, correlation_id: str, stage_label: str) -> bool:
        """True iff a durable ``approved`` event exists for ``stage_label``.

        Label-agnostic by construction: it compares against the label the CALLER
        asks for and ignores every other row. Historical labels left in old
        ledgers by legs that no longer exist — ``dcl-author`` above all (the
        W1-S2 DCL leg, struck 2026-08-15 when guardkit deleted the ``.dcl``
        track) — are therefore DATA, not code: a replay of a run that recorded
        one reads it, finds no live leg asking for it, and drives on unchanged.
        """
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] == stage_label and event["status"] == "approved":
                return True
        return False

    def _leg_event_details(
        self, correlation_id: str, stage_label: str
    ) -> dict[str, Any]:
        """Parsed details of the latest ``approved`` event for ``stage_label``."""
        latest: dict[str, Any] = {}
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] == stage_label and event["status"] == "approved":
                if event["details_json"]:
                    try:
                        latest = json.loads(event["details_json"]) or {}
                    except (json.JSONDecodeError, ValueError):
                        continue
        return latest

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _republish_pending(self, row: Any, plan_run_id: str) -> None:
        """Re-emit the persisted pending request (verbatim request_id)."""
        deps = self._deps
        correlation_id = row["correlation_id"]
        row = deps.store.get_run(correlation_id) or row
        pending_request_id = row["pending_approval_request_id"]
        if not pending_request_id:
            logger.warning(
                "planning driver: no pending request_id to re-emit for %s",
                correlation_id,
            )
            return
        envelope = build_planning_approval_envelope(
            request_id=pending_request_id,
            plan_run_id=plan_run_id,
            feature_id=plan_run_id,
            stage_label=_PRODUCT_DOCS_STAGE,
            summary_data=self._latest_po_output(correlation_id).get("docs_summary")
            or {"recovered": True},
            expected_approver=row["expected_approver"],
            attempt_count=self._attempt_from(pending_request_id),
            checkpoint_type="product_docs_recovered",
        )
        try:
            await deps.approval_publisher.publish_request(envelope)
            logger.info(
                "planning driver: re-emitted pending approval request %s for %s",
                pending_request_id,
                correlation_id,
            )
        except Exception:  # noqa: BLE001 — DDR-007
            logger.exception(
                "planning driver: re-emit failed for %s; pause persists",
                correlation_id,
            )

    def _phase_remaining(self, row: Any, cfg: "PlanningConfig") -> tuple[int, float]:
        """Return ``(phase, remaining_seconds)`` from durable anchors."""
        now = self._deps.clock()
        escalated_at = row["escalated_at"]
        if escalated_at is None:
            anchor_str = row["paused_at"]
            window = cfg.originator_wait_seconds
            phase = 1
        else:
            anchor_str = escalated_at
            window = cfg.escalated_wait_seconds
            phase = 2
        if anchor_str is None:
            # Corrupt row (PAUSED without paused_at) — treat the window
            # as starting now rather than timing out instantly.
            return phase, float(window)
        anchor = datetime.fromisoformat(anchor_str)
        elapsed = (now - anchor).total_seconds()
        return phase, window - elapsed

    def _policy(self, cfg: "PlanningConfig") -> EscalationPolicy:
        return EscalationPolicy(
            originator_wait_seconds=cfg.originator_wait_seconds,
            escalated_wait_seconds=cfg.escalated_wait_seconds,
            escalation_approver=cfg.escalation_approver or "",
            defer_cap=cfg.defer_cap,
        )

    @staticmethod
    def _attempt_from(request_id: str | None) -> int:
        if not request_id:
            return 0
        try:
            _, _, attempt = parse_request_id(request_id)
            return attempt
        except ValueError:
            return 0

    def _load_history(self, correlation_id: str) -> list[_HistoryEvent]:
        """Translate durable event rows into planner-shaped history."""
        history: list[_HistoryEvent] = []
        for event in self._deps.store.list_events(correlation_id):
            stage_label = event["stage_label"]
            status = event["status"]
            details: Mapping[str, Any] = {}
            if event["details_json"]:
                try:
                    details = json.loads(event["details_json"])
                except (json.JSONDecodeError, ValueError):
                    details = {}
            if stage_label == "product_owner" and status == "approved":
                history.append(
                    _HistoryEvent(
                        stage=StageClass.PRODUCT_OWNER,
                        status="approved",
                        details=details,
                    )
                )
            elif status == "checkpoint_cleared":
                history.append(
                    _HistoryEvent(
                        stage=StageClass.PRODUCT_OWNER,
                        status="checkpoint_cleared",
                        details={"stage_label": _PRODUCT_DOCS_STAGE, **details},
                    )
                )
        return history

    def _latest_po_output(self, correlation_id: str) -> dict[str, Any]:
        """Return the most recent recorded PO output (or empty)."""
        latest: dict[str, Any] = {}
        for event in self._deps.store.list_events(correlation_id):
            if event["stage_label"] == "product_owner" and event["status"] == (
                "approved"
            ):
                if event["details_json"]:
                    try:
                        details = json.loads(event["details_json"])
                        latest = details.get("po_output", {}) or {}
                    except (json.JSONDecodeError, ValueError):
                        continue
        return latest

    def _fail(self, correlation_id: str, *, stage_label: str, reason: str) -> None:
        # The write itself lives in forge.planning.failure so the intake
        # consumer's refusal and this driver end a run through ONE piece of
        # code (2026-09-05 rule 4).
        mark_run_failed(
            self._deps.store,
            correlation_id,
            stage_label=stage_label,
            reason=reason,
            log=logger,
        )

    async def _notify(
        self,
        correlation_id: str,
        message: str,
        *,
        level: str = "info",
        mention: bool = True,
    ) -> str:
        """Best-effort originator notification (DDR-007).

        Returns ``"sent"`` / ``"no-notifier"`` / ``"failed"`` so a caller
        that must RECEIPT whether a line went out can (the stamp normalizer's
        un-enforced line). ``mention=False`` asks the notifier for a plain
        line with no @mention (the composition's publisher accepts a
        ``mention`` keyword and drops ``target_user``); a publisher that
        does not take the keyword still gets the line, mentioned, and that is
        logged rather than the line being dropped.
        """
        publish = self._deps.publish_notification
        if publish is None:
            return "no-notifier"
        try:
            if mention or not _accepts_keyword(publish, "mention"):
                if not mention:
                    logger.warning(
                        "planning driver: notifier for %s takes no `mention` "
                        "keyword — the plain line goes out with the notifier's "
                        "default audience",
                        correlation_id,
                    )
                await publish(correlation_id, message, level)
            else:
                await publish(correlation_id, message, level, mention=False)
        except Exception:  # noqa: BLE001 — notifications never block the chain
            logger.warning(
                "planning driver: notification publish failed for %s (best-effort)",
                correlation_id,
            )
            return "failed"
        return "sent"
