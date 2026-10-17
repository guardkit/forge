"""Build the nightly sweep's Opengrep rule file from the community rules.

WHY THIS EXISTS (security check design, 17 October 2026, piece 2). The nightly
security sweep runs Opengrep with the security rules of the
``opengrep/opengrep-rules`` repository at one pinned commit. That repository
holds every kind of rule (style, correctness, performance, security) as one
YAML file per rule set, beside the test files the rules are checked against.
The sweep wants only the security ones, in one file, fixed at image build
time so nothing is fetched when the image runs.

WHAT IT KEEPS. Every rule, in every rule file ending ``.yaml`` or ``.yml``,
whose ``metadata.category`` is ``security`` — except the rules that look for
hard-coded secrets. Left out: the repository's own test files
(``*.test.yaml``, ``*.test.yml``, which hold copies of rules on purpose),
anything under a hidden folder (``.github``), and files that are not rule
files or do not parse. Duplicate rule ids are kept as the repository has
them: some rules exist once per language under the same id.

WHY THE SECRET-VALUE RULES ARE LEFT OUT (fix pass, 17 October 2026).
Secrets belong to Gitleaks, which runs with ``--redact``. A rule here that
detects a secret VALUE would duplicate it, and worse: the sweep sends a code
finding's surrounding lines to the model after hiding every line Gitleaks
flags, so a secret only an Opengrep rule caught could reach the model. Left
out, and only these:

- every rule in a file under ``generic/secrets/``, or under any folder named
  ``secrets`` (the repository's value detectors, in any language);
- any other rule whose CWE metadata names CWE-798 (hard-coded credentials)
  AND whose id starts with ``detected-``.

Rules about how secrets are HANDLED stay: ``secret-in-build-arg``,
``django-no-csrf-token``, ``md5-used-as-password`` and the like find a
weakness in code, not a value. ``deploy/security/dropped-secret-rules.txt``
lists what is left out at the pinned commit, made with ``--list-dropped``.

WHY THE OUTPUT IS JSON IN A ``.yaml`` FILE. JSON is a subset of YAML, which is
what Opengrep reads, and Python's ``json`` writes it the same way every time.
So the file's sha256 depends only on the rules and on how PyYAML reads them,
not on how a YAML writer happens to lay text out. The image records that
sha256 and refuses to build when it differs from the value pinned beside the
commit.

It needs only PyYAML, which the image's own environment already has (the
Dockerfile copies that one package in; nothing is installed for this).

Usage::

    python build_opengrep_rules.py <rules checkout> <output file>
    python build_opengrep_rules.py --list-dropped <rules checkout>

The first prints one line: how many rules were kept and the output's sha256.
The second prints each left-out secret-value rule as ``<file>\t<id>``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

#: The category a rule must carry to be kept.
SECURITY_CATEGORY = "security"

#: The weakness id of hard-coded credentials; rules naming it are Gitleaks' job.
HARD_CODED_CREDENTIALS = "CWE-798"

#: The id prefix the repository's value detectors use.
DETECTOR_PREFIX = "detected-"

#: The folder the repository keeps its value detectors in.
SECRETS_FOLDER = "secrets"


def _rule_files(root: Path) -> list[Path]:
    """Every candidate rule file under ``root``, in a fixed order."""
    found: list[Path] = []
    for path in [*root.rglob("*.yaml"), *root.rglob("*.yml")]:
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if ".test." in path.name:
            continue
        if path.is_file() and not path.is_symlink():
            found.append(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def is_secret_value_rule(rule: dict[str, Any], relative: str) -> bool:
    """Whether a rule detects a secret value (left to Gitleaks).

    ``relative`` is the rule file's path inside the checkout.
    """
    if SECRETS_FOLDER in relative.split("/")[:-1]:
        return True
    metadata = rule.get("metadata")
    cwe = metadata.get("cwe") if isinstance(metadata, dict) else None
    entries = cwe if isinstance(cwe, list) else [cwe]
    names_798 = any(HARD_CODED_CREDENTIALS in str(entry) for entry in entries if entry)
    return names_798 and str(rule.get("id") or "").startswith(DETECTOR_PREFIX)


def _security_rules_by_file(root: Path) -> list[tuple[str, dict[str, Any]]]:
    """Every security rule with its file's relative path, in file order."""
    found: list[tuple[str, dict[str, Any]]] = []
    for path in _rule_files(root):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            # Not every .yaml file in the repository is a rule file, and a few
            # carry templating that is not plain YAML. They hold no rule the
            # sweep could run.
            continue
        if not isinstance(loaded, dict):
            continue
        rules = loaded.get("rules")
        if not isinstance(rules, list):
            continue
        relative = path.relative_to(root).as_posix()
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
                continue
            metadata = rule.get("metadata")
            if isinstance(metadata, dict) and metadata.get("category") == SECURITY_CATEGORY:
                found.append((relative, rule))
    return found


def security_rules(root: Path) -> list[dict[str, Any]]:
    """The security rules of a rules checkout at ``root``, value detectors left out."""
    return [
        rule
        for relative, rule in _security_rules_by_file(root)
        if not is_secret_value_rule(rule, relative)
    ]


def dropped_rules(root: Path) -> list[tuple[str, str]]:
    """``(file, id)`` of every secret-value rule left out, in file order."""
    return [
        (relative, str(rule["id"]))
        for relative, rule in _security_rules_by_file(root)
        if is_secret_value_rule(rule, relative)
    ]


def render(rules: list[dict[str, Any]]) -> bytes:
    """The rule file's exact bytes: one JSON document, keys sorted."""
    text = json.dumps({"rules": rules}, sort_keys=True, indent=1, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--list-dropped":
        for relative, rule_id in dropped_rules(Path(argv[2])):
            print(f"{relative}\t{rule_id}")
        return 0
    if len(argv) != 3:
        print("usage: build_opengrep_rules.py <rules checkout> <output file>", file=sys.stderr)
        return 2
    root, output = Path(argv[1]), Path(argv[2])
    rules = security_rules(root)
    if not rules:
        print("no security rules were found in the checkout", file=sys.stderr)
        return 1
    data = render(rules)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    print(f"{len(rules)} security rules; sha256 {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
