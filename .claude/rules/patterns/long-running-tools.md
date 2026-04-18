---
paths: tools/**/*.py, agents/**/*.py, agent.py, lib/**/*.py
---

<!--
  Vendored from the base langchain-deepagents template's
  patterns/long-running-tools.md. The orchestrator template does not
  `extends: langchain-deepagents`, so this rule is intentionally
  duplicated rather than overlaid. Keep this copy aligned with the base.
  If a future refactor switches this template to `extends:
  langchain-deepagents`, delete this copy and inherit from the base.
  Source of truth: ../../../langchain-deepagents/.claude/rules/patterns/long-running-tools.md
-->

# Long-Running Tools Pattern (Orchestrator Template)

Discipline for tools (and tool-shaped wrappers) whose work can exceed the
30s/240s latency thresholds enforced by typical MCP / serverless / gateway
deployments. The single goal: never let a synchronous tool surface a
generation-loop or retry-loop class of latency.

Fixes prevented: LES1 §4 POLR (Premature Online-Response timeout) — and the
associated description-contract bugs where a tool's docstring claims
"long-running" but its implementation `await`s synchronously.

## The 30s / 240s threshold rule (LES1 §4)

Two thresholds matter:

- **30s** — the soft threshold. Anything that *can* exceed 30s in p95 must
  not be exposed as a synchronous tool. Even if a current call is fast, if
  the worst-case path involves a generation loop, an external API, or
  retries, treat it as long-running.
- **240s** — the hard MCP timeout. Once a wrapped tool blocks for 240s,
  the MCP layer returns a generic timeout to the caller, the work is lost,
  and the caller has no way to recover state. This is POLR.

If a tool *can* exceed 30s in any plausible path, it MUST be implemented as
fire-and-forget + poll (see below), not as a synchronous `await`.

## Fire-and-forget + poll pattern

The shape:

1. The triggering tool (`run_thing`) starts the work in the background,
   stores a session record, and returns a `session_id` **immediately**
   (target: <1s).
2. A `run_thing_status` companion accepts a `session_id` and returns
   `{state: pending|running|done|failed, result?: ..., error?: ...}`.
3. A `run_thing_cancel` companion accepts a `session_id` and cancels the
   background work.

```python
# DO — fire-and-forget + poll
@tool(parse_docstring=True)
def run_thing(prompt: str) -> str:
    """Long-running — session tracked.

    Starts the generation in the background and returns a session_id.
    Poll run_thing_status(session_id) until state == "done"; then read
    result. Use run_thing_cancel(session_id) to abort.

    Args:
        prompt: The prompt to drive the long-running work.
    """
    session_id = _sessions.create(prompt)
    asyncio.create_task(_run_in_background(session_id, prompt))
    return json.dumps({"session_id": session_id, "state": "pending"})


@tool(parse_docstring=True)
def run_thing_status(session_id: str) -> str:
    """Return {state, result?, error?} for a session_id from run_thing.

    Args:
        session_id: The session_id returned by run_thing.
    """
    return json.dumps(_sessions.get(session_id))


@tool(parse_docstring=True)
def run_thing_cancel(session_id: str) -> str:
    """Cancel the background work for session_id.

    Args:
        session_id: The session_id returned by run_thing.
    """
    _sessions.cancel(session_id)
    return json.dumps({"session_id": session_id, "state": "cancelled"})
```

```python
# DON'T — synchronous "long-running" tool
@tool(parse_docstring=True)
async def run_thing(prompt: str) -> str:
    """Long-running — session tracked.

    Args:
        prompt: The prompt.
    """
    # The docstring lies — this blocks for 30-240s+
    return await implementer_agent.ainvoke(
        {"messages": [{"role": "user", "content": prompt}]}
    )
```

## Description is a contract

