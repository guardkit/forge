"""A configuration-error stop is told to a person and files no repair (19 Oct 2026).

GuardKit stops a task before any model call, with the final decision
``configuration_error``, when the project's setup cannot support it (for
example a feature task in a project that declares no ``toolchain.coverage``).
A repair build changes code, not a project's setup, so:

* the runner records the stop in the failure manifest and puts GuardKit's own
  sentence, which names the fix, into the build's failure reason — the text
  every failed-build notice (Slack, the queue row, the dashboard) shows;
* the repair-row producer reads the manifest and files no repair row.

How the runner knows (the R3 rule of 18 October): GuardKit's task result line
``✗ TASK-X: configuration_error (0 turns)`` on the stdout of the process THIS
run started. Only for a task named there is GuardKit's sentence read from the
feature ledger; a ledger entry alone is never used.

The runner tests start a REAL child process (a fake GuardKit shell script)
through the runner's own launch and pipe, as
``test_autobuild_runner_stopped_because.py`` does. The connected test then
hands the manifest that real path wrote to the real producer over a real
SQLite ledger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest
import yaml
from langchain_core.messages import HumanMessage
from nats_core.events import BuildFailedPayload, BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle_bridge.build_state_recorder import build_build_state_recorder
from forge.pipeline.fix_row_producer import SOURCE_BUILD_FAILED, maybe_mint_fix_row
from forge.subagents import autobuild_runner as ar
from forge.subagents import build_monitor as bm

FEATURE_ID = "FEAT-CFG1"
TASK_ID = "TASK-CFG1-001"
OTHER_TASK = "TASK-CFG1-002"
COVERAGE_SENTENCE = (
    "This project declares no coverage command (`toolchain.coverage`) in "
    ".guardkit/config.yaml, and a feature task needs measured coverage to "
    "pass. Nothing else measures coverage, so this task cannot pass however "
    "good the code is; it was stopped before any work was done. To fix it, "
    'add `coverage: "<command that exits 0 when the coverage threshold is '
    'met>"` under `toolchain:` in .guardkit/config.yaml, then run the task '
    "again. Only feature, refactor, glue or wiring tasks need this; other "
    "task types are unaffected."
)


def _result_line(
    task_id: str = TASK_ID, decision: str = "configuration_error", turns: int = 0
) -> str:
    """GuardKit's own line, as ``feature_orchestrator`` prints it."""
    return f"    ✗ {task_id}: {decision} ({turns} turns)"


def _fake_guardkit(tmp_path: Path, lines: list[str], exit_code: int) -> Path:
    script = tmp_path / "bin" / "guardkit"
    script.parent.mkdir(parents=True, exist_ok=True)
    body = ["#!/bin/sh"]
    for line in lines:
        quoted = line.replace("'", "'\"'\"'")
        body.append(f"printf '%s\\n' '{quoted}'")
    body.append(f"exit {exit_code}")
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _write_ledger(worktree: Path, tasks: list[dict[str, Any]]) -> None:
    """The feature ledger GuardKit saves as each task finishes."""
    path = worktree / bm.FEATURES_DIR / f"{FEATURE_ID}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "id": FEATURE_ID,
                "tasks": tasks,
                "execution": {"tasks_completed": 0, "tasks_failed": len(tasks)},
            }
        ),
        encoding="utf-8",
    )


def _stopped_task(
    task_id: str = TASK_ID,
    error: str | None = f"Stopped before any model call: {COVERAGE_SENTENCE}",
    decision: str = "configuration_error",
) -> dict[str, Any]:
    return {
        "id": task_id,
        "status": "failed",
        "result": {"total_turns": 0, "final_decision": decision, "error": error},
    }


def _launch_state(build_id: str) -> dict[str, Any]:
    payload = {
        "build_id": build_id,
        "feature_id": FEATURE_ID,
        "repo": "guardkit/notes-fixture",
        "branch": "lane/config-error",
        "correlation_id": "corr-cfg-1",
    }
    description = (
        "RUN_AUTOBUILD subagent=autobuild_runner "
        f"payload={json.dumps(payload, sort_keys=True)}"
    )
    return {"messages": [HumanMessage(content=description)]}


