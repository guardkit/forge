"""A stand-in ``guardkit`` command for the sidecar's git-route tests.

Written to a temporary file and named through ``FORGE_GUARDKIT_PATH``, so the
sidecar resolves it exactly as it resolves the real command. It behaves like
guardkit's three verbs as far as the sidecar and the driver read them:

* ``qa normalize-stamps --feature <id> --repo <root> [--no-model]`` WRITES a
  ``scenarios:`` map into the plan YAML (the real normalizer's shape) and
  prints the JSON result on stdout with the real exit codes (0 all decided,
  3 partial, 2 refused / cannot run). What it does is scripted through the
  environment (the sidecar inherits the test's), so a test can ask for a
  refusal, a partial, a failure, an older guardkit with no such verb, or one
  with no ``--no-model`` option. ``FAKE_GUARDKIT_NO_MODEL_REFUSES=1`` makes a
  ``--no-model`` call refuse and a plain call write — the shape of a run
  whose rules refuse two titles and whose model fallback then decides them.
* ``feature validate <id> --json`` answers valid (exit 0) or, when asked,
  refuses (exit 1), and records whether the YAML it saw carried stamps.
* ``qa classify-scenarios --feature-file <path> --repo <root> --json`` prints
  the classify JSON (exit 0), with refused titles when asked, or the
  cannot-run object (exit 2).
* ``qa validate pass-bar <path>`` and ``qa validate gate-registry <path>``
  answer valid (exit 0) or, when asked through the environment, refuse (exit
  1) with guardkit's own shape of message on stderr.

Every call appends one JSON line (argv, cwd) to ``FAKE_GUARDKIT_LOG`` so a
test can prove what ran, in what order, with which flags.

Beside it lives a stand-in for the OTHER command the sidecar's checks run:
guardkit's gherkin normalizer, which is a MODULE (``python -m
installer.core.commands.lib.feature_spec_normalize <file>``), not a guardkit
subcommand. :func:`install_fake_normalizer` plants that module where both the
sidecar's own interpreter and the subprocess it starts will find it, so the
resolution the production code does is the resolution the test exercises.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

#: The two titles the fake refuses — the driver test's own fixture titles.
REFUSED_TITLES = (
    "The moon is made of a very particular kind of cheese that no rule family "
    "in the design has ever heard about at all",
    "Another undecidable one",
)

FAKE_GUARDKIT_SOURCE = r'''#!/usr/bin/env python3
"""The stand-in guardkit (see tests/forge/deploy_sidecar/_fake_guardkit.py)."""
import json
import os
import sys
from pathlib import Path

REFUSED = [
    "The moon is made of a very particular kind of cheese that no rule family "
    "in the design has ever heard about at all",
    "Another undecidable one",
]


def log(argv):
    path = os.environ.get("FAKE_GUARDKIT_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")


def option(argv, name):
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


def normalize(argv):
    feature = option(argv, "--feature")
    repo = Path(option(argv, "--repo") or ".")
    no_model = "--no-model" in argv
    mode = os.environ.get("FAKE_GUARDKIT_NORMALIZE", "")
    if not mode:
        if os.environ.get("FAKE_GUARDKIT_NO_MODEL_REFUSES") and no_model:
            mode = "refused"
        else:
            mode = "written"
    if mode == "unavailable":
        sys.stderr.write("Usage: guardkit qa [OPTIONS] COMMAND [ARGS]...\n"
                         "Error: No such command 'normalize-stamps'.\n")
        return 2
    if mode == "no-model-unknown" and no_model:
        sys.stderr.write("Usage: guardkit qa normalize-stamps [OPTIONS]\n"
                         "Error: No such option: --no-model\n")
        return 2
    if mode == "no-model-unknown":
        mode = "written"
    if mode == "failed":
        print(json.dumps({"error": "the plan YAML could not be read"}, indent=2))
        return 2
    if mode == "crash":
        sys.stderr.write("Traceback (most recent call last):\n  boom\n")
        return 1
    yaml_path = repo / ".guardkit" / "features" / f"{feature}.yaml"
    switched_off = {
        "status": "switched_off",
        "detail": "the model fallback was not asked: switched off for this "
                  "stamping by the caller",
    }
    not_configured = {"status": "not_configured",
                      "detail": "no model endpoint is configured"}
    if mode == "refused":
        print(json.dumps({
            "feature": feature, "written": False, "stamped": {}, "rules": {},
            "already_stamped": [], "refused": REFUSED,
            "model_outcome": switched_off if no_model else not_configured,
        }, indent=2))
        return 2
    text = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
    if mode == "partial":
        text += 'scenarios:\n  "ok":\n    verifier: "hurl"\n'
        yaml_path.write_text(text, encoding="utf-8")
        print(json.dumps({
            "feature": feature, "written": True, "stamped": {"ok": "hurl"},
            "rules": {"ok": "R9"}, "already_stamped": [], "refused": REFUSED,
            "model_outcome": switched_off if no_model else not_configured,
        }, indent=2))
        return 3
    stamped = {"ok": "hurl"}
    rules = {"ok": "R9"}
    model_outcome = switched_off if no_model else not_configured
    text += "scenarios:\n" + "".join(
        f'  "{title}":\n    verifier: "{word}"\n' for title, word in stamped.items()
    )
    yaml_path.write_text(text, encoding="utf-8")
    print(json.dumps({
        "feature": feature, "written": True, "stamped": stamped, "rules": rules,
        "already_stamped": [], "refused": [], "model_outcome": model_outcome,
    }, indent=2))
    return 0


def validate(argv):
    feature = argv[0] if argv else ""
    yaml_path = Path(".guardkit") / "features" / f"{feature}.yaml"
    seen = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
    log(["<validate saw stamps>", "scenarios:" in seen, "feature_files:" in seen])
    if os.environ.get("FAKE_GUARDKIT_VALIDATE") == "red":
        print(json.dumps({"valid": False, "errors": ["task file TASK-STAT-001.md missing"]}))
        return 1
    print(json.dumps({"valid": True, "errors": []}))
    return 0


def classify(argv):
    feature_file = option(argv, "--feature-file") or ""
    mode = os.environ.get("FAKE_GUARDKIT_CLASSIFY", "")
    if mode == "cannot":
        print(json.dumps({"error": f"cannot read {feature_file}"}))
        return 2
    if mode == "unavailable":
        sys.stderr.write("Error: No such command 'classify-scenarios'.\n")
        return 2
    refused = list(REFUSED) if mode == "refused" else []
    scenarios = [{"title": "ok", "home": "hurl", "rule": "R9", "refused": False}]
    scenarios += [{"title": t, "home": None, "rule": None, "refused": True} for t in refused]
    print(json.dumps({
        "feature_file": feature_file, "repo_has_http_surface": True,
        "http_surface_evidence": "fake", "scenarios": scenarios,
        "refused_titles": refused,
    }, indent=2))
    return 0


def qa_validate(kind, argv):
    """``qa validate pass-bar|gate-registry <path>`` — guardkit's own schema
    checkers, as far as the sidecar and the driver read them."""
    rel = argv[0] if argv else ""
    path = Path(rel)
    if not path.is_file():
        sys.stderr.write(f"Error: {rel} does not exist\n")
        return 1
    want = "red-pass-bar" if kind == "pass-bar" else "red-gate-registry"
    if os.environ.get("FAKE_GUARDKIT_QA_VALIDATE") == want:
        sys.stderr.write(f"{rel}: 'criteria' is a required property\n")
        return 1
    print(f"{rel}: valid")
    return 0


def main():
    argv = sys.argv[1:]
    log(argv)
    if argv[:2] == ["qa", "normalize-stamps"]:
        return normalize(argv[2:])
    if argv[:2] == ["feature", "validate"]:
        return validate(argv[2:])
    if argv[:2] == ["qa", "classify-scenarios"]:
        return classify(argv[2:])
    if argv[:3] == ["qa", "validate", "pass-bar"]:
        return qa_validate("pass-bar", argv[3:])
    if argv[:3] == ["qa", "validate", "gate-registry"]:
        return qa_validate("gate-registry", argv[3:])
    sys.stderr.write(f"Error: No such command {argv!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''


#: The marker the stand-in normalizer writes into a ``.feature`` it accepted —
#: proof that what the sidecar's checks rewrite in ITS worktree rides the
#: commit, the way the real normalizer's step collapse does.
NORMALIZED_MARKER = "# normalized by the stand-in\n"

#: The module path the normalizer is resolved at in a source checkout — the
#: second of the two candidates the production resolver probes.
NORMALIZER_MODULE = "installer.core.commands.lib.feature_spec_normalize"

FAKE_NORMALIZER_SOURCE = r'''"""The stand-in gherkin normalizer (see _fake_guardkit.py).

