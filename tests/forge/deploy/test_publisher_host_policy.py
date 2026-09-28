from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import copy
import tempfile
import os
from types import SimpleNamespace
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
    monkeypatch.setattr(module, "load_config", lambda path: (bound, {"persistent": True}))
    monkeypatch.setattr(module, "lock", lambda value: open(tmp_path / "lock", "a"))
    monkeypatch.setattr(module.fcntl, "flock", lambda *args: None)
    monkeypatch.setattr(module, "apply_policy", lambda value, config: calls.append(("nft", value)))
    monkeypatch.setattr(module, "persistence_preflight", lambda *args: None)
    monkeypatch.setattr(module, "owned_policy", lambda *args: False)
    monkeypatch.setattr(module, "docker_json", lambda *args: (_ for _ in ()).throw(AssertionError("Docker called")))
    module.cmd_load_static(type("Args", (), {"config": Path("/etc/forge-publisher-policy/x.json")})())
    assert calls == [("nft", bound)]


def test_failed_readback_restores_only_the_previous_owned_table(monkeypatch) -> None:
    module = load_helper(); bound = module.binding("chosen-project"); calls = []
    snapshots = iter([nft_document(module), {"nftables": []}])
    def fake_run(argv, *, input_text=None, env=None, check=True):
        calls.append((argv, input_text))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "nft_json", lambda table: next(snapshots))
    with pytest.raises(module.Refusal):
        module.apply_policy(bound, module.canonical_config(bound, "daemon", module.DOCKER_HOST, False))
    scripts = [body for argv, body in calls if argv[:3] == ["nft", "-f", "-"]]
    assert len(scripts) == 2
    assert scripts[-1] == module.transaction(bound, old_exists=True)
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


@pytest.mark.parametrize("output, count", [("", 0), ('{"ID":"one","Name":"n"}\n', 1), ('{"ID":"one","Name":"n"}\n{"ID":"two","Name":"m"}\n', 2)])
def test_real_docker_list_stream_shape(monkeypatch, output, count):
    m = load_helper()
    monkeypatch.setattr(m, "clean_docker_env", lambda host: {})
    monkeypatch.setattr(m, "run", lambda *a, **k: SimpleNamespace(stdout=output))
    assert len(m.docker_rows(m.DOCKER_HOST, "network", "ls")) == count


@pytest.mark.parametrize("output", ['[]', '{}', 'null', '{"ID":"one"}', '{"ID":"one","Name":"n"}\nnot-json', '\n', '{"ID":7,"Name":"n"}'])
def test_malformed_list_data_is_not_absence(monkeypatch, output):
    m = load_helper(); monkeypatch.setattr(m, "clean_docker_env", lambda host: {})
    monkeypatch.setattr(m, "run", lambda *a, **k: SimpleNamespace(stdout=output))
    with pytest.raises(m.Refusal): m.docker_rows(m.DOCKER_HOST, "network", "ls")


@pytest.mark.parametrize("output", ['{}', '[]', '[null]', '[{},{}]', '{}\n{}'])
def test_inspection_requires_one_object_array(monkeypatch, output):
    m = load_helper(); monkeypatch.setattr(m, "clean_docker_env", lambda host: {})
    monkeypatch.setattr(m, "run", lambda *a, **k: SimpleNamespace(stdout=output))
    with pytest.raises(m.Refusal): m.docker_json(m.DOCKER_HOST, "network", "inspect", "id")


