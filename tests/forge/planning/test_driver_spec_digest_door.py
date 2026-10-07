"""The machine chain's ONE pause: the spec digest review door.

The brief-stage card asked a person about a product brief before any spec
existed, and then the chain wrote the spec, the plan and the checklists and
queued the build without anybody reading what would be built. Stage 2 moves that
one question to where there is something to check — right after the spec — and
changes what it shows: one plain sentence per worked example, mechanically
proven against the examples themselves.

What this file proves:

* the brief card no longer opens on the machine chain, and the run drives on
  from a durable row that says the pause was ABSORBED, not skipped;
* the digest card opens, threads under the run's own anchor, and speaks plain
  language with the examples one click deeper;
* a NOTE rewrites the spec — the owner's words reaching the spec-writer VERBATIM
  with the prior artifact set — and comes back with a fresh card;
* three cards is the whole budget, and past it the run stops LOUDLY quoting
  every note back;
* a restart re-opens the SAME card, word for word;
* the digest is re-proven against the COMMITTED spec, and a mismatch stops the
  run rather than showing a summary nobody can trust;
* an auth-flagged run pauses ONCE: the sign-in question rides the digest card
  and the quality-checklist leg opens no second door — and BOTH answers to that
  question are real. "No sign-in here" carries on; "yes, there is one" takes the
  2026-07-31 attended-registration terminal word for word; setting it aside is
  never read as a yes; and a note still only ever means rewrite the spec;
* the "show me" text is the raw spec, and this file says so out loud, because
  the surface that renders it has a decision to make about that;
* the spec text reaches the durable event log once per card, not once per row.

Real v4 SQLite store, real gate adapters, fakes at the wire seams. No broker, no
network: every subscription is an in-test double.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.git.models import GitOpResult
from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import (
    PlanningConfig,
    PlanningDigestReviewConfig,
    TargetTerminalConfig,
)
from forge.gating.identity import derive_request_id
from forge.lifecycle import migrations
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from forge.planning.target_terminal_tools import ToolOutcome
from nats_core.events import ApprovalResponsePayload, AssumptionDisposition

CID = "digest-run-0001"
PLAN_RUN_ID = f"plan-{CID}"
ORIGINATOR = "U0RIGINATOR"
TARGET_REPO = "guardkit/api_test"
SLUG = "version-endpoint"
BRANCH = f"planning/{CID}"

_DIGEST_STAGE = "feature-spec-digest-review"
_DIGEST_CHECKPOINT_TYPE = "product_docs_spec_digest"
_DRAFT_STAGE = "feature-spec-draft"
_SPEC_STAGE = "feature-spec"
_BARS_STAGE = "qa-pass-bars"
_AUTH_DOOR_STAGE = "qa-pass-bars-auth-confirm"
#: The id the sign-in question rides under on the card and in the answer. Named
#: here rather than imported so a rename has to be a deliberate two-sided act:
#: this string is a CONTRACT with whatever renders the card.
_SIGN_IN_ITEM = "sign-in"

FEATURE_TEXT = (
    "Feature: version endpoint\n"
    "\n"
    "  @key-example @smoke\n"
    "  Scenario: Version endpoint returns the running build\n"
    "    Given the service is running\n"
    "    When the version is asked for\n"
    "    Then the build it started from comes back\n"
    "\n"
    "  @negative\n"
    "  Scenario: Version endpoint rejects an unknown format\n"
    "    Given the service is running\n"
    "    When an unpublished format is asked for\n"
    "    Then the request is refused\n"
)

ASSUMPTIONS_YAML = (
    "assumptions:\n"
    "- id: ASSUM-001\n"
    "  assumption: The version string comes from the build metadata.\n"
    "  basis: common practice; the input did not say\n"
)

DIGEST_YAML = (
    f"feature: {SLUG}\n"
    "generated: '2026-08-14T10:00:00Z'\n"
    "scenarios:\n"
    "- title: Version endpoint returns the running build\n"
    "  tags:\n"
    "  - '@key-example'\n"
    "  - '@smoke'\n"
    "  sentence: Asking the service which version it is running returns the build\n"
    "    it was started from.\n"
    "- title: Version endpoint rejects an unknown format\n"
    "  tags:\n"
    "  - '@negative'\n"
    "  sentence: Asking for the version in a format the service does not publish is\n"
    "    refused rather than guessed at.\n"
    "assumptions:\n"
    "- id: ASSUM-001\n"
    "  text: The version string comes from the build metadata.\n"
    "  basis: common practice; the input did not say\n"
)

_AUTHLESS_SEED = (
    "format_version: '2.0'\n"
    f"feature_slug: {SLUG}\n"
    "auth_surface_bearing: false\n"
    "preconditions:\n"
    "- suite_green_vs_ledger\n"
    "criteria:\n"
    "- id: ver-AC-001\n"
    "  text: A GET request to /version returns the running build\n"
    "  class: machine\n"
    "  evidence_kind: json\n"
    "  runbook_ref: null\n"
)

_AUTH_SEED = _AUTHLESS_SEED.replace(
    "auth_surface_bearing: false",
    "auth_surface_bearing: true\nauth_surface_basis: |\n"
    "  the spec mentions a bearer token when explaining it needs none",
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    cx = sqlite_connect.connect_writer(tmp_path / "digest.db")
    migrations.apply_at_boot(cx)
    return SqlitePlanningRunStore(cx, target_terminal_enabled=True)


class FakePublisher:
    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def publish_request(self, envelope: Any) -> None:
        self.envelopes.append(envelope)


class RefusingPublisher(FakePublisher):
    """A wire that refuses the digest card — nobody can ever be asked."""

    async def publish_request(self, envelope: Any) -> None:
        self.envelopes.append(envelope)
        if envelope.payload["details"].get("checkpoint_type") == (
            _DIGEST_CHECKPOINT_TYPE
        ):
            raise RuntimeError("broker refused the digest card")


class FakeSecondOpinion:
    async def get_summary_for_approval(self, **kwargs: Any) -> dict[str, Any]:
        return {"title": "PO docs"}


class ScriptedSubscriber:
    def __init__(self, script: list[Any], armed: asyncio.Event | None) -> None:
        self._script = script
        self._armed = armed

    async def await_response(self, build_id: str, **kwargs: Any) -> Any:
        if self._armed is not None:
            self._armed.set()
        if not self._script:
            return None
        return self._script.pop(0)


class SharedScriptFactory:
    """One shared answer script across every wait in the run, in order."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)

    def __call__(self, expected_approver: Any, armed: Any) -> ScriptedSubscriber:
        return ScriptedSubscriber(self.script, armed)


class _EmptyRepository:
    """A repository the planner's fact sheet CAN read, and which tracks
    nothing — so the card says only what these tests are about."""

    where = "the stand-in repository"

    def list_files(self) -> list[str]:
        return []

    def files_mentioning(
        self, text: str, *, ignore_case: bool = False, relevant: Any = None
    ) -> list[str]:
        return []

    def read_text(self, path: str) -> str | None:
        return None


class RecordingGitRunner:
    """Records tree writes, runs the pre-commit hook, serves files back."""

    def __init__(self) -> None:
        self.tree_calls: list[dict[str, Any]] = []
        self._branch_files: dict[str, dict[str, str]] = {}
        #: What the planner's fact sheet reads (the 1 October planner fix): by
        #: default a readable, empty repository. A test that needs the
        #: repository unreachable puts its own reader here.
        self.reader: Any = _EmptyRepository()

    def code_reader(self, commit: str | None = None) -> Any:
        #: The commit the planner asked to read at (7 October 2026): the
        #: run's recorded start.
        self.read_at = commit
        return self.reader

    async def fetch_remote_start_point(self, repo_path: str) -> Any:
        from forge.deploy.candidate_tree import RemoteStartPoint

        return RemoteStartPoint(branch="main", commit="0" * 39 + "1")

    async def read_file_at_commit(
        self, repo_path: str, commit: str, file_path: str
    ) -> Any:
        """The memory rule's read (item 2): this stand-in project declares a
        name, so the door lets the run through to what these tests are about."""
        from forge.deploy.candidate_tree import FileAtCommit

        return FileAtCommit(
            content="memory:\n  project: scratch_project\n", found=True
        )

    async def prepare_branch_and_write(
        self,
        repo_path: str,
        branch: str,
        file_path: str,
        content: str,
        *,
        start_commit: str | None = None,
    ) -> GitOpResult:
        self.start_commit_seen = start_commit
        self._branch_files.setdefault(branch, {})[file_path] = content
        return GitOpResult(
            status="success",
            operation="prepare_branch_and_write",
            sha="handoff-sha",
            exit_code=0,
        )

    async def read_file_from_branch(
        self, *, repo_path: str, branch: str, file_path: str
    ) -> str | None:
        return self._branch_files.get(branch, {}).get(file_path)

    async def prepare_branch_and_write_tree(
        self,
        repo_path: str,
        branch: str,
        files: Any,
        message: str,
        *,
        pre_commit: Any = None,
        **_launch: Any,
    ) -> GitOpResult:
        import tempfile

        if pre_commit is not None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                for rel, content in files.items():
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                result = await pre_commit(root)
                if not result.ok:
                    return GitOpResult(
                        status="failed",
                        operation="prepare_branch_and_write_tree",
                        stderr=f"pre-commit refused: {result.detail}",
                        exit_code=-1,
                    )
                # The normalizer rewrites the .feature IN PLACE; whatever is on
                # disk after the hook is what is committed.
                files = {
                    rel: (root / rel).read_text(encoding="utf-8") for rel in files
                }
        self.tree_calls.append({"branch": branch, "files": dict(files)})
        self._branch_files.setdefault(branch, {}).update(
            {str(k): str(v) for k, v in files.items()}
        )
        return GitOpResult(
            status="success",
            operation="prepare_branch_and_write_tree",
            sha="tree-sha",
            exit_code=0,
        )


def _spec_reply(
    *,
    seed: str = _AUTHLESS_SEED,
    digest: str | None = DIGEST_YAML,
    feature: str = FEATURE_TEXT,
    assumptions: str = ASSUMPTIONS_YAML,
    validation_errors: list[str] | None = None,
) -> Any:
    role_output: dict[str, Any] = {
        f"{SLUG}.feature": feature,
        f"{SLUG}_assumptions.yaml": assumptions,
        f"{SLUG}_summary.md": "# summary\n",
        f"pass-bar-seed-{SLUG}.yaml": seed,
        "validation.json": json.dumps(
            {
                "accepted": not validation_errors,
                "errors": validation_errors or [],
                "gates_run": ["gherkin_backstop", "spec_digest"],
            }
        ),
    }
    if digest is not None:
        role_output[f"{SLUG}_digest.yaml"] = digest
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"), role_output=role_output, reason=None
    )


def _plan_reply(feature_id: str) -> Any:
    plan_files = {
        f".guardkit/features/{feature_id}.yaml": (
            f"id: {feature_id}\ntasks:\n- id: TASK-VER-001\n"
        ),
        f"tasks/backlog/{SLUG}/TASK-VER-001.md": "# task\n",
    }
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            **plan_files,
            "validation.json": json.dumps(
                {"accepted": True, "errors": [], "gates_run": ["feature_validate"]}
            ),
            "semantic_review.json": json.dumps(
                {
                    "schema_version": 1,
                    "decision": "approved",
                    "criterion": "request_traceability",
                    "criterion_score": 1.0,
                    "coach_verdict": "GOOD",
                    "artifact_identity": PlanningRunDriver._plan_artifact_identity(
                        plan_files
                    ),
                    "reviewed_after_rewrite": False,
                }
            ),
        },
        reason=None,
    )


class _Harness:
    def __init__(self, driver: PlanningRunDriver, ctx: dict[str, Any]) -> None:
        self.driver = driver
        self.ctx = ctx


def _make_driver(
    store: SqlitePlanningRunStore,
    *,
    subscriber_factory: Any,
    spec_replies: list[Any] | None = None,
    publisher: FakePublisher | None = None,
    originator_wait_seconds: int = 3600,
    digest_review: PlanningDigestReviewConfig | None = None,
    normalize: Any | None = None,
    git: Any | None = None,
    target_terminal_enabled: bool = True,
    classify: Any | None = None,
) -> _Harness:
    from datetime import UTC, datetime

    def clock() -> datetime:
        return datetime.now(UTC)

    repository, state_machine = build_planning_gate_adapters(store, clock=clock)
    publisher = publisher or FakePublisher()
    notifications: list[tuple[str, str, str]] = []
    dispatches: list[dict[str, Any]] = []
    replies = list(spec_replies or [_spec_reply()])

    async def dispatch_po(*, plan_run_id: str, correlation_id: str, **_: Any) -> Any:
        return SimpleNamespace(
            outcome=SimpleNamespace(value="completed"),
            coach_score=0.9,
            criterion_breakdown=[],
            detection_findings=(),
            role_output={"title": "docs", "problem_statement": "ship a thing"},
            reason=None,
        )

    async def dispatch_spec(
        *,
        plan_run_id: str,
        correlation_id: str,
        spec_input: str,
        revision_of: dict[str, str] | None = None,
        validate_feedback: str | None = None,
        request_text: str | None = None,
        repository_facts: str | None = None,
    ) -> Any:
        dispatches.append(
            {
                "revision_of": revision_of,
                "validate_feedback": validate_feedback,
                "request_text": request_text,
                "repository_facts": repository_facts,
            }
        )
        return replies[min(len(dispatches) - 1, len(replies) - 1)]

    async def dispatch_plan(*, feature_id: str, **_: Any) -> Any:
        return _plan_reply(feature_id)

    async def _normalize(worktree: Path, feature_rel: str) -> ToolOutcome:
        if normalize is not None:
            return await normalize(worktree, feature_rel)
        return ToolOutcome(ok=True)

    async def _validate(worktree: Path, feature_id: str) -> ToolOutcome:
        return ToolOutcome(ok=True)

    async def _validate_pass_bar(worktree: Path, bar_rel: str) -> ToolOutcome:
        return ToolOutcome(ok=True)

    async def _validate_gate_registry(worktree: Path, registry_rel: str) -> ToolOutcome:
        return ToolOutcome(ok=True)

    build_triggers: list[str] = []

    async def dispatch_build_trigger(*, feature_id: str, **_: Any) -> Any:
        from forge.planning.driver import BuildTriggerResult

        build_triggers.append(feature_id)
        return BuildTriggerResult(queued=True, build_id="build-1")

    async def publish_notification(cid: str, message: str, level: str) -> None:
        notifications.append((cid, message, level))

    cfg = PlanningConfig(
        enabled=True,
        target_repo_paths={TARGET_REPO: "/srv/repos/api_test"},
        target_terminal=TargetTerminalConfig(enabled=target_terminal_enabled),
        originator_wait_seconds=originator_wait_seconds,
        **({"digest_review": digest_review} if digest_review else {}),
    )
    git = git or RecordingGitRunner()
    deps = PlanningDriverDeps(
        store=store,
        repository=repository,
        state_machine=state_machine,
        approval_publisher=publisher,
        subscriber_factory=subscriber_factory,
        dispatch_product_owner=dispatch_po,
        second_opinion_provider=FakeSecondOpinion(),
        git_runner=git,
        planning_config=cfg,
        clock=clock,
        publish_notification=publish_notification,
        dispatch_feature_spec=dispatch_spec,
        dispatch_feature_plan=dispatch_plan,
        normalize_feature_spec=_normalize,
        validate_feature_plan=_validate,
        validate_pass_bar=_validate_pass_bar,
        validate_gate_registry=_validate_gate_registry,
        dispatch_build_trigger=dispatch_build_trigger,
        # THE PROVABILITY CHECK BEFORE THE CARD (Part K): unwired by default —
        # the not-wired path, receipted; the provability tests inject a fake.
        classify_scenarios=classify,
    )
    return _Harness(
        PlanningRunDriver(deps),
        {
            "notifications": notifications,
            "dispatches": dispatches,
            "publisher": publisher,
            "git": git,
            "build_triggers": build_triggers,
        },
    )


def _queue_with_anchor(store: SqlitePlanningRunStore, parent_request_id: str) -> None:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="add a GET /version endpoint",
        triggered_by="jarvis",
        target_repo=TARGET_REPO,
        parent_request_id=parent_request_id,
    )


def _queue(store: SqlitePlanningRunStore) -> None:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="add a GET /version endpoint",
        triggered_by="jarvis",
        target_repo=TARGET_REPO,
    )


def _digest_request_id(attempt: int = 0) -> str:
    return derive_request_id(
        build_id=PLAN_RUN_ID, stage_label=_DIGEST_STAGE, attempt_count=attempt
    )


def _answer(
    decision: str,
    *,
    notes: str | None = None,
    attempt: int = 0,
    decided_by: str = ORIGINATOR,
    sign_in: str | None = None,
) -> ApprovalResponsePayload:
    """One owner answer on the wire.

    ``sign_in`` is their answer to the sign-in question when the card carried
    it: it rides in the payload's own per-item ``dispositions`` field, the same
    structured channel the assumption dialogue already publishes through — not
    in the note, which at this door means "rewrite the spec".
    """
    return ApprovalResponsePayload(
        request_id=_digest_request_id(attempt),
        decision=decision,
        decided_by=decided_by,
        notes=notes,
        dispositions=(
            [
                AssumptionDisposition(
                    assumption_id=_SIGN_IN_ITEM, disposition=sign_in
                )
            ]
            if sign_in
            else None
        ),
    )


