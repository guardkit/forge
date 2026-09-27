"""Rollout state controls with real Forge migration/writer-created fixtures.

This file is runnable alone in the accepted Forge image; no host app imports and
no broker, model, Slack, sandbox or deployment connection is made.
"""
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
import sys
from types import ModuleType

# Persistence imports forge.pipeline's initializer, which imports the event
# package initializer. Mock its client boundary BEFORE that transitive import;
# neither a real broker client module nor a usable fake connection is loaded.
_mock_client = ModuleType('nats_core.client')
_mock_client.__rollout_mock__ = True
class ForbiddenBrokerClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError('a broker client is outside these SQLite tests')
_mock_client.NATSClient = ForbiddenBrokerClient
_mock_client.NATSKVManifestRegistry = ForbiddenBrokerClient
assert 'nats_core.client' not in sys.modules, 'client boundary initialized before mock'
sys.modules['nats_core.client'] = _mock_client

from forge.lifecycle import migrations
from forge.adapters.sqlite.connect import connect_writer
from forge.lifecycle.persistence import SqliteLifecyclePersistence

BUNDLE = Path(__file__).resolve().parents[3] / 'deploy' / 'estate'
spec = importlib.util.spec_from_file_location('rollout_support', BUNDLE / 'rollout_support.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

@pytest.fixture(autouse=True)
def packaged_boot_transport(monkeypatch):
    # The suite already runs inside the pinned runtime; subprocess executes the
    # real pure boot code while only the Docker transport is replaced.
    def execute(c, code, args=(), mounts=(), **kwargs):
        assert code == r.BOOT_SQLITE_CODE, 'unexpected unmocked container operation'
        root=Path(mounts[0].split('src=')[1].split(',')[0])
        child=subprocess.run(['python','-c',code.replace('/copy/forge.db',str(root/'forge.db'))],capture_output=True,text=True)
        if child.returncode: raise r.Refusal('packaged boot derivation failed')
        return child.stdout
    monkeypatch.setattr(r,'container_python',execute)

def seed(path, version=16, build=False):
    db = connect_writer(path)
    original = migrations._MIGRATIONS
    try:
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= version)
        migrations.apply_at_boot(db)
    finally:
        migrations._MIGRATIONS = original
    if build:
        now = datetime.now(timezone.utc)
        SqliteLifecyclePersistence(connection=db, db_path=path).record_pending_build(SimpleNamespace(
            branch="fixture-branch", originating_adapter=None, originating_user=None, parent_request_id=None, max_turns=5, sdk_timeout_seconds=1800,
            feature_id='FEAT-STATE', repo='example.invalid/made-up', feature_yaml_path='.guardkit/features/f.yaml',
            triggered_by='forge-internal', correlation_id='state-fixture', requested_at=now, queued_at=now))
    db.close()
    return path

