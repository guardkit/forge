"""The release build script plans EVERY image the manifest names.

WHY THIS TEST EXISTS (24 September 2026, stage 2b of the containerisation
rollout gate). The release used to build one image, the coordinator's, and the
estate runs two — the publisher has an image of its own because it holds the
one credential that can write to a project's remote, and that separateness is
the wall. Because nothing built it, every proof of the publisher so far ran
the coordinator's image with the publisher's start line: a stand-in.

So the manifest now names a SET of images and one run builds all of them from
one set of fetched clones. These tests exercise ``--plan-only``, which reads
and checks the manifest and then stops: nothing is fetched, nothing is built,
no tag is written, no network is touched. That is the whole of what can be
checked without a build, and it is the part that decides what a build would
do — so a manifest that would build the wrong set, or tag over something, or
name a file that is not there, fails here rather than twenty minutes into a
release.

The shipped manifest is read as well, so the repository's own release cannot
quietly go back to naming one image.

Nothing here names a target project's language, test runner, package manager
or layout: it runs one shell script over manifests written in tmp_path.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build-release-image.sh"
SHIPPED_MANIFEST = REPO_ROOT / "release" / "manifest.yaml"

#: The script asks for docker before it reads anything, because a build needs
#: it. ``--plan-only`` never starts a container and never needs a daemon, but
#: the binary has to be on PATH for the script to get that far.
needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="the release script requires the docker client on PATH before it reads a manifest",
)

_BASE_DIGEST = "sha256:2e256d0381371566ed96980584957ed31297f437569b79b0e5f7e17f2720e53a"

#: Full 40-character commits. They are never fetched in plan mode; they only
#: have to look like pins, which is what the script checks.
_FORGE_COMMIT = "a" * 40
_OTHER_COMMIT = "b" * 40


def _repositories() -> str:
    return f"""
repositories:
  - name: forge
    url: https://example.invalid/forge.git
    branch: main
    commit: {_FORGE_COMMIT}
    role: build-context-root
  - name: other
    url: https://example.invalid/other.git
    branch: main
    commit: {_OTHER_COMMIT}
    role: named-context