def _events(store: SqlitePlanningRunStore, stage_label: str) -> list[tuple[str, dict]]:
    return [
        (e["status"], json.loads(e["details_json"] or "{}"))
        for e in store.list_events(CID)
        if e["stage_label"] == stage_label
    ]


def _digest_cards(h: _Harness) -> list[Any]:
    return [
        env
        for env in h.ctx["publisher"].envelopes
        if env.payload["details"].get("checkpoint_type") == _DIGEST_CHECKPOINT_TYPE
    ]


# ---------------------------------------------------------------------------
# The pause moves — and stays ONE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_brief_card_never_opens_and_the_run_says_why(
    store: SqlitePlanningRunStore,
) -> None:
    """The brief-stage question is ABSORBED, not skipped: no brief card reaches
    the wire, and the durable row names where the question went instead."""
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    kinds = {
        env.payload["details"].get("checkpoint_type")
        for env in h.ctx["publisher"].envelopes
    }
    assert kinds == {_DIGEST_CHECKPOINT_TYPE}, "the brief card must not be posted"

    cleared = [
        details
        for status, details in _events(store, "product_docs")
        if status == "checkpoint_cleared"
    ]
    assert len(cleared) == 1
    assert cleared[0]["outcome"] == "absorbed"
    assert cleared[0]["absorbed_into"] == _DIGEST_STAGE


@pytest.mark.asyncio
async def test_exactly_one_card_is_ever_put_in_front_of_a_person(
    store: SqlitePlanningRunStore,
) -> None:
    """One pause, counted END TO END: from the request to a queued build there
    is exactly one card and exactly one approval on the record."""
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))

    await h.driver.drive(CID)

    assert len(h.ctx["publisher"].envelopes) == 1
    assert len(h.ctx["build_triggers"]) == 1
    # Exactly one door was ever opened in the whole run — every door, of every
    # kind, records its opening as a GATED row.
    openings = [e["stage_label"] for e in store.list_events(CID) if e["status"] == "GATED"]
    assert openings == [_DIGEST_STAGE]
    # ...and the run never entered the PAUSED state on the way, because the one
    # pause is an inline door, not a half-paused row.
    assert [
        e["stage_label"]
        for e in store.list_events(CID)
        if e["status"] == "checkpoint_cleared" and e["actor_identity"] == ORIGINATOR
    ] == []


@pytest.mark.asyncio
async def test_the_flag_off_path_keeps_the_brief_pause_untouched(
    tmp_path: Path,
) -> None:
    """With the machine chain OFF nothing moves: the brief checkpoint pauses
    exactly as it does today and no digest card exists."""
    cx = sqlite_connect.connect_writer(tmp_path / "off.db")
    migrations.apply_at_boot(cx)
    off_store = SqlitePlanningRunStore(cx, target_terminal_enabled=False)
    _queue(off_store)
    h = _make_driver(
        off_store,
        subscriber_factory=SharedScriptFactory([]),
        target_terminal_enabled=False,
        originator_wait_seconds=1,
    )

    await h.driver.drive(CID)

    # The brief card is posted exactly as it is today, and no digest card
    # exists at all — nothing about the old path moved.
    kinds = [
        env.payload["details"].get("checkpoint_type")
        for env in h.ctx["publisher"].envelopes
    ]
    assert kinds and all(kind == "product_docs" for kind in kinds)
    assert _digest_cards(h) == []
    # Nothing was absorbed: the only way this checkpoint clears is a real answer.
    assert [
        details.get("outcome")
        for status, details in _events(off_store, "product_docs")
        if status == "checkpoint_cleared"
    ] == []


# ---------------------------------------------------------------------------
# What the card says
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_card_is_one_sentence_per_example_in_plain_language(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))

    await h.driver.drive(CID)

    cards = _digest_cards(h)
    assert len(cards) == 1
    details = cards[0].payload["details"]
    assert details["expected_approver"] == ORIGINATOR
    summary = details["summary"]

    sentences = [row["sentence"] for row in summary["what_it_will_do"]]
    assert sentences == [
        "Asking the service which version it is running returns the build it was "
        "started from.",
        "Asking for the version in a format the service does not publish is refused "
        "rather than guessed at.",
    ]
    # The labels travel verbatim; turning them into words a person reads is the
    # card renderer's job, and it renders only the ones it has words for.
    assert summary["what_it_will_do"][0]["tags"] == ["@key-example", "@smoke"]
    # The assumption AND its reason — the half that says whether to trust it.
    assert summary["what_the_machine_assumed"] == [
        {
            "assumption": "The version string comes from the build metadata.",
            "why": "common practice; the input did not say",
        }
    ]
    # The worked examples ride one click deeper — never the ask.
    assert summary["worked_examples"] == FEATURE_TEXT
    # The button must not claim the tap starts a build. It does not.
    assert "Nothing is built yet." in summary["approve_means"]
    assert "build this" not in summary["approve_means"]
    # No internal vocabulary anywhere a person reads.
    readable = json.dumps(
        {k: v for k, v in summary.items() if k != "worked_examples"}
    ).lower()
    for internal in ("gherkin", "passbar", "pass-bar", "forge", "coach", "007"):
        assert internal not in readable


@pytest.mark.asyncio
async def test_digest_card_carries_target_repo(
    store: SqlitePlanningRunStore,
) -> None:
    """The card names the repository this build will land in (rule 5).

    The owner approves a spec at this card; which repository it will be built
    in is part of what they are agreeing to, so it travels on the card rather
    than being knowable only from the logs. A renderer that does not know the
    field shows the card exactly as before.
    """
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))

    await h.driver.drive(CID)

    summary = _digest_cards(h)[0].payload["details"]["summary"]
    assert summary["target_repo"] == TARGET_REPO


@pytest.mark.asyncio
async def test_the_card_threads_under_the_runs_own_anchor(
    store: SqlitePlanningRunStore,
) -> None:
    _queue_with_anchor(store, "parent-abc")
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))

    await h.driver.drive(CID)

    assert _digest_cards(h)[0].payload["details"]["parent_request_id"] == "parent-abc"


# ---------------------------------------------------------------------------
# The note channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_note_rewrites_the_spec_and_comes_back_with_a_fresh_card(
    store: SqlitePlanningRunStore,
) -> None:
    """The owner's red pen is a sentence, not an edit: their words reach the
    spec-writer VERBATIM, with the prior artifact set, and a new card follows."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [
                _answer("reject", notes="the second example should be a 404, not a 400"),
                _answer("approve", attempt=1),
            ]
        ),
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(_digest_cards(h)) == 2

    first, second = h.ctx["dispatches"]
    assert first["revision_of"] is None and first["validate_feedback"] is None
    # ...and, since 2026-09-13, the coach's ground truth rides the first
    # round too: the run's own sentence, word for word.
    assert first["request_text"] == "add a GET /version endpoint"
    assert second["validate_feedback"] == (
        "the second example should be a 404, not a 400"
    )
    # The prior artifact set goes with it, keyed by bare filename.
    assert set(second["revision_of"]) == {
        f"{SLUG}.feature",
        f"{SLUG}_assumptions.yaml",
        f"{SLUG}_summary.md",
        f"{SLUG}_digest.yaml",
    }

    # The note is on the durable record, verbatim.
    revise_rows = [d for status, d in _events(store, _DIGEST_STAGE) if status == "revise"]
    assert len(revise_rows) == 1
    assert revise_rows[0]["digest_review"]["notes"] == (
        "the second example should be a 404, not a 400"
    )


@pytest.mark.asyncio
async def test_three_cards_is_the_whole_budget_and_the_stop_is_loud(
    store: SqlitePlanningRunStore,
) -> None:
    """Past the bound the run STOPS and quotes back what was asked for, rather
    than insisting a fourth try will get there."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [
                _answer("reject", notes="first note"),
                _answer("reject", notes="second note", attempt=1),
                _answer("reject", notes="third note", attempt=2),
            ]
        ),
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert len(_digest_cards(h)) == 3
    assert h.ctx["build_triggers"] == []
    told = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "needs a person" in told
    for note in ("first note", "second note", "third note"):
        assert note in told
    # Plain language all the way out — no internal labels in what a person reads.
    for internal in ("feature-spec", "CYCLE_CAP", "007"):
        assert internal not in told


@pytest.mark.asyncio
async def test_a_no_without_a_note_stops_honestly(
    store: SqlitePlanningRunStore,
) -> None:
    """There is nothing to rewrite from, so the run says so rather than looping."""
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("reject")]))

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    told = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "without leaving a note" in told
    assert [status for status, _d in _events(store, _DIGEST_STAGE)] == [
        "GATED",
        "rejected",
    ]


@pytest.mark.asyncio
async def test_a_note_that_starts_with_reject_cancels_the_run(
    store: SqlitePlanningRunStore,
) -> None:
    """The 2026-08-24 defect, pinned: "reject I typed the wrong sentence" was
    read as a revision note and the machine redrafted the same bad sentence.
    A typed reply whose first word is reject is the owner calling the run off:
    the run ends CANCELLED with their remaining words as the reason."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes="reject - I messed up the original sentence")]
        ),
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.CANCELLED.value
    assert run["error"] == "I messed up the original sentence"
    # No redraft: the spec-writer ran once, for the original draft only.
    assert len(h.ctx["dispatches"]) == 1
    assert len(_digest_cards(h)) == 1
    assert h.ctx["build_triggers"] == []
    # The durable record: the door's verdict row, then the CANCELLED move.
    assert [status for status, _d in _events(store, _DIGEST_STAGE)] == [
        "GATED",
        "cancelled",
        "CANCELLED",
    ]
    told = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "cancelled" in told
    assert "I messed up the original sentence" in told
    assert "fresh sentence" in told


@pytest.mark.asyncio
async def test_a_bare_reject_cancels_with_no_reason_to_quote(
    store: SqlitePlanningRunStore,
) -> None:
    """Just the word, nothing after it: still the owner's stop, still CANCELLED."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("reject", notes="reject")]),
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.CANCELLED.value
    assert len(h.ctx["dispatches"]) == 1
    told = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "cancelled" in told
    assert "fresh sentence" in told


@pytest.mark.asyncio
async def test_a_note_merely_containing_reject_still_means_rewrite(
    store: SqlitePlanningRunStore,
) -> None:
    """Only the FIRST word cancels: ordinary feedback that mentions rejecting
    something is still a revision note and the rewrite loop is unchanged."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [
                _answer("reject", notes="please reject unknown formats with a 400"),
                _answer("approve", attempt=1),
            ]
        ),
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["dispatches"][1]["validate_feedback"] == (
        "please reject unknown formats with a 400"
    )


@pytest.mark.asyncio
async def test_a_later_answer_is_named_never_reported_as_silence(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("defer")]))

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    told = " ".join(m for _cid, m, _lvl in h.ctx["notifications"])
    assert "set the card aside" in told
    assert "Nobody answered" not in told
    verdict = _events(store, _DIGEST_STAGE)[-1][1]["digest_review"]
    assert verdict["decision"] == "defer"
    assert verdict["decided_by"] == ORIGINATOR


@pytest.mark.asyncio
async def test_silence_times_out_and_an_unpostable_card_says_undeliverable(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    _queue(store)
    h = _make_driver(
        store, subscriber_factory=SharedScriptFactory([]), originator_wait_seconds=1
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert [status for status, _d in _events(store, _DIGEST_STAGE)] == [
        "GATED",
        "timed_out",
    ]
    assert "Nobody answered" in " ".join(m for _c, m, _l in h.ctx["notifications"])

    # A card that never reached the wire is NOT "nobody answered" — nobody was
    # ever ASKED.
    cx = sqlite_connect.connect_writer(tmp_path / "undeliverable.db")
    migrations.apply_at_boot(cx)
    other = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    _queue(other)
    h2 = _make_driver(
        other,
        subscriber_factory=SharedScriptFactory([]),
        publisher=RefusingPublisher(),
        originator_wait_seconds=1,
    )

    await h2.driver.drive(CID)

    told = " ".join(m for _c, m, _l in h2.ctx["notifications"])
    assert "could not be delivered" in told
    assert "Nobody answered" not in told


@pytest.mark.asyncio
async def test_a_stranger_and_a_stale_card_are_both_ignored(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [
                _answer("approve", decided_by="U_STRANGER"),
                _answer("approve", attempt=7),  # not this card
            ]
        ),
        originator_wait_seconds=1,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert [status for status, _d in _events(store, _DIGEST_STAGE)] == [
        "GATED",
        "timed_out",
    ]


# ---------------------------------------------------------------------------
# Restart survival
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_re_opens_the_same_card_word_for_word(
    store: SqlitePlanningRunStore,
) -> None:
    """A daemon killed with the card live must not orphan it — nor rewrite the
    spec underneath a card the owner is still reading."""
    _queue(store)
    publisher = FakePublisher()  # ONE wire across both boots

    git = RecordingGitRunner()  # ONE working tree across both boots
    boot1 = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        publisher=publisher,
        git=git,
    )
    task = asyncio.create_task(boot1.driver.drive(CID))
    for _ in range(600):
        await asyncio.sleep(0.01)
        if _digest_cards(boot1):
            break
    assert _digest_cards(boot1), "the door never put a card on the wire"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert store.get_run(CID)["state"] == PlanningState.FEATURE_SPEC.value
    assert [status for status, _d in _events(store, _DIGEST_STAGE)] == ["GATED"]
    assert [status for status, _d in _events(store, _DRAFT_STAGE)] == ["drafted"]

    boot2 = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        publisher=publisher,
        git=git,
    )
    await boot2.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # The spec was written ONCE — the re-drive re-opened the card, it did not
    # re-dispatch the spec-writer.
    assert len(boot2.ctx["dispatches"]) == 0
    cards = _digest_cards(boot2)
    assert len(cards) == 2
    assert {env.payload["request_id"] for env in cards} == {_digest_request_id(0)}
    # The SAME words, replayed from the record — not a re-render off source that
    # may have drifted.
    assert cards[0].payload["details"]["summary"] == (
        cards[1].payload["details"]["summary"]
    )
    assert [status for status, _d in _events(store, _DIGEST_STAGE)] == [
        "GATED",
        "reopened",
        "approved",
    ]


@pytest.mark.asyncio
async def test_an_answered_card_is_never_asked_again(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))
    await h.driver.drive(CID)
    assert len(_digest_cards(h)) == 1

    await h.driver.drive(CID)

    assert len(_digest_cards(h)) == 1
    assert len(h.ctx["dispatches"]) == 1


# ---------------------------------------------------------------------------
# The digest is proven against the COMMITTED spec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_digest_that_does_not_match_the_committed_spec_stops_the_run(
    store: SqlitePlanningRunStore,
) -> None:
    """The normalizer rewrites the .feature in place at pre-commit, and the
    committed file is what the build is checked against. A digest proven only
    against the pre-normalization text is a digest about a different artifact."""

    async def _drop_a_scenario(worktree: Path, feature_rel: str) -> ToolOutcome:
        path = worktree / feature_rel
        text = path.read_text(encoding="utf-8")
        path.write_text(text.split("  @negative")[0], encoding="utf-8")
        return ToolOutcome(ok=True)

    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        normalize=_drop_a_scenario,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert _digest_cards(h) == [], "an unproven digest must never reach a person"
    assert "spec digest" in store.get_run(CID)["error"]
    told = " ".join(m for _c, m, _l in h.ctx["notifications"])
    assert "did not match the spec" in told


@pytest.mark.asyncio
async def test_a_reply_with_no_digest_at_all_stops_the_run(
    store: SqlitePlanningRunStore,
) -> None:
    """Never a "summary unavailable" card: an approval that rests on a summary
    nobody checked is an approval of a lie."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_spec_reply(digest=None)],
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert _digest_cards(h) == []
    told = " ".join(m for _c, m, _l in h.ctx["notifications"])
    assert "no plain-language summary" in told


@pytest.mark.asyncio
async def test_an_ordinary_self_check_failure_still_only_warns(
    store: SqlitePlanningRunStore,
) -> None:
    """TWO POSTURES. Every gate but the digest is ADVISORY: the real oracles —
    the normalizer and the plan validate — run after it, and a self-flagged spec
    that passes them is good enough by the estate's own bar."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _spec_reply(validation_errors=["the summary's counts drifted by one"])
        ],
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value


@pytest.mark.asyncio
async def test_a_digest_error_from_the_spec_writer_stops_the_leg(
    store: SqlitePlanningRunStore,
) -> None:
    """...and the digest is the exception, because there is no oracle after it —
    only a person's eyes."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _spec_reply(
                validation_errors=["spec digest: the digest is missing an example"]
            )
        ],
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert _digest_cards(h) == []


@pytest.mark.asyncio
async def test_the_digest_is_committed_beside_the_spec(
    store: SqlitePlanningRunStore,
) -> None:
    """The branch carries the complete record of what was approved: the list a
    person read AND the examples it summarises."""
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))

    await h.driver.drive(CID)

    committed = await h.ctx["git"].read_file_from_branch(
        repo_path="/srv/repos/api_test",
        branch=BRANCH,
        file_path=f"features/{SLUG}/{SLUG}_digest.yaml",
    )
    assert committed == DIGEST_YAML


