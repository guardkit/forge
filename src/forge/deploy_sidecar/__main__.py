"""``python -m forge.deploy_sidecar`` — run the loopback deploy sidecar.

The ExecStart target of ``scripts/systemd/forge-deploy-sidecar.service``. Binds
127.0.0.1 only; reads its repo allowlist from the forge config named by
``FORGE_CONFIG_PATH`` (else ``./forge.yaml``). The port can be overridden with
``FORGE_DEPLOY_SIDECAR_PORT`` for a drill on a non-default port.
"""

from __future__ import annotations

import os

from forge.deploy_sidecar.service import DEFAULT_PORT, serve


def main() -> None:
    port = int(os.environ.get("FORGE_DEPLOY_SIDECAR_PORT", DEFAULT_PORT))
    serve(port=port)


if __name__ == "__main__":
    main()
