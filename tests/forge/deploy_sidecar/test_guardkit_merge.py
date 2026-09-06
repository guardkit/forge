"""The deploy sidecar's second operation: the merge word's checks, on the host.

Every refusal (unknown repository, a feature name of the wrong shape, a target
commit that is not a full hash, a timeout longer than the cap, a pre-merge
baseline that is not a list of test names), the happy path through an injected
runner that records what would have been run, and a real end-to-end HTTP round
trip on an ephemeral loopback port against a fake ``guardkit`` executable that
prints a merge report.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import (
    GUARDKIT_PATH_ENV,
    MERGE_NOT_STARTED_EXIT_CODE,
    MERGE_STDERR_TAIL_CHARS,
    MERGE_TIMEOUT_DEFAULT,
    MERGE_TIMEOUT_EXIT_CODE,
    MERGE_TIMEOUT_MAX,
    build_server,
    process_guardkit_merge_request,
    resolve_guardkit_command,
    run_merge_command,
)

REPO_KEY = "appmilla/api_test"
FEATURE = "FEAT-3ABD"
MAIN_SHA = "a1b2c3d4" * 5  # forty hex characters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(paths: dict[str, str]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": paths},
        }
    )


class _RecordingMergeRunner:
    """A stub merge runner that records argv/cwd/timeout and answers canned."""

    def __init__(self, result: tuple[int, str, str] = (0, "{}", "")) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[int, str, str]:
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    root.mkdir()
    # A deploy profile is deliberately NOT written: the merge operation runs one
    # fixed command, so it must not require a deployable repository.
    return root


# The baseline reader's shape contract, copied from guardkit's own source
# (guardkit/cli/autobuild.py, _load_baseline_failing): a bare list of test
# names, or an object carrying a failing_node_ids list, and an error for
# anything else. Every stand-in guardkit in these tests reads the file this
# way, so a file the real command would refuse fails here too.
REAL_BASELINE_READER = """
def read_baseline(path):
    import json
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        ids = data.get('failing_node_ids')
        if isinstance(ids, list):
            return [str(x) for x in ids]
        raise ValueError(
            str(path) + ' is an object without a failing_node_ids list')
    raise ValueError(
        str(path) + ' must be a JSON list of node ids or a baseline.json object')
"""


@pytest.fixture
def fake_guardkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real executable named guardkit that echoes its arguments as JSON.

    It reads any baseline file the way the real command reads it, so a file
    written in a shape guardkit refuses stops this stand-in too.
    """
    binary = tmp_path / "bin" / "guardkit"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        + REAL_BASELINE_READER
        + "argv = sys.argv[1:]\n"
        "baseline = None\n"
        "if '--baseline-json' in argv:\n"
        "    try:\n"
        "        baseline = read_baseline(argv[argv.index('--baseline-json') + 1])\n"
        "    except ValueError as exc:\n"
        "        sys.stderr.write('Unexpected error: ' + str(exc))\n"
        "        sys.exit(1)\n"
        "print(json.dumps({'outcome': 'merged', 'post_sha': 'b' * 40,\n"
        "                  'verify_ok': True, 'argv': argv,\n"
        "                  'baseline_seen': baseline}))\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(binary))
    return binary


def _payload(**overrides: Any) -> dict[str, Any]:
    body = {
        "repo": REPO_KEY,
        "feature_id": FEATURE,
        "expect_main_sha": MAIN_SHA,
    }
    body.update(overrides)
    return body


