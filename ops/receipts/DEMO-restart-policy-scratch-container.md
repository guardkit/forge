# O-30 DEMO — restart policy on a THROWAWAY scratch container (E2-S3(d))

Re-run 2026-07-13T18:18:20Z on `promaxgb10-41b1` under the **E2-S3 fix-pass grammar**
(explicit `--apply` guard). **Never touches forge-prod.** This proves the three
things E2-S3(a) claims:

0. **The default is inert** — a bare `restart_policy.py` run (no `--apply`) is a
   PREVIEW: it runs no `docker update`, and the container's policy is unchanged.
1. `restart_policy.py --apply` flips `HostConfig.RestartPolicy` via `docker update`
   with **no container restart** (so no Ack-Pending-0 drain is needed — see the receipt).
2. An `unless-stopped` container **auto-restarts when its process dies unexpectedly**
   (a crash / OOM / power-loss / reboot — the O-30 scenario). The forge-prod
   container has no such policy in its canonical `docker run`; this pass grants it.

> **Explicit-apply guard (why step 2 is a preview and step 3 needs `--apply`):**
> mutation NEVER happens by default. `restart_policy.py` with no flags previews the
> change and issues no `docker update`; only `--apply` mutates the live container.
> `--apply` and `--dry-run` are mutually exclusive. This closes the E2-S3 footgun
> where a bare invocation immediately updated the live default container.

> **Docker semantics note (why the crash demo uses a self-exiting process, not `docker kill`):**
> `docker stop`/`docker kill` set a *manually-stopped* flag and the restart policy is
> deliberately **skipped** — an operator stop must stay stopped. The policy fires only
> when the main process exits on its **own** (crash, OOM, or the daemon coming back
> after a reboot). Signalling PID 1 from *inside* its own namespace is kernel-ignored
> (init-protection), so the demo uses a container whose PID 1 self-exits (`exit 1`) —
> the honest analogue of a crash — and watches the daemon bring it back.

## 1. scratch container created — no `--restart` (= forge-prod as-run)
```
RestartPolicy={"Name":"no","MaximumRetryCount":0} Running=true RestartCount=0
```

## 2. restart_policy.py DEFAULT (no `--apply`) — PREVIEW, issues NO docker update
```
=== restart_policy.py [apply · DRY RUN] ===
container: forge-restart-demo-e2s3fix
restart policy (before): 'no'
target policy: 'unless-stopped'
preflight checklist:
  [     ok] no_restart_occurs: `docker update --restart` is a metadata-only change to 'forge-restart-demo-e2s3fix''s HostConfig.RestartPolicy — it does NOT stop, restart, or recreate the container; the running process is untouched and the new policy takes effect on the next daemon start / reboot.
  [     ok] ack_pending_zero_not_required: Ack-Pending-0 / worker-free drain is NOT needed for this operation: that gate guards a container *recreate* (a real restart that would drop an in-flight build); no restart occurs here, so no drain is required. (Contrast the forge-prod RECREATE at the B4 window, which does gate on Ack-Pending-0.)
  [     ok] target_policy: target HostConfig.RestartPolicy.Name = 'unless-stopped' (auto-recovery on).
would change: True (unless-stopped)
PREVIEW ONLY — no `docker update` executed. Re-run with --apply to actually change 'forge-restart-demo-e2s3fix'.
operator next action: none (dry run — no `docker update` executed)
receipt: …/ops/receipts/demo/restart-policy-apply-dry-run-20260713T181820_088227Z.json
```
```
--- policy still 'no' (preview did not mutate): ---
RestartPolicy={"Name":"no","MaximumRetryCount":0}
```

## 3. restart_policy.py `--apply` — `docker update --restart unless-stopped`
```
=== restart_policy.py [apply] ===
container: forge-restart-demo-e2s3fix
restart policy (before): 'no'
target policy: 'unless-stopped'
preflight checklist:
  [     ok] no_restart_occurs: `docker update --restart` is a metadata-only change to 'forge-restart-demo-e2s3fix''s HostConfig.RestartPolicy — it does NOT stop, restart, or recreate the container; the running process is untouched and the new policy takes effect on the next daemon start / reboot.
  [     ok] ack_pending_zero_not_required: Ack-Pending-0 / worker-free drain is NOT needed for this operation: that gate guards a container *recreate* (a real restart that would drop an in-flight build); no restart occurs here, so no drain is required. (Contrast the forge-prod RECREATE at the B4 window, which does gate on Ack-Pending-0.)
  [     ok] target_policy: target HostConfig.RestartPolicy.Name = 'unless-stopped' (auto-recovery on).
restart policy (after): 'unless-stopped'
changed: True
operator next action: none — the policy is live in the daemon immediately and persists across reboots; no container restart/recreate is needed or performed
receipt: …/ops/receipts/demo/restart-policy-apply-applied-20260713T181820_202625Z.json
```

## 3b. proof `docker update` did NOT restart the container
```
StartedAt before update : 2026-07-13T18:18:19.962432036Z
StartedAt after  update : 2026-07-13T18:18:19.962432036Z   <- UNCHANGED (no restart)
RestartPolicy={"Name":"unless-stopped","MaximumRetryCount":0} Running=true RestartCount=0
```

## 4. process dies on its own (crash/OOM/reboot) → `unless-stopped` auto-restarts it
```
born with: RestartPolicy={"Name":"unless-stopped","MaximumRetryCount":0}
poll (1s cadence): t1:Running=true/RCount=0 t2:Running=true/RCount=1 t3:Running=true/RCount=1 t4:Running=true/RCount=1 t5:Running=true/RCount=2 t6:Running=true/RCount=2 t7:Running=true/RCount=3 t8:Running=true/RCount=3 t9:Running=true/RCount=4 t10:Running=true/RCount=4

-> RestartCount climbed 0 -> 1 -> 2 -> 3 -> 4 while Running stayed true: the daemon
   auto-recovered the self-exiting process every time. This is the
   host-reboot/power-loss auto-recovery O-30 says forge-prod's canonical run lacks
   and restart_policy.py grants.
```

### JSON receipts written by restart_policy.py during this demo
```
restart-policy-apply-applied-20260713T181820_202625Z.json
restart-policy-apply-dry-run-20260713T181820_088227Z.json
```
Both scratch containers were `docker rm -f`'d at the end of the run (trap cleanup);
forge-prod was never touched.
