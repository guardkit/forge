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
import posixpath
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_planning import (
    build_feature_plan_command_args,
    build_feature_spec_command_args,
)
from forge.config.models import PlanningConfig, TargetTerminalConfig
from forge.deploy.candidate_tree import FileAtCommit, RemoteStartPoint
from forge.lifecycle import migrations
from forge.pipeline.dispatchers.specialist import build_specialist_command
from forge.pipeline.stage_taxonomy import StageClass
from forge.planning.declared_memory import (
    DeclaredPath,
    DeclaredProjectDocuments,
    read_declared_project_documents,
)
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.project_documents import (
    PROJECT_DOCUMENTS_BUDGET_BYTES,
    ProjectDocument,
    context_texts,
    read_project_documents_at_commit,
)
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from forge.planning.target_terminal_tools import ToolOutcome
from tests.forge.planning import test_driver_spec_digest_door as door
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
# A committed tree, as the shared reader's raw read sees it
# ---------------------------------------------------------------------------


class _Tree:
    """A stand-in for one repository's committed tree behind the raw read.

    ``files`` maps a path to its text; ``links`` maps a path (a file or a
    folder) to a symbolic link's target name; ``broken`` maps a path to a
    refusal. It answers as the host reader and the sidecar route do for a raw
    read: a link is ``found`` with its target name and mode ``120000``, a
    folder is ``found=False`` with mode ``040000``, nothing is ``found=False``.
    A plain read (the settings file) answers the text as before.
    """

    def __init__(
        self,
        files: dict[str, str],
        links: dict[str, str] | None = None,
        broken: dict[str, str] | None = None,
    ) -> None:
        self.files = dict(files)
        self.links = dict(links or {})
        self.broken = dict(broken or {})
        self.reads: list[tuple[str, str, bool]] = []

    def _mode(self, path: str) -> str | None:
        if path in self.links:
            return "120000"
        if path in self.files:
            return "100644"
        if any(p.startswith(path + "/") for p in [*self.files, *self.links]):
            return "040000"
        return None

    async def read_file_at_commit(
        self,
        repo_path: str,
        commit: str,
        file_path: str,
        *,
        ordinary_file_only: bool = False,
        raw: bool = False,
    ) -> FileAtCommit:
        self.reads.append((commit, file_path, raw))
        if file_path in self.broken:
            return FileAtCommit(refusal=self.broken[file_path])
        if not raw:
            if file_path not in self.files:
                return FileAtCommit(found=False)
            return FileAtCommit(content=self.files[file_path], found=True)
        mode = self._mode(file_path)
        if mode is None:
            return FileAtCommit(found=False)
        if mode == "040000":
            return FileAtCommit(found=False, mode=mode)
        if mode == "120000":
            return FileAtCommit(content=self.links[file_path], found=True, mode=mode)
        return FileAtCommit(
            content=self.files[file_path], found=True, ordinary=True, mode=mode
        )


DECLARED = ("docs/constitution/mission.md", "docs/constitution/tech-stack.md")


def _declared(
    documents: tuple[str, ...] = DECLARED, instructions: tuple[str, ...] = ()
) -> DeclaredProjectDocuments:
    return DeclaredProjectDocuments(
        instructions=tuple(
            DeclaredPath(spelling=p, path=posixpath.normpath(p)) for p in instructions
        ),
        documents=tuple(
            DeclaredPath(spelling=p, path=posixpath.normpath(p)) for p in documents
        ),
    )


async def _read(tree: Any, declared: DeclaredProjectDocuments) -> tuple[Any, Any]:
    return await read_project_documents_at_commit(
        tree, repo_path="/r", commit=START, declared=declared
    )


# ---------------------------------------------------------------------------
# What is declared (the opt-in, and GuardKit's allowed keys)
# ---------------------------------------------------------------------------


def test_nothing_is_judged_unless_binding_documents_are_declared() -> None:
    """A malformed ``autobuild.player`` block without required_documents is
    not this rule's business."""
    for text in (
        "memory:\n  project: p\n",
        "autobuild:\n  player:\n    surprise: 1\n    instructions: nope\n",
        "autobuild:\n  player:\n    required_documents: []\n    surprise: 1\n",
        "autobuild: 7\n",
    ):
        assert read_declared_project_documents(text) == (DeclaredProjectDocuments(), None)


