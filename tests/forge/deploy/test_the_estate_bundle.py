"""The estate bundle renders, carries nothing of this machine, and keeps its
promises about what starts in what order.

WHY THIS TEST EXISTS (24 September 2026, stage 4a). The estate bundle is what
the design calls "the only thing a new machine clones": it composes Forge's own
compose files and adds the parts Forge does not own — the bus, and the one-shot
that provisions the bus's storage before the coordinator is allowed to start.

Three things can go wrong quietly, and each has already gone wrong once in this
rollout:

* a name is added to a compose file and no line is added to the example env, so
  the bundle will not render on a clean machine. Rendering WITH the example is
  what catches that;
* a path or an address belonging to one machine reaches the rendered document
  through an ``include`` or a variable. The check is made on the RENDERED
  bundle, which is what ``docker compose`` would act on;
* the example env's image tags trail the release manifest. Two reviews in one
  day caught exactly that on Forge's own example, so all four of the estate's
  image lines are held to the manifest here.

Nothing here names a language, a test runner or any project's layout: it reads
one rendered compose document and the output of one shell script.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

#: forge/deploy/estate — the bundle this test reads.
ESTATE = Path(__file__).resolve().parents[3] / "deploy" / "estate"

#: forge/release/manifest.yaml — the release every image tag is held to.
MANIFEST = Path(__file__).resolve().parents[3] / "release" / "manifest.yaml"

_AN_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

#: The one an image legitimately renders: a service binding every address of
#: its OWN container.
_ALLOWED_ADDRESSES = ("0.0.0.0",)

#: The IETF's documentation block (RFC 5737). The example env uses it for the
#: factory gateway address so the bundle renders and can be checked; it belongs
#: to nobody and reaches nothing.
_DOCUMENTATION_BLOCK = "192.0.2."

#: The homes the images make for their own unprivileged users. They are the
#: same on every machine, which is the whole point of them.
_IN_IMAGE_HOMES = ("/home/forge/", "/home/publisher/")

#: What the render inherits from whoever runs it. Compose gives the SHELL's
#: environment precedence over ``--env-file``, so the render is made with
#: almost nothing in the environment and what the example says is what is
#: checked.
_ONLY_THESE_ARE_INHERITED = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "XDG_RUNTIME_DIR",
)

#: The bus's eight account passwords never appear in a file in the bundle: they
#: are passed into the command from a child process, which is the bus
#: repository's own arrangement. The render therefore supplies obvious
#: placeholders, exactly as a real run supplies real ones — and the point of
#: the arrangement is that these are the only place a value can come from.
_THE_PASSWORDS_COME_FROM_THE_CALLER = {
    name: "placeholder-not-a-real-password"
    for name in (
        "ADMIN_NATS_PASSWORD",
        "RICH_NATS_PASSWORD",
        "JAMES_NATS_PASSWORD",
        "MARK_NATS_PASSWORD",
        "FORGE_NATS_PASSWORD",
        "FLEET_MEMORY_NATS_PASSWORD",
        "GUARDKIT_NATS_PASSWORD",
        "JARVIS_NATS_PASSWORD",
    )
}


def _rendered(*extra: str) -> str:
    """``docker compose config`` of the estate, with the example env."""
    bare = {
        name: os.environ[name]
        for name in _ONLY_THESE_ARE_INHERITED
        if name in os.environ
    }
    bare.update(_THE_PASSWORDS_COME_FROM_THE_CALLER)
    done = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "forge-estate-bundle-check",
            "--env-file",
            ".env.example",
            "--file",
            "compose.yaml",
            *extra,
            "config",
        ],
        cwd=ESTATE,
        env=bare,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, (
        "the estate bundle would not render with its own .env.example:\n"
        f"{done.stderr}"
    )
    return done.stdout


@pytest.fixture(scope="module")
def rendered() -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    return _rendered()


@pytest.fixture(scope="module")
def rendered_with_the_sandbox() -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    return _rendered("--profile", "sandbox")


class TestTheEstateRenders:
    def test_it_renders_at_all_from_the_example_env(self, rendered: str) -> None:
        """Every name the estate needs has a line in ``.env.example``."""
        assert "services:" in rendered

    def test_it_has_forges_services_and_the_bus(self, rendered: str) -> None:
        """Composed in, not copied: Forge's three services are here because the
        estate includes Forge's own file, and the bus and its one-shot because
        the estate adds them."""
        for service in (
            "coordinator:",
            "answer-service:",
            "forge-publisher:",
            "nats:",
            "nats-provision:",
        ):
            assert service in rendered, f"{service} is missing from the estate"

    def test_the_sandbox_service_does_not_start_by_accident(
        self, rendered: str, rendered_with_the_sandbox: str
    ) -> None:
        """A machine has one sandbox service per project sandbox and a cloud
        machine may have none, so it sits behind a profile: an ordinary ``up``
        leaves it alone and nobody has to comment a service out."""
        assert "sandbox-runner:" not in rendered
        assert "sandbox-runner:" in rendered_with_the_sandbox


class TestNothingOfThisMachineIsInIt:
    def test_no_address_but_the_documentation_one(self, rendered: str) -> None:
        found = {
            address
            for address in _AN_ADDRESS.findall(rendered)
            if address not in _ALLOWED_ADDRESSES
            and not address.startswith(_DOCUMENTATION_BLOCK)
        }
        assert not found, (
            "the rendered estate carries addresses that belong to a machine: "
            f"{sorted(found)}. Every address is a setting NAME; the only "
            "literals allowed here are 0.0.0.0 and the documentation block."
        )

    def test_no_loopback_address(self, rendered: str) -> None:
        assert "127.0.0.1" not in rendered

    def test_no_home_directory_but_the_images_own(self, rendered: str) -> None:
        for line in rendered.splitlines():
            if "/home/" not in line:
                continue
            assert any(home in line for home in _IN_IMAGE_HOMES), (
                f"the rendered estate carries a home directory: {line.strip()}"
            )

    def test_no_mac_home_directory(self, rendered: str) -> None:
        assert "/Users/" not in rendered

    def test_not_this_machines_name(self, rendered: str) -> None:
        name = socket.gethostname().split(".")[0]
        if not name or len(name) < 3:
            pytest.skip("this machine's name is too short to look for safely")
        assert name.lower() not in rendered.lower()

    def test_the_sandbox_service_brings_none_either(
        self, rendered_with_the_sandbox: str
    ) -> None:
        found = {
            address
            for address in _AN_ADDRESS.findall(rendered_with_the_sandbox)
            if address not in _ALLOWED_ADDRESSES
            and not address.startswith(_DOCUMENTATION_BLOCK)
        }
        assert not found, f"the sandbox service brought an address: {sorted(found)}"
        for line in rendered_with_the_sandbox.splitlines():
            if "/home/" not in line:
                continue
            assert any(home in line for home in _IN_IMAGE_HOMES), (
                "the sandbox service carries a home directory, which is where "
                f"the daemon's socket really lives on most machines: {line.strip()}"
            )


class TestWhatTheEstatePromises:
    def test_nothing_runs_on_the_host_network(self, rendered: str) -> None:
        assert "network_mode: host" not in rendered

    def test_two_published_ports_and_only_two(self, rendered: str) -> None:
        """Every crossing of the sandbox boundary goes through the factory
        gateway address, and there are exactly two of them in this bundle: the
        read-only answer service, and the bus. Everything else talks by service
        name."""
        assert rendered.count("mode: ingress") == 2
        assert "target: 8126" in rendered, "the answer service is not published"
        assert "target: 4222" in rendered, "the bus is not published"

    def test_the_coordinator_waits_for_the_bus_to_be_provisioned(
        self, rendered: str
    ) -> None:
        """A bus that is merely running is not enough. Without its
        ``agent-registry`` bucket and its ``PIPELINE`` stream the coordinator
        restarts in a loop showing a programmer's error — met by a reviewer on
        24 September 2026 against a bus nobody had provisioned. The one-shot
        has to finish successfully first, and that is a line in the file rather
        than a sentence somebody has to remember."""
        block = rendered.split("coordinator:", 1)[1].split("\n  answer", 1)[0]
        assert "nats-provision" in block, (
            "the coordinator does not wait for the bus to be provisioned"
        )
        assert "service_completed_successfully" in block, (
            "the coordinator waits for the one-shot, but not for it to SUCCEED"
        )

    def test_the_buss_own_files_come_from_the_release_not_from_a_disk(
        self, rendered: str
    ) -> None:
        """The bus's config and provisioning scripts travel in a volume the
        build step fills from the bus repository at its pinned commit. A folder
        on a disk would have put a machine's path into the running estate."""
        assert "bus-source" in rendered
        assert "external: true" in rendered

    def test_the_settings_volume_is_read_only(self, rendered: str) -> None:
        block = rendered.split("source: forge-settings", 1)
        assert len(block) == 2, "the settings volume is not mounted anywhere"
        assert "read_only: true" in block[1][:200]