def _serve_in_thread(server: Any) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _post(url: str, body: dict[str, Any], *, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Refusals — nothing starts a process
# ---------------------------------------------------------------------------


def test_body_must_be_an_object(repo: Path) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        ["not", "an", "object"],
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "JSON object" in body["error"]
    assert runner.calls == []


def test_repo_is_required(repo: Path) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(repo=None),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "'repo' is required" in body["error"]
    assert runner.calls == []


def test_unknown_repo_names_the_known_ones(repo: Path) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(repo="acme/ghost"),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "unknown target repo" in body["error"]
    assert REPO_KEY in body["error"]
    assert runner.calls == []


@pytest.mark.parametrize(
    "bad_feature",
    ["", "feat-3abd", "FEAT-", "FEAT-ab", "3ABD", "FEAT-TOOLONGTOOLONG", None, 17],
)
def test_bad_feature_name_is_refused(repo: Path, bad_feature: Any) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(feature_id=bad_feature),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "'feature_id'" in body["error"]
    assert "FEAT-ABC1" in body["error"]
    assert runner.calls == []


@pytest.mark.parametrize(
    "bad_sha",
    ["", "a1b2c3d", "z" * 40, "a" * 39, "a" * 41, None, 12345],
)
def test_bad_target_commit_is_refused(repo: Path, bad_sha: Any) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(expect_main_sha=bad_sha),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "'expect_main_sha'" in body["error"]
    assert "forty" in body["error"]
    assert runner.calls == []


def test_timeout_over_the_cap_is_refused(repo: Path) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(timeout_seconds=MERGE_TIMEOUT_MAX + 1),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "may not be longer than" in body["error"]
    assert runner.calls == []


@pytest.mark.parametrize("bad_timeout", [0, -5, "600", True])
def test_timeout_must_be_a_positive_number(repo: Path, bad_timeout: Any) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(timeout_seconds=bad_timeout),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "positive number" in body["error"]
    assert runner.calls == []


def test_timeout_at_the_cap_is_allowed(repo: Path, fake_guardkit: Path) -> None:
    runner = _RecordingMergeRunner()
    status, _ = process_guardkit_merge_request(
        _payload(timeout_seconds=MERGE_TIMEOUT_MAX),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 200
    assert runner.calls[0]["timeout"] == MERGE_TIMEOUT_MAX


def test_baseline_must_be_a_list(repo: Path) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(baseline_failing={"failing": ["tests/test_a.py::test_b"]}),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "'baseline_failing' must be a list" in body["error"]
    assert runner.calls == []


def test_baseline_entries_must_be_text(repo: Path) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(baseline_failing=["tests/test_a.py::test_b", 7]),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "written as text" in body["error"]
    assert runner.calls == []


def test_no_guardkit_on_the_host_is_reported_plainly(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
        command_resolver=lambda: None,
    )
    assert status == 500
    assert "no guardkit command" in body["error"]
    assert runner.calls == []


# ---------------------------------------------------------------------------
# The happy path — exactly one fixed command, in the repository's own directory
# ---------------------------------------------------------------------------


def test_runs_the_one_fixed_command(repo: Path, fake_guardkit: Path) -> None:
    runner = _RecordingMergeRunner(result=(0, '{"outcome": "merged"}', ""))
    status, body = process_guardkit_merge_request(
        _payload(),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 200
    assert body == {
        "exit_code": 0,
        "stdout": '{"outcome": "merged"}',
        "stderr_tail": "",
    }
    call = runner.calls[0]
    assert call["argv"] == [
        str(fake_guardkit),
        "autobuild",
        "merge",
        FEATURE,
        "--target",
        "main",
        "--expect-main-sha",
        MAIN_SHA,
        "--json",
    ]
    assert call["cwd"] == str(repo)
    assert call["timeout"] == MERGE_TIMEOUT_DEFAULT


def test_a_non_zero_exit_code_is_data_not_an_error(
    repo: Path, fake_guardkit: Path
) -> None:
    """Exit 4 means "merged, but the checks did not pass" — an answer."""
    runner = _RecordingMergeRunner(result=(4, '{"outcome": "merged"}', "red"))
    status, body = process_guardkit_merge_request(
        _payload(), config=_config({REPO_KEY: str(repo)}), merge_runner=runner
    )
    assert status == 200
    assert body["exit_code"] == 4
    assert body["stderr_tail"] == "red"


def test_baseline_is_written_on_the_host_and_passed(
    repo: Path, fake_guardkit: Path
) -> None:
    failing = ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    runner = _RecordingMergeRunner()
    status, _ = process_guardkit_merge_request(
        _payload(baseline_failing=failing),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 200
    argv = runner.calls[0]["argv"]
    assert "--baseline-json" in argv
    written = Path(argv[argv.index("--baseline-json") + 1])
    assert written == repo / ".guardkit" / "tmp" / f"merge-baseline-{FEATURE}.json"
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "failing_node_ids": failing
    }


def test_an_empty_baseline_is_still_written(repo: Path, fake_guardkit: Path) -> None:
    """An empty list means "main was green" — it is not the same as no list."""
    runner = _RecordingMergeRunner()
    status, _ = process_guardkit_merge_request(
        _payload(baseline_failing=[]),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 200
    argv = runner.calls[0]["argv"]
    assert "--baseline-json" in argv
    written = Path(argv[argv.index("--baseline-json") + 1])
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "failing_node_ids": []
    }


def test_the_shape_guardkit_refuses_really_does_stop_the_merge(
    repo: Path, fake_guardkit: Path, tmp_path: Path
) -> None:
    """Proof the baseline assertions have teeth.

    This does NOT run the real merge command. It runs a stand-in that reads
    the baseline file by the rule copied from guardkit's own source
    (``guardkit/cli/autobuild.py``, ``_load_baseline_failing``): a bare JSON
    list of test names, or an object carrying a ``failing_node_ids`` list, and
    a ValueError for anything else. What is proven here is that shape contract
    and the sidecar's side of it — written the old way, an object with a
    ``failing`` list, the command exits non-zero and nothing merges; written
    the way the sidecar writes it, the same command is happy. It was also
    checked against the real binary by hand on 2026-09-06; if guardkit ever
    changes that reader, the copy in ``REAL_BASELINE_READER`` must change with
    it — this test cannot notice on its own.
    """
    wrong_shape = tmp_path / "old-shape-baseline.json"
    wrong_shape.write_text(
        json.dumps({"failing": ["tests/test_a.py::test_one"]}), encoding="utf-8"
    )
    exit_code, _stdout, stderr = run_merge_command(
        argv=[
            str(fake_guardkit),
            "autobuild",
            "merge",
            FEATURE,
            "--json",
            "--baseline-json",
            str(wrong_shape),
        ],
        cwd=str(repo),
    )
    assert exit_code == 1
    assert "without a failing_node_ids list" in stderr

    # Written the way the sidecar writes it, the same command is happy.
    right_shape = tmp_path / "baseline.json"
    right_shape.write_text(
        json.dumps({"failing_node_ids": ["tests/test_a.py::test_one"]}),
        encoding="utf-8",
    )
    exit_code, stdout, _stderr = run_merge_command(
        argv=[
            str(fake_guardkit),
            "autobuild",
            "merge",
            FEATURE,
            "--json",
            "--baseline-json",
            str(right_shape),
        ],
        cwd=str(repo),
    )
    assert exit_code == 0
    assert json.loads(stdout)["baseline_seen"] == ["tests/test_a.py::test_one"]


def test_no_baseline_means_no_baseline_flag(repo: Path, fake_guardkit: Path) -> None:
    runner = _RecordingMergeRunner()
    process_guardkit_merge_request(
        _payload(), config=_config({REPO_KEY: str(repo)}), merge_runner=runner
    )
    assert "--baseline-json" not in runner.calls[0]["argv"]


def test_a_baseline_that_cannot_be_written_stops_the_run(
    repo: Path, fake_guardkit: Path
) -> None:
    """Losing the baseline would blame the feature for tests already red."""
    blocker = repo / ".guardkit"
    blocker.write_text("not a directory", encoding="utf-8")
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(baseline_failing=["tests/test_a.py::test_one"]),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 500
    assert "already failing" in body["error"]
    assert runner.calls == []


def test_a_runner_that_blows_up_never_escapes(repo: Path, fake_guardkit: Path) -> None:
    def boom(**_: Any) -> tuple[int, str, str]:
        raise RuntimeError("the runner fell over")

    status, body = process_guardkit_merge_request(
        _payload(), config=_config({REPO_KEY: str(repo)}), merge_runner=boom
    )
    assert status == 500
    assert "the runner fell over" in body["error"]


def test_long_output_keeps_the_end(repo: Path, fake_guardkit: Path) -> None:
    stderr = "x" * (MERGE_STDERR_TAIL_CHARS + 500) + "THE-END"
    runner = _RecordingMergeRunner(result=(1, "{}", stderr))
    _, body = process_guardkit_merge_request(
        _payload(), config=_config({REPO_KEY: str(repo)}), merge_runner=runner
    )
    assert body["stderr_tail"].endswith("THE-END")
    assert len(body["stderr_tail"]) < len(stderr)


# ---------------------------------------------------------------------------
# Where the guardkit command comes from
# ---------------------------------------------------------------------------


def test_the_setting_names_the_command(
    fake_guardkit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert resolve_guardkit_command() == str(fake_guardkit)


def test_an_unusable_setting_falls_through_to_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = tmp_path / "pathbin" / "guardkit"
    on_path.parent.mkdir(parents=True)
    on_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    on_path.chmod(0o755)
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(tmp_path / "nowhere" / "guardkit"))
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert resolve_guardkit_command() == str(on_path)


def test_no_command_anywhere_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.delenv(GUARDKIT_PATH_ENV, raising=False)
    monkeypatch.setenv("PATH", str(empty))
    assert resolve_guardkit_command() is None


# ---------------------------------------------------------------------------
# The real subprocess core — no shell, and the whole process group is stopped
# ---------------------------------------------------------------------------


def test_run_merge_command_captures_both_streams(tmp_path: Path) -> None:
    script = tmp_path / "chatty.sh"
    script.write_text(
        "#!/bin/sh\necho on-stdout\necho on-stderr >&2\nexit 3\n", encoding="utf-8"
    )
    script.chmod(0o755)
    exit_code, stdout, stderr = run_merge_command(
        argv=[str(script)], cwd=str(tmp_path), timeout=20
    )
    assert exit_code == 3
    assert stdout.strip() == "on-stdout"
    assert stderr.strip() == "on-stderr"


def test_run_merge_command_does_not_use_a_shell(tmp_path: Path) -> None:
    """The argument list is fixed; a shell metacharacter is just a character."""
    marker = tmp_path / "should-not-exist"
    exit_code, _, stderr = run_merge_command(
        argv=["/bin/echo", f"hi; touch {marker}"], cwd=str(tmp_path), timeout=20
    )
    assert exit_code == 0
    assert not marker.exists()


def test_run_merge_command_reports_a_missing_command(tmp_path: Path) -> None:
    exit_code, stdout, stderr = run_merge_command(
        argv=[str(tmp_path / "nope")], cwd=str(tmp_path), timeout=20
    )
    assert exit_code == MERGE_NOT_STARTED_EXIT_CODE
    assert stdout == ""
    assert "could not be started" in stderr


def test_run_merge_command_kills_the_whole_process_group(tmp_path: Path) -> None:
    """A test run starts children; a timeout must stop those too, or the read
    that follows would wait for ever on a pipe they still hold open."""
    child_marker = tmp_path / "child.pid"
    script = tmp_path / "slow.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"(sleep 60 & echo $! > {child_marker}) \n"
        "sleep 60\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    started = time.monotonic()
    exit_code, _, stderr = run_merge_command(
        argv=[str(script)], cwd=str(tmp_path), timeout=1.0
    )
    elapsed = time.monotonic() - started
    assert exit_code == MERGE_TIMEOUT_EXIT_CODE
    assert "stopped after" in stderr
    # The read after the kill must END. Without the group kill the grandchild
    # keeps the pipe open and this would sit at the post-kill read wall.
    assert elapsed < 20
    # And the grandchild really is gone.
    for _ in range(50):
        if child_marker.exists() and child_marker.read_text().strip():
            break
        time.sleep(0.05)
    pid = int(child_marker.read_text().strip())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail(f"the grandchild process {pid} survived the timeout kill")


# ---------------------------------------------------------------------------
# End to end over loopback, against a real fake guardkit executable
# ---------------------------------------------------------------------------


def test_end_to_end_over_loopback(repo: Path, fake_guardkit: Path) -> None:
    server = build_server(
        port=0, config_loader=lambda: _config({REPO_KEY: str(repo)})
    )
    _serve_in_thread(server)
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        body = _post(
            f"http://{host}:{port}/guardkit-merge",
            _payload(baseline_failing=["tests/test_a.py::test_one"]),
        )
        assert body["exit_code"] == 0
        report = json.loads(body["stdout"])
        assert report["outcome"] == "merged"
        assert report["argv"][:3] == ["autobuild", "merge", FEATURE]
        assert report["argv"][3:7] == [
            "--target",
            "main",
            "--expect-main-sha",
            MAIN_SHA,
        ]
        assert "--json" in report["argv"]
        baseline = Path(report["argv"][report["argv"].index("--baseline-json") + 1])
        assert json.loads(baseline.read_text(encoding="utf-8")) == {
            "failing_node_ids": ["tests/test_a.py::test_one"]
        }
        # The command really read it: it did not merely receive a path.
        assert report["baseline_seen"] == ["tests/test_a.py::test_one"]
    finally:
        server.shutdown()
        server.server_close()


def test_end_to_end_refusal_is_http_400(repo: Path, fake_guardkit: Path) -> None:
    server = build_server(
        port=0, config_loader=lambda: _config({REPO_KEY: str(repo)})
    )
    _serve_in_thread(server)
    try:
        host, port = server.server_address[:2]
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(
                f"http://{host}:{port}/guardkit-merge",
                _payload(feature_id="not-a-feature"),
            )
        assert caught.value.code == 400
        assert "FEAT-ABC1" in caught.value.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()


def test_the_run_operation_still_works_beside_it(repo: Path) -> None:
    """The merge operation is additive: /run is untouched."""
    (repo / "deploy").mkdir()
    (repo / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(
            {"env_id": "staging", "compose": {"file": "c.yaml", "script": "deploy.sh"}}
        ),
        encoding="utf-8",
    )
    script = repo / "deploy.sh"
    script.write_text("#!/bin/sh\necho deployed\n", encoding="utf-8")
    script.chmod(0o755)
    server = build_server(
        port=0, config_loader=lambda: _config({REPO_KEY: str(repo)})
    )
    _serve_in_thread(server)
    try:
        host, port = server.server_address[:2]
        body = _post(
            f"http://{host}:{port}/run", {"repo": REPO_KEY, "script": "deploy.sh"}
        )
        assert body["exit_code"] == 0
        assert "deployed" in body["output_tail"]
    finally:
        server.shutdown()
        server.server_close()


def test_an_unknown_path_is_still_404(repo: Path) -> None:
    server = build_server(
        port=0, config_loader=lambda: _config({REPO_KEY: str(repo)})
    )
    _serve_in_thread(server)
    try:
        host, port = server.server_address[:2]
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(f"http://{host}:{port}/guardkit-merge-please", _payload())
        assert caught.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# The feature-name rule is the wire's rule
# ---------------------------------------------------------------------------


def test_the_feature_name_rule_is_the_one_the_wire_uses() -> None:
    """The sidecar writes the pattern out rather than importing it, because
    the wire's copy is a private name. Written out means it can drift, so it
    is pinned here: if the wire ever changes what a feature name looks like,
    this fails and the copy is corrected."""
    from nats_core.events._pipeline import (
        FEATURE_ID_PATTERN as WIRE_FEATURE_ID_PATTERN,
    )
    from forge.deploy_sidecar.service import FEATURE_ID_PATTERN

    assert FEATURE_ID_PATTERN.pattern == WIRE_FEATURE_ID_PATTERN.pattern
    assert FEATURE_ID_PATTERN.flags == WIRE_FEATURE_ID_PATTERN.flags


# ---------------------------------------------------------------------------
# The limit on one run of the checks
# ---------------------------------------------------------------------------


def test_the_checks_time_limit_is_passed_to_the_command(
    repo: Path, fake_guardkit: Path
) -> None:
    runner = _RecordingMergeRunner()
    status, _body = process_guardkit_merge_request(
        _payload(verify_timeout_seconds=300),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 200
    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--verify-timeout") + 1] == "300"


def test_no_checks_time_limit_means_no_flag(repo: Path, fake_guardkit: Path) -> None:
    """Without one the merge command keeps its own default — the sidecar does
    not invent a number of its own."""
    runner = _RecordingMergeRunner()
    process_guardkit_merge_request(
        _payload(), config=_config({REPO_KEY: str(repo)}), merge_runner=runner
    )
    assert "--verify-timeout" not in runner.calls[0]["argv"]


@pytest.mark.parametrize("bad", [0, -5, "600", [], True, 1.5])
def test_a_bad_checks_time_limit_is_refused(
    repo: Path, fake_guardkit: Path, bad: Any
) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(verify_timeout_seconds=bad),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "positive whole number" in body["error"]
    assert runner.calls == []  # nothing started


def test_a_checks_time_limit_over_the_cap_is_refused(
    repo: Path, fake_guardkit: Path
) -> None:
    runner = _RecordingMergeRunner()
    status, body = process_guardkit_merge_request(
        _payload(verify_timeout_seconds=MERGE_TIMEOUT_MAX + 1),
        config=_config({REPO_KEY: str(repo)}),
        merge_runner=runner,
    )
    assert status == 400
    assert "may not be longer than" in body["error"]
    assert runner.calls == []
