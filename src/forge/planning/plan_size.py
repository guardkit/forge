"""One plain warning when a plan is much bigger than the project's usual.

WHY (6 October 2026, planning improvements item 6). One delete-user plan
came back with eight tasks and about nine hours of estimated work for a
sentence the project's other plans had done in five tasks. The owner chose a
warning, never a cap: the build-gate card says when a plan is half as big
again as the median of the project's own earlier plans, by task count or by
the plan's own minutes estimate, and starting it stays his choice.

Only GuardKit's own plan format is read (``.guardkit/features/*.yaml``:
``tasks``, each task's ``estimated_minutes``, ``orchestration.
parallel_groups``), through the planner's repository reader. No fixed number
of tasks is anywhere here. At least five earlier plans with two or more tasks,
an estimate on every task and a positive total are needed, or no comparison
is made. Any read
or parse failure is no line. Nothing here can stop a plan or a build.
"""

from __future__ import annotations

import fnmatch
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

__all__ = [
    "MIN_EARLIER_PLANS",
    "PlanSize",
    "plan_size_note",
    "plan_size_of",
    "read_plan_size_note",
]

#: Where GuardKit keeps a project's plans.
_PLAN_PATTERN = ".guardkit/features/*.yaml"
#: At most this many earlier plans are read.
_MAX_EARLIER_PLANS_READ = 60
#: Fewer comparable earlier plans than this, and no comparison is made.
MIN_EARLIER_PLANS = 5
#: Half as big again as the usual.
_BIGGER_BY = 1.5


@dataclass(frozen=True)
class PlanSize:
    tasks: int
    waves: int
    #: The sum of the tasks' own estimates; ``None`` unless every task has one.
    minutes: float | None

    def receipt(self) -> dict[str, Any]:
        return {"tasks": self.tasks, "waves": self.waves, "minutes": self.minutes}


def plan_size_of(text: str) -> PlanSize | None:
    """The size of one plan file, or ``None`` when it is not a plan."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, Mapping) or not isinstance(data.get("tasks"), list):
        return None
    tasks = data["tasks"]
    minutes: float | None = 0.0
    for task in tasks:
        value = task.get("estimated_minutes") if isinstance(task, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            minutes = None
            break
        minutes += float(value)
    orchestration = data.get("orchestration")
    groups = orchestration.get("parallel_groups") if isinstance(orchestration, Mapping) else None
    waves = len(groups) if isinstance(groups, list) else 0
    return PlanSize(tasks=len(tasks), waves=waves, minutes=minutes if tasks else None)


def _whole(value: float) -> int:
    return int(value + 0.5)


def _about(minutes: float) -> str:
    if minutes < 60:
        return f"about {_whole(minutes)} minutes"
    hours = _whole(minutes / 60)
    return f"about {hours} hour" + ("" if hours == 1 else "s")


def _comparable(earlier: list[PlanSize]) -> list[PlanSize]:
    """The earlier plans worth comparing with: two or more tasks, and a
    positive estimate in all (a plan estimated at nothing would make every
    plan look big)."""
    return [p for p in earlier if p.tasks >= 2 and p.minutes is not None and p.minutes > 0]


def plan_size_note(this: PlanSize, earlier: list[PlanSize]) -> str | None:
    """The card's one line, or ``None``: only when this plan is half as big
    again as the median of at least five comparable earlier plans."""
    usable = _comparable(earlier)
    if len(usable) < MIN_EARLIER_PLANS:
        return None
    usual_tasks = statistics.median(p.tasks for p in usable)
    usual_minutes = statistics.median(p.minutes for p in usable if p.minutes is not None)
    bigger = this.tasks >= _BIGGER_BY * usual_tasks or (
        this.minutes is not None and this.minutes >= _BIGGER_BY * usual_minutes
    )
    if not bigger or usual_minutes <= 0:
        return None
    if this.minutes is not None:
        now = f"{this.tasks} tasks, {_about(this.minutes)} by the plan's own estimate"
    else:
        now = f"{this.tasks} tasks"
    usual = f"{_whole(usual_tasks)} tasks and {_about(usual_minutes)}"
    return (
        f"This plan is bigger than this project's usual: {now} (its plans usually "
        f"have {usual}). Starting it is still your choice."
    )


def read_plan_size_note(
    reader: Any, files: Mapping[str, str]
) -> tuple[str | None, dict[str, Any]]:
    """The line for this plan, and what was compared, for the plan's record.

    ``files`` is the plan tree just committed; its own plan file is the one
    measured and is never counted among the earlier ones. Never raises.
    """
    receipt: dict[str, Any] = {"compared_with": 0}
    try:
        own = sorted(path for path in files if fnmatch.fnmatchcase(str(path), _PLAN_PATTERN))
        if not own:
            receipt["not_compared"] = "the plan has no plan file to measure"
            return None, receipt
        this = plan_size_of(files[own[0]])
        if this is None:
            receipt["not_compared"] = "the plan file could not be read"
            return None, receipt
        receipt["this_plan"] = this.receipt()
        begin = getattr(reader, "begin", None)
        if callable(begin):
            begin()
        earlier_paths = sorted(
            path
            for path in reader.list_files()
            if fnmatch.fnmatchcase(str(path), _PLAN_PATTERN) and path not in own
        )[:_MAX_EARLIER_PLANS_READ]
        earlier: list[PlanSize] = []
        for path in earlier_paths:
            text = reader.read_text(path)
            size = plan_size_of(text) if isinstance(text, str) else None
            if size is not None:
                earlier.append(size)
        usable = _comparable(earlier)
        receipt["compared_with"] = len(usable)
        if len(usable) < MIN_EARLIER_PLANS:
            receipt["not_compared"] = (
                f"fewer than {MIN_EARLIER_PLANS} earlier plans with estimates to compare with"
            )
            return None, receipt
        receipt["usual"] = {
            "tasks": statistics.median(p.tasks for p in usable),
            "minutes": statistics.median(p.minutes for p in usable if p.minutes is not None),
        }
        note = plan_size_note(this, earlier)
        receipt["warned"] = note is not None
        return note, receipt
    except Exception as exc:  # noqa: BLE001 — a size line must never stop a plan
        receipt["not_compared"] = f"the earlier plans could not be read ({type(exc).__name__})"
        return None, receipt
