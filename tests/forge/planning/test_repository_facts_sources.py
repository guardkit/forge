"""Where the planner's fact sheet reads the repository, what it says about the
data models and migrations a request touches, and what it says when it cannot
read the repository at all (the 1 October planner fix, 1 October 2026).

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
# specification's words go the same way (the 1 October planner fix, follow-up). On
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
# The coach's findings (the 1 October planner fix, fix pass)
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


# ---------------------------------------------------------------------------
# Planning improvements (6 October 2026): the reader returns matching lines,
# the planner is shown the code ranked by the request's own words (item 1),
# and "all the X" lists the files most likely to hold the set (item 3).
# Neutral scratch repositories only.
# ---------------------------------------------------------------------------

from forge.planning.code_evidence import (  # noqa: E402
    LISTED_ALL_MEANS,
    MAX_EVIDENCE_FILES_PER_WORD,
    MAX_EVIDENCE_WINDOWS,
    MAX_SET_CANDIDATES_LISTED,
    quantified_phrases,
)
from forge.planning.sidecar_git_runner import PartialPlaces  # noqa: E402

#: A neutral request in the shape the evidence has to solve: the route's
#: prefix is declared apart from its path.
THING_REQUEST = (
    "Add a REMOVE /things/{thing_id} endpoint that returns 204 on success and "
    "404 for an unknown id, and make removed things disappear from all the "
    "tally reports."
)

#: The route itself, its prefix declared elsewhere, as many frameworks write it.
THING_ROUTER = (
    "from web import Router\n"
    "\n"
    'router = Router(prefix="/things")\n'
    "\n"
    "\n"
    "@router.remove(\n"
    '    "/{thing_id}",\n'
    "    status=204,\n"
    '    summary="Remove thing",\n'
    '    description="Removes a thing. Returns 204 on success.",\n'
    "    answers={404: 'Thing unknown'},\n"
    ")\n"
    "def remove_thing(thing_id):\n"
    "    store.remove(thing_id)\n"
)

#: A decoy that mentions the full path in a comment and sorts first by path.
THING_DECOY = "# See /things/{thing_id} in the router.\nVALUE = 1\n"


def test_a_local_checkout_returns_each_matching_line_with_its_text(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"a/one.txt": "alpha Tally here\nnothing\n", "b/two.py": "x = 'tally'\n"},
    )
    reader = LocalCheckoutReader(str(checkout))
    assert reader.lines_mentioning("tally") == [("b/two.py", 1, "x = 'tally'")]
    assert reader.lines_mentioning("tally", ignore_case=True) == [
        ("a/one.txt", 1, "alpha Tally here"),
        ("b/two.py", 1, "x = 'tally'"),
    ]


def test_a_local_reader_with_no_repository_refuses_lines(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    with pytest.raises(RepositoryUnreadable):
        LocalCheckoutReader(str(tmp_path / "plain")).lines_mentioning("x")


def test_the_helper_returns_each_matching_line_and_says_when_cut(tmp_path: Path) -> None:
    files = {f"f{n:02d}.py": "".join(f'U = "/things/{n}-{i}"\n' for i in range(10)) for n in range(60)}
    files["g.py"] = "Tally = 1\n"
    clone = _repo(tmp_path / "clone", files)
    with _serving(clone) as url:
        reader = SidecarCodeReader(url, repo=REPO_KEY)
        one = reader.lines_mentioning("tally", ignore_case=True)
        assert list(one) == [("g.py", 1, "Tally = 1")]
        assert getattr(one, "cut", None) is None
        many = reader.lines_mentioning("/things")
        files_answer = reader.files_mentioning("/things")
    assert len(many) >= 200 and all(len(row) == 3 for row in many)
    assert many.cut is not None and "were not searched" in many.cut
    assert files_answer.cut is not None


def _descriptor(reader, *, request: str = THING_REQUEST, spec: str = "", **kwargs):
    reasons: list[str] = []
    partial: list[str] = []
    descriptor = PlanningRunDriver._build_target_repo_descriptor(
        REPO_KEY, COORDINATOR_PATH, spec, reader=reader, unavailable=reasons,
        partial=partial, request_text=request, **kwargs,
    )
    return descriptor, reasons, partial


def test_the_request_is_searched_first_and_keeps_its_placeholders(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"src/things/router.py": THING_ROUTER, "src/things/report.py": "def tally_report(): ...\n"},
    )
    spec = "Feature: x\n  Scenario: y\n    Given the tally_report\n"
    descriptor, reasons, _ = _descriptor(LocalCheckoutReader(str(checkout)), spec=spec)
    entries = descriptor["where_the_specs_words_already_appear"]
    assert [entry["words"] for entry in entries] == ["/things/{thing_id}", "tally_report"]
    assert reasons == []


def test_the_route_is_found_by_its_last_segment_and_shown_as_a_window(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "checkout", {"src/things/router.py": THING_ROUTER})
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert "src/things/router.py:7" in entry["already_in"]
    window = entry["evidence"][0]
    # From 3 lines before the hit to 12 after, cut at the end of the file.
    assert (window["path"], window["first_line"], window["last_line"]) == ("src/things/router.py", 4, 14)
    assert window["text"].splitlines()[3] == '7:     "/{thing_id}",'
    assert "204" in window["text"] and "404" in window["text"]


def test_a_declaration_with_more_request_words_outranks_a_decoy_comment(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"a/notes.py": THING_DECOY, "src/things/router.py": THING_ROUTER},
    )
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    # The bare places keep their old order: the decoy sorts first.
    assert entry["already_in"][0] == "a/notes.py:1"
    # The evidence is ranked by the request's words: the route first.
    windows = entry["evidence"]
    assert windows[0]["path"] == "src/things/router.py"
    assert windows[0]["score"] > windows[-1]["score"]
    assert windows[-1]["path"] == "a/notes.py"


def test_the_files_read_for_one_word_are_bounded(tmp_path: Path) -> None:
    # Twelve files of one hit each: only ten are read, and each of them is
    # shown (the budget holds them; 6 October 2026, evidence coverage).
    files = {f"src/m{n:02d}.py": f"# /things/{{thing_id}} number {n}\n" for n in range(12)}
    checkout = _repo(tmp_path / "checkout", files)
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert len(entry["evidence"]) == MAX_EVIDENCE_FILES_PER_WORD == 10
    assert len({window["path"] for window in entry["evidence"]}) == 10
    # The two hits in files never read are counted: every hit not in a
    # window shown counts.
    assert entry["more_hits"] == 2
    assert len(entry["already_in"]) == 5


def test_no_more_than_the_window_limit_travels_in_all(tmp_path: Path) -> None:
    # Seven words of four hits each, every window small: 28 would fit the
    # size budget, the limit lets 24 travel, shared round by round. (No
    # placeholder: ``/{item_id}`` would be every word's spelling.)
    words = [f"/area{n}/items{n}" for n in range(7)]
    files = {
        f"src/area{n}.py": "".join(f"# {word} line {i}\n" + "\n" * 20 for i in range(4))
        for n, word in enumerate(words)
    }
    checkout = _repo(tmp_path / "checkout", files)
    descriptor, _, _ = _descriptor(
        LocalCheckoutReader(str(checkout)), request="Change " + " and ".join(words)
    )
    entries = descriptor["where_the_specs_words_already_appear"]
    assert sum(len(entry.get("evidence") or []) for entry in entries) == MAX_EVIDENCE_WINDOWS == 24
    assert [len(entry["evidence"]) for entry in entries] == [4, 4, 4, 3, 3, 3, 3]
    assert [entry.get("more_hits") for entry in entries] == [None, None, None, 1, 1, 1, 1]


def test_a_file_the_reader_refuses_keeps_its_place_and_says_why(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "checkout", {"src/things/router.py": THING_ROUTER})

    class Refusing(LocalCheckoutReader):
        def read_text(self, path: str) -> str | None:
            self.refused[path] = "it is not text"
            return None

    descriptor, _, _ = _descriptor(Refusing(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert entry["already_in"] == ["src/things/router.py:7"]
    assert "evidence" not in entry
    assert entry["more_hits"] == 1
    assert entry["evidence_not_read"] == ["`src/things/router.py` could not be read (it is not text)"]


def test_an_unreadable_repository_leaves_the_evidence_out_and_says_why(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    descriptor, reasons, _ = _descriptor(LocalCheckoutReader(str(tmp_path / "plain")))
    assert "where_the_specs_words_already_appear" not in descriptor
    assert any("is not a git repository" in reason for reason in reasons)


# -- item 3: "all the X" ----------------------------------------------------


def test_the_quantified_phrases_are_found_in_plain_grammar() -> None:
    assert quantified_phrases("Show one thing.") == []
    assert quantified_phrases(THING_REQUEST) == [
        ("all the tally reports", "tally", ["all", "the", "tally", "reports"])
    ]
    two = quantified_phrases("Fix every exporter. Then each of its import jobs, and all the rest of it.")
    # At most two phrases are kept.
    assert [(phrase, word) for phrase, word, _ in two] == [
        ("every exporter", "exporter"),
        ("each of its import jobs", "import"),
    ]
    # "all count endpoints" and "all the count endpoints" are the same set.
    assert quantified_phrases("hide them from all count endpoints")[0][:2] == (
        "all count endpoints",
        "count",
    )
    # A short first word names no set worth searching for.
    assert quantified_phrases("all the ids") == []


def test_a_phrase_stopped_short_by_a_number_is_not_a_set() -> None:
    assert quantified_phrases(
        "returns the number of users created on each of the last 7 days, oldest first."
    ) == []


#: A project implemented in a declarations file: the set's word and the
#: request's other words sit on separate lines.
ROUTES_YAML = (
    "routes:\n"
    "  - name: tally-by-day\n"
    "    path: /things/tally-by-day\n"
    "    removed: excluded\n"
    "  - name: unknown-things\n"
    "    path: /things/unknown\n"
)


def test_a_declarations_file_with_the_words_on_separate_lines_ranks_above_a_bare_mention(
    tmp_path: Path,
) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {
            "config/routes.yaml": ROUTES_YAML,
            "aaa/bare.txt": "tally\n",
            "tasks/TASK-1.md": "tally things removed\n",
            "qa/pass-bar-TASK-1.yaml": "tally things removed\n",
            ".guardkit/features/F.yaml": "tally\n",
        },
    )
    descriptor, reasons, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["sets_the_request_names"][0]
    assert reasons == []
    assert [c["path"] for c in entry["candidates"]] == ["config/routes.yaml", "aaa/bare.txt"]
    # Best line first: the one holding more of the request's words.
    assert entry["candidates"][0]["lines"] == ["3: path: /things/tally-by-day", "2: - name: tally-by-day"]
    assert entry["matched"] == 2 and entry["listed"] == 2 and entry["listed_all"] is True


def test_at_most_twenty_four_are_listed_and_then_the_list_is_not_all(tmp_path: Path) -> None:
    files = {f"src/f{n:02d}.py": f"tally = {n}\n" for n in range(30)}
    checkout = _repo(tmp_path / "checkout", files)
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["sets_the_request_names"][0]
    assert entry["matched"] == 30
    assert entry["listed"] == MAX_SET_CANDIDATES_LISTED == len(entry["candidates"])
    assert entry["listed_all"] is False
    assert entry["listed_all_means"] == LISTED_ALL_MEANS


class _FakeSetReader:
    """A reader whose file search answers as told, counting its reads."""

    where = "a stand-in"

    def __init__(self, files: dict[str, str], *, cut: str | None = None, dead: bool = False) -> None:
        self.files = files
        self.cut = cut
        self.dead = dead
        self.reads = 0
        self.refused: dict[str, str] = {}

    def list_files(self) -> list[str]:
        return sorted(self.files)

    def files_mentioning(self, text, *, ignore_case=False, relevant=None):
        if self.dead:
            raise RepositoryUnreadable("the stand-in could not be reached")
        answer = PartialPlaces(p for p, t in sorted(self.files.items()) if text in t.lower())
        answer.cut = self.cut
        return answer

    def places_mentioning(self, text):
        return []

    def read_text(self, path):
        self.reads += 1
        return self.files.get(path)


def test_a_cut_search_is_never_listed_all() -> None:
    reader = _FakeSetReader({"a.py": "tally\n"}, cut="the search stopped at the time limit")
    partial: list[str] = []
    sets = PlanningRunDriver._sets_the_request_names(THING_REQUEST, reader=reader, partial=partial)
    assert sets is not None and sets[0]["matched"] == 1 and sets[0]["listed_all"] is False
    assert partial == [
        "the files that hold `tally` were only partly searched (the search stopped at the time limit)"
    ]


def test_at_most_a_hundred_and_twenty_files_are_read_for_the_ranking() -> None:
    files = {f"f{n:03d}.py": "tally\n" for n in range(130)}
    reader = _FakeSetReader(files)
    sets = PlanningRunDriver._sets_the_request_names(THING_REQUEST, reader=reader)
    assert sets is not None
    assert reader.reads == 120
    assert sets[0]["not_read"] == 10 and sets[0]["matched"] == 130


def test_a_set_search_that_cannot_be_finished_is_said_as_partly_read() -> None:
    """Never the run-wide "could not read the repository": by then the
    inventory and the evidence were read. Said on the entry, and as read in
    part."""
    reasons: list[str] = []
    partial: list[str] = []
    sets = PlanningRunDriver._sets_the_request_names(
        THING_REQUEST, reader=_FakeSetReader({}, dead=True), unavailable=reasons, partial=partial
    )
    finished = (
        'the search for "all the tally reports" could not be finished: '
        "the stand-in could not be reached"
    )
    assert sets == [
        {
            "phrase": "all the tally reports",
            "looked_for": "tally",
            "candidates": [],
            "listed": 0,
            "matched": 0,
            "listed_all": False,
            "listed_all_means": LISTED_ALL_MEANS,
            "unavailable": finished,
        }
    ]
    assert reasons == []
    assert partial == [finished]


def test_a_request_with_no_set_gives_no_key(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "checkout", {"src/things/router.py": THING_ROUTER})
    descriptor, _, _ = _descriptor(
        LocalCheckoutReader(str(checkout)), request="Add a REMOVE /things/{thing_id} endpoint."
    )
    assert "sets_the_request_names" not in descriptor


# -- the descriptor key contract (shared with the specialist-agent side) ----


def test_the_descriptor_keys_the_plan_writer_and_its_checker_read(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"src/things/router.py": THING_ROUTER, "config/routes.yaml": ROUTES_YAML},
    )
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert set(entry) <= {"words", "already_in", "evidence", "more_hits", "evidence_not_read"}
    assert isinstance(entry["words"], str) and isinstance(entry["already_in"], list)
    for window in entry["evidence"]:
        assert set(window) == {"path", "first_line", "last_line", "score", "text"}
        assert isinstance(window["first_line"], int) and isinstance(window["last_line"], int)
        assert isinstance(window["score"], int)
        numbered = window["text"].splitlines()
        assert numbered[0].startswith(f"{window['first_line']}: ")
        assert numbered[-1].startswith(f"{window['last_line']}: ")
    (named,) = descriptor["sets_the_request_names"]
    assert set(named) <= {
        "phrase", "looked_for", "candidates", "listed", "matched", "listed_all",
        "listed_all_means", "not_read", "unavailable", "lines_trimmed",
    }
    assert {"phrase", "looked_for", "candidates", "listed", "matched", "listed_all"} <= set(named)
    assert isinstance(named["listed"], int) and isinstance(named["matched"], int)
    assert isinstance(named["listed_all"], bool)
    for candidate in named["candidates"]:
        assert set(candidate) == {"path", "lines"}
        assert len(candidate["lines"]) <= 2
        assert all(line.split(": ", 1)[0].isdigit() for line in candidate["lines"])
    # Either key is optional: a request without them sends neither.
    plain, _, _ = _descriptor(LocalCheckoutReader(str(checkout)), request="")
    assert "sets_the_request_names" not in plain


def test_a_reader_that_stops_while_ranking_keeps_what_it_found_and_says_so() -> None:
    class Stopping(_FakeSetReader):
        def read_text(self, path):
            if self.reads >= 2:
                raise RepositoryUnreadable("the stand-in stopped answering")
            return super().read_text(path)

    files = {f"f{n}.py": "tally\n" for n in range(4)}
    partial: list[str] = []
    sets = PlanningRunDriver._sets_the_request_names(
        THING_REQUEST, reader=Stopping(files), partial=partial
    )
    assert sets is not None
    entry = sets[0]
    assert entry["matched"] == 4 and entry["listed"] == 4
    assert entry["listed_all"] is False and entry["not_read"] == 2
    assert entry["unavailable"] == "the stand-in stopped answering"
    assert partial == [
        "the files that hold `tally` could not all be read for ranking "
        "(the stand-in stopped answering)"
    ]


# -- coach check 1 (6 October 2026) ----------------------------------------


def test_candidate_lines_come_from_the_texts_read_even_when_a_line_search_is_cut() -> None:
    """A line search the helper cuts short can no longer leave a listed
    candidate without its lines: they come from the text read for ranking."""

    class CutLines(_FakeSetReader):
        def lines_mentioning(self, text, *, ignore_case=False):
            answer = PartialPlaces([("f0.py", 1, "tally")])
            answer.cut = "the helper stopped at 200 matching lines"
            return answer

    files = {f"f{n}.py": "x = 1\ntally = 2\n" for n in range(5)}
    sets = PlanningRunDriver._sets_the_request_names(THING_REQUEST, reader=CutLines(files))
    assert sets is not None
    entry = sets[0]
    assert entry["listed_all"] is True
    assert [c["lines"] for c in entry["candidates"]] == [["2: tally = 2"]] * 5


def test_the_files_read_for_windows_are_chosen_by_hits_not_by_file_type(tmp_path: Path) -> None:
    """Eleven code files mention the route once; a declarations file
    mentions it three times. It is read and shown, though code sorts first."""
    files = {f"src/m{n:02d}.py": "# /things/{thing_id}\n" for n in range(11)}
    files["zz/routes.yaml"] = (
        "routes:\n  - path: /things/{thing_id}\n    remove: 204 on success\n"
        + "\n" * 20
        + "  - path: /things/{thing_id}\n"
        + "\n" * 20
        + "  - path: /things/{thing_id}\n"
    )
    checkout = _repo(tmp_path / "checkout", files)
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert entry["evidence"][0]["path"] == "zz/routes.yaml"


def test_hits_scored_but_not_shown_are_counted(tmp_path: Path) -> None:
    # Six hits far apart in one file, each window about 1,000 characters,
    # and a budget that holds three of them.
    body = "".join("# /things/{thing_id}\n" + ("x" * 60 + "\n") * 30 for _ in range(6))
    checkout = _repo(tmp_path / "checkout", {"src/far.py": body})
    found = PlanningRunDriver._where_the_specs_words_already_appear(
        COORDINATOR_PATH, "", reader=LocalCheckoutReader(str(checkout)),
        request_text=THING_REQUEST, evidence_chars=3200,
    )
    entry = found[0]
    assert len(entry["evidence"]) == 3
    assert sum(len(window["text"]) for window in entry["evidence"]) <= 3200
    assert entry["more_hits"] == 3
    # Only the private counts trim_to_budget reads and removes are extra.
    assert all(
        {key for key in window if not key.startswith("_")}
        == {"path", "first_line", "last_line", "score", "text"}
        for window in entry["evidence"]
    )


def test_a_phrase_followed_by_a_number_keeps_its_set_unless_the_number_comes_before_its_noun() -> None:
    assert quantified_phrases("make all the count endpoints 2x faster")[0][:2] == (
        "all the count endpoints",
        "count",
    )
    assert quantified_phrases("show all active users 30 days after signup")[0][:2] == (
        "all active users",
        "active",
    )
    assert quantified_phrases("users created on each of the last 7 days") == []


def test_the_code_shown_is_kept_within_one_size_budget() -> None:
    from forge.planning.code_evidence import MAX_EVIDENCE_CHARS, trim_to_budget

    def window(score: int, covers: int) -> dict:
        return {"path": "a.py", "first_line": 1, "last_line": 16, "score": score,
                "text": "x" * 3000, "_covers": covers}

    entries = [
        {"words": "/a", "already_in": [], "evidence": [window(8, 1), window(2, 2)]},
        {"words": "/b", "already_in": [], "evidence": [window(5, 1), window(2, 1)], "more_hits": 4},
    ]
    sets = [{
        "phrase": "all the x", "looked_for": "xxxx", "listed": 3, "matched": 3, "listed_all": True,
        "candidates": [{"path": f"c{n}", "lines": ["1: " + "y" * 1000, "2: " + "y" * 1000]} for n in range(3)],
    }]
    receipt = trim_to_budget(entries, sets)
    assert receipt == {
        "chars_before": 12000 + 6 * 1003,
        "chars_after": 9000 + 6 * 1003,
        "windows_trimmed": 1,
        "lines_trimmed": 0,
    }
    # The lowest-scoring window went first (the later word's, on a tie), and
    # its hit is counted.
    assert [w["score"] for w in entries[1]["evidence"]] == [5]
    assert entries[1]["more_hits"] == 5
    assert [w["score"] for w in entries[0]["evidence"]] == [8, 2]
    assert sets[0]["listed_all"] is True and "lines_trimmed" not in sets[0]
    assert all("_covers" not in w for e in entries for w in e.get("evidence") or [])

    # Each part keeps its half: the windows go down to 8,000 characters and
    # no further, then the lines go, second lines first, from the
    # lowest-ranked candidate up, and the entry is no longer listed_all.
    entries = [
        {"words": "/a", "already_in": [], "evidence": [window(8, 1), window(2, 2)]},
        {"words": "/b", "already_in": [], "evidence": [window(5, 1), window(2, 1)]},
    ]
    sets[0]["candidates"] = [
        {"path": f"c{n}", "lines": ["1: " + "y" * 3500, "2: " + "y" * 3500]} for n in range(3)
    ]
    receipt = trim_to_budget(entries, sets)
    assert receipt["chars_after"] <= MAX_EVIDENCE_CHARS
    assert receipt["windows_trimmed"] == 2
    assert [w["score"] for w in entries[0]["evidence"]] == [8]
    assert [w["score"] for w in entries[1]["evidence"]] == [5]
    assert entries[0]["more_hits"] == 2 and entries[1]["more_hits"] == 1
    assert [len(c["lines"]) for c in sets[0]["candidates"]] == [1, 1, 0]
    assert sets[0]["lines_trimmed"] == 4 and sets[0]["listed_all"] is False


def _worst_windows() -> list[dict]:
    """Twelve full windows: 16 lines of 200 characters each, 3 per word."""
    text = "\n".join(f"{n}: " + "w" * 200 for n in range(1, 17))
    return [
        {"words": f"/w{k}", "already_in": [], "evidence": [
            {"path": f"w{k}.py", "first_line": 1, "last_line": 16, "score": 10 - p,
             "text": text, "_covers": 1}
            for p in range(3)
        ]}
        for k in range(4)
    ]


def test_the_worst_case_sets_never_empty_the_top_windows() -> None:
    from forge.planning.code_evidence import MAX_EVIDENCE_CHARS, trim_to_budget

    entries = _worst_windows()
    sets = [
        {"phrase": f"all the s{k}", "looked_for": "xxxx", "listed": 24, "matched": 24,
         "listed_all": True,
         "candidates": [{"path": f"c{n}", "lines": ["1: " + "y" * 200, "2: " + "y" * 200]}
                        for n in range(24)]}
        for k in range(2)
    ]
    receipt = trim_to_budget(entries, sets)
    assert receipt["chars_after"] <= MAX_EVIDENCE_CHARS
    windows_left = sum(len(w["text"]) for e in entries for w in e.get("evidence") or [])
    assert MAX_EVIDENCE_CHARS // 2 - 3300 < windows_left <= MAX_EVIDENCE_CHARS // 2
    # The highest-scoring windows are the ones kept (earlier words first on
    # a tie): the lines' share never takes them.
    kept = [(e["words"], w["score"]) for e in entries for w in e.get("evidence") or []]
    assert kept == [("/w0", 10), ("/w1", 10)]
    assert all(s["listed_all"] is False and s["lines_trimmed"] > 0 for s in sets)


def test_windows_alone_may_use_the_whole_budget() -> None:
    from forge.planning.code_evidence import MAX_EVIDENCE_CHARS, trim_to_budget

    entries = _worst_windows()
    receipt = trim_to_budget(entries, [])
    windows_left = sum(len(w["text"]) for e in entries for w in e.get("evidence") or [])
    assert receipt["chars_after"] == windows_left
    assert MAX_EVIDENCE_CHARS - 3300 < windows_left <= MAX_EVIDENCE_CHARS
    assert windows_left > MAX_EVIDENCE_CHARS // 2


def test_a_descriptor_within_the_budget_is_not_trimmed(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"src/things/router.py": THING_ROUTER, "config/routes.yaml": ROUTES_YAML},
    )
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    assert "lines_trimmed" not in descriptor["sets_the_request_names"][0]
    assert descriptor["sets_the_request_names"][0]["listed_all"] is True


# -- evidence coverage (6 October 2026): which windows the planner is shown --
#
# Replays of real plans found the code a request was about shown only as a
# path:line pointer: three windows per word went to one test file that
# repeats the request's words, and most of the size budget went unused.


def _window(path: str, first: int, score: int, hit: int | None = None, size: int = 100) -> dict:
    return {
        "path": path, "first_line": first, "last_line": first + 15, "score": score,
        "text": "x" * size, "_hit": hit if hit is not None else first + 3,
    }


def test_one_window_per_file_comes_before_a_second_in_any_file() -> None:
    from forge.planning.code_evidence import choose_windows

    entries = [{"words": "/widgets/cache"}]
    candidates = [[
        _window("tests/test_widgets.py", 10, 9),
        _window("tests/test_widgets.py", 40, 9),
        _window("tests/test_widgets.py", 70, 8),
        _window("lib/widgets/cache.src", 1, 4),
    ]]
    hits = [[("tests/test_widgets.py", 13), ("tests/test_widgets.py", 43),
             ("tests/test_widgets.py", 73), ("lib/widgets/cache.src", 4)]]
    choose_windows(entries, candidates, hits, from_request=[True], max_windows=2)
    assert [(w["path"], w["first_line"]) for w in entries[0]["evidence"]] == [
        ("tests/test_widgets.py", 10),
        ("lib/widgets/cache.src", 1),
    ]
    assert entries[0]["more_hits"] == 2


def test_the_requests_rarest_word_is_shown_first_when_the_budget_is_short() -> None:
    from forge.planning.code_evidence import choose_windows

    entries = [{"words": "common_name"}, {"words": "spec-only"}, {"words": "RareName"}]
    candidates = [
        [_window(f"a/common{n}.txt", 1, 9) for n in range(5)],
        [_window("b/spec.txt", 1, 9)],
        [_window("c/rare.txt", 1, 2)],
    ]
    hits = [
        [(f"a/common{n}.txt", 4) for n in range(5)] + [(f"a/more{n}.txt", 1) for n in range(40)],
        [("b/spec.txt", 4)],
        [("c/rare.txt", 4), ("c/rare.txt", 90)],
    ]
    # Room for two windows: the request's rarer word, then its common one;
    # the word only the specification names (one hit) waits.
    choose_windows(entries, candidates, hits, from_request=[True, False, True],
                   max_windows=24, max_chars=200)
    assert [e.get("evidence", [{}])[0].get("path") for e in entries] == [
        "a/common0.txt", None, "c/rare.txt",
    ]
    assert entries[1]["more_hits"] == 1 and entries[2]["more_hits"] == 1


def test_a_hit_already_shown_by_another_words_window_is_not_shown_twice() -> None:
    from forge.planning.code_evidence import choose_windows, trim_to_budget

    entries = [{"words": "first-word"}, {"words": "second_word"}]
    candidates = [
        [_window("src/a.src", 1, 5, hit=4)],
        [_window("src/a.src", 3, 8, hit=6), _window("src/b.src", 1, 7, hit=2)],
    ]
    hits = [[("src/a.src", 4)], [("src/a.src", 6), ("src/b.src", 2)]]
    choose_windows(entries, candidates, hits, from_request=[True, True])
    # The second word's hit at line 6 is inside the first word's window.
    assert [(w["path"], w["first_line"]) for w in entries[0]["evidence"]] == [("src/a.src", 1)]
    assert [(w["path"], w["first_line"]) for w in entries[1]["evidence"]] == [("src/b.src", 1)]
    assert "more_hits" not in entries[0] and "more_hits" not in entries[1]
    # Trimmed (the lower score goes first), that window's hits go back to
    # both words' counts.
    trim_to_budget(entries, [], budget=150)
    assert "evidence" not in entries[0] and entries[0]["more_hits"] == 1
    assert entries[1]["more_hits"] == 1
    assert all(not any(k.startswith("_") for k in w) for e in entries for w in e.get("evidence") or [])


def test_a_window_mostly_inside_one_shown_is_skipped() -> None:
    from forge.planning.code_evidence import choose_windows

    entries = [{"words": "some-name"}]
    candidates = [[_window("src/a.src", 20, 9, hit=23), _window("src/a.src", 16, 8, hit=19),
                   _window("src/a.src", 30, 7, hit=33)]]
    hits = [[("src/a.src", 23), ("src/a.src", 19), ("src/a.src", 33)]]
    choose_windows(entries, candidates, hits, from_request=[True])
    # 16-31 shares 12 of its 16 lines with 20-35, which already holds 33.
    assert [w["first_line"] for w in entries[0]["evidence"]] == [20]
    assert entries[0]["more_hits"] == 1


#: A neutral repository where the tests repeat the request's words and the
#: code that already does the work is one file among several.
WIDGET_REQUEST = "Add WTag support to the GET /widgets list the same way GET /widgets/{widget_id} has it."
WIDGET_TESTS = "".join(
    f"def test_wtag_{n}(client):\n    r = client.get('/widgets')  # WTag support widgets list same way\n"
    + "    assert r.status == 200\n" * 20
    for n in range(8)
)
WIDGET_CODE = (
    "class WTagLayer:\n"
    "    def answer(self, body):\n"
    "        tag = digest(body)\n"
    "        return tag\n"
)


def test_a_mixed_case_name_in_the_request_is_looked_for(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"tests/test_wtag.py": WIDGET_TESTS, "lib/wtag_layer.src": WIDGET_CODE,
         "app/main.src": "app.use(WTagLayer)\n"},
    )
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)), request=WIDGET_REQUEST)
    entries = descriptor["where_the_specs_words_already_appear"]
    assert "WTag" in [entry["words"] for entry in entries]
    shown = {w["path"] for e in entries for w in e.get("evidence") or []}
    # Eight test windows score higher, yet the code and its use are shown.
    assert {"lib/wtag_layer.src", "app/main.src", "tests/test_wtag.py"} <= shown


def test_the_windows_use_what_the_set_candidates_leave(tmp_path: Path) -> None:
    from forge.planning.code_evidence import MAX_EVIDENCE_CHARS

    body = "".join("# /things/{thing_id}\n" + ("y" * 30 + "\n") * 20 for _ in range(30))
    checkout = _repo(tmp_path / "checkout", {"src/many.py": body})
    descriptor, _, _ = _descriptor(
        LocalCheckoutReader(str(checkout)), request="Add a REMOVE /things/{thing_id} endpoint."
    )
    windows = [w for e in descriptor["where_the_specs_words_already_appear"] for w in e["evidence"]]
    used = sum(len(w["text"]) for w in windows)
    # No set: more than half the budget, never more than all of it, and
    # more than the twelve windows of before.
    assert MAX_EVIDENCE_CHARS // 2 < used <= MAX_EVIDENCE_CHARS
    assert len(windows) > 12
    assert all(set(w) == {"path", "first_line", "last_line", "score", "text"} for w in windows)


def test_the_windows_keep_their_half_beside_a_large_set(tmp_path: Path) -> None:
    from forge.planning.code_evidence import MAX_EVIDENCE_CHARS

    body = "".join("# /things/{thing_id}\n" + ("y" * 80 + "\n") * 20 for _ in range(30))
    files = {"src/many.py": body}
    files.update({f"src/tally{n:02d}.py": ("tally " + "z" * 100 + "\n") * 2 for n in range(24)})
    checkout = _repo(tmp_path / "checkout", files)
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    windows = [w for e in descriptor["where_the_specs_words_already_appear"] for w in e["evidence"]]
    used = sum(len(w["text"]) for w in windows)
    lines = sum(len(line) for s in descriptor["sets_the_request_names"]
                for c in s["candidates"] for line in c["lines"])
    assert MAX_EVIDENCE_CHARS // 2 < used <= MAX_EVIDENCE_CHARS - lines
    assert descriptor["sets_the_request_names"][0]["listed_all"] is True


def test_the_factorys_pass_bars_are_never_evidence(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"src/things/router.py": THING_ROUTER,
         "qa/pass-bar-TASK-1.yaml": "check: /things/{thing_id} returns 204\n"},
    )
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert not any(p.startswith("qa/pass-bar-") for p in entry["already_in"])
    assert {w["path"] for w in entry["evidence"]} == {"src/things/router.py"}


# -- coach check 1 on evidence coverage (6 October 2026) --------------------


class _Allowance(LocalCheckoutReader):
    """A checkout read through one shared allowance of ``calls`` answers, as
    the sandbox helper's time allowance runs out; every call is logged."""

    def __init__(self, root: str, calls: int) -> None:
        super().__init__(root)
        self.left = calls
        self.log: list[str] = []

    def _spend(self, what: str) -> None:
        if self.left <= 0:
            raise RepositoryUnreadable("the stand-in's allowance ran out")
        self.left -= 1
        self.log.append(what)

    def places_mentioning(self, text):
        self._spend("places")
        return super().places_mentioning(text)

    def files_mentioning(self, text, *, ignore_case=False, relevant=None):
        self._spend("files")
        return super().files_mentioning(text, ignore_case=ignore_case, relevant=relevant)

    def read_text(self, path):
        self._spend("read")
        return super().read_text(path)


