"""Boot-time migration runner for the Forge SQLite substrate.

Public surface
==============

- :func:`apply_at_boot` — apply every migration whose version exceeds
  the highest row currently in ``schema_version``, all of them or none
  of them.
- :func:`observed_schema_version` — read, and only read, the version a
  database is actually at. Applies nothing, writes nothing, and works on
  a read-only connection.
- :func:`pending_migrations` — which migrations a database at a given
  version still has to apply.
- :func:`target_schema_version` — the version this release ships.

Design notes (DDR-003 + TASK-PSM-002)
-------------------------------------

The schema is shipped as real ``schema*.sql`` files inside this
package — see :mod:`importlib.resources`. ``CREATE TABLE IF NOT EXISTS``
plus ``INSERT OR IGNORE INTO schema_version`` make each script safe to
re-run, which is what gives us the *idempotent* acceptance criterion:
running ``apply_at_boot`` against an already-migrated database is a
no-op (no extra rows, no schema drift).

Why this runner does not use ``executescript`` (26 September 2026)
-----------------------------------------------------------------

Until today the runner wrapped a loop of
``connection.executescript(sql)`` in ``with connection`` and its
docstring claimed the batch was one transaction. It was not, for two
independent reasons:

* ``executescript`` issues a COMMIT before it runs anything, so the
  Python-level transaction was thrown away on the first migration; and
* the writer connection is opened with ``isolation_level=None``
  (:func:`forge.adapters.sqlite.connect.connect_writer`), so every
  statement autocommitted on its own anyway.

A review on 26 September reproduced the consequence on a throwaway
database: a migration that added a column and then failed left the
column behind, left ``schema_version`` at the old number, and made the
retry fail with ``duplicate column name`` — a database that could not be
migrated again. So the runner now owns the transaction itself:

1. every pending migration file is read and split into single statements
   *before* the database is touched, and a migration that carries its own
   ``BEGIN``/``COMMIT``/``ROLLBACK`` is refused by name;
2. foreign-key enforcement is turned off for the duration (SQLite's own
   table-rebuild recipe requires this, and ``PRAGMA foreign_keys`` is
   silently ignored inside a transaction, so it has to happen here,
   outside it) and restored afterwards;
3. ``BEGIN IMMEDIATE``, then every statement of every pending migration
   in order, then ``PRAGMA foreign_key_check`` — whose rows are actually
   looked at, which the old script's copy of it never was — then
   ``COMMIT``;
4. on any failure, ``ROLLBACK`` and :class:`MigrationError` naming the
   migration and the statement that failed. SQLite's DDL is
   transactional, so a rolled-back ``ALTER TABLE`` leaves no column
   behind and the next boot starts from exactly where the last good boot
   left off.

The batch is therefore all-or-nothing: either every pending migration is
applied and every ``schema_version`` row is written, or the database is
byte-for-byte what it was before.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from typing import Final


# The current schema version. Bumped to 3 in TASK-MP-002 to add
# planning_runs and planning_run_events tables; bumped to 4 in Lane B /
# Phase E1 to widen the planning_runs.state CHECK for the target-terminal
# chain (FEATURE_SPEC / FEATURE_PLAN / BUILD_QUEUED); bumped to 5 in
# TASK-UBS-002-integration to add the additive ``builds.profile`` column
# carrying the ``forge queue --profile`` selection to the daemon; bumped to
# 6 to add the additive ``builds.last_coach_score`` column so the UBS1C coach
# score survives the run for the ``min_coach_score`` budget floor; bumped to 7
# in FEAT-UBS-002 stage 2 (DETECT) to add the additive ``builds.budget_breach``
# column — the serve-daemon observer's HONEST record of a mid-run budget cap
# breach (first-write-wins; never a status change); bumped to 9 in the
# monitored-supervision lane (timeout truth) to add the additive
# ``builds.terminal_class`` column, which tells a MONITOR kill, a BUDGET-cap
# kill, a WALL-CLOCK expiry and a guardkit IN-BAND timeout apart from an
# ordinary broken build — all four of which ``status`` still spells FAILED.
# bumped to 10 in the work-queue lane (Lane B stage one) to add the two new
# ``work_queue`` / ``work_queue_events`` tables — the list of sentences the
# factory has been asked for but has not started yet; bumped to 11 in Part M of
# the rewrite-on-refusal lane to add the additive ``builds.merge_branch``
# column — the branch the merge word merges, written by the conductor for a
# fix journey and empty (meaning ``autobuild/<feature id>``) for every feature
# build; bumped to 12 in the one-true-copy lane (item 1, starting and
# recording) to add the additive ``start_commit`` / ``target_branch`` columns
# to BOTH ``planning_runs`` and ``builds`` — where a piece of work started
# from, and which branch of the remote it is aimed at, decided once. NULL on
# every pre-existing row and read as "not recorded", never as a guess;
# bumped to 13 in the same lane (item 2, the project's own memory) to add the
# additive ``memory_project`` column to BOTH tables — which memory this piece
# of work actually belongs to, read from the project's own declaration at the
# recorded starting commit. NULL on every pre-existing row and read as "not
# recorded", never as "guardkit";
# bumped to 14 (22 September 2026) to add the additive ``launch_settings``
# column to BOTH tables — the NAMES, and only the names, that the project
# itself declared its builds need from the launching process beyond the
# factory's own list. NULL on every pre-existing row and read as "not
# declared", which is not the same fact as an empty list ("the project was read
# and asked for nothing extra");
# bumped to 15 (22 September 2026) to add the new ``publication_records``
# table — one row per build saying who gave the merge word, which branch of
# the remote the work is aimed at, the commit the join was made onto and the
# joined commit it produced, what the checks found, and every step written
# down twice (BEFORE it is done and again after), under a lease and a turn
# number that make a worker which was only paused safe to replace. A build
# with no row reads as "not recorded";
# bumped to 16 (23 September 2026) to add the new ``deployment_targets``
# table — one row per deployment target holding that target's OWN deployment
# counter (up by one on every grant or takeover, by any build), who holds the
# lock and until when, and what is running on it now, by commit and by the
# identity the running thing reported. The build's turn number and the
# target's counter count different things and are deliberately separate: a
# build picked up twice is on turn 3 while a fresh build starts at turn 1, so
# the two cannot be compared across builds.
# Future
# schema bumps should follow the same pattern: append a sibling
# ``schema_v{N}.sql`` and add a ``(N, "schema_v{N}.sql")`` entry to
# ``_MIGRATIONS`` in ascending order. The runner applies every entry whose
# version is greater than the current ``schema_version`` ledger row.
_SCHEMA_VERSION: Final[int] = 16
_MIGRATIONS: Final[tuple[tuple[int, str], ...]] = (
    (1, "schema.sql"),
    (2, "schema_v2.sql"),
    (3, "schema_v3.sql"),
    (4, "schema_v4.sql"),
    (5, "schema_v5.sql"),
    (6, "schema_v6.sql"),
    (7, "schema_v7.sql"),
    # v8 (conductor revival Stage 2) — the additive ``builds.task_id``
    # column: the fix journey's DURABLE subject. The queue has always put
    # the task identifier on the wire for a mode-c build; nothing persisted
    # it, so a fix-journey dispatch had no subject to name and the
    # subprocess dispatcher refused it.
    (8, "schema_v8.sql"),
    # v9 (monitored-supervision, timeout truth) — the additive
    # ``builds.terminal_class`` column: WHICH of the five deaths a FAILED
    # build actually died. ``status`` still reads FAILED for all five; the
    # distinction rides beside it, NULL-able, and an ordinary failure never
    # writes it.
    (9, "schema_v9.sql"),
    # v10 (work queue, Lane B stage one) — the two new tables ``work_queue``
    # and ``work_queue_events``. Purely additive: no existing table, column or
    # index is touched, so a forge that never reads the queue behaves exactly
    # as it does today.
    (10, "schema_v10.sql"),
    # v11 (the merge word merges the branch the build made, Part M) — the
    # additive ``builds.merge_branch`` column. A repair's commits land on the
    # fix journey's own branch, and nothing recorded its name, so the merge
    # word would have merged the original feature's branch instead. NULL-able:
    # a feature build never writes it and every reader falls back to
    # ``autobuild/<feature id>``.
    (11, "schema_v11.sql"),
    # v12 (one true copy, item 1 — starting and recording) — the additive
    # ``start_commit`` and ``target_branch`` columns on ``planning_runs`` and
    # on ``builds``. Before the starting rule nothing recorded what commit a
    # piece of work was cut from; now the remote's default branch is fetched
    # once at the start, the branch is cut from exactly that commit, and these
    # columns hold it. NULL-able: every historical row reads back as "not
    # recorded".
    (12, "schema_v12.sql"),
    # v13 (the project's own memory, item 2) — the additive
    # ``memory_project`` column on ``planning_runs`` and on ``builds``. Every
    # build used to read and write memory under the name "guardkit", because
    # that name came from one setting nothing set; now the project declares its
    # own name, Forge reads it at the recorded starting commit and hands it to
    # the build, and this column is where the name that was read is written
    # down. NULL-able: a historical row, and a build queued by hand with no
    # planning run, read back as "not recorded".
    (13, "schema_v13.sql"),
    # v14 (the settings a project says its builds need) — the additive
    # ``launch_settings`` column on ``planning_runs`` and on ``builds``. The
    # launch list is the factory's own and carries no project tool's setting,
    # so a project declares the NAMES its own builds need in its own
    # ``.guardkit/config.yaml`` and this column holds what was read. NULL-able:
    # "not declared", which is not the same as "declared, and empty".
    (14, "schema_v14.sql"),
    # v15 (the merge word joins onto the remote) — the new
    # ``publication_records`` table. Nothing wrote down what the merge word had
    # already done, so a coordinator that stopped part-way could not tell a
    # finished step from an unfinished one and the only way to find out was to
    # ask the owner for the merge word again. The record says what is about to
    # be done before it is done, and what it produced afterwards; every write
    # to it is conditional on the turn number, so a worker that was replaced
    # cannot change anything. Purely additive: one new table, no existing
    # column touched.
    (15, "schema_v15.sql"),
    # v16 (the deployment lock and the target's counter) — the new
    # ``deployment_targets`` table. The reservation this factory had protects
    # one process only, so it could not say who owns a deployment target
    # across two coordinators or across a restart; and a build's own turn
    # number cannot be compared across builds. This table gives each target a
    # counter of its own, held in the ledger, taken in a transaction, and
    # raised on every grant or takeover by any build. Purely additive.
    (16, "schema_v16.sql"),
)


class MigrationError(RuntimeError):
    """Raised when the boot-time migration fails to apply.

    Wraps the underlying ``sqlite3`` exception so callers can surface a
    domain-flavoured error without needing to know the exact storage
    backend.
    """


def _load_migration_sql(filename: str) -> str:
    """Return the bundled migration SQL text.

    Parameters
    ----------
    filename:
        Resource name relative to the ``forge.lifecycle`` package
        (e.g. ``"schema.sql"``).
    """
    resource = files("forge.lifecycle") / filename
    return resource.read_text(encoding="utf-8")


def _current_version(connection: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or 0 if uninitialised.

    The lookup tolerates the very first boot — ``schema_version`` does
    not exist yet — and reports version 0 so the caller applies every
    migration in order.
    """
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version;"
        ).fetchone()
    except sqlite3.OperationalError:
        # ``schema_version`` does not exist on a brand-new DB.
        return 0
    if row is None:
        return 0
    return int(row[0])


