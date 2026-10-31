"""The sandbox helper runs the project's whole declared check (1 November 2026).

For a repository with a sandbox, the merge card's step asks the helper for
the full check: the root command plus every declared component and part,
found in the journey's worktree by GuardKit's own reader (stood in here,
because GuardKit's dependencies are not installed in Forge's tests), each run
in its own folder with its own install first and its own limit.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.forge.full_check_stand_in import full_check_parts

LOADER = "tests_full_check_toolchain_declaration"


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    from forge.deploy_sidecar import service

    worktree = tmp_path / "clone" / ".forge" / "worktrees" / "journey-1"
    for name in ("web", "admin"):
        (worktree / "apps" / name).mkdir(parents=True)
        (worktree / "apps" / name / "package.json").write_text("{}", encoding="utf-8")
    declaration = SimpleNamespace(
        test="bash scripts/check.sh",
        test_timeout=600,
        components=None,
        parts=[
            SimpleNamespace(
                under="apps", marker="package.json", test="npm test",
                test_timeout=360, install="npm ci", install_timeout=180,
            )
        ],
    )
    module = ModuleType(LOADER)
    module.load_toolchain_declaration = lambda _root: declaration  # type: ignore[attr-defined]
    module.full_check_parts = full_check_parts  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, LOADER, module)
    monkeypatch.setattr(service, "TOOLCHAIN_MODULE_CANDIDATES", (LOADER,))
    return SimpleNamespace(worktree=worktree, repo=tmp_path / "clone")


def _runner(answers: dict[str, int], calls: list) -> Any:
    def run(*, argv: list[str], cwd: str, timeout: float, **_kw: Any) -> tuple[int, str, str]:
        command = argv[-1]
        calls.append((command, Path(cwd).name, timeout))
        return answers.get(command, 0), f"{command} said its piece\n", ""

    return run


def test_every_part_runs_and_a_pass_is_exit_zero(declared: SimpleNamespace) -> None:
    from forge.deploy_sidecar.service import _process_declared_full_check

    calls: list = []
    status, body = _process_declared_full_check(
        cwd=str(declared.worktree),
        repo_path=declared.repo,
        command_runner=_runner({}, calls),
        memory_project=None,
        launch_settings=(),
    )
    assert status == 200
    assert body["exit_code"] == 0
    assert body["cwd"] == str(declared.worktree)
    assert calls == [
        ("bash scripts/check.sh", "journey-1", 600.0),
        ("npm ci", "admin", 180.0),
        ("npm test", "admin", 360.0),
        ("npm ci", "web", 180.0),
        ("npm test", "web", 360.0),
    ]


def test_a_red_part_is_exit_one(declared: SimpleNamespace) -> None:
    from forge.deploy_sidecar.service import _process_declared_full_check

    calls: list = []
    status, body = _process_declared_full_check(
        cwd=str(declared.worktree),
        repo_path=declared.repo,
        command_runner=_runner({"npm test": 1}, calls),
        memory_project=None,
        launch_settings=(),
    )
    assert status == 200
    assert body["exit_code"] == 1
    assert "apps/admin" in body["stdout"] and "failed" in body["stdout"]


def test_an_install_that_failed_could_not_run(declared: SimpleNamespace) -> None:
    from forge.deploy_sidecar.service import _process_declared_full_check

    calls: list = []
    status, body = _process_declared_full_check(
        cwd=str(declared.worktree),
        repo_path=declared.repo,
        command_runner=_runner({"npm ci": 2}, calls),
        memory_project=None,
        launch_settings=(),
    )
    assert status == 200
    assert body["exit_code"] is None
    assert "declared install (`npm ci`)" in body["could_not_run"]
    # A part whose install failed is not tested.
    assert ("npm test", "admin", 360.0) not in calls


def test_without_guardkits_reader_it_could_not_run(
    declared: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.deploy_sidecar.service import _process_declared_full_check

    del sys.modules[LOADER].full_check_parts
    status, body = _process_declared_full_check(
        cwd=str(declared.worktree),
        repo_path=declared.repo,
        command_runner=_runner({}, []),
        memory_project=None,
        launch_settings=(),
    )
    assert status == 200
    assert body["exit_code"] is None
    assert "full_check_parts" in body["could_not_run"]


def test_a_part_test_that_runs_out_of_time_is_exit_one(
    declared: SimpleNamespace,
) -> None:
    from forge.deploy_sidecar.service import (
        MERGE_TIMEOUT_EXIT_CODE,
        _process_declared_full_check,
    )

    calls: list = []
    status, body = _process_declared_full_check(
        cwd=str(declared.worktree),
        repo_path=declared.repo,
        command_runner=_runner({"npm test": MERGE_TIMEOUT_EXIT_CODE}, calls),
        memory_project=None,
        launch_settings=(),
    )
    assert status == 200
    assert body["exit_code"] == 1
    assert "`npm test` did not finish within its limit of 360s" in body["stdout"]


# ---------------------------------------------------------------------------
# Through the REAL runner (Codex's check, 31 October 2026)
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        [
            "git",
            "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def a_real_journey(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """A real clone with a real linked journey worktree under
    ``.forge/worktrees``, a component ``api`` and a pattern part
    ``apps/web``; only GuardKit's reader is stood in."""
    from forge.deploy_sidecar import service

    clone = tmp_path / "clone"
    (clone / "api").mkdir(parents=True)
    (clone / "api" / "go.mod").write_text("module api\n", encoding="utf-8")
    (clone / "apps" / "web").mkdir(parents=True)
    (clone / "apps" / "web" / "package.json").write_text("{}", encoding="utf-8")
    _git(clone, "init", "-q", "-b", "main")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")
    worktree = clone / ".forge" / "worktrees" / "journey-1"
    _git(clone, "worktree", "add", "-q", "-b", "fix/one", str(worktree))
    answers = SimpleNamespace(
        api_test="true", worktree=worktree, repo=clone, git_dir=(clone / ".git").resolve()
    )

    def load(_root: Any) -> Any:
        return SimpleNamespace(
            test="test -d api", test_timeout=60,
            components={
                "api": SimpleNamespace(
                    cwd="api", test=answers.api_test, test_timeout=60,
                    install="test -f go.mod", install_timeout=60,
                )
            },
            parts=[
                SimpleNamespace(
                    under="apps", marker="package.json",
                    test='test "${PWD##*/}" = web && test -f package.json',
                    test_timeout=60, install="true", install_timeout=60,
                )
            ],
        )

    module = ModuleType(LOADER)
    module.load_toolchain_declaration = load  # type: ignore[attr-defined]
    module.full_check_parts = full_check_parts  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, LOADER, module)
    monkeypatch.setattr(service, "TOOLCHAIN_MODULE_CANDIDATES", (LOADER,))
    return answers


def _check_with_the_real_runner(
    journey: SimpleNamespace, launch_settings: tuple[str, ...] = ()
) -> dict[str, Any]:
    from forge.deploy_sidecar.service import (
        _process_declared_full_check,
        run_project_program,
    )

    status, body = _process_declared_full_check(
        cwd=str(journey.worktree),
        repo_path=journey.repo,
        command_runner=run_project_program,
        memory_project=None,
        launch_settings=launch_settings,
    )
    assert status == 200
    return body


def test_a_nested_component_runs_through_the_real_runner(
    a_real_journey: SimpleNamespace,
) -> None:
    """A good multi-part feature passes: each part runs in its own folder,
    and its private home is in the clone's own git directory."""
    a_real_journey.api_test = (
        'test "${PWD##*/}" = api && test -f go.mod && '
        f'case "$HOME" in {a_real_journey.git_dir}/*) exit 0;; *) exit 3;; esac'
    )
    body = _check_with_the_real_runner(a_real_journey)
    assert body["exit_code"] == 0, body
    assert [p["label"] for p in body["parts"]] == ["root", "api", "apps/web"]
