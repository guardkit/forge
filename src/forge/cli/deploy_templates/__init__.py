"""The files `forge register-repo --deploy-port` writes into a repository.

They live here as real shell and real compose YAML, not as Python strings, so
they can be read and diffed as what they are.

``sandbox-deploy.sh`` is the wrapper that brings a repository's Docker Sandbox
up and runs its deploy script inside it. It is copied out exactly as it is
here — the same file api_test deploys with, byte for byte — because every
value it needs reaches it in its environment from ``deploy/profile.yaml``.

``deploy.sh`` (a copy of api_test's own deploy script) and
``docker-compose.candidate.yml`` (the overlay that puts the throwaway
candidate copy on its own port) do carry the repository's own names and
ports: the ``@@TOKEN@@`` marks in them are filled in when they are written.
"""