def _the_manifests_version() -> str:
    match = re.search(r"^version:\s*(\S+)\s*$", MANIFEST.read_text(), re.M)
    assert match, "the release manifest has no version line"
    return match.group(1)


class TestTheExampleNamesTheReleaseThatExists:
    """Forge's own example env named a release the manifest had moved past
    twice in one day (24 September 2026) — once one that could not run the
    shipped settings, once one that was never built. The estate has FOUR image
    lines and a volume named after the release, so all five are held here and
    the manifest cannot move without them."""

    def test_every_image_line_names_the_manifests_release(self) -> None:
        version = _the_manifests_version()
        env = (ESTATE / ".env.example").read_text()
        for line in (
            f"FORGE_IMAGE=forge:{version}",
            f"FORGE_PUBLISHER_IMAGE=forge-publisher:{version}",
            f"NATS_IMAGE=factory-nats:{version}",
            f"NATS_PROVISION_IMAGE=factory-nats-provision:{version}",
            f"BUS_SOURCE_VOLUME=factory-bus-source-{version}",
        ):
            assert f"{line}\n" in env, (
                f"deploy/estate/.env.example does not carry '{line}'. The whole "
                f"estate moves as one release, and the manifest is at {version}."
            )

    def test_the_bus_image_name_agrees_with_the_pins_file(self) -> None:
        """The tag in the example is built out of the name in the pins file, so
        the two cannot drift into naming different images."""
        pins = (ESTATE / "estate-pins.conf").read_text()
        assert "BUS_IMAGE_NAME=factory-nats\n" in pins
        assert "BUS_PROVISION_IMAGE_NAME=factory-nats-provision\n" in pins
        assert "BUS_SOURCE_VOLUME_NAME=factory-bus-source\n" in pins


