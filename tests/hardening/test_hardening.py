"""Security and concurrency hardening tests (TASK-IC-012).

This module is the consolidated defence-in-depth test suite for the eight
high-leverage scenarios identified in
``tasks/design_approved/TASK-IC-012-security-concurrency-hardening.md``:

* ``@security security-working-directory-allowlist``
* ``@security security-env-only-credentials``
* ``@security secrets-appearing-in-rationale-text-are-redacted``
* ``@security filesystem-read-allowlist``
* ``@security priors-as-argument-refusal``
* ``@negative negative-disallowed-binary-refused``
* ``@concurrency split-brain-mirror-dedupe``
* ``@concurrency recency-horizon-bound``
* ``@data-integrity supersession-cycle-rejection``

Each test class targets one acceptance criterion. The seven scenarios
originally scoped as separate modules in the task brief
(``test_redaction_fuzz.py``, ``test_subprocess_allowlist_fuzz.py``,
``test_working_dir_traversal.py``, ``test_split_brain_race.py``,
``test_recency_horizon_boundary.py``, ``test_priors_no_argv_leak.py``,
``test_supersession_chain_stress.py``) are consolidated into this single
file because the documentation level for the task is ``minimal``
(2 files total). One paired ``__init__.py`` is *not* required —
``tests/bdd/`` is already collected by pytest without one.

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
* The split-brain race test is fundamentally non-deterministic; it asserts
  on the post-state (entity_id collision under Graphiti's upsert
  semantics, per the dedupe contract carried over from TASK-IC-001), not
  on the timing.
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
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from forge.adapters.guardkit import run as run_module
from forge.adapters.guardkit.context_resolver import ResolvedContext
from forge.adapters.guardkit.run import run
from forge.memory.models import (
    CalibrationAdjustment,
    GateDecision,
    SessionOutcome,
)
from forge.memory.priors import (
    PriorsLeakError,
    Priors,
    assert_not_in_argv,
    render_priors_prose,
    retrieve_priors,
)
from forge.memory.redaction import redact_credentials
from forge.memory.session_outcome import (
    PIPELINE_HISTORY_GROUP_ID,
    _session_outcome_entity_id,
    write_session_outcome,
)
from forge.memory.supersession import (
    SupersessionCycleError,
    assert_no_cycle,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _empty_resolved() -> ResolvedContext:
    """Stand-in for the GuardKit context resolver (tests skip resolution)."""
    return ResolvedContext(flags=[], paths=[], warnings=[])


def _ts(*, day: int = 26, micro: int = 0) -> datetime:
    """Deterministic UTC timestamp keyed on day / microsecond."""
    return datetime(2026, 4, day, 12, 0, 0, micro, tzinfo=UTC)


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


# ---------------------------------------------------------------------------
# AC-004 — Split-brain race: concurrent ``write_session_outcome`` dedupes
# ---------------------------------------------------------------------------


class _ConcurrentExistsCheck:
    """Async exists-check that records every call.

    Used to simulate two concurrent writers racing past the pre-write
    existence check. Both callers see ``False`` from the check (the race
    window) and proceed to write — the dedupe contract is then enforced
    at the storage layer by the deterministic ``entity_id`` derived from
    ``build_id``.
    """

    def __init__(self, *, answers: list[bool] | None = None) -> None:
        self.calls: list[str] = []
        self._answers = list(answers) if answers else []
        self._lock = asyncio.Lock()

    async def __call__(self, build_id: str) -> bool:
        async with self._lock:
            self.calls.append(build_id)
            if self._answers:
                return self._answers.pop(0)
            return False


class _DedupingWriteRecorder:
    """Records writes and dedupes by ``entity_id`` to model storage upsert.

    Graphiti's storage-layer upsert collapses two writes that share an
    ``entity_id`` into a single row. This recorder mirrors that
    behaviour so the test asserts the dedupe contract end-to-end:
    deterministic entity_id from build_id ⇒ storage layer collapses
    duplicates ⇒ exactly one entity exists post-write.
    """

    def __init__(self) -> None:
        self.all_calls: list[tuple[UUID, str]] = []
        self.persisted: dict[UUID, SessionOutcome] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, entity: SessionOutcome, group_id: str) -> None:
        async with self._lock:
            self.all_calls.append((entity.entity_id, group_id))
            # Upsert by entity_id — second write replaces first, but
            # the row count remains 1.
            self.persisted[entity.entity_id] = entity


class _StubRepo:
    """In-memory ``PipelineHistoryRepository`` returning a fixed list."""

    def __init__(self, decisions: list[GateDecision]) -> None:
        self._decisions = decisions

    def get_gate_decisions_for_build(self, build_id: str) -> list[GateDecision]:
        return list(self._decisions)


class TestSplitBrainRaceDedupe:
    """AC-004 — two concurrent writers ⇒ exactly one entity exists."""

    @pytest.mark.asyncio()
    async def test_concurrent_writers_collapse_to_a_single_entity(self) -> None:
        build_id = "build-FEAT-FORGE-006-20260426120000"
        repo = _StubRepo(
            decisions=[
                GateDecision(
                    entity_id=uuid4(),
                    stage_name="planning",
                    decided_at=_ts(),
                    score=0.9,
                    criterion_breakdown={"completeness": 1.0},
                    rationale="ok",
                ),
            ]
        )
        exists_check = _ConcurrentExistsCheck(answers=[False, False])
        recorder = _DedupingWriteRecorder()

        # Two concurrent writers, racing past the existence check.
        result_a, result_b = await asyncio.gather(
            write_session_outcome(
                build_id=build_id,
                outcome="success",
                sqlite_repo=repo,
                exists_check=exists_check,
                closed_at=_ts(),
                write=recorder,
            ),
            write_session_outcome(
                build_id=build_id,
                outcome="success",
                sqlite_repo=repo,
                exists_check=exists_check,
                closed_at=_ts(),
                write=recorder,
            ),
        )

        # Both writers reached ``write_entity`` (the race scenario).
        assert len(recorder.all_calls) == 2

        # Both wrote the *same* deterministic entity_id derived from
        # build_id — the dedupe contract carried from TASK-IC-001.
        ids_written = {entity_id for entity_id, _ in recorder.all_calls}
        assert len(ids_written) == 1
        expected_id = _session_outcome_entity_id(build_id)
        assert ids_written == {expected_id}

        # Storage-layer upsert collapses to exactly one persisted row.
        assert len(recorder.persisted) == 1
        assert expected_id in recorder.persisted

        # Group_id is the canonical pipeline-history group.
        assert {gid for _, gid in recorder.all_calls} == {PIPELINE_HISTORY_GROUP_ID}

        # Both write attempts surfaced the populated entity to the caller.
        assert result_a is not None
        assert result_b is not None
        assert result_a.entity_id == result_b.entity_id == expected_id

    @pytest.mark.asyncio()
    async def test_one_winner_one_noop_when_existence_check_serialises(
        self,
    ) -> None:
        # A faster Graphiti backend wins the race: the second writer's
        # exists_check sees ``True`` and no-ops cleanly.
        build_id = "build-FEAT-FORGE-006-20260426120000"
        repo = _StubRepo(decisions=[])
        exists_check = _ConcurrentExistsCheck(answers=[False, True])
        recorder = _DedupingWriteRecorder()

        result_a, result_b = await asyncio.gather(
            write_session_outcome(
                build_id=build_id,
                outcome="success",
                sqlite_repo=repo,
                exists_check=exists_check,
                closed_at=_ts(),
                write=recorder,
            ),
            write_session_outcome(
                build_id=build_id,
                outcome="success",
                sqlite_repo=repo,
                exists_check=exists_check,
                closed_at=_ts(),
                write=recorder,
            ),
        )

        # Exactly one writer reached ``write_entity``.
        assert len(recorder.all_calls) == 1
        assert len(recorder.persisted) == 1
        # The other no-op'd (returns None).
        outcomes = sorted(
            ["written" if r is not None else "noop" for r in (result_a, result_b)]
        )
        assert outcomes == ["noop", "written"]


# ---------------------------------------------------------------------------
# AC-005 — Recency horizon boundary
# ---------------------------------------------------------------------------


def _make_adjustment(
    *,
    approved: bool,
    expires_at: datetime,
) -> CalibrationAdjustment:
    """Build a minimal ``CalibrationAdjustment`` parameterised by expiry."""
    return CalibrationAdjustment(
        entity_id=uuid4(),
        parameter="confidence_threshold",
        old_value="0.7",
        new_value="0.75",
        approved=approved,
        proposed_at=_ts(day=1),
        expires_at=expires_at,
    )


class _ContextStub:
    """Minimal duck-type for ``forge.pipeline.BuildContext``."""

    feature_id = "FEAT-FORGE-006"
    build_id = "build-FEAT-FORGE-006-20260426120000"


class TestRecencyHorizonBoundary:
    """AC-005 — boundary inclusion at the expiry horizon.

    The ``approved_calibration_adjustments`` filter is the only in-process
    recency-bound check (``approved AND expires_at > now()``), so it is
    the load-bearing boundary to test. The feature-bound
    ``horizon_days``-since filter is enforced by the backend dispatcher;
    we additionally assert the dispatcher receives the correct
    ``since`` value derived from ``now - timedelta(days=horizon_days)``.
    """

    @pytest.mark.asyncio()
    async def test_adjustment_at_now_plus_one_microsecond_is_included(
        self,
    ) -> None:
        now = _ts()
        # Just-barely-not-expired: expires_at = now + 1 microsecond.
        live = _make_adjustment(
            approved=True,
            expires_at=now + timedelta(microseconds=1),
        )

        async def _dispatch(**kwargs: Any):
            if kwargs["entity_type"] == "CalibrationAdjustment":
                return [live.model_dump(mode="json")]
            return []

        priors = await retrieve_priors(
            _ContextStub(), now=now, query_fn=_dispatch
        )

        assert len(priors.approved_calibration_adjustments) == 1
        assert priors.approved_calibration_adjustments[0].entity_id == live.entity_id

    @pytest.mark.asyncio()
    async def test_adjustment_at_exactly_now_is_excluded(self) -> None:
        now = _ts()
        # Exactly expired: expires_at == now ⇒ ``expires_at > now`` is
        # False ⇒ excluded by the boundary filter.
        on_horizon = _make_adjustment(approved=True, expires_at=now)

        async def _dispatch(**kwargs: Any):
            if kwargs["entity_type"] == "CalibrationAdjustment":
                return [on_horizon.model_dump(mode="json")]
            return []

        priors = await retrieve_priors(
            _ContextStub(), now=now, query_fn=_dispatch
        )

        assert priors.approved_calibration_adjustments == []

    @pytest.mark.asyncio()
    async def test_adjustment_at_now_minus_one_microsecond_is_excluded(
        self,
    ) -> None:
        now = _ts()
        # Already expired by 1 µs.
        expired = _make_adjustment(
            approved=True,
            expires_at=now - timedelta(microseconds=1),
        )

        async def _dispatch(**kwargs: Any):
            if kwargs["entity_type"] == "CalibrationAdjustment":
                return [expired.model_dump(mode="json")]
            return []

        priors = await retrieve_priors(
            _ContextStub(), now=now, query_fn=_dispatch
        )

        assert priors.approved_calibration_adjustments == []

    @pytest.mark.asyncio()
    async def test_unapproved_adjustment_is_excluded_even_when_live(self) -> None:
        now = _ts()
        unapproved = _make_adjustment(
            approved=False,
            expires_at=now + timedelta(days=10),
        )

        async def _dispatch(**kwargs: Any):
            if kwargs["entity_type"] == "CalibrationAdjustment":
                return [unapproved.model_dump(mode="json")]
            return []

        priors = await retrieve_priors(
            _ContextStub(), now=now, query_fn=_dispatch
        )

        assert priors.approved_calibration_adjustments == []

    @pytest.mark.asyncio()
    async def test_dispatcher_receives_correct_since_for_horizon_days(self) -> None:
        # Defence-in-depth: assert the horizon_days arithmetic is exact.
        now = _ts()
        captured: list[dict[str, Any]] = []

        async def _dispatch(**kwargs: Any):
            captured.append(dict(kwargs))
            return []

        await retrieve_priors(
            _ContextStub(), now=now, horizon_days=30, query_fn=_dispatch
        )

        expected_since = now - timedelta(days=30)
        for call in captured:
            assert call["since"] == expected_since, (
                f"dispatcher got since={call['since']!r}, "
                f"expected {expected_since!r}"
            )


# ---------------------------------------------------------------------------
# AC-006 — Property test: priors content never appears in ``sys.argv``
# ---------------------------------------------------------------------------


_PRIORS_LEAK_SEED = 0xF06_E_1C_012_E_06


def _random_priors(rng: random.Random) -> Priors:
    """Build a random :class:`Priors` with multi-line, non-trivial content.

    Each rendered line must be >=12 characters to trip the leak detector
    (``_meaningful_lines`` filters short lines), so we pad fields to be
    well above that floor.
    """
    pad = "x" * 16  # ensure rendered lines exceed the 12-char floor.
    n_builds = rng.randint(0, 3)
    builds = [
        SessionOutcome(
            entity_id=uuid4(),
            build_id=f"build-prop-{rng.randrange(10**6):06d}-{pad}",
            outcome=rng.choice(["success", "failure", "aborted"]),
            gate_decision_ids=[],
            closed_at=_ts(day=20 + i),
        )
        for i in range(n_builds)
    ]
    return Priors(recent_similar_builds=builds)


class TestPriorsNoArgvLeak:
    """AC-006 — for any random Priors object, no field content reaches argv."""

    def test_clean_argv_renders_without_raising(self) -> None:
        # Sanity: with our test argv (which doesn't echo priors content),
        # rendering must succeed for many random Priors objects.
        rng = random.Random(_PRIORS_LEAK_SEED)
        for _ in range(64):
            p = _random_priors(rng)
            # Rendering invokes assert_not_in_argv internally.
            prose = render_priors_prose(p)
            assert "## recent_similar_builds" in prose

    def test_priors_content_in_argv_is_detected_and_rejected(self) -> None:
        """If a caller smuggles priors content into argv, the leak fires."""
        rng = random.Random(_PRIORS_LEAK_SEED + 1)
        original_argv = list(sys.argv)
        try:
            for _ in range(64):
                p = _random_priors(rng)
                if not p.recent_similar_builds:
                    continue  # empty priors render only headers — nothing to leak.
                # Pick the first rendered build line and plant it in argv.
                # Mirror render_priors_prose's exact line shape so the
                # leak detector's substring check matches: items render
                # as ``- {_format_item(item)}`` with the leading hyphen.
                outcome = p.recent_similar_builds[0]
                rendered_line = (
                    f"- build={outcome.build_id} outcome={outcome.outcome} "
                    f"closed_at={outcome.closed_at.isoformat()}"
                )
                sys.argv = original_argv + [rendered_line]
                with pytest.raises(PriorsLeakError) as excinfo:
                    render_priors_prose(p)
                # The error message names the offending argv element so
                # operators can locate the leak.
                assert "Priors leak detected" in str(excinfo.value)
        finally:
            sys.argv = original_argv

    def test_assert_not_in_argv_short_lines_do_not_false_positive(self) -> None:
        # Lines under the 12-char floor must not trigger the detector,
        # otherwise unrelated short tokens in argv would block every build.
        original_argv = list(sys.argv)
        try:
            sys.argv = original_argv + ["short"]
            # No raise expected.
            assert_not_in_argv("short\nshort\n")
        finally:
            sys.argv = original_argv

    def test_assert_not_in_argv_section_header_lines_are_safe(self) -> None:
        original_argv = list(sys.argv)
        try:
            sys.argv = original_argv + ["## recent_similar_builds"]
            # Headers are explicitly skipped — no raise.
            assert_not_in_argv("## recent_similar_builds\n")
        finally:
            sys.argv = original_argv


# ---------------------------------------------------------------------------
# AC-007 — Supersession chain stress: depth 9, 10, 11; cycle of any length
# ---------------------------------------------------------------------------


def _build_linear_chain(length: int) -> tuple[
    CalibrationAdjustment, list[CalibrationAdjustment]
]:
    """Build a linear supersession chain: new -> A_1 -> A_2 -> ... -> A_length.

    Returns ``(new_adjustment, ancestors_in_walk_order)``. Ancestors are
    indexed from the proposed adjustment outward.
    """
    # Build ancestors first so each one's ``supersedes`` points further back.
    ancestors: list[CalibrationAdjustment] = []
    prev_id: UUID | None = None
    for i in range(length, 0, -1):
        adj = CalibrationAdjustment(
            entity_id=uuid4(),
            parameter="confidence_threshold",
            old_value=f"{0.5 + 0.01 * i:.2f}",
            new_value=f"{0.5 + 0.01 * (i + 1):.2f}",
            approved=True,
            supersedes=prev_id,
            proposed_at=_ts(day=1),
            expires_at=_ts(day=27),
        )
        prev_id = adj.entity_id
        ancestors.insert(0, adj)
    head_id = ancestors[0].entity_id if ancestors else None
    new = CalibrationAdjustment(
        entity_id=uuid4(),
        parameter="confidence_threshold",
        old_value="0.5",
        new_value="0.51",
        approved=False,
        supersedes=head_id,
        proposed_at=_ts(day=1),
        expires_at=_ts(day=27),
    )
    return new, ancestors


def _build_resolver(
    adjustments: list[CalibrationAdjustment],
):
    by_id = {str(adj.entity_id): adj for adj in adjustments}

    def _resolver(entity_id: str) -> CalibrationAdjustment | None:
        return by_id.get(entity_id)

    return _resolver


class TestSupersessionChainStress:
    """AC-007 — depth-10 succeeds; depth-11 raises; cycles raise immediately."""

    @pytest.mark.parametrize("length", [9, 10])
    def test_chain_within_depth_cap_succeeds(self, length: int) -> None:
        new, ancestors = _build_linear_chain(length)
        resolver = _build_resolver(ancestors)
        # Default max_depth=10 — chains of length 9 and 10 must both pass.
        assert assert_no_cycle(new, resolver) is None

    def test_chain_of_length_eleven_raises(self) -> None:
        new, ancestors = _build_linear_chain(11)
        resolver = _build_resolver(ancestors)
        with pytest.raises(SupersessionCycleError) as excinfo:
            assert_no_cycle(new, resolver)
        # Error message must include the chain context for operator audit.
        assert "max_depth" in str(excinfo.value)
        assert str(new.entity_id) in str(excinfo.value)

    def test_two_node_cycle_raises_immediately(self) -> None:
        # A -> B -> A.
        a_id = uuid4()
        b_id = uuid4()
        a = CalibrationAdjustment(
            entity_id=a_id,
            parameter="p",
            old_value="0.1",
            new_value="0.2",
            approved=True,
            supersedes=b_id,
            proposed_at=_ts(day=1),
            expires_at=_ts(day=27),
        )
        b = CalibrationAdjustment(
            entity_id=b_id,
            parameter="p",
            old_value="0.2",
            new_value="0.3",
            approved=True,
            supersedes=a_id,
            proposed_at=_ts(day=1),
            expires_at=_ts(day=27),
        )
        new = CalibrationAdjustment(
            entity_id=uuid4(),
            parameter="p",
            old_value="0.3",
            new_value="0.4",
            approved=False,
            supersedes=a_id,
            proposed_at=_ts(day=1),
            expires_at=_ts(day=27),
        )
        resolver = _build_resolver([a, b])
        with pytest.raises(SupersessionCycleError) as excinfo:
            assert_no_cycle(new, resolver)
        assert "Supersession cycle detected" in str(excinfo.value)

    @pytest.mark.parametrize("cycle_length", [3, 5, 8])
    def test_cycle_of_arbitrary_length_raises(self, cycle_length: int) -> None:
        # Build A_1 -> A_2 -> ... -> A_n -> A_1 (cycle).
        ids = [uuid4() for _ in range(cycle_length)]
        adjustments: list[CalibrationAdjustment] = []
        for idx, eid in enumerate(ids):
            next_id = ids[(idx + 1) % cycle_length]
            adjustments.append(
                CalibrationAdjustment(
                    entity_id=eid,
                    parameter="p",
                    old_value=f"{0.1 * idx:.2f}",
                    new_value=f"{0.1 * (idx + 1):.2f}",
                    approved=True,
                    supersedes=next_id,
                    proposed_at=_ts(day=1),
                    expires_at=_ts(day=27),
                )
            )
        new = CalibrationAdjustment(
            entity_id=uuid4(),
            parameter="p",
            old_value="0.0",
            new_value="0.01",
            approved=False,
            supersedes=ids[0],
            proposed_at=_ts(day=1),
            expires_at=_ts(day=27),
        )
        resolver = _build_resolver(adjustments)
        with pytest.raises(SupersessionCycleError):
            assert_no_cycle(new, resolver)

    def test_self_supersession_raises_immediately(self) -> None:
        eid = uuid4()
        new = CalibrationAdjustment(
            entity_id=eid,
            parameter="p",
            old_value="0.1",
            new_value="0.2",
            approved=False,
            supersedes=eid,
            proposed_at=_ts(day=1),
            expires_at=_ts(day=27),
        )
        # Resolver is unused — the self-supersession check fires before
        # any lookup.
        with pytest.raises(SupersessionCycleError) as excinfo:
            assert_no_cycle(new, lambda _id: None)
        assert "Self-supersession" in str(excinfo.value)
