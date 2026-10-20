"""Does the project already declare a coverage command? (20 October 2026)

The plan writer refuses a plan that declares the project's coverage command
(``toolchain.coverage`` in ``.guardkit/config.yaml``) after a task that needs
measured coverage, because GuardKit stops such a task before any work. That
rule only matters when the project has no coverage command yet. The plan
writer cannot read the project's files, so Forge tells it, on the
target-repository descriptor's ``declares_coverage_command`` field.

``true`` means every task can be measured: GuardKit decides task by task (a
task with no component needs the root ``coverage`` command, a component's
task needs that component's own, with no fallback between them;
``toolchain_declaration.declared_coverage_command``). So the answer is
``true`` only when the root AND every listed component declare a non-empty
coverage command. A project where only some places do is ``false``, and the
plan writer's rule still runs for it.

The declaration is read with GuardKit's own parser
(``toolchain_declaration.parse_toolchain_block``), so the two cannot
disagree: a block GuardKit would ignore (a misspelt key, an empty command)
declares nothing here either. One difference, on the safe side: a command
of spaces, which GuardKit's parser accepts, measures nothing and is not
counted. GuardKit is installed in the factory's Forge
image; where it cannot be imported, the same all-places rule is read from
the YAML directly, and a block with any empty command declares nothing. That
fallback cannot spot a misspelt key, which only GuardKit's parser knows.

Anything that cannot be read or parsed is ``false``: the plan writer's rule
then still runs, which is the safe side (at worst one more rewrite, never a
plan that cannot be built). Nothing here knows any language, framework or
tool.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: The project's declaration file, as GuardKit names it.
CONFIG_PATH: str = ".guardkit/config.yaml"

#: The descriptor field the plan writer reads, as its descriptor schema
#: names it.
DESCRIPTOR_FIELD: str = "declares_coverage_command"

#: The key the toolchain block sits under in the configuration.
_TOOLCHAIN_KEY: str = "toolchain"


def _guardkit_reading(raw: Any) -> bool | None:
    """GuardKit's own answer for the raw ``toolchain`` block, or ``None``
    when GuardKit cannot be imported here."""
    try:
        from guardkit.orchestrator.toolchain_declaration import (  # type: ignore[import-not-found]
            declared_coverage_command,
            parse_toolchain_block,
        )
    except Exception as exc:  # noqa: BLE001 - fall back to the same rule
        logger.info(
            "coverage declaration: GuardKit's parser is not importable here "
            "(%s: %s); the same rule is read from the YAML",
            type(exc).__name__,
            exc,
        )
        return None
    try:
        declaration = parse_toolchain_block(raw)
    except ValueError:
        return False  # GuardKit ignores a malformed block: nothing declared
    def _declared(component: str | None = None) -> bool:
        # GuardKit takes a command of spaces as declared; it cannot measure
        # anything, so here it is not (the safe side: the rule stays on).
        command = declared_coverage_command(declaration, component)
        return isinstance(command, str) and bool(command.strip())

    if declaration.is_empty or not _declared():
        return False
    return all(_declared(name) for name in declaration.component_names)


def _has_command(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    command = block.get("coverage")
    return isinstance(command, str) and bool(command.strip())


def _has_an_empty_command(block: dict[str, Any]) -> bool:
    """GuardKit refuses a block with an empty command string."""
    return any(
        isinstance(value, str) and not value.strip()
        for key, value in block.items()
        if key != "components"
    )


def _fallback_reading(raw: Any) -> bool:
    """The all-places rule, read from the YAML when GuardKit is absent."""
    if not isinstance(raw, dict) or _has_an_empty_command(raw):
        return False
    if not _has_command(raw):
        return False
    components = raw.get("components")
    if components is None:
        return True
    if not isinstance(components, dict):
        return False
    for block in components.values():
        if not isinstance(block, dict) or _has_an_empty_command(block):
            return False
        if not _has_command(block):
            return False
    return True


def declares_coverage_command(config_text: str | None) -> bool:
    """``True`` only when the root of ``toolchain:`` and every listed
    component declare a non-empty coverage command, as GuardKit reads the
    block. Never raises."""
    if not isinstance(config_text, str) or not config_text.strip():
        return False
    try:
        data = yaml.safe_load(config_text)
    except Exception:  # noqa: BLE001 - unreadable is "not declared"
        return False
    if not isinstance(data, dict):
        return False
    raw = data.get(_TOOLCHAIN_KEY)
    if not isinstance(raw, dict):
        return False
    try:
        answer = _guardkit_reading(raw)
    except Exception:  # noqa: BLE001 - never fail a plan over this
        return False
    return _fallback_reading(raw) if answer is None else answer
