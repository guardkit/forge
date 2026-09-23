"""The merge word's command and a fix journey's legs say whose work they are.

Both go through a door to a helper inside the repository's sandbox, and that
helper reads the PROJECT'S OWN declaration files at a commit before it launches
anything. Until 23 September 2026 both doors told it the same thing:
``by_hand: true`` — the label a person running a command by hand wears. Two
things were wrong with that, and this file holds both shut.

*It is the shape the rule forbids.* A request the factory made, wearing the
manual label, is a factory request that gets manual handling. The merge word
knows which build it is pressing; a journey's leg belongs to a build whose row
the runner is already chosen from. Neither has any business claiming to be
somebody at a keyboard.

*And the reading it got was the wrong one.* "By hand" means "read the
declarations at the committed HEAD of whatever copy you have" — so a build
could widen its own environment door by COMMITTING a line and then asking for
a merge or a leg. Stamped with the build and the commit the coordinator's own
ledger records that build as starting from, the helper reads them where the
RECORD says instead, and checks that pair with the coordinator before it reads
a line.

What is driven here, and by what:

* the press itself (``execute_merge_deploy``) onto a recording helper through
  the real merge door, so the stamp is read off the body that actually went on
  the wire;
* the real chooser (``make_conductor_guardkit_run_chooser``) for a leg, which
  is where a leg's runner is bound to its build's row, and then the real
  conductor dispatcher on top of it — a dispatch cannot drop what the runner
  itself carries;
* the REAL helper's own route for both doors, against a stand-in coordinator,
  so "it binds to the record" is the helper's behaviour and not this file's
  opinion: a matching commit is served, a mismatching one is refused;
* a request from each door with the stamp deleted, which the helper refuses
  rather than serving quietly at its own HEAD;
* and the planning door, whose ``by_hand`` is honest — planning runs before
  there is a build to name — still served.

Nothing real is contacted. Every server here is a child of this process on
127.0.0.1 on a port the kernel picks; every repository is a throwaway git
repository under the test's own temporary directory.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import make_conductor_guardkit_run_chooser
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import COORDINATOR_OWNER_ENV, build_server
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.dispatchers.conductor_subprocess import (
    make_conductor_subprocess_dispatcher,
)
from forge.pipeline.merge_executor import MergeExecutorDeps, execute_merge_deploy
from forge.pipeline.stage_taxonomy import StageClass

#: A repository, a build and a feature that exist nowhere but here.
REPO = "acme/widget-shop"
FEATURE_ID = "FEAT-7F21"
BUILD_ID = "build-FEAT-7F21-20260923"
TASK_ID = "TASK-7F21"
CORRELATION = "corr-7f21"

#: The name this project declares its builds need, and the memory the work
#: belongs to. Both are what makes a request ASK for the project's own
#: declarations to be read, which is what the stamp binds.
DECLARED_SETTING = "SOME_TOOL_CACHE"
THE_MEMORY = "widget_shop"


# ---------------------------------------------------------------------------
# A throwaway repository
# ---------------------------------------------------------------------------


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(where),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real git repository with a feature branch and a bare "remote".

    The merge word joins onto the branch of the remote the work was recorded
    against, so a press driven against this needs one; it is a bare repository
    beside it — real git, nobody's account.

    Its own declaration file names ``SOME_TOOL_CACHE``, and it is COMMITTED,
    because a declaration is a committed line: that is the whole point of
    reading it at a commit rather than off the working copy.
    """
    root = tmp_path / "widget-shop"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    declaration = root / ".guardkit" / "config.yaml"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text(
        f"launch:\n  settings: [{DECLARED_SETTING}]\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the project as it is")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}", "main")
    (root / f"{FEATURE_ID}.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"the feature {FEATURE_ID}")
    _git(root, "checkout", "-q", "main")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", "-q", str(bare)],
        check=True,
        capture_output=True,
    )
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")
    return root


@pytest.fixture
def start_commit(project: Path) -> str:
    """The commit the record says this build starts from — main's own tip."""
    return _git(project, "rev-parse", "main")


# ---------------------------------------------------------------------------
# A helper that records, and a coordinator that remembers
# ---------------------------------------------------------------------------


