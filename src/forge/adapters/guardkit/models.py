"""Pydantic v2 result models for the GuardKit command invocation engine.

This module is the **declarative producer** for the canonical shape every
``forge.adapters.guardkit.run()`` call returns. It is consumed by the
downstream tool wrappers (TASK-GCI-009, TASK-GCI-010), the tolerant output
parser (TASK-GCI-004), and the subprocess wrapper (TASK-GCI-008).

Per ``docs/design/contracts/API-subprocess.md`` §3.4. The 4 KB
``stdout_tail`` field declaratively confirms ASSUM-003 in the schema.

Per the project boundary rules for declarative model layers (see
``forge.config.models``), this module must not import from any I/O or
transport package — it is a pure declarative schema layer with no logic,
no validators beyond what Pydantic provides by default, and no parser,
runner, or filesystem behaviour. Those concerns belong to TASK-GCI-004
and TASK-GCI-008.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class GuardKitWarning(BaseModel):
    """Single non-fatal warning surfaced by a GuardKit subcommand.

    A warning communicates a recoverable anomaly observed during a GuardKit
    invocation. Examples include ``"context_manifest_missing"`` and
    ``"context_manifest_cycle_detected"``. Warnings never imply ``status``
    is ``"failed"`` — that is the exit-code's job.
    """

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class GuardKitResult(BaseModel):
    """Canonical result returned by every ``forge.adapters.guardkit.run()`` call.

    Every field is structural: the model carries data only and contains no
    behaviour. Downstream consumers must rely solely on these fields and
    must not depend on any additional state.

    The ``status`` field is a typed ``Literal`` rather than an :class:`Enum`
    so the JSON wire shape is plain strings — no enum-encoding gymnastics
    when crossing process boundaries to the parser or subprocess wrapper.

    The optional fields (``coach_score``, ``criterion_breakdown``,
    ``detection_findings``, ``stderr``) are explicitly nullable because
    not every GuardKit subcommand emits them; the parser will set them to
    ``None`` when absent rather than picking a sentinel value.
    """

    status: Literal["success", "failed", "timeout"]
    subcommand: str
    artefacts: list[str] = Field(default_factory=list)
    coach_score: float | None = None
    criterion_breakdown: dict[str, float] | None = None
    detection_findings: list[dict[str, Any]] | None = None
    duration_secs: float
    stdout_tail: str = ""
    stderr: str | None = None
    exit_code: int
    warnings: list[GuardKitWarning] = Field(default_factory=list)
    #: The command was refused before anything was run, and the refusal was
    #: an answer in its own words (15 October 2026): nothing it could start
    #: is running.
    refused_before_running: bool = False
    #: No answer came back that could be read (15 October 2026): the command
    #: may have started and may still be running. A ``status="timeout"``
    #: result is read the same way.
    answer_lost: bool = False
    #: True only for a complete answer (15 October 2026): the command ran to
    #: an exit code and was read, or was refused in words before anything
    #: ran. Every other result — and any result built without saying — is
    #: unconfirmed, and the merge press keeps its hold for it.
    confirmed: bool = False


__all__ = [
    "GuardKitResult",
    "GuardKitWarning",
]
