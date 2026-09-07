"""The forge-deploy-sidecar service (S1, C4 residue #24).

A small, loopback-only HTTP service that executes the *vetted, profile-named*
deploy scripts for a target repo. It exists because the forge container
deliberately has no docker access (C4 catch #24): the sidecar runs on Rich's
box, as Rich's user, and adds **zero new privilege** — it can only run scripts
the target repo's own ``deploy/profile.yaml`` already names (deny by default).

Design of record: docs/factory-deploy-execution-surface-design-2026-07-16.md §1.

The narrow contract:

    GET  /healthz -> {"status": "healthy", "rev": "git-<sha>"}
    POST /run  {repo, script, env, timeout_seconds, cwd?}
              -> {exit_code, output_tail, cwd}
    POST /guardkit-merge  {repo, feature_id, expect_main_sha, baseline_failing,
                           timeout_seconds, verify_timeout_seconds}
              -> {exit_code, stdout, stderr_tail}
    POST /git/prepare-branch-and-write-tree
              {repo, branch, files, message, checks: [{name, args, blocking}]}
              -> {status, sha, checks: [{name, blocking, ran, passed, exit_code,
                                          stdout, stderr_tail, timed_out,
                                          detail, note}], detail}
    POST /git/read-file-from-branch {repo, branch, file_path} -> {content|null}
    POST /git/rev-parse {repo, ref} -> {sha|null}

The three git operations (sandbox first, 2026-09-07, rule 70) make the
planning chain's commits where the repository lives: the caller declares the
pre-commit checks by name and the sidecar runs them with the guardkit beside
it. See LAW 9 below the merge operation.

The second operation exists because the merge word's post-merge checks must run
where the builds run. The forge container has no host virtual environment, so a
check resolved to ``<repo>/.venv/bin/python`` exits 127 inside it and the merge
answers "the test runner could not start" (this happened on the first real press
of a merge card, 2026-09-06). The sidecar already runs on the host as Rich's
user, so the merge command runs here instead. It is deny-by-default in the same
way: one fixed command, one known repository, a feature name and a target commit
that must both be well formed.

THE DENY-BY-DEFAULT LAWS (each one a test in tests/forge/deploy_sidecar):

1. ``repo`` resolves via the SAME ``planning.target_repo_paths`` mapping the
   daemon uses; an unknown key is a loud 4xx naming the known keys.
2. The sidecar re-reads ``<repo>/deploy/profile.yaml`` ITSELF (via
   :func:`forge.deploy.profile.load_deploy_profile`) and REFUSES any script the
   profile does not name. The ONLY runnable scripts are ``compose.script``,
   each ``health_checks[].cmd``, and the ``live_gate.driver`` script path.
3. Every env key must be in the allowlist
   ``{REVERT, ROLLBACK_IMAGE_REF, ENV_FILE, CANDIDATE, PROMOTE, CANDIDATE_DOWN,
   SANDBOX_NAME, SANDBOX_MEMORY, SANDBOX_CPUS, SANDBOX_PUBLISH,
   SANDBOX_ALLOW_NETWORK}`` UNION the profile's ``live_gate.env`` and
   ``candidate.env`` key names; anything else is refused loudly. Values must be
   strings.
4. ``timeout_seconds`` is capped (default 600, max 1800).
5. The server binds ``127.0.0.1`` ONLY.
6. There is NO shell: execution goes through the existing
   :func:`forge.executor.shell_steps._run_script_step` subprocess core (reused
   with ``extra_env``) — never a second executor, never freehand shell. The
   merge operation has no vetted script to run, so it runs ONE fixed argument
   list (:func:`run_merge_command`) — still no shell, and on a timeout the whole
   process group is killed, not just the command's own process.
7. The merge operation runs ``guardkit autobuild merge`` and nothing else. The
   repository key, the feature name (``FEAT-`` plus three to twelve capitals or
   digits) and the forty-character target commit are all checked before any
   process starts, and the timeout may not exceed half an hour.
8. A working directory named by the caller (``cwd``) takes effect in ONE case
   only — protect-main (2026-09-07): an existing directory directly under
   ``<repo>/.forge-candidates/``, where the merge word lays out the feature
   branch's tree so the candidate is built from the exact commit the merge
   will land. A path under that directory that does not exist, or is not
   directly under it, is refused loudly. Any other value is ignored and the
   profile's own working directory is used, as it always was. The script
   still has to be one the profile names; it is found relative to the
   working directory, so the candidate's own copy runs. The answer carries
   the working directory the script actually ran in (``cwd``), so the
   caller can tell a sidecar that honoured the candidate tree from one that
   is running old code or a different checkout path and silently ran the
   script from the checkout — main, checked and reported as the branch.

Each request-processing core (:func:`process_run_request` and
:func:`process_guardkit_merge_request`) is a pure function
``(payload, config, runner) -> (http_status, body)`` so every law is
unit-testable without a live socket. Neither **ever raises** past its boundary.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from forge.config.loader import load_config
from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import candidate_trees_root, is_candidate_tree_path
from forge.deploy.profile import (
    DeployProfile,
    DeployProfileError,
    load_deploy_profile,
)
from forge.executor.shell_steps import _run_script_step
from forge.memory.redaction import scrub_process_output
from forge.planning.handoff import (
    PRE_COMMIT_CHECK_NAMES,
    PreCommitCheckOutcome,
    PreCommitResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Loopback-only bind host (LAW 5). The box is on the host network, so binding
#: anything but 127.0.0.1 would expose the runner on every interface.
HOST: str = "127.0.0.1"

#: Default listen port for the sidecar.
DEFAULT_PORT: int = 8125

#: Default per-request timeout in seconds (mirrors the executor default).
TIMEOUT_DEFAULT: float = 600.0

#: Hard cap on a request-supplied timeout (LAW 4). A caller cannot ask the
#: sidecar to hold a subprocess open longer than this.
TIMEOUT_MAX: float = 1800.0

#: Base env-key allowlist (LAW 3). The profile's live_gate.env / candidate.env
#: key names are unioned onto this per request.
ENV_ALLOWLIST_BASE: frozenset[str] = frozenset(
    {
        "REVERT",
        "ROLLBACK_IMAGE_REF",
        "ENV_FILE",
        "CANDIDATE",
        "PROMOTE",
        # Make-merge-work (2026-08-24): the candidate-stack teardown env.
        # Without it every sidecar-surface run leaks the candidate stack on
        # :8902 — the teardown request's env key was refused 400.
        "CANDIDATE_DOWN",
        # Deploying into a Docker Sandbox (2026-09-06 decision). The five
        # settings of the repository's own deployment sandbox, threaded by the
        # deploy stage and read by the repository's vetted wrapper. They name a
        # sandbox and its size, ports and network rules — they carry no secret
        # and grant no new privilege, and a repository whose profile has no
        # sandbox block never sends them.
        "SANDBOX_NAME",
        "SANDBOX_MEMORY",
        "SANDBOX_CPUS",
        "SANDBOX_PUBLISH",
        "SANDBOX_ALLOW_NETWORK",
    }
)

#: Maximum characters of script output returned as ``output_tail``. The
#: _run_script_step core already byte-caps its capture; this trims to the TAIL
#: (the interesting end — the failure/last lines) for the wire response.
OUTPUT_TAIL_CHARS: int = 65_536

#: Truncation marker prepended when the tail drops leading output.
_TAIL_MARKER = "... [OUTPUT HEAD TRUNCATED] ...\n"


# --- the merge operation's own constants -----------------------------------

#: Default wall on the merge command, in seconds (fifteen minutes).
MERGE_TIMEOUT_DEFAULT: float = 900.0

#: Hard cap on a caller-supplied merge timeout, in seconds (half an hour). A
#: request asking for longer is refused, not quietly shortened, so nobody can
#: believe they asked for something the sidecar did not do.
MERGE_TIMEOUT_MAX: float = 1800.0

#: How long the merge command's process group gets to stop politely after a
#: timeout before it is killed outright.
MERGE_KILL_GRACE_SECONDS: float = 5.0

#: How long to keep reading a killed command's output before giving up on it.
MERGE_POST_KILL_READ_SECONDS: float = 10.0

#: Exit code reported when the merge command ran out of time (the shell
#: convention the rest of the estate already uses).
MERGE_TIMEOUT_EXIT_CODE: int = 124

#: Exit code reported when the merge command could not be started at all.
MERGE_NOT_STARTED_EXIT_CODE: int = 127

#: Maximum characters of merge stdout returned. The merge report is printed
#: last, so the TAIL is the part worth keeping.
MERGE_STDOUT_CHARS: int = 262_144

#: Maximum characters of merge stderr returned as ``stderr_tail``.
MERGE_STDERR_TAIL_CHARS: int = 16_384

#: The shape a feature name must have. This is the wire's own pattern
#: (``nats_core.events._pipeline.FEATURE_ID_PATTERN``), written out here rather
#: than imported because that module does not export it.
FEATURE_ID_PATTERN = re.compile(r"^FEAT-[A-Z0-9]{3,12}$")

#: The shape a target commit must have: a full forty-character git hash.
MAIN_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

#: Where the operator can name the guardkit command explicitly. The same knob
#: the build runner already honours, so one setting configures both.
GUARDKIT_PATH_ENV: str = "FORGE_GUARDKIT_PATH"

#: The command's name, as looked up on PATH when the env var is unset. The
#: sidecar unit's PATH puts ``~/.agentecflow/bin`` first, which is where the
#: host's guardkit lives.
GUARDKIT_BINARY_NAME: str = "guardkit"

#: Where the sidecar writes its own copy of the pre-merge baseline, relative to
#: the target repository.
MERGE_BASELINE_DIR: tuple[str, str] = (".guardkit", "tmp")


# ---------------------------------------------------------------------------
# The script-runner protocol (signature-compatible with _run_script_step)
# ---------------------------------------------------------------------------


class ScriptRunner(Protocol):
    """A callable with the :func:`_run_script_step` keyword signature.

    Injected so tests can substitute a stub runner and the production path uses
    the real credential-scrubbing subprocess core — never a second executor.
    """

    def __call__(
        self,
        *,
        cwd: str,
        script: str,
        env_file: str | None,
        timeout: float = ...,
        extra_env: dict[str, str] | None = ...,
    ) -> tuple[int, str]: ...


class MergeRunner(Protocol):
    """A callable that runs one fixed argument list and reports what happened.

    Injected so tests can record exactly what the sidecar would have run
    without starting a process. Returns ``(exit_code, stdout, stderr)``.
    """

    def __call__(
        self,
        *,
        argv: list[str],
        cwd: str,
        timeout: float = ...,
    ) -> tuple[int, str, str]: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SidecarConfigError(RuntimeError):
    """The sidecar could not resolve a forge config to read repo paths from."""


# ---------------------------------------------------------------------------
# Code-version stamp (boot-visible staleness signal, DEFECT #18a sibling)
# ---------------------------------------------------------------------------


def resolve_code_version() -> str:
    """Return ``git-<short-sha>`` of the running code, or a fallback.

    Mirrors ``forge.subagents.autobuild_runner._resolve_runner_code_version`` so
    an operator can grep the journal to confirm the sidecar is serving the
    intended git rev (a ``--restart``ed unit only picks up new code on restart).
    Never raises: the stamp must not block boot.
    """
    module_dir = Path(__file__).resolve().parent
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(module_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        rev = result.stdout.strip()
        if rev:
            return f"git-{rev}"
    except Exception:  # noqa: BLE001 — never block boot on the stamp
        pass
    try:
        from importlib.metadata import version

        return f"pkg-{version('forge')}"
    except Exception:  # noqa: BLE001
        return "unknown"


#: Import-time code-version stamp (see :func:`resolve_code_version`).
SIDECAR_CODE_VERSION: str = resolve_code_version()


# ---------------------------------------------------------------------------
# Allowlist derivation (LAWS 2 + 3)
# ---------------------------------------------------------------------------


def _looks_like_script_path(token: str) -> bool:
    """True iff ``token`` looks like a runnable script path (not a bare interp)."""
    return ("/" in token) or token.endswith((".py", ".sh"))


def allowed_scripts(profile: DeployProfile) -> set[str]:
    """The ONLY scripts this profile permits the sidecar to run (LAW 2).

    ``compose.script`` + every ``health_checks[].cmd`` + the ``live_gate.driver``
    script path(s). If a driver argv names no path-like element (an odd shape),
    every element is allowlisted so a deliberately-vetted driver is not silently
    un-runnable — but only elements the profile itself names.
    """
    scripts: set[str] = set()
    if profile.compose.script:
        scripts.add(profile.compose.script)
    for check in profile.health_checks:
        if check.cmd:
            scripts.add(check.cmd)
    if profile.live_gate is not None:
        driver = [d for d in profile.live_gate.driver if d]
        path_like = [d for d in driver if _looks_like_script_path(d)]
        scripts.update(path_like or driver)
    return scripts


def allowed_env_keys(profile: DeployProfile) -> set[str]:
    """The allowlisted env-key names for this profile (LAW 3).

    Base allowlist UNION ``live_gate.env`` keys UNION ``candidate.env`` keys.
    ``candidate`` is a first-class profile field (S2F): its ``env`` keys are read
    from ``profile.candidate``. A defensive fallback to ``profile.extra`` is kept
    for a profile parsed by an older loader that still parked ``candidate`` in
    ``extra`` (present-and-well-shaped only).
    """
    keys: set[str] = set(ENV_ALLOWLIST_BASE)
    if profile.live_gate is not None:
        keys.update(profile.live_gate.env.keys())
    if profile.candidate is not None:
        keys.update(profile.candidate.env.keys())
    else:
        candidate = profile.extra.get("candidate")
        if isinstance(candidate, dict):
            cand_env = candidate.get("env")
            if isinstance(cand_env, dict):
                keys.update(str(k) for k in cand_env)
    return keys


def _resolve_cwd(repo_path: Path, profile: DeployProfile) -> Path:
    """The subprocess cwd: the target repo root, honouring ``profile.cwd``."""
    if profile.cwd:
        p = Path(profile.cwd)
        return p if p.is_absolute() else repo_path / p
    return repo_path


def _resolve_requested_cwd(
    repo_path: Path, requested: Any
) -> tuple[Path | None, str | None]:
    """LAW 8 — the caller's working directory, honoured only for a candidate tree.

    Returns ``(cwd, error)``: a directory to run in when the request names an
    existing candidate tree; ``(None, None)`` when the value is absent or is
    anything other than a path under the candidate trees directory (ignored,
    as before); ``(None, <plain sentence>)`` when it points under that
    directory but is not an existing directory directly under it.
    """
    if not isinstance(requested, str) or not requested.strip():
        return None, None
    wanted = Path(requested)
    if not wanted.is_absolute():
        wanted = repo_path / wanted
    try:
        trees_root = candidate_trees_root(repo_path).resolve()
        resolved = wanted.resolve()
    except OSError:
        return None, None
    if trees_root not in resolved.parents:
        return None, None
    if not is_candidate_tree_path(repo_path, resolved) or not resolved.is_dir():
        return None, (
            f"working directory {requested!r} is not a candidate tree — the "
            "only working directory the sidecar accepts besides the profile's "
            f"own is an existing directory directly under {trees_root}"
        )
    return resolved, None


def _tail(output: str) -> str:
    """Return the last :data:`OUTPUT_TAIL_CHARS` chars of ``output``."""
    if len(output) <= OUTPUT_TAIL_CHARS:
        return output
    return _TAIL_MARKER + output[-OUTPUT_TAIL_CHARS:]


# ---------------------------------------------------------------------------
# The request-processing core — pure, never raises
# ---------------------------------------------------------------------------


def process_run_request(
    payload: Any,
    *,
    config: ForgeConfig,
    script_runner: ScriptRunner = _run_script_step,
) -> tuple[int, dict[str, Any]]:
    """Validate + execute a ``/run`` payload; return ``(http_status, body)``.

    Enforces every deny-by-default law before any subprocess is spawned. Returns
    a 4xx with a loud ``error`` on a refusal, a 500 on an unexpected internal
    error, and a 200 with ``{exit_code, output_tail, cwd}`` on a permitted run (the
    script's non-zero exit is a 200 with a non-zero ``exit_code``, not an HTTP
    error — the script's verdict is data, not a transport failure). Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}

    repo = payload.get("repo")
    script = payload.get("script")
    raw_env = payload.get("env")
    timeout_seconds = payload.get("timeout_seconds")

    # LAW 1 — repo resolves via planning.target_repo_paths (loud on miss).
    paths = config.planning.target_repo_paths
    if not isinstance(repo, str) or not repo.strip():
        return 400, {
            "error": (
                "'repo' is required (an org/name key from "
                "planning.target_repo_paths)"
            )
        }
    if repo not in paths:
        known = ", ".join(sorted(paths)) or "(none configured)"
        return 400, {
            "error": (
                f"unknown target repo {repo!r} — not in "
                f"planning.target_repo_paths. Known keys: {known}"
            )
        }
    repo_path = Path(paths[repo])

    # LAW 2 (part a) — re-read the target's profile ourselves.
    profile_path = repo_path / "deploy" / "profile.yaml"
    try:
        profile = load_deploy_profile(profile_path)
    except DeployProfileError as exc:
        return 400, {"error": f"target repo {repo!r} is not deployable: {exc}"}

    # LAW 2 (part b) — refuse any script the profile does not name.
    if not isinstance(script, str) or not script.strip():
        return 400, {
            "error": (
                "'script' is required (a script named in the target's "
                "deploy/profile.yaml)"
            )
        }
    permitted = allowed_scripts(profile)
    if script not in permitted:
        names = ", ".join(sorted(permitted)) or (
            "(none — the profile names no runnable scripts)"
        )
        return 400, {
            "error": (
                f"script {script!r} is not named in {repo}'s "
                f"deploy/profile.yaml — deny by default. Runnable scripts: "
                f"{names}"
            )
        }

    # LAW 3 — env keys allowlisted, values must be strings.
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict):
        return 400, {
            "error": "'env' must be a JSON object of allowlisted string values"
        }
    permitted_keys = allowed_env_keys(profile)
    extra_env: dict[str, str] = {}
    for key, value in raw_env.items():
        if key not in permitted_keys:
            names = ", ".join(sorted(permitted_keys))
            return 400, {
                "error": (
                    f"env key {key!r} is not allowlisted — deny by default. "
                    f"Allowed: {names}"
                )
            }
        if not isinstance(value, str):
            return 400, {
                "error": (
                    f"env value for {key!r} must be a string, got "
                    f"{type(value).__name__}"
                )
            }
        extra_env[key] = value

    # LAW 4 — timeout cap.
    timeout = TIMEOUT_DEFAULT
    if timeout_seconds is not None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            return 400, {"error": "'timeout_seconds' must be a positive number"}
        timeout = min(float(timeout_seconds), TIMEOUT_MAX)

    # ENV_FILE is routed through the dedicated _run_script_step param (its
    # documented purpose); anything else rides extra_env.
    env_file = extra_env.pop("ENV_FILE", None)
    cwd = _resolve_cwd(repo_path, profile)

    # LAW 8 — a candidate tree named by the caller is the working directory;
    # anything else the caller names is ignored in favour of the profile's.
    candidate_cwd, cwd_error = _resolve_requested_cwd(repo_path, payload.get("cwd"))
    if cwd_error is not None:
        return 400, {"error": cwd_error}
    if candidate_cwd is not None:
        cwd = candidate_cwd

    # LAW 6 — execute through the shared subprocess core, no shell. The runner
    # itself never raises, but we still fence it so a stub/HTTP-layer surprise
    # cannot take the process down.
    try:
        exit_code, output = script_runner(
            cwd=str(cwd),
            script=script,
            env_file=env_file,
            timeout=timeout,
            extra_env=extra_env or None,
        )
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {
            "error": f"sidecar execution error: {type(exc).__name__}: {exc}",
            "exit_code": 1,
            "output_tail": "",
        }

    # LAW 8, the other half: say where the script ran, so a caller that named
    # a candidate tree can tell it was honoured.
    return 200, {"exit_code": exit_code, "output_tail": _tail(output), "cwd": str(cwd)}


