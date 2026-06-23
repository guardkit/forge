# INPUT — harness-context.md  ·  FF-03

> Candidate input. **Capture-pending:** paste the actual `guardkit` scanner source before the run.

Relevant mechanism: `_scan_ac_for_missing_paths` — the acceptance-criterion path scanner that runs when **no implementation plan is on disk**.

- It treats any **backtick span ending in `.<ext>`** as a file path and existence-checks it. A command span (`` `grep … src/foo.py src/bar.py` ``) collapses to one nonexistent "path"; a markdown-link **label** (`relay/service.py`) is read as a path. Either yields a false "missing file" → high violation → aborted evidence gathering → unwinnable Coach loop.

> Capture from `~/Projects/appmilla_github/guardkit`: the `_scan_ac_for_missing_paths` function (grep the name). Paste its tokenisation / path-extraction logic — that is what the proposer must repair.