async def _run_node(
    guardkit: Path, *, worktree: Path, repo: Path, build_id: str
) -> dict[str, Any]:
    async def _no_sweep(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _branch_exists(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _materialise(*_args: Any, **_kwargs: Any) -> Path:
        return worktree

    async def _remove(*_args: Any, **_kwargs: Any) -> None:
        return None

    with patch.object(ar, "_resolve_repo_path", lambda payload: repo), patch.object(
        ar, "_resolve_guardkit_path", lambda: guardkit
    ), patch.object(ar, "_local_branch_exists", _branch_exists), patch.object(
        ar, "_sweep_build_refs", _no_sweep
    ), patch.object(ar, "_materialise_worktree", _materialise), patch.object(
        ar, "_remove_worktree", _remove
    ):
        return await ar._node_running_wave(_launch_state(build_id))  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(root))
    monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")
    monkeypatch.delenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, raising=False)
    return root


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "wt"
    (worktree / ".guardkit" / "autobuild-private").mkdir(parents=True)
    return repo, worktree


def _manifest(receipts: Path, build_id: str) -> dict[str, Any]:
    return json.loads(
        (receipts / build_id / ar.FAILURE_MANIFEST_NAME).read_text(encoding="utf-8")
    )


BUILD_ID = "build-FEAT-CFG1-1"


