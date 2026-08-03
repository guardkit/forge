"""Security and concurrency hardening tests (TASK-IC-012).

This module is the consolidated defence-in-depth test suite for the
surviving high-leverage scenarios identified in
``tasks/design_approved/TASK-IC-012-security-concurrency-hardening.md``:

* ``@security security-working-directory-allowlist``
* ``@security secrets-appearing-in-rationale-text-are-redacted``
* ``@negative negative-disallowed-binary-refused``

Each test class targets one acceptance criterion.

Trimmed 2026-08-03 (Rich's ruling: retire now, wire via factory later):
the split-brain-dedupe, recency-horizon, priors-argv-leak and
supersession-chain-stress classes exercised the dead Graphiti memory
tiers (``forge.memory.writer`` / ``priors`` / ``session_outcome`` /
``supersession``, deleted with this trim). What survives here hardens
the live code: ``forge.memory.redaction`` and the GuardKit ``run()``
adapter. Successor for the memory-tier hardening = the factory-built
fleet-memory PriorsReader lane (queued).

Shipping policy
---------------

This unit ships only tests; no production code changes. If a hardening
test reveals a production bug, the fix lands in the responsible unit
(TASK-IC-001 through TASK-IC-010), not here.

Notes on tooling
----------------

* The task brief mentions ``hypothesis`` as a candidate for the redaction
  fuzz; it is not currently in the project's optional-dependencies, so the
  AC ("Hypothesis OR pytest-style param") is satisfied here with a
  deterministic ``random.Random`` seed plus ``pytest.mark.parametrize``.
  Adding ``hypothesis`` would be a separate dependency-changing PR.
* The disallowed-binary fuzz documents *which* common binaries explicitly
  cannot run inside the worktree. The wrapper does not select the binary
  by name (``_GUARDKIT_BINARY`` is a constant), so the test simulates the
  permissions-layer refusal at the seam — exercising the code path that
  converts ``PermissionError`` into a structured ``permissions_refused``
  warning for any of the 50+ documented binaries.
"""

from __future__ import annotations

import asyncio
import os
import random
import string
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.guardkit import run as run_module
from forge.adapters.guardkit.context_resolver import ResolvedContext
from forge.adapters.guardkit.run import run
from forge.memory.redaction import redact_credentials


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _empty_resolved() -> ResolvedContext:
    """Stand-in for the GuardKit context resolver (tests skip resolution)."""
    return ResolvedContext(flags=[], paths=[], warnings=[])


# ---------------------------------------------------------------------------
# AC-001 — Hypothesis / pytest-param fuzz on ``redact_credentials``
# ---------------------------------------------------------------------------


_FUZZ_SEED = 0xF06_E_1C_012
_FUZZ_ITERATIONS = 1024  # > 1000 per the AC.

_HEX = string.hexdigits  # 0-9 a-f A-F
_ALNUM = string.ascii_letters + string.digits
_ALNUM_UNDERSCORE = _ALNUM + "_"
_BEARER_CHARSET = _ALNUM + "._-"


