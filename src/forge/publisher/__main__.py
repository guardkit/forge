"""Run the publisher as its own process.

    python -m forge.publisher --settings <path to its settings file>

It binds where its settings say — the loopback address by default, which is
right for a publisher run outside a container, and every address inside its
own container when it is run as the service the compose fragment describes,
which publishes no port and puts it on a network the coordinator alone
shares. It prints the address it bound to on one line so that whatever
started it can find the port when the kernel picked one, and then serves
until it is stopped.

IT REFUSES TO START (2 October 2026) unless its self-check passes: the
credential file is a regular file owned by the publisher's UID with no group or
other access, and it has exactly one network interface besides loopback. The
health route runs the check again on every request and reports
``"self_check": "passed"`` only then; the coordinator asks that before
publication can switch on. What the check does NOT prove is written at
:func:`why_it_will_not_start`.

EVERY ROUTE'S OWN KEY GETS THE SAME CHECKS (13 October 2026): looked at with
``lstat`` and never opened, it has to be a regular file (not a symbolic
link), owned by the publisher's UID, with no group or other access, and not
empty. It also refuses to start when two credential entries are the same
file, or when a route's key could not be read at start; and the health route
looks again at each route key's identity, so a key replaced after start turns
the check to "failed".

IT PRINTS NO CREDENTIAL, and it cannot: the only thing it holds is a
:class:`~forge.publisher.credential.Credential`, which shows itself as
"not shown" on every path Python prints an object by, and the settings it
logs carry the credential file's PATH and never its contents.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import stat
import sys
import threading
from pathlib import Path
from typing import Callable, Sequence

from forge.publisher.service import Publisher, PublisherWillNotStart, serve
from forge.publisher.settings import (
    PublisherSettings,
    SettingsRefused,
    load_settings,
    the_route_key_file,
)

logger = logging.getLogger("forge.publisher")

#: Where the kernel lists this process's network interfaces.
THE_INTERFACES = Path("/sys/class/net")


def why_it_will_not_start(
    settings: PublisherSettings, *, interfaces: Path = THE_INTERFACES
) -> str | None:
    """The self-check. ``None`` when it passes, else why not.

    Run at start (the publisher does not start without it) and again on every
    health-route request, so "passed" is never an old answer.

    WHAT IT PROVES, and no more:

    * the credential file is a regular file owned by the publisher's UID with
      no group or other access;
    * each route's own key, looked at with ``lstat`` and never opened, is a
      regular file and not a symbolic link, owned by the publisher's UID, with
      no group or other access, and not empty;
    * the publisher has exactly one network interface besides loopback.

    WHAT IT DOES NOT PROVE. Several estate containers and the host login share
    UID 1000, so "its own UID" is not "the publisher only": another container
    running as that UID and given the same file could read it. One interface
    does not say WHICH network that is or who else is on it (a host with a
    single interface would also pass). Network membership and reachability
    are checked by ``publisher-host-policy verify`` and by estate-check (items
    7c and 8g); publication does not depend on those checks.
    """
    where = Path(settings.credential_file).expanduser()
    try:
        found = where.stat()
    except OSError as exc:
        return (
            f"the credential file {where} could not be looked at "
            f"({exc.strerror or type(exc).__name__})"
        )
    if not stat.S_ISREG(found.st_mode):
        return f"the credential file {where} is not a file"
    if found.st_uid != os.geteuid():
        return (
            f"the credential file {where} belongs to user {found.st_uid}, not to "
            f"the publisher's own user {os.geteuid()}"
        )
    if found.st_mode & 0o077:
        return (
            f"the credential file {where} can be read or written by others "
            f"(mode {stat.S_IMODE(found.st_mode):o}); it must be readable by "
            "the publisher's own user alone"
        )
    refused = _why_a_route_key_will_not_do(settings)
    if refused:
        return refused
    try:
        names = sorted(entry.name for entry in interfaces.iterdir() if entry.name != "lo")
    except OSError as exc:
        return f"its network interfaces could not be listed ({type(exc).__name__})"
    if len(names) != 1:
        return (
            f"it has {len(names)} network interfaces besides loopback, not "
            "exactly one"
        )
    return None

def _why_a_route_key_will_not_do(settings: PublisherSettings) -> str | None:
    """The same checks for every route's own key, with ``lstat`` alone.

    Never opens a file: this runs again on every health request. Each
    sentence names the project and the path, never the contents.
    """
    named: dict[str, list[str]] = {}
    for route in settings.projects.values():
        if route.credential_name is not None:
            named.setdefault(route.credential_name, []).append(route.name)
    for name in sorted(named):
        projects = sorted(named[name])
        who = (
            f"project{'s' if len(projects) > 1 else ''} "
            + ", ".join(f"'{project}'" for project in projects)
        )
        where = the_route_key_file(name)
        said = f"the credential file {where} named for {who}"
        try:
            found = os.lstat(where)
        except OSError as exc:
            return f"{said} could not be looked at ({exc.strerror or type(exc).__name__})"
        if stat.S_ISLNK(found.st_mode):
            return f"{said} is a symbolic link; a route's key has to be the file itself"
        if not stat.S_ISREG(found.st_mode):
            return f"{said} is not a file"
        if found.st_uid != os.geteuid():
            return (
                f"{said} belongs to user {found.st_uid}, not to the publisher's "
                f"own user {os.geteuid()}"
            )
        if found.st_mode & 0o077:
            return (
                f"{said} can be read or written by others (mode "
                f"{stat.S_IMODE(found.st_mode):o}); it must be readable by the "
                "publisher's own user alone"
            )
        if found.st_size == 0:
            return f"{said} is empty"
    return None


def the_self_check(
    settings: PublisherSettings,
    publisher: Publisher,
    *,
    interfaces: Path = THE_INTERFACES,
) -> Callable[[], str | None]:
    """What the health route runs on every request: the start-up checks, and
    every route key looked at again to be the very file that was read."""

    def check() -> str | None:
        return why_it_will_not_start(
            settings, interfaces=interfaces
        ) or publisher.why_a_route_key_changed()

    return check


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="forge-publisher",
        description=(
            "Send a checked joined commit to its project's recorded target "
            "branch on the remote named origin, and nothing else."
        ),
    )
    parser.add_argument(
        "--settings",
        required=True,
        help="the publisher's settings file (JSON)",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="INFO by default; DEBUG for more"
    )
    parsed = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.getLevelNamesMapping().get(
            str(parsed.log_level).upper(), logging.INFO
        ),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = load_settings(parsed.settings)
    except SettingsRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2

    refused = why_it_will_not_start(settings)
    if refused:
        print(f"the publisher will not start: {refused}", file=sys.stderr)
        return 2
    try:
        publisher = Publisher(settings)
    except PublisherWillNotStart as exc:
        print(f"the publisher will not start: {exc}", file=sys.stderr)
        return 2
    unread = publisher.why_route_keys_refuse_to_start()
    if unread:
        print(f"the publisher will not start: {unread}", file=sys.stderr)
        return 2
    publisher.self_check = the_self_check(settings, publisher)
    logger.info("publisher: settings %s", json.dumps(settings.without_secrets()))
    if not publisher.holds_a_credential:
        logger.warning(
            "publisher: it holds no credential, so every request will be "
            "refused with that reason. Nothing else about it changes."
        )
    server, _thread = serve(settings, publisher=publisher)
    host, port = server.server_address[0], server.server_address[1]
    # ONE LINE, on stdout, so a parent process can find the port the kernel
    # picked. It carries an address and nothing else.
    print(json.dumps({"listening_on": f"http://{host}:{port}"}), flush=True)

    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, _stop)
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())