class TestTheRunnerTellsAPerson:
    @pytest.mark.asyncio
    async def test_the_stop_reaches_the_reason_and_the_manifest(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        repo, worktree = _dirs(tmp_path)
        _write_ledger(worktree, [_stopped_task()])
        guardkit = _fake_guardkit(
            tmp_path, ["INFO: wave 1", _result_line()], exit_code=2
        )

        result = await _run_node(
            guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID
        )

        manifest = _manifest(receipts, BUILD_ID)
        assert manifest[ar.CONFIGURATION_ERRORS_FIELD] == [
            {
                "task_id": TASK_ID,
                "turns": 0,
                "message": f"Stopped before any model call: {COVERAGE_SENTENCE}",
            }
        ]
        reason = manifest["reason"]
        # Today's start of the reason, byte for byte, then the plain sentence.
        assert reason.startswith(
            f"guardkit autobuild exit=2: GuardKit stopped {TASK_ID} before any "
            "work because the project's setup cannot support it."
        )
        assert "A repair build cannot change the project's setup, so none was filed" in reason
        assert f"GuardKit says about {TASK_ID}: Stopped before any model call: " in reason
        assert COVERAGE_SENTENCE in reason
        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        # The failed-build notice reads this field (failure_reason on the wire).
        assert snap["error_message"] == ar._with_worktree_forensics(reason, worktree)

    @pytest.mark.asyncio
    async def test_two_tasks_stopped_are_both_named(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        repo, worktree = _dirs(tmp_path)
        _write_ledger(
            worktree,
            [
                _stopped_task(),
                _stopped_task(OTHER_TASK, error="Configuration error: task_type 'x'."),
            ],
        )
        guardkit = _fake_guardkit(
            tmp_path,
            [_result_line(), _result_line(OTHER_TASK), _result_line()],
            exit_code=2,
        )

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        manifest = _manifest(receipts, BUILD_ID)
        assert [e["task_id"] for e in manifest[ar.CONFIGURATION_ERRORS_FIELD]] == [
            TASK_ID,
            OTHER_TASK,
        ]
        assert f"GuardKit stopped 2 tasks ({TASK_ID}, {OTHER_TASK})" in manifest["reason"]
        assert "Configuration error: task_type 'x'." in manifest["reason"]

    @pytest.mark.asyncio
    async def test_a_stop_the_ledger_records_without_words_still_counts(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        repo, worktree = _dirs(tmp_path)
        _write_ledger(worktree, [_stopped_task(error=None)])
        guardkit = _fake_guardkit(tmp_path, [_result_line()], exit_code=2)

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        manifest = _manifest(receipts, BUILD_ID)
        assert manifest[ar.CONFIGURATION_ERRORS_FIELD] == [
            {"task_id": TASK_ID, "turns": 0, "message": None}
        ]
        assert "the build log has them" in manifest["reason"]

    @pytest.mark.asyncio
    async def test_a_stop_after_turns_does_not_say_before_any_work(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        """GuardKit also ends a task as a configuration error after model turns
        (the reviewer's invalid-task-type flag, the check that could not run).
        Still no repair, but the words must stay true (coach F2)."""
        repo, worktree = _dirs(tmp_path)
        _write_ledger(worktree, [_stopped_task(error="Configuration error: x.")])
        guardkit = _fake_guardkit(tmp_path, [_result_line(turns=3)], exit_code=2)

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        manifest = _manifest(receipts, BUILD_ID)
        assert manifest[ar.CONFIGURATION_ERRORS_FIELD][0]["turns"] == 3
        assert "before any work" not in manifest["reason"]
        assert manifest["reason"].startswith(
            f"guardkit autobuild exit=2: GuardKit stopped {TASK_ID} because the "
            "project's setup cannot support it."
        )

    @pytest.mark.asyncio
    async def test_the_same_words_for_several_tasks_are_given_once(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        repo, worktree = _dirs(tmp_path)
        _write_ledger(worktree, [_stopped_task(), _stopped_task(OTHER_TASK)])
        guardkit = _fake_guardkit(
            tmp_path, [_result_line(), _result_line(OTHER_TASK)], exit_code=2
        )

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        reason = _manifest(receipts, BUILD_ID)["reason"]
        assert reason.count(COVERAGE_SENTENCE) == 1
        assert f"GuardKit says about {TASK_ID}, {OTHER_TASK}: " in reason

    @pytest.mark.asyncio
    async def test_long_many_line_words_are_one_bounded_line(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        repo, worktree = _dirs(tmp_path)
        long_words = "\n".join(f"line {n} of the reason" + "." * 40 for n in range(60))
        assert len(long_words) > 3000
        _write_ledger(worktree, [_stopped_task(error=long_words)])
        guardkit = _fake_guardkit(tmp_path, [_result_line()], exit_code=2)

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        manifest = _manifest(receipts, BUILD_ID)
        message = manifest[ar.CONFIGURATION_ERRORS_FIELD][0]["message"]
        assert "\n" not in message and "\n" not in manifest["reason"]
        assert message.startswith("line 0 of the reason....")
        assert message.index("\u2026 [cut:") == ar._CONFIGURATION_ERROR_MAX_CHARS

    @pytest.mark.asyncio
    async def test_if_the_manifest_cannot_be_written_no_repair_claim_is_made(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        """Without the manifest the producer cannot see the stop, so the
        notice must not say no repair was filed (coach F4)."""
        repo, worktree = _dirs(tmp_path)
        _write_ledger(worktree, [_stopped_task()])
        guardkit = _fake_guardkit(tmp_path, [_result_line()], exit_code=2)

        with patch.object(ar, "_write_failure_manifest", lambda **_kwargs: None):
            result = await _run_node(
                guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID
            )

        said = result["async_tasks"][FEATURE_ID]["error_message"]
        assert said.startswith(
            f"guardkit autobuild exit=2: GuardKit stopped {TASK_ID} before any work "
            "because the project's setup cannot support it. The project needs the "
            "change GuardKit names"
        )
        assert "repair" not in said
        assert COVERAGE_SENTENCE in said

    @pytest.mark.asyncio
    async def test_a_secret_in_guardkits_words_is_replaced(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        repo, worktree = _dirs(tmp_path)
        secret = "ghp_" + "A" * 36
        _write_ledger(worktree, [_stopped_task(error=f"token {secret} in config")])
        guardkit = _fake_guardkit(tmp_path, [_result_line()], exit_code=2)

        result = await _run_node(
            guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID
        )

        assert secret not in json.dumps(_manifest(receipts, BUILD_ID))
        assert secret not in json.dumps(result["async_tasks"][FEATURE_ID])


class TestNothingChangesOtherwise:
    @pytest.mark.asyncio
    async def test_another_failed_decision_leaves_the_reason_as_today(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        repo, worktree = _dirs(tmp_path)
        _write_ledger(worktree, [_stopped_task(decision="unrecoverable_stall")])
        guardkit = _fake_guardkit(
            tmp_path, [_result_line(decision="unrecoverable_stall")], exit_code=2
        )

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        manifest = _manifest(receipts, BUILD_ID)
        assert manifest["reason"] == "guardkit autobuild exit=2"
        assert ar.CONFIGURATION_ERRORS_FIELD not in manifest

    @pytest.mark.asyncio
    async def test_a_ledger_entry_alone_is_never_used(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        """An earlier run's record in a kept worktree, and this run's GuardKit
        printed no such line: the stop is not this run's."""
        repo, worktree = _dirs(tmp_path)
        _write_ledger(worktree, [_stopped_task()])
        guardkit = _fake_guardkit(tmp_path, ["INFO: failed"], exit_code=2)

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        manifest = _manifest(receipts, BUILD_ID)
        assert manifest["reason"] == "guardkit autobuild exit=2"
        assert ar.CONFIGURATION_ERRORS_FIELD not in manifest

    @pytest.mark.asyncio
    async def test_a_printed_line_alone_is_never_used(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        """Stdout names the stop but there is no ledger: the stream also carries
        a project's raw output, so the line alone proves nothing (coach F1)."""
        repo, worktree = _dirs(tmp_path)  # no ledger at all
        guardkit = _fake_guardkit(tmp_path, [_result_line()], exit_code=2)

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        manifest = _manifest(receipts, BUILD_ID)
        assert manifest["reason"] == "guardkit autobuild exit=2"
        assert ar.CONFIGURATION_ERRORS_FIELD not in manifest

    @pytest.mark.asyncio
    async def test_the_ledger_must_agree_for_the_same_task(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        """Stdout names the stop; the ledger records something else for that
        task. Not a stop: the build is repaired as before (coach F1)."""
        repo, worktree = _dirs(tmp_path)
        _write_ledger(
            worktree, [_stopped_task(decision="max_turns_exceeded", error="old words")]
        )
        guardkit = _fake_guardkit(tmp_path, [_result_line()], exit_code=2)

        await _run_node(guardkit, worktree=worktree, repo=repo, build_id=BUILD_ID)

        manifest = _manifest(receipts, BUILD_ID)
        assert manifest["reason"] == "guardkit autobuild exit=2"
        assert ar.CONFIGURATION_ERRORS_FIELD not in manifest


class TestTheLineReader:
    def test_reads_guardkits_line(self) -> None:
        assert ar._parse_configuration_error_line(_result_line()) == (TASK_ID, 0)
        assert ar._parse_configuration_error_line(_result_line(turns=12)) == (
            TASK_ID,
            12,
        )

    def test_reads_it_through_colour_codes(self) -> None:
        line = f"    \x1b[31m✗\x1b[0m {TASK_ID}: configuration_error (0 turns)"
        assert ar._parse_configuration_error_line(line) == (TASK_ID, 0)

    @pytest.mark.parametrize(
        "line",
        [
            f"    ✓ {TASK_ID}: configuration_error (0 turns)",
            f"    ✗ {TASK_ID}: unrecoverable_stall (5 turns)",
            f"    ✗ {TASK_ID}: configuration_error_x (0 turns)",
            f"INFO: {TASK_ID}: configuration_error (0 turns)",
            f"INFO:x: ✗ {TASK_ID}: configuration_error (0 turns)",
            f"    ✗ {TASK_ID}: CONFIGURATION_ERROR - stopped",
            f"| {TASK_ID} | FAILED | 0 | configuration_error |",
            "",
        ],
    )
    def test_any_other_line_is_none(self, line: str) -> None:
        assert ar._parse_configuration_error_line(line) is None

    def test_one_turn_is_singular(self) -> None:
        line = f"    ✗ {TASK_ID}: configuration_error (1 turn)"
        assert ar._parse_configuration_error_line(line) == (TASK_ID, 1)


class TestTheNotice:
    def _errors(self, n: int, *, same: bool) -> list[dict[str, Any]]:
        return [
            {
                "task_id": f"TASK-N-{i:03d}",
                "turns": 0,
                "message": ("x" * 900) if same else (f"{i}" + "y" * 900),
            }
            for i in range(n)
        ]

    def test_the_whole_passage_is_bounded(self) -> None:
        said = ar._configuration_error_sentence(self._errors(5, same=False))
        assert said.index("\u2026 [cut:") == ar._CONFIGURATION_NOTICE_MAX_CHARS
        assert said.endswith("characters; the build log has the rest]")

    def test_a_short_passage_is_not_cut(self) -> None:
        said = ar._configuration_error_sentence(self._errors(2, same=True))
        assert "[cut:" not in said
        assert said.count("x" * 900) == 1


class TestTheLedgerReader:
    def test_reads_decision_and_error(self, tmp_path: Path) -> None:
        _write_ledger(tmp_path, [_stopped_task(), {"id": OTHER_TASK, "status": "pending"}])
        results = bm.read_task_results(tmp_path, FEATURE_ID)
        assert results[TASK_ID][0] == "configuration_error"
        assert COVERAGE_SENTENCE in str(results[TASK_ID][1])
        assert results[OTHER_TASK] == (None, None)

    @pytest.mark.parametrize(
        "text",
        ["", "not: [valid", "- a list", "tasks: 7", "tasks:\n  - 3\n  - {id: 5}"],
    )
    def test_a_bad_ledger_reads_as_nothing(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / bm.FEATURES_DIR / f"{FEATURE_ID}.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        assert bm.read_task_results(tmp_path, FEATURE_ID) == {}

    def test_no_ledger_reads_as_nothing(self, tmp_path: Path) -> None:
        assert bm.read_task_results(tmp_path, FEATURE_ID) == {}


# ---------------------------------------------------------------------------
# The connected result: the manifest the real runner wrote, read by the real
# producer through the build-state recorder, over a real SQLite ledger.
# ---------------------------------------------------------------------------


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    finally:
        cx.close()


def _queue_a_build(pool: SqliteLifecyclePersistence) -> str:
    now = datetime.now(UTC)
    return pool.record_pending_build(
        BuildQueuedPayload(
            feature_id=FEATURE_ID,
            repo="guardkit/notes-fixture",
            feature_yaml_path=f".guardkit/features/{FEATURE_ID}.yaml",
            triggered_by="cli",
            originating_user="rich",
            correlation_id="corr-cfg-1",
            requested_at=now,
            queued_at=now,
        )
    )


def _queue_rows(pool: SqliteLifecyclePersistence) -> list[sqlite3.Row]:
    return list(pool.connection.execute("SELECT * FROM work_queue").fetchall())


async def _fail_the_build(
    pool: SqliteLifecyclePersistence,
    tmp_path: Path,
    lines: list[str],
    ledger: list[dict[str, Any]],
) -> tuple[str, str]:
    build_id = _queue_a_build(pool)
    repo, worktree = _dirs(tmp_path)
    _write_ledger(worktree, ledger)
    guardkit = _fake_guardkit(tmp_path, lines, exit_code=2)
    result = await _run_node(guardkit, worktree=worktree, repo=repo, build_id=build_id)
    reason = result["async_tasks"][FEATURE_ID]["error_message"]
    await build_build_state_recorder(pool)(
        BuildFailedPayload(
            feature_id=FEATURE_ID,
            build_id=build_id,
            failure_reason=reason,
            failed_stage="autobuild",
            recoverable=False,
        )
    )
    return build_id, reason


class TestNoRepairRowForAConfigurationError:
    @pytest.mark.asyncio
    async def test_the_stopped_build_files_no_repair_and_its_row_says_why(
        self,
        pool: SqliteLifecyclePersistence,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("INFO", logger="forge.pipeline.fix_row_producer"):
            build_id, reason = await _fail_the_build(
                pool, tmp_path, [_result_line()], [_stopped_task()]
            )

        assert _queue_rows(pool) == []
        row = pool.get_build_row(build_id)
        assert str(getattr(row.status, "value", row.status)) == "FAILED"
        said = " ".join(r.getMessage() for r in caplog.records)
        assert build_id in said and TASK_ID in said
        assert "configuration error" in said
        # What a person is shown names GuardKit's fix.
        assert COVERAGE_SENTENCE in reason

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_still_files_its_repair(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path
    ) -> None:
        await _fail_the_build(
            pool,
            tmp_path,
            [_result_line(decision="unrecoverable_stall")],
            [_stopped_task(decision="unrecoverable_stall")],
        )

        rows = _queue_rows(pool)
        assert len(rows) == 1
        assert rows[0]["kind"] == "fix"

    @pytest.mark.asyncio
    async def test_a_line_a_project_printed_never_stops_a_real_repair(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path
    ) -> None:
        """The coach's probe (coach F1). A failed smoke gate copies the
        project's raw output onto the same stream, here with a line shaped
        exactly like GuardKit's for a task GuardKit approved; then the real
        failure. The ledger does not record a configuration error for that
        task, so the real failure is repaired and nobody is told otherwise."""
        lines = [
            "WARNING:guardkit.orchestrator.smoke_gates:Smoke gate failed after "
            "wave 1 (exit=1, expected=0)",
            "stderr:",
            "(empty)",
            "stdout:",
            "---- Captured stdout call ----",
            _result_line(OTHER_TASK),
            "FAILED tests/test_log_parser.py::test_reads_failed_line",
            _result_line(decision="unrecoverable_stall", turns=5),
        ]
        ledger = [
            _stopped_task(decision="unrecoverable_stall", error="stalled"),
            _stopped_task(task_id=OTHER_TASK, decision="approved", error=None),
        ]

        _build_id, reason = await _fail_the_build(pool, tmp_path, lines, ledger)

        assert len(_queue_rows(pool)) == 1
        assert "GuardKit stopped" not in reason
        assert "none was filed" not in reason


class TestTheProducerReadsOnlyAWellFormedRecord:
    """The producer side alone, over hand-written manifests."""

    def _pack(self, receipts: Path, build_id: str, field: Any) -> None:
        pack = receipts / build_id
        pack.mkdir(parents=True)
        manifest: dict[str, Any] = {"build_id": build_id, "feature_id": FEATURE_ID}
        if field is not ...:
            manifest[ar.CONFIGURATION_ERRORS_FIELD] = field
        (pack / ar.FAILURE_MANIFEST_NAME).write_text(json.dumps(manifest))

    @pytest.mark.parametrize(
        ("field", "files_a_row"),
        [
            (..., True),
            ([], True),
            ("TASK-1", True),
            ({"task_id": TASK_ID}, True),
            ([{"message": "no task"}], True),
            ([{"task_id": "  "}], True),
            ([7, None], True),
            ([{"task_id": TASK_ID, "message": None}], False),
            ([{"task_id": TASK_ID, "message": 5}], False),
            ([{"task_id": TASK_ID, "message": "words"}], False),
        ],
    )
    def test_only_a_named_task_stops_the_repair(
        self,
        pool: SqliteLifecyclePersistence,
        receipts: Path,
        field: Any,
        files_a_row: bool,
    ) -> None:
        build_id = _queue_a_build(pool)
        self._pack(receipts, build_id, field)

        filed = maybe_mint_fix_row(
            pool=pool,
            build_id=build_id,
            source=SOURCE_BUILD_FAILED,
            detail="guardkit autobuild exit=2",
            receipts_root=receipts,
        )

        assert (filed is not None) is files_a_row
        assert len(_queue_rows(pool)) == (1 if files_a_row else 0)

    def test_a_merge_report_is_filed_even_beside_a_recorded_stop(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        """The rule is for a failed build only: a merge whose checks went red
        is filed whatever an old pack under the same build says."""
        from forge.pipeline.fix_row_producer import SOURCE_MERGE_REPORT

        build_id = _queue_a_build(pool)
        self._pack(receipts, build_id, [{"task_id": TASK_ID, "message": "words"}])

        filed = maybe_mint_fix_row(
            pool=pool,
            build_id=build_id,
            source=SOURCE_MERGE_REPORT,
            detail="the live check went red",
            receipts_root=receipts,
        )

        assert filed is not None
        assert len(_queue_rows(pool)) == 1
