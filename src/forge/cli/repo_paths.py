"""``forge repo-paths`` — print the checkout paths the repository map names.

The register-repo spec (2026-09-05) rule 9: the forge-prod container's
repository binds are no longer hand-written in ``ops/forge-prod-recreate.sh``.
They are derived from ``planning.target_repo_paths`` in ``forge.yaml``, so
registering a repository mints both the claim (the map entry) and the thing
claimed (the container's bind) from one edit.

The estate spells the same repository two ways in the map — ``guardkit/<name>``
and ``appmilla_github/<name>`` — so the same checkout appears twice. This
command prints each distinct path once, sorted, one per line, and nothing else:
its whole job is to be read by a shell loop.

``--config`` is accepted here as well as on the group so the recreate script can
say ``uv run forge repo-paths --config "$FORGE_CONFIG"`` in one line. A config
that cannot be read — missing, not valid YAML, or not a shape ``forge.yaml``
accepts — comes back as one plain sentence and a non-zero exit, never a
traceback: the recreate script prints this command's stderr to a human who is
about to take a container down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

__all__ = ["repo_paths_cmd", "distinct_repo_paths"]


def distinct_repo_paths(config: Any) -> list[str]:
    """The sorted, distinct checkout paths of ``planning.target_repo_paths``.

    Blank values are dropped: a map entry with no path names no checkout, so it
    can mint no bind.
    """
    mapping = (
        getattr(getattr(config, "planning", None), "target_repo_paths", None) or {}
    )
    return sorted({str(value).strip() for value in mapping.values() if str(value).strip()})


@click.command(name="repo-paths")
@click.option(
    "--config",
    "config_path",
    # NOT ``exists=True``: a missing file is answered by the plain sentence
    # below, in the same voice as a malformed one, rather than by Click's
    # usage error.
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to forge.yaml. Defaults to whichever config the group loaded.",
)
@click.pass_obj
def repo_paths_cmd(config: Any, config_path: Path | None) -> None:
    """Print each checkout path in the repository map once, sorted."""
    if config_path is not None:
        from forge.config.loader import load_config

        try:
            config = load_config(config_path)
        except FileNotFoundError:
            raise click.ClickException(
                f"there is no file at {config_path} to read the repository map from"
            ) from None
        except Exception as exc:  # noqa: BLE001 — every failure reads the same
            raise click.ClickException(
                f"{config_path} could not be read as a forge.yaml — it is not "
                f"valid YAML, or it is missing something forge.yaml must have "
                f"({exc.__class__.__name__})"
            ) from None
    if config is None:
        raise click.ClickException(
            "no forge.yaml to read — pass --config /path/to/forge.yaml, or run "
            "from a directory that has one"
        )
    for path in distinct_repo_paths(config):
        click.echo(path)
