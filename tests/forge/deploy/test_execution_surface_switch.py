"""Tests for the deploy execution-surface switch (S1, C4 residue #24).

Proves:

* ``DeployStageConfig`` defaults are byte-identical opt-in (local surface).
* ``register_deploy_handlers(script_runner=...)`` routes ONLY the
  docker-touching steps (``deploy_compose``, ``health_check``) through the
  injected runner; the DB/model/secret steps (seed/warm/import/smoke) stay on
  the in-process subprocess core.
* ``DeployStageRunner._resolve_script_runner`` picks the surface from config
  (local → None; sidecar → a repo-bound ``SidecarScriptRunner``; sidecar with
  no target repo → loud refusal).
* The ``SidecarScriptRunner`` HTTP client round-trips against a real sidecar,
  relays a deny-by-default refusal as a non-zero exit, and never raises when the
  sidecar is unreachable.

The *local default untouched* claim is proven by the existing deploy/executor
suites passing unmodified; this file adds only the sidecar-mode coverage.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from forge.config.models import DeployStageConfig, ForgeConfig
from forge.deploy.sidecar_runner import (
    SIDECAR_TRANSPORT_EXIT_CODE,
    SidecarScriptRunner,
)
from forge.deploy.steps import register_deploy_handlers
from forge.deploy_sidecar.service import build_server
from forge.executor.registry import StepTypeRegistry
from forge.persistence.repositories.runbook_models import Step, StepStatus


def _step(step_type: str, params: dict[str, Any]) -> Step:
    return Step(
        step_type=step_type,
        params=params,
        status=StepStatus.pending,
        sequence_index=0,
    )


class _RecordingRunner:
    def __init__(self, result: tuple[int, str] = (0, "routed-via-runner")) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[int, str]:
        self.calls.append(kwargs)
        return self.result


# ---------------------------------------------------------------------------
# Config defaults — safe opt-in, byte-identical
# ---------------------------------------------------------------------------


def test_config_defaults_local_surface() -> None:
    cfg = DeployStageConfig()
    assert cfg.execution_surface == "local"
    assert cfg.sidecar_url == "http://127.0.0.1:8125"


# ---------------------------------------------------------------------------
# register_deploy_handlers routes ONLY deploy_compose + health_check
# ---------------------------------------------------------------------------


def _registry_with(runner: Any) -> StepTypeRegistry:
    from forge.deploy.live_gate import (
        UnconfiguredBrokerInspector,
        UnconfiguredLiveGateInvoker,
    )

    registry = StepTypeRegistry()
    register_deploy_handlers(
        registry,
        dry_run=False,
        live_gate_invoker=UnconfiguredLiveGateInvoker(),
        broker_inspector=UnconfiguredBrokerInspector(),
        script_runner=runner,
    )
    return registry


def test_deploy_compose_routes_through_injected_runner(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    registry = _registry_with(runner)
    handler = registry.resolve("deploy_compose")
    assert handler is not None
    out = handler(_step("deploy_compose", {"cwd": str(tmp_path), "script": "deploy.sh"}))
    assert len(runner.calls) == 1
    assert runner.calls[0]["script"] == "deploy.sh"
    assert out.result["captured_output"] == "routed-via-runner"


def test_deploy_compose_threads_o32_revert_env_through_runner(tmp_path: Path) -> None:
    # The O-32 revert-env threading is preserved through the sidecar seam (not
    # duplicated): REVERT + ROLLBACK_IMAGE_REF reach the injected runner.
    runner = _RecordingRunner()
    registry = _registry_with(runner)
    handler = registry.resolve("deploy_compose")
    assert handler is not None
    handler(
        _step(
            "deploy_compose",
            {
                "cwd": str(tmp_path),
                "script": "deploy.sh",
                "revert": True,
                "rollback_image_ref": "api_test:rollback-1",
            },
        )
    )
    extra_env = runner.calls[0]["extra_env"]
    assert extra_env["REVERT"] == "1"
    assert extra_env["ROLLBACK_IMAGE_REF"] == "api_test:rollback-1"


def test_health_check_routes_through_injected_runner(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    registry = _registry_with(runner)
    handler = registry.resolve("health_check")
    assert handler is not None
    out = handler(
        _step(
            "health_check",
            {"cwd": str(tmp_path), "checks": [{"cmd": "qa/health.sh"}]},
        )
    )
    assert len(runner.calls) == 1
    assert out.result["ran"][0]["captured_output"] == "routed-via-runner"


def test_seed_fixtures_stays_in_process_even_in_sidecar_mode(tmp_path: Path) -> None:
    # seed_fixtures is a DB-touching step — it must NOT route through the sidecar
    # runner even when one is injected. A bogus script hits the real in-process
    # core (exit 127), proving the injected runner was never used for it.
    runner = _RecordingRunner()
    registry = _registry_with(runner)
    handler = registry.resolve("seed_fixtures")
    assert handler is not None
    out = handler(
        _step(
            "seed_fixtures",
            {"cwd": str(tmp_path), "fixtures": [{"script": "does-not-exist.sh"}]},
        )
    )
    assert runner.calls == []  # sidecar runner never touched
    assert out.result["ran"][0]["exit_code"] == 127  # in-process FileNotFound


# ---------------------------------------------------------------------------
# DeployStageRunner._resolve_script_runner — surface selection
# ---------------------------------------------------------------------------


def _make_runner(config: DeployStageConfig, *, target_repo: str | None):
    from forge.deploy.live_gate import (
        UnconfiguredBrokerInspector,
        UnconfiguredLiveGateInvoker,
    )
    from forge.deploy.stage import DeployStageRunner

    return DeployStageRunner(
        repository=object(),  # type: ignore[arg-type] — not touched by the seam probe
        runbook_publisher=object(),
        deploy_publisher=object(),
        reservation=object(),  # type: ignore[arg-type]
        live_gate_invoker=UnconfiguredLiveGateInvoker(),
        broker_inspector=UnconfiguredBrokerInspector(),
        config=config,
        deploy_record_root="docs/state",
        target_repo=target_repo,
    )


def test_local_surface_resolves_to_none() -> None:
    runner = _make_runner(DeployStageConfig(execution_surface="local"), target_repo="x/y")
    assert runner._resolve_script_runner() is None


def test_sidecar_surface_resolves_to_repo_bound_client() -> None:
    cfg = DeployStageConfig(
        execution_surface="sidecar", sidecar_url="http://127.0.0.1:9999"
    )
    runner = _make_runner(cfg, target_repo="appmilla/api_test")
    resolved = runner._resolve_script_runner()
    assert isinstance(resolved, SidecarScriptRunner)
    assert resolved._repo == "appmilla/api_test"
    assert resolved._base_url == "http://127.0.0.1:9999"


def test_sidecar_surface_without_target_repo_refuses() -> None:
    import pytest

    cfg = DeployStageConfig(execution_surface="sidecar")
    runner = _make_runner(cfg, target_repo=None)
    with pytest.raises(ValueError, match="requires a target_repo"):
        runner._resolve_script_runner()


# ---------------------------------------------------------------------------
# SidecarScriptRunner HTTP client — end-to-end, refusal relay, never-raises
# ---------------------------------------------------------------------------


def _config(paths: dict[str, str]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": paths},
        }
    )


def _write_profile(repo_path: Path) -> None:
    d = repo_path / "deploy"
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("profile.yaml").write_text(
        yaml.safe_dump(
            {"env_id": "staging", "compose": {"file": "compose.yaml", "script": "deploy.sh"}}
        ),
        encoding="utf-8",
    )


def _serve(server: Any) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


def test_client_round_trips_against_real_sidecar(tmp_path: Path) -> None:
    repo = tmp_path / "api_test"
    repo.mkdir()
    _write_profile(repo)

    class _StubCore:
        def __call__(self, **kwargs: Any) -> tuple[int, str]:
            return (0, "deployed-ok")

    server = build_server(
        port=0,
        config_loader=lambda: _config({"appmilla/api_test": str(repo)}),
        script_runner=_StubCore(),
    )
    _serve(server)
    try:
        host, port = server.server_address[:2]
        client = SidecarScriptRunner(
            base_url=f"http://{host}:{port}", repo="appmilla/api_test"
        )
        exit_code, output = client(
            cwd="/ignored-by-sidecar",
            script="deploy.sh",
            env_file=None,
            timeout=30,
            extra_env={"REVERT": "1"},
        )
        assert exit_code == 0
        assert output == "deployed-ok"
    finally:
        server.shutdown()
        server.server_close()


def test_client_relays_deny_by_default_refusal(tmp_path: Path) -> None:
    repo = tmp_path / "api_test"
    repo.mkdir()
    _write_profile(repo)
    server = build_server(
        port=0, config_loader=lambda: _config({"appmilla/api_test": str(repo)})
    )
    _serve(server)
    try:
        host, port = server.server_address[:2]
        client = SidecarScriptRunner(
            base_url=f"http://{host}:{port}", repo="appmilla/api_test"
        )
        exit_code, output = client(
            cwd="/x", script="evil.sh", env_file=None, timeout=10
        )
        assert exit_code == SIDECAR_TRANSPORT_EXIT_CODE
        assert "sidecar refused (HTTP 400)" in output
        assert "deny by default" in output
    finally:
        server.shutdown()
        server.server_close()


def test_client_never_raises_when_sidecar_unreachable() -> None:
    # Nothing listening on this port — the client must return a non-zero exit
    # with a descriptive message, never raise (handlers rely on never-raises).
    client = SidecarScriptRunner(base_url="http://127.0.0.1:1", repo="appmilla/api_test")
    exit_code, output = client(cwd="/x", script="deploy.sh", env_file=None, timeout=1)
    assert exit_code == SIDECAR_TRANSPORT_EXIT_CODE
    assert "unreachable" in output


# ---------------------------------------------------------------------------
# The client sends the working directory; the sidecar honours a candidate
# tree and ignores anything else (protect-main, 2026-09-07)
# ---------------------------------------------------------------------------


def test_client_sends_the_working_directory_and_a_candidate_tree_is_honoured(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "api_test"
    repo.mkdir()
    _write_profile(repo)
    tree = repo / ".forge-candidates" / "FEAT-C1D0"
    tree.mkdir(parents=True)
    seen: list[dict[str, Any]] = []

    class _RecordingCore:
        def __call__(self, **kwargs: Any) -> tuple[int, str]:
            seen.append(kwargs)
            return (0, "ok")

    server = build_server(
        port=0,
        config_loader=lambda: _config({"appmilla/api_test": str(repo)}),
        script_runner=_RecordingCore(),
    )
    _serve(server)
    try:
        host, port = server.server_address[:2]
        client = SidecarScriptRunner(
            base_url=f"http://{host}:{port}", repo="appmilla/api_test"
        )
        exit_code, _ = client(
            cwd=str(tree), script="deploy.sh", env_file=None, timeout=30,
            extra_env={"CANDIDATE": "1"},
        )
        assert exit_code == 0
        assert seen[-1]["cwd"] == str(tree.resolve())
        exit_code, _ = client(
            cwd="/somewhere/else", script="deploy.sh", env_file=None, timeout=30
        )
        assert exit_code == 0
        assert seen[-1]["cwd"] == str(repo)
    finally:
        server.shutdown()
        server.server_close()


def test_client_relays_a_refused_candidate_tree(tmp_path: Path) -> None:
    repo = tmp_path / "api_test"
    repo.mkdir()
    _write_profile(repo)
    server = build_server(
        port=0, config_loader=lambda: _config({"appmilla/api_test": str(repo)})
    )
    _serve(server)
    try:
        host, port = server.server_address[:2]
        client = SidecarScriptRunner(
            base_url=f"http://{host}:{port}", repo="appmilla/api_test"
        )
        exit_code, output = client(
            cwd=str(repo / ".forge-candidates" / "FEAT-GONE"),
            script="deploy.sh", env_file=None, timeout=10,
        )
        assert exit_code == SIDECAR_TRANSPORT_EXIT_CODE
        assert "is not a candidate tree" in output
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# The answer must say it ran in the candidate tree (protect-main, 2026-09-07).
# A sidecar on the host running the code from before the candidate leg
# ignores the working directory and runs the checkout's main; a sidecar with
# a different checkout path runs somewhere else. Either would have been
# called "the branch, checked". The client refuses both, loudly.
# ---------------------------------------------------------------------------


def _fixed_answer_server(answer: dict[str, Any]) -> Any:
    """A stand-in deploy sidecar that answers every /run with ``answer``."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — http.server's own name
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps(answer).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", 0), _Handler)


