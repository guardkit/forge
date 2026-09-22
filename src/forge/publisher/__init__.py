"""The publisher — the one thing in the factory that may write to a remote.

One-true-copy design pass, item 1: the first revision's item 3 and the second
revision's section D. It is its own small service, in its own process, and
nothing that builds or checks a project is in it. It holds one credential,
read once at start from one named file, and it does one thing: send a named
joined commit of a named project to that project's recorded target branch,
without forcing, after checking the publication record and the commit itself.

It writes nothing to the ledger. It never deploys anything. It refuses
everything else in plain words.

Read :mod:`forge.publisher.service` first; the rest is the credential
(:mod:`forge.publisher.credential`), the settings
(:mod:`forge.publisher.settings`), the read-only record
(:mod:`forge.publisher.the_record`) and the git it does
(:mod:`forge.publisher.git_work`).
"""

from __future__ import annotations

from forge.publisher.service import (
    PUBLISH_ROUTE,
    Answer,
    Publisher,
    PublisherHandler,
    serve,
    the_remote_moved,
)
from forge.publisher.settings import (
    ProjectRoute,
    PublisherSettings,
    SettingsRefused,
    load_settings,
)

__all__ = [
    "PUBLISH_ROUTE",
    "Answer",
    "ProjectRoute",
    "Publisher",
    "PublisherHandler",
    "PublisherSettings",
    "SettingsRefused",
    "load_settings",
    "serve",
    "the_remote_moved",
]
