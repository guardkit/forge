# Runbook: FEAT-FORGE-008 Validation — Forge Step 6 Gates

**Status:** Ready for execution (autobuild merged 2026-04-29 via commit `2f13eac`)
**Purpose:** Drive the merged Forge orchestrator through every Step 6 validation gate so the build can be declared canonical and Step 7 (FinProxy first real run) is unblocked.
**Machines:**
- **Local** (MacBook or workstation) — Phases 1–2 (pytest + CLI smoke)
- **GB10** (`promaxgb10-41b1`) — Phases 3–5 (NATS-dependent gates) and Phases 6.1–6.3 (LES1 production-image gates)
- **Both** — Phase 6.4 (canonical-freeze live-verification logged in `command-history.md`)

> **Note for `promaxgb10-41b1` operators:** on this workstation `/etc/hosts`
> maps `promaxgb10-41b1` to `127.0.0.1`, so "Local" and "GB10" phases run
> against the same machine. Skip the `ssh promaxgb10-41b1` prefixes (or treat
> them as no-ops) when working from this host. — *gap-fold 2026-04-30
> (TASK-F8-006 / AC-13)*

**Predecessor:** Build plan `forge/docs/research/ideas/forge-build-plan.md` Step 5 → ✅ 8/8 autobuilds complete (FEAT-FORGE-008 merged via `2f13eac`; autobuild metadata `22c0b1f`; worktree cleanup `51ae6a6`). 14/14 tasks across 7 waves; 86% first-attempt pass; 2 SDK ceiling hits on TASK-MBC8-008/009 (resolved on turn 2).

**Expected duration:** ~3–5 hours total (Phase 1: ~5 min · Phase 2: ~10 min · Phases 3–5: ~45 min on GB10 · Phase 6 LES1 gates: ~2–3 hours including production-image rebuild · Phase 7 wrap-up: ~15 min).

**Outputs:**
- A `RESULTS-FEAT-FORGE-008-validation.md` file alongside this runbook capturing per-gate pass/fail with evidence pointers.
- An entry in `forge/docs/history/command-history.md` per the canonical-freeze gate (Phase 6.4).
- If everything is green: an updated build plan footer (`forge-build-plan.md`) declaring Step 6 ✅ and Step 7 ready.

---

## Why this runbook exists

FEAT-FORGE-008 added Mode B (Feature) and Mode C (Review-Fix) on top of the FEAT-FORGE-001..007 substrate. The unit tests Coach approved per task are **necessary but not sufficient** — they prove each piece works in isolation but miss composition failures that only surface against the live NATS layer, the production specialist-agent image, and the CLI surface end-to-end. This is exactly the failure pattern the LES1 lessons (TASK-MDF-CMDW / PORT / ARFS) caught on the specialist-agent build: 100% green unit tests, broken integration on a clean machine.

Each gate below has a cited evidence pointer back to a TASK-* id or the build plan. Treat them as blockers, not nice-to-haves.

---

## Phase 0: Pre-flight

### 0.1 Confirm you are on the merged main branch

```bash
cd ~/Projects/appmilla_github/forge
git status
git log --oneline -5
```

**Pass:** Working tree clean (or only intentional local mods). `git log` shows `2f13eac feat: Mode B Feature & Mode C Review-Fix via AutoBuild (FEAT-FORGE-008)` somewhere in recent history.

**If you are still on a worktree branch:**
```bash
git worktree list
# If FEAT-FORGE-008 worktree is still present, bail to main:
git -C ~/Projects/appmilla_github/forge checkout main
git pull --ff-only
```

### 0.2 Confirm the FEAT-FORGE-008 artefacts are present

```bash
ls .guardkit/features/FEAT-FORGE-008.yaml
ls tasks/backlog/mode-b-feature-and-mode-c-review-fix/IMPLEMENTATION-GUIDE.md
ls features/mode-b-feature-and-mode-c-review-fix/mode-b-feature-and-mode-c-review-fix.feature
```

**Pass:** All three exist. If any are missing, the merge was incomplete — re-check `2f13eac`.

### 0.3 Confirm the Mode B + Mode C tests landed

```bash
ls tests/integration/test_mode_b_smoke_e2e.py \
   tests/integration/test_mode_c_smoke_e2e.py \
   tests/integration/test_cross_mode_concurrency.py \
   tests/integration/test_mode_b_c_crash_recovery.py
```

**Pass:** All four files exist. These are the integration assertions from TASK-MBC8-010, 011, 013, 014.

### 0.4 Confirm the Python environment is current

This repo's `pyproject.toml` declares `nats-core>=0.2.0,<0.3` via
`[tool.uv.sources]` (sibling `../nats-core` editable install), which **only
`uv` resolves**. Plain `pip install -e .` cannot find `nats-core` and the
install will fail. Use `uv` for the canonical install path.

```bash
python --version
# Expect 3.12+ (project default — confirm with pyproject.toml if unsure)

# 1. Install uv first if not already present (canonical path)
which uv || curl -LsSf https://astral.sh/uv/install.sh | sh
# Re-source so $PATH picks up ~/.local/bin (uv default)
export PATH="$HOME/.local/bin:$PATH"
uv --version

# 2. Editable install with all extras (uv reads [tool.uv.sources];
#    `memory` resolves the sibling ../fleet-memory editable install —
#    the gate's priors read, matching the image's .[providers,memory])
uv pip install -e ".[providers,memory,dev]" 2>&1 | tail -5
```

**Pass:** Install succeeds.

**Fallback for non-uv hosts (Ubuntu 24.04 / PEP 668):** if you cannot install
`uv`, you can run plain pip with `--break-system-packages` (note this skips
`[tool.uv.sources]` and will fail on the `nats-core` dep — only useful if
`nats-core` is already importable on the host):

```bash
pip install --user --break-system-packages -e '.[providers,memory,dev]'
```

**Stale-editable-install rewire (the FEAT-FORGE-005 trap):** if a previous
editable install pointed at a deleted worktree (e.g.
`.guardkit/worktrees/FEAT-FORGE-*`), pip refuses to overwrite it. Rewire
with:

```bash
pip install --user --force-reinstall --no-deps -e .
```

**If `pytest-asyncio` is missing**, the recent dep work in `3092c3a` should
have already pinned it — re-run `uv pip install -e '.[dev]'`. If still
missing, check `pyproject.toml` for the optional-dependencies block.

> *gap-fold 2026-04-30 (TASK-F8-006 / AC-1)*

### 0.5 Confirm the `forge` CLI is callable

`pip install --user` (and `uv pip install`) put the `forge` console script
in `~/.local/bin`. On a fresh shell this may not be on `PATH`.

```bash
# Make sure ~/.local/bin is on PATH (idempotent)
case ":$PATH:" in
  *":$HOME/.local/bin:"*) echo "~/.local/bin already on PATH" ;;
  *) export PATH="$HOME/.local/bin:$PATH"; echo "Prepended ~/.local/bin to PATH" ;;
esac

which forge
forge --help | head -20
forge queue --help
```

