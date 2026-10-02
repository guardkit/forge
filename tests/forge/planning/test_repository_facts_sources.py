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
    RepositoryUnreadable,
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


def _repo(root: Path, files: dict[str, str], *, exist_ok: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=exist_ok)
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


# ---------------------------------------------------------------------------
# The plan-writer's repository inventory and the search for the
# specification's words go the same way (release -3 item 10, follow-up). On
# 1 October both read the coordinator's missing checkout and went quiet:
# "planning without the repository inventory".
# ---------------------------------------------------------------------------

SPEC = "Feature: users\n  Scenario: count\n    When I send GET /users/count-today\n"


def test_the_inventory_and_the_specs_words_are_read_through_the_helper(helper: str) -> None:
    reasons: list[str] = []
    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        REPO_KEY,
        COORDINATOR_PATH,
        SPEC,
        reader=SidecarCodeReader(helper, repo=REPO_KEY),
        unavailable=reasons,
    )
    assert reasons == []
    assert "src/users/models.py" in descriptor["repository_inventory"]["files"]
    places = [p for row in descriptor["where_the_specs_words_already_appear"] for p in row["already_in"]]
    assert any(p.startswith("src/users/router.py:") for p in places)

    # The control: what the descriptor read before — the coordinator's own
    # path — has nothing at it, and now says so instead of going quiet.
    control: list[str] = []
    before = PlanningRunDriver._build_target_repo_descriptor(
        REPO_KEY, COORDINATOR_PATH, SPEC, unavailable=control
    )
    assert "repository_inventory" not in before
    assert control and control[0] == f"there is no checkout at {COORDINATOR_PATH} where the planner runs"


def test_an_unreachable_helper_is_said_for_the_inventory_and_the_words() -> None:
    url = f"http://127.0.0.1:{_closed_port()}"
    reasons: list[str] = []
    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        REPO_KEY, COORDINATOR_PATH, SPEC,
        reader=SidecarCodeReader(url, repo=REPO_KEY),
        unavailable=reasons,
    )
    assert "repository_inventory" not in descriptor
    assert "where_the_specs_words_already_appear" not in descriptor
    # The first failure on the wire makes the helper unavailable for the rest
    # of the pass: every later read says the same thing at once.
    assert reasons and all(
        r.startswith(f"the sandbox helper at {url} could not be reached for /code/list-files")
        for r in reasons
    )
    # No field the plan-writer's schema does not define.
    assert set(descriptor) <= {"repo", "test_roots", "architecture_rules"}


@pytest.mark.asyncio
async def test_the_plan_writer_is_told_when_only_the_descriptor_could_not_read(helper: str) -> None:
    """The fact sheet read fine, the specification's words could not be
    searched: the plan-writer's repository_facts carries both."""

    class _SearchRefused(SidecarCodeReader):
        def places_mentioning(self, text: str) -> list[str]:
            raise RepositoryUnreadable("the helper refused the search")

    reader = _SearchRefused(helper, repo=REPO_KEY)
    reasons: list[str] = []
    PlanningRunDriver._build_target_repo_descriptor(
        REPO_KEY, COORDINATOR_PATH, SPEC, reader=reader, unavailable=reasons
    )
    assert reasons == ["the helper refused the search"]

    driver = PlanningRunDriver(
        SimpleNamespace(git_runner=SimpleNamespace(code_reader=lambda: reader))  # type: ignore[arg-type]
    )
    driver._descriptor_unavailable = {"cid-1": reasons[0]}
    given = await driver._plan_repository_facts(
        "cid-1", COORDINATOR_PATH, {"request_text": SENTENCE}
    )
    assert given is not None
    assert "`src/users/models.py` declares class User(Base)." in given
    assert given.endswith(
        "Repository facts unavailable: the helper refused the search. Nothing on "
        "this sheet says what the repository already has, and that is not "
        "evidence that it has nothing: do not add tables, columns, routes or "
        "files the request does not name on the strength of this silence."
    )
    assert driver._repository_unavailable_line("cid-1") == (
        "The machine could not read the repository while writing this, so "
        "nothing checked it against what the repository already has (the "
        "helper refused the search)."
    )


