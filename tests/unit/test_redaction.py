"""Unit tests for ``forge.memory.redaction`` (TASK-IC-001).

Per AC-005 / AC-006 and the Test Requirements section of the task file:

* At least 3 cases per pattern (positive, negative, edge).
* A fuzz test on random hex strings of varying length.
* Overlapping-match coverage — no original credential value may appear in
  the output.
* Unicode coverage — non-ASCII text around a credential is preserved
  verbatim and the credential is still scrubbed.
* The function is pure (no logging of the original) and idempotent.
"""

from __future__ import annotations

import inspect
import random
import string

import pytest

import forge.memory.redaction as redaction_module
from forge.memory.redaction import redact_credentials

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


# A reproducible RNG so failing fuzz cases are easy to reproduce locally.
_FUZZ_SEED = 0xF06_E_1C_001
_HEX_ALPHABET = string.hexdigits  # 0-9 a-f A-F


def _alpha_num(length: int, *, rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(length))


def _alpha_num_underscore(length: int, *, rng: random.Random) -> str:
    return "".join(
        rng.choice(string.ascii_letters + string.digits + "_") for _ in range(length)
    )


def _hex(length: int, *, rng: random.Random) -> str:
    return "".join(rng.choice(_HEX_ALPHABET) for _ in range(length))


# ---------------------------------------------------------------------------
# Bearer token pattern: 3 cases (positive, negative, edge)
# ---------------------------------------------------------------------------


class TestBearerPattern:
    """``Bearer [A-Za-z0-9._-]{20,}`` → ``Bearer ***REDACTED***``."""

    def test_positive_long_bearer_token_is_redacted(self) -> None:
        token = "abcdEFGH1234567890.abcDEF-_"
        text = f"Authorization: Bearer {token}"
        result = redact_credentials(text)
        assert token not in result
        assert "Bearer ***REDACTED***" in result

    def test_negative_short_bearer_word_is_left_alone(self) -> None:
        # 19 chars — below the 20-char floor that distinguishes a real token
        # from the literal word "Bearer" in prose.
        text = "I am a Bearer Inc. customer"
        result = redact_credentials(text)
        assert result == text, "short 'Bearer …' prose must not be redacted"

    def test_edge_jwt_shaped_bearer_is_redacted_without_leaking_segments(self) -> None:
        # JWTs are dot-separated base64url; this confirms ``.`` and ``-_`` are
        # part of the bearer charset.
        jwt = "header123456789ABCDEF.payload-segment.signature_part"
        text = f"sent Bearer {jwt} to upstream"
        result = redact_credentials(text)
        assert jwt not in result
        # The literal segments must not survive in any form.
        for segment in jwt.split("."):
            assert segment not in result, (
                f"JWT segment {segment!r} leaked into redacted output: {result!r}"
            )
        assert "Bearer ***REDACTED***" in result


# ---------------------------------------------------------------------------
# GitHub-token patterns: 3 cases per family (classic, server, fine-grained)
# ---------------------------------------------------------------------------


class TestGithubClassicPattern:
    """``ghp_[A-Za-z0-9]{36}`` → ``***REDACTED-GITHUB-TOKEN***``."""

    def test_positive_classic_pat_is_redacted(self) -> None:
        token = "ghp_" + "A" * 36
        result = redact_credentials(token)
        assert token not in result
        assert result == "***REDACTED-GITHUB-TOKEN***"

    def test_negative_short_ghp_prefix_is_left_alone(self) -> None:
        # 35 chars after prefix — one short of the strict 36-char tail.
        token = "ghp_" + "A" * 35
        result = redact_credentials(token)
        assert result == token

    def test_edge_classic_pat_inside_sentence_keeps_surrounding_text(self) -> None:
        token = "ghp_" + "x" * 36
        text = f"export GH_TOKEN={token} ; gh auth status"
        result = redact_credentials(text)
        assert token not in result
        assert result.startswith("export GH_TOKEN=***REDACTED-GITHUB-TOKEN***")
        assert result.endswith("; gh auth status")


class TestGithubServerPattern:
    """``ghs_[A-Za-z0-9]{36}`` → ``***REDACTED-GITHUB-TOKEN***``."""

    def test_positive_server_pat_is_redacted(self) -> None:
        token = "ghs_" + "B" * 36
        result = redact_credentials(token)
        assert token not in result
        assert result == "***REDACTED-GITHUB-TOKEN***"

    def test_negative_short_ghs_prefix_is_left_alone(self) -> None:
        token = "ghs_" + "B" * 30  # 6 chars short
        result = redact_credentials(token)
        assert result == token

    def test_edge_two_server_tokens_are_both_redacted(self) -> None:
        a = "ghs_" + "1" * 36
        b = "ghs_" + "2" * 36
        text = f"first {a} and second {b} done"
        result = redact_credentials(text)
        assert a not in result
        assert b not in result
        assert result.count("***REDACTED-GITHUB-TOKEN***") == 2


