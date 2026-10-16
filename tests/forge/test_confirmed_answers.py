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


#: EXACTLY the places a plain ``confirmed=True`` (or a ``"confirmed": True``
#: key, or a ``confirmed = True`` class attribute) is written, each with the
#: validation it relies on (the audit of 15 October 2026, Codex round 9).
ALLOWED: dict[str, dict[str, str]] = {
    "adapters/git/operations.py": {
        "prepare_worktree": "git worktree add ran to an exit code; or refused before git started",
        "cleanup_worktree": "git worktree remove ran to an exit code",
    },
    "adapters/guardkit/run.py": {
        "_binary_not_found_result": "no guardkit binary: refused before anything ran",
        "_refused_cwd_result": "working folder refused before anything ran",
    },
    "adapters/guardkit/run_via_sidecar.py": {
        "run_merge_via_sidecar": "200 reply with an integer exit code, timed_out read first",
    },
    "cli/_conductor_worktree.py": {
        "cut_worktree_in_checkout": "an existing tree reused for this exact build: nothing changed",
        "remove_journey_worktree": "the folder is already gone: nothing to do",
    },
    "deploy/candidate_tree.py": {
        "candidate_tree_path": "feature id refused before anything was written",
        "_git": "git ran to an exit code (or could not be started) for a read",
        "_extract_archive_into": "git archive exited, or was refused; extraction finished",
        "materialise_candidate_tree": "laid out here to the end; or no commit named",
        "_ensure_excluded_sync": "the exclude file could not be written: nothing else done",
        "retire_autobuild_worktree": "ran here to its end with no git command that raised",
    },
    "deploy/live_gate.py": {
        "project_driver_answer": "the driver ran to an exit code and its answer was read",
        "invoke": "a dry run; the driver's environment refused; or a refused spawn",
    },
    "deploy/sidecar_git.py": {
        "materialise_candidate_tree": "200 reply with a path, timed_out read first",
        "add_working_folder": "200 reply with status success or failed, timed_out read first",
        "retire_autobuild_worktree": "200 reply with a known status and no git_raised",
    },
    "deploy/stage.py": {
        "stamped": "every runner and gate answer in the leg's run was confirmed",
    },
    "deploy_sidecar/deploy_executor.py": {
        "run": "the request refused before anything was started",
        "_settle_the_slot": "the request refused before anything was started",
        "_nothing_is_alive_after_all": "the request refused before anything was started",
        "_slot_with_no_note": "the request refused before anything was started",
        "_confirm_with_the_coordinator": "the request refused before anything was started",
        "_start": "the request refused before the command was started",
    },
    "pipeline/check_join.py": {
        "ask_the_build_system_to_check_the_join": "the door refused (MergeCallRefused) before anything was sent",
    },
    "pipeline/merge_executor.py": {
        "execute_merge_deploy": "the retained worktree kept by the press's own decision: nothing touched",
    },
    "executor/programs.py": {
        "RefusedBeforeStart": "the spawn itself failed: nothing ran",
        "Finished": "the program ran to an end and said how",
    },
}

#: EXACTLY the places a result that is complete by its kind (``Finished``,
#: ``RefusedBeforeStart``) is built.
COMPLETE_BY_KIND: dict[str, dict[str, str]] = {
    "executor/shell_steps.py": {
        "_run_script_step": "ran to an exit code; or refused in words before running",
    },
    "deploy/sidecar_runner.py": {
        "__call__": "a 200 that passed every check, a 4xx refusal in words, or the executor's own refusal shape",
    },
    "deploy_sidecar/service.py": {
        "_start_and_collect": "the spawn itself failed: nothing ran",
    },
}

#: Files whose ``confirmed`` means something else entirely.
UNRELATED = {"review_gate/assembler.py", "review_gate/record.py"}


def _plain_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _places() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    confirmed: set[tuple[str, str]] = set()
    by_kind: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel in UNRELATED or ("confirmed" not in text and "Finished(" not in text and "RefusedBeforeStart(" not in text):
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
            if isinstance(node, ast.keyword) and node.arg == "confirmed" and _plain_true(node.value):
                confirmed.add((rel, enclosing(node)))
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "confirmed" and _plain_true(value):
                        confirmed.add((rel, enclosing(node)))
            elif isinstance(node, ast.Assign) and _plain_true(node.value) and any(
                isinstance(t, ast.Name) and t.id == "confirmed" for t in node.targets
            ):
                confirmed.add((rel, enclosing(node)))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("Finished", "RefusedBeforeStart")
            ):
                by_kind.add((rel, enclosing(node)))
    return confirmed, by_kind


def test_confirmed_is_set_true_exactly_where_a_complete_answer_is_read() -> None:
    confirmed, by_kind = _places()
    assert confirmed == {(f, fn) for f, fns in ALLOWED.items() for fn in fns}
    assert by_kind == {(f, fn) for f, fns in COMPLETE_BY_KIND.items() for fn in fns}
