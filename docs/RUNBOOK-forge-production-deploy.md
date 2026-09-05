# Runbook: Forge Production Service — Capture, Rebuild, Promote, Redeploy

**Status:** **Verified**, and **CORRECTED 2026-09-05.** First green walkthrough 2026-08-23 on
`promaxgb10-41b1`, deploying the specification-path change (forge `f580e20`/`bb3336e`).

> **What changed since, and why this file was wrong until 2026-09-05.** Both follow-ups this
> runbook owed have since been done, and it had not caught up:
>
> * **The image's health check was fixed** (commit `907ac58`, "healthcheck probes the port forge
>   actually serves on"). It now reads `${FORGE_HEALTHZ_PORT:-8080}`, so a recreate no longer needs
>   an override. The trap that G4 was written for **no longer exists** — kept below as history,
>   because the gate is still worth running.
> * **The run configuration is committed**: `ops/forge-prod-recreate.sh`, with the settings of
>   record sops-encrypted at `~/.config/fleet-secrets/forge/forge-prod.enc.env`. So the claim that
>   "the only record is the running container" is **false as of 2026-09-03**, and Phase 1 is now a
>   fallback rather than the main path.
>
> **The correction that mattered most:** this file told you to `docker stop forge-prod` with no
> check that the estate was quiet. That contradicts a binding rule — *never restart forge-prod
> unless every build is terminal; PAUSED is not quiet.* The committed script enforces it; this file
> did not. Phase 4 now does.

**Purpose:** Rebuild the `forge` image from the working tree and put it into production.

**Since 2026-09-03 there is a committed recreate script** — `ops/forge-prod-recreate.sh` — which
reads the settings of record from a sops-encrypted file at run time, passes no value through its
own output, and **refuses to touch the container while any build is RUNNING, PAUSED or QUEUED**.
Use it. Phase 4 is a thin wrapper around it.

This runbook still exists for the parts the script does not cover: recon, the build, the image
promotion (which `build-image.sh` deliberately does not perform), and the gates that prove the
deploy actually worked. Phase 1 — capturing a running container's configuration — is now a
**fallback** for the case where the settings file is unavailable, not the main path.

```
Slack sentence
   │
   ▼
jarvis ──NATS──▶ forge-prod  (this runbook's subject)
                    │  entrypoint: forge --config /var/forge/forge.yaml serve
                    │  --network host · --user forge · --restart unless-stopped
                    │  healthz :8088  (FORGE_HEALTHZ_PORT — NOT the image default)
                    │
                    ├──NATS──▶ specialist-agent (architect / product-owner seats)
                    │             └──▶ llama-swap :9000
                    └──▶ target repo working copies (bind-mounted, rw)
```

