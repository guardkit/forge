"""The time limits a repair leg's outer wall is built from, in one place.

Three parts of Forge must agree on these numbers, and before 18 October 2026
each part held its own:

* the dispatcher, which sets each leg's outer limit to the leg's own budget
  plus a margin (``leg_stage_timeout`` in
  ``forge.pipeline.dispatchers.conductor_subprocess``);
* the sandbox helper, which cuts any leg's wall back to
  :data:`LEG_TIMEOUT_MAX` (``forge.deploy_sidecar.service``);
* config loading, which refuses a leg budget whose outer limit would be
  longer than :data:`LEG_TIMEOUT_MAX`, so the helper never has to cut a
  wall the dispatcher worked out (``forge.config.models.BudgetGuards``).

This module imports nothing, so any of the three can read it without
pulling the others in.
"""

from __future__ import annotations

#: The longest wall the sandbox helper allows one leg, in seconds. A leg
#: asked for longer is cut back to this and told so in plain words.
LEG_TIMEOUT_MAX: float = 7200.0

#: How far a review leg's outer limit stays above its model budget
#: (``leg_sdk_timeout_seconds``), in seconds.
REVIEW_LEG_OUTER_MARGIN_SECONDS: int = 60

#: How far a work leg's outer limit stays above the larger of its loop
#: budget (``leg_budget_seconds``) and its model budget, in seconds.
WORK_LEG_OUTER_MARGIN_SECONDS: int = 180

__all__ = [
    "LEG_TIMEOUT_MAX",
    "REVIEW_LEG_OUTER_MARGIN_SECONDS",
    "WORK_LEG_OUTER_MARGIN_SECONDS",
]
