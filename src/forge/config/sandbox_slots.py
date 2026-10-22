"""The numbered slots a project's own sandbox takes its ports from, in one place.

Design "One sandbox per project" (17 October 2026), section 2.2: every
project builds in a sandbox of its own, and each sandbox takes a numbered slot
*k*. Slot *k* gets base + 10·*k* for the build runner, the deploy helper, the
sandbox's git daemon and its two preview ports, so the factory machine's
forwarding of those ports is one range rather than a list. The doors follow the
same numbering: the Git door listens on 31830 + *k* for that sandbox alone, and
the memory door on 31850 + *k*.

Slot 0 is the base block itself, which the shared sandbox from before this
design (``api-test-deploy``) already uses. That sandbox keeps its projects but
never gets a Git door port (section 2.6), so slot 0 is never assigned and a
new sandbox takes a slot from 1 to :data:`MAX_SLOT`.

The ports here are the ones published on the sandbox's machine. Inside each
sandbox the runner, the helper and the git daemon listen on the same ports as
ever (8124, 8125 and 9418); each sandbox is its own small machine.

Preview ports that do not clash (20 October 2026). A preview port is the same
number inside the sandbox as on the sandbox's machine, so a project that uses
8950 + 10·*k* or 8951 + 10·*k* itself (its live app, say) could never start a
preview in slot *k*. Each slot therefore owns a block of ten preview ports,
8950 + 10·*k* to 8959 + 10·*k*, and :func:`choose_preview_ports` takes the
first two the project does not use itself and no slot's factory service has
(slot 4's runner is 8964, in slot 1's block). With no clash that is the usual
pair, so no other project's ports move.

The shared services' fixed ports (22 October 2026). Every slot sandbox reaches
three shared services on the gateway at fixed ports: the embedder on 9000, the
model door on 31823 and the answer service on 31821. 9000 is also 8950 + 10·5,
and the embedder listens on every address of the sandbox's machine, so slot 5's
old usual pair (9000, 9001) could never be published. A preview never takes a
fixed shared port either, so slot 5's usual pair is 9001 and 9002; every other
slot's usual pair is 8950 + 10·*k* and 8951 + 10·*k* as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

__all__ = [
    "ANSWER_PORT",
    "EMBEDDER_PORT",
    "LEGACY_SANDBOX",
    "MAX_SLOT",
    "MODEL_DOOR_PORT",
    "PreviewPortsRefused",
    "SHARED_SERVICE_PORTS",
    "SlotPorts",
    "choose_preview_ports",
    "factory_service_ports",
    "preview_block",
    "slot_ports",
]

#: The shared sandbox from before one sandbox per project. It keeps the
#: projects already in it, takes no new one with a Git door, and has no slot.
LEGACY_SANDBOX: str = "api-test-deploy"

#: How far apart two slots' blocks of ports are.
SLOT_STEP: int = 10

#: Slot 0's ports: the shared sandbox's published ports today.
RUNNER_BASE: int = 8924
HELPER_BASE: int = 8925
GIT_DAEMON_BASE: int = 8918
#: The two preview ports, published on the sandbox machine's loopback with
#: the same number inside (the recipe, sandbox-deploy.sh, says the same).
PREVIEW_BASES: tuple[int, int] = (8950, 8951)

#: The doors, one port per sandbox.
GIT_DOOR_BASE: int = 31830
MEMORY_DOOR_BASE: int = 31850

#: The three shared services every slot sandbox reaches on the gateway, at
#: fixed ports (the recipe's SLOT_EMBEDDER_PORT, SLOT_MODEL_DOOR_PORT and
#: SLOT_ANSWER_PORT; a test holds them equal). No preview ever takes one.
EMBEDDER_PORT: int = 9000
MODEL_DOOR_PORT: int = 31823
ANSWER_PORT: int = 31821
SHARED_SERVICE_PORTS: tuple[int, ...] = (EMBEDDER_PORT, MODEL_DOOR_PORT, ANSWER_PORT)

#: The highest slot, as the recipe and rollout-sandbox take it. Nineteen keeps
#: every Git door port (31831-31849) below the memory doors' (31851-31869);
#: past 19 a Git door port would be another slot's memory door port.
MAX_SLOT: int = 19


@dataclass(frozen=True)
class SlotPorts:
    """The ports of one slot, as published on the sandbox's machine."""

    slot: int
    runner: int
    helper: int
    git_daemon: int
    previews: tuple[int, int]
    git_door: int
    memory_door: int


def _check_slot(slot: int) -> None:
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= MAX_SLOT:
        raise ValueError(
            f"a sandbox's slot is a whole number from 1 to {MAX_SLOT} (slot 0 is "
            f"the ports of {LEGACY_SANDBOX}, which has no Git door)"
        )


