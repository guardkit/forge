"""Entrypoint for the DeepAgents orchestrator exemplar.

Reads ``orchestrator-config.yaml`` for model selection, loads domain context
from ``domains/{domain}/DOMAIN.md``, creates the orchestrator agent graph, and
exports a module-level ``agent`` variable required by ``langgraph.json``.

Usage via LangGraph::

    langgraph.json  →  ``{"graphs": {"orchestrator": "./agent.py:agent"}}``
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from agents import create_orchestrator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Project root directory (where this file lives).
_PROJECT_ROOT = Path(__file__).resolve().parent

#: Default domain name used when no override is provided.
DEFAULT_DOMAIN = "example-domain"

#: Fallback model identifiers used when the config file is missing or invalid.
_DEFAULT_CONFIG: dict[str, Any] = {
    "orchestrator": {
        "reasoning_model": "anthropic:claude-sonnet-4-6",
        "implementation_model": "anthropic:claude-haiku-4-5",
    },
}

#: Canonical env var names (LES1 §3 PMEV). An operator setting either of these
#: overrides the YAML config for the matching role. An empty value is treated
#: as "unset" — see :func:`_resolve_model`.
_ENV_REASONING_MODEL = "AGENT_MODELS__REASONING_MODEL"
_ENV_IMPLEMENTATION_MODEL = "AGENT_MODELS__IMPLEMENTATION_MODEL"

#: Fallback domain prompt returned when the DOMAIN.md file cannot be read.
_DEFAULT_DOMAIN_PROMPT = "No domain-specific guidelines loaded. Follow general software engineering best practices."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config(config_path: Path) -> dict[str, Any]:
    """Read and parse ``orchestrator-config.yaml``.

    Uses :func:`yaml.safe_load` for safe YAML parsing. If the file is missing
    or contains invalid YAML, an empty dict is returned; the env-var /
    hardcoded-default resolution in :func:`_resolve_model` then supplies the
    model identifiers. Returning ``{}`` (rather than ``_DEFAULT_CONFIG``) keeps
    "yaml provided" and "fell through to hardcoded default" distinguishable
    in the INFO log emitted by :func:`_resolve_model`.

    Args:
        config_path: Absolute or relative path to the YAML config file.

    Returns:
        Parsed configuration dictionary, or ``{}`` if the file is missing /
        malformed / has an unexpected top-level structure.
    """
    try:
        with open(config_path) as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.warning(
            "Config file not found at %s — falling through to env / hardcoded defaults.",
            config_path,
        )
        return {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning(
            "Failed to parse config at %s (%s) — falling through to env / hardcoded defaults.",
            config_path,
            exc,
        )
        return {}

    if not isinstance(raw, dict):
        logger.warning(
            "Config file %s has unexpected structure — falling through to env / hardcoded defaults.",
            config_path,
        )
        return {}

    orch = raw.get("orchestrator")
    if orch is not None and not isinstance(orch, dict):
        logger.warning(
            "orchestrator key in %s is not a mapping — falling through to env / hardcoded defaults.",
            config_path,
        )
        # Drop the malformed key so downstream ``config.get("orchestrator") or {}``
        # yields an empty dict cleanly.
        raw = {k: v for k, v in raw.items() if k != "orchestrator"}

    return raw


def _load_domain_prompt(project_root: Path, domain: str) -> str:
    """Read domain-specific guidelines from ``domains/{domain}/DOMAIN.md``.

    If the file is missing or cannot be decoded, a default prompt is returned
    so the orchestrator can operate without domain customisation.

    Args:
        project_root: Root directory of the project.
        domain: Name of the domain sub-directory under ``domains/``.

    Returns:
        The domain prompt text, or a default placeholder string.
    """
    domain_path = project_root / "domains" / domain / "DOMAIN.md"
    try:
        return domain_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Domain file not found at %s — using default prompt.", domain_path)
        return _DEFAULT_DOMAIN_PROMPT
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read domain file %s (%s) — using default prompt.", domain_path, exc)
        return _DEFAULT_DOMAIN_PROMPT


def _resolve_model(
    env_var: str,
    yaml_value: Any,
    default_value: str,
    key_name: str,
) -> str:
    """Resolve a model identifier with precedence ``env > yaml > default``.

    Falsy values (``None``, empty string) at the env and yaml layers fall
    through to the next layer so that an operator who unsets a variable with
    ``AGENT_MODELS__REASONING_MODEL=`` does not accidentally blank out the
    YAML entry. The final resolved value and its source are logged at INFO
    so operators can confirm which layer won.

    Args:
        env_var: Environment variable name (e.g. ``AGENT_MODELS__REASONING_MODEL``).
        yaml_value: Value read from ``orchestrator-config.yaml`` (may be
            ``None`` or missing).
        default_value: Hardcoded fallback from :data:`_DEFAULT_CONFIG`.
        key_name: Short key name used in the INFO log (e.g. ``"reasoning_model"``).

    Returns:
        The resolved model identifier string.
    """
    env_val = os.environ.get(env_var)
    if env_val:
        logger.info("Resolved %s=%s (source=env)", key_name, env_val)
        return env_val
    if yaml_value:
        logger.info("Resolved %s=%s (source=yaml)", key_name, yaml_value)
        return yaml_value
    logger.info("Resolved %s=%s (source=default)", key_name, default_value)
    return default_value


def _build_agent(
    config: dict[str, Any],
    domain_prompt: str,
) -> Any:
    """Wire configuration and domain prompt into :func:`create_orchestrator`.

    Resolves ``reasoning_model`` and ``implementation_model`` with precedence
    ``env > yaml > hardcoded default`` via :func:`_resolve_model`, keeping
    provider resolution at the factory (LES1 §3 PMEV). Remaining wiring is a
    pure function of the resolved values so the helper stays unit-testable.

    Args:
        config: Parsed configuration dictionary (from :func:`_load_config`).
        domain_prompt: Domain-specific prompt text (from :func:`_load_domain_prompt`).

    Returns:
        A compiled state graph produced by ``create_orchestrator()``.
    """
    orch_config = config.get("orchestrator") or {}
    defaults = _DEFAULT_CONFIG["orchestrator"]

    reasoning_model = _resolve_model(
        _ENV_REASONING_MODEL,
        orch_config.get("reasoning_model"),
        defaults["reasoning_model"],
        "reasoning_model",
    )
    implementation_model = _resolve_model(
        _ENV_IMPLEMENTATION_MODEL,
        orch_config.get("implementation_model"),
        defaults["implementation_model"],
        "implementation_model",
    )

    return create_orchestrator(
        reasoning_model=reasoning_model,
        implementation_model=implementation_model,
        domain_prompt=domain_prompt,
    )


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

# Use parse_known_args() so that unexpected sys.argv values injected by the
# LangGraph server (or other import-time callers) do not raise SystemExit.
_arg_parser = argparse.ArgumentParser(
    description="DeepAgents orchestrator exemplar",
    add_help=True,
)
_arg_parser.add_argument(
    "--domain",
    default=DEFAULT_DOMAIN,
    help="Domain name to load from domains/{domain}/DOMAIN.md (default: %(default)s)",
)
_parsed_args, _unknown_args = _arg_parser.parse_known_args()

# ---------------------------------------------------------------------------
# Module-level initialisation
# ---------------------------------------------------------------------------

# Load environment variables from .env (no error if absent).
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)

# Read config and domain, then build the orchestrator agent.
_config = _load_config(_PROJECT_ROOT / "orchestrator-config.yaml")
_domain_prompt = _load_domain_prompt(_PROJECT_ROOT, _parsed_args.domain)

#: Module-level agent variable required by langgraph.json.
#: ``langgraph.json`` references ``./agent.py:agent``.
agent = _build_agent(_config, _domain_prompt)
