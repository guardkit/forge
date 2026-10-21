"""The immutable first-profile input and its current publication authority.

This is a reference to an approved file, never a deployment verdict. The
candidate-check input is its only stored copy; the publication row supplies
the current turn after a takeover.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from forge.deploy.profile import DeployProfileError


class ProfileSelectionRefusedBeforeDispatch(DeployProfileError):
    """Profile validation refused before any deployment request was sent."""


@dataclass(frozen=True)
class FirstDeploymentProfile:
    version: int
    repo: str
    build: str
    offer_id: str
    decision_id: str
    candidate_commit: str
    candidate_tree: str
    start_commit: str
    target_commit: str
    joined_commit: str
    joined_tree: str
    profile_sha256: str
    attempt: int
    candidate_cwd: str

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_wire(cls, value: object) -> FirstDeploymentProfile:
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError("the first deployment profile reference is malformed")
        for name in ("version", "attempt"):
            if type(value[name]) is not int or value[name] < 1:
                raise ValueError(f"the profile reference has no valid {name}")
        if value["version"] != 1:
            raise ValueError("the profile reference version is unsupported")
        for name in ("repo", "build", "offer_id", "decision_id", "candidate_cwd"):
            if not isinstance(value[name], str) or not value[name].strip():
                raise ValueError(f"the profile reference has no {name}")
        for name in ("candidate_commit", "candidate_tree", "start_commit",
                     "target_commit", "joined_commit", "joined_tree"):
            if not isinstance(value[name], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value[name]):
                raise ValueError(f"the profile reference needs a full {name}")
        if not isinstance(value["profile_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["profile_sha256"]):
            raise ValueError("the profile reference has no content digest")
        path = PurePosixPath(value["candidate_cwd"])
        if not path.is_absolute() or ".." in path.parts or path.parent.name != ".forge-candidates":
            raise ValueError("the profile reference names no candidate archive")
        return cls(**value)


def parse_profile_reference(value: object) -> tuple[FirstDeploymentProfile, int]:
    if not isinstance(value, dict) or set(value) != {"selection", "turn"}:
        raise ValueError("the deployment profile reference is malformed")
    if type(value["turn"]) is not int or value["turn"] < 1:
        raise ValueError("the deployment profile reference has no live turn")
    return FirstDeploymentProfile.from_wire(value["selection"]), value["turn"]


def read_profile_approval(connection: Any, build: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the latest real offer and decision using the caller's snapshot."""
    rows = connection.execute(
        "SELECT target_identifier, status, details_json FROM stage_log "
        "WHERE build_id = ? AND target_identifier IN (?, ?) ORDER BY rowid",
        (build, "merge_deploy_offer", "merge_deploy_decision"),
    ).fetchall()
    latest = {str(row[0]): row for row in rows}
    try:
        offered = latest["merge_deploy_offer"]
        decided = latest["merge_deploy_decision"]
        offer = json.loads(offered[2])["merge_offer"]
        decision = json.loads(decided[2])["merge_decision"]
        if (decided[1] != "PASSED" or decision["decision"] != "approve"
                or not offer["request_id"]
                or decision["request_id"] != offer["request_id"]
                or not decision["execution_attempt_id"]):
            raise ValueError("the latest offer has no matching approved decision")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("the first deployment needs a durable approved offer and decision") from exc
    return offer, decision


def selection_from_record(record: Any) -> FirstDeploymentProfile | None:
    """Read only this attempt's canonical candidate-check input."""
    matching = [line for line in record.lines
                if line.kind == "about to" and line.step == "candidate-check"
                and line.attempt == record.attempt
                and line.detail.get("j_commit") == record.j_commit]
    if not matching:
        return None
    raw = matching[-1].detail.get("first_deployment_profile")
    if raw is None:
        return None
    selected = FirstDeploymentProfile.from_wire(raw)
    if any(line.detail.get("first_deployment_profile") != raw for line in matching):
        raise ValueError("the current attempt has contradictory profile selections")
    if (selected.build != record.build_id or selected.repo != record.repo
            or selected.attempt != record.attempt
            or selected.candidate_commit != record.build_tip
            or selected.target_commit != record.g_commit
            or selected.joined_commit != record.j_commit
            or matching[-1].detail.get("j_tree") != selected.joined_tree):
        raise ValueError("the profile selection contradicts the current publication")
    checked = record.checked
    completed = any(line.kind == "done" and line.step == "candidate-check"
                    and line.attempt == record.attempt for line in record.lines)
    if completed and (checked.get("j_commit") != selected.joined_commit
                    or checked.get("j_tree") != selected.joined_tree):
        raise ValueError("the checked artifact contradicts the profile selection")
    return selected


def confirmed_profile_reference(connection: Any, build: str, start_commit: str) -> dict[str, Any] | None:
    """The live authority; caller must hold one consistent read transaction."""
    from forge.pipeline.publication_record import PublicationRecordStore

    store = PublicationRecordStore(connection)
    record = store.read(build)
    selected = selection_from_record(record)
    if selected is None:
        return None
    outcome = store.read_outcome(build)
    if (not record.lease_is_live(datetime.now(UTC)) or record.turn < 1 or outcome is None
            or outcome.turn != record.turn or outcome.finished_at
            or outcome.attempt_id != selected.decision_id):
        raise ValueError("the first deployment publication is no longer active")
    offer, decision = read_profile_approval(connection, build)
    row = connection.execute("SELECT repo, start_commit FROM builds WHERE build_id = ?", (build,)).fetchone()
    if (row is None or row[0] != selected.repo or row[1] != start_commit
            or selected.start_commit != start_commit
            or selected.offer_id != offer.get("request_id")
            or selected.decision_id != decision.get("execution_attempt_id")
            or selected.repo != offer.get("repo")
            or selected.candidate_commit != offer.get("candidate_sha")
            or selected.candidate_tree != offer.get("candidate_tree")):
        raise ValueError("the first deployment approval no longer matches the selection")
    return {"selection": selected.to_wire(), "turn": record.turn}


def confirmed_deployment_layout(connection: Any, build: str, start_commit: str) -> dict[str, Any] | None:
    """Bind the stale-clone fallback to the current, approved archive layout."""
    from forge.pipeline.publication_record import PublicationRecordStore

    store = PublicationRecordStore(connection)
    record = store.read(build)
    if not record.recorded:
        return None
    lines = [line for line in record.lines if line.kind == "about to"
             and line.step == "candidate-check" and line.attempt == record.attempt
             and line.detail.get("j_commit") == record.j_commit]
    if not lines or not lines[-1].detail.get("candidate_cwd"):
        return None
    outcome = store.read_outcome(build)
    if (not record.lease_is_live(datetime.now(UTC)) or outcome is None
            or outcome.turn != record.turn or outcome.finished_at):
        raise ValueError("the deployment layout has no active publication")
    offer, decision = read_profile_approval(connection, build)
    if (offer.get("repo") != record.repo or offer.get("candidate_sha") != record.build_tip
            or decision.get("execution_attempt_id") != outcome.attempt_id):
        raise ValueError("the deployment layout no longer matches the approved publication")
    detail = lines[-1].detail
    for value in (record.j_commit, detail.get("j_tree"), record.build_tip,
                  start_commit, offer.get("candidate_tree")):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            raise ValueError("the deployment layout needs full immutable identities")
    return {"repo": record.repo, "build": build, "start_commit": start_commit,
            "attempt": record.attempt, "turn": record.turn,
            "joined_commit": record.j_commit, "joined_tree": detail["j_tree"],
            "candidate_cwd": detail["candidate_cwd"]}
