"""MergeOfferService — the merge card's offer path, offline.

Covers: the enabled gate, the tasks_failed gate, the empty-correlation and
missing-row/missing-repo skips, the durable latch (written BEFORE any wire,
double-offer refused, publish-failure never retried), the dual-envelope
publish ORDER (approval first, paused second), and the payload shapes
verbatim — including the ``merge-{feature_id}`` join key on the paused
envelope's build_id.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildCompletePayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_STAGE_LABEL,
    MERGE_OFFER_TARGET_IDENTIFIER,
    MergeOfferService,
    _ask_the_head_reader,
    approval_subject_for,
    git_rev_parse_main,
    head_reader_takes_a_branch,
    merge_request_id,
    read_baseline_failing,
)

BUILD_ID = "build-FEAT-MO1-20260824"
FEATURE_ID = "FEAT-MO1"
REPO = "appmilla/api_test"
CORRELATION = "corr-mo-1"
CANDIDATE_SHA = "c" * 40
CANDIDATE_TREE = "d" * 40


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


def _insert_build(
    pool: SqliteLifecyclePersistence,
    *,
    build_id: str = BUILD_ID,
    feature_id: str = FEATURE_ID,
    repo: str = REPO,
    correlation_id: str = CORRELATION,
) -> None:
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode) VALUES (?, ?, ?, ?, 'f.yaml', 'COMPLETE', 'cli', ?, "
        "'2026-08-24T00:00:00Z', 'mode-a')",
        (build_id, feature_id, repo, f"autobuild/{feature_id}", correlation_id),
    )
    pool.connection.commit()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    root.mkdir()
    return root


@pytest.fixture
def config(repo_root: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "merge_executor": {"enabled": True},
        }
    )


class _Recorder:
    """Shared publish recorder proving cross-channel ORDER."""

    def __init__(self, *, approval_raises: bool = False) -> None:
        self.events: list[tuple[str, Any]] = []
        self.approval_raises = approval_raises

    async def raw_publish(self, subject: str, body: bytes) -> None:
        if self.approval_raises:
            raise RuntimeError("wire down")
        self.events.append(("approval", (subject, body)))

    async def publish_build_paused(self, payload: Any) -> None:
        self.events.append(("paused", payload))


class _CandidatePins:
    async def rev_parse(self, ref: str) -> str | None:
        return CANDIDATE_TREE if ref.endswith("^{tree}") else CANDIDATE_SHA


def _service(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    recorder: _Recorder,
    *,
    sha: str | None = "mainsha1234",
    git_surface: Any | None = None,
) -> MergeOfferService:
    async def _git_head(_repo_root: Path) -> str | None:
        return sha

    return MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(
            publish_build_paused=recorder.publish_build_paused
        ),
        raw_publish=recorder.raw_publish,
        git_head=_git_head,
        git_surface=git_surface or (lambda _repo, _root: _CandidatePins()),
        # Pinned 21 September 2026 (the Stage C review, the same hygiene
        # finding): without it these tests call the real finished-feature
        # reader, which looks under the HOST's own receipts folder. This
        # answers exactly what the real reader answers for a build that
        # exported nothing, so the cards are the cards they already read.
        finished_feature_reader=lambda *_a, **_k: (
            None,
            None,
            "nothing was exported for this build",
        ),
    )


def _event(*, tasks_failed: int = 0, build_id: str = BUILD_ID) -> BuildCompletePayload:
    completed = 5 - tasks_failed if tasks_failed <= 5 else 0
    return BuildCompletePayload(
        feature_id=FEATURE_ID,
        build_id=build_id,
        tasks_completed=completed,
        tasks_failed=tasks_failed,
        tasks_total=completed + tasks_failed,
        duration_seconds=10,
        summary="done",
    )


def _offer_rows(pool: SqliteLifecyclePersistence, build_id: str = BUILD_ID):
    return [
        s
        for s in pool.read_stages(build_id)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ]


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class TestGates:
    @pytest.mark.asyncio
    async def test_disabled_config_is_a_no_op(
        self, pool, repo_root: Path
    ) -> None:
        cfg = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            }
        )
        _insert_build(pool)
        recorder = _Recorder()
        await _service(cfg, pool, recorder).maybe_offer(_event())
        assert recorder.events == []
        assert _offer_rows(pool) == []

    @pytest.mark.asyncio
    async def test_non_build_complete_payload_is_a_no_op(
        self, config, pool
    ) -> None:
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(
            SimpleNamespace(build_id=BUILD_ID)
        )
        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_tasks_failed_gate(self, config, pool) -> None:
        _insert_build(pool)
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(_event(tasks_failed=1))
        assert recorder.events == []
        assert _offer_rows(pool) == []

    @pytest.mark.asyncio
    async def test_missing_builds_row_skips_loudly(self, config, pool) -> None:
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(_event())
        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_unmapped_repo_skips(self, pool, repo_root: Path) -> None:
        cfg = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {"acme/other": str(repo_root)}},
                "merge_executor": {"enabled": True},
            }
        )
        _insert_build(pool)
        recorder = _Recorder()
        await _service(cfg, pool, recorder).maybe_offer(_event())
        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_empty_correlation_skips(self, config, pool) -> None:
        _insert_build(pool, correlation_id="")
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(_event())
        assert recorder.events == []
        assert _offer_rows(pool) == []

    @pytest.mark.asyncio
    async def test_unreadable_main_sha_refuses_the_offer(
        self, config, pool
    ) -> None:
        _insert_build(pool)
        recorder = _Recorder()
        await _service(config, pool, recorder, sha=None).maybe_offer(_event())
        assert recorder.events == []
        assert _offer_rows(pool) == []


# ---------------------------------------------------------------------------
# The latch
# ---------------------------------------------------------------------------


class TestDurableLatch:
    @pytest.mark.asyncio
    async def test_latch_written_and_double_offer_refused(
        self, config, pool
    ) -> None:
        _insert_build(pool)
        recorder = _Recorder()
        service = _service(config, pool, recorder)
        await service.maybe_offer(_event())
        await service.maybe_offer(_event())
        rows = _offer_rows(pool)
        assert len(rows) == 1
        assert rows[0].status == "GATED"
        assert rows[0].stage_label == MERGE_OFFER_STAGE_LABEL
        assert rows[0].gate_mode == "MANDATORY_HUMAN_APPROVAL"
        # Exactly ONE dual publish despite two terminal observations.
        assert [kind for kind, _ in recorder.events] == ["approval", "paused"]

    @pytest.mark.asyncio
    async def test_publish_failure_latches_and_never_retries(
        self, config, pool
    ) -> None:
        _insert_build(pool)
        recorder = _Recorder(approval_raises=True)
        service = _service(config, pool, recorder)
        await service.maybe_offer(_event())
        # The latch stands even though the wire raised on the FIRST leg...
        assert len(_offer_rows(pool)) == 1
        # ...the paused mirror was never attempted after the raise...
        assert recorder.events == []
        # ...and a re-observation does NOT retry (one attempt ever).
        recorder.approval_raises = False
        await service.maybe_offer(_event())
        assert recorder.events == []


# ---------------------------------------------------------------------------
# The dual envelope
# ---------------------------------------------------------------------------


class TestDualEnvelope:
    @pytest.mark.asyncio
    async def test_publish_order_and_payload_shapes(self, config, pool) -> None:
        _insert_build(pool)
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(_event())

        assert [kind for kind, _ in recorder.events] == ["approval", "paused"]

        # --- the AGENTS approval envelope, FIRST -----------------------
        subject, body = recorder.events[0][1]
        assert subject == approval_subject_for(FEATURE_ID)
        assert subject == f"agents.approval.forge.merge-{FEATURE_ID}"
        envelope = MessageEnvelope.model_validate_json(body)
        assert envelope.source_id == "forge"
        assert envelope.event_type is EventType.APPROVAL_REQUEST
        assert envelope.correlation_id == CORRELATION
        payload = envelope.payload
        assert payload["request_id"] == merge_request_id(BUILD_ID)
        assert payload["request_id"] == f"merge-{BUILD_ID}"
        assert payload["agent_id"] == "merge-deploy-executor"
        assert payload["risk_level"] == "high"
        assert payload["timeout_seconds"] == 86400
        details = payload["details"]
        assert details["kind"] == "merge_deploy_offer"
        assert details["build_id"] == BUILD_ID
        assert details["feature_id"] == FEATURE_ID
        assert details["repo"] == REPO
        assert details["branch"] == f"autobuild/{FEATURE_ID}"
        assert details["expect_main_sha"] == "mainsha1234"
        assert details["tasks_completed"] == 5
        assert details["tasks_total"] == 5
        assert details["baseline_failing"] is None
        assert details["resume_options"] == ["approve", "reject"]

        # --- the pipeline build-paused mirror, SECOND ------------------
        paused = recorder.events[1][1]
        # The join key jarvis uses — deliberately NOT the real build_id.
        assert paused.build_id == f"merge-{FEATURE_ID}"
        assert paused.feature_id == FEATURE_ID
        assert paused.stage_label == MERGE_OFFER_STAGE_LABEL
        assert paused.gate_mode == "MANDATORY_HUMAN_APPROVAL"
        assert paused.coach_score is None
        assert paused.approval_subject == subject
        assert paused.correlation_id == CORRELATION
        assert "Approve = merge into main" in paused.rationale
        assert "the branch is kept" in paused.rationale
        assert "Reject = nothing changes" in paused.rationale

    @pytest.mark.asyncio
    async def test_the_card_offers_the_branch_the_build_made(
        self, config, pool
    ) -> None:
        """Part M, rule 55: the payload's ``branch`` is the branch that will be merged.

        A repair's commits land on the fix journey's own branch, recorded on
        ``builds.merge_branch`` by the conductor; the offer must name THAT,
        not the feature's ``autobuild/<feature id>`` (already on main), and
        the card says the branch because it is not the feature's own.
        """
        _insert_build(pool)
        pool.record_merge_branch(BUILD_ID, "fix/TASK-MX1FIX1-00000001")
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(_event())

        _subject, body = recorder.events[0][1]
        details = MessageEnvelope.model_validate_json(body).payload["details"]
        assert details["branch"] == "fix/TASK-MX1FIX1-00000001"
        assert details["merge_branch"] == "fix/TASK-MX1FIX1-00000001"
        paused = recorder.events[1][1]
        assert paused.rationale.startswith(
            f"{FEATURE_ID} (branch fix/TASK-MX1FIX1-00000001) built — "
        )
        assert "Approve = merge into main" in paused.rationale
        # The durable latch says the same thing the card said.
        latch = [
            s for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
        ][0].details[MERGE_OFFER_DETAILS_KEY]
        assert latch["branch"] == "fix/TASK-MX1FIX1-00000001"
        assert latch["merge_branch"] == "fix/TASK-MX1FIX1-00000001"

    @pytest.mark.asyncio
    async def test_a_feature_build_with_an_empty_column_offers_its_own_branch(
        self, config, pool
    ) -> None:
        """Rule 54: an empty ``merge_branch`` falls back to ``autobuild/<feature>``."""
        _insert_build(pool)
        assert pool.get_build_row(BUILD_ID).merge_branch is None
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(_event())

        _subject, body = recorder.events[0][1]
        details = MessageEnvelope.model_validate_json(body).payload["details"]
        assert details["branch"] == f"autobuild/{FEATURE_ID}"
        assert details["merge_branch"] is None
        paused = recorder.events[1][1]
        # The card is byte for byte what it always was: no branch named.
        assert paused.rationale.startswith(f"{FEATURE_ID} built — 5 of 5 tasks passed. ")
        assert "(branch " not in paused.rationale

    @pytest.mark.asyncio
    async def test_latch_details_carry_what_the_consumer_needs(
        self, config, pool
    ) -> None:
        _insert_build(pool)
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(_event())
        offer = _offer_rows(pool)[0].details[MERGE_OFFER_DETAILS_KEY]
        assert offer["request_id"] == f"merge-{BUILD_ID}"
        assert offer["correlation_id"] == CORRELATION
        assert offer["repo"] == REPO
        assert offer["expect_main_sha"] == "mainsha1234"
        assert offer["feature_id"] == FEATURE_ID


# ---------------------------------------------------------------------------
# The baseline read (fail-open) and the git pin
# ---------------------------------------------------------------------------


class TestBaseline:
    @pytest.mark.asyncio
    async def test_baseline_rides_the_offer(
        self, config, pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "receipts"
        (receipts / BUILD_ID / "task-1").mkdir(parents=True)
        (receipts / BUILD_ID / "task-1" / "baseline.json").write_text(
            json.dumps({"failing": ["test_a", "test_b"]}), encoding="utf-8"
        )
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(receipts))
        _insert_build(pool)
        recorder = _Recorder()
        await _service(config, pool, recorder).maybe_offer(_event())
        _, (subject, body) = recorder.events[0]
        details = MessageEnvelope.model_validate_json(body).payload["details"]
        assert details["baseline_failing"] == ["test_a", "test_b"]

    def test_garbage_baseline_fails_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "receipts"
        (receipts / BUILD_ID).mkdir(parents=True)
        (receipts / BUILD_ID / "baseline.json").write_text(
            "not json at all", encoding="utf-8"
        )
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(receipts))
        assert read_baseline_failing(BUILD_ID) is None

    def test_bare_list_baseline_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "receipts"
        (receipts / BUILD_ID).mkdir(parents=True)
        (receipts / BUILD_ID / "baseline.json").write_text(
            json.dumps(["only_one"]), encoding="utf-8"
        )
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(receipts))
        assert read_baseline_failing(BUILD_ID) == ["only_one"]

    def test_missing_tree_fails_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "nowhere"))
        assert read_baseline_failing(BUILD_ID) is None


class TestGitPin:
    @pytest.mark.asyncio
    async def test_rev_parse_main_reads_a_real_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "gitrepo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
        )
        (repo / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "one",
            ],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        sha = await git_rev_parse_main(repo)
        assert sha is not None and len(sha) == 40

    @pytest.mark.asyncio
    async def test_rev_parse_main_is_none_outside_a_repo(
        self, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert await git_rev_parse_main(empty) is None


# ---------------------------------------------------------------------------
# THE BRANCH THE CARD IS PINNED ON — the build's own recorded one
# ---------------------------------------------------------------------------


def _a_repo_on(root: Path, branch: str) -> str:
    """A real repository whose only branch is ``branch``. Returns its sha."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", branch], cwd=root, check=True, capture_output=True
    )
    (root / "a.txt").write_text("x", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "one")
    return _git(root, "rev-parse", branch)


