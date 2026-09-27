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
from forge.lifecycle import migrations
from forge.adapters.sqlite.connect import connect_writer
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from nats_core.events import BuildQueuedPayload

BUNDLE = Path(__file__).resolve().parents[3] / 'deploy' / 'estate'
spec = importlib.util.spec_from_file_location('rollout_support', BUNDLE / 'rollout_support.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

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
        SqliteLifecyclePersistence(connection=db, db_path=path).record_pending_build(BuildQueuedPayload(
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
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=''))
    monkeypatch.setattr(r,'volume_inventory',lambda c:{k:None for k in r.VOLUME_ROLES})

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
    monkeypatch.setattr(r,'volume_inventory',lambda c:{k:copies[v] for k,v in c['volumes'].items()})
    assert r.load_volumes(c,d)['idempotent']

def test_occupied_volume_refuses_before_migration(setup,monkeypatch):
    c,d=setup;snapshot_fixture(c,d);mock_load(monkeypatch)
    monkeypatch.setattr(r,'volume_inventory',lambda c:{k:[{'path':'forge.db'}] for k in r.VOLUME_ROLES})
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
    receipt={'snapshot_sha256':meta['sha256'],'volumes':c['volumes'],'mark':{'snapshot_sha256':meta['sha256']}}
    monkeypatch.setattr(r,'compose',lambda *a:SimpleNamespace(stdout='actual-container'))
    monkeypatch.setattr(r,'inspect',lambda *a:{'Id':'actual-container','Image':r.RUNTIME,'State':{'Running':True},'Mounts':[{'Destination':'/var/lib/forge','Type':'volume','Name':c['volumes']['ledger']}]})
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout='{}'))
    with pytest.raises(r.Refusal,match='different snapshot mark'):r.verify_containers(c,d,meta,receipt,False)
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
    monkeypatch.setattr(r,'volume_inventory',lambda c:inventory)
    Path(c['sources']['settings']).write_text('changed input')
    with pytest.raises(r.Refusal,match='receipt or destination'):r.load_volumes(c,d)

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