@pytest.fixture
def setup(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    root = tmp_path / 'snapshots'; root.mkdir()
    db = seed(source / 'forge.db', build=True)
    env = tmp_path / 'estate.env'; env.write_text('FORGE_IMAGE='+r.RUNTIME+'\n')
    compose = tmp_path / 'compose.yaml'; compose.write_text('services: {}\n')
    sources = {}
    for role in ('evidence','threads'):
        d = tmp_path / role; d.mkdir(); (d/'retained.txt').write_text(role+' content'); sources[role] = str(d)
    settings = tmp_path/'forge.yaml'; settings.write_text('projects: {}\n'); sources['settings']=str(settings)
    relay = tmp_path/'relay-progress.json'; relay.write_text('{"messages":42}'); sources['relay_progress']=str(relay)
    c = dict(project='codex-state-test',docker_context='default',env_file=str(env),compose_files=[str(compose)],runtime_image=r.RUNTIME,
        source_db=str(db),snapshot_root=str(root),forbidden_roots=[str(source)],units={x:x+'.service' for x in r.UNIT_ROLES},
        old_containers={x:'old-'+x for x in ('coordinator','memory','relay')}, sources=sources,
        volumes={x:'codex-state-test-'+x for x in r.VOLUME_ROLES})
    destination = root/'20260927T081500Z'
    return c,destination

def snapshot_fixture(c,destination, version=16):
    destination.mkdir()
    seed(destination/'forge.db', version=version)
    r.atomic_json(destination/'previous-runtime.json',{'format_version':1,'env_names':['SECRET_NAME']})
    state=r.ledger_state(destination/'forge.db')
    meta={'format_version':1,'project':c['project'],'source_db':c['source_db'],'created_at':'2026-09-27T08:15:00+00:00',
        'sha256':r.sha256(destination/'forge.db'),'previous_runtime_sha256':r.sha256(destination/'previous-runtime.json'),**state}
    r.atomic_json(destination/'metadata.json',meta)
    return meta

def test_real_writer_state_contains_required_fields_without_build_payload(setup):
    c,_=setup
    state=r.ledger_state(c['source_db'])
    assert state['schema_version']==16
    row=state['work_state']['builds']['rows'][0]
    assert row['status']=='QUEUED' and row['completed_at'] is None
    assert 'feature_yaml_path' not in row and 'repo' not in row

@pytest.mark.parametrize('version',[1,3,11,14,15])
def test_real_older_schema_absence_is_not_zero(tmp_path,version):
    state=r.ledger_state(seed(tmp_path/'old.db',version))
    assert state['schema_version']==version
    assert state['work_state']['deployment_targets']=={'status':'NOT-YET','introduced_in':16}
    if version<15: assert state['work_state']['publication_records']['status']=='NOT-YET'

def test_hash_or_runtime_tamper_refuses(setup):
    c,d=setup; snapshot_fixture(c,d)
    assert r.verify_snapshot(d)['schema_version']==16
    (d/'previous-runtime.json').write_text('{}')
    with pytest.raises(r.Refusal,match='runtime record'): r.verify_snapshot(d)

def test_snapshot_tamper_refuses(setup):
    c,d=setup; snapshot_fixture(c,d)
    with (d/'forge.db').open('ab') as f:f.write(b'changed')
    with pytest.raises(r.Refusal,match='hash'):r.verify_snapshot(d)

def test_symlink_path_refuses(tmp_path):
    real=tmp_path/'real';real.mkdir(); (tmp_path/'alias').symlink_to(real)
    with pytest.raises(r.Refusal,match='symlink'):r.path(tmp_path/'alias'/'absent',exists=False)

def test_manifest_is_content_sensitive_and_rejects_symlinks(tmp_path):
    (tmp_path/'file').write_text('abc'); before=r.manifest(tmp_path)
    (tmp_path/'file').write_text('xyz'); assert r.manifest(tmp_path)!=before
    (tmp_path/'link').symlink_to(tmp_path/'file')
    with pytest.raises(r.Refusal):r.manifest(tmp_path)

def test_config_conflicts_and_unpinned_env_refuse(setup,tmp_path):
    c,_=setup;p=tmp_path/'inventory.json';r.atomic_json(p,c)
    assert r.config(p)['project']==c['project']
    with pytest.raises(r.Refusal,match='conflicts'):r.config(p,project='other')
    Path(c['env_file']).write_text('FORGE_IMAGE=forge:latest\n')
    with pytest.raises(r.Refusal,match='FORGE_IMAGE'):r.config(p)

def test_snapshot_plan_has_no_mutation(setup,monkeypatch):
    c,d=setup
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=''))
    before=r.manifest(d.parent)
    result=r.snapshot(c,d,True)
    assert result['plan'] and result['db_size']>0
    assert r.manifest(d.parent)==before

def test_wrong_snapshot_root_refuses(setup,monkeypatch):
    c,d=setup; c['snapshot_root']=str(Path(c['source_db']).parent)
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=''))
    with pytest.raises(r.Refusal,match='inside source'):r.snapshot(c,Path(c['snapshot_root'])/d.name,True)

def test_load_plan_runs_no_container_or_write(setup,monkeypatch):
    c,d=setup;snapshot_fixture(c,d,11)
    monkeypatch.setattr(r,'rendered',lambda c:{'volumes':{k:{'name':v} for k,v in c['volumes'].items()}})
    monkeypatch.setattr(r,'container_python',lambda *a,**k:pytest.fail('container during plan'))
    before=r.manifest(d.parent)
    assert r.load_volumes(c,d,True)['source_schema']==11
    assert r.manifest(d.parent)==before

def mock_load(monkeypatch):
    monkeypatch.setattr(r,'rendered',lambda c:{'volumes':{k:{'name':v} for k,v in c['volumes'].items()}})
    monkeypatch.setattr(r,'stopped',lambda c:None)
    monkeypatch.setattr(r,'volume_identity',lambda *a: {})
    monkeypatch.setattr(r,'verify_loaded',lambda *a: None)
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=''))
    monkeypatch.setattr(r,'volume_inventory',lambda c,model=None,identities=None:{k:None for k in r.VOLUME_ROLES})

