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


class TestFactsNotVerdicts:
    def test_an_echo_line_and_an_import_path_are_not_stated_as_routes(
        self, repo: Path
    ) -> None:
        """First step 1 run, feature 2 (26 October 2026): the card said the
        build serves ``${folder}/node_modules``, read from an ``echo`` line in
        ``scripts/check.sh``, and the local merge judge declined good work
        over it; feature 8 read ``./config/settings`` the same way. The card
        now states only the files the build changed, against the plan."""
        from forge.cli._serve_gate_activation import card_line_about_scope

        _build_on(
            repo,
            {
                "scripts/check.sh": (
                    "#!/bin/sh\n"
                    'for folder in apps/api; do\n'
                    '  [ -d "$folder/node_modules" ] || echo '
                    '"check.sh: ${folder}/node_modules is missing" >&2\n'
                    "done\n"
                ),
                "src/users/router.ts": (
                    'import { settings } from "./config/settings";\n'
                    'res.setHeader("Access-Control-Allow-Origin", origin);\n'
                    "router.get('/users/created-per-day', counts);\n"
                ),
            },
        )
        reading = read_branch_scope(
            repo_root=repo, base="main", head=BRANCH, feature_id=FEATURE_ID
        )
        report = scope_of_the_build(reading=reading, request=REQUEST)

        assert report.read is True
        assert sorted(report.files_the_plan_did_not_name) == [
            "scripts/check.sh",
            "src/users/router.ts",
        ]
        line = card_line_about_scope(report)
        assert "node_modules" not in line
        assert "web address" not in line
        assert "permissions" not in line
        assert "did not ask for" not in line
        # The diff itself is still read for the card's details.
        assert reading.patch_read is True
        assert "node_modules" in reading.patch


class TestWhenNothingCouldBeRead:
    def test_a_branch_that_is_not_there_is_said_plainly(self, repo: Path) -> None:
        reading = read_branch_scope(
            repo_root=repo,
            base="main",
            head="autobuild/FEAT-NOTHERE",
            feature_id="FEAT-NOTHERE",
        )
        assert reading.error is not None
        # The card's scope line carries the factory's own sentence, never
        # git's error text (which names the revision git could not find).
        assert "git diff stopped with exit code" in reading.error
        assert "fatal" not in reading.error.lower()
        assert "unknown revision" not in reading.error.lower()
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

class TestTheReceipt:
    def test_it_lands_beside_the_build_s_own_evidence(self, tmp_path: Path) -> None:
        receipts = tmp_path / "receipts" / "build-X-1"
        receipts.mkdir(parents=True)
        report = ScopeReport(read=True, plan_read=True)
        report.files_changed = 2
        written = write_scope_report("build-X-1", report, receipts_dir=receipts)
        assert written is not None
        assert written.name == SCOPE_REPORT_NAME
        kept = json.loads(written.read_text(encoding="utf-8"))
        assert kept["read"] is True
        assert kept["files_changed"] == 2
        assert "routes_read" not in kept
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
            "plan_read",
            "files_that_already_existed",
        ):
            assert field in kept, field
        # Facts, not verdicts (step 2, 26 October 2026).
        for gone in (
            "routes_in_the_request",
            "routes_the_branch_declares",
            "routes_the_request_did_not_name",
            "capabilities_the_request_did_not_name",
            "routes_read",
        ):
            assert gone not in kept, gone


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

    def test_a_clean_build_says_nothing_beyond_the_plan(
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
        assert report.files_the_plan_did_not_name == []
        assert card_line_about_scope(report) == (
            "Every file this build changed was named in the plan."
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
            "you merge."
        )

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
