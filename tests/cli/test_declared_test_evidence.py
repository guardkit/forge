"""The merge-ready checkpoint keeps the declared test's own output (F1).

What went wrong, on 2026-09-08 at 18:28Z, in build
``build-FEAT-39F6-20260908172338``: the checkpoint ran the repository's
declared test command inside its sandbox, the suite ended "2 failed, 817
passed, 2 deselected", and that last line was the only thing anybody kept.
It went on one log line and on the decision; the checkpoint's receipts held
the turn's rationale and nothing of the run. Which two tests failed had to be
found by running the whole suite again by hand.

So these tests pin, on the real code paths:

* the exit code is still the only verdict — nothing here reads output to
  decide anything;
* both forms of the runner (the one that runs a command here, and the one
  that asks a sandbox's sidecar to run it) keep a bounded piece of the
  output: the tool's own lines naming what failed, then the tail;
* a long output is cut with a plain sentence saying it was cut;
* a green run keeps its last lines, which is small;
* the failing tests reach the log line, the decision's detail, the failing
  gate's name — and so the reason the journey stops with, which
  ``conductor_driver._red_gate_reason`` builds and this lane did not touch;
* the checkpoint's receipts stage gets the output as a plain text file;
* a runner that keeps nothing (every existing injected test double) leaves
  every report exactly as it was.

Real code, real subprocesses for the host form, a real loopback HTTP server
for the sandbox form, a real SQLite ledger in a temporary directory.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import (
    DECLARED_TEST_EVIDENCE_LIMIT_BYTES,
    _run_declared_command,
    make_conductor_receipts_exporter,
    make_gates_green_reader,
    run_declared_command_in_sandbox,
    summarise_declared_test_output,
)
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.conductor_driver import _red_gate_reason
from forge.pipeline.merge_ready_checkpoint import (
    DECLARED_TEST_EVIDENCE_KEY,
    DECLARED_TEST_OUTPUT_FILENAME,
    GatesReport,
    GateStatus,
    MergeCardDecision,
    MergeCardOutcome,
    MergeReadyCheckpointPublisher,
)

BUILD_ID = "build-FEAT-39F6-20260908172338"
REPO = "org/api-test"
SEAT = "qwen3-coder-30b"

#: The two tests attempt fifteen's suite really failed on, and the shape
#: pytest really printed them in.
FIRST_CASE = (
    "tests/users/test_router.py::TestDeleteUserByEmail::test_by_email_delete_success"
)
SECOND_CASE = (
    "tests/users/test_router.py::TestDeleteUserByEmail::"
    "test_by_email_delete_selective"
)

RED_RUN_OUTPUT = f"""============================= test session starts ==============================
collected 821 items

tests/users/test_router.py ......FF                                      [ 12%]
=================================== FAILURES ===================================
____________ TestDeleteUserByEmail.test_by_email_delete_success ________________
    response = client.get("/users/by-email", params={{"email": email}})
>   assert response.status_code == 200
E   assert 404 == 200
=========================== short test summary info ============================
FAILED {FIRST_CASE} - assert 404 == 200
FAILED {SECOND_CASE} - assert 404 == 200
================== 2 failed, 817 passed, 2 deselected in 38.14s ================
"""

GREEN_RUN_OUTPUT = """============================= test session starts ==============================
collected 823 items

tests/users/test_router.py ................                              [100%]
======================= 823 passed, 1 deselected in 41.02s =====================
"""


def _printing_command(output: str, *, exit_code: int, at: Path) -> str:
    """A real command line that prints ``output`` and exits ``exit_code``.

    The output goes through a file rather than through the shell, so what the
    command prints is exactly the text this test wrote — newlines and all.
    """
    at.parent.mkdir(parents=True, exist_ok=True)
    at.write_text(output, encoding="utf-8")
    program = (
        "import sys; sys.stdout.write(open(sys.argv[1], encoding='utf-8').read()); "
        "sys.exit(int(sys.argv[2]))"
    )
    return (
        f"{json.dumps(sys.executable)} -c {json.dumps(program)} "
        f"{json.dumps(str(at))} {exit_code}"
    )


class _Declaration:
    """Stands in for guardkit's toolchain declaration (duck-typed)."""

    def __init__(self, test: str | None, test_timeout: int = 300) -> None:
        self.test = test
        self.test_timeout = test_timeout


