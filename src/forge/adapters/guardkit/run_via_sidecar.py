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

Sandbox first (2026-09-07, rule 75) adds a SECOND door beside it, with the
same posture: :func:`build_sidecar_leg_run` carries a fix journey's two legs
(``task-review`` and ``task-work``) to the deploy sidecar inside a
repository's sandbox, with the journey worktree as their working directory,
so that the repository's own code is installed and run there and never on the
host. It is a separate builder rather than a widening of the merge door,
because each door should be exactly as wide as the work it carries.

The pre-merge baseline — the list of tests the target branch was already
failing — is sent inline rather than as a path. The executor writes that file
inside the container, where the sidecar cannot read it; the sidecar writes its
own copy on the host from the list this module sends.

The branch to be merged travels the same way, as its own field, for the same
reason the two time limits do: the sidecar builds the command itself, so a
flag left in the argument list here would simply be dropped. It was, and on
2026-09-10 a fix journey's repair passed every check inside the sandbox and
then had its merge refused — "branch autobuild/FEAT-39F6 does not exist" —
because the door had never been taught the journey's own branch. The name sent
is the one the caller was given (the merge executor takes it from
:func:`forge.pipeline.merge_offer.branch_to_merge`, the estate's single answer
to which branch the merge word merges); this module never derives a branch
name of its own, so it can never disagree with the offer, the candidate check
or the landed-merge detection. No branch in the argument list means no branch
in the request, and the merge command derives the feature's own branch exactly
as it always has.

WHAT THESE TWO DOORS DO NOT CARRY, said plainly (27 September 2026, the
seventh review, correcting a record that claimed more than was built). The
deploy stage stamps every request it sends the helper with the build it is for
and the commit the coordinator recorded that build as starting from, so the
helper reads that project's own declaration files THERE. These two doors do
not: their callables are built per ADDRESS, once, rather than per build, and
the requests carry the memory name and the declared setting names but no build
and no commit. The helper reads them as by-hand runs and takes the project's
declarations from the committed HEAD of the copy it has — which is exactly
what they did before the binding existed, so nothing got worse. Nothing here
chooses its own authority either: there is no commit on these requests for the
helper to honour. But it is the one route left by which a build could widen
its own door by COMMITTING a line and then asking for a merge or a leg, and
closing it means threading the build and its recorded starting commit down to
these two calls — a change to the merge path, deliberately not made in the
same pass as the binding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning
from forge.adapters.guardkit.parser import parse_guardkit_output

logger = logging.getLogger(__name__)

#: The sidecar operation this module talks to.
MERGE_ENDPOINT: str = "/guardkit-merge"

#: The only subcommand this door will carry.
ALLOWED_SUBCOMMAND: str = "autobuild"

#: The only verb of that subcommand this door will carry.
ALLOWED_VERB: str = "merge"

#: The branch this door merged into before the merge word had a join, and the
#: one it still merges into when a caller names no other.
ALLOWED_TARGET: str = "main"

#: The one other family of branches this door carries (22 September 2026): the
#: factory's own integration branches. The merge word now joins the build's
#: work onto the commit the remote is at, on a branch of the factory's own, in
#: a working folder of its own — so the target travels as its own field and
#: the folder with it. Anything outside these two is refused here rather than
#: sent, because a door that merges into whatever it is told is a door that
#: can be told to merge into somebody's own branch.
INTEGRATION_TARGET_PREFIX: str = "factory-integration/"


def _target_is_carried(target: str) -> bool:
    """Is this a branch this door will merge into?"""
    return target == ALLOWED_TARGET or target.startswith(INTEGRATION_TARGET_PREFIX)

#: The sidecar operation the fix journey's legs talk to (rule 75).
LEG_ENDPOINT: str = "/guardkit-leg"

#: The only subcommands the leg door will carry.
LEG_SUBCOMMANDS: tuple[str, ...] = ("task-review", "task-work")

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