# ---------------------------------------------------------------------------
# THE ITEM LIST
#
# Section 3 of docs/factory-containerisation-design-pass-2026-09-23.md (in the
# ai-transition repository) says what a fresh machine must have before the
# sandbox comes up, and what must be true after it has started. There are seven
# items before and two after. They are written out here, in that order, with
# the design's own sentence beside each, so that dropping one from the check is
# a failing test rather than a quiet omission.
#
# The check may say MORE than the design does — it does, about the bus's
# storage, the coordinator, the answer service and the publisher, each of which
# is a part of the design's item 8 said separately. What it may not do is say
# less.
# ---------------------------------------------------------------------------
THE_DESIGNS_ITEMS = {
    "1": (
        "machine-qualifies-for-a-sandbox",
        "a supported operating system and processor architecture, hardware "
        "virtualisation available to this user",
    ),
    "2": (
        "docker-is-here-and-answering",
        "Docker is installed and its daemon answers; if the sandbox tool "
        "requires a Docker sign-in, one is present",
    ),
    "3": (
        "sandbox-tool-and-its-daemon",
        "the sbx tool is installed, its version matches the one the release "
        "was proven against, and its daemon answers on its socket",
    ),
    "4": (
        "release-images-present-or-fetchable",
        "the release images are present, or can be fetched, at the digests "
        "the estate bundle names",
    ),
    "5": (
        "volumes-and-disk-room",
        "the declared volumes exist or can be created, and the disk has room",
    ),
    "6": (
        "secret-files-present-and-private",
        "every secret file named in the env file exists, and each is readable "
        "by its own service's user and by nobody else",
    ),
    "7": (
        "every-setting-name-has-a-value",
        "every setting name the factory's own list requires has a value in "
        "the env file",
    ),
    "8": (
        "the-bus-answers",
        "the bus, the memory service and the model seat each answer at the "
        "address the env file gives",
    ),
    "9": (
        "the-answer-service-from-inside-a-sandbox",
        "the coordinator's answer service answers from inside a sandbox at "
        "the address the sandbox profile allows",
    ),
}


class TestTheCheckStillHasEveryItemTheDesignAsksFor:
    @pytest.fixture(scope="class")
    def items(self) -> dict[str, str]:
        done = subprocess.run(
            [str(ESTATE / "estate-check"), "--items"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        found: dict[str, str] = {}
        for line in done.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3:
                found[parts[1]] = parts[2]
        return found

    def test_every_numbered_item_of_the_design_is_there_by_name(
        self, items: dict[str, str]
    ) -> None:
        for number, (name, sentence) in THE_DESIGNS_ITEMS.items():
            assert number in items, (
                f"estate-check no longer has item {number} of the design's "
                f"section 3: {sentence}"
            )
            assert items[number] == name, (
                f"estate-check's item {number} is called '{items[number]}' and "
                f"the design's item {number} is '{name}': {sentence}"
            )

    def test_the_items_before_and_after_are_not_mixed_up(self) -> None:
        """Two checks, not one, because some things must be true before
        anything starts and others can only be true after. The first draft of
        the design asked the bus to answer before the compose file had started
        it."""
        done = subprocess.run(
            [str(ESTATE / "estate-check"), "--items"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        modes = {}
        for line in done.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3:
                modes[parts[1]] = parts[0]
        for number in ("1", "2", "3", "4", "5", "6", "7"):
            assert modes[number] == "host", (
                f"item {number} must be checked BEFORE anything starts"
            )
        for number in ("8", "9"):
            assert modes[number] == "services", (
                f"item {number} can only be checked AFTER things have started"
            )
