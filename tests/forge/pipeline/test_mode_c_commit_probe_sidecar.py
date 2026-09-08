"""The fix journey's commit probe, asked where the journey worktree is.

The thirteenth seam (2026-09-08). A fix journey on a repository that has a
sandbox has its worktree cut inside that sandbox, so counting its commits with
git on the forge side raised "file not found" and the journey was closed out
one step short of its card. These tests pin the cure: a second probe that asks
the sandbox's sidecar the same question, and a chooser that picks between the
two per build.

Real code paths throughout: a real git repository in a temporary directory
with a real journey worktree cut by the sidecar's own route, a real sidecar
HTTP server on an ephemeral loopback port, and the real probes talking to it
over the wire. Nothing live is touched — every path is under ``tmp_path`` and
every address is loopback.

What is pinned:

* the sidecar probe counts nothing on a fresh tree and three after three
  commits, over a real socket;
* it counts from the branch the journey was cut from, not from main, when the
  build was queued on its own branch;
* every failure is loud: an unreachable sidecar, a refusal, an answer that is
  not a count — never a quiet zero, which would throw a real fix journey's
  work away;
* the failures both probes share — no build row, no recorded worktree, an
  allowlist denial — come back in exactly the same words from either;
* the chooser sends a sandboxed repository's build to the sidecar and every
  other build to today's probe, and with no sandboxes configured it IS
  today's probe.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import (
    build_server,
    process_git_worktree_add_request,
)
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.mode_c_commit_probe import (
    make_mode_c_commit_probe,
    make_mode_c_commit_probe_chooser,
    make_sidecar_mode_c_commit_probe,
)

REPO_KEY = "guardkit/api_test"
PLAIN_KEY = "guardkit/plain"
BUILD_ID = "build-FEAT-39F6-20260908161144"
BRANCH = "fix/TASK-CP-001-20260908"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — scratch fixture, list tokens, no shell
        ["git", *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout


def _run(coro: Any) -> Any:
    """Drive a coroutine — the suite does not depend on pytest-asyncio."""
    return asyncio.run(coro)


class _FakePool:
    """The slice of the persistence facade the probes read: one row."""

    def __init__(self, row: Any = None, *, raises: Exception | None = None) -> None:
        self._row = row
        self._raises = raises
        self.calls: list[str] = []

    def get_build_row(self, build_id: str) -> Any:
        self.calls.append(build_id)
        if self._raises is not None:
            raise self._raises
        return self._row


def _row(
    *,
    worktree_path: str | None,
    repo: str = REPO_KEY,
    branch: str | None = "main",
) -> SimpleNamespace:
    return SimpleNamespace(
        build_id=BUILD_ID,
        repo=repo,
        branch=branch,
        worktree_path=worktree_path,
    )


def _build(build_id: str = BUILD_ID) -> Build:
    return Build(build_id=build_id, status=BuildState.RUNNING, mode=BuildMode.MODE_C)


# ---------------------------------------------------------------------------
# The repository, its journey worktree, and a real sidecar in front of them
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway clone standing in for the one inside the sandbox: one
    commit on ``main`` and the repair branch a fix journey is queued on."""
    path = tmp_path / "api_test"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "README").write_text("scratch\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    _git(path, "branch", "repair/TASK-CP-001")
    return path.resolve()


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    """A repository with no sandbox — its builds keep today's path."""
    path = tmp_path / "plain"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "README").write_text("plain\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path.resolve()


@pytest.fixture
def cfg(repo: Path, plain_repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {
                "target_repo_paths": {
                    REPO_KEY: str(repo),
                    PLAIN_KEY: str(plain_repo),
                }
            },
        }
    )


@pytest.fixture
def sidecar(cfg: ForgeConfig):
    """A real sidecar on an ephemeral loopback port."""
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _cut(cfg: ForgeConfig, repo: Path, *, base: str = "main") -> Path:
    status, body = process_git_worktree_add_request(
        {
            "repo": REPO_KEY,
            "path": str(repo / ".forge" / "worktrees" / BUILD_ID),
            "branch": BRANCH,
            "base_ref": base,
        },
        config=cfg,
    )
    assert status == 200 and body["status"] == "success", body
    return Path(body["path"])


