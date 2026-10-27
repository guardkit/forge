"""Admitting a feature whose spec and plan were written elsewhere.

4 October 2026 (project initialisation design, Part 6, points 2 and 3, and
the round 1-3 review dispositions R1, R3 and R7). Every "remote" here is a bare
repository in a temporary directory and the runner is the coordinator's own
``WorktreeGitRunner``, so a fetch and every committed-file read is real git.
Since 14 October 2026 the factory fetches only from the address recorded for
the project, so each test records its bare remote as the project's fetch
address. Nothing touches a live service, a sandbox or a model.

The project in these tests is synthetic: a few text files, no code, no
particular language.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.deploy.candidate_tree import read_file_at_commit
from forge.pipeline.prepared_admission import (
    admit_prepared_build,
)

from tests.forge.recorded_fetch_address import local_remotes, origin_of  # noqa: F401

FEATURE = "FEAT-AB12"
REPO = "synthetic/project"
BRANCH = "feature/prepared"
TASKS = ("TASK-AB12-001", "TASK-AB12-002")
SPEC_DIR = "features/count-things"
SPEC_NAME = "count-things"
TASK_DIR = "tasks/backlog/count-things"


# ---------------------------------------------------------------------------
# A remote on disk and a bundle on a branch of it
# ---------------------------------------------------------------------------


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_env(), capture_output=True, text=True
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


def config_text(
    *,
    memory: str | None = "synthetic_project",
    settings: tuple[str, ...] = (),
    documents: tuple[str, ...] = ("docs/constitution/mission.md",),
    tier1_enforced: bool | None = None,
) -> str:
    lines: list[str] = []
    if memory is not None:
        lines += ["memory:", f"  project: {memory}"]
    if settings:
        lines += ["launch:", f"  settings: [{', '.join(settings)}]"]
    if documents:
        lines += ["autobuild:", "  player:", "    required_documents:"]
        lines += [f"      - {doc}" for doc in documents]
    if tier1_enforced is not None:
        lines += ["qa:", "  tier1:", f"    enforce: {str(tier1_enforced).lower()}"]
    return "\n".join(lines) + "\n"


def bundle(
    *,
    feature_id: str = FEATURE,
    digest: bool = True,
    routing_off: bool = False,
    routes: bool = False,
    leak_sweep: bool = False,
    config: str | None = None,
) -> dict[str, str]:
    """A complete prepared bundle, as /feature-spec and /feature-plan write it."""
    task_files = {
        task: f"{TASK_DIR}/{task}-do-the-thing.md" for task in TASKS
    }
    plan = [
        f"id: {feature_id}",
        'name: "Count things"',
        "tasks:",
    ]
    for task, path in task_files.items():
        plan += [f"  - id: {task}", f'    file_path: "{path}"', "    dependencies: []"]
    plan += ["feature_files:", f'  - "{SPEC_DIR}/{SPEC_NAME}.feature"']
    if routing_off:
        plan += ["routing_law: off"]
    guide = [
        "# Implementation guide",
        "",
        "Read the [mission](../../../docs/constitution/mission.md) first, and the",
        "[summary](../../../features/count-things/count-things_summary.md).",
        "Write `src/new_module.txt` (a file still to be written, back-quoted).",
        "See [the web](https://example.invalid/x) and [below](#integration).",
        "",
        "## §4 Integration Contracts",
        "",
    ]
    guide += ["- route: /things/count", "  scope: api"] if routes else ["No routes."]
    guide += ["", "## Next steps", "", "route: /not-in-the-section"]
    files: dict[str, str] = {
        ".guardkit/config.yaml": config if config is not None else config_text(),
        f".guardkit/features/{feature_id}.yaml": "\n".join(plan) + "\n",
        f"{SPEC_DIR}/{SPEC_NAME}.feature": "Feature: count things\n",
        f"{SPEC_DIR}/{SPEC_NAME}_assumptions.yaml": "assumptions: []\n",
        f"{SPEC_DIR}/{SPEC_NAME}_summary.md": (
            "# Summary\n\nSee [the guide](../../tasks/backlog/count-things/"
            "IMPLEMENTATION-GUIDE.md).\n"
        ),
        f"{TASK_DIR}/IMPLEMENTATION-GUIDE.md": "\n".join(guide) + "\n",
        "docs/constitution/mission.md": "Status: accepted\n\nThe mission.\n",
        f"qa/pass-bar-seed-{SPEC_NAME}.yaml": "seed: true\n",
    }
    for task, path in task_files.items():
        files[path] = f"# {task}\n\nSee [the guide](IMPLEMENTATION-GUIDE.md).\n"
        files[f"qa/pass-bar-{task}.yaml"] = f"task: {task}\n"
    if digest:
        files[f"{SPEC_DIR}/{SPEC_NAME}_digest.yaml"] = "digest: []\n"
    if leak_sweep:
        files["qa/leak-sweep.yaml"] = "claims: []\n"
    return files


class Project:
    """A bare remote with ``main`` and a prepared branch, and a clone of it."""

    def __init__(self, root: Path) -> None:
        self.remote = root / "origin.git"
        self.writer = root / "writer"
        self.copy = root / "copy"
        self.writer.mkdir(parents=True)
        _git(self.writer, "init", "-q", "-b", "main")
        (self.writer / "README.md").write_text("one\n", encoding="utf-8")
        (self.writer / ".guardkit").mkdir()
        (self.writer / ".guardkit" / "config.yaml").write_text(
            config_text(), encoding="utf-8"
        )
        _git(self.writer, "add", ".")
        _git(self.writer, "commit", "-qm", "one")
        self.remote.mkdir()
        _git(self.remote, "init", "--bare", "-q", "-b", "main")
        _git(self.writer, "remote", "add", "origin", str(self.remote))
        _git(self.writer, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(root, "clone", "-q", str(self.remote), str(self.copy))
        # The address recorded for the project, as forge register-repo
        # records it: the bare remote its clone was made from.
        self.fetch_address = origin_of(self.copy)

    def runner(
        self, worktrees_root: Path, *, address: str | None = None
    ) -> WorktreeGitRunner:
        """The coordinator's own runner, with this project's clone and its
        recorded fetch address (``address`` records a different one)."""
        return WorktreeGitRunner(
            worktrees_root=worktrees_root,
            target_repo_paths={REPO: str(self.copy)},
            fetch_addresses={REPO: address or self.fetch_address},
        )

    def commit_on(
        self, branch: str, files: dict[str, str], *, remove: tuple[str, ...] = ()
    ) -> str:
        """Commit ``files`` (and remove ``remove``) on ``branch``; push; return sha."""
        existing = _git(self.writer, "branch", "--list", branch)
        if existing:
            _git(self.writer, "checkout", "-q", branch)
        else:
            _git(self.writer, "checkout", "-q", "-b", branch, "main")
        for rel, text in files.items():
            target = self.writer / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for rel in remove:
            _git(self.writer, "rm", "-q", rel)
        _git(self.writer, "add", "-A")
        _git(self.writer, "commit", "-qm", f"on {branch}")
        _git(self.writer, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
        sha = _git(self.writer, "rev-parse", "HEAD")
        _git(self.writer, "checkout", "-q", "main")
        return sha

    def main_commit(self) -> str:
        return _git(self.remote, "rev-parse", "refs/heads/main")


def recorded_runner(config: Any, worktrees_root: Path) -> WorktreeGitRunner:
    """The coordinator's own runner, given the clones and fetch addresses the
    config records (``planning.target_repo_paths`` and
    ``planning.fetch_addresses``), as the daemon composes it."""
    return WorktreeGitRunner(
        worktrees_root=worktrees_root,
        target_repo_paths=config.planning.target_repo_paths,
        fetch_addresses=config.planning.fetch_addresses,
    )


@pytest.fixture
def project(tmp_path: Path, local_remotes: Path) -> Project:
    return Project(tmp_path)


@pytest.fixture
def runner(tmp_path: Path, project: Project) -> WorktreeGitRunner:
    return project.runner(tmp_path / "wt")


async def _admit(project: Project, runner: WorktreeGitRunner, **kw: str):
    return await admit_prepared_build(
        runner,
        repo=REPO,
        repo_path=str(project.copy),
        feature_id=kw.get("feature_id", FEATURE),
        branch=kw.get("branch", BRANCH),
    )


# ---------------------------------------------------------------------------
# The admitted facts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_complete_bundle_is_admitted_with_one_revision_for_everything(
    project: Project, runner: WorktreeGitRunner
) -> None:
    source = project.commit_on(BRANCH, bundle())

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal
    admitted = answer.admitted
    assert admitted is not None
    # A different source and target branch, recorded correctly (R1): the
    # target is the remote's default branch; start and source are the
    # admitted commit itself, not main's commit or a merge base.
    assert admitted.target_branch == "main"
    assert admitted.source_commit == source
    assert admitted.start_commit == source
    assert source != project.main_commit()
    assert admitted.memory_project == "synthetic_project"
    assert admitted.launch_settings == ()


@pytest.mark.asyncio
async def test_a_feature_queued_on_the_default_branch_itself_is_admitted(
    project: Project, runner: WorktreeGitRunner
) -> None:
    source = project.commit_on("main", bundle())

    answer = await _admit(project, runner, branch="main")

    assert answer.ok, answer.refusal
    assert answer.admitted.target_branch == "main"
    assert answer.admitted.source_commit == source == project.main_commit()


@pytest.mark.asyncio
async def test_settings_and_deploy_profile_changed_on_the_branch_are_read_there(
    project: Project, runner: WorktreeGitRunner
) -> None:
    """A prepared branch that changes its launch-setting names and its deploy
    profile after branching from main has them read at the admitted commit,
    never at main's (R1)."""
    project.commit_on("main", {"deploy/profile.yaml": "port: 1000\n"})
    files = bundle(config=config_text(settings=("BRANCH_ONLY_SETTING",)))
    files["deploy/profile.yaml"] = "port: 2000\n"
    source = project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal
    assert answer.admitted.launch_settings == ("BRANCH_ONLY_SETTING",)
    # start_commit is where Forge reads the deploy profile and launch
    # settings, stamps declared_at and the sidecar enforces against; it is the
    # branch's commit, so the branch's profile is the one read.
    profile = await read_file_at_commit(
        project.copy, answer.admitted.start_commit, "deploy/profile.yaml"
    )
    assert profile.content == "port: 2000\n"
    assert answer.admitted.start_commit == source


