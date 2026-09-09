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
    POST /run  {repo, driver, args, env, timeout_seconds, cwd?}
              -> {exit_code, stdout, stderr_tail, timed_out, cwd, warnings}
    POST /run  {repo, declared_test, cwd, timeout_seconds}
              -> {exit_code, stdout, stderr_tail, timed_out, cwd, command,
                  warnings}
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
    POST /git/is-ancestor {repo, ancestor, descendant} -> {is_ancestor|null}
    POST /git/candidate-tree {repo, feature_id, sha}
              -> {path, tree, exclude_written}
    POST /git/candidate-tree-remove {repo, feature_id} -> {removed, path}
    POST /routing-stamps/evidence {repo, feature_id, worktree, branch}
              -> {feature_yaml, envelope, code_commit_time, history_dir}

The three routes after ``/git/rev-parse`` are the merge press's own git
(sandbox first, 2026-09-07, rule 89): its ancestry guards, and the lay-out and
removal of the branch's tree for the candidate check. Each derives the tree's
place from the repository its ``repo`` key names, so no path a caller sends
ever reaches git or the filesystem here. See the section above
:func:`process_git_is_ancestor_request`.

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
   SANDBOX_ALLOW_NETWORK, SANDBOX_SIDECAR_PUBLISH, SANDBOX_RUNNER_PUBLISH}``
   UNION the profile's ``live_gate.env`` and ``candidate.env`` key names;
   anything else is refused loudly — ``SANDBOX_ENV_FILE``, ``SANDBOX_FORGE_PATH``,
   ``SANDBOX_GUARDKIT_PATH`` and ``SANDBOX_RECEIPTS_PATH`` deliberately
   included, because each of them would let a request choose what a sandbox
   being created reads or mounts; the reason is written beside the list below.
   Values must be strings.
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
11. The two shapes of ``/run`` whose work is not a vetted script (sandbox
   first, rules 85 and 88) keep the same posture by a different route: the
   program comes from the repository, never from the message. ``driver`` must
   be exactly the argument list ``deploy/profile.yaml`` declares as the
   live-gate driver, and ``declared_test`` must be exactly the command
   ``.guardkit/config.yaml`` declares as the toolchain's test; anything else
   is refused in one plain sentence before a process starts. The declared
   test command's working directory must be one of this repository's own
   journey worktrees (LAW 10), and the live-gate driver's may be a candidate
   tree exactly as LAW 8 allows. Both, and ``/routing-stamps/evidence``
   beside them, are answered ONLY by a sidecar running inside a repository's
   sandbox (the bootstrap sets ``FORGE_SIDECAR_IN_SANDBOX``): they exist so a
   repository's own code and its own records are run and read where the
   repository lives, and a host sidecar refuses all three in one plain
   sentence saying where the request belongs.

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
from importlib import import_module
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence

from forge.adapters.guardkit.context_resolver import resolve_context_flags
from forge.config.loader import load_config
from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import candidate_trees_root, is_candidate_tree_path
from forge.deploy.profile import (
    DeployProfile,
    DeployProfileError,
    load_deploy_profile,
    wrapper_inner_script,
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
        # The settings that make a sandbox carry the factory's own two
        # services (2026-09-07/08). A profile's sandbox block can set six of
        # those, and the deploy stage threads every one it finds onto a
        # HOST-WRAPPER deploy, so this list has to know the harmless ones or
        # the first deploy of such a repository is refused at its first step —
        # which is exactly what happened on the first real merge press
        # (2026-09-09). Two of the six are here: they name the two ports the
        # sandbox publishes for those services, which is the same kind of
        # setting as SANDBOX_PUBLISH above — no secret and no new privilege.
        "SANDBOX_SIDECAR_PUBLISH",
        "SANDBOX_RUNNER_PUBLISH",
        # THE OTHER FOUR ARE DELIBERATELY NOT HERE, and this is the reason.
        # SANDBOX_ENV_FILE, SANDBOX_FORGE_PATH, SANDBOX_GUARDKIT_PATH and
        # SANDBOX_RECEIPTS_PATH each name somewhere on this box, and the
        # wrapper reads them only where it CREATES a sandbox: the env file
        # becomes that sandbox's whole environment (it is the sops-rendered
        # file of secrets), the forge and guardkit folders are mounted into it
        # read-only — and forge's parent folder decides three more mounts —
        # and the receipts folder is mounted READ-WRITE, the one writable
        # mount the wrapper makes. This service checks key NAMES and never
        # values, and the sandbox's name has always been the caller's to
        # choose, so a request naming a sandbox that does not exist yet takes
        # the wrapper's "create it" branch. Allowing these four would
        # therefore let one request decide which file on this box becomes a
        # new sandbox's environment and which folders that sandbox mounts —
        # including one it can write to — before it runs any code. That is a
        # widening of what a message can do, and deny-by-default says no.
        # Nothing needs them today: a deploy that runs inside the sandbox is
        # not sent the sandbox's creation settings at all
        # (forge.deploy.runbook_builder's sandbox_env), and creating a
        # factory-carrying sandbox is an attended host-side command, not a
        # request to this service. A repository that one day needs the wrapper
        # driven from here gets these keys by a decision, written down, rather
        # than by accident.
    }
)

#: Maximum characters of script output returned as ``output_tail``. The
#: _run_script_step core already byte-caps its capture; this trims to the TAIL
#: (the interesting end — the failure/last lines) for the wire response.
OUTPUT_TAIL_CHARS: int = 65_536

#: Truncation marker prepended when the tail drops leading output.
_TAIL_MARKER = "... [OUTPUT HEAD TRUNCATED] ...\n"


# --- the two answers /run gives when the work is not a vetted script --------
#
# SANDBOX FIRST (2026-09-07, rules 85 and 88). Two pieces of work that used to
# happen in the forge container have to happen where the repository lives: the
# live-gate driver (rule 85 — the candidate's port is on the sandbox's own
# loopback, not the host's) and the merge-ready gates reader's declared test
# command (rule 88 — for a sandbox repository neither the toolchain nor the
# journey worktree exists on the host, so the reader could only ever answer
# UNKNOWN, and a fix journey could never publish a merge card).
#
# Both ride ``POST /run``, and both keep LAW 2's posture exactly: THE PROGRAM
# COMES FROM THE REPOSITORY, NEVER FROM THE MESSAGE. The caller sends what it
# wants run, and this service checks it against the repository's own
# checked-in declaration — the driver against ``deploy/profile.yaml``'s
# ``live_gate.driver``, the test command against ``.guardkit/config.yaml``'s
# ``toolchain.test`` — and refuses anything else in one plain sentence. So a
# message can choose between the repository's own two declared commands and
# can name no other program at all.

#: How many extra argument tokens the live-gate driver may carry, and how long
#: one may be. The driver's own flags are few; these exist so a runaway caller
#: cannot hand the service an unbounded argument list.
DRIVER_MAX_ARGS: int = 32
DRIVER_MAX_ARG_CHARS: int = 4_096

