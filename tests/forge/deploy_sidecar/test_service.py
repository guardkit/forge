"""Tests for the forge-deploy-sidecar service (S1, C4 residue #24).

Covers every deny-by-default law (repo resolution, script allowlist, env
allowlist, timeout cap, loopback binding, no-shell/never-raises), the happy
path against a tmp repo + tmp profile + stub runner, output_tail capping, and
a live end-to-end HTTP round trip through the real subprocess core.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy_sidecar import service
from forge.deploy_sidecar.service import (
    HOST,
    OUTPUT_TAIL_CHARS,
    TIMEOUT_DEFAULT,
    TIMEOUT_MAX,
    build_server,
    process_run_request,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(paths: dict[str, str]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": paths},
        }
    )


def _write_profile(repo_path: Path, data: dict[str, Any]) -> None:
    deploy_dir = repo_path / "deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    (deploy_dir / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _minimal_profile() -> dict[str, Any]:
    return {"env_id": "staging", "compose": {"file": "compose.yaml", "script": "deploy.sh"}}


class _RecordingRunner:
    """A stub ScriptRunner that records its kwargs and returns a canned result."""

    def __init__(self, result: tuple[int, str] = (0, "ok")) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[int, str]:
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "api_test"
    r.mkdir()
    _write_profile(r, _minimal_profile())
    return r


# ---------------------------------------------------------------------------
# LAW 1 — repo resolves via planning.target_repo_paths
# ---------------------------------------------------------------------------


def test_repo_required(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    status, body = process_run_request(
        {"script": "deploy.sh"}, config=cfg, script_runner=_RecordingRunner()
    )
    assert status == 400
    assert "'repo' is required" in body["error"]


def test_unknown_repo_names_known_keys(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "acme/ghost", "script": "deploy.sh"}, config=cfg, script_runner=runner
    )
    assert status == 400
    assert "unknown target repo" in body["error"]
    assert "appmilla/api_test" in body["error"]  # names the known key
    assert runner.calls == []  # never executed


def test_missing_profile_refused(tmp_path: Path) -> None:
    empty = tmp_path / "noprofile"
    empty.mkdir()
    cfg = _config({"appmilla/api_test": str(empty)})
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=_RecordingRunner(),
    )
    assert status == 400
    assert "not deployable" in body["error"]


# ---------------------------------------------------------------------------
# LAW 2 — script allowlist (compose.script, health cmd, live_gate driver path)
# ---------------------------------------------------------------------------


def test_script_required(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    status, body = process_run_request(
        {"repo": "appmilla/api_test"}, config=cfg, script_runner=_RecordingRunner()
    )
    assert status == 400
    assert "'script' is required" in body["error"]


def test_unnamed_script_refused(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "rm-rf.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 400
    assert "deny by default" in body["error"]
    assert "deploy.sh" in body["error"]  # lists the runnable scripts
    assert runner.calls == []


def test_compose_script_allowed(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert body == {"exit_code": 0, "output_tail": "ok"}
    assert runner.calls[0]["script"] == "deploy.sh"
    assert runner.calls[0]["cwd"] == str(repo)  # cwd resolved to repo root


def test_health_check_cmd_allowed(repo: Path) -> None:
    _write_profile(
        repo,
        {**_minimal_profile(), "health_checks": [{"cmd": "qa/health.sh"}]},
    )
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {"repo": "appmilla/api_test", "script": "qa/health.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200


def test_live_gate_driver_script_path_allowed_but_not_interpreter(repo: Path) -> None:
    _write_profile(
        repo,
        {
            **_minimal_profile(),
            "live_gate": {"driver": ["python3", "qa/gates/local_live_gate.py"]},
        },
    )
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()

    ok, _ = process_run_request(
        {"repo": "appmilla/api_test", "script": "qa/gates/local_live_gate.py"},
        config=cfg,
        script_runner=runner,
    )
    assert ok == 200

    # The bare interpreter element is NOT a runnable script (a path element
    # exists, so only path-like driver tokens are allowlisted).
    refused, _ = process_run_request(
        {"repo": "appmilla/api_test", "script": "python3"},
        config=cfg,
        script_runner=runner,
    )
    assert refused == 400


# ---------------------------------------------------------------------------
# LAW 3 — env allowlist + string values
# ---------------------------------------------------------------------------


def test_base_env_keys_allowed_and_env_file_routed(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {
                "REVERT": "1",
                "ROLLBACK_IMAGE_REF": "api_test:rollback-1",
                "CANDIDATE": "1",
                "PROMOTE": "1",
                "ENV_FILE": "/run/secrets.env",
            },
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    call = runner.calls[0]
    # ENV_FILE routed to the dedicated param; the rest ride extra_env.
    assert call["env_file"] == "/run/secrets.env"
    assert "ENV_FILE" not in call["extra_env"]
    assert call["extra_env"]["REVERT"] == "1"
    assert call["extra_env"]["ROLLBACK_IMAGE_REF"] == "api_test:rollback-1"


def test_unlisted_env_key_refused(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "env": {"EVIL": "x"}},
        config=cfg,
        script_runner=runner,
    )
    assert status == 400
    assert "not allowlisted" in body["error"]
    assert runner.calls == []


def test_non_string_env_value_refused(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "env": {"REVERT": 1}},
        config=cfg,
        script_runner=_RecordingRunner(),
    )
    assert status == 400
    assert "must be a string" in body["error"]


def test_env_not_object_refused(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "env": ["REVERT=1"]},
        config=cfg,
        script_runner=_RecordingRunner(),
    )
    assert status == 400
    assert "'env' must be a JSON object" in body["error"]


def test_candidate_down_env_key_allowed(repo: Path) -> None:
    """Make-merge-work (2026-08-24): CANDIDATE_DOWN rides the BASE allowlist.

    The candidate-stack teardown env — without it every sidecar-surface run
    leaks the candidate stack on :8902 (the teardown env was refused 400).
    """
    assert "CANDIDATE_DOWN" in service.ENV_ALLOWLIST_BASE
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {"CANDIDATE_DOWN": "1"},
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert runner.calls[0]["extra_env"]["CANDIDATE_DOWN"] == "1"


SANDBOX_ENV_KEYS = (
    "SANDBOX_NAME",
    "SANDBOX_MEMORY",
    "SANDBOX_CPUS",
    "SANDBOX_PUBLISH",
    "SANDBOX_ALLOW_NETWORK",
)


def test_the_five_sandbox_env_keys_are_allowed(repo: Path) -> None:
    """Deploying into a Docker Sandbox (2026-09-06): the five settings get in.

    The deploy step puts them in every script step's environment; the sidecar
    is what actually runs those scripts, so if it refused them the whole
    arrangement would 400 at the first deploy. They name a sandbox and its
    size, ports and network rules — no secret, no new privilege.
    """
    for key in SANDBOX_ENV_KEYS:
        assert key in service.ENV_ALLOWLIST_BASE
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {
                "SANDBOX_NAME": "api-test-deploy",
                "SANDBOX_MEMORY": "6g",
                "SANDBOX_CPUS": "4",
                "SANDBOX_PUBLISH": "127.0.0.1:8901:8901,127.0.0.1:8902:8902",
                "SANDBOX_ALLOW_NETWORK": "pypi.org,*.debian.org",
                "CANDIDATE": "1",
            },
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    extra = runner.calls[0]["extra_env"]
    assert extra["SANDBOX_NAME"] == "api-test-deploy"
    assert extra["SANDBOX_PUBLISH"] == "127.0.0.1:8901:8901,127.0.0.1:8902:8902"
    assert extra["SANDBOX_ALLOW_NETWORK"] == "pypi.org,*.debian.org"
    assert extra["CANDIDATE"] == "1"


def test_the_five_keys_ride_the_base_list_not_the_profile(repo: Path) -> None:
    """They are allowed whatever the profile says — the profile that named the
    sandbox is on the forge side; the sidecar's own list is what admits them."""
    # This repo's profile carries no sandbox block at all.
    cfg = _config({"appmilla/api_test": str(repo)})
    status, _ = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {"SANDBOX_NAME": "api-test-deploy"},
        },
        config=cfg,
        script_runner=_RecordingRunner(),
    )
    assert status == 200