**Pass:** `forge` resolves to a console script. `forge queue --help` shows the new `--mode` flag (added by TASK-MBC8-009).

**If `--mode` is not in the help output:** the FEAT-FORGE-008 install is stale. Re-run `uv pip install -e .` (or the fallback from §0.4) and re-check.

> *gap-fold 2026-04-30 (TASK-F8-006 / AC-2)*

### 0.6 (GB10 only) Confirm GB10 is reachable and NATS is up

Skip this on local; required for Phases 3–5. (On `promaxgb10-41b1` the
"GB10" host is the local machine — see the note at the top of the runbook.)

```bash
ssh promaxgb10-41b1 'echo OK'
ssh promaxgb10-41b1 'nats-server --version 2>/dev/null || systemctl status nats 2>/dev/null | head -5'
ssh promaxgb10-41b1 'docker ps --format "{{.Names}}\t{{.Status}}" | grep -i nats'
```

**Pass:** SSH succeeds. NATS server is running (either as a systemd service or a docker container).

**Phase scope of NATS requirement:**

| Phases | NATS requirement | Throwaway `nats:latest` OK? |
|--------|------------------|-----------------------------|
| 2–3    | Any reachable NATS server (pub/sub round-trip only) | ✅ yes |
| 4+     | Canonically *provisioned* JetStream (streams + KV stores per `nats-core` topology) | ❌ no — must be the provisioned server |

**If NATS is not running and you only need Phases 2–3:** start a throwaway
ephemeral NATS in docker for the duration of the smoke. This is enough to
satisfy `forge queue`'s `pipeline.>` publish path, but JetStream streams +
KV stores are **not** provisioned, so Phase 4+ gates will not work.

```bash
# Throwaway NATS for Phases 2–3 only (no JetStream provisioning)
docker run -d --name forge-runbook-nats --network host nats:latest -js
sleep 2
docker logs forge-runbook-nats 2>&1 | tail -3
# Cleanup at end of Phase 3:
# docker stop forge-runbook-nats && docker rm forge-runbook-nats
```

**For Phase 4+ (and the canonical Step 6 freeze):** follow the
`nats-infrastructure` repo's provisioning runbook — `docker compose up -d`
plus `provision-streams.sh` and `provision-kv.sh`. Per build plan §"Hard
Prerequisites": **a fresh-volume NATS without explicit provisioning will
accept publishes (PubAck) but not retain or deliver them** — exactly the
MacBook failure mode. Do not skip this.

The canonical source of truth for **what Forge requires from the
provisioned server** (streams, durable consumer config, KV buckets,
auth/ACL, health probes) is the handoff document:
[`docs/handoffs/F8-007a-nats-canonical-provisioning.md`](../handoffs/F8-007a-nats-canonical-provisioning.md).
If a Phase 4+ gate fails because of a missing stream, missing KV
bucket, or wrong consumer config, cross-check that document first —
its §3–§5 enumerate the exact floors `forge` depends on. (TASK-F8-007a
tracks the cross-repo provisioning hand-off itself.)

> *gap-fold 2026-04-30 (TASK-F8-006 / AC-3, TASK-F8-007a / AC-3)*

---

## Phase 1: Local pytest gate

The cheapest signal. If this is red, every later gate is invalid. Runs on the local machine; no NATS or specialist-agent dependency.

### 1.1 Run the full forge test suite

```bash
echo "=== Phase 1.1: Full pytest suite ==="
cd ~/Projects/appmilla_github/forge

# Capture a clean log so the RESULTS file can cite line counts.
# `tests/bdd/test_infrastructure_coordination.py` collects cleanly as of
# TASK-F8-001 (commit `d0c2f81`) which landed `forge.build.git_operations` +
# `forge.build.test_verification`. No `--ignore` is needed.
pytest -q --tb=short 2>&1 | tee /tmp/forge-pytest-phase1.log
echo "Exit code: $?"
```

**Pass:** All tests green. The expected scope:
- `tests/forge/` — unit tests for FEAT-FORGE-001..007 (regression baseline)
- `tests/integration/` — including the four new Mode B/C suites (`test_mode_b_smoke_e2e.py`, `test_mode_c_smoke_e2e.py`, `test_cross_mode_concurrency.py`, `test_mode_b_c_crash_recovery.py`)
- `tests/bdd/` — including `test_feat_forge_008.py` (the BDD bindings from TASK-MBC8-012)
- `tests/hardening/` — substrate hardening tests
- `tests/unit/` — module-level unit tests

**If any test is red:**
1. Check whether it's a Mode B/C test (TASK-MBC8-*) or a substrate test. Substrate failures mean the merge regressed FEAT-FORGE-007 (see TASK-MBC8-008 acceptance: "Mode A behaviour byte-identical").
2. Capture the failure to `/tmp/forge-pytest-phase1.log` and triage in a separate task. **Do not proceed to later phases with red tests** — they will only add noise.

### 1.2 Run the FEAT-FORGE-008 BDD bindings explicitly

The full pytest sweep above already runs these, but run them in isolation to confirm the @task tags are honoured.

```bash
echo "=== Phase 1.2: FEAT-FORGE-008 BDD bindings ==="
pytest tests/bdd/test_feat_forge_008.py -v --tb=short 2>&1 | tee /tmp/forge-bdd-008.log
```

**Pass:** All 56 scenarios green (39 @mode-b, 28 @mode-c, with overlap on shared substrate). No `@skip` or `@wip` markers fired.

**Capture:** scenario count from the test output for the RESULTS file.

### 1.3 Confirm Mode A regression suite is byte-identical

This is the FEAT-FORGE-007 substrate guard from TASK-MBC8-008 acceptance — Mode A must remain unchanged.

```bash
echo "=== Phase 1.3: Mode A regression ==="
pytest tests/integration/test_mode_a_smoke.py \
       tests/integration/test_mode_a_concurrency_and_integrity.py \
       tests/integration/test_mode_a_crash_recovery.py \
       -v --tb=short 2>&1 | tee /tmp/forge-mode-a-regression.log
```

> **Removed path:** earlier drafts of this block also ran
> `tests/bdd/test_feat_forge_007.py`, but no such file exists in the repo
> (the FEAT-FORGE-007 BDD bindings were never written). If those bindings
> are needed later, file a follow-up task to author them and add the path
> back here. — *gap-fold 2026-04-30 (TASK-F8-006 / AC-5)*

**Pass:** All Mode A tests green. **If any are red, FEAT-FORGE-008 broke the Mode A branch in `Supervisor.next_turn` — stop and triage immediately.**

---

## Phase 2: CLI smoke (local, requires NATS)

Validates the canonical CLI surface (`forge queue` per anchor §5) end-to-end.

