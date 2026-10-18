"""``forge unregister-repo`` (Linear front door design, section 11.6, 18 October 2026).

The inverse of register-repo. The estate is register-repo's own stand-in
(:class:`~tests.forge.cli.test_register_repo.FakeEstate`): nothing here
reaches git, docker, sbx or GitHub, and every name and address is made up.
"Activating" a staged copy is done here the way the printed sequence does it:
the staged copy becomes the live file.

Also here, Codex design review R9: a registration on the shared sandbox copies
the entry of any repository already registered there, not api_test's alone,
and the last registration on the shared sandbox cannot be taken out.
"""

from __future__ import annotations

import datetime
import os
import subprocess

import pytest
import yaml
from click.testing import CliRunner

from forge.cli.main import main
from tests.forge.cli.test_register_repo import (  # noqa: F401
    CLONE,
    CLONE_PATH,
    COORDINATOR_YAML,
    GOOD_CONFIG,
    KEY,
    LEAF,
    SANDBOX_SETTINGS,
    SANDBOX_YAML,
    estate,
    publisher_file,
)
from tests.forge.cli.test_register_repo_sandbox import (  # noqa: F401
    GATEWAY,
    OWN_SETTINGS,
    OWN_YAML,
    SANDBOX_ADDRESS,
    WORKSPACE,
    own,
)

API_TEST = "guardkit/api_test"
B_KEY = "guardkit/bench-two"
#: How unregister-repo names its staged copies and backups: owner and name.
TAG = "guardkit+bench-one"
C_KEY = "guardkit/bench-three"


def _register(key: str, *args: str):
    extra = ["--gateway-address", "192.0.2.10"] if "--publish" in args else []
    return CliRunner().invoke(
        main,
        ["register-repo", key, "--github", f"https://github.com/{key}.git",
         "--sandbox-settings", SANDBOX_SETTINGS, *args, *extra],
        catch_exceptions=False,
    )


def _register_own(key: str, slot: str, workspace: str, *args: str):
    return CliRunner().invoke(
        main,
        ["register-repo", key, "--github", f"https://github.com/{key}.git", "--slot", slot,
         "--sandbox-clone", workspace, "--gateway-address", GATEWAY,
         "--sandbox-address", SANDBOX_ADDRESS, *args],
        catch_exceptions=False,
    )


def _unregister(key: str, *args: str):
    return CliRunner().invoke(main, ["unregister-repo", key, *args], catch_exceptions=False)


def _activate(estate, leaf: str, settings: str, publisher=None, *, removing=False):  # noqa: F811
    """Put the staged copies in place, as step (c) of the printed sequence does.
    ``leaf`` is the short name for register-repo's copies, owner+name for
    unregister-repo's."""
    suffix = f"{leaf}-unregister-pending" if removing else f"{leaf}-pending"
    estate.volume["forge.yaml"] = estate.volume.get(f"forge.yaml.{suffix}", estate.volume["forge.yaml"])
    estate.sandbox_files[settings] = estate.sandbox_files.get(f"{settings}.{suffix}", estate.sandbox_files[settings])
    if publisher is not None:
        pending = publisher.parent / f"settings.json.{suffix}"
        if pending.exists():
            publisher.write_bytes(pending.read_bytes())


def _live(estate, settings: str, publisher=None):  # noqa: F811
    return (
        estate.volume["forge.yaml"], estate.modes["forge.yaml"],
        estate.sandbox_files[settings], estate.modes[settings],
        None if publisher is None else (publisher.read_bytes(), publisher.stat().st_mode),
    )


def _removed(old: str, new: str) -> list[str]:
    remaining = list(old.split("\n"))
    for line in new.split("\n"):
        remaining.remove(line)
    return remaining


# ---------------------------------------------------------------------------
# (1) Register, then unregister: every file byte for byte as before


