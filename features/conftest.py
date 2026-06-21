"""pytest-bdd collection bridge for ``features/`` (canonical GuardKit template).

Two responsibilities — both are infrastructure required to make GuardKit's
``bdd_runner.run_bdd_for_task`` succeed when it points ``pytest`` at a literal
``.feature`` path:

1. **Collection bridge** — pytest-bdd v8 does NOT register a
   ``pytest_collect_file`` hook for ``.feature`` files. The bdd_runner
   subprocess invocation is::

       pytest --gherkin-terminal-reporter --junitxml=... \\
              -m <sanitised_tag> features/<slug>/<slug>.feature

   Without a bridge, pytest exits 4 ("ERROR: not found") because the
   ``.feature`` extension has no registered collector and pytest's argv
   resolver bails before pytest-bdd's machinery runs. The
   :func:`pytest_collect_file` hook below redirects ``.feature`` argv to
   the appropriate sibling ``test_<slug>.py`` glue module, which calls
   :func:`pytest_bdd.scenarios` (or per-scenario ``@scenario``
   decorators) to actually bind the feature file.

2. **Tag → marker sanitisation** — pytest-bdd's default
   :func:`pytest_bdd_apply_tag` registers a marker with the literal tag
   string (``@task:TASK-FG-002`` → marker ``task:TASK-FG-002``).
   GuardKit's ``bdd_runner._build_pytest_argv`` sanitises the same tag
   to ``task_TASK_FG_002`` for the ``-m`` filter (``:`` and ``-`` are
   not valid identifier chars in pytest marker expressions). The
   override below applies the same sanitisation so the ``-m`` filter
   actually matches the registered markers.

Per-task glue lookup (TASK-AB-004)
----------------------------------
When two tasks (e.g. TASK-FG-002 and TASK-FG-003) run in parallel against the
same worktree, both Players writing to a single shared
``test_<slug>.py`` race each other — each rewrites the file to bind only its
own scenarios, so the other task's BDD oracle collects zero scenarios under
its ``-m`` filter even though the ``.feature`` file carries both task tags.

To remove the race, the bdd_runner sets ``GUARDKIT_BDD_TASK_ID=<task_id>``
in the pytest subprocess environment. This conftest reads that variable and
prefers per-task glue ``test_<slug>__<sanitised_task_id>.py`` over the
legacy shared ``test_<slug>.py``. If the per-task module is missing the
collector falls back to the shared module, preserving single-task behaviour
for projects that have not yet adopted per-task glue files.

Sanitisation matches both ``bdd_runner._build_pytest_argv`` and
:func:`_sanitise_tag` below: strip leading ``@``, replace ``:`` and ``-``
with ``_``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, List, TypeVar, cast

import pytest

T = TypeVar("T", bound=Callable[..., object])


_BDD_TASK_ID_ENV: str = "GUARDKIT_BDD_TASK_ID"


def _sanitise_tag(tag: str) -> str:
    """Mirror ``bdd_runner._build_pytest_argv``'s tag normalisation.

    Strips the leading ``@`` (Gherkin tags carry it; pytest markers do
    not), then replaces ``:`` and ``-`` with ``_`` so the result is a
    valid pytest marker identifier.
    """
    return tag.lstrip("@").replace(":", "_").replace("-", "_")


def _glue_candidates(feature_path: Path) -> List[Path]:
    """Return glue modules to try, in priority order.

    Priority:

    1. ``test_<slug>__<sanitised_task_id>.py`` when ``GUARDKIT_BDD_TASK_ID``
       is set (per-task glue, TASK-AB-004).
    2. ``test_<slug>.py`` (legacy shared glue, single-task projects).

    The per-task entry is omitted when the env var is unset so projects
    that do not opt into the per-task convention behave exactly as before.
    """
    slug = feature_path.stem.replace("-", "_")
    candidates: List[Path] = []
    task_id = os.environ.get(_BDD_TASK_ID_ENV)
    if task_id:
        sanitised = _sanitise_tag(task_id)
        candidates.append(
            feature_path.with_name(f"test_{slug}__{sanitised}.py")
        )
    candidates.append(feature_path.with_name(f"test_{slug}.py"))
    return candidates


def _select_glue(feature_path: Path) -> Path | None:
    """Return the first glue candidate that exists on disk, or ``None``."""
    for candidate in _glue_candidates(feature_path):
        if candidate.is_file():
            return candidate
    return None


def pytest_bdd_apply_tag(tag: str, function: T) -> T:
    """Register Gherkin tags as sanitised pytest markers.

    Returning a non-``None`` value short-circuits pytest-bdd's default
    implementation (which uses the literal tag string as the marker
    name). See module docstring for why sanitisation is required.
    """
    mark = getattr(pytest.mark, _sanitise_tag(tag))
    return cast(T, mark(function))


class _FeatureFile(pytest.File):
    """Collector whose ``path`` matches the ``.feature`` argv.

    pytest's args resolver matches positional argv against collector
    ``path`` attributes. Subclassing :class:`pytest.File` keeps the
    ``.feature`` path on the collector while delegating actual item
    collection to the chosen sibling glue module via
    :class:`pytest.Module`'s normal import machinery.
    """

    def collect(self) -> Iterator[Any]:
        glue = _select_glue(self.path)
        if glue is None:
            return
        module_collector = pytest.Module.from_parent(self.parent, path=glue)
        yield from module_collector.collect()


def pytest_collect_file(
    parent: pytest.Collector, file_path: Path
) -> pytest.Collector | None:
    """Bridge ``.feature`` argv to the chosen ``test_<slug>...py`` glue.

    Returning ``None`` for paths without a sibling glue lets pytest fall
    through to its default handling (and surface the original "not
    found" error), so missing-glue cases are not silently swallowed.
    """
    if file_path.suffix != ".feature":
        return None
    if _select_glue(file_path) is None:
        return None
    return _FeatureFile.from_parent(parent, path=file_path)
