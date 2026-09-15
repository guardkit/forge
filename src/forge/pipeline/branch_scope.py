"""Read a finished build's branch: what it changed, what it added, what its
plan said it would touch.

WHY THIS EXISTS (the planner fix, 2026-09-15). The merge card Rich taps on a
feature says how many tasks passed and nothing about scope. One sentence went
through the factory twelve times and something nobody asked for turned up in
ten of the twelve plans — a five-module package here, a database migration
there, the web address moved in three. None of it was visible at the moment
the merge word was asked for. This module is the reading half of the cure: it
asks git three questions about the build's own branch and hands the answers,
unjudged, to :mod:`forge.pipeline.scope_report`, which does the comparing.

The three questions, all read-only, all fixed argument lists, no shell:

* **what this branch changed** — ``diff --name-status -M -z <base>...<head>``,
  the three-dot form, so the answer is what THIS branch did and never what
  main has moved on to since;
* **what this branch added** — ``diff -U0 <base>...<head>``, kept down to the
  lines the branch adds and kept UNDER THE FILE each line was added to,
  because a web address the request never named and a capability nobody asked
  for are both things the branch WROTE, and the same words mean different
  things in a test file and in the code that was asked for;
* **what the plan of record said** — the feature's own file
  (``.guardkit/features/<feature id>.yaml``) read at the branch's head, and
  every task document it names, so the files the plan declared can be
  compared with the files the build actually changed.

WHY IT IS NOT THE FIX JOURNEY'S READER. There is already a reader for what a
branch changed (``read_branch_changes`` in the conductor), and it cannot
answer this: it reads a fix journey's own worktree against that journey's
base, and the sandbox route behind it refuses any path that is not
``<repo>/.forge/worktrees/<leaf>``, with the span fixed at ``<base>...HEAD``.
A routine build needs ``main...autobuild/FEAT-XXXX`` in the clone root, which
that route cannot express, and every routine build's ``worktree_path`` column
is empty anyway. So this is one more git question rather than a widening of
that one, and the fix journey's reader is left exactly as it is.

SANDBOX FIRST. For a repository whose factory lives in a sandbox, the branch
is in the clone inside that sandbox and not in the copy on this side, so the
same reading is asked of that sandbox's deploy sidecar over HTTP. The work
itself is this module's own function, imported by the sidecar, so the two
sides cannot drift apart.

Nothing here raises. Anything that stops the reading comes back as an
``error`` sentence, and the scope pass then publishes "this could not be read"
rather than a count of nothing.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "BRANCH_SCOPE_LIMIT_BYTES",
    "BRANCH_SCOPE_TIMEOUT_SECONDS",
    "BranchScopeReading",
    "added_lines_by_file",
    "added_lines_of",
    "feature_file_path",
    "plan_document_paths",
    "read_branch_scope",
    "read_branch_scope_in_sandbox",
    "reading_from_answer",
    "reading_to_answer",
]

#: How long any one git question may take. A diff over a long branch is real
#: work; this is still far short of anything a person waits for.
BRANCH_SCOPE_TIMEOUT_SECONDS: float = 120.0

#: How much of either reading is carried: 512 KiB, the same limit the fix
#: journey's branch reading uses. Past it the answer SAYS it was cut, because
#: a count taken over half a diff is not a count.
BRANCH_SCOPE_LIMIT_BYTES: int = 512 * 1024

#: How many task documents one feature's plan may name. Five is the most any
#: of the twelve measured plans had; this is a guard against a runaway file,
#: not a real limit.
MAX_PLAN_DOCUMENTS: int = 64

#: How the plan of record names each task document inside the feature's file.
_FILE_PATH_LINE = re.compile(r'^\s*file_path:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)

#: How many git questions the sandbox reading asks, for its own time limit.
SANDBOX_BRANCH_SCOPE_GIT_CALLS: int = 4

#: Room for the HTTP round trip on top of the git questions themselves.
SANDBOX_BRANCH_SCOPE_HTTP_MARGIN_S: float = 15.0


@dataclass(frozen=True)
class BranchScopeReading:
    """What git said, and nothing judged.

    ``error`` is the one field that stops the scope pass: a sentence in it
    means the branch could not be read here, which the card says plainly and
    never dresses up as "this build changed nothing".
    """

    #: ``diff --name-status -M -z`` output, verbatim.
    name_status: str = ""
    #: ``{repository-relative path: the lines this branch adds to that file}``,
    #: each line without its leading ``+``. The file a line came from is kept
    #: because it changes what the line means: ``import logging`` in a test
    #: file is a test getting itself ready, not this build adding logging, and
    #: reading the two the same way put false sentences on the merge card.
    added_by_file: dict[str, str] = field(default_factory=dict)
    #: False when the added lines were cut short, so nothing may be counted
    #: from them.
    added_lines_read_whole: bool = False
    #: The feature's own file at the branch's head, or the empty string.
    feature_file: str = ""
    #: ``{repository-relative path: the document's text}`` for every task
    #: document the feature file names and git could read.
    plan_documents: dict[str, str] = field(default_factory=dict)
    #: The commit that was read, when git could say.
    head_sha: str | None = None
    #: One plain sentence when the branch could not be read at all.
    error: str | None = None

    @property
    def added_lines(self) -> str:
        """Every line this branch adds, whatever file it came from.

        The web-address half of the scope pass reads this, because a web
        address is judged by the string itself. Anything that has to know
        which file a line is in reads :attr:`added_by_file` instead.
        """
        return "\n".join(text for text in self.added_by_file.values() if text)


def feature_file_path(feature_id: str) -> str:
    """Where the plan of record keeps this feature's own file."""
    return f".guardkit/features/{str(feature_id).strip()}.yaml"