A tool's docstring is consumed by the model as part of the system prompt.
This template uses `@tool(parse_docstring=True)` (see
`patterns/tool-delegation.md` and the
`langchain-tool-decorator-specialist` guidance), which makes the docstring
*literally* the schema and description the model sees. If the docstring
says "long-running — session tracked", the implementation MUST return a
`session_id` and not block the caller. If the docstring says "returns the
result", the implementation MUST be synchronous with a worst-case latency
comfortably under the deployment threshold.

Mismatches cause two failure modes:

- **Caller plans for polling, tool blocks** — the model schedules a
  `_status` call that never happens, then times out at the MCP layer.
- **Caller plans for a result, tool returns a session_id** — the model
  treats the session_id JSON as the result, surfacing it to the user.

Audit the docstring/implementation pair on every change to either side.

## Latency-class separation

Do not share one tool shape across two latency classes. If a domain has
both a sync probe path (cheap lookup, <500ms) and a generation-loop path
(LLM call, 10-60s), they MUST be two distinct tools with distinct names
and docstrings — not one tool that branches on an argument.

```python
# DO — two latency classes, two tools
@tool(parse_docstring=True)
def lookup_record(record_id: str) -> str:
    """Sync — returns a record by id (<500ms).

    Args:
        record_id: The record id.
    """
    ...

@tool(parse_docstring=True)
def synthesise_record(prompt: str) -> str:
    """Long-running — session tracked. Returns session_id.

    Args:
        prompt: The synthesis prompt.
    """
    ...
```

```python
# DON'T — one tool, two latency classes, one docstring that can't be both
@tool(parse_docstring=True)
def get_record(record_id: str | None = None, prompt: str | None = None) -> str:
    """Returns a record (lookup or synthesis).

    Args:
        record_id: The record id (lookup path).
        prompt: The synthesis prompt (synthesis path).
    """
    if record_id is not None:
        return _lookup(record_id)        # 200ms
    return _synthesise(prompt)           # 45s
```

The shared shape forces the model to guess the latency class from the
docstring, which can't be right for both branches. The MCP wrapper (if any)
can't pick a timeout that's right for both branches. Split the tool.

## Orchestrator-specific NOTE

The four orchestrator tools (`analyse_context`, `plan_pipeline`,
`execute_command`, `verify_output`) are reasoning tools today and run
inline. None has a documented latency class.

- **`execute_command`** is the highest-risk surface: its current docstring
  says "executes a command" with no timeout discipline. Anything that
  shells out, calls an external API, or invokes a remote graph through
  this tool can plausibly exceed 30s. If you extend `execute_command` to
  cover a generation-loop or remote-graph dispatch, split it: keep the
  sync surface for cheap commands; add a fire-and-forget
  `execute_command_async` (+ `_status` / `_cancel`) for the long path.
- **The Implementer→Builder loop** dispatches to an async-remote subagent;
  any synchronous wrapper around the parent orchestrator graph that
  awaits the full Builder turn-around is itself an accumulated-latency
  surface and MUST be fire-and-forget + poll when wrapped behind MCP.

## When to use this pattern

- Any tool that calls an LLM, an external API, a subprocess, or a
  retry/revision loop that can plausibly exceed 30s in p95.
- Any tool you intend to expose behind MCP, a gateway, or a serverless
  function with a fixed request timeout.
- Any wrapper around the orchestrator graph (`agent.py:agent`) or its
  Implementer→Builder dispatch.

## When NOT to use this pattern

- Pure local lookups, in-memory transforms, and other paths whose worst
  case is comfortably under 1s.
- Tools used only inside the same Python process as the orchestrator
  graph where the parent already owns its own timeout discipline.

## Related

- Base template: `langchain-deepagents/.claude/rules/patterns/long-running-tools.md` (source of truth for this rule)
- Tool delegation: `.claude/rules/patterns/tool-delegation.md`
- LES1 §4 (POLR / description-contract)
- Review: `TASK-REV-LES1` §MEDIUM-2

Source: tools/*.py, agents/*.py
