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
