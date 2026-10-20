"""When the sweep runs (Codex R6), its settings, and ``forge security-sweep --once``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig, SecuritySweepConfig
from forge.lifecycle import migrations
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from forge.planning.work_queue_store import WorkQueueStore
from forge.security_sweep.schedule import (
    RECHECK_SECONDS,
    SKIPPED_LINE,
    NightlySchedule,
    factory_is_idle,
)
from forge.security_sweep.state import SweepAlreadyRunning
from forge.security_sweep.sweep import RunReport

T0 = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def _schedule(clock: Clock, busy_until: datetime | None, runs: list[datetime], lines: list[str], *, raise_running: bool = False) -> NightlySchedule:
    async def run_once() -> RunReport:
        if raise_running:
            raise SweepAlreadyRunning()
        runs.append(clock.now)
        return RunReport(started_at=clock.now)

    async def publish(text: str) -> None:
        lines.append(text)

    return NightlySchedule(
        at="02:30",
        until="06:00",
        run_once=run_once,
        is_idle=lambda: busy_until is None or clock.now >= busy_until,
        publish=publish,
        clock=clock,
        sleep=clock.sleep,
    )


class TestTheNight:
    @pytest.mark.asyncio
    async def test_an_idle_factory_is_swept_at_the_set_time(self) -> None:
        clock, runs, lines = Clock(T0), [], []
        schedule = _schedule(clock, None, runs, lines)
        assert await schedule.run_night(*schedule.window(clock.now)) == "swept"
        assert runs == [T0.replace(hour=2, minute=30)]
        assert lines == []

    @pytest.mark.asyncio
    async def test_a_busy_factory_is_looked_at_again_every_fifteen_minutes(self) -> None:
        clock, runs, lines = Clock(T0), [], []
        schedule = _schedule(clock, T0.replace(hour=3, minute=10), runs, lines)
        assert await schedule.run_night(*schedule.window(clock.now)) == "swept"
        assert runs == [T0.replace(hour=3, minute=15)]
        assert clock.slept[1:] == [RECHECK_SECONDS] * 3

    @pytest.mark.asyncio
    async def test_a_night_with_no_quiet_moment_says_it_was_skipped(self) -> None:
        clock, runs, lines = Clock(T0), [], []
        schedule = _schedule(clock, T0 + timedelta(days=1), runs, lines)
        assert await schedule.run_night(*schedule.window(clock.now)) == "skipped"
        assert runs == []
        assert lines == [SKIPPED_LINE]
        assert lines == ["Nightly security check: skipped: the factory was busy all night."]
        assert clock.now <= T0.replace(hour=6)

    @pytest.mark.asyncio
    async def test_a_sweep_already_running_is_left_alone(self) -> None:
        clock, runs, lines = Clock(T0), [], []
        schedule = _schedule(clock, None, runs, lines, raise_running=True)
        assert await schedule.run_night(*schedule.window(clock.now)) == "already-running"

    def test_a_restart_inside_the_window_still_sweeps_tonight(self) -> None:
        clock = Clock(T0.replace(hour=3))
        schedule = _schedule(clock, None, [], [])
        start, latest = schedule.window(clock.now)
        assert start == T0.replace(hour=2, minute=30)
        assert latest == T0.replace(hour=6)

    def test_after_the_window_the_next_night_is_chosen(self) -> None:
        clock = Clock(T0.replace(hour=7))
        start, _ = _schedule(clock, None, [], []).window(clock.now)
        assert start == T0.replace(hour=2, minute=30) + timedelta(days=1)

    def test_a_latest_start_before_the_start_means_the_next_morning(self) -> None:
        clock = Clock(T0.replace(hour=12))
        schedule = _schedule(clock, None, [], [])
        schedule.at, schedule.until = "23:00", "04:00"
        start, latest = schedule.window(clock.now)
        assert (start, latest) == (T0.replace(hour=23), T0.replace(hour=4) + timedelta(days=1))


class TestIdle:
    @pytest.fixture
    def connection(self, tmp_path: Path):
        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(cx)
        yield cx
        cx.close()

    def test_an_empty_factory_is_idle(self, connection) -> None:
        assert factory_is_idle(connection)

    def test_an_open_planning_run_is_busy(self, connection) -> None:
        SqlitePlanningRunStore(connection).record_queued(
            correlation_id="c-1", originating_user="rich", expected_approver="rich",
            request_text="x", triggered_by="cli",
        )
        assert not factory_is_idle(connection)

    def test_an_admitted_queue_row_is_busy(self, connection) -> None:
        store = WorkQueueStore(connection)
        filed = store.file_sentence(correlation_id="q-1", sentence="x", originating_user="rich")
        assert factory_is_idle(connection)  # waiting in the queue is not in flight
        store.admit(filed.queue_id, actor_identity="t")
        assert not factory_is_idle(connection)

    def test_an_unreadable_database_is_busy(self, connection) -> None:
        connection.close()
        assert not factory_is_idle(connection)


def _insert_build(connection, build_id: str, status: str, *, mode: str = "mode-b") -> None:
    connection.execute(
        """
        INSERT INTO builds (
            build_id, feature_id, repo, branch, feature_yaml_path, status,
            triggered_by, correlation_id, queued_at, max_turns,
            sdk_timeout_seconds, mode
        ) VALUES (?, 'FEAT-TEST1', 'guardkit/example-project', 'main',
                  'feature.yaml', ?, 'cli', ?, ?, 50, 3600, ?)
        """,
        (build_id, status, f"corr-{build_id}", datetime.now(timezone.utc).isoformat(), mode),
    )
    connection.commit()


def _offer_merge_card(connection, build_id: str, *, offered_at: datetime) -> None:
    """A merge card on the record the way the real offer writes it."""
    from forge.lifecycle.persistence import SqliteLifecyclePersistence, StageLogEntry
    from forge.pipeline.merge_offer import (
        MERGE_OFFER_STAGE_LABEL,
        MERGE_OFFER_TARGET_IDENTIFIER,
    )

    SqliteLifecyclePersistence(connection=connection).record_stage(
        StageLogEntry(
            build_id=build_id,
            stage_label=MERGE_OFFER_STAGE_LABEL,
            target_kind="local_tool",
            target_identifier=MERGE_OFFER_TARGET_IDENTIFIER,
            status="GATED",
            gate_mode="MANDATORY_HUMAN_APPROVAL",
            started_at=offered_at,
            completed_at=offered_at,
            duration_secs=0.0,
            details={"merge_offer": {"build_id": build_id, "request_id": f"merge-{build_id}",
                                     "resume_options": ["approve", "reject"]}},
        )
    )


def _planning_run(connection, correlation_id: str, *states, target_terminal: bool = False):
    """A planning run moved through ``states`` by the store's own transitions."""
    from forge.planning.states import PlanningState

    store = SqlitePlanningRunStore(connection, target_terminal_enabled=target_terminal)
    store.record_queued(
        correlation_id=correlation_id, originating_user="rich", expected_approver="rich",
        request_text="x", triggered_by="cli",
    )
    for state in states:
        assert store.transition(correlation_id, PlanningState(state), actor_identity="test") is None
    return store


