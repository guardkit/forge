"""Real-Git controls for retained autobuild worktree ownership and cleanup."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.subagents.autobuild_worktree_lifecycle import (
    inspect_autobuild_worktree,
    retire_autobuild_worktree,
)

BUILD_ID = "build-FEAT-KEEP-20260918"
FEATURE = "FEAT-KEEP"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=tests",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_with_nested(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main", "-q")
    (repo / "README").write_text("main\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "branch", f"autobuild/{FEATURE}")
    base = tmp_path / "autobuild-worktrees"
    outer = base / BUILD_ID
    _git(repo, "worktree", "add", "--detach", str(outer), "main")
    inner = outer / ".guardkit" / "worktrees" / "TASK-KEEP-001"
    inner.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", str(inner), f"autobuild/{FEATURE}")
    return repo, base, outer, inner


def test_offer_identity_preserves_then_successfully_retires_exact_nested_tree(
    tmp_path: Path,
) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    (inner / "untracked-evidence.txt").write_text("keep through offer\n")

    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )

    assert offered["ok"] is True
    assert [Path(r["path"]) for r in offered["nested_registrations"]] == [inner]
    assert outer.is_dir() and inner.is_dir()
    offered["cleanup_registrations"] = offered["nested_registrations"]
    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=outer,
        expected=offered,
    )
    assert result["status"] == "removed", result
    assert not outer.exists() and not inner.exists()
    assert _git(repo, "rev-parse", f"autobuild/{FEATURE}")


def test_unrelated_registered_guardkit_tree_survives_candidate_cleanup(
    tmp_path: Path,
) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    _git(repo, "branch", "unrelated")
    unrelated = outer / ".guardkit/worktrees/TASK-OTHER-001"
    _git(repo, "worktree", "add", str(unrelated), "unrelated")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = [
        row for row in offered["nested_registrations"] if Path(row["path"]) == inner
    ]

    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=outer,
        expected=offered,
    )

    assert result["status"] == "kept"
    assert not inner.exists()
    assert outer.is_dir() and unrelated.is_dir()


def test_changed_untracked_content_after_offer_fails_closed(tmp_path: Path) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    note = inner / "user-note.txt"
    note.write_text("present at offer\n")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    note.write_text("changed after offer\n")

    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=outer,
        expected=offered,
    )

    assert result["status"] == "kept"
    assert "identity no longer matches" in result["detail"]
    assert outer.is_dir() and inner.is_dir()


def test_changed_already_dirty_tracked_content_after_offer_fails_closed(
    tmp_path: Path,
) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    readme = inner / "README"
    readme.write_text("dirty at offer\n")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    readme.write_text("changed after offer\n")

    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=outer,
        expected=offered,
    )

    assert result["status"] == "kept"
    assert readme.read_text() == "changed after offer\n"


def test_stale_or_unowned_path_is_never_removed(tmp_path: Path) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("keep\n")

    result = retire_autobuild_worktree(
        repo=repo,
        base=base,
        build_id=BUILD_ID,
        path=unrelated,
        expected=offered,
    )

    assert result["status"] == "kept"
    assert unrelated.is_dir() and (unrelated / "keep").is_file()
    assert outer.is_dir() and inner.is_dir()


def test_unrelated_nested_registration_blocks_outer_cleanup(tmp_path: Path) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    _git(repo, "branch", "unrelated")
    unexpected = outer / "somewhere-else" / "tree"
    unexpected.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", str(unexpected), "unrelated")

    identity = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )

    assert identity["ok"] is False
    assert str(unexpected) in identity["unexpected_registrations"]
    assert outer.is_dir() and inner.is_dir() and unexpected.is_dir()



@pytest.mark.parametrize("location", ["outer", "inner"])
@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_git_invisible_special_entry_after_offer_preserves_owned_tree(
    tmp_path: Path, location: str, kind: str
) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    assert offered["ok"] is True
    offered["cleanup_registrations"] = offered["nested_registrations"]
    special = (outer if location == "outer" else inner) / "special"
    if kind == "fifo":
        os.mkfifo(special)
    else:
        # /proc/self/fd keeps the AF_UNIX address below its 108-byte limit.
        descriptor = os.open(special.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with socket.socket(socket.AF_UNIX) as endpoint:
                endpoint.bind(f"/proc/self/fd/{descriptor}/special")
        finally:
            os.close(descriptor)
    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )
    assert result["status"] == "kept", result
    assert "unsupported filesystem entry" in result["detail"]
    assert outer.is_dir() and inner.is_dir() and special.exists()
    assert inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )["ok"] is False


def test_a_prepared_builds_own_branch_goes_with_its_retired_worktree(
    tmp_path: Path,
) -> None:
    """A prepared feature's build makes ``forge/source/<build_id>`` at the
    admitted commit (4 October 2026); retiring its worktree deletes it. A
    build without one reports exactly what it always did."""
    repo, base, outer, _inner = _repo_with_nested(tmp_path)
    _git(repo, "branch", f"forge/source/{BUILD_ID}", "main")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert result["status"] == "removed", result
    assert result["source_branch_removed"] == f"forge/source/{BUILD_ID}"
    assert _git(repo, "branch", "--list", f"forge/source/{BUILD_ID}") == ""


def test_a_failed_git_command_reports_only_the_fixed_sentence(
    tmp_path: Path,
) -> None:
    """Git runs through the one git helper (14 October 2026). A failed command
    hands back the helper's fixed sentence, never git's own error words, since
    the text goes into the cleanup receipt and the logs; a command that works
    still hands back git's standard output as data."""
    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    repo, _base, _outer, _inner = _repo_with_nested(tmp_path)

    code, said = lifecycle._git(repo, "branch", "-D", "no-such-branch")
    assert code != 0
    assert said == f"git branch stopped with exit code {code}.".encode()

    code, head = lifecycle._git(repo, "rev-parse", "HEAD")
    assert code == 0
    assert head.strip() == _git(repo, "rev-parse", "HEAD").encode()


