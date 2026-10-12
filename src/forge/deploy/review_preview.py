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
   dispatch the merge press uses, with the preview overlay (``PREVIEW=1``,
   the slot's ports and address, the link token, the resource label
   ``forge.preview.lease=<lease>``, the evidence folder and the compose
   project ``<env>-pv<slot>``).
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

ONLY ``forge.preview.lease`` IS EVER TOUCHED. Teardown, the sweep and the
start-up reconciliation find and remove containers, networks and volumes by
that one label and its exact value. The disposable runs' own label
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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nats_core.events import ReviewEvidence

    from forge.deploy.profile import DeployPreview, DeployProfile, PreviewSlot

logger = logging.getLogger(__name__)

__all__ = [
    "EVIDENCE_FOLDER",
    "LEASE_LABEL",
    "PREVIEW_ENV_KEYS",
    "PREVIEW_ROUTES",
    "PreviewLease",
    "PreviewLeaseStore",
    "PreviewRequest",
    "PreviewTree",
    "ReviewPreviewService",
    "SidecarPreviewVenue",
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

#: Every setting name the preview overlay can carry. The sandbox's deploy
#: sidecar permits these, and only for a project whose committed profile has
#: its preview switched on.
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

#: A lease id: ``pv`` and twelve lower-case hex digits. It becomes a git ref
#: name, a folder name and a label value, so it is held to one plain shape.
_LEASE_ID = re.compile(r"^pv[0-9a-f]{12}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: A lease holds its slot in these states.
_LIVE_STATES: tuple[str, ...] = ("preparing", "ready", "ending")

#: Answers that mean "this link is refused": the project's proxy says 410 for
#: a different or expired candidate; 403 is accepted as the same refusal.
_REFUSED_STATUSES: frozenset[int] = frozenset({403, 410})

#: The most evidence one preview may send out of its sandbox.
EVIDENCE_TRANSFER_CAP_BYTES: int = 256 * 1024 * 1024

#: Said after a reason whose teardown has not been confirmed yet.
_NOT_CONFIRMED: str = " (teardown not confirmed yet)"

#: How often a preparation waiting for a free slot looks again.
_SLOT_POLL_SECONDS: float = 15.0

#: File kinds read as media when the driver named none.
_MEDIA_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webm", ".mp4"})
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
    """Run one fixed argument list (no shell), with a one-minute wall."""
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
                f"{_last_line(err) or _last_line(out) or f'exit code {code}'}"
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

    Regular files and folders only: a link inside the folder is left out
    rather than followed, so nothing outside it can be sent.
    """
    folder = preview_tree_path(repo_root, lease_id) / EVIDENCE_FOLDER
    if not folder.is_dir():
        return {"refusal": f"the preview left no evidence folder ({folder})"}
    total = 0
    buffer = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                continue
            relative = path.relative_to(folder).as_posix()
            if path.is_dir():
                archive.add(str(path), arcname=relative, recursive=False)
            elif path.is_file():
                total += path.stat().st_size
                if total > cap:
                    return {
                        "refusal": (
                            f"the preview's evidence is larger than {cap // (1024 * 1024)} "
                            "MiB, which is more than one preview may send"
                        )
                    }
                archive.add(str(path), arcname=relative, recursive=False)
                count += 1
    return {
        "archive": base64.b64encode(buffer.getvalue()).decode("ascii"),
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


def list_preview_leases(run: CommandRunner = _run_command) -> dict[str, Any]:
    """The lease ids of every container, network and volume carrying the label."""
    engine = shutil.which("docker") or "docker"
    found: set[str] = set()
    template = '{{.Label "' + LEASE_LABEL + '"}}'
    for kind in (["ps", "--all"], ["network", "ls"], ["volume", "ls"]):
        lines = _engine_lines(
            run,
            [engine, *kind, "--filter", f"label={LEASE_LABEL}", "--format", template],
        )
        if lines is None:
            return {"refusal": "the container engine could not be asked about previews"}
        found.update(line for line in lines if _LEASE_ID.match(line))
    return {"leases": sorted(found)}


def remove_preview_resources(
    lease_id: str, run: CommandRunner = _run_command
) -> dict[str, Any]:
    """Remove the containers, networks and volumes labelled for this lease, only."""
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
    for listing, removal in plans:
        ids = _engine_lines(run, [engine, *listing])
        if ids is None:
            return {
                "removed": False,
                "refusal": "the container engine could not be asked",
            }
        if ids:
            run([engine, *removal, *ids])
    remaining = 0
    for listing, _removal in plans:
        ids = _engine_lines(run, [engine, *listing])
        if ids is None:
            return {
                "removed": False,
                "refusal": "the container engine could not be asked",
            }
        remaining += len(ids)
    return {"removed": remaining == 0, "remaining": remaining}


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
            answer = list_preview_leases(run)
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
        self, route: str, body: dict[str, Any], *, timeout: float | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        url = f"{self._base_url}{route}"
        try:
            status, decoded = await asyncio.to_thread(
                self._post,
                url,
                {"repo": self._repo, **body},
                timeout or self._timeout_s,
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
        )
        if answer is None:
            return None, why
        if answer.get("refusal"):
            return None, str(answer["refusal"])
        try:
            return base64.b64decode(
                str(answer.get("archive") or ""), validate=True
            ), None
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


def store_evidence_once(archive: bytes, dest: Path) -> list[str]:
    """Unpack ``archive`` into ``dest`` — once — and make it read-only.

    ``dest`` must not exist: an existing folder is never written into. On a
    failure part-way the folder this call made is removed again. Returns the
    files kept, relative to ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(exist_ok=False)  # FileExistsError = refuse to overwrite
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as unpacked:
            unpacked.extractall(dest, filter="data")
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
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        lease_id_factory: Callable[[], str] = _new_lease_id,
        run_id_factory: Callable[[], str] = _new_run_id,
        slot_poll_seconds: float = _SLOT_POLL_SECONDS,
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
        self._clock = clock
        self._sleep = sleep
        self._token_factory = token_factory
        self._lease_id_factory = lease_id_factory
        self._run_id_factory = run_id_factory
        self._slot_poll_seconds = slot_poll_seconds

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
        for _attempt in range(2):
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
        slot_index, _port = taken
        slot = preview.slots[slot_index]
        compose_project = f"{profile.env_id}-pv{slot_index}"
        vm_evidence_dir = f"{laid.path.rstrip('/')}/{EVIDENCE_FOLDER}"
        overlay = {
            "COMPOSE_PROJECT_NAME": compose_project,
            "FACTORY_PREVIEW_PORT": str(slot.port),
            "FACTORY_PREVIEW_INTERNAL_PORT": str(slot.internal_port),
            "FACTORY_PREVIEW_URL": slot.public_url,
            "FACTORY_PREVIEW_TOKEN": token,
            "FACTORY_RESOURCE_LABEL": f"{LEASE_LABEL}={lease_id}",
            "FACTORY_EVIDENCE_DIR": vm_evidence_dir,
        }
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
            return await self._stand_up(context, overlay)
        except Exception as exc:  # noqa: BLE001 — never leave the slot behind
            logger.exception("review preview: preparing %s raised", lease_id)
            return await self._fail(
                context,
                f"the preview could not be prepared ({type(exc).__name__}: {exc})",
            )

    async def _stand_up(
        self, context: "_Preparation", overlay: dict[str, str]
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
        up = await self._leg(context, "preview_up", overlay)
        if up is None or getattr(up, "outcome", None) != "complete":
            return await self._fail(
                context, f"the preview could not be started: {self._why(up)}"
            )

        # (4) The link.
        refused = _link_refusal(await venue.probe(lease_id, slot.port, token))
        if refused:
            return await self._fail(
                context, f"the preview does not enforce its link — {refused}"
            )

        # (5) The journey, recorded.
        journey_leg = await self._leg(
            context, "preview_journey", {**overlay, "FACTORY_RECORD_VIDEO": "1"}
        )
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
        self._store.update(lease_id, now=self._clock(), state="ready", reason=None)
        return self._record(
            req,
            state="ready",
            run_id=run_id,
            now=now,
            preview=context.preview,
            preview_tree=context.tree.tree,
            app_url=f"{slot.public_url}/?preview={quote(token, safe='')}",
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
        self, context: "_Preparation", leg: str, overlay: dict[str, str]
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
                identity_env=dict(overlay),
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
        return "/".join(
            [str(self._evidence_base_url), *(quote(p, safe="") for p in parts)]
            + [quote(p, safe="") for p in relative.split("/")]
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
        clock=clock,
    )
