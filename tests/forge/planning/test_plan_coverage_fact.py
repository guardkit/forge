"""The plan writer is told whether the project already declares a coverage
command (20 October 2026).

The plan writer holds a plan that sets up the project's coverage command to
declaring it before the tasks GuardKit measures for coverage. That only
matters when the project has no coverage command yet, and the plan writer
cannot read the project's files, so Forge reads ``.guardkit/config.yaml`` and
sends ``declares_coverage_command`` on the target-repository descriptor:
true or false on every plan call, false when the file cannot be read.

These tests drive the real planning driver over a real scratch repository
(the caller level) and pin the reading on its own. Every name is made up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.planning import coverage_declaration as cd
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning.test_driver_target_terminal import (
    CID,
    _git,
    _init_scratch_repo,
    _local_remotes,  # noqa: F401 — the autouse fixture: the remotes here are local folders
    _make_driver,
    _publish_to_origin,
    _queue,
    _recorded_runner,
    store,  # noqa: F401 — the fixture, re-exported for these tests
)


def _declare(repo: Path, toolchain: str) -> None:
    """Add a ``toolchain:`` block to the scratch project's own config and
    publish it, keeping what the harness already wrote there."""
    config = repo / ".guardkit" / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8") + toolchain, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the toolchain")
    _publish_to_origin(repo)


async def _descriptor_sent(store: SqlitePlanningRunStore, repo: Path, tmp_path: Path) -> dict:
    git = _recorded_runner(tmp_path / "wt", repo)
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    return h.ctx["counters"]["last_descriptor"]


@pytest.mark.asyncio
async def test_a_project_that_declares_coverage_is_said_to(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(repo, 'toolchain:\n  test: "npm test"\n  coverage: "npm run coverage"\n')
    descriptor = await _descriptor_sent(store, repo, tmp_path)
    assert descriptor[cd.DESCRIPTOR_FIELD] is True


@pytest.mark.asyncio
async def test_the_root_and_every_component_declaring_coverage_is_true(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(
        repo,
        "toolchain:\n  test: \"npm test\"\n  coverage: \"npm run coverage\"\n"
        "  components:\n    web:\n"
        "      cwd: web\n      test: \"npm test\"\n      coverage: \"npm run coverage\"\n",
    )
    descriptor = await _descriptor_sent(store, repo, tmp_path)
    assert descriptor[cd.DESCRIPTOR_FIELD] is True


@pytest.mark.asyncio
async def test_a_mixed_project_is_false(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """GuardKit decides task by task with no fallback between the root and a
    component, so a project where only one component declares coverage still
    has tasks GuardKit would stop: the plan writer's rule must stay on."""
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(
        repo,
        "toolchain:\n  test: \"npm test\"\n  components:\n    web:\n"
        "      cwd: web\n      test: \"npm test\"\n      coverage: \"npm run coverage\"\n",
    )
    descriptor = await _descriptor_sent(store, repo, tmp_path)
    assert descriptor[cd.DESCRIPTOR_FIELD] is False


