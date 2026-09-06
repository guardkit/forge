"""The forge-deploy-sidecar service (S1, C4 residue #24).

A small, loopback-only HTTP service that executes the *vetted, profile-named*
deploy scripts for a target repo. It exists because the forge container
deliberately has no docker access (C4 catch #24): the sidecar runs on Rich's
box, as Rich's user, and adds **zero new privilege** — it can only run scripts
the target repo's own ``deploy/profile.yaml`` already names (deny by default).

Design of record: docs/factory-deploy-execution-surface-design-2026-07-16.md §1.

The narrow contract:

    GET  /healthz -> {"status": "healthy", "rev": "git-<sha>"}
    POST /run  {repo, script, env, timeout_seconds}
              -> {exit_code, output_tail}
    POST /guardkit-merge  {repo, feature_id, expect_main_sha, baseline_failing,
                           timeout_seconds, verify_timeout_seconds}
              -> {exit_code, stdout, stderr_tail}

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

Each request-processing core (:func:`process_run_request` and
:func:`process_guardkit_merge_request`) is a pure function
``(payload, config, runner) -> (http_status, body)`` so every law is
unit-testable without a live socket. Neither **ever raises** past its boundary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol

from forge.config.loader import load_config
from forge.config.models import ForgeConfig
from forge.deploy.profile import (
    DeployProfile,
    DeployProfileError,
    load_deploy_profile,
)
from forge.executor.shell_steps import _run_script_step
from forge.memory.redaction import scrub_process_output

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
    error, and a 200 with ``{exit_code, output_tail}`` on a permitted run (the
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

    return 200, {"exit_code": exit_code, "output_tail": _tail(output)}


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
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.config_loader = config_loader
        self.script_runner = script_runner
        self.merge_runner = merge_runner


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
            if route not in ("/run", "/guardkit-merge"):
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
) -> _SidecarServer:
    """Build (but do not start) the loopback-only sidecar HTTP server.

    ``host`` defaults to the loopback constant (LAW 5). Tests pass ``port=0`` to
    claim an ephemeral port and assert the bound address is loopback.
    """
    return _SidecarServer(
        (host, port),
        DeploySidecarHandler,
        config_loader=config_loader,
        script_runner=script_runner,
        merge_runner=merge_runner,
    )


def serve(
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    config_loader: ConfigLoader = default_config_loader,
    script_runner: ScriptRunner = _run_script_step,
    merge_runner: MergeRunner = run_merge_command,
) -> None:
    """Run the sidecar forever (the ``python -m forge.deploy_sidecar`` body)."""
    logging.basicConfig(level=logging.INFO)
    server = build_server(
        host=host,
        port=port,
        config_loader=config_loader,
        script_runner=script_runner,
        merge_runner=merge_runner,
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
    "default_config_loader",
    "DeploySidecarHandler",
    "build_server",
    "serve",
]
