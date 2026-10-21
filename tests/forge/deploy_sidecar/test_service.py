"""Tests for the forge-deploy-sidecar service (S1, C4 residue #24).

Covers every deny-by-default law (repo resolution, script allowlist, env
allowlist, timeout cap, loopback binding, no-shell/never-raises), the happy
path against a tmp repo + tmp profile + stub runner, output_tail capping, and
a live end-to-end HTTP round trip through the real subprocess core.
"""

from __future__ import annotations

import json
import io
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy.sidecar_runner import SidecarScriptRunner, no_answer_came_back
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
    # AND COMMITTED. The environment door is widened by what a project has
    # COMMITTED, so a profile these tests only wrote to disk declares nothing.
    if (repo_path / ".git").exists():
        _committed(repo_path)


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
    return _committed(r)


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
    # The answer says where the script ran (LAW 8's other half).
    assert body == {"exit_code": 0, "output_tail": "ok", "cwd": str(repo)}
    assert runner.calls[0]["script"] == "deploy.sh"
    assert runner.calls[0]["cwd"] == str(repo)  # cwd resolved to repo root


def test_a_script_stopped_at_its_time_limit_says_so(repo: Path) -> None:
    """15 October 2026, Codex R2: the caller counts it like no answer."""
    from forge.executor.shell_steps import TimedOut

    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner(result=TimedOut(124, "stopped"))
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert body == {
        "exit_code": 124,
        "output_tail": "stopped",
        "cwd": str(repo),
        "timed_out": True,
    }


def test_a_script_whose_run_raised_is_a_5xx(repo: Path) -> None:
    """15 October 2026, Codex R9: the run raised after the script may have
    started, so the answer is a 5xx, which every caller reads as no answer."""
    from forge.executor.shell_steps import RaisedWhileRunning

    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner(
        result=RaisedWhileRunning(1, "Unexpected error running script: OSError: x")
    )
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 500
    assert "Unexpected error running script" in body["error"]


@pytest.mark.parametrize("how", ["ENOEXEC", "ENOTDIR"])
def test_a_script_the_system_would_not_start_is_a_4xx(repo: Path, how: str) -> None:
    """15 October 2026, Codex R11, through the REAL script runner: the system
    refused to start the script (an exec format error, or a path through a
    file), so nothing ran — a 4xx in the sidecar's own words, an answer."""
    if how == "ENOEXEC":
        script = "deploy.sh"
        (repo / "deploy.sh").write_text("this is not a program\n", encoding="utf-8")
        (repo / "deploy.sh").chmod(0o755)
    else:
        script = "notadir/deploy.sh"
        (repo / "notadir").write_text("a file, not a folder\n", encoding="utf-8")
        _write_profile(
            repo,
            {"env_id": "staging", "compose": {"file": "compose.yaml", "script": script}},
        )
    cfg = _config({"appmilla/api_test": str(repo)})
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": script}, config=cfg
    )
    assert status == 400, body
    assert body["error"]


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
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {"EVIL": "x"},
            # A NAME OUTSIDE THE FACTORY'S OWN LIST asks this project to
            # widen its door, so the request says whose run it is.
            "by_hand": True,
        },
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


#: The two settings for a factory-carrying sandbox that a request may name:
#: the ports it publishes for the factory's own two services. The other four
#: of that group are deliberately refused — see the test below them.
FACTORY_SANDBOX_PORT_KEYS = (
    "SANDBOX_SIDECAR_PUBLISH",
    "SANDBOX_RUNNER_PUBLISH",
)

#: The four settings of that group a request may NOT name: each one points at
#: somewhere on this box that a sandbox being created would take its
#: environment from, or mount.
FACTORY_SANDBOX_PATH_KEYS = (
    "SANDBOX_ENV_FILE",
    "SANDBOX_FORGE_PATH",
    "SANDBOX_GUARDKIT_PATH",
    "SANDBOX_RECEIPTS_PATH",
)


