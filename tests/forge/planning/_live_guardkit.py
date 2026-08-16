"""Shared finder for a LIVE guardkit checkout carrying THE STAMP NORMALIZER.

Used by the seam test (``test_target_terminal_tools.py``) and the driver test
(``test_driver_target_terminal.py``) so the real ``guardkit qa
normalize-stamps`` CLI can be driven end to end when it is reachable, and both
tests skip in the same way when it is not:

* ``FORGE_GUARDKIT_NORMALIZER_CHECKOUT`` — a guardkit checkout / worktree path
  (else the sibling ``guardkit`` checkout, once the normalizer lane merges);
* ``FORGE_GUARDKIT_NORMALIZER_PYTHON`` — an interpreter that imports guardkit's
  CLI deps (else the checkout's ``.venv``, the sibling guardkit ``.venv``, ours).

The ``run_fn`` shells ``python -m guardkit.cli.main`` with ``PYTHONPATH`` = the
checkout (so the branch's code wins over any installed guardkit) with stdout
NOT a tty, and folds the streams through ``parse_guardkit_output`` — the SAME
parser the frozen seam uses, 4 KB stdout tail and all.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from forge.adapters.guardkit.parser import parse_guardkit_output


def find_sibling_checkout(name: str, start: Path) -> Path:
    """First ancestor of ``start`` that CONTAINS a checkout called ``name``
    (a non-existent path when none does, so skip guards still fire)."""
    here = start.resolve()
    for parent in here.parents:
        candidate = parent / name
        if candidate.is_dir():
            return candidate
    return here.parents[-1] / name


def live_guardkit_checkout(start: Path) -> Path | None:
    env = os.environ.get("FORGE_GUARDKIT_NORMALIZER_CHECKOUT")
    candidates = [Path(env)] if env else []
    candidates.append(find_sibling_checkout("guardkit", start))
    for c in candidates:
        if (c / "guardkit" / "orchestrator" / "stamp_normalizer.py").is_file():
            return c
    return None


def live_guardkit_python(checkout: Path, start: Path) -> str:
    env = os.environ.get("FORGE_GUARDKIT_NORMALIZER_PYTHON")
    if env:
        return env
    for venv in (checkout / ".venv", find_sibling_checkout("guardkit", start) / ".venv"):
        py = venv / "bin" / "python"
        if py.is_file():
            return str(py)
    return sys.executable


def live_cli_importable(checkout: Path, python: str) -> tuple[bool, str]:
    probe = subprocess.run(
        [python, "-c", "import guardkit.cli.main, guardkit.orchestrator.stamp_normalizer"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(checkout)},
    )
    return probe.returncode == 0, probe.stderr[-300:]


def live_run_fn(checkout: Path, python: str):
    """A ``run_fn`` shaped like ``forge.adapters.guardkit.run.run``."""

    async def _run(**kwargs: Any):
        cmd = [python, "-m", "guardkit.cli.main", kwargs["subcommand"], *kwargs["args"]]
        env = {**os.environ, "PYTHONPATH": str(checkout)}
        proc = subprocess.run(
            cmd,
            cwd=str(kwargs["repo_path"]),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        return parse_guardkit_output(
            subcommand=kwargs["subcommand"],
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            duration_secs=0.0,
        )

    return _run


def live_guardkit_or_skip(start: Path):
    """``(checkout, python)`` or ``pytest.skip`` with the reason."""
    import pytest

    checkout = live_guardkit_checkout(start)
    if checkout is None:
        pytest.skip("no guardkit checkout with the stamp normalizer reachable")
    python = live_guardkit_python(checkout, start)
    ok, err = live_cli_importable(checkout, python)
    if not ok:
        pytest.skip(f"no interpreter can import guardkit's CLI: {err!r}")
    return checkout, python
