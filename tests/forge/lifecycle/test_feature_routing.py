from __future__ import annotations

import asyncio
import json
import sqlite3
from email.message import Message

import pytest

from forge.adapters.sqlite.connect import connect_writer
from forge.lifecycle.feature_routing import (
    FeatureRoutingBlocked,
    FeatureRoutingSeedStore,
    FeatureRoutingTransactionError,
    router_restart_blockers,
)
from forge.lifecycle.migrations import apply_at_boot
from forge.pipeline.feature_routing import FeatureRoutingGate


def _store(tmp_path):
    path = tmp_path / "forge.db"
    connection = connect_writer(path)
    apply_at_boot(connection)
    return path, connection, FeatureRoutingSeedStore(connection)


def _headers(stored="1", actual="1"):
    headers = Message()
    headers["x-feature-stored-server-id"] = stored
    headers["x-feature-actual-server-id"] = actual
    return headers


def test_started_and_success_are_committed_before_authority(tmp_path):
    path, connection, store = _store(tmp_path)
    observations = []

    async def request(_url, key, _timeout, _preference):
        with sqlite3.connect(path) as reader:
            observations.append(
                reader.execute(
                    "SELECT state FROM feature_routing_seeds WHERE feature_routing_id=?",
                    (key,),
                ).fetchone()[0]
            )
        return 200, _headers()

    gate = FeatureRoutingGate(store, "http://router.invalid", request=request)
    receipt = asyncio.run(
        gate.ensure_seeded("admission_A", origin_kind="planning", origin_id="A")
    )
    assert observations == ["STARTED"]
    with sqlite3.connect(path) as reader:
        assert reader.execute(
            "SELECT state, server_id FROM feature_routing_seeds"
        ).fetchone() == ("SUCCEEDED", 1)
    assert receipt.to_wire() == {
        "feature_routing_id": "admission_A",
        "attempt_id": receipt.attempt_id,
        "server_id": 1,
    }
    connection.close()


def test_restart_poison_and_late_success_never_reseed(tmp_path):
    _path, connection, store = _store(tmp_path)
    claim = store.claim_initial_seed(
        "late_A", "old-token", origin_kind="planning", origin_id="A"
    )
    assert claim.owned
    assert store.poison_unfinished_seeds_on_boot() == 1
    assert store.poison_unfinished_seeds_on_boot() == 0
    with pytest.raises(FeatureRoutingBlocked):
        store.complete_seed("late_A", "old-token", 1)

    requests = []
    async def never_called(*_args):
        requests.append(True)
        return 200, _headers()

    gate = FeatureRoutingGate(
        store,
        "http://router.invalid",
        request=never_called,
    )
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("late_A", origin_kind="build", origin_id="B"))
    assert requests == []
    connection.close()


def test_ambiguous_response_is_terminal_and_concurrent_success_seeds_once(tmp_path):
    _path, connection, store = _store(tmp_path)
    ambiguous_calls = []

    async def ambiguous(*_args):
        ambiguous_calls.append(True)
        raise TimeoutError("partial headers")

    gate = FeatureRoutingGate(store, "http://router.invalid", request=ambiguous)
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("partial_A", origin_kind="planning", origin_id="A"))
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("partial_A", origin_kind="planning", origin_id="A"))
    assert len(ambiguous_calls) == 1
    assert store.state_for("partial_A") == "UNKNOWN"

    calls = []

    async def success(*_args):
        calls.append(True)
        return 200, _headers("2", "2")

    successful = FeatureRoutingGate(store, "http://router.invalid", request=success)

    async def both():
        return await asyncio.gather(
            successful.ensure_seeded("same_A", origin_kind="planning", origin_id="A"),
            successful.ensure_seeded("same_A", origin_kind="planning", origin_id="A"),
        )

    first, second = asyncio.run(both())
    assert first == second
    assert len(calls) == 1
    connection.close()


