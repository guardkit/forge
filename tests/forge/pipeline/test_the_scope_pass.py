"""The scope pass, driven against real git repositories.

Nothing here is a fake: every test builds a repository with a real history,
cuts a real ``autobuild/FEAT-…`` branch off main, commits real files onto it,
and asks the reader what that branch changed and what it added. That matters
because the whole point of this pass is that it reads what was BUILT rather
than what was promised, and a test that only asserts a function was called
would not have caught the thing this exists to catch.

The design of record is
``ai-transition/docs/planner-fix-design-2026-09-15.md`` §2b (what a task
document declares) and §2e (what the receipt keeps).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge.pipeline.branch_scope import (
    BranchScopeReading,
    added_lines_by_file,
    added_lines_of,
    plan_document_paths,
    read_branch_scope,
    reading_from_answer,
    reading_to_answer,
)
from forge.pipeline.scope_report import (
    SCOPE_REPORT_NAME,
    ScopeReport,
    files_the_plan_named,
    read_declared_files,
    scope_of_the_build,
    what_the_branch_declares,
    write_scope_report,
)

FEATURE_ID = "FEAT-SCOPE"
BRANCH = f"autobuild/{FEATURE_ID}"

#: The sentence the twelve-plan experiment sent through the factory twelve
#: times, word for word.
REQUEST = (
    "Add a GET /users/created-per-day endpoint that returns the number of "
    "users created on each of the last 7 days, oldest first."
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _task_document(task_id: str, *, create: list[str], modify: list[str]) -> str:
    def _lines(paths: list[str]) -> str:
        return "\n".join(f"- `{path}`" for path in paths) if paths else "- _none_"

    return (
        "---\n"
        f"id: {task_id}\n"
        f"title: {task_id}\n"
        "task_type: feature\n"
        f"feature_id: {FEATURE_ID}\n"
        "---\n\n"
        "Do the thing the request asks for.\n\n"
        "## The words of the request this task serves\n\n"
        "> the number of users created on each of the last 7 days\n\n"
        "## Files to Create\n\n"
        f"{_lines(create)}\n\n"
        "## Files to Modify\n\n"
        f"{_lines(modify)}\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] it works\n"
    )


def _feature_file(paths: list[str]) -> str:
    rows = "\n".join(
        f'  - id: TASK-SCOPE-00{index + 1}\n    file_path: "{path}"'
        for index, path in enumerate(paths)
    )
    return f"id: {FEATURE_ID}\nname: the scope pass\ntasks:\n{rows}\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with a plan of record on main and nothing built yet."""
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, "src/users/router.py", "# the users router\n")
    _write(root, "src/users/crud.py", "# the users queries\n")
    task_one = "tasks/backlog/daily-counts/TASK-SCOPE-001-the-query.md"
    task_two = "tasks/backlog/daily-counts/TASK-SCOPE-002-the-endpoint.md"
    _write(
        root,
        f".guardkit/features/{FEATURE_ID}.yaml",
        _feature_file([task_one, task_two]),
    )
    _write(
        root,
        task_one,
        _task_document("TASK-SCOPE-001", create=[], modify=["src/users/crud.py"]),
    )
    _write(
        root,
        task_two,
        _task_document("TASK-SCOPE-002", create=[], modify=["src/users/router.py"]),
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the plan of record")
    return root


def _build_on(root: Path, files: dict[str, str], *, branch: str = BRANCH) -> None:
    """Cut the build's branch off main and commit what the build wrote."""
    _git(root, "checkout", "-q", "-b", branch)
    for rel, text in files.items():
        _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "what the build wrote")