# ---------------------------------------------------------------------------
# The merge operation — one fixed command, run where the builds run
# ---------------------------------------------------------------------------


def resolve_guardkit_command() -> str | None:
    """Return the path of the ``guardkit`` command, or ``None`` if there is none.

    Two rungs, the same two the build runner walks:

    1. the ``FORGE_GUARDKIT_PATH`` setting, when it names a file that can be
       run;
    2. a lookup of ``guardkit`` on PATH — the sidecar unit's PATH puts
       ``~/.agentecflow/bin`` first, which is where the host's guardkit lives.

    A setting that names something unusable is reported in the log and the PATH
    lookup is tried anyway, so a stale setting cannot stop the merge on its own.
    """
    override = os.environ.get(GUARDKIT_PATH_ENV, "").strip()
    if override:
        candidate = os.path.abspath(os.path.expanduser(override))
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        logger.warning(
            "forge-deploy-sidecar: %s=%r does not name a file this user can "
            "run — looking for %r on PATH instead",
            GUARDKIT_PATH_ENV,
            override,
            GUARDKIT_BINARY_NAME,
        )
    found = shutil.which(GUARDKIT_BINARY_NAME)
    return os.path.abspath(found) if found else None


def _kill_process_group(process: "subprocess.Popen[bytes]") -> None:
    """Stop the command AND everything it started. Never raises.

    A test run starts children of its own; killing only the command we spawned
    would leave those children holding the output pipes open, and the read that
    follows would never end. So the whole process group is stopped politely,
    then killed outright if it is still there after the grace window.
    """
    try:
        group = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        return
    for sig, wait_for in (
        (signal.SIGTERM, MERGE_KILL_GRACE_SECONDS),
        (signal.SIGKILL, 0.0),
    ):
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        if wait_for <= 0:
            return
        try:
            process.wait(timeout=wait_for)
            return
        except subprocess.TimeoutExpired:
            continue


