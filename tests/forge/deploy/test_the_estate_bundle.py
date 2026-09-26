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

import json
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


def _render(*extra: str, profiles: str | None = None) -> subprocess.CompletedProcess[str]:
    """``docker compose config`` of the estate, with the example env and with
    NO password of any kind in the environment.

    ``profiles`` is passed through the ENVIRONMENT rather than as ``--profile``,
    because Compose's ``--profile`` flag REPLACES ``COMPOSE_PROFILES`` from the
    env file rather than adding to it (checked on Compose v5.2.0, 26 September
    2026). Passing ``--profile sandbox`` on a bundle whose env file asks for
    ``local-bus`` takes the bus and the provisioner out of the project — which
    is exactly the trap the compose file and the README now warn about.
    """
    bare = {
        name: os.environ[name]
        for name in _ONLY_THESE_ARE_INHERITED
        if name in os.environ
    }
    if profiles is not None:
        bare["COMPOSE_PROFILES"] = profiles
    return subprocess.run(
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


def _rendered(*extra: str, profiles: str | None = None) -> str:
    done = _render(*extra, profiles=profiles)
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
    # A machine that looks after a sandbox ADDS it to the env file's profiles
    # line. It does not pass '--profile sandbox', which would replace them.
    return _rendered(profiles="local-bus,sandbox")


#: The external-bus overlay, and the two settings that put the bundle in that
#: mode. ``COMPOSE_PROFILES=""`` because external mode asks for no profile at
#: all — the bus and the mutating provisioner must not be in the project.
_EXTERNAL_BUS = ("--file", "compose.external-bus.yaml")


@pytest.fixture(scope="module")
def rendered_against_an_external_bus() -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    return _rendered(*_EXTERNAL_BUS, profiles="")


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


class TestTheTwoBusModes:
    """WHOSE BUS IS IT (26 September 2026, build item E1).

    The estate has to be startable against a bus that is ALREADY RUNNING and
    belongs to somebody else — the first rollout keeps the live bus, because it
    holds every reader's position and every message still waiting, and a cloud
    machine joining a bus it did not start is the same shape.

    Two mechanisms were tried first and neither would have delivered it, so
    these tests hold the one that does:

    * marking the bus and the provisioner as dependencies that are *not
      required* says what to do when a dependency is UNAVAILABLE. It does not
      take a service out of what ``up`` starts;
    * the provisioning scripts' preview mode, for a stream that already exists,
      prints "Would check/update" and returns before comparing a single field.

    What this bundle does instead: a PROFILE, which is real exclusion — a
    service behind a profile that is not asked for is not in the project at all.
    """

    def test_local_mode_has_the_bus_and_the_provisioner(
        self, rendered: str
    ) -> None:
        """The example env asks for the ``local-bus`` profile, which is the
        clean-machine and cloud path and what every earlier proof used."""
        assert "\n  nats:\n" in rendered, "local mode has no bus"
        assert "\n  nats-provision:\n" in rendered, "local mode has no provisioner"

    def test_external_mode_has_neither(
        self, rendered_against_an_external_bus: str
    ) -> None:
        """NOT STARTED, NOT RESTARTED, NOT BROUGHT BACK BY A SECOND ``up``.

        The provisioner matters more than the bus here: it is the one thing in
        the estate that WRITES to a bus's storage, and in external mode the bus
        is somebody else's.
        """
        for service in ("nats", "nats-provision"):
            assert f"\n  {service}:\n" not in rendered_against_an_external_bus, (
                f"'{service}' is still in the project in external bus mode, so "
                "this estate would start its own bus beside the one being kept"
            )

    def test_one_bus_dependency_and_it_is_in_both_modes(
        self, rendered: str, rendered_against_an_external_bus: str
    ) -> None:
        """``bus-ready`` is every other service's only bus dependency, in both
        modes. Compose REFUSES a project whose service depends on a service
        whose profile is off ("service X depends on undefined service Y"), so
        the old dependency on the provisioner could not stay and could not be
        weakened either — it had to be replaced."""
        for document in (rendered, rendered_against_an_external_bus):
            assert "\n  bus-ready:\n" in document, (
                "bus-ready is not in the project, and it is the one bus "
                "dependency that has to exist in both modes"
            )
            for service in ("coordinator", "memory-relay", "front-door", "bus-gateway"):
                block = _service_block(document, service)
                assert "bus-ready" in block, (
                    f"{service} does not wait for bus-ready"
                )
                assert "nats-provision" not in block, (
                    f"{service} still waits for the provisioner by name, which "
                    "makes Compose refuse the whole project in external bus mode"
                )

    def test_bus_ready_keeps_its_own_dependency_only_in_local_mode(
        self, rendered: str, rendered_against_an_external_bus: str
    ) -> None:
        local = _service_block(rendered, "bus-ready")
        assert "nats-provision" in local, (
            "in local mode bus-ready must wait for the provisioner, or the "
            "coordinator can start against a bus with no storage"
        )
        external = _service_block(rendered_against_an_external_bus, "bus-ready")
        assert "depends_on" not in external, (
            "the external-bus overlay does not clear bus-ready's dependency on "
            "the profiled-off provisioner, so Compose will refuse the project"
        )

    def test_the_retained_bus_network_is_one_that_already_exists(
        self, rendered_against_an_external_bus: str
    ) -> None:
        """``external: true`` is what says "this network is not mine": Compose
        joins it, does not create it, and ``down`` leaves it where it was."""
        networks = rendered_against_an_external_bus.split("\nnetworks:", 1)[1]
        assert "retained-bus:" in networks
        after = networks.split("retained-bus:", 1)[1]
        assert "external: true" in after[:200], (
            "the retained bus's network is not declared as one that already "
            "exists, so this bundle would create a network of its own and the "
            "bus would not be on it"
        )

    def test_external_mode_carries_nothing_of_this_machine_either(
        self, rendered_against_an_external_bus: str
    ) -> None:
        found = {
            address
            for address in _AN_ADDRESS.findall(rendered_against_an_external_bus)
            if address not in _ALLOWED_ADDRESSES
            and not address.startswith(_DOCUMENTATION_BLOCK)
        }
        assert not found, f"external bus mode brought an address: {sorted(found)}"
        for line in rendered_against_an_external_bus.splitlines():
            if "/home/" not in line:
                continue
            assert any(home in line for home in _IN_IMAGE_HOMES), (
                f"external bus mode carries a home directory: {line.strip()}"
            )

    def test_the_example_env_carries_both_modes_and_says_which_is_on(
        self,
    ) -> None:
        env = (ESTATE / ".env.example").read_text()
        for line in ("BUS_MODE=local", "COMPOSE_PROFILES=local-bus", "COMPOSE_FILE=compose.yaml"):
            assert f"\n{line}\n" in env, f"the example env has no '{line}' line"
        for name in ("BUS_MONITORING_ADDRESS=", "BUS_EXTERNAL_NETWORK=", "BUS_READY_TIMEOUT_S="):
            assert f"\n{name}" in env, f"the example env names no {name.rstrip('=')}"

    def test_the_profile_flag_trap_is_written_down(self) -> None:
        """A REAL TRAP, MET WHILE BUILDING THIS. ``--profile sandbox`` on the
        command line REPLACES the env file's profiles rather than adding to
        them, so it takes the bus and the provisioner out of the project. It
        fails loudly — Compose refuses the project, because bus-ready then names
        a service that is not there — but the compose file and the README have
        to say which line to change instead."""
        for page in (ESTATE / "compose.yaml", ESTATE / "README.md"):
            text = page.read_text()
            assert "local-bus,sandbox" in text, (
                f"{page.name} does not say that a machine with a sandbox adds "
                "it to COMPOSE_PROFILES rather than passing --profile"
            )

    def test_asking_for_the_sandbox_the_wrong_way_refuses_loudly(self) -> None:
        """Not a warning and not a quiet loss of the bus: the project does not
        render at all. Proved here so that a future Compose which started
        MERGING profiles instead of replacing them would show up as a failing
        test rather than as a second bus one day."""
        if shutil.which("docker") is None:
            pytest.skip("docker is not installed here, so there is nothing to render")
        done = _render("--profile", "sandbox")
        assert done.returncode != 0, (
            "'--profile sandbox' rendered a project. If Compose now ADDS to "
            "COMPOSE_PROFILES rather than replacing it, this trap is gone and "
            "the warnings in compose.yaml and README.md should be removed."
        )
        assert "bus-ready" in done.stderr, done.stderr


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
        than a sentence somebody has to remember.

        **[26 September 2026] It now waits for ``bus-ready`` rather than for the
        provisioner by name**, and ``bus-ready`` waits for the provisioner in
        local mode — so this promise is unchanged, and in external bus mode it
        means the retained bus answers and already holds what the pinned
        definitions name. The indirection is not decoration: Compose refuses a
        whole project whose service depends on a service whose profile is off.
        """
        block = _service_block(rendered, "coordinator")
        assert "bus-ready" in block, (
            "the coordinator does not wait for the bus to be ready"
        )
        assert "service_completed_successfully" in block, (
            "the coordinator waits for the one-shot, but not for it to SUCCEED"
        )
        ready = _service_block(rendered, "bus-ready")
        assert "nats-provision" in ready and "service_completed_successfully" in ready, (
            "bus-ready does not wait for the provisioner to SUCCEED in local "
            "mode, so the coordinator could start against a bus with no storage"
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
        """Three readings now, not two, because some things must be true before
        anything starts, others can only be true after, and one phase of a
        rollout has to be checked with the door deliberately SHUT. The first
        draft of the design asked the bus to answer before the compose file had
        started it."""
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
        for number in ("1", "2", "3", "4", "5", "6", "7", "7b"):
            assert modes[number] == "host", (
                f"item {number} must be checked BEFORE anything starts"
            )
        for number in ("8", "9"):
            assert modes[number] == "services", (
                f"item {number} can only be checked AFTER things have started"
            )
        assert modes["10"] == "pre-resume", (
            "item 10 asks whether the producers are STOPPED, which is only ever "
            "true in the closed-door phase; in the ordinary services mode a "
            "stopped front door is the failure"
        )

    def test_the_check_knows_whose_bus_it_is(self) -> None:
        """ITEM 7b (26 September 2026, build item E1).

        BUS_MODE is a word in the env file and does nothing on its own: what
        decides whether this estate starts a bus of its own are two settings
        Compose reads for itself. A half-set mode renders perfectly well and
        starts a SECOND BUS beside the one being kept, which is the one mistake
        external mode exists to stop."""
        items = _the_checks_items()
        assert "7b" in items, "estate-check no longer checks the bus mode"
        assert items["7b"][0] == "bus-mode-and-the-files-agree", items["7b"]
        check = (ESTATE / "estate-check").read_text()
        for word in ("COMPOSE_PROFILES", "COMPOSE_FILE", "BUS_EXTERNAL_NETWORK"):
            assert word in check, (
                f"estate-check no longer reads {word}, so BUS_MODE could say "
                "one thing while the project does another"
            )
        readme = (ESTATE / "README.md").read_text()
        assert "external bus mode" in readme.lower(), (
            "the README does not describe external bus mode, so when it is used "
            "and what it deliberately does not do are written down nowhere"
        )

    def test_the_closed_door_check_exists_and_says_what_it_leaves_out(
        self,
    ) -> None:
        """ITEM 10 AND THE CLOSED-DOOR MODE (26 September 2026, build item E2).

        The two producer items are never reported as passed in this mode and
        never as failed: they are reported as not checked, with where they ARE
        checked. An unknown that is read as a pass is how a rollout resumes onto
        an estate nobody has looked at."""
        items = _the_checks_items()
        assert "10" in items, "estate-check has no closed-door item"
        assert items["10"][0] == "the-producers-are-stopped", items["10"]
        check = (ESTATE / "estate-check").read_text()
        for phrase in (
            "--pre-resume",
            "--read-pre-resume",
            "pre-resume.json",
            "not checked in this mode",
            "INVALIDATED",
        ):
            assert phrase in check, (
                f"estate-check no longer carries '{phrase}', which is part of "
                "the closed-door check the resume step reads"
            )
        readme = (ESTATE / "README.md").read_text()
        assert "--pre-resume" in readme, (
            "the README does not mention the closed-door check, so what it "
            "proves and what it deliberately leaves out is written down nowhere"
        )

    def test_the_closed_door_receipt_is_bound_to_the_release_it_is_for(
        self,
    ) -> None:
        """ITEM 10b (26 September 2026, the first of the three conditions
        Codex's third read put on build item E2 — and the one the first
        implementation did not meet).

        The first version wrote the release the env file NAMED and the image id
        the coordinator was REALLY RUNNING into one receipt and never compared
        them, so an env file naming one release while the estate ran another
        produced a receipt saying the wrong release without a word — and reading
        it back accepted it, because the reader compared the recorded id with
        the running id, which is the same number twice. The receipt sits
        directly in front of the point of no return, so the two are compared
        when it is written, and the release it names is resolved again when it
        is read back."""
        items = _the_checks_items()
        assert "10b" in items, (
            "estate-check no longer asks whether what is running is the release "
            "the rollout is for, so a closed-door receipt could again name a "
            "release the estate is not running"
        )
        assert items["10b"][0] == (
            "the-running-release-is-the-one-this-rollout-names"
        ), items["10b"]

        check = (ESTATE / "estate-check").read_text()
        for phrase in (
            "--for-image",
            "image_id_of",
            "coordinator_image_named_id",
            "the tag has been moved since the record was written",
        ):
            assert phrase in check, (
                f"estate-check no longer carries '{phrase}', which is part of "
                "binding the closed-door receipt to the release the rollout is "
                "for"
            )

        readme = (ESTATE / "README.md").read_text()
        assert "10b" in readme, (
            "the README does not mention item 10b, so what binds the receipt to "
            "one release is written down nowhere"
        )

    def test_the_release_item_belongs_to_the_closed_door_phase(self) -> None:
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
        assert modes["10b"] == "pre-resume", (
            "item 10b belongs to the closed-door phase: the ordinary services "
            "check is not a rollout and is never told which release a rollout "
            "is for"
        )


#: The read-only comparison of a running bus against the pinned definitions,
#: and the saved answers it is driven with here. Nothing in this class asks a
#: bus anything: each fixture is one answer the monitoring route could give.
_COMPARE = ESTATE / "provisioner" / "compare-bus-with-definitions.sh"
_BUS_FIXTURES = Path(__file__).resolve().parent / "bus-comparison-fixtures"


def _compare(fixture: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(_COMPARE),
            "--jsz-file",
            str(_BUS_FIXTURES / fixture),
            "--definitions",
            str(_BUS_FIXTURES),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestTheBusIsComparedFieldByField:
    """THE READ-ONLY COMPARISON (26 September 2026, build item E1).

    In external bus mode nothing may write to the bus, so something has to say
    whether the bus that is already running is the bus this release expects.
    This is that something, and these are its four answers.

    WHY NOT THE PROVISIONING SCRIPTS' PREVIEW MODE, which was the first idea:
    for a stream or bucket that already exists it prints "Would check/update"
    and returns before comparing a single field
    (``streams/provision-streams.sh:146-152``, ``kv/provision-kv.sh:134-141``).
    A clean preview is not evidence of a matching bus.

    THE TWO SIDES ARE SPELT DIFFERENTLY, which is the whole reason this is a
    script and not a diff: the definitions say ``"work"`` and ``"7d"`` and the
    bus answers ``"workqueue"`` and ``604800000000000``; a bucket is a stream
    called ``KV_<bucket>`` whose ``max_msgs_per_subject`` is the bucket's
    history. Every conversion in the fixtures is one the real bus made.
    """

    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:
            pytest.skip("jq is not on this machine, and the bus answers JSON")

    def test_a_matching_bus_agrees(self) -> None:
        done = _compare("jsz-a-matching-bus.json")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Nothing was written to the bus" in done.stdout

    def test_one_field_changed_refuses_and_names_the_field(self) -> None:
        done = _compare("jsz-one-field-changed.json")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "A-STREAM" in done.stdout and "max_age" in done.stdout, done.stdout
        assert "604800000000000" in done.stdout and "3600000000000" in done.stdout, (
            "the refusal does not print both what was wanted and what was found"
        )
        assert "NOTHING WAS UPDATED" in done.stderr

    def test_a_missing_stream_refuses_and_names_it(self) -> None:
        done = _compare("jsz-a-stream-missing.json")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "MISSING" in done.stdout and "ANOTHER-STREAM" in done.stdout

    def test_an_answer_without_the_configuration_is_unknown_not_agreement(
        self,
    ) -> None:
        """THE DISTINCTION THAT MATTERS. An answer this cannot read is not a
        matching bus and is not a mismatched one: it is a bus nothing is known
        about, and it leaves by a different door (3, not 0 and not 2)."""
        done = _compare("jsz-without-the-configuration.json")
        assert done.returncode == 3, done.stdout + done.stderr
        assert "could not be read" in done.stderr

    def test_an_answer_that_is_not_json_is_unknown_too(self) -> None:
        done = _compare("jsz-not-json-at-all.txt")
        assert done.returncode == 3, done.stdout + done.stderr
        assert "could not be read" in done.stderr

    def test_a_bus_that_does_not_answer_is_unknown(self) -> None:
        """An address nothing listens on. Loopback and a port nothing serves,
        so this asks nothing of any real bus."""
        done = subprocess.run(
            [
                "bash", str(_COMPARE),
                "--monitoring-address", "127.0.0.1:1",
                "--definitions", str(_BUS_FIXTURES),
                "--timeout", "2",
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 3, done.stdout + done.stderr
        assert "NOT agreement" in done.stderr

    def _own_fixtures(
        self, tmp_path: Path, definition: dict, config: dict
    ) -> subprocess.CompletedProcess[str]:
        """One stream, written the two ways, compared. Used for the shapes the
        committed fixtures do not have — a stream with two subjects, and a count
        written with a unit on it."""
        import json

        (tmp_path / "streams").mkdir()
        (tmp_path / "kv").mkdir()
        (tmp_path / "streams" / "stream-definitions.json").write_text(
            json.dumps({"streams": [definition]})
        )
        (tmp_path / "kv" / "kv-definitions.json").write_text(
            json.dumps({"kv_buckets": []})
        )
        answer = {
            "server_id": "a-made-up-bus",
            "account_details": [
                {
                    "name": "AN-ACCOUNT",
                    "stream_detail": [{"name": config["name"], "config": config}],
                }
            ],
        }
        (tmp_path / "jsz.json").write_text(json.dumps(answer))
        return subprocess.run(
            [
                "bash", str(_COMPARE),
                "--jsz-file", str(tmp_path / "jsz.json"),
                "--definitions", str(tmp_path),
            ],
            capture_output=True, text=True, timeout=60,
        )

    def test_the_same_two_subjects_in_the_other_order_is_the_same_stream(
        self, tmp_path: Path
    ) -> None:
        """26 September 2026, the review of this script. The two sides are two
        JSON arrays and were compared with ``==``, so a bus that answered the
        same two subjects in the other order was called a MISMATCH. It erred
        towards refusing rather than towards agreeing, and neither throwaway bus
        ever did it — but a subject list is a set, not an order."""
        done = self._own_fixtures(
            tmp_path,
            {
                "name": "TWO-SUBJECTS",
                "subjects": ["a.>", "b.>"],
                "retention": "limits",
                "storage": "file",
                "replicas": 1,
            },
            {
                "name": "TWO-SUBJECTS",
                "subjects": ["b.>", "a.>"],
                "retention": "limits",
                "storage": "file",
                "num_replicas": 1,
            },
        )
        assert done.returncode == 0, done.stdout + done.stderr

    def test_a_subject_that_is_genuinely_different_still_refuses(
        self, tmp_path: Path
    ) -> None:
        done = self._own_fixtures(
            tmp_path,
            {
                "name": "TWO-SUBJECTS",
                "subjects": ["a.>", "b.>"],
                "retention": "limits",
                "storage": "file",
                "replicas": 1,
            },
            {
                "name": "TWO-SUBJECTS",
                "subjects": ["a.>", "c.>"],
                "retention": "limits",
                "storage": "file",
                "num_replicas": 1,
            },
        )
        assert done.returncode == 2, done.stdout + done.stderr
        assert "subjects" in done.stdout, done.stdout

    def test_a_count_with_a_unit_on_it_is_unreadable_and_never_guessed(
        self, tmp_path: Path
    ) -> None:
        """26 September 2026, the review of this script. ``max_msgs`` is a number
        of MESSAGES and went through the SIZE converter, so a definition written
        ``"10K"`` messages would have been read as 10240 and compared against a
        bus reporting 10000 messages. A count is a plain number here and a unit
        on one is an unknown, which leaves by the unknown door (3) rather than
        being multiplied by 1024."""
        done = self._own_fixtures(
            tmp_path,
            {
                "name": "COUNTED",
                "subjects": ["c.>"],
                "retention": "limits",
                "max_msgs": "10K",
                "storage": "file",
                "replicas": 1,
            },
            {
                "name": "COUNTED",
                "subjects": ["c.>"],
                "retention": "limits",
                "max_msgs": 10240,
                "storage": "file",
                "num_replicas": 1,
            },
        )
        assert done.returncode == 3, done.stdout + done.stderr
        assert "UNREADABLE-COUNT" in done.stderr, done.stderr
        assert "units this comparison does not read" in done.stderr

    def test_a_count_written_as_a_plain_number_still_agrees(
        self, tmp_path: Path
    ) -> None:
        done = self._own_fixtures(
            tmp_path,
            {
                "name": "COUNTED",
                "subjects": ["c.>"],
                "retention": "limits",
                "max_msgs": 10000,
                "storage": "file",
                "replicas": 1,
            },
            {
                "name": "COUNTED",
                "subjects": ["c.>"],
                "retention": "limits",
                "max_msgs": 10000,
                "storage": "file",
                "num_replicas": 1,
            },
        )
        assert done.returncode == 0, done.stdout + done.stderr

    def test_the_comparison_travels_in_the_provisioning_image(self) -> None:
        """It is a FILE in the image rather than a command written into the
        compose file, so a test can run it on its own — as these do."""
        dockerfile = (ESTATE / "provisioner" / "Dockerfile").read_text()
        assert "compare-bus-with-definitions.sh" in dockerfile
        compose = (ESTATE / "compose.yaml").read_text()
        assert "/usr/local/bin/compare-bus-with-definitions.sh" in compose


class TestTheComparisonChoosesOneAccount:
    """THE THIRD E1/E2 REVIEW (26 September 2026): a bus has several accounts and
    a stream name is unique only within one. Joining every account's streams
    into one map let the last account to answer win, and the closed-door
    check's own waiting-count read had the same fault. The comparison now
    chooses ONE account: the one named in BUS_STREAMS_ACCOUNT, else the single
    account holding every pinned name; two is ambiguous, none is no account —
    both UNKNOWN (exit 3), never a comparison and never twelve false MISSING
    lines."""

    def _run(self, tmp_path: Path, accounts: list[tuple[str, int]], env_account: str | None):
        import json, os

        (tmp_path / "streams").mkdir(exist_ok=True)
        (tmp_path / "kv").mkdir(exist_ok=True)
        (tmp_path / "streams" / "stream-definitions.json").write_text(
            json.dumps({"streams": [{"name": "PIPELINE", "subjects": ["pipeline.>"], "max_msgs": 10000}]})
        )
        (tmp_path / "kv" / "kv-definitions.json").write_text(json.dumps({"kv_buckets": []}))
        answer = {"server_id": "two-account-bus", "account_details": [
            {"name": name, "stream_detail": [{"name": "PIPELINE", "config": {"name": "PIPELINE", "subjects": ["pipeline.>"], "max_msgs": max_msgs}}]}
            for name, max_msgs in accounts
        ]}
        (tmp_path / "jsz.json").write_text(json.dumps(answer))
        env = dict(os.environ)
        env.pop("BUS_STREAMS_ACCOUNT", None)
        if env_account is not None:
            env["BUS_STREAMS_ACCOUNT"] = env_account
        return subprocess.run(
            ["bash", str(_COMPARE), "--jsz-file", str(tmp_path / "jsz.json"), "--definitions", str(tmp_path)],
            capture_output=True, text=True, timeout=60, env=env,
        )

    def test_two_accounts_holding_the_name_is_unknown_not_a_pass(self, tmp_path: Path) -> None:
        done = self._run(tmp_path, [("RVA", 10000), ("RVB", 20000)], None)
        assert done.returncode == 3, done.stdout + done.stderr
        assert "more than one account" in done.stderr and "RVA" in done.stderr and "RVB" in done.stderr
        assert "MISSING" not in done.stdout + done.stderr, "false MISSING lines were printed"

    def test_the_named_account_decides(self, tmp_path: Path) -> None:
        good = self._run(tmp_path, [("RVA", 10000), ("RVB", 20000)], "RVA")
        assert good.returncode == 0, good.stdout + good.stderr
        bad = self._run(tmp_path, [("RVA", 10000), ("RVB", 20000)], "RVB")
        assert bad.returncode == 2, bad.stdout + bad.stderr
        assert "max_msgs" in bad.stdout + bad.stderr

    def test_a_misspelt_account_is_unknown_not_twelve_missing_lines(self, tmp_path: Path) -> None:
        done = self._run(tmp_path, [("RVA", 10000)], "NOT-AN-ACCOUNT")
        assert done.returncode == 3, done.stdout + done.stderr
        assert "no account on this bus holds" in done.stderr
        assert "MISSING" not in done.stdout + done.stderr

    def test_one_account_is_the_ordinary_case(self, tmp_path: Path) -> None:
        done = self._run(tmp_path, [("RVA", 10000)], None)
        assert done.returncode == 0, done.stdout + done.stderr

    def test_the_bus_ready_container_is_handed_the_account(self) -> None:
        compose = (ESTATE / "compose.yaml").read_text()
        assert "BUS_STREAMS_ACCOUNT: ${BUS_STREAMS_ACCOUNT:-}" in compose, (
            "the setting exists in the env file but never reaches the container that reads it"
        )


class TestTheClosedDoorAsksBothServiceManagers:
    """THE THIRD E1/E2 REVIEW: the item that proves the legacy front-door units
    stopped asked the user manager alone and read an empty answer as absent —
    so with no session bus it passed while both live units were running. Now
    both managers are asked and only a READ answer counts. These tests read the
    script's own words, because driving the item needs a whole estate; the
    reviewers drive it for real."""

    def test_both_managers_are_asked(self) -> None:
        text = (ESTATE / "estate-check").read_text()
        assert 'for mgr in "--user" "--system"' in text
        assert "unreachable-or-silent" in text
        assert "could not be read is not stopped" in text

    def test_a_record_from_the_future_is_refused(self) -> None:
        text = (ESTATE / "estate-check").read_text()
        assert 'if [ "${age}" -lt 0 ]' in text
        assert "in the future of this machine" in text

    def test_the_same_bus_question_is_asked_at_the_closed_door_too(self) -> None:
        text = (ESTATE / "estate-check").read_text()
        assert text.count("the_same_bus_or_trouble") >= 3, "defined once, asked at the door and at the closed door"

    def test_the_waiting_counts_are_read_in_one_chosen_account(self) -> None:
        text = (ESTATE / "estate-check").read_text()
        assert "one must be chosen, in BUS_STREAMS_ACCOUNT" in text
        assert 'select(.name == \\$acct)' in text or "select(.name == \$acct)" in text


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


# ---------------------------------------------------------------------------
# THE GATEWAY WATCH (26 September 2026, build item E3)
#
# The named replacement for the retired host alarm. Three tests' worth of
# subject: that the compose file renders it and gives both jarvis services a
# distinct name on the bus, that the example env file carries every new name,
# and that the rule it judges a connection by really needs all three parts.
# ---------------------------------------------------------------------------

_WATCH = ESTATE / "provisioner" / "gateway-watch.sh"
_WATCH_FIXTURES = Path(__file__).resolve().parent / "gateway-watch-fixtures"

#: A fixed moment, so an age is arithmetic and not a race. Every fixture's times
#: are written relative to it: 2026-09-26T16:00:00Z.
_A_FIXED_NOW = "1790438400"


def _watch(
    *,
    connz: str,
    heartbeat: str,
    log: str,
    notifier_file: Path,
    account: str = "jarvis",
    client_name: str = "bus-gateway-factory",
    subject: str = "agents.command.jarvis",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """One look, with every answer substituted from a saved file.

    The three test seams (``--connz-file``, ``--log-file``, ``--now``) are the
    same idea as the retired watchdog's own ``--journal-file`` and
    ``--now-epoch``: the reads are the edges, and the judgement is what is worth
    pinning. The notifier is the stand-in that writes a file — no test of this,
    anywhere, ever touches the real Slack workspace.
    """
    env = {
        **{name: os.environ[name] for name in _ONLY_THESE_ARE_INHERITED if name in os.environ},
        "JARVIS_NATS_USER": account,
        "GATEWAY_WATCH_CLIENT_NAME": client_name,
        "GATEWAY_WATCH_SUBJECT": subject,
        "BUS_MONITORING_ADDRESS": "nats:8222",
        "GATEWAY_WATCH_HEARTBEAT_PATH": str(_WATCH_FIXTURES / heartbeat)
        if heartbeat
        else str(_WATCH_FIXTURES / "no-such-heartbeat.json"),
        "GATEWAY_WATCH_NOTIFIER": "file",
        "GATEWAY_WATCH_NOTIFIER_FILE": str(notifier_file),
        "TZ": "UTC",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            "bash",
            str(_WATCH),
            "--once",
            "--connz-file",
            str(_WATCH_FIXTURES / connz),
            "--log-file",
            str(_WATCH_FIXTURES / log),
            "--now",
            _A_FIXED_NOW,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _messages(notifier_file: Path) -> list[str]:
    if not notifier_file.exists():
        return []
    return [
        json.loads(line)["text"]
        for line in notifier_file.read_text().splitlines()
        if line.strip()
    ]


class TestTheComposeFileGivesEachServiceItsOwnName:
    """E3-j1. The account is shared on purpose; the name is what is not."""

    def test_the_two_jarvis_services_send_two_different_names(
        self, rendered: str
    ) -> None:
        door = _service_block(rendered, "front-door")
        gateway = _service_block(rendered, "bus-gateway")
        assert "JARVIS_NATS_CLIENT_NAME: front-door-factory" in door, door
        assert "JARVIS_NATS_CLIENT_NAME: bus-gateway-factory" in gateway, gateway

    def test_they_still_share_one_account_which_is_why_the_name_is_needed(
        self, rendered: str
    ) -> None:
        door = _service_block(rendered, "front-door")
        gateway = _service_block(rendered, "bus-gateway")
        assert "JARVIS_NATS_USER: jarvis" in door
        assert "JARVIS_NATS_USER: jarvis" in gateway

    def test_the_gateway_is_told_where_to_write_its_heartbeat(
        self, rendered: str
    ) -> None:
        gateway = _service_block(rendered, "bus-gateway")
        assert "JARVIS_SLACK_HEARTBEAT_PATH: /var/lib/jarvis/slack-heartbeat.json" in gateway
        assert "gateway-state" in gateway, (
            "the gateway is told to write a heartbeat and given no volume to "
            "write it in, so it would go into the container's own layer"
        )

    def test_the_front_door_is_not_given_the_gateways_volume(
        self, rendered: str
    ) -> None:
        """One writer per volume, the same rule the front door's threads have."""
        door = _service_block(rendered, "front-door")
        assert "gateway-state" not in door, door


class TestTheWatchIsInTheProjectOnlyWhenAsked:
    def test_up_does_not_start_it(self, rendered: str) -> None:
        """Like the sandbox service: a profile that is not asked for takes the
        service out of the project ALTOGETHER, which is what keeps a rollout
        window quiet."""
        assert "\n  gateway-watch:" not in rendered, (
            "the gateway watch is in the project without its profile, so "
            "'docker compose up' would start the thing that tells Rich the "
            "Slack door is down in the middle of a rollout"
        )

    @pytest.fixture(scope="class")
    def rendered_with_the_watch(self) -> str:
        if shutil.which("docker") is None:
            pytest.skip("docker is not installed here, so there is nothing to render")
        return _rendered(profiles="local-bus,watch")

    def test_the_profile_renders_it(self, rendered_with_the_watch: str) -> None:
        block = _service_block(rendered_with_the_watch, "gateway-watch")
        assert "gateway-watch.sh" in block, block

    def test_it_is_told_the_same_name_the_gateway_sends(
        self, rendered_with_the_watch: str
    ) -> None:
        """Both come from one setting in one file, so the watch cannot end up
        looking for a name nothing sends."""
        watch = _service_block(rendered_with_the_watch, "gateway-watch")
        gateway = _service_block(rendered_with_the_watch, "bus-gateway")
        assert "GATEWAY_WATCH_CLIENT_NAME: bus-gateway-factory" in watch, watch
        assert "JARVIS_NATS_CLIENT_NAME: bus-gateway-factory" in gateway

    def test_it_reads_the_heartbeat_and_cannot_write_it(
        self, rendered_with_the_watch: str
    ) -> None:
        block = _service_block(rendered_with_the_watch, "gateway-watch")
        assert "gateway-state" in block, block
        assert "read_only: true" in block, (
            "the watch mounts the gateway's volume writable, and a reader that "
            "can write the thing it reports on is not a reader"
        )

    def test_it_asks_the_engine_for_a_log_and_says_so(
        self, rendered_with_the_watch: str
    ) -> None:
        """The one real privilege in this service, named rather than hidden."""
        block = _service_block(rendered_with_the_watch, "gateway-watch")
        assert "/var/run/docker.sock" in block, block
        compose = (ESTATE / "compose.yaml").read_text()
        assert "docker.sock" in compose and "privilege" in compose, (
            "the compose file mounts the Docker socket into the watch without "
            "saying in words what that costs"
        )

    def test_it_waits_for_the_bus_like_everything_else(
        self, rendered_with_the_watch: str
    ) -> None:
        block = _service_block(rendered_with_the_watch, "gateway-watch")
        assert "bus-ready" in block, (
            "the watch does not wait for the bus, so its first look could call "
            "the door lost because the bus was not answering yet"
        )

    def test_the_volume_is_declared(self, rendered_with_the_watch: str) -> None:
        assert "gateway-state" in rendered_with_the_watch.split("volumes:")[-1]


class TestTheExampleCarriesEveryNewName:
    def test_the_new_settings_all_have_a_line(self) -> None:
        text = (ESTATE / ".env.example").read_text()
        for name in (
            "FACTORY_INSTANCE",
            "GATEWAY_WATCH_INTERVAL_S",
            "GATEWAY_WATCH_HEARTBEAT_MAX_AGE_S",
            "GATEWAY_WATCH_MAX_SILENCE_S",
            "GATEWAY_WATCH_SUBJECT",
            "GATEWAY_WATCH_NOTIFIER",
            "GATEWAY_WATCH_NOTIFIER_FILE",
            "GATEWAY_WATCH_CONTAINER",
            "JARVIS_WATCHDOG_ALERT_CHANNEL_ID",
        ):
            assert f"\n{name}=" in text, (
                f"{name} is read by the estate and has no line in .env.example, "
                "so a machine copying the example gets the compose default and "
                "no idea the setting exists"
            )

    def test_the_example_says_how_the_fifteen_minutes_happen(self) -> None:
        """A compose file has no timer. What provides the cadence had to be
        decided, and a decision nobody can find was not made."""
        text = (ESTATE / ".env.example").read_text()
        assert "GATEWAY_WATCH_INTERVAL_S=900" in text
        readme = (ESTATE / "README.md").read_text()
        assert "loop in the container" in readme, (
            "the README does not say what provides the fifteen minutes, so the "
            "next person will look for a timer that does not exist"
        )
        assert "--profile watch run --rm gateway-watch --once" in readme, (
            "the README does not say how to look once by hand"
        )

    def test_the_watch_is_named_in_the_profiles_note(self) -> None:
        """The line a machine actually edits has to say the profile exists."""
        text = (ESTATE / ".env.example").read_text()
        before_the_line = text.split("\nCOMPOSE_PROFILES=local-bus")[0]
        assert "watch" in before_the_line[-2000:], (
            "the note above the COMPOSE_PROFILES line does not mention the "
            "watch's profile, so the only way to find it is to read compose.yaml"
        )


class TestTheWatchJudgesOneConnectionByAllThreeThings:
    """THE POINT OF E3, and the finding that drove it.

    The bundle gives the front door and the gateway the same bus account, so a
    check that matches only ``authorized_user`` reports a STOPPED GATEWAY AS
    HEALTHY whenever the front door is up. These cases are the difference.

    Nothing here touches Slack: the notifier is the stand-in that writes a file.
    """

    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:
            pytest.skip("jq is not on this machine, and the bus answers JSON")

    def test_a_healthy_gateway_is_every_component_ok_and_tells_nobody(
        self, tmp_path: Path
    ) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert "bus connection   ok" in done.stdout
        assert "Slack session    ok" in done.stdout
        assert "recent activity  ok" in done.stdout
        assert _messages(told) == [], "a healthy door was reported to somebody"

    def test_the_account_alone_is_not_enough(self, tmp_path: Path) -> None:
        """THE CASE THE OLD RULE GOT WRONG. The front door is connected on the
        same account and the gateway is not there at all."""
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-only-the-account.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10, done.stdout + done.stderr
        assert "bus connection   lost" in done.stdout
        told_texts = _messages(told)
        assert len(told_texts) == 1, f"expected exactly one message, got {told_texts}"
        assert "bus-gateway-factory" in told_texts[0]
        assert "the front door shares it" in told_texts[0], told_texts[0]

    def test_the_name_without_the_subscription_is_not_enough(
        self, tmp_path: Path
    ) -> None:
        """A gateway that has connected and not subscribed answers nothing."""
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-name-but-not-the-subscription.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10
        assert "bus connection   lost" in done.stdout

    def test_the_subscription_under_another_name_is_not_the_gateway(
        self, tmp_path: Path
    ) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-subscription-under-another-name.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10
        assert "bus connection   lost" in done.stdout

    def test_the_other_spelling_of_the_subscription_list_is_read_too(
        self, tmp_path: Path
    ) -> None:
        """The bus answers ``subscriptions_list`` for ``subs=1`` and
        ``subscriptions_list_detail`` for ``subs=detail``. A watch that knew only
        one spelling would call a healthy gateway lost on the other."""
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-with-subscription-detail.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 0, done.stdout + done.stderr

    def test_an_answer_it_cannot_read_is_unknown_and_never_healthy(
        self, tmp_path: Path
    ) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-not-json-at-all.txt",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10, done.stdout + done.stderr
        assert "bus connection   unknown" in done.stdout
        assert "not agreement" in done.stdout

    def test_no_client_name_configured_refuses_to_guess(self, tmp_path: Path) -> None:
        """Falling back to the account would be the old, wrong rule returning
        quietly through the back door."""
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
            client_name="",
        )
        assert done.returncode == 10
        assert "bus connection   unknown" in done.stdout
        assert "refuses to guess" in done.stdout


