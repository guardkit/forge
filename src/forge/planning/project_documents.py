"""The project's own documents, read at one commit for the planning writers.

Project initialisation design, 4 October 2026, Part 3, and its paragraph "One
reading rule for every role". A project names the documents its builds are
held to in ``autobuild.player.required_documents`` (the list GuardKit's Player
already reads). The factory's spec writer and plan writer receive the same
documents, as text, read AT THE COMMIT THE WORK STARTS FROM, under the same
rule GuardKit applies for the Coach, so every role is given the same bytes:

1. *What is read, in order:* ``autobuild.player.instructions`` in declared
   order; then ``AGENTS.md``, ``CLAUDE.md``, ``.claude/CLAUDE.md`` when present
   and not already included; then ``required_documents`` in declared order.
   Nothing is read unless ``required_documents`` is present and non-empty, and
   then the ``autobuild.player`` block is held to GuardKit's allowed keys.
2. *Bytes:* the raw committed bytes, decoded strictly as UTF-8 (invalid UTF-8
   is refused), line endings kept; hash and size are of those bytes, and the
   :data:`PROJECT_DOCUMENTS_BUDGET_BYTES` budget is the sum of raw sizes after
   de-duplication.
3. *Links:* a ``required_documents`` entry must be an ordinary file; a link is
   refused. An instruction file may be a link: it is resolved inside the commit
   the way a filesystem would (each path component, linked folders included,
   chains of at most :data:`MAX_LINK_STEPS`); a target outside the repository,
   a missing target or a loop is refused. Files that come to one target are
   included once, under the first name.
4. *Failures:* an instruction file that is absent is simply not included —
   except one the project declared, which is refused; any other failure to read
   an included file refuses the run. Labels keep the declared spelling.

Every read is the shared committed-file reader's RAW read (``raw=True``), which
reports the tree entry's mode; a link is followed only when the reader says
the entry is one. Nothing here raises: the answer is the documents, or one
plain sentence.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from typing import Any

from forge.planning.declared_memory import (
    BINDING_DOCUMENTS_FIELD,
    DeclaredProjectDocuments,
)

__all__ = [
    "INSTRUCTION_FILES",
    "MAX_LINK_STEPS",
    "PROJECT_DOCUMENTS_BUDGET_BYTES",
    "ProjectDocument",
    "context_texts",
    "read_project_documents_at_commit",
]

#: The most project-document text, in bytes, given in full to a planning
#: writer: instruction files plus binding documents together (about 12,000
#: tokens). The same value GuardKit uses for the Coach.
PROJECT_DOCUMENTS_BUDGET_BYTES: int = 48 * 1024

#: The repository instruction files GuardKit's selector adds automatically,
#: in its order.
INSTRUCTION_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md")

#: The most symbolic links one instruction file's path may pass through.
MAX_LINK_STEPS: int = 8

_ORDINARY_MODES = frozenset({"100644", "100755"})
_LINK_MODE = "120000"
_FOLDER_MODE = "040000"
_INSTRUCTIONS_FIELD = "autobuild.player.instructions"


@dataclass(frozen=True)
class ProjectDocument:
    """One document as read at one commit: its label, hash, size and text.

    ``path`` is the name the document was included under, spelled as declared.
    ``bytes`` and ``sha256`` are of the committed bytes, which are exactly the
    text's UTF-8 encoding because the text was decoded strictly.
    """

    path: str
    sha256: str
    bytes: int
    commit: str
    text: str

    @classmethod
    def of(cls, path: str, text: str, commit: str) -> "ProjectDocument":
        data = text.encode("utf-8")
        return cls(
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
            commit=commit,
            text=text,
        )

    def receipt(self) -> dict[str, Any]:
        """What a receipt records: which file, which version, how big, where."""
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "commit": self.commit,
        }

    def to_record(self) -> dict[str, Any]:
        """The receipt plus the text, so a re-drive reuses what was read."""
        return {**self.receipt(), "text": self.text}

    @classmethod
    def from_record(cls, record: Any) -> "ProjectDocument | None":
        """A recorded document back, or ``None`` when the record does not hold
        together (a missing field, or text whose hash is not the one recorded)."""
        if not isinstance(record, dict):
            return None
        path, text, commit = record.get("path"), record.get("text"), record.get("commit")
        if not (isinstance(path, str) and isinstance(text, str) and isinstance(commit, str)):
            return None
        document = cls.of(path, text, commit)
        if document.sha256 != record.get("sha256") or document.bytes != record.get("bytes"):
            return None
        return document


def context_texts(documents: tuple[ProjectDocument, ...] | list[ProjectDocument]) -> list[str]:
    """The writers' ``context`` list: each text after one line naming its path."""
    return [f"File: {document.path}\n{document.text}" for document in documents]


