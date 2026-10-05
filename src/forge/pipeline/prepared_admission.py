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
3. the supplied bundle — the feature file, its tasks, its spec files, the
   plan's guide, the project's binding documents, the files they link to and
   the QA files both producers write — must be present at that commit. The
   first missing item is named. No model is called.

Nothing here calls a planning capability, writes anything, or changes a
checked-out branch. Every answer is either the admitted facts or one plain
sentence; nothing raises.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import yaml

from forge.planning.declared_memory import (
    read_declarations_at_commit,
    read_declared_project_documents,
)
from forge.planning.project_documents import read_project_documents_at_commit

__all__ = [
    "AdmittedBuild",
    "AdmissionAnswer",
    "FEATURE_YAML_DIR",
    "admit_prepared_build",
    "check_supplied_bundle",
    "feature_yaml_relpath",
    "guide_claims_routes",
    "relative_markdown_links",
]

#: Where GuardKit (and so the build runner) reads a feature's plan file.
FEATURE_YAML_DIR: str = ".guardkit/features"

#: The plan's guide, written beside the task files by ``/feature-plan``.
GUIDE_NAME: str = "IMPLEMENTATION-GUIDE.md"

#: The leak-sweep manifest both producers write when the guide claims routes.
LEAK_SWEEP_PATH: str = "qa/leak-sweep.yaml"


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

    return AdmissionAnswer(
        admitted=AdmittedBuild(
            start_commit=source,
            target_branch=str(start.branch),
            source_commit=source,
            memory_project=str(declarations.memory_project),
            launch_settings=tuple(declarations.launch_settings),
        )
    )


# ---------------------------------------------------------------------------
# The supplied bundle (design Part 6, point 3; review round 2 R3, round 3)
# ---------------------------------------------------------------------------


async def _read(
    runner: Any,
    repo_path: str,
    commit: str,
    path: str,
) -> tuple[str | None, str | None]:
    """``(text, None)`` when present, ``(None, None)`` when absent, ``(None, why)``."""
    read = getattr(runner, "read_file_at_commit", None)
    if read is None:
        return None, (
            "the git runner wired for this factory cannot read a file at a "
            "commit, so the supplied files cannot be checked"
        )
    try:
        answer = await read(repo_path, commit, path)
    except Exception as exc:  # noqa: BLE001 — boundary
        return None, f"{path} could not be read at {commit}: {type(exc).__name__}: {exc}"
    refusal = getattr(answer, "refusal", None)
    if refusal:
        return None, str(refusal)
    if not getattr(answer, "found", False):
        return None, None
    content = getattr(answer, "content", None)
    return (content if isinstance(content, str) else ""), None


def _missing(what: str, path: str, commit: str) -> str:
    return (
        f"the prepared feature cannot be built: {what} {path} is not in the "
        f"commit {commit} it was supplied at. Commit it on the branch and queue "
        f"the build again."
    )


def _short(commit: str) -> str:
    return commit[:12]


_FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'(][^)]*)?\)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def relative_markdown_links(text: str) -> list[str]:
    """The relative link targets in Markdown ``text``, in order, once each.

    ``[text](path)`` and ``![alt](path)`` count; a URL (anything with a
    scheme, ``//host`` included), a bare ``#anchor`` and anything inside a code
    fence or back-quotes do not — task files back-quote paths of files still to
    be written. A ``#fragment`` or ``?query`` is dropped from the path.
    """
    stripped = _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))
    found: list[str] = []
    for match in _LINK_RE.finditer(stripped):
        target = match.group(1).strip()
        if not target or target.startswith("#") or target.startswith("//"):
            continue
        if _SCHEME_RE.match(target):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target:
            continue
        target = unquote(target)
        if target not in found:
            found.append(target)
    return found


def _resolve_link(from_file: str, target: str) -> str | None:
    """The repository path ``target`` names from ``from_file``, or None if outside."""
    if target.startswith("/"):
        joined = target.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(from_file), target)
    normal = posixpath.normpath(joined)
    if normal in ("", ".") or normal == ".." or normal.startswith("../"):
        return None
    return normal