def topology_boundary(m, monkeypatch, *, net_change=None, links_change=None, container_change=None, absent=False):
    bound = m.binding("chosen-project")
    net = {"Id": "network-id", "Name": bound["network"], "Driver": "bridge", "Scope": "local", "Internal": False, "EnableIPv6": False,
           "Labels": {"com.docker.compose.project": bound["project"], "com.docker.compose.network": "forge-publisher-net"},
           "Options": {"com.docker.network.bridge.name": bound["bridge"]}, "Containers": {"publisher-id": {"IPv6Address": ""}, "coordinator-id": {"IPv6Address": ""}}}
    links = [{"ifname": bound["bridge"], "linkinfo": {"info_kind": "bridge"}}]
    if net_change: net_change(net)
    if links_change: links_change(links)
    def fake_run(argv, **kw):
        if argv[0] == "ip": return SimpleNamespace(stdout=json.dumps(links))
        assert argv[:3] == ["docker", "--host", m.DOCKER_HOST]
        args = argv[3:]
        if args[:2] == ["network", "ls"]:
            assert "--filter" not in args
            return SimpleNamespace(stdout="" if absent else json.dumps({"ID": "network-id", "Name": net["Name"]}) + "\n")
        if args[:2] == ["network", "inspect"]: return SimpleNamespace(stdout=json.dumps([net]))
        if args[0] == "ps": return SimpleNamespace(stdout="" if absent else json.dumps({"ID":"publisher-id"}) + "\n")
        if args[0] == "inspect":
            role = "forge-publisher" if args[1] == "publisher-id" else "coordinator"
            item = {"Config": {"Labels": {"com.docker.compose.project": bound["project"], "com.docker.compose.service": role}, "Healthcheck": {"Test": ["CMD", "curl", "http://localhost:8711/healthz"]}}, "State": {"Running": True}, "NetworkSettings": {"Networks": {bound["network"]: {}}, "Ports": {"8711/tcp": None}}, "HostConfig": {"PortBindings": {}, "PublishAllPorts": False}}
            if container_change: container_change(item)
            return SimpleNamespace(stdout=json.dumps([item]))
        raise AssertionError(argv)
    monkeypatch.setattr(m, "run", fake_run); monkeypatch.setattr(m, "clean_docker_env", lambda host: {})
    return bound


def test_real_list_caller_accepts_exact_owned_topology(monkeypatch):
    m=load_helper(); bound=topology_boundary(m, monkeypatch)
    assert set(m.inspect_topology(m.DOCKER_HOST,bound,require_members=True)["members"]) == {"coordinator","forge-publisher"}


@pytest.mark.parametrize("change", [lambda n:n.update(Labels={}), lambda n:n.update(Driver="macvlan"), lambda n:n.update(EnableIPv6=True), lambda n:n.pop("EnableIPv6"), lambda n:n.update(Internal=True), lambda n:n.update(Containers=[]), lambda n:n["Options"].update({"com.docker.network.bridge.name":"foreign"}), lambda n:n["Containers"]["publisher-id"].update(IPv6Address="fd00::2/64")])
def test_global_network_collisions_and_contracts_refuse(monkeypatch, change):
    m=load_helper(); bound=topology_boundary(m, monkeypatch, net_change=change)
    with pytest.raises(m.Refusal): m.inspect_topology(m.DOCKER_HOST,bound,require_members=False)


@pytest.mark.parametrize("kind", ["foreign-interface", "non-bridge"])
def test_interface_collision_refuses(monkeypatch, kind):
    m=load_helper(); bound=topology_boundary(m, monkeypatch, absent=kind=="foreign-interface", links_change=(lambda links: links[0]["linkinfo"].update(info_kind="dummy")) if kind=="non-bridge" else None)
    with pytest.raises(m.Refusal): m.inspect_topology(m.DOCKER_HOST,bound,require_members=False)


@pytest.mark.parametrize("change", [lambda c:c["Config"]["Labels"].update({"com.docker.compose.project":"foreign"}), lambda c:c["NetworkSettings"]["Ports"].update({"8711/tcp":[{"HostPort":"8711"}]}), lambda c:c["NetworkSettings"]["Networks"].update(other={}), lambda c:c["NetworkSettings"].update(GlobalIPv6Address="fd00::1"), lambda c:c["State"].update(Running=False)])
def test_membership_ports_ipv6_and_running_state_refuse(monkeypatch, change):
    m=load_helper();bound=topology_boundary(m,monkeypatch,container_change=change)
    with pytest.raises(m.Refusal):m.inspect_topology(m.DOCKER_HOST,bound,require_members=True)


