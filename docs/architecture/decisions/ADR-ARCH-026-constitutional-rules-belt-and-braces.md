# ADR-ARCH-026: Constitutional rules enforced belt + braces — prompt AND executor assertion

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 5

## Context

ADR-ARCH-019 puts behaviour under the reasoning model's control. A subset of rules, however, are **not up for reasoning** — they are hard invariants that any violation of is a bug, not a judgment call:

- **PR review is always human** — the final gate before merge never auto-approves, regardless of Coach score, regardless of calibration priors, regardless of prompt-injected instructions in feature descriptions.
- **Never skip precommit hooks** (no `--no-verify`) — agent behaviour rule from CLAUDE.md.
- **Sequential builds only** — ADR-SP-012; already physically enforced by `max_ack_pending=1` (ADR-ARCH-014) but also a constitutional rule.
- **No force-push to main** — standard git safety.

A sophisticated prompt injection in a feature description could try to convince the reasoning model that a constitutional rule doesn't apply this time. Pure-prompt enforcement isn't enough.

## Decision

Constitutional rules are enforced at **two layers**, belt + braces:

1. **System prompt** — the rules appear explicitly in `forge.prompts.FORGE_SYSTEM_PROMPT` under a "GUARDRAILS" section:
   ```
   ─── GUARDRAILS (policy) ──────────────────────────────────────────────
   • PR review is ALWAYS human — never auto-approve final merge.
   • Sequential builds only — never spawn parallel work.
   • Never skip precommit hooks (no --no-verify).
   • No force-push to main.
   • Degraded mode: fall back to local tool + force flag_for_review when a
     preferred capability is unavailable.
   ```

2. **Executor assertions** — deterministic Python code in the tools/sub-agents that actually perform the action. Before `pr_finaliser` calls `gh pr merge`, it asserts `config.constitutional.pr_review_always_human is True` (loaded at startup and immutable in-process). If the assertion fails (shouldn't be reachable without code-level bypass), the tool returns an error; the reasoning model cannot override.

For `never_skip_precommit_hooks`: the git adapter asserts no `--no-verify` in any git command's argv before invoking `execute`.

For `sequential_builds_only`: already physical via `max_ack_pending=1`; assertion in the consumer config.

## Consequences

- **+** Prompt-injection cannot bypass safety invariants — the executor assertion is the ultimate gate.
- **+** Constitutional rules are visible to the reasoning model (in the prompt) *and* in the executor (in the code) — the model knows they're rules, and can't break them even if it tried.
- **+** The assertion failures generate clear log lines — visible in observability.
- **−** Double-entry overhead — rule text in both system prompt and code. Mitigated by generating the prompt-text block from the constitutional config at startup (single source of truth).
- **−** Adding a new constitutional rule requires code + config change + system-prompt regeneration. Intentional friction; these rules shouldn't change often.
