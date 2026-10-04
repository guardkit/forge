"""The project's own documents, read at one commit for the planning writers.

Project initialisation design, 4 October 2026, Part 3. A project names the
documents its builds are held to in ``autobuild.player.required_documents``
(the list GuardKit's Player already reads). The factory's spec writer and plan
writer now receive the same documents, as text, read AT THE COMMIT THE WORK
STARTS FROM — never the working folder, never a later commit — together with
the repository instruction files GuardKit adds automatically (``AGENTS.md``,
``CLAUDE.md``, ``.claude/CLAUDE.md``).

Opt-in, exactly as GuardKit's Coach delivery is: a project that declares no
binding documents has nothing read and nothing sent, not even its instruction
files, so its requests stay byte for byte what they were.

Rules, each refused before any model is asked anything:

* a declared document must be present at that commit and be an ordinary file —
  a symbolic link is refused by the shared committed-file reader's tree-mode
  check (``read_file_at_commit(..., ordinary_file_only=True)``), so every role
  reads the same bytes;
* the instruction files plus the documents must come to at most
  :data:`PROJECT_DOCUMENTS_BUDGET_BYTES`, the same figure GuardKit uses for the
  Coach. A binding document silently cut short is worse than a refusal.

An instruction file is optional. When it is a symbolic link it is followed
only to a target inside the repository at that commit, and skipped otherwise;
files that come to one target are delivered once, under the first name found.

Nothing here raises: the answer is the documents, or one plain sentence.
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass
from typing import Any

from forge.planning.declared_memory import BINDING_DOCUMENTS_FIELD

__all__ = [
    "INSTRUCTION_FILES",
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


@dataclass(frozen=True)
class ProjectDocument:
    """One document as read at one commit: its path, hash, size and text."""

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


async def _read(
    runner: Any, repo_path: str, commit: str, path: str, *, ordinary: bool
) -> tuple[Any, str | None]:
    """``(answer, None)`` or ``(None, why)`` — the reader asked once, never raising."""
    read = getattr(runner, "read_file_at_commit", None)
    if read is None:
        return None, (
            "the git runner wired for this factory cannot read a file at a "
            "commit, so the project's documents cannot be read"
        )
    try:
        if ordinary:
            answer = await read(repo_path, commit, path, ordinary_file_only=True)
        else:
            answer = await read(repo_path, commit, path)
    except Exception as exc:  # noqa: BLE001 — boundary
        return None, f"{path} could not be read at {commit}: {type(exc).__name__}: {exc}"
    return answer, None


def _content(answer: Any) -> str:
    content = getattr(answer, "content", None)
    return content if isinstance(content, str) else ""


def _inside(path: str) -> bool:
    return not (
        path.startswith("/") or path in ("", ".", "..") or path.startswith("../")
    )


async def _instruction_file(
    runner: Any, repo_path: str, commit: str, name: str
) -> tuple[str, str] | None:
    """``(resolved path, text)`` for one instruction file, or ``None`` to skip it.

    An ordinary file is read as it is. One whose ordinary-file read is refused
    but whose plain read finds it is a symbolic link stored in git (its plain
    read is the target's name): it is followed once, to an ordinary file inside
    the repository, and skipped otherwise.
    """
    answer, why = await _read(runner, repo_path, commit, name, ordinary=True)
    if why is not None or answer is None:
        return None
    if getattr(answer, "refusal", None) is None:
        if getattr(answer, "found", False):
            return name, _content(answer)
        return None
    plain, why = await _read(runner, repo_path, commit, name, ordinary=False)
    if (
        why is not None
        or plain is None
        or getattr(plain, "refusal", None)
        or not getattr(plain, "found", False)
    ):
        return None
    target = _content(plain).strip()
    if not target or "\n" in target or target.startswith("/"):
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if not _inside(resolved):
        return None
    followed, why = await _read(runner, repo_path, commit, resolved, ordinary=True)
    if (
        why is not None
        or followed is None
        or getattr(followed, "refusal", None)
        or not getattr(followed, "found", False)
    ):
        return None
    return resolved, _content(followed)


async def read_project_documents_at_commit(
    runner: Any,
    *,
    repo_path: str,
    commit: str,
    declared: tuple[str, ...],
) -> tuple[tuple[ProjectDocument, ...], str | None]:
    """Read the instruction files and the ``declared`` documents at ``commit``.

    ``declared`` is the project's binding-document list, already parsed (by
    :func:`~forge.planning.declared_memory.read_declared_binding_documents`).
    Empty means nothing declared: nothing is read and ``((), None)`` comes back.

    Returns ``(documents, None)`` — instruction files first, then the declared
    documents in their declared order — or ``((), why)``.
    """
    if not declared:
        return (), None
    short = commit[:12]

    # The declared documents are read FIRST: each must be an ordinary file, and
    # a helper too old to confirm that refuses here, before any instruction
    # file's link check could be misled by it.
    documents: dict[str, str] = {}
    for path in declared:
        answer, why = await _read(runner, repo_path, commit, path, ordinary=True)
        if why is None and answer is not None:
            why = getattr(answer, "refusal", None)
        if why:
            return (), (
                f"the project's binding document {path} (declared in "
                f"{BINDING_DOCUMENTS_FIELD}) cannot be used at the commit this "
                f"work starts from ({short}): {why}"
            )
        if not getattr(answer, "found", False):
            return (), (
                f"the project declares {path} in {BINDING_DOCUMENTS_FIELD}, but "
                f"there is no such file at the commit this work starts from "
                f"({short}). Commit it, or take it off the list, then ask again."
            )
        documents[path] = _content(answer)

    delivered: list[ProjectDocument] = []
    seen: set[str] = set()
    for name in INSTRUCTION_FILES:
        if name in documents:
            # Declared as binding too: held to the document rules (read above),
            # delivered in the instruction files' place.
            resolved, text = name, documents[name]
        else:
            found = await _instruction_file(runner, repo_path, commit, name)
            if found is None:
                continue
            resolved, text = found
        if resolved in seen:
            continue
        seen.add(resolved)
        delivered.append(ProjectDocument.of(name, text, commit))
    for path, text in documents.items():
        if path in seen:
            continue
        seen.add(path)
        delivered.append(ProjectDocument.of(path, text, commit))

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
