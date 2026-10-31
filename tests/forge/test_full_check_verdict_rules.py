"""The full check reads each part's run with GuardKit's own rules.

Codex's second check of the after-task check redesign, 31 October 2026: the
merge card's full check dropped a part's declared "no tests ran" fields
(``absent_substrings``, ``ran_marker_regex``, ``requires_ran_marker``) and
called every exit 0 a pass, while GuardKit's join applies them, so a card
could be offered for a part that ran no test and the join then refused it.

Each part now carries those fields and is read with GuardKit's own
``declared_no_tests_ran`` and ``ran_marker_problem``, taken from the same
module as ``full_check_parts``. GuardKit's module is not importable in
Forge's tests, so its functions are stood in
(``tests/forge/full_check_stand_in.py``); on 31 October 2026 the stand-ins
gave the same answer as GuardKit's real functions on every case in
:data:`CASES`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from forge.full_check import (
    COULD_NOT_RUN,
    FAILED,
    PASSED,
    CheckPart,
    RanOutput,
    full_check_parts_of,
    run_full_check,
)
from tests.forge.full_check_stand_in import (
    RULES,
    declared_no_tests_ran,
    ran_marker_problem,
    stand_in_for_guardkit,
)

LOADER = "tests_full_check_verdict_rules_toolchain"

#: (declared fields, exit code, output, what GuardKit's rule says).
CASES: list[tuple[dict[str, Any], int | None, str, str | None]] = [
    ({}, 0, "[no test files]", None),
    ({}, 1, "[no test files]", None),
    ({"absent_substrings": ["[no test files]"]}, 0, "ok  api [no test files]\n", 'the output contains "[no test files]"'),
    ({"absent_substrings": ["No Tests Ran"]}, 0, "\nno tests ran in 0.01s", 'the output contains "No Tests Ran"'),
    ({"absent_substrings": ["[no test files]"]}, 1, "[no test files]", 'the output contains "[no test files]"'),
    ({"absent_substrings": ["[no test files]"]}, 0, "ok  api 0.01s", None),
    ({"absent_substrings": [""]}, 0, "anything", None),
    ({"ran_marker_regex": r"[0-9]+ passed"}, 0, "3 passed in 0.1s", None),
    ({"ran_marker_regex": r"[0-9]+ passed"}, 0, "nothing here", "no line matched [0-9]+ passed"),
    ({"ran_marker_regex": r"[0-9]+ passed"}, 1, "nothing here", None),
    ({"ran_marker_regex": r"[0-9]+ passed", "requires_ran_marker": False}, 0, "nothing", None),
    ({"ran_marker_regex": r"[0-9]+ passed", "requires_ran_marker": True}, 0, "nothing", "no line matched [0-9]+ passed"),
    ({"ran_marker_regex": r"^3 passed"}, 0, "\n3 passed", "no line matched ^3 passed"),
    ({"requires_ran_marker": True}, 0, "3 passed", None),
]

#: (declared fields, what GuardKit's ``ran_marker_problem`` says).
PROBLEM_CASES: list[tuple[dict[str, Any], str | None]] = [
    ({}, None),
    ({"requires_ran_marker": True}, (
        "declares `requires_ran_marker: true` but no `ran_marker_regex`, "
        "so nothing in its output could show that a test ran"
    )),
    ({"requires_ran_marker": True, "ran_marker_regex": "x"}, None),
    ({"requires_ran_marker": False}, None),
]


def _commands(fields: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        absent_substrings=fields.get("absent_substrings"),
        ran_marker_regex=fields.get("ran_marker_regex"),
        requires_ran_marker=fields.get("requires_ran_marker"),
    )


@pytest.mark.parametrize(("fields", "code", "output", "why"), CASES)
def test_the_stand_in_says_what_guardkit_says(
    fields: dict[str, Any], code: int | None, output: str, why: str | None
) -> None:
    assert declared_no_tests_ran(_commands(fields), code, output) == why


@pytest.mark.parametrize(("fields", "problem"), PROBLEM_CASES)
def test_the_stand_in_marker_problem_says_what_guardkit_says(
    fields: dict[str, Any], problem: str | None
) -> None:
    assert ran_marker_problem(_commands(fields)) == problem


def _part(**fields: Any) -> CheckPart:
    return CheckPart(
        label="api (in services/api)",
        cwd="services/api",
        test="go test ./...",
        name="api",
        rules=RULES,
        **fields,
    )


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "services" / "api").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _runner(code: int | None, output: str, calls: list) -> Any:
    def run(*, command: str, cwd: Any, timeout_seconds: int) -> tuple[int | None, Any]:
        calls.append(command)
        return code, RanOutput(f"`{command}` exited {code}", output=output)

    return run


def test_an_exit_zero_run_that_ran_no_tests_is_failed_in_guardkits_words(
    tmp_path: Path,
) -> None:
    check = run_full_check(
        [_part(absent_substrings=("[no test files]",))],
        tree=_tree(tmp_path),
        run=_runner(0, "ok  example.com/api [no test files]\n", []),
    )
    assert check.status == FAILED
    assert check.failed == ("api (in services/api)",)
    assert check.detail == (
        'no tests ran in part api (go test ./...): the output contains "[no test files]"'
    )


def test_a_real_pass_is_passed(tmp_path: Path) -> None:
    check = run_full_check(
        [_part(absent_substrings=("[no test files]",), ran_marker_regex=r"ok\s+\S+")],
        tree=_tree(tmp_path),
        run=_runner(0, "ok  example.com/api 0.012s\n", []),
    )
    assert check.status == PASSED


@pytest.mark.parametrize(("code", "status"), [(0, PASSED), (1, FAILED)])
def test_with_nothing_declared_the_exit_code_decides(
    tmp_path: Path, code: int, status: str
) -> None:
    check = run_full_check(
        [_part()], tree=_tree(tmp_path), run=_runner(code, "[no test files]", [])
    )
    assert check.status == status


def test_a_required_marker_with_no_regex_could_not_run_and_nothing_runs(
    tmp_path: Path,
) -> None:
    calls: list = []
    check = run_full_check(
        [_part(requires_ran_marker=True)],
        tree=_tree(tmp_path),
        run=_runner(0, "3 passed", calls),
    )
    assert check.status == COULD_NOT_RUN
    assert check.detail == (
        "the declaration of part api (in services/api) declares "
        "`requires_ran_marker: true` but no `ran_marker_regex`, so nothing in "
        "its output could show that a test ran"
    )
    assert calls == []


def test_a_part_without_guardkits_rules_could_not_run_and_nothing_runs(
    tmp_path: Path,
) -> None:
    calls: list = []
    part = CheckPart(label="root", cwd=".", test="make test", name="root")
    check = run_full_check([part], tree=tmp_path, run=_runner(0, "", calls))
    assert check.status == COULD_NOT_RUN
    assert "declared_no_tests_ran" in check.detail
    assert calls == []


def test_a_runner_that_kept_no_output_cannot_read_a_declared_part(
    tmp_path: Path,
) -> None:
    def plain(*, command: str, cwd: Any, timeout_seconds: int) -> tuple[int, str]:
        return 0, f"`{command}` exited 0"

    declared = run_full_check(
        [_part(absent_substrings=("[no test files]",))], tree=_tree(tmp_path), run=plain
    )
    assert declared.status == COULD_NOT_RUN
    assert "output was not kept" in declared.detail

    undeclared = run_full_check([_part()], tree=_tree(tmp_path), run=plain)
    assert undeclared.status == PASSED


@pytest.fixture
def a_guardkit_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = stand_in_for_guardkit(ModuleType(LOADER))
    monkeypatch.setitem(sys.modules, LOADER, module)
    return module


def test_each_part_carries_its_own_declared_fields_and_guardkits_rules(
    a_guardkit_module: ModuleType, tmp_path: Path
) -> None:
    declaration = SimpleNamespace(
        test="make test",
        absent_substrings=["no tests ran"],
        components={
            "api": SimpleNamespace(
                cwd="services/api", test="go test ./...",
                ran_marker_regex=r"ok\s", requires_ran_marker=True,
            )
        },
    )
    parts = full_check_parts_of(declaration, tmp_path, module_candidates=(LOADER,))
    assert parts is not None
    root, api = parts
    assert (root.name, root.absent_substrings, root.ran_marker_regex) == (
        "root", ("no tests ran",), None,
    )
    assert (api.name, api.absent_substrings, api.ran_marker_regex, api.requires_ran_marker) == (
        "api", None, r"ok\s", True,
    )
    assert root.rules is not None and api.rules is not None
    assert root.rules.no_tests_ran is a_guardkit_module.declared_no_tests_ran
    assert root.rules.marker_problem is a_guardkit_module.ran_marker_problem


@pytest.mark.parametrize("missing", ["declared_no_tests_ran", "ran_marker_problem"])
def test_without_guardkits_rules_the_parts_cannot_be_read(
    a_guardkit_module: ModuleType, tmp_path: Path, missing: str
) -> None:
    delattr(a_guardkit_module, missing)
    declaration = SimpleNamespace(test="make test")
    assert full_check_parts_of(declaration, tmp_path, module_candidates=(LOADER,)) is None
