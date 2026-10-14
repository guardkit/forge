"""The build sandbox keeps no factory keys.

WHY THIS FILE EXISTS (14 October 2026). Five factory secrets reached both
containers in a project's sandbox: the coordinator's bus address with its
password, the memory bus address with its password, the build account's bus
password, the memory store's address with its password, and the model key.
Every process in those containers runs as one user, and both containers hold
the sandbox's engine socket, so project code a build runs could read every one
of them: from another process's settings, or with ``docker inspect``.

The fix takes the keys out rather than trying to hide them:

* two of them were never needed in the sandbox and simply go;
* memory reads and writes go through the memory door on the gateway, which
  needs no key in the sandbox (``FLEET_MEMORY_MCP_URL``);
* model calls go through a new model door on the gateway, which adds the key
  itself, so both containers are given one fixed placeholder that is the same
  in every release and is not a secret;
* both start scripts refuse, by name and never by value, to hand anything
  credential-shaped into the sandbox again. One exact name is excused, the
  stamp check's token count, and only when its value is digits.

What is proven here, with the stand-in ``docker`` and ``sbx`` programs the
other sandbox tests use. **No sandbox, image, container, engine or service of
the estate is touched by anything in this file**, and every "secret" below is a
made-up sentinel.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from forge.launch_environment import (
    RESERVED_CREDENTIAL_WORDS,
    build_launch_env,
    launch_setting_names,
)
from tests.forge.deploy.test_sandbox_bootstrap_from_the_release_image import (  # noqa: F401
    BOOTSTRAP,
    _calls,
    _start_expecting_it_to_end,
    _the_two_starts,
    sandbox,
)
from tests.forge.deploy.test_sandbox_runner_supervisor import (  # noqa: F401
    RUN_SH,
    _calls as _client_calls,
    _settings as _supervisor_settings,
    root,
)
from tests.forge.deploy.test_the_estate_bundle import ESTATE, _rendered

#: The fixed text both containers are given for the model client's key. The
#: model door replaces whatever a caller sends, so this is never a key.
MODEL_PLACEHOLDER = "no-key-the-model-door-adds-it"

#: The five factory secrets that used to cross into the sandbox.
THE_FIVE = (
    "FORGE_NATS_URL",
    "FLEET_MEMORY_NATS_URL",
    "GUARDKIT_NATS_PASSWORD",
    "FLEET_MEMORY_PG_DSN",
    "OPENAI_API_KEY",
)

#: The one credential-shaped name either script lets through, and only with a
#: value made of digits.
THE_ONE_EXCUSED_NAME = "GUARDKIT_STAMP_MODEL_MAX_TOKENS"

#: A made-up value that must never be written anywhere a test can see.
SENTINEL = "sentinel-not-a-key-0f3b6c2a9d814e57"


def _a_url_with(password: str, *, scheme: str = "nats", user: str = "someone") -> str:
    return f"{scheme}://{user}:{password}@192.0.2.10:4222"


def _the_array(script: Path, name: str) -> list[str]:
    """One bash array's words, read out of a script's text."""
    match = re.search(rf"^{name}=\(([^)]*)\)", script.read_text(), re.M)
    assert match, f"{script.name} has no {name}=( ... ) array"
    return match.group(1).split()


def _credential_shaped(name: str) -> bool:
    words = (*RESERVED_CREDENTIAL_WORDS, "DSN")
    return any(word in name.upper() for word in words)


def _the_factory_list() -> list[str]:
    """The names the start script hands both containers, in its own order."""
    text = BOOTSTRAP.read_text()
    body = text.split("FACTORY_ENV_NAMES=(", 1)[1].split("\n)", 1)[0]
    names = []
    for line in body.splitlines():
        word = line.split("#", 1)[0].strip()
        if word:
            names.append(word)
    return names


def _ordinary_value_for(name: str) -> str:
    if name == THE_ONE_EXCUSED_NAME:
        return "4096"
    if name == "FORGE_MAX_CONCURRENT_BUILDS":
        return "2"
    return f"an-ordinary-value-for-{name.lower()}"


def _run_the_supervisor(root: Path, seconds: float = 15, **extra: str):
    """(exit status, output); the status is None when it was still running."""
    env = _supervisor_settings(root)
    env.update(extra)
    process = subprocess.Popen(
        ["bash", str(RUN_SH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        said = process.communicate(timeout=seconds)[0]
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            said = process.communicate(timeout=60)[0]
        except subprocess.TimeoutExpired:  # pragma: no cover - a stuck test
            process.kill()
            said = process.communicate(timeout=10)[0]
        return None, said
    return process.returncode, said


# ---------------------------------------------------------------------------
# The start script inside the sandbox
# ---------------------------------------------------------------------------


def test_no_factory_credential_reaches_either_container(sandbox):
    """Test 1. The five old secrets are set, and a project names two
    credential-shaped settings of its own. The start refuses with exit 2,
    names both of the project's settings, and nothing carrying any of them is
    ever started. No sentinel is written anywhere."""
    extra = {name: SENTINEL for name in THE_FIVE}
    extra["FORGE_NATS_URL"] = _a_url_with(SENTINEL, user="forge")
    extra["FLEET_MEMORY_NATS_URL"] = _a_url_with(SENTINEL, user="fleet-memory")
    extra["FLEET_MEMORY_PG_DSN"] = _a_url_with(SENTINEL, scheme="postgresql")
    extra["A_SERVICE_TOKEN"] = SENTINEL
    extra["SANDBOX_CONTAINER_ENV_NAMES"] = "GUARDKIT_NATS_PASSWORD A_SERVICE_TOKEN"

    status, said = _start_expecting_it_to_end(sandbox, **extra)

    assert status == 2, said
    assert "GUARDKIT_NATS_PASSWORD" in said and "A_SERVICE_TOKEN" in said, said
    runs = [call for call in _calls(sandbox) if call.startswith("run ")]
    for call in runs:
        for name in (*THE_FIVE, "A_SERVICE_TOKEN"):
            assert f"--env {name} " not in call + " ", name
    assert SENTINEL not in "\n".join(_calls(sandbox))
    assert SENTINEL not in said


def test_the_model_placeholder_is_fixed(sandbox):
    """Test 2. Whatever model key the script's own environment holds, both
    containers are given the one fixed placeholder, and never that key."""
    helper, runner = _the_two_starts(sandbox, OPENAI_API_KEY=SENTINEL)
    for call in (helper, runner):
        assert f"--env OPENAI_API_KEY={MODEL_PLACEHOLDER}" in call, call
        assert "--env OPENAI_API_KEY " not in call
        assert SENTINEL not in call
    assert SENTINEL not in "\n".join(_calls(sandbox))


def test_a_url_with_a_password_is_refused_by_name(sandbox):
    """Test 3. An ordinary setting whose value is an address with a password
    in it is refused, by the setting's name, and the password is never
    written down."""
    status, said = _start_expecting_it_to_end(
        sandbox, FLEET_MEMORY_EMBED_URL=f"http://u:{SENTINEL}@h:1"
    )
    assert status == 2, said
    assert "FLEET_MEMORY_EMBED_URL" in said
    assert SENTINEL not in said
    assert SENTINEL not in "\n".join(_calls(sandbox))
    assert not [call for call in _calls(sandbox) if call.startswith("run ")]


def test_the_memory_door_address_reaches_both(sandbox):
    """Test 4. The memory door's address is handed to both containers by
    name, so the build system reads and writes memory without any key."""
    helper, runner = _the_two_starts(
        sandbox, FLEET_MEMORY_MCP_URL="http://192.0.2.1:31822/mcp"
    )
    for call in (helper, runner):
        assert "--env FLEET_MEMORY_MCP_URL" in call, call
        assert "192.0.2.1:31822" not in call


def test_the_stamp_check_uses_the_shared_model_address(sandbox):
    """Test 6c. The stamp check's own model address is no longer handed in,
    so a value left pointing past the model door cannot bypass it: the check
    uses OPENAI_BASE_URL, the one model address in the sandbox."""
    helper, runner = _the_two_starts(
        sandbox, GUARDKIT_STAMP_MODEL_URL="http://192.0.2.1:4000/v1"
    )
    for call in (helper, runner):
        assert "--env GUARDKIT_STAMP_MODEL_URL" not in call, call
    assert "GUARDKIT_STAMP_MODEL_URL" not in _the_factory_list()


# ---------------------------------------------------------------------------
# The outer supervisor, outside the sandbox
# ---------------------------------------------------------------------------


def test_the_outer_supervisor_refuses_a_credential(root):
    """Test 5. Handed a bus password and a bus address with a password in it,
    the outer supervisor stops before the sandbox client is ever called, and
    its one sentence names both settings and neither value."""
    status, said = _run_the_supervisor(
        root,
        SANDBOX_ENV_NAMES="FORGE_IMAGE GUARDKIT_NATS_PASSWORD FLEET_MEMORY_NATS_URL",
        FORGE_IMAGE="a-throwaway-release:for-this-test",
        GUARDKIT_NATS_PASSWORD=SENTINEL,
        FLEET_MEMORY_NATS_URL=_a_url_with(SENTINEL, user="fleet-memory"),
    )
    assert status == 2, said
    assert "GUARDKIT_NATS_PASSWORD" in said and "FLEET_MEMORY_NATS_URL" in said, said
    assert SENTINEL not in said
    assert _client_calls(root) == [], "the sandbox client was called after all"


# ---------------------------------------------------------------------------
# The rule itself, the same in both scripts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [BOOTSTRAP, RUN_SH], ids=["start-script", "supervisor"])
def test_the_credential_words_match_forge(script):
    """Test 6. Both scripts use Forge's own credential words plus DSN, and
    excuse exactly one name: no other name, and no pattern."""
    assert sorted(_the_array(script, "CREDENTIAL_WORDS")) == sorted(
        (*RESERVED_CREDENTIAL_WORDS, "DSN")
    )
    assert _the_array(script, "CREDENTIAL_SHAPED_BUT_ALLOWED") == [THE_ONE_EXCUSED_NAME]


def test_a_start_with_every_retained_factory_setting_succeeds(sandbox, root):
    """Test 6a, the positive control beside the refusals. Every name the
    start script still hands in is set to an ordinary value (the token count
    to 4096), and so is every retained name of the example's list for the
    outer supervisor. Both start, and both hand on every name that was set.
    Without the one excused name this would fail."""
    retained = [
        name
        for name in _the_factory_list()
        if not _credential_shaped(name) or name == THE_ONE_EXCUSED_NAME
    ]
    assert THE_ONE_EXCUSED_NAME in retained
    values = {name: _ordinary_value_for(name) for name in retained}
    # These name files and folders the start script checks or makes before it
    # starts, and the test's own sandbox already sets the ones it needs.
    for name in ("FORGE_CONFIG_PATH", "FORGE_IMAGE_IDENTITY", "FORGE_RECEIPTS_DIR",
                 "FORGE_AUTOBUILD_WORKTREE_BASE", "FORGE_DEPLOY_NOTES_DIR"):
        values.pop(name, None)
    helper, runner = _the_two_starts(sandbox, **values)
    for call in (helper, runner):
        for name in values:
            assert f"--env {name}" in call, f"{name} was not handed in"

    example = (ESTATE / ".env.example").read_text()
    names = re.search(r"^SANDBOX_ENV_NAMES=(.*)$", example, re.M).group(1).split()
    kept = [n for n in names if not _credential_shaped(n) or n == THE_ONE_EXCUSED_NAME]
    if THE_ONE_EXCUSED_NAME not in kept:
        kept.append(THE_ONE_EXCUSED_NAME)
    given = {name: _ordinary_value_for(name) for name in kept}
    status, said = _run_the_supervisor(
        root, seconds=4, SANDBOX_ENV_NAMES=" ".join(kept), **given
    )
    assert status is None, f"the outer supervisor stopped ({status}):\n{said}"
    starts = [c for c in _client_calls(root) if c.endswith("the/projects/own/bootstrap")]
    assert starts, f"the bootstrap was never started:\n{said}"
    for name in kept:
        assert f"{name}={given[name]}" in starts[0], f"{name} was not handed in"


@pytest.mark.parametrize("value", ["4k", "1 2", ""], ids=["a-letter", "a-space", "empty"])
def test_the_token_count_takes_digits_only(sandbox, root, value):
    """Test 6b. The one excused name is excused only for digits. Anything else
    in it refuses the start, by name, in both scripts."""
    status, said = _start_expecting_it_to_end(
        sandbox, GUARDKIT_STAMP_MODEL_MAX_TOKENS=value
    )
    assert status == 2, said
    assert THE_ONE_EXCUSED_NAME in said
    assert not [call for call in _calls(sandbox) if call.startswith("run ")]

    status, said = _run_the_supervisor(
        root,
        SANDBOX_ENV_NAMES=THE_ONE_EXCUSED_NAME,
        GUARDKIT_STAMP_MODEL_MAX_TOKENS=value,
    )
    assert status == 2, said
    assert THE_ONE_EXCUSED_NAME in said
    assert _client_calls(root) == []


@pytest.mark.parametrize(
    "path", [ESTATE / ".env.example", ESTATE.parent / "compose" / ".env.example"],
    ids=["estate", "compose"],
)
def test_the_example_lists_hand_no_credential_in(path):
    """The example lists an operator copies hand none of the five in, nothing
    credential-shaped beyond the one excused name, and the memory door's
    address instead. Copied as they are, the supervisor starts."""
    names = re.search(r"^SANDBOX_ENV_NAMES=(.*)$", path.read_text(), re.M).group(1).split()
    for name in (*THE_FIVE, "GUARDKIT_STAMP_MODEL_URL"):
        assert name not in names, f"{path.name} still hands {name} into the sandbox"
    shaped = [n for n in names if _credential_shaped(n) and n != THE_ONE_EXCUSED_NAME]
    assert not shaped, shaped
    assert "FLEET_MEMORY_MCP_URL" in names


# ---------------------------------------------------------------------------
# The model door in the estate bundle
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def estate() -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    return yaml.safe_load(_rendered())


def test_the_model_door_holds_its_key_as_a_file(estate):
    """Test 7. The model door takes its key only as a secret file, never as a
    declared setting, runs as the image's own user with every capability
    dropped and no way to gain one, binds only the address the env file
    names, and publishes no port.

    It is NOT read-only, unlike the design's first wording: Compose refuses to
    copy a secret read from the environment into a read-only service, so a
    read-only door could not be given its key (found by starting a copy of it
    with a made-up key, 14 October 2026). This test holds that it stays the
    way that works rather than the way that cannot start."""
    services = estate["services"]
    assert "model-door" in services, "the estate has no model door"
    door = services["model-door"]

    declared = door.get("environment") or {}
    assert "SANDBOX_MODEL_KEY" not in declared
    assert "MODEL_DOOR_KEY" not in declared
    assert all("SANDBOX_MODEL_KEY" not in str(value) for value in declared.values())
    secrets = door.get("secrets") or []
    assert [s["source"] for s in secrets] == ["sandbox-model-key"], secrets
    assert estate["secrets"]["sandbox-model-key"]["environment"] == "SANDBOX_MODEL_KEY"

    assert not door.get("read_only"), (
        "Compose cannot hand a secret read from the environment to a "
        "read-only service, so the door would not start"
    )
    assert door.get("cap_drop") == ["ALL"]
    assert not door.get("cap_add")
    assert "no-new-privileges:true" in (door.get("security_opt") or [])
    assert not door.get("user"), "the door runs as the image's own unprivileged user"
    assert not door.get("privileged")
    assert door.get("network_mode") == "host"
    assert "ports" not in door
    assert door.get("restart") == "unless-stopped"
    assert "@sha256:" in door["image"], "the model door's image is not pinned by digest"

    # Its settings file is the bundle's own, bound read-only.
    binds = [v for v in door.get("volumes") or [] if v.get("type") == "bind"]
    assert len(binds) == 1, binds
    assert Path(binds[0]["source"]).resolve() == (ESTATE / "model-door.cfg").resolve()
    assert binds[0].get("read_only") is True

    config = (ESTATE / "model-door.cfg").read_text()
    bind_lines = [
        line.strip() for line in config.splitlines()
        if line.strip().startswith("bind ")
    ]
    assert bind_lines == ['bind "${MODEL_DOOR_BIND}"'], bind_lines
    assert 'http-request set-header Authorization "Bearer ${MODEL_DOOR_KEY}"' in config
    assert "capture request header" not in config
    assert "MODEL_DOOR_BIND" in declared


def test_the_model_door_image_agrees_with_the_pins_file():
    """The image the example names is the one the pins file pins."""
    pins = (ESTATE / "estate-pins.conf").read_text()
    image = re.search(r"^MODEL_DOOR_BASE_IMAGE=(\S+)$", pins, re.M).group(1)
    digest = re.search(r"^MODEL_DOOR_BASE_DIGEST=(sha256:[0-9a-f]{64})$", pins, re.M).group(1)
    example = (ESTATE / ".env.example").read_text()
    assert f"\nFACTORY_MODEL_DOOR_IMAGE={image}@{digest}\n" in example
    assert "\nMODEL_DOOR_PORT=" in example


def test_the_model_door_listens_only_on_the_gateway_address(estate):
    """Fix pass 1, item 6. The door's listening address is BUILT from the
    gateway address and the door's port, like every other gateway service's,
    so nobody can set it to every address of the machine by hand. There is no
    free-text MODEL_DOOR_BIND line to edit."""
    example = (ESTATE / ".env.example").read_text()
    gateway = re.search(r"^FACTORY_GATEWAY_ADDRESS=(\S+)$", example, re.M).group(1)
    port = re.search(r"^MODEL_DOOR_PORT=(\S+)$", example, re.M).group(1)
    door = estate["services"]["model-door"]
    assert door["environment"]["MODEL_DOOR_BIND"] == f"{gateway}:{port}"
    assert "\nMODEL_DOOR_BIND=" not in example
    compose = (ESTATE / "compose.yaml").read_text()
    assert "MODEL_DOOR_BIND: ${FACTORY_GATEWAY_ADDRESS:?" in compose


# ---------------------------------------------------------------------------
# The build's own launch list
# ---------------------------------------------------------------------------


def test_the_memory_door_address_is_on_the_launch_list():
    """Test 8. The build system is handed the memory door's address, so a
    build in the sandbox reads and writes memory through it."""
    assert "FLEET_MEMORY_MCP_URL" in launch_setting_names()
    env = build_launch_env(
        parent={"PATH": "/usr/bin", "FLEET_MEMORY_MCP_URL": "http://192.0.2.1:31822/mcp"}
    )
    assert env["FLEET_MEMORY_MCP_URL"] == "http://192.0.2.1:31822/mcp"


# ---------------------------------------------------------------------------
# Fix pass 1 (14 October 2026): the coach's findings
# ---------------------------------------------------------------------------

#: The rule's own function, as each script defines it, so many cases can be
#: put to both copies quickly. The end-to-end tests above and below prove the
#: scripts really call it.
_THE_RULE = re.compile(
    r"^CREDENTIAL_WORDS=.*?^why_this_setting_may_not_cross\(\) \{.*?^}\n", re.M | re.S
)


def _the_rule_says(script: Path, name: str, value: str | None, label: str = "A_LIST",
                   position: int = 1) -> str:
    """What one script's rule says about one setting: '' when it may cross."""
    rule = _THE_RULE.search(script.read_text())
    assert rule, f"{script.name} has no refusal rule"
    env = {"PATH": "/usr/bin:/bin"}
    if value is not None:
        env[name] = value
    done = subprocess.run(
        ["bash", "-c", rule.group(0) + '\nwhy_this_setting_may_not_cross "$@"',
         "the-rule", name, label, str(position)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


BOTH_SCRIPTS = pytest.mark.parametrize("script", [BOOTSTRAP, RUN_SH],
                                       ids=["start-script", "supervisor"])


@BOTH_SCRIPTS
@pytest.mark.parametrize("name", [
    "A_SERVICE_KEY", "KEY_FOR_THE_DOOR", "THE_KEY_HOLDER", "SANDBOX_MODEL_KEY",
    "LITELLM_MASTER_KEY", "AWS_ACCESS_KEY_ID", "a_service_key",
])
def test_a_name_with_key_as_a_word_is_refused(script, name):
    """Item 4: KEY as a whole word of a name, anywhere in it, is refused, by
    name. The model door's own key name is one of them."""
    said = _the_rule_says(script, name, "an-ordinary-value")
    assert name in said and "credential" in said, said
    assert "an-ordinary-value" not in said


@BOTH_SCRIPTS
@pytest.mark.parametrize("name", ["KEYBOARD_LAYOUT", "MONKEY_COUNT", "TURNKEYS"])
def test_key_inside_a_longer_word_is_not_a_key(script, name):
    """The control: KEY inside another word does not make a name a
    credential, so ordinary settings are not caught by the new word rule."""
    assert _the_rule_says(script, name, "an-ordinary-value") == ""


#: Values that carry a credential, each made-up. The sentinel must never be
#: printed. Each is refused by both scripts whatever the setting is called.
CREDENTIAL_VALUES = {
    "a-user-alone": f"https://{SENTINEL}@192.0.2.10/a-repository.git",
    "user-and-password": f"postgresql://someone:{SENTINEL}@192.0.2.10:5432/db",
    "an-empty-password": "https://someone:@192.0.2.10/",
    "percent-encoded": f"https%3A%2F%2Fsomeone%3A{SENTINEL}%40192.0.2.10%2F",
    "json-escaped": f"https:\\/\\/someone:{SENTINEL}@192.0.2.10\\/",
    "a-token-in-the-query": f"https://192.0.2.10/v1?model=a&access_token={SENTINEL}",
    "an-api-key-in-the-query": f"http://192.0.2.10:4000/v1?api_key={SENTINEL}",
    "an-auth-in-the-query": f"http://192.0.2.10:4000/v1?Auth={SENTINEL}",
    "a-pwd-in-the-query": f"http://192.0.2.10/?user=a;pwd={SENTINEL}",
    "a-libpq-password": f"host=192.0.2.10 dbname=memory password={SENTINEL}",
    "a-passwd-pair": f"passwd={SENTINEL}",
    "an-upper-case-password-pair": f"user=a PASSWORD = {SENTINEL}",
}


@BOTH_SCRIPTS
@pytest.mark.parametrize("value", list(CREDENTIAL_VALUES.values()),
                         ids=list(CREDENTIAL_VALUES))
def test_a_value_carrying_a_credential_is_refused_by_name(script, value):
    """Item 4: a URL with any user information, a URL whose query names a
    credential, and a password=-style pair are all refused, by the setting's
    name, and the value is never printed."""
    said = _the_rule_says(script, "FLEET_MEMORY_EMBED_URL", value)
    assert "FLEET_MEMORY_EMBED_URL" in said, said
    assert SENTINEL not in said


@BOTH_SCRIPTS
@pytest.mark.parametrize("value", [
    "http://192.0.2.1:31822/mcp",
    "http://192.0.2.1:9000/v1?model=an-embedding-model&dims=1024",
    "a-project@example.invalid",
    "an-ordinary-value-with=an-equals-sign",
    "flash-next-t06",
], ids=["the-memory-door", "a-plain-query", "an-email-address", "a-plain-pair", "a-model-name"])
def test_an_ordinary_value_still_crosses(script, value):
    """The control for the value rules: the addresses, names and numbers the
    factory really hands in are not caught."""
    assert _the_rule_says(script, "FLEET_MEMORY_EMBED_URL", value) == ""


def test_the_tightened_rules_reach_the_start_script(sandbox):
    """Item 4, end to end through the start script: a _KEY name, a URL with a
    user only, a token in a query string and a password= pair each stop the
    start, by name, and start nothing."""
    status, said = _start_expecting_it_to_end(
        sandbox,
        SANDBOX_CONTAINER_ENV_NAMES="A_SERVICE_KEY A_REPOSITORY_URL A_MODEL_URL A_DATABASE",
        A_SERVICE_KEY=SENTINEL,
        A_REPOSITORY_URL=CREDENTIAL_VALUES["a-user-alone"],
        A_MODEL_URL=CREDENTIAL_VALUES["a-token-in-the-query"],
        A_DATABASE=CREDENTIAL_VALUES["a-libpq-password"],
    )
    assert status == 2, said
    for name in ("A_SERVICE_KEY", "A_REPOSITORY_URL", "A_MODEL_URL", "A_DATABASE"):
        assert name in said, name
    assert SENTINEL not in said
    assert SENTINEL not in "\n".join(_calls(sandbox))
    assert not [call for call in _calls(sandbox) if call.startswith("run ")]


def test_the_tightened_rules_reach_the_outer_supervisor(root):
    """Item 4, end to end through the outer supervisor: the same four cases
    stop it before the sandbox client is called."""
    status, said = _run_the_supervisor(
        root,
        SANDBOX_ENV_NAMES="A_SERVICE_KEY A_REPOSITORY_URL A_MODEL_URL A_DATABASE",
        A_SERVICE_KEY=SENTINEL,
        A_REPOSITORY_URL=CREDENTIAL_VALUES["a-user-alone"],
        A_MODEL_URL=CREDENTIAL_VALUES["a-token-in-the-query"],
        A_DATABASE=CREDENTIAL_VALUES["a-libpq-password"],
    )
    assert status == 2, said
    for name in ("A_SERVICE_KEY", "A_REPOSITORY_URL", "A_MODEL_URL", "A_DATABASE"):
        assert name in said, name
    assert SENTINEL not in said
    assert _client_calls(root) == []


def test_a_bad_entry_is_named_by_its_position_not_its_text(sandbox, root):
    """Item 5: an entry that is not a setting name (say an operator wrote
    NAME=value into a list) is refused by the list's name and the entry's
    position, and its text, which may hold a value, is never printed."""
    entry = f"A_SETTING={SENTINEL}"
    status, said = _start_expecting_it_to_end(
        sandbox, SANDBOX_CONTAINER_ENV_NAMES=f"FLEET_MEMORY_ENABLED {entry}"
    )
    assert status == 2, said
    assert "an entry in SANDBOX_CONTAINER_ENV_NAMES is not a setting name (position 2)" in said
    assert SENTINEL not in said

    status, said = _run_the_supervisor(root, SANDBOX_ENV_NAMES=f"FORGE_IMAGE {entry}")
    assert status == 2, said
    assert "an entry in SANDBOX_ENV_NAMES is not a setting name (position 2)" in said
    assert SENTINEL not in said
    assert _client_calls(root) == []


# --- the model door's own entry shell, run in the pinned image ------------

def _the_pinned_door_image() -> str:
    pins = (ESTATE / "estate-pins.conf").read_text()
    image = re.search(r"^MODEL_DOOR_BASE_IMAGE=(\S+)$", pins, re.M).group(1)
    digest = re.search(r"^MODEL_DOOR_BASE_DIGEST=(\S+)$", pins, re.M).group(1)
    return f"{image.split(':')[0]}@{digest}"


@pytest.fixture(scope="module")
def door_entry(estate) -> list[str]:
    """The door's entry shell exactly as the estate renders it, ready to run
    in the pinned image with a harmless command in place of HAProxy."""
    image = _the_pinned_door_image()
    held = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if held.returncode != 0:
        pytest.skip("the pinned HAProxy image is not on this machine; nothing is fetched")
    entry = [part.replace("$$", "$") for part in estate["services"]["model-door"]["entrypoint"]]
    return [image, *entry]


def _start_the_door(door_entry, tmp_path: Path, key: bytes, bind: str):
    """Run the door's entry shell with a key file and a listening address;
    no network, every capability dropped, and `true` in place of HAProxy."""
    image, *entry = door_entry
    key_file = tmp_path / "sandbox_model_key"
    key_file.write_bytes(key)
    key_file.chmod(0o444)
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
         "-e", f"MODEL_DOOR_BIND={bind}",
         "-v", f"{key_file}:/run/secrets/sandbox_model_key:ro",
         "--entrypoint", entry[0], image, *entry[1:], "true"],
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("key", [b"\n", b"  \t\n", b"made-up key with spaces\n"],
                         ids=["only-a-newline", "only-whitespace", "whitespace-inside"])
def test_the_door_refuses_a_key_that_is_blank_or_broken(door_entry, tmp_path, key):
    """Item 2: a key file holding only a newline, only whitespace, or a key
    with whitespace in it stops the door at its start, instead of starting a
    door that sends an empty key and fails every model call later."""
    done = _start_the_door(door_entry, tmp_path, key, "192.0.2.1:31823")
    assert done.returncode != 0, done.stdout + done.stderr
    assert "the model door" in done.stderr
    assert "made-up" not in done.stdout + done.stderr


@pytest.mark.parametrize("bind", ["0.0.0.0:31823", "[::]:31823", ":::31823", ":31823", "*:31823"],
                         ids=["every-ipv4-address", "every-ipv6-address", "every-ipv6-bare",
                              "no-host", "a-wildcard"])
def test_the_door_refuses_to_listen_everywhere(door_entry, tmp_path, bind):
    """Item 6: whatever reaches it, the door refuses to listen on every
    address of the machine."""
    done = _start_the_door(door_entry, tmp_path, b"made-up-door-key-for-the-test\n", bind)
    assert done.returncode != 0, done.stdout + done.stderr
    assert "the model door" in done.stderr
    assert "made-up" not in done.stdout + done.stderr


def test_the_door_starts_with_a_good_key_and_the_gateway_address(door_entry, tmp_path):
    """The control for the two refusals above."""
    done = _start_the_door(door_entry, tmp_path, b"made-up-door-key-for-the-test\n",
                           "192.0.2.1:31823")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_estate_readme_documents_the_model_door():
    """Item 3: the door's own error message sends an operator to the README,
    so the README says what the door is, which secret it needs and where it
    listens, and no longer claims the bundle has no host network."""
    readme = (ESTATE / "README.md").read_text()
    for needed in ("model-door", "SANDBOX_MODEL_KEY", "MODEL_DOOR_PORT", "model-door.cfg"):
        assert needed in readme, needed
    assert "no checkout and no host network" not in readme
    assert "a secret the sandbox's containers get by name" not in readme
