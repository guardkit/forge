"""Tests for THE BUILD MONITOR — semantic liveness, honest counts, resume.

Rich's 2026-07-30 ruling killed the blind kill-clock: liveness must be derived
from the semantic diagnostic stream the pipeline already drains. Design of
record: ``ai-transition/docs/build-monitor-design-pass-2026-07-31.md``.

The four contracts under test, and why each one earns its place:

1. **The wedge detector.** Progress — of any kind, on the stream or on disk —
   resets the window; heartbeats and raw output volume never do. A build that
   goes fully silent past W is wedged; a build that moves is not.
2. **Honest ``tasks_completed``.** The old code counted checkpoint-commit
   lines (TURNS) and reported ``max(count, 1)``: a 3-task/9-turn build said 9,
   and a wedged build said 1. Counts now come from the build's own ledger.
3. **The relaunch decision is RESUME, never ``--fresh``.** ``--fresh`` is
   guardkit's destroy-saved-state flag; hardwiring it is what made a killed
   build a total loss.
4. **The negative control.** A slow-but-progressing build is NEVER declared
   wedged — the failure mode that triggered the ruling in the first place.

Everything here is filesystem + regex: no network, no broker, no subprocess,
no model. The monitor is M0-clean by construction and so are its tests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from forge.subagents import build_monitor as bm


# ---------------------------------------------------------------------------
# Fixtures — a fake build tree that looks exactly like guardkit's
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ambient_env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The W math must never read the AMBIENT seat (the recoach finding):
    guardkit's multiplier/floor/factor vars and the backend base-url leak in
    from the M0 seat's shell and shift every hardcoded window assertion.
    Tests construct monitors without an explicit env, so pin the ambient."""
    for var in (
        bm.GUARDKIT_TIMEOUT_MULTIPLIER_ENV,
        bm.BACKEND_BASE_URL_ENV,
        bm.ESTIMATE_TIMEOUT_FACTOR_ENV,
        bm.GUARDKIT_TASK_TIMEOUT_FLOOR_ENV,
        bm.BUILD_MONITOR_ENABLED_ENV,
        bm.BUILD_MONITOR_POLL_ENV,
    ):
        monkeypatch.delenv(var, raising=False)


class _Clock:
    """A fake monotonic clock. Tests move time; they never sleep."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def _write_feature(
    root: Path,
    feature_id: str,
    *,
    statuses: dict[str, str],
    tasks_completed: int = 0,
    tasks_failed: int = 0,
    current_wave: int = 1,
    completed_waves: tuple[int, ...] = (),
    last_updated: str = "2026-07-31T10:00:00",
    task_timeout: float | None = None,
    estimates: dict[str, int] | None = None,
    file_paths: dict[str, str] | None = None,
) -> Path:
    """Write a ``.guardkit/features/<FEAT>.yaml`` in guardkit's real shape."""
    estimates = estimates or {}
    file_paths = file_paths or {}
    path = root / bm.FEATURES_DIR / f"{feature_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = {
        "id": feature_id,
        "name": "a feature",
        "status": "in_progress",
        "tasks": [
            {
                "id": task_id,
                "name": task_id,
                "status": status,
                # guardkit's FeatureTask carries estimated_minutes (default 30)
                # and model_dump writes it back on every save.
                **(
                    {"estimated_minutes": estimates[task_id]}
                    if task_id in estimates
                    else {}
                ),
                # guardkit's FeatureTask also carries the task markdown's
                # path; it is written back on every save.
                **(
                    {"file_path": file_paths[task_id]}
                    if task_id in file_paths
                    else {}
                ),
            }
            for task_id, status in statuses.items()
        ],
        "execution": {
            "worktree_path": str(root),
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "current_wave": current_wave,
            "completed_waves": list(completed_waves),
            "last_updated": last_updated,
        },
    }
    if task_timeout is not None:
        doc["task_timeout"] = task_timeout
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _write_progress(root: Path, task_id: str, lines: list[str]) -> Path:
    path = root / bm.AUTOBUILD_DIR / task_id / bm.PROGRESS_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _snapshot_line(task_id: str, *, elapsed: int, files_changed: int, phase: str) -> str:
    return (
        f"[2026-07-31T10:{elapsed // 60:02d}:00] SNAPSHOT {task_id}: "
        f"elapsed={elapsed}s, phase={phase}, files_changed={files_changed}, "
        "last_tool=Bash"
    )


def _write_inner_head(root: Path, feature_id: str, sha: str) -> Path:
    """A minimal inner build worktree with a plain (non-pointer) ``.git`` dir."""
    gitdir = root / bm.INNER_WORKTREES_DIR / feature_id / ".git"
    gitdir.mkdir(parents=True, exist_ok=True)
    (gitdir / "HEAD").write_text(f"{sha}\n", encoding="utf-8")
    return gitdir


def _make_monitor(root: Path, clock: _Clock, feature_id: str = "FEAT-BM") -> bm.BuildMonitor:
    monitor = bm.BuildMonitor(root=root, feature_id=feature_id, clock=clock)
    # The run declares a 40-minute per-task budget. W is the MAX over that and
    # guardkit's own reconstructed resolution (floor 3000s × multiplier 1.0 on
    # a hosted-API test env), so W = 3000 + 120 = 3120s here. Tests that care
    # about the threshold read ``monitor.window_seconds`` rather than hardcode.
    monitor.note_stdout_line("Starting Wave Execution (task timeout: 40 min)")
    return monitor


# ---------------------------------------------------------------------------
# (a) The stdout grammar — semantic ticks vs heartbeats
# ---------------------------------------------------------------------------


