"""Fleet-memory-backed :class:`forge.gating.wrappers.PriorsReader`.

This module is the production successor to the retired Graphiti memory
tiers (``forge.memory``, retired 2026-08-03): the gate's priors read now
runs against the fleet-memory Postgres store via the same retrieval
surface the ``memory_search`` MCP tool wraps
(:func:`fleet_memory.retrieval.search` over a typed
:class:`~fleet_memory.retrieval.SearchRequest`) — single source of
truth, no drift. Per-item results are mapped straight to
:class:`~forge.gating.models.PriorReference`; the reader deliberately
does **not** call ``assemble_context``, because per-prior identity
(``natural_key`` + score) is the point of a priors read and assembly
collapses it into one synthetic block.

Constraints this module carries
-------------------------------

* **Loop affinity.** The fleet-memory store is an
  ``AsyncPostgresStore`` over psycopg3 + psycopg-pool
  (``AsyncConnectionPool``), and the pool is bound to the event loop
  that opens it. The reader lazily opens the store on the daemon's
  single serve loop and every subsequent gate read must run on that
  same loop. This holds today because ``gate_check`` runs in-process on
  the daemon loop; it BREAKS if gate_check ever moves to a worker
  thread — at that point mirror guardkit's per-thread client factory
  (``FleetMemoryClientFactory``) instead of sharing this reader.
* **Project scoping — the revisit trigger.** Reads are scoped to
  ``FORGE_MEMORY_PROJECT`` (default ``"guardkit"``), matching where the
  factory's build outcomes land today. THE trigger: when per-repo write
  scoping (``GUARDKIT_MEMORY_PROJECT`` set at dispatch) starts landing
  outcome rows OUTSIDE the ``guardkit`` project, revisit
  ``FORGE_MEMORY_PROJECT`` — a correct per-repo read needs the build's
  repo threaded into ``gate_check``, which it is not today.
* **Auto-approve bound.** Priors are *evidence*, never a mode input:
  today the gate's reasoning callable is
  :func:`forge.gating.degraded.degraded_dispatch_gate_model`, which
  mandates human approval unconditionally, so retrieved priors can only
  ever widen the card's evidence — they cannot flip a gate to
  ``AUTO_APPROVE``. That property is a consequence of the degraded
  composition, NOT of this reader; re-check it the day a real reasoning
  model is wired into ``GateCheckDeps.reasoning_model_call``.
* **Activation.** Rich's ruling 3 (memory plan, 2026-08-03): the forge
  memory wire goes live only AFTER the GROI receipt — that receipt is
  the unblock for setting ``FLEET_MEMORY_ENABLED=true`` in the daemon's
  environment. Until then the factory composes an
  :class:`~forge.gating.degraded.EmptyPriorsReader` and says so loudly.

Settings hygiene: the fleet-memory ``Settings`` is pydantic-settings
with ``env_file=".env"`` — a bare ``Settings()`` would silently read
whatever ``.env`` sits in the daemon's CWD, and ``forge/.env`` exists.
The reader therefore constructs ``Settings`` with explicit kwargs from
its own config (plus ``_env_file=None``), making that leak impossible
by construction.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from forge.gating.models import GateTargetKind, PriorReference
from forge.memory import redact_credentials

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge.gating.wrappers import PriorsReader

logger = logging.getLogger(__name__)

#: ``group_id`` stamped on every mapped :class:`PriorReference`.
#: ``PriorReference.group_id`` is a two-value ``Literal`` with
#: ``extra="forbid"`` (DM-gating §6), so any unstamped value raises a
#: ``ValidationError`` on every hit. This is a legacy-shaped label kept
#: for schema compatibility — the fleet-memory ``natural_key`` carried
#: verbatim in ``entity_id`` is the real identity.
PRIORS_GROUP_ID: str = "forge_pipeline_history"

#: Fleet-memory payload types a gate read consults. Build outcomes are
#: the factory's own history; warnings are the cross-project cautions.
_PAYLOAD_TYPES: tuple[str, ...] = ("build_outcome", "warning")

#: Token budget threaded into ``SearchRequest`` (matches the guardkit
#: adapter's floor — the budget only shapes ranking depth here, not
#: assembly, because this reader never assembles).
_TOKEN_BUDGET: int = 2000

#: At most this many priors ride one gate decision.
_MAX_PRIORS: int = 5

#: Bounded head of ``lessons``/``content`` carried as the prior summary.
_SUMMARY_HEAD_CHARS: int = 500


@dataclass(frozen=True, slots=True)
class FleetMemoryPriorsConfig:
    """Environment-shaped configuration for the priors reader.

    Attributes:
        enabled: Whether fleet-memory priors reads are enabled.
        pg_dsn: PostgreSQL DSN of the fleet store. No code default —
            an enabled-but-DSN-less run is refused outright by
            :func:`load_priors_config_from_env` (never a localhost
            fallback).
        embed_url: Embedding service URL (embed-on-read).
        embed_model: Embedding model identifier. Default matches the
            live deployment (Qwen3-Embedding served as ``embed``).
        embed_dims: Embedding vector dimensions. Default matches the
            live 1024-dim corpus — a wrong value silently mis-embeds
            the query against the rebuilt corpus.
        project: Fleet-memory project namespace the gate reads. See the
            module docstring's project-scoping revisit trigger.
        read_timeout_s: Deadline for one whole gate read, lazy connect
            included.
    """

    enabled: bool = False
    pg_dsn: str = ""
    embed_url: str = ""
    embed_model: str = "embed"
    embed_dims: int = 1024
    project: str = "guardkit"
    read_timeout_s: float = 10.0


def _int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back loudly on garbage."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "memory: %s=%r is not an integer — using default %d", name, raw, default
        )
        return default


def _float_env(name: str, default: float) -> float:
    """Parse a float env var, falling back loudly on garbage."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "memory: %s=%r is not a number — using default %s", name, raw, default
        )
        return default