> **NATS truth (gap-fold 2026-04-30 / AC-9):** `forge queue` **always**
> publishes to `$FORGE_NATS_URL` (default `nats://127.0.0.1:4222`). There
> is no built-in fake-mode seam. Either start a local NATS first (the
> throwaway `nats:latest -js` from §0.6 is enough for Phase 2), or skip
> Phase 2's NATS-dependent gates. Earlier drafts claimed "this stubs NATS
> via the integration adapter" — that claim was false.

### 2.1 Initialise a fresh forge state directory + minimal config

This phase produces three pieces of state up-front and re-uses them across
2.2–2.6:

1. `$FORGE_HOME` — tmp dir holding `forge.db` and the YAML stubs.
2. `$FORGE_HOME/forge.yaml` — minimal valid forge config (allowlist only).
3. `$FORGE_HOME/feature-stub.yaml` — minimal `--feature-yaml` stub for the
   Mode B/C calls.

Database routing is set **once** via `FORGE_DB_PATH` (the env-var hook the
queue loader reads); each subcommand uses the appropriate flag below.

> **Per-command flag truth (gap-fold 2026-04-30 / AC-6, refined by F008-RERUN-001):**
> - `--config FILE` is a **top-level** option (`forge --config FILE <subcommand>`).
>   Subcommands reject it. Earlier drafts placed it after the subcommand name —
>   that form fails at the click parser. Or omit `--config` entirely and let
>   the loader auto-pick `./forge.yaml` from CWD.
> - `forge queue` — reads `$FORGE_DB_PATH` from env (no `--db-path` flag).
>   Always requires `--feature-yaml FILE` (Mode A, B and C alike — *not* just
>   Mode B/C as earlier drafts implied; the CLI rejects with `Missing option
>   '--feature-yaml'`).
> - `forge history` — accepts `--db PATH` (note: `--db`, not `--db-path`).
> - `forge status` — accepts `--db-path PATH`.
> - `--repo` is `click.Path(exists=True)`: the path must already exist on the
>   filesystem. Placeholder strings like `guardkit/test-project` fail
>   immediately. Use a real, allowlisted checkout (e.g. the live forge tree).

> **`forge.yaml` allowlist truth (F008-RERUN-001 / AC-2):**
> `ForgeConfig` validators reject relative or `~`-prefixed paths in
> `permissions.filesystem.allowlist`; entries must be absolute. The heredoc
> below uses `<<EOF` (no quotes) so `$HOME` expands at write-time, producing
> an absolute path. Earlier drafts used `<<'EOF'` plus a literal `~/...` —
> the loader fails with
> `ValidationError: filesystem.allowlist entries must be absolute paths`.

```bash
echo "=== Phase 2.1: Fresh state directory + config ==="

# Fresh tmp dir so this doesn't pollute the user's actual ~/.forge
export FORGE_HOME=$(mktemp -d -t forge-validation-XXXXXX)
export FORGE_DB_PATH="$FORGE_HOME/forge.db"
# Pin the repo path (used as `--repo` on every queue invocation below).
# Must point at a real checkout AND appear in the allowlist below.
export FORGE_REPO_PATH="$HOME/Projects/appmilla_github/forge"
echo "FORGE_HOME=$FORGE_HOME"
echo "FORGE_DB_PATH=$FORGE_DB_PATH"
echo "FORGE_REPO_PATH=$FORGE_REPO_PATH"

# Minimal forge.yaml (gap-fold 2026-04-30 / AC-7, F008-RERUN-001 / AC-2).
# `permissions.filesystem.allowlist` is the only mandatory field. Entries
# must be absolute paths — the unquoted heredoc `<<EOF` lets `$HOME` and
# `$FORGE_REPO_PATH` expand inline so the file ends up with literal absolute
# paths.
cat > "$FORGE_HOME/forge.yaml" <<EOF
permissions:
  filesystem:
    allowlist:
      - /tmp
      - $FORGE_REPO_PATH
EOF

# Minimal per-feature stub YAML (gap-fold 2026-04-30 / AC-8).
# `forge queue` requires `--feature-yaml FILE` on EVERY mode (A, B and C);
# the file must exist on disk. The stub below is the smallest shape the
# loader accepts; see src/forge/config/feature_loader.py for the full schema.
cat > "$FORGE_HOME/feature-stub.yaml" <<'EOF'
acceptance_criteria:
  - id: AC-1
    text: stub criterion for runbook Phase 2 smoke
EOF

ls -la "$FORGE_HOME"
```

**Pass:** Tmp dir created with `forge.yaml` and `feature-stub.yaml` present.
Note the path for cleanup at end of phase.

### 2.2 Mode A queue smoke (regression baseline)

Mode A is the existing default; it should keep working unchanged.

> **Identifier note (gap-fold 2026-04-30 / AC-10):** the wire schema regex
> `^FEAT-[A-Z0-9]{3,12}$` (in `nats_core.events._pipeline.BuildQueuedPayload`)
> rejects inner hyphens. The CLI's looser `validate_feature_id` lets the
> hyphenated forms (`FEAT-TEST-MA`) through, but the publish step then
> fails at the pydantic boundary. Examples below use the wire-valid forms
> `FEAT-TESTMA` / `FEAT-TESTMB` / `FEAT-TESTMC`.

```bash
echo "=== Phase 2.2: Mode A queue ==="
forge --config "$FORGE_HOME/forge.yaml" queue FEAT-TESTMA \
    --repo "$FORGE_REPO_PATH" --branch main --mode a \
    --feature-yaml "$FORGE_HOME/feature-stub.yaml"
forge --config "$FORGE_HOME/forge.yaml" status --db-path "$FORGE_DB_PATH"
forge --config "$FORGE_HOME/forge.yaml" history --feature FEAT-TESTMA --db "$FORGE_DB_PATH"
```

**Pass:** Queue command exits 0. `status` lists FEAT-TESTMA with `mode=mode-a`. `history` shows at least the queue event.

### 2.3 Mode B queue smoke (NEW — FEAT-FORGE-008)

```bash
echo "=== Phase 2.3: Mode B queue ==="
forge --config "$FORGE_HOME/forge.yaml" queue FEAT-TESTMB \
    --repo "$FORGE_REPO_PATH" --branch main --mode b \
    --feature-yaml "$FORGE_HOME/feature-stub.yaml"
forge --config "$FORGE_HOME/forge.yaml" status --db-path "$FORGE_DB_PATH"
forge --config "$FORGE_HOME/forge.yaml" history --feature FEAT-TESTMB --db "$FORGE_DB_PATH"
```

**Pass:** `status` shows `mode=mode-b` for FEAT-TESTMB. **If `mode` column is absent from `status` output**, TASK-MBC8-009 acceptance was incomplete — file a follow-up.

### 2.4 Mode B single-feature constraint (boundary check)

Per ASSUM-006: Mode B operates on exactly one feature. The CLI parser must reject multi-feature input.