class TestStdoutGrammar:
    """Only genuinely semantic lines are ticks. Volume is never a signal."""

    @pytest.mark.parametrize(
        "line,kind",
        [
            (
                "INFO:guardkit.orchestrator.progress:[2026-07-31T10:00:00] "
                "Started turn 3: Player invocation",
                "turn_started",
            ),
            (
                "INFO:guardkit.orchestrator.progress:[2026-07-31T10:05:00] "
                "Completed turn 3: feedback - tests still failing",
                "turn_completed",
            ),
            ("[guardkit-checkpoint] Turn 4 complete (tests: pass)", "checkpoint"),
            ("▶ Executing TASK-BM-002: wire the monitor", "task_started"),
            ("⏭ Skipping TASK-BM-001 (already completed)", "task_skipped"),
            ("Wave 2/3: TASK-BM-002, TASK-BM-003", "wave_started"),
            ("Wave 2 ✓ PASSED: 2 passed, 0 failed", "wave_finished"),
        ],
    )
    def test_pinned_grammar_lines_are_semantic_ticks(
        self, line: str, kind: str
    ) -> None:
        event = bm.classify_stdout_line(line)
        assert event is not None, f"{line!r} must be recognised as a semantic tick"
        assert event.kind == kind

    @pytest.mark.parametrize(
        "line",
        [
            # The heartbeat: process alive, build not necessarily moving.
            "[2026-07-31T10:01:00] SNAPSHOT TASK-BM-001: elapsed=60s, "
            "phase=Player invocation, files_changed=0, last_tool=Bash",
            # Raw volume — the ruling's explicit non-signal.
            "." * 4000,
            "  ⠋ thinking...",
            'Traceback (most recent call last):\n  File "x.py", line 1',
            "INFO:httpx:HTTP Request: POST http://localhost:11434 200 OK",
            # The budget banner is a WINDOW INPUT, never a liveness tick —
            # otherwise a build would look alive because it once announced
            # its own timeout.
            "Starting Wave Execution (task timeout: 40 min)",
        ],
    )
    def test_noise_and_heartbeats_are_not_ticks(self, line: str) -> None:
        assert bm.classify_stdout_line(line) is None

    def test_banner_declares_the_budget_without_resetting_the_clock(
        self, tmp_path: Path
    ) -> None:
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        clock = _Clock()
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=clock)
        monitor.poll(now=clock.now)  # observe the ledger, then go quiet
        clock.advance(monitor.window_seconds + 1.0)
        kind = monitor.note_stdout_line(
            "Starting Wave Execution (task timeout: 40 min)"
        )
        assert kind is None, "the banner is not a tick"
        # The silence outlasted W and the banner did not launder it.
        assert monitor.poll(now=clock.now).wedged is True