def test_migration_failure_keeps_snapshot_and_launches_no_apps_then_clean_retry(setup,monkeypatch):
    c,d=setup;snapshot_fixture(c,d,11); before=r.manifest(d)
    mock_load(monkeypatch); calls=[]
    def fail(c,code,args=(),mounts=(),**kw):
        calls.append(code)
        copy=Path(mounts[0].split('src=')[1].split(',')[0])/'forge.db'
        db=sqlite3.connect(copy); db.execute('CREATE TABLE transient_copy_only(x)');db.commit();db.close()
        raise r.Refusal('injected migration failure; retry with the accepted migrator')
    monkeypatch.setattr(r,'container_python',fail)
    with pytest.raises(r.Refusal,match='migration of a disposable copy'):r.load_volumes(c,d)
    assert len(calls)==1 and 'apply_at_boot' in calls[0]
    assert r.manifest(d)==before and not list(d.parent.glob('.load-*'))
    copies={}
    def real_migration_and_fake_transport(c,code,args=(),mounts=(),**kw):
        if 'apply_at_boot' in code:
            copy=Path(mounts[0].split('src=')[1].split(',')[0])/'forge.db'
            db=connect_writer(copy);migrations.apply_at_boot(db);db.close()
        elif 'shutil.copytree' in code:
            source=Path(mounts[0].split('src=')[1].split(',')[0]);name=mounts[1].split('src=')[1].split(',')[0]
            copies[name]=r.manifest(source)
        return ''
    monkeypatch.setattr(r,'container_python',real_migration_and_fake_transport)
    monkeypatch.setattr(r,'volume_manifest',lambda c,n:copies[n])
    result=r.load_volumes(c,d)
    assert result['loaded'] and result['snapshot_sha256']!=result['migrated_sha256']
    assert r.sha256(d/'forge.db')==result['snapshot_sha256']
    assert r.read_json(d/'load-receipt.json')['container_verification'] is None
    monkeypatch.setattr(r,'volume_inventory',lambda c,model=None,identities=None:{k:copies[v] for k,v in c['volumes'].items()})
    assert r.load_volumes(c,d)['idempotent']

def test_occupied_volume_refuses_before_migration(setup,monkeypatch):
    c,d=setup;snapshot_fixture(c,d);mock_load(monkeypatch)
    monkeypatch.setattr(r,'volume_inventory',lambda c,model=None,identities=None:{k:[{'path':'forge.db'}] for k in r.VOLUME_ROLES})
    monkeypatch.setattr(r,'container_python',lambda *a,**k:pytest.fail('must not migrate'))
    with pytest.raises(r.Refusal,match='occupied'):r.load_volumes(c,d)

def test_actual_compose_mount_resolution_refuses_different_volume(setup,monkeypatch):
    c,_=setup
    model={'services':{s:{'volumes':[{'target':'/var/lib/forge','type':'volume','source':'shared'}]} for s in ('coordinator','answer-service','forge-publisher')},'volumes':{'shared':{'name':c['volumes']['ledger']}}}
    model['services']['forge-publisher']['volumes'][0]['type']='bind'
    monkeypatch.setattr(r,'compose',lambda *a:SimpleNamespace(stdout=json.dumps(model)))
    with pytest.raises(r.Refusal,match='forge-publisher'):r.rendered(c)

def test_readback_requires_real_running_container_and_identical_mark(setup,monkeypatch):
    c,d=setup;meta=snapshot_fixture(c,d)
    receipt=receipt_fixture(c,d,meta)
    monkeypatch.setattr(r,'compose',lambda *a:SimpleNamespace(stdout='actual-container'))
    monkeypatch.setattr(r,'inspect',lambda *a:{'Id':'actual-container','Image':r.RUNTIME,'State':{'Running':True},'Mounts':[{'Destination':'/var/lib/forge','Type':'volume','Name':c['volumes']['ledger']}]})
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout='{}'))
    with pytest.raises(r.Refusal,match='different ledger or snapshot mark'):r.verify_containers(c,d,meta,receipt,False)
    assert not (d/'load-receipt.json').exists()

def test_unknown_holder_is_not_empty(tmp_path,monkeypatch):
    db=tmp_path/'forge.db';db.write_text('fixture')
    monkeypatch.setattr(r,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='permission denied'))
    with pytest.raises(r.Refusal,match='unreadable'):r.no_holders(db)

def test_ledger_inspection_releases_its_file_descriptor(setup):
    c,_=setup
    r.ledger_state(c['source_db'])
    held=[]
    for fd in Path('/proc/self/fd').iterdir():
        try:
            if fd.resolve()==Path(c['source_db']):held.append(fd.name)
        except OSError:pass
    assert held==[]

