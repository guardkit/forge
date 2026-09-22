"""The short named list of settings a build is launched with.

Until now a build was launched with ``env=os.environ.copy()``: whatever the
runner's own process happened to be holding, in full. The design pass of
21 September 2026 (item 1, second revision, section D) says plainly why that has
to stop: *"Builds and checks are launched with a short list of named settings,
not a copy of everything. That is worth doing whatever else is decided, and the
list is the one the runner's settings file already names."*

THE LIST IS THE FACTORY'S OWN. Every name on it is something the factory
itself sets or reads (its own binary, its own settings file, its receipts
folder, its bus, its memory service, its model router, its switches) or the
shell's own. Corrected 22 September 2026 after the stage's independent review:
an earlier version said the list "is read from two places that already name
these settings and nothing else"; in truth about half the names come from
GuardKit's own code and appear in no unit or start script. The places that
DO name some of them, and were the starting point:

* the runner's unit and its drop-in on this machine — ``forge``'s
  ``ops/systemd/forge-langgraph-sidecar.service`` and
  ``…service.d/override.conf``, whose own comment says the block exists
  *because* "the runner spawns guardkit with ``env=os.environ.copy()``";
* the start script every repository's sandbox runs —
  ``forge``'s ``src/forge/cli/deploy_templates/sandbox-runner.sh``, shipped
  into each repository as ``deploy/sandbox-runner.sh``, whose own "SETTINGS"
  and "load-bearing settings" sections name the same ones again;
* the runner's other drop-ins on this machine, each of which is an operator
  switch somebody turned on by hand and whose own comment says the file is the
  whole switch. Those are on the list too — see "the switches the owner turned
  on" below, and the fault that put them there.

NOTHING ON THIS LIST BELONGS TO THE PROJECT'S OWN TOOLS (22 September 2026,
after the stage's independent review). An earlier version carried
``UV_CACHE_DIR``, one Python package manager's cache; a project built with
any other tool got nothing, and this is central code that applies to every
project whatever it is written in. What a project's own builds need beyond
this list (a package cache, a toolchain home) is the project's to declare, by
NAME only, in its own ``.guardkit/config.yaml``, read at the commit the work
starts from like its memory name: that is the next stage. Until it lands, a
build gets no project-tool setting from the launch, which is the honest state,
not a hidden one.

STILL TO BE ENUMERATED, and named here rather than quietly assumed: the
installed runner carries a third drop-in, ``…service.d/litellm.conf``
(1 September 2026), whose only line is an ``EnvironmentFile=`` pointing at a
secrets file. The list above was built from the repository's own captured unit
and the sandbox start script, neither of which carries that line, so whatever
NAMES that file supplies to the runner have not been read off. Nobody here
opened it — it is a secrets file, and reading one to write a list is not a
trade worth making. Before this list is relied on in anger somebody with the
right to open it should enumerate its NAMES (never its values) and either add
them with their reasons or say here why they stop at the runner.

WHAT IS NOT ON THE LIST IS NOT PASSED. Not a credential the operator's shell
happens to be carrying, not an agent socket, not a cloud token, not the
coordinator's own ledger. That is the point: what a build can reach is now a
list somebody wrote down, rather than the accident of what the process that
launched it was holding.

NOTHING HERE NAMES A LANGUAGE, A TEST RUNNER, A PROTOCOL, A DATABASE OR A
PRODUCT. Every entry is either the shell's own (where to find programs, where
home is, where scratch files go), the factory's own (which build system, which
harness, which settings file, where receipts go, which bus), the model seat's,
or the memory's. A project says what it is made of in its own
``.guardkit/config.yaml``, and nothing in this file knows or cares.

WHERE THIS MODULE LIVES, and why it is not under ``subagents`` beside the
runner: two different things launch the build system — the long-running build
(``forge.subagents.autobuild_runner``) and the bounded legs
(``forge.adapters.guardkit.run``, the planning stages and the fix journey's two
stages) — and the adapter layer deliberately keeps no dependency on the subagent
layer. One list serves both, so it sits above both.

ADDING ONE. Add the name here with its one-line reason, and nowhere else. A
setting that is not worth a sentence is not worth handing to a build.
"""

from __future__ import annotations

import os
from typing import Mapping

__all__ = [
    "GUARDKIT_MEMORY_PROJECT_ENV",
    "LAUNCH_SETTINGS",
    "SETTINGS_DELIBERATELY_NOT_PASSED",
    "build_launch_env",
    "launch_setting_names",
]


