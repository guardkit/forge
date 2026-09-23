"""Every planning leg's checks run where the repository lives (sandbox first,
2026-09-07, rules 70 and 87).

Rich's rule: nothing the factory runs on a repository runs on the host. When
the repository has a sandbox of its own, no planning leg runs its oracle in a
worktree beside the driver. Each one DECLARES its checks by name — the spec
leg's gherkin normalizer and Part K's provability check, the plan leg's stamp
normalizer and ``feature validate``, one ``qa validate pass-bar`` per minted
bar, and ``qa validate gate-registry`` — the deploy sidecar inside the sandbox
runs them with the guardkit beside it, and their outcomes come back with the
commit. What a refusal means, what the receipts say, which stamping ran by
rule only, what the card says and whether the machine's rewrite round fires
must be exactly what they are when the checks run here.

These tests drive the whole planning run against a REAL sidecar on an
ephemeral loopback port, against a real git repository, with a stand-in
``guardkit`` binary and a stand-in normalizer module running the declared
checks — so the argv, the exit codes, the JSON and the commit are the real
ones.

The fence is still pinned at the bottom: a Python closure handed to a sandbox
runner is refused in one plain sentence rather than quietly run on the host.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import (
    COORDINATOR_OWNER_ENV,
    GUARDKIT_PATH_ENV,
    build_server,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.driver import PlanningRunDriver
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.sidecar_git_runner import CLOSURE_REFUSED_SENTENCE, SidecarGitRunner
from forge.planning.states import PlanningState

from tests.forge.deploy_sidecar._fake_guardkit import (
    NORMALIZED_MARKER,
    REFUSED_TITLES,
    classify_calls,
    fake_normalizer,
    git_show,
    normalize_calls,
    normalizer_calls,
    qa_validate_calls,
    validate_calls,
    write_fake_guardkit,
)
from tests.forge.planning.test_driver_target_terminal import (
    CID,
    _ROUND19_SEED_AUTHLESS,
    _approved_spec_rows,
    _commit_repo_routing_law,
    _drive_to_failure,
    _git_env,
    _error_cards,
    _init_scratch_repo,
    _leg_details,
    _make_driver,
    _plan_result,
    _plan_result_native,
    _plan_result_native_versions,
    _plan_yaml_rel,
    _queue,
    _rewritten_spec_result,
    _seed_gate_surface,
    _spec_by_round,
    _spec_result_native,
    _spec_result_with_seed,
)

REPO_KEY = "guardkit/api_test"

#: The committed spec's ``.feature`` path for the fixture the spec results use.
FEATURE_REL = "features/stats-endpoint/stats-endpoint.feature"


@pytest.fixture
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    cx = sqlite_connect.connect_writer(tmp_path / "sandbox.db")
    migrations.apply_at_boot(cx)
    return SqlitePlanningRunStore(cx, target_terminal_enabled=True)


@pytest.fixture(autouse=True)
def the_records_own_answer(
    store: SqlitePlanningRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """This factory's own read-only answer, serving the record these runs write.

    Since 23 September 2026 a planning write that names what the project
    declares also says whose work it is and where those declarations were
    said, and the helper checks that pair against the record rather than
    reading at whatever HEAD its own copy has. So a drive against a REAL
    sidecar needs something to answer that question — and the honest something
    is the record these very runs are written to, read through the service
    that serves it. It is a child of this process on 127.0.0.1 on a port the
    kernel picks, reading a throwaway file under this test's own temporary
    directory; nothing live is anywhere near it.
    """
    from forge.record_answer.service import ANSWER_ROUTE, serve

    server, _thread = serve(ledger=tmp_path / "sandbox.db", host="127.0.0.1", port=0)
    host, port = server.server_address[:2]
    monkeypatch.setenv(COORDINATOR_OWNER_ENV, f"http://{host}:{port}{ANSWER_ROUTE}")
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


def _sidecar_config(repo: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: str(repo)}},
        }
    )


@pytest.fixture
def fake_guardkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in guardkit the sidecar resolves the way it resolves the real
    one, and a log of every call it made."""
    monkeypatch.setenv(GUARDKIT_PATH_ENV, str(write_fake_guardkit(tmp_path / "bin")))
    log = tmp_path / "guardkit-calls.jsonl"
    monkeypatch.setenv("FAKE_GUARDKIT_LOG", str(log))
    for name in (
        "FAKE_GUARDKIT_NORMALIZE",
        "FAKE_GUARDKIT_VALIDATE",
        "FAKE_GUARDKIT_CLASSIFY",
        "FAKE_GUARDKIT_QA_VALIDATE",
    ):
        monkeypatch.delenv(name, raising=False)
    return log


