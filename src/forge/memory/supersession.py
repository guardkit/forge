"""Supersession-cycle detection for :class:`CalibrationAdjustment` (TASK-IC-008).

A :class:`~forge.memory.models.CalibrationAdjustment` is immutable — to "edit"
one, callers propose a new adjustment whose ``supersedes`` field references
the old adjustment's ``entity_id``. That linkage forms a chain of
adjustments per parameter; under a buggy producer (or a bad operator
import), the chain can close back on itself and become a cycle.

This module provides a single guard, :func:`assert_no_cycle`, that walks
the proposed chain top-down via a caller-supplied ``chain_resolver`` and
either:

* returns ``None`` for a clean linear chain that fits within ``max_depth``;
* raises :class:`SupersessionCycleError` when a visited ``entity_id`` is
  re-entered, when ``new.supersedes == new.entity_id`` (self-supersession),
  or when the chain exceeds ``max_depth``.

The walker is deliberately I/O-free: the ``chain_resolver`` callable
abstracts the lookup source (SQLite ledger, Graphiti node fetch, an
in-memory dict in tests) so this module remains pure and easy to unit-test.
``set`` lookup is O(1); the chain is short by design (default cap 10), so
no further data-structure cleverness is warranted.

The depth cap defaults to 10 and is configurable in ``forge.yaml`` for
operators who deliberately want longer chains. The tradeoff is operator
reasoning: longer supersession chains are harder for a human reviewer to
audit, so the default leans conservative.
"""

from __future__ import annotations

from typing import Callable

from .models import CalibrationAdjustment

__all__ = [
    "SupersessionCycleError",
    "assert_no_cycle",
]


class SupersessionCycleError(ValueError):
    """Raised when proposing a :class:`CalibrationAdjustment` would create a cycle.

    Subclasses :class:`ValueError` so callers that already catch validation
    errors at a coarser granularity (``except ValueError``) continue to
    handle this case sensibly. The message always includes the offending
    chain path so operators can debug from the log without a graph fetch.
    """


def assert_no_cycle(
    new_adjustment: CalibrationAdjustment,
    chain_resolver: Callable[[str], CalibrationAdjustment | None],
    max_depth: int = 10,
) -> None:
    """Walk ``new_adjustment.supersedes`` upward and reject cycles.

    Parameters
    ----------
    new_adjustment:
        The :class:`CalibrationAdjustment` being proposed. Its
        ``entity_id`` is seeded into the visited set before the walk so
        that a parent which loops back to ``new_adjustment`` is detected
        as a cycle.
    chain_resolver:
        Callable that returns the :class:`CalibrationAdjustment` for a
        given ``entity_id`` (as a ``str``), or ``None`` when no such
        adjustment is recorded. The walker stops at the first ``None`` —
        that is treated as a clean end-of-chain, not a cycle.
    max_depth:
        Maximum number of ancestors to traverse before giving up. Default
        is ``10``. Configurable in ``forge.yaml`` per the
        ``@edge-case @data-integrity supersession-cycle-rejection`` rule.

    Returns
    -------
    None
        For a clean linear chain whose length is ``<= max_depth``.

    Raises
    ------
    SupersessionCycleError
        When ``new_adjustment.supersedes == new_adjustment.entity_id``
        (self-supersession), when an ancestor's ``entity_id`` is already
        in the visited set (true cycle), or when the chain length exceeds
        ``max_depth``.
    ValueError
        When ``max_depth < 1``. A non-positive cap is meaningless because
        the walker would refuse to inspect any ancestor.
    """
    if max_depth < 1:
        raise ValueError(
            f"max_depth must be >= 1 (got {max_depth}); a non-positive cap "
            "would short-circuit cycle detection entirely."
        )

    new_id_str = str(new_adjustment.entity_id)

    # AC-005: self-supersession is a special case — detect it before we
    # touch the resolver. This both (a) avoids a useless lookup and
    # (b) gives a clearer error for the most common operator typo
    # ("I copied the entity_id field into supersedes by mistake").
    if (
        new_adjustment.supersedes is not None
        and str(new_adjustment.supersedes) == new_id_str
    ):
        raise SupersessionCycleError(
            "Self-supersession detected: CalibrationAdjustment "
            f"{new_id_str} cannot supersede itself."
        )

    if new_adjustment.supersedes is None:
        # First-ever adjustment for this parameter — no chain to walk.
        return None

    # Seed the visited set with new_adjustment's own id so an ancestor that
    # points back at the proposed adjustment is detected as a cycle.
    visited: set[str] = {new_id_str}
    # Track the path in walk order for human-readable error messages.
    path: list[str] = [new_id_str]

    current_id: str | None = str(new_adjustment.supersedes)

    for _ in range(max_depth):
        # AC-002: cycle detected when re-entering a visited id.
        if current_id is None:
            return None
        if current_id in visited:
            path.append(current_id)
            raise SupersessionCycleError(
                "Supersession cycle detected for CalibrationAdjustment "
                f"{new_id_str}. Cycle path: {' -> '.join(path)}"
            )

        visited.add(current_id)
        path.append(current_id)

        ancestor = chain_resolver(current_id)
        if ancestor is None:
            # The chain ends with a missing/unknown parent — treat as
            # clean termination, not a cycle. The producer is responsible
            # for referential integrity; we only police topology here.
            return None

        next_supersedes = ancestor.supersedes
        current_id = str(next_supersedes) if next_supersedes is not None else None

    # The for-loop budget is consumed. If the chain terminated cleanly on
    # the final iteration (``current_id is None``), the chain length was
    # exactly ``max_depth`` and is acceptable per AC-004 ("depth ≤ 10").
    if current_id is None:
        return None

    # AC-003: walked max_depth ancestors and there is still more chain to
    # follow — either it is genuinely too long or it contains a cycle we
    # have not yet re-entered. Either way, refuse to proceed and surface
    # the chain context so the operator can inspect it.
    raise SupersessionCycleError(
        "Supersession chain exceeds max_depth="
        f"{max_depth} for CalibrationAdjustment {new_id_str}. "
        f"Chain so far: {' -> '.join(path)}"
    )
