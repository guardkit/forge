"""``forge runbook`` — CLI commands for runbook execution (TASK-RBX-005).

Wires the executor to the command line. ``forge runbook run <path>`` reads the
JSON, persists it via ``create_runbook`` (ASSUM-007), then runs it through
``RunbookExecutor`` and reports the outcome.

The runbook group is structured as a Click Group so ``forge runbook <verb>``
can grow later (e.g., ``forge runbook list``, ``forge runbook status``).

TASK-FMDR-002: Wired to real shell handlers (register_shell_handlers) and
real NATS publisher (nats.connect) with best-effort publishing semantics.

TASK-FMDR-008: Authenticates to an auth-required broker via operator-supplied
credentials resolved from ``FORGE_NATS_*`` env vars (see
:func:`_resolve_nats_auth`), and fails fast to the NoOp client against an
auth-rejecting broker (``allow_reconnect=False``) instead of spinning in a
reconnect loop. Secrets are never logged — the server URL is sanitised and
scrubbed before every log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import click

from forge.adapters.nats.runbook_publisher import RunbookPublisher
from forge.adapters.sqlite.connect import connect_writer
from forge.executor.executor import RunbookExecutor
from forge.executor.registry import StepTypeRegistry
from forge.executor.shell_steps import register_shell_handlers
from forge.memory.redaction import redact_credentials, scrub_process_output
from forge.persistence.repositories.runbook import (
    RunbookDuplicateError,
    RunbookRepository,
)
from forge.persistence.repositories.runbook_models import Runbook, Step, StepStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime builders (dependency injection boundary)
# ---------------------------------------------------------------------------


def _build_repository(db_path: Path) -> RunbookRepository:
    """Build a RunbookRepository connected to the SQLite database.

    Args:
        db_path: Path to forge.db (SQLite).

    Returns:
        A wired RunbookRepository.
    """
    connection = connect_writer(db_path)
    return RunbookRepository(connection=connection)


def _build_executor(
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    publisher: RunbookPublisher,
) -> RunbookExecutor:
    """Build a RunbookExecutor with the given dependencies.

    Args:
        repository: Persistence repository for loading/updating runbooks.
        registry: Step type registry for resolving handlers.
        publisher: Event publisher for announcing lifecycle events.

    Returns:
        A wired RunbookExecutor.
    """
    return RunbookExecutor(repository, registry, publisher)


# ---------------------------------------------------------------------------
# Runbook parsing and validation
# ---------------------------------------------------------------------------


def _parse_runbook_file(path: Path) -> Runbook:
    """Read and parse a runbook JSON file into a Runbook object.

    Args:
        path: Path to the runbook JSON file.

    Returns:
        A validated Runbook object.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file contains invalid JSON.
        (KeyError, TypeError, ValueError): If the JSON structure is invalid.
    """
    if not path.exists():
        raise FileNotFoundError(f"Runbook file not found: {path}")

    content = path.read_text(encoding="utf-8")
    data = json.loads(content)

    # Extract required fields
    runbook_id = data["runbook_id"]
    target = data["target"]
    current_step_index = data["current_step_index"]
    status_str = data["status"]
    created_at_str = data["created_at"]
    steps_data = data["steps"]

    # Parse status
    status = StepStatus(status_str)

    # Parse created_at
    created_at = datetime.fromisoformat(created_at_str)

    # Parse steps
    steps = []
    for step_data in steps_data:
        step = Step(
            step_type=step_data["step_type"],
            params=step_data.get("params", {}),
            status=StepStatus(step_data["status"]),
            sequence_index=step_data["sequence_index"],
            result=None,  # Steps from JSON have no result yet
        )
        steps.append(step)

    # Build runbook
    runbook = Runbook(
        runbook_id=runbook_id,
        target=target,
        steps=tuple(steps),
        current_step_index=current_step_index,
        status=status,
        created_at=created_at,
    )

    return runbook


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group(name="runbook")
def runbook_cmd() -> None:
    """Runbook execution commands."""


@runbook_cmd.command(name="run")
@click.argument("path", type=click.Path(path_type=Path))
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="Path to forge.db (SQLite). Defaults to ~/.forge/forge.db.",
)
@click.option(
    "--no-events",
    is_flag=True,
    default=False,
    help="Disable NATS event publishing (useful when no broker is available).",
)
def run_cmd(path: Path, db_path: Path | None, no_events: bool) -> None:
    """Execute a runbook from a JSON file.

    Reads the runbook at PATH, persists it to the database, then executes
    its steps in sequence order. Reports completion or escalation reason.

    The runbook is persisted BEFORE execution (ASSUM-007) so results and
    pointer survive a crash mid-run.

    TASK-FMDR-002: Uses real shell handlers and publishes lifecycle events
    to NATS (best-effort). Use --no-events to skip event publishing.

    TASK-FMDR-008: To publish to an auth-required broker, supply credentials
    via the environment (the secret is never logged):

    \b
      FORGE_NATS_CREDS=/path/to/operator.creds   # NSC .creds file, OR
      FORGE_NATS_TOKEN=<token>                    # token auth, OR
      FORGE_NATS_USER=<user> FORGE_NATS_PASSWORD=<pass>   # user+password

    Against an auth-rejecting broker the connect fails fast and the runbook
    still executes (events are dropped). --no-events remains the credential-
    free escape hatch.
    """
    # 1. Read and parse the runbook file
    try:
        runbook = _parse_runbook_file(path)
    except FileNotFoundError:
        click.echo(
            f"forge runbook run: runbook file could not be found: {path}",
            err=True,
        )
        sys.exit(1)
    except json.JSONDecodeError as exc:
        click.echo(
            f"forge runbook run: runbook file is invalid (JSON decode error): {exc}",
            err=True,
        )
        sys.exit(1)
    except (KeyError, TypeError, ValueError) as exc:
        click.echo(
            f"forge runbook run: runbook file is invalid: {exc}",
            err=True,
        )
        sys.exit(1)

    # 2. Resolve database path
    if db_path is None:
        db_path = Path.home() / ".forge" / "forge.db"

    # 3. Build dependencies
    try:
        repository = _build_repository(db_path)
    except Exception as exc:
        click.echo(
            f"forge runbook run: failed to connect to database: {exc}",
            err=True,
        )
        sys.exit(1)

    # Build registry with real shell handlers (TASK-FMDR-002 AC-001)
    registry = StepTypeRegistry()
    register_shell_handlers(registry)

    # 4. Persist the runbook BEFORE execution (ASSUM-007)
    try:
        repository.create_runbook(
            runbook,
            correlation_id=f"cli-run-{runbook.runbook_id}",
        )
    except RunbookDuplicateError:
        click.echo(
            f"forge runbook run: runbook {runbook.runbook_id!r} already exists; "
            "cannot create duplicate",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            f"forge runbook run: failed to persist runbook: {exc}",
            err=True,
        )
        sys.exit(1)

    # 5. Execute the runbook with NATS lifecycle management
    try:
        result = asyncio.run(
            _run_with_nats(
                repository,
                registry,
                runbook.runbook_id,
                no_events,
            )
        )
    except Exception as exc:
        click.echo(
            f"forge runbook run: execution failed: {exc}",
            err=True,
        )
        sys.exit(1)

    # 6. Report the outcome
    if result.status == "complete":
        click.echo(f"Runbook {runbook.runbook_id} completed successfully")
    elif result.status == "already_complete":
        click.echo(f"Runbook {runbook.runbook_id} was already complete")
    elif result.status == "escalated":
        click.echo(
            f"Runbook {runbook.runbook_id} escalated at step {result.stopped_at_index}: "
            f"{result.reason}"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# NATS connection helpers (TASK-FMDR-002)
# ---------------------------------------------------------------------------


async def _run_with_nats(
    repository: RunbookRepository,
    registry: StepTypeRegistry,
    runbook_id: str,
    no_events: bool,
) -> Any:
    """Run the runbook with NATS lifecycle management.

    Connects to NATS (if events enabled), executes the runbook, and ensures
    the NATS connection is properly closed afterward.

    Args:
        repository: Runbook persistence repository.
        registry: Step type registry with registered handlers.
        runbook_id: ID of the runbook to execute.
        no_events: Whether to skip NATS event publishing.

    Returns:
        RunResult from the executor.
    """
    # Connect to NATS or use no-op client
    if no_events:
        nats_client = _NoOpNATSClient()
    else:
        nats_client = await _connect_nats_best_effort()

    try:
        # Build publisher and executor
        publisher = RunbookPublisher(nats_client=nats_client)
        executor = _build_executor(repository, registry, publisher)

        # Execute the runbook
        result = await executor.run(
            runbook_id,
            correlation_id=f"cli-run-{runbook_id}",
        )
        return result
    finally:
        # Clean up NATS connection if it's a real client
        if not isinstance(nats_client, _NoOpNATSClient):
            try:
                await nats_client.close()
            except Exception as exc:
                logger.debug("Failed to close NATS connection: %s", exc)


#: Default NATS broker URL when ``FORGE_NATS_URL`` is unset. Matches the
#: project-wide default used by :data:`forge.cli._serve_config.DEFAULT_NATS_URL`.
_DEFAULT_NATS_URL = "nats://127.0.0.1:4222"


async def _default_nats_connect(servers: str, **options: Any) -> Any:
    """Production NATS connect — opens a fresh client (TASK-FMDR-008).

    The ``nats`` import is lazy so importing this module (and ``forge --help``)
    does not pull the network stack in, and so ``nats-py`` stays an optional
    dependency. Mirrors the seam pattern in
    :data:`forge.cli._serve_daemon.nats_connect`.

    Args:
        servers: NATS broker URL (or comma-separated list).
        **options: Forwarded verbatim to ``nats.connect`` — including any
            auth kwargs resolved by :func:`_resolve_nats_auth` and the
            fail-fast knobs (``allow_reconnect``, ``max_reconnect_attempts``).

    Returns:
        A connected ``nats.NATS`` client.

    Raises:
        ImportError: When ``nats-py`` is not installed.
    """
    import nats  # type: ignore[import-not-found]

    return await nats.connect(servers=servers, **options)


#: Seam for tests (TASK-FMDR-008). Replace ``forge.cli.runbook.nats_connect``
#: to simulate an auth-rejecting or unreachable broker without a live NATS
#: server. Mirrors :data:`forge.cli._serve_daemon.nats_connect`.
nats_connect = _default_nats_connect


def _resolve_nats_auth(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resolve NATS auth connect-kwargs from ``FORGE_*`` environment variables.

    Lets ``forge runbook run`` authenticate to an auth-required broker
    (TASK-FMDR-008 AC-1) following the project-wide ``FORGE_*`` convention
    (cf. :meth:`forge.cli._serve_config.ServeConfig.from_env`).

    Precedence — first match wins, most-capable first:

    1. ``FORGE_NATS_CREDS``  → ``{"user_credentials": <path>}`` (an NSC
       ``.creds`` file; nats-py reads JWT + nkey seed from it).
    2. ``FORGE_NATS_TOKEN``  → ``{"token": <token>}``.
    3. ``FORGE_NATS_USER`` + ``FORGE_NATS_PASSWORD`` → ``{"user": ...,
       "password": ...}`` (both required; a lone user or password is ignored).

    The secret values are placed straight into the returned kwargs and are
    **never** logged here — the caller passes them to ``nats.connect`` and
    logs only the scrubbed/sanitised server URL (see
    :func:`_connect_nats_best_effort`).

    Args:
        environ: Optional mapping to read instead of :data:`os.environ`
            (tests inject a controlled dict rather than mutate the process).

    Returns:
        A kwargs dict for ``nats.connect``. Empty when no credentials are
        configured (anonymous connect — the historical behaviour).
    """
    env = environ if environ is not None else os.environ

    # ``.strip()`` so a trailing newline (common when a value is sourced from
    # a file) does not corrupt the credential, and a whitespace-only value is
    # treated as unset rather than a bad credential.
    creds = (env.get("FORGE_NATS_CREDS") or "").strip()
    if creds:
        return {"user_credentials": creds}

    token = (env.get("FORGE_NATS_TOKEN") or "").strip()
    if token:
        return {"token": token}

    user = (env.get("FORGE_NATS_USER") or "").strip()
    password = (env.get("FORGE_NATS_PASSWORD") or "").strip()
    if user and password:
        return {"user": user, "password": password}

    return {}