@pytest.fixture
def fake_normalizer_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The stand-in gherkin normalizer, planted where BOTH the sidecar's own
    ``find_spec`` probe and the subprocess it starts resolve it — the spec
    leg's declared check runs a real module in a real subprocess."""
    yield from fake_normalizer(tmp_path / "normalizer", monkeypatch)


def _sandbox(repo: Path, tmp_path: Path):
    """A real sidecar for ``repo`` on a loopback port, and the runner that
    talks to it. Returns ``(runner, shutdown)``."""
    server = build_server(
        port=0,
        config_loader=lambda: _sidecar_config(repo),
        worktrees_root=tmp_path / "sandbox-worktrees",
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]

    def shutdown() -> None:
        server.shutdown()
        server.server_close()

    return SidecarGitRunner(f"http://{host}:{port}", repo=REPO_KEY), shutdown


@pytest.fixture
def sandbox_repo(
    tmp_path: Path, request: pytest.FixtureRequest, fake_normalizer_module: Path
):
    """An enforced-law scratch repository, the in-container runner the legs
    that are not moved yet still use, and the sandbox's git runner."""
    law = getattr(request, "param", "enforced")
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _commit_repo_routing_law(repo, law)
    in_container = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    runner, shutdown = _sandbox(repo, tmp_path)
    try:
        yield repo, in_container, runner
    finally:
        shutdown()


def _plan_yaml_on_branch(repo: Path, branch: str) -> list[str]:
    """The plan YAML files that are on ``branch`` — empty when the plan commit
    never landed (a blocking check refused it)."""
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return []
    return [
        line
        for line in listed.stdout.splitlines()
        if line.startswith(".guardkit/features/")
    ]


def _driver(store: SqlitePlanningRunStore, sandbox_repo, **kwargs: Any):
    repo, in_container, sandbox_runner = sandbox_repo
    return _make_driver(
        store,
        git_runner=in_container,
        git_runner_for_repo=lambda _repo: sandbox_runner,
        repo_path=str(repo),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The plan leg, end to end, with the checks declared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_plan_legs_checks_run_in_the_sandbox_and_the_stamps_ride_the_commit(
    store: SqlitePlanningRunStore, sandbox_repo, fake_guardkit: Path, caplog
) -> None:
    """A clean run: the plan leg declares the two checks instead of running
    them, the sidecar runs them in ITS worktree in order, the stamps the
    normalizer wrote ride the plan commit, and the receipt says the first
    stamping ran by rule only — the same words as when the checks run here."""
    repo, _, _ = sandbox_repo
    _queue(store)
    h = _driver(store, sandbox_repo, plan_result_factory=_plan_result_native)
    with caplog.at_level(logging.INFO, logger="forge.planning.driver"):
        await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value

    # (1) The checks ran in the sandbox's worktree, in order, with the
    #     normalizer first and the model switched off on the first stamping.
    normalizes = normalize_calls(fake_guardkit)
    assert len(normalizes) == 1 and "--no-model" in normalizes[0]
    assert len(validate_calls(fake_guardkit)) == 1

    # (2) The driver said out loud that it declared them rather than ran them.
    declared = [
        r.getMessage()
        for r in caplog.records
        if "the plan leg's pre-commit checks" in r.getMessage()
    ]
    assert declared and "normalize-stamps(blocking, --no-model)" in declared[0]
    assert "feature-validate(blocking)" in declared[0]

    # (3) The stamps the sandbox wrote are on the planning branch.
    feature_id = _leg_details(store, "feature-plan")["feature_id"]
    committed = git_show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)) or ""
    assert 'verifier: "hurl"' in committed

    # (4) The receipt is the one the closure writes.
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert receipt["status"] == "written"
    assert receipt["rules_only"] is True
    assert receipt["stamped"] == {"ok": "hurl"}
    assert receipt["enforcement"] == "enforced"
    # The sidecar normalizer changed the tree after the first approval. Forge
    # sent the exact committed tree through the existing Coach-only review once
    # and stored that decision, bound to those bytes. Nothing rewrites the tree
    # after that review.
    # There are exactly two real mutations: Forge first fills feature_files,
    # then the sidecar appends stamps. Each changed tree is reviewed once.
    assert h.ctx["counters"]["semantic_rereview"] == 2
    first_reviewed, final_reviewed = h.ctx["counters"]["plan_revisions"]
    assert "feature_files:" in first_reviewed[_plan_yaml_rel(feature_id)]
    assert "scenarios:" not in first_reviewed[_plan_yaml_rel(feature_id)]
    assert final_reviewed[_plan_yaml_rel(feature_id)] == committed
    semantic = _leg_details(store, "feature-plan")["semantic_review"]
    assert semantic["reviewed_after_rewrite"] is True
    assert semantic["artifact_identity"] == PlanningRunDriver._plan_artifact_identity(
        final_reviewed
    )
    assert _error_cards(h) == []


