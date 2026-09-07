"""``SidecarScriptRunner`` — the forge-side client of the deploy sidecar (S1).

When ``deploy.execution_surface == "sidecar"`` the deploy stage routes its
docker-touching script steps (``deploy_compose``, ``health_check``) through this
runner instead of the in-process subprocess core. The runner is a drop-in for
:func:`forge.executor.shell_steps._run_script_step`: same keyword signature,
same ``(exit_code, output)`` return, same **never-raises** posture — so the
handlers do not know or care which surface executed the script.

The sidecar resolves the working directory itself (from ``repo`` +
``planning.target_repo_paths``); the runner is bound to the target ``repo``
(org/name) at construction. The ``cwd`` the handler passes rides along as
``cwd`` in the request, and the sidecar honours it in exactly one case —
protect-main (2026-09-07): a candidate tree, an existing directory directly
under ``<checkout>/.forge-candidates/``, so the candidate leg builds the feature
branch's own tree. Any other value is ignored there, as it always was. A
transport or sidecar error is returned as a non-zero exit code with a
descriptive body — never raised — mirroring the local core's contract.

THE ANSWER MUST SAY WHERE IT RAN. A sidecar running the code from before the
candidate leg ignores ``cwd`` and runs the profile's script from the checkout:
the "candidate" would then be main, its checks would pass on main, an unchecked
branch would merge, and the report would say the branch was checked. That is
the exact silent defect protect-main exists to stop, so when the working
directory sent names a candidate tree, the sidecar's answer must carry the
working directory it actually used (``cwd``) and it must be that tree; an
answer without it, or naming somewhere else, is returned as a non-zero exit
with a plain sentence, and the candidate leg stops before anything merges.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from forge.deploy.candidate_tree import CANDIDATE_TREES_DIRNAME

logger = logging.getLogger(__name__)

#: Exit code returned when the sidecar could not be reached or answered
#: malformed data. Distinct from a script's own exit code (which the sidecar
#: relays verbatim) so a transport failure is not mistaken for a script failure.
SIDECAR_TRANSPORT_EXIT_CODE = 1


class SidecarScriptRunner:
    """A ``_run_script_step``-compatible callable that POSTs to the sidecar.

    Bound to one ``repo`` (org/name) and the sidecar ``base_url``. Each call maps
    the handler's ``(cwd, script, env_file, timeout, extra_env)`` to the sidecar
    ``/run`` contract ``{repo, script, env, timeout_seconds, cwd}`` and unpacks
    the ``{exit_code, output_tail}`` response.
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
        cwd: str,
        script: str,
        env_file: str | None,
        timeout: float = 600.0,
        output_cap: int | None = None,  # noqa: ARG002 — sidecar caps its own tail
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        env: dict[str, str] = dict(extra_env or {})
        if env_file is not None:
            env["ENV_FILE"] = env_file
        body: dict[str, object] = {
            "repo": self._repo,
            "script": script,
            "env": env,
            "timeout_seconds": timeout,
        }
        if isinstance(cwd, str) and cwd.strip():
            # The sidecar decides: a candidate tree is honoured, anything else
            # is ignored in favour of the profile's own working directory.
            body["cwd"] = cwd
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
        not_honoured = candidate_tree_not_honoured(cwd, parsed)
        if not_honoured is not None:
            logger.error("sidecar script runner: %s", not_honoured)
            return (SIDECAR_TRANSPORT_EXIT_CODE, not_honoured)
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


def names_a_candidate_tree(cwd: object) -> bool:
    """Does this working directory lie under a ``.forge-candidates`` directory?"""
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    return CANDIDATE_TREES_DIRNAME in Path(cwd).parts


def _same_directory(sent: str, answered: str) -> bool:
    """The same place, spelled either way: as sent, or fully resolved."""
    if os.path.normpath(sent) == os.path.normpath(answered):
        return True
    try:
        return Path(sent).resolve() == Path(answered).resolve()
    except OSError:
        return False


def candidate_tree_not_honoured(sent_cwd: object, answer: dict[str, object]) -> str | None:
    """The plain sentence when a candidate tree was sent and the answer did not run there.

    ``None`` when the working directory sent is not a candidate tree (the
    sidecar ignores it, as it always did) or when the answer names that tree.
    Otherwise the sentence that becomes the step's failure: the deploy sidecar
    on the host is running old code (no ``cwd`` in its answer) or a different
    checkout path (a different ``cwd``), and the candidate was not checked.
    """
    if not names_a_candidate_tree(sent_cwd):
        return None
    sent = str(sent_cwd)
    answered = answer.get("cwd")
    if not isinstance(answered, str) or not answered.strip():
        return (
            f"the deploy sidecar did not run in the candidate tree {sent} — "
            "it did not say where it ran, so it is running old code from before "
            "the candidate check; the candidate was not checked"
        )
    if _same_directory(sent, answered):
        return None
    return (
        f"the deploy sidecar did not run in the candidate tree {sent} — "
        f"it ran in {answered}, so it is running a different checkout path; "
        "the candidate was not checked"
    )


__all__ = [
    "SidecarScriptRunner",
    "SIDECAR_TRANSPORT_EXIT_CODE",
    "candidate_tree_not_honoured",
    "names_a_candidate_tree",
]