def load_priors_config_from_env() -> FleetMemoryPriorsConfig:
    """Build a :class:`FleetMemoryPriorsConfig` from ``os.environ``.

    Reads the environment directly (no pydantic-settings, no ``.env``
    file — see the module docstring's Settings-hygiene note).

    The DSN trap, refused outright: ``FLEET_MEMORY_ENABLED=true`` with
    ``FLEET_MEMORY_PG_DSN`` unset/blank forces ``enabled=False`` with an
    ERROR. There is deliberately NO localhost default — on this estate
    the historical ``localhost:5433`` code default is a TEST Postgres,
    and an enabled-but-DSN-less run degrades into empty reads that look
    exactly like memory working with nothing to say (2026-08-03 audit).
    """
    enabled = os.environ.get("FLEET_MEMORY_ENABLED", "").strip().lower() == "true"
    pg_dsn = os.environ.get("FLEET_MEMORY_PG_DSN", "").strip()
    if enabled and not pg_dsn:
        logger.error(
            "memory: FLEET_MEMORY_ENABLED is true but FLEET_MEMORY_PG_DSN is "
            "unset/blank — REFUSING to enable priors reads (no localhost "
            "fallback; the localhost:5433 code default is a TEST Postgres and "
            "an enabled-but-DSN-less run looks exactly like memory with "
            "nothing to say). Set FLEET_MEMORY_PG_DSN."
        )
        enabled = False
    embed_url_env = os.environ.get("FLEET_MEMORY_EMBED_URL", "").strip()
    if enabled and not embed_url_env:
        logger.error(
            "memory: FLEET_MEMORY_ENABLED is true but FLEET_MEMORY_EMBED_URL "
            "is unset/blank — REFUSING to enable priors reads (fleet-memory "
            "Settings rejects an empty embed_url, so every gate read would "
            "fail after a 'memory: ON' boot line — the misconfiguration is "
            "named ONCE here instead of per read). Set FLEET_MEMORY_EMBED_URL."
        )
        enabled = False
    return FleetMemoryPriorsConfig(
        enabled=enabled,
        pg_dsn=pg_dsn,
        embed_url=embed_url_env,
        embed_model=os.environ.get("FLEET_MEMORY_EMBED_MODEL", "embed").strip()
        or "embed",
        embed_dims=_int_env("FLEET_MEMORY_EMBED_DIMS", 1024),
        project=os.environ.get("FORGE_MEMORY_PROJECT", "guardkit").strip()
        or "guardkit",
        read_timeout_s=_float_env("FORGE_MEMORY_READ_TIMEOUT_S", 10.0),
    )


@dataclass(frozen=True, slots=True)
class _RetrievalBackend:
    """Late-bound fleet_memory surfaces (imported only on the ON path).

    Bundling the four names keeps the import in exactly one place and
    gives tests a single seam to substitute — the unit tier runs
    without ``fleet_memory`` installed.
    """

    settings_cls: type[Any]
    store_context: Callable[..., Any]
    request_cls: type[Any]
    search: Callable[..., Any]


