"""The publisher brings the joined commit out through a read-only git export.

This is the route used for a project built in a sandbox: the sandbox's own
git daemon (port 9418 inside it) is published on the factory's gateway
address, and the publisher's ``source`` is ``git://<address>:<port>/<repo>``.
Here a real ``git daemon`` serves the project's copy on the loopback address;
nothing else is contacted. It also shows the export takes no push.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from forge.publisher.service import Publisher
from forge.publisher.settings import ProjectRoute
from tests.forge.publisher.a_project_and_a_ledger import (
    PROJECT,
    a_request,
    git,
    make_the_ledger,
    make_the_project,
    settings_for,
    what_the_remote_has,
)


def _a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def an_export(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "world"
    project = make_the_project(root)
    port = _a_free_port()
    daemon = subprocess.Popen(
        [
            "git", "daemon", "--reuseaddr", "--export-all",
            f"--base-path={root}", "--listen=127.0.0.1", f"--port={port}",
            str(project["copy"]),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        yield root, project, f"git://127.0.0.1:{port}/copy"
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)


def test_the_publisher_sends_what_it_brought_out_of_the_export(an_export) -> None:
    root, project, source = an_export
    make_the_ledger(root / "forge.db", project=project)
    settings = settings_for(
        root,
        project,
        ledger=root / "forge.db",
        projects={
            PROJECT: ProjectRoute(
                name=PROJECT, source=source, remote=str(project["bare"])
            )
        },
    )

    answer = Publisher(settings).publish(a_request(project))

    assert answer.published is True, answer.refusal
    assert what_the_remote_has(project["bare"]) == project["j"]


def test_the_export_takes_no_push(an_export) -> None:
    _root, project, source = an_export
    with pytest.raises(RuntimeError, match="access denied|not enabled"):
        git(project["copy"], "push", "--dry-run", source, "HEAD:refs/heads/a-probe")
