"""GuardKit's early-stop sentence reaches the failed build's reason (18 Oct 2026).

Design: ``ai-transition/docs/designs/guardkit-early-stop-contradicted-check-2026-10-18.md``
section 3, with review round 2's R3 rule: Forge takes the sentence ONLY from the
stdout of the GuardKit process this run started — one
``GUARDKIT_STOPPED_BECAUSE {...}`` line — and never reads a file for it.

The integration tests start a REAL child process (a fake GuardKit, a tiny shell
script) through the runner's own ``asyncio.create_subprocess_exec`` and pipe,
against an empty receipts root, and read the failure manifest and the failed
snapshot the real path writes. Controls leave the reason byte-identical:
no line, a malformed line, exit 0 with the line, and an old ``stopped_because``
record on disk with a current export that has none.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from forge.subagents import autobuild_runner as ar
from forge.subagents import build_monitor as bm

FEATURE_ID = "FEAT-STOP"
BUILD_ID = "build-FEAT-STOP-1"
BRANCH = "lane/early-stop"
SENTENCE = (
    "The plan contradicts the project's own check: the project's declared "
    'feature check failed while AC-003 ("a count of `1` reads the singular '
    '`1 note`") was met, and passes on the coder\'s version that leaves it out '
    "(kept in the build's worktree, commit `42d3256c0000`)."
)


def _line(sentence: str = SENTENCE, feature_id: str = FEATURE_ID) -> str:
    return "GUARDKIT_STOPPED_BECAUSE " + json.dumps(
        {"feature_id": feature_id, "sentence": sentence}, ensure_ascii=True
    )


def _fake_guardkit(tmp_path: Path, lines: list[str], exit_code: int) -> Path:
    """A real executable standing in for GuardKit: prints, then exits."""
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


def _launch_state() -> dict[str, Any]:
    payload = {
        "build_id": BUILD_ID,
        "feature_id": FEATURE_ID,
        "repo": "guardkit/notes-fixture",
        "branch": BRANCH,
        "correlation_id": "corr-stop-1",
    }
    description = (
        "RUN_AUTOBUILD subagent=autobuild_runner "
        f"payload={json.dumps(payload, sort_keys=True)}"
    )
    return {"messages": [HumanMessage(content=description)]}


async def _run_node(guardkit: Path, *, worktree: Path, repo: Path) -> dict[str, Any]:
    """Drive ``_node_running_wave`` with a REAL child process.

    Only the repository plumbing around the launch is doubled (no git, no
    worktree creation); the child, its pipe, the drain loop, the export, the
    manifest and the snapshot are all the real code.
    """

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
        return await ar._node_running_wave(_launch_state())  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fresh_receipts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An EMPTY receipts root inside tmp, and no build monitor."""
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


