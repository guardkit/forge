"""Production factory for :class:`ForwardContextBuilder` (TASK-FW10-003).

This module is one of the four ``dispatch_autobuild_async`` collaborator
factories that Wave 2 of FEAT-FORGE-010 wires up. Each Wave 2 factory
lives in its own ``_serve_deps_*.py`` module so the five Wave 2 tasks
can land independently without cross-merge conflicts: composition into
the daemon's deps graph is owned by TASK-FW10-007 (the future
``_serve_deps.py``).

What this factory does
----------------------

:func:`build_forward_context_builder` accepts the two production
collaborators :class:`ForwardContextBuilder` requires:

1. ``sqlite_pool`` — a duck-typed object that satisfies the
   :class:`forge.pipeline.forward_context_builder.StageLogReader`
   Protocol (``get_approved_stage_entry`` and
   ``get_all_approved_stage_entries``). Production wires the
   FEAT-FORGE-001 SQLite stage_log adapter; tests inject an in-memory
   fake. The factory does not validate the duck type at runtime — the
   :class:`StageLogReader` Protocol is structural and the builder will
   fail loudly with ``AttributeError`` on the first call if the contract
   is unmet.

2. ``forge_config`` — the validated :class:`forge.config.models.ForgeConfig`
   loaded by ``ServeConfig.from_env()``. The factory reads
   ``forge_config.permissions.filesystem.allowlist`` (the absolute path
   roots the operator declared in ``forge.yaml``) and wraps them into a
   :class:`WorktreeAllowlist`-conforming adapter. The adapter is the
   defence-in-depth twin of the FEAT-FORGE-005 per-build allowlist —
   even if a downstream caller hands the builder a ``build_id`` whose
   per-build allowlist hasn't been wired yet, the project-wide
   allowlist still bounds every artefact path the builder threads onto
   a downstream ``--context`` flag.

The returned :class:`ForwardContextBuilder` is the production object
that ``dispatch_autobuild_async`` (and any other consumer) calls
:meth:`ForwardContextBuilder.build_for` on.

What this factory does NOT do
-----------------------------

* It does not import from ``forge.cli._serve_deps`` — composition into
  the daemon's deps graph is TASK-FW10-007's job. Keeping this factory
  decoupled from the deps composition module is what lets the five
  Wave 2 tasks merge in any order.
* It does not own the rejection-to-envelope translation. When the
  builder filters every artefact path (the "disallowed worktree path"
  rejection branch in the ACs), the caller receives an empty / partial
  forward context. Translating that into a ``build-failed`` JetStream
  envelope is delegated to TASK-FW10-009.
* It does not validate ``sqlite_pool``. The :class:`StageLogReader`
  Protocol is ``runtime_checkable`` but ``isinstance`` against a
  Protocol is best-effort (it only checks attribute names) and we
  prefer the duck-typed contract — production wires a SQLite adapter,
  tests wire an in-memory fake; both satisfy the Protocol without ever
  declaring inheritance.

References:
    - TASK-FW10-003 — this factory's brief.
    - TASK-FW10-007 — composition into the daemon's deps graph.
    - TASK-FW10-009 — build-failed envelope translation.
    - :mod:`forge.pipeline.forward_context_builder` — the class this
      factory constructs and its two Protocol seams.
    - :class:`forge.config.models.ForgeConfig` — the source of truth
      for the filesystem allowlist this factory consumes.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.config.models import ForgeConfig
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.forward_context_builder import (
    ApprovedStageEntry,
    ForwardContextBuilder,
    StageLogReader,
    WorktreeAllowlist,
)
from forge.pipeline.stage_taxonomy import StageClass

__all__ = [
    "ForgeConfigWorktreeAllowlist",
    "build_forward_context_builder",
    "build_stage_log_reader",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ForgeConfigWorktreeAllowlist:
    """:class:`WorktreeAllowlist` adapter over ``ForgeConfig.permissions.filesystem``.

    The adapter exposes the :class:`WorktreeAllowlist` Protocol surface
    expected by :class:`ForwardContextBuilder` (a single
    :meth:`is_allowed` boolean predicate). It is the defence-in-depth
    twin of the per-build FEAT-FORGE-005 allowlist: even before
    FEAT-FORGE-005 wires a per-build allowlist for ``build_id``, this
    adapter ensures every artefact path threaded onto a forward
    ``--context`` flag lies inside one of the operator-declared
    filesystem roots.

    Path containment is computed via the resolved-path / ``commonpath``
    idiom rather than by string ``startswith``: a sibling whose name
    shares a textual prefix with an allowlist root (``/work/build-1``
    vs ``/work/build-12345``) must NOT be allowed, and a textual
    ``startswith`` check would let it through. We resolve both sides
    against the filesystem (without requiring the path to exist —
    :func:`os.path.normpath` strips ``..``) and then ask
    :func:`os.path.commonpath` whether the candidate is contained.

    Attributes:
        allowed_roots: Tuple of normalised absolute path strings the
            adapter compares against. Empty tuple is a legal (and
            deliberately deny-all) configuration — useful for
            integration tests that want to exercise the rejection
            branch without changing the rest of the config.

    Notes:
        ``build_id`` is part of the Protocol signature because
        FEAT-FORGE-005's per-build allowlist needs it. This adapter
        ignores it: the FEAT-FORGE-010 wiring relies on the operator's
        project-wide ``forge.yaml`` allowlist, not a per-build one.
        The argument is preserved on the surface so a future swap to a
        per-build implementation is a drop-in replacement.
    """

    allowed_roots: tuple[str, ...]

    def is_allowed(self, build_id: str, path: str) -> bool:
        """Return ``True`` iff ``path`` is contained in one of :attr:`allowed_roots`.

        The check is symmetric: ``path == root`` is allowed, and any
        descendant ``root/sub/...`` is allowed. A path that escapes the
        root via ``..`` is normalised first and then tested again, so
        an attacker who supplies ``"/work/build-1/../../etc/passwd"``
        sees the rejection.

        ``build_id`` is currently unused (see class docstring). It is
        retained on the signature because the
        :class:`forge.pipeline.forward_context_builder.WorktreeAllowlist`
        Protocol requires it.
        """
        del build_id  # honoured by the per-build allowlist; project-wide here.

        if not path:
            # Defensive — an empty path string can never be inside a
            # non-empty root and admitting it would let a bug upstream
            # silently thread a meaningless ``--context`` value.
            return False

        try:
            candidate = os.path.normpath(os.path.abspath(path))
        except (TypeError, ValueError):
            # ``os.path.abspath`` raises ``TypeError`` for non-string
            # inputs and ``ValueError`` for embedded NULs. Either is a
            # caller bug; refuse rather than crash the builder.
            logger.warning(
                "forge.cli._serve_deps_forward_context: rejecting "
                "non-normalisable path %r",
                path,
            )
            return False

        for root in self.allowed_roots:
            try:
                common = os.path.commonpath([candidate, root])
            except ValueError:
                # Different drives on Windows, or one path is relative
                # while the other is absolute. Either way, no overlap.
                continue
            if common == root:
                return True
        return False


def _normalise_root(root: Path | str) -> str:
    """Normalise a ``forge.yaml`` allowlist entry to an absolute path string."""
    return os.path.normpath(os.path.abspath(str(root)))


def build_forward_context_builder(
    sqlite_pool: Any,
    forge_config: ForgeConfig,
) -> ForwardContextBuilder:
    """Build the production :class:`ForwardContextBuilder` for ``forge serve``.

    Wires:

    * ``sqlite_pool`` — duck-typed :class:`StageLogReader` over the
      FEAT-FORGE-001 ``stage_log`` table. Production passes the SQLite
      reader pool; tests pass an in-memory fake.
    * ``forge_config.permissions.filesystem.allowlist`` — the absolute
      path roots the operator declared in ``forge.yaml``. The factory
      wraps these in :class:`ForgeConfigWorktreeAllowlist` so the
      builder filters every artefact path through the allowlist before
      threading it onto a ``--context`` flag.

    The returned builder is fully wired and ready to call
    :meth:`ForwardContextBuilder.build_for`. The factory is
    idempotent (it allocates a fresh adapter on every call), so two
    builds in the same process can each hold their own builder
    instance without sharing state.

    Args:
        sqlite_pool: Object satisfying the
            :class:`StageLogReader` Protocol. The Protocol is
            ``runtime_checkable`` but the factory does not enforce
            ``isinstance`` — the builder is duck-typed, and the first
            call to :meth:`ForwardContextBuilder.build_for` will fail
            loudly if the contract is unmet.
        forge_config: Validated root config. The factory reads
            ``forge_config.permissions.filesystem.allowlist`` and
            normalises each entry to an absolute path string.

    Returns:
        A :class:`ForwardContextBuilder` whose
        :class:`StageLogReader` and :class:`WorktreeAllowlist`
        collaborators are bound to ``sqlite_pool`` and
        ``forge_config`` respectively.

    Raises:
        AttributeError: If ``forge_config`` lacks the expected
            ``permissions.filesystem.allowlist`` chain. The Pydantic
            schema in :mod:`forge.config.models` enforces this at
            config-load time, so reaching this branch in production
            indicates a malformed test fixture rather than an operator
            misconfiguration.
    """
    # Cast to StageLogReader for static type-checkers. The cast is a
    # documentation marker — the real contract is duck-typed.
    stage_log_reader: StageLogReader = sqlite_pool

    allowed_roots = tuple(
        _normalise_root(entry)
        for entry in forge_config.permissions.filesystem.allowlist
    )
    worktree_allowlist: WorktreeAllowlist = ForgeConfigWorktreeAllowlist(
        allowed_roots=allowed_roots,
    )

    logger.debug(
        "forge.cli._serve_deps_forward_context: bound ForwardContextBuilder "
        "with %d filesystem-allowlist root(s)",
        len(allowed_roots),
    )

    return ForwardContextBuilder(
        stage_log_reader=stage_log_reader,
        worktree_allowlist=worktree_allowlist,
    )


# ---------------------------------------------------------------------------
# Production StageLogReader adapter (TASK-FORGE-FRR-F010B)
# ---------------------------------------------------------------------------


#: Key under which the gate decision lives inside ``stage_log.details_json``.
#: The ``stage_log`` schema does not have a dedicated ``gate_decision`` column
#: (see ``docs/design/contracts/API-sqlite-schema.md`` §2.2); the Protocol-level
#: vocabulary (``"approved"`` / ``"failed"`` / ``"rejected"``) is recorded in
#: ``details_json`` by the writers that own the gate-evaluation transitions
#: (e.g. the supervisor terminal handlers). Rows that lack this key are
#: treated as not-yet-approved and filtered out, matching the Protocol
#: contract on :class:`StageLogReader`.
_DETAILS_GATE_DECISION_KEY: str = "gate_decision"

#: Value of :data:`_DETAILS_GATE_DECISION_KEY` that admits a row to the
#: forward-context builder. Matches the ``_STATUS_APPROVED`` constant used by
#: :mod:`forge.pipeline.mode_c_planner` and
#: :mod:`forge.pipeline.terminal_handlers.mode_c`.
_GATE_DECISION_APPROVED: str = "approved"

#: Key under which the per-feature scoping discriminator is echoed onto
#: ``stage_log.details_json`` by :class:`_AutobuildStageLogRecorder` (TASK-FW10-004).
#: The schema does not have a ``feature_id`` column on ``stage_log``, so we
#: filter on the in-payload echo for per-feature stages.
_DETAILS_FEATURE_ID_KEY: str = "feature_id"

#: Optional artefact-paths key in ``details_json``. Producers that emit
#: file-shaped artefacts populate this list; ``"text"``-shaped producers
#: leave it absent or empty.
_DETAILS_ARTEFACT_PATHS_KEY: str = "artefact_paths"

#: Optional artefact-text key in ``details_json`` for ``"text"``-shaped
#: producers (charters, approved-output blobs, …).
_DETAILS_ARTEFACT_TEXT_KEY: str = "artefact_text"


class _SqliteStageLogReader:
    """Production :class:`StageLogReader` over the ``stage_log`` table.

    Wraps a :class:`SqliteLifecyclePersistence` and projects matching
    ``stage_log`` rows into :class:`ApprovedStageEntry` instances. The
    Protocol-level filter ``gate_decision == "approved"`` lives in
    ``details_json`` (see :data:`_DETAILS_GATE_DECISION_KEY` for why);
    rows that lack that key — or that carry any other value — are
    invisible to the builder, matching the
    :class:`StageLogReader` contract documented in
    :mod:`forge.pipeline.forward_context_builder`.

    Empty ``stage_log`` (the run-4 state for a fresh build) returns
    ``None`` from :meth:`get_approved_stage_entry` and an empty tuple
    from :meth:`get_all_approved_stage_entries` — never raises. This is
    the bug-fix shape of the adapter: TASK-FORGE-FRR-F010B observed
    ``AttributeError: 'SqliteLifecyclePersistence' object has no
    attribute 'get_approved_stage_entry'`` on the autobuild dispatch
    path because the production composer (``_serve_deps.py``) was
    handing the bare facade to ``build_forward_context_builder`` even
    though :class:`SqliteLifecyclePersistence` does not expose the
    :class:`StageLogReader` Protocol surface. Wrapping the pool in this
    adapter at the composition seam closes the gap without bloating the
    facade.
    """

    def __init__(self, persistence: SqliteLifecyclePersistence) -> None:
        if not isinstance(persistence, SqliteLifecyclePersistence):
            # Symmetrical with :func:`build_autobuild_state_initialiser`:
            # refuse a duck-typed input at the boundary so a misuse
            # surfaces here rather than as a confusing AttributeError on
            # the first ``build_for`` call.
            raise TypeError(
                "_SqliteStageLogReader: persistence must be a "
                "SqliteLifecyclePersistence; got "
                f"{type(persistence).__name__}"
            )
        self._persistence = persistence

    def get_approved_stage_entry(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> ApprovedStageEntry | None:
        """Return the first approved row for ``(build_id, stage, feature_id)``.

        Mode A and Mode B only ever emit one approved row per
        (build, stage, feature) tuple, so ``first`` is a deterministic
        choice. Mode C asks for the full list via
        :meth:`get_all_approved_stage_entries`.
        """
        for entry in self._iter_approved_entries(build_id, stage, feature_id):
            return entry
        return None

    def get_all_approved_stage_entries(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None = None,
    ) -> Sequence[ApprovedStageEntry]:
        """Return every approved row for ``(build_id, stage, feature_id)``.

        Order is the chronological insertion order produced by
        :meth:`SqliteLifecyclePersistence.read_stages` (``ORDER BY started_at``).
        Empty tuple if no rows match — never raises.
        """
        return tuple(self._iter_approved_entries(build_id, stage, feature_id))

    def _iter_approved_entries(
        self,
        build_id: str,
        stage: StageClass,
        feature_id: str | None,
    ):
        """Yield each ``ApprovedStageEntry`` matching the scope.

        Filter chain (all must match):

        1. ``stage_label`` equals ``stage.value``.
        2. ``details_json[_DETAILS_FEATURE_ID_KEY]`` equals ``feature_id``
           (echoed by :class:`_AutobuildStageLogRecorder`; absent for
           non-per-feature stages, in which case both sides are
           ``None``).
        3. ``details_json[_DETAILS_GATE_DECISION_KEY]`` equals
           ``_GATE_DECISION_APPROVED``.
        """
        rows = self._persistence.read_stages(build_id)
        for row in rows:
            if row.stage_label != stage.value:
                continue
            details = row.details
            row_feature_id = details.get(_DETAILS_FEATURE_ID_KEY)
            if row_feature_id != feature_id:
                continue
            if details.get(_DETAILS_GATE_DECISION_KEY) != _GATE_DECISION_APPROVED:
                continue
            paths_raw = details.get(_DETAILS_ARTEFACT_PATHS_KEY) or ()
            artefact_paths: tuple[str, ...] = tuple(str(p) for p in paths_raw)
            artefact_text_raw = details.get(_DETAILS_ARTEFACT_TEXT_KEY)
            artefact_text = (
                str(artefact_text_raw) if artefact_text_raw is not None else None
            )
            yield ApprovedStageEntry(
                gate_decision=_GATE_DECISION_APPROVED,
                artefact_paths=artefact_paths,
                artefact_text=artefact_text,
            )


def build_stage_log_reader(
    sqlite_pool: SqliteLifecyclePersistence,
) -> StageLogReader:
    """Construct the production :class:`StageLogReader` for ``forge serve``.

    Symmetric with :func:`forge.cli._serve_deps_state_channel.build_autobuild_state_initialiser`
    and :func:`forge.cli._serve_deps_stage_log.build_stage_log_recorder` —
    every Wave-2 collaborator factory wraps the shared
    :class:`SqliteLifecyclePersistence` in a narrow Protocol-shaped
    adapter rather than handing the bare facade to the consumer.

    The factory is the production-composer half of TASK-FORGE-FRR-F010B:
    before this factory existed, ``forge.cli._serve_deps`` passed
    ``sqlite_pool`` directly to :func:`build_forward_context_builder`,
    and the first ``forward_context_builder.build_for(...)`` call raised
    ``AttributeError`` because :class:`SqliteLifecyclePersistence`
    does not expose the :class:`StageLogReader` Protocol surface.

    Args:
        sqlite_pool: The shared facade owned by ``forge serve``. Must
            already have its schema bootstrapped by
            :func:`forge.lifecycle.migrations.apply_at_boot` —
            :meth:`StageLogReader.get_approved_stage_entry` issues a
            ``SELECT`` against ``stage_log`` and would fail on a fresh
            database otherwise.

    Returns:
        A :class:`StageLogReader` Protocol implementation ready to be
        passed to :func:`build_forward_context_builder`.

    Raises:
        TypeError: If ``sqlite_pool`` is not a
            :class:`SqliteLifecyclePersistence`. Surfacing the
            type-mismatch at the factory boundary mirrors
            :func:`build_autobuild_state_initialiser`'s posture.
    """
    reader = _SqliteStageLogReader(sqlite_pool)
    logger.info(
        "build_stage_log_reader: composed SQLite-backed "
        "StageLogReader against pool db_path=%s",
        sqlite_pool.db_path,
    )
    return reader