def test_the_factory_sandbox_port_keys_are_allowed(repo: Path) -> None:
    """The two port settings for a factory-carrying sandbox get in too.

    They were added to the deploy stage on 2026-09-07/08 and not here, so the
    first real merge press was refused at its first step: "env key
    'SANDBOX_SIDECAR_PUBLISH' is not allowlisted". They name the two ports the
    sandbox publishes for the factory's services, and neither carries a secret.

    They do grant something new, and the allowlist says so beside them: in the
    wrapper, setting BOTH is exactly what switches on the branch that creates a
    sandbox carrying the factory and starts the build runner, so a request
    naming a sandbox that does not exist yet can now cause one to be created.
    That is accepted deliberately — it is the key the first merge press was
    refused for — and bounded by the four keys refused below, which stop a
    request choosing what such a sandbox reads or mounts.
    """
    for key in FACTORY_SANDBOX_PORT_KEYS:
        assert key in service.ENV_ALLOWLIST_BASE
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {
                "SANDBOX_NAME": "api-test-deploy",
                "SANDBOX_SIDECAR_PUBLISH": "127.0.0.1:8925:8125",
                "SANDBOX_RUNNER_PUBLISH": "127.0.0.1:8924:8124",
                "CANDIDATE_DOWN": "1",
            },
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    extra = runner.calls[0]["extra_env"]
    assert extra["SANDBOX_SIDECAR_PUBLISH"] == "127.0.0.1:8925:8125"
    assert extra["SANDBOX_RUNNER_PUBLISH"] == "127.0.0.1:8924:8124"
    assert extra["CANDIDATE_DOWN"] == "1"


@pytest.mark.parametrize("key", FACTORY_SANDBOX_PATH_KEYS)
def test_the_sandbox_creation_paths_are_deliberately_refused(
    repo: Path, key: str
) -> None:
    """A request may not choose which folder or file a new sandbox gets.

    These four are read by the host wrapper only where it CREATES a sandbox:
    the env file becomes that sandbox's whole environment (it is the
    sops-rendered file of secrets), the forge and guardkit folders are mounted
    into it read-only — and forge's parent folder decides three more mounts —
    and the receipts folder is mounted read-write, the one writable mount the
    wrapper makes. This service checks key names and never values, and the
    sandbox's name has always been the caller's to choose, so a request naming
    a sandbox that does not exist yet takes the wrapper's "create it" branch.
    Allowing these keys would therefore let one request decide what a new
    sandbox mounts and reads before it runs any code — a widening of what a
    message can do. Nothing sends them: a deploy that runs inside the sandbox
    is sent none of the sandbox's creation settings, and creating a
    factory-carrying sandbox is an attended host-side command. A repository
    that one day needs them gets them by a decision, not by accident.
    """
    assert key not in service.ENV_ALLOWLIST_BASE
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {key: "/run/user/1000/anything"},
            "by_hand": True,
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 400
    assert "not allowlisted" in body["error"]
    assert runner.calls == []


def test_a_near_miss_sandbox_key_is_still_refused(repo: Path) -> None:
    """The five names are the five names — nothing that merely looks like one."""
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "env": {"SANDBOX_COMMAND": "rm -rf /"},
            "by_hand": True,
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
            "by_hand": True,
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
            "by_hand": True,
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


# ---------------------------------------------------------------------------
# LAW 8 — a working directory is honoured only for a candidate tree
# (protect-main, 2026-09-07)
# ---------------------------------------------------------------------------


def _candidate_tree(repo: Path, feature: str = "FEAT-8A01") -> Path:
    tree = repo / ".forge-candidates" / feature
    tree.mkdir(parents=True)
    return tree


def test_a_candidate_tree_is_the_working_directory(repo: Path) -> None:
    tree = _candidate_tree(repo)
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "cwd": str(tree)},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200, body
    assert runner.calls[0]["cwd"] == str(tree.resolve())
    # The script is still the profile's name — found relative to the tree,
    # so the tree's own copy runs.
    assert runner.calls[0]["script"] == "deploy.sh"
    # And the answer says so, which is how the client tells a sidecar that
    # honoured the tree from one running old code that silently ran main.
    assert body["cwd"] == str(tree.resolve())


def test_the_answer_says_where_the_script_ran(repo: Path) -> None:
    """Every permitted run answers with its working directory: the profile's
    own for an ordinary run, the candidate tree when one was honoured."""
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "cwd": "/ignored-by-sidecar"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert body["cwd"] == str(repo)
    tree = _candidate_tree(repo)
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "cwd": str(tree)},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert body["cwd"] == str(tree.resolve())
    assert body["cwd"] == runner.calls[-1]["cwd"]


