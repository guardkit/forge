"""A client for the sandbox starter's HTTP interface (one sandbox per project,
stage S2, design section 2.3a, 18 October 2026).

The starter is a small service on the sandbox machine. It starts, stops and
reports on a project's sandbox, one operation at a time per sandbox, and only
for the names in its own list. The coordinator decides when; this module is
how it asks. It never raises: an address that cannot be reached, or an answer
that cannot be read, comes back as an answer that says so in plain words.

The interface this client speaks is the starter's own
(``deploy/compose/sandbox-starter/starter.py``):

    GET    /status/<name>    -> {"ok": true, "sandbox", "container": {"name", "state"},
                                 "vm": {"state", ...}, "operations": {"queued": [...],
                                 "running": {...} | null, "recovering": [...],
                                 "unresolved": null | "<reason>", "last"},
                                 "holds": [{"id", "owner", "until", "age_seconds", ...}],
                                 "managed": {"state": "active" | "suspended", "reason"},
                                 "services_ready", "services": {"helper", "runner"},
                                 "readiness", "redeploy_pending", "deployment"}
    POST   /start/<name>     blocks until both services answer (up to the
                             starter's ready limit) -> {"ok", "readiness", ...}
    POST   /stop/<name>      blocks until the supervisor has exited and the VM
                             shows stopped (about seven minutes at most)
    POST   /hold/<name>?owner=..&until=services|deployed
                             blocks until the sandbox is there -> {"hold_id"};
                             until=deployed waits up to 15 minutes for /ready
    DELETE /hold/<id>
    POST   /ready/<name>     refused when the supervisor is not running
    POST   /suspend/<name>?reason=..

Start, stop and hold also take the recorded deployment the starter checks the
engine's containers against: ``compose_project`` and ``identity_label``
(``key=value``), or ``deployment=none`` for none; unsaid, the starter uses its
own settings file. A refusal answers 409 (403 for the shared sandbox, 404 for a
name not in the starter's list) and a failure 503, each with
``{"ok": false, "message"}``. ``readiness`` is one of ``ready``, ``services
ready, redeploy pending``, ``starting``, ``stopping``, ``stopped`` or
``unknown``.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "NO_DEPLOYMENT",
    "REDEPLOY_PENDING",
    "UNSAID",
    "deployment_query",
    "read_status",
    "HoldAnswer",
    "StarterAnswer",
    "StarterClient",
    "StarterStatus",
    "Transport",
    "urllib_transport",
]

#: ``(method, url, body or None, timeout seconds) -> (HTTP status, decoded JSON
#: or text)``. Raises for a transport failure; the client turns that into words.
Transport = Callable[[str, str, Any, float], tuple[int, Any]]

#: How long each blocking call may take, with a margin over the starter's own
#: limits: a start waits for both services (its ready limit, 600 s); a stop for
#: the supervisor's 210 s stop allowance, ``docker wait`` and the VM's own stop
#: (about seven minutes); an ``until=deployed`` hold up to 15 minutes for the
#: coordinator's ``/ready``.
START_TIMEOUT_SECONDS: float = 700.0
STOP_TIMEOUT_SECONDS: float = 540.0
HOLD_SERVICES_TIMEOUT_SECONDS: float = 700.0
HOLD_DEPLOYED_TIMEOUT_SECONDS: float = 1000.0

#: Say nothing about the recorded deployment: the starter's settings file decides.
UNSAID = None
#: Tell the starter there is no recorded deployment.
NO_DEPLOYMENT: dict[str, str] = {}


def urllib_transport(method: str, url: str, body: Any, timeout: float) -> tuple[int, Any]:
    """The standard-library transport: one request, its status and JSON body."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(1_000_000)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read(1_000_000) if exc.fp is not None else b""
        status = int(exc.code)
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text) if text.strip() else {}
    except ValueError:
        return status, text


@dataclass(frozen=True)
class StarterStatus:
    """What the starter says about one sandbox, read defensively."""

    name: str
    #: Whether the starter answered at all.
    reached: bool
    #: Whether the starter manages this name.
    listed: bool = True
    phase: str = ""
    #: An operation is queued, running or recovering at the starter.
    busy: bool = False
    #: The starter has given up on the sandbox until a session looks.
    unresolved: str | None = None
    #: Automatic management is suspended, with the reason.
    suspended: str | None = None
    #: The starter's services are up and it waits for the coordinator's redeploy.
    redeploy_pending: bool = False
    #: The sandbox's supervisor is up, as the starter sees it.
    services_up: bool = False
    #: The sandbox is stopped, as the starter sees it.
    stopped: bool = False
    holds: tuple[dict[str, Any], ...] = ()
    #: The deployment the starter checks containers against now, if any.
    deployment: dict[str, Any] | None = None
    #: Why the status could not be read, when it could not.
    why: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        """Nothing queued, running or recovering at the starter (design 2.3:
        the condition for leaving ``unknown``)."""
        return self.reached and self.listed and not self.busy and not self.unresolved


