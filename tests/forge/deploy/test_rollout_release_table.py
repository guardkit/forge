"""The rollout tools accept images only as one reviewed release's entry (TC1).

Release -3 upgrade runbook, 2 October 2026: an estate moving from release
2026.09.28-2 to the next must be able to name both releases at once, so the
single pinned coordinator and publisher images became a table keyed by release.
These tests use only the standard library and fakes; no Docker, no broker.
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[3] / 'deploy/estate'


def module(name):
    loader = importlib.machinery.SourceFileLoader('release_table_' + name.replace('-', '_'), str(HERE / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


b = module('rollout-back')
q = b.q
r = q.r

V2 = '2026.09.28-2'
V3 = '2026.10.02-3'
V3_ENTRY = {'runtime': 'sha256:' + '3' * 64, 'publisher': 'sha256:' + '4' * 64, 'memory': 'sha256:' + '5' * 64,
            'relay': 'sha256:' + '6' * 64, 'jarvis': 'sha256:' + '7' * 64, 'schema': 16}


@pytest.fixture
def table(monkeypatch):
    """A release -3 entry exists only for the test; the real one is added once its images exist."""
    monkeypatch.setitem(r.RELEASES, V3, dict(V3_ENTRY))
    return r.RELEASES


def inventory(tmp_path, image, release=None):
    """A switch-shaped prepared inventory, plus 'release' when given."""
    old = tmp_path / 'old'; old.mkdir(); prepared = tmp_path / 'prepared'; prepared.mkdir()
    root = tmp_path / 'snapshots'; root.mkdir(); snap = root / '20261002T080000Z'; snap.mkdir()
    db = old / 'forge.db'; db.write_bytes(b'')
    (old / 'forge.yaml').write_text('planning:\n  enabled: false\n'); (prepared / 'forge.yaml').write_text('planning:\n  enabled: false\n')
    for folder in (old / 'evidence', old / 'threads'): folder.mkdir()
    (old / 'relay-progress.json').write_text('{}')
    for p in (old / 'estate.env', prepared / 'estate.env'):
        p.write_text('FORGE_IMAGE=' + image + '\n')
    compose = tmp_path / 'compose.json'; compose.write_text('{}')
    c = {'project': 'codex-release-table', 'runtime_image': image, 'docker_context': 'default', 'source_db': str(db),
         'snapshot_root': str(root), 'forbidden_roots': [str(old)], 'units': {x: 'owned-' + x + ('.timer' if x == 'watchdog_timer' else '.service') for x in r.UNIT_ROLES},
         'old_containers': {x: 'owned-' + x for x in ('coordinator', 'memory', 'relay')},
         'volumes': {x: 'owned-' + x for x in r.VOLUME_ROLES},
         'sources': {'settings': str(prepared / 'forge.yaml'), 'evidence': str(old / 'evidence'), 'threads': str(old / 'threads'), 'relay_progress': str(old / 'relay-progress.json')},
         'env_file': str(prepared / 'estate.env'), 'compose_files': [str(compose)],
         'quiesce': {'receipt': str(tmp_path / 'quiesce.json'), 'original_env_file': str(old / 'estate.env'), 'prepared_env_file': str(prepared / 'estate.env'),
                     'original_settings': str(old / 'forge.yaml'), 'prepared_settings': str(prepared / 'forge.yaml'),
                     'settings_receipt': str(prepared / 'receipt.json'), 'actor': 'fixture', 'release_tag': 'forge:' + V2}}
    if release is not None:
        c['release'] = release
    path = tmp_path / 'inventory.json'; r.atomic_json(path, c)
    args = SimpleNamespace(config=str(path), env_file=c['env_file'], project=c['project'], snapshot=str(snap), secret_env_file=[], plan=False,
                           candidate_image=None, candidate_env_file=None)
    return c, path, args


def test_switch_release_entry_holds_the_images_the_constants_held():
    entry = r.RELEASES[r.SWITCH_RELEASE]
    assert r.SWITCH_RELEASE == V2 and entry['schema'] == 16
    assert entry['runtime'] == r.RUNTIME == 'sha256:1eaa3360b280cadebb308363aa2bfae06ddb852b25b1b8c7cd603cfa62ec0316'
    assert entry['publisher'] == r.PUBLISHER_RUNTIME == 'sha256:dd5281444ec7fbe6f13473331c693383d458819b72789a85815305471a0a604b'
    assert all(isinstance(v, str) and v.startswith('sha256:') and len(v) == 71 for k, v in entry.items() if k != 'schema')


@pytest.mark.parametrize('release', [None, V2])
def test_a_release_minus_2_inventory_with_or_without_its_name_is_accepted(tmp_path, release):
    c, path, args = inventory(tmp_path, r.RUNTIME, release)
    assert r.config(path)['runtime_image'] == r.RUNTIME
    assert q.Estate(args).c['runtime_image'] == r.RUNTIME


def test_an_inventory_naming_release_minus_3_with_its_images_is_accepted(tmp_path, table):
    c, path, args = inventory(tmp_path, V3_ENTRY['runtime'], V3)
    assert r.config(path)['runtime_image'] == V3_ENTRY['runtime']
    assert q.Estate(args).c['release'] == V3


@pytest.mark.parametrize('release', ['2026.10.02-9', '', 3])
def test_an_inventory_naming_a_release_not_in_the_table_refuses(tmp_path, table, release):
    c, path, args = inventory(tmp_path, V3_ENTRY['runtime'], release)
    with pytest.raises(r.Refusal, match='not in the reviewed release table'):
        r.config(path)
    with pytest.raises(r.Refusal, match='not in the reviewed release table'):
        q.Estate(args)


@pytest.mark.parametrize('release,image', [(V2, V3_ENTRY['runtime']), (V3, r.RUNTIME), (None, V3_ENTRY['runtime'])])
def test_an_inventory_whose_image_differs_from_its_release_entry_refuses(tmp_path, table, release, image):
    c, path, args = inventory(tmp_path, image, release)
    with pytest.raises(r.Refusal, match='accepted immutable image of release'):
        r.config(path)
    with pytest.raises(r.Refusal, match='runtime identity is invalid'):
        q.Estate(args)


def model_for(c, coordinator, publisher):
    service = lambda image, mounts: {'image': image, 'volumes': mounts, 'environment': {'FORGE_DB_PATH': '/var/lib/forge/forge.db'}}
    ledger = [{'type': 'volume', 'source': 'ledger', 'target': '/var/lib/forge'}]
    coordinator_mounts = ledger + [{'type': 'volume', 'source': 'settings', 'target': '/etc/forge'}, {'type': 'volume', 'source': 'evidence', 'target': '/var/lib/forge-evidence'}]
    return {'services': {'coordinator': service(coordinator, coordinator_mounts), 'answer-service': service(coordinator, ledger), 'forge-publisher': service(publisher, ledger),
                         'front-door': {'volumes': [{'type': 'volume', 'source': 'threads', 'target': '/app/.langgraph_api'}]},
                         'memory-relay': {'volumes': [{'type': 'volume', 'source': 'relay_progress', 'target': '/var/lib/fleet-memory'}]}},
            'volumes': {role: {'name': c['volumes'][role]} for role in r.VOLUME_ROLES}}


@pytest.mark.parametrize('release,good', [(V3, True), (V2, False)])
def test_rendered_graph_is_checked_against_the_inventory_release_entry(tmp_path, table, monkeypatch, release, good):
    c, _, _ = inventory(tmp_path, r.RELEASES[release]['runtime'], release)
    monkeypatch.setattr(r, 'compose', lambda c, *a: SimpleNamespace(stdout=json.dumps(model_for(c, V3_ENTRY['runtime'], V3_ENTRY['publisher']))))
    if good:
        assert r.rendered(c)['services']['coordinator']['image'] == V3_ENTRY['runtime']
    else:
        with pytest.raises(r.Refusal, match='does not select the accepted immutable image'):
            r.rendered(c)


def test_release_images_name_every_service_and_leave_the_provisioning_image_to_the_env(table):
    assert r.release_image(V3, 'sandbox-runner') == V3_ENTRY['runtime'] and r.release_image(V3, 'front-door') == V3_ENTRY['jarvis']
    assert r.release_image(V3, 'memory-relay') == V3_ENTRY['relay'] and r.release_image(V2, 'forge-publisher') == r.PUBLISHER_RUNTIME
    assert r.release_image(V3, 'bus-ready', {'NATS_PROVISION_IMAGE': 'sha256:' + '8' * 64}) == 'sha256:' + '8' * 64
    assert r.release_image(V3, 'nats') is None


# The same complete proof test_rollout_quiesce_back builds; copied, because that module
# must be the first in its process to touch the broker client boundary.
def valid_h6():
    J='3'*40;target='fixture-target'
    return {'format_version':1,'outcome':'handled-both','candidate_image_id':r.RUNTIME,'pristine_sha256':'a'*64,'working_pre_fixture_sha256':'a'*64,'configuration_sha256':'b'*64,'schema_version':16,'existing_rows_unchanged':True,'real_client_modules':[],'cleanup':'no owned worker remains','worker_group_empty':True,'worker_thread_stopped':True,'init':{'probe_pid':7,'pid1':'/sbin/docker-init'},'columns':{'builds':['build_id','status','mode','start_commit','target_branch'],'publication_records':['build_id','g_commit','j_commit','checked_json','turn','lines_json'],'deployment_targets':['target','counter','holder_build','holder_turn','running_commit']},'git':{'G':'1'*40,'tip':'2'*40,'J':J,'tree':'4'*40},'fixture_ids':{'build':'fixture-build','target':target},'publication':{'turn':4,'result':'published, deployment pending','g_commit':'1'*40,'j_commit':J,'checked':{'identity':J,'j_commit':J,'j_tree':'4'*40},'original_lines_preserved':True,'callbacks':{'publisher':0,'guardkit':0,'deploy':0,'stage_complete':1},'line_kinds':['done join','done merge-checks','done candidate-check','about to send','done send'],'before_sha256':'c'*64,'after_sha256':'d'*64,'send_result':{'found_by_looking':True,'published':True,'contains_j':True,'ran_on':J,'remote_now':J}},'deployment':{'N':41,'N_plus_1':42,'stale_fencing':{'renew':False,'record_running':False,'release':False},'stale_record_unchanged':True,'reconcile':{target:'occupied (adopted)'},'old_note':{'target':target,'counter':41,'highest_counter':41,'group':4,'phase':'running','build':'old'},'final_note':{'target':target,'highest_counter':42,'group':0,'counter':0,'phase':'','highest_build':'new'},'old_answers':[{'accepted':False,'word':'the-deploy-command-was-stopped-by-a-takeover'}],'successor':{'accepted':True,'exit_code':0,'word':'the-deploy-command-ran','output_tail':'DEPLOYED_IDENTITY='+J}}}


def test_h6_check_expects_the_schema_of_the_release_it_was_given():
    proof = valid_h6()
    assert b.validate_h6(proof, r.RUNTIME, 'a' * 64, 'b' * 64, 16) == proof
    with pytest.raises(r.Refusal, match='H6 behavioral proof'):
        b.validate_h6(proof, r.RUNTIME, 'a' * 64, 'b' * 64, 17)


def test_h6_probe_asserts_the_schema_it_is_handed_not_a_constant():
    import inspect
    source = inspect.getsource(b.h6_probe)
    assert "==schema" in source and "schema=int(sys.argv[5])" in source and "fetchone()[0]==16" not in source