```bash
echo "=== Phase 2.4: Mode B single-feature boundary ==="
forge --config "$FORGE_HOME/forge.yaml" queue FEAT-TESTMBMULT FEAT-TESTMBEXTR \
    --repo "$FORGE_REPO_PATH" --branch main --mode b \
    --feature-yaml "$FORGE_HOME/feature-stub.yaml" 2>&1 | tee /tmp/mb-multi-reject.log
echo "Exit code: ${PIPESTATUS[0]}"
```

**Pass:** Non-zero exit code. Stderr explains "Mode B accepts exactly one feature identifier" or similar. **If it succeeds, ASSUM-006 is not enforced at the CLI layer** — file a follow-up against TASK-MBC8-009.

### 2.5 Mode C queue smoke (NEW — FEAT-FORGE-008, post-TASK-F8-002)

> **TASK-F8-002 landed (nats-core 0.3.0 + forge wire wiring, 2026-04-30).**
> The wire payload now carries both a strict `feature_id`
> (`^FEAT-[A-Z0-9]{3,12}$`) **and** a sibling `task_id`
> (`^TASK-[A-Z0-9]{3,12}$`) plus a first-class `mode` field. Mode C
> dispatches now follow ASSUM-004 cleanly: the positional argument is
> the per-fix-task `TASK-*` identifier and the parent `FEAT-*` is read
> from the fix-task YAML's `parent_feature` field. A
> `model_validator(mode="after")` in
> `nats_core.events._pipeline.BuildQueuedPayload` enforces the
> bidirectional invariant `mode == "mode-c"  <=>  task_id is not None`,
> so Mode A/B publishers continue to validate without changes.

**Setup — write a Mode C fix-task YAML** (the file at `--feature-yaml`
must declare a non-empty `parent_feature` field for Mode C; missing or
malformed values exit `4` before any persistence side effect):

```bash
cat > "$FORGE_HOME/fix-task-stub.yaml" <<'YAML'
name: example-fix
parent_feature: FEAT-TESTMC
YAML
```

**Run — canonical Mode C invocation:**

```bash
echo "=== Phase 2.5: Mode C queue (post-TASK-F8-002 canonical form) ==="
forge --config "$FORGE_HOME/forge.yaml" queue TASK-TESTMC \
    --repo "$FORGE_REPO_PATH" --branch main --mode c \
    --feature-yaml "$FORGE_HOME/fix-task-stub.yaml"
forge --config "$FORGE_HOME/forge.yaml" status --db-path "$FORGE_DB_PATH"
forge --config "$FORGE_HOME/forge.yaml" history --feature FEAT-TESTMC --db "$FORGE_DB_PATH"
```

The published `BuildQueuedPayload` carries:
- `feature_id = "FEAT-TESTMC"` (resolved from `parent_feature` in the YAML)
- `task_id    = "TASK-TESTMC"` (the CLI positional)
- `mode       = "mode-c"`

**Pass:**
- `forge queue` exits `0` and prints `Queued FEAT-TESTMC (build pending) mode=mode-c …`.
- `forge status` shows `mode=mode-c` for FEAT-TESTMC.

**Negative-path checks** (each must exit non-zero **before** any
persistence or publish side effect):

```bash
# 1. FEAT-shaped positional rejected at the CLI boundary (exit 4).
forge --config "$FORGE_HOME/forge.yaml" queue FEAT-TESTMC \
    --repo "$FORGE_REPO_PATH" --branch main --mode c \
    --feature-yaml "$FORGE_HOME/fix-task-stub.yaml" 2>&1 | head -3
# Expected: "Mode C requires positional argument to match ^TASK-[A-Z0-9]{3,12}$"

# 2. Missing parent_feature in the fix-task YAML (Click UsageError).
cat > "$FORGE_HOME/no-parent.yaml" <<'YAML'
name: example-fix
YAML
forge --config "$FORGE_HOME/forge.yaml" queue TASK-TESTMC \
    --repo "$FORGE_REPO_PATH" --branch main --mode c \
    --feature-yaml "$FORGE_HOME/no-parent.yaml" 2>&1 | head -3
# Expected: "Mode C requires the fix-task YAML to declare a non-empty 'parent_feature' field"
```

**Pass (negative paths):** Both invocations exit non-zero with no
SQLite write or NATS publish recorded. Coverage for these refusals
lives in `tests/forge/test_cli_mode_flag.py::TestModeCSubject`.

> **AC-10 wire-layer smoke** — `tests/integration/test_mode_c_wire_smoke_e2e.py`
> exercises the same payload shape end-to-end against a live NATS
> broker. It is skipped by default; opt in with:
>
> ```bash
> FORGE_NATS_URL=nats://127.0.0.1:4222 \
>     pytest tests/integration/test_mode_c_wire_smoke_e2e.py -v
> ```
>
> Last run against `nats:2.10` jetstream-enabled container: PASSED
> (round-trip on `pipeline.build-queued.FEAT-<rand>` with both
> identifier fields intact).

### 2.6 Mode-filtered history view

```bash
echo "=== Phase 2.6: Mode-filtered history ==="
forge --config "$FORGE_HOME/forge.yaml" history --mode b --db "$FORGE_DB_PATH"
echo "--- Mode C only ---"
forge --config "$FORGE_HOME/forge.yaml" history --mode c --db "$FORGE_DB_PATH"
echo "--- All modes ---"
forge --config "$FORGE_HOME/forge.yaml" history --db "$FORGE_DB_PATH"
```

**Pass:** `--mode b` shows only FEAT-TESTMB. `--mode c` shows only FEAT-TESTMC. No filter shows all three (FEAT-TESTMA, FEAT-TESTMB, FEAT-TESTMC).

### 2.7 Constitutional skip refusal across modes (security regression)

`forge skip` against a `pull-request-review` pause must be refused regardless of mode (FEAT-FORGE-008 Group C / Group E, ASSUM-011). The unit test in `test_constitutional_regression.py` covers this; this step exercises the CLI path.

```bash
echo "=== Phase 2.7: Constitutional skip refusal ==="
# This is best done via the integration test rather than a manual queue run,
# because reaching pull-request-review needs a mocked subprocess result.
pytest tests/integration/test_constitutional_regression.py -v --tb=short 2>&1 | \
    tee /tmp/forge-constitutional.log
```

**Pass:** All scenarios green, including any that exercise Mode B and Mode C (look for `mode_b` / `mode_c` markers in the output).

### 2.8 Cleanup

```bash
echo "=== Phase 2.8: Cleanup ==="
rm -rf "$FORGE_HOME"
unset FORGE_HOME FORGE_DB_PATH
# If you started a throwaway NATS in §0.6, tear it down here:
docker stop forge-runbook-nats 2>/dev/null && docker rm forge-runbook-nats 2>/dev/null || true
```

---

## Phase 3: NATS pipeline-event observation (GB10)

