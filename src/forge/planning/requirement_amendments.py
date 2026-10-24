"""Exact requirement proposals. Authority is the original request and build ledger.

This module never executes project code, grants Player exemptions, or reads a
mutable checkout. All content reads name raw committed objects.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from difflib import unified_diff
from typing import Any, Mapping

MAX_CANDIDATE_BYTES = 256 * 1024
MAX_CONTENT_BYTES = 128 * 1024
MAX_APPROVAL_BYTES = 512 * 1024
MAX_RATIONALE_CHARS = 1800
FIELD = "requirement_amendment_paths"
SUFFIX = ".requirement-amendments.json"


class AmendmentRefused(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AmendmentRefused("duplicate JSON key: " + key)
        result[key] = value
    return result


def parse_json(raw: str, limit: int = MAX_CANDIDATE_BYTES) -> Any:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > limit:
        raise AmendmentRefused("requirement amendment exceeds serialized byte limit")
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError) as exc:
        raise AmendmentRefused(
            "invalid requirement amendment JSON: " + str(exc)
        ) from exc


def paths(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise AmendmentRefused(
            "requirement amendment scope must be a list of at most 32 exact paths"
        )
    seen: set[str] = set()
    for path in value:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(c in path for c in "*?[]:")
            or any(unicodedata.category(c).startswith("C") for c in path)
            or unicodedata.normalize("NFC", path) != path
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise AmendmentRefused("requirement amendment path is not canonical")
        key = path.casefold()
        if key in seen or any(
            key.startswith(p + "/") or p.startswith(key + "/") for p in seen
        ):
            raise AmendmentRefused(
                "duplicate, colliding or overlapping amendment paths"
            )
        seen.add(key)
    return tuple(sorted(value))


def request_digest(repo: str, request: str, scope: Any) -> str:
    return digest({"repo": repo, "request_text": request, FIELD: list(paths(scope))})


def candidate(raw: str) -> dict[str, Any]:
    value = parse_json(raw)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "base_commit",
        "changes",
    }:
        raise AmendmentRefused("requirement amendment candidate has unexpected fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise AmendmentRefused("unknown requirement amendment schema")
    if not isinstance(value["base_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", value["base_commit"]
    ):
        raise AmendmentRefused("amendment requires a full base commit")
    changes = value["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= 32:
        raise AmendmentRefused("amendment requires 1 to 32 changes")
    for change in changes:
        if not isinstance(change, dict) or set(change) != {
            "path",
            "before_sha256",
            "after_text",
            "reason",
        }:
            raise AmendmentRefused("amendment change has unexpected fields")
        if not isinstance(change["before_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", change["before_sha256"]
        ):
            raise AmendmentRefused("amendment before hash is malformed")
        if (
            not isinstance(change["after_text"], str)
            or not change["after_text"]
            or "\0" in change["after_text"]
        ):
            raise AmendmentRefused("amendment replacement must be nonempty UTF-8 text")
        if not isinstance(change["reason"], str) or not change["reason"].strip():
            raise AmendmentRefused("amendment requires a reason")
    paths([c["path"] for c in changes])
    if sum(len(c["after_text"].encode("utf-8")) for c in changes) > MAX_CONTENT_BYTES:
        raise AmendmentRefused("amendment exceeds decoded content limit")
    value["changes"] = sorted(changes, key=lambda c: c["path"])
    return value


async def tree(runner: Any, repo_path: str, commit: str) -> dict[str, Any]:
    answer = await runner.list_tree_at_commit(repo_path, commit)
    if not answer.ok:
        raise AmendmentRefused("cannot read amendment tree: " + str(answer.refusal))
    return {entry.path: entry for entry in answer.entries}


async def raw_file(runner: Any, repo_path: str, commit: str, path: str) -> str:
    answer = await runner.read_file_at_commit(repo_path, commit, path, raw=True)
    if (
        not answer.ok
        or not answer.found
        or answer.mode != "100644"
        or not isinstance(answer.content, str)
        or "\0" in answer.content
    ):
        raise AmendmentRefused(
            "amendment target must be an ordinary non-executable UTF-8 file: " + path
        )
    return answer.content


def _covered(path: str, roots: Any) -> bool:
    return isinstance(roots, list) and any(
        isinstance(root, str)
        and (path == root.rstrip("/") or path.startswith(root.rstrip("/") + "/"))
        for root in roots
    )


def _control(path: str, config: Any) -> bool:
    parts = path.casefold().split("/")
    if any(
        p
        in {
            ".git",
            ".guardkit",
            ".agents",
            ".claude",
            ".codex",
            "skills",
            "tasks",
            "qa",
            "tests",
            "test",
            "checks",
            "gates",
            "scripts",
            ".github",
        }
        for p in parts
    ):
        return True
    if parts[-1] in {"agents.md", "claude.md", "skill.md", "security.md"}:
        return True

    # Explicit target-owned test/check inventories, independent of language.
    def declared_role(node: Any) -> bool:
        if isinstance(node, str):
            return _covered(path, [node])
        if isinstance(node, list):
            return any(declared_role(item) for item in node)
        if isinstance(node, dict):
            return any(
                declared_role(value)
                for key, value in node.items()
                if key
                in {
                    "path",
                    "paths",
                    "file",
                    "files",
                    "root",
                    "roots",
                    "directory",
                    "directories",
                }
            )
        return False

    def scan(node: Any) -> bool:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in {
                    "instructions",
                    "test_roots",
                    "acceptance_paths",
                    "gate_paths",
                    "check_paths",
                    "validation_paths",
                    "security_policy",
                    "security_policy_path",
                }:
                    if _covered(path, val if isinstance(val, list) else [val]):
                        return True
                if key in {
                    "command",
                    "commands",
                    "run",
                    "test_command",
                    "coverage_command",
                    "validation_command",
                    "acceptance_command",
                }:
                    import shlex

                    for command in val if isinstance(val, list) else [val]:
                        if isinstance(command, str):
                            try:
                                tokens = shlex.split(command)
                            except ValueError:
                                return True
                            if path in {token.removeprefix("./") for token in tokens}:
                                return True
                if key in {
                    "gate",
                    "gates",
                    "checks",
                    "acceptance",
                    "validation",
                    "tests",
                }:
                    if declared_role(val):
                        return True
                if scan(val):
                    return True
        elif isinstance(node, list):
            return any(scan(item) for item in node)
        return False

    return scan(config)


async def validate_proposal(
    *,
    runner: Any,
    repo_path: str,
    raw: str,
    scope: Any,
    base: str,
    plan: str,
    feature_id: str,
) -> dict[str, Any]:
    import yaml

    proposal = candidate(raw)
    selected = paths(scope)
    if proposal["base_commit"] != base or not selected:
        raise AmendmentRefused("amendment has no matching original request scope/base")
    config = yaml.safe_load(
        await raw_file(runner, repo_path, base, ".guardkit/config.yaml")
    )
    if not isinstance(config, dict):
        raise AmendmentRefused("invalid original project declaration")
    player = (config.get("autobuild") or {}).get("player") or {}
    required = player.get("required_documents") or []
    protected = player.get("protected_paths") or []
    base_tree = await tree(runner, repo_path, base)
    plan_tree = await tree(runner, repo_path, plan)
    bound = []
    total = 0
    for change in proposal["changes"]:
        path = change["path"]
        if (
            path not in selected
            or path not in required
            or not _covered(path, protected)
            or _control(path, config)
        ):
            raise AmendmentRefused(
                "amendment target is outside eligible owner scope: " + path
            )
        for entries in (base_tree, plan_tree):
            if any(
                other != path
                and unicodedata.normalize("NFC", other).casefold() == path.casefold()
                for other in entries
            ):
                raise AmendmentRefused(
                    "amendment target has a colliding tree alias: " + path
                )
            entry = entries.get(path)
            if entry is None or entry.mode != "100644" or entry.kind != "blob":
                raise AmendmentRefused(
                    "amendment target is absent, linked or executable: " + path
                )
        before = await raw_file(runner, repo_path, base, path)
        at_plan = await raw_file(runner, repo_path, plan, path)
        if (
            before != at_plan
            or sha(before) != change["before_sha256"]
            or before == change["after_text"]
        ):
            raise AmendmentRefused(
                "amendment before bytes changed or replacement is a no-op: " + path
            )
        total += len(before.encode("utf-8")) + len(change["after_text"].encode("utf-8"))
        bound.append(
            {
                **change,
                "before_text": before,
                "after_sha256": sha(change["after_text"]),
                "mode": "100644",
            }
        )
    if total > MAX_CONTENT_BYTES:
        raise AmendmentRefused("amendment exceeds aggregate before/after content limit")
    return {
        "amendment_state": "proposed",
        "candidate": proposal,
        "candidate_digest": digest(proposal),
        "changes": bound,
        "feature_id": feature_id,
        "base_commit": base,
        "plan_commit": plan,
        FIELD: list(selected),
    }


async def validate_absence(
    *, runner: Any, repo_path: str, scope: Any, base: str, plan: str, feature_id: str
) -> dict[str, Any]:
    """A missing text blob is insufficient: no entry or subtree may exist."""
    selected = paths(scope)
    if not selected or not re.fullmatch(r"[0-9a-f]{40}", base):
        raise AmendmentRefused("absence binding lacks original scope/base")
    manifest = f".guardkit/features/{feature_id}{SUFFIX}"
    entries = await tree(runner, repo_path, plan)
    if any(path == manifest or path.startswith(manifest + "/") for path in entries):
        raise AmendmentRefused(
            "cannot assert amendment absence: candidate tree entry exists"
        )
    return {
        "amendment_state": "absent",
        "candidate": None,
        "candidate_digest": digest(None),
        "changes": [],
        "feature_id": feature_id,
        "base_commit": base,
        "plan_commit": plan,
        FIELD: list(selected),
    }


def verify_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AmendmentRefused("missing requirement amendment binding")
    for key in (
        "repo",
        "build_id",
        "correlation_id",
        "request_id",
        "feature_id",
        "request_digest",
        "plan_commit",
        "base_commit",
        "candidate_digest",
        "review_digest",
        "reviewed_plan_commit",
    ):
        if not isinstance(value.get(key), str) or not value[key]:
            raise AmendmentRefused("amendment binding missing " + key)
    for key in ("request_digest", "candidate_digest", "review_digest"):
        if not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise AmendmentRefused("amendment binding has malformed " + key)
    for key in ("base_commit", "plan_commit", "reviewed_plan_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", value[key]):
            raise AmendmentRefused("amendment binding has malformed " + key)
    state = value.get("amendment_state")
    if state == "absent":
        if (
            "candidate" not in value
            or value["candidate"] is not None
            or value.get("changes") != []
            or value["candidate_digest"] != digest(None)
            or not paths(value.get(FIELD))
        ):
            raise AmendmentRefused("malformed explicit amendment absence binding")
        return value
    if state != "proposed":
        raise AmendmentRefused("missing explicit requirement amendment state")
    proposal = candidate(canonical(value.get("candidate")))
    if (
        digest(proposal) != value["candidate_digest"]
        or proposal["base_commit"] != value["base_commit"]
    ):
        raise AmendmentRefused("amendment candidate digest/base mismatch")
    scope = paths(value.get(FIELD))
    changes = value.get("changes")
    if not isinstance(changes, list) or len(changes) != len(proposal["changes"]):
        raise AmendmentRefused("missing/truncated amendment changes")
    total = 0
    for change, proposed in zip(changes, proposal["changes"]):
        if not isinstance(change, dict) or set(change) != set(proposed) | {
            "before_text",
            "after_sha256",
            "mode",
        }:
            raise AmendmentRefused("malformed complete amendment diff")
        if (
            any(change[k] != v for k, v in proposed.items())
            or change["path"] not in scope
            or change["mode"] != "100644"
        ):
            raise AmendmentRefused("amendment diff/scope mismatch")
        if (
            not isinstance(change["before_text"], str)
            or "\0" in change["before_text"]
            or sha(change["before_text"]) != change["before_sha256"]
            or sha(change["after_text"]) != change["after_sha256"]
            or change["before_text"] == change["after_text"]
        ):
            raise AmendmentRefused("amendment diff hash mismatch or no-op")
        total += len(change["before_text"].encode("utf-8")) + len(
            change["after_text"].encode("utf-8")
        )
    if total > MAX_CONTENT_BYTES:
        raise AmendmentRefused("amendment diff exceeds decoded limit")
    return value


def summary(binding: Mapping[str, Any]) -> str:
    lead = (
        f"Approve {len(binding['changes'])} exact requirement amendment(s) for build {binding['build_id']}. "
        f"Candidate SHA256: {binding['candidate_digest']}. "
        f"Complete scope and diff: forge status {binding['build_id']} --requirement-amendment-diff"
    )
    if binding.get("amendment_state") == "absent":
        lead = (
            f"No requirement amendment proposed for build {binding['build_id']}. "
            f"Absence marker SHA256 (canonical JSON null): {binding['candidate_digest']}. "
            f"Complete scope and binding: forge status {binding['build_id']} --requirement-amendment-diff"
        )
        full = lead + ". Original scope: " + ", ".join(binding[FIELD])
    else:
        full = lead + ". Paths: " + ", ".join(c["path"] for c in binding["changes"])

    # JSON escaping AND Slack escaping are bounded before transport.
    def escaped_size(text: str) -> int:
        return len(
            json.dumps(
                text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
        )

    if escaped_size(full) <= MAX_RATIONALE_CHARS:
        return full
    short = lead + ". Path list shortened here; the command shows the complete scope."
    if escaped_size(short) > MAX_RATIONALE_CHARS:
        raise AmendmentRefused("amendment summary exceeds card rationale limit")
    return short


def diff_text(binding: Mapping[str, Any]) -> str:
    chunks = [summary(binding)]
    for change in binding["changes"]:
        chunks.append(
            f"\n{change['path']} mode {change['mode']}\nbefore SHA256 {change['before_sha256']}\nafter SHA256 {change['after_sha256']}\n"
        )
        chunks.extend(
            unified_diff(
                change["before_text"].splitlines(keepends=True),
                change["after_text"].splitlines(keepends=True),
                fromfile="before/" + change["path"],
                tofile="after/" + change["path"],
            )
        )
    return "".join(chunks)
