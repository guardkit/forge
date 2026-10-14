"""Recording a project's fetch address in tests whose "remote" is on disk.

Since 14 October 2026 (Forge git hardening) the factory fetches a project's
starting point only from the address recorded for it in the coordinator's
settings (``planning.fetch_addresses``), never from the clone's own
``origin``, and production takes only plain ``https://`` addresses. Most
tests make their "remote" a bare repository in ``tmp_path``. They keep doing
that, and say so explicitly:

* :func:`allow_local_remotes` lets an absolute local folder count as a fetch
  address for the test's duration (the ``file`` transport, besides
  ``https``), and keeps the factory's own fetch copies inside ``tmp_path``;
* :func:`origin_of` reads the address the test's clone was given, so the test
  can record it as the project's fetch address (in the config's
  ``planning.fetch_addresses``, or as the ``fetch_address`` of the git surface
  it builds by hand).

Nothing here reads the clone's origin on the factory's behalf: the test
records the address, as ``forge register-repo`` does in production.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

#: The transports a test may record: production's ``https``, and a local
#: folder for a bare repository in ``tmp_path``.
LOCAL_TRANSPORTS: tuple[str, ...] = ("https", "file")


def allow_local_remotes(monkeypatch: pytest.MonkeyPatch, copies: Path) -> None:
    """Let a local folder be a recorded fetch address, and keep the factory's
    own fetch copies under ``copies``."""
    from forge import network_git

    monkeypatch.setattr(network_git, "FETCH_TRANSPORTS", LOCAL_TRANSPORTS)
    monkeypatch.setenv(network_git.FETCH_COPIES_ENV, str(copies))


def origin_of(repo: Path | str) -> str:
    """The address the test gave its clone as ``origin`` (test set-up only)."""
    done = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture()
def local_remotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """:func:`allow_local_remotes` for one test; answers the copies folder."""
    copies = tmp_path / "factory-fetch-copies"
    allow_local_remotes(monkeypatch, copies)
    return copies