def _manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads(
        (tmp_path / "receipts" / BUILD_ID / ar.FAILURE_MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )


def _today(worktree: Path, exit_code: int = 2) -> str:
    return ar._with_worktree_forensics(f"guardkit autobuild exit={exit_code}", worktree)


class TestTheSentenceReachesTheReason:
    @pytest.mark.asyncio
    async def test_the_line_and_exit_2_enrich_manifest_and_snapshot(
        self, tmp_path: Path, _fresh_receipts_root: Path
    ) -> None:
        assert not _fresh_receipts_root.exists(), "a fresh, empty receipts root"
        repo, worktree = _dirs(tmp_path)
        guardkit = _fake_guardkit(
            tmp_path, ["INFO: the repair round stopped", _line()], exit_code=2
        )

        result = await _run_node(guardkit, worktree=worktree, repo=repo)

        expected = f"guardkit autobuild exit=2: {SENTENCE}"
        assert _manifest(tmp_path)["reason"] == expected
        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        assert snap["error_message"] == ar._with_worktree_forensics(expected, worktree)
        # The prefix is byte for byte today's.
        assert snap["error_message"].startswith("guardkit autobuild exit=2: The plan")
        # The line itself is kept in the build's stdout log like any other.
        log = (tmp_path / "receipts" / BUILD_ID / ar.STDOUT_LOG_NAME).read_text()
        assert "GUARDKIT_STOPPED_BECAUSE" in log


class TestTheReasonIsUnchangedOtherwise:
    @pytest.mark.asyncio
    async def test_no_line(self, tmp_path: Path) -> None:
        repo, worktree = _dirs(tmp_path)
        guardkit = _fake_guardkit(tmp_path, ["INFO: failed"], exit_code=2)
        result = await _run_node(guardkit, worktree=worktree, repo=repo)
        assert _manifest(tmp_path)["reason"] == "guardkit autobuild exit=2"
        assert result["async_tasks"][FEATURE_ID]["error_message"] == _today(worktree)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_line",
        [
            "GUARDKIT_STOPPED_BECAUSE not json",
            'GUARDKIT_STOPPED_BECAUSE {"feature_id": "FEAT-STOP"}',
            'GUARDKIT_STOPPED_BECAUSE {"feature_id": "FEAT-STOP", "sentence": ""}',
            'GUARDKIT_STOPPED_BECAUSE {"feature_id": "FEAT-STOP", "sentence": 7}',
            'GUARDKIT_STOPPED_BECAUSE ["a list"]',
            'INFO: GUARDKIT_STOPPED_BECAUSE {"sentence": "not at the start"}',
            _line(feature_id="FEAT-OTHER"),
        ],
    )
    async def test_a_malformed_line(self, tmp_path: Path, bad_line: str) -> None:
        repo, worktree = _dirs(tmp_path)
        guardkit = _fake_guardkit(tmp_path, [bad_line], exit_code=2)
        result = await _run_node(guardkit, worktree=worktree, repo=repo)
        assert _manifest(tmp_path)["reason"] == "guardkit autobuild exit=2"
        assert result["async_tasks"][FEATURE_ID]["error_message"] == _today(worktree)

    @pytest.mark.asyncio
    async def test_exit_0_with_the_line(self, tmp_path: Path) -> None:
        repo, worktree = _dirs(tmp_path)
        guardkit = _fake_guardkit(tmp_path, [_line()], exit_code=0)
        result = await _run_node(guardkit, worktree=worktree, repo=repo)
        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] != "failed"
        assert SENTENCE not in json.dumps(snap)
        assert not (tmp_path / "receipts" / BUILD_ID / ar.FAILURE_MANIFEST_NAME).exists()

    @pytest.mark.asyncio
    async def test_an_old_record_on_disk_is_never_used(
        self, tmp_path: Path, _fresh_receipts_root: Path
    ) -> None:
        """Codex's R3 case: an old ``stopped_because`` record sits on disk, in
        the receipts pack an earlier run of this build left AND in the kept
        worktree's own record folder (so this run's export carries it), while
        this run's GuardKit printed no line. No file is read for the sentence,
        so the reason is today's."""
        repo, worktree = _dirs(tmp_path)
        old_pack = _fresh_receipts_root / BUILD_ID / ".guardkit" / "autobuild-private"
        old_pack.mkdir(parents=True)
        old_record = {"status": "failed", "reason": SENTENCE, "stopped_because": SENTENCE}
        (old_pack / "feature_check.json").write_text(json.dumps(old_record))
        # The kept worktree still holds the earlier run's record, field and
        # all, so this run's own export carries it too (a resumed build in a
        # kept worktree). This run's GuardKit printed no line.
        (worktree / ".guardkit" / "autobuild-private" / "feature_check.json").write_text(
            json.dumps(old_record)
        )
        guardkit = _fake_guardkit(tmp_path, ["INFO: failed"], exit_code=2)

        result = await _run_node(guardkit, worktree=worktree, repo=repo)

        assert _manifest(tmp_path)["reason"] == "guardkit autobuild exit=2"
        assert result["async_tasks"][FEATURE_ID]["error_message"] == _today(worktree)
        assert SENTENCE not in json.dumps(result["async_tasks"][FEATURE_ID])
        # The stale record did travel in this run's export: only the reason
        # ignores it.
        exported = (
            _fresh_receipts_root / BUILD_ID / ".guardkit" / "autobuild-private"
            / "feature_check.json"
        )
        assert json.loads(exported.read_text())["stopped_because"] == SENTENCE


class TestTheLineReader:
    def test_reads_the_sentence(self) -> None:
        assert ar._parse_stopped_because_line(_line(), FEATURE_ID) == SENTENCE

    def test_collapses_whitespace_and_escaped_newlines(self) -> None:
        line = _line("first part\nsecond   part\t end")
        assert ar._parse_stopped_because_line(line, FEATURE_ID) == (
            "first part second part end"
        )

    def test_bounds_the_sentence(self) -> None:
        parsed = ar._parse_stopped_because_line(_line("x" * 5000), FEATURE_ID)
        assert parsed is not None and len(parsed) == ar._STOPPED_BECAUSE_MAX_CHARS

    def test_a_line_without_a_feature_id_is_accepted(self) -> None:
        line = 'GUARDKIT_STOPPED_BECAUSE {"sentence": "why"}'
        assert ar._parse_stopped_because_line(line, FEATURE_ID) == "why"

    def test_another_line_is_none(self) -> None:
        assert ar._parse_stopped_because_line("Completed turn 1: feedback - x") is None