def _config(repo_paths: dict[str, str]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "pipeline": {
                "build_queue_subject": "pipeline.build-queued.team-a",
                "approved_originators": ["terminal"],
            },
            "permissions": {"filesystem": {"allowlist": ["/work"]}},
            "conductor": {"enabled": True, "seat": SEAT},
            "planning": {"target_repo_paths": repo_paths},
        }
    )


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    tree = tmp_path / "worktree"
    (tree / ".guardkit" / "autobuild").mkdir(parents=True)
    (tree / ".guardkit" / "autobuild" / "review.json").write_text(
        "{}", encoding="utf-8"
    )
    return tree


@pytest.fixture
def pool(tmp_path: Path, worktree: Path) -> SqliteLifecyclePersistence:
    """A real ledger with one running fix-journey row."""
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "ledger.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, 'FEAT-39F6', ?, "
        "?, 'f.yaml', 'RUNNING', 'cli', 'corr-39f6', "
        "'2026-09-08T17:23:38Z', '2026-09-08T18:00:43Z', ?, 'mode-c', "
        "'TASK-39F6')",
        (BUILD_ID, REPO, f"fix/{BUILD_ID}", str(worktree)),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


# ---------------------------------------------------------------------------
# The form that runs the command here
# ---------------------------------------------------------------------------


class TestTheRunnerOnThisSideKeepsTheRun:
    def test_a_red_run_keeps_the_failing_names_and_the_tail(
        self, tmp_path: Path
    ) -> None:
        exit_code, detail = _run_declared_command(
            command=_printing_command(
                RED_RUN_OUTPUT, exit_code=1, at=tmp_path / "run.txt"
            ),
            cwd=tmp_path,
            timeout_seconds=60,
        )

        # The exit code is still the whole verdict.
        assert exit_code == 1
        # The sentence a person reads names the tests that failed.
        assert "2 failed:" in detail
        assert FIRST_CASE in detail
        assert SECOND_CASE in detail
        # And the evidence carries the tool's own lines and the tail.
        assert detail.failing_cases == (FIRST_CASE, SECOND_CASE)
        assert f"FAILED {FIRST_CASE}" in detail.evidence
        assert f"FAILED {SECOND_CASE}" in detail.evidence
        assert "assert 404 == 200" in detail.evidence
        assert "2 failed, 817 passed, 2 deselected" in detail.evidence

    def test_a_green_run_keeps_its_last_lines_and_names_nobody(
        self, tmp_path: Path
    ) -> None:
        exit_code, detail = _run_declared_command(
            command=_printing_command(
                GREEN_RUN_OUTPUT, exit_code=0, at=tmp_path / "run.txt"
            ),
            cwd=tmp_path,
            timeout_seconds=60,
        )

        assert exit_code == 0
        assert detail.failing_cases == ()
        assert "failed:" not in detail
        assert "823 passed, 1 deselected" in detail.evidence
        # A green run's evidence is small — its last lines, nothing more.
        assert len(detail.evidence.encode("utf-8")) < 2_000

    def test_a_very_long_output_is_cut_and_says_so(self, tmp_path: Path) -> None:
        # Forty very wide lines right before the summary, so the run's own
        # tail is far bigger than the sixteen kibibytes that may be kept.
        noise = "\n".join(
            f"line {n}: " + ("this run is extremely chatty. " * 40)
            for n in range(40)
        )
        exit_code, detail = _run_declared_command(
            command=_printing_command(
                f"{noise}\n{RED_RUN_OUTPUT}", exit_code=1, at=tmp_path / "run.txt"
            ),
            cwd=tmp_path,
            timeout_seconds=120,
        )

        assert exit_code == 1
        kept = len(detail.evidence.encode("utf-8"))
        assert kept <= DECLARED_TEST_EVIDENCE_LIMIT_BYTES
        assert "earlier output dropped" in detail.evidence
        # What matters most survives the cut.
        assert FIRST_CASE in detail.evidence
        assert "2 failed, 817 passed, 2 deselected" in detail.evidence

    def test_a_command_that_could_not_run_keeps_no_evidence_and_no_verdict(
        self, tmp_path: Path
    ) -> None:
        exit_code, detail = _run_declared_command(
            command=_printing_command(
                "nothing to see", exit_code=0, at=tmp_path / "run.txt"
            ),
            cwd=tmp_path / "not-a-directory",
            timeout_seconds=30,
        )

        assert exit_code is None  # could not run is not could not pass
        assert getattr(detail, "evidence", "") == ""