class TestTheWatchReportsSlackSeparately:
    """The second miss Codex named: Slack can die while the bus is fine."""

    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:
            pytest.skip("jq is not on this machine, and the heartbeat is JSON")

    def test_slack_lost_while_the_bus_is_ok(self, tmp_path: Path) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-disconnected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10, done.stdout + done.stderr
        assert "bus connection   ok" in done.stdout
        assert "Slack session    lost" in done.stdout
        told_texts = _messages(told)
        assert len(told_texts) == 1
        assert "lost its Slack session" in told_texts[0], told_texts[0]

    def test_a_heartbeat_older_than_a_rotation_is_lost(self, tmp_path: Path) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-stale.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10
        assert "Slack session    lost" in done.stdout
        assert "21600s" in done.stdout

    def test_an_absent_heartbeat_is_unknown_and_never_healthy(
        self, tmp_path: Path
    ) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10, done.stdout + done.stderr
        assert "Slack session    unknown" in done.stdout
        assert "never healthy" in done.stdout

    def test_a_heartbeat_that_is_not_the_right_shape_is_unknown(
        self, tmp_path: Path
    ) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-not-json.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10
        assert "Slack session    unknown" in done.stdout


class TestTheWatchKeepsTheOldStalledLogCheck:
    """The retired alarm's third signal, kept, against the container's log."""

    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:
            pytest.skip("jq is not on this machine")

    def test_a_door_silent_past_the_backstop_is_stalled(self, tmp_path: Path) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-silent-past-the-backstop.txt",
            notifier_file=told,
        )
        assert done.returncode == 10, done.stdout + done.stderr
        assert "recent activity  stalled" in done.stdout
        assert "gone silent" in _messages(told)[0]

    def test_the_backstop_is_the_old_alarms_own_six_hours(self, tmp_path: Path) -> None:
        """A shorter window turns a quiet Friday into an alarm: a healthy idle
        door is not silent, and its Slack session rotates roughly every five
        hours and logs when it does."""
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert "21600s backstop" in done.stdout, done.stdout

    def test_a_log_with_no_times_is_unknown(self, tmp_path: Path) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-with-no-times.txt",
            notifier_file=told,
        )
        assert done.returncode == 10
        assert "recent activity  unknown" in done.stdout