def test_an_unknown_player_key_is_refused_when_documents_are_declared() -> None:
    declared, why = read_declared_project_documents(
        "autobuild:\n  player:\n    required_documents: [a.md]\n    surprise: 1\n"
    )
    assert declared == DeclaredProjectDocuments()
    assert why is not None and "surprise" in why and "required_documents" in why


def test_the_declared_lists_keep_their_spelling_and_order() -> None:
    declared, why = read_declared_project_documents(
        "autobuild:\n"
        "  player:\n"
        "    skills: [skills/a]\n"
        "    instructions: [./docs/how-we-work.md, AGENTS.md]\n"
        "    required_documents: [docs/b.md, ./docs/a.md]\n"
    )
    assert why is None
    assert [(d.spelling, d.path) for d in declared.instructions] == [
        ("./docs/how-we-work.md", "docs/how-we-work.md"),
        ("AGENTS.md", "AGENTS.md"),
    ]
    assert [(d.spelling, d.path) for d in declared.documents] == [
        ("docs/b.md", "docs/b.md"),
        ("./docs/a.md", "docs/a.md"),
    ]
    _, why = read_declared_project_documents(
        "autobuild:\n  player:\n    required_documents: [a.md]\n    skills: nope\n"
    )
    assert why is not None and "autobuild.player.skills" in why
    _, why = read_declared_project_documents(
        "autobuild:\n  player:\n    required_documents: [../a.md]\n"
    )
    assert why is not None and "not a path inside the repository" in why


# ---------------------------------------------------------------------------
# The reader: order, de-duplication, links, failures, budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_declared_reads_nothing() -> None:
    tree = _Tree({"AGENTS.md": AGENTS})
    assert await _read(tree, _declared(documents=())) == ((), None)
    assert tree.reads == []


@pytest.mark.asyncio
async def test_declared_instructions_then_conventional_files_then_documents() -> None:
    tree = _Tree(
        {
            "docs/how-we-work.md": "# How we work\n",
            "AGENTS.md": AGENTS,
            ".claude/CLAUDE.md": "# Claude\n",
            DECLARED[0]: MISSION_V1,
            DECLARED[1]: TECH_V1,
        }
    )
    documents, why = await _read(
        tree,
        _declared(
            documents=(DECLARED[1], DECLARED[0]),
            instructions=("./docs/how-we-work.md", ".claude/CLAUDE.md"),
        ),
    )
    assert why is None
    assert [d.path for d in documents] == [
        "./docs/how-we-work.md",  # the declared spelling is the label
        ".claude/CLAUDE.md",  # declared, so not repeated among the conventional
        "AGENTS.md",
        DECLARED[1],
        DECLARED[0],
    ]
    assert documents[4].receipt() == {
        "path": DECLARED[0],
        "sha256": _sha(MISSION_V1),
        "bytes": len(MISSION_V1.encode("utf-8")),
        "commit": START,
    }
    # Every read was a raw read AT the commit the work starts from.
    assert {(commit, raw) for commit, _path, raw in tree.reads} == {(START, True)}


@pytest.mark.asyncio
async def test_a_declared_instruction_that_is_missing_is_refused() -> None:
    tree = _Tree({DECLARED[0]: MISSION_V1})
    documents, why = await _read(
        tree, _declared(documents=(DECLARED[0],), instructions=("docs/how.md",))
    )
    assert documents == ()
    assert why is not None and "docs/how.md" in why and "instructions" in why


@pytest.mark.asyncio
async def test_files_resolving_to_one_target_count_once_under_the_first_name() -> None:
    tree = _Tree(
        {"AGENTS.md": AGENTS, DECLARED[0]: MISSION_V1},
        links={"CLAUDE.md": "AGENTS.md"},
    )
    documents, why = await _read(tree, _declared(documents=(DECLARED[0],)))
    assert why is None
    assert [d.path for d in documents] == ["AGENTS.md", DECLARED[0]]
    assert sum(d.bytes for d in documents) == len(AGENTS) + len(MISSION_V1)

    # An instruction file linked to a declared document: once, first name.
    tree = _Tree({DECLARED[0]: MISSION_V1}, links={"AGENTS.md": DECLARED[0]})
    documents, why = await _read(tree, _declared(documents=(DECLARED[0],)))
    assert why is None
    assert [(d.path, d.text) for d in documents] == [("AGENTS.md", MISSION_V1)]


