"""The merge press's git spoken to a sandbox's sidecar (sandbox first, rule 89).

:class:`~forge.deploy.sidecar_git.SidecarCandidateGit` is the other half of the
venue seam: the same five operations the in-container venue performs, said over
HTTP to the deploy sidecar inside a repository's sandbox. Here it talks to the
REAL service on a real ephemeral loopback port, against a real git repository,
so what is proved is the two halves agreeing — not a mock's idea of them.

The failure half matters as much: a sidecar that cannot be reached must never
crash a press. Every read answers ``None``, a tree's removal answers
``False`` (a working folder's answers ``None``: no answer came back), and
only laying a tree out raises — which is the one place the press already
expects a raise and turns into a plain refusal before the merge.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import CandidateTreeError, InContainerCandidateGit
from forge.deploy.sidecar_git import SidecarCandidateGit
from forge.deploy_sidecar.service import build_server

REPO = "guardkit/api_test"
FEATURE_ID = "FEAT-SG1"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}", "main")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", "feature.txt")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    return root.resolve()


@pytest.fixture
def git(clone: Path) -> SidecarCandidateGit:
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(clone)}},
        }
    )
    srv = build_server(port=0, config_loader=lambda: config)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    try:
        yield SidecarCandidateGit(f"http://{host}:{port}", repo=REPO)
    finally:
        srv.shutdown()
        srv.server_close()


class TestTheTwoHalvesOfTheSeamAgree:
    @pytest.mark.asyncio
    async def test_the_same_five_operations_give_the_same_answers(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        here = InContainerCandidateGit(clone)
        branch = f"autobuild/{FEATURE_ID}"
        tip = _git(clone, "rev-parse", branch)

        assert await git.rev_parse(branch) == await here.rev_parse(branch) == tip
        assert await git.rev_parse(f"{tip}^{{tree}}") == await here.rev_parse(
            f"{tip}^{{tree}}"
        )
        assert await git.rev_parse("autobuild/FEAT-NOPE") is None
        assert await git.is_ancestor("main", branch) is True
        assert await git.is_ancestor(branch, "main") is False
        assert await git.is_ancestor("b" * 40, "main") is None

        laid_out = await git.materialise_candidate_tree(FEATURE_ID, tip)
        assert Path(laid_out.path) == clone / ".forge-candidates" / FEATURE_ID
        assert (Path(laid_out.path) / "feature.txt").is_file()
        # This venue answers both while it is in there, so the press does not
        # have to ask a second time.
        assert laid_out.tree == _git(clone, "rev-parse", f"{tip}^{{tree}}")
        assert laid_out.exclude_written is True

        assert await git.remove_candidate_tree(FEATURE_ID) is True
        assert not Path(laid_out.path).exists()

    @pytest.mark.asyncio
    async def test_the_path_it_gives_back_is_the_one_it_laid_out(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        """The deploy leg and the live gate are pointed at exactly this path."""
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")
        laid_out = await git.materialise_candidate_tree(FEATURE_ID, tip)
        assert Path(laid_out.path).is_dir()

    @pytest.mark.asyncio
    async def test_a_commit_the_clone_does_not_have_raises_the_press_can_read(
        self, git: SidecarCandidateGit
    ) -> None:
        with pytest.raises(CandidateTreeError) as raised:
            await git.materialise_candidate_tree(FEATURE_ID, "b" * 40)
        assert "git archive" in str(raised.value)
        # The sandbox refused it in its own words: an answer (15 October 2026).
        assert raised.value.confirmed is True

    @pytest.mark.asyncio
    async def test_no_path_from_this_side_reaches_git_in_there(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        """A removal names the feature; where its tree is, is the sandbox's to know."""
        other = clone / ".forge-candidates" / "FEAT-OTHER"
        other.mkdir(parents=True)
        (other / "keep.txt").write_text("keep\n", encoding="utf-8")

        assert await git.remove_candidate_tree(FEATURE_ID, str(other)) is True
        assert (other / "keep.txt").is_file()


