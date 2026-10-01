"""Where the planner's fact sheet reads the repository, what it says about the
data models and migrations a request touches, and what it says when it cannot
read the repository at all (release -3 item 10, 1 October 2026).

On 1 October the containerised coordinator had no checkout at its
``repo_path``; the sheet, built never to raise, quietly said nothing, and the
planner's checker let through an invented ``is_deleted`` column although the
repository already soft-deletes users with ``deleted_at``.

The helper cases run the sandbox helper's REAL HTTP server and its real
``/code`` route handlers in-process, on an ephemeral loopback port, over a real
git repository; the reader reaches it through the same composition the
coordinator uses (``compose_planning_git_runner`` → ``RepoRoutedGitRunner`` →
``SidecarGitRunner``). Nothing on the wire is faked.
"""

from __future__ import annotations

import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge.cli._serve_planning import compose_planning_git_runner
from forge.config.models import ForgeConfig, PlanningConfig, SandboxEntry
from forge.deploy_sidecar.service import build_server
from forge.planning.driver import PlanningRunDriver
from forge.planning.repository_facts import (
    UNAVAILABLE_PREFIX,
    LocalCheckoutReader,
    models_in_python_file,
    read_repository_facts,
)
from forge.planning.sidecar_git_runner import SidecarCodeReader

REPO_KEY = "guardkit/api_test"

#: Where the containerised coordinator's settings say the repository is, and
#: where on 1 October nothing was.
COORDINATOR_PATH = "/var/lib/forge/projects/api_test"

SENTENCE = (
    "Add a GET /users/created-per-day endpoint that returns the number of "
    "users created on each of the last 7 days, oldest first."
)

ROUTER = '''
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/count-today", response_model=UserCount)
async def count_today(db: AsyncSession = Depends(get_db)) -> UserCount:
    ...
'''

#: A users model that already soft-deletes.
MODELS = '''
from datetime import datetime


class Base:
    pass


class User(Base):
    """The users table."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return self.email


class Invoice(Base):
    __tablename__ = "invoices"

    total: Mapped[int] = mapped_column()
'''


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed"],
        check=True,
    )
    return root


@pytest.fixture
def helper(tmp_path: Path):
    """The sandbox helper's real server over a real repository that soft-deletes
    its users. Yields its address."""
    clone = _repo(
        tmp_path / "sandbox-clone",
        {
            "src/users/router.py": ROUTER,
            "src/users/models.py": MODELS,
            "db/migrations/0001_create_users.sql": "CREATE TABLE users (id text);\n",
            "db/migrations/0002_add_deleted_at_to_users.sql": "ALTER TABLE users ADD deleted_at timestamptz;\n",
            "db/migrations/0003_create_invoices.sql": "CREATE TABLE invoices (total int);\n",
        },
    )
    cfg = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: str(clone)}},
        }
    )
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _closed_port() -> int:
    """A loopback port nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _driver_over(planning: PlanningConfig) -> PlanningRunDriver:
    """A driver whose git runner is the coordinator's own composition."""
    git_runner, _resolver = compose_planning_git_runner(planning)
    return PlanningRunDriver(SimpleNamespace(git_runner=git_runner))  # type: ignore[arg-type]


def _sandboxed(url: str) -> PlanningConfig:
    return PlanningConfig(
        enabled=True,
        target_repo_paths={REPO_KEY: COORDINATOR_PATH},
        sandboxes={REPO_KEY: SandboxEntry(name="api-test-sbx", sidecar_url=url, runner_url=url)},
    )


# ---------------------------------------------------------------------------
# (a) No checkout on the coordinator, the helper answering: the facts come
#     from the helper
# ---------------------------------------------------------------------------


