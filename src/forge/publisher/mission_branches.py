"""The publisher's own record of the mission branches it was asked to create.

Missions design (17 October 2026), section 4, "Creating the mission branch,
with recovery". Before the publisher pushes a new mission branch it writes
down, in its OWN private folder, which mission asked for which branch at which
commit. That record is what lets a repeated request — the reply was lost, or
either side restarted — be answered "created" without pushing again, and only
when the record shows this very branch was created for this very mission at
this very commit.

WHERE IT LIVES. ``<state_dir>/mission-branches/<project>/<branch>.json``: the
publisher's own folder, which is a volume of its own in the estate and is
nobody else's. It is never the ledger: the publisher still writes nothing to
the ledger, ever.

HOW IT IS WRITTEN. One small file per branch, written to a temporary name,
flushed to disk, renamed over the old one and the folder flushed too, so a
restart finds either the old record or the new one and never half of one.

AGNOSTIC. A project name, a mission id, a branch name and a commit. Nothing
here knows what a project contains.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "CONFIRMED",
    "INTENDED",
    "MissionBranchRecord",
    "TheMissionBranchRecords",
    "TheMissionRecordIsUnreadable",
    "a_mission_branch_name",
    "a_mission_id",
]

#: A mission's id, as the coordinator makes it: ``m-`` and six hexadecimal
#: characters.
_A_MISSION_ID = re.compile(r"^m-[0-9a-f]{6}$")

#: The only branch names this request may create (design section 4).
_A_MISSION_BRANCH = re.compile(r"^mission/m-[0-9a-f]{6}$")

#: What a record says once the remote was read back and had the branch at
#: the commit. A record that says only "intended" was written before a push
#: whose outcome this publisher never saw.
INTENDED: str = "intended"
CONFIRMED: str = "confirmed"


def a_mission_id(value: object) -> str | None:
    """``value`` if it is a mission id, else ``None``."""
    text = str(value or "").strip() if isinstance(value, str) else ""
    return text if _A_MISSION_ID.match(text) else None


def a_mission_branch_name(value: object) -> str | None:
    """``value`` if it is a mission branch name, else ``None``."""
    text = str(value or "").strip() if isinstance(value, str) else ""
    return text if _A_MISSION_BRANCH.match(text) else None


class TheMissionRecordIsUnreadable(RuntimeError):
    """A record is there and could not be read, in one sentence."""


@dataclass(frozen=True)
class MissionBranchRecord:
    """What the publisher wrote down before creating one mission branch."""

    project: str
    mission_id: str
    branch: str
    commit: str
    state: str
    written_at: str

    def is_for(self, *, project: str, mission_id: str, branch: str, commit: str) -> bool:
        """Is this the record of exactly this request?"""
        return (
            self.project == project
            and self.mission_id == mission_id
            and self.branch == branch
            and self.commit == commit
        )


def _folder_name(project: str) -> str:
    """A folder name for a project that two different projects never share."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", project).strip("-.") or "project"
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}"


class TheMissionBranchRecords:
    """The publisher's own records of mission branches, one file per branch."""

    def __init__(self, state_dir: Path) -> None:
        self._root = Path(state_dir) / "mission-branches"

    def where(self, project: str, branch: str) -> Path:
        name = branch.replace("/", "-")
        return self._root / _folder_name(project) / f"{name}.json"

    def read(self, project: str, branch: str) -> MissionBranchRecord | None:
        """The record for this project's branch, ``None`` when there is none.

        Raises :class:`TheMissionRecordIsUnreadable` when a file is there and
        is not a record of this project's branch.
        """
        where = self.where(project, branch)
        try:
            raw = where.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TheMissionRecordIsUnreadable(
                f"the publisher's own record of the branch '{branch}' could not "
                f"be read ({exc.strerror or type(exc).__name__})"
            ) from None
        try:
            decoded = json.loads(raw)
            record = MissionBranchRecord(
                project=str(decoded["project"]),
                mission_id=str(decoded["mission_id"]),
                branch=str(decoded["branch"]),
                commit=str(decoded["commit"]),
                state=str(decoded["state"]),
                written_at=str(decoded["written_at"]),
            )
        except (ValueError, KeyError, TypeError):
            raise TheMissionRecordIsUnreadable(
                f"the publisher's own record of the branch '{branch}' is not a "
                "record it can read"
            ) from None
        if record.project != project or record.branch != branch:
            raise TheMissionRecordIsUnreadable(
                f"the publisher's own record at the place of the branch '{branch}' "
                "names another project or branch"
            )
        return record

    def write(
        self,
        *,
        project: str,
        mission_id: str,
        branch: str,
        commit: str,
        state: str = INTENDED,
    ) -> MissionBranchRecord:
        """Write the record durably, replacing any earlier one. Raises OSError."""
        record = MissionBranchRecord(
            project=project,
            mission_id=mission_id,
            branch=branch,
            commit=commit,
            state=state,
            written_at=datetime.now(timezone.utc).isoformat(),
        )
        where = self.where(project, branch)
        where.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(record.__dict__, sort_keys=True).encode("utf-8")
        temporary = where.with_name(f".{where.name}.{os.getpid()}.tmp")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(handle, body)
            os.fsync(handle)
        finally:
            os.close(handle)
        os.replace(temporary, where)
        folder = os.open(where.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
        return record
