"""``forge unregister-repo`` — take a registration out of the factory.

The inverse of ``forge register-repo`` (Linear front door design, section
11.6, 18 October 2026: the old test registrations are removed before every
registered repository gets a Linear project). It removes what register-repo
wrote for one repository, through register-repo's own stores, staging and
printed stop/start sequence, and nothing else:

* the coordinator's ``forge.yaml`` (in its settings volume): the
  ``planning.sandboxes`` entry (with its memory grant, when it has one), the
  ``planning.target_repo_paths`` key, the ``planning.fetch_addresses`` key, and
  the allowlist path, unless another repository's path is the same;
* the sandbox's own ``forge.yaml``: the same entries with its own paths;
* the publisher's settings file, when it is named: the repository's route.

Each file is edited by deleting lines, so every other line, comments
included, is kept; a key register-repo had to create (``fetch_addresses:``,
say) goes too once nothing is left under it. Each changed file is staged
beside the live one as ``<file>.<leaf>-unregister-pending`` with a dated backup
``<file>.bak-<date>-pre-unregister-<leaf>``, exactly as register-repo stages,
and the printed sequence makes it live. Nothing live is changed and nothing is
restarted.

Left as it is, and said: the sandbox itself (it is never made, stopped or
removed), the factory's clone of the project in the shared sandbox, a sandbox
of its own's settings file and runtime folders, and the estate env file's
memory door and Git door lines, which register-repo printed and never wrote
(the lines to take out are printed).

Refused, with one sentence and nothing changed: a repository the coordinator
has no ``planning.sandboxes`` entry for (it is not registered), and the last
registration on the shared sandbox, which a later registration there copies
(Codex design review R9).
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

import click
import yaml

from forge.cli.register_repo import (
    DEFAULT_COORDINATOR_CONTAINER,
    DEFAULT_SETTINGS_VOLUME,
    DEFAULT_SETTLE_SECONDS,
    ESTATE_ENV_PUBLISHER_SETTINGS,
    ESTATE_ENV_SANDBOX_FILE,
    SANDBOX_ENV_CONFIG_PATH,
    SANDBOX_SETTINGS_IN_CLONE,
    SETTINGS_FILE_NAME,
    _SSH_HOST,
    Estate,
    HostStore,
    JsonEdit,
    Refused,
    SandboxStore,
    Staged,
    Step,
    VolumeStore,
    YamlEdit,
    YamlEditRefused,
    _backup_name,
    _emit,
    _is_ignorable,
    _keeps_every_line,
    _loads_as_forge_settings,
    _mapping,
    _parse_name,
    _pending_name,
    _port_of,
    _resolve_beside,
    _scalar,
    activation_sequence,
    estate_names,
    locate,
    read_env_file,
    stage,
)
from forge.cli import register_repo as _register_repo
from forge.config.sandbox_slots import LEGACY_SANDBOX, MAX_SLOT, SlotPorts, slot_ports

__all__ = ["unregister_repo_cmd"]

#: The name the staged copies and backups carry.
ACTION: str = "unregister"


# ---------------------------------------------------------------------------
# Line deletion (the inverse of register-repo's line insertion)
# ---------------------------------------------------------------------------


def _locate_key(lines: Sequence[str], parent: Sequence[str], key: str):
    """The block of ``key`` under ``parent``, however the key is quoted."""
    for form in dict.fromkeys((_scalar(key), key, json.dumps(key), f"'{key}'")):
        found = locate(lines, (*parent, form))
        if found is not None:
            return found
    return None


def _remove_entry(lines: list[str], parent: Sequence[str], key: str) -> bool:
    """Delete ``key:`` and its content lines; the comments after them stay."""
    block = _locate_key(lines, parent, key)
    if block is None:
        return False
    end = block.key_line + 1 if block.last_content_child is None else block.last_content_child + 1
    del lines[block.key_line:end]
    return True


def _remove_item(lines: list[str], path: Sequence[str], value: str) -> bool:
    """Delete the first ``- value`` line of the block sequence at ``path``."""
    block = locate(lines, path)
    if block is None:
        return False
    if block.inline_value:
        raise YamlEditRefused(
            f"{'.'.join(path)} is written on one line, so this command cannot take "
            "an item out of it safely — edit the file by hand"
        )
    for index in range(block.key_line + 1, block.block_end):
        stripped = lines[index].strip()
        if _is_ignorable(lines[index]) or not stripped.startswith("- "):
            continue
        try:
            item = yaml.safe_load(stripped[2:])
        except yaml.YAMLError:
            continue
        if item == value:
            del lines[index]
            return True
    return False


def _drop_if_empty(lines: list[str], expected: dict[str, Any], path: Sequence[str]) -> None:
    """Delete each key along ``path``, deepest first, that now holds nothing:
    register-repo created it when it was missing."""
    for depth in range(len(path), 0, -1):
        block = locate(lines, path[:depth])
        if block is None or block.inline_value or block.last_content_child is not None:
            return
        del lines[block.key_line]
        parent = expected
        for name in path[: depth - 1]:
            parent = parent[name]
        parent.pop(path[depth - 1], None)


def _on_shared_sandbox(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("name") == LEGACY_SANDBOX


def plan_settings_removal(
    text: str, *, which: str, key: str, require_registered: bool = False, must_load: bool = True
) -> YamlEdit:
    """What taking ``key`` out of one ``forge.yaml`` deletes, line by line.

    The returned edit's ``reference_entry`` is the repository's sandbox entry
    as the file has it (``{}`` when it has none). Refuses, writing nothing,
    when the repository is not registered (``require_registered``), when it
    is the last registration on the shared sandbox, when the result would not
    load, would change anything but the repository's own entries, or would
    add a line. ``must_load=False`` is for a sandbox of its own's settings
    file: without its one project it is as the sandbox recipe left it, which
    names no permissions and so does not load on its own (nothing reads it
    until a project is registered).
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise Refused(which, f"its forge.yaml is not readable YAML ({exc.__class__.__name__})") from None
    if not isinstance(data, dict):
        raise Refused(which, "its forge.yaml is not a set of settings")
    planning = _mapping(data.get("planning"))
    sandboxes = _mapping(planning.get("sandboxes"))
    repo_paths = _mapping(planning.get("target_repo_paths"))
    fetch = _mapping(planning.get("fetch_addresses"))
    allowlist = [str(i) for i in (_mapping(_mapping(data.get("permissions")).get("filesystem")).get("allowlist") or [])]
    entry = sandboxes.get(key)
    if require_registered and entry is None:
        raise Refused(
            "registered",
            f"{key} is not registered with the factory: the coordinator's settings have no "
            "planning.sandboxes entry for it. Nothing was changed",
        )
    if _on_shared_sandbox(entry) and not any(
        _on_shared_sandbox(other) for name, other in sandboxes.items() if name != key
    ):
        raise Refused(
            which,
            f"{key} is the last repository registered on the shared sandbox {LEGACY_SANDBOX}, "
            "and registering another project there copies the entry of one already on it, "
            "so it stays registered. Nothing was changed",
        )

    crlf = "\n" in text and text.count("\r\n") == text.count("\n")
    plain = text.replace("\r\n", "\n") if crlf else text
    lines = plain.split("\n")
    expected = copy.deepcopy(data)
    steps: list[Step] = []
    try:
        if entry is not None and _remove_entry(lines, ("planning", "sandboxes"), key):
            del expected["planning"]["sandboxes"][key]
            said = f"sandboxes {key} (sandbox {entry.get('name') if isinstance(entry, dict) else '?'})"
            granted = entry.get("memory_projects") if isinstance(entry, dict) else None
            if granted:
                said += f", with its memory grant {', '.join(str(g) for g in granted)}"
            steps.append(Step(which, "remove", said))
            _drop_if_empty(lines, expected, ("planning", "sandboxes"))
        path = repo_paths.get(key)
        if path is not None and _remove_entry(lines, ("planning", "target_repo_paths"), key):
            del expected["planning"]["target_repo_paths"][key]
            steps.append(Step(which, "remove", f"target_repo_paths {key} -> {path}"))
            _drop_if_empty(lines, expected, ("planning", "target_repo_paths"))
            users = sorted(k for k, v in repo_paths.items() if k != key and v == path)
            if path in allowlist and users:
                steps.append(Step(which, "keep", f"allowlist {path}: {', '.join(users)} still uses it"))
            elif path in allowlist and _remove_item(lines, ("permissions", "filesystem", "allowlist"), str(path)):
                expected["permissions"]["filesystem"]["allowlist"].remove(path)
                steps.append(Step(which, "remove", f"allowlist {path}"))
                _drop_if_empty(lines, expected, ("permissions", "filesystem", "allowlist"))
        if key in fetch and _remove_entry(lines, ("planning", "fetch_addresses"), key):
            del expected["planning"]["fetch_addresses"][key]
            steps.append(Step(which, "remove", f"fetch_addresses {key}"))
            _drop_if_empty(lines, expected, ("planning", "fetch_addresses"))
    except YamlEditRefused as exc:
        raise Refused(which, str(exc)) from None

    found = dict(entry) if isinstance(entry, dict) else {}
    if not steps:
        return YamlEdit(which, text, None, [Step(which, "unchanged", f"nothing of {key} in it")], found)
    staged = "\n".join(lines)
    if crlf:
        staged = staged.replace("\n", "\r\n")
    try:
        reread = yaml.safe_load(staged)
    except yaml.YAMLError as exc:
        raise Refused(which, f"the edited forge.yaml would not parse ({exc.__class__.__name__})") from None
    if reread != expected:
        raise Refused(
            which,
            f"the edited forge.yaml would change more than {key}'s own entries; nothing was "
            "written — edit it by hand",
        )
    if not _keeps_every_line(lines, plain.split("\n")):
        raise Refused(which, "the edit would do more than delete lines; nothing was written")
    why_not = _loads_as_forge_settings(staged) if must_load else None
    if why_not is not None:
        raise Refused(which, f"the edited forge.yaml would not load ({why_not}); nothing was written")
    return YamlEdit(which, text, staged, steps, found)


