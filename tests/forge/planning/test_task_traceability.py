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
(``docs/planner-fix-twelve-plan-table-2026-09-15.md``). The rows expected to
differ are named in advance: the plan that called everything "metrics" comes
back clean on the tasks whose only flag was that word, because "metrics" is
deliberately off the shipped capability list — a list that flags a plan for its
choice of names is a list a person learns to ignore. Since 16 October 2026 the
table's "cannot cite" flag (not quoting the request word for word) is struck,
because that question was removed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.planning.task_traceability import (
    CONTRADICTED_PATH,
    UNASKED_CAPABILITY,
    review_task_traceability,
)

#: The sentence, all twelve times.
REQUEST = (
    "Add a GET /users/created-per-day endpoint that returns the number of "
    "users created on each of the last 7 days, oldest first."
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


@pytest.mark.parametrize("disk_path", ["/tmp/kcadm-", "/dev/null", "/var/log/app", "/dev/tcp/keycloak/8080", "/opt/keycloak/bin/kcadm", "/tmp}/kc-readiness-XXXXXX"])
def test_an_operating_system_folder_is_a_place_on_disk(disk_path: str) -> None:
    # Found live on 15 October 2026: a Keycloak setup script in a task
    # document wrote its temporary files to /tmp/kcadm- and its noise to
    # /dev/null, and the card said the task "answers at /tmp/kcadm-".
    files = _task(
        "# Set up the identity server\n\n"
        "## Implementation Notes\n\n"
        f"- The setup script writes to {disk_path} while it runs\n"
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert [f.what for f in review.findings if f.flag == CONTRADICTED_PATH] == []


@pytest.mark.parametrize("route", ["/dev/seed", "/sys/user"])
def test_a_web_address_under_a_system_sounding_folder_is_still_named(
    route: str,
) -> None:
    # ``/dev`` and ``/sys`` are real places on disk, but applications also
    # answer at ``/dev/seed`` and ``/sys/user``. Only the well-known device
    # files are skipped inside ``/dev``, and ``/sys`` is not skipped at all.
    files = _task(
        "# Add a helper endpoint\n\n"
        "## Implementation Notes\n\n"
        f"- Add POST {route} for local development\n"
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert [f.what for f in review.findings if f.flag == CONTRADICTED_PATH] == [
        route
    ]


def test_an_unnamed_web_address_beside_a_disk_path_is_still_named() -> None:
    files = _task(
        "# Set up the identity server\n\n"
        "## Implementation Notes\n\n"
        "- The script logs to /dev/null and the service answers at /api/notes\n"
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert [f.what for f in review.findings if f.flag == CONTRADICTED_PATH] == [
        "/api/notes"
    ]


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
# No exact wording (16 October 2026): how a task words what it serves is never
# held against it. Rich: "insists on exact wording from an LLM generated text
# is just never going to be reliable so I don't think insisting on exact
# wording makes sense".
# ---------------------------------------------------------------------------


def test_a_task_that_paraphrases_the_request_is_clear() -> None:
    files = _task(
        "# The daily totals\n\n"
        "## The words of the request this task serves\n\n"
        "> Provide a way to see how many accounts appeared recently.\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] GET /users/created-per-day answers with seven daily counts\n",
    )
    review = review_task_traceability(files, request_text=REQUEST)

    assert review.findings == []
    assert review.sends_it_back is False
    assert review.stops_the_run is False


def test_a_task_that_quotes_nothing_is_clear() -> None:
    files = _task("# Create analytics schema\n\nDefine the models.\n")
    review = review_task_traceability(files, request_text=REQUEST)

    assert review.findings == []
    assert review.sends_it_back is False


def test_a_paraphrase_in_its_own_words_sends_nothing_back_and_names_nothing() -> None:
    """The plain prose a plan writer really writes, in place of a quote: no
    finding, no note, and nothing in the receipt that a card could show."""
    request = "Refuse to create a note with an empty or blank title."
    files = _task(
        "# Blank titles\n\n"
        "## The words of the request this task serves\n\n"
        "Refusing to create a note with an empty title.\n",
    )
    review = review_task_traceability(files, request_text=request)

    assert review.findings == []
    assert review.sends_it_back is False
    assert review.receipt()["flagged"] == []
    assert "CANNOT_CITE" not in str(review.receipt())


def test_a_paraphrasing_task_that_moves_the_address_is_still_sent_back() -> None:
    """Removing the wording rule takes nothing away from the two real
    additions: a task naming /notes/v2 when the request names /notes is still
    a contradiction, whatever words it uses."""
    request = "Add a GET /notes endpoint that lists every note."
    files = _task(
        "# List the notes\n\n"
        "## The words of the request this task serves\n\n"
        "Listing all the notes.\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] GET /notes/v2 returns every note\n",
    )
    review = review_task_traceability(files, request_text=request)

    assert [(f.flag, f.what) for f in review.findings] == [
        (CONTRADICTED_PATH, "/notes/v2")
    ]
    assert review.sends_it_back is True
    assert review.stops_the_run is True


def test_a_paraphrasing_task_that_adds_rate_limiting_is_still_sent_back() -> None:
    request = "Add a GET /notes endpoint that lists every note."
    files = _task(
        "# List the notes\n\n"
        "## The words of the request this task serves\n\n"
        "Listing all the notes.\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] GET /notes applies rate limiting to each caller\n",
    )
    review = review_task_traceability(files, request_text=request)

    assert [(f.flag, f.what) for f in review.findings] == [
        (UNASKED_CAPABILITY, "rate limiting")
    ]
    assert review.sends_it_back is True
    assert review.stops_the_run is True


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

    # Two task documents read, the feature file left alone, nothing raised,
    # and neither says anything the request did not ask for.
    assert review.tasks_read == 2
    assert review.unreadable == []
    assert review.findings == []


def test_no_request_means_nothing_to_judge_against() -> None:
    files = _task("# A task\n\nDo the work at /stats/anything.\n")
    review = review_task_traceability(files, request_text="")

    # A blank request names no web address, so any address a task names is
    # one the request did not name.
    assert [f.flag for f in review.findings] == [CONTRADICTED_PATH]


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
#: own "Flag" column says, in this guard's words; ``capabilities`` is the
#: capability classes it names, in the words of the shipped list. A task the
#: table left blank is absent from this mapping.
#:
#: The table's third flag, "cannot cite" (a task that did not quote the
#: request word for word), is struck from every row: that question was removed
#: on 16 October 2026, so a row whose only flag it was is now absent.
#:
#: "metrics" is recorded here exactly as the table wrote it, because the ONE
#: expected difference is measured against the table as published.
TABLE: dict[str, dict[str, object]] = {
    # A1 — the cleanest of the twelve on the public contract.
    # A2 — the plan that moved the endpoint, and said so only in a criterion.
    "TASK-9230-001": {
        "flags": {CONTRADICTED_PATH},
        "capabilities": set(),
    },
    # A3 — clean on every arm of the rule, and nothing went wrong in it.
    # B1 — refused at the live gate for a login requirement nobody asked for.
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
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"a database migration"},
    },
    # B7 — "Add error handling", the task that produced an application-wide
    # rewrite although its own body said "for the daily counts endpoint".
    "TASK-54E1-004": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"error handling"},
    },
    # B8 — five task documents with no description at all and no capability
    # word anywhere. Only the citation question (removed 16 October 2026)
    # caught this plan, so nothing flags it now.
    # C1 — the worst of the twelve: the path moved, a login requirement and a
    # migration were added beside it.
    "TASK-6F57-001": {"flags": {CONTRADICTED_PATH}, "capabilities": set()},
    "TASK-6F57-003": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"a database migration"},
    },
    "TASK-6F57-004": {
        # The table names authorisation beside authentication; the shipped list
        # covers both words in its first entry, so it is one class here.
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"authentication"},
    },
    "TASK-6F57-005": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"authentication"},
    },
    # C2 — the five-module package that named no source file at all.
    "TASK-6F3D-001": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"a database migration"},
    },
    "TASK-6F3D-004": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"a service layer or module"},
    },
    # C3 — the same shape as C2, with a task called "service layer".
    "TASK-A0AE-004": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"a service layer or module"},
    },
    # C4 — refused at the live gate because the plan moved the endpoint.
    "TASK-CCBF-001": {"flags": {CONTRADICTED_PATH}, "capabilities": set()},
    # C5 — error handling again, and the whole plan framed as "metrics".
    "TASK-C9C4-001": {"flags": {UNASKED_CAPABILITY}, "capabilities": {"metrics"}},
    "TASK-C9C4-003": {
        "flags": {UNASKED_CAPABILITY},
        "capabilities": {"metrics"},
    },
    "TASK-C9C4-004": {
        "flags": {UNASKED_CAPABILITY},
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

#: The rows the design says in advance will differ, and why. "metrics" is off
#: the shipped capability list, so a task whose ONLY remaining flag was that
#: word comes back clean. Until 16 October 2026 that was one row; TASK-C9C4-003
#: was also flagged for not quoting the request word for word, and that
#: question is gone, so it joins it. The other two "metrics" tasks stay
#: flagged, for a service module and for error handling.
THE_EXPECTED_DIFFERENCES = ("TASK-C9C4-001", "TASK-C9C4-003")


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

    Only the rows named in ``THE_EXPECTED_DIFFERENCES`` may differ. Any other
    difference is a defect in the guard, not in the table.
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
            # PUBLISHED (less its struck "cannot cite" flag). This is the
            # comparison section 2g reasons about.
            if bool(published["flags"]) != bool(actual["flags"]):  # type: ignore[arg-type]
                verdict_differences.append(f"{arm} {task_id}")

    # Row by row, once "metrics" is struck from the table, the guard and the
    # hand-written retrospective agree on every one of the fifty-eight tasks.
    assert differences == [], "\n".join(differences)

    # And striking "metrics" changed only the verdicts named before this test
    # ran: the tasks whose only flag was that word come back clean. Any other
    # row here is a defect in the guard.
    assert verdict_differences == [f"C5 {task}" for task in THE_EXPECTED_DIFFERENCES]

    # Nine of the twelve plans draw a flag, and every flag can stop a run.
    # (Before 16 October 2026 eleven drew one: A1 and B8 were flagged only for
    # not quoting the request word for word.)
    assert flagged_plans == 9
    assert plans_that_can_stop_a_run == 9


def test_a_closing_markup_tag_is_not_a_web_address() -> None:
    from forge.planning.task_traceability import _route_shaped

    found = _route_shaped('<span className="date">{created}</span> <time>{t}</time> GET /api/notes')
    assert "/span" not in found and "/time" not in found
    assert "/api/notes" in found
