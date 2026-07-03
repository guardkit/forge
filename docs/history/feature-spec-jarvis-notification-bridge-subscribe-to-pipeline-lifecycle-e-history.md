# /feature-spec — jarvis-notification-bridge-subscribe-to-pipeline-lifecycle-e — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-spec jarvis-notification-bridge-subscribe-to-pipeline-lifecycle-e — 2026-07-03T10:26:05Z

### Prompt

```
/feature-spec "Jarvis notification bridge: subscribe to pipeline.* lifecycle
envelopes and agents.approval.forge.*; route to Telegram with build_id,
feature_id, correlation_id, stage, coach score, rationale. v1 one-way
(terminal states + pauses). v1.1 approval replies → resume path.
Notification failure = log WARNING and continue (DDR-007)."
  --context ../jarvis
  --context src/forge/nats/approval_subscriber.py
  --context docs/research/ideas/unattended-build-service-scope.md
```

### Assistant response

I'll execute the Propose-Review methodology. Starting with **Phase 1: Context Gathering** (silent, no interaction).

The approval_subscriber path doesn't exist as given. Let me locate it and the relevant NATS/envelope schemas.

Jarvis already has substantial forge-notification code. Let me examine the existing jarvis implementation and the nats_core payload schemas to ground the spec.
