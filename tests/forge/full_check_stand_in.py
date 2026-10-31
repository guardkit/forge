"""Stand-ins for GuardKit's ``toolchain_declaration`` full-check functions.

GuardKit's own reader is not importable in Forge's test environment (its own
dependencies are not installed here), so these tests use this stand-in. It
follows GuardKit's rule (guardkit 290ed891): the root ``test`` when declared,
then every named component in name order, then every folder a ``parts:``
pattern matches in the tree (one a component already names is not repeated).
It reads any object with those attributes, so a test's own small declaration
works too.

``declared_no_tests_ran`` and ``ran_marker_problem`` follow GuardKit's own
(guardkit 02316ab2, 31 October 2026), line for line: ``absent_substrings``
compared without regard to case on any exit code, then ``ran_marker_regex``
on exit 0 unless ``requires_ran_marker`` is false; a required marker with no
regex is a problem in the declaration. GuardKit's own module is not
importable in Forge's test environment, so on 31 October 2026 the stand-ins
were checked once against GuardKit's real functions, on every case in
``tests/forge/test_full_check_verdict_rules.py``, and gave the same answers.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from forge.full_check import CheckPart, VerdictRules, _as_check_part


def full_check_parts(declaration: Any, tree: Path | str) -> list[Any]:
    if declaration is None:
        return []
    parts: list[Any] = []
    if getattr(declaration, "test", None):
        parts.append(SimpleNamespace(name="root", cwd=".", test=declaration.test, toolchain=None))
    named = []
    for name, comp in sorted((getattr(declaration, "components", None) or {}).items()):
        named.append(SimpleNamespace(name=name, cwd=comp.cwd, test=comp.test, toolchain=comp))
    folders = {p.cwd for p in named}
    parts += named
    for entry in getattr(declaration, "parts", None) or ():
        base = Path(tree) / entry.under
        try:
            children = sorted(c for c in base.iterdir() if c.is_dir())
        except OSError:
            continue
        for child in children:
            if not (child / entry.marker).is_file():
                continue
            cwd = child.name if entry.under == "." else f"{entry.under}/{child.name}"
            if cwd in folders:
                continue
            parts.append(SimpleNamespace(name=cwd, cwd=cwd, test=entry.test, toolchain=entry))
    return parts


def ran_marker_problem(commands: Any) -> str | None:
    if commands is None:
        return None
    if commands.requires_ran_marker and commands.ran_marker_regex is None:
        return (
            "declares `requires_ran_marker: true` but no `ran_marker_regex`, "
            "so nothing in its output could show that a test ran"
        )
    return None


def declared_no_tests_ran(
    commands: Any, returncode: int | None, output: str | None
) -> str | None:
    if commands is None:
        return None
    text = output or ""
    lowered = text.lower()
    for absent in commands.absent_substrings or ():
        if absent and absent.lower() in lowered:
            return f'the output contains "{absent}"'
    regex = commands.ran_marker_regex
    marker_applies = (
        commands.requires_ran_marker
        if commands.requires_ran_marker is not None
        else regex is not None
    )
    if regex is not None and marker_applies and returncode == 0:
        if re.search(regex, text) is None:
            return f"no line matched {regex}"
    return None


RULES = VerdictRules(no_tests_ran=declared_no_tests_ran, marker_problem=ran_marker_problem)


def stand_in_for_guardkit(module: ModuleType) -> ModuleType:
    """Put the stand-ins on ``module`` under GuardKit's own names."""
    module.full_check_parts = full_check_parts  # type: ignore[attr-defined]
    module.declared_no_tests_ran = declared_no_tests_ran  # type: ignore[attr-defined]
    module.ran_marker_problem = ran_marker_problem  # type: ignore[attr-defined]
    return module


def parts_reader(declaration: Any, tree: Path | str) -> list[CheckPart]:
    """The gates reader's ``parts_reader`` seam, through the stand-in."""
    return [
        _as_check_part(p, declaration, RULES)
        for p in full_check_parts(declaration, tree)
    ]