class _ARecordingHelper:
    """A stand-in for the helper in the sandbox: it records and answers.

    Like the deploy stage's own recorder, it REFUSES a request that carries
    neither the build nor the commit, in the shape the real helper refuses
    one — an HTTP 400 with one plain sentence. That way a stamp dropped
    anywhere between the caller and the wire comes back as a failed merge or
    a failed leg, not as a quietly weaker request.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        recorder = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's spelling
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:  # noqa: BLE001 — a stand-in, never a crash
                    body = {}
                body["_route"] = self.path
                recorder.requests.append(body)
                if not str(body.get("build") or "").strip() or not str(
                    body.get("declared_at") or ""
                ).strip():
                    self._answer(
                        400,
                        {
                            "error": (
                                "this request names no build and no commit, so "
                                "there is nothing to read this project's own "
                                "declarations at and nothing was run"
                            )
                        },
                    )
                    return
                self._answer(
                    200,
                    {
                        "exit_code": 0,
                        "stdout": json.dumps(
                            {"outcome": "merged", "post_sha": "e" * 40}
                        ),
                        "stderr_tail": "",
                    },
                )

            def _answer(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:  # noqa: ANN401
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def only(self, route: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r.get("_route") == route]


@pytest.fixture
def helper() -> Any:
    stand_in = _ARecordingHelper()
    try:
        yield stand_in
    finally:
        stand_in.close()


class _TheCoordinatorsAnswer(BaseHTTPRequestHandler):
    """The coordinator's read-only answer, out of a mapping written here."""

    records: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — the base class's spelling
        from urllib.parse import parse_qs, urlparse

        asked = parse_qs(urlparse(self.path).query)
        build = (asked.get("build") or [""])[0]
        answer: dict[str, Any] = {}
        if build in self.records:
            answer = {"build": build, "start_commit": self.records[build]}
        payload = json.dumps(answer).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # noqa: ANN401
        return


@contextlib.contextmanager
def _a_coordinator_that_recorded(
    records: dict[str, str], monkeypatch: pytest.MonkeyPatch
):
    handler = type("_Answer", (_TheCoordinatorsAnswer,), {"records": dict(records)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    monkeypatch.setenv(
        COORDINATOR_OWNER_ENV, f"http://{host}:{port}/what-did-you-record"
    )
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# The press, driven onto the recorder through the real merge door
# ---------------------------------------------------------------------------


def _config(project: Path, *, sandbox_url: str | None = None) -> ForgeConfig:
    raw: dict[str, Any] = {
        "permissions": {"filesystem": {"allowlist": [str(project.parent)]}},
        "planning": {"target_repo_paths": {REPO: str(project)}},
        "approval": {"expected_approver": "rich"},
        "merge_executor": {"enabled": True},
    }
    if sandbox_url is not None:
        raw["planning"]["sandboxes"] = {
            REPO: {
                "name": "widget-shop-sbx",
                "sidecar_url": sandbox_url,
                "runner_url": sandbox_url,
            }
        }
    return ForgeConfig.model_validate(raw)


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


def _a_build_row(
    pool: SqliteLifecyclePersistence, *, start_commit: str, worktree: Path
) -> None:
    """The ledger row the stamp is read off — including where it starts from."""
    pool.connection.execute(
        "INSERT OR IGNORE INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode, start_commit, target_branch, task_id, worktree_path, "
        "memory_project) VALUES (?, ?, ?, ?, 'f.yaml', 'COMPLETE', 'cli', ?, "
        "'2026-09-23T00:00:00Z', 'mode-c', ?, 'main', ?, ?, ?)",
        (
            BUILD_ID,
            FEATURE_ID,
            REPO,
            f"autobuild/{FEATURE_ID}",
            CORRELATION,
            start_commit,
            TASK_ID,
            str(worktree),
            THE_MEMORY,
        ),
    )
    pool.connection.commit()


async def _press(
    *, config: ForgeConfig, pool: SqliteLifecyclePersistence, project: Path, url: str
) -> Any:
    """One merge press, through the REAL door, onto whatever ``url`` serves."""
    from forge.adapters.guardkit.run_via_sidecar import build_sidecar_guardkit_run

    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=_Quiet(),
        guardkit_run=build_sidecar_guardkit_run(
            base_url=url, repo_paths={REPO: str(project)}
        ),
        deploy_dispatcher=AsyncMock(return_value=None),
    )
    return await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=REPO,
        repo_root=project,
        expect_main_sha=_git(project, "rev-parse", "main"),
        correlation_id=CORRELATION,
        decided_by="rich",
        dry_run=False,
    )


