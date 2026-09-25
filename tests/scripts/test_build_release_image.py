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


def _pins_of(text: str) -> dict[str, str]:
    """repository name -> pinned commit, from the manifest's own lines."""
    pins: dict[str, str] = {}
    name = ""
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("- name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("commit:") and name:
            pins[name] = stripped.split(":", 1)[1].strip()
    return pins


@needs_docker
def test_the_shipped_manifest_plans_every_image_of_the_release(tmp_path):
    """Read from a folder holding only a copy of it, as a clean machine would.

    FIVE, since 25 September 2026: the coordinator, the publisher, the memory
    service, the memory relay and jarvis. The last three are built from another
    repository's clone at that repository's pin, which is what a per-image
    context is for — before it, a service whose code lives in another
    repository could not be a release image at all.

    The jarvis image is ONE image for TWO services — the Slack front door and
    the bus gateway — so it is one entry here, with the role of the service its
    own CMD is. The estate starts the other with a different command, and the
    image's proof script holds both of them to what the image can run.
    """
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
    assert "would build 5 image(s)" in result.stdout
    assert "forge [coordinator] from forge/Dockerfile" in result.stdout
    assert (
        "forge-publisher [publisher] from forge/src/forge/publisher/Dockerfile"
        in result.stdout
    )
    assert (
        "fleet-memory-mcp [memory] from fleet-memory/deploy/mcp/Dockerfile"
        in result.stdout
    )
    assert (
        "fleet-memory-relay [memory-relay] from fleet-memory/deploy/relay/Dockerfile"
        in result.stdout
    )
    assert "jarvis [front-door] from jarvis/deploy/Dockerfile" in result.stdout
    assert "proved by : forge/scripts/verify-forge-oracles.sh" in result.stdout
    assert "proved by : forge/scripts/verify-publisher-image.sh" in result.stdout
    assert "proved by : forge/scripts/verify-fleet-memory-image.sh" in result.stdout
    assert "proved by : forge/scripts/verify-jarvis-image.sh" in result.stdout


@needs_docker
def test_an_image_is_tagged_by_the_commit_of_the_repository_it_came_from(tmp_path):
    """The memory images carry the MEMORY repository's commit, not Forge's.

    A commit tag says "this is what that repository was at". Tagging an image
    built from another repository's clone with the release root's commit would
    make that sentence untrue for every image but one, which is worse than no
    commit tag at all. The release VERSION tag is what says they were built
    together.
    """
    elsewhere = tmp_path / "somewhere"
    elsewhere.mkdir()
    copy = elsewhere / "manifest.yaml"
    text = SHIPPED_MANIFEST.read_text(encoding="utf-8")
    copy.write_text(text, encoding="utf-8")

    pins = _pins_of(text)
    result = _plan(copy, "--allow-existing-tag")
    assert result.returncode == 0, result.stderr

    assert f"forge:{pins['forge']}" in result.stdout
    assert f"fleet-memory-mcp:{pins['fleet-memory']}" in result.stdout
    assert f"fleet-memory-relay:{pins['fleet-memory']}" in result.stdout
    assert f"fleet-memory-mcp:{pins['forge']}" not in result.stdout
    assert f"jarvis:{pins['jarvis']}" in result.stdout
    assert f"jarvis:{pins['forge']}" not in result.stdout


@needs_docker
def test_an_image_whose_context_is_not_a_pinned_repository_is_refused(tmp_path):
    """A context is one of the repositories the release pins, or nothing."""
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-test
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
  - name: from-nowhere
    dockerfile: Dockerfile
    role: stray
    context: a-repository-nobody-pinned
""",
    )
    result = _plan(manifest)

    assert result.returncode != 0
    assert "a-repository-nobody-pinned" in result.stderr
    assert "names no repository of that name" in result.stderr


@needs_docker
def test_an_image_with_a_context_is_planned_from_that_repositorys_clone(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-context
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
  - name: the-other-one
    dockerfile: deploy/Dockerfile
    role: elsewhere
    context: other
""",
    )
    result = _plan(manifest)

    assert result.returncode == 0, result.stderr
    assert "the-other-one [elsewhere] from other/deploy/Dockerfile" in result.stdout
    assert f"would tag : the-other-one:{_OTHER_COMMIT}" in result.stdout
    assert f"would tag : the-coordinator:{_FORGE_COMMIT}" in result.stdout