class TestAgainstThePlan:
    def test_it_names_the_files_the_plan_did_not_name(self, repo: Path) -> None:
        _build_on(
            repo,
            {
                "src/users/router.py": "# the users router\n@router.get('/users/created-per-day')\ndef counts():\n    return []\n",
                "src/users/crud.py": "# the users queries\ndef counts():\n    return []\n",
                "src/analytics/schema.py": "class DailyCount:\n    pass\n",
                "src/analytics/service.py": "def build_counts():\n    return []\n",
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        assert reading.error is None
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.read is True
        assert report.plan_read is True
        assert report.files_changed == 4
        assert sorted(report.files_the_plan_named) == [
            "src/users/crud.py",
            "src/users/router.py",
        ]
        assert sorted(report.files_the_plan_did_not_name) == [
            "src/analytics/schema.py",
            "src/analytics/service.py",
        ]

    def test_tests_and_documentation_are_the_ordinary_cost_of_the_work(
        self, repo: Path
    ) -> None:
        _build_on(
            repo,
            {
                "src/users/router.py": "# the users router\n# /users/created-per-day\n",
                "tests/test_daily_counts.py": "def test_counts():\n    assert True\n",
                "docs/api.md": "# the daily counts endpoint\n",
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.files_the_plan_did_not_name == []
        assert sorted(report.files_allowed_as_scaffolding) == [
            "docs/api.md",
            "tests/test_daily_counts.py",
        ]

    def test_a_plan_that_declares_no_files_is_not_compared_against_nothing(
        self, tmp_path: Path
    ) -> None:
        """Every plan written before task documents carried the two sections
        declares nothing, and a build must not be reported as sprawling just
        because there was nothing to compare it with."""
        root = tmp_path / "older"
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        task = "tasks/backlog/daily-counts/TASK-SCOPE-001.md"
        _write(
            root,
            f".guardkit/features/{FEATURE_ID}.yaml",
            _feature_file([task]),
        )
        _write(
            root,
            task,
            "---\nid: TASK-SCOPE-001\n---\n\nDo the thing.\n\n"
            "## Acceptance Criteria\n\n- [ ] it works\n",
        )
        _write(root, "src/users/router.py", "# the users router\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "an older plan")
        _build_on(root, {"src/analytics/service.py": "x = 1\n"})

        reading = read_branch_scope(
            repo_root=root, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.read is True
        assert report.plan_read is False
        assert report.files_the_plan_did_not_name == []
        assert report.why_not is not None
        assert "names no files" in report.why_not


class TestAgainstTheSentence:
    def test_a_build_inside_its_plan_still_says_the_web_address_moved(
        self, repo: Path
    ) -> None:
        """Rich's item 5, on its own: every file this build changed was named
        in the plan, and it still answers somewhere nobody asked for."""
        _build_on(
            repo,
            {
                "src/users/router.py": (
                    "# the users router\n"
                    "@router.get('/stats/users-created-per-day')\n"
                    "def counts():\n    return []\n"
                ),
                "src/users/crud.py": "# the users queries\ndef counts():\n    return []\n",
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.files_the_plan_did_not_name == []
        assert report.routes_read is True
        assert report.routes_in_the_request == ["/users/created-per-day"]
        assert "/stats/users-created-per-day" in report.routes_the_request_did_not_name
        assert "/users/created-per-day" not in report.routes_the_request_did_not_name

    def test_it_names_a_capability_the_request_never_asked_for(
        self, repo: Path
    ) -> None:
        _build_on(
            repo,
            {
                "src/users/router.py": (
                    "# the users router\n"
                    "# /users/created-per-day\n"
                    "@router.get('/users/created-per-day')\n"
                    "def counts(user = Depends(require_authentication)):\n"
                    "    return []\n"
                ),
                "migrations/versions/0001_add_created_at.py": (
                    "# alembic revision\nrevision = '0001'\n"
                ),
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert "authentication" in report.capabilities_the_request_did_not_name
        assert "a database migration" in report.capabilities_the_request_did_not_name

    def test_a_clean_build_says_nothing_was_added(self, repo: Path) -> None:
        _build_on(
            repo,
            {
                "src/users/router.py": (
                    "# the users router\n"
                    "@router.get('/users/created-per-day')\n"
                    "def counts():\n    return []\n"
                ),
                "src/users/crud.py": "# the users queries\ndef counts():\n    return []\n",
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.files_the_plan_did_not_name == []
        assert report.routes_the_request_did_not_name == []
        assert report.capabilities_the_request_did_not_name == []
        assert report.routes_read is True

    def test_no_sentence_means_the_comparison_is_not_taken(self, repo: Path) -> None:
        _build_on(repo, {"src/analytics/service.py": "x = 1\n"})
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(
            reading=reading,
            request=None,
            request_why_not="there is no planning record for corr-1",
        )
        assert report.routes_read is False
        assert report.routes_the_request_did_not_name == []
        assert report.why_not is not None
        assert "no planning record for corr-1" in report.why_not


class TestWhatCountsAsDeclaringACapability:
    """The capability words are read in what the branch DECLARES.

    They were measured on the prose of task documents, where "logging" is a
    deliberate promise. Read in raw source they fire on ordinary code: driven
    over one real 184-line commit, the word "permissions" inside a test
    fixture put "It also added permissions, which the request did not ask
    for." on the card Rich taps to say merge. A false sentence there buries
    the true one beside it, so a test file, a note to a reader and a bare
    import are all left out of the reading.
    """

    def test_a_word_in_a_test_file_is_not_something_this_build_added(
        self, repo: Path
    ) -> None:
        _build_on(
            repo,
            {
                "src/users/router.py": (
                    "# the users router\n"
                    "@router.get('/users/created-per-day')\n"
                    "def counts():\n    return []\n"
                ),
                "src/users/crud.py": "# the users queries\ndef counts():\n    return []\n",
                "tests/test_router.py": (
                    "import logging\n"
                    "\n"
                    "FIXTURE = 'permissions: {filesystem: {allowlist: [/tmp]}}'\n"
                    "\n"
                    "def test_counts():\n    assert True\n"
                ),
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.routes_read is True
        assert report.capabilities_the_request_did_not_name == []
        # and the fixture's own path is not a web address this build answers
        # at either — the same blindness, in the other half of the sentence
        assert report.routes_the_request_did_not_name == []
        assert "/tmp" not in report.routes_the_branch_declares
        assert "tests/test_router.py" in report.files_allowed_as_scaffolding

        # and the card Rich taps says so: the whole way through, not just in
        # the report
        from forge.cli._serve_gate_activation import card_line_about_scope

        line = card_line_about_scope(report)
        assert "logging" not in line
        assert "permissions" not in line
        assert line == (
            "Every file this build changed was named in the plan, and it "
            "added nothing the request did not ask for."
        )

    def test_an_import_and_a_logger_are_not_logging_the_build_added(
        self, repo: Path
    ) -> None:
        _build_on(
            repo,
            {
                "src/users/router.py": (
                    "import logging\n"
                    "\n"
                    "logger = logging.getLogger(__name__)\n"
                    "\n"
                    "@router.get('/users/created-per-day')\n"
                    "def counts():\n    return []\n"
                ),
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.routes_read is True
        assert "logging" not in report.capabilities_the_request_did_not_name

    def test_a_note_to_a_reader_is_not_something_the_build_does(
        self, repo: Path
    ) -> None:
        _build_on(
            repo,
            {
                "src/users/crud.py": (
                    '"""The daily counts.\n'
                    "\n"
                    "One day this could grow a service layer of its own.\n"
                    '"""\n'
                    "\n"
                    "# error handling could come later\n"
                    "def counts():\n    return []\n"
                ),
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.routes_read is True
        assert report.capabilities_the_request_did_not_name == []

    def test_the_file_the_build_put_there_still_says_what_it_added(
        self, repo: Path
    ) -> None:
        """The path is the declaration here: nothing in these lines says
        migration, but a file under ``migrations/`` is one."""
        _build_on(
            repo,
            {
                "src/users/crud.py": "def counts():\n    return []\n",
                "migrations/0001_add_created_at.py": (
                    "revision = '0001'\n"
                    "\n"
                    "def upgrade():\n    pass\n"
                    "\n"
                    "def downgrade():\n    pass\n"
                ),
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert "a database migration" in report.capabilities_the_request_did_not_name

    def test_a_reading_that_never_said_which_file_is_not_counted(self) -> None:
        """A count nobody could take is never published as a count of
        nothing, so an answer with no files in it says so."""

        class OnlyTheLines:
            error = None
            name_status = "M\x00src/users/crud.py\x00"
            added_lines = "import logging\n"
            added_lines_read_whole = True

        report = scope_of_the_build(reading=OnlyTheLines(), request=REQUEST)

        assert report.routes_read is False
        assert report.capabilities_the_request_did_not_name == []
        assert report.why_not is not None
        assert "could not be read whole" in report.why_not

    def test_what_is_read_and_what_is_left_out(self) -> None:
        declared = what_the_branch_declares(
            {
                "src/users/router.py": (
                    "import logging\n"
                    "logger = logging.getLogger(__name__)\n"
                    "# permissions come later\n"
                    '"""Pagination is out of scope.\n'
                    'Still out of scope.\n"""\n'
                    "def counts(user = Depends(require_authentication)):\n"
                    "    return []\n"
                ),
                "tests/test_router.py": "CACHING = 'caching'\n",
                "docs/api.md": "This endpoint uses rate limiting.\n",
            }
        )

        assert "src/users/router.py" in declared
        assert "require_authentication" in declared
        assert "import logging" not in declared
        assert "logging.getLogger" not in declared
        assert "permissions" not in declared
        assert "Pagination" not in declared
        assert "Still out of scope" not in declared
        assert "caching" not in declared
        assert "rate limiting" not in declared

    def test_it_never_raises_on_a_reading_with_nothing_in_it(self) -> None:
        assert what_the_branch_declares({}) == ""
        assert what_the_branch_declares({"": "x = 1\n"}) == ""


#: The route declaration FEAT-3EF3 really committed to ``src/users/router.py``,
#: copied out of that branch read-only on 2026-09-15. The last line of its
#: description is the whole defect: the only mention of authentication anywhere
#: in the code this build was asked for says the endpoint does NOT require any.
FEAT_3EF3_ROUTER = '''"""Users API router."""


@router.get(
    "/users/created-per-day",
    response_model=CreatedPerDayResponse,
    tags=["users"],
    summary="Get users created per day for the last 7 days",
    description=(
        "Returns exactly 7 entries (today and the previous 6 days) showing "
        "the number of users created on each day, ordered from oldest to newest. "
        "Days with zero creations are included with a count of 0. "
        "This endpoint does not require authentication."
    ),
    responses={
        503: {"description": "Database unavailable"},
    },
)
async def get_users_created_per_day(db=Depends(get_db)):
    return await crud.count_users_created_per_day(db)
'''

#: The same declaration from FEAT-7A25, which denies it the other way round:
#: "publicly accessible without authentication".
FEAT_7A25_ROUTER = '''"""Users API router."""


@router.get(
    "/users/created-per-day",
    response_model=list[CreatedPerDayEntry],
    tags=["users"],
    summary="Get user creation counts per day for the last 7 days",
    description=(
        "Returns a JSON array of {day, count} objects showing the number of "
        "users created on each of the last 7 days, ordered from oldest to "
        "newest. Days with no user creations are included with a count of 0. "
        "This endpoint is publicly accessible without authentication."
    ),
    responses={
        503: {"description": "Database unavailable"},
    },
)
async def get_users_created_per_day(db=Depends(get_db)):
    return await crud.count_users_created_per_day(db)
'''

#: FEAT-BD8F really did add a login requirement nobody asked for, and its
#: evidence is a line with no denial on it at all: the endpoint requires an
#: ``X-Auth-Token`` header. This one must keep its sentence.
FEAT_BD8F_ROUTER = '''"""Users API router."""


@router.get(
    "/users/created-per-day",
    response_model=UserCreationStats,
    tags=["users"],
    summary="Get the number of users created on each of the last seven days",
    description=(
        "Returns the number of users created on each of the last seven calendar "
        "days, oldest day first, with the total the days account for. The window "
        "ends yesterday: today is still in progress and is not answered for. A "
        "day with no creations carries a count of zero rather than going missing. "
        "Soft-deleted users are not counted as creations. Requires the "
        "``X-Auth-Token`` header."
    ),
    responses={
        403: {"description": "Unauthorized: valid authentication token required"},
        503: {"description": "Database unavailable"},
    },
)
async def get_users_created_per_day(request, db=Depends(get_db)):
    if request.headers.get("X-Auth-Token") != AUTH_TOKEN:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return await crud.count_users_created_per_day(db)
'''


class TestALineThatDeniesACapability:
    """A line saying the software does NOT do a thing is not evidence that
    this build added it.

    Driven read-only over all twenty-three real build branches in the api_test
    repository on 2026-09-15, two of them carried the sentence "It also added
    authentication, which the request did not ask for." on the card the owner
    taps to say merge — and in both, the only mention of authentication in the
    code the build was asked for was the route's own description saying the
    endpoint needs none. The three route declarations above are those two
    branches and the one that really did add it, copied out of the branches
    themselves.
    """

    def test_feat_3ef3_loses_a_sentence_that_was_never_true(self, repo: Path) -> None:
        _build_on(repo, {"src/users/router.py": FEAT_3EF3_ROUTER})
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.routes_read is True
        assert report.capabilities_the_request_did_not_name == []

        from forge.cli._serve_gate_activation import card_line_about_scope

        assert "authentication" not in card_line_about_scope(report)

    def test_feat_7a25_loses_it_too_where_the_denying_word_is_without(
        self, repo: Path
    ) -> None:
        _build_on(repo, {"src/users/router.py": FEAT_7A25_ROUTER})
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.routes_read is True
        assert report.capabilities_the_request_did_not_name == []

        from forge.cli._serve_gate_activation import card_line_about_scope

        assert "authentication" not in card_line_about_scope(report)

    def test_feat_bd8f_keeps_the_sentence_that_is_true(self, repo: Path) -> None:
        """This branch really did demand a header nobody asked for, and the
        card must still say so. A denial reading that swallowed this one would
        have cost the pass the only true sentence of the three."""
        _build_on(repo, {"src/users/router.py": FEAT_BD8F_ROUTER})
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.routes_read is True
        assert "authentication" in report.capabilities_the_request_did_not_name

        from forge.cli._serve_gate_activation import card_line_about_scope

        assert (
            "It also added authentication, which the request did not ask for."
            in card_line_about_scope(report)
        )

    def test_the_reading_is_per_line_so_a_true_mention_elsewhere_still_counts(
        self, repo: Path
    ) -> None:
        """The denial only ever covers its own line. A build whose description
        denies authentication on one line and whose code demands an
        ``X-Auth-Token`` header on another has still added authentication, and
        the card still says so."""
        mixed = FEAT_3EF3_ROUTER.replace(
            "async def get_users_created_per_day(db=Depends(get_db)):\n",
            "async def get_users_created_per_day(request, db=Depends(get_db)):\n"
            '    if request.headers.get("X-Auth-Token") != AUTH_TOKEN:\n'
            '        raise HTTPException(status_code=403, detail="Unauthorized")\n',
        )
        assert "X-Auth-Token" in mixed
        _build_on(repo, {"src/users/router.py": mixed})
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert "authentication" in report.capabilities_the_request_did_not_name

    def test_how_far_back_a_denial_reaches(self) -> None:
        """A denial a few words in front of the capability word reaches it; one
        far enough back to be a different clause does not, because a rule that
        swallowed every later sentence would hide real additions."""
        from forge.planning.assumption_review import _CAPABILITIES
        from forge.pipeline.scope_report import promises_the_capability

        auth = dict(_CAPABILITIES)["authentication"]

        assert promises_the_capability("every request requires authentication", auth)
        assert not promises_the_capability(
            "This endpoint does not require authentication.", auth
        )
        assert not promises_the_capability(
            "This endpoint is publicly accessible without authentication.", auth
        )
        assert not promises_the_capability(
            "do not write a scenario about rejecting unauthenticated requests", auth
        )
        assert not promises_the_capability("the route doesn't need a login", auth)
        # Out of reach: the denial is eight words back and belongs to another
        # clause about something else entirely.
        assert promises_the_capability(
            "There is no cache here at all, and the endpoint now requires "
            "authentication on every call.",
            auth,
        )


class TestWhenNothingCouldBeRead:
    def test_a_branch_that_is_not_there_is_said_plainly(self, repo: Path) -> None:
        reading = read_branch_scope(
            repo_root=repo,
            base="main",
            head="autobuild/FEAT-NOTHERE",
            feature_id="FEAT-NOTHERE",
        )
        assert reading.error is not None
        report = scope_of_the_build(reading=reading, request=REQUEST)
        assert report.read is False
        assert report.files_changed == 0
        assert report.why_not

    def test_a_directory_that_is_not_a_repository_is_said_plainly(
        self, tmp_path: Path
    ) -> None:
        reading = read_branch_scope(
            repo_root=tmp_path, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        assert reading.error is not None
        report = scope_of_the_build(reading=reading, request=REQUEST)
        assert report.read is False


class TestTheTaskDocumentsTwoSections:
    def test_the_two_sections_are_kept_apart(self) -> None:
        declared = read_declared_files(
            "## Files to Create\n\n- `src/users/router.py`\n\n"
            "## Files to Modify\n\n- `src/users/crud.py`\n- `src/users/models.py`\n"
        )
        assert declared.create == ("src/users/router.py",)
        assert declared.modify == ("src/users/crud.py", "src/users/models.py")

    def test_a_task_that_creates_nothing_can_say_exactly_that(self) -> None:
        declared = read_declared_files(
            "## Files to Create\n\n- _none_\n\n"
            "## Files to Modify\n\n- `src/users/crud.py`\n"
        )
        assert declared.create == ()
        assert declared.create_present is True
        assert declared.modify == ("src/users/crud.py",)
        assert declared.declared_anything is True

    def test_a_missing_section_is_not_the_same_as_an_empty_one(self) -> None:
        declared = read_declared_files("## Acceptance Criteria\n\n- [ ] it works\n")
        assert declared.create_present is False
        assert declared.modify_present is False
        assert declared.declared_anything is False

    def test_a_section_stops_at_the_next_heading(self) -> None:
        declared = read_declared_files(
            "## Files to Create\n\n- `src/a.py`\n\n"
            "## Acceptance Criteria\n\n- [ ] `src/b.py` is not a declared file\n"
        )
        assert declared.create == ("src/a.py",)

    def test_the_plan_as_a_whole_is_the_union_of_its_tasks(self) -> None:
        named, declared = files_the_plan_named(
            {
                "tasks/one.md": "## Files to Create\n\n- `src/a.py`\n",
                "tasks/two.md": "## Files to Modify\n\n- `src/b.py`\n",
            }
        )
        assert sorted(named) == ["src/a.py", "src/b.py"]
        assert declared is True


class TestTheFeatureFileAndThePatch:
    def test_the_feature_file_names_its_task_documents(self) -> None:
        paths = plan_document_paths(
            _feature_file(["tasks/backlog/x/TASK-SCOPE-001.md", "tasks/y/TASK-2.md"])
        )
        assert paths == ("tasks/backlog/x/TASK-SCOPE-001.md", "tasks/y/TASK-2.md")

    def test_a_path_that_escapes_the_repository_is_dropped(self) -> None:
        assert plan_document_paths('  - file_path: "../../etc/passwd"\n') == ()
        assert plan_document_paths('  - file_path: "/etc/passwd"\n') == ()

    def test_only_the_lines_a_branch_adds_are_read(self) -> None:
        patch = (
            "diff --git a/src/x.py b/src/x.py\n"
            "--- a/src/x.py\n"
            "+++ b/src/x.py\n"
            "@@ -1 +1 @@\n"
            "-the old line\n"
            "+the new line\n"
        )
        assert added_lines_of(patch) == "the new line"

    def test_the_file_each_added_line_came_from_is_kept(self) -> None:
        patch = (
            "diff --git a/src/x.py b/src/x.py\n"
            "--- a/src/x.py\n"
            "+++ b/src/x.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+import logging\n"
            "+def counts():\n"
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "--- /dev/null\n"
            "+++ b/tests/test_x.py\n"
            "@@ -0,0 +1 @@\n"
            "+import logging\n"
        )
        assert added_lines_by_file(patch) == {
            "src/x.py": "import logging\ndef counts():",
            "tests/test_x.py": "import logging",
        }

    def test_the_answer_carries_which_file_each_line_came_from(self) -> None:
        reading = BranchScopeReading(
            name_status="M\x00src/x.py\x00",
            added_by_file={"src/x.py": "def counts():"},
            added_lines_read_whole=True,
        )
        answer = reading_to_answer(reading)
        assert answer["added_by_file"] == {"src/x.py": "def counts():"}

        came_back = reading_from_answer(answer)
        assert came_back.added_by_file == reading.added_by_file
        assert came_back.added_lines == "def counts():"
        assert came_back.added_lines_read_whole is True

    def test_an_answer_that_never_said_which_file_counts_as_unread(self) -> None:
        came_back = reading_from_answer(
            {"name_status": "M\x00src/x.py\x00", "added_lines_read_whole": True}
        )
        assert came_back.added_by_file == {}
        assert came_back.added_lines_read_whole is False


class TestTheReceipt:
    def test_it_lands_beside_the_build_s_own_evidence(self, tmp_path: Path) -> None:
        receipts = tmp_path / "receipts" / "build-X-1"
        receipts.mkdir(parents=True)
        report = ScopeReport(read=True, plan_read=True, routes_read=True)
        report.files_changed = 2
        written = write_scope_report("build-X-1", report, receipts_dir=receipts)
        assert written is not None
        assert written.name == SCOPE_REPORT_NAME
        kept = json.loads(written.read_text(encoding="utf-8"))
        assert kept["read"] is True
        assert kept["files_changed"] == 2
        assert "routes_read" in kept
        assert "why_not" in kept

    def test_it_does_not_start_a_tree_of_receipts_of_its_own(
        self, tmp_path: Path
    ) -> None:
        nowhere = tmp_path / "receipts" / "build-never-run"
        assert write_scope_report("build-never-run", ScopeReport(), receipts_dir=nowhere) is None
        assert not nowhere.exists()

    def test_every_field_the_design_names_is_on_the_receipt(self) -> None:
        kept = ScopeReport().to_dict()
        for field in (
            "read",
            "why_not",
            "request",
            "request_source",
            "files_changed",
            "files_the_plan_named",
            "files_the_plan_did_not_name",
            "files_allowed_as_scaffolding",
            "routes_in_the_request",
            "routes_the_branch_declares",
            "routes_the_request_did_not_name",
            "capabilities_the_request_did_not_name",
            "routes_read",
        ):
            assert field in kept, field


class TestTheFactorysOwnPaperworkOnTheBranch:
    """The way a REAL routine build branch is shaped, which is not the way
    the fixture above is shaped.

    Every real build branch opens with four planning commits — the feature
    file, the specification input, the feature plan with its task documents,
    and the QA pass bars and gate scripts — and only then the coder's commit.
    So all of the factory's own writing is inside ``main...autobuild/FEAT-…``
    and, read as the code the build was asked for, it put six to twelve false
    sentences on the card Rich taps to say merge: a shebang became a web
    address, a placeholder in a gate script became a web address, an example
    in a docstring became a web address, and an open question in the
    specification input ("Are there any authentication or authorization
    requirements for this endpoint?") became "It also added authentication,
    which the request did not ask for."

    Measured on 2026-09-15 over all twenty-two real build branches in the
    api_test repository: 169 "It also …" sentences before this, 11 after, and
    the nine that survive are things the builds really did — three moved the
    web address, four added a database migration, and the rest added a way of
    signing in nobody asked for.
    """

    THE_GATE_SCRIPT = (
        "#!/usr/bin/env python3\n"
        '"""The QA gate for the daily counts endpoint.\n'
        "\n"
        "Example: the gate calls /stats and compares the rows.\n"
        '"""\n'
        "PLACEHOLDER = '/REPLACE_ME'\n"
        "def check():\n"
        "    return True\n"
    )
    THE_SPEC_INPUT = (
        "# The request\n\n"
        "Add a GET /users/created-per-day endpoint.\n\n"
        "**open_questions**: ['Are there any authentication or authorization "
        "requirements for this endpoint?', 'Should a database migration add "
        "the column?']\n"
    )

    @pytest.fixture
    def repo_the_way_the_factory_builds(self, tmp_path: Path) -> Path:
        """main holds the repository as it stood; the planning papers and the
        code are both committed on the build's own branch."""
        root = tmp_path / "api_test"
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "src/users/router.py", "# the users router\n")
        _write(root, "src/users/crud.py", "# the users queries\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "the repository as it stood")

        _git(root, "checkout", "-q", "-b", BRANCH)
        task_one = "tasks/backlog/daily-counts/TASK-SCOPE-001-the-query.md"
        task_two = "tasks/backlog/daily-counts/TASK-SCOPE-002-the-endpoint.md"
        _write(
            root,
            f".guardkit/features/{FEATURE_ID}.yaml",
            _feature_file([task_one, task_two]),
        )
        _write(
            root,
            task_one,
            _task_document("TASK-SCOPE-001", create=[], modify=["src/users/crud.py"]),
        )
        _write(
            root,
            task_two,
            _task_document("TASK-SCOPE-002", create=[], modify=["src/users/router.py"]),
        )
        _write(root, "feature_spec_inputs/8ca406dd-the-request.md", self.THE_SPEC_INPUT)
        _write(
            root,
            "features/created-per-day/created-per-day.feature",
            "Feature: daily counts\n  # written by /feature-spec\n"
            "  Scenario: a request without authentication is rejected\n",
        )
        _write(
            root,
            "features/created-per-day/created-per-day_summary.md",
            "Written by /feature-plan. It may need a database migration.\n",
        )
        _write(root, "qa/gates/created_per_day_gate.py", self.THE_GATE_SCRIPT)
        _write(
            root,
            "qa/pass-bar-TASK-SCOPE-001.yaml",
            "task_id: TASK-SCOPE-001\nbar: the rows come back oldest first\n",
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "planning: the plan of record")
        return root

    def _code(self) -> dict[str, str]:
        """What the coder wrote: the address in two pieces, the way a real
        router declares it."""
        return {
            "src/users/router.py": (
                "# the users router\n"
                'analytics_router = APIRouter(prefix="/users")\n'
                '@analytics_router.get("/created-per-day")\n'
                "def counts():\n"
                "    return []\n"
            ),
            "src/users/crud.py": "# the users queries\ndef counts():\n    return []\n",
        }

    def _report(self, root: Path) -> ScopeReport:
        reading = read_branch_scope(
            repo_root=root, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        assert reading.error is None
        return scope_of_the_build(reading=reading, request=REQUEST)

    def test_a_clean_build_says_nothing_beyond_the_plan_or_the_request(
        self, repo_the_way_the_factory_builds: Path
    ) -> None:
        """The whole point: a build that did exactly what was asked, with the
        factory's own paperwork beside it, gets the quiet sentence."""
        from forge.cli._serve_gate_activation import card_line_about_scope

        root = repo_the_way_the_factory_builds
        self._code_and_commit(root)
        report = self._report(root)

        assert report.read is True
        assert report.plan_read is True
        assert report.routes_read is True
        assert report.files_the_plan_did_not_name == []
        assert report.routes_the_request_did_not_name == []
        assert report.capabilities_the_request_did_not_name == []
        assert card_line_about_scope(report) == (
            "Every file this build changed was named in the plan, and it "
            "added nothing the request did not ask for."
        )

    def _code_and_commit(self, root: Path) -> dict[str, str]:
        files = self._code()
        for rel, text in files.items():
            _write(root, rel, text)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "what the build wrote")
        return files

    def test_the_paperwork_is_never_a_file_the_plan_did_not_name(
        self, repo_the_way_the_factory_builds: Path
    ) -> None:
        """The blast radius names the one real surprise, not the fourteen
        papers the factory wrote on its way there."""
        from forge.cli._serve_gate_activation import card_line_about_scope

        root = repo_the_way_the_factory_builds
        files = self._code()
        files["alembic/versions/0001_add_created_at.py"] = (
            "def upgrade() -> None:\n    pass\n"
            "def downgrade() -> None:\n    pass\n"
        )
        for rel, text in files.items():
            _write(root, rel, text)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "what the build wrote")
        report = self._report(root)

        assert report.files_the_plan_did_not_name == [
            "alembic/versions/0001_add_created_at.py"
        ]
        assert ".guardkit/features/FEAT-SCOPE.yaml" in (
            report.files_allowed_as_scaffolding
        )
        for paper in (
            "feature_spec_inputs/8ca406dd-the-request.md",
            "features/created-per-day/created-per-day.feature",
            "qa/gates/created_per_day_gate.py",
            "qa/pass-bar-TASK-SCOPE-001.yaml",
            "tasks/backlog/daily-counts/TASK-SCOPE-001-the-query.md",
        ):
            assert paper in report.files_allowed_as_scaffolding
        assert card_line_about_scope(report) == (
            "This build also changed 1 file the plan did not name: "
            "alembic/versions/0001_add_created_at.py — worth a look before "
            "you merge. It also added a database migration, which the "
            "request did not ask for."
        )

    def test_the_shebang_the_placeholder_and_the_example_are_not_web_addresses(
        self, repo_the_way_the_factory_builds: Path
    ) -> None:
        """The three that showed on every real card: /usr/bin/env from a
        shebang, /REPLACE_ME from a placeholder, /stats from an example in a
        docstring."""
        root = repo_the_way_the_factory_builds
        self._code_and_commit(root)
        report = self._report(root)

        for false_address in ("/usr/bin/env", "/REPLACE_ME", "/stats"):
            assert false_address not in report.routes_the_branch_declares
            assert false_address not in report.routes_the_request_did_not_name

    def test_an_open_question_in_the_spec_input_is_not_a_capability(
        self, repo_the_way_the_factory_builds: Path
    ) -> None:
        """The specification input asks, as an open question, whether this
        endpoint needs authentication. A question the specification asked is
        not a way of signing in that this build added."""
        root = repo_the_way_the_factory_builds
        self._code_and_commit(root)
        report = self._report(root)

        assert report.capabilities_the_request_did_not_name == []

    def test_a_shebang_and_an_example_in_ordinary_code_are_still_not_addresses(
        self, repo_the_way_the_factory_builds: Path
    ) -> None:
        """The same filter, in a file that is NOT paperwork: a script the
        build really wrote still must not put its own shebang on the card."""
        root = repo_the_way_the_factory_builds
        files = self._code()
        files["scripts/backfill_created_at.py"] = (
            "#!/usr/bin/env python3\n"
            '"""Backfill the column.\n\nCall /admin/backfill by hand.\n"""\n'
            "# see /internal/notes for why\n"
            "def run():\n    return None\n"
        )
        for rel, text in files.items():
            _write(root, rel, text)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "what the build wrote")
        report = self._report(root)

        for false_address in ("/usr/bin/env", "/admin/backfill", "/internal/notes"):
            assert false_address not in report.routes_the_branch_declares

    def test_a_build_that_moved_the_address_is_still_named(
        self, repo_the_way_the_factory_builds: Path
    ) -> None:
        """Nothing above is allowed to silence the thing this line exists for:
        three of the twelve builds moved the endpoint, and that still shows."""
        root = repo_the_way_the_factory_builds
        files = self._code()
        files["src/users/router.py"] = (
            "# the users router\n"
            'stats_router = APIRouter(prefix="/stats")\n'
            '@stats_router.get("/users-created-per-day")\n'
            "def counts():\n    return []\n"
        )
        for rel, text in files.items():
            _write(root, rel, text)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "what the build wrote")
        report = self._report(root)

        assert "/stats" in report.routes_the_request_did_not_name
        assert "/users-created-per-day" in report.routes_the_request_did_not_name


class TestHowAPathIsRead:
    def test_a_dot_folder_keeps_its_dot(self) -> None:
        """``.guardkit/features/FEAT-1.yaml`` was being read as
        ``guardkit/features/FEAT-1.yaml``, because the old normaliser stripped
        the characters ``.`` and ``/`` one at a time, and so every folder rule
        below it missed the factory's own feature file."""
        from forge.pipeline.scope_report import _is_scaffolding, _repo_path

        assert _repo_path(".guardkit/features/FEAT-1.yaml") == (
            ".guardkit/features/FEAT-1.yaml"
        )
        assert _repo_path("./src/users/router.py") == "src/users/router.py"
        assert _repo_path("src\\users\\router.py") == "src/users/router.py"
        assert _is_scaffolding(".guardkit/features/FEAT-1.yaml") is True
        assert _is_scaffolding("src/users/router.py") is False

    def test_the_papers_the_factory_writes_are_not_the_thing_asked_for(self) -> None:
        from forge.pipeline.scope_report import _is_scaffolding

        for paper in (
            ".guardkit/features/FEAT-1.yaml",
            "feature_spec_inputs/abc.md",
            "features/daily-counts/daily-counts.feature",
            "qa/pass-bar-TASK-1.yaml",
            "qa/gates/a_gate.py",
            "tasks/backlog/TASK-1.md",
            "tasks/design_approved/TASK-1.md",
            "docs/api.md",
            "tests/test_counts.py",
        ):
            assert _is_scaffolding(paper) is True, paper
        for real in (
            "src/users/router.py",
            "alembic/versions/0001_add.py",
            "app/main.py",
            "scripts/backfill.py",
        ):
            assert _is_scaffolding(real) is False, real

    def test_an_address_written_in_pieces_is_not_a_new_address(self) -> None:
        """A router declares its address in two lines. Each piece read alone
        looked like somewhere nobody asked for."""
        from forge.pipeline.scope_report import _pieces_of_a_web_address

        pieces = _pieces_of_a_web_address("/users/created-per-day")
        assert pieces == ("/users", "/users/created-per-day", "/created-per-day")
        assert _pieces_of_a_web_address("/stats") == ("/stats",)
        assert _pieces_of_a_web_address("") == ()


class TestTheReasonsReadAsSentences:
    def test_two_reasons_are_two_sentences(self, tmp_path: Path) -> None:
        """Both halves can go uncounted at once, and the receipt then carried
        '…to compare against. the build this repair belongs to…' — a full
        stop followed by a lower-case word. Each reason now starts and ends
        like a sentence."""
        root = tmp_path / "api_test"
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "src/users/router.py", "# the users router\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "the repository as it stood")
        _build_on(root, {"src/users/router.py": "# the users router\ndef counts():\n    return []\n"})
        reading = read_branch_scope(
            repo_root=root, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(
            reading=reading,
            request=None,
            request_why_not="the build this repair belongs to is not in the record",
        )

        assert report.why_not is not None
        sentences = [part.strip() for part in report.why_not.split(". ") if part.strip()]
        assert len(sentences) == 2
        for sentence in sentences:
            assert sentence[0].isupper(), sentence
        assert report.why_not.endswith(".")
