"""The bootstrap inside a project's sandbox runs the factory from the release image.

WHY THIS FILE EXISTS (24 September 2026, stage 4d of the containerisation
rollout gate). Until this pass the script under test —
``src/forge/cli/deploy_templates/sandbox-runner.sh``, the bootstrap Forge ships
into every registered repository — copied the factory's own code out of
read-only mounts of five checkouts on one machine, built a virtual environment
inside the sandbox from them and ran the two services out of that. None of
those five checkouts exists on a clean machine. Rich, 23 September: *"Why are
we still using systemd after I asked for containerisation to allow easy
deployment both locally and to the cloud?"*

So the bootstrap now checks the release image the machine handed into the
sandbox and runs the deploy helper and the build runner as two containers from
it, and from nothing else. Four things about that have to hold, and they are
what is proven here:

* it REFUSES, by name, an image that is missing or is not the one the machine
  handed over — and it never fetches anything instead;
* it starts both services from that one image, with the project's own clone
  bound read-write and every setting handed in BY NAME, never by value;
* a second start REFUSES, so a sandbox can never end up with two supervisors
  and two sets of containers (the pile-up that cost a day on 2026-09-11);
* the STOP WORD stops and removes both containers and exits 0 ONLY when both
  are really gone — which is what the host-side sandbox service's own stop
  requires of it, because out there ending the client ends nothing in here.

HOW, without a sandbox. The script's whole contact with the world is one
Docker client. These tests put a stand-in on PATH that records every call and
answers as told, so what is checked is exactly what the bootstrap would have
asked a real engine to do. **No sandbox, image, container, engine or service of
the estate is touched by anything in this file.**

Nothing here names a language, a test runner, a package manager or any
project's layout: the bootstrap is the factory's, the factory is agnostic, and
the throwaway project below is an empty folder with a deploy/ in it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import forge.cli.deploy_templates as templates

#: The bootstrap under test, exactly as Forge ships it into a repository.
BOOTSTRAP = Path(templates.__file__).resolve().parent / "sandbox-runner.sh"

#: The throwaway release the stand-in engine pretends to hold.
IMAGE = "a-throwaway-release:for-this-test"
LAYERS = [
    "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    "sha256:2222222222222222222222222222222222222222222222222222222222222222",
]
VERSION = "2026.09.24-throwaway"
MANIFEST = "3333333333333333333333333333333333333333333333333333333333333333"
ENGINE_ID = "sha256:4444444444444444444444444444444444444444444444444444444444444444"

#: The fingerprint the bootstrap computes: the layer list, one per line,
#: hashed. Computed here the same way the script computes it, from the same
#: list, so the test never copies an answer out of the script.
CONTENT_ID = hashlib.sha256(("\n".join(LAYERS) + "\n").encode()).hexdigest()

#: A stand-in Docker client. It records every call, one line each, keeps a tiny
#: record of which containers exist and which are running, and answers
#: ``image inspect`` for exactly one image. Two settings let a test make it
#: behave badly: STANDIN_RM_REFUSES names a container that will not go, and
#: STANDIN_NO_IMAGE makes the engine hold no image at all.
STANDIN_DOCKER = '''#!/usr/bin/env python3
import os, pathlib, sys

calls = pathlib.Path(os.environ["STANDIN_CALLS"])
state = pathlib.Path(os.environ["STANDIN_STATE"])
state.mkdir(parents=True, exist_ok=True)
argv = sys.argv[1:]
with calls.open("a") as handle:
    handle.write(" ".join(argv) + "\\n")

LAYERS = os.environ["STANDIN_LAYERS"].split(",")


def named(arguments):
    """The container names in a --filter name=^X$ list, in order."""
    found = []
    for index, word in enumerate(arguments):
        if word == "--filter" and index + 1 < len(arguments):
            value = arguments[index + 1]
            if value.startswith("name="):
                found.append(value[len("name="):].strip("^$"))
    return found


def running(name):
    return (state / (name + ".running")).exists()


def exists(name):
    return (state / name).exists()


verb = argv[0] if argv else ""

if verb == "image" and len(argv) > 1 and argv[1] == "inspect":
    if os.environ.get("STANDIN_NO_IMAGE"):
        sys.exit(1)
    fmt = ""
    for index, word in enumerate(argv):
        if word == "--format" and index + 1 < len(argv):
            fmt = argv[index + 1]
    wanted = argv[-1]
    if wanted != os.environ["STANDIN_IMAGE"]:
        sys.exit(1)
    if "RootFS.Layers" in fmt:
        for layer in LAYERS:
            print(layer)
    elif "release.version" in fmt:
        print(os.environ.get("STANDIN_VERSION", ""))
    elif "manifest.sha256" in fmt:
        print(os.environ.get("STANDIN_MANIFEST", ""))
    elif ".Id" in fmt:
        print(os.environ.get("STANDIN_ENGINE_ID", ""))
    sys.exit(0)

if verb == "ps":
    all_of_them = "-a" in argv
    for name in named(argv):
        if exists(name) and (all_of_them or running(name)):
            print("an-id-for-" + name)
    sys.exit(0)

if verb == "stop":
    for name in argv[1:]:
        if name.startswith("-") or name.isdigit():
            continue
        (state / (name + ".running")).unlink(missing_ok=True)
    sys.exit(0)

if verb == "rm":
    for name in argv[1:]:
        if name.startswith("-"):
            continue
        if name == os.environ.get("STANDIN_RM_REFUSES", ""):
            sys.exit(1)
        (state / name).unlink(missing_ok=True)
        (state / (name + ".running")).unlink(missing_ok=True)
    sys.exit(0)

if verb == "run":
    name = ""
    for index, word in enumerate(argv):
        if word == "--name" and index + 1 < len(argv):
            name = argv[index + 1]
    (state / name).write_text("made\\n")
    (state / (name + ".running")).write_text("up\\n")
    print("an-id-for-" + name)
    sys.exit(0)

sys.exit(0)
'''


@pytest.fixture()
def sandbox(tmp_path):
    """A throwaway stand-in for the inside of a sandbox.

    A project folder with the bootstrap in its ``deploy/``, a home of its own
    for the bootstrap's lock and process record, and a stand-in Docker client.
    """
    project = tmp_path / "a-throwaway-project"
    (project / "deploy").mkdir(parents=True)
    shutil.copy2(BOOTSTRAP, project / "deploy" / "sandbox-runner.sh")
    (project / "deploy" / "sandbox-runner.sh").chmod(0o755)

    client = tmp_path / "docker-standin"
    client.write_text(STANDIN_DOCKER)
    client.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    return {
        "project": project,
        "script": project / "deploy" / "sandbox-runner.sh",
        "client": client,
        "home": home,
        "calls": tmp_path / "calls",
        "state": tmp_path / "engine",
    }


def _settings(sandbox, **extra):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(sandbox["home"]),
        "SANDBOX_DOCKER": str(sandbox["client"]),
        "STANDIN_CALLS": str(sandbox["calls"]),
        "STANDIN_STATE": str(sandbox["state"]),
        "STANDIN_IMAGE": IMAGE,
        "STANDIN_LAYERS": ",".join(LAYERS),
        "STANDIN_VERSION": VERSION,
        "STANDIN_MANIFEST": MANIFEST,
        "STANDIN_ENGINE_ID": ENGINE_ID,
        "FORGE_IMAGE": IMAGE,
        "FORGE_IMAGE_CONTENT_ID": CONTENT_ID,
        "SANDBOX_RUNNER_RESTART_SECONDS": "1",
    }
    env.update(extra)
    return env


def _run(sandbox, *arguments, **extra):
    return subprocess.run(
        ["bash", str(sandbox["script"]), *arguments],
        env=_settings(sandbox, **extra),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(sandbox):
    path = sandbox["calls"]
    return path.read_text().splitlines() if path.exists() else []


# ---------------------------------------------------------------------------
class TestItRefusesAnImageItCannotVouchFor:
    """No image, a different image, or nothing to check against: it refuses.

    And in every one of these cases it must FETCH NOTHING. There is no
    source-clone fallback in this path on purpose (design pass, section 5): a
    clone at the pinned commit is not the tested image.
    """

    def test_no_image_named_at_all(self, sandbox):
        result = _run(sandbox, FORGE_IMAGE="")
        assert result.returncode == 2
        assert "FORGE_IMAGE is not set" in result.stdout
        assert "no source fallback" in result.stdout

    def test_no_fingerprint_to_check_against(self, sandbox):
        result = _run(sandbox, FORGE_IMAGE_CONTENT_ID="")
        assert result.returncode == 2
        assert "FORGE_IMAGE_CONTENT_ID is not set" in result.stdout

    def test_the_image_is_not_in_the_sandbox(self, sandbox):
        result = _run(sandbox, STANDIN_NO_IMAGE="1")
        assert result.returncode == 2
        assert "is not in this sandbox's own engine" in result.stdout
        assert "Hand it in first" in result.stdout
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_a_different_image_under_the_same_name(self, sandbox):
        wrong = "0" * 64
        result = _run(sandbox, FORGE_IMAGE_CONTENT_ID=wrong)
        assert result.returncode == 2
        # The sentence names BOTH, so an operator can see which is which.
        assert wrong in result.stdout
        assert CONTENT_ID in result.stdout
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_a_release_version_that_does_not_match(self, sandbox):
        result = _run(sandbox, FORGE_RELEASE_VERSION="some-other-release")
        assert result.returncode == 2
        assert "some-other-release" in result.stdout
        assert VERSION in result.stdout

    def test_a_manifest_hash_that_does_not_match(self, sandbox):
        result = _run(sandbox, FORGE_RELEASE_MANIFEST_SHA256="9" * 64)
        assert result.returncode == 2
        assert "9" * 64 in result.stdout

    def test_the_right_image_passes_and_starts_nothing_in_a_warm_up(self, sandbox):
        result = _run(
            sandbox,
            FORGE_RELEASE_VERSION=VERSION,
            FORGE_RELEASE_MANIFEST_SHA256=MANIFEST,
            SANDBOX_RUNNER_BOOTSTRAP_ONLY="1",
        )
        assert result.returncode == 0
        assert CONTENT_ID in result.stdout
        assert not any(line.startswith("run ") for line in _calls(sandbox))


# ---------------------------------------------------------------------------
class TestItStartsTwoContainersFromThatOneImage:
    """What it asks the engine for, and what it never asks for."""

    @pytest.fixture()
    def started(self, sandbox):
        process = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(sandbox),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(100):
            if len([c for c in _calls(sandbox) if c.startswith("run ")]) >= 2:
                break
            time.sleep(0.1)
        try:
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - a stuck test
                process.kill()

    def test_both_come_from_the_image_that_was_checked(self, sandbox, started):
        runs = [c for c in _calls(sandbox) if c.startswith("run ")]
        assert len(runs) == 2
        assert all(IMAGE in call for call in runs)
        assert any("--name forge-sandbox-helper" in call for call in runs)
        assert any("--name forge-sandbox-runner" in call for call in runs)

    def test_the_two_ports_are_published_inside_the_sandbox(self, sandbox, started):
        runs = " || ".join(c for c in _calls(sandbox) if c.startswith("run "))
        assert "--publish 0.0.0.0:8125:8125" in runs
        assert "--publish 0.0.0.0:8124:8124" in runs

    def test_the_projects_own_clone_is_bound_read_write(self, sandbox, started):
        clone = str(sandbox["project"])
        runs = [c for c in _calls(sandbox) if c.startswith("run ")]
        assert all(f"--volume {clone}:{clone}:rw" in call for call in runs)

    def test_nothing_of_the_factorys_source_is_mounted_and_no_venv_is_made(
        self, sandbox, started
    ):
        runs = " || ".join(c for c in _calls(sandbox) if c.startswith("run "))
        for word in ("guardkit", "nats-core", "fleet-memory", "guardkitfactory"):
            assert word not in runs
        assert not (sandbox["home"] / ".forge-venv").exists()
        assert not (sandbox["home"] / ".forge-src").exists()
        assert "uv " not in " ".join(_calls(sandbox))

    def test_settings_cross_by_name_and_never_by_value(self, sandbox):
        secret = "a-value-that-must-never-be-written-down"
        process = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(
                sandbox,
                FORGE_TARGET_OWNER_URL=secret,
                FORGE_NATS_URL=secret,
                FACTORY_GATEWAY_ADDRESS=secret,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                if len([c for c in _calls(sandbox) if c.startswith("run ")]) >= 2:
                    break
                time.sleep(0.1)
            runs = " || ".join(c for c in _calls(sandbox) if c.startswith("run "))
            for name in (
                "FORGE_TARGET_OWNER_URL",
                "FORGE_NATS_URL",
                "FACTORY_GATEWAY_ADDRESS",
            ):
                assert f"--env {name}" in runs
            assert secret not in runs
        finally:
            process.terminate()
            output = process.communicate(timeout=30)[0]
        assert secret not in output

    def test_a_project_can_name_settings_of_its_own(self, sandbox):
        process = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(
                sandbox,
                SANDBOX_CONTAINER_ENV_NAMES="A_THING_THIS_PROJECT_NEEDS,ANOTHER_ONE",
                A_THING_THIS_PROJECT_NEEDS="something",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                if len([c for c in _calls(sandbox) if c.startswith("run ")]) >= 2:
                    break
                time.sleep(0.1)
            runs = " || ".join(c for c in _calls(sandbox) if c.startswith("run "))
            assert "--env A_THING_THIS_PROJECT_NEEDS" in runs
            # One with nothing set is left out rather than handed in empty.
            assert "--env ANOTHER_ONE" not in runs
        finally:
            process.terminate()
            process.wait(timeout=30)

    def test_the_coordinators_record_is_never_handed_in(self, sandbox):
        process = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(
                sandbox,
                SANDBOX_CONTAINER_ENV_NAMES="FORGE_DB_PATH",
                FORGE_DB_PATH="/somewhere/forge.db",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                if len([c for c in _calls(sandbox) if c.startswith("run ")]) >= 2:
                    break
                time.sleep(0.1)
            runs = " || ".join(c for c in _calls(sandbox) if c.startswith("run "))
            assert "FORGE_DB_PATH" not in runs
            assert "/somewhere/forge.db" not in runs
        finally:
            process.terminate()
            process.wait(timeout=30)


# ---------------------------------------------------------------------------
class TestASecondStartRefuses:
    """One supervisor per checkout, or a sandbox ends up with two of everything."""

    def test_it_refuses_and_starts_nothing(self, sandbox):
        first = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(sandbox),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                if len([c for c in _calls(sandbox) if c.startswith("run ")]) >= 2:
                    break
                time.sleep(0.1)
            before = len([c for c in _calls(sandbox) if c.startswith("run ")])
            second = _run(sandbox)
            assert second.returncode == 0
            assert "refusing to start" in second.stdout
            assert "already running in this sandbox" in second.stdout
            assert len([c for c in _calls(sandbox) if c.startswith("run ")]) == before
        finally:
            first.terminate()
            first.wait(timeout=30)


# ---------------------------------------------------------------------------
class TestTheStopWord:
    """Exit 0 only when both containers are really gone."""

    def test_it_ends_both_and_exits_zero(self, sandbox):
        first = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(sandbox),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(100):
            if len([c for c in _calls(sandbox) if c.startswith("run ")]) >= 2:
                break
            time.sleep(0.1)
        stopped = _run(sandbox, "stop")
        assert stopped.returncode == 0
        assert "stopped:" in stopped.stdout
        assert not (sandbox["state"] / "forge-sandbox-helper").exists()
        assert not (sandbox["state"] / "forge-sandbox-runner").exists()
        first.wait(timeout=30)

    def test_a_stop_with_no_supervisor_still_ends_the_containers(self, sandbox):
        # A session that dropped leaves the containers running in there with
        # nothing watching them. The stop must still be the thing that ends
        # them, because the host side has no other word.
        sandbox["state"].mkdir(parents=True, exist_ok=True)
        for name in ("forge-sandbox-helper", "forge-sandbox-runner"):
            (sandbox["state"] / name).write_text("made\n")
            (sandbox["state"] / (name + ".running")).write_text("up\n")
        stopped = _run(sandbox, "stop")
        assert stopped.returncode == 0
        assert "no supervisor record" in stopped.stdout
        assert not (sandbox["state"] / "forge-sandbox-helper").exists()

    def test_a_container_that_will_not_go_is_a_non_zero_stop(self, sandbox):
        sandbox["state"].mkdir(parents=True, exist_ok=True)
        for name in ("forge-sandbox-helper", "forge-sandbox-runner"):
            (sandbox["state"] / name).write_text("made\n")
            (sandbox["state"] / (name + ".running")).write_text("up\n")
        stopped = _run(sandbox, "stop", STANDIN_RM_REFUSES="forge-sandbox-runner")
        assert stopped.returncode != 0
        assert "would not go" in stopped.stdout
        assert "stopped:" not in stopped.stdout

    def test_stopping_what_was_never_started_is_not_a_failure(self, sandbox):
        stopped = _run(sandbox, "stop")
        assert stopped.returncode == 0

    def test_an_unknown_word_is_refused_rather_than_guessed(self, sandbox):
        result = _run(sandbox, "restart-everything-please")
        assert result.returncode == 2
        assert "usage" in result.stdout
