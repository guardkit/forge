"""The publisher refuses to send the factory's own records or private paths.

Design: "Factory records never enter a project's commits" (14 October 2026),
tests 15 to 36. Before it sends, the publisher looks at the commits the send
would add (those reachable from the join and not from the published branch)
four ways:

a. a record path added or changed, at any depth, even one later deleted;
b. a record moved or copied to another path, judged on the final state;
c. a record's exact content saved under any other path;
d. any new file version holding one of the factory's private folder names.

When any look finds something, nothing is sent, and the answer is one plain
sentence. When a look itself fails, nothing is sent either.

NOTHING HERE CONTACTS ANYTHING REAL. The "remote" is a bare repository in the
test's own temporary folder. The private folder name is made up
(``/home/example``). Nothing names a language: the product is text files.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.network_git import NetworkResult
from forge.publisher import git_work
from forge.publisher.service import Publisher
from forge.publisher.settings import SettingsRefused, load_settings
from tests.forge.publisher.a_project_and_a_ledger import (
    FEATURE,
    MADE_UP_PRIVATE_FOLDER,
    a_request,
    every_push_the_remote_saw,
    git,
    make_the_ledger,
    settings_for,
    what_the_remote_has,
)

PRIVATE = MADE_UP_PRIVATE_FOLDER

#: Twenty lines of ordinary JSON, with no private folder name in it.
OLD_RECORD = (
    "{\n"
    + "".join(f'  "field_{n}": "value number {n}",\n' for n in range(19))
    + '  "last": true\n}\n'
)
EDITED = OLD_RECORD + '{"one more": "line"}\n'

RECORDS_KIND = "it-holds-factory-records"
PRIVATE_KIND = "it-holds-private-paths"
UNCHECKED_KIND = "the-commits-could-not-be-checked"


class World:
    """A remote, a copy with a build branch, and a real ``--no-ff`` join."""

    def __init__(self, root: Path, base: dict[str, str] | None = None) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.bare = root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", "-q", str(self.bare)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(self.bare), "config", "core.logAllRefUpdates", "true"],
            check=True, capture_output=True,
        )
        self.copy = root / "copy"
        self.copy.mkdir()
        git(self.copy, "init", "-q", "-b", "main")
        self.write({"README": "the widget shop\n", "src/app.txt": "one\n", **(base or {})})
        git(self.copy, "add", "-A")
        git(self.copy, "commit", "-q", "-m", "the beginning")
        git(self.copy, "remote", "add", "origin", str(self.bare))
        git(self.copy, "push", "-q", "origin", "main")
        self.build = f"autobuild/{FEATURE}"
        git(self.copy, "checkout", "-q", "-b", self.build)

    # -- changing things ---------------------------------------------------

    def write(self, files: dict[str, str | None], *, where: Path | None = None) -> None:
        top = where or self.copy
        for path, text in files.items():
            target = top / path
            if text is None:
                if target.is_symlink() or target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            target.write_text(text, encoding="utf-8")

    def commit(self, files: dict[str, str | None] | None = None, message: str = "work") -> str:
        self.write(files or {})
        git(self.copy, "add", "-A")
        git(self.copy, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.copy, "rev-parse", "HEAD")

    def run(self, *args: str) -> str:
        return git(self.copy, *args)

    def the_target_gains(self, files: dict[str, str | None], message: str = "target") -> None:
        git(self.copy, "checkout", "-q", "main")
        self.commit(files, message)
        git(self.copy, "push", "-q", "origin", "main")
        git(self.copy, "checkout", "-q", self.build)

    def make_this_the_target(self) -> None:
        """What the build branch has so far becomes the published target."""
        git(self.copy, "checkout", "-q", "main")
        git(self.copy, "merge", "-q", "--ff-only", self.build)
        git(self.copy, "push", "-q", "origin", "main")
        git(self.copy, "checkout", "-q", self.build)

    def the_build_takes_the_target(self, extra: dict[str, str | None] | None = None) -> None:
        if extra:
            git(self.copy, "merge", "-q", "--no-ff", "--no-commit", "main")
            self.write(extra)
            git(self.copy, "add", "-A")
            git(self.copy, "commit", "-q", "-m", "take the target, and more")
        else:
            git(self.copy, "merge", "-q", "--no-ff", "-m", "take the target", "main")

    def join(self, extra: dict[str, str | None] | None = None) -> dict[str, Any]:
        g = git(self.copy, "rev-parse", "main")
        tip = git(self.copy, "rev-parse", self.build)
        folder = self.root / "join"
        git(self.copy, "worktree", "add", "-q", "-b", f"factory-integration/{FEATURE}", str(folder), g)
        if extra:
            git(folder, "merge", "-q", "--no-ff", "--no-commit", self.build)
            self.write(extra, where=folder)
            git(folder, "add", "-A")
            git(folder, "commit", "-q", "-m", f"join {FEATURE}")
        else:
            git(folder, "merge", "-q", "--no-ff", "-m", f"join {FEATURE}", self.build)
        j = git(folder, "rev-parse", "HEAD")
        log = self.bare / "logs" / "refs" / "heads" / "main"
        if log.is_file():
            log.write_text("", encoding="utf-8")
        return {
            "bare": self.bare, "copy": self.copy, "branch": "main",
            "g": g, "tip": tip, "j": j, "join_folder": folder,
        }

    # -- publishing --------------------------------------------------------

    def publish(self, project: dict[str, Any], **settings: Any) -> Any:
        ledger = self.root / "forge.db"
        make_the_ledger(ledger, project=project)
        publisher = Publisher(settings_for(self.root, project, ledger=ledger, **settings))
        self.before = what_the_remote_has(self.bare)
        return publisher.publish(a_request(project))


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return World(tmp_path / "world")


def tracking(tmp_path: Path, files: dict[str, str]) -> World:
    return World(tmp_path / "world", base=files)


def refused(world: World, answer: Any, kind: str) -> str:
    assert answer.published is False, answer
    assert answer.refusal_kind == kind, answer
    sentence = str(answer.refusal)
    assert "Nothing was sent." in sentence
    assert what_the_remote_has(world.bare) == world.before
    assert every_push_the_remote_saw(world.bare) == []
    return sentence


def published(world: World, answer: Any, project: dict[str, Any]) -> None:
    assert answer.published is True, answer
    assert answer.contains_j is True
    assert what_the_remote_has(world.bare) == project["j"]


# ---------------------------------------------------------------------------
# Look a: a record added or changed, at any depth
# ---------------------------------------------------------------------------


class TestARecordAddedOrChanged:
    def test_15_a_record_added_then_deleted_is_refused(self, world: World) -> None:
        world.commit({".guardkit/autobuild/x.json": '{"turn": 1}\n'})
        world.commit({".guardkit/autobuild/x.json": None, "src/app.txt": "two\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert ".guardkit/autobuild/x.json" in sentence
        assert "1 of the factory's own build records" in sentence
        assert project["j"][:10] in sentence

    def test_16_a_diverged_target_with_its_own_record_is_not_a_false_refusal(
        self, world: World
    ) -> None:
        world.commit({"src/app.txt": "two\n"})
        world.the_target_gains({".guardkit/autobuild/target.json": '{"t": 1}\n'})
        project = world.join()

        published(world, world.publish(project), project)

    def test_17_a_diverged_target_still_catches_the_builds_own_record(
        self, world: World
    ) -> None:
        world.commit({"src/app.txt": "two\n", ".guardkit/qav-shadow/queue.jsonl": "{}\n"})
        world.the_target_gains({".guardkit/autobuild/target.json": '{"t": 1}\n'})
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert ".guardkit/qav-shadow/queue.jsonl" in sentence
        assert "target.json" not in sentence
        assert "1 of the factory's own build records" in sentence

    def test_18_a_record_the_join_itself_introduces_is_refused(self, world: World) -> None:
        world.commit({"src/app.txt": "two\n"})
        project = world.join(extra={".guardkit/memory-query-log.jsonl": '{"q": 1}\n'})

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert ".guardkit/memory-query-log.jsonl" in sentence

    def test_19_a_build_that_merged_an_updated_target_is_published(
        self, world: World
    ) -> None:
        world.commit({"src/app.txt": "two\n"})
        world.the_target_gains({".guardkit/autobuild/target.json": '{"t": 1}\n'})
        world.the_build_takes_the_target()
        world.commit({"src/more.txt": "more\n"})
        project = world.join()

        published(world, world.publish(project), project)

    def test_22_a_changed_tracked_record_is_refused(self, tmp_path: Path) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({".guardkit/autobuild/old.json": EDITED, "src/app.txt": "two\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert ".guardkit/autobuild/old.json" in sentence

    def test_22_a_nested_build_folder_committed_as_an_embedded_repository_is_refused(
        self, world: World
    ) -> None:
        world.run(
            "update-index", "--add", "--cacheinfo",
            f"160000,{'1' * 40},.guardkit/worktrees/FEAT-1",
        )
        world.write({"src/app.txt": "two\n"})
        world.run("add", "src/app.txt")
        world.run("commit", "-q", "-m", "an inner build folder")
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert ".guardkit/worktrees/FEAT-1" in sentence


# ---------------------------------------------------------------------------
# Look b: a record moved or copied out, judged on the final state
# ---------------------------------------------------------------------------


class TestARecordMovedOrCopiedOut:
    @pytest.mark.parametrize("shape", ["git mv", "copy left beside its source"])
    def test_20_a_lightly_edited_record_moved_out_is_refused(
        self, tmp_path: Path, shape: str
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        if shape == "git mv":
            (world.copy / "notes").mkdir()
            world.run("mv", ".guardkit/autobuild/old.json", "notes/old.json")
        world.commit({"notes/old.json": EDITED, "src/app.txt": "two\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert "notes/old.json" in sentence

    def test_20_control_deleting_the_record_is_published(self, tmp_path: Path) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({".guardkit/autobuild/old.json": None, "src/app.txt": "two\n"})
        project = world.join()

        published(world, world.publish(project), project)

    def test_20_control_a_product_file_copied_from_another_is_published(
        self, tmp_path: Path
    ) -> None:
        world = tracking(
            tmp_path,
            {".guardkit/autobuild/old.json": OLD_RECORD, "src/long.txt": OLD_RECORD + "x\n"},
        )
        world.commit({"src/copy.txt": OLD_RECORD + "x\ny\n"})
        project = world.join()

        published(world, world.publish(project), project)

    def test_28_a_copy_the_join_itself_brings_in_is_refused(self, tmp_path: Path) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({"src/app.txt": "two\n"})
        project = world.join(extra={"notes/copy.json": EDITED})

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert "notes/copy.json" in sentence

    def test_28_a_copy_an_earlier_unpublished_merge_brings_in_is_refused(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({"src/app.txt": "two\n"})
        world.the_target_gains({"src/target.txt": "target\n"})
        world.the_build_takes_the_target(extra={"notes/copy.json": EDITED})
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert "notes/copy.json" in sentence

    def test_28_a_plain_commit_that_moves_it_with_git_mv_is_refused(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        (world.copy / "docs").mkdir()
        world.run("mv", ".guardkit/autobuild/old.json", "docs/old.json")
        world.run("commit", "-q", "-m", "moved")
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert "docs/old.json" in sentence

    def test_29_out_of_scope_by_design_an_edited_copy_gone_by_the_join_is_published(
        self, tmp_path: Path
    ) -> None:
        """A disguised record with no private text, gone before the join.

        Out of scope by design (the design's goal, round 5): catching it means
        following git's history rules for moves and copies through every
        commit, which five review rounds showed keeps finding new corners.
        """
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({"notes/old.json": EDITED})
        world.commit({"notes/old.json": None, "src/app.txt": "two\n"})
        project = world.join()

        published(world, world.publish(project), project)


# ---------------------------------------------------------------------------
# Look c: a record's exact content under another path
# ---------------------------------------------------------------------------


class TestRecordContentElsewhere:
    def test_21_a_tracked_record_copied_byte_for_byte_is_refused(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({"docs/copy.json": OLD_RECORD})
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert "docs/copy.json" in sentence

    def test_32_a_short_identical_copy_gone_by_the_join_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/short.json": "{}\n"})
        world.commit({"notes/copy.json": "{}\n"})
        world.commit({"notes/copy.json": None, "src/app.txt": "two\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert "notes/copy.json" in sentence
        assert ".guardkit/autobuild/short.json" in sentence

    def test_32_control_an_empty_product_file_beside_an_empty_record_is_published(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/empty.json": ""})
        world.commit({"src/empty.txt": ""})
        project = world.join()

        published(world, world.publish(project), project)

    def test_34_a_symbolic_link_replaced_by_identical_record_content_is_refused(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        (world.copy / "src" / "link").symlink_to("app.txt")
        world.commit(message="a link")
        world.make_this_the_target()
        world.commit({"src/link": OLD_RECORD})
        project = world.join()

        sentence = refused(world, world.publish(project), RECORDS_KIND)

        assert "src/link" in sentence


# ---------------------------------------------------------------------------
# Look d: private folder names in any new file version
# ---------------------------------------------------------------------------


class TestPrivateFolderNames:
    def test_23_a_private_line_added_then_removed_is_refused(self, world: World) -> None:
        line = f'labels: "com.example.workdir={PRIVATE}/projects/shop"\n'
        world.commit({"deploy/compose.yml": "services: {}\n" + line})
        world.commit({"deploy/compose.yml": "services: {}\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), PRIVATE_KIND)

        assert "deploy/compose.yml" in sentence
        assert PRIVATE not in sentence
        assert "com.example.workdir" not in sentence

    def test_33_a_document_quoting_a_diff_header_is_refused(self, world: World) -> None:
        world.commit({"docs/notes.md": f"An example:\n\n+++ {PRIVATE}/result.json\n"})
        world.commit({"docs/notes.md": None, "src/app.txt": "two\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), PRIVATE_KIND)

        assert "docs/notes.md" in sentence

    def test_34_a_file_turned_into_a_link_to_a_private_folder_is_refused(
        self, world: World
    ) -> None:
        world.commit({"src/where": "nowhere special\n"})
        world.make_this_the_target()
        (world.copy / "src" / "where").unlink()
        (world.copy / "src" / "where").symlink_to(f"{PRIVATE}/forge-state/receipts")
        world.commit(message="a link now")
        world.commit({"src/where": None, "src/app.txt": "two\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), PRIVATE_KIND)

        assert "src/where" in sentence

    def test_35_removing_one_of_two_private_lines_is_refused(self, tmp_path: Path) -> None:
        world = tracking(
            tmp_path,
            {"deploy/notes.txt": f"first {PRIVATE}/a\nsecond {PRIVATE}/b\nplain\n"},
        )
        world.commit({"deploy/notes.txt": f"second {PRIVATE}/b\nplain\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), PRIVATE_KIND)

        assert "deploy/notes.txt" in sentence

    def test_36_a_contaminated_file_copied_then_deleted_is_refused(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {"deploy/old.txt": f"path {PRIVATE}/x\n"})
        world.commit({"deploy/copy.txt": f"path {PRIVATE}/x\n"})
        world.commit({"deploy/copy.txt": None, "src/app.txt": "two\n"})
        project = world.join()

        sentence = refused(world, world.publish(project), PRIVATE_KIND)

        assert "deploy/copy.txt" in sentence

    def test_36_control_an_untouched_contaminated_file_still_publishes(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {"deploy/old.txt": f"path {PRIVATE}/x\n"})
        world.commit({"src/app.txt": "two\n"})
        project = world.join()

        published(world, world.publish(project), project)


# ---------------------------------------------------------------------------
# Ordinary work is never refused (tests 24, 30, 31)
# ---------------------------------------------------------------------------


class TestNoFalseRefusal:
    def test_24_the_base_tracks_a_record_the_branch_does_not_touch(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({"src/app.txt": "two\n"})
        project = world.join()

        published(world, world.publish(project), project)

    def test_24_the_branch_deletes_another_record(self, tmp_path: Path) -> None:
        world = tracking(
            tmp_path,
            {".guardkit/autobuild/old.json": OLD_RECORD, ".guardkit/autobuild/b.json": "{}\n"},
        )
        world.commit({".guardkit/autobuild/b.json": None})
        project = world.join()

        published(world, world.publish(project), project)

    def test_24_features_product_files_and_an_empty_file(self, world: World) -> None:
        world.commit(
            {
                ".guardkit/features/G.yaml": "id: G\n",
                "tasks/backlog/TASK-1.md": "# task\n",
                "src/new.txt": "new\n",
                "src/empty.txt": "",
            }
        )
        project = world.join()

        published(world, world.publish(project), project)

    def test_24_removing_the_only_private_line_is_published(self, tmp_path: Path) -> None:
        world = tracking(tmp_path, {"deploy/notes.txt": f"where {PRIVATE}/a\nplain\n"})
        world.commit({"deploy/notes.txt": "plain\n"})
        project = world.join()

        published(world, world.publish(project), project)

    def test_30_the_targets_record_and_product_copy_inherited_into_the_join(
        self, world: World
    ) -> None:
        world.commit({"src/more.txt": "more\n"})
        world.the_target_gains(
            {".guardkit/autobuild/target.json": OLD_RECORD, "src/app-copy.txt": "one\n"}
        )
        project = world.join()

        published(world, world.publish(project), project)

    def test_30_the_build_copies_an_ordinary_product_file(self, world: World) -> None:
        world.commit({"src/app-copy.txt": "one\n"})
        project = world.join()

        published(world, world.publish(project), project)

    def test_30_the_build_deletes_a_record(self, tmp_path: Path) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({".guardkit/autobuild/old.json": None})
        project = world.join()

        published(world, world.publish(project), project)

    def test_30_the_targets_own_public_record_copy_merged_into_the_build(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({"src/more.txt": "more\n"})
        world.the_target_gains({"docs/public-copy.json": EDITED})
        world.the_build_takes_the_target()
        project = world.join()

        published(world, world.publish(project), project)

    def test_30_the_target_deleted_a_record_the_build_did_not_touch(
        self, tmp_path: Path
    ) -> None:
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({"src/more.txt": "more\n"})
        world.the_target_gains({".guardkit/autobuild/old.json": None})
        project = world.join()

        published(world, world.publish(project), project)

    def test_31_out_of_scope_by_design_an_edited_copy_of_a_record_the_target_deleted(
        self, tmp_path: Path
    ) -> None:
        """Out of scope by design (round 5): a disguised record, no private text."""
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.the_target_gains({".guardkit/autobuild/old.json": None})
        world.commit({"notes/old.json": EDITED})
        project = world.join()

        published(world, world.publish(project), project)

    def test_31_out_of_scope_by_design_the_round_5_sequence(self, tmp_path: Path) -> None:
        """The build merges the target's deletion first, then copies. Out of
        scope by design, for the same reason as test 29."""
        world = tracking(tmp_path, {".guardkit/autobuild/old.json": OLD_RECORD})
        world.commit({"src/more.txt": "more\n"})
        world.the_target_gains({".guardkit/autobuild/old.json": None})
        world.the_build_takes_the_target()
        world.commit({"notes/old.json": EDITED})
        project = world.join()

        published(world, world.publish(project), project)


# ---------------------------------------------------------------------------
# A look that fails refuses (test 25), and the setting (test 26)
# ---------------------------------------------------------------------------


class TestALookThatFailsRefuses:
    def test_25_git_failing_during_the_looks_refuses_and_sends_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.commit({"src/app.txt": "two\n"})
        project = world.join()
        real = git_work.run_network_git

        def log_fails(args: Any, **kwargs: Any) -> NetworkResult:
            if list(args)[:1] == ["log"]:
                return NetworkResult(1, "", "git log stopped with exit code 1.")
            return real(args, **kwargs)

        monkeypatch.setattr(git_work, "run_network_git", log_fails)

        sentence = refused(world, world.publish(project), UNCHECKED_KIND)

        assert project["j"][:10] in sentence

    def test_25_a_commit_the_copy_does_not_have_cannot_be_checked(
        self, world: World
    ) -> None:
        world.commit({"src/app.txt": "two\n"})
        project = world.join()
        make_the_ledger(world.root / "forge.db", project=project)
        publisher = Publisher(settings_for(world.root, project, ledger=world.root / "forge.db"))
        route = publisher.settings.route(a_request(project)["project"])
        assert route is not None
        commits = publisher._its_own_copy(route)  # noqa: SLF001 - the seam under test
        assert commits.bring_the_joined_commit_out(project["j"]).ok

        assert commits.what_must_not_be_published("f" * 40, project["j"], private_prefixes=(PRIVATE,)) is None

    def test_25_no_private_prefixes_means_nothing_is_sent(self, world: World) -> None:
        world.commit({"src/app.txt": "two\n"})
        project = world.join()

        refused(world, world.publish(project, private_path_prefixes=()), UNCHECKED_KIND)


def _settings_file(tmp_path: Path, **extra: Any) -> Path:
    where = tmp_path / "settings.json"
    body = {"credential_file": "/k", "ledger": "/l", "state_dir": "/s", **extra}
    where.write_text(json.dumps(body), encoding="utf-8")
    return where


class TestTheSettingIsRequired:
    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"private_path_prefixes": []},
            {"private_path_prefixes": ["/home/"]},
            {"private_path_prefixes": ["/var/"]},
            {"private_path_prefixes": ["/home"]},
            {"private_path_prefixes": ["/"]},
            {"private_path_prefixes": ["relative/folder"]},
            {"private_path_prefixes": "/home/example"},
            {"private_path_prefixes": [PRIVATE, "/var"]},
            {"private_path_prefixes": [PRIVATE, 7]},
        ],
        ids=[
            "missing", "empty", "home", "var", "home without slash", "root",
            "relative", "not a list", "one generic among good", "not text",
        ],
    )
    def test_26_a_missing_empty_or_generic_setting_is_refused(
        self, tmp_path: Path, extra: dict[str, Any]
    ) -> None:
        with pytest.raises(SettingsRefused) as caught:
            load_settings(_settings_file(tmp_path, **extra))
        assert "private_path_prefixes" in str(caught.value)

    def test_26_a_real_looking_setting_is_read(self, tmp_path: Path) -> None:
        loaded = load_settings(
            _settings_file(
                tmp_path, private_path_prefixes=[PRIVATE, "/srv/sandboxes/storage/"]
            )
        )
        assert loaded.private_path_prefixes == (PRIVATE, "/srv/sandboxes/storage")
        assert "private_path_prefixes" not in json.dumps(loaded.without_secrets())
        assert loaded.without_secrets()["private_path_prefix_count"] == 2
