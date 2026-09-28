from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "deploy" / "estate" / "publisher-host-policy"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("publisher_host_policy", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def nft_document(module, project="chosen-project"):
    bound = module.binding(project)
    common = [
        {"match": {"op": "==", "left": {"meta": {"key": "oifname"}}, "right": bound["bridge"]}},
        {"match": {"op": "==", "left": {"meta": {"key": "l4proto"}}, "right": "tcp"}},
        {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": 8711}},
    ]
    return {"nftables": [
        {"metainfo": {"json_schema_version": 1}},
        {"table": {"family": "inet", "name": bound["table"], "handle": 12}},
        {"chain": {"family": "inet", "table": bound["table"], "name": "host_output",
                   "type": "filter", "hook": "output", "prio": -5, "policy": "accept", "handle": 1}},
        {"rule": {"family": "inet", "table": bound["table"], "chain": "host_output", "handle": 2,
                  "expr": [*common,
                           {"match": {"op": "==", "left": {"ct": {"key": "direction"}}, "right": "reply"}},
                           {"return": None}]}},
        {"rule": {"family": "inet", "table": bound["table"], "chain": "host_output", "handle": 3,
                  "expr": [*common, {"counter": {"packets": 4, "bytes": 240}}, {"drop": None}]}},
    ]}


def test_describe_is_filesystem_only_and_project_derived() -> None:
    result = subprocess.run(
        [sys.executable, str(HELPER), "describe", "--project", "forge-estate-example"],
        text=True, capture_output=True, check=False,
        env={"PATH": "/nonexistent", "DOCKER_HOST": "tcp://must-not-be-read"},
    )
    assert result.returncode == 0, result.stderr
    described = json.loads(result.stdout)
    assert described["FORGE_PUBLISHER_BRIDGE"] == "fpbf1a26ceba1cc"
    assert described["table"] == "forge_pub_f1a26ceba1cc"
    assert described["port"] == 8711


def test_fixed_serialization_has_only_the_two_ordered_output_rules() -> None:
    module = load_helper(); bound = module.binding("chosen-project")
    text = module.nft_text(bound)
    assert "hook output priority -5; policy accept" in text
    assert text.index("ct direction reply return") < text.index("counter drop")
    assert text.count("tcp dport 8711") == 2
    assert "forward" not in text.lower()
    assert bound["bridge"] in text


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["nftables"].pop(),
        lambda d: d["nftables"].append({"chain": {"family": "inet", "table": "extra", "name": "x"}}),
        lambda d: d["nftables"][2].update(chain={**d["nftables"][2]["chain"], "hook": "forward"}),
        lambda d: d["nftables"][2].update(chain={**d["nftables"][2]["chain"], "prio": 0}),
        lambda d: d["nftables"][3]["rule"]["expr"][2]["match"].update(right=8712),
        lambda d: d["nftables"][3]["rule"]["expr"][0]["match"].update(right="wrongbridge"),
        lambda d: d["nftables"][3]["rule"]["expr"][3]["match"].update(right="original"),
        lambda d: d["nftables"][3]["rule"]["expr"].reverse(),
    ],
)
def test_semantic_readback_refuses_missing_changed_extra_and_wrong_shape(change) -> None:
    module = load_helper(); document = nft_document(module)
    change(document)
    with pytest.raises(module.Refusal):
        module.verify_semantics(document, module.binding("chosen-project"))


def test_semantic_readback_accepts_only_counter_and_handle_changes() -> None:
    module = load_helper(); document = nft_document(module)
    module.verify_semantics(document, module.binding("chosen-project"))
    document["nftables"][4]["rule"]["handle"] = 999
    document["nftables"][4]["rule"]["expr"][-2]["counter"] = {"packets": 99, "bytes": 12345}
    module.verify_semantics(document, module.binding("chosen-project"))


