"""The publisher: it does one thing, and refuses everything else in plain words.

One-true-copy design pass, item 1, second revision section D, and the third
revision's section E. The publisher is its own small service, in its own
process, and it holds the one credential that can write to a project's remote.
Nothing that builds or checks can see that credential, because nothing that
builds or checks is in this process.

WHAT IT DOES. Given ``{project, build_id, turn, j_commit, target_branch}``:

1. it reads the publication record ITSELF, read-only, and refuses unless a
   record exists for that build whose turn equals the request's, whose joined
   commit equals the request's, whose target branch equals the request's, and
   whose two kinds of check are recorded as PASSED on exactly that joined
   commit. A ``done`` line is not enough: the line has to say it ran on this
   commit and its verdict has to be a pass;
2. it brings the joined commit out of the project's copy through the
   read-only git address it was given, never a writable path;
3. it checks for itself — not on anybody's word — that the joined commit is a
   merge commit whose parents are exactly the recorded G and the recorded
   build tip, and that G is part of the remote's target branch as it is NOW,
   so that sending moves the branch forwards and never sideways;
4. it sends the joined commit to the remote's recorded target branch with an
   ordinary, NON-FORCING push, using the one credential it holds;
5. it reads the remote back and answers whether the branch now CONTAINS the
   joined commit — not whether it IS it, because somebody else may add to the
   branch in the seconds between.

WHAT IT NEVER DOES. It writes nothing to the ledger, ever — the ledger is the
coordinator's record and the publisher is a reader of it. It never reads the
folder of exported records, which a sandbox can write. It never logs, answers
with, or hands a child its credential. It never forces a push: there is one
function that builds a push and it cannot express one. It never deploys
anything: what happens after a commit is on the remote is the executor's
stage, and this service has no part in it.

EVERY REFUSAL IS ONE SENTENCE a person can act on, and rides beside a short
machine-readable name so that the coordinator can tell the one refusal it
retries — the remote moved — from every other, which it does not.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from forge.publisher.credential import Credential, read_the_credential
from forge.publisher.git_work import (
    TheProjectsCommits,
    a_plain_branch_name,
    a_plain_commit_name,
)
from forge.publisher.settings import ProjectRoute, PublisherSettings
from forge.publisher.the_record import (
    STEP_CANDIDATE_CHECK,
    STEP_MERGE_CHECKS,
    TheLedgerIsUnreadable,
    TheRecord,
    TheRecordReader,
    a_step_that_passed_on,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PUBLISH_ROUTE",
    "Answer",
    "Publisher",
    "PublisherHandler",
    "serve",
    "the_remote_moved",
]

#: The one route that does anything.
PUBLISH_ROUTE: str = "/publish"

#: The short name on the refusal the coordinator retries: the remote moved
#: under this send, so a new join onto where it is now is the answer. Every
#: other refusal name is a stop.
THE_REMOTE_MOVED: str = "the-remote-moved"


def the_remote_moved(answer: dict[str, Any] | None) -> bool:
    """Is this the one refusal a new attempt is the answer to?"""
    if not isinstance(answer, dict):
        return False
    return str(answer.get("refusal_kind") or "") == THE_REMOTE_MOVED


@dataclass(frozen=True)
class Answer:
    """What the publisher answers, every time, whatever happened."""

    published: bool
    remote_now: str | None = None
    contains_j: bool = False
    refusal: str | None = None
    refusal_kind: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "published": bool(self.published),
            "remote_now": self.remote_now,
            "contains_j": bool(self.contains_j),
            "refusal": self.refusal,
            "refusal_kind": self.refusal_kind,
        }


def _refused(kind: str, sentence: str, **rest: Any) -> Answer:
    return Answer(published=False, refusal=sentence, refusal_kind=kind, **rest)


class Publisher:
    """One publisher: its settings, its credential, its ledger, its git.

    The credential is read ONCE here, when the publisher is made, and from
    the one named file its settings point at. Nothing re-reads it and nothing
    else supplies one.
    """

    def __init__(
        self,
        settings: PublisherSettings,
        *,
        reader: TheRecordReader | None = None,
        commits_for: Callable[[ProjectRoute], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._state = Path(settings.state_dir).expanduser()
        self._state.mkdir(parents=True, exist_ok=True)
        credential, refusal = read_the_credential(settings.credential_file)
        self._credential: Credential | None = credential
        self._no_credential: str | None = refusal.sentence if refusal else None
        if self._no_credential:
            logger.warning("publisher: %s", self._no_credential)
        else:
            logger.info(
                "publisher: one credential was read at start from its named "
                "file; it is never logged, never in an answer and never in a "
                "child's environment"
            )
        self._reader = reader or TheRecordReader(settings.ledger)
        self._commits_for = commits_for or self._its_own_copy
        # ONE PROJECT AT A TIME. The publisher keeps one repository of its own
        # per project, and git takes locks inside it: two requests for the
        # same project at the same moment would have one of them fail on a
        # lock and be reported as a refusal that never really happened.
        # Requests for DIFFERENT projects are unaffected — they are different
        # repositories and different locks.
        self._one_at_a_time: dict[str, threading.Lock] = {}
        self._handing_out_locks = threading.Lock()

    # -- the pieces --------------------------------------------------------

    @property
    def settings(self) -> PublisherSettings:
        return self._settings

    @property
    def holds_a_credential(self) -> bool:
        return self._credential is not None and self._credential.held

    def _the_lock_for(self, project: str) -> threading.Lock:
        with self._handing_out_locks:
            return self._one_at_a_time.setdefault(str(project), threading.Lock())

    def _its_own_copy(self, route: ProjectRoute) -> TheProjectsCommits:
        return TheProjectsCommits(
            route,
            state_dir=self._state,
            credential=self._credential,
            timeout_seconds=self._settings.git_timeout_seconds,
        )

    # -- the one thing it does ---------------------------------------------

    def publish(self, request: Any) -> Answer:
        """The whole of it, in the order the design writes. Never raises."""
        try:
            return self._publish(request)
        except Exception as exc:  # noqa: BLE001 - a refusal, never a crash
            logger.exception("publisher: a request ended in an unexpected error")
            return _refused(
                "the-publisher-could-not-finish",
                (
                    "the publisher stopped on an unexpected error "
                    f"({type(exc).__name__}) and sent nothing. Its log says "
                    "where."
                ),
            )

    def _publish(self, request: Any) -> Answer:
        # -- what was asked for --------------------------------------------
        if not isinstance(request, dict):
            return _refused(
                "the-request-made-no-sense",
                "the publisher was sent something that is not a request, so "
                "nothing was sent.",
            )
        project = str(request.get("project") or "").strip()
        build_id = str(request.get("build_id") or "").strip()
        j_commit = a_plain_commit_name(request.get("j_commit"))
        target_branch = a_plain_branch_name(request.get("target_branch"))
        turn = request.get("turn")
        if not project or not build_id:
            return _refused(
                "the-request-made-no-sense",
                "a request to publish has to name the project and the build; "
                "this one did not, so nothing was sent.",
            )
        if j_commit is None:
            return _refused(
                "the-request-made-no-sense",
                f"'{request.get('j_commit')}' is not a commit the publisher "
                "will put on a git command line, so nothing was sent.",
            )
        if target_branch is None:
            return _refused(
                "the-request-made-no-sense",
                f"'{request.get('target_branch')}' is not a branch name the "
                "publisher will put on a git command line, so nothing was sent.",
            )
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 1:
            return _refused(
                "the-request-made-no-sense",
                "a request to publish has to carry the turn number of the "
                "worker that made it; this one did not, so nothing was sent.",
            )

        # -- a project it was told about -----------------------------------
        route = self._settings.route(project)
        if route is None:
            return _refused(
                "the-project-is-not-one-of-mine",
                f"the publisher was not told how to reach the project "
                f"'{project}', so nothing was sent. Name its read-only source "
                "and its remote in the publisher's settings.",
            )

        # -- the record, read by the publisher itself ----------------------
        try:
            record = self._reader.read(build_id)
        except TheLedgerIsUnreadable as exc:
            return _refused("the-record-could-not-be-read", f"{exc}, so nothing was sent.")
        refusal = self._what_the_record_refuses(
            record,
            build_id=build_id,
            turn=int(turn),
            j_commit=j_commit,
            target_branch=target_branch,
        )
        if refusal is not None:
            return refusal

        g_commit = a_plain_commit_name(record.g_commit)
        build_tip = a_plain_commit_name(record.build_tip)
        if g_commit is None or build_tip is None:
            return _refused(
                "the-record-is-incomplete",
                f"the record for build {build_id} does not say which commit "
                "the join was made onto and which commit of the build was "
                "joined, so the publisher cannot check the joined commit for "
                "itself and sent nothing.",
            )

        # -- the credential ------------------------------------------------
        if not self.holds_a_credential:
            return _refused(
                "there-is-no-credential",
                f"{self._no_credential or 'the publisher holds no credential'} "
                "Nothing was sent.",
            )

        with self._the_lock_for(route.name):
            return self._the_git_part(
                route,
                build_id=build_id,
                j_commit=j_commit,
                g_commit=g_commit,
                build_tip=build_tip,
                target_branch=target_branch,
            )

    def _the_git_part(
        self,
        route: ProjectRoute,
        *,
        build_id: str,
        j_commit: str,
        g_commit: str,
        build_tip: str,
        target_branch: str,
    ) -> Answer:
        """Bring the commit out, look at it, send it, read the remote back.

        Held under this project's lock: the publisher has ONE repository of
        its own per project and git takes locks inside it.
        """
        logger.info(
            "publisher: build %s of %s — bringing %s out of the project's "
            "read-only copy and looking at it",
            build_id,
            route.name,
            str(j_commit)[:10],
        )
        commits = self._commits_for(route)

        # -- bring the joined commit out of the project's copy -------------
        brought = commits.bring_the_joined_commit_out(j_commit)
        if not brought.ok:
            return _refused("the-joined-commit-is-not-there", f"{brought.said}.")

        # -- look at the joined commit for itself --------------------------
        parents = commits.the_parents_of(j_commit)
        if len(parents) != 2:
            return _refused(
                "it-is-not-the-join",
                f"{j_commit[:10]} has {len(parents)} parent(s), so it is not "
                f"a join of {g_commit[:10]} and the build's tip "
                f"{build_tip[:10]}. Nothing was sent.",
            )
        if parents[0] != g_commit or parents[1] != build_tip:
            return _refused(
                "it-is-not-the-join",
                f"{j_commit[:10]} is a merge of {parents[0][:10]} and "
                f"{parents[1][:10]}, and the record says the join was made of "
                f"{g_commit[:10]} and the build's tip {build_tip[:10]}. "
                "Nothing was sent.",
            )

        # -- where the remote's branch is now, and is G part of it? ---------
        where = commits.where_the_remotes_branch_is(target_branch)
        if not where.ok:
            return _refused("the-remote-could-not-be-read", f"{where.said}.")
        remote_now = where.out
        already = commits.is_in(j_commit, remote_now)
        if already is True:
            # It is already there. Somebody's send landed and its answer was
            # lost; saying so is the truth, and saying it again is not a
            # second send.
            return Answer(published=True, remote_now=remote_now, contains_j=True)
        forwards = commits.is_in(g_commit, remote_now)
        if forwards is None:
            return _refused(
                "the-remote-could-not-be-read",
                f"git could not say whether {g_commit[:10]} is part of "
                f"'{target_branch}' on the remote named origin, so nothing "
                "was sent.",
                remote_now=remote_now,
            )
        if not forwards:
            return _refused(
                THE_REMOTE_MOVED,
                f"the join was made onto {g_commit[:10]}, and the branch "
                f"'{target_branch}' on the remote named origin is at "
                f"{remote_now[:10]}, which does not contain it. Sending "
                f"{j_commit[:10]} would move that branch sideways rather than "
                "forwards, so nothing was sent: the work has to be joined "
                "onto where the branch is now.",
                remote_now=remote_now,
            )
        # AND FORWARDS MEANS FORWARDS: what the branch is at now has to be
        # part of what is being sent. G being on the branch says the join was
        # made from this branch's own history; it does not say the branch has
        # not gained anything since. If it has, the send cannot move the
        # branch forwards — git itself would refuse it — and the answer is
        # the one the coordinator retries: join onto where the branch is now.
        onwards = commits.is_in(remote_now, j_commit)
        if onwards is None:
            return _refused(
                "the-remote-could-not-be-read",
                f"git could not say whether {remote_now[:10]} is part of "
                f"{j_commit[:10]}, so nothing was sent.",
                remote_now=remote_now,
            )
        if not onwards:
            return _refused(
                THE_REMOTE_MOVED,
                f"the branch '{target_branch}' on the remote named origin is "
                f"at {remote_now[:10]}, and the joined result "
                f"{j_commit[:10]} does not contain it: the branch has gained "
                "work since the join was made. Sending would not move the "
                "branch forwards, so nothing was sent — the work has to be "
                "joined onto where the branch is now.",
                remote_now=remote_now,
            )

        # -- the send ------------------------------------------------------
        sent = commits.send(j_commit, target_branch)
        if not sent.ok:
            after = commits.where_the_remotes_branch_is(target_branch)
            moved = after.ok and after.out != remote_now
            return _refused(
                THE_REMOTE_MOVED if moved else "the-remote-refused-the-send",
                (
                    f"the remote named origin refused the send of "
                    f"{j_commit[:10]} to '{target_branch}': {sent.said}. "
                    "Nothing is on the branch that was not there before."
                ),
                remote_now=after.out if after.ok else remote_now,
            )

        # -- read the remote back ------------------------------------------
        read_back = commits.where_the_remotes_branch_is(target_branch)
        if not read_back.ok:
            return _refused(
                "the-remote-could-not-be-read-back",
                f"the send was made, and then {read_back.said}. Whether the "
                "branch has it cannot be said from here.",
            )
        contains = commits.is_in(j_commit, read_back.out)
        if contains is not True:
            return _refused(
                "the-remote-does-not-have-it",
                f"the send reported success, but the branch '{target_branch}' "
                f"on the remote named origin is at {read_back.out[:10]} and "
                f"does not contain {j_commit[:10]}.",
                remote_now=read_back.out,
            )
        return Answer(published=True, remote_now=read_back.out, contains_j=True)

    # -- what the record refuses -------------------------------------------

    def _what_the_record_refuses(
        self,
        record: TheRecord,
        *,
        build_id: str,
        turn: int,
        j_commit: str,
        target_branch: str,
    ) -> Answer | None:
        """``None`` when the record allows this send; a refusal otherwise."""
        if not record.recorded:
            return _refused(
                "there-is-no-record",
                f"there is no publication record for build {build_id}, so "
                f"nothing is known to have been joined or checked. Nothing "
                "was sent.",
            )
        if int(record.turn) != int(turn):
            return _refused(
                "the-turn-has-moved-on",
                f"this request carries turn {turn} and build {build_id}'s "
                f"record is on turn {record.turn}: the worker that made this "
                "request has been replaced, so nothing was sent.",
            )
        if not record.j_commit or str(record.j_commit) != j_commit:
            return _refused(
                "that-is-not-the-recorded-join",
                f"this request asks for {j_commit[:10]}, and build "
                f"{build_id}'s record names "
                f"{(record.j_commit or 'nothing')[:10]} as its joined commit. "
                "Nothing was sent.",
            )
        if not record.target_branch or str(record.target_branch) != target_branch:
            return _refused(
                "that-is-not-the-recorded-branch",
                f"this request asks to send to '{target_branch}', and build "
                f"{build_id}'s record says this work is aimed at "
                f"'{record.target_branch or 'no branch at all'}'. Nothing was "
                "sent.",
            )
        for step, said in (
            (STEP_MERGE_CHECKS, "the build system's own checks after the join"),
            (STEP_CANDIDATE_CHECK, "the factory's own live check of the joined result"),
        ):
            if a_step_that_passed_on(record, step, j_commit) is None:
                return _refused(
                    "the-checks-are-not-recorded-as-passed",
                    f"build {build_id}'s record does not show {said} passing "
                    f"on {j_commit[:10]}. A step that was written down is not "
                    "a step that passed, and a step that passed on another "
                    "commit is not about this one. Nothing was sent.",
                )
        return None


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class _PublisherServer(ThreadingHTTPServer):
    """A loopback HTTP server carrying one publisher."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        publisher: Publisher,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.publisher = publisher