def test_register_then_unregister_leaves_every_file_as_it_was(estate, publisher_file):  # noqa: F811
    before = _live(estate, SANDBOX_SETTINGS, publisher_file)
    publish = ("--publish", "--publisher-settings", str(publisher_file))
    assert _register(KEY, *publish).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS, publisher_file)
    registered = _live(estate, SANDBOX_SETTINGS, publisher_file)
    assert registered != before

    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS, "--publisher-settings", str(publisher_file))
    assert result.exit_code == 0, result.output
    assert _live(estate, SANDBOX_SETTINGS, publisher_file) == registered, "nothing live is changed"
    _activate(estate, TAG, SANDBOX_SETTINGS, publisher_file, removing=True)

    assert _live(estate, SANDBOX_SETTINGS, publisher_file) == before
    # The sandbox and the factory's clone in it are left as they are.
    assert estate.clones == {CLONE_PATH: f"https://github.com/{KEY}.git"}
    assert f"the factory's clone {CLONE_PATH}" in result.output
    # Dated backups of the live text, named for this command.
    stamp = datetime.date.today().strftime("%Y%m%d")
    backup = f"bak-{stamp}-pre-unregister-{TAG}"
    assert estate.volume[f"forge.yaml.{backup}"] == registered[0]
    assert estate.sandbox_files[f"{SANDBOX_SETTINGS}.{backup}"] == registered[2]
    assert (publisher_file.parent / f"settings.json.{backup}").read_bytes() == registered[4][0]
    # The sequence that makes it live, checking it is gone at the end.
    assert "unregister-repo again" in result.output
    assert "no longer lists the project's sandbox" in result.output
    assert "docker restart forge-estate-forge-publisher-1" in result.output


def test_register_then_unregister_a_sandbox_of_its_own_leaves_every_file_as_it_was(own, publisher_file):  # noqa: F811
    before = _live(own, OWN_SETTINGS, publisher_file)
    assert _register_own(KEY, "1", WORKSPACE, "--via-git-door", "--publish",
                         "--publisher-settings", str(publisher_file)).exit_code == 0
    _activate(own, LEAF, OWN_SETTINGS, publisher_file)
    assert yaml.safe_load(own.volume["forge.yaml"])["planning"]["sandboxes"][KEY]["memory_projects"] == ["bench_one"]

    result = _unregister(KEY, "--sandbox-clone", WORKSPACE, "--publisher-settings", str(publisher_file))
    assert result.exit_code == 0, result.output
    _activate(own, TAG, OWN_SETTINGS, publisher_file, removing=True)

    assert _live(own, OWN_SETTINGS, publisher_file) == before
    assert "with its memory grant bench_one" in result.output
    # The estate's door lines register-repo printed are named, never edited.
    assert "31851:bench_one" in result.output and "FLEET_MEMORY_SANDBOX_DOORS" in result.output
    assert f"31831/{KEY}" in result.output and "GIT_DOOR_REPOS_FILE" in result.output
    assert f"the sandbox {LEAF} itself" in result.output
    # No sandbox was made, stopped or removed: every sbx call is an exec.
    assert all(call[1] == "exec" for call in own.calls if call[0] == "sbx")


# ---------------------------------------------------------------------------
# (2) A dry run changes nothing and says every change


def test_dry_run_changes_nothing_and_says_each_change(estate, publisher_file):  # noqa: F811
    assert _register(KEY, "--publish", "--publisher-settings", str(publisher_file)).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS, publisher_file)
    volume, files, clones = dict(estate.volume), dict(estate.sandbox_files), dict(estate.clones)
    listing = {p: p.read_bytes() for p in publisher_file.parent.iterdir()}
    estate.writes.clear()

    result = _unregister(KEY, "--dry-run", "--sandbox-settings", SANDBOX_SETTINGS,
                         "--publisher-settings", str(publisher_file))

    assert result.exit_code == 0, result.output
    assert estate.writes == []
    assert (estate.volume, estate.sandbox_files, estate.clones) == (volume, files, clones)
    assert {p: p.read_bytes() for p in publisher_file.parent.iterdir()} == listing
    out = result.output
    for said in (
        f"coordinator  remove", f"sandboxes {KEY} (sandbox api-test-deploy)",
        f"target_repo_paths {KEY} -> /var/lib/forge/projects/{LEAF}",
        f"allowlist /var/lib/forge/projects/{LEAF}", f"fetch_addresses {KEY}",
        f"target_repo_paths {KEY} -> {CLONE_PATH}", f"allowlist {CLONE_PATH}",
        f"route {KEY}", "What would make it live",
    ):
        assert said in out, said
    assert out.count("would stage") == 3


