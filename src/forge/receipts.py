"""Where forge's durable per-build receipts live — the paths, and nothing else.

The one-minute version
======================

Every build forge runs leaves a small pile of evidence on disk: the subprocess
narrative, the failure manifest, the coach verdicts, and (from the in-flight
lane, 2026-08-07) a heartbeat file naming what the build is doing right now.
All of it lands under one root, ``<receipts>/<build_id>/``.

Until this module the rules for finding that root lived in
:mod:`forge.subagents.autobuild_runner` — the langgraph node module. That was
fine while only the WRITE side needed them. It stopped being fine the moment a
READ side appeared: ``forge status`` is a SQLite-only CLI that must keep
working when the bus is down and when langgraph is not even installed, and it
cannot import a module whose first act is ``import langgraph``.

So the path rules moved here, to a leaf that imports nothing but the standard
library, and ``autobuild_runner`` re-exports them. Every existing caller,
constant name and monkeypatch target is preserved: the runner still owns a
module-level :data:`BOUND_STATE_ROOT` (tests patch it), and its
``_receipts_root()`` passes that global down, so there is exactly ONE
implementation of the resolution order and still two places you can steer it
from.

References:
    - ``ai-transition/docs/build-monitor-design-pass-2026-07-31.md`` §h — the
      in-flight stage row, which is what forced the split.
    - FEAT-DRC / FEAT-DRF — the receipts export and the failure pack.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "BOUND_STATE_ROOT",
    "DEFAULT_RECEIPTS_DIR",
    "IN_FLIGHT_STATE_NAME",
    "RECEIPTS_DIRNAME",
    "RECEIPTS_DIR_ENV",
    "receipts_root",
]


#: Env var naming the durable receipts root (FEAT-DRC). Default rides
#: ``~/forge-state`` (bind-mounted at ``/var/forge`` in forge-prod, so the
#: daemon and accrual counters can read ``/var/forge/receipts/<build_id>/``).
RECEIPTS_DIR_ENV: str = "FORGE_RECEIPTS_DIR"
DEFAULT_RECEIPTS_DIR: str = "~/forge-state/receipts"

#: Where the host's ``~/forge-state`` is bind-mounted inside forge-prod
#: (``docker run … -v ~/forge-state:/var/forge``, ops/README.md §a). The
#: mount is NOT same-path, and that asymmetry is the whole defect this
#: constant closes: the build half of the estate runs host-side, where
#: ``~/forge-state/receipts`` IS the durable tree, while the daemon runs in
#: there, where the very same expression resolves to ``/home/forge/...`` —
#: a directory bound to nothing, wiped with the container. The first
#: production fix journey exported its receipts there and lost them
#: (2026-08-03). Path arithmetic plus one cheap ``is_dir()``; the mount is
#: either present or it is not.
BOUND_STATE_ROOT: Path = Path("/var/forge")

#: Sub-directory of the durable state root holding the receipts tree. One
#: spelling, so :data:`DEFAULT_RECEIPTS_DIR` and the bound root above
#: cannot drift into naming two different directories.
RECEIPTS_DIRNAME: str = "receipts"

#: THE IN-FLIGHT HEARTBEAT (design §h stage 1, 2026-08-07). Written into
#: ``<receipts>/<build_id>/`` by the build monitor's own poll loop and read by
#: ``forge status`` so a RUNNING build's STAGE cell can say what is actually
#: happening instead of ``—``. It lives beside the stdout tee on purpose: that
#: directory already exists from the build's first line of output, already has
#: the right owner-only permissions, and needs no new path arithmetic and no
#: new schema. Deleted when the build reaches a terminal, so it can never
#: outlive the build it describes.
IN_FLIGHT_STATE_NAME: str = "in-flight.json"


def receipts_root(*, bound_state_root: Path | None = None) -> Path:
    """Resolve the durable receipts root (FEAT-DRC / FEAT-DRF).

    Resolution order, first wins:

    1. ``$FORGE_RECEIPTS_DIR`` — the estate's configured knob. Unchanged,
       and still the only thing an operator has to set to move the tree.
    2. :data:`BOUND_STATE_ROOT` ``/receipts``, when that mount is present.
       This is the arm added 2026-08-03. Inside forge-prod the host's
       ``~/forge-state`` is bound at ``/var/forge``, NOT same-path, so
       ``~`` there names a container-local directory that dies with the
       container — which is exactly where the first production fix
       journey's receipts went. When the mount is there, it is the durable
       tree by definition, and a home-derived default would be a lie about
       a path that exists.
    3. ``~/forge-state/receipts`` — the host-side default, which is right
       for every process that runs outside the container (the build half)
       and for local development.

    ``bound_state_root`` exists so :mod:`forge.subagents.autobuild_runner` can
    pass its OWN module-level constant down: tests steer tier 2 by patching
    that global, and a re-export cannot carry a patch. Left unset, tier 2 reads
    this module's :data:`BOUND_STATE_ROOT` — the same path, same object.

    One ``is_dir()`` and otherwise path arithmetic; never raises (a
    home-less environment falls back to the literal default).
    """
    raw = os.environ.get(RECEIPTS_DIR_ENV)
    if raw and raw.strip():
        try:
            return Path(raw).expanduser()
        except (RuntimeError, OSError):  # pragma: no cover — no resolvable HOME
            return Path(raw)

    bound = BOUND_STATE_ROOT if bound_state_root is None else bound_state_root
    try:
        if bound.is_dir():
            return bound / RECEIPTS_DIRNAME
    except OSError:  # pragma: no cover — an unreadable mount point
        pass

    try:
        return Path(DEFAULT_RECEIPTS_DIR).expanduser()
    except (RuntimeError, OSError):  # pragma: no cover — no resolvable HOME
        return Path(DEFAULT_RECEIPTS_DIR)
