"""Context manifest resolver for the GuardKit adapter (TASK-GCI-003 / DDR-005).

Reads ``.guardkit/context-manifest.yaml`` from a target repo, follows
dependency references up to a depth-2 cap, filters documents by the
per-subcommand allowed-category table, prepends
``internal_docs.always_include``, omits documents outside the filesystem
read allowlist, and returns the ordered ``--context <path>`` argument
list for a GuardKit invocation.

Per ``docs/design/decisions/DDR-005-cli-context-manifest-resolution.md``
and ``docs/design/contracts/API-subprocess.md`` §3.3.

Per ASSUM-007 the resolver is **stateless** — there is no module-level
cache, every call re-reads the manifests. Two concurrent calls against
the same ``repo_path`` produce independent :class:`ResolvedContext`
values.

The resolver intentionally raises :class:`KeyError` when the caller
passes a Graphiti GuardKit subcommand name. Per the BDD scenario
"Graphiti GuardKit subcommands skip context-manifest resolution
entirely" the caller (TASK-GCI-010) must skip resolution entirely
rather than calling this function — the ``KeyError`` is a *programmer
error*, not a runtime degradation.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from forge.adapters.guardkit.models import GuardKitWarning

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DDR-005 configuration constants
# ---------------------------------------------------------------------------


# DDR-005 §"Decision" — hardcoded category filter table. Nine entries; the
# Graphiti subcommands are *intentionally absent* — callers must not invoke
# the resolver for them at all.
_COMMAND_CATEGORY_FILTER: dict[str, set[str]] = {
    "system-arch": {"architecture", "decisions"},
    "system-design": {"specs", "decisions", "contracts", "architecture"},
    "system-plan": {"architecture", "decisions", "specs"},
    "feature-spec": {"specs", "contracts", "source", "decisions"},  # ASSUM-004
    "feature-plan": {"specs", "decisions", "architecture"},
    "task-review": {"contracts", "source"},
    "task-work": {"contracts", "source"},
    "task-complete": {"contracts", "source"},
    "autobuild": {"contracts", "source"},
    # Graphiti subcommands intentionally absent — caller must skip resolution.
}

# Stable category iteration order. Matches the order categories first
# appear in ``_COMMAND_CATEGORY_FILTER``'s value sets, which is the most
# natural reading of "categories in _COMMAND_CATEGORY_FILTER insertion
# order" given that the values are unordered ``set`` literals.
_CATEGORY_ORDER: tuple[str, ...] = (
    "architecture",
    "decisions",
    "specs",
    "contracts",
    "source",
)

_DEPTH_CAP = 2  # ASSUM-002 — manifest hops, not document count.
_MANIFEST_RELATIVE_PATH = Path(".guardkit") / "context-manifest.yaml"


# ---------------------------------------------------------------------------
# Result value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedContext:
    """Output of :func:`resolve_context_flags`.

    A frozen dataclass — the ``flags``, ``paths`` and ``warnings`` slots
    cannot be reassigned, but the lists themselves are mutable. The
    resolver returns fresh lists on every call (ASSUM-007).
    """

    flags: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    warnings: list[GuardKitWarning] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_context_flags(
    repo_path: Path,
    subcommand: str,
    read_allowlist: list[Path],
) -> ResolvedContext:
    """Assemble ``--context <path>`` flags for a GuardKit subcommand.

    Parameters
    ----------
    repo_path:
        Absolute or relative path to the target repository's root. Must
        contain (or be expected to contain) a ``.guardkit/context-manifest.yaml``.
    subcommand:
        The GuardKit subcommand the resolver is being invoked for (e.g.
        ``"feature-spec"``). Must be a key in
        :data:`_COMMAND_CATEGORY_FILTER` — Graphiti subcommands are
        *intentionally absent* and raise :class:`KeyError` per DDR-005.
    read_allowlist:
        Absolute paths the caller is permitted to read. Documents whose
        resolved absolute path is not under at least one allowlist entry
        are omitted with a structured warning.

    Returns
    -------
    ResolvedContext
        Ordered ``flags``/``paths`` plus any non-fatal ``warnings``.
        ``flags`` is always paired ``["--context", "<abs path>", ...]``;
        ``paths`` is the corresponding bare-path list.

    Raises
    ------
    KeyError
        If ``subcommand`` is not a key in :data:`_COMMAND_CATEGORY_FILTER`.
        Graphiti subcommands fall into this bucket — the caller (the
        ``guardkit_graphiti_*`` tools per TASK-GCI-010) must skip context
        resolution entirely rather than call this function.
    """
    if subcommand not in _COMMAND_CATEGORY_FILTER:
        raise KeyError(
            f"subcommand {subcommand!r} is not eligible for context "
            "resolution; Graphiti subcommands must skip resolution "
            "entirely (see DDR-005 / TASK-GCI-010)"
        )

    allowed_categories: set[str] = _COMMAND_CATEGORY_FILTER[subcommand]
    origin_root: Path = repo_path.resolve(strict=False)
    allowlist_resolved: list[Path] = [p.resolve(strict=False) for p in read_allowlist]

    warnings: list[GuardKitWarning] = []

    origin_manifest_path = origin_root / _MANIFEST_RELATIVE_PATH
    origin_manifest = _load_manifest(origin_manifest_path, warnings)
    if origin_manifest is None:
        # Missing-origin-manifest is the *only* case that emits a
        # ``context_manifest_missing`` warning per the BDD scenario.
        warnings.append(
            GuardKitWarning(
                code="context_manifest_missing",
                message=(
                    f"context manifest not found at {origin_manifest_path}; "
                    "proceeding without --context flags"
                ),
                details={"path": str(origin_manifest_path)},
            )
        )
        return ResolvedContext(flags=[], paths=[], warnings=warnings)

    # always_include is sourced solely from the *origin* manifest. It
    # is added regardless of category filter and prepended to every
    # other path in the result.
    always_include_paths: list[str] = []
    seen_paths: set[str] = set()
    internal_docs = origin_manifest.get("internal_docs") or {}
    for rel in internal_docs.get("always_include") or []:
        if not isinstance(rel, str):
            continue
        candidate = origin_root / rel
        ok, abs_path = _validate_doc_path(
            candidate=candidate,
            owning_repo_root=origin_root,
            allowlist=allowlist_resolved,
            warnings=warnings,
        )
        if ok and abs_path not in seen_paths:
            always_include_paths.append(abs_path)
            seen_paths.add(abs_path)

    # Bucket discovered docs by category so we can preserve a stable
    # category-major ordering across the BFS.
    docs_by_category: dict[str, list[str]] = {c: [] for c in _CATEGORY_ORDER}

    # BFS through the manifest dependency graph, tracking the *current
    # chain* (not a global visited set) so cycle detection is per-chain
    # — diamond imports stay legal, true cycles are broken safely.
    queue: deque[tuple[dict[str, Any], Path, frozenset[Path], int]] = deque()
    queue.append((origin_manifest, origin_root, frozenset({origin_manifest_path}), 0))

    while queue:
        manifest_data, manifest_root, chain, depth = queue.popleft()
        dependencies = manifest_data.get("dependencies") or {}
        if not isinstance(dependencies, dict):
            continue

        for dep_name, dep_info in dependencies.items():
            if not isinstance(dep_info, dict):
                continue
            dep_rel = dep_info.get("path")
            if not isinstance(dep_rel, str):
                continue
            dep_root = (manifest_root / dep_rel).resolve(strict=False)

            # Collect this dep entry's key_docs (filtered by category).
            for key_doc in dep_info.get("key_docs") or []:
                if not isinstance(key_doc, dict):
                    continue
                category = key_doc.get("category")
                doc_rel = key_doc.get("path")
                if (
                    not isinstance(category, str)
                    or not isinstance(doc_rel, str)
                    or category not in allowed_categories
                ):
                    continue
                candidate = dep_root / doc_rel
                ok, abs_path = _validate_doc_path(
                    candidate=candidate,
                    owning_repo_root=dep_root,
                    allowlist=allowlist_resolved,
                    warnings=warnings,
                )
                if not ok or abs_path in seen_paths:
                    continue
                docs_by_category.setdefault(category, []).append(abs_path)
                seen_paths.add(abs_path)

            # Recurse into the dependency's own manifest, subject to the
            # depth cap and per-chain cycle guard.
            dep_manifest_path = dep_root / _MANIFEST_RELATIVE_PATH
            if depth + 1 > _DEPTH_CAP:
                warnings.append(
                    GuardKitWarning(
                        code="context_manifest_cycle_detected",
                        message=(
                            "depth cap reached; not chasing dependency "
                            f"{dep_name!r} at {dep_manifest_path}"
                        ),
                        details={
                            "manifest": str(dep_manifest_path),
                            "reason": "depth_cap",
                            "depth_cap": _DEPTH_CAP,
                            "dependency": dep_name,
                        },
                    )
                )
                continue
            if dep_manifest_path in chain:
                warnings.append(
                    GuardKitWarning(
                        code="context_manifest_cycle_detected",
                        message=(
                            f"cycle detected: {dep_manifest_path} already in "
                            "current dependency chain"
                        ),
                        details={
                            "manifest": str(dep_manifest_path),
                            "reason": "cycle",
                            "dependency": dep_name,
                        },
                    )
                )
                continue
            dep_manifest = _load_manifest(dep_manifest_path, warnings)
            if dep_manifest is None:
                # A *transitive* dependency missing its manifest is
                # silent — only the origin's missing manifest emits the
                # ``context_manifest_missing`` warning per spec.
                continue
            queue.append(
                (
                    dep_manifest,
                    dep_root,
                    chain | {dep_manifest_path},
                    depth + 1,
                )
            )

    # Assemble final ordered paths: always_include first, then categories
    # in canonical order, then by manifest declaration order within each
    # category (preserved by ``docs_by_category[category]`` list order).
    ordered_paths: list[str] = list(always_include_paths)
    for category in _CATEGORY_ORDER:
        ordered_paths.extend(docs_by_category.get(category, []))

    flags: list[str] = []
    for path in ordered_paths:
        flags.extend(["--context", path])

    return ResolvedContext(flags=flags, paths=ordered_paths, warnings=warnings)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_manifest(
    manifest_path: Path,
    warnings: list[GuardKitWarning],
) -> dict[str, Any] | None:
    """Read and parse a manifest YAML file.

    Returns ``None`` if the file does not exist OR cannot be parsed
    (YAML errors degrade to a structured warning rather than raising).
    """
    if not manifest_path.is_file():
        return None
    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("failed to read manifest %s: %s", manifest_path, exc)
        warnings.append(
            GuardKitWarning(
                code="context_manifest_unreadable",
                message=f"could not parse manifest at {manifest_path}: {exc}",
                details={"path": str(manifest_path), "error": str(exc)},
            )
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def _validate_doc_path(
    candidate: Path,
    owning_repo_root: Path,
    allowlist: list[Path],
    warnings: list[GuardKitWarning],
) -> tuple[bool, str]:
    """Resolve ``candidate``, then validate it against the repo root and
    read allowlist.

    Symlinks are followed by ``Path.resolve(strict=False)`` *before* the
    allowlist check, so a symlink that points outside the allowlist is
    correctly rejected.

    Returns ``(ok, abs_path)`` where ``ok`` indicates the path is safe
    to include and ``abs_path`` is its resolved absolute string form
    (always populated, even on rejection — for warning detail).
    """
    resolved = candidate.resolve(strict=False)
    abs_path = str(resolved)

    if not _is_within(resolved, owning_repo_root):
        warnings.append(
            GuardKitWarning(
                code="context_manifest_path_outside_repo",
                message=(
                    f"manifest entry {abs_path} resolves outside its repo "
                    f"root {owning_repo_root}; omitted"
                ),
                details={
                    "path": abs_path,
                    "repo_root": str(owning_repo_root),
                },
            )
        )
        return False, abs_path

    if not any(_is_within(resolved, allowed) for allowed in allowlist):
        warnings.append(
            GuardKitWarning(
                code="context_manifest_path_outside_allowlist",
                message=(
                    f"manifest entry {abs_path} is outside the read "
                    "allowlist; omitted"
                ),
                details={
                    "path": abs_path,
                    "allowlist": [str(a) for a in allowlist],
                },
            )
        )
        return False, abs_path

    return True, abs_path


def _is_within(child: Path, parent: Path) -> bool:
    """Return ``True`` iff ``child`` is equal to or nested under ``parent``.

    Both paths must already be resolved/absolute. Uses
    :meth:`Path.is_relative_to` (Python 3.9+) which only inspects path
    components — no filesystem touch.
    """
    try:
        return child == parent or child.is_relative_to(parent)
    except ValueError:
        return False


__all__ = [
    "ResolvedContext",
    "resolve_context_flags",
]
