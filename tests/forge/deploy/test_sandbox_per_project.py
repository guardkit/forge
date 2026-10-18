"""One sandbox per project, stage S1: the recipe and the supervisor's new step.

Design: one-sandbox-per-project (17 October 2026), sections 2.1 and 2.2.

* ``deploy_templates/sandbox-deploy.sh`` with ``SANDBOX_SLOT`` makes a
  project's own sandbox: its own clone, no factory source mounts, the slot's
  ports, 4 GiB / 2 processors by default, its own receipts folder mounted at
  runtime, and network rules naming only its own two doors and the three
  shared services on the gateway (never the bus or the model router).
* Without ``SANDBOX_SLOT`` nothing changes (the other wrapper tests hold that).
* ``deploy/compose/sandbox-runner/run.sh`` mounts the receipts folder again
  and refuses to start when the release image is missing from the sandbox's
  own engine, and never imports it.

Fake ``sbx``, ``systemctl`` and ``docker`` programs record every call. No real
sandbox, unit, daemon or engine is touched.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

import forge.cli.deploy_templates as templates

TEMPLATES = Path(templates.__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
RUN_SH = ROOT / "deploy" / "compose" / "sandbox-runner" / "run.sh"
COMPOSE = ROOT / "deploy" / "compose" / "compose.sandbox-runner.yaml"

GATEWAY = "192.0.2.10"
PUBLISH = "198.51.100.2"

FAKE_SBX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SBX_LOG"
# The disk sizes sbx reads from its own settings at creation.
[ "$1" = create ] && printf 'docker=%s workspace=%s\\n' \\
  "${DOCKER_SANDBOXES_DOCKER_SIZE-unset}" "${DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE-unset}" >> "$SBX_LOG.disks"
case "$1" in
  ls) printf '%s\\n' "${SBX_LS:-}" ;;
  policy) [ "$2 $3" = "check network" ] && exit 1 ;;
  mount) touch "$SBX_STATE/mounted"; exit "${SBX_MOUNT_STATUS:-0}" ;;
  exec)
    case "$3" in
      mountpoint)
        [ -e "$SBX_STATE/mounted" ] && [ -z "${SBX_MOUNT_NEVER_SHOWS:-}" ] && exit 0
        exit 1 ;;
    esac
    exit "${SBX_EXEC_STATUS:-0}" ;;
esac
exit 0
"""