class TestTheBoundedSummaryItself:
    def test_a_tool_that_writes_no_summary_lines_keeps_its_last_lines(self) -> None:
        output = "\n".join(f"step {n} done" for n in range(200))

        evidence = summarise_declared_test_output(output, tail_lines=5)

        assert "the last 5 lines" in evidence
        assert "step 199 done" in evidence
        assert "step 100 done" not in evidence

    def test_a_run_that_printed_nothing_keeps_nothing(self) -> None:
        assert summarise_declared_test_output("") == ""
        assert summarise_declared_test_output("   \n\n") == ""

    def test_the_cut_marker_says_which_half_of_the_list_was_kept(self) -> None:
        """A run with hundreds of failures overruns the space kept for the
        tool's own list of names. That list keeps its FIRST names, so the
        sentence under it must say the LATER ones went — telling a reader
        the earlier ones went would send them looking at the wrong end."""
        failures = "\n".join(
            f"FAILED tests/module_{n}.py::test_case - assert 404 == 200"
            for n in range(300)
        )

        evidence = summarise_declared_test_output(
            f"{failures}\n300 failed, 4 passed\n"
        )

        assert "tests/module_0.py" in evidence  # the first names survived
        # A name from the middle of the list is gone altogether: too late for
        # the kept half, too early for the run's own last lines.
        assert "tests/module_200.py" not in evidence
        assert "later output dropped: only the first" in evidence
        assert "earlier output dropped" not in evidence
        assert len(evidence.encode("utf-8")) <= DECLARED_TEST_EVIDENCE_LIMIT_BYTES

    def test_the_cut_marker_names_the_size_that_really_was_kept(self) -> None:
        """The sentence says a byte count; that count is the size of the
        piece beside it, not the budget it was cut down to."""
        failures = "\n".join(
            f"FAILED tests/module_{n}.py::test_case - assert 404 == 200"
            for n in range(300)
        )

        evidence = summarise_declared_test_output(
            f"{failures}\n300 failed, 4 passed\n"
        )

        marker_line = next(
            line for line in evidence.split("\n") if "later output dropped" in line
        )
        said_kept = int(marker_line.split("only the first ")[1].split(" bytes")[0])
        named_half = evidence.split(marker_line)[0]
        really_kept = len(named_half.rstrip("\n").encode("utf-8"))

        assert said_kept == really_kept
        # And it is under the budget it was cut to, because the sentence
        # itself had to fit inside that budget as well.
        assert said_kept < DECLARED_TEST_EVIDENCE_LIMIT_BYTES // 2

    def test_other_tools_summary_words_are_kept_too(self) -> None:
        """Not a pytest-only reader: any line whose first word names a
        failing case is the tool's own summary of what failed."""
        evidence = summarise_declared_test_output(
            "FAIL  suite/login/can-sign-in (0.4s)\n"
            "not ok 7 - the cart totals up\n"
            "Ran 12 tests, 2 failures\n"
        )

        assert "FAIL  suite/login/can-sign-in" in evidence
        assert "not ok 7 - the cart totals up" in evidence


# ---------------------------------------------------------------------------
# The form that asks a sandbox's sidecar to run the command
# ---------------------------------------------------------------------------


