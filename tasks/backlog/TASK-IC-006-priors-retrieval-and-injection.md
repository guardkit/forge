---
id: TASK-IC-006
title: "Priors retrieval and prose injection"
status: backlog
created: 2026-04-25T14:36:00Z
updated: 2026-04-25T14:36:00Z
priority: high
task_type: feature
tags: [memory, priors, prompt-injection]
complexity: 5
parent_review: TASK-REV-IC8B
feature_id: FEAT-FORGE-006
wave: 3
implementation_mode: task-work
dependencies: [TASK-IC-002]
estimated_minutes: 120
consumer_context:
  - task: TASK-IC-002
    consumes: priors_prose_injection_schema
    framework: "DeepAgents reasoning model system prompt (str.format with {domain_prompt})"
    driver: "domain_context_injection_specialist pattern"
    format_note: "Four named prose sections: recent_similar_builds, recent_override_behaviour, approved_calibration_adjustments, qa_priors. Empty sections render as '(none)' marker — never omitted."
---

# Task: Priors retrieval and prose injection

## Description

At build start, retrieve four categories of priors via parallel `asyncio.gather()`
sub-queries against Graphiti. Assemble results into a structured prose block
with four named sections. Inject into the reasoning-model system prompt via
the `{domain_prompt}` placeholder.

Covers `@key-example key-priors-retrieval-runtime`, `@key-example key-priors-retrieval-qa`,
`@boundary boundary-expired-adjustments`, `@edge-case empty-priors-representation`,
`@edge-case @security priors-as-argument-refusal`.

## Module: `forge/memory/priors.py`

```python
@dataclass
class Priors:
    recent_similar_builds: list[SessionOutcome]
    recent_override_behaviour: list[OverrideEvent]
    approved_calibration_adjustments: list[CalibrationAdjustment]
    qa_priors: list[CalibrationEvent]

async def retrieve_priors(build_context: BuildContext) -> Priors:
    """Issue four concurrent Graphiti queries; return assembled Priors."""

def render_priors_prose(priors: Priors) -> str:
    """Render Priors to a structured prose block with four named sections.
    Empty sections rendered as '(none)' marker, never omitted."""
```

## Acceptance Criteria

- [ ] Four parallel queries via `asyncio.gather()` (one per category)
- [ ] Recency filter: similar builds, override behaviour, adjustments,
      Q&A priors all bounded by configurable horizon (default 30 days)
- [ ] Approved-adjustments query filters `approved=True` AND
      `expires_at > now()` (`@boundary boundary-expired-adjustments`)
- [ ] Empty section renders as the literal `(none)` marker, NEVER omitted
      (`@edge-case empty-priors-representation`)
- [ ] Prose block injected via `{domain_prompt}` placeholder in system
      prompt template (mirror `domain-context-injection-specialist` pattern)
- [ ] Priors are NEVER passed as subprocess arguments
      (`@edge-case @security priors-as-argument-refusal`) — assert in code +
      test
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- [ ] `tests/unit/test_priors_retrieval.py` — mocked Graphiti returning
      various combinations (all empty, partial, full); assert four queries
      issued concurrently
- [ ] `tests/unit/test_priors_prose.py` — empty section produces `(none)`,
      not omitted; section order is stable
- [ ] `tests/unit/test_priors_not_in_argv.py` — assert no priors content
      reaches `subprocess.Popen.args`
- [ ] BDD step impls for the listed `@key-example`, `@boundary`, `@edge-case`,
      `@security` scenarios (TASK-IC-011)

## Seam Tests

The following seam test validates the integration contract with the producer task. Implement this test to verify the boundary before integration.

```python
"""Seam test: verify priors_prose_injection_schema from TASK-IC-002."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("priors_prose_injection_schema")
def test_priors_prose_section_schema():
    """Verify priors prose has the four required named sections.

    Contract: Four named sections (recent_similar_builds, recent_override_behaviour,
    approved_calibration_adjustments, qa_priors). Empty sections render as
    '(none)' marker — never omitted.
    Producer: TASK-IC-002 (Graphiti write) → consumed by reasoning model
    """
    from forge.memory.priors import Priors, render_priors_prose

    empty = Priors(
        recent_similar_builds=[],
        recent_override_behaviour=[],
        approved_calibration_adjustments=[],
        qa_priors=[],
    )
    prose = render_priors_prose(empty)
    for section in [
        "recent_similar_builds",
        "recent_override_behaviour",
        "approved_calibration_adjustments",
        "qa_priors",
    ]:
        assert section in prose, f"Section {section} missing from prose"
    assert prose.count("(none)") == 4, \
        "Each empty section must render exactly one '(none)' marker"
```

## Implementation Notes

- Use `asyncio.gather()` to issue all four queries concurrently — wall-clock
  cost should be ~one round-trip latency, not four.
- Recency horizon should be a single config value reused across all four
  queries.
- The "no priors as argv" assertion is a small but important safety: a
  helper `assert_not_in_argv(text: str)` walks `sys.argv` and any pending
  subprocess invocations to confirm no priors text leaks. Document why.
- Coordinate with the existing system-prompt template module
  (`agents/system-prompt-template-specialist`); prose injection follows the
  same `str.format()` placeholder pattern.