Validates that pipeline events publish to NATS with correct correlation threading (FEAT-FORGE-002 substrate, exercised by FEAT-FORGE-008 Group H).

### 3.1 Open a NATS subscription on the GB10

In one SSH session, subscribe to every pipeline event and pretty-print:

```bash
ssh promaxgb10-41b1
# On GB10:
# `--raw` prints message bodies verbatim. Earlier drafts also passed
# `--headers`, but that flag does not exist on the `nats` CLI shipped here
# (the valid flag is `--headers-only`, which suppresses bodies — not what
# we want). The envelope's `correlation_id` is in the JSON body, not in
# NATS headers, so `--raw` alone is sufficient evidence.
# — *gap-fold 2026-04-30 (F008-RERUN-001 / AC-5)*
nats sub 'pipeline.>' --raw 2>&1 | tee /tmp/forge-nats-pipeline.log
```

Leave this running. The runbook will produce events in 3.2.

### 3.2 Run a queue-only smoke against the live NATS

In a second SSH session (or local terminal pointing at GB10's NATS):

```bash
ssh promaxgb10-41b1
# On GB10, in the forge checkout:
cd ~/Projects/appmilla_github/forge

# Use a fresh state dir so we don't fight with any prior runs.
# Same env-var + --feature-yaml + top-level --config shape as Phase 2.1
# (AC-6/AC-7/AC-8 + F008-RERUN-001 / AC-1..AC-4).
export FORGE_HOME=$(mktemp -d)
export FORGE_DB_PATH="$FORGE_HOME/forge.db"
export FORGE_REPO_PATH="$HOME/Projects/appmilla_github/forge"

cat > "$FORGE_HOME/forge.yaml" <<EOF
permissions:
  filesystem:
    allowlist:
      - /tmp
      - $FORGE_REPO_PATH
EOF
cat > "$FORGE_HOME/feature-stub.yaml" <<'EOF'
acceptance_criteria:
  - id: AC-1
    text: stub criterion for runbook Phase 3 NATS smoke
EOF

# Queue a Mode B build — feature does not need to exist; we just want the queue event.
# Identifier is `FEAT-NATSCHECK` (no inner hyphens) to satisfy the wire-schema
# regex `^FEAT-[A-Z0-9]{3,12}$` (AC-10).
forge --config "$FORGE_HOME/forge.yaml" queue FEAT-NATSCHECK \
    --repo "$FORGE_REPO_PATH" --branch main --mode b \
    --feature-yaml "$FORGE_HOME/feature-stub.yaml"

# Capture the build's correlation ID for later assertion
BUILD_ID=$(forge --config "$FORGE_HOME/forge.yaml" status --db-path "$FORGE_DB_PATH" | grep FEAT-NATSCHECK | awk '{print $2}')
echo "BUILD_ID=$BUILD_ID"
```

**Pass:** A `pipeline.build-queued.FEAT-NATSCHECK` event appears in the subscription log within ~1 second. The event's `correlation_id` field is populated.

### 3.3 Verify correlation threading from queue → terminal

The Group H scenario "Every published lifecycle event for a build threads the same correlation identifier from queue to terminal" is the key assertion.

For now we have only the queue event; to thread through more lifecycle events we need a working subprocess dispatch. Two options:

**Option A (light)** — let the integration test exercise it:
```bash
pytest tests/integration/test_per_build_routing.py \
       tests/integration/test_durable_decision_on_publish_failure.py \
       -v --tb=short
```

**Option B (heavy)** — drive a real Mode B build with stubbed subprocess and observe live NATS events. This requires the `forge serve` daemon (Phase 6.1 covers this end-to-end). Defer Option B until Phase 6.1.

**Pass:** Option A green. Capture from the integration test logs the correlation ID assertions and the count of distinct event types observed.

### 3.4 Cleanup

```bash
# Stop the subscription (Ctrl-C in the first SSH session)
# Save the log
scp promaxgb10-41b1:/tmp/forge-nats-pipeline.log /tmp/forge-nats-pipeline.log
rm -rf "$FORGE_HOME"
unset FORGE_HOME FORGE_DB_PATH
```

---

## Phase 4: Checkpoint forced-flag exercise (GB10)

Validates that lowering `auto_approve` forces a flag-for-review, that `pipeline.build-paused` arrives, and that `forge` CLI approval resumes the build (FEAT-FORGE-004 substrate, exercised by FEAT-FORGE-008 Group A, D).

### 4.1 Stage a forge.yaml that forces flag-for-review

```bash
ssh promaxgb10-41b1
# On GB10:
cd ~/Projects/appmilla_github/forge
export FORGE_HOME=$(mktemp -d)

cat > "$FORGE_HOME/forge.yaml" <<EOF
# Phase 4 forced-flag config — sets auto_approve threshold above any realistic
# Coach score so every flagged-for-review checkpoint pauses, regardless of mode.
approval:
  auto_approve_threshold: 999.0   # impossibly high → always flag
  hard_stop_threshold: -1.0
  max_wait_seconds: 60            # keep the test snappy
  # TASK-JNB-101: the decided_by identity the subscriber accepts. Model
  # default is "rich" — must string-equal jarvis JARVIS_SLACK_DECIDED_BY
  # VERBATIM or every phone approval is silently refused. NOTE the
  # permissive->enforcing flip: deployments omitting the approval block
  # now enforce "rich" by default. extra="forbid" also means this key
  # only boots on images that define the field.
  expected_approver: rich

queue:
  default_mode: a                  # Mode A is the simplest to drive end-to-end
  repo_allowlist:
    - guardkit/test-project
EOF

cat "$FORGE_HOME/forge.yaml"
```

**Pass:** File written. Inspect to confirm thresholds are inverted from defaults.

**Note:** the actual config field names depend on `src/forge/config/models.py` (`ForgeConfig` / `ApprovalConfig`). If the names above don't match, adjust to the model — the *intent* is to force every gate decision into FLAG_FOR_REVIEW. Capture any discrepancy as a runbook gap (Phase 7).

### 4.2 Subscribe to approval-gated lifecycle events

In a separate SSH session:
```bash
ssh promaxgb10-41b1
nats sub 'pipeline.build-paused.>' 'pipeline.build-resumed.>' --headers --raw \
    2>&1 | tee /tmp/forge-checkpoint-events.log
```

### 4.3 Drive a build to its first flag-for-review

The simplest path is the same integration test that already covers this — but exercised against the live NATS adapter rather than the in-memory fake.

```bash
# On GB10, in a third session
cd ~/Projects/appmilla_github/forge
NATS_URL=nats://localhost:4222 \
FORGE_CONFIG="$FORGE_HOME/forge.yaml" \
    pytest tests/integration/test_approval_round_trip.py \
           tests/integration/test_pause_and_publish_atomicity.py \
           -v --tb=short 2>&1 | tee /tmp/forge-checkpoint-tests.log
```

**Pass:** All scenarios green. The NATS subscription log shows:
- At least one `pipeline.build-paused.<build-id>` event
- At least one `pipeline.build-resumed.<build-id>` event after the test injects an approval response
- Each pair shares the same `build-id` (build-keyed routing per FEAT-FORGE-008 Group D edge-case "approval routed by build identifier")

### 4.4 Verify constitutional PR-review pin (executor-side, mode-agnostic)

This is the headline FEAT-FORGE-008 acceptance: ASSUM-011 — even a misconfigured prompt cannot auto-approve PR review.

```bash
# On GB10
pytest tests/integration/test_constitutional_regression.py -v --tb=short 2>&1 | \
    tee /tmp/forge-constitutional-live.log
```

**Pass:** Every Mode A, Mode B, and Mode C "constitutional belt-and-braces" scenario green. **If any pass with a synthetic auto-approve where they should pause, the executor-layer guard is broken** — block on this.

### 4.5 Cleanup

```bash
rm -rf "$FORGE_HOME"
unset FORGE_HOME FORGE_CONFIG NATS_URL
```

---

## Phase 5: Degraded-mode exercise (GB10)

Validates that a Mode A build with no specialist agents reachable forces FLAG_FOR_REVIEW with a degraded-specialist rationale recorded (FEAT-FORGE-003 substrate). FEAT-FORGE-008 ASSUM-014 says **Mode B does not exhibit this** (no specialist dispatch happens) — so this phase has two sub-steps:

### 5.1 Mode A degraded — FLAG_FOR_REVIEW with rationale

```bash
ssh promaxgb10-41b1
# Stop any running specialist-agent processes
docker ps --format "{{.Names}}" | grep -i specialist | xargs -r docker stop
ps aux | grep -i specialist-agent | grep -v grep | awk '{print $2}' | xargs -r kill

# Confirm none are reachable
ssh promaxgb10-41b1 'curl -s http://localhost:5000/health 2>&1' || echo "Specialist offline as expected"

# Drive the degraded-mode integration test against live NATS
cd ~/Projects/appmilla_github/forge
NATS_URL=nats://localhost:4222 \
    pytest tests/integration/test_unrecognised_responder.py \
           tests/forge/test_specialist_outage_*.py \
           -v --tb=short 2>&1 | tee /tmp/forge-degraded-mode-a.log
```

**Pass:** Tests assert that a Mode A build's `product-owner` or `architect` stage flags for review with `degraded-specialist` rationale recorded on the build's stage history.

### 5.2 Mode B no-degraded-rationale (positive assertion)

Per FEAT-FORGE-008 Group L — Mode B does NOT record a degraded-specialist rationale because no specialist dispatch is attempted (ASSUM-014).

```bash
# Specialist is still down from 5.1
pytest tests/integration/test_mode_b_smoke_e2e.py::test_mode_b_no_degraded_specialist_rationale \
    -v --tb=short 2>&1 | tee /tmp/forge-mode-b-no-degraded.log

# Or, if the test ID is different, run the full Mode B smoke with the specialist down:
pytest tests/integration/test_mode_b_smoke_e2e.py -v --tb=short
```

**Pass:** Mode B smoke runs to PR-awaiting-review terminal even with specialists offline. No `degraded-specialist` rationale appears on the stage history. **If Mode B blocks waiting for specialists, ASSUM-014 was not enforced** — file a follow-up against TASK-MBC8-003.

### 5.3 Restart specialist agents

```bash
# Restart whatever you stopped in 5.1 — the exact command depends on how the
# specialist agents are deployed on GB10:
docker start <specialist-container-name>
# OR
systemctl --user start specialist-agent@architect specialist-agent@product-owner
# Confirm:
curl -s http://localhost:5000/health
```

---

## Phase 6: LES1 Parity Gates (production-image, all four required)

Per build plan §"Specialist-agent LES1 Parity Gates" — these are the gates that proved necessary on the specialist-agent build (TASK-MDF-CMDW / PORT / ARFS). Do not skip. Each must be green on the production image, not on a dev build.

### 6.1 CMDW gate — production-image subscription round-trip

Build the forge production container, run `forge serve` inside it, publish one real `pipeline.build-queued` from outside the container, verify the subscribed JetStream pull consumer delivers it to an actual pipeline run.

> **Build context prerequisite.** The canonical BuildKit invocation
> below uses `--build-context nats-core=../nats-core`, which means the
> sibling `nats-core` working tree **must be present at `../nats-core`**
> (i.e. as a peer of the `forge/` working tree) before the build is
> invoked. If the sibling is missing, `docker buildx build` fails fast
> at the named-context resolution step. No host-side mutation of
> `pyproject.toml`, symlinks, or `.env` is required (canonical-freeze
> §3.4 / scenario A5); the build is fully container-native.

```bash
ssh promaxgb10-41b1
# Run from inside forge/ so that the BuildKit named contexts
# `--build-context nats-core=../nats-core`,
# `--build-context guardkit=../guardkit`,
# `--build-context fleet-memory=../fleet-memory` and
# `--build-context guardkitfactory=../guardkitfactory` resolve to the
# sibling `nats-core` / `guardkit` / `fleet-memory` / `guardkitfactory`
# working trees (one level up from forge/, alongside it). This is the
# canonical invocation — no host-side mutation. Fixed in
# TASK-FORGE-FRR-003 after the GB10 first-real-run revealed the previous
# "cd to forge's parent" form pointed `../nats-core` at the wrong
# directory. The `guardkit` context was added after B4 run 4b3b0893
# caught the target-terminal oracles (normalizer + `guardkit feature
# validate`) missing from the image. The `fleet-memory` context supplies
# the gate's priors read (the forge `memory` extra; fleet-memory has no
# PyPI distribution). The `guardkitfactory` context supplies the LangGraph
# leg harness — the conductor's first real leg died in-container with
# `GUARDKIT_HARNESS=langgraph but guardkitfactory is not importable`
# because the image baked guardkit but not its harness runtime.
cd ~/Projects/appmilla_github/forge

# Build the production image (Contract A — canonical BuildKit invocation)
docker buildx build --build-context nats-core=../nats-core --build-context guardkit=../guardkit --build-context fleet-memory=../fleet-memory --build-context guardkitfactory=../guardkitfactory -t forge:production-validation -f Dockerfile .

# Run forge serve inside it, with NATS pointing at the GB10 host
docker run -d --name forge-cmdw \
    --network host \
    -e NATS_URL=nats://localhost:4222 \
    -e FORGE_LOG_LEVEL=info \
    forge:production-validation \
    forge serve

# Wait for subscription to be ready (look for the subscribe log line)
# Post TASK-FORGE-FRR-002: ``-e FORGE_LOG_LEVEL=info`` now produces
# visible log lines in stderr. Expected output (one-line example):
#   2026-05-01T12:34:56 [INFO] forge.cli._serve_healthz: healthz listening on 0.0.0.0:8080
sleep 10
docker logs forge-cmdw 2>&1 | grep -iE "subscribed|listening|ready" | head -5

# Publish a real BuildQueuedPayload from outside the container
nats pub 'pipeline.build-queued.FEAT-CMDW-001' \
    "$(cat <<'EOF'
{
  "schema_version": "1.0",
  "build_id": "build-cmdw-001",
  "feature_id": "FEAT-CMDW-001",
  "correlation_id": "corr-cmdw-001",
  "mode": "mode-a",
  "queued_at": "2026-04-29T12:00:00Z",
  "repo": "guardkit/test-project",
  "branch": "main"
}
EOF
)"