def test_duplicate_or_unrecognized_ack_fails_and_outer_transaction_is_refused(tmp_path):
    _path, connection, store = _store(tmp_path)
    duplicate = _headers()
    duplicate["x-feature-actual-server-id"] = "1"
    async def duplicate_response(*_args):
        return 200, duplicate

    gate = FeatureRoutingGate(store, "http://router.invalid", request=duplicate_response)
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("dupe_A", origin_kind="planning", origin_id="A"))
    assert store.state_for("dupe_A") == "FAILED"

    connection.execute("BEGIN")
    with pytest.raises(FeatureRoutingTransactionError):
        store.claim_initial_seed(
            "nested_A", "token", origin_kind="planning", origin_id="A"
        )
    connection.execute("ROLLBACK")
    assert store.state_for("nested_A") is None
    connection.close()


def test_restart_projection_sees_build_queued_before_consumer_delivery(tmp_path):
    _path, connection, store = _store(tmp_path)
    connection.execute(
        "INSERT INTO planning_runs "
        "(correlation_id, state, request_text, originating_user, expected_approver, "
        "triggered_by, queued_at) VALUES "
        "('queued_A', 'BUILD_QUEUED', 'request', 'owner', 'owner', "
        "'forge-internal', datetime('now'))"
    )
    store.claim_initial_seed(
        "queued_A", "token", origin_kind="planning", origin_id="queued_A"
    )
    store.complete_seed("queued_A", "token", 1)
    assert router_restart_blockers(connection) == ("queued-undelivered:queued_A",)
    connection.close()


def test_restart_projection_sees_post_build_merge_and_publication_work(tmp_path):
    _path, connection, store = _store(tmp_path)
    assert store is not None
    connection.execute(
        "INSERT INTO builds "
        "(build_id, feature_id, repo, branch, feature_yaml_path, status, "
        "triggered_by, correlation_id, queued_at, completed_at, "
        "pending_approval_request_id) VALUES "
        "('build-card', 'FEAT-CARD', 'org/repo', 'feature/card', "
        "'.guardkit/features/FEAT-CARD.yaml', 'COMPLETE', 'forge-internal', "
        "'card_A', datetime('now'), datetime('now'), 'approval-1')"
    )
    connection.execute(
        "INSERT INTO publication_records "
        "(build_id, result, lease_holder, lease_expires_at) VALUES "
        "('build-recover', NULL, NULL, NULL), "
        "('build-held', 'publication pending', 'worker-1', '2000-01-01T00:00:00+00:00'), "
        "('build-history', 'publication pending', NULL, NULL)"
    )
    connection.execute(
        "INSERT INTO stage_log "
        "(build_id, stage_label, target_kind, target_identifier, status, "
        "started_at, completed_at, duration_secs, details_json) VALUES "
        "('build-card', 'merge-offer', 'local_tool', 'merge_deploy_offer', "
        "'GATED', datetime('now'), datetime('now'), 0, '{}')"
    )

    assert router_restart_blockers(connection) == (
        "merge-approval:build-card:approval-1",
        "merge-offer-pending:build-card",
        "publication-pending:build-recover",
        "publication-held:build-held:worker-1",
    )
    connection.close()


