"""Pytest plugin shim — route .feature collection to paired test_*.py.

The GuardKit BDD oracle (``guardkit/orchestrator/quality_gates/bdd_runner.py``)
invokes pytest with a ``features/.../*.feature`` file as a positional
argument. pytest-bdd 8.1 does **not** register a ``pytest_collect_file``
hook for ``.feature`` files (only the ``@scenario(...)`` decorators in
``test_*.py`` files bind scenarios to test items), so without this shim
pytest exits with code 4 ('ERROR: not found') and Coach surfaces the
runner error as a synthetic BDD failure.

Two hooks here:

1. ``pytest_load_initial_conftests`` — runs before pytest resolves
   positional args into collectable paths. We inspect ``args`` and
   substitute every ``features/.../*.feature`` path with the matching
   ``tests/bdd/test_<slug>.py`` module, where ``<slug>`` is the
   feature file stem with hyphens replaced by underscores.
2. ``pytest_bdd_apply_tag`` — pytest-bdd reflects raw Gherkin tags as
   ``getattr(pytest.mark, tag)`` (e.g. ``task:TASK-GCI-004``). pytest's
   ``-m`` expression evaluator can't match marker names containing
   ``:`` or ``-``, so the BDD runner sanitises the tag
   (``task_TASK_GCI_004``) for the ``-m`` filter. This hook applies
   the sanitised mark in addition to the raw one so direct
   expressions and BDD-runner-style sanitised filters both resolve.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _resolve_test_module(feature_path: Path) -> Path | None:
    """Return the ``tests/bdd/test_*.py`` paired with ``feature_path``.

    Convention: ``features/<slug>/<slug>.feature`` ↔
    ``tests/bdd/test_<slug-with-underscores>.py``.

    Walks up from the feature file until a sibling ``tests/bdd/`` folder
    is found. Returns ``None`` when no paired module exists — the BDD
    runner treats that as "no scenarios collected" rather than a runner
    error.
    """
    slug = feature_path.stem
    test_filename = f"test_{slug.replace('-', '_')}.py"
    for parent in feature_path.parents:
        candidate = parent / "tests" / "bdd" / test_filename
        if candidate.is_file():
            return candidate
    return None


def _rewrite_feature_args(args: list[str]) -> tuple[list[str], bool]:
    """Return ``(rewritten_args, changed)``.

    Each ``.feature`` path is replaced with the paired
    ``tests/bdd/test_<slug>.py`` module (deduped). When no paired module
    exists, the ``.feature`` arg is dropped so pytest reports zero
    collected items (the documented "no scenarios" signal in
    ``bdd_runner.run_bdd_for_task``) rather than a usage-error exit.
    """
    rewritten: list[str] = []
    seen_modules: set[str] = set()
    changed = False
    for arg in args:
        try:
            path = Path(arg)
        except (TypeError, ValueError):
            rewritten.append(arg)
            continue
        if path.suffix == ".feature":
            test_module_path = _resolve_test_module(path)
            if test_module_path is not None:
                key = str(test_module_path)
                if key not in seen_modules:
                    rewritten.append(key)
                    seen_modules.add(key)
                changed = True
                continue
            changed = True
            continue
        rewritten.append(arg)
    return rewritten, changed


def pytest_load_initial_conftests(early_config, parser, args):
    """Rewrite ``.feature`` positional args to paired ``test_*.py`` paths.

    Runs before pytest resolves args into collectable paths.
    """
    rewritten, changed = _rewrite_feature_args(args)
    if changed:
        args[:] = rewritten


def pytest_cmdline_main(config):
    """Backstop rewrite — also fires when initial conftests miss the hook.

    pytest's plugin discovery loads ``conftest.py`` at the rootdir during
    ``pytest_load_initial_conftests``, so a hook defined in this file
    can miss its own initial-conftest pass. ``pytest_cmdline_main``
    fires after conftest loading and lets us mutate ``config.args``
    before collection starts.
    """
    rewritten, changed = _rewrite_feature_args(list(config.args))
    if changed:
        config.args = rewritten
    return None


@pytest.hookimpl
def pytest_bdd_apply_tag(tag: str, function):
    """Apply a sanitised marker name in addition to the raw Gherkin tag.

    pytest-bdd applies ``@<tag>`` as ``getattr(pytest.mark, tag)``. With
    Gherkin tags like ``@task:TASK-GCI-004`` the resulting mark name
    contains ``:`` and ``-``, which pytest's ``-m`` expression
    evaluator rejects. The GuardKit BDD oracle sanitises the tag to
    ``task_TASK_GCI_004`` before passing it to ``-m``, so we apply the
    sanitised mark on top of the raw one. Returning ``True`` signals
    pytest-bdd that the tag has been handled here; pytest-bdd skips
    its default application path when this hook returns truthy.
    """
    sanitised = tag.replace(":", "_").replace("-", "_")
    function = getattr(pytest.mark, tag)(function)
    if sanitised != tag:
        function = getattr(pytest.mark, sanitised)(function)
    return True