def test_the_windows_are_read_before_the_set_search_spends_the_allowance(tmp_path: Path) -> None:
    files = {"src/things/router.py": THING_ROUTER}
    files.update({f"src/tally{n:02d}.py": "tally = 1\n" for n in range(40)})
    checkout = _repo(tmp_path / "checkout", files)
    probe = _Allowance(str(checkout), calls=10_000)
    _descriptor(probe)
    # Every search and read for the windows comes before the set search.
    assert probe.log.index("files") > max(i for i, w in enumerate(probe.log) if w == "places")
    # An allowance that runs out inside the set search: the windows stay,
    # and the repository is never said to be unreadable.
    short = _Allowance(str(checkout), calls=probe.log.index("files") + 5)
    descriptor, reasons, partial = _descriptor(short)
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert entry["evidence"][0]["path"] == "src/things/router.py"
    assert reasons == []
    assert any("could not all be read for ranking" in line for line in partial)


def test_a_window_search_stopped_part_way_is_a_part_read(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"src/things/router.py": THING_ROUTER, "src/things/report.py": "def tally_report(): ...\n"},
    )
    spec = "Feature: x\n  Scenario: y\n    Given the tally_report\n"
    probe = _Allowance(str(checkout), calls=10_000)
    _descriptor(probe, spec=spec, request="Add a REMOVE /things/{thing_id} endpoint.")
    # Out of allowance after the first word's searches and read.
    first_word_calls = probe.log.index("places", 4)
    short = _Allowance(str(checkout), calls=first_word_calls)
    descriptor, reasons, partial = _descriptor(
        short, spec=spec, request="Add a REMOVE /things/{thing_id} endpoint."
    )
    entries = descriptor["where_the_specs_words_already_appear"]
    assert [entry["words"] for entry in entries] == ["/things/{thing_id}"]
    assert entries[0]["evidence"][0]["path"] == "src/things/router.py"
    assert reasons == []
    assert any(
        line.startswith("looking for where the specification's words already appear stopped part-way")
        for line in partial
    )
    # A repository that answers no search at all is still unreadable.
    none = _Allowance(str(checkout), calls=0)
    descriptor, reasons, _ = _descriptor(none, spec=spec)
    assert "where_the_specs_words_already_appear" not in descriptor
    assert any("allowance ran out" in reason for reason in reasons)


