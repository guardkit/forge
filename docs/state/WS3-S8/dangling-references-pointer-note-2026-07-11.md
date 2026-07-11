# WS3-S8 forge sweep — dangling-reference pointer note (2026-07-11)

The task audit (guardkit `d378c5e3`) flags four `TASK-*` ids named by first-party
source / feature YAMLs that no forge task file declares. None is mechanically
resolvable under the sweep rules (no source edits, no invented task files), so
each is recorded here and reported as a residual. `docs/state` stubs do **not**
clear a dangling reference (by tool design), so these persist in re-audit.

| Dangling id | Referenced by | Disposition |
|---|---|---|
| `TASK-ABW-002` | `src/forge/subagents/autobuild_runner.py:1142,1330` (TEMP HOTFIX markers) | forge-local: a tracked hotfix follow-up that was never filed as a task file. Left for a forge owner to either file the task or retire the marker; not invented here. |
| `TASK-JNB-004` | `.guardkit/features/FEAT-1872.yaml:8` (description prose) | Cross-repo: jarvis task (v1 checkpoint, 2026-07-04). Prose mention only; canonical file lives in jarvis. Benign. |
| `TASK-JNB-107` | `src/forge/cli/serve.py:367`, `src/forge/cli/_serve_deps_gating.py:127` (operator-probe comments) | Cross-repo: jarvis live-validation task (§3 canon). Comment label only; canonical file lives in jarvis. Benign. |
| `TASK-MEM-008` | `.guardkit/features/FEAT-FMDR.yaml:5,128` (description prose) | Cross-repo: fleet-memory task ("first harvested exemplar"). Prose mention only; canonical file lives in fleet-memory. Benign. |

Three of the four (`JNB-004`, `JNB-107`, `MEM-008`) are cross-repo prose/comment
mentions of tasks owned by sibling repos — expected and harmless. Only
`TASK-ABW-002` is a forge-local marker with no backing task file; it is a
disposition question for a forge owner, out of scope for a mechanical tracker sweep.