class PublisherHandler(BaseHTTPRequestHandler):
    """One route that does something, one that says the service is alive."""

    server_version = "forge-publisher/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        logger.info("publisher %s - %s", self.address_string(), fmt % args)

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            if self.path.split("?", 1)[0] == "/healthz":
                self._answer(200, {"status": "healthy"})
                return
            self._answer(404, {"error": f"no such path: {self.path}"})
        except Exception:  # noqa: BLE001 - never crash the service
            logger.exception("publisher: a GET ended in an error")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            if self.path.split("?", 1)[0] != PUBLISH_ROUTE:
                self._answer(404, {"error": f"no such path: {self.path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                request = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError) as exc:
                self._answer(
                    400,
                    Answer(
                        published=False,
                        refusal=f"the request was not readable as JSON ({exc}), "
                        "so nothing was sent.",
                        refusal_kind="the-request-made-no-sense",
                    ).to_wire(),
                )
                return
            answer = self.server.publisher.publish(request)  # type: ignore[attr-defined]
            self._answer(200, answer.to_wire())
        except Exception:  # noqa: BLE001 - never crash the service
            logger.exception("publisher: a request ended in an error")
            try:
                self._answer(
                    500,
                    Answer(
                        published=False,
                        refusal="the publisher stopped on an unexpected error "
                        "and sent nothing.",
                        refusal_kind="the-publisher-could-not-finish",
                    ).to_wire(),
                )
            except Exception:  # noqa: BLE001 - the socket is already gone
                pass


def serve(
    settings: PublisherSettings, *, publisher: Publisher | None = None
) -> tuple[_PublisherServer, threading.Thread]:
    """Bind the publisher on loopback and serve it on a thread of its own.

    ``port`` 0 in the settings means "let the kernel pick", which is what a
    test and a bench want; the bound port is on ``server.server_address``.
    """
    made = publisher or Publisher(settings)
    server = _PublisherServer(
        (settings.host, int(settings.port)), PublisherHandler, publisher=made
    )
    thread = threading.Thread(
        target=server.serve_forever, name="forge-publisher", daemon=True
    )
    thread.start()
    return server, thread