def test_a_near_miss_sandbox_key_is_still_refused(repo: Path) -> None:
    """The five names are the five names — nothing that merely looks like one."""
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {"SANDBOX_COMMAND": "rm -rf /"},
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 400
    assert "not allowlisted" in body["error"]
    assert runner.calls == []


def test_live_gate_env_key_allowed(repo: Path) -> None:
    _write_profile(
        repo,
        {
            **_minimal_profile(),
            "live_gate": {
                "driver": ["qa/gate.sh"],
                "env": {"BASE_URL": "http://localhost:9"},
            },
        },
    )
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {"BASE_URL": "http://localhost:8080"},
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert runner.calls[0]["extra_env"]["BASE_URL"] == "http://localhost:8080"


def test_candidate_env_key_allowed(repo: Path) -> None:
    # `candidate` is not a first-class profile field yet (S2) — it lands in
    # profile.extra and its env keys are still allowlisted, forward-compatibly.
    _write_profile(
        repo,
        {**_minimal_profile(), "candidate": {"env": {"CAND_PORT": "18080"}}},
    )
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {"CAND_PORT": "18080"},
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# LAW 4 — timeout cap
# ---------------------------------------------------------------------------


def test_timeout_default_when_absent(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert runner.calls[0]["timeout"] == TIMEOUT_DEFAULT


def test_timeout_capped_at_max(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "timeout_seconds": 999999},
        config=cfg,
        script_runner=runner,
    )
    assert runner.calls[0]["timeout"] == TIMEOUT_MAX