def test_a_sandboxed_repository_is_read_through_the_helpers_code_routes(helper: str) -> None:
    assert not Path(COORDINATOR_PATH).exists()
    driver = _driver_over(_sandboxed(helper))

    reader = driver._repository_reader_for(COORDINATOR_PATH)
    assert isinstance(reader, SidecarCodeReader)
    facts = read_repository_facts(reader, SENTENCE)

    assert facts.unavailable is None
    sheet = facts.text or ""
    # The routes beside the one asked for, read by the helper.
    assert "`src/users/router.py` defines GET /users/count-today." in sheet
    assert "None of them declares an authentication dependency." in sheet
    # The model the request touches, with the column that already soft-deletes.
    assert "`src/users/models.py` declares class User(Base)." in sheet
    assert "__tablename__ = 'users'" in sheet
    assert "deleted_at: Mapped[datetime | None]" in sheet
    # A model the request does not touch is not on the sheet.
    assert "Invoice" not in sheet
    # The migrations, by name only.
    assert "The repository has 3 migration file(s)" in sheet
    assert "`db/migrations/0001_create_users.sql`" in sheet
    assert "`db/migrations/0002_add_deleted_at_to_users.sql`" in sheet
    assert "0003_create_invoices" not in sheet
    assert "Their contents were not read." in sheet

    # The control: the coordinator's own path — what the sheet read before —
    # has nothing at it, and says so rather than going quiet.
    local = read_repository_facts(LocalCheckoutReader(COORDINATOR_PATH), SENTENCE)
    assert local.unavailable == f"there is no checkout at {COORDINATOR_PATH} where the planner runs"


def test_a_repository_without_a_sandbox_is_still_read_from_the_coordinators_checkout(
    tmp_path: Path,
) -> None:
    checkout = _repo(tmp_path / "checkout", {"src/users/models.py": MODELS})
    driver = _driver_over(
        PlanningConfig(enabled=True, target_repo_paths={REPO_KEY: str(checkout)})
    )
    reader = driver._repository_reader_for(str(checkout))
    assert isinstance(reader, LocalCheckoutReader)
    assert "deleted_at" in (read_repository_facts(reader, SENTENCE).text or "")


# ---------------------------------------------------------------------------
# (b) No checkout and the helper unreachable: an explicit unavailable state
# ---------------------------------------------------------------------------


def test_an_unreachable_helper_is_said_never_silenced() -> None:
    url = f"http://127.0.0.1:{_closed_port()}"
    driver = _driver_over(_sandboxed(url))

    facts = read_repository_facts(driver._repository_reader_for(COORDINATOR_PATH), SENTENCE)

    assert facts.sheet is None
    assert facts.unavailable is not None
    assert facts.unavailable.startswith(
        f"the sandbox helper at {url} could not be reached for /code/list-files"
    )
    text = facts.text or ""
    assert text.startswith(UNAVAILABLE_PREFIX + "the sandbox helper at ")
    assert "that is not evidence that it has nothing" in text


def test_a_helper_that_refuses_the_repository_is_said_too(helper: str) -> None:
    """Reachable, but it does not know this repository: still unavailable,
    with the helper's own reason."""
    reader = SidecarCodeReader(helper, repo="guardkit/not-a-repo")
    facts = read_repository_facts(reader, SENTENCE)
    assert facts.unavailable is not None
    assert f"the sandbox helper at {helper} answered 400 to /code/list-files" in facts.unavailable


def test_a_request_with_no_words_reads_nothing_and_says_nothing() -> None:
    url = f"http://127.0.0.1:{_closed_port()}"
    facts = read_repository_facts(SidecarCodeReader(url, repo=REPO_KEY), "   ")
    assert facts.text is None and facts.unavailable is None


# ---------------------------------------------------------------------------
# (c) A models file with a soft-delete column appears on the sheet
# ---------------------------------------------------------------------------


def test_a_soft_delete_column_is_on_the_sheet_without_any_route(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "checkout", {"src/users/models.py": MODELS})
    sheet = read_repository_facts(
        LocalCheckoutReader(str(checkout)), "Let a user be removed without losing their history."
    ).text
    assert sheet is not None
    assert "`src/users/models.py` declares class User(Base)." in sheet
    assert "deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)" in sheet
    assert "Fields it inherits were not read." in sheet
    assert "__repr__" not in sheet


