"""``forge queue`` — write-then-publish enqueue command (TASK-PSM-008).

Behavioural contract (``API-cli.md §3.3``)
------------------------------------------

The ``forge queue`` command implements the **write-then-publish**
discipline (concern ``sc_002`` from TASK-REV-3EEE). The flow is, in order:

1. **Validate ``feature_id``** through
   :func:`forge.lifecycle.identifiers.validate_feature_id` (concern
   ``sc_003``). On :class:`InvalidIdentifierError` we exit ``4`` *before*
   any SQLite write or NATS publish runs — no side effects.
2. **Allowlist check** — refuse with exit ``2`` when ``--repo`` is not in
   ``ForgeConfig.queue.repo_allowlist`` (Group C "path-allowlist
   refused"). An empty allowlist (the schema default) means "no
   restriction" per :class:`forge.config.models.QueueConfig`.
3. **Merge defaults** — CLI flags override ``ForgeConfig.queue.default_*``.
4. **Build the wire payload** —
   :class:`nats_core.events.BuildQueuedPayload` with
   ``triggered_by="cli"`` and ``originating_user`` resolved from
   :func:`os.getlogin` (with a tolerant fallback for unattended
   environments where ``getlogin()`` raises :class:`OSError`).
5. **Active in-flight check** — short-circuit with exit ``3`` when
   :meth:`SqliteLifecyclePersistence.exists_active_build` returns
   ``True`` (Group C "active in-flight duplicate").
6. **Write the SQLite row first** via
   :meth:`SqliteLifecyclePersistence.record_pending_build`. A
   :class:`DuplicateBuildError` raised by the unique
   ``(feature_id, correlation_id)`` index translates to exit ``3``
   (Group B "duplicate refused").
7. **Then publish** the
   :class:`nats_core.envelope.MessageEnvelope` to subject
   ``pipeline.build-queued.{feature_id}``. The publisher seam is
   :func:`publish` — production wires it to a real NATS connection;
   tests monkey-patch it.
8. **On publish failure** — *do not* roll back the SQLite row. Print a
   diagnostic on stderr that mentions both ``"publish failed"`` and the
   ``"messaging-layer"`` cause (Group H), then exit ``1``. Pipeline
   truth lives in SQLite; the JetStream stream is a derived projection
   and the on-boot reconciler (TASK-PSM-007) redrives any orphaned
   rows.

Test seams
----------

Two module-level callables make the side-effect-bearing steps mockable
without a NATS broker or a real ``forge.db`` file:

- :func:`make_persistence` — returns a
  :class:`SqliteLifecyclePersistence` (or any duck-typed equivalent).
- :func:`publish` — synchronous wrapper around the async NATS publish.

Both seams default to production-correct implementations; they are *not*
stubs. The :class:`PublishError` raised by the default publisher is the
single error-shape the queue command translates into exit code ``1``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import click

from forge.config.models import ForgeConfig
from forge.lifecycle.identifiers import (
    InvalidIdentifierError,
    validate_feature_id,
)
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import (
    DuplicateBuildError,
    SqliteLifecyclePersistence,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exit-code constants — see ``API-cli.md §3.3``
# ---------------------------------------------------------------------------

#: Successful enqueue.
EXIT_OK = 0
#: Generic CLI error / messaging-layer publish failure (Group G + Group H).
EXIT_PUBLISH_FAILED = 1
#: Configuration / authorization refusal — repo not in allowlist (Group C).
EXIT_PATH_REFUSED = 2
#: Duplicate refused — either ``(feature_id, correlation_id)`` collision
#: (Group B) or an active in-flight build for the feature (Group C).
EXIT_DUPLICATE = 3
#: Identifier validation failed — traversal/null-byte/disallowed-char
#: (concern ``sc_003`` / Group A). NEW exit code added by TASK-PSM-008.
EXIT_INVALID_IDENTIFIER = 4

#: Default location of the SQLite substrate when ``$FORGE_DB_PATH`` is
#: unset. Honours the convention used by every other Forge subsystem.
DEFAULT_DB_PATH = Path("~/.forge/forge.db")

#: NATS subject family for build-queued events
#: (``API-nats-pipeline-events.md §3``).
BUILD_QUEUED_SUBJECT_PREFIX = "pipeline.build-queued"

#: Source-id stamped onto every envelope this CLI emits. Distinct from
#: the ``"forge"`` value used by the agent-runtime publisher so
#: subscribers can tell CLI-originated builds from runtime-originated
#: ones.
SOURCE_ID = "forge-cli"


#: Exit code reserved for ``--mode`` related parser refusals
#: (e.g. Mode B with multiple feature ids — ASSUM-006). Re-uses the
#: standard Click usage-error exit code so operators see a familiar
#: parse-time message.
EXIT_MODE_USAGE = 2

#: Mapping from the ``--mode`` short flag values to canonical
#: :class:`BuildMode` enum members. Operators type ``--mode b`` rather
#: than ``--mode mode-b`` per FEAT-FORGE-008 ASSUM-016.
_MODE_FLAG_TO_BUILD_MODE: dict[str, BuildMode] = {
    "a": BuildMode.MODE_A,
    "b": BuildMode.MODE_B,
    "c": BuildMode.MODE_C,
}

#: Help text for ``--mode``. References FEAT-FORGE-008 chain shapes
#: verbatim so operators do not need to read source code to choose a
#: mode (AC: "Help text for --mode references the FEAT-FORGE-008 chain
#: shapes verbatim").
_MODE_HELP_TEXT = (
    "Pipeline build mode (FEAT-FORGE-008). "
    "'a' = Mode A: full greenfield run "
    "(product-owner -> architect -> system-arch -> system-design -> "
    "feature-spec -> task-review -> autobuild -> pull-request-review). "
    "'b' = Mode B: add-feature-to-existing-project "
    "(starts at /feature-spec, skips product-owner / architect / "
    "/system-arch / /system-design; ASSUM-001). "
    "'c' = Mode C: review-and-fix cycle "
    "(/task-review pairs with /task-work per fix task; "
    "optional pull-request-review terminator; ASSUM-004). "
    "Default 'a' preserves backwards compatibility."
)


__all__ = [
    "BUILD_QUEUED_SUBJECT_PREFIX",
    "DEFAULT_DB_PATH",
    "EXIT_DUPLICATE",
    "EXIT_INVALID_IDENTIFIER",
    "EXIT_MODE_USAGE",
    "EXIT_OK",
    "EXIT_PATH_REFUSED",
    "EXIT_PUBLISH_FAILED",
    "PublishError",
    "SOURCE_ID",
    "make_persistence",
    "publish",
    "queue_cmd",
    "resolve_mode",
]


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class PublishError(RuntimeError):
    """Raised by :func:`publish` when the NATS write fails.

    Distinct from
    :class:`forge.adapters.nats.pipeline_publisher.PublishFailure` so the
    CLI does not need to import the NATS adapter to catch the error —
    this seam translates any underlying failure into a domain shape so
    the import-discipline rule (``cli/`` modules must not pull
    ``forge.adapters.nats.*`` at the top level) holds.
    """


# ---------------------------------------------------------------------------
# Persistence-protocol shape (duck type)
# ---------------------------------------------------------------------------


class _PersistenceLike(Protocol):
    """Subset of :class:`SqliteLifecyclePersistence` used by ``forge queue``.

    The real :class:`SqliteLifecyclePersistence` satisfies this Protocol
    structurally — declaring it explicitly here lets tests substitute
    a minimal stand-in without inheriting from the full facade.
    """

    def exists_active_build(self, feature_id: str) -> bool: ...

    def record_pending_build(self, payload: Any) -> str: ...


# ---------------------------------------------------------------------------
# Module-level seams (mockable in tests)
# ---------------------------------------------------------------------------


def make_persistence(config: ForgeConfig) -> _PersistenceLike:
    """Construct the production :class:`SqliteLifecyclePersistence`.

    The CLI is short-lived (per ``API-cli.md``), so we open a fresh
    writer connection on each invocation and never hand it back to the
    caller. The connection is intentionally not closed here — Python's
    GC drops it on process exit, and SQLite's per-connection WAL state
    is flushed on every ``COMMIT`` issued by the persistence layer.

    The DB path is resolved from ``$FORGE_DB_PATH`` (operator override)
    or falls back to :data:`DEFAULT_DB_PATH`. Parent directories are
    created on demand so a fresh checkout can run ``forge queue`` without
    manual setup.
    """
    # Imports are local so importing :mod:`forge.cli.queue` does not
    # eagerly pull in the SQLite adapter or the migration runner — keeps
    # ``forge --help`` fast and helps the import-discipline check.
    from forge.adapters.sqlite.connect import connect_writer
    from forge.lifecycle.migrations import apply_at_boot

    raw_path = os.environ.get("FORGE_DB_PATH")
    db_path = Path(raw_path).expanduser() if raw_path else DEFAULT_DB_PATH.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = connect_writer(db_path)
    apply_at_boot(connection)

    # ``config`` is forward-passed only — current persistence reads no
    # config fields. Keeping the parameter lets future tasks (e.g.
    # WAL-mode tuning) plumb new fields without breaking the seam.
    _ = config
    return SqliteLifecyclePersistence(connection=connection, db_path=db_path)


def publish(subject: str, body: bytes) -> None:
    """Publish ``body`` to NATS subject ``subject`` synchronously.

    The CLI is sync; the underlying NATS client (``nats-py``) is async.
    We bridge by spinning up a single-shot :func:`asyncio.run` that opens
    a connection, publishes the body, flushes, and closes — fire-and-
    forget semantics matching the rest of the pipeline (LES1 parity
    rule: PubAck is informational, not proof of delivery).

    Args:
        subject: Canonical NATS subject
            (``pipeline.build-queued.{feature_id}``).
        body: Pre-serialised envelope bytes.

    Raises:
        PublishError: When the NATS connect/publish/flush fails for any
            reason. The originating exception is preserved as
            ``__cause__``.
    """
    import asyncio  # local — keeps top-level import surface small

    servers = os.environ.get("FORGE_NATS_URL", "nats://127.0.0.1:4222")

    async def _publish_once() -> None:
        try:
            import nats  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - covered via seam
            raise PublishError(
                "nats client not installed — `pip install nats-py`"
            ) from exc

        client = await nats.connect(servers=servers)
        try:
            await client.publish(subject, body)
            await client.flush()
        finally:
            await client.close()

    try:
        asyncio.run(_publish_once())
    except PublishError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised as a domain error
        raise PublishError(f"publish to {subject!r} failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_originating_user() -> str | None:
    """Best-effort lookup of the operator's login name.

    :func:`os.getlogin` raises :class:`OSError` on detached terminals
    (containers, CI, ``nohup`` jobs). We fall back to ``$USER``/``$LOGNAME``
    and finally to ``None`` — :class:`BuildQueuedPayload` allows a
    missing originating user.
    """
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or None


def _path_in_allowlist(repo: Path, allowlist: list[Path]) -> bool:
    """Return ``True`` when ``repo`` matches an entry in ``allowlist``.

    Comparison is done against the *resolved* absolute path so a
    relative ``--repo`` is matched against canonical allowlist entries.

    A bare/empty ``allowlist`` (the schema default — see
    :class:`forge.config.models.QueueConfig`) means "no restriction" per
    the model docstring; in that case every repo passes.
    """
    if not allowlist:
        return True
    repo_resolved = Path(repo).expanduser().resolve()
    for entry in allowlist:
        try:
            entry_resolved = Path(entry).expanduser().resolve()
        except (OSError, RuntimeError):
            # Defensive — pathological symlink loops should not crash
            # the CLI. Skip the bad entry; the operator can re-run after
            # cleaning up ``forge.yaml``.
            logger.warning("repo_allowlist entry %r could not be resolved", entry)
            continue
        if repo_resolved == entry_resolved:
            return True
        # Allow nested checkouts — a queue against
        # ``/work/repos/foo/sub`` passes when ``/work/repos/foo`` is
        # allowlisted.
        try:
            repo_resolved.relative_to(entry_resolved)
        except ValueError:
            continue
        return True
    return False


#: Permitted characters in a slug segment after sanitisation. Mirrors the
#: ``REPO_PATTERN`` in :mod:`nats_core.events._pipeline` so the derived
#: ``BuildQueuedPayload.repo`` value never trips the upstream validator.
_REPO_SEGMENT_FORBIDDEN_CHARS = " /\\:;,'\""


def _sanitise_segment(segment: str) -> str:
    """Replace any character outside ``[A-Za-z0-9._-]`` with ``_``."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in segment)


