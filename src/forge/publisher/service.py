"""The publisher: it does one thing, and refuses everything else in plain words.

One-true-copy design pass, item 1, second revision section D, and the third
revision's section E. The publisher is its own small service, in its own
process, and it holds the one credential that can write to a project's remote.
Nothing that builds or checks can see that credential, because nothing that
builds or checks is in this process.

WHAT IT DOES. Given ``{project, build_id, turn, j_commit, target_branch}``:

1. it reads the publication record ITSELF, read-only, and refuses unless a
   record exists for that build whose PROJECT equals the request's, whose
   turn equals the request's, whose joined commit equals the request's, whose
   target branch equals the request's, and whose two kinds of check are
   recorded as PASSED on exactly that joined commit. The project is asked
   first and it is asked of the record, not of the request: a build's name is
   not a project's name, and a record belonging to another project would put
   one project's work on another project's remote. A ``done`` line is not
   enough either: the line has to say it ran on this commit and its verdict
   has to be a pass;
2. it brings the joined commit out of the project's copy through the
   read-only git address it was given, never a writable path;
3. it checks for itself — not on anybody's word — that the joined commit is a
   merge commit whose parents are exactly the recorded G and the recorded
   build tip, and that G is part of the remote's target branch as it is NOW,
   so that sending moves the branch forwards and never sideways;
3a. it looks at the commits the send would add, and refuses when they bring
   in one of the factory's own build records, move or copy one elsewhere, or
   add a file version naming one of the factory's private folders (14
   October 2026, design "Factory records never enter a project's commits").
   A look that cannot be made refuses too. It never removes anything;
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

ONE CREDENTIAL PER ROUTE (13 October 2026). A project's route may name a key
of its own (:mod:`forge.publisher.settings`). Each distinct file is read once,
at start, into one cache: ``"publisher-wide"`` for the existing file and
``"route:<name>"`` for each route key. Two entries that are the same file —
by ``(st_dev, st_ino)``, not by path text — stop the publisher before anything
is read. A route that names a key is sent with that key or refused: there is
no fallback to another credential, ever. A route key's identity is looked at
again (``lstat``, never opening it) on every health self-check and
immediately before each send for that route.

ONLY PRIVATE REPOSITORIES, WHEN TOLD SO (17 October 2026; design "Building
production features from a signed-off prototype", section 2.8). With
``private_only`` in its settings (off by default, which is today's
behaviour exactly), every route's repository must be confirmed private
(:mod:`forge.publisher.private_remote`: the route's own key reads it, and an
anonymous lookup does not find it). That is asked once at start-up, when a
route with a known answer other than "private" is refused, with its reason,
until the publisher is restarted, while every other route works (a lookup
that merely failed is logged and left to the next check); and asked again
immediately before every push, which any failure refuses.

EVERY REFUSAL IS ONE SENTENCE a person can act on, and rides beside a short
machine-readable name so that the coordinator can tell the one refusal it
retries — the remote moved — from every other, which it does not.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from forge.publisher.credential import (
    Credential,
    read_the_credential,
    the_askpass_program,
)
from forge.publisher.git_work import (
    TheProjectsCommits,
    a_plain_branch_name,
    a_plain_commit_name,
)
from forge.publisher.private_remote import (
    LOOKUP_FAILED,
    Visibility,
    anonymous_lookup,
    confirmed_private,
)
from forge.publisher.settings import (
    ProjectRoute,
    PublisherSettings,
    the_route_key_file,
)
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
    "PUBLISHER_WIDE",
    "Answer",
    "Publisher",
    "PublisherWillNotStart",
    "PublisherHandler",
    "serve",
    "the_remote_moved",
]

#: The one route that does anything.
PUBLISH_ROUTE: str = "/publish"

#: The cache key of the publisher-wide credential. A route's own key is
#: ``"route:<name>"``.
PUBLISHER_WIDE: str = "publisher-wide"


class PublisherWillNotStart(RuntimeError):
    """The publisher refuses to start, in one sentence naming the settings.

    Raised when two credential entries are the same file. Never carries a
    credential's contents: it is raised before anything is read.
    """


def _route_key(name: str) -> str:
    return f"route:{name}"


#: The short name on the refusal the coordinator retries: the remote moved
#: under this send, so a new join onto where it is now is the answer. Every
#: other refusal name is a stop.
THE_REMOTE_MOVED: str = "the-remote-moved"


#: The refusal said when a route's key is not the file read at start.
THE_ROUTE_KEY_CHANGED: str = "the-route-credential-changed"

#: The refusals of the looks before a send (design "Factory records never
#: enter a project's commits"). None is retried: the way forward is a fresh
#: build, or a product file with the private folder name taken out.
IT_HOLDS_FACTORY_RECORDS: str = "it-holds-factory-records"
IT_HOLDS_PRIVATE_PATHS: str = "it-holds-private-paths"
THE_COMMITS_COULD_NOT_BE_CHECKED: str = "the-commits-could-not-be-checked"

#: The refusal when, with ``private_only`` on, a route's repository is not
#: confirmed private (17 October 2026). Not retried.
NOT_CONFIRMED_PRIVATE: str = "the-remote-is-not-confirmed-private"


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
        visibility_lookup: Callable[[str, str], int | None] | None = None,
    ) -> None:
        self._settings = settings
        self._state = Path(settings.state_dir).expanduser()
        self._state.mkdir(parents=True, exist_ok=True)
        # ONE CACHE, keyed by WHICH credential, never by path text: the
        # publisher-wide file and each distinct route key name, each read
        # once, here, and nowhere else.
        self._credentials: dict[str, Credential | None] = {}
        self._refusals: dict[str, str | None] = {}
        #: The (st_dev, st_ino) of each route key, recorded with ``lstat``
        #: before it was read. Looked at again before each send and on every
        #: health self-check, never by opening the file.
        self._identities: dict[str, tuple[int, int]] = {}
        #: With it, the file's type and its inode-change time. An inode
        #: number freed by a deleted file is commonly handed straight to the
        #: next file made, so (st_dev, st_ino) alone can call a replacement
        #: "the same file"; a replacement also has a new change time, and a
        #: link a different type.
        self._looked: dict[str, tuple[int, int]] = {}
        self._look_before_reading()
        self._read_each_credential_once()
        credential = self._credentials.get(PUBLISHER_WIDE)
        self._credential: Credential | None = credential
        self._no_credential: str | None = self._refusals.get(PUBLISHER_WIDE)
        if self._no_credential:
            logger.warning("publisher: %s", self._no_credential)
        else:
            logger.info(
                "publisher: one credential was read at start from its named "
                "file; it is never logged, never in an answer and never in a "
                "child's environment"
            )
        for key, held in sorted(self._credentials.items()):
            if held is None or not held.held:
                continue
            # AT START, ONCE. The program git asks for the credential through
            # is written here rather than on the way into each git command:
            # this publisher runs requests for different projects at the same
            # time, and a program being rewritten under a running git command
            # is a program that command can read half of. Writing it now means
            # every later call finds it already there. Each credential has a
            # program of its own (Credential.program_name).
            the_askpass_program(held, state_dir=self._state)
        for key in sorted(k for k in self._refusals if k != PUBLISHER_WIDE):
            if self._refusals[key]:
                logger.warning(
                    "publisher: %s: %s", self._who_names(key), self._refusals[key]
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
        #: The self-check, set by the process entry point
        #: (:func:`forge.publisher.__main__.why_it_will_not_start`) and run
        #: again on every health request. ``None`` means it was never set.
        self.self_check: Callable[[], str | None] | None = None
        self._handing_out_locks = threading.Lock()
        # ONLY PRIVATE REPOSITORIES, when the settings say so (17 October
        # 2026). The anonymous lookup is injectable for the tests. Each
        # route is asked once here; one with a KNOWN answer that is not
        # "private" (public, not on GitHub, its key cannot read it) is
        # refused until the publisher is restarted, and the others work. A
        # lookup that failed (403, a rate limit, a server error, no answer)
        # is passing trouble, so it is only logged: the check immediately
        # before the push decides (coach review, 17 October 2026).
        self._visibility_lookup = visibility_lookup or anonymous_lookup
        self._not_private: dict[str, str] = {}
        if settings.private_only:
            for name, route in sorted(settings.projects.items()):
                answer = self._is_it_private(route)
                if answer.private:
                    logger.info(
                        "publisher: project '%s' is confirmed private, so it is served",
                        name,
                    )
                    continue
                if answer.kind == LOOKUP_FAILED:
                    logger.warning(
                        "publisher: project '%s' could not be confirmed private at "
                        "start (%s); it is checked again before every push",
                        name,
                        answer.reason,
                    )
                    continue
                self._not_private[name] = str(answer.reason)
                logger.warning(
                    "publisher: project '%s' is refused until the publisher is "
                    "restarted: %s",
                    name,
                    answer.reason,
                )

    # -- the credentials, read once ----------------------------------------

    def _route_key_names(self) -> list[str]:
        return sorted(
            {
                route.credential_name
                for route in self._settings.projects.values()
                if route.credential_name is not None
            }
        )

    def _who_names(self, key: str) -> str:
        """``project 'a'`` or ``projects 'a', 'b'``: the routes using a key."""
        name = key.split(":", 1)[1] if key.startswith("route:") else None
        projects = sorted(
            route.name
            for route in self._settings.projects.values()
            if route.credential_name == name
        )
        listed = ", ".join(f"'{project}'" for project in projects)
        return f"project{'s' if len(projects) > 1 else ''} {listed}"

    def _look_before_reading(self) -> None:
        """Record each credential file's identity, and refuse aliases.

        The publisher-wide file is looked at with ``stat``, which follows a
        symbolic link: it may be one (the start-up check allows it), and its
        identity is then the file it really reads. A route key is looked at
        with ``lstat``: it may never be a link. Two entries with one identity
        are one file, and the publisher will not start — before anything is
        read.
        """
        seen: dict[tuple[int, int], tuple[str, str]] = {}
        wide = str(self._settings.credential_file or "").strip()
        if wide:
            where = Path(wide).expanduser()
            try:
                found = os.stat(where)
            except OSError:
                found = None  # the read below says why, as it always has
            if found is not None:
                seen[(found.st_dev, found.st_ino)] = (
                    "the publisher-wide credential_file",
                    str(where),
                )
        for name in self._route_key_names():
            key = _route_key(name)
            where = the_route_key_file(name)
            try:
                found = os.lstat(where)
            except FileNotFoundError:
                self._refusals[key] = (
                    f"the credential file {where} is not there, so the "
                    "publisher holds no credential for this route and can "
                    "send nothing for it."
                )
                continue
            except OSError as exc:
                self._refusals[key] = (
                    f"the credential file {where} could not be looked at "
                    f"({exc.strerror or type(exc).__name__}), so the publisher "
                    "holds no credential for this route and can send nothing "
                    "for it."
                )
                continue
            identity = (found.st_dev, found.st_ino)
            said = (f"the route key '{name}' of {self._who_names(key)}", str(where))
            if identity in seen:
                other, other_path = seen[identity]
                raise PublisherWillNotStart(
                    f"{other} ({other_path}) and {said[0]} ({said[1]}) are "
                    "the same file. Every credential has to be a file of its "
                    "own, so that a route's key is never another's; nothing "
                    "was read."
                )
            seen[identity] = said
            self._identities[key] = identity
            self._looked[key] = (stat.S_IFMT(found.st_mode), found.st_ctime_ns)

    def _read_each_credential_once(self) -> None:
        credential, refusal = read_the_credential(self._settings.credential_file)
        self._credentials[PUBLISHER_WIDE] = credential
        self._refusals[PUBLISHER_WIDE] = refusal.sentence if refusal else None
        for name in self._route_key_names():
            key = _route_key(name)
            if key not in self._identities:
                self._credentials[key] = None  # its refusal is already said
                continue
            credential, refusal = read_the_credential(
                the_route_key_file(name),
                no_follow=True,
                identity=self._identities[key],
                own_program=True,
            )
            self._credentials[key] = credential
            self._refusals[key] = refusal.sentence if refusal else None

    def _the_credential_for(
        self, route: ProjectRoute
    ) -> tuple[Credential | None, str | None]:
        """THE ONE PLACE a route's credential is picked. No fallback, ever.

        A route that names a key gets that key or that key's refusal, and
        nothing else. A route that names none gets the publisher-wide
        credential and its refusal, as before.
        """
        if route.credential_name is None:
            return self._credential, self._no_credential
        key = _route_key(route.credential_name)
        credential = self._credentials.get(key)
        refusal = self._refusals.get(key)
        if credential is None or not credential.held:
            return None, (
                f"the route for project '{route.name}' names its own key "
                f"'{route.credential_name}': "
                + (refusal or "the publisher holds no credential from it.")
            )
        return credential, None

    def why_route_keys_refuse_to_start(self) -> str | None:
        """A route key that could not be read at start, or ``None``.

        A route that names a key can never reach a send without it, so the
        process entry point does not start while this says anything.
        """
        said = [
            f"{self._who_names(key)}: {self._refusals[key]}"
            for key in sorted(self._refusals)
            if key != PUBLISHER_WIDE and self._refusals[key]
        ]
        return "; ".join(said) or None

    def _what_changed_under(self, name: str) -> str | None:
        """Is the route key still the very file that was read? ``lstat`` only."""
        key = _route_key(name)
        recorded = self._identities.get(key)
        if recorded is None:
            return None  # never read; its refusal stands on its own
        where = the_route_key_file(name)
        try:
            found = os.lstat(where)
        except OSError as exc:
            return (
                f"the credential file {where} named for {self._who_names(key)} "
                f"is no longer there ({exc.strerror or type(exc).__name__})"
            )
        if (found.st_dev, found.st_ino) != recorded or (
            stat.S_IFMT(found.st_mode),
            found.st_ctime_ns,
        ) != self._looked.get(key):
            return (
                f"the credential file {where} named for {self._who_names(key)} "
                "has changed since the publisher started (it was replaced, "
                "made a link, or its permissions or owner changed). The "
                "publisher must be restarted after a key file is changed"
            )
        return None

    def why_a_route_key_changed(self) -> str | None:
        """Every route key looked at again, never opened. For the health check."""
        said = [
            changed
            for changed in (self._what_changed_under(n) for n in self._route_key_names())
            if changed
        ]
        return "; ".join(said) or None

    # -- only private repositories (17 October 2026) ----------------------

    def _is_it_private(self, route: ProjectRoute, commits: Any = None) -> Visibility:
        """The confirmed-private test for one route, with its own key only."""
        credential, _refusal = self._the_credential_for(route)
        held = credential is not None and credential.held

        def key_reads() -> bool:
            if not held:
                return False
            copy = commits if commits is not None else self._commits_for(route)
            return bool(copy.the_key_reads_the_remote().ok)

        return confirmed_private(
            route.remote, key_reads_the_remote=key_reads, lookup=self._visibility_lookup
        )

    def why_routes_are_not_private(self) -> dict[str, str]:
        """With ``private_only`` on: each route refused at start-up, and why."""
        return dict(self._not_private)

    def _not_confirmed_private(self, route: ProjectRoute, commits: Any) -> Answer | None:
        """Immediately before a push, with ``private_only`` on: is it still
        confirmed private? ``None`` when it is, or when the setting is off."""
        if not self._settings.private_only:
            return None
        answer = self._is_it_private(route, commits)
        if answer.private:
            return None
        logger.warning("publisher: %s: %s; nothing was sent", route.name, answer.reason)
        return _refused(
            NOT_CONFIRMED_PRIVATE,
            f"the route for project '{route.name}': {answer.reason}. The "
            "publisher publishes only to private repositories, so nothing was "
            "sent.",
        )

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
        # A project's copy is handed ITS OWN route's credential and no other.
        credential, _refusal = self._the_credential_for(route)
        return TheProjectsCommits(
            route,
            state_dir=self._state,
            credential=credential,
            timeout_seconds=self._settings.git_timeout_seconds,
            known_hosts=self._settings.known_hosts_file,
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
        if route.name in self._not_private:
            return _refused(
                NOT_CONFIRMED_PRIVATE,
                f"the route for project '{route.name}' was refused when the "
                f"publisher started: {self._not_private[route.name]}. The "
                "publisher publishes only to private repositories; restart it "
                "once the repository is private and its key reads it. Nothing "
                "was sent.",
            )

        # -- the record, read by the publisher itself ----------------------
        try:
            record = self._reader.read(build_id)
        except TheLedgerIsUnreadable as exc:
            return _refused("the-record-could-not-be-read", f"{exc}, so nothing was sent.")
        refusal = self._what_the_record_refuses(
            record,
            project=project,
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

        # -- the credential: this route's own, and no other -----------------
        credential, no_credential = self._the_credential_for(route)
        if credential is None or not credential.held:
            return _refused(
                "there-is-no-credential",
                f"{no_credential or 'the publisher holds no credential'} "
                "Nothing was sent.",
            )

        with self._the_lock_for(route.name):
            changed = self._a_route_key_that_changed(route)
            if changed is not None:
                return changed
            return self._the_git_part(
                route,
                build_id=build_id,
                j_commit=j_commit,
                g_commit=g_commit,
                build_tip=build_tip,
                target_branch=target_branch,
            )

    def _a_route_key_that_changed(self, route: ProjectRoute) -> Answer | None:
        """Immediately before a send: is this route's key the file read?"""
        if route.credential_name is None:
            return None
        changed = self._what_changed_under(route.credential_name)
        if changed is None:
            return None
        logger.warning("publisher: %s; nothing was sent", changed)
        return _refused(
            THE_ROUTE_KEY_CHANGED,
            f"the route for project '{route.name}': {changed}, so nothing was "
            "sent with it and no other credential was tried. Nothing was sent.",
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

        # -- what must not be published -----------------------------------
        kept_back = self._what_must_not_be_published(
            commits, remote_now=remote_now, j_commit=j_commit, target_branch=target_branch
        )
        if kept_back is not None:
            return kept_back

        # -- the send ------------------------------------------------------
        # Looked at once more, immediately before the push, so the window in
        # which a route's key could be swapped is as short as it can be.
        changed = self._a_route_key_that_changed(route)
        if changed is not None:
            return changed
        not_private = self._not_confirmed_private(route, commits)
        if not_private is not None:
            return not_private
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

    # -- what must not be published ---------------------------------------

    def _what_must_not_be_published(
        self,
        commits: Any,
        *,
        remote_now: str,
        j_commit: str,
        target_branch: str,
    ) -> Answer | None:
        """``None`` when the commits the send adds may be published; a
        refusal otherwise. Looks a to c (records) are said before look d
        (private folder names). Nothing is ever removed or rewritten."""
        findings = commits.what_must_not_be_published(
            remote_now,
            j_commit,
            private_prefixes=self._settings.private_path_prefixes,
        )
        if findings is None:
            return _refused(
                THE_COMMITS_COULD_NOT_BE_CHECKED,
                f"The commits {j_commit[:10]} would add to '{target_branch}' "
                "could not be checked for the factory's own build records and "
                "private folder names. Nothing was sent.",
                remote_now=remote_now,
            )
        records = [f for f in findings if not f.private_paths]
        private = [f for f in findings if f.private_paths]
        if records:
            paths = {path for f in records for path in f.paths}
            count = len(paths) or sum(f.count for f in records)
            logger.warning(
                "publisher: %s holds %d of the factory's own records (looks %s); "
                "nothing was sent",
                j_commit[:10],
                count,
                ", ".join(f.look for f in records),
            )
            return _refused(
                IT_HOLDS_FACTORY_RECORDS,
                f"{j_commit[:10]} would publish {count} of the factory's own "
                f"build records (for example {records[0].first}), added, "
                "changed, moved or copied by commits that are not yet on "
                f"'{target_branch}'. These are working files of the factory, "
                "not part of the project, and they can hold private paths. "
                "Nothing was sent. Build the feature again on a release that "
                "keeps them out of commits.",
                remote_now=remote_now,
            )
        if private:
            logger.warning(
                "publisher: %s holds %d file version(s), file name(s) or commit "
                "message(s) naming a private folder; nothing was sent",
                j_commit[:10],
                private[0].count,
            )
            return _refused(
                IT_HOLDS_PRIVATE_PATHS,
                f"{j_commit[:10]} would publish {private[0].count} new file "
                "version(s), file name(s) or commit message(s) naming a folder "
                "that exists only on the factory's own machines (first: "
                f"{private[0].first}). Nothing was sent. Build the feature "
                "again on a release that keeps such paths out of commits.",
                remote_now=remote_now,
            )
        logger.info(
            "publisher: %s — the four looks found no factory record and no "
            "private folder name in the commits it adds",
            j_commit[:10],
        )
        return None

    # -- what the record refuses -------------------------------------------

    def _what_the_record_refuses(
        self,
        record: TheRecord,
        *,
        project: str,
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
        # THE RECORD IS BOUND TO ITS OWN PROJECT, and this is asked before
        # anything else about the record, because everything after it is read
        # off a record that has to be THIS project's. The request names the
        # project, and the project decides which remote is written to; a
        # record belonging to another project would hand one project's commit
        # to another project's remote, which is the worst thing this service
        # could do. The two names have to be the same name.
        recorded_project = str(record.project or "").strip()
        if not recorded_project:
            return _refused(
                "the-record-does-not-say-which-project",
                f"build {build_id}'s record does not say which project it "
                f"belongs to, so the publisher cannot tell that it is "
                f"'{project}'s build. A record with no project is not "
                "evidence about any project. Nothing was sent.",
            )
        if recorded_project != project:
            return _refused(
                "that-build-belongs-to-another-project",
                f"this request names the project '{project}', and build "
                f"{build_id}'s record says that build belongs to "
                f"'{recorded_project}'. A build's record is bound to its own "
                "project, and sending it would put one project's work on "
                "another project's remote. Nothing was sent.",
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
    """The server carrying one publisher, bound where the settings say."""

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
                check = self.server.publisher.self_check  # type: ignore[attr-defined]
                said = "not run" if check is None else (check() or "passed")
                self._answer(
                    200,
                    {
                        "status": "healthy",
                        "self_check": "passed" if said == "passed" else "failed",
                        "self_check_said": said,
                        # With private_only on (17 October 2026): the routes
                        # refused at start, and why. Said here, not as a
                        # failed self-check, because the other routes work.
                        "routes_refused_at_start": (
                            self.server.publisher.why_routes_are_not_private()  # type: ignore[attr-defined]
                        ),
                    },
                )
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
    """Bind the publisher where its settings say, on a thread of its own.

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
