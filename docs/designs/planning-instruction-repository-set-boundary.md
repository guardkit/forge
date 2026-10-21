# Planning instructions are not repository sets

## Problem

Forge finds phrases beginning with `all`, `every`, or `each` in the original
request and turns at most two of them into `sets_the_request_names` entries in
the target-repository descriptor. The planning consumer treats every supplied
entry as a claim about members that already exist in the repository. Its
ordinary-code check then requires the plan to cite shown repository lines for
each member and to include one aggregate acceptance promise for the set.

That contract is correct for requests such as “all existing tests” and “every
registered handler”. It is not correct for an instruction that constrains the
plan Forge is about to create. For example:

> All existing tests must still pass. Keep each planned task to five or fewer
> acceptance criteria, preserving every requested requirement and leaving room
> below the factory limit of eight.

The current extractor selects `all existing tests must` and `each planned task
to`. The first phrase describes an existing repository set. The second describes
future plan output, so its members cannot have starting-repository citations.
Simply dropping the second match is also incomplete: because extraction is
capped at two accepted phrases, `every requested requirement and` becomes the
next match. That phrase is part of the same plan-sizing instruction and is not a
repository-membership assertion either.

A prior design proposed removing a set when its repository search returned zero
matches. Independent review rejected that rule. “All existing tests” searches
for the modifier `existing`; a repository can contain a real test set without
containing that word. A complete zero-result search, a partial search, or an
unreadable repository therefore cannot justify removing an otherwise eligible
set.

## Pinned producer and consumer

The implementation base is Forge
`a3be5a5cc957d4363ea40fa5e904dd79b1b6aebc`. The connected path is:

1. `src/forge/planning/code_evidence.py` defines the quantifier grammar and
   `quantified_phrases()`. It currently chooses the first word after the
   quantifier as the search word and stops after two accepted phrases.
2. `PlanningRunDriver._sets_the_request_names()` in
   `src/forge/planning/driver.py` searches and constructs the entries.
3. `PlanningRunDriver._build_target_repo_descriptor()` places retained entries
   under `sets_the_request_names` and the normal feature-plan dispatch sends the
   descriptor together with the original request.
4. The planning consumer pinned in the private review record reads only the
   supplied descriptor entries through `plan_content_check.set_entries()`;
   `_check_sets()` and `_check_set_line()` enforce the existing member-citation,
   candidate-accounting, nonempty-discovery, and aggregate-acceptance controls.
   The registered-mode session attaches those findings to the must-pass
   `request_traceability` decision.

The planning consumer is enforcing the descriptor it receives. The false
classification originates in Forge, before the descriptor is built.

## Decision

Apply one conservative rule at phrase selection:

> A quantified object that lies inside a narrowly recognized instruction for
> producing this plan or performing the requested work is not a
> starting-repository set. Everything else keeps the existing guarded path.

Recognition is by complete local instruction shape, not by a blacklist of the
words `planned`, `task`, `requested`, `requirement`, `verification`, or
`automatically`. The initial implementation recognizes only these two observed
families:

1. A plan-sizing instruction beginning at a sentence or clause boundary, with
   `keep` or `limit`, a quantified `planned task` or `planned tasks` object,
   `to`, a nonzero decimal number or a number word from one through eight, `or
   fewer`, and `acceptance criteria`. When that instruction has a comma followed
   by `preserve every requested requirement` or `preserving every requested
   requirement`, exclude the requirement object only when it is followed
   directly by `and leaving room below the factory limit of` and the same bounded
   number form. A locator or other modifier between `requirement` and that
   continuation leaves the requirement phrase guarded. The recognized spans end
   at their own object text; they do not consume an adjacent clause merely
   because it is in the same sentence.
2. The complete bare-activity instruction “Complete all verification
   automatically”, optionally followed only by “within the factory”. Here
   `verification` is work to perform. Any artifact noun or repository locator,
   including `tests`, `scripts`, `jobs`, a stored backlog, or a path, leaves the
   phrase on the existing guarded path.

Use one private helper in `code_evidence.py` to return the exact quantified-object
spans recognized by these instruction families. The plan-sizing recognizer must
return separate spans for the planned-task object and the attached
requested-requirement object. `quantified_phrases()` skips a regex match only
when that match's quantified object is inside one of those spans. It skips before
deduplication and before incrementing the existing `MAX_SET_PHRASES` quota, so an
instruction cannot hide a later genuine repository set.

Match case-insensitively, allow ordinary whitespace and punctuation at the
stated boundaries, and do not cross a sentence boundary. An unrecognized
variation remains eligible for repository-set handling. This is a deliberately
incomplete grammar with a conservative default, rather than a claim to classify
arbitrary English.

The original request is not edited. It continues to reach the plan writer and
semantic `request_traceability` review word for word, so task sizing,
requirement preservation, and automatic execution remain requirements. This
change removes only the inappropriate repository-member and citation contract.

