"""What the nightly sweep found, how each finding is named, and which matter.

Public surface
==============

- :class:`Finding` — one finding, holding ONLY the fields the design allows
  out of the sweep (step 3): project, file path, line, rule id, the rule's own
  fixed message, package, version, advisory id, severity, score and fixed
  versions, plus the tool, the kind, and whether the path is a test,
  documentation or a lockfile. No source text and no scanner output.
- :func:`dependency_fingerprint` / :func:`line_fingerprint` — the names
  findings are remembered by (step 4, Codex R11).
- :func:`path_kinds` — test, documentation or lockfile, from the path alone.
- :func:`is_serious` / :func:`seriousness_order` — the design's step 6 rule,
  and the order the ten most serious are chosen in (Codex R10).

Why the line fingerprint is keyed
---------------------------------

A secret or code finding is remembered by its project, tool, rule, file and
line, and by a hash of that line's text exactly as committed. The hash is
HMAC-SHA-256 with a random key kept only in the sweep's own state folder, so
the stored hash cannot be used to check a guessed secret: without the key it
says nothing about the line. The line is read from the exported file, hashed
and dropped; it is never stored. Hashing the whole line means a documentation
example swapped in place for a real key is a NEW finding, sorted afresh,
while an untouched finding keeps its name and stays remembered.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable

#: The three kinds of finding.
KIND_DEPENDENCY = "dependency"
KIND_SECRET = "secret"
KIND_CODE = "code"

#: The three tools, by the names the sweep records.
TOOL_OSV = "osv-scanner"
TOOL_GITLEAKS = "gitleaks"
TOOL_OPENGREP = "opengrep"

#: Advisory ids that name a known malicious package.
MALICIOUS_PREFIX = "MAL-"

#: Lockfile names the sweep recognises when it says where a secret sits. The
#: dependency scanner recognises its own files; this list only labels a path.
LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "go.sum",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "pdm.lock",
        "composer.lock",
        "gemfile.lock",
        "packages.lock.json",
        "mix.lock",
        "pubspec.lock",
        "gradle.lockfile",
        "conan.lock",
    }
)

_TEST_FOLDERS = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "fixtures", "testdata", "e2e"}
)
_DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".adoc", ".txt"})
_DOC_FOLDERS = frozenset({"doc", "docs", "documentation"})


def shown(text: object) -> str:
    """``text`` safe to repeat outward: control characters made visible.

    A file name, package name or version comes from the project, and a
    newline or other control character in one could break a line Rich reads
    (the queued sentence, the Slack line) or the saved state and history.
    Every character below 0x20, and 0x7f, is written as ``\\xNN`` instead
    (fix pass, 17 October 2026).
    """
    return "".join(
        f"\\x{ord(ch):02x}" if ord(ch) < 0x20 or ord(ch) == 0x7F else ch
        for ch in str(text)
    )


def path_kinds(path: str) -> tuple[str, ...]:
    """Whether ``path`` is a test, documentation or lockfile, from its name.

    Read from the path alone, so it can be kept beside a secret finding
    without anything around the secret ever being read.
    """
    pure = PurePosixPath(path)
    name = pure.name.lower()
    folders = {part.lower() for part in pure.parts[:-1]}
    kinds: list[str] = []
    stem = name.split(".", 1)[0]
    if (
        folders & _TEST_FOLDERS
        or stem.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
    ):
        kinds.append("test")
    if pure.suffix.lower() in _DOC_SUFFIXES or folders & _DOC_FOLDERS:
        kinds.append("documentation")
    if name in LOCKFILE_NAMES:
        kinds.append("lockfile")
    return tuple(kinds)


@dataclass(frozen=True)
class Finding:
    """One finding, as the sweep keeps it: allowlisted fields only."""

    project: str
    kind: str
    tool: str
    rule: str
    file: str
    line: int | None
    fingerprint: str
    #: The rule's own fixed message (code findings), cut to one sentence.
    message: str | None = None
    #: ``ERROR``/``WARNING``/``INFO`` for code; ``critical``/``high``/... for
    #: dependency advisories.
    severity: str | None = None
    package: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    advisory: str | None = None
    score: float | None = None
    fixed: tuple[str, ...] = ()
    path_kinds: tuple[str, ...] = ()
    #: The last line of a code finding, used only to cut the excerpt the
    #: model sees; never stored.
    end_line: int | None = field(default=None, compare=False)
    #: The file's path exactly as committed (``file`` is its shown form),
    #: used only to read the excerpt; never stored.
    source_file: str | None = field(default=None, compare=False)

    @property
    def needs_sorting(self) -> bool:
        """Secrets and code patterns are sorted by the model; advisories are not."""
        return self.kind in (KIND_SECRET, KIND_CODE)

    @property
    def is_malicious(self) -> bool:
        return self.kind == KIND_DEPENDENCY and str(self.advisory or "").startswith(
            MALICIOUS_PREFIX
        )

    def record(self) -> dict[str, object]:
        """The finding as saved state: the allowlisted fields, no others."""
        out: dict[str, object] = {
            "project": self.project,
            "kind": self.kind,
            "tool": self.tool,
            "rule": self.rule,
            "file": self.file,
            "line": self.line,
        }
        for name in ("message", "severity", "package", "version", "ecosystem", "advisory", "score"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.fixed:
            out["fixed"] = list(self.fixed)
        if self.path_kinds:
            out["path_kinds"] = list(self.path_kinds)
        return out

    @classmethod
    def from_record(cls, fingerprint: str, record: dict[str, object]) -> "Finding":
        """A finding read back from saved state."""
        line = record.get("line")
        score = record.get("score")
        return cls(
            project=str(record.get("project") or ""),
            kind=str(record.get("kind") or ""),
            tool=str(record.get("tool") or ""),
            rule=str(record.get("rule") or ""),
            file=str(record.get("file") or ""),
            line=int(line) if isinstance(line, int) else None,
            fingerprint=fingerprint,
            message=_text_or_none(record.get("message")),
            severity=_text_or_none(record.get("severity")),
            package=_text_or_none(record.get("package")),
            version=_text_or_none(record.get("version")),
            ecosystem=_text_or_none(record.get("ecosystem")),
            advisory=_text_or_none(record.get("advisory")),
            score=float(score) if isinstance(score, (int, float)) else None,
            fixed=tuple(str(v) for v in (record.get("fixed") or []) if isinstance(v, str)),
            path_kinds=tuple(
                str(v) for v in (record.get("path_kinds") or []) if isinstance(v, str)
            ),
        )


def _text_or_none(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _digest(parts: Iterable[str]) -> str:
    joined = "\x00".join(parts).encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(joined).hexdigest()[:32]


def dependency_fingerprint(project: str, advisory: str, package: str, version: str) -> str:
    """A dependency finding's name: project, advisory, and package with version."""
    return _digest((KIND_DEPENDENCY, project, advisory, f"{package}@{version}"))