@pytest.mark.asyncio
async def test_sidecar_normalizer_review_refusal_fails_after_commit_before_build(
    store: SqlitePlanningRunStore, sandbox_repo, fake_guardkit: Path
) -> None:
    """A mutating declared check cannot inherit the pre-normalization receipt.

    The sidecar has already made the immutable plan commit when Forge can ask
    the async specialist to judge its exact bytes. A refused review therefore
    leaves that audit commit in place but stops before the build trigger.
    """
    repo, _, _ = sandbox_repo
    _queue(store)

    async def reviewer(**kwargs: Any) -> Any:
        revision = kwargs.get("revision_of")
        if revision is None:
            return _plan_result_native(kwargs["feature_id"])
        plan_yaml = revision[_plan_yaml_rel(kwargs["feature_id"])]
        if "scenarios:" not in plan_yaml:
            return _plan_result(
                kwargs["feature_id"],
                dict(revision),
                reviewed_after_rewrite=True,
            )
        return SimpleNamespace(
            outcome=SimpleNamespace(value="error"),
            role_output={},
            reason="Coach refused normalized tree",
        )

    h = _driver(store, sandbox_repo, plan_dispatch=reviewer)

    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value
    assert h.ctx["counters"]["build_trigger"] == 0
    assert len(normalize_calls(fake_guardkit)) == 1
    assert _plan_yaml_on_branch(repo, f"planning/{CID}")
    assert any(
        "committed plan semantic re-review failed" in message
        and "Coach refused normalized tree" in message
        for _, message, _ in h.ctx["notifications"]
    )


