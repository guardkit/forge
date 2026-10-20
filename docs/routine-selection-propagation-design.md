# Routine selection propagation for sandbox builds

## Problem

Forge validates `routine.seat` and `routine.timeout_multiplier` in the
coordinator's `forge.yaml`. A routine build that runs in the coordinator can
therefore receive those settings directly. A routine build routed to a
repository sandbox crosses a different boundary:

1. the coordinator calls `dispatch_autobuild_async`;
2. the async-task adapter serialises its launch context;
3. the sandbox's `autobuild_runner` reconstructs that context and launches
   `guardkit autobuild`.

The launch context previously carried the build, repository, branch, budget,
memory and declared setting names, but did not carry the routine model or
timeout multiplier. The runner tried to recover both from its own
`FORGE_CONFIG_PATH`. A dedicated sandbox settings file contained the sandbox's
planning configuration, not the coordinator's routine selection, so the
runner added neither `--model` nor `--timeout-multiplier`. GuardKit then used
its own model default.

Copying the coordinator's routine section into every sandbox settings file
would create several independent copies of a setting that is expected to
change. A registered sandbox could then drift from the coordinator that chose
where to send the build.

## Existing boundaries

The relevant paths are:

| Path | Current responsibility | Design consequence |
| --- | --- | --- |
| `src/forge/config/models.py` | Validates the optional routine seat and multiplier. | Keep this as the public configuration contract. |
| `src/forge/cli/_serve_deps.py` | Composes fresh launches, approval resumes and boot re-arms from the validated `ForgeConfig`. | Resolve the selection here for every coordinator-driven launch. |
| `src/forge/pipeline/dispatchers/autobuild_async.py` | Builds the internal async launch payload. | Add one optional, JSON-safe routine-selection entry. |
| `src/forge/cli/_serve_async_task_starter.py` | Serialises the launch payload onto the existing async-task request. | No new transport or authentication surface is needed. |
| `src/forge/subagents/autobuild_runner.py` | Reconstructs the payload and creates the `guardkit autobuild` argv. | Prefer a supplied, validated selection; retain local loading only for an old coordinator. |
| `src/forge/cli/serve.py` and `src/forge/pipeline/dispatchers/subprocess_dispatcher.py` | Bind the routine seat onto synchronous routine-stage commands. | Leave this already-working path unchanged. |
| `src/forge/cli/register_repo.py` | Creates a minimal settings file for a dedicated sandbox. | Do not add routine settings to `NEW_SANDBOX_SETTINGS`. |

Build requests and repair requests both reach the same pipeline consumer and
fresh-launch closure. An approval resume and a boot-time re-arm also use the
same `_build_resume_launcher`. GuardKit's in-process repair rounds remain in
the subprocess that was launched with the selected model and multiplier. The
conductor's fix journey is separate and continues to use `conductor.seat`.

## Decision

Carry a snapshot of the coordinator's validated routine selection in the
existing async launch payload.

The payload gains one optional mapping, named `routine_selection`, with this
shape:

```json
{
  "routine_selection": {
    "seat": "openai:configured-seat",
    "timeout_multiplier": 1.0
  }
}
```

The coordinator-driven production composition always supplies the mapping,
even when both values are null. This distinction is required:

- no `routine_selection` key means the launch came from an older coordinator,
  so a newer runner preserves the old local-config lookup;
- a present mapping with null values means the current coordinator explicitly
  selected no override, so the runner must not consult possibly stale sandbox
  settings;
- a present mapping with values is the selection for that launch.

The coordinator builds this wire mapping explicitly from the already-validated
`forge_config.routine.seat` and
`forge_config.routine.timeout_multiplier` properties. It must contain exactly
those two keys; it is not a dump of the whole `RoutineConfig` model. The
selection is injected by the composition root, not copied from
`BuildQueuedPayload`, so callers that can queue work cannot choose a model
through the build-request schema.

`dispatch_autobuild_async` accepts the mapping as an optional keyword-only
argument. It adds the entry only when the argument is not `None`. Existing
direct callers and older tests therefore retain their current payload bytes.
The async-task adapter already carries arbitrary JSON-safe context fields and
needs no special-case change.

The runner resolves both values together:

1. If `routine_selection` is present, require a mapping with exactly `seat`
   and `timeout_multiplier`, then validate those values again with
   `RoutineConfig`. A malformed mapping, an option-shaped seat, a missing or
   unknown key, or an out-of-range multiplier fails the build. It must not
   silently use GuardKit's default or the sandbox's local file. Keeping the
   wire keys explicit prevents a future defaulted `RoutineConfig` field from
   appearing accidentally in launches to older runners. If this wire contract
   ever gains a field, runner support must be deployed before a coordinator
   starts sending it, because unknown wire keys are deliberately refused.
2. If the key is absent, use the existing `_load_routine_seat` and
   `_load_routine_timeout_multiplier` behaviour. This is the rolling-upgrade
   path for an old coordinator talking to a new runner.
3. Append `--model` and `--timeout-multiplier` in the same positions and string
   forms used today. A valid supplied null adds nothing.

Resolve and validate the selection at the top of `_run_one_build`, immediately
after the launch payload and feature identifier are checked. A malformed
selection must return a failed snapshot before repository resolution, a
repository claim, any residue or reference sweep, source-branch creation,
worktree materialisation, GuardKit lookup or subprocess creation. The resolved
values are retained and appended later at the existing argv construction site.

Revalidation at the runner is defence in depth across the serialized process
boundary. The route, sandbox admission check, async-task endpoint and launch
authorization do not change. No model value is accepted from the external
build-queued message, and no new endpoint is introduced.

