"""The boot migration batch is all-or-nothing (26 September 2026).

A review on 26 September took the migration runner's control flow, gave it
a connection in the same mode the real writer uses
(``isolation_level=None``) and injected a migration that adds a column and
then fails. The column survived, the recorded version stayed where it was,
and the retry failed with ``duplicate column name`` — a database that could
not be migrated again. The cause was ``executescript``, which commits any
open transaction before it runs anything.

These tests pin the fixed contract:

- the batch runs in one transaction the runner owns, so a failure leaves
  nothing behind — not a column, not a table, not a ``schema_version`` row;
- a retry after a failed migration succeeds (the review's exact case);
- a failure in the *last* of three pending migrations persists none of the
  three;
- a migration that manages its own transaction is refused, by name, in a
  plain sentence, before the database is touched;
- a migration that leaves a broken reference behind is rolled back, not
  committed;
- re-running with nothing pending is still a no-op;
- the read-only ``observed schema version`` and ``pending migrations``
  answers the rollout tooling needs apply nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def real_migrations() -> Iterator[tuple[tuple[int, str], ...]]:
    """Restore the real migration list however a test leaves it."""
    original = migrations._MIGRATIONS
    try:
        yield original
    finally:
        migrations._MIGRATIONS = original


def _writer_at(
    tmp_path: Path,
    name: str,
    *,
    real: tuple[tuple[int, str], ...],
    version: int,
) -> sqlite3.Connection:
    """Open a real writer connection and migrate it to ``version`` exactly."""
    cx = sqlite_connect.connect_writer(tmp_path / name)
    migrations._MIGRATIONS = tuple(m for m in real if m[0] <= version)
    try:
        assert migrations.apply_at_boot(cx) == version
    finally:
        migrations._MIGRATIONS = real
    return cx


def _columns(cx: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in cx.execute(f"PRAGMA table_info({table})")]


def _tables(cx: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _inject(
    monkeypatch: pytest.MonkeyPatch,
    scripts: dict[str, str],
    order: tuple[tuple[int, str], ...],
) -> None:
    """Replace the migration list and loader with in-memory scripts."""
    migrations._MIGRATIONS = order
    monkeypatch.setattr(
        migrations,
        "_load_migration_sql",
        lambda filename: scripts[filename],
    )


# The review's exact injected migration: add a column, then fail, then try to
# record the version. Nothing after the failing statement may survive.
_ADDS_A_COLUMN_THEN_FAILS = """
ALTER TABLE planning_runs ADD COLUMN start_commit TEXT;
SELECT * FROM deliberately_missing_table;
INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (12, datetime('now'));
"""

_ADDS_A_COLUMN = """
ALTER TABLE planning_runs ADD COLUMN start_commit TEXT;
INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (12, datetime('now'));
"""


# ---------------------------------------------------------------------------
# The review's case: a failed migration, then a retry
# ---------------------------------------------------------------------------


def test_failed_migration_leaves_no_column_and_no_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """A rolled-back ALTER TABLE leaves no column and no version row."""
    cx = _writer_at(tmp_path, "review.db", real=real_migrations, version=11)
    try:
        before = _columns(cx, "planning_runs")
        assert "start_commit" not in before

        _inject(
            monkeypatch,
            {"fixture.sql": _ADDS_A_COLUMN_THEN_FAILS},
            ((12, "fixture.sql"),),
        )
        with pytest.raises(migrations.MigrationError) as first:
            migrations.apply_at_boot(cx)

        assert "fixture.sql" in str(first.value)
        assert "deliberately_missing_table" in str(first.value)
        # Nothing survived the failure.
        assert _columns(cx, "planning_runs") == before
        assert migrations.observed_schema_version(cx) == 11
        assert cx.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=12"
        ).fetchone()[0] == 0
        # And no transaction was left open on the writer.
        assert cx.in_transaction is False
    finally:
        cx.close()


def test_retry_of_the_same_broken_migration_fails_the_same_way(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """The second attempt fails on the real cause, not on a leftover column."""
    cx = _writer_at(tmp_path, "twice.db", real=real_migrations, version=11)
    try:
        _inject(
            monkeypatch,
            {"fixture.sql": _ADDS_A_COLUMN_THEN_FAILS},
            ((12, "fixture.sql"),),
        )
        messages = []
        for _attempt in range(2):
            with pytest.raises(migrations.MigrationError) as raised:
                migrations.apply_at_boot(cx)
            messages.append(str(raised.value))

        assert messages[0] == messages[1]
        assert "duplicate column name" not in messages[1]
        assert migrations.observed_schema_version(cx) == 11
    finally:
        cx.close()


def test_retry_succeeds_once_the_migration_is_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """The review's case: after a failure the database can still be migrated."""
    cx = _writer_at(tmp_path, "retry.db", real=real_migrations, version=11)
    try:
        scripts = {"fixture.sql": _ADDS_A_COLUMN_THEN_FAILS}
        _inject(monkeypatch, scripts, ((12, "fixture.sql"),))
        with pytest.raises(migrations.MigrationError):
            migrations.apply_at_boot(cx)

        # The cause is fixed; the same migration is applied again.
        scripts["fixture.sql"] = _ADDS_A_COLUMN
        assert migrations.apply_at_boot(cx) == 12
        assert "start_commit" in _columns(cx, "planning_runs")
        assert migrations.observed_schema_version(cx) == 12
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# A partial batch never persists
# ---------------------------------------------------------------------------