def test_a_relative_candidate_tree_resolves_under_the_repo(repo: Path) -> None:
    tree = _candidate_tree(repo)
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "cwd": ".forge-candidates/FEAT-8A01"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert runner.calls[0]["cwd"] == str(tree.resolve())


def test_a_candidate_tree_that_does_not_exist_is_refused(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "cwd": str(repo / ".forge-candidates" / "FEAT-GONE"),
        },
        config=cfg,
        script_runner=runner,
    )
    assert status == 400
    assert "is not a candidate tree" in body["error"]
    assert runner.calls == []


def test_a_path_deeper_than_a_candidate_tree_is_refused(repo: Path) -> None:
    tree = _candidate_tree(repo)
    deeper = tree / "deploy"
    deeper.mkdir()
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "cwd": str(deeper)},
        config=cfg,
        script_runner=runner,
    )
    assert status == 400
    assert "is not a candidate tree" in body["error"]
    assert runner.calls == []


def test_the_trees_root_itself_is_refused(repo: Path) -> None:
    _candidate_tree(repo)
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "cwd": str(repo / ".forge-candidates")},
        config=cfg,
        script_runner=runner,
    )
    # The root is not under itself: it is ignored like any other path.
    assert status == 200
    assert runner.calls[0]["cwd"] == str(repo)


def test_a_dot_dot_cannot_walk_out_of_the_trees(repo: Path, tmp_path: Path) -> None:
    _candidate_tree(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {
            "repo": "appmilla/api_test",
            "script": "deploy.sh",
            "cwd": str(repo / ".forge-candidates" / "FEAT-8A01" / ".." / ".." / ".." / "outside"),
        },
        config=cfg,
        script_runner=runner,
    )
    # Resolved, it is outside the trees: ignored, the profile's cwd stands.
    assert status == 200
    assert runner.calls[0]["cwd"] == str(repo)


def test_any_other_working_directory_is_ignored_as_before(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh", "cwd": "/ignored-by-sidecar"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert runner.calls[0]["cwd"] == str(repo)


def test_no_working_directory_is_the_profile_s(repo: Path) -> None:
    cfg = _config({"appmilla/api_test": str(repo)})
    runner = _RecordingRunner()
    status, _ = process_run_request(
        {"repo": "appmilla/api_test", "script": "deploy.sh"},
        config=cfg,
        script_runner=runner,
    )
    assert status == 200
    assert runner.calls[0]["cwd"] == str(repo)


def _profile_reference(cwd: Path) -> dict[str, Any]:
    return {
        "selection": {
            "version": 1,
            "repo": "example/service",
            "build": "build-one",
            "offer_id": "offer-one",
            "decision_id": "decision-one",
            "candidate_commit": "a" * 40,
            "candidate_tree": "b" * 40,
            "start_commit": "c" * 40,
            "target_commit": "c" * 40,
            "joined_commit": "d" * 40,
            "joined_tree": "e" * 40,
            "profile_sha256": "f" * 64,
            "attempt": 1,
            "candidate_cwd": str(cwd),
        },
        "turn": 2,
    }


@pytest.mark.parametrize("parent_kind", ["absent", "tree", "link", "blob", "submodule"])
def test_committed_profile_absence_requires_an_ordinary_parent(
    repo: Path, parent_kind: str,
) -> None:
    import shutil
    import subprocess

    from forge.deploy.candidate_tree import read_file_at_commit_sync
    from forge.deploy.profile_selection import validate_committed_profile_path

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True,
            text=True, check=True,
        ).stdout.strip()

    old_head = git("rev-parse", "HEAD")
    shutil.rmtree(repo / "deploy")
    if parent_kind == "tree":
        (repo / "deploy").mkdir()
        (repo / "deploy" / "other").write_text("ordinary directory\n")
    elif parent_kind == "link":
        (repo / "deploy").symlink_to("missing-directory", target_is_directory=True)
    elif parent_kind == "blob":
        (repo / "deploy").write_text("not a directory\n")
    git("add", "-A")
    if parent_kind == "submodule":
        git("update-index", "--add", "--cacheinfo", "160000", old_head, "deploy")
    git("-c", "user.name=Factory Test", "-c", "user.email=factory@example.invalid",
        "commit", "-m", "Declare the profile parent")
    profile = read_file_at_commit_sync(repo, "HEAD", "deploy/profile.yaml", raw=True)
    parent = read_file_at_commit_sync(repo, "HEAD", "deploy", raw=True)
    assert not profile.found and profile.mode is None
    if parent_kind in ("absent", "tree"):
        validate_committed_profile_path(profile, parent)
    else:
        with pytest.raises(ValueError, match="ordinary committed parent"):
            validate_committed_profile_path(profile, parent)