def _answer_merge_card(connection, build_id: str) -> None:
    """The merge word on the record, the way the merge executor writes it."""
    from forge.lifecycle.persistence import SqliteLifecyclePersistence, StageLogEntry
    from forge.pipeline.merge_executor import MERGE_DECISION_TARGET_IDENTIFIER
    from forge.pipeline.merge_offer import MERGE_OFFER_STAGE_LABEL

    now = datetime.now(timezone.utc)
    SqliteLifecyclePersistence(connection=connection).record_stage(
        StageLogEntry(
            build_id=build_id,
            stage_label=MERGE_OFFER_STAGE_LABEL,
            target_kind="local_tool",
            target_identifier=MERGE_DECISION_TARGET_IDENTIFIER,
            status="PASSED",
            gate_mode="MANDATORY_HUMAN_APPROVAL",
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={"merge_decision": {"decision": "approve", "decided_by": "rich"}},
        )
    )


#: The build states that are working: the queue's active states but PAUSED.
WORKING_BUILD_STATES = sorted({"QUEUED", "PREPARING", "RUNNING", "FINALISING"})


class TestWorkWaitingForAPersonDoesNotKeepTheSweepOff:
    """20 October 2026: one merge card left waiting for a person kept the
    sweep off all night. The sweep waits so as not to compete with builds for
    the model and the machines; work that is only waiting for a person's
    answer uses neither. The idle test is read through :func:`compose_sweep`,
    which the nightly run, ``--once`` and the mid-sweep check all use, with
    the coordinator's default settings, whose queue counts an unanswered card
    as a place for a day."""

    @pytest.fixture
    def connection(self, tmp_path: Path):
        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(cx)
        yield cx
        cx.close()

    def _is_idle(self, connection, tmp_path: Path):
        from forge.security_sweep.schedule import compose_sweep

        config = ForgeConfig(permissions={"filesystem": {"allowlist": []}})
        # The queue's own hold is still a day; only the sweep ignores it.
        assert config.queue.merge_offer_hold_seconds == 24 * 60 * 60

        async def publish(text: str) -> None:
            pass

        deps, is_idle = compose_sweep(
            config, db_path=tmp_path / "forge.db", publish=publish, connection=connection
        )
        # The mid-sweep check ("a build started during the sweep") is the same test.
        assert deps.is_idle is is_idle
        return is_idle

    def test_an_open_merge_card_younger_than_the_hold_and_nothing_else_is_idle(self, connection, tmp_path: Path) -> None:
        """MUTATION CHECK: read the merge cards with the queue's hold again and this goes red."""
        from forge.planning.work_queue_loop import count_in_flight

        _insert_build(connection, "build-TEST1", "COMPLETE")
        _offer_merge_card(connection, "build-TEST1", offered_at=datetime.now(timezone.utc) - timedelta(minutes=12))
        # The queue still counts the card as work in flight.
        assert count_in_flight(connection, merge_offer_hold_seconds=24 * 60 * 60) == 1
        assert self._is_idle(connection, tmp_path)() is True
        assert factory_is_idle(connection) is True

    def test_a_planning_run_paused_at_a_card_and_nothing_else_is_idle(self, connection, tmp_path: Path) -> None:
        """MUTATION CHECK: count a PAUSED planning run as busy again and this goes red."""
        store = _planning_run(connection, "c-paused", "RUNNING")
        # The planning checkpoint writes its card row, then pauses the run.
        store._record_event(correlation_id="c-paused", stage_label="product-docs", status="GATED")
        assert store.transition("c-paused", PlanningState.PAUSED, actor_identity="gate-check") is None
        assert self._is_idle(connection, tmp_path)() is True

    def test_a_planning_run_at_its_spec_card_and_nothing_else_is_idle(self, connection, tmp_path: Path) -> None:
        """MUTATION CHECK: count a run whose latest event is an open card as busy and this goes red.

        The spec card waits with the run still in its spec state, not PAUSED."""
        store = _planning_run(connection, "c-spec", "RUNNING", "FEATURE_SPEC", target_terminal=True)
        store._record_event(correlation_id="c-spec", stage_label="feature-spec-digest-review", status="GATED")
        assert self._is_idle(connection, tmp_path)() is True
        # A card put back after a restart is still only waiting.
        store._record_event(correlation_id="c-spec", stage_label="feature-spec-digest-review", status="reopened")
        assert self._is_idle(connection, tmp_path)() is True

    def test_a_spec_card_once_answered_is_busy_again(self, connection, tmp_path: Path) -> None:
        store = _planning_run(connection, "c-spec", "RUNNING", "FEATURE_SPEC", target_terminal=True)
        store._record_event(correlation_id="c-spec", stage_label="feature-spec-digest-review", status="GATED")
        store._record_event(correlation_id="c-spec", stage_label="feature-spec-digest-review", status="approved")
        assert self._is_idle(connection, tmp_path)() is False

    def test_a_planning_run_writing_its_spec_is_busy(self, connection, tmp_path: Path) -> None:
        _planning_run(connection, "c-spec", "RUNNING", "FEATURE_SPEC", target_terminal=True)
        assert self._is_idle(connection, tmp_path)() is False

    def test_a_build_paused_at_its_gate_and_nothing_else_is_idle(self, connection, tmp_path: Path) -> None:
        """MUTATION CHECK: count a PAUSED build as busy again and this goes red."""
        _insert_build(connection, "build-TEST3", "PAUSED")
        assert self._is_idle(connection, tmp_path)() is True

    def test_a_repair_running_with_its_merge_card_open_and_nothing_else_is_idle(self, connection, tmp_path: Path) -> None:
        """MUTATION CHECK: count every RUNNING build as busy again and this goes red.

        A repair (a fix journey, mode c) publishes its merge card and leaves
        its build RUNNING until the merge word closes it."""
        _insert_build(connection, "build-TEST4", "RUNNING", mode="mode-c")
        _offer_merge_card(connection, "build-TEST4", offered_at=datetime.now(timezone.utc) - timedelta(minutes=12))
        assert self._is_idle(connection, tmp_path)() is True

    def test_a_repair_running_once_its_merge_card_is_answered_is_busy(self, connection, tmp_path: Path) -> None:
        _insert_build(connection, "build-TEST4", "RUNNING", mode="mode-c")
        _offer_merge_card(connection, "build-TEST4", offered_at=datetime.now(timezone.utc) - timedelta(minutes=12))
        _answer_merge_card(connection, "build-TEST4")
        assert self._is_idle(connection, tmp_path)() is False

    @pytest.mark.parametrize("status", ["QUEUED", "PREPARING", "FINALISING"])
    def test_a_repair_run_again_with_its_merge_card_open_is_busy(self, connection, tmp_path: Path, status: str) -> None:
        """MUTATION CHECK: leave out an open-card build in every state and this goes red.

        After a coordinator restart a repair at its card is reset to PREPARING
        and run again under the same build id; its old card is still open."""
        _insert_build(connection, "build-TEST6", status, mode="mode-c")
        _offer_merge_card(connection, "build-TEST6", offered_at=datetime.now(timezone.utc) - timedelta(minutes=12))
        assert self._is_idle(connection, tmp_path)() is False

    @pytest.mark.parametrize("status", WORKING_BUILD_STATES)
    def test_every_working_build_state_is_busy(self, connection, tmp_path: Path, status: str) -> None:
        from forge.planning.work_queue_loop import BUILD_ACTIVE_STATES

        # The list here is the queue's own active states without PAUSED.
        assert set(WORKING_BUILD_STATES) == set(BUILD_ACTIVE_STATES) - {"PAUSED"}
        _insert_build(connection, "build-TEST5", status)
        assert self._is_idle(connection, tmp_path)() is False

    def test_an_admitted_mission_attempt_is_busy(self, connection, tmp_path: Path) -> None:
        from forge.lifecycle.missions import ensure_tables

        ensure_tables(connection)
        connection.execute(
            "INSERT INTO mission_attempts (mission_id, key, attempt, correlation_id, status) "
            "VALUES ('m-test01', 'A', 1, 'corr-mission-1', 'ADMITTED')"
        )
        connection.commit()
        assert self._is_idle(connection, tmp_path)() is False

    def test_an_active_build_is_busy(self, connection, tmp_path: Path) -> None:
        _insert_build(connection, "build-TEST2", "RUNNING")
        assert self._is_idle(connection, tmp_path)() is False

    def test_an_open_planning_run_is_busy(self, connection, tmp_path: Path) -> None:
        SqlitePlanningRunStore(connection).record_queued(
            correlation_id="c-1", originating_user="rich", expected_approver="rich",
            request_text="x", triggered_by="cli",
        )
        assert self._is_idle(connection, tmp_path)() is False

    def test_a_card_waiting_beside_real_work_is_busy(self, connection, tmp_path: Path) -> None:
        _insert_build(connection, "build-TEST3", "PAUSED")
        _planning_run(connection, "c-running", "RUNNING")
        assert self._is_idle(connection, tmp_path)() is False


