"""A merge that landed but whose checks never finished must not read as refused.

Driven end to end on real code paths: a REAL git repository in a temporary
directory, the REAL deploy sidecar handler on an ephemeral loopback port, a
REAL executable named ``guardkit`` on the sidecar's path, the sidecar-backed
run, and the real merge executor. Nothing here is mocked; the only stand-in is
guardkit itself, and each stand-in behaves the way the real command behaves on
the path under test:

* one merges the branch for real and then dies without printing a report (a
  command that was killed after git had already done the work);
* one dies the same way having merged nothing (a command that was killed
  before it got anywhere);
* one prints the refusal report the real command prints, with its own
  ``refusal_reason`` sentence;
* one prints a merged report and echoes the arguments it was given, so the
  time limit forge sets on the checks can be read off the real command line.

The first case is the one that went wrong at small scale: the branch was on
main and the merge report said it had been refused.
"""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.guardkit.run_via_sidecar import build_sidecar_guardkit_run
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import GUARDKIT_PATH_ENV, build_server
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_executor import (
    MergeExecutorDeps,
    execute_merge_deploy,
)

REPO_KEY = "appmilla/api_test"
BUILD_ID = "build-FEAT-MX9-20260906"
FEATURE_ID = "FEAT-MX9"
CORRELATION = "corr-landed-1"

#: The sentence the sidecar itself adds when it stops a command that overran.
KILLED_SENTENCE = (
    "the merge command was stopped after 2 seconds and everything it had "
    "started was stopped with it"
)


# ---------------------------------------------------------------------------
# A real git repository with a branch waiting to be merged
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main with one commit, and ``autobuild/FEAT-MX9`` one commit ahead."""
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", "feature.txt")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    return root


def _main_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "main")


def _write_guardkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> Path:
    binary = tmp_path / "bin" / "guardkit"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(binary))
    return binary


#: Merge the branch for real, print nothing, and die the way a killed command
#: dies: exit 124 with the stopper's sentence on stderr and no report at all.
MERGES_THEN_DIES = f"""
import subprocess, sys
argv = sys.argv[1:]
feature = argv[2]
git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
       "-c", "commit.gpgsign=false"]
subprocess.run(git + ["merge", "--no-ff", "-m", "merge " + feature,
                      "autobuild/" + feature], check=True)