class _SidecarFake(BaseHTTPRequestHandler):
    """A real HTTP server answering ``/run`` the way the sidecar does."""

    answer: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802 — http.server's own name
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        body = dict(type(self).answer)
        body.setdefault("cwd", request.get("cwd"))
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # keep the test output quiet
        return


@pytest.fixture
def sidecar_answering() -> Any:
    """A loopback sidecar whose ``/run`` reply the test sets per case."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SidecarFake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def _answering(**reply: Any) -> Any:
        _SidecarFake.answer = reply
        host, port = server.server_address[0], server.server_address[1]
        return SimpleNamespace(
            name="api-test-deploy", sidecar_url=f"http://{host}:{port}"
        )

    try:
        yield _answering
    finally:
        server.shutdown()
        server.server_close()


class TestTheRunnerInsideASandboxKeepsTheSameRun:
    def test_a_red_run_over_the_wire_keeps_the_same_evidence(
        self, sidecar_answering: Any, tmp_path: Path
    ) -> None:
        sandbox = sidecar_answering(
            exit_code=1, stdout=RED_RUN_OUTPUT, stderr_tail=""
        )

        exit_code, detail = run_declared_command_in_sandbox(
            command="qa/run-suite.sh",
            cwd=tmp_path,
            timeout_seconds=60,
            sandbox=sandbox,
            repo=REPO,
        )

        assert exit_code == 1
        assert "inside sandbox api-test-deploy" in detail
        assert detail.failing_cases == (FIRST_CASE, SECOND_CASE)
        assert "2 failed:" in detail
        assert f"FAILED {SECOND_CASE}" in detail.evidence
        assert "2 failed, 817 passed, 2 deselected" in detail.evidence

    def test_both_streams_are_read_not_just_one(
        self, sidecar_answering: Any, tmp_path: Path
    ) -> None:
        """The summary is on one stream and the crash on the other, and which
        is which differs by tool."""
        sandbox = sidecar_answering(
            exit_code=2,
            stdout=RED_RUN_OUTPUT,
            stderr_tail="Traceback (most recent call last):\nOSError: port 5433\n",
        )

        _exit_code, detail = run_declared_command_in_sandbox(
            command="qa/run-suite.sh",
            cwd=tmp_path,
            timeout_seconds=60,
            sandbox=sandbox,
            repo=REPO,
        )

        assert f"FAILED {FIRST_CASE}" in detail.evidence
        assert "OSError: port 5433" in detail.evidence

    def test_a_green_run_over_the_wire_keeps_its_last_lines(
        self, sidecar_answering: Any, tmp_path: Path
    ) -> None:
        sandbox = sidecar_answering(
            exit_code=0, stdout=GREEN_RUN_OUTPUT, stderr_tail=""
        )

        exit_code, detail = run_declared_command_in_sandbox(
            command="qa/run-suite.sh",
            cwd=tmp_path,
            timeout_seconds=60,
            sandbox=sandbox,
            repo=REPO,
        )

        assert exit_code == 0
        assert detail.failing_cases == ()
        assert "823 passed, 1 deselected" in detail.evidence

    def test_a_sidecar_that_could_not_run_it_keeps_no_evidence(
        self, tmp_path: Path
    ) -> None:
        dead = SimpleNamespace(name="gone", sidecar_url="http://127.0.0.1:9")

        exit_code, detail = run_declared_command_in_sandbox(
            command="qa/run-suite.sh",
            cwd=tmp_path,
            timeout_seconds=5,
            sandbox=dead,
            repo=REPO,
        )

        assert exit_code is None
        assert getattr(detail, "evidence", "") == ""


# ---------------------------------------------------------------------------
# The gate set, the decision, and the words a person reads at the end
# ---------------------------------------------------------------------------


def _reader(pool: Any, *, command: str, **kwargs: Any) -> Any:
    return make_gates_green_reader(
        pool=pool,
        config=_config({REPO: "/canonical/api-test"}),
        declaration_loader=lambda _root: _Declaration(command),
        **kwargs,
    )


class TestTheGateSetCarriesTheEvidence:
    def test_a_red_gate_set_names_the_failing_tests_everywhere_a_person_looks(
        self, pool: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        read = _reader(
            pool,
            command="qa/run-suite.sh",
            command_runner=lambda **kw: _run_declared_command(
                command=_printing_command(
                    RED_RUN_OUTPUT, exit_code=1, at=Path(kw["cwd"]) / "run.txt"
                ),
                cwd=kw["cwd"],
                timeout_seconds=kw["timeout_seconds"],
            ),
        )

        with caplog.at_level("WARNING"):
            report = read(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.RED
        assert FIRST_CASE in report.detail and SECOND_CASE in report.detail
        assert f"FAILED {FIRST_CASE}" in report.evidence
        # The failing gate's name carries the tests, so the reason the journey
        # stops with names them — built by the driver, which this lane did not
        # touch.
        assert report.failed_gates[0].startswith("declared toolchain test — 2 failed:")
        reason = _red_gate_reason(
            SimpleNamespace(dispatch_result=SimpleNamespace(gates=report))
        )
        assert FIRST_CASE in reason and SECOND_CASE in reason
        # And the log line a person reads first says it too.
        assert FIRST_CASE in caplog.text
        assert "what the declared test command printed" in caplog.text

    def test_a_green_gate_set_keeps_its_last_lines_too(self, pool: Any) -> None:
        read = _reader(
            pool,
            command="qa/run-suite.sh",
            command_runner=lambda **kw: _run_declared_command(
                command=_printing_command(
                    GREEN_RUN_OUTPUT, exit_code=0, at=Path(kw["cwd"]) / "run.txt"
                ),
                cwd=kw["cwd"],
                timeout_seconds=kw["timeout_seconds"],
            ),
            stamps_leg=lambda **kw: SimpleNamespace(
                status="not-enforced", detail="", blocks_card=False, attended=()
            ),
        )

        report = read(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.GREEN
        assert "823 passed, 1 deselected" in report.evidence

    def test_a_runner_that_keeps_nothing_leaves_the_report_as_it_was(
        self, pool: Any
    ) -> None:
        """Every existing injected runner returns a plain sentence. The report
        it produces must be exactly what it has always been."""
        read = _reader(
            pool,
            command="npm test",
            command_runner=lambda **kw: (1, "`npm test` exited 1"),
        )

        report = read(build_id=BUILD_ID, branch=f"fix/{BUILD_ID}")

        assert report.status is GateStatus.RED
        assert report.failed_gates == ("declared toolchain test",)
        assert report.detail == "`npm test` exited 1"
        assert report.evidence == ""


# ---------------------------------------------------------------------------
# The receipts of the checkpoint's own stage
# ---------------------------------------------------------------------------


def _checkpoint_turn(gates: GatesReport, *, outcome: MergeCardOutcome) -> Any:
    """A turn report shaped as the driver's, with the checkpoint's decision."""
    evidence = gates.evidence
    return SimpleNamespace(
        chosen_stage=SimpleNamespace(value="pull-request-review"),
        rationale="the merge-ready checkpoint ran the declared suite",
        dispatch_result=MergeCardDecision(
            outcome=outcome,
            build_id=BUILD_ID,
            feature_id="FEAT-39F6",
            gates=gates,
            details=(
                {DECLARED_TEST_EVIDENCE_KEY: evidence} if evidence else {}
            ),
        ),
    )


class TestTheCheckpointStageKeepsTheOutputAsAFile:
    def test_a_red_checkpoints_receipts_hold_the_run(
        self, pool: Any, tmp_path: Path
    ) -> None:
        export = make_conductor_receipts_exporter(
            pool=pool, receipts_root=tmp_path / "receipts"
        )

        key = export(
            build_id=BUILD_ID,
            report=_checkpoint_turn(
                GatesReport(
                    status=GateStatus.RED,
                    failed_gates=("declared toolchain test — 2 failed: …",),
                    detail="`qa/run-suite.sh` exited 1",
                    evidence=summarise_declared_test_output(RED_RUN_OUTPUT),
                ),
                outcome=MergeCardOutcome.RED_GATE_LOOP_BACK,
            ),
        )

        stage = tmp_path / "receipts" / BUILD_ID / "stages" / str(key)
        kept = (stage / DECLARED_TEST_OUTPUT_FILENAME).read_text(encoding="utf-8")
        assert FIRST_CASE in kept and SECOND_CASE in kept
        assert "2 failed, 817 passed, 2 deselected" in kept
        # The rationale is still there beside it.
        assert (stage / "turn-rationale.txt").is_file()

    def test_a_green_checkpoints_receipts_hold_its_last_lines_too(
        self, pool: Any, tmp_path: Path
    ) -> None:
        export = make_conductor_receipts_exporter(
            pool=pool, receipts_root=tmp_path / "receipts"
        )

        key = export(
            build_id=BUILD_ID,
            report=_checkpoint_turn(
                GatesReport(
                    status=GateStatus.GREEN,
                    detail="`qa/run-suite.sh` exited 0",
                    evidence=summarise_declared_test_output(GREEN_RUN_OUTPUT),
                ),
                outcome=MergeCardOutcome.CARD_PUBLISHED,
            ),
        )

        stage = tmp_path / "receipts" / BUILD_ID / "stages" / str(key)
        kept = (stage / DECLARED_TEST_OUTPUT_FILENAME).read_text(encoding="utf-8")
        assert "823 passed, 1 deselected" in kept

    def test_a_turn_with_no_test_run_writes_no_such_file(
        self, pool: Any, tmp_path: Path
    ) -> None:
        """Every work leg and every review leg: nothing to keep, no file."""
        export = make_conductor_receipts_exporter(
            pool=pool, receipts_root=tmp_path / "receipts"
        )

        key = export(
            build_id=BUILD_ID,
            report=SimpleNamespace(
                chosen_stage=SimpleNamespace(value="task-work"),
                rationale="the fix task ran",
                dispatch_result=SimpleNamespace(exit_code=0),
            ),
        )

        stage = tmp_path / "receipts" / BUILD_ID / "stages" / str(key)
        assert (stage / "turn-rationale.txt").is_file()
        assert not (stage / DECLARED_TEST_OUTPUT_FILENAME).exists()


# ---------------------------------------------------------------------------
# The decision the checkpoint hands back
# ---------------------------------------------------------------------------


class TestTheDecisionCarriesTheEvidence:
    def test_a_red_decision_carries_what_the_suite_printed(self) -> None:
        publisher = MergeReadyCheckpointPublisher(
            gates_green_reader=lambda **_kw: GatesReport(
                status=GateStatus.RED,
                failed_gates=("declared toolchain test — 2 failed: …",),
                detail="`qa/run-suite.sh` exited 1",
                evidence=summarise_declared_test_output(RED_RUN_OUTPUT),
            )
        )

        decision = asyncio.run(
            publisher.submit_decision(
                build_id=BUILD_ID,
                feature_id="FEAT-39F6",
                auto_approve=False,
                rationale="mode-c-commits-present",
            )
        )

        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert FIRST_CASE in decision.details[DECLARED_TEST_EVIDENCE_KEY]

    def test_a_gate_set_with_nothing_to_keep_files_nothing(self) -> None:
        """Every gate set built before this lane, and every one that ran no
        command: the decision is exactly what it always was."""
        publisher = MergeReadyCheckpointPublisher(
            gates_green_reader=lambda **_kw: GatesReport(
                status=GateStatus.RED, failed_gates=("pytest",), detail="2 failed"
            )
        )

        decision = asyncio.run(
            publisher.submit_decision(
                build_id=BUILD_ID,
                feature_id="FEAT-39F6",
                auto_approve=False,
                rationale="mode-c-commits-present",
            )
        )

        assert dict(decision.details) == {}