# ---------------------------------------------------------------------------
# The thin-feature setting — both paths built, so the ruling costs a value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_default_even_a_thin_feature_still_asks(
    store: SqlitePlanningRunStore,
) -> None:
    """A spec with no assumptions still has worked examples, and it is the
    examples that say what will be built."""
    thin_feature = (
        "Feature: version endpoint\n"
        "\n"
        "  Scenario: Version endpoint returns the running build\n"
        "    Given the service is running\n"
        "    Then the build it started from comes back\n"
    )
    thin_digest = (
        f"feature: {SLUG}\n"
        "generated: '2026-08-14T10:00:00Z'\n"
        "scenarios:\n"
        "- title: Version endpoint returns the running build\n"
        "  tags: []\n"
        "  sentence: Asking the service which version it is running returns the build\n"
        "    it was started from.\n"
        "assumptions: []\n"
    )
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _spec_reply(
                feature=thin_feature, digest=thin_digest, assumptions="assumptions: []\n"
            )
        ],
    )
    await h.driver.drive(CID)

    assert len(_digest_cards(h)) == 1
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value


@pytest.mark.asyncio
async def test_the_skip_is_available_and_records_itself(
    store: SqlitePlanningRunStore,
) -> None:
    """Turned off, the card is skipped ONLY on a thin feature — and the skip is
    on the durable record, never silent."""
    thin_feature = (
        "Feature: version endpoint\n"
        "\n"
        "  Scenario: Version endpoint returns the running build\n"
        "    Given the service is running\n"
        "    Then the build it started from comes back\n"
    )
    thin_digest = (
        f"feature: {SLUG}\n"
        "generated: '2026-08-14T10:00:00Z'\n"
        "scenarios:\n"
        "- title: Version endpoint returns the running build\n"
        "  tags: []\n"
        "  sentence: Asking the service which version it is running returns the build\n"
        "    it was started from.\n"
        "assumptions: []\n"
    )
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        spec_replies=[
            _spec_reply(
                feature=thin_feature,
                digest=thin_digest,
                assumptions="assumptions: []\n",
            )
        ],
        digest_review=PlanningDigestReviewConfig(always_ask=False),
        originator_wait_seconds=1,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert _digest_cards(h) == []
    assert [status for status, _d in _events(store, _DIGEST_STAGE)] == ["skipped"]


@pytest.mark.asyncio
async def test_the_skip_never_applies_to_a_feature_with_assumptions(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        digest_review=PlanningDigestReviewConfig(always_ask=False),
    )

    await h.driver.drive(CID)

    assert len(_digest_cards(h)) == 1


# ---------------------------------------------------------------------------
# The one tap answers the sign-in question too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_auth_flagged_run_pauses_once_and_the_card_carries_the_question(
    store: SqlitePlanningRunStore,
) -> None:
    """The sign-in flag is raised by the SPEC, so the question is asked where
    the spec is — not an hour later on a card with no spec attached."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_spec_reply(seed=_AUTH_SEED)],
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # ONE card in the whole run, and it carried both questions.
    assert len(h.ctx["publisher"].envelopes) == 1
    check = _digest_cards(h)[0].payload["details"]["summary"]["sign_in_check"]
    assert "signing in" in check["body"]
    assert check["flagged_lines"] == [
        "the spec mentions a bearer token when explaining it needs none"
    ]
    # The quality-checklist leg opened NO second door, and its receipt still
    # names who answered.
    assert [status for status, _d in _events(store, "qa-pass-bars-auth-confirm")] == []
    bars = [d for status, d in _events(store, "qa-pass-bars") if status == "approved"]
    assert bars[-1]["auth_confirmation"]["decided_by"] == ORIGINATOR
    assert bars[-1]["auth_confirmation"]["answered_on"] == "the spec digest card"


@pytest.mark.asyncio
async def test_the_card_offers_a_real_answer_for_yes_there_is_a_sign_in(
    store: SqlitePlanningRunStore,
) -> None:
    """The card must not promise an answer the machine cannot take.

    It used to: it asked the owner to say "yes, there is a sign-in" IN A NOTE,
    and a note at this door means REWRITE THE SPEC. So the card names the
    answer channel it actually reads, and names what each answer does.
    """
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_spec_reply(seed=_AUTH_SEED)],
    )

    await h.driver.drive(CID)

    check = _digest_cards(h)[0].payload["details"]["summary"]["sign_in_check"]
    # The answer rides the per-item channel, under an id the renderer can key on.
    assert check["answer_id"] == _SIGN_IN_ITEM
    # Both answers are spelled out, and so is saying nothing.
    assert "carries on" in check["agree_means"]
    assert "STOPS" in check["disagree_means"]
    assert "no sign-in here" in check["no_answer_means"]
    # The promise that could not be kept is GONE from every word of the card.
    assert "note" not in json.dumps(check).lower()


@pytest.mark.asyncio
async def test_yes_there_is_a_sign_in_stops_the_run_for_an_attended_checklist(
    store: SqlitePlanningRunStore,
) -> None:
    """The 2026-07-31 guarantee, reached through the ONE pause.

    The owner says yes to the spec and, on the same card, disagrees that this
    feature is free of signing in. The spec is approved and the plan is written
    — the person doing the attended registration needs both — and then the run
    STOPS at the quality checklist rather than registering it authless.
    """
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("approve", sign_in="rejected")]
        ),
        spec_replies=[_spec_reply(seed=_AUTH_SEED)],
    )

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    # ONE card in the whole run. The old second door is still never opened —
    # the answer came off the digest card, it just was not a yes.
    assert len(h.ctx["publisher"].envelopes) == 1
    assert [s for s, _d in _events(store, _AUTH_DOOR_STAGE)] == []
    # No checklist was registered and no build was queued: the leg's row is the
    # FAILED one, never the "approved" idempotency sentinel.
    assert [s for s, _d in _events(store, _BARS_STAGE)] == ["FAILED"]
    assert h.ctx["build_triggers"] == []
    # The owner's answer is on the durable spec row, in the sign-in door's own
    # vocabulary, so the record reads the same whichever door answered.
    spec = [d for s, d in _events(store, _SPEC_STAGE) if s == "approved"][-1]
    assert spec["spec_review"]["sign_in_answer"] == "rejected"
    assert "auth_confirmed" not in spec["spec_review"]
    # The machine's receipt is the 2026-07-31 one, WORD FOR WORD — this is the
    # same terminal, reached from the one pause instead of a second door.
    assert "SPL-007 §A.2" in run["error"]
    assert "attended registration" in run["error"]
    assert "confirmed this IS a sign-in surface" in run["error"]
    # The owner's sentence names no internal label.
    reasons = " ".join(m for _c, m, _l in h.ctx["notifications"])
    assert "stopped at registering the quality checklist" in reasons
    for internal in ("qa-pass-bars", "auth_surface_bearing", "SPL-007"):
        assert internal not in reasons
    # And they were told AT THE TAP, not an hour later.
    assert "task plan and then stop" in reasons


@pytest.mark.asyncio
async def test_no_there_is_no_sign_in_is_the_same_yes_it_always_was(
    store: SqlitePlanningRunStore,
) -> None:
    """Answering the question explicitly must land exactly where saying nothing
    about it lands — the 2026-08-14 ruling that one tap confirms it."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("approve", sign_in="accepted")]
        ),
        spec_replies=[_spec_reply(seed=_AUTH_SEED)],
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert [s for s, _d in _events(store, _AUTH_DOOR_STAGE)] == []
    bars = [d for s, d in _events(store, _BARS_STAGE) if s == "approved"][-1]
    assert bars["auth_confirmation"]["outcome"] == "confirmed"
    assert bars["auth_confirmation"]["answered_on"] == "the spec digest card"


@pytest.mark.asyncio
async def test_a_sign_in_answer_that_decided_nothing_is_never_read_as_a_yes(
    store: SqlitePlanningRunStore,
) -> None:
    """Set the sign-in question aside and the run stops and NAMES that — the
    one thing it must never do is take silence-with-a-shrug for confirmation."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("approve", sign_in="deferred")]
        ),
        spec_replies=[_spec_reply(seed=_AUTH_SEED)],
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h.ctx["build_triggers"] == []
    spec = [d for s, d in _events(store, _SPEC_STAGE) if s == "approved"][-1]
    assert spec["spec_review"]["sign_in_answer"] == "deferred"
    assert "set the confirmation card aside" in store.get_run(CID)["error"]


@pytest.mark.asyncio
async def test_a_note_still_only_ever_means_rewrite_the_spec(
    store: SqlitePlanningRunStore,
) -> None:
    """The defect this pair of channels exists to kill.

    A note saying "yes — this really does involve signing in" is prose, and the
    machine does not read prose for decisions. It does what a note has always
    meant here: rewrite the spec from those words. The sign-in answer is a
    separate value, and on the fresh card the question is asked again against
    the spec that was actually written.
    """
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [
                _answer(
                    "reject",
                    notes="Yes — this really does involve signing in.",
                    attempt=0,
                ),
                _answer("approve", attempt=1, sign_in="rejected"),
            ]
        ),
        spec_replies=[_spec_reply(seed=_AUTH_SEED), _spec_reply(seed=_AUTH_SEED)],
    )

    await h.driver.drive(CID)

    # Round 1 rewrote the spec from their words, VERBATIM.
    assert h.ctx["dispatches"][1]["validate_feedback"] == (
        "Yes — this really does involve signing in."
    )
    # Round 2 asked the sign-in question again — the spec had changed underneath
    # it — and their answer on THAT card is the one that counts.
    assert len(_digest_cards(h)) == 2
    assert "sign_in_check" in _digest_cards(h)[1].payload["details"]["summary"]
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert h.ctx["build_triggers"] == []


@pytest.mark.asyncio
async def test_a_crash_between_the_tap_and_the_commit_keeps_the_sign_in_answer(
    store: SqlitePlanningRunStore,
) -> None:
    """The narrow window that would turn a "yes, there IS a sign-in" into a yes.

    The owner answers; the door writes its verdict; the daemon dies before the
    spec leg writes its own row. The re-drive replays the answered door rather
    than re-asking — so what it replays has to be the WHOLE answer. If the
    per-item answers were dropped there, the re-drive would read their silence
    as agreement and register the checklist authless.
    """
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("approve", sign_in="rejected")]
        ),
        spec_replies=[_spec_reply(seed=_AUTH_SEED)],
    )
    await h.driver.drive(CID)
    assert [s for s, _d in _events(store, _DIGEST_STAGE)] == ["GATED", "approved"]

    # Now re-drive the leg from the durable record alone, as a fresh boot would,
    # with NOBODY left on the wire to answer anything.
    boot2 = _make_driver(store, subscriber_factory=SharedScriptFactory([]))
    row = store.get_run(CID)
    draft = boot2.driver._open_spec_draft(CID)
    replay = await boot2.driver._spec_digest_review_door(row, CID, draft or {})

    assert replay.outcome == "approved"
    assert replay.item_answers == {_SIGN_IN_ITEM: "rejected"}
    assert boot2.driver._sign_in_answer(draft or {}, replay) == "rejected"


@pytest.mark.asyncio
async def test_a_sign_in_answer_on_a_rewrite_round_is_recorded_not_acted_on(
    store: SqlitePlanningRunStore,
) -> None:
    """A round that asks for a rewrite decides nothing about the sign-in: the
    spec is about to change. It is still on the record, because an answer
    somebody gave is not something to throw away."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [
                _answer("reject", notes="call it the build stamp", sign_in="rejected"),
                _answer("approve", attempt=1),
            ]
        ),
        spec_replies=[_spec_reply(seed=_AUTH_SEED), _spec_reply(seed=_AUTHLESS_SEED)],
    )

    await h.driver.drive(CID)

    revise = [d for s, d in _events(store, _DIGEST_STAGE) if s == "revise"][-1]
    assert revise["digest_review"]["item_answers"] == {_SIGN_IN_ITEM: "rejected"}
    # The rewritten spec does not trip the scan, so the fresh card does not ask
    # — and the run is judged on the spec that was actually written.
    assert "sign_in_check" not in _digest_cards(h)[1].payload["details"]["summary"]
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value


@pytest.mark.asyncio
async def test_an_unflagged_run_gets_no_sign_in_line(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))

    await h.driver.drive(CID)

    assert "sign_in_check" not in _digest_cards(h)[0].payload["details"]["summary"]


@pytest.mark.asyncio
async def test_a_flagged_feature_is_never_thin_enough_to_skip(
    store: SqlitePlanningRunStore,
) -> None:
    """The skip must never push the sign-in question onto a later door — that
    is the second pause this design exists to remove."""
    thin_feature = (
        "Feature: version endpoint\n"
        "\n"
        "  Scenario: Version endpoint returns the running build\n"
        "    Given the service is running\n"
        "    Then the build it started from comes back\n"
    )
    thin_digest = (
        f"feature: {SLUG}\n"
        "generated: '2026-08-14T10:00:00Z'\n"
        "scenarios:\n"
        "- title: Version endpoint returns the running build\n"
        "  tags: []\n"
        "  sentence: Asking the service which version it is running returns the build\n"
        "    it was started from.\n"
        "assumptions: []\n"
    )
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _spec_reply(
                seed=_AUTH_SEED,
                feature=thin_feature,
                digest=thin_digest,
                assumptions="assumptions: []\n",
            )
        ],
        digest_review=PlanningDigestReviewConfig(always_ask=False),
    )

    await h.driver.drive(CID)

    assert len(_digest_cards(h)) == 1
    assert "sign_in_check" in _digest_cards(h)[0].payload["details"]["summary"]


# ---------------------------------------------------------------------------
# What the "show me" view inherits, pinned so it cannot be inherited by accident
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_show_me_text_is_the_raw_spec_unscrubbed(
    store: SqlitePlanningRunStore,
) -> None:
    """The card's "show me" field is the whole committed spec, VERBATIM.

    Everything else on this card is composed from the digest and is safe to put
    in front of a person by construction. This one field is not: it is the
    spec's own words, and real specs in this estate carry task ids and internal
    tool names that the plain-name fence forbids on a user surface.

    This test does not decide what the renderer should do about that — it pins
    what is actually IN the field, so whoever builds the "show me" view has to
    decide deliberately (scrub it, or exempt that view) rather than find out on
    the first live run. The fence's own suite renders neutral fixtures and will
    not catch this.
    """
    feature = FEATURE_TEXT.replace(
        "  @key-example @smoke\n", "  @key-example @smoke @task:TASK-MP-008\n"
    ).replace(
        "    Given the service is running\n"
        "    When the version is asked for\n",
        "    Given the guardkit service is running\n"
        "    When the version is asked for\n",
        1,
    )
    digest = DIGEST_YAML.replace(
        "  - '@smoke'\n", "  - '@smoke'\n  - '@task:TASK-MP-008'\n"
    )
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_spec_reply(feature=feature, digest=digest)],
    )

    await h.driver.drive(CID)

    card = _digest_cards(h)[0].payload["details"]["summary"]
    # (1) "Show me" is the whole committed spec, byte for byte — task id, tool
    # name, step text and all.
    assert card["worked_examples"] == feature
    assert "@task:TASK-MP-008" in card["worked_examples"]
    assert "guardkit" in card["worked_examples"]

    # (2) The labels travel raw too — but this one is ALREADY answered by the
    # card's contract: the renderer shows only the labels it has a plain word
    # for, so an unknown label is dropped rather than shown. Pinned so that
    # contract stays a decision somebody made, not an accident.
    assert card["what_it_will_do"][0]["tags"] == [
        "@key-example",
        "@smoke",
        "@task:TASK-MP-008",
    ]

    # (3) Everything a person is actually ASKED about — every sentence, every
    # assumption, every "what this means" line — is composed from the digest
    # and is clean. That is why (1) is the one field the renderer must rule on.
    #
    # ``target_repo`` is excluded alongside it, and deliberately: it is the
    # repository name itself (2026-09-05 rule 5 — the card says where this
    # will be built), so it CONTAINS the org name by design. It is not spec
    # text and it is not a leak.
    prose = json.dumps(
        {
            k: v
            for k, v in card.items()
            if k not in ("worked_examples", "what_it_will_do", "target_repo")
        }
    )
    prose += json.dumps([e["sentence"] for e in card["what_it_will_do"]])
    assert "TASK-MP-008" not in prose
    assert "guardkit" not in prose