def test_a_failed_removal_puts_only_the_fixed_sentence_in_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    repo, base, outer, _inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    real = lifecycle.run_git

    def _remove_fails(args, **kwargs):
        if "remove" in args:
            # A real failing git command, so the sentence is the helper's own.
            return real(["-C", str(repo), "branch", "-D", "no-such-branch"], **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(lifecycle, "run_git", _remove_fails)
    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )
    assert result["status"] == "kept", result
    assert result["detail"].endswith(": git branch stopped with exit code 1."), result
    assert "error:" not in result["detail"]


# ---------------------------------------------------------------------------
# READ-ONLY FOLDERS INSIDE A WORKTREE (15 October 2026, FEAT-6AC9). Tools such
# as a language's module cache write their caches with folders the owner
# cannot write. Git cannot delete inside such a folder, so `git worktree
# remove --force` deleted the task's working folder, then stopped on Git's own
# record folder for it (which held GuardKit's scratch area) with exit 255 and
# left a "prunable" registration behind. The retirement now makes the
# offer-pinned trees owner-writable first, never through a symbolic link.
# Nothing is ever cleared on a retry: a leftover registration stays, and so
# does its tree ("kept"), which holds nothing up.
# ---------------------------------------------------------------------------


def _record_folder(repo: Path, worktree: Path) -> Path:
    for record in (repo / ".git" / "worktrees").iterdir():
        pointer = (record / "gitdir").read_text().strip()
        if Path(pointer).resolve() == (worktree / ".git").resolve():
            return record
    raise AssertionError(f"no record folder for {worktree}")


def _read_only_cache(top: Path) -> list[Path]:
    """A cache in the shape module caches take: read-only folders, all the
    way down, with read-only files in them."""
    deepest = top / "cache" / "mod" / "example.invalid" / "pkg@v1.0.0" / "sub"
    deepest.mkdir(parents=True)
    (deepest / "file.txt").write_text("cached\n")
    (deepest / "file.txt").chmod(0o444)
    folders = []
    probe = deepest
    while probe != top:
        folders.append(probe)
        probe = probe.parent
    for folder in folders:  # deepest first, so each can still be reached
        folder.chmod(0o555)
    return folders


@pytest.fixture
def _writable_again(tmp_path: Path):
    """Whatever a failing test leaves read-only is made writable again, so the
    temporary folder can always be cleaned away."""
    yield
    for root, dirs, _files in os.walk(tmp_path):
        for name in dirs:
            full = Path(root) / name
            if not full.is_symlink():
                full.chmod(0o755)