#: Fixed marker substituted for a known secret value before logging.
_LOG_SECRET_MARKER = "***REDACTED***"


def _sanitise_servers(servers: str) -> tuple[str, list[str]]:
    """Split inline credentials out of a NATS server URL (or comma list).

    ``FORGE_NATS_URL`` may legitimately carry inline credentials
    (``nats://user:pass@host:4222``, or even a scheme-less
    ``user:pass@host:4222``). The :mod:`forge.memory.redaction` scrubbers do
    not recognise ``nats://`` authority sections, so we strip the userinfo
    structurally here (TASK-FMDR-008 AC-1: never log the secret).

    Args:
        servers: A NATS URL, or comma-separated list of URLs.

    Returns:
        ``(display, inline_secrets)`` where ``display`` has each URL's
        ``user:pass@`` userinfo replaced by ``host:port`` (safe to log) and
        ``inline_secrets`` lists the userinfo substrings (and their password
        component) so the caller can also redact them from free-form error
        text. Tokens that do not parse as URLs are passed through unchanged.
    """
    displays: list[str] = []
    secrets: list[str] = []

    def _record_userinfo(userinfo: str) -> None:
        secrets.append(userinfo)
        if ":" in userinfo:
            # The password component alone, for redacting error strings that
            # echo only the password rather than the whole ``user:pass``.
            secrets.append(userinfo.split(":", 1)[1])

    for raw in servers.split(","):
        token = raw.strip()
        parts = urlsplit(token)
        if parts.netloc and "@" in parts.netloc:
            userinfo, _, host = parts.netloc.rpartition("@")
            _record_userinfo(userinfo)
            token = urlunsplit(
                (parts.scheme, host, parts.path, parts.query, parts.fragment)
            )
        elif not parts.netloc and "://" not in token and "@" in token:
            # Scheme-less ``user:pass@host:port`` — urlsplit leaves it in the
            # path, so handle it explicitly rather than leaking the userinfo.
            userinfo, _, host = token.rpartition("@")
            _record_userinfo(userinfo)
            token = host
        displays.append(token)

    return ",".join(displays), secrets


