# forge-prod live restart-policy state — read-only capture (E2-S3, 2026-07-13T17:43:36Z)

READ-ONLY `docker inspect` — this session did NOT run `docker update` against
forge-prod (rule 3: live application is the coordinator's step). Captured only
to record the true live state, per the zero-refuted-claims honesty rule.

```
container   = /forge-prod
image       = forge:latest
RestartPolicy = {"Name":"unless-stopped","MaximumRetryCount":0}
Running     = true
StartedAt   = 2026-07-12T09:39:33.608449795Z
RestartCount= 0
```

## Finding (honest, re-verified this session)

forge-prod **currently carries `unless-stopped`** — the O-30 auto-recovery policy
is present on the LIVE container as of this capture. This differs from the
2026-07-13 gap snapshot (O-30: "NO `--restart` policy"), which read the canonical
`docker run` command, not the live container; the policy was evidently applied
out-of-band since. A "perishable — re-verify at claim" item (handoff §0), now re-verified.

**The durable gap remains and is what E2-S3 closes:** the canonical launch command
(RUNBOOK-FEAT-FORGE-008-finproxy-first-run.md + the finproxy runbook + the
Prerequisite-C `docker run`) still has **no `--restart`**, so ANY forge-prod
recreate (e.g. the B4-window recreate, an image bump) re-introduces docker's
default `no` and silently loses auto-recovery. `restart_policy.py` makes
re-applying it a one-command, receipted, idempotent step (a no-op if already on);
the boot-order note (ops/README.md) recommends baking `--restart unless-stopped`
into the run command as the belt-and-braces durable fix.