# ---------------------------------------------------------------------------
# The coach's findings (release -3 item 10, fix pass)
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402



@contextlib.contextmanager
def _serving(clone: Path):
    """The helper's real server over ``clone``; yields its address."""
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


#: 250 lines mentioning the route — more than the helper's 200-line cap —
#: in a file that sorts BEFORE the router (the coach's reproducer).
NOISY_CLIENT = "".join(f'URL_{i} = "/users/thing-{i}"\n' for i in range(250))

ROUTER_UNDER_SRC = '''
from fastapi import APIRouter

router = APIRouter(prefix="/users")


@router.get("/count-today")
async def count_today() -> int:
    ...
'''


def test_a_search_cut_at_the_helpers_cap_still_finds_the_router(tmp_path: Path) -> None:
    clone = _repo(
        tmp_path / "clone",
        {"a/client_api.py": NOISY_CLIENT, "src/users/router.py": ROUTER_UNDER_SRC},
    )
    with _serving(clone) as url:
        reader = SidecarCodeReader(url, repo=REPO_KEY)
        sheet = read_repository_facts(reader, "Add GET /users/created-per-day").text or ""
    assert "`src/users/router.py` defines GET /users/count-today." in sheet
    # Everything the cap left out was recovered (the file it fell inside was
    # read whole), so nothing is reported missing.
    assert "incomplete" not in sheet


def test_a_search_that_cannot_be_recovered_keeps_what_it_found_and_says_so(tmp_path: Path) -> None:
    """Sixty files of ten matches each at the top of the repository: the cap
    falls in the twentieth, and recovering the other forty is more than one
    search may spend. The places found are kept; the answer says it is
    incomplete."""
    files = {f"f{n:02d}.py": "".join(f'U = "/users/{n}-{i}"\n' for i in range(10)) for n in range(60)}
    clone = _repo(tmp_path / "clone", files)
    with _serving(clone) as url:
        places = SidecarCodeReader(url, repo=REPO_KEY).places_mentioning("/users")
    assert len(places) >= 200
    assert places.cut is not None and "too many pieces to recover" in places.cut and "were not searched" in places.cut


def test_a_timed_out_search_and_a_cut_listing_are_said() -> None:
    """The helper's own flags, answered by a stand-in on the wire."""

    def post(url: str, body: dict, timeout: float) -> tuple[int, dict]:
        if url.endswith("/code/list-files"):
            return 200, {"files": ["src/a.py"], "capped": True, "total_tracked": 9000}
        return 200, {"matches": [], "timed_out": True, "files_searched": 17, "capped": False}

    reader = SidecarCodeReader("http://helper", repo=REPO_KEY, post=post)
    sheet = read_repository_facts(reader, "Add GET /users/created-per-day").text or ""
    assert "the helper listed only the first 1 of 9000 tracked files" in sheet
    assert "stopped at the helper's time limit after 17 file(s)" in sheet
    reasons: list[str] = []
    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        REPO_KEY, COORDINATOR_PATH, "", reader=reader, unavailable=reasons
    )
    assert "repository_inventory" not in descriptor
    assert "the helper listed only the first 1 of 9000 tracked files" in reasons


def test_the_readers_wire_waits_longer_than_the_helpers_own_walls() -> None:
    from forge.deploy_sidecar.service import (
        CODE_GIT_TIMEOUT_SECONDS,
        CODE_SEARCH_TIMEOUT_SECONDS,
    )

    reader = SidecarCodeReader("http://helper", repo=REPO_KEY)
    assert reader._timeout_s > CODE_GIT_TIMEOUT_SECONDS + CODE_SEARCH_TIMEOUT_SECONDS