"""


def _manifest(tmp_path: Path, body: str, name: str = "manifest.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _plan(manifest: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the script in plan mode, from a directory that is not a checkout."""
    return subprocess.run(
        ["bash", str(SCRIPT), str(manifest), "--plan-only", *extra],
        cwd=str(manifest.parent),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _two_image_manifest(tmp_path: Path, version: str = "0.0.0-test") -> Path:
    return _manifest(
        tmp_path,
        f"""
schema: 2
version: {version}
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
    proof: scripts/prove-the-coordinator.sh
  - name: the-publisher
    dockerfile: src/somewhere/Dockerfile
    role: publisher
    proof: scripts/prove-the-publisher.sh
""",
    )


# ---------------------------------------------------------------------------
# The plan says what a build would do — for every image
# ---------------------------------------------------------------------------


@needs_docker
def test_the_plan_names_both_images_their_files_and_their_roles(tmp_path):
    result = _plan(_two_image_manifest(tmp_path))

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "would build 2 image(s)" in out
    assert "the-coordinator [coordinator] from forge/Dockerfile" in out
    assert "the-publisher [publisher] from forge/src/somewhere/Dockerfile" in out


@needs_docker
def test_the_plan_names_both_tag_sets(tmp_path):
    """Two tags per image: the commit the release is built from, and the version."""
    result = _plan(_two_image_manifest(tmp_path, version="0.0.0-tags"))

    assert result.returncode == 0, result.stderr
    for name in ("the-coordinator", "the-publisher"):
        assert f"would tag : {name}:{_FORGE_COMMIT}" in result.stdout
        assert f"would tag : {name}:0.0.0-tags" in result.stdout


@needs_docker
def test_the_plan_names_the_proof_each_image_must_pass(tmp_path):
    result = _plan(_two_image_manifest(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "proved by : forge/scripts/prove-the-coordinator.sh" in result.stdout
    assert "proved by : forge/scripts/prove-the-publisher.sh" in result.stdout


@needs_docker
def test_an_image_that_names_no_proof_says_so_rather_than_looking_proved(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-unproved
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
""",
    )

    result = _plan(manifest)

    assert result.returncode == 0, result.stderr
    assert "proved by : nothing — this image names no proof" in result.stdout


@needs_docker
def test_the_plan_fetches_nothing_and_builds_nothing(tmp_path):
    """The urls above do not resolve, so a plan that reached the network would fail."""
    result = _plan(_two_image_manifest(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "Nothing was fetched and nothing was built (--plan-only)." in result.stdout
    assert not list(tmp_path.glob("*.json")), "plan mode wrote a receipt"


# ---------------------------------------------------------------------------
# The refusals, which apply to every image
# ---------------------------------------------------------------------------


@needs_docker
def test_two_images_under_one_name_are_refused(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-twice
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
  - name: the-coordinator
    dockerfile: other/Dockerfile
    role: publisher
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "both called 'the-coordinator'" in result.stderr


@needs_docker
def test_an_image_with_no_dockerfile_is_refused_by_name(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-nofile
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
  - name: the-publisher
    role: publisher
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "image 'the-publisher' has no dockerfile" in result.stderr


@needs_docker
def test_a_dockerfile_outside_the_fetched_clone_is_refused(tmp_path):
    """A release is built from the clones this run fetched, and nothing else."""
    for offending in ("/etc/Dockerfile", "../beside-the-clone/Dockerfile"):
        manifest = _manifest(
            tmp_path,
            f"""
schema: 2
version: 0.0.0-outside
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
  - name: the-publisher
    dockerfile: {offending}
    role: publisher
""",
            name=f"manifest-{abs(hash(offending))}.yaml",
        )

        result = _plan(manifest)

        assert result.returncode != 0, offending
        assert "the-publisher" in result.stderr


@needs_docker
def test_a_release_with_no_coordinator_is_refused(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-headless
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-publisher
    dockerfile: src/somewhere/Dockerfile
    role: publisher
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "no image in the manifest has role coordinator" in result.stderr


@needs_docker
def test_image_name_and_the_coordinator_entry_must_agree(tmp_path):
    """``ops/forge-prod-recreate.sh`` reads ``image_name`` to name the release."""
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-disagree
image_name: something-else
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "image_name is 'something-else'" in result.stderr


@needs_docker
def test_a_floating_tag_is_refused_for_any_image(tmp_path):
    result = _plan(_two_image_manifest(tmp_path, version="latest"))

    assert result.returncode != 0
    assert "will not produce a" in result.stderr
    assert ":latest" in result.stderr


@needs_docker
def test_a_key_from_the_other_list_is_reported_by_name(tmp_path):
    """An images entry cannot quietly carry a ``commit:`` and look pinned."""
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-mixed
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
    commit: {_FORGE_COMMIT}
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "commit" in result.stderr
    assert "under images:" in result.stderr


@needs_docker
def test_a_list_this_reader_does_not_know_is_reported_by_name(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-thirdlist
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
seats:
  - name: something
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "seats:" in result.stderr


# ---------------------------------------------------------------------------
# Schema 1 — a manifest cut before the publisher's image existed
# ---------------------------------------------------------------------------


@needs_docker
def test_a_schema_1_manifest_still_plans_its_one_image(tmp_path):
    """The old shape is read as one image: the coordinator, from the root Dockerfile."""
    manifest = _manifest(
        tmp_path,
        f"""
schema: 1
version: 0.0.0-old
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
""",
    )

    result = _plan(manifest)

    assert result.returncode == 0, result.stderr
    assert "would build 1 image(s)" in result.stdout
    assert "the-coordinator [coordinator] from forge/Dockerfile" in result.stdout
    assert "proved by : forge/scripts/verify-forge-oracles.sh" in result.stdout


@needs_docker
def test_a_schema_1_manifest_that_also_lists_images_is_refused(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 1
version: 0.0.0-confused
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "schema 1 and also carries an 'images:' list" in result.stderr


@needs_docker
def test_an_unknown_schema_is_refused_and_says_which_it_reads(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 99
version: 0.0.0-future
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "schema '99'" in result.stderr


# ---------------------------------------------------------------------------
# The repository's own release manifest
# ---------------------------------------------------------------------------


@needs_docker
def test_the_shipped_manifest_plans_the_coordinator_and_the_publisher(tmp_path):
    """Read from a folder holding only a copy of it, as a clean machine would."""
    elsewhere = tmp_path / "a-folder-that-is-not-a-checkout"
    elsewhere.mkdir()
    copy = elsewhere / "manifest.yaml"
    copy.write_text(SHIPPED_MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")

    # ``--allow-existing-tag`` because this test is about WHAT the shipped
    # manifest would build, and a machine that has already built this release
    # (or a release sharing its commit tag) would otherwise be refused at the
    # door for a reason that has nothing to do with the question being asked.
    # The refusal itself has its own test above.
    result = _plan(copy, "--allow-existing-tag")

    assert result.returncode == 0, result.stderr
    assert "would build 2 image(s)" in result.stdout
    assert "forge [coordinator] from forge/Dockerfile" in result.stdout
    assert (
        "forge-publisher [publisher] from forge/src/forge/publisher/Dockerfile"
        in result.stdout
    )
    assert "proved by : forge/scripts/verify-forge-oracles.sh" in result.stdout
    assert "proved by : forge/scripts/verify-publisher-image.sh" in result.stdout


def test_the_shipped_manifest_names_files_this_repository_really_has():
    """Every dockerfile and proof it names is in the tree it is cut from."""
    named: list[str] = []
    for line in SHIPPED_MANIFEST.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip().lstrip("- ").strip()
        for key in ("dockerfile:", "proof:"):
            if stripped.startswith(key):
                named.append(stripped[len(key):].strip())

    assert named, "the shipped manifest names no dockerfile at all"
    for path in named:
        assert (REPO_ROOT / path).is_file(), f"the manifest names {path}, which is not in this tree"
