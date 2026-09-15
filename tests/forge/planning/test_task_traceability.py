"""The plan read against the request — the guard, and the twelve real plans.

WHY THIS FILE EXISTS (2026-09-15). One sentence went through the factory twelve
times. The specification the person approved was right twelve times out of
twelve; the plan behind it was different every time, and every refusal in the
whole experiment came from the plan. The guard under test here is the
deterministic half of the cure: no model, pure text, never raises, and it reads
the whole plan tree before anything is committed.

The last test in this file is the acceptance test the design asks for
(``docs/planner-fix-design-2026-09-15.md`` section 3, lane F2): run the guard
over ALL TWELVE captured plan trees and compare, task by task, against the
retrospective that was written by hand from the same branches
(``docs/planner-fix-twelve-plan-table-2026-09-15.md``). Exactly ONE row is
expected to differ, and the design names it in advance: the plan that called
everything "metrics" comes back clean on its first task, because "metrics" is
deliberately off the shipped capability list — a list that flags a plan for its
choice of names is a list a person learns to ignore.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.planning.task_traceability import (
    CANNOT_CITE,
    CONTRADICTED_PATH,
    SCAFFOLDING_KINDS,
    UNASKED_CAPABILITY,
    excused_task_line,
    review_task_traceability,
)

#: The sentence, all twelve times.
REQUEST = (
    "Add a GET /users/created-per-day endpoint that returns the number of "
    "users created on each of the last 7 days, oldest first."
)

#: The boilerplate that appears in forty-nine of the fifty-eight task documents
#: measured, and must be struck before anyone asks whether a task said anything.
LINT_LINE = (
    "- [ ] All modified files pass project-configured lint/format checks "
    "with zero errors"
)


def _task(
    body: str,
    *,
    task_id: str = "TASK-AAAA-001",
    front: str = "",
    path: str | None = None,
) -> dict[str, str]:
    """One task document in a plan tree, as the plan writer emits it."""
    head = f"---\nid: {task_id}\nfeature_id: FEAT-AAAA\n{front}---\n\n"
    return {path or f"tasks/backlog/thing/{task_id}.md": head + body}


# ---------------------------------------------------------------------------
# Question one: does the task name a web address the request did not name?
# ---------------------------------------------------------------------------


def test_a_moved_web_address_is_a_contradiction() -> None:
    files = _task(
        "# Create the statistics endpoint\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] GET /stats/users-created-per-day returns 200 OK\n"
    )
    review = review_task_traceability(files, request_text=REQUEST)

    flags = [(f.flag, f.what) for f in review.findings]
    assert (CONTRADICTED_PATH, "/stats/users-created-per-day") in flags
    # It is read out of an acceptance criterion, which is where the plan that
    # actually moved the endpoint said it and nowhere else.
    assert review.stops_the_run is True


def test_the_address_the_request_names_is_never_a_contradiction() -> None:
    files = _task(
        "# Add the endpoint\n\nImplement GET /users/created-per-day.\n"
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert [f.flag for f in review.findings if f.flag == CONTRADICTED_PATH] == []


def test_an_address_the_repository_already_has_is_not_this_plans_invention() -> None:
    files = _task("# Reuse the counter\n\nCall /users/count-today for the total.\n")

    without_facts = review_task_traceability(files, request_text=REQUEST)
    assert any(f.flag == CONTRADICTED_PATH for f in without_facts.findings)

    with_facts = review_task_traceability(
        files,
        request_text=REQUEST,
        repository_facts=(
            "src/users/router.py already declares GET /users/count-today. "
            "None of them declares an authentication dependency."
        ),
    )
    assert [f.flag for f in with_facts.findings if f.flag == CONTRADICTED_PATH] == []


def test_a_file_under_a_declared_test_root_is_a_place_on_disk() -> None:
    files = _task(
        "# Add the tests\n\n"
        "## Implementation Notes\n\n"
        "- Add to /tests/users/test_analytics.py and /docs/api.md\n",
        front="task_type: testing\n",
    )
    review = review_task_traceability(
        files,
        request_text=REQUEST,
        test_roots=["tests/health", "tests/users"],
    )

    assert review.findings == []


# ---------------------------------------------------------------------------
# Question two: does it ask for a capability the request never mentioned?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "capability"),
    [
        ("Endpoint requires authentication", "authentication"),
        ("Add an alembic revision for the new column", "a database migration"),
        ("Implement the analytics service layer", "a service layer or module"),
        ("Implement error handling for the endpoint", "error handling"),
        ("Add structured logs for every call", "logging"),
    ],
)
def test_a_capability_nobody_asked_for_is_named(words: str, capability: str) -> None:
    files = _task(
        f"# A task\n\nDo the work.\n\n## Acceptance Criteria\n\n- [ ] {words}\n"
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert (UNASKED_CAPABILITY, capability) in [
        (f.flag, f.what) for f in review.findings
    ]
    assert review.stops_the_run is True


def test_metrics_is_deliberately_not_on_the_list() -> None:
    """A plan that calls everything "metrics" chose a naming family; it did not
    invent a capability. Flagging it would teach a person to ignore the guard."""
    files = _task("# Create metrics endpoint\n\nGET /users/created-per-day.\n")
    review = review_task_traceability(files, request_text=REQUEST)

    assert review.findings == []


def test_a_capability_the_request_itself_asks_for_is_a_reading_not_an_addition() -> None:
    request = "Add a GET /admin/users endpoint that requires authentication."
    files = _task(
        "# The admin list\n\nGET /admin/users, and it requires authentication.\n"
    )
    review = review_task_traceability(files, request_text=request)

    assert review.findings == []


def test_a_scaffolding_task_is_never_excused_from_the_first_two_questions() -> None:
    """Fourteen of the fifty-eight tasks measured were test tasks — and the
    authentication that got two runs refused sat inside a test task's
    acceptance criteria, twice."""
    files = _task(
        "# Add the tests\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] Test the endpoint requires authentication\n",
        front="scaffolding_kind: tests\n",
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert [(f.flag, f.what) for f in review.findings] == [
        (UNASKED_CAPABILITY, "authentication")
    ]


# ---------------------------------------------------------------------------
# Question three: can it point at the words of the request it serves?
# ---------------------------------------------------------------------------


def test_a_task_that_quotes_nothing_is_named_and_never_stops_a_run() -> None:
    files = _task("# Create analytics schema\n\nDefine the models.\n")
    review = review_task_traceability(files, request_text=REQUEST)

    assert [f.flag for f in review.findings] == [CANNOT_CITE]
    assert review.sends_it_back is True
    assert review.stops_the_run is False
    assert "do not quote any of the words" in (review.cannot_cite_line() or "")


def test_three_consecutive_words_of_the_request_are_a_citation() -> None:
    files = _task("# The query\n\nCount users for each of the last 7 days.\n")
    review = review_task_traceability(files, request_text=REQUEST)

    assert review.findings == []


def test_the_task_quotes_the_request_under_its_own_heading() -> None:
    files = _task(
        "# The query\n\n"
        "## The words of the request this task serves\n\n"
        "> the number of users created\n",
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert review.findings == []


def test_the_excused_line_is_a_declaration_not_a_quote() -> None:
    """The exact string a task writes when it genuinely cannot quote the
    request. It is not a citation; what excuses the task is its kind."""
    assert excused_task_line("tests") == "_none — this is a tests task_"

    excused = _task(
        "# Add the tests\n\n"
        "## The words of the request this task serves\n\n"
        f"{excused_task_line('tests')}\n",
        front="scaffolding_kind: tests\n",
    )
    assert review_task_traceability(excused, request_text=REQUEST).findings == []

    # The same document with no kind to stand on is a task that said nothing.
    unexcused = _task(
        "# Add the tests\n\n"
        "## The words of the request this task serves\n\n"
        f"{excused_task_line('tests')}\n",
    )
    review = review_task_traceability(unexcused, request_text=REQUEST)
    assert [f.flag for f in review.findings] == [CANNOT_CITE]


def test_a_kind_outside_the_four_excuses_nothing() -> None:
    files = _task(
        "# Add the error handling\n\nDo the work.\n",
        front="scaffolding_kind: error-handling\n",
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert CANNOT_CITE in [f.flag for f in review.findings]
    assert "error-handling" not in SCAFFOLDING_KINDS


def test_the_lint_boilerplate_is_struck_before_the_question_is_asked() -> None:
    """Forty-nine of the fifty-eight task documents carry this one sentence.
    Left in, every task looks as though it quoted something."""
    request = "List all modified files in the repository, newest first."
    files = _task(f"# A task\n\n## Acceptance Criteria\n\n{LINT_LINE}\n")

    review = review_task_traceability(files, request_text=request)
    assert [f.flag for f in review.findings] == [CANNOT_CITE]

    # Proof the strike is what did it: the same words outside the boilerplate
    # sentence really are a citation.
    real = _task("# A task\n\nThis lists all modified files for the person.\n")
    assert review_task_traceability(real, request_text=request).findings == []


def test_words_borrowed_from_an_invented_address_are_not_the_requests_words() -> None:
    """The plan that moved the endpoint to /stats/users-created-per-day shares
    the words "users created per" with the request only because it renamed the
    request's own address. That is not a quote, and the retrospective read it
    the same way."""
    files = _task(
        "## Description\n\nCreate the endpoint.\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] GET /stats/users-created-per-day returns 200 OK\n"
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert sorted(f.flag for f in review.findings) == [CANNOT_CITE, CONTRADICTED_PATH]


# ---------------------------------------------------------------------------
# It never raises, and it says what it could not read
# ---------------------------------------------------------------------------


def test_a_document_that_cannot_be_read_is_said_not_raised() -> None:
    files = {
        "tasks/backlog/thing/TASK-AAAA-001.md": None,  # type: ignore[dict-item]
        "tasks/backlog/thing/TASK-AAAA-002.md": (
            "---\nid: TASK-AAAA-002\n  broken: [yaml\n---\n\n# A task\n"
        ),
        ".guardkit/features/FEAT-AAAA.yaml": "id: FEAT-AAAA\n",
    }
    review = review_task_traceability(files, request_text=REQUEST)

    # Two task documents read, the feature file left alone, nothing raised.
    assert review.tasks_read == 2
    assert [f.task_id for f in review.findings] == [
        "TASK-AAAA-001",
        "TASK-AAAA-002",
    ]


def test_no_request_means_nothing_to_judge_against() -> None:
    files = _task("# A task\n\nDo the work at /stats/anything.\n")
    review = review_task_traceability(files, request_text="")

    # A blank request cannot make a route a contradiction of anything, and the
    # citation question has no words to look for.
    assert [f.flag for f in review.findings] == [CONTRADICTED_PATH, CANNOT_CITE]


def test_the_note_carries_every_finding_word_for_word() -> None:
    files = {
        **_task(
            "# Create the statistics endpoint\n\nGET /stats/users-created-per-day.\n",
            task_id="TASK-AAAA-001",
        ),
        **_task(
            "# Add the migration\n\nAdd an alembic revision.\n",
            task_id="TASK-AAAA-002",
        ),
    }
    review = review_task_traceability(files, request_text=REQUEST)
    note = review.note()

    for finding in review.findings:
        assert finding.sentence in note
    assert "it may not add" in note


# ---------------------------------------------------------------------------
# THE ACCEPTANCE TEST — all twelve captured plans, row by row
# ---------------------------------------------------------------------------

#: Where the lead exported the twelve plan trees, read-only, at the start of
#: this work. They are read from there and never from the factory-built
#: repository on the host.
TWELVE = Path(
    os.environ.get(
        "TWELVE_PLAN_TREES",
        "/tmp/claude-1000/-home-richardwoollcott-Projects-appmilla-github-"
        "ai-transition/e3a08329-6e4c-4dee-a1cf-d85b04d5eeb2/scratchpad/"
        "twelve/planning",
    )
)

#: Arm → (the exported plan tree, the feature the arm planned). Straight out of
#: ``docs/planner-fix-twelve-plan-table-2026-09-15.md``, one row per arm.
ARMS: dict[str, tuple[str, str]] = {
    "A1": ("77e41562-e481-4eb3-96ea-d2b1d3d53a55", "FEAT-B539"),
    "A2": ("5a93d29c-25e2-4aa5-be3f-0ed36ffa1aeb", "FEAT-9230"),
    "A3": ("921da05c-2d66-411f-ae4c-9c455b69fa94", "FEAT-BB40"),
    "B1": ("5f26899c-47ec-4f46-970a-c02e6e4147c7", "FEAT-BD8F"),
    "B6": ("8ca406dd-2ee3-4285-b71b-e465bba5a8dc", "FEAT-3560"),
    "B7": ("4108c150-9767-45ca-b4ef-934b0b6106b4", "FEAT-54E1"),
    "B8": ("01c90fa9-086a-423b-a4b5-5f3a56be6408", "FEAT-D49B"),
    "C1": ("a04f1cf2-69ca-4fa6-bd86-aefa49dd50df", "FEAT-6F57"),
    "C2": ("181186ba-8602-47f7-b4eb-4d9606586194", "FEAT-6F3D"),
    "C3": ("6584fe6b-34e2-49e1-b5a0-c104426ba6ef", "FEAT-A0AE"),
    "C4": ("48d5979d-8dfe-40ca-98e6-0e4246b6b6bb", "FEAT-CCBF"),
    "C5": ("af89e008-0024-417f-85f5-faac37dfdb26", "FEAT-C9C4"),
}

#: The retrospective, transcribed task by task. ``flags`` is what the table's
#: own "Flag" column says, in this guard's three words; ``capabilities`` is the
#: capability classes it names, in the words of the shipped list. A task the
#: table left blank is absent from this mapping.
#:
#: "metrics" is recorded here exactly as the table wrote it, because the ONE
#: expected difference is measured against the table as published.
TABLE: dict[str, dict[str, object]] = {
    # A1 — the cleanest of the twelve on the public contract.
    "TASK-B539-003": {"flags": {CANNOT_CITE}, "capabilities": set()},
    # A2 — the plan that moved the endpoint, and said so only in a criterion.
    "TASK-9230-001": {
        "flags": {CONTRADICTED_PATH, CANNOT_CITE},
        "capabilities": set(),
    },
    # A3 — clean on every arm of the rule, and nothing went wrong in it.
    # B1 — refused at the live gate for a login requirement nobody asked for.
    "TASK-BD8F-001": {"flags": {CANNOT_CITE}, "capabilities": set()},
    "TASK-BD8F-003": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"authentication"},
    },
    "TASK-BD8F-004": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"authentication"},
    },
    # B6 — the migration that later poisoned the next run's database.
    "TASK-3560-003": {
        "flags": {UNASKED_CAPABILITY, CANNOT_CITE},
        "capabilities": {"a database migration"},
    },
    # B7 — "Add error handling", the task that produced an application-wide
    # rewrite although its own body said "for the daily counts endpoint".
    "TASK-54E1-004": {
        "flags": {UNASKED_CAPABILITY, CANNOT_CITE},
        "capabilities": {"error handling"},
    },
    # B8 — five task documents with no description at all; no capability word
    # anywhere, so only the citation question catches this plan.
    "TASK-D49B-001": {"flags": {CANNOT_CITE}, "capabilities": set()},
    "TASK-D49B-002": {"flags": {CANNOT_CITE}, "capabilities": set()},
    "TASK-D49B-004": {"flags": {CANNOT_CITE}, "capabilities": set()},
    # C1 — the worst of the twelve: the path moved, a login requirement and a
    # migration were added beside it.
    "TASK-6F57-001": {"flags": {CONTRADICTED_PATH}, "capabilities": set()},
    "TASK-6F57-003": {
        "flags": {UNASKED_CAPABILITY, CANNOT_CITE},
        "capabilities": {"a database migration"},
    },
    "TASK-6F57-004": {
        # The table names authorisation beside authentication; the shipped list
        # covers both words in its first entry, so it is one class here.
        "flags": {UNASKED_CAPABILITY, CANNOT_CITE},
        "capabilities": {"authentication"},
    },
    "TASK-6F57-005": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"authentication"},
    },
    # C2 — the five-module package that named no source file at all.
    "TASK-6F3D-001": {
        "flags": {UNASKED_CAPABILITY, CANNOT_CITE},
        "capabilities": {"a database migration"},
    },
    "TASK-6F3D-002": {"flags": {CANNOT_CITE}, "capabilities": set()},
    "TASK-6F3D-004": {
        "flags": {UNASKED_CAPABILITY, CANNOT_CITE},
        "capabilities": {"a service layer or module"},
    },
    # C3 — the same shape as C2, with a task called "service layer".
    "TASK-A0AE-001": {"flags": {CANNOT_CITE}, "capabilities": set()},
    "TASK-A0AE-002": {"flags": {CANNOT_CITE}, "capabilities": set()},
    "TASK-A0AE-004": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"a service layer or module"},
    },
    # C4 — refused at the live gate because the plan moved the endpoint.
    "TASK-CCBF-001": {"flags": {CONTRADICTED_PATH}, "capabilities": set()},
    "TASK-CCBF-003": {"flags": {CANNOT_CITE}, "capabilities": set()},
    # C5 — error handling again, and the whole plan framed as "metrics".
    "TASK-C9C4-001": {"flags": {UNASKED_CAPABILITY}, "capabilities": {"metrics"}},
    "TASK-C9C4-003": {
        "flags": {UNASKED_CAPABILITY, CANNOT_CITE},
        "capabilities": {"metrics"},
    },
    "TASK-C9C4-004": {
        "flags": {UNASKED_CAPABILITY, CANNOT_CITE},
        "capabilities": {
            "a service layer or module",
            "error handling",
            "metrics",
        },
    },
    "TASK-C9C4-005": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"error handling", "metrics"},
    },
}

#: The one row the design says in advance will differ, and why. "metrics" is
#: off the shipped capability list, so the task whose ONLY flag was that word
#: comes back clean. Nothing else changes: the other three "metrics" tasks stay
#: flagged, for citing nothing, for a service module and for error handling.
THE_ONE_EXPECTED_DIFFERENCE = "TASK-C9C4-001"


def _as_the_shipped_list_reads_it(row: dict[str, object]) -> dict[str, set[str]]:
    """The table's row with "metrics" struck out of it.

    The retrospective was measured with "metrics" on the capability list; the
    shipped list leaves it off, for the reason section 2g gives. Striking it
    here is what makes the comparison below a comparison of the rule against
    the evidence, rather than of the rule against a word it never had.
    """
    flags = set(row["flags"])  # type: ignore[arg-type]
    capabilities = set(row["capabilities"]) - {"metrics"}  # type: ignore[arg-type]
    if not capabilities:
        flags.discard(UNASKED_CAPABILITY)
    return {"flags": flags, "capabilities": capabilities}


def _plan_tree(root: Path, feature_id: str) -> dict[str, str]:
    """One captured plan tree as the driver holds it: repository-relative path
    to content, the feature file and every task document of that feature."""
    stem = feature_id.split("-", 1)[1]
    files: dict[str, str] = {}
    for task in sorted(root.rglob(f"TASK-{stem}-*.md")):
        files[str(task.relative_to(root))] = task.read_text()
    feature = root / ".guardkit" / "features" / f"{feature_id}.yaml"
    if feature.is_file():
        files[str(feature.relative_to(root))] = feature.read_text()
    return files


@pytest.mark.skipif(
    not TWELVE.is_dir(),
    reason="the twelve captured plan trees are not exported on this machine",
)
def test_the_guard_agrees_with_the_twelve_plan_retrospective() -> None:
    """Run the guard over all twelve captured plans and compare, row by row,
    with the table a person wrote by hand from the same branches.

    Exactly one row may differ, and the design names it before the test runs.
    A second difference is a defect in the guard, not in the table.
    """
    differences: list[str] = []
    verdict_differences: list[str] = []
    flagged_plans = 0
    plans_that_can_stop_a_run = 0

    for arm, (tree, feature_id) in ARMS.items():
        root = TWELVE / tree
        assert root.is_dir(), f"{arm}: the exported plan tree is missing"
        files = _plan_tree(root, feature_id)
        assert files, f"{arm}: no task documents were exported for {feature_id}"

        review = review_task_traceability(files, request_text=REQUEST)
        assert review.unreadable == [], f"{arm}: {review.unreadable}"
        assert review.tasks_read in (4, 5), f"{arm}: read {review.tasks_read} tasks"

        if review.findings:
            flagged_plans += 1
        if review.contradictions:
            plans_that_can_stop_a_run += 1

        found: dict[str, dict[str, set[str]]] = {}
        for finding in review.findings:
            row = found.setdefault(
                finding.task_id, {"flags": set(), "capabilities": set()}
            )
            row["flags"].add(finding.flag)
            if finding.flag == UNASKED_CAPABILITY:
                row["capabilities"].add(finding.what)

        # Every task of this plan, by the id in its own front matter — which
        # is the first three parts of its file name, the slug after it being
        # the writer's own words.
        task_ids = sorted(
            {
                "-".join(str(path).rsplit("/", 1)[-1].split("-")[:3])
                for path in files
                if str(path).rsplit("/", 1)[-1].startswith("TASK-")
            }
        )
        assert len(task_ids) == review.tasks_read, f"{arm}: {task_ids}"
        for task_id in task_ids:
            published = TABLE.get(task_id, {"flags": set(), "capabilities": set()})
            expected = _as_the_shipped_list_reads_it(published)
            actual = found.get(task_id, {"flags": set(), "capabilities": set()})
            if (
                actual["flags"] != expected["flags"]
                or actual["capabilities"] != expected["capabilities"]
            ):
                differences.append(
                    f"{arm} {task_id}: the table says "
                    f"{sorted(expected['flags'])} {sorted(expected['capabilities'])}, "
                    f"the guard says "
                    f"{sorted(actual['flags'])} {sorted(actual['capabilities'])}"
                )
            # The row-level verdict — flagged or clean — against the table AS
            # PUBLISHED. This is the comparison section 2g reasons about, and
            # exactly one row of the fifty-eight may come out differently.
            if bool(published["flags"]) != bool(actual["flags"]):  # type: ignore[arg-type]
                verdict_differences.append(f"{arm} {task_id}")

    # Row by row, once "metrics" is struck from the table, the guard and the
    # hand-written retrospective agree on every one of the fifty-eight tasks.
    assert differences == [], "\n".join(differences)

    # And striking "metrics" changed exactly ONE row's verdict, the row the
    # design named before this test was written: the task whose only flag was
    # that word comes back clean. A second row here is a defect in the guard.
    assert verdict_differences == [f"C5 {THE_ONE_EXPECTED_DIFFERENCE}"]

    # The plan counts the design rests on still hold: eleven of the twelve
    # plans draw a flag, and nine of the twelve draw one that can stop a run.
    assert flagged_plans == 11
    assert plans_that_can_stop_a_run == 9