def _long_router(prefix: str) -> str:
    routes = "".join(
        f'@router.get("/a-rather-long-and-descriptive-route-name-number-{i}")\n'
        f"async def r{i}() -> SomeRatherLongResponseModelName{i}:\n    ...\n\n"
        for i in range(12)
    )
    return f'from fastapi import APIRouter\nrouter = APIRouter(prefix="{prefix}")\n\n' + routes


def test_long_route_facts_never_push_the_model_facts_out(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {
            "src/users/router.py": _long_router("/users"),
            "src/users/api_v2.py": _long_router("/users"),
            "src/users/views.py": _long_router("/users"),
            "src/users/models.py": MODELS,
        },
    )
    sheet = read_repository_facts(LocalCheckoutReader(str(checkout)), SENTENCE).text or ""
    assert len(sheet) <= 6000
    assert "deleted_at: Mapped[datetime | None]" in sheet
    assert "more line(s) about routes were left out to keep the sheet short" in sheet


def test_route_paths_are_capped_and_one_first_segment_is_searched_once(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "checkout", {"src/users/router.py": ROUTER})

    class Counting(LocalCheckoutReader):
        searched: list[str] = []

        def files_mentioning(
            self, text: str, *, ignore_case: bool = False, relevant=None
        ) -> list[str]:
            self.searched.append(text)
            return super().files_mentioning(text, ignore_case=ignore_case, relevant=relevant)

    reader = Counting(str(checkout))
    read_repository_facts(
        reader,
        "Add /users/a and /users/b, then /orders/x, /items/y and /carts/z.",
    )
    route_searches = [t for t in reader.searched if t.startswith('"/')]
    assert route_searches.count('"/users') == 1
    assert '"/carts' not in route_searches  # the fourth path is past the cap


def test_a_declared_model_path_that_matches_nothing_is_said(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {
            ".guardkit/config.yaml": "repository_facts:\n  data_models: [\"app/entities/**\"]\n",
            "src/users/models.py": MODELS,
        },
    )
    sheet = read_repository_facts(LocalCheckoutReader(str(checkout)), SENTENCE).text or ""
    assert (
        "The project declares its data models at `app/entities/**`, and no "
        "tracked file is there, so no model was read."
    ) in sheet


def test_files_in_the_repositorys_test_folders_are_not_read_as_route_files(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {
            "src/users/router.py": ROUTER,
            "tests/test_api_documentation.py": 'DOCS = "/users/count-today"\n',
            # Not Python: before the fix its route literals reached the sheet.
            "tests/users/users_api_client.ts": 'get("/users/count-today");\n',
        },
    )
    sheet = read_repository_facts(LocalCheckoutReader(str(checkout)), SENTENCE).text or ""
    assert "src/users/router.py" in sheet
    assert "tests/" not in sheet


RULES = "rules:\n  - id: R-1\n    rule: Routers stay thin; queries live in the data layer.\n"


def test_the_rules_file_and_the_test_folders_come_through_the_helper(tmp_path: Path) -> None:
    clone = _repo(
        tmp_path / "clone",
        {
            "docs/architecture-rules.yaml": RULES,
            "src/users/router.py": ROUTER,
            "tests/users/test_router.py": "def test_it():\n    pass\n",
            "tests/health/test_health.py": "def test_it():\n    pass\n",
        },
    )
    assert not Path(COORDINATOR_PATH).exists()
    with _serving(clone) as url:
        reasons: list[str] = []
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            REPO_KEY,
            COORDINATOR_PATH,
            "",
            reader=SidecarCodeReader(url, repo=REPO_KEY),
            unavailable=reasons,
        )
    assert reasons == []
    assert descriptor["test_roots"] == ["tests/health", "tests/users"]
    assert [r["id"] for r in descriptor["architecture_rules"]["rules"]] == ["R-1"]

    # The control: the coordinator's own path has neither.
    before = PlanningRunDriver._build_target_repo_descriptor(REPO_KEY, COORDINATOR_PATH, "")
    assert before["test_roots"] == [] and "architecture_rules" not in before


