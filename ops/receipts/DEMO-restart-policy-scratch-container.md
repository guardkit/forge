# O-30 DEMO — restart policy on a THROWAWAY scratch container (E2-S3(d))

Run 2026-07-13T17:41:59Z on `promaxgb10-41b1`. **Never touches forge-prod.**
This proves the two things E2-S3(a) claims:

1. `restart_policy.py` flips `HostConfig.RestartPolicy` via `docker update` with
   **no container restart** (so no Ack-Pending-0 drain is needed — see the receipt).
2. An `unless-stopped` container **auto-restarts when its process dies unexpectedly**
   (a crash / OOM / power-loss / reboot — the O-30 scenario). The forge-prod
   container has no such policy today; this pass grants it.

> **Docker semantics note (why the demo crashes the process instead of `docker kill`):**
> `docker stop`/`docker kill` set a *manually-stopped* flag and the restart policy is
> deliberately **skipped** — an operator stop must stay stopped. The policy fires only
> when the main process exits on its **own** (crash, OOM, or the daemon coming back after
> a reboot). The O-30 failure mode is exactly the latter, so the demo makes PID 1 crash
> (`kill -9 $$` from inside) rather than issuing `docker kill`.

## 1. scratch container created — no `--restart` (= forge-prod as-run)
```
RestartPolicy={"Name":"no","MaximumRetryCount":0} Running=true RestartCount=0
```

## 2. restart_policy.py --dry-run (read-only; issues NO docker update)
```
=== restart_policy.py [apply · DRY RUN] ===
container: forge-restart-demo-e2s3
restart policy (before): 'no'
target policy: 'unless-stopped'
preflight checklist:
  [     ok] no_restart_occurs: `docker update --restart` is a metadata-only change to 'forge-restart-demo-e2s3''s HostConfig.RestartPolicy — it does NOT stop, restart, or recreate the container; the running process is untouched and the new policy takes effect on the next daemon start / reboot.
  [     ok] ack_pending_zero_not_required: Ack-Pending-0 / worker-free drain is NOT needed for this operation: that gate guards a container *recreate* (a real restart that would drop an in-flight build); no restart occurs here, so no drain is required. (Contrast the forge-prod RECREATE at the B4 window, which does gate on Ack-Pending-0.)
  [     ok] target_policy: target HostConfig.RestartPolicy.Name = 'unless-stopped' (auto-recovery on).
would change: True (unless-stopped)
operator next action: none (dry run — no `docker update` executed)
receipt: /home/richardwoollcott/Projects/appmilla_github/forge/ops/receipts/demo/restart-policy-apply-dry-run-20260713T174159_315686Z.json
```

## 3. restart_policy.py apply — `docker update --restart unless-stopped`
```
=== restart_policy.py [apply] ===
container: forge-restart-demo-e2s3
restart policy (before): 'no'
target policy: 'unless-stopped'
preflight checklist:
  [     ok] no_restart_occurs: `docker update --restart` is a metadata-only change to 'forge-restart-demo-e2s3''s HostConfig.RestartPolicy — it does NOT stop, restart, or recreate the container; the running process is untouched and the new policy takes effect on the next daemon start / reboot.
  [     ok] ack_pending_zero_not_required: Ack-Pending-0 / worker-free drain is NOT needed for this operation: that gate guards a container *recreate* (a real restart that would drop an in-flight build); no restart occurs here, so no drain is required. (Contrast the forge-prod RECREATE at the B4 window, which does gate on Ack-Pending-0.)
  [     ok] target_policy: target HostConfig.RestartPolicy.Name = 'unless-stopped' (auto-recovery on).
restart policy (after): 'unless-stopped'
changed: True
operator next action: none — the policy is live in the daemon immediately and persists across reboots; no container restart/recreate is needed or performed
receipt: /home/richardwoollcott/Projects/appmilla_github/forge/ops/receipts/demo/restart-policy-apply-applied-20260713T174159_403871Z.json
```

## 3b. proof `docker update` did NOT restart the container
```
StartedAt before update : 2026-07-13T17:41:59.169607433Z
StartedAt after  update : 2026-07-13T17:41:59.169607433Z   <- UNCHANGED (no restart)
RestartPolicy={"Name":"unless-stopped","MaximumRetryCount":0} Running=true RestartCount=0
```

## 4. process crashes (kill -9 $$ inside) → `unless-stopped` auto-restarts it
```
poll (1s cadence): t1:Running=true/RCount=0 t2:Running=true/RCount=0 t3:Running=true/RCount=1 t4:Running=true/RCount=1 t5:Running=true/RCount=1 t6:Running=true/RCount=2

-> RestartCount climbed 0 -> 1 -> 2 while Running returned to true: the daemon
   auto-recovered the crashed process. This is the host-reboot/power-loss
   auto-recovery O-30 says forge-prod lacks and restart_policy.py grants.
```

### JSON receipts written by restart_policy.py during this demo
```
restart-policy-apply-applied-20260713T174159_403871Z.json
restart-policy-apply-dry-run-20260713T174159_315686Z.json
```
