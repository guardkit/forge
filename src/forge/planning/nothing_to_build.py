"""The "already done, nothing to build" outcome of the plan leg (7 October 2026).

The planner may answer that every part of a request is already in the
repository, with its proof: for each part of the request, the code it is in;
for each approved example, an existing test that checks it. Ordinary code in
the planner decides whether that answer is allowed; this module is Forge's
half, and it holds only ordinary code, with no repository read and no model:

* :func:`read_claim` recognises the answer in the plan writer's reply and
  reads its proof out of ``validation.json``;
* :func:`proof_lines`, :func:`owner_message` and :func:`terminal_details` say
  what happened, in plain words, once, the same way everywhere it is read;
* :func:`queue_reason` is the reason the queue row closes with.

The run ends in the existing success state, PLANNED_HANDOFF, with the marker in
the transition's ``details_json`` and the stage label :data:`STAGE_LABEL`. It
never writes a ``feature-plan`` event, so the plan leg's re-drive shortcut can
never mistake it for a committed plan.

Design of record: ai-transition ``docs/designs/already-done-outcome-2026-10-07.md``
(the accepted design's simple option), Part 2 section 4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

__all__ = [
    "OUTCOME",
    "OUTCOME_KEY",
    "PROOF_KEY",
    "QUEUE_REASON_PREFIX",
    "REQUEST_FIELD",
    "STAGE_LABEL",
    "Claim",
    "owner_message",
    "proof_lines",
    "queue_reason",
    "read_claim",
    "terminal_details",
]

#: The field Forge adds to the plan writer's call when its switch is on
#: (``planning.nothing_to_build.enabled``). Absent, the call is today's.
REQUEST_FIELD = "already_done_allowed"

#: The ``outcome`` value in the plan writer's ``validation.json``, on the
#: PLANNED_HANDOFF transition's details and on ``pipeline.planning-complete``.
OUTCOME = "nothing_to_build"

#: Where the marker sits, on the transition's details and on the event.
OUTCOME_KEY = "outcome"

#: The proof lines, on the transition's details and on the event.
PROOF_KEY = "proof"

#: The durable stage label of the PLANNED_HANDOFF transition this outcome
#: writes. Never ``feature-plan``: that label means a committed plan.
STAGE_LABEL = "nothing-to-build"

#: How the queue row's closing reason starts, and how the queue says it.
QUEUE_REASON_PREFIX = "already done, nothing to build"

#: ``path:line`` or ``path:first-last``, split at the LAST colon (the plan
#: writer's own rule), so a path may itself hold a colon.

@dataclass(frozen=True)
class Claim:
    """The plan writer's "nothing to build" answer, as its proof says it.

    ``parts`` are ``(quote, citations)`` pairs: words copied from the request
    and the code lines that already do them. ``scenarios`` are
    ``(title, citations)`` pairs: an approved example's title and the
    existing test that checks it. A citation is ``path:line`` or
    ``path:first-last``, split at the last colon.
    """

    parts: tuple[tuple[str, tuple[str, ...]], ...]
    scenarios: tuple[tuple[str, tuple[str, ...]], ...]

    def citations(self) -> Iterator[str]:
        for _, cited in self.parts + self.scenarios:
            yield from cited


def _validation_of(role_output: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = role_output.get("validation.json")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _is_feature_plan_file(path: str) -> bool:
    """A feature YAML or a task document: what a plan to build carries."""
    name = path.rsplit("/", 1)[-1]
    return path.endswith((".yaml", ".yml")) or (
        name.startswith("TASK-") and name.endswith(".md")
    )


def _pairs(
    entries: Any, label: str, what: str
) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], str | None]:
    if not isinstance(entries, list) or not entries:
        return (), f"it listed no {what}"
    pairs: list[tuple[str, tuple[str, ...]]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            return (), f"one of its {what} is not a list entry"
        words = entry.get(label)
        cited = entry.get("citations")
        if not isinstance(words, str) or not words.strip():
            return (), f"one of its {what} has no {label}"
        if (
            not isinstance(cited, list)
            or not cited
            or any(not isinstance(c, str) or not c.strip() for c in cited)
        ):
            return (), f'"{words.strip()}" cites no line of code'
        pairs.append((words.strip(), tuple(c.strip() for c in cited)))
    return tuple(pairs), None


def read_claim(
    role_output: Mapping[str, Any], files: Mapping[str, str]
) -> tuple[Claim | None, str | None]:
    """Recognise a "nothing to build" answer in the plan writer's reply.

    Returns ``(None, None)`` when the reply is not one (its ``validation.json``
    carries no ``outcome: nothing_to_build``): the plan leg goes on exactly as
    today. Returns ``(claim, None)`` for a well-formed answer, and
    ``(None, why)`` when the reply says nothing to build but cannot be read
    as one: it also carries a feature plan, it was not accepted, or its proof
    is missing or malformed. ``why`` is plain words.
    """
    validation = _validation_of(role_output)
    if validation is None or validation.get(OUTCOME_KEY) != OUTCOME:
        return None, None
    if validation.get("accepted") is not True:
        return None, "its own checks did not accept the answer"
    planned = sorted(path for path in files if _is_feature_plan_file(path))
    if planned:
        return None, "it also sent a plan to build (" + ", ".join(planned) + ")"
    parts, why = _pairs(validation.get("parts"), "quote", "parts of the request")
    if why is not None:
        return None, why
    scenarios, why = _pairs(validation.get("scenarios"), "title", "approved examples")
    if why is not None:
        return None, why
    return Claim(parts=parts, scenarios=scenarios), None



def proof_lines(claim: Claim) -> list[str]:
    """One plain line per part and per approved example, in the claim's order."""
    lines = [
        f'"{quote}" is already done: {", ".join(cited)}'
        for quote, cited in claim.parts
    ]
    lines += [
        f'"{title}" is checked by an existing test: {", ".join(cited)}'
        for title, cited in claim.scenarios
    ]
    return lines


def _repository_name(target_repo: str) -> str:
    return (target_repo or "").rstrip("/").rsplit("/", 1)[-1] or "the repository"


def owner_message(request_text: str, target_repo: str, claim: Claim) -> str:
    """The one message the owner is sent: the outcome, then every proof line."""
    request = " ".join((request_text or "").split()) or "The request"
    lines = [
        f'Already done, nothing to build. "{request}" is already in '
        f"{_repository_name(target_repo)}:"
    ]
    lines += [f"- {line}" for line in proof_lines(claim)]
    lines.append(
        "Nothing was built or merged. If something is missing, send the "
        "sentence again and say what."
    )
    return "\n".join(lines)


def terminal_details(claim: Claim, message: str) -> dict[str, Any]:
    """What the PLANNED_HANDOFF transition's ``details_json`` carries."""
    return {
        OUTCOME_KEY: OUTCOME,
        PROOF_KEY: proof_lines(claim),
        "owner_message": message,
    }


def queue_reason(details: Mapping[str, Any] | None) -> str | None:
    """The queue row's closing reason for a run that ended nothing to build.

    ``None`` when the details do not carry the marker: the row closes as it
    always has.
    """
    if not isinstance(details, Mapping) or details.get(OUTCOME_KEY) != OUTCOME:
        return None
    proof = details.get(PROOF_KEY)
    first = (
        proof[0].strip()
        if isinstance(proof, list) and proof and isinstance(proof[0], str)
        else ""
    )
    return f"{QUEUE_REASON_PREFIX}: {first}" if first else QUEUE_REASON_PREFIX
