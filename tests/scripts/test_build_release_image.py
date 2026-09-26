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

import json
import os
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

    # NO OVERRIDE FLAG. A plan is read-only, so it no longer refuses on a tag
    # that already exists on this machine — it prints that the tag is there and
    # that a real build would refuse it. Until 25 September 2026 this test had
    # to pass ``--allow-existing-tag`` just to read a plan, which is exactly
    # how people learn to reach for an override flag out of habit. The real
    # build's refusal is a separate test, below.
    result = _plan(copy)

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


# ---------------------------------------------------------------------------
# THE SWEEP — nothing of the building machine goes out in a release image
#
# Written 25 September 2026. Two real hits reached a PUBLIC image before this
# existed: a developer's compiled files carrying the absolute path of the
# machine that compiled them, and a host name a package hard-coded as a
# default. Both were found by sweeping an image BY HAND, because every image's
# own proof script reads the image's CONFIGURATION and its labels and both hits
# were in the FILESYSTEM.
#
# A sweep of a real image needs a real build, which needs twenty minutes and a
# network, so what is held here is the SHAPE — where the words come from, what
# is searched, what a hit does, and what an unset variable says. The sweep
# itself is driven against a real image with a planted word in the stage's
# evidence.
# ---------------------------------------------------------------------------


def _sweep_exception_manifest(tmp_path: Path, **overrides: str) -> Path:
    entry = {
        "image": "the-coordinator",
        "path": "somewhere/inside/the-image/a-file.md",
        "reason": "the detector's own patterns",
    }
    entry.update(overrides)
    lines = "\n".join(
        f"    {key}: {value}" if key != "image" else f"  - image: {value}"
        for key, value in entry.items()
        if value
    )
    return _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-sweepexception
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
sweep_exceptions:
{lines}
""",
    )


def test_the_words_swept_for_come_from_the_machine_and_not_from_this_repository():
    """A tracked list of this machine's names would BE the defect.

    The rule (Rich, on containerisation): a machine's name as a default value
    is the worst form of it — which means the list of words a sweep looks for
    cannot itself be a tracked list of real machine names in a public
    repository. It comes from the environment, at build time.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'SWEEP_TERMS="${RELEASE_SWEEP_TERMS:-}"' in script, (
        "the release script no longer takes its sweep words from the machine's "
        "own environment"
    )


def test_the_sweep_reads_the_filesystem_and_not_only_the_configuration():
    """The whole point: both real hits were in the filesystem."""
    script = SCRIPT.read_text(encoding="utf-8")
    for needle in ("docker create", "docker export", "docker image history"):
        assert needle in script, (
            f"the release script no longer runs `{needle}`, so it is back to "
            "asking the small question each proof script already asks"
        )
    assert "/bin/grep -r -l -F" in script, "the sweep no longer searches the unpacked filesystem"


def test_a_hit_refuses_the_release_and_names_the_image_and_the_file():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "carries names belonging to the machine that built it" in script
    assert 'echo "       in ${rpath}" >&2' in script, "a refusal no longer names the file"
    assert "${rposition} of the ${SWEEP_TERM_COUNT}" in script, (
        "a refusal no longer says WHICH of the words matched, so a hit cannot "
        "be diagnosed at all"
    )


def test_a_refusal_says_which_word_by_its_position_and_never_the_word():
    """CODEX'S REVIEW, 26 September 2026. The allowed-exception line was
    redacted on 25 September and the REFUSAL line still printed the matched
    word in full — so a build log, which is kept and pasted, carried one of
    this machine's own names. A refusal has to be diagnosable without
    publishing the thing it is refusing.

    The path is redacted with it, because this sweep counts a file whose NAME
    holds one of the words as a hit, and the path is what gets printed.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert "${rterm}" not in script, (
        "the refusal path still has the matched word itself in it somewhere"
    )
    assert "term_position()" in script, "nothing turns a matched word into its position"
    assert "redact_terms()" in script, "nothing takes a matched word out of a path"
    refusals_written = [
        line
        for line in script.splitlines()
        if '>> "${refusals}"' in line and "printf" in line
    ]
    assert refusals_written, "nothing writes a refusal any more"
    for line in refusals_written:
        assert "term_position" in line, (
            "a refusal is written with the word itself rather than its "
            f"position: {line.strip()}"
        )
    sweep_tree = script.split("sweep_tree() {", 1)[1].split("\n}", 1)[0]
    assert 'safepath="$(redact_terms' in sweep_tree, (
        "the path a refusal prints is not redacted, so a file NAMED after one "
        "of the words publishes it"
    )


def test_an_image_that_could_not_be_swept_is_not_called_clean():
    """Not swept is not a pass — for an export that fails and for one that
    unpacks short of what was in it."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.count("Not swept is not a pass") >= 2
    assert "was never searched" in script


