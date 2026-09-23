"""The coordinator's read-only answer: two questions about its own record.

WHY THIS EXISTS (27 September 2026, the seventh review). The helper that runs
a project's deploy scripts cannot see the coordinator's record and never will.
Two of its gates need one fact each out of that record, and until this service
there was nothing anywhere in the estate to ask:

* **which commit a build was recorded as starting from.** The helper reads a
  project's own two declaration files — the launch settings in its
  ``.guardkit/config.yaml`` and the identity names in its
  ``deploy/profile.yaml`` — at a commit, and the commit a request names is
  honoured only when the coordinator confirms it is the one recorded for that
  build. With nobody to ask, the helper refuses every request that names a
  commit, which is the safe side and which stops the factory deploying: the
  coordinator stamps that commit on every deploy request for a build that came
  through planning. This service is the other end of that check;
* **who owns a deployment target.** The executor inside the helper asks this
  when its own note for a target is gone and nothing is alive on it: a delayed
  request presenting an old counter cannot establish who owns the target, so
  the question goes to the thing that granted it. That question was designed
  first and had no answer either.

WHAT IT ANSWERS, and nothing else:

    GET <route>?build=<build>   -> {"build": …, "recorded": …,
                                    "start_commit": … or null}
    GET <route>?target=<target> -> {"target": …, "recorded": …,
                                    "counter": …, "build": … or null}
    GET /healthz                -> {"status": "healthy"}

READ-ONLY IN THE STRONG SENSE, the way the publisher reads the same record:
the connection is opened through the URI form with ``mode=ro``, so the store
itself refuses a write on it. It is not a habit kept by this module, it is a
property of the handle. Nothing here writes and nothing here can.

WHAT IT NEVER DOES. It never takes anything from the asker but the name of a
build or a target: there is no route that changes anything, no verb but GET,
and no field on the way in that reaches the record except those two names, each
passed as a bound parameter. It holds no credential — a question about a build
is not a secret, and this service could not tell one if it held one. It never
guesses: a build nobody wrote a starting commit for answers "recorded: false"
with no commit, which is the honest state, and a record that cannot be read at
all is an unanswered question (503) rather than an answer, so every gate on the
far side treats it as "nobody said" and refuses rather than letting something
in.

Nothing in this module names a language, a test runner, a package manager, a
product, a hosting provider or any project's layout. It reads two rows out of
the coordinator's own record.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "ANSWER_ROUTE",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HEALTH_ROUTE",
    "RecordAnswerHandler",
    "TheCoordinatorsRecord",
    "TheRecordIsUnreadable",
    "serve",
]

#: The one route that answers a question about the record.
ANSWER_ROUTE: str = "/recorded"

#: The route that says the service is alive.
HEALTH_ROUTE: str = "/healthz"

#: Where it listens by default: the loopback address, because the thing that
#: asks it runs beside it. An operator whose helper is somewhere else binds it
#: where that helper can reach and says so in the helper's own setting.
DEFAULT_HOST: str = "127.0.0.1"

#: The port it listens on by default — beside the two the factory's own
#: services already use.
DEFAULT_PORT: int = 8126

#: How long a read waits on the record before giving up. Short: the asker is
#: settling a request while somebody waits.
READ_TIMEOUT_SECONDS: float = 10.0


class TheRecordIsUnreadable(RuntimeError):
    """The record could not be opened or read, said in one sentence."""


class TheCoordinatorsRecord:
    """Opens the coordinator's record read-only and reads two things out.

    One handle per question, opened and closed around it: this service answers
    rarely and holding a handle open across a coordinator's own writes buys
    nothing.
    """

    def __init__(self, ledger: str | Path) -> None:
        self._path = Path(ledger).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        if not self._path.exists():
            raise TheRecordIsUnreadable(
                "this service cannot read the coordinator's record: there is "
                f"nothing at {self._path}"
            )
        try:
            return sqlite3.connect(
                f"file:{self._path}?mode=ro", uri=True, timeout=READ_TIMEOUT_SECONDS
            )
        except sqlite3.Error as exc:
            raise TheRecordIsUnreadable(
                "this service cannot read the coordinator's record: the record "
                f"at {self._path} could not be opened read-only ({exc})"
            ) from exc

    def _one_row(self, statement: str, name: str) -> Any:
        connection = self._connect()
        try:
            try:
                return connection.execute(statement, (str(name),)).fetchone()
            except sqlite3.Error as exc:
                raise TheRecordIsUnreadable(
                    "this service cannot read the coordinator's record: the "
                    f"record at {self._path} would not answer about "
                    f"{name!r} ({exc})"
                ) from exc
        finally:
            connection.close()

    def what_build_starts_from(self, build: str) -> dict[str, Any]:
        """What commit was this build recorded as starting from?

        ``recorded`` false with no commit for a build nobody wrote one for —
        a build queued before the starting rule existed, or one queued by hand
        with no planning behind it. That is a fact about the record, never a
        puzzle this service solves some other way.
        """
        row = self._one_row(
            "SELECT start_commit FROM builds WHERE build_id = ?", build
        )
        commit = str(row[0]).strip() if row is not None and row[0] else ""
        return {
            "build": str(build),
            "recorded": bool(commit),
            "start_commit": commit or None,
        }

    def who_owns(self, target: str) -> dict[str, Any]:
        """Which build holds this deployment target, and at which counter?

        ``recorded`` false with counter 0 for a target nothing has ever been
        deployed to. The asker compares both halves against what its request
        carried, so an absent row refuses that request rather than admitting
        it.
        """
        row = self._one_row(
            "SELECT counter, holder_build FROM deployment_targets WHERE target = ?",
            target,
        )
        if row is None:
            return {
                "target": str(target),
                "recorded": False,
                "counter": 0,
                "build": None,
            }
        holder = str(row[1]).strip() if row[1] else ""
        return {
            "target": str(target),
            "recorded": True,
            "counter": int(row[0] or 0),
            "build": holder or None,
        }


class _AnswerServer(ThreadingHTTPServer):
    """The server carrying one reader of the record."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        record: TheCoordinatorsRecord,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.record = record


