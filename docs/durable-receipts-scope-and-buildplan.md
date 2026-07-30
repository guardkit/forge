# U1 / FEAT-DRC — durable build receipts (scope + buildplan)
## 2026-07-30 · BINDING SPEC · plan-of-record U1 (register 2a4 — THE M4 BLOCKER)

> Grounded in the two sources of truth (ai-transition mission + plan) and the
> 2026-07-30 unattended-day handoff §5-U1. Design pass: coordinator-direct reads
> (two scout attempts died on server-side 529/500 — zero tokens spent; the seams
> below are pinned by the coordinator's own reads, the playbook's small-pass path).
> Measurable: **M4** (unblocks shadow-receipt accrual) + M2/M5 evidence durability.
> M0: none claimed — say so at close.

## The problem (proven on FEAT-UDBE, 2026-07-28)

On build SUCCESS the sidecar runner removes the outer worktree
(`src/forge/subagents/autobuild_runner.py` — success branch of
`_node_running_wave`, `await _remove_worktree(repo_path, worktree_path)` ≈:2048),
destroying every receipt guardkit wrote there under the isolated topology:

- `.guardkit/autobuild-private/<task>/` — coach verdicts (`coach_turn_N.json`),
  evidence dossiers (`coach_evidence_turn_N.json`), and the FEAT-SCG
  `spec_conformance/` snapshot (the post-SBHO `task_private_dir`
  worktree-shape rule resolves INTO the outer worktree here —
  guardkit `paths.py:616`).
- `.guardkit/qav-shadow/queue.jsonl` — the shadow judge's receipt
  (guardkit `qa/qav_shadow.py:186`, `QAV_SHADOW_QUEUE` resolved per-repo-root =
  the outer worktree under isolation).
- `.guardkit/autobuild/<FEAT>/` — review summaries.

The FAILURE path already keeps the worktree (DEFECT #19 forensics) — receipts
survive there today; this lane touches ONLY the success path.

## The design (seams pinned)

1. **New helper `_export_receipts(worktree_path, build_id) -> bool`** in
   `autobuild_runner.py`, modeled on `_remove_worktree`'s best-effort idiom
   (:1706 — log-and-swallow, never raises, never blocks the terminal flow):
   copies, if present, `worktree/.guardkit/autobuild-private/**`,
   `worktree/.guardkit/qav-shadow/**`, `worktree/.guardkit/autobuild/**` to
   `<receipts_root>/<build_id>/` preserving relative layout. Missing sources are
   fine (copy what exists; an empty export is still True). Returns False only on
   a real copy failure (logged WARNING with the error).
2. **Destination**: env `FORGE_RECEIPTS_DIR`, default `~/forge-state/receipts`
   (expanduser at call time). The sidecar runs on the HOST as the user, so this
   writes directly; `~/forge-state` is also bind-mounted at `/var/forge` in
   forge-prod, so the daemon (and future accrual counters) can read
   `/var/forge/receipts/<build_id>/`. The helper creates dirs as needed.
3. **Call site + ordering (the crux)**: in the success branch, replace
   `if worktree_path is not None: await _remove_worktree(...)` with:
   export first; **remove ONLY if export returned True**; on export failure log
   WARNING "keeping worktree — receipts not exported" (the worktree then behaves
   like the failure path: on-disk forensics; the F3 preflight prune does NOT
   delete directories, and a kept tree never regresses a succeeded build — the
   `_remove_worktree` docstring's own principle). `build_id` comes from
   `payload.get("build_id")` (in scope; fall back to the worktree dir name).
4. **No guardkit changes. No ledger/schema changes. No envelope changes.**
   The export is invisible to the build's outcome by construction.
5. **Tests** (in `tests/forge/test_autobuild_runner_worktree.py` beside the
   existing worktree behavior tests, mock-tree style): (a) success path exports
   all three families and then removes; (b) export failure (unwritable dest) ⇒
   worktree KEPT + WARNING, snapshot unchanged (still success — the build's
   outcome is never altered); (c) missing receipt dirs ⇒ export succeeds with
   what exists; (d) `FORGE_RECEIPTS_DIR` override honored; (e) failure path
   untouched (no export call — receipts already survive with the kept tree).

## Honest notes for the ledger

- The qav-shadow ACCRUAL mechanism has an open question the design does not
  block on: FEAT-UBEM's organic receipt reached the api_test MAIN checkout's
  queue while FEAT-UDBE's did not — mechanism unconfirmed (possibly a path-
  resolution difference between runs). The export makes every receipt durable
  regardless; today's M4 counting is a coordinator read, and
  `receipts/<build_id>/` becomes a countable location. Flag for the next
  routine sit's verification: check BOTH the main-checkout queue and the
  export dir.
- Live proof requires a real gated build (a tap) — parked for the next attended
  window; today's proof = the unit tests above.

## Fences

Forge venue ONLY (no guardkit/api_test/jarvis changes). No broker access
anywhere (the standing playbook block; tests are pure-filesystem). No changes to
terminal/emit semantics, snapshots, or the ledger. `.guardkit/**` and `uv.lock`
untouched. Local path-limited commits; push only after the coordinator's own
review; deploy = sidecar stop-wait-start + `/proc` env re-verify (handoff §2.5).
