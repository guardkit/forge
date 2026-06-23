# INPUT — harness-context.md  ·  FF-05

> Candidate input. **Capture-pending:** paste the actual `guardkit` BDD-gate source + the project's (absent) conftest before the run.

Relevant mechanism: the **BDD gate runner** + pytest-bdd **collection**.

- pytest-bdd needs a `features/conftest.py` collection bridge to bind Gherkin scenarios to step definitions. With none present, `pytest <feature>` raises a **collection error** (exit 4) — distinct from a test *failure*. The gate maps exit-4 to "feature failed" instead of recognising "scenarios pending / unbound (tolerated scaffolding)".

> Capture from `~/Projects/appmilla_github/guardkit`: the BDD-gate runner and its exit-code interpretation (grep `exit 4` / the gate's pytest invocation), plus the canonical `features/conftest.py` template the installer ships (the bridge that should be present).