def _safe_server_display(servers: str) -> str:
    """Return ``servers`` with any inline ``user:pass@`` userinfo stripped."""
    return _sanitise_servers(servers)[0]


def _scrub_for_log(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Scrub credentials from ``text`` before logging (TASK-FMDR-008 AC-1).

    Two layers:

    1. **Known-value redaction** — any literal secret the caller resolved
       (token / password / inline-URL userinfo) is replaced with
       :data:`_LOG_SECRET_MARKER`. This is a *deterministic* guarantee that
       the operator's secret never reaches the log, independent of the
       secret's shape — the existing scrubbers only match credential
       *patterns* (e.g. ``password=``, DSNs, GitHub/bearer tokens) and would
       not catch a short opaque NATS token.
    2. **Shape redaction** — the result is routed through the existing
       :mod:`forge.memory.redaction` scrubbers to catch credential shapes a
       broker error string might carry that the caller does not know about.

    Pure: no I/O; idempotent for a fixed ``secrets`` set.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, _LOG_SECRET_MARKER)
    return scrub_process_output(redact_credentials(text))


async def _connect_nats_best_effort(
    environ: Mapping[str, str] | None = None,
) -> _NoOpNATSClient | Any:
    """Connect to NATS broker with best-effort, fail-fast semantics.

    Attempts to connect to the broker at ``FORGE_NATS_URL`` (default
    :data:`_DEFAULT_NATS_URL`), authenticating with any operator-supplied
    credentials resolved by :func:`_resolve_nats_auth`. If the connection
    fails for *any* reason — unreachable broker, ``nats-py`` not installed, or
    an ``Authorization Violation`` from an auth-required broker — it falls back
    to :class:`_NoOpNATSClient` so the runbook still executes its steps.

    TASK-FMDR-008:

    - **AC-1** — auth kwargs (token / user+password / ``.creds`` file) are
      passed to ``nats.connect``; the server URL is sanitised + scrubbed
      before any log line, so the secret is never logged.
    - **AC-2** — ``allow_reconnect=False`` makes an auth-rejecting broker
      raise immediately instead of spinning in a reconnect loop; the single
      raise is caught here and converted to the NoOp fallback. (The earlier
      ``max_reconnect_attempts=0`` alone did *not* prevent the spin.)

    Args:
        environ: Optional mapping to read instead of :data:`os.environ`.

    Returns:
        A connected NATS client, or :class:`_NoOpNATSClient` if the connect
        fails for any reason.
    """
    env = environ if environ is not None else os.environ
    servers = env.get("FORGE_NATS_URL", _DEFAULT_NATS_URL)
    auth_kwargs = _resolve_nats_auth(env)

    # Collect every concrete secret value so it can be redacted from any log
    # line deterministically (TASK-FMDR-008 AC-1), regardless of its shape.
    safe_servers, secrets = _sanitise_servers(servers)
    for key in ("token", "password"):
        if key in auth_kwargs:
            secrets.append(auth_kwargs[key])
    safe_display = _scrub_for_log(safe_servers, secrets)

    try:
        client = await nats_connect(
            servers,
            connect_timeout=2,  # Fail fast if broker unavailable.
            # AC-2: disable reconnect entirely so an Authorization Violation
            # raises on the first attempt instead of spinning. Keeping
            # max_reconnect_attempts=0 too is belt-and-suspenders.
            allow_reconnect=False,
            max_reconnect_attempts=0,
            **auth_kwargs,
        )
    except ImportError:
        logger.warning(
            "nats-py not installed; lifecycle events will not be published. "
            "Install with: pip install nats-py"
        )
        return _NoOpNATSClient()
    except Exception as exc:
        logger.warning(
            "Failed to connect to NATS broker at %s: %s. "
            "Runbook will execute but lifecycle events will not be published.",
            safe_display,
            _scrub_for_log(str(exc), secrets),
        )
        return _NoOpNATSClient()

    logger.info("Connected to NATS broker at %s", safe_display)
    return client


# ---------------------------------------------------------------------------
# No-op NATS client for CLI (events not published from CLI)
# ---------------------------------------------------------------------------


class _NoOpNATSClient:
    """No-op stand-in for NATS client when events aren't published from CLI.

    The CLI runs runbooks synchronously and doesn't need to publish lifecycle
    events. This no-op client satisfies the RunbookPublisher interface without
    requiring a live NATS connection.
    """

    async def publish(self, subject: str, payload: bytes) -> None:
        """No-op publish method."""


__all__ = ["nats_connect", "runbook_cmd"]
