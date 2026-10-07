"""The files the factory itself places in a project's tree.

Neutral ground (7 October 2026) for the two places that need the list: the
command-line package that ships the scripts (``forge.cli.deploy_templates``)
and the planner, whose code evidence and "all the X" candidates must never
offer the factory's own files as the project's code. A live run offered the
factory's sandbox runner, which the upgrade procedure copies into the
sandbox's clone and which mentions a "counter", as a member of "all the
count endpoints".

A file counts as the factory's only where the factory puts it AND when it
reads as the factory's script: a project's own ``tools/sandbox-runner.sh``,
or a ``deploy/sandbox-runner.sh`` the project wrote itself, stays the
project's file.
"""

from __future__ import annotations

__all__ = ["SHIPPED_SCRIPTS", "is_shipped_script", "shipped_script_path"]

#: Each script the factory ships for a project's sandbox, by file name, and
#: the line its header carries in every version shipped so far (its third
#: line; the first two are the interpreter line and a bare ``#``). The
#: scripts are placed at ``deploy/<name>`` (the one-page upgrade procedure,
#: and :mod:`forge.launch_environment`).
SHIPPED_SCRIPTS: dict[str, str] = {
    "sandbox-deploy.sh": "# The sandbox deploy wrapper — the one script the factory's deploy step runs for",
    "sandbox-runner.sh": "# The sandbox runner bootstrap — the one script that runs INSIDE a repository's",
}

#: The folder in a project the scripts are placed in.
SHIPPED_SCRIPTS_FOLDER = "deploy"

#: How many of a file's first lines are searched for the header.
_HEADER_LINES = 5


def shipped_script_path(path: str) -> str | None:
    """The shipped script's name when ``path`` is where the factory places
    one (``deploy/<name>``), else ``None``."""
    folder, _, name = str(path).rpartition("/")
    if folder == SHIPPED_SCRIPTS_FOLDER and name in SHIPPED_SCRIPTS:
        return name
    return None


def is_shipped_script(path: str, text: str | None) -> bool:
    """True when ``path`` is where the factory places a script AND ``text``
    (the file's content) carries that script's header in its first lines.
    Unread text (``None``) is never taken to be the factory's."""
    name = shipped_script_path(path)
    if name is None or text is None:
        return False
    header = SHIPPED_SCRIPTS[name]
    return any(line.rstrip() == header for line in text.split("\n")[:_HEADER_LINES])