@pytest.mark.parametrize("case", [
    "good-old-info", "good-iptables-info", "good-firewall-object", "good-firewall-object-info",
    "legacy", "native", "native-object", "malformed-object", "malformed-object-info",
    "conflicting-fields", "missing-chains", "native-table", "rootless", "unknown-info",
])
def test_positive_backend_qualification(monkeypatch, case):
    m=load_helper(); info={"ID":"daemon", "OSType":"linux", "SecurityOptions":["name=seccomp,profile=builtin"]}
    if case=="good-iptables-info":info["FirewallBackend"]="iptables"
    if case=="good-firewall-object":info["FirewallBackend"]={"Driver":"iptables"}
    if case=="good-firewall-object-info":info["FirewallBackend"]={"Driver":"iptables", "Info":[["EnableUserlandProxy","true"],["UserlandProxyPath","/usr/bin/docker-proxy"]]}
    if case=="native":info["FirewallBackend"]="nftables"
    if case=="unknown-info":info["FirewallBackend"]=""
    if case=="native-object":info["FirewallBackend"]={"Driver":"nftables"}
    if case=="malformed-object":info["FirewallBackend"]={}
    if case=="malformed-object-info":info["FirewallBackend"]={"Driver":"iptables", "Info":"unreadable"}
    if case=="conflicting-fields":info.update(FirewallBackend={"Driver":"iptables"}, FirewallDriver="nftables")
    if case=="rootless":info["SecurityOptions"].append("name=rootless")
    monkeypatch.setattr(m,"docker_json",lambda *a:info)
    def run(argv,**kw):
        if argv==["iptables","--version"]:return SimpleNamespace(stdout="iptables v1.8.10 (legacy)" if case=="legacy" else "iptables v1.8.10 (nf_tables)")
        if argv==["nft","--version"]:return SimpleNamespace(stdout="nftables v1.0.9 (Old Doc Yak)")
        if argv[0]=="iptables":return SimpleNamespace(stdout="" if case=="missing-chains" else "-N DOCKER\n-N DOCKER-USER\n-A FORWARD -j DOCKER-USER\n")
        if argv==["nft","-j","list","tables"]:return SimpleNamespace(stdout=json.dumps({"nftables":[{"table":{"family":"ip","name":"docker-bridges"}}] if case=="native-table" else []}))
        raise AssertionError(argv)
    monkeypatch.setattr(m,"run",run)
    if case.startswith("good"): assert m.daemon_identity(m.DOCKER_HOST)=="daemon"
    else:
        with pytest.raises(m.Refusal):m.daemon_identity(m.DOCKER_HOST)


@pytest.mark.parametrize("owned, changed", [(False,False),(True,True)])
def test_foreign_or_changed_table_never_mutates(monkeypatch, owned, changed):
    m=load_helper();bound=m.binding("chosen-project");data=nft_document(m)
    if changed:data["nftables"][2]["chain"]["prio"]=0
    monkeypatch.setattr(m,"nft_json",lambda table:data)
    monkeypatch.setattr(m,"run",lambda *a,**k:pytest.fail("mutated foreign table"))
    with pytest.raises(m.Refusal):m.apply_policy(bound,{"owned":True} if owned else None)


def test_unreadable_table_inventory_is_not_absence(monkeypatch):
    m=load_helper()
    def run(argv,**kw):
        if argv==["nft","-j","list","tables"]:raise m.Refusal("permission denied")
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(m,"run",run)
    with pytest.raises(m.Refusal):m.nft_json("forge_pub_000000000000")


