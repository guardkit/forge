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
from typing import Sequence

from forge.publisher.service import Publisher, serve
from forge.publisher.settings import PublisherSettings, SettingsRefused, load_settings

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
        level=getattr(logging, str(parsed.log_level).upper(), logging.INFO),
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
    publisher = Publisher(settings)
    publisher.self_check = lambda: why_it_will_not_start(settings)
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

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())
