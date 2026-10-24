"""Trusted ledger binding shared by approval publication, status and admission."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from forge.planning.requirement_amendments import (
    AmendmentRefused,
    FIELD,
    MAX_APPROVAL_BYTES,
    canonical,
    digest,
    parse_json,
    paths,
    request_digest,
    summary,
    tree,
    raw_file,
    validate_proposal,
    validate_absence,
    verify_binding,
)


def has_table(cx: sqlite3.Connection, name: str) -> bool:
    return (
        cx.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def record_plan(
    cx: sqlite3.Connection,
    correlation_id: str,
    proposal: dict[str, Any],
    review: dict[str, Any],
) -> None:
    row = cx.execute(
        "SELECT * FROM planning_runs WHERE correlation_id=?", (correlation_id,)
    ).fetchone()
    if row is None or not review or not row["target_repo"]:
        raise AmendmentRefused(
            "amendment lacks original planning record or semantic review"
        )
    scope = paths(json.loads(row[FIELD]))
    if (
        scope != paths(proposal[FIELD])
        or row["start_commit"] != proposal["base_commit"]
    ):
        raise AmendmentRefused(
            "amendment scope/base differs from original planning record"
        )
    value = {
        **proposal,
        "reviewed_plan_commit": proposal["plan_commit"],
        "qa_receipts": None,
        "repo": row["target_repo"],
        "correlation_id": correlation_id,
        "request_digest": request_digest(
            row["target_repo"], row["request_text"], scope
        ),
        "review_digest": digest(review),
        "semantic_review": review,
    }
    serialized = canonical(value)
    existing = cx.execute(
        "SELECT proposal_json FROM planning_requirement_amendments WHERE correlation_id=?",
        (correlation_id,),
    ).fetchone()
    if existing and existing[0] != serialized:
        raise AmendmentRefused(
            "reviewed amendment already recorded with different bytes"
        )
    cx.execute(
        "INSERT OR IGNORE INTO planning_requirement_amendments VALUES (?, ?)",
        (correlation_id, serialized),
    )
    cx.commit()


def _plan(cx: sqlite3.Connection, correlation_id: str) -> dict[str, Any] | None:
    if not has_table(cx, "planning_requirement_amendments"):
        return None
    row = cx.execute(
        "SELECT proposal_json FROM planning_requirement_amendments WHERE correlation_id=?",
        (correlation_id,),
    ).fetchone()
    return parse_json(row[0], MAX_APPROVAL_BYTES) if row else None


def binding_for_build(
    cx: sqlite3.Connection, build_id: str, *, required: bool = False
) -> dict[str, Any] | None:
    """Exact build ID only. Reads immutable bytes, never project files or HEAD."""
    build = cx.execute("SELECT * FROM builds WHERE build_id=?", (build_id,)).fetchone()
    if build is None:
        raise AmendmentRefused("no exact build for requirement amendment diff")
    if (
        "requirement_amendment_required" in build.keys()
        and not build["requirement_amendment_required"]
    ):
        if required:
            raise AmendmentRefused(
                "build does not carry an originating amendment binding"
            )
        return None
    plan = _plan(cx, build["correlation_id"])
    if not has_table(cx, "build_requirement_amendments"):
        if required or plan:
            raise AmendmentRefused("amendment ledger is unavailable")
        return None
    stored = cx.execute(
        "SELECT * FROM build_requirement_amendments WHERE build_id=?", (build_id,)
    ).fetchone()
    if stored is None:
        if required or plan:
            raise AmendmentRefused("complete stored amendment binding is unavailable")
        return None
    binding = verify_binding(parse_json(stored["binding_json"], MAX_APPROVAL_BYTES))
    if digest(binding) != stored["binding_sha256"]:
        raise AmendmentRefused("stored amendment binding digest mismatch")
    if (
        not plan
        or plan.get("qa_receipts") is None
        or any(binding.get(k) != v for k, v in plan.items())
    ):
        raise AmendmentRefused("amendment differs from its reviewed planning record")
    original = cx.execute(
        "SELECT * FROM planning_runs WHERE correlation_id=?",
        (binding["correlation_id"],),
    ).fetchone()
    if (
        original is None
        or request_digest(
            original["target_repo"],
            original["request_text"],
            json.loads(original[FIELD]),
        )
        != binding["request_digest"]
        or original["start_commit"] != binding["base_commit"]
    ):
        raise AmendmentRefused("amendment original request mismatch")
    if any(
        binding[k] != build[k]
        for k in ("build_id", "repo", "feature_id", "correlation_id")
    ):
        raise AmendmentRefused("amendment build identity mismatch")
    if digest(binding["semantic_review"]) != binding["review_digest"]:
        raise AmendmentRefused("amendment semantic review identity mismatch")
    if binding.get("planning_branch") != build["branch"]:
        raise AmendmentRefused("amendment isolated planning branch mismatch")
    pending = build["pending_approval_request_id"]
    accepted = stored["accepted_request_id"]
    if binding["request_id"] not in (pending, accepted):
        raise AmendmentRefused("amendment approval request identity mismatch")
    if accepted and (
        accepted != binding["request_id"] or not stored["accepted_responder"]
    ):
        raise AmendmentRefused("amendment accepted responder/request mismatch")
    if accepted:
        if not has_table(cx, "build_requirement_amendment_responses"):
            raise AmendmentRefused("durable amendment response ledger is unavailable")
        winner = cx.execute(
            "SELECT * FROM build_requirement_amendment_responses WHERE build_id=? AND request_id=?",
            (build_id, accepted),
        ).fetchone()
        if (
            winner is None
            or winner["decision"] != "approve"
            or winner["responder"] != stored["accepted_responder"]
            or winner["binding_sha256"] != stored["binding_sha256"]
        ):
            raise AmendmentRefused("amendment acceptance differs from first response")
    if stored["source_commit"] and stored["source_commit"] != build["source_commit"]:
        raise AmendmentRefused("amendment admitted source mismatch")
    return binding


def project_approval(cx: sqlite3.Connection, envelope: Any) -> Any:
    payload = envelope.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("details"), dict):
        return envelope
    details = payload["details"]
    build_id = details.get("build_id")
    if not build_id:
        return envelope
    build = cx.execute("SELECT * FROM builds WHERE build_id=?", (build_id,)).fetchone()
    if build is None:
        return envelope
    if (
        "requirement_amendment_required" in build.keys()
        and not build["requirement_amendment_required"]
    ):
        return envelope
    plan = _plan(cx, build["correlation_id"])
    if plan is None:
        if (
            "requirement_amendment_required" in build.keys()
            and build["requirement_amendment_required"]
        ):
            raise AmendmentRefused("required amendment planning binding is unavailable")
        return envelope
    if details.get("recovery"):
        from forge.gating.identity import parse_request_id

        request_build, request_stage, _attempt = parse_request_id(
            payload.get("request_id", "")
        )
        if request_build != build_id:
            raise AmendmentRefused("recovered amendment request/build mismatch")
        if request_stage == "autobuild":
            details = {**details, "stage_label": "autobuild"}
    if details.get("stage_label") != "autobuild":
        # Only the existing pre-build card grants admission authority.
        return envelope
    if plan.get("qa_receipts") is None:
        raise AmendmentRefused("final planning source is not frozen")
    request_id = payload.get("request_id")
    if (
        not request_id
        or request_id != build["pending_approval_request_id"]
        or envelope.correlation_id != build["correlation_id"]
    ):
        raise AmendmentRefused("amendment card does not match pending build/request")
    existing = cx.execute(
        "SELECT * FROM build_requirement_amendments WHERE build_id=?", (build_id,)
    ).fetchone()
    if existing:
        prior = verify_binding(parse_json(existing["binding_json"], MAX_APPROVAL_BYTES))
        if digest(prior) != existing["binding_sha256"]:
            raise AmendmentRefused("stored amendment binding digest mismatch")
        if prior["request_id"] != request_id:
            # Existing defer/rearm increments request IDs. Only the identity
            # changes; the frozen reviewed proposal cannot change with it.
            if existing["accepted_request_id"] or any(
                prior.get(k) != v for k, v in plan.items()
            ):
                raise AmendmentRefused(
                    "cannot replace an accepted or changed amendment card"
                )
            prior["request_id"] = request_id
            cx.execute(
                "UPDATE build_requirement_amendments SET binding_json=?,binding_sha256=? WHERE build_id=? AND accepted_request_id IS NULL",
                (canonical(prior), digest(prior), build_id),
            )
            cx.commit()
    else:
        binding = verify_binding(
            {**plan, "build_id": build_id, "request_id": request_id}
        )
        cx.execute(
            "INSERT INTO build_requirement_amendments(build_id,binding_json,binding_sha256) VALUES (?,?,?)",
            (build_id, canonical(binding), digest(binding)),
        )
        cx.commit()
    # Publication and CLI use exactly the same verified read side.
    binding = binding_for_build(cx, build_id, required=True)
    metadata = json.dumps(
        {
            "request_id": request_id,
            "build_id": build_id,
            "correlation_id": build["correlation_id"],
            "approval_subject": "agents.approval.forge." + build_id,
        },
        separators=(",", ":"),
    )
    if len(metadata) >= 2000:
        raise AmendmentRefused("amendment action metadata exceeds card transport limit")
    enriched = {
        **payload,
        "details": {
            **details,
            "requirement_amendment": binding,
            "rationale": summary(binding),
        },
    }
    answer = envelope.model_copy(update={"payload": enriched})
    if len(answer.model_dump_json().encode("utf-8")) > MAX_APPROVAL_BYTES:
        raise AmendmentRefused("complete amendment approval envelope exceeds 512 KiB")
    return answer


@dataclass(frozen=True)
class ScopedAmendmentResponse:
    """A validated scoped response using durable first-winner dedup semantics."""

    durable: bool = True


def validate_response_identity(
    cx: sqlite3.Connection,
    build_id: str,
    envelope: Any,
    response: Any,
    expected: str | None,
    additional: Any = (),
) -> bool | ScopedAmendmentResponse:
    if not has_table(cx, "build_requirement_amendments"):
        return True
    exists = cx.execute(
        "SELECT 1 FROM build_requirement_amendments WHERE build_id=?", (build_id,)
    ).fetchone()
    if not exists:
        return True
    binding = binding_for_build(cx, build_id, required=True)
    if (
        not expected
        or not response.decided_by
        or response.decided_by not in (expected, *additional)
        or envelope.correlation_id != binding["correlation_id"]
        or response.request_id != binding["request_id"]
    ):
        return False
    if response.decision not in ("approve", "reject", "defer"):
        return False
    return ScopedAmendmentResponse()


def accept_response(
    cx: sqlite3.Connection,
    build_id: str,
    envelope: Any,
    response: Any,
    expected: str | None,
    additional: Any = (),
) -> bool | ScopedAmendmentResponse:
    if not validate_response_identity(
        cx, build_id, envelope, response, expected, additional
    ):
        return False
    if (
        not has_table(cx, "build_requirement_amendments")
        or not cx.execute(
            "SELECT 1 FROM build_requirement_amendments WHERE build_id=?", (build_id,)
        ).fetchone()
    ):
        return True
    binding = binding_for_build(cx, build_id, required=True)
    if not has_table(cx, "build_requirement_amendment_responses"):
        raise AmendmentRefused("durable amendment response ledger is unavailable")
    # The insert arbitrates all subscribers/processes, not just one short-TTL
    # dedup buffer. Bind the claim to the still-pending, exact frozen card in
    # the same transaction that can grant authority. Reject/defer consume the
    # request too; a new rearm request has its own first-winner slot.
    from forge.lifecycle.card_revisions import immediate_transaction

    if cx.in_transaction:
        raise AmendmentRefused("amendment response requires an idle ledger transaction")
    try:
        with immediate_transaction(cx):
            claimed = cx.execute(
                """INSERT OR IGNORE INTO build_requirement_amendment_responses
                    (build_id, request_id, binding_sha256, decision, responder)
                    SELECT a.build_id, ?, a.binding_sha256, ?, ?
                    FROM build_requirement_amendments a JOIN builds b USING (build_id)
                    WHERE a.build_id=? AND a.binding_sha256=?
                      AND a.accepted_request_id IS NULL
                      AND b.pending_approval_request_id=? AND b.status='PAUSED'""",
                (
                    response.request_id,
                    response.decision,
                    response.decided_by,
                    build_id,
                    digest(binding),
                    response.request_id,
                ),
            )
            if claimed.rowcount != 1:
                # A crash may have persisted the first response before the gate
                # dequeued it. A new waiter can recover only that exact decision;
                # an existing waiter suppresses its duplicate independently of TTL.
                winner = cx.execute(
                    """SELECT r.decision, r.responder FROM build_requirement_amendment_responses r
                       JOIN build_requirement_amendments a USING (build_id)
                       JOIN builds b USING (build_id)
                       WHERE r.build_id=? AND r.request_id=? AND r.binding_sha256=?
                         AND a.binding_sha256=r.binding_sha256
                         AND b.pending_approval_request_id=r.request_id AND b.status='PAUSED'""",
                    (build_id, response.request_id, digest(binding)),
                ).fetchone()
                if winner is None or (winner["decision"], winner["responder"]) != (
                    response.decision,
                    response.decided_by,
                ):
                    return False
                return ScopedAmendmentResponse()
            if response.decision == "approve":
                changed = cx.execute(
                    "UPDATE build_requirement_amendments SET accepted_request_id=?, accepted_responder=? WHERE build_id=? AND accepted_request_id IS NULL AND binding_sha256=?",
                    (
                        response.request_id,
                        response.decided_by,
                        build_id,
                        digest(binding),
                    ),
                )
                if changed.rowcount != 1:
                    raise AmendmentRefused("amendment response lost its frozen binding")
    except sqlite3.DatabaseError as exc:
        raise AmendmentRefused("amendment response transaction failed") from exc
    return ScopedAmendmentResponse()


# In-process contenders share a lock; Git CAS and the write-once ledger update
# also arbitrate separate processes. A crash leaves the exact commit recoverable.
_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def admit(
    cx: sqlite3.Connection, build_id: str, runner: Any, repo_path: str
) -> str | None:
    build = cx.execute("SELECT * FROM builds WHERE build_id=?", (build_id,)).fetchone()
    if build is None:
        raise AmendmentRefused("amendment launch has no build record")
    if (
        "requirement_amendment_required" in build.keys()
        and not build["requirement_amendment_required"]
    ):
        return build["source_commit"]
    plan = _plan(cx, build["correlation_id"])
    if not plan:
        if (
            "requirement_amendment_required" in build.keys()
            and build["requirement_amendment_required"]
        ):
            raise AmendmentRefused("required amendment planning binding is unavailable")
        return build["source_commit"]
    binding = binding_for_build(cx, build_id, required=True)
    key = (repo_path, build_id)
    async with _locks.setdefault(key, asyncio.Lock()):
        stored = cx.execute(
            "SELECT * FROM build_requirement_amendments WHERE build_id=?", (build_id,)
        ).fetchone()
        if (
            not stored["accepted_request_id"]
            or stored["accepted_request_id"] != binding["request_id"]
            or not stored["accepted_responder"]
        ):
            raise AmendmentRefused("amendment has no accepted exact owner decision")
        p = binding["plan_commit"]
        if binding["amendment_state"] == "absent":
            for commit in (binding["reviewed_plan_commit"], p):
                await validate_absence(
                    runner=runner,
                    repo_path=repo_path,
                    scope=binding[FIELD],
                    base=binding["base_commit"],
                    plan=commit,
                    feature_id=binding["feature_id"],
                )
            # The explicit no-proposal binding preserves ordinary build source
            # behavior. It authorizes no write and creates no amendment commit.
            return build["source_commit"]
        original_raw = await raw_file(
            runner,
            repo_path,
            p,
            f".guardkit/features/{binding['feature_id']}.requirement-amendments.json",
        )
        checked = await validate_proposal(
            runner=runner,
            repo_path=repo_path,
            raw=original_raw,
            scope=binding[FIELD],
            base=binding["base_commit"],
            plan=p,
            feature_id=binding["feature_id"],
        )
        if any(binding[k] != v for k, v in checked.items()):
            raise AmendmentRefused(
                "admission proposal differs from frozen reviewed bytes"
            )
        source = stored["source_commit"]
        branch = build["branch"]
        if not source:
            observed = await runner.rev_parse(repo_path, branch)
            if observed == p:
                result = await runner.prepare_branch_and_write_tree(
                    repo_path,
                    branch,
                    {c["path"]: c["after_text"] for c in binding["changes"]},
                    "Apply owner-approved requirement amendment [skip ci]",
                    expected_head=p,
                    start_commit=p,
                )
                if result.status == "failed" or not result.sha:
                    # A competing exact apply may have won the CAS. Verify
                    # its entire result below rather than writing again.
                    observed = await runner.rev_parse(repo_path, branch)
                    if observed == p:
                        raise AmendmentRefused(
                            "amendment commit refused: " + str(result.stderr)
                        )
                    source = observed
                else:
                    source = result.sha
            else:
                source = observed
        if (
            not source
            or await runner.rev_parse(repo_path, source + "^1") != p
            or await runner.rev_parse(repo_path, source + "^2") is not None
        ):
            raise AmendmentRefused(
                "amendment source is unavailable or has unexpected parent(s)"
            )
        before, after = (
            await tree(runner, repo_path, p),
            await tree(runner, repo_path, source),
        )
        changed = {
            path
            for path in before.keys() | after.keys()
            if before.get(path) != after.get(path)
        }
        if changed != {c["path"] for c in binding["changes"]}:
            raise AmendmentRefused(
                "amendment commit has unexpected tree changes; preserved for inspection"
            )
        for change in binding["changes"]:
            if (
                await raw_file(runner, repo_path, source, change["path"])
                != change["after_text"]
            ):
                raise AmendmentRefused(
                    "amendment committed bytes do not match approved diff"
                )
        receipt = canonical(
            {
                "base_commit": binding["base_commit"],
                "reviewed_plan_commit": binding["reviewed_plan_commit"],
                "plan_commit": p,
                "source_commit": source,
                "candidate_digest": binding["candidate_digest"],
                "request_id": binding["request_id"],
                "author": "owner-approved-admission",
            }
        )
        cx.execute("BEGIN IMMEDIATE")
        try:
            current = cx.execute(
                "SELECT source_commit FROM build_requirement_amendments WHERE build_id=?",
                (build_id,),
            ).fetchone()[0]
            prior_source = cx.execute(
                "SELECT source_commit FROM builds WHERE build_id=?", (build_id,)
            ).fetchone()[0]
            if current not in (None, source) or prior_source not in (None, source):
                raise AmendmentRefused("admitted amendment source is write-once")
            cx.execute(
                "UPDATE build_requirement_amendments SET source_commit=?, apply_receipt_json=? WHERE build_id=? AND source_commit IS NULL",
                (source, receipt, build_id),
            )
            cx.execute(
                "UPDATE builds SET source_commit=? WHERE build_id=? AND source_commit IS NULL",
                (source, build_id),
            )
            cx.commit()
        except BaseException:
            cx.rollback()
            raise
        return source


def output_claims(
    files: dict[str, str], before_tree: dict[str, Any]
) -> list[dict[str, Any]]:
    """Hash the actual generator map, before a check or Git write can alter it."""
    from forge.planning.requirement_amendments import sha

    if (
        len(files) > 256
        or sum(len(text.encode("utf-8")) for text in files.values())
        > MAX_APPROVAL_BYTES
    ):
        raise AmendmentRefused(
            "QA registration inventory exceeds bounded amendment evidence limit"
        )
    return [
        {
            "path": path,
            "sha256": sha(text),
            "bytes": len(text.encode("utf-8")),
            "mode": before_tree[path].mode if path in before_tree else "100644",
        }
        for path, text in sorted(files.items())
    ]


async def verify_outputs(
    runner: Any, repo_path: str, commit: str, outputs: list[dict[str, Any]]
) -> None:
    from forge.planning.requirement_amendments import sha

    for output in outputs:
        answer = await runner.read_file_at_commit(
            repo_path, commit, output["path"], raw=True
        )
        if (
            not answer.ok
            or not answer.found
            or answer.mode != output["mode"]
            or not isinstance(answer.content, str)
            or sha(answer.content) != output["sha256"]
            or len(answer.content.encode("utf-8")) != output["bytes"]
        ):
            raise AmendmentRefused(
                "QA output differs from generated and validated bytes: "
                + output["path"]
            )


async def freeze_final_plan(
    cx: sqlite3.Connection,
    correlation_id: str,
    runner: Any,
    repo_path: str,
    *,
    pass_stage: str,
    gate_stage: str,
) -> dict[str, Any] | None:
    """Freeze Q from the one current run's ordered producer receipts, never HEAD."""
    plan = _plan(cx, correlation_id)
    if plan is None:
        return None
    p = plan["reviewed_plan_commit"]
    stages = ("feature-plan", pass_stage, gate_stage)
    records = []
    for stage in stages:
        rows = cx.execute(
            "SELECT * FROM planning_run_events WHERE correlation_id=? AND stage_label=? AND status='approved' ORDER BY id",
            (correlation_id, stage),
        ).fetchall()
        if len(rows) != 1:
            raise AmendmentRefused(
                "missing or contradictory amendment planning receipt: " + stage
            )
        row = rows[0]
        records.append((row["id"], json.loads(row["details_json"])))
    if not records[0][0] < records[1][0] < records[2][0]:
        raise AmendmentRefused("amendment planning receipts are out of order")
    reviewed = records[0][1]
    if (
        reviewed.get("sha") != p
        or reviewed.get("feature_id") != plan["feature_id"]
        or reviewed.get("target_repo") != plan["repo"]
        or digest(reviewed.get("semantic_review")) != plan["review_digest"]
    ):
        raise AmendmentRefused("reviewed plan receipt identity mismatch")
    branch = reviewed.get("branch")
    if not isinstance(branch, str) or not branch:
        raise AmendmentRefused("reviewed plan branch identity missing")
    p_tree = await tree(runner, repo_path, p)
    previous = p
    evidence = []
    for stage, (_id, receipt) in zip(stages[1:], records[1:]):
        proof = receipt.get("requirement_amendment_qa")
        if not isinstance(proof, dict) or any(
            proof.get(k) != v
            for k, v in {
                "repo": plan["repo"],
                "correlation_id": correlation_id,
                "feature_id": plan["feature_id"],
                "branch": branch,
                "reviewed_plan_commit": p,
                "parent_commit": previous,
            }.items()
        ):
            raise AmendmentRefused(
                "QA registration provenance is missing or stale: " + stage
            )
        if receipt.get("feature_id") != plan["feature_id"]:
            raise AmendmentRefused("QA registration feature mismatch")
        if receipt.get("skipped"):
            if (
                receipt.get("sha")
                or proof.get("outputs") != []
                or proof.get("commit") != previous
                or not receipt.get("reason_code")
            ):
                raise AmendmentRefused("invalid no-write QA registration receipt")
        else:
            commit = receipt.get("sha")
            if (
                receipt.get("registered_at_sha") != p
                or receipt.get("branch") != branch
                or proof.get("commit") != commit
            ):
                raise AmendmentRefused("QA registration commit/plan mismatch")
            outputs = proof.get("outputs")
            declared = (
                receipt.get("bar_files")
                if stage == pass_stage
                else [receipt.get("gate_file"), receipt.get("registry_file")]
            )
            if (
                not isinstance(outputs, list)
                or not outputs
                or not isinstance(declared, list)
                or sorted(o.get("path", "") for o in outputs) != sorted(declared)
            ):
                raise AmendmentRefused("QA registration output inventory mismatch")
            if commit != previous and (
                await runner.rev_parse(repo_path, commit + "^1") != previous
                or await runner.rev_parse(repo_path, commit + "^2") is not None
            ):
                raise AmendmentRefused(
                    "QA registration commit ancestry is not the recorded sequence"
                )
            prior_tree = await tree(runner, repo_path, previous)
            next_tree = await tree(runner, repo_path, commit)
            changed = {
                path
                for path in prior_tree.keys() | next_tree.keys()
                if prior_tree.get(path) != next_tree.get(path)
            }
            if not changed.issubset(set(declared)):
                raise AmendmentRefused("unexpected QA registration tree delta")
            await verify_outputs(runner, repo_path, commit, outputs)
            previous = commit
        evidence.append({"stage": stage, "event_id": _id, "receipt": receipt})
    q = previous
    q_tree = await tree(runner, repo_path, q)
    for path in reviewed["plan_files"]:
        if p_tree.get(path) is None or p_tree.get(path) != q_tree.get(path):
            raise AmendmentRefused(
                "QA registration changed a reviewed plan artifact: " + path
            )
    # Independently recompute the original exact-artifact semantic identity.
    artifacts = {}
    for path in reviewed["plan_files"]:
        answer = await runner.read_file_at_commit(repo_path, p, path, raw=True)
        if not answer.ok or not answer.found or not isinstance(answer.content, str):
            raise AmendmentRefused("reviewed plan artifact unavailable")
        artifacts[path] = answer.content
    identity = {
        "algorithm": "sha256",
        "digest": digest(artifacts),
        "paths": sorted(artifacts),
    }
    if reviewed["semantic_review"].get("artifact_identity") != identity:
        raise AmendmentRefused("semantic review does not match raw reviewed plan tree")
    manifest = f".guardkit/features/{plan['feature_id']}.requirement-amendments.json"
    if plan["amendment_state"] == "absent":
        for commit in (p, q):
            checked = await validate_absence(
                runner=runner,
                repo_path=repo_path,
                scope=plan[FIELD],
                base=plan["base_commit"],
                plan=commit,
                feature_id=plan["feature_id"],
            )
    else:
        checked = await validate_proposal(
            runner=runner,
            repo_path=repo_path,
            raw=artifacts[manifest],
            scope=plan[FIELD],
            base=plan["base_commit"],
            plan=q,
            feature_id=plan["feature_id"],
        )
    if any(
        plan[k] != checked[k]
        for k in (
            "amendment_state",
            "candidate",
            "candidate_digest",
            "changes",
            "feature_id",
            "base_commit",
            FIELD,
        )
    ):
        raise AmendmentRefused(
            "final planning requirement bytes differ from reviewed proposal"
        )
    final = {
        **plan,
        "plan_commit": q,
        "qa_receipts": evidence,
        "planning_branch": branch,
    }
    if plan.get("qa_receipts") is not None:
        if final != plan:
            raise AmendmentRefused(
                "final amendment source is already frozen differently"
            )
        return plan
    # This is the only P→Q binding update, before any build/card is admitted.
    if cx.execute(
        "SELECT 1 FROM builds WHERE correlation_id=?", (correlation_id,)
    ).fetchone():
        raise AmendmentRefused("cannot finalize amendment after build admission")
    cx.execute(
        "UPDATE planning_requirement_amendments SET proposal_json=? WHERE correlation_id=? AND proposal_json=?",
        (canonical(final), correlation_id, canonical(plan)),
    )
    cx.commit()
    return final


