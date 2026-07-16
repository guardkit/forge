"""``SidecarScriptRunner`` — the forge-side client of the deploy sidecar (S1).

When ``deploy.execution_surface == "sidecar"`` the deploy stage routes its
docker-touching script steps (``deploy_compose``, ``health_check``) through this
runner instead of the in-process subprocess core. The runner is a drop-in for
:func:`forge.executor.shell_steps._run_script_step`: same keyword signature,
same ``(exit_code, output)`` return, same **never-raises** posture — so the
handlers do not know or care which surface executed the script.

The sidecar resolves the working directory itself (from ``repo`` +
``planning.target_repo_paths``), so the ``cwd`` the handler passes is ignored
here; the runner is bound to the target ``repo`` (org/name) at construction. A
transport or sidecar error is returned as a non-zero exit code with a
descriptive body — never raised — mirroring the local core's contract.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

#: Exit code returned when the sidecar could not be reached or answered
#: malformed data. Distinct from a script's own exit code (which the sidecar
#: relays verbatim) so a transport failure is not mistaken for a script failure.
SIDECAR_TRANSPORT_EXIT_CODE = 1


class SidecarScriptRunner:
    """A ``_run_script_step``-compatible callable that POSTs to the sidecar.

    Bound to one ``repo`` (org/name) and the sidecar ``base_url``. Each call maps
    the handler's ``(cwd, script, env_file, timeout, extra_env)`` to the sidecar
    ``/run`` contract ``{repo, script, env, timeout_seconds}`` and unpacks the
    ``{exit_code, output_tail}`` response.
    """

    def __init__(self, *, base_url: str, repo: str, http_timeout_margin: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._repo = repo
        # The HTTP read wall is the script timeout plus a margin, so the socket
        # does not trip before the sidecar's own subprocess timeout fires.
        self._http_timeout_margin = http_timeout_margin

    def __call__(
        self,
        *,
        cwd: str,  # noqa: ARG002 — the sidecar resolves cwd from repo itself
        script: str,
        env_file: str | None,
        timeout: float = 600.0,
        output_cap: int | None = None,  # noqa: ARG002 — sidecar caps its own tail
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        env: dict[str, str] = dict(extra_env or {})
        if env_file is not None:
            env["ENV_FILE"] = env_file
        body = {
            "repo": self._repo,
            "script": script,
            "env": env,
            "timeout_seconds": timeout,
        }
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/run",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        http_timeout = float(timeout) + self._http_timeout_margin
        try:
            with urllib.request.urlopen(request, timeout=http_timeout) as resp:
                raw = resp.read()
                parsed = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx from the sidecar (e.g. a deny-by-default refusal). Read
            # the error body so the refusal message is on the record.
            detail = self._read_error_body(exc)
            return (
                SIDECAR_TRANSPORT_EXIT_CODE,
                f"sidecar refused (HTTP {exc.code}): {detail}",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return (
                SIDECAR_TRANSPORT_EXIT_CODE,
                f"sidecar unreachable at {self._base_url}: {exc}",
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return (
                SIDECAR_TRANSPORT_EXIT_CODE,
                f"sidecar returned a non-JSON response: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — never raise past the boundary
            logger.warning("sidecar script runner unexpected error: %s", exc)
            return (SIDECAR_TRANSPORT_EXIT_CODE, f"sidecar client error: {exc}")

        if not isinstance(parsed, dict) or "exit_code" not in parsed:
            return (
                SIDECAR_TRANSPORT_EXIT_CODE,
                f"sidecar response missing exit_code: {parsed!r}",
            )
        exit_code = parsed.get("exit_code")
        output = parsed.get("output_tail", "")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return (
                SIDECAR_TRANSPORT_EXIT_CODE,
                f"sidecar returned a non-integer exit_code: {exit_code!r}",
            )
        return (exit_code, output if isinstance(output, str) else str(output))

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            if isinstance(payload, dict) and "error" in payload:
                return str(payload["error"])
            return str(payload)
        except Exception:  # noqa: BLE001 — best-effort detail
            return exc.reason if isinstance(exc.reason, str) else "unknown"


__all__ = ["SidecarScriptRunner", "SIDECAR_TRANSPORT_EXIT_CODE"]
