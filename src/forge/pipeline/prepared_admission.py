"""Admitting a feature whose spec and plan were written elsewhere.

4 October 2026 (project initialisation design, Part 6). A feature planned
elsewhere (for example in Pi) is committed on its own branch and queued
through the normal build route — Jarvis's ``queue_build`` to Forge's build
queue — with no planning run behind it. Before this, such a build recorded no
start commit, target branch, memory name or launch settings, so it ran with
memory off and built whatever the branch said when the runner got to it.

Admission now establishes those facts itself, BEFORE the build row is
written, through the same git runner the planning door uses for the
repository (the sandbox's for a sandboxed project, the coordinator's
otherwise):

1. the project's remote ``origin`` is fetched and asked for its default
   branch (the integration target) and the commit the queued branch names
   (the source). That one commit is recorded as both ``start_commit`` and
   ``source_commit``: the build, its declarations, its deploy settings and its
   merge card all read the same revision, and a branch that moves after
   admission changes nothing;
2. the project's ``.guardkit/config.yaml`` is read AT THAT COMMIT with the
   planning door's own reader, for the memory name and the launch-setting
   names, refused in the planning door's own words;
3. the feature file must be at that commit and read as a plan with tasks,
   and each task's file must be there: without them there is nothing to
   build. (The checks on the rest of the supplied bundle — spec files,
   companion files, the plan's guide, binding documents, links, ids written
   with spaces, the leak-sweep manifest — went in the cull, 27 October
   2026.) No model is called.

Nothing here calls a planning capability, writes anything, or changes a
checked-out branch. Every answer is either the admitted facts or one plain
sentence; nothing raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from forge.planning.declared_memory import (
    read_declarations_at_commit,
    read_declared_reference,
)

__all__ = [
    "AdmittedBuild",
    "AdmissionAnswer",
    "FEATURE_YAML_DIR",
    "admit_prepared_build",
    "check_supplied_bundle",
    "feature_yaml_relpath",
]

#: Where GuardKit (and so the build runner) reads a feature's plan file.
FEATURE_YAML_DIR: str = ".guardkit/features"


def feature_yaml_relpath(feature_id: str) -> str:
    """The one feature file the runner builds: ``.guardkit/features/<id>.yaml``."""
    return f"{FEATURE_YAML_DIR}/{feature_id}.yaml"


@dataclass(frozen=True)
class AdmittedBuild:
    """What admission established for a build with no planning run.

    ``start_commit`` and ``source_commit`` are the same commit on purpose
    (design round 1, R1): one admitted revision for everything.
    """

    start_commit: str
    target_branch: str
    source_commit: str
    memory_project: str
    launch_settings: tuple[str, ...] = ()
    #: The project's ``reference.signed_off`` record at the admitted commit,
    #: or ``None`` when it declares none (17 October 2026, Codex
    #: implementation review R4). Kept with the build row, so the merge card
    #: knows a record was declared even if the commit cannot be read later.
    reference_signed_off: str | None = None


@dataclass(frozen=True)
class AdmissionAnswer:
    """The admitted facts, or the one sentence that says why not."""

    admitted: AdmittedBuild | None = None
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        return self.admitted is not None and self.refusal is None


async def admit_prepared_build(
    runner: Any,
    *,
    repo: str,
    repo_path: str,
    feature_id: str,
    branch: str,
) -> AdmissionAnswer:
    """Fetch, read the declarations and check the bundle, at one commit.

    ``runner`` is the repository's git runner (``fetch_remote_start_point``
    with a ``branch`` and ``read_file_at_commit``). Refuses exactly where the
    planning door refuses — no memory name, a reserved setting name, a remote
    that cannot be reached — and also for a branch the remote does not have
    and for the first item of the supplied bundle missing at that commit.
    """
    fetch = getattr(runner, "fetch_remote_start_point", None)
    if fetch is None:
        return AdmissionAnswer(
            refusal=(
                "the git runner wired for this factory cannot fetch a "
                "project's remote, so there is no way to admit the work at the "
                "commit that remote holds"
            )
        )
    try:
        start = await fetch(repo_path, branch)
    except Exception as exc:  # noqa: BLE001 — boundary, never crash dispatch
        return AdmissionAnswer(
            refusal=(
                f"the project's remote could not be read: "
                f"{type(exc).__name__}: {exc}"
            )
        )
    if start is None or not getattr(start, "ok", False):
        return AdmissionAnswer(
            refusal=str(
                getattr(start, "refusal", None)
                or "the project's remote gave no starting point and no reason"
            )
        )
    source = getattr(start, "branch_commit", None)
    if not source:
        return AdmissionAnswer(
            refusal=(
                f"the remote did not say which commit the branch '{branch}' "
                f"is at, so there is nothing to build"
            )
        )
    source = str(source)

    declarations = await read_declarations_at_commit(
        runner, repo=repo, repo_path=repo_path, commit=source
    )
    if not declarations.ok:
        return AdmissionAnswer(
            refusal=declarations.refusal
            or "the project's memory name could not be read"
        )

    missing = await check_supplied_bundle(
        runner,
        repo_path=repo_path,
        commit=source,
        feature_id=feature_id,
        config_text=declarations.content,
    )
    if missing is not None:
        return AdmissionAnswer(refusal=missing)
    # Already checked (and found well formed) by the bundle check above.
    reference, _why = read_declared_reference(declarations.content)

    return AdmissionAnswer(
        admitted=AdmittedBuild(
            start_commit=source,
            target_branch=str(start.branch),
            source_commit=source,
            memory_project=str(declarations.memory_project),
            launch_settings=tuple(declarations.launch_settings),
            reference_signed_off=reference.signed_off if reference.declared else None,
        )
    )


# ---------------------------------------------------------------------------
# The supplied bundle (design Part 6, point 3; review round 2 R3, round 3)
# ---------------------------------------------------------------------------


_ORDINARY_MODES = frozenset({"100644", "100755"})
_BUILT_PREFIX = "the prepared feature cannot be built: "


async def _raw(
    runner: Any, repo_path: str, commit: str, path: str, *, mode_only: bool = False
) -> tuple[Any, str | None]:
    """The raw committed read of one path: ``(answer, None)`` or ``(None, why)``.

    ``mode_only`` asks only whether the entry is there and what kind it is;
    nothing is read out of it or decoded (R7)."""
    read = getattr(runner, "read_file_at_commit", None)
    if read is None:
        return None, (
            "the git runner wired for this factory cannot read a file at a "
            "commit, so the supplied files cannot be checked"
        )
    try:
        if mode_only:
            answer = await read(repo_path, commit, path, raw=True, mode_only=True)
        else:
            answer = await read(repo_path, commit, path, raw=True)
    except Exception as exc:  # noqa: BLE001 — boundary
        return None, f"{path} could not be read at {commit}: {type(exc).__name__}: {exc}"
    refusal = getattr(answer, "refusal", None)
    if refusal:
        return None, str(refusal)
    if getattr(answer, "found", False) and not getattr(answer, "mode", None):
        return None, (
            f"{path} could not be read at {commit}: the reader did not say "
            f"what kind of file it is"
        )
    return answer, None


async def _read(
    runner: Any,
    repo_path: str,
    commit: str,
    path: str,
) -> tuple[str | None, str | None]:
    """``(text, None)`` for a committed file, ``(None, None)`` when absent,
    ``(None, why)`` when the reader could not answer."""
    answer, why = await _raw(runner, repo_path, commit, path)
    if why:
        return None, why
    if getattr(answer, "found", False) and getattr(answer, "mode", None) in _ORDINARY_MODES:
        content = getattr(answer, "content", None)
        return (content if isinstance(content, str) else ""), None
    return None, None


def _cannot(why: str) -> str:
    """The refusal for a read that could not be used, in plain words."""
    if why.startswith(_BUILT_PREFIX):
        return why
    return f"the prepared feature cannot be checked: {why}"


def _missing(what: str, path: str, commit: str) -> str:
    return (
        f"the prepared feature cannot be built: {what} {path} is not in the "
        f"commit {commit} it was supplied at. Commit it on the branch and queue "
        f"the build again."
    )


def _short(commit: str) -> str:
    return commit[:12]



def _yaml_path(raw: str) -> str | None:
    """A path the feature YAML names (a task's ``file_path``) as a path in the
    commit, or ``None`` when it is absolute or has a ``..`` component (no
    such path can be read out of a commit). Only ``.`` and empty components
    are dropped."""
    if raw.startswith("/") or raw.startswith("\\"):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


async def check_supplied_bundle(
    runner: Any,
    *,
    repo_path: str,
    commit: str,
    feature_id: str,
    config_text: str | None,
) -> str | None:
    """``None`` when there is something to build at ``commit``; else why not.

    The feature file must be there and read as a plan with tasks, and each
    task's file must be there. ``config_text`` is kept for the callers'
    signature.
    """
    del config_text
    at = _short(commit)

    yaml_path = feature_yaml_relpath(feature_id)
    text, why = await _read(runner, repo_path, commit, yaml_path)
    if why:
        return _cannot(why)
    if text is None:
        return _missing("its feature file", yaml_path, at)
    try:
        plan = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 — input, not code
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return f"the prepared feature cannot be built: {yaml_path} could not be read: {first}"
    if not isinstance(plan, dict):
        return f"the prepared feature cannot be built: {yaml_path} is not a feature plan"

    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return f"the prepared feature cannot be built: {yaml_path} names no tasks"
    for index, task in enumerate(tasks):
        file_path = task.get("file_path") if isinstance(task, dict) else None
        normal = _yaml_path(file_path) if isinstance(file_path, str) else None
        if normal is None:
            continue
        content, why = await _read(runner, repo_path, commit, normal)
        if why:
            return _cannot(why)
        if content is None:
            return _missing("the task file", normal, at)
    return None