def run_merge_command(
    *,
    argv: list[str],
    cwd: str,
    timeout: float = MERGE_TIMEOUT_DEFAULT,
) -> tuple[int, str, str]:
    """Run one fixed argument list with no shell; return exit code and output.

    The command is started in a session of its own so a timeout can stop the
    whole process group (see :func:`_kill_process_group`). Output is captured
    separately — the caller needs the report on stdout intact — decoded, and
    passed through the same credential scrub the deploy scripts already use.

    Never raises: a command that cannot be started comes back as a non-zero
    exit code with a plain sentence saying so.
    """
    try:
        process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return (
            MERGE_NOT_STARTED_EXIT_CODE,
            "",
            f"the merge command could not be started: {exc}",
        )
    except NotADirectoryError as exc:
        return (
            MERGE_NOT_STARTED_EXIT_CODE,
            "",
            f"the merge command could not be started: {exc}",
        )
    except PermissionError as exc:
        return (126, "", f"the merge command could not be run: {exc}")
    except OSError as exc:
        return (1, "", f"the merge command could not be started: {exc}")

    timed_out = False
    try:
        raw_out, raw_err = process.communicate(timeout=timeout)
        exit_code = process.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        try:
            raw_out, raw_err = process.communicate(
                timeout=MERGE_POST_KILL_READ_SECONDS
            )
        except subprocess.TimeoutExpired:
            raw_out, raw_err = b"", b""
        exit_code = MERGE_TIMEOUT_EXIT_CODE

    stdout = scrub_process_output((raw_out or b"").decode("utf-8", errors="replace"))
    stderr = scrub_process_output((raw_err or b"").decode("utf-8", errors="replace"))
    if timed_out:
        stderr += (
            f"\nthe merge command was stopped after {timeout:g} seconds and "
            "everything it had started was stopped with it"
        )
    return exit_code, stdout, stderr