@pytest.mark.parametrize("artifact", ["helper", "unit", "dropin"])
def test_shared_collisions_checked_before_policy_apply(monkeypatch, tmp_path, artifact):
    m=load_helper();bound=m.binding("chosen-project"); paths={"helper":tmp_path/"helper", "unit":tmp_path/"unit", "dropin":tmp_path/f"forge-publisher-host-policy-{bound['suffix']}.conf"}
    monkeypatch.setattr(m,"HELPER",paths["helper"]);monkeypatch.setattr(m,"UNIT",paths["unit"]);monkeypatch.setattr(m,"DROPIN_DIR",tmp_path)
    monkeypatch.setattr(m,"trusted_path",lambda path,**kw:path.exists())
    paths[artifact].write_text("foreign")
    with pytest.raises(m.Refusal):m.persistence_preflight(bound,True,None)
    assert paths[artifact].read_text()=="foreign"


def test_persistent_install_cannot_silently_become_runtime_only():
    m=load_helper()
    with pytest.raises(m.Refusal,match="transition"):m.persistence_preflight(m.binding("chosen-project"),False,{"persistent":True})


@pytest.mark.parametrize("kind", ["symlink", "unowned", "public"])
def test_config_trust_refuses_foreign_paths(tmp_path, kind):
    m=load_helper();path=tmp_path/"config";path.write_text("{}")
    if kind=="symlink":link=tmp_path/"link";link.symlink_to(path);path=link
    if kind=="public":path.chmod(0o666)
    with pytest.raises(m.Refusal):m.load_config(path)


def declared_model(m,project="chosen-project", settings=None):
    b=m.binding(project)
    if settings is None:
        handle=tempfile.NamedTemporaryFile(delete=False);handle.write(b'{"host":"0.0.0.0","port":8711}');handle.close();settings=Path(handle.name)
    return {"name":project,"networks":{"forge-publisher-net":{"name":b["network"],"driver":"bridge","enable_ipv6":False,"driver_opts":{"com.docker.network.bridge.name":b["bridge"]}}},"services":{"coordinator":{"networks":{"factory":{},"forge-publisher-net":{}},"environment":{"FORGE_PUBLISHER_URL":"http://forge-publisher:8711"}},"forge-publisher":{"volumes":[{"type":"bind","source":str(settings),"target":"/etc/forge-publisher/settings.json","read_only":True}],"networks":{"forge-publisher-net":{}},"healthcheck":{"test":["CMD","curl","http://localhost:8711/healthz"]}}}}


@pytest.mark.parametrize("change", [lambda d:d.update(name="wrong-project"),lambda d:d["services"]["forge-publisher"].update(ports=["8711:8711"]),lambda d:d["networks"]["forge-publisher-net"]["driver_opts"].clear(),lambda d:d["networks"]["forge-publisher-net"].update(enable_ipv6=True)])
def test_declared_graph_refuses_before_first_network_exists(change):
    m=load_helper();d=declared_model(m);m.validate_declared(d,m.binding("chosen-project"));change(d)
    with pytest.raises(m.Refusal):m.validate_declared(d,m.binding("chosen-project"))


def installed_paths(m, monkeypatch, tmp_path):
    monkeypatch.setattr(m, 'CONFIG_DIR', tmp_path/'config');m.CONFIG_DIR.mkdir()
    monkeypatch.setattr(m, 'HELPER', tmp_path/'helper');m.HELPER.write_bytes(HELPER.read_bytes())
    monkeypatch.setattr(m, 'UNIT', tmp_path/'unit');m.UNIT.write_bytes(m.unit_bytes())
    monkeypatch.setattr(m, 'DROPIN_DIR', tmp_path/'dropins');m.DROPIN_DIR.mkdir()
    monkeypatch.setattr(m, 'trusted_path', lambda path, **kw: path.exists())
    monkeypatch.setattr(m, 'require_root', lambda: None)
    monkeypatch.setattr(m, 'lock', lambda bound: open(tmp_path/'lock','a'))
    monkeypatch.setattr(m, 'daemon_identity', lambda host:'daemon')
    monkeypatch.setattr(m, 'clean_docker_env', lambda host:{})
    monkeypatch.setattr(m, 'systemd_paths', lambda:[m.UNIT.parent])
    monkeypatch.setattr(m, 'loader_effective', lambda *a,**kw:None)
    monkeypatch.setattr(m, 'verify_docker_dependency', lambda *a:None)