#: Where a repository's toolchain declaration lives, and the two shapes the
#: estate installs guardkit in. The same candidates
#: :func:`forge.cli._serve_conductor.load_declared_toolchain` uses — resolved
#: here rather than imported so the sidecar never pulls the daemon's CLI in.
#: Both delegate to guardkit's OWN loader; neither parses the YAML itself.
TOOLCHAIN_MODULE_CANDIDATES: tuple[str, ...] = (
    "guardkit.orchestrator.toolchain_declaration",
    "orchestrator.toolchain_declaration",
)


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

    ``what`` names the command in a "could not be started" sentence, and
    ``extra_env`` is a non-secret overlay for the command's own environment.
    Both are optional: every caller that predates the live gate and the gates
    reader leaves them out, so a runner that does not accept them is still a
    runner.
    """

    def __call__(
        self,
        *,
        argv: list[str],
        cwd: str,
        timeout: float = ...,
        what: str = ...,
        extra_env: dict[str, str] | None = ...,
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


#: The environment value the in-sandbox bootstrap (``deploy_templates/
#: sandbox-runner.sh``) sets before it starts this service. It says one thing:
#: *this copy of the sidecar is running inside a repository's sandbox*.
SIDECAR_IN_SANDBOX_ENV: str = "FORGE_SIDECAR_IN_SANDBOX"

#: What counts as "yes" in that value.
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def sidecar_is_inside_sandbox(env: "dict[str, str] | None" = None) -> bool:
    """Is this sidecar the one inside a repository's sandbox?

    Read from the environment the bootstrap sets, and ``False`` whenever it is
    absent or says anything else — so the sidecar on the HOST, which is the
    one every repository without a sandbox still uses, never widens anything
    on the strength of a value nobody set.
    """
    source = os.environ if env is None else env
    return str(source.get(SIDECAR_IN_SANDBOX_ENV, "")).strip().lower() in _TRUTHY


def _not_inside_a_sandbox(what: str, *, verb: str = "run") -> str:
    """The refusal a HOST sidecar gives to the sandbox-only shapes.

    Written for whoever reads it in a log or a receipt: it says which sidecar
    answered, why it will not do this, and where the request should have gone.
    """
    return (
        "this sidecar is running on the host, not inside a repository's "
        f"sandbox, so it will not {verb} a repository's {what}. That is what the "
        "sidecar inside the repository's sandbox is for: a repository's own "
        "code runs where the repository lives, never on the host. Send this "
        "request to that sandbox's sidecar (its address is the repository's "
        "sidecar_url in planning.sandboxes)."
    )


def allowed_scripts(
    profile: DeployProfile, *, inside_sandbox: bool | None = None
) -> set[str]:
    """The ONLY scripts this profile permits the sidecar to run (LAW 2).

    ``compose.script`` + every ``health_checks[].cmd`` + the ``live_gate.driver``
    script path(s). If a driver argv names no path-like element (an odd shape),
    every element is allowlisted so a deliberately-vetted driver is not silently
    un-runnable — but only elements the profile itself names.
    """
    scripts: set[str] = set()
    if profile.compose.script:
        scripts.add(profile.compose.script)
        # SANDBOX FIRST (rule 85), AND ONLY INSIDE ONE (L3b's coach, 2026-09-08).
        # A profile that names a HOST sandbox wrapper (``deploy/
        # sandbox-deploy.sh``) names its inner script too, by the shared
        # template's fixed pairing: the wrapper's whole job is to put the
        # sandbox in place and then run ``deploy/deploy.sh`` inside it. The
        # sidecar INSIDE that sandbox cannot run the wrapper — it would ask
        # ``sbx`` for a sandbox from inside one — so the deploy stage sends it
        # the inner script and this allowlist has to name it.
        #
        # The sidecar on the HOST must NOT name it. There the wrapper is the
        # whole point: it is what puts the work inside a sandbox, and
        # permitting the inner script would let something ask the host sidecar
        # to run the repository's deploy straight against the host's Docker
        # engine — the wall Rich's rule of 2026-09-07 puts up. So the widening
        # is gated on the bootstrap's own flag, and a host sidecar's allowlist
        # is byte for byte what it was before this lane.
        if (
            sidecar_is_inside_sandbox()
            if inside_sandbox is None
            else bool(inside_sandbox)
        ):
            inner = wrapper_inner_script(profile.compose.script)
            if inner:
                scripts.add(inner)
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
    The base list carries every setting a profile's ``sandbox`` block can put
    in a deploy step's environment but one — see the note beside
    :data:`ENV_ALLOWLIST_BASE` for the one that is refused on purpose.
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


def _allowlisted_env(
    raw_env: Any, profile: DeployProfile
) -> tuple[dict[str, str], str | None]:
    """LAW 3 — the caller's env, or one plain sentence saying why not.

    One implementation for every shape ``/run`` carries: the vetted script,
    the live-gate driver. Absent reads as no overlay at all.
    """
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict):
        return {}, "'env' must be a JSON object of allowlisted string values"
    permitted_keys = allowed_env_keys(profile)
    env: dict[str, str] = {}
    for key, value in raw_env.items():
        if key not in permitted_keys:
            names = ", ".join(sorted(permitted_keys))
            return {}, (
                f"env key {key!r} is not allowlisted — deny by default. "
                f"Allowed: {names}"
            )
        if not isinstance(value, str):
            return {}, (
                f"env value for {key!r} must be a string, got "
                f"{type(value).__name__}"
            )
        env[key] = value
    return env, None


def _text_list(
    value: Any, *, field: str, max_items: int, max_chars: int
) -> tuple[list[str], str | None]:
    """Read a list of plain-text tokens; return ``(tokens, error)``.

    Absent reads as an empty list. Anything that is not a bounded list of
    text is a refusal in one sentence, checked before a process starts.
    """
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], f"'{field}' must be a list of text values"
    if len(value) > max_items:
        return [], (
            f"'{field}' may carry at most {max_items} values; got {len(value)}"
        )
    tokens: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            return [], (
                f"every entry in '{field}' must be written as text; got "
                f"{type(entry).__name__}"
            )
        if len(entry) > max_chars:
            return [], (
                f"a value in '{field}' may be at most {max_chars} characters "
                f"long; one is {len(entry)}"
            )
        tokens.append(entry)
    return tokens, None


def declared_test_command(
    repo_path: "Path | str",
    *,
    module_candidates: Sequence[str] = TOOLCHAIN_MODULE_CANDIDATES,
) -> tuple[str | None, int | None, str | None]:
    """The repository's own declared test command, read where it lives.

    Returns ``(command, timeout_seconds, error)``. Delegates to guardkit's OWN
    ``toolchain_declaration.load_toolchain_declaration`` — the loader that
    owns the schema — so this service never forms a second opinion about what
    a repository declared. ``error`` is one plain sentence when guardkit is
    not importable here, when the repository declares no toolchain, or when it
    declares one with no ``test:`` command; the caller turns that into a
    refusal and the gates reader turns the refusal into UNKNOWN, which is red.
    Never raises.
    """
    for candidate in module_candidates:
        try:
            module = import_module(candidate)
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
        try:
            declaration = module.load_toolchain_declaration(Path(repo_path))
        except Exception as exc:  # noqa: BLE001 — a loader defect is not a pass
            return None, None, (
                f"{repo_path}/.guardkit/config.yaml could not be read: "
                f"{type(exc).__name__}: {exc}"
            )
        if declaration is None:
            return None, None, (
                f"{repo_path}/.guardkit/config.yaml declares no toolchain, so "
                "there is no declared test command to run"
            )
        command = getattr(declaration, "test", None)
        if not command:
            return None, None, (
                f"{repo_path}/.guardkit/config.yaml declares a toolchain but "
                "no `test:` command, so there is no verdict-bearing gate to run"
            )
        timeout = getattr(declaration, "test_timeout", None)
        return (
            str(command),
            int(timeout) if isinstance(timeout, int) and timeout > 0 else None,
            None,
        )
    return None, None, (
        "guardkit's toolchain declaration loader is not importable in this "
        f"service (tried {', '.join(module_candidates)}), so "
        f"{repo_path}'s declared test command cannot be read"
    )


def _bounded_timeout(value: Any, *, default: float, ceiling: float) -> tuple[
    float, list[dict[str, str]], str | None
]:
    """The wall this run gets: the caller's, clamped, never refused for length.

    Ruled 2026-09-07 21:05Z on the leg route and applied here for the same
    reason: a stage wall wider than a route's ceiling should run at the
    ceiling with a warning on the result, not fail a run that could have
    happened. A wall that is not a positive number is still a refusal, because
    that is a mistake in the caller rather than an ambitious budget.
    """
    if value is None:
        return default, [], None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default, [], "'timeout_seconds' must be a positive number"
    timeout = float(value)
    if timeout <= ceiling:
        return timeout, [], None
    return ceiling, [
        {
            "code": "timeout_clamped",
            "message": (
                f"this run was asked for up to {timeout:g} seconds, which is "
                f"longer than the longest wall this route allows, so it was "
                f"given {ceiling:g} seconds instead"
            ),
        }
    ], None


def process_live_gate_run(
    payload: dict[str, Any],
    *,
    repo_path: Path,
    profile: DeployProfile,
    extra_env: dict[str, str],
    command_runner: MergeRunner,
) -> tuple[int, dict[str, Any]]:
    """``/run`` carrying the repository's own live-gate driver (rule 85).

    The candidate stands up inside the sandbox and its port is on the
    sandbox's own loopback, so the driver that checks it has to run in there
    too. The driver is the profile's ``live_gate.driver`` and nothing else:
    the request must send that exact argument list, and any other program is
    refused. The flags the caller adds (``--feature``, ``--target``,
    ``--gates``) ride in ``args``.

    The answer carries ``stdout`` whole (up to the merge route's cap) rather
    than a combined tail, because the driver prints its results envelope
    there and the caller reads the verdict out of it.
    """
    spec = profile.live_gate
    if spec is None:
        return 400, {
            "error": (
                "this repository's deploy/profile.yaml declares no live_gate "
                "driver, so there is no gate for the sidecar to run"
            )
        }
    driver, error = _text_list(
        payload.get("driver"),
        field="driver",
        max_items=DRIVER_MAX_ARGS,
        max_chars=DRIVER_MAX_ARG_CHARS,
    )
    if error:
        return 400, {"error": error}
    declared = [token for token in spec.driver]
    if driver != declared:
        return 400, {
            "error": (
                "the live-gate driver must be the one this repository's "
                f"deploy/profile.yaml declares ({' '.join(declared)}); it was "
                f"asked to run {' '.join(driver) if driver else '(nothing)'}"
            )
        }
    args, error = _text_list(
        payload.get("args"),
        field="args",
        max_items=DRIVER_MAX_ARGS,
        max_chars=DRIVER_MAX_ARG_CHARS,
    )
    if error:
        return 400, {"error": error}
    timeout, warnings, error = _bounded_timeout(
        payload.get("timeout_seconds"),
        default=float(spec.timeout_seconds),
        ceiling=MERGE_TIMEOUT_MAX,
    )
    if error:
        return 400, {"error": error}

    cwd = _resolve_cwd(repo_path, profile)
    candidate_cwd, cwd_error = _resolve_requested_cwd(repo_path, payload.get("cwd"))
    if cwd_error is not None:
        return 400, {"error": cwd_error}
    if candidate_cwd is not None:
        cwd = candidate_cwd

    argv = [*driver, *args]
    logger.info(
        "forge-deploy-sidecar: running the live-gate driver %s in %s (up to "
        "%g seconds)",
        " ".join(argv),
        cwd,
        timeout,
    )
    try:
        exit_code, stdout, stderr = command_runner(
            argv=argv,
            cwd=str(cwd),
            timeout=timeout,
            what="the live-gate driver",
            extra_env=extra_env or None,
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
        "timed_out": exit_code == MERGE_TIMEOUT_EXIT_CODE,
        "cwd": str(cwd),
        "warnings": warnings,
    }


def process_declared_test_run(
    payload: dict[str, Any],
    *,
    repo_path: Path,
    command_runner: MergeRunner,
) -> tuple[int, dict[str, Any]]:
    """``/run`` carrying the repository's own declared test command (rule 88).

    The merge-ready gates reader runs the command the repository declares in
    ``.guardkit/config.yaml`` in the fix journey's worktree, and for a
    repository with a sandbox neither that file nor that worktree exists on
    the host. So it runs here. The command is read from the repository's own
    declaration by guardkit's own loader and the request must send that exact
    command; anything else is refused. The working directory must be one of
    this repository's own journey worktrees.

    A declaration is a *command line* (``uv run --frozen pytest -q``), which
    is what the repository owner wrote and what guardkit's own executor runs,
    so it is handed to ``/bin/sh -c`` — the same shape the in-container reader
    uses. What may reach that shell is the repository's own checked-in text
    and nothing a caller composed.
    """
    error = _worktree_path_error(repo_path, payload.get("cwd"), what="cwd")
    if error:
        return 400, {"error": error}
    cwd = os.path.normpath(os.path.abspath(str(payload["cwd"])))
    if not os.path.isdir(cwd):
        return 400, {
            "error": (
                f"the working directory {cwd} is not there, so there is "
                "nowhere to run the declared test command"
            )
        }
    declared, declared_timeout, error = declared_test_command(repo_path)
    if error or declared is None:
        return 400, {"error": error}
    asked = payload.get("declared_test")
    if not isinstance(asked, str) or asked != declared:
        return 400, {
            "error": (
                "the test command must be the one this repository declares in "
                f".guardkit/config.yaml ({declared!r}); it was asked to run "
                f"{asked!r}"
            )
        }
    timeout, warnings, error = _bounded_timeout(
        payload.get("timeout_seconds"),
        default=float(declared_timeout or 300),
        ceiling=MERGE_TIMEOUT_MAX,
    )
    if error:
        return 400, {"error": error}

    logger.info(
        "forge-deploy-sidecar: running the declared test command %r in %s "
        "(up to %g seconds)",
        declared,
        cwd,
        timeout,
    )
    try:
        exit_code, stdout, stderr = command_runner(
            argv=["/bin/sh", "-c", declared],
            cwd=cwd,
            timeout=timeout,
            what="the declared test command",
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
        "timed_out": exit_code == MERGE_TIMEOUT_EXIT_CODE,
        "cwd": cwd,
        "command": declared,
        "warnings": warnings,
    }


def process_run_request(
    payload: Any,
    *,
    config: ForgeConfig,
    script_runner: ScriptRunner = _run_script_step,
    command_runner: "MergeRunner | None" = None,
    inside_sandbox: bool | None = None,
) -> tuple[int, dict[str, Any]]:
    """Validate + execute a ``/run`` payload; return ``(http_status, body)``.

    Enforces every deny-by-default law before any subprocess is spawned. Returns
    a 4xx with a loud ``error`` on a refusal, a 500 on an unexpected internal
    error, and a 200 with ``{exit_code, output_tail, cwd}`` on a permitted run (the
    script's non-zero exit is a 200 with a non-zero ``exit_code``, not an HTTP
    error — the script's verdict is data, not a transport failure). Never raises.

    Two other kinds of work reach this route (sandbox first, rules 85 and 88),
    each named by its own field and each running the repository's own declared
    command rather than one the caller composed: ``driver`` runs the profile's
    live-gate driver (:func:`process_live_gate_run`), and ``declared_test``
    runs the repository's declared test command in a journey worktree
    (:func:`process_declared_test_run`). A request carrying neither is the
    vetted-script request this route has always served, unchanged.

    BOTH of those are refused unless this sidecar is the one INSIDE a
    repository's sandbox (L3b's coach, 2026-09-08). They exist so that a
    repository's own code runs where the repository lives; answering them on
    the host would be the opposite — the host sidecar would run a
    repository's whole test suite, or its live-gate driver, under the
    operator's account, which is the wall Rich's rule of 2026-09-07 puts up.
    The host sidecar therefore still runs exactly what it ran before this
    lane: the programs ``deploy/profile.yaml`` names, and nothing else.

    ``inside_sandbox`` is the answer to "is this sidecar inside a sandbox?".
    Left as ``None`` it is read from the bootstrap's own environment value
    (:func:`sidecar_is_inside_sandbox`), which is how the running service
    answers it; a caller passes it only in tests.
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

    in_sandbox = (
        sidecar_is_inside_sandbox() if inside_sandbox is None else bool(inside_sandbox)
    )

    # SANDBOX FIRST (rule 88) — the merge-ready gates reader's declared test
    # command. It is answered BEFORE the deploy profile is read, because a
    # repository can have a fix journey without being deployable at all: what
    # it needs is a toolchain declaration and a journey worktree, not a deploy
    # profile. Its own function checks both.
    if payload.get("declared_test") is not None:
        if not in_sandbox:
            return 400, {"error": _not_inside_a_sandbox("declared test command")}
        return process_declared_test_run(
            payload,
            repo_path=repo_path,
            command_runner=command_runner or run_merge_command,
        )

    # LAW 2 (part a) — re-read the target's profile ourselves.
    profile_path = repo_path / "deploy" / "profile.yaml"
    try:
        profile = load_deploy_profile(profile_path)
    except DeployProfileError as exc:
        return 400, {"error": f"target repo {repo!r} is not deployable: {exc}"}

    # SANDBOX FIRST (rule 85) — the live-gate driver, whose program comes from
    # the repository's profile rather than from ``script``. Answered whole by
    # its own function, before a single line of the vetted-script path below
    # is reached, so that path is exactly what it always was for every request
    # that does not name a driver.
    if payload.get("driver") is not None:
        if not in_sandbox:
            return 400, {"error": _not_inside_a_sandbox("live-gate driver")}
        env_only, error = _allowlisted_env(payload.get("env"), profile)
        if error:
            return 400, {"error": error}
        return process_live_gate_run(
            payload,
            repo_path=repo_path,
            profile=profile,
            extra_env=env_only,
            command_runner=command_runner or run_merge_command,
        )

    # LAW 2 (part b) — refuse any script the profile does not name.
    if not isinstance(script, str) or not script.strip():
        return 400, {
            "error": (
                "'script' is required (a script named in the target's "
                "deploy/profile.yaml)"
            )
        }
    permitted = allowed_scripts(profile, inside_sandbox=in_sandbox)
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
    extra_env, env_error = _allowlisted_env(raw_env, profile)
    if env_error is not None:
        return 400, {"error": env_error}

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
    what: str = "the merge command",
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run one fixed argument list with no shell; return exit code and output.

    The command is started in a session of its own so a timeout can stop the
    whole process group (see :func:`_kill_process_group`). Output is captured
    separately — the caller needs the report on stdout intact — decoded, and
    passed through the same credential scrub the deploy scripts already use.

    Never raises: a command that cannot be started comes back as a non-zero
    exit code with a plain sentence saying so.

    ``what`` names the command in those sentences. It is the merge command by
    default, because that is what this runner was written for and what every
    existing caller runs; the live gate and the gates reader (sandbox first,
    rules 85 and 88) pass their own name so a person reading a failure is told
    which command would not start.

    ``extra_env`` is laid over this service's own environment for the command
    only. The live-gate driver needs it: the candidate leg's gate must address
    the candidate's port rather than the live one, and that address is one of
    the allowlisted, non-secret values the profile itself declares. ``None``
    (every caller before the live gate) inherits the environment exactly as
    before.
    """
    env = (os.environ | extra_env) if extra_env else None
    try:
        process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return (
            MERGE_NOT_STARTED_EXIT_CODE,
            "",
            f"{what} could not be started: {exc}",
        )
    except NotADirectoryError as exc:
        return (
            MERGE_NOT_STARTED_EXIT_CODE,
            "",
            f"{what} could not be started: {exc}",
        )
    except PermissionError as exc:
        return (126, "", f"{what} could not be run: {exc}")
    except OSError as exc:
        return (1, "", f"{what} could not be started: {exc}")

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
            f"\n{what} was stopped after {timeout:g} seconds and "
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
# them; a check must be one of the six names below (nothing else runs, and
# ``classify-scenarios`` may never be declared blocking); a check is one
# fixed argument list through the same no-shell runner the merge uses.
# ---------------------------------------------------------------------------

#: The three routes.
GIT_WRITE_TREE_ROUTE: str = "/git/prepare-branch-and-write-tree"
GIT_READ_FILE_ROUTE: str = "/git/read-file-from-branch"
GIT_REV_PARSE_ROUTE: str = "/git/rev-parse"

#: The checks the sidecar knows how to run — the closed list.
GIT_CHECK_NAMES: tuple[str, ...] = PRE_COMMIT_CHECK_NAMES

#: Each check's own time limit when the caller names none: the stamp
#: normalizer and the provability check are rules over a handful of files
#: (seconds); a feature validate reads a whole plan tree, and the gherkin
#: normalizer and the two schema checks are the planning oracles' usual ten
#: minutes — the same budget the driver's own closures give them.
GIT_CHECK_TIMEOUT_DEFAULTS: dict[str, float] = {
    "normalize-stamps": 120.0,
    "feature-validate": 600.0,
    "classify-scenarios": 120.0,
    "normalize-feature": 600.0,
    "validate-pass-bar": 600.0,
    "validate-gate-registry": 600.0,
}

#: Whether a check blocks the commit when the caller does not say: every
#: check the driver's own hook stops on, and never the provability check.
GIT_CHECK_BLOCKING_DEFAULTS: dict[str, bool] = {
    "normalize-stamps": True,
    "feature-validate": True,
    "classify-scenarios": False,
    "normalize-feature": True,
    "validate-pass-bar": True,
    "validate-gate-registry": True,
}

#: The one argument each path-shaped check takes, by name. Every value is a
#: repository-relative path, shape-checked before the worktree is joined to
#: it, exactly as ``classify-scenarios``'s always was.
GIT_CHECK_PATH_ARGS: dict[str, str] = {
    "classify-scenarios": "feature_file",
    "normalize-feature": "feature_file",
    "validate-pass-bar": "bar_file",
    "validate-gate-registry": "registry_file",
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
            key = GIT_CHECK_PATH_ARGS[name]
            error = _relative_path_error(
                args.get(key), what=f"checks[{index}] args.{key}"
            )
            if error:
                return None, error
            clean = {key: str(args[key])}
            allowed = {key}
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


#: The check whose command is NOT guardkit's: the gherkin normalizer the spec
#: leg runs is a guardkit MODULE (``python -m …``), resolved by its own dual
#: candidate probe.
NORMALIZER_CHECK_NAME: str = "normalize-feature"


def resolve_normalizer_command_for_checks(
    *, python_executable: str = sys.executable
) -> tuple[str, ...] | None:
    """The ``python -m <module>`` prefix the ``normalize-feature`` check runs,
    or ``None`` when guardkit's normalizer module is not importable here.

    The same resolution the spec leg's own oracle uses
    (:func:`forge.planning.target_terminal_tools.resolve_normalizer_command`):
    the wheel layout first, the source checkout second, probed in the
    interpreter the sidecar runs under — which is the interpreter the
    subprocess will use, so an importable spec here predicts the subprocess.
    ``None`` becomes one plain sentence to the caller, before any worktree is
    made; it is never a silent skip of the normalizer.
    """
    from forge.planning.target_terminal_tools import (
        NormalizerModuleUnresolved,
        resolve_normalizer_command,
    )

    try:
        return resolve_normalizer_command(python_executable=python_executable)
    except NormalizerModuleUnresolved as exc:
        logger.error("forge-deploy-sidecar: %s", exc)
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
    normalizer_command: tuple[str, ...] | None = None,
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
    * ``normalize-feature`` — the spec leg's gherkin normalizer over the
      committed ``.feature``: the box-drawing divider repair first (the
      closure's own, in place, its receipt riding ``note``), then
      ``python -m <the normalizer module> <the file>``; passed means exit 0,
      and the sentence a failure carries is the closure's word for word.
    * ``validate-pass-bar`` / ``validate-gate-registry`` — ``guardkit qa
      validate pass-bar <path>`` and ``guardkit qa validate gate-registry
      <path>``; passed means exit 0, and the details are the two legs' own
      sentences (a bar's names the bar first, as the pass-bar leg's loop
      does).

    Never raises: a runner that blows up is a failed check with the reason.
    """
    from forge.planning.target_terminal_tools import (
        NO_MODEL_OPTION_UNKNOWN_NOTE,
        _NORMALIZER_NO_MODEL_UNKNOWN_RE,
        _check_status_word,
        _repair_box_drawing_dividers,
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
        if check.name == "normalize-feature":
            feature_rel = str(check.args["feature_file"])
            target = (worktree / feature_rel).resolve()
            # The closure's own pre-parse repair, in place, before the parse:
            # a top-level box-drawing divider becomes a comment so the run
            # gets a parseable spec instead of dying with no revision loop.
            repair_note = _repair_box_drawing_dividers(target, feature_rel) or ""
            if repair_note:
                logger.warning(
                    "forge-deploy-sidecar: normalize-feature — %s", repair_note
                )
            if not normalizer_command:
                return _check_outcome(
                    check,
                    exit_code=1,
                    stdout="",
                    stderr="",
                    passed=False,
                    detail=(
                        "the gherkin normalizer module could not be resolved "
                        "in this sidecar's interpreter"
                    ),
                )
            exit_code, stdout, stderr = _run([*normalizer_command, str(target)])
            if exit_code == MERGE_TIMEOUT_EXIT_CODE:
                return _check_outcome(
                    check,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    passed=False,
                    detail=(
                        f"normalizer timed out after {check.timeout:g}s "
                        f"({feature_rel})"
                    ),
                    note=repair_note,
                )
            if exit_code != 0:
                return _check_outcome(
                    check,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    passed=False,
                    detail=(
                        f"normalizer exit {exit_code} for {feature_rel}: "
                        f"{(stderr or stdout).strip()[:500]}"
                    ),
                    note=repair_note,
                )
            return _check_outcome(
                check,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                passed=True,
                detail=repair_note,
                note=repair_note,
            )
        if check.name in ("validate-pass-bar", "validate-gate-registry"):
            verb = (
                "pass-bar" if check.name == "validate-pass-bar" else "gate-registry"
            )
            rel = str(check.args[GIT_CHECK_PATH_ARGS[check.name]])
            exit_code, stdout, stderr = _run([*command, "qa", "validate", verb, rel])
            status = _check_status_word(exit_code, exit_code == MERGE_TIMEOUT_EXIT_CODE)
            if status == "success" and exit_code == 0:
                return _check_outcome(
                    check,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    passed=True,
                    detail="",
                )
            detail = (
                f"guardkit qa validate {verb} {status} (exit {exit_code}) for "
                f"{rel}: {(stderr or stdout).strip()[:500]}"
            )
            if check.name == "validate-pass-bar":
                # The pass-bar leg's loop names the bar before the oracle's
                # own sentence; the same words reach the leg from here.
                detail = f"{rel}: {detail}"
            return _check_outcome(
                check,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                passed=False,
                detail=detail,
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
    normalizer_command: tuple[str, ...] | None = None,
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
                normalizer_command=normalizer_command,
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
    normalizer_resolver: Callable[
        [], tuple[str, ...] | None
    ] = resolve_normalizer_command_for_checks,
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
    if any(check.name != NORMALIZER_CHECK_NAME for check in checks):
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
    normalizer_command: tuple[str, ...] | None = None
    if any(check.name == NORMALIZER_CHECK_NAME for check in checks):
        normalizer_command = normalizer_resolver()
        if not normalizer_command:
            return 500, {
                "error": (
                    "this sidecar cannot resolve guardkit's gherkin normalizer "
                    "module, so the spec leg's check cannot be run here — "
                    "install guardkit in the interpreter the sidecar runs "
                    "under, or provide a checkout with an importable "
                    "top-level installer package"
                )
            }

    outcomes: list[PreCommitCheckOutcome] = []
    hook = (
        _declared_checks_hook(
            checks,
            command=command or (),
            check_runner=check_runner,
            outcomes=outcomes,
            normalizer_command=normalizer_command,
        )
        if checks
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
# The merge press's own git, made where the repository lives
# (sandbox first, 2026-09-07, rule 89)
# ---------------------------------------------------------------------------
#
# The merge word is one of Rich's three touches, and for a repository whose
# factory lives in its sandbox the branch the build made is in the clone in
# there — not in the copy of the repository on this side. So the press's own
# git comes here: the ancestry questions it asks before and after the merge,
# the lay-out of the branch's tree for the candidate check, and that tree's
# removal when the run ends. The branch look-up, main's commit and the tree
# ids are the existing ``/git/rev-parse`` route, which already answers a
# ``^{tree}`` revision.
#
# Each route acts on the repository its ``repo`` key names and on NO path the
# caller sends: the tree's place is derived here, from that repository's own
# path, exactly as :mod:`forge.deploy.candidate_tree` derives it on the other
# side. Refs and ids are shape-checked before git is started, and no shell is
# ever used.
#
# WHY THESE THREE ARE NOT GATED ON "AM I INSIDE A SANDBOX?", when the two
# ``/run`` shapes and the routing-law evidence are. Those three run or read a
# repository's OWN things — its test suite, its live-gate driver, its records
# — and running a repository's code on the host is the wall Rich's rule of
# 2026-09-07 puts up. These three run git and nothing else: a commit look-up,
# an ancestry question, and an archive of a commit extracted into a directory,
# with no repository code executed and no shell. They are the same class of
# thing as ``/git/rev-parse`` and ``/git/read-file-from-branch``, which the
# sidecar on the host has answered since L1 because the planning chain uses
# them. So the host sidecar answers these too, for the repositories it already
# serves, and gains no power it did not have.

#: The three routes.
GIT_IS_ANCESTOR_ROUTE: str = "/git/is-ancestor"
GIT_CANDIDATE_TREE_ROUTE: str = "/git/candidate-tree"
GIT_CANDIDATE_TREE_REMOVE_ROUTE: str = "/git/candidate-tree-remove"


def _feature_id_error(value: Any) -> str | None:
    """A plain sentence unless ``value`` is one plain feature id.

    The id becomes one directory name under the trees root, so it is held to
    the same shape every other id on these routes is: letters, digits and the
    three separators, and never a path.
    """
    if not isinstance(value, str) or not SAFE_NAME_PATTERN.match(value):
        return (
            "'feature_id' is required and must be a plain feature id (letters, "
            "digits, dots, dashes and underscores, no slashes); got "
            f"{value!r}"
        )
    return None


def process_git_is_ancestor_request(
    payload: Any, *, config: ForgeConfig
) -> tuple[int, dict[str, Any]]:
    """``{repo, ancestor, descendant}`` → ``{is_ancestor}``.

    ``git merge-base --is-ancestor`` in this repository: ``true``, ``false``,
    or ``null`` when git could not say (a commit it does not know, or git not
    running), which is exactly the three answers the press's guards are
    written for. Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    ancestor = payload.get("ancestor")
    error = _ref_error(ancestor, what="ancestor")
    if error:
        return 400, {"error": error}
    descendant = payload.get("descendant")
    error = _ref_error(descendant, what="descendant")
    if error:
        return 400, {"error": error}
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [
                "git",
                "-C",
                str(repo_path),
                "merge-base",
                "--is-ancestor",
                str(ancestor),
                str(descendant),
            ],
            capture_output=True,
            text=True,
            timeout=GIT_REV_PARSE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 500, {"error": f"sidecar git error: {type(exc).__name__}: {exc}"}
    if result.returncode == 0:
        return 200, {"is_ancestor": True}
    if result.returncode == 1:
        return 200, {"is_ancestor": False}
    return 200, {
        "is_ancestor": None,
        "detail": (
            f"git could not say whether {ancestor} is in {descendant} "
            f"(it exited {result.returncode})"
        ),
    }


def process_git_candidate_tree_request(
    payload: Any, *, config: ForgeConfig
) -> tuple[int, dict[str, Any]]:
    """``{repo, feature_id, sha}`` → ``{path, tree, exclude_written}``.

    Lays the tree of ``sha`` out at ``<clone>/.forge-candidates/<feature id>``
    with the very code the in-container venue runs
    (:func:`forge.deploy.candidate_tree.materialise_candidate_tree`), keeps
    that directory out of the clone's eyes first, and answers the commit's
    tree id so the caller need not ask twice.

    A lay-out that fails leaves nothing behind and comes back as a 400 with
    git's own words: it is an answer about this repository, not a transport
    failure. Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    feature_id = payload.get("feature_id")
    error = _feature_id_error(feature_id)
    if error:
        return 400, {"error": error}
    sha = payload.get("sha")
    error = _ref_error(sha, what="sha")
    if error:
        return 400, {"error": error}

    from forge.deploy.candidate_tree import (
        CandidateTreeError,
        ensure_candidate_trees_excluded,
        git_rev_parse,
        materialise_candidate_tree,
    )

    logger.info(
        "forge-deploy-sidecar: laying %s's tree out for %s in %s",
        sha,
        feature_id,
        repo_path,
    )
    try:
        excluded = _run_coroutine(ensure_candidate_trees_excluded(repo_path))
        laid_out = _run_coroutine(
            materialise_candidate_tree(repo_path, str(feature_id), str(sha))
        )
        tree = _run_coroutine(git_rev_parse(repo_path, f"{sha}^{{tree}}"))
    except CandidateTreeError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {"error": f"sidecar git error: {type(exc).__name__}: {exc}"}
    return 200, {
        "path": str(laid_out),
        "tree": tree,
        "exclude_written": excluded,
    }


def process_git_candidate_tree_remove_request(
    payload: Any, *, config: ForgeConfig
) -> tuple[int, dict[str, Any]]:
    """``{repo, feature_id}`` → ``{removed, path}``.

    Removes ``<clone>/.forge-candidates/<feature id>``. A tree that is already
    gone is a removal that succeeded — the press calls this on every ending,
    including endings where nothing was ever laid out. Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    feature_id = payload.get("feature_id")
    error = _feature_id_error(feature_id)
    if error:
        return 400, {"error": error}

    from forge.deploy.candidate_tree import (
        CandidateTreeError,
        candidate_tree_path,
        remove_candidate_tree,
    )

    try:
        path = candidate_tree_path(repo_path, str(feature_id))
    except CandidateTreeError as exc:
        return 400, {"error": str(exc)}
    logger.info("forge-deploy-sidecar: removing the candidate tree at %s", path)
    try:
        removed = _run_coroutine(remove_candidate_tree(path))
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {"error": f"sidecar git error: {type(exc).__name__}: {exc}"}
    return 200, {"removed": bool(removed), "path": str(path)}


# ---------------------------------------------------------------------------
# The fix journey's tree and its receipts, made where the repository lives
# (sandbox first, 2026-09-07, rules 76 and 77)
# ---------------------------------------------------------------------------
#
# The conductor used to cut a fix journey's worktree with git inside
# forge-prod, against the operator's bind-mounted checkout, and to copy that
# tree's receipts out with the container's own filesystem. For a repository
# that has a sandbox neither is possible any more, and neither should be:
# nothing the factory runs on a repository runs on the host. These three
# routes are the same work, done inside the sandbox, on the factory's clone.
#
# LAW 10 (the worktree routes' own): the routes act on the given repository's
# path and on nothing else. A journey tree is
# ``<repo>/.forge/worktrees/<build id>`` — one directory, directly under that
# one parent — and a path that is not exactly that shape is refused before
# git is started. The work itself is the conductor's own module, imported,
# so the two sides cannot drift apart.

#: The three routes, and the fix journey's commit count (2026-09-08).
GIT_WORKTREE_ADD_ROUTE: str = "/git/worktree-add"
GIT_WORKTREE_REMOVE_ROUTE: str = "/git/worktree-remove"
GIT_WORKTREE_COMMIT_COUNT_ROUTE: str = "/git/worktree-commit-count"
RECEIPTS_EXPORT_ROUTE: str = "/receipts/export"

#: The shape a build id, a stage name or a worktree leaf may have before it
#: is joined to a path or put in a directory name: letters, digits and the
#: three separators the estate's own ids use. No slash, no dot, so ``..`` and
#: a second path component are both impossible by construction.
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _worktree_path_error(
    repo_path: Path, value: Any, *, what: str = "path"
) -> str | None:
    """LAW 10 — a plain sentence unless ``value`` is this repository's own
    journey-tree path (``<repo>/.forge/worktrees/<leaf>``)."""
    if not isinstance(value, str) or not value.strip():
        return (
            f"'{what}' is required (the journey worktree's path, "
            "<repo>/.forge/worktrees/<build id>)"
        )
    from forge.cli._conductor_worktree import WORKTREES_DIR

    wanted = os.path.normpath(os.path.abspath(value))
    parent = os.path.normpath(os.path.abspath(str(repo_path / WORKTREES_DIR)))
    leaf = os.path.basename(wanted)
    if os.path.dirname(wanted) != parent or not SAFE_NAME_PATTERN.match(leaf):
        return (
            f"'{what}' {value!r} is not a journey worktree of this repository — "
            f"the sidecar acts on {parent}/<build id> and on no other path"
        )
    return None


def process_git_worktree_add_request(
    payload: Any, *, config: ForgeConfig
) -> tuple[int, dict[str, Any]]:
    """Validate and perform a ``/git/worktree-add`` payload.

    ``{repo, path, branch, base_ref}`` → on a permitted request a 200 carrying
    ``{status, path, branch, base_ref, reused, detail}``: ``status`` is
    ``success`` when the tree is there (``reused`` says whether it was
    already), or ``failed`` with ``detail`` saying in one sentence why not — a
    collision or a base branch nobody made is an answer, not a transport
    error. A refusal of the request itself is a 400 with one plain sentence.
    Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    error = _worktree_path_error(repo_path, payload.get("path"))
    if error:
        return 400, {"error": error}
    branch = payload.get("branch")
    error = _ref_error(branch, what="branch")
    if error:
        return 400, {"error": error}
    from forge.cli._conductor_worktree import (
        JOURNEY_BASE_REF,
        cut_worktree_in_checkout,
    )

    base_ref = payload.get("base_ref") or JOURNEY_BASE_REF
    error = _ref_error(base_ref, what="base_ref")
    if error:
        return 400, {"error": error}

    build_id = os.path.basename(os.path.normpath(str(payload["path"])))
    logger.info(
        "forge-deploy-sidecar: cutting %s in %s on %s off %s",
        build_id,
        repo_path,
        branch,
        base_ref,
    )
    try:
        cut = _run_coroutine(
            cut_worktree_in_checkout(
                checkout=repo_path,
                build_id=build_id,
                branch=str(branch),
                base_ref=str(base_ref),
            )
        )
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {
            "error": f"sidecar git error: {type(exc).__name__}: {exc}",
            "status": "failed",
            "path": None,
            "reused": False,
            "detail": "",
        }
    return 200, {
        "status": "success" if cut.ok else "failed",
        "path": cut.path or None,
        "branch": cut.branch,
        "base_ref": cut.base_ref,
        "reused": bool(cut.reused),
        "detail": cut.reason,
    }


def process_git_worktree_remove_request(
    payload: Any, *, config: ForgeConfig
) -> tuple[int, dict[str, Any]]:
    """Validate and perform a ``/git/worktree-remove`` payload.

    ``{repo, path}`` → ``{status, path, detail}``. A path that is already
    gone is a success: there is nothing left to remove, and a second call
    must be safe. Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    error = _worktree_path_error(repo_path, payload.get("path"))
    if error:
        return 400, {"error": error}

    from forge.cli._conductor_worktree import remove_journey_worktree

    worktree = str(payload["path"])
    logger.info("forge-deploy-sidecar: removing the worktree at %s", worktree)
    try:
        removed = _run_coroutine(remove_journey_worktree(worktree=worktree))
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {
            "error": f"sidecar git error: {type(exc).__name__}: {exc}",
            "status": "failed",
            "path": worktree,
            "detail": "",
        }
    return 200, {
        "status": "success" if removed.ok else "failed",
        "path": removed.path,
        "detail": removed.reason,
    }


def process_git_worktree_commit_count_request(
    payload: Any, *, config: ForgeConfig
) -> tuple[int, dict[str, Any]]:
    """``{repo, path, base}`` → ``{count, head}`` — did this fix journey
    actually commit anything?

    The thirteenth seam (2026-09-08). The conductor asks this question at the
    end of every fix journey, and it used to ask it by running git itself,
    against the journey worktree. For a repository whose factory lives in its
    sandbox that worktree is inside the sandbox and forge cannot see it, so
    the journey died with a "file not found" one step short of its card. The
    question comes here instead, the way the tree's cutting and the legs
    already do.

    Two fixed git commands in the tree the caller names: ``rev-list --count
    <base>..HEAD`` for the number, and ``rev-parse HEAD`` for the commit the
    count was taken at (an answer the caller can put in a log and check
    later). No shell, no repository code, nothing written.

    LAW 10 applies unchanged: the path must be this repository's own journey
    worktree and nothing else, and ``base`` is shape-checked exactly like
    every other ref this sidecar passes to git. A request that breaks either
    rule is a 400 with one plain sentence; git failing to answer is a 500
    saying so, because a probe that cannot answer must never be read as "this
    journey changed nothing". Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    error = _worktree_path_error(repo_path, payload.get("path"))
    if error:
        return 400, {"error": error}
    worktree = os.path.normpath(os.path.abspath(str(payload["path"])))
    if not os.path.isdir(worktree):
        return 400, {
            "error": (
                f"the journey worktree {worktree} is not there, so there are "
                "no commits to count in it"
            )
        }
    base = payload.get("base")
    error = _ref_error(base, what="base")
    if error:
        return 400, {"error": error}

    def _git(*args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", worktree, *args],
            capture_output=True,
            text=True,
            timeout=GIT_REV_PARSE_TIMEOUT_SECONDS,
            check=False,
        )

    try:
        counted = _git("rev-list", "--count", f"{base}..HEAD")
        head = _git("rev-parse", "HEAD") if counted.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 500, {"error": f"sidecar git error: {type(exc).__name__}: {exc}"}

    if counted.returncode != 0:
        detail = (counted.stderr or counted.stdout or "").strip() or "<no output>"
        return 500, {
            "error": (
                f"git could not count {base}..HEAD in {worktree} "
                f"(it exited {counted.returncode}): {detail}"
            )
        }
    raw = (counted.stdout or "").strip()
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 500, {
            "error": (
                f"git rev-list --count answered {raw!r} for {base}..HEAD in "
                f"{worktree}, which is not a number"
            )
        }
    sha = (head.stdout or "").strip() if head is not None and head.returncode == 0 else ""
    return 200, {"count": count, "head": sha or None}


def process_receipts_export_request(
    payload: Any, *, config: ForgeConfig
) -> tuple[int, dict[str, Any]]:
    """Validate and perform a ``/receipts/export`` payload.

    ``{build_id, stage, worktree, extra_files}`` → ``{status, stage_key, dest,
    families, files}``: the fix journey's own receipts export, run here, so
    the tree it copies from never leaves the sandbox and what it writes lands
    under the receipts root forge resolves (``FORGE_RECEIPTS_DIR``), which is
    mounted read-write and is the same directory forge-prod reads.

    ``extra_files`` is the small text the conductor writes beside the copied
    families (the turn's rationale); each name must be a plain file name.

    The worktree must be inside one of the repositories this sidecar knows,
    for the same reason the git routes fence their paths: the sidecar copies
    from where the factory works and from nowhere else. Never raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    build_id = payload.get("build_id")
    if not isinstance(build_id, str) or not SAFE_NAME_PATTERN.match(build_id):
        return 400, {
            "error": (
                "'build_id' is required and must be a plain build id (letters, "
                f"digits, dots, dashes and underscores); got {build_id!r}"
            )
        }
    stage = payload.get("stage")
    if not isinstance(stage, str) or not SAFE_NAME_PATTERN.match(stage):
        return 400, {
            "error": (
                "'stage' is required and must be a plain stage name; got "
                f"{stage!r}"
            )
        }
    worktree = payload.get("worktree")
    if not isinstance(worktree, str) or not worktree.strip():
        return 400, {"error": "'worktree' is required (the tree to export from)"}
    wanted = os.path.normpath(os.path.abspath(worktree))
    inside = any(
        _is_inside(wanted, os.path.normpath(os.path.abspath(str(root))))
        for root in config.planning.target_repo_paths.values()
    )
    if not inside:
        known = ", ".join(sorted(config.planning.target_repo_paths)) or "(none)"
        return 400, {
            "error": (
                f"'worktree' {worktree!r} is not inside any repository this "
                f"sidecar knows ({known}), so there is nothing here to export "
                "receipts from"
            )
        }
    extra_files, error = _validate_extra_files(payload.get("extra_files"))
    if error:
        return 400, {"error": error}

    from forge.pipeline.fix_journey_receipts import export_stage_receipts

    logger.info(
        "forge-deploy-sidecar: exporting %s receipts for %s from %s",
        stage,
        build_id,
        wanted,
    )
    try:
        result = export_stage_receipts(
            build_id=build_id,
            stage=stage,
            worktree_path=wanted,
            extra_files=extra_files or None,
        )
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {"error": f"sidecar receipts error: {type(exc).__name__}: {exc}"}
    dest = str(result.dest)
    families = list(result.families)
    return 200, {
        "status": "success" if result.ok else "failed",
        "stage_key": result.stage_key,
        "dest": dest,
        "families": families,
        "files": [f"{dest}/{name}" for name in families]
        + [f"{dest}/{name}" for name in sorted(extra_files or {})],
    }


# ---------------------------------------------------------------------------
# The routing law's own evidence, read where the repository lives (rule 88)
# ---------------------------------------------------------------------------
#
# Found by L3b's coach, 2026-09-08. The merge-ready gates reader has five
# steps, and the last one is the routing law's stamped-verifier check: it
# reads the feature's per-scenario ``verifier:`` stamps from the canonical
# repository and then asks, home by home, whether the promised verifier
# really ran green for this branch — from the newest results envelope under
# the journey worktree, and the branch's last code commit time, which says
# whether that envelope is fresh enough to count.
#
# All three of those live inside the sandbox for a repository that has one.
# Left reading the host, the check found no feature file, answered "this
# feature carries no scenario stamps", had no effect, and a GREEN merge card
# went out with the routing law silently not applied. That is the one
# direction this reader is written never to fail in. So the three reads
# happen here, and the DECISION still happens on the forge side, out of the
# same pure function it always used: this route reads, it never judges.

#: The route.
STAMPS_EVIDENCE_ROUTE: str = "/routing-stamps/evidence"


def process_stamps_evidence_request(
    payload: Any,
    *,
    config: ForgeConfig,
    worktrees_root: Path | None = None,
    inside_sandbox: bool | None = None,
) -> tuple[int, dict[str, Any]]:
    """``{repo, feature_id, worktree, branch}`` → the routing law's evidence.

    The answer carries three things and no opinion about them:

    * ``feature_yaml`` — the feature's plan of record, read from the CANONICAL
      branch of the factory's clone (``main``, never the worktree the journey
      has been editing, for the same reason the declared toolchain is), with
      the path it has in here so every sentence a person reads names the real
      file. ``present`` is false when the file is not on that branch, which
      upstream reads exactly as an absent file on the host does.
    * ``envelope`` — the newest results envelope under the journey worktree's
      ``qa/gates/history/``, or ``null``.
    * ``code_commit_time`` — the branch's last code commit, ISO-8601, or
      ``null`` when git could not say.

    The worktree must be one of this repository's own journey worktrees (LAW
    10). Never raises.

    REFUSED ON THE HOST, like the two ``/run`` shapes beside it (L3e, the
    third coach's second must-fix). This route exists so that the routing
    law's evidence is read where a sandboxed repository's clone, its journey
    worktrees and its gate receipts actually are; a host sidecar answering it
    would be reading the operator's own checkout, which is the wall Rich's
    rule of 2026-09-07 puts up. ``inside_sandbox`` is read from the
    bootstrap's environment value unless a caller (a test) says otherwise.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    in_sandbox = (
        sidecar_is_inside_sandbox() if inside_sandbox is None else bool(inside_sandbox)
    )
    if not in_sandbox:
        return 400, {
            "error": _not_inside_a_sandbox("routing-law evidence", verb="read")
        }
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    feature_id = payload.get("feature_id")
    if not isinstance(feature_id, str) or not SAFE_NAME_PATTERN.match(feature_id):
        return 400, {
            "error": (
                "'feature_id' is required and must be a plain feature id "
                f"(letters, digits, dots, dashes and underscores); got "
                f"{feature_id!r}"
            )
        }
    error = _worktree_path_error(repo_path, payload.get("worktree"), what="worktree")
    if error:
        return 400, {"error": error}
    worktree = os.path.normpath(os.path.abspath(str(payload["worktree"])))
    branch = payload.get("branch")
    if branch is not None:
        error = _ref_error(branch, what="branch")
        if error:
            return 400, {"error": error}
    from forge.cli._conductor_worktree import JOURNEY_BASE_REF

    canonical = payload.get("canonical_branch") or JOURNEY_BASE_REF
    error = _ref_error(canonical, what="canonical_branch")
    if error:
        return 400, {"error": error}

    from forge.pipeline.routing_stamps import (
        HISTORY_RELATIVE_PATH,
        feature_yaml_relative_path,
        read_last_code_commit_time,
        read_newest_envelope,
    )

    runner = _git_runner(worktrees_root)
    found_path = feature_yaml_relative_path(feature_id)
    content: str | None = None
    try:
        for suffix in ("yaml", "yml"):
            relative = feature_yaml_relative_path(feature_id, suffix=suffix)
            answer = _run_coroutine(
                runner.read_file_from_branch(
                    repo_path=str(repo_path),
                    branch=str(canonical),
                    file_path=relative,
                )
            )
            if isinstance(answer, str):
                content, found_path = answer, relative
                break
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {"error": f"sidecar git error: {type(exc).__name__}: {exc}"}

    history_dir = os.path.join(worktree, str(HISTORY_RELATIVE_PATH))
    try:
        envelope = read_newest_envelope(history_dir)
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {"error": f"sidecar receipts error: {type(exc).__name__}: {exc}"}
    try:
        commit_time = read_last_code_commit_time(
            worktree, str(branch) if branch else None
        )
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {"error": f"sidecar git error: {type(exc).__name__}: {exc}"}

    return 200, {
        "feature_yaml": {
            "path": str(Path(repo_path) / found_path),
            "branch": str(canonical),
            "present": content is not None,
            "text": content,
        },
        "envelope": None
        if envelope is None
        else {
            "path": str(envelope.path),
            "run_id": envelope.run_id,
            "verdict": envelope.verdict,
            "started": None
            if envelope.started is None
            else envelope.started.isoformat(),
            "gates": dict(envelope.gates),
            "feature_id": envelope.feature_id,
        },
        "code_commit_time": None
        if commit_time is None
        else commit_time.isoformat(),
        "history_dir": history_dir,
    }


def _is_inside(candidate: str, root: str) -> bool:
    """Containment by path components, never by ``startswith``."""
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:  # pragma: no cover — different drives (win32)
        return False


def _validate_extra_files(raw: Any) -> tuple[dict[str, str], str | None]:
    """The small text files written beside the copied families."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "'extra_files' must be a JSON object of file name → text"
    out: dict[str, str] = {}
    for name, text in raw.items():
        if not isinstance(name, str) or not SAFE_NAME_PATTERN.match(name):
            return {}, (
                f"'extra_files' name {name!r} must be a plain file name "
                "(letters, digits, dots, dashes and underscores)"
            )
        if not isinstance(text, str):
            return {}, f"'extra_files' value for {name!r} must be text"
        out[name] = text
    return out, None


# ---------------------------------------------------------------------------
# The fix journey's legs, run where the repository lives (rule 75)
# ---------------------------------------------------------------------------
#
# A journey's review and work legs (``guardkit task-review``, ``guardkit
# task-work``) install and run the repository's own code, so for a repository
# with a sandbox they run inside it. The sidecar already carried the merge
# word's one command; this route carries those two, and nothing else.
#
# LAW 11 (the leg route's own): the subcommand is one of two words; the
# working directory is a journey worktree of the repository named, and no
# other directory; the arguments are passed as one fixed argument list with
# no shell, exactly as the merge's are; the exit code is data, because "the
# leg ran and failed" is an answer.
#
# A leg is given what it would have been given in the container: the
# conductor's forward-context paths, the ``--context`` flags the
# repository's own manifest asks for (read HERE, against the tree the leg
# runs in), and ``--nats`` when the caller wants progress messages. The
# answer carries the whole of stdout, whether the leg was stopped at its
# wall, and anything the manifest reading had to say — so the caller can
# read the leg's own findings out of it exactly as it reads a leg that ran
# in the container.

#: The route.
GUARDKIT_LEG_ROUTE: str = "/guardkit-leg"

#: The only two subcommands this route will carry.
LEG_SUBCOMMANDS: tuple[str, ...] = ("task-review", "task-work")

#: A leg's own time limits. The default matches the conductor's work-stage
#: tripwire; the cap is longer than either tripwire so an operator who widens
#: a leg's budget in a profile is not silently cut back here.
LEG_TIMEOUT_DEFAULT: float = 1800.0
LEG_TIMEOUT_MAX: float = 7200.0

#: How many argument tokens a leg may carry, and how long one may be. The
#: forward context rides as text pairs, so these are generous; they exist so
#: a runaway caller cannot hand the sidecar an unbounded argument list.
LEG_MAX_ARGS: int = 256
LEG_MAX_ARG_CHARS: int = 65_536

#: How many paths a leg's ``extra_context_paths`` or ``read_allowlist`` may
#: carry. The same reason as ``LEG_MAX_ARGS``: generous, and bounded.
LEG_MAX_PATHS: int = 256


def _leg_path_list(value: Any, *, field: str) -> tuple[list[str], str | None]:
    """Read one of the leg route's path lists; return ``(paths, error)``.

    Absent reads as an empty list. Anything that is not a list of plain text
    paths is a refusal in one sentence, checked before a process starts.
    """
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], f"'{field}' must be a list of paths written as text"
    if len(value) > LEG_MAX_PATHS:
        return [], (
            f"'{field}' may carry at most {LEG_MAX_PATHS} paths; got {len(value)}"
        )
    paths: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry:
            return [], (
                f"every entry in '{field}' must be a path written as text; got "
                f"{type(entry).__name__}"
            )
        if len(entry) > LEG_MAX_ARG_CHARS:
            return [], (
                f"a path in '{field}' may be at most {LEG_MAX_ARG_CHARS} "
                f"characters long; one is {len(entry)}"
            )
        paths.append(entry)
    return paths, None


def _leg_context_flags(
    *,
    cwd: str,
    repo_path: str,
    subcommand: str,
    read_allowlist: list[str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Work out the leg's ``--context`` flags from the manifest, in here.

    The in-container runner reads the repository's own
    ``.guardkit/context-manifest.yaml`` and turns it into ``--context`` flags
    before it spawns guardkit. A leg run in the sandbox must be told the same
    thing, and the manifest it must be read from is the one in the sandbox's
    own tree — so the reading happens here, against the journey worktree the
    leg will run in, rather than in the forge container where that tree may
    not exist at all.

    Never raises: a manifest that cannot be read costs the leg its context
    flags and says so, exactly as it does in the container.
    """
    allowlist = [Path(entry) for entry in read_allowlist] or [Path(repo_path)]
    try:
        resolved = resolve_context_flags(Path(cwd), subcommand, allowlist)
    except KeyError:
        return [], [
            {
                "code": "context_resolver_unknown_subcommand",
                "message": (
                    f"resolver has no category filter for subcommand "
                    f"{subcommand!r}; proceeding with no --context flags"
                ),
            }
        ]
    except Exception as exc:  # noqa: BLE001 — a leg never dies of its context
        logger.warning(
            "forge-deploy-sidecar: reading the context manifest under %s "
            "raised %s: %s — the leg runs with no --context flags",
            cwd,
            type(exc).__name__,
            exc,
        )
        return [], [
            {
                "code": "context_manifest_unreadable",
                "message": (
                    f"could not read the context manifest under {cwd}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        ]
    warnings = [
        {"code": warning.code, "message": warning.message}
        for warning in resolved.warnings
    ]
    return list(resolved.flags), warnings


def process_guardkit_leg_request(
    payload: Any,
    *,
    config: ForgeConfig,
    leg_runner: MergeRunner = run_merge_command,
    command_resolver: Callable[[], str | None] = resolve_guardkit_command,
) -> tuple[int, dict[str, Any]]:
    """Validate and run a ``/guardkit-leg`` payload; return ``(status, body)``.

    ``{repo, cwd, subcommand, args, timeout_seconds, extra_context_paths,
    read_allowlist, with_nats_streaming}`` → on a permitted request a 200
    carrying ``{exit_code, stdout, stderr_tail, timed_out,
    context_warnings}``. The exit code is data, because "the leg ran and
    failed" is an answer; ``timed_out`` is separate from it, because a leg
    stopped at its wall is not a leg that failed.

    The last three fields of the request are how a leg is told what it would
    have been told in the container: ``extra_context_paths`` are the
    conductor's own forward-context paths, ``read_allowlist`` bounds which of
    the manifest's documents may be read (absent: the repository itself), and
    ``with_nats_streaming`` asks for ``--nats`` so the leg publishes its
    progress. The manifest's own ``--context`` flags are worked out here,
    against the tree the leg runs in.

    Everything is checked before a process starts: the repository must be one
    the forge configuration names; the working directory must be one of that
    repository's own journey worktrees and must exist; the subcommand must be
    ``task-review`` or ``task-work``; every argument and every path must be
    text; the timeout must be a positive number. A wall longer than this
    route's ceiling is cut back to the ceiling and said so in the result's
    warnings, never refused — a profile that asks for a longer stage wall
    should run, not fail. A refusal is a 4xx with one plain sentence. Never
    raises.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    repo_path, error = _resolve_repo_key(payload, config)
    if error or repo_path is None:
        return 400, {"error": error}
    error = _worktree_path_error(repo_path, payload.get("cwd"), what="cwd")
    if error:
        return 400, {"error": error}
    cwd = os.path.normpath(os.path.abspath(str(payload["cwd"])))
    if not os.path.isdir(cwd):
        return 400, {
            "error": (
                f"the working directory {cwd} is not there, so there is no "
                "worktree for this leg to run in"
            )
        }
    subcommand = payload.get("subcommand")
    if subcommand not in LEG_SUBCOMMANDS:
        return 400, {
            "error": (
                "'subcommand' must be one of "
                f"{', '.join(LEG_SUBCOMMANDS)}; got {subcommand!r}"
            )
        }
    raw_args = payload.get("args") or []
    if not isinstance(raw_args, list):
        return 400, {"error": "'args' must be a list of text arguments"}
    if len(raw_args) > LEG_MAX_ARGS:
        return 400, {
            "error": (
                f"'args' may carry at most {LEG_MAX_ARGS} arguments; got "
                f"{len(raw_args)}"
            )
        }
    args: list[str] = []
    for entry in raw_args:
        if not isinstance(entry, str):
            return 400, {
                "error": (
                    "every entry in 'args' must be written as text; got "
                    f"{type(entry).__name__}"
                )
            }
        if len(entry) > LEG_MAX_ARG_CHARS:
            return 400, {
                "error": (
                    f"an argument may be at most {LEG_MAX_ARG_CHARS} characters "
                    f"long; one is {len(entry)}"
                )
            }
        args.append(entry)

    # THE LEG IS TOLD WHAT IT WOULD HAVE BEEN TOLD IN THE CONTAINER. Rule 75
    # moves where a leg runs, not what it is given: the conductor's forward
    # context paths, the manifest's own context flags and the progress switch
    # all travel with the request, and the flags are assembled below in the
    # same order the in-container runner assembles them.
    extra_context_paths, error = _leg_path_list(
        payload.get("extra_context_paths"), field="extra_context_paths"
    )
    if error:
        return 400, {"error": error}
    read_allowlist, error = _leg_path_list(
        payload.get("read_allowlist"), field="read_allowlist"
    )
    if error:
        return 400, {"error": error}
    with_nats_streaming = payload.get("with_nats_streaming", False)
    if not isinstance(with_nats_streaming, bool):
        return 400, {
            "error": (
                "'with_nats_streaming' must be true or false; got "
                f"{type(with_nats_streaming).__name__}"
            )
        }

    timeout_seconds = payload.get("timeout_seconds")
    timeout = LEG_TIMEOUT_DEFAULT
    # A WALL WIDER THAN THIS ROUTE ALLOWS IS CUT BACK, NEVER REFUSED (ruled
    # 2026-09-07 21:05Z). Refusing it with a 400 would fail a leg that could
    # have run: the caller would get "internal error before dispatch" for a
    # profile that merely asked for a longer stage wall than this route's
    # ceiling. So the leg runs with the longest wall the route has, and the
    # result says so in plain words alongside the context warnings.
    timeout_warnings: list[dict[str, str]] = []
    if timeout_seconds is not None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            return 400, {"error": "'timeout_seconds' must be a positive number"}
        timeout = float(timeout_seconds)
        if timeout > LEG_TIMEOUT_MAX:
            timeout_warnings.append(
                {
                    "code": "leg_timeout_clamped",
                    "message": (
                        f"this leg was asked for up to {timeout:g} seconds, "
                        f"which is longer than the longest wall this route "
                        f"allows, so it was given {LEG_TIMEOUT_MAX:g} seconds "
                        f"instead"
                    ),
                }
            )
            timeout = LEG_TIMEOUT_MAX

    command = command_resolver()
    if not command:
        return 500, {
            "error": (
                "this sidecar has no guardkit command to run a leg with — set "
                f"{GUARDKIT_PATH_ENV} to its path, or put {GUARDKIT_BINARY_NAME} "
                "on the service's PATH"
            )
        }

    context_flags, context_warnings = _leg_context_flags(
        cwd=cwd,
        repo_path=repo_path,
        subcommand=str(subcommand),
        read_allowlist=read_allowlist,
    )
    context_warnings = [*timeout_warnings, *context_warnings]
    for path in extra_context_paths:
        context_flags.extend(["--context", path])
    nats_flag = ["--nats"] if with_nats_streaming else []
    argv = [command, str(subcommand), *args, *context_flags, *nats_flag]
    logger.info(
        "forge-deploy-sidecar: running guardkit %s in %s (up to %g seconds, "
        "%d context paths, progress %s)",
        subcommand,
        cwd,
        timeout,
        len(context_flags) // 2,
        "on" if with_nats_streaming else "off",
    )
    try:
        exit_code, stdout, stderr = leg_runner(argv=argv, cwd=cwd, timeout=timeout)
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
        # A leg that was stopped at its wall is not a leg that failed, and the
        # caller can only tell the two apart if this side says which happened.
        "timed_out": exit_code == MERGE_TIMEOUT_EXIT_CODE,
        "context_warnings": context_warnings,
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
                GIT_IS_ANCESTOR_ROUTE,
                GIT_CANDIDATE_TREE_ROUTE,
                GIT_CANDIDATE_TREE_REMOVE_ROUTE,
                GIT_WORKTREE_ADD_ROUTE,
                GIT_WORKTREE_REMOVE_ROUTE,
                GIT_WORKTREE_COMMIT_COUNT_ROUTE,
                RECEIPTS_EXPORT_ROUTE,
                STAMPS_EVIDENCE_ROUTE,
                GUARDKIT_LEG_ROUTE,
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
            elif route == GIT_IS_ANCESTOR_ROUTE:
                status, body = process_git_is_ancestor_request(payload, config=config)
            elif route == GIT_CANDIDATE_TREE_ROUTE:
                status, body = process_git_candidate_tree_request(
                    payload, config=config
                )
            elif route == GIT_CANDIDATE_TREE_REMOVE_ROUTE:
                status, body = process_git_candidate_tree_remove_request(
                    payload, config=config
                )
            elif route == GIT_WORKTREE_ADD_ROUTE:
                status, body = process_git_worktree_add_request(
                    payload, config=config
                )
            elif route == STAMPS_EVIDENCE_ROUTE:
                status, body = process_stamps_evidence_request(
                    payload,
                    config=config,
                    worktrees_root=self.server.worktrees_root,  # type: ignore[attr-defined]
                )
            elif route == GIT_WORKTREE_REMOVE_ROUTE:
                status, body = process_git_worktree_remove_request(
                    payload, config=config
                )
            elif route == GIT_WORKTREE_COMMIT_COUNT_ROUTE:
                status, body = process_git_worktree_commit_count_request(
                    payload, config=config
                )
            elif route == RECEIPTS_EXPORT_ROUTE:
                status, body = process_receipts_export_request(payload, config=config)
            elif route == GUARDKIT_LEG_ROUTE:
                status, body = process_guardkit_leg_request(
                    payload,
                    config=config,
                    leg_runner=self.server.merge_runner,  # type: ignore[attr-defined]
                )
            else:
                status, body = process_run_request(
                    payload,
                    config=config,
                    script_runner=self.server.script_runner,  # type: ignore[attr-defined]
                    command_runner=self.server.merge_runner,  # type: ignore[attr-defined]
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
    "sidecar_is_inside_sandbox",
    "SIDECAR_IN_SANDBOX_ENV",
    "allowed_env_keys",
    "process_run_request",
    "process_guardkit_merge_request",
    "GIT_WRITE_TREE_ROUTE",
    "GIT_READ_FILE_ROUTE",
    "GIT_REV_PARSE_ROUTE",
    "GIT_CHECK_NAMES",
    "GIT_CHECK_PATH_ARGS",
    "GIT_CHECK_TIMEOUT_DEFAULTS",
    "GIT_CHECK_BLOCKING_DEFAULTS",
    "NORMALIZER_CHECK_NAME",
    "resolve_normalizer_command_for_checks",
    "GIT_HOOK_TIMEOUT_SECONDS",
    "REF_NAME_PATTERN",
    "resolve_check_command",
    "run_declared_check",
    "process_git_write_tree_request",
    "process_git_read_file_request",
    "process_git_rev_parse_request",
    "GIT_WORKTREE_ADD_ROUTE",
    "GIT_WORKTREE_REMOVE_ROUTE",
    "GIT_WORKTREE_COMMIT_COUNT_ROUTE",
    "RECEIPTS_EXPORT_ROUTE",
    "STAMPS_EVIDENCE_ROUTE",
    "process_stamps_evidence_request",
    "SAFE_NAME_PATTERN",
    "process_git_worktree_add_request",
    "process_git_worktree_remove_request",
    "process_git_worktree_commit_count_request",
    "process_receipts_export_request",
    "GUARDKIT_LEG_ROUTE",
    "LEG_SUBCOMMANDS",
    "LEG_TIMEOUT_DEFAULT",
    "LEG_TIMEOUT_MAX",
    "LEG_MAX_PATHS",
    "process_guardkit_leg_request",
    "default_config_loader",
    "DeploySidecarHandler",
    "build_server",
    "serve",
]