#: The guide's Integration Contracts heading, read EXACTLY as the specialist
#: planner's own emitter reads it (specialist-agent
#: ``src/specialist_agent/qa/leak_sweep_emit.py``, ``_INTEGRATION_SECTION_RE``
#: and ``_extract_integration_section``): case-sensitive, ``## §4 Integration
#: Contracts`` or a bare ``## §4`` first, at any level of two or more.
#: GuardKit's ``/feature-plan`` template writes the heading with a colon,
#: ``## §4: Integration Contracts`` (installer/core/commands/feature-plan.md),
#: which neither reader recognises; that is noted for the producers' owners
#: and deliberately not papered over here.
_INTEGRATION_HEADING_RE = re.compile(
    r"^##+\s*(?:§\s*)?4\s*(?:Integration\s+Contracts?)?\s*$",
    re.MULTILINE,
)
#: Only when no such heading exists: ``## Integration Contracts`` (the
#: emitter's fallback, also case-sensitive).
_INTEGRATION_FALLBACK_RE = re.compile(
    r"^##+\s*Integration\s+Contracts?\s*$", re.MULTILINE
)
#: The section runs to the next heading of level two or more, as the emitter's.
_SECTION_RE = re.compile(r"^##+", re.MULTILINE)
#: The emitter's own route pattern (``_ROUTE_RE``, which is case-insensitive),
#: with a route that is only spaces not counted, as the emitter skips it.
_ROUTE_LINE_RE = re.compile(r"(?i)^\s*[-]?\s*route:\s*\S", re.MULTILINE)


def guide_claims_routes(guide_text: str) -> bool:
    """Does the guide's Integration Contracts section declare a ``route:``?

    Only the FIRST matching heading's section is read, exactly as the
    specialist emitter reads it; a ``route:`` line in it is that producer's own
    signal that it wrote ``qa/leak-sweep.yaml``.
    """
    heading = _INTEGRATION_HEADING_RE.search(guide_text)
    if heading is None:
        heading = _INTEGRATION_FALLBACK_RE.search(guide_text)
    if heading is None:
        return False
    rest = guide_text[heading.end():]
    following = _SECTION_RE.search(rest)
    body = rest[: following.start()] if following else rest
    return _ROUTE_LINE_RE.search(body) is not None


