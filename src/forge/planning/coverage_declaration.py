"""Does the project already declare a coverage command? (20 October 2026)

The plan writer refuses a plan that declares the project's coverage command
(``toolchain.coverage`` in ``.guardkit/config.yaml``) after a task that needs
measured coverage, because GuardKit stops such a task before any work. That
rule only matters when the project has no coverage command yet. The plan
writer cannot read the project's files, so Forge tells it, on the
target-repository descriptor's ``declares_coverage_command`` field: ``true``
when the project's configuration declares a coverage command, ``false``
otherwise.

"Declares" is GuardKit's own reading (``toolchain_declaration.
declared_coverage_command``): a non-empty ``coverage`` command under
``toolchain:``, or under any one of ``toolchain.components``. Anything that
cannot be read or parsed counts as not declared: the plan writer's rule then
still runs, which is the safe side (at worst one more rewrite, never a plan
that cannot be built).

Nothing here knows any language, framework or tool.
"""

from __future__ import annotations

from typing import Any

import yaml

#: The project's declaration file, as GuardKit names it.
CONFIG_PATH: str = ".guardkit/config.yaml"

#: The descriptor field the plan writer reads, as its descriptor schema
#: names it.
DESCRIPTOR_FIELD: str = "declares_coverage_command"


def _has_command(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    command = block.get("coverage")
    return isinstance(command, str) and bool(command.strip())


def declares_coverage_command(config_text: str | None) -> bool:
    """``True`` when the configuration text declares a coverage command at
    the root of ``toolchain:`` or for any of its components. Never raises."""
    if not isinstance(config_text, str) or not config_text.strip():
        return False
    try:
        data = yaml.safe_load(config_text)
    except Exception:  # noqa: BLE001 - unreadable is "not declared"
        return False
    if not isinstance(data, dict):
        return False
    toolchain = data.get("toolchain")
    if not isinstance(toolchain, dict):
        return False
    if _has_command(toolchain):
        return True
    components = toolchain.get("components")
    if isinstance(components, dict):
        return any(_has_command(block) for block in components.values())
    return False
