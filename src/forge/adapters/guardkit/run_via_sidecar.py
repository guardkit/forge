"""The merge word's checks, run on the host through the deploy sidecar.

The merge card's post-merge checks used to run inside the forge container.
Guardkit resolves a repository's test command to that repository's own virtual
environment, and inside the container that interpreter is a link to a host file
which is not there — so the command exited 127 and the merge answered "the test
runner could not start". That happened on the first real press of a merge card
(FEAT-3ABD, 2026-09-06): the branch went in and nothing else moved.

Every build's own tests already run on the host, through the deploy sidecar. So
does the merge's check now. This module is the forge side of that door: a
callable with exactly the shape the merge executor already calls
(:func:`forge.adapters.guardkit.run.run`), returning exactly the
:class:`~forge.adapters.guardkit.models.GuardKitResult` the executor already
reads, but sending the work over loopback HTTP to the sidecar's
``/guardkit-merge`` operation instead of starting a process in the container.

The door is deliberately one command wide. It accepts ``autobuild`` with
``merge`` as its first argument and refuses everything else loudly, so no other
guardkit call can wander onto the host this way.

The pre-merge baseline — the list of tests the target branch was already
failing — is sent inline rather than as a path. The executor writes that file
inside the container, where the sidecar cannot read it; the sidecar writes its
own copy on the host from the list this module sends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning

logger = logging.getLogger(__name__)

#: The sidecar operation this module talks to.
MERGE_ENDPOINT: str = "/guardkit-merge"

#: The only subcommand this door will carry.
ALLOWED_SUBCOMMAND: str = "autobuild"

#: The only verb of that subcommand this door will carry.
ALLOWED_VERB: str = "merge"

#: The only branch this door will merge into. The sidecar runs
#: ``--target main`` as a fixed part of its command, so a request naming any
#: other branch could not be honoured and is refused here instead of being
#: quietly run against main.
ALLOWED_TARGET: str = "main"

#: How much longer than the command's own wall the HTTP read waits, in seconds.
#: The socket must not give up before the sidecar's own timeout fires — the
#: same discipline the deploy stage's sidecar client uses.
HTTP_TIMEOUT_MARGIN_SECONDS: float = 30.0

#: Exit code reported when the sidecar could not be reached or answered
#: something that was not a merge result. Kept distinct from guardkit's own
#: exit codes so a transport problem is never read as a merge verdict.
TRANSPORT_EXIT_CODE: int = 1


class MergeCallRefused(ValueError):
    """The caller asked for something this door does not carry.

    Raised — never returned as a result — because it means a caller tried to
    run something other than the merge word's own command on the host. That is
    a programming mistake to fix, not a merge outcome to report.
    """


def _flag_value(args: list[str], flag: str) -> str | None:
    """Return the value that follows ``flag`` in ``args``, or ``None``."""
    for index, token in enumerate(args):
        if token == flag and index + 1 < len(args):
            return args[index + 1]
        if token.startswith(f"{flag}="):
            return token.split("=", 1)[1]
    return None


def _read_baseline_failing(path_text: str) -> list[str] | None:
    """Read the list of already-failing tests out of the baseline file.

    Returns ``None`` when the file cannot be read or does not carry a list —
    the merge then runs without a pre-merge baseline, which is what happens
    today when the file could not be written either.
    """
    try:
        data = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "guardkit merge over the sidecar: could not read the list of "
            "tests that were already failing from %s (%s) — the merge runs "
            "without one",
            path_text,
            exc,
        )
        return None
    failing: Any = None
    if isinstance(data, list):
        failing = data
    elif isinstance(data, dict):
        # "failing_node_ids" is the name guardkit itself uses, both in the
        # baseline file it accepts and in the baseline.json it writes; the
        # older "failing" is still read so a file written by a previous
        # version of forge is not thrown away.
        failing = data.get("failing_node_ids")
        if failing is None:
            failing = data.get("failing")
    if not isinstance(failing, list):
        logger.warning(
            "guardkit merge over the sidecar: %s does not carry a list of "
            "tests that were already failing — the merge runs without one",
            path_text,
        )
        return None
    return [str(entry) for entry in failing]


def _resolve_repo_key(repo_path: Path, repo_paths: Mapping[str, str]) -> str | None:
    """Return the org/name key the sidecar knows this repository by."""
    wanted = repo_path.resolve(strict=False)
    for key, configured in repo_paths.items():
        if Path(configured).resolve(strict=False) == wanted:
            return key
    return None


def _failed_result(
    *, detail: str, duration_secs: float, warning_code: str
) -> GuardKitResult:
    """A failure this side of the wire, in the shape the executor reads."""
    return GuardKitResult(
        status="failed",
        subcommand=f"{ALLOWED_SUBCOMMAND} {ALLOWED_VERB}",
        duration_secs=duration_secs,
        stdout_tail="",
        stderr=detail,
        exit_code=TRANSPORT_EXIT_CODE,
        warnings=[GuardKitWarning(code=warning_code, message=detail)],
    )


def _post(url: str, body: dict[str, Any], *, timeout: float) -> tuple[int, Any]:
    """POST ``body`` as JSON and return ``(http_status, parsed_body)``.

    A 4xx or 5xx comes back as its status and its parsed body rather than an
    exception, so the caller can put the sidecar's own sentence on the record.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 — best-effort detail
            reason = exc.reason if isinstance(exc.reason, str) else "unknown"
            return exc.code, {"error": reason}