# ---------------------------------------------------------------------------
# (3) A repository that is not registered


def test_a_repository_that_is_not_registered_is_refused(estate):  # noqa: F811
    for key in ("guardkit/never-here", "checkouts/api_test"):
        result = _unregister(key, "--sandbox-settings", SANDBOX_SETTINGS)
        assert result.exit_code == 1
        assert f"{key} is not registered with the factory" in result.output
    assert estate.writes == []


# ---------------------------------------------------------------------------
# (4) Two registered: the other one's entries, shared items included, stay


def test_unregistering_one_of_two_leaves_exactly_the_other(estate, publisher_file):  # noqa: F811
    publish = ("--publish", "--publisher-settings", str(publisher_file))
    original = publisher_file.read_bytes()
    # What B alone gives.
    assert _register(B_KEY, *publish).exit_code == 0
    _activate(estate, "bench-two", SANDBOX_SETTINGS, publisher_file)
    b_only = _live(estate, SANDBOX_SETTINGS, publisher_file)

    # Start again: A, then B, then A out.
    estate.volume = {"forge.yaml": COORDINATOR_YAML}
    estate.sandbox_files = {SANDBOX_SETTINGS: SANDBOX_YAML}
    for name in os.listdir(publisher_file.parent):
        if name != "settings.json":
            (publisher_file.parent / name).unlink()
    publisher_file.write_bytes(original)
    assert _register(KEY, *publish).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS, publisher_file)
    assert _register(B_KEY, *publish).exit_code == 0
    _activate(estate, "bench-two", SANDBOX_SETTINGS, publisher_file)
    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS, "--publisher-settings", str(publisher_file))
    assert result.exit_code == 0, result.output
    _activate(estate, TAG, SANDBOX_SETTINGS, publisher_file, removing=True)

    assert _live(estate, SANDBOX_SETTINGS, publisher_file) == b_only
    coordinator = yaml.safe_load(estate.volume["forge.yaml"])
    assert coordinator["planning"]["sandboxes"][B_KEY]["name"] == "api-test-deploy"
    assert coordinator["planning"]["fetch_addresses"] == {B_KEY: f"https://github.com/{B_KEY}.git"}


def test_a_path_another_repository_uses_stays_on_the_allowlist(estate):  # noqa: F811
    """api_test's folder is also checkouts/api_test's; and B keeps the shared
    sandbox registered, so api_test itself may go."""
    assert _register(B_KEY).exit_code == 0
    _activate(estate, "bench-two", SANDBOX_SETTINGS)
    result = _unregister(API_TEST, "--sandbox-settings", SANDBOX_SETTINGS)
    assert result.exit_code == 0, result.output
    staged = yaml.safe_load(estate.volume["forge.yaml.guardkit+api_test-unregister-pending"])
    assert "/var/lib/forge/projects/api_test" in staged["permissions"]["filesystem"]["allowlist"]
    assert staged["planning"]["target_repo_paths"]["checkouts/api_test"] == "/var/lib/forge/projects/api_test"
    assert API_TEST not in staged["planning"]["sandboxes"] and B_KEY in staged["planning"]["sandboxes"]
    assert "checkouts/api_test still uses it" in result.output
    assert "default_target_repo still names guardkit/api_test" in result.output