class TestTheWatchTellsSomebodyTheRightWay:
    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:
            pytest.skip("jq is not on this machine")

    def test_it_reuses_the_retired_alarms_own_setting_names(self) -> None:
        """Not a second notifier with a second set of names to configure."""
        text = _WATCH.read_text()
        for name in (
            "JARVIS_SLACK_BOT_TOKEN",
            "JARVIS_SLACK_CHANNEL_ID",
            "JARVIS_WATCHDOG_ALERT_CHANNEL_ID",
        ):
            assert name in text, f"the watch does not read {name}"
        assert "chat.postMessage" in text, (
            "the watch does not post the way the retired alarm did"
        )

    def test_the_message_names_a_component_and_not_a_unit(
        self, tmp_path: Path
    ) -> None:
        told = tmp_path / "messages.jsonl"
        _watch(
            connz="connz-only-the-account.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        message = _messages(told)[0]
        assert "bus connection" in message
        assert "Slack session" in message
        assert "recent activity" in message
        assert "systemd" not in message and "is-active" not in message, (
            "the message still talks about a systemd unit, which is the thing "
            "this replacement exists to stop saying"
        )

    def test_one_unhappy_look_is_exactly_one_message(self, tmp_path: Path) -> None:
        """Two components wrong is still one message. Rich is told once."""
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-only-the-account.json",
            heartbeat="heartbeat-disconnected.json",
            log="log-silent-past-the-backstop.txt",
            notifier_file=told,
        )
        assert done.returncode == 10
        assert len(_messages(told)) == 1

    def test_a_stand_in_with_nowhere_to_write_is_a_broken_watch(
        self, tmp_path: Path
    ) -> None:
        """Exit 20, not 0. A watch that could not deliver its alarm must never
        look like a healthy door."""
        done = _watch(
            connz="connz-only-the-account.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=tmp_path / "unused.jsonl",
            extra_env={"GATEWAY_WATCH_NOTIFIER_FILE": ""},
        )
        assert done.returncode == 20, done.stdout + done.stderr
        assert "nowhere to go" in done.stderr

    def test_an_unknown_notifier_is_a_broken_watch_too(self, tmp_path: Path) -> None:
        done = _watch(
            connz="connz-only-the-account.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=tmp_path / "unused.jsonl",
            extra_env={"GATEWAY_WATCH_NOTIFIER": "smoke-signals"},
        )
        assert done.returncode == 20
        assert "'slack' or 'file'" in done.stderr

    def test_it_never_names_the_real_slack_host_in_a_rehearsal(
        self, tmp_path: Path
    ) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-only-the-account.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert "Nothing was sent to Slack" in done.stdout, done.stdout


