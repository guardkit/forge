"""YAML loader for ``forge.yaml``.

This module is the **integration contract producer** ``CONFIG_LOADER``
(see IMPLEMENTATION-GUIDE.md §4) consumed by the queue lifecycle
subsystem (TASK-PSM-008/009/010/011).

Design choices (TASK-PSM-003):

- ``yaml.safe_load`` is used so untrusted YAML cannot construct arbitrary
  Python objects. An empty file is normalised to an empty dict so the
  Pydantic root model can apply its own defaults.
- ``ForgeConfig.model_validate`` is invoked directly. Any
  ``pydantic.ValidationError`` raised during validation propagates to the
  caller **unchanged** — wrapping would hide structured error data the CLI
  needs to format actionable messages (AC-004 of TASK-PSM-003).

ADDRESSES ARE NAMES (24 September 2026, the containerisation pass).
--------------------------------------------------------------------

Every address this settings file used to carry was a machine's own: the
loopback address of the machine the coordinator shared a network with, or
that machine's address on the local network. Move the coordinator into a
container on a declared network and every one of those is wrong, and wrong
in the worst way — it still parses, it still starts, and the thing at the
other end is simply never reached.

So an address field in this file may now be written as a **name**:

.. code-block:: yaml

    planning:
      sandboxes:
        the-org/the-project:
          name: the-sandbox
          sidecar_url: ${FORGE_SANDBOX_SIDECAR_URL}
          runner_url: ${FORGE_SANDBOX_RUNNER_URL}

and the value comes from the environment the service was started with —
which, in the container world, is one env file per machine and the only
place a machine's own address is written down.

THREE THINGS THIS DELIBERATELY DOES NOT DO:

* it never supplies a default address. A name nothing set is a **refusal**,
  by that name, naming the field it was written in. Filling in a default
  would put back exactly the failure this replaces: something that starts
  and reaches nothing;
* it only fills in address fields — a key called ``url``, ``host`` or
  ``address``, or one ending ``_url``, ``_host``, ``_address`` (and their
  plurals). Everything else in the file is left exactly as written, so a
  value that happens to contain ``${...}`` somewhere else is not touched;
* it only understands the braced form ``${NAME}``. A bare ``$NAME`` is a
  perfectly good beginning to a real address and is left alone.

Nothing here names a language, a test runner, a package manager, a product
or any project's layout. It fills in addresses from names.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from forge.config.models import ForgeConfig

#: The keys whose values are addresses. Exact names first, then endings.
ADDRESS_KEY_NAMES: tuple[str, ...] = (
    "url",
    "urls",
    "host",
    "hosts",
    "address",
    "addresses",
)
ADDRESS_KEY_ENDINGS: tuple[str, ...] = (
    "_url",
    "_urls",
    "_host",
    "_hosts",
    "_address",
    "_addresses",
)

#: ``${NAME}`` and nothing else. A name is the ordinary shape of an
#: environment setting's name: a letter or underscore, then letters, digits
#: and underscores.
_A_NAME_IN_BRACES = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class AnAddressNameIsNotSet(RuntimeError):
    """An address field named a setting, and nothing set it.

    Said in one sentence naming both the setting and the field, because the
    person reading it is setting this machine up and needs to know which line
    to fix and which name to give a value.
    """


def is_an_address_key(key: Any) -> bool:
    """Is this the key of a field that holds an address?

    Named rather than inlined so a reader can see the whole rule in one
    place, and so a test can ask it directly.
    """
    name = str(key).strip().lower()
    if not name:
        return False
    return name in ADDRESS_KEY_NAMES or name.endswith(ADDRESS_KEY_ENDINGS)


def _fill_in_one(
    written: str, *, field: str, where: str, environ: Mapping[str, str]
) -> str:
    """Replace every ``${NAME}`` in one address, or refuse by name."""

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        value = str(environ.get(name, "")).strip()
        if not value:
            raise AnAddressNameIsNotSet(
                f"the settings file {where} writes the address of '{field}' as "
                f"the setting {name}, and nothing set {name}. Give {name} a "
                f"value in this machine's env file, or write the address "
                f"itself on that line. This loader never picks an address "
                f"for you."
            )
        return value

    return _A_NAME_IN_BRACES.sub(replace, written)


def _fill_in_addresses_below(
    value: Any, *, field: str, where: str, environ: Mapping[str, str]
) -> Any:
    """Everything under an address key: a string, or a list of strings."""
    if isinstance(value, str):
        return _fill_in_one(value, field=field, where=where, environ=environ)
    if isinstance(value, list):
        return [
            _fill_in_addresses_below(
                item, field=f"{field}[{index}]", where=where, environ=environ
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        # A mapping under an address key — a field holding one address per
        # name. Each value is an address; the keys are names, not fields.
        return {
            key: _fill_in_addresses_below(
                item, field=f"{field}.{key}", where=where, environ=environ
            )
            for key, item in value.items()
        }
    return value


def fill_in_address_names(
    raw: Any,
    *,
    where: str,
    environ: Mapping[str, str] | None = None,
    field: str = "",
) -> Any:
    """Return ``raw`` with every ``${NAME}`` in an address field filled in.

    Walks the whole document. A mapping's value is filled in when its key is
    an address key; a list under an address key has each of its strings
    filled in, so a field holding several addresses works the same way.

    Args:
        raw: the document as ``yaml.safe_load`` returned it.
        where: the settings file's own path, for the refusal sentence.
        environ: what to read the names out of; the process's own
            environment when not given. Tests pass their own mapping rather
            than mutating process-wide state.
        field: the dotted path of the field being filled in, built up as the
            walk descends, for the refusal sentence.

    Raises:
        AnAddressNameIsNotSet: when an address field names a setting that has
            no value.
    """
    env = os.environ if environ is None else environ
    if isinstance(raw, dict):
        filled: dict[Any, Any] = {}
        for key, value in raw.items():
            path = f"{field}.{key}" if field else str(key)
            if is_an_address_key(key):
                filled[key] = _fill_in_addresses_below(
                    value, field=path, where=where, environ=env
                )
            else:
                filled[key] = fill_in_address_names(
                    value, where=where, environ=env, field=path
                )
        return filled
    if isinstance(raw, list):
        return [
            fill_in_address_names(
                item, where=where, environ=env, field=f"{field}[{index}]"
            )
            for index, item in enumerate(raw)
        ]
    return raw


def load_config(
    path: Path, *, environ: Mapping[str, str] | None = None
) -> ForgeConfig:
    """Read ``path`` as YAML and validate it against :class:`ForgeConfig`.

    Args:
        path: Filesystem location of the ``forge.yaml`` document.
        environ: Where an address written as ``${NAME}`` is looked up; the
            process's own environment when not given.

    Returns:
        A validated :class:`ForgeConfig` instance.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        AnAddressNameIsNotSet: If an address field names a setting that has
            no value. Refused before validation, because a half-filled
            address would otherwise reach the model as a real one.
        pydantic.ValidationError: If the YAML payload fails Pydantic
            validation. The exception is **not** wrapped — callers (the CLI
            in particular) catch ``ValidationError`` directly so they can
            format ``error.errors()`` for the operator.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = fill_in_address_names(raw, where=str(path), environ=environ)
    return ForgeConfig.model_validate(raw)


__all__ = [
    "ADDRESS_KEY_ENDINGS",
    "ADDRESS_KEY_NAMES",
    "AnAddressNameIsNotSet",
    "fill_in_address_names",
    "is_an_address_key",
    "load_config",
]