def test_another_sandbox_of_its_own_keeps_its_entry_and_its_memory_grant(own):  # noqa: F811
    assert _register_own(KEY, "1", WORKSPACE).exit_code == 0
    _activate(own, LEAF, OWN_SETTINGS)
    second = "/workspace/bench-two"
    second_settings = f"{second}/.guardkit/tmp/factory-runtime/forge.yaml"
    own.sandbox_files[second_settings] = OWN_YAML
    own.modes[second_settings] = "600"
    own.sandbox_name, own.clone_root = "bench-two", second
    own.write_project(".guardkit/config.yaml", GOOD_CONFIG.replace("bench_one", "bench_two"))
    assert _register_own(B_KEY, "2", second).exit_code == 0
    _activate(own, "bench-two", second_settings)
    b_entry = yaml.safe_load(own.volume["forge.yaml"])["planning"]["sandboxes"][B_KEY]
    b_file = own.sandbox_files[second_settings]

    own.sandbox_name, own.clone_root = LEAF, WORKSPACE
    result = _unregister(KEY, "--sandbox-clone", WORKSPACE)
    assert result.exit_code == 0, result.output
    _activate(own, TAG, OWN_SETTINGS, removing=True)

    planning = yaml.safe_load(own.volume["forge.yaml"])["planning"]
    assert KEY not in planning["sandboxes"] and planning["sandboxes"][B_KEY] == b_entry
    assert b_entry["memory_projects"] == ["bench_two"]
    assert planning["fetch_addresses"] == {B_KEY: f"https://github.com/{B_KEY}.git"}
    assert own.sandbox_files[second_settings] == b_file, "the other sandbox's file is not touched"
    assert own.sandbox_files[OWN_SETTINGS] == OWN_YAML


# ---------------------------------------------------------------------------
# R9: the shared sandbox's reference is any registration on it, and the last
# one stays


def test_the_last_registration_on_the_shared_sandbox_is_refused(estate):  # noqa: F811
    result = _unregister(API_TEST, "--sandbox-settings", SANDBOX_SETTINGS)
    assert result.exit_code == 1
    assert "guardkit/api_test is the last repository registered on the shared sandbox" in result.output
    assert estate.writes == []


def test_after_api_test_is_out_a_new_registration_copies_another_one(estate):  # noqa: F811
    assert _register(B_KEY).exit_code == 0
    _activate(estate, "bench-two", SANDBOX_SETTINGS)
    # C registered beside api_test and B: what it would have given.
    assert _register(C_KEY).exit_code == 0
    beside = (estate.volume["forge.yaml.bench-three-pending"],
              estate.sandbox_files[f"{SANDBOX_SETTINGS}.bench-three-pending"])

    with_a = (estate.volume["forge.yaml"], estate.sandbox_files[SANDBOX_SETTINGS])
    assert _unregister(API_TEST, "--sandbox-settings", SANDBOX_SETTINGS).exit_code == 0
    _activate(estate, "guardkit+api_test", SANDBOX_SETTINGS, removing=True)
    a_lines = [_removed(with_a[0], estate.volume["forge.yaml"]),
               _removed(with_a[1], estate.sandbox_files[SANDBOX_SETTINGS])]

    result = _register(C_KEY)
    assert result.exit_code == 0, result.output
    after = (estate.volume["forge.yaml.bench-three-pending"],
             estate.sandbox_files[f"{SANDBOX_SETTINGS}.bench-three-pending"])

    for old, new, gone in zip(beside, after, a_lines):
        expected = old.split("\n")
        for line in gone:
            expected.remove(line)  # api_test's own lines, the first of each
        assert new.split("\n") == expected
    coordinator = yaml.safe_load(after[0])["planning"]["sandboxes"]
    assert coordinator[C_KEY] == coordinator[B_KEY]
    assert yaml.safe_load(after[1])["planning"]["sandboxes"][C_KEY]["sidecar_url"] == "http://192.0.2.10:8925"


