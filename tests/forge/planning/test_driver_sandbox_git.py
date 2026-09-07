"""The plan leg's checks run where the repository lives (sandbox first, 2026-09-07).

Rich's rule: nothing the factory runs on a repository runs on the host. When
the repository has a sandbox of its own, the plan leg no longer runs the
stamp normalizer and ``feature validate`` itself in a worktree beside the
driver: it DECLARES the two checks by name, the deploy sidecar inside the
sandbox runs them with the guardkit beside it, and their outcomes come back
with the commit. What a refusal means, what the receipts say, which stamping
ran by rule only and whether the machine's rewrite round fires must be
exactly what they are when the checks run here.

These tests drive the whole planning run against a REAL sidecar on an
ephemeral loopback port, against a real git repository, with a stand-in
``guardkit`` binary running the declared checks — so the argv, the exit
codes, the JSON and the commit are the real ones.

Phase 1's boundary, said out loud: only the PLAN leg's checks are declared.
The spec leg, the pass-bar leg and the feature-gate leg still hand their
oracle to the git runner as a Python closure, so those legs keep the git
runner inside the forge container; a sandbox runner refuses a closure in one
plain sentence rather than quietly running the repository's checks on the
host. The last test here pins that refusal.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import GUARDKIT_PATH_ENV, build_server
from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.sidecar_git_runner import CLOSURE_REFUSED_SENTENCE, SidecarGitRunner
from forge.planning.states import PlanningState

from tests.forge.deploy_sidecar._fake_guardkit import (
    REFUSED_TITLES,
    git_show,
    normalize_calls,
    validate_calls,
    write_fake_guardkit,
)
from tests.forge.planning.test_driver_target_terminal import (
    CID,
    _approved_spec_rows,
    _commit_repo_routing_law,
    _drive_to_failure,
    _error_cards,
    _init_scratch_repo,
    _leg_details,
    _make_driver,
    _plan_result_native,
    _plan_yaml_rel,
    _queue,
    _rewritten_spec_result,
    _spec_by_round,
    _spec_result_native,
)

REPO_KEY = "guardkit/api_test"


@pytest.fixture
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    cx = sqlite_connect.connect_writer(tmp_path / "sandbox.db")
    migrations.apply_at_boot(cx)
    return SqlitePlanningRunStore(cx, target_terminal_enabled=True)


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
    for name in ("FAKE_GUARDKIT_NORMALIZE", "FAKE_GUARDKIT_VALIDATE", "FAKE_GUARDKIT_CLASSIFY"):
        monkeypatch.delenv(name, raising=False)
    return log


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
def sandbox_repo(tmp_path: Path, request: pytest.FixtureRequest):
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
        if "are declared to the sandbox git runner" in r.getMessage()
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
    assert _error_cards(h) == []


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
# Phase 1's boundary, pinned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_leg_that_still_hands_over_a_closure_is_refused_out_loud(
    sandbox_repo, fake_guardkit: Path
) -> None:
    """The legs phase 1 has not moved (the spec leg, the pass bars, the
    feature gate) still hand their oracle over as a Python closure. A sandbox
    runner refuses it in one plain sentence and sends nothing — it never
    quietly runs the repository's checks on the host instead. Until those
    legs declare their checks too, they keep the git runner in the forge
    container, which is what the composition gives them.
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