# Statements that would take the transaction away from this runner. A
# migration file is refused outright if it contains one of these, because
# the runner cannot promise all-or-nothing while the file is opening and
# closing transactions of its own.
_TRANSACTION_WORDS: Final[frozenset[str]] = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}
)


def _strip_comments_and_space(text: str) -> str:
    """Return ``text`` with SQL comments and surrounding space removed.

    Used for two decisions only: is what is left after the last semicolon
    an unfinished statement, and what is a statement's first word. Both
    have to ignore comments — several migration files discuss ``BEGIN``
    and ``COMMIT`` in their header prose, and one of them mentions a
    "STARTING COMMIT", none of which is a transaction statement.
    """
    # A COMMENT IS WHITESPACE, NOT NOTHING (Codex's review of 26 September
    # 2026). Removing a comment outright joined the words either side of it,
    # so ``COMMIT/**/TRANSACTION`` read as one word, ``COMMITTRANSACTION``,
    # which is in no list — and SQLite, which reads the comment as a space,
    # committed the batch early. Each comment becomes one space, as SQLite
    # itself treats it.
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
            out.append(" ")
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        out.append(text[i])
        i += 1
    return "".join(out).strip()


def _split_statements(sql: str, filename: str) -> list[str]:
    """Split one migration file into single, complete SQL statements.

    The scan honours single-quoted strings (including ``''`` escapes),
    double-quoted and backtick-quoted and bracket-quoted identifiers,
    ``--`` line comments and ``/* */`` block comments, so a semicolon
    inside any of those is not a statement boundary. A candidate is only
    accepted at a semicolon when :func:`sqlite3.complete_statement` says
    it is complete, which is what keeps a ``CREATE TRIGGER`` body (whose
    ``BEGIN … END`` contains semicolons) in one piece.

    Raises
    ------
    MigrationError
        If anything but comments and whitespace follows the last
        complete statement — an unclosed quote, comment or statement.
    """
    statements: list[str] = []
    start = 0
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        if char in ("'", '"', "`"):
            i += 1
            while i < n:
                if sql[i] == char:
                    if i + 1 < n and sql[i + 1] == char:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if char == "[":
            end = sql.find("]", i + 1)
            i = n if end == -1 else end + 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if char == ";":
            candidate = sql[start : i + 1]
            if sqlite3.complete_statement(candidate):
                statements.append(candidate.strip())
                start = i + 1
            i += 1
            continue
        i += 1

    trailing = _strip_comments_and_space(sql[start:])
    if trailing:
        raise MigrationError(
            f"migration {filename!r} ends with an unfinished statement — "
            "the last statement is missing its semicolon, or a quote or "
            "comment was never closed"
        )
    if not statements:
        raise MigrationError(f"migration {filename!r} contains no statements")
    return statements


