"""Forge's one "run git" helper runs no program the project names.

Ported from GuardKit's ``tests/unit/test_project_git.py`` (the same names, the
same cases), so both copies of the rules are pinned by the same tests; Forge's
own additions (``run_git_async``, ``open_git``, the caller allow-list,
``commit-tree``, ``grep -O`` and the local fetch from the factory's own copy)
are at the end.

Real git in ``tmp_path``. Each repository's own settings and hooks name marker
programs (see :mod:`tests.forge.git.git_marker_lab`). Every marker writes its
name and whether it could see the factory's secret.

* ``test_planted_settings_fire_with_plain_git`` is the positive control: plain
  ``git`` with the inherited environment runs the planted programs, so the
  fixture really does plant something on this machine's git.
* ``test_run_git_runs_no_project_program`` runs the same cases through
  :func:`forge.project_git.run_git` and expects no marker.
* The remaining tests pin the individual rules: names read just before each
  command, the fixed environment, signing, submodules and nested
  repositories, rebases, network commands, new worktrees, identity, literal
  file names and fixed failure sentences.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import pytest

from tests.forge.git.git_marker_lab import (
    GIT,
    SECRET_NAME,
    SECRET_VALUE,
    Lab,
)


def _pg():
    """The helper module, imported late so the positive controls run without it."""
    from forge import project_git

    return project_git


@pytest.fixture()
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    made = Lab(tmp_path / "lab")
    monkeypatch.setenv("HOME", str(made.home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS",
                 "GIT_CONFIG_COUNT", "GIT_EXTERNAL_DIFF", "GIT_ASKPASS", "SSH_ASKPASS",
                 "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.delenv(name, raising=False)
    return made


Runner = Callable[..., subprocess.CompletedProcess]


def _plain_runner(lab: Lab) -> Runner:
    def run(repo: Path, *args: str, input: Optional[str] = None) -> subprocess.CompletedProcess:
        return lab.plain(repo, *args, input=input)

    return run


def _helper_runner(lab: Lab) -> Runner:
    run_git = _pg().run_git

    def run(repo: Path, *args: str, input: Optional[str] = None) -> subprocess.CompletedProcess:
        return run_git(list(args), cwd=repo, input=input)

    return run


# ---------------------------------------------------------------------------
# The cases: one per row of the design's "What I proved" table


@dataclass(frozen=True)
class Case:
    id: str
    build: Callable[[Lab], Path]
    commands: Callable[[Lab, Path], List[Sequence[str]]]
    #: Programs plain git runs today (all must fire, with the secret).
    expect: frozenset
    #: The helper refuses this case with the network sentence.
    network: bool = False
    #: stdin for the last command.
    stdin: Optional[str] = None


def _marked(edits: bool = True, process: bool = False) -> Callable[[Lab], Path]:
    return lambda lab: lab.marked_repo("repo", edits=edits, process=process)


def _same(*argv: str) -> Callable[[Lab, Path], List[Sequence[str]]]:
    return lambda lab, repo: [list(argv)]


def _without_external_diff(lab: Lab) -> Path:
    repo = lab.marked_repo("repo")
    lab.setup_git(repo, "config", "--unset", "diff.external")
    return repo


def _apply_patch(lab: Lab) -> Path:
    repo = lab.marked_repo("repo", edits=False)
    (lab.root / "a.patch").write_text(
        lab.setup_git(repo, "diff", "--no-ext-diff", "--no-textconv", "HEAD~1", "HEAD", "--", "a.txt")
    )
    return repo


def _on_side(lab: Lab) -> Path:
    repo = lab.marked_repo("repo", edits=False)
    lab.setup_git(repo, "checkout", "-q", "side")
    return repo


def _verify_signatures(lab: Lab) -> Path:
    repo = lab.merge_repo("repo")
    lab.setup_git(repo, "config", "merge.verifySignatures", "true")
    return repo


def _merge_options(lab: Lab) -> Path:
    repo = lab.merge_repo("repo")
    lab.setup_git(repo, "config", "branch.main.mergeOptions", "--gpg-sign")
    return repo


def _merge_options_include(lab: Lab) -> Path:
    repo = lab.merge_repo("repo")
    lab.include(repo, None, '[branch "main"]\n\tmergeOptions = --gpg-sign\n'
                "[merge]\n\tverifySignatures = true\n", "signing.inc")
    return repo


def _named_remote(lab: Lab) -> Path:
    repo = lab.base_repo("repo")
    origin = lab.root / "origin.git"
    lab.setup_git(lab.root, "clone", "-q", "--bare", str(repo), str(origin))
    lab.setup_git(repo, "remote", "add", "origin", str(origin))
    lab.setup_git(repo, "config", "remote.origin.uploadpack", lab.path("uploadpack"))
    hooks = repo / ".git" / "hooks"
    shutil.copy(lab.bin / "hook-reference-transaction", hooks / "reference-transaction")
    return repo


def _ssh_command(lab: Lab) -> Path:
    repo = lab.base_repo("repo")
    lab.setup_git(repo, "config", "core.sshCommand", lab.path("sshcmd"))
    return repo


def _credential_helpers(lab: Lab) -> Path:
    repo = lab.base_repo("repo")
    lab.setup_git(repo, "config", "credential.helper", lab.path("credhelper"))
    lab.setup_git(repo, "config", "credential.https://example.invalid.helper",
                  lab.path("credhelper_url"))
    return repo


def _askpass(lab: Lab) -> Path:
    repo = lab.base_repo("repo")
    lab.setup_git(repo, "config", "core.askPass", lab.path("askpass"))
    return repo


def _lazy_ext(lab: Lab) -> Path:
    repo = lab.partial_clone("repo")
    lab.setup_git(repo, "config", "remote.origin.url", f"ext::{lab.path('extmark')} %S")
    lab.setup_git(repo, "config", "protocol.ext.allow", "always")
    return repo


def _lazy_git_proxy(lab: Lab) -> Path:
    repo = lab.partial_clone("repo")
    lab.setup_git(repo, "config", "remote.origin.url", "git://127.0.0.1:9/x")
    lab.setup_git(repo, "config", "protocol.git.allow", "always")
    lab.setup_git(repo, "config", "core.gitProxy", lab.path("gitproxy"))
    return repo


def _include_repo(condition: str, program: str) -> Callable[[Lab], Path]:
    def build(lab: Lab) -> Path:
        repo = lab.base_repo("repo", attributes="*.txt filter=late\n")
        lab.include(repo, condition, f'[filter "late"]\n\tsmudge = {lab.path(program)}\n', "cond.inc")
        return repo

    return build


def _new_worktree(lab: Lab, repo: Path) -> List[Sequence[str]]:
    return [["worktree", "add", "-q", str(lab.root / "new-worktree"), "-b", "autobuild/new", "main"]]


def _branch_pair_with_include(start: str, condition: str) -> Callable[[Lab], Path]:
    """``main`` and ``side/9`` both change ``a.txt``; HEAD is on ``start``; an
    include active on ``condition`` names a smudge filter."""

    def build(lab: Lab) -> Path:
        repo = lab.base_repo("repo", attributes="*.txt filter=late\n")
        g = lambda *a: lab.setup_git(repo, *a)  # noqa: E731
        (repo / "a.txt").write_text("1\n2\n3\n4\n5\n6\n")
        g("commit", "-qam", "six")
        g("checkout", "-q", "-b", "side/9")
        (repo / "a.txt").write_text("1\n2\n3\n4\n5\nsix\n")
        g("commit", "-qam", "side")
        g("checkout", "-q", "main")
        (repo / "a.txt").write_text("one\n2\n3\n4\n5\n6\n")
        g("commit", "-qam", "main")
        g("checkout", "-q", start)
        lab.include(repo, condition, f'[filter "late"]\n\tsmudge = {lab.path("smudge_co")}\n', "co.inc")
        return repo

    return build


def _submodule(lab: Lab) -> Path:
    return lab.submodule_repo("repo")


READ_COMMANDS = [
    ["rev-parse", "HEAD"],
    ["symbolic-ref", "-q", "HEAD"],
    ["ls-tree", "-r", "-z", "HEAD"],
    ["cat-file", "blob", "HEAD:a.txt"],
    ["merge-base", "HEAD", "side"],
    ["rev-list", "--count", "main..side"],
    ["log", "--format=%H", "-1", "--", "a.txt"],
    ["show", "HEAD:a.txt"],
]

CASES = [
    Case("reads", _marked(), lambda lab, repo: READ_COMMANDS, frozenset()),
    Case("status", _marked(), _same("status", "--porcelain"),
         frozenset({"clean", "fsmonitor"})),
    Case("status-process-filter", _marked(process=True), _same("status", "--porcelain"),
         frozenset({"process"})),
    Case("diff-name-only", _marked(), _same("diff", "--name-only", "HEAD"),
         frozenset({"clean", "fsmonitor"})),
    Case("diff-numstat", _marked(), _same("diff", "--numstat", "HEAD"),
         frozenset({"clean", "fsmonitor"})),
    Case("diff-process-filter", _marked(process=True), _same("diff", "--name-only", "HEAD"),
         frozenset({"process"})),
    Case("diff-quiet", _marked(), _same("diff", "--quiet", "HEAD"),
         frozenset({"clean", "fsmonitor"})),
    Case("diff-patch", _marked(), _same("diff", "HEAD"),
         frozenset({"clean", "fsmonitor", "extdiff"})),
    Case("diff-U0-text-converter", _without_external_diff, _same("diff", "-U0", "HEAD"),
         frozenset({"clean", "fsmonitor", "textconv"})),
    Case("ls-files", _marked(), _same("ls-files", "--others", "--exclude-standard"),
         frozenset({"fsmonitor"})),
    Case("diff-cached", _marked(), _same("diff", "--cached", "--name-only"),
         frozenset({"fsmonitor"})),
    Case("add-then-commit", _marked(),
         lambda lab, repo: [["add", "-A"], ["commit", "-q", "-m", "checkpoint"]],
         frozenset({"clean", "fsmonitor", "hook-pre-commit",
                    "hook-prepare-commit-msg", "hook-commit-msg", "gpg"})),
    Case("reset-hard", _marked(), _same("reset", "-q", "--hard", "HEAD"),
         frozenset({"smudge", "fsmonitor"})),
    Case("checkout", _marked(edits=False), _same("checkout", "-q", "side"),
         frozenset({"smudge", "hook-post-checkout"})),
    Case("worktree-add", _marked(edits=False),
         lambda lab, repo: [["worktree", "add", "-q", str(lab.root / "wt"), "side"]],
         frozenset({"smudge", "hook-post-checkout"})),
    Case("apply-reverse", _apply_patch, _same("apply", "-R", "--recount", "-"),
         frozenset({"clean", "smudge"}), stdin="patch"),
    Case("checkout-process-filter", _marked(process=True),
         _same("checkout", "-q", "HEAD", "--", "c.dat"), frozenset({"process"})),
    Case("merge", _marked(edits=False), _same("merge", "--no-ff", "-q", "-m", "join", "side"),
         frozenset({"mergedrv", "hook-reference-transaction"})),
    Case("rebase", _on_side, _same("rebase", "main"),
         frozenset({"mergedrv", "hook-pre-rebase", "hook-reference-transaction"})),
    Case("merge-verify-signatures", _verify_signatures,
         _same("merge", "--no-ff", "-q", "-m", "m", "side"), frozenset({"gpg"})),
    Case("merge-options", _merge_options,
         _same("merge", "--no-ff", "-q", "-m", "m", "side"), frozenset({"gpg"})),
    Case("merge-options-include", _merge_options_include,
         _same("merge", "--no-ff", "-q", "-m", "m", "side"), frozenset({"gpg"})),
    Case("branch-delete", _marked(edits=False), _same("branch", "-D", "side"),
         frozenset({"hook-reference-transaction"})),
    Case("fetch-named-remote", _named_remote, _same("fetch", "-q", "origin", "main"),
         frozenset({"uploadpack"}), network=True),
    Case("fetch-ssh-command", _ssh_command, _same("fetch", "ssh://example.invalid/x", "main"),
         frozenset({"sshcmd"}), network=True),
    Case("credential-helpers", _credential_helpers, _same("credential", "fill"),
         frozenset({"credhelper", "credhelper_url"}), network=True,
         stdin="protocol=https\nhost=example.invalid\n\n"),
    Case("credential-askpass", _askpass, _same("credential", "fill"),
         frozenset({"askpass"}), network=True,
         stdin="protocol=https\nhost=example.invalid\n\n"),
    Case("lazy-fetch-ext", _lazy_ext, _same("checkout", "-q", "main"), frozenset({"extmark"})),
    Case("lazy-fetch-git-proxy", _lazy_git_proxy, _same("checkout", "-q", "main"),
         frozenset({"gitproxy"})),
    Case("worktree-add-gitdir-include", _include_repo("gitdir:**/worktrees/**", "smudge_wt"),
         _new_worktree, frozenset({"smudge_wt"})),
    Case("worktree-add-onbranch-include", _include_repo("onbranch:autobuild/**", "smudge_br"),
         _new_worktree, frozenset({"smudge_br"})),
    Case("merge-starting-branch-include", _branch_pair_with_include("main", "onbranch:main"),
         _same("merge", "--no-ff", "-q", "-m", "m", "side/9"), frozenset({"smudge_co"})),
    Case("rebase-starting-branch-include", _branch_pair_with_include("side/9", "onbranch:side/**"),
         _same("rebase", "main"), frozenset({"smudge_co"})),
    Case("submodule-status", _submodule, _same("status", "--porcelain"), frozenset({"subclean"})),
    Case("submodule-diff", _submodule, _same("diff", "--name-only", "HEAD"),
         frozenset({"subclean"})),
    Case("submodule-add", _submodule, _same("add", "-A"), frozenset({"subclean"})),
]

# ---------------------------------------------------------------------------
# Forge's own local commands (the design's "Local commands GuardKit's tests did
# not cover" table). Shared with ``test_planted_settings.py``, whose positive
# controls run them with plain git.


def _diff_converter_only(lab: Lab) -> Path:
    """The marked repository with no external diff, so the text converter is
    what a patch read would run."""
    repo = lab.marked_repo("repo", edits=False)
    lab.setup_git(repo, "config", "--unset", "diff.external")
    return repo


def _stale_build_worktree(lab: Lab) -> Path:
    """A build worktree the model wrote in, with a same-size edit, as the
    build-start sweep finds it."""
    repo = lab.marked_repo("repo", edits=False)
    tree = lab.root / "stale-worktree"
    lab.setup_git(repo, "worktree", "add", "-q", "--detach", str(tree), "side")
    lab.same_size_edit(tree / "b.txt")
    return tree


def _forced_add(lab: Lab, repo: Path) -> List[Sequence[str]]:
    (repo / "ignored.txt").write_text("force\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    return [["add", "-f", "--", "ignored.txt", "a.txt"], ["commit", "-q", "-m", "repair"]]


FORGE_LOCAL_CASES = [
    Case("archive", _marked(edits=False), _same("archive", "--format=tar", "HEAD"),
         frozenset({"smudge"})),
    Case("merge-tree", _marked(edits=False), _same("merge-tree", "--write-tree", "main", "side"),
         frozenset({"mergedrv"})),
    Case("update-ref", _marked(edits=False), _same("update-ref", "refs/heads/preview", "side"),
         frozenset({"hook-reference-transaction"})),
    Case("diff -G", _diff_converter_only,
         _same("diff", "-U0", "-Gone", "side...main"), frozenset({"textconv"})),
    Case("status", _marked(), _same("status", "--porcelain=v1", "-z"),
         frozenset({"clean", "fsmonitor"})),
    Case("grep", _marked(), _same("grep", "-I", "-n", "-e", "3"), frozenset({"fsmonitor"})),
    Case("worktree add --lock", _marked(edits=False),
         lambda lab, repo: [["worktree", "add", "--lock", "-b", "autobuild/FEAT-1",
                             str(lab.root / "locked-worktree"), "main"]],
         frozenset({"smudge", "hook-post-checkout"})),
    Case("checkout --detach", _stale_build_worktree, _same("checkout", "--detach", "main"),
         frozenset({"smudge", "hook-post-checkout"})),
    Case("add -f and commit", _marked(), _forced_add,
         frozenset({"clean", "fsmonitor", "hook-pre-commit", "gpg"})),
]

CASES = CASES + FORGE_LOCAL_CASES

POSITIVE = [case for case in CASES if case.expect]


def _stdin(lab: Lab, case: Case) -> Optional[str]:
    if case.stdin == "patch":
        return (lab.root / "a.patch").read_text()
    return case.stdin


def _run_case(lab: Lab, case: Case, runner: Runner) -> List[subprocess.CompletedProcess]:
    repo = case.build(lab)
    lab.fired()
    commands = case.commands(lab, repo)
    done = []
    for index, argv in enumerate(commands):
        stdin = _stdin(lab, case) if index == len(commands) - 1 else None
        done.append(runner(repo, *argv, input=stdin))
    return done


@pytest.mark.parametrize("case", POSITIVE, ids=[case.id for case in POSITIVE])
def test_planted_settings_fire_with_plain_git(lab: Lab, case: Case) -> None:
    _run_case(lab, case, _plain_runner(lab))
    fired = lab.fired()
    assert case.expect <= set(Lab.names(fired)), fired
    assert Lab.saw_secret(fired), fired


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_run_git_runs_no_project_program(lab: Lab, case: Case) -> None:
    done = _run_case(lab, case, _helper_runner(lab))
    assert lab.fired() == []
    if case.network:
        assert done[-1].returncode != 0
        assert done[-1].stderr == _pg().NETWORK_REFUSAL
    elif case.id == "submodule-add":
        assert done[-1].stderr == _pg().SUBMODULE_REFUSAL.format(command="add")
    elif case.id == "diff-quiet":
        assert done[-1].returncode == 1  # "there are differences", not a failure
    elif not case.id.startswith("lazy-fetch"):
        assert [d.returncode for d in done] == [0] * len(done), [d.stderr for d in done]


# ---------------------------------------------------------------------------
# Names are read just before each command, and the race is bounded


def test_run_git_reads_names_just_before_the_command(lab: Lab) -> None:
    repo = lab.base_repo("repo", attributes="*.txt filter=late\n")
    lab.setup_git(repo, "config", "filter.late.clean", lab.path("late"))
    lab.same_size_edit(repo / "a.txt")
    lab.fired()
    lab.plain(repo, "status", "--porcelain")
    assert Lab.names(lab.fired()) == ["late"]  # positive control
    lab.same_size_edit(repo / "a.txt")
    done = _pg().run_git(["status", "--porcelain"], cwd=repo)
    assert done.returncode == 0
    assert lab.fired() == []


def test_run_git_race_is_bounded_to_no_secrets(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg = _pg()
    repo = lab.base_repo("repo", attributes="*.txt filter=racer\n")
    dump = lab.root / "racer-environment.txt"
    lab.program("racer", f'env > "{dump}"\ncat\n')
    real_read = pg._read_setting_names

    def read_then_race(*args, **kwargs):
        names = real_read(*args, **kwargs)
        lab.setup_git(repo, "config", "filter.racer.clean", lab.path("racer"))
        return names

    monkeypatch.setattr(pg, "_read_setting_names", read_then_race)
    lab.same_size_edit(repo / "a.txt")
    lab.fired()
    pg.run_git(["status", "--porcelain"], cwd=repo)
    fired = lab.fired()
    assert Lab.names(fired) == ["racer"]  # the stated limit: a race does run it ...
    assert not Lab.saw_secret(fired)  # ... but without any secret
    seen = dict(
        line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line
    )
    assert SECRET_NAME not in seen
    assert "SSH_AUTH_SOCK" not in seen
    assert seen.get("GIT_ALLOW_PROTOCOL") == "none"
    assert seen.get("GIT_NO_LAZY_FETCH") == "1"


def test_run_git_environment_has_no_secret(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    dump = lab.root / "monitor-environment.txt"
    lab.program("envdump", f'env > "{dump}"\nexit 1\n')
    lab.setup_git(repo, "config", "core.fsmonitor", lab.path("envdump"))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(lab.root / "agent.sock"))
    # The three secrets Forge's sandbox containers carry.
    for name in ("OPENAI_API_KEY", "FLEET_MEMORY_PG_DSN", "GUARDKIT_NATS_PASSWORD"):
        monkeypatch.setenv(name, "not-a-real-value")
    lab.plain(repo, "status", "--porcelain")
    assert f"{SECRET_NAME}={SECRET_VALUE}" in dump.read_text()  # positive control
    dump.unlink()
    pg.run_git(["status", "--porcelain"], cwd=repo)
    assert not dump.exists()  # the file monitor is switched off altogether
    environment = pg._child_environment({})
    assert SECRET_NAME not in environment
    assert "SSH_AUTH_SOCK" not in environment
    for name in ("OPENAI_API_KEY", "FLEET_MEMORY_PG_DSN", "GUARDKIT_NATS_PASSWORD"):
        assert name not in environment
    assert environment["GIT_ALLOW_PROTOCOL"] == "none"
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["LC_ALL"] == "C"
    assert set(environment) <= set(pg.FIXED_ENVIRONMENT) | set(pg.KEPT_ENVIRONMENT_NAMES) | {"PATH"}


def test_run_git_ignores_injected_settings(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = lab.base_repo("repo", attributes="*.txt diff=mark\n")
    (repo / "a.txt").write_text("changed\n")
    injected = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": lab.path("fsmonitor"),
        "GIT_CONFIG_PARAMETERS": f"'diff.mark.textconv'='{lab.path('textconv')}'",
        "GIT_EXTERNAL_DIFF": lab.path("extdiff"),
        "GIT_ASKPASS": lab.path("askpass"),
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    lab.fired()
    lab.plain(repo, "status", "--porcelain")
    lab.plain(repo, "diff", "HEAD")
    assert {"fsmonitor", "extdiff"} <= set(Lab.names(lab.fired()))  # positive control
    pg = _pg()
    assert pg.run_git(["status", "--porcelain"], cwd=repo).returncode == 0
    assert pg.run_git(["diff", "HEAD"], cwd=repo).returncode == 0
    assert lab.fired() == []


# ---------------------------------------------------------------------------
# Merges: signing and merge programs


@pytest.mark.parametrize(
    "build", [_verify_signatures, _merge_options, _merge_options_include],
    ids=["verifySignatures", "mergeOptions", "include"],
)
def test_run_git_merge_signing_is_off(lab: Lab, build) -> None:
    repo = build(lab)
    lab.fired()
    done = _pg().run_git(["merge", "--no-ff", "-q", "-m", "m", "side"], cwd=repo)
    assert done.returncode == 0
    assert lab.fired() == []
    assert (repo / "b.md").exists()


def test_run_git_merge_program_replaced_by_git_merge(lab: Lab) -> None:
    repo = lab.marked_repo("repo", edits=False)
    lab.fired()
    done = _pg().run_git(["merge", "--no-ff", "-q", "-m", "join", "side"], cwd=repo)
    assert done.returncode == 0
    assert lab.fired() == []
    assert (repo / "a.txt").read_text() == "one\n2\n3\n4\n5\nsix\n"


# ---------------------------------------------------------------------------
# Submodules and nested repositories


@pytest.mark.parametrize(
    "argv",
    [
        ["add", "-A"],
        ["commit", "-q", "--allow-empty", "-m", "c"],
        ["checkout", "-q", "-b", "other"],
        ["reset", "-q", "--hard", "HEAD"],
        ["merge", "--no-ff", "-q", "-m", "m", "main"],
    ],
    ids=["add", "commit", "checkout", "reset", "merge"],
)
def test_run_git_refuses_writes_with_a_submodule(lab: Lab, argv: List[str]) -> None:
    pg = _pg()
    repo = lab.submodule_repo("repo")
    head = lab.setup_git(repo, "rev-parse", "HEAD")
    lab.fired()
    done = pg.run_git(argv, cwd=repo)
    assert done.returncode != 0
    assert done.stderr == pg.SUBMODULE_REFUSAL.format(command=argv[0])
    assert "sub" not in done.stderr.replace("submodule", "")
    assert lab.fired() == []
    assert lab.setup_git(repo, "rev-parse", "HEAD") == head


def _nested(lab: Lab, repo: Path, where: str) -> None:
    inner = repo / where
    inner.mkdir(parents=True)
    lab.setup_git(inner, "init", "-q")
    (inner / "f.md").write_text("f\n")
    lab.setup_git(inner, "add", "-A")
    lab.setup_git(inner, "-c", "user.name=Lab", "-c", "user.email=lab@example.invalid",
                  "commit", "-qm", "nested")


@pytest.mark.parametrize(
    "where", ["inner", "vendor/component"], ids=["top level", "below an untracked folder"]
)
def test_run_git_refuses_add_of_a_nested_repository(lab: Lab, where: str) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    _nested(lab, repo, where)
    (repo / "plain" / "deep").mkdir(parents=True)
    (repo / "plain" / "deep" / "x.md").write_text("x\n")
    index = repo / ".git" / "index"
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    done = pg.run_git(["add", "-A"], cwd=repo)
    assert done.returncode != 0
    assert done.stderr == pg.SUBMODULE_REFUSAL.format(command="add")
    assert where.split("/")[0] not in done.stderr
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("where", ["ignored", "node_modules"],
                         ids=["ignored", "node_modules exclusion"])
def test_run_git_add_ignores_nested_repositories_add_would_skip(lab: Lab, where: str) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    (repo / ".gitignore").write_text("ignored/\n")
    lab.setup_git(repo, "add", "-A")
    lab.setup_git(repo, "commit", "-qm", "ignore")
    _nested(lab, repo, f"{where}/pkg")
    (repo / "new.md").write_text("n\n")
    argv = ["add", "-A", "--", "."]
    if where == "node_modules":
        argv.append(pg.pattern_pathspec(":(exclude,glob)**/node_modules/**"))
    done = pg.run_git(argv, cwd=repo)
    assert done.returncode == 0, done.stderr
    staged = lab.setup_git(repo, "ls-files", "-s")
    assert "160000" not in staged
    assert "new.md" in staged


# ---------------------------------------------------------------------------
# Rebases


def _two_branches(lab: Lab, with_merge: bool) -> Path:
    repo = lab.base_repo("repo")
    g = lambda *a: lab.setup_git(repo, *a)  # noqa: E731
    g("checkout", "-q", "-b", "side")
    (repo / "s.md").write_text("s\n")
    g("add", "-A")
    g("commit", "-qm", "side one")
    if with_merge:
        g("checkout", "-q", "-b", "topic")
        (repo / "u.md").write_text("u\n")
        g("add", "-A")
        g("commit", "-qm", "topic")
        g("checkout", "-q", "side")
        g("merge", "-q", "--no-ff", "-m", "merge topic", "topic")
    else:
        (repo / "t.md").write_text("t\n")
        g("add", "-A")
        g("commit", "-qm", "side two")
    g("checkout", "-q", "main")
    (repo / "m.md").write_text("m\n")
    g("add", "-A")
    g("commit", "-qm", "main")
    g("checkout", "-q", "side")
    return repo


def _exec_format(lab: Lab) -> str:
    return f"%s%nexec {lab.path('execmark')}"


@pytest.mark.parametrize("variant", ["instructionFormat", "rebase-merges", "include"])
def test_run_git_rebase_runs_no_exec_line(lab: Lab, variant: str) -> None:
    def build() -> Path:
        repo = _two_branches(lab, with_merge=variant == "rebase-merges")
        if variant == "include":
            lab.include(repo, None, f'[rebase]\n\tinstructionFormat = "{_exec_format(lab)}"\n'
                        "\trebaseMerges = true\n", "rebase.inc")
        else:
            lab.setup_git(repo, "config", "rebase.instructionFormat", _exec_format(lab))
            if variant == "rebase-merges":
                lab.setup_git(repo, "config", "rebase.rebaseMerges", "true")
        return repo

    control = build()
    lab.fired()
    lab.plain(control, "rebase", "--autostash", "main")
    assert "execmark" in Lab.names(lab.fired())  # positive control
    shutil.rmtree(control)
    repo = build()
    lab.fired()
    done = _pg().run_git(["rebase", "--autostash", "main"], cwd=repo)
    assert done.returncode == 0, done.stderr
    assert lab.fired() == []
    subjects = lab.setup_git(repo, "log", "--format=%s", "main..HEAD").split("\n")
    assert "side one" in subjects


@pytest.mark.parametrize("option", ["--exec=true", "-x", "-i", "--interactive"])
def test_run_git_refuses_rebase_programs(lab: Lab, option: str) -> None:
    pg = _pg()
    repo = _two_branches(lab, with_merge=False)
    argv = ["rebase", option] + (["true"] if option == "-x" else []) + ["main"]
    done = pg.run_git(argv, cwd=repo)
    assert done.returncode != 0
    assert done.stderr == pg.REBASE_REFUSAL


# ---------------------------------------------------------------------------
# Reads that could go into a submodule


def _traced(monkeypatch: pytest.MonkeyPatch, trace: Path) -> None:
    pg = _pg()
    real = pg._child_environment

    def with_trace(extra):
        return {**real(extra), "GIT_TRACE": str(trace)}

    monkeypatch.setattr(pg, "_child_environment", with_trace)


def _entered(trace: Path) -> bool:
    text = trace.read_text() if trace.exists() else ""
    return any(
        ("/sub" in line or "cd sub" in line or "'sub'" in line) and "run_command" in line
        for line in text.splitlines()
    )


def test_run_git_status_reports_a_moved_submodule_without_entering_it(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = lab.moved_submodule_repo("repo", external=True)
    trace = tmp_path / "trace.txt"
    _traced(monkeypatch, trace)
    lab.fired()
    done = _pg().run_git(["status", "--porcelain"], cwd=repo)
    assert done.returncode == 0
    assert " M sub" in done.stdout
    assert not _entered(trace)
    assert lab.fired() == []


PATCH_READS = {
    "diff": ["diff", "HEAD"],
    "diff -U0": ["diff", "-U0", "HEAD"],
    "log -p": ["log", "-p", "-1"],
    "show": ["show", "HEAD"],
}


@pytest.mark.parametrize("external", [True, False], ids=["external diff", "text converter"])
@pytest.mark.parametrize("read", list(PATCH_READS), ids=list(PATCH_READS))
def test_run_git_patch_reads_stay_out_of_submodules(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, read: str, external: bool
) -> None:
    repo = lab.moved_submodule_repo("repo", external=external)
    if read in ("log -p", "show"):
        lab.setup_git(repo, "add", "sub")
        lab.setup_git(repo, "commit", "-qm", "move sub")
    argv = PATCH_READS[read]
    trace = tmp_path / "plain-trace.txt"
    lab.fired()
    lab.plain(repo, *argv, env={**lab.plain_env(), "GIT_TRACE": str(trace)})
    assert lab.fired() or _entered(trace)  # positive control
    trace = tmp_path / "trace.txt"
    _traced(monkeypatch, trace)
    done = _pg().run_git(argv, cwd=repo)
    assert done.returncode == 0
    assert lab.fired() == []
    assert not _entered(trace)
    assert "Subproject commit" in done.stdout


def test_run_git_status_stays_out_of_submodules(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = lab.moved_submodule_repo("repo", external=False)
    lab.same_size_edit(repo / "sub" / "a.txt")
    trace = tmp_path / "plain-trace.txt"
    lab.plain(repo, "status", env={**lab.plain_env(), "GIT_TRACE": str(trace)})
    assert _entered(trace)  # positive control
    trace = tmp_path / "trace.txt"
    _traced(monkeypatch, trace)
    done = _pg().run_git(["status"], cwd=repo)
    assert done.returncode == 0
    assert not _entered(trace)


# ---------------------------------------------------------------------------
# Network commands and lazy fetches


@pytest.mark.parametrize(
    "argv",
    [
        ["fetch", "origin"],
        ["pull", "origin", "main"],
        ["push", "origin", "main"],
        ["clone", "origin-copy"],
        ["ls-remote", "origin"],
        ["credential", "fill"],
        ["remote", "update"],
        ["submodule", "update", "--init"],
    ],
    ids=["fetch", "pull", "push", "clone", "ls-remote", "credential", "remote update",
         "submodule"],
)
def test_run_git_refuses_network_commands(lab: Lab, argv: List[str]) -> None:
    pg = _pg()
    repo = _named_remote(lab)
    lab.fired()
    done = pg.run_git(argv, cwd=repo, input="protocol=https\nhost=example.invalid\n\n")
    assert done.returncode != 0
    assert done.stderr == pg.NETWORK_REFUSAL
    assert lab.fired() == []
    assert not (repo / "origin-copy").exists()


def test_run_git_allows_reading_remote_settings(lab: Lab) -> None:
    repo = _named_remote(lab)
    done = _pg().run_git(["remote", "-v"], cwd=repo)
    assert done.returncode == 0
    assert "origin" in done.stdout


@pytest.mark.parametrize("kind", ["ext", "git-proxy", "ordinary partial clone"])
def test_run_git_blocks_lazy_fetch(lab: Lab, kind: str) -> None:
    repo = {"ext": _lazy_ext, "git-proxy": _lazy_git_proxy,
            "ordinary partial clone": lambda lab: lab.partial_clone("repo")}[kind](lab)
    lab.fired()
    done = _pg().run_git(["checkout", "-q", "main"], cwd=repo)
    assert lab.fired() == []
    assert done.returncode != 0  # the missing file could not be fetched
    assert not (repo / "x.md").exists()


# ---------------------------------------------------------------------------
# New worktrees and branch switches


@pytest.mark.parametrize(
    "condition, program",
    [("gitdir:**/worktrees/**", "smudge_wt"), ("onbranch:autobuild/**", "smudge_br")],
    ids=["gitdir", "onbranch"],
)
def test_run_git_worktree_add_in_two_steps(lab: Lab, condition: str, program: str) -> None:
    pg = _pg()
    repo = _include_repo(condition, program)(lab)
    destination = lab.root / "new-worktree"
    lab.fired()
    done = pg.run_git(
        ["worktree", "add", "-q", str(destination), "-b", "autobuild/new", "main"], cwd=repo
    )
    assert done.returncode == 0, done.stderr
    assert lab.fired() == []
    assert (destination / "a.txt").read_text() == "one\n"
    assert pg.run_git(["status", "--porcelain"], cwd=destination).stdout == ""
    assert lab.fired() == []


def test_run_git_worktree_add_cleans_up_when_the_checkout_fails(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    destination = lab.root / "new-worktree"
    real_read = pg._read_setting_names

    def fail_in_the_new_worktree(prefix, cwd, timeout):
        if any(str(destination) == part for part in prefix):
            return None
        return real_read(prefix, cwd, timeout)

    monkeypatch.setattr(pg, "_read_setting_names", fail_in_the_new_worktree)
    done = pg.run_git(["worktree", "add", "-q", str(destination), "-b", "autobuild/x"], cwd=repo)
    assert done.returncode != 0
    assert done.stderr == pg.WORKTREE_REFUSAL
    assert not destination.exists()
    assert str(destination) not in lab.setup_git(repo, "worktree", "list")


@pytest.mark.parametrize("command", ["checkout", "checkout -b", "merge", "rebase"])
def test_run_git_switch_reads_settings_at_start(lab: Lab, command: str) -> None:
    start, condition = {
        "checkout": ("main", "onbranch:main"),
        "checkout -b": ("main", "onbranch:main"),
        "merge": ("main", "onbranch:main"),
        "rebase": ("side/9", "onbranch:side/**"),
    }[command]
    argv = {
        "checkout": ["checkout", "-q", "side/9"],
        "checkout -b": ["checkout", "-q", "-b", "fresh", "side/9"],
        "merge": ["merge", "--no-ff", "-q", "-m", "m", "side/9"],
        "rebase": ["rebase", "main"],
    }[command]
    build = _branch_pair_with_include(start, condition)
    control = build(lab)
    lab.fired()
    lab.plain(control, *argv)
    assert Lab.names(lab.fired()) == ["smudge_co"]  # positive control
    shutil.rmtree(control)
    repo = build(lab)
    lab.fired()
    done = _pg().run_git(argv, cwd=repo)
    assert done.returncode == 0, done.stderr
    assert lab.fired() == []


# ---------------------------------------------------------------------------
# Identity, file names and failures


@pytest.mark.parametrize("where", ["system", "global"])
def test_run_git_commit_identity(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    repo = lab.base_repo("repo")
    lab.setup_git(repo, "config", "--unset", "user.name")
    lab.setup_git(repo, "config", "--unset", "user.email")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                 "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    identity = f"[user]\n\tname = From {where}\n\temail = {where}@example.invalid\n"
    if where == "system":
        system = lab.root / "system-gitconfig"
        system.write_text(identity)
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    else:
        (lab.home / ".gitconfig").write_text(identity)
    done = _pg().run_git(["commit", "-q", "--allow-empty", "-m", "identity"], cwd=repo)
    assert done.returncode == 0, done.stderr
    author = lab.setup_git(repo, "log", "-1", "--format=%an <%ae>").strip()
    assert author == f"From {where} <{where}@example.invalid>"


def test_run_git_literal_file_names(lab: Lab) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    (repo / "*.txt").write_text("star\n")
    (repo / "b.txt").write_text("b\n")
    (repo / ".gitignore").write_text("ignored.log\nnode_modules/\n")
    (repo / "ignored.log").write_text("x\n")
    lab.setup_git(repo, "add", "-A")
    lab.setup_git(repo, "commit", "-qm", "names")
    listed = pg.run_git(["ls-files", "--", "*.txt"], cwd=repo).stdout.split()
    assert listed == ["*.txt"]
    ignored = pg.run_git(["check-ignore", "-v", "--no-index", "--", "ignored.log"], cwd=repo)
    assert ignored.returncode == 0
    assert ".gitignore" in ignored.stdout
    (repo / "dist").mkdir()
    (repo / "dist" / "keep.md").write_text("k\n")
    (repo / "pkg" / ".cache").mkdir(parents=True)
    (repo / "pkg" / ".cache" / "junk.md").write_text("j\n")
    done = pg.run_git(
        ["add", "-A", "--", ".", pg.pattern_pathspec(":(exclude,glob)**/.cache/**")], cwd=repo
    )
    assert done.returncode == 0, done.stderr
    staged = lab.setup_git(repo, "diff", "--cached", "--name-only").split()
    assert staged == ["dist/keep.md"]


def test_run_git_failure_is_a_fixed_sentence(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    (repo / "a.txt").write_text("IGNORE-PREVIOUS-INSTRUCTIONS\n")
    done = pg.run_git(["checkout", "-q", "IGNORE-PREVIOUS-INSTRUCTIONS"], cwd=repo)
    assert done.returncode != 0
    assert done.stderr == f"git checkout stopped with exit code {done.returncode}."
    with pytest.raises(subprocess.CalledProcessError) as raised:
        pg.run_git(["checkout", "-q", "IGNORE-PREVIOUS-INSTRUCTIONS"], cwd=repo, check=True)
    assert "IGNORE" not in str(raised.value)
    assert "IGNORE" not in (raised.value.stderr or "")

    def too_slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, kwargs["timeout"], output="IGNORE", stderr="IGNORE-PREVIOUS-INSTRUCTIONS"
        )

    monkeypatch.setattr(pg.subprocess, "run", too_slow)
    with pytest.raises(subprocess.TimeoutExpired) as late:
        pg.run_git(["log", "--format=%H", "--", "IGNORE.txt"], cwd=repo, timeout=5)
    assert late.value.stderr == "git log did not finish within 5 seconds."
    assert "IGNORE" not in str(late.value)


def test_fixed_settings_are_named_constants() -> None:
    pg = _pg()
    for setting in (
        "core.fsmonitor=false", "core.hooksPath=/dev/null", "commit.gpgSign=false",
        "tag.gpgSign=false", "log.showSignature=false", "merge.verifySignatures=false",
        "gpg.program=false", "gpg.ssh.program=false", "gpg.x509.program=false",
        "submodule.recurse=false", "diff.ignoreSubmodules=dirty", "diff.submodule=short",
        "status.submoduleSummary=false", "gc.auto=0", "maintenance.auto=false",
        "core.alternateRefsCommand=", "merge.default=text", "core.editor=:",
        "sequence.editor=:", "core.pager=cat",
    ):
        assert setting in pg.FIXED_SETTINGS
    for setting in pg.REBASE_SETTINGS:
        assert setting in pg.FIXED_SETTINGS
    assert "rebase.instructionFormat=%s" in pg.REBASE_SETTINGS
    assert pg.FIXED_ENVIRONMENT["GIT_ALLOW_PROTOCOL"] == "none"
    assert {"fetch", "pull", "push", "clone", "ls-remote", "credential", "submodule"} <= set(
        pg.NETWORK_COMMANDS
    )


# ---------------------------------------------------------------------------
# Review fix pass (13 October 2026): names that are not UTF-8, a name read
# that does not succeed, and abbreviated rebase options


def _bytes_named_repo(lab: Lab, name: bytes, kind: str) -> Path:
    """A repository whose own settings name a program under ``name``, which
    need not be valid UTF-8. ``kind`` is ``filter`` or ``merge``."""
    repo = lab.base_repo("repo")
    g = lambda *a: lab.setup_git(repo, *a)  # noqa: E731
    attribute = b"filter" if kind == "filter" else b"merge"
    (repo / ".gitattributes").write_bytes(b"*.txt " + attribute + b"=" + name + b"\n")
    if kind == "merge":
        (repo / "a.txt").write_text("1\n2\n3\n4\n5\n6\n")
        g("add", "-A")
        g("commit", "-qm", "six")
        g("checkout", "-q", "-b", "side")
        (repo / "a.txt").write_text("1\n2\n3\n4\n5\nsix\n")
        g("commit", "-qam", "side")
        g("checkout", "-q", "main")
        (repo / "a.txt").write_text("one\n2\n3\n4\n5\n6\n")
        g("commit", "-qam", "main")
        section = b'[merge "' + name + b'"]\n\tdriver = ' + lab.path("mergedrv").encode() + b" %O %A %B\n"
    else:
        g("add", "-A")
        g("commit", "-qm", "attributes")
        section = b'[filter "' + name + b'"]\n\tclean = ' + lab.path("clean").encode() + b"\n"
        lab.same_size_edit(repo / "a.txt")
    with open(repo / ".git" / "config", "ab") as config:
        config.write(section)
    return repo


NON_UTF8 = {
    "filter-ff-fe": (b"\xff\xfe", "filter"),
    "filter-latin1": (b"caf\xe9", "filter"),
    "merge-driver-ff-fe": (b"\xff\xfe", "merge"),
}


@pytest.mark.parametrize("case", list(NON_UTF8), ids=list(NON_UTF8))
def test_run_git_switches_off_names_that_are_not_utf8(lab: Lab, case: str) -> None:
    name, kind = NON_UTF8[case]
    argv = (["status", "--porcelain"] if kind == "filter"
            else ["merge", "--no-ff", "-q", "-m", "m", "side"])
    control = _bytes_named_repo(lab, name, kind)
    lab.fired()
    lab.plain(control, *argv)
    assert Lab.names(lab.fired()) == (["clean"] if kind == "filter" else ["mergedrv"])
    shutil.rmtree(control)
    repo = _bytes_named_repo(lab, name, kind)
    lab.fired()
    done = _pg().run_git(argv, cwd=repo)
    assert lab.fired() == []
    assert done.returncode == 0, done.stderr


def _failing_name_read(pg, how: str, unless: str = ""):
    """Make the helper's setting-name read fail in one way; other reads run
    (and so does a name read whose command line mentions ``unless``)."""
    real = pg._spawn

    def spawn(argv, cwd, env, timeout):
        if "config" in argv and "--list" in argv and not (unless and unless in argv):
            if how == "cannot start":
                return None
            if how == "exits non-zero":
                return 128, b""
            if how == "times out":
                raise pg._ReadTimedOut()
            if how == "empty in a repository":
                return 0, b""
        return real(argv, cwd, env, timeout)

    return spawn


READ_FAILURES = ["cannot start", "exits non-zero", "times out", "empty in a repository"]


@pytest.mark.parametrize("how", READ_FAILURES)
def test_run_git_refuses_when_the_setting_names_cannot_be_read(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    pg = _pg()
    repo = lab.marked_repo("repo")
    monkeypatch.setattr(pg, "_spawn", _failing_name_read(pg, how))
    lab.fired()
    done = pg.run_git(["status", "--porcelain"], cwd=repo)
    assert lab.fired() == []
    assert done.returncode != 0
    assert done.stderr == pg.SETTINGS_REFUSAL.format(command="status")


@pytest.mark.parametrize("how", READ_FAILURES)
def test_run_git_worktree_add_refuses_when_the_setting_names_cannot_be_read(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    pg = _pg()
    repo = _include_repo("gitdir:**/worktrees/**", "smudge_wt")(lab)
    destination = lab.root / "new-worktree"
    # Only the source repository's read fails; the new worktree's would work.
    monkeypatch.setattr(pg, "_spawn", _failing_name_read(pg, how, unless=str(destination)))
    lab.fired()
    done = pg.run_git(["worktree", "add", "-q", str(destination), "-b", "autobuild/x"], cwd=repo)
    assert lab.fired() == []
    assert done.returncode != 0
    assert not destination.exists()


def test_run_git_names_outside_a_repository_are_not_a_refusal(lab: Lab) -> None:
    folder = lab.root / "not-a-repository"
    folder.mkdir()
    done = _pg().run_git(["rev-parse", "--is-inside-work-tree"], cwd=folder)
    assert done.returncode != 0
    assert done.stderr == "git rev-parse stopped with exit code 128."


@pytest.mark.parametrize(
    "option", ["--exe=true", "--interac", "--rebase-m", "-r", "--update-refs", "--empty=keep"]
)
def test_run_git_refuses_rebase_options_the_factory_does_not_use(lab: Lab, option: str) -> None:
    pg = _pg()
    repo = _two_branches(lab, with_merge=False)
    done = pg.run_git(["rebase", option, "main"], cwd=repo)
    assert done.returncode != 0
    assert done.stderr == pg.REBASE_REFUSAL


# ---------------------------------------------------------------------------
# Forge's additions: three ways to run, the caller allow-list, commit-tree,
# grep -O


def test_run_git_refuses_caller_settings_outside_the_allow_list(lab: Lab) -> None:
    pg = _pg()
    repo = lab.marked_repo("repo")
    for setting in ("core.hooksPath=/tmp", "core.fsmonitor=true",
                    f"filter.mark.clean={lab.path('clean')}", "protocol.ext.allow=always",
                    "include.path=/tmp/x", "core.sshCommand=ssh"):
        with pytest.raises(ValueError):
            pg.run_git(["-c", setting, "status", "--porcelain"], cwd=repo)
    assert lab.fired() == []


def test_run_git_takes_the_allowed_caller_settings(lab: Lab) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    lab.setup_git(repo, "config", "--unset", "user.name")
    lab.setup_git(repo, "config", "--unset", "user.email")
    done = pg.run_git(
        ["-c", "safe.directory=*", "-c", "core.quotepath=false", "-c", "user.name=Repair",
         "-c", "user.email=repair@example.invalid", "commit", "-q", "--allow-empty", "-m", "r"],
        cwd=repo,
    )
    assert done.returncode == 0, done.stderr
    assert lab.setup_git(repo, "log", "-1", "--format=%an").strip() == "Repair"
    assert set(pg.CALLER_SETTINGS) == {"safe.directory", "core.quotepath", "user.name",
                                       "user.email"}


def test_run_git_async_runs_no_project_program(lab: Lab) -> None:
    import asyncio

    pg = _pg()
    repo = lab.marked_repo("repo")
    lab.fired()
    done = asyncio.run(pg.run_git_async(["status", "--porcelain"], cwd=repo, timeout=30))
    assert done.returncode == 0
    assert "a.txt" in done.stdout
    assert lab.fired() == []
    failed = asyncio.run(pg.run_git_async(["rev-parse", "--verify", "IGNORE-ME"], cwd=repo))
    assert failed.stderr == f"git rev-parse stopped with exit code {failed.returncode}."


def test_open_git_streams_without_project_programs(lab: Lab) -> None:
    import io
    import tarfile

    pg = _pg()
    repo = lab.marked_repo("repo", edits=False)
    lab.fired()
    process = pg.open_git(["archive", "--format=tar", "HEAD"], cwd=repo)
    data = process.stdout.read()
    assert process.wait() == 0
    names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    assert "a.txt" in names
    batch = pg.open_git(["cat-file", "--batch"], cwd=repo, stdin=subprocess.PIPE)
    out, _ = batch.communicate(b"HEAD:b.txt\n")
    assert out.split(b"\n", 1)[1].startswith(b"two")
    assert lab.fired() == []
    with pytest.raises(pg.GitRefused) as refused:
        pg.open_git(["fetch", "origin"], cwd=repo)
    assert str(refused.value) == pg.NETWORK_REFUSAL
    assert isinstance(refused.value, OSError)


def test_commit_tree_is_never_signed(lab: Lab) -> None:
    pg = _pg()
    repo = lab.marked_repo("repo", edits=False)
    lab.fired()
    lab.plain(repo, "commit-tree", "-S", "HEAD^{tree}", "-m", "signed")
    assert "gpg" in Lab.names(lab.fired())  # positive control: -S runs the program
    done = pg.run_git(["commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "preview"], cwd=repo)
    assert done.returncode == 0, done.stderr
    assert lab.fired() == []
    assert "--no-gpg-sign" in pg.FORGE_COMMAND_OPTIONS["commit-tree"]


@pytest.mark.parametrize(
    "option", ["-O", "-Ocat", "--open-files-in-pager=cat", "--open-files", "-nO"]
)
def test_run_git_refuses_grep_open_files_in_pager(lab: Lab, option: str) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    done = pg.run_git(["grep", option, "-e", "one"], cwd=repo)
    assert done.returncode != 0
    assert done.stderr == pg.PAGER_REFUSAL


def test_run_git_grep_still_searches(lab: Lab) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    done = pg.run_git(["grep", "-I", "-n", "-e", "-O"], cwd=repo)
    assert done.returncode == 1  # a pattern that happens to look like -O is searched for
    found = pg.run_git(["grep", "-I", "-n", "-e", "one"], cwd=repo)
    assert found.returncode == 0 and "a.txt:1:one" in found.stdout


# ---------------------------------------------------------------------------
# Coach fix pass (14 October 2026): archive is an uncompressed tar only


def test_planted_tar_filter_command_fires_with_plain_git(lab: Lab) -> None:
    """Positive control: ``tar.<format>.command`` names a program that
    ``git archive --format=<format>`` runs."""
    repo = lab.base_repo("repo")
    lab.setup_git(repo, "config", "tar.tgz.command", lab.path("clean"))
    lab.fired()
    lab.plain(repo, "archive", "--format=tgz", "HEAD")
    fired = lab.fired()
    assert "clean" in Lab.names(fired) and Lab.saw_secret(fired)


@pytest.mark.parametrize(
    "options",
    [["--format=tgz"], ["--format=tar.gz"], ["--format=zip"], ["--format", "tgz"],
     ["-o", "out.tgz"], ["--output=out.tar.gz"], ["--worktree-attributes", "--format=tar"],
     ["--exec=x", "--format=tar"]],
    ids=["tgz", "tar.gz", "zip", "two-word format", "-o", "--output", "worktree attributes",
         "exec"],
)
def test_run_git_archive_is_an_uncompressed_tar_only(lab: Lab, options: List[str]) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    lab.setup_git(repo, "config", "tar.tgz.command", lab.path("clean"))
    lab.setup_git(repo, "config", "tar.tar.gz.command", lab.path("clean"))
    lab.fired()
    done = pg.run_git(["archive", *options, "HEAD"], cwd=repo, text=False)
    assert done.returncode != 0
    assert done.stderr.decode() == pg.ARCHIVE_REFUSAL
    assert lab.fired() == []
    assert not (repo / "out.tgz").exists() and not (repo / "out.tar.gz").exists()
    with pytest.raises(pg.GitRefused):
        pg.open_git(["archive", *options, "HEAD"], cwd=repo)


def test_run_git_archive_as_a_tar_still_works(lab: Lab) -> None:
    pg = _pg()
    repo = lab.base_repo("repo")
    done = pg.run_git(["archive", "--format=tar", "--prefix=p/", "HEAD"], cwd=repo, text=False)
    assert done.returncode == 0
    assert b"p/a.txt" in done.stdout