class TestTheCheckAndTheWatchAgree:
    """One estate, one rule. Item 8i of the services check asks the same three
    things as the watch — an item that was laxer than the watch would be a
    second, quieter opinion about whether the door is up."""

    def test_item_8i_asks_for_the_name_and_the_subscription_too(self) -> None:
        check = (ESTATE / "estate-check").read_text()
        assert "connz?auth=1&subs=1" in check, (
            "item 8i still asks connz without subscriptions, so it cannot ask "
            "for the gateway's subscription"
        )
        assert "FACTORY_INSTANCE" in check, (
            "item 8i does not read FACTORY_INSTANCE, so it cannot know the name "
            "the gateway sends"
        )
        assert "agents.command.jarvis" in check

    def test_item_8i_no_longer_matches_the_account_on_its_own(self) -> None:
        """The exact line Codex named: a count of ``authorized_user`` matches,
        with nothing else asked."""
        check = (ESTATE / "estate-check").read_text()
        assert '/bin/grep -o "\\"authorized_user\\":\\"${account}\\""' not in check, (
            "item 8i still counts connections by the account alone, which "
            "reports a stopped gateway as healthy whenever the front door is up"
        )

    def test_item_8is_sentence_says_all_three(self) -> None:
        items = _the_checks_items()
        assert "8i" in items
        sentence = items["8i"][1]
        for word in ("account", "client name", "subscription"):
            assert word in sentence, sentence