async def check_supplied_bundle(
    runner: Any,
    *,
    repo_path: str,
    commit: str,
    feature_id: str,
    config_text: str | None,
) -> str | None:
    """``None`` when the supplied bundle is all at ``commit``; else the first gap.

    In order: the feature file and its ``id``; each task's file; at least one
    spec file, each present (whatever ``routing_law`` says); the two companion
    files beside each spec file; the guide in each folder holding task files;
    the project's binding documents, as ordinary files; every relative
    Markdown link in the supplied summaries, guides and task files; a pass bar
    per task and a QA seed per spec file (whatever the tier-1 enforcement
    setting); and the leak-sweep manifest when the guide claims a route. The
    specialist-only ``<name>_digest.yaml`` is never required.
    """
    at = _short(commit)

    # 1. The feature file, the one the runner builds.
    yaml_path = feature_yaml_relpath(feature_id)
    text, why = await _read(runner, repo_path, commit, yaml_path)
    if why:
        return f"the prepared feature cannot be checked: {why}"
    if text is None:
        return _missing("its feature file", yaml_path, at)
    try:
        plan = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 — input, not code
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return f"the prepared feature cannot be built: {yaml_path} could not be read: {first}"
    if not isinstance(plan, dict):
        return f"the prepared feature cannot be built: {yaml_path} is not a feature plan"
    declared_id = plan.get("id")
    if str(declared_id or "").strip() != feature_id:
        return (
            f"the prepared feature cannot be built: {yaml_path} at {at} names "
            f"the feature {declared_id!r}, not {feature_id}"
        )

    # 2. Every task's file.
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return f"the prepared feature cannot be built: {yaml_path} names no tasks"
    task_files: list[str] = []
    task_ids: list[str] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            return f"the prepared feature cannot be built: task {index + 1} in {yaml_path} is not a task"
        task_id = str(task.get("id") or "").strip()
        file_path = task.get("file_path")
        if not task_id:
            return f"the prepared feature cannot be built: task {index + 1} in {yaml_path} has no id"
        if not isinstance(file_path, str) or not file_path.strip():
            return f"the prepared feature cannot be built: task {task_id} in {yaml_path} names no task file"
        normal = _resolve_link("", file_path.strip())
        if normal is None:
            return f"the prepared feature cannot be built: task {task_id}'s file {file_path} is outside the repository"
        task_ids.append(task_id)
        task_files.append(normal)
    texts: dict[str, str] = {}
    for path in task_files:
        content, why = await _read(runner, repo_path, commit, path)
        if why:
            return f"the prepared feature cannot be checked: {why}"
        if content is None:
            return _missing("the task file", path, at)
        texts[path] = content

    # 3. At least one spec file, each present — even with routing_law: off,
    #    because a prepared feature comes from /feature-spec, which always
    #    writes one.
    feature_files = plan.get("feature_files")
    if not isinstance(feature_files, list) or not [
        f for f in feature_files if isinstance(f, str) and f.strip()
    ]:
        return (
            f"the prepared feature cannot be built: {yaml_path} lists no spec "
            f"file (feature_files); a feature planned elsewhere is built "
            f"against the .feature its spec wrote"
        )
    specs: list[str] = []
    for raw in feature_files:
        if not isinstance(raw, str) or not raw.strip():
            continue
        normal = _resolve_link("", raw.strip())
        if normal is None:
            return f"the prepared feature cannot be built: the spec file {raw} is outside the repository"
        content, why = await _read(runner, repo_path, commit, normal)
        if why:
            return f"the prepared feature cannot be checked: {why}"
        if content is None:
            return _missing("the spec file", normal, at)
        specs.append(normal)

    # 4. The two companion files both producers write beside each spec file.
    summaries: list[str] = []
    for spec in specs:
        folder = posixpath.dirname(spec)
        name = posixpath.basename(spec)
        if name.endswith(".feature"):
            name = name[: -len(".feature")]
        for suffix, what in (
            ("_assumptions.yaml", "the spec's assumptions file"),
            ("_summary.md", "the spec's summary"),
        ):
            companion = posixpath.join(folder, f"{name}{suffix}")
            content, why = await _read(runner, repo_path, commit, companion)
            if why:
                return f"the prepared feature cannot be checked: {why}"
            if content is None:
                return _missing(what, companion, at)
            if suffix == "_summary.md":
                texts[companion] = content
                summaries.append(companion)

    # 5. The plan's guide, in each folder that holds task files.
    guides: list[str] = []
    for folder in dict.fromkeys(posixpath.dirname(p) for p in task_files):
        guide = posixpath.join(folder, GUIDE_NAME) if folder else GUIDE_NAME
        content, why = await _read(runner, repo_path, commit, guide)
        if why:
            return f"the prepared feature cannot be checked: {why}"
        if content is None:
            return _missing("the plan's guide", guide, at)
        texts[guide] = content
        guides.append(guide)

    # 6. The project's own documents, by the ONE reading rule the planning
    #    door and GuardKit's Coach use (4 October 2026): the same reader, so
    #    admission refuses exactly what planning and the Coach would refuse —
    #    declared instruction files present and resolvable, binding documents
    #    ordinary files of strict UTF-8 at their exact committed bytes, and the
    #    48 KiB budget. Opt-in: nothing is judged unless binding documents are
    #    declared.
    declared, why = read_declared_project_documents(config_text)
    if why:
        return f"the prepared feature cannot be built: {why}"
    if declared.documents:
        _documents, why = await read_project_documents_at_commit(
            runner, repo_path=repo_path, commit=commit, declared=declared
        )
        if why:
            return f"the prepared feature cannot be built: {why}"

    # 7. Referenced context: every relative Markdown link in the supplied
    #    summaries, guides and task files names a file at this commit.
    checked: set[str] = set()
    for source in [*summaries, *guides, *task_files]:
        for target in relative_markdown_links(texts.get(source, "")):
            resolved = _resolve_link(source, target)
            if resolved is None:
                return (
                    f"the prepared feature cannot be built: {source} links to "
                    f"{target}, which is outside the repository"
                )
            if resolved in checked:
                continue
            content, why = await _read(runner, repo_path, commit, resolved)
            if why:
                return f"the prepared feature cannot be checked: {why}"
            if content is None:
                return (
                    f"the prepared feature cannot be built: {source} links to "
                    f"{target}, and {resolved} is not in the commit {at} it was "
                    f"supplied at. Commit it on the branch, or correct the "
                    f"link, and queue the build again."
                )
            checked.add(resolved)

    # 8. The QA files both producers write, whatever the tier-1 enforcement
    #    setting: a pass bar per task and a seed per spec file.
    for task_id in task_ids:
        bar = f"qa/pass-bar-{task_id}.yaml"
        content, why = await _read(runner, repo_path, commit, bar)
        if why:
            return f"the prepared feature cannot be checked: {why}"
        if content is None:
            return _missing(f"the pass bar for {task_id}", bar, at)
    for spec in specs:
        name = posixpath.basename(spec)
        if name.endswith(".feature"):
            name = name[: -len(".feature")]
        seed = f"qa/pass-bar-seed-{name}.yaml"
        content, why = await _read(runner, repo_path, commit, seed)
        if why:
            return f"the prepared feature cannot be checked: {why}"
        if content is None:
            return _missing("the spec's QA seed", seed, at)

    # 9. The leak-sweep manifest, exactly when the guide claims a route.
    if any(guide_claims_routes(texts.get(guide, "")) for guide in guides):
        content, why = await _read(runner, repo_path, commit, LEAK_SWEEP_PATH)
        if why:
            return f"the prepared feature cannot be checked: {why}"
        if content is None:
            return (
                f"the prepared feature cannot be built: the plan's guide "
                f"declares a route under Integration Contracts, so "
                f"{LEAK_SWEEP_PATH} must be supplied too, and it is not in the "
                f"commit {at}. Commit it on the branch and queue the build "
                f"again."
            )

    return None