def test_an_unreachable_helper_is_said_for_the_rules_and_the_test_folders() -> None:
    url = f"http://127.0.0.1:{_closed_port()}"
    reasons: list[str] = []
    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        REPO_KEY, COORDINATOR_PATH, "", reader=SidecarCodeReader(url, repo=REPO_KEY), unavailable=reasons
    )
    assert descriptor["test_roots"] == [] and "architecture_rules" not in descriptor
    # Said for the test folders AND the rules, without a second wait.
    assert len(reasons) >= 2
    assert all("could not be reached for /code/list-files" in r for r in reasons)


# ---------------------------------------------------------------------------
# Review round 1 (Codex R1, R2) and the re-check coach's three findings
# ---------------------------------------------------------------------------


def test_a_cap_inside_a_root_level_file_is_recovered_not_dropped(tmp_path: Path) -> None:
    """Codex R1: the only file sits at the top of the repository and holds
    250 matches; the helper stops at 200 inside it. The rest of the file is
    searched here, whole, and nothing is reported missing because nothing
    is."""
    clone = _repo(tmp_path / "clone", {"client_api.py": NOISY_CLIENT})
    with _serving(clone) as url:
        places = SidecarCodeReader(url, repo=REPO_KEY).places_mentioning("/users")
    assert len(places) == 250
    assert places.cut is None


def test_a_term_past_the_helpers_500_characters_is_still_found(tmp_path: Path) -> None:
    """Codex R2: the helper searches only the first 500 characters of a line.
    The long line's file is read whole here, so the term is found."""
    long_line = "x" * 600 + ' "/users/hidden-route"\n'
    clone = _repo(tmp_path / "clone", {"src/users/long.py": "A = 1\n" + long_line})
    with _serving(clone) as url:
        places = SidecarCodeReader(url, repo=REPO_KEY).places_mentioning("hidden-route")
    assert list(places) == ["src/users/long.py:2"] and places.cut is None


def test_long_lines_that_cannot_all_be_listed_are_said(tmp_path: Path) -> None:
    lines = "".join("y" * 600 + "\n" for _ in range(210))
    clone = _repo(tmp_path / "clone", {"src/blob.py": lines, "src/users/router.py": ROUTER_UNDER_SRC})
    with _serving(clone) as url:
        places = SidecarCodeReader(url, repo=REPO_KEY).places_mentioning("count-today")
    assert "src/users/router.py:7" in places
    assert places.cut is not None and "longer than 500 characters" in places.cut


def test_folders_are_resumed_by_the_helpers_full_path_order(tmp_path: Path) -> None:
    """Re-check 1 (the coach's orderdemo): the helper walks ``api-client/``
    BEFORE ``api/`` (full paths, sorted), so a cap inside ``api-client/``
    must still lead to ``api/`` being searched."""
    clone = _repo(
        tmp_path / "clone",
        {
            "a/client.py": "".join(f'U{i} = "/users/x{i}"\n' for i in range(150)),
            "api-client/client.py": "".join(f'V{i} = "/users/y{i}"\n' for i in range(100)),
            "api/routes.py": 'router = APIRouter(prefix="/users")\n@router.get("/count-today")\nasync def f(): ...\n',
        },
    )
    with _serving(clone) as url:
        sheet = read_repository_facts(
            SidecarCodeReader(url, repo=REPO_KEY), "Add GET /users/created-per-day"
        ).text or ""
    assert "`api/routes.py` defines GET /users/count-today." in sheet


