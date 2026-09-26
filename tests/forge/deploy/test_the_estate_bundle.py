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

#: The bus's eight account passwords. They never appear in a file in the
#: bundle: they are passed into the command from a child process, which is the
#: bus repository's own arrangement. **Nothing below sets one**, and that is
#: the point — the estate hands each to the bus as a secret read from the
#: environment, so the bundle RENDERS without them and only STARTING needs
#: them. Until 24 September 2026 they were interpolated with ``:?`` and every
#: command that had to render the file refused in a clean shell, which made
#: ``estate-check services`` and ``factory-hello`` unusable as the README
#: documents them.
_THE_BUSS_ACCOUNT_PASSWORDS = (
    "ADMIN_NATS_PASSWORD",
    "RICH_NATS_PASSWORD",
    "JAMES_NATS_PASSWORD",
    "MARK_NATS_PASSWORD",
    "FORGE_NATS_PASSWORD",
    "FLEET_MEMORY_NATS_PASSWORD",
    "GUARDKIT_NATS_PASSWORD",
    "JARVIS_NATS_PASSWORD",
)


def _rendered(*extra: str) -> str:
    """``docker compose config`` of the estate, with the example env and with
    NO password of any kind in the environment."""
    bare = {
        name: os.environ[name]
        for name in _ONLY_THESE_ARE_INHERITED
        if name in os.environ
    }
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