def test_a_plural_and_its_singular_are_one_word(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "checkout", {"lib/tags.src": "WTags here\nWTag there\n"})
    descriptor, _, _ = _descriptor(
        LocalCheckoutReader(str(checkout)),
        request="Make WTags strong.",
        spec="Feature: x\n  Scenario: y\n    Given a WTag\n",
    )
    entries = descriptor["where_the_specs_words_already_appear"]
    assert [entry["words"] for entry in entries] == ["WTag"]
    assert entries[0]["already_in"] == ["lib/tags.src:1", "lib/tags.src:2"]


def test_words_the_repository_does_not_hold_take_no_place(tmp_path: Path) -> None:
    # Eight words found nowhere, then two the repository holds.
    missing = [f"ex-ample-{n}" for n in range(8)]
    checkout = _repo(tmp_path / "checkout", {"src/a.src": "keep_one\nkeep_two\n"})
    descriptor, _, _ = _descriptor(
        LocalCheckoutReader(str(checkout)),
        request="Change " + " ".join(missing) + " keep_one keep_two",
    )
    entries = descriptor["where_the_specs_words_already_appear"]
    assert [entry["words"] for entry in entries] == ["keep_one", "keep_two"]


# -- the factory's own files and machine-written lines (7 October 2026) -----
#
# A live run offered the factory's own sandbox runner (it mentions a
# "counter") and a committed one-line coverage report as members of "all
# the count endpoints".