class TestTheJoinsWorkingFolderIsRemovedInThere:
    @pytest.mark.asyncio
    async def test_a_path_from_this_side_still_removes_the_folder_in_there(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        """FEAT-A804 (8 October 2026): the press removes the folder by the path
        it has on its own side, which for a sandboxed repository is not the
        clone's path in there. Only the folder's name travels, so it works."""
        made = await git.add_working_folder("integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main")
        assert made.ok, made.refusal
        assert Path(made.path) == clone / ".forge" / "worktrees" / "integration-FEAT-SG1"

        coordinator_side = "/var/lib/forge/projects/api_test/.forge/worktrees/integration-FEAT-SG1"
        assert await git.remove_working_folder(coordinator_side) is True
        assert not Path(made.path).exists()

    @staticmethod
    def _a_read_only_module_cache_in(folder: Path) -> None:
        """What the project's Go checks leave behind (FEAT-D267, 15 October
        2026): a module cache of read-only folders in GuardKit's scratch area
        inside Git's record folder for the working folder."""
        record = Path(
            (folder / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        )
        module = record / "guardkit-scratch" / "HOME" / "go" / "pkg" / "mod" / "m@v1"
        module.mkdir(parents=True)
        (module / "go.mod").write_text("module m\n", encoding="utf-8")
        module.chmod(0o555)
        module.parent.chmod(0o555)

    @pytest.mark.asyncio
    async def test_a_read_only_module_cache_does_not_stop_the_removal(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        made = await git.add_working_folder(
            "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
        )
        assert made.ok, made.refusal
        self._a_read_only_module_cache_in(Path(made.path))

        assert await git.remove_working_folder(str(made.path)) is True
        assert not Path(made.path).exists()
        assert "integration-" not in _git(clone, "worktree", "list", "--porcelain")

    @pytest.mark.asyncio
    async def test_a_removal_the_sandbox_answered_it_could_not_do_is_false(
        self,
        git: SidecarCandidateGit,
        clone: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ANSWERED "not removed" is ``False``; only a lost answer is
        ``None`` (the press keeps its hold for that alone)."""
        import forge.subagents.autobuild_worktree_lifecycle as lifecycle

        made = await git.add_working_folder(
            "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
        )
        assert made.ok, made.refusal
        self._a_read_only_module_cache_in(Path(made.path))
        # As before the fix: nothing makes the cache's folders writable first.
        monkeypatch.setattr(lifecycle, "make_worktree_removable", lambda _path: None)

        assert await git.remove_working_folder(str(made.path)) is False

    @pytest.mark.asyncio
    async def test_a_folder_the_sandbox_refused_was_answered(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        """A refusal in the sandbox's own words is an answer: nothing is
        being made in there."""
        made = await git.add_working_folder(
            "integration/../escape", "factory-integration/FEAT-SG1", "main"
        )
        assert made.ok is False
        assert made.confirmed is True


class TestTheProjectsOwnFileIsReadInThere:
    """The press reads ``deploy/profile.yaml`` at a commit through this venue
    (3 October 2026, FEAT-E592), so the two halves must agree on that too."""

    @pytest.mark.asyncio
    async def test_a_file_at_a_commit_reads_the_same_on_both_sides(
        self, git: SidecarCandidateGit, clone: Path
    ) -> None:
        from forge.deploy.candidate_tree import read_file_at_commit

        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")

        there = await git.read_file_at_commit(tip, "feature.txt")
        here = await read_file_at_commit(clone, tip, "feature.txt")
        assert there == here
        assert there.found is True and there.content == "the feature\n"
        # Not in that commit is an answer, not a refusal.
        absent = await git.read_file_at_commit("main", "feature.txt")
        assert absent.ok is True and absent.found is False
        # A commit the clone does not have is a refusal, in git's words.
        missing = await git.read_file_at_commit("b" * 40, "feature.txt")
        assert missing.ok is False and "does not have the commit" in str(
            missing.refusal
        )


class TestASidecarThatCannotBeReached:
    """Never a crash: an honest answer, and the press says what it could not do."""

    @staticmethod
    def _unreachable() -> SidecarCandidateGit:
        # Port 1 on loopback: nothing listens, and nothing is asked to.
        return SidecarCandidateGit("http://127.0.0.1:1", repo=REPO)

    @pytest.mark.asyncio
    async def test_reads_answer_none_and_a_removal_answers_false(self) -> None:
        git = self._unreachable()

        assert await git.rev_parse("main") is None
        assert await git.is_ancestor("main", "main") is None
        assert await git.ensure_candidate_trees_excluded() is None
        # No answer at all is None, not False (15 October 2026).
        assert await git.remove_candidate_tree(FEATURE_ID) is None
        unread = await git.read_file_at_commit("main", "deploy/profile.yaml")
        assert unread.ok is False and "127.0.0.1:1" in str(unread.refusal)

    @pytest.mark.asyncio
    async def test_a_working_folder_with_no_answer_says_it_had_none(self) -> None:
        """15 October 2026: the press keeps its hold when the join folder's
        request got no answer, so the venue says so."""
        made = await self._unreachable().add_working_folder(
            "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
        )
        assert made.ok is False
        assert made.confirmed is False

    @pytest.mark.asyncio
    async def test_a_working_folder_removal_with_no_answer_answers_none(self) -> None:
        """15 October 2026, FEAT-D267: no answer is ``None``, not ``False``,
        so the press can tell it from a removal the sandbox refused."""
        assert (
            await self._unreachable().remove_working_folder(
                "/x/.forge/worktrees/integration-FEAT-SG1"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_a_retirement_with_no_answer_says_it_had_none(self) -> None:
        """15 October 2026: the press puts its hold down after a retirement
        the sandbox answered "kept", but not after one with no answer, which
        may still be running in there."""
        retired = await self._unreachable().retire_autobuild_worktree(
            "build-X", "/retained/worktree", {}
        )
        assert retired["status"] == "kept"
        assert retired["answered"] is False

    @pytest.mark.asyncio
    async def test_a_lay_out_raises_with_the_address_in_the_sentence(self) -> None:
        with pytest.raises(CandidateTreeError) as raised:
            await self._unreachable().materialise_candidate_tree(FEATURE_ID, "a" * 40)
        assert "127.0.0.1:1" in str(raised.value)
        # No answer came back: the tree may still be being written.
        assert raised.value.confirmed is False

    def test_it_refuses_to_be_built_without_the_repositorys_key(self) -> None:
        with pytest.raises(ValueError):
            SidecarCandidateGit("http://127.0.0.1:1", repo="")

    def test_it_says_where_it_is_in_words_a_person_reads(self) -> None:
        assert self._unreachable().venue == f"in the sandbox that holds {REPO}"

class TestRetainedAutobuildWorktreeLifecycle:
    @pytest.mark.asyncio
    async def test_sidecar_inspects_and_retires_only_the_offer_pinned_tree(
        self,
        git: SidecarCandidateGit,
        clone: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        build_id = "build-FEAT-SG1-20260918"
        base = tmp_path / "autobuild-worktrees"
        outer = base / build_id
        monkeypatch.setenv("FORGE_AUTOBUILD_WORKTREE_BASE", str(base))
        _git(clone, "worktree", "add", "--detach", str(outer), "main")
        inner = outer / ".guardkit/worktrees/TASK-SG1-001"
        inner.parent.mkdir(parents=True)
        _git(clone, "worktree", "add", str(inner), f"autobuild/{FEATURE_ID}")
        (inner / "ignored-by-owner.txt").write_text("retained\n")

        offered = await git.inspect_autobuild_worktree(build_id, str(outer))
        assert offered["ok"] is True
        assert offered["nested_registrations"][0]["path"] == str(inner)
        assert outer.is_dir() and inner.is_dir()
        offered["cleanup_registrations"] = offered["nested_registrations"]

        result = await git.retire_autobuild_worktree(
            build_id, str(outer), offered
        )
        assert result["status"] == "removed", result
        assert not outer.exists() and not inner.exists()

    @pytest.mark.asyncio
    async def test_sidecar_refuses_a_path_outside_the_configured_base(
        self,
        git: SidecarCandidateGit,
        clone: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "FORGE_AUTOBUILD_WORKTREE_BASE", str(tmp_path / "owned-base")
        )
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep\n")

        answer = await git.inspect_autobuild_worktree("build-X", str(outside))
        assert answer["ok"] is False
        assert "configured autobuild worktree" in answer["detail"]
        assert (outside / "keep").is_file()



class _ASidecarThatAnswersWith:
    """A loopback server that answers every request with ``status`` and
    ``body``, whatever was asked (15 October 2026, Codex R3)."""

    def __init__(self, status: int, body: object) -> None:
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                answer = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address[:2]
        self.git = SidecarCandidateGit(f"http://{host}:{port}", repo=REPO)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestAnAnswerThatIsNotTheOperationsOwnIsNoAnswer:
    """15 October 2026, Codex R3: a reply that is a JSON object but carries no
    result of the operation (``{}`` on a 200, or a refusal with no sentence)
    says nothing about whether the operation happened, so it is read as no
    answer. A refusal in the sidecar's own words is still an answer."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status, body",
        [
            (200, {}),
            (200, {"status": "maybe"}),
            (400, {}),
            (200, []),
            # Codex R9: a 5xx is an error met while the operation may have
            # been under way, whatever its sentence.
            (500, {"error": "sidecar git error: OSError: the disk went away"}),
        ],
        ids=[
            "empty-200",
            "unknown-status",
            "refusal-with-no-sentence",
            "not-an-object",
            "5xx-with-a-sentence",
        ],
    )
    async def test_malformed_replies(self, status: int, body: object) -> None:
        sidecar = _ASidecarThatAnswersWith(status, body)
        try:
            git = sidecar.git
            made = await git.add_working_folder(
                "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
            )
            assert made.ok is False and made.confirmed is False
            assert await git.remove_working_folder("/x/integration-FEAT-SG1") is None
            assert await git.remove_candidate_tree(FEATURE_ID) is None
            with pytest.raises(CandidateTreeError) as raised:
                await git.materialise_candidate_tree(FEATURE_ID, "a" * 40)
            assert raised.value.confirmed is False
            retired = await git.retire_autobuild_worktree("build-X", "/retained", {})
            assert retired["status"] == "kept" and retired["answered"] is False
        finally:
            sidecar.close()

    @pytest.mark.asyncio
    async def test_a_refusal_in_the_sidecars_own_words_is_an_answer(self) -> None:
        sidecar = _ASidecarThatAnswersWith(400, {"error": "that branch already exists"})
        try:
            git = sidecar.git
            made = await git.add_working_folder(
                "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
            )
            assert made.ok is False and made.confirmed is True
            assert await git.remove_working_folder("/x/integration-FEAT-SG1") is False
            assert await git.remove_candidate_tree(FEATURE_ID) is False
            with pytest.raises(CandidateTreeError) as raised:
                await git.materialise_candidate_tree(FEATURE_ID, "a" * 40)
            assert raised.value.confirmed is True
            retired = await git.retire_autobuild_worktree("build-X", "/retained", {})
            assert retired["status"] == "kept" and retired["answered"] is True
        finally:
            sidecar.close()

    @pytest.mark.asyncio
    async def test_answered_operations_read_as_before(self) -> None:
        sidecar = _ASidecarThatAnswersWith(200, {"status": "failed", "removed": False})
        try:
            git = sidecar.git
            made = await git.add_working_folder(
                "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
            )
            assert made.ok is False and made.confirmed is True
            assert await git.remove_working_folder("/x/integration-FEAT-SG1") is False
            assert await git.remove_candidate_tree(FEATURE_ID) is False
        finally:
            sidecar.close()



class TestTheSandboxSaysWhetherGitAnswered:
    """15 October 2026, Codex R13, through the REAL sidecar route: a
    ``git worktree`` command that raised after it was sent is a 5xx — no
    answer — and one git answered with an exit code is an answer."""

    @staticmethod
    def _git_worktree(monkeypatch: pytest.MonkeyPatch, verb: str, how: str) -> None:
        from tests.forge.pipeline.test_the_merge_word_publishes import (
            _git_worktree_commands_that,
        )

        _git_worktree_commands_that(monkeypatch, verb, how)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how, expected", [("raises", None), ("refused", False)])
    async def test_the_removal(
        self, git: SidecarCandidateGit, monkeypatch: pytest.MonkeyPatch, how, expected
    ) -> None:
        made = await git.add_working_folder(
            "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
        )
        assert made.ok and made.confirmed
        self._git_worktree(monkeypatch, "remove", how)
        assert await git.remove_working_folder(str(made.path)) is expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how, confirmed", [("raises", False), ("refused", True)])
    async def test_the_creation(
        self, git: SidecarCandidateGit, monkeypatch: pytest.MonkeyPatch, how, confirmed
    ) -> None:
        self._git_worktree(monkeypatch, "add", how)
        made = await git.add_working_folder(
            "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
        )
        assert made.ok is False
        assert made.confirmed is confirmed


class TestATimeoutIsReadFirstInEveryGitReply:
    """15 October 2026, Codex round 9, R17: a reply that says the operation
    was stopped at its time limit is no answer, however complete the rest of
    it looks."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status, body",
        [
            (200, {"status": "success", "path": "/x/integration-FEAT-SG1", "removed": True, "timed_out": True}),
            (200, {"status": "failed", "removed": False, "timed_out": True}),
            (400, {"error": "stopped at its limit", "timed_out": True}),
        ],
        ids=["success", "failure", "refusal"],
    )
    async def test_every_operation(self, status: int, body: dict) -> None:
        sidecar = _ASidecarThatAnswersWith(status, body)
        try:
            git = sidecar.git
            made = await git.add_working_folder(
                "integration-FEAT-SG1", "factory-integration/FEAT-SG1", "main"
            )
            assert made.confirmed is False
            assert await git.remove_working_folder("/x/integration-FEAT-SG1") is None
            assert await git.remove_candidate_tree(FEATURE_ID) is None
            with pytest.raises(CandidateTreeError) as raised:
                await git.materialise_candidate_tree(FEATURE_ID, "a" * 40)
            assert raised.value.confirmed is False
            retired = await git.retire_autobuild_worktree("build-X", "/retained", {})
            assert retired["confirmed"] is False
        finally:
            sidecar.close()
