"""``forge register-repo --slot <k>`` (17 October 2026).

Design "One sandbox per project", sections 2.2, 2.4, 2.5 and 2.6: a project
is given a sandbox of its own. The sandbox is made from the project's own
clone, so its workspace clone is the factory's clone and nothing is cloned;
the ``planning.sandboxes`` entry names that sandbox, the helper and runner at
the slot's ports and the sandbox's own Git door port; the publisher fetches
from the sandbox's own git daemon port; a sandbox takes one project; and the
shared sandbox ``api-test-deploy`` never gets a slot or a Git door.

The estate is the stand-in the main register-repo tests use
(:class:`~tests.forge.cli.test_register_repo.FakeEstate`): nothing here
reaches git, docker, sbx or GitHub, and every name and address is made up.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess

import pytest
import yaml
from click.testing import CliRunner

from forge.cli import register_repo
from forge.cli.main import main
from forge.config.sandbox_slots import slot_ports
from tests.forge.cli.test_register_repo import (  # noqa: F401
    COORDINATOR_YAML,
    GOOD_CONFIG,
    KEY,
    LEAF,
    URL,
    estate,
    publisher_file,
    route_key_estate,
)

OWN = LEAF
WORKSPACE = f"/workspace/{LEAF}"
OWN_SETTINGS = f"{WORKSPACE}/.guardkit/tmp/factory-runtime/forge.yaml"
GATEWAY = "192.0.2.1"
SANDBOX_ADDRESS = "192.0.2.10"
DOOR = f"http://{GATEWAY}:31831/"
DOOR_ADDRESS = f"{DOOR}{KEY}.git"

#: The new sandbox's own settings file, as the recipe leaves it: nothing of
#: any project in it yet.
OWN_YAML = """\
# THE NEW SANDBOX'S OWN SETTINGS FILE (fixture): paths inside the sandbox.
planning:
  enabled: true