def test_the_shipped_scripts_are_the_scripts_the_factory_ships() -> None:
    import forge.cli.deploy_templates as templates
    from forge.factory_files import SHIPPED_SCRIPTS, is_shipped_script

    folder = Path(templates.__file__).parent
    assert set(SHIPPED_SCRIPTS) == {p.name for p in folder.glob("*.sh")}
    assert templates.SHIPPED_SCRIPTS is SHIPPED_SCRIPTS
    for name in SHIPPED_SCRIPTS:
        text = (folder / name).read_text()
        # Each template, as shipped, reads as itself at deploy/<name> only.
        assert is_shipped_script(f"deploy/{name}", text)
        assert not is_shipped_script(f"tools/{name}", text)
        assert not is_shipped_script(f"deploy/{name}", "#!/bin/sh\necho mine\n")
        assert not is_shipped_script(f"deploy/{name}", None)


def _factory_script(name: str) -> str:
    from forge.factory_files import SHIPPED_SCRIPTS

    return (
        "#!/usr/bin/env bash\n#\n" + SHIPPED_SCRIPTS[name] + "\n"
        "# the tally counter for things removed\n# /things/{thing_id} 204 404\n"
    )


def test_the_factorys_own_scripts_are_neither_candidates_nor_evidence(tmp_path: Path) -> None:
    from forge.factory_files import SHIPPED_SCRIPTS

    files = {
        "src/things/router.py": THING_ROUTER,
        "config/routes.yaml": ROUTES_YAML,
    }
    for name in SHIPPED_SCRIPTS:
        files[f"deploy/{name}"] = _factory_script(name)
    checkout = _repo(tmp_path / "checkout", files)
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    (named,) = descriptor["sets_the_request_names"]
    assert [c["path"] for c in named["candidates"]] == ["config/routes.yaml"]
    assert named["matched"] == 1 and named["listed_all"] is True
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert not any(p.startswith("deploy/") for p in entry["already_in"])
    assert {w["path"] for w in entry["evidence"]} == {"src/things/router.py"}


