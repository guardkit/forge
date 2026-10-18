"""The Git door in the estate bundle (17 October 2026).

Design "Building production features from a signed-off prototype", section
2.8: a stock HAProxy at the gateway address that lets the sandbox READ the
private repositories on its allow-list, adding a read-only token of its own,
and refuses everything else, a push included. Built like the model door,
behind its own profile so it starts only when a machine switches it on.

What is proven here: the rendered service's shape; that it is off by
default; that its image is the pinned one; and, in the pinned image with no
network at all, that its entry shell refuses to start without a usable token
or allow-list, that HAProxy accepts its settings, and that only git's two
read requests for a listed repository are passed on, with the caller's
credential replaced by the door's. Since one sandbox per project (17
October 2026) the door listens on one port per sandbox and its allow-list is
keyed by the port a request arrived on: a repository listed for one port is
refused on another. **No service, sandbox, GitHub or real token is touched**;
every token, owner and name is made up.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.forge.deploy.test_the_estate_bundle import (
    _IN_IMAGE_HOMES,
    ESTATE,
    _lines_naming_a_home,
    _rendered,
)

MADE_UP_TOKEN = b"made-up-door-token-TESTONLY\n"
LISTED = "example-owner/listed-repo"
#: Two sandboxes' ports, and a repository listed for each.
PORT_A, PORT_B = 18080, 18081
OTHER = "example-owner/other-sandboxes-repo"


@pytest.fixture(scope="module")
def with_the_door() -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    return yaml.safe_load(_rendered(profiles="local-bus,git-door"))


@pytest.fixture(scope="module")
def without_the_door() -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    return yaml.safe_load(_rendered())


def test_with_the_door_on_the_bundle_still_names_no_home():
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    for line in _lines_naming_a_home(_rendered(profiles="local-bus,git-door")):
        assert any(home in line for home in _IN_IMAGE_HOMES), line.strip()


def test_the_door_is_off_unless_its_profile_is_on(without_the_door, with_the_door):
    assert "git-door" not in without_the_door["services"]
    assert "git-door" in with_the_door["services"]
    assert with_the_door["services"]["git-door"].get("profiles") == ["git-door"]


def test_the_door_holds_its_token_as_a_file_and_is_hardened_like_the_model_door(with_the_door):
    door = with_the_door["services"]["git-door"]
    declared = door.get("environment") or {}
    assert set(declared) == {"GIT_DOOR_BIND"}
    assert all("TOKEN" not in str(v) for v in declared.values())
    assert [s["source"] for s in door.get("secrets") or []] == ["git-door-token"]
    assert with_the_door["secrets"]["git-door-token"]["environment"] == "GIT_DOOR_TOKEN"

    assert door.get("cap_drop") == ["ALL"]
    assert not door.get("cap_add")
    assert "no-new-privileges:true" in (door.get("security_opt") or [])
    assert not door.get("user") and not door.get("privileged")
    assert door.get("network_mode") == "host"
    assert "ports" not in door
    assert door.get("restart") == "unless-stopped"
    assert "@sha256:" in door["image"]

    binds = {v["target"]: v for v in door.get("volumes") or [] if v.get("type") == "bind"}
    assert set(binds) == {
        "/usr/local/etc/haproxy/git-door.cfg",
        "/usr/local/etc/haproxy/git-door.repos",
    }
    assert all(b.get("read_only") is True for b in binds.values())
    assert Path(binds["/usr/local/etc/haproxy/git-door.cfg"]["source"]).resolve() == (
        ESTATE / "git-door.cfg"
    ).resolve()
    example = (ESTATE / ".env.example").read_text()
    repos = re.search(r"^GIT_DOOR_REPOS_FILE=(\S+)$", example, re.M).group(1)
    assert binds["/usr/local/etc/haproxy/git-door.repos"]["source"] == repos
    assert not repos.startswith("/home/")


def test_the_door_listens_only_on_the_gateway_address_and_its_own_port(with_the_door):
    example = (ESTATE / ".env.example").read_text()
    gateway = re.search(r"^FACTORY_GATEWAY_ADDRESS=(\S+)$", example, re.M).group(1)
    port = re.search(r"^GIT_DOOR_PORTS=(\S+)$", example, re.M).group(1)
    door = with_the_door["services"]["git-door"]
    assert door["environment"]["GIT_DOOR_BIND"] == f"{gateway}:{port}"
    assert port == "31831", "the example's port is slot 1's, 31830 + 1"
    others = {
        re.search(rf"^{name}=(\S+)$", example, re.M).group(1)
        for name in ("MODEL_DOOR_PORT", "FLEET_MEMORY_SANDBOX_PORT", "FLEET_MEMORY_PORT")
    }
    assert port not in others, "the Git door has a gateway port of its own"
    assert "\nGIT_DOOR_BIND=" not in example


@pytest.mark.parametrize("settings, bind", [
    ({"GIT_DOOR_PORTS": "31831,31832"}, "192.0.2.1:31831,31832"),
    ({"GIT_DOOR_PORTS": "", "GIT_DOOR_PORT": "31825"}, "192.0.2.1:31825"),
], ids=["one-port-per-sandbox", "the-single-port-of-before"])
def test_the_door_takes_every_sandbox_s_port_and_still_the_single_one(settings, bind):
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here, so there is nothing to render")
    from tests.forge.deploy.test_the_estate_bundle import _render

    done = _render(profiles="local-bus,git-door", settings=settings)
    assert done.returncode == 0, done.stderr
    assert yaml.safe_load(done.stdout)["services"]["git-door"]["environment"]["GIT_DOOR_BIND"] == bind


def test_the_door_image_agrees_with_the_pins_file():
    pins = (ESTATE / "estate-pins.conf").read_text()
    image = re.search(r"^MODEL_DOOR_BASE_IMAGE=(\S+)$", pins, re.M).group(1)
    digest = re.search(r"^MODEL_DOOR_BASE_DIGEST=(sha256:[0-9a-f]{64})$", pins, re.M).group(1)
    example = (ESTATE / ".env.example").read_text()
    assert f"\nFACTORY_GIT_DOOR_IMAGE={image}@{digest}\n" in example


def test_the_settings_allow_only_the_two_reads_and_log_no_header():
    config = (ESTATE / "git-door.cfg").read_text()
    bind_lines = [ln.strip() for ln in config.splitlines() if ln.strip().startswith("bind ")]
    assert bind_lines == ['bind "${GIT_DOOR_LISTEN}"']
    assert "http-request set-var(txn.key) dst_port,concat(/,txn.repo)" in config
    assert "acl listed        var(txn.key) -m str -f" in config
    assert 'http-request set-header Authorization "Basic ${GIT_DOOR_BASIC}"' in config
    assert "http-request del-header Authorization" in config
    assert "capture request header" not in config
    assert "git-receive-pack" not in config.split("frontend", 1)[1].replace(
        "a push (git-receive-pack)", ""
    )
    assert "ssl verify required" in config and "verifyhost github.com" in config
    assert "sni str(github.com)" in config


def test_the_estate_readme_documents_the_git_door():
    readme = (ESTATE / "README.md").read_text()
    assert "## The Git door" in readme
    for needed in ("git-door", "GIT_DOOR_TOKEN", "GIT_DOOR_PORTS", "GIT_DOOR_REPOS_FILE",
                   "git-door.cfg", "--via-git-door", "allow-list", "COMPOSE_PROFILES",
                   "<port>/<owner>/<name>", "--slot"):
        assert needed in readme, needed


# --- the door's own entry shell and settings, run in the pinned image ------


@pytest.fixture(scope="module")
def door_entry(with_the_door) -> list[str]:
    pins = (ESTATE / "estate-pins.conf").read_text()
    image = re.search(r"^MODEL_DOOR_BASE_IMAGE=(\S+)$", pins, re.M).group(1)
    digest = re.search(r"^MODEL_DOOR_BASE_DIGEST=(\S+)$", pins, re.M).group(1)
    pinned = f"{image.split(':')[0]}@{digest}"
    held = subprocess.run(["docker", "image", "inspect", pinned], capture_output=True)
    if held.returncode != 0:
        pytest.skip("the pinned HAProxy image is not on this machine; nothing is fetched")
    entry = [p.replace("$$", "$") for p in with_the_door["services"]["git-door"]["entrypoint"]]
    return [pinned, *entry]


def _run_the_door(door_entry, tmp_path: Path, *command: str, token: bytes | None = MADE_UP_TOKEN,
                  repos: str | None = f"# the allow-list\n{PORT_A}/{LISTED}\n", script: str | None = None,
                  bind: str = f"127.0.0.1:{PORT_A}"):
    image, *entry = door_entry
    mounts = ["-v", f"{ESTATE / 'git-door.cfg'}:/usr/local/etc/haproxy/git-door.cfg:ro"]
    if token is not None:
        token_file = tmp_path / "git_door_token"
        token_file.write_bytes(token)
        token_file.chmod(0o444)
        mounts += ["-v", f"{token_file}:/run/secrets/git_door_token:ro"]
    if repos is not None:
        repos_file = tmp_path / "git-door.repos"
        repos_file.write_text(repos)
        repos_file.chmod(0o444)
        mounts += ["-v", f"{repos_file}:/usr/local/etc/haproxy/git-door.repos:ro"]
    if script is not None:
        probe = tmp_path / "probe.sh"
        probe.write_text(script)
        probe.chmod(0o444)
        mounts += ["-v", f"{probe}:/probe.sh:ro"]
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges:true",
         "-e", f"GIT_DOOR_BIND={bind}", *mounts,
         "--entrypoint", entry[0], image, *entry[1:], *command],
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("token", [None, b"", b"\n", b"made up token with spaces\n"],
                         ids=["no-file", "empty", "only-a-newline", "whitespace-inside"])
def test_the_door_refuses_to_start_without_a_usable_token(door_entry, tmp_path, token):
    done = _run_the_door(door_entry, tmp_path, "true", token=token)
    assert done.returncode != 0
    assert "the Git door" in done.stderr
    assert "made up" not in done.stdout + done.stderr


@pytest.mark.parametrize("repos, said", [
    (None, "no allow-list"),
    ("# only a comment\n\n", "names no repository"),
    (f"{PORT_A}/{LISTED}\nnot a repository line\n", "not <port>/<owner>/<name>"),
    (f"{PORT_A}/{LISTED}\n{PORT_A}/owner/name/extra\n", "not <port>/<owner>/<name>"),
    (f"{LISTED}\n", "not <port>/<owner>/<name>"),
    (f"0{PORT_A}/{LISTED}\n", "not <port>/<owner>/<name>"),
    (f"{PORT_B}/{LISTED}\n", f"names the port {PORT_B}, which the door does not listen on"),
    (f"{PORT_A}/{LISTED}\n18082/Example-Owner/Listed-Repo\n", "for more than one port"),
], ids=["no-file", "nothing-listed", "a-bad-line", "four-parts", "no-port-the-old-form",
        "a-port-written-with-a-zero", "a-port-the-door-does-not-listen-on", "one-repo-two-ports"])
def test_the_door_refuses_to_start_without_a_usable_allow_list(door_entry, tmp_path, repos, said):
    done = _run_the_door(door_entry, tmp_path, "true", repos=repos, bind=f"127.0.0.1:{PORT_A},18082")
    assert done.returncode != 0
    assert said in done.stderr


@pytest.mark.parametrize("bind, said", [
    (f"127.0.0.1:{PORT_A},{PORT_A}", "named twice"),
    (f"127.0.0.1:{PORT_A},", "comma-separated list"),
    (f"127.0.0.1:,{PORT_A}", "comma-separated list"),
    (f"127.0.0.1:{PORT_A},0{PORT_B}", "not a number from 1 to 65535"),
    (f"127.0.0.1:{PORT_A},70000", "not a number from 1 to 65535"),
    ("127.0.0.1:", "it has no port"),
], ids=["a-port-twice", "a-trailing-comma", "a-leading-comma", "a-leading-zero",
        "too-big", "no-port"])
def test_the_door_refuses_a_port_list_that_is_not_plain(door_entry, tmp_path, bind, said):
    done = _run_the_door(door_entry, tmp_path, "true", bind=bind)
    assert done.returncode != 0 and said in done.stderr, done.stderr


def test_the_door_refuses_to_listen_everywhere(door_entry, tmp_path):
    image, *entry = door_entry
    done = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
         "-e", "GIT_DOOR_BIND=0.0.0.0:18080",
         "--entrypoint", entry[0], image, *entry[1:], "true"],
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode != 0 and "the Git door listens only" in done.stderr


def test_haproxy_accepts_the_door_s_settings(door_entry, tmp_path):
    done = _run_the_door(door_entry, tmp_path, "haproxy", "-c", "-f",
                         "/usr/local/etc/haproxy/git-door.cfg")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "made-up-door-token" not in done.stdout + done.stderr


#: Each request, and what the door does with it with no network: 503 means
#: it was passed on (github.com cannot be reached here); 403 means refused.
PROBES = {
    "refs-of-a-listed-repo": (f"GET /{LISTED}.git/info/refs?service=git-upload-pack", "503"),
    "pack-of-a-listed-repo": (f"POST /{LISTED}.git/git-upload-pack", "503"),
    "push-refs": (f"GET /{LISTED}.git/info/refs?service=git-receive-pack", "403"),
    "push-pack": (f"POST /{LISTED}.git/git-receive-pack", "403"),
    "an-unlisted-repo": ("GET /example-owner/other-repo.git/info/refs?service=git-upload-pack", "403"),
    "a-longer-name": (f"GET /{LISTED}2.git/info/refs?service=git-upload-pack", "403"),
    "another-case": ("GET /Example-owner/listed-repo.git/info/refs?service=git-upload-pack", "403"),
    "pack-with-a-query": (f"POST /{LISTED}.git/git-upload-pack?x=1", "403"),
    "refs-with-more-query": (f"GET /{LISTED}.git/info/refs?service=git-upload-pack&x=1", "403"),
    "pack-by-get": (f"GET /{LISTED}.git/git-upload-pack", "403"),
    "refs-by-head": (f"HEAD /{LISTED}.git/info/refs?service=git-upload-pack", "403"),
    "the-api": (f"GET /repos/{LISTED}", "403"),
    "a-climb": (f"GET /example-owner/../{LISTED}.git/info/refs?service=git-upload-pack", "403"),
    # Coach review, 17 October 2026.
    "an-encoded-slash": ("GET /example-owner%2Flisted-repo.git/info/refs?service=git-upload-pack", "403"),
    "an-archive": (f"POST /{LISTED}.git/git-upload-archive", "403"),
    "an-absolute-url": (
        f"GET http://github.com/{LISTED}.git/info/refs?service=git-upload-pack", "403"
    ),
}


def test_a_repository_is_read_only_on_its_own_sandbox_s_port(door_entry, tmp_path):
    """Two sandboxes' ports, one repository listed for each: each repository
    is passed on through its own port and refused on the other's."""
    probes = {
        "a-on-its-own-port": (PORT_A, LISTED, "503"),
        "a-on-the-other-port": (PORT_B, LISTED, "403"),
        "b-on-its-own-port": (PORT_B, OTHER, "503"),
        "b-on-the-other-port": (PORT_A, OTHER, "403"),
    }
    lines = ["haproxy -D -f /usr/local/etc/haproxy/git-door.cfg -p /tmp/door.pid 2>/dev/null",
             "sleep 1"]
    for name, (port, repo, _) in probes.items():
        lines.append(
            f"printf 'GET /{repo}.git/info/refs?service=git-upload-pack HTTP/1.1\\r\\nHost: github.com"
            f"\\r\\nConnection: close\\r\\n\\r\\n' | nc -w 3 127.0.0.1 {port} | head -1 | sed 's/^/{name} /'"
        )
    done = _run_the_door(
        door_entry, tmp_path, "sh", "/probe.sh", script="\n".join(lines) + "\n",
        bind=f"127.0.0.1:{PORT_A},{PORT_B}", repos=f"{PORT_A}/{LISTED}\n{PORT_B}/{OTHER}\n",
    )
    assert done.returncode == 0, done.stderr
    answered = {
        line.split()[0]: line.split()[2]
        for line in done.stdout.splitlines() if line.strip() and len(line.split()) > 2
    }
    assert answered == {name: status for name, (_, _, status) in probes.items()}, done.stdout


