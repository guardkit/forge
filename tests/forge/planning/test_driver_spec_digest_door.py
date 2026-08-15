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
  and the quality-checklist leg opens no second door.

Real v4 SQLite store, real gate adapters, fakes at the wire seams. No broker, no
network: every subscription is an in-test double.
"""

from __future__ import annotations

import asyncio
import json
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
from nats_core.events import ApprovalResponsePayload

CID = "digest-run-0001"
PLAN_RUN_ID = f"plan-{CID}"
ORIGINATOR = "U0RIGINATOR"
TARGET_REPO = "guardkit/api_test"
SLUG = "version-endpoint"
BRANCH = f"planning/{CID}"

_DIGEST_STAGE = "feature-spec-digest-review"
_DIGEST_CHECKPOINT_TYPE = "product_docs_spec_digest"
_DRAFT_STAGE = "feature-spec-draft"

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


class RecordingGitRunner:
    """Records tree writes, runs the pre-commit hook, serves files back."""

    def __init__(self) -> None:
        self.tree_calls: list[dict[str, Any]] = []
        self._branch_files: dict[str, dict[str, str]] = {}

    async def prepare_branch_and_write(
        self, repo_path: str, branch: str, file_path: str, content: str
    ) -> GitOpResult:
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
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={
            f".guardkit/features/{feature_id}.yaml": (
                f"id: {feature_id}\ntasks:\n- id: TASK-VER-001\n"
            ),
            f"tasks/backlog/{SLUG}/TASK-VER-001.md": "# task\n",
            "validation.json": json.dumps(
                {"accepted": True, "errors": [], "gates_run": ["feature_validate"]}
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
    ) -> Any:
        dispatches.append(
            {"revision_of": revision_of, "validate_feedback": validate_feedback}
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
) -> ApprovalResponsePayload:
    return ApprovalResponsePayload(
        request_id=_digest_request_id(attempt),
        decision=decision,
        decided_by=decided_by,
        notes=notes,
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
    assert first == {"revision_of": None, "validate_feedback": None}
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