def _first_word(statement: str) -> str:
    """Return a statement's first word, upper-cased, comments ignored."""
    words = _strip_comments_and_space(statement).split(None, 1)
    return words[0].strip("(;").upper() if words else ""


def _deny_transaction_control(action: int, *_args: object) -> int:
    """SQLite's authorizer: deny BEGIN/COMMIT/ROLLBACK/END and savepoints.

    Called by SQLite for every operation it is about to perform while a
    migration's statement runs. It sees what the ENGINE parsed, not what a
    text check guessed, so a transaction statement is denied however it was
    spelled — behind a comment, behind a byte-order mark, in any case.
    Everything else is allowed.
    """
    if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _refuse_own_transaction(
    statements: list[str],
    filename: str,
) -> None:
    """Refuse a migration that opens or closes a transaction itself.

    The runner puts one transaction around the whole pending batch. A
    migration that says ``BEGIN`` or ``COMMIT`` inside that would either
    fail or, worse, commit half the batch — which is the very failure
    this runner exists to prevent. The check looks at each statement's
    first word, not at the file's text, so prose that discusses a commit
    is not mistaken for one.
    """
    for statement in statements:
        word = _first_word(statement)
        if word in _TRANSACTION_WORDS:
            raise MigrationError(
                f"migration {filename!r} manages its own transaction "
                f"(it contains a {word} statement). The migration runner "
                "puts one transaction around the whole batch, so a "
                "migration must not open or close one of its own."
            )