@pytest.mark.parametrize('collision', ['config', 'helper', 'unit', 'dropin', 'table'])
def test_install_preflights_all_collisions_before_writes(monkeypatch,tmp_path,collision):
    m=load_helper();bound=m.binding('chosen-project');installed_paths(m,monkeypatch,tmp_path)
    monkeypatch.setattr(m,'validate_env',lambda *args:None)
    monkeypatch.setattr(m,'inspect_topology',lambda *a,**k:{'network_id':'','members':{}})
    monkeypatch.setattr(m,'atomic_write',lambda *a,**k:pytest.fail('mutation before ownership refusal'))
    monkeypatch.setattr(m,'apply_policy',lambda *a,**k:pytest.fail('policy changed before ownership refusal'))
    monkeypatch.setattr(m,'nft_json',lambda table:nft_document(m) if collision=='table' else None)
    if collision=='config':
        (m.CONFIG_DIR/f"{bound['suffix']}.json").write_text('{}')
        monkeypatch.setattr(m,'load_config',lambda path: (_ for _ in ()).throw(m.Refusal('foreign config')))
    elif collision in ('helper','unit'):getattr(m,collision.upper()).write_text('foreign')
    elif collision=='dropin':(m.DROPIN_DIR/f"forge-publisher-host-policy-{bound['suffix']}.conf").write_bytes(m.dropin_bytes(bound['suffix']))
    with pytest.raises(m.Refusal):m.cmd_install(SimpleNamespace(project=bound['project'],env_file=tmp_path/'env',docker_host=m.DOCKER_HOST,runtime_only=False))


@pytest.mark.parametrize('changed', [False,True])
def test_removing_one_project_preserves_other_and_shared_artifacts(monkeypatch,tmp_path,changed):
    m=load_helper();installed_paths(m,monkeypatch,tmp_path);bound=m.binding('chosen-project');other=m.binding('second-project')
    configs={}
    for b in (bound,other):
        config=m.canonical_config(b,'daemon',m.DOCKER_HOST,True);path=m.CONFIG_DIR/f"{b['suffix']}.json";path.write_text(json.dumps(config));configs[path]=(b,config)
        (m.DROPIN_DIR/f"forge-publisher-host-policy-{b['suffix']}.conf").write_bytes(m.dropin_bytes(b['suffix']))
    monkeypatch.setattr(m,'load_config',lambda p:configs[p]);calls=[];data=nft_document(m)
    if changed:data['nftables'][2]['chain']['prio']=0
    monkeypatch.setattr(m,'nft_json',lambda table:data)
    def run(argv,**kw):
        calls.append(argv)
        if argv[:3]==['docker','--host',m.DOCKER_HOST]:return SimpleNamespace(stdout='')
        if argv[0]=='ip':return SimpleNamespace(stdout='[]')
        if argv[:2]==['systemctl','show']:return SimpleNamespace(stdout='Requires=other.service\nAfter=other.service\n')
        return SimpleNamespace(stdout='',returncode=0)
    monkeypatch.setattr(m,'run',run)
    args=SimpleNamespace(project=bound['project'],docker_host=m.DOCKER_HOST)
    if changed:
        with pytest.raises(m.Refusal):m.cmd_remove(args)
        assert not any(a[0] in ('systemctl','nft') for a in calls)
        assert (m.CONFIG_DIR/f"{bound['suffix']}.json").exists()
    else:
        m.cmd_remove(args)
        assert not (m.CONFIG_DIR/f"{bound['suffix']}.json").exists()
        assert calls.index(['systemctl','daemon-reload']) < calls.index(['nft','delete','table','inet',bound['table']])
    assert (m.CONFIG_DIR/f"{other['suffix']}.json").exists()
    assert (m.DROPIN_DIR/f"forge-publisher-host-policy-{other['suffix']}.conf").read_bytes()==m.dropin_bytes(other['suffix'])
    assert m.HELPER.read_bytes()==HELPER.read_bytes() and m.UNIT.read_bytes()==m.unit_bytes()
    assert not any('stop' in a or 'restart' in a for a in calls)


