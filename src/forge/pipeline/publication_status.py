"""Say what the next merge word would decide about publication.

The GitHub publishing gate (2 October 2026). Run inside the coordinator's container::

    docker exec <coordinator> python -m forge.pipeline.publication_status

It loads the coordinator's own settings file, reads the machine's answers with
the SAME reader the merge press uses
(:func:`~forge.pipeline.publication_facts.read_publication_facts`) and asks the
SAME question the press asks
(:func:`~forge.pipeline.publication_switch.publication_is_switched_on`), so
what it prints is what the next merge word would read. It changes nothing,
sends nothing and writes nothing.

Exit status: 0 when publication is on, 1 when it is off (the sentence says
why), 2 when the settings could not be loaded.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from forge.pipeline.publication_facts import read_publication_facts, the_machine_now
from forge.pipeline.publication_switch import (
    publication_is_switched_on,
    why_publication_is_off,
)

__all__ = ["main", "where_publication_stands"]

#: Where the coordinator's settings file is in the estate's coordinator
#: container (deploy/compose/compose.yaml's ``--config``).
_THE_COORDINATORS_SETTINGS = Path("/etc/forge/forge.yaml")


def where_publication_stands(config: object) -> tuple[bool, str]:
    """The press's own verdict and sentence, from one read of the facts."""
    machine = the_machine_now(read_publication_facts)
    if publication_is_switched_on(config, machine):
        return True, "publication is ON: the setting says so and every condition holds"
    return False, f"publication is OFF: {why_publication_is_off(config, machine)}"


def _settings_path(given: str | None) -> Path:
    if given:
        return Path(given)
    named = os.environ.get("FORGE_CONFIG_PATH", "").strip()
    if named:
        return Path(named)
    return _THE_COORDINATORS_SETTINGS


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m forge.pipeline.publication_status",
        description=(
            "Say whether the next merge word would publish, using the same "
            "reader and the same question as the merge press."
        ),
    )
    parser.add_argument(
        "--config",
        help=(
            "the coordinator's settings file (default: FORGE_CONFIG_PATH, "
            f"else {_THE_COORDINATORS_SETTINGS})"
        ),
    )
    args = parser.parse_args(argv)
    from forge.config.loader import load_config

    path = _settings_path(args.config)
    try:
        config = load_config(path)
    except Exception as exc:  # noqa: BLE001 - said plainly, never a traceback
        print(
            f"publication status: the settings file at {path} could not be "
            f"loaded ({type(exc).__name__}), so nothing can be said",
            file=sys.stderr,
        )
        return 2
    on, sentence = where_publication_stands(config)
    print(sentence)
    return 0 if on else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
