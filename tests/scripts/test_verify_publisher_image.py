"""The publisher image's proof refuses an image with anything where keys go.

13 October 2026, the publisher's per-route credentials. The route keys are
mounted at run time as one read-only folder, /etc/forge-publisher/route-keys,
exactly as the publisher-wide credential is mounted as one file; an image that
already had something there would carry a key into every copy of itself, or
a place for one. The image proof checks that path the way it checks the
credential's.

NOTHING REAL IS RUN. ``docker`` here is a stand-in placed first on PATH: it
answers each of the proof's questions the way a good image would, reading the
publisher's package from this checkout for the provenance step, and is told
by the test whether anything is at the route-keys path. No image is pulled,
built or started, and no network is touched.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify-publisher-image.sh"

_STAND_IN_DOCKER = r'''#!/usr/bin/env python3
"""A stand-in docker answering the publisher image proof's questions."""
import os, subprocess, sys
args = sys.argv[1:]
if args[:3] != ["run", "--rm", "--entrypoint"]:
    sys.exit(1)
entrypoint, rest = args[3], args[5:]
if entrypoint == "python":
    print("usage: forge-publisher --settings SETTINGS [--log-level LEVEL]")
    sys.exit(0)
script = rest[1]
if "find forge/publisher" in script:
    sys.exit(subprocess.run(["sh", "-c", script, "manifest", os.environ["STAND_IN_SRC"]]).returncode)
if "id -u" in script:
    print("1000:1000:publisher", end="")
    sys.exit(0)
if "can-this-user-write-here" in script:
    print("1000:1000 writable")
    sys.exit(0)
if "command -v git" in script:
    sys.exit(0)
if "for t in docker" in script:
    sys.exit(0)
if "/etc/forge-publisher/route-keys" in script:
    sys.exit(1 if os.environ.get("STAND_IN_ROUTE_KEYS_IN_IMAGE") else 0)
if "/etc/forge-publisher/credential" in script:
    sys.exit(0)
sys.exit(1)
'''


def _prove(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    docker = tools / "docker"
    docker.write_text(_STAND_IN_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    called = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "STAND_IN_SRC": str(REPO_ROOT / "src"),
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "forge-publisher:stand-in"],
        capture_output=True,
        text=True,
        env=called,
        timeout=120,
    )


def test_a_good_image_passes_and_the_route_keys_path_was_asked(tmp_path: Path) -> None:
    done = _prove(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "nothing at the path the route keys are mounted at" in done.stdout
    assert "verification PASSED" in done.stdout


def test_an_image_with_anything_at_the_route_keys_path_fails(tmp_path: Path) -> None:
    done = _prove(tmp_path, STAND_IN_ROUTE_KEYS_IN_IMAGE="1")
    assert done.returncode == 1
    assert "/etc/forge-publisher/route-keys" in done.stderr
    assert "never in an image" in done.stderr
    assert "PASSED" not in done.stdout
