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
import json
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
#: What each of the two kinds of engine calls this image. They differ ON
#: PURPOSE: a machine's own engine keeps images the old way and answers with
#: the digest of the image's CONFIG, a sandbox's engine keeps them the
#: containerd way and answers with the digest of its MANIFEST. Asking each for
#: "the id" gives two answers for the same image, which is why the identity
#: below is what crosses between them and an id is only ever used locally.
ENGINE_ID = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
ENGINE_ID_THE_OTHER_WAY = (
    "sha256:5555555555555555555555555555555555555555555555555555555555555555"
)

#: The image a moved tag would point at instead.
ANOTHER_IMAGE_ID = (
    "sha256:6666666666666666666666666666666666666666666666666666666666666666"
)


def _the_identity_format(script: Path) -> str:
    """The question the bootstrap asks an engine, taken out of the script.

    A test can then ask the stand-in engine the very same question, in the very
    same words, and work out the answer for itself — without ever copying what
    the bootstrap made of it.
    """
    body = script.read_text().split("IMAGE_IDENTITY_DOCUMENT_FORMAT='", 1)[1]
    return body.split("'", 1)[0]


IDENTITY_FORMAT = _the_identity_format(BOOTSTRAP)


def an_image(**how) -> dict:
    """One image in the stand-in engine, as a plain record.

    The defaults are the reviewed release. A test changes one thing about it
    and asks what the bootstrap makes of the result.
    """
    image = {
        "id_classic": ENGINE_ID,
        "id_containerd": ENGINE_ID_THE_OTHER_WAY,
        "architecture": "a-throwaway-architecture",
        "os": "a-throwaway-operating-system",
        "layers": list(LAYERS),
        "env": ["A_SETTING_BAKED_INTO_THE_IMAGE=as-reviewed"],
        "entrypoint": ["the-entry-point-it-was-reviewed-with"],
        "cmd": ["the-command-it-was-reviewed-with"],
        "user": "1234:5678",
        "workdir": "/a/working/directory",
        "labels": {
            "com.guardkit.release.version": VERSION,
            "com.guardkit.release.manifest.sha256": MANIFEST,
        },
        "ports": ["8124/tcp"],
        "volumes": ["/a/declared/volume"],
        "stopsignal": "SIGTERM",
    }
    image.update(how)
    return image


def an_engine(style: str = "classic", **how) -> dict:
    """The whole of a stand-in engine's image table."""
    engine = {
        "style": style,
        "images": {"the-reviewed-release": an_image()},
        "tags": {IMAGE: "the-reviewed-release"},
        "move_tag_to": None,
    }
    engine.update(how)
    return engine


