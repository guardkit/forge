# API Contract — Subprocess (GuardKit + git + gh via DeepAgents `execute`)

> **Type:** Outbound shell calls
> **Transport:** DeepAgents built-in `execute` tool + thin wrappers
> **Related ADRs:** [ADR-ARCH-004](../../architecture/decisions/ADR-ARCH-004-full-guardkit-cli-tool-surface.md), [ADR-ARCH-020](../../architecture/decisions/ADR-ARCH-020-adopt-deepagents-builtins.md), [ADR-ARCH-023](../../architecture/decisions/ADR-ARCH-023-permissions-constitutional-safety.md), [ADR-ARCH-028](../../architecture/decisions/ADR-ARCH-028-ephemeral-per-build-worktrees.md)

---

## 1. Purpose

Every shell call Forge makes goes through DeepAgents `execute`, wrapped by one of three adapter sets:

- **`forge.adapters.guardkit`** — GuardKit CLI subcommands, with NATS streaming and progress parsing.
- **git / gh** — version control and PR operations, called directly from `autobuild_runner` via thin wrappers.
- **Arbitrary shell** — the reasoning model can invoke `execute` directly for build-specific commands (test runners, linters), constrained by `forge.yaml.permissions.shell.allowlist`.

All shell calls run within the build's worktree (`/var/forge/builds/{build_id}/`) — ephemeral, allowlisted path (ADR-ARCH-028).

---

## 2. Permissions Constraint (ADR-ARCH-023)

`forge.yaml.permissions` governs every `execute` call. The config schema:

```yaml
permissions:
  shell:
    allowlist:                             # Binary names or absolute paths
      - /usr/local/bin/guardkit
      - /usr/bin/git
      - /usr/bin/gh
      - python
      - pytest
      - pre-commit
      - npm
      - yarn
    working_directory_allowlist:
      - /var/forge/builds/*                # Glob allowlist — ephemeral worktrees only
    timeout_default_seconds: 600
  filesystem:
    read_allowlist:
      - /var/forge/builds/*
      - ~/.forge/cache/**
    write_allowlist:
      - /var/forge/builds/*
      - ~/.forge/forge.db
      - ~/.forge/logs/**
  network:
    allowlist:
      - api.github.com
      - objects.githubusercontent.com
      - *.googleapis.com                   # For Gemini + Graphiti embeddings
      - *.tail*.ts.net                     # Tailscale (Graphiti on NAS)
```

These permissions are **constitutional** — not reasoning-adjustable (ADR-ARCH-023). Violations are enforced by DeepAgents' permissions system; Forge relies on the framework, does not re-check at the adapter layer.

---

## 3. GuardKit Adapter

### 3.1 `execute` invocation shape

```python
# forge.adapters.guardkit.run()
async def run(
    subcommand: str,                       # e.g. "feature-spec"
    args: list[str],                       # positional + flags
    repo_path: Path,
    timeout_seconds: int = 600,
    with_nats_streaming: bool = True,
) -> GuardKitResult:
    cmd = [
        _resolve_guardkit_binary(),        # see the ladder below
        subcommand,
        *args,
        *(["--nats"] if with_nats_streaming else []),
    ]
    raw = await execute(
        command=cmd,
        cwd=str(repo_path),
        timeout=timeout_seconds,
    )
    return parse_guardkit_output(raw, subcommand=subcommand)
```

**`argv[0]` — the resolution ladder** (leg-invocation design pass 2026-08-02
§d stage-1 venue-B). `argv[0]` was the literal `/usr/local/bin/guardkit`, which
exists only inside the image, so outside the container this seam could not
spawn at all. It now walks the same ladder the long-running autobuild path
walks, resolved **once** per process and logged at INFO on first use:

1. `FORGE_GUARDKIT_PATH`, when it names an executable file (a set-but-unusable
   value warns and falls through);
2. `PATH` lookup for `guardkit` — **the container's rung**: `/usr/local/bin` is
   on the image's `PATH` and nothing ahead of it ships a file of that name
   (`/opt/venv/bin` holds `guardkit-py`), so the image's binary still wins. The
   found path is used **unresolved**, so the argv names the installed symlink,
   not its target;
3. `~/.agentecflow/bin/guardkit` — the local installer's launcher.

