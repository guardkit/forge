"""ASSUM-009 part 3: server-mode round-trip via `langgraph dev` + SDK.

Spike: TASK-SPIKE-D2F7 (follow-up to TASK-SPIKE-C1E9).

What this script does:
    1. Connects to a running `langgraph dev` server (default URL
       http://127.0.0.1:2024) that exposes the `assum009_interrupt` graph
       from `spikes/deepagents-053/langgraph.json`.
    2. Creates a thread, streams a run on an empty input — the graph pauses
       at `interrupt(ApprovalRequest(...))` inside `propose`.
    3. Resumes the run via `client.runs.wait(..., command={"resume": ...})`
       where the resume value is an `ApprovalDecision.model_dump(mode="json")`
       sent over HTTP — the serialization layer that is distinct from direct
       `CompiledStateGraph.invoke`.
    4. Reads the thread's final state and prints verdict rows mirroring
       the direct-mode verdict table in `deepagents-053-verification.md`.

Pass: the node-side `isinstance(decision, ApprovalDecision)` is True, nested
    Pydantic/UUID/datetime fields survive, and the graph runs past the
    interrupt to `finalise` — all observed through HTTP-serialized resume.
Fail: the node saw a dict / the nested fields degraded / the graph did not
    reach `finalise`. Any divergence vs. the direct-mode verdicts is itself
    a finding.

Prerequisites:
    - `pip install 'langgraph-cli[inmem]'` (adds server runtime)
    - Boot the server against the spike-local config:
          cd spikes/deepagents-053
          langgraph dev --no-browser
      (or equivalent; the driver auto-waits for the server port.)
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone

from langgraph_sdk import get_sync_client

# Local import — reuse the Pydantic payload types and the graph structure.
from interrupt_graph import ApprovalDecision, Requestor


ASSISTANT_ID = "assum009_interrupt"
DEFAULT_URL = os.environ.get("LANGGRAPH_URL", "http://127.0.0.1:2024")
STARTUP_TIMEOUT_S = 60.0


def _wait_for_server(client, timeout_s: float = STARTUP_TIMEOUT_S) -> None:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            client.assistants.search(limit=1)
            return
        except Exception as err:  # noqa: BLE001 — SDK wraps many transport errors
            last_err = err
            time.sleep(0.5)
    raise RuntimeError(
        f"langgraph dev server did not become ready within {timeout_s}s: {last_err!r}"
    )


def main() -> int:
    client = get_sync_client(url=DEFAULT_URL)
    _wait_for_server(client)

    thread = client.threads.create()
    thread_id = thread["thread_id"]
    print(f"thread_id: {thread_id}")

    # ---- kick off: expect an interrupt -----------------------------------
    run_result = client.runs.wait(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        input={},
    )

    # The returned state at the pause point should expose __interrupt__.
    interrupts = (
        run_result.get("__interrupt__")
        if isinstance(run_result, dict)
        else None
    )
    print(
        f"run-to-interrupt returned keys: "
        f"{list(run_result.keys()) if isinstance(run_result, dict) else type(run_result).__name__}"
    )
    if interrupts:
        for idx, ir in enumerate(interrupts):
            # Over the wire the SDK gives us dicts for interrupt records;
            # the value is whatever the graph passed to `interrupt(...)`.
            value = ir.get("value") if isinstance(ir, dict) else getattr(ir, "value", ir)
            print(f"  interrupt[{idx}] value type (SDK side): {type(value).__name__}")
            print(f"  interrupt[{idx}] value repr:            {value!r}")

    # Confirm paused state via the threads API.
    paused_state = client.threads.get_state(thread_id=thread_id)
    print(f"paused at nodes: {paused_state.get('next')}")

    # ---- resume with a typed ApprovalDecision ---------------------------
    decision = ApprovalDecision(
        approved=True,
        decided_at=datetime.now(timezone.utc),
        decided_by=Requestor(id=uuid.uuid4(), handle="rich"),
        notes="approved via spike server-mode resume",
    )
    resume_payload = decision.model_dump(mode="json")
    print(f"resuming with JSON-serialised ApprovalDecision: {resume_payload}")

    client.runs.wait(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        command={"resume": resume_payload},
    )

    final_state = client.threads.get_state(thread_id=thread_id)
    values = final_state.get("values", {}) if isinstance(final_state, dict) else {}

    # ---- Verdicts --------------------------------------------------------
    print()
    print("=" * 64)
    print("ASSUM-009 server-mode verdict (TASK-SPIKE-D2F7)")
    print("=" * 64)
    print(f"  resumed value type name (seen by node):   {values.get('decision_type_name')!r}")
    print(f"  isinstance(decision, ApprovalDecision):   {values.get('decision_is_pydantic')}")
    print(f"  nested decided_by type:                   {values.get('decision_decided_by_type')!r}")
    print(f"  decided_at type:                          {values.get('decision_decided_at_type')!r}")
    print(f"  decision.approved survived:               {values.get('decision_approved')}")
    print(f"  graph reached finalise node:              {values.get('finalised')}")

    ok = (
        values.get("decision_type_name") == "ApprovalDecision"
        and values.get("decision_is_pydantic") is True
        and values.get("decision_decided_by_type") == "Requestor"
        and values.get("decision_decided_at_type") == "datetime"
        and values.get("decision_approved") is True
        and values.get("finalised") is True
    )
    if ok:
        print(
            "RESULT: PASS — typed Pydantic payload round-tripped through "
            "interrupt() + Command(resume=...) over langgraph dev's HTTP layer."
        )
        return 0
    else:
        print(
            "RESULT: FAIL — server-mode typed round-trip degraded. "
            "Divergence vs. direct-invoke mode is itself a finding."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
