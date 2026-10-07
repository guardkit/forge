"""The two shell scripts the factory ships for a project's Docker Sandbox.

They live here as real shell, not as Python strings, so they can be read and
diffed as what they are.

``sandbox-deploy.sh`` is the wrapper that brings a project's Docker Sandbox
up and runs its deploy script inside it. It is the same file api_test deploys
with, byte for byte, because every value it needs reaches it in its
environment from ``deploy/profile.yaml``.

``sandbox-runner.sh`` is the bootstrap that runs *inside* the sandbox and
brings up the factory's two services there — the deploy helper and the build
runner — from the release image (Rich's rule of 2026-09-07: nothing the
factory runs on a project runs on the host). The one-page upgrade procedure
copies it into the sandbox's clone, and ``deploy/estate/rollout-sandbox``
installs it.

Until 5 October 2026 ``forge register-repo --deploy-port`` also wrote a
templated ``deploy.sh`` and candidate overlay into a repository. The container
set-up's register-repo writes nothing into a project, and deploy files belong
to a project that deploys its own app, so those two templates were removed.
"""

# Which of these scripts a project's tree holds as the factory's own, and
# where, is kept in forge.factory_files, which the planner reads too.
from forge.factory_files import SHIPPED_SCRIPTS  # noqa: E402

__all__ = ["SHIPPED_SCRIPTS"]