def test_a_partly_searched_spec_word_keeps_its_places_and_is_not_unreadable(tmp_path: Path) -> None:
    """Re-check 2: a word the helper could not search in full keeps what was
    found and is named as partly read; the other words are kept too, and
    nothing says the repository could not be read."""
    files = {f"tests/f{n:02d}.py": "".join(f"full_name_{i} = 1\n" for i in range(10)) for n in range(60)}
    files["src/users/models.py"] = MODELS + "\nfull_name = 1\n"
    files["src/users/router.py"] = ROUTER_UNDER_SRC
    clone = _repo(tmp_path / "clone", files)
    spec = "Feature: x\n  Scenario: y\n    Given the full_name and /users/count-today\n"
    reasons: list[str] = []
    partial: list[str] = []
    with _serving(clone) as url:
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            REPO_KEY, COORDINATOR_PATH, spec,
            reader=SidecarCodeReader(url, repo=REPO_KEY),
            unavailable=reasons, partial=partial,
        )
    assert reasons == []
    words = {row["words"]: row["already_in"] for row in descriptor["where_the_specs_words_already_appear"]}
    assert "/users/count-today" in words
    assert "full_name" in words
    assert partial and partial[0].startswith("where `full_name` already appears was only partly searched")


def test_a_hung_helper_is_waited_on_once_per_run_and_within_the_allowance() -> None:
    """Re-check 3 (the coach's hung.py): every request times out. The first
    failure makes the helper unavailable for the rest of the run, so the
    description and the fact sheet together cost ONE wait, and that wait is
    never longer than the allowance."""
    from forge.planning.sidecar_git_runner import FACT_GATHERING_BUDGET_S

    calls: list[tuple[str, float]] = []

    def post(url: str, body: dict, timeout: float) -> tuple[int, dict]:
        calls.append((url.rsplit("/", 1)[-1], timeout))
        raise TimeoutError("timed out")

    reader = SidecarCodeReader("http://h:1", repo=REPO_KEY, post=post)
    driver = PlanningRunDriver(
        SimpleNamespace(git_runner=SimpleNamespace(code_reader=lambda: reader))  # type: ignore[arg-type]
    )
    shared = driver._repository_reader_for(COORDINATOR_PATH, "cid-1")
    assert driver._repository_reader_for(COORDINATOR_PATH, "cid-1") is shared
    reasons: list[str] = []
    PlanningRunDriver._build_target_repo_descriptor(
        REPO_KEY, COORDINATOR_PATH, "Given deleted_at and /users/x\n", reader=shared, unavailable=reasons
    )
    facts = read_repository_facts(shared, "Add GET /users/created-per-day counting deleted_at")
    assert len(calls) == 1
    assert calls[0][1] <= FACT_GATHERING_BUDGET_S
    assert facts.unavailable is not None and "could not be reached" in facts.unavailable
    assert (facts.text or "").startswith("Repository facts unavailable:")


def test_the_allowance_running_out_keeps_what_was_read() -> None:
    """A slow helper: the clock passes the allowance part-way. The facts read
    before that stay on the sheet and the sheet says the rest was not read."""
    now = [0.0]

    def post(url: str, body: dict, timeout: float) -> tuple[int, dict]:
        now[0] += 50.0
        if url.endswith("/code/list-files"):
            return 200, {"files": ["src/users/router.py", "src/users/models.py"]}
        if url.endswith("/code/search"):
            return 200, {"matches": [{"path": "src/users/router.py", "line": 3}]}
        return 200, {"content": ROUTER_UNDER_SRC if "router" in body["path"] else MODELS}

    reader = SidecarCodeReader("http://h:1", repo=REPO_KEY, post=post, clock=lambda: now[0])
    facts = read_repository_facts(reader, "Add GET /users/created-per-day")
    assert facts.unavailable is None
    assert "`src/users/router.py` defines GET /users/count-today." in (facts.text or "")
    assert "allowance for reading the repository" in (facts.text or "")
    assert facts.partial is not None