async def begin_qa(
    cx: sqlite3.Connection,
    correlation_id: str,
    runner: Any,
    repo_path: str,
    branch: str,
    files: dict[str, str],
) -> dict[str, Any] | None:
    plan = _plan(cx, correlation_id)
    if plan is None:
        return None
    parent = await runner.rev_parse(repo_path, branch)
    if not parent:
        raise AmendmentRefused("cannot read QA registration starting commit")
    claims = output_claims(files, await tree(runner, repo_path, parent))
    return {
        "repo": plan["repo"],
        "correlation_id": correlation_id,
        "feature_id": plan["feature_id"],
        "branch": branch,
        "reviewed_plan_commit": plan["reviewed_plan_commit"],
        "parent_commit": parent,
        "outputs": claims,
    }


async def finish_qa(
    proof: dict[str, Any] | None, runner: Any, repo_path: str, commit: str
) -> dict[str, Any] | None:
    if proof is None:
        return None
    await verify_outputs(runner, repo_path, commit, proof["outputs"])
    return {**proof, "commit": commit}


def skipped_qa(
    cx: sqlite3.Connection, correlation_id: str, branch: str, parent: str
) -> dict[str, Any] | None:
    plan = _plan(cx, correlation_id)
    if plan is None:
        return None
    return {
        "repo": plan["repo"],
        "correlation_id": correlation_id,
        "feature_id": plan["feature_id"],
        "branch": branch,
        "reviewed_plan_commit": plan["reviewed_plan_commit"],
        "parent_commit": parent,
        "commit": parent,
        "outputs": [],
    }