## Preserved controls

No result of a repository search participates in the decision above. For every
phrase that remains eligible:

- zero matches still produce the existing descriptor entry;
- positive, truncated, partly read, and unreadable searches retain their current
  entry shape and truth about completeness;
- `listed_all` keeps its current narrow meaning and never claims semantic set
  membership;
- the candidate and evidence budgets remain unchanged; and
- the consumer's P7/P8 checks remain unchanged, including dynamic discovery
  for a set with no candidates, citations for member lines, accounting for every
  candidate, and the aggregate acceptance promise.

These examples stay guarded:

- “Run all existing tests automatically.”
- “Run all verification scripts automatically.”
- “Audit every planned task in the stored backlog.”
- “Check every requested requirement in `requirements.yaml`.”
- an incomplete or unfamiliar plan instruction that does not match the bounded
  grammar.

An adjacent genuine set stays guarded even when it shares a sentence with a
recognized planning instruction. A full-sentence exclusion would violate this
contract.

## Implementation scope

The implementation owner is the coordinating Codex session after independent
design acceptance. The source change is limited to
`src/forge/planning/code_evidence.py`; focused regression tests may change only
the existing Forge planning test files named below.

Do not change the descriptor schema, `PlanningRunDriver`, dispatch arguments,
planning-consumer source, product repositories, product specifications, product
plans, or product tests. Do not add configuration, a fallback descriptor field,
a model call, a growing noun list, or a second classifier. Do not manufacture
citations for future plan files, suppress P7/P8, or use search results to infer
that a phrase is merely procedural.

## Acceptance tests

Add public, synthetic tests without estate names, release identifiers, build
identifiers, private paths, or copied trace text.

### Phrase selection

In `tests/forge/planning/test_repository_facts_sources.py`, prove:

1. The example sentence in the Problem section retains only the existing-tests
   set. Neither the planned-task phrase nor its attached requested-requirement
   phrase consumes a quota slot.
2. Put the complete sizing/preservation instruction before two genuine sets,
   such as all existing tests and every registered handler. Both genuine sets
   fill the existing two slots, in order.
3. Put a genuine existing-tests clause next to the sizing instruction in the
   same sentence. The existing-tests phrase remains, proving that the
   implementation excludes object spans rather than the sentence.
4. “Complete all verification automatically” and the allowed “within the
   factory” suffix are excluded. “Run all existing tests automatically” and
   “Run all verification scripts automatically” remain.
5. Stored-artifact forms remain: every planned task in a stored backlog and
   every requested requirement in a named requirements file. Bare uses of the
   individual words also remain unless the complete recognized instruction is
   present.
6. Existing numeric-date exclusions, ordinary all/every/each extraction,
   deduplication, ordering, singularization, and the two-phrase cap remain
   unchanged.

### Descriptor and real dispatch seam

Use the existing in-memory/fake readers and local synthetic Git repositories;
no network, daemon, broker, model, or product checkout is needed.

1. Call `PlanningRunDriver._sets_the_request_names()` with the combined request
   and a reader returning zero hits for `existing`. The existing-tests entry
   must still be emitted with zero candidates; no planned-task or
   requested-requirement entry may be emitted.
2. Reuse the existing positive, truncated, partly-read, and unreadable cases and
   assert their output is unchanged for retained sets.
3. Extend the normal planning-driver test in
   `tests/forge/planning/test_driver_target_terminal.py`. Drive the public
   synthetic sentence through the normal target-descriptor construction and
   feature-plan command-argument builder, then inspect the captured
   `target_repo_descriptor` on the real plan dispatch. It must carry the
   retained genuine set and no instruction-only set. The captured
   `request_text` must still equal the complete input sentence.
4. Keep the existing dispatcher contract-pin and specialist-dispatcher tests
   green. They prove that the descriptor object travels unchanged through the
   caller rather than through a helper-only substitute.

### P7/P8 positive and negative controls

The planning consumer remains pinned and unchanged. Its focused tests must
continue to prove both sides of the boundary:

- a retained set with zero candidates still requires the existing dynamic
  discovery/nonempty aggregate form;
- a retained set with two candidate files still fails when one file is not
  accounted for or a member has no shown `path:line` citation;
- accounting for both candidates and supplying the valid aggregate line clears
  those findings; and
- absence of a descriptor entry creates no P7/P8 obligation, while the exact
  request remains available to semantic request-traceability review.

The Forge dispatch test above is the producer-side proof for the last case. The
unchanged consumer tests are the other side of the proof; no consumer relaxation
is part of this repair.

## Acceptance commands