FAKE_RECORDER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$OTHER_LOG"
exit 0
"""


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


@pytest.fixture
def slot_repo(tmp_path: Path):
    repo = tmp_path / "estate" / "a-private-project"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(TEMPLATES / "sandbox-deploy.sh", repo / "deploy" / "sandbox-deploy.sh")
    (repo / "deploy" / "sandbox-deploy.sh").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "sbx").write_text(FAKE_SBX)
    (fake_bin / "systemctl").write_text(FAKE_RECORDER)
    (fake_bin / "docker").write_text(FAKE_RECORDER)
    for name in ("sbx", "systemctl", "docker"):
        (fake_bin / name).chmod(0o755)
    receipts = tmp_path / "receipts-a-private-project"
    receipts.mkdir()
    state = tmp_path / "sbx-state"
    state.mkdir()
    return repo, fake_bin, receipts, state


def _drive(slot_repo, tmp_path: Path, *args: str, **env: str):
    repo, fake_bin, receipts, state = slot_repo
    run_env = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SBX_LOG": str(tmp_path / "sbx.log"),
        "OTHER_LOG": str(tmp_path / "other.log"),
        "SBX_STATE": str(state),
        "SBX_LS": "",
        "SANDBOX_NAME": "a-private-project",
        "SANDBOX_SLOT": "1",
        "SANDBOX_PUBLISH_ADDRESS": PUBLISH,
        "SANDBOX_GATEWAY_ADDRESS": GATEWAY,
        "SANDBOX_RECEIPTS_PATH": str(receipts),
        "SANDBOX_ALLOW_NETWORK": "deb.debian.org,pypi.org,github.com:443",
    }
    run_env.update(env)
    result = subprocess.run(
        [str(repo / "deploy" / "sandbox-deploy.sh"), *args],
        cwd=repo, env=run_env, capture_output=True, text=True,
    )
    return result, _lines(tmp_path / "sbx.log"), _lines(tmp_path / "other.log")


class TestAProjectsOwnSandbox:
    def test_it_is_made_on_its_own_clone_with_no_factory_mounts_and_its_slot_ports(self, slot_repo, tmp_path):
        repo, _, receipts, _ = slot_repo
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare")
        assert result.returncode == 0, result.stdout + result.stderr
        created = [line for line in sbx if line.startswith("create ")]
        assert created == [
            f"create shell {repo} --name a-private-project --clone --memory 4g --cpus 2 "
            f"-p {PUBLISH}:8935:8125 -p {PUBLISH}:8934:8124 -p {PUBLISH}:8928:9418 "
            "-p 127.0.0.1:8960:8960 -p 127.0.0.1:8961:8961 "
            "--env SANDBOX_RECEIPTS_PATH"
        ]
        assert ":ro" not in created[0]
        assert str(receipts) not in created[0]

    def test_slot_nineteen_is_the_last_and_its_doors_meet_no_other_slot(self, slot_repo, tmp_path):
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare", SANDBOX_SLOT="19")
        assert result.returncode == 0, result.stdout + result.stderr
        allowed = [line for line in sbx if line.startswith("policy allow ")][0]
        assert f"{GATEWAY}:31849" in allowed and f"{GATEWAY}:31869" in allowed
        git_doors = {31830 + k for k in range(1, 20)}
        memory_doors = {31850 + k for k in range(1, 20)}
        assert not git_doors & memory_doors

    def test_slot_two_is_ten_ports_further_on(self, slot_repo, tmp_path):
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare", SANDBOX_SLOT="2")
        assert result.returncode == 0, result.stdout + result.stderr
        created = [line for line in sbx if line.startswith("create ")][0]
        for port in (f"{PUBLISH}:8945:8125", f"{PUBLISH}:8944:8124", f"{PUBLISH}:8938:9418",
                     "127.0.0.1:8970:8970", "127.0.0.1:8971:8971"):
            assert f"-p {port}" in created
        allowed = [line for line in sbx if line.startswith("policy allow ")][0]
        assert f"{GATEWAY}:31832" in allowed and f"{GATEWAY}:31852" in allowed

    def test_the_size_is_a_setting(self, slot_repo, tmp_path):
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare", SANDBOX_MEMORY="6g", SANDBOX_CPUS="3")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "--memory 6g --cpus 3" in [line for line in sbx if line.startswith("create ")][0]

    def test_its_disks_are_made_big_enough_for_real_builds_by_default(self, slot_repo, tmp_path):
        """S1 window, 18 October 2026: sbx's default Docker disk left 2.1 GB
        free. A slot sandbox is created with a 40g Docker disk and a 60g
        workspace disk unless its settings say otherwise."""
        result, _, _ = _drive(slot_repo, tmp_path, "prepare")
        assert result.returncode == 0, result.stdout + result.stderr
        assert _lines(tmp_path / "sbx.log.disks") == ["docker=40g workspace=60g"]
        assert "a 40g Docker disk and a 60g workspace disk" in result.stdout + result.stderr

    def test_its_disk_sizes_are_settings(self, slot_repo, tmp_path):
        result, _, _ = _drive(slot_repo, tmp_path, "prepare",
                              SANDBOX_DOCKER_DISK="80g", SANDBOX_WORKSPACE_DISK="2048m",
                              DOCKER_SANDBOXES_DOCKER_SIZE="1g")
        assert result.returncode == 0, result.stdout + result.stderr
        # The script's own value wins over one already in the environment.
        assert _lines(tmp_path / "sbx.log.disks") == ["docker=80g workspace=2048m"]

    @pytest.mark.parametrize("setting, value", [
        ("SANDBOX_DOCKER_DISK", "40"), ("SANDBOX_DOCKER_DISK", "40gb"), ("SANDBOX_DOCKER_DISK", "-1g"),
        ("SANDBOX_DOCKER_DISK", "511m"), ("SANDBOX_DOCKER_DISK", "0g"),
        ("SANDBOX_WORKSPACE_DISK", "0m"), ("SANDBOX_WORKSPACE_DISK", "1.5g"),
        ("SANDBOX_WORKSPACE_DISK", "60g;x"),
    ])
    def test_a_disk_size_that_is_not_one_is_refused_before_anything_is_made(self, slot_repo, tmp_path, setting, value):
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare", **{setting: value})
        assert result.returncode == 2
        assert setting in result.stdout + result.stderr
        assert not [line for line in sbx if line.startswith(("create ", "policy allow "))]

    def test_the_shared_shape_is_created_as_before_with_no_disk_sizes(self, slot_repo, tmp_path):
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare", SANDBOX_SLOT="",
                                SANDBOX_NAME="a-shared-shape", SANDBOX_DOCKER_DISK="7g")
        assert result.returncode == 0, result.stdout + result.stderr
        assert _lines(tmp_path / "sbx.log.disks") == ["docker=unset workspace=unset"]

    def test_the_rules_name_only_its_own_doors_and_the_shared_services(self, slot_repo, tmp_path):
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare")
        assert result.returncode == 0, result.stdout + result.stderr
        allowed = [line for line in sbx if line.startswith("policy allow ")]
        assert allowed == [
            "policy allow network --sandbox a-private-project "
            "deb.debian.org,pypi.org,github.com:443,"
            f"{GATEWAY}:31831,{GATEWAY}:31851,{GATEWAY}:31823,{GATEWAY}:31821,{GATEWAY}:9000"
        ]
        rules = allowed[0].rsplit(" ", 1)[1].split(",")
        gateway_rules = [r for r in rules if r.startswith(GATEWAY + ":")]
        assert sorted(gateway_rules) == sorted(
            [f"{GATEWAY}:{p}" for p in (31831, 31851, 31823, 31821, 9000)])
        assert not any(r.endswith((":4222", ":4000")) for r in rules)

    @pytest.mark.parametrize("rule", [
        f"{GATEWAY}:4222", f"{GATEWAY}:31852", f"{GATEWAY}:31824", "*",
        "198.51.100.77:4000", "nats.example.test:4222",
        "192.0.2.0/24", "0.0.0.0/0", "http://example.test/any/path", " pypi.org", "pypi.org ",
    ])
    def test_a_project_rule_for_the_gateway_the_bus_or_the_router_is_refused(self, slot_repo, tmp_path, rule):
        result, sbx, other = _drive(slot_repo, tmp_path, "prepare",
                                    SANDBOX_ALLOW_NETWORK=f"pypi.org,{rule}")
        assert result.returncode == 2
        assert "Refusing" in result.stdout
        assert sbx == [] and other == []

    def test_the_shared_sandbox_is_never_made_a_slot_sandbox(self, slot_repo, tmp_path):
        """Codex R1: refused before any sbx, docker or systemctl call."""
        result, sbx, other = _drive(slot_repo, tmp_path, "prepare", SANDBOX_NAME="api-test-deploy",
                                    SBX_LS="api-test-deploy running")
        assert result.returncode == 2
        assert "is the shared sandbox" in result.stdout
        assert sbx == [] and other == []

    @pytest.mark.parametrize("change,words", [
        ({"SANDBOX_MODEL_DOOR_PORT": "4000"}, "fixed at 31823"),
        ({"SANDBOX_ANSWER_PORT": "4222"}, "fixed at 31821"),
        ({"SANDBOX_EMBEDDER_PORT": "31824"}, "fixed at 9000"),
        ({"SANDBOX_MODEL_DOOR_PORT": "31824"}, "fixed at 31823"),
        ({"SANDBOX_ANSWER_PORT": "31822"}, "fixed at 31821"),
        ({"SANDBOX_EMBEDDER_PORT": "31852"}, "fixed at 9000"),
        ({"SANDBOX_MODEL_DOOR_PORT": "31900"}, "fixed at 31823"),
        ({"SANDBOX_ALLOW_NETWORK": "pypi.org,mirror.example.test:31845"}, "another project's Git door or memory door"),
        ({"SANDBOX_ALLOW_NETWORK": "pypi.org,mirror.example.test:4000"}, "the bus's or the model router's"),
    ])
    def test_the_whole_policy_is_checked_before_any_call(self, slot_repo, tmp_path, change, words):
        """Codex R2: the shared services are fixed for a slot sandbox, and the
        project's rules are checked too; refused before any call."""
        result, sbx, other = _drive(slot_repo, tmp_path, "prepare", **change)
        assert result.returncode == 2, result.stdout
        assert words in result.stdout
        assert sbx == [] and other == []

    def test_naming_the_fixed_value_itself_is_accepted(self, slot_repo, tmp_path):
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare", SANDBOX_MODEL_DOOR_PORT="31823",
                                SANDBOX_ANSWER_PORT="31821", SANDBOX_EMBEDDER_PORT="9000")
        assert result.returncode == 0, result.stdout + result.stderr
        allowed = [line for line in sbx if line.startswith("policy allow ")][0]
        gateway = sorted(r for r in allowed.rsplit(" ", 1)[1].split(",") if r.startswith(GATEWAY + ":"))
        assert gateway == sorted(f"{GATEWAY}:{p}" for p in (31831, 31851, 31823, 31821, 9000))
        assert not any(r.endswith((":31822", ":31824")) for r in gateway)

    def test_the_receipts_are_mounted_at_runtime_and_checked(self, slot_repo, tmp_path):
        _, _, receipts, _ = slot_repo
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare")
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"mount a-private-project {receipts}" in sbx
        assert sbx.index(f"mount a-private-project {receipts}") > sbx.index(
            [line for line in sbx if line.startswith("policy allow ")][0])

    def test_receipts_already_mounted_are_left_alone(self, slot_repo, tmp_path):
        _, _, _, state = slot_repo
        (state / "mounted").touch()
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare", SBX_LS="a-private-project running")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not [line for line in sbx if line.startswith("mount ")]
        assert not [line for line in sbx if line.startswith("create ")]

    def test_a_mount_that_does_not_show_inside_refuses(self, slot_repo, tmp_path):
        result, _, _ = _drive(slot_repo, tmp_path, "prepare", SBX_MOUNT_NEVER_SHOWS="1")
        assert result.returncode == 2
        assert "still not a mount point" in result.stdout

    def test_no_host_unit_is_asked_about_or_started(self, slot_repo, tmp_path):
        result, _, other = _drive(slot_repo, tmp_path, "prepare")
        assert result.returncode == 0, result.stdout + result.stderr
        assert other == []
        assert "up.sh a-private-project <version>" in result.stdout

    def test_prepare_deploys_nothing_and_a_plain_run_deploys(self, slot_repo, tmp_path):
        repo, _, _, _ = slot_repo
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare")
        assert result.returncode == 0
        assert not [line for line in sbx if "deploy/deploy.sh" in line]
        (tmp_path / "sbx.log").unlink()
        result, sbx, _ = _drive(slot_repo, tmp_path, SBX_LS="a-private-project running")
        assert result.returncode == 0, result.stdout + result.stderr
        assert [line for line in sbx if line.endswith("a-private-project deploy/deploy.sh")]

    @pytest.mark.parametrize("change,words", [
        ({"SANDBOX_SLOT": "0"}, "slot from 1 to 19"),
        ({"SANDBOX_SLOT": "x"}, "slot from 1 to 19"),
        ({"SANDBOX_SLOT": "20"}, "slot from 1 to 19"),
        ({"SANDBOX_PUBLISH_ADDRESS": "127.0.0.1"}, "SANDBOX_PUBLISH_ADDRESS"),
        ({"SANDBOX_GATEWAY_ADDRESS": ""}, "SANDBOX_GATEWAY_ADDRESS"),
        ({"SANDBOX_RECEIPTS_PATH": ""}, "SANDBOX_RECEIPTS_PATH"),
        ({"SANDBOX_RUNNER_PUBLISH": "127.0.0.1:8924:8124"}, "Leave both out"),
        ({"SANDBOX_MODEL_DOOR_PORT": "door"}, "fixed at 31823"),
    ])
    def test_a_bad_slot_setting_refuses_before_anything(self, slot_repo, tmp_path, change, words):
        result, sbx, other = _drive(slot_repo, tmp_path, "prepare", **change)
        assert result.returncode == 2
        assert words in result.stdout
        assert sbx == [] and other == []

    def test_a_folder_that_is_not_a_checkout_is_refused(self, slot_repo, tmp_path):
        elsewhere = tmp_path / "not-a-checkout"
        elsewhere.mkdir()
        result, sbx, _ = _drive(slot_repo, tmp_path, "prepare", SANDBOX_REPO_ROOT=str(elsewhere))
        assert result.returncode == 2
        assert "not a git checkout" in result.stdout
        assert not [line for line in sbx if line.startswith("create ")]

    def test_an_unknown_argument_is_refused(self, slot_repo, tmp_path):
        result, sbx, _ = _drive(slot_repo, tmp_path, "destroy")
        assert result.returncode == 2 and sbx == []