sys.stderr.write({KILLED_SENTENCE!r})
sys.exit(124)
"""

#: Killed before it got anywhere: nothing merged, no report.
DIES_WITHOUT_MERGING = f"""
import sys
sys.stderr.write({KILLED_SENTENCE!r})
sys.exit(124)
"""

#: The refusal report the real command prints, with its own sentence.
REFUSES_WITH_A_SENTENCE = """
import json, sys
print(json.dumps({
    "outcome": "refused",
    "refusal_reason": (
        "the working tree has uncommitted changes; commit or stash them "
        "before merging"
    ),
    "branch": "autobuild/" + sys.argv[3],
}))
sys.exit(2)
"""

#: A clean merge that echoes what it was actually asked to do.
MERGES_AND_ECHOES = """
import json, os, sys
argv = sys.argv[1:]
print(json.dumps({
    "outcome": "merged", "post_sha": "e" * 40, "verify_ok": True,
    "verify_status": "passed", "charged_failures": [],
    "checks_passed": 8, "checks_total": 8,
    "argv": argv, "cwd": os.getcwd(),
}))
sys.exit(0)
"""


# ---------------------------------------------------------------------------
# The real sidecar, the real adapter, the real executor
# ---------------------------------------------------------------------------


@pytest.fixture
def config(repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: str(repo)}},
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True},
        }
    )


@pytest.fixture
def sidecar(config: ForgeConfig):
    server = build_server(port=0, config_loader=lambda: config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


@pytest.fixture(autouse=True)
def _receipts_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


class _Publisher:
    def __init__(self) -> None:
        self.reports: list[Any] = []

    async def publish_stage_complete(self, payload: Any) -> None:
        self.reports.append(payload)


class _Deploy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            outcome="complete", verdict="8/8", deploy_record_ref="docs/state/x.md"
        )


def _ensure_build(pool: SqliteLifecyclePersistence) -> None:
    pool.connection.execute(
        "INSERT OR IGNORE INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode) VALUES (?, ?, ?, ?, 'f.yaml', 'COMPLETE', 'cli', ?, "
        "'2026-09-06T00:00:00Z', 'mode-a')",
        (
            BUILD_ID,
            FEATURE_ID,
            REPO_KEY,
            f"autobuild/{FEATURE_ID}",
            CORRELATION,
        ),
    )
    pool.connection.commit()


async def _press_merge(
    *,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar_url: str,
    repo: Path,
    expect_main_sha: str,
) -> tuple[Any, _Publisher, _Deploy]:
    publisher = _Publisher()
    deploy = _Deploy()
    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=publisher,
        guardkit_run=build_sidecar_guardkit_run(
            base_url=sidecar_url, repo_paths={REPO_KEY: str(repo)}
        ),
        deploy_dispatcher=deploy,
        clock=lambda: datetime.now(timezone.utc),
    )
    _ensure_build(pool)
    outcome = await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=REPO_KEY,
        repo_root=repo,
        expect_main_sha=expect_main_sha,
        correlation_id=CORRELATION,
        decided_by="rich",
    )
    return outcome, publisher, deploy


def _repair_rows(pool: SqliteLifecyclePersistence) -> list[str]:
    rows = pool.connection.execute(
        "SELECT sentence FROM work_queue WHERE kind = 'fix' ORDER BY id"
    ).fetchall()
    return [str(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# The merge landed and the checks never finished
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_merge_that_landed_before_the_command_died_is_not_called_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar: str,
) -> None:
    _write_guardkit(tmp_path, monkeypatch, MERGES_THEN_DIES)
    before = _main_sha(repo)

    outcome, publisher, deploy = await _press_merge(
        config=config,
        pool=pool,
        sidecar_url=sidecar,
        repo=repo,
        expect_main_sha=before,
    )

    # git really did merge the branch: main moved and holds the branch tip.
    after = _main_sha(repo)
    assert after != before
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"autobuild/{FEATURE_ID}", after],
            cwd=str(repo),
        ).returncode
        == 0
    )

    assert outcome.result == "merged-verify-failed"
    assert outcome.status == "FAILED"
    assert outcome.failed_step == "verify"
    assert outcome.merged_sha == after
    assert outcome.verify_status == "unverified"
    assert f"{FEATURE_ID} merged ({after[:10]})" in outcome.detail
    assert "could not finish" in outcome.detail
    assert KILLED_SENTENCE in outcome.detail
    assert "The deploy was not dispatched." in outcome.detail

    # Nothing was deployed, one report went out, and no repair was filed:
    # what is broken is the check, and no amount of building mends that.
    assert deploy.calls == []
    assert len(publisher.reports) == 1
    assert publisher.reports[0].result == "merged-verify-failed"
    assert publisher.reports[0].verify_status == "unverified"
    assert _repair_rows(pool) == []


@pytest.mark.asyncio
async def test_a_command_that_died_having_merged_nothing_is_still_a_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar: str,
) -> None:
    """Main did not move, so nothing landed and 'refused' is the truth."""
    _write_guardkit(tmp_path, monkeypatch, DIES_WITHOUT_MERGING)
    before = _main_sha(repo)

    outcome, publisher, deploy = await _press_merge(
        config=config,
        pool=pool,
        sidecar_url=sidecar,
        repo=repo,
        expect_main_sha=before,
    )

    assert _main_sha(repo) == before
    assert outcome.result == "merge-refused"
    assert outcome.failed_step == "merge"
    assert outcome.merged_sha is None
    assert KILLED_SENTENCE in outcome.detail
    assert deploy.calls == []
    assert _repair_rows(pool) == []


@pytest.mark.asyncio
async def test_main_moving_on_its_own_is_not_taken_for_this_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar: str,
) -> None:
    """Someone else's commit on main is not this feature's merge.

    Main moves, but it does not contain the branch tip, so the merge is still
    reported as refused — the probe asks both questions, not just the first.
    """
    _write_guardkit(tmp_path, monkeypatch, DIES_WITHOUT_MERGING)
    before = _main_sha(repo)
    (repo / "someone-else.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "someone-else.txt")
    _git(repo, "commit", "-q", "-m", "someone else's commit")
    assert _main_sha(repo) != before

    outcome, _publisher, deploy = await _press_merge(
        config=config,
        pool=pool,
        sidecar_url=sidecar,
        repo=repo,
        expect_main_sha=before,
    )

    assert outcome.result == "merge-refused"
    assert outcome.merged_sha is None
    assert deploy.calls == []


# ---------------------------------------------------------------------------
# A refusal speaks guardkit's own sentence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_report_is_repeated_word_for_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar: str,
) -> None:
    _write_guardkit(tmp_path, monkeypatch, REFUSES_WITH_A_SENTENCE)

    outcome, publisher, deploy = await _press_merge(
        config=config,
        pool=pool,
        sidecar_url=sidecar,
        repo=repo,
        expect_main_sha=_main_sha(repo),
    )

    assert outcome.result == "merge-refused"
    assert outcome.detail == (
        "the working tree has uncommitted changes; commit or stash them "
        "before merging"
    )
    # No wrapper words, no slice of JSON.
    assert "status=" not in outcome.detail
    assert "{" not in outcome.detail
    assert deploy.calls == []
    assert publisher.reports[0].detail == outcome.detail


# ---------------------------------------------------------------------------
# The time limit on the checks reaches the real command line
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_checks_time_limit_reaches_the_command_on_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    pool: SqliteLifecyclePersistence,
    _receipts_env: Path,
) -> None:
    """Forge's limit for one check run must survive the trip to the host.

    The sidecar builds the command itself, so the limit travels as its own
    field in the request; if it were dropped, the checks would silently fall
    back to guardkit's own default and forge's outer wall would be sized for
    a limit nobody was keeping to.
    """
    _write_guardkit(tmp_path, monkeypatch, MERGES_AND_ECHOES)
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: str(repo)}},
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True, "verify_timeout_seconds": 300},
        }
    )
    server = build_server(port=0, config_loader=lambda: config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        outcome, _publisher, deploy = await _press_merge(
            config=config,
            pool=pool,
            sidecar_url=f"http://{host}:{port}",
            repo=repo,
            expect_main_sha=_main_sha(repo),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert outcome.result == "merged-and-running"
    assert deploy.calls  # a clean merge deploys

    receipt = json.loads(
        (_receipts_env / f"merge-{BUILD_ID}" / "merge_deploy_merge.json").read_text(
            encoding="utf-8"
        )
    )
    argv = receipt["report"]["argv"]
    assert argv[:3] == ["autobuild", "merge", FEATURE_ID]
    assert "--verify-timeout" in argv
    assert argv[argv.index("--verify-timeout") + 1] == "300"
    assert receipt["report"]["cwd"] == str(repo.resolve())


#: The real command's conflict report: no sentence of its own, only the files.
CONFLICTS_ON_ONE_FILE = """
import json, sys
print(json.dumps({
    "outcome": "conflict",
    "refusal_reason": None,
    "conflict_files": ["src/users/service.py"],
    "branch": "autobuild/" + sys.argv[3],
    "pre_sha": None, "post_sha": None,
}))
sys.exit(3)
"""

#: Merges, then dies after a log line and one real sentence on stderr.
MERGES_THEN_DIES_TALKING = f"""
import subprocess, sys
argv = sys.argv[1:]
feature = argv[2]
git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
       "-c", "commit.gpgsign=false"]