@pytest.mark.asyncio
async def test_the_spec_text_is_written_to_the_event_log_once_per_card(
    store: SqlitePlanningRunStore,
) -> None:
    """The card carries the whole spec, so the durable log must not carry it
    over and over: it belongs on the OPENING row a restart replays from, and
    nowhere else. A three-card run used to write it six or seven times."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [
                _answer("reject", notes="call it the build stamp", attempt=0),
                _answer("approve", attempt=1),
            ]
        ),
    )

    await h.driver.drive(CID)

    rows = _events(store, _DIGEST_STAGE)
    assert [s for s, _d in rows] == ["GATED", "revise", "GATED", "approved"]
    carrying = [s for s, d in rows if "worked_examples" in json.dumps(d)]
    assert carrying == ["GATED", "GATED"], (
        "the spec text belongs on the opening rows only"
    )
    # The verdict rows still say everything a verdict row is FOR.
    verdict = dict(rows[1][1]["digest_review"])
    assert verdict["outcome"] == "revise"
    assert verdict["decided_by"] == ORIGINATOR
    assert verdict["notes"] == "call it the build stamp"
    assert verdict["decision"] == "reject"


@pytest.mark.asyncio
async def test_the_absorption_is_written_once_and_never_spins(
    store: SqlitePlanningRunStore,
) -> None:
    """If the row is already there and the chain still asks to pause, something
    upstream is not reading it. Stop loudly rather than write it forever."""
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))
    assert h.driver._absorb_product_docs_checkpoint(CID) is True
    assert h.driver._absorb_product_docs_checkpoint(CID) is False
    assert (
        len([s for s, _d in _events(store, "product_docs") if s == "checkpoint_cleared"])
        == 1
    )


# ---------------------------------------------------------------------------
# Was the note actually honoured? (2026-09-05)
# ---------------------------------------------------------------------------
#
# Rich sent "drop example 3, seven exactly is the rule" on a spec digest card.
# The spec-writer came back with the same six examples (one reworded, still
# there), its coach scored the rewrite 1.0 because its criteria never ask
# whether the feedback was resolved, and the second card was identical to the
# first line for line — with no word that nothing had changed. One of the three
# touches a person has silently did nothing. These tests pin the card and the
# ping saying which it was.

#: The first card's ``what_happened``, byte for byte. Written out here rather
#: than imported so a change to the words a person reads has to be a deliberate
#: two-sided act.
_ROUND_ONE_TEXT = (
    "The spec-writer has written the worked examples this build will be "
    "checked against. Below is one sentence per example, in the order they "
    "appear. This list is checked by ordinary code against the examples "
    "themselves — every example is here, none has been left out."
)

_THE_NOTE = "drop example 3, seven exactly is the rule"

_FEATURE_THREE = (
    "Feature: version endpoint\n"
    "\n"
    "  @key-example @smoke\n"
    "  Scenario: Version endpoint returns the running build\n"
    "    Given the service is running\n"
    "    When the version is asked for\n"
    "    Then the build it started from comes back\n"
    "\n"
    "  @negative\n"
    "  Scenario: Version endpoint rejects an unknown format\n"
    "    Given the service is running\n"
    "    When an unpublished format is asked for\n"
    "    Then the request is refused\n"
    "\n"
    "  @negative\n"
    "  Scenario: Version endpoint refuses an empty request\n"
    "    Given the service is running\n"
    "    When nothing at all is asked for\n"
    "    Then the request is refused\n"
)

_DIGEST_THREE = (
    f"feature: {SLUG}\n"
    "generated: '2026-09-05T10:00:00Z'\n"
    "scenarios:\n"
    "- title: Version endpoint returns the running build\n"
    "  tags:\n"
    "  - '@key-example'\n"
    "  - '@smoke'\n"
    "  sentence: Asking the service which version it is running returns the build\n"
    "    it was started from.\n"
    "- title: Version endpoint rejects an unknown format\n"
    "  tags:\n"
    "  - '@negative'\n"
    "  sentence: Asking for the version in a format the service does not publish is\n"
    "    refused rather than guessed at.\n"
    "- title: Version endpoint refuses an empty request\n"
    "  tags:\n"
    "  - '@negative'\n"
    "  sentence: Asking for nothing at all is refused rather than answered with a\n"
    "    guess.\n"
    "assumptions:\n"
    "- id: ASSUM-001\n"
    "  text: The version string comes from the build metadata.\n"
    "  basis: common practice; the input did not say\n"
)

#: The honoured rewrite: the third example is GONE and the two that remain are
#: said differently — "2 examples changed, 1 removed".
_FEATURE_TWO_REWORDED = (
    "Feature: version endpoint\n"
    "\n"
    "  @key-example @smoke\n"
    "  Scenario: Version endpoint returns the running build\n"
    "    Given the service is running\n"
    "    When the version is asked for\n"
    "    Then the build it started from comes back\n"
    "\n"
    "  @negative\n"
    "  Scenario: Version endpoint rejects an unknown format\n"
    "    Given the service is running\n"
    "    When an unpublished format is asked for\n"
    "    Then the request is refused with a 404\n"
)

_DIGEST_TWO_REWORDED = (
    f"feature: {SLUG}\n"
    "generated: '2026-09-05T11:00:00Z'\n"
    "scenarios:\n"
    "- title: Version endpoint returns the running build\n"
    "  tags:\n"
    "  - '@key-example'\n"
    "  - '@smoke'\n"
    "  sentence: Asking the service which build stamp it is running returns the\n"
    "    stamp it was started from.\n"
    "- title: Version endpoint rejects an unknown format\n"
    "  tags:\n"
    "  - '@negative'\n"
    "  sentence: Asking for the build stamp in a format the service does not publish\n"
    "    comes back as not found.\n"
    "assumptions:\n"
    "- id: ASSUM-001\n"
    "  text: The version string comes from the build metadata.\n"
    "  basis: common practice; the input did not say\n"
)


#: Fields on the approval envelope that no person reads: routing keys, ids the
#: machine addresses itself by, and the raw ``.feature`` text (one click
#: deeper, ruled on elsewhere). EVERYTHING else on the card is prose written
#: for a reader and must survive the raw-id sweep — reading only ``summary``
#: is what let ``action_description`` keep saying "for U0RIGINATOR's word"
#: while the sweep passed (coach finding, 2026-09-05).
#: A raw chat member id as it appears in text ("U03QR8WKT29", "U0RIGINATOR").
#: The SHAPE of one, not one particular id, so a sentence that interpolates a
#: different member's id is caught by the same sweep.
_RAW_CHAT_ID = re.compile(r"\bU[0-9A-Z]{8,}\b")

_MACHINE_ONLY_TOP_LEVEL = frozenset({"request_id", "agent_id"})
_MACHINE_ONLY_DETAILS = frozenset(
    {
        "build_id",
        "feature_id",
        "stage_label",
        "gate_mode",
        "expected_approver",
        "parent_request_id",
        "originating_channel",
    }
)


def _card_text(envelope: Any) -> str:
    """Everything on one card a PERSON reads — machine fields excluded."""
    payload = {
        key: value
        for key, value in envelope.payload.items()
        if key not in _MACHINE_ONLY_TOP_LEVEL
    }
    details = {
        key: value
        for key, value in (payload.get("details") or {}).items()
        if key not in _MACHINE_ONLY_DETAILS
    }
    summary = dict(details.get("summary") or {})
    summary.pop("worked_examples", None)
    details["summary"] = summary
    payload["details"] = details
    return json.dumps(payload, default=str)


@pytest.mark.asyncio
async def test_the_first_card_says_exactly_what_it_always_said(
    store: SqlitePlanningRunStore,
) -> None:
    """Round one has nothing to compare against, so nothing is added to it."""
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))

    await h.driver.drive(CID)

    summary = _digest_cards(h)[0].payload["details"]["summary"]
    assert summary["what_happened"] == _ROUND_ONE_TEXT


@pytest.mark.asyncio
async def test_a_rewrite_that_changed_nothing_says_so_on_the_card(
    store: SqlitePlanningRunStore,
) -> None:
    """THE DEFECT. The spec-writer returns the same list; the second card used
    to be identical to the first with no word that the note did nothing."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes=_THE_NOTE), _answer("approve", attempt=1)]
        ),
    )

    await h.driver.drive(CID)

    cards = _digest_cards(h)
    assert len(cards) == 2
    first, second = (c.payload["details"]["summary"] for c in cards)
    # The lists really are identical — this is the defect's own shape.
    assert first["what_it_will_do"] == second["what_it_will_do"]
    assert second["what_happened"] == (
        'The rewrite came back with the same list. Your note was: '
        f'"{_THE_NOTE}". Approve anyway, send another note, or reject.'
    )
    # Never blocked, no fourth act: the same three answers, and the run went on
    # to build when the owner said yes anyway.
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert set(second) == set(first)


@pytest.mark.asyncio
async def test_a_rewrite_that_changed_nothing_says_so_in_the_ping(
    store: SqlitePlanningRunStore,
) -> None:
    """One sentence, on the notification that opens that round."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes=_THE_NOTE), _answer("approve", attempt=1)]
        ),
    )

    await h.driver.drive(CID)

    said = [m for _c, m, _l in h.ctx["notifications"] if "same list" in m]
    assert len(said) == 1
    assert said[0] == (
        f"Planning run {CID}: the rewrite came back with the same list — your "
        f'note was "{_THE_NOTE}" — so approve anyway, send another note, or '
        "reject."
    )
    # ONE sentence: no full stop before the last one.
    assert said[0].count(".") == 1


@pytest.mark.asyncio
async def test_a_rewrite_that_changed_something_names_what_changed(
    store: SqlitePlanningRunStore,
) -> None:
    """The honoured note: the card is what it was, plus one line of counts."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes=_THE_NOTE), _answer("approve", attempt=1)]
        ),
        spec_replies=[
            _spec_reply(feature=_FEATURE_THREE, digest=_DIGEST_THREE),
            _spec_reply(feature=_FEATURE_TWO_REWORDED, digest=_DIGEST_TWO_REWORDED),
        ],
    )

    await h.driver.drive(CID)

    cards = _digest_cards(h)
    assert len(cards) == 2
    first, second = (c.payload["details"]["summary"] for c in cards)
    assert len(first["what_it_will_do"]) == 3
    assert len(second["what_it_will_do"]) == 2
    assert second["what_happened"] == (
        _ROUND_ONE_TEXT + " What changed since your note: 2 examples changed, "
        "1 removed."
    )
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value


@pytest.mark.asyncio
async def test_an_assumption_that_changed_is_counted_too(
    store: SqlitePlanningRunStore,
) -> None:
    """The card asks about the assumptions as well, so they are compared too."""
    _queue(store)
    dropped_assumptions = "assumptions: []\n"
    digest_without = DIGEST_YAML.split("assumptions:\n")[0] + "assumptions: []\n"
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes=_THE_NOTE), _answer("approve", attempt=1)]
        ),
        spec_replies=[
            _spec_reply(),
            _spec_reply(assumptions=dropped_assumptions, digest=digest_without),
        ],
    )

    await h.driver.drive(CID)

    second = _digest_cards(h)[1].payload["details"]["summary"]
    assert second["what_it_will_do"] == (
        _digest_cards(h)[0].payload["details"]["summary"]["what_it_will_do"]
    )
    assert second["what_happened"] == (
        _ROUND_ONE_TEXT + " What changed since your note: 1 assumption removed."
    )


@pytest.mark.asyncio
async def test_a_rewrite_that_only_dropped_the_sign_in_question_is_a_change(
    store: SqlitePlanningRunStore,
) -> None:
    """The sign-in question is part of what the card ASKS — it is answered by
    the same tap — so a rewrite whose only visible change is that question
    disappearing is a change, not "the same list" (coach finding, 2026-09-05).
    """
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes=_THE_NOTE), _answer("approve", attempt=1)]
        ),
        spec_replies=[_spec_reply(seed=_AUTH_SEED), _spec_reply(seed=_AUTHLESS_SEED)],
    )

    await h.driver.drive(CID)

    cards = _digest_cards(h)
    first, second = (c.payload["details"]["summary"] for c in cards)
    # Every sentence and every assumption is word for word what it was...
    assert first["what_it_will_do"] == second["what_it_will_do"]
    assert first["what_the_machine_assumed"] == second["what_the_machine_assumed"]
    # ...and the question is gone, so the card says so rather than "same list".
    assert "sign_in_check" in first and "sign_in_check" not in second
    assert second["what_happened"] == (
        _ROUND_ONE_TEXT + " What changed since your note: the sign-in question "
        "is gone."
    )
    said = " ".join(m for _c, m, _l in h.ctx["notifications"])
    assert "same list" not in said


@pytest.mark.asyncio
async def test_a_rewrite_that_only_added_the_sign_in_question_is_a_change(
    store: SqlitePlanningRunStore,
) -> None:
    """The other direction: a question that was not there before is a change."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes=_THE_NOTE), _answer("approve", attempt=1)]
        ),
        spec_replies=[_spec_reply(seed=_AUTHLESS_SEED), _spec_reply(seed=_AUTH_SEED)],
    )

    await h.driver.drive(CID)

    first, second = (
        c.payload["details"]["summary"] for c in _digest_cards(h)
    )
    assert first["what_it_will_do"] == second["what_it_will_do"]
    assert "sign_in_check" not in first and "sign_in_check" in second
    assert second["what_happened"] == (
        _ROUND_ONE_TEXT + " What changed since your note: the sign-in question "
        "was added."
    )


@pytest.mark.asyncio
async def test_no_line_this_door_sends_carries_a_chat_id(
    store: SqlitePlanningRunStore,
) -> None:
    """The grep: every line the door sent a person, swept for a raw member id.

    ``ORIGINATOR`` is one particular id; this is the shape of ALL of them, so a
    new sentence that interpolates a different one is caught too.
    """
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes=_THE_NOTE), _answer("approve", attempt=1)]
        ),
        spec_replies=[_spec_reply(seed=_AUTH_SEED), _spec_reply(seed=_AUTH_SEED)],
    )

    await h.driver.drive(CID)

    assert h.ctx["notifications"], "the run sent nobody anything"
    for _cid, message, _level in h.ctx["notifications"]:
        found = _RAW_CHAT_ID.findall(message)
        assert not found, f"raw chat id {found} in a line a person reads: {message}"
    for card in _digest_cards(h):
        found = _RAW_CHAT_ID.findall(_card_text(card))
        assert not found, f"raw chat id {found} on a card a person reads"


@pytest.mark.asyncio
async def test_no_card_or_ping_ever_shows_a_raw_chat_id(
    store: SqlitePlanningRunStore,
) -> None:
    """"U03QR8WKT29 sent a note" identified nobody. The person is "you"."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes=_THE_NOTE), _answer("approve", attempt=1)]
        ),
    )

    await h.driver.drive(CID)

    told = " ".join(m for _c, m, _l in h.ctx["notifications"])
    assert ORIGINATOR not in told
    for card in _digest_cards(h):
        assert ORIGINATOR not in _card_text(card)
        # The one-line summary at the top of the card is read by a person too.
        assert "for your word" in card.payload["action_description"]
        # The id is still on the row the machine routes by — that is not text.
        assert card.payload["details"]["expected_approver"] == ORIGINATOR
    # And it still says who did what — in a word a person recognises.
    assert "you sent a note" in told
    assert "you said yes to the spec" in told
    assert "You have a card listing" in told


# ---------------------------------------------------------------------------
# What the card promises about silence is what silence actually does
# ---------------------------------------------------------------------------
#
# This door is an INLINE wait, not a paused checkpoint: ``_open_inline_door``
# runs its own window off ``PlanningConfig.originator_wait_seconds`` and, when
# that window closes with no answer, ends the run. The two-phase "wait, then
# remind the same person, then wait again" in ``planning/escalation.py`` runs
# only for a row that is durably PAUSED (every transition there is a
# compare-and-swap from PAUSED), and this door deliberately never enters that
# state. ``escalated_wait_seconds`` therefore has NO effect on any card in this
# file, and a sentence promising a reminder here would promise something the
# machine never sends.
#
# These two tests are the guard on that: they hold the words next to the
# behaviour. If the door is ever changed to remind and wait again, they fail —
# and the words must change in the same act.


@pytest.mark.asyncio
async def test_the_wait_the_card_promises_is_the_wait_the_door_really_keeps(
    store: SqlitePlanningRunStore,
) -> None:
    """One wait, on the person who asked, and then the run stops.

    The run is driven with a one-second window and nobody answers, so the
    SAME run proves both halves: what the card said would happen, and what
    happened.
    """
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        originator_wait_seconds=1,
    )

    await h.driver.drive(CID)

    # What happened: one window, then the end. No second round, no re-target.
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert [status for status, _d in _events(store, _DIGEST_STAGE)] == [
        "GATED",
        "timed_out",
    ]
    assert len(_digest_cards(h)) == 1

    # What the card said would happen, in the same words.
    card = _digest_cards(h)[0]
    said = card.payload["details"]["summary"]["no_answer_means"]
    assert "1 second" in said
    assert "the run stops" in said

    # And the Slack line that opened the door said the same one wait.
    ping = next(
        m for _c, m, _l in h.ctx["notifications"] if "You have a card listing" in m
    )
    assert "1 second" in ping
    assert "stops the run" in ping

    # Neither surface promises a reminder or a second window: this door sends
    # no reminder, and the escalated wait (4 hours by default) never applies.
    for words in (_card_text(card).lower(), ping.lower()):
        assert "remind" not in words
        assert "4 hours" not in words


def test_the_sign_in_cards_own_words_about_silence_match_the_same_one_wait(
    store: SqlitePlanningRunStore,
) -> None:
    """The second door's card and ping, built straight from the driver.

    This door opens only when the digest door did not carry the question, and
    it is the same inline wait — so it makes the same promise about silence
    and no other.
    """
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([]))

    card = h.driver._auth_confirmation_card(
        seed={"feature_slug": SLUG},
        basis_lines=["the spec mentions a bearer token when explaining it needs none"],
        wait_seconds=3600,
    )
    ping = h.driver._auth_door_open_message(CID, wait_seconds=3600)

    assert "1 hour" in card["no_answer_means"]
    assert "the run stops" in card["no_answer_means"]
    assert "1 hour" in ping
    assert "stops the run" in ping

    for words in (json.dumps(card).lower(), ping.lower()):
        assert "remind" not in words
        assert "4 hours" not in words