def _tail_chars(text: str, limit: int) -> str:
    """Return the last ``limit`` characters of ``text``, marked when trimmed."""
    if len(text) <= limit:
        return text
    return _TAIL_MARKER + text[-limit:]


def process_guardkit_merge_request(
    payload: Any,
    *,
    config: ForgeConfig,
    merge_runner: MergeRunner = run_merge_command,
    command_resolver: Callable[[], str | None] = resolve_guardkit_command,
) -> tuple[int, dict[str, Any]]:
    """Validate and run a ``/guardkit-merge`` payload; return ``(status, body)``.

    Everything is checked before a process starts: the repository must be one
    the forge configuration names, the feature name and the target commit must
    both be well formed, the timeout must be a positive number no larger than
    half an hour, and a pre-merge baseline, if one is sent, must be a list of
    test names. A refusal is a 4xx with one plain sentence saying what was
    wrong. A permitted run is a 200 carrying ``{exit_code, stdout, stderr_tail}``
    — the exit code is data, exactly as it is for a deploy script, because
    "merged but the checks failed" is an answer, not a transport failure.

    Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}

    # The repository must be one the forge configuration already names.
    repo = payload.get("repo")
    paths = config.planning.target_repo_paths
    if not isinstance(repo, str) or not repo.strip():
        return 400, {
            "error": (
                "'repo' is required (an org/name key from "
                "planning.target_repo_paths)"
            )
        }
    if repo not in paths:
        known = ", ".join(sorted(paths)) or "(none configured)"
        return 400, {
            "error": (
                f"unknown target repo {repo!r} — not in "
                f"planning.target_repo_paths. Known keys: {known}"
            )
        }
    repo_path = Path(paths[repo])

    # The feature name must have the shape the rest of the estate uses.
    feature_id = payload.get("feature_id")
    if not isinstance(feature_id, str) or not FEATURE_ID_PATTERN.match(feature_id):
        return 400, {
            "error": (
                f"'feature_id' must look like FEAT-ABC1 (the letters FEAT, a "
                f"dash, then three to twelve capitals or digits); got "
                f"{feature_id!r}"
            )
        }

    # The target commit must be a full git hash — a short one would let the
    # merge run against a branch that has moved since the checks ran.
    expect_main_sha = payload.get("expect_main_sha")
    if not isinstance(expect_main_sha, str) or not MAIN_SHA_PATTERN.match(
        expect_main_sha
    ):
        return 400, {
            "error": (
                "'expect_main_sha' must be a full forty-character commit hash; "
                f"got {expect_main_sha!r}"
            )
        }

    # The timeout must be a positive number, and no longer than the cap.
    timeout_seconds = payload.get("timeout_seconds")
    timeout = MERGE_TIMEOUT_DEFAULT
    if timeout_seconds is not None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            return 400, {"error": "'timeout_seconds' must be a positive number"}
        if float(timeout_seconds) > MERGE_TIMEOUT_MAX:
            return 400, {
                "error": (
                    f"'timeout_seconds' may not be longer than "
                    f"{MERGE_TIMEOUT_MAX:g} seconds; got {timeout_seconds}"
                )
            }
        timeout = float(timeout_seconds)

    # How long ONE run of the post-merge checks may take. It is separate from
    # the wall above: that one holds the whole command, this one holds each
    # check run inside it, and the merge command needs to be told it or the
    # checks fall back to guardkit's own default.
    verify_timeout_seconds = payload.get("verify_timeout_seconds")
    verify_timeout: int | None = None
    if verify_timeout_seconds is not None:
        if (
            isinstance(verify_timeout_seconds, bool)
            or not isinstance(verify_timeout_seconds, (int, float))
            or not float(verify_timeout_seconds).is_integer()
            or int(verify_timeout_seconds) < 1
        ):
            return 400, {
                "error": (
                    "'verify_timeout_seconds' must be a positive whole number "
                    "of seconds"
                )
            }
        if float(verify_timeout_seconds) > MERGE_TIMEOUT_MAX:
            return 400, {
                "error": (
                    f"'verify_timeout_seconds' may not be longer than "
                    f"{MERGE_TIMEOUT_MAX:g} seconds; got {verify_timeout_seconds}"
                )
            }
        verify_timeout = int(verify_timeout_seconds)

    # A pre-merge baseline, when one is sent, is a list of test names.
    baseline_failing = payload.get("baseline_failing")
    if baseline_failing is not None:
        if not isinstance(baseline_failing, list):
            return 400, {
                "error": (
                    "'baseline_failing' must be a list of test names; got "
                    f"{type(baseline_failing).__name__}"
                )
            }
        for entry in baseline_failing:
            if not isinstance(entry, str):
                return 400, {
                    "error": (
                        "every entry in 'baseline_failing' must be a test name "
                        f"written as text; got {type(entry).__name__}"
                    )
                }

    command = command_resolver()
    if not command:
        return 500, {
            "error": (
                "this host has no guardkit command to run — set "
                f"{GUARDKIT_PATH_ENV} to its path, or put {GUARDKIT_BINARY_NAME} "
                "on the service's PATH"
            )
        }

    argv = [
        command,
        "autobuild",
        "merge",
        feature_id,
        "--target",
        "main",
        "--expect-main-sha",
        expect_main_sha,
        "--json",
    ]
    if verify_timeout is not None:
        argv += ["--verify-timeout", str(verify_timeout)]

    # The sidecar writes its OWN copy of the baseline: the caller's file lives
    # inside the forge container and is not on this host at all. Failing to
    # write it stops the run, because a merge that quietly loses its baseline
    # would blame the feature for tests that were already red.
    if baseline_failing is not None:
        baseline_path = repo_path.joinpath(*MERGE_BASELINE_DIR) / (
            f"merge-baseline-{feature_id}.json"
        )
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(
                json.dumps(
                    {"failing_node_ids": list(baseline_failing)}, indent=2
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            return 500, {
                "error": (
                    "the sidecar could not write the list of tests that were "
                    f"already failing to {baseline_path}: {exc}"
                )
            }
        argv += ["--baseline-json", str(baseline_path)]

    logger.info(
        "forge-deploy-sidecar: running the merge word's checks for %s in %s "
        "(up to %g seconds)",
        feature_id,
        repo_path,
        timeout,
    )
    try:
        exit_code, stdout, stderr = merge_runner(
            argv=argv, cwd=str(repo_path), timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {
            "error": f"sidecar execution error: {type(exc).__name__}: {exc}",
            "exit_code": 1,
            "stdout": "",
            "stderr_tail": "",
        }

    return 200, {
        "exit_code": exit_code,
        "stdout": _tail_chars(stdout, MERGE_STDOUT_CHARS),
        "stderr_tail": _tail_chars(stderr, MERGE_STDERR_TAIL_CHARS),
    }


# ---------------------------------------------------------------------------
# The git operations — the planning chain's commits, made where the
# repository lives (sandbox first, 2026-09-07, rule 70)
#
# Rich's rule: nothing the factory runs on a repository runs on the host. The
# planning chain's commits (the spec, the plan) used to be made by forge's
# own git runner in a worktree of the operator's checkout, with the plan
# stage's pre-commit checks (the stamp normalizer, feature validate) run as a
# Python closure beside it. In the sandbox, the sidecar makes those commits
# on the factory's clone instead: the caller declares the checks BY NAME with
# their arguments, the sidecar materialises the worktree exactly as the
# in-container runner does (the same class, imported), writes the files, runs
# each named check with the guardkit installed beside it, and commits only
# when every check that blocks has passed. The outcomes ride the answer so
# the caller reads them through the parsers it always used.
#
# LAW 9 (the git routes' own): the repository is the same key as everywhere
# else; a branch, a ref and every file path are shape-checked before git sees
# them; a check must be one of the three names below (nothing else runs, and
# ``classify-scenarios`` may never be declared blocking); a check is one
# fixed argument list through the same no-shell runner the merge uses.
# ---------------------------------------------------------------------------

#: The three routes.
GIT_WRITE_TREE_ROUTE: str = "/git/prepare-branch-and-write-tree"
GIT_READ_FILE_ROUTE: str = "/git/read-file-from-branch"
GIT_REV_PARSE_ROUTE: str = "/git/rev-parse"

#: The checks the sidecar knows how to run — the closed list.
GIT_CHECK_NAMES: tuple[str, ...] = PRE_COMMIT_CHECK_NAMES

#: Each check's own time limit when the caller names none: the normalizer
#: and the provability check are rules over a handful of files (seconds); a
#: feature validate reads a whole plan tree (the oracle's usual ten minutes).
GIT_CHECK_TIMEOUT_DEFAULTS: dict[str, float] = {
    "normalize-stamps": 120.0,
    "feature-validate": 600.0,
    "classify-scenarios": 120.0,
}

#: Whether a check blocks the commit when the caller does not say: the two
#: the driver's own hook stops on, and never the provability check.
GIT_CHECK_BLOCKING_DEFAULTS: dict[str, bool] = {
    "normalize-stamps": True,
    "feature-validate": True,
    "classify-scenarios": False,
}

#: Ceiling on the whole pre-commit step (every check together) — the same
#: ceiling the in-container runner puts on its closure.
GIT_HOOK_TIMEOUT_SECONDS: float = 900.0

#: How long a ``rev-parse`` may take.
GIT_REV_PARSE_TIMEOUT_SECONDS: float = 30.0

#: The shape a branch name or a ref must have before git sees it: it starts
#: with a letter or digit (never a dash, so it can never be read as an
#: option), and carries only the characters branch names and revisions use.
REF_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*(\^\{[a-z]+\})?$")


@dataclass(frozen=True)
class _DeclaredCheck:
    """One check as the request declared it, after validation."""

    name: str
    args: dict[str, Any]
    blocking: bool
    timeout: float


def _resolve_repo_key(
    payload: dict[str, Any], config: ForgeConfig
) -> tuple[Path | None, str | None]:
    """LAW 1 for the git routes — ``(repo_path, None)`` or ``(None, error)``."""
    repo = payload.get("repo")
    paths = config.planning.target_repo_paths
    if not isinstance(repo, str) or not repo.strip():
        return None, (
            "'repo' is required (an org/name key from planning.target_repo_paths)"
        )
    if repo not in paths:
        known = ", ".join(sorted(paths)) or "(none configured)"
        return None, (
            f"unknown target repo {repo!r} — not in planning.target_repo_paths. "
            f"Known keys: {known}"
        )
    return Path(paths[repo]), None


def _ref_error(value: Any, *, what: str) -> str | None:
    """A plain sentence when ``value`` is not a usable branch name or ref."""
    if not isinstance(value, str) or not value.strip():
        return f"'{what}' is required (a branch name or a commit)"
    if (
        not REF_NAME_PATTERN.match(value)
        or ".." in value
        or "//" in value
        or value.endswith("/")
        or value.endswith(".lock")
    ):
        return (
            f"'{what}' {value!r} is not a branch name or a commit the sidecar "
            "will pass to git (letters, digits, dots, dashes and slashes, not "
            "starting with a dash)"
        )
    return None


def _relative_path_error(value: Any, *, what: str) -> str | None:
    """A plain sentence when ``value`` is not a relative path inside the tree."""
    if not isinstance(value, str) or not value.strip():
        return f"'{what}' must be a relative path inside the repository"
    if value.startswith("/") or "\\" in value:
        return f"'{what}' {value!r} must be a relative path inside the repository"
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return (
            f"'{what}' {value!r} must be a relative path inside the repository "
            "(no '..', no '.', no empty segments)"
        )
    return None


def _validate_files(raw: Any) -> tuple[dict[str, str] | None, str | None]:
    """The files to write: a non-empty object of relative path → text."""
    if not isinstance(raw, dict) or not raw:
        return None, "'files' must be a non-empty JSON object of relative path → text"
    files: dict[str, str] = {}
    for rel, content in raw.items():
        error = _relative_path_error(rel, what="files key")
        if error:
            return None, error
        if not isinstance(content, str):
            return None, (
                f"the content of {rel!r} must be text, got {type(content).__name__}"
            )
        files[rel] = content
    return files, None


def _parse_checks(raw: Any) -> tuple[list[_DeclaredCheck] | None, str | None]:
    """The declared checks, validated name by name (LAW 9)."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "'checks' must be a list of {name, args, blocking} objects"
    checks: list[_DeclaredCheck] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"checks[{index}] must be an object with a 'name'"
        name = item.get("name")
        if name not in GIT_CHECK_NAMES:
            return None, (
                f"checks[{index}] names {name!r}, which the sidecar does not run "
                f"— the checks it runs are: {', '.join(GIT_CHECK_NAMES)}"
            )
        args = item.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return None, f"checks[{index}] ({name}): 'args' must be an object"
        blocking_raw = item.get("blocking")
        if blocking_raw is None:
            blocking = GIT_CHECK_BLOCKING_DEFAULTS[name]
        elif isinstance(blocking_raw, bool):
            blocking = blocking_raw
        else:
            return None, f"checks[{index}] ({name}): 'blocking' must be true or false"
        if name == "classify-scenarios" and blocking:
            return None, (
                "classify-scenarios never blocks a commit — declare it without "
                "'blocking'"
            )
        timeout_raw = item.get("timeout_seconds")
        timeout = GIT_CHECK_TIMEOUT_DEFAULTS[name]
        if timeout_raw is not None:
            if (
                isinstance(timeout_raw, bool)
                or not isinstance(timeout_raw, (int, float))
                or timeout_raw <= 0
                or float(timeout_raw) > TIMEOUT_MAX
            ):
                return None, (
                    f"checks[{index}] ({name}): 'timeout_seconds' must be a "
                    f"positive number no larger than {TIMEOUT_MAX:g}"
                )
            timeout = float(timeout_raw)
        clean: dict[str, Any]
        if name in ("normalize-stamps", "feature-validate"):
            feature_id = args.get("feature_id")
            if not isinstance(feature_id, str) or not FEATURE_ID_PATTERN.match(feature_id):
                return None, (
                    f"checks[{index}] ({name}): args.feature_id must look like "
                    f"FEAT-ABC1; got {feature_id!r}"
                )
            clean = {"feature_id": feature_id}
            allowed = {"feature_id"}
            if name == "normalize-stamps":
                no_model = args.get("no_model", False)
                if not isinstance(no_model, bool):
                    return None, (
                        f"checks[{index}] (normalize-stamps): args.no_model must "
                        "be true or false"
                    )
                clean["no_model"] = no_model
                allowed.add("no_model")
        else:
            error = _relative_path_error(
                args.get("feature_file"), what=f"checks[{index}] args.feature_file"
            )
            if error:
                return None, error
            clean = {"feature_file": str(args["feature_file"])}
            allowed = {"feature_file"}
        extra = sorted(set(args) - allowed)
        if extra:
            return None, (
                f"checks[{index}] ({name}) carries arguments the check does not "
                f"take: {', '.join(extra)}"
            )
        checks.append(
            _DeclaredCheck(name=name, args=clean, blocking=blocking, timeout=timeout)
        )
    return checks, None