class LegCallRefused(ValueError):
    """The caller asked the leg door for something it does not carry.

    Raised — never returned as a result — for the same reason
    :class:`MergeCallRefused` is: it means a caller tried to run something
    other than a fix journey's own legs inside the sandbox. That is a
    programming mistake to fix, not a leg outcome to report.
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


def _resolve_repo_key_for_worktree(
    worktree: Path, repo_paths: Mapping[str, str]
) -> str | None:
    """Return the org/name key of the repository ``worktree`` lives inside.

    A journey worktree is under the repository, not equal to it (it is
    ``<repo>/.forge/worktrees/<build id>``), so this is containment rather
    than the equality :func:`_resolve_repo_key` uses for the merge's repo
    path. The longest matching root wins, so a repository nested inside
    another's directory is still resolved to itself.
    """
    wanted = worktree.resolve(strict=False)
    best: tuple[int, str] | None = None
    for key, configured in repo_paths.items():
        root = Path(configured).resolve(strict=False)
        if wanted == root or root in wanted.parents:
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, key)
    return best[1] if best else None


def _failed_result(
    *,
    detail: str,
    duration_secs: float,
    warning_code: str,
    subcommand: str | None = None,
) -> GuardKitResult:
    """A failure this side of the wire, in the shape the executor reads."""
    return GuardKitResult(
        status="failed",
        subcommand=subcommand or f"{ALLOWED_SUBCOMMAND} {ALLOWED_VERB}",
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
        memory_project: str | None = None,
        launch_settings: Sequence[str] | None = None,
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
        if target is not None and not _target_is_carried(target):
            raise MergeCallRefused(
                "the deploy sidecar merges into "
                f"{ALLOWED_TARGET!r} or one of the factory's own "
                f"{INTEGRATION_TARGET_PREFIX}* branches, and into nothing "
                f"else; it was asked to merge into {target!r}"
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
        # WHICH MEMORY THE MERGE WORD'S OWN COMMAND BELONGS TO, and what else
        # this project asked to be launched with. The merge command runs the
        # build system inside the sandbox, so it reads and writes memory like
        # any other leg, and it uses the name recorded for this build rather
        # than whatever the checkout on the far side declares.
        if memory_project:
            body["memory_project"] = str(memory_project)
        if launch_settings:
            body["launch_settings"] = [str(name) for name in launch_settings]
        if body.get("memory_project") or body.get("launch_settings"):
            # AND THIS DOOR CITES NO BUILD RECORD, AND SAYS SO (23 September
            # 2026, the eighth review). The helper refuses a request that asks
            # for a project's declarations to be read and names neither a
            # build nor a commit, because that is what a dropped coordinator
            # stamp looks like. This door has never carried the build id onto
            # the wire, so it claims what it is really asking for: read them
            # at the committed HEAD of the copy you have. CARRY IT FORWARD:
            # the merge word does know which build it is for, and stamping
            # this request with it would bind these names to the recorded
            # commit the way the deploy stage's requests are bound.
            body["by_hand"] = True
        # THE BRANCH TRAVELS AS ITS OWN FIELD. The sidecar builds the command
        # on the far side, so a --branch left in this list would be dropped
        # and a fix journey's repair would be merged from a branch nobody
        # made. What is sent is exactly what the caller put on the command
        # line — the executor takes it from merge_offer.branch_to_merge — and
        # this door never makes up a name of its own. No flag means no field,
        # and the merge command derives the feature's own branch as before.
        branch = _flag_value(args, "--branch")
        named_a_branch = any(
            token == "--branch" or token.startswith("--branch=") for token in args
        )
        if named_a_branch and not (branch or "").strip():
            raise MergeCallRefused(
                "the merge command's --branch needs the name of the branch to "
                f"merge after it; got {args!r}"
            )
        if branch is not None:
            body["branch"] = branch
        # THE TARGET AND THE FOLDER TRAVEL AS THEIR OWN FIELDS, for the same
        # reason the branch does: the sidecar builds the command on the far
        # side, so a flag left in this list would simply be dropped. No
        # ``--target`` means no field, and the far side merges into main
        # exactly as it always has; no ``--in-worktree`` means no field, and
        # the merge happens where the command is run, as it always did.
        if target is not None:
            body["target"] = target
        in_worktree = _flag_value(args, "--in-worktree")
        named_a_folder = any(
            token == "--in-worktree" or token.startswith("--in-worktree=")
            for token in args
        )
        if named_a_folder and not (in_worktree or "").strip():
            raise MergeCallRefused(
                "the merge command's --in-worktree needs the path of the "
                f"working folder to merge in after it; got {args!r}"
            )
        if in_worktree is not None:
            body["in_worktree"] = in_worktree
        # THE TWO WALLS TRAVEL TOGETHER. ``timeout_seconds`` is the wall around
        # the whole command; ``--verify-timeout`` is how long ONE run of the
        # checks may take. The sidecar builds the command itself, so the inner
        # limit has to be carried across as its own field or it is lost and
        # the checks quietly fall back to guardkit's own default.
        verify_timeout = _flag_value(args, "--verify-timeout")
        if verify_timeout is not None:
            try:
                body["verify_timeout_seconds"] = int(verify_timeout)
            except ValueError:
                raise MergeCallRefused(
                    "--verify-timeout must be a whole number of seconds; got "
                    f"{verify_timeout!r}"
                ) from None
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


def _context_warnings(raw: Any) -> list[GuardKitWarning]:
    """Turn the sidecar's context-manifest warnings into result warnings.

    The manifest is read inside the sandbox, so anything it had to say — a
    missing manifest, a document outside the allowlist — reaches forge only
    if it rides the answer. Anything unreadable here is ignored rather than
    raised: a warning is never worth losing a leg's result over.
    """
    if not isinstance(raw, list):
        return []
    warnings: list[GuardKitWarning] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        message = entry.get("message")
        if isinstance(code, str) and isinstance(message, str):
            warnings.append(GuardKitWarning(code=code, message=message))
    return warnings


def build_sidecar_leg_run(
    *,
    base_url: str,
    repo_paths: Mapping[str, str],
    http_timeout_margin: float = HTTP_TIMEOUT_MARGIN_SECONDS,
) -> Callable[..., Awaitable[GuardKitResult]]:
    """Return a fix-journey ``guardkit_run`` that works through the sidecar.

    Rich's rule, 2026-09-07: nothing the factory runs on a repository runs on
    the host. A journey's review and work legs install and run the
    repository's own code, so for a repository that has a sandbox they run
    inside it, reached through that sandbox's deploy sidecar (sandbox first,
    rule 75).

    The door is two commands wide — ``task-review`` and ``task-work`` — and
    refuses anything else loudly, for the same reason the merge door is one
    command wide: no other guardkit call may wander through it.

    The working directory is the journey worktree. The conductor's dispatcher
    already passes that path as ``repo_path`` (it is the directory the leg
    runs in), so this door sends it as the request's ``cwd`` and the sidecar
    checks it is one of that repository's own journey worktrees before
    starting anything.

    **The leg is told what it would have been told in the container, and its
    answer is read the same way.** Rule 75 moves where a leg runs, not what
    it is given: the conductor's forward-context paths
    (``extra_context_paths``), the allowlist the repository's context
    manifest is filtered against (``read_allowlist`` — the sandbox reads the
    manifest itself, against the tree the leg runs in, because that tree may
    not exist in the forge container at all) and the progress switch
    (``with_nats_streaming``) all travel with the request, and the sidecar
    assembles the arguments in the same order
    :func:`forge.adapters.guardkit.run.run` assembles them. Coming back, the
    leg's output goes through the very parser the in-container path uses
    (:func:`forge.adapters.guardkit.parser.parse_guardkit_output`), so the
    artefacts it wrote, its findings block, the coach's score and a stop at
    its wall all survive the wire. Without that, a review leg that ran
    perfectly would be recorded as FAILED for emitting "no readable findings
    block".

    Args:
        base_url: Where the sandbox's deploy sidecar listens.
        repo_paths: ``planning.target_repo_paths``, used to work out which
            repository a worktree belongs to.
        http_timeout_margin: Seconds added to the leg's own wall before the
            socket gives up, so the sidecar's timeout always fires first.
    """
    endpoint = f"{base_url.rstrip('/')}{LEG_ENDPOINT}"
    known_paths = dict(repo_paths)

    async def run_leg_via_sidecar(
        *,
        subcommand: str,
        args: list[str],
        repo_path: Path,
        read_allowlist: list[Path] | None = None,
        timeout_seconds: int = 1800,
        with_nats_streaming: bool = True,
        extra_context_paths: list[str] | None = None,
        memory_project: str | None = None,
        launch_settings: Sequence[str] | None = None,
    ) -> GuardKitResult:
        started_at = time.monotonic()

        # THE DOOR IS TWO COMMANDS WIDE. Anything else is a mistake in the
        # caller, so it is raised rather than reported as a leg outcome.
        if subcommand not in LEG_SUBCOMMANDS:
            raise LegCallRefused(
                "the sandbox sidecar carries the fix journey's two legs "
                f"({', '.join(LEG_SUBCOMMANDS)}); it was asked to run "
                f"{subcommand!r}"
            )

        repo_key = _resolve_repo_key_for_worktree(repo_path, known_paths)
        if repo_key is None:
            known = ", ".join(sorted(known_paths)) or "(none configured)"
            return _failed_result(
                detail=(
                    f"the sandbox sidecar does not know which repository "
                    f"{repo_path} belongs to — it is not inside any of the "
                    "paths in planning.target_repo_paths. Known repositories: "
                    f"{known}"
                ),
                duration_secs=time.monotonic() - started_at,
                warning_code="sidecar_repo_not_configured",
                subcommand=subcommand,
            )

        # EVERYTHING THE LEG WOULD HAVE BEEN TOLD IN THE CONTAINER TRAVELS
        # WITH IT. Rule 75 moves where a leg runs, not what it is given: the
        # conductor's forward-context paths, the read allowlist the
        # repository's context manifest is filtered against (the sandbox
        # reads that manifest itself, against the tree the leg runs in, so
        # the flags are the manifest's own), and the progress switch.
        body: dict[str, Any] = {
            "repo": repo_key,
            "cwd": str(repo_path),
            "subcommand": subcommand,
            "args": list(args),
            "timeout_seconds": float(timeout_seconds),
            "with_nats_streaming": bool(with_nats_streaming),
        }
        if extra_context_paths:
            body["extra_context_paths"] = [str(path) for path in extra_context_paths]
        if read_allowlist:
            body["read_allowlist"] = [str(path) for path in read_allowlist]
        # WHICH MEMORY THIS LEG BELONGS TO, and what else its project asked to
        # be launched with, travel with the request exactly as the context does
        # (22 September 2026). The sandbox is a different machine's worth of
        # environment from the coordinator's, and the worktree the leg runs in
        # may declare a different name from the commit the work started from —
        # which is how a changed working copy came to choose the memory for a
        # call the factory made. The recorded name is sent; absent, the sandbox
        # runs the leg with memory explicitly off.
        if memory_project:
            body["memory_project"] = str(memory_project)
        if launch_settings:
            body["launch_settings"] = [str(name) for name in launch_settings]
        if body.get("memory_project") or body.get("launch_settings"):
            # AND THIS DOOR CITES NO BUILD RECORD, AND SAYS SO — the same
            # sentence as the merge door above, for the same reason, with the
            # same thing to carry forward: the fix journey's legs do know
            # which build they belong to.
            body["by_hand"] = True
        http_timeout = float(timeout_seconds) + http_timeout_margin
        try:
            status, parsed = await asyncio.to_thread(
                _post, endpoint, body, timeout=http_timeout
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return _failed_result(
                detail=(
                    f"the sandbox sidecar at {base_url} could not be reached, "
                    f"so the {subcommand} leg did not run: {exc}"
                ),
                duration_secs=time.monotonic() - started_at,
                warning_code="sidecar_unreachable",
                subcommand=subcommand,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _failed_result(
                detail=(
                    f"the sandbox sidecar at {base_url} answered something "
                    f"that was not JSON, so the {subcommand} leg's result is "
                    f"unknown: {exc}"
                ),
                duration_secs=time.monotonic() - started_at,
                warning_code="sidecar_bad_answer",
                subcommand=subcommand,
            )
        except Exception as exc:  # noqa: BLE001 — never raise past the boundary
            return _failed_result(
                detail=(
                    f"talking to the sandbox sidecar at {base_url} went wrong, "
                    f"so the {subcommand} leg's result is unknown: "
                    f"{type(exc).__name__}: {exc}"
                ),
                duration_secs=time.monotonic() - started_at,
                warning_code="sidecar_client_error",
                subcommand=subcommand,
            )

        duration = time.monotonic() - started_at
        if status != 200:
            detail = ""
            if isinstance(parsed, dict):
                detail = str(parsed.get("error") or "")
            return _failed_result(
                detail=(
                    f"the sandbox sidecar refused to run the {subcommand} leg "
                    f"(HTTP {status}): {detail or parsed!r}"
                ),
                duration_secs=duration,
                warning_code="sidecar_refused",
                subcommand=subcommand,
            )
        if (
            not isinstance(parsed, dict)
            or not isinstance(parsed.get("exit_code"), int)
            or isinstance(parsed.get("exit_code"), bool)
        ):
            return _failed_result(
                detail=(
                    "the sandbox sidecar's answer did not carry an exit code, "
                    f"so the {subcommand} leg's result is unknown: {parsed!r}"
                ),
                duration_secs=duration,
                warning_code="sidecar_bad_answer",
                subcommand=subcommand,
            )

        exit_code = int(parsed["exit_code"])
        stdout = parsed.get("stdout")
        stderr = parsed.get("stderr_tail")
        # THE LEG'S ANSWER IS READ, NOT THROWN AWAY. A leg says what it found
        # in its own output — the artefacts it wrote, its findings block, the
        # coach's score — and the conductor's dispatcher records a review leg
        # as FAILED when that block is missing. So the answer goes through
        # the same parser a leg's output goes through in the container, and
        # the sidecar's own "it was stopped at its wall" travels with it, so
        # a leg that timed out is reported as a timeout and not as a failure.
        result = parse_guardkit_output(
            subcommand=subcommand,
            stdout=stdout if isinstance(stdout, str) else "",
            stderr=stderr if isinstance(stderr, str) else "",
            exit_code=exit_code,
            duration_secs=duration,
            timed_out=parsed.get("timed_out") is True,
        )
        warnings = _context_warnings(parsed.get("context_warnings"))
        if warnings:
            return result.model_copy(
                update={"warnings": warnings + list(result.warnings)}
            )
        return result

    return run_leg_via_sidecar


__all__ = [
    "ALLOWED_SUBCOMMAND",
    "ALLOWED_TARGET",
    "ALLOWED_VERB",
    "HTTP_TIMEOUT_MARGIN_SECONDS",
    "MERGE_ENDPOINT",
    "TRANSPORT_EXIT_CODE",
    "MergeCallRefused",
    "LEG_ENDPOINT",
    "LEG_SUBCOMMANDS",
    "LegCallRefused",
    "build_sidecar_guardkit_run",
    "build_sidecar_leg_run",
]