Every rung missing is not an exception: the boundary returns its usual
`status="failed"` result with a `guardkit_binary_not_found` warning naming each
rung it searched. Only a *success* is memoised, so a binary installed later in
the process's life is picked up.

### 3.2 Progress stream integration

When `--nats` is passed, GuardKit emits progress events on `pipeline.stage-complete.*` subjects directly (see [API-nats-pipeline-events.md §3.1](API-nats-pipeline-events.md#31-subject-family)). Forge subscribes in parallel to pick up interim progress for `forge status`.

The synchronous `execute` call still blocks on GuardKit exit; the NATS stream is just telemetry — the authoritative result is the parsed stdout/exit-code.

### 3.3 Context manifest resolution

Before every GuardKit invocation, `forge.adapters.guardkit.resolve_context_flags()` reads `.guardkit/context-manifest.yaml` from `repo_path` and adds `--context <path>` flags according to the command type:

| GuardKit command | Manifest categories loaded |
|---|---|
| `system-arch` | architecture + decisions |
| `system-design` | specs + decisions + contracts |
| `feature-spec` | specs + contracts + source |
| `feature-plan` | specs + decisions + architecture |
| `autobuild` | contracts + source |
| (any) | `internal_docs.always_include` |

See [DDR-005-cli-context-manifest-resolution.md](../decisions/DDR-005-cli-context-manifest-resolution.md) for the resolution algorithm and fallback for missing manifests.

### 3.4 Result schema

```python
class GuardKitResult(BaseModel):
    status: Literal["success", "failed", "timeout"]
    subcommand: str
    artefacts: list[str]                   # Absolute paths emitted by GuardKit
    coach_score: float | None              # Extracted from stdout when present
    criterion_breakdown: dict[str, float] | None
    detection_findings: list[dict[str, Any]] | None
    duration_secs: float
    stdout_tail: str                       # Last 4 KB for logging
    stderr: str | None
    exit_code: int
```

Parser is tolerant — unknown GuardKit output shapes degrade to `status="success"` with empty `artefacts` rather than failing the whole call. Reasoning model decides whether the stage actually produced useful output.

---

## 4. Git + gh Adapters

Thin wrappers under `forge.adapters.git`:

```python
async def prepare_worktree(build_id: str, repo: Path, branch: str) -> Path: ...
async def commit_all(worktree: Path, message: str) -> str: ...     # returns sha
async def push(worktree: Path, remote_branch: str) -> None: ...
async def create_pr(
    worktree: Path,
    title: str,
    body: str,
    base: str = "main",
    draft: bool = False,
) -> str: ...                                                     # returns PR URL
```

All methods:

- Use `execute` under the hood; respect permissions allowlist.
- Run inside the build's worktree path.
- Return structured errors on failure (ADR-ARCH-025 — never raise past the adapter boundary).

### 4.1 gh authentication

Uses `gh auth token` from `GH_TOKEN` env var (set by Docker deployment — not stored in `forge.yaml`). Forge does not manage GitHub credentials; credentials leave the container via subprocess only.

---

## 5. Worktree Lifecycle

Per ADR-ARCH-028:

1. **On `PREPARING`** — `forge.adapters.git.prepare_worktree(build_id, repo, branch)` creates `/var/forge/builds/{build_id}/`, initialises a fresh `git worktree`.
2. **On RUNNING** — all reads/writes/executes constrained to this path.
3. **On terminal state transition** — `forge.adapters.git.cleanup_worktree(build_id)` deletes the path. Forge retains SQLite + Graphiti history; source is reproducible from the PR.

Worktree cleanup is best-effort: failure to delete is logged but does not block state transition.

---

## 6. Return-value Contract

Every subprocess wrapper returns a typed Pydantic result (`GuardKitResult`, `GitOpResult`, etc.) and raises only at the adapter's internal edges. Tool-layer functions catch exceptions, convert to structured error strings, and return them as JSON (ADR-ARCH-025). The reasoning model sees `status: "error"` + message, never a raised exception.

---

## 7. Related

- Tool layer: [API-tool-layer.md](API-tool-layer.md) (`guardkit_*` wrapper tools)
- Subagents: [API-subagents.md](API-subagents.md) (`autobuild_runner` is the primary subprocess consumer)
- DDR: [DDR-005-cli-context-manifest-resolution.md](../decisions/DDR-005-cli-context-manifest-resolution.md)