def resolve_check_command(
    *,
    command_resolver: Callable[[], str | None] = resolve_guardkit_command,
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
    python_executable: str = sys.executable,
) -> tuple[str, ...] | None:
    """The guardkit command the checks run, or ``None`` when there is none.

    Three rungs: the ``FORGE_GUARDKIT_PATH`` setting and a ``guardkit`` on
    PATH (the same two the merge walks, through :func:`resolve_guardkit_command`),
    then the module form ``python -m guardkit.cli.main`` when guardkit is
    importable by the interpreter the sidecar runs under — the way the
    planning tools resolve their own normalizer, so a sandbox whose guardkit
    is installed in the sidecar's venv but not on the unit's PATH still has
    its command.
    """
    found = command_resolver()
    if found:
        return (found,)
    try:
        spec = find_spec("guardkit.cli.main")
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec is not None:
        return (python_executable, "-m", "guardkit.cli.main")
    return None


def _check_outcome(
    check: _DeclaredCheck,
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    passed: bool,
    detail: str,
    note: str = "",
) -> PreCommitCheckOutcome:
    return PreCommitCheckOutcome(
        name=check.name,
        blocking=check.blocking,
        ran=True,
        passed=passed,
        exit_code=exit_code,
        stdout=_tail_chars(stdout, MERGE_STDOUT_CHARS),
        stderr_tail=_tail_chars(stderr, MERGE_STDERR_TAIL_CHARS),
        timed_out=exit_code == MERGE_TIMEOUT_EXIT_CODE,
        detail=detail,
        note=note,
    )


