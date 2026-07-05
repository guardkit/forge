"""Deterministic ``request_id`` derivation for the gating protocol.

This module closes risk **R5** (re-emission diverges from the original
``request_id``, breaking responder idempotency on Rich's side) per
ASSUM-006 and ``API-nats-approval-protocol.md §6``: the human-approval
responder deduplicates on ``request_id`` with first-response-wins
semantics. Forge re-emits the request after a crash, and the re-emitted
request **must** carry the same ``request_id`` for that dedup to hold.

Therefore the derivation in :func:`derive_request_id` is a **pure
function** of ``(build_id, stage_label, attempt_count)`` only — no
UUIDs, no timestamps, no ambient state, no I/O.

Format
------

The derivation produces an output of the shape::

    f"{enc(build_id)}:{enc(stage_label)}:{attempt_count}"

where ``enc()`` percent-encodes each component so that the combined
string is simultaneously:

* **URL-safe** — only RFC 3986 unreserved characters (``A-Z``,
  ``a-z``, ``0-9``, ``-``, ``_``) plus the percent-encoding alphabet
  (``%`` followed by two hex digits) and the non-NATS-token separator
  ``:``. The unreserved characters ``.`` and ``~`` are also encoded so
  they cannot appear in the output.
* **NATS-subject-safe** — the NATS subject separator ``.``, the
  wildcards ``*`` and ``>``, and ASCII whitespace cannot appear in the
  output. Stage labels containing spaces (e.g. ``"Architecture
  Review"``) become ``Architecture%20Review`` after encoding.

The format is **stable**: a future change to the encoding would change
the request_id for the same logical inputs and break responder dedup
on outstanding approvals. Treat the format as a wire contract.

Consumers
---------

* TASK-CGCP-006 — publisher emits ``request_id`` on first send.
* TASK-CGCP-007 — subscriber dedups on ``request_id``.
* TASK-CGCP-008 — synthetic CLI injector reuses for the paused stage.
* TASK-CGCP-010 — state-machine integration; reads ``attempt_count``
  from SQLite for re-emission.
"""

from __future__ import annotations

# AC-007: only standard-library imports — nothing from ``nats_core``,
# ``nats-py``, or ``langgraph``.
from urllib.parse import quote, unquote

__all__ = ["derive_request_id", "parse_request_id"]


# Characters that are RFC 3986 unreserved but problematic for NATS
# subjects (``.``) or visually ambiguous in URLs (``~``). We encode
# them post-quote() so the output alphabet is reduced to
# ``[A-Za-z0-9_\-:%]``.
_EXTRA_ENCODE = (
    (".", "%2E"),
    ("~", "%7E"),
)


def _encode_component(value: str) -> str:
    """Percent-encode ``value`` for safe inclusion in the request_id.

    ``urllib.parse.quote`` with ``safe=""`` encodes every reserved
    character; the only unreserved characters it lets through are
    ``A-Z``, ``a-z``, ``0-9``, ``-``, ``_``, ``.``, and ``~``. We then
    further encode ``.`` and ``~`` so neither can appear in a NATS
    subject token.
    """
    encoded = quote(value, safe="")
    for raw, escaped in _EXTRA_ENCODE:
        encoded = encoded.replace(raw, escaped)
    return encoded


def derive_request_id(
    *,
    build_id: str,
    stage_label: str,
    attempt_count: int,
) -> str:
    """Return a deterministic, URL-safe ``request_id`` for an approval.

    Args:
        build_id: Stable identifier of the build being gated. Must be
            non-empty.
        stage_label: Human-readable stage label (e.g. ``"Architecture
            Review"``). Spaces and other non-URL-safe characters are
            percent-encoded; the empty string is rejected.
        attempt_count: Monotonic counter incremented on each
            refresh-on-timeout per ``API §7``. Same ``attempt_count``
            for the same ``(build_id, stage_label)`` pair MUST yield
            the same id (idempotency); a different ``attempt_count``
            MUST yield a different id (refresh distinguishability).
            Must be ``>= 0``.

    Returns:
        A stable, URL-safe, NATS-subject-safe string formed by
        joining the encoded ``build_id``, the encoded ``stage_label``,
        and the integer ``attempt_count`` with ``:`` separators. The
        function is pure: same inputs ⇒ same output across calls and
        across processes.

    Raises:
        ValueError: if ``build_id`` is empty, ``stage_label`` is
            empty, or ``attempt_count`` is negative.
    """
    if not build_id:
        raise ValueError("build_id must be a non-empty string")
    if not stage_label:
        raise ValueError("stage_label must be a non-empty string")
    if attempt_count < 0:
        raise ValueError(
            f"attempt_count must be non-negative, got {attempt_count!r}"
        )

    return (
        f"{_encode_component(build_id)}"
        f":{_encode_component(stage_label)}"
        f":{attempt_count}"
    )


def parse_request_id(request_id: str) -> tuple[str, str, int]:
    """Invert :func:`derive_request_id`.

    ``derive_request_id`` emits ``f"{enc(build_id)}:{enc(stage_label)}:
    {attempt_count}"`` where ``enc`` percent-encodes every reserved
    character (including ``:``) via ``urllib.parse.quote(safe="")`` and
    then additionally escapes ``.`` and ``~``. Because the encoded
    ``build_id`` / ``stage_label`` components can never contain a literal
    ``:``, the output holds *exactly* two ``:`` separators, so splitting on
    ``:`` recovers the three fields unambiguously. ``urllib.parse.unquote``
    reverses both the ``quote`` step and the extra ``%2E`` / ``%7E``
    escapes (they decode straight back to ``.`` / ``~``), making this the
    exact inverse of the forward derivation.

    Used by the rearm path (TASK-GATE-D659): the persisted
    ``pending_approval_request_id`` is the durable home of the paused
    build's ``(stage_label, attempt_count)`` pair — parsing it back avoids
    a second SQLite column.

    Args:
        request_id: A ``request_id`` produced by :func:`derive_request_id`.

    Returns:
        The ``(build_id, stage_label, attempt_count)`` triple that would
        re-derive ``request_id`` exactly.

    Raises:
        ValueError: If ``request_id`` is empty, does not contain exactly
            two ``:`` separators, or the final component is not a
            non-negative integer. Callers (rearm) log ERROR and skip the
            row on a legacy / unparseable id.
    """
    if not request_id:
        raise ValueError("parse_request_id: request_id must be a non-empty string")

    parts = request_id.split(":")
    if len(parts) != 3:
        raise ValueError(
            "parse_request_id: expected exactly two ':' separators "
            f"(three fields); got {len(parts)} field(s) in {request_id!r}"
        )

    encoded_build, encoded_stage, raw_attempt = parts
    try:
        attempt_count = int(raw_attempt)
    except ValueError as exc:
        raise ValueError(
            "parse_request_id: attempt_count component is not an integer: "
            f"{raw_attempt!r} in {request_id!r}"
        ) from exc
    if attempt_count < 0:
        raise ValueError(
            "parse_request_id: attempt_count must be non-negative; got "
            f"{attempt_count!r} in {request_id!r}"
        )

    build_id = unquote(encoded_build)
    stage_label = unquote(encoded_stage)
    if not build_id or not stage_label:
        raise ValueError(
            "parse_request_id: decoded build_id / stage_label must be "
            f"non-empty; got build_id={build_id!r} stage_label={stage_label!r}"
        )
    return build_id, stage_label, attempt_count
