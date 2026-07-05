# API Contract — NATS Approval Protocol (interrupt round-trip)

> **Type:** Bidirectional — Forge publishes approval requests; Rich's client publishes responses
> **Transport:** NATS core pub/sub on `AGENTS` stream (captured for audit)
> **Related ADRs:** [ADR-ARCH-021](../../architecture/decisions/ADR-ARCH-021-paused-via-langgraph-interrupt.md) (Revision 10), [ADR-ARCH-026](../../architecture/decisions/ADR-ARCH-026-constitutional-rules-belt-and-braces.md)

---

## 1. Purpose

When `forge.gating.evaluate_gate()` returns `FLAG_FOR_REVIEW`, `HARD_STOP`, or `MANDATORY_HUMAN_APPROVAL`, the graph halts via LangGraph `interrupt()` and a parallel NATS protocol carries the request to Rich's client (Jarvis → notification adapter → Rich's phone/terminal) and carries the response back.

Under `langgraph dev` / LangGraph server mode (Forge's canonical deployment), the resume value arrives as `dict`, not as a typed Pydantic instance — **every call site MUST rehydrate via `forge.adapters.langgraph.resume_value_as(ApprovalResponsePayload, raw)` before attribute access** (ADR-ARCH-021 Revision 10). See [DDR-002-resume-value-rehydration-helper.md](../decisions/DDR-002-resume-value-rehydration-helper.md).

---

## 2. Subjects

| Direction | Template | Resolution example |
|---|---|---|
| Forge → Rich | `agents.approval.forge.{build_id}` | `agents.approval.forge.build-FEAT-A1B2-20260423170501` |
| Rich → Forge | `agents.approval.forge.{build_id}.response` | `agents.approval.forge.build-FEAT-A1B2-20260423170501.response` |
| Project-scoped | `Topics.for_project("finproxy", "agents.approval.forge.{build_id}")` | `finproxy.agents.approval.forge.…` |

**Why `agents.approval.forge.{build_id}` not `agents.approval.forge.{task_id}`:**

Forge's "task" in the pause sense is always the current build. Keying by `build_id` aligns with SQLite `builds.build_id` (the crash-recovery anchor) and keeps the subject stable across re-emissions if `interrupt()` re-fires after restart.

---

## 3. Approval Request (Forge → Rich)

### 3.1 Envelope + payload

Reuses `nats-core.MessageEnvelope` (`event_type=EventType.APPROVAL_REQUEST`) + `nats-core.ApprovalRequestPayload`:

```python
class ApprovalRequestPayload(BaseModel):
    request_id: str                        # correlation_id threaded back on response
    agent_id: str = "forge"                # Always "forge" for gating pauses
    action_description: str                # One-line summary for Rich: "Architecture review flagged 0.52"
    risk_level: Literal["low", "medium", "high"]
    details: dict[str, Any]                # See §3.2 — Forge convention
    timeout_seconds: int = 300
```

### 3.2 `details` convention

Forge populates `details` with a consistent shape so notification adapters can render it:

```python
details = {
    "build_id": "build-FEAT-A1B2-20260423170501",
    "feature_id": "FEAT-A1B2",
    "stage_label": "Architecture Review",                 # Emergent label
    "gate_mode": "FLAG_FOR_REVIEW",                       # | "HARD_STOP" | "MANDATORY_HUMAN_APPROVAL"
    "coach_score": 0.52,
    "criterion_breakdown": {"fidelity": 0.4, "rigour": 0.6, ...},
    "detection_findings": [
        {"pattern": "SCOPE_CREEP", "severity": "medium", "evidence": "..."},
    ],
    "rationale": "Reasoning model explanation of why this flagged",
    "evidence_priors": [                                  # Which retrieved priors informed the call
        {"entity_id": "...", "summary": "..."},
    ],
    "artefact_paths": [
        "/var/forge/builds/build-FEAT-A1B2-20260423170501/docs/architecture/..."
    ],
    "resume_options": ["approve", "reject", "defer", "override"],
}
```

### 3.3 Risk-level derivation

| Gate mode | `risk_level` |
|---|---|
| `FLAG_FOR_REVIEW` | `"low"` if `coach_score ≥ 0.65`; else `"medium"` |
| `HARD_STOP` | `"high"` |
| `MANDATORY_HUMAN_APPROVAL` | `"medium"` (unconditional human review, not an emergency) |

Published by Forge with `source_id="forge"` and `correlation_id` = the build's correlation_id from `BuildQueuedPayload`.

---

## 4. Approval Response (Rich → Forge)

### 4.1 Envelope + payload

```python
class ApprovalResponsePayload(BaseModel):
    request_id: str                        # Must echo ApprovalRequestPayload.request_id
    decision: Literal["approve", "reject", "defer", "override"]
    responder: str                         # "rich" / jarvis adapter id
    reason: str | None = None
    # Optional Forge-specific extension — passed through without mandatory validation
    override_context: dict[str, Any] | None = None
```

### 4.2 Rehydration contract

Under `langgraph dev` / server mode, the value returned by `interrupt()` is `dict`. The adapter MUST:

```python
from forge.adapters.langgraph import resume_value_as

raw = interrupt({...})                    # See API-tool-layer.md "approval_tools.request_approval"
response = resume_value_as(ApprovalResponsePayload, raw)
# Now safe to access response.decision, response.responder, etc.
```

The `isinstance` short-circuit in `resume_value_as` makes this a no-op in direct-invoke mode (where typed round-trip already holds) — no call-site churn if the deferred Option B serde fix lands later (ADR-ARCH-021 Revision 10).

---

## 5. Consumer Implementation — Response Subscriber

```python
# forge.adapters.nats.approval_subscriber
async def await_approval(build_id: str, timeout_seconds: int = 3600) -> ApprovalResponsePayload:
    subject = f"agents.approval.forge.{build_id}.response"
    sub = await nc.subscribe(subject, max_msgs=1)
    try:
        msg = await sub.next_msg(timeout=timeout_seconds)
        envelope = MessageEnvelope.model_validate_json(msg.data)
        return ApprovalResponsePayload.model_validate(envelope.payload)
    finally:
        await sub.unsubscribe()
```

This subscriber runs inside the NATS approval consumer that the LangGraph runtime uses to feed `interrupt()` resume values. In server mode, the runtime's resume API delivers a `dict` to the `interrupt()` call-site, which then goes through `resume_value_as`.

---

## 6. Idempotency (Crash Recovery)

Per ADR-ARCH-021:

- On Forge crash during PAUSED state, the in-graph `interrupt()` is lost but SQLite marks `PAUSED` and the `ApprovalRequestPayload` was already published.
- On restart, Forge re-enters PAUSED for that build and **re-emits** `ApprovalRequestPayload`. This is deliberate — it guarantees Rich sees the request even if the first one was missed.
- **Responders MUST be idempotent on `request_id`** — first response wins; duplicates are discarded.
- Rich's client / Jarvis adapter enforces this via a short-TTL `processed_request_ids` set in memory.

---

## 7. Timeout Handling

| Scenario | Behaviour |
|---|---|
| `timeout_seconds` elapses (default 300 for initial request; refresh up to `forge.yaml.approval.max_wait_seconds` ≈ 3600) | Forge emits a repeat `ApprovalRequestPayload` with incremented attempt count; continues waiting. |
| Rich runs `forge cancel FEAT-XXX` while paused | NATS `agents.command.forge` receives cancel; resume subscriber injects synthetic `ApprovalResponsePayload(decision="reject", responder="rich", reason="cli cancel")`; graph resumes, state → CANCELLED. |
| Rich runs `forge skip FEAT-XXX` | Synthetic `ApprovalResponsePayload(decision="override", responder="rich", reason="cli skip")`; graph resumes, specific stage skipped. |

### 7.1 Responder identity — `forge.yaml.approval.expected_approver` (TASK-JNB-101)

The production `ApprovalSubscriber` accepts a response only when its
`decided_by` **string-equals `approval.expected_approver` verbatim** — no
trimming, no case folding (jarvis publishes `JARVIS_SLACK_DECIDED_BY`
untouched; forge compares with `!=`). The pinned shared value is **`rich`**
(operator-chosen 2026-07-04) and is the model default, so a forge.yaml that
omits the key still enforces it. `expected_approver: null` selects permissive
mode (any responder — dev only). A mismatch is **silent** from the operator's
view: jarvis logs `slack_reply_decision_published`, forge logs
`approval_subscriber: unrecognised responder … NOT resuming`, and the build
stays paused — TASK-JNB-107's first troubleshooting step is checking these
two values. Deploy-order note: `ApprovalConfig` is `extra="forbid"`, so a
forge.yaml carrying this key boots only on images that define the field.

Resume-emit note (TASK-JNB-101): `pipeline.build-resumed` is published only
for `approve` / `override` decisions. A `reject` terminates in CANCELLED and
signals via `pipeline.build-cancelled` (TASK-JNB-102); `defer` re-publishes
the request and stays paused.

---

## 8. Constitutional Rule — PR Review

Per ADR-ARCH-026 belt+braces:

- **Prompt**: system-prompt template `SAFETY_CONSTITUTION` asserts PR review is always human (never auto-approved).
- **Executor**: `forge.gating.evaluate_gate()` has a hardcoded branch — any `tool_name in {"review_pr", "create_pr_after_review"}` → `GateMode.MANDATORY_HUMAN_APPROVAL` regardless of score or priors.

Both must be wired independently. Loss of either is a constitutional regression.

---

## 9. Related

- Data model: [DM-gating.md](../models/DM-gating.md)
- Pipeline events: [API-nats-pipeline-events.md](API-nats-pipeline-events.md)
- Tool layer: [API-tool-layer.md](API-tool-layer.md) (`approval_tools.request_approval`)
- DDR: [DDR-002-resume-value-rehydration-helper.md](../decisions/DDR-002-resume-value-rehydration-helper.md)