@pytest.mark.parametrize("network_present", [False,True])
def test_stranded_publisher_refuses_before_install_mutation(monkeypatch,tmp_path,network_present):
    m=load_helper();bound=topology_boundary(m,monkeypatch,absent=not network_present,links_change=(lambda links:links.clear()) if not network_present else None)
    base_run=m.run
    def run(argv,**kw):
        if argv[3:4]==["ps"]: return SimpleNamespace(stdout=json.dumps({"ID":"stranded-publisher"})+"\n")
        if argv[3:5]==["inspect","stranded-publisher"]:return SimpleNamespace(stdout=json.dumps([{"State":{"Running":True},"Config":{"Labels":{"com.docker.compose.project":bound["project"],"com.docker.compose.service":"forge-publisher"}},"NetworkSettings":{"Networks":{"foreign":{}}}}]))
        return base_run(argv,**kw)
    monkeypatch.setattr(m,"run",run);monkeypatch.setattr(m,"require_root",lambda:None)
    monkeypatch.setattr(m,"validate_env",lambda *a:None);monkeypatch.setattr(m,"daemon_identity",lambda *a:"daemon")
    monkeypatch.setattr(m,"lock",lambda b:open(tmp_path/"lock","a"));monkeypatch.setattr(m,"atomic_write",lambda *a,**k:pytest.fail("wrote before stranded-publisher refusal"))
    monkeypatch.setattr(m,"apply_policy",lambda *a:pytest.fail("mutated before stranded-publisher refusal"))
    with pytest.raises(m.Refusal):m.cmd_install(SimpleNamespace(project=bound["project"],env_file=tmp_path/"env",docker_host=m.DOCKER_HOST,runtime_only=True))


@pytest.mark.parametrize("state", ["owned", "changed"])
def test_static_loader_existing_state_is_verified_without_docker_or_mutation(monkeypatch,tmp_path,state):
    m=load_helper();b=m.binding("chosen-project");config=m.canonical_config(b,"daemon",m.DOCKER_HOST,True);data=nft_document(m)
    if state=="changed":data["nftables"][2]["chain"]["prio"]=0
    monkeypatch.setattr(m,"require_root",lambda:None);monkeypatch.setattr(m,"load_config",lambda p:(b,config));monkeypatch.setattr(m,"lock",lambda b:open(tmp_path/"lock","a"))
    monkeypatch.setattr(m,"persistence_preflight",lambda *a:None);monkeypatch.setattr(m,"nft_json",lambda t:data)
    monkeypatch.setattr(m,"run",lambda *a,**k:pytest.fail("static load invoked a command instead of using exact owned state"))
    args=SimpleNamespace(config=tmp_path/"config")
    if state=="changed":
        with pytest.raises(m.Refusal):m.cmd_load_static(args)
    else:m.cmd_load_static(args)


def test_install_requires_stopped_publisher_even_with_owned_previous_policy(monkeypatch,tmp_path):
    m=load_helper();installed_paths(m,monkeypatch,tmp_path);b=m.binding("chosen-project");config=m.canonical_config(b,"daemon",m.DOCKER_HOST,False)
    path=m.CONFIG_DIR/f"{b['suffix']}.json";path.write_text(json.dumps(config))
    monkeypatch.setattr(m,"validate_env",lambda *a:None);monkeypatch.setattr(m,"load_config",lambda p:(b,config));monkeypatch.setattr(m,"nft_json",lambda table:nft_document(m))
    monkeypatch.setattr(m,"inspect_topology",lambda *a,**kw:{"members":{"forge-publisher":"publisher-id"}})
    monkeypatch.setattr(m,"docker_json",lambda *a:[{"State":{"Running":True}}])
    monkeypatch.setattr(m,"atomic_write",lambda *a,**kw:pytest.fail("wrote while publisher running"))
    with pytest.raises(m.Refusal,match="stopped"):m.cmd_install(SimpleNamespace(project=b["project"],env_file=tmp_path/"env",docker_host=m.DOCKER_HOST,runtime_only=True))