@dataclass(frozen=True)
class StarterAnswer:
    """The starter's answer to start, stop, ready or suspend."""

    ok: bool
    #: A clear refusal: nothing was done, and the starter said why.
    refused: bool = False
    sentence: str = ""


@dataclass(frozen=True)
class HoldAnswer:
    """The starter's answer to a hold."""

    hold_id: str | None
    sentence: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.hold_id)


def _text(value: Any) -> str | None:
    if value is None or value is False:
        return None
    if value is True:
        return "yes"
    text = str(value).strip()
    return text or None


def _said(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("refusal") or body.get("error") or "")
    return str(body or "")[:300]


#: The supervisor container states that mean it is not running.
_CONTAINER_DOWN = frozenset({"exited", "created", "dead", "missing", "removing", "paused"})
#: The readiness the starter reports while its services answer.
REDEPLOY_PENDING = "services ready, redeploy pending"


def read_status(name: str, status: int, body: Any) -> StarterStatus:
    """A :class:`StarterStatus` from one HTTP answer of ``GET /status/<name>``."""
    if status in (403, 404):
        return StarterStatus(
            name=name,
            reached=True,
            listed=False,
            why=_said(body) or f"the starter does not manage {name}",
        )
    if status != 200 or not isinstance(body, dict):
        return StarterStatus(
            name=name,
            reached=False,
            why=f"the starter answered {status} for {name}'s status ({_said(body)[:200]})",
        )
    readiness = str(body.get("readiness") or "").strip().lower()
    operations = body.get("operations") if isinstance(body.get("operations"), dict) else {}
    busy = bool(
        operations.get("queued") or operations.get("running") or operations.get("recovering")
    ) or readiness in ("starting", "stopping")
    unresolved = _text(operations.get("unresolved"))
    managed = body.get("managed") if isinstance(body.get("managed"), dict) else {}
    suspended = None
    if str(managed.get("state") or "").lower() == "suspended":
        suspended = _text(managed.get("reason")) or "suspended"
    container = body.get("container")
    state = str(container.get("state") if isinstance(container, dict) else container or "")
    state = state.strip().lower()
    holds_raw = body.get("holds")
    holds = tuple(h for h in holds_raw if isinstance(h, dict)) if isinstance(holds_raw, list) else ()
    return StarterStatus(
        name=name,
        reached=True,
        listed=True,
        phase=readiness,
        busy=busy,
        unresolved=unresolved,
        suspended=suspended,
        redeploy_pending=bool(body.get("redeploy_pending")) or readiness == REDEPLOY_PENDING,
        services_up=state == "running",
        stopped=state in _CONTAINER_DOWN or (readiness == "stopped" and state != "running"),
        holds=holds,
        deployment=body.get("deployment") if isinstance(body.get("deployment"), dict) else None,
        raw=body,
    )


def deployment_query(deployment: dict[str, str] | None) -> dict[str, str]:
    """The query parameters that tell the starter the recorded deployment.

    ``None`` says nothing (the starter's settings file decides); an empty
    mapping says there is none; otherwise ``compose_project`` and
    ``identity_label`` (``key=value``).
    """
    if deployment is None:
        return {}
    if not deployment:
        return {"deployment": "none"}
    return {
        "compose_project": str(deployment.get("compose_project") or ""),
        "identity_label": str(deployment.get("identity_label") or ""),
    }