def test_malformed_input_refuses_in_one_sentence_without_traceback(capsys):
    assert r.main_guard(lambda: (_ for _ in ()).throw(AttributeError('secret must not print')))==2
    err=capsys.readouterr().err
    assert err.count('\n')==1 and 'Traceback' not in err and 'secret' not in err

def test_copy_rejects_changed_input_on_retry(setup,monkeypatch):
    c,d=setup;meta=snapshot_fixture(c,d);mock_load(monkeypatch)
    source_manifests={role:r.manifest(c['sources'][role]) for role in ('evidence','threads')}
    for role,name in [('settings','forge.yaml'),('relay_progress','relay-progress.json')]:
        p=Path(c['sources'][role]);source_manifests[role]=[{'path':name,'size':p.stat().st_size,'sha256':r.sha256(p)}]
    inventory={k:[] for k in r.VOLUME_ROLES}
    r.atomic_json(d/'load-receipt.json',{'project':c['project'],'runtime_image':c['runtime_image'],'snapshot_sha256':meta['sha256'],'volumes':c['volumes'],'manifests':inventory,'source_manifests':source_manifests})
    monkeypatch.setattr(r,'volume_inventory',lambda c,model=None,identities=None:inventory)
    Path(c['sources']['settings']).write_text('changed input')
    with pytest.raises(r.Refusal,match='receipt'):r.load_volumes(c,d)

def test_source_tree_symlink_refuses_before_migration(setup,monkeypatch):
    c,d=setup;snapshot_fixture(c,d);mock_load(monkeypatch)
    (Path(c['sources']['evidence'])/'outside').symlink_to(c['source_db'])
    monkeypatch.setattr(r,'container_python',lambda *a,**k:pytest.fail('migration before source check'))
    with pytest.raises(r.Refusal,match='symlink'):r.load_volumes(c,d)

def test_stopped_units_require_zero_control_pid(setup,monkeypatch):
    c,_=setup
    monkeypatch.setattr(r,'run',lambda *a,**k:SimpleNamespace(stdout='LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=123\n'))
    with pytest.raises(r.Refusal,match='not proved inactive'):r.stopped(c)

def test_snapshot_failure_keeps_no_invalid_directory(setup,monkeypatch):
    c,d=setup
    monkeypatch.setattr(r,'safe_snapshot_destination',lambda c,p:Path(p))
    monkeypatch.setattr(r,'stopped',lambda c:({}, {'coordinator':{'Id':'stable'}}))
    monkeypatch.setattr(r,'no_holders',lambda p:None)
    monkeypatch.setattr(r.time,'sleep',lambda s:None)
    monkeypatch.setattr(r,'previous_runtime',lambda *a:{})
    def broken_backup(argv,**kwargs):
        assert argv[0]=='sqlite3' and argv[1]=='-readonly' and argv[3].startswith('.backup ')
        raise r.Refusal('backup failed; retry')
    monkeypatch.setattr(r,'run',broken_backup)
    with pytest.raises(r.Refusal,match='backup failed'):r.snapshot(c,d)
    assert not d.exists() and not list(d.parent.glob('.snapshot-*'))

def test_previous_runtime_omits_secret_environment_values(setup,monkeypatch):
    c,_=setup
    item={'Image':r.RUNTIME,'Name':'/old','Id':'cid','Mounts':[],
          'Config':{'Env':['PASSWORD=never-copy-this-secret','FORGE_DB_PATH=/old/db'],'Hostname':'old','User':'forge','WorkingDir':'/app','Entrypoint':['forge'],'Cmd':['serve']},
          'HostConfig':{'PortBindings':{},'RestartPolicy':{'Name':'unless-stopped'},'NetworkMode':'none'},'NetworkSettings':{'Networks':{'none':{}},'Ports':{}}}
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=json.dumps([{'RepoTags':['forge:previous']} ])))
    previous=r.previous_runtime(c,item)
    assert previous['env_names']==['FORGE_DB_PATH','PASSWORD']
    assert 'never-copy-this-secret' not in json.dumps(previous)