class TestWindowDerivation:
    """W comes from the build's own declared budgets — no folklore numbers."""

    def test_window_is_task_budget_plus_slack(self) -> None:
        assert bm.derive_window_seconds(2400.0) == 2520.0

    def test_slack_scales_with_the_heartbeat_interval(self) -> None:
        # slack = max(2 × interval, 120s)
        assert bm.derive_window_seconds(2400.0, task_log_interval=300.0) == 3000.0
        assert bm.derive_window_seconds(2400.0, task_log_interval=10.0) == 2520.0

    @pytest.mark.parametrize(
        "banner,expected",
        [
            ("Starting Wave Execution (task timeout: 40 min)", 2400.0),
            ("Starting Wave Execution (task timeout: 160 min)", 9600.0),
            ("task timeout: 90 seconds", 90.0),
        ],
    )
    def test_banner_parsing(self, banner: str, expected: float) -> None:
        assert bm.parse_task_timeout_banner(banner) == expected

    def test_banner_absent_reconstructs_guardkits_OWN_budget(
        self, tmp_path: Path
    ) -> None:
        """The RAW yaml number is not guardkit's budget — the floor is.

        guardkit enforces ``max(3000s floor, yaml) × multiplier``
        (feature_orchestrator.py:798-802). Reading ``task_timeout: 6000`` and
        calling it the budget is right only by luck; reading ``2400`` and
        calling it the budget put W at 2520s against a real 3000s (API seat) or
        12000s (local seat) budget — SHORTER than the blind clock this lane
        replaces, and inside guardkit's own timeout.
        """
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "in_progress"},
            task_timeout=6000.0,
        )
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()  # the ledger read harvests the raw budget
        assert monitor.window_seconds == 6120.0  # max(3000, 6000) × 1.0 + 120
        assert monitor.window_source == bm.WINDOW_SOURCE_RECONSTRUCTED

    def test_the_window_never_sits_below_guardkits_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pinned regression: a 2400s yaml is a 3000s (or 12000s) budget."""
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "in_progress"},
            task_timeout=2400.0,
        )
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 3120.0, (
            "W must cover guardkit's 3000s floor, not the raw 2400s yaml value"
        )

        # The local M0 seat: guardkit auto-detects a 4.0 multiplier, so its real
        # per-task budget is 12000s. A 2520s window would kill a healthy task
        # 4.8× early — the double-kill of design §j risk 6.
        monkeypatch.setenv(bm.BACKEND_BASE_URL_ENV, "http://localhost:8000/v1")
        local = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        local.poll()
        assert local.window_seconds == 12120.0

    def test_guardkits_own_env_overrides_are_mirrored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        monkeypatch.setenv(bm.GUARDKIT_TASK_TIMEOUT_FLOOR_ENV, "600")
        monkeypatch.setenv(bm.GUARDKIT_TIMEOUT_MULTIPLIER_ENV, "2")
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        # max(600 floor, 2400 yaml default) × 2 = 4800 (+120 slack)
        assert monitor.window_seconds == 4920.0

    @pytest.mark.parametrize(
        "line,expected_task,expected_budget,expected_default",
        [
            (
                "INFO:guardkit.orchestrator.feature_orchestrator:Starting "
                "parallel gather for wave 2: tasks=['TASK-A', 'TASK-B'], "
                "task_timeout=12000s (per-task=[TASK-A=12000s, TASK-B=21600s])",
                "TASK-B",
                21600.0,
                12000.0,
            ),
            (
                "INFO:guardkit.orchestrator.feature_orchestrator:[TASK-BIG] "
                "Raising task_timeout to estimate-derived floor: "
                "estimated_minutes=113 × 60 × 1.5 × multiplier=1.0 = 10170s "
                "(feature default was 3000s)",
                "TASK-BIG",
                10170.0,
                3000.0,
            ),
            (
                "[TASK-OVR] Per-task task_timeout override active: "
                "frontmatter=7200s × multiplier=4.0 = 28800s, floored at 3000s "
                "→ 28800s (feature default was 12000s)",
                "TASK-OVR",
                28800.0,
                12000.0,
            ),
        ],
    )
    def test_the_per_task_budget_grammars_are_parsed(
        self,
        line: str,
        expected_task: str,
        expected_budget: float,
        expected_default: float,
    ) -> None:
        declaration = bm.parse_budget_declaration(line)
        assert dict(declaration.per_task)[expected_task] == expected_budget
        assert declaration.feature_level == expected_default

    def test_a_raised_per_task_budget_lifts_the_window_above_the_banner(
        self, tmp_path: Path
    ) -> None:
        """§j risk 6: a task raised above the banner must not be pre-empted.

        ``_resolve_task_timeout`` raises an individual task's budget above the
        feature-level number the banner prints. If W tracked the banner alone,
        the monitor would kill that task BEFORE guardkit's own timeout fired —
        the exact double-kill the design forbids.
        """
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-BIG": "in_progress"})
        monitor = _make_monitor(tmp_path, _Clock())
        monitor.poll()
        assert monitor.window_seconds == 3120.0

        monitor.note_stdout_line(
            "[TASK-BIG] Raising task_timeout to estimate-derived floor: "
            "estimated_minutes=113 × 60 × 1.5 × multiplier=1.0 = 10170s "
            "(feature default was 3000s)"
        )
        assert monitor.window_seconds == 10290.0
        assert monitor.window_source == bm.WINDOW_SOURCE_TASK_BUDGET_LOG

    def test_a_finished_tasks_budget_is_dropped(self, tmp_path: Path) -> None:
        """A budget only counts while the task it belongs to can still be running."""
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-BIG": "in_progress"})
        monitor = _make_monitor(tmp_path, _Clock())
        monitor.note_stdout_line(
            "[TASK-BIG] Per-task task_timeout override active: frontmatter=7200s "
            "× multiplier=1.0 = 7200s, floored at 3000s → 7200s "
            "(feature default was 3000s)"
        )
        monitor.poll()
        assert monitor.window_seconds == 7320.0

        _write_feature(
            tmp_path, "FEAT-BM", statuses={"TASK-BIG": "completed"}, tasks_completed=1
        )
        monitor.poll()
        assert monitor.window_seconds == 3120.0, (
            "a completed task cannot still be burning its budget"
        )

    def test_the_silent_estimate_floor_is_computed_not_awaited(
        self, tmp_path: Path
    ) -> None:
        """guardkit raises a big task's budget WITHOUT a reliable announcement.

        ``_task_estimate_floor_seconds`` = estimate × 60 × 1.5 × multiplier is
        applied inside guardkit; the INFO line that reports it may never reach
        this process. The monitor therefore computes it from the same
        ``estimated_minutes`` it already reads out of the feature YAML.
        """
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-BIG": "in_progress"},
            estimates={"TASK-BIG": 113},
        )
        monitor = _make_monitor(tmp_path, _Clock())
        monitor.poll()
        assert monitor.window_seconds == 113 * 60 * 1.5 + 120.0  # 10290.0
        assert monitor.window_source == bm.WINDOW_SOURCE_ESTIMATE_FLOOR

    def test_an_estimate_on_a_finished_task_does_not_hold_the_window_open(
        self, tmp_path: Path
    ) -> None:
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-BIG": "completed", "TASK-SMALL": "in_progress"},
            estimates={"TASK-BIG": 113, "TASK-SMALL": 10},
            tasks_completed=1,
        )
        monitor = _make_monitor(tmp_path, _Clock())
        monitor.poll()
        assert monitor.window_seconds == 3120.0, (
            "only the live task's estimate can still be in force"
        )

    def test_before_any_task_is_in_flight_every_pending_estimate_counts(
        self, tmp_path: Path
    ) -> None:
        """The prelude — bootstrap, uv sync, preflight, the baseline probe.

        guardkit prints its banner only after all of that, and no task is
        ``in_progress`` yet, so the honest scope is every not-yet-terminal task.
        """
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "pending", "TASK-BIG": "pending"},
            estimates={"TASK-A": 10, "TASK-BIG": 113},
        )
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 10290.0

    def test_nothing_declared_on_the_stream_says_so_LOUDLY(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Design §j risk 5: the stage-1 parser must fail loud, never silent."""
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        with caplog.at_level(logging.WARNING, logger="forge.subagents.build_monitor"):
            window = monitor.window_seconds
        # Still a floored, reconstructed number — never a folklore one.
        assert window == 3120.0
        assert any(
            "has not declared a per-task budget" in record.getMessage()
            for record in caplog.records
        ), "a reconstructed window must be announced, never applied silently"

    def test_a_declared_budget_is_not_announced_as_a_fallback(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.note_stdout_line("Starting Wave Execution (task timeout: 400 min)")
        with caplog.at_level(logging.WARNING, logger="forge.subagents.build_monitor"):
            assert monitor.window_seconds == 24120.0
        assert not [
            record
            for record in caplog.records
            if "has not declared a per-task budget" in record.getMessage()
        ]


# ---------------------------------------------------------------------------
# THE DECLARED PER-TASK OVERRIDE — frontmatter.autobuild.task_timeout
# ---------------------------------------------------------------------------
#
# The 2026-08-01 wedge rehearsal's register item, verbatim: "the
# `autobuild.task_timeout` frontmatter override never reached the window
# derivation (W stayed on the 3000s default)". An operator declared a per-task
# budget in the task markdown, guardkit enforced it, and the monitor's W was
# derived as though it had not been declared at all.
#
# Why no other tier covers it: an explicit override REPLACES guardkit's
# feature-level number AND its floor, so the reconstruction tier cannot see it;
# and the per-task INFO line that announces it is printed only when guardkit
# dispatches that task, so the whole prelude before that runs on the wrong W.


def _write_task_markdown(
    root: Path,
    task_id: str,
    *,
    state: str = "backlog",
    slug: str = "a-feature",
    frontmatter: str | None = None,
) -> Path:
    """A task markdown where guardkit's own TaskLoader would find it."""
    path = root / bm.TASKS_DIR / state / slug / f"{task_id}-do-a-thing.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    block = "id: %s\nstatus: pending\n" % task_id
    if frontmatter:
        block += frontmatter
    path.write_text(f"---\n{block}---\n\n# {task_id}\n\nDo a thing.\n", encoding="utf-8")
    return path


_OVERRIDE_FRONTMATTER = "autobuild:\n  task_timeout: 7200\n"


class TestFrontmatterOverrideReachesTheWindow:
    """A task's DECLARED budget is a window input, not a log line to await."""

    def test_a_declared_override_raises_the_window(self, tmp_path: Path) -> None:
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        baseline = bm.BuildMonitor(
            root=tmp_path, feature_id="FEAT-BM", clock=_Clock()
        )
        baseline.poll()
        assert baseline.window_seconds == 3120.0, "the 3000s-default look"

        _write_task_markdown(tmp_path, "TASK-A", frontmatter=_OVERRIDE_FRONTMATTER)
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 7320.0  # 7200 × 1.0 + 120
        assert monitor.window_source == bm.WINDOW_SOURCE_FRONTMATTER_OVERRIDE

    def test_the_override_is_multiplied_exactly_as_guardkit_multiplies_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On the local M0 seat guardkit enforces override × 4.0."""
        monkeypatch.setenv(bm.BACKEND_BASE_URL_ENV, "http://localhost:4000")
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        _write_task_markdown(tmp_path, "TASK-A", frontmatter=_OVERRIDE_FRONTMATTER)
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 7200 * 4.0 + 120.0
        assert monitor.window_source == bm.WINDOW_SOURCE_FRONTMATTER_OVERRIDE

    @pytest.mark.parametrize(
        "frontmatter",
        [
            pytest.param(None, id="no-autobuild-block"),
            pytest.param("autobuild:\n  enable_pre_loop: true\n", id="no-timeout-key"),
            pytest.param("autobuild:\n  task_timeout: 0\n", id="zero"),
            pytest.param("autobuild:\n  task_timeout: -1\n", id="negative"),
            pytest.param("autobuild:\n  task_timeout: soon\n", id="not-a-number"),
            pytest.param("autobuild: 7200\n", id="autobuild-not-a-mapping"),
        ],
    )
    def test_absence_keeps_todays_derivation_byte_identical(
        self, tmp_path: Path, frontmatter: str | None
    ) -> None:
        """No usable override ⇒ the window AND its source are unchanged.

        The parametrised shapes are exactly the ones guardkit itself refuses
        (it warns and falls back to the feature-level budget), plus the
        overwhelmingly common case: a task markdown that says nothing about
        timeouts. None of them may move W.
        """
        for root in (tmp_path / "before", tmp_path / "after"):
            root.mkdir()
            _write_feature(
                root,
                "FEAT-BM",
                statuses={"TASK-A": "in_progress", "TASK-B": "pending"},
                estimates={"TASK-A": 20},
            )
        _write_task_markdown(
            tmp_path / "after", "TASK-A", frontmatter=frontmatter
        )
        _write_task_markdown(tmp_path / "after", "TASK-B")

        before = bm.BuildMonitor(
            root=tmp_path / "before", feature_id="FEAT-BM", clock=_Clock()
        )
        after = bm.BuildMonitor(
            root=tmp_path / "after", feature_id="FEAT-BM", clock=_Clock()
        )
        before.poll()
        after.poll()
        assert after.window_seconds == before.window_seconds
        assert after.window_source == before.window_source

    def test_the_ledgers_declared_path_is_read(self, tmp_path: Path) -> None:
        """The feature YAML names the file; that exact path is tried first."""
        path = _write_task_markdown(
            tmp_path, "TASK-A", state="design_approved",
            frontmatter=_OVERRIDE_FRONTMATTER,
        )
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "in_progress"},
            file_paths={"TASK-A": str(path.relative_to(tmp_path))},
        )
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 7320.0

    def test_a_stale_declared_path_falls_back_to_guardkits_own_search(
        self, tmp_path: Path
    ) -> None:
        """guardkit MOVES a task file as it runs; the yaml's path goes stale.

        guardkit's own loader ignores ``file_path`` entirely and searches
        ``tasks/<state>/**/<id>*.md``. The monitor must do the same rather than
        conclude "no override" from a path that no longer exists.
        """
        _write_task_markdown(
            tmp_path, "TASK-A", state="in_progress",
            frontmatter=_OVERRIDE_FRONTMATTER,
        )
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "in_progress"},
            file_paths={"TASK-A": "tasks/backlog/a-feature/TASK-A-do-a-thing.md"},
        )
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 7320.0

    def test_a_finished_tasks_override_does_not_hold_the_window_open(
        self, tmp_path: Path
    ) -> None:
        _write_task_markdown(tmp_path, "TASK-BIG", frontmatter=_OVERRIDE_FRONTMATTER)
        _write_task_markdown(tmp_path, "TASK-SMALL")
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-BIG": "completed", "TASK-SMALL": "in_progress"},
            tasks_completed=1,
        )
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 3120.0, (
            "a completed task cannot still be burning its declared budget"
        )

    def test_a_task_file_that_is_not_there_yet_is_retried_not_remembered(
        self, tmp_path: Path
    ) -> None:
        """A file mid-move must never be cached as 'declares nothing'."""
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 3120.0

        _write_task_markdown(tmp_path, "TASK-A", frontmatter=_OVERRIDE_FRONTMATTER)
        monitor.poll()
        assert monitor.window_seconds == 7320.0

    def test_the_markdown_is_read_once_per_task(self, tmp_path: Path) -> None:
        """An override is authored before the build; one read settles it.

        Proven behaviourally: the file is deleted after the first poll and the
        window does not fall back — so no poll after the first re-reads it.
        """
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        path = _write_task_markdown(
            tmp_path, "TASK-A", frontmatter=_OVERRIDE_FRONTMATTER
        )
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        monitor.poll()
        assert monitor.window_seconds == 7320.0
        path.unlink()
        monitor.poll()
        assert monitor.window_seconds == 7320.0

    def test_the_wedge_verdict_honours_the_declared_budget(
        self, tmp_path: Path
    ) -> None:
        """The whole point: a build inside its OWN declared budget is alive.

        At the pre-cure window (3120s) this build reads WEDGED; guardkit would
        still have been 4080s away from its own timeout — the double-kill of
        design §j risk 6.
        """
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        _write_progress(
            tmp_path,
            "TASK-A",
            [_snapshot_line("TASK-A", elapsed=60, files_changed=2, phase="green")],
        )
        _write_task_markdown(tmp_path, "TASK-A", frontmatter=_OVERRIDE_FRONTMATTER)
        clock = _Clock()
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=clock)
        monitor.poll()  # baseline the signals
        clock.advance(3200.0)  # past the OLD window, inside the declared one
        verdict = monitor.poll()
        assert not verdict.wedged
        assert verdict.window_seconds == 7320.0
        clock.advance(4300.0)  # past the declared budget + slack
        assert monitor.poll().wedged


class TestFrontmatterTimeoutParsing:
    """The mirror rejects exactly what guardkit itself rejects."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("---\nautobuild:\n  task_timeout: 7200\n---\nbody\n", 7200.0),
            ("---\nautobuild:\n  task_timeout: '7200'\n---\n", 7200.0),
            ("---\nautobuild:\n  task_timeout: 7200.0\n---\n", 7200.0),
            ("---\nautobuild:\n  task_timeout: 0\n---\n", None),
            ("---\nautobuild:\n  task_timeout: -5\n---\n", None),
            ("---\nautobuild:\n  task_timeout: later\n---\n", None),
            ("---\nautobuild: {}\n---\n", None),
            ("---\nid: TASK-A\n---\n", None),
            ("# no frontmatter at all\n", None),
            ("---\nautobuild:\n  task_timeout: 7200\n", None),  # unterminated
            ("---\nnot: [a, mapping\n---\n", None),  # unparseable YAML
            ("", None),
        ],
    )
    def test_parse(self, text: str, expected: float | None) -> None:
        assert bm.parse_frontmatter_task_timeout(text) == expected

    def test_the_search_order_mirrors_guardkits_loader(self, tmp_path: Path) -> None:
        assert bm.TASK_SEARCH_DIRS == (
            "backlog",
            "in_progress",
            "design_approved",
            "in_review",
            "blocked",
        ), "guardkit's TaskLoader.SEARCH_PATHS, in its order"
        assert "completed" not in bm.TASK_SEARCH_DIRS, (
            "a finished task cannot still be burning a budget"
        )

    def test_find_task_file_never_raises_on_an_absent_tree(
        self, tmp_path: Path
    ) -> None:
        assert bm.find_task_file(tmp_path / "nope", "TASK-A") is None
        assert bm.find_task_file(tmp_path, "TASK-A", "tasks/gone/TASK-A.md") is None