def build_sidecar_guardkit_run(
    *,
    base_url: str,
    repo_paths: Mapping[str, str],
    http_timeout_margin: float = HTTP_TIMEOUT_MARGIN_SECONDS,
) -> Callable[..., Awaitable[GuardKitResult]]:
    """Return a merge-only ``guardkit_run`` that works through the sidecar.

    Args:
        base_url: Where the deploy sidecar listens, e.g.
            ``http://127.0.0.1:8125``.
        repo_paths: The ``planning.target_repo_paths`` mapping, used to turn
            the executor's repository path back into the org/name key the
            sidecar resolves paths by.
        http_timeout_margin: Seconds added to the command's own wall before
            the socket gives up, so the sidecar's timeout always fires first.

    The returned callable takes the same keywords the in-container run takes
    (``subcommand``, ``args``, ``repo_path``, ``read_allowlist``,
    ``timeout_seconds``, and the two the merge never uses) and returns the same
    :class:`GuardKitResult`.
    """
    endpoint = f"{base_url.rstrip('/')}{MERGE_ENDPOINT}"
    known_paths = dict(repo_paths)

    async def run_merge_via_sidecar(
        *,
        subcommand: str,
        args: list[str],
        repo_path: Path,
        read_allowlist: list[Path] | None = None,  # noqa: ARG001 — the sidecar
        # resolves the working directory from the repository key itself
        timeout_seconds: int = 900,
        with_nats_streaming: bool = False,  # noqa: ARG001 — no broker on this door
        extra_context_paths: list[str] | None = None,  # noqa: ARG001 — merge only
    ) -> GuardKitResult:
        started_at = time.monotonic()

        # THE DOOR IS ONE COMMAND WIDE. Anything else is a mistake in the
        # caller, so it is raised rather than reported as a merge outcome.
        if subcommand != ALLOWED_SUBCOMMAND or not args or args[0] != ALLOWED_VERB:
            raise MergeCallRefused(
                "the deploy sidecar only carries the merge word's own command "
                f"({ALLOWED_SUBCOMMAND} {ALLOWED_VERB}); it was asked to run "
                f"{subcommand!r} with {args!r}"
            )
        target = _flag_value(args, "--target")
        if target is not None and target != ALLOWED_TARGET:
            raise MergeCallRefused(
                "the deploy sidecar merges into "
                f"{ALLOWED_TARGET!r} and nothing else; it was asked to merge "
                f"into {target!r}"
            )
        if len(args) < 2:
            raise MergeCallRefused(
                "the merge command needs the feature name after 'merge'; got "
                f"{args!r}"
            )
        feature_id = args[1]
        expect_main_sha = _flag_value(args, "--expect-main-sha")
        if not expect_main_sha:
            raise MergeCallRefused(
                "the merge command needs --expect-main-sha so the merge can "
                f"refuse a target branch that has moved; got {args!r}"
            )

        repo_key = _resolve_repo_key(repo_path, known_paths)
        if repo_key is None:
            known = ", ".join(sorted(known_paths)) or "(none configured)"
            return _failed_result(
                detail=(
                    f"the deploy sidecar does not know the repository at "
                    f"{repo_path} — it is not one of the paths in "
                    f"planning.target_repo_paths. Known repositories: {known}"
                ),
                duration_secs=time.monotonic() - started_at,
                warning_code="sidecar_repo_not_configured",
            )

        body: dict[str, Any] = {
            "repo": repo_key,
            "feature_id": feature_id,
            "expect_main_sha": expect_main_sha,
            "timeout_seconds": float(timeout_seconds),
        }
        baseline_path = _flag_value(args, "--baseline-json")
        if baseline_path:
            failing = _read_baseline_failing(baseline_path)
            if failing is not None:
                body["baseline_failing"] = failing

        http_timeout = float(timeout_seconds) + http_timeout_margin
        try:
            status, parsed = await asyncio.to_thread(
                _post, endpoint, body, timeout=http_timeout
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return _failed_result(
                detail=(
                    f"the deploy sidecar at {base_url} could not be reached, "
                    f"so the merge did not run: {exc}"
                ),
                duration_secs=time.monotonic() - started_at,
                warning_code="sidecar_unreachable",
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _failed_result(
                detail=(
                    f"the deploy sidecar at {base_url} answered something that "
                    f"was not JSON, so the merge result is unknown: {exc}"
                ),
                duration_secs=time.monotonic() - started_at,
                warning_code="sidecar_bad_answer",
            )
        except Exception as exc:  # noqa: BLE001 — never raise past the boundary
            return _failed_result(
                detail=(
                    f"talking to the deploy sidecar at {base_url} went wrong, "
                    f"so the merge result is unknown: {type(exc).__name__}: {exc}"
                ),
                duration_secs=time.monotonic() - started_at,
                warning_code="sidecar_client_error",
            )

        duration = time.monotonic() - started_at
        if status != 200:
            detail = ""
            if isinstance(parsed, dict):
                detail = str(parsed.get("error") or "")
            return _failed_result(
                detail=(
                    f"the deploy sidecar refused to run the merge "
                    f"(HTTP {status}): {detail or parsed!r}"
                ),
                duration_secs=duration,
                warning_code="sidecar_refused",
            )
        if not isinstance(parsed, dict) or not isinstance(
            parsed.get("exit_code"), int
        ) or isinstance(parsed.get("exit_code"), bool):
            return _failed_result(
                detail=(
                    "the deploy sidecar's answer did not carry an exit code, "
                    f"so the merge result is unknown: {parsed!r}"
                ),
                duration_secs=duration,
                warning_code="sidecar_bad_answer",
            )

        exit_code = int(parsed["exit_code"])
        stdout = parsed.get("stdout")
        stderr = parsed.get("stderr_tail")
        return GuardKitResult(
            status="success" if exit_code == 0 else "failed",
            subcommand=f"{ALLOWED_SUBCOMMAND} {ALLOWED_VERB}",
            duration_secs=duration,
            # The WHOLE of stdout, not a short tail: the executor reads the
            # merge report out of it.
            stdout_tail=stdout if isinstance(stdout, str) else "",
            stderr=stderr if isinstance(stderr, str) else None,
            exit_code=exit_code,
        )

    return run_merge_via_sidecar


__all__ = [
    "ALLOWED_SUBCOMMAND",
    "ALLOWED_TARGET",
    "ALLOWED_VERB",
    "HTTP_TIMEOUT_MARGIN_SECONDS",
    "MERGE_ENDPOINT",
    "TRANSPORT_EXIT_CODE",
    "MergeCallRefused",
    "build_sidecar_guardkit_run",
]