"""

ENTRY = {
    "name": OWN,
    "sidecar_url": f"http://{SANDBOX_ADDRESS}:8935",
    "runner_url": f"http://{SANDBOX_ADDRESS}:8934",
    "git_door": DOOR,
}
#: The project's own receipts folder, as the coordinator sees it (design
#: section 2.2a): in the coordinator's entry only.
RECEIPTS = f"/var/lib/forge-project-receipts/{LEAF}"
#: And the memory name the project declares (GOOD_CONFIG: bench_one), granted
#: to it in the coordinator's entry only (design section 2.4a).
COORDINATOR_ENTRY = {**ENTRY, "receipts_root": RECEIPTS, "memory_projects": ["bench_one"]}


@pytest.fixture
def own(estate):  # noqa: F811
    estate.sandbox_name = OWN
    estate.clone_root = WORKSPACE
    estate.sandbox_files = {OWN_SETTINGS: OWN_YAML}
    estate.modes[OWN_SETTINGS] = "600"
    return estate


def _run_own(*args: str, slot: str = "1"):
    return CliRunner().invoke(
        main,
        ["register-repo", KEY, "--github", URL, "--slot", slot,
         "--sandbox-clone", WORKSPACE, "--gateway-address", GATEWAY,
         "--sandbox-address", SANDBOX_ADDRESS, *args],
        catch_exceptions=False,
    )


def _with_other_project(estate, entry: dict[str, str]) -> None:  # noqa: F811
    lines = "".join(f"      {k}: {v}\n" for k, v in entry.items())
    estate.volume["forge.yaml"] = COORDINATOR_YAML.replace(
        "    checkouts/api_test: /var/lib/forge/projects/api_test\n",
        "    checkouts/api_test: /var/lib/forge/projects/api_test\n"
        "    guardkit/other-project: /var/lib/forge/projects/other-project\n",
    ).replace(
        "    # A note after the last sandbox.\n",
        f"    guardkit/other-project:\n{lines}    # A note after the last sandbox.\n",
    )


# ---------------------------------------------------------------------------
# The slot scheme


def test_the_slot_scheme_is_base_plus_ten_per_slot_and_one_door_port_each():
    one = slot_ports(1)
    assert (one.runner, one.helper, one.git_daemon, one.previews) == (8934, 8935, 8928, (8960, 8961))
    assert (one.git_door, one.memory_door) == (31831, 31851)
    last = slot_ports(19)
    assert (last.runner, last.git_door, last.memory_door) == (9114, 31849, 31869)
    every = [p for k in range(1, 20) for p in (
        slot_ports(k).runner, slot_ports(k).helper, slot_ports(k).git_daemon, *slot_ports(k).previews,
        slot_ports(k).git_door, slot_ports(k).memory_door)]
    assert len(every) == len(set(every)), "no two slots, and no two ports of one slot, share a port"


@pytest.mark.parametrize("slot", [0, 20, -1, True])
def test_a_slot_outside_one_to_nineteen_is_refused(slot):
    with pytest.raises(ValueError, match="1 to 19"):
        slot_ports(slot)


# ---------------------------------------------------------------------------
# A sandbox of its own


def test_the_project_gets_its_own_sandbox_entry_from_its_slot(own):
    result = _run_own()
    assert result.exit_code == 0, result.output

    coordinator = yaml.safe_load(own.volume[f"forge.yaml.{LEAF}-pending"])
    assert coordinator["planning"]["sandboxes"][KEY] == COORDINATOR_ENTRY
    assert coordinator["planning"]["sandboxes"]["guardkit/api_test"]["name"] == "api-test-deploy"
    assert coordinator["planning"]["fetch_addresses"] == {KEY: URL}
    assert coordinator["planning"]["target_repo_paths"][KEY] == f"/var/lib/forge/projects/{LEAF}"

    sandbox = yaml.safe_load(own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"])
    assert sandbox["planning"]["sandboxes"] == {KEY: ENTRY}
    assert sandbox["planning"]["target_repo_paths"] == {KEY: WORKSPACE}
    assert sandbox["permissions"]["filesystem"]["allowlist"] == [WORKSPACE]
    assert own.sandbox_files[OWN_SETTINGS] == OWN_YAML, "the live file is left alone"

    assert own.clones == {}, "the sandbox's workspace clone is the project's clone; nothing is cloned"
    assert not [c for c in own.calls if c[:2] == ["sbx", "exec"] and "clone" in c]
    assert "workspace clone" in result.output
    assert "Slot 1" in result.output and "8934" in result.output and "31831" in result.output
    assert "curl -sf http://192.0.2.10:8935/healthz" in result.output


def test_the_publisher_fetches_from_the_sandboxs_own_git_daemon_port(own, publisher_file):
    result = _run_own("--publish", "--publisher-settings", str(publisher_file))
    assert result.exit_code == 0, result.output
    staged = json.loads((publisher_file.parent / f"settings.json.{LEAF}-pending").read_text())
    assert staged["projects"][KEY] == {
        "source": f"git://{SANDBOX_ADDRESS}:8928/{LEAF}",
        "remote": f"git@github.com:{KEY}.git",
    }
    assert staged["projects"]["guardkit/api_test"]["source"] == "git://192.0.2.10:8918/api_test"


def test_through_its_own_door_the_address_and_the_read_use_the_sandboxs_port(own):
    result = _run_own("--via-git-door")
    assert result.exit_code == 0, result.output
    coordinator = yaml.safe_load(own.volume[f"forge.yaml.{LEAF}-pending"])
    assert coordinator["planning"]["fetch_addresses"] == {KEY: DOOR_ADDRESS}
    assert "git_door" not in coordinator["planning"], "the single door stays unset"
    reads = [c for c in own.calls if c[:2] == ["sbx", "exec"] and "ls-remote" in c]
    assert reads and all(DOOR_ADDRESS in call for call in reads)
    assert all("GIT_ALLOW_PROTOCOL=http" in call for call in reads)
    assert f"31831/{KEY}" in result.output and "GIT_DOOR_PORTS" in result.output


def test_a_repository_its_door_port_will_not_read_is_refused_with_the_allow_list_line(own):
    own.sandbox_can_read = False
    result = _run_own("--via-git-door")
    assert result.exit_code == 1
    assert "through its Git door port" in result.output and f"31831/{KEY}" in result.output
    assert own.writes == []


def test_running_again_changes_nothing(own):
    assert _run_own().exit_code == 0
    own.volume["forge.yaml"] = own.volume[f"forge.yaml.{LEAF}-pending"]
    own.sandbox_files[OWN_SETTINGS] = own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"]
    again = _run_own()
    assert again.exit_code == 0, again.output
    assert "nothing is staged" in again.output


# ---------------------------------------------------------------------------
# One project per sandbox, one sandbox per slot


def test_a_second_project_is_refused_in_a_sandbox_that_already_has_one(own):
    _with_other_project(own, {**ENTRY, "sidecar_url": "http://192.0.2.10:8945",
                              "runner_url": "http://192.0.2.10:8944",
                              "git_door": "http://192.0.2.1:31832/"})
    result = _run_own()
    assert result.exit_code == 1
    assert f"{OWN} already belongs to guardkit/other-project" in result.output
    assert own.writes == []


@pytest.mark.parametrize("field, value", [
    ("sidecar_url", "http://192.0.2.10:8935"),
    ("runner_url", "http://192.0.2.10:8934"),
    ("git_door", DOOR),
    ("receipts_root", RECEIPTS),
    # By port, whatever the address is written as.
    ("sidecar_url", "http://sandbox-machine.invalid:8935/"),
    ("runner_url", "http://192.0.2.99:8935"),
    ("git_door", "http://192.0.2.77:31831/"),
])
def test_a_slot_another_sandbox_has_is_refused(own, field, value):
    other = {"name": "other-sbx", "sidecar_url": "http://192.0.2.10:8945",
             "runner_url": "http://192.0.2.10:8944", "git_door": "http://192.0.2.1:31832/",
             "receipts_root": "/var/lib/forge-project-receipts/other"}
    other[field] = value
    _with_other_project(own, other)
    result = _run_own()
    assert result.exit_code == 1
    assert "pick a slot no sandbox has" in result.output
    assert own.writes == []


def test_the_receipts_folder_defaults_to_the_coordinator_s_mount_and_can_be_named(own):
    named = _run_own("--receipts-root", "/srv/receipts/bench-one", "--dry-run")
    assert named.exit_code == 0, named.output
    assert "receipts_root" not in yaml.safe_load(OWN_YAML)["planning"]
    result = _run_own()
    assert result.exit_code == 0, result.output
    coordinator = yaml.safe_load(own.volume[f"forge.yaml.{LEAF}-pending"])
    assert coordinator["planning"]["sandboxes"][KEY]["receipts_root"] == (
        "/var/lib/forge-project-receipts/bench-one"
    )
    assert "receipts_root" not in coordinator["planning"]["sandboxes"]["guardkit/api_test"]


@pytest.mark.parametrize("receipts", ["relative/folder", "/var/lib/../etc"])
def test_a_sandbox_of_its_own_needs_its_own_receipts_folder(own, receipts):
    args = ["register-repo", KEY, "--github", URL, "--slot", "1", "--sandbox-clone", WORKSPACE,
            "--gateway-address", GATEWAY]
    if receipts is not None:
        args += ["--receipts-root", receipts]
    result = CliRunner().invoke(main, args, catch_exceptions=False)
    assert result.exit_code == 1 and "--receipts-root" in result.output
    assert own.calls == []


def test_the_shared_sandbox_takes_no_receipts_folder_of_its_own(estate):  # noqa: F811
    result = CliRunner().invoke(main, [
        "register-repo", KEY, "--github", URL, "--receipts-root", "/var/lib/forge-receipts/x",
        "--sandbox-settings", "/workspace/api_test/.guardkit/tmp/factory-runtime/forge.yaml",
    ], catch_exceptions=False)
    assert result.exit_code == 1 and "single receipts folder" in result.output


def test_a_sandbox_of_its_own_needs_a_slot(own):
    result = CliRunner().invoke(main, [
        "register-repo", KEY, "--github", URL, "--sandbox", "notes-sbx",
        "--sandbox-settings", OWN_SETTINGS, "--gateway-address", GATEWAY,
    ], catch_exceptions=False)
    assert result.exit_code == 1 and "--slot" in result.output
    assert own.calls == []


@pytest.mark.parametrize("slot", ["0", "20"])
def test_a_slot_outside_the_scheme_is_refused_before_anything_is_read(own, slot):
    result = _run_own(slot=slot)
    assert result.exit_code == 1 and "1 to 19" in result.output
    assert own.calls == []


def test_the_git_export_port_must_be_the_slots_own(own, publisher_file):
    result = _run_own("--publish", "--publisher-settings", str(publisher_file),
                      "--git-export-port", "8918")
    assert result.exit_code == 1 and "8928" in result.output


def test_a_coordinator_with_the_single_door_cannot_also_have_doors_per_sandbox(own):
    own.volume["forge.yaml"] = COORDINATOR_YAML.replace(
        "planning:\n  enabled: true\n", f"planning:\n  enabled: true\n  git_door: {DOOR}\n"
    )
    result = _run_own()
    assert result.exit_code == 1
    assert "would not load" in result.output
    assert own.writes == []


def test_the_runtime_folder_must_be_git_ignored_in_the_workspace_clone(own):
    own.ignored = False
    result = _run_own()
    assert result.exit_code == 1
    assert ".gitignore" in result.output and own.writes == []


def test_a_workspace_clone_of_another_project_is_refused(own):
    own.clones[WORKSPACE] = "https://github.com/guardkit/another-project.git"
    result = _run_own()
    assert result.exit_code == 1
    assert "made for another project" in result.output and own.writes == []


@pytest.mark.parametrize("origin", [
    f"https://github.com/{KEY}.git", f"git@github.com:{KEY}.git", "/run/sandbox/source",
])
def test_a_workspace_clone_of_this_project_or_of_a_folder_is_taken(own, origin):
    own.clones[WORKSPACE] = origin
    result = _run_own()
    assert result.exit_code == 0, result.output


def test_a_new_sandbox_s_own_settings_file_is_written_by_the_command(own):
    """The recipe makes the sandbox without one (17 October 2026); the
    command writes it at FORGE_CONFIG_PATH's place, owner-only, with the
    project's own entries, and stages nothing for it."""
    own.sandbox_files = {}
    dry = _run_own("--dry-run")
    assert dry.exit_code == 0, dry.output
    assert own.sandbox_files == {} and "would create" in dry.output

    result = _run_own()
    assert result.exit_code == 0, result.output
    assert list(own.sandbox_files) == [OWN_SETTINGS]
    assert own.modes[OWN_SETTINGS] == "600"
    written = yaml.safe_load(own.sandbox_files[OWN_SETTINGS])
    assert written["planning"]["sandboxes"] == {KEY: ENTRY}
    assert written["planning"]["target_repo_paths"] == {KEY: WORKSPACE}
    assert "created" in result.output
    assert f"forge.yaml.{LEAF}-pending" in own.volume, "the coordinator's file is still staged"


