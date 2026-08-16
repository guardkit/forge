# `guardkit qa normalize-stamps` stdout shapes (captured 2026-08-16)

Captured VERBATIM from the guardkit-side normalizer (guardkit branch
`lane/stamp-normalizer-0816` @ 33cf6c73, `guardkit/cli/qa.py`) driven as a
subprocess with stdout NOT a tty over the api_test `5bc6fd1` fixture's
FEAT-TIME with its `scenarios:` map stripped (the worktree path replaced by
`/wt`). Note the rich console echo AFTER the JSON on the refusal — wrapped at
80 columns because stdout is a pipe — which is why forge's parser prefers the
JSON and treats the echo as a last resort.

- `written-stdout.txt`        exit 0, 4 stamps minted + written
- `nothing-to-do-stdout.txt`  exit 0, all 4 already stamped, `written: false`
- `refusal-stdout.txt`        exit 2, two undecidable titles (JSON `refused` + echo)
- `not-found-stdout.txt`      exit 2, cannot run (`error`, `refused: []`)

An OLDER guardkit (no such subcommand) prints nothing on stdout and
`Error: No such command 'normalize-stamps'.` on stderr, exit 2 (click).

## The PARTIAL-WRITE law (guardkit `lane/stamp-normalizer-0816` @ a8e5bfa3, captured 2026-08-16)

Same api_test `5bc6fd1` fixture, `scenarios:` stripped, driven as a subprocess
with stdout NOT a tty. The normalizer now WRITES every decided stamp and names
the undecidable titles in the JSON `refused` list; the human echo moved to
STDERR. Exit 3 = PARTIAL (distinct from 2 = cannot run, 0 = all decided).

- `partial-stdout.txt`        exit 3, two undecidable titles ADDED to the
                              feature; the four decidable ones stamped +
                              written (`written: true`, `rules` per title)
- `partial-stderr.txt`        the same run's stderr: guardkit's own WARNING
                              lines + the rich `! normalize-stamps PARTIAL:`
                              echo (wrapped at 80 cols — the last-resort
                              title source when stdout's JSON is clipped)
- `partial-none-stdout.txt`   exit 3, EVERY title undecidable — nothing
                              decided, `written: false`, `stamped: {}`