def test_first_profile_authority_refusal_echoes_requested_reference_before_execution(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = repo / ".forge-candidates" / "selection"
    archive.mkdir(parents=True)
    reference = _profile_reference(archive)
    monkeypatch.setattr(
        service, "coordinator_recorded_build",
        lambda: lambda _build: {
            "build": "build-one",
            service.RECORDED_START_KEY: "c" * 40,
            "first_deployment_profile": {**reference, "turn": 1},
        },
    )
    runner = _RecordingRunner()
    status, body = process_run_request(
        {
            "repo": "example/service",
            "build": "build-one",
            "declared_at": "c" * 40,
            "script": "deploy.sh",
            "cwd": str(archive),
            "helper_instance": service.HELPER_INSTANCE,
            "first_deployment_profile": reference,
        },
        config=_config({"example/service": str(repo)}),
        script_runner=runner,
        inside_sandbox=True,
    )
    assert status == 400
    assert body["requested_first_deployment_profile"] == reference
    assert body["helper_instance"] == service.HELPER_INSTANCE
    assert body["execution_started"] is False
    assert "first_deployment_profile" not in body
    assert runner.calls == []


@pytest.mark.parametrize("authority", ["missing_layout", "unavailable", "wrong_start", "unstamped", "by_hand"])
def test_missing_clone_profile_base_env_still_requires_current_layout(
    repo: Path, monkeypatch: pytest.MonkeyPatch, authority: str,
) -> None:
    import subprocess

    start = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    (repo / "deploy" / "profile.yaml").unlink()
    archive = repo / ".forge-candidates" / "selection"
    archive.mkdir(parents=True)
    asked: list[str] = []

    def _answer(build: str) -> dict[str, Any]:
        asked.append(build)
        if authority == "unavailable":
            raise OSError("record unavailable")
        if authority == "wrong_start":
            return {
                "build": build, service.RECORDED_START_KEY: "a" * 40,
                "deployment_layout": {
                    "repo": "example/service", "build": build, "start_commit": start,
                    "candidate_cwd": str(archive), "attempt": 1, "turn": 1,
                },
            }
        return {"build": build, service.RECORDED_START_KEY: start}

    monkeypatch.setattr(service, "coordinator_recorded_build", lambda: _answer)
    runner = _RecordingRunner()
    status, body = process_run_request(
        {
            "repo": "example/service",
            **({"build": "build-one"} if authority != "unstamped" else {}),
            **({"by_hand": True} if authority == "by_hand" else {}),
            "declared_at": start,
            "script": "deploy.sh",
            "env": {},
            "cwd": str(archive),
        },
        config=_config({"example/service": str(repo)}),
        script_runner=runner,
    )
    assert status == 400
    assert body["error"]
    assert asked == ([] if authority in ("unstamped", "by_hand") else ["build-one"])
    assert runner.calls == []


def test_script_client_probes_each_selected_command_and_requires_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _profile_reference(tmp_path / ".forge-candidates" / "selection")
    sent: list[str] = []

    class _Response:
        def __init__(self, body: dict[str, Any]) -> None:
            self._body = body

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._body).encode()

    def _old_helper(request: Any, *, timeout: float) -> _Response:
        sent.append(request.full_url)
        return _Response({"status": "healthy", "instance": "one"})

    monkeypatch.setattr(urllib.request, "urlopen", _old_helper)
    client = SidecarScriptRunner(
        base_url="http://127.0.0.1:1", repo="example/service",
        first_deployment_profile=reference,
    )
    refusal = client(cwd=reference["selection"]["candidate_cwd"], script="deploy.sh", env_file=None)
    assert refusal[0] != 0 and not no_answer_came_back(refusal)
    assert sent == ["http://127.0.0.1:1/healthz"]

    sent.clear()

    def _missing_echo(request: Any, *, timeout: float) -> _Response:
        sent.append(request.full_url)
        if request.full_url.endswith("/healthz"):
            return _Response({
                "status": "healthy", "instance": "one",
                "capabilities": ["first_deployment_profile_v1"],
            })
        return _Response({"exit_code": 0, "output_tail": "ok", "cwd": reference["selection"]["candidate_cwd"]})

    monkeypatch.setattr(urllib.request, "urlopen", _missing_echo)
    uncertain = client(cwd=reference["selection"]["candidate_cwd"], script="deploy.sh", env_file=None)
    assert no_answer_came_back(uncertain)
    assert sent == ["http://127.0.0.1:1/healthz", "http://127.0.0.1:1/run"]