@pytest.mark.asyncio
async def test_a_chain_of_links_and_a_linked_folder_are_followed_inside_the_commit() -> None:
    tree = _Tree(
        {"shared/claude/CLAUDE.md": "# Claude\n", "docs/agents.md": AGENTS, DECLARED[0]: MISSION_V1},
        links={
            ".claude": "shared/claude",  # a linked FOLDER
            "AGENTS.md": "docs/a1.md",  # a chain: AGENTS.md -> a1 -> a2 -> agents.md
            "docs/a1.md": "a2.md",
            "docs/a2.md": "../docs/agents.md",
        },
    )
    documents, why = await _read(tree, _declared(documents=(DECLARED[0],)))
    assert why is None
    assert [(d.path, d.text) for d in documents] == [
        ("AGENTS.md", AGENTS),
        (".claude/CLAUDE.md", "# Claude\n"),
        (DECLARED[0], MISSION_V1),
    ]


@pytest.mark.parametrize(
    ("links", "files", "said"),
    [
        ({"AGENTS.md": "../outside/AGENTS.md"}, {}, "outside the repository"),
        ({"AGENTS.md": "/etc/passwd"}, {}, "outside the repository"),
        ({".claude": "../../elsewhere"}, {}, "outside the repository"),
        ({"AGENTS.md": "docs/not-there.md"}, {}, "target is not a file"),
        ({"AGENTS.md": "CLAUDE.md", "CLAUDE.md": "AGENTS.md"}, {}, "a loop"),
    ],
    ids=["outside", "absolute", "linked-folder-outside", "missing-target", "loop"],
)
@pytest.mark.asyncio
async def test_an_instruction_link_that_cannot_be_followed_refuses_the_run(
    links: dict[str, str], files: dict[str, str], said: str
) -> None:
    tree = _Tree({DECLARED[0]: MISSION_V1, **files}, links=links)
    documents, why = await _read(tree, _declared(documents=(DECLARED[0],)))
    assert documents == ()
    assert why is not None and said in why


@pytest.mark.asyncio
async def test_an_instruction_file_that_cannot_be_read_refuses_the_run() -> None:
    tree = _Tree(
        {"AGENTS.md": AGENTS, DECLARED[0]: MISSION_V1},
        broken={"AGENTS.md": "the sandbox sidecar could not be reached"},
    )
    documents, why = await _read(tree, _declared(documents=(DECLARED[0],)))
    assert documents == ()
    assert why is not None and "AGENTS.md" in why and "could not be reached" in why


@pytest.mark.asyncio
async def test_an_answer_that_does_not_say_the_mode_is_refused() -> None:
    """A reader that ignored the raw read (no mode) is never trusted."""

    class _OldReader:
        async def read_file_at_commit(self, *_a: Any, **_k: Any) -> FileAtCommit:
            return FileAtCommit(content=MISSION_V1, found=True)

    documents, why = await _read(_OldReader(), _declared(documents=(DECLARED[0],)))
    assert documents == ()
    assert why is not None and "exact bytes" in why


@pytest.mark.asyncio
async def test_a_missing_document_is_refused_by_name() -> None:
    tree = _Tree({DECLARED[0]: MISSION_V1})
    documents, why = await _read(tree, _declared())
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
    documents, why = await _read(tree, _declared(documents=(DECLARED[0],)))
    assert documents == ()
    assert why is not None
    assert DECLARED[0] in why and "symbolic link" in why


@pytest.mark.asyncio
async def test_over_the_budget_is_refused_naming_every_file_and_size() -> None:
    at_limit = "x" * (PROJECT_DOCUMENTS_BUDGET_BYTES - len(AGENTS))
    tree = _Tree({"AGENTS.md": AGENTS, DECLARED[0]: at_limit})
    documents, why = await _read(tree, _declared(documents=(DECLARED[0],)))
    assert why is None and sum(d.bytes for d in documents) == PROJECT_DOCUMENTS_BUDGET_BYTES

    tree.files[DECLARED[0]] = at_limit + "x"
    documents, why = await _read(tree, _declared(documents=(DECLARED[0],)))
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
# The exact committed bytes, through real git (the host reader)
# ---------------------------------------------------------------------------