def target_schema_version() -> int:
    """Return the schema version this release of Forge ships."""
    return _SCHEMA_VERSION


def observed_schema_version(connection: sqlite3.Connection) -> int:
    """Return the schema version a database is *actually* at.

    Reads and only reads: it applies nothing, writes nothing, and opens
    no transaction, so it is safe on a ``mode=ro`` connection and on a
    copy of a live record. A database with no ``schema_version`` table at
    all reads as 0, meaning "nothing has been applied here yet".

    This exists for the rollout tooling, which has to tell the schema a
    record *is* from the schema a release *wants*: a valid older record
    must not be refused merely for being older than
    :func:`target_schema_version`.
    """
    return _current_version(connection)


def pending_migrations(version: int) -> tuple[tuple[int, str], ...]:
    """Return the migrations a database at ``version`` has still to apply.

    Each entry is ``(version, filename)``, in ascending order. An empty
    tuple means the database is already at — or beyond — the version this
    release ships. Nothing is read or written; the answer is a function
    of the number alone, so the rollout tooling can ask it about a
    version it read from a snapshot minutes earlier.
    """
    return tuple(m for m in _MIGRATIONS if m[0] > version)


def apply_at_boot(connection: sqlite3.Connection) -> int:
    """Apply every pending migration to ``connection``, all or nothing.

    The whole pending batch runs inside one ``BEGIN IMMEDIATE`` …
    ``COMMIT``. Either every pending migration is applied and every
    ``schema_version`` row is written, or — on any failure — the
    transaction is rolled back and the database is exactly what it was
    before. SQLite's DDL is transactional, so a rolled-back
    ``ALTER TABLE`` leaves no column behind and the failed migration can
    be retried once its cause is fixed.

    ``executescript`` is deliberately *not* used: it commits any open
    transaction before it runs, and the writer connection is in
    autocommit mode (``isolation_level=None``), so a loop of
    ``executescript`` calls has no transaction around it at all. Each
    migration file is instead split into single statements and executed
    one at a time. A migration that carries its own ``BEGIN``/``COMMIT``
    is refused by name, because it would take the transaction away from
    this runner.

    The function is **idempotent**: running it twice against the same
    database leaves the schema unchanged, because every DDL statement in
    the bundled scripts uses ``IF NOT EXISTS`` and every
    ``schema_version`` row is written with ``INSERT OR IGNORE``. With
    nothing pending, nothing at all is executed.

    Foreign-key enforcement is switched off for the duration and restored
    afterwards. SQLite's own table-rebuild recipe requires that, and
    ``PRAGMA foreign_keys`` is silently ignored inside a transaction, so
    it can only be done here — outside it. Before the commit,
    ``PRAGMA foreign_key_check`` runs and its rows are *read*: a
    migration that broke a reference is rolled back rather than
    committed.

    Parameters
    ----------
    connection:
        A writable ``sqlite3.Connection`` — typically the persistent
        connection returned by
        :func:`forge.adapters.sqlite.connect.connect_writer`. The runner
        needs to own the transaction, so a connection that already has one
        open makes SQLite refuse the nested ``BEGIN`` and the refusal is
        reported as a :class:`MigrationError` — nothing is applied and the
        caller's own transaction is left alone.

    Returns
    -------
    int
        The schema version after the migrations have been applied
        (i.e. the highest version present in ``schema_version``).

    Raises
    ------
    MigrationError
        If a migration file cannot be split into statements, manages its
        own transaction, or raises a SQLite error. The message names the
        migration and the statement that failed — never the data. Any
        originating exception is preserved as ``__cause__``.
    """
    starting_version = _current_version(connection)

    pending = pending_migrations(starting_version)
    if not pending:
        # Already up to date — re-running the scripts would still be a
        # no-op, but skipping them avoids the cost on every boot, and
        # avoids opening a transaction for nothing.
        return starting_version

    # Read and check every file BEFORE the database is touched, so a
    # malformed or transaction-managing migration is refused without
    # having changed anything at all.
    batch: list[tuple[int, str, list[str]]] = []
    for version, filename in pending:
        sql = _load_migration_sql(filename)
        statements = _split_statements(sql, filename)
        _refuse_own_transaction(statements, filename)
        batch.append((version, filename, statements))

    foreign_keys_were_on = bool(
        connection.execute("PRAGMA foreign_keys;").fetchone()[0]
    )
    if foreign_keys_were_on:
        # Ignored inside a transaction, which is exactly why it is here.
        connection.execute("PRAGMA foreign_keys = OFF;")

    try:
        connection.execute("BEGIN IMMEDIATE;")
        try:
            for _version, filename, statements in batch:
                for statement in statements:
                    try:
                        # THE ENGINE ITSELF REFUSES TRANSACTION CONTROL while a
                        # migration's statement runs (Codex, 26 September 2026:
                        # a byte-order mark before COMMIT slipped past the
                        # textual guard, as a comment had the day before, and
                        # SQLite committed the batch early). The guard above
                        # is the friendly early sentence; this is the wall:
                        # SQLite's own parser decides what is a transaction
                        # statement, and its authorizer denies every one of
                        # them, whatever bytes it was spelled with.
                        connection.set_authorizer(_deny_transaction_control)
                        try:
                            connection.execute(statement)
                        finally:
                            connection.set_authorizer(None)
                    except sqlite3.Error as exc:
                        raise MigrationError(
                            f"failed to apply migration {filename!r} at "
                            f"statement {_first_line(statement)!r}: {exc}"
                        ) from exc
                connection.execute(
                    "INSERT OR IGNORE INTO schema_version (version, applied_at) "
                    "VALUES (?, datetime('now'));",
                    (_version,),
                )
            broken = list(connection.execute("PRAGMA foreign_key_check;").fetchall())
            if broken:
                tables = sorted({str(row[0]) for row in broken})
                raise MigrationError(
                    "migrations left broken references behind, so nothing "
                    f"was applied. Tables with broken references: "
                    f"{', '.join(tables)}"
                )
            connection.execute("COMMIT;")
        except BaseException:
            # Nothing survives a failure: not a column, not a table, not
            # a schema_version row.
            try:
                connection.execute("ROLLBACK;")
            except sqlite3.Error:  # pragma: no cover - rollback of a dead txn
                pass
            raise
    except sqlite3.Error as exc:
        raise MigrationError(f"failed to apply migrations: {exc}") from exc
    finally:
        if foreign_keys_were_on:
            connection.execute("PRAGMA foreign_keys = ON;")

    return _current_version(connection)


def _first_line(statement: str) -> str:
    """Return a one-line, shortened form of ``statement`` for a message.

    Migration files are DDL, so this carries no row data. It is trimmed
    so an error line stays readable.
    """
    collapsed = " ".join(_strip_comments_and_space(statement).split())
    return collapsed if len(collapsed) <= 120 else collapsed[:117] + "..."


__all__ = [
    "MigrationError",
    "apply_at_boot",
    "observed_schema_version",
    "pending_migrations",
    "target_schema_version",
]
