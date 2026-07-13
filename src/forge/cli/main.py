"""Click entry point for the ``forge`` CLI (TASK-PSM-008 / TASK-PSM-011 / TASK-PSM-012).

This module exposes the top-level Click group :func:`main` and registers
every subcommand currently shipped:

* ``forge queue`` (TASK-PSM-008 — write-then-publish enqueue command)
* ``forge status`` (TASK-PSM-009 — read-path bypass to SQLite)
* ``forge history`` (TASK-PSM-010 — read-path bypass to SQLite)
* ``forge cancel`` (TASK-PSM-011 — thin wrapper over CliSteeringHandler)
* ``forge skip`` (TASK-PSM-011 — thin wrapper over CliSteeringHandler)

The ``[project.scripts]`` entry in ``pyproject.toml`` (TASK-PSM-012) wires
``forge.cli.main:main`` to the ``forge`` console script so
``pip install -e .`` puts the binary on ``$PATH``.

The CLI runtime helpers (:class:`CliRuntime` and :func:`build_cli_runtime`)
live in :mod:`forge.cli.runtime` so subcommand modules can import them
without a circular import.

Per AC-002 of TASK-PSM-008, ``main`` loads ``forge.yaml`` *once* and
passes the parsed :class:`ForgeConfig` to subcommands via
:attr:`click.Context.obj`. ``forge queue`` (the principal consumer)
reads its ``repo_allowlist`` and queue defaults from that object;
``forge history`` keys off the same value to pull
``queue.default_history_limit``. ``forge cancel`` reads ``ctx.obj``
*best-effort* (O-02) for the gate's pinned ``approval.expected_approver``
default, but never requires it — running ``forge cancel`` without
``--config`` and without a ``./forge.yaml`` continues to work (the pinned
approver resolves to ``None`` and the responder falls back to the env / OS
user chain); ``forge skip`` needs no config.
"""

from __future__ import annotations

from pathlib import Path

import click

from forge.cli import cancel as _cancel
from forge.cli import history as _history
from forge.cli import queue as _queue
from forge.cli import review_gate as _review_gate
from forge.cli import runbook as _runbook
from forge.cli import serve as _serve
from forge.cli import skip as _skip
from forge.cli import status as _status
from forge.cli.runtime import CliRuntime, build_cli_runtime
from forge.config.loader import load_config

#: Default location of ``forge.yaml`` when ``--config`` is omitted.
#: Resolved against the current working directory so an operator can
#: ``cd`` into a project that ships its own ``forge.yaml`` and run
#: subcommands without an explicit ``--config`` flag.
DEFAULT_CONFIG_FILENAME = Path("forge.yaml")


def _resolve_context_object(config_path: Path | None) -> object:
    """Decide what to put in ``ctx.obj`` given an optional ``--config``.

    Behaviour:

    * If ``config_path`` is supplied (Click already verified the file
      exists via ``exists=True``), load it via :func:`load_config` and
      return the :class:`ForgeConfig`.
    * Otherwise look for ``./forge.yaml`` in the CWD; if present, load
      it. If absent, return ``None`` so subcommands that do not need a
      config (``cancel``/``skip``) keep working unchanged.

    The function is intentionally tolerant of a missing default
    ``forge.yaml`` because not every subcommand requires one; the
    ``queue`` subcommand enforces its own "config required" rule via
    :func:`forge.cli.queue._require_forge_config`.
    """
    if config_path is not None:
        return load_config(config_path)
    if DEFAULT_CONFIG_FILENAME.exists():
        return load_config(DEFAULT_CONFIG_FILENAME)
    return None


@click.group(name="forge")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to forge.yaml. Defaults to ./forge.yaml when present.",
)
@click.pass_context
def main(ctx: click.Context, config_path: Path | None) -> None:
    """Forge — pipeline orchestrator and checkpoint manager CLI."""
    ctx.obj = _resolve_context_object(config_path)


main.add_command(_queue.queue_cmd)
main.add_command(_status.status_cmd)
main.add_command(_history.history_cmd)
main.add_command(_cancel.cancel_cmd)
main.add_command(_skip.skip_cmd)
main.add_command(_serve.serve_cmd)
main.add_command(_runbook.runbook_cmd)
main.add_command(_review_gate.review_gate_cmd)


__all__ = ["CliRuntime", "build_cli_runtime", "main"]