def receipt_fixture(c,d,meta):
    migrated=r.sha256(d/'forge.db')
    boot=d/'expected-boot.db';shutil.copyfile(d/'forge.db',boot)
    child=subprocess.run(['python','-c',r.BOOT_SQLITE_CODE.replace('/copy/forge.db',str(boot))],capture_output=True,text=True)
    assert child.returncode==0,child.stderr
    startup=r.consolidated_logical_digest(boot);boot.unlink()
    r.retain_migrated_artifact(d,d/'forge.db')
    mark={'format_version':1,'snapshot_sha256':meta['sha256'],'snapshot_created_at':meta['created_at'],
          'migrated_sha256':migrated,'source_schema_version':meta['schema_version'],'loaded_schema_version':16,
          'loaded_logical_sha256':r.consolidated_logical_digest(d/'forge.db'),'startup_logical_sha256':startup}
    staging=d/'loaded-ledger';staging.mkdir()
    shutil.copyfile(d/'forge.db',staging/'forge.db');r.atomic_json(staging/r.MARK,mark)
    sources={role:r.manifest(c['sources'][role]) for role in ('evidence','threads')}
    for role,name in [('settings','forge.yaml'),('relay_progress','relay-progress.json')]:
        p=Path(c['sources'][role]);sources[role]=[{'path':name,'size':p.stat().st_size,'sha256':r.sha256(p)}]
    return {'format_version':1,'project':c['project'],'snapshot_sha256':meta['sha256'],'migrated_sha256':migrated,
        'runtime_image':c['runtime_image'],'volumes':c['volumes'],'mark':mark,'manifests':dict(sources,ledger=r.manifest(staging)),
        'source_manifests':sources,'loaded_logical_sha256':mark['loaded_logical_sha256'],
        'startup_logical_sha256':mark['startup_logical_sha256'],'container_verification':None}

@pytest.mark.parametrize('value',['../source','innocent/../source','nested/../../source'])
def test_parent_traversal_rejected_before_any_path_operation(tmp_path,value):
    with pytest.raises(r.Refusal,match='traverses a parent'):r.path(tmp_path/value,exists=False)

def test_multivalue_env_and_literal_shell_text_are_data(setup,tmp_path):
    c,_=setup;p=tmp_path/'inventory.json';r.atomic_json(p,c)
    sentinel=tmp_path/'must-not-exist'
    with Path(c['env_file']).open('a') as f:
        f.write('ROLLOUT_BUS_CONSUMERS=forge-serve forge-serve-planning\nSANDBOX_ENV_NAMES=ONE TWO THREE\n')
        f.write('LITERAL=$(touch '+str(sentinel)+') ${NAME} `id`\n')
    assert r.config(p)['runtime_image']==r.RUNTIME and not sentinel.exists()

def test_snapshot_plan_preserves_pristine_wal_mode_snapshot(setup,monkeypatch):
    c,d=setup;snapshot_fixture(c,d,11)
    for suffix in ('-wal','-shm'):
        p=Path(str(d/'forge.db')+suffix)
        if p.exists():p.unlink()
    before=r.manifest(d)
    monkeypatch.setattr(r,'rendered',lambda c:{})
    assert r.load_volumes(c,d,plan=True)['plan']
    assert r.manifest(d)==before

@pytest.mark.parametrize('missing',['format_version','mark','migrated_sha256','manifests','source_manifests'])
def test_incomplete_receipt_cannot_authorize_empty_volumes(setup,missing):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta);receipt.pop(missing)
    with pytest.raises(r.Refusal,match='incomplete'):r.validate_receipt(c,meta,receipt)

def test_empty_or_forged_manifests_refuse(setup):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    receipt['manifests']['ledger']=[]
    with pytest.raises(r.Refusal,match='ledger and snapshot mark'):r.validate_receipt(c,meta,receipt)
    receipt=receipt | {'manifests':dict(receipt['manifests'],settings=[])}
    with pytest.raises(r.Refusal,match='original source'):r.validate_receipt(c,meta,receipt)

@pytest.mark.parametrize('changes',[{'Labels':{}},{'Driver':'nfs'},{'Options':{'type':'none','device':'/some/path','o':'bind'}},{'Scope':'global'}])
def test_existing_volume_configuration_checked_before_mount(setup,monkeypatch,changes):
    c,_=setup;name=c['volumes']['ledger'];model={'volumes':{'ledger':{'name':name}}}
    item={'Name':name,'Driver':'local','Scope':'local','Options':None,'Labels':{'com.docker.compose.project':c['project'],'com.docker.compose.volume':'ledger'}}
    item.update(changes)
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=json.dumps([item])))
    with pytest.raises(r.Refusal,match='ownership or storage'):r.volume_identity(c,name,model)

def test_unchanged_bytes_with_wrong_permissions_refuse(setup,monkeypatch):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    monkeypatch.setattr(r,'volume_identity',lambda *a:{})
    def unreadable(*a,**k):raise r.Refusal('ownership or mode differs')
    monkeypatch.setattr(r,'container_python',unreadable)
    with pytest.raises(r.Refusal,match='incorrect ownership and modes'):r.verify_loaded(c,receipt,{})

