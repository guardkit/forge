"""ASSUM-009 part 1: pause a LangGraph run at `interrupt()` and persist.

Spike: TASK-SPIKE-C1E9 (parent review TASK-REV-A7D3 §5).

What this script does:
    1. Defines a two-node LangGraph. Node A calls `interrupt(payload)`
       where `payload` is a richly-typed Pydantic model (`ApprovalRequest`,
       with nested model, UUID, datetime, and Literal fields).
    2. Runs the graph with a `SqliteSaver` checkpointer backed by
       `interrupt_state.sqlite` and a fixed `thread_id`.
    3. Exits while the graph is paused at the interrupt. The checkpoint
       remains on disk for the separate resume entry point
       (`interrupt_resume.py`) to pick up.

The resume entry point verifies that the value received from
`Command(resume=ApprovalDecision(...))` is a fully-typed Pydantic model
instance with nested fields intact.

Run with: `python spikes/deepagents-053/interrupt_graph.py`.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


# ---- Pydantic payload types (the object under test) ------------------------


class Requestor(BaseModel):
    """Nested model to prove nested-type hydration survives resume."""

    id: uuid.UUID
    handle: str


class ApprovalRequest(BaseModel):
    """Payload passed TO `interrupt()` (graph → human)."""

    action: Literal["deploy", "rollback", "pause"]
    requestor: Requestor
    requested_at: datetime
    reason: str


class ApprovalDecision(BaseModel):
    """Payload passed BACK via `Command(resume=...)` (human → graph)."""

    approved: bool
    decided_at: datetime
    decided_by: Requestor
    notes: str | None = None


# ---- Graph state -----------------------------------------------------------


class State(TypedDict, total=False):
    request: ApprovalRequest
    decision_type_name: str
    decision_is_pydantic: bool
    decision_decided_by_type: str
    decision_decided_at_type: str
    decision_approved: bool
    finalised: bool


# ---- Nodes -----------------------------------------------------------------


def propose(state: State) -> State:
    request = ApprovalRequest(
        action="deploy",
        requestor=Requestor(id=uuid.uuid4(), handle="rich"),
        requested_at=datetime.now(timezone.utc),
        reason="spike ASSUM-009 exercise",
    )
    # interrupt() blocks here; the resumed value comes back as its return.
    decision = interrupt(request)

    # --- the core evidence: what did we actually get back? -----------------
    return {
        "request": request,
        "decision_type_name": type(decision).__name__,
        "decision_is_pydantic": isinstance(decision, ApprovalDecision),
        "decision_decided_by_type": type(
            getattr(decision, "decided_by", None)
        ).__name__,
        "decision_decided_at_type": type(
            getattr(decision, "decided_at", None)
        ).__name__,
        "decision_approved": bool(getattr(decision, "approved", False)),
    }


def finalise(state: State) -> State:
    return {"finalised": True}


# ---- Graph factory (also imported by interrupt_resume.py) -----------------


SPIKE_DIR = Path(__file__).parent
DB_PATH = SPIKE_DIR / "interrupt_state.sqlite"
THREAD_ID = "assum-009-thread"


def build_graph():
    graph = StateGraph(State)
    graph.add_node("propose", propose)
    graph.add_node("finalise", finalise)
    graph.add_edge(START, "propose")
    graph.add_edge("propose", "finalise")
    graph.add_edge("finalise", END)
    return graph


def compiled_with_checkpointer(checkpointer):
    return build_graph().compile(checkpointer=checkpointer)


# Server-mode export. `langgraph dev` injects its own checkpointer, so the
# graph is compiled without one. Referenced from `spikes/deepagents-053/
# langgraph.json` by TASK-SPIKE-D2F7 to close the server-mode coverage gap
# on ASSUM-009. Reuses the same Pydantic payload types as direct-invoke mode.
graph = build_graph().compile()


# ---- Entry point: run-until-interrupt ------------------------------------


def main() -> int:
    # Clean start so the spike is idempotent.
    if DB_PATH.exists():
        os.remove(DB_PATH)

    with SqliteSaver.from_conn_string(str(DB_PATH)) as checkpointer:
        graph = compiled_with_checkpointer(checkpointer)
        config = {"configurable": {"thread_id": THREAD_ID}}

        print("starting graph; expecting pause at interrupt()...")
        result = graph.invoke({}, config=config)

        # With interrupt(), the result contains __interrupt__ metadata.
        interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
        print(f"graph returned: keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__}")
        if interrupts:
            for idx, ir in enumerate(interrupts):
                payload = getattr(ir, "value", ir)
                print(f"  interrupt[{idx}] value type: {type(payload).__name__}")
                print(f"  interrupt[{idx}] value repr: {payload!r}")

        # Check: state should be persisted at the interrupt.
        state = graph.get_state(config)
        print(f"next nodes (should be ['propose'] before resume): {state.next}")
        print()
        print(f"checkpoint persisted at: {DB_PATH}")
        print("now run `python spikes/deepagents-053/interrupt_resume.py` "
              "to resume with a typed ApprovalDecision.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
