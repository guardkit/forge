"""The project's own documents reach the spec writer and the plan writer.

Project initialisation design, 4 October 2026, Part 3 ("Spec writer", the Forge
half of "Plan writer", "Binding documents are ordinary files") and the
"Planning context" and "Same bytes" acceptance bullets.

What these hold down:

* a project that names binding documents in ``autobuild.player.required_documents``
  has them — and the repository instruction files GuardKit adds automatically —
  read AT THE COMMIT THE WORK STARTS FROM, never the working folder and never a
  later commit, and recorded with path, hash, size and commit before any model
  is asked anything;
* those exact texts go to the spec writer as ``context`` on every call,
  rewrites included, and to the plan writer as ``context``; both receipts
  record which files and which versions were sent;
* a re-drive reuses what was recorded and reads nothing again;
* a missing document, a symbolic-link document and documents over the budget
  stop the run at the door with a plain sentence, with nothing dispatched;
* a project that declares nothing has nothing read and nothing sent: the calls
  are what they always were;
* the generic dispatcher keeps ``context`` on the wire.

Nobody's real repository, broker, model or sandbox is contacted: the git
repositories here are scratch ones on disk, and the specialist dispatches are
stand-ins that record what they were given.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.deploy.candidate_tree import FileAtCommit
from forge.lifecycle import migrations
from forge.planning.project_documents import (
    PROJECT_DOCUMENTS_BUDGET_BYTES,
    ProjectDocument,
    context_texts,
    read_project_documents_at_commit,
)
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning import test_memory_name_at_the_door as at_the_door

_DOCUMENTS_STAGE = "project-documents"
START = "a" * 40

MISSION_V1 = "# Mission\n\nStatus: accepted\n\nServe the widget shop.\n"
TECH_V1 = "# Technical decisions\n\nOne service, one database.\n"
AGENTS = "# Agents\n\nRun the root check before you finish.\n"

DECLARES_DOCUMENTS = (
    "memory:\n"
    "  project: widget_shop\n"
    "autobuild:\n"
    "  player:\n"
    "    required_documents:\n"
    "      - docs/constitution/mission.md\n"
    "      - docs/constitution/tech-stack.md\n"
)
DECLARES_NOTHING = "memory:\n  project: widget_shop\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# A committed tree, as the shared reader sees it
# ---------------------------------------------------------------------------


class _Tree:
    """A stand-in for one repository's committed tree behind the shared reader.

    ``files`` maps a path to its text; ``links`` maps a path to a symbolic
    link's target name. It answers exactly as the host reader and the sidecar
    route do: an ordinary-file read of a link is refused, a plain read of a link
    hands back the target's NAME, a missing path is ``found=False``.
    """

    def __init__(
        self,
        files: dict[str, str],
        links: dict[str, str] | None = None,
    ) -> None:
        self.files = dict(files)
        self.links = dict(links or {})
        self.reads: list[tuple[str, str, bool]] = []

    async def read_file_at_commit(
        self,
        repo_path: str,
        commit: str,
        file_path: str,
        *,
        ordinary_file_only: bool = False,
    ) -> FileAtCommit:
        self.reads.append((commit, file_path, ordinary_file_only))
        if file_path in self.links:
            if ordinary_file_only:
                return FileAtCommit(
                    refusal=(
                        f"{file_path} at {commit} is a symbolic link, not an "
                        f"ordinary file; a document the project's builds are "
                        f"held to must be the file itself"
                    )
                )
            return FileAtCommit(content=self.links[file_path], found=True)
        if file_path not in self.files:
            return FileAtCommit(found=False)
        return FileAtCommit(
            content=self.files[file_path], found=True, ordinary=ordinary_file_only
        )


DECLARED = ("docs/constitution/mission.md", "docs/constitution/tech-stack.md")


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_declared_reads_nothing() -> None:
    tree = _Tree({"AGENTS.md": AGENTS})
    documents, why = await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=()
    )
    assert (documents, why) == ((), None)
    assert tree.reads == []


@pytest.mark.asyncio
async def test_instruction_files_first_then_documents_in_declared_order() -> None:
    tree = _Tree(
        {
            "AGENTS.md": AGENTS,
            ".claude/CLAUDE.md": "# Claude\n",
            DECLARED[0]: MISSION_V1,
            DECLARED[1]: TECH_V1,
        }
    )
    documents, why = await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=(DECLARED[1], DECLARED[0])
    )
    assert why is None
    assert [d.path for d in documents] == [
        "AGENTS.md",
        ".claude/CLAUDE.md",
        DECLARED[1],
        DECLARED[0],
    ]
    mission = documents[3]
    assert mission.receipt() == {
        "path": DECLARED[0],
        "sha256": _sha(MISSION_V1),
        "bytes": len(MISSION_V1.encode("utf-8")),
        "commit": START,
    }
    # Every read was AT the commit the work starts from.
    assert {commit for commit, _path, _ordinary in tree.reads} == {START}
    # Each declared document was read as an ordinary file.
    assert (START, DECLARED[0], True) in tree.reads


@pytest.mark.asyncio
async def test_instruction_files_resolving_to_one_file_count_once() -> None:
    """CLAUDE.md is a link to AGENTS.md: delivered once, under AGENTS.md. A
    link that leaves the repository, and a dangling one, are skipped."""
    tree = _Tree(
        {"AGENTS.md": AGENTS, DECLARED[0]: MISSION_V1},
        links={
            "CLAUDE.md": "AGENTS.md",
            ".claude/CLAUDE.md": "../../outside/CLAUDE.md",
        },
    )
    documents, why = await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=(DECLARED[0],)
    )
    assert why is None
    assert [d.path for d in documents] == ["AGENTS.md", DECLARED[0]]
    assert documents[0].text == AGENTS
    assert sum(d.bytes for d in documents) == len(AGENTS) + len(MISSION_V1)

    dangling = _Tree(
        {DECLARED[0]: MISSION_V1}, links={"AGENTS.md": "docs/not-there.md"}
    )
    documents, why = await read_project_documents_at_commit(
        dangling, repo_path="/r", commit=START, declared=(DECLARED[0],)
    )
    assert why is None
    assert [d.path for d in documents] == [DECLARED[0]]


@pytest.mark.asyncio
async def test_a_linked_instruction_file_inside_the_repository_is_followed() -> None:
    tree = _Tree(
        {"docs/agents/AGENTS.md": AGENTS, DECLARED[0]: MISSION_V1},
        links={"CLAUDE.md": "docs/agents/AGENTS.md"},
    )
    documents, why = await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=(DECLARED[0],)
    )
    assert why is None
    assert [(d.path, d.text) for d in documents] == [
        ("CLAUDE.md", AGENTS),
        (DECLARED[0], MISSION_V1),
    ]


@pytest.mark.asyncio
async def test_a_missing_document_is_refused_by_name() -> None:
    tree = _Tree({DECLARED[0]: MISSION_V1})
    documents, why = await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=DECLARED
    )
    assert documents == ()
    assert why is not None
    assert DECLARED[1] in why
    assert "autobuild.player.required_documents" in why
    assert "no such file" in why


@pytest.mark.asyncio
async def test_a_symbolic_link_document_is_refused() -> None:
    tree = _Tree(
        {"docs/real-mission.md": MISSION_V1},
        links={DECLARED[0]: "../real-mission.md"},
    )
    documents, why = await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=(DECLARED[0],)
    )
    assert documents == ()
    assert why is not None
    assert DECLARED[0] in why and "symbolic link" in why


@pytest.mark.asyncio
async def test_over_the_budget_is_refused_naming_every_file_and_size() -> None:
    at_limit = "x" * (PROJECT_DOCUMENTS_BUDGET_BYTES - len(AGENTS))
    tree = _Tree({"AGENTS.md": AGENTS, DECLARED[0]: at_limit})
    documents, why = await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=(DECLARED[0],)
    )
    assert why is None and sum(d.bytes for d in documents) == PROJECT_DOCUMENTS_BUDGET_BYTES

    tree.files[DECLARED[0]] = at_limit + "x"
    documents, why = await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=(DECLARED[0],)
    )
    assert documents == ()
    assert why is not None
    assert str(PROJECT_DOCUMENTS_BUDGET_BYTES) in why
    assert f"AGENTS.md ({len(AGENTS)} bytes)" in why
    assert f"{DECLARED[0]} ({len(at_limit) + 1} bytes)" in why


def test_each_context_text_starts_with_one_line_naming_its_path() -> None:
    documents = (
        ProjectDocument.of("AGENTS.md", AGENTS, START),
        ProjectDocument.of(DECLARED[0], MISSION_V1, START),
    )
    assert context_texts(documents) == [
        f"File: AGENTS.md\n{AGENTS}",
        f"File: {DECLARED[0]}\n{MISSION_V1}",
    ]


def test_a_recorded_document_whose_text_was_changed_does_not_come_back() -> None:
    record = ProjectDocument.of(DECLARED[0], MISSION_V1, START).to_record()
    assert ProjectDocument.from_record(record) == ProjectDocument.of(
        DECLARED[0], MISSION_V1, START
    )
    record["text"] = MISSION_V1 + "edited\n"
    assert ProjectDocument.from_record(record) is None


# ---------------------------------------------------------------------------
# The door, against real git (the host reader)
# ---------------------------------------------------------------------------


def _seed_project(tmp_path: Path, *, extra: dict[str, str] | None = None) -> tuple[Path, Path]:
    """A remote whose one commit declares two documents and carries them, plus
    an AGENTS.md and a CLAUDE.md that is a link to it, and a copy of it."""
    remote, copy = at_the_door.make_remote_and_copy(
        tmp_path, declaration=DECLARES_DOCUMENTS
    )
    (copy / "docs" / "constitution").mkdir(parents=True)
    (copy / DECLARED[0]).write_text(MISSION_V1, encoding="utf-8")
    (copy / DECLARED[1]).write_text(TECH_V1, encoding="utf-8")
    (copy / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
    (copy / "CLAUDE.md").symlink_to("AGENTS.md")
    for path, text in (extra or {}).items():
        (copy / path).parent.mkdir(parents=True, exist_ok=True)
        (copy / path).write_text(text, encoding="utf-8")
    _push(copy, "the project's documents")
    return remote, copy


def _push(copy: Path, message: str) -> None:
    at_the_door._git(copy, "add", "-A")
    at_the_door._git(copy, "commit", "-qm", message)
    at_the_door._git(copy, "push", "-q", "origin", "HEAD:refs/heads/main")


def _recorded(store: SqlitePlanningRunStore, cid: str) -> list[list[dict[str, Any]]]:
    return [
        json.loads(event["details_json"])["project_documents"]
        for event in store.list_events(cid)
        if event["stage_label"] == _DOCUMENTS_STAGE
    ]


@pytest.fixture
def ledger(tmp_path: Path) -> SqlitePlanningRunStore:
    connection = sqlite3.connect(str(tmp_path / "docs.db"))
    connection.row_factory = sqlite3.Row
    migrations.apply_at_boot(connection)
    return SqlitePlanningRunStore(connection, target_terminal_enabled=True)


@pytest.mark.asyncio
async def test_the_door_reads_the_documents_at_the_start_commit_not_the_working_folder(
    ledger: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = _seed_project(tmp_path)
    start = at_the_door._git(copy, "rev-parse", "HEAD")
    # The copy's working folder says something else; it is never consulted.
    (copy / DECLARED[0]).write_text("# Mission\n\nWHATEVER THE FOLDER SAYS\n")
    h = at_the_door._make_driver(ledger, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = at_the_door._queue_running(ledger)

    assert await h.driver._door(row, at_the_door.CID) is not None

    recorded = _recorded(ledger, at_the_door.CID)
    assert len(recorded) == 1
    assert [d["path"] for d in recorded[0]] == ["AGENTS.md", *DECLARED]
    by_path = {d["path"]: d for d in recorded[0]}
    assert by_path[DECLARED[0]]["text"] == MISSION_V1
    assert by_path[DECLARED[0]]["sha256"] == _sha(MISSION_V1)
    assert by_path[DECLARED[0]]["bytes"] == len(MISSION_V1)
    assert {d["commit"] for d in recorded[0]} == {start}
    assert h.errors == []


@pytest.mark.asyncio
async def test_a_re_drive_reuses_what_was_recorded_and_never_reads_a_newer_commit(
    ledger: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = _seed_project(tmp_path)
    start = at_the_door._git(copy, "rev-parse", "HEAD")
    h = at_the_door._make_driver(ledger, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = at_the_door._queue_running(ledger)
    assert await h.driver._door(row, at_the_door.CID) is not None

    # The remote moves on: a newer mission is pushed after the run started.
    (copy / DECLARED[0]).write_text(MISSION_V1 + "\nA newer word.\n")
    _push(copy, "a newer mission")

    assert await h.driver._door(row, at_the_door.CID) is not None
    documents, why = h.driver._recorded_project_documents(at_the_door.CID)

    assert why is None
    assert len(_recorded(ledger, at_the_door.CID)) == 1
    assert {d.commit for d in documents} == {start}
    assert dict((d.path, d.text) for d in documents)[DECLARED[0]] == MISSION_V1


@pytest.mark.asyncio
async def test_a_committed_symbolic_link_document_fails_the_run_at_the_door(
    ledger: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = _seed_project(tmp_path)
    (copy / DECLARED[1]).unlink()
    (copy / "docs" / "real-tech.md").write_text(TECH_V1, encoding="utf-8")
    (copy / DECLARED[1]).symlink_to("../real-tech.md")
    _push(copy, "the technical decisions become a link")
    h = at_the_door._make_driver(ledger, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = at_the_door._queue_running(ledger)

    assert await h.driver._door(row, at_the_door.CID) is None

    assert ledger.get_run(at_the_door.CID)["state"] == PlanningState.FAILED.value
    assert len(h.errors) == 1
    assert DECLARED[1] in h.errors[0] and "symbolic link" in h.errors[0]
    assert _recorded(ledger, at_the_door.CID) == []
    # Nothing was recorded about where the work belongs either.
    assert ledger.get_memory_project(at_the_door.CID) is None


@pytest.mark.asyncio
async def test_a_document_missing_at_the_commit_fails_the_run_at_the_door(
    ledger: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _remote, copy = _seed_project(tmp_path)
    at_the_door._git(copy, "rm", "-q", DECLARED[1])
    at_the_door._git(copy, "commit", "-qm", "drop the technical decisions")
    at_the_door._git(copy, "push", "-q", "origin", "HEAD:refs/heads/main")
    # The working folder still has it; that is not the commit.
    (copy / DECLARED[1]).write_text(TECH_V1, encoding="utf-8")
    h = at_the_door._make_driver(ledger, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = at_the_door._queue_running(ledger)

    assert await h.driver._door(row, at_the_door.CID) is None

    assert ledger.get_run(at_the_door.CID)["state"] == PlanningState.FAILED.value
    assert DECLARED[1] in h.errors[0] and "no such file" in h.errors[0]


@pytest.mark.asyncio
async def test_documents_over_the_budget_fail_the_run_at_the_door(
    ledger: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    big = "y" * PROJECT_DOCUMENTS_BUDGET_BYTES
    _remote, copy = _seed_project(tmp_path, extra={DECLARED[1]: big})
    h = at_the_door._make_driver(ledger, repo_path=copy, worktrees_root=tmp_path / "wt")
    row = at_the_door._queue_running(ledger)

    assert await h.driver._door(row, at_the_door.CID) is None

    assert ledger.get_run(at_the_door.CID)["state"] == PlanningState.FAILED.value
    assert str(PROJECT_DOCUMENTS_BUDGET_BYTES) in h.errors[0]
    assert f"{DECLARED[1]} ({len(big)} bytes)" in h.errors[0]