def run_declared_check(
    check: _DeclaredCheck,
    *,
    worktree: Path,
    command: tuple[str, ...],
    check_runner: MergeRunner = run_merge_command,
) -> PreCommitCheckOutcome:
    """Run one declared check in ``worktree`` and judge it the way the
    driver's closure judged it.

    * ``normalize-stamps`` — the argv :func:`make_normalize_stamps` builds;
      when ``--no-model`` was asked for and the installed guardkit has no such
      option, it is run again without it and the outcome's ``note`` says so
      (the same second run, the same sentence). Passed means the outcome is
      not a failure — written, nothing to do, or unavailable — read with
      :func:`classify_normalizer_check`, the parser the driver reads it with.
    * ``feature-validate`` — the task documents' front matter is repaired
      first (the closure's own pre-oracle repair; the receipt rides ``note``),
      then ``guardkit feature validate <id> --json``; passed means exit 0.
    * ``classify-scenarios`` — ``guardkit qa classify-scenarios``; its verdict
      is exit 0, and it never blocks.

    Never raises: a runner that blows up is a failed check with the reason.
    """
    from forge.planning.target_terminal_tools import (
        NO_MODEL_OPTION_UNKNOWN_NOTE,
        _NORMALIZER_NO_MODEL_UNKNOWN_RE,
        classify_normalizer_check,
        classify_scenarios_check,
        repair_plan_task_frontmatter,
        validate_feature_plan_check,
    )

    def _run(argv: list[str]) -> tuple[int, str, str]:
        return check_runner(argv=argv, cwd=str(worktree), timeout=check.timeout)

    try:
        if check.name == "normalize-stamps":
            feature_id = str(check.args["feature_id"])
            no_model = bool(check.args.get("no_model"))
            argv = [
                *command,
                "qa",
                "normalize-stamps",
                "--feature",
                feature_id,
                "--repo",
                str(worktree),
            ]
            note = ""
            exit_code, stdout, stderr = _run(argv + (["--no-model"] if no_model else []))
            if (
                no_model
                and exit_code != 0
                and _NORMALIZER_NO_MODEL_UNKNOWN_RE.search(stderr or "")
            ):
                note = NO_MODEL_OPTION_UNKNOWN_NOTE
                logger.warning(
                    "forge-deploy-sidecar: normalize-stamps for %s — %s", feature_id, note
                )
                exit_code, stdout, stderr = _run(argv)
            outcome = classify_normalizer_check(
                feature_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=exit_code == MERGE_TIMEOUT_EXIT_CODE,
            )
            return _check_outcome(
                check,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                passed=not outcome.is_failure,
                detail=f"stamp normalizer {outcome.status}: {outcome.detail}",
                note=note,
            )
        if check.name == "feature-validate":
            feature_id = str(check.args["feature_id"])
            repair = repair_plan_task_frontmatter(worktree, feature_id)
            note = repair.receipt(feature_id) if repair.fired else ""
            if note:
                logger.warning("forge-deploy-sidecar: feature-validate: %s", note)
            exit_code, stdout, stderr = _run(
                [*command, "feature", "validate", feature_id, "--json"]
            )
            outcome = validate_feature_plan_check(
                feature_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=exit_code == MERGE_TIMEOUT_EXIT_CODE,
                note=note,
            )
            return _check_outcome(
                check,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                passed=outcome.ok,
                detail=outcome.detail,
                note=note,
            )
        feature_file = worktree / str(check.args["feature_file"])
        exit_code, stdout, stderr = _run(
            [
                *command,
                "qa",
                "classify-scenarios",
                "--feature-file",
                str(feature_file),
                "--repo",
                str(worktree),
                "--json",
            ]
        )
        outcome = classify_scenarios_check(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=exit_code == MERGE_TIMEOUT_EXIT_CODE,
        )
        return _check_outcome(
            check,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            passed=exit_code == 0 and outcome.status == "checked",
            detail=outcome.detail,
        )
    except Exception as exc:  # noqa: BLE001 — never raise past the check
        logger.exception("forge-deploy-sidecar: check %s raised", check.name)
        return PreCommitCheckOutcome(
            name=check.name,
            blocking=check.blocking,
            ran=True,
            passed=False,
            exit_code=1,
            detail=f"the check could not be run: {type(exc).__name__}: {exc}",
        )


