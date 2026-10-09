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

    async def request(_url, key, _timeout):
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
