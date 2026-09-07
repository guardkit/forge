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

Every call appends one JSON line (argv, cwd) to ``FAKE_GUARDKIT_LOG`` so a
test can prove what ran, in what order, with which flags.
"""

from __future__ import annotations

import os
import stat
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


def main():
    argv = sys.argv[1:]
    log(argv)
    if argv[:2] == ["qa", "normalize-stamps"]:
        return normalize(argv[2:])
    if argv[:2] == ["feature", "validate"]:
        return validate(argv[2:])
    if argv[:2] == ["qa", "classify-scenarios"]:
        return classify(argv[2:])
    sys.stderr.write(f"Error: No such command {argv!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''


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
