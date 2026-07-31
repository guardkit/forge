"""The fix journey's receipts fold — the pack in, the per-stage receipts out.

Revival design pass §b.2 (``supervisor-revival-design-pass-2026-07-31``),
Stage 1c.

The routine path (codename Mode A) already leaves durable receipts: on
success the receipt families are copied out of the worktree before it is
removed (FEAT-DRC), and on failure it leaves a whole **failure pack** —
the receipt families plus a stdout narrative plus a machine-readable
``failure-manifest.json`` index — under
``$FORGE_RECEIPTS_DIR/<build_id>/`` (default
``~/forge-state/receipts/<build_id>/``), hardened to the owner
(0700 dirs / 0600 files, FEAT-DRF).

The fix journey folds that learning in **both directions**:

*As input* — the journey's first review reads the failed build's pack.
:func:`read_failure_pack` parses the index and names the receipt families
that actually made it out, so the reviewer starts from the evidence
rather than from a reason string.

*As output* — every ``/task-review`` and ``/task-work`` completion
exports its own per-stage receipts into the SAME root under
``<build_id>/stages/<NNN>-<stage>/``, with the SAME hardening; and a fix
journey that FAILS writes its own ``failure-manifest.json`` beside them.
Success and failure alike leave receipts.

Reuse, not duplication
----------------------

The path law, the hardening helper, the receipt-family list, the manifest
filename and the prior-manifest archiver all live in
:mod:`forge.subagents.autobuild_runner` and are imported from there — one
implementation, one posture. What this module adds is the *root
injection* every helper here accepts (``receipts_root=``), so a test can
point the whole fold at a ``tmp_path`` without touching the process
environment.

Posture (inherited verbatim from the routine path): **nothing here ever
raises and nothing here ever alters an outcome.** A receipts failure is
logged at WARNING and the journey continues — the pack's existence beats
its completeness, and a lost receipt must never lose a build.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "FIX_JOURNEY_PACK_KIND",
    "STAGES_DIRNAME",
    "FailurePackIndex",
    "StageReceiptExport",
    "export_stage_receipts",
    "fix_journey_receipts_root",
    "next_stage_key",
    "read_failure_pack",
    "write_fix_journey_failure_pack",
]


#: Sub-directory of a build's receipts pack holding the per-stage exports.
#: ``<root>/<build_id>/stages/<NNN>-<stage>/`` — deliberately a sibling of
#: the routine path's ``.guardkit/...`` family tree so a fix journey's
#: receipts never collide with a routine build's under a reused build_id.
STAGES_DIRNAME: str = "stages"

#: Discriminator stamped on a fix journey's own failure manifest. The
#: filename is deliberately the SAME as the routine path's
#: (``failure-manifest.json``) so every existing diagnoser finds it; this
#: key is how a reader tells "the build failed" from "the fix journey
#: that was meant to repair it failed".
FIX_JOURNEY_PACK_KIND: str = "fix-journey"

#: ``NNN-stage`` directory-name grammar for the per-stage exports.
_STAGE_DIR_RE = re.compile(r"^(?P<seq>\d{3})-(?P<rest>.+)$")


def fix_journey_receipts_root(receipts_root: "Path | str | None" = None) -> Path:
    """Resolve the durable receipts root, with the root injectable.

    Args:
        receipts_root: Explicit root. ``None`` (production) defers to the
            routine path's own resolution
            (:func:`forge.subagents.autobuild_runner._receipts_root` —
            ``$FORGE_RECEIPTS_DIR`` else ``~/forge-state/receipts``), so
            there is exactly ONE path law in the tree and this module
            does not get to disagree with it.

    Returns:
        The receipts root as a :class:`~pathlib.Path`. Path arithmetic
        only — never touches disk, never raises.
    """
    if receipts_root is not None:
        return Path(receipts_root).expanduser()
    # Local import: the runner pulls langgraph/yaml at module scope, and
    # this module is imported by the supervisor's composition root.
    from forge.subagents.autobuild_runner import _receipts_root

    return _receipts_root()


def _harden(root: Path) -> None:
    """Apply the routine path's 0700/0600 hardening to ``root``."""
    from forge.subagents.autobuild_runner import _harden_pack_permissions

    _harden_pack_permissions(root)