class TestTheUnrecoveredDropTheHeartbeatCannotSee:
    """A CORRECTION THE CODE FORCED (26 September 2026).

    The heartbeat is written from the Slack client library's own lifecycle
    listeners, and on a CLOSE that library RECONNECTS FIRST and only then runs
    those listeners. So when the reconnect itself throws — which is exactly what
    an unrecovered Slack drop looks like — the listeners never run, and the file
    keeps saying 'connected' and is never rewritten again. The freshness window
    would catch it after six hours; the retired alarm caught it at the next
    quarter of an hour by reading the SHAPE of the lifecycle in the log.

    Dropping that would have been a real loss of cover, so the watch keeps the
    retired alarm's second signal from the same log it already reads. The design
    named only the silence backstop; this is the design meeting the code.
    """

    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:
            pytest.skip("jq is not on this machine")

    def test_a_drop_with_nothing_after_it_is_slack_lost(self, tmp_path: Path) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-a-drop-that-never-came-back.txt",
            notifier_file=told,
        )
        assert done.returncode == 10, done.stdout + done.stderr
        assert "bus connection   ok" in done.stdout
        assert "Slack session    lost" in done.stdout
        assert "no 'session established' after it" in done.stdout.lower(), done.stdout

    def test_a_healthy_rotation_is_not_a_drop(self, tmp_path: Path) -> None:
        """THE FALSE ALARM THIS HAD TO AVOID. A rotation always logs its trouble
        line immediately followed by an established line, and a watch that read
        the first without the second would alarm every five hours."""
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-a-healthy-rotation.txt",
            notifier_file=told,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Slack session    ok" in done.stdout
        assert _messages(told) == []

    def test_trouble_older_than_the_gateways_own_word_stays_quiet(
        self, tmp_path: Path
    ) -> None:
        """The gateway said 'connected' AFTER the trouble line, so it recovered
        and said so. Somebody else's use of the word 'disconnect' in the same log
        must not become a live Slack failure either."""
        log = tmp_path / "log-trouble-then-the-gateway-spoke.txt"
        log.write_text(
            "2026-09-26T15:00:00.000000000Z Reconnecting...\n"
            "2026-09-26T15:59:55.000000000Z {\"event\": \"nats_subscribed\"}\n"
        )
        told = tmp_path / "messages.jsonl"
        done = subprocess.run(
            [
                "bash",
                str(_WATCH),
                "--once",
                "--connz-file",
                str(_WATCH_FIXTURES / "connz-the-gateway-is-there.json"),
                "--log-file",
                str(log),
                "--now",
                _A_FIXED_NOW,
            ],
            capture_output=True,
            text=True,
            env={
                **{
                    name: os.environ[name]
                    for name in _ONLY_THESE_ARE_INHERITED
                    if name in os.environ
                },
                "JARVIS_NATS_USER": "jarvis",
                "GATEWAY_WATCH_CLIENT_NAME": "bus-gateway-factory",
                "BUS_MONITORING_ADDRESS": "nats:8222",
                # The gateway last spoke at 15:59:30, after the 15:00 trouble.
                "GATEWAY_WATCH_HEARTBEAT_PATH": str(
                    _WATCH_FIXTURES / "heartbeat-connected.json"
                ),
                "GATEWAY_WATCH_NOTIFIER": "file",
                "GATEWAY_WATCH_NOTIFIER_FILE": str(told),
                "TZ": "UTC",
            },
            timeout=60,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Slack session    ok" in done.stdout


class TestABusBlipIsNotALostSlackSession:
    """THE ONE BLOCKER AN INDEPENDENT REVIEW FOUND, 26 September 2026.

    A reviewer restarted a throwaway bus container and touched nothing else.
    Slack was up, the gateway reconnected to the bus by itself, and the watch
    sent one message headed 'The factory's Slack door has lost its Slack
    session'. The cause: the trouble words above were the retired alarm's own
    ('disconnect', 'reconnect', 'connection ... refused') and the gateway
    container's log is NOT a Slack log — jarvis's own bus client writes
    ``nats_disconnect``, ``nats_reconnect`` and ``nats_error`` into it, and only
    slack-sdk ever writes the line that clears the signal.

    That is the retired alarm's own crying-wolf failure reappearing in its
    replacement, with the wrong component named. The log lines below were
    CAPTURED from a real gateway container on a throwaway estate whose bus was
    restarted, not written by hand.
    """

    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:
            pytest.skip("jq is not on this machine")

    def test_the_buss_own_drop_and_reconnect_leave_slack_alone(
        self, tmp_path: Path
    ) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-the-bus-blipped-and-slack-was-fine.txt",
            notifier_file=told,
        )
        assert "Slack session    ok" in done.stdout, (
            "a bus restart is being reported as a lost Slack session, which is "
            "the retired alarm crying wolf again with the wrong component named"
        )
        assert "bus connection   ok" in done.stdout
        assert "recent activity  ok" in done.stdout
        assert done.returncode == 0, done.stdout + done.stderr
        assert _messages(told) == [], (
            "somebody was told about a Slack failure there was not"
        )

    def test_a_bus_error_carrying_slack_sounding_words_is_still_the_bus(
        self, tmp_path: Path
    ) -> None:
        """The belt as well as the braces. jarvis passes the words of whatever
        failed straight through into ``nats_error``, so an error string can carry
        any words at all — including Slack's. A record written by the bus client
        is about the bus whatever it says, and is dropped by its logger name."""
        log = tmp_path / "log-a-bus-error-that-sounds-like-slack.txt"
        log.write_text(
            '2026-09-26T15:59:50.000000000Z {"error_class": "OSError", '
            '"error": "Failed to send a message: the session is no longer active", '
            '"event": "nats_error", "level": "warning", '
            '"logger": "jarvis.infrastructure.nats_client", '
            '"timestamp": "2026-09-26T15:59:50.000000Z"}\n'
        )
        told = tmp_path / "messages.jsonl"
        done = subprocess.run(
            [
                "bash",
                str(_WATCH),
                "--once",
                "--connz-file",
                str(_WATCH_FIXTURES / "connz-the-gateway-is-there.json"),
                "--log-file",
                str(log),
                "--now",
                _A_FIXED_NOW,
            ],
            capture_output=True,
            text=True,
            env={
                **{
                    name: os.environ[name]
                    for name in _ONLY_THESE_ARE_INHERITED
                    if name in os.environ
                },
                "JARVIS_NATS_USER": "jarvis",
                "GATEWAY_WATCH_CLIENT_NAME": "bus-gateway-factory",
                "BUS_MONITORING_ADDRESS": "nats:8222",
                "GATEWAY_WATCH_HEARTBEAT_PATH": str(
                    _WATCH_FIXTURES / "heartbeat-connected.json"
                ),
                "GATEWAY_WATCH_NOTIFIER": "file",
                "GATEWAY_WATCH_NOTIFIER_FILE": str(told),
                "TZ": "UTC",
            },
            timeout=60,
        )
        assert "Slack session    ok" in done.stdout, done.stdout
        assert done.returncode == 0, done.stdout + done.stderr

    def test_a_real_slack_drop_still_speaks_with_bus_noise_all_around_it(
        self, tmp_path: Path
    ) -> None:
        """The fix must not have turned the second signal off. Slack's own words
        in the middle of a bus blip are still an unrecovered Slack drop."""
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-the-gateway-is-there.json",
            heartbeat="heartbeat-connected.json",
            log="log-a-slack-drop-with-bus-noise-around-it.txt",
            notifier_file=told,
        )
        assert "Slack session    lost" in done.stdout, done.stdout
        assert "bus connection   ok" in done.stdout
        assert done.returncode == 10, done.stdout + done.stderr
        assert len(_messages(told)) == 1
        assert "Slack" in _messages(told)[0]

    def test_the_retired_alarms_bare_words_are_gone_from_the_slack_signal(
        self) -> None:
        """A regression guard on the source, because this defect was inherited by
        copying: the Slack trouble pattern must not contain the bare words that
        match jarvis's own bus records."""
        text = _WATCH.read_text()
        pattern = next(
            (
                line
                for line in text.splitlines()
                if line.startswith("readonly SLACK_SESSION_IN_TROUBLE=")
            ),
            "",
        )
        assert pattern, (
            "the watch has no named Slack trouble pattern, so the Slack question "
            "is being asked of words that are not Slack's"
        )
        for bare in ("disconnect", "|reconnect", "connection.*", "failed to connect"):
            assert bare not in pattern.lower(), (
                f"the Slack trouble pattern still contains '{bare}', which "
                "matches jarvis's own bus records in the same log"
            )
        assert 'logger' in text and 'nats_client' in text, (
            "nothing drops the bus client's own records from the Slack question"
        )