def _commit(tree: Path, name: str) -> None:
    (tree / name).write_text(name, encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-m", f"add {name}")


# ---------------------------------------------------------------------------
# The sidecar probe, end to end
# ---------------------------------------------------------------------------


class TestTheSidecarProbeOverARealSocket:
    def test_a_fresh_journey_has_no_commits(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        tree = _cut(cfg, repo)
        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path=str(tree))),
            sidecar_url=sidecar,
            repo=REPO_KEY,
        )

        result = _run(probe(_build()))

        assert result.failed is False
        assert result.count == 0
        assert result.has_commits is False

    def test_three_commits_are_counted_and_the_journey_has_commits(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        tree = _cut(cfg, repo)
        for name in ("one", "two", "three"):
            _commit(tree, name)
        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path=str(tree))),
            sidecar_url=sidecar,
            repo=REPO_KEY,
        )

        result = _run(probe(_build()))

        assert result.failed is False
        assert result.count == 3
        assert result.has_commits is True

    def test_a_repair_branch_is_counted_from_its_own_branch(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        # The build was queued on repair/TASK-CP-001, whose own commit must
        # never be read as a leg's work (Part M rule 56).
        tree = _cut(cfg, repo, base="repair/TASK-CP-001")
        _commit(tree, "the-fix")
        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(
                _row(worktree_path=str(tree), branch="repair/TASK-CP-001")
            ),
            sidecar_url=sidecar,
            repo=REPO_KEY,
        )

        result = _run(probe(_build()))

        assert result.failed is False and result.count == 1

    def test_a_trailing_slash_on_the_address_is_not_two_slashes_on_the_wire(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        tree = _cut(cfg, repo)
        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path=str(tree))),
            sidecar_url=sidecar + "/",
            repo=REPO_KEY,
        )

        assert _run(probe(_build())).failed is False


class TestTheSidecarProbeFailsLoudly:
    """A probe that cannot answer must never be read as "no commits"."""

    def test_a_sidecar_that_cannot_be_reached_is_a_failure(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        tree = _cut(cfg, repo)
        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path=str(tree))),
            # Nothing is listening here: the port is closed on purpose.
            sidecar_url="http://127.0.0.1:1",
            repo=REPO_KEY,
            sandbox_name="api-test-deploy",
        )

        result = _run(probe(_build()))

        assert result.failed is True
        assert result.count == 0
        assert "api-test-deploy" in (result.error or "")

    def test_a_refusal_comes_back_as_the_sidecars_own_sentence(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        # A worktree path that is not a journey tree of this repository: the
        # sidecar refuses it, and the refusal is the probe's failure.
        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path="/etc")),
            sidecar_url=sidecar,
            repo=REPO_KEY,
        )

        result = _run(probe(_build()))

        assert result.failed is True
        assert "and on no other path" in (result.error or "")

    def test_an_unknown_repository_key_is_a_failure(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        tree = _cut(cfg, repo)
        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path=str(tree))),
            sidecar_url=sidecar,
            repo="acme/ghost",
        )

        result = _run(probe(_build()))

        assert result.failed is True
        assert "unknown target repo" in (result.error or "")

    def test_an_answer_that_is_not_a_count_is_a_failure(self) -> None:
        def _post(url: str, body: dict, timeout: float) -> tuple[int, Any]:
            return 200, {"count": "lots"}

        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path="/repo/.forge/worktrees/b1")),
            sidecar_url="http://127.0.0.1:8925",
            repo=REPO_KEY,
            post=_post,
        )

        result = _run(probe(_build()))

        assert result.failed is True
        assert "'lots'" in (result.error or "")

    def test_a_transport_that_raises_is_a_failure_not_a_crash(self) -> None:
        def _post(url: str, body: dict, timeout: float) -> tuple[int, Any]:
            raise TimeoutError("the read timed out")

        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path="/repo/.forge/worktrees/b1")),
            sidecar_url="http://127.0.0.1:8925",
            repo=REPO_KEY,
            post=_post,
        )

        result = _run(probe(_build()))

        assert result.failed is True
        assert "TimeoutError: the read timed out" in (result.error or "")

    def test_what_it_sends_is_the_repository_the_tree_and_the_base(self) -> None:
        sent: list[dict] = []

        def _post(url: str, body: dict, timeout: float) -> tuple[int, Any]:
            sent.append({"url": url, "body": body})
            return 200, {"count": 2, "head": "abc123"}

        probe = make_sidecar_mode_c_commit_probe(
            _FakePool(_row(worktree_path="/repo/.forge/worktrees/b1")),
            sidecar_url="http://127.0.0.1:8925",
            repo=REPO_KEY,
            base_branch="release/2026-09",
            post=_post,
        )

        assert _run(probe(_build())).count == 2
        assert sent[0]["url"] == (
            "http://127.0.0.1:8925/git/worktree-commit-count"
        )
        assert sent[0]["body"] == {
            "repo": REPO_KEY,
            "path": "/repo/.forge/worktrees/b1",
            "base": "release/2026-09",
        }


