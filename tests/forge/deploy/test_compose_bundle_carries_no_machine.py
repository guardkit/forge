"""The compose bundle renders with nothing of this machine in it.

WHY THIS TEST EXISTS (24 September 2026). The containerisation rule is not
"no machine paths in the files a person reads" — it is that the thing which
actually runs carries none. A compose file can look clean and still render a
home path, because ``include:`` pulls in another file whose relative paths
resolve against wherever this repository happens to be checked out, and
because a variable with a machine's value in it looks like a name until it is
substituted. That is exactly what happened here on the day this was written:
the publisher's fragment rendered a ``build.context`` of this checkout's
folder and a bind of a file beside it. Both were fixed; this test is what
stops either coming back.

So the check is made on the RENDERED bundle, which is what ``docker compose``
would act on, and it is made with the example env file the repository ships —
so a name added to the bundle without a line in ``.env.example`` fails here
too, by refusing to render.

WHAT COUNTS AS THIS MACHINE:

* any address literal that is not ``0.0.0.0`` (a container binding every
  address of its OWN) and not in ``192.0.2.0/24`` (the IETF's documentation
  block, RFC 5737, which the example uses and which routes nowhere);
* any home directory but the two the images make for their own users;
* this machine's own name, whatever it is called today.

Nothing here names a language, a test runner, a package manager or any
project's layout: it reads one rendered compose document.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

#: forge/deploy/compose — the bundle this test reads.
BUNDLE = Path(__file__).resolve().parents[3] / "deploy" / "compose"

#: Every dotted quad in the rendered output.
_AN_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

#: The two an image legitimately renders.
_ALLOWED_ADDRESSES = ("0.0.0.0",)

#: The IETF's documentation block. The example env uses it for the factory
#: gateway address so that the bundle renders and can be checked; it belongs
#: to nobody and reaches nothing.
_DOCUMENTATION_BLOCK = "192.0.2."

#: The homes the two images make for their own unprivileged users. They are
#: the same on every machine, which is the whole point of them.
_IN_IMAGE_HOMES = ("/home/forge/", "/home/publisher/")


#: What the render is allowed to inherit from whoever is running it. Compose
#: gives the SHELL's own environment precedence over ``--env-file``, so a
#: session that happens to export one of the bundle's setting names would
#: otherwise be rendered instead of the example — which is how this test first
#: read a loopback bus address that is nowhere in the bundle (the suite's own
#: broker fence exports one). The render is therefore made with almost nothing
#: in the environment, and what the example says is what is checked.
_ONLY_THESE_ARE_INHERITED = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "XDG_RUNTIME_DIR",
)


def _rendered() -> str:
    """``docker compose config`` of the bundle, with the example env."""
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
            "forge-compose-bundle-check",
            "--env-file",
            ".env.example",
            "--file",
            "compose.yaml",
            "config",
        ],
        cwd=BUNDLE,
        env=bare,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, (
        "the compose bundle would not render with its own .env.example:\n"
        f"{done.stderr}"
    )
    return done.stdout


@pytest.fixture(scope="module")
def rendered() -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    return _rendered()


class TestTheBundleRenders:
    def test_it_renders_at_all_from_the_example_env(self, rendered: str) -> None:
        """Every name the bundle needs has a line in ``.env.example``."""
        assert "services:" in rendered

    def test_it_has_the_three_services(self, rendered: str) -> None:
        for service in ("coordinator:", "answer-service:", "forge-publisher:"):
            assert service in rendered, f"{service} is missing from the bundle"


class TestNothingOfThisMachineIsInIt:
    def test_no_address_but_the_documentation_one(self, rendered: str) -> None:
        found = {
            address
            for address in _AN_ADDRESS.findall(rendered)
            if address not in _ALLOWED_ADDRESSES
            and not address.startswith(_DOCUMENTATION_BLOCK)
        }

        assert not found, (
            "the rendered bundle carries addresses that belong to a machine: "
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
                f"the rendered bundle carries a home directory: {line.strip()}"
            )

    def test_no_mac_home_directory(self, rendered: str) -> None:
        assert "/Users/" not in rendered

    def test_not_this_machines_name(self, rendered: str) -> None:
        name = socket.gethostname().split(".")[0]
        if not name or len(name) < 3:
            pytest.skip("this machine's name is too short to look for safely")
        assert name.lower() not in rendered.lower()


class TestWhatTheBundlePromises:
    def test_nothing_runs_on_the_host_network(self, rendered: str) -> None:
        assert "network_mode: host" not in rendered

    def test_nothing_binds_a_folder_off_this_machine(self, rendered: str) -> None:
        """Binds are allowed only for the two files a machine supplies.

        The publisher's settings file and its credential file are supplied by
        the machine at paths the env file names. Everything else is a named
        volume, because a bind is a path and a path belongs to a machine.
        """
        allowed_targets = (
            "/etc/forge-publisher/settings.json",
            "/etc/forge-publisher/credential",
        )
        blocks = rendered.split("- type: bind")
        for block in blocks[1:]:
            head = block[:400]
            assert any(target in head for target in allowed_targets), (
                "the bundle binds something other than the publisher's own two "
                f"files:\n{head}"
            )

    def test_only_the_answer_service_publishes_a_port(self, rendered: str) -> None:
        """One crossing of the sandbox boundary, and it is named as such."""
        assert rendered.count("mode: ingress") == 1
        assert "target: 8126" in rendered

    def test_the_settings_volume_is_read_only(self, rendered: str) -> None:
        block = rendered.split("source: forge-settings", 1)
        assert len(block) == 2, "the settings volume is not mounted anywhere"
        assert "read_only: true" in block[1][:200]