class TestGithubFineGrainedPattern:
    """``github_pat_[A-Za-z0-9_]{82,}`` → ``***REDACTED-GITHUB-TOKEN***``."""

    def test_positive_fine_grained_pat_is_redacted(self) -> None:
        # Documented minimum suffix length is 82.
        token = "github_pat_" + "C" * 82
        result = redact_credentials(token)
        assert token not in result
        assert "***REDACTED-GITHUB-TOKEN***" in result

    def test_negative_too_short_fine_grained_prefix_is_left_alone(self) -> None:
        token = "github_pat_" + "C" * 81  # one char short of the floor
        result = redact_credentials(token)
        assert result == token

    def test_edge_fine_grained_pat_with_underscores_is_fully_redacted(self) -> None:
        # ``_`` is in the fine-grained charset but not the bearer charset, so
        # bearer-pass ordering matters here.
        token = "github_pat_" + ("A_" * 41) + "AB"  # 84 suffix chars
        text = f"token={token}#tail"
        result = redact_credentials(text)
        assert token not in result
        assert "A_" * 41 not in result
        assert "***REDACTED-GITHUB-TOKEN***" in result


# ---------------------------------------------------------------------------
# Long-hex pattern: 3 cases (positive, negative, edge)
# ---------------------------------------------------------------------------


class TestLongHexPattern:
    """``\\b[0-9a-fA-F]{40,}\\b`` → ``***REDACTED-HEX***``."""

    def test_positive_sha1_length_hex_is_redacted(self) -> None:
        sha1 = "a" * 40  # exactly the minimum length
        text = f"signature: {sha1}"
        result = redact_credentials(text)
        assert sha1 not in result
        assert "***REDACTED-HEX***" in result

    def test_negative_short_hex_is_left_alone(self) -> None:
        short_hex = "deadbeef" * 4  # 32 chars — short of the 40-char floor
        text = f"crc={short_hex}"
        result = redact_credentials(text)
        assert result == text

    def test_edge_hex_inside_alphanumeric_word_is_not_matched(self) -> None:
        # ``\b`` must prevent matching when the hex run is part of a longer
        # alphanumeric blob (e.g. "salt-prefix-followed-by-hex" inside
        # a single token).
        embedded = "Z" + "a" * 50 + "Z"  # 52-char hex bracketed by non-hex
        text = f"opaque={embedded} done"
        result = redact_credentials(text)
        assert result == text, (
            "hex inside a longer alphanumeric word must not be redacted "
            f"(violates word-boundary contract): {result!r}"
        )


# ---------------------------------------------------------------------------
# Fuzz on random hex strings of varying length
# ---------------------------------------------------------------------------


class TestHexFuzz:
    """Fuzz coverage on random hex strings — must redact iff length >= 40."""

    def test_random_hex_redacted_when_at_least_40_chars(self) -> None:
        rng = random.Random(_FUZZ_SEED)
        # 40 random lengths in [40, 200], each at or above the floor.
        for _ in range(40):
            length = rng.randint(40, 200)
            blob = _hex(length, rng=rng)
            result = redact_credentials(blob)
            assert blob not in result, (
                f"long hex blob (len={length}) leaked into output"
            )
            assert result == "***REDACTED-HEX***", (
                f"long hex blob must reduce to the redaction marker; "
                f"got {result!r} for length {length}"
            )

    def test_random_hex_left_alone_when_under_40_chars(self) -> None:
        rng = random.Random(_FUZZ_SEED + 1)
        for _ in range(40):
            length = rng.randint(1, 39)
            blob = _hex(length, rng=rng)
            result = redact_credentials(blob)
            assert result == blob, (
                f"short hex blob (len={length}) was incorrectly redacted: {result!r}"
            )

    def test_random_hex_inside_prose_round_trips_around_redaction(self) -> None:
        rng = random.Random(_FUZZ_SEED + 2)
        for _ in range(20):
            length = rng.randint(40, 100)
            blob = _hex(length, rng=rng)
            text = f"sha={blob};status=ok"
            result = redact_credentials(text)
            assert blob not in result
            assert result.startswith("sha=***REDACTED-HEX***")
            assert result.endswith(";status=ok")


# ---------------------------------------------------------------------------
# Overlapping-match coverage — no original cred may survive
# ---------------------------------------------------------------------------