def plan_publisher_removal(text: str, *, key: str) -> JsonEdit:
    """Take the route ``projects[key]`` out, written as register-repo writes."""
    from forge.publisher.settings import load_settings

    try:
        data = json.loads(text)
    except ValueError:
        raise Refused("publisher", "the publisher's settings file is not readable JSON") from None
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict) or key not in projects:
        return JsonEdit(text, None, [Step("publisher", "unchanged", f"it has no route for {key}")])
    route = projects[key]
    data["projects"] = {name: value for name, value in projects.items() if name != key}
    staged = json.dumps(data, indent=2) + "\n"
    with tempfile.TemporaryDirectory(prefix="forge-unregister-") as folder:
        probe = Path(folder) / "settings.json"
        probe.write_text(staged, encoding="utf-8")
        try:
            load_settings(probe)
        except Exception as exc:  # noqa: BLE001 — the loader's own sentence
            raise Refused("publisher", f"the edited publisher settings would not load: {exc}") from None
    source = route.get("source") if isinstance(route, dict) else None
    return JsonEdit(text, staged, [Step("publisher", "remove", f"route {key}" + (f" (from {source})" if source else ""))])


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _slot_of(entry: dict[str, Any]) -> SlotPorts | None:
    """The slot a sandbox of its own's helper port belongs to, or ``None``."""
    port = _port_of(entry.get("sidecar_url"))
    for slot in range(1, MAX_SLOT + 1):
        if port is not None and slot_ports(slot).helper == port:
            return slot_ports(slot)
    return None


