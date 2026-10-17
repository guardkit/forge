"""The nightly security sweep of every product the factory builds.

Security check design, 17 October 2026, piece 3
(``factory-security-check-2026-10-17``). Rich's
words: "yes to automatically drafting a security repair, please proceed as
per your recommendation".

Once a night, when the factory is idle, the coordinator fetches each project
it builds from GitHub into its own mirror, scans the default branch with
three scanners from the release image (OSV-Scanner for dependencies, Gitleaks
for secrets, Opengrep for risky code), has the factory's own model sort the
secret and code findings, and files the serious ones as one ordinary repair
request per project. That request reaches Rich as a normal spec card and the
factory's own local models do the repair. One Slack line says what happened.

Why it lives in the coordinator rather than a new service: the coordinator
already has the list of projects (``planning.fetch_addresses``), network
access to GitHub, the work queue a repair is filed into, and the bus route to
Slack. A separate service would need its own copies of the bus password, the
Slack token and the ledger.

What it does not do: it does not scan running applications, does not sweep
products the factory does not fetch from GitHub, and never fixes anything
itself. Its model answer only decides what is offered to Rich; it cannot
merge, deploy or approve anything.

Modules:

- :mod:`.findings` — what a finding holds, its fingerprint, what is serious;
- :mod:`.scanners` — the three scanners, their arguments, what is kept;
- :mod:`.repository` — the mirror fetch and the export from the object store;
- :mod:`.sorting` — the model question and the redaction boundary;
- :mod:`.drafting` — the repair request, at most once per finding;
- :mod:`.state` — the sweep's folder: state, history, key and lock;
- :mod:`.sweep` — one run, and its Slack line;
- :mod:`.schedule` — when it runs, and how ``forge serve`` starts it.
"""

from __future__ import annotations

__all__: list[str] = []