@pytest.mark.asyncio
async def test_the_admitted_commit_does_not_follow_a_branch_that_moves_later(
    project: Project, runner: WorktreeGitRunner
) -> None:
    first = project.commit_on(BRANCH, bundle())
    answer = await _admit(project, runner)
    project.commit_on(BRANCH, {"later.md": "later\n"})

    assert answer.admitted.source_commit == first
    # The commit stays in the copy for the runner to build.
    assert _git(project.copy, "cat-file", "-t", first) == "commit"


# ---------------------------------------------------------------------------
# Refused exactly where the planning door refuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_memory_name_is_refused_in_the_doors_words(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(BRANCH, bundle(config=config_text(memory=None)))

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "memory" in (answer.refusal or "")
    assert "project:" in (answer.refusal or "")  # names the two lines to add


@pytest.mark.asyncio
async def test_a_reserved_setting_name_is_refused(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(BRANCH, bundle(config=config_text(settings=("FORGE_DB_PATH",))))

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "FORGE_DB_PATH" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_a_branch_the_remote_does_not_have_is_refused(
    project: Project, runner: WorktreeGitRunner
) -> None:
    answer = await _admit(project, runner, branch="feature/nowhere")

    assert not answer.ok
    assert "has no branch called 'feature/nowhere'" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_an_unreachable_remote_is_refused(
    project: Project, tmp_path: Path
) -> None:
    # The address recorded for the project leads nowhere; the clone's own
    # origin still reaches the remote, and is never asked.
    gone = project.runner(tmp_path / "wt", address=str(project.remote) + "-gone")

    answer = await _admit(project, gone)

    assert not answer.ok
    assert "could not be reached" in (answer.refusal or "")
    # The refusal is the factory's own sentence, never git's text.
    assert "fatal" not in (answer.refusal or "")


# ---------------------------------------------------------------------------
# The supplied bundle: each missing item refused by name (R3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [f"qa/pass-bar-{TASKS[0]}.yaml", f"qa/pass-bar-seed-{SPEC_NAME}.yaml"],
    ids=["pass bar", "QA seed"],
)
async def test_missing_qa_files_are_left_to_guardkits_own_precondition(
    project: Project, runner: WorktreeGitRunner, path: str
) -> None:
    """Step 2 (26 October 2026, census FRG-101): one rule in one place.
    GuardKit's task-start precondition requires a pinned pass bar where a
    project switches it on; admission no longer requires the QA files."""
    files = bundle(config=config_text(tier1_enforced=False))
    files.pop(path)
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal


@pytest.mark.asyncio
async def test_back_quoted_paths_urls_and_anchors_are_not_checked(
    project: Project, runner: WorktreeGitRunner
) -> None:
    files = bundle()
    task_path = f"{TASK_DIR}/{TASKS[0]}-do-the-thing.md"
    files[task_path] += (
        "\nCreate `src/not_yet_written.txt` and `[x](also/not/a/link.md)`.\n"
        "```\n[fenced](not/checked.md)\n```\n"
        "[site](https://example.invalid/y) [top](#top) [mail](mailto:a@b.invalid)\n"
    )
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal


# ---------------------------------------------------------------------------
# The one reading rule: admission refuses what planning and the Coach refuse
# ---------------------------------------------------------------------------


def _commit_bytes_on(project: Project, branch: str, files: dict[str, bytes]) -> None:
    _git(project.writer, "checkout", "-q", branch)
    for rel, data in files.items():
        (project.writer / rel).parent.mkdir(parents=True, exist_ok=True)
        (project.writer / rel).write_bytes(data)
    _git(project.writer, "add", "-A")
    _git(project.writer, "commit", "-qm", "bytes")
    _git(project.writer, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
    _git(project.writer, "checkout", "-q", "main")


# ---------------------------------------------------------------------------
# Digest, routing_law and the leak-sweep manifest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("digest", [True, False])
async def test_the_specialist_digest_is_optional(
    project: Project, runner: WorktreeGitRunner, digest: bool
) -> None:
    project.commit_on(BRANCH, bundle(digest=digest))

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal


@pytest.mark.asyncio
async def test_a_guide_declaring_a_route_with_the_manifest_is_admitted(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(BRANCH, bundle(routes=True, leak_sweep=True))

    assert (await _admit(project, runner)).ok


@pytest.mark.asyncio
async def test_a_guide_with_no_routes_and_no_manifest_is_admitted(
    project: Project, runner: WorktreeGitRunner
) -> None:
    # The fixture's guide has a route: line OUTSIDE the Integration Contracts
    # section, which does not count.
    project.commit_on(BRANCH, bundle(routes=False, leak_sweep=False))

    assert (await _admit(project, runner)).ok


def _colon_guide(files: dict[str, str]) -> dict[str, str]:
    guide = f"{TASK_DIR}/IMPLEMENTATION-GUIDE.md"
    files[guide] = files[guide].replace(
        "## §4 Integration Contracts", "## §4: Integration Contracts"
    )
    return files


@pytest.mark.asyncio
@pytest.mark.parametrize("digest", [True, False], ids=["with-digest", "without-digest"])
async def test_a_colon_heading_route_with_the_manifest_is_admitted(
    project: Project, runner: WorktreeGitRunner, digest: bool
) -> None:
    project.commit_on(
        BRANCH, _colon_guide(bundle(digest=digest, routes=True, leak_sweep=True))
    )

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal


# ---------------------------------------------------------------------------
# Every bundle file is the file itself (R2); YAML paths stay inside (R3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_markdown_reference_up_and_across_to_a_real_file_is_admitted(
    project: Project, runner: WorktreeGitRunner
) -> None:
    files = bundle()
    task = f"{TASK_DIR}/{TASKS[0]}-do-the-thing.md"
    files[task] += "\nSee [the API](../../../docs/design/API.md).\n"
    files["docs/design/API.md"] = "# API\n"
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal


_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x00binary"


def _guide_referencing_a_picture(files: dict[str, str]) -> dict[str, str]:
    guide = f"{TASK_DIR}/IMPLEMENTATION-GUIDE.md"
    files[guide] += "\n![the flow](../../../docs/flow.png)\n"
    return files


@pytest.mark.asyncio
async def test_a_committed_picture_referenced_from_the_guide_is_admitted(
    project: Project, runner: WorktreeGitRunner
) -> None:
    """A reference target need only be an ordinary committed file; its bytes
    are never decoded (R7)."""
    project.commit_on(BRANCH, _guide_referencing_a_picture(bundle()))
    _commit_bytes_on(project, BRANCH, {"docs/flow.png": _PNG})

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal


@pytest.mark.asyncio
async def test_the_same_paths_written_plainly_are_admitted(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(BRANCH, bundle())

    assert (await _admit(project, runner)).ok