def _sandbox_settings_path(
    *, sandbox: str, shared: bool, sandbox_settings: str | None, sandbox_env_file: Path | None,
    sandbox_clone: str | None, estate_values: dict[str, str], estate_env_file: Path | None,
) -> str:
    """The sandbox's own forge.yaml, found the way register-repo finds it."""
    path = sandbox_settings
    if not path and shared:
        if sandbox_env_file is None and estate_values.get(ESTATE_ENV_SANDBOX_FILE) and estate_env_file:
            sandbox_env_file = _resolve_beside(estate_values[ESTATE_ENV_SANDBOX_FILE], estate_env_file)
        if sandbox_env_file is not None:
            try:
                path = read_env_file(sandbox_env_file).get(SANDBOX_ENV_CONFIG_PATH) or None
            except OSError as exc:
                raise Refused("sandbox", f"could not read the sandbox env file {sandbox_env_file} ({exc.strerror})") from None
    if not path and sandbox_clone:
        path = f"{sandbox_clone.rstrip('/')}/{SANDBOX_SETTINGS_IN_CLONE}"
    if not path:
        raise Refused(
            "sandbox",
            f"which file is the sandbox {sandbox}'s own forge.yaml? Pass --sandbox-settings or "
            "--sandbox-clone (or, for the shared sandbox, --sandbox-env-file or --estate-env-file)",
        )
    if not path.startswith("/"):
        raise Refused("sandbox", f"the sandbox's settings path {path!r} is not a full path")
    return path


