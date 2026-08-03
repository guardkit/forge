"""THE CAP LAW: a fix journey with no review-cycle cap does not open.

Leg-invocation stage-2 design §4, built belt-and-braces over ONE rule.

The defect this kills is measured, not imagined. The 2026-08-02 crossing
ran ~200 legs because the build carried no budget profile, resolved the
reserved ``attended`` profile, and every cap came back ``None`` — at
which point :meth:`Supervisor._enforce_mode_c_budget` is a strict no-op
that (until this lane) logged nothing at all. Nothing was capped and
nothing said so. Worse, the deployed ``forge.yaml`` spells out
``budget.profiles``, which SHADOWS the in-code defaults, so
``resolve('fix-journey')`` raises ``KeyError`` in production and the
router would have swallowed it into a silent drop onto the routine path.

So three things are proven here:

1. **The brace** (:mod:`forge.config.conductor`) — one statement of the
   rule, read by both surfaces, with ``resolve()``'s ``KeyError``
   surfacing as the same readable refusal instead of a traceback.
2. **The queue belt** — the refusal sees mode AND profile and fires
   before every side effect (no row, no publish, not even the budget
   echo).
3. **The router belt** — and its load-bearing negative: an uncapped fix
   journey must NEVER make the router answer ``DECLINED``, because
   DECLINED means "not mine, launch it the routine way". The build goes
   FAILED with the reason on the row instead. That property is tested
   twice: at the router, and end-to-end through ``dispatch_build`` where
   the routine launcher is recorded and must never be called.

Since the activation lane (design §3) the router speaks the
taken-and-terminal VOCABULARY rather than a bool: ``DECLINED`` /
``TAKEN_RUNNING`` / ``TakenTerminal(reason=...)``. The refusal therefore
does more than mark a row — it acks the JetStream slot and emits
``build-failed`` — which is what keeps a cap-refused build from wedging
the whole consumer for an hour under ``max_ack_pending=1``. Both are
asserted end to end here.

Zero broker contact: the queue's publisher seam is monkey-patched, the
router tests never construct a NATS client, and no test opens a socket.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import yaml
from click.testing import CliRunner

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps
from forge.cli import queue as cli_queue
from forge.cli._conductor_outcome import (
    DECLINED,
    TAKEN_RUNNING,
    ConductorOutcomeContractError,
    TakenTerminal,
)
from forge.cli._conductor_worktree import WorktreeReady
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.cli.serve import build_conductor_router
from forge.config.conductor import (
    MODE_C_MIN_REVIEW_CYCLES,
    UNCAPPED_ESCAPE_PROFILE_NAME,
    ModeCCapRefusal,
    mode_c_cap_refusal,
    mode_c_cap_refusal_from_config,
    uncapped_escape_applies,
)
from forge.config.models import (
    FIX_JOURNEY_PROFILE_NAME,
    BudgetConfig,
    BudgetGuards,
    ConductorConfig,
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.identifiers import derive_build_id
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState

# ---------------------------------------------------------------------------
# 1. The brace — one statement of the rule (forge/config/conductor.py)
# ---------------------------------------------------------------------------


class TestTheBrace:
    """:func:`mode_c_cap_refusal` — the whole law, in one place."""

    def test_a_cap_at_the_floor_opens_the_journey(self) -> None:
        assert (
            mode_c_cap_refusal(
                profile_name="fix-journey",
                guards=BudgetGuards(max_review_cycles=MODE_C_MIN_REVIEW_CYCLES),
            )
            is None
        )

    def test_a_cap_above_the_floor_opens_the_journey(self) -> None:
        assert (
            mode_c_cap_refusal(
                profile_name="generous",
                guards=BudgetGuards(max_review_cycles=9),
            )
            is None
        )

    def test_an_unset_cap_is_refused_never_read_as_unlimited(self) -> None:
        """The measured runaway shape: profile resolved, every cap None."""
        refusal = mode_c_cap_refusal(
            profile_name="attended", guards=BudgetGuards()
        )
        assert isinstance(refusal, ModeCCapRefusal)
        assert refusal.profile == "attended"
        # The operator is told WHICH profile and WHAT to do about it.
        assert "attended" in refusal.message
        assert "max_review_cycles" in refusal.message
        assert f"max_review_cycles: {MODE_C_MIN_REVIEW_CYCLES}" in refusal.message
        # The summary is one line — it goes on a database column.
        assert "\n" not in refusal.summary

    def test_a_cap_of_one_is_refused_as_the_trap_it_is(self) -> None:
        """1 is not "tighter" — it breaches at the mandatory follow-up."""
        refusal = mode_c_cap_refusal(
            profile_name="too-tight", guards=BudgetGuards(max_review_cycles=1)
        )
        assert refusal is not None
        assert "too-tight" in refusal.message
        assert "follow-up" in refusal.message
        assert str(MODE_C_MIN_REVIEW_CYCLES) in refusal.summary

    @pytest.mark.parametrize("cap", ["2", 2.5, object()])
    def test_a_cap_that_is_not_a_whole_number_is_refused(self, cap: Any) -> None:
        refusal = mode_c_cap_refusal(
            profile_name="odd", guards=SimpleNamespace(max_review_cycles=cap)
        )
        assert refusal is not None
        assert "not a whole number" in refusal.summary

    @pytest.mark.parametrize("cap", [True, False])
    def test_a_boolean_cap_lands_on_the_floor_refusal(self, cap: bool) -> None:
        """A bool IS an int in Python — and both values are below the floor."""
        refusal = mode_c_cap_refusal(
            profile_name="odd", guards=SimpleNamespace(max_review_cycles=cap)
        )
        assert refusal is not None
        assert "below the floor" in refusal.summary

    def test_an_object_with_no_cap_attribute_is_refused(self) -> None:
        refusal = mode_c_cap_refusal(profile_name="shapeless", guards=object())
        assert refusal is not None

    def test_the_profile_absent_arm_is_a_refusal_not_a_crash(self) -> None:
        """``resolve()``'s KeyError wears the same readable sentence."""
        refusal = mode_c_cap_refusal(
            profile_name=None,
            guards=None,
            resolve_error="unknown budget profile 'fix-journey'; known "
            "profiles: ['attended', 'unattended']",
        )
        assert refusal is not None
        assert "unknown budget profile 'fix-journey'" in refusal.message
        assert "could not be resolved" in refusal.summary