RUNTIME_FOLDERS = [f"{WORKSPACE}/.guardkit/tmp/factory-runtime/{name}" for name in ("worktrees", "deploy-notes", "tmp")]


def test_the_runtime_folders_its_settings_name_are_made_with_the_settings_file(own):
    """The S1 window (18 October 2026): the sandbox's start-up failed
    (mktemp) because the worktrees, deploy-notes and tmp folders beside its
    new settings file were never made. They are made with it, owner-only, as
    the sandbox user, and a dry run only says so."""
    own.sandbox_files = {}
    dry = _run_own("--dry-run")
    assert dry.exit_code == 0, dry.output
    assert own.sandbox_folders == {}
    assert "would create" in dry.output and all(folder in dry.output for folder in RUNTIME_FOLDERS)

    result = _run_own()
    assert result.exit_code == 0, result.output
    assert own.sandbox_folders == {folder: "700" for folder in RUNTIME_FOLDERS}
    made = [w for w in own.writes if w.startswith(("sandbox:", "folder:"))]
    assert made == [f"sandbox:{OWN_SETTINGS}", *(f"folder:{folder}" for folder in RUNTIME_FOLDERS)]
    # Through the same sbx exec route, as the sandbox user (the fake checks -u 1000).
    call = next(c for c in own.calls if c[:2] == ["sbx", "exec"] and register_repo._MAKE_FOLDERS in c)
    assert call[-3:] == RUNTIME_FOLDERS
    assert "the runtime folders its start-up uses" in result.output