def _load_backend() -> _RetrievalBackend:
    """Import the fleet_memory retrieval surface (the forge ``memory`` extra)."""
    from fleet_memory.retrieval import SearchRequest
    from fleet_memory.retrieval import search as fm_search
    from fleet_memory.settings import Settings
    from fleet_memory.store import async_store_context

    return _RetrievalBackend(
        settings_cls=Settings,
        store_context=async_store_context,
        request_cls=SearchRequest,
        search=fm_search,
    )


def _to_prior_reference(item: Any) -> PriorReference | None:
    """Map one fleet-memory search item to a :class:`PriorReference`.

    Returns ``None`` for an item without a usable ``natural_key``
    (``PriorReference.entity_id`` requires a non-empty string) so one
    malformed row never poisons the whole read.
    """
    value = getattr(item, "value", None)
    if not isinstance(value, dict):
        return None
    natural_key = value.get("natural_key")
    if not isinstance(natural_key, str) or not natural_key:
        return None
    raw_summary: Any = value.get("lessons")
    if isinstance(raw_summary, list):
        raw_summary = " ".join(str(part) for part in raw_summary)
    if not isinstance(raw_summary, str) or not raw_summary:
        raw_summary = value.get("content")
    if not isinstance(raw_summary, str):
        raw_summary = ""
    score = float(getattr(item, "score", None) or 0.0)
    return PriorReference(
        entity_id=natural_key,
        group_id=PRIORS_GROUP_ID,  # type: ignore[arg-type]
        summary=redact_credentials(raw_summary[:_SUMMARY_HEAD_CHARS]),
        relevance_score=min(max(score, 0.0), 1.0),
    )


class FleetMemoryPriorsReader:
    """Fleet-memory implementation of the ``PriorsReader`` protocol.

    The store connection is opened lazily on the first gate read (under
    an :class:`asyncio.Lock` — two gate sites can race the first read)
    and reused for the daemon's lifetime; ``aclose`` releases it at
    shutdown. Every read — lazy connect included — runs inside a
    ``read_timeout_s`` deadline, and every failure mode degrades to an
    empty priors list with an ERROR naming the build: a memory outage
    slows a gate by at most one deadline, it never blocks or fails one.

    Args:
        config: Validated reader configuration (``enabled`` is the
            factory's concern; the reader assumes it is only
            constructed on the ON path).
        backend: Optional pre-imported :class:`_RetrievalBackend`.
            The factory passes the one it import-probed; tests pass a
            stub so the unit tier runs without ``fleet_memory``.
    """

    def __init__(
        self,
        config: FleetMemoryPriorsConfig,
        *,
        backend: _RetrievalBackend | None = None,
    ) -> None:
        self._config = config
        self._backend = backend
        self._store: Any = None
        self._store_cm: Any = None
        self._init_lock = asyncio.Lock()

    async def read_priors(
        self,
        *,
        target_kind: GateTargetKind,
        target_identifier: str,
        stage_label: str,
        build_id: str,
    ) -> list[PriorReference]:
        """Read up to five priors for one gated stage; never raises.

        The whole body (lazy store open included) runs under the
        configured deadline. Timeout/cancellation and any other failure
        discard the store handle — the next gate read reconnects fresh —
        and return ``[]`` so the gate proceeds without priors.
        """
        try:
            return await asyncio.wait_for(
                self._read(
                    target_kind=target_kind,
                    target_identifier=target_identifier,
                    stage_label=stage_label,
                ),
                timeout=self._config.read_timeout_s,
            )
        except (TimeoutError, asyncio.CancelledError):
            await self._discard_store()
            logger.error(
                "fleet-memory priors: read for build_id=%s timed out after "
                "%ss (embed cold-start likely) — store reset, will reconnect; "
                "gate proceeds WITHOUT priors",
                build_id,
                self._config.read_timeout_s,
            )
            return []
        except Exception as exc:  # noqa: BLE001 — gate reads never raise
            await self._discard_store()
            logger.error(
                "fleet-memory priors: read for build_id=%s failed (%s) — "
                "store discarded; gate proceeds WITHOUT priors",
                build_id,
                exc,
            )
            return []

    async def aclose(self) -> None:
        """Release the store connection (idempotent; daemon shutdown)."""
        await self._discard_store()

    async def _read(
        self,
        *,
        target_kind: GateTargetKind,
        target_identifier: str,
        stage_label: str,
    ) -> list[PriorReference]:
        async with self._init_lock:
            store = await self._open_store()
        backend = self._backend
        assert backend is not None  # _open_store resolved it
        request = backend.request_cls(
            project=self._config.project,
            query=f"{target_kind} {target_identifier} {stage_label} outcome",
            payload_types=list(_PAYLOAD_TYPES),
            token_budget=_TOKEN_BUDGET,
            include_superseded=False,
        )
        results = await backend.search(request, store)
        if not results:
            # Empty is NORMAL — a young corpus, not a failure. INFO, not
            # debug: a gate-time read must be visible in the daemon log at
            # the production level (the ruling-3 receipt bar), and silence
            # here is indistinguishable from the read never running.
            logger.info(
                "fleet-memory priors: 0 priors matched for %s %s at %s",
                target_kind,
                target_identifier,
                stage_label,
            )
            return []
        priors: list[PriorReference] = []
        for item in results:
            if len(priors) >= _MAX_PRIORS:
                break
            prior = _to_prior_reference(item)
            if prior is not None:
                priors.append(prior)
        logger.info(
            "fleet-memory priors: %d prior(s) matched for %s %s at %s",
            len(priors),
            target_kind,
            target_identifier,
            stage_label,
        )
        return priors

    async def _open_store(self) -> Any:
        """Enter the fleet-memory store context once (caller holds the lock).

        A failure — cancellation mid-open included — is a construction
        failure: both handles stay ``None`` so the next read starts a
        fresh open; there is never a half-open store to reuse.
        """
        if self._store is not None:
            return self._store
        if self._backend is None:
            self._backend = _load_backend()
        # Explicit kwargs ONLY, plus _env_file=None: a bare Settings()
        # would read any CWD .env via pydantic-settings (forge/.env
        # exists) — this construction makes that impossible.
        settings = self._backend.settings_cls(
            _env_file=None,
            pg_dsn=self._config.pg_dsn,
            embed_url=self._config.embed_url,
            embed_model=self._config.embed_model,
            embed_dims=self._config.embed_dims,
        )
        store_cm = self._backend.store_context(settings)
        self._store_cm = store_cm
        try:
            self._store = await store_cm.__aenter__()
        except BaseException:
            self._store = None
            self._store_cm = None
            raise
        return self._store

    async def _discard_store(self) -> None:
        """Close and forget the store handles, swallowing secondary errors."""
        store_cm = self._store_cm
        self._store = None
        self._store_cm = None
        if store_cm is None:
            return
        try:
            await store_cm.__aexit__(None, None, None)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            pass


