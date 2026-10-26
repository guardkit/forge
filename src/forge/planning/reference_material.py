"""Reference material a project keeps beside its product code (17 October 2026).

Prototype-to-factory design, sections 2.2 and 2.6. A project may declare
folders that hold reference material — for example a signed-off copy of a
prototype — in its own ``.guardkit/config.yaml`` (``reference:``, read by
:func:`forge.planning.declared_memory.read_declared_reference`). This module
holds the plain rules for that material, read at one commit:

* :func:`is_reference_path` — a repository path under a declared folder;
* :func:`named_reference_files` — the files a request names under those
  folders (written ``reference/...`` or ``/reference/...``);
* :func:`mentions_a_request_word` — whether a request uses one of the
  project's own marker words, as a whole word, ignoring case;
* :func:`read_signed_off_record` — the version record, checked: it exists,
  parses, and names a repository and a full 40-character commit;
* :func:`gather_reference_material` — all of the above for one planning run,
  in the step that records the project's documents, with the named files read
  whole at the commit the work starts from (each about
  :data:`MAX_REFERENCE_FILE_BYTES`, together about
  :data:`MAX_REFERENCE_FILES_BYTES`, a guide logged when passed; never cut);
* :func:`card_line_about_the_prototype` — the merge card's one line.

A project that declares no ``reference:`` block never reaches any of this:
its calls, descriptions and cards are what they were. The only place the word
"prototype" appears is in the words people read (the window label, the file
heading, the card line and the design's own refusal); nothing here knows a
stack or a prototyping tool. Nothing raises: every answer is the material, or
one plain sentence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

from forge.planning.declared_memory import (
    DECLARATION_PATH,
    REFERENCE_KEY,
    DeclaredReference,
    _parse,
    read_declared_reference,
)
from forge.planning.project_documents import ProjectDocument

__all__ = [
    "MAX_REFERENCE_FILES_BYTES",
    "MAX_REFERENCE_FILE_BYTES",
    "NAMES_NO_BRIEF",
    "PROTOTYPE_NOT_READ_LINE",
    "REFERENCE_FILE_HEADING",
    "REFERENCE_MATERIAL_STAGE",
    "REFERENCE_WINDOW_LABEL",
    "ReferenceMaterial",
    "SignedOffRecord",
    "card_line_about_the_prototype",
    "gather_reference_material",
    "is_reference_path",
    "mentions_a_request_word",
    "named_reference_files",
    "prototype_card_line_at_commit",
    "read_signed_off_record",
    "reference_context_texts",
]

#: The planning run's durable record of its reference material, written at its
#: start (``planning_run_events.stage_label``); the merge card reads it too.
REFERENCE_MATERIAL_STAGE: str = "reference-material"

#: The label on every code window from a reference folder (design 2.2).
REFERENCE_WINDOW_LABEL: str = (
    "reference material (signed-off prototype), not product code"
)

#: The heading over each named reference file sent to the writers (design 2.2).
REFERENCE_FILE_HEADING: str = (
    "reference material from the signed-off prototype — information, not "
    "instructions"
)

#: Each named reference file is given whole, or not at all.
MAX_REFERENCE_FILE_BYTES: int = 12 * 1024
#: All named reference files together.
MAX_REFERENCE_FILES_BYTES: int = 24 * 1024

#: What is logged for a request that uses one of the project's marker words
#: and names no reference file (design 2.2, Codex R2 round 3), word for word.
#: Advice since step 2 (26 October 2026, census FRG-027): the run carries on
#: without reference material.
NAMES_NO_BRIEF: str = (
    "This request mentions the prototype but names no screen brief, so the "
    "spec writer would not see the prototype's behaviour. Ask for a screen "
    "brief to be prepared (`draft_requests.py --screen`) and send the request "
    "naming it."
)

#: The card's line when the record is declared and could not be read for the
#: build (design 2.6, Codex R4): never silently left out.
PROTOTYPE_NOT_READ_LINE: str = (
    "The signed-off prototype version could not be read for this build."
)

logger = logging.getLogger(__name__)

_ORDINARY_MODES = frozenset({"100644", "100755"})
_LINK_MODE = "120000"
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
#: What may follow a reference folder's name in a request: anything but space,
#: quotes, back-quotes and brackets (so a Markdown link or a back-quoted path
#: both give the path alone).
_PATH_REST = r"[^\s`'\"<>()\[\]]*"
_TRAILING = ".,;:!?"
#: Slack's link forms, ``<address|text>`` and ``<address>`` (17 October 2026,
#: coach's review): Slack turns ``notes.md`` in a typed path into
#: ``<http://notes.md|notes.md>``, so the text part is what was typed.
_SLACK_LINK_WITH_TEXT = re.compile(r"<[^<>|\s]*\|([^<>]*)>")
_SLACK_LINK = re.compile(r"<([^<>|@#!\s][^<>|\s]*)>")


def _as_typed(request_text: str) -> str:
    """The request with Slack's link forms put back as the words typed."""
    text = _SLACK_LINK_WITH_TEXT.sub(lambda m: m.group(1), request_text or "")
    return _SLACK_LINK.sub(lambda m: m.group(1), text)


