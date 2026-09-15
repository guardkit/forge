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
    added_lines_of,
    plan_document_paths,
    read_branch_scope,
)
from forge.pipeline.scope_report import (
    SCOPE_REPORT_NAME,
    ScopeReport,
    files_the_plan_named,
    read_declared_files,
    scope_of_the_build,
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
