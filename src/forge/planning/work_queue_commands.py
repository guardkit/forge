"""What the forge says back about the queue, and what a queue command does.

Public surface
==============

- :func:`queued_reply` — the one line a filed sentence is answered with.
- :func:`execute_command` — run one queue command against the store and
  return the plain line to post in the thread.

Design notes
------------

Every string in this module is read by a person in Slack, so every string is
an ordinary English sentence: no status codes, no identifiers a person did
not type, no house shorthand. The only symbol is the ``#12`` a person uses to
name a row, because that is what they typed.

Jarvis parses the *shape* of a command and forwards it; nothing here parses
anything. The command arrives as a flat object — ``{"verb": ..., "id": ...,
"after": ..., "sentence": ...}`` — and this module turns it into one store
call and one sentence back.

References
----------
- ``docs/work-queue-spec-2026-09-05.md`` contracts 3 and 5.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from forge.planning.work_queue_store import FiledRow, WorkQueueStore


#: The verbs the forge acts on. Anything else is answered with one line and
#: changes nothing.
COMMAND_VERBS: tuple[str, ...] = (
    "list",
    "add_front",
    "add_before",
    "promote",
    "link",
    "keep",
    "drop",
)

#: Counts up to this many are written as words ("Two ahead of it."); beyond it
#: the digits are easier to read than the words.
_COUNT_WORDS: tuple[str, ...] = (
    "Nothing",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
    "Twenty",
)

#: How a closed row is described when someone asks the forge to move it.
_CLOSED_WORDS: Mapping[str, str] = {
    "DONE": "done",
    "WITHDRAWN": "withdrawn",
    "BLOCKED": "blocked",
}


def _count_phrase(count: int) -> str:
    """"Nothing", "One", "Two", ... — the front of the "ahead of it" sentence."""
    if 0 <= count < len(_COUNT_WORDS):
        return _COUNT_WORDS[count]
    return str(count)


def _label(target_repo: str | None, kind: str) -> str:
    """``(api_test · feature)``, or ``(feature)`` when no repository is named."""
    if target_repo:
        return f"({target_repo} · {kind})"
    return f"({kind})"


def queued_reply(
    *, queue_id: int, target_repo: str | None, kind: str, ahead: int
) -> str:
    """The line a person gets back when their sentence is filed."""
    return (
        f"Queued as #{queue_id} {_label(target_repo, kind)}. "
        f"{_count_phrase(ahead)} ahead of it."
    )


def age_phrase(queued_at: str, now: datetime) -> str:
    """How long a row has been waiting, in words a person reads at a glance."""
    try:
        queued = datetime.fromisoformat(queued_at)
    except ValueError:
        return "queued at an unknown time"
    if queued.tzinfo is None:
        queued = queued.replace(tzinfo=timezone.utc)
    seconds = max(0.0, (now - queued).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "1 minute ago" if minutes == 1 else f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 48:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    days = hours // 24
    return f"{days} days ago"


def list_reply(rows: list[sqlite3.Row], *, now: datetime) -> str:
    """The queue as a person reads it: one row per line, oldest first."""
    if not rows:
        return "Nothing in the queue."
    positions = {int(row["id"]): index + 1 for index, row in enumerate(rows)}
    oldest_first = sorted(rows, key=lambda row: (str(row["queued_at"]), int(row["id"])))
    lines = [
        f"#{int(row['id'])} {_label(row['target_repo'], str(row['kind']))} — "
        f"asked for {age_phrase(str(row['queued_at']), now)}, "
        f"position {positions[int(row['id'])]}"
        for row in oldest_first
    ]
    return "\n".join(lines)


def _no_such_row(queue_id: Any) -> str:
    return f"There is no #{queue_id} in the queue."


def _closed_row(row: sqlite3.Row) -> str:
    word = _CLOSED_WORDS.get(str(row["status"]), str(row["status"]).lower())
    return f"#{int(row['id'])} is not in the queue any more — it is {word}."


def _reply_for_missing(store: WorkQueueStore, queue_id: Any) -> str:
    """One line for an id that cannot be moved: unknown, or already closed."""
    if not isinstance(queue_id, int):
        return _no_such_row(queue_id)
    row = store.get(queue_id)
    if row is None:
        return _no_such_row(queue_id)
    return _closed_row(row)


def execute_command(
    store: WorkQueueStore,
    command: Mapping[str, Any],
    *,
    actor_identity: str,
    correlation_id: str,
    originating_user: str,
    target_repo: str | None = None,
    kind: str = "feature",
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Run one queue command and return the line to post in the thread.

    Parameters
    ----------
    command:
        The flat object jarvis forwarded — ``verb`` plus whichever of ``id``,
        ``after`` and ``sentence`` the verb takes.
    actor_identity:
        The Slack identity that sent the command; it is written on every
        events row so a reordering is attributable.
    correlation_id:
        The command's own correlation id — used as the idempotency key when
        the command files a new row (``next:`` / ``before #12:``).
    """
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    verb = str(command.get("verb") or "")
    queue_id = command.get("id")
    if verb not in COMMAND_VERBS:
        return "I do not know that queue command, so I have changed nothing."

    if verb == "list":
        return list_reply(store.list_open(), now=now)

    if verb in ("add_front", "add_before"):
        sentence = str(command.get("sentence") or "").strip()
        if not sentence:
            return "That command had no sentence in it, so I have queued nothing."
        if verb == "add_before":
            if not isinstance(queue_id, int) or store.get(queue_id) is None:
                return _no_such_row(queue_id)
        filed: FiledRow = store.file_sentence(
            correlation_id=correlation_id,
            sentence=sentence,
            originating_user=originating_user,
            target_repo=target_repo,
            kind=kind,
            position="front" if verb == "add_front" else "before",
            before_id=queue_id if verb == "add_before" else None,
            actor_identity=actor_identity,
        )
        return queued_reply(
            queue_id=filed.queue_id,
            target_repo=target_repo,
            kind=kind,
            ahead=filed.ahead,
        )

    if verb == "promote":
        if isinstance(queue_id, int) and store.promote(
            queue_id, actor_identity=actor_identity
        ):
            return f"#{queue_id} is next."
        return _reply_for_missing(store, queue_id)

    if verb == "link":
        after = command.get("after")
        if not isinstance(queue_id, int) or not isinstance(after, int):
            return _no_such_row(queue_id)
        if store.get(after) is None:
            return _no_such_row(after)
        if store.link(queue_id, after, actor_identity=actor_identity):
            return f"#{queue_id} will wait until #{after} is done."
        return _reply_for_missing(store, queue_id)

    if verb == "keep":
        if isinstance(queue_id, int) and store.keep(
            queue_id, actor_identity=actor_identity
        ):
            return f"#{queue_id} stays in the queue."
        return _reply_for_missing(store, queue_id)

    # drop — the row closes WITHDRAWN and is never deleted.
    if isinstance(queue_id, int) and store.drop(
        queue_id, actor_identity=actor_identity
    ):
        return f"#{queue_id} is out of the queue. Nothing was deleted."
    return _reply_for_missing(store, queue_id)


__all__ = [
    "COMMAND_VERBS",
    "age_phrase",
    "execute_command",
    "list_reply",
    "queued_reply",
]
