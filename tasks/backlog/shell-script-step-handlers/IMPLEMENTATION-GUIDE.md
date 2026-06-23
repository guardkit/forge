# Implementation Guide — Shell Script Step Handlers (FEAT-SSH)

**Review:** TASK-REV-SSH1 · **Slug:** `shell-script-step-handlers` · **Focus:** security + correctness

Two concrete runbook step handlers — `deploy_compose` and `run_smoke_tests` —
that wrap existing shell scripts as subprocesses and register into the executor's
step-type registry (FEAT-RBX, upstream). Each runs a named script in a working
directory with an env-file available **by path**, captures combined
stdout/stderr, scrubs credentials at the capture boundary, and maps the script's
exit status to a verdict.

**Planning decisions applied:**
- Shared subprocess core + two thin handlers (single credential-scrub site).
- New sibling scrubber `scrub_process_output` in `src/forge/memory/redaction.py`.
- **Hardened** timeout + output size-cap now (extends ASSUM-008/009).
- ASSUM-013 (env-file pre-validation) stays **deferred** — missing env file
  surfaces as the script's own non-zero exit.
- Step-type keys: `deploy_compose` / `run_smoke_tests`.

---

## Data Flow: Read/Write Paths

```mermaid
flowchart LR
    subgraph Writes["Write Paths (script output)"]
        W1["script stdout"]
        W2["script stderr"]
        W3["env_file (path only)"]
    end

    subgraph Core["Subprocess Core (TASK-SSH-002)"]
        C1["_run_script_step()<br/>capture + timeout + size-cap"]
        C2["scrub_process_output()<br/>(TASK-SSH-001)"]
    end

    subgraph Storage["Step Result"]
        S1[("StepOutcome.result<br/>{exit_code, captured_output}")]
    end

    subgraph Reads["Read Paths"]
        R1["deploy_compose() (TASK-SSH-003)"]
        R2["run_smoke_tests() (TASK-SSH-004)"]
        R3["executor.update_step_status()<br/>(FEAT-RBX)"]
        R4["lifecycle events / publish<br/>(FEAT-RBX)"]
    end

    W1 -->|"combined"| C1
    W2 -->|"combined"| C1
    W3 -.->|"path injected as ENV_FILE, never read"| C1
    C1 -->|"once, at boundary"| C2
    C2 -->|"scrubbed (exit_code, output)"| R1
    C2 -->|"scrubbed (exit_code, output)"| R2
    R1 -->|"StepOutcome"| S1
    R2 -->|"StepOutcome"| S1
    S1 -->|"verbatim"| R3
    R3 -->|"scrubbed already"| R4

    style C2 fill:#cfc,stroke:#090
    style W3 fill:#ffd,stroke:#cc0
```

_What to look for: every output path funnels through the single `scrub_process_output`
site (green) before it can reach storage or publish. `env_file` (yellow) is a
dotted/never-read path — it is injected by path only. No disconnected read/write
paths: every write reaches a reader._

**Disconnection check:** ✅ none. Every capture write flows through scrub →
`StepOutcome.result` → executor persist/publish (both read paths have callers).

---

## Integration Contracts (sequence)

```mermaid
sequenceDiagram
    participant E as Executor (FEAT-RBX)
    participant H as deploy_compose / run_smoke_tests
    participant C as _run_script_step
    participant K as scrub_process_output
    participant P as Subprocess (script)

    E->>H: handler(step)
    H->>C: _run_script_step(cwd, script, env_file, timeout, cap)
    C->>P: spawn (cwd, ENV_FILE=path)
    P-->>C: stdout + stderr + exit_code (or TimeoutExpired -> kill, 124)
    C->>C: truncate to output_cap
    C->>K: scrub(output)  %% exactly once
    K-->>C: scrubbed output
    C-->>H: (exit_code, scrubbed_output)
    H-->>E: StepOutcome(passed if 0 else failed, result={...})
    Note over E,K: Output is scrubbed before it ever leaves the core — store and publish receive already-redacted text.
```

_What to look for: the scrub happens once, inside the core, before the handler or
executor ever sees the output. There is no "fetch-then-discard": every retrieved
value is passed onward scrubbed._

---

## Task Dependencies

```mermaid
graph TD
    T1[TASK-SSH-001: scrub_process_output] --> T2[TASK-SSH-002: subprocess core]
    T2 --> T3[TASK-SSH-003: deploy_compose]
    T2 --> T4[TASK-SSH-004: run_smoke_tests]
    T3 --> T5[TASK-SSH-005: register handlers]
    T4 --> T5
    T5 --> T6[TASK-SSH-006: integration test]

    style T3 fill:#cfc,stroke:#090
    style T4 fill:#cfc,stroke:#090
```

_Tasks with green background (the two handlers) can run in parallel._

| Wave | Tasks | Notes |
|------|-------|-------|
| 1 | TASK-SSH-001 | Scrubber foundation (security boundary) |
| 2 | TASK-SSH-002 | Subprocess core (timeout + size-cap + scrub) |
| 3 | TASK-SSH-003, TASK-SSH-004 | Two handlers — **parallel-safe** |
| 4 | TASK-SSH-005 | Registry wiring |
| 5 | TASK-SSH-006 | `@integration @slow` real-script proof |

---

## §4: Integration Contracts

### Contract: SCRUB_MARKERS
- **Producer task:** TASK-SSH-001 (`scrub_process_output`)
- **Consumer task(s):** TASK-SSH-002 (subprocess core)
- **Artifact type:** pure function contract (`str -> str`)
- **Format constraint:** Captured output MUST pass through `scrub_process_output`
  **exactly once**, at the capture boundary, before being returned/stored.
  Postgres DSNs → `***REDACTED-DSN***`; `password=`/`PGPASSWORD=` values →
  `***REDACTED-PASSWORD***`. Idempotent — already-redacted output is unchanged.
- **Validation method:** Coach runs the SCRUB_MARKERS seam test in TASK-SSH-002;
  asserts a planted DSN/password is absent from returned output and the marker is
  present.

### Contract: STEP_OUTCOME
- **Producer task:** TASK-SSH-002 (subprocess core return shape) →
  TASK-SSH-003 / TASK-SSH-004 (handler return)
- **Consumer task(s):** Executor (FEAT-RBX) via `update_step_status(result=…)`
- **Artifact type:** `StepOutcome(status, result)` value object
- **Format constraint:** `status` ∈ {`passed`, `failed`} for shell steps
  (`0 → passed`, non-zero → `failed`; for `run_smoke_tests` the exit status *is*
  the verdict). `result` is a JSON-serializable dict (`exit_code`,
  `captured_output`) — persisted verbatim by the executor.
- **Validation method:** Coach asserts handler output is a `StepOutcome` with a
  terminal status and a JSON-serializable `result` dict.

---

## Deferred risks (acknowledged)

| Assumption | Disposition | Residual risk |
|------------|-------------|---------------|
| ASSUM-008 (timeout) | **Hardened** in TASK-SSH-002 | none (killed at timeout → `failed`) |
| ASSUM-009 (size cap) | **Hardened** in TASK-SSH-002 | none (truncate-then-scrub) |
| ASSUM-013 (env-file pre-validation) | **Deferred** | a missing env file surfaces as the script's own non-zero exit, not a handler-side rejection (matches spec scenario) |

---

## AutoBuild

```bash
/feature-build FEAT-SSH
```
