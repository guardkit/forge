"""``ServeConfig`` model and Integration Contract B/C constants (TASK-F009-001).

This module is the **declarative producer** for two integration contracts
shared across FEAT-FORGE-009:

- **Contract B** — ``DEFAULT_HEALTHZ_PORT = 8080`` is the port the
  Wave-2 healthz HTTP server (TASK-F009-004) binds to and the value the
  Dockerfile (TASK-F009-005) ``EXPOSE``s.
- **Contract C** — ``DEFAULT_DURABLE_NAME = "forge-serve"`` is the
  JetStream durable name the Wave-2 daemon (TASK-F009-003) uses when it
  creates its consumer; it must be stable across restarts so the broker
  can replay un-acked messages to the same logical subscriber.

Both constants are re-exported from :mod:`forge.cli.serve` so callers can
``from forge.cli.serve import DEFAULT_HEALTHZ_PORT`` (the canonical
import path documented in the acceptance criteria).

The :class:`ServeConfig` Pydantic v2 model captures every knob the daemon
needs at boot. Defaults are anchored to the contract constants above so
running ``forge serve`` with zero flags and zero env vars still produces
a healthy default configuration. Environment-variable overrides follow
the ``FORGE_*`` convention used elsewhere in the codebase
(``FORGE_NATS_URL``, ``FORGE_HEALTHZ_PORT``, ``FORGE_LOG_LEVEL``,
``FORGE_DB_PATH``); they are resolved at construction time via the
``from_env`` classmethod so callers do not have to plumb ``os.environ``
reads through their own code.

TASK-FIX-F010 — adds the ``db_path`` field plus ``FORGE_DB_PATH``
environment override so :func:`forge.cli._serve_production.bind_production_serve`
can resolve the SQLite writer path without a parallel knob. The default
``~/.forge/forge.db`` matches the operator-friendly location already
used by :mod:`forge.cli.queue` and :mod:`forge.cli.status`; no new env
var name is introduced.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Integration Contract constants (B and C)
# ---------------------------------------------------------------------------

#: Contract B — the port the healthz HTTP server binds to. The Dockerfile
#: ``EXPOSE`` directive and the Kubernetes readiness probe both anchor on
#: this value; do not change it without coordinating across F009.
DEFAULT_HEALTHZ_PORT: int = 8080

#: Contract C — the JetStream durable name used by the daemon's consumer.
#: Must remain stable across restarts so un-acked messages replay to the
#: same logical subscriber.
DEFAULT_DURABLE_NAME: str = "forge-serve"

#: Default NATS broker URL for local development. Production deployments
#: override this via ``FORGE_NATS_URL``.
DEFAULT_NATS_URL: str = "nats://127.0.0.1:4222"

#: Default log level — ``info`` matches the ``logging`` package's
#: lower-case INFO level name.
DEFAULT_LOG_LEVEL: str = "info"

#: Default SQLite writer path for the daemon's lifecycle persistence.
#: Matches :mod:`forge.cli.queue`'s ``DEFAULT_DB_PATH`` and the operator-
#: friendly local default referenced by ADR-ARCH-001. Production
#: deployments that mount writable storage at a non-home path override
#: this via ``FORGE_DB_PATH`` (the same env var honoured by
#: :mod:`forge.cli.status` / :mod:`forge.cli.queue`).
DEFAULT_DB_PATH: Path = Path("~/.forge/forge.db")


class ServeConfig(BaseModel):
    """Pydantic v2 settings model for the ``forge serve`` daemon.

    All fields ship with sensible defaults so ``ServeConfig()`` works.
    Construction-time environment-variable overrides are handled by
    :meth:`from_env`.

    Attributes:
        nats_url: NATS broker URL (Contract A consumer side).
        healthz_port: Port the healthz HTTP server binds to (Contract B).
        durable_name: JetStream durable name (Contract C).
        log_level: Lower-case log level name passed to ``logging``.
        db_path: Filesystem path to the SQLite lifecycle database
            (TASK-FIX-F010). Defaults to ``~/.forge/forge.db`` (the
            ``~`` is expanded eagerly by the default factory so tests
            see a deterministic absolute path at instantiation time).
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    nats_url: str = Field(default=DEFAULT_NATS_URL)
    healthz_port: int = Field(default=DEFAULT_HEALTHZ_PORT, ge=1, le=65535)
    durable_name: str = Field(default=DEFAULT_DURABLE_NAME, min_length=1)
    log_level: str = Field(default=DEFAULT_LOG_LEVEL, min_length=1)
    # ``default_factory`` keeps the expanduser() call lazy so the value
    # is resolved at instantiation time (deterministic for tests that
    # monkeypatch ``$HOME``) rather than at module import.
    db_path: Path = Field(
        default_factory=lambda: DEFAULT_DB_PATH.expanduser()
    )

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "ServeConfig":
        """Construct a :class:`ServeConfig` honouring ``FORGE_*`` env vars.

        Recognised variables (all optional):

        - ``FORGE_NATS_URL``    → ``nats_url``
        - ``FORGE_HEALTHZ_PORT`` → ``healthz_port`` (parsed as ``int``)
        - ``FORGE_DURABLE_NAME`` → ``durable_name``
        - ``FORGE_LOG_LEVEL``    → ``log_level``
        - ``FORGE_DB_PATH``      → ``db_path`` (``~`` is expanded;
          TASK-FIX-F010)

        Args:
            environ: Optional mapping to read from instead of
                :data:`os.environ`. Tests inject a controlled dict here
                so they do not have to mutate process-wide state.

        Returns:
            A fully validated :class:`ServeConfig`.
        """
        env = environ if environ is not None else os.environ
        kwargs: dict[str, object] = {}
        if "FORGE_NATS_URL" in env:
            kwargs["nats_url"] = env["FORGE_NATS_URL"]
        if "FORGE_HEALTHZ_PORT" in env:
            kwargs["healthz_port"] = int(env["FORGE_HEALTHZ_PORT"])
        if "FORGE_DURABLE_NAME" in env:
            kwargs["durable_name"] = env["FORGE_DURABLE_NAME"]
        if "FORGE_LOG_LEVEL" in env:
            kwargs["log_level"] = env["FORGE_LOG_LEVEL"]
        if "FORGE_DB_PATH" in env:
            # Reuse the project-wide convention (forge queue / forge
            # status both read FORGE_DB_PATH); never introduce a parallel
            # FORGE_SQLITE_PATH (TASK-REV-F010 D2.A*).
            kwargs["db_path"] = Path(env["FORGE_DB_PATH"]).expanduser()
        return cls(**kwargs)


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_DURABLE_NAME",
    "DEFAULT_HEALTHZ_PORT",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_NATS_URL",
    "ServeConfig",
]
