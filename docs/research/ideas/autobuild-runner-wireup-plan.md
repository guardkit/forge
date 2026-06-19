Prefer a small task count (1-3 tasks). This is a focused stub-replacement,
not a greenfield surface. Skip BDD-style test scaffolding; one or two
direct pytest integration tests against autobuild_runner are sufficient
verification.


Feature: autobuild_runner real-work execution (closes the stub introduced by FOLLOWUP-B-FIX)

Context
=======
The autobuild_runner subagent in src/forge/subagents/autobuild_runner.py
currently has four lifecycle-stub nodes (starting → planning_waves →
running_wave → completed). Per the inline comment at lines 982-989, these
were deliberately left empty to close the lifecycle-envelope contract
(FOLLOWUP-B) without committing to the autobuild-execution contract. As a
result, today:

- jarvis publishes pipeline.build-queued.FEAT-XXX correctly
- forge-prod's pipeline_consumer dequeues correctly
- The SSE bridge emits pipeline.build-started.FEAT-XXX and
  pipeline.build-complete.FEAT-XXX correctly within ~1 second
- BUT no code is actually written — guardkit autobuild is never invoked

This feature closes that gap by making _node_running_wave invoke
`guardkit autobuild feature <feature_id> --fresh --verbose` as an async
subprocess and map its exit code to the terminal lifecycle.

DDDSW demo target (2026-05-16) is FEAT-9E59 in
~/Projects/appmilla_github/api_test on the ddd-demo branch:
.guardkit/features/FEAT-9E59.yaml — a single TASK-VER-001 adding a GET
/version endpoint, ~33 min estimated wall-clock.

Scope (in)
==========
1. _node_running_wave invokes guardkit autobuild as an async subprocess.
   - Use asyncio.create_subprocess_exec with cwd=resolved_repo_path.
   - Inherit env from the sidecar process (ANTHROPIC_API_KEY etc.).
   - Subprocess command: [guardkit_path, "autobuild", "feature",
     feature_id, "--fresh", "--verbose"].
   - guardkit_path resolution: shutil.which("guardkit") with a
     configurable fallback via env var FORGE_GUARDKIT_PATH.

2. Repo-slug-to-path resolver.
   - Input: payload.repo (e.g. "appmilla/api_test").
   - Output: absolute Path to the local checkout.
   - Convention: <FORGE_REPO_BASE>/<basename-of-repo-slug>.
     Default FORGE_REPO_BASE=~/Projects/appmilla_github.
   - Validate the resolved path exists and is a git repo before running
     guardkit; if not, emit failed with a structured reason.
   - Validate the resolved path is inside forge's
     permissions.filesystem.allowlist; reject with a structured failed
     payload if not.

3. Exit-code → terminal lifecycle mapping.
   - Exit code 0: transition running_wave → completed; build-complete
     envelope publishes via the existing bridge translator.
   - Non-zero exit / timeout: add a new _node_failed terminal node and a
     conditional edge from running_wave that selects completed vs failed
     based on subprocess result. The build-failed envelope publishes via
     the existing emitter (PipelineLifecycleEmitter.emit_failed).

4. Optional: stream stdout for granular stage_complete emission.
   - Read subprocess.stdout line-by-line.
   - On each "[guardkit-checkpoint] Turn N complete (tests: pass|fail)"
     line, emit one stage_complete envelope.
   - If the implementation cost of streaming pushes this out of scope,
     cheap fallback: a single stage_complete emit between running_wave
     and completed when subprocess returns 0.

5. Operational artefact: update ~/forge-state/forge.yaml allowlist on the
   GB10 to include ~/Projects/appmilla_github/api_test. Document this as a
   per-host operational step, not a code change. Restart forge-prod after
   editing.

6. Smoke test path: a fast-finishing feature (a single completed-status
   feature, no real work) that exits in <30s, used to verify the wiring
   without the 33-min FEAT-9E59 wall-clock.

7. Sidecar restart workflow document update — autobuild_runner.py edits
   require `pkill -f "langgraph dev" && rm -rf .langgraph_api/ &&
   <re-launch>` per the multi-specialist runbook §2.0 pattern, since the
   sidecar is started with --no-reload.

Scope (out)
===========
- Local-only Anthropic-API proxy / vLLM setup. The DDDSW demo uses real
  Anthropic API; ANTHROPIC_API_KEY is set in the sidecar's env. (The
  archived script at guardkit/scripts/archive-vllm/vllm-serve.sh remains
  the path for an all-local future; out of scope here.)
- Multi-repo path mapping in forge.yaml. The convention + env var is
  sufficient for the current single-host layout.
- Concurrent autobuilds. The state machine is currently
  max_ack_pending=1 single-flight per ADR-ARCH-014; this feature
  preserves that.
- Branch parameter honouring beyond what guardkit autobuild itself does.
  guardkit operates within whatever git state cwd is on; jarvis publishes
  branch=ddd-demo, and we trust the operator to have checked that branch
  out before running the build. A follow-up can add explicit branch
  switching.
- PR creation / merge automation. guardkit autobuild leaves the worktree
  on autobuild/<feature_id> for human review; that's the contract.

Cross-repo verification path
============================
After this feature lands:

1. forge-side: rebuild forge:latest if any container code changed;
   restart langgraph-runner sidecar to load the new autobuild_runner.py.
2. forge-side: update ~/forge-state/forge.yaml allowlist; restart
   forge-prod.
3. jarvis-side: nothing — queue_build already publishes the correct
   payload.
4. api_test-side: checkout ddd-demo branch with FEAT-9E59 planned (done
   2026-05-14).
5. End-to-end smoke: from a jarvis chat REPL, paste the prompt from
   jarvis/docs/runbooks/RUNBOOK-jarvis-forge-autobuild-version-endpoint-demo.md
   §4.1. Expect ~33 min wall-clock, then verify:
   - pipeline.build-queued + build-started + (≥1 stage-complete) +
     build-complete on the wire
   - autobuild/FEAT-9E59 branch in api_test with Player-Coach commits
   - src/version/router.py exists with the VersionResponse schema and
     GET /version handler
   - tests pass when invoked from the worktree

Reference material
==================
- Current stub: src/forge/subagents/autobuild_runner.py lines 982-1030
- Lifecycle emitter contract: forge/pipeline/PipelineLifecycleEmitter
  (emit_started, emit_stage_complete, emit_complete, emit_failed)
- SSE bridge translator: src/forge/lifecycle_bridge/translator.py
- guardkit CLI surface: guardkit autobuild feature --help (in
  ~/Projects/appmilla_github/guardkit)
- Pipeline consumer allowlist check:
  src/forge/adapters/nats/pipeline_consumer.py:_path_inside_allowlist
- Last known-green wire roundtrip:
  jarvis/docs/runbooks/RESULTS-jarvis-multi-specialist-openwebui-dddsw-demo-2026-05-13.md
- Companion runbook to update post-implementation:
  jarvis/docs/runbooks/RUNBOOK-jarvis-forge-autobuild-version-endpoint-demo.md
- Operational lessons (worktree state, --fresh semantics):
  jarvis/docs/runbooks/autobuild-orchestration.md

Estimated complexity
====================
- ~2-4 hours for code changes (one runner node body + small resolver +
  new failed node + conditional edge)
- ~30 min for operational allowlist update + sidecar restart workflow
- ~60-90 min for one full end-to-end rehearsal against FEAT-9E59

DDDSW demo deadline: 2026-05-16. Day-before dress rehearsal: 2026-05-15.
