"""A stand-in for GuardKit's ``toolchain_declaration.full_check_parts``.

GuardKit's own reader is not importable in Forge's test environment (its own
dependencies are not installed here), so these tests use this stand-in. It
follows GuardKit's rule (guardkit 290ed891): the root ``test`` when declared,
then every named component in name order, then every folder a ``parts:``
pattern matches in the tree (one a component already names is not repeated).
It reads any object with those attributes, so a test's own small declaration
works too.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from forge.full_check import CheckPart, _as_check_part


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


def parts_reader(declaration: Any, tree: Path | str) -> list[CheckPart]:
    """The gates reader's ``parts_reader`` seam, through the stand-in."""
    return [_as_check_part(p, declaration) for p in full_check_parts(declaration, tree)]
