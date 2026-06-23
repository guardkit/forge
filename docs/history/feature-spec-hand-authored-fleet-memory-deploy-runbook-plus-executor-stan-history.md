# /feature-spec — hand-authored-fleet-memory-deploy-runbook-plus-executor-stan — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-spec hand-authored-fleet-memory-deploy-runbook-plus-executor-stan — 2026-06-22T14:13:21Z

### Prompt

```
/feature-spec "Hand-authored fleet-memory deploy runbook plus executor stand-up: a typed 2-step runbook (deploy_compose then run_smoke_tests, both targeting fleet-memory/deploy/nas with .env.deploy, NO approval gates because the target is local and reversible) persisted via the runbook model and executed via 'forge runbook run'; on success fleet-memory Postgres+pgvector is live on the NAS with smoke gates G3-G5 green; the runbook JSON saved as the first harvested exemplar under forge/runbooks/; an end-to-end test that runs the executor against a disposable compose target (fleet-memory deploy/local, NOT the real NAS) proving deploy then smoke then runbook-complete; then the same executor (forge runbook run with the real .env.deploy) runs the runbook against the real NAS as the actual stand-up — not a manual ./deploy.sh — which closes TASK-MEM-008 and ticks FEAT-MEM-01's NAS acceptance criterion"
```

### Assistant response

I'll start with Phase 1: Context Gathering. Let me silently scan the codebase to understand the runbook model, executor, fleet-memory deploy structure, and smoke gates.

Fleet-memory deploy assets aren't in this repo (likely a sibling repo). Let me read the executor source, CLI, step handlers, existing runbook feature conventions, and the MEM/exemplar context docs.

Let me read the build plan for FORGE-OL-04, the step handlers, and the CLI runbook command.

Let me quickly confirm the step handler names/params and registry, plus glance at an existing runbook feature file for convention.