def unregister(
    *,
    name: str,
    dry_run: bool,
    estate_env_file: Path | None,
    sandbox_env_file: Path | None,
    sandbox_settings: str | None,
    sandbox_clone: str | None,
    publisher_settings: Path | None,
    sandbox_ssh: str | None,
    volume_name: str,
    coordinator: str,
    settle_seconds: int,
    steps: list[Step],
) -> list[str]:
    """Every read and check first, then the staged copies; returns the
    printed sequence. Refuses (writing nothing) on any check's no."""
    run = _register_repo.run_command
    org, leaf = _parse_name(name)
    key = f"{org}/{leaf}"
    if sandbox_ssh is not None and not _SSH_HOST.match(sandbox_ssh):
        raise Refused("sandbox", "--sandbox-ssh is not an ssh host (the value is not repeated here)")
    estate_values: dict[str, str] = {}
    if estate_env_file is not None:
        try:
            estate_values = read_env_file(estate_env_file)
        except OSError as exc:
            raise Refused("estate", f"could not read the estate env file {estate_env_file} ({exc.strerror})") from None

    volume = VolumeStore(volume_name, run)
    coordinator_text = volume.read(SETTINGS_FILE_NAME)
    if coordinator_text is None:
        raise Refused("coordinator", f"could not read {SETTINGS_FILE_NAME} from the volume {volume_name}")
    coordinator_edit = plan_settings_removal(coordinator_text, which="coordinator", key=key, require_registered=True)
    entry = coordinator_edit.reference_entry
    sandbox_name = str(entry.get("name") or "")
    shared = sandbox_name == LEGACY_SANDBOX
    slot = None if shared else _slot_of(entry)

    sandbox_path = _sandbox_settings_path(
        sandbox=sandbox_name, shared=shared, sandbox_settings=sandbox_settings,
        sandbox_env_file=sandbox_env_file, sandbox_clone=sandbox_clone,
        estate_values=estate_values, estate_env_file=estate_env_file,
    )
    sandbox = SandboxStore(sandbox_name, run, ssh_host=sandbox_ssh)
    sandbox_text = sandbox.read(sandbox_path)
    if sandbox_text is None:
        raise Refused("sandbox", f"could not read {sandbox_path} in the sandbox {sandbox_name}. Nothing was changed")
    sandbox_edit = plan_settings_removal(sandbox_text, which="sandbox", key=key, must_load=shared)
    sandbox_paths = _mapping(_mapping(_mapping(yaml.safe_load(sandbox_text)).get("planning")).get("target_repo_paths"))

    host = HostStore()
    if publisher_settings is None and estate_values.get(ESTATE_ENV_PUBLISHER_SETTINGS) and estate_env_file:
        publisher_settings = _resolve_beside(estate_values[ESTATE_ENV_PUBLISHER_SETTINGS], estate_env_file)
    publisher_edit: JsonEdit | None = None
    if publisher_settings is not None:
        publisher_text = host.read(str(publisher_settings))
        if publisher_text is None:
            raise Refused("publisher", f"could not read the publisher's settings file {publisher_settings}")
        publisher_edit = plan_publisher_removal(publisher_text, key=key)

    # Every read is done and nothing refused: now stage.
    staged: dict[str, Staged | None] = {"coordinator": None, "sandbox": None, "publisher": None}
    plans: list[tuple[Any, Any, str, str | None, str | None]] = [
        (coordinator_edit, volume, SETTINGS_FILE_NAME, None, None),
        (sandbox_edit, sandbox, sandbox_path, str(PurePosixPath(sandbox_path).parent), None),
    ]
    if publisher_edit is not None:
        live = str(publisher_settings)
        plans.append((publisher_edit, host, live, str(Path(live).parent), live))
    for edit, store, live, folder, like in plans:
        which = getattr(edit, "which", "publisher")
        steps += edit.steps
        if edit.staged is None:
            continue
        line, where = stage(
            store, which=which, live=live, folder=folder, leaf=leaf, original=edit.original,
            staged=edit.staged, dry_run=dry_run, like=like, action=ACTION,
        )
        steps.append(line)
        staged[which] = where or Staged(
            live, _pending_name(live, leaf, ACTION), _backup_name(live, leaf, action=ACTION)
        )
    if publisher_edit is None:
        steps.append(Step(
            "publisher", "not looked at",
            "no publisher settings file was named; pass --publisher-settings or --estate-env-file "
            "to take out its route too",
        ))

    # What this command leaves as it is, said.
    clone = sandbox_paths.get(key)
    if shared:
        steps.append(Step(
            "sandbox", "left",
            f"the shared sandbox {sandbox_name} itself"
            + (f", and the factory's clone {clone} in it (nothing reads it once the registration is out)" if clone else ""),
        ))
    else:
        steps.append(Step(
            "sandbox", "left",
            f"the sandbox {sandbox_name} itself, its workspace clone and its runtime folders",
        ))
    if slot is not None and entry.get("memory_projects"):
        names = ", ".join(f"{slot.memory_door}:{m}" for m in entry["memory_projects"])
        steps.append(Step(
            "memory-door", "note",
            f"take {names} out of the estate env file's FLEET_MEMORY_SANDBOX_DOORS (and "
            f"{slot.memory_door} out of FLEET_MEMORY_SANDBOX_DOOR_PORTS) and restart the memory "
            "service; register-repo printed that line and never wrote it, so neither does this",
        ))
    fetch = _mapping(_mapping(_mapping(yaml.safe_load(coordinator_text)).get("planning")).get("fetch_addresses"))
    door = entry.get("git_door")
    if slot is not None and door and str(fetch.get(key, "")).startswith(str(door)):
        steps.append(Step(
            "git-door", "note",
            f"take the line {slot.git_door}/{key} out of the file the estate env file's "
            f"GIT_DOOR_REPOS_FILE names (and {slot.git_door} out of GIT_DOOR_PORTS) and restart "
            "the Git door; this command does not edit the estate's files",
        ))
    default = _mapping(_mapping(yaml.safe_load(coordinator_text)).get("planning")).get("default_target_repo")
    if default == key:
        steps.append(Step(
            "coordinator", "note",
            f"planning.default_target_repo still names {key}, so a request naming no repository "
            "will be refused until it names another; change that line by hand",
        ))

    env_text, estate_check = estate_names(estate_values, estate_env_file)
    urls = (str(entry.get("sidecar_url", "")), str(entry.get("runner_url", "")))
    return activation_sequence(
        key=key,
        estate=Estate(
            env_file=env_text, estate_check=estate_check, sandbox=sandbox_name, volume=volume_name,
            coordinator=coordinator, settle_seconds=settle_seconds, slot=slot, sandbox_ssh=sandbox_ssh,
        ),
        coordinator=staged["coordinator"],
        sandbox_file=staged["sandbox"],
        publisher=staged["publisher"],
        helper_urls=urls if all(u and "${" not in u for u in urls) else None,
        dry_run=dry_run,
        removing=True,
    )