def _path_to_repo_slug(repo: Path) -> str:
    """Derive an ``org/name`` slug for :class:`BuildQueuedPayload.repo`.

    The CLI's ``--repo`` flag is a filesystem path (it has to be — we
    need to allowlist-check it and pass it to GuardKit), but the wire
    payload requires an ``org/name`` GitHub-shaped slug
    (``REPO_PATTERN`` in :mod:`nats_core.events._pipeline`). We bridge
    the two by taking the resolved path's last two components and
    sanitising any character outside ``[A-Za-z0-9._-]`` to ``_``.

    A single-component path (e.g. ``/tmp``) is mapped to ``local/{name}``
    so the slug is always a valid two-segment string.
    """
    resolved = Path(repo).expanduser().resolve()
    name = _sanitise_segment(resolved.name) or "repo"
    parent = resolved.parent.name
    org = _sanitise_segment(parent) if parent else "local"
    if not org:
        org = "local"
    return f"{org}/{name}"


#: Regex that gates the Mode C positional argument at the CLI boundary
#: (TASK-F8-002 / F008-VAL-002). Mirrors the canonical
#: ``TASK_ID_PATTERN`` in :mod:`nats_core.events._pipeline` so an
#: operator-supplied task_id is refused at the boundary rather than at
#: the Pydantic wire-validation layer one stack frame deeper.
_TASK_ID_REGEX: re.Pattern[str] = re.compile(r"^TASK-[A-Z0-9]{3,12}$")