@pytest.mark.parametrize("client_kind", ["script", "live_gate"])
@pytest.mark.parametrize("reply_kind", [
    "malformed_health", "unavailable_health", "missing_capability",
    "changed_instance", "changed_selection",
])
def test_selected_clients_bind_the_probe_to_the_success_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client_kind: str, reply_kind: str,
) -> None:
    from forge.deploy.live_gate import SidecarLiveGateInvoker

    reference = _profile_reference(tmp_path / ".forge-candidates" / "selection")
    sent: list[str] = []

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def read(self) -> bytes:
            return self.body

    def respond(request: Any, *, timeout: float) -> Response:
        sent.append(request.full_url)
        if request.full_url.endswith("/healthz"):
            if reply_kind == "unavailable_health":
                raise urllib.error.URLError("helper unavailable")
            if reply_kind == "malformed_health":
                return Response(b"not json")
            return Response(json.dumps({
                "status": "healthy", "instance": "one",
                "capabilities": [] if reply_kind == "missing_capability" else ["first_deployment_profile_v1"],
            }).encode())
        return Response(json.dumps({
            "exit_code": 0, "output_tail": "ok", "stdout": "{}", "stderr_tail": "",
            "cwd": reference["selection"]["candidate_cwd"],
            "helper_instance": "replacement" if reply_kind == "changed_instance" else "one",
            "first_deployment_profile": {**reference, "turn": 99} if reply_kind == "changed_selection" else reference,
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    before_post = reply_kind in ("malformed_health", "unavailable_health", "missing_capability")
    if client_kind == "script":
        client = SidecarScriptRunner(
            base_url="http://127.0.0.1:1", repo="example/service",
            first_deployment_profile=reference,
        )
        answer = client(cwd=reference["selection"]["candidate_cwd"], script="deploy.sh", env_file=None)
        assert answer[0] != 0
        assert no_answer_came_back(answer) is not before_post
    else:
        gate = SidecarLiveGateInvoker(
            base_url="http://127.0.0.1:1", repo="example/service",
            repo_path=Path(reference["selection"]["candidate_cwd"]),
            driver_argv=["node", "gate.mjs"], first_deployment_profile=reference,
        )
        answer = gate.invoke(feature="generic", target="candidate")
        assert answer.confirmed is before_post
        assert answer.answer_lost is not before_post
    assert sent == ["http://127.0.0.1:1/healthz"] + ([] if before_post else ["http://127.0.0.1:1/run"])


@pytest.mark.parametrize("client_kind", ["script", "live_gate"])
@pytest.mark.parametrize("reply_kind", [
    "exact", "missing_instance", "changed_reference", "execution_started", "server_error", "malformed",
])
def test_selected_clients_accept_only_attested_preexecution_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client_kind: str, reply_kind: str,
) -> None:
    from forge.deploy.live_gate import SidecarLiveGateInvoker

    reference = _profile_reference(tmp_path / ".forge-candidates" / "selection")

    class _Health:
        def __enter__(self) -> "_Health":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({
                "status": "healthy", "instance": "one",
                "capabilities": ["first_deployment_profile_v1"],
            }).encode()

    def _refusal(request: Any, *, timeout: float) -> _Health:
        if request.full_url.endswith("/healthz"):
            return _Health()
        body = {
            "error": "the current turn changed",
            "helper_instance": "one",
            "requested_first_deployment_profile": reference,
            "execution_started": False,
        }
        if reply_kind == "missing_instance":
            body.pop("helper_instance")
        elif reply_kind == "changed_reference":
            body["requested_first_deployment_profile"] = {**reference, "turn": 99}
        elif reply_kind == "execution_started":
            body["execution_started"] = True
        encoded = b"not json" if reply_kind == "malformed" else json.dumps(body).encode()
        raise urllib.error.HTTPError(
            request.full_url, 500 if reply_kind == "server_error" else 400,
            "refused", {}, io.BytesIO(encoded),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _refusal)
    if client_kind == "script":
        client = SidecarScriptRunner(
            base_url="http://127.0.0.1:1", repo="example/service",
            first_deployment_profile=reference,
        )
        answer = client(cwd=reference["selection"]["candidate_cwd"], script="deploy.sh", env_file=None)
        assert answer[0] != 0
        assert no_answer_came_back(answer) is (reply_kind != "exact")
    else:
        gate = SidecarLiveGateInvoker(
            base_url="http://127.0.0.1:1", repo="example/service",
            repo_path=Path(reference["selection"]["candidate_cwd"]),
            driver_argv=["node", "gate.mjs"], first_deployment_profile=reference,
        )
        answer = gate.invoke(feature="generic", target="candidate")
        assert answer.confirmed is (reply_kind == "exact")
        assert answer.answer_lost is (reply_kind != "exact")


@pytest.mark.parametrize("driver", [
    ["node", "gate.mjs"], ["ruby", "gate.rb"],
    ["/usr/bin/node", "gate.mjs"], ["node", "./gate.mjs"],
])
def test_driver_sources_follow_git_files_not_language_suffixes(repo: Path, driver: list[str]) -> None:
    import subprocess

    source = Path(driver[-1]).as_posix()
    (repo / source).write_text("a committed driver\n", encoding="utf-8")
    subprocess.run(["git", "add", source], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "driver"], cwd=repo, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    archive = repo / ".forge-candidates" / "selection"
    archive.mkdir(parents=True)
    (archive / source).write_text("a committed driver\n", encoding="utf-8")
    sources, refusal = service._driver_archive_sources(repo, archive, commit, driver)
    assert refusal is None
    assert sources == [driver[-1]]
    assert service._same_archive_source(repo, archive, commit, sources[0]) is None
    (archive / source).write_text("substituted driver\n", encoding="utf-8")
    assert "differs" in service._same_archive_source(repo, archive, commit, sources[0])


def test_inline_driver_remains_bound_to_its_profile(repo: Path) -> None:
    import subprocess

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    sources, refusal = service._driver_archive_sources(
        repo, repo, commit, ["node", "-e", "console.log('ok')"]
    )
    assert sources == [] and refusal is None


@pytest.mark.parametrize("where", ["profile", "missing-parent", "existing-parent"])
def test_clone_profile_symlinks_never_count_as_absence(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str,
) -> None:
    profile = repo / "deploy" / "profile.yaml"
    profile.unlink()
    if where == "profile":
        profile.symlink_to(tmp_path / "missing-profile")
    else:
        profile.parent.rmdir()
        destination = tmp_path / "other-deploy"
        if where == "existing-parent":
            destination.mkdir()
        profile.parent.symlink_to(destination, target_is_directory=True)
    monkeypatch.setattr(service, "coordinator_recorded_build", lambda: pytest.fail("symlink reached authority lookup"))
    runner = _RecordingRunner()
    status, body = process_run_request(
        {"repo": "example/service", "build": "build-one", "script": "deploy.sh"},
        config=_config({"example/service": str(repo)}), script_runner=runner,
    )
    assert status == 400 and "symbolic link" in body["error"]
    assert runner.calls == []


@pytest.mark.parametrize("shape", ["absolute-executable", "option-source", "clone-executable"])
def test_driver_source_classification_cannot_skip_project_executables(repo: Path, shape: str) -> None:
    import subprocess

    source = repo / "gate"
    source.write_text("committed source\n", encoding="utf-8")
    subprocess.run(["git", "add", "gate"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "driver"], cwd=repo, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    archive = repo / ".forge-candidates" / "selection"
    archive.mkdir(parents=True)
    (archive / "gate").write_text("substituted source\n", encoding="utf-8")
    argv = {
        "absolute-executable": [str(archive / "gate")],
        "option-source": ["node", "--require=./gate", "-e", "0"],
        "clone-executable": [str(repo / "gate")],
    }[shape]
    sources, refusal = service._driver_archive_sources(repo, archive, commit, argv)
    if shape == "clone-executable":
        assert refusal and "outside" in refusal
    else:
        assert refusal is None and len(sources) == 1
        assert "differs" in service._same_archive_source(repo, archive, commit, sources[0])
