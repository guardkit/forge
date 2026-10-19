"""The nightly check's fetch of a private project (design, 19 October 2026).

The coordinator cannot reach the Git door (it answers only the project's own
sandbox), so a private project is fetched through its sandbox:

1. the sandbox helper's ``/git/remote-start-point`` updates the sandbox
   clone's copy of GitHub and answers with the default branch and commit;
2. ``refs/remotes/origin/<branch>`` is fetched from the sandbox's read-only
   git export (the coordinator's ``git_export`` setting);
3. the commit must be the one the sandbox reported.

The lab: a bare "GitHub" repository, a sandbox clone of it served by a real
``git daemon`` on 127.0.0.1, whose own ``main`` carries a commit GitHub does
not have, and a fake helper that does what the real one does to the clone
(``git fetch`` of GitHub's main into ``refs/remotes/origin/main``). A smart
HTTP server stands at the door address and records every request, so a test
can say nothing reached it. Nothing reaches the internet.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pytest

from forge import network_git
from forge.config.models import ForgeConfig, PlanningConfig, SandboxEntry
from forge.security_sweep.repository import (
    FETCH_REASONS,
    REASON_COPY_MISMATCH,
    REASON_COPY_UNREADABLE,
    REASON_GITHUB,
    REASON_NO_EXPORT,
    REASON_SANDBOX_SILENT,
    SOURCE_GITHUB,
    SOURCE_SANDBOX_COPY,
    Mirror,
    fetch_through_sandbox,
)
from forge.security_sweep.sweep import run_sweep
from forge.security_sweep.state import HISTORY_FILE

from tests.forge.git.git_marker_lab import GitDaemon, SmartHttp, free_port

from .conftest import Lab, git

KEY = "example/notes-private"
LEAF = "notes-private"
LIVE_SHAPE = "git://169.254.207.2:8928/notes-private-fixture"


def _git(cwd: Path, *args: str) -> str:
    return git(cwd, *args)


@dataclass
class SandboxLab:
    """GitHub, the sandbox's clone (served by git daemon), the door, the helper."""

    github: Path
    github_work: Path
    clone: Path
    daemon: GitDaemon
    door: SmartHttp
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    #: What the fake helper does: "fetch" (as the real one), "lie" (reports
    #: GitHub's commit without fetching), "refuse", or "down".
    helper: str = "fetch"

    @property
    def export(self) -> str:
        return self.daemon.url(LEAF)

    @property
    def door_base(self) -> str:
        return self.door.url("")

    @property
    def door_address(self) -> str:
        return f"{self.door_base}{KEY}.git"

    def github_main(self) -> str:
        return _git(self.github, "rev-parse", "refs/heads/main")

    def clone_ref(self, ref: str) -> str:
        return _git(self.clone, "rev-parse", ref)

    def push_to_github(self, name: str) -> str:
        (self.github_work / name).write_text(name + "\n")
        _git(self.github_work, "add", "-A")
        _git(self.github_work, "commit", "-qm", name)
        _git(self.github_work, "push", "-q", str(self.github), "HEAD:refs/heads/main")
        return self.github_main()

    def post(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        self.posts.append((url, dict(body)))
        if self.helper == "down":
            raise ConnectionRefusedError("helper down")
        if self.helper == "refuse":
            return 400, {"error": "the Git door did not answer"}
        if self.helper == "head":
            return 200, {"branch": "HEAD", "commit": self.clone_ref("refs/remotes/origin/main"), "refusal": None}
        if self.helper == "fetch":
            _git(self.clone, "fetch", "-q", str(self.github), "+refs/heads/main:refs/remotes/origin/main")
            return 200, {"branch": "main", "commit": self.clone_ref("refs/remotes/origin/main"), "refusal": None}
        return 200, {"branch": "main", "commit": self.github_main(), "refusal": None}


@pytest.fixture
def sandbox_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SandboxLab]:
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    github_work = tmp_path / "github-work"
    github_work.mkdir()
    _git(github_work, "init", "-q", "-b", "main")
    (github_work / "README.md").write_text("on github\n")
    _git(github_work, "add", "-A")
    _git(github_work, "commit", "-qm", "first")
    github = tmp_path / "github.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(github_work), str(github)], check=True,
                   capture_output=True)
    served = tmp_path / "served"
    served.mkdir()
    clone = served / LEAF
    subprocess.run(["git", "clone", "-q", str(github), str(clone)], check=True, capture_output=True)
    # The sandbox's own main moves on: a commit GitHub never saw.
    (clone / "local-only.txt").write_text("not on github\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "sandbox only")
    door_root = tmp_path / "door"
    door_root.mkdir()
    daemon = GitDaemon(served)
    door = SmartHttp(door_root)
    lab = SandboxLab(github=github, github_work=github_work, clone=clone, daemon=daemon, door=door)
    try:
        yield lab
    finally:
        daemon.close()
        close = getattr(door, "close", None)
        if close:
            close()


def _through(lab: SandboxLab, folder: Path, **overrides: Any) -> Mirror:
    values: dict[str, Any] = dict(
        sidecar_url="http://192.0.2.10:8935",
        git_door=lab.door_base,
        git_export=lab.export,
        post=lab.post,
    )
    values.update(overrides)
    return fetch_through_sandbox(KEY, lab.door_address, folder, **values)


# ---------------------------------------------------------------------------
# The export address


def test_the_live_export_shape_is_taken() -> None:
    assert network_git.git_export_problem(LIVE_SHAPE) is None
    assert network_git.git_export_problem("git://192.0.2.10:8918/a/b-c/d.e_f") is None


@pytest.mark.parametrize("bad", [
    "", " git://192.0.2.10:8928/x", "git://u@192.0.2.10:8928/x", "git://192.0.2.10:8928/x?y",
    "git://192.0.2.10:8928/x#y", "git://192.0.2.10/x", "https://192.0.2.10:8928/x",
    "http://192.0.2.10:8928/x", "git://192.0.2.10:8928/../x", "git://192.0.2.10:8928/x/..",
    "git://192.0.2.10:8928/", "git://192.0.2.10:8928", "git://0.0.0.0:8928/x",
    "git://192.0.2.10:8928/.hidden", "git://192.0.2.10:8928/x//y", "git://192.0.2.10:99999/x",
])
def test_anything_but_a_plain_git_export_is_refused(bad: str) -> None:
    problem = network_git.git_export_problem(bad)
    assert problem
    if bad.strip():
        assert bad.strip() not in problem


def test_the_settings_take_a_git_export_and_check_its_shape() -> None:
    base = {"name": "sbx", "sidecar_url": "http://192.0.2.10:8935", "runner_url": "http://192.0.2.10:8934"}
    assert SandboxEntry(**base, git_export=LIVE_SHAPE).git_export == LIVE_SHAPE
    assert SandboxEntry(**base).git_export is None
    with pytest.raises(ValueError, match="git_export must be exactly"):
        SandboxEntry(**base, git_export="git://u@192.0.2.10:8928/x")


def test_the_shared_sandbox_never_has_a_git_export() -> None:
    with pytest.raises(ValueError, match="never has one"):
        PlanningConfig(
            target_repo_paths={"example/shared": "/w/s"},
            sandboxes={"example/shared": {
                "name": "api-test-deploy", "sidecar_url": "http://192.0.2.10:8925",
                "runner_url": "http://192.0.2.10:8924", "git_export": "git://192.0.2.10:8918/x",
            }},
        )


# ---------------------------------------------------------------------------
# The fetch from the export


def test_the_export_gives_the_copy_of_github_never_the_sandbox_s_own_main(
    sandbox_lab: SandboxLab, tmp_path: Path
) -> None:
    github = sandbox_lab.clone_ref("refs/remotes/origin/main")
    local = sandbox_lab.clone_ref("refs/heads/main")
    assert github != local
    point = network_git.fetch_sandbox_copy_branch(
        sandbox_lab.export, branch="main", expected_commit=github, copies_root=tmp_path / "m"
    )
    assert point.refusal is None and point.commit == github and point.branch == "main"


def test_a_copy_not_at_the_reported_commit_is_refused(sandbox_lab: SandboxLab, tmp_path: Path) -> None:
    local = sandbox_lab.clone_ref("refs/heads/main")
    point = network_git.fetch_sandbox_copy_branch(
        sandbox_lab.export, branch="main", expected_commit=local, copies_root=tmp_path / "m"
    )
    assert point.refusal == network_git.SANDBOX_COPY_MISMATCH


def test_an_export_that_does_not_answer_is_refused(tmp_path: Path) -> None:
    point = network_git.fetch_sandbox_copy_branch(
        f"git://127.0.0.1:{free_port()}/{LEAF}", branch="main", expected_commit="a" * 40,
        copies_root=tmp_path / "m", timeout=20,
    )
    assert point.refusal and point.refusal != network_git.SANDBOX_COPY_MISMATCH


@pytest.mark.parametrize("branch, commit", [("-x", "a" * 40), ("main", "abc"), ("main", "A" * 40)])
def test_a_branch_or_commit_that_will_not_do_is_refused_before_anything_runs(
    tmp_path: Path, branch: str, commit: str
) -> None:
    point = network_git.fetch_sandbox_copy_branch(
        LIVE_SHAPE, branch=branch, expected_commit=commit, copies_root=tmp_path / "m"
    )
    assert point.refusal
    assert not (tmp_path / "m").exists()


def test_a_head_branch_answer_is_refused_before_anything_runs(tmp_path: Path) -> None:
    """HEAD is git's pointer, never a branch (coach review, 19 October 2026)."""
    point = network_git.fetch_sandbox_copy_branch(
        LIVE_SHAPE, branch="HEAD", expected_commit="a" * 40, copies_root=tmp_path / "m"
    )
    assert point.refusal == network_git.SANDBOX_BRANCH_REFUSAL
    assert not (tmp_path / "m").exists()


# ---------------------------------------------------------------------------
# Through the sandbox: refresh, read, check


def test_a_sandbox_that_names_head_as_its_branch_is_said_and_nothing_is_fetched(
    sandbox_lab: SandboxLab, tmp_path: Path
) -> None:
    sandbox_lab.helper = "head"
    got = _through(sandbox_lab, tmp_path / "m")
    assert got.reason == REASON_SANDBOX_SILENT and got.path is None
    assert got.refusal == network_git.SANDBOX_BRANCH_REFUSAL
    assert not (tmp_path / "m").exists()


def test_the_sandbox_is_asked_to_refresh_first_so_the_newest_merge_is_checked(
    sandbox_lab: SandboxLab, tmp_path: Path
) -> None:
    stale = sandbox_lab.clone_ref("refs/remotes/origin/main")
    merged = sandbox_lab.push_to_github("merged-last-night")
    assert merged != stale
    got = _through(sandbox_lab, tmp_path / "m")
    assert got.refusal is None, got.refusal
    assert (got.commit, got.branch, got.source) == (merged, "main", SOURCE_SANDBOX_COPY)
    ((url, body),) = sandbox_lab.posts
    assert url == "http://192.0.2.10:8935/git/remote-start-point"
    assert body == {"repo": KEY, "fetch_address": sandbox_lab.door_address, "git_door": sandbox_lab.door_base}
    assert sandbox_lab.door.seen == [], "the coordinator never reaches the door"


def test_no_export_address_is_said_and_nothing_is_asked(sandbox_lab: SandboxLab, tmp_path: Path) -> None:
    got = _through(sandbox_lab, tmp_path / "m", git_export=None)
    assert got.reason == REASON_NO_EXPORT and got.path is None
    assert sandbox_lab.posts == []


@pytest.mark.parametrize("helper", ["refuse", "down"])
def test_a_sandbox_that_does_not_answer_is_said_and_the_export_is_not_read(
    sandbox_lab: SandboxLab, tmp_path: Path, helper: str
) -> None:
    sandbox_lab.helper = helper
    got = _through(sandbox_lab, tmp_path / "m")
    assert got.reason == REASON_SANDBOX_SILENT and got.path is None
    assert not (tmp_path / "m").exists(), "the export was not read"


def test_a_copy_that_does_not_match_what_the_sandbox_said_is_refused(
    sandbox_lab: SandboxLab, tmp_path: Path
) -> None:
    sandbox_lab.push_to_github("not-fetched-into-the-sandbox")
    sandbox_lab.helper = "lie"
    got = _through(sandbox_lab, tmp_path / "m")
    assert got.reason == REASON_COPY_MISMATCH and got.path is None


def test_an_export_that_cannot_be_read_is_said(sandbox_lab: SandboxLab, tmp_path: Path) -> None:
    got = _through(sandbox_lab, tmp_path / "m", git_export=f"git://127.0.0.1:{free_port()}/{LEAF}")
    assert got.reason == REASON_COPY_UNREADABLE and got.path is None


# ---------------------------------------------------------------------------
# The composed sweep


def _db(tmp_path: Path) -> Path:
    from forge.adapters.sqlite import connect as sqlite_connect
    from forge.lifecycle import migrations

    db = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db)
    migrations.apply_at_boot(cx)
    cx.close()
    return db


