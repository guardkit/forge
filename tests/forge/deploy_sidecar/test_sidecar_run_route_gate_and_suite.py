"""The two shapes of ``POST /run`` whose program comes from the repository
(sandbox first, 2026-09-07, rules 85 and 88).

The deny-by-default posture of this service is that it can only run what the
repository itself declares. These two shapes keep it by a different route from
LAW 2's script allowlist: the live-gate driver must be exactly the argument
list ``deploy/profile.yaml`` declares, and the declared test command must be
exactly the command ``.guardkit/config.yaml`` declares. This file drives the
refusals and the shapes; the end-to-end runs live beside the deploy stage and
the conductor, where a real driver and a real worktree can be laid out.

Nothing live is touched: every run goes through an injected recorder rather
than a process, except where a test says otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy.profile import parse_deploy_profile, wrapper_inner_script
from forge.deploy_sidecar.service import (
    MERGE_TIMEOUT_MAX,
    allowed_scripts,
    declared_test_command,
    process_run_request,
)

REPO = "guardkit/api_test"
FAKE_LOADER = "tests_fake_toolchain_for_run_route"


class _Recorder:
    def __init__(self, result: tuple[int, str, str] = (0, "out", "err")) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[int, str, str]:
        self.calls.append(kwargs)
        return self.result


def _profile_dict(root: Path, *, script: str) -> dict[str, Any]:
    return {
        "env_id": "apitest",
        "compose": {"file": "docker-compose.yml", "script": script},
        "cwd": str(root),
        "live_gate": {
            "driver": ["python3", "qa/gates/local_live_gate.py"],
            "timeout_seconds": 120,
            "env": {"API_TEST_BASE_URL": "http://localhost:8901"},
        },
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(_profile_dict(root, script="deploy/sandbox-deploy.sh")),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def config(repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(repo.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(repo)}},
        }
    )


# ---------------------------------------------------------------------------
# LAW 2's companion: a wrapper names its inner script
# ---------------------------------------------------------------------------


class TestTheScriptAllowlistNamesTheWrappersInnerScript:
    def test_a_sandbox_wrapper_permits_the_script_it_runs(self, repo: Path) -> None:
        profile = parse_deploy_profile(
            _profile_dict(repo, script="deploy/sandbox-deploy.sh")
        )

        permitted = allowed_scripts(profile)

        assert "deploy/sandbox-deploy.sh" in permitted
        assert "deploy/deploy.sh" in permitted

    def test_an_ordinary_script_permits_only_itself(self, repo: Path) -> None:
        profile = parse_deploy_profile(_profile_dict(repo, script="deploy/deploy.sh"))

        permitted = allowed_scripts(profile)

        assert "deploy/deploy.sh" in permitted
        assert not any(name.startswith("deploy/sandbox-") for name in permitted)

    def test_the_derivation_is_the_one_the_deploy_stage_uses(self) -> None:
        # One derivation, in one place: the stage and this allowlist must not
        # drift, or the stage would send a script the sidecar refuses.
        from forge.deploy.stage import wrapper_inner_script as stage_side

        assert stage_side is wrapper_inner_script
        assert wrapper_inner_script("deploy/sandbox-deploy.sh") == "deploy/deploy.sh"


# ---------------------------------------------------------------------------
# The live-gate driver shape
# ---------------------------------------------------------------------------


class TestTheLiveGateDriverMustBeTheDeclaredOne:
    def test_the_declared_driver_runs_and_the_answer_says_where(
        self, repo: Path, config: ForgeConfig
    ) -> None:
        runner = _Recorder((0, '{"verdict": "pass"}', ""))

        status, body = process_run_request(
            {
                "repo": REPO,
                "driver": ["python3", "qa/gates/local_live_gate.py"],
                "args": ["--feature", "FEAT-X", "--target", "apitest"],
                "env": {"API_TEST_BASE_URL": "http://localhost:8902"},
            },
            config=config,
            command_runner=runner,
        )

        assert status == 200
        assert body["exit_code"] == 0
        assert body["stdout"] == '{"verdict": "pass"}'
        assert body["cwd"] == str(repo)
        assert body["timed_out"] is False
        assert runner.calls[0]["argv"] == [
            "python3",
            "qa/gates/local_live_gate.py",
            "--feature",
            "FEAT-X",
            "--target",
            "apitest",
        ]
        assert runner.calls[0]["extra_env"] == {
            "API_TEST_BASE_URL": "http://localhost:8902"
        }

    def test_another_program_is_refused_before_anything_runs(
        self, config: ForgeConfig
    ) -> None:
        runner = _Recorder()

        status, body = process_run_request(
            {"repo": REPO, "driver": ["python3", "qa/gates/mine.py"]},
            config=config,
            command_runner=runner,
        )

        assert status == 400
        assert "deploy/profile.yaml declares" in body["error"]
        assert runner.calls == []

    def test_an_env_key_the_profile_does_not_name_is_refused(
        self, config: ForgeConfig
    ) -> None:
        runner = _Recorder()

        status, body = process_run_request(
            {
                "repo": REPO,
                "driver": ["python3", "qa/gates/local_live_gate.py"],
                "env": {"AWS_SECRET_ACCESS_KEY": "x"},
            },
            config=config,
            command_runner=runner,
        )

        assert status == 400
        assert "not allowlisted" in body["error"]
        assert runner.calls == []

    def test_a_wall_wider_than_the_route_allows_is_clamped_not_refused(
        self, config: ForgeConfig
    ) -> None:
        runner = _Recorder()

        status, body = process_run_request(
            {
                "repo": REPO,
                "driver": ["python3", "qa/gates/local_live_gate.py"],
                "timeout_seconds": MERGE_TIMEOUT_MAX * 4,
            },
            config=config,
            command_runner=runner,
        )

        assert status == 200
        assert runner.calls[0]["timeout"] == MERGE_TIMEOUT_MAX
        assert body["warnings"][0]["code"] == "timeout_clamped"
        assert "longer than the longest wall" in body["warnings"][0]["message"]

    def test_a_wall_that_is_not_a_number_is_still_a_refusal(
        self, config: ForgeConfig
    ) -> None:
        status, body = process_run_request(
            {
                "repo": REPO,
                "driver": ["python3", "qa/gates/local_live_gate.py"],
                "timeout_seconds": "soon",
            },
            config=config,
            command_runner=_Recorder(),
        )

        assert status == 400
        assert "positive number" in body["error"]


# ---------------------------------------------------------------------------
# The declared test command shape
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_loader(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = ModuleType(FAKE_LOADER)

    class _Declaration:
        def __init__(self, test: str | None, test_timeout: int = 90) -> None:
            self.test = test
            self.test_timeout = test_timeout

    def load_toolchain_declaration(root: Any) -> Any:
        path = Path(root) / ".guardkit" / "config.yaml"
        if not path.is_file():
            return None
        block = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        toolchain = block.get("toolchain") or {}
        if not toolchain.get("test"):
            return _Declaration(None)
        return _Declaration(
            str(toolchain["test"]), int(toolchain.get("test_timeout", 90))
        )

    module.load_toolchain_declaration = load_toolchain_declaration  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, FAKE_LOADER, module)
    return module


class TestTheDeclaredTestCommandIsReadFromTheRepository:
    def test_it_reads_the_command_and_the_wall(
        self, repo: Path, fake_loader: ModuleType
    ) -> None:
        (repo / ".guardkit").mkdir()
        (repo / ".guardkit" / "config.yaml").write_text(
            "toolchain:\n  test: uv run --frozen pytest -q\n  test_timeout: 900\n",
            encoding="utf-8",
        )

        command, timeout, error = declared_test_command(
            repo, module_candidates=(FAKE_LOADER,)
        )

        assert (command, timeout, error) == ("uv run --frozen pytest -q", 900, None)

    def test_no_declaration_is_a_plain_sentence_never_a_pass(
        self, repo: Path, fake_loader: ModuleType
    ) -> None:
        command, timeout, error = declared_test_command(
            repo, module_candidates=(FAKE_LOADER,)
        )

        assert command is None and timeout is None
        assert "declares no toolchain" in error

    def test_a_loader_that_is_not_importable_says_so(self, repo: Path) -> None:
        command, _timeout, error = declared_test_command(
            repo, module_candidates=("nothing_of_this_name_exists",)
        )

        assert command is None
        assert "not importable" in error