def plan_document_paths(feature_file: str) -> tuple[str, ...]:
    """The task documents a feature file names, in the order it names them.

    A mechanical read of the ``file_path:`` lines — no judgement, and no YAML
    parser, because this also runs inside the sidecar where the answer must
    stay a report on what git printed. Anything that is not a plain
    repository-relative path is dropped rather than passed to git.
    """
    found: list[str] = []
    for raw in _FILE_PATH_LINE.findall(str(feature_file or "")):
        path = raw.strip()
        if not path or path.startswith("/") or "\\" in path:
            continue
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            continue
        if path not in found:
            found.append(path)
        if len(found) >= MAX_PLAN_DOCUMENTS:
            break
    return tuple(found)


def added_lines_by_file(patch: str) -> dict[str, str]:
    """The lines a unified diff ADDS, kept under the file they were added to.

    The header care lives in one place already —
    :func:`forge.pipeline.merge_ready_checkpoint.iter_patch_lines` knows that
    ``+++ b/file`` is a header and that a ``+`` inside a hunk is content — so
    this reuses it rather than learning the same lesson a second time. It
    already says which file each line belongs to, and this keeps that answer:
    the same words mean different things in a test file and in the code the
    build was asked for.
    """
    from forge.pipeline.merge_ready_checkpoint import iter_patch_lines

    by_file: dict[str, list[str]] = {}
    for path, added, line in iter_patch_lines(patch):
        if added:
            by_file.setdefault(str(path or ""), []).append(line)
    return {path: "\n".join(lines) for path, lines in by_file.items()}


def added_lines_of(patch: str) -> str:
    """The lines a unified diff ADDS, one per line, without their ``+``.

    The whole of what the branch wrote, for the one question that does not
    care which file a line came from: whether the branch answers at a web
    address the request never named.
    """
    return "\n".join(
        text for text in added_lines_by_file(patch).values() if text
    )


def _git(repo_root: "Path | str", *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "-c", "core.quotepath=false", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=BRANCH_SCOPE_TIMEOUT_SECONDS,
        check=False,
    )


