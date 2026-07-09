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
"""

from __future__ import annotations

import logging
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


class DryRunLiveGateInvoker:
    """Records the intended ``guardkit qa live-gate`` command without running it.

    Returns a ``dry_run=True`` invocation with an explicit ``pass`` verdict that
    is labelled as a dry run — NOT a claim that the gate passed. The stage marks
    the whole run dry-run in the F7 record and never publishes a live QA verdict
    consumers would mistake for a real one.
    """

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
            detail={"planned_command": planned},
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