class _Refused(Exception):
    """One plain sentence: why the documents cannot be given to the writers."""


class _Absent(Exception):
    """The path itself is simply not in the commit (no link led to the gap)."""


class _Reader:
    """The raw committed-file read, at one commit, refusing every failure.

    Answers are kept per path, so a path asked about twice is read once.
    """

    def __init__(self, runner: Any, repo_path: str, commit: str) -> None:
        self._read = getattr(runner, "read_file_at_commit", None)
        self._repo_path = repo_path
        self.commit = commit
        self.short = commit[:12]
        self._answers: dict[str, Any] = {}

    async def __call__(self, path: str) -> Any:
        if path in self._answers:
            return self._answers[path]
        if self._read is None:
            raise _Refused(
                "the git runner wired for this factory cannot read a file at a "
                "commit, so the project's documents cannot be read"
            )
        try:
            answer = await self._read(self._repo_path, self.commit, path, raw=True)
        except Exception as exc:  # noqa: BLE001 — boundary
            raise _Refused(
                f"{path} could not be read at {self.short}: "
                f"{type(exc).__name__}: {exc}"
            ) from None
        refusal = getattr(answer, "refusal", None)
        if refusal:
            raise _Refused(f"{path} could not be read at {self.short}: {refusal}")
        found = bool(getattr(answer, "found", False))
        mode = getattr(answer, "mode", None)
        if found and not mode:
            raise _Refused(
                f"{path} could not be read at {self.short}: the reader did not "
                f"say what kind of file it is or confirm its exact bytes"
            )
        if found and not isinstance(getattr(answer, "content", None), str):
            raise _Refused(f"{path} could not be read at {self.short}: no contents")
        self._answers[path] = answer
        return answer


def _is_entry(answer: Any) -> bool:
    """A file, link or submodule entry — anything but a folder or nothing."""
    return bool(answer.found) or (
        answer.mode is not None and answer.mode != _FOLDER_MODE
    )


async def _resolve_instruction(read: _Reader, spelling: str, path: str) -> str:
    """The repository path an instruction file resolves to, inside the commit.

    Exactly as GuardKit resolves it (``_resolve_committed_path``): component by
    component, ``.`` and empty components skipped, ``..`` taking the current
    folder's parent (refused at the top), a link at any component replaced by
    its target's components, at most :data:`MAX_LINK_STEPS` links. Raises
    :class:`_Absent` when a component of the path ITSELF is not there (or a
    file is used as a folder); refuses a link target outside the repository
    or missing, a loop, or a result that is not an ordinary file.
    """
    queue: deque[tuple[str, bool]] = deque((part, False) for part in path.split("/"))
    current: list[str] = []
    steps = 0
    while queue:
        part, from_link = queue.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            if not current:
                raise _Refused(
                    f"the instruction file {spelling} leads outside the "
                    f"repository at {read.short}"
                )
            current.pop()
            continue
        candidate = "/".join([*current, part])
        answer = await read(candidate)
        if answer.found and answer.mode == _LINK_MODE:
            steps += 1
            if steps > MAX_LINK_STEPS:
                raise _Refused(
                    f"the instruction file {spelling} passes through more than "
                    f"{MAX_LINK_STEPS} symbolic links at {read.short} (a loop, "
                    f"or a chain too long)"
                )
            target = str(answer.content)
            if target.startswith("/"):
                raise _Refused(
                    f"the instruction file {spelling} passes through "
                    f"{candidate}, a symbolic link to {target!r} outside the "
                    f"repository"
                )
            queue.extendleft(reversed([(token, True) for token in target.split("/")]))
            continue
        if _is_entry(answer):
            if any(token not in ("", ".") for token, _ in queue):
                raise _Absent(spelling)  # a file used as a folder: not there
            current.append(part)
            continue
        if answer.mode == _FOLDER_MODE:
            current.append(part)
            continue
        if from_link:
            raise _Refused(
                f"the instruction file {spelling} passes through a symbolic "
                f"link whose target ({candidate}) is not in the repository at "
                f"{read.short}"
            )
        raise _Absent(spelling)
    resolved = "/".join(current)
    final = await read(resolved) if resolved else None
    if final is None or not final.found or final.mode not in _ORDINARY_MODES:
        raise _Refused(
            f"the instruction file {spelling} is not an ordinary file at "
            f"{read.short}"
        )
    return resolved


