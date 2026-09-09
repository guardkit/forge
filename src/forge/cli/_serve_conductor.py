"""The conductor's PRODUCTION composition — the factories the router needs.

Conductor revival Stage 2, shakeout items 3 and 4
(``supervisor-revival-design-pass-2026-07-31``).

What Stage 1 left, and why
--------------------------

Stage 1 built every piece and wired none of the last two:

* ``build_conductor_router`` was called with **no** ``supervisor_factory``,
  so it logged loudly and returned ``None`` — inert even with the flag ON.
  That was the honest Stage-1 posture ("the daemon composes no Supervisor
  today; stay inert rather than half-wire one"), and this module is the
  discharge of it.
* ``ConductorDriverDeps`` fell back to *all-None* seams, so the first
  non-terminal turn hit a wait it could not perform and died
  ``WAIT_EXPIRED`` with no receipts at all.

Both are composition problems, and this is the composition root's own
module so ``cli/serve.py`` grows two calls rather than three hundred lines.

The M0 statement, made structural
---------------------------------

Design pass §g: **the fix journey adds zero frontier calls to the routine
path**, because the mode branch runs at step 1a of ``next_turn`` — before
the reasoning-model step. That is a claim about control flow, and a claim
is cheap. Here it is a *guard*: the Mode-A-only seams
(``reasoning_model``, ``specialist_dispatcher``, ``async_task_starter``)
are filled with :class:`_ModeAOnlySeam` stand-ins that RAISE, naming
themselves, if a fix-journey turn ever reaches them. A conductor that
started consulting a reasoning model would fail loudly on its first turn
instead of quietly spending frontier tokens on the routine path.

Flag OFF changes nothing here: nothing in this module is constructed
unless ``conductor.enabled`` is on — ``build_conductor_mode_kwargs``
answers ``{}`` and ``build_conductor_router`` answers ``None`` first.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import subprocess
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Sequence

from forge.cli._conductor_worktree import JOURNEY_BASE_REF
from forge.pipeline.conductor_driver import (
    CHECKPOINT_VERDICT_RECORDED_KEY,
    ConductorDriverDeps,
    WaitWindow,
)
from forge.pipeline.stage_taxonomy import StageClass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The two leg TRIPWIRES (LI stage-2 design §1)
# ---------------------------------------------------------------------------
#
# READ THIS BEFORE CHANGING EITHER NUMBER.
#
# These two constants are **tripwires for "the leg itself is broken" —
# never work-limiters.** They exist to catch a wedged spawn, a dead
# harness, a process that will never return. They are NOT a statement
# about how long real work is allowed to take, and no one may cite them as
# one. Rich's ruling of 2026-07-30, quoted verbatim in
# ``forge/subagents/build_monitor.py``:
#
#     "hardcoding kill time limits isn't the way — the forge should be
#     able to monitor the autobuilds — it spews out enough diagnostics."
#
# The ledgered DESTINATION is therefore the **monitored-supervision path**:
# BuildMonitor-class semantic liveness applied to this one-shot dispatch
# seam, which replaces the blind clock with the leg's own diagnostics (and
# brings with it the in-flight stage row and a TIMEOUT dispatch status
# distinct from FAILED). Until that lands these two numbers are the only
# ceiling a fix journey has, and a leg with NO ceiling is how the crossing
# ran ~200 legs.
#
# Why 1800 for the work leg, specifically: the approval wait window anchors
# on ``builds.started_at`` with ``approval.max_wait_seconds`` defaulting to
# 3600, and the JetStream ``ack_wait`` is 3600. 1800 keeps a small journey
# inside both. Anything larger MUST raise those two in the same change.
#
# Why the review leg keeps 600: its inner budget is 480s, and the
# inner-under-outer discipline is load-bearing — on an outer timeout the
# parser discards even a perfect marker block, so the leg dies silently.

#: Outer tripwire for a ``task-review`` leg. Its inner budget is 480s.
CONDUCTOR_REVIEW_STAGE_TIMEOUT_SECONDS: int = 1860  # 2026-09-09: the review leg's model budget is profile-driven (leg_sdk_timeout_seconds, now up to 1800s on the workhorse) and the outer wall must exceed it. Today's eight real reviews on this task ran 191, 221, 312, 326, 358, 675, 723 and 903 seconds on an idle box with the model warm — the tail is long, and a review that runs past its budget costs the owner a whole tap

#: Outer tripwire for a ``task-work`` leg. The leg's own inner budget is
#: 1620s (the builder venue's ``--leg-budget`` default) — the same
#: inner-under-outer discipline the review leg already ships.
CONDUCTOR_WORK_STAGE_TIMEOUT_SECONDS: int = 1800

#: The per-stage mapping the dispatcher adapter selects from. Derived from
#: the two constants above so there is exactly one place either number is
#: written — and READ-ONLY (:class:`~types.MappingProxyType`) so that rule
#: is structural rather than conventional: this object is handed straight
#: to the dispatcher factory at composition time, and a plain dict would
#: let any holder rewrite a tripwire in place, leaving the named constant
#: above saying one thing and the live mapping doing another.
CONDUCTOR_STAGE_TIMEOUT_SECONDS: "Mapping[StageClass, int]" = MappingProxyType(
    {
        StageClass.TASK_REVIEW: CONDUCTOR_REVIEW_STAGE_TIMEOUT_SECONDS,
        StageClass.TASK_WORK: CONDUCTOR_WORK_STAGE_TIMEOUT_SECONDS,
    }
)

#: **The seat's env stopgap is GONE — landed as config-as-code, 2026-08-03.**
#: The fix journey's SEAT (the local model a leg runs on, LI stage-2 design
#: §3.4) used to ride an operator env var, ``FORGE_CONDUCTOR_LEG_MODEL``,
#: read exactly once in :func:`build_conductor_supervisor_factory`. That
#: read's own ledger comment promised: "When that field lands, this env
#: read is DELETED, not kept beside it: two statements of one rule is a
#: future lie."
#:
#: The field landed (conductor-activation design pass §2 / FA3): the seat
#: is :attr:`forge.config.models.ConductorConfig.seat`, threaded from the
#: production composition root (``serve.py``'s ``_compose_conductor_router``)
#: as this factory's ``leg_model`` argument. ``conductor.enabled: true``
#: with no seat now REFUSES at config load, so the daemon cannot boot
#: half-activated and no leg can ride ``None`` down to a frontier default.
#: There is nothing to read from the environment any more, and this note
#: exists so nobody re-introduces the second statement.

__all__ = [
    "CONDUCTOR_REVIEW_STAGE_TIMEOUT_SECONDS",
    "CONDUCTOR_STAGE_TIMEOUT_SECONDS",
    "CONDUCTOR_WORK_STAGE_TIMEOUT_SECONDS",
    "DECLARED_TEST_EVIDENCE_LIMIT_BYTES",
    "DECLARED_TEST_EVIDENCE_TAIL_LINES",
    "DeclaredTestDetail",
    "TOOLCHAIN_MODULE_CANDIDATES",
    "failing_cases_in_output",
    "failure_lines_in_output",
    "summarise_declared_test_output",
    "build_conductor_driver_deps_factory",
    "build_conductor_supervisor_factory",
    "load_declared_toolchain",
    "make_conductor_close_out",
    "make_conductor_failure_pack_writer",
    "make_conductor_queue_release",
    "make_conductor_guardkit_run_chooser",
    "make_conductor_receipts_exporter",
    "RECEIPTS_EXPORT_TIMEOUT_S",
    "make_conductor_merge_card_published_probe",
    "make_conductor_subscribe_resume",
    "make_conductor_wait_window_reader",
    "candidate_is_checked_before_the_merge",
    "make_gates_green_reader",
    "make_specification_fence",
    "load_declared_specification_paths",
    "load_declared_specification_paths_from_sandbox",
    "read_branch_changes",
    "BRANCH_DIFF_LIMIT_BYTES",
    "read_branch_changes_in_sandbox",
    "specification_paths_in",
    "make_merge_ready_checkpoint",
]


#: ``stage_log`` statuses that count as "this stage was approved" for the
#: ordering guard's read side. The fix journey's rows are written by
#: ``_serve_deps_stage_log.build_fix_journey_stage_log_writer``, which maps a
#: successful dispatch to ``PASSED``.
_APPROVED_STATUSES: frozenset[str] = frozenset({"PASSED"})


class _ModeAOnlySeam:
    """A collaborator the fix journey must never reach.

    ``build_supervisor`` requires the full Mode A collaborator set even
    though a Mode C turn consults barely half of it. Filling the unused
    seams with ``None`` would turn a control-flow mistake into an
    ``AttributeError`` three frames away; filling them with a working
    implementation would mean the conductor *could* silently take the Mode
    A path — and for ``reasoning_model`` that would mean a frontier call
    on a path whose whole point is not making one (M0, design pass §g).

    So each unused seam is this: an object that raises with its own name
    and the reason, the moment anything touches it.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            f"conductor: the {self._name!r} seam was reached on a "
            "conductor-driven build. The fix journey branches at step 1a of "
            "next_turn, before every Mode A collaborator — reaching this seam "
            "means the mode branch did not fire. Refusing rather than running "
            "the Mode A path (for reasoning_model that would also be a "
            "frontier call on the M0-zero path)."
        )

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        return self._refuse

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._refuse(*args, **kwargs)


class _NoToolsMiddleware:
    """Stand-in for the async-subagent middleware, with an empty tool set.

    ``build_supervisor`` READS ``middleware.tools`` at construction time to
    populate ``Supervisor.tools`` — so this seam cannot be a
    :class:`_ModeAOnlySeam` (that would refuse during composition, before
    any turn has run) and it cannot be ``None`` (the factory would then
    construct the real DeepAgents middleware, which exists to dispatch the
    Mode A autobuild the fix journey never runs).

    An empty tuple is the honest answer: the conductor's reasoning loop is
    the Mode C planner, a stateless pure function that uses no tools.
    """

    __slots__ = ()

    tools: tuple = ()


class _SqliteOrderingStageLogReader:
    """``stage_ordering_guard.StageLogReader`` over the daemon's pool.

    Two questions only: "is this stage approved for this build?" and "what
    features are in the catalogue?". The fix journey has no feature
    catalogue — its subject is a task — so the catalogue answers empty,
    which the guard reads as "``pull-request-review`` is not dispatchable
    on the multi-feature branch". The Mode C chain reaches the merge-ready
    checkpoint through its own planner branch, not that one.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def is_approved(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> bool:
        try:
            rows = self._pool.read_stages(build_id)
        except Exception as exc:  # noqa: BLE001 — a read defect is not approval
            logger.error(
                "conductor ordering reader: read_stages raised %s: %s for "
                "build_id=%s — answering NOT approved (never guess approved)",
                type(exc).__name__,
                exc,
                build_id,
            )
            return False
        for row in rows:
            if getattr(row, "stage_label", None) != stage.value:
                continue
            if str(getattr(row, "status", "")) not in _APPROVED_STATUSES:
                continue
            details = getattr(row, "details", None) or {}
            if feature_id is not None and details.get("feature_id") != feature_id:
                continue
            return True
        return False

    def feature_catalogue(self, build_id: str) -> list[str]:
        return []


class _SqliteBuildStateReader:
    """``supervisor.StateMachineReader`` over the daemon's pool."""

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def get_build_state(self, build_id: str) -> Any:
        row = self._pool.get_build_row(build_id)
        if row is None:
            from forge.lifecycle.state_machine import BuildState

            logger.error(
                "conductor state reader: no builds row for build_id=%s — "
                "answering FAILED so the turn stops rather than dispatching "
                "against a row that is not there",
                build_id,
            )
            return BuildState.FAILED
        return row.status