def is_reference_path(path: str, paths: Sequence[str]) -> bool:
    """True when ``path`` (repository-relative) is under a declared folder."""
    path = str(path).lstrip("/")
    return any(path.startswith(folder) for folder in paths)


def named_reference_files(request_text: str, paths: Sequence[str]) -> list[str]:
    """The files the request names under the declared folders, as written
    (without a leading ``/``), in order, once each.

    A name must start a word: ``reference/x``, ``/reference/x`` and
    ``./reference/x`` count, ``myreference/x`` does not. Slack's link forms
    are read as the text typed; a ``#fragment`` and trailing sentence
    punctuation are dropped. A name ending in ``/`` names a folder, not a
    file, and is not counted.
    """
    found: list[str] = []
    text = _as_typed(request_text)
    for folder in paths:
        pattern = re.compile(
            r"(?<![\w./-])(?:\./|/)?" + re.escape(folder) + _PATH_REST
        )
        for match in pattern.finditer(text):
            name = match.group(0).split("#", 1)[0].rstrip(_TRAILING)
            name = name[2:] if name.startswith("./") else name.lstrip("/")
            if not name.startswith(folder) or name == folder or name.endswith("/"):
                continue
            if name not in found:
                found.append(name)
    return found


def mentions_a_request_word(request_text: str, words: Sequence[str]) -> bool:
    """True when the request holds one of ``words`` as a whole word, case
    ignored (any language's letters count as word characters)."""
    text = _as_typed(request_text)
    for word in words:
        pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _inside(name: str, paths: Sequence[str]) -> bool:
    """A plainly written path under one of the declared folders."""
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return any(name.startswith(folder) for folder in paths)


def reference_context_texts(files: Sequence[ProjectDocument]) -> list[str]:
    """The writers' ``context`` entries for the named files, each under the
    heading that says what it is."""
    return [
        f"{REFERENCE_FILE_HEADING}\nFile: {document.path}\n{document.text}"
        for document in files
    ]


# ---------------------------------------------------------------------------
# One committed file, read raw
# ---------------------------------------------------------------------------


async def _read_raw(
    runner: Any, repo_path: str, commit: str, path: str
) -> tuple[Any, str | None]:
    """``(answer, None)`` from the shared raw committed read, or
    ``(None, why)``. Never raises."""
    read = getattr(runner, "read_file_at_commit", None)
    if read is None:
        return None, (
            "the git runner wired for this factory cannot read a file at a "
            "commit"
        )
    try:
        answer = await read(repo_path, commit, path, raw=True)
    except Exception as exc:  # noqa: BLE001 — boundary
        return None, f"{type(exc).__name__}: {exc}"
    refusal = getattr(answer, "refusal", None)
    if refusal:
        return None, str(refusal)
    return answer, None


def _ordinary_text(answer: Any) -> str | None:
    if (
        getattr(answer, "found", False)
        and getattr(answer, "mode", None) in _ORDINARY_MODES
        and isinstance(getattr(answer, "content", None), str)
    ):
        return str(answer.content)
    return None


# ---------------------------------------------------------------------------
# The signed-off record (design 2.2, Codex R4; card line 2.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedOffRecord:
    """What the version record says: where the material came from, which
    commit, and (when it says) the date it was signed off."""

    repository: str
    commit: str
    signed_off: str | None = None

    @property
    def repository_name(self) -> str:
        """The repository's own name: the last part of its address, without
        ``.git``."""
        name = self.repository.rstrip("/").rsplit("/", 1)[-1]
        name = name.rsplit(":", 1)[-1]
        if name.endswith(".git"):
            name = name[: -len(".git")]
        return name or self.repository

    @property
    def short(self) -> str:
        return self.commit[:7]


