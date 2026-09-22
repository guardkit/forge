"""Run the publisher as its own process.

    python -m forge.publisher --settings <path to its settings file>

It binds where its settings say — the loopback address by default, which is
right for a publisher run outside a container, and every address inside its
own container when it is run as the service the compose fragment describes,
which publishes no port and puts it on a network the coordinator alone
shares. It prints the address it bound to on one line so that whatever
started it can find the port when the kernel picked one, and then serves
until it is stopped.

IT PRINTS NO CREDENTIAL, and it cannot: the only thing it holds is a
:class:`~forge.publisher.credential.Credential`, which shows itself as
"not shown" on every path Python prints an object by, and the settings it
logs carry the credential file's PATH and never its contents.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from typing import Sequence

from forge.publisher.service import Publisher, serve
from forge.publisher.settings import SettingsRefused, load_settings

logger = logging.getLogger("forge.publisher")


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

    publisher = Publisher(settings)
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