def test_read_only_folders_in_a_nested_worktree_and_its_record_are_removed(
    tmp_path: Path, _writable_again: None
) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    _read_only_cache(inner)
    _read_only_cache(_record_folder(repo, inner) / "guardkit-scratch")
    _read_only_cache(_record_folder(repo, outer) / "guardkit-scratch")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    assert offered["ok"] is True, offered
    offered["cleanup_registrations"] = offered["nested_registrations"]

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert result["status"] == "removed", result
    assert not outer.exists() and not inner.exists()
    assert not (repo / ".git" / "worktrees").exists() or not list(
        (repo / ".git" / "worktrees").iterdir()
    )
    assert "prunable" not in _git(repo, "worktree", "list", "--porcelain")


def _todays_state(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, dict]:
    """Today's state, made the way it was made live: the earlier attempt's
    `git worktree remove --force` deleted the working folder and stopped on the
    read-only record folder, leaving a prunable registration."""
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    record = _record_folder(repo, inner)
    _read_only_cache(record / "guardkit-scratch")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    assert offered["ok"] is True, offered
    offered["cleanup_registrations"] = offered["nested_registrations"]
    failed = subprocess.run(
        ["git", "worktree", "remove", "--force", str(inner)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    assert not inner.exists() and record.exists()
    assert "prunable" in _git(repo, "worktree", "list", "--porcelain")
    return repo, base, outer, inner, record, offered


@pytest.mark.parametrize("left_by", ["git-stopped-part-way", "folder-deleted"])
def test_a_retry_clears_nothing_and_keeps_the_tree(
    tmp_path: Path, _writable_again: None, left_by: str
) -> None:
    """A registration whose working folder is gone, whether Git's own failed
    removal left it half-deleted or somebody deleted only the folder, is
    never cleared: its record folder stays, Git still lists it, and the
    tree is kept with the reason."""
    import shutil

    if left_by == "git-stopped-part-way":
        repo, base, outer, inner, record, offered = _todays_state(tmp_path)
    else:
        repo, base, outer, inner = _repo_with_nested(tmp_path)
        record = _record_folder(repo, inner)
        offered = inspect_autobuild_worktree(
            repo=repo, base=base, build_id=BUILD_ID, path=outer
        )
        assert offered["ok"] is True, offered
        offered["cleanup_registrations"] = offered["nested_registrations"]
        shutil.rmtree(inner)

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert result["status"] == "kept", result
    assert result["detail"]
    assert "cleared_registrations" not in result
    assert record.exists() and outer.is_dir()
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert str(inner) in listed and "prunable" in listed


def test_on_retry_the_outer_trees_uncommitted_changes_are_kept(
    tmp_path: Path, _writable_again: None
) -> None:
    """The coach's reproduction: on retry, an edited tracked file and a new
    file in the outer tree must survive."""
    repo, base, outer, inner, _record, offered = _todays_state(tmp_path)
    (outer / "README").write_text("edited after the offer\n")
    (outer / "notes.txt").write_text("somebody's notes\n")

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert result["status"] == "kept", result
    assert (outer / "README").read_text() == "edited after the offer\n"
    assert (outer / "notes.txt").read_text() == "somebody's notes\n"
    assert str(outer) in _git(repo, "worktree", "list", "--porcelain")


def test_a_gone_folder_that_was_not_offered_is_not_cleared(
    tmp_path: Path, _writable_again: None
) -> None:
    """Only an offer-pinned path is ever cleared: a prunable registration the
    offer did not name keeps its record folder, and the tree is kept."""
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    _git(repo, "branch", "unrelated")
    unrelated = outer / ".guardkit/worktrees/TASK-OTHER-001"
    _git(repo, "worktree", "add", str(unrelated), "unrelated")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = [
        row for row in offered["nested_registrations"] if Path(row["path"]) == inner
    ]
    other_record = _record_folder(repo, unrelated)
    import shutil

    shutil.rmtree(unrelated)

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert result["status"] == "kept", result
    assert other_record.exists() and outer.is_dir() and inner.is_dir()
    assert result.get("cleared_registrations", []) == []


def test_a_symbolic_link_to_a_read_only_folder_outside_is_not_followed(
    tmp_path: Path, _writable_again: None
) -> None:
    repo, base, outer, inner = _repo_with_nested(tmp_path)
    outside = tmp_path / "outside"
    outside_folders = _read_only_cache(outside)
    outside.chmod(0o555)
    before = {folder: folder.stat().st_mode for folder in [outside, *outside_folders]}
    # Git refuses to pin a dirty link that leaves the tree, so the link sits
    # in an ignored folder, where caches usually are; one in Git's own record
    # folder for the worktree too.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("ignored/\n")
    (inner / "ignored").mkdir()
    (inner / "ignored" / "link-out").symlink_to(outside, target_is_directory=True)
    (_record_folder(repo, inner) / "link-out").symlink_to(
        outside, target_is_directory=True
    )
    _read_only_cache(inner / "own")
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    assert offered["ok"] is True, offered
    offered["cleanup_registrations"] = offered["nested_registrations"]

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert result["status"] == "removed", result
    assert {folder: folder.stat().st_mode for folder in before} == before
    assert (outside_folders[0] / "file.txt").read_text() == "cached\n"


def test_a_folder_swapped_for_a_link_after_listing_is_not_followed(
    tmp_path: Path, _writable_again: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, R2: a folder replaced by a link to a read-only folder
    outside, between being listed and being changed, is never followed: each
    folder is opened without following links and changed by its handle."""
    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    repo, base, outer, inner = _repo_with_nested(tmp_path)
    outside = tmp_path / "outside"
    outside_folders = _read_only_cache(outside)
    outside.chmod(0o555)
    before = {folder: folder.stat().st_mode for folder in [outside, *outside_folders]}
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("ignored/\n")
    swap = inner / "ignored" / "swap-me"
    swap.mkdir(parents=True)
    swap.chmod(0o555)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    assert offered["ok"] is True, offered
    offered["cleanup_registrations"] = offered["nested_registrations"]
    real = lifecycle._subfolders
    swapped: list[bool] = []

    def listing_then_swap(descriptor: int) -> list[str]:
        names = real(descriptor)
        if "swap-me" in names and not swapped:
            swap.rmdir()
            swap.symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return names

    monkeypatch.setattr(lifecycle, "_subfolders", listing_then_swap)

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert swapped == [True]
    assert {folder: folder.stat().st_mode for folder in before} == before
    assert (outside_folders[0] / "file.txt").read_text() == "cached\n"
    assert result["status"] == "removed", result


def test_a_walk_redirected_to_folders_owned_by_someone_else_changes_nothing(
    tmp_path: Path, _writable_again: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, R3: a folder above the tree can be a link, and opening the
    tree's top follows it. The walk changes only folders the cleaning user
    owns, so a walk redirected to folders owned by anyone else changes
    nothing there. Another owner is simulated: tests cannot chown."""
    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    outside = tmp_path / "outside"
    outside_folders = _read_only_cache(outside)
    outside.chmod(0o555)
    before = {folder: folder.stat().st_mode for folder in [outside, *outside_folders]}
    owned_elsewhere = {
        (folder.stat().st_dev, folder.stat().st_ino)
        for folder in [outside, *outside_folders]
    }
    redirected = tmp_path / "worktrees-link"
    redirected.symlink_to(tmp_path, target_is_directory=True)
    real_fstat = os.fstat

    def someone_elses(descriptor: int) -> os.stat_result:
        found = real_fstat(descriptor)
        if (found.st_dev, found.st_ino) in owned_elsewhere:
            fields = list(found)
            fields[4] = os.geteuid() + 1  # st_uid
            return os.stat_result(fields)
        return found

    monkeypatch.setattr(lifecycle.os, "fstat", someone_elses)
    lifecycle._make_owner_writable(redirected / "outside")
    lifecycle._make_owner_writable(redirected / "outside" / "cache")

    assert {folder: folder.stat().st_mode for folder in before} == before


def test_a_folder_above_the_tree_swapped_for_a_link_to_the_users_own_folders_is_not_followed(
    tmp_path: Path, _writable_again: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, R3, round 3: a folder ABOVE the tree replaced by a link
    to a tree the same user owns. The walk's top is reached one name at a
    time from / without following any link, so the walk does not start,
    nothing outside changes, and the retirement says "kept"."""
    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    repo, base, outer, inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    assert offered["ok"] is True, offered
    offered["cleanup_registrations"] = offered["nested_registrations"]
    # The same user's own tree, laid out like .guardkit, read-only throughout.
    outside = tmp_path / "outside-guardkit"
    mirror = outside / "worktrees" / inner.name
    _read_only_cache(mirror)
    for folder in (mirror, mirror.parent, outside):
        folder.chmod(0o555)
    folders = [Path(root) for root, _dirs, _files in os.walk(outside)]
    before = {folder: folder.stat().st_mode for folder in folders}
    files = {
        Path(root) / name: (Path(root) / name).read_bytes()
        for root, _dirs, names in os.walk(outside)
        for name in names
    }
    real_inspect = lifecycle.inspect_autobuild_worktree

    def inspect_then_swap(**kwargs: Any) -> dict[str, Any]:
        answer = real_inspect(**kwargs)
        guardkit = outer / ".guardkit"
        guardkit.rename(outer / ".guardkit-moved")
        guardkit.symlink_to(outside, target_is_directory=True)
        return answer

    monkeypatch.setattr(lifecycle, "inspect_autobuild_worktree", inspect_then_swap)

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert result["status"] == "kept", result
    assert {folder: folder.stat().st_mode for folder in folders} == before
    assert {
        Path(root) / name: (Path(root) / name).read_bytes()
        for root, _dirs, names in os.walk(outside)
        for name in names
    } == files


def test_gits_record_folders_swapped_for_a_link_to_the_users_own_copy_are_not_followed(
    tmp_path: Path, _writable_again: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, R3, round 4: ``.git/worktrees`` replaced by a link to a
    copy the same user owns, record folders and back pointers and all. The
    record folder's path is built by text alone and reached from / without
    following any link, so the copy is never touched and the retirement
    says "kept"."""
    import shutil

    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    repo, base, outer, inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    assert offered["ok"] is True, offered
    offered["cleanup_registrations"] = offered["nested_registrations"]
    records = repo / ".git" / "worktrees"
    outside = tmp_path / "outside-records"
    shutil.copytree(records, outside, symlinks=True)
    _read_only_cache(outside / inner.name / "guardkit-scratch")
    for root, dirs, _files in os.walk(outside, topdown=False):
        for name in dirs:
            (Path(root) / name).chmod(0o555)
    outside.chmod(0o555)
    folders = [Path(root) for root, _dirs, _files in os.walk(outside)]
    before = {folder: folder.stat().st_mode for folder in folders}
    files = {
        Path(root) / name: (Path(root) / name).read_bytes()
        for root, _dirs, names in os.walk(outside)
        for name in names
    }
    real_inspect = lifecycle.inspect_autobuild_worktree

    def inspect_then_swap(**kwargs: Any) -> dict[str, Any]:
        answer = real_inspect(**kwargs)
        records.rename(repo / ".git" / "worktrees-moved")
        records.symlink_to(outside, target_is_directory=True)
        return answer

    monkeypatch.setattr(lifecycle, "inspect_autobuild_worktree", inspect_then_swap)

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    assert result["status"] == "kept", result
    assert {folder: folder.stat().st_mode for folder in folders} == before
    assert {
        Path(root) / name: (Path(root) / name).read_bytes()
        for root, _dirs, names in os.walk(outside)
        for name in names
    } == files


def test_gits_record_folders_swapped_for_a_link_to_a_writable_copy_are_not_removed(
    tmp_path: Path, _writable_again: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review, R3, round 5: the same swap as above, but the outside copy
    is writable, so only refusing to call Git at all keeps it intact."""
    import shutil

    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    repo, base, outer, inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    assert offered["ok"] is True, offered
    offered["cleanup_registrations"] = offered["nested_registrations"]
    records = repo / ".git" / "worktrees"
    outside = tmp_path / "outside-records"
    shutil.copytree(records, outside, symlinks=True)
    files = {
        Path(root) / name: (Path(root) / name).read_bytes()
        for root, _dirs, names in os.walk(outside)
        for name in names
    }
    real_inspect = lifecycle.inspect_autobuild_worktree

    def inspect_then_swap(**kwargs: Any) -> dict[str, Any]:
        answer = real_inspect(**kwargs)
        records.rename(repo / ".git" / "worktrees-moved")
        records.symlink_to(outside, target_is_directory=True)
        return answer

    monkeypatch.setattr(lifecycle, "inspect_autobuild_worktree", inspect_then_swap)

    result = retire_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer, expected=offered
    )

    # Refused before Git is asked: Git 2.43 happens to refuse too (exit 128),
    # but the boundary must not depend on that.
    assert "without following a link" in result["detail"], result
    assert result["status"] == "kept", result
    assert {
        Path(root) / name: (Path(root) / name).read_bytes()
        for root, _dirs, names in os.walk(outside)
        for name in names
    } == files
    assert inner.exists()


# ---------------------------------------------------------------------------
# A GIT COMMAND THAT RAISED IS NOT AN ANSWER (15 October 2026, Codex R15). The
# retirement used to turn it into exit 127 and an ordinary "kept" report, and
# both venues confirmed that report. Now the report says git_raised and is
# never confirmed: in this container, nor through the sandbox's route (a 5xx).
# ---------------------------------------------------------------------------


def _remove_raises(monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    """``git worktree remove`` raises for the ``nested`` tree or the ``outer``
    one, as a command can after it was sent; every other git runs for real."""
    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    real = lifecycle.run_git

    def _git(args, **kwargs):  # type: ignore[no-untyped-def]
        if "remove" in args:
            target = Path(args[-1])
            nested = ".guardkit" in target.parts
            if (which == "nested") == nested:
                raise OSError("the pipe to git worktree remove broke")
        return real(args, **kwargs)

    monkeypatch.setattr(lifecycle, "run_git", _git)


@pytest.mark.parametrize("which", ["nested", "outer"])
def test_a_removal_that_raised_is_noted_and_never_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    import asyncio

    from forge.deploy.candidate_tree import InContainerCandidateGit

    repo, base, outer, _inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    _remove_raises(monkeypatch, which)
    monkeypatch.setenv("FORGE_AUTOBUILD_WORKTREE_BASE", str(base))

    # Through this container's venue, which runs the real helper.
    retired = asyncio.run(
        InContainerCandidateGit(repo).retire_autobuild_worktree(BUILD_ID, str(outer), offered)
    )
    assert retired["status"] == "kept", retired
    assert retired["git_raised"], retired
    assert retired["confirmed"] is False


def test_an_ordinary_failed_removal_is_a_confirmed_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from forge.deploy.candidate_tree import InContainerCandidateGit
    from forge.subagents import autobuild_worktree_lifecycle as lifecycle

    repo, base, outer, _inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    real = lifecycle.run_git

    def _remove_fails(args, **kwargs):  # type: ignore[no-untyped-def]
        if "remove" in args:
            return real(["-C", str(repo), "branch", "-D", "no-such-branch"], **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(lifecycle, "run_git", _remove_fails)
    monkeypatch.setenv("FORGE_AUTOBUILD_WORKTREE_BASE", str(base))
    retired = asyncio.run(
        InContainerCandidateGit(repo).retire_autobuild_worktree(BUILD_ID, str(outer), offered)
    )
    assert retired["status"] == "kept", retired
    assert "git_raised" not in retired
    assert retired["confirmed"] is True


def test_the_sandbox_route_answers_a_raised_removal_with_a_5xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.config.models import ForgeConfig
    from forge.deploy_sidecar.service import process_git_autobuild_worktree_retire_request

    repo, base, outer, _inner = _repo_with_nested(tmp_path)
    offered = inspect_autobuild_worktree(
        repo=repo, base=base, build_id=BUILD_ID, path=outer
    )
    offered["cleanup_registrations"] = offered["nested_registrations"]
    _remove_raises(monkeypatch, "outer")
    monkeypatch.setenv("FORGE_AUTOBUILD_WORKTREE_BASE", str(base))
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
            "planning": {"target_repo_paths": {"acme/shop": str(repo)}},
        }
    )
    status, body = process_git_autobuild_worktree_retire_request(
        {"repo": "acme/shop", "build_id": BUILD_ID, "path": str(outer), "expected": offered},
        config=config,
    )
    assert status == 500, body
    assert body["git_raised"]
