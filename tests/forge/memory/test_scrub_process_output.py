"""Unit tests for ``scrub_process_output`` credential scrubber (TASK-SSH-001).

Per AC-004 / AC-005 / AC-006 and the Implementation Notes of the task file:

* DSN patterns (postgresql://, postgres://, with optional +driver dialect).
* password= and PGPASSWORD= patterns (key preserved, value redacted).
* The function is pure (no logging, no I/O) and idempotent.
* No false positives on http(s):// URLs.
* Non-``str`` input raises ``TypeError``.
"""

from __future__ import annotations

import inspect

import pytest

import forge.memory.redaction as redaction_module
from forge.memory.redaction import scrub_process_output


# ---------------------------------------------------------------------------
# PostgreSQL DSN patterns
# ---------------------------------------------------------------------------


class TestPostgreSQLDSNPattern:
    """``postgresql://...`` and ``postgres://...`` DSNs → ``***REDACTED-DSN***``."""

    def test_positive_postgresql_dsn_with_user_pass_is_redacted(self) -> None:
        dsn = "postgresql://user:secret@localhost:5432/mydb"
        result = scrub_process_output(dsn)
        assert "secret" not in result
        assert result == "***REDACTED-DSN***"

    def test_positive_postgres_dsn_with_user_pass_is_redacted(self) -> None:
        # postgres:// is the canonical scheme (postgresql:// is alias).
        dsn = "postgres://admin:hunter2@db.example.com:5432/production"
        result = scrub_process_output(dsn)
        assert "hunter2" not in result
        assert result == "***REDACTED-DSN***"

    def test_positive_dsn_with_driver_suffix_is_redacted(self) -> None:
        # Async DSNs (postgresql+asyncpg://, postgresql+psycopg://, etc.).
        dsn = "postgresql+asyncpg://user:pass@host:5432/db"
        result = scrub_process_output(dsn)
        assert "pass" not in result
        assert result == "***REDACTED-DSN***"

    def test_positive_dsn_with_query_params_is_redacted(self) -> None:
        dsn = "postgresql://user:pass@host:5432/db?sslmode=require&pool_size=10"
        result = scrub_process_output(dsn)
        assert "pass" not in result
        assert result == "***REDACTED-DSN***"

    def test_negative_http_url_is_not_redacted(self) -> None:
        # False positive prevention — http(s):// URLs are not DSNs.
        url = "https://example.com:8080/api/v1/users"
        result = scrub_process_output(url)
        assert result == url

    def test_negative_https_url_with_path_is_not_redacted(self) -> None:
        url = "https://api.github.com/repos/owner/repo/issues?state=open"
        result = scrub_process_output(url)
        assert result == url

    def test_edge_dsn_inside_log_line_keeps_surrounding_text(self) -> None:
        dsn = "postgresql://app:secret@db:5432/prod"
        text = f"Connecting to {dsn} for migration"
        result = scrub_process_output(text)
        assert "secret" not in result
        assert result.startswith("Connecting to ***REDACTED-DSN***")
        assert result.endswith(" for migration")

    def test_edge_dsn_without_password_is_still_redacted(self) -> None:
        # DSN with username but no password (uncommon but valid).
        dsn = "postgresql://readonly@localhost:5432/analytics"
        result = scrub_process_output(dsn)
        assert result == "***REDACTED-DSN***"


# ---------------------------------------------------------------------------
# password= and PGPASSWORD= patterns
# ---------------------------------------------------------------------------


class TestPasswordKeyValuePattern:
    """``password=<value>`` → ``password=***REDACTED-PASSWORD***``."""

    def test_positive_password_equals_is_redacted(self) -> None:
        text = "password=hunter2"
        result = scrub_process_output(text)
        assert "hunter2" not in result
        assert result == "password=***REDACTED-PASSWORD***"

    def test_positive_password_case_insensitive(self) -> None:
        # Key is case-insensitive per AC-003.
        text = "PASSWORD=secret123"
        result = scrub_process_output(text)
        assert "secret123" not in result
        assert result == "PASSWORD=***REDACTED-PASSWORD***"

    def test_positive_password_with_special_chars_is_redacted(self) -> None:
        text = "password=P@ssw0rd!#"
        result = scrub_process_output(text)
        assert "P@ssw0rd!#" not in result
        assert result == "password=***REDACTED-PASSWORD***"

    def test_edge_password_inside_shell_command_keeps_surrounding_text(self) -> None:
        text = "psql -h localhost -U admin password=secret123 -d mydb"
        result = scrub_process_output(text)
        assert "secret123" not in result
        assert (
            "psql -h localhost -U admin password=***REDACTED-PASSWORD*** -d mydb"
            == result
        )