@click.command(name="unregister-repo")
@click.argument("name", required=False)
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Say what would be taken out of each file; write nothing anywhere.")
@click.option("--estate-env-file", type=click.Path(dir_okay=False, path_type=Path),
              envvar="FORGE_ESTATE_ENV_FILE", default=None,
              help="The estate env file the factory runs on now; names the sandbox's and the "
                   "publisher's settings files and is used in the printed commands.")
@click.option("--sandbox-env-file", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help=f"The shared sandbox's bootstrap env file (default: the estate env's {ESTATE_ENV_SANDBOX_FILE}).")
@click.option("--sandbox-settings", default=None,
              help=f"The sandbox's own forge.yaml (default: the bootstrap env's {SANDBOX_ENV_CONFIG_PATH}).")
@click.option("--sandbox-clone", default=None,
              help="The sandbox's workspace clone; its settings file is in its runtime folder.")
@click.option("--publisher-settings", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help=f"The publisher's settings file (default: the estate env's {ESTATE_ENV_PUBLISHER_SETTINGS}).")
@click.option("--sandbox-ssh", "sandbox_ssh", default=None, envvar="FORGE_SANDBOX_SSH",
              help="The sandbox's machine, when it is not this one: sbx commands go there over ssh.")
@click.option("--settle-seconds", type=click.IntRange(min=0), default=DEFAULT_SETTLE_SECONDS,
              show_default=True, help="How far apart the printed drained check's two looks are.")