@pytest.mark.parametrize('collision',['instance','template-dropin','instance-dropin','prefix-dropin','type-dropin','alias','alternate-template'])
def test_effective_loader_files_refuse_all_override_locations(tmp_path,collision):
    m=load_helper();m.UNIT=tmp_path/'owned'/'forge-publisher-host-policy@.service';m.UNIT.parent.mkdir();m.UNIT.write_bytes(m.unit_bytes());b=m.binding('chosen-project')
    alternate=tmp_path/'alternate';alternate.mkdir();instance=f"forge-publisher-host-policy@{b['suffix']}.service"
    names={'instance':instance,'template-dropin':m.UNIT.name+'.d/override.conf','instance-dropin':instance+'.d/override.conf','prefix-dropin':'forge-publisher-.service.d/override.conf','type-dropin':'service.d/override.conf','alternate-template':m.UNIT.name}
    if collision=='alias':(alternate/'other@.service').symlink_to(m.UNIT)
    else:
        path=alternate/names[collision];path.parent.mkdir(parents=True,exist_ok=True);path.write_text('[Service]\nExecStart=/bin/true\n')
    with pytest.raises(m.Refusal):m.loader_files_preflight(b,[m.UNIT.parent,alternate])


@pytest.mark.parametrize('changed',['none','argv','missing-exec','alias','dropin','fragment','requires','after'])
def test_effective_loader_and_docker_dependency_are_read_back(monkeypatch,changed):
    m=load_helper();b=m.binding('chosen-project');name=f"forge-publisher-host-policy@{b['suffix']}.service"
    properties={'Id':name,'Names':name,'FragmentPath':str(m.UNIT),'DropInPaths':'','LoadState':'loaded','Type':'oneshot','RemainAfterExit':'yes','ExecStart':f"{{ path={m.HELPER} ; argv[]={m.HELPER} load-static --config {m.CONFIG_DIR}/{b['suffix']}.json ; ignore_errors=no ; }}"}
    dependencies={'Requires':name,'After':name}
    if changed=='argv':properties['ExecStart']='{ path=/bin/true ; argv[]=/bin/true ; }'
    if changed=='missing-exec':properties.pop('ExecStart')
    if changed=='alias':properties['Names']+=' alias.service'
    if changed=='dropin':properties['DropInPaths']='/run/foreign.conf'
    if changed=='fragment':properties['FragmentPath']='/run/foreign.service'
    if changed in ('requires','after'):dependencies[changed.title()]=''
    calls=[]
    def run(argv,**kw):
        calls.append(argv);assert argv[:2]==['systemctl','show']
        data=dependencies if argv[2]=='docker.service' else properties
        requested=next(value.split('=',1)[1].split(',') for value in argv if value.startswith('--property='))
        return SimpleNamespace(
            stdout='\n'.join(key+'='+data[key] for key in requested if key in data), returncode=0
        )
    monkeypatch.setattr(m,'run',run)
    if changed=='none':m.loader_effective(b,installed=True);m.verify_docker_dependency(b)
    else:
        with pytest.raises(m.Refusal):m.loader_effective(b,installed=True);m.verify_docker_dependency(b)
    assert calls and all(c[1]=='show' for c in calls)