def test_restart_projection_orders_each_merge_offer_decision_and_report(tmp_path):
    _path, connection, _store_value = _store(tmp_path)
    connection.execute(
        "INSERT INTO builds "
        "(build_id, feature_id, repo, branch, feature_yaml_path, status, "
        "triggered_by, correlation_id, queued_at, completed_at) VALUES "
        "('build-repeat', 'FEAT-REPEAT', 'org/repo', 'feature/repeat', "
        "'.guardkit/features/FEAT-REPEAT.yaml', 'COMPLETE', "
        "'forge-internal', 'repeat_A', datetime('now'), datetime('now'))"
    )

    def record(
        target: str, status: str, details: dict[str, object] | None = None
    ) -> None:
        connection.execute(
            "INSERT INTO stage_log "
            "(build_id, stage_label, target_kind, target_identifier, status, "
            "started_at, completed_at, duration_secs, details_json) VALUES "
            "('build-repeat', 'merge', 'local_tool', ?, ?, datetime('now'), "
            "datetime('now'), 0, ?)",
            (target, status, json.dumps(details or {})),
        )

    record("merge_deploy_offer", "GATED")
    assert router_restart_blockers(connection) == (
        "merge-offer-pending:build-repeat",
    )

    # A decline closes this offer and schedules no work.
    record("merge_deploy_decision", "SKIPPED")
    record("merge_deploy_executor", "SKIPPED")
    assert router_restart_blockers(connection) == ()

    # A later offer is not closed by the old decision.  Its approval is active
    # until a report written after that decision; the historical report above
    # cannot settle this attempt.
    record("merge_deploy_offer", "GATED")
    assert router_restart_blockers(connection) == (
        "merge-offer-pending:build-repeat",
    )
    attempt = {
        "merge_decision": {
            "execution_attempt_version": 1,
            "execution_attempt_id": "a" * 32,
        }
    }
    record("merge_deploy_decision", "PASSED", attempt)
    assert router_restart_blockers(connection) == (
        "merge-approved-pending:build-repeat",
    )
    record("merge_deploy_executor", "GATED", attempt)
    assert router_restart_blockers(connection) == (
        "merge-approved-pending:build-repeat",
    )
    # A later terminal from another press (including the direct CLI) cannot
    # settle the consumer decision merely because it shares the build id.
    record(
        "merge_deploy_executor",
        "FAILED",
        {
            "merge_decision": {
                "execution_attempt_version": 1,
                "execution_attempt_id": "b" * 32,
            }
        },
    )
    assert router_restart_blockers(connection) == (
        "merge-approved-pending:build-repeat",
    )
    record("merge_deploy_executor", "FAILED", attempt)
    assert router_restart_blockers(connection) == ()

    # An unversioned legacy approval has no provable report attribution.  It
    # remains fail closed even if a later same-build terminal row exists.
    record("merge_deploy_offer", "GATED")
    record("merge_deploy_decision", "PASSED")
    record("merge_deploy_executor", "PASSED")
    assert router_restart_blockers(connection) == (
        "merge-approved-pending:build-repeat",
    )
    connection.close()


def test_restart_projection_blocks_isolated_malformed_merge_attribution(tmp_path):
    _path, connection, _store_value = _store(tmp_path)
    connection.execute(
        "INSERT INTO builds "
        "(build_id, feature_id, repo, branch, feature_yaml_path, status, "
        "triggered_by, correlation_id, queued_at, completed_at) VALUES "
        "('build-malformed', 'FEAT-MAL', 'org/repo', 'feature/mal', "
        "'f.yaml', 'COMPLETE', 'forge-internal', 'mal_A', datetime('now'), "
        "datetime('now'))"
    )
    for target, status, details in (
        ("merge_deploy_offer", "GATED", "{}"),
        ("merge_deploy_decision", "PASSED", "{"),
        ("merge_deploy_executor", "PASSED", "["),
    ):
        connection.execute(
            "INSERT INTO stage_log "
            "(build_id, stage_label, target_kind, target_identifier, status, "
            "started_at, completed_at, duration_secs, details_json) VALUES "
            "('build-malformed', 'merge', 'local_tool', ?, ?, "
            "datetime('now'), datetime('now'), 0, ?)",
            (target, status, details),
        )

    assert router_restart_blockers(connection) == (
        "merge-approved-pending:build-malformed",
    )
    connection.close()


