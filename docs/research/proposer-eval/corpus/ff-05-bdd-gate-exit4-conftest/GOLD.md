# GOLD (answer key) — FF-05 bdd-gate-exit4-conftest

> **Held-out.** Source: FEAT-MEM-07 (all RIP tasks).

**label:** `false-failure`

## Gold diagnosis

Not a code defect. There is no `features/conftest.py` collection bridge, so pytest-bdd cannot bind the Gherkin scenarios → a pytest **collection error (exit 4)**, which the gate misreads as a build failure. The 56 "failures" are all `StepDefinitionNotFoundError` (zero assertion failures) — the scenarios are simply **pending/unbound**, which is the tolerated scaffolding state.

## Gold fix

Install the canonical `features/conftest.py` collection bridge + pending step-glue so scenarios **collect and pend** (tolerated) instead of erroring. This is repo-wide infrastructure: it fixes the BDD exit-4 for **every** feature, not just this one. (Optionally, also harden the gate so a pure collection-of-pending state is not treated as a hard failure.)

## verify.sh (held-out)

```bash
#!/usr/bin/env bash
set -euo pipefail
# With the candidate's conftest bridge applied:
pytest <feature-path> --collect-only   # ASSERT exit 0, scenarios collected, ZERO collection errors
# <run the BDD gate>                    # ASSERT it no longer exits 4 (pending scenarios tolerated)
```