def _commit_bytes(tmp_path: Path, files: dict[str, bytes], links: dict[str, str] | None = None) -> tuple[Path, str]:
    repo = tmp_path / "bytes-repo"
    repo.mkdir()
    at_the_door._git(repo, "init", "-q", "-b", "main")
    at_the_door._git(repo, "config", "core.autocrlf", "false")
    for rel, data in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    for rel, target in (links or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).symlink_to(target)
    at_the_door._git(repo, "add", "-A")
    at_the_door._git(repo, "commit", "-qm", "bytes")
    return repo, at_the_door._git(repo, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_line_endings_are_kept_and_hashed_as_committed(tmp_path: Path) -> None:
    crlf = b"# Mission\r\n\r\nServe the shop.\r\n"
    lone_cr = b"# Tech\rone line\r"
    unicode = "# Agents — café\n".encode("utf-8")
    repo, sha = _commit_bytes(
        tmp_path,
        {DECLARED[0]: crlf, DECLARED[1]: lone_cr, "AGENTS.md": unicode},
    )
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    documents, why = await read_project_documents_at_commit(
        runner, repo_path=str(repo), commit=sha, declared=_declared()
    )

    assert why is None
    by_path = {d.path: d for d in documents}
    for path, data in ((DECLARED[0], crlf), (DECLARED[1], lone_cr), ("AGENTS.md", unicode)):
        assert by_path[path].text.encode("utf-8") == data
        assert by_path[path].sha256 == hashlib.sha256(data).hexdigest()
        assert by_path[path].bytes == len(data)


@pytest.mark.asyncio
async def test_invalid_utf8_is_refused(tmp_path: Path) -> None:
    repo, sha = _commit_bytes(
        tmp_path, {DECLARED[0]: b"# Mission\n\xff\xfe not text\n"}
    )
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    documents, why = await read_project_documents_at_commit(
        runner, repo_path=str(repo), commit=sha, declared=_declared(documents=(DECLARED[0],))
    )

    assert documents == ()
    assert why is not None and DECLARED[0] in why and "not UTF-8 text" in why


@pytest.mark.asyncio
async def test_the_budget_counts_raw_bytes(tmp_path: Path) -> None:
    """Multi-byte characters count by their bytes: 16,385 three-byte
    characters are 49,155 bytes, over the 49,152-byte budget."""
    text = "—" * (PROJECT_DOCUMENTS_BUDGET_BYTES // 3 + 1)
    repo, sha = _commit_bytes(tmp_path, {DECLARED[0]: text.encode("utf-8")})
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    documents, why = await read_project_documents_at_commit(
        runner, repo_path=str(repo), commit=sha, declared=_declared(documents=(DECLARED[0],))
    )

    assert len(text) < PROJECT_DOCUMENTS_BUDGET_BYTES
    assert documents == ()
    assert why is not None and f"({len(text.encode('utf-8'))} bytes)" in why


@pytest.mark.asyncio
async def test_real_links_chains_and_linked_folders(tmp_path: Path) -> None:
    repo, sha = _commit_bytes(
        tmp_path,
        {
            "shared/claude/CLAUDE.md": b"# Claude\n",
            "docs/agents.md": AGENTS.encode(),
            DECLARED[0]: MISSION_V1.encode(),
        },
        links={
            ".claude": "shared/claude",
            "AGENTS.md": "docs/a1.md",
            "docs/a1.md": "agents.md",
            "CLAUDE.md": "AGENTS.md",
        },
    )
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    documents, why = await read_project_documents_at_commit(
        runner, repo_path=str(repo), commit=sha, declared=_declared(documents=(DECLARED[0],))
    )

    assert why is None
    assert [(d.path, d.text) for d in documents] == [
        ("AGENTS.md", AGENTS),
        (".claude/CLAUDE.md", "# Claude\n"),
        (DECLARED[0], MISSION_V1),
    ]


@pytest.mark.asyncio
async def test_a_real_link_out_of_the_repository_is_refused(tmp_path: Path) -> None:
    repo, sha = _commit_bytes(
        tmp_path, {DECLARED[0]: MISSION_V1.encode()}, links={"AGENTS.md": "../../etc/hosts"}
    )
    runner = WorktreeGitRunner(worktrees_root=tmp_path / "wt")

    documents, why = await read_project_documents_at_commit(
        runner, repo_path=str(repo), commit=sha, declared=_declared(documents=(DECLARED[0],))
    )

    assert documents == ()
    assert why is not None and "AGENTS.md" in why and "outside the repository" in why


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


# ---------------------------------------------------------------------------
# The whole chain: what the spec writer and the plan writer are sent
# ---------------------------------------------------------------------------


class _DocsGitRunner(door.RecordingGitRunner):
    """The digest-door stand-in, whose commit also carries the project's files."""

    def __init__(self, tree: _Tree) -> None:
        super().__init__()
        self.tree = tree

    async def fetch_remote_start_point(self, repo_path: str) -> Any:
        return RemoteStartPoint(branch="main", commit=START)

    async def read_file_at_commit(  # type: ignore[override]
        self,
        repo_path: str,
        commit: str,
        file_path: str,
        *,
        ordinary_file_only: bool = False,
        raw: bool = False,
    ) -> Any:
        return await self.tree.read_file_at_commit(
            repo_path, commit, file_path, ordinary_file_only=ordinary_file_only, raw=raw
        )


def _chain(
    store: SqlitePlanningRunStore,
    tree: _Tree,
    *,
    answers: list[Any],
    on_spec: Any = None,
) -> SimpleNamespace:
    """A driver whose spec and plan writers record every keyword they get."""
    from datetime import UTC, datetime

    def clock() -> datetime:
        return datetime.now(UTC)

    repository, state_machine = build_planning_gate_adapters(store, clock=clock)
    calls: dict[str, list[dict[str, Any]]] = {"po": [], "spec": [], "plan": []}
    notifications: list[tuple[str, str, str]] = []

    async def dispatch_po(**kwargs: Any) -> Any:
        calls["po"].append(kwargs)
        return SimpleNamespace(
            outcome=SimpleNamespace(value="completed"),
            coach_score=0.9,
            criterion_breakdown=[],
            detection_findings=(),
            role_output={"title": "docs", "problem_statement": "ship a thing"},
            reason=None,
        )

    async def dispatch_spec(**kwargs: Any) -> Any:
        calls["spec"].append(kwargs)
        if on_spec is not None:
            on_spec(len(calls["spec"]))
        return door._spec_reply()

    async def dispatch_plan(**kwargs: Any) -> Any:
        calls["plan"].append(kwargs)
        return door._plan_reply(kwargs["feature_id"])

    async def ok(*_args: Any) -> ToolOutcome:
        return ToolOutcome(ok=True)

    async def dispatch_build_trigger(**_: Any) -> Any:
        from forge.planning.driver import BuildTriggerResult

        return BuildTriggerResult(queued=True, build_id="build-1")

    async def publish_notification(cid: str, message: str, level: str) -> None:
        notifications.append((cid, message, level))

    cfg = PlanningConfig(
        enabled=True,
        target_repo_paths={door.TARGET_REPO: "/srv/repos/api_test"},
        target_terminal=TargetTerminalConfig(enabled=True),
        originator_wait_seconds=3600,
    )
    git = _DocsGitRunner(tree)
    driver = PlanningRunDriver(
        PlanningDriverDeps(
            store=store,
            repository=repository,
            state_machine=state_machine,
            approval_publisher=door.FakePublisher(),
            subscriber_factory=door.SharedScriptFactory(answers),
            dispatch_product_owner=dispatch_po,
            second_opinion_provider=door.FakeSecondOpinion(),
            git_runner=git,
            planning_config=cfg,
            clock=clock,
            publish_notification=publish_notification,
            dispatch_feature_spec=dispatch_spec,
            dispatch_feature_plan=dispatch_plan,
            normalize_feature_spec=ok,
            validate_feature_plan=ok,
            validate_pass_bar=ok,
            validate_gate_registry=ok,
            dispatch_build_trigger=dispatch_build_trigger,
        )
    )
    return SimpleNamespace(
        driver=driver, calls=calls, notifications=notifications, tree=tree
    )


@pytest.fixture
def chain_store(tmp_path: Path) -> SqlitePlanningRunStore:
    cx = sqlite_connect.connect_writer(tmp_path / "chain.db")
    migrations.apply_at_boot(cx)
    return SqlitePlanningRunStore(cx, target_terminal_enabled=True)


def _declared_tree() -> _Tree:
    return _Tree(
        {
            ".guardkit/config.yaml": DECLARES_DOCUMENTS,
            "AGENTS.md": AGENTS,
            DECLARED[0]: MISSION_V1,
            DECLARED[1]: TECH_V1,
        },
        links={"CLAUDE.md": "AGENTS.md"},
    )


def _events(store: SqlitePlanningRunStore, stage: str) -> list[dict[str, Any]]:
    return [
        json.loads(event["details_json"] or "{}")
        for event in store.list_events(door.CID)
        if event["stage_label"] == stage
    ]


def _drafted(store: SqlitePlanningRunStore) -> list[dict[str, Any]]:
    return [
        json.loads(event["details_json"])
        for event in store.list_events(door.CID)
        if event["stage_label"] == door._DRAFT_STAGE and event["status"] == "drafted"
    ]


@pytest.mark.asyncio
async def test_both_writers_get_the_documents_on_every_call_and_say_so(
    chain_store: SqlitePlanningRunStore,
) -> None:
    """First spec call, the rewrite after the owner's note, and the plan call
    all carry the same texts — the ones read at the start commit, even though
    the project changes after the first spec call — and both receipts record
    path, hash, size and commit."""
    tree = _declared_tree()

    def the_project_moves_on(call: int) -> None:
        if call == 1:
            tree.files[DECLARED[0]] = MISSION_V1 + "\nChanged after the start.\n"

    door._queue(chain_store)
    h = _chain(
        chain_store,
        tree,
        answers=[
            door._answer("reject", notes="the second example should be a 404"),
            door._answer("approve", attempt=1),
        ],
        on_spec=the_project_moves_on,
    )
    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    expected = [
        f"File: AGENTS.md\n{AGENTS}",
        f"File: {DECLARED[0]}\n{MISSION_V1}",
        f"File: {DECLARED[1]}\n{TECH_V1}",
    ]
    first, rewrite = h.calls["spec"]
    assert first["context"] == expected
    assert rewrite["validate_feedback"] == "the second example should be a 404"
    assert rewrite["context"] == expected
    assert [call["context"] for call in h.calls["plan"]] == [expected]
    # Nothing was read again after the door: every project-file read came first.
    paths_read = [path for _commit, path, _ordinary in tree.reads]
    assert paths_read.count(DECLARED[0]) == 1

    receipt = [
        {
            "path": "AGENTS.md",
            "sha256": _sha(AGENTS),
            "bytes": len(AGENTS),
            "commit": START,
        },
        {
            "path": DECLARED[0],
            "sha256": _sha(MISSION_V1),
            "bytes": len(MISSION_V1),
            "commit": START,
        },
        {
            "path": DECLARED[1],
            "sha256": _sha(TECH_V1),
            "bytes": len(TECH_V1),
            "commit": START,
        },
    ]
    # The first draft and the rewrite: one ``drafted`` row each.
    drafts = _drafted(chain_store)
    assert len(drafts) == 2
    for draft in drafts:
        assert draft["spec_draft"]["project_documents"] == {
            "status": "sent",
            "documents": receipt,
        }
    (plan_row,) = _events(chain_store, "feature-plan")
    assert plan_row["project_documents"] == {"status": "sent", "documents": receipt}
    # Recorded once, at the door, before the product-owner was asked anything.
    labels = [event["stage_label"] for event in chain_store.list_events(door.CID)]
    assert labels.count(_DOCUMENTS_STAGE) == 1
    assert labels.index(_DOCUMENTS_STAGE) < labels.index("product_owner")


@pytest.mark.asyncio
async def test_a_project_that_declares_nothing_sends_what_it_always_sent(
    chain_store: SqlitePlanningRunStore,
) -> None:
    tree = _Tree(
        {".guardkit/config.yaml": DECLARES_NOTHING, "AGENTS.md": AGENTS}
    )
    door._queue(chain_store)
    h = _chain(chain_store, tree, answers=[door._answer("approve")])

    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert all("context" not in call for call in h.calls["spec"] + h.calls["plan"])
    # Only the settings file was read: not even the instruction file.
    assert [path for _c, path, _o in tree.reads] == [".guardkit/config.yaml"] * len(
        tree.reads
    )
    assert _events(chain_store, _DOCUMENTS_STAGE) == []
    drafts = _drafted(chain_store)
    assert drafts and all("project_documents" not in d["spec_draft"] for d in drafts)
    (plan_row,) = _events(chain_store, "feature-plan")
    assert "project_documents" not in plan_row


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda t: t.files.pop(DECLARED[1]), "no such file"),
        (lambda t: t.links.__setitem__(DECLARED[1], "../x.md") or t.files.pop(DECLARED[1]), "symbolic link"),
        (
            lambda t: t.files.__setitem__(
                DECLARED[1], "z" * PROJECT_DOCUMENTS_BUDGET_BYTES
            ),
            str(PROJECT_DOCUMENTS_BUDGET_BYTES),
        ),
        (
            lambda t: t.files.__setitem__(
                ".guardkit/config.yaml",
                DECLARES_DOCUMENTS + "    surprise: [x]\n",
            ),
            "surprise",
        ),
        (lambda t: t.broken.__setitem__("AGENTS.md", "sidecar unreachable"), "sidecar unreachable"),
    ],
    ids=["missing", "symbolic-link", "over-budget", "unknown-player-key", "unreadable-instruction"],
)
@pytest.mark.asyncio
async def test_a_refusal_stops_the_run_before_any_model_is_asked(
    chain_store: SqlitePlanningRunStore, change: Any, said: str
) -> None:
    tree = _declared_tree()
    change(tree)
    door._queue(chain_store)
    h = _chain(chain_store, tree, answers=[door._answer("approve")])

    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.FAILED.value
    assert h.calls == {"po": [], "spec": [], "plan": []}
    errors = [message for _cid, message, level in h.notifications if level == "error"]
    assert len(errors) == 1 and said in errors[0]
    assert _events(chain_store, _DOCUMENTS_STAGE) == []


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def test_the_generic_dispatcher_keeps_context_on_the_wire() -> None:
    texts = [f"File: {DECLARED[0]}\n{MISSION_V1}"]
    _command, spec_args = build_specialist_command(
        StageClass.FEATURE_SPEC,
        request_text=None,
        context_entries=[],
        extra_command_args=build_feature_spec_command_args(
            from_input="the input", context=texts
        ),
    )
    assert spec_args["context"] == texts

    _command, plan_args = build_specialist_command(
        StageClass.FEATURE_PLAN,
        request_text=None,
        context_entries=[],
        extra_command_args=build_feature_plan_command_args(
            feature_id="FEAT-1",
            spec_feature="Feature: x\n",
            spec_summary="# s\n",
            target_repo_descriptor={"repo": "o/r", "test_roots": []},
            context=texts,
        ),
    )
    assert plan_args["context"] == texts

    _command, plain = build_specialist_command(
        StageClass.FEATURE_SPEC,
        request_text=None,
        context_entries=[],
        extra_command_args=build_feature_spec_command_args(from_input="the input"),
    )
    assert "context" not in plain


