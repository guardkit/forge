"""The merge word's checks, sent to the deploy sidecar instead of run in place.

Driven against a REAL sidecar handler on an ephemeral loopback port, with a
fake ``guardkit`` executable on PATH — never a mocked client. The tests prove
the door is one command wide, that the payload carries what the sidecar needs,
that the pre-merge baseline travels inline (the file the executor wrote lives
in the container and is not on the host), and that the result the executor
reads is the same shape it reads today.
"""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import pytest

from forge.adapters.guardkit.models import GuardKitResult
from forge.adapters.guardkit.run_via_sidecar import (
    MergeCallRefused,
    build_sidecar_guardkit_run,
)
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import GUARDKIT_PATH_ENV, build_server

REPO_KEY = "appmilla/api_test"
FEATURE = "FEAT-3ABD"
MAIN_SHA = "c" * 40

#: The argument list the merge executor builds today, unchanged.
def _executor_args(baseline_path: str | None = None) -> list[str]:
    args = [
        "merge",
        FEATURE,
        "--target",
        "main",
        "--expect-main-sha",
        MAIN_SHA,
        "--json",
    ]
    if baseline_path is not None:
        args += ["--baseline-json", baseline_path]
    return args


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    root.mkdir()
    return root


@pytest.fixture
def guardkit_on_the_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real executable that prints the merge report the merge verb prints."""
    binary = tmp_path / "bin" / "guardkit"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "baseline = None\n"
        "if '--baseline-json' in argv:\n"
        "    with open(argv[argv.index('--baseline-json') + 1]) as fh:\n"
        "        data = json.load(fh)\n"
        # The real command accepts a bare list or an object carrying a
        # failing_node_ids list, and stops with an error for anything else.
        "    if isinstance(data, list):\n"
        "        baseline = [str(x) for x in data]\n"
        "    elif isinstance(data.get('failing_node_ids'), list):\n"
        "        baseline = [str(x) for x in data['failing_node_ids']]\n"
        "    else:\n"
        "        sys.stderr.write('is an object without a failing_node_ids list')\n"
        "        sys.exit(1)\n"
        "print(json.dumps({\n"
        "    'outcome': 'merged', 'post_sha': 'd' * 40, 'verify_ok': True,\n"
        "    'verify_status': 'passed', 'charged_failures': [],\n"
        "    'checks_passed': 17, 'checks_total': 17,\n"
        "    'argv': argv, 'cwd': os.getcwd(), 'baseline_seen': baseline}))\n"
        "sys.exit(int(os.environ.get('FAKE_GUARDKIT_EXIT', '0')))\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(binary))
    return binary


@pytest.fixture
def sidecar(repo: Path):
    """A real sidecar handler listening on an ephemeral loopback port."""
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: str(repo)}},
        }
    )
    server = build_server(port=0, config_loader=lambda: config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _run(sidecar_url: str, repo: Path):
    return build_sidecar_guardkit_run(
        base_url=sidecar_url, repo_paths={REPO_KEY: str(repo)}
    )


# ---------------------------------------------------------------------------
# The door is one command wide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subcommand,args",
    [
        ("feature-spec", ["propose"]),
        ("autobuild", ["task"]),
        ("autobuild", []),
        ("graphiti", ["add-context"]),
    ],
)
async def test_only_the_merge_command_goes_through(
    sidecar: str, repo: Path, subcommand: str, args: list[str]
) -> None:
    run = _run(sidecar, repo)
    with pytest.raises(MergeCallRefused) as caught:
        await run(
            subcommand=subcommand,
            args=args,
            repo_path=repo,
            read_allowlist=[repo],
            timeout_seconds=900,
            with_nats_streaming=False,
        )
    assert "merge word's own command" in str(caught.value)


@pytest.mark.asyncio
async def test_a_target_other_than_main_is_refused(sidecar: str, repo: Path) -> None:
    run = _run(sidecar, repo)
    args = _executor_args()
    args[args.index("main")] = "release"
    with pytest.raises(MergeCallRefused) as caught:
        await run(
            subcommand="autobuild",
            args=args,
            repo_path=repo,
            read_allowlist=[repo],
            timeout_seconds=900,
            with_nats_streaming=False,
        )
    assert "'main'" in str(caught.value)


@pytest.mark.asyncio
async def test_a_missing_target_commit_is_refused(sidecar: str, repo: Path) -> None:
    run = _run(sidecar, repo)
    with pytest.raises(MergeCallRefused) as caught:
        await run(
            subcommand="autobuild",
            args=["merge", FEATURE, "--target", "main", "--json"],
            repo_path=repo,
            read_allowlist=[repo],
            timeout_seconds=900,
            with_nats_streaming=False,
        )
    assert "--expect-main-sha" in str(caught.value)


# ---------------------------------------------------------------------------
# The happy path, end to end through the real sidecar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_merge_runs_on_the_host_and_reports_back(
    sidecar: str, repo: Path, guardkit_on_the_host: Path
) -> None:
    run = _run(sidecar, repo)
    result = await run(
        subcommand="autobuild",
        args=_executor_args(),
        repo_path=repo,
        read_allowlist=[repo],
        timeout_seconds=900,
        with_nats_streaming=False,
    )
    assert isinstance(result, GuardKitResult)
    assert result.status == "success"
    assert result.exit_code == 0
    report = json.loads(result.stdout_tail)
    assert report["argv"][:3] == ["autobuild", "merge", FEATURE]
    assert report["cwd"] == str(repo.resolve())
    assert report["baseline_seen"] is None
    assert result.duration_secs >= 0


@pytest.mark.asyncio
async def test_a_red_merge_is_a_failed_result_with_the_report_intact(
    sidecar: str, repo: Path, guardkit_on_the_host: Path, monkeypatch
) -> None:
    """Exit 4 is "merged, but the checks did not pass" — the executor must
    still be able to read the report out of stdout."""
    monkeypatch.setenv("FAKE_GUARDKIT_EXIT", "4")
    run = _run(sidecar, repo)
    result = await run(
        subcommand="autobuild",
        args=_executor_args(),
        repo_path=repo,
        read_allowlist=[repo],
        timeout_seconds=900,
        with_nats_streaming=False,
    )
    assert result.status == "failed"
    assert result.exit_code == 4
    assert json.loads(result.stdout_tail)["outcome"] == "merged"


@pytest.mark.asyncio
async def test_the_baseline_travels_inline_not_as_a_path(
    sidecar: str, repo: Path, guardkit_on_the_host: Path, tmp_path: Path
) -> None:
    """The executor writes its baseline inside the forge container. The host
    has no such file, so the list itself is sent and the sidecar writes its
    own copy."""
    container_file = tmp_path / "container-only" / "merge-baseline.json"
    container_file.parent.mkdir()
    failing = ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    container_file.write_text(
        json.dumps({"failing_node_ids": failing}), encoding="utf-8"
    )

    run = _run(sidecar, repo)
    result = await run(
        subcommand="autobuild",
        args=_executor_args(str(container_file)),
        repo_path=repo,
        read_allowlist=[repo],
        timeout_seconds=900,
        with_nats_streaming=False,
    )
    report = json.loads(result.stdout_tail)
    assert result.status == "success"
    # The command on the host read the list, so the file the sidecar wrote is
    # in a shape the real merge command accepts.
    assert report["baseline_seen"] == failing
    # The host's own copy is the one the command was pointed at.
    on_host = Path(report["argv"][report["argv"].index("--baseline-json") + 1])
    assert on_host == repo / ".guardkit" / "tmp" / f"merge-baseline-{FEATURE}.json"
    assert on_host != container_file


@pytest.mark.asyncio
async def test_a_baseline_file_written_the_old_way_is_still_read(
    sidecar: str, repo: Path, guardkit_on_the_host: Path, tmp_path: Path
) -> None:
    """Earlier versions of forge wrote the list under the name "failing".

    A build whose baseline file was written before this change still has its
    list carried across, so an upgrade never silently drops a baseline.
    """
    container_file = tmp_path / "old-shape" / "merge-baseline.json"
    container_file.parent.mkdir()
    failing = ["tests/test_a.py::test_one"]
    container_file.write_text(json.dumps({"failing": failing}), encoding="utf-8")

    run = _run(sidecar, repo)
    result = await run(
        subcommand="autobuild",
        args=_executor_args(str(container_file)),
        repo_path=repo,
        read_allowlist=[repo],
        timeout_seconds=900,
        with_nats_streaming=False,
    )
    assert result.status == "success"
    assert json.loads(result.stdout_tail)["baseline_seen"] == failing


@pytest.mark.asyncio
async def test_a_baseline_file_that_is_not_there_still_merges(
    sidecar: str, repo: Path, guardkit_on_the_host: Path, tmp_path: Path
) -> None:
    run = _run(sidecar, repo)
    result = await run(
        subcommand="autobuild",
        args=_executor_args(str(tmp_path / "missing.json")),
        repo_path=repo,
        read_allowlist=[repo],
        timeout_seconds=900,
        with_nats_streaming=False,
    )
    assert result.status == "success"
    assert json.loads(result.stdout_tail)["baseline_seen"] is None


# ---------------------------------------------------------------------------
# When the far side says no, or is not there at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repository_the_sidecar_does_not_know_is_reported_plainly(
    sidecar: str, repo: Path, tmp_path: Path
) -> None:
    run = build_sidecar_guardkit_run(base_url=sidecar, repo_paths={REPO_KEY: str(repo)})
    stranger = tmp_path / "somewhere-else"
    stranger.mkdir()
    result = await run(
        subcommand="autobuild",
        args=_executor_args(),
        repo_path=stranger,
        read_allowlist=[stranger],
        timeout_seconds=900,
        with_nats_streaming=False,
    )
    assert result.status == "failed"
    assert "does not know the repository" in (result.stderr or "")
    assert REPO_KEY in (result.stderr or "")


@pytest.mark.asyncio
async def test_a_refusal_from_the_sidecar_carries_its_own_sentence(
    sidecar: str, repo: Path, guardkit_on_the_host: Path
) -> None:
    run = _run(sidecar, repo)
    args = _executor_args()
    args[args.index(MAIN_SHA)] = "short"
    result = await run(
        subcommand="autobuild",
        args=args,
        repo_path=repo,
        read_allowlist=[repo],
        timeout_seconds=900,
        with_nats_streaming=False,
    )
    assert result.status == "failed"
    assert "refused to run the merge" in (result.stderr or "")
    assert "forty" in (result.stderr or "")


@pytest.mark.asyncio
async def test_a_sidecar_that_is_not_listening_is_reported_plainly(
    repo: Path,
) -> None:
    # Claim a port, then let it go — nothing is listening there.
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    run = build_sidecar_guardkit_run(
        base_url=f"http://127.0.0.1:{dead_port}", repo_paths={REPO_KEY: str(repo)}
    )
    result = await run(
        subcommand="autobuild",
        args=_executor_args(),
        repo_path=repo,
        read_allowlist=[repo],
        timeout_seconds=5,
        with_nats_streaming=False,
    )
    assert result.status == "failed"
    assert "could not be reached" in (result.stderr or "")
    assert result.exit_code != 0
