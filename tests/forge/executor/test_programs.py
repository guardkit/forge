"""The one place the merge press's legs start a program (15 October 2026).

Six review rounds on the merge press's publication hold kept finding the same
fault in a different backend: an error that came AFTER a program had started
was answered as a refusal, so the press put its hold down while something it
had started might still be running. Every backend now starts programs through
:mod:`forge.executor.programs`, which draws the line in one place:

* the spawn's own error — :class:`RefusedBeforeStart`, nothing ran;
* stopped at its limit — :class:`TimedOut`;
* raised once it had started — :class:`RaisedWhileRunning`.
"""

from __future__ import annotations

import ast
import copy
import pickle
import subprocess
from pathlib import Path

import pytest

from forge.executor import programs
from forge.executor.programs import (
    ProgramRefused,
    RaisedWhileRunning,
    RefusedBeforeStart,
    TimedOut,
    run_program,
    start_program,
)

SRC = Path(__file__).resolve().parents[3] / "src" / "forge"

#: Every backend the press's legs start a program through.
BACKENDS = (
    "executor/shell_steps.py",
    "deploy_sidecar/service.py",
    "deploy_sidecar/deploy_executor.py",
    "deploy/live_gate.py",
    "adapters/guardkit/run.py",
)

#: Direct ways to start a program, other than through the helper.
DIRECT = {
    ("subprocess", "run"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
}


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_backend_starts_a_program_except_through_the_helper(backend: str) -> None:
    tree = ast.parse((SRC / backend).read_text(encoding="utf-8"))
    direct = [
        f"{backend}:{node.lineno} {node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and (node.func.value.id, node.func.attr) in DIRECT
    ]
    assert direct == []


def _script(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class TestTheThreeWaysAProgramDoesNotFinish:
    def test_a_program_that_runs_to_an_end(self, tmp_path: Path) -> None:
        ran = run_program(
            [str(_script(tmp_path, "ok.sh", "#!/bin/sh\necho hello\nexit 3\n"))],
            cwd=str(tmp_path),
            env=None,
            timeout=10,
        )
        assert ran.exit_code == 3 and ran.stdout == b"hello\n"
        assert not (ran.refused_before_start or ran.timed_out or ran.raised_while_running)

    @pytest.mark.parametrize(
        "make",
        [
            lambda d: d / "missing.sh",
            lambda d: _script(d, "no-format.sh", "this is not a program\n"),
            lambda d: _script(d, "bad-interpreter.sh", "#!/no/such/interpreter\n"),
        ],
        ids=["not-found", "ENOEXEC", "missing-interpreter"],
    )
    def test_a_spawn_refusal_is_refused_before_start(self, tmp_path: Path, make) -> None:
        ran = run_program([str(make(tmp_path))], cwd=str(tmp_path), env=None, timeout=10)
        assert ran.refused_before_start is True
        assert isinstance(ran.error, OSError)

    def test_a_working_folder_that_is_a_file_is_refused_before_start(
        self, tmp_path: Path
    ) -> None:
        not_a_folder = tmp_path / "a-file"
        not_a_folder.write_text("x", encoding="utf-8")
        ran = run_program(["/bin/true"], cwd=str(not_a_folder), env=None, timeout=10)
        assert ran.refused_before_start is True
        assert isinstance(ran.error, NotADirectoryError)

    def test_a_program_stopped_at_its_limit(self, tmp_path: Path) -> None:
        ran = run_program(
            [str(_script(tmp_path, "slow.sh", "#!/bin/sh\necho started\nsleep 30\n"))],
            cwd=str(tmp_path),
            env=None,
            timeout=1,
        )
        assert ran.timed_out is True
        assert b"started" in ran.stdout

    def test_an_error_after_the_start_is_raised_while_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken_read(self, *args, **kwargs):
            raise OSError("the pipe broke")

        monkeypatch.setattr(subprocess.Popen, "communicate", _broken_read)
        ran = run_program(
            [str(_script(tmp_path, "ok.sh", "#!/bin/sh\nsleep 30\n"))],
            cwd=str(tmp_path),
            env=None,
            timeout=10,
        )
        assert ran.raised_while_running is True
        assert not ran.refused_before_start


def test_start_program_raises_its_own_refusal(tmp_path: Path) -> None:
    with pytest.raises(ProgramRefused) as refused:
        start_program([str(tmp_path / "missing")])
    assert isinstance(refused.value, OSError)
    assert isinstance(refused.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize(
    "kind, flags",
    [
        (RefusedBeforeStart, {"refused_before_running": True, "answer_lost": False}),
        (TimedOut, {"timed_out": True, "answer_lost": True}),
        (RaisedWhileRunning, {"answer_lost": True, "refused_before_running": False}),
    ],
)
def test_the_results_unpack_and_survive_a_copy(kind, flags) -> None:
    two = kind(124, "out")
    three = kind((1, "out", "err"))
    assert tuple(two) == (124, "out") and tuple(three) == (1, "out", "err")
    for each in (copy.deepcopy(two), pickle.loads(pickle.dumps(three))):
        assert type(each) is kind
        for name, value in flags.items():
            assert getattr(each, name) is value


def test_the_helper_names_what_it_offers() -> None:
    assert set(programs.__all__) >= {
        "run_program",
        "start_program",
        "start_program_async",
        "RefusedBeforeStart",
        "TimedOut",
        "RaisedWhileRunning",
    }
