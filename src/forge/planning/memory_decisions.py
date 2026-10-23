"""The project's decision records from memory, for the planning writers.

23 October 2026. Planning stages run without tools, so nothing a project's
architecture sessions saved to memory reached the spec writer or the plan
writer. GuardKit's ``/system-arch`` and ``/system-plan`` save each decision
record as an ``adr`` payload tagged ``architecture`` in the project's own memory
project. Once per run, at the door, Forge searches that project for exactly
those, ranked against the request, keeps the best within :data:`BUDGET_BYTES`
and records the outcome; every spec and plan call then sends the recorded text.

Nothing here refuses a run. Memory switched off, a name the repository was not
granted, a failed search or no records each record a plain outcome, and the
writers are sent what they were sent before.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from forge.adapters.fleet_memory.priors import SEARCH_RESULT_LIMIT
from forge.memory import redact_credentials

STAGE = "memory-decisions"
RECEIPT_KEY = "memory_decisions"
PAYLOAD_TYPE = "adr"
DOMAIN_TAG = "architecture"
#: The records' own text, summed, never goes over this. Records that would are
#: left out, lowest-ranked first, and the text and the record both say so.
BUDGET_BYTES = 16 * 1024

_FIELDS = (
    ("status", "Status"),
    ("decision", "Decision"),
    ("context", "Context"),
    ("consequences", "Consequences"),
    ("alternatives", "Alternatives considered"),
    ("source_ref", "Source"),
)


def render_record(natural_key: str, payload: dict[str, Any]) -> str:
    """One decoded decision record as plain text: a heading, then its filled
    fields. ``natural_key`` (the envelope's) names it when it has no title."""
    title = payload.get("title") or payload.get("identifier") or natural_key
    lines = [f"### {title}"]
    for key, label in _FIELDS:
        field = payload.get(key)
        if isinstance(field, list):
            field = "; ".join(str(item) for item in field if str(item).strip())
        if field is not None and str(field).strip():
            lines.append(f"{label}: {str(field).strip()}")
    return redact_credentials("\n".join(lines)) + "\n"


def _unusable(record: dict[str, Any]) -> str | None:
    """Why a searched record cannot be given, or ``None``. A decision record's
    one required substance is its decision text."""
    if record.get("problem"):
        return str(record["problem"])
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return "its stored content could not be decoded"
    decision = payload.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        return "its stored content has no decision text"
    return None


def outcome(
    memory_project: str | None,
    records: list[dict[str, Any]] | None,
    why_not: str | None,
) -> dict[str, Any]:
    """The recorded outcome: what was searched, what is sent, what was left out.

    ``records`` are the reader's, best first, decoded from their stored
    envelopes, and at most the search's :data:`SEARCH_RESULT_LIMIT` best
    matches, which the detail says. A record that could not be decoded or has
    no decision text is skipped and named with the reason. The rest are kept in
    rank order while their rendered text fits :data:`BUDGET_BYTES`; the first
    that does not, and every one ranked below it, is left out by name.
    """
    base: dict[str, Any] = {
        "memory_project": memory_project,
        "payload_type": PAYLOAD_TYPE,
        "domain_tag": DOMAIN_TAG,
        "budget_bytes": BUDGET_BYTES,
        "search_limit": SEARCH_RESULT_LIMIT,
        "records": [],
        "dropped": [],
        "skipped": [],
        "text": None,
    }
    if why_not is not None:
        return {**base, "outcome": "not-searched",
                "detail": f"No decision records were read from memory: {why_not}."}
    if not records:
        return {**base, "outcome": "none-found",
                "detail": f"The memory search found no {PAYLOAD_TYPE} records tagged "
                          f"{DOMAIN_TAG} in memory project {memory_project} among the at "
                          f"most {SEARCH_RESULT_LIMIT} best matches for the request."}
    kept: list[tuple[dict[str, Any], str]] = []
    dropped: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    total = 0
    for record in records:
        why = _unusable(record)
        if why is not None:
            skipped.append({"natural_key": record["natural_key"], "score": record["score"],
                            "why": why})
            continue
        text = render_record(record["natural_key"], record["payload"])
        size = len(text.encode("utf-8"))
        entry = {"natural_key": record["natural_key"], "score": record["score"], "bytes": size}
        if dropped or total + size > BUDGET_BYTES:
            dropped.append(entry)
            continue
        total += size
        kept.append(({**entry, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}, text))
    detail = (f"{len(kept)} decision record(s) from memory project {memory_project}, "
              f"{total} bytes, best match first. The memory search returns at most the "
              f"{SEARCH_RESULT_LIMIT} best matches for the request.")
    if dropped:
        detail += (f" {len(dropped)} lower-ranked record(s) were left out to stay within "
                   f"{BUDGET_BYTES} bytes: {', '.join(d['natural_key'] for d in dropped)}.")
    if skipped:
        detail += (f" {len(skipped)} record(s) could not be read and were skipped: "
                   + "; ".join(f"{s['natural_key']} ({s['why']})" for s in skipped) + ".")
    text = None
    if kept:
        text = ("Architecture decision records from this project's memory. " + detail
                + "\n\n" + "\n".join(t for _entry, t in kept))
    result = "given" if kept else "none-fitted" if dropped else "none-readable"
    return {**base, "outcome": result, "detail": detail, "records": [e for e, _t in kept],
            "dropped": dropped, "skipped": skipped, "text": text}


def record(store: Any, correlation_id: str, result: dict[str, Any]) -> None:
    store._record_event(
        correlation_id=correlation_id, stage_label=STAGE, status="recorded",
        actor_identity="planning-driver", details_json=json.dumps({RECEIPT_KEY: result}),
    )


def recorded(store: Any, correlation_id: str) -> dict[str, Any] | None:
    """The run's recorded outcome, or ``None`` when its door predates this step.

    A row that cannot be read back is reported as such rather than failing the
    run: these records are evidence for the writers, never a binding document.
    """
    latest = None
    for event in store.list_events(correlation_id):
        if event["stage_label"] == STAGE and event["status"] == "recorded":
            latest = event
    if latest is None:
        return None
    try:
        result = json.loads(latest["details_json"] or "{}")[RECEIPT_KEY]
        if not isinstance(result, dict) or not isinstance(result.get("text"), (str, type(None))):
            raise ValueError("malformed")
        return result
    except (ValueError, KeyError, TypeError):
        return {"outcome": "unreadable", "text": None, "records": [], "dropped": [],
                "detail": "The decision records recorded for this run could not be read "
                          "back, so none were sent."}


def receipt(result: dict[str, Any]) -> dict[str, Any]:
    """What a writer's receipt says it was sent: the outcome without the text."""
    return {"status": "sent" if result.get("text") else "none sent",
            **{key: value for key, value in result.items() if key != "text"}}