@needs_docker
def test_a_plan_says_plainly_when_no_sweep_would_run(tmp_path):
    manifest = _two_image_manifest(tmp_path, version="0.0.0-nosweep")
    environment = dict(os.environ)
    environment.pop("RELEASE_SWEEP_TERMS", None)

    result = subprocess.run(
        ["bash", str(SCRIPT), str(manifest), "--plan-only"],
        cwd=str(manifest.parent),
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "RELEASE_SWEEP_TERMS is not set, so a real build would sweep nothing" in result.stdout


@needs_docker
def test_a_plan_says_how_many_words_would_be_swept_for_without_printing_them(tmp_path):
    """The count, not the words. They are this machine's names, and a plan is
    something people paste into a page."""
    manifest = _two_image_manifest(tmp_path, version="0.0.0-withsweep")
    environment = dict(os.environ)
    environment["RELEASE_SWEEP_TERMS"] = "alpha-box someaccount /home/someaccount"

    result = subprocess.run(
        ["bash", str(SCRIPT), str(manifest), "--plan-only"],
        cwd=str(manifest.parent),
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "3 term(s) from RELEASE_SWEEP_TERMS" in result.stdout
    assert "alpha-box" not in result.stdout
    assert "someaccount" not in result.stdout


# ---------------------------------------------------------------------------
# Sweep exceptions are named by PATH, never by word
# ---------------------------------------------------------------------------


@needs_docker
def test_a_sweep_exception_is_read_and_the_plan_still_works(tmp_path):
    result = _plan(_sweep_exception_manifest(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "would build 1 image(s)" in result.stdout


@needs_docker
def test_a_sweep_exception_without_a_reason_is_refused(tmp_path):
    """An exception that cannot say why it exists is one nobody can review."""
    result = _plan(_sweep_exception_manifest(tmp_path, reason=""))

    assert result.returncode != 0
    assert "gives no reason" in result.stderr


@needs_docker
def test_a_sweep_exception_without_a_path_is_refused(tmp_path):
    """An exception is a named FILE. Allowing a WORD would put a machine's name
    back into the manifest, which is the thing being prevented."""
    result = _plan(_sweep_exception_manifest(tmp_path, path=""))

    assert result.returncode != 0
    assert "names no path" in result.stderr
    assert "never a word" in result.stderr


@needs_docker
def test_a_sweep_exception_for_an_image_the_release_does_not_build_is_refused(tmp_path):
    result = _plan(_sweep_exception_manifest(tmp_path, image="an-image-that-is-not-here"))

    assert result.returncode != 0
    assert "an-image-that-is-not-here" in result.stderr


@needs_docker
def test_an_unknown_key_under_sweep_exceptions_is_reported_by_name(tmp_path):
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: 0.0.0-sweepkey
image_name: the-coordinator
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: the-coordinator
    dockerfile: Dockerfile
    role: coordinator
sweep_exceptions:
  - image: the-coordinator
    word: some-machine-name
""",
    )

    result = _plan(manifest)

    assert result.returncode != 0
    assert "word" in result.stderr
    assert "under sweep_exceptions:" in result.stderr


# ---------------------------------------------------------------------------
# --plan-only is READ-ONLY, so it needs no override flag
# ---------------------------------------------------------------------------


@needs_docker
def test_a_plan_reports_an_existing_tag_rather_than_refusing(tmp_path):
    """Planning a release on the machine that built it should not need a flag.

    The image used is whichever one this machine already has — the question is
    about an EXISTING tag, so the test asks the daemon for one rather than
    building or pulling anything. The name is only read.
    """
    listing = subprocess.run(
        ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    existing = [
        line
        for line in listing.stdout.splitlines()
        if line and "<none>" not in line and ":" in line
    ]
    if not existing:
        pytest.skip("this machine has no tagged image, so there is no existing tag to plan over")

    name, _, tag = existing[0].rpartition(":")
    manifest = _manifest(
        tmp_path,
        f"""
schema: 2
version: {tag}
image_name: {name}
python_base_digest: {_BASE_DIGEST}
{_repositories()}
images:
  - name: {name}
    dockerfile: Dockerfile
    role: coordinator
""",
    )

    result = _plan(manifest)

    assert result.returncode == 0, result.stderr
    assert "ALREADY EXISTS" in result.stdout
    assert "a real build would refuse this" in result.stdout


def test_the_existing_tag_refusal_still_exists_for_a_real_build():
    """Moved, not removed: a release tag is still written once."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert "already exists on this machine. A release tag is written once" in script
    plan_branch = script.index('if [ "${PLAN_ONLY}" = "1" ]; then')
    refusal = script.index("already exists on this machine. A release tag is written once")
    assert refusal > plan_branch, (
        "the existing-tag refusal is back above the --plan-only branch, so a "
        "read-only plan needs --allow-existing-tag again"
    )


# ---------------------------------------------------------------------------
# THE SWEEP READS WHAT A PUSH SENDS, and not only what a container would see
#
# Added 25 September 2026, after a review measured the gap. `docker export` is
# the FLATTENED FINAL filesystem: a file a Dockerfile copies in and a later
# step deletes is gone from it and still in the image, because the layer that
# holds it is still one of the image's layers and is still what `docker save`
# and a registry push send. On the jarvis image — the image this sweep was
# written for — the export carried this estate's account name in 0 files while
# the image's own layers carried it in 2,194.
# ---------------------------------------------------------------------------


def test_the_sweep_reads_every_layer_a_push_would_send():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "docker save" in script, (
        "the sweep no longer reads what a push would send, so a file deleted "
        "by a later layer is invisible to it while still being in the image"
    )
    assert "sweep_layers" in script, "the release script has no layer sweep"
    assert "sweep_layers " in script.split("sweep_layers()", 1)[1], (
        "the layer sweep is defined and never called"
    )


def test_a_layer_that_could_not_be_unpacked_is_not_a_pass():
    """Not swept is not a pass — for the layers as well as for the export."""
    script = SCRIPT.read_text(encoding="utf-8")
    for sentence in (
        "could not be saved, so the layers a push would send could not be swept",
        "so part of what a push would send was never searched",
        "could not be unpacked, so its layers were never searched",
    ):
        assert sentence in script, (
            f"the layer sweep no longer refuses when it cannot read something: {sentence!r}"
        )


def test_every_layer_is_unpacked_on_its_own_so_one_cannot_hide_another():
    """Two layers can hold different files at the same path; unpacked over
    each other, the earlier one would be swept in the later one's clothes — or
    not swept at all."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert '${dir}/layers/${layers}' in script, (
        "the layers are no longer unpacked into a directory of their own"
    )
    assert "in layer ${i} of ${layers}" in script, (
        "a refusal no longer says which layer the file is in"
    )


def test_a_file_whose_name_carries_the_word_is_a_hit_too():
    """A machine's name in a path is the same defect as one in a line."""
    script = SCRIPT.read_text(encoding="utf-8")
    sweep_tree = script.split("sweep_tree() {", 1)[1].split("\n}", 1)[0]
    assert "find" in sweep_tree and 'case "${path#"${root}"}"' in sweep_tree, (
        "the sweep reads file contents only, so a file NAMED after this "
        "machine would pass"
    )


def test_a_refused_sweep_removes_the_tags_that_run_wrote():
    """Both tags are written at build time, before anything can be swept.

    Left behind, they make the next attempt at the same commit refuse with
    "the tag ... already exists", and the operator reaches for
    --allow-existing-tag — the habit the plan-only fix has just finished
    getting rid of.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert "remove_this_runs_tags" in script, "a refused sweep leaves its tags on the machine"
    refusal = script.index("carries names belonging to the machine that built it")
    # The call AFTER the refusal text: die() itself now removes this run's
    # tags on any failure once a tag is written, so an earlier call exists too.
    called = script.find("        remove_this_runs_tags\n", refusal)
    assert called > refusal, "the tags are not removed on the sweep's refusal"
    assert "docker rmi" in script, "nothing removes a tag"


def test_a_tag_that_was_there_before_the_run_is_left_alone():
    """Only with --allow-existing-tag can a tag pre-date the run, and that one
    is not this run's to remove."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert "PRE_EXISTING_TAGS" in script
    assert "was on this machine before this run started, so it has been left alone" in script


def test_the_estates_env_example_says_that_filling_it_in_does_not_arm_the_sweep():
    """RELEASE_SWEEP_TERMS lives in compose's env file, and the release build
    reads the environment of the shell that runs it and sources no file."""
    example = (REPO_ROOT / "deploy" / "estate" / ".env.example").read_text(encoding="utf-8")
    assert "DOES NOT BY ITSELF ARM" in example, (
        "the estate's .env.example still reads as though filling in "
        "RELEASE_SWEEP_TERMS there is what makes a release build sweep"
    )
    assert "export RELEASE_SWEEP_TERMS=" in example, (
        "the estate's .env.example does not say how to arm the sweep"
    )


# ---------------------------------------------------------------------------
# A WHOLE RUN, DRIVEN — the two things a source read cannot answer
# ---------------------------------------------------------------------------
#
# Added 26 September 2026, after Codex's review of release 2026.09.26-1 found
# two claims that were true of the source and false of the running script:
#
#   * "no sweep word is printed" — the allowed-exception line was redacted and
#     the REFUSAL line still printed the matched word in full;
#   * "a refused run takes its tags with it" — the removal was inside die(), and
#     an ordinary failing command under `set -e` is not a die(). A run that
#     built and swept a clean image set and then failed to write its receipt
#     left both tags on the machine.
#
# Both of those are about what the script DOES, so these drive the script's
# public entry to the end, with a fake engine on PATH and a local git fixture.
# The method is Codex's own reproducer, kept here so the repository's suite owns
# it: nothing reaches a Docker daemon, a network, or any real image or tag, and
# the swept word is explicitly synthetic — a real one belongs to a machine and
# would be the defect this sweep looks for if it were written down here.
#
# The fake engine understands only the handful of docker calls this script makes
# and refuses anything else by name, so a future call it does not model fails
# loudly rather than passing quietly.

_FAKE_ENGINE = r'''#!/usr/bin/env python3
import sys, os, json, tarfile, io
from pathlib import Path
args = sys.argv[1:]
statefile = Path(os.environ["FAKE_ENGINE_STATE"])
s = json.loads(statefile.read_text()) if statefile.exists() else {"tags": {}, "labels": {}}
def save(): statefile.write_text(json.dumps(s))
def argvalue(flag): return args[args.index(flag) + 1]
def tarbytes():
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        data = os.environ.get("FAKE_IMAGE_TEXT", "clean content").encode()
        m = tarfile.TarInfo(os.environ.get("FAKE_IMAGE_FILE", "probe.txt"))
        m.size = len(data); m.mode = 0o644
        t.addfile(m, io.BytesIO(data))
    return b.getvalue()
if args[:2] == ["buildx", "build"]:
    iid = "sha256:" + "a" * 64
    for i, a in enumerate(args[:-1]):
        if a == "-t": s["tags"][args[i + 1]] = iid
        if a == "--label":
            k, v = args[i + 1].split("=", 1); s["labels"][k] = v
    save(); Path(argvalue("--iidfile")).write_text(iid + "\n"); sys.exit(0)
if args[:2] == ["image", "inspect"]:
    fmt = argvalue("--format") if "--format" in args else None
    refs = [a for i, a in enumerate(args[2:], 2) if a != "--format" and (i == 0 or args[i - 1] != "--format")]
    ref = refs[0]
    if ref not in s["tags"] and not (ref == "sha256:" + "a" * 64 and s["labels"]): sys.exit(1)
    if not fmt: print("[]")
    elif ".RootFS.Layers" in fmt: print("1")
    elif ".RepoDigests" in fmt: print("[]")
    elif ".Config.Labels" in fmt: print(json.dumps(s["labels"]))
    elif ".Config" in fmt: print(json.dumps({"Labels": s["labels"]}))
    else: raise SystemExit("unhandled format " + fmt)
    sys.exit(0)
if args[:2] == ["image", "history"]: print("fixture image"); sys.exit(0)
if args[0] == "create": print("fixture-container"); sys.exit(0)
if args[0] == "export": sys.stdout.buffer.write(tarbytes()); sys.exit(0)
if args[0] == "save":
    with tarfile.open(argvalue("-o"), "w") as t:
        for name, data in [("layer/layer.tar", tarbytes()), ("manifest.json", b"[{}]"), ("config.json", b"{}")]:
            m = tarfile.TarInfo(name); m.size = len(data); m.mode = 0o644
            t.addfile(m, io.BytesIO(data))
    sys.exit(0)
if args[0] == "rm": sys.exit(0)
if args[0] == "rmi":
    for a in args[1:]: s["tags"].pop(a, None)
    save(); sys.exit(0)
raise SystemExit("this fake engine does not model: " + repr(args))
'''

#: Explicitly synthetic, and said twice because it matters: a sweep word is one
#: of a machine's own names, and a real one written down here would be the
#: defect the sweep exists to find.
_SYNTHETIC_TERM = "A-SYNTHETIC-SWEEP-WORD"


def _fake_engine_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A fake engine on PATH, a one-commit local repository, and a manifest that
    pins it. Returns the working directory, the manifest, and the environment
    every drive below runs in."""
    work = tmp_path / "release-fixture"
    binaries = work / "bin"
    binaries.mkdir(parents=True)
    engine = binaries / "docker"
    engine.write_text(_FAKE_ENGINE, encoding="utf-8")
    engine.chmod(0o755)

    source = work / "source"
    source.mkdir()
    (source / "Dockerfile").write_text(f"FROM python:review@{_BASE_DIGEST}\n", encoding="utf-8")
    environment = {
        "PATH": f"{binaries}:/usr/bin:/bin",
        "HOME": str(work / "home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "LANG": "C.UTF-8",
        "TMPDIR": str(work),
    }

    def git(*arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=source, env=environment, stderr=subprocess.STDOUT, text=True
        ).strip()

    git("init", "-b", "main")
    git("add", "Dockerfile")
    git(
        "-c", "user.name=Release Fixture",
        "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture",
    )
    commit = git("rev-parse", "HEAD")

    manifest = work / "manifest.yaml"
    manifest.write_text(
        f"""schema: 2
version: 0.0.0-drive
image_name: review-image
python_base_digest: {_BASE_DIGEST}
repositories:
  - name: source
    url: {source.as_uri()}
    branch: main
    commit: {commit}
    role: build-context-root
images:
  - name: review-image
    dockerfile: Dockerfile
    role: coordinator
""",
        encoding="utf-8",
    )
    return work, manifest, environment


def _drive(
    tmp_path: Path,
    *,
    case: str,
    receipt: Path | str,
    image_text: str = "clean content",
    image_file: str = "probe.txt",
    extra: tuple[str, ...] = (),
) -> tuple[int, str, list[str], Path]:
    """One whole run of the script's public entry. Returns its exit status, its
    whole output, the tags left on the fake engine afterwards, and the receipt
    path it was given."""
    work, manifest, environment = _fake_engine_fixture(tmp_path / case)
    state = work / "engine.json"
    done = subprocess.run(
        ["bash", str(SCRIPT), str(manifest), "--skip-proof", "--receipt", str(receipt), *extra],
        cwd=work,
        env={
            **environment,
            "FAKE_ENGINE_STATE": str(state),
            "FAKE_IMAGE_TEXT": image_text,
            "FAKE_IMAGE_FILE": image_file,
            "RELEASE_SWEEP_TERMS": _SYNTHETIC_TERM,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    tags = sorted(json.loads(state.read_text())["tags"]) if state.exists() else []
    return done.returncode, done.stdout + done.stderr, tags, Path(receipt)


class TestARunThatIsRefusedOrFails:
    def test_a_clean_run_keeps_its_tags_and_writes_its_receipt(self, tmp_path):
        """The control. Without it, a script that refused everything would pass
        every other test in this class."""
        receipt = tmp_path / "control" / "receipt.json"
        receipt.parent.mkdir(parents=True)
        status, output, tags, written = _drive(tmp_path, case="control", receipt=receipt)
        assert status == 0, output
        assert len(tags) == 2, f"a successful run left {tags}"
        assert json.loads(written.read_text())["sweep"]["terms_counted"] == 1, output

    def test_a_refusal_prints_no_sweep_word_when_the_word_is_in_a_file(self, tmp_path):
        status, output, tags, _ = _drive(
            tmp_path,
            case="word-in-contents",
            receipt=tmp_path / "word-in-contents.json",
            image_text=f"a line holding {_SYNTHETIC_TERM} in it",
        )
        assert status != 0, output
        assert _SYNTHETIC_TERM not in output, (
            "the refusal printed the matched word, which is one of this "
            "machine's own names and goes into a build log that is kept"
        )
        assert "carries names belonging to the machine that built it" in output
        assert "number 1 of the 1" in output, (
            "the refusal does not say which of the words matched, so nobody "
            "can diagnose it"
        )
        assert tags == [], f"a refused run left {tags}"

    def test_a_refusal_prints_no_sweep_word_when_the_word_is_in_a_path(self, tmp_path):
        """A file whose NAME holds the word is a hit too, and the path is what
        a refusal prints — so redacting only the word leaves the word in the
        output. This is the case the first fix missed."""
        status, output, tags, _ = _drive(
            tmp_path,
            case="word-in-path",
            receipt=tmp_path / "word-in-path.json",
            image_file=f"home/{_SYNTHETIC_TERM}/notes.txt",
        )
        assert status != 0, output
        assert _SYNTHETIC_TERM not in output, (
            "the refusal printed a path with the matched word still in it"
        )
        assert "<word 1 of 1>" in output, (
            "the redacted path does not say which word was taken out of it"
        )
        assert tags == [], f"a refused run left {tags}"

    def test_a_receipt_that_cannot_be_written_takes_this_runs_tags_with_it(self, tmp_path):
        """CODEX'S REPRODUCER, 26 September 2026. A clean build and a clean
        sweep, then a receipt destination whose parent directory does not
        exist: an ordinary failure under ``set -e``, which never reaches
        ``die()``. It left both tags behind."""
        status, output, tags, written = _drive(
            tmp_path,
            case="receipt-write-failure",
            receipt=tmp_path / "receipt-write-failure" / "no-such-directory" / "receipt.json",
        )
        assert status != 0, output
        assert not written.exists(), "a receipt appeared where its directory does not exist"
        assert tags == [], (
            "the run failed writing its receipt and left its tags on the "
            f"machine: {tags}. The next attempt at the same commit then meets "
            "'the tag already exists' and the operator reaches for "
            "--allow-existing-tag."
        )
        assert output.count("Nothing this run built is to be shipped") == 1, (
            "the tags were removed more than once, or not named as removed"
        )

    def test_a_plan_removes_nothing(self, tmp_path):
        """A plan writes no tag, so it has nothing to take back — and it must
        not touch a tag another run left."""
        status, output, tags, _ = _drive(
            tmp_path,
            case="plan",
            receipt=tmp_path / "plan.json",
            extra=("--plan-only",),
        )
        assert status == 0, output
        assert tags == [], output
        assert "docker rmi" not in output
        assert "Nothing this run built is to be shipped" not in output