def line_fingerprint(
    key: bytes,
    *,
    project: str,
    kind: str,
    tool: str,
    rule: str,
    file: str,
    line: int,
    line_text: bytes,
) -> str:
    """A secret or code finding's name (Codex R11).

    ``line_text`` is the line's bytes exactly as committed, without its line
    ending. It is hashed with ``key`` (HMAC-SHA-256) and dropped; only the
    combined digest is returned.
    """
    keyed = hmac.new(key, line_text, hashlib.sha256).hexdigest()
    return _digest((kind, project, tool, rule, file, str(line), keyed))


def is_serious(finding: Finding, status: str, serious_score: float) -> bool:
    """The design's step 6 rule.

    Serious means: a dependency advisory scoring ``serious_score`` or more, or
    any malicious package (``MAL-``); a secret in the current tree the model
    sorted real; a code finding of ERROR severity the model sorted real.
    """
    if finding.kind == KIND_DEPENDENCY:
        if finding.is_malicious:
            return True
        return finding.score is not None and finding.score >= serious_score
    if status not in ("real", "drafted"):
        return False
    if finding.kind == KIND_SECRET:
        return True
    if finding.kind == KIND_CODE:
        return str(finding.severity or "").upper() == "ERROR"
    return False


def seriousness_order(finding: Finding) -> tuple[object, ...]:
    """Sort key, most serious first (Codex R10).

    Malicious packages first, then real secrets, then the rest by score,
    highest first (a code finding has no score and follows the advisories).
    Ties are broken by project, file, line and rule so the choice is the same
    every night.
    """
    if finding.is_malicious:
        tier = 0
    elif finding.kind == KIND_SECRET:
        tier = 1
    elif finding.kind == KIND_DEPENDENCY:
        tier = 2
    else:
        tier = 3
    score = finding.score if finding.score is not None else -1.0
    return (
        tier,
        -score,
        finding.project,
        finding.file,
        finding.line if finding.line is not None else -1,
        finding.rule,
        finding.package or "",
        finding.advisory or "",
    )


__all__ = [
    "Finding",
    "KIND_CODE",
    "KIND_DEPENDENCY",
    "KIND_SECRET",
    "LOCKFILE_NAMES",
    "MALICIOUS_PREFIX",
    "TOOL_GITLEAKS",
    "TOOL_OPENGREP",
    "TOOL_OSV",
    "dependency_fingerprint",
    "is_serious",
    "line_fingerprint",
    "path_kinds",
    "seriousness_order",
    "shown",
]