def test_a_failure_in_the_last_of_three_persists_none_of_the_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """Three pending migrations, the third fails: none of the three persists."""
    cx = _writer_at(tmp_path, "batch.db", real=real_migrations, version=11)
    try:
        tables_before = _tables(cx)
        scripts = {
            "one.sql": (
                "CREATE TABLE IF NOT EXISTS first_new_table (id TEXT PRIMARY KEY);\n"
                "INSERT OR IGNORE INTO schema_version (version, applied_at) "
                "VALUES (12, datetime('now'));\n"
            ),
            "two.sql": (
                "ALTER TABLE planning_runs ADD COLUMN target_branch TEXT;\n"
                "INSERT OR IGNORE INTO schema_version (version, applied_at) "
                "VALUES (13, datetime('now'));\n"
            ),
            "three.sql": (
                "CREATE TABLE IF NOT EXISTS third_new_table (id TEXT PRIMARY KEY);\n"
                "INSERT INTO third_new_table (id) VALUES ('a'), ('a');\n"
                "INSERT OR IGNORE INTO schema_version (version, applied_at) "
                "VALUES (14, datetime('now'));\n"
            ),
        }
        _inject(
            monkeypatch,
            scripts,
            ((12, "one.sql"), (13, "two.sql"), (14, "three.sql")),
        )

        with pytest.raises(migrations.MigrationError) as raised:
            migrations.apply_at_boot(cx)
        assert "three.sql" in str(raised.value)

        assert _tables(cx) == tables_before
        assert "first_new_table" not in _tables(cx)
        assert "third_new_table" not in _tables(cx)
        assert "target_branch" not in _columns(cx, "planning_runs")
        assert migrations.observed_schema_version(cx) == 11
        assert cx.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version > 11"
        ).fetchone()[0] == 0
    finally:
        cx.close()


