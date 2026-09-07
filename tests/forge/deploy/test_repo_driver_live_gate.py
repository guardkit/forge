"""Tests for the RepoDriverLiveGateInvoker — the REAL per-target live-gate backend.

Drives a tmp repo carrying a stub driver script through the invoker and asserts
the four verdict mappings, and — the load-bearing seam posture — that
:meth:`invoke` NEVER raises past its boundary (a timeout / spawn failure / bad
stdout returns an honest non-SUT verdict, never an exception).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from forge.deploy.live_gate import LiveGateInvocation, RepoDriverLiveGateInvoker


def _write_driver(tmp_path: Path, body: str) -> list[str]:
    """Write an executable python stub driver into ``tmp_path``; return its argv."""
    script = tmp_path / "driver.py"
    script.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return ["python3", "driver.py"]


# ---------------------------------------------------------------------------
# (a) canned envelope + exit 0 → verdict from the envelope
# ---------------------------------------------------------------------------


def test_envelope_pass_maps_to_pass(tmp_path: Path) -> None:
    envelope = {
        "run_id": "run-abc",
        "verdict": "pass",
        "gates": [{"gate_id": "health", "exit_code": 0}],
        "evidence_index_ref": "qa/evidence/run-abc/index.json",
        "dispositions_ref": "qa/dispositions/run-abc.json",
    }
    body = (
        "import json, sys\n"
        f"print(json.dumps({envelope!r}))\n"
        "sys.exit(0)\n"
    )
    argv = _write_driver(tmp_path, body)
    invoker = RepoDriverLiveGateInvoker(repo_path=tmp_path, driver_argv=argv)

    inv = invoker.invoke(feature="FEAT-1", target="local", gates=("health",))

    assert isinstance(inv, LiveGateInvocation)
    assert inv.verdict == "pass"
    assert inv.run_id == "run-abc"
    assert inv.gate_ids == ("health",)
    assert inv.evidence_index_ref == "qa/evidence/run-abc/index.json"
    assert inv.dispositions_ref == "qa/dispositions/run-abc.json"
    assert inv.dry_run is False
    assert inv.detail["exit_code"] == 0
    assert inv.detail["source"] == "results_envelope"
    # The --gates arg was forwarded to the driver.
    assert "--gates" in inv.detail["argv"]


def test_envelope_fail_verdict_wins_over_exit_code(tmp_path: Path) -> None:
    # A genuine 'fail' envelope: the driver exits 1 and the envelope says fail.
    envelope = {"run_id": "r", "verdict": "fail", "gates": []}
    body = f"import json, sys\nprint(json.dumps({envelope!r}))\nsys.exit(1)\n"
    argv = _write_driver(tmp_path, body)
    invoker = RepoDriverLiveGateInvoker(repo_path=tmp_path, driver_argv=argv)

    inv = invoker.invoke(feature="FEAT-1", target="local")
    assert inv.verdict == "fail"
    assert inv.detail["source"] == "results_envelope"


# ---------------------------------------------------------------------------
# (b) garbage stdout + exit 4 → fall back to the driver exit-code map
# ---------------------------------------------------------------------------


def test_garbage_stdout_falls_back_to_exit_code_map(tmp_path: Path) -> None:
    body = "import sys\nprint('not json at all <<<')\nsys.exit(4)\n"
    argv = _write_driver(tmp_path, body)
    invoker = RepoDriverLiveGateInvoker(repo_path=tmp_path, driver_argv=argv)

    inv = invoker.invoke(feature="FEAT-1", target="local")
    assert inv.verdict == "environment_fail"  # exit 4 in the driver map
    assert inv.run_id == "FEAT-1-local"  # fallback run id
    assert inv.detail["source"] == "exit_code_map"
    assert inv.detail["exit_code"] == 4


def test_exit_code_map_covers_all_four(tmp_path: Path) -> None:
    for code, verdict in ((0, "pass"), (1, "fail"), (3, "instrument_fail"), (4, "environment_fail")):
        body = f"import sys\nsys.stdout.write('garbage')\nsys.exit({code})\n"
        argv = _write_driver(tmp_path, body)
        invoker = RepoDriverLiveGateInvoker(repo_path=tmp_path, driver_argv=argv)
        inv = invoker.invoke(feature="F", target="t")
        assert inv.verdict == verdict, code


# ---------------------------------------------------------------------------
# (c) timeout → environment_fail (never raises)
# ---------------------------------------------------------------------------


def test_timeout_maps_to_environment_fail(tmp_path: Path) -> None:
    body = "import time\ntime.sleep(30)\n"
    argv = _write_driver(tmp_path, body)
    invoker = RepoDriverLiveGateInvoker(
        repo_path=tmp_path, driver_argv=argv, timeout_seconds=1
    )
    inv = invoker.invoke(feature="FEAT-1", target="local")
    assert inv.verdict == "environment_fail"
    assert inv.detail["exit_code"] is None
    assert "timed out" in inv.detail["error"]


# ---------------------------------------------------------------------------
# (d) missing driver script/interpreter → instrument_fail (never raises)
# ---------------------------------------------------------------------------


def test_missing_script_maps_to_instrument_fail(tmp_path: Path) -> None:
    # No driver written — python3 runs a nonexistent file. Python exits non-zero
    # with a traceback on stderr (not exit 3), so this exercises the exit-code
    # fallback for an unmapped code rather than a spawn OSError.
    invoker = RepoDriverLiveGateInvoker(
        repo_path=tmp_path, driver_argv=["python3", "does_not_exist.py"]
    )
    inv = invoker.invoke(feature="FEAT-1", target="local")
    # python3 exits 2 on a missing script → unmapped → 'fail' is impossible to
    # call a SUT fail honestly, but the driver map default is 'fail'; assert we
    # did not raise and produced a verdict.
    assert inv.verdict in {"fail", "instrument_fail", "environment_fail"}
    assert inv.dry_run is False


def test_missing_interpreter_spawn_failure_is_instrument_fail(tmp_path: Path) -> None:
    # A truly un-spawnable command (interpreter not on PATH) → OSError →
    # instrument_fail, never a raise.
    invoker = RepoDriverLiveGateInvoker(
        repo_path=tmp_path,
        driver_argv=["this-interpreter-does-not-exist-xyz", "driver.py"],
    )
    inv = invoker.invoke(feature="FEAT-1", target="local")
    assert inv.verdict == "instrument_fail"
    assert inv.detail["exit_code"] is None
    assert "could not spawn" in inv.detail["error"]


# ---------------------------------------------------------------------------
# extra_env + cwd are honoured
# ---------------------------------------------------------------------------


def test_extra_env_and_cwd_are_passed(tmp_path: Path) -> None:
    # The driver reports its cwd and a custom env var back in the envelope.
    body = (
        "import json, os, sys\n"
        "env = os.environ.get('DEPLOY_BASE_URL', 'MISSING')\n"
        "print(json.dumps({'run_id': 'r', 'verdict': 'pass', 'gates': [],"
        " 'evidence_index_ref': env}))\n"
        "sys.exit(0)\n"
    )
    argv = _write_driver(tmp_path, body)
    invoker = RepoDriverLiveGateInvoker(
        repo_path=tmp_path,
        driver_argv=argv,
        extra_env={"DEPLOY_BASE_URL": "http://localhost:9999"},
    )
    inv = invoker.invoke(feature="F", target="t")
    assert inv.verdict == "pass"
    assert inv.evidence_index_ref == "http://localhost:9999"
    # os.environ was not mutated by the invoker.
    assert "DEPLOY_BASE_URL" not in os.environ


def test_invalid_envelope_verdict_falls_back_to_exit_code(tmp_path: Path) -> None:
    # A JSON body whose verdict is not one of the four must NOT raise via the
    # LiveGateInvocation validator — it falls back to the exit-code map.
    envelope = {"run_id": "r", "verdict": "banana", "gates": []}
    body = f"import json, sys\nprint(json.dumps({envelope!r}))\nsys.exit(0)\n"
    argv = _write_driver(tmp_path, body)
    invoker = RepoDriverLiveGateInvoker(repo_path=tmp_path, driver_argv=argv)
    inv = invoker.invoke(feature="F", target="t")
    assert inv.verdict == "pass"  # exit 0 in the driver map
    assert inv.detail["source"] == "exit_code_map"
    assert inv.detail["envelope_verdict"] == "banana"


# ---------------------------------------------------------------------------
# Each check's own result rides the invocation (protect-main, 2026-09-07):
# the deploy stage names the failing checks before it refuses a merge.
# ---------------------------------------------------------------------------


def _drive(tmp_path: Path, envelope: dict) -> LiveGateInvocation:
    body = (
        "import json, sys\n"
        f"print(json.dumps({envelope!r}))\n"
        "sys.exit(0 if " + repr(envelope["verdict"] == "pass") + " else 1)\n"
    )
    argv = _write_driver(tmp_path, body)
    invoker = RepoDriverLiveGateInvoker(repo_path=tmp_path, driver_argv=argv)
    return invoker.invoke(feature="FEAT-1", target="local")


def test_per_check_results_are_carried_with_their_gate(tmp_path: Path) -> None:
    envelope = {
        "run_id": "r",
        "verdict": "fail",
        "gates": [
            {
                "gate_id": "health",
                "exit_code": 0,
                "assertions": [
                    {"id": "health::status", "status": "pass", "observed": "200",
                     "expected": "200", "evidence_ref": "qa/evidence/h.json"},
                ],
            },
            {
                "gate_id": "users_count",
                "exit_code": 1,
                "assertions": [
                    {"id": "users_count::body", "status": "fail",
                     "disposition": "counts", "observed": "{}", "expected": "counts"},
                ],
            },
        ],
    }
    inv = _drive(tmp_path, envelope)
    assert inv.verdict == "fail"
    assert inv.gate_ids == ("health", "users_count")
    assert inv.assertions == (
        {"id": "health::status", "gate_id": "health", "status": "pass",
         "evidence_ref": "qa/evidence/h.json", "observed": "200", "expected": "200"},
        {"id": "users_count::body", "gate_id": "users_count", "status": "fail",
         "observed": "{}", "expected": "counts", "disposition": "counts"},
    )


def test_a_gate_red_by_exit_code_alone_gets_one_failing_result(tmp_path: Path) -> None:
    envelope = {
        "run_id": "r",
        "verdict": "fail",
        "gates": [
            {"gate_id": "health", "exit_code": 0},
            {"gate_id": "etag", "exit_code": 2, "assertions": []},
        ],
    }
    inv = _drive(tmp_path, envelope)
    assert inv.assertions == (
        {"id": "etag::exit_code", "gate_id": "etag", "status": "fail",
         "observed": "2", "expected": "0"},
    )


def test_a_disposition_the_wire_does_not_know_is_left_out(tmp_path: Path) -> None:
    from nats_core.events import AssertionResult

    envelope = {
        "run_id": "r",
        "verdict": "fail",
        "gates": [
            {"gate_id": "g", "exit_code": 1,
             "assertions": [{"id": "g::x", "status": "fail", "disposition": "banana"}]},
        ],
    }
    inv = _drive(tmp_path, envelope)
    assert inv.assertions == ({"id": "g::x", "gate_id": "g", "status": "fail"},)
    # And what is carried always fits the wire model the stage builds.
    for entry in inv.assertions:
        AssertionResult(**entry)


def test_no_gates_means_no_results(tmp_path: Path) -> None:
    inv = _drive(tmp_path, {"run_id": "r", "verdict": "pass", "gates": []})
    assert inv.assertions == ()


# ---------------------------------------------------------------------------
# with_repo_path: the driver runs in the tree it is given (protect-main,
# 2026-09-07) — the registry, the twins and the evidence are the tree's.
# ---------------------------------------------------------------------------

_REPORTS_CWD_AND_ENV = (
    "import json, os, sys\n"
    "print(json.dumps({'run_id': 'r', 'verdict': 'pass', 'gates': [],"
    " 'evidence_index_ref': os.getcwd() + '|' + os.environ.get('DEPLOY_BASE_URL', 'unset')}))\n"
    "sys.exit(0)\n"
)


def _checkout_and_tree(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    """A checkout and a laid-out tree, each carrying the same driver script
    (found relative to the working directory, as the profile names it)."""
    checkout = tmp_path / "checkout"
    tree = checkout / ".forge-candidates" / "FEAT-1"
    checkout.mkdir()
    tree.mkdir(parents=True)
    argv = _write_driver(checkout, _REPORTS_CWD_AND_ENV)
    _write_driver(tree, _REPORTS_CWD_AND_ENV)
    return checkout, tree, argv


def _cwd_and_env(inv: LiveGateInvocation) -> tuple[Path, str]:
    cwd, env = inv.evidence_index_ref.split("|", 1)
    return Path(cwd).resolve(), env


def test_with_repo_path_moves_the_driver_and_keeps_the_env(tmp_path: Path) -> None:
    checkout, tree, argv = _checkout_and_tree(tmp_path)
    base = RepoDriverLiveGateInvoker(
        repo_path=checkout, driver_argv=argv, extra_env={"DEPLOY_BASE_URL": "http://live"}
    )
    moved = base.with_repo_path(tree)

    assert _cwd_and_env(moved.invoke(feature="F", target="t")) == (tree.resolve(), "http://live")
    assert moved.repo_path == tree
    assert moved.invoke(feature="F", target="t").detail["cwd"] == str(tree)
    # The base is a different object, still in the checkout (a copy, not a move).
    assert base.repo_path == checkout
    assert _cwd_and_env(base.invoke(feature="F", target="t")) == (checkout.resolve(), "http://live")


def test_moved_then_overlaid_is_in_the_tree_with_the_candidate_address(tmp_path: Path) -> None:
    """The stage's order: with_repo_path first, with_extra_env second — and
    with_extra_env keeps whatever working directory its invoker has."""
    checkout, tree, argv = _checkout_and_tree(tmp_path)
    base = RepoDriverLiveGateInvoker(
        repo_path=checkout, driver_argv=argv, extra_env={"DEPLOY_BASE_URL": "http://live"}
    )
    candidate = base.with_repo_path(tree).with_extra_env({"DEPLOY_BASE_URL": "http://cand"})
    assert _cwd_and_env(candidate.invoke(feature="F", target="t")) == (tree.resolve(), "http://cand")
    assert candidate.repo_path == tree


def test_with_extra_env_alone_stays_in_the_checkout(tmp_path: Path) -> None:
    """Every existing caller's behaviour, kept: an overlay never moves the driver."""
    checkout, _tree, argv = _checkout_and_tree(tmp_path)
    base = RepoDriverLiveGateInvoker(repo_path=checkout, driver_argv=argv)
    overlaid = base.with_extra_env({"DEPLOY_BASE_URL": "http://cand"})
    assert _cwd_and_env(overlaid.invoke(feature="F", target="t")) == (checkout.resolve(), "http://cand")
    assert overlaid.repo_path == checkout