def _not_run(check: _DeclaredCheck) -> PreCommitCheckOutcome:
    return PreCommitCheckOutcome(
        name=check.name,
        blocking=check.blocking,
        ran=False,
        passed=False,
        exit_code=-1,
        detail="not run: an earlier check refused the commit",
    )


def _declared_checks_hook(
    checks: list[_DeclaredCheck],
    *,
    command: tuple[str, ...],
    check_runner: MergeRunner,
    outcomes: list[PreCommitCheckOutcome],
) -> Callable[[Path], Awaitable[PreCommitResult]]:
    """The pre-commit hook the in-container runner takes, built from the
    declaration: each check in order, in a worker thread (the runner is
    async, the checks are subprocesses); the first blocking failure refuses
    the commit and the rest are reported as not run."""

    async def _hook(worktree: Path) -> PreCommitResult:
        for index, check in enumerate(checks):
            outcome = await asyncio.to_thread(
                run_declared_check,
                check,
                worktree=worktree,
                command=command,
                check_runner=check_runner,
            )
            outcomes.append(outcome)
            if check.blocking and not outcome.passed:
                outcomes.extend(_not_run(rest) for rest in checks[index + 1 :])
                return PreCommitResult(ok=False, detail=outcome.detail)
        return PreCommitResult(ok=True)

    return _hook


def _run_coroutine(coro: Awaitable[Any]) -> Any:
    """Run ``coro`` to completion from synchronous code.

    The request handlers are plain threads with no event loop, so
    :func:`asyncio.run` is the ordinary path; when a loop is already running
    in this thread (a test calling the core from async code) the coroutine
    runs on a fresh thread of its own instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()  # type: ignore[arg-type]


def _git_runner(worktrees_root: Path | None) -> Any:
    """The in-container planning runner, imported — the sidecar makes its
    worktrees exactly the way forge's own runner does, never a second way."""
    from forge.adapters.git.planning_runner import WorktreeGitRunner

    return WorktreeGitRunner(
        worktrees_root=worktrees_root, hook_timeout_s=GIT_HOOK_TIMEOUT_SECONDS
    )