def test_a_whole_batch_of_three_is_applied_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """The success side of the same batch: all three, and all three rows."""
    cx = _writer_at(tmp_path, "batch-ok.db", real=real_migrations, version=11)
    try:
        scripts = {
            "one.sql": "CREATE TABLE first_new_table (id TEXT PRIMARY KEY);\n",
            "two.sql": "ALTER TABLE planning_runs ADD COLUMN target_branch TEXT;\n",
            "three.sql": "CREATE TABLE third_new_table (id TEXT PRIMARY KEY);\n",
        }
        _inject(
            monkeypatch,
            scripts,
            ((12, "one.sql"), (13, "two.sql"), (14, "three.sql")),
        )
        assert migrations.apply_at_boot(cx) == 14
        assert {"first_new_table", "third_new_table"} <= _tables(cx)
        assert "target_branch" in _columns(cx, "planning_runs")
        # The runner writes the version row for every migration it applies,
        # so the batch's atomicity does not depend on each file remembering.
        assert [
            row[0]
            for row in cx.execute(
                "SELECT version FROM schema_version WHERE version > 11 ORDER BY version"
            )
        ] == [12, 13, 14]
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# A migration may not manage its own transaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word, script",
    [
        ("BEGIN", "BEGIN;\nCREATE TABLE t (id TEXT);\nCOMMIT;\n"),
        ("COMMIT", "CREATE TABLE t (id TEXT);\nCOMMIT;\n"),
        ("ROLLBACK", "CREATE TABLE t (id TEXT);\nROLLBACK;\n"),
        # Codex's review of 26 September 2026: a comment between the two words
        # used to be removed outright, so the guard saw COMMITTRANSACTION (in
        # no list) while SQLite, reading the comment as a space, committed the
        # batch early. A comment is whitespace.
        ("COMMIT", "CREATE TABLE t (id TEXT);\nCOMMIT/**/TRANSACTION;\n"),
        ("COMMIT", "CREATE TABLE t (id TEXT);\nCOMMIT -- said quietly\nTRANSACTION;\n"),
        ("BEGIN", "/* opening */BEGIN/* now */;\nCREATE TABLE t (id TEXT);\n"),
        ("END", "CREATE TABLE t (id TEXT);\nEND/**/TRANSACTION;\n"),
        # Codex's re-review of 26 September 2026: a byte-order mark before
        # the word is invisible to a text split but not to SQLite, which
        # committed the batch early. The engine's own authorizer now denies
        # transaction control however it is spelled; the message names the
        # migration and the statement that was denied.
        ("COMMIT", "CREATE TABLE t (id TEXT);\n\ufeffCOMMIT;\n"),
        ("END", "CREATE TABLE t (id TEXT);\n\ufeffEND TRANSACTION;\n"),
        ("COMMIT", "CREATE TABLE t (id TEXT);\n\u200bCOMMIT;\n"),
    ],
)
def test_a_migration_with_its_own_transaction_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
    word: str,
    script: str,
) -> None:
    """Refused by name, in a plain sentence, before the database is touched."""
    cx = _writer_at(tmp_path, f"refuse-{word}.db", real=real_migrations, version=11)
    try:
        tables_before = _tables(cx)
        _inject(monkeypatch, {"own-transaction.sql": script}, ((12, "own-transaction.sql"),))

        with pytest.raises(migrations.MigrationError) as raised:
            migrations.apply_at_boot(cx)

        message = str(raised.value)
        assert "own-transaction.sql" in message
        # Either the friendly textual guard fired (plain spellings) or the
        # engine's authorizer denied it (a spelling only SQLite could read);
        # both name the migration and leave nothing behind.
        # ... or SQLite refused the spelling outright (a zero-width space is a
        # syntax error to it). Any of the three is a refusal with nothing kept.
        assert (
            "manages its own transaction" in message
            or "not authorized" in message
            or "syntax error" in message
        ), message
        assert _tables(cx) == tables_before
        assert migrations.observed_schema_version(cx) == 11
    finally:
        cx.close()