@pytest.mark.asyncio
async def test_a_refusal_in_the_sandbox_still_fires_the_machines_rewrite_round(
    store: SqlitePlanningRunStore, sandbox_repo, fake_guardkit: Path, monkeypatch
) -> None:
    """Rules 1a and 2 through the wire: the first stamping runs by rule only
    and the sandbox's guardkit refuses two worked examples, so the machine
    sends its own note to the spec writer, the plan is written again, and the
    second stamping — this time with the model allowed — stamps clean. The
    run carries on to the build queue with one plain line and no card."""
    _queue(store)
    # The stand-in refuses a ``--no-model`` call and writes a plain one: the
    # shape of a run whose rules refuse and whose model fallback then decides.
    monkeypatch.setenv("FAKE_GUARDKIT_NO_MODEL_REFUSES", "1")
    h = _driver(
        store,
        sandbox_repo,
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value

    # Two stampings in the sandbox: by rule only, then with the model allowed.
    normalizes = normalize_calls(fake_guardkit)
    assert len(normalizes) == 2
    assert "--no-model" in normalizes[0] and "--no-model" not in normalizes[1]

    # The machine's note named the refused titles verbatim and was its own.
    rewritten = _approved_spec_rows(store)[-1]["rewritten_by_machine"]
    assert rewritten["round"] == 1
    assert rewritten["refused_titles"] == list(REFUSED_TITLES)
    assert rewritten["author"] == "planning-driver (stamp normalizer refusal)"
    for title in REFUSED_TITLES:
        assert title in rewritten["note"]

    # Rich sees one plain line, never a card.
    assert _error_cards(h) == []
    assert any(
        "could not be proven as written, so the machine asked the spec writer"
        in message
        for _, message, _ in h.ctx["notifications"]
    )
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert receipt["status"] == "written"
    assert receipt["rewrite"]["first_stamping"]["status"] == "refused"


@pytest.mark.asyncio
async def test_a_second_refusal_in_the_sandbox_stops_the_run_with_the_card(
    store: SqlitePlanningRunStore, sandbox_repo, fake_guardkit: Path, monkeypatch
) -> None:
    """Every stamping refuses: the round fires once and the run stops with the
    plan-stop card, exactly as it does when the checks run here — and the
    plan was never committed."""
    repo, _, _ = sandbox_repo
    _queue(store)
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "refused")
    h = _driver(
        store,
        sandbox_repo,
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
    )
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value

    assert len(normalize_calls(fake_guardkit)) == 2
    # The commit never landed: a blocking check refused it, so the branch has
    # no plan YAML on it.
    assert _plan_yaml_on_branch(repo, f"planning/{CID}") == []
    cards = _error_cards(h)
    assert cards and any("The machine already asked the spec writer once" in c for c in cards)
    for title in REFUSED_TITLES:
        assert any(title in c for c in cards)


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_repo", ["off"], indirect=True)
async def test_where_the_law_is_not_enforced_the_refusal_never_blocks_the_commit(
    store: SqlitePlanningRunStore, sandbox_repo, fake_guardkit: Path, monkeypatch
) -> None:
    """The stop is gated on the routing law here as it is there: with the law
    off, the normalizer is declared as a check that does not block, the
    refusal rides the receipts, and the plan commit lands anyway."""
    repo, _, _ = sandbox_repo
    _queue(store)
    monkeypatch.setenv("FAKE_GUARDKIT_NORMALIZE", "refused")
    h = _driver(store, sandbox_repo, plan_result_factory=_plan_result_native)
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # One stamping only: the rewrite round needs the law enforced.
    normalizes = normalize_calls(fake_guardkit)
    assert len(normalizes) == 1 and "--no-model" not in normalizes[0]
    feature_id = _leg_details(store, "feature-plan")["feature_id"]
    assert git_show(repo, f"planning/{CID}", _plan_yaml_rel(feature_id)) is not None
    receipt = _leg_details(store, "feature-plan")["stamp_normalizer"]
    assert receipt["status"] == "refused"
    assert receipt["enforcement"] == "off"
    assert _error_cards(h) == []


@pytest.mark.asyncio
async def test_a_red_feature_validate_in_the_sandbox_fails_the_leg_with_its_reason(
    store: SqlitePlanningRunStore, sandbox_repo, fake_guardkit: Path, monkeypatch
) -> None:
    """The second declared check blocks the same way: a red ``feature
    validate`` in the sandbox refuses the commit and the leg fails with what
    guardkit said, not with a transport error."""
    repo, _, _ = sandbox_repo
    _queue(store)
    monkeypatch.setenv("FAKE_GUARDKIT_VALIDATE", "red")
    h = _driver(store, sandbox_repo, plan_result_factory=_plan_result_native)
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value

    assert len(validate_calls(fake_guardkit)) == 1
    assert _plan_yaml_on_branch(repo, f"planning/{CID}") == []
    # The leg failed with what guardkit said, not with a transport error.
    failure = json.dumps(
        [
            json.loads(e["details_json"] or "{}")
            for e in store.list_events(CID)
            if e["stage_label"] == "feature-plan"
        ]
    )
    assert "task file TASK-STAT-001.md missing" in failure
    assert "could not be reached" not in failure