@pytest.mark.parametrize(
    ("decision_version", "report_version"),
    ((True, 1), (1.0, 1), (1, True), (1, 1.0)),
)
def test_restart_projection_requires_integer_merge_attempt_versions(
    tmp_path, decision_version, report_version
):
    _path, connection, _store_value = _store(tmp_path)
    connection.execute(
        "INSERT INTO builds "
        "(build_id, feature_id, repo, branch, feature_yaml_path, status, "
        "triggered_by, correlation_id, queued_at, completed_at) VALUES "
        "('build-version-type', 'FEAT-VERSION', 'org/repo', "
        "'feature/version', 'f.yaml', 'COMPLETE', 'forge-internal', "
        "'version_A', datetime('now'), datetime('now'))"
    )

    def record(target: str, version: object) -> None:
        connection.execute(
            "INSERT INTO stage_log "
            "(build_id, stage_label, target_kind, target_identifier, status, "
            "started_at, completed_at, duration_secs, details_json) VALUES "
            "('build-version-type', 'merge', 'local_tool', ?, 'PASSED', "
            "datetime('now'), datetime('now'), 0, ?)",
            (
                target,
                json.dumps(
                    {
                        "merge_decision": {
                            "execution_attempt_version": version,
                            "execution_attempt_id": "a" * 32,
                        }
                    }
                ),
            ),
        )

    record("merge_deploy_decision", decision_version)
    record("merge_deploy_executor", report_version)
    assert router_restart_blockers(connection) == (
        "merge-approved-pending:build-version-type",
    )
    connection.close()


def _planning(connection, correlation_id, state):
    connection.execute(
        "INSERT INTO planning_runs "
        "(correlation_id, state, request_text, originating_user, expected_approver, "
        "triggered_by, queued_at) VALUES "
        "(?, ?, 'request', 'owner', 'owner', 'forge-internal', datetime('now'))",
        (correlation_id, state),
    )


def _build(connection, build_id, correlation_id, status, approval=None):
    connection.execute(
        "INSERT INTO builds "
        "(build_id, feature_id, repo, branch, feature_yaml_path, status, "
        "triggered_by, correlation_id, queued_at, pending_approval_request_id) "
        "VALUES (?, 'FEAT-PIN', 'org/repo', 'feature/pin', 'f.yaml', ?, "
        "'forge-internal', ?, datetime('now'), ?)",
        (build_id, status, correlation_id, approval),
    )


def _seeded(store, key, server_id):
    store.claim_initial_seed(key, f"token-{key}", origin_kind="planning", origin_id=key)
    store.complete_seed(key, f"token-{key}", server_id)


def test_live_pins_count_model_work_ahead_and_not_terminal_history(tmp_path):
    _path, connection, store = _store(tmp_path)
    assert store.live_pin_counts() == {1: 0, 2: 0}

    # Delivery gap: planning handed over but the build row does not exist yet.
    _planning(connection, "gap_A", "BUILD_QUEUED")
    _seeded(store, "gap_A", 1)
    # Planning-to-build overlap counts the routing key once.
    _planning(connection, "overlap_A", "RUNNING")
    _build(connection, "build-overlap", "overlap_A", "RUNNING")
    _seeded(store, "overlap_A", 2)
    # A paused build still holds its seat.
    _planning(connection, "paused_A", "BUILD_QUEUED")
    _build(connection, "build-paused", "paused_A", "PAUSED")
    _seeded(store, "paused_A", 2)
    # A build admitted without planning is keyed by its correlation.
    _build(connection, "build-direct", "direct_A", "QUEUED")
    _seeded(store, "direct_A", 1)
    assert store.live_pin_counts() == {1: 2, 2: 2}

    # Fully terminal history, including a merge card after COMPLETE, does not count.
    _planning(connection, "done_A", "BUILD_QUEUED")
    _build(connection, "build-done", "done_A", "COMPLETE", approval="approval-1")
    _seeded(store, "done_A", 1)
    _planning(connection, "failed_A", "FAILED")
    _seeded(store, "failed_A", 2)
    # Live work without a successful seed is not a pin.
    _planning(connection, "unknown_A", "RUNNING")
    store.claim_initial_seed(
        "unknown_A", "token-unknown", origin_kind="planning", origin_id="unknown_A"
    )
    store.fail_seed("unknown_A", "token-unknown", state="UNKNOWN", reason="test")
    assert store.live_pin_counts() == {1: 2, 2: 2}

    connection.execute("UPDATE builds SET status='COMPLETE' WHERE build_id='build-paused'")
    connection.execute("UPDATE planning_runs SET state='CANCELLED' WHERE correlation_id='overlap_A'")
    connection.execute("UPDATE builds SET status='CANCELLED' WHERE build_id='build-overlap'")
    assert store.live_pin_counts() == {1: 2, 2: 0}
    connection.close()