def read_branch_scope(
    *, repo_root: "Path | str", base: str, head: str, feature_id: str
) -> BranchScopeReading:
    """Read ``<base>...<head>`` in ``repo_root``, plus the plan behind it.

    ``base`` is normally ``main`` and ``head`` the build's own branch
    (``autobuild/FEAT-XXXX``). Never raises: a git that will not run, a
    branch that is not there, or a repository that is not a repository all
    come back as an ``error`` sentence.
    """
    try:
        names = _git(repo_root, "diff", "--name-status", "-M", "-z", f"{base}...{head}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return BranchScopeReading(
            error=(
                f"git could not be run in {repo_root} to read what {head} "
                f"changed against {base} ({type(exc).__name__}: {exc})"
            )
        )
    if names.returncode != 0:
        detail = (names.stderr or names.stdout or "").strip() or "<no output>"
        return BranchScopeReading(
            error=(
                f"git could not read what {head} changed against {base} in "
                f"{repo_root} (it exited {names.returncode}): {detail}"
            )
        )
    name_status = names.stdout or ""
    if len(name_status.encode("utf-8")) > BRANCH_SCOPE_LIMIT_BYTES:
        return BranchScopeReading(
            error=(
                f"what {head} changed against {base} in {repo_root} is too "
                "big to read whole, so the files it changed could not be "
                "counted"
            )
        )

    # WHAT THE BRANCH ADDED. This one never stops anything: it feeds the
    # comparison with the request, which is a report on a card, so a diff git
    # could not answer or one too long to carry is said plainly and carries
    # nothing.
    added: dict[str, str] = {}
    read_whole = False
    try:
        patch = _git(repo_root, "diff", "-U0", "--no-color", f"{base}...{head}")
        if patch.returncode == 0:
            body = patch.stdout or ""
            if len(body.encode("utf-8")) <= BRANCH_SCOPE_LIMIT_BYTES:
                added = added_lines_by_file(body)
                read_whole = True
            else:
                logger.info(
                    "the scope pass: what %s added against %s in %s is too "
                    "long to read whole, so nothing is counted from it",
                    head,
                    base,
                    repo_root,
                )
        else:
            logger.info(
                "the scope pass: git could not read what %s added against %s "
                "in %s (it exited %s), so nothing is counted from it",
                head,
                base,
                repo_root,
                patch.returncode,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info(
            "the scope pass: reading what %s added against %s in %s raised "
            "%s: %s, so nothing is counted from it",
            head,
            base,
            repo_root,
            type(exc).__name__,
            exc,
        )

    feature_file, documents = _read_the_plan(repo_root, head=head, feature_id=feature_id)

    sha: str | None = None
    try:
        resolved = _git(repo_root, "rev-parse", head)
        if resolved.returncode == 0:
            sha = (resolved.stdout or "").strip() or None
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover — git gone
        sha = None

    return BranchScopeReading(
        name_status=name_status,
        added_by_file=added,
        added_lines_read_whole=read_whole,
        feature_file=feature_file,
        plan_documents=documents,
        head_sha=sha,
    )


def _read_the_plan(
    repo_root: "Path | str", *, head: str, feature_id: str
) -> tuple[str, dict[str, str]]:
    """The feature's own file at ``head`` and every task document it names.

    A plan that cannot be read is an empty answer, not a failure: the scope
    pass then says the plan declared nothing to compare against, which is the
    truth about every plan written before task documents started declaring
    their files.
    """
    documents: dict[str, str] = {}
    try:
        shown = _git(repo_root, "show", f"{head}:{feature_file_path(feature_id)}")
    except (OSError, subprocess.TimeoutExpired):
        return "", documents
    if shown.returncode != 0:
        return "", documents
    feature_file = shown.stdout or ""
    for path in plan_document_paths(feature_file):
        try:
            document = _git(repo_root, "show", f"{head}:{path}")
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
            continue
        if document.returncode == 0:
            documents[path] = document.stdout or ""
    return feature_file, documents


def reading_to_answer(reading: BranchScopeReading) -> dict[str, Any]:
    """The reading as the sidecar's JSON answer."""
    return {
        "name_status": reading.name_status,
        "added_by_file": dict(reading.added_by_file),
        "added_lines_read_whole": reading.added_lines_read_whole,
        "feature_file": reading.feature_file,
        "plan_documents": dict(reading.plan_documents),
        "head": reading.head_sha,
        "error": reading.error,
    }


def reading_from_answer(answer: Any) -> BranchScopeReading:
    """The sidecar's JSON answer as a reading — tolerant of an older sidecar.

    A sidecar that has never heard of this question answers 404, which the
    caller turns into an error sentence before it gets here. One that answers
    without a field simply leaves that field empty, which reads as "nothing
    was counted from it".
    """
    if not isinstance(answer, dict):
        return BranchScopeReading(error="the sandbox answered something that was not a reading")
    error = answer.get("error")
    documents_raw = answer.get("plan_documents")
    documents = (
        {str(k): str(v) for k, v in documents_raw.items()}
        if isinstance(documents_raw, dict)
        else {}
    )
    added_raw = answer.get("added_by_file")
    added = (
        {str(k): str(v) for k, v in added_raw.items()}
        if isinstance(added_raw, dict)
        else {}
    )
    return BranchScopeReading(
        name_status=str(answer.get("name_status") or ""),
        added_by_file=added,
        # An answer that never said which file its lines came from is an
        # answer this side cannot judge, so it counts as not read at all
        # rather than as a branch that added nothing.
        added_lines_read_whole=bool(answer.get("added_lines_read_whole"))
        and isinstance(added_raw, dict),
        feature_file=str(answer.get("feature_file") or ""),
        plan_documents=documents,
        head_sha=str(answer["head"]) if answer.get("head") else None,
        error=str(error) if error else None,
    )


def read_branch_scope_in_sandbox(
    *,
    sandbox: Any,
    repo: str,
    base: str,
    head: str,
    feature_id: str,
    post: Callable[..., Any] | None = None,
) -> BranchScopeReading:
    """The same reading, run where the repository actually lives.

    "Sandbox first" (the estate's rule 89, 2026-09-07): a repository whose
    factory runs in a sandbox has its git in that sandbox, so the question is
    asked there rather than of whatever copy happens to be on this side.

    Same :class:`BranchScopeReading` contract as :func:`read_branch_scope`.
    A sidecar that cannot be reached, refuses, or has never heard of this
    question comes back as an ``error`` sentence, and the card then says the
    branch could not be read here.
    """
    from forge.deploy_sidecar.service import GIT_BRANCH_SCOPE_ROUTE
    from forge.planning.sidecar_git_runner import _urllib_post

    sender = post if post is not None else _urllib_post
    name = getattr(sandbox, "name", "?")
    url = f"{str(getattr(sandbox, 'sidecar_url', '')).rstrip('/')}{GIT_BRANCH_SCOPE_ROUTE}"
    body = {"repo": repo, "base": base, "head": head, "feature_id": feature_id}
    try:
        status, decoded = sender(
            url,
            body,
            BRANCH_SCOPE_TIMEOUT_SECONDS * SANDBOX_BRANCH_SCOPE_GIT_CALLS
            + SANDBOX_BRANCH_SCOPE_HTTP_MARGIN_S,
        )
    except Exception as exc:  # noqa: BLE001 — could not read is not "nothing"
        return BranchScopeReading(
            error=(
                f"the sidecar in sandbox {name} could not be reached at {url} "
                f"to read what {head} changed against {base}: "
                f"{type(exc).__name__}: {exc}"
            )
        )
    answer = decoded if isinstance(decoded, dict) else {}
    if status != 200:
        return BranchScopeReading(
            error=(
                f"the sidecar in sandbox {name} did not read what {head} "
                f"changed against {base} (HTTP {status}): "
                f"{answer.get('error') or answer}"
            )
        )
    return reading_from_answer(answer)