class TestPGPasswordEnvVarPattern:
    """``PGPASSWORD=<value>`` → ``PGPASSWORD=***REDACTED-PASSWORD***``."""

    def test_positive_pgpassword_is_redacted(self) -> None:
        text = "PGPASSWORD=hunter2"
        result = scrub_process_output(text)
        assert "hunter2" not in result
        assert result == "PGPASSWORD=***REDACTED-PASSWORD***"

    def test_positive_pgpassword_in_export_statement(self) -> None:
        text = "export PGPASSWORD=secret123; psql -U user"
        result = scrub_process_output(text)
        assert "secret123" not in result
        assert result == "export PGPASSWORD=***REDACTED-PASSWORD***; psql -U user"

    def test_edge_pgpassword_lowercase_is_not_matched(self) -> None:
        # PGPASSWORD is case-sensitive (env var convention).
        text = "pgpassword=hunter2"
        result = scrub_process_output(text)
        # This should be caught by the password= pattern instead.
        assert result == "pgpassword=***REDACTED-PASSWORD***"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """``scrub_process_output(scrub_process_output(s)) == scrub_process_output(s)``."""

    def test_idempotent_already_redacted_dsn_is_unchanged(self) -> None:
        # Output containing the DSN marker must not be re-redacted.
        text = "Connecting to ***REDACTED-DSN*** for migration"
        once = scrub_process_output(text)
        twice = scrub_process_output(once)
        assert once == twice

    def test_idempotent_already_redacted_password_is_unchanged(self) -> None:
        text = "password=***REDACTED-PASSWORD***"
        once = scrub_process_output(text)
        twice = scrub_process_output(once)
        assert once == twice

    def test_idempotent_mixed_already_redacted_and_new_creds(self) -> None:
        # Mix of already-redacted markers and new credentials.
        text = "***REDACTED-DSN*** password=newsecret"
        once = scrub_process_output(text)
        twice = scrub_process_output(once)
        assert "newsecret" not in once
        assert once == twice


# ---------------------------------------------------------------------------
# Pattern ordering (DSN-first prevents double-redaction)
# ---------------------------------------------------------------------------


class TestPatternOrdering:
    """DSN pattern runs first to consume embedded `password@` before password= pass."""

    def test_ordering_dsn_with_password_in_authority_not_double_redacted(self) -> None:
        # DSN contains `:password@` in the authority section. If password=
        # ran first, the DSN would be partially redacted and leak the host.
        dsn = "postgresql://user:password123@host:5432/db"
        result = scrub_process_output(dsn)
        assert "password123" not in result
        assert "host:5432" not in result
        # Entire DSN replaced by single marker.
        assert result == "***REDACTED-DSN***"

    def test_ordering_dsn_followed_by_password_kv_both_redacted(self) -> None:
        text = "postgresql://u:p@h:5432/d password=hunter2"
        result = scrub_process_output(text)
        assert "***REDACTED-DSN***" in result
        assert "password=***REDACTED-PASSWORD***" in result
        assert "p@h" not in result
        assert "hunter2" not in result


# ---------------------------------------------------------------------------
# Purity and API guarantees
# ---------------------------------------------------------------------------


class TestPurityAndApi:
    """Side-effect, type, and module-level invariants."""

    def test_empty_string_round_trips(self) -> None:
        assert scrub_process_output("") == ""

    def test_input_without_any_credential_is_unchanged(self) -> None:
        text = "Starting deployment to production environment"
        assert scrub_process_output(text) == text

    def test_non_string_input_raises_typeerror(self) -> None:
        for bad in (None, 123, b"bytes are not str", ["list"], {"dict": 1}):
            with pytest.raises(TypeError):
                scrub_process_output(bad)  # type: ignore[arg-type]

    def test_function_does_not_log_original_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The purity contract forbids the function from logging the input.
        dsn = "postgresql://admin:secret@db:5432/prod"
        with caplog.at_level("DEBUG"):
            scrub_process_output(f"Connecting: {dsn}")
        assert not caplog.records, (
            f"scrub_process_output must be silent; emitted: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    def test_function_is_exported_in_module_all(self) -> None:
        # AC-001: exported via __all__.
        assert "scrub_process_output" in redaction_module.__all__


# ---------------------------------------------------------------------------
# Unicode coverage
# ---------------------------------------------------------------------------


class TestUnicode:
    """Non-ASCII text around credentials survives intact."""

    def test_unicode_around_dsn_is_preserved(self) -> None:
        dsn = "postgresql://user:pass@host:5432/db"
        text = f"日本語 {dsn} عربى"
        result = scrub_process_output(text)
        assert "pass" not in result
        assert "日本語" in result
        assert "عربى" in result
        assert "***REDACTED-DSN***" in result

    def test_unicode_only_input_passes_through_unchanged(self) -> None:
        text = "完全なユニコードのみ — нет учётных данных — 🚀"
        result = scrub_process_output(text)
        assert result == text


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestModuleLevelInvariants:
    """Guard against supply-chain creep and verify documentation."""

    def test_module_imports_are_stdlib_only(self) -> None:
        # The function is pure-stdlib (re-only). Guard against silent
        # dependency creep that would broaden the supply-chain surface.
        source = inspect.getsource(redaction_module)
        forbidden = ("requests", "httpx", "boto3", "logging", "asyncio")
        for needle in forbidden:
            # ``logging`` is intentionally forbidden — see purity contract.
            assert f"import {needle}" not in source, (
                f"redaction module must not import {needle!r}"
            )