def test_live_pins_count_a_build_with_empty_correlation_under_its_build_id(tmp_path):
    _path, connection, store = _store(tmp_path)
    _build(connection, "build-bare", "", "RUNNING")
    _seeded(store, "build-bare", 2)
    assert store.live_pin_counts() == {1: 0, 2: 1}
    connection.close()


def _echo_gate(store, seen, *, delay=None, ack=None):
    async def request(_url, key, _timeout, preference):
        seen.append((key, preference))
        if delay is not None:
            await delay(key)
        server = ack if ack is not None else preference
        return 200, _headers(str(server), str(server))

    return FeatureRoutingGate(store, "http://router.invalid", request=request)


def test_preference_is_the_seat_with_fewer_live_pins(tmp_path):
    _path, connection, store = _store(tmp_path)
    seen = []
    gate = _echo_gate(store, seen)

    async def admit(key):
        _planning(connection, key, "RUNNING")
        return await gate.ensure_seeded(key, origin_kind="planning", origin_id=key)

    first = asyncio.run(admit("pref_A"))
    second = asyncio.run(admit("pref_B"))
    assert seen == [("pref_A", 1), ("pref_B", 2)]
    assert (first.server_id, second.server_id) == (1, 2)
    # Terminal correlations are not counted: once pref_A ends, server 1 is emptier.
    connection.execute("UPDATE planning_runs SET state='FAILED' WHERE correlation_id='pref_A'")
    asyncio.run(admit("pref_C"))
    assert seen[-1] == ("pref_C", 1)
    connection.close()


def test_recorded_server_comes_from_the_ack_not_the_preference(tmp_path):
    _path, connection, store = _store(tmp_path)
    seen = []
    gate = _echo_gate(store, seen, ack=2)
    _planning(connection, "fallback_A", "RUNNING")
    receipt = asyncio.run(
        gate.ensure_seeded("fallback_A", origin_kind="planning", origin_id="fallback_A")
    )
    assert seen == [("fallback_A", 1)]
    assert receipt.server_id == 2
    assert store.live_pin_counts() == {1: 0, 2: 1}
    connection.close()


@pytest.mark.parametrize(
    "headers",
    (
        {},
        {"x-feature-stored-server-id": "0", "x-feature-actual-server-id": "0"},
        {"x-feature-stored-server-id": "1", "x-feature-actual-server-id": "2"},
        {"x-feature-stored-server-id": "3", "x-feature-actual-server-id": "3"},
    ),
)
def test_missing_or_invalid_ack_still_refuses(tmp_path, headers):
    _path, connection, store = _store(tmp_path)

    async def respond(_url, _key, _timeout, _preference):
        message = Message()
        for name, value in headers.items():
            message[name] = value
        return 200, message

    gate = FeatureRoutingGate(store, "http://router.invalid", request=respond)
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("ack_A", origin_kind="planning", origin_id="A"))
    assert store.state_for("ack_A") == "FAILED"
    connection.close()


def test_concurrent_different_keys_wait_for_the_earlier_placement(tmp_path):
    _path, connection, store = _store(tmp_path)
    _planning(connection, "race_A", "RUNNING")
    _planning(connection, "race_B", "RUNNING")
    seen = []

    async def scenario():
        release_first = asyncio.Event()

        async def delay(key):
            if key == "race_A":
                await release_first.wait()

        gate = _echo_gate(store, seen, delay=delay)
        first = asyncio.create_task(
            gate.ensure_seeded("race_A", origin_kind="planning", origin_id="race_A")
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            gate.ensure_seeded("race_B", origin_kind="planning", origin_id="race_B")
        )
        for _ in range(20):
            await asyncio.sleep(0)
        # The second admission has not counted or sent while the first is open.
        assert seen == [("race_A", 1)]
        release_first.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(scenario())
    assert seen == [("race_A", 1), ("race_B", 2)]
    assert (first.server_id, second.server_id) == (1, 2)
    connection.close()


