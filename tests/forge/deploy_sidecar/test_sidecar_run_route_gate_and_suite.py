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
    return _committed(root)


def _committed(root: Path) -> Path:
    """Put this throwaway project into a commit, and answer its root.

    A DECLARATION IS A COMMITTED LINE (27 September 2026). The helper reads a
    project's ``deploy/profile.yaml`` — the ``identity`` names and the
    ``candidate: env:`` names its environment door is widened by — out of the
    project's own history, never off the working copy. A project laid out in a
    directory with no history declares nothing, so these fixtures commit.
    """
    import subprocess

    def _git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c", "user.email=tests@example.invalid",
                "-c", "user.name=tests",
                "-c", "commit.gpgsign=false",
                *args,
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )

    if not (root / ".git").exists():
        _git("init", "-q", "-b", "main")
    _git("add", "-A")
    _git("commit", "-q", "--allow-empty", "-m", "the project as it is")
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
    def test_the_sidecar_inside_a_sandbox_permits_the_script_it_runs(
        self, repo: Path
    ) -> None:
        profile = parse_deploy_profile(
            _profile_dict(repo, script="deploy/sandbox-deploy.sh")
        )

        permitted = allowed_scripts(profile, inside_sandbox=True)

        assert "deploy/sandbox-deploy.sh" in permitted
        assert "deploy/deploy.sh" in permitted

    def test_the_sidecar_on_the_host_permits_only_the_wrapper(
        self, repo: Path
    ) -> None:
        """The wall Rich's rule of 2026-09-07 puts up (L3b's coach).

        On the host the wrapper is the whole point: it is what puts the work
        inside a sandbox. Permitting its inner script here would let something
        ask the host sidecar to run the repository's deploy straight against
        the host's Docker engine.
        """
        profile = parse_deploy_profile(
            _profile_dict(repo, script="deploy/sandbox-deploy.sh")
        )

        permitted = allowed_scripts(profile, inside_sandbox=False)

        assert "deploy/sandbox-deploy.sh" in permitted
        assert "deploy/deploy.sh" not in permitted

    def test_the_flag_comes_from_the_bootstraps_own_environment_value(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.deploy_sidecar.service import (
            SIDECAR_IN_SANDBOX_ENV,
            sidecar_is_inside_sandbox,
        )

        profile = parse_deploy_profile(
            _profile_dict(repo, script="deploy/sandbox-deploy.sh")
        )

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        assert sidecar_is_inside_sandbox() is False
        assert "deploy/deploy.sh" not in allowed_scripts(profile)

        monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
        assert sidecar_is_inside_sandbox() is True
        assert "deploy/deploy.sh" in allowed_scripts(profile)

    def test_the_in_sandbox_bootstrap_sets_that_value(self) -> None:
        """The one place the flag is turned on: the script that starts the
        sidecar inside a repository's sandbox."""
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        bootstrap = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "forge"
            / "cli"
            / "deploy_templates"
            / "sandbox-runner.sh"
        )

        assert f"export {SIDECAR_IN_SANDBOX_ENV}=1" in bootstrap.read_text(
            encoding="utf-8"
        )

    def test_the_host_sidecar_refuses_to_run_the_inner_script(
        self, repo: Path, config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        ran: list[Any] = []

        status, body = process_run_request(
            {"repo": REPO, "script": "deploy/deploy.sh"},
            config=config,
            script_runner=lambda **kw: ran.append(kw),
        )

        assert status == 400
        assert "deny by default" in body["error"]
        assert ran == []

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
    """The shape as the sidecar INSIDE a sandbox answers it.

    ``inside_sandbox=True`` is what the running service reads out of the
    environment the in-sandbox bootstrap sets; the host sidecar's refusal of
    the very same request is the class below.
    """

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
            inside_sandbox=True,
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
            inside_sandbox=True,
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
            inside_sandbox=True,
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
            inside_sandbox=True,
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
            inside_sandbox=True,
        )

        assert status == 400
        assert "positive number" in body["error"]