@pytest.mark.asyncio
async def test_an_unknown_player_key_without_documents_is_not_refused(
    chain_store: SqlitePlanningRunStore,
) -> None:
    tree = _Tree(
        {
            ".guardkit/config.yaml": DECLARES_NOTHING
            + "autobuild:\n  player:\n    surprise: [x]\n",
            "AGENTS.md": AGENTS,
        }
    )
    door._queue(chain_store)
    h = _chain(chain_store, tree, answers=[door._answer("approve")])

    await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert all("context" not in call for call in h.calls["spec"] + h.calls["plan"])


@pytest.mark.asyncio
async def test_a_run_past_the_door_before_documents_were_read_says_so_once(
    chain_store: SqlitePlanningRunStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A run whose memory name was recorded before this change has no
    documents record: nothing is read now (a re-drive never moves a run onto
    what its project says today), its writers are sent none, and one log line
    says so."""
    tree = _declared_tree()
    door._queue(chain_store)
    chain_store.record_start_point(door.CID, start_commit=START, target_branch="main")
    chain_store.record_memory_project(door.CID, memory_project="widget_shop")
    chain_store.record_launch_settings(door.CID, names=[])
    h = _chain(chain_store, tree, answers=[door._answer("approve")])

    with caplog.at_level("INFO", logger="forge.planning.driver"):
        await h.driver.drive(door.CID)

    assert chain_store.get_run(door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert all("context" not in call for call in h.calls["spec"] + h.calls["plan"])
    assert not any(raw for _c, _p, raw in tree.reads)
    said = [r for r in caplog.records if "has no project documents recorded" in r.getMessage()]
    assert len(said) == 1