@pytest.mark.parametrize("outcome", ("rejected", "ambiguous", "cancelled"))
def test_failed_or_cancelled_seed_releases_the_placement_lock(tmp_path, outcome):
    _path, connection, store = _store(tmp_path)
    seen = []

    async def scenario():
        started = asyncio.Event()
        never = asyncio.Event()

        async def request(_url, key, _timeout, preference):
            seen.append((key, preference))
            if key == "other_B":
                return 200, _headers(str(preference), str(preference))
            if outcome == "rejected":
                return 503, Message()
            if outcome == "ambiguous":
                raise ConnectionResetError("reset")
            started.set()
            await never.wait()

        gate = FeatureRoutingGate(store, "http://router.invalid", request=request)
        first = asyncio.create_task(
            gate.ensure_seeded("stuck_A", origin_kind="planning", origin_id="A")
        )
        if outcome == "cancelled":
            await asyncio.wait_for(started.wait(), timeout=2)
            first.cancel()
        with pytest.raises((FeatureRoutingBlocked, asyncio.CancelledError)):
            await first
        return await asyncio.wait_for(
            gate.ensure_seeded("other_B", origin_kind="planning", origin_id="B"),
            timeout=2,
        )

    receipt = asyncio.run(scenario())
    assert receipt.server_id == 1
    assert seen[-1] == ("other_B", 1)
    assert store.state_for("stuck_A") == (
        "FAILED" if outcome == "rejected" else "UNKNOWN"
    )
    connection.close()


def test_eight_long_admissions_a_minute_apart_split_four_and_four(tmp_path):
    """Simulation: admissions arrive a minute apart; every build runs for hours."""
    _path, connection, store = _store(tmp_path)
    seen = []
    gate = _echo_gate(store, seen)

    async def admissions():
        for minute in range(8):
            key = f"long_{minute}"
            _planning(connection, key, "BUILD_QUEUED")
            _build(connection, f"build-{minute}", key, "RUNNING")
            await gate.ensure_seeded(key, origin_kind="build", origin_id=key)

    asyncio.run(admissions())
    assert [preference for _key, preference in seen] == [1, 2] * 4
    assert store.live_pin_counts() == {1: 4, 2: 4}
    connection.close()


# ---------------------------------------------------------------------------
# One retry of a completed rejection (step 1, 24 October 2026; FRG-015/166)
# ---------------------------------------------------------------------------


def _counting_gate(store, responses):
    """A gate whose router answers from ``responses`` in order, counting calls."""
    calls = []

    async def request(_url, key, _timeout, _preference):
        calls.append(key)
        answer = responses[len(calls) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return FeatureRoutingGate(store, "http://router.invalid", request=request), calls


def test_a_rejection_then_success_is_two_requests_under_the_same_id(tmp_path):
    _path, connection, store = _store(tmp_path)
    gate, calls = _counting_gate(store, [(503, Message()), (200, _headers("2", "2"))])
    receipt = asyncio.run(
        gate.ensure_seeded("retry_A", origin_kind="planning", origin_id="A")
    )
    assert calls == ["retry_A", "retry_A"]
    assert receipt.server_id == 2
    assert store.state_for("retry_A") == "SUCCEEDED"
    # A SUCCEEDED receipt is reused with no new request.
    again = asyncio.run(
        gate.ensure_seeded("retry_A", origin_kind="planning", origin_id="A")
    )
    assert again == receipt
    assert len(calls) == 2
    connection.close()


def test_a_second_rejection_makes_no_third_request(tmp_path):
    _path, connection, store = _store(tmp_path)
    gate, calls = _counting_gate(store, [(503, Message()), (503, Message())])
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("twice_A", origin_kind="planning", origin_id="A"))
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("twice_A", origin_kind="planning", origin_id="A"))
    assert len(calls) == 2
    assert store.state_for("twice_A") == "FAILED"
    connection.close()