def test_making_the_runtime_folders_keeps_ones_already_there_and_owned(tmp_path):
    """The folder command is idempotent, leaves each folder 0700, and refuses
    something that is not a folder."""
    folders = [str(tmp_path / "rt" / name) for name in ("worktrees", "deploy-notes", "tmp")]
    for _ in range(2):
        done = subprocess.run(["sh", "-c", register_repo._MAKE_FOLDERS, "register-repo", *folders])
        assert done.returncode == 0
    assert all(stat.S_IMODE(os.stat(folder).st_mode) == 0o700 for folder in folders)
    (tmp_path / "rt" / "file").write_text("")
    refused = subprocess.run(
        ["sh", "-c", register_repo._MAKE_FOLDERS, "register-repo", str(tmp_path / "rt" / "file")],
        capture_output=True,
    )
    assert refused.returncode != 0


def test_a_failure_making_the_runtime_folders_is_refused_and_put_right_by_running_again(own, monkeypatch):
    """Codex review of the S1 window fixes, R3: the settings file is written
    before the folders, so a run that fails at the folders must have them
    made by the next run, which finds the settings file already there."""
    own.sandbox_files = {}
    real = own._sbx_exec
    failing = {"now": True}

    def sometimes_failing(rest, input):
        if register_repo._MAKE_FOLDERS in rest and failing["now"]:
            return subprocess.CompletedProcess(rest, 1, "", "mkdir: permission denied\n")
        return real(rest, input)

    monkeypatch.setattr(own, "_sbx_exec", sometimes_failing)
    result = _run_own()
    assert result.exit_code == 1
    assert "could not make the runtime folders" in result.output
    assert OWN_SETTINGS in own.sandbox_files and own.sandbox_folders == {}

    failing["now"] = False
    again = _run_own()
    assert again.exit_code == 0, again.output
    assert own.sandbox_folders == {folder: "700" for folder in RUNTIME_FOLDERS}