# ---------------------------------------------------------------------------
# The fence, pinned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_caller_that_hands_over_a_closure_is_refused_out_loud(
    sandbox_repo, fake_guardkit: Path
) -> None:
    """A Python closure handed to a sandbox runner is refused in one plain
    sentence and nothing is sent — it is never quietly run on the host
    instead. Every planning leg now declares its checks, so nothing in the
    chain reaches this refusal; it stays as the fence that says so.
    """
    repo, _, sandbox_runner = sandbox_repo

    async def closure(worktree: Path) -> Any:
        raise AssertionError("the closure must never be run")

    result = await sandbox_runner.prepare_branch_and_write_tree(
        str(repo), "planning/x", {"a.md": "x"}, "planning: x", pre_commit=closure
    )
    assert result.status == "failed"
    assert result.detail == CLOSURE_REFUSED_SENTENCE
    assert git_show(repo, "planning/x", "a.md") is None


# ---------------------------------------------------------------------------
# The routing law itself, read across the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_law_is_read_from_the_repository_even_before_the_branch_exists(
    sandbox_repo, fake_guardkit: Path
) -> None:
    """What says whether a refusal stops the run is the repository's own
    routing-law file. When the plan is the first thing written to a planning
    branch, that branch does not exist yet, so the law is read from the
    repository's current tip instead — never quietly read as "off".
    """
    from forge.planning.driver import PlanningRunDriver

    repo, in_container, sandbox_runner = sandbox_repo
    driver = PlanningRunDriver.__new__(PlanningRunDriver)
    declared = await PlanningRunDriver._declare_plan_checks(
        driver,
        sandbox_runner,
        {".guardkit/features/FEAT-C5B0.yaml": "id: FEAT-C5B0\ntasks:\n- id: TASK-STAT-001\n"},
        "FEAT-C5B0",
        ["features/stats-endpoint/stats-endpoint.feature"],
        rules_only=True,
        repo_path=str(repo),
        branch="planning/never-made",
    )
    assert declared.prelude.enforcement["enforcement"] == "enforced"
    normalize = declared.checks.checks[0]
    assert normalize.name == "normalize-stamps"
    assert normalize.blocking is True
    assert normalize.args == {"feature_id": "FEAT-C5B0", "no_model": True}
    assert [c.name for c in declared.checks.checks] == [
        "normalize-stamps",
        "feature-validate",
    ]
    # forge wrote the committed spec's .feature path into the plan YAML it is
    # about to commit, exactly as it does in a worktree of its own.
    filled = declared.files[".guardkit/features/FEAT-C5B0.yaml"]
    assert "features/stats-endpoint/stats-endpoint.feature" in filled