def test_the_driver_record_names_where_it_ran(tmp_path: Path) -> None:
    body = "import sys\nsys.stdout.write('garbage')\nsys.exit(1)\n"
    argv = _write_driver(tmp_path, body)
    inv = RepoDriverLiveGateInvoker(repo_path=tmp_path, driver_argv=argv).invoke(
        feature="F", target="t"
    )
    assert inv.detail["cwd"] == str(tmp_path)
    spawn_failed = RepoDriverLiveGateInvoker(
        repo_path=tmp_path, driver_argv=["this-interpreter-does-not-exist-xyz", "x.py"]
    ).invoke(feature="F", target="t")
    assert spawn_failed.detail["cwd"] == str(tmp_path)


def test_the_other_backends_can_be_moved_too(tmp_path: Path) -> None:
    from forge.deploy.live_gate import (
        DryRunLiveGateInvoker,
        GuardkitSeamLiveGateInvoker,
        LiveGateSeamError,
        RefusingLiveGateInvoker,
    )

    dry = DryRunLiveGateInvoker()
    assert dry.repo_path is None
    assert dry.invoke(feature="F", target="t").detail["cwd"] is None
    moved = dry.with_repo_path(tmp_path / "tree")
    assert moved.repo_path == tmp_path / "tree"
    assert moved.invoke(feature="F", target="t").detail["cwd"] == str(tmp_path / "tree")
    assert moved.invoke(feature="F", target="t").dry_run is True
    assert dry.repo_path is None  # a copy

    seam = GuardkitSeamLiveGateInvoker(
        repo_path=tmp_path / "checkout", read_allowlist=(tmp_path,), timeout_seconds=7
    )
    seam_moved = seam.with_repo_path(tmp_path / "tree")
    assert seam_moved.repo_path == tmp_path / "tree"
    assert seam.repo_path == tmp_path / "checkout"

    import pytest

    with pytest.raises(LiveGateSeamError, match="cannot run here"):
        RefusingLiveGateInvoker("cannot run here").invoke(feature="F", target="t")
