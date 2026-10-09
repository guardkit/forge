"""Durable, seed-once feature routing authority.

The SQLite row, rather than an in-process lock or lifecycle row, is the safety
boundary.  A STARTED claim is committed before its caller may perform HTTP and
a SUCCEEDED compare-and-swap is committed before a receipt may be released.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping

FEATURE_ROUTING_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}\Z", re.ASCII)
MAX_ERROR_LENGTH = 240


class FeatureRoutingError(RuntimeError):
    """Base class for fail-closed routing errors."""


class FeatureRoutingBlocked(FeatureRoutingError):
    """The identity has no authority to seed or dispatch."""


class FeatureRoutingTransactionError(FeatureRoutingError):
    """The writer cannot prove that its transaction committed."""


def validate_feature_routing_id(value: object) -> str:
    """Return a valid raw routing ID without coercion or normalization."""
    if not isinstance(value, str) or FEATURE_ROUTING_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "feature_routing_id must be an unmodified ASCII "
            "[A-Za-z0-9_-]{1,256} value"
        )
    return value


@dataclass(frozen=True, slots=True)
class FeatureRoutingReceipt:
    feature_routing_id: str
    attempt_id: str
    server_id: int

    def to_wire(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_wire(
        cls, value: object, *, expected_feature_routing_id: str
    ) -> "FeatureRoutingReceipt":
        key = validate_feature_routing_id(expected_feature_routing_id)
        if not isinstance(value, Mapping):
            raise FeatureRoutingBlocked("feature routing receipt is missing")
        if set(value) != {"feature_routing_id", "attempt_id", "server_id"}:
            raise FeatureRoutingBlocked("feature routing receipt shape is invalid")
        receipt_key = value.get("feature_routing_id")
        attempt_id = value.get("attempt_id")
        server_id = value.get("server_id")
        if receipt_key != key:
            raise FeatureRoutingBlocked("feature routing receipt identity conflicts")
        if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 256:
            raise FeatureRoutingBlocked("feature routing receipt attempt is invalid")
        if (
            isinstance(server_id, bool)
            or not isinstance(server_id, int)
            or server_id not in (1, 2)
        ):
            raise FeatureRoutingBlocked("feature routing receipt server is invalid")
        return cls(key, attempt_id, server_id)


def validate_feature_routing_launch_receipt(
    feature_routing_id: object,
    receipt: object,
    *,
    required: bool,
) -> tuple[str | None, dict[str, object] | None]:
    """Validate one launch's explicit routing authority.

    Required launches need both the exact routing key and the coordinator's
    committed receipt.  Optional, unkeyed standalone launches retain their
    historical shape.  A receipt that *is* supplied is always consumed and
    validated; it can never be silently ignored merely because routing is
    optional at that call site.
    """
    key = (
        validate_feature_routing_id(feature_routing_id)
        if feature_routing_id is not None
        else None
    )
    if required and key is None:
        raise FeatureRoutingBlocked("required feature routing has no identity")
    if receipt is None:
        if required:
            raise FeatureRoutingBlocked("required feature routing has no receipt")
        return key, None
    if key is None:
        raise FeatureRoutingBlocked(
            "feature routing receipt was supplied without an identity"
        )
    canonical = FeatureRoutingReceipt.from_wire(
        receipt, expected_feature_routing_id=key
    )
    return key, canonical.to_wire()


@dataclass(frozen=True, slots=True)
class SeedClaim:
    owned: bool
    receipt: FeatureRoutingReceipt | None = None


class FeatureRoutingSeedStore:
    """Synchronous store over the coordinator's existing writer connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = threading.RLock()

    def _require_clean_boundary(self) -> None:
        if self._connection.in_transaction:
            raise FeatureRoutingTransactionError(
                "feature routing refuses an uncommitted outer transaction"
            )

    def _begin(self) -> None:
        self._require_clean_boundary()
        self._connection.execute("BEGIN IMMEDIATE")

    def claim_initial_seed(
        self,
        feature_routing_id: str,
        attempt_id: str,
        *,
        origin_kind: str,
        origin_id: str,
    ) -> SeedClaim:
        key = validate_feature_routing_id(feature_routing_id)
        if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 256:
            raise ValueError("attempt_id must be a nonempty string of at most 256 characters")
        if not origin_kind or not origin_id:
            raise ValueError("seed origin kind and id must be nonempty")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT state, attempt_id, server_id FROM feature_routing_seeds "
                    "WHERE feature_routing_id = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        "INSERT INTO feature_routing_seeds "
                        "(feature_routing_id, state, attempt_id, origin_kind, origin_id, started_at) "
                        "VALUES (?, 'STARTED', ?, ?, ?, ?)",
                        (key, attempt_id, str(origin_kind), str(origin_id), now),
                    )
                    self._connection.execute("COMMIT")
                    return SeedClaim(owned=True)
                state, existing_attempt, server_id = row
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        if state == "SUCCEEDED":
            return SeedClaim(
                owned=False,
                receipt=FeatureRoutingReceipt.from_wire(
                    {
                        "feature_routing_id": key,
                        "attempt_id": existing_attempt,
                        "server_id": server_id,
                    },
                    expected_feature_routing_id=key,
                ),
            )
        raise FeatureRoutingBlocked(
            f"feature routing identity {key!r} is permanently blocked in {state}"
        )

    def complete_seed(
        self, feature_routing_id: str, attempt_id: str, server_id: int
    ) -> FeatureRoutingReceipt:
        key = validate_feature_routing_id(feature_routing_id)
        if (
            isinstance(server_id, bool)
            or not isinstance(server_id, int)
            or server_id not in (1, 2)
        ):
            raise ValueError("recognized server_id must be 1 or 2")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._begin()
            try:
                cursor = self._connection.execute(
                    "UPDATE feature_routing_seeds SET state='SUCCEEDED', completed_at=?, "
                    "server_id=?, error=NULL WHERE feature_routing_id=? AND attempt_id=? "
                    "AND state='STARTED'",
                    (now, server_id, key, attempt_id),
                )
                if cursor.rowcount != 1:
                    raise FeatureRoutingBlocked(
                        "seed success lost ownership; no dispatch authority released"
                    )
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return self.read_success_receipt(key)

    def fail_seed(
        self,
        feature_routing_id: str,
        attempt_id: str,
        *,
        state: Literal["UNKNOWN", "FAILED"],
        reason: str,
    ) -> bool:
        key = validate_feature_routing_id(feature_routing_id)
        if state not in ("UNKNOWN", "FAILED"):
            raise ValueError("seed failure state must be UNKNOWN or FAILED")
        bounded = str(reason).replace("\n", " ")[:MAX_ERROR_LENGTH] or "seed-failed"
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._begin()
            try:
                cursor = self._connection.execute(
                    "UPDATE feature_routing_seeds SET state=?, completed_at=?, server_id=NULL, error=? "
                    "WHERE feature_routing_id=? AND attempt_id=? AND state='STARTED'",
                    (state, now, bounded, key, attempt_id),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return cursor.rowcount == 1

    def poison_unfinished_seeds_on_boot(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._begin()
            try:
                cursor = self._connection.execute(
                    "UPDATE feature_routing_seeds SET state='UNKNOWN', completed_at=?, "
                    "error='seed-outcome-unknown-after-coordinator-restart' "
                    "WHERE state='STARTED'",
                    (now,),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return cursor.rowcount

    def read_success_receipt(self, feature_routing_id: str) -> FeatureRoutingReceipt:
        key = validate_feature_routing_id(feature_routing_id)
        with self._lock:
            self._require_clean_boundary()
            row = self._connection.execute(
                "SELECT state, attempt_id, server_id FROM feature_routing_seeds "
                "WHERE feature_routing_id=?",
                (key,),
            ).fetchone()
        if row is None or row[0] != "SUCCEEDED":
            state = "ABSENT" if row is None else str(row[0])
            raise FeatureRoutingBlocked(
                f"feature routing identity {key!r} has no committed success ({state})"
            )
        return FeatureRoutingReceipt.from_wire(
            {
                "feature_routing_id": key,
                "attempt_id": row[1],
                "server_id": row[2],
            },
            expected_feature_routing_id=key,
        )

    def state_for(self, feature_routing_id: str) -> str | None:
        key = validate_feature_routing_id(feature_routing_id)
        row = self._connection.execute(
            "SELECT state FROM feature_routing_seeds WHERE feature_routing_id=?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])


def router_restart_blockers(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Return durable reasons a router restart/table reset is unsafe.

    This is a read-only, fail-closed ledger projection for the operations
    helper.  In addition to ordinary nonterminal planning/build rows it covers
    R6's delivery gap: planning has published BUILD_QUEUED, but the durable
    consumer has not yet created (or terminally settled) the matching build.
    A terminal build is not sufficient on its own: the merge card and the
    publication press deliberately run after routine builds become COMPLETE.
    Their durable pending/claimed records therefore remain blockers too.
    """
    started = not connection.in_transaction
    if started:
        connection.execute("BEGIN")
    try:
        blockers = _router_restart_blockers_snapshot(connection)
    except BaseException:
        if started:
            connection.rollback()
        raise
    else:
        if started:
            connection.commit()
        return blockers


def _router_restart_blockers_snapshot(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    from forge.lifecycle.merge_retirement import retired_decision_ids
    from forge.lifecycle.planning_handoff_retirement import (
        retired_planning_handoff_correlations,
    )

    # Parity validation with the router's production maintenance guard. Forge
    # grants no blocker exemption from this set; its runtime admission guards
    # consume the same canonical reader separately.
    retired_planning_handoff_correlations(connection)

    planning_terminal = (
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "PLANNED_HANDOFF",
        "BUILD_QUEUED",
    )
    build_terminal = ("COMPLETE", "FAILED", "CANCELLED", "SKIPPED")
    blockers: list[str] = []
    planning_marks = ",".join("?" for _ in planning_terminal)
    for correlation_id, state in connection.execute(
        "SELECT correlation_id, state FROM planning_runs "
        f"WHERE state NOT IN ({planning_marks})",
        planning_terminal,
    ):
        blockers.append(f"planning:{correlation_id}:{state}")
    build_marks = ",".join("?" for _ in build_terminal)
    for build_id, state in connection.execute(
        "SELECT build_id, status FROM builds "
        f"WHERE status NOT IN ({build_marks})",
        build_terminal,
    ):
        blockers.append(f"build:{build_id}:{state}")

    # A routine build is COMPLETE before its merge approval card is offered.
    # The request id is the durable fact that the card is still outstanding.
    for build_id, request_id in connection.execute(
        "SELECT build_id, pending_approval_request_id FROM builds "
        "WHERE pending_approval_request_id IS NOT NULL "
        "AND trim(pending_approval_request_id) <> ''"
    ):
        blockers.append(f"merge-approval:{build_id}:{request_id}")

    # Merge approval has its own durable stage pair and is offered after a
    # routine build is COMPLETE.  An offer without its matching decision is
    # an active paused journey even though the build row is terminal.
    for (build_id,) in connection.execute(
        "SELECT DISTINCT offered.build_id FROM stage_log AS offered "
        "WHERE offered.target_identifier = 'merge_deploy_offer' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM stage_log AS decided "
        "WHERE decided.build_id = offered.build_id "
        "AND decided.target_identifier = 'merge_deploy_decision' "
        "AND decided.id > offered.id"
        ")"
    ):
        blockers.append(f"merge-offer-pending:{build_id}")

    # An approved decision closes the offer before the executor's task gets
    # its first event-loop turn.  Keep that accepted work active until the
    # executor records its terminal report.  PASSED is the durable approve;
    # SKIPPED is a decline and needs no work.  This also covers recovery after
    # a process exit without consulting a lease or a wall clock.  Report
    # attribution is exact: row order alone cannot distinguish this consumer's
    # queued press from a direct CLI press that started earlier and reported
    # later for the same build.  Legacy/unversioned approved decisions have no
    # provable matching report and therefore remain fail-closed blockers.
    retired_ids = retired_decision_ids(connection)
    approved_query = (
        "SELECT decided.id, decided.build_id FROM stage_log AS decided "
        "WHERE decided.target_identifier = 'merge_deploy_decision' "
        "AND decided.status = 'PASSED' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM stage_log AS reported "
        "WHERE reported.build_id = decided.build_id "
        "AND reported.target_identifier = 'merge_deploy_executor' "
        "AND reported.status IN ('PASSED', 'FAILED', 'SKIPPED') "
        "AND reported.id > decided.id "
        "AND json_type(CASE WHEN json_valid(decided.details_json) "
        "THEN decided.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_version') = 'integer' "
        "AND json_extract(CASE WHEN json_valid(decided.details_json) "
        "THEN decided.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_version') = 1 "
        "AND json_type(CASE WHEN json_valid(decided.details_json) "
        "THEN decided.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_id') = 'text' "
        "AND length(json_extract(CASE WHEN json_valid(decided.details_json) "
        "THEN decided.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_id')) = 32 "
        "AND json_extract(CASE WHEN json_valid(decided.details_json) "
        "THEN decided.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_id') "
        "NOT GLOB '*[^0-9a-f]*' "
        "AND json_type(CASE WHEN json_valid(reported.details_json) "
        "THEN reported.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_version') = 'integer' "
        "AND json_extract(CASE WHEN json_valid(reported.details_json) "
        "THEN reported.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_version') = "
        "json_extract(CASE WHEN json_valid(decided.details_json) "
        "THEN decided.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_version') "
        "AND json_extract(CASE WHEN json_valid(reported.details_json) "
        "THEN reported.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_id') = "
        "json_extract(CASE WHEN json_valid(decided.details_json) "
        "THEN decided.details_json ELSE '{}' END, "
        "'$.merge_decision.execution_attempt_id')"
        ")"
    )
    for decision_id, build_id in connection.execute(approved_query):
        marker = f"merge-approved-pending:{build_id}"
        if decision_id not in retired_ids and marker not in blockers:
            blockers.append(marker)

    # Publication records have their own lifecycle after builds are terminal.
    # Do not interpret lease expiry here: result=NULL is queued/recoverable
    # work even without a holder, and any retained holder is active or
    # ambiguous until the publication executor releases it.  A non-NULL
    # result with no holder is historical under the current executor.
    for build_id, result, holder in connection.execute(
        "SELECT build_id, result, lease_holder FROM publication_records "
        "WHERE result IS NULL "
        "OR (lease_holder IS NOT NULL AND trim(lease_holder) <> '')"
    ):
        if result is None:
            blockers.append(f"publication-pending:{build_id}")
        else:
            blockers.append(f"publication-held:{build_id}:{holder}")

    # Immutable correlation join: only a committed SUCCEEDED seed for this
    # exact planning correlation is relevant.  A missing build row is the
    # JetStream pending-delivery window; a nonterminal one remains active.
    rows = connection.execute(
        "SELECT p.correlation_id, b.build_id, b.status "
        "FROM planning_runs AS p "
        "JOIN feature_routing_seeds AS s "
        "ON s.feature_routing_id = p.correlation_id AND s.state = 'SUCCEEDED' "
        "LEFT JOIN builds AS b ON b.correlation_id = p.correlation_id "
        "WHERE p.state = 'BUILD_QUEUED'"
    ).fetchall()
    for correlation_id, build_id, status in rows:
        if build_id is None:
            blockers.append(f"queued-undelivered:{correlation_id}")
        elif status not in build_terminal:
            marker = f"queued-build-active:{correlation_id}:{build_id}:{status}"
            if f"build:{build_id}:{status}" not in blockers:
                blockers.append(marker)
    return tuple(blockers)