@click.option("--settings-volume", "volume", default=DEFAULT_SETTINGS_VOLUME, show_default=True,
              help="The coordinator's settings volume.")
@click.option("--coordinator-container", "coordinator", default=DEFAULT_COORDINATOR_CONTAINER,
              show_default=True, help="The running coordinator's container.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print the report as JSON.")
def unregister_repo_cmd(
    name: str | None,
    dry_run: bool,
    estate_env_file: Path | None,
    sandbox_env_file: Path | None,
    sandbox_settings: str | None,
    sandbox_clone: str | None,
    publisher_settings: Path | None,
    sandbox_ssh: str | None,
    settle_seconds: int,
    volume: str,
    coordinator: str,
    as_json: bool,
) -> None:
    """Take a repository's registration out of the factory (the inverse of register-repo).

    Stages the coordinator's, the sandbox's and (when named) the publisher's
    settings without the repository's entries, beside the live ones, and
    prints the stop/start sequence that makes it live. It restarts nothing and
    leaves the sandbox itself alone.
    """
    steps: list[Step] = []
    try:
        if not name:
            raise Refused("name", "name the repository: forge unregister-repo <org>/<name>")
        tail = unregister(
            name=name, dry_run=dry_run, estate_env_file=estate_env_file,
            sandbox_env_file=sandbox_env_file, sandbox_settings=sandbox_settings,
            sandbox_clone=sandbox_clone, publisher_settings=publisher_settings,
            sandbox_ssh=sandbox_ssh, volume_name=volume, coordinator=coordinator,
            settle_seconds=settle_seconds, steps=steps,
        )
    except Refused as refusal:
        steps.append(Step(refusal.step, "refused", refusal.detail))
        _emit(steps, as_json=as_json)
        raise click.exceptions.Exit(1) from None
    _emit(steps, as_json=as_json, tail=tail)