class TestAReleaseThatSendsNoNameIsUnknownAndNotDown:
    """MEASURED ON THE ACTUAL RELEASE IMAGE 2026.09.26-2, 26 September 2026:
    it sends no client name, so the bus reports one for its connection at all.
    Calling that gateway 'lost' would alarm Rich every fifteen minutes about a
    door that is working perfectly; calling it 'ok' on the account alone is the
    defect this whole item exists to remove. So it is UNKNOWN — never healthy,
    never a failure — and item 8i says the same thing in the same case."""

    @pytest.fixture(autouse=True)
    def _needs_jq(self) -> None:
        if shutil.which("jq") is None:
            pytest.skip("jq is not on this machine")

    def test_the_watch_says_unknown(self, tmp_path: Path) -> None:
        told = tmp_path / "messages.jsonl"
        done = _watch(
            connz="connz-a-release-that-sends-no-name.json",
            heartbeat="heartbeat-connected.json",
            log="log-fresh.txt",
            notifier_file=told,
        )
        assert done.returncode == 10, done.stdout + done.stderr
        assert "bus connection   unknown" in done.stdout
        assert "before 26 September 2026" in done.stdout

    def test_the_check_says_the_same_thing(self) -> None:
        check = (ESTATE / "estate-check").read_text()
        assert "NONE of them sends a client name" in check, (
            "item 8i has no answer for a gateway from a release that sends no "
            "name, so it would either fail a working door or pass on the "
            "account alone"
        )