# ---------------------------------------------------------------------------
# run.sh: the receipts mount comes back, and a missing image refuses the start
# ---------------------------------------------------------------------------

FAKE_CLIENT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
if [ "$1" = mount ]; then touch "$STATE/mounted"; exit 0; fi
case "$3" in
  mountpoint) [ -e "$STATE/mounted" ] && exit 0; exit 1 ;;
  docker) [ "$4 $5" = "image inspect" ] && exit "${FAKE_IMAGE_STATUS:-0}" ;;
  pkill) exit 0 ;;
esac
for word in "$@"; do
  case "$word" in
    *keeper-hold*) exec sleep 600 ;;
    stop) exit 0 ;;
  esac
done
exec sleep 600
"""


@pytest.fixture
def client_root():
    made = Path(tempfile.mkdtemp(prefix="sbxslot-"))
    (made / "sbx").write_text(FAKE_CLIENT)
    (made / "sbx").chmod(0o755)
    (made / "state").mkdir()
    socket_dir = made / "client-state" / "state" / "sandboxes" / "sandboxes" / "sandboxd"
    socket_dir.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_dir / "sandboxd.sock"))
    try:
        yield made
    finally:
        listener.close()
        shutil.rmtree(made, ignore_errors=True)


def _supervise(root: Path, timeout: float = 20, **extra: str):
    env = {
        "PATH": os.environ["PATH"],
        "SANDBOX_CLIENT": str(root / "sbx"),
        "SANDBOX_NAME": "a-private-project",
        "SANDBOX_BOOTSTRAP": "/the/clone/deploy/sandbox-runner.sh",
        "SANDBOX_RESTART_SECONDS": "1",
        "SANDBOX_STOP_TIMEOUT_SECONDS": "10",
        "SANDBOXES_STORAGE_ROOT": str(root / "client-state"),
        "CALLS": str(root / "calls"),
        "STATE": str(root / "state"),
        "SANDBOX_NOT_READY_WAIT_SECONDS": "0",
        **extra,
    }
    process = subprocess.Popen(["bash", str(RUN_SH)], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    return process


def _finish(process: subprocess.Popen, seconds: float = 15) -> tuple[int, str]:
    try:
        out, _ = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
        out, _ = process.communicate(timeout=30)
    return process.returncode, out


class TestTheSupervisorPreparesTheSandbox:
    def test_a_missing_release_image_refuses_the_start_and_imports_nothing(self, client_root):
        process = _supervise(client_root, SANDBOX_RELEASE_IMAGE="forge:a-release",
                             FAKE_IMAGE_STATUS="1")
        status, out = _finish(process)
        assert status == 2, out
        assert "the release image forge:a-release is not in the sandbox a-private-project's own Docker engine" in out
        assert "never imports" in out
        calls = _lines(client_root / "calls")
        assert not [c for c in calls if "sandbox-runner.sh" in c and not c.endswith(" stop")]
        assert not [c for c in calls if "keeper-hold" in c]
        assert not [c for c in calls if " load" in c or " pull" in c or " import" in c]

    def test_a_refusal_waits_before_it_exits_and_calls_nothing_meanwhile(self, client_root):
        import time
        began = time.monotonic()
        process = _supervise(client_root, SANDBOX_RELEASE_IMAGE="forge:a-release",
                             FAKE_IMAGE_STATUS="1", SANDBOX_NOT_READY_WAIT_SECONDS="3")
        for _ in range(100):
            if any("image inspect" in c for c in _lines(client_root / "calls")):
                break
            time.sleep(0.05)
        calls_at_refusal = len(_lines(client_root / "calls"))
        status, out = _finish(process, seconds=30)
        assert status == 2, out
        assert time.monotonic() - began >= 3
        assert "waiting 3s before exiting" in out
        assert len(_lines(client_root / "calls")) == calls_at_refusal

    def test_a_stop_ends_the_refusals_wait(self, client_root):
        import signal
        import time
        process = _supervise(client_root, SANDBOX_RELEASE_IMAGE="forge:a-release",
                             FAKE_IMAGE_STATUS="1", SANDBOX_NOT_READY_WAIT_SECONDS="300")
        lines = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            lines.append(line)
            if "before exiting" in line:
                break
        began = time.monotonic()
        process.send_signal(signal.SIGTERM)
        status, out = _finish(process, seconds=20)
        assert time.monotonic() - began < 15
        assert status == 0, "".join(lines) + out
        assert "asked to stop while refusing" in out

    def test_an_image_that_disagrees_with_what_is_handed_in_refuses(self, client_root):
        process = _supervise(client_root, SANDBOX_RELEASE_IMAGE="forge:a-release",
                             FORGE_IMAGE="forge:another-release", SANDBOX_ENV_NAMES="FORGE_IMAGE")
        status, out = _finish(process)
        assert status == 2, out
        assert "must name the same release image" in out

    def test_a_lost_receipts_mount_is_made_again_before_the_bootstrap(self, client_root):
        process = _supervise(client_root, SANDBOX_RECEIPTS_MOUNT="/receipts/a-private-project",
                             SANDBOX_RELEASE_IMAGE="forge:a-release")
        try:
            calls_path = client_root / "calls"
            for _ in range(100):
                if any(c.endswith("/the/clone/deploy/sandbox-runner.sh") for c in _lines(calls_path)):
                    break
                subprocess.run(["sleep", "0.1"])
            calls = _lines(calls_path)
        finally:
            status, out = _finish(process, seconds=0.1)
        mount = calls.index("mount a-private-project /receipts/a-private-project")
        image = next(i for i, c in enumerate(calls) if "image inspect" in c)
        bootstrap = next(i for i, c in enumerate(calls) if c.endswith("/the/clone/deploy/sandbox-runner.sh"))
        assert mount < image < bootstrap, calls
        assert "mounted inside a-private-project again" in out

    def test_a_relative_receipts_path_refuses(self, client_root):
        process = _supervise(client_root, SANDBOX_RECEIPTS_MOUNT="receipts")
        status, out = _finish(process)
        assert status == 2, out
        assert "absolute path" in out

    def test_without_the_two_settings_nothing_new_is_asked(self, client_root):
        process = _supervise(client_root)
        try:
            for _ in range(100):
                if any(c.endswith("/the/clone/deploy/sandbox-runner.sh") for c in _lines(client_root / "calls")):
                    break
                subprocess.run(["sleep", "0.1"])
            calls = _lines(client_root / "calls")
        finally:
            _finish(process, seconds=0.1)
        assert not [c for c in calls if c.startswith("mount ") or "mountpoint" in c or "image inspect" in c]


def test_the_compose_file_hands_both_settings_to_the_supervisor_off_by_default():
    import yaml
    service = yaml.safe_load(COMPOSE.read_text())["services"]["sandbox-runner"]
    assert service["environment"]["SANDBOX_RECEIPTS_MOUNT"] == "${SANDBOX_RECEIPTS_MOUNT:-}"
    assert service["environment"]["SANDBOX_RELEASE_IMAGE"] == "${SANDBOX_RELEASE_IMAGE:-}"


# ---------------------------------------------------------------------------
# The coordinator's mount of each project's own receipts (design 2.2a)
# ---------------------------------------------------------------------------

PROJECT_RECEIPTS = ROOT / "deploy" / "compose" / "compose.project-receipts.yaml"


def test_the_project_receipts_file_adds_one_read_write_bind_to_the_coordinator_only():
    import yaml
    document = yaml.safe_load(PROJECT_RECEIPTS.read_text())
    assert list(document["services"]) == ["coordinator"]
    (volume,) = document["services"]["coordinator"]["volumes"]
    assert volume["type"] == "bind"
    assert volume["target"] == "/var/lib/forge-project-receipts"
    assert volume["source"].startswith("${FORGE_PROJECT_RECEIPTS_HOST_DIR:?")
    assert volume["bind"] == {"create_host_path": False}
    assert not volume.get("read_only")


def test_without_the_file_the_coordinator_has_no_project_receipts_mount():
    text = (ROOT / "deploy" / "compose" / "compose.yaml").read_text()
    assert "forge-project-receipts" not in text
    assert "FORGE_PROJECT_RECEIPTS_HOST_DIR" not in text


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed here")
def test_with_the_file_the_render_keeps_the_old_mount_and_adds_the_new_one(tmp_path):
    import json as _json
    receipts = tmp_path / "forge-receipts"
    receipts.mkdir()
    bare = {name: os.environ[name] for name in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
            if name in os.environ}
    bare["FORGE_PUBLISHER_BRIDGE"] = "fpbprojectrcpt1"
    bare["FORGE_PROJECT_RECEIPTS_HOST_DIR"] = str(receipts)
    done = subprocess.run(
        ["docker", "compose", "--project-name", "forge-project-receipts-check", "--env-file", ".env.example",
         "--file", "compose.yaml", "--file", "compose.project-receipts.yaml", "config", "--format", "json"],
        cwd=ROOT / "deploy" / "compose", env=bare, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    volumes = _json.loads(done.stdout)["services"]["coordinator"]["volumes"]
    targets = {v["target"]: v for v in volumes}
    assert "/var/lib/forge-evidence" in targets
    assert targets["/var/lib/forge-project-receipts"]["source"] == str(receipts)
    assert not targets["/var/lib/forge-project-receipts"].get("read_only")