def test_absent_loader_accepts_recorded_sparse_systemctl_shape(monkeypatch):
    m=load_helper();b=m.binding('chosen-project');name=f"forge-publisher-host-policy@{b['suffix']}.service"
    recorded='''Type=
RemainAfterExit=no
Id={name}
Names={name}
LoadState=not-found
FragmentPath=
DropInPaths=
'''.format(name=name)
    observed=dict(line.split('=',1) for line in recorded.splitlines())
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        requested=next(value.split('=',1)[1].split(',') for value in argv if value.startswith('--property='))
        return SimpleNamespace(
            stdout='\n'.join(key+'='+observed[key] for key in requested if key in observed), returncode=0
        )
    monkeypatch.setattr(m,'run',run)
    m.loader_effective(b,installed=False)
    assert calls == [[
        'systemctl','show',name,
        '--property=Id,Names,FragmentPath,DropInPaths,LoadState','--no-pager',
    ]]


@pytest.mark.parametrize('changed',['alias','dropin','fragment','unknown-state'])
def test_absent_loader_refuses_conflicting_or_unknown_identity(monkeypatch,changed):
    m=load_helper();b=m.binding('chosen-project');name=f"forge-publisher-host-policy@{b['suffix']}.service"
    properties={
        'Id':name,
        'Names':name,
        'FragmentPath':'',
        'DropInPaths':'',
        'LoadState':'not-found',
    }
    if changed=='alias':properties['Names']+=' alias.service'
    if changed=='dropin':properties['DropInPaths']='/run/foreign.conf'
    if changed=='fragment':properties['FragmentPath']='/run/foreign.service'
    if changed=='unknown-state':properties['LoadState']='error'
    def run(argv,**kw):
        requested=next(value.split('=',1)[1].split(',') for value in argv if value.startswith('--property='))
        return SimpleNamespace(
            stdout='\n'.join(key+'='+properties[key] for key in requested if key in properties), returncode=0
        )
    monkeypatch.setattr(m,'run',run)
    with pytest.raises(m.Refusal):m.loader_effective(b,installed=False)


def test_runtime_only_installs_narrow_verifier_without_boot_dependency(monkeypatch,tmp_path):
    m=load_helper();installed_paths(m,monkeypatch,tmp_path);m.HELPER.unlink();m.UNIT.unlink();b=m.binding('chosen-project')
    monkeypatch.setattr(m,'validate_env',lambda *a:None);monkeypatch.setattr(m,'inspect_topology',lambda *a,**k:{'members':{},'network_id':''});monkeypatch.setattr(m,'nft_json',lambda table:None)
    monkeypatch.setattr(m,'apply_policy',lambda *a:None);monkeypatch.setattr(m,'run',lambda *a,**k:pytest.fail('runtime-only install called systemd'))
    m.cmd_install(SimpleNamespace(project=b['project'],env_file=tmp_path/'env',docker_host=m.DOCKER_HOST,runtime_only=True))
    assert m.HELPER.read_bytes()==HELPER.read_bytes() and not m.UNIT.exists() and not list(m.DROPIN_DIR.iterdir())


@pytest.mark.parametrize('changed',['none','source','bytes','writable'])
def test_running_publisher_settings_identity_is_checked(monkeypatch,tmp_path,changed):
    m=load_helper();settings=tmp_path/'settings.json';settings.write_text('{"host":"0.0.0.0","port":8711}')
    container={'Mounts':[{'Type':'bind','Source':str(settings) if changed!='source' else str(tmp_path/'other.json'),'Destination':'/etc/forge-publisher/settings.json','RW':changed=='writable'}],'State':{'Running':True}}
    monkeypatch.setattr(m,'docker_json',lambda *a:[container]);monkeypatch.setattr(m,'clean_docker_env',lambda host:{})
    identity=m.settings_identity(settings)
    if changed=='bytes':identity['sha256']='0'*64
    monkeypatch.setattr(m,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(identity)))
    if changed=='none':m.verify_publisher_settings(m.DOCKER_HOST,'owned',settings)
    else:
        with pytest.raises(m.Refusal):m.verify_publisher_settings(m.DOCKER_HOST,'owned',settings)