def _client_for(server: Any) -> SidecarScriptRunner:
    host, port = server.server_address[:2]
    return SidecarScriptRunner(base_url=f"http://{host}:{port}", repo="appmilla/api_test")


def test_an_answer_without_cwd_is_refused_for_a_candidate_tree(tmp_path: Path) -> None:
    """An old sidecar: it ran main and says nothing about where."""
    tree = tmp_path / "api_test" / ".forge-candidates" / "FEAT-0LD1"
    tree.mkdir(parents=True)
    server = _fixed_answer_server({"exit_code": 0, "output_tail": "deployed-ok"})
    _serve(server)
    try:
        exit_code, output = _client_for(server)(
            cwd=str(tree), script="deploy.sh", env_file=None, timeout=10
        )
        assert exit_code == SIDECAR_TRANSPORT_EXIT_CODE
        assert output == (
            f"the deploy sidecar did not run in the candidate tree {tree} — it did "
            "not say where it ran, so it is running old code from before the "
            "candidate check; the candidate was not checked"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_an_answer_without_cwd_is_accepted_for_the_profiles_own_directory(
    tmp_path: Path,
) -> None:
    """An ordinary run never named a candidate tree, so an old sidecar's
    answer is as good as it always was."""
    repo = tmp_path / "api_test"
    repo.mkdir()
    server = _fixed_answer_server({"exit_code": 0, "output_tail": "deployed-ok"})
    _serve(server)
    try:
        exit_code, output = _client_for(server)(
            cwd=str(repo), script="deploy.sh", env_file=None, timeout=10
        )
        assert (exit_code, output) == (0, "deployed-ok")
    finally:
        server.shutdown()
        server.server_close()


def test_an_answer_that_names_a_different_directory_is_refused(tmp_path: Path) -> None:
    """A sidecar whose checkout path differs from the daemon's ran the script
    from its own checkout, not the tree."""
    repo = tmp_path / "api_test"
    tree = repo / ".forge-candidates" / "FEAT-0LD2"
    tree.mkdir(parents=True)
    server = _fixed_answer_server(
        {"exit_code": 0, "output_tail": "deployed-ok", "cwd": str(repo)}
    )
    _serve(server)
    try:
        exit_code, output = _client_for(server)(
            cwd=str(tree), script="deploy.sh", env_file=None, timeout=10
        )
        assert exit_code == SIDECAR_TRANSPORT_EXIT_CODE
        assert output == (
            f"the deploy sidecar did not run in the candidate tree {tree} — it ran "
            f"in {repo}, so it is running a different checkout path; the candidate "
            "was not checked"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_an_answer_naming_the_same_tree_spelled_differently_is_accepted(
    tmp_path: Path,
) -> None:
    """The sidecar answers the resolved path; the client may have sent an
    unresolved one. The same directory either way is honoured."""
    repo = tmp_path / "api_test"
    tree = repo / ".forge-candidates" / "FEAT-0LD3"
    tree.mkdir(parents=True)
    sent = repo / ".forge-candidates" / "FEAT-0LD3" / "." / ".." / "FEAT-0LD3"
    server = _fixed_answer_server(
        {"exit_code": 0, "output_tail": "deployed-ok", "cwd": str(tree.resolve())}
    )
    _serve(server)
    try:
        exit_code, output = _client_for(server)(
            cwd=str(sent), script="deploy.sh", env_file=None, timeout=10
        )
        assert (exit_code, output) == (0, "deployed-ok")
    finally:
        server.shutdown()
        server.server_close()


def test_a_command_the_executor_stopped_comes_back_as_no_deploy(tmp_path: Path) -> None:
    """The takeover word travels the whole way to whatever reads the step.

    The executor answers ``accepted: false`` with its own word when a later
    holder of the target stopped its command part-way. Through this client that
    is a non-zero exit carrying the word and the sentence — never a zero exit,
    and never a step that looks as though it ran.
    """
    from forge.deploy_sidecar.deploy_executor import STOPPED_BY_A_TAKEOVER

    repo = tmp_path / "api_test"
    repo.mkdir()
    server = _fixed_answer_server(
        {
            "accepted": False,
            "word": STOPPED_BY_A_TAKEOVER,
            "sentence": (
                "the deploy command for shop::live from build build-a (the "
                "target's counter 5) was stopped part-way by a later holder of "
                "the target, so it did not run to an end and nothing was "
                "deployed by this request."
            ),
            "exit_code": None,
            "output_tail": "starting a\n",
        }
    )
    _serve(server)
    try:
        exit_code, output = _client_for(server)(
            cwd=str(repo),
            script="deploy.sh",
            env_file=None,
            timeout=10,
            deploy={"target": "shop::live", "target_counter": 5, "build": "build-a"},
        )
        assert exit_code == SIDECAR_TRANSPORT_EXIT_CODE
        assert exit_code != 0
        assert output.startswith(f"[{STOPPED_BY_A_TAKEOVER}]")
        assert "nothing was deployed by this request" in output
    finally:
        server.shutdown()
        server.server_close()