#: The setting that hands a build its memory name on purpose. Forge reads the
#: project's own declaration at the recorded starting commit and sets this; the
#: build system uses the name it was handed (the project's own memory, item 2,
#: 2026-09-21).
GUARDKIT_MEMORY_PROJECT_ENV: str = "GUARDKIT_MEMORY_PROJECT"


#: Every setting a build is launched with, and why each one is there. The order
#: is the order a person would want to read them in, and it is the order they
#: are put into the launch environment, so two launches of the same build are
#: byte-identical.
LAUNCH_SETTINGS: tuple[tuple[str, str], ...] = (
    # --- the shell's own ---------------------------------------------------
    (
        "PATH",
        "where the build finds git, the build system itself and the programs "
        "its project declares; the runner's unit pins an enriched one because "
        "the minimal one has almost nothing on it",
    ),
    (
        "HOME",
        "the build system, uv and git all keep their per-user state under it, "
        "and the sandbox's own start script builds every path it uses from it",
    ),
    (
        "TMPDIR",
        "where a build's scratch files go; left off, a build writes into the "
        "machine's shared temporary folder, which a reboot empties underneath "
        "anything still running",
    ),
    # --- the factory's own -------------------------------------------------
    (
        "FORGE_GUARDKIT_PATH",
        "WHICH build system binary is run — the one in the factory's own venv, "
        "never one that happens to be on somebody's disk",
    ),
    (
        "GUARDKIT_HARNESS",
        "which harness the build system runs its agents under; the unit pins "
        "it so a build never falls through to a different one by accident",
    ),
    (
        "FORGE_CONFIG_PATH",
        "the factory's settings file, which the build system's own legs read "
        "back for the project's registration",
    ),
    (
        "FORGE_RECEIPTS_DIR",
        "where a build's receipts are written, so its evidence lands where the "
        "factory looks for it rather than beside the code",
    ),
    (
        "FORGE_NATS_URL",
        "the bus a build publishes its progress on; without it the build runs "
        "blind and nothing can be shown while it works",
    ),
    # --- the model seat ----------------------------------------------------
    (
        "OPENAI_BASE_URL",
        "the local model seat the build's agents talk to; this is what keeps a "
        "build on this estate's own hardware",
    ),
    (
        "OPENAI_API_KEY",
        "satisfies that client's key check — on this estate a placeholder, not "
        "a secret, and it is never read or logged here",
    ),
    (
        "ANTHROPIC_BASE_URL",
        "the same seat for the build system's other client; it is also what "
        "the build system's own timeout arithmetic reads to decide it is "
        "talking to a local seat, so dropping it would change how long a "
        "build is given",
    ),
    (
        "GUARDKIT_TIMEOUT_MULTIPLIER",
        "the build system's own timeout multiplier; the runner's supervision "
        "mirrors this exact number, so the two must see the same value or the "
        "supervisor pre-empts the build system's own timeout",
    ),
    (
        "GUARDKIT_AUTOBUILD_TASK_TIMEOUT_FLOOR",
        "the build system's per-task timeout floor, mirrored by the same "
        "supervision for the same reason",
    ),
    # --- the switches the owner turned on ----------------------------------
    # These three are a class of their own, and the reason they are named here
    # is a fault this list caused on the day it was written. Each one is a
    # check the build system ships with turned OFF; an operator turns it on by
    # dropping a file beside the runner's unit that sets it, and that file is
    # the WHOLE switch. Before this list existed they reached a build because
    # the launch was a copy of everything — which is exactly what the drop-in's
    # own comment says its ``Environment=`` lines are for. A named list that
    # leaves them out turns them off again silently: no error, no log line, the
    # drop-in still sitting there looking switched on. So a switch an owner
    # turned on travels, and if one is ever to stop travelling it moves to the
    # list below with the sentence that says why.
    (
        "GUARDKIT_ARCH_CONFORMANCE_BLOCKING",
        "The owner turned this on, 31 August 2026: a rule the project itself "
        "declares, broken by freshly written code, is sent back to whatever "
        "wrote it as a fix-this on its next turn instead of being filed as a "
        "note. Unset, the build system adds no such rule at all",
    ),
    (
        "GUARDKIT_ZERO_TEST_BLOCKING",
        "the same shape of switch for work that arrives with no check of its "
        "own: on, that stops the work; off, it is recorded. Not set on this "
        "machine today, and it travels for the same reason the one above does",
    ),
    (
        "GUARDKIT_BOOT_SMOKE_BLOCKING",
        "the same shape of switch for the project's own does-it-start check. "
        "Also unset today, and named here so turning it on is one file and "
        "not a hunt through this list",
    ),
    # --- the memory --------------------------------------------------------
    (
        GUARDKIT_MEMORY_PROJECT_ENV,
        "WHICH MEMORY this build reads and writes — the name the project "
        "declares, read at the commit the work started from and handed over "
        "on purpose, so a stale checkout cannot supply it",
    ),
    (
        "FLEET_MEMORY_ENABLED",
        "whether this build uses memory at all",
    ),
    (
        "FLEET_MEMORY_PG_DSN",
        "where the memory store is; without it the build system falls back to "
        "a code default that is not the fleet store at all",
    ),
    (
        "FLEET_MEMORY_EMBED_URL",
        "where memory's embedder is, so a read is embedded the same way the "
        "corpus was",
    ),
    (
        "FLEET_MEMORY_EMBED_MODEL",
        "which embedding model, for the same reason: a different one silently "
        "mis-matches against the stored corpus",
    ),
    (
        "FLEET_MEMORY_EMBED_DIMS",
        "and its width, for the same reason",
    ),
    (
        "FLEET_MEMORY_NATS_URL",
        "where a build's memory writes are published",
    ),
    (
        "GUARDKIT_NATS_PASSWORD",
        "what lets those writes be published at all; it moves opaquely and is "
        "never read or printed here",
    ),
)