def test_the_shipped_manifest_names_files_this_repository_really_has():
    """Every dockerfile in THIS repository's own context, and every proof.

    An image built from another repository's clone names a file this tree does
    not have, and it must not be looked for here: the release build checks it
    against the fetched clone at the pin, which is the only honest place. A
    proof, though, is always this repository's — the proofs belong to the
    release, and asking another repository to carry the factory's proof script
    would put the factory into it.
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in SHIPPED_MANIFEST.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            current = {}
            entries.append(current)
            stripped = stripped[2:].strip()
        if current is None or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        current[key.strip()] = value.strip()

    named: list[str] = []
    for entry in entries:
        if "proof" in entry:
            named.append(entry["proof"])
        if "dockerfile" in entry and entry.get("context", "forge") == "forge":
            named.append(entry["dockerfile"])

    assert named, "the shipped manifest names no dockerfile at all"
    for path in named:
        assert (REPO_ROOT / path).is_file(), f"the manifest names {path}, which is not in this tree"


# ---------------------------------------------------------------------------
# An image's STANDARD labels name its own repository
# ---------------------------------------------------------------------------


def test_the_standard_revision_and_source_labels_are_set_per_image():
    """org.opencontainers.image.revision / .source, per image, not per release.

    WHY (25 September 2026, the review of stage 4b). These two labels mean
    "the commit this image was built from" and "the repository it came from",
    and they are what every ordinary tool reads. The script set both ONCE,
    from the build-context root, which was right while every image came from
    this repository and became wrong the moment an image could be built from
    another repository's clone: fleet-memory-mcp reported Forge's commit and
    Forge's address, while its own tag and its com.guardkit.* labels said
    fleet-memory's. Two answers to one question, and the widely-read one was
    the wrong one.

    This reads the script rather than building, because a build takes twenty
    minutes and a network; the real images are checked in the release proof.
    """
    script = SCRIPT.read_text(encoding="utf-8")

    assert "--label \"org.opencontainers.image.revision=${icommit}\"" in script, (
        "the release script no longer labels each image with the commit of "
        "the repository THAT image was built from"
    )
    assert "--label \"org.opencontainers.image.source=${iurl}\"" in script, (
        "the release script no longer labels each image with the address of "
        "the repository THAT image was built from"
    )
    assert "org.opencontainers.image.revision=${ROOT_COMMIT}" not in script, (
        "the release script is back to giving every image the build-context "
        "root's commit as its standard revision label"
    )
    assert "org.opencontainers.image.source=${ROOT_URL}" not in script, (
        "the release script is back to giving every image the build-context "
        "root's address as its standard source label"
    )


def test_the_release_wide_labels_are_still_release_wide():
    """The version, the manifest hash and the base digest are facts about the
    RELEASE and are the same on every image of it. Only the two that name a
    repository moved."""
    script = SCRIPT.read_text(encoding="utf-8")
    for label in (
        'LABEL_ARGS+=(--label "com.guardkit.release.version=${VERSION}")',
        'LABEL_ARGS+=(--label "com.guardkit.release.manifest.sha256=${MANIFEST_SHA}")',
        'LABEL_ARGS+=(--label "com.guardkit.release.base.digest=${BASE_DIGEST}")',
        'LABEL_ARGS+=(--label "org.opencontainers.image.version=${VERSION}")',
    ):
        assert label in script, f"the release no longer stamps every image with {label}"
