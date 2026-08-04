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
import logging
import subprocess
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Sequence

from forge.pipeline.conductor_driver import ConductorDriverDeps, WaitWindow
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
CONDUCTOR_REVIEW_STAGE_TIMEOUT_SECONDS: int = 600

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
    "TOOLCHAIN_MODULE_CANDIDATES",
    "build_conductor_driver_deps_factory",
    "build_conductor_supervisor_factory",
    "load_declared_toolchain",
    "make_conductor_close_out",
    "make_conductor_failure_pack_writer",
    "make_conductor_receipts_exporter",
    "make_conductor_merge_card_published_probe",
    "make_conductor_subscribe_resume",
    "make_conductor_wait_window_reader",
    "make_gates_green_reader",
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
    return completed.returncode, (
        f"`{command}` exited {completed.returncode} in {cwd}"
        + (f" — last line: {tail[-1]}" if tail else "")
    )


def make_gates_green_reader(
    *,
    pool: Any,
    config: Any,
    declaration_loader: Callable[..., Any] | None = None,
    command_runner: Callable[..., Any] | None = None,
    repo_root_reader: Callable[[str], Any] | None = None,
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

    Every UNKNOWN is red-safe by the checkpoint's own precondition
    ("proven green", never "not proven red"), so every degrade on this
    path fails towards *no card*, never towards a card.

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
    """
    from forge.pipeline.merge_ready_checkpoint import GateStatus, GatesReport

    _load = declaration_loader or load_declared_toolchain
    _run = command_runner or _run_declared_command

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

    def read_gates(*, build_id: str, branch: Any = None) -> Any:
        row = pool.get_build_row(build_id)
        if row is None:
            return _unknown("no builds row to read a worktree from", build_id)
        worktree = getattr(row, "worktree_path", None)
        if not worktree:
            return _unknown(
                "the build row carries no worktree_path, so there is nowhere "
                "to run the gate set",
                build_id,
            )
        if not Path(worktree).is_dir():
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

        declaration = _load(repo_root)
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
            exit_code, detail = _run(
                command=command, cwd=worktree, timeout_seconds=timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 — a runner defect is not green
            return _unknown(
                f"running the declared test command raised "
                f"{type(exc).__name__}: {exc}",
                build_id,
            )

        if exit_code is None:
            return _unknown(detail, build_id)
        if exit_code == 0:
            logger.info(
                "conductor gates: build_id=%s — GREEN. %s", build_id, detail
            )
            return GatesReport(status=GateStatus.GREEN, detail=detail)
        logger.warning(
            "conductor gates: build_id=%s — RED. %s. No merge card is "
            "published; the fix cycle runs BEFORE the merge word",
            build_id,
            detail,
        )
        return GatesReport(
            status=GateStatus.RED,
            failed_gates=("declared toolchain test",),
            detail=detail,
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


def make_merge_ready_checkpoint(
    *,
    pool: Any,
    publish_card: Callable[..., Any] | None,
    gates_green_reader: Callable[..., Any] | None = None,
    has_commits_probe: Callable[[str], Any] | None = None,
    receipts_root: "Path | str | None" = None,
    published_probe: Callable[[str], Any] | None = None,
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
    """
    from forge.pipeline.fix_journey_receipts import write_fix_journey_failure_pack
    from forge.pipeline.merge_ready_checkpoint import MergeReadyCheckpointPublisher

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

    return MergeReadyCheckpointPublisher(
        publish_card=publish_card,
        gates_green_reader=gates_green_reader,
        has_commits_probe=has_commits_probe,
        branch_reader=_branch_reader,
        failure_pack_writer=_failure_pack_writer,
        published_probe=(
            published_probe
            if published_probe is not None
            else make_conductor_merge_card_published_probe(pool=pool)
        ),
    )


# ---------------------------------------------------------------------------
# The Supervisor factory (shakeout item 3)
# ---------------------------------------------------------------------------


def build_conductor_supervisor_factory(
    *,
    pool: Any,
    config: Any,
    forward_context_builder: Any,
    worktree_allowlist: Any,
    read_allowlist: "list[Path]",
    subprocess_runner: Any,
    lifecycle_emitter: Any = None,
    publish_approval_request: Any = None,
    publish_card: Callable[..., Any] | None = None,
    gates_green_reader: Callable[..., Any] | None = None,
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
            subprocess_runner=subprocess_runner,
            timeout_seconds_by_stage=stage_timeouts,
            leg_model=resolved_leg_model,
            leg_budgets=leg_budgets,
        )
        checkpoint = make_merge_ready_checkpoint(
            pool=pool,
            publish_card=publish_card,
            gates_green_reader=gates_green_reader,
            has_commits_probe=None,
            receipts_root=receipts_root,
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


def make_conductor_receipts_exporter(
    *,
    pool: Any,
    receipts_root: "Path | str | None" = None,
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
    """
    from forge.pipeline.fix_journey_receipts import export_stage_receipts

    def export(*, build_id: str, report: Any) -> Any:
        stage = getattr(report, "chosen_stage", None)
        stage_name = getattr(stage, "value", None)
        if not stage_name:
            return None
        row = pool.get_build_row(build_id)
        rationale = getattr(report, "rationale", "") or ""
        result = export_stage_receipts(
            build_id=build_id,
            stage=stage_name,
            worktree_path=getattr(row, "worktree_path", None),
            receipts_root=receipts_root,
            extra_files={"turn-rationale.txt": rationale} if rationale else None,
        )
        return getattr(result, "stage_key", None)

    return export


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

    The bus seam is the ONLY one that touches NATS, and it arrives
    injected, so every test here runs network-free.
    """
    read_window = make_conductor_wait_window_reader(
        pool=pool, config=config, clock=clock
    )
    export = make_conductor_receipts_exporter(pool=pool, receipts_root=receipts_root)
    write_pack = make_conductor_failure_pack_writer(
        pool=pool,
        receipts_root=receipts_root,
        source_build_id_reader=source_build_id_reader,
    )
    close_out = make_conductor_close_out(pool=pool)
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
        )

    return deps_factory