@pytest.mark.asyncio
async def test_a_law_that_cannot_be_read_at_all_says_so_and_does_not_block(
    tmp_path: Path, fake_guardkit: Path, caplog
) -> None:
    """A repository with no routing-law file: the law is off, the normalizer
    is declared as a check that does not block, and the machine log says the
    file could not be read rather than leaving a silent "off"."""
    from forge.planning.driver import PlanningRunDriver

    repo = tmp_path / "lawless"
    _init_scratch_repo(repo)
    # This test is about a settings file that cannot be read AT ALL, so this
    # copy has none. Every other scratch copy carries one since 2026-09-21,
    # because a project that declares no memory is refused at the door — but
    # nothing here goes through the door: it calls the plan leg's check
    # declaration directly.
    subprocess.run(
        ["git", "rm", "-q", "-r", ".guardkit"], cwd=repo, check=True, env=_git_env()
    )
    subprocess.run(
        ["git", "commit", "-qm", "no settings file at all"],
        cwd=repo,
        check=True,
        env=_git_env(),
    )
    runner, shutdown = _sandbox(repo, tmp_path)
    try:
        driver = PlanningRunDriver.__new__(PlanningRunDriver)
        with caplog.at_level(logging.WARNING, logger="forge.planning.driver"):
            declared = await PlanningRunDriver._declare_plan_checks(
                driver,
                runner,
                {".guardkit/features/FEAT-C5B0.yaml": "id: FEAT-C5B0\n"},
                "FEAT-C5B0",
                [],
                rules_only=True,
                repo_path=str(repo),
                branch="planning/never-made",
            )
    finally:
        shutdown()
    assert declared.prelude.enforcement["enforcement"] == "off"
    assert declared.checks.checks[0].blocking is False
    assert any(
        "could not be read" in r.getMessage() and "routing law reads as off" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# The other three legs' checks, declared too (rule 87, 2026-09-07)
# ---------------------------------------------------------------------------


def _on_branch(repo: Path, branch: str, prefix: str) -> list[str]:
    """The files under ``prefix`` that are on ``branch`` — empty when the
    commit never landed (a blocking check refused it)."""
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return []
    return [line for line in listed.stdout.splitlines() if line.startswith(prefix)]


def _failure_text(store: SqlitePlanningRunStore, stage: str) -> str:
    return json.dumps(
        [
            json.loads(e["details_json"] or "{}")
            for e in store.list_events(CID)
            if e["stage_label"] == stage
        ]
    )


def _draft_provability(store: SqlitePlanningRunStore) -> list[dict[str, Any]]:
    """The ``provability`` receipt on every spec-draft row, in order."""
    out: list[dict[str, Any]] = []
    for event in store.list_events(CID):
        if event["stage_label"] != "feature-spec-draft":
            continue
        draft = (json.loads(event["details_json"] or "{}") or {}).get("spec_draft") or {}
        if "provability" in draft:
            out.append(draft["provability"])
    return out


@pytest.mark.asyncio
async def test_the_spec_legs_normalizer_runs_in_the_sandbox_and_its_rewrite_rides_the_commit(
    store: SqlitePlanningRunStore,
    sandbox_repo,
    fake_guardkit: Path,
    fake_normalizer_module: Path,
    caplog,
) -> None:
    """The spec leg declares its gherkin normalizer instead of running it: the
    sidecar runs the module in ITS worktree, what the normalizer rewrites
    there rides the spec commit, and the collaborator inside the forge
    container is never called."""
    repo, _, _ = sandbox_repo
    _queue(store)
    h = _driver(store, sandbox_repo, plan_result_factory=_plan_result_native)
    with caplog.at_level(logging.INFO, logger="forge.planning.driver"):
        await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["normalize"] == 0  # never run in the container

    calls = normalizer_calls(fake_guardkit)
    assert len(calls) == 1
    assert calls[0][0].endswith("/" + FEATURE_REL)
    assert not calls[0][0].startswith(str(repo))  # the sandbox's worktree, not the checkout

    committed = git_show(repo, f"planning/{CID}", FEATURE_REL) or ""
    assert committed.endswith(NORMALIZED_MARKER)

    declared = [
        r.getMessage()
        for r in caplog.records
        if "the spec leg's pre-commit checks" in r.getMessage()
    ]
    assert declared and "normalize-feature(blocking)" in declared[0]


@pytest.mark.asyncio
async def test_a_red_normalizer_in_the_sandbox_fails_the_spec_leg_with_the_same_words(
    store: SqlitePlanningRunStore,
    sandbox_repo,
    fake_guardkit: Path,
    fake_normalizer_module: Path,
    monkeypatch,
) -> None:
    """A spec the normalizer cannot parse fails the leg with the sentence the
    closure gives it, and nothing reaches the branch."""
    repo, _, _ = sandbox_repo
    _queue(store)
    monkeypatch.setenv("FAKE_NORMALIZER", "red")
    h = _driver(store, sandbox_repo, plan_result_factory=_plan_result_native)
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value

    assert _on_branch(repo, f"planning/{CID}", "features/") == []
    failure = _failure_text(store, "feature-spec")
    assert "spec write / normalizer failed" in failure
    assert f"normalizer exit 1 for {FEATURE_REL}" in failure
    assert "could not be reached" not in failure


@pytest.mark.asyncio
async def test_the_pass_bar_leg_declares_one_check_per_bar_in_the_sandbox(
    store: SqlitePlanningRunStore,
    sandbox_repo,
    fake_guardkit: Path,
    fake_normalizer_module: Path,
) -> None:
    """guardkit's own ``qa validate pass-bar`` runs in the sandbox, once per
    minted bar, and the bars land."""
    repo, _, _ = sandbox_repo
    _queue(store)
    h = _driver(store, sandbox_repo, plan_result_factory=_plan_result_native)
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["pass_bar_validate"] == 0
    calls = qa_validate_calls(fake_guardkit, "pass-bar")
    assert calls == [["qa", "validate", "pass-bar", "qa/pass-bar-TASK-STAT-001.yaml"]]
    assert _on_branch(repo, f"planning/{CID}", "qa/pass-bar-") == [
        "qa/pass-bar-TASK-STAT-001.yaml"
    ]


@pytest.mark.asyncio
async def test_a_malformed_bar_in_the_sandbox_fails_the_leg_and_no_bar_lands(
    store: SqlitePlanningRunStore,
    sandbox_repo,
    fake_guardkit: Path,
    fake_normalizer_module: Path,
    monkeypatch,
) -> None:
    repo, _, _ = sandbox_repo
    _queue(store)
    monkeypatch.setenv("FAKE_GUARDKIT_QA_VALIDATE", "red-pass-bar")
    h = _driver(store, sandbox_repo, plan_result_factory=_plan_result_native)
    assert await _drive_to_failure(h, store) == PlanningState.FAILED.value

    assert _on_branch(repo, f"planning/{CID}", "qa/pass-bar-") == []
    failure = _failure_text(store, "qa-pass-bars")
    assert "pass-bar write / qa validate failed" in failure
    assert "qa/pass-bar-TASK-STAT-001.yaml: guardkit qa validate pass-bar" in failure


@pytest.mark.asyncio
async def test_the_feature_gate_legs_check_runs_in_the_sandbox(
    store: SqlitePlanningRunStore,
    tmp_path: Path,
    fake_guardkit: Path,
    fake_normalizer_module: Path,
) -> None:
    """The last writing leg: the appended registry is validated by guardkit
    inside the sandbox before the gate lands."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    _commit_repo_routing_law(repo, "enforced")
    in_container = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    runner, shutdown = _sandbox(repo, tmp_path)
    try:
        _queue(store)
        h = _make_driver(
            store,
            git_runner=in_container,
            git_runner_for_repo=lambda _repo: runner,
            repo_path=str(repo),
            spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
            plan_result_factory=_plan_result_native_versions,
        )
        await h.driver.drive(CID)
    finally:
        shutdown()

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["gate_registry_validate"] == 0
    assert qa_validate_calls(fake_guardkit, "gate-registry") == [
        ["qa", "validate", "gate-registry", "qa/gates/registry.yaml"]
    ]
    branch = f"planning/{CID}"
    assert git_show(repo, branch, "qa/gates/version_endpoint_gate.py") is not None
    registry = git_show(repo, branch, "qa/gates/registry.yaml") or ""
    assert "version-endpoint" in registry


# ---------------------------------------------------------------------------
# Part K's provability check, answered by the sandbox with the spec commit
# ---------------------------------------------------------------------------


def _never_classify_here():
    async def _classify(repo_path: Path, feature_text: str):
        raise AssertionError(
            "the provability check must run in the repository's sandbox, "
            "never in the forge container"
        )

    return _classify


@pytest.mark.asyncio
async def test_the_provability_check_answers_from_the_sandbox_with_the_spec_commit(
    store: SqlitePlanningRunStore,
    sandbox_repo,
    fake_guardkit: Path,
    fake_normalizer_module: Path,
) -> None:
    """Rule 87's last item: for a sandboxed repository Part K's check rides
    the spec commit as a declared, non-blocking check, so it runs on the
    bytes that landed — and the check in the forge container is never run."""
    _queue(store)
    h = _driver(
        store,
        sandbox_repo,
        plan_result_factory=_plan_result_native,
        classify_fn=_never_classify_here(),
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert len(classify_calls(fake_guardkit)) == 1
    receipts = _draft_provability(store)
    assert len(receipts) == 1
    assert receipts[0]["checked_by_rule"] is True
    assert receipts[0]["refused_titles"] == []
    assert receipts[0]["rewritten"] is False
    assert _error_cards(h) == []


@pytest.mark.asyncio
async def test_a_refused_example_in_the_sandbox_fires_the_machines_round_before_the_card(
    store: SqlitePlanningRunStore,
    sandbox_repo,
    fake_guardkit: Path,
    fake_normalizer_module: Path,
    monkeypatch,
) -> None:
    """The sandbox's own guardkit refuses two examples by rule, so the
    machine's note round runs before the card exactly as it does when the
    check runs in the container: two spec-writer calls, two checks, and the
    card carries rule 45's words."""
    _queue(store)
    monkeypatch.setenv("FAKE_GUARDKIT_CLASSIFY", "refused")
    h = _driver(
        store,
        sandbox_repo,
        spec_result_factory=_spec_by_round(_spec_result_native(), _rewritten_spec_result()),
        plan_result_factory=_plan_result_native,
        classify_fn=_never_classify_here(),
    )
    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["spec"] == 2
    assert len(classify_calls(fake_guardkit)) == 2
    receipt = _draft_provability(store)[-1]
    assert receipt["round"] == 1
    assert receipt["rewritten"] is True
    assert receipt["refused_titles"] == list(REFUSED_TITLES)
    for title in REFUSED_TITLES:
        assert title in receipt["note"]
    assert (
        "The machine rewrote 2 of the worked examples so they can be proven"
        in _digest_card_text(h)
    )


def _digest_card_text(h) -> str:
    """The what-happened text on the one spec-digest card Rich reads."""
    cards = [
        env
        for env in h.ctx["publisher"].envelopes
        if env.payload["details"].get("checkpoint_type") == "product_docs_spec_digest"
    ]
    assert len(cards) == 1, cards
    return str(cards[0].payload["details"]["summary"].get("what_happened") or "")


# ---------------------------------------------------------------------------
# The whole run, on the composition's own routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_whole_run_for_a_sandbox_repository_reaches_the_build_queue(
    store: SqlitePlanningRunStore,
    tmp_path: Path,
    fake_guardkit: Path,
    fake_normalizer_module: Path,
) -> None:
    """The production shape end to end: the driver holds the ONE runner the
    composition builds from ``planning.sandboxes``, so every read and every
    write of the planning chain goes to the sidecar inside the sandbox. The
    run reaches the build queue, no oracle ran in the forge container, and
    every artefact the four legs write is on the branch.
    """
    from forge.planning.sidecar_git_runner import RepoRoutedGitRunner

    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _seed_gate_surface(repo)
    _commit_repo_routing_law(repo, "enforced")
    sandbox_runner, shutdown = _sandbox(repo, tmp_path)
    routed = RepoRoutedGitRunner(
        runners_by_repo={REPO_KEY: sandbox_runner},
        repo_paths={REPO_KEY: str(repo)},
        default=WorktreeGitRunner(worktrees_root=tmp_path / "must-not-be-used"),
    )
    try:
        _queue(store)
        h = _make_driver(
            store,
            git_runner=routed,
            git_runner_for_repo=routed.runner_for,
            repo_path=str(repo),
            spec_result=_spec_result_with_seed(_ROUND19_SEED_AUTHLESS),
            plan_result_factory=_plan_result_native_versions,
        )
        await h.driver.drive(CID)
    finally:
        shutdown()

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value

    # Not one oracle ran in the forge container.
    counters = h.ctx["counters"]
    assert counters["normalize"] == 0
    assert counters["validate"] == 0
    assert counters["pass_bar_validate"] == 0
    assert counters["gate_registry_validate"] == 0

    # Every one of them ran in the sandbox instead.
    assert len(normalizer_calls(fake_guardkit)) == 1
    assert len(normalize_calls(fake_guardkit)) == 1
    assert len(validate_calls(fake_guardkit)) == 1
    assert qa_validate_calls(fake_guardkit, "pass-bar") == [
        ["qa", "validate", "pass-bar", f"qa/pass-bar-TASK-VER-00{n}.yaml"]
        for n in (1, 2, 3)
    ]
    assert len(qa_validate_calls(fake_guardkit, "gate-registry")) == 1

    # And the four legs' artefacts are all on the planning branch.
    branch = f"planning/{CID}"
    feature_id = _leg_details(store, "feature-plan")["feature_id"]
    assert git_show(repo, branch, "features/version-endpoint/version-endpoint.feature")
    assert git_show(repo, branch, _plan_yaml_rel(feature_id))
    assert git_show(repo, branch, "qa/pass-bar-TASK-VER-001.yaml")
    assert git_show(repo, branch, "qa/gates/version_endpoint_gate.py")
    assert _error_cards(h) == []