# Verify forge picked it up
sleep 5
docker logs forge-cmdw 2>&1 | grep -iE "build-cmdw-001|FEAT-CMDW-001" | head -10
```

**Pass:** forge logs show the build was picked up from the queue and at least one stage dispatch was attempted. **A stale container build that silently fails to subscribe is the exact specialist-agent CMDW failure mode applied to forge** — if logs show no pickup, the build is broken regardless of unit-test status.

**Cleanup:**
```bash
docker stop forge-cmdw && docker rm forge-cmdw
```

### 6.2 PORT gate — `(specialist_role, forge_stage)` dispatch matrix

For every `(role ∈ {product-owner, architect}, stage)` pair used in Mode A, execute one end-to-end round-trip via NATS on the production specialist-agent image. Mode B and Mode C do **not** dispatch to specialists (ASSUM-014) so this matrix is Mode-A-only.

The matrix:

| Role | Stage | Test |
|------|-------|------|
| product-owner | product-owner | `pytest tests/integration/test_specialist_dispatch_po.py -v` (or equivalent) |
| architect | architect | `pytest tests/integration/test_specialist_dispatch_architect.py -v` |

```bash
ssh promaxgb10-41b1
# Confirm both specialist roles are running on the production image
docker ps --format "{{.Names}}\t{{.Image}}" | grep specialist-agent
# OR:
ps aux | grep specialist-agent | grep -v grep

