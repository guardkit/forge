# GOLD (answer key) — FF-03 plan-audit-ac-path-misparse

> **Held-out.** Source: quirks#3 (backtick variant) + FEAT-MEM-07 RIP-002 (markdown-link variant) — two variants of one bug.

**label:** `false-failure`

## Gold diagnosis

Not a code defect. `_scan_ac_for_missing_paths` (invoked when no plan is on disk) misparses AC text into file paths: any backtick span ending in `.ext` becomes a path, and markdown-link labels are read as paths. The "missing file" is an artefact of tokenisation; the real source is under `src/fleet_memory/…`. The false high-violation aborts evidence gathering → unwinnable Coach loop.

## Gold fix

- **Immediate (authoring workaround):** word ACs so commands/labels don't sit in a single backtick/label span ending in a path token — put the pattern and each real path in separate backticks.
- **Harness (the graded fix):** repair `_scan_ac_for_missing_paths` so it resolves markdown-link labels to their targets and splits command spans before existence-checking — i.e. only treat genuine path tokens as paths.

## verify.sh (held-out)

```bash
#!/usr/bin/env bash
set -euo pipefail
# Run plan_audit against a fixture AC for EACH variant:
#   (a) `grep -n foo src/a.py src/b.py`   (b) [see relay/service.py](...)
# ASSERT no false missing-path violation is raised for either.
# <invoke guardkit plan_audit / _scan_ac_for_missing_paths on the fixtures>
```