def test_live_source_reader_keeps_committed_wal_visible(tmp_path):
    p=seed(tmp_path/'wal.db',11)
    c=connect_writer(p);c.execute('PRAGMA wal_autocheckpoint=0')
    c.execute('INSERT INTO schema_version(version,applied_at) VALUES(12,"test")')
    try:
        assert Path(str(p)+'-wal').stat().st_size>0
        assert r.ledger_state(p)['schema_version']==12
        with pytest.raises(r.Refusal,match='nonempty WAL'):r.ledger_state(p,consolidated=True)
    finally:c.close()

def test_failed_actual_readback_clears_previous_verification(setup,monkeypatch):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    receipt['container_verification']={'services':{'coordinator':{'container_id':'removed'}}}
    r.atomic_json(d/'load-receipt.json',receipt)
    monkeypatch.setattr(r,'compose',lambda *a:SimpleNamespace(stdout=''))
    with pytest.raises(r.Refusal,match='no unique actual container'):
        r.verify_containers(c,d,meta,receipt,False)
    assert r.read_json(d/'load-receipt.json')['container_verification'] is None

def test_readback_code_uses_configured_user_and_creates_no_sidecars(setup,monkeypatch):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    target=d/'loaded-ledger'
    for suffix in ('-wal','-shm'):
        p=Path(str(target/'forge.db')+suffix)
        if p.exists():p.unlink()
    before=r.manifest(target);seen=[]
    monkeypatch.setattr(r,'compose',lambda c,*args:SimpleNamespace(stdout=args[-1]))
    def inspected(c,name):
        return {'Id':name,'Image':r.PUBLISHER_RUNTIME if name=='forge-publisher' else r.RUNTIME,
                'State':{'Running':True},'Mounts':[{'Destination':'/var/lib/forge','Type':'volume','Name':c['volumes']['ledger'],'RW':name=='coordinator'}]}
    monkeypatch.setattr(r,'inspect',inspected)
    def execute(c,*argv,**kwargs):
        assert argv[0]=='exec' and '--user' not in argv
        code=argv[-1].replace('/var/lib/forge/forge.db',str(target/'forge.db'))
        child=subprocess.run(['python','-c',code],capture_output=True,text=True)
        assert child.returncode==0,child.stderr
        seen.append(argv[1]);return child
    monkeypatch.setattr(r,'docker',execute)
    assert len(r.verify_containers(c,d,meta,receipt,False)['container_verification'])==3
    assert len(seen)==3 and r.manifest(target)==before

def test_sibling_prefix_is_not_mistaken_for_source_root(setup,monkeypatch):
    c,d=setup;sibling=Path(c['source_db']).parent.with_name('source-sibling');sibling.mkdir()
    c['snapshot_root']=str(sibling)
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=''))
    assert r.snapshot(c,sibling/d.name,plan=True)['plan']
    assert not list(sibling.iterdir())

def test_fresh_process_import_boundary_and_real_writer(tmp_path):
    # A fresh interpreter is required: a package initializer may hide a client
    # import even when the requested submodule contains only event dataclasses.
    code = '''import importlib.abc,runpy,sys,tempfile,pathlib,json
class NoBrokerClient(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='nats' or fullname.startswith('nats.') or fullname=='nats_core.client':
   raise AssertionError('real broker-client import attempted: '+fullname)
sys.meta_path.insert(0,NoBrokerClient())
module=runpy.run_path(sys.argv[1])
with tempfile.TemporaryDirectory() as temporary:
 p=module['seed'](pathlib.Path(temporary)/'forge.db',build=True)
 assert module['r'].ledger_state(p)['work_state']['builds']['count']==1
clients=[n for n in sys.modules if n=='nats' or n.startswith('nats.')]
assert not clients,clients
assert sys.modules['nats_core.client'].__rollout_mock__ is True
print(json.dumps({'real_writer_rows':1,'client_modules':clients,'client_boundary_mocked':True}))
'''
    result=subprocess.run(['python','-c',code,str(Path(__file__).resolve())],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'real_writer_rows':1,'client_modules':[],'client_boundary_mocked':True}

def test_logical_digest_covers_all_rows_schema_and_blobs(tmp_path):
    p=seed(tmp_path/'forge.db')
    db=sqlite3.connect(p)
    initial=r.logical_digest(db)
    db.execute('CREATE TABLE extra_history(value BLOB, note TEXT)');db.commit()
    with_table=r.logical_digest(db);assert with_table!=initial
    db.executemany('INSERT INTO extra_history VALUES(?,?)',[(b'\x00\xff','same'),(b'\x00\xff','same')]);db.commit()
    duplicate=r.logical_digest(db);assert duplicate!=with_table
    db.execute('DELETE FROM extra_history WHERE rowid=1');db.commit()
    assert r.logical_digest(db)!=duplicate
    db.execute('CREATE INDEX extra_history_note ON extra_history(note)');db.commit()
    with_index=r.logical_digest(db)
    db.execute('UPDATE schema_version SET applied_at="changed" WHERE version=1');db.commit()
    assert r.logical_digest(db)!=with_index
    db.close()