# ---------------------------------------------------------------------------
# Neither shape exists on the host (L3b's coach, 2026-09-08)
# ---------------------------------------------------------------------------


class TestTheHostSidecarHasNeitherOfTheTwoNewShapes:
    """The wall Rich's rule of 2026-09-07 puts up, on the route itself.

    Both shapes run a program the REPOSITORY declares — its live-gate driver,
    its whole test suite — and both exist so that those run where the
    repository lives. The sidecar on the host must refuse both outright, or
    this lane would have handed the host two new ways to run a repository's
    code under the operator's account. The refusal comes before anything is
    read, resolved or started.
    """

    def test_the_host_refuses_the_declared_test_command(
        self, repo: Path, config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        runner = _Recorder()

        status, body = process_run_request(
            {
                "repo": REPO,
                "declared_test": "uv run --frozen pytest -q",
                "cwd": str(repo / ".forge" / "worktrees" / "build-1"),
            },
            config=config,
            command_runner=runner,
        )

        assert status == 400
        assert "running on the host, not inside a repository's sandbox" in (
            body["error"]
        )
        assert runner.calls == []

    def test_the_host_refuses_the_live_gate_driver(
        self, repo: Path, config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        runner = _Recorder()

        status, body = process_run_request(
            {
                "repo": REPO,
                "driver": ["python3", "qa/gates/local_live_gate.py"],
                "args": ["--feature", "FEAT-X"],
            },
            config=config,
            command_runner=runner,
        )

        assert status == 400
        assert "running on the host, not inside a repository's sandbox" in (
            body["error"]
        )
        assert runner.calls == []

    def test_the_flag_the_bootstrap_sets_is_what_opens_them(
        self, repo: Path, config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No second switch: the same value that widens the script allowlist
        is the one that opens these two shapes."""
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        runner = _Recorder((0, "ok", ""))
        request = {
            "repo": REPO,
            "driver": ["python3", "qa/gates/local_live_gate.py"],
        }

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        refused, _ = process_run_request(
            dict(request), config=config, command_runner=runner
        )

        monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
        allowed, _ = process_run_request(
            dict(request), config=config, command_runner=runner
        )

        assert (refused, allowed) == (400, 200)
        assert len(runner.calls) == 1

    def test_a_request_naming_neither_is_the_route_it_always_was(
        self, repo: Path, config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repository without a sandbox changed nothing: the vetted-script
        request the host sidecar has always served still runs."""
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        ran: list[Any] = []

        status, _body = process_run_request(
            {"repo": REPO, "script": "deploy/sandbox-deploy.sh"},
            config=config,
            script_runner=lambda **kw: ran.append(kw) or (0, "out"),
        )

        assert status == 200
        assert len(ran) == 1


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


# ---------------------------------------------------------------------------
# The routing law's evidence route (rule 88, repaired 2026-09-08)
# ---------------------------------------------------------------------------
#
# It reads three things and judges none of them, and it fences the tree it
# reads from exactly as the other worktree routes do.


class TestTheStampsEvidenceRouteRefusesWhatItShould:
    """Every case here is asked of a sidecar INSIDE a sandbox.

    The route is one of the sandbox-only shapes (L3e, rule 89): the host
    sidecar refuses it outright, which is proved in
    :class:`TestTheStampsEvidenceRouteIsRefusedOnTheHost` below. These cases
    are about what the in-sandbox one refuses.
    """

    @staticmethod
    def _body(repo_path: Path, **over: Any) -> dict[str, Any]:
        body = {
            "repo": REPO,
            "feature_id": "FEAT-G88",
            "worktree": str(repo_path / ".forge" / "worktrees" / "build-1"),
        }
        body.update(over)
        return body

    def test_a_worktree_of_another_place_is_refused(
        self, repo: Path, config: ForgeConfig
    ) -> None:
        from forge.deploy_sidecar.service import process_stamps_evidence_request

        status, body = process_stamps_evidence_request(
            self._body(repo, worktree="/etc"), config=config, inside_sandbox=True
        )

        assert status == 400
        assert "not a journey worktree" in body["error"]

    def test_a_feature_id_that_is_not_a_plain_id_is_refused(
        self, repo: Path, config: ForgeConfig
    ) -> None:
        from forge.deploy_sidecar.service import process_stamps_evidence_request

        status, body = process_stamps_evidence_request(
            self._body(repo, feature_id="../../etc/passwd"),
            config=config,
            inside_sandbox=True,
        )

        assert status == 400
        assert "'feature_id'" in body["error"]

    def test_an_unknown_repository_is_refused(self, config: ForgeConfig) -> None:
        from forge.deploy_sidecar.service import process_stamps_evidence_request

        status, body = process_stamps_evidence_request(
            {
                "repo": "someone/else",
                "feature_id": "FEAT-G88",
                "worktree": "/tmp/x",
            },
            config=config,
            inside_sandbox=True,
        )

        assert status == 400
        assert "someone/else" in body["error"]


class TestTheStampsEvidenceRouteIsRefusedOnTheHost:
    """The third sandbox-only shape (L3e, rule 89, the third coach's second
    must-fix).

    The routing law's evidence is a sandboxed repository's clone, its journey
    worktree and its gate receipts. A host sidecar answering this request
    would be reading the operator's own checkout, so it refuses in one plain
    sentence saying which sidecar it is and where the request belongs — the
    same shape the declared test command and the live-gate driver already
    get, and for the same reason.
    """

    @staticmethod
    def _body(repo_path: Path) -> dict[str, Any]:
        return {
            "repo": REPO,
            "feature_id": "FEAT-G88",
            "worktree": str(repo_path / ".forge" / "worktrees" / "build-1"),
        }

    def test_the_host_says_which_sidecar_it_is_and_where_to_send_it(
        self, repo: Path, config: ForgeConfig
    ) -> None:
        from forge.deploy_sidecar.service import process_stamps_evidence_request

        status, body = process_stamps_evidence_request(
            self._body(repo), config=config, inside_sandbox=False
        )

        assert status == 400
        assert "running on the host" in body["error"]
        assert "routing-law evidence" in body["error"]
        assert "sidecar_url in planning.sandboxes" in body["error"]

    def test_the_default_answer_is_read_from_the_bootstraps_own_value(
        self, repo: Path, config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No flag passed = ask the environment, exactly as the service does."""
        from forge.deploy_sidecar.service import (
            SIDECAR_IN_SANDBOX_ENV,
            process_stamps_evidence_request,
        )

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        status, body = process_stamps_evidence_request(self._body(repo), config=config)
        assert status == 400 and "running on the host" in body["error"]

        monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
        status, body = process_stamps_evidence_request(self._body(repo), config=config)
        # Inside a sandbox the request is judged on its merits: this worktree
        # does not exist, but the route answers about the repository, not
        # about which sidecar is asking.
        assert "running on the host" not in str(body.get("error") or "")

    def test_the_real_route_on_a_host_sidecar_refuses_over_the_wire(
        self, repo: Path, config: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through the real handler on a real loopback port, as a caller sees it."""
        import json
        import threading
        import urllib.error
        import urllib.request

        from forge.deploy_sidecar.service import (
            SIDECAR_IN_SANDBOX_ENV,
            STAMPS_EVIDENCE_ROUTE,
            build_server,
        )

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        srv = build_server(port=0, config_loader=lambda: config)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        try:
            request = urllib.request.Request(
                f"http://{host}:{port}{STAMPS_EVIDENCE_ROUTE}",
                data=json.dumps(self._body(repo)).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=10)
                raise AssertionError("the host sidecar answered the request")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
                said = json.loads(exc.read().decode("utf-8"))["error"]
                assert "running on the host" in said
        finally:
            srv.shutdown()
            srv.server_close()