class TestTheHonestEscape:
    """The escape opens on BOTH halves and on nothing less."""

    def test_the_named_profile_plus_the_acknowledgment_opens_it(self) -> None:
        assert (
            mode_c_cap_refusal(
                profile_name=UNCAPPED_ESCAPE_PROFILE_NAME,
                guards=BudgetGuards(),
                uncapped_acknowledged=True,
            )
            is None
        )

    def test_the_named_profile_alone_is_still_refused(self) -> None:
        assert (
            mode_c_cap_refusal(
                profile_name=UNCAPPED_ESCAPE_PROFILE_NAME,
                guards=BudgetGuards(),
                uncapped_acknowledged=False,
            )
            is not None
        )

    def test_the_acknowledgment_never_opens_the_reserved_profile(self) -> None:
        """'attended' is reserved; the escape has to be asked for by name."""
        refusal = mode_c_cap_refusal(
            profile_name="attended",
            guards=BudgetGuards(),
            uncapped_acknowledged=True,
        )
        assert refusal is not None
        assert UNCAPPED_ESCAPE_PROFILE_NAME in refusal.message
        assert "attended" in refusal.message

    @pytest.mark.parametrize(
        "profile,ack,expected",
        [
            (UNCAPPED_ESCAPE_PROFILE_NAME, True, True),
            (UNCAPPED_ESCAPE_PROFILE_NAME, False, False),
            ("attended", True, False),
            (None, True, False),
            ("fix-journey", True, False),
        ],
    )
    def test_the_escape_predicate_truth_table(
        self, profile: str | None, ack: bool, expected: bool
    ) -> None:
        assert uncapped_escape_applies(profile, ack) is expected


class TestTheBraceOverAConfig:
    """:func:`mode_c_cap_refusal_from_config` — resolution failures included."""

    def _config(self, **budget: Any) -> ForgeConfig:
        return ForgeConfig(
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=[]),
            ),
            budget=BudgetConfig(**budget),
        )

    def test_the_default_profile_is_what_gets_judged(self) -> None:
        """No ``--profile`` means the config default — 'attended', uncapped."""
        refusal = mode_c_cap_refusal_from_config(self._config(), None)
        assert refusal is not None
        assert refusal.profile == "attended"

    def test_a_named_capped_profile_opens_the_journey(self) -> None:
        assert (
            mode_c_cap_refusal_from_config(self._config(), "fix-journey") is None
        )

    def test_the_production_shape_an_absent_fix_journey_block(self) -> None:
        """The deployed forge.yaml spells out profiles and shadows defaults."""
        config = self._config(
            default_profile="attended",
            profiles={
                "attended": BudgetGuards(),
                "unattended": BudgetGuards(max_review_cycles=2),
            },
        )
        refusal = mode_c_cap_refusal_from_config(config, "fix-journey")
        assert refusal is not None
        assert "unknown budget profile 'fix-journey'" in refusal.message
        assert "unattended" in refusal.message  # it lists what IS defined

    def test_a_config_with_no_budget_section_is_refused(self) -> None:
        refusal = mode_c_cap_refusal_from_config(object(), "fix-journey")
        assert refusal is not None
        assert "budget" in refusal.message

    def test_a_resolver_that_blows_up_is_a_refusal_not_a_traceback(self) -> None:
        class _Broken:
            default_profile = "boom"

            def resolve(self, name: str | None) -> Any:
                raise RuntimeError("the config is on fire")

        refusal = mode_c_cap_refusal_from_config(
            SimpleNamespace(budget=_Broken()), "boom"
        )
        assert refusal is not None
        assert "RuntimeError" in refusal.message
        assert "the config is on fire" in refusal.message


