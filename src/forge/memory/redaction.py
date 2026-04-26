"""Credential redaction for entity payloads written to Graphiti.

This module exposes a single pure function — :func:`redact_credentials` —
which scrubs credential-shaped substrings from free-text fields before any
entity is constructed for a Graphiti write (TASK-IC-001).

The function MUST be applied to every ``rationale``,
``operator_rationale``, ``question``, and ``answer`` field at the call
site, before model construction, so that no credential ever lands in the
graph store. Apply at the boundary, not deep inside the model — the
redaction is a *policy*, not a model invariant, and forcing it into a
validator would conflate "the input was valid" with "the operator
remembered to scrub". See TASK-IC-001 §"redact_credentials" notes.

Pattern set and justification
-----------------------------

The patterns below are ordered most-specific-first. Each replacement
substitutes a fixed marker so subsequent passes find nothing to redact;
the function is therefore idempotent (``f(f(x)) == f(x)``).

1. ``github_pat_[A-Za-z0-9_]{82,}`` → ``***REDACTED-GITHUB-TOKEN***``

   GitHub fine-grained personal-access-token prefix. The 82-char floor
   matches the documented minimum suffix length; longer values (the
   suffix is variable on fine-grained tokens) are also caught. Run
   *before* the bearer regex because ``_`` is in this charset but not
   in the bearer charset, so a bearer-pass would otherwise truncate
   the match.

2. ``ghp_[A-Za-z0-9]{36}`` → ``***REDACTED-GITHUB-TOKEN***``

   GitHub classic personal-access-token prefix; suffix is exactly 36
   alphanumerics per GitHub's published format.

3. ``ghs_[A-Za-z0-9]{36}`` → ``***REDACTED-GITHUB-TOKEN***``

   GitHub server-to-server token prefix (used by GitHub Apps); suffix
   is exactly 36 alphanumerics per GitHub's published format.

4. ``Bearer [A-Za-z0-9._\\-]{20,}`` → ``Bearer ***REDACTED***``

   Generic ``Authorization: Bearer <token>`` payloads (RFC 6750). The
   20-char floor avoids false positives on the literal word "Bearer"
   followed by short non-token tokens (e.g. "Bearer Inc."). The
   charset matches opaque tokens plus typical JWT separators (``.``
   for ``header.payload.sig``) and base64url ``-_``.

5. ``\\b[0-9a-fA-F]{40,}\\b`` → ``***REDACTED-HEX***``

   Long hex strings — covers SHA1/SHA256 fingerprints, AWS-style
   hex secrets, and high-entropy hex API keys. The 40-char floor
   matches the SHA1 hex length so we don't redact short hex snippets
   like 8-char hex error codes or short commit SHAs. Word boundaries
   (``\\b``) prevent matching a hex prefix inside a longer
   alphanumeric word.

Purity contract
---------------

* No I/O. No logging. The original ``text`` is **never** retained,
  printed, or written anywhere — neither in success nor in error paths.
* No mutation of inputs (Python ``str`` is immutable, so this falls
  out of the type, but the same applies if the contract is ever
  widened).
* No randomness. Same input always yields the same output, so
  redacted outputs are safe to use in deterministic test fixtures.
* Idempotent: ``redact_credentials(redact_credentials(s)) ==
  redact_credentials(s)`` for every ``s``.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

#: GitHub fine-grained PAT (must run before the bearer regex; see module
#: docstring §1).
_GITHUB_FINE_GRAINED_RE = re.compile(r"github_pat_[A-Za-z0-9_]{82,}")

#: GitHub classic PAT.
_GITHUB_CLASSIC_RE = re.compile(r"ghp_[A-Za-z0-9]{36}")

#: GitHub server-to-server token (GitHub App installations).
_GITHUB_SERVER_RE = re.compile(r"ghs_[A-Za-z0-9]{36}")

#: Generic ``Authorization: Bearer <token>`` payloads (RFC 6750).
_BEARER_RE = re.compile(r"Bearer [A-Za-z0-9._\-]{20,}")

#: Long hex strings — SHA1/SHA256/AWS hex secrets. Word-bounded so a
#: hex prefix inside a longer alphanumeric blob (e.g. left-over after a
#: GitHub redaction) is *not* matched.
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{40,}\b")

# ---------------------------------------------------------------------------
# Replacement markers
# ---------------------------------------------------------------------------

_GITHUB_REDACTION = "***REDACTED-GITHUB-TOKEN***"
_BEARER_REDACTION = "Bearer ***REDACTED***"
_HEX_REDACTION = "***REDACTED-HEX***"


def redact_credentials(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced.

    See the module docstring for the full pattern set and justifications.
    The function is pure: no logging, no I/O, no original-value retention.

    Args:
        text: The input text to scrub. Unicode is supported; only ASCII
            credential shapes are matched (all documented patterns above
            are ASCII), so non-ASCII characters around a match are
            preserved verbatim.

    Returns:
        The scrubbed string. If no pattern matches, the original string
        is returned unchanged (but note Python may still allocate a new
        object — callers must not rely on identity).

    Raises:
        TypeError: ``text`` is not a string.
    """
    if not isinstance(text, str):
        raise TypeError(
            "redact_credentials expected str, got " f"{type(text).__name__}"
        )

    # Order matters — see module docstring. Most-specific GitHub prefixes
    # first (so their non-bearer characters survive), then bearer tokens,
    # then long hex secrets last (so previously-redacted markers, which
    # contain no long hex runs, are not double-processed).
    text = _GITHUB_FINE_GRAINED_RE.sub(_GITHUB_REDACTION, text)
    text = _GITHUB_CLASSIC_RE.sub(_GITHUB_REDACTION, text)
    text = _GITHUB_SERVER_RE.sub(_GITHUB_REDACTION, text)
    text = _BEARER_RE.sub(_BEARER_REDACTION, text)
    text = _HEX_RE.sub(_HEX_REDACTION, text)
    return text


__all__ = ["redact_credentials"]