def test_only_the_two_reads_of_a_listed_repository_are_passed_on(door_entry, tmp_path):
    lines = ["haproxy -D -f /usr/local/etc/haproxy/git-door.cfg -p /tmp/door.pid 2>/dev/null",
             "sleep 1"]
    for name, (line, _) in PROBES.items():
        length = "Content-Length: 0\\r\\n" if line.startswith("POST") else ""
        lines.append(
            f"printf '%s HTTP/1.1\\r\\nHost: github.com\\r\\nAuthorization: Basic Y2FsbGVy\\r\\n{length}"
            f"Connection: close\\r\\n\\r\\n' '{line}' | nc -w 3 127.0.0.1 18080 | head -1 "
            f"| sed 's/^/{name} /'"
        )
    done = _run_the_door(door_entry, tmp_path, "sh", "/probe.sh", script="\n".join(lines) + "\n")
    assert done.returncode == 0, done.stderr
    answered = {
        line.split()[0]: line.split()[2]
        for line in done.stdout.splitlines() if line.strip() and len(line.split()) > 2
    }
    assert answered == {name: status for name, (_, status) in PROBES.items()}, done.stdout


def test_the_caller_s_credential_is_replaced_by_the_door_s(door_entry, tmp_path):
    """The door's settings with only the upstream changed to a listener inside
    the same container, to read exactly what would be sent to GitHub."""
    config = (ESTATE / "git-door.cfg").read_text()
    local = re.sub(r"^    server github .*$", "    server github 127.0.0.1:19090", config,
                   flags=re.M)
    assert local != config
    local_cfg = tmp_path / "local.cfg"
    local_cfg.write_text(local)
    local_cfg.chmod(0o444)
    script = "\n".join([
        "haproxy -D -f /local.cfg -p /tmp/door.pid 2>/dev/null",
        "( nc -l -p 19090 > /tmp/upstream.txt & )",
        "sleep 1",
        f"printf 'GET /{LISTED}.git/info/refs?service=git-upload-pack HTTP/1.1\\r\\nHost: x\\r\\n"
        "Authorization: Basic Y2FsbGVy\\r\\nCookie: c=1\\r\\nConnection: close\\r\\n\\r\\n' "
        "| nc -w 2 127.0.0.1 18080 >/dev/null",
        "sleep 1",
        "cat /tmp/upstream.txt",
    ]) + "\n"
    image, *entry = door_entry
    (tmp_path / "git_door_token").write_bytes(MADE_UP_TOKEN)
    (tmp_path / "git-door.repos").write_text(f"{PORT_A}/{LISTED}\n")
    (tmp_path / "probe.sh").write_text(script)
    for name in ("git_door_token", "git-door.repos", "probe.sh"):
        (tmp_path / name).chmod(0o444)
    done = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
         "-e", "GIT_DOOR_BIND=127.0.0.1:18080",
         "-v", f"{local_cfg}:/local.cfg:ro",
         "-v", f"{tmp_path / 'git_door_token'}:/run/secrets/git_door_token:ro",
         "-v", f"{tmp_path / 'git-door.repos'}:/usr/local/etc/haproxy/git-door.repos:ro",
         "-v", f"{tmp_path / 'probe.sh'}:/probe.sh:ro",
         "--entrypoint", entry[0], image, *entry[1:], "sh", "/probe.sh"],
        capture_output=True, text=True, timeout=120,
    )
    import base64

    sent = done.stdout.lower()
    expected = base64.b64encode(b"x-access-token:" + MADE_UP_TOKEN.strip()).decode().lower()
    assert f"authorization: basic {expected}" in sent, done.stdout + done.stderr
    assert "y2fsbgvy" not in sent, "the caller's own credential is not passed on"
    assert "cookie" not in sent
    assert "host: github.com" in sent