def process_git_write_tree_request(
    payload: Any,
    *,
    config: ForgeConfig,
    check_runner: MergeRunner = run_merge_command,
    command_resolver: Callable[[], tuple[str, ...] | None] = resolve_check_command,
    worktrees_root: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """Validate and perform a ``/git/prepare-branch-and-write-tree`` payload.

    ``{repo, branch, files, message, checks}`` → on a permitted request a 200
    carrying ``{status, sha, checks, detail}``: ``status`` is the runner's
    (``success`` with the commit's ``sha``, or ``failed`` with ``detail``
    saying why — a check that refused the commit is a ``failed`` with the
    checks' outcomes beside it, data rather than a transport error); every
    declared check appears in ``checks`` in order, run or not. A refusal of
    the request itself is a 4xx with one plain sentence; a sidecar with no
    guardkit to run the checks is a 500 saying so, before any worktree is
    made. Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    branch = payload.get("branch")
    error = _ref_error(branch, what="branch")
    if error:
        return 400, {"error": error}
    files, error = _validate_files(payload.get("files"))
    if error or files is None:
        return 400, {"error": error}
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return 400, {"error": "'message' is required (the commit message)"}
    checks, error = _parse_checks(payload.get("checks"))
    if error or checks is None:
        return 400, {"error": error}

    command: tuple[str, ...] | None = None
    if checks:
        command = command_resolver()
        if not command:
            return 500, {
                "error": (
                    "this sidecar has no guardkit command to run the declared "
                    f"checks with — set {GUARDKIT_PATH_ENV} to its path, put "
                    f"{GUARDKIT_BINARY_NAME} on the service's PATH, or install "
                    "guardkit beside the sidecar"
                )
            }

    outcomes: list[PreCommitCheckOutcome] = []
    hook = (
        _declared_checks_hook(
            checks, command=command, check_runner=check_runner, outcomes=outcomes
        )
        if checks and command
        else None
    )
    logger.info(
        "forge-deploy-sidecar: writing %d file(s) onto %s in %s with %d declared "
        "check(s): %s",
        len(files),
        branch,
        repo_path,
        len(checks),
        ", ".join(c.name for c in checks) or "(none)",
    )
    try:
        result = _run_coroutine(
            _git_runner(worktrees_root).prepare_branch_and_write_tree(
                repo_path=str(repo_path),
                branch=str(branch),
                files=files,
                message=message,
                pre_commit=hook,
            )
        )
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {
            "error": f"sidecar git error: {type(exc).__name__}: {exc}",
            "status": "failed",
            "sha": None,
            "checks": [o.to_wire() for o in outcomes],
            "detail": "",
        }
    return 200, {
        "status": result.status,
        "sha": result.sha,
        "checks": [o.to_wire() for o in outcomes],
        "detail": result.stderr or "",
    }


def process_git_read_file_request(
    payload: Any,
    *,
    config: ForgeConfig,
    worktrees_root: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """``{repo, branch, file_path}`` → ``{content}`` (``null`` when the file
    is not on the branch), through the in-container runner's own read.
    Never raises."""
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    branch = payload.get("branch")
    error = _ref_error(branch, what="branch")
    if error:
        return 400, {"error": error}
    file_path = payload.get("file_path")
    error = _relative_path_error(file_path, what="file_path")
    if error:
        return 400, {"error": error}
    try:
        content = _run_coroutine(
            _git_runner(worktrees_root).read_file_from_branch(
                repo_path=str(repo_path), branch=str(branch), file_path=str(file_path)
            )
        )
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {"error": f"sidecar git error: {type(exc).__name__}: {exc}"}
    return 200, {"content": content}


def process_git_rev_parse_request(
    payload: Any, *, config: ForgeConfig
) -> tuple[int, dict[str, Any]]:
    """``{repo, ref}`` → ``{sha}`` (``null`` when the ref names no commit):
    ``git rev-parse --verify --quiet <ref>^{commit}``, one fixed argument
    list, no shell. Never raises."""
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    ref = payload.get("ref")
    error = _ref_error(ref, what="ref")
    if error:
        return 400, {"error": error}
    spec = str(ref) if str(ref).endswith("}") else f"{ref}^{{commit}}"
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", spec],
            capture_output=True,
            text=True,
            timeout=GIT_REV_PARSE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 500, {"error": f"sidecar git error: {type(exc).__name__}: {exc}"}
    sha = result.stdout.strip() if result.returncode == 0 else ""
    return 200, {"sha": sha or None}


# ---------------------------------------------------------------------------
# Config resolution (re-read per request so path changes are picked up)
# ---------------------------------------------------------------------------


ConfigLoader = Callable[[], ForgeConfig]


def default_config_loader() -> ForgeConfig:
    """Load the forge config the sidecar validates ``repo`` keys against.

    Reads ``FORGE_CONFIG_PATH`` (the systemd unit sets it), else ``./forge.yaml``.
    Raises :class:`SidecarConfigError` when neither is present.
    """
    env_path = os.environ.get("FORGE_CONFIG_PATH")
    if env_path:
        return load_config(Path(env_path))
    default = Path("forge.yaml")
    if default.exists():
        return load_config(default)
    raise SidecarConfigError(
        "no forge config: set FORGE_CONFIG_PATH or run from a directory that "
        "ships forge.yaml"
    )


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _SidecarServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer carrying the injected loader + runner seams."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        config_loader: ConfigLoader,
        script_runner: ScriptRunner,
        merge_runner: MergeRunner = run_merge_command,
        check_runner: MergeRunner = run_merge_command,
        worktrees_root: Path | None = None,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.config_loader = config_loader
        self.script_runner = script_runner
        self.merge_runner = merge_runner
        # The git routes' seams: the subprocess core the declared checks run
        # through, and where the worktrees are made (None = the runner's own
        # default under the temp directory).
        self.check_runner = check_runner
        self.worktrees_root = worktrees_root


class DeploySidecarHandler(BaseHTTPRequestHandler):
    """The request handler. Every path is fenced so a request cannot crash the
    server (never-raises posture) — an internal error becomes an honest 500."""

    server_version = "forge-deploy-sidecar/1"

    # Route logging through the module logger instead of stderr.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        logger.info("sidecar %s - %s", self.address_string(), fmt % args)

    def _write_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        try:
            if self.path.split("?", 1)[0] == "/healthz":
                self._write_json(
                    200, {"status": "healthy", "rev": SIDECAR_CODE_VERSION}
                )
                return
            self._write_json(404, {"error": f"no such path: {self.path}"})
        except Exception as exc:  # noqa: BLE001 — never crash the server
            logger.exception("sidecar GET handler error")
            self._safe_500(exc)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        try:
            route = self.path.split("?", 1)[0]
            if route not in (
                "/run",
                "/guardkit-merge",
                GIT_WRITE_TREE_ROUTE,
                GIT_READ_FILE_ROUTE,
                GIT_REV_PARSE_ROUTE,
            ):
                self._write_json(404, {"error": f"no such path: {self.path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._write_json(400, {"error": f"invalid JSON body: {exc}"})
                return
            try:
                config = self.server.config_loader()  # type: ignore[attr-defined]
            except SidecarConfigError as exc:
                self._write_json(500, {"error": str(exc)})
                return
            if route == "/guardkit-merge":
                status, body = process_guardkit_merge_request(
                    payload,
                    config=config,
                    merge_runner=self.server.merge_runner,  # type: ignore[attr-defined]
                )
            elif route == GIT_WRITE_TREE_ROUTE:
                status, body = process_git_write_tree_request(
                    payload,
                    config=config,
                    check_runner=self.server.check_runner,  # type: ignore[attr-defined]
                    worktrees_root=self.server.worktrees_root,  # type: ignore[attr-defined]
                )
            elif route == GIT_READ_FILE_ROUTE:
                status, body = process_git_read_file_request(
                    payload,
                    config=config,
                    worktrees_root=self.server.worktrees_root,  # type: ignore[attr-defined]
                )
            elif route == GIT_REV_PARSE_ROUTE:
                status, body = process_git_rev_parse_request(payload, config=config)
            else:
                status, body = process_run_request(
                    payload,
                    config=config,
                    script_runner=self.server.script_runner,  # type: ignore[attr-defined]
                )
            self._write_json(status, body)
        except Exception as exc:  # noqa: BLE001 — never crash the server
            logger.exception("sidecar POST handler error")
            self._safe_500(exc)

    def _safe_500(self, exc: Exception) -> None:
        try:
            self._write_json(
                500, {"error": f"internal error: {type(exc).__name__}: {exc}"}
            )
        except Exception:  # noqa: BLE001 — headers may already be sent
            pass


def build_server(
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    config_loader: ConfigLoader = default_config_loader,
    script_runner: ScriptRunner = _run_script_step,
    merge_runner: MergeRunner = run_merge_command,
    check_runner: MergeRunner = run_merge_command,
    worktrees_root: Path | None = None,
) -> _SidecarServer:
    """Build (but do not start) the loopback-only sidecar HTTP server.

    ``host`` defaults to the loopback constant (LAW 5). Tests pass ``port=0`` to
    claim an ephemeral port and assert the bound address is loopback.
    ``check_runner`` and ``worktrees_root`` are the git routes' seams.
    """
    return _SidecarServer(
        (host, port),
        DeploySidecarHandler,
        config_loader=config_loader,
        script_runner=script_runner,
        merge_runner=merge_runner,
        check_runner=check_runner,
        worktrees_root=worktrees_root,
    )


def serve(
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    config_loader: ConfigLoader = default_config_loader,
    script_runner: ScriptRunner = _run_script_step,
    merge_runner: MergeRunner = run_merge_command,
    check_runner: MergeRunner = run_merge_command,
) -> None:
    """Run the sidecar forever (the ``python -m forge.deploy_sidecar`` body)."""
    logging.basicConfig(level=logging.INFO)
    server = build_server(
        host=host,
        port=port,
        config_loader=config_loader,
        script_runner=script_runner,
        merge_runner=merge_runner,
        check_runner=check_runner,
    )
    bound_host, bound_port = server.server_address[:2]
    logger.info(
        "forge-deploy-sidecar: import-time code version stamp rev=%s "
        "(boot-visible staleness signal)",
        SIDECAR_CODE_VERSION,
    )
    logger.info(
        "forge-deploy-sidecar listening on http://%s:%s (loopback-only)",
        bound_host,
        bound_port,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = [
    "HOST",
    "DEFAULT_PORT",
    "TIMEOUT_DEFAULT",
    "TIMEOUT_MAX",
    "ENV_ALLOWLIST_BASE",
    "OUTPUT_TAIL_CHARS",
    "SIDECAR_CODE_VERSION",
    "MERGE_TIMEOUT_DEFAULT",
    "MERGE_TIMEOUT_MAX",
    "MERGE_TIMEOUT_EXIT_CODE",
    "MERGE_NOT_STARTED_EXIT_CODE",
    "MERGE_STDOUT_CHARS",
    "MERGE_STDERR_TAIL_CHARS",
    "FEATURE_ID_PATTERN",
    "MAIN_SHA_PATTERN",
    "GUARDKIT_PATH_ENV",
    "GUARDKIT_BINARY_NAME",
    "ScriptRunner",
    "MergeRunner",
    "SidecarConfigError",
    "ConfigLoader",
    "resolve_code_version",
    "resolve_guardkit_command",
    "run_merge_command",
    "allowed_scripts",
    "allowed_env_keys",
    "process_run_request",
    "process_guardkit_merge_request",
    "GIT_WRITE_TREE_ROUTE",
    "GIT_READ_FILE_ROUTE",
    "GIT_REV_PARSE_ROUTE",
    "GIT_CHECK_NAMES",
    "GIT_CHECK_TIMEOUT_DEFAULTS",
    "GIT_CHECK_BLOCKING_DEFAULTS",
    "GIT_HOOK_TIMEOUT_SECONDS",
    "REF_NAME_PATTERN",
    "resolve_check_command",
    "run_declared_check",
    "process_git_write_tree_request",
    "process_git_read_file_request",
    "process_git_rev_parse_request",
    "default_config_loader",
    "DeploySidecarHandler",
    "build_server",
    "serve",
]