def _service_block(rendered: str, service: str) -> str:
    """One service's lines out of the rendered document. A service's own lines
    are indented four spaces; anything less ends it."""
    after = rendered.split(f"\n  {service}:", 1)
    assert len(after) == 2, f"{service} is not in the rendered estate"
    kept: list[str] = []
    for line in after[1].splitlines():
        if line.strip() and not line.startswith("    "):
            break
        kept.append(line)
    return "\n".join(kept)


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

    def test_it_renders_with_no_password_in_the_environment(
        self, rendered: str
    ) -> None:
        """READING THE ESTATE NEEDS NO SECRET. The render above was made with
        nothing but PATH and Docker's own names in the environment, so the fact
        that it produced a document at all is the test: every command that
        reads a running estate — ``ps``, ``estate-check services``,
        ``factory-hello`` — has to render this bundle first, and on 24
        September 2026 all of them refused in a clean shell and reported
        healthy services as missing. Starting the estate still needs the
        values, and ``estate-check host`` checks that they are there."""
        for name in _THE_BUSS_ACCOUNT_PASSWORDS:
            assert name not in os.environ or not os.environ[name], (
                f"{name} is set in this test's own environment, so this test "
                "cannot show that the bundle renders without it"
            )
        assert "nats:" in rendered

    def test_no_password_reaches_a_container_environment(
        self, rendered: str
    ) -> None:
        """The bus's eight passwords reach it as FILES. The bus repository's
        own compose file puts them in ``environment:``, where ``docker
        inspect`` shows them to anybody who can reach the Docker daemon; the
        estate hands each one in as a secret instead and a wrapper puts them
        into the environment of the bus's own entrypoint process."""
        bus = _service_block(rendered, "nats")
        for name in _THE_BUSS_ACCOUNT_PASSWORDS:
            assert f"{name}:" not in bus, (
                f"the bus names {name} in its container environment, where "
                "'docker inspect' would show its value"
            )
        assert "/run/secrets" in rendered or "secrets:" in rendered

    def test_the_bus_still_reads_its_own_config(self, rendered: str) -> None:
        """NAMING AN ENTRYPOINT EMPTIES THE IMAGE'S OWN COMMAND. Compose drops
        it, and a nats-server started with no command reads no config at all:
        it listens on 4222, serves no monitoring route, holds no JetStream, and
        writes a contented log while the estate's health probe fails and every
        service that waits for the bus never starts. Met on 24 September 2026
        the first time the bus was given a wrapper."""
        bus = _service_block(rendered, "nats")
        assert "entrypoint:" in bus
        assert "/etc/nats/nats-server.conf" in bus.split("healthcheck:", 1)[0], (
            "the bus names an entrypoint and no command, so it will start with "
            "no configuration at all"
        )

    def test_the_provisioning_address_carries_no_credential(
        self, rendered: str
    ) -> None:
        """THE BLOCKER OF 24 SEPTEMBER 2026. The bus repository's two
        provisioning scripts print the address they are given, twice each, so
        an address of the ``nats://user:password@host`` form put the password
        into the one-shot's container log at every start, on every machine.
        The address it is given now carries no credential at all."""
        one_shot = _service_block(rendered, "nats-provision")
        for line in one_shot.splitlines():
            if "nats://" in line:
                address = line.split("nats://", 1)[1]
                assert "@" not in address.split()[0], (
                    "the one-shot that provisions the bus is given an address "
                    f"with a credential in it, and it prints that address: {line.strip()}"
                )

    def test_it_has_forges_services_the_bus_and_memory(self, rendered: str) -> None:
        """Composed in, not copied: Forge's three services are here because the
        estate includes Forge's own file, and the bus, its one-shot and the
        memory service's two containers because the estate adds them."""
        for service in (
            "coordinator:",
            "answer-service:",
            "forge-publisher:",
            "nats:",
            "nats-provision:",
            "memory:",
            "memory-relay:",
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

    def test_three_published_ports_and_only_three(self, rendered: str) -> None:
        """Every crossing of the sandbox boundary goes through the factory
        gateway address, and there are exactly three of them in this bundle:
        the read-only answer service, the bus, and the memory service — the
        three things section 7 of the design says a sandbox reaches.
        Everything else talks by service name, and the memory RELAY publishes
        nothing at all, because nothing calls it."""
        assert rendered.count("mode: ingress") == 3
        assert "target: 8126" in rendered, "the answer service is not published"
        assert "target: 4222" in rendered, "the bus is not published"
        assert "target: 8005" in rendered, "the memory service is not published"

    def test_the_memory_relay_publishes_nothing(self, rendered: str) -> None:
        relay = _service_block(rendered, "memory-relay")
        assert "ports:" not in relay, (
            "the memory relay publishes a port. Nothing calls it — it listens "
            "on the bus — so a published port is a way in and nothing else."
        )

    def test_memory_is_on_the_factory_network_and_no_other(
        self, rendered: str
    ) -> None:
        """Not the host's network, which is where both of them ran until 25
        September 2026, and not the publisher's, which only the coordinator
        joins."""
        for service in ("memory", "memory-relay"):
            block = _service_block(rendered, service)
            assert "factory" in block, f"{service} is not on the factory network"
            assert "forge-publisher-net" not in block, (
                f"{service} is on the publisher's own network, and only the "
                "coordinator may be"
            )

    def test_the_memory_relays_state_is_a_volume_and_not_a_folder(
        self, rendered: str
    ) -> None:
        """It bound ~/.local/state/fleet-memory, a folder under somebody's home
        directory. The design's inventory row says what that was: habit."""
        relay = _service_block(rendered, "memory-relay")
        assert "memory-state" in relay, (
            "the memory relay has no memory-state volume, so its progress "
            "marker has nowhere of its own to live"
        )
        assert "type: bind" not in relay, (
            "the memory relay binds a folder on this machine's disk"
        )

    def test_the_memory_store_address_never_reaches_a_container_environment(
        self, rendered: str
    ) -> None:
        """It carries the store's password, so it is a secret and travels as a
        file — the same way the bus's eight account passwords do, and
        deliberately NOT the way the coordinator's bus address still does."""
        for service in ("memory", "memory-relay"):
            block = _service_block(rendered, service)
            environment = block.split("environment:", 1)
            if len(environment) == 2:
                declared = environment[1].split("secrets:", 1)[0]
                assert "FLEET_MEMORY_PG_DSN:" not in declared, (
                    f"{service} names the store's address in its container "
                    "environment, where 'docker inspect' would show its password"
                )
            assert "memory_database_address" in block, (
                f"{service} is not given the store's address as a file"
            )

    def test_the_relays_bus_address_carries_no_credential(
        self, rendered: str
    ) -> None:
        """The memory repository's own compose file hands the relay a whole
        nats:// address with the password in it. Here the address and the
        account are plain and the password arrives as a file."""
        relay = _service_block(rendered, "memory-relay")
        for line in relay.splitlines():
            if "nats://" in line:
                address = line.split("nats://", 1)[1]
                assert "@" not in address.split()[0], (
                    "the memory relay is given a bus address with a credential "
                    f"in it: {line.strip()}"
                )
        assert "memory_bus_password" in relay, (
            "the memory relay is not given the bus password as a file"
        )

    def test_each_memory_service_writes_its_command_out(
        self, rendered: str
    ) -> None:
        """NAMING AN ENTRYPOINT EMPTIES THE IMAGE'S OWN COMMAND. Both of these
        services name one, because each is given its database address as a file
        rather than as a value anybody with the Docker daemon can read — so
        each has to write its command out, and a release proof holds those two
        lines to what the two images really say."""
        for service in ("memory", "memory-relay"):
            block = _service_block(rendered, service)
            assert "entrypoint:" in block, f"{service} names no entrypoint"
            assert "command:" in block, (
                f"{service} names an entrypoint and no command, so it would "
                "start with no command at all"
            )

    def test_the_coordinator_is_given_forges_own_memory_settings(
        self, rendered: str
    ) -> None:
        """THE BLOCKER OF 25 SEPTEMBER 2026, held shut.

        Forge reads the factory's memory itself, at gate time, out of the
        store — it does not use the ``memory`` service, which is what a Claude
        session asks over MCP. It reads five names
        (src/forge/adapters/fleet_memory/priors.py), and the live coordinator
        is given all five by ops/forge-prod-recreate.sh. This bundle gave it
        NONE, and Forge does not refuse that: it logs ``memory: OFF``, hands
        the gate an empty reader and answers its health route perfectly, so
        every check in the bundle still passed while the factory remembered
        nothing. Replacing forge-prod with this estate would have turned memory
        off in silence.
        """
        coordinator = _service_block(rendered, "coordinator")
        for name in (
            "FLEET_MEMORY_ENABLED:",
            "FLEET_MEMORY_EMBED_URL:",
            "FLEET_MEMORY_EMBED_MODEL:",
            "FLEET_MEMORY_EMBED_DIMS:",
        ):
            assert name in coordinator, (
                f"the coordinator is not given {name.rstrip(':')}, so its own "
                "memory would be off and nothing would say so"
            )
        assert "memory_database_address" in coordinator, (
            "the coordinator is not given the store's address, so with memory "
            "on it has nothing to read"
        )

    def test_the_coordinators_store_address_is_a_file_and_not_a_value(
        self, rendered: str
    ) -> None:
        """The store's address carries its password, so the coordinator gets it
        the same way the two memory containers do — as a file, put into its own
        process's environment by the wrapper and nowhere else."""
        coordinator = _service_block(rendered, "coordinator")
        environment = coordinator.split("environment:", 1)
        if len(environment) == 2:
            declared = environment[1].split("secrets:", 1)[0]
            assert "FLEET_MEMORY_PG_DSN:" not in declared, (
                "the coordinator names the store's address in its container "
                "environment, where 'docker inspect' would show its password"
            )
        assert "entrypoint:" in coordinator, (
            "the coordinator has no wrapper, so it could not be handed the "
            "store's address as a file"
        )
        assert "command:" in coordinator, (
            "the coordinator names an entrypoint and no command, so it would "
            "start with no command at all"
        )

    # -----------------------------------------------------------------
    # THE SLACK FRONT DOOR AND THE BUS GATEWAY (25 September 2026, stage 4c)
    # -----------------------------------------------------------------

    def test_the_front_door_and_the_gateway_are_in_the_estate(
        self, rendered: str
    ) -> None:
        """Both ran as host units out of a checkout and a virtual environment
        under a home directory. The design's inventory calls that habit."""
        for service in ("front-door", "bus-gateway"):
            assert f"  {service}:" in rendered, (
                f"the estate has no '{service}' service"
            )

    def test_the_two_jarvis_services_are_one_image(self, rendered: str) -> None:
        """One image per repository release, one start command per service —
        the shape the coordinator and the answer service already have. Two
        images built from one commit could drift; one cannot."""
        images = []
        for service in ("front-door", "bus-gateway"):
            block = _service_block(rendered, service)
            line = [ln for ln in block.splitlines() if ln.strip().startswith("image:")]
            assert line, f"{service} names no image"
            images.append(line[0].strip())
        assert images[0] == images[1], (
            f"the front door and the bus gateway are different images: {images}"
        )

    def test_the_two_jarvis_services_are_on_the_factory_network_and_no_other(
        self, rendered: str
    ) -> None:
        for service in ("front-door", "bus-gateway"):
            block = _service_block(rendered, service)
            assert "factory" in block, f"{service} is not on the factory network"
            assert "forge-publisher-net" not in block, (
                f"{service} is on the publisher's own network, and only the "
                "coordinator may be"
            )

    def test_neither_jarvis_service_publishes_a_port(
        self, rendered: str
    ) -> None:
        """Slack pushes nothing to this estate: the reply path is socket mode,
        an outbound WebSocket the front door dials. So there is no inbound
        route to open, and a published port would be a way in and nothing
        else."""
        for service in ("front-door", "bus-gateway"):
            block = _service_block(rendered, service)
            assert "ports:" not in block, (
                f"{service} publishes a port. Slack is dialled out to over a "
                "WebSocket and never heard from, so nothing needs to reach it."
            )

    def test_neither_jarvis_service_binds_anything_of_this_machine(
        self, rendered: str
    ) -> None:
        """They read a checkout's own settings file today. Nothing of a
        checkout, and no folder on a disk, may reach either container."""
        for service in ("front-door", "bus-gateway"):
            block = _service_block(rendered, service)
            assert "type: bind" not in block, (
                f"{service} binds a folder on this machine's disk"
            )

    def test_the_front_doors_saved_threads_are_in_a_volume_of_its_own(
        self, rendered: str
    ) -> None:
        """THE BLOCKER OF 26 SEPTEMBER 2026, held shut.

        A review drove the release image itself: a thread created through the
        front door's API was still there after stopping and starting the same
        container, and gone — 404 — from a replacement container built from the
        identical image. Rich's approvals and merge words live in those threads,
        and replacing a container is how this estate takes a new release.

        The mount point is ``/app/.langgraph_api`` and not something tidier
        because the development server writes its threads under that RELATIVE
        path in its working directory, with no setting for anywhere else, and the
        working directory has to stay ``/app`` — ``langgraph.json`` names its two
        graphs as ``./src`` paths.
        """
        front_door = _service_block(rendered, "front-door")
        assert "front-door-state" in front_door, (
            "the front door has no volume for its saved threads, so replacing "
            "its container deletes every approval and merge word in them"
        )
        assert "/app/.langgraph_api" in front_door, (
            "the front door's volume is not mounted where its server really "
            "writes: the development runtime writes under the relative path "
            ".langgraph_api in its working directory, which is /app"
        )
        assert "type: bind" not in front_door, (
            "the front door binds a folder on this machine's disk"
        )

    def test_the_gateway_does_not_share_the_front_doors_state(
        self, rendered: str
    ) -> None:
        """ONE WRITER. The gateway keeps no files, and two processes sharing one
        checkpoint directory is a different and worse arrangement than each
        keeping its own."""
        gateway = _service_block(rendered, "bus-gateway")
        assert "front-door-state" not in gateway, (
            "the bus gateway mounts the front door's state volume, so two "
            "processes write one checkpoint directory"
        )

    def test_the_front_door_refuses_to_start_on_state_it_cannot_write(
        self, rendered: str
    ) -> None:
        """A FRESH VOLUME TAKES ITS OWNERSHIP FROM THE IMAGE, and a jarvis image
        that does not create that directory gets a root-owned one. The server
        creates the directory if it can and writes its threads into it, so an
        unwritable one is not a start-up failure: the front door would come up,
        answer its health route, take an approval and lose it. The wrapper says
        which of those it is, by name."""
        front_door = _service_block(rendered, "front-door")
        assert "FRONT_DOOR_STATE_DIR" in front_door, (
            "nothing tells the front door's wrapper where its saved threads go, "
            "so nothing checks that it can write them"
        )
        assert "cannot write" in front_door, (
            "the front door starts without asking whether its state directory "
            "is writable, so an unwritable volume loses approvals in silence"
        )

    def test_the_slack_credentials_never_reach_a_container_environment(
        self, rendered: str
    ) -> None:
        """A bot token and an app-level token: both secrets, both files. The
        live units get them from a decrypt tool named by an absolute path in a
        unit file; here they arrive under /run/secrets and the wrapper puts
        them into the process's own environment and nowhere else."""
        for service in ("front-door", "bus-gateway"):
            block = _service_block(rendered, service)
            environment = block.split("environment:", 1)
            if len(environment) == 2:
                declared = environment[1].split("secrets:", 1)[0]
                for name in (
                    "JARVIS_SLACK_BOT_TOKEN:",
                    "JARVIS_SLACK_APP_TOKEN:",
                ):
                    assert name not in declared, (
                        f"{service} names {name.rstrip(':')} in its container "
                        "environment, where 'docker inspect' shows it to "
                        "anybody who can reach the Docker daemon"
                    )
            for target in ("slack_bot_token", "slack_app_token"):
                assert target in block, (
                    f"{service} is not given {target} as a file"
                )

    def test_the_jarvis_bus_address_carries_no_credential(
        self, rendered: str
    ) -> None:
        """The live gateway's start line is a whole nats:// address with the
        password in it. Here the address and the account are plain and the
        password arrives as a file, which the wrapper refuses to do twice."""
        for service in ("front-door", "bus-gateway"):
            block = _service_block(rendered, service)
            for line in block.splitlines():
                if "nats://" in line and "JARVIS_NATS_URL:" in line:
                    address = line.split("nats://", 1)[1]
                    assert "@" not in address.split()[0], (
                        f"{service} is given a bus address with a credential "
                        f"in it: {line.strip()}"
                    )
            assert "jarvis_bus_password" in block, (
                f"{service} is not given the bus password as a file"
            )

    def test_each_jarvis_service_writes_its_command_out(
        self, rendered: str
    ) -> None:
        """NAMING AN ENTRYPOINT EMPTIES THE IMAGE'S OWN COMMAND, and both name
        one because both are given their credentials as files. So both write
        their command out, and the image's release proof holds those two lines
        to what that one image can really run."""
        for service in ("front-door", "bus-gateway"):
            block = _service_block(rendered, service)
            assert "entrypoint:" in block, f"{service} names no entrypoint"
            assert "command:" in block, (
                f"{service} names an entrypoint and no command, so it would "
                "start with no command at all"
            )

    def test_the_memory_switch_has_no_default(self) -> None:
        """Memory being off must be a decision, never an omission: the compose
        file interpolates FLEET_MEMORY_ENABLED with ``:?``, so an env file that
        does not say either way refuses to render, by name."""
        text = (ESTATE / "compose.yaml").read_text()
        assert "${FLEET_MEMORY_ENABLED:?" in text, (
            "FLEET_MEMORY_ENABLED has a default or is not required, so an "
            "estate whose env file forgot it would start with the "
            "coordinator's memory off and say nothing"
        )

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
    shipped settings, once one that was never built. The estate has SIX image
    lines and a volume named after the release, so all seven are held here and
    the manifest cannot move without them."""

    def test_every_image_line_names_the_manifests_release(self) -> None:
        version = _the_manifests_version()
        env = (ESTATE / ".env.example").read_text()
        for line in (
            f"FORGE_IMAGE=forge:{version}",
            f"FORGE_PUBLISHER_IMAGE=forge-publisher:{version}",
            f"FLEET_MEMORY_MCP_IMAGE=fleet-memory-mcp:{version}",
            f"FLEET_MEMORY_RELAY_IMAGE=fleet-memory-relay:{version}",
            f"JARVIS_IMAGE=jarvis:{version}",
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
    "8c": (
        "the-memory-service-answers",
        "the memory service answers at the address the env file gives — the "
        "part of the design's item 8 that the memory service is",
    ),
    "8c-relay": (
        "the-memory-relay-is-running",
        "the memory relay is running and has written the progress marker it "
        "writes when it starts. It answers nobody over a network, so that "
        "file is the honest question to ask about it",
    ),
    "9": (
        "the-answer-service-from-inside-a-sandbox",
        "the coordinator's answer service answers from inside a sandbox at "
        "the address the sandbox profile allows",
    ),
}


def _the_checks_items() -> dict[str, tuple[str, str]]:
    """``estate-check --items``: number -> (name, the check's own sentence)."""
    done = subprocess.run(
        [str(ESTATE / "estate-check"), "--items"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    found: dict[str, tuple[str, str]] = {}
    for line in done.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 4:
            found[parts[1]] = (parts[2], parts[3])
    return found


class TestTheCheckStillHasEveryItemTheDesignAsksFor:
    @pytest.fixture(scope="class")
    def items(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for number, (name, _sentence) in _the_checks_items().items():
            found[number] = name
        return found

    def test_where_the_check_is_weaker_than_the_design_the_readme_says_so(
        self,
    ) -> None:
        """ITEM 4 IS DELIBERATELY WEAKER THAN THE DESIGN. Section 3's item 4
        and the rollout table both say the bundle supplies the tested image by
        DIGEST; this bundle names TAGS, which the work order relaxed on
        purpose. Holding the item's name alone let that difference sit quietly
        between a stored design sentence saying 'digests' and a check saying
        'tags' — nothing compared them. So: the check must SAY tags, and the
        README must carry it as a rollout precondition. Remove either and this
        fails."""
        design_says = THE_DESIGNS_ITEMS["4"][1]
        assert "digests" in design_says

        check_says = _the_checks_items()["4"][1]
        assert "TAGS" in check_says or "tags" in check_says, (
            "estate-check item 4 no longer says it checks tags. If it now "
            "checks digests, change THE_DESIGNS_ITEMS and the README instead "
            "of leaving the two disagreeing silently."
        )

        readme = (ESTATE / "README.md").read_text()
        preconditions = readme.split("The rollout preconditions this bundle does not meet", 1)
        assert len(preconditions) == 2, "the README has no rollout preconditions section"
        assert "digest" in preconditions[1].split("## ", 1)[0], (
            "estate-check item 4 checks tags where the design asks for "
            "digests, and the README's rollout preconditions no longer record "
            "that. An unmet design requirement that nothing writes down is one "
            "nobody meets."
        )

    def test_the_readme_records_what_the_services_check_does_not_prove(
        self,
    ) -> None:
        """Section 7 asks the services check to prove EVERY forbidden
        direction, including from the LAN, and to repeat after a restart. It
        proves one. The README's preconditions must say that about the CHECK
        and not only about the firewall rule."""
        readme = (ESTATE / "README.md").read_text()
        preconditions = readme.split(
            "The rollout preconditions this bundle does not meet", 1
        )[1].split("## ", 1)[0]
        for word in ("local network", "restart", "check"):
            assert word in preconditions, (
                "the README's rollout preconditions no longer record that "
                f"estate-check proves one forbidden direction only ({word})"
            )

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

    def test_the_check_asks_the_coordinator_about_its_own_memory(self) -> None:
        """ITEM 8c-forge, added 25 September 2026 after the review of stage 4b.

        Item 8c asks the memory SERVICE, whose caller is a Claude session over
        MCP from outside the estate. The coordinator does not use it: Forge
        reads the store itself. So 8c passing was never evidence that anything
        in the estate used memory, and while the bundle gave the coordinator no
        memory settings at all, every item here still passed. 8c-forge reads
        the coordinator's own memory line out of its own log, and the README
        has to say the two are different questions."""
        items = _the_checks_items()
        assert "8c-forge" in items, (
            "estate-check no longer asks the coordinator whether its own "
            "memory is on, so an estate that remembers nothing would pass "
            "every item again"
        )
        name, sentence = items["8c-forge"]
        assert name == "the-coordinator-reads-memory-itself", name
        assert "coordinator" in sentence

        check = (ESTATE / "estate-check").read_text()
        for word in ("memory: ON", "memory: OFF", "memory: DEGRADED"):
            assert word in check, (
                f"estate-check no longer looks for Forge's own '{word}' line, "
                "which is the only thing that says what the coordinator did"
            )

        readme = (ESTATE / "README.md").read_text()
        assert "8c-forge" in readme, (
            "the README does not mention item 8c-forge, so the difference "
            "between the memory service answering and the estate using memory "
            "is written down nowhere"
        )

    def test_the_check_asks_about_the_front_door_and_the_gateway(self) -> None:
        """ITEMS 8h AND 8i, added 25 September 2026, stage 4c.

        Neither of the two jarvis services publishes a port, so neither can be
        asked anything from outside — and "the container is running" would
        prove only that a process exists. So each is asked the honest question:
        the front door's own health route, inside its own container, and for
        the gateway, the BUS, because the gateway answers nobody and the bus
        knows who is connected to it."""
        items = _the_checks_items()
        for number, name in (
            ("8h", "the-front-door-answers"),
            ("8i", "the-bus-gateway-is-on-the-bus"),
        ):
            assert number in items, (
                f"estate-check no longer has item {number}, so the estate "
                "could have no Slack front door and pass every item"
            )
            assert items[number][0] == name, items[number]

        assert "asked of the BUS itself" in items["8i"][1], (
            "item 8i no longer says the question goes to the bus. Asking the "
            "gateway whether it is running proves a process exists, and this "
            "command refuses to start without the bus anyway."
        )

        readme = (ESTATE / "README.md").read_text()
        assert "8h" in readme, (
            "the README does not mention item 8h, so what it proves — and what "
            "it does not, which is anything about Slack itself — is written "
            "down nowhere"
        )

    def test_the_readme_says_the_front_door_runs_a_development_server(
        self,
    ) -> None:
        """THE HONEST LABEL, held in place.

        ``langgraph dev`` is the langgraph CLI's development server. It is what
        the live host unit has always run and what the image runs; containing
        it changed where the front door runs, not what runs. The production
        path that CLI offers needs a licence key for a closed-source server, no
        licence is baked, and what ought to serve these graphs is open. A page
        that stopped saying so would let a development server pass quietly for
        a production one."""
        readme = (ESTATE / "README.md").read_text()
        section = readme.split("The Slack front door, and the bus gateway", 1)
        assert len(section) == 2, "the README has no front door section"
        body = section[1].split("\n## ", 1)[0]
        for phrase in ("development", "licence", "socket mode"):
            assert phrase in body, (
                f"the README's front door section no longer says '{phrase}'"
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


class TestTheEstateForwardsWhatTheBundleForwards:
    """The estate composes deploy/compose in, so the names it hands a project's
    sandbox are that bundle's names. The two lists had drifted by 24 September
    2026 (the estate's copy did not forward FORGE_IMAGE or FORGE_IMAGE_IDENTITY,
    which the bootstrap requires); this holds them identical."""

    def _names(self, path: Path) -> list[str]:
        import re
        text = path.read_text()
        match = re.search(r"^SANDBOX_ENV_NAMES=(.*)$", text, re.M)
        assert match, f"{path} has no SANDBOX_ENV_NAMES line"
        return match.group(1).split()

    def test_the_two_lists_are_identical(self) -> None:
        estate = self._names(ESTATE / ".env.example")
        bundle = self._names(ESTATE.parent / "compose" / ".env.example")
        assert estate == bundle, (
            "deploy/estate/.env.example forwards a different list from "
            f"deploy/compose/.env.example:\n  estate only: {sorted(set(estate) - set(bundle))}"
            f"\n  bundle only: {sorted(set(bundle) - set(estate))}"
        )

    def test_every_forwarded_name_has_a_line_in_the_estates_example(self) -> None:
        text = (ESTATE / ".env.example").read_text()
        missing = [n for n in self._names(ESTATE / ".env.example") if f"\n{n}=" not in text]
        assert not missing, f"forwarded but given no line to fill in: {missing}"