def _record_the_target_branch(
    pool: SqliteLifecyclePersistence, branch: str, build_id: str = BUILD_ID
) -> None:
    pool.connection.execute(
        "UPDATE builds SET target_branch = ? WHERE build_id = ?", (branch, build_id)
    )
    pool.connection.commit()


def _an_offer(config: ForgeConfig, pool, recorder: "_Recorder", reader) -> MergeOfferService:
    return MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(
            publish_build_paused=recorder.publish_build_paused
        ),
        raw_publish=recorder.raw_publish,
        git_head=reader,
        git_surface=lambda _repo, _root: _CandidatePins(),
        finished_feature_reader=lambda *_a, **_k: (
            None,
            None,
            "nothing was exported for this build",
        ),
    )


class TestTheCardIsPinnedOnTheRecordedBranch:
    """Carried from stage 4a, and it had no test until now (23 September 2026).

    The card used to be pinned by reading the branch literally called ``main``,
    and NO CARD is made when the pin cannot be read — so a project whose
    recorded branch is ``trunk`` could never be offered a merge word at all.
    The offer reads the build row's own recorded branch now, and this is the
    proof of both halves, with the real reader against a real repository.
    """

    @pytest.mark.asyncio
    async def test_a_project_whose_recorded_branch_is_trunk_gets_a_card(
        self, pool, tmp_path: Path
    ) -> None:
        repo_root = tmp_path / "trunk-project"
        sha = _a_repo_on(repo_root, "trunk")
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO: str(repo_root)}},
                "merge_executor": {"enabled": True},
            }
        )
        _insert_build(pool)
        _record_the_target_branch(pool, "trunk")
        recorder = _Recorder()
        asked: list[str | None] = []

        async def _the_real_reader(root: Path, branch: str | None = None):
            asked.append(branch)
            return await git_rev_parse_main(root, branch)

        await _an_offer(config, pool, recorder, _the_real_reader).maybe_offer(_event())

        # THERE IS A CARD, and it is pinned on trunk's own commit.
        assert [kind for kind, _ in recorder.events] == ["approval", "paused"]
        assert asked == ["trunk"]
        _subject, body = recorder.events[0][1]
        details = MessageEnvelope.model_validate_json(body).payload["details"]
        assert details["expect_main_sha"] == sha
        assert len(_offer_rows(pool)) == 1

    @pytest.mark.asyncio
    async def test_a_build_with_no_recorded_branch_still_gets_a_card(
        self, pool, tmp_path: Path
    ) -> None:
        """Nothing recorded is a build from before the starting rule.

        There is nothing to read but the remote's own default, so that is what
        is read — and a card is still offered, which is the point: teaching
        this to read a recorded branch must not take the card away from every
        build that has none.
        """
        repo_root = tmp_path / "default-project"
        sha = _a_repo_on(repo_root, "main")
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO: str(repo_root)}},
                "merge_executor": {"enabled": True},
            }
        )
        _insert_build(pool)
        assert pool.get_build_row(BUILD_ID).target_branch is None
        recorder = _Recorder()
        asked: list[str | None] = []

        async def _the_real_reader(root: Path, branch: str | None = None):
            asked.append(branch)
            return await git_rev_parse_main(root, branch)

        await _an_offer(config, pool, recorder, _the_real_reader).maybe_offer(_event())

        assert [kind for kind, _ in recorder.events] == ["approval", "paused"]
        assert asked == [None], "nothing was recorded, and it says so by asking None"
        _subject, body = recorder.events[0][1]
        details = MessageEnvelope.model_validate_json(body).payload["details"]
        assert details["expect_main_sha"] == sha

    @pytest.mark.asyncio
    async def test_a_head_reader_that_predates_the_branch_still_makes_a_card(
        self, config, pool
    ) -> None:
        """A caller that bound the old one-argument seam is not a crash.

        :func:`head_reader_takes_a_branch` is asked, the old call is made, and
        the log says the pin was read the way it always was.
        """
        _insert_build(pool)
        _record_the_target_branch(pool, "trunk")
        recorder = _Recorder()
        calls: list[tuple] = []

        async def _the_old_seam(root: Path):
            calls.append((root,))
            return "oldshapesha"

        assert head_reader_takes_a_branch(_the_old_seam) is False
        await _an_offer(config, pool, recorder, _the_old_seam).maybe_offer(_event())

        assert len(calls) == 1
        assert [kind for kind, _ in recorder.events] == ["approval", "paused"]
        _subject, body = recorder.events[0][1]
        details = MessageEnvelope.model_validate_json(body).payload["details"]
        assert details["expect_main_sha"] == "oldshapesha"

    @pytest.mark.asyncio
    async def test_which_seams_are_asked_for_the_branch_and_which_are_not(
        self,
    ) -> None:
        """Both halves of the question :func:`_ask_the_head_reader` asks."""

        async def _takes_one(root: Path):
            return "one"

        async def _takes_a_branch(root: Path, branch: str | None = None):
            return f"two:{branch}"

        async def _takes_kwargs(root: Path, **kw):
            return f"kw:{kw.get('branch')}"

        assert head_reader_takes_a_branch(_takes_one) is False
        assert head_reader_takes_a_branch(_takes_a_branch) is True
        assert head_reader_takes_a_branch(_takes_kwargs) is True
        assert await _ask_the_head_reader(_takes_one, Path("."), "trunk") == "one"
        assert (
            await _ask_the_head_reader(_takes_a_branch, Path("."), "trunk")
            == "two:trunk"
        )