# ---------------------------------------------------------------------------
# As input — the failure pack the fix journey consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailurePackIndex:
    """The parsed ``failure-manifest.json`` of a failed build's pack.

    Every field is optional-shaped because the manifest is written
    best-effort by a *failing* build: a truncated or partially-written
    pack must degrade to "less context", never to an exception in the
    fix journey that is trying to repair it.

    Attributes:
        build_id: The failed build the pack belongs to.
        pack_dir: Directory the pack was read from.
        manifest_path: The index file itself.
        feature_id / correlation_id: Identity carried by the manifest.
        reason: The failure reason string.
        timed_out / exit_code / wedged: How the run ended.
        worktree_path / branch: Where the failed tree still is.
        failed_at: ISO timestamp the manifest was written.
        receipt_families_exported: Families THIS run actually copied.
        semantic_state_at_kill / resume: The build-monitor additions.
        present_families: Families that actually exist on disk now (the
            honest read — the manifest records intent at write time).
        stdout_log: The tee'd narrative, when present.
        raw: The manifest verbatim, for fields this dataclass has not
            grown a name for yet.
    """

    build_id: str
    pack_dir: Path
    manifest_path: Path | None = None
    feature_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None
    timed_out: bool | None = None
    exit_code: int | None = None
    wedged: bool | None = None
    worktree_path: str | None = None
    branch: str | None = None
    failed_at: str | None = None
    receipt_families_exported: tuple[str, ...] = ()
    semantic_state_at_kill: Mapping[str, Any] | None = None
    resume: Mapping[str, Any] | None = None
    present_families: tuple[str, ...] = ()
    stdout_log: Path | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_manifest(self) -> bool:
        """``True`` when a machine-readable index was found and parsed."""
        return self.manifest_path is not None

    def to_context(self) -> dict[str, Any]:
        """Render the pack as the plain mapping the fix task is handed.

        Keys are stable and JSON-safe so the mapping can ride a dispatch
        payload or a stage_log row verbatim.
        """
        return {
            "build_id": self.build_id,
            "pack_dir": str(self.pack_dir),
            "manifest_path": (
                str(self.manifest_path) if self.manifest_path is not None else None
            ),
            "feature_id": self.feature_id,
            "correlation_id": self.correlation_id,
            "reason": self.reason,
            "timed_out": self.timed_out,
            "exit_code": self.exit_code,
            "wedged": self.wedged,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "failed_at": self.failed_at,
            "receipt_families_exported": list(self.receipt_families_exported),
            "present_families": list(self.present_families),
            "stdout_log": (
                str(self.stdout_log) if self.stdout_log is not None else None
            ),
        }