class TestBothProbesFailInTheSameWords:
    """The three faults that happen before either probe leaves the box."""

    @staticmethod
    def _pair(pool: _FakePool, **kwargs: Any) -> tuple[Any, Any]:
        return (
            make_mode_c_commit_probe(pool, **kwargs),
            make_sidecar_mode_c_commit_probe(
                pool,
                sidecar_url="http://127.0.0.1:8925",
                repo=REPO_KEY,
                post=lambda *a: (500, {"error": "the wire was never reached"}),
                **kwargs,
            ),
        )

    def test_no_build_row(self) -> None:
        here, there = self._pair(_FakePool(None))

        assert _run(here(_build())).error == _run(there(_build())).error
        assert "no builds row" in (_run(there(_build())).error or "")

    def test_no_recorded_worktree(self) -> None:
        here, there = self._pair(_FakePool(_row(worktree_path=None)))

        assert _run(here(_build())).error == _run(there(_build())).error
        assert "worktree_path" in (_run(there(_build())).error or "")

    def test_a_row_that_cannot_be_read(self) -> None:
        here, there = self._pair(_FakePool(raises=RuntimeError("db is locked")))

        assert _run(here(_build())).error == _run(there(_build())).error
        assert "reading the build row" in (_run(there(_build())).error or "")

    def test_an_allowlist_denial(self) -> None:
        class _Deny:
            def is_allowed(self, build_id: str, path: str) -> bool:
                return False

        here, there = self._pair(
            _FakePool(_row(worktree_path="/repo/.forge/worktrees/b1")),
            worktree_allowlist=_Deny(),
        )

        assert _run(here(_build())).error == _run(there(_build())).error
        assert "allowlist denied" in (_run(there(_build())).error or "")


# ---------------------------------------------------------------------------
# The chooser
# ---------------------------------------------------------------------------


def _with_sandbox(cfg: ForgeConfig, sidecar_url: str) -> ForgeConfig:
    raw = cfg.model_dump()
    raw["planning"]["sandboxes"] = {
        REPO_KEY: {
            "name": "api-test-deploy",
            "sidecar_url": sidecar_url,
            "runner_url": "http://127.0.0.1:8924",
        }
    }
    return ForgeConfig.model_validate(raw)