async def _instruction_file(
    read: _Reader, spelling: str, path: str, *, declared: bool
) -> tuple[str, str] | None:
    """``(resolved path, text)``, or ``None`` when an undeclared instruction
    file is simply absent. A declared one that is absent is refused."""
    try:
        resolved = await _resolve_instruction(read, spelling, path)
    except _Absent:
        if declared:
            raise _Refused(
                f"the project declares {spelling} in {_INSTRUCTIONS_FIELD}, but "
                f"there is no such file at the commit this work starts from "
                f"({read.short}). Commit it, or take it off the list, then ask "
                f"again."
            ) from None
        return None
    answer = await read(resolved)
    return resolved, str(answer.content)


async def _binding_document(read: _Reader, spelling: str, path: str) -> str:
    """The text of one declared binding document, which must be an ordinary
    file at exactly that path — no link at any component, as GuardKit checks."""
    parts = path.split("/")
    answer = None if ".." in parts else await read(path)
    if answer is not None and answer.found and answer.mode in _ORDINARY_MODES:
        return str(answer.content)
    # Not an ordinary file there: say why, as GuardKit does — a link at any
    # component first, then nothing there, then some other kind of entry.
    for index in range(1, len(parts) + 1):
        prefix_parts = parts[:index]
        if ".." in prefix_parts:
            break
        prefix = "/".join(prefix_parts)
        step = answer if index == len(parts) else await read(prefix)
        if step is not None and step.found and step.mode == _LINK_MODE:
            raise _Refused(
                f"the project's binding document {spelling} (declared in "
                f"{BINDING_DOCUMENTS_FIELD}) cannot be used at the commit this "
                f"work starts from ({read.short}): {prefix} is a symbolic link; "
                f"a document the project's builds are held to must be the "
                f"file itself"
            )
        if step is None or not (step.found or step.mode is not None):
            break
    if answer is None or (not answer.found and answer.mode is None):
        raise _Refused(
            f"the project declares {spelling} in {BINDING_DOCUMENTS_FIELD}, but "
            f"there is no such file at the commit this work starts from "
            f"({read.short}). Commit it, or take it off the list, then ask again."
        )
    raise _Refused(
        f"the project's binding document {spelling} (declared in "
        f"{BINDING_DOCUMENTS_FIELD}) is not an ordinary file at {read.short} "
        f"(git mode {answer.mode})"
    )


async def read_project_documents_at_commit(
    runner: Any,
    *,
    repo_path: str,
    commit: str,
    declared: DeclaredProjectDocuments,
) -> tuple[tuple[ProjectDocument, ...], str | None]:
    """Read what ``declared`` names, by the one reading rule, at ``commit``.

    ``declared`` comes from
    :func:`~forge.planning.declared_memory.read_declared_project_documents`;
    no binding documents means nothing declared: nothing is read and
    ``((), None)`` comes back. Otherwise ``(documents, None)`` in the rule's
    order, or ``((), why)``.
    """
    if not declared.documents:
        return (), None
    read = _Reader(runner, repo_path, commit)
    included: list[tuple[str, str, str]] = []  # (label, resolved, text)
    try:
        for entry in declared.instructions:
            found = await _instruction_file(
                read, entry.spelling, entry.path, declared=True
            )
            if found is not None:
                included.append((entry.spelling, *found))
        listed = {entry.path for entry in declared.instructions}
        for name in INSTRUCTION_FILES:
            if name in listed:
                continue
            found = await _instruction_file(read, name, name, declared=False)
            if found is not None:
                included.append((name, *found))
        for entry in declared.documents:
            text = await _binding_document(read, entry.spelling, entry.path)
            included.append((entry.spelling, entry.path, text))
    except _Refused as refused:
        return (), str(refused)

    delivered: list[ProjectDocument] = []
    seen: set[str] = set()
    for label, resolved, text in included:
        if resolved in seen:
            continue
        seen.add(resolved)
        delivered.append(ProjectDocument.of(label, text, commit))

    total = sum(document.bytes for document in delivered)
    if total > PROJECT_DOCUMENTS_BUDGET_BYTES:
        sizes = ", ".join(f"{d.path} ({d.bytes} bytes)" for d in delivered)
        return (), (
            f"the project's instruction files and binding documents come to "
            f"{total} bytes, over the {PROJECT_DOCUMENTS_BUDGET_BYTES}-byte limit "
            f"for documents given to the spec and plan writers in full: {sizes}. "
            f"A binding document is never cut short; shorten the documents or "
            f"declare fewer in {BINDING_DOCUMENTS_FIELD}."
        )
    return tuple(delivered), None
