"""Admitting a feature whose spec and plan were written elsewhere.

4 October 2026 (project initialisation design, Part 6, points 2 and 3, and
the round 1-3 review dispositions R1, R3 and R7). Every "remote" here is a bare
repository in a temporary directory and the runner is the coordinator's own
``WorktreeGitRunner``, so a fetch and every committed-file read is real git.
Nothing touches a live service, a sandbox or a model.

The project in these tests is synthetic: a few text files, no code, no
particular language.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.deploy.candidate_tree import read_file_at_commit
from forge.pipeline.prepared_admission import (
    admit_prepared_build,
    guide_claims_routes,
    relative_markdown_links,
)

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


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


@pytest.fixture
def runner(tmp_path: Path) -> WorktreeGitRunner:
    return WorktreeGitRunner(worktrees_root=tmp_path / "wt")


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
    project: Project, runner: WorktreeGitRunner
) -> None:
    _git(project.copy, "remote", "set-url", "origin", str(project.remote) + "-gone")

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "could not be reached" in (answer.refusal or "")


# ---------------------------------------------------------------------------
# The supplied bundle: each missing item refused by name (R3)
# ---------------------------------------------------------------------------

_MISSING_CASES = {
    "feature file": (f".guardkit/features/{FEATURE}.yaml", "its feature file"),
    "task file": (f"{TASK_DIR}/{TASKS[1]}-do-the-thing.md", "the task file"),
    "spec file": (f"{SPEC_DIR}/{SPEC_NAME}.feature", "the spec file"),
    "assumptions": (f"{SPEC_DIR}/{SPEC_NAME}_assumptions.yaml", "assumptions file"),
    "summary": (f"{SPEC_DIR}/{SPEC_NAME}_summary.md", "the spec's summary"),
    "guide": (f"{TASK_DIR}/IMPLEMENTATION-GUIDE.md", "the plan's guide"),
    "declared document": (
        "docs/constitution/mission.md",
        "autobuild.player.required_documents",
    ),
    "pass bar": (f"qa/pass-bar-{TASKS[0]}.yaml", f"the pass bar for {TASKS[0]}"),
    "QA seed": (f"qa/pass-bar-seed-{SPEC_NAME}.yaml", "the spec's QA seed"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_MISSING_CASES))
async def test_each_missing_bundle_item_is_refused_by_name(
    project: Project, runner: WorktreeGitRunner, case: str
) -> None:
    path, words = _MISSING_CASES[case]
    files = bundle()
    files.pop(path)
    # The summary links to the guide and the guide to the mission; drop
    # nothing else, so the FIRST missing item is the one removed.
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert not answer.ok, case
    assert path in (answer.refusal or ""), answer.refusal
    assert words in (answer.refusal or ""), answer.refusal


@pytest.mark.asyncio
@pytest.mark.parametrize("item", ["pass bar", "QA seed"])
async def test_the_qa_files_are_required_with_tier1_enforcement_off(
    project: Project, runner: WorktreeGitRunner, item: str
) -> None:
    path, _words = _MISSING_CASES[item]
    files = bundle(config=config_text(tier1_enforced=False))
    files.pop(path)
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert not answer.ok
    assert path in (answer.refusal or "")


@pytest.mark.asyncio
async def test_a_feature_file_naming_another_feature_is_refused(
    project: Project, runner: WorktreeGitRunner
) -> None:
    files = bundle()
    files[f".guardkit/features/{FEATURE}.yaml"] = files[
        f".guardkit/features/{FEATURE}.yaml"
    ].replace(f"id: {FEATURE}", "id: FEAT-0THER")
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "FEAT-0THER" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_a_broken_relative_link_is_refused_naming_the_link(
    project: Project, runner: WorktreeGitRunner
) -> None:
    files = bundle()
    task_path = f"{TASK_DIR}/{TASKS[0]}-do-the-thing.md"
    files[task_path] += "\nAlso read [the contract](../../../docs/contracts/api.md).\n"
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "docs/contracts/api.md" in (answer.refusal or "")
    assert task_path in (answer.refusal or "")


@pytest.mark.asyncio
async def test_a_link_to_a_folder_is_refused(
    project: Project, runner: WorktreeGitRunner
) -> None:
    files = bundle()
    task_path = f"{TASK_DIR}/{TASKS[0]}-do-the-thing.md"
    files[task_path] += "\nThe [constitution](../../../docs/constitution) folder.\n"
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "docs/constitution" in (answer.refusal or "")


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


@pytest.mark.asyncio
async def test_a_declared_document_that_is_a_symbolic_link_is_refused(
    project: Project, runner: WorktreeGitRunner
) -> None:
    files = bundle()
    files.pop("docs/constitution/mission.md")
    files["docs/constitution/real-mission.md"] = "The mission.\n"
    project.commit_on(BRANCH, files)
    # Make mission.md a symbolic link on the branch.
    _git(project.writer, "checkout", "-q", BRANCH)
    (project.writer / "docs" / "constitution" / "mission.md").symlink_to(
        "real-mission.md"
    )
    _git(project.writer, "add", "-A")
    _git(project.writer, "commit", "-qm", "a link")
    _git(project.writer, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{BRANCH}")
    _git(project.writer, "checkout", "-q", "main")

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "docs/constitution/mission.md" in (answer.refusal or "")
    assert "symbolic link" in (answer.refusal or "")


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


@pytest.mark.asyncio
async def test_a_binding_document_that_is_not_utf8_is_refused(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(BRANCH, bundle())
    _commit_bytes_on(project, BRANCH, {"docs/constitution/mission.md": b"The \xff mission\n"})

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "docs/constitution/mission.md" in (answer.refusal or "")
    assert "not UTF-8 text" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_documents_over_the_budget_are_refused(
    project: Project, runner: WorktreeGitRunner
) -> None:
    from forge.planning.project_documents import PROJECT_DOCUMENTS_BUDGET_BYTES

    files = bundle()
    files["docs/constitution/mission.md"] = "m" * (PROJECT_DOCUMENTS_BUDGET_BYTES + 1)
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert not answer.ok
    assert str(PROJECT_DOCUMENTS_BUDGET_BYTES) in (answer.refusal or "")


@pytest.mark.asyncio
async def test_a_declared_instruction_file_must_be_there(
    project: Project, runner: WorktreeGitRunner
) -> None:
    config = config_text().replace(
        "    required_documents:", "    instructions: [docs/how-we-work.md]\n    required_documents:"
    )
    project.commit_on(BRANCH, bundle(config=config))

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "docs/how-we-work.md" in (answer.refusal or "")
    assert "autobuild.player.instructions" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_an_instruction_link_out_of_the_repository_is_refused(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(BRANCH, bundle())
    _git(project.writer, "checkout", "-q", BRANCH)
    (project.writer / "AGENTS.md").symlink_to("../../outside/AGENTS.md")
    _git(project.writer, "add", "-A")
    _git(project.writer, "commit", "-qm", "a link out")
    _git(project.writer, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{BRANCH}")
    _git(project.writer, "checkout", "-q", "main")

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "AGENTS.md" in (answer.refusal or "")
    assert "outside the repository" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_an_unknown_player_key_is_refused_only_when_documents_are_declared(
    project: Project, runner: WorktreeGitRunner
) -> None:
    declared = config_text() + "    surprise: [x]\n"
    project.commit_on(BRANCH, bundle(config=declared))
    refused = await _admit(project, runner)

    undeclared = config_text(documents=()) + "autobuild:\n  player:\n    surprise: [x]\n"
    project.commit_on(BRANCH, bundle(config=undeclared))
    admitted = await _admit(project, runner)

    assert not refused.ok and "surprise" in (refused.refusal or "")
    assert admitted.ok, admitted.refusal


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
async def test_routing_law_off_still_needs_a_spec_file(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(BRANCH, bundle(routing_off=True))
    assert (await _admit(project, runner)).ok

    files = bundle(routing_off=True)
    plan_path = f".guardkit/features/{FEATURE}.yaml"
    files[plan_path] = files[plan_path].replace(
        f'feature_files:\n  - "{SPEC_DIR}/{SPEC_NAME}.feature"\n', ""
    )
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "lists no spec file" in (answer.refusal or "")


@pytest.mark.asyncio
async def test_a_guide_declaring_a_route_needs_the_leak_sweep_manifest(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(BRANCH, bundle(routes=True, leak_sweep=False))

    answer = await _admit(project, runner)

    assert not answer.ok
    assert "qa/leak-sweep.yaml" in (answer.refusal or "")


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


# ---------------------------------------------------------------------------
# The two text rules, directly
# ---------------------------------------------------------------------------


def test_the_integration_contracts_rule_matches_the_producers() -> None:
    assert guide_claims_routes("## §4 Integration Contracts\n- route: /a\n")
    assert guide_claims_routes("### Integration Contracts\nroute: /a\n## Next\n")
    assert guide_claims_routes("## §4\n  - route: /a\n")
    assert guide_claims_routes("## 4 Integration Contract\nROUTE: /a\n")
    assert not guide_claims_routes("## Integration Contracts\nnone\n## B\nroute: /b\n")
    assert not guide_claims_routes("# Integration Contracts\nroute: /a\n")
    assert not guide_claims_routes("## Overview\nroute: /a\n")
    assert not guide_claims_routes("## §4 Integration Contracts\nroute:   \n")


def test_guardkits_colon_form_is_not_recognised_like_the_emitter() -> None:
    # GuardKit's template writes this form; the specialist emitter does not
    # recognise it, and neither does admission (noted for the producers).
    assert not guide_claims_routes("## §4: Integration Contracts\n- route: /a\n")


def test_the_heading_is_case_sensitive_like_the_emitter() -> None:
    assert not guide_claims_routes("## integration contracts\nroute: /a\n")
    assert not guide_claims_routes("## §4 INTEGRATION CONTRACTS\nroute: /a\n")


def test_only_the_first_matching_section_is_read() -> None:
    first_empty = (
        "## §4 Integration Contracts\nnone here\n"
        "## Other\n\n## §4 Integration Contracts\n- route: /late\n"
    )
    assert not guide_claims_routes(first_empty)
    # The fallback heading is read only when no §4 heading exists at all.
    assert not guide_claims_routes(
        "## §4\nnothing\n## Integration Contracts\nroute: /a\n"
    )


def test_only_relative_markdown_links_are_collected() -> None:
    text = (
        "[a](docs/a.md#part) ![i](img/x.png) [u](https://x.invalid) [h](#top) "
        "`[c](code.md)` [s](//cdn.invalid/x) [q](b.md?raw=1) [sp](a%20b.md)\n"
        "```\n[f](fenced.md)\n```\n[t](c.md \"title\")"
    )
    assert relative_markdown_links(text) == [
        "docs/a.md",
        "img/x.png",
        "b.md",
        "a b.md",
        "c.md",
    ]