class StarterClient:
    """Asks the starter about, and for, one sandbox at a time. Never raises."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._base = str(base_url).rstrip("/")
        self._transport = transport or urllib_transport
        self._timeout = float(timeout_s)

    @property
    def base_url(self) -> str:
        return self._base

    def _ask(
        self, method: str, path: str, body: Any = None, timeout: float | None = None
    ) -> tuple[int | None, Any, str | None]:
        url = f"{self._base}{path}"
        try:
            status, decoded = self._transport(method, url, body, timeout or self._timeout)
        except Exception as exc:  # noqa: BLE001 — a transport boundary
            return None, None, (
                f"the sandbox starter at {self._base} could not be reached "
                f"({type(exc).__name__}: {exc})"
            )
        return status, decoded, None

    @staticmethod
    def _quoted(name: str) -> str:
        return urllib.parse.quote(str(name), safe="")

    @staticmethod
    def _with(path: str, query: dict[str, str]) -> str:
        return f"{path}?{urllib.parse.urlencode(query)}" if query else path

    # -- the synchronous calls (the out-of-process commands use these) --------

    def status_sync(self, name: str) -> StarterStatus:
        status, body, why = self._ask("GET", f"/status/{self._quoted(name)}")
        if status is None:
            return StarterStatus(name=name, reached=False, why=why)
        return read_status(name, status, body)

    def _post_sync(self, path: str, *, timeout: float | None = None) -> StarterAnswer:
        status, decoded, why = self._ask("POST", path, None, timeout)
        if status is None:
            return StarterAnswer(ok=False, sentence=why or "no answer")
        said = _said(decoded)
        if status == 200 and not (isinstance(decoded, dict) and decoded.get("ok") is False):
            return StarterAnswer(ok=True, sentence=said)
        if status in (400, 403, 404, 409, 423):
            return StarterAnswer(ok=False, refused=True, sentence=said or f"refused ({status})")
        return StarterAnswer(ok=False, sentence=said or f"the starter answered {status}")

    def start_sync(self, name: str, *, deployment: dict[str, str] | None = UNSAID) -> StarterAnswer:
        return self._post_sync(
            self._with(f"/start/{self._quoted(name)}", deployment_query(deployment)),
            timeout=START_TIMEOUT_SECONDS,
        )

    def stop_sync(self, name: str, *, deployment: dict[str, str] | None = UNSAID) -> StarterAnswer:
        return self._post_sync(
            self._with(f"/stop/{self._quoted(name)}", deployment_query(deployment)),
            timeout=STOP_TIMEOUT_SECONDS,
        )

    def ready_sync(self, name: str, *, deployment: dict[str, str] | None = UNSAID) -> StarterAnswer:
        """The redeploy is confirmed for this deployment (the starter refuses a
        ready that names another than its current one)."""
        return self._post_sync(
            self._with(f"/ready/{self._quoted(name)}", deployment_query(deployment))
        )

    def clear_sync(self, name: str) -> StarterAnswer:
        """A session clears a suspension (tests and tools; the coordinator never)."""
        return self._post_sync(f"/clear/{self._quoted(name)}")

    def suspend_sync(self, name: str, reason: str) -> StarterAnswer:
        return self._post_sync(
            self._with(f"/suspend/{self._quoted(name)}", {"reason": str(reason)[:500] or "unstated"})
        )

    def hold_sync(
        self,
        name: str,
        *,
        owner: str,
        until: str,
        deployment: dict[str, str] | None = UNSAID,
        timeout_s: float | None = None,
    ) -> HoldAnswer:
        """Take a hold; the call returns once the sandbox is there (``until``).

        A hold the starter opened and then ended because the call failed comes
        back with no id and its sentence: there is nothing to release.
        """
        query = {"owner": owner, "until": until, **deployment_query(deployment)}
        wait = timeout_s or (
            HOLD_DEPLOYED_TIMEOUT_SECONDS if until == "deployed" else HOLD_SERVICES_TIMEOUT_SECONDS
        )
        status, decoded, why = self._ask(
            "POST", self._with(f"/hold/{self._quoted(name)}", query), None, wait
        )
        if status is None:
            return HoldAnswer(None, why or "no answer")
        if status == 200 and isinstance(decoded, dict) and decoded.get("ok") is not False:
            hold_id = _text(decoded.get("hold_id"))
            if hold_id:
                return HoldAnswer(hold_id)
        return HoldAnswer(None, _said(decoded) or f"the starter answered {status} to the hold")

    def release_sync(self, hold_id: str) -> bool:
        status, _decoded, _why = self._ask("DELETE", f"/hold/{self._quoted(hold_id)}")
        return status == 200

    # -- the coordinator's calls ----------------------------------------------

    async def status(self, name: str) -> StarterStatus:
        return await asyncio.to_thread(self.status_sync, name)

    async def start(self, name: str, *, deployment: dict[str, str] | None = UNSAID) -> StarterAnswer:
        return await asyncio.to_thread(lambda: self.start_sync(name, deployment=deployment))

    async def stop(self, name: str, *, deployment: dict[str, str] | None = UNSAID) -> StarterAnswer:
        return await asyncio.to_thread(lambda: self.stop_sync(name, deployment=deployment))

    async def ready(self, name: str, *, deployment: dict[str, str] | None = UNSAID) -> StarterAnswer:
        return await asyncio.to_thread(lambda: self.ready_sync(name, deployment=deployment))

    async def suspend(self, name: str, reason: str) -> StarterAnswer:
        return await asyncio.to_thread(self.suspend_sync, name, reason)