@pytest.mark.parametrize(
    ("wait_seconds", "in_words"),
    [(3600, "1 hour"), (600, "10 minutes")],
)
def test_all_four_silence_sentences_name_the_one_wait_the_config_sets(
    store: SqlitePlanningRunStore, wait_seconds: int, in_words: str
) -> None:
    """The wait is READ, not written into the words.

    Four surfaces say what silence does — the spec card and the line that
    opens it, the sign-in card and the line that opens it. All four are built
    straight from the driver here at two different settings, so a number typed
    into a sentence instead of read from the setting fails this.

    The live setting is an hour; ten minutes is the same sentence with a
    different setting behind it. Neither ever mentions the four-hour escalated
    window, because no card this door sends is affected by it.
    """
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        originator_wait_seconds=wait_seconds,
    )

    spec_card = h.driver._digest_review_card(
        CID,
        digest_obj={
            "scenarios": [{"sentence": "the list comes back in order", "tags": []}],
            "assumptions": [],
        },
        feature_text="Feature: anything\n",
    )
    spec_ping = h.driver._digest_door_open_message(CID, wait_seconds=wait_seconds)
    auth_card = h.driver._auth_confirmation_card(
        seed={"feature_slug": SLUG},
        basis_lines=["the spec mentions a token while explaining it needs none"],
        wait_seconds=wait_seconds,
    )
    auth_ping = h.driver._auth_door_open_message(CID, wait_seconds=wait_seconds)

    said = [
        spec_card["no_answer_means"],
        spec_ping,
        auth_card["no_answer_means"],
        auth_ping,
    ]
    for sentence in said:
        assert "No answer within " + in_words in sentence
        # The whole promise, in one window: silence ends the run.
        assert "stops" in sentence

    # Nothing anywhere on either card, or in either line, offers a reminder or
    # a second window. The escalated wait is four hours and is never reached.
    for words in (
        json.dumps(spec_card).lower(),
        spec_ping.lower(),
        json.dumps(auth_card).lower(),
        auth_ping.lower(),
    ):
        assert "remind" not in words
        assert "4 hours" not in words
        assert "try again" not in words


def test_no_sentence_in_the_driver_promises_a_reminder_nobody_sends() -> None:
    """A FIFTH surface cannot quietly make the promise the first four don't.

    The tests above build the four sentences that exist today. This one reads
    the driver's own source — the code, not its explanations — and holds every
    "…answer within…" sentence in it to the same two rules: the wait is
    rendered from the run's own window through ``_plain_wait``, and the
    sentence does not offer a reminder. A card added later is caught here even
    if nobody thinks to test its words.
    """
    import ast

    from forge.planning import driver as driver_module

    source = Path(driver_module.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()

    # The explanations are not the promise: skip every docstring, so a
    # paragraph that DESCRIBES the rule is not read as breaking it.
    prose: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        body = getattr(node, "body", None) or []
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            prose.update(range(first.lineno - 1, (first.end_lineno or first.lineno)))

    sites = [
        i
        for i, line in enumerate(lines)
        if "answer within" in line and i not in prose and not line.lstrip().startswith("#")
    ]
    # The four that exist today: the spec card and its line, the sign-in card
    # and its line. A floor, not a cap — a fifth is checked, not banned.
    assert len(sites) >= 4

    for index in sites:
        sentence = " ".join(lines[index : index + 3])
        assert "_plain_wait(wait_seconds)" in sentence, (
            f"line {index + 1} writes a wait into the words instead of reading "
            "it from the run's own window"
        )
        assert "remind" not in sentence.lower(), (
            f"line {index + 1} promises a reminder; this door sends none"
        )


# ---------------------------------------------------------------------------
# When the rewrite is refused, forge says what to do (rule 23, 2026-09-06)
# ---------------------------------------------------------------------------


def _refused_reply(reason: str) -> Any:
    """The spec writer's revision round coming back NOT ok: the checker
    refused the rewrite (the must-pass "what the note asks must change, and
    nothing else may move without reason")."""
    return SimpleNamespace(
        outcome=SimpleNamespace(value="error"), role_output={}, reason=reason
    )


@pytest.mark.asyncio
async def test_a_refused_rewrite_after_your_note_says_what_to_do(
    store: SqlitePlanningRunStore,
) -> None:
    """The owner sent a note; the checker refused the rewrite. The thread says
    whose note it was, quotes it, gives the checker's reason in one sentence,
    says nothing was built, and says what to do — one message, no card."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("reject", notes=_THE_NOTE)]),
        spec_replies=[
            _spec_reply(),
            _refused_reply(
                "the rewrite changed the first example, which the note did not mention."
            ),
        ],
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert len(_digest_cards(h)) == 1  # no second card
    errors = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert errors == [
        f"Planning run {CID} stopped at the spec: the spec writer could not "
        f'honour your note "{_THE_NOTE}" — the checker refused the rewrite twice '
        "(the rewrite changed the first example, which the note did not "
        "mention). Nothing was built. To try again, send the sentence again "
        "with the note folded into it."
    ]
    # The machine record keeps the internal reason and names whose note it was.
    error = store.get_run(CID)["error"] or ""
    assert error.startswith("007 dispatch error: the rewrite changed the first example")
    assert "the checker refused the revision round after the owner's note" in error


@pytest.mark.asyncio
async def test_a_refused_first_round_keeps_todays_plain_sentence(
    store: SqlitePlanningRunStore,
) -> None:
    """No note yet, so nothing to honour: the first-round failure keeps the
    default sentence (the plain stage name plus the leg's reason)."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        spec_replies=[_refused_reply("no specialist was reachable")],
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    errors = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert len(errors) == 1
    assert errors[0].startswith(f"Planning run {CID} stopped at writing the spec: ")
    assert "could not honour" not in errors[0]
    assert "007 dispatch error: no specialist was reachable" in errors[0]


@pytest.mark.asyncio
async def test_a_refusal_with_no_reason_still_says_what_to_do(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("reject", notes=_THE_NOTE)]),
        spec_replies=[_spec_reply(), _refused_reply("")],
    )

    await h.driver.drive(CID)

    errors = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert errors == [
        f"Planning run {CID} stopped at the spec: the spec writer could not "
        f'honour your note "{_THE_NOTE}" — the checker refused the rewrite twice '
        "(the checker gave no reason). Nothing was built. To try again, send "
        "the sentence again with the note folded into it."
    ]


# ---------------------------------------------------------------------------
# THE WORKED EXAMPLES ARE CHECKED FOR PROVABILITY BEFORE THE CARD (Part K of
# the rewrite-on-refusal lane, 2026-09-07, on Rich's decision).
#
# Until now the routing law first saw the examples at the plan stage, after
# Rich's yes; a refusal there cost him a wait and, at worst, a stop. Now the
# committed draft is run through guardkit's rules-only check before the door
# opens; a refused example goes back to the spec writer once as the machine's
# own note — the same round the plan stage runs — and the card says what
# happened, in the spec's words. No new buttons, no new touch.
# ---------------------------------------------------------------------------

from forge.planning.target_terminal_tools import ScenarioProvabilityOutcome  # noqa: E402

_REFUSED_TITLE = "Version endpoint rejects an unknown format"
_HOMED_TITLE = "Version endpoint returns the running build"

#: Rule 2's note, the spec's own words, with the fixture's refused title.
_PRE_CARD_NOTE = (
    "These worked examples cannot be proven as written, because they describe "
    "the database or the code rather than what a caller sees:\n"
    f"- {_REFUSED_TITLE}\n"
    "\n"
    "Rewrite each of them as what can be proven, keeping the behaviour itself "
    "unchanged: a request to the endpoint and the reply it gets (the method and "
    "path, the status code, and what is in the body), or, for behaviour one "
    "request cannot show — two requests at once, timing — the repository test "
    "that proves it, named. Keep every other worked example exactly as it is."
)
_PRE_CARD_AUTHOR = "planning-driver (stamp normalizer refusal)"

#: Rule 45's two lines, the spec's own words, with the fixture's numbers.
_REWROTE_LINE = (
    "The machine rewrote 1 of the worked examples so they can be proven (they "
    "described the database or the code rather than what a caller sees). What "
    "changed: 1 example changed."
)
_UNPROVABLE_LINE = (
    f"1 of the worked examples cannot be proven as written: “{_REFUSED_TITLE}”. "
    "If you approve, the plan stage will ask the model fallback to place them; "
    "or send a note."
)

#: The rewrite: the refused example said as what the endpoint does.
_REWRITTEN_FEATURE = FEATURE_TEXT.replace(
    "    When an unpublished format is asked for\n    Then the request is refused\n",
    "    When GET /version?format=xml is sent\n    Then the reply is 406\n",
)
_REWRITTEN_DIGEST = DIGEST_YAML.replace(
    "  sentence: Asking for the version in a format the service does not publish is\n"
    "    refused rather than guessed at.\n",
    "  sentence: GET /version with a format the service does not publish answers\n"
    "    406.\n",
)


def _checked(refused: list[str]) -> ScenarioProvabilityOutcome:
    """A rules-only answer over the fixture's two examples."""
    homes = {t: "hurl" for t in (_HOMED_TITLE, _REFUSED_TITLE) if t not in refused}
    return ScenarioProvabilityOutcome(
        status="checked",
        detail=(
            f"{len(refused)} of 2 scenario(s) cannot be proven by rule"
            if refused
            else "every one of the 2 scenario(s) can be proven by rule"
        ),
        refused_titles=tuple(refused),
        homes=homes,
        rules={t: "R9" for t in homes},
        scenario_count=2,
        repo_has_http_surface=True,
        http_surface_evidence="fastapi is an exact dependency in pyproject.toml",
    )


class _RecordingClassify:
    """A fake ``classify_scenarios`` seam: answers in call order (the last
    repeats) and records what it was asked to check."""

    def __init__(self, outcomes: list[ScenarioProvabilityOutcome]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = outcomes

    async def __call__(self, repo_path: Path, feature_text: str) -> ScenarioProvabilityOutcome:
        self.calls.append({"repo_path": repo_path, "feature_text": feature_text})
        return self._outcomes[min(len(self.calls) - 1, len(self._outcomes) - 1)]


def _not_ok_reply(reason: str) -> Any:
    """The checker refusing a revision round (the must-pass note-honoured
    criterion failed twice): a not-ok dispatch with the checker's reason."""
    return SimpleNamespace(
        outcome=SimpleNamespace(value="failed"), role_output={}, reason=reason
    )


def _draft_rows(store: SqlitePlanningRunStore) -> list[tuple[str, dict]]:
    return [(status, d.get("spec_draft") or {}) for status, d in _events(store, _DRAFT_STAGE)]


def _card_summary(h: _Harness, index: int = 0) -> dict[str, Any]:
    return _digest_cards(h)[index].payload["details"]["summary"]


@pytest.mark.asyncio
async def test_a_clean_spec_leaves_the_card_and_the_dispatch_count_unchanged(
    store: SqlitePlanningRunStore,
) -> None:
    """Rule 46's cost: a clean spec costs exactly one spec-writer call, the
    card is byte-identical to today's, and the draft row says the check ran
    and refused nothing. The check saw the COMMITTED .feature, in the target
    repository's checkout."""
    _queue(store)
    classify = _RecordingClassify([_checked([])])
    h = _make_driver(
        store, subscriber_factory=SharedScriptFactory([_answer("approve")]), classify=classify
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 1
    assert len(classify.calls) == 1
    assert classify.calls[0] == {
        "repo_path": Path("/srv/repos/api_test"),
        "feature_text": FEATURE_TEXT,
    }
    assert len(_digest_cards(h)) == 1
    assert _card_summary(h)["what_happened"] == _ROUND_ONE_TEXT
    rows = _draft_rows(store)
    assert [status for status, _ in rows] == ["drafted"]
    receipt = rows[0][1]["provability"]
    assert receipt["checked_by_rule"] is True
    assert receipt["refused_titles"] == []
    assert receipt["rewritten"] is False
    assert receipt["changes"] is None
    assert "round" not in receipt
    assert receipt["check"]["status"] == "checked"
    assert receipt["check"]["homes"] == {_HOMED_TITLE: "hurl", _REFUSED_TITLE: "hurl"}
    # The approved row carries the receipt forward (the plan stage reads it).
    approved = [d for status, d in _events(store, _SPEC_STAGE) if status == "approved"]
    assert approved[-1]["provability"] == receipt


@pytest.mark.asyncio
async def test_one_refused_example_runs_the_machines_note_round_before_the_card(
    store: SqlitePlanningRunStore,
) -> None:
    """Rules 44–46: one example refused by rule → the spec writer is
    dispatched with the machine's note VERBATIM as ``validate_feedback`` and
    the prior spec as ``revision_of`` → the rewrite is checked again, clean →
    ONE card, opening on the rewrite, with rule 45's rewrite line → the draft
    row carries the receipt, the machine log names the round, and the
    owner's touches are unchanged."""
    _queue(store)
    classify = _RecordingClassify([_checked([_REFUSED_TITLE]), _checked([])])
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _spec_reply(),
            _spec_reply(feature=_REWRITTEN_FEATURE, digest=_REWRITTEN_DIGEST),
        ],
        classify=classify,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    first, second = h.ctx["dispatches"]
    assert first["revision_of"] is None and first["validate_feedback"] is None
    # ...and, since 2026-09-13, the coach's ground truth rides the first
    # round too: the run's own sentence, word for word.
    assert first["request_text"] == "add a GET /version endpoint"
    assert second["validate_feedback"] == _PRE_CARD_NOTE
    assert set(second["revision_of"]) == {
        f"{SLUG}.feature",
        f"{SLUG}_assumptions.yaml",
        f"{SLUG}_summary.md",
        f"{SLUG}_digest.yaml",
    }
    assert second["revision_of"][f"{SLUG}.feature"] == FEATURE_TEXT
    # The check ran twice: on the draft as first written, then on the rewrite.
    assert [c["feature_text"] for c in classify.calls] == [FEATURE_TEXT, _REWRITTEN_FEATURE]
    # ONE card, on the rewrite, saying what the machine did in the spec's words.
    assert len(_digest_cards(h)) == 1
    card = _card_summary(h)
    assert card["what_happened"] == f"{_ROUND_ONE_TEXT} {_REWROTE_LINE}"
    assert [row["sentence"] for row in card["what_it_will_do"]][1] == (
        "GET /version with a format the service does not publish answers 406."
    )
    assert card["worked_examples"] == _REWRITTEN_FEATURE
    assert "Your note" not in json.dumps(card)
    # The rows: the first draft superseded by the machine's note (no card on
    # it — the spec text belongs on the row a card opens from), then the
    # rewrite's drafted row with the receipt.
    rows = _draft_rows(store)
    assert [status for status, _ in rows] == ["superseded", "drafted"]
    superseded = rows[0][1]
    assert superseded["superseded_by_note"] == _PRE_CARD_NOTE
    assert superseded["author"] == _PRE_CARD_AUTHOR
    assert superseded["refused_titles"] == [_REFUSED_TITLE]
    assert "card" not in superseded
    receipt = rows[1][1]["provability"]
    assert receipt["checked_by_rule"] is True
    assert receipt["refused_titles"] == [_REFUSED_TITLE]
    assert receipt["rewritten"] is True
    assert receipt["changes"] == "1 example changed"
    assert receipt["round"] == 1
    assert receipt["author"] == _PRE_CARD_AUTHOR
    assert receipt["note"] == _PRE_CARD_NOTE
    assert receipt["refused_by_checker"] is False
    assert receipt["still_refused"] == []
    assert receipt["second_check"]["status"] == "checked"
    assert receipt["card_line"] == _REWROTE_LINE
    # The machine's round is not one of the owner's: no revise row, the
    # owner's budget untouched, and the row the door replays carries no
    # "your note" record.
    assert [status for status, _ in _events(store, _DIGEST_STAGE)] == ["GATED", "approved"]
    assert "rewrite" not in rows[1][1]
    assert rows[1][1]["cycle"] == 1
    # The approved row carries the round, so the plan stage never sends the
    # note again.
    approved = [d for status, d in _events(store, _SPEC_STAGE) if status == "approved"]
    assert approved[-1]["provability"]["round"] == 1
    # Nothing was said to a person beyond the card and its opening ping.
    assert not [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]


@pytest.mark.asyncio
async def test_still_refused_after_the_round_says_so_on_the_card(
    store: SqlitePlanningRunStore,
) -> None:
    """Rule 45's second line: the rewrite landed and changed the list, but
    the example still cannot be proven by rule — the card carries the rewrite
    line AND the cannot-be-proven line naming the title verbatim; the receipt
    says which is still refused; the run goes on to the owner's yes."""
    _queue(store)
    classify = _RecordingClassify([_checked([_REFUSED_TITLE]), _checked([_REFUSED_TITLE])])
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _spec_reply(),
            _spec_reply(feature=_REWRITTEN_FEATURE, digest=_REWRITTEN_DIGEST),
        ],
        classify=classify,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 2
    assert len(_digest_cards(h)) == 1
    assert _card_summary(h)["what_happened"] == (
        f"{_ROUND_ONE_TEXT} {_REWROTE_LINE} {_UNPROVABLE_LINE}"
    )
    receipt = _draft_rows(store)[-1][1]["provability"]
    assert receipt["rewritten"] is True
    assert receipt["changes"] == "1 example changed"
    assert receipt["still_refused"] == [_REFUSED_TITLE]
    assert receipt["card_line"] == f"{_REWROTE_LINE} {_UNPROVABLE_LINE}"


@pytest.mark.asyncio
async def test_a_rewrite_that_changed_nothing_on_the_list_and_is_still_refused_gets_only_the_cannot_be_proven_line(
    store: SqlitePlanningRunStore,
) -> None:
    """The rewrite came back with the same list and the check still refuses
    the example: saying "the machine rewrote it" would be a lie, so the card
    carries only the cannot-be-proven line. The receipt keeps the truth: a
    rewrite landed, nothing on the list changed."""
    _queue(store)
    classify = _RecordingClassify([_checked([_REFUSED_TITLE])])
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_spec_reply(), _spec_reply()],
        classify=classify,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 2
    assert _card_summary(h)["what_happened"] == f"{_ROUND_ONE_TEXT} {_UNPROVABLE_LINE}"
    receipt = _draft_rows(store)[-1][1]["provability"]
    assert receipt["rewritten"] is True
    assert receipt["changes"] is None
    assert receipt["still_refused"] == [_REFUSED_TITLE]


