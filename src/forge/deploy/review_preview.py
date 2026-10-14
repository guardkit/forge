"""The review preview before a merge offer (web readiness W7, 12 October 2026).

A web project that switches ``preview:`` on in its deploy profile gets, on
its merge card, a running copy of exactly what approval would merge, the
recording of its key journey, and the test report — all prepared BEFORE the
card goes out. This module prepares that and looks after it afterwards:

1. **Join.** The candidate is joined onto the recorded target branch as the
   offer has just fetched it from ``origin`` (``preview_base_sha``, read with
   the merge press's own helper, so it is the commit the press will join onto;
   never the clone's local branch, which can lag the remote) the way the
   merge press joins it: the build system's
   merge is ``git merge --no-ff``, whose tree for a clean merge is exactly
   what ``git merge-tree --write-tree`` computes, and the press's own
   executor refuses an approval whose joined tree differs from the one
   previewed. The joined commit is kept under an unpushed local ref,
   ``refs/forge/preview/<lease>``, and its tree is laid out where the
   candidate's would be (``.forge-candidates/preview-<lease>``).
2. **Lease a slot.** One row in the ledger's ``preview_leases`` table,
   waiting up to the profile's ``slot_wait_minutes`` for a free slot.
3. **Stand it up.** The candidate leg's own deploy steps, through the deploy
   dispatch the merge press uses, with the values the preview issues
   (``PREVIEW=1``, the slot's ports and address, the link token, the resource
   label ``forge.preview.lease=<lease>``, the evidence folder and the compose
   project ``<env>-pv<slot>``), carried apart from every other setting as
   ``issued_env``.
4. **Check the link.** Inside the sandbox, the slot's public listener must
   refuse a request with no token and one with a wrong token (403 or 410)
   and accept the right one.
5. **Record the journey.** The project's own live-gate driver, with
   ``FACTORY_RECORD_VIDEO=1``. How it records is the project's choice; it
   writes its report and media under ``FACTORY_EVIDENCE_DIR`` and says where
   in its results envelope (``report_ref``, ``screenshot_refs``,
   ``media_refs``, ``trace_refs`` and ``outcome`` — see
   :func:`forge.deploy.live_gate.project_driver_answer`).
6. **Keep the evidence** — copied out of the sandbox, BEFORE anything is torn
   down, into ``<evidence_root>/<org>/<name>/<feature>/<sha>/<run>/``. Each
   folder is written once and made read-only; an existing one is never
   overwritten.
7. **Return** the review-evidence record (``nats_core`` ``ReviewEvidence``).

Any failure tears the preview down and returns ``state="failed"`` with the
reason in plain words, and a diagnostics link when anything was copied. A
failed record never leaves a running preview behind and never makes an offer
approvable.

WHERE THE WORK HAPPENS. A repository's clone, its Docker engine and its
preview all live in its sandbox, so the git join, the link check, the
evidence read and the label-scoped removal are six small routes on the
sandbox's deploy sidecar (``/preview/...``). The functions those routes run
are in this module (:func:`serve_preview_operation`), so the logic is tested
here, with a fake command runner, and the sidecar only resolves the
repository and calls in. A repository without a sandbox gets a failed preview
saying so.

PREPARATION RUNS BEFORE THE OFFER'S DURABLE ROW, and that has three
consequences (recorded 12 October 2026):

* a second offer for the same build while one is preparing is refused by the
  offer itself (:class:`forge.pipeline.merge_offer.MergeOfferService`), so a
  preview is never prepared twice;
* a card whose publish fails has its preview released at once;
* A RESTART DURING PREPARATION LEAVES THE BUILD WITH NO CARD. The merge offer
  is fired once, after the build's terminal event is published and its queue
  message acknowledged (``lifecycle_bridge.wireup._spawn_merge_offer``), and
  no existing path offers a COMPLETE build's card again. The start-up
  reconcile ends the half-prepared lease ("the coordinator restarted while
  this preview was being prepared"). Recovery is the existing one: run the
  feature again. ``forge merge-deploy`` refuses such a build (previews on,
  the project prepares them, no previewed card on record), so it is never
  merged unseen. This gap is as old as the offer itself; a preview only makes the
  window minutes rather than moments. No retry mechanism is added for it.

ONLY ``forge.preview.lease`` IS EVER TOUCHED. Teardown, the sweep and the
start-up reconciliation find and remove containers, networks, volumes and
images by that one label and its exact value (an image still used by a
container without that label is never removed). Preparation checks that the
started preview really carries the label, so a teardown that finds nothing
means nothing is there. The disposable runs' own label
(``guardkit.fixture.owner``) never appears here.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from forge.project_git import run_git

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nats_core.events import ReviewEvidence

    from forge.deploy.profile import DeployPreview, DeployProfile, PreviewSlot

logger = logging.getLogger(__name__)

__all__ = [
    "EVIDENCE_FOLDER",
    "IssuedPreviewValues",
    "LEASE_LABEL",
    "PREVIEW_ENV_KEYS",
    "issued_preview_refusal",
    "PREVIEW_ROUTES",
    "PreviewLease",
    "PreviewLeaseStore",
    "PreviewRequest",
    "PreviewTree",
    "ReviewPreviewService",
    "SidecarPreviewVenue",
    "UnavailableReviewPreview",
    "build_review_preview_service",
    "serve_preview_operation",
]

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

#: The one label a preview's containers, networks and volumes carry, with the
#: lease's id as its value. Nothing else is ever removed by this module.
LEASE_LABEL: str = "forge.preview.lease"

#: Where a preview's joined commit is kept: local, never pushed.
PREVIEW_REF_PREFIX: str = "refs/forge/preview/"

#: The laid-out tree's leaf under ``.forge-candidates`` is this plus the lease.
PREVIEW_TREE_PREFIX: str = "preview-"

#: The folder inside the preview's tree the project writes its evidence into.
EVIDENCE_FOLDER: str = ".factory-evidence"

#: Every setting name the preview issues to the project. They travel only as
#: values the preview issued (:class:`IssuedPreviewValues`, the ``issued_env``
#: field), never as an ordinary overlay, and the sandbox's deploy sidecar
#: accepts them only for a project whose committed profile has its preview
#: switched on.
PREVIEW_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PREVIEW",
        "COMPOSE_PROJECT_NAME",
        "FACTORY_PREVIEW_PORT",
        "FACTORY_PREVIEW_INTERNAL_PORT",
        "FACTORY_PREVIEW_URL",
        "FACTORY_PREVIEW_TOKEN",
        "FACTORY_RESOURCE_LABEL",
        "FACTORY_EVIDENCE_DIR",
        "FACTORY_RECORD_VIDEO",
    }
)

#: The one preview name that is a credential: the link token. The factory
#: makes it fresh for each preview and issues it TO the project; it is never
#: one of the factory's own.
PREVIEW_TOKEN_NAME: str = "FACTORY_PREVIEW_TOKEN"

#: What a freshly made token looks like: ``secrets.token_urlsafe(32)`` makes
#: 43 of these characters, and anything shorter or wider is not one.
_FRESH_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,}$")


class IssuedPreviewValues:
    """Values the review preview issues to the project's own programs.

    The preview's link token is a credential, but one the factory makes for
    this one preview and hands TO the project, which is meant to have it.
    Every other credential-shaped name is refused on the way to a project
    program (design "Project commands run without the factory's credentials",
    section 1a), so these values travel on a narrow path of their own: this
    type, the separate ``issued_env`` field at every hop, and the helper's
    separate ``issued`` argument.

    Built in two places only — the preview's own preparation
    (:meth:`ReviewPreviewService._prepare`) and the helper service's reader
    for a request's ``issued_env`` — which a guard test checks. Only the
    names in :data:`PREVIEW_ENV_KEYS` are accepted. Its text form names the
    settings and never shows a value.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        checked: dict[str, str] = {}
        for name, value in dict(values).items():
            if name not in PREVIEW_ENV_KEYS:
                raise ValueError(
                    f"{name!r} is not one of the review preview's own settings"
                )
            if not isinstance(value, str):
                raise ValueError(f"the preview's {name!r} must be text")
            checked[name] = value
        object.__setattr__(self, "_values", checked)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("issued preview values cannot be changed")

    def __getitem__(self, name: str) -> str:
        return self._values[name]

    def names(self) -> frozenset[str]:
        return frozenset(self._values)

    def as_dict(self) -> dict[str, str]:
        """A plain copy, for a hop that serialises (a runbook, a request)."""
        return dict(self._values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, IssuedPreviewValues) and other._values == self._values

    def __hash__(self) -> int:
        return hash(tuple(sorted(self._values.items())))

    def __repr__(self) -> str:
        return f"IssuedPreviewValues(names={sorted(self._values)})"


def issued_preview_refusal(
    values: Any,
    *,
    parent: Mapping[str, str],
    others: Sequence[Mapping[str, str]] = (),
) -> str | None:
    """Why these values cannot be taken as issued by the preview, or ``None``.

    Asked where the values are read (the helper service) and again where a
    program's environment is built. The sentence names the setting and never
    shows its value.

    * every name is one of :data:`PREVIEW_ENV_KEYS`, and every value is text;
    * the token has the shape of a freshly made one (at least 32 of
      ``A-Z a-z 0-9 _ -``);
    * the token equals no value in ``parent`` (the process building the
      program's environment) or in ``others`` (the additions and the overlay),
      so a credential the factory holds cannot be passed off as issued. The
      other names are not compared: a port or a project name can match by
      chance, and they are neither credential-shaped nor free to choose.
    """
    if not isinstance(values, Mapping):
        return "the preview's issued values must be a JSON object of text values"
    for name, value in values.items():
        if name not in PREVIEW_ENV_KEYS:
            return (
                f"{name!r} is not one of the review preview's own settings "
                f"({', '.join(sorted(PREVIEW_ENV_KEYS))})"
            )
        if not isinstance(value, str):
            return f"the preview's {name!r} must be text"
    token = values.get(PREVIEW_TOKEN_NAME)
    if token is None:
        return None
    if not _FRESH_TOKEN.fullmatch(token):
        return (
            f"{PREVIEW_TOKEN_NAME} does not have the shape of a freshly made "
            f"link token (at least 32 letters, digits, '_' or '-')"
        )
    if any(token == str(v) for v in parent.values()) or any(
        token == str(v) for other in others for v in other.values()
    ):
        return (
            f"{PREVIEW_TOKEN_NAME} equals a setting the factory already holds, "
            f"so it was not issued by the preview"
        )
    return None