**Machine:** `promaxgb10-41b1`. **Conventions:** [`RUNBOOK-CONVENTIONS.md`](https://github.com/guardkit/dgx-spark/blob/main/RUNBOOK-CONVENTIONS.md) — recon → drift report → gates; promotion by PR.
**Expected wall-clock:** ~12–18 min (the image build dominates at ~8–12 min; recon and redeploy are ~2 min each).
**Outputs:** `RESULTS-forge-deploy-<YYYY-MM-DD>.md`, and the captured `forge-prod-inspect-<YYYY-MM-DD>.json` — **keep that file; it is the configuration's only backup.**

**Execution modes (CONVENTIONS §2.2):**
`fresh` — top to bottom (first deploy, or after the container has been destroyed: Phase 1 then reads its configuration from the PINS block below rather than from a running container).
`re-run` — same file on a running box; idempotent, gates re-verify. **~12 min.**
`update` — Phase 0 recon reports drift between the working tree and the running image; run Phases 2–5.

---

```
PINS (runbook v1, set 2026-08-23)
  container name        forge-prod
  image tag (live)      forge:latest
  image tag (build)     forge:production-validation     ← what build-image.sh actually produces
  rollback tag format   forge:rollback-YYYYMMDD-pre-<thing>
  build script          scripts/build-image.sh          ← Contract A: the ONLY place buildx is invoked
  recreate script       ops/forge-prod-recreate.sh      ← the settings of record, committed (2026-09-03)
  settings file         ~/.config/fleet-secrets/forge/forge-prod.enc.env   (sops; NEVER decrypted to disk)
  entrypoint            forge
  command               --config /var/forge/forge.yaml serve
  workdir               /home/forge
  user                  forge
  network               host
  restart policy        unless-stopped
  healthz port          8088   (via FORGE_HEALTHZ_PORT; the IMAGE hardcodes 8080 — see G4)
  code path in image    /opt/venv/lib/python3.14/site-packages/forge     ← NOT /app/src
  bind mounts           6   (see Phase 1)
  operator env vars     15  (names in Phase 1; values are never written down — see the note there)
```

---

## Phase 0: Recon (read-only, advisory — nothing changes)

```bash
# 0a. What is running, and how old is it?
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.RunningFor}}' | grep forge-prod

# 0b. Is there drift between the working tree and the running container?
cd ~/Projects/appmilla_github/forge
git log --oneline -1
git status --porcelain | grep -v '^??' || echo "  working tree clean"

# 0c. What image tags exist, and how far back does rollback go?
docker images forge --format '{{.Repository}}:{{.Tag}}\t{{.CreatedSince}}' | head -6
```

**Drift report:** if the running container predates the commit you intend to deploy, that is the
drift, and it is the reason for the run. Record both in `RESULTS-*`.

> **The single most common finding here:** the container is days old and carries none of the merged
> work. A merge is not a deploy. On 2026-08-23 the architect seat had been redeployed and forge had
> not, so only one half of a two-repository change was live — and every proof of it up to that point
> had come from supplying by hand what forge was supposed to send.

---

## Phase 0.5: Pre-flight

```bash
# The build needs the sibling nats-core working copy.
[ -d ~/Projects/appmilla_github/nats-core ] && echo PASS || echo "FAIL: sibling nats-core missing"

# Enough disk for another ~800 MB image plus the rollback tag you are about to keep.
df -h / | awk 'NR==2 {print ($4+0 > 5 ? "PASS "$4" free" : "FAIL only "$4" free")}'

# Nothing else is mid-run on the box that a forge restart would interrupt.
docker ps --format '{{.Names}}' | grep -E 'specialist-agent|jarvis' || echo "  (no peers up)"
```

---

## Phase 1: CAPTURE THE CONFIGURATION — do this BEFORE anything is stopped

**This is the phase the runbook exists for. It has no side effects, and skipping it is
unrecoverable.**

```bash
STAMP=$(date +%Y-%m-%d)
OUT=~/forge-deploy-receipts; mkdir -p "$OUT"
docker inspect forge-prod > "$OUT/forge-prod-inspect-$STAMP.json"
echo "captured $(wc -c < "$OUT/forge-prod-inspect-$STAMP.json") bytes"
```

**GATE G1 — the capture is complete and parseable.** Halts if not.

```bash
python3 - "$OUT/forge-prod-inspect-$STAMP.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))[0]
c,h=d["Config"],d["HostConfig"]
mounts=d.get("Mounts") or []
envs=[e.split("=",1)[0] for e in (c.get("Env") or []) if "=" in e]
hc=c.get("Healthcheck") or {}
ok = (len(mounts)==6 and h.get("NetworkMode")=="host"
      and c.get("Entrypoint")==["forge"] and hc.get("Test"))
print(f"  mounts={len(mounts)} network={h.get('NetworkMode')} env={len(envs)} "
      f"healthcheck={'present' if hc.get('Test') else 'MISSING'}")
print("PASS" if ok else "FAIL — capture incomplete; do NOT stop the container")
PY
```

**What the capture must contain** (all read from the file, never retyped):

| | |
|---|---|
| Entrypoint / Cmd | `forge` / `--config /var/forge/forge.yaml serve` |
| Workdir / User | `/home/forge` / `forge` |
| Network / Restart | `host` / `unless-stopped` |
| Mounts (6) | `~/forge-state → /var/forge` · `~/forge-prod-state/.forge → /home/forge/.forge` · the **api_test**, **jarvis**, **study-tutor** and **ts-api-test** working copies, each bind-mounted rw at its own host path |
| Env (15 operator-supplied) | `FLEET_MEMORY_EMBED_DIMS` · `FLEET_MEMORY_EMBED_MODEL` · `FLEET_MEMORY_EMBED_URL` · `FLEET_MEMORY_ENABLED` · `FLEET_MEMORY_PG_DSN` · `FORGE_AUTOBUILD_RUNNER_URL` · `FORGE_HEALTHZ_PORT` · `FORGE_LOG_LEVEL` · `FORGE_NATS_URL` · `GIT_AUTHOR_EMAIL` · `GIT_AUTHOR_NAME` · `GIT_COMMITTER_EMAIL` · `GIT_COMMITTER_NAME` · `OPENAI_API_KEY` · `OPENAI_BASE_URL` |
| Healthcheck | an **override**, see G4 |

> **Values are never written into this runbook or into `RESULTS-*`.** They are copied
> container-to-container from the captured JSON. Two of those names are credentials.
> `FLEET_MEMORY_PG_DSN` is a connection string with embedded credentials — never echo it, never
> paste it into a document. Refs-only, per the secrets register.

> **Do NOT re-pass image-owned variables** — `PATH`, `HOME`, `HOSTNAME`, `PWD`, `PYTHON_VERSION`,
> `PYTHON_SHA256`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `NODE_OPTIONS`. The new image
> supplies its own; carrying the old ones across pins a stale interpreter path into a new image.

**Generate the recreate command from the capture** (never hand-typed):

```bash
python3 - "$OUT/forge-prod-inspect-$STAMP.json" "$OUT/forge-recreate.sh" <<'PY'
import json,shlex,sys
d=json.load(open(sys.argv[1]))[0]; c,h=d["Config"],d["HostConfig"]
IMAGE_OWNED={"PATH","HOSTNAME","HOME","PWD","PYTHON_VERSION","PYTHON_SHA256",
             "PYTHONDONTWRITEBYTECODE","PYTHONUNBUFFERED","NODE_OPTIONS"}
def ns(v): return f"{int(v)//1_000_000_000}s" if v else None
hc=c.get("Healthcheck") or {}
a=["docker","run","-d","--name","forge-prod",
   "--restart",h["RestartPolicy"]["Name"],"--network",h["NetworkMode"],
   "--user",c.get("User") or "forge","-w",c.get("WorkingDir") or "/home/forge"]
if hc.get("Test"):                      # G4: the override, or the image's 8080 wins
    a += ["--health-cmd", hc["Test"][1]]
    for flag,key in (("--health-interval","Interval"),("--health-timeout","Timeout"),
                     ("--health-start-period","StartPeriod")):
        if hc.get(key): a += [flag, ns(hc[key])]
    if hc.get("Retries"): a += ["--health-retries", str(hc["Retries"])]
for m in d.get("Mounts") or []:
    a += ["-v", f'{m["Source"]}:{m["Destination"]}:{"rw" if m.get("RW",True) else "ro"}']
for e in c.get("Env") or []:
    if e.split("=",1)[0] not in IMAGE_OWNED: a += ["-e", e]
a += [c["Image"]] + (c.get("Cmd") or [])
open(sys.argv[2],"w").write("#!/usr/bin/env bash\nset -euo pipefail\n"
                            + " ".join(shlex.quote(x) for x in a) + "\n")
print("PASS — recreate script written")
PY
chmod +x "$OUT/forge-recreate.sh"
```

> **That script contains credential values.** It lives under `~/forge-deploy-receipts`, not in a
> repository. `chmod 600` it if it will outlive the deploy.

---

## Phase 2: Build

```bash
cd ~/Projects/appmilla_github/forge
./scripts/build-image.sh 2>&1 | tail -20
```

**GATE G2 — the build's own oracle verification passed.** The script ends with a self-check
(resolver, harness, producer, CLI). Halts if not.

```bash
./scripts/build-image.sh 2>&1 | grep -q "forge oracle verification PASSED" && echo PASS || echo FAIL
```

**GATE G3 — the new image carries the change you came to deploy.** Substitute the symbol your
change introduces; the example is the 2026-08-23 specification-path work.

```bash
docker run --rm --entrypoint sh forge:production-validation -c \
  'V=/opt/venv/lib/python3.14/site-packages/forge; grep -c "spec_feature_paths" $V/cli/_serve_planning.py' \
  | awk '{print ($1+0 > 0 ? "PASS" : "FAIL — the image does not contain the change")}'
```

> **The code is at `/opt/venv/lib/python3.14/site-packages/forge`, not `/app/src`.** Grepping the
> wrong path returns "No such file or directory", which reads like a missing change and is not one.

---

## Phase 3: Promote

`build-image.sh` produces **`forge:production-validation`** and deliberately does **not** touch
`latest`. Promotion is a separate, reversible act.

```bash
docker tag forge:latest forge:rollback-$(date +%Y%m%d)-pre-<thing>   # keep the way back FIRST
docker tag forge:production-validation forge:latest
docker images forge --format '{{.Repository}}:{{.Tag}}\t{{.CreatedSince}}' | head -3
```

---

## Phase 4: Redeploy

**Use the committed script.** It gates the estate itself, reads the settings from sops, and prints
no value:

```bash
cd ~/Projects/appmilla_github/forge
DRY_RUN=1 bash ops/forge-prod-recreate.sh     # names only, changes nothing — read it first
bash ops/forge-prod-recreate.sh
```

> **✋ THE ESTATE GATE — binding, and the script enforces it.** It refuses while any forge build is
> `RUNNING`, `PAUSED` or `QUEUED`. **`PAUSED` is not quiet**: a paused build is mid-flight, and
> recreating under it drops the branch so the runner "succeeds" in two seconds having done nothing.
> If the script refuses, that is the gate working — let the builds finish, do not force it.

**Fallback only** — if the settings file is unavailable and you are recreating from a Phase 1
capture, you must apply the same gate by hand, because the generated script does not contain it:

```bash
docker exec forge-prod forge --config /var/forge/forge.yaml status | grep -qE 'RUNNING|PAUSED|QUEUED' \
  && echo "HALT — a build is in flight; do not recreate" \
  || { docker stop forge-prod && docker rm forge-prod && bash "$OUT/forge-recreate.sh"; }
sleep 20 && docker ps --format '{{.Names}}\t{{.Status}}' | grep forge-prod
```

**GATE G4 — the container reports HEALTHY, not merely "up".** ✋ **This gate exists because it
caught a live failure on 2026-08-23.**

```bash
for i in $(seq 1 10); do
  s=$(docker inspect forge-prod --format '{{.State.Health.Status}}' 2>/dev/null)
  [ "$s" = "healthy" ] && break; sleep 15
done
echo "health=$s"; [ "$s" = "healthy" ] && echo PASS || echo "FAIL — see the note below"
```

> **THE TRAP.** The **image** bakes in `curl -fs http://localhost:8080/healthz`, with the port
> **hardcoded**. The container listens on **8088** via `FORGE_HEALTHZ_PORT`. The original
> `docker run` therefore passed an explicit override —
> `curl -fs http://localhost:${FORGE_HEALTHZ_PORT:-8080}/healthz` — which lives **only in the
> container**, not in the image.
>
> Recreate without carrying it across and the container reads **unhealthy while the endpoint
> answers HTTP 200**. Confirm which you are looking at by comparing both:
> `docker inspect forge-prod --format '{{json .Config.Healthcheck.Test}}'` against
> `docker inspect forge:latest --format '{{json .Config.Healthcheck.Test}}'` — **they differ, and
> that difference is the whole point of Phase 1.**
>
> **FIXED 2026-09-05 — this trap no longer exists.** Commit `907ac58` changed the image's health
> check to read `${FORGE_HEALTHZ_PORT:-8080}`, so it now survives a recreate without an override.
> Verified: the live image and the running container both carry the port-aware form, and the
> committed recreate script deliberately passes **no** health-check override because it no longer
> needs one.
>
> The gate stays, because "healthy, not merely up" is worth asserting on every deploy whatever the
> cause. The history is kept because it explains why Phase 1 captures the health check at all.

**GATE G5 — the endpoint answers.**

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://localhost:8088/healthz \
  | awk '{print ($1=="200" ? "PASS" : "FAIL http "$1)}'
```

**GATE G6 — the change is live in the RUNNING container**, not merely in the image.

```bash
docker exec forge-prod sh -c \
  'V=/opt/venv/lib/python3.14/site-packages/forge; grep -c "spec_feature_paths" $V/cli/_serve_planning.py' \
  | awk '{print ($1+0 > 0 ? "PASS" : "FAIL")}'
```

**GATE G7 — the git identity survived.** ✋ **This gate exists because a missing git identity
produced 41 false CI failures on 2026-08-22.** Forge's planning driver commits through git in the
code under test; without an identity every commit dies with `Author identity unknown` and the run
is recorded as a plan failure.

```bash
docker exec forge-prod sh -c '[ -n "$GIT_AUTHOR_NAME" ] && [ -n "$GIT_COMMITTER_EMAIL" ]' \
  && echo PASS || echo "FAIL — commits will die with 'Author identity unknown'"
```

**GATE G8 — the service composed its dispatch chain and bound its consumer.**

```bash
docker logs --tail 40 forge-prod 2>&1 | grep -q "dispatch chain composed" && echo PASS || echo FAIL
```

---

## Phase 5: Decision Gate

| Gate | Asserts | Result |
|---|---|---|
| G1 | The configuration was captured, complete and parseable, **before anything stopped** | |
| G2 | The build's own oracle verification passed | |
| G3 | The new **image** carries the change | |
| G4 | The container reports **healthy** — not merely up (health-check override survived) | |
| G5 | `/healthz` answers 200 on the configured port | |
| G6 | The change is live in the **running container** | |
| G7 | Git identity present (else every planning commit fails) | |
| G8 | Dispatch chain composed, consumer bound | |

**All eight PASS → green.** Any FAIL → do not consider the deploy done; Appendix A rolls back in
under a minute.

---

## Phase 6: Cleanup & Harden

```bash
chmod 600 "$OUT"/forge-recreate.sh "$OUT"/forge-prod-inspect-*.json
docker images forge --format '{{.Tag}}' | grep '^rollback-' | tail -n +6   # prune beyond 5, by hand
```

Write `RESULTS-forge-deploy-<YYYY-MM-DD>.md`: the commit deployed, the rollback tag created, the
eight gate results, and anything Phase 0 recon flagged. **Never the environment values.**

---

## Appendix A: Rollback

```bash
docker tag forge:rollback-<YYYYMMDD>-pre-<thing> forge:latest
bash ops/forge-prod-recreate.sh   # gates the estate, reads the settings, image-independent
```

The recreate script is image-independent — it carries the container's configuration, not the
image's — so rolling back is a tag swap plus a recreate. **The estate gate applies to a rollback
exactly as it does to a deploy**; if a build is in flight, the rollback waits.

Then re-run G4, G5, G7, G8. The recreate script is reusable because it carries the container's
configuration, not the image's — which is why Phase 1 is worth its own phase.

## Appendix B: If the container is already gone

Phase 1 cannot capture what is not running — but since 2026-09-03 it does not need to.
`ops/forge-prod-recreate.sh` plus the sops settings file reconstruct the container from nothing:

```bash
bash ops/forge-prod-recreate.sh
```

Only if the settings file itself is also gone do you fall back to a previous
`forge-prod-inspect-*.json` under `~/forge-deploy-receipts`, and only then are values at risk.

---

## Both follow-ups this runbook owed are DONE

| Owed | Done | Evidence |
|---|---|---|
| The image's health check should read the port variable so it survives a recreate without an override | **2026-09-05** | commit `907ac58`; live image and container both carry `${FORGE_HEALTHZ_PORT:-8080}` |
| Forge's run configuration should be a committed file, not only the running container | **2026-09-03** | `ops/forge-prod-recreate.sh` + sops settings of record; first real use 2026-09-04, healthy |

Nothing is outstanding. This section stays so the next reader can see the loop closed rather than
wonder whether it ever was.
