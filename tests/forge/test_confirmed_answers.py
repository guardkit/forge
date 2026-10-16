"""Only a complete answer can let the merge press put its hold down.

15 October 2026, after eight review rounds on the merge press's publication
hold, each finding one more place where an answer nobody could vouch for was
read as a complete one. The default is now the other way round: every result
that can affect the hold carries ``confirmed``, which is False unless it is
set True at the one place a complete, checked answer is built. The press puts
the hold down only when everything it received was confirmed.

These tests pin both halves: every such result is unconfirmed when built
without saying, and ``confirmed`` is set to a plain True only in the places
named below — each one a place where a complete answer is read.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "forge"


def _defaults() -> list[tuple[str, Any]]:
    from forge.adapters.git.models import GitOpResult
    from forge.adapters.guardkit.models import GuardKitResult
    from forge.cli._conductor_worktree import WorktreeCut
    from forge.deploy.candidate_tree import (
        CandidateTreeError,
        CandidateTreeLayout,
        WorkingFolder,
    )
    from forge.deploy.live_gate import LiveGateInvocation
    from forge.deploy.sidecar_runner import NoAnswer
    from forge.deploy.stage import DeployStageResult
    from forge.deploy_sidecar.deploy_executor import ExecutorAnswer
    from forge.executor.programs import RaisedWhileRunning, TimedOut
    from forge.pipeline.check_join import CheckJoinAnswer
    from forge.pipeline.merge_join import JoinOutcome

    return [
        ("GitOpResult", GitOpResult(status="success", operation="x", exit_code=0)),
        ("WorktreeCut", WorktreeCut(ok=True)),
        ("WorkingFolder", WorkingFolder(ok=True)),
        ("JoinOutcome", JoinOutcome(ok=True)),
        ("CandidateTreeLayout", CandidateTreeLayout(path="/x")),
        ("CandidateTreeError", CandidateTreeError("x")),
        ("ExecutorAnswer", ExecutorAnswer(accepted=True, word="w", sentence="s")),
        ("DeployStageResult", DeployStageResult(outcome="complete", deploy_run_id="r")),
        (
            "GuardKitResult",
            GuardKitResult(status="success", subcommand="x", duration_secs=0, exit_code=0),
        ),
        ("CheckJoinAnswer", CheckJoinAnswer(ran=True, passed=True, sentence="s")),
        ("LiveGateInvocation", LiveGateInvocation(verdict="pass", run_id="r")),
        ("NoAnswer", NoAnswer("s")),
        ("TimedOut", TimedOut(124, "")),
        ("RaisedWhileRunning", RaisedWhileRunning(1, "")),
    ]


@pytest.mark.parametrize("name, built", _defaults(), ids=[n for n, _ in _defaults()])
def test_every_result_is_unconfirmed_unless_it_says_so(name: str, built: Any) -> None:
    assert built.confirmed is False, name


def test_only_a_complete_programs_answer_is_confirmed_by_its_kind() -> None:
    from forge.executor.programs import Finished, RefusedBeforeStart

    assert Finished(0, "").confirmed is True
    assert RefusedBeforeStart(127, "").confirmed is True


#: The places a plain ``confirmed=True`` (or a ``"confirmed": True`` key) may
#: be written: each reads a complete answer — git's exit code, a reply that
#: passed its operation's own check, a refusal before anything ran, a
#: process group confirmed gone, a leg whose every answer was complete.
ALLOWED: dict[str, set[str]] = {
    "adapters/git/operations.py": {"prepare_worktree", "cleanup_worktree"},
    "adapters/guardkit/run.py": {"_binary_not_found_result", "_refused_cwd_result"},
    "adapters/guardkit/run_via_sidecar.py": {"run_merge_via_sidecar"},
    "cli/_conductor_worktree.py": {"cut_worktree_in_checkout", "remove_journey_worktree"},
    "deploy/candidate_tree.py": {
        "candidate_tree_path",
        "_git",
        "_extract_archive_into",
        "materialise_candidate_tree",
        "_ensure_excluded_sync",
        "retire_autobuild_worktree",
    },
    "deploy/live_gate.py": {"project_driver_answer", "invoke"},
    "deploy/sidecar_git.py": {
        "materialise_candidate_tree",
        "add_working_folder",
        "retire_autobuild_worktree",
    },
    "deploy/stage.py": {"stamped"},
    "deploy_sidecar/deploy_executor.py": {
        "run",
        "_settle_the_slot",
        "_nothing_is_alive_after_all",
        "_slot_with_no_note",
        "_confirm_with_the_coordinator",
        "_start",
    },
    "pipeline/check_join.py": {"ask_the_build_system_to_check_the_join"},
    "pipeline/merge_executor.py": {"execute_merge_deploy"},
    # The two kinds of program result that are complete by what they are.
    "executor/programs.py": {"Finished", "RefusedBeforeStart"},
}

#: Files whose ``confirmed`` means something else entirely.
UNRELATED = {"review_gate/assembler.py"}


def _plain_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def test_confirmed_is_set_true_only_where_a_complete_answer_is_read() -> None:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        if "confirmed" not in text or rel in UNRELATED:
            continue
        tree = ast.parse(text)
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def enclosing(node: ast.AST) -> str:
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return node.name
            return "<module>"

        for node in ast.walk(tree):
            places: list[ast.AST] = []
            if isinstance(node, ast.keyword) and node.arg == "confirmed" and _plain_true(node.value):
                places.append(node)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "confirmed" and _plain_true(value):
                        places.append(node)
            elif isinstance(node, ast.Assign) and _plain_true(node.value) and any(
                isinstance(t, ast.Name) and t.id == "confirmed" for t in node.targets
            ):
                places.append(node)
            for place in places:
                where = enclosing(place)
                if where not in ALLOWED.get(rel, set()):
                    found.append(f"{rel}:{getattr(place, 'lineno', '?')} in {where}")
    assert found == [], "confirmed=True set outside the validated-answer places"