class _Quiet:
    """Every pipeline event, dropped. This file is about what goes on the wire."""

    def __getattr__(self, _name: str) -> Any:
        async def _publish(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _publish


@pytest.fixture(autouse=True)
def _receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


class TestTheMergeWordStampsItsOwnCommand:
    @pytest.mark.asyncio
    async def test_the_press_sends_its_build_and_the_recorded_commit(
        self, project, start_commit, pool, helper, tmp_path
    ) -> None:
        _a_build_row(pool, start_commit=start_commit, worktree=tmp_path / "wt")
        await _press(
            config=_config(project), pool=pool, project=project, url=helper.url
        )
        sent = helper.only("/guardkit-merge")
        assert sent, "the press sent no merge request at all"
        assert sent[0]["build"] == BUILD_ID
        assert sent[0]["declared_at"] == start_commit

    @pytest.mark.asyncio
    async def test_the_press_never_says_it_is_somebody_working_by_hand(
        self, project, start_commit, pool, helper, tmp_path
    ) -> None:
        """The label belongs to a person at a keyboard, and this is not one."""
        _a_build_row(pool, start_commit=start_commit, worktree=tmp_path / "wt")
        await _press(
            config=_config(project), pool=pool, project=project, url=helper.url
        )
        for request in helper.only("/guardkit-merge"):
            assert "by_hand" not in request


# ---------------------------------------------------------------------------
# A journey's leg, driven through the real chooser and the real dispatcher
# ---------------------------------------------------------------------------


def _a_journey_worktree(project: Path) -> Path:
    """Where a fix journey's legs run: a folder INSIDE the repository.

    The door works out which repository a leg belongs to by containment, so a
    worktree somewhere else is refused before anything is sent — which would
    make this file prove nothing.
    """
    worktree = project / ".forge" / "worktrees" / BUILD_ID
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


@dataclass
class _Row:
    """The ledger row the chooser and the adapter read, as they read it."""

    build_id: str
    repo: str
    start_commit: str | None
    worktree_path: str
    task_id: str | None = TASK_ID
    correlation_id: str = CORRELATION
    feature_yaml_path: str | None = None
    feature_id: str = FEATURE_ID
    branch: str = f"fix/{FEATURE_ID}"
    memory_project: str | None = THE_MEMORY
    launch_settings: tuple[str, ...] = (DECLARED_SETTING,)


class _APoolOfOneRow:
    def __init__(self, row: _Row) -> None:
        self._row = row

    def get_build_row(self, _build_id: str) -> _Row:
        return self._row


async def _a_leg(
    *, config: ForgeConfig, row: _Row, stage: StageClass = StageClass.TASK_REVIEW
) -> Any:
    """One fix-journey leg, chooser and dispatcher both the real ones."""
    pool = _APoolOfOneRow(row)
    runner_for = make_conductor_guardkit_run_chooser(
        pool=pool,
        config=config,
        in_container_run=_never_in_the_container,
    )
    dispatcher = make_conductor_subprocess_dispatcher(
        build_row_reader=pool.get_build_row,
        read_allowlist=[Path(row.worktree_path)],
        worktree_allowlist=_AnythingInTheWorktree(Path(row.worktree_path)),
        forward_context_builder=_NoForwardContext(),
        stage_log_writer=_NoStageLog(),
        subprocess_runner=runner_for(row.build_id),
        correlation_id_minter=lambda **_kw: "corr-leg",
    )
    return await dispatcher(stage=stage, build_id=row.build_id, feature_id=None)


async def _never_in_the_container(**_kwargs: Any) -> Any:  # pragma: no cover
    raise AssertionError(
        "this repository has a sandbox, so its legs must not run in the "
        "container"
    )


class _AnythingInTheWorktree:
    def __init__(self, root: Path) -> None:
        self._root = root

    def is_allowed(self, path: Any) -> bool:
        return True

    def __contains__(self, path: Any) -> bool:
        return True


class _NoForwardContext:
    def build(self, *_args: Any, **_kwargs: Any) -> Any:
        return []

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        return []


class _NoStageLog:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(self, **row: Any) -> None:
        self.rows.append(row)

    def for_fix_task(self, _subject: Any) -> "_NoStageLog":
        return self


class TestAJourneysLegStampsItsOwnRequest:
    @pytest.mark.asyncio
    async def test_the_leg_carries_its_build_and_the_rows_recorded_commit(
        self, project, start_commit, helper, tmp_path
    ) -> None:
        worktree = _a_journey_worktree(project)
        outcome = await _a_leg(
            config=_config(project, sandbox_url=helper.url),
            row=_Row(
                build_id=BUILD_ID,
                repo=REPO,
                start_commit=start_commit,
                worktree_path=str(worktree),
            ),
        )
        sent = helper.only("/guardkit-leg")
        assert sent, getattr(outcome, "rationale", outcome)
        assert sent[0]["build"] == BUILD_ID
        assert sent[0]["declared_at"] == start_commit
        assert "by_hand" not in sent[0]

    @pytest.mark.asyncio
    async def test_a_row_with_no_recorded_commit_sends_the_build_and_no_claim(
        self, project, helper, tmp_path
    ) -> None:
        """No commit on the record is not a licence to claim a by-hand run.

        The build still travels, and the helper asks the coordinator what that
        build starts from. What must never happen is the old answer: a claim
        this leg has no grounds to make, and a read at whatever HEAD the copy
        on the far side happens to be at.
        """
        worktree = _a_journey_worktree(project)
        await _a_leg(
            config=_config(project, sandbox_url=helper.url),
            row=_Row(
                build_id=BUILD_ID,
                repo=REPO,
                start_commit=None,
                worktree_path=str(worktree),
            ),
        )
        sent = helper.only("/guardkit-leg")
        assert sent, "the dispatch sent no leg request at all"
        assert sent[0]["build"] == BUILD_ID
        assert "declared_at" not in sent[0]
        assert "by_hand" not in sent[0]

    def test_and_the_real_helper_refuses_it_rather_than_reading_at_its_own_head(
        self, project, monkeypatch, tmp_path
    ) -> None:
        """And what the REAL helper does with that request: nothing, in words.

        The same shape the dispatch sends above — the build named, no commit,
        and a declaration to read — put to the real route against a coordinator
        that holds no record for that build. That is what a row queued before
        the starting rule existed looks like, and it is the case that used to
        fall through to whatever HEAD the far side happened to be at. It is
        refused, and nothing is started.
        """
        worktree = _a_journey_worktree(project)
        with _a_coordinator_that_recorded({}, monkeypatch):
            status, body = _ask_the_real_helper(
                project, "/guardkit-leg", _a_leg_request(worktree, build=BUILD_ID)
            )
        assert status == 400
        assert BUILD_ID in body["error"]
        assert "by hand" in body["error"]
        assert "exit_code" not in body


# ---------------------------------------------------------------------------
# The merge-ready gates reader, which had the build id all along
# ---------------------------------------------------------------------------


class TestTheGatesReaderStampsTheProjectsOwnCommand:
    """The third door that used to claim a by-hand run, with a build in hand.

    The reader runs the PROJECT'S OWN declared test command inside the
    repository's sandbox to decide whether a merge card may be published. That
    command is launched from the project's own declarations, so the same
    binding applies — and the build id was right there in the reader's own
    parameters the whole time.
    """

    def test_the_declared_command_carries_the_build_and_the_recorded_commit(
        self, project, start_commit, pool, helper, tmp_path
    ) -> None:
        from types import SimpleNamespace

        from forge.cli._serve_conductor import make_gates_green_reader

        worktree = _a_journey_worktree(project)
        _a_build_row(pool, start_commit=start_commit, worktree=worktree)
        reader = make_gates_green_reader(
            pool=pool,
            config=_config(project, sandbox_url=helper.url),
            # Only the command runner is the real one: what this test is about
            # is the request THAT sends, so the two seams either side of it
            # answer without going anywhere.
            sandbox_declaration_loader=lambda _root, **_kw: SimpleNamespace(
                test="qa/run", test_timeout=30
            ),
            sandbox_stamps_leg=lambda **_kw: SimpleNamespace(
                status="not-enforced", detail="", blocks_card=False, attended=()
            ),
        )

        reader(build_id=BUILD_ID, branch=f"fix/{FEATURE_ID}")

        sent = helper.only("/run")
        assert sent, "the reader ran no command in the sandbox at all"
        assert sent[0]["build"] == BUILD_ID
        assert sent[0]["declared_at"] == start_commit
        assert "by_hand" not in sent[0]


# ---------------------------------------------------------------------------
# The REAL helper, against the record
# ---------------------------------------------------------------------------


def _ask_the_real_helper(
    project: Path, route: str, body: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """POST to the REAL sidecar route and read its answer back."""
    server = build_server(port=0, config_loader=lambda: _config(project))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}{route}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        try:
            with urllib.request.urlopen(request, timeout=60) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as refused:  # a 4xx is an answer
            return refused.code, json.loads(refused.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()


def _a_merge_request(**extra: Any) -> dict[str, Any]:
    return {
        "repo": REPO,
        "feature_id": FEATURE_ID,
        "expect_main_sha": "a" * 40,
        "timeout_seconds": 5,
        "memory_project": THE_MEMORY,
        "launch_settings": [DECLARED_SETTING],
        **extra,
    }


def _a_leg_request(worktree: Path, **extra: Any) -> dict[str, Any]:
    return {
        "repo": REPO,
        "cwd": str(worktree),
        "subcommand": "task-review",
        "args": ["--task-id", TASK_ID],
        "timeout_seconds": 5,
        "with_nats_streaming": False,
        "memory_project": THE_MEMORY,
        "launch_settings": [DECLARED_SETTING],
        **extra,
    }


class TestAStampIsALabelAndNotAQuestion:
    """What the stamp must NOT cost: a request that reads no declaration.

    The merge word's own command asks the project for nothing — no memory, no
    setting name — so there is no declaration to read and no commit to read one
    at. Stamping it says whose work it is, which is worth saying; it must not
    drag the request into a check against a record it was never going to read
    anything at. Measured here on the real route, because that is where it was
    measured going wrong: a stamped press-shaped request answered 400 where its
    parent answered 200, on a machine with no coordinator configured and on a
    build the coordinator has no record for.
    """

    def _a_stand_in_command(self, tmp_path: Path, monkeypatch) -> None:
        """A stand-in for the project's own command: it answers and exits.

        The route has to REACH the command for a 200 to mean anything, so this
        is the smallest thing that can be reached. It runs nothing, reads
        nothing and writes nothing.
        """
        import stat

        from forge.deploy_sidecar.service import GUARDKIT_PATH_ENV

        where = tmp_path / "bin"
        where.mkdir(parents=True, exist_ok=True)
        command = where / "stand-in"
        command.write_text(
            '#!/bin/sh\necho \'{"outcome": "merged"}\'\nexit 0\n', encoding="utf-8"
        )
        command.chmod(command.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        monkeypatch.setenv(GUARDKIT_PATH_ENV, str(command))

    def _press_shaped(self, **extra: Any) -> dict[str, Any]:
        """The body the press really sends: nothing declared is asked for."""
        return {
            "repo": REPO,
            "feature_id": FEATURE_ID,
            "expect_main_sha": "a" * 40,
            "timeout_seconds": 20,
            **extra,
        }

    def test_a_stamped_press_shaped_merge_runs_with_no_coordinator_at_all(
        self, project, start_commit, monkeypatch, tmp_path
    ) -> None:
        """Nothing declared is asked for, so nothing is bound and nobody is asked."""
        self._a_stand_in_command(tmp_path, monkeypatch)
        monkeypatch.delenv(COORDINATOR_OWNER_ENV, raising=False)
        status, body = _ask_the_real_helper(
            project,
            "/guardkit-merge",
            self._press_shaped(build=BUILD_ID, declared_at=start_commit),
        )
        assert status == 200, body
        assert body["exit_code"] == 0

    def test_a_stamped_merge_a_coordinator_has_no_record_for_still_runs(
        self, project, start_commit, monkeypatch, tmp_path
    ) -> None:
        """A historical row with no recorded starting commit is not a refusal here.

        There is nothing to refuse it over: this request reads none of the
        project's own declarations, so no commit is chosen for it and no record
        decides anything.
        """
        self._a_stand_in_command(tmp_path, monkeypatch)
        with _a_coordinator_that_recorded({}, monkeypatch):
            status, body = _ask_the_real_helper(
                project,
                "/guardkit-merge",
                self._press_shaped(build=BUILD_ID, declared_at=start_commit),
            )
        assert status == 200, body
        assert body["exit_code"] == 0

    def test_the_same_request_asking_for_a_declaration_is_refused_as_before(
        self, project, start_commit, monkeypatch, tmp_path
    ) -> None:
        """And the moment it asks for one, every word of the binding is back."""
        self._a_stand_in_command(tmp_path, monkeypatch)
        monkeypatch.delenv(COORDINATOR_OWNER_ENV, raising=False)
        status, body = _ask_the_real_helper(
            project,
            "/guardkit-merge",
            self._press_shaped(
                build=BUILD_ID,
                declared_at=start_commit,
                memory_project=THE_MEMORY,
                launch_settings=[DECLARED_SETTING],
            ),
        )
        assert status == 400
        assert COORDINATOR_OWNER_ENV in body["error"]
        assert "exit_code" not in body


class TestTheHelperBindsTheStampToTheRecord:
    """What the stamp BUYS: the far side checks it, and refuses a wrong one.

    These go to the real route. A 400 is the whole answer being tested — the
    refusal comes back before anything is started, so none of these launches a
    command or needs a build system to exist.
    """

    def test_a_merge_whose_commit_is_not_the_recorded_one_is_refused(
        self, project, start_commit, monkeypatch, tmp_path
    ) -> None:
        somebody_elses = "b" * 40
        with _a_coordinator_that_recorded({BUILD_ID: start_commit}, monkeypatch):
            status, body = _ask_the_real_helper(
                project,
                "/guardkit-merge",
                _a_merge_request(build=BUILD_ID, declared_at=somebody_elses),
            )
        assert status == 400
        assert start_commit in body["error"]
        assert somebody_elses in body["error"]

    def test_a_leg_whose_commit_is_not_the_recorded_one_is_refused(
        self, project, start_commit, monkeypatch, tmp_path
    ) -> None:
        worktree = _a_journey_worktree(project)
        with _a_coordinator_that_recorded({BUILD_ID: start_commit}, monkeypatch):
            status, body = _ask_the_real_helper(
                project,
                "/guardkit-leg",
                _a_leg_request(worktree, build=BUILD_ID, declared_at="c" * 40),
            )
        assert status == 400
        assert start_commit in body["error"]

    def test_a_merge_with_the_stamp_deleted_is_refused_not_served(
        self, project, start_commit, monkeypatch
    ) -> None:
        """The mutation: the same request with the pair taken off it.

        This is the shape a dropped coordinator stamp takes, and it is the
        shape both doors used to send on purpose. It must not be served.
        """
        with _a_coordinator_that_recorded({BUILD_ID: start_commit}, monkeypatch):
            status, body = _ask_the_real_helper(
                project, "/guardkit-merge", _a_merge_request()
            )
        assert status == 400
        assert "by_hand" in body["error"]
        assert "exit_code" not in body

    def test_a_leg_with_the_stamp_deleted_is_refused_not_served(
        self, project, start_commit, monkeypatch, tmp_path
    ) -> None:
        worktree = _a_journey_worktree(project)
        with _a_coordinator_that_recorded({BUILD_ID: start_commit}, monkeypatch):
            status, body = _ask_the_real_helper(
                project, "/guardkit-leg", _a_leg_request(worktree)
            )
        assert status == 400
        assert "by_hand" in body["error"]
        assert "exit_code" not in body

    def test_a_persons_own_claim_is_still_served_on_the_tree_route(
        self, project, start_commit, monkeypatch
    ) -> None:
        """The claim belongs to a person at a keyboard, and it still works.

        The planning chain stopped making it on 23 September 2026 — its runs
        are this factory's work and it names the run and the commit the record
        holds for it, like every other door. What is left is somebody writing
        a tree by hand, who says so; that request is still admitted, and the
        declared name is admitted with it.
        """
        with _a_coordinator_that_recorded({BUILD_ID: start_commit}, monkeypatch):
            status, body = _ask_the_real_helper(
                project,
                "/git/prepare-branch-and-write-tree",
                {
                    "repo": REPO,
                    "branch": "planning/handoff-7f21",
                    "files": {"docs/handoff.md": "the plan\n"},
                    "message": "planning: the handoff",
                    "checks": [],
                    "memory_project": THE_MEMORY,
                    "launch_settings": [DECLARED_SETTING],
                    "by_hand": True,
                },
            )
        assert status == 200, body
        assert body.get("status") == "success", body
