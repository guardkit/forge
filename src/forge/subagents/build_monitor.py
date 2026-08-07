"""The BUILD MONITOR — semantic liveness for the guardkit autobuild subprocess.

Rich's 2026-07-30 ruling: *"hardcoding kill time limits isn't the way — the
forge should be able to monitor the autobuilds — it spews out enough
diagnostics."* Design of record:
``ai-transition/docs/build-monitor-design-pass-2026-07-31.md`` (stage 1).

The one-minute version
======================

Until this lane the pipeline supervised a running build with a blind one-hour
wall clock: a healthy multi-wave build got killed mid-work, and the killed
build was a total loss because the relaunch was hardwired to ``--fresh``. This
module replaces the clock with a monitor that reads the build's *own*
diagnostics — turns finished, tasks finished, waves finished, files actually
changing — and only calls a build stuck when **nothing meaningful has moved**
for a window the build's own declared budgets define.

The wedge rule (design §b, verbatim)
====================================

    A build is wedged when, for a bounded window W, there is (1) no semantic
    progress — no turn completion, no task status transition, no wave
    completion — AND (2) no state movement — no ``files_changed`` delta in any
    live task's progress.log, no inner-worktree HEAD movement, no
    content-bearing write to the feature YAML.

Three consequences are load-bearing and each has a test:

* **Heartbeats never reset the window.** A ``SNAPSHOT`` line in a task's
  progress.log, or an ``execution.last_updated`` tick, proves the *process* is
  alive; it does not prove the *build* is. A retry loop spinning in place emits
  heartbeats forever with a frozen ``files_changed`` — alive-looking,
  semantically dead. Raw output volume is in neither set: a spinner emitting
  megabytes counts for nothing.
* **Silence in one signal is not a wedge.** A torn feature-YAML read, a
  mid-append events.jsonl line and a tee failure are all "no evidence", never
  wedge evidence (design §j risk 2) — the monitor requires the *full* signal
  set silent before it calls wedge.
* **``events.jsonl`` is an attribution source, NOT a liveness signal.** Every
  LLM call appends an event, so a retrying-in-place task would keep the file
  growing; counting it as movement would re-introduce the very defect the
  ruling kills. Events are read here only to name the task/turn/verdict in the
  honest wedge report.

How W is derived (design §b)
============================

``W = task-budget + slack``, where ``slack = max(2 × task_log_interval, 120s)``
and the task budget is the **largest budget guardkit could still be enforcing
for a task that has not finished** (design §b). It is the MAX over five
sources, because every one of them can be the binding constraint and
under-deriving W kills a healthy build while over-deriving it only delays a
wedge call (design §j risk 6):

1. the per-task ``INFO`` budget logs — ``[TASK-X] Raising task_timeout to
   estimate-derived floor: ... = <N>s (feature default was <M>s)``,
   ``[TASK-X] Per-task task_timeout override active: ... → <N>s (feature
   default was <M>s)``, and the wave's ``Starting parallel gather for wave
   <k>: tasks=[...], task_timeout=<M>s (per-task=[TASK-A=<N>s, ...])``. These
   are the only lines that carry a budget guardkit actually raised ABOVE the
   run-level banner, so parsing them is what keeps the guardkit-fires-first
   invariant true for a task with a large ``estimated_minutes`` or a
   frontmatter override. A per-task budget is dropped once the ledger reports
   that task terminal;
2. the feature-level numbers those lines carry (``feature default was <M>s``,
   ``task_timeout=<M>s``) and the once-per-run ``Starting Wave Execution (task
   timeout: N min)`` banner (the banner floor-divides to whole minutes,
   under-reporting by <60s — absorbed by the ≥120s slack);
3. **guardkit's own resolution, reconstructed** — ``max(
   GUARDKIT_AUTOBUILD_TASK_TIMEOUT_FLOOR (3000s), yaml task_timeout (2400s
   default)) × timeout-multiplier`` (``feature_orchestrator.py:798-802``),
   where the multiplier mirrors ``detect_timeout_multiplier``
   (``agent_invoker.py:566-593``): ``GUARDKIT_TIMEOUT_MULTIPLIER`` if set, else
   4.0 when ``ANTHROPIC_BASE_URL`` points at localhost/127.0.0.1, else 1.0.
   This tier is ALWAYS in the max, never a "fallback": trusting the raw yaml
   number (or the 2400s default) was a 1.2×–4.8× UNDER-derivation — W=2520s
   against a real 3000s budget on the API seat and a real 12000s budget on the
   local M0 seat, i.e. SHORTER than the blind clock this lane replaces;
4. the **estimate-derived floor** guardkit applies per task —
   ``estimated_minutes × 60 × GUARDKIT_AUTOBUILD_ESTIMATE_TIMEOUT_FACTOR (1.5)
   × multiplier`` (``feature_orchestrator.py:3743-3757``) — read straight from
   the ``estimated_minutes`` field of the very feature YAML this module already
   parses. Scope: the tasks the ledger reports ``in_progress``; before any task
   is in flight (bootstrap, ``uv sync``, preflight, the wave-0 baseline probe —
   the whole prelude that runs BEFORE guardkit prints its banner at
   ``feature_orchestrator.py:2283-2286``) it is every not-yet-terminal task.
   forge's own shipped features carry estimates up to 240 minutes (25 tasks at
   113, nine at 170) → an enforced 10170s–21600s at multiplier 1.0 alone; a W
   that ignored this would fire 3.3× early on the 113-minute class;
5. a task's **own declared budget** — ``autobuild.task_timeout`` in the task
   markdown's frontmatter, ``× multiplier`` (``_resolve_task_timeout``). The
   2026-08-01 wedge rehearsal banked the gap verbatim: "the
   ``autobuild.task_timeout`` frontmatter override never reached the window
   derivation (W stayed on the 3000s default)". It cannot come from tier 3,
   because an explicit override REPLACES the feature-level number and its
   floor; and tier 1 sees it only once guardkit dispatches that task, so the
   whole prelude before that ran on a window the operator had already declared
   too small. Same scope as tier 4 (the tasks that could still be running),
   read from the same markdown guardkit reads. NOT mirrored: guardkit's
   ``GUARDKIT_MIN_TURN_BUDGET × --max-turns`` floor on top of an override —
   ``--max-turns`` is invisible to the monitor, and at guardkit's defaults that
   floor (3000s) is already tier 3's floor (see
   :func:`frontmatter_task_budget`).

Because W is therefore never below guardkit's own per-task budget, guardkit's
in-band timeout machinery fires FIRST whenever it is healthy — and an orderly
task failure IS semantic movement, so the monitor stays silent. The monitor
only ever fires when guardkit itself is wedged (event-loop starvation, a hung
thread past its cancel, a stalled model-switchboard socket, D-state) — the
exact class the blind clock was mis-covering. When nothing has been declared on
the stream yet, the derivation says so at WARNING (design §j risk 5: the
stage-1 parser must fail LOUD, never silent) — but it still returns a
reconstructed, floored number, never a folklore one.

No evidence is never wedge evidence (design §j risk 2)
======================================================

A wedge requires at least one ON-DISK signal to have been observed. A monitor
that has never read a ledger, a progress.log or an inner HEAD — a wrong
``root``/``feature_id``, a tree that does not exist — has observed nothing, so
it can conclude nothing, and it never calls wedge. Stdout ticks still reset the
window, but they are not on their own sufficient evidence to end a build.

M0 posture: regex + file polling. Zero model calls of any kind, no network, no
broker. The monitor is safe on the routine critical path by construction.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: Sentinel for "this signal has never been observed" — distinct from ``None``,
#: which is a legitimate observed value nowhere in this module but would blur
#: the never-seen / seen-as-empty line if reused.
_UNOBSERVED: Any = object()


# ---------------------------------------------------------------------------
# Knobs — every default is either read from the run or documented as guardkit's
# own default. None of them is a folklore kill time.
# ---------------------------------------------------------------------------

#: Kill switch for an operator who needs the pre-monitor behaviour back.
#: ``0`` / ``false`` / ``off`` (case-insensitive) disable the monitor; anything
#: else (including unset) leaves it armed.
BUILD_MONITOR_ENABLED_ENV: str = "FORGE_BUILD_MONITOR"

#: Poll cadence override, in seconds. Defaults to guardkit's own
#: ``--task-log-interval`` default (60s) so the monitor samples exactly as
#: often as the build writes its heartbeat.
BUILD_MONITOR_POLL_ENV: str = "FORGE_BUILD_MONITOR_POLL_SECONDS"

#: guardkit's ``TaskProgressLogger`` default interval (``progress_logger.py``)
#: — the heartbeat cadence, and therefore our poll cadence.
DEFAULT_TASK_LOG_INTERVAL_SECONDS: float = 60.0

#: guardkit's documented per-feature ``task_timeout`` default (2400s = 40 min)
#: — the RAW yaml value, which is NOT the budget guardkit enforces (it is
#: floored and multiplied first: see :func:`reconstruct_guardkit_task_budget`).
DEFAULT_TASK_TIMEOUT_SECONDS: float = 2400.0

#: guardkit floors the feature's ``task_timeout`` at 3000s BEFORE applying the
#: multiplier (``feature_orchestrator.py:796-802``, TASK-ABSR-FLOR), and reads
#: the floor from this env var at construction time. The monitor honours the
#: same var so its reconstruction stays a mirror, not a guess.
GUARDKIT_TASK_TIMEOUT_FLOOR_ENV: str = "GUARDKIT_AUTOBUILD_TASK_TIMEOUT_FLOOR"
DEFAULT_TASK_TIMEOUT_FLOOR_SECONDS: float = 3000.0

#: guardkit multiplies every resolved timeout by a backend multiplier
#: (``agent_invoker.py:566-593``): an explicit ``GUARDKIT_TIMEOUT_MULTIPLIER``
#: wins, else 4.0 when ``ANTHROPIC_BASE_URL`` points at a local backend, else
#: 1.0. The monitor cannot see the SUBPROCESS's backend env reliably, so with
#: no explicit override it assumes the slowest documented configuration — 4.0.
#: Over-assuming only delays a wedge call; under-assuming kills healthy builds.
GUARDKIT_TIMEOUT_MULTIPLIER_ENV: str = "GUARDKIT_TIMEOUT_MULTIPLIER"
LOCAL_BACKEND_TIMEOUT_MULTIPLIER: float = 4.0
BACKEND_BASE_URL_ENV: str = "ANTHROPIC_BASE_URL"
LOCAL_BACKEND_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1")

#: guardkit raises a task's budget to ``estimated_minutes × 60 × factor ×
#: multiplier`` when that exceeds the feature-level number
#: (``feature_orchestrator.py:3742-3757``). Same env var, same 1.5 default.
ESTIMATE_TIMEOUT_FACTOR_ENV: str = "GUARDKIT_AUTOBUILD_ESTIMATE_TIMEOUT_FACTOR"
DEFAULT_ESTIMATE_TIMEOUT_FACTOR: float = 1.5

#: Statuses that mean a task can no longer be burning a per-task budget. Any
#: other status (including an unknown one) is treated as still in flight — the
#: safe direction, since it can only make W larger.
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "skipped", "cancelled", "blocked"}
)

#: Floor on the window slack (design §b: ``slack = max(2 × interval, 120s)``).
MIN_WINDOW_SLACK_SECONDS: float = 120.0

#: Where the window's task budget came from — named on every verdict and in
#: the failure pack, so a review can see whether W was DERIVED or RECONSTRUCTED.
WINDOW_SOURCE_TASK_BUDGET_LOG: str = "per-task-budget-log"
WINDOW_SOURCE_BANNER: str = "wave-execution-banner"
WINDOW_SOURCE_RECONSTRUCTED: str = "reconstructed-from-feature-yaml"
WINDOW_SOURCE_ESTIMATE_FLOOR: str = "feature-yaml-estimate-floor"
WINDOW_SOURCE_FRONTMATTER_OVERRIDE: str = "task-frontmatter-override"

#: Relative paths, inside the build's cwd, of the artifacts the monitor reads.
FEATURES_DIR: str = ".guardkit/features"
AUTOBUILD_DIR: str = ".guardkit/autobuild"
INNER_WORKTREES_DIR: str = ".guardkit/worktrees"
PROGRESS_LOG_NAME: str = "progress.log"
EVENTS_LOG_NAME: str = "events.jsonl"

#: Where a task's markdown lives, and the order guardkit's own ``TaskLoader``
#: searches (``guardkit/tasks/task_loader.py``: ``tasks/<state>/**/<id>*.md``).
#: ``completed`` is deliberately absent from the search — a finished task
#: cannot still be burning a budget, and the monitor only ever asks about tasks
#: that are still in flight.
TASKS_DIR: str = "tasks"
TASK_SEARCH_DIRS: tuple[str, ...] = (
    "backlog",
    "in_progress",
    "design_approved",
    "in_review",
    "blocked",
)


def monitor_enabled() -> bool:
    """Is the build monitor armed? (Default: yes.)"""
    raw = os.environ.get(BUILD_MONITOR_ENABLED_ENV, "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def resolve_poll_interval_seconds() -> float:
    """Poll cadence in seconds — env override, else the heartbeat cadence.

    A malformed or non-positive value falls back to the default rather than
    raising: a stray env typo must never crash a build.
    """
    raw = os.environ.get(BUILD_MONITOR_POLL_ENV, "").strip()
    if not raw:
        return DEFAULT_TASK_LOG_INTERVAL_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        logger.warning(
            "build_monitor: %s=%r is not a number — using the default %ss",
            BUILD_MONITOR_POLL_ENV,
            raw,
            DEFAULT_TASK_LOG_INTERVAL_SECONDS,
        )
        return DEFAULT_TASK_LOG_INTERVAL_SECONDS
    if parsed <= 0:
        return DEFAULT_TASK_LOG_INTERVAL_SECONDS
    return parsed


def derive_window_seconds(
    task_timeout_seconds: float,
    *,
    task_log_interval: float = DEFAULT_TASK_LOG_INTERVAL_SECONDS,
) -> float:
    """The wedge window W, derived from the build's own per-task budget.

    ``W = task_timeout + max(2 × task_log_interval, 120s)`` (design §b). The
    slack keeps the monitor strictly OUTSIDE guardkit's own budget so guardkit
    always gets to fail a task in-band first — a monitor kill while a task
    timeout was pending is a monitor defect, not a build defect (design §j
    risk 6).
    """
    slack = max(2.0 * task_log_interval, MIN_WINDOW_SLACK_SECONDS)
    return float(task_timeout_seconds) + slack


def resolve_timeout_multiplier(env: Mapping[str, str] | None = None) -> float:
    """guardkit's timeout multiplier, mirrored exactly.

    guardkit resolves this as: explicit ``GUARDKIT_TIMEOUT_MULTIPLIER`` > 4.0
    when ``ANTHROPIC_BASE_URL`` points at localhost/127.0.0.1 > 1.0
    (``agent_invoker.py:566-593``). The monitor mirrors the same three rungs
    against the SAME environment the build subprocess is launched with (the
    runner hands it in), so the reconstructed budget is guardkit's number, not
    a guess. Getting this wrong low is the double-kill defect of design §j
    risk 6 — the monitor pre-empting guardkit's own in-band timeout.
    """
    source = os.environ if env is None else env
    raw = str(source.get(GUARDKIT_TIMEOUT_MULTIPLIER_ENV, "") or "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "build_monitor: %s=%r is not a number — falling back to "
                "guardkit's own auto-detection for the wedge window",
                GUARDKIT_TIMEOUT_MULTIPLIER_ENV,
                raw,
            )
        else:
            if value > 0:
                return max(0.1, value)  # guardkit's own clamp
    base_url = str(source.get(BACKEND_BASE_URL_ENV, "") or "")
    if any(host in base_url for host in LOCAL_BACKEND_HOSTS):
        return LOCAL_BACKEND_TIMEOUT_MULTIPLIER
    return 1.0


def resolve_estimate_timeout_factor(env: Mapping[str, str] | None = None) -> float:
    """guardkit's safety factor on a task's own ``estimated_minutes``.

    ``_estimate_timeout_factor`` (``feature_orchestrator.py:127-145``): 1.5 by
    default, operator policy via ``GUARDKIT_AUTOBUILD_ESTIMATE_TIMEOUT_FACTOR``,
    with a non-positive/unparseable value falling back to the default.
    """
    source = os.environ if env is None else env
    raw = str(source.get(ESTIMATE_TIMEOUT_FACTOR_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_ESTIMATE_TIMEOUT_FACTOR
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ESTIMATE_TIMEOUT_FACTOR
    return value if value > 0 else DEFAULT_ESTIMATE_TIMEOUT_FACTOR


def estimate_floor_seconds(
    estimated_minutes: float | None, *, env: Mapping[str, str] | None = None
) -> float:
    """guardkit's estimate-derived per-task timeout floor, mirrored.

    ``estimated_minutes × 60 × factor × multiplier``
    (``_task_estimate_floor_seconds``, ``feature_orchestrator.py:3742-3757``),
    ``0`` for a missing/non-positive estimate. guardkit raises a task's budget
    to this whenever it exceeds the feature-level number, and it does so
    WITHOUT any announcement the monitor can rely on reaching stdout — so the
    monitor computes it from the same ``estimated_minutes`` field it already
    reads out of the feature YAML.
    """
    if estimated_minutes is None or estimated_minutes <= 0:
        return 0.0
    return (
        float(estimated_minutes)
        * 60.0
        * resolve_estimate_timeout_factor(env)
        * resolve_timeout_multiplier(env)
    )


def resolve_task_timeout_floor(env: Mapping[str, str] | None = None) -> float:
    """guardkit's pre-multiplier per-task timeout floor, mirrored."""
    source = os.environ if env is None else env
    raw = str(source.get(GUARDKIT_TASK_TIMEOUT_FLOOR_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_TASK_TIMEOUT_FLOOR_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "build_monitor: %s=%r is not a number — using guardkit's documented "
            "%ss floor for the wedge window",
            GUARDKIT_TASK_TIMEOUT_FLOOR_ENV,
            raw,
            DEFAULT_TASK_TIMEOUT_FLOOR_SECONDS,
        )
        return DEFAULT_TASK_TIMEOUT_FLOOR_SECONDS
    # guardkit permits 0 to DISABLE the floor; a negative value is nonsense.
    return max(value, 0.0)


def reconstruct_guardkit_task_budget(
    raw_yaml_task_timeout: float | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> float:
    """The budget guardkit will ACTUALLY enforce, from the feature YAML.

    ``max(floor, raw yaml task_timeout) × multiplier`` — the arithmetic at
    ``feature_orchestrator.py:796-802``. The raw YAML number on its own is NOT
    guardkit's budget: with the 3000s floor and a local backend's 4.0
    multiplier, a feature declaring the 2400s default is enforced at 12000s.
    Deriving W from the raw 2400 gave a window of 2520s — 4.8× under, and
    shorter than the blind 3600s clock this lane exists to replace.
    """
    raw = (
        raw_yaml_task_timeout
        if raw_yaml_task_timeout and raw_yaml_task_timeout > 0
        else DEFAULT_TASK_TIMEOUT_SECONDS
    )
    return max(resolve_task_timeout_floor(env), float(raw)) * resolve_timeout_multiplier(
        env
    )


def frontmatter_task_budget(
    override_seconds: float, *, env: Mapping[str, str] | None = None
) -> float:
    """The budget guardkit enforces for a task that DECLARES its own timeout.

    ``frontmatter.autobuild.task_timeout × multiplier``
    (``_resolve_task_timeout``, ``feature_orchestrator.py``): an explicit
    per-task override REPLACES the feature-level number and the feature-level
    floor — the whole reconstruction tier — so a task declaring 7200s on a
    local backend is enforced at 28800s while the reconstruction tier still
    reads 12000s.

    NOT mirrored: guardkit then raises this to ``GUARDKIT_MIN_TURN_BUDGET
    (600s) × --max-turns``, and ``--max-turns`` is a CLI flag of the subprocess
    that the monitor cannot see. That floor only ever RAISES guardkit's budget,
    and at guardkit's own defaults it is 600 × 5 = 3000s — exactly the
    reconstruction tier's floor, which is already in the max. So the gap is a
    non-default ``--max-turns`` on a multiplier-1.0 seat, where W can sit below
    guardkit's own budget; the honest fix for that is a budgets manifest
    (design §h stage 3), not a guess here.
    """
    return float(override_seconds) * resolve_timeout_multiplier(env)


def parse_frontmatter_task_timeout(text: str) -> float | None:
    """``autobuild.task_timeout`` from a task markdown's YAML frontmatter.

    ``None`` for anything that is not a declared, usable override — no
    frontmatter block, no ``autobuild`` mapping, no ``task_timeout``, a value
    guardkit itself would reject. guardkit reads the same field through
    ``TaskLoader`` and coerces with ``int()``, warning and falling back to the
    feature-level budget on a non-integer or non-positive value
    (``feature_orchestrator.py``); the mirror rejects exactly those, so a
    malformed override never inflates W.
    """
    body = text.lstrip("﻿")  # a BOM must not hide the frontmatter fence
    if not body.startswith("---"):
        return None
    lines = body.splitlines()
    end: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            end = index
            break
    if end is None:  # an unterminated block is not a frontmatter block
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    if not isinstance(data, Mapping):
        return None
    autobuild = data.get("autobuild")
    if not isinstance(autobuild, Mapping):
        return None
    try:
        declared = int(autobuild.get("task_timeout"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return float(declared) if declared > 0 else None


def find_task_file(
    root: Path, task_id: str, declared_path: str | None = None
) -> Path | None:
    """The task's markdown file under ``root``, or ``None``.

    ``declared_path`` is the feature YAML's own ``file_path`` for the task and
    is tried first because it is exact. It goes STALE by design, though —
    guardkit moves a task's file between ``tasks/<state>/`` directories as the
    task progresses — so the fallback is guardkit's own discovery: the first
    ``tasks/<state>/**/<task_id>*.md`` in ``TaskLoader``'s search order.
    Never raises: an unreadable tree is no evidence.
    """
    root = Path(root)
    if declared_path:
        candidate = Path(declared_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            pass
    for dir_name in TASK_SEARCH_DIRS:
        search_dir = root / TASKS_DIR / dir_name
        try:
            if not search_dir.is_dir():
                continue
            for path in sorted(search_dir.rglob(f"{task_id}*.md")):
                return path
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# (a) The stdout grammar — semantic ticks vs heartbeats
# ---------------------------------------------------------------------------
#
# Grammar pinned by the design pass's signal inventory (guardkit
# orchestrator/progress.py, feature_orchestrator.py, worktree_checkpoints.py).
# Anything NOT listed here is noise for liveness purposes — including every
# heartbeat/elapsed/spinner line. Stdout is a liveness corroborator and the
# durable narrative; it is BANNED as an attribution source (design §c / §j
# risk 8) — the counts below never become ``tasks_completed``.

#: ``INFO:...progress:[<ISO>] Started turn <N>: <phase>``
_TURN_STARTED_RE = re.compile(r"Started\s+turn\s+(\d+)\s*:", re.IGNORECASE)

#: ``INFO:...progress:[<ISO>] Completed turn <N>: success|feedback - <summary>``
_TURN_COMPLETED_RE = re.compile(
    r"Completed\s+turn\s+(\d+)\s*:\s*(success|feedback)\s*-", re.IGNORECASE
)

#: ``[guardkit-checkpoint] Turn <N> complete (tests: pass|fail|...)``
_CHECKPOINT_RE = re.compile(
    r"\[guardkit-checkpoint\]\s+Turn\s+(\d+)\s+complete", re.IGNORECASE
)

#: ``▶ Executing TASK-<X>: <name>``
_TASK_STARTED_RE = re.compile(r"Executing\s+(TASK-[A-Za-z0-9_.\-]+)")

#: ``⏭ Skipping TASK-<X> (already completed)`` — the resume path honouring state.
_TASK_SKIPPED_RE = re.compile(
    r"Skipping\s+(TASK-[A-Za-z0-9_.\-]+)\s*\(already\s+completed\)", re.IGNORECASE
)

#: ``Wave <k>/<K>: TASK-A, TASK-B``
_WAVE_STARTED_RE = re.compile(r"\bWave\s+(\d+)\s*/\s*(\d+)\s*:")

#: ``Wave <k> ✓ PASSED: n passed, m failed`` (and the FAILED counterpart).
_WAVE_FINISHED_RE = re.compile(
    r"\bWave\s+(\d+)\b[^\n]*?\b(PASSED|FAILED)\b", re.IGNORECASE
)

#: ``Starting Wave Execution (task timeout: N min)`` — the build declaring its
#: OWN per-task budget. This is the window-derivation input, not a tick.
_TASK_TIMEOUT_BANNER_RE = re.compile(
    r"task\s+timeout:\s*(\d+(?:\.\d+)?)\s*(min(?:ute)?s?|s(?:ec(?:onds?)?)?)\b",
    re.IGNORECASE,
)


def parse_task_timeout_banner(line: str) -> float | None:
    """Extract the run's declared per-task budget, in seconds.

    Matches guardkit's once-per-run banner ``Starting Wave Execution (task
    timeout: 40 min)``. Returns ``None`` when the line does not carry a
    budget — the caller then keeps whatever it already had.
    """
    match = _TASK_TIMEOUT_BANNER_RE.search(line)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:  # pragma: no cover — the regex only matches numbers
        return None
    if value <= 0:
        return None
    unit = match.group(2).lower()
    return value * 60.0 if unit.startswith("min") else value


#: ``[TASK-X] Raising task_timeout to estimate-derived floor: estimated_minutes=
#: 60 × 60 × 1.5 × multiplier=4.0 = 21600s (feature default was 12000s)``
_ESTIMATE_FLOOR_BUDGET_RE = re.compile(
    r"Raising\s+task_timeout\s+to\s+estimate-derived\s+floor:.*?"
    r"=\s*(?P<effective>\d+(?:\.\d+)?)s\s*"
    r"\(feature\s+default\s+was\s+(?P<default>\d+(?:\.\d+)?)s",
    re.IGNORECASE,
)

#: ``[TASK-X] Per-task task_timeout override active: frontmatter=7200s ×
#: multiplier=4.0 = 28800s, floored at 3000s → 28800s (feature default was
#: 12000s)``
_OVERRIDE_BUDGET_RE = re.compile(
    r"Per-task\s+task_timeout\s+override\s+active:.*?"
    r"(?:→|->)\s*(?P<effective>\d+(?:\.\d+)?)s\s*"
    r"\(feature\s+default\s+was\s+(?P<default>\d+(?:\.\d+)?)s",
    re.IGNORECASE,
)

#: The rejected-override WARNINGs: no effective budget, but they DO carry the
#: feature-level number in exact seconds — better than the banner's minutes.
_FEATURE_DEFAULT_ONLY_RE = re.compile(
    r"feature-level\s+task_timeout=(?P<default>\d+(?:\.\d+)?)s", re.IGNORECASE
)


#: ``Starting parallel gather for wave 2: tasks=['TASK-A', 'TASK-B'],
#: task_timeout=12000s (per-task=[TASK-A=12000s, TASK-B=21600s])``
#: (``feature_orchestrator.py:3229-3233``) — the richest budget line guardkit
#: emits: the feature-level number AND every task's effective budget.
_WAVE_GATHER_BUDGET_RE = re.compile(
    r"Starting\s+parallel\s+gather\s+for\s+wave\s+\d+:.*?"
    r"task_timeout=(?P<default>\d+(?:\.\d+)?)s",
    re.IGNORECASE,
)

#: The ``per-task=[...]`` pairs inside that line.
_PER_TASK_PAIR_RE = re.compile(r"([A-Za-z0-9_.\-]+)=(\d+(?:\.\d+)?)s")

#: The ``[TASK-X]`` prefix guardkit stamps on its per-task budget logs.
_BRACKETED_TASK_ID_RE = re.compile(r"\[([A-Za-z0-9_.\-]*TASK-[A-Za-z0-9_.\-]+)\]")


@dataclass(frozen=True)
class BudgetDeclaration:
    """Per-task and feature-level budgets declared on one stdout line."""

    per_task: tuple[tuple[str, float], ...] = ()
    feature_level: float | None = None

    def __bool__(self) -> bool:
        return bool(self.per_task) or self.feature_level is not None


def parse_budget_declaration(line: str) -> BudgetDeclaration:
    """Budgets guardkit declares for itself on one line.

    Three grammars, all pinned (``feature_orchestrator.py``):

    * ``Starting parallel gather for wave <k>: tasks=[...],
      task_timeout=<M>s (per-task=[TASK-A=<N>s, ...])`` (:3229-3233);
    * ``[TASK-X] Raising task_timeout to estimate-derived floor: ... = <N>s
      (feature default was <M>s)`` (:3919-3928);
    * ``[TASK-X] Per-task task_timeout override active: ... → <N>s (feature
      default was <M>s)`` (:3956-3962), plus the rejected-override warnings
      that carry only ``feature-level task_timeout=<M>s``.

    These are the only lines that announce a budget guardkit raised ABOVE the
    run-level banner. A task with ``estimated_minutes ≥ 34`` or a frontmatter
    override runs on a budget the banner never mentions; deriving W from the
    banner alone would put the monitor INSIDE guardkit's own timeout for that
    task — the double-kill defect of design §j risk 6.
    """
    gather = _WAVE_GATHER_BUDGET_RE.search(line)
    if gather is not None:
        tail = line[gather.end() :]
        pairs = tuple(
            (task_id, value)
            for task_id, raw in _PER_TASK_PAIR_RE.findall(tail)
            if (value := _positive_float(raw)) is not None
        )
        return BudgetDeclaration(
            per_task=pairs, feature_level=_positive_float(gather.group("default"))
        )

    for pattern in (_ESTIMATE_FLOOR_BUDGET_RE, _OVERRIDE_BUDGET_RE):
        match = pattern.search(line)
        if match is None:
            continue
        effective = _positive_float(match.group("effective"))
        feature_level = _positive_float(match.group("default"))
        task_match = _BRACKETED_TASK_ID_RE.search(line)
        per_task: tuple[tuple[str, float], ...] = ()
        if effective is not None:
            # An unattributable budget is still a budget: key it by the line's
            # own shape so it can never be dropped as "a finished task's".
            task_id = task_match.group(1) if task_match is not None else _UNATTRIBUTED
            per_task = ((task_id, effective),)
        return BudgetDeclaration(per_task=per_task, feature_level=feature_level)

    match = _FEATURE_DEFAULT_ONLY_RE.search(line)
    if match is not None:
        return BudgetDeclaration(feature_level=_positive_float(match.group("default")))
    return BudgetDeclaration()


#: Key for a per-task budget whose task id could not be read off the line. It
#: never matches a ledger task id, so it is never dropped as terminal.
_UNATTRIBUTED: str = "<unattributed>"


def _positive_float(raw: str) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):  # pragma: no cover — regex yields digits
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class SemanticEvent:
    """One semantic tick recognised on the build's stdout.

    ``kind`` is one of ``turn_started`` / ``turn_completed`` / ``checkpoint`` /
    ``task_started`` / ``task_skipped`` / ``wave_started`` / ``wave_finished``.
    """

    kind: str
    task_id: str | None = None
    turn: int | None = None
    decision: str | None = None
    wave: int | None = None


def classify_stdout_line(line: str) -> SemanticEvent | None:
    """Classify one stdout line as a semantic tick, or ``None`` for noise.

    ``None`` covers every heartbeat, ``elapsed=`` snapshot echo, spinner frame
    and traceback: they prove the process breathes, never that the build moved.
    """
    match = _TURN_COMPLETED_RE.search(line)
    if match is not None:
        return SemanticEvent(
            kind="turn_completed",
            turn=int(match.group(1)),
            decision=match.group(2).lower(),
        )
    match = _CHECKPOINT_RE.search(line)
    if match is not None:
        return SemanticEvent(kind="checkpoint", turn=int(match.group(1)))
    match = _TURN_STARTED_RE.search(line)
    if match is not None:
        return SemanticEvent(kind="turn_started", turn=int(match.group(1)))
    match = _TASK_SKIPPED_RE.search(line)
    if match is not None:
        return SemanticEvent(kind="task_skipped", task_id=match.group(1))
    match = _TASK_STARTED_RE.search(line)
    if match is not None:
        return SemanticEvent(kind="task_started", task_id=match.group(1))
    match = _WAVE_STARTED_RE.search(line)
    if match is not None:
        return SemanticEvent(kind="wave_started", wave=int(match.group(1)))
    match = _WAVE_FINISHED_RE.search(line)
    if match is not None:
        return SemanticEvent(kind="wave_finished", wave=int(match.group(1)))
    return None


# ---------------------------------------------------------------------------
# (b) The on-disk signals — the build's own ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureLedger:
    """The authoritative task/wave ledger — the file guardkit's resume trusts.

    Read from ``.guardkit/features/<FEAT>.yaml``. ``execution.last_updated`` is
    deliberately NOT part of this record: it ticks on every save and would make
    a heartbeat look like progress. Everything here is content-bearing.
    """

    tasks_completed: int
    tasks_failed: int
    current_wave: int
    completed_waves: int
    task_statuses: tuple[tuple[str, str], ...]
    in_progress_task_ids: tuple[str, ...]
    #: The feature YAML's RAW ``task_timeout``. Deliberately named "raw": it is
    #: NOT the budget guardkit enforces (that is floored and multiplied first),
    #: and using it directly as W under-derived the window 4.8×. It feeds
    #: :func:`reconstruct_guardkit_task_budget` and nothing else.
    raw_task_timeout_seconds: float | None
    #: ``(task_id, estimated_minutes)`` for every task that declares one. The
    #: input to guardkit's estimate-derived per-task floor, which it applies
    #: silently — so the monitor must compute it rather than wait to be told.
    task_estimates: tuple[tuple[str, float], ...] = ()
    #: ``(task_id, file_path)`` as the feature YAML declares it — the exact
    #: first guess at where a task's markdown (and therefore its
    #: ``autobuild.task_timeout`` frontmatter override) lives. Advisory only:
    #: guardkit moves task files between state directories as they run, so
    #: :func:`find_task_file` falls back to guardkit's own search.
    task_files: tuple[tuple[str, str], ...] = ()

    def fingerprint(self) -> tuple[Any, ...]:
        """The content-bearing view compared across polls."""
        return (
            self.tasks_completed,
            self.tasks_failed,
            self.current_wave,
            self.completed_waves,
            self.task_statuses,
        )

    def is_terminal(self, task_id: str) -> bool:
        """Has the ledger recorded this task as finished (any outcome)?"""
        for known_id, status in self.task_statuses:
            if known_id == task_id:
                return status in TERMINAL_TASK_STATUSES
        return False

    def live_task_ids(self) -> tuple[str, ...]:
        """Tasks that could still be burning a per-task budget.

        The ones the ledger calls ``in_progress`` when any are; otherwise every
        not-yet-terminal task — which is the honest answer during the prelude
        (bootstrap, ``uv sync``, preflight, the wave-0 baseline probe) that runs
        BEFORE guardkit prints its banner and before any task is in flight.
        """
        if self.in_progress_task_ids:
            return self.in_progress_task_ids
        return tuple(
            task_id
            for task_id, status in self.task_statuses
            if status not in TERMINAL_TASK_STATUSES
        )


def read_feature_ledger(root: Path, feature_id: str) -> FeatureLedger | None:
    """Parse ``<root>/.guardkit/features/<feature_id>.yaml``.

    Returns ``None`` on ANY trouble — missing file, torn/partial write,
    unparseable YAML, wrong shape. ``None`` means **no evidence**, never wedge
    evidence (design §j risk 2): the caller must not treat it as movement OR
    as silence.
    """
    path = Path(root) / FEATURES_DIR / f"{feature_id}.yaml"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        logger.debug(
            "build_monitor: feature yaml at %s did not parse (torn read?) — "
            "treated as NO EVIDENCE, not as a wedge signal",
            path,
        )
        return None
    if not isinstance(data, Mapping):
        return None

    execution = data.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}

    statuses: list[tuple[str, str]] = []
    in_progress: list[str] = []
    estimates: list[tuple[str, float]] = []
    files: list[tuple[str, str]] = []
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        for entry in tasks:
            if not isinstance(entry, Mapping):
                continue
            task_id = str(entry.get("id") or "")
            status = str(entry.get("status") or "unknown")
            if not task_id:
                continue
            statuses.append((task_id, status))
            if status == "in_progress":
                in_progress.append(task_id)
            estimate = _as_optional_float(entry.get("estimated_minutes"))
            if estimate is not None:
                estimates.append((task_id, estimate))
            file_path = entry.get("file_path")
            if isinstance(file_path, str) and file_path.strip():
                files.append((task_id, file_path.strip()))

    completed_waves = execution.get("completed_waves")
    completed_wave_count = (
        len(completed_waves) if isinstance(completed_waves, list) else 0
    )

    return FeatureLedger(
        tasks_completed=_as_int(execution.get("tasks_completed")),
        tasks_failed=_as_int(execution.get("tasks_failed")),
        current_wave=_as_int(execution.get("current_wave")),
        completed_waves=completed_wave_count,
        task_statuses=tuple(sorted(statuses)),
        in_progress_task_ids=tuple(sorted(in_progress)),
        raw_task_timeout_seconds=_as_optional_float(data.get("task_timeout")),
        task_estimates=tuple(sorted(estimates)),
        task_files=tuple(sorted(files)),
    )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


#: ``[<ISO>] SNAPSHOT TASK-X: elapsed=60s, phase=P, files_changed=N, last_tool=T``
_PROGRESS_SNAPSHOT_RE = re.compile(
    r"SNAPSHOT\s+(?P<task>[A-Za-z0-9_.\-]+)\s*:.*?"
    r"phase=(?P<phase>[^,]*),\s*files_changed=(?P<files>\d+)",
    re.IGNORECASE,
)

#: ``[<ISO>] START|COMPLETE|TIMEOUT TASK-X: ...`` — the non-heartbeat records.
_PROGRESS_MARKER_RE = re.compile(
    r"\b(?P<marker>START|COMPLETE|TIMEOUT)\s+(?P<task>[A-Za-z0-9_.\-]+)\s*:"
)


@dataclass(frozen=True)
class TaskProgress:
    """The last state a task's ``progress.log`` reports.

    ``files_changed`` and ``phase`` are the STATE-MOVEMENT signals; the number
    of ``SNAPSHOT`` heartbeats is deliberately absent — counting it would let a
    task spinning in a retry loop look alive forever.
    """

    task_id: str
    files_changed: int
    phase: str
    markers: int  # START / COMPLETE / TIMEOUT records — semantic, not heartbeat
    decision: str | None
    mtime: float

    def fingerprint(self) -> tuple[Any, ...]:
        return (self.task_id, self.files_changed, self.phase, self.markers)


#: ``COMPLETE TASK-X: elapsed=171s, decision=approved, snapshots=2``
_PROGRESS_DECISION_RE = re.compile(r"decision=(?P<decision>[A-Za-z_\-]+)")


def read_task_progress(root: Path) -> tuple[TaskProgress, ...] | None:
    """Read every ``.guardkit/autobuild/<task_id>/progress.log`` under ``root``.

    Returns ``None`` when the autobuild directory does not exist (no evidence);
    an empty tuple when it exists but holds no progress logs yet. Individual
    unreadable logs are skipped, never raised.
    """
    base = Path(root) / AUTOBUILD_DIR
    try:
        if not base.is_dir():
            return None
        entries = sorted(base.iterdir())
    except OSError:
        return None

    out: list[TaskProgress] = []
    for entry in entries:
        log_path = entry / PROGRESS_LOG_NAME
        try:
            if not log_path.is_file():
                continue
            text = log_path.read_text(encoding="utf-8", errors="replace")
            mtime = log_path.stat().st_mtime
        except OSError:
            continue
        parsed = _parse_progress_log(entry.name, text, mtime)
        if parsed is not None:
            out.append(parsed)
    return tuple(out)


def _parse_progress_log(
    task_id: str, text: str, mtime: float
) -> TaskProgress | None:
    files_changed = 0
    phase = ""
    markers = 0
    decision: str | None = None
    seen = False
    for line in text.splitlines():
        snapshot = _PROGRESS_SNAPSHOT_RE.search(line)
        if snapshot is not None:
            seen = True
            phase = snapshot.group("phase").strip()
            try:
                files_changed = int(snapshot.group("files"))
            except ValueError:  # pragma: no cover — regex guarantees digits
                pass
            continue
        marker = _PROGRESS_MARKER_RE.search(line)
        if marker is not None:
            seen = True
            markers += 1
            if marker.group("marker").upper() == "START":
                # A new SDK call: guardkit restarts the file-change count.
                files_changed = 0
            found = _PROGRESS_DECISION_RE.search(line)
            if found is not None:
                decision = found.group("decision")
    if not seen:
        return None
    return TaskProgress(
        task_id=task_id,
        files_changed=files_changed,
        phase=phase,
        markers=markers,
        decision=decision,
        mtime=mtime,
    )


def read_inner_head(root: Path, feature_id: str) -> str | None:
    """Resolve the inner build worktree's git HEAD sha, without running git.

    One checkpoint commit lands per turn, so HEAD movement is machine-checkable
    proof that the code state moved. Pure file reads (``.git`` pointer →
    ``HEAD`` → loose ref → ``packed-refs``); ANY trouble returns ``None`` =
    no evidence.
    """
    worktree = Path(root) / INNER_WORKTREES_DIR / feature_id
    try:
        git_path = worktree / ".git"
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            gitdir = Path(pointer.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (worktree / gitdir).resolve()
        elif git_path.is_dir():
            gitdir = git_path
        else:
            return None

        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        refname = head.split(":", 1)[1].strip()

        commondir = gitdir
        commondir_file = gitdir / "commondir"
        if commondir_file.is_file():
            raw = commondir_file.read_text(encoding="utf-8").strip()
            candidate = Path(raw)
            commondir = (
                candidate if candidate.is_absolute() else (gitdir / candidate).resolve()
            )

        for base in (gitdir, commondir):
            ref_path = base / refname
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip() or None

        packed = commondir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if not line or line.startswith(("#", "^")):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == refname:
                    return parts[0]
        return None
    except (OSError, ValueError):
        return None


def read_last_event(root: Path, feature_id: str) -> dict[str, Any] | None:
    """Last COMPLETE JSON line of ``.guardkit/autobuild/<FEAT>/events.jsonl``.

    ATTRIBUTION ONLY — never a liveness signal (see the module docstring): a
    task retrying in place keeps appending events, so counting them as movement
    would resurrect the defect this lane removes. A mid-append final line is
    skipped, exactly like a torn YAML read.
    """
    path = Path(root) / AUTOBUILD_DIR / feature_id / EVENTS_LOG_NAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue  # mid-append tail line — no evidence, not an error
        if isinstance(parsed, dict):
            return parsed
    return None


#: The three on-disk signal families, in digest order.
SIGNAL_LEDGER: str = "ledger"
SIGNAL_PROGRESS: str = "progress"
SIGNAL_HEAD: str = "inner-head"


@dataclass(frozen=True)
class StateDigest:
    """One observation of the build's content-bearing state.

    Every component is ``None`` when that signal produced NO EVIDENCE this
    poll — a torn YAML read, an absent ``.guardkit/autobuild``, an inner
    worktree that does not exist yet. ``None`` is neither movement nor silence:
    :meth:`components` hands the monitor only the components that carried
    evidence, and the monitor remembers each signal's last OBSERVED value, so a
    torn read can neither fake progress nor manufacture a wedge, and movement
    ACROSS a torn read is still seen.
    """

    ledger: tuple[Any, ...] | None
    progress: tuple[tuple[Any, ...], ...] | None
    head_sha: str | None

    def components(self) -> dict[str, Any]:
        """The named components that produced evidence this poll."""
        pairs = (
            (SIGNAL_LEDGER, self.ledger),
            (SIGNAL_PROGRESS, self.progress),
            (SIGNAL_HEAD, self.head_sha),
        )
        return {name: value for name, value in pairs if value is not None}


@dataclass(frozen=True)
class WedgeVerdict:
    """The monitor's answer for one poll."""

    wedged: bool
    silent_seconds: float
    window_seconds: float
    last_state: str
    #: On-disk signal families this monitor has EVER observed. Empty means the
    #: monitor has seen nothing to be silent — it can never call wedge.
    evidence: tuple[str, ...] = ()
    #: Where the window's task budget came from (``WINDOW_SOURCE_*``).
    window_source: str = WINDOW_SOURCE_RECONSTRUCTED

    def reason(self) -> str:
        """The honest terminal reason — never the word "timeout"."""
        return (
            f"wedged: no semantic progress or state movement for "
            f"{self.silent_seconds:.0f}s (window {self.window_seconds:.0f}s "
            f"from {self.window_source}; signals observed: "
            f"{','.join(self.evidence) or 'none'}) "
            f"(last: {self.last_state})"
        )


@dataclass(frozen=True)
class TaskCounts:
    """Honest per-build task attribution, with its provenance named."""

    tasks_completed: int
    tasks_failed: int
    wave_index: int
    source: str


#: Provenance values for :class:`TaskCounts`.
SOURCE_FEATURE_LEDGER: str = "feature-ledger"
#: guardkit exited 0 but its ledger says zero tasks completed (an unflushed or
#: never-written ``execution`` block). The count is floored so the wire still
#: sees the build's work — and the floor is NAMED rather than hidden.
SOURCE_FEATURE_LEDGER_SUCCESS_FLOOR: str = "feature-ledger+success-floor"
SOURCE_STDOUT_TASK_STARTS: str = "stdout-task-starts"
SOURCE_ASSUMED_SINGLE_UNIT: str = "assumed-single-unit"
SOURCE_UNKNOWN: str = "unknown"


def resolve_task_counts(
    ledger: FeatureLedger | None,
    *,
    stdout_task_ids: tuple[str, ...] = (),
    succeeded: bool,
) -> TaskCounts:
    """Task counts for the exit snapshot — the ``max(count, 1)`` hardcode's cure.

    The defect (design §c): the drain loop counted CHECKPOINT COMMIT lines —
    which are *turns* — and reported ``tasks_completed = max(count, 1)``. A
    3-task build with 9 turns reported 9; a wedged build reported 1. Both are
    lies, and the second one is the dangerous kind.

    Precedence, most authoritative first:

    1. ``.guardkit/features/<FEAT>.yaml`` — the same ledger guardkit's own
       resume trusts, recomputed from per-task status on every save.
    2. Distinct ``▶ Executing TASK-X`` ids seen on stdout — task-grained, not
       turn-grained. Used only when the ledger is unreadable.
    3. Last resort, SUCCESS ONLY: guardkit exited 0, so the feature built; we
       report it as one completed unit and NAME the assumption in ``source``.
       On a failure there is no such licence — the honest answer is 0.

    **The success floor.** On ``succeeded=True`` a ledger claiming ZERO
    completed tasks is not trusted as-is: guardkit exited 0, so at least one
    unit of work landed, and reporting 0 would ALSO suppress the wire's
    ``stage_complete`` envelope, whose delta rule is
    ``snap.tasks_completed > prev.tasks_completed``
    (``lifecycle_bridge/translation.py:504-515``). The old
    ``max(stage_complete_count, 1)`` hardcode existed for exactly that reason;
    the floor keeps the guarantee while the LEDGER — never a turn count — still
    supplies every number it actually has. The floor is named in ``source``.
    """
    if ledger is not None:
        completed = ledger.tasks_completed
        source = SOURCE_FEATURE_LEDGER
        if succeeded and completed < 1:
            completed = len(stdout_task_ids) or 1
            source = SOURCE_FEATURE_LEDGER_SUCCESS_FLOOR
        return TaskCounts(
            tasks_completed=completed,
            tasks_failed=ledger.tasks_failed,
            wave_index=max(ledger.current_wave - 1, 0),
            source=source,
        )
    if stdout_task_ids:
        completed = len(stdout_task_ids) if succeeded else 0
        return TaskCounts(
            tasks_completed=completed,
            tasks_failed=0 if succeeded else 1,
            wave_index=0,
            source=SOURCE_STDOUT_TASK_STARTS,
        )
    if succeeded:
        return TaskCounts(
            tasks_completed=1,
            tasks_failed=0,
            wave_index=0,
            source=SOURCE_ASSUMED_SINGLE_UNIT,
        )
    return TaskCounts(
        tasks_completed=0, tasks_failed=1, wave_index=0, source=SOURCE_UNKNOWN
    )


# ---------------------------------------------------------------------------
# The monitor
# ---------------------------------------------------------------------------


class BuildMonitor:
    """Semantic liveness supervisor for one guardkit autobuild subprocess.

    Two inputs, one question:

    * :meth:`note_stdout_line` — every line the drain loop already reads.
      Semantic ticks reset the silence clock; heartbeats and noise do not.
    * :meth:`poll` — a filesystem sample of the build's own ledger, progress
      logs and inner HEAD. A content-bearing change resets the silence clock.

    The question, answered by :meth:`poll`: has the build been silent in BOTH
    families for longer than :attr:`window_seconds`? Only then is it wedged.

    The monitor is deliberately free of I/O beyond file reads: no subprocess,
    no network, no model. It never raises into the caller — an unreadable
    artifact is no evidence, and no evidence is never a wedge.
    """

    def __init__(
        self,
        *,
        root: Path,
        feature_id: str,
        task_log_interval: float = DEFAULT_TASK_LOG_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._root = Path(root)
        self._feature_id = feature_id
        self._task_log_interval = task_log_interval
        self._clock = clock
        self._env = env
        self._last_movement_at: float = clock()
        #: Per-signal memory of the last OBSERVED value. Keyed by signal name,
        #: so a signal that reads as no-evidence for a poll (torn write) does
        #: not erase what we knew, and movement across the gap is still seen.
        self._observed: dict[str, Any] = {}

        # Window derivation inputs. W is the MAX over all of them (see
        # _resolve_task_budget): every one can be the binding constraint, and
        # only the per-task map ever shrinks — when the ledger says that task
        # is done, its budget can no longer be running.
        self._banner_task_timeout: float | None = None
        self._declared_feature_default: float | None = None
        self._task_budgets: dict[str, float] = {}
        self._ledger_raw_task_timeout: float | None = None
        self._last_ledger: FeatureLedger | None = None
        #: Per-task ``autobuild.task_timeout`` overrides already resolved from
        #: the task markdown, and the ids whose file was actually READ. A
        #: frontmatter budget is fixed before the build starts, so one
        #: successful read settles it; a task whose file could not be read is
        #: deliberately NOT remembered, so a file mid-move is retried rather
        #: than cached as "no override".
        self._frontmatter_budgets: dict[str, float] = {}
        self._frontmatter_read: set[str] = set()
        self._warned_default_window = False
        self._warned_no_disk_evidence = False

        # Narrative (attribution) state — stdout side.
        self._task_ids: list[str] = []
        self._last_task_id: str | None = None
        self._last_turn: int | None = None
        self._last_decision: str | None = None
        self._last_wave: int | None = None
        self._semantic_ticks = 0

    # -- inputs -----------------------------------------------------------

    def note_stdout_line(self, line: str, *, now: float | None = None) -> str | None:
        """Feed one drained stdout line; returns its semantic kind or ``None``.

        The banner carrying the run's declared per-task budget is harvested
        here too — it is a window input, not a tick, so it does not reset the
        silence clock on its own.
        """
        banner = parse_task_timeout_banner(line)
        if banner is not None and self._banner_task_timeout != banner:
            self._banner_task_timeout = banner
            logger.info(
                "build_monitor: build declared its per-task budget: %ss "
                "→ wedge window W=%ss",
                banner,
                self.window_seconds,
            )

        # The per-task budget logs — the ONLY announcement of a budget guardkit
        # raised ABOVE the banner. Without them W can sit inside guardkit's own
        # timeout for a big task, and the monitor would pre-empt orderly
        # in-band handling (design §j risk 6).
        declaration = parse_budget_declaration(line)
        for task_id, budget in declaration.per_task:
            if budget > self._task_budgets.get(task_id, 0.0):
                self._task_budgets[task_id] = budget
                logger.info(
                    "build_monitor: build declared %s's per-task budget: %ss "
                    "→ wedge window W=%ss",
                    task_id,
                    budget,
                    self.window_seconds,
                )
        declared_default = declaration.feature_level
        if declared_default is not None and (
            self._declared_feature_default is None
            or declared_default > self._declared_feature_default
        ):
            self._declared_feature_default = declared_default

        event = classify_stdout_line(line)
        if event is None:
            return None

        self._semantic_ticks += 1
        if event.task_id:
            self._last_task_id = event.task_id
            if event.task_id not in self._task_ids:
                self._task_ids.append(event.task_id)
        if event.turn is not None:
            self._last_turn = event.turn
        if event.decision is not None:
            self._last_decision = event.decision
        if event.wave is not None:
            self._last_wave = event.wave
        self._last_movement_at = self._clock() if now is None else now
        return event.kind

    def poll(self, *, now: float | None = None) -> WedgeVerdict:
        """Sample the on-disk signals and answer "is this build wedged?"."""
        stamp = self._clock() if now is None else now
        ledger = read_feature_ledger(self._root, self._feature_id)
        if ledger is not None:
            # Window inputs the ledger carries. A torn read leaves the previous
            # values standing — no evidence never shrinks W.
            self._last_ledger = ledger
            if ledger.raw_task_timeout_seconds is not None:
                self._ledger_raw_task_timeout = ledger.raw_task_timeout_seconds

        progress = read_task_progress(self._root)
        digest = StateDigest(
            ledger=ledger.fingerprint() if ledger is not None else None,
            progress=(
                tuple(sorted(item.fingerprint() for item in progress))
                if progress is not None
                else None
            ),
            head_sha=read_inner_head(self._root, self._feature_id),
        )
        if self._observe(digest):
            self._last_movement_at = stamp

        window, window_source = self._resolve_task_budget()
        window = derive_window_seconds(
            window, task_log_interval=self._task_log_interval
        )
        silent = max(stamp - self._last_movement_at, 0.0)
        evidence = self.observed_signals

        if not evidence and not self._warned_no_disk_evidence:
            self._warned_no_disk_evidence = True
            logger.warning(
                "build_monitor: no on-disk signal has EVER been observed for "
                "%s under %s (no readable feature ledger, no progress.log, no "
                "inner worktree HEAD) — the monitor is running blind and will "
                "NOT call this build wedged. Check the build root and the "
                "feature id; a live build normally materialises these within "
                "the first task.",
                self._feature_id,
                self._root,
            )

        return WedgeVerdict(
            # NO EVIDENCE IS NEVER WEDGE EVIDENCE (design §b / §j risk 2): a
            # monitor that has never observed a signal cannot report one
            # silent. Without this, an empty tree — a root/feature_id mismatch,
            # or the phase before .guardkit/autobuild exists — reads exactly
            # like a dead build and kills a healthy one.
            wedged=bool(evidence) and silent >= window,
            silent_seconds=silent,
            window_seconds=window,
            last_state=self.describe_last_state(progress=progress, ledger=ledger),
            evidence=evidence,
            window_source=window_source,
        )

    def _observe(self, digest: StateDigest) -> bool:
        """Fold one digest into the per-signal memory; True when state MOVED.

        A signal seen for the FIRST time establishes a baseline and is not
        movement (otherwise every monitor would reset its own clock on its
        first poll). A signal that produced no evidence this poll leaves its
        remembered value untouched.
        """
        moved = False
        for name, value in digest.components().items():
            previous = self._observed.get(name, _UNOBSERVED)
            self._observed[name] = value
            if previous is _UNOBSERVED:
                continue
            if value != previous:
                moved = True
        return moved

    # -- derived values ---------------------------------------------------

    @property
    def window_seconds(self) -> float:
        """W — derived from the build's own declared budgets (design §b)."""
        task_timeout, _source = self._resolve_task_budget()
        return derive_window_seconds(
            task_timeout, task_log_interval=self._task_log_interval
        )

    @property
    def window_source(self) -> str:
        """Where the window's task budget came from (``WINDOW_SOURCE_*``)."""
        return self._resolve_task_budget()[1]

    def _resolve_task_budget(self) -> tuple[float, str]:
        """The largest budget guardkit could still be enforcing, and its source.

        The MAX is the guardkit-fires-first invariant in one line. Five
        candidates, and any of them can be the binding one:

        1. a live task's declared budget (the gather line / the per-task INFO
           logs) — dropped once the ledger reports that task terminal;
        2. the feature-level budget the run announced (those same lines' tail,
           or the ``task timeout: N min`` banner);
        3. guardkit's own resolution, RECONSTRUCTED from the feature YAML —
           always in the max, never merely a fallback, because the banner is
           printed only after the whole prelude has already run;
        4. the estimate-derived floor guardkit applies per task, computed from
           the ledger's ``estimated_minutes`` — guardkit raises a task to this
           silently, so waiting to be told would leave W below the real budget;
        5. a live task's own ``autobuild.task_timeout`` frontmatter override,
           read from its markdown (register find, 2026-08-01 wedge rehearsal:
           the override "never reached the window derivation — W stayed on the
           3000s default"). An override REPLACES the feature-level budget in
           guardkit, so the reconstruction tier cannot cover it, and the
           per-task INFO line that announces it is printed only when the task
           is dispatched — the whole prelude before that ran on the wrong W.

        Under-deriving W kills a healthy build; over-deriving it only delays a
        wedge call. The max is therefore the honest direction (design §j risk 6).
        """
        candidates: list[tuple[float, str]] = []

        ledger = self._last_ledger
        for task_id, budget in self._task_budgets.items():
            if ledger is not None and ledger.is_terminal(task_id):
                continue  # that task cannot still be burning its budget
            candidates.append((budget, WINDOW_SOURCE_TASK_BUDGET_LOG))
        declared_stream = bool(candidates)

        if self._declared_feature_default:
            candidates.append(
                (self._declared_feature_default, WINDOW_SOURCE_TASK_BUDGET_LOG)
            )
            declared_stream = True
        if self._banner_task_timeout:
            candidates.append((self._banner_task_timeout, WINDOW_SOURCE_BANNER))
            declared_stream = True

        # (3) guardkit's own arithmetic on the feature YAML — the floor and the
        # multiplier, not the raw number.
        candidates.append(
            (
                reconstruct_guardkit_task_budget(
                    self._ledger_raw_task_timeout, env=self._env
                ),
                WINDOW_SOURCE_RECONSTRUCTED,
            )
        )

        # (4) the silent estimate-derived floor, scoped to tasks still in flight.
        estimate_floor = self._live_estimate_floor()
        if estimate_floor > 0:
            candidates.append((estimate_floor, WINDOW_SOURCE_ESTIMATE_FLOOR))

        # (5) a live task's OWN declared budget, read from its markdown. Last
        # in the list on purpose: `max` keeps the FIRST of equal candidates, so
        # a build with no override — or one that only ties an existing tier —
        # keeps today's window AND today's window_source byte-identically.
        frontmatter_budget = self._live_frontmatter_budget()
        if frontmatter_budget > 0:
            candidates.append(
                (frontmatter_budget, WINDOW_SOURCE_FRONTMATTER_OVERRIDE)
            )

        if not declared_stream and not self._warned_default_window:
            self._warned_default_window = True
            logger.warning(
                "build_monitor: %s has not declared a per-task budget on the "
                "stream yet (no 'task timeout: N min' banner, no wave gather "
                "line, no per-task budget INFO line) — the wedge window is "
                "RECONSTRUCTED from guardkit's own resolution (max(floor %ss, "
                "yaml %ss) × multiplier %s) and its estimate floor (%ss), not "
                "derived from the run. If this persists past wave 1 the log "
                "grammar has drifted (design §j risk 5).",
                self._feature_id,
                resolve_task_timeout_floor(self._env),
                self._ledger_raw_task_timeout or DEFAULT_TASK_TIMEOUT_SECONDS,
                resolve_timeout_multiplier(self._env),
                estimate_floor,
            )
        return max(candidates, key=lambda pair: pair[0])

    def _live_estimate_floor(self) -> float:
        """The largest estimate-derived floor still possibly in force."""
        ledger = self._last_ledger
        if ledger is None or not ledger.task_estimates:
            return 0.0
        live = set(ledger.live_task_ids())
        floors = [
            estimate_floor_seconds(estimate, env=self._env)
            for task_id, estimate in ledger.task_estimates
            if task_id in live
        ]
        return max(floors, default=0.0)

    def _live_frontmatter_budget(self) -> float:
        """The largest DECLARED per-task override still possibly in force.

        Scoped exactly like the estimate floor: the tasks the ledger says could
        still be burning a budget. Each task's markdown is read at most once —
        an override is authored before the build starts and cannot change under
        it — and a file that could not be read is retried next poll rather than
        remembered as "no override" (a task file being moved between state
        directories must not silently shrink W).
        """
        ledger = self._last_ledger
        if ledger is None:
            return 0.0
        declared = dict(ledger.task_files)
        budgets = [
            self._frontmatter_budget_for(task_id, declared.get(task_id))
            for task_id in ledger.live_task_ids()
        ]
        return max(budgets, default=0.0)

    def _frontmatter_budget_for(
        self, task_id: str, declared_path: str | None
    ) -> float:
        """One task's frontmatter-derived budget (``0.0`` = none declared)."""
        if task_id in self._frontmatter_read:
            return self._frontmatter_budgets.get(task_id, 0.0)
        path = find_task_file(self._root, task_id, declared_path)
        if path is None:
            return 0.0  # not found (yet) — no evidence, and nothing remembered
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return 0.0  # a torn/unreadable read is no evidence either
        self._frontmatter_read.add(task_id)
        declared_seconds = parse_frontmatter_task_timeout(text)
        if declared_seconds is None:
            return 0.0
        budget = frontmatter_task_budget(declared_seconds, env=self._env)
        self._frontmatter_budgets[task_id] = budget
        logger.info(
            "build_monitor: %s declares its own task_timeout in %s: %ss × "
            "multiplier %s = %ss → the wedge window honours it",
            task_id,
            path,
            declared_seconds,
            resolve_timeout_multiplier(self._env),
            budget,
        )
        return budget

    @property
    def observed_signals(self) -> tuple[str, ...]:
        """On-disk signal families this monitor has ever observed."""
        return tuple(sorted(self._observed))

    @property
    def poll_interval_seconds(self) -> float:
        return resolve_poll_interval_seconds()

    @property
    def stdout_task_ids(self) -> tuple[str, ...]:
        """Distinct task ids observed STARTING on stdout, in order."""
        return tuple(self._task_ids)

    @property
    def semantic_tick_count(self) -> int:
        return self._semantic_ticks

    def ledger(self) -> FeatureLedger | None:
        """Read the feature ledger now (``None`` = unreadable/absent)."""
        return read_feature_ledger(self._root, self._feature_id)

    def describe_last_state(
        self,
        *,
        progress: tuple[TaskProgress, ...] | None = None,
        ledger: FeatureLedger | None = None,
    ) -> str:
        """The human/machine-readable last known semantic state.

        Shape (design §d): ``task=TASK-X turn=N decision=feedback
        files_changed=3 phase=...`` — what was in flight when the build went
        quiet, so the failure pack names a TASK rather than a build-level
        guess.
        """
        if progress is None:
            progress = read_task_progress(self._root) or ()
        if ledger is None:
            ledger = read_feature_ledger(self._root, self._feature_id)

        task_id = self._last_task_id
        if ledger is not None and ledger.in_progress_task_ids:
            task_id = ledger.in_progress_task_ids[0]

        newest: TaskProgress | None = None
        for item in progress:
            if task_id is not None and item.task_id == task_id:
                newest = item
                break
            if newest is None or item.mtime > newest.mtime:
                newest = item

        parts = [f"task={task_id or 'unknown'}"]
        parts.append(f"turn={self._last_turn if self._last_turn is not None else '?'}")
        decision = self._last_decision or (newest.decision if newest else None)
        parts.append(f"decision={decision or 'none'}")
        parts.append(
            f"files_changed={newest.files_changed if newest is not None else '?'}"
        )
        parts.append(f"phase={newest.phase if newest is not None else '?'}")
        if self._last_wave is not None:
            parts.append(f"wave={self._last_wave}")
        if ledger is not None:
            parts.append(
                f"ledger_tasks_completed={ledger.tasks_completed} "
                f"ledger_tasks_failed={ledger.tasks_failed}"
            )
        return " ".join(parts)

    def semantic_state(self) -> dict[str, Any]:
        """Machine-consumable last-known semantic state for the failure pack."""
        ledger = read_feature_ledger(self._root, self._feature_id)
        event = read_last_event(self._root, self._feature_id)
        state: dict[str, Any] = {
            "last_task_id": self._last_task_id,
            "last_turn": self._last_turn,
            "last_decision": self._last_decision,
            "last_wave": self._last_wave,
            "semantic_ticks": self._semantic_ticks,
            "stdout_task_ids": list(self._task_ids),
            "window_seconds": self.window_seconds,
            "window_source": self.window_source,
            "observed_signals": list(self.observed_signals),
            "description": self.describe_last_state(),
        }
        if ledger is not None:
            state["ledger"] = {
                "tasks_completed": ledger.tasks_completed,
                "tasks_failed": ledger.tasks_failed,
                "current_wave": ledger.current_wave,
                "completed_waves": ledger.completed_waves,
                "in_progress": list(ledger.in_progress_task_ids),
                "task_statuses": [list(pair) for pair in ledger.task_statuses],
            }
        if event is not None:
            # Attribution only — events never voted on liveness.
            state["last_event"] = {
                key: event.get(key)
                for key in ("task_id", "timestamp", "turn_count", "verification_status")
                if key in event
            }
        return state


# ---------------------------------------------------------------------------
# (d) The relaunch decision — RESUME, never --fresh
# ---------------------------------------------------------------------------
#
# The defect (design §d): the pipeline's argv hardwires ``--fresh``, which is
# precisely guardkit's "destroy saved state" flag. So a killed build was a
# TOTAL LOSS — the relaunch started from zero. Rich has used the resume surface
# "loads"; it skips ``completed`` tasks, reuses the recorded worktree, refuses
# to skip any wave not persisted as smoke-verified, and re-runs the in-progress
# task. A failed feature is explicitly resumable.
#
# Stage 1 does not auto-relaunch (receipts accrue on manual resume first): this
# planner produces the DECISION and the exact command, which rides in the
# failure pack's manifest. The one thing it will never produce is a --fresh
# argv: a silent fall-through to restart-from-zero is the defect being removed,
# so an impossible resume is an honest refusal instead.

#: Hard cap on machine relaunches for one build (design §d, the ladder).
MAX_RESUME_ATTEMPTS: int = 2


@dataclass(frozen=True)
class RelaunchPlan:
    """The decision: can this failed build be RESUMED, and with what argv?"""

    possible: bool
    reason: str
    cwd: str | None = None
    argv: tuple[str, ...] = ()
    base_branch: str | None = None
    attempt_no: int = 0

    def command(self) -> str | None:
        """The human-runnable one-liner, or ``None`` when resume is refused."""
        if not self.possible:
            return None
        return " ".join(self.argv)

    def to_manifest(self) -> dict[str, Any]:
        """The failure manifest's ``resume`` block."""
        return {
            "possible": self.possible,
            "reason": self.reason,
            "cwd": self.cwd,
            "argv": list(self.argv),
            "base_branch": self.base_branch,
            "attempt_no": self.attempt_no,
            "command": self.command(),
        }


def plan_relaunch(
    *,
    feature_id: str,
    guardkit_path: Path | str | None,
    worktree_path: Path | None,
    base_branch: str | None,
    attempt_no: int = 1,
    max_attempts: int = MAX_RESUME_ATTEMPTS,
) -> RelaunchPlan:
    """Decide how a failed/wedged build is relaunched — resume or refusal.

    Guarantees, each pinned by a test:

    * The argv NEVER contains ``--fresh``. Not on the missing-worktree path,
      not on the cap-reached path, not ever.
    * The resume runs in the KEPT worktree (``cwd``), where the feature YAML,
      the inner worktree and every checkpoint commit live.
    * ``--base-branch`` is passed AGAIN from the build's own branch. Resume's
      fallback re-runs base resolution, and in a detached cwd guardkit's
      ``--base-branch > cwd branch > 'main'`` chain silently falls to ``main``
      — the F12 wrong-base defect receipted live on 2026-07-26 (FEAT-UCNT
      built on main's tip).
    * A missing kept worktree is an honest ``resume impossible`` refusal, never
      a silent restart-from-zero (design §d).
    """
    if guardkit_path is None:
        return RelaunchPlan(
            possible=False,
            reason=(
                "resume impossible: the guardkit binary is not resolvable — "
                "no relaunch argv can be formed"
            ),
            base_branch=base_branch,
            attempt_no=attempt_no,
        )
    if worktree_path is None:
        return RelaunchPlan(
            possible=False,
            reason=(
                "resume impossible: this build ran in the shared checkout (no "
                "isolated worktree was materialised), so there is no kept tree "
                "to resume in — and --fresh is NOT a substitute for resume"
            ),
            base_branch=base_branch,
            attempt_no=attempt_no,
        )
    if not Path(worktree_path).is_dir():
        return RelaunchPlan(
            possible=False,
            reason=(
                f"resume impossible: kept worktree missing ({worktree_path}) — "
                "the resume point is gone (a reboot sweeping the worktree base "
                "does this). NOT falling through to --fresh: that silent "
                "restart-from-zero is the defect this lane removes"
            ),
            base_branch=base_branch,
            attempt_no=attempt_no,
        )
    if attempt_no > max_attempts:
        return RelaunchPlan(
            possible=False,
            reason=(
                f"resume refused: attempt cap reached (attempt {attempt_no} > "
                f"{max_attempts}) — loud stop with the packs, the queue moves on"
            ),
            cwd=str(worktree_path),
            base_branch=base_branch,
            attempt_no=attempt_no,
        )

    argv: list[str] = [
        str(guardkit_path),
        "autobuild",
        "feature",
        feature_id,
        "--resume",
        "--verbose",
    ]
    if base_branch:
        argv += ["--base-branch", base_branch]
    return RelaunchPlan(
        possible=True,
        reason=(
            "resume from the kept worktree — guardkit skips completed tasks, "
            "refuses to skip an unverified wave, and re-runs the in-progress "
            "task; the F3 ref sweep is fresh-path-only and is NOT re-run"
        ),
        cwd=str(worktree_path),
        argv=tuple(argv),
        base_branch=base_branch,
        attempt_no=attempt_no,
    )
