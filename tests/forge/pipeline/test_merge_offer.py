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
    approval_subject_for,
    git_rev_parse_main,
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
            f"{FEATURE_ID} (branch fix/TASK-MX1FIX1-00000001) built clean — "
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
        assert paused.rationale.startswith(f"{FEATURE_ID} built clean — 5 of 5 tasks passed. ")
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