def test_prose_about_a_commit_is_not_a_commit(
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """Every shipped migration passes the refusal — comments included.

    ``schema_v13.sql`` talks about a "STARTING COMMIT" in its header and
    ``schema_v4.sql`` explains the transaction the runner now owns, so the
    check has to read statements, not the file's text.
    """
    for _version, filename in real_migrations:
        sql = migrations._load_migration_sql(filename)
        statements = migrations._split_statements(sql, filename)
        migrations._refuse_own_transaction(statements, filename)
        assert statements, filename

    thirteenth = migrations._load_migration_sql("schema_v13.sql")
    assert "COMMIT" in thirteenth  # the prose really is there


def test_an_unfinished_statement_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    cx = _writer_at(tmp_path, "unfinished.db", real=real_migrations, version=11)
    try:
        _inject(
            monkeypatch,
            {"unfinished.sql": "CREATE TABLE t (id TEXT);\nCREATE TABLE u (id TEXT)\n"},
            ((12, "unfinished.sql"),),
        )
        with pytest.raises(migrations.MigrationError) as raised:
            migrations.apply_at_boot(cx)
        assert "unfinished statement" in str(raised.value)
        assert "u" not in _tables(cx) and "t" not in _tables(cx)
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Splitting honours comments and string literals
# ---------------------------------------------------------------------------


def test_semicolons_inside_comments_and_strings_are_not_boundaries() -> None:
    sql = (
        "-- a comment with a ; in it\n"
        "/* and a block comment; with another */\n"
        "CREATE TABLE t (id TEXT DEFAULT 'a;b', \"odd;name\" TEXT);\n"
        "INSERT INTO t (id) VALUES ('it''s; fine');\n"
        "-- trailing comment after the last statement\n"
    )
    statements = migrations._split_statements(sql, "fixture.sql")
    assert len(statements) == 2
    assert statements[0].endswith(");")
    assert statements[1].startswith("INSERT")


def test_a_trigger_body_stays_in_one_piece() -> None:
    sql = (
        "CREATE TRIGGER t AFTER INSERT ON x BEGIN\n"
        "  INSERT INTO y (id) VALUES (new.id);\n"
        "  UPDATE z SET n = n + 1;\n"
        "END;\n"
    )
    statements = migrations._split_statements(sql, "trigger.sql")
    assert len(statements) == 1
    assert statements[0].strip().endswith("END;")


# ---------------------------------------------------------------------------
# References are checked before the commit, not after
# ---------------------------------------------------------------------------


def test_a_migration_that_breaks_a_reference_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """Foreign-key enforcement is off during the batch, so the check matters."""
    cx = _writer_at(tmp_path, "broken-ref.db", real=real_migrations, version=11)
    try:
        _inject(
            monkeypatch,
            {
                "orphan.sql": (
                    "INSERT INTO planning_run_events "
                    "(correlation_id, stage_label, status, recorded_at) "
                    "VALUES ('no-such-run', 's', 'x', '2026-09-26T00:00:00Z');\n"
                )
            },
            ((12, "orphan.sql"),),
        )
        with pytest.raises(migrations.MigrationError) as raised:
            migrations.apply_at_boot(cx)
        assert "broken references" in str(raised.value)
        assert "planning_run_events" in str(raised.value)

        assert cx.execute("SELECT COUNT(*) FROM planning_run_events").fetchone()[0] == 0
        assert migrations.observed_schema_version(cx) == 11
        # Enforcement is back on for the caller.
        assert cx.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        cx.close()


def test_foreign_key_enforcement_is_restored_after_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    cx = _writer_at(tmp_path, "fk-restore.db", real=real_migrations, version=11)
    try:
        _inject(
            monkeypatch,
            {"fixture.sql": _ADDS_A_COLUMN_THEN_FAILS},
            ((12, "fixture.sql"),),
        )
        with pytest.raises(migrations.MigrationError):
            migrations.apply_at_boot(cx)
        assert cx.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            cx.execute(
                "INSERT INTO planning_run_events "
                "(correlation_id, stage_label, status, recorded_at) "
                "VALUES ('ghost', 's', 'x', '2026-09-26T00:00:00Z')"
            )
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Idempotency, and the read-only answers the rollout tooling needs
# ---------------------------------------------------------------------------


def test_re_running_the_real_chain_changes_nothing(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "idem.db")
    try:
        first = migrations.apply_at_boot(cx)
        rows = cx.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        schema = cx.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()

        assert migrations.apply_at_boot(cx) == first
        assert (
            cx.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
            == rows
        )
        assert (
            cx.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            == schema
        )
        assert first == migrations.target_schema_version()
    finally:
        cx.close()


def test_observed_schema_version_applies_nothing(
    tmp_path: Path,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """The rollout tooling reads the version without migrating anything."""
    cx = _writer_at(tmp_path, "observed.db", real=real_migrations, version=11)
    try:
        tables_before = _tables(cx)
        assert migrations.observed_schema_version(cx) == 11
        assert migrations.observed_schema_version(cx) == 11
        assert _tables(cx) == tables_before
        assert cx.in_transaction is False
    finally:
        cx.close()

    reader = sqlite_connect.read_only_connect(tmp_path / "observed.db")
    try:
        assert migrations.observed_schema_version(reader) == 11
    finally:
        reader.close()


def test_observed_schema_version_of_a_database_with_no_ledger(tmp_path: Path) -> None:
    cx = sqlite_connect.connect_writer(tmp_path / "empty.db")
    try:
        assert migrations.observed_schema_version(cx) == 0
        assert _tables(cx) == set()
    finally:
        cx.close()


def test_pending_migrations_for_a_given_version(
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    target = migrations.target_schema_version()

    assert migrations.pending_migrations(target) == ()
    assert migrations.pending_migrations(target + 5) == ()
    assert migrations.pending_migrations(0) == real_migrations

    from_eleven = migrations.pending_migrations(11)
    assert [version for version, _name in from_eleven] == list(range(12, target + 1))
    assert all(name.endswith(".sql") for _version, name in from_eleven)


def test_nothing_pending_means_nothing_is_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """An up-to-date database is not even opened for writing."""
    cx = sqlite_connect.connect_writer(tmp_path / "uptodate.db")
    try:
        version = migrations.apply_at_boot(cx)

        def _refuse_to_load(filename: str) -> str:  # pragma: no cover - must not run
            raise AssertionError(f"loaded {filename} with nothing pending")

        monkeypatch.setattr(migrations, "_load_migration_sql", _refuse_to_load)
        assert migrations.apply_at_boot(cx) == version
    finally:
        cx.close()


def test_a_bom_before_ordinary_ddl_is_still_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """The authorizer denies transaction control only: a byte-order mark
    before an ordinary CREATE TABLE is a valid statement to SQLite and is
    applied — Codex's control case."""
    cx = _writer_at(tmp_path, "bom-ddl.db", real=real_migrations, version=11)
    try:
        _inject(monkeypatch, {"bom.sql": "\ufeffCREATE TABLE bom_ok (id TEXT);\n"}, ((12, "bom.sql"),))
        assert migrations.apply_at_boot(cx) == 12
        assert "bom_ok" in _tables(cx)
    finally:
        cx.close()


def test_an_earlier_migration_does_not_survive_a_bom_commit_then_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_migrations: tuple[tuple[int, str], ...],
) -> None:
    """Codex's exact reproduction: migration 12 is good, 13 ends with a
    byte-order-marked COMMIT and 14 fails. Before the authorizer, 12 stayed
    committed; now nothing does."""
    cx = _writer_at(tmp_path, "bom-batch.db", real=real_migrations, version=11)
    try:
        tables_before = _tables(cx)
        _inject(
            monkeypatch,
            {
                "good.sql": "CREATE TABLE good_one (id TEXT);\n",
                "bom-commit.sql": "CREATE TABLE sneaky (id TEXT);\n\ufeffCOMMIT;\n",
                "bad.sql": "CREATE TABLE later (id TEXT);\nALTER TABLE no_such_table ADD COLUMN x TEXT;\n",
            },
            ((12, "good.sql"), (13, "bom-commit.sql"), (14, "bad.sql")),
        )
        with pytest.raises(migrations.MigrationError):
            migrations.apply_at_boot(cx)
        assert _tables(cx) == tables_before, "an earlier migration survived"
        assert migrations.observed_schema_version(cx) == 11
    finally:
        cx.close()