# ---------------------------------------------------------------------------
# The command is listed


def test_the_command_is_listed_and_has_help():
    listed = CliRunner().invoke(main, ["--help"])
    assert listed.exit_code == 0 and "unregister-repo" in listed.output
    own_help = CliRunner().invoke(main, ["unregister-repo", "--help"])
    assert own_help.exit_code == 0 and "--dry-run" in own_help.output


def test_no_name_is_refused_in_one_sentence(estate):  # noqa: F811
    result = CliRunner().invoke(main, ["unregister-repo"], catch_exceptions=False)
    assert result.exit_code == 1 and "name the repository" in result.output
    assert estate.writes == []


# ---------------------------------------------------------------------------
# Codex review round 1


def _with_inner_repository(estate, path: str) -> None:  # noqa: F811
    """Another repository mapped at ``path`` in the live coordinator file."""
    estate.volume["forge.yaml"] = estate.volume["forge.yaml"].replace(
        "    checkouts/api_test: /var/lib/forge/projects/api_test\n",
        f"    checkouts/api_test: /var/lib/forge/projects/api_test\n    guardkit/inner: {path}\n",
    )


@pytest.mark.parametrize("path", [
    f"/var/lib/forge/projects/{LEAF}/inner",
    f"/var/lib/forge/projects/{LEAF}/",
    f"/var/lib/forge/projects/./{LEAF}",
    f"/var/lib/forge//projects/{LEAF}/sub/../inner",
])
def test_r1_an_allowlist_entry_another_repository_is_under_stays(estate, path):  # noqa: F811
    assert _register(KEY).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS)
    _with_inner_repository(estate, path)
    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)
    assert result.exit_code == 0, result.output
    staged = yaml.safe_load(estate.volume[f"forge.yaml.{TAG}-unregister-pending"])
    assert f"/var/lib/forge/projects/{LEAF}" in staged["permissions"]["filesystem"]["allowlist"]
    assert KEY not in staged["planning"]["target_repo_paths"]
    assert "guardkit/inner still uses it" in result.output


def test_r1_a_sibling_whose_name_only_starts_the_same_does_not_keep_it(estate):  # noqa: F811
    assert _register(KEY).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS)
    _with_inner_repository(estate, f"/var/lib/forge/projects/{LEAF}-two")
    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)
    assert result.exit_code == 0, result.output
    staged = yaml.safe_load(estate.volume[f"forge.yaml.{TAG}-unregister-pending"])
    assert f"/var/lib/forge/projects/{LEAF}" not in staged["permissions"]["filesystem"]["allowlist"]


def test_r2_a_comment_inside_the_entry_is_refused_naming_its_line(estate):  # noqa: F811
    assert _register(KEY).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS)
    lines = estate.volume["forge.yaml"].split("\n")
    at = lines.index(f"    {KEY}:") + 2
    lines.insert(at, "      # Why this project has the shared sandbox.")
    estate.volume["forge.yaml"] = "\n".join(lines)
    estate.writes.clear()

    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)

    assert result.exit_code == 1
    assert f"removing {KEY} would delete a comment (line {at + 1} of its forge.yaml)" in result.output
    assert estate.writes == []


def test_r2_comments_around_the_entry_are_kept(estate):  # noqa: F811
    assert _register(KEY).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS)
    estate.volume["forge.yaml"] = estate.volume["forge.yaml"].replace(
        f"    {KEY}:\n      name:", f"    # Added for the bench.\n    {KEY}:\n      name:"
    )
    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)
    assert result.exit_code == 0, result.output
    staged = estate.volume[f"forge.yaml.{TAG}-unregister-pending"]
    assert "    # Added for the bench.\n" in staged and "# A note after the last sandbox." in staged