Run the same focused Forge commands at the pinned base and at the implementation
candidate. Run the privately pinned planning-consumer controls once, and run the
normal Forge suite at the candidate. Every pytest invocation goes through `dell-test`
with xdist, the cache provider, and inherited pytest options disabled. The root
operator records the exact checkout commits, commands, exit codes, and any
baseline difference. Use GuardKit
`cdc6fd369519fc78234215e06fc4331b385f9313`, nats-core
`1a04654892c7cf8f046e02f73efed308b9b2e850`, and fleet-memory
`f5d8465aa106fd9bf620c2f70b666547d07fa83b` for both the Forge base and
candidate. Record the resolved import locations before either run.

```sh
: "${FORGE_CHECKOUT:?set FORGE_CHECKOUT to the exact Forge checkout}"
: "${FORGE_PYTHON:?set FORGE_PYTHON to an absolute Forge virtualenv interpreter}"
: "${GUARDKIT_CHECKOUT:?set GUARDKIT_CHECKOUT to the pinned GuardKit checkout}"
: "${NATS_CORE_CHECKOUT:?set NATS_CORE_CHECKOUT to the pinned nats-core checkout}"
: "${FLEET_MEMORY_CHECKOUT:?set FLEET_MEMORY_CHECKOUT to the pinned fleet-memory checkout}"
case "$FORGE_PYTHON" in /*) ;; *) echo 'FORGE_PYTHON must be absolute' >&2; exit 2 ;; esac
test "$(git -C "$GUARDKIT_CHECKOUT" rev-parse HEAD)" = \
  cdc6fd369519fc78234215e06fc4331b385f9313 || exit 2
test "$(git -C "$NATS_CORE_CHECKOUT" rev-parse HEAD)" = \
  1a04654892c7cf8f046e02f73efed308b9b2e850 || exit 2
test "$(git -C "$FLEET_MEMORY_CHECKOUT" rev-parse HEAD)" = \
  f5d8465aa106fd9bf620c2f70b666547d07fa83b || exit 2
cd "$FORGE_CHECKOUT"
dell-test env \
  PYTHONPATH="$FORGE_CHECKOUT/src:$GUARDKIT_CHECKOUT:$NATS_CORE_CHECKOUT/src:$FLEET_MEMORY_CHECKOUT/src" \
  "$FORGE_PYTHON" -c \
  'import forge, nats_core, fleet_memory; from guardkit.lib import pytest_argv; from installer.core.commands.lib import smoke_gates_nudge; print(forge.__file__); print(pytest_argv.__file__); print(smoke_gates_nudge.__file__); print(nats_core.__file__); print(fleet_memory.__file__)'
dell-test timeout 1800 env PYTEST_ADDOPTS= \
  PYTHONPATH="$FORGE_CHECKOUT/src:$GUARDKIT_CHECKOUT:$NATS_CORE_CHECKOUT/src:$FLEET_MEMORY_CHECKOUT/src" \
  "$FORGE_PYTHON" -m pytest -q -p no:xdist -p no:cacheprovider -o addopts= \
  tests/forge/planning/test_repository_facts_sources.py \
  tests/forge/planning/test_driver_target_terminal.py \
  tests/forge/planning/test_target_terminal_contract_pin.py \
  tests/forge/pipeline/dispatchers/test_specialist.py
```

The private review record must name the exact planning-consumer checkout and
full commit and retain the exact focused consumer commands and test paths. Run
those recorded commands through `dell-test` with `PYTEST_ADDOPTS=`,
`-p no:xdist`, `-p no:cacheprovider`, and `-o addopts=`. Record the command,
resolved source location, commit and result privately. Consumer acceptance is
required even though its identity and repository paths do not belong in this
public document.

Then run the full Forge test tree with the same candidate checkout and
interpreter:

```sh
cd "$FORGE_CHECKOUT"
dell-test timeout 7200 env PYTEST_ADDOPTS= \
  PYTHONPATH="$FORGE_CHECKOUT/src:$GUARDKIT_CHECKOUT:$NATS_CORE_CHECKOUT/src:$FLEET_MEMORY_CHECKOUT/src" \
  "$FORGE_PYTHON" -m pytest -q -p no:xdist -p no:cacheprovider -o addopts= \
  tests
```

Do not treat a helper-only unit result as acceptance when the real dispatch test
has not passed. The selected files include tests that use local fake HTTP
servers and require ordinary host-loopback access. If a managed runner blocks
that access, repeat the same command with operator-approved host permissions;
do not exclude or weaken those tests.

## Review and activation boundary

Implementation waits for independent acceptance of this committed design. The
implementation review must compare the exact candidate with the pinned base,
inspect the complete phrase-selection and real-dispatch tests, and confirm there
is no descriptor or planning-consumer change.

Tests establish the source boundary only. Building images, installing the
candidate, changing a running service, and exercising a live planning request
are separate operator-owned steps. Live acceptance must use the reviewed Forge
commit with the planning consumer pinned in the private review record and must
demonstrate both a planning instruction that no longer creates repository
citation demands and a genuine repository set that still does.
