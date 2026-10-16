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
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from forge.deploy.candidate_tree import CANDIDATE_TREES_DIRNAME
from forge.executor.shell_steps import Finished, TimedOut

logger = logging.getLogger(__name__)

#: Exit code returned when the sidecar could not be reached or answered
#: malformed data. Distinct from a script's own exit code (which the sidecar
#: relays verbatim) so a transport failure is not mistaken for a script failure.
SIDECAR_TRANSPORT_EXIT_CODE = 1

#: The owned deploy executor's result words: lowercase words joined by dashes.
_EXECUTOR_WORD = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class NoAnswer(tuple):
    """``(exit code, sentence)`` for a request that got NO ANSWER at all.

    15 October 2026. The request could not be sent, timed out, had its
    connection dropped, or came back unreadable: the script may still be
    running in the sandbox, which no exit code can say. It unpacks exactly
    like every runner's ``(exit_code, output)``, so a caller that does not
    ask is unchanged; one that asks reads :func:`no_answer_came_back`.
    """

    answer_lost = True
    confirmed = False

    def __new__(cls, sentence: str) -> "NoAnswer":
        return super().__new__(cls, (SIDECAR_TRANSPORT_EXIT_CODE, sentence))

    def __getnewargs__(self) -> tuple[str]:
        # So a copy or a pickle is made from the sentence, as ``__new__`` takes.
        return (self[1],)


def no_answer_came_back(answer: object) -> bool:
    """Is ``answer`` a runner's answer saying no answer came back — or that
    the script was stopped at its time limit, which is read the same way
    (:class:`forge.executor.shell_steps.TimedOut`)?"""
    return getattr(answer, "answer_lost", False) is True


