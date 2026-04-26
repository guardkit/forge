"""Unit tests for ``forge.memory.supersession`` (TASK-IC-008).

Each test class maps to one or more acceptance criteria from
``tasks/design_approved/TASK-IC-008-supersession-cycle-detection.md``:

* :class:`TestCleanChainPasses`            — AC-001, AC-004 (walks the chain
                                              via ``chain_resolver`` and
                                              returns ``None`` for clean
                                              linear chains within depth).
* :class:`TestCycleDetection`              — AC-002, AC-006 (visited-set
                                              cycle detection of length 2
                                              and length 5; error message
                                              includes the cycle path).
* :class:`TestDepthCap`                    — AC-003 (depth-11 chain raises;
                                              ``max_depth`` is configurable;
                                              chain context in message).
* :class:`TestSelfSupersession`            — AC-005 (``new.supersedes ==
                                              new.entity_id`` raises
                                              immediately).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from forge.memory.models import CalibrationAdjustment
from forge.memory.supersession import SupersessionCycleError, assert_no_cycle

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ts(offset_minutes: int = 0) -> datetime:
    """Return a deterministic UTC timestamp with optional minute offset."""
    return datetime(2026, 4, 25, 12, 0, tzinfo=UTC) + timedelta(minutes=offset_minutes)


def _adj(
    entity_id: UUID,
    supersedes: UUID | None = None,
    *,
    parameter: str = "confidence_threshold",
    old_value: str = "0.7",
    new_value: str = "0.8",
) -> CalibrationAdjustment:
    """Build a minimal valid :class:`CalibrationAdjustment` for tests."""
    return CalibrationAdjustment(
        entity_id=entity_id,
        parameter=parameter,
        old_value=old_value,
        new_value=new_value,
        approved=False,
        supersedes=supersedes,
        proposed_at=_ts(),
        expires_at=_ts(60),
    )


def _build_resolver(
    adjustments: list[CalibrationAdjustment],
):
    """Return a ``chain_resolver`` keyed by ``str(entity_id)``.

    The resolver is the abstraction the unit tests target — the real
    implementation may be backed by SQLite or Graphiti, but for unit
    coverage we feed a pre-built dict.
    """
    by_id: dict[str, CalibrationAdjustment] = {
        str(adj.entity_id): adj for adj in adjustments
    }

    def _resolver(entity_id: str) -> CalibrationAdjustment | None:
        return by_id.get(entity_id)

    return _resolver


# ---------------------------------------------------------------------------
# AC-001 / AC-004 — Clean linear chains return None
# ---------------------------------------------------------------------------


class TestCleanChainPasses:
    """``assert_no_cycle`` returns ``None`` for clean linear chains."""

    def test_no_supersedes_returns_none(self) -> None:
        """First-ever adjustment (``supersedes=None``) is always safe."""
        new = _adj(uuid4(), supersedes=None)
        resolver = _build_resolver([])

        result = assert_no_cycle(new, resolver)

        assert result is None

    def test_short_linear_chain_returns_none(self) -> None:
        """Linear chain of depth 3 (new → A → B → None) returns None."""
        b = _adj(uuid4(), supersedes=None)
        a = _adj(uuid4(), supersedes=b.entity_id)
        new = _adj(uuid4(), supersedes=a.entity_id)
        resolver = _build_resolver([a, b])

        result = assert_no_cycle(new, resolver)

        assert result is None

    def test_chain_at_max_depth_returns_none(self) -> None:
        """Chain of exactly depth 10 (the default cap) is still clean."""
        # Build a strictly-linear chain of 10 ancestors so total depth is 10.
        ancestors: list[CalibrationAdjustment] = []
        prev_id: UUID | None = None
        for _ in range(10):
            adj = _adj(uuid4(), supersedes=prev_id)
            ancestors.append(adj)
            prev_id = adj.entity_id
        new = _adj(uuid4(), supersedes=prev_id)
        resolver = _build_resolver(ancestors)

        result = assert_no_cycle(new, resolver)

        assert result is None

    def test_walks_via_chain_resolver_lookup(self) -> None:
        """Resolver is invoked with ``str(entity_id)`` keys (AC-001)."""
        b = _adj(uuid4(), supersedes=None)
        a = _adj(uuid4(), supersedes=b.entity_id)
        new = _adj(uuid4(), supersedes=a.entity_id)
        seen_keys: list[str] = []

        def resolver(entity_id: str) -> CalibrationAdjustment | None:
            seen_keys.append(entity_id)
            return {str(a.entity_id): a, str(b.entity_id): b}.get(entity_id)

        assert_no_cycle(new, resolver)

        # Walked the chain top-down: a first, then b. Both keys were strings.
        assert seen_keys == [str(a.entity_id), str(b.entity_id)]
        assert all(isinstance(k, str) for k in seen_keys)


# ---------------------------------------------------------------------------
# AC-002 / AC-006 — Visited-set cycle detection + path in error message
# ---------------------------------------------------------------------------


class TestCycleDetection:
    """Cycles raise :class:`SupersessionCycleError` with chain context."""

    def test_cycle_of_length_two_raises(self) -> None:
        """new → A → new (A.supersedes points back at new) is a cycle."""
        new_id = uuid4()
        a_id = uuid4()
        a = _adj(a_id, supersedes=new_id)
        new = _adj(new_id, supersedes=a_id)
        resolver = _build_resolver([a, new])

        with pytest.raises(SupersessionCycleError) as exc_info:
            assert_no_cycle(new, resolver)

        msg = str(exc_info.value)
        assert str(new_id) in msg
        assert str(a_id) in msg

    def test_cycle_of_length_five_raises(self) -> None:
        """A 5-node cycle: new → A → B → C → D → A is detected."""
        ids = [uuid4() for _ in range(4)]  # A, B, C, D
        a, b, c, d = ids
        # D supersedes A → cycle closes back to A
        d_adj = _adj(d, supersedes=a)
        c_adj = _adj(c, supersedes=d)
        b_adj = _adj(b, supersedes=c)
        a_adj = _adj(a, supersedes=b)
        new = _adj(uuid4(), supersedes=a)
        resolver = _build_resolver([a_adj, b_adj, c_adj, d_adj])

        with pytest.raises(SupersessionCycleError) as exc_info:
            assert_no_cycle(new, resolver)

        msg = str(exc_info.value)
        # AC-006: the cycle path must be visible in the message.
        for entity_id in ids:
            assert str(entity_id) in msg

    def test_cycle_error_is_value_error_subclass(self) -> None:
        """``SupersessionCycleError`` must subclass ``ValueError`` per spec."""
        new_id = uuid4()
        a = _adj(uuid4(), supersedes=new_id)
        new = _adj(new_id, supersedes=a.entity_id)
        resolver = _build_resolver([a, new])

        with pytest.raises(ValueError):
            assert_no_cycle(new, resolver)


# ---------------------------------------------------------------------------
# AC-003 — Configurable max_depth
# ---------------------------------------------------------------------------


class TestDepthCap:
    """``max_depth`` defaults to 10 and is configurable; cap raises."""

    def test_chain_of_depth_eleven_raises_at_default_cap(self) -> None:
        """An 11-deep chain exceeds the default ``max_depth=10``."""
        ancestors: list[CalibrationAdjustment] = []
        prev_id: UUID | None = None
        for _ in range(11):
            adj = _adj(uuid4(), supersedes=prev_id)
            ancestors.append(adj)
            prev_id = adj.entity_id
        new = _adj(uuid4(), supersedes=prev_id)
        resolver = _build_resolver(ancestors)

        with pytest.raises(SupersessionCycleError) as exc_info:
            assert_no_cycle(new, resolver)

        msg = str(exc_info.value)
        # AC-003: error message must carry chain context.
        assert "depth" in msg.lower() or "max_depth" in msg.lower()

    def test_max_depth_is_configurable(self) -> None:
        """A custom ``max_depth=2`` rejects a depth-3 chain."""
        c = _adj(uuid4(), supersedes=None)
        b = _adj(uuid4(), supersedes=c.entity_id)
        a = _adj(uuid4(), supersedes=b.entity_id)
        new = _adj(uuid4(), supersedes=a.entity_id)
        resolver = _build_resolver([a, b, c])

        with pytest.raises(SupersessionCycleError):
            assert_no_cycle(new, resolver, max_depth=2)

    def test_resolver_returning_none_terminates_walk(self) -> None:
        """Walk stops when resolver returns ``None`` (broken/missing chain)."""
        # ``new`` references a missing parent — the chain ends, not a cycle.
        new = _adj(uuid4(), supersedes=uuid4())
        resolver = _build_resolver([])

        result = assert_no_cycle(new, resolver)

        assert result is None


# ---------------------------------------------------------------------------
# AC-005 — Self-supersession is rejected immediately
# ---------------------------------------------------------------------------


class TestSelfSupersession:
    """``new.supersedes == new.entity_id`` raises before resolver is called."""

    def test_self_supersession_raises_immediately(self) -> None:
        """An adjustment that supersedes itself is rejected up front."""
        same_id = uuid4()
        new = _adj(same_id, supersedes=same_id)
        # Resolver sentinel that fails the test if it is invoked at all.
        called: list[str] = []

        def resolver(entity_id: str) -> CalibrationAdjustment | None:
            called.append(entity_id)
            return None

        with pytest.raises(SupersessionCycleError) as exc_info:
            assert_no_cycle(new, resolver)

        # AC-005: detection happens before any resolver lookup.
        assert called == []
        # AC-006: the offending id is included in the error message.
        assert str(same_id) in str(exc_info.value)