#: A lease id: ``pv`` and twelve lower-case hex digits. It becomes a git ref
#: name, a folder name and a label value, so it is held to one plain shape.
_LEASE_ID = re.compile(r"^pv[0-9a-f]{12}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
#: A compose project name, as compose itself allows one.
_COMPOSE_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: A lease holds its slot in these states.
_LIVE_STATES: tuple[str, ...] = ("preparing", "ready", "ending")

#: Answers that mean "this link is refused": the project's proxy says 410 for
#: a different or expired candidate; 403 is accepted as the same refusal.
_REFUSED_STATUSES: frozenset[int] = frozenset({403, 410})

#: The most evidence one preview may send out of its sandbox, and the most
#: the coordinator will take in: checked on the files before packing, on the
#: packed archive, and again on the coordinator while unpacking. The archive
#: travels base64 in one JSON answer, so the coordinator briefly holds about
#: two copies of it; 64 MiB keeps that well clear of its memory guard.
EVIDENCE_TRANSFER_CAP_BYTES: int = 64 * 1024 * 1024

#: The longest evidence answer the coordinator reads off the wire: the base64
#: of the cap and a margin for the rest of the JSON.
_EVIDENCE_ANSWER_MAX_BYTES: int = (EVIDENCE_TRANSFER_CAP_BYTES // 3 + 1) * 4 + 64 * 1024

#: The most files one preview's evidence may hold.
EVIDENCE_MEMBER_CAP: int = 2000

#: Said after a reason whose teardown has not been confirmed yet.
_NOT_CONFIRMED: str = " (teardown not confirmed yet)"

#: On top of the slot wait and the live-gate timeout, how long a preparation
#: may take (the start-up and the evidence copy) before the sweep ends it.
_PREPARATION_MARGIN: timedelta = timedelta(minutes=60)

#: How long to wait before reading an unreadable profile a second time.
_PROFILE_RETRY_SECONDS: float = 3.0

#: How often a preparation waiting for a free slot looks again.
_SLOT_POLL_SECONDS: float = 15.0

#: File kinds read as media when the driver named none.
_SCREENSHOT_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})
_VIDEO_SUFFIXES: frozenset[str] = frozenset({".webm", ".mp4"})
_MEDIA_SUFFIXES: frozenset[str] = _SCREENSHOT_SUFFIXES | _VIDEO_SUFFIXES
_MEDIA_SCAN_CAP: int = 20

#: The six sidecar routes.
PREVIEW_TREE_ROUTE: str = "/preview/tree"
PREVIEW_TREE_REMOVE_ROUTE: str = "/preview/tree-remove"
PREVIEW_PROBE_ROUTE: str = "/preview/probe"
PREVIEW_EVIDENCE_ROUTE: str = "/preview/evidence"
PREVIEW_PROJECTS_ROUTE: str = "/preview/projects"
PREVIEW_PROJECT_REMOVE_ROUTE: str = "/preview/project-remove"
PREVIEW_ROUTES: tuple[str, ...] = (
    PREVIEW_TREE_ROUTE,
    PREVIEW_TREE_REMOVE_ROUTE,
    PREVIEW_PROBE_ROUTE,
    PREVIEW_EVIDENCE_ROUTE,
    PREVIEW_PROJECTS_ROUTE,
    PREVIEW_PROJECT_REMOVE_ROUTE,
)
#: The routes that start or reach a preview need the preview switched on in
#: the project's profile; reading evidence and removing leftovers never do,
#: so a preview switched off later can still be cleaned up.
PREVIEW_ROUTES_NEEDING_THE_SWITCH: frozenset[str] = frozenset(
    {PREVIEW_TREE_ROUTE, PREVIEW_PROBE_ROUTE}
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_time(text: Any) -> datetime | None:
    if isinstance(text, datetime):
        value = text
    elif isinstance(text, str) and text.strip():
        try:
            value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _new_lease_id() -> str:
    return f"pv{secrets.token_hex(6)}"


def _new_run_id() -> str:
    return f"pvrun-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    """What the merge offer pinned, handed to :meth:`ReviewPreviewService.prepare`."""

    project: str
    feature_id: str
    build_id: str
    branch: str
    candidate_sha: str
    candidate_tree: str
    expect_main_sha: str
    target_branch: str
    #: The commit the preview is joined onto: the recorded target branch as
    #: freshly fetched from ``origin`` by the offer, through the merge press's
    #: own helper (:func:`forge.pipeline.merge_join.target_branch_now`), so it
    #: is what the press will join onto. ``expect_main_sha`` is the clone's
    #: local branch and can lag the remote, so it is never joined onto.
    preview_base_sha: str | None = None
    #: Why that commit could not be read, when it could not.
    preview_base_refusal: str | None = None


# ---------------------------------------------------------------------------
# The ledger table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewLease:
    """One row of ``preview_leases``."""

    lease_id: str
    slot: int | None
    port: int | None
    token: str
    project: str
    feature_id: str
    build_id: str
    candidate_sha: str
    candidate_tree: str
    preview_tree: str | None
    run_id: str
    compose_project: str | None
    evidence_dir: str | None
    state: str
    reason: str | None
    created_at: str
    app_expires_at: str
    evidence_expires_at: str
    ended_at: str | None = None
    evidence_deleted_at: str | None = None

    @property
    def live(self) -> bool:
        return self.state in _LIVE_STATES


_COLUMNS: tuple[str, ...] = (
    "lease_id",
    "slot",
    "port",
    "token",
    "project",
    "feature_id",
    "build_id",
    "candidate_sha",
    "candidate_tree",
    "preview_tree",
    "run_id",
    "compose_project",
    "evidence_dir",
    "state",
    "reason",
    "created_at",
    "app_expires_at",
    "evidence_expires_at",
    "ended_at",
    "evidence_deleted_at",
)


class PreviewLeaseStore:
    """Read and write ``preview_leases`` (schema v19) on the ledger's connection.

    The same idiom as :class:`forge.pipeline.deployment_lock.DeploymentLockStore`:
    a slot is taken in one ``BEGIN IMMEDIATE`` transaction, and the partial
    unique index on live leases' ports means two live leases can never hold
    one slot, whichever process asks.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._cx = connection

    @contextmanager
    def _transaction(self):
        started = False
        if not self._cx.in_transaction:
            self._cx.execute("BEGIN IMMEDIATE")
            started = True
        try:
            yield
        except BaseException:
            if started:
                self._cx.rollback()
            raise
        else:
            if started:
                self._cx.commit()

    def _rows(self, where: str, params: Sequence[Any] = ()) -> list[PreviewLease]:
        cursor = self._cx.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM preview_leases {where}", tuple(params)
        )
        return [PreviewLease(**dict(zip(_COLUMNS, row, strict=True))) for row in cursor]

    def get(self, lease_id: str) -> PreviewLease | None:
        rows = self._rows("WHERE lease_id = ?", (lease_id,))
        return rows[0] if rows else None

    def live(self) -> list[PreviewLease]:
        marks = ", ".join("?" for _ in _LIVE_STATES)
        return self._rows(f"WHERE state IN ({marks}) ORDER BY created_at", _LIVE_STATES)

    def for_run(self, run_id: str) -> PreviewLease | None:
        """The lease a preparation run wrote, or None."""
        rows = self._rows("WHERE run_id = ? ORDER BY created_at DESC", (run_id,))
        return rows[0] if rows else None

    def live_for_build(self, build_id: str) -> list[PreviewLease]:
        marks = ", ".join("?" for _ in _LIVE_STATES)
        return self._rows(
            f"WHERE build_id = ? AND state IN ({marks}) ORDER BY created_at",
            (build_id, *_LIVE_STATES),
        )

    def projects(self) -> list[str]:
        return [
            str(row[0])
            for row in self._cx.execute(
                "SELECT DISTINCT project FROM preview_leases ORDER BY project"
            )
        ]

    def evidence_to_delete(self, now: datetime) -> list[PreviewLease]:
        rows = self._rows(
            "WHERE evidence_dir IS NOT NULL AND evidence_deleted_at IS NULL "
            "AND state NOT IN ('preparing', 'ready', 'ending')"
        )
        due: list[PreviewLease] = []
        for lease in rows:
            expiry = _parse_time(lease.evidence_expires_at)
            if expiry is not None and now >= expiry:
                due.append(lease)
        return due

    def insert(self, lease: PreviewLease, *, now: datetime) -> None:
        values = [getattr(lease, name) for name in _COLUMNS]
        with self._transaction():
            self._cx.execute(
                f"INSERT INTO preview_leases ({', '.join(_COLUMNS)}, updated_at) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)}, ?)",
                (*values, now.isoformat()),
            )

    def take_slot(
        self,
        lease: PreviewLease,
        *,
        slots: Sequence[tuple[int, int]],
        now: datetime,
    ) -> tuple[int, int] | None:
        """Give ``lease`` the first free ``(slot, port)``, or ``None``.

        The lease is written in the same transaction, in state ``preparing``,
        holding the slot it got.
        """
        try:
            with self._transaction():
                marks = ", ".join("?" for _ in _LIVE_STATES)
                held = {
                    int(row[0])
                    for row in self._cx.execute(
                        f"SELECT port FROM preview_leases WHERE port IS NOT NULL "
                        f"AND state IN ({marks})",
                        _LIVE_STATES,
                    )
                }
                for slot, port in slots:
                    if port in held:
                        continue
                    taken = replace(lease, slot=slot, port=port, state="preparing")
                    self._cx.execute(
                        f"INSERT INTO preview_leases ({', '.join(_COLUMNS)}, updated_at) "
                        f"VALUES ({', '.join('?' for _ in _COLUMNS)}, ?)",
                        (*[getattr(taken, n) for n in _COLUMNS], now.isoformat()),
                    )
                    return slot, port
        except sqlite3.IntegrityError as exc:
            logger.info(
                "review preview: a slot was taken under us (%s); asking again", exc
            )
        return None

    def update(self, lease_id: str, *, now: datetime, **fields: Any) -> None:
        unknown = set(fields) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown preview lease field(s): {sorted(unknown)}")
        sets = {**fields, "updated_at": now.isoformat()}
        with self._transaction():
            self._cx.execute(
                f"UPDATE preview_leases SET {', '.join(f'{k} = ?' for k in sets)} "
                "WHERE lease_id = ?",
                (*sets.values(), lease_id),
            )


# ---------------------------------------------------------------------------
# Inside the sandbox: what the six sidecar routes run
# ---------------------------------------------------------------------------

#: ``(argv) -> (exit code, stdout, stderr)``. The sidecar passes a real one;
#: tests pass a fake, so nothing here needs Docker or git to be tested.
CommandRunner = Callable[[Sequence[str]], tuple[int, str, str]]

#: ``(url) -> HTTP status, or None when nothing answered``.
LinkFetcher = Callable[[str], "int | None"]

_GIT_IDENTITY: dict[str, str] = {
    "GIT_AUTHOR_NAME": "Forge review preview",
    "GIT_AUTHOR_EMAIL": "forge-preview@localhost.invalid",
    "GIT_COMMITTER_NAME": "Forge review preview",
    "GIT_COMMITTER_EMAIL": "forge-preview@localhost.invalid",
}