def test_load_static_uses_no_docker(monkeypatch, tmp_path: Path) -> None:
    module = load_helper(); bound = module.binding("chosen-project")
    calls = []
    monkeypatch.setattr(module, "require_root", lambda: None)
    monkeypatch.setattr(module, "load_config", lambda path: (bound, {"fixed": True}))
    monkeypatch.setattr(module, "lock", lambda value: open(tmp_path / "lock", "a"))
    monkeypatch.setattr(module.fcntl, "flock", lambda *args: None)
    monkeypatch.setattr(module, "apply_policy", lambda value: calls.append(("nft", value)))
    monkeypatch.setattr(module, "docker_json", lambda *args: (_ for _ in ()).throw(AssertionError("Docker called")))
    module.cmd_load_static(type("Args", (), {"config": Path("/etc/forge-publisher-policy/x.json")})())
    assert calls == [("nft", bound)]


def test_failed_readback_restores_only_the_previous_owned_table(monkeypatch) -> None:
    module = load_helper(); bound = module.binding("chosen-project"); calls = []
    previous = f"table inet {bound['table']} {{\n chain old {{ }}\n}}\n"

    def fake_run(argv, *, input_text=None, env=None, check=True):
        calls.append((argv, input_text))
        return subprocess.CompletedProcess(argv, 0, previous if argv[:2] == ["nft", "list"] else "", "")

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "nft_json", lambda table: {"nftables": []})
    with pytest.raises(module.Refusal):
        module.apply_policy(bound)
    scripts = [body for argv, body in calls if argv[:3] == ["nft", "-f", "-"]]
    assert scripts[-1] == f"delete table inet {bound['table']}\n{previous}"
    assert "flush ruleset" not in "".join(body or "" for _, body in calls)


def test_project_endpoint_and_environment_collisions_refuse(monkeypatch) -> None:
    module = load_helper()
    with pytest.raises(module.Refusal): module.binding("Wrong Project")
    with pytest.raises(module.Refusal): module.clean_docker_env("tcp://127.0.0.1:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "another")
    with pytest.raises(module.Refusal): module.clean_docker_env(module.DOCKER_HOST)


def test_compose_and_operator_paths_share_one_contract() -> None:
    fragment = (ROOT / "src/forge/publisher/compose-fragment.yml").read_text()
    settings = (ROOT / "deploy/estate/rollout-settings").read_text()
    estate = (ROOT / "deploy/estate/README.md").read_text()
    component = (ROOT / "deploy/compose/README.md").read_text()
    assert "com.docker.network.bridge.name: ${FORGE_PUBLISHER_BRIDGE:?" in fragment
    assert "enable_ipv6: false" in fragment
    assert "expected_bridge = \"fpb\" + hashlib.sha256(project.encode" in settings
    assert estate.count("--project-name \"$FACTORY_ESTATE_PROJECT\"") == 2
    assert '-f "$FACTORY_ESTATE_DIR/compose.yaml" -f "$FACTORY_ESTATE_DIR/compose.external-bus.yaml" up -d' in estate
    assert "not a supported standalone" in " ".join(component.split())
    assert "docker compose --env-file .env --profile sandbox" not in estate
    assert "do not pass" in estate and "--profile sandbox" in estate


def test_current_policy_is_rechecked_before_receipt_success_and_recovery_up() -> None:
    check = (ROOT / "deploy/estate/estate-check").read_text()
    reader = check[check.index("check_read_pre_resume()") :]
    assert reader.index("publisher_policy_verify members") < reader.index("MAY BE ACTED ON")
    back = (ROOT / "deploy/estate/rollout-back").read_text()
    after = back[back.index("def after(self):") :]
    assert after.index("publisher-host-policy") < after.index("'up','--no-deps'")


def test_removal_keeps_shared_artifacts_and_targets_only_owned_names() -> None:
    source = HELPER.read_text()
    remove = source[source.index("def cmd_remove") : source.index("def parser")]
    assert "HELPER.unlink" not in remove and "UNIT.unlink" not in remove
    assert "shared helper and unit retained" in remove
    assert "delete\", \"table\", \"inet\", bound[\"table\"]" in remove
    assert "flush ruleset" not in source