def test_a_capped_search_finished_from_a_capped_listing_stays_partial(tmp_path: Path) -> None:
    """Codex round 2, R3: 5,000 non-matching files under ``a/`` fill the
    helper's listing, so ``y/`` and ``z/`` are not on it; the search walks
    every tracked file and stops at 200 matches inside ``y/client_api.py``.
    Nothing past the listing can be proven searched, so the answer is
    partial — and the spec word is marked partly searched."""
    root = tmp_path / "clone"
    (root / "a").mkdir(parents=True)
    for n in range(5000):
        (root / "a" / f"f{n:04d}.txt").write_text("nothing\n", encoding="utf-8")
    _repo(
        root,
        {"y/client_api.py": NOISY_CLIENT, "z/router.py": 'X = "/users/z"\n'},
        exist_ok=True,
    )
    with _serving(root) as url:
        reader = SidecarCodeReader(url, repo=REPO_KEY)
        places = reader.places_mentioning("/users")
        assert reader.listing_cut is not None
        assert "z/router.py:1" in places or places.cut is not None
        assert places.cut is not None and "file list" in places.cut

        partial: list[str] = []
        reasons: list[str] = []
        PlanningRunDriver._where_the_specs_words_already_appear(
            COORDINATOR_PATH,
            "Given /users\n",
            reader=SidecarCodeReader(url, repo=REPO_KEY),
            unavailable=reasons,
            partial=partial,
        )
    assert reasons == []
    assert any(p.startswith("where `/users` already appears was only partly searched") for p in partial)


# ---------------------------------------------------------------------------
# Codex round 3, R4: a file the sheet chose to read and the helper refused is
# said by name and reason — "could not read" when nothing else was learned,
# "read only in part" when other facts remain.
# ---------------------------------------------------------------------------

#: A users model over the helper's 262,144-byte limit for one file.
HUGE_MODELS = MODELS + "\n# " + "x" * 270_000 + "\n"


def test_a_refused_model_with_nothing_else_read_is_unavailable(tmp_path: Path) -> None:
    clone = _repo(tmp_path / "clone", {"src/users/models.py": HUGE_MODELS})
    with _serving(clone) as url:
        facts = read_repository_facts(SidecarCodeReader(url, repo=REPO_KEY), "Show users")
    assert facts.sheet is None
    assert facts.unavailable is not None
    assert facts.unavailable.startswith(
        "the files that matter for this request could not be read: "
        "`src/users/models.py` could not be read (the helper answered 400:"
    )
    assert (facts.text or "").startswith("Repository facts unavailable: the files that matter")


def test_a_refused_model_beside_a_readable_route_is_partial(tmp_path: Path) -> None:
    clone = _repo(
        tmp_path / "clone",
        {"src/users/models.py": HUGE_MODELS, "src/users/router.py": ROUTER_UNDER_SRC},
    )
    with _serving(clone) as url:
        facts = read_repository_facts(
            SidecarCodeReader(url, repo=REPO_KEY), "Show users at /users/count-today"
        )
    assert facts.unavailable is None
    sheet = facts.text or ""
    assert "`src/users/router.py` defines GET /users/count-today." in sheet
    assert "Not read, so this sheet is incomplete: `src/users/models.py` could not be read" in sheet
    assert facts.partial is not None and "src/users/models.py" in facts.partial


def test_a_local_checkout_says_why_a_file_was_not_read(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "checkout", {"src/users/models.py": HUGE_MODELS})
    facts = read_repository_facts(LocalCheckoutReader(str(checkout)), "Show users")
    assert facts.unavailable is not None
    assert "over the 262144-byte limit for one file" in facts.unavailable


def test_a_tracked_rules_file_the_helper_refuses_is_said(tmp_path: Path) -> None:
    clone = _repo(
        tmp_path / "clone",
        {"docs/architecture-rules.yaml": RULES + "# " + "x" * 270_000 + "\n", "src/a.py": "x = 1\n"},
    )
    reasons: list[str] = []
    partial: list[str] = []
    with _serving(clone) as url:
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            REPO_KEY, COORDINATOR_PATH, "",
            reader=SidecarCodeReader(url, repo=REPO_KEY),
            unavailable=reasons, partial=partial,
        )
    assert "architecture_rules" not in descriptor and reasons == []
    assert partial and partial[0].startswith(
        "the architecture rules file `docs/architecture-rules.yaml` could not be read (the helper answered 400:"
    )