Behaves like guardkit's own module as far as the sidecar reads it: it is run
as ``python -m <this module> <path to the .feature>``, it may REWRITE the file
in place, and its exit code is the verdict.
"""
import json
import os
import sys
from pathlib import Path

MARKER = "# normalized by the stand-in\n"


def main():
    argv = sys.argv[1:]
    path = Path(argv[0]) if argv else None
    log = os.environ.get("FAKE_GUARDKIT_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": ["<normalize-feature>", *argv],
                                 "cwd": os.getcwd()}) + "\n")
    mode = os.environ.get("FAKE_NORMALIZER", "")
    if mode == "red":
        sys.stderr.write("gherkin parse error: line 3: expected a step keyword\n")
        return 1
    if path is None or not path.is_file():
        sys.stderr.write(f"no such feature file: {argv!r}\n")
        return 1
    text = path.read_text(encoding="utf-8")
    if MARKER not in text:
        path.write_text(text + MARKER, encoding="utf-8")
    print(f"{path}: parseable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def write_fake_normalizer(directory: Path) -> Path:
    """Write the stand-in normalizer as a real importable package tree under
    ``directory`` and return ``directory`` (the path to put on the import
    path). The tree is the source-checkout layout the production resolver's
    second candidate names."""
    package = directory / "installer" / "core" / "commands" / "lib"
    package.mkdir(parents=True, exist_ok=True)
    for level in (
        directory / "installer",
        directory / "installer" / "core",
        directory / "installer" / "core" / "commands",
        package,
    ):
        (level / "__init__.py").write_text("", encoding="utf-8")
    (package / "feature_spec_normalize.py").write_text(
        FAKE_NORMALIZER_SOURCE, encoding="utf-8"
    )
    return directory


def fake_normalizer(directory: Path, monkeypatch) -> "Iterator[Path]":
    """Plant the stand-in normalizer and make it the one BOTH the sidecar's
    own interpreter (which probes with ``find_spec``) and the subprocess it
    starts (which reads ``PYTHONPATH``) resolve. Use it from a fixture::

        @pytest.fixture
        def fake_normalizer_module(tmp_path, monkeypatch):
            yield from fake_normalizer(tmp_path / "normalizer", monkeypatch)

    Any ``installer`` package a previous test imported — the sibling guardkit
    checkout the planning conftest appends for its own test-root discovery —
    is dropped from the module cache on the way in and on the way out, so the
    plant is invisible to every other test and so is its removal.
    """
    import importlib
    import sys

    def forget_installer() -> None:
        for name in [
            n for n in sys.modules if n == "installer" or n.startswith("installer.")
        ]:
            del sys.modules[name]

    forget_installer()
    root = write_fake_normalizer(directory)
    # APPENDED, never prepended: the planning conftest appends the sibling
    # guardkit checkout for its own test-root discovery, and prepending would
    # take that name away from it. All the sidecar's probe needs is that the
    # module resolves at all; which one the SUBPROCESS runs is decided by
    # PYTHONPATH below, where the stand-in is first.
    monkeypatch.setattr(sys, "path", [*sys.path, str(root)])
    importlib.invalidate_caches()
    existing = os.environ.get("PYTHONPATH")
    monkeypatch.setenv(
        "PYTHONPATH", f"{root}{os.pathsep}{existing}" if existing else str(root)
    )
    monkeypatch.delenv("FAKE_NORMALIZER", raising=False)
    try:
        yield root
    finally:
        forget_installer()


def write_fake_guardkit(directory: Path) -> Path:
    """Write the stand-in to ``directory/guardkit`` and make it runnable."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "guardkit"
    binary.write_text(FAKE_GUARDKIT_SOURCE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def read_log(path: Path) -> list[dict]:
    """The fake's call log: one dict per line."""
    import json

    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def normalize_calls(path: Path) -> list[list[str]]:
    """The argv of every ``qa normalize-stamps`` call, in order."""
    return [
        entry["argv"]
        for entry in read_log(path)
        if entry["argv"][:2] == ["qa", "normalize-stamps"]
    ]


def validate_calls(path: Path) -> list[list[str]]:
    return [
        entry["argv"] for entry in read_log(path) if entry["argv"][:2] == ["feature", "validate"]
    ]


def classify_calls(path: Path) -> list[list[str]]:
    return [
        entry["argv"]
        for entry in read_log(path)
        if entry["argv"][:2] == ["qa", "classify-scenarios"]
    ]


def normalizer_calls(path: Path) -> list[list[str]]:
    """The argv of every stand-in NORMALIZER call, in order (the module run
    as ``python -m …``, logged under its check's name)."""
    return [
        entry["argv"][1:]
        for entry in read_log(path)
        if entry["argv"][:1] == ["<normalize-feature>"]
    ]


def qa_validate_calls(path: Path, kind: str) -> list[list[str]]:
    """The argv of every ``qa validate <kind> …`` call, in order."""
    return [
        entry["argv"]
        for entry in read_log(path)
        if entry["argv"][:3] == ["qa", "validate", kind]
    ]


def validate_saw(path: Path) -> list[tuple[bool, bool]]:
    """Per validate call: (saw stamps, saw feature_files)."""
    return [
        (bool(entry["argv"][1]), bool(entry["argv"][2]))
        for entry in read_log(path)
        if entry["argv"][:1] == ["<validate saw stamps>"]
    ]


def scratch_repo(path: Path) -> Path:
    """A git repository with one commit, the way the driver tests make one."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "README.md").write_text("scratch\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, env=env)
    return path


def git_show(repo: Path, branch: str, rel: str) -> str | None:
    import subprocess

    res = subprocess.run(
        ["git", "show", f"{branch}:{rel}"], cwd=repo, capture_output=True, text=True
    )
    return res.stdout if res.returncode == 0 else None


def git_rev_parse(repo: Path, ref: str) -> str | None:
    import subprocess

    res = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, capture_output=True, text=True
    )
    return res.stdout.strip() if res.returncode == 0 else None