def _config(lab: SandboxLab, *, git_export: str | None, public: str | None = None) -> ForgeConfig:
    entry: dict[str, Any] = {
        "name": LEAF,
        "sidecar_url": "http://192.0.2.10:8935",
        "runner_url": "http://192.0.2.10:8934",
        "git_door": lab.door_base,
    }
    if git_export:
        entry["git_export"] = git_export
    addresses = {KEY: lab.door_address}
    if public:
        addresses["example/public"] = public
    return ForgeConfig(
        permissions={"filesystem": {"allowlist": []}},
        planning={
            "target_repo_paths": {KEY: "/w/notes"},
            "sandboxes": {KEY: entry},
            "fetch_addresses": addresses,
        },
        security_sweep={"enabled": True},
    )


async def _publish(text: str) -> None:
    return None


def test_the_composed_sweep_fetches_a_private_project_through_its_sandbox_never_the_door(
    sandbox_lab: SandboxLab, tmp_path: Path
) -> None:
    from forge.security_sweep.schedule import compose_sweep

    merged = sandbox_lab.push_to_github("merged")
    deps, _ = compose_sweep(
        _config(sandbox_lab, git_export=sandbox_lab.export), db_path=_db(tmp_path),
        publish=_publish, sandbox_post=sandbox_lab.post,
    )
    got = deps.fetch(deps.projects[KEY], tmp_path / "mirrors")
    assert got.refusal is None and got.commit == merged and got.source == SOURCE_SANDBOX_COPY
    assert [body["repo"] for _, body in sandbox_lab.posts] == [KEY]
    assert sandbox_lab.door.seen == [], "nothing reached the door address"


