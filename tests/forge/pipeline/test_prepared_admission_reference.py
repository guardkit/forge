"""Prepared admission and a project's reference material (17 October 2026).

Prototype-to-factory design, sections 2.2 (Codex R4) and 2.4 (Codex R3):

* with ``reference.signed_off`` declared, admission checks the record at the
  queued commit — present, parsed, naming a repository and a full commit —
  as the planning door does; a malformed ``reference:`` block is refused;
  a project without the setting is never checked;
* the link convention: a spec summary, a plan guide or a task file links a
  reference file from the repository top (``/reference/prototype/...``), and
  admission checks it exists at the pinned commit. A relative
  ``reference/...`` link from a nested task file resolves under the task's
  own folder and is refused — which is why the convention uses the leading
  ``/``.

The project here is synthetic, as in ``test_prepared_admission``.
"""

from __future__ import annotations

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from tests.forge.pipeline.test_prepared_admission import (  # noqa: F401 - fixtures
    SPEC_DIR,
    SPEC_NAME,
    TASK_DIR,
    TASKS,
    Project,
    _admit,
    bundle,
    config_text,
    project,
    runner,
)
from tests.forge.recorded_fetch_address import local_remotes  # noqa: F401

BRANCH = "feature/prepared"
RECORD_PATH = "reference/signed-off.yaml"
SCREEN = "reference/prototype/src/screens/Gadgets.view"
GOOD_RECORD = (
    "repository: https://example.invalid/acme/widget-mockup.git\n"
    f"commit: {'7' * 40}\n"
    "signed_off: 2026-10-01\n"
)


def _with_reference(files: dict[str, str], block: str) -> dict[str, str]:
    files = dict(files)
    files[".guardkit/config.yaml"] = config_text() + block
    files[SCREEN] = "the gadgets screen\n"
    return files


DECLARES_RECORD = (
    "reference:\n"
    '  paths: ["reference/"]\n'
    f'  signed_off: "{RECORD_PATH}"\n'
)


# ---------------------------------------------------------------------------
# The signed-off record at the queued commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_good_record_is_admitted(project: Project, runner: WorktreeGitRunner) -> None:
    files = _with_reference(bundle(), DECLARES_RECORD)
    files[RECORD_PATH] = GOOD_RECORD
    project.commit_on(BRANCH, files)

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal
    # Kept with the build, so the merge card knows a record was declared.
    assert answer.admitted is not None
    assert answer.admitted.reference_signed_off == RECORD_PATH



@pytest.mark.asyncio
async def test_without_a_record_setting_nothing_is_checked(
    project: Project, runner: WorktreeGitRunner
) -> None:
    project.commit_on(
        BRANCH, _with_reference(bundle(), 'reference:\n  paths: ["reference/"]\n')
    )

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal
    assert answer.admitted is not None and answer.admitted.reference_signed_off is None


# ---------------------------------------------------------------------------
# The link convention (design 2.4): links from the repository top
# ---------------------------------------------------------------------------


def _link_from(files: dict[str, str], source: str, target: str) -> dict[str, str]:
    files = dict(files)
    files[source] += f"\nIt implements the [gadgets screen]({target}).\n"
    return files


SOURCES = {
    "spec summary": f"{SPEC_DIR}/{SPEC_NAME}_summary.md",
    "plan guide": f"{TASK_DIR}/IMPLEMENTATION-GUIDE.md",
    "task file": f"{TASK_DIR}/{TASKS[0]}-do-the-thing.md",
}


@pytest.mark.parametrize("where", sorted(SOURCES))
@pytest.mark.asyncio
async def test_a_link_from_the_repository_top_is_admitted(
    project: Project, runner: WorktreeGitRunner, where: str
) -> None:
    files = _with_reference(bundle(), 'reference:\n  paths: ["reference/"]\n')
    project.commit_on(BRANCH, _link_from(files, SOURCES[where], f"/{SCREEN}"))

    answer = await _admit(project, runner)

    assert answer.ok, answer.refusal



