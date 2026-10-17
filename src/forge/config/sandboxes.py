"""Which repositories have a sandbox, in one place (sandbox first, rule 71).

Rich's rule of 2026-09-07: nothing the factory runs on a repository runs on
the host. A repository that has been given a sandbox carries an entry in
``planning.sandboxes`` naming that sandbox and the two addresses forge reaches
inside it — the deploy sidecar and the build runner. Four places have to ask
the same question of the same mapping: where a build is dispatched, where the
merge word's command runs, where the deploy stage's scripts and the live gate
run, and (rule 88) where the merge-ready gates reader reads and runs. This is
that question, asked once and answered the same way everywhere, rather than
four look-ups that could drift apart.

The default is an empty mapping, so on every estate that has not been given a
sandbox yet :func:`sandbox_for` answers ``None`` for every repository and each
caller keeps the path it has always taken.
"""

from __future__ import annotations

from typing import Any

__all__ = ["sandbox_for", "has_sandboxes", "sandboxes_of", "git_door_for", "use_the_settings_receipts"]


def _sandboxes(config: Any) -> dict[str, Any]:
    """``planning.sandboxes`` as a plain mapping, empty when there is none.

    Never raises: a configuration object of any shape — including the small
    stand-ins tests build — simply has no sandboxes if it has no such field.
    """
    entries = getattr(getattr(config, "planning", None), "sandboxes", None)
    if not entries:
        return {}
    try:
        return dict(entries)
    except (TypeError, ValueError):  # pragma: no cover — a mapping is the model
        return {}


def sandboxes_of(config: Any) -> dict[str, Any]:
    """Every repository that has a sandbox, by its ``org/name`` key.

    The composition that needs to build something once per sandboxed
    repository — the build dispatch registers one middleware per sandbox
    runner — asks for the whole mapping rather than probing repository by
    repository. Empty when there are none.
    """
    return _sandboxes(config)


def has_sandboxes(config: Any) -> bool:
    """Does this configuration give ANY repository a sandbox?

    The cheap question a composition asks once at boot, so that an estate with
    no sandboxes composes exactly what it composed before this lane, with no
    per-call routing at all.
    """
    return bool(_sandboxes(config))


def sandbox_for(config: Any, repo: Any) -> Any | None:
    """This repository's sandbox entry, or ``None`` when it has none.

    ``repo`` is the ``org/name`` key the repository map uses. A blank or
    unknown repository has no sandbox, which is the same answer as an estate
    with no sandboxes at all: the caller takes today's path.
    """
    key = str(repo or "").strip()
    if not key:
        return None
    return _sandboxes(config).get(key)


def git_door_for(config: Any, repo: Any) -> str | None:
    """The one Git door ``repo``'s fetch address may go through, or ``None``.

    One sandbox per project (17 October 2026): the door of the sandbox the
    repository is mapped to when that sandbox has one, otherwise the single
    ``planning.git_door`` of an estate with one sandbox. The same rule as
    :meth:`forge.config.models.PlanningConfig.git_door_for`, asked of a
    configuration of any shape (a stand-in without the fields has no door).
    """
    entry = sandbox_for(config, repo)
    own = getattr(entry, "git_door", None) if entry is not None else None
    return own or getattr(getattr(config, "planning", None), "git_door", None)


def use_the_settings_receipts(config: Any) -> None:
    """Point :func:`forge.receipts.receipts_root_for` at each repository's own
    receipts folder (its sandbox entry's ``receipts_root``, design section
    2.2a), from these settings. A repository without one keeps today's root."""
    from forge.receipts import use_project_receipts

    use_project_receipts({
        repo: str(entry.receipts_root)
        for repo, entry in sandboxes_of(config).items()
        if getattr(entry, "receipts_root", None)
    })