curl -s http://localhost:5000/health  # PO
curl -s http://localhost:5001/health  # architect (port may differ)

# Drive the dispatch matrix
cd ~/Projects/appmilla_github/forge
NATS_URL=nats://localhost:4222 \
    pytest tests/integration/ -k "specialist_dispatch or per_build_routing" \
    -v --tb=short 2>&1 | tee /tmp/forge-port-matrix.log
```

**Pass:** Every `(role, stage)` pair returns a `ResultPayload` containing `coach_score`, `criterion_breakdown`, and `detection_findings`. Any red pair is a hard stop.

**If a pair is red:** the failure mode is almost certainly that the specialist-agent's PORT bug is unfixed — handlers not registered. Report against the specialist-agent build, not forge.

### 6.3 ARFS gate — per-tool handler-completeness matrix

For each tool in the forge `AgentManifest`, walk the full chain `tool-schema → NATS adapter handler → core API → orchestrator method` and execute one smoke-test round-trip.

The forge tool surface (per anchor §10):
- `forge_greenfield` — Mode A
- `forge_feature` — Mode B (NEW from FEAT-FORGE-008)
- `forge_review_fix` — Mode C (NEW from FEAT-FORGE-008)
- `forge_status`
- `forge_cancel`

```bash
ssh promaxgb10-41b1
cd ~/Projects/appmilla_github/forge

# Inspect the manifest the production image registers
docker run --rm forge:production-validation forge --debug-manifest 2>&1 | tee /tmp/forge-manifest.log
# OR (if no --debug-manifest flag exists yet):
docker run --rm forge:production-validation \
    python -c "from forge.fleet.manifest import build_manifest; import json; print(json.dumps(build_manifest().model_dump(), indent=2))" \
    2>&1 | tee /tmp/forge-manifest.log

# Confirm every tool above appears
for TOOL in forge_greenfield forge_feature forge_review_fix forge_status forge_cancel; do
    if grep -q "$TOOL" /tmp/forge-manifest.log; then
        echo "✓ $TOOL present"
    else
        echo "✗ $TOOL MISSING — ARFS gate fails"
    fi
done

# Smoke-test each tool's NATS adapter handler
NATS_URL=nats://localhost:4222 \
    pytest tests/integration/ -k "tool_dispatch or agent_dispatch" \
    -v --tb=short 2>&1 | tee /tmp/forge-arfs.log

# Walk the chain manually for each tool — confirm no NotImplementedError or TODO
for TOOL in forge_greenfield forge_feature forge_review_fix forge_status forge_cancel; do
    grep -rn "$TOOL" src/forge/ 2>/dev/null | grep -iE "todo|notimplemented|raise NotImplemented" | head -3
done
```

**Pass:** Every tool present in the manifest. Every tool's adapter handler exercises a real orchestrator method (no `NotImplementedError`, no unhandled `TODO`). The pytest sweep is green.

**If `forge_feature` or `forge_review_fix` are missing from the manifest:** TASK-MBC8-009 only added the CLI surface, not the tool registration. The new modes are not callable via the fleet — file a follow-up. This is exactly the ARFS failure mode.

### 6.4 Canonical-freeze live-verification gate

Every shell block in this runbook AND in the build plan's Step 6 section must have been executed verbatim on a clean MacBook + GB10 in a single walkthrough session, logged in `forge/docs/history/command-history.md`. Annotate any block that required workarounds with `[as of commit <sha>]`.

This is the LES1 §8 lesson: **runbook copy-paste blocks are code; a CI-passing runbook can still fail on a clean machine**.

```bash
# On the clean machine (MacBook):
cd ~/Projects/appmilla_github/forge
git pull --ff-only
git log --oneline -1   # capture the sha for the [as of commit <sha>] marker

# Walk through Phases 0–2 (local) verbatim, logging each block to
# command-history.md as you go.

# Then on GB10, walk through Phases 3–6.3 verbatim.

# Summarise gaps in the RESULTS file (Phase 7) — anything that needed an
# inline tweak gets folded back into this runbook in a follow-up commit.
```

**Pass:** Every shell block executes without manual edits, OR every required edit is documented with `[as of commit <sha>]` markers and folded back into the runbook by Phase 7. The walkthrough is logged in `command-history.md`.

---

## Phase 7: Wrap-up — RESULTS file + build plan update

### 7.1 Capture results

Write `forge/docs/runbooks/RESULTS-FEAT-FORGE-008-validation.md` using this template:

```markdown
# Results: FEAT-FORGE-008 Validation

**Executed:** 2026-04-XX (start) → 2026-04-XX (end)
**Operator:** <name>
**Commit at start:** <sha from `git log --oneline -1` in Phase 6.4>

## Per-gate outcomes

