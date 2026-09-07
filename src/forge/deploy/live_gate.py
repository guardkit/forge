"""Live-gate + broker-preflight seams for the deploy stage (WS2-B8).

Two interfaces the deploy stage depends on, each with three concrete backends
(unconfigured / dry-run / real):

- :class:`LiveGateInvoker` — the ``run_live_gate`` step shells
  ``guardkit qa live-gate`` through the **frozen** seam
  ``forge.adapters.guardkit.run`` (seam v1 frozen; consumed as a subprocess
  black box, never edited). Returns a :class:`LiveGateInvocation` carrying the
  results-envelope verdict + refs so the stage can build the B7
  ``QAVerdictPayload`` / ``LiveGateResultPayload``.
- :class:`BrokerInspector` — the ``broker_preflight`` step diffs live broker
  state against the F6 broker-contract section before services start.

Guardrail (FEAT-DD4F): every unconfigured seam **raises loudly if invoked** —
never a silent no-op that reads green. The default production backend (until
V1) is the ``Unconfigured*`` one; the dry-run backend records what it *would*
do (explicitly labelled ``dry_run=True`` — an honest non-verdict, not a fake
pass); the real backend does the work.

The guardkit seam is ``async``; the executor invokes step handlers
synchronously. :class:`GuardkitSeamLiveGateInvoker` bridges by running the
frozen coroutine to completion on a dedicated worker thread with its own event
loop, so the seam stays untouched and the sync handler contract is preserved.

WHERE THE GATE RUNS (protect-main, Part J, 2026-09-07). A backend that runs a
driver has a working directory, fixed when the deploy stage is composed: the
repository checkout. The candidate leg checks the feature branch's laid-out
tree, and its gate must read THAT tree's registry and Hurl twins, not the
checkout's (which is main — the per-feature gate is registered on the branch
and only reaches main with the merge). So every backend with a working
directory offers ``with_repo_path``: a copy moved into the tree, exactly as
``with_extra_env`` is a copy carrying the candidate's addresses. The stage
applies the move first and the env overlay second, for the candidate leg
only; the promote leg and a plain deploy keep the checkout.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forge.adapters.guardkit.models import GuardKitResult

logger = logging.getLogger(__name__)

__all__ = [
    "LiveGateInvocation",
    "LiveGateInvoker",
    "UnconfiguredLiveGateInvoker",
    "DryRunLiveGateInvoker",
    "GuardkitSeamLiveGateInvoker",
    "RepoDriverLiveGateInvoker",
    "RefusingLiveGateInvoker",
    "BrokerDiff",
    "BrokerInspector",
    "UnconfiguredBrokerInspector",
    "DryRunBrokerInspector",
    "LiveGateSeamError",
]


# The four-for-four verdict enum (DF-017 / B7). instrument_fail / environment_fail
# never indict the system under test and are never counted against the feature.
_VALID_VERDICTS = frozenset({"pass", "fail", "instrument_fail", "environment_fail"})


class LiveGateSeamError(RuntimeError):
    """Raised by an unconfigured seam when it is invoked."""


# ---------------------------------------------------------------------------
# Live-gate invoker
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveGateInvocation:
    """The outcome of one live-gate invocation (a results-envelope projection).

    Field names mirror the B7 ``QAVerdictPayload`` / ``LiveGateResultPayload``
    vocabulary so the stage maps them onto the wire with no translation.

    Attributes:
        verdict: Four-for-four verdict (pass|fail|instrument_fail|environment_fail).
        run_id: The results envelope's run id.
        gate_ids: Gate scripts executed.
        assertions: Per-assertion outcome dicts (id/gate_id/status/disposition/…).
        evidence_index_ref: The envelope's evidence index (F5 convention).
        app_url: Live instance driven, or None.
        screenshot_refs: Ordered screenshot evidence refs.
        trace_refs: Trace/HAR/log evidence refs.
        dispositions_ref: Ref to the F8 dispositions record, or None.
        attempts_ledger_ref: Ref to the F9 attempts ledger, or None.
        leak_sweep_findings: Count of leak-sweep findings, or None.
        dry_run: True when this is a dry-run projection, not a real verdict.
        detail: Backend-specific detail (command line, stdout tail, …).
    """

    verdict: str
    run_id: str
    gate_ids: tuple[str, ...] = ()
    assertions: tuple[dict[str, Any], ...] = ()
    evidence_index_ref: str = ""
    app_url: str | None = None
    screenshot_refs: tuple[str, ...] = ()
    trace_refs: tuple[str, ...] = ()
    dispositions_ref: str | None = None
    attempts_ledger_ref: str | None = None
    leak_sweep_findings: int | None = None
    dry_run: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"verdict must be one of {sorted(_VALID_VERDICTS)}, got {self.verdict!r}"
            )


@runtime_checkable
class LiveGateInvoker(Protocol):
    """Shells ``guardkit qa live-gate`` and returns a :class:`LiveGateInvocation`."""

    def invoke(
        self,
        *,
        feature: str,
        target: str,
        gates: tuple[str, ...] = (),
    ) -> LiveGateInvocation:
        """Run the live gate for ``feature`` against ``target``."""
        ...


class UnconfiguredLiveGateInvoker:
    """Raises if invoked (FEAT-DD4F). The default until the seam is configured."""

    def invoke(
        self, *, feature: str, target: str, gates: tuple[str, ...] = ()
    ) -> LiveGateInvocation:
        raise LiveGateSeamError(
            f"run_live_gate invoked for feature={feature!r} target={target!r} but "
            "no live-gate invoker is configured. Refusing to synthesize a verdict "
            "(a fake pass is worse than none). Wire a GuardkitSeamLiveGateInvoker "
            "or run in dry-run mode."
        )


class RefusingLiveGateInvoker:
    """Raises ``reason`` whenever it is invoked.

    For a gate that must NOT run as it stands — the stage puts this in the
    runbook when the candidate leg was given a tree to run in but the
    configured invoker cannot be moved into one. The step then fails with the
    reason on record, instead of the gate quietly running in the checkout and
    checking main's registry against the branch's build (the very defect the
    candidate check exists to catch).
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def invoke(
        self, *, feature: str, target: str, gates: tuple[str, ...] = ()
    ) -> LiveGateInvocation:
        raise LiveGateSeamError(
            f"run_live_gate refused for feature={feature!r} target={target!r}: "
            f"{self._reason}"
        )