def _load_parent_feature_from_fix_task_yaml(yaml_path: Path) -> str:
    """Read the ``parent_feature`` field from a fix-task YAML.

    Mode C dispatches carry a per-fix-task identifier on the wire
    alongside the parent feature_id. The fix-task YAML at
    ``--feature-yaml`` is the canonical source for the parent feature
    reference — the same artefact a developer reads when running
    ``/task-work TASK-XXX`` — so resolving it here keeps Mode C from
    having to thread a duplicate ``--parent-feature`` flag.

    Args:
        yaml_path: Path to the fix-task YAML supplied via
            ``--feature-yaml``. The CLI has already verified the file
            exists via Click's ``type=click.Path(exists=True)``.

    Returns:
        The raw ``parent_feature`` string (the caller is responsible
        for running it through :func:`validate_feature_id` and the
        wire-layer regex).

    Raises:
        click.UsageError: When the YAML is malformed, the top-level
            value is not a mapping, or ``parent_feature`` is missing
            or not a string.
    """
    # ``yaml`` is imported lazily — the queue CLI's import surface is
    # otherwise stdlib + click + nats-core, and YAML parsing only runs
    # on the Mode C branch.
    import yaml

    try:
        text = Path(yaml_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise click.UsageError(
            f"Cannot read fix-task YAML {str(yaml_path)!r}: {exc}"
        ) from exc

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise click.UsageError(
            f"Fix-task YAML {str(yaml_path)!r} is malformed: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise click.UsageError(
            f"Fix-task YAML {str(yaml_path)!r} must be a YAML mapping "
            "at the top level"
        )

    parent = data.get("parent_feature")
    if not isinstance(parent, str) or not parent.strip():
        raise click.UsageError(
            "Mode C requires the fix-task YAML to declare a non-empty "
            f"'parent_feature' field (string); got {parent!r} in {yaml_path}"
        )
    return parent


def _envelope_bytes(payload: Any, correlation_id: str) -> bytes:
    """Wrap ``payload`` in :class:`MessageEnvelope` and serialise to bytes.

    The lazy :mod:`nats_core.envelope` import keeps the envelope module
    off the top-level import path of :mod:`forge.cli.queue` — important
    for the CLI startup-cost budget.
    """
    from nats_core.envelope import EventType, MessageEnvelope

    envelope = MessageEnvelope(
        source_id=SOURCE_ID,
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    return envelope.model_dump_json().encode("utf-8")


def resolve_mode(value: str) -> BuildMode:
    """Translate a ``--mode {a|b|c}`` flag into a :class:`BuildMode`.

    Accepts both the short single-character form (``a``/``b``/``c`` —
    the canonical CLI surface per FEAT-FORGE-008 ASSUM-016) and the full
    enum-string form (``mode-a``/``mode-b``/``mode-c``) so callers that
    already hold the wire value can round-trip without translation.

    Args:
        value: Raw flag value supplied on the command line.

    Returns:
        The corresponding :class:`BuildMode` enum member.

    Raises:
        click.BadParameter: For any value outside the accepted set.
    """
    if not isinstance(value, str):
        raise click.BadParameter(f"--mode must be a string; got {type(value).__name__}")
    candidate = value.strip().lower()
    if candidate in _MODE_FLAG_TO_BUILD_MODE:
        return _MODE_FLAG_TO_BUILD_MODE[candidate]
    # Allow the full enum-string form too (``mode-a`` etc.).
    try:
        return BuildMode(candidate)
    except ValueError as exc:
        raise click.BadParameter(
            f"--mode must be one of 'a'/'b'/'c' (or 'mode-a'/'mode-b'/'mode-c'); "
            f"got {value!r}"
        ) from exc


def _require_forge_config(config: Any) -> ForgeConfig:
    """Coerce ``ctx.obj`` into a :class:`ForgeConfig` or fail with a clear error.

    The top-level group (:func:`forge.cli.main.main`) populates
    ``ctx.obj`` with a parsed :class:`ForgeConfig` when ``--config`` is
    supplied (or when ``./forge.yaml`` exists). For ``forge queue``
    specifically a config is mandatory — the queue subcommand needs the
    repo allowlist and the queue defaults — so we surface a
    :class:`click.UsageError` if it is missing.
    """
    if isinstance(config, ForgeConfig):
        return config
    raise click.UsageError(
        "forge queue requires a forge.yaml — pass --config <path> or run "
        "from a directory containing forge.yaml"
    )


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command(name="queue")
@click.argument("feature_ids", nargs=-1, required=True)
@click.option(
    "--mode",
    "mode_flag",
    type=click.Choice(["a", "b", "c"], case_sensitive=False),
    default="a",
    show_default=True,
    help=_MODE_HELP_TEXT,
)
@click.option(
    "--repo",
    required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=True),
    help="Filesystem path to the local checkout. Must match repo_allowlist.",
)
@click.option(
    "--branch",
    default="main",
    show_default=True,
    help="Branch the build should target.",
)
@click.option(
    "--feature-yaml",
    "feature_yaml",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the feature YAML spec consumed by GuardKit.",
)
@click.option(
    "--max-turns",
    type=int,
    default=None,
    help="Per-build Player-Coach turn budget. Overrides queue.default_max_turns.",
)
@click.option(
    "--timeout",
    "sdk_timeout_seconds",
    type=int,
    default=None,
    help=(
        "GuardKit autobuild SDK timeout in seconds. Overrides "
        "queue.default_sdk_timeout_seconds."
    ),
)
@click.option(
    "--correlation-id",
    "correlation_id",
    default=None,
    help=(
        "Stable identifier for tracing the build across stages. "
        "Auto-generated (uuid4) when omitted."
    ),
)
@click.option(
    "--profile",
    "profile_name",
    default=None,
    help=(
        "Budget-guard profile from config.budget.profiles (e.g. 'unattended'). "
        "Selects review-cycle / wall-clock caps for the build (FEAT-UBS-002). "
        "Attended (the default) = caps off (ASSUM-010)."
    ),
)
@click.pass_obj
def queue_cmd(
    config_obj: Any,
    feature_ids: tuple[str, ...],
    mode_flag: str,
    repo: str,
    branch: str,
    feature_yaml: str,
    max_turns: int | None,
    sdk_timeout_seconds: int | None,
    correlation_id: str | None,
    profile_name: str | None,
) -> None:
    """Enqueue a build for ``feature_id`` (write-then-publish).

    See module docstring for the full step-by-step contract.
    """
    # Resolve the mode early so its validation runs *before* any side
    # effect (mirrors the validate_feature_id discipline at AC-003 / sc_003).
    build_mode = resolve_mode(mode_flag)

    # Mode B requires exactly one feature identifier (ASSUM-006: single
    # feature per Mode B build). Mode A and Mode C currently accept
    # exactly one identifier too — multi-feature inputs are rejected at
    # parse time across every mode so the persistence layer never has
    # to fan out a single CLI invocation. Mode C's single ``feature_ids``
    # member carries the *subject* identifier (e.g. a task ID or a PR
    # ID), not necessarily a FEAT-XXX value; the existing schema column
    # is feature-id-shaped which is wide enough to hold it.
    if len(feature_ids) == 0:
        # ``nargs=-1, required=True`` already enforces this; defensive.
        raise click.UsageError(
            "forge queue requires at least one feature/subject identifier"
        )
    if len(feature_ids) > 1:
        if build_mode is BuildMode.MODE_B:
            raise click.UsageError(
                f"forge queue --mode b requires exactly one feature "
                f"identifier (FEAT-FORGE-008 ASSUM-006: single feature per "
                f"Mode B build); got {len(feature_ids)}: "
                f"{list(feature_ids)!r}"
            )
        raise click.UsageError(
            f"forge queue accepts exactly one feature/subject identifier; "
            f"got {len(feature_ids)}: {list(feature_ids)!r}"
        )
    positional_id = feature_ids[0]

    config = _require_forge_config(config_obj)

    # 0. FEAT-UBS-002 — resolve + validate the budget-guard profile BEFORE any
    #    side effect (mirrors the mode-resolution discipline). An unknown name
    #    is rejected here rather than silently falling back to the default.
    #
    #    TASK-UBS-002-integration §2(a): the validated name is now carried to
    #    the daemon on the ``builds.profile`` column (schema_v5.sql) — see the
    #    ``queue_build(..., profile=profile_name)`` write below. The daemon
    #    reads it back via ``BuildRow.profile`` and resolves caps with
    #    ``config.budget.resolve(row.profile)`` (NULL → default_profile). The
    #    nats-core payload deliberately stays profile-free (§2(b) barred).
    try:
        # Pass profile_name straight through — resolve() owns the None→default
        # mapping (models.py), so a future change to that logic is not silently
        # bypassed by pre-computing the effective name here.
        guards = config.budget.resolve(profile_name)
    except KeyError as exc:
        # Reuse resolve()'s message (single sort); args[0] strips KeyError's
        # repr-quoting so the operator sees a clean sentence.
        raise click.UsageError(exc.args[0])
    effective_profile = profile_name or config.budget.default_profile
    if guards.caps_enabled:
        # Render only the caps that are set, derived from the model so a new cap
        # field needs no edit here. The coach-score floor is flagged dormant
        # (ADR-ARCH-033) so an inert stub is not shown as an active cap.
        rendered = ", ".join(
            f"{name}={value}"
            + (
                " (dormant — coach-score feed pending, ADR-ARCH-033)"
                if name == "min_coach_score"
                else ""
            )
            for name, value in guards.model_dump().items()
            if value is not None
        )
        click.echo(f"budget profile {effective_profile!r}: {rendered}")
    # Honest-deferral note: the profile is carried to the daemon on the
    # ``builds.profile`` row (TASK-UBS-002-integration §2(a)) and resolved via
    # ``config.budget.resolve(row.profile)``. The serve-side wiring is now
    # complete — ``serve.py`` builds the resolved caps
    # (``resolve_budget_for_build``), the wall-clock reader, and the
    # ``budget_pause`` collaborator, and ``build_supervisor`` threads them onto
    # the Supervisor's budget fields; the durable coach-score store is live too.
    # What remains dormant is the *enforcement loop*: the Mode-C ``next_turn``
    # driver that would consume those caps to pause a live build has no
    # production caller yet (activation is a plan-of-record decision). So caps
    # resolve and the wiring exists, but they do not yet pause a running build.
    # Surface that for a capped override so an operator is not led to believe
    # caps are being enforced yet.
    if profile_name is not None and guards.caps_enabled:
        click.echo(
            f"NOTE: --profile {profile_name!r} is persisted to the build row "
            "and resolved by the daemon, and the budget wiring exists in the "
            "supervisor (serve.py resolve_budget_for_build / make_budget_pause), "
            "but the Mode C enforcement loop that would pause a live build is "
            "not yet activated (a plan-of-record decision). Caps travel and "
            "resolve but do not yet pause a running build.",
            err=True,
        )

    # 1. Resolve ``feature_id`` and (Mode C only) ``task_id`` BEFORE any
    #    side effect (AC-003 / sc_003). Mode A/B reuse the existing
    #    discipline; Mode C interprets the positional as a TASK-XXX
    #    dispatch target and resolves the parent feature_id from the
    #    fix-task YAML's ``parent_feature`` field (TASK-F8-002 AC-8 / 9).
    task_id: str | None
    if build_mode is BuildMode.MODE_C:
        # 1a. Mode C positional must be TASK-XXX. The wire layer will
        # also enforce this regex, but refusing at the CLI boundary
        # gives a clearer error before any persistence/publish work.
        if not _TASK_ID_REGEX.match(positional_id):
            click.echo(
                "Mode C requires positional argument to match "
                f"{_TASK_ID_REGEX.pattern}; got {positional_id!r}",
                err=True,
            )
            sys.exit(EXIT_INVALID_IDENTIFIER)
        task_id = positional_id
        # 1b. Parent feature_id comes from the fix-task YAML.
        try:
            raw_parent = _load_parent_feature_from_fix_task_yaml(Path(feature_yaml))
        except click.UsageError:
            # Click handles UsageError formatting + exit code on its own
            # — re-raise so the operator sees the canonical message
            # rather than a swallow-and-re-format here.
            raise
        try:
            feature_id = validate_feature_id(raw_parent)
        except InvalidIdentifierError as exc:
            click.echo(
                f"Invalid parent_feature in fix-task YAML ({exc.reason}): "
                f"{exc.value!r}",
                err=True,
            )
            sys.exit(EXIT_INVALID_IDENTIFIER)
    else:
        # Mode A / Mode B: positional is the feature_id (existing
        # contract); no per-fix-task identifier rides on the wire.
        task_id = None
        try:
            feature_id = validate_feature_id(positional_id)
        except InvalidIdentifierError as exc:
            click.echo(
                f"Invalid feature_id ({exc.reason}): {exc.value!r}",
                err=True,
            )
            sys.exit(EXIT_INVALID_IDENTIFIER)

    # 2. Allowlist check (AC-004 / Group C "path-allowlist refused").
    repo_path = Path(repo)
    if not _path_in_allowlist(repo_path, config.queue.repo_allowlist):
        click.echo(
            f"Repository {repo!r} is not in queue.repo_allowlist; "
            "refusing to enqueue (Group C path-allowlist refused).",
            err=True,
        )
        sys.exit(EXIT_PATH_REFUSED)

    # 3. Merge defaults (AC-005).
    effective_max_turns = (
        max_turns if max_turns is not None else config.queue.default_max_turns
    )
    effective_timeout = (
        sdk_timeout_seconds
        if sdk_timeout_seconds is not None
        else config.queue.default_sdk_timeout_seconds
    )
    effective_correlation_id = correlation_id or str(uuid.uuid4())

    # 4. Build the wire payload. ``nats_core.events`` is imported lazily
    #    so this module's import surface stays small.
    from nats_core.events import BuildQueuedPayload

    now = datetime.now(UTC)
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo=_path_to_repo_slug(repo_path),
        branch=branch,
        feature_yaml_path=str(Path(feature_yaml)),
        max_turns=effective_max_turns,
        sdk_timeout_seconds=effective_timeout,
        triggered_by="cli",
        originating_adapter="cli-wrapper",
        originating_user=_resolve_originating_user(),
        correlation_id=effective_correlation_id,
        requested_at=now,
        queued_at=now,
        # TASK-F8-002 / F008-VAL-002 — mode is now a first-class wire
        # field; ``task_id`` is non-None iff ``mode == "mode-c"`` (the
        # nats-core ``_task_id_required_iff_mode_c`` model_validator
        # enforces the bidirectional invariant).
        mode=build_mode.value,
        task_id=task_id,
    )

    # 5. Construct the persistence facade (production: SQLite; tests:
    #    monkey-patched fake).
    persistence = make_persistence(config)

    # 6. Active in-flight check (Group C "active duplicate").
    if persistence.exists_active_build(feature_id):
        click.echo(
            f"duplicate build refused: an active build for {feature_id} "
            "is already in flight (Group C).",
            err=True,
        )
        sys.exit(EXIT_DUPLICATE)

    # 7. Write SQLite row FIRST (AC-006 / sc_002 ordering). The mode is
    #    persisted on the ``Build`` row via ``queue_build`` (alias of
    #    ``record_pending_build`` accepting an explicit ``mode=`` kwarg
    #    — TASK-MBC8-001) so crash-recovery (FEAT-FORGE-001) and the
    #    supervisor wiring from TASK-MBC8-008 see the correct mode after
    #    a restart.
    try:
        if hasattr(persistence, "queue_build"):
            persistence.queue_build(payload, mode=build_mode, profile=profile_name)
        else:
            # Fallback for in-memory test fakes that pre-date
            # TASK-MBC8-001's ``queue_build`` alias. The mode and profile
            # are passed via the payload's open ``ConfigDict(extra="allow")``
            # slot so the fake's ``record_pending_build`` can sniff them via
            # ``getattr(payload, "mode"/"profile", None)``.
            try:
                payload.mode = build_mode.value  # type: ignore[attr-defined]
                payload.profile = profile_name  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                # Last-ditch: pass via a kwarg the fake may accept.
                pass
            persistence.record_pending_build(payload)
    except DuplicateBuildError as exc:
        click.echo(
            f"duplicate build refused: {exc} (Group B).",
            err=True,
        )
        sys.exit(EXIT_DUPLICATE)

    # 8. THEN publish to NATS (AC-006 second half).
    subject = f"{BUILD_QUEUED_SUBJECT_PREFIX}.{feature_id}"
    body = _envelope_bytes(payload, effective_correlation_id)
    try:
        publish(subject, body)
    except PublishError as exc:
        # 9. AC-007: do NOT roll back the SQLite row; surface a clear
        #    diagnostic on stderr identifying the messaging-layer cause
        #    (Group H "messaging unreachable") and exit 1.
        click.echo(
            f"Queued {feature_id} (build pending) but pipeline NOT NOTIFIED — "
            f"publish failed (messaging-layer): {exc}",
            err=True,
        )
        sys.exit(EXIT_PUBLISH_FAILED)

    # 10. Happy path — print operator-facing confirmation and exit 0.
    click.echo(
        f"Queued {feature_id} (build pending) "
        f"mode={build_mode.value} "
        f"correlation_id={effective_correlation_id}"
    )
    sys.exit(EXIT_OK)