def test_a_projects_own_script_of_the_same_name_stays_the_projects(tmp_path: Path) -> None:
    """Elsewhere in the tree, or at deploy/ with the project's own content,
    a file named like the factory's script is the project's: listed, shown
    and counted, and listed_all stays true."""
    mine = "#!/bin/sh\n# our tally runner\necho /things/{thing_id}\n"
    checkout = _repo(
        tmp_path / "checkout",
        {
            "src/things/router.py": THING_ROUTER,
            "tools/sandbox-runner.sh": mine,
            "deploy/sandbox-deploy.sh": mine,
            "deploy/sandbox-runner.sh": _factory_script("sandbox-runner.sh"),
        },
    )
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    (named,) = descriptor["sets_the_request_names"]
    assert sorted(c["path"] for c in named["candidates"]) == [
        "deploy/sandbox-deploy.sh", "tools/sandbox-runner.sh",
    ]
    assert named["matched"] == 2 and named["listed_all"] is True
    entry = descriptor["where_the_specs_words_already_appear"][0]
    shown = {w["path"] for w in entry["evidence"]}
    assert {"tools/sandbox-runner.sh", "deploy/sandbox-deploy.sh"} <= shown
    assert "deploy/sandbox-runner.sh" not in shown
    assert not any(p.startswith("deploy/sandbox-runner.sh") for p in entry["already_in"])


