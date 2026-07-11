---
id: TASK-FORGE-SCAFFOLD
title: Hand-Scaffold Orchestrator Template Files into Forge Repo
status: completed
task_type: implementation
reconciled: "2026-07-11 WS3-S8 sweep — frontmatter added (file had none); forge repo fully scaffolded, foundational task long completed"
---

# TASK-FORGE-SCAFFOLD — Hand-Scaffold Orchestrator Template Files into Forge Repo

## Type
Implementation — mechanical file rendering, no design decisions

## Priority
HIGH — unblocks Forge Phase 0 `/system-arch`

## Estimated Effort
30–60 minutes (copy four files, substitute placeholders, commit)

## Context

`guardkit init` is config-layer-only by design
(TASK-INST-010, 2026-03-02; re-affirmed by TASK-REV-A5F8, 2026-04-11). The
`templates/` tree inside each template package is the **pattern layer** —
build-time context for AutoBuild's Player, not init-time scaffolding. No
runtime consumer of the pattern layer exists today; FEAT-1A5E will wire it
into AutoBuild's Player context, and R4 in TASK-REV-A925 proposes a future
`guardkit render <template>` command.

In the interim, initialising a project from a pattern-rich template like
`langchain-deepagents-orchestrator` leaves the consumer repo without its
scaffold files. For the Forge this means:

- No `pyproject.toml` (needed for `pip install .[providers]` and for
  `/system-arch` to reason about packaging)
- No `AGENTS.md` (needed for MemoryMiddleware + R2A1 ainvoke contract)
- No `agent.py` (needed as the primary entry point)
- No `langgraph.json` (needed for LangGraph deployment config)

This task manually renders those four files from the installed template
source into the Forge repo, using forge-appropriate values for the
Jinja placeholders.

## Authoritative Source (DO NOT EDIT)

The canonical template files live at:

    ~/.agentecflow/templates/langchain-deepagents-orchestrator/templates/other/other/

Specifically:

- `pyproject.toml.template`
- `AGENTS.md.template`
- `agent.py.template`
- `langgraph.json.template` *(if present — verify during execution)*

These files include the post-LES1 hardening (LCL-004 `[providers]` extras,
LCL-005 R2A1 ainvoke contract section, LCL-006 env-var factory resolution,
LCL-007 Evaluator tool-inventory assertion).

**Do NOT edit the source templates.** This task renders them; the source is
owned by the guardkit repo and updated via `/template-create` workflow.

## Placeholder Substitutions

The templates use `{{Placeholder}}` syntax. Use these values for the Forge:

| Placeholder | Forge Value |
|---|---|
| `{{ProjectName}}` | `forge` |
| `{{ProjectNameSnake}}` | `forge` (already snake) |
| `{{ProjectNamePascal}}` | `Forge` |
| `{{Namespace}}` | `forge` |
| `{{PackageName}}` | `forge` |
| `{{Description}}` | `Pipeline orchestrator and checkpoint manager for the Software Factory` |
| `{{Author}}` | `Richard Woollcott` |
| `{{AuthorEmail}}` | `rich@appmilla.com` |
| `{{PythonVersion}}` | `>=3.11,<4.0` |

*(If other placeholders are present in the templates, use names consistent
with the Forge's identity — `forge` for anything project-name-like,
`Forge` for anything display-name-like.)*

## Implementation Steps

### Step 1: Inventory the template source

Confirm which files are present in the installed template, because the
original investigation only enumerated three of the four expected files:

    ls -la ~/.agentecflow/templates/langchain-deepagents-orchestrator/templates/other/other/

Record the full list in `command-history.md` (create if needed at
`~/Projects/appmilla_github/forge/command-history.md`).

### Step 2: Render each template file

For each `.template` file, read the source, substitute placeholders, and
write the rendered file to the Forge repo root.

Target locations (confirm against the template's expected output structure —
some files may target `src/forge/` rather than repo root):

| Source | Forge destination |
|---|---|
| `pyproject.toml.template` | `pyproject.toml` |
| `AGENTS.md.template` | `AGENTS.md` |
| `agent.py.template` | `agent.py` *(or `src/forge/agent.py` — check template conventions)* |
| `langgraph.json.template` | `langgraph.json` *(or whatever the template indicates)* |

**Recommended approach:** use `sed` or a short Python script for placeholder
substitution. Example one-liner per file:

    sed -e 's/{{ProjectName}}/forge/g' \
        -e 's/{{ProjectNamePascal}}/Forge/g' \
        -e 's/{{Namespace}}/forge/g' \
        -e 's/{{PackageName}}/forge/g' \
        -e 's/{{Description}}/Pipeline orchestrator and checkpoint manager for the Software Factory/g' \
        -e 's/{{Author}}/Richard Woollcott/g' \
        -e 's/{{AuthorEmail}}/rich@appmilla.com/g' \
        ~/.agentecflow/templates/langchain-deepagents-orchestrator/templates/other/other/pyproject.toml.template \
        > ~/Projects/appmilla_github/forge/pyproject.toml

For `agent.py.template` the target may need `src/forge/__init__.py` to be
created alongside. Use the template's internal import paths as the guide —
if it imports `from forge.prompts import ...` then `src/forge/` is the
correct target with `__init__.py`.

