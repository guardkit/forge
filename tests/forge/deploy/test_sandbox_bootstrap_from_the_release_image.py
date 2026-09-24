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
class TestTheFoldersBothContainersShare:
    """Anything the two containers share has to be a folder of the sandbox's.

    The supervisor throws a container away and makes another one from the
    image whenever it dies, so a folder INSIDE a container is gone with it —
    and the other container never saw it in the first place. A build's
    per-build worktrees (the runner cuts them, the helper retires them) and its
    receipts (written during the build, read afterwards from outside both
    containers) are therefore made in the sandbox and bound into both at the
    same path. The stage 4d reviewer's first two findings, 24 September 2026.
    """

    @staticmethod
    def _runs_of_a_started_bootstrap(sandbox, **extra):
        """Start it, wait for both containers, return the two run calls."""
        process = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(sandbox, **extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                if len([c for c in _calls(sandbox) if c.startswith("run ")]) >= 2:
                    break
                time.sleep(0.1)
            return [c for c in _calls(sandbox) if c.startswith("run ")]
        finally:
            process.terminate()
            process.wait(timeout=30)

    def test_the_named_folders_are_made_and_bound_into_both(self, sandbox, tmp_path):
        receipts = tmp_path / "a-receipts-root"
        worktrees = tmp_path / "a-worktree-base"
        runs = self._runs_of_a_started_bootstrap(
            sandbox,
            SANDBOX_RECEIPTS_PATH=str(receipts),
            FORGE_AUTOBUILD_WORKTREE_BASE=str(worktrees),
        )
        assert len(runs) == 2
        # Made before anything started — Docker would otherwise make the bind
        # source itself, owned by root, for a container that is not root.
        assert receipts.is_dir()
        assert worktrees.is_dir()
        for call in runs:
            assert f"--volume {receipts}:{receipts}:rw" in call
            assert f"--volume {worktrees}:{worktrees}:rw" in call
            # And both containers are TOLD where they are, by name.
            assert "--env FORGE_RECEIPTS_DIR" in call
            assert "--env FORGE_AUTOBUILD_WORKTREE_BASE" in call

    def test_with_no_setting_a_folder_of_the_sandbox_is_used_anyway(self, sandbox):
        # Unset, the factory's own defaults are folders inside the container.
        # The bootstrap names ones in the sandbox instead, so a replaced
        # container does not take a running build's work with it.
        runs = self._runs_of_a_started_bootstrap(sandbox)
        assert len(runs) == 2
        home = str(sandbox["home"])
        for call in runs:
            words = call.split()
            bound = [
                word for before, word in zip(words, words[1:]) if before == "--volume"
            ]
            # Two read-write folders of the sandbox's own home, each bound at
            # the same path it has in the sandbox. (The runner also gets its
            # graph declaration from in there, read-only, at a path of its
            # own — that one is not a folder the two containers share.)
            shared = [
                word
                for word in bound
                if word.startswith(home) and word.endswith(":rw")
            ]
            assert len(shared) == 2, call
            for word in shared:
                inside, outside = word[: -len(":rw")].split(":", 1)
                assert inside == outside
            assert "--env FORGE_RECEIPTS_DIR" in call
            assert "--env FORGE_AUTOBUILD_WORKTREE_BASE" in call

    def test_a_worktree_base_that_cannot_be_made_is_refused_by_name(
        self, sandbox, tmp_path
    ):
        in_the_way = tmp_path / "this-is-a-file"
        in_the_way.write_text("not a folder\n")
        wanted = in_the_way / "under-a-file"
        result = _run(sandbox, FORGE_AUTOBUILD_WORKTREE_BASE=str(wanted))
        assert result.returncode == 2
        assert "FORGE_AUTOBUILD_WORKTREE_BASE" in result.stdout
        assert str(wanted) in result.stdout
        # Never handed in unbound: nothing was started at all.
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_the_helper_is_given_the_group_that_owns_the_engine_socket(
        self, sandbox, tmp_path
    ):
        """A bound socket a container's user cannot open is no socket at all.

        Found by running it, 24 September 2026: the socket was bound into the
        helper and every call answered "permission denied while trying to
        connect to the docker API". The socket is owner-and-group only and the
        container runs as a plain user who is in none of the sandbox's groups.
        """
        socket_path = tmp_path / "an-engine.sock"
        subprocess.run(
            [
                "python3",
                "-c",
                "import socket,sys\n"
                "s=socket.socket(socket.AF_UNIX)\n"
                "s.bind(sys.argv[1])\n",
                str(socket_path),
            ],
            check=True,
            timeout=30,
        )
        group = socket_path.stat().st_gid
        runs = self._runs_of_a_started_bootstrap(
            sandbox, SANDBOX_DOCKER_SOCKET=str(socket_path)
        )
        helper = [call for call in runs if "--name forge-sandbox-helper" in call]
        runner = [call for call in runs if "--name forge-sandbox-runner" in call]
        assert len(helper) == 1 and len(runner) == 1
        assert f"--volume {socket_path}:/var/run/docker.sock" in helper[0]
        assert f"--group-add {group}" in helper[0]
        # The runner is given no socket, so it is given no group either.
        assert "--group-add" not in runner[0]
        assert "docker.sock" not in runner[0]

    def test_a_receipts_root_that_cannot_be_made_is_refused_by_name(
        self, sandbox, tmp_path
    ):
        in_the_way = tmp_path / "also-a-file"
        in_the_way.write_text("not a folder\n")
        wanted = in_the_way / "under-a-file"
        result = _run(sandbox, SANDBOX_RECEIPTS_PATH=str(wanted))
        assert result.returncode == 2
        assert "SANDBOX_RECEIPTS_PATH" in result.stdout
        assert str(wanted) in result.stdout
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_the_fallback_receipts_root_says_nothing_outside_is_looking(
        self, sandbox
    ):
        """Surviving a container is not the same as being where anyone reads.

        The stage 4e reviewer's rollout note, 24 September 2026: with nothing
        naming a receipts root, the bootstrap uses one of its own in the
        sandbox. That survives a container being replaced — which is all this
        stage was about — but it is a path nothing outside the sandbox knows,
        and a project whose receipts are read from outside has to name its own
        folder. Nothing does it for anyone, so the start log has to say so.
        """
        process = subprocess.Popen(
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
        finally:
            process.terminate()
            said = process.communicate(timeout=30)[0]
        assert "SANDBOX_RECEIPTS_PATH" in said
        assert "receipts_path" in said
        assert "nothing outside this sandbox is looking at it" in said


# ---------------------------------------------------------------------------
class TestTheHeaderNamesBothWorktreeFoldersInsideTheClone:
    """A list that names one of two sibling folders reads as if the other moved.

    Forge cuts per-build trees in two places inside a registered checkout:
    ``.guardkit/worktrees/<task or feature id>`` (autobuild) and
    ``.forge/worktrees/<build id>`` (the conductor,
    ``src/forge/cli/_conductor_worktree.py``). Both are under the project's
    clone, which IS the shared mount, so the mounting was right either way —
    but the header's list named only the first, which the stage 4e reviewer
    recorded on 24 September 2026. This holds the naming complete.
    """

    def test_both_are_named_under_the_clone(self):
        header = BOOTSTRAP.read_text().split("WHAT IT NEVER DOES", 1)[0]
        clause = header.split("the project's own clone", 1)[1].split(
            "the per-build git worktrees", 1
        )[0]
        assert ".guardkit/worktrees" in clause
        assert ".forge/worktrees" in clause

    def test_the_conductors_own_module_still_cuts_them_there(self):
        """If Forge moves that folder, this list is wrong and should fail."""
        conductor = (
            Path(templates.__file__).resolve().parents[1] / "_conductor_worktree.py"
        )
        assert ".forge/worktrees" in conductor.read_text()


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
            # EXIT 4, its own code with its own sentence (stage 4e). Anything
            # that reads a status rather than the words has to see a refusal
            # here, not a success.
            assert second.returncode == 4
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