class TestOverlappingMatches:
    """Mixed-pattern inputs must redact every credential, leaking none."""

    def test_bearer_then_long_hex_both_redacted(self) -> None:
        token = "Bearer xyz1234567890abcdef.ghi"
        sha = "f" * 64  # SHA256-length hex
        text = f"{token}\nfingerprint={sha}"
        result = redact_credentials(text)
        # Both originals must be gone.
        assert "xyz1234567890abcdef" not in result
        assert sha not in result
        # Both markers must be present.
        assert "Bearer ***REDACTED***" in result
        assert "***REDACTED-HEX***" in result

    def test_github_pat_then_bearer_no_truncation(self) -> None:
        # The GitHub-fine-grained PAT contains ``_``, which is NOT in the
        # bearer charset. If the bearer pass ran first, it could truncate the
        # PAT around the underscore and leak hex of the residue.
        pat = "github_pat_" + "A" * 100
        bearer = "Bearer " + "B" * 40
        text = f"{pat} {bearer}"
        result = redact_credentials(text)
        assert pat not in result
        assert "B" * 40 not in result
        assert "***REDACTED-GITHUB-TOKEN***" in result
        assert "Bearer ***REDACTED***" in result

    def test_idempotent_double_application(self) -> None:
        # Running the function twice on the same input must produce the same
        # output as running it once. This guards against pattern interactions
        # that could re-process the redaction markers themselves.
        text = (
            "ghp_" + "A" * 36 + " "
            "Bearer abcdef0123456789abcdef "
            + "f" * 50
        )
        once = redact_credentials(text)
        twice = redact_credentials(once)
        assert once == twice

    def test_no_original_credential_value_survives_in_overlapping_input(self) -> None:
        # AC-006 exemplar — overlapping/mixed creds: assert the union of
        # original-value substrings is fully scrubbed.
        creds = [
            "ghp_" + "A" * 36,
            "ghs_" + "B" * 36,
            "github_pat_" + "C" * 90,
            "Bearer " + "D" * 40,
            "e" * 50,  # long hex
        ]
        text = " | ".join(creds) + " | trailer"
        result = redact_credentials(text)
        for original in creds:
            # The "Bearer " prefix is intentionally retained by the bearer
            # rule, so we only assert on the token portion of that one.
            needle = original[len("Bearer ") :] if original.startswith("Bearer ") else original
            assert needle not in result, (
                f"original credential value leaked: {needle!r} in {result!r}"
            )
        assert result.endswith(" | trailer")


# ---------------------------------------------------------------------------
# Unicode coverage
# ---------------------------------------------------------------------------


class TestUnicode:
    """Non-ASCII text around a credential survives intact."""

    def test_unicode_around_bearer_token_is_preserved(self) -> None:
        token = "Bearer " + "A" * 40
        text = f"日本語 {token} عربى"
        result = redact_credentials(text)
        assert "A" * 40 not in result
        assert "日本語" in result
        assert "عربى" in result
        assert "Bearer ***REDACTED***" in result

    def test_unicode_only_input_passes_through_unchanged(self) -> None:
        text = "完全なユニコードのみ — нет учётных данных — 🚀"
        result = redact_credentials(text)
        assert result == text

    def test_emoji_adjacent_to_long_hex_is_preserved(self) -> None:
        sha = "0" * 50
        text = f"🔐{sha}🔐"
        result = redact_credentials(text)
        assert sha not in result
        assert "🔐" in result
        # Both bookend emoji must survive.
        assert result.count("🔐") == 2


# ---------------------------------------------------------------------------
# Purity / API guarantees
# ---------------------------------------------------------------------------


class TestPurityAndApi:
    """Side-effect, type, and module-level invariants."""

    def test_empty_string_round_trips(self) -> None:
        assert redact_credentials("") == ""

    def test_input_without_any_credential_is_unchanged(self) -> None:
        text = "the quick brown fox jumps over the lazy dog"
        assert redact_credentials(text) == text

    def test_non_string_input_raises_typeerror(self) -> None:
        for bad in (None, 123, b"bytes are not str", ["list"], {"dict": 1}):
            with pytest.raises(TypeError):
                redact_credentials(bad)  # type: ignore[arg-type]

    def test_function_does_not_log_original_text(self, caplog: pytest.LogCaptureFixture) -> None:
        # The purity contract forbids the function from logging the input.
        # We assert nothing is emitted at any level during a redaction call.
        token = "ghp_" + "Z" * 36
        with caplog.at_level("DEBUG"):
            redact_credentials(f"secret={token}")
        assert not caplog.records, (
            f"redact_credentials must be silent; emitted: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    def test_module_docstring_documents_each_pattern(self) -> None:
        doc = redaction_module.__doc__ or ""
        # Each documented pattern's marker phrase must appear in the
        # module docstring (AC-005: "Document each pattern's
        # justification in the module docstring").
        for needle in (
            "github_pat_",
            "ghp_",
            "ghs_",
            "Bearer",
            "Hex",  # hex section title — module uses "Long hex strings"
        ):
            assert needle.lower() in doc.lower(), (
                f"module docstring is missing pattern justification for {needle!r}"
            )

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