class DryRunLiveGateInvoker:
    """Records the intended ``guardkit qa live-gate`` command without running it.

    Returns a ``dry_run=True`` invocation with an explicit ``pass`` verdict that
    is labelled as a dry run — NOT a claim that the gate passed. The stage marks
    the whole run dry-run in the F7 record and never publishes a live QA verdict
    consumers would mistake for a real one.

    ``repo_path`` is the working directory the gate WOULD run in (None when
    unsaid); :meth:`with_repo_path` returns a copy that records a different
    one, so a dry run of the candidate leg says it would run in the tree.
    """

    def __init__(self, *, repo_path: Path | None = None) -> None:
        self._repo_path = Path(repo_path) if repo_path is not None else None

    @property
    def repo_path(self) -> Path | None:
        """The working directory this dry run would use, or None."""
        return self._repo_path

    def with_repo_path(self, repo_path: Path | str) -> "DryRunLiveGateInvoker":
        """A copy that would run in ``repo_path`` (this one is unchanged)."""
        return DryRunLiveGateInvoker(repo_path=Path(repo_path))

    def invoke(
        self, *, feature: str, target: str, gates: tuple[str, ...] = ()
    ) -> LiveGateInvocation:
        gate_args = list(gates)
        planned = [
            "guardkit",
            "qa",
            "live-gate",
            "--feature",
            feature,
            "--target",
            target,
        ]
        if gate_args:
            planned += ["--gates", ",".join(gate_args)]
        logger.info("dry-run live-gate: would invoke %s", " ".join(planned))
        return LiveGateInvocation(
            verdict="pass",
            run_id=f"dryrun-{feature}-{target}",
            gate_ids=tuple(gate_args),
            evidence_index_ref="",
            dry_run=True,
            detail={
                "planned_command": planned,
                "cwd": str(self._repo_path) if self._repo_path is not None else None,
            },
        )