def _check_line(output: str, marker: str) -> str:
    """The pipeline after ``docker logs ... 2>&1 |`` on the printed line with ``marker``."""
    line = next(line for line in output.splitlines() if marker in line)
    return line.split("2>&1 |", 1)[1]


def test_r3_the_unregister_sequence_checks_the_route_is_gone_after_the_restart(estate, publisher_file):  # noqa: F811
    assert _register(KEY, "--publish", "--publisher-settings", str(publisher_file)).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS, publisher_file)
    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS, "--publisher-settings", str(publisher_file))
    assert result.exit_code == 0, result.output
    out = result.output
    assert "lists the new route" not in out
    assert "publisher_since=$(date -u +%Y-%m-%dT%H:%M:%SZ)" in out
    assert 'docker logs --since "$publisher_since" forge-estate-forge-publisher-1' in out
    assert "coordinator_since=$(date -u" in out and 'docker logs --since "$coordinator_since"' in out
    # The restart comes after the drained check and the time mark, in one command.
    lines = out.splitlines()
    mark = next(i for i, line in enumerate(lines) if "publisher_since=$(" in line)
    assert "--check-drained" in lines[mark - 1] and "docker restart forge-estate-forge-publisher-1" in lines[mark + 1]

    # The check itself, run on made-up log lines.
    check = _check_line(out, "$publisher_since")
    gone = 'publisher: settings {"projects": {"guardkit/api_test": {"source": "x"}}}\n'
    there = f'publisher: settings {{"projects": {{"{KEY}": {{"source": "x"}}}}}}\n'
    def runs(log: str) -> int:
        return subprocess.run(["bash", "-c", check], input=log, text=True, capture_output=True).returncode
    assert runs(gone) == 0, "started, and the route is gone"
    assert runs(there) == 1, "the route is still in the settings it loaded"
    assert runs("") == 1, "no settings line since the restart: it did not start"
    assert runs(there + gone) == 0, "only the newest settings line counts"


# ---------------------------------------------------------------------------
# Codex review round 2 (R2): an inline comment on any line to be deleted


def _registered(estate):  # noqa: F811
    assert _register(KEY).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS)
    estate.writes.clear()


@pytest.mark.parametrize("old, new", [
    # On a child value of the sandbox entry.
    ('      runner_url: "${FORGE_SANDBOX_RUNNER_URL}"\n  fetch_addresses:',
     '      runner_url: "${FORGE_SANDBOX_RUNNER_URL}"  # its runner\n  fetch_addresses:'),
    # On a scalar mapping entry.
    (f"    {KEY}: /var/lib/forge/projects/{LEAF}\n",
     f"    {KEY}: /var/lib/forge/projects/{LEAF}  # the bench\n"),
    # On an allowlist item.
    (f"      - /var/lib/forge/projects/{LEAF}\n",
     f"      - /var/lib/forge/projects/{LEAF} # the bench\n"),
])
def test_r2_an_inline_comment_on_a_line_to_be_deleted_is_refused(estate, old, new):  # noqa: F811
    _registered(estate)
    live = estate.volume["forge.yaml"]
    assert live.count(old) == 1, old
    estate.volume["forge.yaml"] = live.replace(old, new)
    number = estate.volume["forge.yaml"].split("\n").index(new.split("\n")[0]) + 1

    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)

    assert result.exit_code == 1
    assert f"would delete a comment (line {number} of its forge.yaml)" in result.output
    assert estate.writes == []
    assert not [name for name in estate.volume if "unregister" in name]


@pytest.mark.parametrize("value", ["'/var/lib/forge/projects/#'", '"/var/lib/forge/projects/a #b"'])
def test_r2_a_hash_inside_quotes_is_not_a_comment(estate, value):  # noqa: F811
    _registered(estate)
    estate.volume["forge.yaml"] = estate.volume["forge.yaml"].replace(
        f"    {KEY}: /var/lib/forge/projects/{LEAF}\n", f"    {KEY}: {value}\n"
    )
    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)
    assert result.exit_code == 0, result.output
    staged = yaml.safe_load(estate.volume[f"forge.yaml.{TAG}-unregister-pending"])
    assert KEY not in staged["planning"]["target_repo_paths"]