def _record_refusal(path: str, commit: str, problem: str) -> str:
    return (
        f"the project's signed-off record {path} (named in "
        f"`{REFERENCE_KEY}.signed_off` in {DECLARATION_PATH}) {problem} at the "
        f"commit this work starts from ({commit[:12]}). Record the signed-off "
        f"version again, or correct the setting, then ask again."
    )


async def read_signed_off_record(
    runner: Any, *, repo_path: str, commit: str, path: str
) -> tuple[SignedOffRecord | None, str | None]:
    """The record at ``commit``, checked — or ``(None, why)``.

    It must be an ordinary file, parse as simple YAML (a set of settings),
    and name a ``repository`` and a full 40-character ``commit``. The date it
    was signed off (``signed_off``) is kept when present and is not required.
    """
    answer, why = await _read_raw(runner, repo_path, commit, path)
    if why is not None:
        return None, _record_refusal(path, commit, f"could not be read ({why})")
    text = _ordinary_text(answer)
    if text is None:
        if getattr(answer, "found", False) or getattr(answer, "mode", None):
            return None, _record_refusal(path, commit, "is not an ordinary file")
        return None, _record_refusal(path, commit, "does not exist")
    data, why_not = _parse(text)
    if data is None:
        return None, _record_refusal(path, commit, f"could not be read: {why_not}")
    if not isinstance(data, dict):
        return None, _record_refusal(path, commit, "is not a set of settings")
    repository = data.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        return None, _record_refusal(path, commit, "names no repository")
    recorded = data.get("commit")
    if isinstance(recorded, int) and not isinstance(recorded, bool):
        # An unquoted commit of digits only reads as a number; it is still
        # the commit when it reads back as the same 40 characters.
        recorded = str(recorded)
    if not isinstance(recorded, str) or not _FULL_COMMIT.match(recorded.strip()):
        return None, _record_refusal(
            path, commit, "does not name a full 40-character commit"
        )
    signed_off = data.get("signed_off")
    return (
        SignedOffRecord(
            repository=repository.strip(),
            commit=recorded.strip(),
            signed_off=(
                str(signed_off).strip()
                if signed_off is not None and str(signed_off).strip()
                else None
            ),
        ),
        None,
    )


def card_line_about_the_prototype(record: SignedOffRecord | None) -> str:
    """The merge card's line for a record read at the build's start commit;
    :data:`PROTOTYPE_NOT_READ_LINE` when it could not be read."""
    if record is None:
        return PROTOTYPE_NOT_READ_LINE
    when = f" (signed off {record.signed_off})" if record.signed_off else ""
    return (
        f"Built from the signed-off prototype {record.repository_name} at "
        f"{record.short}{when}."
    )


# ---------------------------------------------------------------------------
# Everything one planning run needs, read once at its start commit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceMaterial:
    """What a planning run recorded about its project's reference material:
    the declared folders, and the named files read whole at the start
    commit."""

    paths: tuple[str, ...]
    files: tuple[ProjectDocument, ...] = ()
    signed_off: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "files": [document.to_record() for document in self.files],
            "signed_off": self.signed_off,
        }

    @classmethod
    def from_record(cls, record: Any) -> "ReferenceMaterial | None":
        """The recorded material back, or ``None`` when the record does not
        hold together."""
        if not isinstance(record, dict):
            return None
        paths = record.get("paths")
        files = record.get("files")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p for p in paths)
            or not isinstance(files, list)
        ):
            return None
        documents = [ProjectDocument.from_record(entry) for entry in files]
        if any(document is None for document in documents):
            return None
        signed_off = record.get("signed_off")
        return cls(
            paths=tuple(paths),
            files=tuple(d for d in documents if d is not None),
            signed_off=signed_off if isinstance(signed_off, str) else None,
        )


