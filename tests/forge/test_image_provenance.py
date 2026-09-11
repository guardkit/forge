"""An image cannot ship code other than the tree it was built from.

WHY THIS FILE EXISTS. On 2026-09-11, during the go-live of forge
``a24a825`` + ``2ad935f``, ``scripts/build-image.sh`` was run from a clean
checkout. The build log showed ``COPY src ./src`` as executed rather than
cached, a fresh forge wheel, and the runtime stage's
``COPY --from=builder /opt/venv /opt/venv`` also as executed rather than
cached. ``scripts/verify-forge-oracles.sh`` then printed "forge oracle
verification PASSED". The image nevertheless carried the PREVIOUS commit's
code: the installed ``forge/subagents/autobuild_runner.py`` was 4,907 lines
— exactly commit ``5242da9`` — against 5,061 lines in the tree it was built
from, and neither new function was in it. Step by step: a throwaway
``COPY src ./src`` build of the same context gave 5,061 (the build context
was right); ``--target builder`` gave 5,061 in both ``/build/src`` and the
builder's own venv (the builder stage was right); a rebuild with the
builder's cache disabled still produced 4,907 in the final image; a rebuild
with the RUNTIME stage's cache disabled produced 5,061. The stale content
entered at the runtime stage's copy of the virtual environment, and the
build log called that step executed on both occasions.

A passing verification had proved the oracles resolved and had proved
nothing whatever about the code.

WHAT THESE TESTS PIN, and why each one is here.

* The build script computes the commit and whether the working tree is
  dirty, and passes BOTH into the build. Without that the image cannot say
  where it came from.
* The Dockerfile declares and consumes the commit in the runtime stage
  BEFORE it copies the virtual environment out of the builder. This is the
  whole of the cache guard: every runtime layer from that point down
  carries the commit in its cache key, so a layer built from a different
  commit cannot be reused. It is an input rather than a cache-disabling
  flag on purpose — a flag makes every build slow and can be left off,
  while an input that changes with the source keeps the cache working for
  repeat builds of the SAME commit and cannot be forgotten. If a future
  edit moves these lines below the COPY, the guard silently stops guarding,
  so the ORDER is asserted rather than mere presence.
* The comparison itself — identical trees pass; one changed byte fails and
  names the file; a missing file fails; an extra file fails; ``__pycache__``
  and ``.dist-info`` are ignored. These run the real script over real
  temporary directories, not a reimplementation of it.
* The canonical ``docker buildx build`` line still contains, byte for byte,
  the Contract A string that ``tests/dockerfile/test_install_layer.py``
  literal-matches. The provenance arguments are appended AFTER the context
  ``.`` for exactly that reason, and this test reads the constant out of
  that other test file rather than restating it, so the two cannot drift.
* The deploy runbook carries the new gate beside the old one.

No docker is required by anything in this file, and no live service is
touched: the two build-time behaviours are read out of the files that
implement them, and the comparison is driven over temporary directories.
"""

from __future__ import annotations

import importlib.util
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-image.sh"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify-forge-oracles.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DEPLOY_RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK-forge-production-deploy.md"
LITERAL_MATCH_TEST = REPO_ROOT / "tests" / "dockerfile" / "test_install_layer.py"

# The copy of the virtual environment the 2026-09-11 incident came through.
VENV_COPY_LINE = "COPY --from=builder /opt/venv /opt/venv"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _buildx_line(script_text: str) -> str:
    """Return the script's real ``docker buildx build`` line.

    The script's comment block discusses the invocation in prose, so only
    an active (non-comment) line carrying the image tag counts — the same
    rule ``tests/bdd/test_forge_production_image.py`` uses.
    """
    for line in script_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "docker buildx build" in stripped and "-t forge:" in stripped:
            return stripped
    pytest.fail(
        f"{BUILD_SCRIPT} has no active 'docker buildx build ... -t forge:' line"
    )


def _build_arg(tokens: list[str], name: str) -> str:
    """Return the value passed for ``--build-arg <name>=<value>``."""
    for index, token in enumerate(tokens):
        if token != "--build-arg":
            continue
        if index + 1 >= len(tokens):
            pytest.fail(f"{BUILD_SCRIPT}: --build-arg with no value after it")
        argument = tokens[index + 1]
        key, _, value = argument.partition("=")
        if key == name:
            return value
    pytest.fail(
        f"{BUILD_SCRIPT} does not pass --build-arg {name}=... — an image built "
        "by this script could not say which tree it came from"
    )