def test_the_composed_sweep_says_a_missing_export_and_makes_no_network_call(
    sandbox_lab: SandboxLab, tmp_path: Path
) -> None:
    from forge.security_sweep.schedule import compose_sweep

    deps, _ = compose_sweep(
        _config(sandbox_lab, git_export=None), db_path=_db(tmp_path),
        publish=_publish, sandbox_post=sandbox_lab.post,
    )
    got = deps.fetch(deps.projects[KEY], tmp_path / "mirrors")
    assert got.reason == REASON_NO_EXPORT
    assert sandbox_lab.posts == [] and sandbox_lab.door.seen == []


def test_the_composed_sweep_says_a_sandbox_that_refuses(sandbox_lab: SandboxLab, tmp_path: Path) -> None:
    from forge.security_sweep.schedule import compose_sweep

    sandbox_lab.helper = "refuse"
    deps, _ = compose_sweep(
        _config(sandbox_lab, git_export=sandbox_lab.export), db_path=_db(tmp_path),
        publish=_publish, sandbox_post=sandbox_lab.post,
    )
    got = deps.fetch(deps.projects[KEY], tmp_path / "mirrors")
    assert got.reason == REASON_SANDBOX_SILENT and sandbox_lab.door.seen == []


def test_a_public_project_is_fetched_from_github_as_before_with_no_sandbox_call(
    sandbox_lab: SandboxLab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.security_sweep import repository
    from forge.security_sweep.schedule import compose_sweep

    public = "https://github.com/example/public.git"
    asked: list[str] = []

    def fake_public(address: str, folder: Path, **_: Any) -> Mirror:
        asked.append(address)
        return Mirror(refusal="lab", reason=REASON_GITHUB)

    monkeypatch.setattr(repository, "fetch_mirror", fake_public)
    deps, _ = compose_sweep(
        _config(sandbox_lab, git_export=sandbox_lab.export, public=public),
        db_path=_db(tmp_path), publish=_publish, sandbox_post=sandbox_lab.post,
    )
    deps.fetch(public, tmp_path / "mirrors")
    assert asked == [public]
    assert sandbox_lab.posts == [] and sandbox_lab.door.seen == []


def test_the_real_https_fetch_still_refuses_a_door_address(tmp_path: Path) -> None:
    from forge.security_sweep.repository import fetch_mirror

    got = fetch_mirror("http://192.0.2.1:31831/example/notes-private.git", tmp_path)
    assert got.refusal and got.reason == REASON_GITHUB and got.path is None


# ---------------------------------------------------------------------------
# The line and the history


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", sorted(FETCH_REASONS))
async def test_the_line_names_each_fixed_reason(lab: Lab, reason: str) -> None:
    lab.add("org/app", {"a.txt": "a\n"})
    await run_sweep(lab.deps(fetch=lambda address, folder: Mirror(refusal="x", reason=reason)))
    (line,) = lab.slack.lines
    assert line == f"Nightly security check: app — could not be fetched ({reason}), so it was not checked."
    history = json.loads((lab.folder / HISTORY_FILE).read_text().splitlines()[-1])
    assert history["projects"]["org/app"]["not_fetched_reason"] == reason


@pytest.mark.asyncio
async def test_anything_but_a_fixed_reason_is_never_put_in_the_line(lab: Lab) -> None:
    lab.add("org/app", {"a.txt": "a\n"})
    said = "git://169.254.207.2:8928/secret-name could not be reached"
    await run_sweep(lab.deps(fetch=lambda address, folder: Mirror(refusal=said, reason=said)))
    (line,) = lab.slack.lines
    assert line == "Nightly security check: app — could not be fetched, so it was not checked."
    assert "169.254" not in (lab.folder / HISTORY_FILE).read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [SOURCE_GITHUB, SOURCE_SANDBOX_COPY])
