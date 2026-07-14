"""Make guardkit's ``discover_test_roots`` importable for planning tests.

The descriptor builder REUSES guardkit's own test-root discovery
(:func:`forge.planning.target_terminal_tools.discover_target_test_roots`),
which imports one of two module paths:

* ``guardkit._installer_core.commands.lib.smoke_gates_nudge`` — the wheel/pip
  form the forge production IMAGE installs (Dockerfile);
* ``installer.core.commands.lib.smoke_gates_nudge`` — the source-checkout form.

The local dev venv installs NEITHER. This conftest makes the source-checkout
candidate importable by adding the sibling ``../guardkit`` checkout to
``sys.path`` — the SAME dev-host form the production code documents as its
second candidate — so the descriptor tests exercise the real guardkit function
(exact ``tests/<name>`` roots) rather than the guardkit-less shallow fallback.

Scoped to ``tests/forge/planning`` and guarded: it only inserts the path when
neither candidate already resolves and the sibling checkout actually exists, so
an image/CI run where guardkit is pip-installed is untouched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CANDIDATES = (
    "guardkit._installer_core.commands.lib.smoke_gates_nudge",
    "installer.core.commands.lib.smoke_gates_nudge",
)


def _already_importable() -> bool:
    for name in _CANDIDATES:
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
    return False


def _ensure_guardkit_discovery_importable() -> None:
    if _already_importable():
        return
    # tests/forge/planning/conftest.py -> forge repo root is parents[3].
    forge_root = Path(__file__).resolve().parents[3]
    guardkit_checkout = forge_root.parent / "guardkit"
    marker = guardkit_checkout / "installer" / "core" / "commands" / "lib" / (
        "smoke_gates_nudge.py"
    )
    if marker.is_file():
        # APPEND (lowest priority), never insert(0): the guardkit checkout root
        # exposes several top-level packages (api/, lib/, tests/, main.py) that
        # would shadow forge's own if prepended. Appending lets forge + stdlib +
        # site-packages win every name they provide; guardkit only fills the
        # ``installer`` / ``guardkit`` names forge lacks.
        sys.path.append(str(guardkit_checkout))


_ensure_guardkit_discovery_importable()