def build_priors_reader_from_env(
    *, config: FleetMemoryPriorsConfig | None = None
) -> "PriorsReader":
    """Compose the daemon's priors reader from the environment; never raises.

    Three loud outcomes (the 2026-08-03 reconnection lesson — a run
    without memory is acceptable, a run that hides it is not):

    * OFF → :class:`~forge.gating.degraded.EmptyPriorsReader` + WARNING.
    * ON but ``fleet_memory`` not importable (the forge ``memory``
      extra) → :class:`EmptyPriorsReader` + ERROR (DEGRADED).
    * ON → :class:`FleetMemoryPriorsReader` + INFO naming project and
      deadline.

    ``fleet_memory`` is imported ONLY on the ON path, so the default
    OFF composition never requires the extra to be installed.

    Args:
        config: Optional pre-built config; loaded from ``os.environ``
            when ``None``.
    """
    from forge.gating.degraded import EmptyPriorsReader

    if config is None:
        config = load_priors_config_from_env()
    if not config.enabled:
        logger.warning(
            "memory: OFF — FLEET_MEMORY_ENABLED unset/false; gates read no "
            "priors. Set FLEET_MEMORY_ENABLED=true and FLEET_MEMORY_PG_DSN "
            "to enable."
        )
        return EmptyPriorsReader()
    try:
        backend = _load_backend()
    except Exception as exc:  # noqa: BLE001 — the factory never raises
        logger.error(
            "memory: DEGRADED — requested ON but fleet_memory is not "
            "installed (forge `memory` extra): %s; gates run without priors",
            exc,
        )
        return EmptyPriorsReader()
    logger.info(
        "memory: ON (project=%s) — priors read at gate time, %ss deadline",
        config.project,
        config.read_timeout_s,
    )
    return FleetMemoryPriorsReader(config, backend=backend)


__all__ = [
    "PRIORS_GROUP_ID",
    "FleetMemoryPriorsConfig",
    "FleetMemoryPriorsReader",
    "build_priors_reader_from_env",
    "load_priors_config_from_env",
]