def _run_verify(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the verification script in one of its no-docker modes."""
    return subprocess.run(
        ["bash", str(VERIFY_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _manifest(directory: Path, out: Path) -> None:
    """Write the script's own manifest of ``directory`` to ``out``."""
    result = _run_verify("--manifest-dir", str(directory))
    assert result.returncode == 0, (
        f"--manifest-dir failed on {directory}:\n{result.stdout}\n{result.stderr}"
    )
    out.write_text(result.stdout, encoding="utf-8")


def _package(root: Path, files: dict[str, str]) -> Path:
    """Create a small package tree on disk and return its directory."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


BASE_PACKAGE = {
    "__init__.py": "VERSION = '1'\n",
    "cli/main.py": "def main() -> None:\n    return None\n",
    "subagents/autobuild_runner.py": "RUNNER = 'real'\n",
}


# ---------------------------------------------------------------------------
# (a) the build script computes the provenance and passes it in
# ---------------------------------------------------------------------------


def test_build_script_computes_the_commit_and_whether_the_tree_is_dirty() -> None:
    """The two facts are read from git, not guessed or hard-coded."""
    text = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'FORGE_GIT_SHA="$(git -C "${FORGE_DIR}" rev-parse HEAD' in text, (
        "the build script must read the commit of the tree it is building with "
        "'git rev-parse HEAD'"
    )
    assert 'git -C "${FORGE_DIR}" status --porcelain' in text, (
        "the build script must decide dirtiness with 'git status --porcelain'"
    )
    assert "FORGE_GIT_DIRTY=true" in text and "FORGE_GIT_DIRTY=false" in text, (
        "the build script must record dirtiness as a plain true/false"
    )
    assert "PROVENANCE:" in text, (
        "the build script must print one plain line saying which commit it is "
        "building and whether the tree is dirty"
    )


def test_build_script_passes_both_build_arguments_to_the_build() -> None:
    """The invocation's own argument list carries the two facts.

    Asserted against the parsed argument list rather than by grepping the
    file, and no docker runs here.
    """
    tokens = shlex.split(_buildx_line(BUILD_SCRIPT.read_text(encoding="utf-8")))

    assert tokens[:3] == ["docker", "buildx", "build"]
    assert _build_arg(tokens, "FORGE_GIT_SHA") == "${FORGE_GIT_SHA}"
    assert _build_arg(tokens, "FORGE_GIT_DIRTY") == "${FORGE_GIT_DIRTY}"


def test_a_dirty_working_tree_is_recorded_and_does_not_refuse_the_build() -> None:
    """Dirty is a fact to report, never a reason to block the owner.

    The estate builds from working trees. The only acceptable response to a
    dirty tree is to say so, so nothing in the provenance block may exit.
    """
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    start = text.index("FORGE_GIT_DIRTY=true")
    end = text.index("docker buildx build", start)
    dirty_block = text[start:end]

    assert "exit 1" not in dirty_block, (
        "a dirty working tree must not stop the build — it is recorded, printed "
        "and built from"
    )
    assert "uncommitted changes" in dirty_block, (
        "a build from a dirty tree must say so in plain words"
    )


# ---------------------------------------------------------------------------
# (b) the cache cannot cross commits
# ---------------------------------------------------------------------------


def _runtime_instructions() -> list[str]:
    """The runtime stage's real instructions, comments and blanks removed.

    Comments are dropped deliberately: the Dockerfile's own commentary
    quotes the ``COPY --from=builder`` line while explaining the incident,
    and an order assertion that matched prose would be meaningless.
    """
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("FROM ") and line.rstrip().endswith("AS runtime"):
            runtime_start = index
            break
    else:  # pragma: no cover - the Dockerfile always has a runtime stage
        pytest.fail("the Dockerfile has no 'FROM ... AS runtime' stage")

    return [
        line
        for line in lines[runtime_start:]
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _index_of(instructions: list[str], prefix: str, what: str) -> int:
    for index, line in enumerate(instructions):
        if line.startswith(prefix):
            return index
    pytest.fail(f"the runtime stage has no {what} (looked for a line starting {prefix!r})")


def test_runtime_stage_consumes_the_commit_before_it_copies_the_venv() -> None:
    """The whole of the cache guard, pinned as an ORDER.

    The commit must be declared AND consumed inside the runtime stage and
    ahead of the copy of the virtual environment, because that is what puts
    it into the cache key of every layer from there down. Presence alone
    would pass while the guard did nothing.
    """
    instructions = _runtime_instructions()

    arg_declaration = _index_of(instructions, "ARG FORGE_GIT_SHA", "commit argument")
    env_consumption = _index_of(
        instructions, "ENV FORGE_GIT_SHA=${FORGE_GIT_SHA}", "commit environment variable"
    )
    stamp_write = _index_of(instructions, "RUN test -n \"${FORGE_GIT_SHA}\"", "stamp step")
    venv_copy = _index_of(instructions, VENV_COPY_LINE, "copy of the virtual environment")

    assert arg_declaration < venv_copy, (
        "ARG FORGE_GIT_SHA must be declared in the runtime stage before "
        f"'{VENV_COPY_LINE}'"
    )
    assert arg_declaration < env_consumption < venv_copy, (
        "the runtime stage must CONSUME the commit before it copies the "
        "virtual environment — a declared-but-unused ARG changes no cache key"
    )
    assert env_consumption < stamp_write < venv_copy, (
        "the stamp file must be written before the virtual environment is "
        "copied, so the copy itself sits behind the commit in the cache chain"
    )


def test_runtime_stage_declares_the_dirty_flag_too() -> None:
    """An image built from uncommitted work says so from the inside."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    runtime_stage = text.index("AS runtime")

    assert "ARG FORGE_GIT_DIRTY" in text[runtime_stage:]
    assert "ENV FORGE_GIT_SHA=${FORGE_GIT_SHA}" in text[runtime_stage:]
    assert "FORGE_GIT_DIRTY=${FORGE_GIT_DIRTY}" in text[runtime_stage:]


def test_the_guard_is_an_input_not_a_cache_disabling_flag() -> None:
    """No ``--no-cache`` anywhere: it would slow every build and can be left off."""
    script_text = BUILD_SCRIPT.read_text(encoding="utf-8")
    line = _buildx_line(script_text)

    assert "--no-cache" not in line, (
        "the cache guard is the commit argument, not a flag: a flag makes every "
        "build slow and can be forgotten"
    )


# ---------------------------------------------------------------------------
# (c) the comparison itself, over real directories
# ---------------------------------------------------------------------------


def test_identical_trees_pass_and_the_script_says_what_it_proved(
    tmp_path: Path,
) -> None:
    tree = _package(tmp_path / "tree", BASE_PACKAGE)
    image = _package(tmp_path / "image", BASE_PACKAGE)
    _manifest(tree, tmp_path / "tree.tsv")
    _manifest(image, tmp_path / "image.tsv")

    result = _run_verify("--compare", str(tmp_path / "tree.tsv"), str(tmp_path / "image.tsv"))

    assert result.returncode == 0, result.stderr
    assert "3 Python files compared" in result.stdout, (
        "the script must say in one plain sentence how many files it compared "
        f"and that they match; it said: {result.stdout!r}"
    )


def test_one_changed_byte_fails_and_names_the_file(tmp_path: Path) -> None:
    tree = _package(tmp_path / "tree", BASE_PACKAGE)
    changed = dict(BASE_PACKAGE)
    changed["subagents/autobuild_runner.py"] = "RUNNER = 'stale'\n"
    image = _package(tmp_path / "image", changed)
    _manifest(tree, tmp_path / "tree.tsv")
    _manifest(image, tmp_path / "image.tsv")

    result = _run_verify("--compare", str(tmp_path / "tree.tsv"), str(tmp_path / "image.tsv"))

    assert result.returncode != 0
    assert "subagents/autobuild_runner.py" in result.stderr, (
        "the failure must name the file that differs — this is exactly the file "
        f"the live incident shipped stale; it said: {result.stderr!r}"
    )


def test_a_file_missing_from_the_image_fails_and_names_it(tmp_path: Path) -> None:
    tree = _package(tmp_path / "tree", BASE_PACKAGE)
    short = {k: v for k, v in BASE_PACKAGE.items() if k != "cli/main.py"}
    image = _package(tmp_path / "image", short)
    _manifest(tree, tmp_path / "tree.tsv")
    _manifest(image, tmp_path / "image.tsv")

    result = _run_verify("--compare", str(tmp_path / "tree.tsv"), str(tmp_path / "image.tsv"))

    assert result.returncode != 0
    assert "cli/main.py" in result.stderr
    assert "not in the image" in result.stderr


def test_a_file_the_tree_does_not_have_fails_and_names_it(tmp_path: Path) -> None:
    tree = _package(tmp_path / "tree", BASE_PACKAGE)
    extra = dict(BASE_PACKAGE)
    extra["cli/leftover.py"] = "OLD = True\n"
    image = _package(tmp_path / "image", extra)
    _manifest(tree, tmp_path / "tree.tsv")
    _manifest(image, tmp_path / "image.tsv")

    result = _run_verify("--compare", str(tmp_path / "tree.tsv"), str(tmp_path / "image.tsv"))

    assert result.returncode != 0
    assert "cli/leftover.py" in result.stderr
    assert "not in the tree" in result.stderr


def test_pycache_and_dist_info_are_ignored(tmp_path: Path) -> None:
    """Compiled caches and installation records are not source.

    The image carries both; the tree carries neither. Counting them would
    fail every honest build.
    """
    tree = _package(tmp_path / "tree", BASE_PACKAGE)
    image = _package(tmp_path / "image", BASE_PACKAGE)
    _package(
        image / "__pycache__",
        {"main.cpython-314.pyc": "compiled\n", "stray.py": "SHOULD_BE_IGNORED = 1\n"},
    )
    _package(
        image / "forge-0.1.0.dist-info",
        {"RECORD": "forge/__init__.py\n", "installer.py": "IGNORED = 1\n"},
    )
    (image / "cli" / "main.pyc").write_text("compiled\n", encoding="utf-8")

    _manifest(tree, tmp_path / "tree.tsv")
    _manifest(image, tmp_path / "image.tsv")

    result = _run_verify("--compare", str(tmp_path / "tree.tsv"), str(tmp_path / "image.tsv"))

    assert result.returncode == 0, (
        "__pycache__, .pyc files and .dist-info directories must be ignored; "
        f"the comparison said: {result.stderr!r}"
    )


def test_the_comparison_refuses_a_manifest_it_cannot_read(tmp_path: Path) -> None:
    """Unknown is not a pass."""
    result = _run_verify("--compare", str(tmp_path / "nope.tsv"), str(tmp_path / "also-nope.tsv"))

    assert result.returncode != 0


def test_the_verification_fails_an_image_with_no_stamp() -> None:
    """An older image predates the guard and is refused, never waved through."""
    text = VERIFY_SCRIPT.read_text(encoding="utf-8")

    assert "/etc/forge-image-provenance" in text
    assert "built before this guard existed" in text, (
        "an image with no provenance stamp must fail with a plain sentence "
        "saying it predates the guard"
    )
    assert "Unknown is not a pass." in text, (
        "git missing, docker missing, no installed package: each must fail "
        "plainly rather than pass quietly"
    )


def test_the_provenance_check_runs_before_the_oracles() -> None:
    """The order is the point: an image is refused before anything else is claimed."""
    text = VERIFY_SCRIPT.read_text(encoding="utf-8")

    provenance_call = text.index('check_image_provenance "${IMAGE}"')
    first_oracle = text.index("Verifying forge target-terminal oracles")

    assert provenance_call < first_oracle


# ---------------------------------------------------------------------------
# The Contract A line, and the runbook
# ---------------------------------------------------------------------------


def _contract_a_invocation() -> str:
    """Read the canonical string out of the Dockerfile-side literal-match test.

    Read rather than restated, so this test and that one cannot drift: if
    the canonical line changes in one place, this fails in the other.
    """
    spec = importlib.util.spec_from_file_location(
        "_literal_match_test", LITERAL_MATCH_TEST
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CONTRACT_A_INVOCATION


def test_the_canonical_buildx_line_still_holds_byte_for_byte() -> None:
    """The provenance arguments are APPENDED, so the contract line is untouched.

    Four consumers literal-match that string — the runbook, the workflow and
    two test files — and docker accepts flags after the positional context,
    so appending is the only placement under which nothing already there
    moved.
    """
    line = _buildx_line(BUILD_SCRIPT.read_text(encoding="utf-8"))
    canonical = _contract_a_invocation()

    assert canonical in line, (
        "the canonical Contract A invocation must still appear byte for byte in "
        f"the build script's buildx line.\nExpected: {canonical!r}\nGot: {line!r}"
    )
    assert line.index(canonical) == 0, (
        "the canonical line must remain the START of the invocation, with the "
        "provenance arguments appended after it"
    )


def test_the_deploy_runbook_carries_the_new_gate_beside_the_old_one() -> None:
    """G3 stays — an operator's eyes on their own change — and the machine's own check is named."""
    text = DEPLOY_RUNBOOK.read_text(encoding="utf-8")

    assert "GATE G3" in text, (
        "G3 stays: it is the operator looking for the specific change they came "
        "to deploy, which is not what the machine's comparison does"
    )
    assert "scripts/verify-forge-oracles.sh" in text
    assert "2026-09-11" in text, (
        "the incident must be recorded so the next person does not re-derive it"
    )
