"""What the publisher checks for itself, and what it sends when it is satisfied.

One-true-copy design pass, item 1, second revision section D: the publisher
"checks for itself what it can: that J is a merge of G and the recorded build
tip; that G was the remote's target branch; that sending J moves the remote's
branch forwards and never sideways."

THE "REMOTE" IS A BARE REPOSITORY ON DISK. Real git, nobody's account, made
in the test's own temporary folder and thrown away with it. Nothing here
contacts a real remote, starts a service, builds an image or touches a
sandbox.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from forge.publisher import git_work
from forge.publisher.service import Publisher
from tests.forge.publisher.a_project_and_a_ledger import (
    FEATURE,
    a_request,
    every_push_the_remote_saw,
    git,
    make_the_ledger,
    make_the_project,
    settings_for,
    somebody_else_lands_work,
    what_the_remote_has,
)


def _string_literals(path: Path) -> list[str]:
    """Every string literal in the file — not its comments or docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.fixture()
def project(tmp_path: Path) -> dict:
    return make_the_project(tmp_path / "world")


def _publisher(tmp_path: Path, project: dict, **ledger: object) -> Publisher:
    root = tmp_path / "world"
    make_the_ledger(root / "forge.db", project=project, **ledger)  # type: ignore[arg-type]
    return Publisher(settings_for(root, project, ledger=root / "forge.db"))


class TestItSends:
    def test_nothing_moved_upstream(self, tmp_path: Path, project: dict) -> None:
        publisher = _publisher(tmp_path, project)

        answer = publisher.publish(a_request(project))

        assert answer.published is True
        assert answer.contains_j is True
        assert answer.refusal is None
        assert answer.remote_now == project["j"]
        assert what_the_remote_has(project["bare"]) == project["j"]
        # ONE push, counted on the remote's own reflog.
        assert len(every_push_the_remote_saw(project["bare"])) == 1

    def test_the_branch_CONTAINS_it_rather_than_IS_it(
        self, tmp_path: Path, project: dict
    ) -> None:
        """Somebody else may add to the branch in the seconds between.

        The publisher sends, then somebody else lands a commit on top, then
        the question "does the branch contain J" is asked again — and it is
        still yes. That is the rule the design writes, and the reason it is
        "contains" and not "is".
        """
        publisher = _publisher(tmp_path, project)
        assert publisher.publish(a_request(project)).published is True

        after = somebody_else_lands_work(project["bare"], tmp_path / "world", "later")
        assert after != project["j"]

        # Asked again: already there, so it is not sent a second time.
        again = publisher.publish(a_request(project))
        assert again.published is True
        assert again.contains_j is True
        assert again.remote_now == after
        assert len(every_push_the_remote_saw(project["bare"])) == 2  # the other hand's

    def test_a_branch_that_is_not_called_main(self, tmp_path: Path) -> None:
        """Nothing here knows the word "main". The recorded branch is used."""
        project = make_the_project(tmp_path / "world", branch="the-line")
        root = tmp_path / "world"
        make_the_ledger(root / "forge.db", project=project)
        publisher = Publisher(settings_for(root, project, ledger=root / "forge.db"))

        answer = publisher.publish(a_request(project))

        assert answer.published is True
        assert what_the_remote_has(project["bare"], "the-line") == project["j"]


class TestItChecksTheJoinedCommitForItself:
    def test_a_commit_that_is_not_a_merge_at_all(
        self, tmp_path: Path, project: dict
    ) -> None:
        """The record says this is J; git says it has one parent."""
        root = tmp_path / "world"
        # A record whose joined commit is the build's own tip — a single
        # commit, not a join. Every other thing the publisher reads agrees.
        forged = dict(project)
        forged["j"] = project["tip"]
        make_the_ledger(root / "forge.db", project=forged)
        publisher = Publisher(settings_for(root, forged, ledger=root / "forge.db"))
        before = what_the_remote_has(project["bare"])

        answer = publisher.publish(a_request(forged))

        assert answer.published is False
        assert answer.refusal_kind == "it-is-not-the-join"
        assert "parent" in str(answer.refusal)
        assert what_the_remote_has(project["bare"]) == before
        assert every_push_the_remote_saw(project["bare"]) == []

    def test_a_join_of_the_wrong_two_commits(self, tmp_path: Path) -> None:
        """A real merge — of an older tip of the build. Its tree is not the one."""
        root = tmp_path / "world"
        project = make_the_project(root)
        copy = project["copy"]
        # The build gains a fix AFTER the join was made, so the recorded
        # build tip moves and the join is a join of the older one.
        git(copy, "checkout", "-q", f"autobuild/{FEATURE}")
        (copy / "the-fix").write_text("the fix\n", encoding="utf-8")
        git(copy, "add", "the-fix")
        git(copy, "commit", "-q", "-m", "the fix")
        newer_tip = git(copy, "rev-parse", "HEAD")
        git(copy, "checkout", "-q", project["branch"])
        moved = dict(project)
        moved["tip"] = newer_tip
        make_the_ledger(root / "forge.db", project=moved)
        publisher = Publisher(settings_for(root, moved, ledger=root / "forge.db"))
        before = what_the_remote_has(project["bare"])

        answer = publisher.publish(a_request(moved))

        assert answer.published is False
        assert answer.refusal_kind == "it-is-not-the-join"
        assert project["tip"][:10] in str(answer.refusal)
        assert newer_tip[:10] in str(answer.refusal)
        assert what_the_remote_has(project["bare"]) == before

    def test_the_joined_commit_is_not_in_the_projects_copy(
        self, tmp_path: Path, project: dict
    ) -> None:
        """A record naming a commit nobody has. Nothing is brought out."""
        root = tmp_path / "world"
        nowhere = dict(project)
        nowhere["j"] = "0" * 40
        make_the_ledger(root / "forge.db", project=nowhere)
        publisher = Publisher(settings_for(root, nowhere, ledger=root / "forge.db"))

        answer = publisher.publish(a_request(nowhere))

        assert answer.published is False
        assert answer.refusal_kind == "the-joined-commit-is-not-there"
        assert every_push_the_remote_saw(project["bare"]) == []