# ---------------------------------------------------------------------------
# 2. The queue belt — the refusal sees mode AND profile, before side effects
# ---------------------------------------------------------------------------


class _RecordingPersistence:
    """Records every call, so a refusal can be proved to have made none."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rows: list[tuple[Any, Any, str | None]] = []

    def exists_active_build(self, feature_id: str) -> bool:
        self.calls.append(("exists_active_build", feature_id))
        return False

    def queue_build(
        self,
        payload: Any,
        *,
        mode: Any = None,
        profile: str | None = None,
    ) -> str:
        self.calls.append(("queue_build", payload))
        self.rows.append((payload, mode, profile))
        return "build-1"

    def record_pending_build(self, payload: Any) -> str:  # pragma: no cover
        self.calls.append(("record_pending_build", payload))
        self.rows.append((payload, getattr(payload, "mode", None), None))
        return "build-1"


@pytest.fixture
def cli_persistence(monkeypatch: pytest.MonkeyPatch) -> _RecordingPersistence:
    fake = _RecordingPersistence()
    monkeypatch.setattr(cli_queue, "make_persistence", lambda config: fake)
    return fake


@pytest.fixture
def cli_published(
    monkeypatch: pytest.MonkeyPatch, cli_persistence: _RecordingPersistence
) -> list[tuple[str, bytes]]:
    captured: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        cli_queue, "publish", lambda subject, body: captured.append((subject, body))
    )
    return captured


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    return repo


@pytest.fixture
def fix_task_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "fix-task.yaml"
    path.write_text("name: fix\nparent_feature: FEAT-CAP1\n", encoding="utf-8")
    return path


def _write_config(
    tmp_path: Path,
    repo_dir: Path,
    *,
    name: str,
    budget: dict[str, Any] | None = None,
) -> Path:
    body: dict[str, Any] = {
        "queue": {"repo_allowlist": [str(repo_dir)]},
        "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
        "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
    }
    if budget is not None:
        body["budget"] = budget
    path = tmp_path / name
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


@pytest.fixture
def config_defaults(tmp_path: Path, repo_dir: Path) -> Path:
    """Conductor ON, no budget block — the in-code defaults apply."""
    return _write_config(tmp_path, repo_dir, name="forge-defaults.yaml")


@pytest.fixture
def config_production_shape(tmp_path: Path, repo_dir: Path) -> Path:
    """The deployed shape: profiles spelled out, no ``fix-journey`` block."""
    return _write_config(
        tmp_path,
        repo_dir,
        name="forge-prod.yaml",
        budget={
            "default_profile": "attended",
            "profiles": {
                "attended": {},
                "unattended": {"max_review_cycles": 2},
            },
        },
    )


@pytest.fixture
def config_with_escape(tmp_path: Path, repo_dir: Path) -> Path:
    return _write_config(
        tmp_path,
        repo_dir,
        name="forge-escape.yaml",
        budget={
            "default_profile": "attended",
            "profiles": {
                "attended": {},
                "fix-journey": {"max_review_cycles": 2},
                "too-tight": {"max_review_cycles": 1},
                UNCAPPED_ESCAPE_PROFILE_NAME: {},
            },
        },
    )


def _queue(
    config_path: Path,
    *,
    positional: str,
    repo_dir: Path,
    feature_yaml: Path,
    mode: str | None = "c",
    extra: list[str] | None = None,
):
    from forge.cli.main import main

    argv = [
        "--config",
        str(config_path),
        "queue",
        positional,
        "--repo",
        str(repo_dir),
        "--feature-yaml",
        str(feature_yaml),
    ]
    if mode is not None:
        argv += ["--mode", mode]
    argv += extra or []
    return CliRunner().invoke(main, argv)


class TestQueueBelt:
    def test_an_uncapped_fix_journey_is_refused_and_writes_nothing(
        self,
        config_defaults: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        """No ``--profile`` resolves 'attended': the runaway's exact shape."""
        result = _queue(
            config_defaults,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
        )
        assert result.exit_code == cli_queue.EXIT_MODE_USAGE
        assert cli_persistence.rows == []
        assert cli_persistence.calls == []
        assert cli_published == []
        assert "Nothing was queued." in result.output

    def test_the_refusal_fires_before_the_budget_echo(
        self,
        config_defaults: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        """Before EVERY side effect, output included."""
        result = _queue(
            config_defaults,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            extra=["--profile", "attended"],
        )
        assert result.exit_code != 0
        assert "budget profile" not in result.output

    def test_the_absent_profile_reads_as_a_sentence_not_a_traceback(
        self,
        config_production_shape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        """The production KeyError path — the one that actually bit."""
        result = _queue(
            config_production_shape,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            extra=["--profile", "fix-journey"],
        )
        assert result.exit_code == cli_queue.EXIT_MODE_USAGE
        assert result.exception is None or isinstance(
            result.exception, SystemExit
        ), result.exception
        assert "unknown budget profile 'fix-journey'" in result.output
        # Specifically the CAP LAW's refusal, not the older unknown-profile
        # UsageError that also exits 2 — the operator is told the journey
        # was refused and what to set, not just that a name was unknown.
        assert "the fix journey is refused" in result.output
        assert "Nothing was queued." in result.output
        assert f"max_review_cycles: {MODE_C_MIN_REVIEW_CYCLES}" in result.output
        assert cli_persistence.rows == []

    def test_a_cap_of_one_is_refused_at_the_queue(
        self,
        config_with_escape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        result = _queue(
            config_with_escape,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            extra=["--profile", "too-tight"],
        )
        assert result.exit_code == cli_queue.EXIT_MODE_USAGE
        assert cli_persistence.rows == []

    def test_a_capped_profile_queues_the_fix_journey(
        self,
        config_with_escape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        """The law is a gate, not a wall."""
        result = _queue(
            config_with_escape,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            extra=["--profile", "fix-journey"],
        )
        assert result.exit_code == 0, result.output
        assert len(cli_persistence.rows) == 1
        _, mode, profile = cli_persistence.rows[0]
        assert mode is BuildMode.MODE_C
        assert profile == "fix-journey"
        assert len(cli_published) == 1

    def test_the_routine_build_is_untouched_by_the_cap_law(
        self,
        config_defaults: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        """Mode-a under the uncapped default still queues — this is a mode-c law."""
        result = _queue(
            config_defaults,
            positional="FEAT-ROUTINE",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            mode="a",
        )
        assert result.exit_code == 0, result.output
        assert len(cli_persistence.rows) == 1

    def test_the_escape_needs_both_halves_and_says_so_out_loud(
        self,
        config_with_escape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        refused = _queue(
            config_with_escape,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            extra=["--profile", UNCAPPED_ESCAPE_PROFILE_NAME],
        )
        assert refused.exit_code == cli_queue.EXIT_MODE_USAGE
        assert cli_persistence.rows == []

    def test_the_acknowledged_escape_queues_loudly(
        self,
        config_with_escape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        result = _queue(
            config_with_escape,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            extra=[
                "--profile",
                UNCAPPED_ESCAPE_PROFILE_NAME,
                "--acknowledge-uncapped",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(cli_persistence.rows) == 1
        assert "UNCAPPED" in result.output
        # It must also say where the escape stops.
        assert "daemon" in result.output

    def test_the_acknowledgment_alone_opens_nothing(
        self,
        config_with_escape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        result = _queue(
            config_with_escape,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            extra=["--acknowledge-uncapped"],
        )
        assert result.exit_code == cli_queue.EXIT_MODE_USAGE
        assert cli_persistence.rows == []
        assert UNCAPPED_ESCAPE_PROFILE_NAME in result.output

    def test_the_acknowledgment_on_a_routine_build_says_it_did_nothing(
        self,
        config_with_escape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        """Accepted-and-ignored is a lie the operator cannot see.

        The flag is a mode-c door. On a routine build nothing reads it, so
        silence lets an operator believe they acknowledged something the
        machine heard. The build itself is unaffected — this is a NOTICE,
        never a refusal: the row is still written and the exit code is
        still 0.
        """
        result = _queue(
            config_with_escape,
            positional="FEAT-ROUTINE",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            mode="a",
            extra=["--acknowledge-uncapped"],
        )
        assert result.exit_code == 0, result.output
        assert len(cli_persistence.rows) == 1
        assert len(cli_published) == 1
        # STDERR, and stderr only — a machine consumer parsing the Queued line
        # on stdout must never see the notice.
        assert "--acknowledge-uncapped does nothing outside '--mode c'" in (
            result.stderr
        )
        assert "--acknowledge-uncapped does nothing" not in result.stdout

    def test_the_routine_build_without_the_flag_stays_silent(
        self,
        config_with_escape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        """The notice must fire on the flag, not on every routine build."""
        result = _queue(
            config_with_escape,
            positional="FEAT-ROUTINE",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            mode="a",
        )
        assert result.exit_code == 0, result.output
        assert "acknowledge-uncapped" not in result.output

    def test_the_capped_fix_journey_gets_no_ignored_flag_notice(
        self,
        config_with_escape: Path,
        repo_dir: Path,
        fix_task_yaml: Path,
        cli_persistence: _RecordingPersistence,
        cli_published: list,
    ) -> None:
        """Inside mode c the flag is real, so the notice must not appear."""
        result = _queue(
            config_with_escape,
            positional="TASK-CAP1",
            repo_dir=repo_dir,
            feature_yaml=fix_task_yaml,
            extra=[
                "--profile",
                "fix-journey",
                "--acknowledge-uncapped",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "does nothing outside" not in result.output


# ---------------------------------------------------------------------------
# 3. The router belt — loud FAIL, and NEVER a silent downgrade
# ---------------------------------------------------------------------------


class _StubNatsClient:
    """Records every publish so the terminal EMIT is provable, not assumed.

    Zero sockets: this is the whole transport for these tests.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        self.published.append((subject, body))
        return None

    def subjects(self) -> list[str]:
        return [subject for subject, _body in self.published]


class _RecordingStarter:
    def start_async_task(self, subagent_name: str, context: dict) -> str:
        return "task-cap-law"

    async def astart_async_task(self, subagent_name: str, context: dict) -> str:
        return "task-cap-law"


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(
    writer_db: sqlite3.Connection, tmp_path: Path
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(
        connection=writer_db, db_path=tmp_path / "forge.db"
    )


def _payload(feature_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/fix.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="cap-law-test",
        correlation_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        parent_request_id=None,
        queued_at=datetime(2026, 8, 2, 9, 0, 0, tzinfo=UTC),
    )


async def _writer_ok(_pool: Any, _config: Any, build_id: str) -> Any:
    """A worktree writer that always succeeds (activation design §1).

    The router materialises the journey's worktree between the cap-law
    belt and the spawn. These are CAP-LAW and SETUP-SEAM tests: injecting
    a satisfied writer keeps each one on the seam it is actually about.
    The writer's own arms — reuse, collision, allowlist, materialise
    failure, the gitignore guard — are driven against a real scratch git
    checkout in ``tests/forge/test_conductor_worktree.py``.
    """
    return WorktreeReady(path=f"/srv/forge/{build_id}", branch="fix/TASK-CAP-0000")


def _router_config(tmp_path: Path, **budget: Any) -> ForgeConfig:
    return ForgeConfig(
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[tmp_path]),
        ),
        conductor=ConductorConfig(enabled=True, seat="qwen3-coder-30b"),
        budget=BudgetConfig(**budget) if budget else BudgetConfig(),
    )


class TestRouterBelt:
    @pytest.mark.asyncio
    async def test_an_uncapped_fix_journey_is_taken_and_failed_not_downgraded(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        """THE property: TakenTerminal, never DECLINED (the routine path)."""
        build_id = persistence.record_pending_build(
            _payload("FEAT-CAPR1"), mode=BuildMode.MODE_C
        )
        made: list[str] = []
        spawned: list[Any] = []

        router = build_conductor_router(
            pool=persistence,
            config=_router_config(tmp_path),
            supervisor_factory=lambda bid: made.append(bid),
            spawn=lambda coro: spawned.append(coro),
        )
        assert router is not None

        taken = await router(build_id=build_id)

        assert isinstance(taken, TakenTerminal), (
            "an uncapped fix journey answered "
            f"{taken!r} — anything but a TakenTerminal is the SILENT "
            "DOWNGRADE (DECLINED) or a slot held for an hour (a bare "
            "'taken' with no terminal to ack on)"
        )
        assert taken is not DECLINED
        # The REASON rides the outcome, so the build-failed emit needs no
        # ``builds.error`` re-read to be honest.
        assert "refused" in taken.reason
        assert made == []  # no supervisor was constructed
        assert spawned == []  # no turn loop was spawned

        row = persistence.get_build_row(build_id)
        assert row is not None
        assert row.status is BuildState.FAILED
        assert row.error is not None
        assert "refused" in row.error
        assert "\n" not in row.error  # the one-line summary, not the essay
        assert taken.reason == row.error  # one rule, one reason

    @pytest.mark.asyncio
    async def test_the_absent_profile_keyerror_is_a_failed_row_not_a_crash(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        """The production shape: a 'fix-journey' row on a config without it."""
        build_id = persistence.record_pending_build(
            _payload("FEAT-CAPR2"),
            mode=BuildMode.MODE_C,
            profile="fix-journey",
        )
        config = _router_config(
            tmp_path,
            default_profile="attended",
            profiles={
                "attended": BudgetGuards(),
                "unattended": BudgetGuards(max_review_cycles=2),
            },
        )
        router = build_conductor_router(
            pool=persistence,
            config=config,
            supervisor_factory=lambda bid: pytest.fail(
                "a supervisor was built for an unresolvable profile"
            ),
        )
        assert router is not None

        outcome = await router(build_id=build_id)
        assert isinstance(outcome, TakenTerminal)
        assert "could not be resolved" in outcome.reason
        row = persistence.get_build_row(build_id)
        assert row is not None and row.status is BuildState.FAILED
        assert "could not be resolved" in (row.error or "")

    @pytest.mark.asyncio
    async def test_a_capped_fix_journey_still_opens(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        """The belt is a gate: a capped journey passes through untouched."""
        build_id = persistence.record_pending_build(
            _payload("FEAT-CAPR3"),
            mode=BuildMode.MODE_C,
            profile="fix-journey",
        )
        spawned: list[Any] = []

        def spawn(coro: Any) -> Any:
            spawned.append(coro)
            coro.close()
            return None

        router = build_conductor_router(
            pool=persistence,
            config=_router_config(tmp_path),
            supervisor_factory=lambda bid: object(),
            spawn=spawn,
            worktree_writer=_writer_ok,
        )
        assert router is not None

        assert await router(build_id=build_id) is TAKEN_RUNNING
        assert len(spawned) == 1
        row = persistence.get_build_row(build_id)
        assert row is not None and row.status is not BuildState.FAILED

    @pytest.mark.asyncio
    async def test_the_daemon_never_honours_the_queue_side_escape(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        """A ``sandbox-uncapped`` row is refused outright by the daemon."""
        build_id = persistence.record_pending_build(
            _payload("FEAT-CAPR4"),
            mode=BuildMode.MODE_C,
            profile=UNCAPPED_ESCAPE_PROFILE_NAME,
        )
        config = _router_config(
            tmp_path,
            default_profile="attended",
            profiles={
                "attended": BudgetGuards(),
                UNCAPPED_ESCAPE_PROFILE_NAME: BudgetGuards(),
            },
        )
        router = build_conductor_router(
            pool=persistence,
            config=config,
            supervisor_factory=lambda bid: pytest.fail("escape honoured"),
        )
        assert router is not None

        assert isinstance(await router(build_id=build_id), TakenTerminal)
        row = persistence.get_build_row(build_id)
        assert row is not None and row.status is BuildState.FAILED

    @pytest.mark.asyncio
    async def test_a_routine_build_still_declines_to_the_routine_path(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        """The 'conductor declined, routine proceeds' semantics are intact.

        The uncapped default config would refuse a mode-c build — a mode-a
        build under exactly the same config must still answer
        ``DECLINED``.
        """
        build_id = persistence.record_pending_build(_payload("FEAT-CAPR5"))
        router = build_conductor_router(
            pool=persistence,
            config=_router_config(tmp_path),
            supervisor_factory=lambda bid: pytest.fail("mode-a took the conductor"),
        )
        assert router is not None
        assert await router(build_id=build_id) is DECLINED

    @pytest.mark.asyncio
    async def test_a_resolver_that_blows_up_fails_closed(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        """An unreadable budget is not evidence that a cap exists.

        (The pool's own faults are caught earlier, by the mode read's
        pre-existing degrade rail — this arm covers everything after it.)
        """
        build_id = persistence.record_pending_build(
            _payload("FEAT-CAPR6"), mode=BuildMode.MODE_C
        )

        class _BrokenBudget:
            default_profile = "attended"

            def resolve(self, name: str | None) -> Any:
                raise RuntimeError("the budget config fell over")

        config = SimpleNamespace(
            conductor=SimpleNamespace(enabled=True),
            budget=_BrokenBudget(),
        )
        router = build_conductor_router(
            pool=persistence,
            config=config,
            supervisor_factory=lambda bid: pytest.fail("supervisor built"),
        )
        assert router is not None

        # Fails closed: taken + refused, never downgraded to the routine path.
        assert isinstance(await router(build_id=build_id), TakenTerminal)
        row = persistence.get_build_row(build_id)
        assert row is not None and row.status is BuildState.FAILED

    @pytest.mark.asyncio
    async def test_an_unmarkable_row_still_refuses_and_never_downgrades(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The row write can fail; the refusal still holds, loudly."""
        build_id = persistence.record_pending_build(
            _payload("FEAT-CAPR7"), mode=BuildMode.MODE_C
        )

        class _WriteRefusingPool:
            def __init__(self, real: Any) -> None:
                self._real = real

            def read_build_mode(self, bid: str) -> Any:
                return self._real.read_build_mode(bid)

            def get_build_row(self, bid: str) -> Any:
                return self._real.get_build_row(bid)

            def apply_transition(self, hop: Any) -> None:
                raise RuntimeError("the row is wedged")

        router = build_conductor_router(
            pool=_WriteRefusingPool(persistence),
            config=_router_config(tmp_path),
            supervisor_factory=lambda bid: pytest.fail("supervisor built"),
        )
        assert router is not None

        with caplog.at_level(logging.ERROR, logger="forge.cli.serve"):
            outcome = await router(build_id=build_id)
        assert isinstance(outcome, TakenTerminal)
        assert any("needs a hand" in r.getMessage() for r in caplog.records)
        # The degraded arm is NAMED in the reason the terminal carries, so
        # the emitted build-failed never claims a clean refusal that did
        # not actually land on the row.
        assert "could NOT be marked FAILED" in outcome.reason


class TestTheSetupExceptionSeam:
    """SEAM 1 (activation design §4.1) — the arm that used to say False.

    A mode-c build whose supervisor / driver-deps factory throws used to
    ``return False`` — literally below the comment forbidding it — and the
    fix task then ran as a ROUTINE autobuild against a TASK-xxx subject.
    It is taken-and-terminal now, with the exception NAMED.
    """

    @pytest.mark.asyncio
    async def test_a_failing_setup_is_terminal_with_the_exception_named(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        build_id = persistence.record_pending_build(
            _payload("FEAT-CAPS1"),
            mode=BuildMode.MODE_C,
            profile=FIX_JOURNEY_PROFILE_NAME,
        )

        def exploding_factory(bid: str) -> Any:
            raise RuntimeError("no supervisor today")

        router = build_conductor_router(
            pool=persistence,
            config=_router_config(tmp_path),
            supervisor_factory=exploding_factory,
            worktree_writer=_writer_ok,
        )
        assert router is not None

        outcome = await router(build_id=build_id)

        assert isinstance(outcome, TakenTerminal), (
            "a failing per-build setup answered "
            f"{outcome!r} — DECLINED here is the silent downgrade §4.1 "
            "closes"
        )
        assert outcome is not DECLINED
        assert "RuntimeError" in outcome.reason
        assert "no supervisor today" in outcome.reason

        row = persistence.get_build_row(build_id)
        assert row is not None
        assert row.status is BuildState.FAILED
        assert "RuntimeError" in (row.error or "")
        assert "\n" not in (row.error or "")

    @pytest.mark.asyncio
    async def test_a_failing_deps_factory_is_terminal_too(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        """The second half of the same arm — deps, not supervisor."""
        build_id = persistence.record_pending_build(
            _payload("FEAT-CAPS2"),
            mode=BuildMode.MODE_C,
            profile=FIX_JOURNEY_PROFILE_NAME,
        )

        def exploding_deps(bid: str, sup: Any) -> Any:
            raise ValueError("the driver deps would not compose")

        router = build_conductor_router(
            pool=persistence,
            config=_router_config(tmp_path),
            supervisor_factory=lambda bid: object(),
            driver_deps_factory=exploding_deps,
            spawn=lambda coro: pytest.fail("a journey was spawned"),
            worktree_writer=_writer_ok,
        )
        assert router is not None

        outcome = await router(build_id=build_id)
        assert isinstance(outcome, TakenTerminal)
        assert "ValueError" in outcome.reason
        row = persistence.get_build_row(build_id)
        assert row is not None and row.status is BuildState.FAILED


class TestTheWidenedContract:
    """The contract is REPLACED, not dual-shaped (activation design §3)."""

    @pytest.mark.asyncio
    async def test_a_legacy_bare_bool_router_refuses_loudly(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale ``return False`` must never be read as 'not mine'.

        If the widened seam quietly tolerated the legacy bool, a
        half-migrated router would downgrade every fix journey in silence
        — the exact class this vocabulary exists to abolish. It raises.
        """
        launched: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            launched.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        async def legacy_router(**kwargs: Any) -> bool:
            return False

        deps = build_pipeline_consumer_deps(
            _StubNatsClient(),
            _router_config(tmp_path),
            persistence,
            async_task_starter=_RecordingStarter(),
            conductor_router=legacy_router,
        )

        async def _ack() -> None:
            return None

        with pytest.raises(ConductorOutcomeContractError) as excinfo:
            await deps.dispatch_build(_payload("FEAT-LEGACY"), _ack)

        assert "LEGACY bare-bool" in str(excinfo.value)
        assert launched == []

    @pytest.mark.asyncio
    async def test_a_legacy_true_is_refused_just_as_loudly(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``True`` is no better: it cannot say running-vs-terminal."""

        async def _recording_dispatch(**kwargs: Any) -> Any:
            pytest.fail("a routine build was launched")

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        async def legacy_router(**kwargs: Any) -> bool:
            return True

        deps = build_pipeline_consumer_deps(
            _StubNatsClient(),
            _router_config(tmp_path),
            persistence,
            async_task_starter=_RecordingStarter(),
            conductor_router=legacy_router,
        )

        async def _ack() -> None:
            return None

        with pytest.raises(ConductorOutcomeContractError):
            await deps.dispatch_build(_payload("FEAT-LEGACY2"), _ack)

    def test_a_terminal_with_nothing_to_say_is_refused_at_construction(
        self,
    ) -> None:
        """An empty reason would publish an empty ``failure_reason``."""
        with pytest.raises(ValueError):
            TakenTerminal(reason="   ")


class TestNeverDowngradedEndToEnd:
    """The property that matters, at the seam it would have broken."""

    @pytest.mark.asyncio
    async def test_dispatch_never_launches_a_routine_build_for_a_fix_task(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``dispatch_build`` + a fix journey = zero routine launches, ever.

        ``DECLINED`` from the router is what the routine launcher listens
        for. This test drives the REAL dispatch closure with the REAL
        router and records every call into the routine launcher.

        Note what this composition is: no approval gate is bound, so the
        build meets the DDR-007 no-gate soft-fail arm — and since the
        activation lane that arm has its OWN mode-c refusal (design §4.4:
        the fix journey's safety story is the pre-dispatch card, so for
        mode-c the gate is load-bearing, not best-effort). The refusal
        that fires here is therefore the gate-less one; the cap-law
        router belt's own end-to-end ack/emit proof rides the GATED
        composition in
        ``tests/integration/test_gate_activation_production_wiring.py``.
        Either way the property under test is the same and it holds:
        nothing routine is launched for a TASK-xxx subject, the slot is
        acked in-line, and a terminal reaches the wire.
        """
        launched: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            launched.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        config = _router_config(tmp_path)
        router = build_conductor_router(
            pool=persistence,
            config=config,
            supervisor_factory=lambda bid: pytest.fail("supervisor built"),
        )
        assert router is not None

        nats = _StubNatsClient()
        deps = build_pipeline_consumer_deps(
            nats,
            config,
            persistence,
            async_task_starter=_RecordingStarter(),
            conductor_router=router,
        )

        payload = _payload("FEAT-CAPE1")
        payload.mode = BuildMode.MODE_C

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        await deps.dispatch_build(payload, _ack)

        assert launched == [], (
            "an uncapped fix journey was launched as a ROUTINE build — the "
            "silent downgrade the router belt exists to prevent"
        )
        # The ack cure (§3): the slot is released IN-LINE, not an hour
        # later at the JetStream ack_wait expiry. Under max_ack_pending=1
        # the un-acked refusal wedged the WHOLE consumer for that hour.
        assert acked == [True], (
            "a refused fix journey did not ack its queue slot — under "
            "max_ack_pending=1 that wedges the entire consumer until the "
            "1h ack_wait redelivery"
        )
        # And a terminal reached the wire, so a correlation-id-following
        # observer is not left waiting forever.
        assert f"pipeline.build-failed.{payload.feature_id}" in nats.subjects()
        row = persistence.get_build_row(
            derive_build_id(payload.feature_id, payload.queued_at)
        )
        assert row is not None and row.status is BuildState.FAILED

    @pytest.mark.asyncio
    async def test_the_no_gate_arm_refuses_a_fix_journey_with_the_reason_on_the_wire(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SEAM 4 (design §4.4) named at its own seam, reason and all.

        DDR-007's soft-fail is the RULED posture for routine builds and is
        untouched. For a fix journey it would open an UNATTENDED journey,
        so mode-c refuses — and says so on the wire.
        """
        launched: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            launched.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        config = _router_config(tmp_path)
        nats = _StubNatsClient()
        deps = build_pipeline_consumer_deps(
            nats,
            config,
            persistence,
            async_task_starter=_RecordingStarter(),
            # No router at all: the flag-off boot. The queue-time belt
            # already refuses a mode-c queue here, so this row is the
            # "unreachable but guarded" shape.
            conductor_router=None,
        )

        payload = _payload("FEAT-CAPE2")
        payload.mode = BuildMode.MODE_C

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        await deps.dispatch_build(payload, _ack)

        assert launched == []
        assert acked == [True]
        failed = [
            json.loads(body)["payload"]
            for subject, body in nats.published
            if subject == f"pipeline.build-failed.{payload.feature_id}"
        ]
        assert len(failed) == 1
        assert failed[0]["recoverable"] is False
        assert "gate" in failed[0]["failure_reason"]
        assert "mode-c" in failed[0]["failure_reason"]

    @pytest.mark.asyncio
    async def test_a_routine_build_on_the_no_gate_arm_still_launches(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The mutation guard: seam 4 must not brick the ROUTINE path.

        DDR-007's soft-fail posture is Rich's ruling for routine builds.
        A guard that refused everything would pass the seam-4 test above
        and break production — so the negative is pinned here.
        """
        launched: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            launched.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        deps = build_pipeline_consumer_deps(
            _StubNatsClient(),
            _router_config(tmp_path),
            persistence,
            async_task_starter=_RecordingStarter(),
            conductor_router=None,
        )

        acked: list[bool] = []

        async def _ack() -> None:
            acked.append(True)

        await deps.dispatch_build(_payload("FEAT-CAPE3"), _ack)

        assert len(launched) == 1
        assert acked == []  # a live build's ack rides its own terminal