def _run_command(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run one fixed argument list (no shell), with a one-minute wall.

    Git commands go through Forge's one way of running git
    (:func:`forge.project_git.run_git`): a fixed environment with no factory
    secret, no hooks, filters, merge programs or signing from the project's
    settings, and on failure a fixed sentence instead of git's own text, so
    nothing git prints reaches the review card. Only the commit identity is
    passed in. Every other command (the container engine) runs as before.
    """
    if argv and argv[0] == "git":
        try:
            done = run_git(list(argv), timeout=60, env=_GIT_IDENTITY)
        except subprocess.TimeoutExpired as exc:
            # The helper's timeout carries only a sentence naming the command.
            return 127, "", str(exc.stderr or "git did not finish within 60 seconds.")
        except OSError as exc:  # a refusal (GitRefused) or git missing
            return 127, "", f"{type(exc).__name__}: {exc}"
        return done.returncode, done.stdout or "", done.stderr or ""
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv, no shell
            list(argv),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, **_GIT_IDENTITY},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout, done.stderr


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def preview_tree_path(repo_root: Path | str, lease_id: str) -> Path:
    """``<repo>/.forge-candidates/preview-<lease>`` — where the deploy sidecar
    lets a deploy step and the live-gate driver run (its LAW 8)."""
    from forge.deploy.candidate_tree import candidate_tree_path

    return candidate_tree_path(repo_root, f"{PREVIEW_TREE_PREFIX}{lease_id}")


def make_preview_tree(
    repo_root: Path | str,
    *,
    lease_id: str,
    onto: str,
    candidate: str,
    run: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Join ``candidate`` onto ``onto``, keep it under a ref, and lay it out.

    Returns ``{"commit", "tree", "path"}`` or ``{"refusal"}``. The join is
    ``git merge-tree --write-tree`` — the same merge machinery, and for a clean
    merge the same tree, as the build system's ``git merge --no-ff``; a
    conflicting candidate is refused naming the files.
    """
    from forge.deploy.candidate_tree import (
        CandidateTreeError,
        _ensure_excluded_sync,
        _materialise_sync,
    )

    root = str(repo_root)
    merged = run(
        [
            "git",
            "-C",
            root,
            "merge-tree",
            "--write-tree",
            "--name-only",
            onto,
            candidate,
        ]
    )
    code, out, err = merged
    lines = out.splitlines()
    if code == 1 and lines:
        files: list[str] = []
        for line in lines[1:]:
            if not line.strip():
                break
            files.append(line.strip())
        named = ", ".join(files[:10]) or "files git did not name"
        return {
            "refusal": (
                f"the candidate does not join cleanly onto {onto[:10]}: these "
                f"files conflict — {named}"
            )
        }
    if code != 0 or not lines or not _SHA.match(lines[0].strip()):
        return {
            "refusal": (
                f"git could not join the candidate onto {onto[:10]}: "
                # Only the runner's stderr: for the real runner that is the
                # helper's fixed sentence. A failed command's stdout is not
                # read for the reason, as it can carry git's own words.
                f"{_last_line(err) or f'exit code {code}'}"
            )
        }
    tree = lines[0].strip()
    code, out, err = run(
        [
            "git",
            "-C",
            root,
            "commit-tree",
            tree,
            "-p",
            onto,
            "-p",
            candidate,
            "-m",
            f"Review preview {lease_id}: {candidate[:10]} joined onto {onto[:10]}",
        ]
    )
    commit = out.strip()
    if code != 0 or not _SHA.match(commit):
        return {
            "refusal": f"git could not record the joined commit: {_last_line(err) or code}"
        }
    code, out, err = run(
        ["git", "-C", root, "update-ref", f"{PREVIEW_REF_PREFIX}{lease_id}", commit, ""]
    )
    if code != 0:
        return {
            "refusal": (
                f"git could not keep the joined commit under "
                f"{PREVIEW_REF_PREFIX}{lease_id}: {_last_line(err) or code}"
            )
        }
    dest = preview_tree_path(root, lease_id)
    try:
        _ensure_excluded_sync(Path(root))
        _materialise_sync(Path(root), dest, commit)
    except (CandidateTreeError, OSError) as exc:
        run(["git", "-C", root, "update-ref", "-d", f"{PREVIEW_REF_PREFIX}{lease_id}"])
        return {"refusal": f"the joined tree could not be laid out: {exc}"}
    return {"commit": commit, "tree": tree, "path": str(dest)}


def remove_preview_tree(
    repo_root: Path | str, *, lease_id: str, run: CommandRunner = _run_command
) -> dict[str, Any]:
    """Remove the preview's ref and laid-out tree. Already gone is removed."""
    root = str(repo_root)
    ref = f"{PREVIEW_REF_PREFIX}{lease_id}"
    code, _out, _err = run(["git", "-C", root, "show-ref", "--verify", "--quiet", ref])
    ref_removed = True
    if code == 0:
        ref_removed = run(["git", "-C", root, "update-ref", "-d", ref])[0] == 0
    path = preview_tree_path(root, lease_id)
    try:
        if path.exists():
            shutil.rmtree(path)
        tree_removed = True
    except OSError as exc:
        logger.warning("review preview: %s could not be removed (%s)", path, exc)
        tree_removed = False
    return {"removed": bool(ref_removed and tree_removed), "path": str(path)}


def _fetch_status(url: str) -> int | None:
    """GET ``url`` without following redirects; its status, or None."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=10) as response:  # noqa: S310 — loopback only
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def probe_preview_link(
    *, port: int, token: str, fetch: LinkFetcher = _fetch_status
) -> dict[str, Any]:
    """Ask the slot's listener, on this sandbox's loopback, three times.

    No token, a wrong token, the right token — each answer's status, or
    ``None`` when nothing answered.
    """
    wrong = secrets.token_urlsafe(16)
    while wrong == token:  # pragma: no cover - astronomically unlikely
        wrong = secrets.token_urlsafe(16)
    base = f"http://127.0.0.1:{int(port)}/"
    return {
        "no_token": fetch(base),
        "wrong_token": fetch(f"{base}?preview={quote(wrong, safe='')}"),
        "right_token": fetch(f"{base}?preview={quote(token, safe='')}"),
    }


def pack_preview_evidence(
    repo_root: Path | str, *, lease_id: str, cap: int = EVIDENCE_TRANSFER_CAP_BYTES
) -> dict[str, Any]:
    """The preview's evidence folder as a gzipped tar, base64 — or a refusal.

    Nothing outside the folder can be sent. The folder itself, and every
    folder between the repository and it (``.forge-candidates``, the
    preview's tree), must be a real folder, not a symbolic link — checked
    with ``lstat``, so a project cannot point ``.factory-evidence`` at
    anything else in its sandbox. The walk then starts from a descriptor
    opened with ``O_NOFOLLOW`` (``os.fwalk``), so a folder swapped for a link
    after those checks is refused, not read. Inside, only regular files and
    folders are packed, each file opened with ``O_NOFOLLOW``: a link is left
    out rather than followed, and a linked folder is never walked into.
    """
    folder = preview_tree_path(repo_root, lease_id) / EVIDENCE_FOLDER
    here = Path(repo_root)
    for name in folder.relative_to(here).parts:
        here = here / name
        try:
            mode = here.lstat().st_mode
        except FileNotFoundError:
            return {"refusal": f"the preview left no evidence folder ({folder})"}
        if stat.S_ISLNK(mode):
            return {
                "refusal": (
                    f"the preview's evidence folder is reached through a symbolic "
                    f"link ({here}), so nothing was sent"
                )
            }
        if not stat.S_ISDIR(mode):
            return {"refusal": f"the preview left no evidence folder ({folder})"}
    # The walk starts from a descriptor opened WITHOUT following a link, so a
    # folder swapped for a link after the checks above is refused rather than
    # read, and everything below it is opened relative to real folders only.
    try:
        top = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return {
            "refusal": (
                "the preview's evidence folder could not be opened without "
                f"following a link ({exc.strerror})"
            )
        }
    total = 0
    buffer = io.BytesIO()
    count = 0
    try:
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for here, folders, files, here_fd in os.fwalk(
                ".", dir_fd=top, follow_symlinks=False
            ):
                for name in sorted(folders):
                    st = os.stat(name, dir_fd=here_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(st.st_mode):
                        folders.remove(name)  # a link: never walked into
                        continue
                    info = tarfile.TarInfo(os.path.normpath(os.path.join(here, name)))
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = int(st.st_mtime)
                    archive.addfile(info)
                for name in sorted(files):
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=here_fd)
                    except OSError:
                        continue  # a link, or gone: left out
                    with os.fdopen(fd, "rb") as handle:
                        st = os.fstat(handle.fileno())
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        total += st.st_size
                        count += 1
                        if total > cap or count > EVIDENCE_MEMBER_CAP:
                            return {
                                "refusal": (
                                    f"the preview's evidence is larger than "
                                    f"{cap // (1024 * 1024)} MiB or "
                                    f"{EVIDENCE_MEMBER_CAP} files, which is more "
                                    "than one preview may send"
                                )
                            }
                        info = tarfile.TarInfo(os.path.normpath(os.path.join(here, name)))
                        info.size = st.st_size
                        info.mode = 0o644
                        info.mtime = int(st.st_mtime)
                        archive.addfile(info, handle)
    finally:
        os.close(top)
    packed = buffer.getvalue()
    buffer.close()
    if len(packed) > cap:
        return {
            "refusal": (
                f"the preview's packed evidence is larger than {cap // (1024 * 1024)} "
                "MiB, which is more than one preview may send"
            )
        }
    return {
        "archive": base64.b64encode(packed).decode("ascii"),
        "files": count,
        "bytes": total,
    }


def _engine_lines(run: CommandRunner, argv: Sequence[str]) -> list[str] | None:
    code, out, err = run(list(argv))
    if code != 0:
        logger.warning(
            "review preview: %s failed: %s", " ".join(argv[:3]), _last_line(err)
        )
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def list_preview_leases(
    run: CommandRunner = _run_command, *, compose_project: str | None = None
) -> dict[str, Any]:
    """The lease ids of everything carrying the label, and of running containers.

    ``leases``: every container (running or not), network, volume and image
    carrying the label. ``running``: the leases with at least one RUNNING
    container — what preparation checks after the preview is started. With
    ``compose_project``, also ``compose_containers``: every container of that
    compose project, by name, with the lease label it carries (``""`` for
    none), so preparation can require every one of them to be labelled.
    """
    engine = shutil.which("docker") or "docker"
    found: set[str] = set()
    # ``.Label`` is a documented format field of ``docker ps``, ``network ls``
    # and ``volume ls`` — but NOT of ``docker image ls`` (its fields are ID,
    # Repository, Tag, Digest, CreatedSince, CreatedAt and Size). Images are
    # therefore listed by ID and their labels read with ``image inspect``.
    template = '{{.Label "' + LEASE_LABEL + '"}}'
    for kind in (
        ["ps", "--all"],
        ["network", "ls"],
        ["volume", "ls"],
    ):
        lines = _engine_lines(
            run,
            [engine, *kind, "--filter", f"label={LEASE_LABEL}", "--format", template],
        )
        if lines is None:
            return {"refusal": "the container engine could not be asked about previews"}
        found.update(line for line in lines if _LEASE_ID.match(line))
    image_ids = _engine_lines(
        run,
        [engine, "image", "ls", "--filter", f"label={LEASE_LABEL}", "--format", "{{.ID}}"],
    )
    if image_ids is None:
        return {"refusal": "the container engine could not be asked about previews"}
    image_ids = sorted(set(image_ids))
    if image_ids:
        labels = _engine_lines(
            run,
            [
                engine,
                "image",
                "inspect",
                "--format",
                '{{index .Config.Labels "' + LEASE_LABEL + '"}}',
                *image_ids,
            ],
        )
        if labels is None:
            return {"refusal": "the container engine could not be asked about previews"}
        found.update(line for line in labels if _LEASE_ID.match(line))
    running = _engine_lines(
        run, [engine, "ps", "--filter", f"label={LEASE_LABEL}", "--format", template]
    )
    if running is None:
        return {"refusal": "the container engine could not be asked about previews"}
    answer: dict[str, Any] = {
        "leases": sorted(found),
        "running": sorted({line for line in running if _LEASE_ID.match(line)}),
    }
    if compose_project is not None:
        members = _engine_lines(
            run,
            [
                engine,
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={compose_project}",
                "--format",
                '{{.Names}} {{.Label "' + LEASE_LABEL + '"}}',
            ],
        )
        if members is None:
            return {"refusal": "the container engine could not be asked about previews"}
        answer["compose_containers"] = [
            {"name": parts[0], "lease": parts[1] if len(parts) > 1 else ""}
            for parts in (line.split() for line in members)
        ]
    return answer


def _images_other_containers_use(
    run: CommandRunner, engine: str, lease_id: str, image_ids: Sequence[str]
) -> set[str] | None:
    """Of ``image_ids``, those a container NOT labelled for this lease uses.

    Asked per image with the engine's ``ancestor`` filter (which also matches
    containers built on top of the image, so it can only protect more).
    ``None`` when the engine could not be asked.
    """
    protected: set[str] = set()
    template = '{{.ID}} {{.Label "' + LEASE_LABEL + '"}}'
    for image in image_ids:
        lines = _engine_lines(
            run,
            [engine, "ps", "--all", "--filter", f"ancestor={image}", "--format", template],
        )
        if lines is None:
            return None
        for line in lines:
            parts = line.split()
            if (parts[1] if len(parts) > 1 else "") != lease_id:
                protected.add(image)
    return protected


def remove_preview_resources(
    lease_id: str, run: CommandRunner = _run_command
) -> dict[str, Any]:
    """Remove the containers, networks, volumes and images labelled for this lease.

    Only that label and value is ever matched. An image is never removed while
    a container without this lease's label uses it (another lease's, or one
    that is nothing to do with previews); it is left, and not counted as
    remaining, because it is not this lease's to remove.
    """
    engine = shutil.which("docker") or "docker"
    selector = f"label={LEASE_LABEL}={lease_id}"
    plans = (
        (["ps", "--all", "--quiet", "--filter", selector], ["rm", "--force"]),
        (["network", "ls", "--quiet", "--filter", selector], ["network", "rm"]),
        (
            ["volume", "ls", "--quiet", "--filter", selector],
            ["volume", "rm", "--force"],
        ),
    )
    images = ["image", "ls", "--quiet", "--no-trunc", "--filter", selector]
    for listing, removal in plans:
        ids = _engine_lines(run, [engine, *listing])
        if ids is None:
            return {
                "removed": False,
                "refusal": "the container engine could not be asked",
            }
        if ids:
            run([engine, *removal, *ids])
    # Images last: their containers are gone by now.
    image_ids = _engine_lines(run, [engine, *images])
    if image_ids is None:
        return {"removed": False, "refusal": "the container engine could not be asked"}
    protected = _images_other_containers_use(run, engine, lease_id, image_ids)
    if protected is None:
        return {"removed": False, "refusal": "the container engine could not be asked"}
    removable = [image for image in image_ids if image not in protected]
    if removable:
        run([engine, "image", "rm", *removable])
    remaining = 0
    for listing, _removal in plans:
        ids = _engine_lines(run, [engine, *listing])
        if ids is None:
            return {
                "removed": False,
                "refusal": "the container engine could not be asked",
            }
        remaining += len(ids)
    left = _engine_lines(run, [engine, *images])
    if left is None:
        return {"removed": False, "refusal": "the container engine could not be asked"}
    remaining += len([image for image in left if image not in protected])
    answer: dict[str, Any] = {"removed": remaining == 0, "remaining": remaining}
    if protected:
        answer["images_kept_in_use"] = sorted(protected)
    return answer


def serve_preview_operation(
    route: str,
    payload: Mapping[str, Any],
    *,
    repo_path: Path,
    slot_ports: frozenset[int],
    run: CommandRunner = _run_command,
    fetch: LinkFetcher = _fetch_status,
) -> tuple[int, dict[str, Any]]:
    """One ``/preview/...`` request, after the sidecar resolved the repository.

    ``slot_ports`` are the ports the project's profile declares for its
    preview slots; the link check reaches only those, only on loopback. Never
    raises.
    """
    try:
        if route == PREVIEW_PROJECTS_ROUTE:
            compose_project = payload.get("compose_project")
            if compose_project is not None and (
                not isinstance(compose_project, str)
                or not _COMPOSE_PROJECT.match(compose_project)
            ):
                return 400, {
                    "error": f"'compose_project' must be a compose project name; got {compose_project!r}"
                }
            answer = list_preview_leases(run, compose_project=compose_project)
            return (200 if "leases" in answer else 503), answer
        lease_id = payload.get("lease_id")
        if not isinstance(lease_id, str) or not _LEASE_ID.match(lease_id):
            return 400, {
                "error": f"'lease_id' must be a preview lease id (pv + 12 hex digits); got {lease_id!r}"
            }
        if route == PREVIEW_TREE_ROUTE:
            onto, candidate = payload.get("onto"), payload.get("candidate")
            for name, value in (("onto", onto), ("candidate", candidate)):
                if not isinstance(value, str) or not _SHA.match(value):
                    return 400, {"error": f"'{name}' must be a 40-character commit id"}
            answer = make_preview_tree(
                repo_path, lease_id=lease_id, onto=onto, candidate=candidate, run=run
            )
            return 200, answer
        if route == PREVIEW_TREE_REMOVE_ROUTE:
            return 200, remove_preview_tree(repo_path, lease_id=lease_id, run=run)
        if route == PREVIEW_PROBE_ROUTE:
            port, token = payload.get("port"), payload.get("token")
            if (
                not isinstance(port, int)
                or isinstance(port, bool)
                or port not in slot_ports
            ):
                return 400, {
                    "error": f"'port' must be one of the profile's preview ports; got {port!r}"
                }
            if not isinstance(token, str) or not token.strip():
                return 400, {"error": "'token' is required"}
            return 200, probe_preview_link(port=port, token=token, fetch=fetch)
        if route == PREVIEW_EVIDENCE_ROUTE:
            return 200, pack_preview_evidence(repo_path, lease_id=lease_id)
        if route == PREVIEW_PROJECT_REMOVE_ROUTE:
            return 200, remove_preview_resources(lease_id, run)
        return 404, {"error": f"no such preview route: {route}"}
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary
        return 500, {"error": f"preview error: {type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# The coordinator's side of those routes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewTree:
    """The joined commit, its tree and where it is laid out — or why not."""

    commit: str | None = None
    tree: str | None = None
    path: str | None = None
    refusal: str | None = None


class SidecarPreviewVenue:
    """The six preview operations, asked of one repository's sandbox sidecar.

    Never raises: a sidecar that cannot be reached, or that refuses, is a
    refusal (or ``False`` / ``None``) in its own words.
    """

    def __init__(
        self,
        base_url: str,
        *,
        repo: str,
        post: Callable[[str, dict[str, Any], float], tuple[int, Any]] | None = None,
        timeout_s: float = 120.0,
        layout_timeout_s: float = 600.0,
    ) -> None:
        if post is None:
            from forge.planning.sidecar_git_runner import _urllib_post

            post = _urllib_post
        self._base_url = str(base_url).rstrip("/")
        self._repo = str(repo)
        self._post = post
        self._timeout_s = timeout_s
        self._layout_timeout_s = layout_timeout_s

    async def _ask(
        self,
        route: str,
        body: dict[str, Any],
        *,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        url = f"{self._base_url}{route}"
        try:
            status, decoded = await asyncio.to_thread(
                self._post,
                url,
                {"repo": self._repo, **body},
                timeout or self._timeout_s,
                **({"max_bytes": max_bytes} if max_bytes is not None else {}),
            )
        except Exception as exc:  # noqa: BLE001 — transport boundary
            return None, (
                f"the sandbox sidecar at {self._base_url} could not be reached for "
                f"{route}: {type(exc).__name__}: {exc}"
            )
        if status != 200 or not isinstance(decoded, dict):
            error = decoded.get("error") if isinstance(decoded, dict) else decoded
            return None, f"the sandbox sidecar answered {status} for {route}: {error}"
        return decoded, None

    async def lay_out(self, lease_id: str, *, onto: str, candidate: str) -> PreviewTree:
        answer, why = await self._ask(
            PREVIEW_TREE_ROUTE,
            {"lease_id": lease_id, "onto": onto, "candidate": candidate},
            timeout=self._layout_timeout_s,
        )
        if answer is None:
            return PreviewTree(refusal=why)
        if answer.get("refusal"):
            return PreviewTree(refusal=str(answer["refusal"]))
        commit, tree, path = (
            answer.get("commit"),
            answer.get("tree"),
            answer.get("path"),
        )
        if not (
            isinstance(commit, str) and isinstance(tree, str) and isinstance(path, str)
        ):
            return PreviewTree(
                refusal=f"the sandbox sidecar's answer made no sense: {answer!r}"
            )
        return PreviewTree(commit=commit, tree=tree, path=path)

    async def remove_tree(self, lease_id: str) -> bool:
        answer, why = await self._ask(PREVIEW_TREE_REMOVE_ROUTE, {"lease_id": lease_id})
        if answer is None:
            logger.warning("review preview: %s", why)
            return False
        return bool(answer.get("removed"))

    async def probe(self, lease_id: str, port: int, token: str) -> dict[str, Any]:
        answer, why = await self._ask(
            PREVIEW_PROBE_ROUTE, {"lease_id": lease_id, "port": port, "token": token}
        )
        return answer if answer is not None else {"error": why}

    async def fetch_evidence(self, lease_id: str) -> tuple[bytes | None, str | None]:
        answer, why = await self._ask(
            PREVIEW_EVIDENCE_ROUTE,
            {"lease_id": lease_id},
            timeout=self._layout_timeout_s,
            # The base64 of the most a preview may send, and room for the rest
            # of the JSON: anything longer is refused before it is parsed.
            max_bytes=_EVIDENCE_ANSWER_MAX_BYTES,
        )
        if answer is None:
            return None, why
        if answer.get("refusal"):
            return None, str(answer["refusal"])
        encoded = answer.pop("archive", None) or ""
        del answer
        # Held to this side's own limit before decoding, so an oversized
        # answer is never doubled in memory.
        if len(encoded) > (EVIDENCE_TRANSFER_CAP_BYTES // 3 + 1) * 4:
            return None, (
                "the evidence archive is larger than "
                f"{EVIDENCE_TRANSFER_CAP_BYTES // (1024 * 1024)} MiB"
            )
        try:
            return base64.b64decode(str(encoded), validate=True), None
        except (ValueError, TypeError) as exc:
            return None, f"the evidence archive could not be read: {exc}"

    async def list_leases(self) -> set[str] | None:
        answer, why = await self._ask(PREVIEW_PROJECTS_ROUTE, {})
        if answer is None or not isinstance(answer.get("leases"), list):
            logger.warning(
                "review preview: previews could not be listed: %s", why or answer
            )
            return None
        return {str(item) for item in answer["leases"]}

    async def preview_containers(
        self, compose_project: str
    ) -> tuple[set[str], list[tuple[str, str]]] | None:
        """``(leases with a running container, [(container, its lease label)]
        for every container of ``compose_project``)``, or None if unknown."""
        answer, why = await self._ask(
            PREVIEW_PROJECTS_ROUTE, {"compose_project": compose_project}
        )
        if (
            answer is None
            or not isinstance(answer.get("running"), list)
            or not isinstance(answer.get("compose_containers"), list)
        ):
            logger.warning(
                "review preview: the preview's containers could not be listed: %s",
                why or answer,
            )
            return None
        members = [
            (str(item.get("name") or ""), str(item.get("lease") or ""))
            for item in answer["compose_containers"]
            if isinstance(item, dict)
        ]
        return {str(item) for item in answer["running"]}, members

    async def remove_project(self, lease_id: str) -> bool:
        answer, why = await self._ask(
            PREVIEW_PROJECT_REMOVE_ROUTE, {"lease_id": lease_id}
        )
        if answer is None:
            logger.warning("review preview: %s", why)
            return False
        return bool(answer.get("removed"))


# ---------------------------------------------------------------------------
# Evidence storage (on the coordinator's machine)
# ---------------------------------------------------------------------------


def _segments(project: str, feature_id: str, sha: str, run_id: str) -> list[str]:
    parts = [*str(project).split("/"), str(feature_id), str(sha), str(run_id)]
    for part in parts:
        if not _SAFE_SEGMENT.match(part):
            raise ValueError(f"{part!r} cannot be one folder name of the evidence path")
    return parts


def store_evidence_once(
    archive: bytes,
    dest: Path,
    *,
    cap: int = EVIDENCE_TRANSFER_CAP_BYTES,
    max_members: int = EVIDENCE_MEMBER_CAP,
) -> list[str]:
    """Unpack ``archive`` into ``dest`` — once — and make it read-only.

    ``dest`` must not exist: an existing folder is never written into. The
    archive, the total size of what it holds and the number of its members
    are all held to this machine's own limits WHILE unpacking, member by
    member, so a small archive that would unpack into something huge stops
    at the limit (``ValueError``). On any failure part-way the folder this
    call made is removed again. Returns the files kept, relative to ``dest``.
    """
    if len(archive) > cap:
        raise ValueError(
            f"the evidence archive is larger than {cap // (1024 * 1024)} MiB"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(exist_ok=False)  # FileExistsError = refuse to overwrite
    try:
        total = 0
        count = 0
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as unpacked:
            for member in unpacked:
                count += 1
                if count > max_members:
                    raise ValueError(
                        f"the evidence archive holds more than {max_members} entries"
                    )
                total += max(0, int(member.size))
                if total > cap:
                    raise ValueError(
                        "the evidence would unpack to more than "
                        f"{cap // (1024 * 1024)} MiB"
                    )
                unpacked.extract(member, dest, filter="data")
    except BaseException:
        _make_writable(dest)
        shutil.rmtree(dest, ignore_errors=True)
        raise
    kept: list[str] = []
    for path in sorted(dest.rglob("*"), reverse=True):
        if path.is_file() and not path.is_symlink():
            kept.append(path.relative_to(dest).as_posix())
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(0o555)
    dest.chmod(0o555)
    return sorted(kept)


def _make_writable(folder: Path) -> None:
    for path in [folder, *folder.rglob("*")]:
        try:
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        except OSError:
            pass


def delete_evidence(folder: Path, root: Path) -> bool:
    """Delete one run's evidence folder, refusing anything outside ``root``."""
    try:
        resolved, base = folder.resolve(), root.resolve()
    except OSError:
        return False
    if base not in resolved.parents:
        logger.error(
            "review preview: refusing to delete %s, which is outside %s", folder, root
        )
        return False
    if not resolved.exists():
        return True
    _make_writable(resolved)
    shutil.rmtree(resolved, ignore_errors=True)
    return not resolved.exists()


