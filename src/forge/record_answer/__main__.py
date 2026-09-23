"""Run the coordinator's read-only answer as its own process.

    python -m forge.record_answer --ledger <the coordinator's record> \
        [--host 127.0.0.1] [--port 8126]

It binds where it is told — the loopback address by default, because the
helper that asks it usually runs beside it — and prints the address it bound
to on one line, so whatever started it can find the port when the kernel picked
one. Then it answers until it is stopped.

The record is opened READ-ONLY on every question, so this process cannot write
to it however it is asked. The address printed here is what the helper's own
setting (``FORGE_TARGET_OWNER_URL``) has to name, with this service's route on
the end: ``http://<host>:<port>/recorded``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Sequence

from forge.record_answer.service import (
    ANSWER_ROUTE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    serve,
)

logger = logging.getLogger("forge.record_answer")

#: The setting the rest of the estate already names the coordinator's record
#: with. Used only as the default for ``--ledger``, so an operator who has it
#: set does not have to say the same path twice.
LEDGER_ENV: str = "FORGE_DB_PATH"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="forge-record-answer",
        description=(
            "Answer, read-only, what commit a build was recorded as starting "
            "from and which build holds a deployment target — and nothing else."
        ),
    )
    parser.add_argument(
        "--ledger",
        default=os.environ.get(LEDGER_ENV, ""),
        help=(
            "the coordinator's own record; defaults to the path in "
            f"{LEDGER_ENV}"
        ),
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--log-level", default="INFO", help="INFO by default; DEBUG for more"
    )
    parsed = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=getattr(logging, str(parsed.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ledger = str(parsed.ledger or "").strip()
    if not ledger:
        print(
            "this service reads the coordinator's own record, so it has to be "
            f"told where it is: pass --ledger, or set {LEDGER_ENV}.",
            file=sys.stderr,
        )
        return 2
    if not Path(ledger).expanduser().exists():
        # Said at the door rather than on the first question, because a
        # service pointed at nothing answers nothing and an operator should
        # learn that now rather than during a deploy.
        print(
            f"there is no record at {ledger}. That is the path this service "
            "was told to read; point it at the coordinator's own record.",
            file=sys.stderr,
        )
        return 2

    server, _thread = serve(ledger=ledger, host=parsed.host, port=int(parsed.port))
    host, port = server.server_address[0], server.server_address[1]
    address = f"http://{host}:{port}{ANSWER_ROUTE}"
    logger.info("record answer: reading %s, answering at %s", ledger, address)
    # ONE LINE, on stdout, so a parent process can find the port the kernel
    # picked. It carries an address and a path, and nothing else.
    print(json.dumps({"listening_on": address}), flush=True)

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