@pytest.mark.parametrize(
    "first",
    (TimeoutError("partial headers"), ConnectionResetError("reset")),
)
def test_an_unknown_outcome_is_never_replayed(tmp_path, first):
    _path, connection, store = _store(tmp_path)
    gate, calls = _counting_gate(store, [first, (200, _headers())])
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("unknown_A", origin_kind="planning", origin_id="A"))
    assert len(calls) == 1
    assert store.state_for("unknown_A") == "UNKNOWN"
    connection.close()


def test_a_cancelled_seed_is_not_asked_again(tmp_path):
    _path, connection, store = _store(tmp_path)
    calls = []

    async def scenario():
        started = asyncio.Event()

        async def request(_url, key, _timeout, _preference):
            calls.append(key)
            started.set()
            await asyncio.Event().wait()

        gate = FeatureRoutingGate(store, "http://router.invalid", request=request)
        task = asyncio.create_task(
            gate.ensure_seeded("cancel_A", origin_kind="planning", origin_id="A")
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(FeatureRoutingBlocked):
            await gate.ensure_seeded("cancel_A", origin_kind="planning", origin_id="A")

    asyncio.run(scenario())
    assert calls == ["cancel_A"]
    assert store.state_for("cancel_A") == "UNKNOWN"
    connection.close()


def test_a_seed_left_started_by_a_restart_is_not_asked_again(tmp_path):
    _path, connection, store = _store(tmp_path)
    store.claim_initial_seed("restart_A", "old", origin_kind="planning", origin_id="A")
    store.poison_unfinished_seeds_on_boot()
    gate, calls = _counting_gate(store, [(200, _headers())])
    with pytest.raises(FeatureRoutingBlocked):
        asyncio.run(gate.ensure_seeded("restart_A", origin_kind="planning", origin_id="A"))
    assert calls == []
    connection.close()


def test_a_failed_record_write_after_an_ack_retries_only_the_write(tmp_path):
    _path, connection, store = _store(tmp_path)
    real_complete = store.complete_seed
    writes = []

    def flaky_complete(key, attempt_id, server_id):
        writes.append(attempt_id)
        if len(writes) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_complete(key, attempt_id, server_id)

    store.complete_seed = flaky_complete
    gate, calls = _counting_gate(store, [(200, _headers("2", "2"))])
    receipt = asyncio.run(
        gate.ensure_seeded("write_A", origin_kind="planning", origin_id="A")
    )
    assert calls == ["write_A"]
    assert len(writes) == 2 and writes[0] == writes[1]
    assert receipt.server_id == 2
    assert store.state_for("write_A") == "SUCCEEDED"
    connection.close()


def test_a_record_write_that_fails_twice_still_dispatches_the_build(tmp_path, caplog):
    """Step 1 (25 October 2026): the acknowledged placement is used for the
    dispatch; the request is not sent again and is never called unknown."""
    from forge.pipeline.dispatchers.autobuild_async import dispatch_autobuild_async
    from forge.subagents.autobuild_runner import _validated_feature_routing
    from tests.forge.test_feature_routing_launch import (
        _Context,
        _Recorder,
        _Starter,
        _State,
    )

    _path, connection, store = _store(tmp_path)

    def broken_complete(*_args):
        raise sqlite3.OperationalError("disk I/O error")

    store.complete_seed = broken_complete
    gate, calls = _counting_gate(store, [(200, _headers("2", "2"))])
    starter = _Starter()
    asyncio.run(
        dispatch_autobuild_async(
            "build-B",
            "FEAT-B",
            "write_B",
            forward_context_builder=_Context(),
            async_task_starter=starter,
            stage_log_recorder=_Recorder(),
            state_channel=_State(),
            feature_routing_id="write_B",
            feature_routing_gate=gate,
            feature_routing_required=True,
        )
    )
    assert calls == ["write_B"]
    assert starter.context["feature_routing_receipt"]["server_id"] == 2
    # The runner takes the receipt it was handed.
    assert _validated_feature_routing(starter.context) == ("write_B", True)
    assert store.state_for("write_B") != "UNKNOWN"
    assert "the acknowledged placement is used for this dispatch" in caplog.text
    connection.close()