The snapshot is per launch. A retry inside the same runner thread sees the
same launch message. A coordinator re-dispatch, repair build, approval resume
or boot re-arm creates a new launch from that coordinator process's validated
configuration. This prevents a sandbox-local copy from deciding the selection
while keeping a single launch deterministic. The failure pack's existing
`plan_relaunch` command is a manual advisory path and predates this hand-off;
making that suggested command reproduce the selection is explicitly outside
this correction.

## Implementation shape

The implementation should stay within these seams:

1. Add a small helper in `_serve_deps.py` that explicitly returns only
   `seat` and `timeout_multiplier` from the validated
   `forge_config.routine`. Pass it to both uses of `_build_resume_launcher`:
   fresh pipeline consumption and the public boot-rearm composer.
2. Add the optional keyword-only `routine_selection` parameter to
   `_build_resume_launcher` and `dispatch_autobuild_async`, and forward it
   unchanged into the launch payload when supplied. Both dispatch arms in
   `_build_resume_launcher` must receive it: the prepared-build arm carrying
   `source_commit` and the ordinary arm.
3. Add one runner resolver that returns the seat and multiplier as a pair.
   Call it at the top of `_run_one_build`, use the payload mapping when present
   and the two existing file loaders only when absent.
4. Extend the existing runner launch log with the exact fields
   `timeout_multiplier=` and `selection_source=`. The multiplier is separate
   from the wall-clock `timeout`, and the source is one of
   `coordinator-payload`, `coordinator-explicit-none`, or
   `legacy-local-file`. Only a mapping where both values are null uses
   `coordinator-explicit-none`; a half-populated mapping uses
   `coordinator-payload`. Log `unnamed` when no multiplier is resolved. Do not
   log the complete argv or any unrelated payload content.
5. Keep `NEW_SANDBOX_SETTINGS`, `BuildQueuedPayload`, the synchronous
   subprocess dispatcher and conductor wiring unchanged.

The implementation should not persist the selection on the build row. The
coordinator already owns the source configuration, the launch payload is the
existing hand-off to the runner, and adding another durable copy would create
new replay semantics unrelated to this defect.

## Compatibility and release behaviour

| Coordinator | Sandbox runner | Behaviour |
| --- | --- | --- |
| old | old | Existing local settings lookup. |
| old | new | Key absent; existing local settings lookup. |
| new | old | Extra JSON field is ignored; safe during rollout, but the correction is not active for that launch. |
| new | new | Coordinator selection is validated and applied. |

The release must therefore update the coordinator and sandbox runner surfaces
before treating the live check as proof. No sandbox settings migration is
required. Removing or changing the coordinator's routine selection takes
effect on launches made after the coordinator reloads its configuration by
restarting; it does not require editing registered sandbox files.

## Verification to add with the implementation

Run all tests on the Dell through `dell-test`, without xdist.

Focused tests should prove:

1. the production fresh-build composer puts the exact validated seat and
   multiplier into the async launch context for both an ordinary build and a
   prepared build carrying `source_commit`;
2. the approval-resume and boot-rearm composer uses the same selection,
   including the approval-resumed prepared-build arm;
3. `dispatch_autobuild_async` omits the new key for a legacy direct caller,
   preserves an explicitly empty mapping, and serialises populated values;
4. the async-task description round-trip preserves the mapping;
5. a runner payload selection overrides conflicting values in its local
   settings file;
6. an explicitly empty selection suppresses values found in that local file;
7. an absent selection retains the current local-file fallback;
8. malformed payload values fail before repository resolution, claim, sweep,
   source-branch creation, worktree materialisation, GuardKit lookup or
   `create_subprocess_exec`;
9. the final argv contains both flags once, after the existing arguments, and
   existing no-selection argv tests remain unchanged;
10. a republished repair build and a re-armed build pass through the same
    selection-bearing launch seam; and
11. the launch log names the resolved timeout multiplier and asserts each of
    the three sources: `coordinator-payload`, `coordinator-explicit-none`, and
    `legacy-local-file`; and
12. existing `RoutineConfig` validation tests still reject invalid settings.

Suggested focused command, from the implementation checkout:

```bash
~/.local/bin/dell-test timeout 1800 .venv/bin/python -m pytest -q \
  tests/forge/test_dispatch_autobuild_async.py \
  tests/forge/test_serve_async_task_starter.py \
  tests/cli/test_serve_deps.py \
  tests/integration/test_gate_restart_recovery.py \
  tests/forge/subagents/test_autobuild_runner_routine_seat.py \
  tests/forge/subagents/test_autobuild_runner_routine_multiplier.py
```

After the focused tests, run the repository's normal Forge suite through the
same wrapper. Do not use xdist.

## Live acceptance and model-record meaning

After release, inspect a newly launched sandbox build rather than a build that
was already running during deployment. Its runner launch line must show the
coordinator's configured model alias, `timeout_multiplier=1.0`, and
`selection_source=coordinator-payload`; the separate `timeout=` value remains
the subprocess wall-clock bound. The GuardKit run events must show the alias as
the requested model. Repeat this for the next ordinary build or repair build
that exercises the sandbox route.

This proves the factory requested the intended selection. It does not, by
itself, prove which physical model served the inference. LangChain response
metadata may omit `model_name`; the current recording path then falls back to
the request alias. Consequently a run record whose `model` and
`requested_model` both equal the alias is honest evidence of the request, not
gateway-backed evidence of the physical backend. Provider metadata can add
context, but the provider name and alias still do not identify the loaded
weights. Physical-backend attestation would require authoritative gateway or
server telemetry and is outside this narrowly scoped correction.