| Phase | Gate | Outcome | Evidence |
|-------|------|---------|----------|
| 1.1 | Full pytest suite | ✅ / ❌ | `/tmp/forge-pytest-phase1.log` — N tests, M green, K red |
| 1.2 | FEAT-FORGE-008 BDD bindings (56 scenarios) | ✅ / ❌ | `/tmp/forge-bdd-008.log` |
| 1.3 | Mode A regression (FEAT-FORGE-007) | ✅ / ❌ | `/tmp/forge-mode-a-regression.log` |
| 2.1–2.7 | CLI smoke (forge queue across modes) | ✅ / ❌ | <inline notes per substep> |
| 3.x | NATS pipeline-event observation | ✅ / ❌ | `/tmp/forge-nats-pipeline.log`, `/tmp/forge-checkpoint-events.log` |
| 4.x | Checkpoint forced-flag exercise | ✅ / ❌ | `/tmp/forge-checkpoint-tests.log`, `/tmp/forge-constitutional-live.log` |
| 5.x | Degraded-mode exercise | ✅ / ❌ | `/tmp/forge-degraded-mode-a.log`, `/tmp/forge-mode-b-no-degraded.log` |
| 6.1 | CMDW (production-image subscription) | ✅ / ❌ | docker logs excerpt |
| 6.2 | PORT (specialist × stage matrix) | ✅ / ❌ | `/tmp/forge-port-matrix.log` |
| 6.3 | ARFS (per-tool handler completeness) | ✅ / ❌ | `/tmp/forge-manifest.log`, `/tmp/forge-arfs.log` |
| 6.4 | Canonical-freeze live-verification | ✅ / ❌ | `forge/docs/history/command-history.md` entries |

## Runbook gaps discovered during execution

(Per RESULTS-v3 precedent — list every block that required a manual tweak so
they can be folded back into the runbook in a follow-up commit.)

| Phase | Block | What needed adjustment | Suggested runbook fix |
|-------|-------|------------------------|----------------------|
| ... | ... | ... | ... |

## Headline metrics

- Test count: <N> tests / <M> green / <K> red
- BDD scenarios: 56/56 green
- Cross-mode concurrency assertion: <pass/fail>
- LES1 production-image subscription: <pass/fail>
- Tool surface complete: <forge_greenfield, forge_feature, forge_review_fix, forge_status, forge_cancel> all wired
- Canonical-freeze walkthrough: <date> on <machines>; <N> manual tweaks needed

## Decision

- [ ] **Step 6 ✅ canonical** — proceed to Step 7 (FinProxy first real run)
- [ ] **Step 6 partially passed** — file follow-up tasks (linked below) and re-run blocked gates
- [ ] **Step 6 failed** — block until <specific issue> resolved

## Follow-up tasks

- TASK-FORGE-VAL-001: <issue title> — <one-line scope>
- TASK-FORGE-VAL-002: ...
```

### 7.2 If all gates green: update the build plan

```bash
cd ~/Projects/appmilla_github/forge
$EDITOR docs/research/ideas/forge-build-plan.md
```

Edits to apply:

1. **Status header** (line 3): change `autobuild ✅ 8/8 complete` → keep as is, but change `Next:` to `Step 7 FinProxy first real run`
2. **Step 6 row** (progress log table): mark ✅ complete with the date, RESULTS file pointer, and headline metric
3. **Footer log**: append a new entry capturing the validation outcome

### 7.3 If gates failed: file follow-up tasks

Each red gate becomes a discrete task using `/task-create`:

```bash
/task-create "Resolve FEAT-FORGE-008 validation gate <X>: <one-line scope>" \
    task_type:feature priority:high
```

Link the task to this runbook's RESULTS file in the task description. Re-run the affected phases of this runbook after the fix lands.

### 7.4 Hand-off to Step 7

If Step 6 is canonical, the next runbook is `RUNBOOK-FEAT-FORGE-008-finproxy-first-run.md` (to be drafted) — it covers `forge queue FEAT-FINPROXY-001 --repo guardkit/finproxy --branch main` end-to-end against a real specialist-agent fleet on GB10.

Notes for the FinProxy runbook author:
- Include a CMDW-style gate **for the FinProxy repo specifically** — the production forge image must subscribe and pick up the FEAT-FINPROXY-001 build queued from outside the container.
- Capture the build's stage history at terminal as the canonical proof of "Forge ran end-to-end."
- The constitutional PR-review pause is the expected terminal — there is no auto-approve path. Approval is an operator action.

---

## Common runbook gaps to watch for

These are the failure modes the llama-swap RUNBOOK-v3 hit when first executed; pre-emptively scan for them here.

1. **`pkill -f` self-kills the script.** Use `pkill -x` (exact basename) when stopping `llama-server`, `nats-server`, or `forge` processes — `-f` matches the script's own command line.
2. **Port re-use.** Phase 6.1 starts a containerised `forge serve`; if a host-level forge or another service holds NATS port 4222, the integration tests will pick the wrong endpoint. Confirm with `lsof -ti :4222`.
3. **NATS fresh-volume ack-no-deliver.** If the GB10 NATS was provisioned with a fresh volume since the last run, publishes succeed (PubAck) but messages are not retained. Re-run `provision-streams.sh` and `provision-kv.sh` before Phase 3.
4. **Stale production image.** If the forge production image on GB10 was built before commit `2f13eac`, `forge_feature` and `forge_review_fix` will not be in the manifest. ARFS will fail. Always rebuild the image at the start of Phase 6.1.
5. **Specialist agents using a different NATS account.** If specialist-agents on GB10 publish to a different account than the one Phase 3 subscribes to, the messages won't appear. Confirm both sides use the same account in their JetStream config.

---

## References

- **Build plan:** `forge/docs/research/ideas/forge-build-plan.md` — Step 6 spec, LES1 parity gate definitions
- **Predecessor runbooks (style + LES1 lessons):**
  - `guardkit/docs/research/dgx-spark/RUNBOOK-v3-production-deployment.md` — production deployment shape
  - `guardkit/docs/research/dgx-spark/RUNBOOK-v2-all-llamacpp-architecture.md` — phase structure + gap-folding pattern
- **Relevant TASK-MDF lessons:**
  - `specialist-agent` TASK-MDF-CMDW — production-image subscription round-trip
  - `specialist-agent` TASK-MDF-PORT — `(role, stage)` dispatch matrix
  - `specialist-agent` TASK-MDF-ARFS — per-tool handler-completeness matrix
- **Feature artefacts:**
  - `forge/features/mode-b-feature-and-mode-c-review-fix/mode-b-feature-and-mode-c-review-fix.feature`
  - `forge/.guardkit/features/FEAT-FORGE-008.yaml`
  - `forge/tasks/backlog/mode-b-feature-and-mode-c-review-fix/IMPLEMENTATION-GUIDE.md`
- **Autobuild summary:** `forge/.guardkit/autobuild/FEAT-FORGE-008/review-summary.md`

---

*Generated 2026-04-29 alongside the FEAT-FORGE-008 merge. Update with `[as of commit <sha>]` annotations during the canonical-freeze walkthrough.*
