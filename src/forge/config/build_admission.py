"""Deterministic admission for work that will execute a project BUILD.

The publication setting ``builds_may_run_inside_the_coordinator`` predates
this module. Its compatibility default is ``True``. Consolidation sets it to
``False``; in that mode a build is admitted only when Forge can resolve one
registered repository identity and that registration has a usable sandbox
route. Planning, repository investigation and queue controls do not call this
helper because they are not BUILD admission.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

__all__ = [
    "BuildAdmissionDecision",
    "build_admission",
    "builds_require_sandbox",
]


@dataclass(frozen=True, slots=True)
class BuildAdmissionDecision:
    """The resolved repository and whether its build may proceed."""

    allowed: bool
    repo_key: str | None = None
    reason: str | None = None


def builds_require_sandbox(config: Any) -> bool:
    """Return whether the existing publication flag enables strict admission.

    Missing fields retain the historic ``True`` default, including the small
    stand-ins used by older callers and tests.
    """

    publication = getattr(config, "publication", None)
    return (
        getattr(publication, "builds_may_run_inside_the_coordinator", True)
        is False
    )


def _mapping(value: Any) -> dict[str, Any]:
    try:
        return dict(value or {})
    except (TypeError, ValueError):
        return {}


def _path_identity(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return os.path.abspath(os.path.normpath(str(Path(text).expanduser())))


def _route(entry: Any, field: str) -> str:
    value = getattr(entry, field, None)
    if value is None and isinstance(entry, Mapping):
        value = entry.get(field)
    return str(value or "").strip()


def _usable_web_route(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _refusal(repo: str, detail: str) -> BuildAdmissionDecision:
    return BuildAdmissionDecision(
        allowed=False,
        reason=f"sandbox-required: repository {repo} {detail}",
    )


def build_admission(
    config: Any,
    *,
    target_repo: Any = None,
    repo_path: Any = None,
    require_sidecar: bool = False,
) -> BuildAdmissionDecision:
    """Resolve and enforce the shared BUILD sandbox policy.

    ``target_repo`` may be the declared canonical key or a checkout path.
    ``repo_path`` is an optional explicit checkout path supplied by CLI and
    repair callers. In strict mode every supplied identity must resolve to the
    same unique registration. Path resolution uses only
    ``planning.target_repo_paths``; it never invents an ``org/name`` from
    trailing path components.
    """

    if not builds_require_sandbox(config):
        key = str(target_repo or "").strip() or None
        return BuildAdmissionDecision(allowed=True, repo_key=key)

    planning = getattr(config, "planning", None)
    paths = _mapping(getattr(planning, "target_repo_paths", None))
    sandboxes = _mapping(getattr(planning, "sandboxes", None))
    target = str(target_repo or "").strip()
    supplied_path = _path_identity(repo_path)

    candidates: set[str] = set()
    unresolved: list[str] = []
    ambiguous = False
    exact_target = target if target in paths else None

    if target:
        if exact_target is not None:
            candidates.add(target)
        else:
            target_path = _path_identity(target)
            matches = {
                str(key)
                for key, configured_path in paths.items()
                if target_path is not None
                and _path_identity(configured_path) == target_path
            }
            if len(matches) > 1:
                ambiguous = True
            elif matches:
                candidates.update(matches)
            else:
                unresolved.append(repr(target))

    if supplied_path is not None:
        if exact_target is not None:
            configured_path = _path_identity(paths[exact_target])
            if configured_path != supplied_path:
                unresolved.append(repr(str(repo_path)))
        else:
            matches = {
                str(key)
                for key, configured_path in paths.items()
                if _path_identity(configured_path) == supplied_path
            }
            if len(matches) > 1:
                ambiguous = True
            elif matches:
                candidates.update(matches)
            else:
                unresolved.append(repr(str(repo_path)))

    shown = repr(target or (str(repo_path) if repo_path is not None else "<missing>"))
    if ambiguous or len(candidates) > 1:
        return _refusal(
            shown, "has an ambiguous planning.target_repo_paths registration"
        )
    if unresolved and not candidates:
        return _refusal(shown, "is not registered in planning.target_repo_paths")
    if unresolved and candidates:
        return _refusal(
            shown, "does not resolve consistently to one registered identity"
        )
    if not candidates:
        return _refusal(shown, "is missing from BUILD admission")

    repo_key = next(iter(candidates))
    entry = sandboxes.get(repo_key)
    if entry is None:
        return _refusal(repr(repo_key), "has no registered sandbox")
    runner_url = _route(entry, "runner_url")
    if not _usable_web_route(runner_url):
        return _refusal(repr(repo_key), "has no usable sandbox runner route")
    if require_sidecar:
        sidecar_url = _route(entry, "sidecar_url")
        if not _usable_web_route(sidecar_url):
            return _refusal(repr(repo_key), "has no usable sandbox sidecar route")
    return BuildAdmissionDecision(allowed=True, repo_key=repo_key)