class TestRetainedCandidateIdentity:
    @pytest.mark.asyncio
    async def test_offer_pins_candidate_and_registered_retained_worktree(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        repo_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from forge.deploy.candidate_tree import InContainerCandidateGit
        from forge.subagents.autobuild_worktree_lifecycle import (
            inspect_autobuild_worktree,
        )

        build_id = BUILD_ID
        _git(repo_root, "init", "-b", "main", "-q")
        (repo_root / "README.md").write_text("main\n", encoding="utf-8")
        _git(repo_root, "add", "README.md")
        _git(repo_root, "commit", "-q", "-m", "main")
        _git(repo_root, "branch", f"autobuild/{FEATURE_ID}")
        base = tmp_path / "autobuild-worktrees"
        outer = base / build_id
        monkeypatch.setenv("FORGE_AUTOBUILD_WORKTREE_BASE", str(base))
        _git(repo_root, "worktree", "add", "--detach", str(outer), "main")
        inner = outer / ".guardkit/worktrees/TASK-MO-001"
        inner.parent.mkdir(parents=True)
        _git(repo_root, "worktree", "add", str(inner), f"autobuild/{FEATURE_ID}")
        (inner / "offered-note.txt").write_text("keep through offer\n")
        retained = inspect_autobuild_worktree(
            repo=repo_root, base=base, build_id=build_id, path=outer
        )
        assert retained["ok"] is True

        _insert_build(pool)
        event = _event()
        object.__setattr__(event, "worktree_retention", retained)
        recorder = _Recorder()
        await _service(
            config,
            pool,
            recorder,
            git_surface=lambda _repo, root: InContainerCandidateGit(root),
        ).maybe_offer(event)

        offer = _offer_rows(pool)[0].details[MERGE_OFFER_DETAILS_KEY]
        candidate_sha = _git(repo_root, "rev-parse", f"autobuild/{FEATURE_ID}")
        assert offer["candidate_identity_version"] == 1
        assert offer["candidate_sha"] == candidate_sha
        assert offer["candidate_tree"] == _git(
            repo_root, "rev-parse", f"{candidate_sha}^{{tree}}"
        )
        assert offer["worktree_retention"]["path"] == str(outer)
        assert offer["worktree_retention"]["nested_registrations"][0][
            "path"
        ] == str(inner)
        assert pool.get_build_row(build_id).worktree_path == str(outer)
        assert outer.is_dir() and inner.is_dir()


# ---------------------------------------------------------------------------
# When planning registered no after-deploy check (4 October 2026)
#
# Planning records on its own run when it could not register a feature's
# after-deploy check (a PATCH address, a placeholder in the address, no
# address named, and so on). The routine card now says so in one sentence,
# read from that record; when a check was registered, or nothing can be
# read, the card is byte for byte what it was.
# ---------------------------------------------------------------------------

from forge.pipeline.merge_offer import (  # noqa: E402
    NO_AFTER_DEPLOY_CHECK_SENTENCE,
    read_after_deploy_check_skip,
    what_was_checked,
)

_APPROVE_LINE = (
    "Approve = merge into main, deploy to the sandbox and run the checks; "
    "the branch is kept either way. Reject = nothing changes."
)

_PATCH_SKIP = {
    "skipped": True,
    "reason": "the spec names PATCH /users/{user_id}/deactivate; only a GET "
    "address can be checked automatically — no gate registered",
    "reason_code": "unsupported_method",
    "feature_id": FEATURE_ID,
    "address": {"method": "PATCH", "path": "/users/{user_id}/deactivate"},
}

_PATCH_SENTENCE = (
    "Planning did not register an after-deploy check for this feature "
    "automatically (reason: only GET addresses are supported, and this one "
    "is PATCH)."
)


def _record_planning_run(pool: SqliteLifecyclePersistence, correlation_id: str) -> None:
    pool.connection.execute(
        "INSERT INTO planning_runs (correlation_id, state, originating_user, "
        "expected_approver, request_text, target_repo, triggered_by, "
        "originating_adapter, parent_request_id, queued_at) VALUES "
        "(?, 'BUILD_QUEUED', 'rich', 'rich', 'a sentence', ?, 'jarvis', "
        "'slack', NULL, '2026-10-04T00:00:00Z')",
        (correlation_id, REPO),
    )
    pool.connection.commit()


def _record_gate_step(
    pool: SqliteLifecyclePersistence, correlation_id: str, details: dict[str, Any]
) -> None:
    pool.connection.execute(
        "INSERT INTO planning_run_events (correlation_id, stage_label, status, "
        "actor_identity, details_json, recorded_at) VALUES "
        "(?, 'qa-feature-gate', 'approved', 'planning-driver', ?, "
        "'2026-10-04T00:00:00Z')",
        (correlation_id, json.dumps(details)),
    )
    pool.connection.commit()


def _card_service(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    recorder: _Recorder,
    **kwargs: Any,
) -> MergeOfferService:
    """The routine card's service with the scope pass and the finished-feature
    reading pinned, so the only thing that can differ is this sentence."""

    async def _git_head(_repo_root: Path) -> str | None:
        return "mainsha1234"

    return MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(
            publish_build_paused=recorder.publish_build_paused
        ),
        raw_publish=recorder.raw_publish,
        git_head=_git_head,
        git_surface=lambda _repo, _root: _CandidatePins(),
        finished_feature_reader=lambda *_a, **_k: (
            None,
            None,
            "nothing was exported for this build",
        ),
        scope_pass=lambda **_k: None,
        **kwargs,
    )


