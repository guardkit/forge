"""Find a sibling guardkit checkout and an interpreter that imports it.

Used by the fix-admission test that drives GuardKit's real task loader:

* ``FORGE_GUARDKIT_NORMALIZER_PYTHON`` — an interpreter that imports guardkit's
  dependencies (else the checkout's ``.venv``, the sibling guardkit ``.venv``,
  ours).

(The stamp normalizer's live helpers went in the cull, 27 October 2026.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_sibling_checkout(name: str, start: Path) -> Path:
    """First ancestor of ``start`` that CONTAINS a checkout called ``name``
    (a non-existent path when none does, so skip guards still fire)."""
    here = start.resolve()
    for parent in here.parents:
        candidate = parent / name
        if candidate.is_dir():
            return candidate
    return here.parents[-1] / name


def live_guardkit_python(checkout: Path, start: Path) -> str:
    env = os.environ.get("FORGE_GUARDKIT_NORMALIZER_PYTHON")
    if env:
        return env
    for venv in (checkout / ".venv", find_sibling_checkout("guardkit", start) / ".venv"):
        py = venv / "bin" / "python"
        if py.is_file():
            return str(py)
    return sys.executable