async def test_the_history_line_says_what_was_scanned_and_where_from(lab: Lab, source: str) -> None:
    project = lab.add("org/app", {"a.txt": "a\n"})
    commit = git(project.work, "rev-parse", "HEAD")

    def fetch(address: str, folder: Path) -> Mirror:
        return Mirror(path=project.mirror, branch="main", commit=commit, source=source)

    await run_sweep(lab.deps(fetch=fetch))
    history = json.loads((lab.folder / HISTORY_FILE).read_text().splitlines()[-1])
    got = history["projects"]["org/app"]
    assert (got["branch"], got["commit"], got["source"]) == ("main", commit, source)
    assert "not_fetched_reason" not in got


@pytest.mark.asyncio
async def test_the_detail_goes_to_the_log_without_addresses_and_never_to_slack_or_history(
    lab: Lab, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    lab.add("org/app", {"a.txt": "a\n"})
    detail = (
        "The sandbox's copy of 'main' could not be fetched: the sandbox sidecar at "
        "http://169.254.207.2:8935 answered 500 for git://169.254.207.2:8928/notes-private-fixture"
    )
    caplog.set_level(logging.WARNING, logger="forge.security_sweep.sweep")
    await run_sweep(lab.deps(
        fetch=lambda address, folder: Mirror(refusal=detail, reason=REASON_COPY_UNREADABLE)
    ))
    (line,) = lab.slack.lines
    assert line == (
        f"Nightly security check: app — could not be fetched ({REASON_COPY_UNREADABLE}), "
        "so it was not checked."
    )
    history = (lab.folder / HISTORY_FILE).read_text()
    assert "answered 500" not in history and "169.254" not in history
    logged = [r.getMessage() for r in caplog.records if "could not be fetched" in r.getMessage()]
    assert logged == [
        f"security sweep: org/app could not be fetched ({REASON_COPY_UNREADABLE}): "
        "The sandbox's copy of 'main' could not be fetched: the sandbox sidecar at "
        "<address> answered 500 for <address>"
    ]