def _relative_ref(ref: str, vm_evidence_dir: str | None) -> str | None:
    """A driver's evidence reference as a path inside the copied folder, or None."""
    text = str(ref or "").strip()
    if not text or "://" in text:
        return None
    if vm_evidence_dir and text.startswith(vm_evidence_dir.rstrip("/") + "/"):
        text = text[len(vm_evidence_dir.rstrip("/")) + 1 :]
    elif text.startswith(EVIDENCE_FOLDER + "/"):
        text = text[len(EVIDENCE_FOLDER) + 1 :]
    elif text.startswith("/"):
        return None
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def _link_refusal(answer: Mapping[str, Any]) -> str | None:
    """None when the link is enforced; otherwise what was wrong, in words."""
    if answer.get("error"):
        return f"it could not be checked ({answer['error']})"
    problems: list[str] = []
    for key, words in (
        ("no_token", "with no token"),
        ("wrong_token", "with a wrong token"),
    ):
        status = answer.get(key)
        if status not in _REFUSED_STATUSES:
            problems.append(
                f"a request {words} got {status if status is not None else 'no answer'}"
            )
    right = answer.get("right_token")
    if not isinstance(right, int) or not 200 <= right < 400:
        problems.append(
            f"a request with the right token got {right if right is not None else 'no answer'}"
        )
    return "; ".join(problems) or None