def test_packaged_boot_digest_is_deterministic_and_broker_free(tmp_path):
    original=seed(tmp_path/'loaded.db',11)
    db=connect_writer(original);migrations.apply_at_boot(db);db.close()
    before=r.consolidated_logical_digest(original)
    copies=[]
    for name in ('first.db','second.db'):
        target=tmp_path/name;shutil.copyfile(original,target)
        code=r.BOOT_SQLITE_CODE.replace('/copy/forge.db',str(target))
        result=subprocess.run(['python','-c',code],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        copies.append(r.consolidated_logical_digest(target))
    assert copies[0]==copies[1] and copies[0]!=before
    assert r.consolidated_logical_digest(original)==before

@pytest.mark.parametrize('field',['loaded_logical_sha256','startup_logical_sha256'])
def test_missing_or_mismatched_runtime_digest_refuses(setup,field):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    receipt[field]='0'*64
    with pytest.raises(r.Refusal,match='provenance'):r.validate_receipt(c,meta,receipt)
    receipt.pop(field)
    with pytest.raises(r.Refusal,match='incomplete'):r.validate_receipt(c,meta,receipt)

def test_normal_boot_wal_and_checkpoint_verify_but_data_change_refuses(setup,monkeypatch):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    target=d/'loaded-ledger';ledger=target/'forge.db'
    expected=d/'expected-boot.db';shutil.copyfile(ledger,expected)
    result=subprocess.run(['python','-c',r.BOOT_SQLITE_CODE.replace('/copy/forge.db',str(expected))],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    startup=r.consolidated_logical_digest(expected)
    receipt['startup_logical_sha256']=receipt['mark']['startup_logical_sha256']=startup
    r.atomic_json(target/r.MARK,receipt['mark']);receipt['manifests']['ledger']=r.manifest(target)
    monkeypatch.setattr(r,'compose',lambda c,*args:SimpleNamespace(stdout=args[-1]))
    monkeypatch.setattr(r,'inspect',lambda c,name:{'Id':name,'Image':r.PUBLISHER_RUNTIME if name=='forge-publisher' else r.RUNTIME,
        'State':{'Running':True},'Mounts':[{'Destination':'/var/lib/forge','Type':'volume','Name':c['volumes']['ledger'],'RW':name=='coordinator'}]})
    def execute(c,*argv,**kwargs):
        assert argv[0]=='exec' and '--user' not in argv
        child=subprocess.run(['python','-c',argv[-1].replace('/var/lib/forge/forge.db',str(ledger))],capture_output=True,text=True)
        assert child.returncode==0,child.stderr
        return child
    monkeypatch.setattr(r,'docker',execute)
    assert r.verify_containers(c,d,meta,receipt,False)['container_verification']
    # Use the same packaged boot functions, retaining their real writer/WAL.
    namespace={}
    code=r.BOOT_SQLITE_CODE.split('assert not any(')[0].replace('/copy/forge.db',str(ledger))
    exec(code.replace("c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close()",''),namespace)
    writer=namespace['c']
    try:
        assert Path(str(ledger)+'-wal').stat().st_size>0
        assert r.verify_containers(c,d,meta,receipt,False)['container_verification']['coordinator']['logical_sha256']==startup
        before=writer.execute('SELECT applied_at FROM schema_version WHERE version=1').fetchone()[0]
        writer.execute('UPDATE schema_version SET applied_at=? WHERE version=1',('unapproved',));writer.commit()
        with pytest.raises(r.Refusal,match='different ledger'):r.verify_containers(c,d,meta,receipt,False)
        assert r.read_json(d/'load-receipt.json')['container_verification'] is None
        writer.execute('UPDATE schema_version SET applied_at=? WHERE version=1',(before,));writer.commit()
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        assert r.verify_containers(c,d,meta,receipt,False)['container_verification']
    finally:writer.close()
    assert r.sha256(d/'forge.db')==meta['sha256']


def test_forged_self_consistent_startup_claim_refuses(setup):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    receipt['startup_logical_sha256']=receipt['mark']['startup_logical_sha256']='f'*64
    marker=(json.dumps(receipt['mark'],sort_keys=True,indent=2)+'\n').encode()
    import hashlib
    for entry in receipt['manifests']['ledger']:
        if entry['path']==r.MARK:entry.update(size=len(marker),sha256=hashlib.sha256(marker).hexdigest())
    r.validate_receipt(c,meta,receipt)
    with pytest.raises(r.Refusal,match='independently derived'):r.verify_derivation(c,d,receipt)

@pytest.mark.parametrize('failure',['render','snapshot','incomplete'])
def test_public_preflight_clears_success_before_refusal(setup,monkeypatch,failure):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    receipt['container_verification']={'verified_at':'old','services':{'coordinator':{'container_id':'stale'}}}
    if failure=='incomplete':receipt.pop('mark')
    r.atomic_json(d/'load-receipt.json',receipt)
    def refuse(*a,**k):raise r.Refusal('preflight failed')
    monkeypatch.setattr(r,'rendered',refuse if failure=='render' else lambda c:{})
    if failure=='snapshot':(d/'forge.db').write_bytes(b'changed snapshot')
    with pytest.raises(r.Refusal):r.load_volumes(c,d,verify=True)
    assert r.read_json(d/'load-receipt.json')['container_verification'] is None

def test_early_invalidation_preserves_unparseable_bytes_and_symlink_target(setup,tmp_path):
    c,d=setup;snapshot_fixture(c,d);p=d/'load-receipt.json';p.write_bytes(b'{broken original')
    with pytest.raises(r.Refusal,match='malformed'):r.invalidate_verification(d)
    assert p.read_bytes()==b'{broken original'
    p.unlink();other=tmp_path/'other.json';other.write_text('{"container_verification":{"old":true}}');p.symlink_to(other)
    with pytest.raises(r.Refusal,match='symlink'):r.invalidate_verification(d)
    assert json.loads(other.read_text())['container_verification']=={'old':True}

def test_verify_plan_preserves_prior_attestation_even_on_failure(setup,monkeypatch):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    receipt['container_verification']={'old':True};r.atomic_json(d/'load-receipt.json',receipt)
    before=r.manifest(d)
    monkeypatch.setattr(r,'rendered',lambda c:(_ for _ in ()).throw(r.Refusal('wrong image')))
    with pytest.raises(r.Refusal):r.load_volumes(c,d,plan=True,verify=True)
    assert r.manifest(d)==before

@pytest.mark.parametrize('change',['bytes','permissions','missing'])
def test_retained_artifact_must_remain_original_private_bytes(setup,change):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta);p=d/r.MIGRATED_ARTIFACT
    if change=='bytes':p.write_bytes(b'changed')
    elif change=='permissions':p.chmod(0o644)
    else:p.unlink()
    with pytest.raises(r.Refusal):r.verify_derivation(c,d,receipt)

def test_cli_configuration_failure_clears_success_before_config_read(setup,monkeypatch,tmp_path):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    receipt['container_verification']={'old':True};r.atomic_json(d/'load-receipt.json',receipt)
    invalid=tmp_path/'invalid-config.json';invalid.write_text('{bad config')
    monkeypatch.setattr(sys,'argv',['rollout-load-volumes','--config',str(invalid),'--snapshot',str(d),'--verify-containers'])
    assert r.cli('load')==2
    assert r.read_json(d/'load-receipt.json')['container_verification'] is None

def test_derivation_plan_has_no_disposable_copy_or_container(setup,monkeypatch):
    c,d=setup;meta=snapshot_fixture(c,d);receipt=receipt_fixture(c,d,meta)
    before=r.manifest(d.parent)
    monkeypatch.setattr(r,'container_python',lambda *a,**k:pytest.fail('plan launched derivation'))
    r.verify_derivation(c,d,receipt,plan=True)
    assert r.manifest(d.parent)==before


def test_early_invalidation_refuses_unrelated_directory(tmp_path):
    unrelated=tmp_path/'other';unrelated.mkdir();p=unrelated/'load-receipt.json'
    original=b'{"container_verification":{"unrelated":true}}';p.write_bytes(original)
    with pytest.raises(r.Refusal,match='dated snapshot'):r.invalidate_verification(unrelated)
    assert p.read_bytes()==original


def test_early_invalidation_refuses_hard_linked_malformed_object(setup,tmp_path):
    import os
    c,d=setup;snapshot_fixture(c,d)
    original=b'{"container_verification":{"old":true},"incomplete":true}'
    other=tmp_path/'original-input.json';other.write_bytes(original)
    receipt=d/'load-receipt.json';os.link(other,receipt)
    with pytest.raises(r.Refusal,match='hard link'):r.invalidate_verification(d)
    assert other.read_bytes()==receipt.read_bytes()==original
    assert other.stat().st_ino==receipt.stat().st_ino