class GuardkitSeamLiveGateInvoker:
    """Shells ``guardkit qa live-gate`` through the FROZEN seam (real backend).

    Bridges the async frozen seam (``forge.adapters.guardkit.run.run``) to the
    executor's sync handler contract by running the coroutine on a dedicated
    worker thread with its own event loop — the seam is consumed verbatim,
    never edited (seam v1 frozen).

    The seam returns a :class:`GuardKitResult` (stdout/exit_code/status). v1
    maps that to a verdict via :meth:`_verdict_from_result`; the richer
    per-assertion envelope parsing is B4's disposition/verdict layer — this
    backend carries the coarse verdict + stdout so the stage is wired end to
    end without reaching into the seam's internals.
    """

    def __init__(
        self,
        *,
        repo_path: Path,
        read_allowlist: tuple[Path, ...],
        timeout_seconds: int = 600,
    ) -> None:
        self._repo_path = repo_path
        self._read_allowlist = list(read_allowlist)
        self._timeout_seconds = timeout_seconds

    @property
    def repo_path(self) -> Path:
        """The repository the seam is pointed at (the gate's working directory)."""
        return self._repo_path

    def with_repo_path(self, repo_path: Path | str) -> "GuardkitSeamLiveGateInvoker":
        """A copy pointed at ``repo_path`` — the candidate tree — with the same
        read allowlist and timeout. This invoker is unchanged."""
        return GuardkitSeamLiveGateInvoker(
            repo_path=Path(repo_path),
            read_allowlist=tuple(self._read_allowlist),
            timeout_seconds=self._timeout_seconds,
        )

    def invoke(
        self, *, feature: str, target: str, gates: tuple[str, ...] = ()
    ) -> LiveGateInvocation:
        # Import inside the method so importing this module never imports the
        # frozen seam (keeps the seam boundary explicit and test isolation
        # clean — tests patch `_call_seam`, not the seam module).
        args = ["live-gate", "--feature", feature, "--target", target]
        if gates:
            args += ["--gates", ",".join(gates)]
        result = self._call_seam(args)
        verdict = self._verdict_from_result(result)
        return LiveGateInvocation(
            verdict=verdict,
            run_id=f"{feature}-{target}",
            gate_ids=tuple(gates),
            evidence_index_ref="",
            dry_run=False,
            detail={
                "seam_status": getattr(result, "status", None),
                "exit_code": getattr(result, "exit_code", None),
            },
        )

    def _call_seam(self, args: list[str]) -> GuardKitResult:
        """Run the frozen async seam to completion on a worker thread."""
        import asyncio

        from forge.adapters.guardkit import run as guardkit_run

        def _runner() -> GuardKitResult:
            return asyncio.run(
                guardkit_run.run(
                    subcommand="qa",
                    args=args,
                    repo_path=self._repo_path,
                    read_allowlist=self._read_allowlist,
                    timeout_seconds=self._timeout_seconds,
                )
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result()

    @staticmethod
    def _verdict_from_result(result: GuardKitResult) -> str:
        """Coarse v1 verdict from the seam result (B4 refines per-assertion).

        A guardkit ``qa live-gate`` exit code carries the runner's own
        four-for-four verdict mapping (B3): 0=pass, 4=environment_fail,
        5=instrument_fail (scope §3 / DF-017), any other non-zero = fail.
        A seam-level ``timeout``/``failed`` status is an environment/instrument
        problem, never a SUT ``fail`` — it never indicts the feature.
        """
        status = getattr(result, "status", None)
        exit_code = getattr(result, "exit_code", None)
        if status == "timeout":
            return "environment_fail"
        if exit_code == 0:
            return "pass"
        if exit_code == 4:
            return "environment_fail"
        if exit_code == 5:
            return "instrument_fail"
        if status == "failed" and (exit_code is None or exit_code < 0):
            # The wrapper itself failed (binary missing, cwd refused): an
            # instrument problem, not a SUT fail.
            return "instrument_fail"
        return "fail"


# ---------------------------------------------------------------------------
# Real per-target live-gate backend (runs a TARGET REPO's own driver)
# ---------------------------------------------------------------------------


#: The DRIVER's own four-for-four exit-code map (mirrors api_test
#: ``qa/gates/local_live_gate.py::_VERDICT_EXIT``): 0=pass, 1=fail,
#: 3=instrument_fail, 4=environment_fail. Used as the fallback verdict when the
#: driver's stdout is NOT a parseable results envelope. Any other exit code maps
#: to ``fail`` (a non-zero the driver did not classify).
_DRIVER_EXIT_VERDICT: dict[int, str] = {
    0: "pass",
    1: "fail",
    3: "instrument_fail",
    4: "environment_fail",
}

#: Bound on the stdout/stderr tail recorded in ``LiveGateInvocation.detail`` so a
#: chatty driver never bloats a deploy record.
_STDIO_TAIL_CAP = 2000


def _bounded_tail(text: str | None, cap: int = _STDIO_TAIL_CAP) -> str:
    """The last ``cap`` characters of ``text`` (empty string for None/empty)."""
    if not text:
        return ""
    return text[-cap:]


class RepoDriverLiveGateInvoker:
    """The REAL per-target live-gate backend: runs a target repo's own driver.

    F16 story (why the guardkit-CLI backend is not usable as the per-target real
    backend today): guardkit's live-gate pre-flight ALWAYS consults an F16
    perishable-prereq checklist provider, but the ``guardkit qa live-gate`` CLI
    wires NONE — so :class:`GuardkitSeamLiveGateInvoker` (which shells that CLI
    through the frozen seam) short-circuits to ``environment_fail`` (exit 4) on
    EVERY repo BEFORE any registered gate script runs, no matter how healthy the
    deployment is. That is a guardkit-side v1 gap, not a target-repo authoring
    gap. Until guardkit gains an F16-provider CLI hook, each target repo instead
    carries its own honest driver (e.g. api_test ``qa/gates/local_live_gate.py``)
    that injects a minimal F16 health-probe provider into the SAME UNMODIFIED
    guardkit ``LiveGateRunner``, executes the registered gates against the live
    deployment, prints the genuine results-envelope JSON on stdout, and exits by
    the four-for-four verdict map above. This backend runs that driver as a
    subprocess and projects its envelope onto a :class:`LiveGateInvocation`.

    The frozen seam (``forge.adapters.guardkit.run``) hardcodes the guardkit
    binary, so it cannot run a repo driver — hence a distinct subprocess path
    here. **This backend retires when guardkit gains an F16-provider CLI hook**
    (or a real WS5 F16 source): the target drivers fold back into
    ``guardkit qa live-gate`` and :class:`GuardkitSeamLiveGateInvoker`.

    Mirrors the seam posture: :meth:`invoke` NEVER raises — a timeout is an
    ``environment_fail``, a spawn failure an ``instrument_fail``, and an
    unparseable stdout falls back to the driver's own exit-code map. None of
    those indict the system under test (DF-017).

    WHICH TREE THE DRIVER CHECKS is decided by its working directory alone:
    the drivers' ``--repo`` defaults to ``.``, and guardkit's ``LiveGateRunner``
    resolves the gate registry (``qa/gates/registry.yaml``), the gates' Hurl
    twins and the evidence directory (``qa/gates/evidence/``) relative to it.
    So the candidate leg gets a copy moved into the branch's laid-out tree
    (:meth:`with_repo_path`) and the driver named in the profile is found,
    run and read from that tree — its registry, its twins, its evidence. No
    ``--repo`` is added to the argv: the working directory is the one lever
    every driver already honours, and a second one would be an argument every
    driver would have to accept.

    Args:
        repo_path: Absolute path to the target repo (the subprocess ``cwd``).
        driver_argv: The per-target driver command, e.g.
            ``["python3", "qa/gates/local_live_gate.py"]``.
        timeout_seconds: Hard wall on the driver subprocess (default 600).
        extra_env: Non-secret env overlaid on ``os.environ`` for the driver
            (e.g. a base URL); an empty map by default.
    """

    def __init__(
        self,
        *,
        repo_path: Path,
        driver_argv: list[str],
        timeout_seconds: int = 600,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._repo_path = Path(repo_path)
        self._driver_argv = list(driver_argv)
        self._timeout_seconds = timeout_seconds
        self._extra_env = dict(extra_env or {})

    @property
    def repo_path(self) -> Path:
        """The working directory the driver runs in — the tree it checks."""
        return self._repo_path

    def with_repo_path(self, repo_path: Path | str) -> "RepoDriverLiveGateInvoker":
        """Return a copy whose driver runs in ``repo_path``, env and argv kept.

        Protect-main (rule 38): the candidate leg's gate runs IN the feature
        branch's laid-out tree, so the driver found relative to that tree reads
        that tree's registry and twins and writes its evidence there. The stage
        applies this BEFORE :meth:`with_extra_env` (which keeps whatever
        working directory its invoker has), for the candidate leg only. A copy
        (not a mutation): the shared injected invoker still points at the
        checkout for the promote leg and for a plain deploy.
        """
        return RepoDriverLiveGateInvoker(
            repo_path=Path(repo_path),
            driver_argv=self._driver_argv,
            timeout_seconds=self._timeout_seconds,
            extra_env=self._extra_env,
        )

    def with_extra_env(self, overlay: dict[str, str]) -> "RepoDriverLiveGateInvoker":
        """Return a copy whose driver env is this invoker's env plus ``overlay``.

        Candidate-then-promote sequencing (S2F): the candidate-leg live gate must
        address the ``-cand`` instance, so the candidate.env overlay (e.g.
        ``API_TEST_BASE_URL=http://localhost:8902``) is merged ON TOP of the
        profile's live_gate.env (overlay wins on a key clash) for that leg ONLY —
        the promote-leg live gate keeps the base env untouched. A copy (not a
        mutation) so the shared injected invoker is never altered. The working
        directory is kept as it is on THIS invoker — the candidate leg moves it
        with :meth:`with_repo_path` first.
        """
        merged = {**self._extra_env, **overlay}
        return RepoDriverLiveGateInvoker(
            repo_path=self._repo_path,
            driver_argv=self._driver_argv,
            timeout_seconds=self._timeout_seconds,
            extra_env=merged,
        )

    def invoke(
        self, *, feature: str, target: str, gates: tuple[str, ...] = ()
    ) -> LiveGateInvocation:
        # Imported inside the method to keep this module's import surface small
        # (the seam-boundary precedent above).
        import json
        import subprocess

        argv = [*self._driver_argv, "--feature", feature, "--target", target]
        if gates:
            argv += ["--gates", ",".join(gates)]
        env = os.environ | self._extra_env
        run_id_fallback = f"{feature}-{target}"
        gate_ids = tuple(gates)

        try:
            proc = subprocess.run(
                argv,
                cwd=str(self._repo_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # A driver that never returns is an ENVIRONMENT problem — never a SUT
            # fail (it never indicts the feature).
            return LiveGateInvocation(
                verdict="environment_fail",
                run_id=run_id_fallback,
                gate_ids=gate_ids,
                dry_run=False,
                detail={
                    "argv": argv,
                    "cwd": str(self._repo_path),
                    "exit_code": None,
                    "error": f"driver timed out after {self._timeout_seconds}s",
                    "stdout_tail": _bounded_tail(
                        exc.stdout if isinstance(exc.stdout, str) else None
                    ),
                    "stderr_tail": _bounded_tail(
                        exc.stderr if isinstance(exc.stderr, str) else None
                    ),
                },
            )
        except OSError as exc:
            # Spawn failure — missing interpreter / script / not executable. An
            # INSTRUMENT problem (the gate could not be run), never a SUT fail.
            return LiveGateInvocation(
                verdict="instrument_fail",
                run_id=run_id_fallback,
                gate_ids=gate_ids,
                dry_run=False,
                detail={
                    "argv": argv,
                    "cwd": str(self._repo_path),
                    "exit_code": None,
                    "error": f"could not spawn driver: {exc}",
                    "stdout_tail": "",
                    "stderr_tail": "",
                },
            )
        except Exception as exc:  # noqa: BLE001 — NEVER raise past invoke()
            return LiveGateInvocation(
                verdict="instrument_fail",
                run_id=run_id_fallback,
                gate_ids=gate_ids,
                dry_run=False,
                detail={
                    "argv": argv,
                    "cwd": str(self._repo_path),
                    "exit_code": None,
                    "error": f"driver invocation error: {exc}",
                },
            )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        detail: dict[str, Any] = {
            "argv": argv,
            "cwd": str(self._repo_path),
            "exit_code": proc.returncode,
            "stdout_tail": _bounded_tail(stdout),
            "stderr_tail": _bounded_tail(stderr),
        }

        envelope: dict[str, Any] | None = None
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                envelope = parsed
        except (json.JSONDecodeError, ValueError):
            envelope = None

        if envelope is not None:
            verdict = envelope.get("verdict")
            if verdict in _VALID_VERDICTS:
                gate_ids_env = tuple(
                    str(g.get("gate_id"))
                    for g in (envelope.get("gates") or [])
                    if isinstance(g, dict) and g.get("gate_id")
                )
                return LiveGateInvocation(
                    verdict=str(verdict),
                    run_id=str(envelope.get("run_id") or run_id_fallback),
                    gate_ids=gate_ids_env or gate_ids,
                    assertions=_per_check_results(envelope),
                    evidence_index_ref=str(envelope.get("evidence_index_ref") or ""),
                    dispositions_ref=envelope.get("dispositions_ref"),
                    attempts_ledger_ref=envelope.get("attempts_ledger_ref"),
                    dry_run=False,
                    detail={**detail, "source": "results_envelope"},
                )
            # A JSON body with a missing/unknown verdict is NOT a valid envelope;
            # fall through to the exit-code map rather than raise on a bad verdict.
            detail["envelope_verdict"] = verdict

        verdict = _DRIVER_EXIT_VERDICT.get(proc.returncode, "fail")
        return LiveGateInvocation(
            verdict=verdict,
            run_id=run_id_fallback,
            gate_ids=gate_ids,
            dry_run=False,
            detail={**detail, "source": "exit_code_map"},
        )


#: The three failure attributions the wire model accepts on an assertion.
#: Anything else the envelope says is dropped rather than break the payload.
_KNOWN_DISPOSITIONS: frozenset[str] = frozenset({"counts", "instrument", "environment"})


def _per_check_results(envelope: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Every check's result from the envelope, in the wire's assertion shape.

    Protect-main (2026-09-07): the deploy stage has to say which of the
    sandbox checks failed, by name, before it refuses a merge — "failed 2 of 8
    checks (users_count, etag)". The envelope carries that per gate
    (``gates[].exit_code`` and ``gates[].assertions[].status``) and this
    backend used to drop it, so the stage saw only the list of names.

    Each gate's own assertions are carried through with the gate's id on
    them; a gate that failed by exit code but reported no failing assertion
    gets one assertion saying so, so a red gate is never counted green. Only
    the fields the wire model knows are copied, and a disposition it would
    refuse is left out — a malformed envelope must never stop a deploy.
    """
    results: list[dict[str, Any]] = []
    for gate in envelope.get("gates") or []:
        if not isinstance(gate, dict) or not gate.get("gate_id"):
            continue
        gate_id = str(gate["gate_id"])
        any_failed = False
        for raw in gate.get("assertions") or []:
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or "").strip().lower() or "fail"
            entry: dict[str, Any] = {
                "id": str(raw.get("id") or f"{gate_id}::assertion"),
                "gate_id": gate_id,
                "status": status,
            }
            for key in ("evidence_ref", "observed", "expected"):
                value = raw.get(key)
                if value is not None:
                    entry[key] = str(value)
            disposition = raw.get("disposition")
            if isinstance(disposition, str) and disposition in _KNOWN_DISPOSITIONS:
                entry["disposition"] = disposition
            if status != "pass":
                any_failed = True
            results.append(entry)
        exit_code = gate.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            if exit_code != 0 and not any_failed:
                results.append(
                    {
                        "id": f"{gate_id}::exit_code",
                        "gate_id": gate_id,
                        "status": "fail",
                        "observed": str(exit_code),
                        "expected": "0",
                    }
                )
    return tuple(results)


# ---------------------------------------------------------------------------
# Broker pre-flight
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrokerDiff:
    """The result of diffing live broker state against the F6 contract.

    Attributes:
        matches: True iff live broker state matches the contract.
        drifts: Human-readable drift descriptions (empty when matches).
        dry_run: True when this is a dry-run projection, not a live diff.
    """

    matches: bool
    drifts: tuple[str, ...] = ()
    dry_run: bool = False


@runtime_checkable
class BrokerInspector(Protocol):
    """Diffs live broker state against the F6 broker-contract section (LPA-16)."""

    def diff(self, broker_contract_ref: str) -> BrokerDiff:
        """Diff live streams/consumers against the contract at ``broker_contract_ref``."""
        ...


class UnconfiguredBrokerInspector:
    """Raises if invoked (FEAT-DD4F). The default until an inspector is wired."""

    def diff(self, broker_contract_ref: str) -> BrokerDiff:
        raise LiveGateSeamError(
            f"broker_preflight invoked for contract {broker_contract_ref!r} but no "
            "broker inspector is configured. Refusing to report the broker healthy "
            "without checking it (drift would fail the deploy silently). Wire a "
            "real inspector or run in dry-run mode."
        )


class DryRunBrokerInspector:
    """Records the intended broker diff without touching the live broker."""

    def diff(self, broker_contract_ref: str) -> BrokerDiff:
        logger.info(
            "dry-run broker_preflight: would diff live broker against %s",
            broker_contract_ref,
        )
        return BrokerDiff(matches=True, drifts=(), dry_run=True)