@pytest.mark.asyncio
async def test_a_project_with_no_coverage_command_is_said_not_to(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(repo, 'toolchain:\n  test: "npm test"\n  coverage: ""\n')
    descriptor = await _descriptor_sent(store, repo, tmp_path)
    assert descriptor[cd.DESCRIPTOR_FIELD] is False


@pytest.mark.asyncio
async def test_a_config_that_cannot_be_read_is_said_not_to(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Unreadable is false: the plan writer's rule still runs, the safe side."""
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    git = _recorded_runner(tmp_path / "wt", repo)

    async def _cannot_read(*, repo_path: str, branch: str, file_path: str):
        if file_path == cd.CONFIG_PATH:
            raise OSError("the file could not be read")
        return await type(git).read_file_from_branch(
            git, repo_path=repo_path, branch=branch, file_path=file_path
        )

    git.read_file_from_branch = _cannot_read  # type: ignore[method-assign]
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    await h.driver.drive(CID)
    assert h.ctx["counters"]["last_descriptor"][cd.DESCRIPTOR_FIELD] is False


@pytest.mark.asyncio
async def test_a_branch_without_the_file_falls_back_to_the_repositorys_tip(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The same two places the plan-commit stamping reads the file: the
    planning branch, else the repository's current tip."""
    repo = tmp_path / "app"
    _init_scratch_repo(repo)
    _declare(repo, 'toolchain:\n  coverage: "npm run coverage"\n')
    git = _recorded_runner(tmp_path / "wt", repo)
    asked: list[str] = []

    async def _branch_lacks_it(*, repo_path: str, branch: str, file_path: str):
        if file_path == cd.CONFIG_PATH:
            asked.append(branch)
            if branch != "HEAD":
                return None
        return await type(git).read_file_from_branch(
            git, repo_path=repo_path, branch=branch, file_path=file_path
        )

    git.read_file_from_branch = _branch_lacks_it  # type: ignore[method-assign]
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    await h.driver.drive(CID)
    assert h.ctx["counters"]["last_descriptor"][cd.DESCRIPTOR_FIELD] is True
    assert "HEAD" in asked and asked[0] != "HEAD"


_READING_CASES = [
        ('toolchain:\n  coverage: "make coverage"\n', True),
        # Every place: the root and each listed component.
        ("toolchain:\n  coverage: r\n  components:\n    a:\n      cwd: a\n"
         "      coverage: x\n    b:\n      cwd: b\n      coverage: y\n", True),
        ("toolchain:\n  coverage: r\n  components:\n    a:\n      cwd: a\n"
         "      coverage: x\n    b:\n      cwd: b\n      test: y\n", False),
        ("toolchain:\n  components:\n    a:\n      cwd: a\n      coverage: x\n", False),
        ("toolchain:\n  coverage: r\n  components:\n    a:\n      cwd: a\n      test: x\n", False),
        ("toolchain:\n  coverage: r\n  components: [a]\n", False),
        # An empty command anywhere: GuardKit ignores the whole block.
        ('toolchain:\n  coverage: "make coverage"\n  lint: ""\n', False),
        ('toolchain:\n  coverage: r\n  components:\n    a:\n      cwd: a\n'
         '      coverage: x\n      test: ""\n', False),
        ('toolchain:\n  coverage: "  "\n', False),
        ('toolchain:\n  coverage: ""\n', False),
        ("toolchain:\n  coverage_timeout: 30\n", False),
        ("toolchain:\n  test: x\n", False),
        ("coverage: x\n", False),
        ("toolchain: [x]\n", False),
        ("not: [valid", False),
        ("", False),
        (None, False),
]


@pytest.mark.parametrize(("text", "declared"), _READING_CASES)
def test_the_reading_follows_guardkits(text: str | None, declared: bool) -> None:
    assert cd.declares_coverage_command(text) is declared


def test_guardkits_own_answer_is_used_when_guardkit_is_importable(monkeypatch) -> None:
    """In the factory image GuardKit's parser decides; the YAML reading is
    only the fallback. A stand-in GuardKit that disagrees with the fallback
    shows which one answered."""
    import sys
    import types

    calls: list[object] = []

    class _Declaration:
        is_empty = False
        component_names: list[str] = []

    def parse_toolchain_block(raw):
        calls.append(raw)
        if "misspelt" in raw:
            raise ValueError("unknown key")
        return _Declaration()

    def declared_coverage_command(declaration, component=None):
        return None  # GuardKit says: nothing declared

    module = types.ModuleType("guardkit.orchestrator.toolchain_declaration")
    module.parse_toolchain_block = parse_toolchain_block
    module.declared_coverage_command = declared_coverage_command
    monkeypatch.setitem(sys.modules, "guardkit", types.ModuleType("guardkit"))
    monkeypatch.setitem(
        sys.modules, "guardkit.orchestrator", types.ModuleType("guardkit.orchestrator")
    )
    monkeypatch.setitem(sys.modules, "guardkit.orchestrator.toolchain_declaration", module)

    text = 'toolchain:\n  coverage: "make coverage"\n'
    assert cd._fallback_reading({"coverage": "make coverage"}) is True
    assert cd.declares_coverage_command(text) is False
    assert calls == [{"coverage": "make coverage"}]
    assert cd.declares_coverage_command('toolchain:\n  misspelt: x\n') is False


def test_guardkits_reading_and_the_fallback_agree() -> None:
    """Where GuardKit can be imported, its answer and the fallback's agree
    on every case above. Skipped where it cannot (this machine's Forge
    environment); the factory image installs it."""
    try:
        from guardkit.orchestrator import toolchain_declaration  # noqa: F401
    except Exception:  # noqa: BLE001
        pytest.skip("GuardKit is not importable in this environment")
    import yaml

    for text, declared in _READING_CASES:
        data = yaml.safe_load(text) if isinstance(text, str) else None
        raw = data.get("toolchain") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            assert cd._guardkit_reading(raw) is cd._fallback_reading(raw), text


def test_guardkits_answer_needs_the_root_and_every_component(monkeypatch) -> None:
    """Through GuardKit's path too, one component without its own coverage
    command makes the answer false (GuardKit decides task by task)."""
    import sys
    import types

    class _Declaration:
        is_empty = False

        def __init__(self, raw):
            self.raw = raw
            self.component_names = sorted(raw.get("components") or {})

    def declared_coverage_command(declaration, component=None):
        block = declaration.raw if component is None else declaration.raw["components"][component]
        return block.get("coverage")

    module = types.ModuleType("guardkit.orchestrator.toolchain_declaration")
    module.parse_toolchain_block = _Declaration
    module.declared_coverage_command = declared_coverage_command
    monkeypatch.setitem(sys.modules, "guardkit", types.ModuleType("guardkit"))
    monkeypatch.setitem(
        sys.modules, "guardkit.orchestrator", types.ModuleType("guardkit.orchestrator")
    )
    monkeypatch.setitem(sys.modules, "guardkit.orchestrator.toolchain_declaration", module)

    both = {"coverage": "r", "components": {"a": {"coverage": "x"}, "b": {"coverage": "y"}}}
    one = {"coverage": "r", "components": {"a": {"coverage": "x"}, "b": {}}}
    blank = {"coverage": "  "}
    assert cd._guardkit_reading(both) is True
    assert cd._guardkit_reading(one) is False
    assert cd._guardkit_reading({"components": {"a": {"coverage": "x"}}}) is False
    assert cd._guardkit_reading(blank) is False
