"""``forge register-repo --slot`` chooses preview ports the project does not use (20 October 2026).

Found live: slot 1's preview ports are 8960 and 8961, the very ports a project
in slot 1 used itself for its live app and its merge-check copy. A preview port
is the same number inside the sandbox as on its machine, so no preview could
start. register-repo now reads the ports the project's committed deploy profile
declares and writes, as ``preview_ports``, the first two free ports in the
slot's block of ten (8960 to 8969 for slot 1). With no clash that is the usual
pair. A project registered before is moved by running the command again: its
one ``preview_ports`` line is rewritten in both files, and nothing else.

The estate is the stand-in of the other register-repo tests; nothing reaches
git, docker, sbx or GitHub, and every name and address is made up.
"""

from __future__ import annotations

import yaml

from tests.forge.cli.test_register_repo import KEY, LEAF, estate  # noqa: F401
from tests.forge.cli.test_register_repo_sandbox import (  # noqa: F401
    COORDINATOR_ENTRY,
    ENTRY,
    OWN,
    OWN_SETTINGS,
    _run_own,
    own,
)

#: The deploy profile of a project written for the shared sandbox: its live
#: app on 8960, its merge-check copy on 8961, previews on 8950 and 8951.
CLASHING_PROFILE = """\
format_version: "1.0"
env_id: example
compose:
  file: compose.yaml
  script: deploy/deploy.sh
sandbox:
  name: api-test-deploy
  publish: ["127.0.0.1:8960:8960", "127.0.0.1:8961:8961", "127.0.0.1:8950:8950", "127.0.0.1:8951:8951"]
live_gate:
  driver: ["node", "check.mjs"]
  env:
    APP_BASE_URL: "http://127.0.0.1:8960"
candidate:
  env:
    CANDIDATE_PORT: "8961"
preview:
  enabled: true
  slots:
    - port: 8950
      internal_port: 8970
    - port: 8951
      internal_port: 8971
"""

#: The same project's ports, none of them in slot 1's block.
CLEAR_PROFILE = CLASHING_PROFILE.replace("8960", "8901").replace("8961", "8902")

PENDING = f"forge.yaml.{LEAF}-pending"


def _staged(own):  # noqa: F811
    coordinator = yaml.safe_load(own.volume[PENDING])["planning"]["sandboxes"][KEY]
    sandbox = yaml.safe_load(own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"])["planning"]["sandboxes"][KEY]
    return coordinator, sandbox


def test_a_clash_gives_the_next_free_pair_in_both_files_and_the_spark_lines(own):  # noqa: F811
    own.write_project("deploy/profile.yaml", CLASHING_PROFILE)
    result = _run_own()
    assert result.exit_code == 0, result.output

    coordinator, sandbox = _staged(own)
    assert coordinator == {**COORDINATOR_ENTRY, "preview_ports": [8962, 8963]}
    assert sandbox == {**ENTRY, "preview_ports": [8962, 8963]}

    out = result.output
    assert "previews on 8962 and 8963, not the slot's usual 8960 and 8961" in out
    assert "the project itself uses 8960 (sandbox.publish 127.0.0.1:8960:8960)" in out
    assert f"sbx ports {OWN} --publish 127.0.0.1:8962:8962 --publish 127.0.0.1:8963:8963" in out
    assert "sudo -n tailscale serve --bg --https=8962 http://127.0.0.1:8962" in out
    assert "sudo -n tailscale serve --bg --https=8963 http://127.0.0.1:8963" in out
    assert "ss -ltnH '( sport = :8962 or sport = :8963 )'" in out
    assert "sudo -n tailscale serve --https=8960 off and sudo -n tailscale serve --https=8961 off" in out


def test_no_clash_keeps_the_usual_pair_and_prints_no_extra_lines(own):  # noqa: F811
    own.write_project("deploy/profile.yaml", CLEAR_PROFILE)
    result = _run_own()
    assert result.exit_code == 0, result.output
    coordinator, sandbox = _staged(own)
    assert coordinator == COORDINATOR_ENTRY and sandbox == ENTRY
    assert "sbx ports" not in result.output and "tailscale" not in result.output


def test_a_block_with_no_free_pair_is_refused_and_nothing_is_written(own):  # noqa: F811
    rules = ", ".join(f'"127.0.0.1:{p}:{p}"' for p in range(8960, 8969))
    own.write_project(
        "deploy/profile.yaml",
        CLASHING_PROFILE.replace(
            'publish: ["127.0.0.1:8960:8960", "127.0.0.1:8961:8961", "127.0.0.1:8950:8950", "127.0.0.1:8951:8951"]',
            f"publish: [{rules}]",
        ),
    )
    result = _run_own()
    assert result.exit_code == 1
    assert "slot 1's preview ports are 8960 to 8969" in result.output
    assert "fewer than two are left for its previews" in result.output
    assert own.writes == []


def test_a_profile_that_cannot_be_used_is_refused_in_words(own):  # noqa: F811
    own.write_project("deploy/profile.yaml", "env_id: example\n")
    result = _run_own()
    assert result.exit_code == 1
    assert "deploy/profile.yaml could not be used" in result.output
    assert "its preview ports cannot be chosen" in result.output
    assert own.writes == []


def test_a_project_registered_before_has_only_its_preview_ports_line_rewritten(own):  # noqa: F811
    # Registered before this fix: no profile was read, so it has the usual pair.
    assert _run_own().exit_code == 0
    own.volume["forge.yaml"] = own.volume[PENDING]
    own.sandbox_files[OWN_SETTINGS] = own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"]
    before = {"coordinator": own.volume["forge.yaml"], "sandbox": own.sandbox_files[OWN_SETTINGS]}

    own.write_project("deploy/profile.yaml", CLASHING_PROFILE)
    again = _run_own()
    assert again.exit_code == 0, again.output
    assert f"sandboxes {KEY} preview_ports 8960, 8961 -> 8962, 8963" in again.output

    after = {
        "coordinator": own.volume[PENDING],
        "sandbox": own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"],
    }
    for which in ("coordinator", "sandbox"):
        old_lines, new_lines = before[which].splitlines(), after[which].splitlines()
        assert len(old_lines) == len(new_lines), which
        changed = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
        assert [(a.strip(), b.strip()) for a, b in changed] == [
            ("preview_ports: [8960, 8961]", "preview_ports: [8962, 8963]")
        ], which

    # And running once more changes nothing.
    own.volume["forge.yaml"] = own.volume[PENDING]
    own.sandbox_files[OWN_SETTINGS] = own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"]
    del own.volume[PENDING]
    third = _run_own()
    assert third.exit_code == 0, third.output
    assert "nothing is staged" in third.output


def test_recorded_ports_the_project_does_not_use_are_not_rewritten(own):  # noqa: F811
    """Only ports that clash are moved: a recorded pair written some other way,
    which the project does not use, is refused as any differing entry is."""
    assert _run_own().exit_code == 0
    own.volume["forge.yaml"] = own.volume[PENDING].replace(
        "preview_ports: [8960, 8961]", "preview_ports: [8964, 8965]"
    )
    own.sandbox_files[OWN_SETTINGS] = own.sandbox_files[f"{OWN_SETTINGS}.{LEAF}-pending"]
    own.writes.clear()
    own.write_project("deploy/profile.yaml", CLASHING_PROFILE)
    again = _run_own()
    assert again.exit_code == 1
    assert "already has an entry" in again.output
    assert own.writes == []