class SidecarScriptRunner:
    """A ``_run_script_step``-compatible callable that POSTs to the sidecar.

    Bound to one ``repo`` (org/name) and the sidecar ``base_url``. Each call maps
    the handler's ``(cwd, script, env_file, timeout, extra_env)`` to the sidecar
    ``/run`` contract ``{repo, script, env, timeout_seconds, cwd}`` and unpacks
    the ``{exit_code, output_tail}`` response.
    """

    def __init__(
        self,
        *,
        base_url: str,
        repo: str,
        build: str | None = None,
        start_commit: str | None = None,
        by_hand: bool = False,
        http_timeout_margin: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._repo = repo
        # ONE PLACE STAMPS EVERY REQUEST THIS BUILD SENDS (27 September 2026,
        # Codex's requirement of the 23rd). ``build`` and ``start_commit`` are
        # read off the COORDINATOR'S OWN LEDGER for this build, once, where
        # this runner is made — never off a step's parameters, a runbook or
        # anything else a caller composed. Every request this runner sends —
        # the candidate check, the promote, the read-only "what are you
        # running" question, the teardown — carries the same pair, so the far
        # side can confirm it with the coordinator before it reads a line of
        # this project's declarations.
        #
        # A runner made with neither is a BY-HAND runner: it stamps nothing,
        # and the far side reads at the committed HEAD of the copy it has and
        # says so. It does not forward a commit a caller hands it, because a
        # commit with no build behind it is exactly the authority a request
        # may not establish for itself, and forwarding one would only earn a
        # refusal further on.
        #
        # AND A BY-HAND RUNNER SAYS SO OUT LOUD (23 September 2026, the ninth
        # review). "No build" stopped being the quiet default at the far side:
        # a request that asks the project to widen its environment door and
        # says nothing about whose work it is looks exactly like a factory
        # request whose stamp was dropped, and is refused. So an attended run
        # makes its runner with ``by_hand=True`` and the claim travels on the
        # request. It is NEVER derived from "this runner has no build": that
        # would turn a dropped stamp back into a claim, which is the whole
        # thing being guarded against. Only a caller that knows a person asked
        # for this sets it.
        self._build = str(build or "").strip() or None
        self._start_commit = str(start_commit or "").strip() or None
        self._by_hand = bool(by_hand)
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
        memory_project: str | None = None,
        launch_settings: Sequence[str] | None = None,
        deploy: dict[str, object] | None = None,
        issued_env: dict[str, str] | None = None,
        repository: str | None = None,  # noqa: ARG002 — the far side knows its own
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
        # WHAT THE PROJECT DECLARED, AND WHICH MEMORY THIS WORK BELONGS TO, so
        # the far side can build the child's environment from the named list
        # rather than from whatever it happens to hold — the environment door
        # (23 September 2026).
        if memory_project:
            body["memory_project"] = str(memory_project)
        if launch_settings:
            body["launch_settings"] = [str(name) for name in launch_settings]
        # AND WHERE THE PROJECT SAID THEM: the build this request is for, and
        # the commit the coordinator's own ledger records that build as
        # starting from. Both are the stamp bound when this runner was made,
        # never anything a caller passed in here. The far side reads the
        # project's own declaration files at that commit rather than off the
        # working copy it runs the scripts out of — and, before it reads a
        # thing, it asks the coordinator whether that commit really is the one
        # recorded for that build.
        if self._build:
            body["build"] = self._build
        if self._start_commit:
            body["declared_at"] = self._start_commit
        elif self._by_hand and not self._build:
            body["by_hand"] = True
        # THE OWNERSHIP OF A DEPLOY OF THE LIVE THING. Present only on the leg
        # that changes the live target; its presence is what sends the request
        # through the far side's EXECUTOR rather than straight to a runner.
        if deploy:
            body["deploy"] = dict(deploy)
        # THE VALUES THE REVIEW PREVIEW ISSUED (13 October 2026), in a field of
        # their own and never inside ``env``: the far side checks where they
        # came from before a program is given them, and refuses a preview
        # name sent the ordinary way.
        if issued_env:
            body["issued_env"] = {str(k): str(v) for k, v in dict(issued_env).items()}
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
            # A 4xx/5xx from the sidecar (e.g. a deny-by-default refusal). It is
            # an ANSWER only when it carries the sidecar's own refusal sentence
            # (15 October 2026, Codex R5); an empty, unreadable or HTML body,
            # or a JSON object with no sentence, says nothing about whether the
            # script ran, so it is no answer. A body that says the script was
            # stopped at its time limit is read FIRST, whatever else it says.
            payload = sidecar_error_payload(exc)
            if says_it_timed_out(payload):
                return TimedOut(
                    SIDECAR_TRANSPORT_EXIT_CODE,
                    f"sidecar answered HTTP {exc.code}: the script was stopped "
                    "at its time limit",
                )
            # Only a refusal BEFORE running is an answer, and only a 4xx is
            # one: any 5xx, whatever its body, is no answer (15 October 2026).
            detail = refusal_sentence_in(payload) if 400 <= exc.code < 500 else None
            if detail is None:
                return NoAnswer(
                    f"sidecar answered HTTP {exc.code} with no refusal it "
                    "could be read as"
                )
            # A 4xx in the sidecar's own words: refused before running.
            return Finished(
                SIDECAR_TRANSPORT_EXIT_CODE,
                f"sidecar refused (HTTP {exc.code}): {detail}",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return NoAnswer(f"sidecar unreachable at {self._base_url}: {exc}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return NoAnswer(f"sidecar returned a non-JSON response: {exc}")
        except Exception as exc:  # noqa: BLE001 — never raise past the boundary
            logger.warning("sidecar script runner unexpected error: %s", exc)
            return NoAnswer(f"sidecar client error: {exc}")

        # TIMEOUT FIRST (15 October 2026, Codex round 3): a reply that says the
        # script was stopped at its time limit is a lost answer whatever else
        # is wrong with it — a missing or wrong cwd, an odd exit code.
        if says_it_timed_out(parsed):
            code = parsed.get("exit_code")
            if not isinstance(code, int) or isinstance(code, bool):
                code = SIDECAR_TRANSPORT_EXIT_CODE
            tail = parsed.get("output_tail", "")
            return TimedOut(code, tail if isinstance(tail, str) else str(tail))
        if not isinstance(parsed, dict) or "exit_code" not in parsed:
            return NoAnswer(f"sidecar response missing exit_code: {parsed!r}")
        # A DEPLOY THE EXECUTOR REFUSED is not a script that went red and not a
        # transport failure: nothing ran. It comes back as a non-zero exit with
        # the executor's own plain sentence, which is what the step records and
        # what a person reads, and the word beside it so a caller can tell the
        # refusals apart without reading English.
        if parsed.get("accepted") is False:
            # A REFUSAL IS CONFIRMED ONLY IN THE EXECUTOR'S OWN SHAPE (15
            # October 2026, Codex R16): a result word, a sentence, and an exit
            # code that is an integer or the executor's own "none" (nothing
            # ran). Anything less was not an answer anybody can rely on.
            word = parsed.get("word")
            sentence = parsed.get("sentence")
            refused_code = parsed.get("exit_code")
            if not (
                isinstance(word, str)
                and _EXECUTOR_WORD.match(word)
                and isinstance(sentence, str)
                and sentence.strip()
                and (
                    refused_code is None
                    or (isinstance(refused_code, int) and not isinstance(refused_code, bool))
                )
            ):
                return NoAnswer(f"sidecar answered a refusal of no known shape: {parsed!r}")
            return Finished(SIDECAR_TRANSPORT_EXIT_CODE, f"[{word}] {sentence}")
        exit_code = parsed.get("exit_code")
        output = parsed.get("output_tail", "")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return NoAnswer(f"sidecar returned a non-integer exit_code: {exit_code!r}")
        not_honoured = candidate_tree_not_honoured(cwd, parsed)
        if not_honoured is not None:
            logger.error("sidecar script runner: %s", not_honoured)
            return Finished(SIDECAR_TRANSPORT_EXIT_CODE, not_honoured)
        # A reply that passed every check: a complete answer (15 October 2026).
        return Finished(exit_code, output if isinstance(output, str) else str(output))


def says_it_timed_out(payload: object) -> bool:
    """Does a sidecar reply say its program was stopped at its time limit?

    Read FIRST by every client (15 October 2026): such a reply is a lost
    answer whatever else is wrong with it.
    """
    return isinstance(payload, dict) and payload.get("timed_out") is True


def sidecar_error_payload(exc: urllib.error.HTTPError) -> object:
    """The decoded body of an HTTP error, or ``None`` when it cannot be read."""
    try:
        return json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable body is no answer
        return None


def refusal_sentence_in(payload: object) -> str | None:
    """The sidecar's own refusal sentence in a decoded error body, or ``None``.

    15 October 2026 (Codex R5). Every sidecar refusal is a JSON object with a
    plain ``error`` sentence. Only that is an answer; anything else is
    ``None``, and the caller treats it as no answer at all.
    """
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, str) or not error.strip():
        return None
    return error


def sidecar_refusal_sentence(exc: urllib.error.HTTPError) -> str | None:
    """:func:`refusal_sentence_in` the body of ``exc``."""
    return refusal_sentence_in(sidecar_error_payload(exc))


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
    "NoAnswer",
    "SIDECAR_TRANSPORT_EXIT_CODE",
    "no_answer_came_back",
    "candidate_tree_not_honoured",
    "names_a_candidate_tree",
    "refusal_sentence_in",
    "says_it_timed_out",
    "sidecar_error_payload",
    "sidecar_refusal_sentence",
]