subprocess.run(git + ["merge", "--no-ff", "-m", "merge " + feature,
                      "autobuild/" + feature], check=True)
sys.stderr.write("INFO resolve_verify_command: repository toolchain declaration: "
                 + "/some/very/long/venv/path/bin/python -m pytest -q tests/ (cwd=/x, timeout=600s)\\n")
sys.stderr.write({KILLED_SENTENCE!r} + "\\n")
sys.exit(124)
"""


@pytest.mark.asyncio
async def test_a_conflict_is_said_in_words_naming_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar: str,
) -> None:
    _write_guardkit(tmp_path, monkeypatch, CONFLICTS_ON_ONE_FILE)

    outcome, publisher, deploy = await _press_merge(
        config=config,
        pool=pool,
        sidecar_url=sidecar,
        repo=repo,
        expect_main_sha=_main_sha(repo),
    )

    assert outcome.result == "merge-refused"
    assert outcome.detail == (
        "the merge stopped on a conflict in src/users/service.py; nothing was "
        "merged and the branch is kept"
    )
    assert "{" not in outcome.detail and "status=" not in outcome.detail
    assert deploy.calls == []


@pytest.mark.asyncio
async def test_a_killed_command_is_quoted_by_its_last_sentence_not_a_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar: str,
) -> None:
    _write_guardkit(tmp_path, monkeypatch, MERGES_THEN_DIES_TALKING)

    outcome, publisher, deploy = await _press_merge(
        config=config,
        pool=pool,
        sidecar_url=sidecar,
        repo=repo,
        expect_main_sha=_main_sha(repo),
    )

    assert outcome.result == "merged-verify-failed"
    assert outcome.verify_status == "unverified"
    assert f"could not finish: {KILLED_SENTENCE}." in outcome.detail
    # The log line before the sentence never reaches the card, whole or cut.
    assert "resolve_verify_command" not in outcome.detail
    assert "laration" not in outcome.detail


def _merge_step_rows(pool: SqliteLifecyclePersistence) -> list[str]:
    return [
        str(s.status)
        for s in pool.read_stages(BUILD_ID)
        if s.target_identifier == "merge_deploy_merge"
    ]


@pytest.mark.asyncio
async def test_a_refusal_that_merged_nothing_gives_the_press_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar: str,
) -> None:
    """2026-09-06: Rich's press refused over a dirty tree and the build could
    never be pressed again. A refusal that landed nothing releases the merge
    step, so the next press runs."""
    _write_guardkit(tmp_path, monkeypatch, REFUSES_WITH_A_SENTENCE)
    before = _main_sha(repo)
    first, _, _ = await _press_merge(
        config=config, pool=pool, sidecar_url=sidecar, repo=repo, expect_main_sha=before
    )
    assert first.result == "merge-refused"
    assert _merge_step_rows(pool) == ["GATED", "SKIPPED"]

    _write_guardkit(tmp_path, monkeypatch, MERGES_AND_ECHOES)
    second, publisher, deploy = await _press_merge(
        config=config, pool=pool, sidecar_url=sidecar, repo=repo, expect_main_sha=before
    )
    assert second.result != "merge-refused", second.detail
    assert "already on record" not in (second.detail or "")
    assert _merge_step_rows(pool)[:3] == ["GATED", "SKIPPED", "GATED"]


@pytest.mark.asyncio
async def test_a_merge_that_landed_is_never_run_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    sidecar: str,
) -> None:
    _write_guardkit(tmp_path, monkeypatch, MERGES_THEN_DIES)
    before = _main_sha(repo)
    first, _, _ = await _press_merge(
        config=config, pool=pool, sidecar_url=sidecar, repo=repo, expect_main_sha=before
    )
    assert first.result == "merged-verify-failed"
    assert _merge_step_rows(pool) == ["GATED"]

    _write_guardkit(tmp_path, monkeypatch, MERGES_AND_ECHOES)
    second, _, _ = await _press_merge(
        config=config, pool=pool, sidecar_url=sidecar, repo=repo, expect_main_sha=_main_sha(repo)
    )
    assert second.result == "merge-refused"
    assert "already on record" in second.detail

