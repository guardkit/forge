"""Naming and resolving the repository a planning sentence is about.

One sentence from Slack may name the repository it wants built in. The name
a person types is short ("study-tutor"); the configuration key is long
("guardkit/study-tutor"), and two keys sometimes point at the SAME checkout
(the same repo under two org names). This module is the single place that
turns a typed name into a configuration key, and the single place that
writes the list of names a person is allowed to type.

Both are needed in three places — the intake consumer (refuse an unknown
name before any leg runs), the chain driver and the handoff handler (name
the known repositories when a late resolution fails) — so they live here
rather than being written out three times.

References:
- ``docs/target-repo-intake-fix-spec-2026-09-05.md`` rules 3 and 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "RepoResolution",
    "format_known_repos",
    "known_repo_names",
    "resolve_target_repo",
    "unknown_repo_message",
]


@dataclass(frozen=True, slots=True)
class RepoResolution:
    """The outcome of resolving one typed name.

    Attributes:
        name: The configuration key to use, or None when the name could not
            be resolved.
        reason: The machine reason for the durable row when ``name`` is
            None; empty when the name resolved.
    """

    name: str | None
    reason: str = ""


def _basename(key: str) -> str:
    """The part of an ``org/name`` key after the slash."""
    return key.rsplit("/", 1)[-1]


def known_repo_names(target_repo_paths: Mapping[str, str]) -> list[str]:
    """The names a person may type, one per checkout.

    Two configuration keys that point at the SAME checkout are the same
    repository under two names, so they collapse to a single entry: the
    shared short name when the keys share one, else the first key.
    """
    by_path: dict[str, list[str]] = {}
    for key, path in target_repo_paths.items():
        by_path.setdefault(str(path), []).append(key)

    names: list[str] = []
    for keys in by_path.values():
        if len(keys) == 1:
            names.append(keys[0])
            continue
        basenames = {_basename(key) for key in keys}
        names.append(basenames.pop() if len(basenames) == 1 else keys[0])
    return names


def format_known_repos(target_repo_paths: Mapping[str, str]) -> str:
    """The known names as one comma-separated phrase for a person to read."""
    names = known_repo_names(target_repo_paths)
    if not names:
        return "nothing yet — no repositories are configured"
    return ", ".join(names)


def unknown_repo_message(name: str, target_repo_paths: Mapping[str, str]) -> str:
    """The one plain sentence a person gets when the name is not known."""
    return (
        f"I don't know a repository called {name}. "
        f"I can build in: {format_known_repos(target_repo_paths)}."
    )


def resolve_target_repo(
    name: str, target_repo_paths: Mapping[str, str]
) -> RepoResolution:
    """Resolve a typed name against the configured repositories (rule 3).

    An exact key wins. Otherwise a name with no slash may match keys by
    their short half: if every match points at one checkout it resolves to
    the first of those keys; if the matches point at different checkouts the
    name is genuinely ambiguous and is refused.
    """
    if name in target_repo_paths:
        return RepoResolution(name=name)

    if "/" in name:
        return RepoResolution(
            name=None,
            reason=f"unknown target repository {name}",
        )

    matches = [key for key in target_repo_paths if _basename(key) == name]
    if not matches:
        return RepoResolution(
            name=None,
            reason=f"unknown target repository {name}",
        )

    paths = {str(target_repo_paths[key]) for key in matches}
    if len(paths) == 1:
        return RepoResolution(name=matches[0])

    return RepoResolution(
        name=None,
        reason=(
            f"ambiguous target repository {name}: "
            f"{', '.join(matches)} are different checkouts"
        ),
    )