class RecordAnswerHandler(BaseHTTPRequestHandler):
    """One question at a time, read-only, and nothing else at all."""

    server_version = "forge-record-answer/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        logger.info("record answer %s - %s", self.address_string(), fmt % args)

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        try:
            parsed = urlparse(self.path)
            if parsed.path == HEALTH_ROUTE:
                self._answer(200, {"status": "healthy"})
                return
            if parsed.path != ANSWER_ROUTE:
                self._answer(
                    404,
                    {
                        "error": (
                            f"this service answers at {ANSWER_ROUTE} and says "
                            f"it is alive at {HEALTH_ROUTE}; there is nothing "
                            f"at {parsed.path}"
                        )
                    },
                )
                return
            asked = parse_qs(parsed.query)
            build = (asked.get("build") or [""])[0].strip()
            target = (asked.get("target") or [""])[0].strip()
            if build and target:
                self._answer(
                    400,
                    {
                        "error": (
                            "this service answers one question at a time: what "
                            "commit a build was recorded as starting from, or "
                            "which build holds a deployment target. This "
                            "request asked both, so neither was answered."
                        )
                    },
                )
                return
            if not build and not target:
                self._answer(
                    400,
                    {
                        "error": (
                            "this request asked nothing: name the build whose "
                            "recorded starting commit you want, or the "
                            "deployment target whose holder you want."
                        )
                    },
                )
                return
            record: TheCoordinatorsRecord = self.server.record  # type: ignore[attr-defined]
            try:
                answer = (
                    record.what_build_starts_from(build)
                    if build
                    else record.who_owns(target)
                )
            except TheRecordIsUnreadable as exc:
                # AN UNANSWERED QUESTION, NEVER AN ANSWER. Everything that asks
                # this service treats "nobody said" as a refusal on the paths
                # that matter, so an unreadable record must not come back
                # looking like a recorded fact.
                logger.warning("record answer: %s", exc)
                self._answer(503, {"error": str(exc)})
                return
            self._answer(200, answer)
        except Exception:  # noqa: BLE001 — never crash the service
            logger.exception("record answer: a question ended in an error")
            try:
                self._answer(
                    500,
                    {
                        "error": (
                            "this service stopped on an unexpected error and "
                            "answered nothing about the record."
                        )
                    },
                )
            except Exception:  # noqa: BLE001 — the socket is already gone
                pass

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        """Nothing here changes anything, so nothing here is sent anything."""
        self._answer(
            405,
            {
                "error": (
                    "this service only answers questions about what the "
                    "coordinator already wrote down; it changes nothing, so it "
                    "takes nothing."
                )
            },
        )

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST


def serve(
    *,
    ledger: str | Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> tuple[_AnswerServer, threading.Thread]:
    """Bind the answer where it is told, on a thread of its own.

    ``port`` 0 means "let the kernel pick", which is what a test and a bench
    want; the bound port is on ``server.server_address``.
    """
    server = _AnswerServer(
        (host, int(port)),
        RecordAnswerHandler,
        record=TheCoordinatorsRecord(ledger),
    )
    thread = threading.Thread(
        target=server.serve_forever, name="forge-record-answer", daemon=True
    )
    thread.start()
    return server, thread