@pytest.mark.asyncio
async def test_a_checker_refused_pre_card_rewrite_opens_the_card_on_the_original_draft(
    store: SqlitePlanningRunStore,
) -> None:
    """Rule 44's last sentence: the checker refused the machine's rewrite
    (the note-honoured criterion failed twice). The run does NOT stop — the
    card opens on the draft as first written, with the cannot-be-proven line,
    and the owner's yes carries on to the plan stage as today."""
    _queue(store)
    classify = _RecordingClassify([_checked([_REFUSED_TITLE])])
    reason = (
        "could not carry out what this round required, after 2 attempts. "
        "'feedback_resolved' must be met"
    )
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_spec_reply(), _not_ok_reply(reason)],
        classify=classify,
    )

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 2
    assert h.ctx["dispatches"][1]["validate_feedback"] == _PRE_CARD_NOTE
    # Only the first draft was checked: there was no rewrite to check again.
    assert [c["feature_text"] for c in classify.calls] == [FEATURE_TEXT]
    assert len(_digest_cards(h)) == 1
    card = _card_summary(h)
    assert card["what_happened"] == f"{_ROUND_ONE_TEXT} {_UNPROVABLE_LINE}"
    assert card["worked_examples"] == FEATURE_TEXT
    assert [row["sentence"] for row in card["what_it_will_do"]][1] == (
        "Asking for the version in a format the service does not publish is "
        "refused rather than guessed at."
    )
    rows = _draft_rows(store)
    assert [status for status, _ in rows] == ["superseded", "drafted"]
    assert rows[1][1]["sha"] == rows[0][1]["sha"]  # the draft of record
    receipt = rows[1][1]["provability"]
    assert receipt["round"] == 1
    assert receipt["rewritten"] is False
    assert receipt["changes"] is None
    assert receipt["refused_by_checker"] is True
    assert receipt["checker_reason"] == reason
    assert receipt["still_refused"] == [_REFUSED_TITLE]
    # Nobody was told the run stopped, because it did not. The words looked
    # for are "stopped at", which is how every stop card in this driver opens
    # ("stopped at the spec", "stopped at writing the task plan"); a bare
    # "stopped" also matches the plan review's own plain line, which says the
    # opposite — that the run was NOT stopped.
    assert not [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert not any("stopped at" in m for _, m, _ in h.ctx["notifications"])


@pytest.mark.asyncio
async def test_an_owners_note_after_the_card_keeps_todays_path_byte_for_byte(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Rule 47's last case: with the check wired and clean, an owner's note
    round is byte-identical to the same run without the check — the same
    dispatches, the same cards word for word, the same rows in the same
    order. The check runs on the rewrite too (the machine checks every list
    before a person reads it) and leaves everything as it was."""
    script = [
        _answer("reject", notes="the second example should be a 404, not a 400"),
        _answer("approve", attempt=1),
    ]

    _queue(store)
    today = _make_driver(store, subscriber_factory=SharedScriptFactory(list(script)))
    await today.driver.drive(CID)

    cx = sqlite_connect.connect_writer(tmp_path / "with-check.db")
    migrations.apply_at_boot(cx)
    checked_store = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    _queue(checked_store)
    classify = _RecordingClassify([_checked([])])
    checked = _make_driver(
        checked_store, subscriber_factory=SharedScriptFactory(list(script)), classify=classify
    )
    await checked.driver.drive(CID)

    for h, st in ((today, store), (checked, checked_store)):
        assert st.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert checked.ctx["dispatches"] == today.ctx["dispatches"]
    assert len(classify.calls) == 2
    assert [c.payload["details"]["summary"] for c in _digest_cards(checked)] == [
        c.payload["details"]["summary"] for c in _digest_cards(today)
    ]
    assert [m for _, m, _ in checked.ctx["notifications"]] == [
        m for _, m, _ in today.ctx["notifications"]
    ]
    assert [s for s, _ in _events(checked_store, _DIGEST_STAGE)] == [
        s for s, _ in _events(store, _DIGEST_STAGE)
    ]
    assert [s for s, _ in _draft_rows(checked_store)] == [s for s, _ in _draft_rows(store)]
    # The only difference on the record is the receipt itself.
    for (_, with_check), (_, without) in zip(_draft_rows(checked_store), _draft_rows(store)):
        with_check = dict(with_check)
        receipt = with_check.pop("provability", None)
        without = dict(without)
        without.pop("provability", None)
        assert with_check == without
        if receipt is not None:
            assert receipt["checked_by_rule"] is True and receipt["refused_titles"] == []


@pytest.mark.asyncio
async def test_an_unwired_or_older_check_leaves_the_card_unchanged_and_says_so(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """No collaborator wired, or a guardkit that predates the verb: the card
    is byte-identical to today's, one spec-writer call, and the draft row says
    the check did not run and why — never silent, never a stop."""
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 1
    assert _card_summary(h)["what_happened"] == _ROUND_ONE_TEXT
    receipt = _draft_rows(store)[-1][1]["provability"]
    assert receipt["checked_by_rule"] is False
    assert receipt["check"]["status"] == "not-wired"
    assert "no provability check is wired" in receipt["not_checked"]

    cx = sqlite_connect.connect_writer(tmp_path / "older.db")
    migrations.apply_at_boot(cx)
    older_store = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    _queue(older_store)
    older = _RecordingClassify(
        [
            ScenarioProvabilityOutcome(
                status="unavailable",
                detail="the guardkit on this image has no `qa classify-scenarios` verb",
            )
        ]
    )
    h2 = _make_driver(
        older_store, subscriber_factory=SharedScriptFactory([_answer("approve")]), classify=older
    )
    await h2.driver.drive(CID)
    assert older_store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h2.ctx["dispatches"]) == 1
    assert _card_summary(h2)["what_happened"] == _ROUND_ONE_TEXT
    receipt = _draft_rows(older_store)[-1][1]["provability"]
    assert receipt["checked_by_rule"] is False
    assert receipt["check"]["status"] == "unavailable"
    assert "no `qa classify-scenarios` verb" in receipt["not_checked"]
    assert not [m for _, m, lvl in h2.ctx["notifications"] if lvl == "error"]


@pytest.mark.asyncio
async def test_a_restart_re_opens_the_card_with_the_provability_line_word_for_word(
    store: SqlitePlanningRunStore,
) -> None:
    """The line is on the row the door replays from: a daemon killed with the
    card live re-opens the SAME card, rewrite line and all, and neither the
    spec writer nor the check runs again."""
    _queue(store)
    publisher = FakePublisher()
    git = RecordingGitRunner()
    classify = _RecordingClassify([_checked([_REFUSED_TITLE]), _checked([])])
    boot1 = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        publisher=publisher,
        git=git,
        spec_replies=[
            _spec_reply(),
            _spec_reply(feature=_REWRITTEN_FEATURE, digest=_REWRITTEN_DIGEST),
        ],
        classify=classify,
    )
    task = asyncio.create_task(boot1.driver.drive(CID))
    for _ in range(600):
        await asyncio.sleep(0.01)
        if _digest_cards(boot1):
            break
    assert _digest_cards(boot1), "the door never put a card on the wire"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert _card_summary(boot1)["what_happened"] == f"{_ROUND_ONE_TEXT} {_REWROTE_LINE}"
    assert [s for s, _ in _draft_rows(store)] == ["superseded", "drafted"]

    classify2 = _RecordingClassify([_checked([])])
    boot2 = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        publisher=publisher,
        git=git,
        classify=classify2,
    )
    await boot2.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(boot2.ctx["dispatches"]) == 0
    assert classify2.calls == []
    cards = _digest_cards(boot2)
    assert len(cards) == 2
    assert cards[0].payload["details"]["summary"] == cards[1].payload["details"]["summary"]
    assert cards[1].payload["details"]["summary"]["what_happened"] == (
        f"{_ROUND_ONE_TEXT} {_REWROTE_LINE}"
    )


# ---------------------------------------------------------------------------
# The assumptions are reviewed before the card (planning coach, 2026-09-13)
# ---------------------------------------------------------------------------

_INVENTING_ASSUMPTIONS = (
    "assumptions:\n"
    "- id: ASSUM-001\n"
    "  assumption: The version string comes from the build metadata.\n"
    "  basis: common practice; the input did not say\n"
    "- id: ASSUM-002\n"
    "  assumption: The endpoint requires authentication\n"
    "  basis: Not stated in input; common security practice for analytics endpoints\n"
)
_INVENTING_DIGEST = DIGEST_YAML + (
    "- id: ASSUM-002\n"
    "  text: The endpoint requires authentication\n"
    "  basis: Not stated in input; common security practice for analytics endpoints\n"
)


@pytest.mark.asyncio
async def test_an_invented_requirement_goes_back_to_the_writer_once_and_the_card_says_so(
    store: SqlitePlanningRunStore,
) -> None:
    """Arm B's defect, replayed: the first draft assumes authentication nobody
    asked for; the reviewer sends it back as the machine's note; the writer
    drops it; the card carries one line saying what happened."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _spec_reply(assumptions=_INVENTING_ASSUMPTIONS, digest=_INVENTING_DIGEST),
            _spec_reply(),  # the rewrite: the invented assumption is gone
        ],
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    dispatches = h.ctx["dispatches"]
    assert len(dispatches) == 2, "exactly one machine round, never a third try"
    note = dispatches[1]["validate_feedback"]
    assert note.startswith("The reviewer found 1 assumption(s) that add something the request did not ask for")
    assert "ASSUM-002" in note and "authentication was not asked for" in note
    assert note.endswith("Change nothing else.")
    superseded = [d for status, d in _events(store, "feature-spec-draft") if status == "superseded"]
    assert superseded and superseded[0]["spec_draft"]["author"] == "planning-driver (assumption review)"
    assert superseded[0]["spec_draft"]["flagged_assumptions"] == ["ASSUM-002"]
    cards = _digest_cards(h)
    assert len(cards) == 1
    text = json.dumps(cards[0].payload)
    assert "The machine's reviewer removed 1 assumption(s) the request did not ask for (authentication)." in text
    assert "not asked for" not in json.dumps(cards[0].payload["details"]["summary"].get("what_the_machine_assumed"))


@pytest.mark.asyncio
async def test_an_invented_requirement_the_writer_keeps_is_shown_with_its_finding(
    store: SqlitePlanningRunStore,
) -> None:
    """The writer would not drop it: the card opens on the rewritten draft with
    the reviewer's finding under that assumption, and nothing is asked twice."""
    _queue(store)
    inventing = _spec_reply(assumptions=_INVENTING_ASSUMPTIONS, digest=_INVENTING_DIGEST)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[inventing, inventing],
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 2
    cards = _digest_cards(h)
    assert len(cards) == 1
    assumed = cards[0].payload["details"]["summary"]["what_the_machine_assumed"]
    kept = next(a for a in assumed if a["assumption"] == "The endpoint requires authentication")
    assert "⚠ not asked for — " in kept["why"]
    assert "authentication was not asked for" in kept["why"]
    untouched = next(a for a in assumed if a["assumption"].startswith("The version string"))
    assert "not asked for" not in untouched["why"]
    assert "reviewer removed" not in json.dumps(cards[0].payload)


@pytest.mark.asyncio
async def test_a_clean_manifest_leaves_the_leg_byte_for_byte(
    store: SqlitePlanningRunStore,
) -> None:
    """No finding, no round, no line: one dispatch and the card as before."""
    _queue(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))
    await h.driver.drive(CID)
    assert len(h.ctx["dispatches"]) == 1
    assert "reviewer" not in json.dumps(_digest_cards(h)[0].payload)