def _free_in_block(slot: int, taken: "set[int] | frozenset[int]") -> list[int]:
    """The ports of slot ``slot``'s preview block that are neither in ``taken``
    nor a factory or shared service's (:func:`factory_service_ports`)."""
    services = factory_service_ports()
    return [port for port in preview_block(slot) if port not in taken and port not in services]


def slot_ports(slot: int) -> SlotPorts:
    """Slot ``slot``'s ports. Raises ``ValueError`` for a slot outside 1 to
    :data:`MAX_SLOT` (slot 0 is the shared sandbox's, which gets no door).

    Its ``previews`` are the slot's usual pair: the first two ports of its
    block that no service has (:func:`choose_preview_ports` for a project that
    uses none of them), 8950 + 10·*k* and 8951 + 10·*k* except in slot 5."""
    _check_slot(slot)
    step = SLOT_STEP * slot
    first, second = _free_in_block(slot, frozenset())[:2]
    return SlotPorts(
        slot=slot,
        runner=RUNNER_BASE + step,
        helper=HELPER_BASE + step,
        git_daemon=GIT_DAEMON_BASE + step,
        previews=(first, second),
        git_door=GIT_DOOR_BASE + slot,
        memory_door=MEMORY_DOOR_BASE + slot,
    )


class PreviewPortsRefused(ValueError):
    """No two preview ports are left in a slot's block; the message says why."""


def preview_block(slot: int) -> range:
    """The ten ports slot ``slot``'s previews may use: 8950 + 10·*k* to
    8959 + 10·*k*. No other slot's preview block overlaps it, but three of its
    ports may be other slots' factory services (8954 + 10·*k* and 8955 + 10·*k*
    are slot *k* + 3's runner and helper, 8958 + 10·*k* slot *k* + 4's git
    daemon), and slot 5's first port is the embedder's 9000, all of which
    :func:`choose_preview_ports` skips. Raises ``ValueError`` for a slot
    outside 1 to :data:`MAX_SLOT`, as :func:`slot_ports` does."""
    _check_slot(slot)
    first = PREVIEW_BASES[0] + SLOT_STEP * slot
    return range(first, first + SLOT_STEP)


def factory_service_ports() -> frozenset[int]:
    """Every port the slot layout gives a factory service of any slot, slot 0's
    included: the build runner, the deploy helper and the git daemon, and the
    two doors; and the three shared services' fixed ports
    (:data:`SHARED_SERVICE_PORTS`). A preview never takes one (Codex review R1,
    20 October 2026): slot 4's runner and helper are 8964 and 8965, inside
    slot 1's block, and the embedder's 9000 is the first port of slot 5's. The
    slots' usual preview pairs are never among them."""
    reserved: set[int] = {RUNNER_BASE, HELPER_BASE, GIT_DAEMON_BASE, *SHARED_SERVICE_PORTS}
    for slot in range(1, MAX_SLOT + 1):
        step = SLOT_STEP * slot
        reserved |= {
            RUNNER_BASE + step,
            HELPER_BASE + step,
            GIT_DAEMON_BASE + step,
            GIT_DOOR_BASE + slot,
            MEMORY_DOOR_BASE + slot,
        }
    return frozenset(reserved)


def choose_preview_ports(slot: int, used: "Iterable[int]") -> tuple[int, int]:
    """The first two ports in slot ``slot``'s block that the project does not
    use itself (``used``: the ports its deploy profile declares, see
    :func:`forge.deploy.profile.ports_the_project_uses`) and that the slot
    layout does not give a factory service (:func:`factory_service_ports`).

    With none of the usual pair in ``used`` this is the usual pair. Raises
    :class:`PreviewPortsRefused` when fewer than two are left.
    """
    block = preview_block(slot)
    taken = {int(port) for port in used}
    services = factory_service_ports()
    free = _free_in_block(slot, taken)
    if len(free) < 2:
        mine = sorted(port for port in block if port in taken)
        kept = sorted(port for port in block if port in services and port not in taken)
        whose = (
            "other slots' services and the shared services"
            if any(port in SHARED_SERVICE_PORTS for port in kept)
            else "other slots' services"
        )
        kept_text = (
            f", and the factory keeps {', '.join(str(p) for p in kept)} for {whose}"
            if kept
            else ""
        )
        raise PreviewPortsRefused(
            f"slot {slot}'s preview ports are {block[0]} to {block[-1]}, and the project "
            f"itself uses {len(mine)} of them ({', '.join(str(p) for p in mine)}){kept_text}, "
            "so fewer than two are left for its previews; move some of the project's own "
            f"ports out of {block[0]} to {block[-1]} in its deploy profile, or give it another slot"
        )
    return free[0], free[1]