def _card_as_it_was() -> str:
    """The routine card before this sentence existed, built from its parts."""
    checked = what_was_checked(None, None, "nothing was exported for this build")
    lines = [f"{FEATURE_ID} built — 5 of 5 tasks passed."]
    lines.extend(line for line in checked.lines if line)
    lines.append(_APPROVE_LINE)
    return "\n".join(lines)


async def _routine_card(config, pool, **kwargs: Any) -> str:
    recorder = _Recorder()
    await _card_service(config, pool, recorder, **kwargs).maybe_offer(_event())
    paused = [payload for kind, payload in recorder.events if kind == "paused"]
    assert len(paused) == 1
    return paused[0].rationale


class TestTheAfterDeployCheckSentenceOnTheRoutineCard:
    @pytest.mark.asyncio
    async def test_a_skip_puts_the_sentence_with_its_reason_on_the_card(
        self, config, pool
    ) -> None:
        _insert_build(pool)
        _record_planning_run(pool, CORRELATION)
        _record_gate_step(pool, CORRELATION, _PATCH_SKIP)

        words = await _routine_card(config, pool)

        lines = words.split("\n")
        assert lines[-2] == _PATCH_SENTENCE
        assert lines[-1] == _APPROVE_LINE
        # Only the sentence was added; every other line is as it was.
        assert "\n".join(lines[:-2] + lines[-1:]) == _card_as_it_was()
        # It says what planning did, never that no other check exists.
        assert "no check" not in words.lower()

    @pytest.mark.asyncio
    async def test_a_registered_gate_leaves_the_card_byte_for_byte(
        self, config, pool
    ) -> None:
        _insert_build(pool)
        _record_planning_run(pool, CORRELATION)
        _record_gate_step(
            pool,
            CORRELATION,
            {
                "feature_id": FEATURE_ID,
                "gate_file": "qa/gates/active_count_gate.py",
                "endpoint": {"method": "GET", "path": "/users/active-count"},
            },
        )

        assert await _routine_card(config, pool) == _card_as_it_was()

    @pytest.mark.asyncio
    async def test_an_older_free_text_record_still_gives_a_sentence(
        self, config, pool
    ) -> None:
        _insert_build(pool)
        _record_planning_run(pool, CORRELATION)
        _record_gate_step(
            pool,
            CORRELATION,
            {"skipped": True, "reason": "no derivable endpoint — no gate registered"},
        )

        words = await _routine_card(config, pool)

        assert words.split("\n")[-2] == (
            "Planning did not register an after-deploy check for this feature "
            "automatically (reason: no address it could check was found)."
        )

    @pytest.mark.asyncio
    async def test_a_repair_build_finds_its_parent_s_planning_run(
        self, config, pool
    ) -> None:
        parent = "build-FEAT-MO1-20260823"
        _insert_build(pool, build_id=parent)
        _insert_build(pool, correlation_id=f"fix-{parent}")
        _record_planning_run(pool, CORRELATION)
        _record_gate_step(pool, CORRELATION, _PATCH_SKIP)

        words = await _routine_card(config, pool)

        assert words.split("\n")[-2] == _PATCH_SENTENCE

    @pytest.mark.asyncio
    async def test_no_planning_record_leaves_the_card_and_says_why_in_the_log(
        self, config, pool, caplog: pytest.LogCaptureFixture
    ) -> None:
        _insert_build(pool)

        with caplog.at_level("INFO", logger="forge.pipeline.merge_offer"):
            words = await _routine_card(config, pool)

        assert words == _card_as_it_was()
        assert any(
            "has no record of the step" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_a_reader_that_raises_leaves_the_card_and_is_logged(
        self, config, pool, caplog: pytest.LogCaptureFixture
    ) -> None:
        _insert_build(pool)
        _record_planning_run(pool, CORRELATION)
        _record_gate_step(pool, CORRELATION, _PATCH_SKIP)

        def _falls_over(_pool: Any, _row: Any) -> Any:
            raise RuntimeError("the ledger is locked")

        with caplog.at_level("WARNING", logger="forge.pipeline.merge_offer"):
            words = await _routine_card(
                config, pool, after_deploy_check_reader=_falls_over
            )

        assert words == _card_as_it_was()
        warned = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("could not be read" in r.getMessage() for r in warned)

    def test_an_unreadable_record_gives_nothing_and_is_logged(
        self, pool, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The shared reader itself never raises: a record that is not JSON
        is a logged warning and no sentence."""
        _insert_build(pool)
        _record_planning_run(pool, CORRELATION)
        pool.connection.execute(
            "INSERT INTO planning_run_events (correlation_id, stage_label, "
            "status, details_json, recorded_at) VALUES (?, 'qa-feature-gate', "
            "'approved', '{not json', '2026-10-04T00:00:00Z')",
            (CORRELATION,),
        )
        pool.connection.commit()

        with caplog.at_level("WARNING", logger="forge.pipeline.merge_offer"):
            skip = read_after_deploy_check_skip(pool, pool.get_build_row(BUILD_ID))

        assert skip is None
        assert any("could not be read" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "details,reason",
    [
        (_PATCH_SKIP, "only GET addresses are supported, and this one is PATCH"),
        (
            {
                "skipped": True,
                "reason_code": "placeholder_in_address",
                "address": {"method": "GET", "path": "/users/{user_id}"},
            },
            "the address has a placeholder, {user_id}",
        ),
        (
            {
                "skipped": True,
                "reason_code": "placeholder_in_address",
                "address": {"method": "GET", "path": "/orgs/{org_id}/users/{user_id}"},
            },
            "the address has placeholders, {org_id} and {user_id}",
        ),
        (
            {"skipped": True, "reason_code": "no_endpoint_named", "address": None},
            "the spec named no address",
        ),
        (
            {"skipped": True, "reason_code": "no_pass_bars"},
            "the plan registered no pass bars",
        ),
        (
            {"skipped": True, "reason_code": "no_template"},
            "the project has no gate template",
        ),
        (
            {"skipped": True, "reason_code": "no_registry"},
            "the project has no gate registry",
        ),
        # older records: the one jargon wording gets plain words, any other
        # text is passed on exactly as it was recorded
        (
            {"skipped": True, "reason": "no derivable endpoint — no gate registered"},
            "no address it could check was found",
        ),
        (
            {
                "skipped": True,
                "reason": "the plan registered no pass bars — no gate "
                "pass_bar_ref to anchor; no gate registered",
            },
            "the plan registered no pass bars — no gate pass_bar_ref to "
            "anchor; no gate registered",
        ),
    ],
)
def test_the_reason_words(
    pool: SqliteLifecyclePersistence, details: dict[str, Any], reason: str
) -> None:
    _insert_build(pool)
    _record_planning_run(pool, CORRELATION)
    _record_gate_step(pool, CORRELATION, details)

    skip = read_after_deploy_check_skip(pool, pool.get_build_row(BUILD_ID))

    assert skip is not None
    assert skip.reason == reason
    assert skip.planning_run == CORRELATION
    assert skip.sentence == NO_AFTER_DEPLOY_CHECK_SENTENCE.format(reason=reason)