class TestTheChooser:
    def test_with_no_sandboxes_it_is_todays_probe(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        # Not "behaves like": it IS the probe the composition built before
        # this lane, so a boot with no sandboxes composes exactly today's.
        pool = _FakePool(_row(worktree_path=None))

        probe = make_mode_c_commit_probe_chooser(pool, config=cfg)

        assert inspect.iscoroutinefunction(probe)
        result = _run(probe(_build()))
        assert result.failed is True
        assert "worktree_path" in (result.error or "")
        # One row read, by the probe itself: the chooser adds no look-up.
        assert pool.calls == [BUILD_ID]

    def test_a_sandboxed_repository_is_counted_inside_its_sandbox(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        tree = _cut(cfg, repo)
        _commit(tree, "the-fix")
        pool = _FakePool(_row(worktree_path=str(tree), repo=REPO_KEY))

        probe = make_mode_c_commit_probe_chooser(
            pool, config=_with_sandbox(cfg, sidecar)
        )
        result = _run(probe(_build()))

        # The tree exists only under the repository the sidecar serves; the
        # count came back over the wire.
        assert result.failed is False and result.count == 1

    def test_a_repository_without_a_sandbox_keeps_todays_path(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        # The row names the repository that has no sandbox, so the chooser
        # must not go near the wire: the probe it picks is the subprocess one,
        # which fails on the missing worktree in its own words.
        pool = _FakePool(_row(worktree_path=None, repo=PLAIN_KEY))

        probe = make_mode_c_commit_probe_chooser(
            pool,
            config=_with_sandbox(cfg, sidecar),
            sidecar_probe_factory=_never_called,
        )
        result = _run(probe(_build()))

        assert result.failed is True
        assert "worktree_path" in (result.error or "")

    def test_the_sidecar_probe_is_built_once_per_repository(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        built: list[str] = []

        async def _answer(build: Any) -> Any:
            from forge.pipeline.terminal_handlers.mode_c import CommitProbeResult

            return CommitProbeResult(count=7, failed=False)

        def _factory(*, repo: str, entry: Any) -> Any:
            built.append(repo)
            return _answer

        pool = _FakePool(_row(worktree_path="/repo/.forge/worktrees/b1"))
        probe = make_mode_c_commit_probe_chooser(
            pool,
            config=_with_sandbox(cfg, sidecar),
            sidecar_probe_factory=_factory,
        )

        assert _run(probe(_build())).count == 7
        assert _run(probe(_build())).count == 7
        assert built == [REPO_KEY]

    def test_a_row_that_cannot_be_read_falls_to_todays_probe(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        pool = _FakePool(raises=RuntimeError("db is locked"))

        probe = make_mode_c_commit_probe_chooser(
            pool,
            config=_with_sandbox(cfg, sidecar),
            sidecar_probe_factory=_never_called,
        )
        result = _run(probe(_build()))

        assert result.failed is True
        assert "reading the build row" in (result.error or "")

    def test_no_row_at_all_falls_to_todays_probe(
        self, cfg: ForgeConfig, repo: Path, sidecar: str
    ) -> None:
        pool = _FakePool(None)

        probe = make_mode_c_commit_probe_chooser(
            pool,
            config=_with_sandbox(cfg, sidecar),
            sidecar_probe_factory=_never_called,
        )
        result = _run(probe(_build()))

        assert result.failed is True
        assert "no builds row" in (result.error or "")


def _never_called(**kwargs: Any) -> Any:
    raise AssertionError(
        "the chooser built a sidecar probe for a build that has no sandbox"
    )


# ---------------------------------------------------------------------------
# The composition
# ---------------------------------------------------------------------------


def test_the_daemon_composes_the_chooser_and_it_still_answers(
    cfg: ForgeConfig, repo: Path, sidecar: str
) -> None:
    """``serve.py`` composes the chooser where it composed the probe."""
    from forge.cli.serve import build_conductor_mode_kwargs

    raw = _with_sandbox(cfg, sidecar).model_dump()
    raw["conductor"] = {"enabled": True, "seat": "qwen3-coder-30b"}
    config = ForgeConfig.model_validate(raw)
    tree = _cut(cfg, repo)
    _commit(tree, "the-fix")
    _commit(tree, "and-its-test")
    pool = _FakePool(_row(worktree_path=str(tree)))

    kwargs = build_conductor_mode_kwargs(pool=pool, config=config)
    result = _run(kwargs["mode_c_commit_probe"](_build()))

    assert result.failed is False and result.count == 2


def test_the_route_name_is_not_written_twice() -> None:
    """The probe posts to the sidecar's own constant, never to a copy."""
    from forge.deploy_sidecar.service import GIT_WORKTREE_COMMIT_COUNT_ROUTE

    sent: list[str] = []

    def _post(url: str, body: dict, timeout: float) -> tuple[int, Any]:
        sent.append(url)
        return 200, {"count": 0, "head": None}

    probe = make_sidecar_mode_c_commit_probe(
        _FakePool(_row(worktree_path="/repo/.forge/worktrees/b1")),
        sidecar_url="http://127.0.0.1:8925",
        repo=REPO_KEY,
        post=_post,
    )
    _run(probe(_build()))

    assert sent == [f"http://127.0.0.1:8925{GIT_WORKTREE_COMMIT_COUNT_ROUTE}"]
    assert json.dumps({"route": GIT_WORKTREE_COMMIT_COUNT_ROUTE})