def test_the_runtime_folders_are_made_sure_of_when_the_settings_file_is_already_there(own):
    """A settings file written before this step existed (the S1 window) gets
    its folders too; a dry run only says so."""
    dry = _run_own("--dry-run")
    assert dry.exit_code == 0, dry.output
    assert own.sandbox_folders == {} and all(folder in dry.output for folder in RUNTIME_FOLDERS)
    result = _run_own()
    assert result.exit_code == 0, result.output
    assert own.sandbox_folders == {folder: "700" for folder in RUNTIME_FOLDERS}


def test_the_shared_sandbox_is_given_no_runtime_folders(estate):  # noqa: F811
    from tests.forge.cli.test_register_repo import _run

    assert _run().exit_code == 0
    assert estate.sandbox_folders == {}
    assert not [c for c in estate.calls if register_repo._MAKE_FOLDERS in c]


def test_a_sandbox_of_its_own_is_named_after_its_project(own):
    result = CliRunner().invoke(main, [
        "register-repo", KEY, "--github", URL, "--sandbox", "another-name", "--slot", "1",
        "--sandbox-clone", WORKSPACE, "--gateway-address", GATEWAY,
    ], catch_exceptions=False)
    assert result.exit_code == 1 and f"named after its project, {LEAF}" in result.output
    assert own.calls == []
    named = _run_own("--sandbox", LEAF)
    assert named.exit_code == 0, named.output


# ---------------------------------------------------------------------------
# The shared sandbox keeps its projects and never gets a door


def test_the_shared_sandbox_never_reads_through_the_git_door(estate):  # noqa: F811
    result = CliRunner().invoke(main, [
        "register-repo", KEY, "--github", URL, "--sandbox-settings",
        "/workspace/api_test/.guardkit/tmp/factory-runtime/forge.yaml", "--via-git-door",
    ], catch_exceptions=False)
    assert result.exit_code == 1
    assert "never reads through the Git door" in result.output
    assert estate.calls == [] and estate.writes == []


def test_the_shared_sandbox_has_no_slot(estate):  # noqa: F811
    result = CliRunner().invoke(main, [
        "register-repo", KEY, "--github", URL, "--sandbox", "api-test-deploy", "--slot", "1",
        "--sandbox-settings", "/workspace/api_test/.guardkit/tmp/factory-runtime/forge.yaml",
    ], catch_exceptions=False)
    assert result.exit_code == 1 and "has no slot" in result.output
    assert estate.calls == []