def read_failure_pack(
    build_id: str,
    *,
    receipts_root: "Path | str | None" = None,
) -> FailurePackIndex | None:
    """Read the failure pack a failed build left, or ``None``.

    ``None`` means "no pack directory" — the honest answer when the
    failed build predates FEAT-DRF, when the receipts root is on another
    box, or when the pack was pruned. A pack directory that exists but
    carries no readable manifest still returns an index (with
    ``has_manifest`` ``False``): the receipt families on disk are worth
    handing to the reviewer even when the index is missing.

    Never raises.

    Args:
        build_id: The FAILED build whose pack to read (not the fix
            journey's own build id).
        receipts_root: Injectable root; see
            :func:`fix_journey_receipts_root`.
    """
    from forge.subagents.autobuild_runner import (
        _RECEIPT_FAMILIES,
        FAILURE_MANIFEST_NAME,
        STDOUT_LOG_NAME,
    )

    try:
        pack_dir = fix_journey_receipts_root(receipts_root) / build_id
        if not pack_dir.is_dir():
            logger.info(
                "fix_journey_receipts: no failure pack for build_id=%s at %s",
                build_id,
                pack_dir,
            )
            return None

        present = tuple(
            family for family in _RECEIPT_FAMILIES if (pack_dir / family).is_dir()
        )
        stdout_log = pack_dir / STDOUT_LOG_NAME
        manifest_path = pack_dir / FAILURE_MANIFEST_NAME

        raw: dict[str, Any] = {}
        parsed_manifest: Path | None = None
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
                    parsed_manifest = manifest_path
                else:  # pragma: no cover - defensive
                    logger.warning(
                        "fix_journey_receipts: %s is not a JSON object; "
                        "reading the pack without an index",
                        manifest_path,
                    )
            except (OSError, ValueError) as exc:
                logger.warning(
                    "fix_journey_receipts: could not parse %s (%s: %s); "
                    "reading the pack without an index",
                    manifest_path,
                    type(exc).__name__,
                    exc,
                )

        exported = raw.get("receipt_families_exported")
        exported_tuple = (
            tuple(str(item) for item in exported)
            if isinstance(exported, (list, tuple))
            else ()
        )
        return FailurePackIndex(
            build_id=build_id,
            pack_dir=pack_dir,
            manifest_path=parsed_manifest,
            feature_id=_opt_str(raw.get("feature_id")),
            correlation_id=_opt_str(raw.get("correlation_id")),
            reason=_opt_str(raw.get("reason")),
            timed_out=_opt_bool(raw.get("timed_out")),
            exit_code=_opt_int(raw.get("exit_code")),
            wedged=_opt_bool(raw.get("wedged")),
            worktree_path=_opt_str(raw.get("worktree_path")),
            branch=_opt_str(raw.get("branch")),
            failed_at=_opt_str(raw.get("failed_at")),
            receipt_families_exported=exported_tuple,
            semantic_state_at_kill=(
                raw.get("semantic_state_at_kill")
                if isinstance(raw.get("semantic_state_at_kill"), dict)
                else None
            ),
            resume=(raw.get("resume") if isinstance(raw.get("resume"), dict) else None),
            present_families=present,
            stdout_log=stdout_log if stdout_log.is_file() else None,
            raw=raw,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort: never block a journey
        logger.warning(
            "fix_journey_receipts: failure-pack read FAILED for %s (%s: %s) — "
            "the fix journey continues without it",
            build_id,
            type(exc).__name__,
            exc,
        )
        return None


def _opt_str(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int)) and value != "" else None


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# As output — the per-stage receipts every fix-journey stage leaves
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageReceiptExport:
    """Outcome of one per-stage receipts export.

    Attributes:
        ok: ``False`` only on a real copy failure (logged at WARNING).
            An export of *nothing* is still a success — a stage that
            produced no receipt families is a legitimate state.
        stage_key: The ``NNN-stage`` directory name that was written.
        dest: Where the receipts landed.
        families: The families THIS export actually copied — never the
            destination read back (the 07-30 coach finding: reading the
            destination falsely claims an earlier run's families).
    """

    ok: bool
    stage_key: str
    dest: Path
    families: tuple[str, ...] = ()


def next_stage_key(
    build_id: str,
    stage: str,
    *,
    receipts_root: "Path | str | None" = None,
    suffix: str | None = None,
) -> str:
    """Return the next ``NNN-stage`` directory name for ``build_id``.

    A fix journey dispatches the SAME stage repeatedly — an initial
    ``/task-review``, N ``/task-work`` siblings, one follow-up
    ``/task-review``. Keying the export by stage name alone would make
    each cycle overwrite the last, which is exactly the receipts loss
    this fold exists to prevent. The monotonically-increasing prefix is
    derived from what is already on disk, so the numbering survives a
    daemon restart mid-journey.

    Never raises; an unreadable directory yields ``001-<stage>``.
    """
    tail = f"{stage}-{suffix}" if suffix else stage
    try:
        stages_dir = fix_journey_receipts_root(receipts_root) / build_id / STAGES_DIRNAME
        highest = 0
        if stages_dir.is_dir():
            for child in stages_dir.iterdir():
                match = _STAGE_DIR_RE.match(child.name)
                if match is None:
                    continue
                highest = max(highest, int(match.group("seq")))
        return f"{highest + 1:03d}-{tail}"
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning(
            "fix_journey_receipts: could not scan stage receipts for %s (%s); "
            "starting the sequence at 001",
            build_id,
            exc,
        )
        return f"001-{tail}"


