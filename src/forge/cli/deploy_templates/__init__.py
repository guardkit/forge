"""The two shell files `forge register-repo --deploy-port` writes.

They live here as real shell, not as Python strings, so they can be read and
diffed like the shell they are. ``deploy.sh`` is a copy of api_test's own
deploy script; ``sandbox-deploy.sh`` is the wrapper that brings a repository's
Docker Sandbox up and runs that script inside it. The ``@@TOKEN@@`` marks in
both are filled in with the repository's name, compose project and ports.
"""