def test_a_model_is_found_by_the_name_its_body_sets_as_well_as_its_own() -> None:
    """Any framework's spelling: a class named otherwise whose own body sets
    the request's word, and plain assignments as well as annotated ones."""
    source = (
        "class Account(models.Model):\n"
        "    email = models.EmailField()\n"
        "    deleted_at = models.DateTimeField(null=True)\n"
        "    class Meta:\n"
        "        pass\n"
        "    __collection__ = 'users'\n"
    )
    found = models_in_python_file(source, ["users"])
    assert [m.name for m in found] == ["Account"]
    assert "deleted_at = models.DateTimeField(null=True)" in found[0].fields
    assert models_in_python_file(source, ["invoices"]) == []
    assert models_in_python_file("class broken(:", ["users"]) == []


def test_the_project_can_say_where_its_models_and_migrations_are(tmp_path: Path) -> None:
    """Declared paths are used instead of the path words, so a project whose
    migrations live in a folder no path word names is still read."""
    checkout = _repo(
        tmp_path / "checkout",
        {
            ".guardkit/config.yaml": (
                "repository_facts:\n"
                "  data_models: [\"app/db/**\"]\n"
                "  migrations: [\"revisions\"]\n"
            ),
            "app/db/people.py": MODELS,
            "revisions/a1_add_deleted_at_to_users.py": "x = 1\n",
            "revisions/b2_create_invoices.py": "x = 1\n",
        },
    )
    sheet = read_repository_facts(LocalCheckoutReader(str(checkout)), SENTENCE).text or ""
    assert "`app/db/people.py` declares class User(Base)." in sheet
    assert "The repository has 2 migration file(s), declared by the project at `revisions`." in sheet
    assert "`revisions/a1_add_deleted_at_to_users.py`" in sheet


def test_the_sheet_is_bounded(tmp_path: Path) -> None:
    fields = "\n".join(f"    column_{i:03d}: Mapped[str] = mapped_column(String(200), nullable=True)" for i in range(400))
    models = "class User:\n    __tablename__ = 'users'\n" + fields + "\n"
    checkout = _repo(
        tmp_path / "checkout",
        {f"src/models/user_{n}.py": models for n in range(5)},
    )
    sheet = read_repository_facts(LocalCheckoutReader(str(checkout)), SENTENCE).text or ""
    assert 0 < len(sheet) <= 6000


# ---------------------------------------------------------------------------
# (d) A repository that is not Python yields only what can be proven
# ---------------------------------------------------------------------------


def test_a_non_python_repository_yields_only_provable_facts(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {
            "src/routes/users.ts": (
                'router.get("/users/count-today", countToday);\n'
                'router.delete("/users/:id", requireAuth, remove);\n'
            ),
            "src/models/user.ts": "export class User {\n  deletedAt?: Date;\n  email!: string;\n}\n",
            "db/migrate/20240101_add_deleted_at_to_users.rb": "class AddDeletedAt; end\n",
            "README.md": "The users model lives in src/models.\n",
        },
    )
    sheet = read_repository_facts(LocalCheckoutReader(str(checkout)), SENTENCE).text or ""

    assert "`src/routes/users.ts` mentions /users, /users/count-today." in sheet
    assert "whether those routes require authentication and what they return was not read" in sheet
    assert (
        "`src/models/user.ts` looks like a data-model file by its path, and its "
        "path names user. It is not Python, so what it declares was not read."
    ) in sheet
    assert "`db/migrate/20240101_add_deleted_at_to_users.rb`" in sheet
    # Nothing is read out of a file the sheet cannot parse: no field, no
    # class, no guess about authentication.
    assert "deletedAt" not in sheet
    assert "declares class" not in sheet
    assert "requireAuth" not in sheet
    assert "README" not in sheet
