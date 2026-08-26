"""Digest conformance v1 — does the merged code keep the spec digest's promises?

ADVISORY, DETERMINISTIC, OFFLINE. This check runs inside the merge executor
after a merge lands. It can add one plain warning line to the merge report and
a receipt beside the other merge receipts; it can never block a merge, a
deploy, or anything else.

WHY IT EXISTS (the motivating case, 2026-08-25/26)
--------------------------------------------------
api_test FEAT-EF8D: the approved digest declared ``GET /users/created-per-day``
and promised "exactly seven days ... never more than seven entries". The built
router served a 30-day, no-zero-fill query at that exact path, and put the
correct 7-day logic at an unrequested ``/users/daily-counts``. Every test was
green — because the tests asserting the number 7 all pointed at the wrong
path. Only a human diff-read caught it. The lesson: running the tests proves
the tests; only reading the spec against the tree proves the promise.

WHAT IT CHECKS
--------------
Given the feature's digest yaml (located through the feature record at
``.guardkit/features/<feature-id>.yaml`` -> ``feature_files`` -> the sibling
``<slug>_digest.yaml``) and the merged working tree:

(a) endpoint-exists — the declared method+path is registered somewhere in the
    code (decorator-style registration, resolving same-file router prefixes,
    or an OpenAPI document).
(b) scenario-has-a-test — each digest scenario has at least one plausible
    verification: a test file or twin whose name or content mentions the
    endpoint path, quotes the scenario title, or matches enough of the
    title's key words.
(c) number-promise-is-tested — each numeric promise in the scenario sentences
    ("exactly N", "never/no more than N", "at least N", number words
    included) is echoed by at least one test file that mentions the declared
    path AND has an assert-style line comparing against that exact number.

HONEST STATEMENT OF BLINDNESS
-----------------------------
These are text heuristics, not proofs, and each has a known blind side:

* Route matching is by path text. A route whose literal merely matches the
  TAIL of the declared path passes check (a) with a weaker evidence string —
  a matching tail under a different, unresolved router prefix would pass
  wrongly. Registration styles other than decorators and OpenAPI documents
  are invisible.
* Check (b) is satisfied for EVERY scenario by a single test file that
  mentions the endpoint path. It catches a feature with no tests pointed at
  it at all; it does not prove any scenario's behaviour is asserted.
* Check (c) looks for the digit next to a comparison sign on an assert-style
  line in a file that mentions the path. It does not prove the comparison
  runs against this endpoint's response, and it does not match the DIRECTION
  of the promise (a ``<= 7`` satisfies an "exactly 7" promise here).
* A digest that declares no endpoint gets check (c) scoped to all test
  files, which is much weaker.

Every receipt this module writes carries these blind spots under
``blind_spots`` so a reader of the receipt sees the limits without opening
this file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "check_digest_conformance",
    "find_digest_for_feature",
    "run_digest_conformance",
]

#: Directories never scanned at all.
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".venv",
    "venv",
    ".tox",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".guardkit",
}

#: Directory names whose ``.py`` files count as VERIFICATION (tests/twins).
_VERIFICATION_DIR_NAMES = {"tests", "test", "qa"}

#: Excluded from the ROUTE scan on top of the verification dirs: specs, docs
#: and task notes may quote a path without serving it.
_NON_SOURCE_DIR_NAMES = _VERIFICATION_DIR_NAMES | {"features", "docs", "tasks"}

#: Files above this size are skipped (route/tests scans are line-oriented).
_MAX_FILE_BYTES = 2_000_000

_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

#: Words too common in scenario titles to identify one scenario.
_TITLE_STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "of",
    "is",
    "in",
    "are",
    "and",
    "or",
    "not",
    "with",
    "for",
    "on",
    "that",
    "this",
    "it",
    "its",
    "does",
    "do",
    "when",
    "then",
    "given",
    "be",
    "has",
    "have",
    "any",
    "all",
}

#: Decorator-style route registration: ``@router.get("/x")`` (whitespace and
#: newlines allowed after the opening paren, single or double quotes).
_ROUTE_DECORATOR_RE = re.compile(
    r"@\w+(?:\.\w+)*\.(get|post|put|delete|patch|head|options)"
    r"\(\s*[\"']([^\"']*)[\"']",
    re.IGNORECASE,
)

#: Same-file router prefix: ``APIRouter(prefix="/users", ...)``.
_ROUTER_PREFIX_RE = re.compile(
    r"APIRouter\(\s*[^)]*?prefix\s*=\s*[\"']([^\"']*)[\"']", re.DOTALL
)

_EXACTLY_RE = re.compile(r"\bexactly\s+([a-z0-9]+)")
_AT_MOST_RE = re.compile(
    r"\b(?:never[^.]{0,80}?more than|no more than|not more than|at most)"
    r"\s+([a-z0-9]+)"
)
_AT_LEAST_RE = re.compile(r"\bat least\s+([a-z0-9]+)")

_ANY_COMPARATOR_RE = re.compile(r"==|<=|>=|!=|<|>")

_BLIND_SPOTS = [
    "route matching is by path text: a route matching only the tail of the "
    "declared path passes with weaker evidence, and a matching tail under a "
    "different router prefix could pass wrongly",
    "one test file that mentions the endpoint path satisfies the "
    "scenario-has-a-test check for every scenario — that check catches a "
    "feature with no tests pointed at it, not a scenario left unasserted",
    "the number check looks for the digit next to a comparison sign on an "
    "assert-style line in a file that mentions the path; it does not prove "
    "the comparison runs against this endpoint's response, and it does not "
    "match the direction of the promise",
    "only decorator-style route registrations and OpenAPI documents are "
    "scanned; other registration styles are invisible",
]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _load_structured(path: Path) -> Any:
    text = _read_text(path)
    if text is None:
        return None
    try:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        return yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):
        return None


def _collect_files(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """One deterministic walk -> (source files, verification files, openapi)."""
    source: list[Path] = []
    verification: list[Path] = []
    openapi: list[Path] = []
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        rel_dirs = set(path.relative_to(root).parts[:-1])
        if rel_dirs & _SKIP_DIRS:
            continue
        suffix = path.suffix.lower()
        in_verification_dir = bool(rel_dirs & _VERIFICATION_DIR_NAMES)
        if suffix == ".hurl" or (
            suffix == ".py"
            and (in_verification_dir or path.name.startswith("test_"))
        ):
            verification.append(path)
        if rel_dirs & _NON_SOURCE_DIR_NAMES:
            continue
        if path.name.lower() in ("openapi.json", "openapi.yaml", "openapi.yml"):
            openapi.append(path)
        if suffix == ".py" and not path.name.startswith("test_"):
            source.append(path)
    return source, verification, openapi


def _path_reference_forms(path: str) -> list[str]:
    """The text forms a test plausibly uses to point at the endpoint."""
    forms: list[str] = []
    path_l = path.lower()
    if path_l:
        forms.append(path_l)
    segment = path_l.rstrip("/").rsplit("/", 1)[-1]
    if segment and segment not in forms:
        forms.append(segment)
    underscored = segment.replace("-", "_")
    if underscored and underscored not in forms:
        forms.append(underscored)
    return forms


def _references_path(
    path: Path, text: str, forms: list[str], root: Path
) -> bool:
    rel = str(path.relative_to(root)).lower()
    content = text.lower()
    return any(form in content or form in rel for form in forms)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _scenario_tokens(title: str) -> list[str]:
    """Key words of a scenario title (number words doubled as digits)."""
    tokens: list[str] = []
    for word in re.findall(r"[a-z0-9]+", title.lower()):
        if word in _TITLE_STOPWORDS:
            continue
        if not (word.isdigit() or len(word) >= 3):
            continue
        if word not in tokens:
            tokens.append(word)
        digit = _NUMBER_WORDS.get(word)
        if digit is not None and str(digit) not in tokens:
            tokens.append(str(digit))
    return tokens


def _token_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _extract_number_promises(
    scenarios: list[dict[str, Any]],
) -> list[tuple[str, int, str]]:
    """(kind, number, quoted sentence) for each distinct numeric promise."""
    seen: dict[tuple[str, int], str] = {}
    patterns = (
        (_AT_MOST_RE, "no more than"),
        (_EXACTLY_RE, "exactly"),
        (_AT_LEAST_RE, "at least"),
    )
    for scenario in scenarios:
        for text in (
            str(scenario.get("title") or ""),
            str(scenario.get("sentence") or ""),
        ):
            lowered = text.lower()
            for regex, kind in patterns:
                for match in regex.finditer(lowered):
                    number = _token_number(match.group(1))
                    if number is None:
                        continue
                    seen.setdefault((kind, number), text.strip())
    return [(kind, number, quoted) for (kind, number), quoted in seen.items()]


def _check_endpoint_exists(
    method: str,
    path: str,
    source_files: list[Path],
    openapi_files: list[Path],
    root: Path,
) -> tuple[bool, str]:
    method_l = method.lower()
    path_l = path.lower().rstrip("/") or "/"
    tail_evidence: str | None = None
    for source in source_files:
        text = _read_text(source)
        if text is None:
            continue
        prefixes = [m.group(1) for m in _ROUTER_PREFIX_RE.finditer(text)]
        for match in _ROUTE_DECORATOR_RE.finditer(text):
            if match.group(1).lower() != method_l:
                continue
            literal = match.group(2)
            literal_l = literal.lower().rstrip("/")
            lineno = text.count("\n", 0, match.start()) + 1
            rel = source.relative_to(root)
            if literal_l == path_l:
                return True, (
                    f'{rel}:{lineno} registers {method.upper()} "{literal}"'
                )
            for prefix in prefixes:
                stripped = literal_l.lstrip("/")
                joined = (
                    (prefix.lower().rstrip("/") + "/" + stripped).rstrip("/")
                    if stripped
                    else prefix.lower().rstrip("/")
                )
                if joined == path_l:
                    return True, (
                        f'{rel}:{lineno} registers {method.upper()} '
                        f'"{literal}" under router prefix "{prefix}"'
                    )
            if (
                tail_evidence is None
                and len(literal_l) > 1
                and path_l.endswith(literal_l)
            ):
                tail_evidence = (
                    f'{rel}:{lineno} registers {method.upper()} "{literal}", '
                    f"which matches the end of {path} — the router prefix "
                    "was not resolved, so this is a weaker match"
                )
    for doc_path in openapi_files:
        document = _load_structured(doc_path)
        paths = document.get("paths") if isinstance(document, dict) else None
        if not isinstance(paths, dict):
            continue
        for key, operations in paths.items():
            if str(key).lower().rstrip("/") != path_l:
                continue
            if isinstance(operations, dict) and method_l in {
                str(op).lower() for op in operations
            }:
                return True, (
                    f"{doc_path.relative_to(root)} documents "
                    f"{method.upper()} {path}"
                )
    if tail_evidence is not None:
        return True, tail_evidence
    return False, (
        f"no route registration or OpenAPI entry for {method.upper()} {path} "
        "was found in the merged code"
    )


def _check_scenario_mapped(
    title: str,
    verification: list[tuple[Path, str]],
    path_forms: list[str],
    root: Path,
) -> tuple[bool, str]:
    normalized_title = _normalize(title)
    tokens = _scenario_tokens(title)
    threshold = max(2, (len(tokens) + 1) // 2)
    for path, text in verification:
        rel = str(path.relative_to(root))
        rel_l = rel.lower()
        content = text.lower()
        for form in path_forms:
            if form in content or form in rel_l:
                return True, f"{rel} mentions the endpoint path ({form})"
        if normalized_title and normalized_title in _normalize(text):
            return True, f"{rel} quotes the scenario title"
        if tokens:
            hits = [
                token
                for token in tokens
                if re.search(
                    r"(?<![a-z0-9_])" + re.escape(token) + r"(?![a-z0-9_])",
                    content,
                )
                or re.search(
                    r"(?<![a-z0-9_])" + re.escape(token) + r"(?![a-z0-9_])",
                    rel_l,
                )
            ]
            if len(hits) >= threshold:
                return True, (
                    f"{rel} matches key words from the title: "
                    + ", ".join(hits[:6])
                )
    if not verification:
        return False, "no test files or twins were found in the merged tree"
    return False, (
        "no test or twin mentions the endpoint path, quotes this scenario's "
        "title, or matches enough of its key words"
    )


def _check_number_promise(
    kind: str,
    number: int,
    referencing: list[tuple[Path, str]],
    method: str,
    path: str,
    root: Path,
) -> tuple[bool, str]:
    digits = re.escape(str(number))
    comparator_number = re.compile(
        r"(?:==|<=|>=|!=|<|>)\s*" + digits + r"(?![\w.])"
        r"|(?<![\w.])" + digits + r"\s*(?:==|<=|>=|!=|<|>)"
    )
    for file_path, text in referencing:
        is_hurl = file_path.suffix.lower() == ".hurl"
        for lineno, line in enumerate(text.splitlines(), 1):
            assert_like = (
                bool(_ANY_COMPARATOR_RE.search(line))
                if is_hurl
                else "assert" in line
            )
            if assert_like and comparator_number.search(line):
                rel = file_path.relative_to(root)
                return True, (
                    f"{rel}:{lineno} compares against {number}: "
                    + line.strip()[:120]
                )
    where = f"{method.upper()} {path}" if path else "the declared endpoint"
    if referencing:
        names = ", ".join(
            str(p.relative_to(root)) for p, _ in referencing[:3]
        )
        more = (
            f" and {len(referencing) - 3} more"
            if len(referencing) > 3
            else ""
        )
        return False, (
            f"{len(referencing)} test file(s) mention {where} "
            f"({names}{more}), but none of them asserts the number {number} "
            "in a comparison"
        )
    return False, (
        f"no test or twin mentions {where} at all, so the promise of "
        f"{kind} {number} is untested"
    )


def _compose_warning(
    failing: list[dict[str, Any]], method: str, path: str
) -> str:
    phrases: list[str] = []
    for check in failing:
        if check["check"] == "endpoint-exists":
            phrases.append(
                f"the spec declares {check['subject']} but no route for it "
                "was found in the merged code"
            )
        elif check["check"] == "number-promise-is-tested":
            where = (
                f"{method.upper()} {path}" if path else "the feature"
            )
            phrases.append(
                f'the spec promises "{check["subject"]}" for {where}, but '
                "no test that mentions that path asserts the number "
                f"{check['number']} in a comparison"
            )
        elif check["check"] == "scenario-has-a-test":
            phrases.append(
                f'no test was found for the scenario "{check["subject"]}"'
            )
    rest = len(phrases) - 1
    tail = (
        f" (and {rest} more finding{'s' if rest != 1 else ''} in the merge "
        "receipts)"
        if rest > 0
        else ""
    )
    return (
        phrases[0]
        + tail
        + ". This check is advisory and did not block the merge — worth a "
        "look at the diff."
    )


def check_digest_conformance(
    digest: dict[str, Any], repo_root: Path
) -> dict[str, Any]:
    """Check one parsed digest against the tree at ``repo_root``.

    Pure and deterministic; returns the receipt dict (see the module
    docstring for the three checks and their honest limits).
    """
    endpoint = digest.get("endpoint") or {}
    method = (
        str(endpoint.get("method") or "").strip() or "GET"
        if isinstance(endpoint, dict)
        else "GET"
    )
    path = (
        str(endpoint.get("path") or "").strip()
        if isinstance(endpoint, dict)
        else ""
    )
    scenarios = [
        s for s in (digest.get("scenarios") or []) if isinstance(s, dict)
    ]

    source_files, verification_paths, openapi_files = _collect_files(repo_root)
    verification: list[tuple[Path, str]] = []
    for verification_path in verification_paths:
        text = _read_text(verification_path)
        if text is not None:
            verification.append((verification_path, text))

    checks: list[dict[str, Any]] = []
    if path:
        ok, evidence = _check_endpoint_exists(
            method, path, source_files, openapi_files, repo_root
        )
        checks.append(
            {
                "check": "endpoint-exists",
                "subject": f"{method.upper()} {path}",
                "verdict": "pass" if ok else "fail",
                "evidence": evidence,
            }
        )
    else:
        checks.append(
            {
                "check": "endpoint-exists",
                "subject": "(no endpoint declared)",
                "verdict": "pass",
                "evidence": (
                    "the digest declares no endpoint, so there is no route "
                    "to look for"
                ),
            }
        )

    path_forms = _path_reference_forms(path) if path else []
    if path:
        referencing = [
            (p, text)
            for p, text in verification
            if _references_path(p, text, path_forms, repo_root)
        ]
    else:
        # No declared path to scope by — every test file is in scope, which
        # is a much weaker check (named under blind_spots).
        referencing = list(verification)

    for kind, number, quoted in _extract_number_promises(scenarios):
        ok, evidence = _check_number_promise(
            kind, number, referencing, method, path, repo_root
        )
        checks.append(
            {
                "check": "number-promise-is-tested",
                "subject": f"{kind} {number}",
                "kind": kind,
                "number": number,
                "quoted_from": quoted,
                "verdict": "pass" if ok else "fail",
                "evidence": evidence,
            }
        )

    for scenario in scenarios:
        title = str(scenario.get("title") or "").strip()
        if not title:
            continue
        ok, evidence = _check_scenario_mapped(
            title, verification, path_forms, repo_root
        )
        checks.append(
            {
                "check": "scenario-has-a-test",
                "subject": title,
                "verdict": "pass" if ok else "fail",
                "evidence": evidence,
            }
        )

    failing = [c for c in checks if c["verdict"] == "fail"]
    conformant = not failing
    warning = None if conformant else _compose_warning(failing, method, path)
    return {
        "advisory": True,
        "conformant": conformant,
        "checks": checks,
        "warning": warning,
        "blind_spots": list(_BLIND_SPOTS),
    }


def find_digest_for_feature(repo_root: Path, feature_id: str) -> Path | None:
    """Locate the feature's digest through its ``.guardkit`` feature record."""
    record_path = repo_root / ".guardkit" / "features" / f"{feature_id}.yaml"
    if not record_path.is_file():
        return None
    record = _load_structured(record_path)
    if not isinstance(record, dict):
        return None
    for entry in record.get("feature_files") or []:
        feature_file = repo_root / str(entry)
        digest_path = feature_file.with_name(
            feature_file.stem + "_digest.yaml"
        )
        if digest_path.is_file():
            return digest_path
    return None


def run_digest_conformance(
    *, repo_root: Path, feature_id: str
) -> dict[str, Any]:
    """Locate the feature's digest and check the tree against it.

    Always returns a receipt dict. When there is no digest to check (or it
    cannot be read), ``conformant`` is None, ``skipped`` says why in plain
    words, and there is never a warning — an absent digest is not a failure.
    """
    repo_root = Path(repo_root)
    base: dict[str, Any] = {
        "advisory": True,
        "feature_id": feature_id,
        "conformant": None,
        "checks": [],
        "warning": None,
    }
    digest_path = find_digest_for_feature(repo_root, feature_id)
    if digest_path is None:
        base["skipped"] = (
            f"no spec digest was found for {feature_id} — nothing was checked"
        )
        return base
    digest = _load_structured(digest_path)
    if not isinstance(digest, dict):
        base["digest_path"] = str(digest_path.relative_to(repo_root))
        base["skipped"] = (
            f"the digest at {digest_path.relative_to(repo_root)} could not "
            "be read as a mapping — nothing was checked"
        )
        return base
    report = check_digest_conformance(digest, repo_root)
    report["feature_id"] = feature_id
    report["digest_path"] = str(digest_path.relative_to(repo_root))
    return report