def export_stage_receipts(
    *,
    build_id: str,
    stage: str,
    worktree_path: "Path | str | None",
    receipts_root: "Path | str | None" = None,
    stage_key: str | None = None,
    suffix: str | None = None,
    extra_files: "Mapping[str, str] | None" = None,
) -> StageReceiptExport:
    """Export one fix-journey stage's receipts, hardened to the owner.

    Copies the routine path's :data:`_RECEIPT_FAMILIES` out of
    ``worktree_path`` into
    ``<root>/<build_id>/stages/<NNN>-<stage>/``, preserving the relative
    layout, then applies the 0700/0600 hardening to the whole build pack.

    ``extra_files`` writes small text artefacts (a rationale, a dispatch
    result summary) alongside the copied families — the seam the driver
    loop uses to record *why* a stage ended where it did without inventing
    a second receipts location.

    Never raises: a copy failure is logged and reported through
    :attr:`StageReceiptExport.ok`, never propagated into the journey.
    """
    from forge.subagents.autobuild_runner import _RECEIPT_FAMILIES

    key = stage_key or next_stage_key(
        build_id, stage, receipts_root=receipts_root, suffix=suffix
    )
    families: list[str] = []
    try:
        pack_root = fix_journey_receipts_root(receipts_root) / build_id
        dest_root = pack_root / STAGES_DIRNAME / key
        dest_root.mkdir(parents=True, exist_ok=True)

        if worktree_path is not None:
            src_root = Path(worktree_path)
            for family in _RECEIPT_FAMILIES:
                src = src_root / family
                if not src.is_dir():
                    continue
                dest = dest_root / family
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest, dirs_exist_ok=True)
                families.append(family)

        for name, text in (extra_files or {}).items():
            (dest_root / name).write_text(text, encoding="utf-8")

        _harden(pack_root)
        logger.info(
            "fix_journey_receipts: stage receipts exported for %s -> %s (%s)",
            build_id,
            dest_root,
            ", ".join(families) if families else "no receipt families",
        )
        return StageReceiptExport(
            ok=True, stage_key=key, dest=dest_root, families=tuple(families)
        )
    except Exception as exc:  # noqa: BLE001 — best-effort: never block a stage
        logger.warning(
            "fix_journey_receipts: stage receipts export FAILED for %s stage=%s "
            "(%s: %s) — the fix journey's outcome is unaffected",
            build_id,
            stage,
            type(exc).__name__,
            exc,
        )
        dest_fallback = (
            fix_journey_receipts_root(receipts_root) / build_id / STAGES_DIRNAME / key
        )
        return StageReceiptExport(
            ok=False, stage_key=key, dest=dest_fallback, families=tuple(families)
        )


def write_fix_journey_failure_pack(
    *,
    build_id: str,
    reason: str,
    outcome: str,
    feature_id: str | None = None,
    correlation_id: str | None = None,
    source_build_id: str | None = None,
    branch: str | None = None,
    worktree_path: "Path | str | None" = None,
    review_cycles: int | None = None,
    stage_keys: "tuple[str, ...] | list[str] | None" = None,
    receipts_root: "Path | str | None" = None,
    clock: "Any | None" = None,
) -> Path | None:
    """Write the fix journey's OWN failure pack index.

    "A fix journey that FAILS leaves its own failure pack" (design pass
    §b.2). The filename is the routine path's
    ``failure-manifest.json`` so every existing diagnoser finds it; the
    ``pack_kind`` key (:data:`FIX_JOURNEY_PACK_KIND`) tells a reader that
    what failed here was the *repair attempt*, and ``source_build_id``
    points back at the build it was trying to repair.

    An earlier manifest under a reused ``build_id`` is archived aside by
    the routine path's own archiver, never destroyed.

    Returns the manifest path, or ``None`` when the write failed (logged
    at WARNING — the journey's outcome is unaffected either way).
    """
    from forge.subagents.autobuild_runner import (
        FAILURE_MANIFEST_NAME,
        _archive_prior_manifest,
    )

    try:
        pack_root = fix_journey_receipts_root(receipts_root) / build_id
        pack_root.mkdir(parents=True, exist_ok=True)
        manifest_path = pack_root / FAILURE_MANIFEST_NAME
        if manifest_path.exists():
            _archive_prior_manifest(manifest_path)

        now = clock() if clock is not None else datetime.now(timezone.utc)
        manifest = {
            "pack_kind": FIX_JOURNEY_PACK_KIND,
            "build_id": build_id,
            "feature_id": feature_id,
            "correlation_id": correlation_id,
            # The build this fix journey was trying to repair — the
            # cross-pack pointer a diagnoser needs to read both halves.
            "source_build_id": source_build_id,
            "reason": reason,
            "outcome": outcome,
            "branch": branch,
            "worktree_path": (
                str(worktree_path) if worktree_path is not None else None
            ),
            "review_cycles": review_cycles,
            "stage_receipts": list(stage_keys or ()),
            "failed_at": now.isoformat(),
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        _harden(pack_root)
        logger.info(
            "fix_journey_receipts: fix-journey failure pack written for %s -> %s",
            build_id,
            manifest_path,
        )
        return manifest_path
    except Exception as exc:  # noqa: BLE001 — best-effort: never block a terminal
        logger.warning(
            "fix_journey_receipts: fix-journey failure pack NOT written for %s "
            "(%s: %s) — the journey's outcome is unaffected",
            build_id,
            type(exc).__name__,
            exc,
        )
        return None