class TestTheSettings:
    def test_off_by_default_with_the_designs_values(self) -> None:
        settings = SecuritySweepConfig()
        assert (settings.enabled, settings.at, settings.until) == (False, "02:30", "06:00")
        assert (settings.serious_score, settings.draft_repairs, settings.weekly_all_clear) == (
            7.0, True, "monday",
        )
        assert ForgeConfig(permissions={"filesystem": {"allowlist": []}}).security_sweep == settings

    def test_bad_values_are_refused_at_load(self) -> None:
        for bad in ({"at": "25:00"}, {"until": "6"}, {"weekly_all_clear": "someday"},
                    {"serious_score": 11}, {"unknown": 1}):
            with pytest.raises(ValidationError):
                SecuritySweepConfig(**bad)

    def test_times_and_days_are_normalised(self) -> None:
        settings = SecuritySweepConfig(at="2:30", weekly_all_clear="Friday")
        assert settings.at == "02:30" and settings.weekly_all_clear == "friday"
        assert SecuritySweepConfig(weekly_all_clear=None).weekly_all_clear is None


class TestTheCommand:
    def _config_file(self, tmp_path: Path, enabled: bool) -> Path:
        path = tmp_path / "forge.yaml"
        path.write_text(
            "permissions:\n  filesystem:\n    allowlist: []\n"
            f"security_sweep:\n  enabled: {'true' if enabled else 'false'}\n"
        )
        return path

    def test_it_needs_once(self, tmp_path: Path) -> None:
        from forge.cli.main import main

        result = CliRunner().invoke(main, ["--config", str(self._config_file(tmp_path, True)), "security-sweep"])
        assert result.exit_code == 2
        assert "Say --once" in result.output

    def test_switched_off_it_runs_nothing(self, tmp_path: Path) -> None:
        from forge.cli.main import main

        result = CliRunner().invoke(main, ["--config", str(self._config_file(tmp_path, False)), "security-sweep", "--once"])
        assert result.exit_code == 3
        assert "switched off" in result.output

    def test_a_busy_factory_is_not_swept_by_hand_either(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from forge.cli import security_sweep as command
        from forge.cli.main import main

        db = tmp_path / "forge.db"
        cx = sqlite_connect.connect_writer(db)
        migrations.apply_at_boot(cx)
        SqlitePlanningRunStore(cx).record_queued(
            correlation_id="c-1", originating_user="rich", expected_approver="rich",
            request_text="x", triggered_by="cli",
        )
        cx.close()
        monkeypatch.setenv("FORGE_DB_PATH", str(db))

        async def no_bus(url: str):
            raise ConnectionError("no bus in tests")

        monkeypatch.setattr(command, "_connect", no_bus)
        result = CliRunner().invoke(main, ["--config", str(self._config_file(tmp_path, True)), "security-sweep", "--once"])
        assert result.exit_code == 3
        assert "work in flight" in result.output
        assert not (tmp_path / "security-sweep" / "history.jsonl").exists()

    def test_the_command_is_registered(self) -> None:
        from forge.cli.main import main

        assert "security-sweep" in main.commands


class TestTheSlackPost:
    @pytest.mark.asyncio
    async def test_the_line_is_a_top_level_post_that_mentions_the_approver(self) -> None:
        import json as _json

        from forge.security_sweep.schedule import notification_publisher

        sent: list[tuple[str, bytes]] = []

        class Bus:
            async def publish(self, subject: str, body: bytes) -> None:
                sent.append((subject, body))

            async def flush(self) -> None:
                return None

        await notification_publisher(Bus(), "rich")("Nightly security check: app — nothing new.")
        ((subject, body),) = sent
        assert subject == "jarvis.notification.slack"
        payload = _json.loads(body)["payload"]
        assert payload["message"] == "Nightly security check: app — nothing new."
        assert payload["target_user"] == "rich"
        assert payload["parent_request_id"] is None and payload["thread_ts"] is None


class TestTheServeWiring:
    def test_compose_builds_a_schedule_from_the_coordinators_settings(self, tmp_path: Path) -> None:
        from forge.security_sweep.schedule import compose_nightly_schedule

        db = tmp_path / "forge.db"
        cx = sqlite_connect.connect_writer(db)
        migrations.apply_at_boot(cx)
        cx.close()
        config = ForgeConfig(
            permissions={"filesystem": {"allowlist": []}},
            planning={"fetch_addresses": {"org/app": "https://github.com/org/app.git"}},
            security_sweep={"enabled": True, "at": "01:15", "until": "05:00"},
        )
        schedule = compose_nightly_schedule(config, db_path=db, nats_client=object())
        assert (schedule.at, schedule.until) == ("01:15", "05:00")
        assert schedule.is_idle() is True

    def test_serve_runs_the_sweep_only_when_switched_on(self) -> None:
        import inspect

        from forge.cli import serve

        assert serve.security_sweep_config is None
        source = inspect.getsource(serve.serve_cmd.callback)
        assert "security_sweep_config" in source
        run_serve = inspect.getsource(serve._run_serve)
        assert "compose_nightly_schedule" in run_serve
        assert 'name="forge-serve-security-sweep"' in run_serve


def test_the_coordinator_is_handed_the_sweeps_own_model_names_empty_by_default() -> None:
    """Item 9, as decided: the coordinator gets FORGE_SWEEP_MODEL_* with empty
    defaults, and no GUARDKIT_STAMP_MODEL* name at all (that would switch on
    the plan's stamp check)."""
    import yaml

    root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((root / "deploy" / "compose" / "compose.yaml").read_text())
    environment = compose["services"]["coordinator"]["environment"]
    for name in ("FORGE_SWEEP_MODEL_URL", "FORGE_SWEEP_MODEL", "FORGE_SWEEP_MODEL_EXTRA_BODY"):
        assert environment[name] == "${" + name + ":-}"
    assert not [n for n in environment if n.startswith("GUARDKIT_STAMP_MODEL")]
    assert "OPENAI_BASE_URL" not in environment
    for example in ("deploy/estate/.env.example", "deploy/compose/.env.example"):
        text = (root / example).read_text()
        for name in ("FORGE_SWEEP_MODEL_URL", "FORGE_SWEEP_MODEL", "FORGE_SWEEP_MODEL_EXTRA_BODY"):
            assert f"\n{name}=\n" in text, f"{example} does not document {name}, empty"


def test_the_estate_coordinator_sets_no_stamp_model_name() -> None:
    """The estate's own compose file adds nothing GUARDKIT_STAMP_MODEL* to the
    coordinator either."""
    import yaml

    root = Path(__file__).resolve().parents[3]
    estate = yaml.safe_load((root / "deploy" / "estate" / "compose.yaml").read_text())
    environment = (estate.get("services", {}).get("coordinator") or {}).get("environment") or {}
    assert not [n for n in environment if str(n).startswith("GUARDKIT_STAMP_MODEL")]
