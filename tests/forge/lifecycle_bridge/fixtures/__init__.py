"""SSE-stream fixtures for the TASK-FRR-PEB-003 translation contract test.

The fixtures live in this package so pytest discovers them at collection
time and so :class:`pathlib.Path` access in the contract test can be
relative to ``__file__``. The canonical fixture
``sse_stream_canonical.jsonl`` records both the success and failure
paths for the AutobuildState lifecycle — see the contract test module
for the full schema.

When the ``langgraph-api`` minor version is bumped (per AC-5 of
TASK-FRR-PEB-003), the fixture MUST be re-recorded against the new
sidecar — silent SSE-shape drift is the Option C risk this contract
test exists to mitigate.
"""

from __future__ import annotations

from pathlib import Path

CANONICAL_FIXTURE: Path = Path(__file__).parent / "sse_stream_canonical.jsonl"

__all__ = ["CANONICAL_FIXTURE"]