#: A committed report a tool wrote on one line: it names every file and so
#: holds most of the request's words.
ONE_LINE_REPORT = (
    '{"files": {'
    + ", ".join(
        f'"src/things/remove_{n}.py": {{"tally": {n}, "unknown": 0, "removed": 1}}'
        for n in range(40)
    )
    + ', "/things/{thing_id}": "204 404 success endpoint returns"}}\n'
)


def test_a_one_line_report_ranks_after_every_hand_written_candidate(tmp_path: Path) -> None:
    from forge.planning.code_evidence import MACHINE_WRITTEN_LINE_CHARS

    assert len(ONE_LINE_REPORT) > MACHINE_WRITTEN_LINE_CHARS
    checkout = _repo(
        tmp_path / "checkout",
        {"aaa/report.json": ONE_LINE_REPORT, "config/routes.yaml": ROUTES_YAML,
         "zz/bare.txt": "tally\n"},
    )
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    (named,) = descriptor["sets_the_request_names"]
    # Still a candidate (it does hold the word), but last.
    assert [c["path"] for c in named["candidates"]] == [
        "config/routes.yaml", "zz/bare.txt", "aaa/report.json",
    ]


def test_a_hit_past_what_a_window_shows_of_its_line_is_no_window(tmp_path: Path) -> None:
    checkout = _repo(
        tmp_path / "checkout",
        {"aaa/report.json": ONE_LINE_REPORT, "src/things/router.py": THING_ROUTER},
    )
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    assert "aaa/report.json:1" in entry["already_in"]
    # The report's hit is thousands of characters into its one line.
    assert ONE_LINE_REPORT.find("{thing_id}") > 2000
    assert {w["path"] for w in entry["evidence"]} == {"src/things/router.py"}
    # Its hit is still counted as one not shown.
    assert entry["more_hits"] == 1


def test_a_long_hand_written_line_with_the_hit_near_its_start_keeps_its_window(
    tmp_path: Path,
) -> None:
    from forge.planning.code_evidence import MACHINE_WRITTEN_LINE_CHARS

    table = (
        'ROUTES = ["/things/{thing_id}", '
        + ", ".join(f'"/things/extra-{n}"' for n in range(120))
        + "]\n"
    )
    assert len(table) > MACHINE_WRITTEN_LINE_CHARS and table.find("{thing_id}") < 60
    checkout = _repo(tmp_path / "checkout", {"src/routes.src": table})
    descriptor, _, _ = _descriptor(LocalCheckoutReader(str(checkout)))
    entry = descriptor["where_the_specs_words_already_appear"][0]
    (window,) = entry["evidence"]
    assert window["path"] == "src/routes.src" and "/things/{thing_id}" in window["text"]
    assert "more_hits" not in entry