def test_r2_what_counts_as_a_comment():
    from forge.cli.unregister_repo import _has_comment

    for line in ("  # whole", "x: y # inline", "- /a\t# tab", "x: it's # still", 'x: "a" # after',
                 "x: 'it''s' # after"):
        assert _has_comment(line), line
    for line in ("x: '#'", 'x: "a #b"', "x: a#b", 'x: "a \\" #b"', "- '/srv/#x'", "x: [a, '#']"):
        assert not _has_comment(line), line


# ---------------------------------------------------------------------------
# Coach review: C2 (two owners, one short name) and C3 (the sandbox's default)


def test_c2_two_repositories_with_one_short_name_keep_their_own_copies(estate):  # noqa: F811
    other = f"other/{LEAF}"
    _registered(estate)
    estate.clones.clear()  # the shared projects folder holds one clone per short name
    assert _register(other).exit_code == 0
    _activate(estate, LEAF, SANDBOX_SETTINGS)
    live = estate.volume["forge.yaml"]

    first = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)
    second = _unregister(other, "--sandbox-settings", SANDBOX_SETTINGS)

    assert first.exit_code == 0 and second.exit_code == 0, first.output + second.output
    stamp = datetime.date.today().strftime("%Y%m%d")
    mine = yaml.safe_load(estate.volume[f"forge.yaml.{TAG}-unregister-pending"])["planning"]
    theirs = yaml.safe_load(estate.volume[f"forge.yaml.other+{LEAF}-unregister-pending"])["planning"]
    assert KEY not in mine["sandboxes"] and other in mine["sandboxes"]
    assert other not in theirs["sandboxes"] and KEY in theirs["sandboxes"]
    for tag in (TAG, f"other+{LEAF}"):
        assert estate.volume[f"forge.yaml.bak-{stamp}-pre-unregister-{tag}"] == live
        assert estate.sandbox_files[f"{SANDBOX_SETTINGS}.bak-{stamp}-pre-unregister-{tag}"] == \
            estate.sandbox_files[SANDBOX_SETTINGS]


def test_c2_an_earlier_staged_copy_or_backup_is_never_overwritten(estate):  # noqa: F811
    _registered(estate)
    assert _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS).exit_code == 0
    again = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)
    assert again.exit_code == 0 and "already staged" in again.output, "the same run again is fine"

    # The live file changes; the earlier staged copy no longer matches.
    estate.volume["forge.yaml"] = estate.volume["forge.yaml"].replace("enabled: true", "enabled: true\n  # x", 1)
    before = (dict(estate.volume), dict(estate.sandbox_files))
    estate.writes.clear()
    result = _unregister(KEY, "--sandbox-settings", SANDBOX_SETTINGS)
    assert result.exit_code == 1
    assert "never overwrites a staged copy or a backup" in result.output
    assert estate.writes == [] and (estate.volume, estate.sandbox_files) == before


def test_c3_the_default_repository_is_said_for_each_file_that_names_it(estate):  # noqa: F811
    assert _register(B_KEY).exit_code == 0
    _activate(estate, "bench-two", SANDBOX_SETTINGS)
    estate.sandbox_files[SANDBOX_SETTINGS] = estate.sandbox_files[SANDBOX_SETTINGS].replace(
        "  enabled: true\n", f"  enabled: true\n  default_target_repo: {API_TEST}\n", 1
    )
    result = _unregister(API_TEST, "--sandbox-settings", SANDBOX_SETTINGS)
    assert result.exit_code == 0, result.output
    notes = [line for line in result.output.splitlines() if "default_target_repo still names" in line]
    assert [line.split()[0] for line in notes] == ["coordinator", "sandbox"]