def test_the_shared_sandbox_refuses_a_reference_entry_that_names_a_door(estate):  # noqa: F811
    estate.volume["forge.yaml"] = COORDINATOR_YAML.replace(
        '      runner_url: "${FORGE_SANDBOX_RUNNER_URL}"\n',
        f'      runner_url: "${{FORGE_SANDBOX_RUNNER_URL}}"\n      git_door: {DOOR}\n',
    )
    result = CliRunner().invoke(main, [
        "register-repo", KEY, "--github", URL, "--sandbox-settings",
        "/workspace/api_test/.guardkit/tmp/factory-runtime/forge.yaml",
    ], catch_exceptions=False)
    assert result.exit_code == 1 and "never has one" in result.output
    assert estate.writes == []


def test_the_shared_sandbox_still_takes_a_public_project_into_its_projects_folder(estate):  # noqa: F811
    from tests.forge.cli.test_register_repo import CLONE_PATH, _run

    result = _run()
    assert result.exit_code == 0, result.output
    staged = yaml.safe_load(estate.volume[f"forge.yaml.{LEAF}-pending"])
    assert "git_door" not in staged["planning"]["sandboxes"][KEY]
    assert estate.clones == {CLONE_PATH: URL}


# ---------------------------------------------------------------------------
# Two machines (Codex implementation review 1, R3): the sandbox on the Spark,
# the coordinator's volume and the publisher's file on this machine


SSH = ["ssh", "-o", "BatchMode=yes", "spark-example", "--"]


class _OverSsh:
    """The fake estate behind an ssh hop: a routed command is unwrapped the
    way the remote shell reads it (shlex.split), so the estate sees exactly the
    words that would run on the sandbox's machine."""

    def __init__(self, estate):  # noqa: ANN001
        self.estate = estate
        self.routed: list[list[str]] = []
        self.local: list[list[str]] = []

    def __call__(self, argv, *, input=None, timeout=None):  # noqa: A002
        import shlex

        argv = list(argv)
        if argv[: len(SSH)] == SSH:
            assert len(argv) == len(SSH) + 1, "one quoted command line after the hop"
            words = shlex.split(argv[-1])
            self.routed.append(words)
            return self.estate(words, input=input, timeout=timeout)
        self.local.append(argv)
        return self.estate(argv, input=input, timeout=timeout)


def test_on_two_machines_every_sbx_command_goes_to_the_sandbox_s_machine(own, publisher_file, monkeypatch):
    from forge.cli import register_repo

    hop = _OverSsh(own)
    monkeypatch.setattr(register_repo, "run_command", hop)
    result = _run_own("--via-git-door", "--sandbox-ssh", "spark-example",
                      "--publish", "--publisher-settings", str(publisher_file))
    assert result.exit_code == 0, result.output

    assert hop.routed and all(words[0] == "sbx" for words in hop.routed)
    assert not [c for c in hop.local if "sbx" in c], "no sbx command runs on this machine"
    assert [c for c in hop.local if c[:2] == ["docker", "run"]], "the volume is read here"
    # A write through the hop: the settings file was created on the sandbox side.
    assert OWN_SETTINGS in own.sandbox_files
    # Arguments with spaces and quotes arrive as written.
    assert any("umask 077" in " ".join(words) for words in hop.routed)
    # The publisher's file and the coordinator's volume are staged here.
    assert (publisher_file.parent / f"settings.json.{LEAF}-pending").is_file()
    assert f"forge.yaml.{LEAF}-pending" in own.volume
    assert "ssh -o BatchMode=yes spark-example -- sbx exec" in result.output


def test_the_shared_sandbox_on_the_other_machine_is_reached_the_same_way(estate, monkeypatch):
    from forge.cli import register_repo
    from tests.forge.cli.test_register_repo import _run

    hop = _OverSsh(estate)
    monkeypatch.setattr(register_repo, "run_command", hop)
    result = _run("--sandbox-ssh", "spark-example")
    assert result.exit_code == 0, result.output
    assert hop.routed and not [c for c in hop.local if "sbx" in c]
    assert any(words[:2] == ["sbx", "exec"] and "clone" in words for words in hop.routed)
    assert "ssh -o BatchMode=yes spark-example -- sbx exec -u 1000 api-test-deploy cmp" in result.output