### Step 3: Verify no unresolved placeholders remain

    grep -rn "{{" ~/Projects/appmilla_github/forge/pyproject.toml \
                  ~/Projects/appmilla_github/forge/AGENTS.md \
                  ~/Projects/appmilla_github/forge/agent.py \
                  2>/dev/null

Expected output: nothing. Any remaining `{{Placeholder}}` means the
substitution missed a variable — add it to the sed script and re-render.

### Step 4: Validate basic package sanity

    cd ~/Projects/appmilla_github/forge
    pip install -e ".[providers]"
    python -c "import forge; print(forge.__name__)"

*(The second line may fail if `agent.py` lives at repo root rather than
`src/forge/agent.py` — that's fine for this task, as long as `pip install`
succeeds and `pyproject.toml` is syntactically valid.)*

### Step 5: Commit as the Forge's anchor commit

This commit is the `status: as of commit <hash>` anchor for the LES1
re-validation gate pattern going forward.

    git add pyproject.toml AGENTS.md agent.py langgraph.json src/ 2>/dev/null
    git commit -m "chore: hand-scaffold from langchain-deepagents-orchestrator template

    Template source: ~/.agentecflow/templates/langchain-deepagents-orchestrator/
                     templates/other/other/ (post-LES1 hardening)
    Rendered manually because guardkit-init is config-layer-only by design
    (TASK-INST-010, re-affirmed TASK-REV-A5F8).

    Included post-LES1 hardening via LCL-004..007:
    - [providers] extras in pyproject.toml
    - AGENTS.md with R2A1 ainvoke contract
    - env-var factory resolution in agent.py
    - Evaluator tool-inventory assertion (where applicable)

    Source tracking reference: guardkit/.claude/reviews/TASK-REV-A925-review-report.md

    Forge-specific scaffold tasks (src/forge/cli, src/forge/pipeline, etc.)
    remain owned by /feature-plan → AutoBuild per forge-build-plan.md."

### Step 6: Record in command-history.md

Append an entry to `forge/command-history.md`:

- Date
- Task ID (TASK-FORGE-SCAFFOLD)
- Files rendered and their source templates
- Commit hash of the anchor commit
- Reference back to `guardkit/.claude/reviews/TASK-REV-A925-review-report.md`

## Acceptance Criteria

- [ ] `~/Projects/appmilla_github/forge/pyproject.toml` exists and contains
      a `[providers]` extras section with `langchain-anthropic`,
      `langchain-openai`, `langchain-google-genai`
- [ ] `~/Projects/appmilla_github/forge/AGENTS.md` exists and contains the
      "Framework Contract: ainvoke() Message Rules (TASK-REV-R2A1)" section
- [ ] `~/Projects/appmilla_github/forge/agent.py` (or `src/forge/agent.py`)
      exists and is syntactically valid Python
- [ ] `~/Projects/appmilla_github/forge/langgraph.json` exists if the
      template ships it (N/A otherwise)
- [ ] No `{{Placeholder}}` strings remain in any rendered file
- [ ] `pip install -e ".[providers]"` succeeds from the Forge repo root
- [ ] `.guardkit/context-manifest.yaml` is unchanged (pre-existing file must
      not be affected by this task)
- [ ] Anchor commit pushed to the Forge repo
- [ ] `command-history.md` entry recorded

## Scope Boundaries

**In scope:**
- Mechanical rendering of template files into the Forge repo root
- Placeholder substitution using Forge-appropriate values
- Anchor commit + command-history entry

**Out of scope:**
- Any business logic implementation (state machine, NATS integration,
  checkpoint protocol, etc.) — those are FEAT-FORGE-001..008, owned by
  `/feature-plan` per `docs/research/ideas/forge-build-plan.md`
- Editing the source templates in the guardkit repo
- Editing `guardkit-init` or `install.sh` — those are covered by
  TASK-REV-A925's R2/R3/R5 in the guardkit repo
- Designing a `guardkit render` command — that's R4 in the guardkit review,
  routed to `/feature-plan`

## Related

- `guardkit/.claude/reviews/TASK-REV-A925-review-report.md` — architectural
  justification for this task being forge-side rather than guardkit-side
- `guardkit/.claude/reviews/TASK-REV-A5F8-review-report.md` — 11 April 2026
  decision that `guardkit init` is config-layer-only by design
- `guardkit/.claude/reviews/TASK-REV-LES1-review-report.md` — LCL-004..007
  hardening in the source templates
- `forge/docs/research/ideas/forge-build-plan.md` — Step 1 (`/system-arch`)
  is unblocked once this task completes (modulo NATS prerequisites)

## Follow-On

After this task completes and the anchor commit lands:

1. Verify the hard prerequisites from `forge-build-plan.md`:
   - nats-infrastructure running on GB10
   - nats-core integration tests passing (v2.2 payloads added)
   - specialist-agent Phase 3 complete (architect role NATS-callable)
2. Once prerequisites are green, proceed to Step 1 of the build plan:
   `/system-arch` with the full `--context` flag set.
3. When `guardkit render <template>` ships (R4 future feature), this
   manually-rendered scaffold becomes the reference for how the future
   automated render should behave for the Forge.