#: Named on purpose so nobody has to wonder whether they were forgotten. Each
#: one WAS in the runner's whole environment and is deliberately not handed to a
#: build.
SETTINGS_DELIBERATELY_NOT_PASSED: tuple[tuple[str, str], ...] = (
    (
        "FORGE_DB_PATH",
        "the coordinator's ledger. A build has no business opening it, and the "
        "design pass (item 1, second revision, section D) turns on the ledger "
        "being something a build cannot write. The sandbox's own start script "
        "already unsets it before it starts anything; this does the same for a "
        "build launched on this side",
    ),
    (
        "FORGE_SIDECAR_IN_SANDBOX",
        "tells the sandbox's helper service which deploy script it may run. It "
        "is the service's setting, not a build's",
    ),
    (
        "FORGE_BUILD_MONITOR",
        "switches the runner's own supervision on and off. It belongs to the "
        "thing doing the supervising, not to the thing being supervised",
    ),
    (
        "FORGE_DEFAULT_REPO",
        "the runner's own fallback for a launch that names no repository. The "
        "runner has already resolved the repository by the time it launches "
        "anything, so a build has no use for it",
    ),
    (
        "everything else",
        "whatever else the process that launched the build happened to be "
        "holding — an operator's shell, an agent socket, a cloud token, a "
        "forwarded credential. None of it is named above, so none of it is "
        "passed",
    ),
)


def launch_setting_names() -> tuple[str, ...]:
    """Just the names, in the order they are set."""
    return tuple(name for name, _ in LAUNCH_SETTINGS)


def build_launch_env(
    *,
    parent: Mapping[str, str] | None = None,
    memory_project: str | None = None,
) -> dict[str, str]:
    """The environment a build is launched with: the named list and nothing else.

    Args:
        parent: The settings to take the named ones from. Defaults to this
            process's own.
        memory_project: The memory name recorded for this build, handed over on
            purpose. ``None`` means nothing was recorded — a build queued by
            hand, or one from before the memory rule — and then the name is NOT
            set at all, so the build system falls back to the project's own
            declaration in the folder it is building, and to memory OFF if
            there is none. It is never filled in with a guess.

    Returns:
        A new dictionary. A name the parent does not have is simply absent: an
        unset setting stays unset rather than becoming an empty string, because
        the two mean different things to almost everything that reads them.
    """
    source: Mapping[str, str] = os.environ if parent is None else parent
    env: dict[str, str] = {}
    for name, _reason in LAUNCH_SETTINGS:
        if name == GUARDKIT_MEMORY_PROJECT_ENV:
            continue  # decided below, from the record, never inherited
        value = source.get(name)
        if value is not None:
            env[name] = str(value)
    if memory_project and str(memory_project).strip():
        env[GUARDKIT_MEMORY_PROJECT_ENV] = str(memory_project).strip()
    return env
