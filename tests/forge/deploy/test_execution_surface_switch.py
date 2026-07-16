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
