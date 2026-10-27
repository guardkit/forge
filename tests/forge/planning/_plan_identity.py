"""The specialist's canonical identity for a plan tree, as its review record
carries it. Forge no longer checks the record against the tree (the cull,
27 October 2026); the tests still write records in the shape the planning
seat sends."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def plan_artifact_identity(files: Mapping[str, str]) -> dict[str, Any]:
    tree = {str(path): str(content) for path, content in files.items()}
    canonical = json.dumps(
        tree, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "algorithm": "sha256",
        "digest": hashlib.sha256(canonical).hexdigest(),
        "paths": sorted(tree),
    }
