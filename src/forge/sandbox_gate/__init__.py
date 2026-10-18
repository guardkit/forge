"""Starting and stopping a project's own sandbox (one sandbox per project,
stage S2, design section 2.3a, 18 October 2026).

Off by default: with no ``planning.sandbox_starter.url``, or no sandbox entry
saying ``idle_stop: true``, nothing is installed and every entry point passes
straight through.
"""

from forge.sandbox_gate.gate import (
    RedeployOutcome,
    SandboxGate,
    SandboxNotReady,
    admits,
    build_sandbox_gate,
    current_gate,
    install_gate,
    managed_sandboxes,
    using,
)

__all__ = [
    "RedeployOutcome",
    "SandboxGate",
    "SandboxNotReady",
    "admits",
    "build_sandbox_gate",
    "current_gate",
    "install_gate",
    "managed_sandboxes",
    "using",
]