@pytest.mark.asyncio
async def test_a_reviewer_that_cannot_read_never_stops_the_run(
    store: SqlitePlanningRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same posture as the provability check beside it: the card opens as
    written and the receipt says the review could not run."""
    _queue(store)

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("the branch read fell over")

    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]))
    monkeypatch.setattr(
        type(h.driver), "_review_assumptions_on_branch", boom, raising=True
    )
    await h.driver.drive(CID)
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(_digest_cards(h)) == 1
    assert len(h.ctx["dispatches"]) == 1


# ---------------------------------------------------------------------------
# The planner's repository facts reach the coach and the card (2 October 2026
# item 10, 1 October 2026). On 1 October the containerised coordinator had no
# checkout at its repo_path, the fact sheet said nothing, and nothing said
# that it had said nothing.
# ---------------------------------------------------------------------------


def _queue_users_sentence(store: SqlitePlanningRunStore) -> None:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text=(
            "Add a GET /users/created-per-day endpoint that returns the number "
            "of users created on each of the last 7 days."
        ),
        triggered_by="jarvis",
        target_repo=TARGET_REPO,
    )


@pytest.mark.asyncio
async def test_an_unreadable_repository_is_told_to_the_coach_and_on_the_card(
    store: SqlitePlanningRunStore,
) -> None:
    """No checkout where the planner runs and a helper nobody answers at: the
    spec writer's coach is given the explicit unavailable sentence, and the
    card the person approves says so in plain words."""
    import socket

    from forge.planning.sidecar_git_runner import SidecarCodeReader

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    git = RecordingGitRunner()
    git.reader = SidecarCodeReader(url, repo=TARGET_REPO)

    _queue_users_sentence(store)
    h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]), git=git)
    await h.driver.drive(CID)

    given = h.ctx["dispatches"][0]["repository_facts"]
    assert given is not None
    assert given.startswith(
        f"Repository facts unavailable: the sandbox helper at {url} could not "
        "be reached for /code/list-files"
    )
    what_happened = _digest_cards(h)[0].payload["details"]["summary"]["what_happened"]
    assert (
        "The machine could not read the repository while writing this, so "
        "nothing checked it against what the repository already has (the "
        f"sandbox helper at {url} could not be reached for /code/list-files"
    ) in what_happened
    drafted = [d for status, d in _events(store, "feature-spec-draft") if status == "drafted"]
    assert drafted and drafted[-1]["spec_draft"]["repository_facts_unavailable"].startswith(
        "The machine could not read the repository"
    )


@pytest.mark.asyncio
async def test_a_sandboxed_repository_is_read_through_its_helper_for_the_coach(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """No checkout at the coordinator's /srv/repos/api_test; the helper's real
    server answers over a real socket from its own clone, and the coach is
    given the users model with the column that already soft-deletes. The card
    carries no unavailable line."""
    import subprocess
    import threading

    from forge.config.models import ForgeConfig
    from forge.deploy_sidecar.service import build_server
    from forge.planning.sidecar_git_runner import SidecarGitRunner

    clone = tmp_path / "clone"
    (clone / "src" / "users").mkdir(parents=True)
    (clone / "src" / "users" / "models.py").write_text(
        "class User:\n"
        "    __tablename__ = 'users'\n"
        "    email: Mapped[str] = mapped_column(String)\n"
        "    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
        check=True,
    )
    cfg = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {TARGET_REPO: str(clone)}},
        }
    )
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    try:
        git = RecordingGitRunner()
        git.reader = SidecarGitRunner(f"http://{host}:{port}", repo=TARGET_REPO).code_reader()
        _queue_users_sentence(store)
        h = _make_driver(
            store, subscriber_factory=SharedScriptFactory([_answer("approve")]), git=git
        )
        await h.driver.drive(CID)
    finally:
        srv.shutdown()
        srv.server_close()

    assert not Path("/srv/repos/api_test").exists()
    given = h.ctx["dispatches"][0]["repository_facts"] or ""
    assert "`src/users/models.py` declares class User." in given
    assert "deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)" in given
    what_happened = _digest_cards(h)[0].payload["details"]["summary"]["what_happened"]
    assert "could not read the repository" not in what_happened


# ---------------------------------------------------------------------------
# Codex round 3, R4: a file the sheet chose and the helper refused reaches the
# coach and the card — "could not read" alone, "only in part" beside facts.
# ---------------------------------------------------------------------------


def _helper_over_files(tmp_path: Path, files: dict[str, str]):
    import subprocess
    import threading

    from forge.config.models import ForgeConfig
    from forge.deploy_sidecar.service import build_server

    clone = tmp_path / "clone"
    for rel, text in files.items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
        check=True,
    )
    cfg = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {TARGET_REPO: str(clone)}},
        }
    )
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    return f"http://{host}:{port}", srv


_HUGE_USERS_MODEL = (
    "class User:\n    __tablename__ = 'users'\n    deleted_at: Mapped[datetime | None] = mapped_column()\n# "
    + "x" * 270_000
    + "\n"
)


async def _drive_with_helper_files(store, tmp_path: Path, files: dict[str, str], sentence: str):
    from forge.planning.sidecar_git_runner import SidecarCodeReader

    url, srv = _helper_over_files(tmp_path, files)
    try:
        git = RecordingGitRunner()
        git.reader = SidecarCodeReader(url, repo=TARGET_REPO)
        store.record_queued(
            correlation_id=CID,
            originating_user=ORIGINATOR,
            expected_approver=ORIGINATOR,
            request_text=sentence,
            triggered_by="jarvis",
            target_repo=TARGET_REPO,
        )
        h = _make_driver(store, subscriber_factory=SharedScriptFactory([_answer("approve")]), git=git)
        await h.driver.drive(CID)
    finally:
        srv.shutdown()
        srv.server_close()
    return h


@pytest.mark.asyncio
async def test_a_refused_model_and_nothing_else_is_could_not_read_on_coach_and_card(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    h = await _drive_with_helper_files(
        store, tmp_path, {"src/users/models.py": _HUGE_USERS_MODEL}, "Show users"
    )
    given = h.ctx["dispatches"][0]["repository_facts"] or ""
    assert given.startswith(
        "Repository facts unavailable: the files that matter for this request "
        "could not be read: `src/users/models.py` could not be read (the helper answered 400:"
    )
    what_happened = _digest_cards(h)[0].payload["details"]["summary"]["what_happened"]
    assert "The machine could not read the repository while writing this" in what_happened
    assert "src/users/models.py" in what_happened
    plan = [d for status, d in _events(store, "feature-plan") if status == "approved"]
    assert plan and plan[-1]["repository_unavailable"].startswith(
        "The machine could not read the repository while writing this"
    )


@pytest.mark.asyncio
async def test_a_refused_model_beside_other_facts_is_partly_read_on_coach_and_card(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    router = (
        'from fastapi import APIRouter\nrouter = APIRouter(prefix="/users")\n\n'
        '@router.get("/count-today")\nasync def f() -> int: ...\n'
    )
    h = await _drive_with_helper_files(
        store,
        tmp_path,
        {"src/users/models.py": _HUGE_USERS_MODEL, "src/users/router.py": router},
        'Show users at /users/count-today',
    )
    given = h.ctx["dispatches"][0]["repository_facts"] or ""
    assert "defines GET /users/count-today" in given
    assert "Not read, so this sheet is incomplete: `src/users/models.py` could not be read" in given
    what_happened = _digest_cards(h)[0].payload["details"]["summary"]["what_happened"]
    assert "The machine could read the repository only in part while writing this" in what_happened
    assert "could not read the repository" not in what_happened
    # And the plan's record the build gate card reads carries the same state.
    plan = [d for status, d in _events(store, "feature-plan") if status == "approved"]
    assert plan and plan[-1]["repository_unavailable"].startswith(
        "The machine could read the repository only in part while writing this"
    )


# ---------------------------------------------------------------------------
# The spec example check (4 October 2026): worked examples about something the
# request does not mention, by the words the PROJECT declares in its own
# .guardkit/config.yaml. It shares the assumption review's one rewrite, and
# the card names what was removed and what was kept.
# ---------------------------------------------------------------------------

_PADDED_TITLE = "A POST request to the version endpoint is rejected"

_PADDED_FEATURE = FEATURE_TEXT + (
    "\n"
    "  @negative\n"
    f"  Scenario: {_PADDED_TITLE}\n"
    "    Given the service is running\n"
    "    When a POST request is sent to the version endpoint\n"
    "    Then the request is refused\n"
)
_PADDED_DIGEST = DIGEST_YAML.replace(
    "assumptions:\n",
    f"- title: {_PADDED_TITLE}\n"
    "  tags:\n"
    "  - '@negative'\n"
    "  sentence: Sending the version endpoint a POST is refused.\n"
    "assumptions:\n",
)
#: The writer kept the example and quoted the request in its # Why: line.
_PADDED_KEPT_FEATURE = _PADDED_FEATURE.replace(
    f"  @negative\n  Scenario: {_PADDED_TITLE}\n",
    f'  # Why: the request says "a GET /version endpoint"\n  @negative\n  Scenario: {_PADDED_TITLE}\n',
)

_SPEC_EXAMPLES_CONFIG = (
    "memory:\n"
    "  project: scratch_project\n"
    "spec_examples:\n"
    "  not_asked_for:\n"
    "    - name: another request method\n"
    '      example_words: ["POST request*", "non-GET", "405"]\n'
    '      request_words: ["POST", "other methods"]\n'
)

_EXAMPLE_NOTE = (
    "These worked examples look like things the request does not mention:\n"
    f'- "{_PADDED_TITLE}" (another request method)\n'
    "\n"
    "Remove each one unless the request needs it. If you keep one, quote the words of "
    "the request that need it in its # Why: line. Remove any assumption written only "
    "for an example you remove. Do not add other examples of the same kind. Keep every "
    "other worked example exactly as it is."
)
_REMOVED_LINE = (
    f'Removed as not asked for: "{_PADDED_TITLE}". If one of them was needed, send a note.'
)
_KEPT_LINE = (
    f'Not asked for, but kept: "{_PADDED_TITLE}" (another request method). If you '
    "approve, it will be built; to drop it, send a note."
)


class _DeclaringRepository(_EmptyRepository):
    """A readable repository whose only file the planner reads is the
    project's own declaration."""

    def __init__(self, config: str) -> None:
        self._config = config

    def read_text(self, path: str) -> str | None:
        return self._config if path == ".guardkit/config.yaml" else None


def _declaring_git(config: str = _SPEC_EXAMPLES_CONFIG) -> RecordingGitRunner:
    git = RecordingGitRunner()
    git.reader = _DeclaringRepository(config)
    return git


def _padded() -> Any:
    return _spec_reply(feature=_PADDED_FEATURE, digest=_PADDED_DIGEST)


@pytest.mark.asyncio
async def test_a_padded_example_goes_back_once_and_the_card_names_what_was_removed(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_padded(), _spec_reply()],
        git=_declaring_git(),
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    dispatches = h.ctx["dispatches"]
    assert len(dispatches) == 2
    assert dispatches[1]["validate_feedback"] == _EXAMPLE_NOTE
    superseded = [d["spec_draft"] for status, d in _events(store, _DRAFT_STAGE) if status == "superseded"]
    assert superseded[0]["author"] == "planning-driver (spec example check)"
    assert superseded[0]["flagged_examples"] == [_PADDED_TITLE]
    assert superseded[0]["flagged_assumptions"] == []
    cards = _digest_cards(h)
    assert len(cards) == 1
    card = cards[0].payload["details"]["summary"]
    assert card["what_happened"] == f"{_ROUND_ONE_TEXT} {_REMOVED_LINE}"
    assert card["worked_examples"] == FEATURE_TEXT
    drafted = [d["spec_draft"] for status, d in _events(store, _DRAFT_STAGE) if status == "drafted"]
    receipt = drafted[-1]["example_review"]
    assert receipt["checked"] is True
    assert receipt["first"]["flagged"] == [{"title": _PADDED_TITLE, "kinds": ["another request method"]}]
    assert receipt["final"]["flagged"] == []
    assert receipt["card_lines"] == [_REMOVED_LINE]


@pytest.mark.asyncio
async def test_a_padded_example_the_writer_keeps_with_a_reason_is_named_and_the_card_still_goes(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    kept = _spec_reply(feature=_PADDED_KEPT_FEATURE, digest=_PADDED_DIGEST)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_padded(), kept],
        git=_declaring_git(),
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 2
    cards = _digest_cards(h)
    assert len(cards) == 1
    card = cards[0].payload["details"]["summary"]
    assert card["what_happened"] == f"{_ROUND_ONE_TEXT} {_KEPT_LINE}"
    assert card["worked_examples"] == _PADDED_KEPT_FEATURE


@pytest.mark.asyncio
async def test_assumptions_and_examples_share_one_rewrite_not_two(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    both = _spec_reply(
        feature=_PADDED_FEATURE,
        digest=_PADDED_DIGEST.replace(
            "  basis: common practice; the input did not say\n",
            "  basis: common practice; the input did not say\n"
            "- id: ASSUM-002\n"
            "  text: The endpoint requires authentication\n"
            "  basis: Not stated in input; common security practice for analytics endpoints\n",
        ),
        assumptions=_INVENTING_ASSUMPTIONS,
    )
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[both, _spec_reply()],
        git=_declaring_git(),
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    dispatches = h.ctx["dispatches"]
    assert len(dispatches) == 2, "one shared machine round, never two"
    note = dispatches[1]["validate_feedback"]
    assert note.startswith("The reviewer found 1 assumption(s) that add something the request did not ask for:\n- ASSUM-002: ")
    assert (
        "\n\nThese worked examples look like things the request does not mention:\n"
        f'- "{_PADDED_TITLE}" (another request method)\n\n'
    ) in note
    assert note.endswith(
        "\n\nRemove these assumptions and every worked example that depends on them. "
        "Remove each worked example listed above unless the request needs it; if you "
        "keep one, quote the words of the request that need it in its # Why: line. "
        "Remove any assumption written only for an example you remove. Do not add "
        "other assumptions or examples of the same kind. Change nothing else."
    )
    # One closing instruction, not two that contradict each other.
    assert note.count("Change nothing else.") == 1
    assert "Keep every other worked example exactly as it is." not in note
    superseded = [d["spec_draft"] for status, d in _events(store, _DRAFT_STAGE) if status == "superseded"]
    assert len(superseded) == 1
    assert superseded[0]["author"] == "planning-driver (assumption review)"
    assert superseded[0]["flagged_assumptions"] == ["ASSUM-002"]
    assert superseded[0]["flagged_examples"] == [_PADDED_TITLE]
    what_happened = _digest_cards(h)[0].payload["details"]["summary"]["what_happened"]
    assert what_happened == (
        f"{_ROUND_ONE_TEXT} The machine's reviewer removed 1 assumption(s) the request "
        f"did not ask for (authentication). {_REMOVED_LINE}"
    )


@pytest.mark.asyncio
async def test_a_checker_refused_rewrite_opens_on_the_first_draft_and_names_what_was_kept(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_padded(), _not_ok_reply("'feedback_resolved' must be met")],
        git=_declaring_git(),
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 2
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["worked_examples"] == _PADDED_FEATURE
    assert card["what_happened"] == f"{_ROUND_ONE_TEXT} {_KEPT_LINE}"
    assert not [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]


@pytest.mark.asyncio
async def test_an_example_the_owner_asked_for_in_a_note_is_never_sent_back(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [_answer("reject", notes="Also show that a POST is refused."), _answer("approve", attempt=1)]
        ),
        spec_replies=[_spec_reply(), _padded()],
        git=_declaring_git(),
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    dispatches = h.ctx["dispatches"]
    assert len(dispatches) == 2, "the owner's round only; no machine round"
    assert dispatches[1]["validate_feedback"] == "Also show that a POST is refused."
    cards = _digest_cards(h)
    assert len(cards) == 2
    second = cards[1].payload["details"]["summary"]
    assert second["worked_examples"] == _PADDED_FEATURE
    assert "not asked for" not in second["what_happened"].lower()


@pytest.mark.asyncio
async def test_no_declared_list_leaves_the_card_exactly_as_today(
    store: SqlitePlanningRunStore,
) -> None:
    """The same padded spec, from a project that declares nothing: one
    dispatch, the card word for word as before, and the record says why
    there was no check."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_padded()],
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 1
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == _ROUND_ONE_TEXT
    drafted = [d["spec_draft"] for status, d in _events(store, _DRAFT_STAGE) if status == "drafted"]
    assert [status for status, _ in _events(store, _DRAFT_STAGE)] == ["drafted"]
    receipt = drafted[-1]["example_review"]
    assert receipt["checked"] is False
    assert receipt["unreadable"] is None
    assert receipt["not_checked"] == "`.guardkit/config.yaml` was not read (it was not served)"


@pytest.mark.asyncio
async def test_a_declared_list_that_cannot_be_read_is_said_on_the_card(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_padded()],
        git=_declaring_git("spec_examples:\n  not_asked_for: outage\n"),
    )
    await h.driver.drive(CID)

    assert len(h.ctx["dispatches"]) == 1
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == (
        f"{_ROUND_ONE_TEXT} The project's list of examples it does not want unless "
        "asked for could not be read (has no `not_asked_for` list), so the worked "
        "examples were not checked against it."
    )


@pytest.mark.asyncio
async def test_a_sandboxed_projects_list_is_read_through_its_helper(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The list is read where the builds read the repository: the helper's
    read-only route, over a real socket, on the factory's own clone."""
    from forge.planning.sidecar_git_runner import SidecarCodeReader

    url, srv = _helper_over_files(tmp_path, {".guardkit/config.yaml": _SPEC_EXAMPLES_CONFIG})
    try:
        git = RecordingGitRunner()
        git.reader = SidecarCodeReader(url, repo=TARGET_REPO)
        _queue(store)
        h = _make_driver(
            store,
            subscriber_factory=SharedScriptFactory([_answer("approve")]),
            spec_replies=[_padded(), _spec_reply()],
            git=git,
        )
        await h.driver.drive(CID)
    finally:
        srv.shutdown()
        srv.server_close()

    assert len(h.ctx["dispatches"]) == 2
    assert h.ctx["dispatches"][1]["validate_feedback"] == _EXAMPLE_NOTE
    assert _digest_cards(h)[0].payload["details"]["summary"]["what_happened"].endswith(_REMOVED_LINE)


@pytest.mark.asyncio
async def test_an_assumption_review_that_cannot_read_still_sends_the_flagged_examples_back(
    store: SqlitePlanningRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card never names as kept an example the writer was never asked about."""
    _queue(store)

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("the branch read fell over")

    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_padded(), _spec_reply()],
        git=_declaring_git(),
    )
    monkeypatch.setattr(type(h.driver), "_review_assumptions_on_branch", boom, raising=True)
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(h.ctx["dispatches"]) == 2
    assert h.ctx["dispatches"][1]["validate_feedback"] == _EXAMPLE_NOTE
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == f"{_ROUND_ONE_TEXT} {_REMOVED_LINE}"
    drafted = [d["spec_draft"] for status, d in _events(store, _DRAFT_STAGE) if status == "drafted"]
    assert drafted[-1]["example_review"]["card_lines"] == [_REMOVED_LINE]


@pytest.mark.asyncio
async def test_a_slow_spec_writer_does_not_use_up_the_sandbox_reading_allowance(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The fact sheet starts the helper reader's 120-second allowance before
    the first spec dispatch; the writer then takes minutes. The project's list
    is read after that, with an allowance of its own, so the check still runs."""
    from forge.planning.sidecar_git_runner import SidecarCodeReader

    url, srv = _helper_over_files(tmp_path, {".guardkit/config.yaml": _SPEC_EXAMPLES_CONFIG})
    try:
        git = RecordingGitRunner()
        _queue(store)
        h = _make_driver(
            store,
            subscriber_factory=SharedScriptFactory([_answer("approve")]),
            spec_replies=[_padded(), _spec_reply()],
            git=git,
        )
        # Each spec dispatch takes 200 seconds on this clock.
        git.reader = SidecarCodeReader(
            url, repo=TARGET_REPO, clock=lambda: 200.0 * len(h.ctx["dispatches"])
        )
        await h.driver.drive(CID)
    finally:
        srv.shutdown()
        srv.server_close()

    assert len(h.ctx["dispatches"]) == 2
    assert h.ctx["dispatches"][1]["validate_feedback"] == _EXAMPLE_NOTE
    what_happened = _digest_cards(h)[0].payload["details"]["summary"]["what_happened"]
    assert what_happened.endswith(_REMOVED_LINE)


# ---------------------------------------------------------------------------
# The possible contradiction (4 October 2026, the owner: "yes make the change so
# it's a warning on the card"). The spec writer no longer refuses over a pair
# its reviewer says cannot both be true: it writes coherence_warning.json beside
# the spec, and the card carries the pair in a field of its own.
# ---------------------------------------------------------------------------

_KEY_TITLE = "Version endpoint returns the running build"
_ASSUMPTION_TEXT = "The version string comes from the build metadata."
_WARNING_PAIR = {
    "first": _KEY_TITLE,
    "second": _ASSUMPTION_TEXT,
    "why": "One example reads the build, the assumption reads the metadata.",
}
_EXPECTED_WARNING = (
    "Possible contradiction, found by the machine's reviewer and not checked by "
    f'a person: "{_KEY_TITLE}" and "{_ASSUMPTION_TEXT}". Its reason: "One '
    'example reads the build, the assumption reads the metadata.". If they '
    "really conflict, send a note; otherwise approve as usual."
)


def _warned_reply(record: Any) -> Any:
    reply = _spec_reply()
    reply.role_output["coherence_warning.json"] = (
        record if isinstance(record, str) else json.dumps(record)
    )
    return reply


def _warning(pairs: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "pairs": [_WARNING_PAIR] if pairs is None else pairs,
        "dropped_after_repair": [],
        "spec_changed_after_check": False,
    }


async def _one_card(store: SqlitePlanningRunStore, reply: Any) -> tuple[_Harness, dict]:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[reply],
    )
    await h.driver.drive(CID)
    return h, _digest_cards(h)[0].payload["details"]["summary"]


async def _todays_card(tmp_path: Path) -> dict:
    cx = sqlite_connect.connect_writer(tmp_path / "today.db")
    migrations.apply_at_boot(cx)
    other = SqlitePlanningRunStore(cx, target_terminal_enabled=True)
    _h, summary = await _one_card(other, _spec_reply())
    return summary


@pytest.mark.asyncio
async def test_the_possible_contradiction_rides_the_card_and_the_draft_row(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    h, summary = await _one_card(store, _warned_reply(_warning()))

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert summary["possible_contradiction"] == _EXPECTED_WARNING
    # The opening paragraph is exactly what it is without a warning.
    today = await _todays_card(tmp_path)
    assert summary["what_happened"] == today["what_happened"]
    assert {k: v for k, v in summary.items() if k != "possible_contradiction"} == today

    # Kept on the draft row, so a restart replays it.
    drafted = [d for status, d in _events(store, _DRAFT_STAGE) if status == "drafted"]
    record = drafted[-1]["spec_draft"]
    assert record["card"]["possible_contradiction"] == _EXPECTED_WARNING
    assert record["coherence_warning"]["pairs"] == [_WARNING_PAIR]
    # Never committed: only the spec files reach the branch.
    assert "coherence_warning.json" not in record["spec_files"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        None,
        {
            "pairs": [],
            "dropped_after_repair": [
                {**_WARNING_PAIR, "reason": "the spec changed after the check"}
            ],
            "spec_changed_after_check": True,
        },
    ],
    ids=["no file", "every pair dropped"],
)
async def test_no_warning_leaves_the_card_exactly_as_today(
    store: SqlitePlanningRunStore, tmp_path: Path, extra: Any
) -> None:
    reply = _spec_reply() if extra is None else _warned_reply(extra)
    _h, summary = await _one_card(store, reply)
    assert "possible_contradiction" not in summary
    assert summary == await _todays_card(tmp_path)
    drafted = [d for status, d in _events(store, _DRAFT_STAGE) if status == "drafted"]
    assert "coherence_warning" not in drafted[-1]["spec_draft"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "{not json",
        json.dumps(["a list"]),
        json.dumps({"pairs": "two"}),
        json.dumps({"pairs": [{"first": "only one side"}]}),
    ],
)
async def test_a_bad_warning_file_gives_no_field_and_the_leg_carries_on(
    store: SqlitePlanningRunStore, bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        _h, summary = await _one_card(store, _warned_reply(bad))
    assert "possible_contradiction" not in summary
    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert "coherence_warning.json could not be read" in caplog.text


@pytest.mark.asyncio
async def test_a_rewrite_after_a_note_carries_its_own_warning_or_none(
    store: SqlitePlanningRunStore,
) -> None:
    """The value comes from each reply. A note's rewrite without a pair drops
    the field; a rewrite with a different pair shows that pair."""
    other_pair = {
        "first": "Version endpoint rejects an unknown format",
        "second": _ASSUMPTION_TEXT,
        "why": "",
    }
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory(
            [
                _answer("reject", notes="say which build"),
                _answer("reject", notes="and the format", attempt=1),
                _answer("approve", attempt=2),
            ]
        ),
        spec_replies=[
            _warned_reply(_warning()),
            _spec_reply(),
            _warned_reply(_warning([other_pair])),
        ],
    )
    await h.driver.drive(CID)

    first, second, third = (
        card.payload["details"]["summary"] for card in _digest_cards(h)
    )
    assert first["possible_contradiction"] == _EXPECTED_WARNING
    assert "possible_contradiction" not in second
    assert third["possible_contradiction"] == (
        "Possible contradiction, found by the machine's reviewer and not checked "
        'by a person: "Version endpoint rejects an unknown format" and '
        f'"{_ASSUMPTION_TEXT}". If they really conflict, send a note; otherwise '
        "approve as usual."
    )
    drafted = [d for status, d in _events(store, _DRAFT_STAGE) if status == "drafted"]
    assert [("coherence_warning" in d["spec_draft"]) for d in drafted] == [
        True,
        False,
        True,
    ]


@pytest.mark.asyncio
async def test_a_restart_replays_the_possible_contradiction(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    publisher = FakePublisher()
    git = RecordingGitRunner()
    boot1 = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([]),
        publisher=publisher,
        git=git,
        spec_replies=[_warned_reply(_warning())],
    )
    task = asyncio.create_task(boot1.driver.drive(CID))
    for _ in range(600):
        await asyncio.sleep(0.01)
        if _digest_cards(boot1):
            break
    assert _digest_cards(boot1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    boot2 = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        publisher=publisher,
        git=git,
    )
    await boot2.driver.drive(CID)
    assert len(boot2.ctx["dispatches"]) == 0
    cards = _digest_cards(boot2)
    assert [c.payload["details"]["summary"]["possible_contradiction"] for c in cards] == [
        _EXPECTED_WARNING,
        _EXPECTED_WARNING,
    ]


def test_two_pairs_are_shown_then_and_n_more_under_1400_characters() -> None:
    from forge.planning.driver import _possible_contradiction_text

    pairs = [
        {"first": "F" * 400, "second": "S" * 400, "why": "W" * 900} for _ in range(5)
    ]
    text = _possible_contradiction_text(pairs)
    assert len(text) < 1400
    assert text.startswith(
        "Possible contradiction, found by the machine's reviewer and not checked "
        "by a person:"
    )
    assert text.count("Its reason:") == 2
    assert "And 3 more." in text
    assert text.endswith("If they really conflict, send a note; otherwise approve as usual.")


def test_and_n_more_counts_every_pair_found_not_only_those_in_the_file() -> None:
    """Coach follow-up 2: the spec writer's file keeps at most three pairs and
    records the total as pair_count; the card counts from that total."""
    from forge.planning.driver import PlanningRunDriver

    pairs = [
        {"first": f"Example {n}", "second": _ASSUMPTION_TEXT, "why": ""}
        for n in range(1, 4)
    ]
    reply = _warned_reply({**_warning(pairs), "pair_count": 5})
    warning = PlanningRunDriver._capture_coherence_warning(reply.role_output, CID)
    assert warning is not None
    assert warning["pair_count"] == 5
    assert "And 3 more." in warning["possible_contradiction"]

    # An older file without the count, or a count that is not a number, counts
    # the pairs in the file.
    for record in (_warning(pairs), {**_warning(pairs), "pair_count": "five"}):
        reply = _warned_reply(record)
        warning = PlanningRunDriver._capture_coherence_warning(reply.role_output, CID)
        assert "And 1 more." in warning["possible_contradiction"]
        assert warning["pair_count"] == 3


# ---------------------------------------------------------------------------
# The request as one side (4 October 2026, the better coherence check). The
# spec writer may now name the request itself as one side of a pair, written as
# ``the request: "<text>"``. This file is exactly what the spec writer's own
# connected test writes for the saved B9 draft (specialist-agent
# tests/fixtures/coherence_better_check/b9_request_coherence_warning.json); it
# reaches the card through the real draft and card path, with no Forge change.
# ---------------------------------------------------------------------------

_B9_REQUEST_WARNING = Path(__file__).parent / "fixtures" / "b9_request_coherence_warning.json"


@pytest.mark.asyncio
async def test_a_pair_against_the_request_reaches_the_card_whole(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    record = json.loads(_B9_REQUEST_WARNING.read_text(encoding="utf-8"))
    pair = record["pairs"][0]
    assert pair["first"].startswith('the request: "') and pair["first"].endswith('"')
    assert len(pair["first"]) <= 150  # the spec writer keeps the whole side within the cap

    h, summary = await _one_card(store, _warned_reply(record))

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # The request side is shown whole: no cut, its closing quote kept.
    assert summary["possible_contradiction"] == (
        "Possible contradiction, found by the machine's reviewer and not checked "
        f'by a person: "{pair["first"]}" and "{pair["second"]}". Its reason: '
        f'"{pair["why"]}". If they really conflict, send a note; otherwise '
        "approve as usual."
    )
    assert 'the request: "Add a GET /users/created-per-day endpoint' in summary[
        "possible_contradiction"
    ]
    assert "…" not in summary["possible_contradiction"]
    assert "And " not in summary["possible_contradiction"]  # pair_count is 1
    assert len(summary["possible_contradiction"]) < 1400
    # Every other card field is exactly today's.
    today = await _todays_card(tmp_path)
    assert {k: v for k, v in summary.items() if k != "possible_contradiction"} == today

    drafted = [d for status, d in _events(store, _DRAFT_STAGE) if status == "drafted"]
    assert drafted[-1]["spec_draft"]["coherence_warning"]["pairs"] == [pair]
    assert drafted[-1]["spec_draft"]["coherence_warning"]["pair_count"] == 1


# ---------------------------------------------------------------------------
# Planning improvements, item 4 (6 October 2026): two more example checks,
# for every project and with no project words — the # Why: line must quote
# the request (or a note, or a project document) for real, and the spec
# writer's checker says whether an example asks for more than its quote.
# They share the same note, the one rewrite and the card lines. They run ONLY
# when the draft carries the checker's example_support.json, which the spec
# writer sends once its own switch is on; without it, everything above holds
# word for word. None of these projects declares a spec_examples block.
# ---------------------------------------------------------------------------

from forge.planning.project_documents import ProjectDocument  # noqa: E402

#: FEATURE_TEXT as the switched-on spec writer writes it: each example
#: quotes the request in its # Why: line.
_QUOTED_FEATURE_TEXT = FEATURE_TEXT.replace(
    "  @key-example @smoke\n", '  # Why: "add a GET /version endpoint"\n  @key-example @smoke\n'
).replace("  @negative\n", '  # Why: "a GET /version endpoint"\n  @negative\n')

#: The checker's file when it judged every example to follow from its quote.
_CLEAN_SUPPORT = {"status": "checked", "checked_request": True, "goes_beyond": []}

_UNQUOTED_TITLE = "A version read survives a restart of the service"


def _supported(reply: Any, support: dict | None) -> Any:
    if support is not None:
        reply.role_output["example_support.json"] = json.dumps(support)
    return reply


def _quoted_reply(support: dict | None = _CLEAN_SUPPORT) -> Any:
    return _supported(_spec_reply(feature=_QUOTED_FEATURE_TEXT), support)


def _reply_with_example(title: str, why: str, *, support: dict | None = _CLEAN_SUPPORT) -> Any:
    """_QUOTED_FEATURE_TEXT and DIGEST_YAML with one more worked example."""
    feature = _QUOTED_FEATURE_TEXT + (
        "\n"
        + (f"  # Why: {why}\n" if why else "")
        + "  @edge-case\n"
        f"  Scenario: {title}\n"
        "    Given the service has restarted\n"
        "    When the version is asked for\n"
        "    Then the same build comes back\n"
    )
    digest = DIGEST_YAML.replace(
        "assumptions:\n",
        f"- title: {title}\n"
        "  tags:\n"
        "  - '@edge-case'\n"
        "  sentence: After a restart the same build comes back.\n"
        "assumptions:\n",
    )
    return _supported(_spec_reply(feature=feature, digest=digest), support)


_QUOTE_NOTE = (
    "These worked examples look like things the request does not mention:\n"
    f'- "{_UNQUOTED_TITLE}" (it quotes no words of the request)\n'
    "\n"
    "Remove each one unless the request needs it. If you keep one, quote the words of "
    "the request that need it in its # Why: line, copied exactly, in double quotes, "
    "and the example asks for nothing more than those words do. Remove any assumption "
    "written only for an example you remove. Do not add other examples of the same "
    "kind. Keep every other worked example exactly as it is."
)


def _last_receipt(store: SqlitePlanningRunStore) -> dict:
    drafted = [d["spec_draft"] for status, d in _events(store, _DRAFT_STAGE) if status == "drafted"]
    return drafted[-1]["example_review"]


@pytest.mark.asyncio
async def test_without_the_checkers_file_an_unquoted_example_changes_nothing(
    store: SqlitePlanningRunStore,
) -> None:
    """The spec writer's switch is off: no quote check, no new card line,
    and the receipt is the 4 October one."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_reply_with_example(_UNQUOTED_TITLE, "", support=None)],
    )
    await h.driver.drive(CID)

    assert len(h.ctx["dispatches"]) == 1
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == _ROUND_ONE_TEXT
    assert _last_receipt(store) == {
        "checked": False,
        "kinds": None,
        "not_checked": "`.guardkit/config.yaml` was not read (it was not served)",
        "unreadable": None,
    }


@pytest.mark.asyncio
async def test_with_no_project_words_an_unquoted_example_goes_back_and_the_card_says_removed(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _reply_with_example(_UNQUOTED_TITLE, "the request asks for durable versions"),
            _quoted_reply(),
        ],
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    dispatches = h.ctx["dispatches"]
    assert len(dispatches) == 2
    assert dispatches[1]["validate_feedback"] == _QUOTE_NOTE
    removed = f'Removed as not asked for: "{_UNQUOTED_TITLE}". If one of them was needed, send a note.'
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == f"{_ROUND_ONE_TEXT} {removed}"
    receipt = _last_receipt(store)
    assert receipt["checked"] is True
    assert receipt["project_words"]["kinds"] is None
    assert receipt["quotes"]["checked"] is True and receipt["quotes"]["guard_fired"] is False
    assert receipt["quotes"]["first"]["untraced"] == [_UNQUOTED_TITLE]
    assert receipt["quotes"]["final"]["untraced"] == []
    assert receipt["reading"] == {
        "status": "checked",
        "first": {"status": "checked", "goes_beyond": []},
        "final": {"status": "checked", "goes_beyond": []},
    }
    assert receipt["first"]["flagged"] == [
        {"title": _UNQUOTED_TITLE, "kinds": ["it quotes no words of the request"]}
    ]
    assert receipt["card_lines"] == [removed]


@pytest.mark.asyncio
async def test_with_no_project_words_an_unquoted_example_the_writer_keeps_is_named(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _reply_with_example(_UNQUOTED_TITLE, ""),
            _reply_with_example(_UNQUOTED_TITLE, ""),
        ],
    )
    await h.driver.drive(CID)

    assert len(h.ctx["dispatches"]) == 2
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == (
        f'{_ROUND_ONE_TEXT} Not asked for, but kept: "{_UNQUOTED_TITLE}" (it quotes no '
        "words of the request). If you approve, it will be built; to drop it, send a note."
    )


@pytest.mark.asyncio
async def test_an_example_that_quotes_a_project_document_is_not_flagged(
    store: SqlitePlanningRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _reply_with_example(_UNQUOTED_TITLE, '"the version survives every restart"')
        ],
    )
    document = ProjectDocument.of(
        "docs/rules.md", "# Rules\n\nThe version survives every restart.\n", "c" * 40
    )
    monkeypatch.setattr(
        h.driver, "_recorded_project_documents", lambda correlation_id: ((document,), None)
    )
    # The writer is sent the document; this stand-in writer takes no context.
    dispatch = h.driver._deps.dispatch_feature_spec

    async def without_context(**kwargs: Any) -> Any:
        assert kwargs.pop("context") == [f"File: docs/rules.md\n{document.text}"]
        return await dispatch(**kwargs)

    monkeypatch.setattr(h.driver._deps, "dispatch_feature_spec", without_context)
    await h.driver.drive(CID)

    assert len(h.ctx["dispatches"]) == 1
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == _ROUND_ONE_TEXT


_BEYOND = {
    "status": "checked",
    "checked_request": True,
    "goes_beyond": [{"title": _UNQUOTED_TITLE, "why": "nothing in the request asks about restarts"}],
}


@pytest.mark.asyncio
async def test_an_example_the_checker_reads_as_asking_for_more_is_sent_back_and_shown_kept(
    store: SqlitePlanningRunStore,
) -> None:
    """The quote is real, so only the checker's reading can catch it: judged
    to ask for more than its quote in the first and the final draft, it is
    named as kept."""
    _queue(store)
    real_quote = '"add a GET /version endpoint"'
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _reply_with_example(_UNQUOTED_TITLE, real_quote, support=_BEYOND),
            _reply_with_example(_UNQUOTED_TITLE, real_quote, support=_BEYOND),
        ],
    )
    await h.driver.drive(CID)

    dispatches = h.ctx["dispatches"]
    assert len(dispatches) == 2
    assert (
        f'- "{_UNQUOTED_TITLE}" (it asks for more than the words it quotes)'
        in dispatches[1]["validate_feedback"]
    )
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == (
        f'{_ROUND_ONE_TEXT} Not asked for, but kept: "{_UNQUOTED_TITLE}" (it asks for '
        "more than the words it quotes). If you approve, it will be built; to drop it, "
        "send a note."
    )
    receipt = _last_receipt(store)
    assert receipt["checked"] is True
    assert receipt["quotes"]["final"]["untraced"] == []
    assert receipt["reading"]["status"] == "checked"
    assert receipt["reading"]["final"]["goes_beyond"] == _BEYOND["goes_beyond"]
    assert set(receipt) >= {"checked", "project_words", "quotes", "reading", "first", "final", "card_lines"}


@pytest.mark.asyncio
async def test_a_draft_that_mostly_quotes_nothing_is_not_sent_back_and_the_card_says_so(
    store: SqlitePlanningRunStore,
) -> None:
    """The checker ran but the examples carry no quotes: the whole spec is
    not sent back, and the card says the quotes were not checked."""
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[_supported(_spec_reply(), _CLEAN_SUPPORT)],
    )
    await h.driver.drive(CID)

    assert len(h.ctx["dispatches"]) == 1
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == (
        f"{_ROUND_ONE_TEXT} Most worked examples do not quote the request in their "
        "# Why: line, so their quotes were not checked."
    )
    assert _last_receipt(store)["quotes"]["guard_fired"] is True


@pytest.mark.asyncio
async def test_a_checker_that_gave_no_answer_is_said_on_the_card(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[
            _quoted_reply({"status": "no_verdict", "checked_request": True, "goes_beyond": []})
        ],
    )
    await h.driver.drive(CID)

    assert len(h.ctx["dispatches"]) == 1
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == (
        f"{_ROUND_ONE_TEXT} The check of whether each example follows from the "
        "request gave no answer."
    )


@pytest.mark.asyncio
async def test_an_unreadable_checker_file_is_ignored_and_never_fails_the_leg(
    store: SqlitePlanningRunStore,
) -> None:
    _queue(store)
    reply = _spec_reply()
    reply.role_output["example_support.json"] = "{not json"
    h = _make_driver(
        store,
        subscriber_factory=SharedScriptFactory([_answer("approve")]),
        spec_replies=[reply],
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    card = _digest_cards(h)[0].payload["details"]["summary"]
    assert card["what_happened"] == _ROUND_ONE_TEXT