class _SqliteTurnRecorder:
    """``supervisor.StageLogTurnRecorder`` over the daemon's pool.

    Every conductor turn leaves a durable audit row: the outcome, the
    permitted set, the chosen stage, the planner's rationale. This is the
    fix journey's own history for a human reading back what the machine
    decided — distinct from the dispatch rows the dispatcher writes.
    """

    __slots__ = ("_pool", "_clock")

    def __init__(self, pool: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self._pool = pool
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def record_turn(
        self,
        *,
        build_id: str,
        outcome: Any,
        permitted_stages: Any,
        chosen_stage: Any,
        chosen_feature_id: str | None,
        rationale: str,
        gate_verdict: str | None,
    ) -> None:
        from forge.lifecycle.persistence import StageLogEntry

        now = self._clock()
        try:
            self._pool.record_stage(
                StageLogEntry(
                    build_id=build_id,
                    stage_label="conductor-turn",
                    target_kind="local_tool",
                    target_identifier=(
                        getattr(chosen_stage, "value", None) or "no-stage"
                    ),
                    status="PASSED",
                    gate_mode=None,
                    coach_score=None,
                    threshold_applied=None,
                    started_at=now,
                    completed_at=now,
                    duration_secs=0.0,
                    details={
                        "outcome": getattr(outcome, "value", str(outcome)),
                        "permitted_stages": sorted(
                            getattr(s, "value", str(s)) for s in permitted_stages
                        ),
                        "chosen_stage": getattr(chosen_stage, "value", None),
                        "chosen_feature_id": chosen_feature_id,
                        "rationale": rationale,
                        "gate_verdict": gate_verdict,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — an audit row never kills a turn
            logger.warning(
                "conductor turn recorder: record_stage raised %s: %s for "
                "build_id=%s — the turn stands, the audit row is missing",
                type(exc).__name__,
                exc,
                build_id,
            )


# ---------------------------------------------------------------------------
# THE GATE SET ON THE CANDIDATE BRANCH — a real, bounded evaluation
# ---------------------------------------------------------------------------


#: Where guardkit's declaration loader lives, in the two shapes the estate
#: installs guardkit (the wheel ships ``packages = ["guardkit"]``; a source
#: checkout on ``PYTHONPATH`` also exposes the bare package). Resolved the
#: same way :mod:`forge.planning.target_terminal_tools` resolves its own
#: guardkit imports — by candidate, never by assuming one shape.
TOOLCHAIN_MODULE_CANDIDATES: tuple[str, ...] = (
    "guardkit.orchestrator.toolchain_declaration",
    "orchestrator.toolchain_declaration",
)


def load_declared_toolchain(
    repo_root: "Path | str",
    *,
    module_candidates: Sequence[str] = TOOLCHAIN_MODULE_CANDIDATES,
) -> Any | None:
    """Read ``<repo_root>/.guardkit/config.yaml``'s ``toolchain:`` block.

    Delegates to guardkit's OWN
    ``toolchain_declaration.load_toolchain_declaration`` — the loader that
    already owns the schema, the ``extra="forbid"`` typo refusal and the
    malformed-block loud degrade. Forge does not re-implement any of it and
    does not parse the YAML itself; a second parser would be a second
    opinion about what a repo declared.

    Returns ``None`` — never raises — when guardkit is not importable in
    this interpreter, when the file is absent, or when the block declares
    nothing. Every one of those is an honest UNKNOWN upstream, which the
    merge-ready checkpoint treats as RED.
    """
    for candidate in module_candidates:
        try:
            module = import_module(candidate)
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
        try:
            return module.load_toolchain_declaration(Path(repo_root))
        except Exception as exc:  # noqa: BLE001 — a loader defect is not green
            logger.error(
                "conductor gates: %s.load_toolchain_declaration raised %s: %s "
                "for repo_root=%s — no declaration; the gate set answers "
                "UNKNOWN, which is red",
                candidate,
                type(exc).__name__,
                exc,
                repo_root,
            )
            return None
    logger.warning(
        "conductor gates: guardkit's toolchain declaration loader is not "
        "importable in this interpreter (tried %s) — no declaration can be "
        "read, so the gate set answers UNKNOWN (red). Install the guardkit "
        "distribution in the forge image to make declared gates evaluable",
        list(module_candidates),
    )
    return None


# ---------------------------------------------------------------------------
# WHAT THE TESTS THEMSELVES SAID (ruled 2026-09-08)
# ---------------------------------------------------------------------------
#
# Attempt fifteen's merge-ready checkpoint ran the repository's declared
# suite inside its sandbox and it came back "2 failed, 817 passed, 2
# deselected". That last line was the ONLY thing kept: it went on the log
# line and on the decision, and the checkpoint's receipts held a rationale
# and nothing of the run. Which two tests failed, and why, had to be found
# by running the whole suite again by hand.
#
# So both forms of the declared-test runner — the one that runs here and the
# one that runs through a sandbox's sidecar — now keep a bounded piece of the
# command's own output. Two rules hold:
#
#   * THE EXIT CODE IS STILL THE VERDICT. Nothing below decides anything.
#     Reading the output is for the person who has to fix the tests.
#   * NO PYTEST-ONLY PARSER. Test tools write a short summary naming the
#     cases that failed; when such lines are there they are kept first,
#     because they are the answer to "which tests?". When they are not,
#     the last lines of the output are kept instead, which is all any tool
#     can be relied on to give.
#   * ONLY A RUN THAT FAILED IS DESCRIBED AS FAILING (the coach, 2026-09-08).
#     The exit code decides whether the sentence says anything about failing
#     tests at all, so a run that PASSED is never written down — in the log
#     line, on the decision or in the receipts — as a run with failures,
#     however many lines its captured logs begin with the word ERROR.
#   * ONLY SOMETHING THAT LOOKS LIKE A TEST IS NAMED AS ONE (the same coach).
#     A marker line contributes a NAME only when what follows the marker
#     reads like a case identifier: it holds ``::``, a path separator or a
#     dot, the way every test tool writes one. "ERROR shutting down worker
#     pool" is kept in the output and counted, never named, because a stop
#     reason that says a test called "shutting" failed is a false sentence.

#: How much of a declared test's own output is kept as evidence: sixteen
#: kibibytes. Big enough for a short summary and the failure sections under
#: it, small enough to sit on a decision, in a log and in one receipts file
#: without becoming a second copy of the run.
DECLARED_TEST_EVIDENCE_LIMIT_BYTES: int = 16 * 1024

#: How many last lines of the output are kept when the tool wrote no summary
#: lines of its own (and, alongside the summary, as the run's tail).
DECLARED_TEST_EVIDENCE_TAIL_LINES: int = 60

#: How many failing case names the one-sentence detail may list before it
#: says how many more there are. The whole list is in the evidence.
DECLARED_TEST_NAMES_IN_DETAIL: int = 6

#: The first word of a line that names a failing case, across the tools the
#: estate actually declares: pytest writes ``FAILED …`` and ``ERROR …`` in
#: its short summary, other runners write ``FAIL …``, and a TAP producer
#: writes ``not ok …``. Matching is on the line's first word only, so a
#: sentence that merely contains the word is not mistaken for a summary line.
_FAILING_CASE_MARKERS: tuple[str, ...] = ("FAILED", "FAIL", "not ok", "ERROR")

#: ``ERROR`` is the weak one: a test tool writes it in its summary for a case
#: that could not even run, and a captured log writes it for a message that
#: has nothing to do with a case. So when the output has any of the other
#: three, they are what the sentence names, and ``ERROR`` lines are named only
#: when nothing else did. Both kinds are kept in the evidence either way.
_WEAK_FAILING_CASE_MARKERS: tuple[str, ...] = ("ERROR",)

#: Said in place of the lines that did not fit, so nobody reads a cut-off
#: piece of output as the whole of it. There are two of these because a part
#: is cut in two different directions: the tail of the output keeps its END
#: (its earlier lines are the ones that go), while the tool's own list of
#: failing cases keeps its START (its later names are the ones that go).
#: Printing the wrong one sends a reader hunting for a test name at the wrong
#: end of the list, so each direction says exactly what happened to it. The
#: size named is the size actually kept, not the budget it was cut to.
_EVIDENCE_TRUNCATED_MARKER_KEEPING_THE_END: str = (
    "[… earlier output dropped: only the last {kept} bytes of this part are "
    "kept, of {whole} the command printed …]"
)
_EVIDENCE_TRUNCATED_MARKER_KEEPING_THE_START: str = (
    "[… later output dropped: only the first {kept} bytes of this part are "
    "kept, of {whole} the command printed …]"
)


class DeclaredTestDetail(str):
    """The one sentence about a declared test run, with the run kept on it.

    It IS the sentence: an ordinary string, equal to the string the runners
    have always returned, so every caller that unpacks ``(exit_code, detail)``
    and prints or matches it sees exactly what it saw before. What it adds is
    two attributes for the caller that keeps evidence — the gate-set reader:

    * ``evidence`` — the bounded text of what the command printed.
    * ``failing_cases`` — the case names the tool's own summary lines named,
      in the order it wrote them, empty when it wrote none and empty on any
      run that exited zero: a run that passed has no failing cases, whatever
      its output looks like.
    * ``command`` — the declared command that was run, verbatim, and
      ``exit_code`` — what it answered. Added 2026-09-08 for the document a
      red checkpoint hands the review seat: "these tests failed" is not
      actionable without "this is what was run, and this is what it said".
      ``None`` for a run that never started.

    Carrying them on the sentence rather than widening the runners' return
    means the two runner seams keep the shape every existing caller and every
    injected test double already speaks; a runner that returns a plain string
    simply has no evidence, and the reader says so by keeping none.
    """

    __slots__ = ("evidence", "failing_cases", "command", "exit_code")

    # Named for the type checker as well as for the reader: the four things
    # a slot holds, said once, so nothing has to be silenced below.
    evidence: str
    failing_cases: "tuple[str, ...]"
    command: str
    exit_code: "int | None"

    def __new__(
        cls,
        sentence: str,
        *,
        evidence: str = "",
        failing_cases: "tuple[str, ...]" = (),
        command: str = "",
        exit_code: "int | None" = None,
    ) -> "DeclaredTestDetail":
        detail = super().__new__(cls, sentence)
        detail.evidence = evidence
        detail.failing_cases = tuple(failing_cases)
        detail.command = command
        detail.exit_code = exit_code
        return detail


def _both_streams(stdout: Any, stderr: Any) -> str:
    """The command's whole output, standard output first, then errors.

    A test tool writes its summary on one stream and a crash on the other,
    and which is which differs by tool, so the evidence reads both. Only the
    one-line sentence beside it keeps the single stream it always kept.
    """
    parts = [str(stream or "") for stream in (stdout, stderr)]
    return "\n".join(part for part in parts if part.strip())


def _names_a_failing_case(line: str) -> bool:
    """Is this one of the tool's own lines naming a case that failed?"""
    stripped = line.strip()
    for marker in _FAILING_CASE_MARKERS:
        if stripped == marker or stripped.startswith(marker + " "):
            return True
    return False


def _failing_case_name(line: str) -> str:
    """The case name out of one summary line, or ``""``.

    ``FAILED tests/users/test_router.py::TestX::test_y - assert 404 == 200``
    is the shape pytest writes and ``FAIL  suite/case (0.2s)`` the shape
    others do: the name is the first thing after the marker word, up to the
    tool's own separator. Nothing here is required to succeed — a line whose
    shape is not understood keeps its whole self in the evidence and simply
    contributes no name to the sentence.
    """
    stripped = line.strip()
    for marker in _FAILING_CASE_MARKERS:
        if stripped.startswith(marker + " "):
            rest = stripped[len(marker) :].strip()
            rest = rest.split(" - ")[0].split(" — ")[0].strip()
            name = rest.split()[0].strip(",;:") if rest.split() else ""
            return name
    return ""


def _looks_like_a_case_identifier(name: str) -> bool:
    """Does this word, taken from a marker line, read like a test's name?

    Test tools name a case with a path, a module or a dotted name:
    ``tests/users/test_router.py::TestX::test_y``, ``suite/login/can-sign-in``,
    ``app.tests.CartTest``. Ordinary prose does not. So a name counts only
    when it carries one of those joins — ``::``, a path separator or a dot —
    and starts the way an identifier or a path starts.

    The rule is deliberately blunt, and it errs towards saying no: a line
    whose name is refused is still kept, whole, in the evidence, and still
    counted in the sentence. What it prevents is the opposite mistake, which
    a person actually reads: a log line such as "ERROR shutting down worker
    pool" turning into a failing test called "shutting" in the reason a
    journey stopped.
    """
    if not name:
        return False
    first = name[0]
    if not (first.isalnum() or first == "_"):
        return False
    return "::" in name or "/" in name or "." in name


def _keep_within(text: str, budget_bytes: int, *, keep_end: bool) -> str:
    """Trim ``text`` to ``budget_bytes``, saying plainly that it was trimmed.

    ``keep_end`` says which end survives, and the sentence left behind says
    the same thing in words: keeping the end means the earlier output went,
    keeping the start means the later output went. The sentence also names
    the size that really is kept, which is a little under the budget because
    the sentence itself has to fit inside it.
    """
    whole = len(text.encode("utf-8", errors="replace"))
    if whole <= budget_bytes:
        return text
    template = (
        _EVIDENCE_TRUNCATED_MARKER_KEEPING_THE_END
        if keep_end
        else _EVIDENCE_TRUNCATED_MARKER_KEEPING_THE_START
    )
    # Room is measured against the longest the sentence could be — the kept
    # size is never larger than the budget, so no later wording of it can be
    # longer than this one and overrun what was reserved for it.
    longest = template.format(kept=max(budget_bytes, 0), whole=whole)
    room = max(budget_bytes - len(longest.encode("utf-8")) - 1, 0)
    encoded = text.encode("utf-8", errors="replace")
    piece = (encoded[-room:] if keep_end else encoded[:room]).decode(
        "utf-8", errors="replace"
    )
    # A cut through the middle of a multi-byte character becomes a
    # replacement character, which can be wider than the bytes it stands
    # for; drop from the cut end until the piece really does fit.
    while piece and len(piece.encode("utf-8", errors="replace")) > room:
        piece = piece[1:] if keep_end else piece[:-1]
    kept = len(piece.encode("utf-8", errors="replace"))
    marker = template.format(kept=kept, whole=whole)
    return f"{marker}\n{piece}" if keep_end else f"{piece}\n{marker}"


def summarise_declared_test_output(
    output: str,
    *,
    limit_bytes: int = DECLARED_TEST_EVIDENCE_LIMIT_BYTES,
    tail_lines: int = DECLARED_TEST_EVIDENCE_TAIL_LINES,
) -> str:
    """The bounded evidence kept from one declared test run.

    Two parts, in the order a person wants them: the tool's own lines naming
    what failed, when it wrote any, and then the last lines of the output,
    which is where every test tool puts its own summing up. A green run has
    no failing-case lines, so its evidence is simply its last lines — small.

    The whole is bounded by ``limit_bytes``. The naming lines are kept first
    and the tail is trimmed from its front, because the tail's own end is the
    part worth keeping; a naming list long enough to overrun its own half of
    the budget is trimmed the other way, keeping the first names. Either way
    the piece that was dropped is said in plain words, in the direction it
    really went, where it went. Never raises: evidence that could not be summarised is not
    a reason to lose a verdict.
    """
    text = (output or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        return ""
    lines = text.split("\n")
    named = [line.strip() for line in lines if _names_a_failing_case(line)]
    tail = lines[-max(tail_lines, 1) :]

    parts: list[str] = []
    if named:
        # Said flatly, because this text is kept for a GREEN run as well:
        # these are the lines that begin with one of the marker words, which
        # on a failing run are the tool's own list of what failed and on a
        # passing run may be nothing more than its captured logs.
        summary = (
            "the lines the test command wrote that begin with FAILED, FAIL, "
            "ERROR or not ok:\n" + "\n".join(named)
        )
        parts.append(_keep_within(summary, max(limit_bytes // 2, 0), keep_end=False))
    spent = sum(len(part.encode("utf-8", errors="replace")) + 2 for part in parts)
    tail_block = f"the last {len(tail)} lines of the output:\n" + "\n".join(tail)
    parts.append(
        _keep_within(tail_block, max(limit_bytes - spent, 0), keep_end=True)
    )
    return "\n\n".join(part for part in parts if part.strip())


def failing_cases_in_output(output: str) -> "tuple[str, ...]":
    """The case names the tool's own summary lines named, in its own order.

    The strong markers first; the weak one (``ERROR``) only when they named
    nothing, so a run whose captured logs are full of error messages does not
    turn them into a list of failing tests.

    A marker line contributes a name only when that name reads like a case
    identifier (:func:`_looks_like_a_case_identifier`). Every other marker
    line keeps its whole self in the evidence and is counted by
    :func:`failure_lines_in_output`; what it must never do is put a word out
    of an ordinary sentence into a list of failing tests.
    """
    strong: list[str] = []
    weak: list[str] = []
    for line in (output or "").replace("\r\n", "\n").split("\n"):
        if not _names_a_failing_case(line):
            continue
        name = _failing_case_name(line)
        if not _looks_like_a_case_identifier(name):
            continue
        stripped = line.strip()
        is_weak = any(
            stripped == marker or stripped.startswith(marker + " ")
            for marker in _WEAK_FAILING_CASE_MARKERS
        )
        into = weak if is_weak else strong
        if name not in into:
            into.append(name)
    return tuple(strong or weak)


def failure_lines_in_output(output: str) -> int:
    """How many lines the tool wrote that begin with one of the markers.

    What the sentence falls back to when a failing run's marker lines name
    nothing that looks like a test: how many such lines there are, so a
    person knows the kept output has something to read, and the names are
    left to that output rather than invented here.
    """
    return sum(
        1
        for line in (output or "").replace("\r\n", "\n").split("\n")
        if _names_a_failing_case(line)
    )


def _names_the_failing_cases(names: "tuple[str, ...]") -> str:
    """``"2 failed: a, b"`` — the half-sentence a person reads first.

    At most :data:`DECLARED_TEST_NAMES_IN_DETAIL` names, then how many more
    there are; the whole list is always in the evidence.
    """
    listed = ", ".join(names[:DECLARED_TEST_NAMES_IN_DETAIL])
    more = len(names) - DECLARED_TEST_NAMES_IN_DETAIL
    return f"{len(names)} failed: {listed}" + (
        f", and {more} more" if more > 0 else ""
    )


def _counts_the_failure_lines(count: int) -> str:
    """``"the command wrote 3 lines about failures …"`` — the honest fallback.

    Used when a run that failed wrote marker lines but none of them named
    anything shaped like a test. Saying how many there are is true and
    useful; naming the words out of them would not be.
    """
    lines = "line" if count == 1 else "lines"
    return (
        f"the command wrote {count} {lines} about failures without naming a "
        "test in them; the lines are in the kept output"
    )


def _detail_with_the_run_kept(
    sentence: str, output: str, *, exit_code: "int | None", command: str = ""
) -> DeclaredTestDetail:
    """One sentence about the run, with the run's own evidence on it.

    **The exit code decides whether this sentence mentions failures at all.**
    A run that exited zero passed, whatever its output looks like, so nothing
    about failing tests is added to its sentence and it carries no failing
    case names — the sentence a green run gets is the sentence it has always
    got, byte for byte. Only a run whose exit code says it failed is
    described as having failed.

    When such a run's tool named the cases that failed, the sentence says how
    many and which — that is the word a person reads on the RED log line, on
    the decision and, through the failing-gate name, in the reason the journey
    stops with. It lists at most
    :data:`DECLARED_TEST_NAMES_IN_DETAIL` of them and says how many more
    there are; all of them are in the evidence. When the tool wrote lines
    about failures but named nothing that looks like a test, the sentence
    says how many such lines it wrote and leaves them to the kept output.
    """
    failed = exit_code is not None and exit_code != 0
    try:
        evidence = summarise_declared_test_output(output)
        names = failing_cases_in_output(output) if failed else ()
    except Exception as exc:  # noqa: BLE001 — evidence never costs a verdict
        logger.warning(
            "conductor gates: the declared test command's output could not be "
            "summarised (%s: %s), so this run keeps no evidence; the exit "
            "code is still the verdict",
            type(exc).__name__,
            exc,
        )
        return DeclaredTestDetail(
            sentence, command=command, exit_code=exit_code
        )
    if names:
        sentence = f"{sentence} — {_names_the_failing_cases(names)}"
    elif failed:
        wrote = failure_lines_in_output(output)
        if wrote:
            sentence = f"{sentence} — {_counts_the_failure_lines(wrote)}"
    return DeclaredTestDetail(
        sentence,
        evidence=evidence,
        failing_cases=names,
        command=command,
        exit_code=exit_code,
    )


def _run_declared_command(
    *, command: str, cwd: "Path | str", timeout_seconds: int
) -> "tuple[int | None, str]":
    """Run one declared command, bounded. ``(exit_code, detail)``.

    **THE EXIT CODE IS THE VERDICT** (guardkit's own law, design §B.4).
    Nothing here parses stdout to decide anything; the returned detail is
    for a human reading the decision back, never for the verdict.

    A timeout, or a command that could not be started at all, answers
    ``None`` — which the caller turns into UNKNOWN rather than into a
    pass or a fail it did not observe.

    Runs through the shell because a declaration is a *command line*
    (``npm test``, ``uv run pytest -q``), which is what the repo owner
    wrote and what guardkit's own executor runs. The command comes from
    the repo's checked-in config, not from a message.
    """
    try:
        completed = subprocess.run(  # noqa: S602 — the repo's own declared command
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, (
            f"the declared test command {command!r} did not finish within "
            f"{timeout_seconds}s in {cwd}"
        )
    except Exception as exc:  # noqa: BLE001 — could not run is not could not pass
        return None, (
            f"the declared test command {command!r} could not be started in "
            f"{cwd}: {type(exc).__name__}: {exc}"
        )
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return completed.returncode, _detail_with_the_run_kept(
        f"`{command}` exited {completed.returncode} in {cwd}"
        + (f" — last line: {tail[-1]}" if tail else ""),
        _both_streams(completed.stdout, completed.stderr),
        exit_code=completed.returncode,
        command=command,
    )


# ---------------------------------------------------------------------------
# THE SAME GATE SET, READ AND RUN WHERE THE REPOSITORY LIVES (rule 88)
# ---------------------------------------------------------------------------
#
# Found by L3a's second coach, 2026-09-07 21:02Z: the reader above reads the
# repository's declared toolchain from a checkout on the host and runs the
# declared test command in a journey worktree on the host. For a repository
# that has a sandbox NEITHER EXISTS THERE — the clone and the worktrees are
# inside the sandbox — so the reader would degrade to UNKNOWN, which is red,
# and a sandbox repository's fix journey could never publish a merge card.
# These two functions are the same two acts, done through that repository's
# own deploy sidecar. A repository without a sandbox never reaches them.

#: How long reading one small file out of the clone may take.
SANDBOX_TOOLCHAIN_READ_TIMEOUT_S: float = 30.0

#: How much longer than the declared command's own wall the HTTP read waits,
#: so the sidecar's timeout always fires before the socket gives up.
SANDBOX_TEST_HTTP_MARGIN_S: float = 30.0


def load_declared_toolchain_from_sandbox(
    repo_root: "Path | str",
    *,
    sandbox: Any,
    repo: str,
    branch: str = JOURNEY_BASE_REF,
    post: Callable[..., Any] | None = None,
) -> Any | None:
    """Read ``.guardkit/config.yaml`` out of the sandbox's clone (rule 88).

    Same law as the in-container reader keeps: the declaration is read from
    the CANONICAL tree — ``main`` in the factory's own clone — never from the
    worktree the fix journey has been editing, because a build that could
    rewrite ``toolchain.test`` to ``true`` could green itself. The file comes
    back over the sidecar's existing read-file route and is handed to
    guardkit's OWN loader, exactly as
    :func:`load_declared_toolchain` does, so forge still forms no second
    opinion about what a repository declared.

    ``repo_root`` is the clone's path INSIDE the sandbox — the same path the
    repository map names — and it is used to say, in every sentence a person
    reads, which file was being read and where it lives.

    Returns ``None`` — never raises — when the sidecar cannot be reached or
    refuses, when the file is not on the branch, or when the block declares
    nothing. Every one of those is an honest UNKNOWN upstream, which the
    merge-ready checkpoint treats as RED.
    """
    import tempfile

    from forge.deploy_sidecar.service import GIT_READ_FILE_ROUTE
    from forge.planning.sidecar_git_runner import _urllib_post

    sender = post if post is not None else _urllib_post
    url = f"{str(sandbox.sidecar_url).rstrip('/')}{GIT_READ_FILE_ROUTE}"
    declaration_file = f"{Path(repo_root)}/.guardkit/config.yaml"
    body = {"repo": repo, "branch": branch, "file_path": ".guardkit/config.yaml"}
    try:
        status, decoded = sender(url, body, SANDBOX_TOOLCHAIN_READ_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — could not read is not could not pass
        logger.error(
            "conductor gates: the sidecar in sandbox %s could not be reached "
            "at %s to read %s (%s's declared toolchain) (%s: %s) — the gate "
            "set answers UNKNOWN, which is red",
            getattr(sandbox, "name", "?"),
            url,
            declaration_file,
            repo,
            type(exc).__name__,
            exc,
        )
        return None
    answer = decoded if isinstance(decoded, dict) else {}
    content = answer.get("content")
    if status != 200 or not isinstance(content, str) or not content.strip():
        logger.warning(
            "conductor gates: %s could not be read from branch %s of %s's "
            "clone in sandbox %s (HTTP %s): %s — the gate set answers "
            "UNKNOWN, which is red",
            declaration_file,
            branch,
            repo,
            getattr(sandbox, "name", "?"),
            status,
            answer.get("error") or "the file is not on that branch",
        )
        return None
    with tempfile.TemporaryDirectory(prefix="forge-toolchain-") as tmp:
        root = Path(tmp)
        (root / ".guardkit").mkdir(parents=True, exist_ok=True)
        (root / ".guardkit" / "config.yaml").write_text(content, encoding="utf-8")
        return load_declared_toolchain(root)


def run_declared_command_in_sandbox(
    *,
    command: str,
    cwd: "Path | str",
    timeout_seconds: int,
    sandbox: Any,
    repo: str,
    post: Callable[..., Any] | None = None,
) -> "tuple[int | None, str]":
    """Run the declared test command in the sandbox. ``(exit_code, detail)``.

    Same contract as :func:`_run_declared_command`, which the reader already
    depends on: **the exit code is the verdict**, and ``None`` means the
    command could not be run or did not finish — which the caller turns into
    UNKNOWN rather than into a pass or a fail it did not observe.

    The sidecar checks the command against the repository's own checked-in
    declaration before it runs anything, so what runs in there is the
    repository's own text and nothing composed on this side.
    """
    from forge.planning.sidecar_git_runner import _urllib_post

    sender = post if post is not None else _urllib_post
    url = f"{str(sandbox.sidecar_url).rstrip('/')}/run"
    body = {
        "repo": repo,
        "declared_test": command,
        "cwd": str(cwd),
        "timeout_seconds": float(timeout_seconds),
    }
    try:
        status, decoded = sender(
            url, body, float(timeout_seconds) + SANDBOX_TEST_HTTP_MARGIN_S
        )
    except Exception as exc:  # noqa: BLE001 — could not run is not could not pass
        return None, (
            f"the declared test command {command!r} could not be sent to the "
            f"sidecar in sandbox {getattr(sandbox, 'name', '?')} at {url}: "
            f"{type(exc).__name__}: {exc}"
        )
    answer = decoded if isinstance(decoded, dict) else {}
    if status != 200:
        return None, (
            f"the sidecar in sandbox {getattr(sandbox, 'name', '?')} refused "
            f"to run the declared test command {command!r} (HTTP {status}): "
            f"{answer.get('error') or answer}"
        )
    if answer.get("timed_out") is True:
        return None, (
            f"the declared test command {command!r} did not finish within "
            f"{timeout_seconds}s in {cwd} inside sandbox "
            f"{getattr(sandbox, 'name', '?')}"
        )
    exit_code = answer.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None, (
            f"the sidecar in sandbox {getattr(sandbox, 'name', '?')} answered "
            f"something that is not a test result: {answer!r}"
        )
    ran_in = answer.get("cwd")
    if not isinstance(ran_in, str) or os.path.normpath(ran_in) != os.path.normpath(
        str(cwd)
    ):
        return None, (
            f"the declared test command was asked to run in {cwd} and the "
            f"sidecar in sandbox {getattr(sandbox, 'name', '?')} ran it in "
            f"{ran_in!r}, so the tree that was tested is not the tree the "
            "journey is on"
        )
    tail = (
        (str(answer.get("stderr_tail") or "") or str(answer.get("stdout") or ""))
        .strip()
        .splitlines()
    )
    return exit_code, _detail_with_the_run_kept(
        f"`{command}` exited {exit_code} in {cwd} inside sandbox "
        f"{getattr(sandbox, 'name', '?')}"
        + (f" — last line: {tail[-1]}" if tail else ""),
        # The sidecar answers with both streams; a test tool's summary is
        # usually on stdout and a crash usually on stderr, so the evidence
        # reads both, stdout first. The sentence above keeps the source and
        # the order it always had.
        _both_streams(answer.get("stdout"), answer.get("stderr_tail")),
        exit_code=exit_code,
        command=command,
    )


#: How long the routing law's three small reads inside the sandbox may take.
SANDBOX_STAMPS_READ_TIMEOUT_S: float = 90.0


def read_stamps_in_sandbox(
    *,
    feature_id: str,
    repo_root: "Path | str",
    worktree: "Path | str",
    branch: Any,
    toolchain_green: bool,
    sandbox: Any,
    repo: str,
    candidate_check_before_merge: bool = False,
    post: Callable[..., Any] | None = None,
) -> Any:
    """Step 5 — the routing law's stamped-verifier check, read in the sandbox.

    Found by L3b's coach, 2026-09-08. Steps 3 and 4 of the gates reader were
    routed into the sandbox and this one was left reading the host, where a
    sandbox repository's feature file and gate receipts are not. The check
    then found no stamps, said so, had no effect, and a GREEN merge card went
    out with the routing law silently not applied — a check that could not run
    turned into a card, which is the one direction this reader must never fail
    in.

    So the three reads happen in the sandbox, over one route, and the DECISION
    is the same pure function the in-container leg uses
    (:func:`forge.pipeline.routing_stamps.evaluate_stamps`): the stamps from
    the canonical branch of the clone, the newest results envelope under the
    journey worktree, and the branch's last code commit time. Anything that
    stops those being read — an unreachable sidecar, a refusal, an answer that
    is not evidence — is UNREADABLE, which is UNKNOWN, which is no card.
    """
    from datetime import datetime

    from forge.deploy_sidecar.service import STAMPS_EVIDENCE_ROUTE
    from forge.pipeline.routing_stamps import (
        HISTORY_RELATIVE_PATH,
        Envelope,
        StampsRead,
        evaluate_stamps,
        feature_yaml_relative_path,
        parse_scenario_stamps,
    )
    from forge.planning.sidecar_git_runner import _urllib_post

    history_dir = Path(worktree) / HISTORY_RELATIVE_PATH
    where = Path(repo_root) / feature_yaml_relative_path(feature_id)
    name = getattr(sandbox, "name", "?")

    def _unreadable(reason: str) -> Any:
        logger.error(
            "conductor gates: the routing law's evidence for %s could not be "
            "read in sandbox %s — %s. The gate set answers UNKNOWN, which is "
            "red: no merge card",
            feature_id,
            name,
            reason,
        )
        return evaluate_stamps(
            StampsRead(path=where, present=True, error=reason),
            toolchain_green=toolchain_green,
            envelope=None,
            code_commit_time=None,
            history_dir=history_dir,
            feature_id=feature_id,
        )

    sender = post if post is not None else _urllib_post
    url = f"{str(sandbox.sidecar_url).rstrip('/')}{STAMPS_EVIDENCE_ROUTE}"
    body = {
        "repo": repo,
        "feature_id": feature_id,
        "worktree": str(worktree),
        "branch": str(branch) if branch else None,
    }
    try:
        status, decoded = sender(url, body, SANDBOX_STAMPS_READ_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — could not read is not could not pass
        return _unreadable(
            f"the sidecar in sandbox {name} could not be reached at {url} "
            f"({type(exc).__name__}: {exc})"
        )
    answer = decoded if isinstance(decoded, dict) else {}
    if status != 200:
        return _unreadable(
            f"the sidecar in sandbox {name} refused the request (HTTP "
            f"{status}): {answer.get('error') or answer}"
        )

    feature_yaml = answer.get("feature_yaml")
    if not isinstance(feature_yaml, dict):
        return _unreadable(
            f"the sidecar in sandbox {name} answered something that is not "
            f"the routing law's evidence: {answer!r}"
        )
    said_path = feature_yaml.get("path")
    at = Path(said_path) if isinstance(said_path, str) and said_path else where
    text = feature_yaml.get("text")
    if not feature_yaml.get("present") or not isinstance(text, str):
        # The feature carries no plan of record on the canonical branch — the
        # same answer an absent file gives on this side, and the same
        # consequence: the stamped-verifier check is not enforced for it.
        stamps_read = StampsRead(path=at, present=False)
    else:
        stamps_read = parse_scenario_stamps(text, path=at)

    envelope: Any = None
    raw_envelope = answer.get("envelope")
    if raw_envelope is not None:
        if not isinstance(raw_envelope, dict) or not isinstance(
            raw_envelope.get("run_id"), str
        ):
            return _unreadable(
                f"the sidecar in sandbox {name} answered with something that "
                f"is not a results envelope: {raw_envelope!r}"
            )
        started_raw = raw_envelope.get("started")
        started = None
        if isinstance(started_raw, str) and started_raw.strip():
            try:
                started = datetime.fromisoformat(started_raw.strip())
            except ValueError:
                started = None
        gates = raw_envelope.get("gates")
        envelope = Envelope(
            path=Path(str(raw_envelope.get("path") or history_dir)),
            run_id=str(raw_envelope.get("run_id")),
            verdict=str(raw_envelope.get("verdict") or ""),
            started=started,
            gates=dict(gates) if isinstance(gates, dict) else {},
            feature_id=raw_envelope.get("feature_id"),
        )

    commit_time = None
    raw_commit = answer.get("code_commit_time")
    if isinstance(raw_commit, str) and raw_commit.strip():
        try:
            commit_time = datetime.fromisoformat(raw_commit.strip())
        except ValueError:
            commit_time = None

    return evaluate_stamps(
        stamps_read,
        toolchain_green=toolchain_green,
        envelope=envelope,
        code_commit_time=commit_time,
        history_dir=history_dir,
        feature_id=feature_id,
        candidate_check_before_merge=candidate_check_before_merge,
    )


#: Where a repository keeps the deploy profile the deploy stage reads.
DEPLOY_PROFILE_RELATIVE_PATH = Path("deploy") / "profile.yaml"


def candidate_is_checked_before_the_merge(config: Any, repo_root: "Path | str") -> bool:
    """Does this repository's merge run a live gate on a candidate first?

    THREE facts, all read the way the deploy stage reads them
    (:mod:`forge.pipeline.merge_executor`'s deploy dispatcher and
    :class:`forge.deploy.stage.DeployStageRunner`):

    1. the deploy settings put the stage's docker-touching scripts on a
       sidecar (``deploy.execution_surface == "sidecar"``);
    2. the repository's own ``deploy/profile.yaml`` carries a ``candidate:``
       block, which is what makes the stage stand the build up BEFORE the
       merge rather than after; and
    3. that same profile carries a ``live_gate:`` block AND the deploy
       settings leave the live gate on (``deploy.run_live_gate``).

    The third fact is the one that makes the deferral honest. With a candidate
    block and no live gate the stage stands the candidate up, takes its health
    checks as the whole check and writes ``verdict: pass`` without running a
    gate at all (``stage.py``: ``if self._config.run_live_gate: ... else:``),
    and the merge then proceeds — so a check deferred to it would never be
    run by anybody. Deferring is only ever allowed to something that runs.

    (A fourth setting, ``deploy.enabled``, is deliberately NOT read here: when
    the stage is disabled the merge press refuses the merge outright — "the
    deploy stage is disabled" — so nothing lands unchecked either way.)

    True means the checkpoint may defer a stamped check whose evidence does not
    exist yet, because the merge press runs its own check of the candidate
    before anything lands. Never raises: this only decides whether a check is
    deferred or called missing, and missing — today's answer — is the safe one,
    so anything unreadable is False.
    """
    deploy_settings = getattr(config, "deploy", None)
    if str(getattr(deploy_settings, "execution_surface", "")) != "sidecar":
        return False
    if not bool(getattr(deploy_settings, "run_live_gate", False)):
        logger.info(
            "conductor gates: the deploy settings have run_live_gate off, so a "
            "candidate is stood up and merged without a gate verdict — the "
            "checkpoint defers nothing for %s",
            repo_root,
        )
        return False
    try:
        from forge.deploy.profile import load_deploy_profile

        profile = load_deploy_profile(Path(repo_root) / DEPLOY_PROFILE_RELATIVE_PATH)
    except Exception as exc:  # noqa: BLE001 — unreadable is "no candidate check"
        logger.info(
            "conductor gates: %s has no readable deploy profile (%s: %s), so "
            "the checkpoint assumes its merge does NOT check a candidate first",
            repo_root,
            type(exc).__name__,
            exc,
        )
        return False
    if getattr(profile, "candidate", None) is None:
        return False
    if getattr(profile, "live_gate", None) is None:
        logger.info(
            "conductor gates: %s stands a candidate up but its deploy profile "
            "declares no live gate, so the merge has no gate verdict to give — "
            "the checkpoint defers nothing",
            repo_root,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# THE SPECIFICATION FENCE — reading what the journey's branch changed
# (Rich's ruling, 2026-09-09)
# ---------------------------------------------------------------------------
#
# The checkpoint owns the refusal (see the fence section in
# :mod:`forge.pipeline.merge_ready_checkpoint`); this is where the two things
# it needs are read.
#
# 1. WHICH FILES THIS REPOSITORY CALLS ITS SPECIFICATION. Declared in the
#    place a repository already declares things about its gates —
#    ``.guardkit/config.yaml``, beside the ``toolchain:`` block the
#    merge-ready checks already read — under a ``specification:`` key:
#
#        specification:
#          paths:
#            - "qa/twins/**"
#
#    A repository that declares nothing gets
#    :data:`~forge.pipeline.merge_ready_checkpoint.DEFAULT_SPECIFICATION_PATHS`
#    — the acceptance twins, which is api_test's own shape and the one the
#    incident used. No new file, and no new ceremony: a repository says this
#    once, in the file it already has, or says nothing and takes the default.
#
#    Forge parses this key itself rather than through guardkit's loader,
#    because it is forge's own key and guardkit's loader owns the
#    ``toolchain:`` block alone. The declaration is read from the CANONICAL
#    tree, never from the worktree the journey has been editing — the same law
#    the toolchain declaration is read under, and for the same reason: a
#    branch that could rewrite the declaration could free itself.
#
# 2. WHAT THE BRANCH CHANGED, against its base. Two git diffs. Where the
#    repository lives (rule 88): in this container for a repository with no
#    sandbox, and through the sandbox's own deploy sidecar for one that has.

#: Where a repository declares which of its files are the specification —
#: the same file its toolchain is declared in.
SPECIFICATION_DECLARATION_FILE: str = ".guardkit/config.yaml"

#: The key inside it.
SPECIFICATION_DECLARATION_KEY: str = "specification"

#: How long the two diffs may take on this side.
BRANCH_DIFF_TIMEOUT_SECONDS: float = 120.0

#: How much of either diff is read: 512 KiB, the same bound the sidecar's own
#: route keeps, so the two venues cut at the same place. Past it the reading
#: is UNREADABLE rather than partial — a refusal that missed the line it was
#: looking for would be worse than an honest "this could not be read whole".
BRANCH_DIFF_LIMIT_BYTES: int = 512 * 1024

#: How much longer than the diffs' own wall the HTTP read waits, so the
#: sidecar's timeout always fires before the socket gives up.
SANDBOX_BRANCH_DIFF_HTTP_MARGIN_S: float = 30.0


def specification_paths_in(text: Any) -> "tuple[str, ...] | None":
    """The ``specification: paths:`` list in one ``.guardkit/config.yaml``.

    ``None`` — never a raise — when the file says nothing about it: no such
    key, an empty list, a shape that is not a list of non-empty strings, or
    YAML that will not parse. Every one of those means "this repository
    declares no specification of its own", and the caller applies the
    default. A declaration is only ever read as MORE protection than the
    default or as different protection; it can never be read as none, because
    an unreadable file answers the same as an absent one.
    """
    import yaml

    if not isinstance(text, str) or not text.strip():
        return None
    try:
        loaded = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 — an unparseable file declares nothing
        logger.warning(
            "the specification fence: %s could not be parsed (%s: %s), so this "
            "repository is read as declaring no specification of its own and "
            "the default applies",
            SPECIFICATION_DECLARATION_FILE,
            type(exc).__name__,
            exc,
        )
        return None
    if not isinstance(loaded, dict):
        return None
    block = loaded.get(SPECIFICATION_DECLARATION_KEY)
    if not isinstance(block, dict):
        return None
    raw = block.get("paths")
    if not isinstance(raw, list):
        return None
    paths = tuple(
        entry.strip() for entry in raw if isinstance(entry, str) and entry.strip()
    )
    return paths or None


def load_declared_specification_paths(repo_root: "Path | str") -> "tuple[str, ...] | None":
    """Read the declaration out of the canonical checkout on this side."""
    path = Path(repo_root) / SPECIFICATION_DECLARATION_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.info(
            "the specification fence: %s could not be read (%s), so the "
            "default applies",
            path,
            exc,
        )
        return None
    return specification_paths_in(text)


def load_declared_specification_paths_from_sandbox(
    repo_root: "Path | str",
    *,
    sandbox: Any,
    repo: str,
    branch: str = JOURNEY_BASE_REF,
    post: Callable[..., Any] | None = None,
) -> "tuple[str, ...] | None":
    """Read the same declaration out of the sandbox's clone (rule 88).

    Same route, same canonical branch and same never-raise contract as
    :func:`load_declared_toolchain_from_sandbox`. Anything that stops the
    file being read is "this repository declares no specification of its
    own", and the default applies — which protects more, not less.
    """
    from forge.deploy_sidecar.service import GIT_READ_FILE_ROUTE
    from forge.planning.sidecar_git_runner import _urllib_post

    sender = post if post is not None else _urllib_post
    url = f"{str(sandbox.sidecar_url).rstrip('/')}{GIT_READ_FILE_ROUTE}"
    body = {
        "repo": repo,
        "branch": branch,
        "file_path": SPECIFICATION_DECLARATION_FILE,
    }
    try:
        status, decoded = sender(url, body, SANDBOX_TOOLCHAIN_READ_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — unreachable is "declares nothing"
        logger.info(
            "the specification fence: the sidecar in sandbox %s could not be "
            "reached at %s to read %s/%s (%s: %s), so the default applies",
            getattr(sandbox, "name", "?"),
            url,
            repo_root,
            SPECIFICATION_DECLARATION_FILE,
            type(exc).__name__,
            exc,
        )
        return None
    answer = decoded if isinstance(decoded, dict) else {}
    content = answer.get("content")
    if status != 200 or not isinstance(content, str):
        return None
    return specification_paths_in(content)


def read_branch_changes(
    *, worktree: "Path | str", base: str
) -> "tuple[str, str, str | None]":
    """What this branch changed, read here. ``(name_status, patch, error)``.

    ``error`` is one plain sentence when the reading did not happen, and the
    fence turns that into a refusal — never into "nothing changed".

    THE ONE CASE THAT IS NOT AN ERROR: a path that is not the root of a git
    tree (no ``.git`` in it). A path like that has no branch and no commits,
    so nothing in it can reach main and there is nothing for the fence to
    read. It answers empty, not unreadable.
    """
    root = Path(worktree)
    if not (root / ".git").exists():
        logger.info(
            "the specification fence: %s is not the root of a git tree, so it "
            "carries no branch and no commits and there is nothing to read",
            root,
        )
        return "", "", None

    from forge.pipeline.merge_ready_checkpoint import APPROVAL_MARKER

    span = f"{base}...HEAD"

    def _git(*args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-c", "core.quotepath=false", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=BRANCH_DIFF_TIMEOUT_SECONDS,
            check=False,
        )

    try:
        names = _git("diff", "--name-status", "-M", "-z", span)
        if names.returncode != 0:
            detail = (names.stderr or names.stdout or "").strip() or "<no output>"
            return "", "", (
                f"git could not read the changed files of {span} in {root} "
                f"(it exited {names.returncode}): {detail}"
            )
        patch = _git(
            "diff", "-U0", "--no-color", "--no-renames", f"-G{APPROVAL_MARKER}", span
        )
        if patch.returncode != 0:
            detail = (patch.stderr or patch.stdout or "").strip() or "<no output>"
            return "", "", (
                f"git could not read the approval lines of {span} in {root} "
                f"(it exited {patch.returncode}): {detail}"
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", "", (
            f"reading {span} in {root} raised {type(exc).__name__}: {exc}"
        )
    name_status = names.stdout or ""
    approval_patch = patch.stdout or ""
    if (
        len(name_status.encode("utf-8")) > BRANCH_DIFF_LIMIT_BYTES
        or len(approval_patch.encode("utf-8")) > BRANCH_DIFF_LIMIT_BYTES
    ):
        return "", "", (
            f"what {root} changed against {base} is too big to read whole, so "
            "a change to the specification could have been past the end of it"
        )
    return name_status, approval_patch, None


def read_branch_changes_in_sandbox(
    *,
    worktree: "Path | str",
    base: str,
    sandbox: Any,
    repo: str,
    post: Callable[..., Any] | None = None,
) -> "tuple[str, str, str | None]":
    """The same two diffs, run where the repository lives (rule 88).

    Same ``(name_status, patch, error)`` contract as
    :func:`read_branch_changes`. Anything that stops the reading is an
    ``error`` sentence, which the fence turns into a refusal: for a sandbox
    repository the tree really is there and git really does run, so a failure
    means something is wrong rather than that there was nothing to see.
    """
    from forge.deploy_sidecar.service import GIT_WORKTREE_CHANGED_FILES_ROUTE
    from forge.planning.sidecar_git_runner import _urllib_post

    sender = post if post is not None else _urllib_post
    name = getattr(sandbox, "name", "?")
    url = f"{str(sandbox.sidecar_url).rstrip('/')}{GIT_WORKTREE_CHANGED_FILES_ROUTE}"
    body = {"repo": repo, "path": str(worktree), "base": base}
    try:
        status, decoded = sender(
            url, body, BRANCH_DIFF_TIMEOUT_SECONDS + SANDBOX_BRANCH_DIFF_HTTP_MARGIN_S
        )
    except Exception as exc:  # noqa: BLE001 — could not read is not "nothing"
        return "", "", (
            f"the sidecar in sandbox {name} could not be reached at {url} to "
            f"read what {worktree} changed against {base}: "
            f"{type(exc).__name__}: {exc}"
        )
    answer = decoded if isinstance(decoded, dict) else {}
    if status != 200:
        return "", "", (
            f"the sidecar in sandbox {name} refused to read what {worktree} "
            f"changed against {base} (HTTP {status}): "
            f"{answer.get('error') or answer}"
        )
    if answer.get("truncated") is True:
        return "", "", (
            f"what {worktree} changed against {base} is too big to read whole "
            f"in sandbox {name}, so a change to the specification could have "
            "been past the end of it"
        )
    names = answer.get("name_status")
    patch = answer.get("approval_patch")
    if not isinstance(names, str) or not isinstance(patch, str):
        return "", "", (
            f"the sidecar in sandbox {name} answered something that is not a "
            f"diff: {answer!r}"
        )
    return names, patch, None


def make_specification_fence(
    *,
    pool: Any,
    config: Any,
    declaration_loader: Callable[..., Any] | None = None,
    sandbox_declaration_loader: Callable[..., Any] | None = None,
    changes_reader: Callable[..., Any] | None = None,
    sandbox_changes_reader: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """Build the merge-ready checkpoint's REAL specification fence.

    Answers ``(*, build_id, branch) -> SpecificationFenceReport``. In order:

    1. Read the build row. No row, or no recorded worktree → a refusal
       saying so: the branch this build made cannot be found, so nothing can
       say its specification is untouched.
    2. Work out the branch's base — the branch the row was queued on, or
       ``main`` (:func:`forge.cli._conductor_worktree.journey_base_ref`), the
       same one rule the commit probe and the worktree writer use.
    3. Read this repository's declaration of its own specification from the
       CANONICAL tree; a repository that declares none takes the default.
    4. Read what the branch changed, in this container or through the
       repository's sandbox.
    5. Hand both to
       :func:`~forge.pipeline.merge_ready_checkpoint.judge_branch_changes`,
       which holds the two rules and is the same code either way.

    Every seam is injectable so the tests drive real git in a real temporary
    repository without a daemon, a sandbox or a network.
    """
    from forge.config.sandboxes import sandbox_for
    from forge.pipeline.merge_ready_checkpoint import (
        DEFAULT_SPECIFICATION_PATHS,
        judge_branch_changes,
        parse_changed_approval_lines,
        parse_changed_files,
        unreadable_branch_changes,
    )

    _load = declaration_loader or load_declared_specification_paths
    _sandbox_load = (
        sandbox_declaration_loader or load_declared_specification_paths_from_sandbox
    )
    _read = changes_reader or read_branch_changes
    _sandbox_read = sandbox_changes_reader or read_branch_changes_in_sandbox

    def read_fence(*, build_id: str, branch: Any = None) -> Any:
        try:
            row = pool.get_build_row(build_id)
        except Exception as exc:  # noqa: BLE001 — an unread row is not clear
            return unreadable_branch_changes(
                f"reading the build row raised {type(exc).__name__}: {exc}"
            )
        if row is None:
            return unreadable_branch_changes(
                f"there is no builds row for build_id={build_id!r}"
            )
        worktree = getattr(row, "worktree_path", None)
        if not worktree or not str(worktree).strip():
            return unreadable_branch_changes(
                f"build_id={build_id!r} has no recorded worktree_path, so "
                "there is no branch to read"
            )

        from forge.cli._conductor_worktree import journey_base_ref

        base = journey_base_ref(getattr(row, "branch", None))
        repo_key = str(getattr(row, "repo", "") or "")
        entry = sandbox_for(config, repo_key)

        # WHICH FILES THIS REPOSITORY CALLS ITS SPECIFICATION — read from the
        # canonical tree, never from the branch. A repository forge cannot
        # locate, or one that declares nothing, gets the default, which
        # protects more rather than less.
        paths = getattr(getattr(config, "planning", None), "target_repo_paths", None)
        repo_root = (paths or {}).get(repo_key)
        declared: Any = None
        if repo_root:
            try:
                declared = (
                    _sandbox_load(repo_root, sandbox=entry, repo=repo_key)
                    if entry is not None
                    else _load(repo_root)
                )
            except Exception as exc:  # noqa: BLE001 — a loader defect is not a hole
                logger.warning(
                    "the specification fence: reading %s's declaration raised "
                    "%s: %s — the default applies",
                    repo_key,
                    type(exc).__name__,
                    exc,
                )
                declared = None
        specification_paths = tuple(declared) if declared else DEFAULT_SPECIFICATION_PATHS

        try:
            names, patch, error = (
                _sandbox_read(
                    worktree=worktree, base=base, sandbox=entry, repo=repo_key
                )
                if entry is not None
                else _read(worktree=worktree, base=base)
            )
        except Exception as exc:  # noqa: BLE001 — a reader defect is not clear
            return unreadable_branch_changes(
                f"reading what the branch changed raised "
                f"{type(exc).__name__}: {exc}"
            )
        if error:
            return unreadable_branch_changes(str(error))

        report = judge_branch_changes(
            changes=parse_changed_files(names),
            approval_lines=parse_changed_approval_lines(patch),
            specification_paths=specification_paths,
        )
        if report.refuses:
            logger.error(
                "the specification fence: build_id=%s on %s — %s (the files "
                "this repository calls its specification: %s)",
                build_id,
                branch or getattr(row, "branch", None),
                report.detail,
                ", ".join(specification_paths),
            )
        else:
            logger.info(
                "the specification fence: build_id=%s — the branch changes "
                "none of the files this repository calls its specification "
                "(%s) and no line recording an approval",
                build_id,
                ", ".join(specification_paths),
            )
        return report

    return read_fence


def make_gates_green_reader(
    *,
    pool: Any,
    config: Any,
    declaration_loader: Callable[..., Any] | None = None,
    command_runner: Callable[..., Any] | None = None,
    repo_root_reader: Callable[[str], Any] | None = None,
    stamps_leg: Callable[..., Any] | None = None,
    sandbox_declaration_loader: Callable[..., Any] | None = None,
    sandbox_command_runner: Callable[..., Any] | None = None,
    sandbox_stamps_leg: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """Build the merge-ready checkpoint's REAL ``gates_green_reader``.

    Until now this was hardcoded ``None`` in the production composition,
    which meant every fix journey read UNKNOWN, treated it as red, and
    could never publish a merge card. That was a *safe* posture and an
    honest one — but it was also a wall, and this is the door.

    What it does, in the order it decides:

    1. Read the build row. No row, no worktree → **UNKNOWN**.
    2. Resolve the target repo's root through the estate's one law for
       that question — ``planning.target_repo_paths[builds.repo]``, the
       same mapping the deploy sidecar and the planning handoff use. An
       unmapped repo → **UNKNOWN**, said plainly. Deliberately NOT
       falling back to the worktree: the declaration is read from the
       CANONICAL tree precisely because the worktree is what the fix
       journey's own agent has been editing, and a build that could
       rewrite ``toolchain.test`` to ``true`` could green itself.
    3. Load the repo's declared ``toolchain.test`` command through
       guardkit's loader. No declaration, or one that declares no test
       command → **UNKNOWN**, logged plainly. This is the honest wall:
       an undeclared repo cannot be gated, and cannot be carded either.
    4. Run that command **in the fix branch's worktree**, bounded by the
       declaration's own ``test_timeout``. **Exit 0 = GREEN.** Any other
       exit code = **RED**. A timeout, or a command that would not
       start = **UNKNOWN**.
    5. **The routing law's close-side check (card Q8/A.2 — the
       ``stamps_satisfied`` leg).** Only reached on a GREEN suite. Read
       the feature's per-scenario ``verifier:`` stamps from the CANONICAL
       repo's ``.guardkit/features/<feature_id>.yaml`` (the plan of
       record, same canonical-not-worktree law as step 2) and ask, home
       by home, whether the promised verifier RAN GREEN for this branch:
       ``toolchain`` rides step 4; ``hurl`` needs the newest F4 envelope
       under the worktree's ``qa/gates/history/`` to be ``pass``, name
       ``hurl-twins`` exit 0, and be newer than the branch's last code
       commit; ``exam``/``probe:*``/``flutter``/``playwright`` need that
       same fresh green envelope to name them; ``operator`` is satisfied
       by declaration but LISTED as attended in the card detail. A
       stamped verifier that did not run is **ABSENT = UNKNOWN**: no
       card, and the detail names the scenario and the missing home in
       plain language. **No stamps at all → not enforced**: the report
       is exactly what step 4 said. See
       :mod:`forge.pipeline.routing_stamps`.

    Every UNKNOWN is red-safe by the checkpoint's own precondition
    ("proven green", never "not proven red"), so every degrade on this
    path fails towards *no card*, never towards a card.

    SANDBOX FIRST (rule 88). Steps 3, 4 and 5 all touch the repository — one
    reads its declaration, one runs its tests, one reads its plan of record
    and its gate receipts — and for a repository that has a sandbox neither
    the clone nor the journey worktree is on this side at all. So for such a
    repository all three go through that sandbox's own deploy sidecar
    (:func:`load_declared_toolchain_from_sandbox`,
    :func:`run_declared_command_in_sandbox` and
    :func:`read_stamps_in_sandbox`), and the canonical-not-worktree law of
    step 2 is kept: both the declaration and the stamps are read from ``main``
    in the clone. Everything else about the decision is identical, and a
    repository without a sandbox is byte for byte what it was.

    Step 5 was left on the host by this lane's first cut, and its coach proved
    what that cost: the stamps could not be found, the check said "this
    feature carries no scenario stamps", and a GREEN card went out with the
    routing law not applied. A check that could not run must never become a
    card, so it runs where the evidence is, and every way of failing to read
    it is UNKNOWN.

    Args:
        pool: The daemon's SQLite persistence facade.
        config: The loaded ``ForgeConfig`` — read only for
            ``planning.target_repo_paths``.
        declaration_loader: ``(repo_root) -> declaration | None``.
            Defaults to :func:`load_declared_toolchain`. Injected so
            tests neither need guardkit installed nor a real repo.
        command_runner: ``(*, command, cwd, timeout_seconds) ->
            (exit_code | None, detail)``. Defaults to
            :func:`_run_declared_command`. Injected so tests are
            subprocess-free.
        repo_root_reader: ``(build_id) -> Path | str | None`` — override
            for step 2.
        sandbox_declaration_loader: ``(repo_root, *, sandbox, repo) ->
            declaration | None`` — step 3 for a repository that has a
            sandbox. Defaults to
            :func:`load_declared_toolchain_from_sandbox`.
        sandbox_command_runner: ``(*, command, cwd, timeout_seconds, sandbox,
            repo) -> (exit_code | None, detail)`` — step 4 for the same.
            Defaults to :func:`run_declared_command_in_sandbox`.
        sandbox_stamps_leg: ``(*, feature_id, repo_root, worktree, branch,
            toolchain_green, sandbox, repo) -> StampsVerdict`` — step 5 for
            the same. Defaults to :func:`read_stamps_in_sandbox`.
        stamps_leg: ``(*, feature_id, repo_root, worktree, branch,
            toolchain_green) -> StampsVerdict`` — step 5. Defaults to
            :func:`forge.pipeline.routing_stamps.make_stamps_leg` over
            the real feature YAML, envelope and git readers. Injected so
            tests drive every home without a repo on disk.
    """
    from forge.pipeline.merge_ready_checkpoint import GateStatus, GatesReport
    from forge.pipeline.routing_stamps import make_stamps_leg

    from forge.config.sandboxes import sandbox_for

    _load = declaration_loader or load_declared_toolchain
    _run = command_runner or _run_declared_command
    _sandbox_load = sandbox_declaration_loader or load_declared_toolchain_from_sandbox
    _sandbox_run = sandbox_command_runner or run_declared_command_in_sandbox
    _sandbox_stamps = sandbox_stamps_leg or read_stamps_in_sandbox
    _stamps = stamps_leg or make_stamps_leg()

    def _default_repo_root(build_id: str) -> Any | None:
        row = pool.get_build_row(build_id)
        repo = getattr(row, "repo", None) if row is not None else None
        if not repo:
            return None
        paths = getattr(getattr(config, "planning", None), "target_repo_paths", None)
        if not paths:
            return None
        return paths.get(repo)

    _repo_root = repo_root_reader or _default_repo_root

    def _unknown(detail: str, build_id: str) -> Any:
        logger.warning(
            "conductor gates: build_id=%s — %s. The gate set answers UNKNOWN, "
            "which the merge-ready checkpoint treats as RED: no merge card "
            "will be published for this journey",
            build_id,
            detail,
        )
        return GatesReport(status=GateStatus.UNKNOWN, detail=detail)

    def _apply_stamps_leg(
        *,
        build_id: str,
        feature_id: str,
        repo_root: Any,
        worktree: Any,
        branch: Any,
        suite_detail: str,
        suite_evidence: str = "",
        stamps: Callable[..., Any] | None = None,
        candidate_check_before_merge: bool = False,
    ) -> Any:
        """Step 5 — the routing law's ``stamps_satisfied`` leg on a GREEN suite.

        ``stamps`` is the leg to ask: the in-container one, or — for a
        repository whose clone and worktrees are inside its sandbox — the one
        that reads the same three pieces of evidence in there. Both answer the
        same verdict type and both fail towards no card.

        ``suite_evidence`` is what the declared suite itself printed, kept
        (bounded) by the runner and carried onto the report so a green run
        leaves its last lines behind too — the receipts of a card are worth
        as much as the receipts of a stop.

        ``candidate_check_before_merge`` is asked of the leg ONLY when it is
        true, so that a repository whose merge does not check a candidate first
        — which is every repository until an operator gives one a sandbox and a
        candidate block — calls the leg with exactly the arguments it has
        always been called with.
        """
        deferring = (
            {"candidate_check_before_merge": True}
            if candidate_check_before_merge
            else {}
        )
        try:
            verdict = (stamps or _stamps)(
                feature_id=feature_id,
                repo_root=repo_root,
                worktree=worktree,
                branch=branch,
                toolchain_green=True,
                **deferring,
            )
        except Exception as exc:  # noqa: BLE001 — a leg defect is not green
            return _unknown(
                f"the declared suite is green ({suite_detail}) but the routing "
                f"law's stamps leg raised {type(exc).__name__}: {exc}, so the "
                "stamped verifiers cannot be proven to have run",
                build_id,
            )
        stamps_detail = getattr(verdict, "detail", "") or ""
        if str(getattr(verdict, "status", "")) == "not-enforced":
            # No stamps on this feature: the leg has NO effect. The report
            # is byte-for-byte what step 4 said (backward compatible), and
            # the log says why nothing more was asked.
            logger.info(
                "conductor gates: build_id=%s — GREEN. %s (%s)",
                build_id,
                suite_detail,
                stamps_detail or "routing law: not enforced",
            )
            return GatesReport(
                status=GateStatus.GREEN,
                detail=str(suite_detail),
                evidence=suite_evidence,
            )
        if getattr(verdict, "blocks_card", False):
            missing = tuple(
                f"routing law: {home} (scenario {title!r})"
                for title, home in (getattr(verdict, "missing", ()) or ())
            ) or ("routing law: scenario stamps unreadable",)
            logger.warning(
                "conductor gates: build_id=%s — the declared suite is GREEN "
                "but the ROUTING LAW reads %s: %s The gate set answers "
                "UNKNOWN, which the merge-ready checkpoint treats as RED: no "
                "merge card will be published for this journey",
                build_id,
                getattr(verdict, "status", "absent"),
                stamps_detail,
            )
            return GatesReport(
                status=GateStatus.UNKNOWN,
                failed_gates=missing,
                detail=(
                    f"{stamps_detail} The declared suite itself is green "
                    f"({suite_detail})."
                ),
                evidence=suite_evidence,
            )
        attended = tuple(getattr(verdict, "attended", ()) or ())
        if attended:
            logger.warning(
                "conductor gates: build_id=%s — ATTENDED scenarios on this "
                "card (operator-stamped, verified by a human, not by the "
                "machinery): %s",
                build_id,
                "; ".join(repr(t) for t in attended),
            )
        deferred_detail = str(getattr(verdict, "deferred_detail", "") or "")
        if deferred_detail:
            logger.info(
                "conductor gates: build_id=%s — %s (this repository's merge "
                "stands the candidate up and runs the live gate on it before "
                "anything lands)",
                build_id,
                deferred_detail,
            )
        logger.info(
            "conductor gates: build_id=%s — GREEN. %s %s",
            build_id,
            suite_detail,
            stamps_detail,
        )
        return GatesReport(
            status=GateStatus.GREEN,
            detail=f"{suite_detail} {stamps_detail}".strip(),
            deferred_detail=deferred_detail,
            evidence=suite_evidence,
        )

    def read_gates(*, build_id: str, branch: Any = None) -> Any:
        row = pool.get_build_row(build_id)
        if row is None:
            return _unknown("no builds row to read a worktree from", build_id)
        # WHERE THE TOOLCHAIN IS READ AND THE SUITE IS RUN (rule 88). For a
        # repository that has a sandbox, both the clone and the journey
        # worktree are inside it, so both happen through its own deploy
        # sidecar. Every other repository takes the path it always took, in
        # this container, with the two seams the tests inject.
        repo_key = str(getattr(row, "repo", "") or "")
        entry = sandbox_for(config, repo_key)
        stamps_here: Callable[..., Any] | None = None
        if entry is None:
            load, run_command = _load, _run
        else:
            def load(repo_root: Any, _entry: Any = entry, _repo: str = repo_key) -> Any:
                return _sandbox_load(repo_root, sandbox=_entry, repo=_repo)

            def run_command(
                *,
                command: str,
                cwd: Any,
                timeout_seconds: int,
                _entry: Any = entry,
                _repo: str = repo_key,
            ) -> Any:
                return _sandbox_run(
                    command=command,
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                    sandbox=_entry,
                    repo=_repo,
                )

            # Step 5 goes in there too (L3b's coach, 2026-09-08): the feature's
            # stamps, the gate receipts and the branch's last code commit are
            # all inside the sandbox, and a check that cannot run must never
            # become a green card.
            def stamps_here(  # type: ignore[misc]
                *,
                feature_id: str,
                repo_root: Any,
                worktree: Any,
                branch: Any,
                toolchain_green: bool,
                candidate_check_before_merge: bool = False,
                _entry: Any = entry,
                _repo: str = repo_key,
            ) -> Any:
                return _sandbox_stamps(
                    feature_id=feature_id,
                    repo_root=repo_root,
                    worktree=worktree,
                    branch=branch,
                    toolchain_green=toolchain_green,
                    sandbox=_entry,
                    repo=_repo,
                    candidate_check_before_merge=candidate_check_before_merge,
                )

        worktree = getattr(row, "worktree_path", None)
        if not worktree:
            return _unknown(
                "the build row carries no worktree_path, so there is nowhere "
                "to run the gate set",
                build_id,
            )
        if entry is None and not Path(worktree).is_dir():
            # A sandbox repository's worktree is inside its sandbox and is not
            # a directory on this side at all; the sidecar checks that it is
            # there before it runs anything, and says so plainly if it is not.
            return _unknown(
                f"the recorded worktree {worktree} is not a directory",
                build_id,
            )

        try:
            repo_root = _repo_root(build_id)
        except Exception as exc:  # noqa: BLE001 — a reader defect is not green
            return _unknown(
                f"resolving the target repo root raised "
                f"{type(exc).__name__}: {exc}",
                build_id,
            )
        if not repo_root:
            return _unknown(
                f"the build's repo {getattr(row, 'repo', None)!r} is not in "
                "planning.target_repo_paths, so the repo's declared toolchain "
                "cannot be located (the declaration is read from the "
                "canonical repo, never from the worktree the fix journey has "
                "been editing)",
                build_id,
            )

        declaration = load(repo_root)
        if declaration is None:
            return _unknown(
                f"{repo_root}/.guardkit/config.yaml declares no toolchain",
                build_id,
            )
        command = getattr(declaration, "test", None)
        if not command:
            return _unknown(
                f"{repo_root}/.guardkit/config.yaml declares a toolchain but "
                "no `test:` command, so there is no verdict-bearing gate to "
                "run",
                build_id,
            )
        timeout_seconds = int(getattr(declaration, "test_timeout", 300) or 300)

        logger.info(
            "conductor gates: build_id=%s running the DECLARED test command "
            "for the merge-ready checkpoint — %r in %s (branch=%s, bound=%ss)",
            build_id,
            command,
            worktree,
            branch,
            timeout_seconds,
        )
        try:
            exit_code, detail = run_command(
                command=command, cwd=worktree, timeout_seconds=timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 — a runner defect is not green
            return _unknown(
                f"running the declared test command raised "
                f"{type(exc).__name__}: {exc}",
                build_id,
            )

        # WHAT THE RUN ITSELF SAID. The runner keeps it on the sentence it
        # returns (:class:`DeclaredTestDetail`); a runner that returns a plain
        # string — every injected test double, and any older seam — simply has
        # none, and the report is what it always was.
        evidence = str(getattr(detail, "evidence", "") or "")
        failing_cases = tuple(getattr(detail, "failing_cases", ()) or ())

        if exit_code is None:
            return _unknown(detail, build_id)
        if exit_code == 0:
            logger.info(
                "conductor gates: build_id=%s — declared suite GREEN. %s",
                build_id,
                detail,
            )
            # THE STAMPS THAT ARE PROVEN AT THE MERGE INSTEAD (ruled
            # 2026-09-08). Five verifier homes have no forge-side runner and a
            # fix journey runs no live gate before this checkpoint, so on a
            # repository whose scenarios are stamped on one of them the leg
            # could only ever say ABSENT and no fix journey could reach its
            # card. Where the merge press stands the candidate up and runs the
            # live gate on it BEFORE anything lands, those checks are deferred
            # to the press rather than called missing — and only there.
            defers = entry is not None and candidate_is_checked_before_the_merge(
                config, repo_root
            )
            return _apply_stamps_leg(
                build_id=build_id,
                feature_id=getattr(row, "feature_id", None) or "",
                repo_root=repo_root,
                worktree=worktree,
                branch=branch or getattr(row, "branch", None),
                suite_detail=detail,
                suite_evidence=evidence,
                stamps=stamps_here,
                candidate_check_before_merge=defers,
            )
        logger.warning(
            "conductor gates: build_id=%s — RED. %s. No merge card is "
            "published; the fix cycle runs BEFORE the merge word",
            build_id,
            detail,
        )
        if evidence:
            logger.warning(
                "conductor gates: build_id=%s — what the declared test "
                "command printed, kept so nobody has to run the suite again "
                "to find out:\n%s",
                build_id,
                evidence,
            )
        # THE FAILING GATE'S NAME CARRIES THE FAILING TESTS. The reason a
        # journey stops with (``conductor_driver._red_gate_reason``) names the
        # failed gates when there are any, so the tests that failed have to be
        # in the name for a person to read them there. With no names to add —
        # a tool that wrote no summary, or an injected runner — the name is
        # the one word it has always been.
        gate_name = "declared toolchain test"
        if failing_cases:
            gate_name = f"{gate_name} — {_names_the_failing_cases(failing_cases)}"
        # ``detail`` is passed as the runner returned it, not re-flattened
        # with ``str()``: it IS the sentence (``DeclaredTestDetail`` is a
        # string), and keeping the object lets the checkpoint's own row carry
        # the command that ran and the code it exited with. Every reader that
        # only wants the sentence sees exactly the sentence it saw before.
        return GatesReport(
            status=GateStatus.RED,
            failed_gates=(gate_name,),
            detail=detail,
            evidence=evidence,
        )

    return read_gates


# ---------------------------------------------------------------------------
# The merge-ready checkpoint's production instance
# ---------------------------------------------------------------------------


def make_conductor_merge_card_published_probe(
    *, pool: Any
) -> Callable[[str], bool]:
    """Build the DURABLE half of the one-card latch (shadow-replay item 5).

    The question: *has a merge card already gone out for this build?* The
    answer lives in the gate's own rows and always has. ``gate_check``
    writes a ``stage_log`` row — via ``record_decision`` and again via
    ``record_paused_build`` — carrying the card's ``target_identifier``,
    and it writes it BEFORE it waits for the owner. So a row bearing the
    merge card's identifier is durable proof that the card was published.

    ``target_identifier`` is the key rather than ``stage_label`` because
    the label is *copy*: it is the phrase-book plain name a human reads on
    the card, and copy is allowed to change. The identifier is the
    machine's name for the same thing and is what the routine path already
    matches on.

    Reads answer ``False`` on any failure. An unreadable probe must never
    wedge a journey that has never carded — the checkpoint's in-process
    latch still covers the same-process case, and the publisher logs the
    degrade loudly.
    """
    from forge.cli._serve_gate_activation import _MERGE_CARD_TARGET_IDENTIFIER

    def already_carded(build_id: str) -> bool:
        try:
            rows = pool.read_stages(build_id)
        except Exception as exc:  # noqa: BLE001 — a read defect is not a card
            logger.error(
                "conductor one-card latch: read_stages raised %s: %s for "
                "build_id=%s — answering NOT carded (the in-process latch is "
                "the only guard on this turn)",
                type(exc).__name__,
                exc,
                build_id,
            )
            return False
        for row in rows or ():
            if (
                getattr(row, "target_identifier", None)
                == _MERGE_CARD_TARGET_IDENTIFIER
            ):
                return True
        return False

    return already_carded


def _checkpoint_class_that_writes_its_verdict() -> Any:
    """The merge-ready checkpoint, with its verdict written into the journey.

    Attempt fifteen, 2026-09-08: the checkpoint ran the repository's declared
    suite on the journey worktree, two tests failed, and nothing durable said
    so. The planner reads the journey's ``stage_log`` rows, and the only rows
    the checkpoint left were conductor-turn rows all saying the same three
    words — so the planner re-read the same clean follow-up review and chose
    the checkpoint again. Four identical turns later the nothing-changed rule
    stopped the build.

    The cure is one line of behaviour added AFTER the checkpoint has decided:
    the same publisher, subclassed, recording what it just decided. It is a
    subclass rather than a wrapper on purpose — the design pass's rule is
    "one publisher behind all four call sites", and everything that reads the
    gate (the one-card latch, the seams, an ``isinstance`` check) must still
    be looking at that publisher.

    A decision that never read the gate set (no commits, already carded)
    writes no row: there is no verdict to record. A recorder that raises
    leaves the decision exactly as the checkpoint made it, and the loop is
    bounded by the nothing-changed rule as it was before this lane.

    Built lazily so this module keeps its import edge to the checkpoint
    inside the factory, where it has always been.
    """
    from forge.pipeline.merge_ready_checkpoint import MergeReadyCheckpointPublisher

    class _CheckpointThatWritesItsVerdict(MergeReadyCheckpointPublisher):
        def __init__(self, *, record: Callable[[Any], Any], **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._record = record

        async def submit_decision(self, **kwargs: Any) -> Any:
            decision = await super().submit_decision(**kwargs)
            return self._record(decision)

    return _CheckpointThatWritesItsVerdict


def make_merge_ready_checkpoint(
    *,
    pool: Any,
    publish_card: Callable[..., Any] | None,
    gates_green_reader: Callable[..., Any] | None = None,
    has_commits_probe: Callable[[str], Any] | None = None,
    receipts_root: "Path | str | None" = None,
    published_probe: Callable[[str], Any] | None = None,
    stage_log_writer: Any = None,
    review_cycle_cap: int | None = None,
    specification_fence: Callable[..., Any] | None = None,
) -> Any:
    """Compose the ONE ``pr_review_gate`` implementation for production.

    ``publish_card`` is
    :func:`forge.cli._serve_gate_activation.make_merge_card_publisher`'s
    closure — the SAME approve-click machinery the routine path delivers
    through, jarvis untouched. ``None`` is the shadow-replay posture
    (design pass Stage 2, delivery deliberately OFF): the checkpoint still
    runs its gates-green precondition and reports honestly.

    ``gates_green_reader`` is left ``None`` unless a caller wires one, and
    that is a REFUSAL, not an omission: with no reader the checkpoint reads
    :attr:`~forge.pipeline.merge_ready_checkpoint.GateStatus.UNKNOWN`,
    which it treats as red. The precondition is "proven green", never "not
    proven red", so an unwired gate set can never publish a card.
    :func:`make_gates_green_reader` is the production one.

    ``published_probe`` defaults to
    :func:`make_conductor_merge_card_published_probe` — the DURABLE half of
    the one-card latch. The publisher is built fresh per build, so its
    in-memory latch is empty after a daemon restart; without the durable
    probe a restart mid-journey could put a second card in front of the
    owner for one merge word.

    ``stage_log_writer`` is the fix journey's own writer. When it is given,
    the checkpoint's verdict is written into the journey's history and a RED
    verdict goes back into the fix cycle instead of being asked for again
    (2026-09-08, attempt fifteen). Left ``None`` — every caller that predates
    this lane — the checkpoint is byte for byte what it was.

    ``specification_fence`` is the fence of 2026-09-09: it reads which files
    the journey's branch changed against its base and refuses a card when any
    of them is what the repository calls its specification, or when the change
    touches a line recording somebody's approval.
    :func:`make_specification_fence` is the production one. Left ``None`` —
    every caller that predates the fence — no fence runs and the checkpoint is
    byte for byte what it was.

    ``review_cycle_cap`` is how many review cycles this build's profile
    allows, read from the same resolved profile the supervisor is judged
    against. It answers one question: on a red gate, is there a cycle left to
    loop back into? With one, the gate loops back and the next plan sends the
    failing tests to the review seat; with none, the journey ends FAILED and
    the driver's close-out names the tests that kept it red. ``None`` means
    the profile caps nothing, so a red gate always loops back — today's
    behaviour, and the attended profile's.
    """
    from forge.pipeline.fix_journey_receipts import write_fix_journey_failure_pack
    from forge.pipeline.merge_ready_checkpoint import (
        MergeReadyCheckpointPublisher,
        RedGateAction,
    )
    from forge.pipeline.mode_c_history_reader import project_mode_c_history
    from forge.pipeline.mode_c_planner import a_review_cycle_remains

    def _branch_reader(build_id: str) -> str | None:
        row = pool.get_build_row(build_id)
        return getattr(row, "branch", None) if row is not None else None

    def _failure_pack_writer(
        *, build_id: str, feature_id: str, reason: str, gates: Any
    ) -> Any:
        row = pool.get_build_row(build_id)
        return write_fix_journey_failure_pack(
            build_id=build_id,
            reason=reason,
            outcome="merge-ready-checkpoint",
            feature_id=feature_id or getattr(row, "feature_id", None),
            correlation_id=getattr(row, "correlation_id", None),
            branch=getattr(row, "branch", None),
            worktree_path=getattr(row, "worktree_path", None),
            receipts_root=receipts_root,
        )

    def _red_gate_action(build_id: str, gates: Any) -> Any:
        """Loop back into the fix cycle, or end the journey — §c.3's choice.

        The design's words for a red gate are "it loops back into the fix
        cycle, or terminates FAILED when there is no cycle left to loop
        into". That question is answered here, off the journey's own rows and
        the profile's own cap, using the SAME arithmetic the planner and the
        budget guard use, so no two of the three can disagree about where the
        last cycle ends.

        A ledger that cannot be read answers "loop back": the planner reads
        the same rows a moment later and will end the journey itself if there
        is really nothing left, and refusing to loop on an unreadable read
        would stop a journey that still had a cycle to spend.
        """
        try:
            history = project_mode_c_history(pool.read_stages(build_id))
        except Exception as exc:  # noqa: BLE001 — a read defect is not a verdict
            logger.warning(
                "the merge-ready checkpoint: reading build_id=%s's history to "
                "decide whether a review cycle is left raised %s: %s — "
                "looping back into the fix cycle, where the planner reads the "
                "same rows and decides",
                build_id,
                type(exc).__name__,
                exc,
            )
            return RedGateAction.LOOP_BACK
        if a_review_cycle_remains(history, review_cycle_cap):
            return RedGateAction.LOOP_BACK
        logger.error(
            "the merge-ready checkpoint: the checks are red for build_id=%s "
            "and this build's profile has no review cycle left (cap=%s) — the "
            "journey ends FAILED, naming what stayed red: %s",
            build_id,
            review_cycle_cap,
            ", ".join(getattr(gates, "failed_gates", ()) or ()) or "no gate named",
        )
        return RedGateAction.TERMINATE_FAILED

    seams: dict[str, Any] = {
        "publish_card": publish_card,
        "gates_green_reader": gates_green_reader,
        "has_commits_probe": has_commits_probe,
        "branch_reader": _branch_reader,
        "failure_pack_writer": _failure_pack_writer,
        "published_probe": (
            published_probe
            if published_probe is not None
            else make_conductor_merge_card_published_probe(pool=pool)
        ),
        "specification_fence": specification_fence,
    }
    if stage_log_writer is None:
        # Every caller that predates this lane: the publisher itself, with
        # the seams it has always had and no red-gate action of its own.
        return MergeReadyCheckpointPublisher(**seams)
    return _checkpoint_class_that_writes_its_verdict()(
        red_gate_action=_red_gate_action,
        record=lambda decision: record_checkpoint_verdict(
            decision, pool=pool, stage_log_writer=stage_log_writer
        ),
        **seams,
    )


def record_checkpoint_verdict(
    decision: Any, *, pool: Any, stage_log_writer: Any
) -> Any:
    """Write one checkpoint verdict into the journey's history; return it.

    The decision comes back with :data:`CHECKPOINT_VERDICT_RECORDED_KEY` on
    its details when the row was really written, which is what tells the
    conductor's loop that a red gate's loop-back moved the journey rather
    than standing still.

    Never raises. A row that could not be written is said plainly and the
    decision is returned exactly as the checkpoint made it — the journey is
    then bounded by the nothing-changed rule, as it was before this lane.
    """
    gates = getattr(decision, "gates", None)
    if gates is None:
        # No gate set was read (no commits to check, or the one card is
        # already spent). There is no verdict to record.
        return decision

    detail = getattr(gates, "detail", "")
    failing = tuple(getattr(detail, "failing_cases", ()) or ())
    green = bool(getattr(gates, "is_green", False))
    build_id = str(getattr(decision, "build_id", "") or "")
    status = getattr(getattr(gates, "status", None), "value", None) or "red"
    # The row's own account of itself, which the projection carries forward as
    # the failure reason on a red row — so the sentence that names what
    # stopped a journey is written once, here.
    reason = f"the merge-ready checks are {status}: {detail or 'no detail'}"
    try:
        stage_log_writer.record_checkpoint(
            build_id=build_id,
            feature_id=getattr(decision, "feature_id", "") or None,
            green=green,
            rationale=reason,
            failing_tests=failing,
            failed_gates=tuple(getattr(gates, "failed_gates", ()) or ()),
            declared_test_command=str(getattr(detail, "command", "") or ""),
            declared_test_exit_code=getattr(detail, "exit_code", None),
            evidence=str(getattr(gates, "evidence", "") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — a row is not worth a journey
        logger.error(
            "the merge-ready checkpoint: writing its verdict into build_id=%s's "
            "history raised %s: %s — the decision stands, but the planner "
            "cannot read what the checks said and the journey is bounded by "
            "the nothing-changed rule instead",
            build_id,
            type(exc).__name__,
            exc,
        )
        return decision

    try:
        return dataclasses.replace(
            decision,
            details={
                **dict(getattr(decision, "details", None) or {}),
                CHECKPOINT_VERDICT_RECORDED_KEY: True,
            },
        )
    except Exception as exc:  # noqa: BLE001 — the row is written either way
        logger.warning(
            "the merge-ready checkpoint: the verdict for build_id=%s is "
            "recorded but could not be marked on the decision (%s: %s)",
            build_id,
            type(exc).__name__,
            exc,
        )
        return decision


# ---------------------------------------------------------------------------
# The Supervisor factory (shakeout item 3)
# ---------------------------------------------------------------------------


def make_conductor_guardkit_run_chooser(
    *,
    pool: Any,
    config: Any,
    in_container_run: Any,
    build_sidecar_run: Callable[..., Any] | None = None,
) -> Callable[[str], Any]:
    """Return ``(build_id) -> guardkit_run`` — where this build's legs run.

    Rich's rule, 2026-09-07: nothing the factory runs on a repository runs on
    the host. A fix journey's legs (``guardkit task-review``, ``guardkit
    task-work``) install and run the repository's own code, so for a
    repository that has a sandbox they run inside it, reached through that
    sandbox's deploy sidecar, with the journey worktree as their working
    directory (sandbox first, rule 75). Every other repository keeps today's
    path exactly: the legs run in the forge container, through
    :func:`forge.adapters.guardkit.run.run`.

    The choice is per build, not per boot, because one daemon serves every
    repository and only some of them have a sandbox. With
    ``planning.sandboxes`` empty — the default, and the estate's state until
    an operator fills it in — this returns the in-container runner for every
    build without reading a row at all, which is byte for byte the
    composition before this lane.

    Args:
        pool: The lifecycle persistence facade (``get_build_row``).
        config: The loaded forge config.
        in_container_run: Today's runner, used for every repository that has
            no sandbox.
        build_sidecar_run: ``(base_url, repo_paths) -> runner`` — injected by
            tests; production uses
            :func:`forge.adapters.guardkit.run_via_sidecar.build_sidecar_leg_run`,
            the two-command door that carries ``task-review`` and
            ``task-work`` to the sandbox with the journey worktree as their
            working directory.
    """
    sandboxes = dict(
        getattr(getattr(config, "planning", None), "sandboxes", None) or {}
    )
    if not sandboxes:
        def choose_in_container(build_id: str) -> Any:  # noqa: ARG001 — one answer
            return in_container_run

        return choose_in_container

    repo_paths = dict(
        getattr(getattr(config, "planning", None), "target_repo_paths", None) or {}
    )

    def _factory(base_url: str) -> Any:
        if build_sidecar_run is not None:
            return build_sidecar_run(base_url=base_url, repo_paths=repo_paths)
        from forge.adapters.guardkit.run_via_sidecar import build_sidecar_leg_run

        return build_sidecar_leg_run(base_url=base_url, repo_paths=repo_paths)

    runners: dict[str, Any] = {}

    def choose(build_id: str) -> Any:
        try:
            row = pool.get_build_row(build_id)
        except Exception as exc:  # noqa: BLE001 — never break a dispatch
            logger.warning(
                "conductor composition: reading build_id=%s to decide where "
                "its legs run raised %s: %s — the legs run in the forge "
                "container, as they always did",
                build_id,
                type(exc).__name__,
                exc,
            )
            return in_container_run
        repo = str(getattr(row, "repo", "") or "") if row is not None else ""
        entry = sandboxes.get(repo)
        if entry is None:
            return in_container_run
        if repo not in runners:
            runners[repo] = _factory(str(entry.sidecar_url))
            logger.info(
                "conductor composition: %s has a sandbox (%s), so its fix "
                "journey's legs run inside it through the sidecar at %s, with "
                "the journey worktree as their working directory — the "
                "repository's own code never runs on the host",
                repo,
                getattr(entry, "name", "?"),
                entry.sidecar_url,
            )
        return runners[repo]

    return choose


def with_the_gate_evidence(
    inner: Callable[..., Any] | None, *, pool: Any
) -> Callable[..., Any] | None:
    """Wrap the fix-task context builder so a gate-driven review is told why.

    The supervisor asks its ``fix_task_context_builder`` for BOTH Mode C
    stages. For a ``/task-review`` that follows a RED merge-ready checkpoint
    this adds one more ``--context`` entry: the declared command that failed,
    the tests it named, what it printed, and the instruction that the
    repository's existing tests are the specification.

    Everything else is left exactly as it was found — any other stage, and a
    review the checks have not sent back, gets the very mapping the inner
    builder made, so a routine build and an ordinary review cycle are
    byte-identical to before. ``None`` in, ``None`` out: a composition with
    no context builder wired stays without one.

    Never raises: a document that cannot be built is a review dispatched the
    way it always was, said once in the log.
    """
    if inner is None:
        return None

    from forge.pipeline.fix_task_context_builder import build_gate_evidence_context

    def build(stage: Any, build_id: str, fix_task: Any) -> Any:
        context = inner(stage, build_id, fix_task)
        if stage is not StageClass.TASK_REVIEW:
            return context
        try:
            row = pool.get_build_row(build_id)
            entry = build_gate_evidence_context(
                rows=pool.read_stages(build_id),
                worktree_path=getattr(row, "worktree_path", None) if row else None,
                task_id=str(getattr(row, "task_id", "") or "") if row else "",
            )
        except Exception as exc:  # noqa: BLE001 — a read defect is not a journey
            logger.warning(
                "gate evidence: reading build_id=%s to build the review's "
                "document raised %s: %s — the review is dispatched exactly as "
                "before",
                build_id,
                type(exc).__name__,
                exc,
            )
            return context
        if entry is None:
            return context
        updated = dict(context or {})
        entries = list(updated.get("context_entries") or ())
        entries.append(entry)
        updated["context_entries"] = entries
        return updated

    return build


def build_conductor_supervisor_factory(
    *,
    pool: Any,
    config: Any,
    forward_context_builder: Any,
    worktree_allowlist: Any,
    read_allowlist: "list[Path]",
    subprocess_runner: Any,
    subprocess_runner_for_build: Callable[[str], Any] | None = None,
    lifecycle_emitter: Any = None,
    publish_approval_request: Any = None,
    publish_card: Callable[..., Any] | None = None,
    gates_green_reader: Callable[..., Any] | None = None,
    specification_fence: Callable[..., Any] | None = None,
    stage_log_writer: Any = None,
    coach_score_reader: Callable[[str], float | None] | None = None,
    base_branch: str = "main",
    receipts_root: "Path | str | None" = None,
    failure_pack_source_reader: Callable[[str], str | None] | None = None,
    build_supervisor: Callable[..., Any] | None = None,
    mode_kwargs_builder: Callable[..., dict] | None = None,
    budget_kwargs_builder: Callable[..., dict] | None = None,
    timeout_seconds_by_stage: "Mapping[StageClass, int] | None" = None,
    leg_model: str | None = None,
) -> Callable[[str], Any]:
    """Return the ``(build_id) -> Supervisor`` factory the router injects.

    This is what ``build_conductor_router`` refused to invent for itself in
    Stage 1. Per build it composes:

    * the **mode collaborators** — ``build_conductor_mode_kwargs``: the
      mode reader, the fix-journey planner, the ``stage_log`` projection,
      the terminal handler + commit probe, and the fix-task context
      builder;
    * the **budget collaborators** — ``build_conductor_budget_kwargs``:
      caps off the build's profile, the wall-clock anchors, and the pause
      that publishes the risk-high escalation (design pass §b.1);
    * the **merge-ready checkpoint** as ``pr_review_gate`` — one
      implementation behind all four ``submit_decision`` call sites, and
      one per build so its one-card latch is scoped to this journey;
    * the **conductor dispatcher adapter** as ``subprocess_dispatcher`` —
      the seam that binds ``task_id`` off the build row and translates the
      supervisor's kwargs into the dispatcher's;
    * refusing stand-ins for the Mode-A-only seams (see
      :class:`_ModeAOnlySeam`).

    The factory builds a FRESH Supervisor per build on purpose: the budget
    caps are per-build (they come off ``builds.profile``) and the merge
    card's latch is per-journey. Sharing one instance across builds would
    share both.

    Args:
        specification_fence: The merge-ready checkpoint's specification
            fence (Rich's ruling, 2026-09-09) — the seam that reads which
            files the journey's branch changed against its base and refuses
            a card when any of them is the repository's specification, or
            when the change touches a line recording somebody's approval.
            Left unset, :func:`make_specification_fence` is built over this
            pool and this config, which is what production gets. A caller
            that wants no fence at all passes one that answers ``None``.
        timeout_seconds_by_stage: Per-stage subprocess tripwires for the
            dispatcher adapter. Defaults to
            :data:`CONDUCTOR_STAGE_TIMEOUT_SECONDS` — read the comment
            block above those constants before changing either number.
            Injectable so a test can drive the selection without waiting
            out half an hour.
        leg_model: The fix journey's seat — the local model every MODE_C
            leg runs on, appended to its argv as ``--model <seat>``. The
            production composition root passes
            ``config.conductor.seat`` (design pass §2); there is no env
            fallback any more. ``None`` (or blank) emits no ``--model`` at
            all, keeping the argv byte-identical to the one this
            composition has always emitted with the conductor disabled.

    The **leg budgets** are deliberately NOT an argument here. They ride on
    the build's resolved budget profile — the object
    ``build_conductor_budget_kwargs`` already produces per build — and this
    factory hands that same object to the dispatcher adapter. So an
    operator turns ``--max-turns`` / ``--sdk-timeout`` / ``--leg-budget``
    by editing the profile in ``forge.yaml`` the build was queued under,
    and the caps the build is judged against and the budgets its legs are
    given are the same profile by construction rather than by care. A
    profile carrying no ``leg_*`` fields — which is every profile written
    before they existed — emits no budget tokens at all.
    """
    from forge.cli.serve import (
        build_conductor_budget_kwargs,
        build_conductor_mode_kwargs,
    )
    from forge.cli.serve import build_supervisor as _default_build_supervisor
    from forge.pipeline.constitutional_guard import ConstitutionalGuard
    from forge.pipeline.dispatchers.conductor_subprocess import (
        make_conductor_subprocess_dispatcher,
    )
    from forge.pipeline.per_feature_sequencer import PerFeatureLoopSequencer
    from forge.pipeline.stage_ordering_guard import StageOrderingGuard

    _build = build_supervisor or _default_build_supervisor
    _mode_kwargs = mode_kwargs_builder or build_conductor_mode_kwargs
    _budget_kwargs = budget_kwargs_builder or build_conductor_budget_kwargs
    # WHERE THIS BUILD'S LEGS RUN (sandbox first, 2026-09-07, rule 75). One
    # daemon serves every repository and only some have a sandbox, so the
    # runner is chosen per build rather than per boot. With
    # planning.sandboxes empty the chooser answers ``subprocess_runner`` for
    # every build, which is byte for byte the composition before this lane.
    _runner_for = subprocess_runner_for_build or make_conductor_guardkit_run_chooser(
        pool=pool, config=config, in_container_run=subprocess_runner
    )
    # THE SPECIFICATION FENCE (Rich's ruling, 2026-09-09). Built once per
    # boot over the same pool and config the gate set is: it reads the build
    # row for the branch and the worktree, this repository's own declaration
    # of what its specification is, and — through the sandbox when the
    # repository has one — the branch's own changes.
    _fence = specification_fence or make_specification_fence(pool=pool, config=config)

    if stage_log_writer is None:
        from forge.cli._serve_deps_stage_log import (
            build_fix_journey_stage_log_writer,
        )

        stage_log_writer = build_fix_journey_stage_log_writer(pool)

    ordering_reader = _SqliteOrderingStageLogReader(pool)

    stage_timeouts = (
        CONDUCTOR_STAGE_TIMEOUT_SECONDS
        if timeout_seconds_by_stage is None
        else timeout_seconds_by_stage
    )

    # The seat, config-as-code: the production composition root passes
    # ``config.conductor.seat`` (design pass §2). The env stopgap that used
    # to be read here is DELETED — see the note above the stage-timeout
    # constants. A blank seat is already normalised to None by the config
    # model, and an ENABLED conductor with no seat never reaches this
    # function: it refuses at config load.
    resolved_leg_model = (leg_model or "").strip() or None
    if resolved_leg_model:
        logger.info(
            "conductor composition: fix-journey legs will name the seat "
            "%r on every MODE_C dispatch (--model, from conductor.seat)",
            resolved_leg_model,
        )
    else:
        logger.warning(
            "conductor composition: no leg seat is named (conductor.seat "
            "unset) — the pipeline emits NO --model and the builder picks "
            "its own default. Zero-frontier then rests on the builder's own "
            "chokepoint fence, not on anything this side says"
        )

    def supervisor_factory(build_id: str) -> Any:
        mode_kwargs = _mode_kwargs(
            pool=pool,
            config=config,
            base_branch=base_branch,
            worktree_allowlist=worktree_allowlist,
            forward_context_builder=forward_context_builder,
            failure_pack_source_reader=failure_pack_source_reader,
            receipts_root=receipts_root,
        )
        budget_kwargs = _budget_kwargs(
            pool=pool,
            config=config,
            build_id=build_id,
            publish_approval_request=publish_approval_request,
            lifecycle_emitter=lifecycle_emitter,
            coach_score_reader=coach_score_reader,
        )
        # The leg budgets come off the SAME resolved profile the supervisor
        # is about to be judged against — read out of the budget kwargs
        # rather than resolved a second time. Two resolutions of one
        # ``builds.profile`` is two statements of one rule, and the day
        # they disagree the caps a build is held to and the budgets its
        # legs were given come from different profiles. ``.get`` because
        # ``budget_kwargs_builder`` is injectable and a test's stand-in
        # need not supply guards; absent simply means no leg budgets, and
        # no leg budgets means the argv this composition always emitted.
        leg_budgets = budget_kwargs.get("budget_guards")
        dispatcher = make_conductor_subprocess_dispatcher(
            build_row_reader=pool.get_build_row,
            read_allowlist=read_allowlist,
            worktree_allowlist=worktree_allowlist,
            forward_context_builder=forward_context_builder,
            stage_log_writer=stage_log_writer,
            subprocess_runner=_runner_for(build_id),
            timeout_seconds_by_stage=stage_timeouts,
            leg_model=resolved_leg_model,
            leg_budgets=leg_budgets,
        )
        # HOW MANY REVIEW CYCLES THIS BUILD MAY SPEND — read off the SAME
        # resolved profile the supervisor is judged against (above), never
        # resolved a second time. The checkpoint asks it once, on a red gate:
        # with a cycle left the failures go back to the review seat, with
        # none the journey ends and says which tests kept it red.
        guards = budget_kwargs.get("budget_guards")
        review_cycle_cap = (
            guards.max_review_cycles
            if guards is not None and getattr(guards, "caps_enabled", False)
            else None
        )
        checkpoint = make_merge_ready_checkpoint(
            pool=pool,
            publish_card=publish_card,
            gates_green_reader=gates_green_reader,
            has_commits_probe=None,
            receipts_root=receipts_root,
            stage_log_writer=stage_log_writer,
            review_cycle_cap=review_cycle_cap,
            specification_fence=_fence,
        )
        # THE REVIEW LEG'S CONTEXT, WITH THE GATE'S EVIDENCE ON IT. A review
        # the checks sent the journey back to is handed what they ran, what
        # failed and what the run printed — the same carriage K2's
        # verification document uses, added here because this is the layer
        # that holds both the ledger and the build row.
        mode_kwargs = dict(mode_kwargs)
        mode_kwargs["fix_task_context_builder"] = with_the_gate_evidence(
            mode_kwargs.get("fix_task_context_builder"), pool=pool
        )
        return _build(
            forward_context_builder=forward_context_builder,
            async_task_starter=_ModeAOnlySeam("async_task_starter"),
            stage_log_recorder=_ModeAOnlySeam("stage_log_recorder"),
            state_channel=_ModeAOnlySeam("state_channel"),
            lifecycle_emitter=lifecycle_emitter,
            ordering_guard=StageOrderingGuard(),
            per_feature_sequencer=PerFeatureLoopSequencer(),
            constitutional_guard=ConstitutionalGuard(),
            state_reader=_SqliteBuildStateReader(pool),
            ordering_stage_log_reader=ordering_reader,
            per_feature_stage_log_reader=_ModeAOnlySeam(
                "per_feature_stage_log_reader"
            ),
            async_task_reader=_ModeAOnlySeam("async_task_reader"),
            reasoning_model=_ModeAOnlySeam("reasoning_model"),
            turn_recorder=_SqliteTurnRecorder(pool),
            specialist_dispatcher=_ModeAOnlySeam("specialist_dispatcher"),
            subprocess_dispatcher=dispatcher,
            pr_review_gate=checkpoint,
            async_subagent_middleware=_NoToolsMiddleware(),
            **mode_kwargs,
            **budget_kwargs,
        )

    return supervisor_factory


# ---------------------------------------------------------------------------
# The driver deps factory (shakeout item 4)
# ---------------------------------------------------------------------------


def make_conductor_wait_window_reader(
    *,
    pool: Any,
    config: Any,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[str], WaitWindow]:
    """Build the structured wait's durable-anchor reader.

    **Recomputed from durable rows on every iteration** — the property
    that makes the wait re-entrant across a daemon restart. Nothing is
    counted down in memory.

    The anchors, in the order they decide:

    1. **No row, or a terminal row** → resolved. There is nothing left to
       wait for; the loop re-plans and the turn reports the terminal.
    2. **No ``pending_approval_request_id``** → resolved. The build is not
       parked on a human; whatever it was waiting for has moved.
    3. Otherwise the window runs from the build's ``started_at`` anchor
       for ``approval.default_wait_seconds`` (phase 1), then to
       ``approval.max_wait_seconds`` (phase 2, ``needs_republish`` set so
       the persisted request is re-emitted AFTER the waiter arms). Past
       that the window is expired and the journey stops loudly with a pack.

    Those two window numbers are the approval protocol's own, read from
    config rather than invented here — a fix journey's pause must not
    outlive, or undercut, the pause the rest of the estate honours.
    """
    _clock = clock or (lambda: datetime.now(timezone.utc))

    def read_window(build_id: str) -> WaitWindow:
        from forge.lifecycle.state_machine import TERMINAL_STATES

        row = pool.get_build_row(build_id)
        if row is None:
            return WaitWindow(remaining_seconds=0.0, resolved=True)
        if row.status in TERMINAL_STATES:
            return WaitWindow(remaining_seconds=0.0, resolved=True)
        if not getattr(row, "pending_approval_request_id", None):
            return WaitWindow(remaining_seconds=0.0, resolved=True)

        first = float(config.approval.default_wait_seconds)
        ceiling = float(config.approval.max_wait_seconds)
        anchor = getattr(row, "started_at", None) or getattr(row, "queued_at", None)
        if anchor is None:
            return WaitWindow(remaining_seconds=first, phase=1)
        elapsed = (_clock() - anchor).total_seconds()
        if elapsed < first:
            return WaitWindow(remaining_seconds=first - elapsed, phase=1)
        if elapsed < ceiling:
            return WaitWindow(
                remaining_seconds=ceiling - elapsed, phase=2, needs_republish=True
            )
        return WaitWindow(remaining_seconds=0.0, phase=2)

    return read_window


def _declared_test_evidence_of(report: Any) -> str:
    """What the declared test printed on this turn, or ``""``.

    The merge-ready checkpoint hands its decision back on the turn report
    (``dispatch_result``), and the gate set it read carries the declared test
    command's own output. Every other turn's dispatch result has no gate set
    and no evidence, so it writes no file — this reader asks, it does not
    assume, and it never raises: a receipt that could not be written must
    never cost a journey.
    """
    from forge.pipeline.merge_ready_checkpoint import DECLARED_TEST_EVIDENCE_KEY

    decision = getattr(report, "dispatch_result", None)
    gates = getattr(decision, "gates", None)
    evidence = getattr(gates, "evidence", "")
    if not isinstance(evidence, str) or not evidence.strip():
        details = getattr(decision, "details", None)
        if isinstance(details, Mapping):
            evidence = details.get(DECLARED_TEST_EVIDENCE_KEY, "")
    return evidence if isinstance(evidence, str) and evidence.strip() else ""


def make_conductor_receipts_exporter(
    *,
    pool: Any,
    receipts_root: "Path | str | None" = None,
    config: Any = None,
    post: Any = None,
) -> Callable[..., Any]:
    """Build the driver's ``export_stage_receipts`` seam — ONE shape.

    The seam shape had to be settled, not split: the driver calls
    ``(*, build_id, report)`` because that is all a turn loop knows, while
    the real exporter
    (:func:`forge.pipeline.fix_journey_receipts.export_stage_receipts`)
    needs ``(*, build_id, stage, worktree_path)``. This adapter is where
    the two meet, and the driver's shape is the one that stands: the
    worktree is a durable row read (not something a turn loop should
    carry) and the stage comes off the report the driver already has.

    A turn that dispatched nothing exports nothing and returns ``None`` —
    receipts belong to stages, not to planning ticks.

    **Where the copying happens** (sandbox first, 2026-09-07, rule 77). For a
    repository with a sandbox the journey's tree is inside that sandbox and
    forge-prod cannot read it, so the export runs there, over the sidecar's
    ``/receipts/export`` route, writing under the receipts root that is
    mounted read-write — the same files forge-prod reads afterwards. Every
    other repository copies in the container exactly as before. ``config``
    absent (a test that passes only a pool) means no sandboxes, hence
    today's path.

    **The sandbox branch answers with something to await.** The copy is a
    call over the wire, and this seam is called from the conductor's turn
    loop, which runs on the daemon's own event loop alongside every other
    build and journey. So for a sandbox repository the seam hands back a
    coroutine that does the waiting on a worker thread; the driver already
    awaits whatever this seam returns
    (:func:`forge.pipeline.conductor_driver._maybe_await`), so the contract
    is unchanged and the daemon keeps answering while the receipts copy.
    The in-container branch returns the stage key directly, exactly as it
    always has.
    """
    from forge.pipeline.fix_journey_receipts import export_stage_receipts
    from forge.pipeline.merge_ready_checkpoint import DECLARED_TEST_OUTPUT_FILENAME

    sandboxes = dict(
        getattr(getattr(config, "planning", None), "sandboxes", None) or {}
    )

    def export(*, build_id: str, report: Any) -> Any:
        stage = getattr(report, "chosen_stage", None)
        stage_name = getattr(stage, "value", None)
        if not stage_name:
            return None
        row = pool.get_build_row(build_id)
        rationale = getattr(report, "rationale", "") or ""
        extra_files: dict[str, str] = {}
        if rationale:
            extra_files["turn-rationale.txt"] = rationale
        evidence = _declared_test_evidence_of(report)
        if evidence:
            extra_files[DECLARED_TEST_OUTPUT_FILENAME] = evidence
        worktree_path = getattr(row, "worktree_path", None)
        entry = sandboxes.get(str(getattr(row, "repo", "") or "")) if row else None
        if entry is not None:
            # A coroutine, not a key: the driver awaits it, and the wire wait
            # happens on a worker thread instead of on the daemon's loop.
            return _export_receipts_in_sandbox(
                entry=entry,
                build_id=build_id,
                stage=stage_name,
                worktree_path=worktree_path,
                extra_files=extra_files or None,
                post=post,
            )
        result = export_stage_receipts(
            build_id=build_id,
            stage=stage_name,
            worktree_path=worktree_path,
            receipts_root=receipts_root,
            extra_files=extra_files or None,
        )
        return getattr(result, "stage_key", None)

    return export


#: How long exporting one stage's receipts over the wire may take. Copying a
#: worktree's receipt families is file work, not model work; five minutes is
#: generous and still bounded.
RECEIPTS_EXPORT_TIMEOUT_S: float = 300.0


async def _export_receipts_in_sandbox(
    *,
    entry: Any,
    build_id: str,
    stage: str,
    worktree_path: Any,
    extra_files: "dict[str, str] | None",
    post: Any = None,
) -> str | None:
    """Ask the sidecar inside the repository's sandbox to export the receipts.

    Returns the stage key it wrote, or ``None`` when it could not — the same
    contract the in-container export has, so a failure to copy never blocks a
    journey; it is logged and the turn carries on.

    The POST waits on a worker thread (``asyncio.to_thread``), as this lane's
    two sibling seams do — the tree cut in
    :mod:`forge.cli._conductor_worktree` and the leg run in
    :mod:`forge.adapters.guardkit.run_via_sidecar`. A journey turn runs on the
    daemon's shared event loop, so waiting there would stop the bus
    subscriptions, the queue and every other build for as long as the copy
    took, and for the whole five minutes if the sandbox's sidecar were slow.
    """
    from forge.deploy_sidecar.service import RECEIPTS_EXPORT_ROUTE
    from forge.planning.sidecar_git_runner import _urllib_post

    if not worktree_path:
        logger.warning(
            "conductor receipts: build_id=%s has no worktree path, so there "
            "is nothing in sandbox %s to export",
            build_id,
            getattr(entry, "name", "?"),
        )
        return None
    sender = post if post is not None else _urllib_post
    url = f"{str(entry.sidecar_url).rstrip('/')}{RECEIPTS_EXPORT_ROUTE}"
    body: dict[str, Any] = {
        "build_id": build_id,
        "stage": stage,
        "worktree": str(worktree_path),
    }
    if extra_files:
        body["extra_files"] = extra_files
    try:
        status, decoded = await asyncio.to_thread(
            sender, url, body, RECEIPTS_EXPORT_TIMEOUT_S
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — never block a turn
        logger.warning(
            "conductor receipts: the sidecar in sandbox %s could not be "
            "reached at %s to export build_id=%s's %s receipts (%s: %s) — the "
            "journey's outcome is unaffected",
            getattr(entry, "name", "?"),
            url,
            build_id,
            stage,
            type(exc).__name__,
            exc,
        )
        return None
    answer = decoded if isinstance(decoded, dict) else {}
    if status != 200 or answer.get("status") != "success":
        logger.warning(
            "conductor receipts: exporting build_id=%s's %s receipts inside "
            "sandbox %s did not succeed (HTTP %s): %s — the journey's outcome "
            "is unaffected",
            build_id,
            stage,
            getattr(entry, "name", "?"),
            status,
            answer.get("error") or answer.get("detail") or answer,
        )
        return None
    logger.info(
        "conductor receipts: build_id=%s's %s receipts were exported inside "
        "sandbox %s to %s",
        build_id,
        stage,
        getattr(entry, "name", "?"),
        answer.get("dest"),
    )
    key = answer.get("stage_key")
    return str(key) if key else None


def make_conductor_failure_pack_writer(
    *,
    pool: Any,
    receipts_root: "Path | str | None" = None,
    source_build_id_reader: Callable[[str], str | None] | None = None,
) -> Callable[..., Any]:
    """Build the driver's ``write_failure_pack`` seam.

    "A fix journey that fails leaves its own failure pack" (design pass
    §b.2). Success and failure alike leave receipts; this is the failure
    half, pointed back at the build the journey was trying to repair so a
    diagnoser can read both packs.
    """
    from forge.pipeline.fix_journey_receipts import write_fix_journey_failure_pack

    def write(
        *,
        build_id: str,
        reason: str,
        outcome: str,
        stage_keys: "tuple[str, ...]" = (),
    ) -> Any:
        row = pool.get_build_row(build_id)
        source_build_id = None
        if source_build_id_reader is not None:
            try:
                source_build_id = source_build_id_reader(build_id)
            except Exception as exc:  # noqa: BLE001 — never block a stop
                logger.warning(
                    "conductor failure pack: source_build_id_reader raised "
                    "%s: %s for build_id=%s",
                    type(exc).__name__,
                    exc,
                    build_id,
                )
        return write_fix_journey_failure_pack(
            build_id=build_id,
            reason=reason,
            outcome=outcome,
            feature_id=getattr(row, "feature_id", None),
            correlation_id=getattr(row, "correlation_id", None),
            source_build_id=source_build_id,
            branch=getattr(row, "branch", None),
            worktree_path=getattr(row, "worktree_path", None),
            stage_keys=stage_keys,
            receipts_root=receipts_root,
        )

    return write


#: ``ModeCTerminalDecision.outcome`` token meaning the journey FAILED. The
#: handler's other terminal tokens all begin ``clean-review-``; matched on
#: the lowercased word so this composition seam keeps no import edge to the
#: terminal-handler package (the same duck-typing the driver uses).
_TERMINAL_TOKEN_FAILED: str = "failed"

#: Prefix of the handler's two SUCCESS terminals
#: (``clean-review-no-fixes`` / ``clean-review-no-commits``): the journey
#: finished, there is nothing to merge, and the row is COMPLETE.
_TERMINAL_TOKEN_CLEAN_PREFIX: str = "clean-review"


#: The turn outcome that says "this journey is over". Read as a word for
#: the same no-import-edge reason as the tokens above.
_TURN_OUTCOME_TERMINAL: str = "terminal"

#: What the close-out returns when the report carries a terminal turn whose
#: decision it cannot read. Not a real handler outcome — it is the word the
#: unknown branch NAMES, so the stuck row becomes a legible failure rather
#: than an invisible one.
TERMINAL_TOKEN_UNREADABLE: str = "unreadable-terminal"


def _terminal_decision_token(report: Any) -> str | None:
    """The journey's terminal outcome as a word, or ``None`` to leave the row.

    ``None`` means "not this seam's to adjudicate" — chiefly the merge-card
    path, whose row is written by the gate's own state machine. Racing that
    writer is how a healthy build gets a false terminal (the FTR lesson),
    so the close-out declines to guess there.

    A TERMINAL turn whose decision is missing or shapeless answers
    :data:`TERMINAL_TOKEN_UNREADABLE` rather than ``None``: the journey IS
    over (the supervisor's own fallback path produces exactly this when the
    terminal handler raises), and leaving that row RUNNING is the defect
    this whole seam exists to end.
    """
    decision = getattr(report, "dispatch_result", None)
    if decision is not None and hasattr(decision, "card_published"):
        return None

    raw = getattr(decision, "outcome", None) if decision is not None else None
    token = (
        str(getattr(raw, "value", None) or raw).strip().lower()
        if raw is not None
        else ""
    )
    if token:
        return token

    turn = getattr(report, "outcome", None)
    turn_token = str(getattr(turn, "value", None) or turn or "").strip().lower()
    if turn_token == _TURN_OUTCOME_TERMINAL:
        return TERMINAL_TOKEN_UNREADABLE
    return None


def make_conductor_close_out(*, pool: Any) -> Callable[..., Any]:
    """Build the driver's ``close_out`` seam — the journey's last write.

    Two writes, in this order:

    1. One ``conductor-close-out`` ``stage_log`` row naming how the journey
       ended (unchanged).
    2. **The build row's terminal transition.** COMPLETE on the journey's
       success terminals, FAILED with the reason on every other one.

    The second write is the 2026-08-03 correction. The seam previously
    recorded and stopped, on the reasoning that "terminal transitions on
    this estate are owned by the lifecycle bridge and the gate's own state
    machine". True of the merge-card path — and false of every terminal the
    conductor reaches WITHOUT publishing a card, which is all three of the
    silent ones (§c.6: a clean review, a no-commit ending, a tooling
    fault). Nobody owned those, so nobody wrote them: the first production
    fix journey reached its terminal, logged "closed out", and left
    ``builds.status = RUNNING`` with an empty ``error`` forever.

    The card path is still left alone — :func:`_terminal_decision_token`
    answers ``None`` for it — so the FTR lesson keeps its force exactly
    where it applies.

    The transition goes through :func:`forge.cli._conductor_outcome.finish_mode_c_build`,
    the one careful writer: it refuses to touch an already-terminal row,
    never raises, and composes legal hops so ``apply_transition`` stays the
    sole writer of ``builds.status``.
    """
    from forge.cli._conductor_outcome import finish_mode_c_build
    from forge.lifecycle.persistence import StageLogEntry
    from forge.lifecycle.state_machine import BuildState

    def close_out(*, build_id: str, report: Any) -> None:
        now = datetime.now(timezone.utc)
        rationale = getattr(report, "rationale", "") or ""
        try:
            pool.record_stage(
                StageLogEntry(
                    build_id=build_id,
                    stage_label="conductor-close-out",
                    target_kind="local_tool",
                    target_identifier="conductor",
                    status="PASSED",
                    gate_mode=None,
                    coach_score=None,
                    threshold_applied=None,
                    started_at=now,
                    completed_at=now,
                    duration_secs=0.0,
                    details={
                        "outcome": getattr(
                            getattr(report, "outcome", None), "value", None
                        ),
                        "rationale": rationale,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — close-out is best-effort
            logger.warning(
                "conductor close-out: record_stage raised %s: %s for "
                "build_id=%s — the terminal stands, the row is missing",
                type(exc).__name__,
                exc,
                build_id,
            )

        token = _terminal_decision_token(report)
        if token is None:
            logger.info(
                "conductor close-out: build_id=%s ended on an outcome this "
                "seam does not adjudicate (no Mode C terminal decision on the "
                "report) — leaving builds.status to its own writer",
                build_id,
            )
            return

        if token.startswith(_TERMINAL_TOKEN_CLEAN_PREFIX):
            to_state = BuildState.COMPLETE
        elif token == _TERMINAL_TOKEN_FAILED:
            to_state = BuildState.FAILED
        else:
            # An outcome word this seam has never seen. Leaving the row
            # RUNNING is the failure this whole change exists to end, and
            # guessing COMPLETE would claim a delivery — so it is FAILED,
            # and the reason names the unknown word rather than hiding it.
            logger.error(
                "conductor close-out: build_id=%s reached terminal outcome "
                "%r, which this seam does not recognise — marking the row "
                "FAILED and naming the word rather than leaving it RUNNING",
                build_id,
                token,
            )
            to_state = BuildState.FAILED

        summary = _one_line(rationale) or f"fix journey terminal: {token}"
        finish_mode_c_build(
            pool,
            build_id,
            to_state=to_state,
            summary=summary,
            what="the fix journey's terminal close-out",
            log=logger,
        )

    return close_out


def make_conductor_queue_release(
    *,
    pool: Any,
    take_ack_handle: Callable[[str], Any] | None = None,
) -> Callable[[str], Any]:
    """Build the seam that lets the next build start when a journey ends.

    The pipeline consumer takes ONE build message at a time: it holds that
    message unacknowledged for the whole build and every later build waits
    behind it. For a routine build the lifecycle bridge watches the run and
    releases the message when the run ends. For a fix journey the bridge
    deliberately stands down — nothing it can see says when the journey is
    over — so the release has to come from the conductor, and until now
    nothing did it. On 2026-09-08 two journeys closed (one FAILED, one
    cancelled) with their messages still held, forge-prod's health line
    said the slot was held, a restart would not cure it (the boot check
    read the held message as a live build, correctly), and the message had
    to be pulled off the stream by hand.

    The returned ``async (build_id) -> None``:

    1. reads the build's feature id off its row (that is the name the
       bridge files the handle under);
    2. takes the handle — taking it, not borrowing it, so a second
       close-out for the same build cannot acknowledge twice;
    3. acknowledges the message once.

    Anything missing — no bridge this boot, no row, a journey that began
    before the bridge attached, a second close-out — is one plain log line
    and nothing else. It is never an error: a journey ending with no
    message to release is an ordinary thing.
    """

    async def release_queue_message(build_id: str) -> None:
        if take_ack_handle is None:
            logger.info(
                "conductor close-out: build_id=%s — no lifecycle bridge is "
                "wired this boot, so there is no queued message to release",
                build_id,
            )
            return

        feature_id: str | None = None
        try:
            row = pool.get_build_row(build_id)
            feature_id = getattr(row, "feature_id", None) if row else None
        except Exception as exc:  # noqa: BLE001 — a read must not end a terminal
            logger.warning(
                "conductor close-out: could not read the build row for "
                "build_id=%s (%s: %s) — the queued message is not released "
                "here; the queue frees it at the redelivery",
                build_id,
                type(exc).__name__,
                exc,
            )
            return

        if not feature_id:
            logger.info(
                "conductor close-out: build_id=%s has no feature id on its "
                "row, so there is no queued message to release",
                build_id,
            )
            return

        handle = take_ack_handle(feature_id)
        if handle is None:
            logger.info(
                "conductor close-out: build_id=%s (%s) — no queued message is "
                "held for this build, so there is nothing to release (it was "
                "released already, or the journey started before the bridge "
                "attached)",
                build_id,
                feature_id,
            )
            return

        await handle.ack()
        logger.info(
            "conductor close-out: build_id=%s (%s) — released the queued "
            "message; the next build can start",
            build_id,
            feature_id,
        )

    return release_queue_message


#: ``builds.error`` is a one-line column and ``forge status`` renders it in
#: a table cell. The terminal rationale can carry a multi-line leg banner.
_ERROR_COLUMN_LIMIT: int = 500


def _one_line(text: str) -> str:
    """Collapse ``text`` to one trimmed line for ``builds.error``."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > _ERROR_COLUMN_LIMIT:
        return collapsed[: _ERROR_COLUMN_LIMIT - 1] + "…"
    return collapsed


def make_conductor_subscribe_resume(
    *,
    pool: Any,
    subscriber_factory: Callable[..., Any],
    expected_approver: str | None = None,
    default_stage_label: str | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Build the driver's ``subscribe_resume`` seam over a real subscriber.

    **Reused, not invented** (shadow-replay item 3). The subscriber this
    composes over is the SAME ``ApprovalSubscriber`` the spec-writer
    driver and ``rearm_paused_gates`` both wait on, reached through the
    SAME ``(expected_approver, armed) -> subscriber`` factory shape, and
    awaited through its ONE public method::

        await_response(build_id, *, stage_label, attempt_count,
                       timeout_seconds)

    That last point is the correction this function exists for. The Stage-2
    seam called ``wait_for_response(request_id, timeout_seconds=…)``, a
    method no subscriber in this tree has — so the moment a real
    subscriber had been wired behind it, every wait would have died on an
    ``AttributeError`` inside the waiter and the journey would have
    reported a wait expiry. It was never exercised because the composition
    passed ``subscriber_factory=None``; an unwired seam hid a broken one.

    ``stage_label`` and ``attempt_count`` are not invented here either:
    they are READ BACK out of the durable
    ``builds.pending_approval_request_id`` with
    :func:`~forge.gating.identity.parse_request_id`, which is exactly what
    the rearm path does with the same column. The persisted id is the
    durable home of that pair, so the wait re-derives its refresh ids the
    same way the pause published them — no second column, no guess.

    An unparseable id (a legacy row) degrades to ``default_stage_label``
    with attempt 0, logged plainly, rather than refusing the wait.
    """
    from forge.gating.identity import parse_request_id
    from forge.pipeline.merge_ready_checkpoint import MERGE_READY_CHECKPOINT_LABEL

    fallback_label = default_stage_label or MERGE_READY_CHECKPOINT_LABEL

    async def subscribe_resume(
        build_id: str,
        *,
        armed: asyncio.Event,
        timeout_seconds: int,
    ) -> Any:
        row = pool.get_build_row(build_id)
        request_id = getattr(row, "pending_approval_request_id", None)
        if not request_id:
            # Nothing to wait on. Arm so the driver's arm-timeout does not
            # fire, then answer immediately; the window reader will report
            # the wait resolved on the next iteration.
            armed.set()
            return None
        try:
            _bid, stage_label, attempt_count = parse_request_id(str(request_id))
        except Exception as exc:  # noqa: BLE001 — a legacy id is not fatal
            logger.warning(
                "conductor resume: pending_approval_request_id=%r for "
                "build_id=%s does not parse (%s: %s) — waiting under the "
                "default stage label %r at attempt 0",
                request_id,
                build_id,
                type(exc).__name__,
                exc,
                fallback_label,
            )
            stage_label, attempt_count = fallback_label, 0
        subscriber = subscriber_factory(expected_approver, armed)
        return await subscriber.await_response(
            build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
            timeout_seconds=timeout_seconds,
        )

    return subscribe_resume


def build_conductor_driver_deps_factory(
    *,
    pool: Any,
    config: Any,
    subscriber_factory: Callable[..., Any] | None = None,
    republish_pending: Callable[[str], Any] | None = None,
    receipts_root: "Path | str | None" = None,
    source_build_id_reader: Callable[[str], str | None] | None = None,
    take_ack_handle: Callable[[str], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[str, Any], ConductorDriverDeps]:
    """Return the ``(build_id, supervisor) -> ConductorDriverDeps`` factory.

    Fills every seam Stage 1 left ``None``:

    * ``wait_window_reader`` — :func:`make_conductor_wait_window_reader`.
    * ``subscribe_resume`` — the approval/resume signal, composed over the
      injected ``subscriber_factory`` (``(expected_approver, armed) ->
      subscriber``, the shape the live-proven spec-writer driver and the
      gate rearm both use) by
      :func:`make_conductor_subscribe_resume`. The subscription sets
      ``armed`` as its FIRST action, which is what makes arm-before-post
      real rather than aspirational. ``None`` leaves the seam unwired, and
      the driver then stops loudly rather than spin-polling — the honest
      degrade, never a busy wait.
    * ``escalation_resolved`` — whether a budget escalation has cleared,
      read off the build row for the report's rationale.
    * ``export_stage_receipts`` / ``write_failure_pack`` / ``close_out`` —
      the receipts fold, both directions (design pass §b.2).
    * ``release_queue_message`` — :func:`make_conductor_queue_release`,
      composed over the lifecycle bridge's ``take_ack_handle``. It is what
      lets the next build start when a fix journey ends; ``take_ack_handle``
      ``None`` (no bridge this boot, and every test here) leaves it saying
      so in one line and doing nothing.

    The bus seam is the ONLY one that touches NATS, and it arrives
    injected, so every test here runs network-free.
    """
    read_window = make_conductor_wait_window_reader(
        pool=pool, config=config, clock=clock
    )
    export = make_conductor_receipts_exporter(
        pool=pool, receipts_root=receipts_root, config=config
    )
    write_pack = make_conductor_failure_pack_writer(
        pool=pool,
        receipts_root=receipts_root,
        source_build_id_reader=source_build_id_reader,
    )
    close_out = make_conductor_close_out(pool=pool)
    release_queue_message = make_conductor_queue_release(
        pool=pool, take_ack_handle=take_ack_handle
    )
    expected_approver = getattr(config.approval, "expected_approver", None)

    subscribe_resume = (
        None
        if subscriber_factory is None
        else make_conductor_subscribe_resume(
            pool=pool,
            subscriber_factory=subscriber_factory,
            expected_approver=expected_approver,
        )
    )

    def escalation_resolved(build_id: str) -> bool:
        row = pool.get_build_row(build_id)
        if row is None:
            return False
        return not getattr(row, "pending_approval_request_id", None)

    def deps_factory(build_id: str, supervisor: Any) -> ConductorDriverDeps:
        return ConductorDriverDeps(
            supervisor=supervisor,
            wait_window_reader=read_window,
            subscribe_resume=subscribe_resume,
            republish_pending=republish_pending,
            escalation_resolved=escalation_resolved,
            export_stage_receipts=export,
            write_failure_pack=write_pack,
            close_out=close_out,
            release_queue_message=release_queue_message,
        )

    return deps_factory
