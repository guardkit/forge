"""Plain names for pipeline stages — the noun's SINGLE SOURCE (Rich, 2026-07-31).

The ruling (the hybrid): a stage name that reaches a user surface — a Slack
notification, an approval card, CLI help — speaks HUMAN. It does that through
three parts, and this module is the first two of them:

1. **ONE static table.** :data:`STAGE_PLAIN_NAMES` maps every internal stage
   label (the :class:`~forge.pipeline.stage_taxonomy.StageClass` values plus the
   planning-driver leg labels) onto a plain-name NOUN taken from the ratified
   factory phrase-book (``docs/ways-of-working/factory-phrase-book.md`` in
   ai-transition). One row per label; the table is the only place a noun is
   minted.
2. **A deterministic humaniser.** :func:`humanise_stage_label` renders an
   UNKNOWN label ("transition-to-cancelled", a label minted after this table was
   written) as ``the <words> step``. Never a crash, never a raw internal field
   name in front of the owner. It is a rare net, not the norm — a CI fence
   (``tests/forge/pipeline/test_stage_names.py``) asserts every stage class and
   every driver leg label has a real row.

The third part is NOT here: render sites compose their own human SENTENCES
around the noun. Bespoke phrasing is welcome and encouraged; the noun always
comes from :func:`plain_stage_name`.

**No runtime AI.** A dict and two functions. Deterministic, offline, free.

**What this module is NOT for.** Logs, correlation ids, NATS subjects, durable
event rows, wire payload fields (``stage_label`` on an approval envelope) and
the stage enums themselves are MACHINE surfaces and stay exactly as they are.
Translating there would break identity, idempotency and every grep. This module
is consulted at the last hop before a human reads the text.

This module deliberately imports nothing but :mod:`forge.pipeline.stage_taxonomy`
(itself import-free), so any dispatcher, driver or CLI can consult it without
forming an import cycle.
"""

from __future__ import annotations

import re

from forge.pipeline.stage_taxonomy import StageClass

__all__ = [
    "STAGE_PLAIN_NAMES",
    "plain_stage_name",
    "humanise_stage_label",
]


#: Internal stage label → plain-name noun. THE single source of the noun.
#:
#: Keys are the exact internal labels that appear in code, durable rows and wire
#: payloads. Values are phrase-book English: what a NEW reader — a client, a
#: YouTube viewer, James — understands in one pass with no glossary. They are
#: written to drop into a sentence after "stopped at" / "waiting at" / "finished"
#: without further inflection.
#:
#: A lane that mints a new stage label adds its row HERE in the same commit; the
#: fence test fails otherwise.
STAGE_PLAIN_NAMES: dict[str, str] = {
    # ------------------------------------------------------------------ #
    # The canonical stage taxonomy (StageClass — pipeline/stage_taxonomy.py)
    # ------------------------------------------------------------------ #
    StageClass.PRODUCT_OWNER.value: "shaping the product brief",
    StageClass.ARCHITECT.value: "shaping the architecture",
    StageClass.SYSTEM_ARCH.value: "drawing the system architecture",
    StageClass.SYSTEM_DESIGN.value: "drawing the system design",
    StageClass.FEATURE_SPEC.value: "writing the spec",
    StageClass.FEATURE_PLAN.value: "writing the task plan",
    StageClass.AUTOBUILD.value: "running the build",
    # The phrase-book's own row: pull-request-review is the PRE-RULING name for
    # the merge-ready checkpoint. Matches MERGE_READY_CHECKPOINT_LABEL verbatim
    # (pipeline/merge_ready_checkpoint.py) — one noun, two call sites.
    StageClass.PULL_REQUEST_REVIEW.value: "the merge-ready checkpoint",
    # The fix journey (phrase-book: "failed build in → diagnosed, bounded fix out")
    StageClass.TASK_REVIEW.value: "diagnosing the failed build",
    StageClass.TASK_WORK.value: "working the bounded fix",
    # The last mile (phrase-book: deploy the merged feature + the live smoke test)
    StageClass.DEPLOY.value: "deploying the merged feature",
    StageClass.LIVE_GATE.value: "the live smoke test",
    # ------------------------------------------------------------------ #
    # The planning-driver leg labels (planning/driver.py, planning/revision.py)
    # ------------------------------------------------------------------ #
    "planning-start": "starting the planning run",
    "resource-preflight": "the pre-run resource check",
    "planning-boundary": "the planning safety check",
    "planning-dispatch": "handing the work to the spec-writer",
    # The PO leg's own label is underscored where the stage class is hyphenated;
    # both are the same act, so both carry the same noun.
    "product_owner": "shaping the product brief",
    "product_docs": "checking the machine's assumptions",
    "planning-escalation-timeout": "chasing an unanswered card",
    "planning-revision": "revising from your note",
    "planned-handoff": "handing the plan over",
    "target-terminal-enter": "starting the machine chain",
    "feature-spec-complete": "finishing the spec",
    "feature-spec-draft": "writing the spec",
    "feature-spec-digest-review": "reading the spec digest",
    "build-queued": "handing to the build system",
    # HISTORICAL — old ledger rows only. The W1-S2 DCL leg was struck on
    # 2026-08-15 (guardkit deleted the `.dcl` spec track outright), so no run
    # records this label any more; the entry stays so ledgers written before
    # the strike still render in plain words.
    "dcl-author": "writing the capability file",
    "qa-pass-bars": "registering the quality checklist",
    "qa-pass-bars-auth-confirm": "confirming whether there is a sign-in",
    "qa-feature-gate": "registering the live check",
}


#: Every run of separators / whitespace that the humaniser collapses to one space.
_SEPARATORS = re.compile(r"[-_\s]+")

#: What an EMPTY or all-separator label renders as. Never an empty sentence
#: fragment, never a bare colon in front of the owner.
_UNNAMED_STEP = "the current step"


def humanise_stage_label(label: str) -> str:
    """Render an unknown internal label as plain English — the fallback net.

    Deterministic, offline, total: hyphens and underscores become spaces, runs
    collapse, and the words are wrapped as ``the <words> step``. An empty,
    whitespace-only or separators-only label renders as
    :data:`_UNNAMED_STEP` rather than an empty fragment.

    This is the NET, not the norm — a label that reaches here has no row in
    :data:`STAGE_PLAIN_NAMES`, which the CI fence forbids for every known stage.
    It exists so a label minted at runtime (``run_store`` synthesises
    ``transition-to-<state>`` when a caller supplies none) still reads as English
    instead of leaking a raw field value or raising.

    >>> humanise_stage_label("qa-feature-gate")
    'the qa feature gate step'
    >>> humanise_stage_label("some_weird__label")
    'the some weird label step'
    >>> humanise_stage_label("")
    'the current step'
    """
    words = _SEPARATORS.sub(" ", str(label)).strip()
    if not words:
        return _UNNAMED_STEP
    return f"the {words} step"


def plain_stage_name(label: str) -> str:
    """The plain-name noun for ``label`` — table first, humaniser as the net.

    The ONE function every render site calls. Composing the sentence AROUND the
    noun is the render site's own job (bespoke phrasing welcome); minting the
    noun is not.

    >>> plain_stage_name("qa-pass-bars")
    'registering the quality checklist'
    >>> plain_stage_name("pull-request-review")
    'the merge-ready checkpoint'
    >>> plain_stage_name("never-heard-of-it")
    'the never heard of it step'
    """
    return STAGE_PLAIN_NAMES.get(str(label).strip(), "") or humanise_stage_label(label)