#: A stand-in Docker client: one small engine in a file. It records every call,
#: one line each, keeps a tiny record of which containers exist and which are
#: running, and answers ``image inspect`` out of an image table the test wrote.
#:
#: WHAT IT MODELS, AND WHAT IT DOES NOT (stage 4f, 24 September 2026). It
#: models the two kinds of engine and their two ways of naming an image, a tag
#: moved onto another image after it has been inspected, an engine that will
#: not answer at all, and an image whose configuration was changed while its
#: layers stayed as they were. It runs nothing: no real engine, image or
#: container is touched by anything in this file, and these are not claims
#: about a real engine substituting a real image.
#:
#: Settings a test uses to make it behave badly: STANDIN_NO_IMAGE (this engine
#: holds nothing), STANDIN_RM_REFUSES (this container will not go),
#: STANDIN_PS_FAILS_FROM (the numbered listing from which this engine stops
#: answering at all — which is not the same as answering "nothing there") and
#: STANDIN_ANSWERS_WITH_HALF_A_DOCUMENT (it renders only the layers part of
#: the identity document, which is what the stage 4d reviewer's own stand-in
#: engine does).
STANDIN_DOCKER = '''#!/usr/bin/env python3
import json, os, pathlib, sys

calls = pathlib.Path(os.environ["STANDIN_CALLS"])
state = pathlib.Path(os.environ["STANDIN_STATE"])
state.mkdir(parents=True, exist_ok=True)
argv = sys.argv[1:]
with calls.open("a") as handle:
    handle.write(" ".join(argv) + "\\n")

table = json.loads(pathlib.Path(os.environ["STANDIN_TABLE"]).read_text())
style = table.get("style", "classic")
the_tag_has_moved = state / "the-tag-has-moved"


def image_id(key):
    return table["images"][key]["id_" + style]


def resolve(reference):
    """What a name means to this engine RIGHT NOW: an image, or nothing."""
    tags = dict(table["tags"])
    if table.get("move_tag_to") and the_tag_has_moved.exists():
        for tag in list(tags):
            tags[tag] = table["move_tag_to"]
    if reference in tags:
        return tags[reference]
    for key in table["images"]:
        if reference == image_id(key):
            return key
    return None


def identity_document(key):
    """Exactly the fields the bootstrap's format string asks this engine for."""
    image = table["images"][key]
    lines = ["forge-image-identity/1",
             "architecture " + image["architecture"],
             "os " + image["os"]]
    lines += ["layer " + one for one in image["layers"]]
    lines += ["env " + one for one in image["env"]]
    lines += ["entrypoint " + one for one in image["entrypoint"]]
    lines += ["cmd " + one for one in image["cmd"]]
    lines += ["user " + image["user"], "workdir " + image["workdir"]]
    lines += ["label %s=%s" % (name, image["labels"][name])
              for name in sorted(image["labels"])]
    lines += ["port " + one for one in sorted(image["ports"])]
    lines += ["volume " + one for one in sorted(image["volumes"])]
    lines += ["stopsignal " + image["stopsignal"]]
    return "\\n".join(lines)


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
    key = resolve(argv[-1])
    if key is None:
        sys.exit(1)
    if "forge-image-identity/1" in fmt:
        if os.environ.get("STANDIN_ANSWERS_WITH_HALF_A_DOCUMENT"):
            # An engine that rendered only the part it understood.
            for one in table["images"][key]["layers"]:
                print("layer " + one)
            sys.exit(0)
        print(identity_document(key))
        if table.get("move_tag_to"):
            the_tag_has_moved.write_text("the tag names another image now\\n")
    elif "release.version" in fmt:
        print(table["images"][key]["labels"].get("com.guardkit.release.version", ""))
    elif "manifest.sha256" in fmt:
        print(table["images"][key]["labels"].get("com.guardkit.release.manifest.sha256", ""))
    elif ".Id" in fmt:
        print(image_id(key))
    sys.exit(0)

if verb == "ps":
    counter = state / "listings"
    with counter.open("a") as handle:
        handle.write("one\\n")
    so_far = len(counter.read_text().splitlines())
    fails_from = int(os.environ.get("STANDIN_PS_FAILS_FROM") or 10 ** 6)
    if so_far >= fails_from:
        print("Cannot connect to the Docker daemon (a stand-in, on purpose)",
              file=sys.stderr)
        sys.exit(1)
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
    reference = ""
    for word in argv:
        if word != name and resolve(word) is not None:
            reference = word
            break
    with (state / "what-was-started.jsonl").open("a") as handle:
        handle.write(json.dumps({
            "container": name,
            "reference": reference,
            "resolved": image_id(resolve(reference)) if reference else None,
        }) + "\\n")
    (state / name).write_text("made\\n")
    (state / (name + ".running")).write_text("up\\n")
    print("an-id-for-" + name)
    sys.exit(0)

sys.exit(0)
'''


def _write_the_engine(sandbox, engine: dict) -> Path:
    """Put an image table where the stand-in client will read it."""
    path = sandbox["table"]
    path.write_text(json.dumps(engine, indent=2))
    sandbox["engine"] = engine
    return path


