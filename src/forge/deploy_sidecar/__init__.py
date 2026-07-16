"""forge-deploy-sidecar — the durable execution surface for the deploy stage.

The forge container deliberately has no docker access (C4 catch #24). This
package is the loopback-only host service that runs the deploy stage's vetted,
profile-named scripts, so the in-container daemon can dispatch deploys without a
docker socket. See :mod:`forge.deploy_sidecar.service` for the contract and the
deny-by-default laws; design of record:
``docs/factory-deploy-execution-surface-design-2026-07-16.md`` §1.
"""

from __future__ import annotations

from forge.deploy_sidecar.service import (
    DEFAULT_PORT,
    HOST,
    SIDECAR_CODE_VERSION,
    allowed_env_keys,
    allowed_scripts,
    build_server,
    process_run_request,
    resolve_code_version,
    serve,
)

__all__ = [
    "DEFAULT_PORT",
    "HOST",
    "SIDECAR_CODE_VERSION",
    "allowed_env_keys",
    "allowed_scripts",
    "build_server",
    "process_run_request",
    "resolve_code_version",
    "serve",
]