class TestTheSendMovesTheBranchForwardsOrNotAtAll:
    def test_G_is_not_on_the_remotes_branch_any_more(
        self, tmp_path: Path, project: dict
    ) -> None:
        """The remote moved SIDEWAYS: its branch was rewritten off G.

        The publisher refuses before it sends, because sending would move the
        branch somewhere that does not contain what is there now.
        """
        publisher = _publisher(tmp_path, project)
        # The remote's branch is put somewhere that does not contain G — the
        # shape a rewritten history has. Done with git, on the bare
        # repository, which is the only way it can really happen.
        other = tmp_path / "world" / "rewritten"
        subprocess.run(
            ["git", "clone", "-q", str(project["bare"]), str(other)],
            check=True,
            capture_output=True,
        )
        git(other, "checkout", "-q", "--orphan", "elsewhere")
        (other / "nothing-to-do-with-it").write_text("different\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "a different history")
        sideways = git(other, "rev-parse", "HEAD")
        git(other, "push", "-q", "--force", "origin", f"HEAD:{project['branch']}")
        assert what_the_remote_has(project["bare"]) == sideways

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-remote-moved"
        assert "sideways rather than forwards" in str(answer.refusal)
        assert answer.remote_now == sideways
        assert what_the_remote_has(project["bare"]) == sideways

    def test_the_branch_gained_work_after_the_join_was_made(
        self, tmp_path: Path, project: dict
    ) -> None:
        """G is still on the branch, and the branch has moved on past it.

        The commonest shape by far: somebody else landed work while the build
        was being checked. Sending would not move the branch forwards — git
        itself would refuse it — so the publisher says so first, in the one
        refusal a new attempt is the answer to.
        """
        publisher = _publisher(tmp_path, project)
        moved = somebody_else_lands_work(
            project["bare"], tmp_path / "world", "while-checking"
        )

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-remote-moved"
        assert "has gained work since the join was made" in str(answer.refusal)
        assert "joined onto where the branch is now" in str(answer.refusal)
        assert answer.remote_now == moved
        assert what_the_remote_has(project["bare"]) == moved
        # The one push on the remote's reflog is the other hand's, not the
        # publisher's: nothing of this send is on the branch.
        assert len(every_push_the_remote_saw(project["bare"])) == 1

    def test_the_remote_moved_between_the_look_and_the_send(
        self, tmp_path: Path, project: dict
    ) -> None:
        """A REAL non-fast-forward refusal, from a real bare repository.

        The publisher is allowed to look, and the branch moves underneath it
        before the push. Git itself refuses the push, because the push does
        not force — which is the guarantee the design wants: the remote is
        the thing that decides.
        """
        publisher = _publisher(tmp_path, project)
        route = publisher.settings.route(a_request(project)["project"])
        assert route is not None
        commits = publisher._its_own_copy(route)  # noqa: SLF001 - the seam under test

        moved_to: dict[str, str] = {}

        real_look = commits.where_the_remotes_branch_is

        def look_then_let_somebody_else_land(branch: str):
            answer = real_look(branch)
            if "moved" not in moved_to:
                moved_to["moved"] = somebody_else_lands_work(
                    project["bare"], tmp_path / "world", "between"
                )
            return answer

        commits.where_the_remotes_branch_is = look_then_let_somebody_else_land  # type: ignore[method-assign]
        publisher._commits_for = lambda _route: commits  # noqa: SLF001

        answer = publisher.publish(a_request(project))

        assert answer.published is False
        assert answer.refusal_kind == "the-remote-moved"
        assert "refused the send" in str(answer.refusal)
        # And the branch is exactly what the other hand left: nothing of this
        # send is on it.
        assert what_the_remote_has(project["bare"]) == moved_to["moved"]


class TestAForcedPushIsNotExpressible:
    def test_the_one_argument_list_a_send_is_made_with(self) -> None:
        argv = git_work.the_send_argv("somewhere", "a" * 40, "the-line")
        assert argv == ["push", "somewhere", f"{'a' * 40}:refs/heads/the-line"]
        assert "--force" not in argv
        assert "-f" not in argv
        assert "--force-with-lease" not in argv
        assert not argv[2].startswith("+")

    def test_no_forcing_word_appears_anywhere_in_the_publisher(self) -> None:
        """Read the whole package. A force is not refused; it is not writable.

        String LITERALS only — a docstring saying "no ``--force``" is a
        docstring, and a literal is a thing the program can hand to git.
        """
        package = Path(git_work.__file__).resolve().parent
        forbidden = ("--force", "--force-with-lease", "--mirror", "+refs/heads/*:refs")
        found: dict[str, list[str]] = {}
        for path in sorted(package.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            said = [
                text
                for text in _string_literals(path)
                if any(word in text for word in forbidden)
            ]
            if said:
                found[path.name] = said
        assert found == {}, found

    def test_the_only_refspec_that_leaves_the_publisher_is_one_commit(self) -> None:
        """It sends a commit to a branch, never a whole namespace."""
        argv = git_work.the_send_argv("somewhere", "b" * 40, "trunk")
        source, _colon, destination = argv[2].partition(":")
        assert source == "b" * 40
        assert destination == "refs/heads/trunk"
