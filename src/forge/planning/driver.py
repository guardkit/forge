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
import difflib
import hashlib
import inspect
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

import yaml

from forge.gating.identity import (
    derive_request_id,
    is_accepted_approver,
    parse_request_id,
)
from forge.lifecycle.identifiers import validate_feature_id
from forge.lifecycle.missions import mission_membership
from forge.pipeline.merge_offer import (
    AFTER_DEPLOY_CHECK_STAGE,
    SKIP_NO_ENDPOINT_NAMED,
    SKIP_NO_PASS_BARS,
    SKIP_NO_REGISTRY,
    SKIP_NO_TEMPLATE,
    SKIP_PLACEHOLDER_IN_ADDRESS,
    SKIP_REGISTRATION_FAILED,
    SKIP_UNSUPPORTED_ADDRESS,
    SKIP_UNSUPPORTED_METHOD,
    placeholders_in,
)
from forge.pipeline.stage_names import plain_stage_name
from forge.pipeline.stage_taxonomy import StageClass
from forge.planning.checkpoint import (
    PlanningEscalationContext,
    _dispatch_approval_response,
    _person_words,
    build_planning_approval_envelope,
    checkpoint_product_docs,
)
from forge.planning.declared_memory import (
    read_declarations_at_commit,
    read_declared_project_documents,
    read_declared_reference,
)
from forge.planning.factory_deployment_context import ensure_factory_context
from forge.planning import memory_decisions
from forge.planning.project_documents import (
    ProjectDocument,
    context_texts,
    read_project_documents_at_commit,
)
from forge.planning.reference_material import (
    REFERENCE_MATERIAL_STAGE,
    ReferenceMaterial,
    gather_reference_material,
    is_reference_path,
    reference_context_texts,
)
from forge.planning.escalation import (
    EscalationOutcome,
    EscalationPolicy,
    evaluate_escalation_phase,
)
from forge.planning.failure import (
    DRIVER_ACTOR,
    fail_run,
    mark_run_failed,
    settle_routing_failure,
)
from forge.planning import coverage_declaration, nothing_to_build
from forge.planning.handoff import (
    PlannedHandoffHandler,
    PreCommitCheck,
    PreCommitChecks,
    PreCommitResult,
    build_feature_spec_input_content,
)
from forge.planning.planner import (
    DispatchProductOwner,
    ExecuteHandoff,
    Fail,
    PauseAtCheckpoint,
    plan_next_step,
)
from forge.planning.code_evidence import (
    LISTED_ALL_MEANS,
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_WINDOWS,
    MAX_SPEC_EVIDENCE_CHARS,
    QuotedExample,
    candidate_windows,
    quoted_example_shapes,
    spec_writer_windows,
    ranked_source_field,
    choose_windows,
    is_documentation,
    is_factory_file,
    quantified_phrases,
    request_words,
    scenario_words,
    set_candidates,
    trim_to_budget,
)
from forge.planning.repository_facts import (
    LocalCheckoutReader,
    RepositoryFacts,
    RepositoryUnreadable,
    UnreadableRepository,
    read_repository_facts,
    unavailable_text,
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
from forge.planning.states import PlanningState
from forge.planning.target_repos import format_known_repos

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge.config.models import PlanningConfig
    from forge.gating.wrappers import GateRepository, StateMachine
    from forge.planning.checkpoint import SecondOpinionProvider
    from forge.planning.target_terminal_tools import (
        NormalizeFeatureSpecFn,
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
#: The merge cards read the same label (one constant, so the two never drift).
_QA_FEATURE_GATE_STAGE = AFTER_DEPLOY_CHECK_STAGE

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

#: Durable stage label for THE PROJECT'S DOCUMENTS as the door read them
#: (project initialisation design, 4 October 2026): the instruction files and
#: binding documents, read at the starting commit, with path, hash, size,
#: commit and text. Written once, at the door, and only for a project that
#: declares binding documents; every later spec and plan call sends exactly
#: what it holds, so a re-drive never reads them again. Status ``recorded``,
#: never ``approved``: it is a record, not any leg's sentinel.
_PROJECT_DOCUMENTS_STAGE = "project-documents"

#: Durable stage label for THE PROJECT'S REFERENCE MATERIAL as the door read it
#: (17 October 2026, prototype-to-factory design 2.2): the folders the project
#: declares in ``reference.paths`` and the files the request names under them,
#: read whole at the starting commit. Written once, at the door, beside the
#: project's documents, and only for a project that declares the folders;
#: every later spec and plan call, the planner's description of the
#: repository and the facts sheet read it from here. Status ``recorded``.
_REFERENCE_MATERIAL_STAGE = REFERENCE_MATERIAL_STAGE

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
#: The sign-in answer recorded for a mission feature whose spec may involve
#: sign-in when the mission message did not say ``[no sign-in]`` (missions
#: design, section 5). Not a yes, so the pass-bars leg stops honestly, and no
#: card is opened inside a mission.
_MISSION_SIGN_IN_NOT_SAID = "not said in the mission"

_AUTH_DOOR_TERMINAL_SUFFIX = {
    _MISSION_SIGN_IN_NOT_SAID: (
        "The spec may involve sign-in and the mission didn't say it has none, "
        "so the quality checklist must be registered attended."
    ),
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
#:
#: The path is the WHOLE address as written — everything up to the next space,
#: less closing sentence punctuation — never a partial match (4 October 2026,
#: Codex R5). The earlier pattern stopped at the last word boundary and at the
#: first dot, so ``/users/{user_id}`` was read as ``/users/{user_id`` and
#: ``/v1.0/users/{user_id}`` as ``/v1``: a check against an address nobody
#: named. :data:`_FEATURE_GATE_PLAIN_PATH_RE` then decides whether that whole
#: address is one a check can send as written.
_FEATURE_GATE_ENDPOINT_RE = re.compile(
    r"\bA (?P<method>GET|POST|PUT|PATCH|DELETE) request to (?P<path>/\S*)"
)

#: Closing punctuation that ends a criterion sentence, not the address in it.
_FEATURE_GATE_PATH_TRAILING = ".,;:!?)]\"'"

#: A whole address a derived check can send exactly as written: a rooted path
#: of letters, digits and ``_ . ~ / -`` (plus ``{}`` so a placeholder is seen
#: whole and skipped by name, never cut short). Anything else in the address
#: — a query string, an encoded character — is not checked automatically.
_FEATURE_GATE_PLAIN_PATH_RE = re.compile(r"/[A-Za-z0-9_.~/{}-]+")


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

#: How long forge waits for the target repository's own `git ls-files` before
#: giving up on the inventory. Listing tracked files is a local read of the
#: index; on api_test (695 tracked files) it returns in milliseconds, so a few
#: seconds is generous and a repository that cannot answer in that time must not
#: hold up a planning run.
_REPO_INVENTORY_GIT_TIMEOUT_SECONDS = 10

#: The factory's own paperwork: records ABOUT the repository rather than the
#: repository's code, and the bulk of the tree (400 of api_test's 695 tracked
#: files). None of it tells a plan-writer what the application already contains,
#: so none of it travels.
#:
#:   .guardkit/            the build harness's feature YAMLs, task records and
#:                         gate state, written by the factory for the factory
#:   .forge/               forge's own per-repository state
#:   .claude/              harness commands, settings and agents — the tools
#:                         that work ON the repository, not the application
#:   tasks/                one record per task the factory has run
#:   features/             the committed specifications; the current one is
#:                         already sent with the request, in full
#:   feature_spec_inputs/  the plain sentences those specifications came from
#:   qa/receipts/          evidence kept from past runs
_REPO_INVENTORY_SKIP_PREFIXES = (
    ".guardkit/",
    ".forge/",
    ".claude/",
    "tasks/",
    "features/",
    "feature_spec_inputs/",
    "qa/receipts/",
)

#: At or under this many files the inventory carries the file paths themselves.
#: Over it, it carries one entry per directory with the number of files in it
#: instead: a big repository has to teach the seat its SHAPE, because a list cut
#: off part-way reads like the whole truth and is worse than no list at all.
_MAX_REPO_INVENTORY_FILES = 400

#: How many of the specification's own words are looked for in the repository,
#: and how many places each may report. Both small on purpose: this is a
#: pointer for the plan-writer, not a search result page.
_MAX_SPEC_WORDS_LOOKED_FOR = 8
#: How many words are tried to find those 8 (6 October 2026, evidence
#: coverage): a word the repository does not hold at all (a route still to
#: be built, a product's name, an example id) no longer takes a place.
_MAX_SPEC_WORDS_TRIED = 12
_MAX_PLACES_PER_SPEC_WORD = 5

#: A word out of the specification worth looking for: a route path, or an
#: identifier long enough to be this repository's own name for something.
#: Deliberately not a language's grammar — kebab, snake and camel all match,
#: so the same rule serves Python, TypeScript, Go or anything else.
#: Three shapes, and every one of them is a NAME rather than a word of
#: English: a route path, a snake or kebab identifier, and a camel one. An
#: ordinary word like "numeric" or "boundaries" matches none of them, which is
#: the point — looking for those returns half the repository and teaches the
#: plan-writer nothing.
#: A route path keeps its placeholders (6 October 2026): before, the
#: request's ``/users/{user_id}`` was cut to ``/users/``.
#: A fourth shape (6 October 2026, evidence coverage): a name that starts
#: with a capital and has another capital inside it, next to a small letter
#: (``ETag``, ``HttpClient``, ``OrderLine``). Before, a request asking for
#: "ETag support" never looked for ``ETag``. A capitalised English word
#: ("Given"), an all-capitals one ("GET", "HTTP") and an all-capitals
#: plural ("URLs", "APIs") are none of these shapes.
_SPEC_WORD_PATTERN = re.compile(
    r"/[A-Za-z0-9{][A-Za-z0-9/_{}-]{2,}"
    r"|[A-Za-z][A-Za-z0-9]*[_-][A-Za-z0-9][A-Za-z0-9_-]*"
    r"|[a-z]+[A-Z][A-Za-z0-9]+"
    r"|\b(?=[A-Za-z0-9]{4})(?![A-Z0-9]+s\b)[A-Z][A-Za-z0-9]*?(?:[a-z][A-Z]|[A-Z][a-z])[A-Za-z0-9]*"
)

#: Words that appear in almost every specification and would match half the
#: repository. Looking for them tells the plan-writer nothing.
_SPEC_WORDS_NOT_WORTH_LOOKING_FOR = frozenset(
    {
        "scenario", "feature", "background", "given", "should", "returns",
        "return", "request", "response", "endpoint", "database", "example",
        "examples", "assumption", "assumptions", "criteria", "criterion",
        "number", "values", "value", "system", "service", "create", "created",
        "update", "updated", "delete", "deleted", "exists", "including",
        "without", "before", "after",
    }
)


def _one_form(word: str) -> str:
    """A looked-for word's key: lower case, no leading slash, and a plural
    "s" removed, so ``ETag`` and ``ETags`` are one word."""
    lowered = word.lower().lstrip("/")
    if len(lowered) > 4 and lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered



#: The longest ranked code search query sent to the sandbox helper (12 October
#: 2026): the request's distinctive words, cut here.
_RANKED_QUERY_CHARS = 300
#: The specification format's own words, left out of that query when it is
#: built from the specification (no request text).
_GHERKIN_WORDS = (
    "feature", "scenario", "outline", "background", "examples", "example",
    "given", "when", "then", "and", "but", "rule",
)


def _reads_beyond_the_working_tree(reader: Any) -> bool:
    """True unless ``reader`` reads this coordinator's checkout as it is on
    disk (a :class:`LocalCheckoutReader` with no commit): every other reader
    must be asked, never the filesystem beside it."""
    return not isinstance(reader, LocalCheckoutReader) or reader.commit is not None


def _how_interesting(place: str) -> tuple[int, str]:
    """The repository's own code first, then its tests, then its paperwork.

    A writer asking "does this already exist?" is answered by the code that
    builds it, not by a line in a markdown file that happens to mention it.
    Since 6 October 2026 this only orders the bare ``already_in`` places and
    breaks ties between evidence windows of equal score: no file type is
    ever left out because of it.
    """
    head = place.split("/", 1)[0]
    name = place.split(":", 1)[0]
    # Data and paperwork answer "where is it written down", never "where is
    # it built". Last, always.
    if name.endswith((".json", ".lock", ".yaml", ".yml", ".md", ".txt", ".csv")):
        return (3, place)
    if head in {"tests", "test", "spec", "qa"}:
        return (1, place)
    if head in {"docs", "doc", "features"}:
        return (2, place)
    return (0, place)


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
        "semantic_review.json",
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

PublishPlanningFailedFn = Callable[[str, str], Awaitable[None]]
"""``async (correlation_id, reason)`` — terminal derived projection."""

PublishPlanningCompleteFn = Callable[..., Awaitable[None]]
"""``async (correlation_id, *, feature_id, extra) -> None`` — the successful
terminal's derived projection, ``pipeline.planning-complete``. ``extra`` holds
the fields the event carries beyond its typed ones (``outcome``, ``proof``,
``summary``). Published only for the "already done, nothing to build" outcome
(7 October 2026), and only after its PLANNED_HANDOFF transition committed."""

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
first-round dispatch passes neither.

``context`` carries the recorded project and reference documents in their
existing order, then the project's decision records from memory when the door
recorded some, followed by the immutable factory deployment interface for new
runs. Legacy runs retain their original context. Every rewrite uses the same
recorded bytes."""

DispatchFeaturePlanFn = Callable[..., Awaitable[Any]]
"""``async (*, plan_run_id, correlation_id, feature_id, spec_feature,
spec_summary, target_repo_descriptor, spec_assumptions=None,
spec_feature_paths=None, request_text=None, repository_facts=None)
-> StageDispatchResult``.

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
unaffected.

2026-09-15 (the planner fix): ``request_text`` and ``repository_facts`` are the
two documents the plan-writer's own reviewer is judged against — the sentence
the person sent, word for word, and a short sheet of what the repository
already does, read by ordinary code rather than by a model. The spec leg has
sent both since 2026-09-13; the plan leg sent neither, which is why a plan that
moved the web address or added a login requirement nobody asked for could still
be scored a good plan. Both are OPTIONAL on both sides, so the two images may be
redeployed in either order, and a run with no sentence on its row sends the set
that shipped before this existed.

4 October 2026 (project initialisation): ``context`` carries the same project
documents the spec leg is sent, on every plan call, only for a project that
declares binding documents. Optional on both sides (specialist-agent fcec409):
an older plan writer ignores it, so the plan receipt says "sent"."""


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


def _card_sign_in_check(card: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The sign-in question a digest card carries, or ``None`` when it carries none.

    This block is a QUESTION the reader answers with the same tap that
    approves the spec, so a rewrite that made it appear or vanish changed what
    the person is being asked — even when every sentence in the list is word
    for word what it was.
    """
    block = (card or {}).get("sign_in_check")
    return block if isinstance(block, Mapping) else None


def _sign_in_change_phrase(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> str:
    """How the sign-in question changed between two cards; "" when it did not."""
    before = _card_sign_in_check(previous)
    after = _card_sign_in_check(current)
    if before == after:
        return ""
    if before is None:
        return "the sign-in question was added"
    if after is None:
        return "the sign-in question is gone"
    return "the sign-in question changed"


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
    # The sign-in question is part of what the card asks, so a rewrite that
    # added or dropped it is a change the reader is told about (coach finding,
    # 2026-09-05: without this a sign-in-only rewrite was called "the same
    # list").
    sign_in = _sign_in_change_phrase(previous, current)
    if sign_in:
        parts.append(sign_in)
    return "; ".join(parts)


def _cards_say_the_same_thing(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    """True when a rewrite came back with the same list the owner just rejected.

    "The same" means the whole of what the reader is asked: the sentences, the
    assumptions, AND the sign-in question, which is answered by the same tap.
    A rewrite whose only visible change was that question appearing or
    disappearing used to be reported as the same list (coach finding,
    2026-09-05).
    """
    return (
        _card_sentences(previous) == _card_sentences(current)
        and _card_assumptions(previous) == _card_assumptions(current)
        and _card_sign_in_check(previous) == _card_sign_in_check(current)
    )


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

# ---------------------------------------------------------------------------
# The possible contradiction (4 October 2026, the owner: "yes make the change so
# it's a warning on the card"). The spec writer no longer refuses a spec over a
# pair its reviewer says cannot both be true; it writes the pair to
# ``coherence_warning.json`` beside the spec, and the card shows it in its own
# section. It is never added to ``what_happened``, so that paragraph stays
# exactly as it was, and the extra section has a bounded length of its own.
# ---------------------------------------------------------------------------

#: The file the spec writer puts beside the spec when its reviewer named a pair.
_COHERENCE_WARNING_FILE = "coherence_warning.json"
#: At most this many pairs are shown; the rest are counted ("And N more.").
_POSSIBLE_CONTRADICTION_SHOWN = 2
#: Per-field cuts. With two pairs these keep the whole text under 1,400
#: characters by construction (the longest possible text is about 1,330).
_POSSIBLE_CONTRADICTION_TITLE_CHARS = 150
_POSSIBLE_CONTRADICTION_REASON_CHARS = 250
_POSSIBLE_CONTRADICTION_MAX_CHARS = 1399

_POSSIBLE_CONTRADICTION_OPENING = (
    "Possible contradiction, found by the machine's reviewer and not checked "
    "by a person:"
)
_POSSIBLE_CONTRADICTION_CLOSING = (
    "If they really conflict, send a note; otherwise approve as usual."
)


def _cut_for_card(text: Any, limit: int) -> str:
    words = " ".join(str(text or "").split())
    if len(words) <= limit:
        return words
    return words[: limit - 1].rstrip() + "…"


def _possible_contradiction_text(
    pairs: Sequence[Mapping[str, Any]], pair_count: int | None = None
) -> str:
    """The card's words for the pairs: at most two, then "And N more.".

    ``pair_count`` is the total the spec writer found, before its file's own
    cap; without it the pairs in the file are all that can be counted.
    """
    sentences: list[str] = []
    for pair in pairs[:_POSSIBLE_CONTRADICTION_SHOWN]:
        first = _cut_for_card(pair.get("first"), _POSSIBLE_CONTRADICTION_TITLE_CHARS)
        second = _cut_for_card(pair.get("second"), _POSSIBLE_CONTRADICTION_TITLE_CHARS)
        reason = _cut_for_card(pair.get("why"), _POSSIBLE_CONTRADICTION_REASON_CHARS)
        sentence = f'"{first}" and "{second}".'
        if reason:
            sentence += f' Its reason: "{reason}".'
        if sentences:
            sentence = "Also " + sentence
        sentences.append(sentence)
    total = max(len(pairs), pair_count or 0)
    more = total - _POSSIBLE_CONTRADICTION_SHOWN
    if more > 0:
        sentences.append(f"And {more} more.")
    text = " ".join(
        [_POSSIBLE_CONTRADICTION_OPENING, *sentences, _POSSIBLE_CONTRADICTION_CLOSING]
    )
    # Defensive only: the cuts above already keep it shorter.
    return text[:_POSSIBLE_CONTRADICTION_MAX_CHARS]


#: Where the plan leg's approved record carries that line for the build gate
#: (:func:`forge.cli._serve_gate_activation.maybe_gate_build` reads it).
PLAN_REPOSITORY_UNAVAILABLE_KEY = "repository_unavailable"


#: Where the plan leg's approved record carries the build card's line about
#: the plan's review: its request-traceability score (step 2, 26 October 2026)
#: or that the record could not be read (24 October 2026), read beside the two
#: lines above.
PLAN_REVIEW_NOTE_KEY = "plan_review_note"


#: The build card's line when the plan reviewer did not approve the plan
#: (step 2, 27 October 2026): advice for the person, never a refusal.
_PLAN_REVIEW_REFUSED_CARD_LINE = (
    "The plan reviewer did not approve this plan ({reason}). The plan was kept "
    "for you to judge; the planner's notes give its objections where it stated "
    "them. This card decides."
)

#: Where the plan leg's approved record carries the planning seat's advice for
#: the build card (step 2, 26 October 2026), read beside the lines above, and
#: the line it is shown in. Display only: nothing waits or refuses on it.
PLAN_ADVICE_NOTE_KEY = "plan_advice_note"
_PLAN_ADVICE_CARD_LINE = "The planner's notes for you: {advice}"
#: The seat's prefix for a prerequisite the repository does not have yet;
#: such lines are shown first.
_MISSING_PREREQUISITE = "Missing prerequisite:"


#: The build card's line with the plan reviewer's request-traceability score
#: (step 2, 26 October 2026): advice for the person, never a refusal.
_PLAN_REVIEW_SCORE_CARD_LINE = (
    "The plan reviewer scored how closely the plan follows the request at "
    "{score} out of 1 (its verdict: {verdict})."
)


#: The spec card's line when the planner could not read the repository
#: (the 1 October planner fix, 1 October 2026). Plain words; the reason is the
#: reader's own clause.
_REPOSITORY_FACTS_UNAVAILABLE_CARD_LINE = (
    "The machine could not read the repository while writing this, so nothing "
    "checked it against what the repository already has ({reason})."
)

#: The cards' line when the planner read the repository only in part (a file
#: it chose was refused, or an answer was cut short and could not be
#: recovered). What was read was used; the reason is shortened for a card.
_REPOSITORY_FACTS_PARTIAL_CARD_LINE = (
    "The machine could read the repository only in part while writing this, "
    "so some of what it already has may not have been checked ({reason})."
)


def _partial_card_line(reason: str) -> str:
    text = " ".join(str(reason).split())
    if len(text) > 300:
        text = text[:299].rstrip() + "\u2026"
    return _REPOSITORY_FACTS_PARTIAL_CARD_LINE.format(reason=text)



# ---------------------------------------------------------------------------
# The machine rewrites on a refusal before it asks (2026-09-06).
#
# Two of Rich's sentences that weekend stopped at the plan stage because the
# routing law could not prove some worked examples. The law was right to
# refuse; the machine was wrong to ask a person to repair its draft. So the
# plan leg sends the refused titles back to the spec writer ONCE, as its own
# note, exactly as an owner's note is sent, and stamps again. The note is
# stack-neutral and bounded by the request/context the writer can actually
# read. Only a second refusal asks.
# ---------------------------------------------------------------------------


#: What the owner reads when the spec writer's revision round is refused by
#: the checker after a note (rule 23) — theirs or the machine's. It says what
#: happened, that nothing was built, and what to do, and it quotes the note so
#: the reader does not have to find it.
_NOTE_REFUSED_MESSAGE = (
    "Planning run {correlation_id} stopped at the spec: the spec writer could "
    'not honour your note "{note}" — the checker refused the rewrite twice '
    "({reason}). Nothing was built. To try again, send the sentence again "
    "with the note folded into it."
)


@dataclass(frozen=True)
class _PlanAttempt:
    """One pass of the plan leg's work: read the spec, write the plan.

    A pass that failed the run loudly is not an attempt record at all — the
    helper returns ``None`` after ``_fail_leg``.
    """

    files: Mapping[str, str]
    slug: str
    sha: str | None = None
    #: The plan reviewer's record as the planning seat wrote it, shown on the
    #: build card. The legacy field name is retained.
    traceability: Mapping[str, Any] | None = None
    #: Why the plan reviewer did not approve the plan (step 2, 27 October
    #: 2026): the build card says so and decides; nothing refuses on it.
    review_refused: str | None = None
    #: Set when the plan writer answered "already done, nothing to build"
    #: (7 October 2026): nothing was written, and the caller ends the run
    #: through :meth:`PlanningRunDriver._finish_nothing_to_build`.
    nothing_to_build: "_NothingToBuildAnswer | None" = None
    #: The planning seat's ``plan_advice`` lines (``Missing prerequisite:``
    #: lines first), shown on the build card as information for the approver
    #: (step 2, 26 October 2026). Display only: nothing reads them to decide.
    plan_advice: tuple[str, ...] = ()


@dataclass(frozen=True)
class _NothingToBuildAnswer:
    """The plan writer's "nothing to build" answer. ``claim`` is ``None`` and
    ``problem`` says why when the answer could not be read as one."""

    claim: "nothing_to_build.Claim | None"
    problem: str | None



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
    publish_planning_failed: PublishPlanningFailedFn | None = None
    # The "already done, nothing to build" outcome (7 October 2026): the
    # successful terminal's event. None = nothing is published, and the run
    # still ends and the owner is still told.
    publish_planning_complete: PublishPlanningCompleteFn | None = None
    # Sandbox first (2026-09-07, rule 71) — the git runner BY TARGET REPO. The
    # composition sets it when any repository has a sandbox: ``org/name`` →
    # that repository's runner (its sandbox sidecar's, or the in-container
    # one). None = ``git_runner`` serves every repository, exactly as before.
    # The plan leg asks it which runner it has, and whether that runner takes
    # its pre-commit checks declared (``supports_declared_checks``) rather
    # than as a closure run here.
    git_runner_for_repo: Callable[[str], Any] | None = None
    # Lane B / Phase E1 (B2) — the target-terminal spec/plan legs. All optional
    # and default None: with the target-terminal flag OFF they are never
    # consulted (byte-for-byte no-op). With the flag ON they are required; a
    # missing collaborator fails the run LOUDLY (never a silent skip).
    dispatch_feature_spec: DispatchFeatureSpecFn | None = None
    dispatch_feature_plan: DispatchFeaturePlanFn | None = None
    normalize_feature_spec: "NormalizeFeatureSpecFn | None" = None
    # Lane B / Phase E1 (B3) — the build trigger. Optional / default None: with
    # the target-terminal flag OFF it is never consulted; with the flag ON it is
    # required and a missing collaborator fails the run LOUDLY (never silent).
    dispatch_build_trigger: DispatchBuildTriggerFn | None = None
    feature_routing_gate: Any | None = None
    feature_routing_required: bool = False
    # ``approval.additional_approvers`` (Linear front door, 18 October 2026):
    # identities whose answer to a planning card counts as well as the run's
    # pinned approver. Empty (the default) = only the pinned approver.
    additional_approvers: tuple[str, ...] = ()
    # The fleet-memory reader (``read_records``) the door asks once per run for
    # the project's decision records (23 October 2026). None, or a reader
    # without ``read_records`` (memory off) = nothing searched, said plainly.
    memory_reader: Any | None = None


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

    def _feature_routing_receipt(
        self, correlation_id: str
    ) -> dict[str, object] | None:
        """Read the immutable committed receipt for a planning launch."""
        gate = self._deps.feature_routing_gate
        if gate is None:
            return None
        return gate.require_committed_success(correlation_id).to_wire()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def drive(
        self, correlation_id: str, *, republish_pending: bool = False
    ) -> None:
        """Drive ``correlation_id`` forward from its durable state.

        See :meth:`_drive_run`, which does the work. When it returns, however
        it ends, the run's spec-writer code windows
        (:meth:`_spec_code_evidence`) are let go, so that cache never grows
        across runs (17 October 2026). A later drive of the same run builds
        them again from the same recorded starting commit.
        """
        try:
            await self._drive_run(correlation_id, republish_pending=republish_pending)
        finally:
            cache = self.__dict__.get("_spec_code_evidence_cache")
            if cache:
                cache.pop(correlation_id, None)

    async def _drive_run(
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

        # Recovery/card entry points may only reuse a committed receipt.  A
        # PAUSED run is never a fresh admission and therefore cannot seed.
        if deps.feature_routing_gate is not None and state is PlanningState.PAUSED:
            try:
                deps.feature_routing_gate.require_committed_success(correlation_id)
            except Exception as exc:  # fail closed; settlement remains existing path
                await self._settle_routing_failure(
                    correlation_id, f"feature routing is unavailable: {exc}"
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

        # The QUEUED→RUNNING commit above precedes the sole HTTP seed.  A
        # recovered RUNNING/FEATURE_* path executes the same durable gate;
        # SUCCEEDED reuses its receipt and STARTED/UNKNOWN/FAILED refuses.
        if deps.feature_routing_gate is not None and state is not PlanningState.PAUSED:
            try:
                await deps.feature_routing_gate.ensure_seeded(
                    correlation_id,
                    origin_kind="planning",
                    origin_id=correlation_id,
                )
            except Exception as exc:
                await self._settle_routing_failure(
                    correlation_id, f"feature routing admission failed: {exc}"
                )
                return

        # THE DOOR, AND IT IS BEFORE THE FIRST MODEL IS ASKED ANYTHING
        # (22 September 2026, after the stage's second independent review).
        #
        # The starting rule and the memory rule used to run inside
        # :meth:`_enter_target_terminal`, which is where the branch is cut —
        # and that is AFTER the product-owner dispatch. So a project that
        # declares no memory was refused, correctly, but only after a model had
        # already been asked to do work, with neither the starting commit nor
        # the memory name recorded when that call was made. A door that opens
        # after the first dispatch is not a door.
        #
        # Both rules run HERE instead, before the loop below reaches any
        # dispatch: the project's remote is fetched and the starting commit and
        # branch written down; the project's own settings file is read AT that
        # commit for the memory name and for the setting names its builds ask
        # for; a project that declares no memory, or anything unusable, is
        # refused in plain words with nothing cut and nothing recorded beyond
        # where it would have started.
        #
        # Only on the two states from which a run can still reach a dispatch,
        # and only when the machine chain is on — a run already past the door
        # (FEATURE_SPEC and later) has been through it, and re-running it on a
        # re-drive could refuse work already in flight. Everything it does is
        # idempotent anyway: a run with both facts recorded fetches nothing and
        # reads nothing.
        if self._target_terminal_enabled() and state in (
            PlanningState.QUEUED,
            PlanningState.RUNNING,
        ):
            row = deps.store.get_run(correlation_id)
            if row is None:  # pragma: no cover - defensive
                return
            if await self._door(row, correlation_id) is None:
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
                # The run may have ENDED inside the plan leg without failing
                # (7 October 2026, "already done, nothing to build"), or been
                # ended by another writer while it ran: the leg's re-drive
                # shortcut returns True whenever a ``feature-plan`` event
                # exists, whatever the row says now. Read the row again, and
                # never run the pass-bar, gate or build legs for a run that is
                # over.
                current = deps.store.get_run(correlation_id)
                if (
                    current is None
                    or PlanningState(current["state"]) in _TERMINAL_STATES
                ):
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

            if isinstance(decision, Fail):
                await self._fail(
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
                        await self._fail(
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
                        await self._fail(
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
            await self._fail(
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
        await self._fail(
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
                additional_approvers=deps.additional_approvers,
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
                    f"{_person_words(_responder_display_name(response))}.",
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
        await self._fail(correlation_id, stage_label="planned-handoff", reason=reason)
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

    def _ranked_source_enabled(self) -> bool:
        """True iff the ``planning.ranked_source.enabled`` switch is on."""
        cfg = getattr(self._deps.planning_config, "ranked_source", None)
        return bool(getattr(cfg, "enabled", False))

    def _nothing_to_build_enabled(self) -> bool:
        """True iff the ``planning.nothing_to_build.enabled`` switch is on."""
        cfg = getattr(self._deps.planning_config, "nothing_to_build", None)
        return bool(getattr(cfg, "enabled", False))

    async def _finish_nothing_to_build(
        self,
        row: Any,
        correlation_id: str,
        answer: _NothingToBuildAnswer,
        *,
        target_repo: str,
        feature_id: str,
    ) -> str | None:
        """End a run whose plan writer answered "already done, nothing to build",
        or say what to tell the plan writer when it must plan as usual.

        Returns ``None`` when the run has ended here, and otherwise the note
        for the one ordinary plan the caller then asks for: an answer that
        could not be read, or one given while the switch is off, means "plan
        as usual". (The check of its citations against Forge's own code
        windows went in the cull, 27 October 2026.) Otherwise:

        1. **End the run in the existing success state.** FEATURE_PLAN →
           PLANNED_HANDOFF, labelled :data:`nothing_to_build.STAGE_LABEL`,
           with the marker, the proof and the owner's sentence in the
           transition's ``details_json``. No ``feature-plan`` event is
           written, so the plan leg's re-drive shortcut can never fire.
        2. **Tell people once, and only if the transition committed.** One
           ``pipeline.planning-complete`` carrying the marker and the proof,
           then one owner message. A refused transition means another writer
           already ended the run: nothing is sent, and the refusal is logged.
        """
        if not self._nothing_to_build_enabled():
            return self._plan_as_usual(
                correlation_id,
                "this factory does not take that answer (the "
                "planning.nothing_to_build switch is off)",
                proven=(),
            )
        claim = answer.claim
        if claim is None:
            problem = answer.problem or "it could not be read"
            return self._plan_as_usual(correlation_id, problem, proven=())

        message = nothing_to_build.owner_message(
            self._request_text_of(row), target_repo, claim
        )
        details = nothing_to_build.terminal_details(claim, message)
        refused = self._deps.store.transition(
            correlation_id=correlation_id,
            to_state=PlanningState.PLANNED_HANDOFF,
            actor_identity=DRIVER_ACTOR,
            stage_label=nothing_to_build.STAGE_LABEL,
            details_json=json.dumps(details),
            expected_from_state=PlanningState.FEATURE_PLAN,
        )
        if isinstance(refused, TransitionRefused):
            logger.warning(
                "planning driver: run %s nothing-to-build PLANNED_HANDOFF "
                "transition refused (current=%s); another writer ended the run, "
                "so nothing is sent",
                correlation_id,
                refused.current_state,
            )
            return None
        logger.info(
            "planning driver: run %s ended already done, nothing to build "
            "(%d proof lines; no build queued)",
            correlation_id,
            len(details[nothing_to_build.PROOF_KEY]),
        )
        publish = self._deps.publish_planning_complete
        if publish is not None:
            try:
                await publish(
                    correlation_id,
                    feature_id=feature_id,
                    extra={
                        nothing_to_build.OUTCOME_KEY: nothing_to_build.OUTCOME,
                        nothing_to_build.PROOF_KEY: list(
                            details[nothing_to_build.PROOF_KEY]
                        ),
                        "summary": message,
                    },
                )
            except Exception:  # noqa: BLE001 — a projection never blocks the row
                logger.warning(
                    "planning driver: planning-complete projection did not go "
                    "out for %s (durable row remains PLANNED_HANDOFF)",
                    correlation_id,
                )
        await self._notify(correlation_id, message, level="info")
        return None

    @staticmethod
    def _plan_as_usual(
        correlation_id: str, problem: str, *, proven: Sequence[str]
    ) -> str:
        """The note for the one ordinary plan after an already-done answer
        whose proof could not be used (step 1 simplification, 24 October 2026).

        Logged as advice. The parts that were proven ride the note so the plan
        writer can build on them rather than rebuild them; nothing is refused.
        """
        logger.warning(
            "planning driver: run %s already-done answer not used (%s) — "
            "ADVISORY; asking once for an ordinary plan (%d proven part(s) "
            "carried as its note)",
            correlation_id,
            problem,
            len(proven),
        )
        note = (
            "Your answer that the request is already done could not be used: "
            f"{problem}. Write an ordinary plan to build the request."
        )
        if proven:
            note += (
                " These parts were shown to exist already; the plan may rely on "
                "them rather than rebuild them: " + "; ".join(proven) + "."
            )
        return note

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

        # THE DOOR'S OWN FACTS, asked for again here and answered from the
        # record. ``drive`` walks the door before the first dispatch, so by the
        # time the branch is cut the starting commit, the memory name and the
        # project's declared setting names are already written down and this
        # fetches nothing and reads nothing. It is called again rather than
        # passed down because this leg is also reachable on a re-drive, and a
        # leg that cuts a branch must never do it from facts it was handed by
        # something that might not have run.
        passed = await self._door(row, correlation_id)
        if passed is None:
            return False
        start_commit, target_branch, memory_project, _declared = passed

        try:
            result = await deps.git_runner.prepare_branch_and_write(
                repo_path=repo_path,
                branch=branch,
                file_path=handoff_path,
                content=content,
                start_commit=start_commit,
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
                    "start_commit": start_commit,
                    "target_branch": target_branch,
                    "memory_project": memory_project,
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
            "planning driver: run %s entered the target terminal (branch=%s, "
            "cut from %s, the commit %s had for %s)",
            correlation_id,
            branch,
            start_commit,
            target_repo,
            target_branch,
        )
        return True

    def _recorded_launch(
        self, correlation_id: str
    ) -> tuple[str | None, tuple[str, ...], str, str | None]:
        """What this factory recorded about the run the write belongs to.

        ``(memory name, declared setting names, whose work it is, where its
        declarations were said)``. All four are read off the run rather than
        passed down, so any leg that declares checks to a sandbox is launching
        the build system with exactly what this factory wrote down for this
        run and nothing else. Nothing recorded for the first two — a run from
        before either rule, or a ledger without the columns — answers ``(None,
        ())``, and the checks are then launched with the factory's own list
        and memory explicitly off, never with what the sandbox's own checkout
        happens to declare.

        AND THE WRITE SAYS WHOSE WORK IT IS (23 September 2026). "Whose" is
        the correlation id: the id this factory's own planning record keeps
        the run under, and the id the run's recorded starting commit is filed
        against. It is NOT a build id — a build gets its own, derived later
        from the feature and the moment it was queued — so the pair sent to a
        sandbox names the RUN, and the read-only answer looks a run up in the
        planning record when no build of that name exists. "Where" is the
        starting commit written onto the run before its first branch was cut,
        read here and never composed; a run with none sends none, and the door
        that would otherwise have to read a declaration refuses rather than
        falling back to the far side's HEAD.
        """
        store = self._deps.store
        declared_at: str | None = None
        start_getter = getattr(store, "get_start_point", None)
        if start_getter is not None:
            try:
                recorded_commit, _recorded_branch = start_getter(correlation_id)
                declared_at = str(recorded_commit) if recorded_commit else None
            except Exception:  # noqa: BLE001 — a read, never a failed leg
                declared_at = None
        name = None
        getter = getattr(store, "get_memory_project", None)
        if getter is not None:
            try:
                name = getter(correlation_id)
            except Exception:  # noqa: BLE001 — a read, never a failed leg
                name = None
        names: tuple[str, ...] = ()
        settings_getter = getattr(store, "get_launch_settings", None)
        if settings_getter is not None:
            try:
                names = tuple(settings_getter(correlation_id) or ())
            except Exception:  # noqa: BLE001 — same
                names = ()
        return (str(name) if name else None), names, str(correlation_id), declared_at

    async def _door(
        self, row: Any, correlation_id: str
    ) -> tuple[str, str, str, tuple[str, ...]] | None:
        """The door: everything decided before a model is asked anything.

        Returns ``(start_commit, target_branch, memory_project, declared)`` or
        ``None`` after failing the run loudly and telling the owner why.

        Three questions, in this order, because each needs the one before it:

        1. **where does this work start?** The project's remote named ``origin``
           is fetched and asked which branch it calls its default and which
           commit that branch is at, and both are written down (the starting
           rule, one true copy, item 1, 2026-09-21);
        2. **which memory does it belong to?** The project's own
           ``.guardkit/config.yaml`` is read AT that commit and must name it
           (item 2). A project that declares none is refused in plain words
           naming the two lines to add. Building with no context is pointless,
           and filing one project's outcomes under another project's name is
           worse than filing none;
        3. **what do its builds need to be launched with?** The same file, the
           same commit, the same parse: the NAMES the project declares beyond
           the factory's own list (22 September 2026). A project that asks for
           nothing gets nothing extra; one that asks for a name this factory
           keeps for itself is refused, naming it;
        4. **which documents is its work held to?** (4 October 2026) The same
           file names them in ``autobuild.player.required_documents``; each is
           read AT that commit, with the repository's instruction files, and
           recorded for the spec and plan writers. A missing document, a
           symbolic link or documents over the budget are refused. A project
           that names none has nothing read.

        Every part is idempotent: a run that already has these facts recorded
        fetches nothing and reads nothing, and keeps exactly what it started
        with. A re-drive must never move a run onto a newer commit, a different
        memory or a different set of settings than the branch it already cut.
        """
        resolved = await self._resolve_repo(
            row, correlation_id, stage_label="target-terminal-enter"
        )
        if resolved is None:
            return None
        target_repo, repo_path = resolved

        start = await self._start_point(correlation_id, target_repo, repo_path)
        if start is None:
            return None
        start_commit, target_branch = start

        declarations = await self._declarations(
            correlation_id,
            target_repo,
            repo_path,
            start_commit,
            request_text=self._request_text_of(row),
        )
        if declarations is None:
            return None
        memory_project, declared = declarations
        await self._recall_memory_decisions(
            correlation_id, target_repo, memory_project, self._request_text_of(row)
        )
        return start_commit, target_branch, memory_project, declared

    async def _recall_memory_decisions(
        self, correlation_id: str, target_repo: str, memory_project: str, request_text: str
    ) -> None:
        """Search the project's memory once for its decision records and record
        what will be sent (23 October 2026). Never fails the run: memory off, a
        name the repository was not granted, a failed search or no records are
        recorded as the outcome and the writers are sent what they always were.
        A run that already has the outcome recorded searches nothing again."""
        deps = self._deps
        if memory_decisions.recorded(deps.store, correlation_id) is not None:
            return
        read = getattr(deps.memory_reader, "read_records", None)
        if read is None:
            records, why_not = None, "memory is not switched on for this factory"
        else:
            # Only a name the repository was granted at registration is read,
            # the same rule as the build card's priors (design section 2.4a).
            sandboxes = getattr(deps.planning_config, "sandboxes", None) or {}
            entry = sandboxes.get(target_repo) if hasattr(sandboxes, "get") else None
            try:
                records, why_not = await read(
                    memory_project=memory_project,
                    granted_memory_projects=tuple(getattr(entry, "memory_projects", None) or ()),
                    payload_types=(memory_decisions.PAYLOAD_TYPE,),
                    domain_tags=(memory_decisions.DOMAIN_TAG,),
                    query=request_text,
                    token_budget=memory_decisions.BUDGET_BYTES // 4,
                    purpose=f"planning run {correlation_id}",
                )
            except Exception as exc:  # noqa: BLE001 — evidence, never a failed leg
                records, why_not = None, f"the memory search raised {type(exc).__name__}"
        result = memory_decisions.outcome(memory_project, records, why_not)
        memory_decisions.record(deps.store, correlation_id, result)
        logger.info("planning driver: run %s — %s", correlation_id, result["detail"])

    def _recorded_memory_decisions(
        self, correlation_id: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        """``(text to send or None, receipt or None)``; both None for a run
        whose door ran before decision records were searched."""
        result = memory_decisions.recorded(self._deps.store, correlation_id)
        if result is None:
            return None, None
        return result.get("text"), memory_decisions.receipt(result)

    async def _start_point(
        self, correlation_id: str, target_repo: str, repo_path: str
    ) -> tuple[str, str] | None:
        """``(start_commit, target_branch)``, or None after failing the run.

        The starting rule's one call, and the place it is written down (one
        true copy, item 1, 2026-09-21):

        * a run that ALREADY has a recorded starting point keeps it. The
          target branch is decided once, at the start, and a re-drive of this
          leg must not quietly move a run onto a newer commit than the branch
          it already cut. Nothing is fetched in that case;
        * otherwise the git runner for this repository is asked to fetch the
          remote named ``origin`` and say where its default branch is. The
          answer is recorded on the planning run BEFORE the branch is cut, so
          a crash between the two leaves a record of where the work was about
          to start rather than nothing at all;
        * a refusal — no such remote, unreachable, no default branch — fails
          the run loudly with the refusal's own sentence, which reaches the
          owner exactly where every other planning refusal does.
        """
        deps = self._deps
        recorded_commit, recorded_branch = deps.store.get_start_point(correlation_id)
        if recorded_commit and recorded_branch:
            logger.info(
                "planning driver: run %s already started from %s on %s; not "
                "fetching again",
                correlation_id,
                recorded_commit,
                recorded_branch,
            )
            return str(recorded_commit), str(recorded_branch)

        # A MISSION FEATURE starts from its mission's branch, never from the
        # default branch (missions design, 17 October 2026, section 5).
        # Membership is this run's correlation id in ``mission_attempts``.
        membership = mission_membership(
            getattr(deps.store, "_connection", None), correlation_id
        )
        if membership is not None:
            return await self._mission_start_point(
                correlation_id, target_repo, repo_path, membership
            )

        # The SAME runner the branch write uses, so the fetch and the cut
        # always happen in one place: in the sandbox for a project that has
        # one, in the coordinator for a project that does not.
        runner = deps.git_runner
        fetch = getattr(runner, "fetch_remote_start_point", None)
        if fetch is None:
            await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                (
                    "the git runner wired for this factory cannot fetch a "
                    "project's remote, so there is no way to start the work "
                    "from the commit that remote holds"
                ),
            )
            return None
        try:
            answer = await fetch(repo_path)
        except Exception as exc:  # noqa: BLE001 — boundary, never crash the run
            await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                (
                    f"the project's remote could not be read: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            return None
        if answer is None or not getattr(answer, "ok", False):
            reason = str(
                getattr(answer, "refusal", None)
                or "the project's remote gave no starting point and no reason"
            )
            await self._fail_leg(correlation_id, "target-terminal-enter", reason)
            return None

        commit = str(answer.commit)
        remote_branch = str(answer.branch)
        deps.store.record_start_point(
            correlation_id,
            start_commit=commit,
            target_branch=remote_branch,
        )
        logger.info(
            "planning driver: run %s starts from %s, the commit %s has for "
            "its default branch %s",
            correlation_id,
            commit,
            target_repo,
            remote_branch,
        )
        return commit, remote_branch

    async def _mission_start_point(
        self,
        correlation_id: str,
        target_repo: str,
        repo_path: str,
        membership: tuple[Any, Any, Any],
    ) -> tuple[str, str] | None:
        """A mission feature's start: its mission branch's tip, or None after failing.

        Inconsistent mission metadata fails the run honestly here, before any
        model is asked, naming what is wrong, and is never planned from the
        default branch instead: a mission that is missing or not RUNNING, an
        attempt that is not ADMITTED, a repository that is not the mission's,
        or a mission branch the remote does not have. Otherwise the branch is
        fetched by name and its tip is recorded as the start, with the mission
        branch as the target. The features it needs have landed there, so the
        tip contains them.
        """
        mission, attempt, _feature = membership
        problem: str | None = None
        if mission is None:
            problem = f"its mission ({attempt.mission_id}) is not on record"
        elif mission.status != "RUNNING":
            problem = f"its mission {mission.id} is {mission.status}, not running"
        elif mission.stopping:
            # A stop was asked for (section 11): the run is refused at its
            # mission check, and the attempt closes from this refusal.
            problem = (
                f"its mission {mission.id} was stopped by "
                f"{mission.stop_requested_by} at {mission.stop_requested_at}"
            )
        elif attempt.status != "ADMITTED":
            problem = f"its mission attempt is {attempt.status}, not admitted"
        elif mission.target_repo != target_repo:
            problem = (
                f"its mission is for {mission.target_repo}, not {target_repo}"
            )
        if problem is not None:
            await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                f"this run is part of a mission but {problem}, so it was not "
                "planned from anywhere",
            )
            return None
        fetch = getattr(self._deps.git_runner, "fetch_remote_start_point", None)
        if fetch is None:
            await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                "the git runner wired for this factory cannot fetch a "
                "project's remote, so the mission branch could not be read",
            )
            return None
        try:
            answer = await fetch(repo_path, branch=mission.branch)
        except Exception as exc:  # noqa: BLE001 — boundary, never crash the run
            await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                f"the mission branch {mission.branch} could not be read: "
                f"{type(exc).__name__}: {exc}",
            )
            return None
        commit = getattr(answer, "branch_commit", None) if answer is not None else None
        if answer is None or not getattr(answer, "ok", False) or not commit:
            reason = str(
                getattr(answer, "refusal", None)
                or f"the remote has no branch {mission.branch}"
            )
            await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                f"the mission branch {mission.branch} could not be read: {reason}",
            )
            return None
        self._deps.store.record_start_point(
            correlation_id, start_commit=str(commit), target_branch=mission.branch
        )
        logger.info(
            "planning driver: run %s is mission %s's %s and starts from %s, the "
            "tip of %s",
            correlation_id,
            mission.id,
            attempt.key,
            commit,
            mission.branch,
        )
        return str(commit), mission.branch

    async def _declarations(
        self,
        correlation_id: str,
        target_repo: str,
        repo_path: str,
        start_commit: str,
        *,
        request_text: str = "",
    ) -> tuple[str, tuple[str, ...]] | None:
        """What the project declares at the starting commit, or None on refusal.

        ``(memory name, the setting names its builds asked for)``. ONE read of
        ONE file at ONE commit answers both, because they live in the same file
        and asking twice is how two readers come to disagree.

        The rules written down (the project's own memory, item 2, 2026-09-21;
        the project's own launch settings, 22 September 2026):

        * a run that ALREADY has both recorded keeps them. A re-drive must not
          quietly move a run onto a different memory, or a different set of
          settings, than the one its branch was cut under. Nothing is read;
        * otherwise the git runner for this repository — the SAME one that
          fetched the starting commit and is about to cut the branch — is asked
          for the project's own ``.guardkit/config.yaml`` AS IT IS AT THAT
          COMMIT. Never the working folder, never the checked-out branch: a
          stale checkout must not be able to supply either answer;
        * both are recorded on the planning run BEFORE the branch is cut, so a
          crash between the two leaves a record of what the work was about to
          belong to rather than nothing at all;
        * a project that declares no memory, a name the memory service would
          refuse, a setting name this factory keeps for itself, or a settings
          file that could not be read at that commit, all fail the run loudly
          with their own sentence, which reaches the owner exactly where every
          other planning refusal does.
        """
        deps = self._deps
        recorded_name = None
        getter = getattr(deps.store, "get_memory_project", None)
        if getter is not None:
            recorded_name = getter(correlation_id)
        recorded_settings = None
        settings_getter = getattr(deps.store, "get_launch_settings", None)
        if settings_getter is not None:
            recorded_settings = settings_getter(correlation_id)
        if recorded_name and recorded_settings is not None:
            logger.info(
                "planning driver: run %s already belongs to the memory %s and "
                "already knows what its project asked to be launched with; not "
                "reading the declaration again",
                correlation_id,
                recorded_name,
            )
            self._say_once_when_no_documents_are_recorded(correlation_id)
            return str(recorded_name), tuple(recorded_settings)

        # The read itself is shared with the build admission of a feature
        # planned elsewhere (4 October 2026), so both refuse in the same words
        # and neither keeps a copy of it.
        declarations = await read_declarations_at_commit(
            deps.git_runner,
            repo=target_repo,
            repo_path=repo_path,
            commit=start_commit,
        )
        if not declarations.ok:
            await self._fail_leg(
                correlation_id,
                "target-terminal-enter",
                declarations.refusal
                or "the project's memory name could not be read",
            )
            return None

        # THE PROJECT'S OWN DOCUMENTS (project initialisation design, 4 October
        # 2026), out of the same file at the same commit, read and recorded
        # BEFORE the memory name and settings. So a run whose memory name is
        # recorded has always had its documents decided: recorded when the
        # project declares any, nothing when it declares none.
        if not await self._read_and_record_project_documents(
            correlation_id,
            repo_path=repo_path,
            start_commit=start_commit,
            config_text=declarations.content,
        ):
            return None
        # THE PROJECT'S REFERENCE MATERIAL (17 October 2026), in the same step
        # and on the same terms: out of the same file at the same commit,
        # recorded before the memory name, refused in plain words.
        if not await self._read_and_record_reference_material(
            correlation_id,
            repo_path=repo_path,
            start_commit=start_commit,
            config_text=declarations.content,
            request_text=request_text,
        ):
            return None

        name = str(declarations.memory_project)
        wanted_names = declarations.launch_settings
        recorder = getattr(deps.store, "record_memory_project", None)
        if recorder is not None:
            recorder(correlation_id, memory_project=name)
        settings_recorder = getattr(deps.store, "record_launch_settings", None)
        if settings_recorder is not None:
            settings_recorder(correlation_id, names=list(wanted_names))
        logger.info(
            "planning driver: run %s belongs to the memory %s, the name %s "
            "declares at %s, and asks to be launched with %s",
            correlation_id,
            name,
            target_repo,
            start_commit,
            # NAMES ONLY, never values — and this is central code, which does
            # not know or care what tool a name belongs to.
            ", ".join(wanted_names) or "nothing beyond the factory's own list",
        )
        return name, tuple(wanted_names)

    async def _read_and_record_project_documents(
        self,
        correlation_id: str,
        *,
        repo_path: str,
        start_commit: str,
        config_text: str | None,
    ) -> bool:
        """Read the project's documents at the starting commit and record them.

        True to carry on (recorded, or nothing declared); False after failing
        the run in plain words. Opt-in, as GuardKit's is: a project whose
        ``autobuild.player.required_documents`` is absent or empty has nothing
        read and nothing recorded, so every request it sends is what it was.
        A run that already has its documents recorded reads nothing again.
        """
        if self._project_documents_event(correlation_id) is not None:
            return True
        declared, why = read_declared_project_documents(config_text)
        documents: tuple[ProjectDocument, ...] = ()
        if why is None and declared.documents:
            documents, why = await read_project_documents_at_commit(
                self._deps.git_runner,
                repo_path=repo_path,
                commit=start_commit,
                declared=declared,
            )
        if why is not None:
            await self._fail_leg(correlation_id, "target-terminal-enter", why)
            return False
        if not declared.documents:
            return True
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_PROJECT_DOCUMENTS_STAGE,
            status="recorded",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {"project_documents": [d.to_record() for d in documents]}
            ),
        )
        logger.info(
            "planning driver: run %s will give the spec and plan writers %d "
            "project document(s) read at %s: %s",
            correlation_id,
            len(documents),
            start_commit,
            ", ".join(f"{d.path} ({d.bytes} bytes)" for d in documents),
        )
        return True

    async def _read_and_record_reference_material(
        self,
        correlation_id: str,
        *,
        repo_path: str,
        start_commit: str,
        config_text: str | None,
        request_text: str,
    ) -> bool:
        """Read the project's reference material at the starting commit and
        record it (17 October 2026, prototype-to-factory design 2.2).

        True to carry on (recorded, or nothing declared); False after failing
        the run in plain words, before any model is asked anything. Opt-in: a
        project without ``reference:`` has nothing read and nothing recorded,
        so every request it sends is what it was. With it, the signed-off
        record is checked when one is declared, the files the request names
        under the reference folders are read whole, and a request that uses
        one of the project's marker words but names no such file is refused
        (:func:`~forge.planning.reference_material.gather_reference_material`).
        A run that already has its material recorded reads nothing again.
        """
        if self._reference_material_event(correlation_id) is not None:
            return True
        declared, why = read_declared_reference(config_text)
        material: ReferenceMaterial | None = None
        if why is None and declared.declared:
            material, why = await gather_reference_material(
                self._deps.git_runner,
                repo_path=repo_path,
                commit=start_commit,
                declared=declared,
                request_text=request_text,
            )
        if why is not None:
            await self._fail_leg(correlation_id, "target-terminal-enter", why)
            return False
        if material is None:
            return True
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_REFERENCE_MATERIAL_STAGE,
            status="recorded",
            actor_identity="planning-driver",
            details_json=json.dumps({"reference_material": material.to_record()}),
        )
        logger.info(
            "planning driver: run %s treats %s as reference material, and "
            "will give the spec and plan writers %d named reference file(s) "
            "read at %s%s",
            correlation_id,
            ", ".join(material.paths),
            len(material.files),
            start_commit,
            (": " + ", ".join(f"{d.path} ({d.bytes} bytes)" for d in material.files))
            if material.files
            else "",
        )
        return True

    def _reference_material_event(self, correlation_id: str) -> Any | None:
        """The run's recorded reference-material row, or ``None`` (also when
        there is no ledger to ask: the facts sheet asks too, and must never
        fail over this)."""
        list_events = getattr(getattr(self._deps, "store", None), "list_events", None)
        if list_events is None:
            return None
        latest = None
        for event in list_events(correlation_id):
            if (
                event["stage_label"] == _REFERENCE_MATERIAL_STAGE
                and event["status"] == "recorded"
            ):
                latest = event
        return latest

    def _recorded_reference_material(
        self, correlation_id: str
    ) -> tuple[ReferenceMaterial | None, str | None]:
        """What the door recorded: ``(material, None)``; ``(None, None)`` when
        the project declares no reference folders; ``(None, why)`` when the
        record does not hold together (sending nothing in its place would
        quietly drop what the request named)."""
        event = self._reference_material_event(correlation_id)
        if event is None:
            return None, None
        broken = (
            "the reference material recorded for this run at its start could "
            "not be read back intact, so it cannot be given to the writers"
        )
        try:
            record = json.loads(event["details_json"] or "{}").get(
                "reference_material"
            )
        except (json.JSONDecodeError, ValueError, AttributeError):
            return None, broken
        material = ReferenceMaterial.from_record(record)
        if material is None:
            return None, broken
        return material, None

    def _reference_paths_of(self, correlation_id: str) -> tuple[str, ...]:
        """The run's recorded reference folders; ``()`` when none."""
        material, _broken = self._recorded_reference_material(correlation_id)
        return material.paths if material is not None else ()

    def _say_once_when_no_documents_are_recorded(self, correlation_id: str) -> None:
        """One log line for a run past the door with no documents record.

        Documents are read and recorded before the memory name, so such a run
        either declares none or passed the door before documents were read (4
        October 2026). Either way its writers are sent none, and nothing is
        read now: a re-drive never moves a run onto what its project says
        today. Said once per run per process.
        """
        said: set[str] = self.__dict__.setdefault("_no_documents_said", set())
        if correlation_id in said:
            return
        if self._project_documents_event(correlation_id) is not None:
            return
        said.add(correlation_id)
        logger.info(
            "planning driver: run %s has no project documents recorded (its "
            "project declares none, or its door ran before documents were "
            "read); its spec and plan writers are sent none",
            correlation_id,
        )

    def _project_documents_event(self, correlation_id: str) -> Any | None:
        """The run's recorded project-documents row, or ``None``."""
        latest = None
        for event in self._deps.store.list_events(correlation_id):
            if (
                event["stage_label"] == _PROJECT_DOCUMENTS_STAGE
                and event["status"] == "recorded"
            ):
                latest = event
        return latest

    def _recorded_project_documents(
        self, correlation_id: str
    ) -> tuple[tuple[ProjectDocument, ...], str | None]:
        """What the door recorded, to send as it is: ``(documents, None)``.

        ``((), None)`` when nothing was recorded (the project declares no
        binding documents). ``((), why)`` when the record does not hold
        together — sending nothing in its place would quietly drop a document
        the project said its work is held to.
        """
        event = self._project_documents_event(correlation_id)
        if event is None:
            return (), None
        broken = (
            "the project documents recorded for this run at its start could "
            "not be read back intact, so they cannot be given to the writers"
        )
        try:
            records = json.loads(event["details_json"] or "{}").get(
                "project_documents"
            )
        except (json.JSONDecodeError, ValueError, AttributeError):
            return (), broken
        if not isinstance(records, list) or not records:
            return (), broken
        documents = [ProjectDocument.from_record(record) for record in records]
        if any(document is None for document in documents):
            return (), broken
        return tuple(d for d in documents if d is not None), None

    @staticmethod
    def _project_documents_receipt(
        documents: tuple[ProjectDocument, ...],
    ) -> dict[str, Any]:
        """What a writer's receipt records about the documents it was sent."""
        return {
            "status": "sent",
            "documents": [document.receipt() for document in documents],
        }

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
                # The draft row is written HERE, with the card, so the card a
                # restart replays is the card that was shown.
                draft = self._open_the_card(correlation_id, drafted)

            # THE ONE PAUSE. A run configured to skip the card on a thin
            # feature skips it here — mechanically decidable, no judgement.
            skip = self._digest_review_skip_reason(draft, correlation_id)
            mission_review = self._mission_spec_review(correlation_id, draft)
            if skip is not None and mission_review is not None:
                # A mission feature: Ready means spec approved, recorded with
                # the mission approval rather than silent (section 5).
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
                return self._record_spec_approved(
                    correlation_id, draft, branch, spec_review=mission_review
                )
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
        """Dispatch the spec-writer, commit the spec, return the draft.

        ``notes`` carries the owner's plain-English notes from earlier rounds;
        the newest is threaded back as ``validate_feedback`` VERBATIM together
        with the prior artifact set as ``revision_of`` — discharging the C5
        channel that has been vocabulary since it was designed. A first-round
        dispatch passes neither and is byte-identical to the call that shipped.

        The draft row is written by :meth:`_open_the_card`.

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

        # The project's documents, exactly as the door recorded them, on EVERY
        # spec call, rewrites included. The separate factory reference follows
        # these and the target's reference files.
        documents, broken = self._recorded_project_documents(correlation_id)
        if broken is not None:
            await self._fail_leg(correlation_id, _FEATURE_SPEC_STAGE, broken)
            return None
        # The files the request named under the project's reference folders
        # (17 October 2026), after the documents, each under its heading.
        # None named, or no folders declared: the call is today's.
        reference, broken = self._recorded_reference_material(correlation_id)
        if broken is not None:
            await self._fail_leg(correlation_id, _FEATURE_SPEC_STAGE, broken)
            return None
        reference_files = reference.files if reference is not None else ()
        factory_document = ensure_factory_context(deps.store, correlation_id)
        texts = context_texts(documents) + reference_context_texts(reference_files)
        # The project's decision records from memory, as the door recorded
        # them, before the factory reference. None recorded: nothing is added.
        memory_text, memory_receipt = self._recorded_memory_decisions(correlation_id)
        if memory_text:
            texts.append(memory_text)
        if factory_document is not None:
            texts.append(factory_document.context_text())
        project_context: dict[str, Any] = {"context": texts} if texts else {}
        # What the repository already has where the request's words and
        # quoted examples appear (17 October 2026, FEAT-15DD): the same
        # windows the plan writer is shown, built once per run. No windows,
        # no field: the call is today's.
        code_evidence = await self._spec_code_evidence(
            row, correlation_id, target_repo=target_repo, repo_path=repo_path
        )
        if code_evidence is not None:
            project_context["code_evidence"] = code_evidence

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
                # The coach's ground truth (2026-09-13): the sentence, word for
                # word, and what the repository already does for its words.
                request_text=self._request_text_of(row),
                repository_facts=await self._repository_facts_for(correlation_id, repo_path, row),
                **project_context,
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
            # After a note, a not-ok dispatch is the checker refusing the
            # rewrite (the must-pass "what the note asks must change, and
            # nothing else may move without reason"). The owner is told what
            # happened, whose note it was, and what to do — rule 23,
            # 2026-09-06. A first-round failure keeps the plain default.
            owner_message = (
                self._note_refused_message(
                    correlation_id,
                    note=str(notes[-1]),
                    reason=getattr(result, "reason", None),
                )
                if notes
                else None
            )
            await self._fail_leg(
                correlation_id,
                _FEATURE_SPEC_STAGE,
                f"007 dispatch {reason}"
                + (
                    " (the checker refused the revision round after the owner's note)"
                    if notes
                    else ""
                ),
                owner_message=owner_message,
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

        feature_rel = self._feature_file_rel(files)
        normalize = deps.normalize_feature_spec

        async def _pre_commit(worktree: Path) -> PreCommitResult:
            if feature_rel is None:
                return PreCommitResult(
                    ok=False, detail="spec contract has no .feature file to normalize"
                )
            outcome = await normalize(worktree, feature_rel)
            return PreCommitResult(ok=outcome.ok, detail=outcome.detail)

        # Sandbox first (2026-09-07, rule 87): a git runner that lives in the
        # repository's sandbox cannot run the closure above. For such a runner
        # the SAME normalizer is DECLARED by name against the same file. The
        # in-container runner keeps the closure.
        git_runner = self._git_runner_for(target_repo)
        pre_commit: Any = _pre_commit
        if self._runner_takes_declared_checks(git_runner):
            if feature_rel is None:
                await self._fail_leg(
                    correlation_id,
                    _FEATURE_SPEC_STAGE,
                    "spec write / normalizer failed: spec contract has no "
                    ".feature file to normalize",
                )
                return None
            pre_commit = self._declare_spec_checks(correlation_id, feature_rel)

        try:
            (
                recorded_memory,
                recorded_settings,
                whose_work,
                declared_at,
            ) = self._recorded_launch(correlation_id)
            gitres = await git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=f"planning: feature spec for {correlation_id} (Lane B 007)",
                pre_commit=pre_commit,
                memory_project=recorded_memory,
                launch_settings=recorded_settings,
                build=whose_work,
                declared_at=declared_at,
                feature_routing_id=correlation_id,
                feature_routing_required=self._deps.feature_routing_required,
                feature_routing_receipt=self._feature_routing_receipt(correlation_id),
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
        digest_obj, card = await self._spec_card(
            correlation_id,
            target_repo=target_repo,
            repo_path=repo_path,
            branch=branch,
            files=files,
            digest_text=digest_text,
            seed=seed,
        )

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
                card, rewrite = self._compare_with_previous_card(
                    previous,
                    card,
                    note=str(notes[-1]),
                )

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
        if factory_document is not None:
            draft["factory_context"] = factory_document.receipt()
        if memory_receipt is not None:
            draft[memory_decisions.RECEIPT_KEY] = memory_receipt
        if documents:
            # Which files, which versions, were given to the spec writer.
            draft["project_documents"] = self._project_documents_receipt(documents)
        if reference_files:
            draft["reference_files"] = self._project_documents_receipt(
                reference_files
            )
        if rewrite is not None:
            # Persisted on the draft row, not on the card: the door's opening
            # notification reads it, and a restart that re-opens the draft
            # re-reads it rather than re-deriving it from a branch that has
            # moved on. A renderer never sees it.
            draft["rewrite"] = rewrite
        # THE POSSIBLE CONTRADICTION (4 October 2026). Read from THIS reply, so
        # a rewrite always carries its own (or none). Saved on the draft row so
        # a restart replays it; the card gets it in _open_the_card_with.
        coherence_warning = self._capture_coherence_warning(
            role_output, correlation_id
        )
        if coherence_warning is not None:
            draft["coherence_warning"] = coherence_warning
        return draft

    @staticmethod
    def _compare_with_previous_card(
        previous: Mapping[str, Any],
        card: Mapping[str, Any],
        *,
        note: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """What a note's rewrite changed on the list, and the card's words for
        it. Returns ``(card, rewrite)``: the rewrite record (``repeat``, the
        note, the what-changed sentence) and the card, annotated with the
        same-list text or the changed-list line."""
        rewrite: dict[str, Any]
        if _cards_say_the_same_thing(previous, card):
            rewrite = {"repeat": True, "note": note, "changes": ""}
        else:
            changes = _plain_card_changes(previous, card)
            rewrite = {"repeat": False, "note": note, "changes": changes}
        card = dict(card)
        if rewrite["repeat"]:
            card["what_happened"] = _SAME_LIST_CARD_TEXT.format(note=note)
        else:
            card["what_happened"] = (
                f"{card.get('what_happened', '')} "
                + _CHANGED_LIST_CARD_LINE.format(changes=rewrite["changes"])
            ).strip()
        return card, rewrite

    def _record_spec_draft(
        self,
        correlation_id: str,
        draft: Mapping[str, Any],
    ) -> None:
        """Write the durable ``drafted`` row for a committed draft.

        Status ``drafted``, deliberately NOT ``approved``: this row is the
        door's restart sentinel, never the leg's.
        """
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_SPEC_DRAFT_STAGE,
            status="drafted",
            actor_identity="planning-driver",
            details_json=json.dumps({"spec_draft": dict(draft)}),
        )
        logger.info(
            "planning driver: run %s feature-spec drafted (slug=%s, %d files, "
            "%d worked example(s), pass-bar seed %s) — showing the digest",
            correlation_id,
            draft.get("slug"),
            len(draft.get("spec_files") or []),
            int(draft.get("scenario_count") or 0),
            "captured" if draft.get("pass_bar_seed") is not None else "ABSENT",
        )

    @staticmethod
    def _request_text_of(row: Any) -> str:
        """The sentence, word for word, off the run row; empty when absent."""
        try:
            return str(row["request_text"] or "")
        except (KeyError, IndexError, TypeError):
            return str(getattr(row, "request_text", "") or "")

    async def _spec_code_evidence(
        self,
        row: Any,
        correlation_id: str,
        *,
        target_repo: str,
        repo_path: str,
    ) -> dict[str, Any] | None:
        """The code windows the spec writer is shown, made once per run.

        WHY (17 October 2026, FEAT-15DD). The spec writer was shown no code,
        so it settled an open question ("1 note" or "1 notes") by guessing,
        though the project's code and a committed browser check already
        answered it. Since 1 October Forge reads the code itself, so the seat
        still needs no checkout.

        The same :meth:`_build_target_repo_descriptor` the plan leg calls,
        from the request alone (there is no specification yet), at half the
        plan's window budget
        (:data:`~forge.planning.code_evidence.MAX_SPEC_EVIDENCE_CHARS`) and
        never with the ranked search. Only the entries carrying the windows
        :func:`~forge.planning.code_evidence.choose_windows` picked are kept
        (:func:`~forge.planning.code_evidence.spec_writer_windows`), so
        reference material stays labelled and capped, and no ranked block,
        set line, file list or rule reaches the spec writer.

        Kept while the run is driven: every spec call of one drive (rewrites
        included) is sent the same windows, and the repository is read once
        for them; :meth:`drive` lets them go when it returns.
        ``None`` when nothing was found, or the repository could not be read
        (the fact sheet, sent beside, already says that); the field is then
        not sent. Never raises.
        """
        cache: dict[str, dict[str, Any] | None] = self.__dict__.setdefault(
            "_spec_code_evidence_cache", {}
        )
        if correlation_id in cache:
            return cache[correlation_id]
        evidence: dict[str, Any] | None = None
        try:
            descriptor = await asyncio.to_thread(
                self._build_target_repo_descriptor,
                target_repo,
                repo_path,
                "",
                reader=self._repository_reader_for(repo_path, correlation_id),
                unavailable=[],
                partial=[],
                request_text=self._request_text_of(row),
                ranked_source=False,
                reference_paths=self._reference_paths_of(correlation_id),
                max_evidence_chars=MAX_SPEC_EVIDENCE_CHARS,
            )
            evidence = spec_writer_windows(descriptor)
        except Exception as exc:  # noqa: BLE001 — never fail a spec over this
            logger.warning(
                "planning driver: run %s — the spec writer's code windows could "
                "not be built (%s: %s); it is sent none",
                correlation_id,
                type(exc).__name__,
                exc,
            )
        if evidence is not None:
            logger.info(
                "planning driver: run %s — the spec writer is shown %d code "
                "window(s), %d characters of window text",
                correlation_id,
                sum(len(e["evidence"]) for e in evidence["where_the_specs_words_already_appear"]),
                sum(
                    len(w.get("text") or "")
                    for e in evidence["where_the_specs_words_already_appear"]
                    for w in e["evidence"]
                ),
            )
        cache[correlation_id] = evidence
        return evidence

    async def _repository_facts_for(
        self, correlation_id: str, repo_path: str, row: Any
    ) -> str | None:
        """The fact sheet's text for this run, made once and kept for the leg.

        What the coach and the deterministic reviewers are given: the facts,
        or — when the repository could not be read — the explicit
        "Repository facts unavailable: <reason>" sentence (1 October 2026,
        the 1 October planner fix). ``None`` only when the request has nothing to
        look for or the repository has nothing to say about it, so the
        dispatch is then byte for byte what it was.
        """
        return (await self._repository_facts_state(correlation_id, repo_path, row)).text

    async def _repository_facts_state(
        self, correlation_id: str, repo_path: str, row: Any
    ) -> RepositoryFacts:
        """The whole answer, cached per run so the coach, the reviewers and
        the card all see the same sheet. Read off the event loop: a sandboxed
        repository is a dozen small HTTP reads."""
        cache: dict[str, RepositoryFacts] = self.__dict__.setdefault(
            "_repository_facts_cache", {}
        )
        if correlation_id not in cache:
            reader = self._repository_reader_for(repo_path, correlation_id)
            # The facts describe the product: the project's reference
            # folders, when it declares any, are left out (17 October 2026).
            reference_paths = self._reference_paths_of(correlation_id)
            facts = await asyncio.to_thread(
                read_repository_facts,
                reader,
                self._request_text_of(row),
                **({"reference_paths": reference_paths} if reference_paths else {}),
            )
            if facts.unavailable:
                logger.warning(
                    "planning driver: run %s — the planner's repository facts "
                    "are UNAVAILABLE (%s); the coach is told so, and so is the card",
                    correlation_id,
                    facts.unavailable,
                )
            cache[correlation_id] = facts
        return cache[correlation_id]

    def _recorded_start(self, correlation_id: str | None) -> tuple[str | None, str | None]:
        """``(the run's recorded starting commit or None, None)``, or
        ``(None, why)`` when looking it up FAILED.

        An ABSENT start (the store answers ``(None, None)``: a run older than
        the starting rule, or no store to ask) is ``(None, None)`` and reads
        as before. A lookup that failed is not an absent start (Codex review
        round 1, 7 October 2026): its reads are "could not read the starting
        commit", never a working tree in its place.
        """
        start_point = getattr(getattr(self._deps, "store", None), "get_start_point", None)
        if correlation_id is None or not callable(start_point):
            return None, None
        failed: str | None = None
        recorded: Any = None
        try:
            recorded = start_point(correlation_id)
        except Exception as exc:  # noqa: BLE001 — said, never a working tree
            failed = f"{type(exc).__name__}: {exc}"
        else:
            if not isinstance(recorded, (tuple, list)) or len(recorded) != 2:
                failed = f"the run store answered {type(recorded).__name__}"
            else:
                first = recorded[0]
                if first is None:
                    return None, None
                if isinstance(first, str) and first.strip():
                    return first.strip(), None
                failed = f"the run store's recorded start is {first!r}"
        return None, (
            f"could not read the starting commit of run {correlation_id}: "
            f"looking it up failed ({str(failed)[:200]})"
        )

    def _repository_reader_for(self, repo_path: str, correlation_id: str | None = None) -> Any:
        """Where the fact sheet reads the repository: wherever every other
        planning call for ``repo_path`` goes.

        A sandboxed repository's git runner (its helper's
        :class:`~forge.planning.sidecar_git_runner.SidecarGitRunner`, reached
        through the routed runner exactly as the spec branch's own writes
        are) offers a ``code_reader`` over the helper's read-only ``/code``
        routes, on the factory's own clone inside the sandbox — the copy the
        builds see. Every other repository is read from the checkout the
        coordinator holds, as before. There is deliberately no fallback from
        one to the other: a helper that cannot be reached is said on the
        card, not papered over with a checkout that may be a different copy.

        With ``correlation_id`` the reader is kept for the run, so a helper
        found unreachable once is not waited on again by every later read of
        the same run (the 1 October planner fix, review round 1).

        AT THE RUN'S STARTING COMMIT (7 October 2026). The reader reads the
        commit the run recorded as its start, never whatever the copy has
        checked out. Until then it read the sandbox clone's working tree,
        which on 7 October was four days behind the remote's main the run
        started from, and carried the factory's own local edits to two
        deploy files: every plan in those four days was shown four-day-old
        code. A commit that cannot be read is said as "could not read the
        starting commit", never answered from the working tree. A run with
        no recorded start (only one older than the starting rule) reads as
        before.
        """
        cache: dict[tuple[str, str], Any] = self.__dict__.setdefault("_repository_readers", {})
        key = (str(correlation_id), str(repo_path))
        if correlation_id is not None and key in cache:
            return cache[key]
        commit, failed = self._recorded_start(correlation_id)
        if failed is not None:
            logger.warning("planning driver: %s; nothing is read in its place", failed)
            reader = UnreadableRepository(failed)
            if correlation_id is not None:
                cache[key] = reader
            return reader
        runner = self._deps.git_runner
        route = getattr(runner, "runner_for_path", None)
        if callable(route):
            runner = route(repo_path)
        make = getattr(runner, "code_reader", None)
        if callable(make):
            reader = make(commit) if commit else make()
        else:
            repo_key, address = self._fetch_address_for(repo_path)
            reader = LocalCheckoutReader(
                repo_path, commit=commit, fetch_address=address, repo=repo_key
            )
        if correlation_id is not None:
            cache[key] = reader
        return reader

    def _fetch_address_for(self, repo_path: str) -> tuple[str | None, str | None]:
        """``(org/name, recorded fetch address)`` for the checkout at
        ``repo_path``, from the coordinator's own settings
        (``planning.target_repo_paths`` and ``planning.fetch_addresses``);
        either may be ``None``. The checkout's own origin is never read."""
        from forge.network_git import address_for_path

        cfg = getattr(self._deps, "planning_config", None)
        return address_for_path(
            getattr(cfg, "target_repo_paths", None) or {},
            getattr(cfg, "fetch_addresses", None) or {},
            repo_path,
        )

    async def _plan_repository_facts(
        self, correlation_id: str, repo_path: str, row: Any
    ) -> str | None:
        """What the plan-writer's coach is given as ``repository_facts``: the
        fact sheet, plus — when the descriptor's inventory or the search for
        the specification's words could not read the repository — the same
        "Repository facts unavailable: <reason>" sentence (2 October 2026 item
        10). Said once: a sheet already unavailable is not repeated."""
        facts = await self._repository_facts_state(correlation_id, repo_path, row)
        parts = [facts.text] if facts.text else []
        reason = (self.__dict__.get("_descriptor_unavailable") or {}).get(correlation_id)
        if reason and not facts.unavailable:
            parts.append(unavailable_text(reason))
        partly = (self.__dict__.get("_descriptor_partial") or {}).get(correlation_id)
        if partly:
            # Read in part is not "could not be read": said as what it is.
            parts.append(
                "Repository facts partly read: "
                + "; ".join(dict.fromkeys(partly))
                + ". What was found is listed; it may not be everything."
            )
        return "\n".join(parts) if parts else None

    def _repository_unavailable_line(self, correlation_id: str) -> str | None:
        """The plain line for the build gate: the fact sheet's state, else the
        plan-writer's descriptor's (its inventory, rules, test folders or the
        specification's words) — "could not read" before "read only in
        part"; ``None`` when everything the planner reads was read."""
        cache = self.__dict__.get("_repository_facts_cache") or {}
        facts = cache.get(correlation_id)
        if facts is not None and facts.unavailable:
            return _REPOSITORY_FACTS_UNAVAILABLE_CARD_LINE.format(reason=facts.unavailable)
        reason = (self.__dict__.get("_descriptor_unavailable") or {}).get(correlation_id)
        if reason:
            return _REPOSITORY_FACTS_UNAVAILABLE_CARD_LINE.format(reason=reason)
        if facts is not None and facts.partial:
            return _partial_card_line(facts.partial)
        partly = (self.__dict__.get("_descriptor_partial") or {}).get(correlation_id)
        if partly:
            return _partial_card_line("; ".join(dict.fromkeys(partly)))
        return None

    def _repository_facts_card_line(self, correlation_id: str) -> str | None:
        """The card's one line when the planner could not read the
        repository; ``None`` when it could (or never had to)."""
        cache = self.__dict__.get("_repository_facts_cache") or {}
        facts = cache.get(correlation_id)
        if facts is None:
            return None
        if facts.unavailable:
            return _REPOSITORY_FACTS_UNAVAILABLE_CARD_LINE.format(reason=facts.unavailable)
        if facts.partial:
            return _partial_card_line(facts.partial)
        return None


    # ------------------------------------------------------------------ #
    # The worked examples are checked against the request before the card
    # (4 October 2026), by the words the project itself declares
    # ------------------------------------------------------------------ #


    def _open_the_card(
        self, correlation_id: str, draft: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Put the spec writer's possible contradiction and the repository
        line on the card, write the draft's ``drafted`` row, and return the
        draft the door opens on."""
        final = dict(draft)
        # The possible contradiction gets a field of its own, never a line in
        # what_happened (4 October 2026). No warning: the card is unchanged.
        warning = final.get("coherence_warning")
        if isinstance(warning, Mapping) and warning.get("possible_contradiction"):
            card = dict(final.get("card") or {})
            card["possible_contradiction"] = str(warning["possible_contradiction"])
            final["card"] = card
        elif "possible_contradiction" in (final.get("card") or {}):
            card = dict(final.get("card") or {})
            card.pop("possible_contradiction", None)
            final["card"] = card
        unavailable_line = self._repository_facts_card_line(correlation_id)
        if unavailable_line is not None:
            # Silence was the bug (1 October 2026, the 1 October planner fix): the
            # person approving the spec is told the checker never saw the
            # repository, and why. Receipted on the draft row beside the card.
            card = dict(final.get("card") or {})
            card["what_happened"] = (
                f"{card.get('what_happened', '')} {unavailable_line}".strip()
            )
            final["card"] = card
            final["repository_facts_unavailable"] = unavailable_line
        self._record_spec_draft(correlation_id, final)
        return final


    async def _spec_card(
        self,
        correlation_id: str,
        *,
        target_repo: str | None = None,
        repo_path: str,
        branch: str,
        files: Mapping[str, str],
        digest_text: str | None,
        seed: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read the digest and the COMMITTED spec, then build the card.

        Returns ``(digest, card)``. A digest that is missing or cannot be read
        gives an empty summary; the worked examples ride along and the person
        reading the card decides (the cull, 27 October 2026).
        """
        digest_obj: Any = {}
        if digest_text and str(digest_text).strip():
            try:
                digest_obj = yaml.safe_load(str(digest_text))
            except yaml.YAMLError:
                digest_obj = {}
        if not isinstance(digest_obj, Mapping):
            digest_obj = {}
        feature_rel = self._feature_file_rel(files)
        committed_feature: str | None = None
        if feature_rel:
            committed_feature = await self._deps.git_runner.read_file_from_branch(
                repo_path=repo_path, branch=branch, file_path=feature_rel
            )
        card = self._digest_review_card(
            correlation_id,
            digest_obj=digest_obj,
            feature_text=committed_feature or "",
            seed=seed,
            target_repo=target_repo,
        )
        return dict(digest_obj), card

    def _record_spec_approved(
        self,
        correlation_id: str,
        draft: Mapping[str, Any],
        branch: str,
        *,
        answer: "_DoorAnswer | None" = None,
        spec_review: Mapping[str, Any] | None = None,
    ) -> bool:
        """Write the leg's durable ``approved`` row and advance to the plan leg.

        ``spec_review`` is a mission feature's record of the mission approval
        (:meth:`_mission_spec_review`), written in the same shape a card's
        answer is.

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
        elif spec_review is not None:
            record["spec_review"] = dict(spec_review)
        decided_by = (answer.decided_by if answer else None) or (
            spec_review.get("decided_by") if spec_review else None
        )
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_FEATURE_SPEC_STAGE,
            status="approved",
            actor_identity=decided_by or "planning-driver",
            details_json=json.dumps(record),
        )
        logger.info(
            "planning driver: run %s feature-spec approved (slug=%s, %d files)",
            correlation_id,
            record["slug"],
            len(record["spec_files"]),
        )
        return self._advance_after_spec(correlation_id)


    @staticmethod
    def _note_refused_message(
        correlation_id: str,
        *,
        note: str,
        reason: Any,
    ) -> str:
        """The owner's sentence when the checker refused a revision round
        (rule 23): whose note it was, the note itself, the checker's reason in
        one sentence, that nothing was built, and what to do next."""
        one_sentence = " ".join(str(reason or "").split()).rstrip(".").strip()
        reason_words = one_sentence or "the checker gave no reason"
        return _NOTE_REFUSED_MESSAGE.format(
            correlation_id=correlation_id,
            note=note,
            reason=reason_words,
        )

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
            # A mission feature's answer came from the mission message, not a
            # card (missions design, section 5), and the record says so.
            "answered_on": (
                "the mission message"
                if review.get("source") == "mission message"
                else "the spec digest card"
            ),
        }

    def _mission_sign_in_answer(self, correlation_id: str) -> dict[str, Any] | None:
        """For a mission feature with no recorded sign-in answer, the answer the
        mission message gave, so no sign-in card is ever opened inside a
        mission; None for any other run."""
        membership = mission_membership(
            getattr(self._deps.store, "_connection", None), correlation_id
        )
        if membership is None or membership[0] is None:
            return None
        mission, _attempt, feature = membership
        said = feature is not None and feature.no_sign_in
        return {
            "outcome": "confirmed" if said else _MISSION_SIGN_IN_NOT_SAID,
            "decided_by": mission.approved_by,
            "request_id": mission.plan_request_id,
            "answered_on": "the mission message",
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

    def _mission_spec_review(
        self, correlation_id: str, draft: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """A mission feature's spec approval record, or None for any other run.

        Moving a feature to Ready is approving its spec (Rich, 17 October
        2026), so a mission feature's spec is recorded as approved by whoever
        approved the mission plan, citing that card's request. The sign-in
        question (section 5): when the spec is flagged, an author who wrote
        ``[no sign-in]`` has supplied the answer, recorded as ``confirmed``
        with ``source: "mission message"``; without it the answer is recorded
        as not given, which the pass-bars leg turns into its existing honest
        stop with no card opened.
        """
        membership = mission_membership(
            getattr(self._deps.store, "_connection", None), correlation_id
        )
        if membership is None or membership[0] is None:
            return None
        mission, _attempt, feature = membership
        review: dict[str, Any] = {
            "decided_by": mission.approved_by,
            "request_id": mission.plan_request_id,
            "source": "mission message",
            "mission_id": mission.id,
        }
        if (draft.get("card") or {}).get("sign_in_check"):
            if feature is not None and feature.no_sign_in:
                review["sign_in_answer"] = "confirmed"
                review["auth_confirmed"] = True
            else:
                review["sign_in_answer"] = _MISSION_SIGN_IN_NOT_SAID
        return review

    def _digest_review_skip_reason(
        self, draft: Mapping[str, Any], correlation_id: str | None = None
    ) -> str | None:
        """Why this run may skip the digest card, or ``None`` (it always asks).

        A mission feature always skips it, with the reason "part of mission
        <id>, approved by <who> at <when>" (missions design, section 5):
        Ready means spec approved, and the mission plan card was the approval.

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
        membership = mission_membership(
            getattr(self._deps.store, "_connection", None), correlation_id
        )
        if membership is not None and membership[0] is not None:
            mission = membership[0]
            return (
                f"part of mission {mission.id}, approved by "
                f"{mission.approved_by} at {mission.approved_at}"
            )
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
                        f"Planning run {correlation_id}: {_person_words(answer.decided_by_name)} "
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
                    f"{_person_words(answer.decided_by_name)} said yes "
                    "to the spec. Writing the task plan and the quality "
                    "checklist next — nothing is built until you give the "
                    "go-ahead.",
                    level="info",
                )
            elif answer.outcome == "revise":
                await self._notify(
                    correlation_id,
                    f"Planning run {correlation_id}: "
                    f"{_person_words(answer.decided_by_name)} "
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
            # ``_approver`` is the chat id the door routes by; the line it
            # writes never prints one, so it is not passed on.
            open_message=lambda _approver, wait: self._digest_door_open_message(
                correlation_id,
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
                f"No answer within {self._plain_wait(wait_seconds)}: the same "
                "card is sent once more, and with no answer after another "
                f"{self._plain_wait(wait_seconds)} the run stops and says so — "
                "nothing is built."
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
        # No name travels this far — the door routes by a chat id, and an id is
        # never printed — so this reads "You have a card…".
        who = _person_words(None)
        subject, verb = ("You", "have") if who == "you" else (who, "has")
        return (
            f"Planning run {correlation_id}: the spec is written. {subject} "
            f"{verb} a card listing, in one sentence each, everything this "
            "build will be checked against. Say yes and the machine writes the "
            "task plan and the quality checklist; send a note and it rewrites "
            f"the spec from what you say. No answer within "
            f"{self._plain_wait(wait_seconds)} sends the same card once more, "
            "and silence after that stops the run — nothing is built either "
            "way until you give the go-ahead."
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
        """FEATURE_PLAN leg: mint the FEAT id, dispatch 008, write the plan.

        Returns True once the plan tree is committed (the caller proceeds to
        the B3 build trigger), False when the run has ended (failed, or
        nothing to build). A durable ``feature-plan`` approved event
        short-circuits a re-drive (no re-dispatch — returns True so the build
        trigger still runs, which is what makes a crash between the plan
        commit and BUILD_QUEUED recover).
        """
        deps = self._deps
        if self._has_leg_event(correlation_id, _FEATURE_PLAN_STAGE):
            logger.info(
                "planning driver: run %s feature-plan already complete "
                "(idempotent re-drive — proceeding to the B3 build trigger)",
                correlation_id,
            )
            return True

        if deps.dispatch_feature_plan is None:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "target terminal ON but the plan leg collaborator "
                "(dispatch_feature_plan) is not wired",
            )
            return False
        resolved = await self._resolve_repo(row, correlation_id, stage_label=_FEATURE_PLAN_STAGE)
        if resolved is None:
            return False
        target_repo, repo_path = resolved
        branch = f"planning/{correlation_id}"
        plan_run_id = f"plan-{correlation_id}"
        # The feature id is minted ONCE per run (rule 3).
        feature_id = self._mint_feature_id(correlation_id)

        attempt = await self._plan_attempt(
            row,
            correlation_id,
            target_repo=target_repo,
            repo_path=repo_path,
            branch=branch,
            plan_run_id=plan_run_id,
            feature_id=feature_id,
            round_label="first",
        )
        if attempt is None:
            return False  # already loud and terminal
        # "Already done, nothing to build" (7 October 2026): the answer ends
        # the run; one that could not be read is followed by one ordinary plan
        # (24 October 2026), which carries on below as usual.
        if attempt.nothing_to_build is not None:
            plan_note = await self._finish_nothing_to_build(
                row,
                correlation_id,
                attempt.nothing_to_build,
                target_repo=target_repo,
                feature_id=feature_id,
            )
            if plan_note is None:
                return False
            attempt = await self._plan_attempt(
                row,
                correlation_id,
                target_repo=target_repo,
                repo_path=repo_path,
                branch=branch,
                plan_run_id=plan_run_id,
                feature_id=feature_id,
                round_label="first (an ordinary plan after an already-done answer)",
                plan_note=plan_note,
            )
            if attempt is None:
                return False
            if attempt.nothing_to_build is not None:
                return await self._fail_leg(
                    correlation_id,
                    _FEATURE_PLAN_STAGE,
                    "the planner answered already done again when asked for an "
                    "ordinary plan",
                )
        return await self._finish_feature_plan(
            correlation_id,
            attempt,
            feature_id=feature_id,
            target_repo=target_repo,
            branch=branch,
            repo_path=repo_path,
        )

    async def _plan_attempt(
        self,
        row: Any,
        correlation_id: str,
        *,
        target_repo: str,
        repo_path: str,
        branch: str,
        plan_run_id: str,
        feature_id: str,
        round_label: str,
        plan_note: str | None = None,
    ) -> "_PlanAttempt | None":
        """One pass of the plan leg's work: read the approved spec back off the
        branch, dispatch the plan-writer, write the plan tree.

        ``row`` is the run row, handed down from :meth:`_feature_plan_leg`,
        which already holds it (2026-09-15): the sentence the person actually
        sent lives on it.

        ``plan_note`` (step 1 simplification, 24 October 2026): set for the one
        ordinary plan asked for after an already-done answer that could not be
        used. It goes to the plan writer as its note, and the already-done
        answer is not offered on that call.

        Returns the attempt record, or ``None`` after failing the run loudly.
        The plan reviewer's record is read for the build card only (the cull,
        27 October 2026): nothing here refuses on what a model wrote about the
        plan.
        """
        deps = self._deps
        spec_details = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE)
        slug = str(spec_details.get("slug") or self._slug_of({}, correlation_id))

        # The 008 contract needs the CONTENTS of the committed spec triple, not
        # paths. Read them back off the planning branch (they are no longer in
        # memory after the spec leg returned — and on an idempotent re-drive the
        # spec leg never ran this drive at all). The .feature is the committed,
        # normalizer-collapsed content of record.
        spec_files = spec_details.get("spec_files") or []
        spec_feature, spec_summary, spec_assumptions = await self._read_spec_triple(
            repo_path, branch, spec_files
        )
        if not spec_feature:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "could not read the committed spec's .feature off branch "
                f"{branch} (spec_files={sorted(spec_files)})",
            )
            return None
        # The summary and assumptions files are optional (the cull, 27
        # October 2026): one that did not come is sent as nothing.
        spec_summary = spec_summary or ""
        descriptor_unavailable: list[str] = []
        descriptor_partial: list[str] = []
        target_repo_descriptor = await asyncio.to_thread(
            self._build_target_repo_descriptor,
            target_repo,
            repo_path,
            spec_feature,
            reader=self._repository_reader_for(repo_path, correlation_id),
            unavailable=descriptor_unavailable,
            partial=descriptor_partial,
            request_text=self._request_text_of(row),
            ranked_source=self._ranked_source_enabled(),
            reference_paths=self._reference_paths_of(correlation_id),
        )
        # Whether the project already declares a coverage command (20 October
        # 2026): the plan writer holds a plan that sets the command up to
        # declaring it before the tasks GuardKit measures, and needs to know
        # when the project has one already. Read from the same file and the
        # same places the stamping below reads it; unreadable is "false".
        target_repo_descriptor[coverage_declaration.DESCRIPTOR_FIELD] = (
            await self._coverage_command_declared(repo_path, branch)
        )
        if descriptor_unavailable:
            self.__dict__.setdefault("_descriptor_unavailable", {})[correlation_id] = (
                descriptor_unavailable[0]
            )
        if descriptor_partial:
            self.__dict__.setdefault("_descriptor_partial", {})[correlation_id] = list(
                descriptor_partial
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
        logger.info(
            "planning driver: run %s plan attempt (%s) for %s from spec %s",
            correlation_id,
            round_label,
            feature_id,
            spec_feature_paths,
        )

        # The project's documents, exactly as the door recorded them — the same
        # texts the spec writer was given — on every plan call. The factory
        # interface follows the target-owned documents. The receipt says
        # "sent", never that the model used the evidence correctly.
        documents, broken = self._recorded_project_documents(correlation_id)
        if broken is not None:
            await self._fail_leg(correlation_id, _FEATURE_PLAN_STAGE, broken)
            return None
        reference, broken = self._recorded_reference_material(correlation_id)
        if broken is not None:
            await self._fail_leg(correlation_id, _FEATURE_PLAN_STAGE, broken)
            return None
        factory_document = ensure_factory_context(self._deps.store, correlation_id)
        texts = context_texts(documents) + reference_context_texts(
            reference.files if reference is not None else ()
        )
        # The same decision records the spec writer was sent; the plan
        # writer's exact re-review is sent them too.
        memory_text, _memory_receipt = self._recorded_memory_decisions(correlation_id)
        if memory_text:
            texts.append(memory_text)
        if factory_document is not None:
            texts.append(factory_document.context_text())

        from forge.planning.requirement_amendments import FIELD, paths
        run_record = deps.store.get_run(correlation_id)
        amendment_scope = paths(json.loads(run_record[FIELD])) if run_record is not None and FIELD in run_record.keys() else ()
        if amendment_scope:
            target_repo_descriptor[FIELD] = list(amendment_scope)
            target_repo_descriptor["requirement_amendment_base_commit"] = run_record["start_commit"]
            target_repo_descriptor["requirement_amendment_before_sha256"] = {doc.path: doc.sha256 for doc in documents if doc.path in amendment_scope}

        async def _ask_the_plan_writer() -> Any:
            """One call to the plan writer."""
            extra: dict[str, Any] = {}
            if plan_note is not None:
                extra["validate_feedback"] = plan_note
            if texts:
                extra["context"] = texts
            # "Already done, nothing to build" (7 October 2026): asked for only
            # with the switch on. Not on the ordinary plan asked for after an
            # already-done answer that could not be used (24 October 2026).
            if self._nothing_to_build_enabled() and plan_note is None:
                extra[nothing_to_build.REQUEST_FIELD] = True
            return await deps.dispatch_feature_plan(
                plan_run_id=plan_run_id,
                correlation_id=correlation_id,
                feature_id=feature_id,
                spec_feature=spec_feature,
                spec_summary=spec_summary,
                target_repo_descriptor=target_repo_descriptor,
                spec_assumptions=spec_assumptions,
                spec_feature_paths=spec_feature_paths,
                # The coach's ground truth, the same two the spec leg sends
                # (2026-09-15): the sentence, word for word, and what the
                # repository already does for the words it uses.
                request_text=self._request_text_of(row),
                repository_facts=await self._plan_repository_facts(
                    correlation_id, repo_path, row
                ),
                **extra,
            )

        try:
            result = await _ask_the_plan_writer()
        except Exception as exc:  # noqa: BLE001 — dispatch boundary
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"008 dispatch raised {type(exc).__name__}: {exc}",
            )
            return None
        ok, reason = self._dispatch_ok(result)
        if not ok:
            await self._fail_leg(
                correlation_id, _FEATURE_PLAN_STAGE, f"008 dispatch {reason}"
            )
            return None

        role_output = self._role_output_of(result)
        files = self._plan_tree_files(role_output)
        if not files:
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                "008 returned no plan tree (invalid artifacts)",
            )
            return None

        # The plan reviewer's record, for the build card only.
        review = self._plan_review_record(role_output)
        review_refused: str | None = None
        if review is not None and review.get("decision") not in (None, "approved"):
            review_refused = "its review record says not approved"

        # "ALREADY DONE, NOTHING TO BUILD" (7 October 2026). The plan writer
        # may answer that every part of the request is already in the
        # repository, with its proof. Nothing is written: the caller ends the
        # run. A reply without the marker goes on exactly as today.
        claim, claim_problem = nothing_to_build.read_claim(role_output, files)
        if claim is not None or claim_problem is not None:
            return _PlanAttempt(
                files=files,
                slug=slug,
                traceability=review,
                nothing_to_build=_NothingToBuildAnswer(
                    claim=claim, problem=claim_problem
                ),
            )

        # The plan YAML names the committed spec .feature when the plan-writer
        # left ``feature_files:`` out: Forge committed that file one leg
        # earlier and is the party that knows it.
        files = self._with_feature_files(files, feature_id, spec_feature_paths)
        git_runner = self._git_runner_for(target_repo)
        try:
            (
                recorded_memory,
                recorded_settings,
                whose_work,
                declared_at,
            ) = self._recorded_launch(correlation_id)
            gitres = await git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=(
                    f"planning: feature plan {feature_id} for {correlation_id} "
                    "(Lane B 008)"
                ),
                memory_project=recorded_memory,
                launch_settings=recorded_settings,
                build=whose_work,
                declared_at=declared_at,
                feature_routing_id=correlation_id,
                feature_routing_required=self._deps.feature_routing_required,
                feature_routing_receipt=self._feature_routing_receipt(correlation_id),
            )
        except Exception as exc:  # noqa: BLE001 — write boundary
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"plan write raised {type(exc).__name__}: {exc}",
            )
            return None
        if gitres.status == "failed":
            await self._fail_leg(
                correlation_id,
                _FEATURE_PLAN_STAGE,
                f"plan write failed: {gitres.stderr}",
            )
            return None
        return _PlanAttempt(
            files=files,
            slug=slug,
            sha=gitres.sha,
            traceability=review,
            review_refused=review_refused,
            plan_advice=tuple(
                sorted(
                    (text for _rule, text in self._plan_advice_items(role_output)),
                    key=lambda line: not line.startswith(_MISSING_PREREQUISITE),
                )
            ),
        )

    @staticmethod
    def _with_feature_files(
        files: Mapping[str, str], feature_id: str, spec_feature_paths: Sequence[str]
    ) -> dict[str, str]:
        """The plan files, with the committed spec path(s) written into a plan
        YAML that declares no ``feature_files:``. Done on a temporary copy of
        the plan's own files; nothing of the repository is read or run."""
        from forge.planning.target_terminal_tools import declare_feature_files_if_absent

        files_out: dict[str, str] = {str(k): str(v) for k, v in files.items()}
        with tempfile.TemporaryDirectory(prefix="forge-plan-files-") as td:
            shadow = Path(td).resolve()
            for rel, content in files_out.items():
                target = (shadow / rel).resolve()
                if not str(target).startswith(str(shadow) + os.sep):
                    continue  # the runner refuses an escaping path itself
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            fill = declare_feature_files_if_absent(shadow, feature_id, spec_feature_paths)
            if fill.fired:
                files_out[fill.yaml_rel] = (shadow / fill.yaml_rel).read_text(
                    encoding="utf-8"
                )
        return files_out

    # -- the plan leg's pre-commit checks, declared (sandbox first, rule 70) --

    def _git_runner_for(self, target_repo: str) -> Any:
        """The git runner for ``org/name``: the composition's resolver when
        it set one (a sandboxed repository's sidecar runner, else the
        in-container one), otherwise the single ``git_runner`` — which is
        every repository's before this lane and for a composition with no
        sandboxes."""
        resolver = self._deps.git_runner_for_repo
        if resolver is not None:
            runner = resolver(target_repo)
            if runner is not None:
                return runner
        return self._deps.git_runner

    @staticmethod
    def _runner_takes_declared_checks(git_runner: Any) -> bool:
        """Feature-detect the declared form: a runner that says
        ``supports_declared_checks()`` is true. The in-container runner has
        no such method and keeps the closure."""
        probe = getattr(git_runner, "supports_declared_checks", None)
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 — a runner that cannot answer takes closures
            return False


    def _declare_spec_checks(
        self, correlation_id: str, feature_rel: str
    ) -> PreCommitChecks:
        """The spec leg's pre-commit, declared: the gherkin normalizer over
        the committed ``.feature`` (blocking, as the closure is)."""
        checks = [
            PreCommitCheck(
                name="normalize-feature",
                args={"feature_file": feature_rel},
                blocking=True,
            )
        ]
        logger.info(
            "planning driver: run %s — the spec leg's pre-commit checks on %s "
            "are declared to the sandbox git runner rather than run here: %s",
            correlation_id,
            feature_rel,
            ", ".join(
                f"{c.name}({'blocking' if c.blocking else 'not blocking'})"
                for c in checks
            ),
        )
        return PreCommitChecks(tuple(checks))



    async def _finish_feature_plan(
        self,
        correlation_id: str,
        attempt: "_PlanAttempt",
        *,
        feature_id: str,
        target_repo: str,
        branch: str,
        repo_path: str | None = None,
    ) -> bool:
        """The plan is on the branch: the approved row, and on to the build
        trigger."""
        deps = self._deps
        files = attempt.files
        # Keep the plan reviewer's record on the durable approved row.
        semantic_review = (
            dict(attempt.traceability) if attempt.traceability is not None else None
        )
        details: dict[str, Any] = {
            "feature_id": feature_id,
            "slug": attempt.slug,
            "plan_files": sorted(files),
            "target_repo": target_repo,
            "branch": branch,
            "sha": attempt.sha,
        }
        from forge.planning.requirement_amendments import (
            FIELD,
            SUFFIX,
            validate_proposal,
            validate_absence,
            AmendmentRefused,
        )
        from forge.planning.requirement_amendment_store import record_plan

        amendment_names = [name for name in files if name.endswith(SUFFIX)]
        run_record = deps.store.get_run(correlation_id)
        scope = (
            json.loads(run_record[FIELD])
            if run_record is not None and FIELD in run_record.keys()
            else []
        )
        if amendment_names or scope:
            expected_name = f".guardkit/features/{feature_id}{SUFFIX}"
            try:
                if (
                    (amendment_names and amendment_names != [expected_name])
                    or repo_path is None
                    or semantic_review is None
                ):
                    raise AmendmentRefused(
                        "amendment artifact/feature/review identity is missing or mismatched"
                    )
                kwargs = dict(
                    runner=self._git_runner_for(target_repo),
                    repo_path=repo_path,
                    scope=scope,
                    base=run_record["start_commit"],
                    plan=attempt.sha,
                    feature_id=feature_id,
                )
                if amendment_names:
                    proposal = await validate_proposal(
                        raw=files[expected_name], **kwargs
                    )
                else:
                    proposal = await validate_absence(**kwargs)
                record_plan(
                    deps.store._connection, correlation_id, proposal, semantic_review
                )
                details["requirement_amendment_digest"] = proposal["candidate_digest"]
                details["requirement_amendment_state"] = proposal["amendment_state"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                return await self._fail_leg(
                    correlation_id,
                    _FEATURE_PLAN_STAGE,
                    "requirement amendment refused: " + str(exc),
                )
        factory_document = ensure_factory_context(deps.store, correlation_id)
        if factory_document is not None:
            details["factory_context"] = factory_document.receipt()
        _memory_text, memory_receipt = self._recorded_memory_decisions(correlation_id)
        if memory_receipt is not None:
            details[memory_decisions.RECEIPT_KEY] = memory_receipt
        if semantic_review is not None:
            details["semantic_review"] = semantic_review
            score = semantic_review.get("criterion_score")
            details[PLAN_REVIEW_NOTE_KEY] = _PLAN_REVIEW_SCORE_CARD_LINE.format(
                score=f"{float(score):.2f}"
                if isinstance(score, (int, float)) and not isinstance(score, bool)
                else "unknown",
                verdict=semantic_review.get("coach_verdict") or "not given",
            )
        if attempt.review_refused:
            details[PLAN_REVIEW_NOTE_KEY] = _PLAN_REVIEW_REFUSED_CARD_LINE.format(
                reason=attempt.review_refused
            )
        if attempt.plan_advice:
            # Information for the approver, never a gate (step 2).
            details[PLAN_ADVICE_NOTE_KEY] = _PLAN_ADVICE_CARD_LINE.format(
                advice=" ".join(
                    line if line.rstrip().endswith((".", "!", "?")) else line.rstrip() + "."
                    for line in attempt.plan_advice
                )
            )
        documents, _broken = self._recorded_project_documents(correlation_id)
        if documents:
            # Which files, which versions, were given to the plan writer.
            details["project_documents"] = self._project_documents_receipt(documents)
        reference, _broken = self._recorded_reference_material(correlation_id)
        if reference is not None and reference.files:
            details["reference_files"] = self._project_documents_receipt(
                reference.files
            )
        unavailable_line = self._repository_unavailable_line(correlation_id)
        if unavailable_line is not None:
            # Read by the build gate (the 1 October planner fix): the card the person
            # taps to start the build says the plan was written without the
            # repository, in the same words as the spec card.
            details[PLAN_REPOSITORY_UNAVAILABLE_KEY] = unavailable_line
        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_FEATURE_PLAN_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(details),
        )
        await self._notify(
            correlation_id,
            f"Planning run {correlation_id}: machine spec + plan complete "
            f"(feature {feature_id}, branch {branch}); queueing the build.",
            level="info",
        )
        logger.info(
            "planning driver: run %s feature-plan committed (feature_id=%s); "
            "proceeding to the B3 build trigger",
            correlation_id,
            feature_id,
        )
        return True


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
        # FRG-067 (24 October 2026): minted from the run, as the plan leg did.
        feature_id = str(
            plan_details.get("feature_id") or self._mint_feature_id(correlation_id)
        )
        target_repo = str(plan_details.get("target_repo") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        plan_files = plan_details.get("plan_files") or []
        plan_run_id = f"plan-{correlation_id}"

        # FRG-063 (24 October 2026): freezing an amendment run's final plan
        # source is bookkeeping. One retry, then the problem is logged and the
        # build is queued as planned; the amendment's own fence still decides
        # whether its approved bytes are applied.
        from forge.planning.requirement_amendment_store import freeze_final_plan
        resolved = deps.planning_config.target_repo_paths.get(target_repo)
        for attempt in (1, 2):
            try:
                await freeze_final_plan(deps.store._connection, correlation_id, self._git_runner_for(target_repo), str(resolved),
                                        pass_stage=_QA_PASS_BARS_STAGE, gate_stage=_QA_FEATURE_GATE_STAGE)
                break
            except (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                sqlite3.Error,
                OSError,
            ) as exc:
                logger.warning(
                    "planning driver: run %s final requirement amendment planning "
                    "source not frozen (attempt %d of 2): %s — ADVISORY; the "
                    "build is queued as planned",
                    correlation_id,
                    attempt,
                    exc,
                )

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

        # The seed was captured at the spec commit and persisted on the durable
        # feature-spec event (it survives an idempotent re-drive where the spec
        # leg never ran this drive).
        spec_details = self._leg_event_details(correlation_id, _FEATURE_SPEC_STAGE)
        raw_seed = spec_details.get("pass_bar_seed")
        # STEP 2 (26 October 2026, census FRG-065): one rule in one place.
        # GuardKit's own task-start precondition is where a missing pass bar
        # is refused (when a project switches it on); Forge's copy here no
        # longer ends the run. A seed that is missing or unreadable is logged
        # and no pass bars are registered.
        seed_problem: str | None = None
        seed: Any = None
        if not raw_seed:
            seed_problem = (
                "the 007 reply shipped no pass-bar seed (pass-bar-seed-*.yaml)"
            )
        else:
            try:
                seed = yaml.safe_load(raw_seed)
            except yaml.YAMLError as exc:
                seed_problem = f"the captured pass-bar seed is not parseable YAML: {exc}"
            else:
                if not isinstance(seed, Mapping):
                    seed_problem = "the captured pass-bar seed is not a YAML mapping"
        if seed_problem is not None:
            return self._skip_pass_bars(correlation_id, seed_problem)

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
            if already is None:
                already = self._mission_sign_in_answer(correlation_id)
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
        # FRG-067 (24 October 2026): the feature id is minted from the run, so
        # a record that lost it is read the same way it was made.
        feature_id = str(
            plan_details.get("feature_id") or self._mint_feature_id(correlation_id)
        )
        plan_sha = str(plan_details.get("sha") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        plan_files = [str(f) for f in (plan_details.get("plan_files") or [])]
        plan_tasks: list[Any] | None = [] if not plan_sha else None
        resolved = await self._resolve_repo(
            row, correlation_id, stage_label=_QA_PASS_BARS_STAGE
        )
        if resolved is None:
            return False
        _target_repo, repo_path = resolved

        # The per-task bar ids ARE the validated plan's task ids — the SAME
        # source ``guardkit feature validate`` reads: the committed feature YAML.
        if plan_tasks is None:
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
            from forge.planning.requirement_amendment_store import skipped_qa

            amendment_qa = skipped_qa(
                deps.store._connection, correlation_id, branch, plan_sha
            )
            deps.store._record_event(
                correlation_id=correlation_id,
                stage_label=_QA_PASS_BARS_STAGE,
                status="approved",
                actor_identity="planning-driver",
                details_json=json.dumps(
                    {
                        "feature_id": feature_id,
                        "skipped": True,
                        "reason_code": "no_tasks" if plan_sha else "plan_record_incomplete",
                        "reason": (
                            "The validated plan has no tasks requiring per-task pass bars."
                            if plan_sha
                            else "The plan record names no plan commit, so no pass bars were registered."
                        ),
                        **({"requirement_amendment_qa": amendment_qa}
                           if amendment_qa is not None else {}),
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

        git_runner = self._git_runner_for(_target_repo)
        try:
            from forge.planning.requirement_amendment_store import begin_qa, finish_qa
            amendment_qa = await begin_qa(deps.store._connection, correlation_id, git_runner, repo_path, branch, bars)
            (
                recorded_memory,
                recorded_settings,
                whose_work,
                declared_at,
            ) = self._recorded_launch(correlation_id)
            gitres = await git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=bars,
                message=(
                    f"planning: register {len(bars)} per-task QA pass bar(s) for "
                    f"{correlation_id} ({feature_id}, Lane B seed fan-out)"
                ),
                memory_project=recorded_memory,
                launch_settings=recorded_settings,
                build=whose_work,
                declared_at=declared_at,
                feature_routing_id=correlation_id,
                feature_routing_required=self._deps.feature_routing_required,
                feature_routing_receipt=self._feature_routing_receipt(correlation_id),
            )
            if gitres.status != "failed":
                amendment_qa = await finish_qa(amendment_qa, git_runner, repo_path, gitres.sha)
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
                f"pass-bar write failed: {gitres.stderr}",
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_PASS_BARS_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    **({"requirement_amendment_qa": amendment_qa} if amendment_qa is not None else {}),
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

        WHAT THAT MEANS FOR THE WORDS ON THESE CARDS, said here because it has
        already been got wrong once (2026-09-05). ``planning/escalation.py``
        refuses anything that is not durably PAUSED, so
        ``PlanningConfig.escalated_wait_seconds`` has NO effect on any card
        this door sends. Since 24 October 2026 (FRG-035) the door itself posts
        the same card, with the same ``request_id``, once more when the first
        ``originator_wait_seconds`` window closes, and waits one more window of
        the same length; then the leg ends and the owner is told it stopped.
        Every "No answer within…" sentence on these cards says exactly that.
        ``tests/forge/planning/test_driver_spec_digest_door.py`` holds them
        together.

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
        # FRG-035 (24 October 2026): at the first deadline the SAME card, with
        # the same request_id, is posted once more and the door waits one more
        # window. An answer to either copy is taken; cancellation still ends it.
        posted_again = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and published and not posted_again:
                posted_again = True
                deadline = time.monotonic() + float(wait_seconds)
                remaining = float(wait_seconds)
                logger.warning(
                    "planning driver: run %s %s window (%ds) closed with no "
                    "answer — posting the same card %s once more and waiting "
                    "one more window",
                    correlation_id,
                    log_noun,
                    wait_seconds,
                    request_id,
                )
                try:
                    await deps.approval_publisher.publish_request(envelope)
                except Exception:  # noqa: BLE001 — the first card is still live
                    logger.exception(
                        "planning driver: %s card could not be posted again for "
                        "%s (request_id=%s); the first card still answers it",
                        log_noun,
                        correlation_id,
                        request_id,
                    )
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
            if expected_approver and not is_accepted_approver(
                response.decided_by, expected_approver, deps.additional_approvers
            ):
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
                    f"Planning run {correlation_id}: "
                    f"{_person_words(answer.decided_by_name)} "
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
            # ``_approver`` is the chat id the door routes by; the line it
            # writes never prints one, so it is not passed on.
            open_message=lambda _approver, wait: self._auth_door_open_message(
                correlation_id, wait_seconds=wait
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
                f"No answer within {self._plain_wait(wait_seconds)}: the same "
                "card is sent once more, and with no answer after another "
                f"{self._plain_wait(wait_seconds)} the run stops, the same as "
                "Reject."
            ),
        }
        feature = str(seed.get("feature_slug") or "").strip()
        if feature:
            card["feature"] = feature
        return card

    def _auth_door_open_message(
        self, correlation_id: str, *, wait_seconds: int
    ) -> str:
        """The plain-language ping that says the run is WAITING, not broken.

        It takes no approver: the id this door routes by is never printed. The
        line used to open "U03QR8WKT29 has a card to decide", which named
        nobody (2026-09-05 defect, third door). It is the person reading it.
        """
        # Only an id is held here, and an id is never printed.
        who = _person_words(None)
        subject, verb = ("You", "have") if who == "you" else (who, "has")
        return (
            f"Planning run {correlation_id}: the spec checker flagged this "
            "feature as sitting behind a sign-in, which is often a false alarm "
            "— nothing has failed. "
            f"{subject} {verb} a card to decide: confirm there is no sign-in "
            "and the quality checklist registers as authless and the build "
            "carries on; reject and the run stops so the checklist is "
            "registered by hand. No "
            f"answer within {self._plain_wait(wait_seconds)} sends the same "
            "card once more, and silence after that stops the run too."
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
          (a) neither the digest nor a machine criterion yields a GET
              ``{method,path}`` → honest ``skipped`` leg event and CONTINUE to
              the build (non-endpoint features are legitimate). The record
              says which: the spec named an address with another method
              (``unsupported_method``, with that address), named a GET
              address a check cannot send as written, such as one with a
              query string (``unsupported_address``), or named none
              (``no_endpoint_named``). A GET address with a ``{placeholder}``
              in it is skipped too (``placeholder_in_address``): the filled
              check would send the placeholder as written and could never
              pass (FEAT-E613, 24 August 2026);
          (b) the target repo carries no ``qa/gates/feature_behaviour_gate.py``
              template or no ``qa/gates/registry.yaml`` on the branch → the same
              honest skip (the repo has not adopted the F4 gate surface);
          (c) template + derivable but the fill / validate / commit fails → LOUD
              :meth:`_fail_leg` (same posture as the bars leg);
          (d) an auth-flagged seed only reaches this leg CONFIRMED authless —
              the bars leg's confirmation door is the single place that
              question is asked, and a rejected / unanswered door fails the
              bars leg first — so no auth handling is re-implemented here.

        Every skip record carries ``reason_code``, the ``address`` the spec
        named (or None) and the ``feature_id`` beside the free-text
        ``reason``, and is logged as a warning: both merge cards read it
        (:func:`forge.pipeline.merge_offer.read_after_deploy_check_skip`) and
        say in one sentence that planning registered no after-deploy check.

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

        # The feature id is read BEFORE any skip, so every skip record names
        # the feature it is about (4 October 2026).
        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        feature_id = str(
            plan_details.get("feature_id") or self._mint_feature_id(correlation_id)
        )

        # The digest's optional endpoint field first (the spec author stated it
        # outright); the criterion-prose regex stays as the fallback so every
        # feature that registers a gate today still registers one.
        endpoint = self._feature_gate_endpoint_from_digest(
            spec_details.get("digest")
        ) or self._derive_feature_gate_endpoint(criteria)
        if endpoint is None:
            named = self._feature_gate_address_named(
                spec_details.get("digest"), criteria
            )
            if named is not None and named["method"] != "GET":
                return self._skip_feature_gate(
                    correlation_id,
                    f"the spec names {named['method']} {named['path']}; only a "
                    "GET address can be checked automatically — no gate "
                    "registered",
                    reason_code=SKIP_UNSUPPORTED_METHOD,
                    feature_id=feature_id,
                    address=named,
                )
            if named is not None:
                return self._skip_feature_gate(
                    correlation_id,
                    f"the spec names GET {named['path']}, which a check cannot "
                    "send as written — no gate registered",
                    reason_code=SKIP_UNSUPPORTED_ADDRESS,
                    feature_id=feature_id,
                    address=named,
                )
            return self._skip_feature_gate(
                correlation_id,
                "the spec names no address to check — no gate registered",
                reason_code=SKIP_NO_ENDPOINT_NAMED,
                feature_id=feature_id,
                address=None,
            )
        if placeholders_in(endpoint["path"]) or "}" in endpoint["path"]:
            return self._skip_feature_gate(
                correlation_id,
                f"the address GET {endpoint['path']} has a placeholder the "
                "check would send as written, so it could never pass — no "
                "gate registered",
                reason_code=SKIP_PLACEHOLDER_IN_ADDRESS,
                feature_id=feature_id,
                address=endpoint,
            )

        plan_sha = str(plan_details.get("sha") or "")
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        if not plan_sha:
            # FRG-067 (24 October 2026): bookkeeping, never the end of the run.
            return self._skip_feature_gate(
                correlation_id,
                "the plan record names no plan commit — no gate registered",
                reason_code="plan_record_incomplete",
                feature_id=feature_id,
                address=endpoint,
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
                reason_code=SKIP_NO_PASS_BARS,
                feature_id=feature_id,
                address=endpoint,
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
                reason_code=SKIP_NO_TEMPLATE,
                feature_id=feature_id,
                address=endpoint,
            )
        registry_raw = await deps.git_runner.read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=_GATE_REGISTRY_REL
        )
        if not registry_raw:
            return self._skip_feature_gate(
                correlation_id,
                f"the target repo carries no {_GATE_REGISTRY_REL} on the branch "
                "— the F4 gate surface is not adopted; no gate registered",
                reason_code=SKIP_NO_REGISTRY,
                feature_id=feature_id,
                address=endpoint,
            )

        # Derive the slug + snake filename deterministically from the seed.
        raw_slug = str(seed.get("feature_slug") or "").strip()
        slug = self._sanitise_slug(raw_slug) or feature_id.lower()
        slug_snake = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
        # STEP 2 (26 October 2026, census FRG-069): registering this optional
        # after-deploy check is the factory's own bookkeeping. Anything that
        # stops it below is an honest skip with its reason, which both merge
        # cards say; it never ends the run.
        if not slug_snake:
            return self._skip_feature_gate(
                correlation_id,
                f"could not derive a gate filename from feature_slug={raw_slug!r} "
                f"/ feature_id={feature_id} — no gate registered",
                reason_code=SKIP_REGISTRATION_FAILED,
                feature_id=feature_id,
                address=endpoint,
            )
        gate_rel = f"qa/gates/{slug_snake}_gate.py"

        # (c) Fill the template + append the registry entry. A drift in the
        # template's fillable literals, or a malformed registry, is a skip.
        try:
            filled_gate = self._fill_feature_gate(template, slug, endpoint)
            new_registry = self._append_gate_registry_entry(
                registry_raw,
                gate_id=slug,
                gate_path=gate_rel,
                pass_bar_ref=pass_bar_ref,
            )
        except _FeatureGateFillError as exc:
            return self._skip_feature_gate(
                correlation_id,
                f"{exc} — no gate registered",
                reason_code=SKIP_REGISTRATION_FAILED,
                feature_id=feature_id,
                address=endpoint,
            )

        git_runner = self._git_runner_for(_target_repo)
        files = {gate_rel: filled_gate, _GATE_REGISTRY_REL: new_registry}
        try:
            from forge.planning.requirement_amendment_store import begin_qa, finish_qa
            amendment_qa = await begin_qa(deps.store._connection, correlation_id, git_runner, repo_path, branch, files)
            (
                recorded_memory,
                recorded_settings,
                whose_work,
                declared_at,
            ) = self._recorded_launch(correlation_id)
            gitres = await git_runner.prepare_branch_and_write_tree(
                repo_path=repo_path,
                branch=branch,
                files=files,
                message=(
                    f"planning: register the per-feature live gate for "
                    f"{correlation_id} ({feature_id}, GET {endpoint['path']} — "
                    "F2 seed derivation)"
                ),
                memory_project=recorded_memory,
                launch_settings=recorded_settings,
                build=whose_work,
                declared_at=declared_at,
                feature_routing_id=correlation_id,
                feature_routing_required=self._deps.feature_routing_required,
                feature_routing_receipt=self._feature_routing_receipt(correlation_id),
            )
            if gitres.status != "failed":
                amendment_qa = await finish_qa(amendment_qa, git_runner, repo_path, gitres.sha)
        except Exception as exc:  # noqa: BLE001 — write boundary
            return self._skip_feature_gate(
                correlation_id,
                f"feature-gate write raised {type(exc).__name__}: {exc} — no "
                "gate registered",
                reason_code=SKIP_REGISTRATION_FAILED,
                feature_id=feature_id,
                address=endpoint,
            )
        if gitres.status == "failed":
            return self._skip_feature_gate(
                correlation_id,
                f"feature-gate write failed: {gitres.stderr} — no gate registered",
                reason_code=SKIP_REGISTRATION_FAILED,
                feature_id=feature_id,
                address=endpoint,
            )

        deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_FEATURE_GATE_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    **({"requirement_amendment_qa": amendment_qa} if amendment_qa is not None else {}),
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

    def _skip_pass_bars(self, correlation_id: str, reason: str) -> bool:
        """Record a skipped ``qa-pass-bars`` leg and CONTINUE (step 2, 26
        October 2026, census FRG-065): no pass bars are registered, the reason
        is logged, and the run goes on to the feature-gate leg, which skips
        its gate for want of a pass bar. GuardKit's own task-start
        precondition is where a missing pass bar is refused, where enforced."""
        logger.warning(
            "planning driver: run %s — %s — ADVISORY; no per-task pass bars are "
            "registered and the run carries on",
            correlation_id,
            reason,
        )
        from forge.planning.requirement_amendment_store import skipped_qa

        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        branch = str(plan_details.get("branch") or f"planning/{correlation_id}")
        plan_sha = str(plan_details.get("sha") or "")
        amendment_qa = skipped_qa(
            self._deps.store._connection, correlation_id, branch, plan_sha
        )
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_PASS_BARS_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    "skipped": True,
                    "reason_code": "no_usable_seed",
                    "reason": reason,
                    **({"requirement_amendment_qa": amendment_qa}
                       if amendment_qa is not None else {}),
                    "bar_files": [],
                    "registered_at_sha": plan_sha,
                    "branch": branch,
                }
            ),
        )
        return True

    def _skip_feature_gate(
        self,
        correlation_id: str,
        reason: str,
        *,
        reason_code: str,
        feature_id: str,
        address: Mapping[str, str] | None,
    ) -> bool:
        """Record an HONEST skipped ``qa-feature-gate`` leg event and CONTINUE.

        A non-endpoint feature (or a repo that has not adopted the qa/gates/
        surface) is a legitimate no-gate outcome, not a failure: record the
        durable label (so a re-drive is a clean no-op) with a ``skipped`` detail
        and return True so the caller proceeds to the B3 build trigger. Zero
        target-repo writes.

        The record carries the free-text ``reason`` as before, plus (since 4
        October 2026) a ``reason_code``, the ``address`` the spec named (or
        None) and the ``feature_id``, which is what both merge cards read to
        say why no after-deploy check was registered. Older records carry only
        ``skipped`` and ``reason`` and stay readable. Logged as a WARNING: a
        feature reaching its merge card with no after-deploy check of planning's
        making is worth seeing in the log, though it never stops the run.
        """
        logger.warning(
            "planning driver: run %s feature gate skipped (%s, feature %s, "
            "address %s) — %s",
            correlation_id,
            reason_code,
            feature_id or "not recorded",
            (
                f"{address.get('method')} {address.get('path')}"
                if address
                else "none named"
            ),
            reason,
        )
        from forge.planning.requirement_amendment_store import skipped_qa
        plan_details = self._leg_event_details(correlation_id, _FEATURE_PLAN_STAGE)
        bars_details = self._leg_event_details(correlation_id, _QA_PASS_BARS_STAGE)
        amendment_qa = skipped_qa(self._deps.store._connection, correlation_id,
            str(plan_details.get("branch") or ""), str(bars_details.get("sha") or plan_details.get("sha") or ""))
        self._deps.store._record_event(
            correlation_id=correlation_id,
            stage_label=_QA_FEATURE_GATE_STAGE,
            status="approved",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {
                    **({"requirement_amendment_qa": amendment_qa} if amendment_qa is not None else {}),
                    "skipped": True,
                    "reason": reason,
                    "reason_code": reason_code,
                    "feature_id": feature_id or None,
                    "address": dict(address) if address else None,
                }
            ),
        )
        return True

    @staticmethod
    def _feature_gate_address_named(
        digest_text: Any, criteria: Any
    ) -> dict[str, str] | None:
        """The address the spec names, whatever its method, or None.

        Read only to say WHY no gate was registered, never to register one:
        the digest's optional ``endpoint`` field first (any method, a rooted
        path), then the first machine criterion matching the criterion
        grammar with any verb. The method comes back upper-cased.
        """
        try:
            obj = yaml.safe_load(str(digest_text or ""))
        except yaml.YAMLError:
            obj = None
        endpoint = obj.get("endpoint") if isinstance(obj, Mapping) else None
        if isinstance(endpoint, Mapping):
            method = str(endpoint.get("method") or "").strip().upper()
            path = str(endpoint.get("path") or "").strip()
            if method and path.startswith("/"):
                return {"method": method, "path": path}
        if isinstance(criteria, list):
            for crit in criteria:
                if not isinstance(crit, Mapping) or str(crit.get("class")) != "machine":
                    continue
                named = PlanningRunDriver._address_in_criterion(
                    str(crit.get("text") or "")
                )
                if named is not None:
                    return named
        return None

    @staticmethod
    def _address_in_criterion(text: str) -> dict[str, str] | None:
        """The method and the WHOLE address one criterion names, or None.

        The whole address up to the next space, less closing sentence
        punctuation, so ``/users/{user_id}.`` is ``/users/{user_id}`` and
        ``/v1.0/users`` is ``/v1.0/users`` — never a partial match.
        """
        match = _FEATURE_GATE_ENDPOINT_RE.search(text)
        if match is None:
            return None
        path = match.group("path").rstrip(_FEATURE_GATE_PATH_TRAILING)
        if len(path) < 2:
            return None
        return {"method": match.group("method"), "path": path}

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

        The path is the whole address as written, and it must be one a check
        can send as written (:data:`_FEATURE_GATE_PLAIN_PATH_RE`); a partial
        match is never returned as if it were the endpoint. A placeholder is
        returned whole, and the leg then skips it by name.
        """
        named = PlanningRunDriver._address_in_criterion(text)
        if named is None or named["method"] != "GET":
            return None
        if _FEATURE_GATE_PLAIN_PATH_RE.fullmatch(named["path"]) is None:
            return None
        return {"method": "GET", "path": named["path"]}

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
            publish_terminal=self._deps.publish_planning_failed,
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
        Only the ``.feature`` is needed (without it there is no spec); the
        assumptions and summary files are committed when they came, the first
        of each when several did (the cull, 27 October 2026). The committed
        paths are the canonical ``features/<slug>/`` files (``slug`` already
        resolved via ``_slug_of``). ``None`` when no ``.feature`` came.

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
        if not by_suffix[_SPEC_FEATURE_SUFFIX]:
            return None
        base = f"features/{slug}"
        return {
            f"{base}/{slug}{suffix}": found[0]
            for suffix, found in by_suffix.items()
            if found
        }

    @staticmethod
    def _spec_triple_files(
        role_output: Mapping[str, Any], slug: str
    ) -> dict[str, str] | None:
        """Project the committed spec contract from the 007 role_output.

        Shape resolution (belt-and-braces):
          1. An explicit ``files`` mapping (specialist-authored repo-relative
             paths — the legacy/alternate shape), committed verbatim.
          2. The DEPLOYED native suffix-keyed artifact map (a ``*.feature``,
             with ``*_assumptions.yaml`` / ``*_summary.md`` when they came, plus
             tolerated extras), projected onto ``features/<slug>/``.
          3. Field-based ``feature`` (and optional ``assumptions`` /
             ``summary``) fallback.
        ``None`` when no shape yields a ``.feature`` (the invalid-artifacts
        fail path: there is no spec).
        """
        files = role_output.get("files")
        if isinstance(files, Mapping) and files:
            return {str(k): str(v) for k, v in files.items()}
        native = PlanningRunDriver._project_native_spec_triple(role_output, slug)
        if native is not None:
            return native
        feature = role_output.get("feature")
        if not feature:
            return None
        base = f"features/{slug}"
        files = {f"{base}/{slug}.feature": str(feature)}
        if role_output.get("assumptions"):
            files[f"{base}/{slug}_assumptions.yaml"] = str(role_output["assumptions"])
        if role_output.get("summary"):
            files[f"{base}/{slug}_summary.md"] = str(role_output["summary"])
        return files

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
    def _plan_review_record(role_output: Mapping[str, Any]) -> dict[str, Any] | None:
        """The plan reviewer's ``semantic_review.json`` as the planning seat
        wrote it, for the build card; ``None`` when there is none or it is
        not a JSON object. Nothing is checked or refused on it (the cull,
        27 October 2026)."""
        raw = role_output.get("semantic_review.json")
        if isinstance(raw, str):
            try:
                parsed: Any = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return None
        else:
            parsed = raw
        return dict(parsed) if isinstance(parsed, Mapping) else None


    @staticmethod
    def _plan_advice_items(role_output: Mapping[str, Any]) -> list[tuple[str, str]]:
        """The specialist's ``plan_advice`` as ``(rule, text)`` pairs, or ``[]``.

        Read from the reply's ``validation.json``; anything unreadable is
        simply no advice.
        """
        raw = role_output.get("validation.json")
        data: Any = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return []
        advice = data.get("plan_advice") if isinstance(data, Mapping) else None
        if not isinstance(advice, list):
            return []
        items: list[tuple[str, str]] = []
        for item in advice:
            if isinstance(item, Mapping):
                rule = str(item.get("rule") or "").strip()
                text = str(item.get("text") or "").strip()
                if text:
                    items.append((rule, text))
        return items


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
    def _capture_coherence_warning(
        role_output: Mapping[str, Any], correlation_id: str
    ) -> dict[str, Any] | None:
        """The possible contradiction the spec writer recorded, or ``None``.

        Reads ``coherence_warning.json`` from the reply (a tolerated extra,
        never committed). ``None`` when there is no file, when every pair was
        dropped because the spec changed after the check, or when the file
        cannot be read: a bad file is logged and never fails the leg.
        """
        raw: Any = None
        for name, content in role_output.items():
            if str(name).rsplit("/", 1)[-1] == _COHERENCE_WARNING_FILE:
                raw = content
                break
        if raw is None:
            return None
        try:
            record = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(record, Mapping):
                raise ValueError("not a JSON object")
            entries = record.get("pairs") or []
            if not isinstance(entries, list):
                raise ValueError("'pairs' is not a list")
            pairs: list[dict[str, str]] = []
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise ValueError("a pair is not an object")
                first = str(entry.get("first") or "").strip()
                second = str(entry.get("second") or "").strip()
                if not first or not second:
                    raise ValueError("a pair is missing a side")
                pairs.append(
                    {
                        "first": first,
                        "second": second,
                        "why": str(entry.get("why") or "").strip(),
                    }
                )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "planning driver: run %s — the spec writer's %s could not be "
                "read (%s); the card shows no possible contradiction",
                correlation_id,
                _COHERENCE_WARNING_FILE,
                exc,
            )
            return None
        if not pairs:
            return None
        # The total before the file's own 3-pair cap. A value that is not a
        # whole number is ignored and the pairs in the file are counted.
        raw_count = record.get("pair_count")
        pair_count = (
            raw_count
            if isinstance(raw_count, int) and not isinstance(raw_count, bool)
            else None
        )
        return {
            "pairs": pairs,
            "pair_count": max(len(pairs), pair_count or 0),
            "spec_changed_after_check": bool(record.get("spec_changed_after_check")),
            "possible_contradiction": _possible_contradiction_text(pairs, pair_count),
        }


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
    def _read_architecture_rules(
        repo_path: str,
        *,
        reader: Any = None,
        unavailable: list[str] | None = None,
        partial: list[str] | None = None,
    ) -> dict[str, Any] | None:
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

        For a repository read through its sandbox helper (``reader`` is not a
        local checkout reader; the 1 October planner fix), the file is read with the
        helper's ``/code/read-file`` on the factory's own clone. A helper that
        cannot be reached is not "no rules file": its reason is appended to
        ``unavailable`` so the plan-writer is told.
        """

        def _clip(text: str) -> str:
            text = " ".join(text.split())
            if len(text) <= _MAX_ARCHITECTURE_RULE_CHARS:
                return text
            return text[: _MAX_ARCHITECTURE_RULE_CHARS - 1].rstrip() + "\u2026"

        path: Any = Path(repo_path) / _ARCHITECTURE_RULES_REL
        try:
            # Through the reader whenever it reads something other than this
            # checkout's working tree: the helper, or (7 October 2026) any
            # reader pinned to the run's starting commit, so the rules are the
            # commit's and a link is followed inside the commit only.
            if reader is not None and _reads_beyond_the_working_tree(reader):
                path = f"{_ARCHITECTURE_RULES_REL} ({getattr(reader, 'where', 'the helper')})"
                try:
                    if _ARCHITECTURE_RULES_REL not in set(reader.list_files()):
                        return None  # the repository keeps no rules file
                    text = reader.read_text(_ARCHITECTURE_RULES_REL)
                except RepositoryUnreadable as exc:
                    logger.warning(
                        "target_repo_descriptor: could not read the target "
                        "repo's architecture rules (%s); planning without them, "
                        "and the plan-writer is told so",
                        exc,
                    )
                    if unavailable is not None:
                        unavailable.append(str(exc))
                    return None
                if text is None:
                    # Not there is not the same as there and refused: a
                    # tracked rules file the helper would not serve is said.
                    why = (getattr(reader, "refused", None) or {}).get(
                        _ARCHITECTURE_RULES_REL, "it was not served"
                    )
                    if partial is not None:
                        partial.append(
                            f"the architecture rules file `{_ARCHITECTURE_RULES_REL}` "
                            f"could not be read ({why})"
                        )
                    return None
                data = yaml.safe_load(text)
            else:
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
    def _read_repository_inventory(
        repo_path: str,
        *,
        reader: Any = None,
        unavailable: list[str] | None = None,
        reference_paths: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        """Read what the target repository already contains, for the plan seat.

        The plan-writer is handed the specification's words and a small
        description of the target repository, and it cannot open files. So when
        a sentence says "add an endpoint that counts users per day", the seat
        has no way of knowing that the users module, its model, its data access
        and its router have existed since the repository was born — and it
        writes a plan whose first two waves build them again. That happened on
        2026-09-11 (FEAT-CDFD), and the invented field name in that plan,
        ``creation_timestamp``, was the specification's English rather than the
        repository's actual column, ``created_at`` — exactly what a writer who
        cannot see the code produces.

        This is the same cure as the architecture rules one level along: one
        optional field on the descriptor, and a repository that yields nothing
        plans byte for byte as it does today.

        What travels, and why it is the TRACKED files:

        * ``git ls-files`` is the repository's OWN statement of what belongs to
          it, so nothing here has to guess at virtual environments, caches,
          build output or a stray database file — none of those are tracked.
        * The factory's own paperwork is dropped
          (:data:`_REPO_INVENTORY_SKIP_PREFIXES`): it is records ABOUT the
          repository rather than the repository's code, and on api_test it is
          400 of 695 files.
        * At or under :data:`_MAX_REPO_INVENTORY_FILES` the paths themselves
          travel. Over it, one entry per directory with a count travels instead,
          and a flag says so.
        * Everything is sorted, so two runs on the same tree produce the same
          bytes.

        WHICH COPY (1 October 2026, the 1 October planner fix): ``reader`` is the
        planner's repository reader — for a repository with a sandbox, the
        helper's read-only ``/code/list-files`` on the factory's own clone;
        otherwise the checkout at ``repo_path``. In the containerised
        coordinator that checkout does not exist, and this used to answer
        ``None`` with a log line nobody read, so the plan-writer planned
        without the inventory and was never told.

        Anything that goes wrong — no path, no git, not a git repository, a
        timeout, a non-zero exit, a helper that cannot be reached — is
        ``None`` and one plain WARNING line; the reason is also appended to
        ``unavailable`` when the caller passes it, and the descriptor then
        tells the plan-writer the repository could not be read. An inventory
        must never be able to stop a planning run, which is why the whole
        reader sits inside one try/except.

        REFERENCE MATERIAL (17 October 2026, prototype-to-factory design 2.2).
        Files under the project's declared reference folders
        (``reference_paths``) are not product code: they are left out of the
        list and of the :data:`_MAX_REPO_INVENTORY_FILES` count, and each
        folder is said in one line instead (``reference_material``), so a few
        hundred reference files cannot turn a small product's list into
        folder counts. Without reference folders nothing changes.
        """
        try:
            reader = reader or LocalCheckoutReader(
                repo_path, timeout_s=_REPO_INVENTORY_GIT_TIMEOUT_SECONDS
            )
            try:
                tracked = reader.list_files()
            except RepositoryUnreadable as exc:
                # Said, not swallowed (the 1 October planner fix, 1 October 2026):
                # the reason goes back to the descriptor, which tells the
                # plan-writer the inventory is missing and why.
                logger.warning(
                    "target_repo_descriptor: could not list the tracked files "
                    "of %s (%s); planning without the repository inventory, "
                    "and the plan-writer is told so",
                    getattr(reader, "where", repo_path),
                    exc,
                )
                if unavailable is not None:
                    unavailable.append(str(exc))
                return None
            path = getattr(reader, "where", repo_path)
            cut = getattr(reader, "listing_cut", None)
            if isinstance(cut, str) and cut:
                # A cut listing sent as the inventory would read as the whole
                # repository; it is said instead.
                logger.warning(
                    "target_repo_descriptor: %s (%s); planning without the "
                    "repository inventory, and the plan-writer is told so",
                    cut,
                    path,
                )
                if unavailable is not None:
                    unavailable.append(cut)
                return None
            files = sorted(
                entry
                for entry in tracked
                if not entry.startswith(_REPO_INVENTORY_SKIP_PREFIXES)
            )
            reference_lines: list[str] = []
            if reference_paths:
                for folder in reference_paths:
                    count = sum(1 for entry in files if entry.startswith(folder))
                    reference_lines.append(
                        f"`{folder}` — {count} files of reference material (not "
                        f"product code); see the bound index"
                    )
                files = [f for f in files if not is_reference_path(f, reference_paths)]
            if not files and not reference_lines:
                logger.warning(
                    "target_repo_descriptor: %s tracks no files outside the "
                    "factory's own paperwork; planning without the repository "
                    "inventory",
                    path,
                )
                return None

            inventory: dict[str, Any] = {
                "gathered_from": (
                    "the repository's own tracked files (git ls-files), with "
                    "the factory's build and specification records left out"
                ),
                "file_count": len(files),
                "status": "complete",
                "covered_namespaces": [""],
                "excluded_prefixes": [
                    *_REPO_INVENTORY_SKIP_PREFIXES,
                    *reference_paths,
                ],
                "base_identity": (
                    {"status": "bound", "commit": reader.commit}
                    if getattr(reader, "commit", None)
                    else {"status": "unavailable"}
                ),
            }
            if reference_lines:
                inventory["reference_material"] = reference_lines
            if len(files) <= _MAX_REPO_INVENTORY_FILES:
                inventory["files"] = files
                return inventory

            counts: dict[str, int] = {}
            for entry in files:
                directory = entry.rsplit("/", 1)[0] if "/" in entry else "."
                counts[directory] = counts.get(directory, 0) + 1
            inventory["directories"] = [
                {"path": directory, "file_count": counts[directory]}
                for directory in sorted(counts)
            ]
            # Said out loud, because a seat reading a list of directories must
            # know it is reading the repository's shape and not its file list.
            inventory["directories_only"] = True
            inventory["status"] = "summarized"
            return inventory
        except Exception as exc:  # noqa: BLE001 — never fail a plan over this
            logger.warning(
                "target_repo_descriptor: could not list the tracked files of "
                "%s (%s); planning without the repository inventory",
                repo_path,
                exc,
            )
            if unavailable is not None:
                unavailable.append(
                    f"listing the repository's files failed ({type(exc).__name__})"
                )
            return None

    @staticmethod
    def _where_the_specs_words_already_appear(
        repo_path: str,
        spec_feature: str,
        *,
        reader: Any = None,
        unavailable: list[str] | None = None,
        partial: list[str] | None = None,
        request_text: str = "",
        evidence_chars: int | Callable[[], int] = MAX_EVIDENCE_CHARS,
        test_roots: Sequence[str] = (),
        reference_paths: Sequence[str] = (),
    ) -> list[dict[str, Any]] | None:
        """Where this feature's own words already occur in the repository.

        WHY (2026-09-13, FEAT-19C4). The plan-writer is given the repository's
        file NAMES and cannot open any of them. So it knew
        ``src/users/router.py`` existed and could not know that
        ``/users/count-today`` was already defined inside it — and wrote a plan
        whose first task was "**create** the /users/count-today endpoint" for an
        endpoint that had been serving for weeks. The same shape as FEAT-CDFD
        before the inventory existed, one level down: names were not enough.

        This is the smallest thing that answers it, and deliberately not the
        exploration pass: no new stage, no seat reading files, no language's
        grammar. The specification's own distinctive words — route paths, and
        identifiers long enough to be this repository's name for something — are
        looked for in the tracked files with ``git grep``. A word that is
        already there comes back with the places it is, so the writer can plan
        to CHANGE what exists rather than build it again.

        Stack-agnostic by construction: ``git grep`` for a literal string knows
        nothing about Python, TypeScript or Go, and the word pattern matches
        kebab, snake and camel alike. Both caps are small — this is a pointer,
        not a search result page.

        Returns ``None`` when nothing can be looked for or nothing is found, so
        the descriptor simply has no such key and planning is byte for byte what
        it is today. Never raises: like the inventory beside it, this must never
        be able to stop a planning run.

        Read through ``reader`` (1 October 2026, the 1 October planner fix): the
        helper's ``/code/search`` for a sandboxed repository, ``git grep`` on
        the checkout otherwise. A repository that cannot be read is ``None``
        with its reason appended to ``unavailable``, never a quiet "not found".

        WHAT WAS READ IS KEPT (the 1 October planner fix, review round 1). A word
        whose search came back incomplete (the helper's caps) keeps the
        places it found and is named in ``partial`` in plain words; it is not
        "the repository could not be read". A helper that stops answering
        part-way keeps every word already looked up.

        THE CODE ITSELF, RANKED BY THE REQUEST (6 October 2026, planning
        improvements item 1). Five places per word, ordered by kind of file
        and then by path, never reached the delete route a request asked for:
        comments and messages that mention the path sort first, and the route
        is written apart from its prefix. So:

        * the request's own words are looked for first (``request_text``),
          then the specification's, still at most
          :data:`_MAX_SPEC_WORDS_LOOKED_FOR`;
        * a route's last segment is also looked for with its slash
          (``/{user_id}``), which is how a route reads where its prefix is
          declared elsewhere;
        * each hit's file is read once and the window from 3 lines before to
          12 after the hit (starting higher, at the first line of the block
          the hit sits in, when that block begins at most 24 lines above it)
          is scored by how many of the request's words start a word inside
          it. Each entry keeps ``words`` and ``already_in``
          exactly as before and gains ``evidence``: its windows,
          ``{"path", "first_line", "last_line", "score", "text"}`` with the
          lines numbered in ``text``. ``more_hits`` counts the hits not
          inside a window shown, so a cut list never reads as whole, and
          ``evidence_not_read`` says why a file was refused.

        WHICH WINDOWS (6 October 2026, evidence coverage). The windows are
        chosen across all the words at once, not 3 per word: the request's
        own words before the specification's, the word found in fewest
        places first, one window per file before a second in any file, and
        as many as ``evidence_chars`` characters of window text hold (what
        the set candidates leave of the budget; at most
        :data:`MAX_EVIDENCE_WINDOWS`). See
        :func:`~forge.planning.code_evidence.choose_windows`. The factory's
        own pass bars are never a place. ``evidence_chars`` may be a
        callable: it is called once, after every file the windows need has
        been read and before they are chosen, so the descriptor's other
        searches can run after this one's reads and still set its share.

        WHICH WORDS (6 October 2026, evidence coverage). A plural and its
        singular are one word, looked for as the shorter (``ETag`` finds
        ``ETags``). Up to :data:`_MAX_SPEC_WORDS_TRIED` words are tried to
        find :data:`_MAX_SPEC_WORDS_LOOKED_FOR` the repository holds.

        A TEST FOR EACH SCENARIO (8 October 2026). Each approved scenario
        that no window shown is a test of is given one from ``test_roots``
        (the project's declared test folders), on strong evidence only, in
        place of documentation windows, so the planner is shown a test it
        can cite for each scenario (on the deactivate request it was shown
        the error scenarios' tests and not the success one's, and could not
        answer "already done"). A test window must name what the request
        names: its own words' spellings with punctuation in them. See
        :func:`~forge.planning.code_evidence.choose_windows`.

        A SEARCH THAT STOPS PART-WAY (6 October 2026) keeps every word
        already looked up and says so in ``partial``: it is a part read,
        not an unreadable repository. Only a repository that answered no
        search at all is ``unavailable``.

        REFERENCE MATERIAL (17 October 2026). Windows from the project's
        declared reference folders (``reference_paths``) are kept, labelled
        as reference material, and at most
        :data:`~forge.planning.code_evidence.MAX_REFERENCE_WINDOWS` of them
        are shown; see
        :func:`~forge.planning.code_evidence.choose_windows`.

        THE REQUEST'S QUOTED EXAMPLES (17 October 2026, FEAT-15DD). A quoted
        phrase in the request that holds a number ("3 notes") is looked for
        in its shape, whatever stands for the number
        (:func:`~forge.planning.code_evidence.quoted_example_shapes`), and
        the lines found join the words as ONE more entry the request names,
        so the window choice, the reference label and cap and the budget
        apply unchanged. A request with no such phrase, or one found nowhere,
        gives exactly what it gave before.
        """
        reader = reader or LocalCheckoutReader(repo_path)
        try:
            # The specification's OWN words, not the file's furniture. A
            # .feature carries a generated header ("# Generated by
            # /feature-spec") and Gherkin tags (@key-example, @edge-case,
            # @happy-path) that are the format's vocabulary rather than this
            # feature's, and looking for them returns the whole repository's
            # test corpus and buries the one hit that matters — on 2026-09-13
            # the real answer, src/users/router.py:210, came back THIRD behind
            # three of them.
            readable = "\n".join(
                line
                for line in (spec_feature or "").splitlines()
                if not line.lstrip().startswith(("#", "@"))
            )
            words: list[str] = []
            seen: set[str] = set()
            # The request's own words first: a spec writer may drop the route
            # the request named from its final draft (FEAT-F9B3 did).
            in_request = {
                _one_form(raw.strip().strip(".,;:"))
                for raw in _SPEC_WORD_PATTERN.findall(request_text or "")
            }
            readable = f"{request_text or ''}\n{readable}"
            for raw in _SPEC_WORD_PATTERN.findall(readable):
                word = raw.strip().strip(".,;:")
                lowered = word.lower().lstrip("/")
                if not word or lowered in _SPEC_WORDS_NOT_WORTH_LOOKING_FOR:
                    continue
                key = _one_form(word)
                if key in seen:
                    # The same word again, singular or plural: keep the
                    # shorter, whose search finds the longer too.
                    at = next(i for i, w in enumerate(words) if _one_form(w) == key)
                    if len(word) < len(words[at]):
                        words[at] = word
                    continue
                if len(words) >= _MAX_SPEC_WORDS_TRIED:
                    continue
                seen.add(key)
                words.append(word)
            # The request's own quoted examples holding a number (17 October
            # 2026, FEAT-15DD): plain English names no word above, so "3
            # notes" found nothing. None quoted, nothing here changes.
            quoted = quoted_example_shapes(request_text)
            if not words and not quoted:
                return None

            found: list[dict[str, Any]] = []
            found_candidates: list[list[dict[str, Any]]] = []
            found_hits: list[list[tuple[str, int]]] = []
            found_in_request: list[bool] = []
            # What the request names, as it writes it. A scenario's test
            # window must call one of the request's routes whole
            # (``routes``); only when the request names no route, one of
            # its other words' spellings that carry punctuation
            # (``active_count``), or the word itself when none does.
            anchors: list[str] = []
            routes: list[str] = []
            stopped: str | None = None
            scoring_words = request_words(request_text)
            texts: dict[str, str | None] = {}
            searched_any = False
            for word in words:
                if stopped or len(found) >= _MAX_SPEC_WORDS_LOOKED_FOR:
                    break
                # The word as the specification wrote it, and the two other
                # shapes a repository commonly spells the same name in. A route
                # written /users/count-today is a function named count_today
                # somewhere, and neither spelling finds the other. A route's
                # last segment with its slash, too (6 October 2026): where a
                # prefix is declared elsewhere, that is how the route reads.
                tail = word.lstrip("/").split("/")[-1]
                spellings = {word, tail, tail.replace("-", "_"), tail.replace("_", "-")}
                if word.startswith("/") and tail:
                    spellings.add("/" + tail)
                places: list[str] = []
                for spelling in sorted(s for s in spellings if len(s) > 3):
                    try:
                        answer = reader.places_mentioning(spelling)
                    except RepositoryUnreadable as exc:
                        stopped = str(exc)
                        break
                    searched_any = True
                    cut = getattr(answer, "cut", None)
                    if cut and partial is not None:
                        partial.append(
                            f"where `{spelling}` already appears was only partly "
                            f"searched ({cut})"
                        )
                    for path_and_line in answer:
                        if any(
                            path_and_line.startswith(prefix)
                            for prefix in _REPO_INVENTORY_SKIP_PREFIXES
                        ):
                            continue
                        if is_factory_file(
                            reader, path_and_line.rpartition(":")[0], texts
                        ):
                            continue
                        if path_and_line not in places:
                            places.append(path_and_line)
                if places:
                    places.sort(key=_how_interesting)
                    entry: dict[str, Any] = {
                        "words": word,
                        "already_in": places[:_MAX_PLACES_PER_SPEC_WORD],
                    }
                    try:
                        candidates, hits, not_read = candidate_windows(
                            reader,
                            places,
                            words=scoring_words,
                            rank=_how_interesting,
                            texts=texts,
                            spellings=sorted(s for s in spellings if len(s) > 3),
                            is_reference=lambda path: is_reference_path(
                                path, reference_paths
                            ),
                        )
                    except RepositoryUnreadable as exc:
                        # What was found is kept; the windows stop here.
                        stopped = str(exc)
                        candidates, not_read = [], []
                        hits = [
                            (path, int(line))
                            for path, _, line in (p.rpartition(":") for p in places)
                            if path and line.isdigit()
                        ]
                    if not_read:
                        entry["evidence_not_read"] = not_read
                    found.append(entry)
                    found_candidates.append(candidates)
                    found_hits.append(hits)
                    found_in_request.append(_one_form(word) in in_request)
                    if found_in_request[-1] and word.startswith("/"):
                        # A route the request names, whole: a scenario's
                        # test must call all of it, never just its last
                        # segment (``/archive`` is any route's).
                        if word not in routes:
                            routes.append(word)
                    elif found_in_request[-1]:
                        # Not a bare placeholder (``{user_id}``): that is
                        # any route's, not this request's name.
                        named = [
                            s
                            for s in spellings
                            if len(s) > 3
                            and re.search(r"[^A-Za-z0-9]", s)
                            and not re.fullmatch(r"/?\{[^}]*\}", s)
                        ]
                        anchors.extend(a for a in (named or [word]) if a not in anchors)
            if quoted and not stopped:
                # One more entry, beside the words: where the request's quoted
                # examples already stand in the code, with whatever stands
                # for the number. The window choice, the reference label and
                # cap, and the size budget below treat it like any word the
                # request names. Nothing found, nothing is added.
                example_places: list[str] = []
                looked_for: list[QuotedExample] = []
                for example in quoted:
                    try:
                        answer = reader.lines_mentioning(example.literal)
                    except RepositoryUnreadable as exc:
                        # Not looked for, not said: the description stays
                        # what the words alone made it.
                        logger.warning(
                            "target_repo_descriptor: the search for the "
                            'example "%s" could not run (%s); left out',
                            example.phrase,
                            exc,
                        )
                        break
                    searched_any = True
                    # A search cut short adds no note: the lines it did find
                    # are used, and a cut search that found none leaves the
                    # description exactly as it was (17 October 2026).
                    for path, number, text in answer:
                        place = f"{path}:{number}"
                        if (
                            not example.holds(text)
                            or place in example_places
                            or any(place.startswith(p) for p in _REPO_INVENTORY_SKIP_PREFIXES)
                            or is_factory_file(reader, path, texts)
                        ):
                            continue
                        example_places.append(place)
                        if example not in looked_for:
                            looked_for.append(example)
                if example_places:
                    example_places.sort(key=_how_interesting)
                    entry = {
                        "words": ", ".join(f'"{e.phrase}"' for e in looked_for),
                        "already_in": example_places[:_MAX_PLACES_PER_SPEC_WORD],
                    }
                    try:
                        candidates, hits, not_read = candidate_windows(
                            reader,
                            example_places,
                            words=scoring_words,
                            rank=_how_interesting,
                            texts=texts,
                            spellings=[e.literal for e in looked_for],
                            is_reference=lambda path: is_reference_path(
                                path, reference_paths
                            ),
                        )
                    except RepositoryUnreadable as exc:
                        stopped = str(exc)
                        candidates, not_read = [], []
                        hits = [
                            (path, int(line))
                            for path, _, line in (p.rpartition(":") for p in example_places)
                            if path and line.isdigit()
                        ]
                    if not_read:
                        entry["evidence_not_read"] = not_read
                    found.append(entry)
                    found_candidates.append(candidates)
                    found_hits.append(hits)
                    found_in_request.append(True)
            choose_windows(
                found,
                found_candidates,
                found_hits,
                from_request=found_in_request,
                max_windows=MAX_EVIDENCE_WINDOWS,
                max_chars=evidence_chars() if callable(evidence_chars) else evidence_chars,
                scenarios=[(words, values) for _title, words, values in scenario_words(spec_feature)],
                test_roots=test_roots,
                anchors=routes or anchors,
                # Only what is positively documentation may give way to a
                # scenario's test; a data file may be a test's cases or code.
                may_give_way=lambda path: is_documentation(path, test_roots),
                request_text=request_text,
                is_reference=lambda path: is_reference_path(path, reference_paths),
            )
            for entry in found:
                # The keys in the order the plan-writer has always read them.
                for key in ("evidence", "more_hits", "evidence_not_read"):
                    if key in entry:
                        entry[key] = entry.pop(key)
            if stopped:
                logger.warning(
                    "target_repo_descriptor: stopped looking for the "
                    "specification's words in %s (%s); the words already looked "
                    "up are kept, and the plan-writer is told the rest",
                    getattr(reader, "where", repo_path),
                    stopped,
                )
                if not searched_any:
                    if unavailable is not None:
                        unavailable.append(stopped)
                elif partial is not None:
                    partial.append(
                        "looking for where the specification's words already "
                        f"appear stopped part-way ({stopped}); the words "
                        "already looked up are kept"
                    )
            return found or None
        except RepositoryUnreadable as exc:
            logger.warning(
                "target_repo_descriptor: could not look for the "
                "specification's words in %s (%s); planning without them, and "
                "the plan-writer is told so",
                getattr(reader, "where", repo_path),
                exc,
            )
            if unavailable is not None:
                unavailable.append(str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — never fail a plan over this
            logger.warning(
                "target_repo_descriptor: could not look for the "
                "specification's words in %s (%s); planning without them",
                repo_path,
                exc,
            )
            if unavailable is not None:
                unavailable.append(
                    f"searching the repository failed ({type(exc).__name__})"
                )
            return None

    @staticmethod
    def _sets_the_request_names(
        request_text: str,
        *,
        reader: Any,
        unavailable: list[str] | None = None,
        partial: list[str] | None = None,
        reference_paths: Sequence[str] = (),
    ) -> list[dict[str, Any]] | None:
        """The files most likely to hold each set the request names.

        WHY (6 October 2026, planning improvements item 3). A request said
        "make deleted users disappear from all the count endpoints", and the
        plan's one count task named two files and missed the analytics
        files. When a request says "all the X", "every X" or "each X", this
        lists the files that hold the X word anywhere, so the plan-writer has
        to sort each one into "a member" or "not a member".

        * Every tracked file holding the word, case ignored, is a candidate.
          There is no file-type filter and no same-line rule: where the
          request's other words sit only ranks a file, never excludes it.
        * Only the factory's own files are left out
          (:data:`_REPO_INVENTORY_SKIP_PREFIXES`, the planner's own
          ``qa/pass-bar-*.yaml`` and, since 7 October 2026, the sandbox
          scripts the factory ships into a project, at ``deploy/<name>``
          and reading as the script). A file that
          holds the word only on machine-written lines (over
          :data:`~forge.planning.code_evidence.MACHINE_WRITTEN_LINE_CHARS`
          characters) is ranked after every other.
        * Ranked by how many of the request's other words a file holds, then
          by how densely it holds the X word, then by path; at most 120 files
          are read for that, and the rest follow by path.
        * At most 24 are listed, each with its two best matching lines.
          ``listed_all`` is true only when nothing was cut by that limit or by
          a search cut short. It says nothing about membership.

        ``None`` when the request names no set. A repository that cannot be
        read still gives the entry, with no candidates, ``listed_all`` false
        and the reason, because finding the phrase needs no read. A search
        that cannot be finished is said on that entry and as a part of the
        repository read only in part (``partial``), never as the repository
        being unreadable: by then the inventory and the evidence were read.
        Never raises.
        """
        try:
            phrases = quantified_phrases(request_text or "")
        except Exception:  # noqa: BLE001 — never fail a plan over this
            return None
        if not phrases:
            return None
        entries: list[dict[str, Any]] = []
        for phrase, word, phrase_words in phrases:
            try:
                entries.append(
                    set_candidates(
                        reader,
                        request_text,
                        phrase,
                        word,
                        phrase_words,
                        # A file in a reference folder is never a member of
                        # the product's set (17 October 2026).
                        skip_prefixes=(
                            *_REPO_INVENTORY_SKIP_PREFIXES,
                            *reference_paths,
                        ),
                        partial=partial,
                    )
                )
                continue
            except RepositoryUnreadable as exc:
                why = str(exc)
            except Exception as exc:  # noqa: BLE001 — never fail a plan over this
                why = f"searching the repository failed ({type(exc).__name__})"
            reason = f'the search for "{phrase}" could not be finished: {why}'
            logger.warning(
                "target_repo_descriptor: %s (%s); the plan-writer is told so",
                reason,
                getattr(reader, "where", "the repository"),
            )
            if partial is not None and reason not in partial:
                partial.append(reason)
            entries.append(
                {
                    "phrase": phrase,
                    "looked_for": word,
                    "candidates": [],
                    "listed": 0,
                    # Nothing was searched: 0, with the reason beside it, so
                    # the reader's whole-number field stays a whole number.
                    "matched": 0,
                    "listed_all": False,
                    "listed_all_means": LISTED_ALL_MEANS,
                    "unavailable": reason,
                }
            )
        return entries

    async def _coverage_command_declared(self, repo_path: str, branch: str) -> bool:
        """``True`` when the project's ``.guardkit/config.yaml`` declares a
        coverage command, read off the planning branch, else off the
        repository's current tip (the same two places the plan-commit
        stamping reads it). ``False`` when it cannot be read. Never raises."""
        git_runner = self._deps.git_runner
        for ref in (branch, "HEAD"):
            try:
                text = await git_runner.read_file_from_branch(
                    repo_path=repo_path,
                    branch=ref,
                    file_path=coverage_declaration.CONFIG_PATH,
                )
            except Exception as exc:  # noqa: BLE001 — never fail a plan over this
                logger.warning(
                    "planning driver: %s could not be read from %s (%s); the "
                    "plan writer is told no coverage command is declared",
                    coverage_declaration.CONFIG_PATH,
                    ref,
                    exc,
                )
                return False
            if text is not None:
                return coverage_declaration.declares_coverage_command(text)
        return False

    @staticmethod
    def _build_target_repo_descriptor(
        target_repo: str,
        repo_path: str,
        spec_feature: str = "",
        *,
        reader: Any = None,
        unavailable: list[str] | None = None,
        partial: list[str] | None = None,
        request_text: str = "",
        ranked_source: bool = False,
        reference_paths: Sequence[str] = (),
        max_evidence_chars: int = MAX_EVIDENCE_CHARS,
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
        2026-08-31; ``repository_inventory`` is present only when forge can read
        the target repository's tracked files (see
        :meth:`_read_repository_inventory`), and its schema field is the
        companion change on the specialist-agent side — the field is optional on
        both sides deliberately, so either image can be redeployed first and a
        descriptor without it plans byte for byte as it does today.

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

        ``reference_paths`` (17 October 2026, prototype-to-factory design 2.2)
        are the project's declared reference folders, read from the run's
        record: not product code, so no test folder is found in them, the
        inventory counts them apart, their code windows are labelled and
        capped, and no set's candidates come from them. Empty, the
        descriptor is byte for byte today's.

        ``max_evidence_chars`` (17 October 2026) is the size budget of the
        windows' text and the set candidates' lines together; the spec
        leg's call (:meth:`_spec_code_evidence`) passes half the plan's.
        Left out, it is :data:`~forge.planning.code_evidence.MAX_EVIDENCE_CHARS`
        as before.
        """
        # Local import: keep the guardkit-boundary reuse out of the module
        # import graph (target_terminal_tools imports the guardkit run seam).
        from forge.planning.target_terminal_tools import (
            TargetTestRootsUnresolved,
            discover_target_test_roots,
            discover_test_roots_from_listing,
            shallow_discover_test_roots,
        )

        # Every read below goes through the planner's repository reader
        # (1 October 2026, the 1 October planner fix): the sandbox helper for a
        # sandboxed repository, the checkout otherwise. When the repository
        # cannot be read the reason goes back through ``unavailable``; the
        # descriptor gains no field its schema does not define, and the plan
        # leg says it on the plan-writer's ``repository_facts`` instead.
        reader = reader or LocalCheckoutReader(
            repo_path, timeout_s=_REPO_INVENTORY_GIT_TIMEOUT_SECONDS
        )
        reasons: list[str] = []
        begin = getattr(reader, "begin", None)
        if callable(begin):
            begin()  # one time allowance for the whole description
        if _reads_beyond_the_working_tree(reader):
            # No checkout here to walk, or (7 October 2026) a reader pinned
            # to the starting commit, whose working tree is not that commit:
            # the same discovery runs over the reader's own listing.
            try:
                test_roots = discover_test_roots_from_listing(reader.list_files())
            except RepositoryUnreadable as exc:
                logger.warning(
                    "target_repo_descriptor: could not discover the test "
                    "folders (%s); the plan-writer is told so",
                    exc,
                )
                reasons.append(str(exc))
                test_roots = []
        else:
            try:
                test_roots = discover_target_test_roots(repo_path)
            except TargetTestRootsUnresolved as exc:
                # Degraded path: guardkit absent from the interpreter. Production
                # images always ship guardkit (the Dockerfile asserts the import),
                # so this only fires in a guardkit-less env where the real
                # ``feature validate`` oracle cannot run either. Fall back to
                # forge's own discovery so the descriptor is still built and the
                # run reaches the oracle (the last line of defense) rather than
                # crashing — log LOUDLY.
                #
                # The old fallback returned bare ``["tests"]`` / ``["test"]``,
                # which is the ROUND-10 DEFECT SHAPE: a bare ``tests`` root is a
                # prefix of every ``tests/<x>`` path, so the in-session
                # containment gate waves through an invented subdirectory. The
                # replacement reproduces guardkit's per-suite Python shape AND
                # the TypeScript shapes, so the degraded answer has the same
                # geometry as the healthy one.
                logger.warning(
                    "target_repo_descriptor: guardkit test-root discovery "
                    "unavailable (%s); falling back to forge's own shallow "
                    "discovery — the roots are shape-correct but are not the "
                    "oracle's own answer",
                    exc,
                )
                test_roots = shallow_discover_test_roots(repo_path)
        if reference_paths:
            # A test folder inside reference material is not the product's.
            test_roots = [
                root
                for root in test_roots
                if not is_reference_path(f"{root.rstrip('/')}/", reference_paths)
            ]
        descriptor: dict[str, Any] = {"repo": target_repo, "test_roots": test_roots}
        if getattr(reader, "commit", None):
            descriptor["planning_base_identity"] = {
                "status": "bound",
                "commit": reader.commit,
            }
        # The repo's own architecture rules, when it keeps a rules file. Before
        # this, the plan-writer was never shown them, and two features built the
        # week of 2026-08-24 drifted from rules nobody had told it about.
        architecture_rules = PlanningRunDriver._read_architecture_rules(
            repo_path, reader=reader, unavailable=reasons, partial=partial
        )
        if architecture_rules is not None:
            descriptor["architecture_rules"] = architecture_rules
        # What the repository ALREADY contains. Before this, the seat was told
        # the rules but never the code, so on 2026-09-11 a one-endpoint sentence
        # came back as a four-wave plan whose first two waves rebuilt a model, a
        # data-access layer and a router the repository has had since it was
        # born. Same shape of cure as the rules above: present only when the
        # repository can be read, absent otherwise, and planning unchanged when
        # it is absent (see :meth:`_read_repository_inventory`).
        repository_inventory = PlanningRunDriver._read_repository_inventory(
            repo_path,
            reader=reader,
            unavailable=reasons,
            reference_paths=reference_paths,
        )
        if repository_inventory is not None:
            descriptor["repository_inventory"] = repository_inventory
        # Names were not enough (FEAT-19C4): the writer knew the router file
        # existed and planned to create a route already inside it. Present only
        # when the specification has distinctive words AND the repository
        # already has them; absent, the plan is byte for byte what it is today.
        # "All the X" (6 October 2026, planning improvements item 3): the
        # files most likely to hold the set's members, for the plan-writer to
        # sort into members and not. Absent when the request names no set.
        # Searched after the windows' files are read and before the windows
        # are chosen (6 October 2026, evidence coverage): the windows then
        # know how much of the budget the candidates' lines leave them (their
        # half, and whatever the lines do not use), and on a reader with one
        # shared time allowance the set search, not the code already there,
        # is what a timeout cuts.
        searched: dict[str, Any] = {}

        def sets_then_window_share() -> int:
            searched["sets"] = PlanningRunDriver._sets_the_request_names(
                request_text,
                reader=reader,
                unavailable=reasons,
                partial=partial,
                reference_paths=reference_paths,
            )
            set_lines_chars = sum(
                len(line)
                for entry in searched["sets"] or []
                for candidate in entry.get("candidates") or []
                for line in candidate.get("lines") or []
            )
            return max(max_evidence_chars // 2, max_evidence_chars - set_lines_chars)

        already_there = PlanningRunDriver._where_the_specs_words_already_appear(
            repo_path,
            spec_feature,
            reader=reader,
            unavailable=reasons,
            partial=partial,
            request_text=request_text,
            evidence_chars=sets_then_window_share,
            test_roots=test_roots,
            reference_paths=reference_paths,
        )
        if "sets" not in searched:
            # No word to look for, or the search could not run: the set
            # search still runs.
            sets_then_window_share()
        sets = searched["sets"]
        # One size budget for both, across the whole descriptor: the
        # lowest-scoring windows go first, then candidate lines, and what
        # went is counted on the entries it came from.
        trimmed = trim_to_budget(
            already_there or [], sets or [], budget=max_evidence_chars
        )
        if trimmed["windows_trimmed"] or trimmed["lines_trimmed"]:
            logger.info(
                "target_repo_descriptor: the code shown to the plan-writer was "
                "trimmed to its size budget (%s)",
                trimmed,
            )
        if already_there:
            descriptor["where_the_specs_words_already_appear"] = already_there
        if sets:
            descriptor["sets_the_request_names"] = sets
        # Ranked code blocks from the starting commit (12 October 2026), only
        # with the planning.ranked_source switch on and a reader that can ask
        # the sandbox helper; off, the descriptor is byte for byte today's.
        if ranked_source:
            field = PlanningRunDriver._ranked_source_for(
                reader,
                request_text or spec_feature,
                test_roots=test_roots,
                shown=[
                    window
                    for entry in already_there or []
                    for window in entry.get("evidence") or []
                ],
            )
            if field is not None:
                descriptor["ranked_source"] = field
        if reasons and unavailable is not None:
            unavailable.extend(reasons)
        return descriptor

    @staticmethod
    def _ranked_source_for(
        reader: Any,
        text: str,
        *,
        test_roots: list[str],
        shown: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """The descriptor's ``ranked_source``, or ``None`` when this reader
        cannot ask the sandbox helper (a local checkout). A helper that cannot
        answer gives a field that says so, never an empty "nothing found"."""
        ask = getattr(reader, "ranked_source", None)
        if not callable(ask):
            return None
        # The request's own distinctive words, filler words left out, so a
        # long request or a Gherkin specification still makes a short query.
        query = " ".join(request_words(text or "", leave_out=_GHERKIN_WORDS))[
            :_RANKED_QUERY_CHARS
        ].strip()
        if not query:
            return None
        try:
            answer = ask(query)
        except RepositoryUnreadable as exc:
            logger.warning("target_repo_descriptor: ranked code search failed (%s)", exc)
            return {
                "commit": None,
                "tool": None,
                "query_words": [],
                "status": "tool_error",
                "blocks": [],
                "truncated": False,
                "truncation": [],
                "coverage": {},
                "note": f"the ranked code search could not run: {exc}",
            }
        return ranked_source_field(
            answer, reader=reader, test_roots=test_roots, shown_windows=shown
        )

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

    async def _fail(
        self, correlation_id: str, *, stage_label: str, reason: str
    ) -> None:
        # The write itself lives in forge.planning.failure so the intake
        # consumer's refusal and this driver end a run through ONE piece of
        # code (2026-09-05 rule 4).
        transitioned = mark_run_failed(
            self._deps.store,
            correlation_id,
            stage_label=stage_label,
            reason=reason,
            log=logger,
        )
        publish = self._deps.publish_planning_failed
        if transitioned and publish is not None:
            try:
                await publish(correlation_id, reason)
            except Exception:
                logger.warning(
                    "planning driver: planning-failed projection did not go out "
                    "for %s (durable row remains FAILED)",
                    correlation_id,
                )

    async def _settle_routing_failure(
        self, correlation_id: str, reason: str
    ) -> None:
        transitioned = settle_routing_failure(
            self._deps.store,
            correlation_id,
            stage_label="feature-routing",
            reason=reason,
            log=logger,
        )
        publish = self._deps.publish_planning_failed
        if transitioned and publish is not None:
            try:
                await publish(correlation_id, reason)
            except Exception:
                logger.warning(
                    "planning driver: routing failure projection did not go out "
                    "for %s (durable row remains FAILED)",
                    correlation_id,
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