class ReviewPreviewService:
    """Prepares, keeps and ends review previews (the W7 interface).

    Every seam is injected so the whole of it runs in tests without Docker,
    git or a network: ``profile_reader`` reads a project's deploy profile,
    ``venue_for`` answers the project's sandbox venue (``None`` when it has no
    sandbox), ``deploy_dispatcher`` is the merge press's own deploy dispatch
    (:func:`forge.pipeline.merge_executor.build_in_daemon_deploy_dispatcher`),
    and ``clock`` / ``sleep`` drive time. Use :func:`build_review_preview_service`
    for the production composition.
    """

    def __init__(
        self,
        *,
        store: PreviewLeaseStore,
        profile_reader: Callable[[str], "DeployProfile | None"],
        venue_for: Callable[[str], Any],
        deploy_dispatcher: Callable[..., Awaitable[Any]],
        repo_paths: Mapping[str, str],
        evidence_root: str | Path | None,
        evidence_base_url: str | None,
        public_host: str | None = None,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        lease_id_factory: Callable[[], str] = _new_lease_id,
        run_id_factory: Callable[[], str] = _new_run_id,
        slot_poll_seconds: float = _SLOT_POLL_SECONDS,
        profile_retry_seconds: float = _PROFILE_RETRY_SECONDS,
        blocking_sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self._store = store
        self._profile_reader = profile_reader
        self._venue_for = venue_for
        self._dispatch = deploy_dispatcher
        self._repo_paths = dict(repo_paths)
        self._evidence_root = Path(evidence_root) if evidence_root else None
        self._evidence_base_url = (
            str(evidence_base_url).rstrip("/") if evidence_base_url else None
        )
        self._public_host = public_host or None
        self._clock = clock
        self._sleep = sleep
        self._token_factory = token_factory
        self._lease_id_factory = lease_id_factory
        self._run_id_factory = run_id_factory
        self._slot_poll_seconds = slot_poll_seconds
        self._profile_retry_seconds = profile_retry_seconds
        self._blocking_sleep = blocking_sleep

    def _public_url_of(self, slot: "PreviewSlot") -> str | None:
        """Where a reviewer opens this slot: the profile's own ``public_url``
        when it names one, else ``https://<review_preview.public_host>:<port>``,
        else None (and the preview is not prepared)."""
        if slot.public_url:
            return slot.public_url
        # Checked here too, not only in the settings, so a host that is not a
        # plain DNS name can never reach a link: it counts as no address.
        from forge.deploy.profile import plain_public_host

        host = plain_public_host(self._public_host)
        if host:
            return f"https://{host}:{int(slot.port)}"
        return None

    # -- the switch ---------------------------------------------------------

    def _preview_of(
        self, project: str
    ) -> "tuple[DeployProfile | None, DeployPreview | None, str | None]":
        """``(profile, preview, None)``, or ``(None, None, why)`` when unreadable.

        A profile that cannot be read is asked for once more; a second failure
        is reported, never taken for "no preview" — with previews switched on,
        an unreadable profile must not become an ordinary approvable card.
        A project with no profile at all reads as ``(None, None, None)``.
        """
        why = ""
        for attempt in range(2):
            if attempt:
                # A short pause, so one blip does not withhold Approve. This
                # runs off the event loop (both callers ask from a thread).
                self._blocking_sleep(self._profile_retry_seconds)
            try:
                profile = self._profile_reader(project)
            except Exception as exc:  # noqa: BLE001 — a project's file is input
                why = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "review preview: %s's profile could not be read (%s)", project, why
                )
                continue
            preview = getattr(profile, "preview", None) if profile is not None else None
            if preview is None or not preview.enabled or not preview.slots:
                return profile, None, None
            return profile, preview, None
        return None, None, why

    def enabled_for(self, project: str) -> bool:
        """True when the project's deploy profile has an enabled ``preview`` —
        and when the profile cannot be read, so that preparation fails closed
        with the reason and the card offers no Approve."""
        _profile, preview, unreadable = self._preview_of(project)
        return preview is not None or unreadable is not None

    # -- preparing ------------------------------------------------------------

    async def prepare(self, req: PreviewRequest) -> "ReviewEvidence":
        """Prepare the preview and its evidence. Never raises for expected failures."""
        now = self._clock()
        run_id = self._run_id_factory()
        try:
            return await self._prepare(req, now=now, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — the offer must always get a record
            logger.exception("review preview: preparing %s raised", req.build_id)
            return self._record(
                req,
                state="failed",
                run_id=run_id,
                now=now,
                preview_tree=None,
                failure_reason=f"the preview could not be prepared ({type(exc).__name__}: {exc})",
            )

    async def _prepare(
        self, req: PreviewRequest, *, now: datetime, run_id: str
    ) -> "ReviewEvidence":
        # Off the event loop: the profile is read where the project lives,
        # which for a sandbox is a sidecar call of up to 30 seconds.
        profile, preview, unreadable = await asyncio.to_thread(
            self._preview_of, req.project
        )

        def early(reason: str) -> "ReviewEvidence":
            return self._record(
                req,
                state="failed",
                run_id=run_id,
                now=now,
                preview=preview,
                preview_tree=None,
                failure_reason=reason,
            )

        if unreadable is not None:
            return early(f"could not read the deploy profile ({unreadable})")
        if profile is None or preview is None:
            return early("the project's deploy profile has no preview switched on")
        for preview_slot in preview.slots:
            if self._public_url_of(preview_slot) is None:
                return early(
                    f"no public address configured for preview slot {preview_slot.port}"
                )
        if self._evidence_root is None or not self._evidence_base_url:
            return early(
                "evidence storage is not set up on the coordinator "
                "(review_preview.evidence_root and evidence_base_url)"
            )
        for name in ("candidate_sha", "candidate_tree", "expect_main_sha"):
            if not _SHA.match(str(getattr(req, name))):
                return early(f"the offer's {name} is not a full commit id")
        if not _SHA.match(str(req.preview_base_sha or "")):
            return early(
                f"{req.target_branch} could not be fetched from the remote, so "
                "there is nothing to join the candidate onto"
                + (f": {req.preview_base_refusal}" if req.preview_base_refusal else "")
            )
        venue = self._venue_for(req.project)
        if venue is None:
            return early(
                "previews are prepared only for a repository that has a sandbox"
            )
        repo_root = self._repo_paths.get(req.project)
        if not repo_root:
            return early(f"{req.project} is not a registered repository")
        try:
            dest = self._evidence_dest(req, run_id)
        except ValueError as exc:
            return early(f"the evidence folder cannot be named: {exc}")

        # One preview per build: an earlier one for this build is ended first.
        await self.release(
            feature_id=req.feature_id,
            build_id=req.build_id,
            reason="a new preview was prepared for this build",
        )

        lease_id = self._lease_id_factory()
        token = self._token_factory()
        app_expires = now + timedelta(hours=preview.app_ttl_hours)
        evidence_expires = now + timedelta(days=preview.evidence_days)
        base = PreviewLease(
            lease_id=lease_id,
            slot=None,
            port=None,
            token=token,
            project=req.project,
            feature_id=req.feature_id,
            build_id=req.build_id,
            candidate_sha=req.candidate_sha,
            candidate_tree=req.candidate_tree,
            preview_tree=None,
            run_id=run_id,
            compose_project=None,
            evidence_dir=None,
            state="failed",
            reason=None,
            created_at=now.isoformat(),
            app_expires_at=app_expires.isoformat(),
            evidence_expires_at=evidence_expires.isoformat(),
        )

        # A preparation cancelled before it holds a slot (a restart, an offer
        # that was itself cancelled) still removes its joined tree and writes
        # its lease down as failed — shielded, so the cleanup is not cancelled
        # with it — and then lets the cancellation go on.
        try:
            # (1) Join, and lay the joined tree out.
            laid: PreviewTree = await venue.lay_out(
                lease_id, onto=str(req.preview_base_sha), candidate=req.candidate_sha
            )
            if laid.refusal or not laid.tree or not laid.path:
                reason = (
                    f"the candidate could not be joined onto {req.target_branch}: "
                    f"{laid.refusal or 'no tree came back'}"
                )
                self._store.insert(
                    replace(base, reason=reason, ended_at=now.isoformat()),
                    now=now,
                )
                await venue.remove_tree(lease_id)
                return early(reason)
            base = replace(base, preview_tree=laid.tree)

            # (2) A slot.
            taken = await self._take_slot(base, preview)
            if taken is None:
                reason = (
                    f"no preview slot came free within {preview.slot_wait_minutes} minutes"
                )
                self._store.insert(
                    replace(base, reason=reason, ended_at=self._clock().isoformat()),
                    now=self._clock(),
                )
                await venue.remove_tree(lease_id)
                return self._record(
                    req,
                    state="failed",
                    run_id=run_id,
                    now=now,
                    preview=preview,
                    preview_tree=laid.tree,
                    failure_reason=reason,
                )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._cancelled_before_the_slot(venue, base, lease_id)
            )
            raise
        slot_index, _port = taken
        slot = preview.slots[slot_index]
        compose_project = f"{profile.env_id}-pv{slot_index}"
        vm_evidence_dir = f"{laid.path.rstrip('/')}/{EVIDENCE_FOLDER}"
        # THE VALUES THIS PREVIEW ISSUES TO THE PROJECT (13 October 2026). They
        # travel apart from every other setting, as values the preview issued,
        # so the link token reaches the project's programs while every other
        # credential-shaped name stays refused. The journey's copy also asks
        # for the recording.
        values = {
            "COMPOSE_PROJECT_NAME": compose_project,
            "FACTORY_PREVIEW_PORT": str(slot.port),
            "FACTORY_PREVIEW_INTERNAL_PORT": str(slot.internal_port),
            "FACTORY_PREVIEW_URL": str(self._public_url_of(slot)),
            "FACTORY_PREVIEW_TOKEN": token,
            "FACTORY_RESOURCE_LABEL": f"{LEASE_LABEL}={lease_id}",
            "FACTORY_EVIDENCE_DIR": vm_evidence_dir,
        }
        issued = IssuedPreviewValues(values)
        issued_for_the_journey = IssuedPreviewValues(
            {**values, "FACTORY_RECORD_VIDEO": "1"}
        )
        context = _Preparation(
            req=req,
            run_id=run_id,
            now=now,
            preview=preview,
            lease_id=lease_id,
            token=token,
            slot=slot,
            venue=venue,
            repo_root=str(repo_root),
            tree=laid,
            dest=dest,
            vm_evidence_dir=vm_evidence_dir,
        )
        # From here on the slot is held, so whatever happens — even something
        # nobody expected — ends in a teardown and a failed lease, never in a
        # preview left running under a lease that says "preparing".
        try:
            self._store.update(
                lease_id,
                now=self._clock(),
                compose_project=compose_project,
                evidence_dir=str(dest),
            )
            return await self._stand_up(context, issued, issued_for_the_journey)
        except asyncio.CancelledError:
            # Cancelled while the slot is held (during the start-up, the checks
            # or the journey): torn down and failed, shielded, then re-raised.
            await asyncio.shield(self._cancelled(context))
            raise
        except Exception as exc:  # noqa: BLE001 — never leave the slot behind
            logger.exception("review preview: preparing %s raised", lease_id)
            return await self._fail(
                context,
                f"the preview could not be prepared ({type(exc).__name__}: {exc})",
            )

    async def _cancelled_before_the_slot(
        self, venue: Any, base: PreviewLease, lease_id: str
    ) -> None:
        """Clean up a preparation cancelled before it held a slot."""
        now = self._clock()
        try:
            if self._store.get(lease_id) is None:
                self._store.insert(
                    replace(
                        base,
                        reason="the preparation was cancelled",
                        ended_at=now.isoformat(),
                    ),
                    now=now,
                )
            await venue.remove_tree(lease_id)
        except Exception:  # noqa: BLE001 — a cleanup never raises
            logger.exception("review preview: cleaning up %s raised", lease_id)

    async def _cancelled(self, context: "_Preparation") -> None:
        """Clean up a preparation cancelled while it held a slot."""
        try:
            torn_down = await self._tear_down(context.venue, context.lease_id)
            now = self._clock()
            reason = "the preparation was cancelled"
            self._store.update(
                context.lease_id,
                now=now,
                state="failed" if torn_down else "ending",
                reason=reason if torn_down else f"{reason}{_NOT_CONFIRMED}",
                ended_at=now.isoformat() if torn_down else None,
            )
        except Exception:  # noqa: BLE001 — a cleanup never raises
            logger.exception(
                "review preview: cleaning up %s raised", context.lease_id
            )

    async def _stand_up(
        self,
        context: "_Preparation",
        issued: IssuedPreviewValues,
        issued_for_the_journey: IssuedPreviewValues,
    ) -> "ReviewEvidence":
        """Steps 3 to 7: stand the preview up, check its link, record, keep, report."""
        req, venue, slot = context.req, context.venue, context.slot
        lease_id, token, run_id, now = (
            context.lease_id,
            context.token,
            context.run_id,
            context.now,
        )
        # (3) Stand it up.
        up = await self._leg(context, "preview_up", issued)
        if up is None or getattr(up, "outcome", None) != "complete":
            return await self._fail(
                context, f"the preview could not be started: {self._why(up)}"
            )

        # (3b) Its resources carry the lease's label. Teardown, the sweep and
        # the start-up reconcile find a preview ONLY by that label, so a
        # project that did not apply it would leave a preview nothing could
        # find, while its teardown read as "confirmed".
        # EVERY container of the preview's compose project must carry it: one
        # left unlabelled would outlive the teardown unseen.
        seen = await venue.preview_containers(issued["COMPOSE_PROJECT_NAME"])
        if seen is None:
            return await self._fail(
                context, "the preview's containers could not be listed"
            )
        running, members = seen
        unlabelled = [name for name, label in members if label != lease_id]
        if unlabelled:
            return await self._fail(
                context,
                "the project did not label its preview resources: "
                + ", ".join(unlabelled[:5])
                + (f" and {len(unlabelled) - 5} more" if len(unlabelled) > 5 else "")
                + f" carr{'y' if len(unlabelled) > 1 else 'ies'} no "
                f"{LEASE_LABEL}={lease_id}",
            )
        if lease_id not in running:
            return await self._fail(
                context, "the project did not label its preview resources"
            )

        # (4) The link.
        refused = _link_refusal(await venue.probe(lease_id, slot.port, token))
        if refused:
            return await self._fail(
                context, f"the preview does not enforce its link — {refused}"
            )

        # (5) The journey, recorded.
        journey_leg = await self._leg(context, "preview_journey", issued_for_the_journey)
        if journey_leg is None or getattr(journey_leg, "outcome", None) != "complete":
            return await self._fail(
                context, f"the key journey could not be run: {self._why(journey_leg)}"
            )
        journey = dict((getattr(journey_leg, "detail", {}) or {}).get("journey") or {})

        # (6) Keep the evidence, before anything is torn down.
        kept, why_not = await self._keep_evidence(context)
        if kept is None:
            return await self._fail(
                context, f"the evidence could not be kept: {why_not}"
            )
        context.kept = kept

        # (7) The record.
        verdict = str(journey.get("verdict") or "")
        outcome = _outcome_counts(journey.get("outcome"))
        report = self._report_of(context, journey)
        if verdict != "pass" or (outcome.get("failed") or 0) > 0:
            return await self._fail(
                context,
                f"the key journey did not pass (verdict: {verdict or 'none'})",
                journey=journey,
            )
        if report is None:
            return await self._fail(
                context, "the key journey left no test report", journey=journey
            )
        # A person approves what they could watch: the PASSING journey's own
        # recording and screenshots must be among what was copied. A driver
        # that records only on failure leaves neither for a pass.
        kept = context.kept or []
        if not any(Path(f).suffix.lower() in _VIDEO_SUFFIXES for f in kept) or not any(
            Path(f).suffix.lower() in _SCREENSHOT_SUFFIXES for f in kept
        ):
            return await self._fail(
                context,
                "the passing journey left no video/screenshots "
                "(failure-only recording?)",
                journey=journey,
            )
        self._store.update(lease_id, now=self._clock(), state="ready", reason=None)
        return self._record(
            req,
            state="ready",
            run_id=run_id,
            now=now,
            preview=context.preview,
            preview_tree=context.tree.tree,
            app_url=f"{self._public_url_of(slot)}/?preview={quote(token, safe='')}",
            report_url=self._url(context, report),
            media_urls=[
                self._url(context, rel) for rel in self._media_of(context, journey)
            ],
            outcome=outcome,
        )

    async def _take_slot(
        self, lease: PreviewLease, preview: "DeployPreview"
    ) -> tuple[int, int] | None:
        slots = [(index, slot.port) for index, slot in enumerate(preview.slots)]
        deadline = self._clock() + timedelta(minutes=preview.slot_wait_minutes)
        while True:
            taken = self._store.take_slot(lease, slots=slots, now=self._clock())
            if taken is not None:
                return taken
            # A preview whose day is up is not a reason to keep waiting.
            await self._end_expired(self._clock())
            taken = self._store.take_slot(lease, slots=slots, now=self._clock())
            if taken is not None:
                return taken
            if self._clock() >= deadline:
                return None
            logger.info(
                "review preview: every slot is in use; %s waits for one", lease.build_id
            )
            await self._sleep(self._slot_poll_seconds)

    async def _leg(
        self, context: "_Preparation", leg: str, issued: IssuedPreviewValues
    ) -> Any:
        try:
            return await self._dispatch(
                repo=context.req.project,
                repo_root=Path(context.repo_root),
                feature_id=context.req.feature_id,
                build_id=context.req.build_id,
                correlation_id=f"preview-{context.lease_id}",
                decided_by="review-preview",
                dry_run=False,
                leg=leg,
                candidate_cwd=context.tree.path,
                deploy_run_id=context.run_id,
                issued_env=issued,
            )
        except Exception as exc:  # noqa: BLE001 — a leg's crash is a reason
            return _LegCrashed(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _why(result: Any) -> str:
        if result is None:
            return "the deploy stage is switched off (deploy.enabled)"
        if isinstance(result, _LegCrashed):
            return result.reason
        detail = getattr(result, "detail", {}) or {}
        words = detail.get("refusal") or detail.get("reason")
        step = getattr(result, "failed_step", None)
        if step and words:
            return f"it stopped at {step} — {words}"
        return str(
            words or (f"it stopped at {step}" if step else "no reason was given")
        )

    async def _keep_evidence(
        self, context: "_Preparation"
    ) -> tuple[list[str] | None, str | None]:
        if context.dest.exists():
            return None, (
                f"evidence for run {context.run_id} already exists at {context.dest}; "
                "it is never overwritten"
            )
        archive, why = await context.venue.fetch_evidence(context.lease_id)
        if archive is None:
            return None, why
        try:
            kept = await asyncio.to_thread(store_evidence_once, archive, context.dest)
        except FileExistsError:
            return (
                None,
                f"evidence already exists at {context.dest}; it is never overwritten",
            )
        except (tarfile.TarError, OSError, ValueError) as exc:
            return None, f"the evidence archive could not be unpacked: {exc}"
        return kept, None

    async def _fail(
        self,
        context: "_Preparation",
        reason: str,
        *,
        journey: Mapping[str, Any] | None = None,
    ) -> "ReviewEvidence":
        """Keep what evidence there is, tear the preview down, record the failure."""
        if context.kept is None and not context.dest.exists():
            kept, _why = await self._keep_evidence(context)
            context.kept = kept
        torn_down = await self._tear_down(context.venue, context.lease_id)
        now = self._clock()
        self._store.update(
            context.lease_id,
            now=now,
            state="failed" if torn_down else "ending",
            reason=reason if torn_down else f"{reason}{_NOT_CONFIRMED}",
            ended_at=now.isoformat() if torn_down else None,
            evidence_dir=str(context.dest) if context.kept is not None else None,
        )
        diagnostics = self._diagnostics_of(context, journey or {})
        return self._record(
            context.req,
            state="failed",
            run_id=context.run_id,
            now=context.now,
            preview=context.preview,
            preview_tree=context.tree.tree,
            failure_reason=reason,
            diagnostics_url=self._url(context, diagnostics) if diagnostics else None,
            outcome=_outcome_counts((journey or {}).get("outcome")),
        )

    # -- the evidence's addresses ---------------------------------------------

    def _evidence_dest(self, req: PreviewRequest, run_id: str) -> Path:
        assert self._evidence_root is not None
        return self._evidence_root.joinpath(
            *_segments(req.project, req.feature_id, req.candidate_sha, run_id)
        )

    def _url(self, context: "_Preparation", relative: str) -> str:
        parts = _segments(
            context.req.project,
            context.req.feature_id,
            context.req.candidate_sha,
            context.run_id,
        )
        names = relative.split("/")
        # A report's index.html is linked as its folder ("…/report/"): static
        # file servers redirect a request for ".../index.html" to "./", and
        # behind a path-mounted server (tailscale serve --set-path) that
        # redirect drops the mount's sub-path and lands on the evidence root
        # (seen live on 13 October 2026). Every such server answers the
        # folder with its index.html.
        if names[-1] == "index.html":
            names[-1] = ""
        return "/".join(
            [str(self._evidence_base_url), *(quote(p, safe="") for p in parts)]
            + [quote(p, safe="") for p in names]
        )

    def _kept_ref(self, context: "_Preparation", ref: Any) -> str | None:
        relative = _relative_ref(str(ref or ""), context.vm_evidence_dir)
        if relative and context.kept is not None and relative in context.kept:
            return relative
        return None

    def _report_of(
        self, context: "_Preparation", journey: Mapping[str, Any]
    ) -> str | None:
        named = self._kept_ref(context, journey.get("report_ref"))
        if named:
            return named
        reports = sorted(
            (f for f in context.kept or [] if f.rsplit("/", 1)[-1] == "index.html"),
            key=lambda f: (f.count("/"), f),
        )
        return reports[0] if reports else None

    def _media_of(
        self, context: "_Preparation", journey: Mapping[str, Any]
    ) -> list[str]:
        named: list[str] = []
        for key in ("screenshot_refs", "media_refs"):
            for ref in journey.get(key) or []:
                kept = self._kept_ref(context, ref)
                if kept and kept not in named:
                    named.append(kept)
        if named:
            return named
        return [
            f for f in (context.kept or []) if Path(f).suffix.lower() in _MEDIA_SUFFIXES
        ][:_MEDIA_SCAN_CAP]

    def _diagnostics_of(
        self, context: "_Preparation", journey: Mapping[str, Any]
    ) -> str | None:
        if not context.kept:
            return None
        report = self._report_of(context, journey)
        if report:
            return report
        for ref in journey.get("trace_refs") or []:
            kept = self._kept_ref(context, ref)
            if kept:
                return kept
        return context.kept[0]

    def _record(
        self,
        req: PreviewRequest,
        *,
        state: str,
        run_id: str,
        now: datetime,
        preview_tree: str | None,
        preview: "DeployPreview | None" = None,
        app_url: str | None = None,
        report_url: str | None = None,
        media_urls: Sequence[str] = (),
        diagnostics_url: str | None = None,
        failure_reason: str | None = None,
        outcome: Mapping[str, int | None] | None = None,
    ) -> "ReviewEvidence":
        from nats_core.events import ReviewEvidence, ReviewOutcome

        hours = preview.app_ttl_hours if preview is not None else 24
        days = preview.evidence_days if preview is not None else 14
        # A failure before the join has no joined tree; the candidate's own
        # tree stands in, and the record is failed, so it can never be approved.
        tree = (
            preview_tree
            if preview_tree and _SHA.match(preview_tree)
            else req.candidate_tree
        )
        return ReviewEvidence(
            state=state,
            app_url=app_url,
            report_url=report_url,
            media_urls=list(media_urls),
            diagnostics_url=diagnostics_url,
            failure_reason=failure_reason,
            candidate_sha=req.candidate_sha,
            candidate_tree=req.candidate_tree,
            preview_tree=tree,
            preview_base_sha=(
                req.preview_base_sha
                if _SHA.match(str(req.preview_base_sha or ""))
                else None
            ),
            run_id=run_id,
            created_at=now,
            app_expires_at=now + timedelta(hours=hours),
            evidence_expires_at=now + timedelta(days=days),
            outcome=ReviewOutcome(**dict(outcome or {})),
        )

    # -- ending -----------------------------------------------------------------

    async def _tear_down(self, venue: Any, lease_id: str) -> bool:
        """Remove this lease's labelled resources, then its ref and tree."""
        if venue is None:
            return False
        try:
            removed = bool(await venue.remove_project(lease_id))
        except Exception as exc:  # noqa: BLE001 — never raise past a teardown
            logger.warning(
                "review preview: %s could not be removed (%s)", lease_id, exc
            )
            removed = False
        try:
            if not await venue.remove_tree(lease_id):
                logger.warning(
                    "review preview: %s's tree and ref were not removed", lease_id
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "review preview: %s's tree could not be removed (%s)", lease_id, exc
            )
        return removed

    async def _end(
        self, lease: PreviewLease, *, reason: str, state: str = "ended"
    ) -> None:
        torn_down = await self._tear_down(
            self._venue_for(lease.project), lease.lease_id
        )
        now = self._clock()
        reason = reason.removesuffix(_NOT_CONFIRMED)
        if torn_down:
            self._store.update(
                lease.lease_id,
                now=now,
                state=state,
                reason=reason,
                ended_at=now.isoformat(),
            )
        else:
            self._store.update(
                lease.lease_id,
                now=now,
                state="ending",
                reason=f"{reason}{_NOT_CONFIRMED}",
            )

    async def release(self, *, feature_id: str, build_id: str, reason: str) -> None:
        """Tear down this build's preview and release its lease; idempotent; keeps evidence."""
        try:
            for lease in self._store.live_for_build(build_id):
                if lease.feature_id != feature_id:
                    continue
                await self._end(lease, reason=reason)
        except Exception:  # noqa: BLE001 — a release never raises
            logger.exception("review preview: releasing %s's preview raised", build_id)

    async def _end_expired(self, now: datetime) -> None:
        for lease in self._store.live():
            expiry = _parse_time(lease.app_expires_at)
            if lease.state == "ready" and expiry is not None and now >= expiry:
                await self._end(lease, reason="the preview's time was up")
            elif lease.state == "ending":
                await self._end(lease, reason=lease.reason or "teardown retried")
            elif lease.state == "preparing" and await self._preparation_overran(
                lease, now
            ):
                # A preparation that should long since have finished — its
                # process died, or it was cut off without cleaning up.
                await self._end(
                    lease,
                    reason="its preparation never finished",
                    state="failed",
                )

    async def _preparation_overran(self, lease: PreviewLease, now: datetime) -> bool:
        """Is this preparing lease past the longest a preparation can take?

        The bound is the lease's start, plus the profile's slot wait, plus its
        live-gate timeout, plus :data:`_PREPARATION_MARGIN` for the start-up
        and the evidence copy. A profile that cannot be read uses the
        defaults. Nothing younger than the margin is ever looked at.
        """
        started = _parse_time(lease.created_at)
        if started is None:
            return False
        if now < started + _PREPARATION_MARGIN:
            return False
        slot_wait = timedelta(minutes=30)
        gate = timedelta(seconds=600)
        try:
            profile = await asyncio.to_thread(self._profile_reader, lease.project)
        except Exception:  # noqa: BLE001 — the defaults stand in
            profile = None
        preview = getattr(profile, "preview", None)
        if preview is not None:
            slot_wait = timedelta(minutes=preview.slot_wait_minutes)
        live_gate = getattr(profile, "live_gate", None)
        if live_gate is not None:
            gate = timedelta(seconds=live_gate.timeout_seconds)
        return now >= started + slot_wait + gate + _PREPARATION_MARGIN

    async def sweep(self, *, now: datetime) -> None:
        """Tear down previews past their time; delete evidence past its retention."""
        try:
            await self._end_expired(now)
            for lease in self._store.evidence_to_delete(now):
                if self._evidence_root is None or not lease.evidence_dir:
                    continue
                if delete_evidence(Path(lease.evidence_dir), self._evidence_root):
                    self._store.update(
                        lease.lease_id,
                        now=self._clock(),
                        evidence_deleted_at=now.isoformat(),
                    )
        except Exception:  # noqa: BLE001 — a sweep never raises
            logger.exception("review preview: the sweep raised")

    async def reconcile_on_boot(self) -> None:
        """Leases whose preview is gone end; preview projects with no live lease go.

        Only resources labelled ``forge.preview.lease`` are ever looked at or
        removed.
        """
        try:
            for lease in self._store.live():
                if lease.state == "preparing":
                    await self._end(
                        lease,
                        reason="the coordinator restarted while this preview was being prepared",
                        state="failed",
                    )
                elif lease.state == "ending":
                    await self._end(lease, reason=lease.reason or "teardown retried")
            for project in self._store.projects():
                venue = self._venue_for(project)
                if venue is None:
                    continue
                running = await venue.list_leases()
                if running is None:
                    logger.warning(
                        "review preview: %s's previews could not be listed; nothing "
                        "was reconciled there",
                        project,
                    )
                    continue
                now = self._clock()
                for lease in self._store.live():
                    if (
                        lease.project == project
                        and lease.state == "ready"
                        and lease.lease_id not in running
                    ):
                        await venue.remove_tree(lease.lease_id)
                        self._store.update(
                            lease.lease_id,
                            now=now,
                            state="ended",
                            reason="its preview was no longer running after a restart",
                            ended_at=now.isoformat(),
                        )
                live_ids = {lease.lease_id for lease in self._store.live()}
                for orphan in sorted(running - live_ids):
                    logger.info(
                        "review preview: removing preview %s, which has no live lease",
                        orphan,
                    )
                    await venue.remove_project(orphan)
        except Exception:  # noqa: BLE001 — start-up never fails on this
            logger.exception("review preview: reconciling previews at start-up raised")

    # -- approval -------------------------------------------------------------

    def approval_refusal(
        self, review_evidence: Mapping[str, Any] | None, *, now: datetime
    ) -> str | None:
        """None when approval may proceed; otherwise a plain-English reason."""
        if not isinstance(review_evidence, Mapping):
            return "this offer carries no preview record, so it cannot be approved"
        if review_evidence.get("state") != "ready":
            return (
                "the preview for this offer was not prepared, so it cannot be "
                "approved — reject it and run the feature again"
            )
        expires = _parse_time(review_evidence.get("app_expires_at"))
        if expires is None:
            return "the preview record has no expiry time, so it cannot be approved"
        if now >= expires:
            return (
                f"the preview and its approval window ended at "
                f"{expires.strftime('%a %d %b %H:%M UTC')} — reject it and run the "
                "feature again"
            )
        # The preview must still be standing: a lease ended by a release, the
        # sweep or the start-up reconcile is not approvable, whatever the
        # card's own record says. A ledger that cannot be read raises, which
        # the merge press treats as a check that could not be made.
        lease = self._store.for_run(str(review_evidence.get("run_id") or ""))
        if (
            lease is None
            or lease.state != "ready"
            or lease.candidate_sha != review_evidence.get("candidate_sha")
        ):
            return (
                "the preview for this offer is no longer running"
                + (f" ({lease.reason})" if lease is not None and lease.reason else "")
                + ", so it cannot be approved — reject it and run the feature again"
            )
        folder = self._folder_of(review_evidence)
        if folder is None or not folder.is_dir():
            return (
                "the preview's evidence folder is missing, so this offer cannot be "
                "approved — reject it and run the feature again"
            )
        return None

    def _folder_of(self, review_evidence: Mapping[str, Any]) -> Path | None:
        """The run's evidence folder on this machine, worked out from the report link."""
        if self._evidence_root is None or not self._evidence_base_url:
            return None
        report = str(review_evidence.get("report_url") or "")
        prefix = f"{self._evidence_base_url}/"
        if not report.startswith(prefix):
            return None
        parts = [unquote(p) for p in report[len(prefix) :].split("/")]
        sha, run_id = (
            review_evidence.get("candidate_sha"),
            review_evidence.get("run_id"),
        )
        for index in range(len(parts) - 1):
            if parts[index] == sha and parts[index + 1] == run_id:
                head = parts[: index + 2]
                if any(not _SAFE_SEGMENT.match(p) for p in head):
                    return None
                folder = self._evidence_root.joinpath(*head)
                try:
                    if self._evidence_root.resolve() not in folder.resolve().parents:
                        return None
                except OSError:
                    return None
                return folder
        return None


@dataclass
class _Preparation:
    """One preparation's facts, carried between its steps."""

    req: PreviewRequest
    run_id: str
    now: datetime
    preview: "DeployPreview"
    lease_id: str
    token: str
    slot: "PreviewSlot"
    venue: Any
    repo_root: str
    tree: PreviewTree
    dest: Path
    vm_evidence_dir: str
    kept: list[str] | None = field(default=None)


@dataclass(frozen=True)
class _LegCrashed:
    reason: str


def _outcome_counts(raw: Any) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    source = raw if isinstance(raw, Mapping) else {}
    for name in ("passed", "failed", "skipped", "flaky"):
        value = source.get(name)
        counts[name] = (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else None
        )
    return counts


class UnavailableReviewPreview:
    """Stands in when previews are switched on but the service could not start.

    It keeps the coordinator running and fails every preview CLOSED: each
    project counts as previewing, each preparation comes back as a failed
    record with the reason (so the card offers no Approve), and every approval
    of such a record is refused. Nothing is started, so there is nothing to
    release, sweep or reconcile.
    """

    def __init__(self, error: str, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self.reason = (
            "previews are switched on but the preview service could not start: "
            f"{error}"
        )
        self._clock = clock

    def enabled_for(self, project: str) -> bool:
        return True

    async def prepare(self, req: PreviewRequest) -> "ReviewEvidence":
        from nats_core.events import ReviewEvidence, ReviewOutcome

        now = self._clock()
        return ReviewEvidence(
            state="failed",
            failure_reason=self.reason,
            candidate_sha=req.candidate_sha,
            candidate_tree=req.candidate_tree,
            # Nothing was joined: the candidate's own tree stands in, and the
            # record is failed, so it can never be approved.
            preview_tree=req.candidate_tree,
            preview_base_sha=(
                req.preview_base_sha
                if _SHA.match(str(req.preview_base_sha or ""))
                else None
            ),
            run_id=_new_run_id(),
            created_at=now,
            app_expires_at=now + timedelta(hours=24),
            evidence_expires_at=now + timedelta(days=14),
            outcome=ReviewOutcome(),
        )

    async def release(self, *, feature_id: str, build_id: str, reason: str) -> None:
        return None

    async def sweep(self, *, now: datetime) -> None:
        return None

    async def reconcile_on_boot(self) -> None:
        return None

    def approval_refusal(
        self, review_evidence: Mapping[str, Any] | None, *, now: datetime
    ) -> str | None:
        return self.reason


# ---------------------------------------------------------------------------
# Production composition
# ---------------------------------------------------------------------------


def _profile_reader_for(config: Any) -> Callable[[str], "DeployProfile | None"]:
    """Read a project's committed deploy profile where the project lives.

    For a repository with a sandbox: the sandbox sidecar's
    ``/git/read-file-at-commit`` at its clone's committed ``HEAD`` (the same
    read the merge press makes). Otherwise the copy here, at its committed
    ``HEAD``. Synchronous, because ``enabled_for`` is. ``None`` when the
    project has no profile; RAISES when it cannot be read (a sidecar that
    answers anything but 200, a read refused, a file that cannot be used), so
    the service can retry once and then fail the preview closed.
    """

    def read(project: str) -> "DeployProfile | None":
        from forge.config.sandboxes import sandbox_for
        from forge.deploy.candidate_tree import FileAtCommit, read_file_at_commit_sync
        from forge.pipeline.merge_executor import (
            DEPLOY_PROFILE_PATH,
            _the_profile_out_of,
        )
        from forge.planning.sidecar_git_runner import _urllib_post

        sandbox = sandbox_for(config, project)
        if sandbox is None:
            root = config.planning.target_repo_paths.get(project)
            if not root:
                return None
            answer = read_file_at_commit_sync(Path(root), "HEAD", DEPLOY_PROFILE_PATH)
        else:
            status, decoded = _urllib_post(
                f"{str(sandbox.sidecar_url).rstrip('/')}/git/read-file-at-commit",
                {"repo": project, "commit": "HEAD", "file_path": DEPLOY_PROFILE_PATH},
                30.0,
            )
            if status != 200:
                raise OSError(f"the sandbox sidecar answered {status}")
            answer = FileAtCommit.from_wire(decoded)
        # Unreadable is an error (the caller retries once and then fails the
        # preview closed); a profile the project simply does not have is None.
        if answer.refusal:
            raise OSError(str(answer.refusal))
        if not answer.found:
            return None
        profile, why = _the_profile_out_of(answer, "HEAD")
        if profile is None:
            raise ValueError(why or "the deploy profile could not be used")
        return profile

    return read


def build_review_preview_service(
    config: Any,
    *,
    connection: sqlite3.Connection,
    deploy_dispatcher: Callable[..., Awaitable[Any]],
    clock: Callable[[], datetime] = _utcnow,
) -> ReviewPreviewService:
    """The production service: the ledger's connection, the press's deploy dispatch.

    ``deploy_dispatcher`` is the one the merge executor is given
    (:func:`forge.pipeline.merge_executor.build_in_daemon_deploy_dispatcher`).
    """
    from forge.config.sandboxes import sandbox_for

    def venue_for(project: str) -> SidecarPreviewVenue | None:
        sandbox = sandbox_for(config, project)
        if sandbox is None:
            return None
        return SidecarPreviewVenue(str(sandbox.sidecar_url), repo=project)

    settings = config.review_preview
    return ReviewPreviewService(
        store=PreviewLeaseStore(connection),
        profile_reader=_profile_reader_for(config),
        venue_for=venue_for,
        deploy_dispatcher=deploy_dispatcher,
        repo_paths=dict(config.planning.target_repo_paths),
        evidence_root=settings.evidence_root,
        evidence_base_url=settings.evidence_base_url,
        public_host=settings.public_host,
        clock=clock,
    )