# ---------------------------------------------------------------------------
# (b) THE WEDGE DETECTOR — progress vs silence over the stream
# ---------------------------------------------------------------------------


class TestWedgeDetector:
    """A build is wedged only when EVERY signal has been silent for W."""

    def test_total_silence_past_the_window_is_wedged(self, tmp_path: Path) -> None:
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "in_progress"},
            tasks_completed=0,
        )
        _write_progress(
            tmp_path,
            "TASK-A",
            [
                "[2026-07-31T10:00:00] START TASK-A: Player invocation",
                _snapshot_line("TASK-A", elapsed=60, files_changed=2, phase="Player"),
            ],
        )
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        monitor.note_stdout_line("▶ Executing TASK-A: do the thing")

        clock.advance(monitor.window_seconds - 1.0)
        assert monitor.poll(now=clock.now).wedged is False, "inside W is not a wedge"

        clock.advance(2.0)
        verdict = monitor.poll(now=clock.now)
        assert verdict.wedged is True
        assert verdict.silent_seconds >= verdict.window_seconds
        reason = verdict.reason()
        assert reason.startswith("wedged: no semantic progress or state movement")
        assert "timed out" not in reason, "a wedge is never reported as a timeout"
        assert "task=TASK-A" in reason, "the terminal must NAME the stuck task"

    def test_a_turn_completion_resets_the_window(self, tmp_path: Path) -> None:
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)

        clock.advance(monitor.window_seconds - 10.0)
        assert monitor.poll(now=clock.now).wedged is False
        monitor.note_stdout_line(
            "INFO:guardkit.orchestrator.progress:[2026-07-31T10:40:00] "
            "Completed turn 7: feedback - one more round",
            now=clock.now,
        )
        clock.advance(monitor.window_seconds - 10.0)
        assert monitor.poll(now=clock.now).wedged is False, (
            "a finished turn is semantic progress — the window restarts"
        )

    def test_heartbeats_alone_never_reset_the_window(self, tmp_path: Path) -> None:
        """The retry-loop case: alive-looking, semantically dead.

        The progress.log keeps growing (a SNAPSHOT every 60s) but
        ``files_changed`` and ``phase`` are frozen, and no turn ever finishes.
        Counting heartbeats as liveness is exactly the mistake that would make
        the monitor useless against the class it exists for.
        """
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        lines = ["[2026-07-31T10:00:00] START TASK-A: Player invocation"]
        _write_progress(tmp_path, "TASK-A", lines)
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)

        elapsed = 0
        wedged_at: float | None = None
        for _ in range(60):  # 60 minutes of pure heartbeat
            elapsed += 60
            lines.append(
                _snapshot_line(
                    "TASK-A", elapsed=elapsed, files_changed=3, phase="Player"
                )
            )
            _write_progress(tmp_path, "TASK-A", lines)
            clock.advance(60.0)
            verdict = monitor.poll(now=clock.now)
            if verdict.wedged and wedged_at is None:
                wedged_at = clock.now
        assert wedged_at is not None, (
            "heartbeats with a frozen files_changed must NOT hold the window open"
        )

    def test_a_files_changed_delta_is_state_movement(self, tmp_path: Path) -> None:
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        lines = ["[2026-07-31T10:00:00] START TASK-A: Player invocation"]
        _write_progress(tmp_path, "TASK-A", lines)
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)

        files = 0
        for step in range(60):
            files += 1  # the task is genuinely touching files
            lines.append(
                _snapshot_line(
                    "TASK-A",
                    elapsed=(step + 1) * 60,
                    files_changed=files,
                    phase="Player",
                )
            )
            _write_progress(tmp_path, "TASK-A", lines)
            clock.advance(60.0)
            assert monitor.poll(now=clock.now).wedged is False

    def test_a_task_status_transition_is_semantic_progress(
        self, tmp_path: Path
    ) -> None:
        _write_feature(
            tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress", "TASK-B": "pending"}
        )
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        monitor.poll(now=clock.now)

        clock.advance(monitor.window_seconds - 5.0)
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "completed", "TASK-B": "in_progress"},
            tasks_completed=1,
        )
        assert monitor.poll(now=clock.now).wedged is False

        clock.advance(monitor.window_seconds - 5.0)
        assert monitor.poll(now=clock.now).wedged is False, (
            "the ledger write restarted the window"
        )
        clock.advance(10.0)
        assert monitor.poll(now=clock.now).wedged is True

    def test_a_last_updated_tick_alone_is_not_movement(self, tmp_path: Path) -> None:
        """``execution.last_updated`` is a heartbeat field, not content."""
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        monitor.poll(now=clock.now)

        for minute in range(60):
            clock.advance(60.0)
            _write_feature(
                tmp_path,
                "FEAT-BM",
                statuses={"TASK-A": "in_progress"},
                last_updated=f"2026-07-31T11:{minute:02d}:00",
            )
            verdict = monitor.poll(now=clock.now)
        assert verdict.wedged is True

    def test_inner_worktree_head_movement_is_state_movement(
        self, tmp_path: Path
    ) -> None:
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        _write_inner_head(tmp_path, "FEAT-BM", "a" * 40)
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        monitor.poll(now=clock.now)

        clock.advance(monitor.window_seconds - 5.0)
        _write_inner_head(tmp_path, "FEAT-BM", "b" * 40)  # a checkpoint commit landed
        assert monitor.poll(now=clock.now).wedged is False
        clock.advance(monitor.window_seconds - 5.0)
        assert monitor.poll(now=clock.now).wedged is False

    def test_head_resolution_follows_a_worktree_pointer_and_symbolic_ref(
        self, tmp_path: Path
    ) -> None:
        """The real inner worktree layout: ``.git`` file → gitdir → ref file."""
        common = tmp_path / "outer" / ".git"
        (common / "refs" / "heads").mkdir(parents=True)
        (common / "refs" / "heads" / "autobuild").write_text("c" * 40 + "\n")
        gitdir = common / "worktrees" / "FEAT-BM"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/autobuild\n")
        (gitdir / "commondir").write_text("../..\n")
        inner = tmp_path / bm.INNER_WORKTREES_DIR / "FEAT-BM"
        inner.mkdir(parents=True)
        (inner / ".git").write_text(f"gitdir: {gitdir}\n")

        assert bm.read_inner_head(tmp_path, "FEAT-BM") == "c" * 40

    def test_a_torn_feature_yaml_read_is_NO_EVIDENCE(self, tmp_path: Path) -> None:
        """Design §j risk 2: a partial write is never wedge evidence — nor progress.

        The monitor must (a) not crash, (b) not treat the unparseable read as a
        change that resets the window, and (c) not treat it as proof of death
        either — the other signals still decide.
        """
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        monitor.poll(now=clock.now)

        torn = tmp_path / bm.FEATURES_DIR / "FEAT-BM.yaml"
        for i in range(60):  # 60 minutes of torn writes — past W
            clock.advance(60.0)
            # A genuinely half-written document: the flow mapping never closes,
            # and the truncation point moves, so a "changed bytes = movement"
            # reading of this file would hold the window open forever.
            torn.write_text(
                "id: FEAT-BM\ntasks: [ {id: TASK-A, status: in_progres"[: 40 + i]
            )
            verdict = monitor.poll(now=clock.now)
        assert bm.read_feature_ledger(tmp_path, "FEAT-BM") is None, (
            "an unparseable feature YAML must read as NO EVIDENCE"
        )
        assert verdict.wedged is True, (
            "a stream of torn reads must not hold the window open"
        )

    def test_events_jsonl_growth_is_attribution_NOT_liveness(
        self, tmp_path: Path
    ) -> None:
        """A task retrying in place keeps appending events. That is not life.

        This test is the fence against someone "improving" the monitor by
        counting events.jsonl lines as movement — it would resurrect exactly
        the alive-looking-but-dead class the ruling targets.
        """
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        events = tmp_path / bm.AUTOBUILD_DIR / "FEAT-BM" / bm.EVENTS_LOG_NAME
        events.parent.mkdir(parents=True, exist_ok=True)
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)

        with events.open("a", encoding="utf-8") as handle:
            for i in range(60):  # 60 minutes of pure event churn — past W
                clock.advance(60.0)
                handle.write(
                    json.dumps(
                        {
                            "task_id": "TASK-A",
                            "timestamp": f"2026-07-31T11:{i:02d}:00",
                            "attempt": i,
                        }
                    )
                    + "\n"
                )
                handle.flush()
                verdict = monitor.poll(now=clock.now)
        assert verdict.wedged is True

    def test_a_mid_append_events_line_is_skipped_not_fatal(
        self, tmp_path: Path
    ) -> None:
        events = tmp_path / bm.AUTOBUILD_DIR / "FEAT-BM" / bm.EVENTS_LOG_NAME
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text(
            json.dumps({"task_id": "TASK-A", "turn_count": 2}) + "\n" + '{"task_id": "TA',
            encoding="utf-8",
        )
        last = bm.read_last_event(tmp_path, "FEAT-BM")
        assert last is not None and last["turn_count"] == 2

    def test_an_absent_build_tree_never_crashes_the_monitor(
        self, tmp_path: Path
    ) -> None:
        monitor = bm.BuildMonitor(
            root=tmp_path / "nope", feature_id="FEAT-BM", clock=_Clock()
        )
        verdict = monitor.poll()
        assert verdict.wedged is False
        assert monitor.ledger() is None

    def test_NO_signal_ever_observed_is_never_a_wedge(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Total evidence absence is not evidence of death (design §j risk 2).

        A monitor rooted where nothing is written — a root/feature_id mismatch,
        a tree that does not exist — sees no ledger, no progress.log and no
        inner HEAD. Scoring that as "every signal silent" produces a kill whose
        own reason is self-evidently evidence-free
        (``task=unknown turn=? decision=none files_changed=? phase=?``), i.e.
        a healthy build killed for the monitor's own blindness.
        """
        clock = _Clock()
        monitor = _make_monitor(tmp_path / "wrong-root", clock)
        with caplog.at_level(logging.WARNING, logger="forge.subagents.build_monitor"):
            clock.advance(monitor.window_seconds * 3)
            verdict = monitor.poll(now=clock.now)
        assert verdict.wedged is False
        assert verdict.evidence == ()
        assert verdict.silent_seconds > verdict.window_seconds, (
            "the silence is real — it is the EVIDENCE that is missing"
        )
        assert any(
            "no on-disk signal has EVER been observed" in record.getMessage()
            for record in caplog.records
        ), "running blind must be loud, not silent"

    def test_one_observed_signal_gone_quiet_IS_enough(self, tmp_path: Path) -> None:
        """The other half of the rule: observed-then-silent still wedges."""
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        monitor.poll(now=clock.now)
        clock.advance(monitor.window_seconds + 1.0)
        verdict = monitor.poll(now=clock.now)
        assert verdict.wedged is True
        assert bm.SIGNAL_LEDGER in verdict.evidence
        assert "signals observed: ledger" in verdict.reason()

    def test_movement_ACROSS_a_torn_read_is_still_movement(
        self, tmp_path: Path
    ) -> None:
        """Per-signal memory: a torn poll must not erase what we knew.

        Poll 1 reads the ledger, poll 2 is torn (no evidence), poll 3 reads a
        CHANGED ledger. Comparing only consecutive polls would compare "changed"
        against "no evidence", see nothing, and let a live build's window run
        out over a single unlucky write.
        """
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        monitor.poll(now=clock.now)

        path = tmp_path / bm.FEATURES_DIR / "FEAT-BM.yaml"
        good = path.read_text(encoding="utf-8")
        clock.advance(monitor.window_seconds - 60.0)
        path.write_text("id: FEAT-BM\ntasks: [ {id: TASK-A, stat", encoding="utf-8")
        assert monitor.poll(now=clock.now).wedged is False

        clock.advance(60.0)
        path.write_text(
            good.replace("status: in_progress", "status: completed"), encoding="utf-8"
        )
        assert monitor.poll(now=clock.now).wedged is False
        clock.advance(monitor.window_seconds - 60.0)
        assert monitor.poll(now=clock.now).wedged is False, (
            "the ledger DID move across the torn read — the window restarted"
        )


# ---------------------------------------------------------------------------
# THE NEGATIVE CONTROL — the failure mode that triggered the ruling
# ---------------------------------------------------------------------------


class TestSlowButProgressingBuildIsNeverWedged:
    """A healthy multi-wave build must survive, however long it takes.

    The old 3600s clock killed exactly these builds, and the kill was a total
    loss. If the monitor ever fails these tests it has reproduced the defect it
    was built to remove.
    """

    def test_eight_hours_of_slow_turns_are_never_wedged(self, tmp_path: Path) -> None:
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        # One local-judge checker turn can approach 40 minutes; W is 42 minutes.
        turn_gap = 39.0 * 60.0
        elapsed = 0.0
        turn = 0
        while elapsed < 8 * 3600:
            for _ in range(int(turn_gap // 60)):
                clock.advance(60.0)  # the poll cadence
                elapsed += 60.0
                assert monitor.poll(now=clock.now).wedged is False, (
                    f"a build still finishing turns was called wedged at "
                    f"{elapsed}s — this is the killed-healthy-build defect"
                )
            turn += 1
            monitor.note_stdout_line(
                f"INFO:guardkit.orchestrator.progress:[t] Completed turn {turn}: "
                "success - green",
                now=clock.now,
            )
        assert turn >= 12, "the horizon must actually cover a long build"

    def test_a_build_whose_only_sign_of_life_is_on_disk_is_never_wedged(
        self, tmp_path: Path
    ) -> None:
        """No stdout at all (a tee failure / a quiet phase) — disk still moves."""
        statuses = {"TASK-A": "in_progress", "TASK-B": "pending", "TASK-C": "pending"}
        _write_feature(tmp_path, "FEAT-BM", statuses=statuses)
        lines = ["[2026-07-31T10:00:00] START TASK-A: Player invocation"]
        _write_progress(tmp_path, "TASK-A", lines)
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)

        files = 0
        for step in range(6 * 60):  # six hours, one poll a minute
            clock.advance(60.0)
            if step % 30 == 0:  # a real file change every half hour
                files += 1
                lines.append(
                    _snapshot_line(
                        "TASK-A",
                        elapsed=step * 60,
                        files_changed=files,
                        phase="Player",
                    )
                )
                _write_progress(tmp_path, "TASK-A", lines)
            assert monitor.poll(now=clock.now).wedged is False

    def test_a_wave_boundary_alone_holds_the_window_open(self, tmp_path: Path) -> None:
        """Between-phase silences (bootstrap, smoke) are covered by the same W."""
        _write_feature(tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"})
        clock = _Clock()
        monitor = _make_monitor(tmp_path, clock)
        for wave in range(1, 8):
            clock.advance(monitor.window_seconds - 60.0)
            assert monitor.poll(now=clock.now).wedged is False
            monitor.note_stdout_line(
                f"Wave {wave} ✓ PASSED: 2 passed, 0 failed", now=clock.now
            )


# ---------------------------------------------------------------------------
# (c) HONEST tasks_completed — the max(count, 1) hardcode's cure
# ---------------------------------------------------------------------------


class TestHonestTaskCounts:
    """Counts come from the build's ledger. Turns are not tasks. Ever."""

    def test_the_ledger_beats_the_turn_count(self, tmp_path: Path) -> None:
        """A 3-task build with 9 turns reports 3 — not 9, and not 1."""
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={
                "TASK-A": "completed",
                "TASK-B": "completed",
                "TASK-C": "completed",
            },
            tasks_completed=3,
            current_wave=2,
            completed_waves=(1, 2),
        )
        ledger = bm.read_feature_ledger(tmp_path, "FEAT-BM")
        assert ledger is not None and ledger.tasks_completed == 3

        counts = bm.resolve_task_counts(
            ledger,
            stdout_task_ids=("TASK-A", "TASK-B", "TASK-C"),
            succeeded=True,
        )
        assert counts.tasks_completed == 3
        assert counts.wave_index == 1  # current_wave is 1-indexed on disk
        assert counts.source == bm.SOURCE_FEATURE_LEDGER

    def test_a_wedged_build_reports_what_it_actually_finished(
        self, tmp_path: Path
    ) -> None:
        """The dangerous half of the old lie: a wedged build claimed 1 done."""
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={
                "TASK-A": "completed",
                "TASK-B": "completed",
                "TASK-C": "in_progress",
            },
            tasks_completed=2,
            tasks_failed=0,
        )
        counts = bm.resolve_task_counts(
            bm.read_feature_ledger(tmp_path, "FEAT-BM"),
            stdout_task_ids=("TASK-A", "TASK-B", "TASK-C"),
            succeeded=False,
        )
        assert counts.tasks_completed == 2
        assert counts.source == bm.SOURCE_FEATURE_LEDGER

    def test_a_zero_ledger_on_SUCCESS_is_floored_and_says_so(
        self, tmp_path: Path
    ) -> None:
        """guardkit exited 0, so 'zero tasks completed' cannot stand as-is.

        Two consequences of reporting the raw 0: the wire under-reports a
        build that worked, AND the bridge translator's stage_complete delta
        (``snap.tasks_completed > prev.tasks_completed``,
        ``lifecycle_bridge/translation.py:504-515``) never fires — which is
        precisely why the old ``max(stage_complete_count, 1)`` existed. The
        floor keeps that guarantee and NAMES itself.
        """
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "completed"},
            tasks_completed=0,  # an unflushed / never-written execution block
        )
        ledger = bm.read_feature_ledger(tmp_path, "FEAT-BM")
        counts = bm.resolve_task_counts(
            ledger, stdout_task_ids=("TASK-A",), succeeded=True
        )
        assert counts.tasks_completed == 1
        assert counts.source == bm.SOURCE_FEATURE_LEDGER_SUCCESS_FLOOR

        # …and with no stdout ids either, the floor is still 1.
        assert bm.resolve_task_counts(ledger, succeeded=True).tasks_completed == 1

    def test_a_zero_ledger_on_a_FAILURE_is_left_at_zero(self, tmp_path: Path) -> None:
        """No exit-0 licence on the failure path: zero done means zero done."""
        _write_feature(
            tmp_path, "FEAT-BM", statuses={"TASK-A": "in_progress"}, tasks_completed=0
        )
        counts = bm.resolve_task_counts(
            bm.read_feature_ledger(tmp_path, "FEAT-BM"),
            stdout_task_ids=("TASK-A",),
            succeeded=False,
        )
        assert counts.tasks_completed == 0
        assert counts.source == bm.SOURCE_FEATURE_LEDGER

    def test_no_ledger_falls_back_to_task_STARTS_not_turns(self) -> None:
        counts = bm.resolve_task_counts(
            None, stdout_task_ids=("TASK-A", "TASK-B"), succeeded=True
        )
        assert counts.tasks_completed == 2
        assert counts.source == bm.SOURCE_STDOUT_TASK_STARTS

    def test_no_evidence_at_all_on_a_FAILURE_reports_zero(self) -> None:
        """No licence to invent a completed task on a build that died."""
        counts = bm.resolve_task_counts(None, succeeded=False)
        assert counts.tasks_completed == 0
        assert counts.source == bm.SOURCE_UNKNOWN

    def test_no_evidence_at_all_on_a_SUCCESS_names_its_assumption(self) -> None:
        counts = bm.resolve_task_counts(None, succeeded=True)
        assert counts.tasks_completed == 1
        assert counts.source == bm.SOURCE_ASSUMED_SINGLE_UNIT, (
            "the last-resort tier must be labelled as an assumption on the wire"
        )

    def test_stdout_task_ids_are_distinct_and_ordered(self, tmp_path: Path) -> None:
        monitor = bm.BuildMonitor(root=tmp_path, feature_id="FEAT-BM", clock=_Clock())
        for line in [
            "▶ Executing TASK-A: one",
            "[guardkit-checkpoint] Turn 1 complete (tests: pass)",
            "[guardkit-checkpoint] Turn 2 complete (tests: pass)",
            "▶ Executing TASK-B: two",
            "▶ Executing TASK-A: one (retry)",
        ]:
            monitor.note_stdout_line(line)
        assert monitor.stdout_task_ids == ("TASK-A", "TASK-B")
        assert monitor.semantic_tick_count == 5

    def test_semantic_state_names_the_task_for_the_failure_pack(
        self, tmp_path: Path
    ) -> None:
        _write_feature(
            tmp_path,
            "FEAT-BM",
            statuses={"TASK-A": "completed", "TASK-B": "in_progress"},
            tasks_completed=1,
        )
        _write_progress(
            tmp_path,
            "TASK-B",
            [
                "[2026-07-31T10:00:00] START TASK-B: Player invocation",
                _snapshot_line("TASK-B", elapsed=120, files_changed=7, phase="Coach"),
            ],
        )
        events = tmp_path / bm.AUTOBUILD_DIR / "FEAT-BM" / bm.EVENTS_LOG_NAME
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text(
            json.dumps(
                {
                    "task_id": "TASK-B",
                    "timestamp": "2026-07-31T10:02:00",
                    "turn_count": 3,
                    "verification_status": "feedback",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monitor = _make_monitor(tmp_path, _Clock())
        monitor.note_stdout_line(
            "INFO:...progress:[t] Completed turn 3: feedback - not yet"
        )
        state = monitor.semantic_state()
        assert state["ledger"]["tasks_completed"] == 1
        assert state["ledger"]["in_progress"] == ["TASK-B"]
        assert state["last_event"]["task_id"] == "TASK-B"
        described = state["description"]
        assert "task=TASK-B" in described
        assert "turn=3" in described
        assert "decision=feedback" in described
        assert "files_changed=7" in described


# ---------------------------------------------------------------------------
# (d) THE RELAUNCH DECISION — resume, never --fresh
# ---------------------------------------------------------------------------


class TestRelaunchDecision:
    """``--fresh`` destroys saved state. It is never the relaunch."""

    def test_a_kept_worktree_relaunches_with_RESUME(self, tmp_path: Path) -> None:
        worktree = tmp_path / "build-FEAT-BM-1"
        worktree.mkdir()
        plan = bm.plan_relaunch(
            feature_id="FEAT-BM",
            guardkit_path="/usr/local/bin/guardkit",
            worktree_path=worktree,
            base_branch="lane/build-monitor",
        )
        assert plan.possible is True
        assert plan.argv == (
            "/usr/local/bin/guardkit",
            "autobuild",
            "feature",
            "FEAT-BM",
            "--resume",
            "--verbose",
            "--base-branch",
            "lane/build-monitor",
        )
        assert "--fresh" not in plan.argv
        assert plan.cwd == str(worktree), "resume must run IN the kept worktree"
        assert plan.attempt_no == 1

    def test_the_base_branch_is_passed_again_F12(self, tmp_path: Path) -> None:
        """Resume's fallback re-runs base resolution; a detached cwd falls to main.

        FEAT-UCNT was built on main's tip on 2026-07-26 because of exactly this.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        plan = bm.plan_relaunch(
            feature_id="FEAT-BM",
            guardkit_path="/usr/bin/guardkit",
            worktree_path=worktree,
            base_branch="feature/planning",
        )
        assert "--base-branch" in plan.argv
        assert plan.argv[plan.argv.index("--base-branch") + 1] == "feature/planning"

    def test_a_missing_kept_worktree_REFUSES_and_never_falls_back_to_fresh(
        self, tmp_path: Path
    ) -> None:
        """The reboot-swept-/tmp case. Honest refusal beats restart-from-zero."""
        plan = bm.plan_relaunch(
            feature_id="FEAT-BM",
            guardkit_path="/usr/bin/guardkit",
            worktree_path=tmp_path / "swept-away",
            base_branch="lane/x",
        )
        assert plan.possible is False
        assert "resume impossible: kept worktree missing" in plan.reason
        assert plan.argv == ()
        assert plan.command() is None
        block = plan.to_manifest()
        # The refusal SAYS "--fresh" (to state what it is refusing to do); what
        # it must never do is hand anyone a runnable --fresh command.
        assert block["argv"] == []
        assert block["command"] is None
        assert "NOT falling through to --fresh" in block["reason"]

    def test_a_legacy_shared_checkout_build_refuses_rather_than_restarting(
        self,
    ) -> None:
        plan = bm.plan_relaunch(
            feature_id="FEAT-BM",
            guardkit_path="/usr/bin/guardkit",
            worktree_path=None,
            base_branch=None,
        )
        assert plan.possible is False
        assert "--fresh is NOT a substitute" in plan.reason
        assert plan.argv == ()

    def test_the_attempt_cap_stops_the_loop(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        plan = bm.plan_relaunch(
            feature_id="FEAT-BM",
            guardkit_path="/usr/bin/guardkit",
            worktree_path=worktree,
            base_branch="lane/x",
            attempt_no=bm.MAX_RESUME_ATTEMPTS + 1,
        )
        assert plan.possible is False
        assert "attempt cap reached" in plan.reason
        assert plan.argv == ()

    def test_an_unresolvable_guardkit_refuses(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        plan = bm.plan_relaunch(
            feature_id="FEAT-BM",
            guardkit_path=None,
            worktree_path=worktree,
            base_branch="lane/x",
        )
        assert plan.possible is False
        assert plan.argv == ()

    def test_the_manifest_block_is_machine_and_human_runnable(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        block = bm.plan_relaunch(
            feature_id="FEAT-BM",
            guardkit_path="/usr/bin/guardkit",
            worktree_path=worktree,
            base_branch="lane/x",
        ).to_manifest()
        assert block["possible"] is True
        assert block["cwd"] == str(worktree)
        assert block["command"].endswith("--verbose --base-branch lane/x")
        assert "--resume" in block["command"]
        assert "--fresh" not in json.dumps(block)

    def test_NO_path_through_the_planner_ever_produces_a_fresh_argv(
        self, tmp_path: Path
    ) -> None:
        """The docstring says "not ever" — this is the sweep that pins it."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        cases = [
            {"guardkit_path": None, "worktree_path": worktree},
            {"guardkit_path": "/usr/bin/guardkit", "worktree_path": None},
            {
                "guardkit_path": "/usr/bin/guardkit",
                "worktree_path": tmp_path / "gone",
            },
            {
                "guardkit_path": "/usr/bin/guardkit",
                "worktree_path": worktree,
                "attempt_no": bm.MAX_RESUME_ATTEMPTS + 5,
            },
            {"guardkit_path": "/usr/bin/guardkit", "worktree_path": worktree},
        ]
        for case in cases:
            plan = bm.plan_relaunch(
                feature_id="FEAT-BM", base_branch="lane/x", **case  # type: ignore[arg-type]
            )
            assert "--fresh" not in plan.argv
            assert "--fresh" not in (plan.command() or "")


class TestMonitorKillSwitch:
    def test_enabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(bm.BUILD_MONITOR_ENABLED_ENV, raising=False)
        assert bm.monitor_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE"])
    def test_operator_can_disarm(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, value)
        assert bm.monitor_enabled() is False

    def test_poll_interval_override_and_fallbacks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(bm.BUILD_MONITOR_POLL_ENV, "5")
        assert bm.resolve_poll_interval_seconds() == 5.0
        monkeypatch.setenv(bm.BUILD_MONITOR_POLL_ENV, "not-a-number")
        assert bm.resolve_poll_interval_seconds() == 60.0
        monkeypatch.setenv(bm.BUILD_MONITOR_POLL_ENV, "-3")
        assert bm.resolve_poll_interval_seconds() == 60.0