def _the_identity_the_other_engine_recorded(sandbox, engine: dict, reference=IMAGE):
    """What the OTHER kind of engine says this image is, hashed.

    This is what the machine outside records and hands in: it asks its own
    engine the same question, in the same words, and hashes the answer the way
    both scripts do — carriage returns out, exactly one newline at the end. The
    bootstrap under test then asks an engine of the other kind. Same image,
    same document, same hash: that is the whole claim, and every test in this
    file leans on it because this is where the setting comes from.
    """
    other = dict(engine)
    other["style"] = "containerd" if engine.get("style", "classic") == "classic" else "classic"
    elsewhere = sandbox["project"].parent / "the-other-engine"
    elsewhere.mkdir(exist_ok=True)
    table = elsewhere / "table.json"
    table.write_text(json.dumps(other, indent=2))
    answer = subprocess.run(
        [str(sandbox["client"]), "image", "inspect", "--format", IDENTITY_FORMAT, reference],
        env={
            "PATH": os.environ["PATH"],
            "STANDIN_CALLS": str(elsewhere / "calls"),
            "STANDIN_STATE": str(elsewhere / "engine"),
            "STANDIN_TABLE": str(table),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert answer.returncode == 0, answer.stderr
    document = answer.stdout.replace("\r", "").rstrip("\n")
    assert document, "the other engine said nothing about the image"
    return hashlib.sha256((document + "\n").encode()).hexdigest()


@pytest.fixture()
def sandbox(tmp_path):
    """A throwaway stand-in for the inside of a sandbox.

    A project folder with the bootstrap in its ``deploy/``, a home of its own
    for the bootstrap's lock and process record, and a stand-in Docker client
    holding one image: the reviewed release.
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
    made = {
        "project": project,
        "script": project / "deploy" / "sandbox-runner.sh",
        "client": client,
        "home": home,
        "calls": tmp_path / "calls",
        "state": tmp_path / "engine",
        "table": tmp_path / "image-table.json",
    }
    _write_the_engine(made, an_engine())
    made["identity"] = _the_identity_the_other_engine_recorded(made, made["engine"])
    return made


def _settings(sandbox, **extra):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(sandbox["home"]),
        "SANDBOX_DOCKER": str(sandbox["client"]),
        "STANDIN_CALLS": str(sandbox["calls"]),
        "STANDIN_STATE": str(sandbox["state"]),
        "STANDIN_TABLE": str(sandbox["table"]),
        "FORGE_IMAGE": IMAGE,
        # Recorded by the OTHER kind of engine; checked here against this one.
        "FORGE_IMAGE_IDENTITY": sandbox["identity"],
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


def _what_was_started(sandbox):
    """Every container start the engine saw: the name, and what it resolved."""
    ledger = sandbox["state"] / "what-was-started.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text().splitlines() if line]


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

    def test_nothing_to_check_the_image_against(self, sandbox):
        result = _run(sandbox, FORGE_IMAGE_IDENTITY="")
        assert result.returncode == 2
        assert "FORGE_IMAGE_IDENTITY is not set" in result.stdout

    def test_the_older_layers_only_setting_is_refused_by_name(self, sandbox):
        """A machine still carrying the stage 4d setting is told, not obeyed.

        FORGE_IMAGE_CONTENT_ID hashed the layer list alone, so an image could
        keep every layer and still have been reconfigured after it was
        reviewed. Falling back to it would be worse than refusing; being
        silent about it would be worse still.
        """
        result = _run(
            sandbox,
            FORGE_IMAGE_IDENTITY="",
            FORGE_IMAGE_CONTENT_ID="a-value-from-the-old-setting",
        )
        assert result.returncode == 2
        assert "FORGE_IMAGE_CONTENT_ID" in result.stdout
        assert "LAYERS alone" in result.stdout
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_the_image_is_not_in_the_sandbox(self, sandbox):
        result = _run(sandbox, STANDIN_NO_IMAGE="1")
        assert result.returncode == 2
        assert "is not in this sandbox's own engine" in result.stdout
        assert "Hand it in first" in result.stdout
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_a_different_image_under_the_same_name(self, sandbox):
        wrong = "0" * 64
        result = _run(sandbox, FORGE_IMAGE_IDENTITY=wrong)
        assert result.returncode == 2
        # The sentence names BOTH, so an operator can see which is which.
        assert wrong in result.stdout
        assert sandbox["identity"] in result.stdout
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
        assert sandbox["identity"] in result.stdout
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
        # BY THE ID THIS ENGINE HOLDS THE CHECKED IMAGE UNDER, never by the
        # tag: a tag is a name and a name can be moved (stage 4f).
        assert all(ENGINE_ID in call for call in runs)
        assert not any(IMAGE in call for call in runs)
        assert any("--name forge-sandbox-helper" in call for call in runs)
        assert any("--name forge-sandbox-runner" in call for call in runs)
        assert [one["resolved"] for one in _what_was_started(sandbox)] == [
            ENGINE_ID,
            ENGINE_ID,
        ]

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
            # Three read-write folders of the sandbox's own home, each bound
            # at the same path it has in the sandbox: the receipts root, the
            # per-build worktree base and the deploy helper's executor notes.
            # (The runner also gets its graph declaration from in there,
            # read-only, at a path of its own — that one is not a folder the
            # two containers share.)
            shared = [
                word
                for word in bound
                if word.startswith(home) and word.endswith(":rw")
            ]
            assert len(shared) == 3, call
            for word in shared:
                inside, outside = word[: -len(":rw")].split(":", 1)
                assert inside == outside
            assert "--env FORGE_RECEIPTS_DIR" in call
            assert "--env FORGE_AUTOBUILD_WORKTREE_BASE" in call
            assert "--env FORGE_DEPLOY_NOTES_DIR" in call

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


# ---------------------------------------------------------------------------
def _the_supervisor_record(sandbox) -> Path:
    """Where the bootstrap keeps the number of the supervisor it started."""
    which = hashlib.sha256(str(sandbox["project"]).encode()).hexdigest()
    return sandbox["home"] / ".forge-runner" / which / "supervisor"


def _birth_time_of(pid: int) -> str:
    """What the system says about when a process was born: field 22."""
    statline = Path(f"/proc/{pid}/stat").read_text()
    return statline.split(") ", 1)[1].split()[19]


class TestAnUnreadableEngineIsNotAnEmptyEngine:
    """Codex's first finding, 24 September 2026, as the four cases it asked for.

    ``docker ps`` answering nothing and ``docker ps`` not answering at all look
    identical if the exit status is thrown away — and stage 4d threw it away.
    A stop then reported both containers gone while both were running, and the
    host-side service, which starts nothing on top of a stop that failed, was
    handed the successful stop its start gate requires.
    """

    def _two_containers_running(self, sandbox):
        sandbox["state"].mkdir(parents=True, exist_ok=True)
        for name in ("forge-sandbox-helper", "forge-sandbox-runner"):
            (sandbox["state"] / name).write_text("made\n")
            (sandbox["state"] / (name + ".running")).write_text("up\n")

    def _a_record_left_behind(self, sandbox):
        record = _the_supervisor_record(sandbox)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("999999999 0\n")
        return record

    def test_an_engine_that_will_not_answer_at_all_is_a_non_zero_stop(
        self, sandbox
    ):
        self._two_containers_running(sandbox)
        record = self._a_record_left_behind(sandbox)

        stopped = _run(sandbox, "stop", STANDIN_PS_FAILS_FROM="1")

        assert stopped.returncode == 5, stopped.stdout
        assert "stopped:" not in stopped.stdout
        assert "would not say what is running in it" in stopped.stdout
        # The plain reason, in the engine's own words, not a guess at it.
        assert "Cannot connect to the Docker daemon" in stopped.stdout
        # Both are still there, and the record is KEPT: it is what the next
        # stop needs to find the supervisor again.
        assert (sandbox["state"] / "forge-sandbox-helper.running").exists()
        assert (sandbox["state"] / "forge-sandbox-runner.running").exists()
        assert record.exists()

    def test_an_engine_that_stops_answering_during_the_confirmation(
        self, sandbox
    ):
        """The removal was asked for; whether it worked is now unknowable."""
        self._two_containers_running(sandbox)
        record = self._a_record_left_behind(sandbox)

        # The listings before and during the removal answer; the confirmation
        # afterwards does not.
        stopped = _run(sandbox, "stop", STANDIN_PS_FAILS_FROM="4")

        assert stopped.returncode == 5, stopped.stdout
        assert "stopped:" not in stopped.stdout
        assert "would not say whether they had" in stopped.stdout
        assert "A stop that cannot be seen to have worked is not a stop" in (
            stopped.stdout
        )
        assert record.exists()

    def test_an_engine_that_answers_and_holds_nothing_is_a_clean_stop(
        self, sandbox
    ):
        """The case that must NOT be confused with the two above."""
        stopped = _run(sandbox, "stop")
        assert stopped.returncode == 0
        assert "stopped:" in stopped.stdout

    def test_a_real_stop_of_two_real_containers_still_ends_zero(self, sandbox):
        self._two_containers_running(sandbox)
        record = self._a_record_left_behind(sandbox)

        stopped = _run(sandbox, "stop")

        assert stopped.returncode == 0, stopped.stdout
        assert "stopped:" in stopped.stdout
        assert not (sandbox["state"] / "forge-sandbox-helper").exists()
        assert not (sandbox["state"] / "forge-sandbox-runner").exists()
        # Nothing to recover from, so the record goes.
        assert not record.exists()

    def test_the_supervisor_does_not_replace_a_container_it_cannot_see(
        self, sandbox
    ):
        """An engine that will not answer is not a container that died.

        Tearing one down and making another on the strength of a failed
        question would throw away a container that is very probably running
        perfectly well behind an engine that is merely busy or restarting.
        """
        process = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(
                sandbox,
                # The two listings before each start answer; every listing the
                # watch makes afterwards does not.
                STANDIN_PS_FAILS_FROM="3",
                SANDBOX_RUNNER_RESTART_SECONDS="0.2",
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
            time.sleep(2)
            assert len([c for c in _calls(sandbox) if c.startswith("run ")]) == 2
        finally:
            process.terminate()
            said = process.communicate(timeout=30)[0]
        assert "would not say whether the deploy helper is running" in said
        assert "nothing was changed" in said


class TestTheRecordMustNameThisCheckoutsSupervisor:
    """Codex's second finding: a number on its own is not an identity.

    A supervisor that died leaves its record behind, the sandbox goes on
    making processes, and sooner or later something unrelated is given that
    number. Stage 4d signalled whatever the record named.
    """

    def test_a_stale_record_does_not_signal_the_process_it_names(self, sandbox):
        """The process here is this test's own sleep and nothing else's."""
        unrelated = subprocess.Popen(["sleep", "60"])
        try:
            record = _the_supervisor_record(sandbox)
            record.parent.mkdir(parents=True, exist_ok=True)
            # Its number, and a birth time that is not its own.
            record.write_text(f"{unrelated.pid} 0\n")

            stopped = _run(sandbox, "stop")

            assert stopped.returncode == 0, stopped.stdout
            assert "is not a running supervisor of this checkout" in stopped.stdout
            assert "nothing was signalled" in stopped.stdout
            assert unrelated.poll() is None, (
                "an unrelated process was killed because a stale record "
                "happened to name its number"
            )
        finally:
            unrelated.kill()
            unrelated.wait(timeout=10)

    def test_a_process_of_the_right_age_that_is_not_this_bootstrap(
        self, sandbox
    ):
        """Both questions have to be answered, not just the first.

        This one's birth time is written down correctly — what it is not is
        this checkout's bootstrap.
        """
        unrelated = subprocess.Popen(["sleep", "60"])
        try:
            record = _the_supervisor_record(sandbox)
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(f"{unrelated.pid} {_birth_time_of(unrelated.pid)}\n")

            stopped = _run(sandbox, "stop")

            assert stopped.returncode == 0, stopped.stdout
            assert "is not a running supervisor of this checkout" in stopped.stdout
            assert unrelated.poll() is None
        finally:
            unrelated.kill()
            unrelated.wait(timeout=10)

    def test_a_matching_supervisor_that_will_not_go_is_not_a_stop(self, sandbox):
        """A live supervisor would make the containers again afterwards.

        So a stop that signalled one and did not see it go says so and ends
        non-zero, rather than removing containers that are about to come
        straight back and calling it a stop. The stand-in here is a process of
        this test's own that runs under this bootstrap's name and ignores the
        signal — which is what a wedged supervisor looks like from outside.
        """
        deaf = subprocess.Popen(
            # The trailing `:` keeps this a shell running the name it was
            # given: without it bash hands its own place straight to `sleep`
            # and the process is no longer running this bootstrap at all.
            ["bash", "-c", "trap '' TERM; sleep 60; :", str(sandbox["script"])]
        )
        try:
            record = _the_supervisor_record(sandbox)
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(f"{deaf.pid} {_birth_time_of(deaf.pid)}\n")
            sandbox["state"].mkdir(parents=True, exist_ok=True)
            for name in ("forge-sandbox-helper", "forge-sandbox-runner"):
                (sandbox["state"] / name).write_text("made\n")

            # A second of patience here, where the shipped default is thirty:
            # a supervisor removes both containers on its way out and Docker
            # gives each one ten seconds to go, so the default has to cover an
            # ordinary shutdown (found by running it in a sandbox, 24
            # September 2026).
            stopped = _run(
                sandbox, "stop", SANDBOX_RUNNER_STOP_PATIENCE_SECONDS="1"
            )

            assert stopped.returncode == 5, stopped.stdout
            assert "stopped:" not in stopped.stdout
            assert "1 seconds later it is still running" in stopped.stdout
            assert "makes the two containers again" in stopped.stdout
            # Nothing was removed on top of a supervisor that would remake it.
            assert (sandbox["state"] / "forge-sandbox-helper").exists()
            assert record.exists()
        finally:
            deaf.kill()
            deaf.wait(timeout=10)

    def test_the_default_patience_covers_two_container_stops(self):
        """Thirty seconds, because twenty of them are an ordinary shutdown."""
        script = BOOTSTRAP.read_text()
        assert 'SANDBOX_RUNNER_STOP_PATIENCE_SECONDS:-30' in script

    def test_the_record_it_writes_is_its_own_birth_time(self, sandbox):
        """Not the wall clock, which any later process can be made to match."""
        process = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(sandbox),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            record = _the_supervisor_record(sandbox)
            for _ in range(100):
                if record.exists():
                    break
                time.sleep(0.1)
            owner, born = record.read_text().split()
            assert int(owner) == process.pid
            assert born == _birth_time_of(process.pid)
        finally:
            process.terminate()
            process.wait(timeout=30)


class TestTheImageIsCheckedByWhatItIsAndNotByItsLayers:
    """Codex's third finding: layers are the filesystem, not the image.

    The OCI image configuration specification keeps runtime configuration —
    environment, entry point, command, user, working directory, labels — apart
    from the layers on purpose. Two images with the same filesystem and two
    copied labels can do entirely different things.
    """

    def _an_engine_holding(self, sandbox, image, key="the-image-in-the-sandbox"):
        engine = an_engine(images={key: image}, tags={IMAGE: key})
        _write_the_engine(sandbox, engine)
        return engine

    def test_the_same_layers_with_a_changed_configuration_are_refused(
        self, sandbox
    ):
        """The reviewer's drive A, as a positive assertion."""
        recorded_when_it_was_reviewed = sandbox["identity"]
        self._an_engine_holding(
            sandbox,
            an_image(env=["A_SETTING_BAKED_INTO_THE_IMAGE=changed-after-review"]),
        )

        result = _run(
            sandbox,
            FORGE_IMAGE_IDENTITY=recorded_when_it_was_reviewed,
            FORGE_RELEASE_VERSION=VERSION,
            FORGE_RELEASE_MANIFEST_SHA256=MANIFEST,
        )

        assert result.returncode == 2, result.stdout
        assert recorded_when_it_was_reviewed in result.stdout
        assert "runtime configuration" in result.stdout
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_a_changed_entry_point_alone_is_refused(self, sandbox):
        """Same layers, same labels, same environment; one word different."""
        recorded = sandbox["identity"]
        self._an_engine_holding(
            sandbox, an_image(entrypoint=["something-else-entirely"])
        )
        result = _run(sandbox, FORGE_IMAGE_IDENTITY=recorded)
        assert result.returncode == 2
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_half_an_answer_is_refused_rather_than_hashed(self, sandbox):
        """A hash of half the truth compares perfectly well with another one.

        Found on 24 September 2026 by running the stage 4d reviewer's own
        stand-in engine against the fixed script: that engine answers the
        identity question with the layer list alone, and a script that simply
        hashed whatever came back would have been comparing layer lists again
        without anybody noticing. The document's first line is a fixed word,
        so an answer that is not the whole document is refused.
        """
        result = _run(sandbox, STANDIN_ANSWERS_WITH_HALF_A_DOCUMENT="1")
        assert result.returncode == 2
        assert "did not begin" in result.stdout or "does not begin" in result.stdout
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_a_changed_platform_alone_is_refused(self, sandbox):
        recorded = sandbox["identity"]
        self._an_engine_holding(
            sandbox, an_image(architecture="a-different-architecture")
        )
        result = _run(sandbox, FORGE_IMAGE_IDENTITY=recorded)
        assert result.returncode == 2
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    @pytest.mark.parametrize("style", ["classic", "containerd"])
    def test_the_unchanged_image_passes_on_either_kind_of_engine(
        self, sandbox, style
    ):
        """The point of the identity: it crosses, and the id does not.

        The setting is worked out on an engine of one kind and checked on an
        engine of the other. The two disagree about what to CALL the image —
        one answers with the digest of its config, the other with the digest
        of its manifest — and agree about what the image IS.
        """
        engine = an_engine(style=style)
        _write_the_engine(sandbox, engine)
        recorded_elsewhere = _the_identity_the_other_engine_recorded(sandbox, engine)

        result = _run(
            sandbox,
            FORGE_IMAGE_IDENTITY=recorded_elsewhere,
            FORGE_RELEASE_VERSION=VERSION,
            FORGE_RELEASE_MANIFEST_SHA256=MANIFEST,
            SANDBOX_RUNNER_BOOTSTRAP_ONLY="1",
        )

        assert result.returncode == 0, result.stdout
        assert recorded_elsewhere in result.stdout
        # And the local name really is the other one of the two.
        assert (
            ENGINE_ID if style == "classic" else ENGINE_ID_THE_OTHER_WAY
        ) in result.stdout

    def test_the_two_kinds_of_engine_name_the_same_image_differently(
        self, sandbox
    ):
        """If they ever agreed, the test above would be proving nothing."""
        assert ENGINE_ID != ENGINE_ID_THE_OTHER_WAY
        one = _the_identity_the_other_engine_recorded(sandbox, an_engine("classic"))
        other = _the_identity_the_other_engine_recorded(
            sandbox, an_engine("containerd")
        )
        assert one == other

    def test_a_tag_moved_after_the_check_cannot_select_another_image(
        self, sandbox
    ):
        """The reviewer's drive B, as a positive assertion.

        The engine here moves the tag onto another image the moment the
        bootstrap has finished inspecting it — the race a registry push, or
        anything else that retags, would give you. Both containers must still
        be made from the image that was checked.
        """
        engine = an_engine(
            images={
                "the-reviewed-release": an_image(),
                "the-replacement": an_image(
                    id_classic=ANOTHER_IMAGE_ID,
                    id_containerd=ANOTHER_IMAGE_ID,
                    env=["A_SETTING_BAKED_INTO_THE_IMAGE=not-the-reviewed-one"],
                ),
            },
            tags={IMAGE: "the-reviewed-release"},
            move_tag_to="the-replacement",
        )
        _write_the_engine(sandbox, engine)

        process = subprocess.Popen(
            ["bash", str(sandbox["script"])],
            env=_settings(
                sandbox,
                FORGE_IMAGE_IDENTITY=_the_identity_the_other_engine_recorded(
                    sandbox, engine
                ),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                if len(_what_was_started(sandbox)) >= 2:
                    break
                time.sleep(0.1)
            started = _what_was_started(sandbox)
        finally:
            process.terminate()
            process.wait(timeout=30)

        assert len(started) == 2, started
        for one in started:
            assert one["reference"] == ENGINE_ID, (
                "a container was started by a name that can be moved: "
                f"{one}"
            )
            assert one["resolved"] == ENGINE_ID
            assert one["resolved"] != ANOTHER_IMAGE_ID


class TestTheDeployHelpersNotesOutliveItsContainer:
    """Codex's fourth item: the executor's notes are durable state too.

    The helper writes one note per deployment target before it runs anything —
    the target, the build, the counter it was granted, the process group it
    started. A note is what stops a helper that has just been replaced coming
    back to an empty slot while the deploy command it started is still
    running. The factory's own default puts them inside the container, and the
    supervisor in here throws containers away.
    """

    def test_a_named_folder_is_made_and_bound_into_both_with_its_name(
        self, sandbox, tmp_path
    ):
        notes = tmp_path / "where-the-notes-go"
        runs = TestTheFoldersBothContainersShare._runs_of_a_started_bootstrap(
            sandbox, FORGE_DEPLOY_NOTES_DIR=str(notes)
        )
        assert len(runs) == 2
        assert notes.is_dir(), (
            "made before anything started, by this script's own user: Docker "
            "would otherwise make the bind source itself, owned by root"
        )
        for call in runs:
            assert f"--volume {notes}:{notes}:rw" in call
            assert "--env FORGE_DEPLOY_NOTES_DIR" in call

    def test_the_helpers_own_start_line_carries_both(self, sandbox, tmp_path):
        """Said of the helper alone, because the helper is what writes them."""
        notes = tmp_path / "where-the-notes-go"
        runs = TestTheFoldersBothContainersShare._runs_of_a_started_bootstrap(
            sandbox, FORGE_DEPLOY_NOTES_DIR=str(notes)
        )
        helper = [call for call in runs if "--name forge-sandbox-helper" in call]
        assert len(helper) == 1
        assert f"--volume {notes}:{notes}:rw" in helper[0]
        assert "--env FORGE_DEPLOY_NOTES_DIR" in helper[0]

    def test_with_no_setting_a_folder_of_the_sandbox_is_used(self, sandbox):
        """Unset, the factory's own default is a folder inside the container."""
        runs = TestTheFoldersBothContainersShare._runs_of_a_started_bootstrap(
            sandbox
        )
        assert len(runs) == 2
        home = str(sandbox["home"])
        for call in runs:
            assert "--env FORGE_DEPLOY_NOTES_DIR" in call
            bound = [
                word
                for before, word in zip(call.split(), call.split()[1:])
                if before == "--volume"
            ]
            notes = [
                word
                for word in bound
                if word.startswith(home) and "deploy-executor-notes" in word
            ]
            assert len(notes) == 1, call
            inside, outside = notes[0][: -len(":rw")].split(":", 1)
            assert inside == outside

    def test_the_start_log_says_where_the_notes_went(self, sandbox):
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
        assert "no folder was named for the deploy helper's executor notes" in said
        assert "the deploy helper's executor notes" in said

    def test_a_notes_folder_that_cannot_be_made_is_refused_by_name(
        self, sandbox, tmp_path
    ):
        in_the_way = tmp_path / "this-one-is-a-file"
        in_the_way.write_text("not a folder\n")
        wanted = in_the_way / "under-a-file"

        result = _run(sandbox, FORGE_DEPLOY_NOTES_DIR=str(wanted))

        assert result.returncode == 2
        assert "FORGE_DEPLOY_NOTES_DIR" in result.stdout
        assert str(wanted) in result.stdout
        # Never handed in with nothing bound under it.
        assert not any(line.startswith("run ") for line in _calls(sandbox))

    def test_the_header_says_where_the_notes_live(self):
        header = BOOTSTRAP.read_text().split("WHAT IT NEVER DOES", 1)[0]
        assert "FORGE_DEPLOY_NOTES_DIR" in header
        assert "executor notes" in header


class TestTheTwoScriptsAskTheEnginesTheSameQuestion:
    """One document, two scripts, or the check means nothing.

    The machine outside records the identity with
    ``deploy/estate/hand-release-image-to-sandbox.sh`` and the bootstrap in
    here checks it. If the two ever ask for different fields, or the same
    fields in a different order, every hand-in would end in a refusal nobody
    could explain.
    """

    HAND_IN = (
        Path(__file__).resolve().parents[3]
        / "deploy"
        / "estate"
        / "hand-release-image-to-sandbox.sh"
    )

    def test_both_carry_the_same_format_string(self):
        assert self.HAND_IN.exists(), self.HAND_IN
        assert _the_identity_format(self.HAND_IN) == IDENTITY_FORMAT

    def test_the_document_covers_configuration_platform_and_filesystem(self):
        for wanted in (
            "{{.Architecture}}",
            "{{.Os}}",
            ".RootFS.Layers",
            ".Config.Env",
            ".Config.Entrypoint",
            ".Config.Cmd",
            ".Config.User",
            ".Config.WorkingDir",
            ".Config.Labels",
            ".Config.ExposedPorts",
            ".Config.Volumes",
            ".Config.StopSignal",
        ):
            assert wanted in IDENTITY_FORMAT, wanted
        # Nothing the engine says ABOUT the image rather than reads FROM it.
        for unwanted in ("{{.Id}}", "RepoDigests", "RepoTags", "{{.Created}}"):
            assert unwanted not in IDENTITY_FORMAT, unwanted
