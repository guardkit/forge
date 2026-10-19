"""Holds for the commands that run outside the coordinator (one sandbox per
project, stage S2, design section 2.3a, Codex S2 round 1, R3).

The coordinator's gate lives in its own process, so a command run beside it —
``register-repo``, ``forge merge-deploy``, ``forge cancel`` — cannot take it.
Instead, before touching a managed sandbox, such a command takes a hold at the
starter: the starter starts the sandbox if it is stopped, refuses every stop
while the hold is open, and the command ends the hold when it is done.
``until=services`` returns once the helper and runner answer;
``until=deployed`` once the coordinator has recorded the redeploy as ready.

Holds never expire. A command that is killed leaves its hold open and the
sandbox simply stays up; the starter's status lists it with its owner and age.

A sandbox is managed here exactly as the coordinator decides it: its
``planning.sandboxes`` entry says ``idle_stop`` and
``planning.sandbox_starter.url`` is set. For anything else these are nothing
at all. When a hold cannot be taken the command refuses in one plain sentence.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import socket
import sqlite3
from collections.abc import Iterator, Mapping
from typing import Any

from forge.sandbox_gate.starter_client import UNSAID, StarterClient

__all__ = [
    "HoldRefused",
    "hold_for_config",
    "managed_in_settings",
    "starter_hold",
]


class HoldRefused(Exception):
    """A hold could not be taken; the message is the command's one sentence."""


def managed_in_settings(settings: Mapping[str, Any] | None, sandbox: str) -> str | None:
    """The starter's address when ``sandbox`` is managed in these coordinator
    settings (a parsed ``forge.yaml``), else ``None``."""
    planning = (settings or {}).get("planning") if isinstance(settings, Mapping) else None
    if not isinstance(planning, Mapping):
        return None
    starter = planning.get("sandbox_starter")
    url = starter.get("url") if isinstance(starter, Mapping) else None
    if not url:
        return None
    entries = planning.get("sandboxes")
    if not isinstance(entries, Mapping):
        return None
    for entry in entries.values():
        if isinstance(entry, Mapping) and entry.get("name") == sandbox and entry.get("idle_stop") is True:
            return str(url).rstrip("/")
    return None


def _owner(command: str) -> str:
    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001 — a name for the status listing only
        who = "unknown"
    return f"{command} ({who}@{socket.gethostname()}, pid {os.getpid()})"[:200]


@contextlib.contextmanager
def starter_hold(
    starter_url: str,
    sandbox: str,
    *,
    command: str,
    until: str,
    deployment: dict[str, str] | None = UNSAID,
    client: Any | None = None,
) -> Iterator[str]:
    """Hold ``sandbox`` open for ``command``; yields the hold id."""
    starter = client or StarterClient(starter_url)
    answer = starter.hold_sync(
        sandbox, owner=_owner(command), until=until, deployment=deployment
    )
    if not answer.ok:
        raise HoldRefused(
            f"{command} did not run: {sandbox} is started and stopped by the sandbox "
            f"starter, and it could not be held open for this command "
            f"({answer.sentence or 'no reason given'}). Nothing was changed."
        )
    hold_id = str(answer.hold_id)
    try:
        yield hold_id
    finally:
        # An end whose answer is lost leaves the hold open: the sandbox stays
        # up, the safe direction, and the starter lists it for a session.
        starter.release_sync(hold_id)


def hold_for_config(
    config: Any,
    repo: str | None,
    *,
    command: str,
    until: str,
    connection: sqlite3.Connection | None = None,
    client: Any | None = None,
) -> contextlib.AbstractContextManager[Any]:
    """A hold on ``repo``'s sandbox when it is managed; nothing otherwise."""
    from forge.sandbox_gate.gate import managed_sandboxes

    planning = getattr(config, "planning", None)
    url = getattr(getattr(planning, "sandbox_starter", None), "url", None)
    if not url or not repo:
        return contextlib.nullcontext()
    for name, (entry, repos) in managed_sandboxes(config).items():
        if repo in repos:
            starter = client or StarterClient(url)
            deployment: dict[str, str] | None = UNSAID
            if connection is not None:
                # ALWAYS SAID (Codex S2 implementation review 1, R1): the
                # starter keeps what it was told, so an omitted deployment
                # would leave an earlier "none" in force.
                from forge.sandbox_gate.deployment import deployment_to_tell

                said = starter.status_sync(name)
                deployment, _warning = deployment_to_tell(
                    connection, repos, entry, starter_says=said.deployment
                )
            return starter_hold(
                url,
                name,
                command=command,
                until=until,
                deployment=deployment,
                client=starter,
            )
    return contextlib.nullcontext()