def _rand_str(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _generate_fuzz_credentials(
    iterations: int = _FUZZ_ITERATIONS,
) -> list[tuple[str, str, str]]:
    """Return ``[(label, original_credential, surrounding_text)]``.

    Five credential families, randomly distributed across ``iterations``
    samples, so each family gets ~200 cases. Surrounding text mixes
    Unicode, punctuation, and other tokens so the redactor is exercised
    at non-trivial offsets.

    Word-boundary discipline
    ------------------------
    Each generated case constitutes a *positive match* per AC-001
    ("positive matches always redacted"). The credential MUST therefore
    be preceded and followed by characters that produce a regex
    ``\\b`` word boundary — otherwise patterns like
    ``\\b[0-9a-fA-F]{40,}\\b`` will (correctly) decline to match because
    the credential is glued to a neighbouring word character. The fuzz
    wraps every credential with whitespace before appending free-form
    noise so the wrapping is always boundary-safe regardless of the
    surrounding noise content.
    """
    rng = random.Random(_FUZZ_SEED)
    samples: list[tuple[str, str, str]] = []
    # Free-form noise added beyond the boundary-safe whitespace wrapper.
    # Includes Unicode (per the redactor's "Unicode preserved verbatim"
    # contract), punctuation, and word-content so we exercise mixed text.
    noise_pool = [
        "operator note: ",
        "  ",
        "(é ü 中文)",  # unicode coverage
        "// ",
        "\n\t",
        "audit-trail#",
        "operator=rich, time=2026-04-26T12:00 ",
        "",  # empty noise — pure whitespace wrapping case
    ]
    for i in range(iterations):
        family = i % 5
        if family == 0:
            # GitHub fine-grained PAT — variable suffix length 82-110.
            suffix_len = rng.randint(82, 110)
            cred = "github_pat_" + _rand_str(rng, _ALNUM_UNDERSCORE, suffix_len)
            label = "github_fine_grained"
        elif family == 1:
            # GitHub classic PAT — exactly 36-char alnum suffix.
            cred = "ghp_" + _rand_str(rng, _ALNUM, 36)
            label = "github_classic"
        elif family == 2:
            # GitHub server-to-server token — exactly 36-char alnum suffix.
            cred = "ghs_" + _rand_str(rng, _ALNUM, 36)
            label = "github_server"
        elif family == 3:
            # Bearer token — variable length 20-120 in the bearer charset.
            tok_len = rng.randint(20, 120)
            cred = "Bearer " + _rand_str(rng, _BEARER_CHARSET, tok_len)
            label = "bearer"
        else:
            # Long hex — variable length 40-128 over hex alphabet.
            hex_len = rng.randint(40, 128)
            cred = _rand_str(rng, _HEX, hex_len)
            label = "long_hex"

        prefix = rng.choice(noise_pool)
        suffix = rng.choice(noise_pool)
        # Whitespace-wrap the credential so the regex word boundaries
        # always trigger on a positive-match input — see docstring.
        text = f"{prefix} {cred} {suffix}"
        samples.append((label, cred, text))
    return samples


_FUZZ_CASES: list[tuple[str, str, str]] = _generate_fuzz_credentials()


class TestRedactionFuzz:
    """AC-001 — 1000+ random credential strings: no original text leaks."""

    def test_fuzz_corpus_has_at_least_one_thousand_cases(self) -> None:
        # Guard the AC's "1000+" floor so a future contributor can't
        # silently shrink the corpus and still pass CI.
        assert len(_FUZZ_CASES) >= 1000, (
            f"AC requires >=1000 fuzz cases, got {len(_FUZZ_CASES)}"
        )

    @pytest.mark.parametrize(
        ("label", "credential", "text"),
        _FUZZ_CASES,
        ids=[f"{label}#{idx}" for idx, (label, _, _) in enumerate(_FUZZ_CASES)],
    )
    def test_fuzz_credential_never_leaks_to_output(
        self, label: str, credential: str, text: str
    ) -> None:
        result = redact_credentials(text)
        assert credential not in result, (
            f"[{label}] redact_credentials leaked the original credential "
            f"in the output: input_len={len(text)} cred_len={len(credential)}"
        )

    @pytest.mark.parametrize(
        ("label", "credential", "text"),
        _FUZZ_CASES[:64],
        ids=[
            f"{label}#{idx}-idemp" for idx, (label, _, _) in enumerate(_FUZZ_CASES[:64])
        ],
    )
    def test_fuzz_redaction_is_idempotent(
        self, label: str, credential: str, text: str
    ) -> None:
        once = redact_credentials(text)
        twice = redact_credentials(once)
        assert once == twice, (
            f"[{label}] redact_credentials(redact_credentials(x)) != "
            "redact_credentials(x); idempotency contract broken"
        )


# ---------------------------------------------------------------------------
# AC-002 — Working-directory traversal attempts are rejected
# ---------------------------------------------------------------------------


class TestWorkingDirTraversal:
    """AC-002 — relative, traversal, absolute-outside, and symlink-escape
    ``repo_path`` values are all rejected by the cwd-allowlist check.

    The ``run()`` boundary check is defence-in-depth atop DeepAgents'
    own enforcement. A test that the seam is *never* reached when the
    cwd is rejected protects against a future refactor that accidentally
    moves the executor before the check.
    """

    @pytest.fixture()
    def allowlist_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "allowed"
        root.mkdir()
        return root

    @pytest.fixture()
    def absolute_outside(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside"
        outside.mkdir()
        return outside

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "relative_path",
        [
            "build",
            "./build",
            "../build",
            "../../etc/passwd",
            "../../../etc/passwd",
            "build/../build",
            "subdir/.././subdir",
        ],
        ids=[
            "bare-relative",
            "dot-slash",
            "single-up",
            "double-up-etc-passwd",
            "triple-up-etc-passwd",
            "self-cancelling-traversal",
            "nested-cancelling-traversal",
        ],
    )
    async def test_relative_or_traversal_repo_path_is_refused(
        self,
        allowlist_root: Path,
        relative_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Sentinel: ensure the seam never executes when cwd is refused.
        seam_reached = {"called": False}

        async def _should_not_run(**_: Any):
            seam_reached["called"] = True
            return ("", "", 0, 0.0, False)

        monkeypatch.setattr(run_module, "_execute_subprocess", _should_not_run)
        monkeypatch.setattr(
            run_module,
            "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=Path(relative_path),
            read_allowlist=[allowlist_root],
            with_nats_streaming=False,
        )

        assert result.status == "failed", (
            f"relative path {relative_path!r} must be refused, got {result.status!r}"
        )
        assert seam_reached["called"] is False, (
            f"executor was reached for refused path {relative_path!r} — "
            "the cwd guard regressed"
        )
        codes = [w.code for w in result.warnings]
        assert "cwd_outside_allowlist" in codes

    @pytest.mark.asyncio()
    async def test_absolute_path_outside_allowlist_is_refused(
        self,
        allowlist_root: Path,
        absolute_outside: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seam_reached = {"called": False}

        async def _should_not_run(**_: Any):
            seam_reached["called"] = True
            return ("", "", 0, 0.0, False)

        monkeypatch.setattr(run_module, "_execute_subprocess", _should_not_run)
        monkeypatch.setattr(
            run_module,
            "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=absolute_outside,
            read_allowlist=[allowlist_root],
            with_nats_streaming=False,
        )

        assert result.status == "failed"
        assert seam_reached["called"] is False
        codes = [w.code for w in result.warnings]
        assert "cwd_outside_allowlist" in codes

    @pytest.mark.asyncio()
    async def test_symlink_escape_outside_allowlist_is_refused(
        self,
        allowlist_root: Path,
        absolute_outside: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Plant a symlink *inside* the allowlist that resolves *outside*.
        # The cwd guard resolves before checking, so the symlink must not
        # smuggle the executor out of the allowed sub-tree.
        link = allowlist_root / "escape-link"
        try:
            os.symlink(absolute_outside, link)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable on this platform: {exc!r}")

        seam_reached = {"called": False}

        async def _should_not_run(**_: Any):
            seam_reached["called"] = True
            return ("", "", 0, 0.0, False)

        monkeypatch.setattr(run_module, "_execute_subprocess", _should_not_run)
        monkeypatch.setattr(
            run_module,
            "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=link,
            read_allowlist=[allowlist_root],
            with_nats_streaming=False,
        )

        assert result.status == "failed"
        assert seam_reached["called"] is False
        codes = [w.code for w in result.warnings]
        assert "cwd_outside_allowlist" in codes


# ---------------------------------------------------------------------------
# AC-003 — Disallowed-binary fuzz: 50+ binaries trigger permissions refusal
# ---------------------------------------------------------------------------


# Documents "things that explicitly cannot run in the worktree". This list
# is intentionally exhaustive — it acts as a security assertion in code
# review: if any of these binaries appear in a Forge command line, that
# is an audit-flag.
_DISALLOWED_BINARIES: tuple[str, ...] = (
    # Shell builtins / shells
    "bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "ash",
    # Interpreters
    "python", "python2", "python3", "perl", "ruby", "node", "deno", "lua",
    "php", "Rscript", "tclsh",
    # Network / data exfil tools
    "curl", "wget", "nc", "netcat", "ssh", "scp", "rsync", "ftp", "telnet",
    "tftp", "socat",
    # Filesystem mutation
    "rm", "mv", "cp", "dd", "chmod", "chown", "chgrp", "ln", "mkfs",
    "shred", "truncate",
    # Process / system
    "kill", "killall", "pkill", "sudo", "su", "doas", "systemctl",
    # File reads / dumps that should not be invoked from this layer
    "cat", "tac", "head", "tail", "less", "more", "strings", "xxd",
    "hexdump", "od",
    # Compilers / package managers (would side-effect the worktree)
    "gcc", "g++", "make", "cmake", "cargo", "npm", "pip", "pip3", "uv",
    "apt", "apt-get", "yum", "dnf", "brew",
)


class TestDisallowedBinaryRefusal:
    """AC-003 — 50+ common binaries all refused by the wrapper.

    The wrapper composes the command around ``_GUARDKIT_BINARY`` (a
    module constant), so it never *selects* a binary by name. The
    runtime defence is the OS / DeepAgents shell allowlist, which surfaces
    as ``PermissionError`` from the subprocess seam. This test enumerates
    each disallowed binary, simulates the refusal at the seam by raising
    a ``PermissionError`` whose message names the binary, and asserts the
    wrapper produces the canonical ``permissions_refused`` warning shape.
    """

    def test_disallowed_binary_corpus_exceeds_fifty(self) -> None:
        # AC floor — guard against accidental shrinkage.
        assert len(_DISALLOWED_BINARIES) >= 50, (
            f"AC requires >=50 disallowed binaries, got {len(_DISALLOWED_BINARIES)}"
        )

    def test_guardkit_binary_constant_is_not_in_the_disallowed_set(self) -> None:
        # Sanity: the *only* binary the wrapper actually invokes must
        # not be in the disallowed set, otherwise the test below is
        # circular.
        assert run_module._GUARDKIT_BINARY not in _DISALLOWED_BINARIES
        assert Path(run_module._GUARDKIT_BINARY).name not in _DISALLOWED_BINARIES

    @pytest.fixture()
    def worktree(self, tmp_path: Path) -> Path:
        repo = tmp_path / "build"
        repo.mkdir()
        return repo

    @pytest.fixture()
    def allowlist(self, worktree: Path) -> list[Path]:
        return [worktree.parent]

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "binary", _DISALLOWED_BINARIES, ids=list(_DISALLOWED_BINARIES)
    )
    async def test_disallowed_binary_yields_permissions_refused(
        self,
        binary: str,
        worktree: Path,
        allowlist: list[Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Simulate the OS / DeepAgents shell-allowlist refusing the binary.
        # The seam raises ``PermissionError``; the wrapper must convert it
        # into a structured ``status="failed"`` result with the canonical
        # ``permissions_refused`` warning code.
        async def _refuse(**kwargs: Any):
            raise PermissionError(
                f"binary {binary!r} not in shell allowlist"
            )

        monkeypatch.setattr(run_module, "_execute_subprocess", _refuse)
        monkeypatch.setattr(
            run_module,
            "resolve_context_flags",
            lambda *a, **kw: _empty_resolved(),
        )

        result = await run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=allowlist,
            with_nats_streaming=False,
        )

        assert result.status == "failed", (
            f"disallowed binary {binary!r} produced status={result.status!r}"
        )
        codes = [w.code for w in result.warnings]
        assert "permissions_refused" in codes, (
            f"expected permissions_refused warning for {binary!r}; got {codes!r}"
        )
        # The binary name should appear in the warning message so audit
        # logs can identify *which* binary was refused.
        msgs = " ".join(w.message for w in result.warnings)
        assert binary in msgs