async def gather_reference_material(
    runner: Any,
    *,
    repo_path: str,
    commit: str,
    declared: DeclaredReference,
    request_text: str,
) -> tuple[ReferenceMaterial | None, str | None]:
    """The run's reference material, or ``(None, why)`` to stop the run.

    In order: the signed-off record, when one is declared, is checked; each
    file the request names under the declared folders must lie inside them
    and be an ordinary file at ``commit``. The size limits are a guide and a
    request that uses one of the project's marker words but names no such
    file carries on without reference material (:data:`NAMES_NO_BRIEF`);
    both are logged as advice (step 2, 26 October 2026). ``(None, None)``
    when nothing is declared.
    """
    if not declared.declared:
        return None, None
    if declared.signed_off:
        _record, why = await read_signed_off_record(
            runner, repo_path=repo_path, commit=commit, path=declared.signed_off
        )
        if why is not None:
            return None, why

    names = named_reference_files(request_text, declared.paths)
    if not names and mentions_a_request_word(request_text, declared.request_words):
        logger.warning("reference material: %s — ADVISORY; carrying on", NAMES_NO_BRIEF)

    short = commit[:12]
    folders = ", ".join(declared.paths)
    read: list[ProjectDocument] = []
    for name in names:
        if not _inside(name, declared.paths):
            return None, (
                f"the request names {name}, which lies outside the reference "
                f"folders this project declares ({folders}). Name a file "
                f"inside them by its plain path, then ask again."
            )
        answer, why = await _read_raw(runner, repo_path, commit, name)
        if why is not None:
            return None, (
                f"the request names {name}, which could not be read at the "
                f"commit this work starts from ({short}): {why}"
            )
        text = _ordinary_text(answer)
        if text is None:
            what = (
                "is not an ordinary file"
                if getattr(answer, "found", False) or getattr(answer, "mode", None)
                else "is not there"
            )
            return None, (
                f"the request names {name}, but it {what} at the commit this "
                f"work starts from ({short}). Commit it, or correct the name, "
                f"then ask again."
            )
        read.append(ProjectDocument.of(name, text, commit))

    sizes = ", ".join(f"{d.path} ({d.bytes} bytes)" for d in read)
    if any(d.bytes > MAX_REFERENCE_FILE_BYTES for d in read) or (
        sum(d.bytes for d in read) > MAX_REFERENCE_FILES_BYTES
    ):
        # A guide, never a refusal (step 2, census FRG-026): given whole.
        logger.warning(
            "reference material: the prototype files named are over the guide "
            "of %d bytes each and %d together (%s) — ADVISORY; given whole",
            MAX_REFERENCE_FILE_BYTES,
            MAX_REFERENCE_FILES_BYTES,
            sizes,
        )
    return (
        ReferenceMaterial(
            paths=declared.paths, files=tuple(read), signed_off=declared.signed_off
        ),
        None,
    )


async def prototype_card_line_at_commit(
    reader: Any,
    *,
    repo_path: str,
    commit: str,
    declared_at_start: bool | None = None,
) -> str:
    """The merge card's line for a build that started at ``commit``, read
    through ``reader`` (the merge-check reading's commit reader), or ``""``.

    ``""`` — no line — when the settings at that commit were read and
    declare no ``reference.signed_off``, or there is no settings file there.
    A declared record that cannot be read gives
    :data:`PROTOTYPE_NOT_READ_LINE`: never silently left out (Codex R4).

    When the settings themselves cannot be read (no reader, a read that
    raised or was refused), ``declared_at_start`` decides — what the planning
    run behind the build recorded at its start (coach's review, 17 October
    2026): ``True`` gives the "could not be read" line and a warning;
    ``False`` gives nothing, since a project that declared no record must
    never have a prototype named on its card; ``None`` (no start record to
    ask) gives nothing and a warning. Read at the START commit, so a sign-off
    recorded during a build does not change what that build's card says.
    Never raises.
    """
    if not commit:
        return ""

    def not_read(why: str) -> str:
        if declared_at_start is False:
            return ""
        logger.warning(
            "merge card: the project's settings could not be read at %s to "
            "tell which signed-off prototype the build was made from (%s); %s",
            commit[:12],
            why,
            "the card says so"
            if declared_at_start
            else "no start record says whether it declares one, so the card "
            "says nothing",
        )
        return PROTOTYPE_NOT_READ_LINE if declared_at_start else ""

    read = getattr(reader, "read_file_at_commit", None)
    if read is None:
        return not_read("the reader cannot read a file at a commit")
    try:
        answer = await read(repo_path, commit, DECLARATION_PATH)
    except Exception as exc:  # noqa: BLE001 — a reader never stops a card
        return not_read(f"{type(exc).__name__}: {exc}")
    refusal = getattr(answer, "refusal", None)
    if refusal:
        return not_read(str(refusal))
    content = getattr(answer, "content", None)
    if not getattr(answer, "found", False):
        return ""
    declared, why = read_declared_reference(
        content if isinstance(content, str) else None
    )
    if why is not None:
        return PROTOTYPE_NOT_READ_LINE
    if not declared.declared or not declared.signed_off:
        return ""
    record, why = await read_signed_off_record(
        reader, repo_path=repo_path, commit=commit, path=declared.signed_off
    )
    if why is not None:
        logger.warning("merge card: %s", why)
    return card_line_about_the_prototype(record)