@pytest.mark.parametrize("bad", [0, -5, True, "60"])
def test_bad_timeout_refused(repo: Path, bad: Any) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "timeout_seconds": bad},
        config=cfg,
        script_runner=_RecordingRunner(),
    )
    assert status == 400
    assert "positive number" in body["error"]


# ---------------------------------------------------------------------------
# output_tail capping + script-verdict-is-data + never-raises
# ---------------------------------------------------------------------------


def test_output_tail_capped(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    big = "x" * (OUTPUT_TAIL_CHARS + 10_000)
    runner = _RecordingRunner(result=(0, big))
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    tail = body["output_tail"]
    assert len(tail) <= OUTPUT_TAIL_CHARS + len(service._TAIL_MARKER)
    assert tail.startswith(service._TAIL_MARKER)
    assert tail.endswith("x")


def test_script_nonzero_exit_is_data_not_http_error(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner(result=(7, "boom"))
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200  # transport success
    assert body["exit_code"] == 7  # the script's verdict is carried as data


def test_never_raises_on_runner_exception(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})

    def boom(**_: Any) -> tuple[int, str]:
        raise RuntimeError("kaboom")

    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=boom,
    )
    assert status == 500
    assert "kaboom" in body["error"]
    assert body["exit_code"] == 1


def test_non_dict_payload_refused(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    status, body = process_run_request(
        ["not", "a", "dict"], config=cfg, script_runner=_RecordingRunner()
    )
    assert status == 400
    assert "must be a JSON object" in body["error"]


# ---------------------------------------------------------------------------
# LAW 5 — loopback binding + healthz + live end-to-end
# ---------------------------------------------------------------------------


def test_host_constant_is_loopback() -> None:
    assert HOST == "127.0.0.1"


def test_server_binds_loopback_only() -> None:
    server = build_server(port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def _serve_in_thread(server: Any) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def test_healthz_endpoint(repo: Path) -> None:
    server = build_server(port=0, config_loader=lambda: _config({}))
    _serve_in_thread(server)
    try:
        host, port = server.server_address[:2]
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as r:
            body = json.loads(r.read().decode("utf-8"))
        assert body["status"] == "healthy"
        assert body["rev"] == service.SIDECAR_CODE_VERSION
    finally:
        server.shutdown()
        server.server_close()


def test_run_endpoint_end_to_end_real_subprocess(repo: Path) -> None:
    # A real executable script named by the profile, run through the DEFAULT
    # runner (the real _run_script_step subprocess core) over the wire.
    script = repo / "deploy.sh"
    script.write_text("#!/bin/sh\necho hello-from-deploy\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    server = build_server(
        port=0, config_loader=lambda: _config({"appmilla/api_test": str(repo)})
    )
    _serve_in_thread(server)
    try:
        host, port = server.server_address[:2]
        payload = json.dumps(
            {"repo": "appmilla/api_test", "script": "deploy.sh"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}/run",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
        assert body["exit_code"] == 0
        assert "hello-from-deploy" in body["output_tail"]
    finally:
        server.shutdown()
        server.server_close()


def test_run_endpoint_refusal_is_http_400(repo: Path) -> None:
    import urllib.error

    server = build_server(
        port=0, config_loader=lambda: _config({"appmilla/api_test": str(repo)})
    )
    _serve_in_thread(server)
    try:
        host, port = server.server_address[:2]
        payload = json.dumps(
            {"repo": "appmilla/api_test", "script": "evil.sh"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}/run",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=10)
        assert exc_info.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