@pytest.mark.parametrize("host", ["-oProxyCommand=x", "a b", "spark;rm", ""])
def test_the_sandbox_s_machine_must_be_an_ssh_host(own, host):
    result = _run_own("--sandbox-ssh", host)
    assert result.exit_code == 1 and "--sandbox-ssh is not an ssh host" in result.output
    assert own.calls == []


# ---------------------------------------------------------------------------
# The memory grant (design section 2.4a, 18 October 2026)


def test_the_coordinator_entry_is_granted_the_declared_memory_name_and_the_door_line_names_it(own):
    result = _run_own()
    assert result.exit_code == 0, result.output
    coordinator = yaml.safe_load(own.volume[f"forge.yaml.{LEAF}-pending"])
    assert coordinator["planning"]["sandboxes"][KEY]["memory_projects"] == ["bench_one"]
    assert "memory_projects" not in coordinator["planning"]["sandboxes"]["guardkit/api_test"]
    sandbox = yaml.safe_load(own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"])
    assert "memory_projects" not in sandbox["planning"]["sandboxes"][KEY], "the coordinator holds the grant"
    # The memory door line says the same name, at slot 1's memory door port.
    assert "FLEET_MEMORY_SANDBOX_DOORS has the entry 31851:bench_one" in result.output
    assert "memory_projects: [bench_one]" in result.output


def test_the_grant_loads_as_the_coordinator_s_settings(own):
    from forge.config.models import SandboxEntry

    assert _run_own().exit_code == 0
    sandboxes = yaml.safe_load(own.volume[f"forge.yaml.{LEAF}-pending"])["planning"]["sandboxes"]
    assert SandboxEntry.model_validate(sandboxes[KEY]).memory_projects == ["bench_one"]


def test_a_project_with_no_declared_memory_name_is_refused(own):
    own.write_project(".guardkit/config.yaml", GOOD_CONFIG.replace("memory:\n  project: bench_one\n", ""))
    result = _run_own()
    assert result.exit_code == 1
    assert "does not say which memory it uses" in result.output
    assert own.writes == []


def test_a_declared_name_the_memory_service_would_refuse_is_refused(own):
    own.write_project(".guardkit/config.yaml", GOOD_CONFIG.replace("project: bench_one", "project: Bench_One"))
    result = _run_own()
    assert result.exit_code == 1
    assert "lower-case letters, digits and underscores" in result.output
    assert own.writes == []


def test_a_sandbox_registered_before_the_grant_is_given_it_and_nothing_else(own):
    assert _run_own().exit_code == 0
    staged = own.volume[f"forge.yaml.{LEAF}-pending"]
    # The entry as a run before 18 October wrote it: no memory_projects line.
    before = "\n".join(line for line in staged.split("\n") if "memory_projects" not in line)
    own.volume["forge.yaml"] = before
    own.sandbox_files[OWN_SETTINGS] = own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"]
    again = _run_own()
    assert again.exit_code == 0, again.output
    after = yaml.safe_load(own.volume[f"forge.yaml.{LEAF}-pending"])
    assert after["planning"]["sandboxes"][KEY] == COORDINATOR_ENTRY
    assert f"sandboxes {KEY} memory_projects bench_one" in again.output
    assert yaml.safe_load(before)["planning"]["sandboxes"][KEY] == {
        k: v for k, v in COORDINATOR_ENTRY.items() if k != "memory_projects"
    }


def test_a_sandbox_granted_another_name_is_refused(own):
    assert _run_own().exit_code == 0
    staged = own.volume[f"forge.yaml.{LEAF}-pending"]
    own.volume["forge.yaml"] = staged.replace("memory_projects: [bench_one]", "memory_projects: [another_one]")
    own.sandbox_files[OWN_SETTINGS] = own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"]
    own.writes.clear()
    again = _run_own()
    assert again.exit_code == 1
    assert "already has an entry" in again.output
    assert own.writes == []


def test_the_shared_sandbox_grants_no_memory(estate):  # noqa: F811
    from tests.forge.cli.test_register_repo import _run

    result = _run()
    assert result.exit_code == 0, result.output
    coordinator = yaml.safe_load(estate.volume[f"forge.yaml.{LEAF}-pending"])
    assert "memory_projects" not in coordinator["planning"]["sandboxes"][KEY]
    assert "FLEET_MEMORY_SANDBOX_DOORS" not in result.output
