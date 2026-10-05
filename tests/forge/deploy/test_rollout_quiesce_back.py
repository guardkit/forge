"""Owned offline tests; no real broker client may even be imported."""
import sys,types,importlib.abc,importlib.machinery,importlib.util
class NoClient(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name=='nats' or name.startswith('nats.') or name=='nats_core.client':raise AssertionError('real broker client import')
sys.meta_path.insert(0,NoClient())
if 'nats_core.client' not in sys.modules:
    mock=types.ModuleType('nats_core.client');mock.__rollout_mock__=True
    def forbidden(*a,**k):raise AssertionError('broker client use')
    mock.NATSClient=forbidden;mock.NATSKVManifestRegistry=forbidden;sys.modules['nats_core.client']=mock
assert getattr(sys.modules['nats_core.client'],'__rollout_mock__',False)
import json,os,sqlite3,subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from forge.adapters.sqlite.connect import connect_writer
from forge.gating.identity import derive_request_id
from forge.lifecycle.migrations import apply_at_boot
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
HERE=Path(__file__).resolve().parents[3]/'deploy/estate'
def module(name):
    loader=importlib.machinery.SourceFileLoader('test_'+name,str(HERE/name));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m);return m
b=module('rollout-back');q=b.q;r=q.r

def monitor(pending=0,ack=0):
    return {'account_details':[{'name':'factory','stream_detail':[{'name':'PIPELINE','consumer_detail':[{'name':name,'num_pending':pending,'num_ack_pending':ack} for name in ('forge-serve','forge-serve-planning')]}]}]}

@pytest.fixture
def estate(tmp_path):
    old=tmp_path/'old';old.mkdir();prepared=tmp_path/'prepared';prepared.mkdir();snaproot=tmp_path/'snapshots';snaproot.mkdir();snap=snaproot/'20260927T110000Z';snap.mkdir()
    db=old/'forge.db';cx=connect_writer(db);apply_at_boot(cx);cx.close()
    for folder in (old/'evidence',old/'threads'):folder.mkdir()
    (old/'relay-progress.json').write_text('{}');(old/'forge.yaml').write_text('planning:\n  enabled: false\n');(prepared/'forge.yaml').write_text('planning:\n  enabled: false\n')
    env='FORGE_IMAGE='+r.RUNTIME+'\nBUS_MODE=external\nBUS_EXTERNAL_NETWORK=none\nFORGE_NATS_URL=nats://fake.invalid:14222\nBUS_MONITORING_ADDRESS=fake.invalid:18222\nROLLOUT_BUS_STREAM=PIPELINE\nROLLOUT_BUS_CONSUMERS=forge-serve forge-serve-planning\n'
    publisher_settings=tmp_path/'publisher.json';publisher_settings.write_text('{"host":"0.0.0.0","port":8711}')
    env+='FORGE_PUBLISHER_SETTINGS_FILE='+str(publisher_settings)+'\n'
    for p in (old/'estate.env',prepared/'estate.env'):p.write_text(env)
    compose=tmp_path/'compose.json';compose.write_text('{}')
    c={'project':'codex-quiesce-test','runtime_image':r.RUNTIME,'docker_context':'default','source_db':str(db),'snapshot_root':str(snaproot),'units':{x:'owned-'+x+'.service' for x in r.UNIT_ROLES},'old_containers':{x:'owned-'+x for x in ('coordinator','memory','relay')},'volumes':{x:'owned-'+x for x in r.VOLUME_ROLES},'sources':{'settings':str(old/'forge.yaml'),'evidence':str(old/'evidence'),'threads':str(old/'threads'),'relay_progress':str(old/'relay-progress.json')},'env_file':str(old/'estate.env'),'compose_files':[str(compose)],'quiesce':{'receipt':str(tmp_path/'quiesce.json'),'original_env_file':str(old/'estate.env'),'prepared_env_file':str(prepared/'estate.env'),'original_settings':str(old/'forge.yaml'),'prepared_settings':str(prepared/'forge.yaml'),'settings_receipt':str(prepared/'receipt.json'),'actor':'fixture','release_tag':'fixture:release'}}
    path=tmp_path/'inventory.json';r.atomic_json(path,c)
    args=SimpleNamespace(config=str(path),env_file=c['env_file'],project=c['project'],snapshot=str(snap),secret_env_file=[],plan=False,candidate_image=None,candidate_env_file=None)
    return q.Estate(args)

@pytest.mark.parametrize('pending,ack',[(1,0),(0,1),(1,1)])
def test_ack_and_pending_never_zero(pending,ack):
    with pytest.raises(r.Refusal) as refused:q.quiet_counts(q.reader_counts(monitor(pending,ack)))
    message=str(refused.value)
    assert 'reader forge-serve' in message
    assert f'pending={pending}' in message
    assert f'unacknowledged={ack}' in message
def test_zero_counts_are_quiet():
    assert q.quiet_counts(q.reader_counts(monitor(0,0))) is None
@pytest.mark.parametrize('value',[None,'0',False,-1])
def test_unknown_count_is_not_zero(value):
    with pytest.raises(r.Refusal):q.reader_counts(monitor(value))
def test_duplicate_account_refuses():
    doc=monitor();doc['account_details']*=2
    with pytest.raises(r.Refusal):q.reader_counts(doc)
def test_missing_reader_refuses():
    doc=monitor();doc['account_details'][0]['stream_detail'][0]['consumer_detail'].pop()
    with pytest.raises(r.Refusal):q.reader_counts(doc)

def test_plan_no_helpers_receipts_or_sidecars(estate,monkeypatch):
    before=r.manifest(estate.receipt.parent)
    monkeypatch.setattr(estate,'helper',lambda *a,**k:pytest.fail('plan helper'))
    monkeypatch.setattr(r,'run',lambda *a,**k:pytest.fail('plan mutation'))
    assert estate.plan('close')['observations']['work']['schema_version']==r.LEDGER_SCHEMA_VERSION
    assert r.manifest(estate.receipt.parent)==before

def test_current_phase_constructor_never_stats_old_folder(estate,monkeypatch):
    c=estate.c;c['env_file']=estate.q['prepared_env_file'];c['sources']['settings']=estate.q['prepared_settings'];r.atomic_json(estate.config_path,c)
    old=Path(c['source_db']).parent
    original=Path.stat
    def no_old(path,*a,**kw):
        if path==old or old in path.parents:pytest.fail('touched old folder')
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,'stat',no_old)
    args=estate.args;args.env_file=c['env_file'];new=q.Estate(args);assert new.phase=='prepared'

def test_phase_transition_binding_is_stable(estate):
    before=estate.binding;c=estate.c;c['env_file']=estate.q['prepared_env_file'];c['sources']['settings']=estate.q['prepared_settings'];r.atomic_json(estate.config_path,c);estate.args.env_file=c['env_file']
    assert q.Estate(estate.args).binding==before

def test_close_receipt_alias_refuses_before_any_operation(estate):
    c=estate.c;c['quiesce']['receipt']=c['source_db'];r.atomic_json(estate.config_path,c)
    with pytest.raises(r.Refusal):q.Estate(estate.args)

def test_close_timer_service_settle_mask_order(estate,monkeypatch):
    unit={'LoadState':'loaded','ActiveState':'inactive','SubState':'dead','MainPID':'0','ControlPID':'0','UnitFileState':'disabled'};events=[]
    monkeypatch.setattr(estate,'markers',lambda:None);monkeypatch.setattr(estate,'unit',lambda name:dict(unit));monkeypatch.setattr(estate,'systemctl',lambda *a:events.append(a))
    monkeypatch.setattr(r,'inspect',lambda c,n:{'Config':{'Labels':{}},'Id':n});monkeypatch.setattr(r,'previous_runtime',lambda c,item:{'service_identity':{'container_id':item['Id']}})
    estate.close()
    assert events[:4]==[('stop',estate.c['units']['watchdog_timer']),('stop',estate.c['units']['watchdog_service']),('mask',estate.c['units']['watchdog_timer']),('mask',estate.c['units']['watchdog_service'])]
    assert set(estate.doc['old_runtimes'])=={'coordinator','memory','relay'}

def test_alarm_unsettled_refuses_before_mask(estate,monkeypatch):
    estate.doc={'binding':estate.binding,'format_version':1,'stage':'recorded'};estate.save();events=[]
    monkeypatch.setattr(estate,'markers',lambda:None);monkeypatch.setattr(estate,'systemctl',lambda *a:events.append(a));monkeypatch.setattr(estate,'unit',lambda name:{'ActiveState':'active','MainPID':'42','ControlPID':'0'})
    with pytest.raises(r.Refusal):estate.close()
    assert all(x[0]!='mask' for x in events)

def test_inflight_arriving_second_read_refuses(estate,monkeypatch):
    estate.doc={'observations':[]};monkeypatch.setattr(estate,'closed',lambda:None);counts=iter([q.reader_counts(monitor()),q.reader_counts(monitor(1))]);monkeypatch.setattr(estate,'monitor',lambda:next(counts));monkeypatch.setattr(estate,'state',lambda:{'work_state':{}});monkeypatch.setattr(q.time,'sleep',lambda n:None)
    with pytest.raises(r.Refusal):estate.settle()


def public_final_argv(estate):
    return ['--config',str(estate.config_path),'--env-file',estate.c['env_file'],
            '--project',estate.c['project'],'--snapshot',str(estate.snapshot),'--final']


@pytest.mark.parametrize('partial',[False,True])
def test_public_final_preserves_sanitized_sandbox_refusal_before_legacy_stops(estate,monkeypatch,capsys,partial):
    estate.snapshot.rmdir()
    estate.phase='original';estate.doc={'format_version':1,'binding':estate.binding,'stage':'settled'}
    monkeypatch.setattr(estate,'closed',lambda:None);legacy=[]
    monkeypatch.setattr(estate,'systemctl',lambda *a:legacy.append(('systemctl',a)))
    monkeypatch.setattr(r,'inspect',lambda *a:legacy.append(('inspect',a)))
    estate.private_values=['sentinel-private-value']
    if partial:
        child='Refusing: systemctl unmask refused for unit owned-runner.service; current-template/profile installation may be incomplete, so reconcile the private evidence before unmasking either unit; sentinel-private-value.\n'
    else:
        child='Refusing: known_files changed for ["known-file.txt"]; systemctl stop refused for unit owned-keeper.service; nothing has been replaced, and work may still be running inside it — shall I try again, or put it back as it was and stop for today? sentinel-private-value\n'
    def refused(argv,**kwargs):
        assert Path(argv[0]).name=='rollout-sandbox' and '--stop-legacy' in argv
        return SimpleNamespace(returncode=2,stdout='sentinel-private-value stdout',stderr=child)
    monkeypatch.setattr(r,'run',refused);monkeypatch.setattr(q,'Estate',lambda args:estate)
    assert q.main(public_final_argv(estate))==2
    error=capsys.readouterr().err
    assert 'sentinel-private-value' not in error and '[REDACTED]' in error
    assert legacy==[]
    report=r.read_json(estate.sandbox_evidence)
    assert report['passed'] is False and report['commands'][0]['exit']==2
    assert 'sentinel-private-value' not in json.dumps(report) and '[REDACTED]' in json.dumps(report)
    assert estate.sandbox_evidence.stat().st_mode&0o777==0o600
    assert not estate.snapshot.exists()
    if partial:
        assert 'installation may be incomplete' in error
        assert 'nothing has been replaced' not in error
    else:
        assert 'known-file.txt' in error and 'owned-keeper.service' in error
        assert 'shall I try again, or put it back as it was and stop for today?' in error


def test_sandbox_final_success_records_sanitized_command(estate,monkeypatch):
    estate.snapshot.rmdir()
    estate.private_values=['sentinel-private-value']
    monkeypatch.setattr(r,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='ok sentinel-private-value',stderr=''))
    result=estate.sandbox_final([HERE/'rollout-sandbox','--stop-legacy'])
    assert result.returncode==0
    report=r.read_json(estate.sandbox_evidence)
    assert report['passed'] is True and report['commands'][0]['exit']==0
    assert 'sentinel-private-value' not in json.dumps(report) and '[REDACTED]' in json.dumps(report)
    assert not estate.snapshot.exists()


def test_public_final_success_reaches_legacy_stop_before_snapshot_creation(estate,monkeypatch,capsys):
    estate.snapshot.rmdir();estate.phase='original';estate.doc={'format_version':1,'binding':estate.binding,'stage':'settled'}
    monkeypatch.setattr(estate,'closed',lambda:None);legacy=[]
    def stop(*args):
        legacy.append(args);raise r.Refusal('captured expected legacy stop')
    monkeypatch.setattr(estate,'systemctl',stop)
    monkeypatch.setattr(r,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='',stderr=''))
    monkeypatch.setattr(q,'Estate',lambda args:estate)
    assert q.main(public_final_argv(estate))==2
    assert legacy and 'captured expected legacy stop' in capsys.readouterr().err
    assert r.read_json(estate.sandbox_evidence)['passed'] is True
    assert not estate.snapshot.exists()


def test_public_final_timeout_is_unknown_and_never_claims_nothing_replaced(estate,monkeypatch,capsys):
    estate.snapshot.rmdir()
    estate.phase='original';estate.doc={'format_version':1,'binding':estate.binding,'stage':'settled'}
    monkeypatch.setattr(estate,'closed',lambda:None);legacy=[]
    monkeypatch.setattr(estate,'systemctl',lambda *a:legacy.append(a));monkeypatch.setattr(r,'inspect',lambda *a:legacy.append(a))
    def timeout(argv,**kwargs):raise subprocess.TimeoutExpired(argv,180,stderr='sentinel-private-value')
    estate.private_values=['sentinel-private-value'];monkeypatch.setattr(r,'run',timeout);monkeypatch.setattr(q,'Estate',lambda args:estate)
    assert q.main(public_final_argv(estate))==2
    error=capsys.readouterr().err
    assert 'outcome is unknown and installation may be incomplete' in error
    assert 'nothing has been replaced' not in error and 'sentinel-private-value' not in error
    assert legacy==[]
    report=r.read_json(estate.sandbox_evidence)
    assert report['passed'] is False and report['commands'][0]['exit'] is None
    assert not estate.snapshot.exists()


def test_child_refusal_survives_command_evidence_write_failure(estate,monkeypatch):
    estate.snapshot.rmdir()
    child='Refusing: known-file.txt changed; nothing has been replaced; shall I try again?\n'
    monkeypatch.setattr(r,'run',lambda *a,**k:SimpleNamespace(returncode=2,stdout='',stderr=child))
    monkeypatch.setattr(r,'atomic_json',lambda *a,**k:(_ for _ in ()).throw(OSError('fixture evidence write failed')))
    with pytest.raises(r.Refusal,match='known-file.txt changed'):
        estate.sandbox_final([HERE/'rollout-sandbox','--stop-legacy'])
    assert not estate.snapshot.exists()

@pytest.mark.parametrize('shape',['half','mismatch','invalid'])
def test_marker_pair_refuses_ambiguous(estate,monkeypatch,shape):
    marker={'format_version':1,'project':estate.c['project'],'snapshot':str(estate.snapshot)};r.atomic_json(estate.snapshot/'resumed.json',marker)
    monkeypatch.setattr(estate,'volume_exists',lambda:True);other=None if shape=='half' else (dict(marker,project='other') if shape=='mismatch' else [])
    monkeypatch.setattr(estate,'volume',lambda *a,**k:json.dumps(other))
    with pytest.raises(r.Refusal):estate.markers()

@pytest.mark.parametrize('change',['publication-finished','low-target-counter','plan-only','external-effect-unrecorded','crash-after-marker'])
def test_marker_alone_blocks_before_restore_and_legacy_reopen(estate,monkeypatch,change):
    recovery=b.Recovery(estate.args);monkeypatch.setattr(recovery,'markers',lambda:{'resumed':change})
    monkeypatch.setattr(recovery,'record',lambda:pytest.fail('restoration preflight after marker'))
    with pytest.raises(r.Refusal):recovery.before()
    with pytest.raises(r.Refusal):recovery.reopen()

def test_work_questions_keep_detail_and_not_yet():
    state={'work_state':{'publication_records':{'status':'observed','rows':[{'result':'publication pending','build_id':'b','turn':2}]},'deployment_targets':{'status':'NOT-YET','introduced_in':16},'planning_runs':{'status':'observed','rows':[{'state':'FEATURE_PLAN'}]}}}
    assert [x['table'] for x in q.work_problems(state)]==['publication_records','planning_runs']

def test_moved_tag_refuses_with_actual_id_evidence(estate,monkeypatch):
    recovery=b.Recovery(estate.args);record={'image_id':r.RUNTIME,'repo_tags':['fixture:previous']}
    monkeypatch.setattr(r,'docker',lambda c,*a,**k:SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME if a[-1]==r.RUNTIME else 'sha256:'+'0'*64}])))
    with pytest.raises(r.Refusal,match='tag fixture:previous now resolves'):recovery.validate_runtime(record)

def test_resume_pair_precedes_settings_restart_and_post_checks(estate,monkeypatch):
    estate.phase='prepared';estate.doc={'format_version':1,'binding':estate.binding,'stage':'final'};estate.save();events=[];pair={}
    monkeypatch.setattr(estate,'markers',lambda:pair.get('marker'));monkeypatch.setattr(estate,'record',lambda:estate.doc);monkeypatch.setattr(estate,'prepared_settings',lambda:None);monkeypatch.setattr(estate,'unit',lambda n:{});monkeypatch.setattr(estate,'watch_closed',lambda **kw:None);monkeypatch.setattr(estate,'producers_stopped',lambda:None);monkeypatch.setattr(estate,'monitor',lambda:q.reader_counts(monitor()));monkeypatch.setattr(r,'load_volumes',lambda *a,**k:events.append('state-proof'));monkeypatch.setattr(r,'verify_snapshot',lambda d:{'sha256':'a'*64});monkeypatch.setattr(estate,'current_state',lambda:{'work_state':{}});estate.values['ROLLOUT_STATE_DIR']=str(estate.snapshot)
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}])))
    def volume(role,code,args=(),**kwargs):pair['marker']=json.loads(args[0]);events.append('ledger-marker')
    monkeypatch.setattr(estate,'volume',volume)
    def planning(enabled):assert (estate.snapshot/'resumed.json').exists();events.append('planning')
    monkeypatch.setattr(estate,'planning',planning);monkeypatch.setattr(estate,'compose',lambda *a:events.append('watch' if 'gateway-watch' in a else 'compose'));monkeypatch.setattr(estate,'systemctl',lambda *a:events.append('watch'))
    monkeypatch.setattr(estate,'wait_ready',lambda commands:events.append('ready'))
    monkeypatch.setattr(r,'run',lambda argv,**kw:events.append('hello' if str(argv[0]).endswith('factory-hello') else ('services' if 'services' in argv else 'pre-resume')))
    assert estate.resume()['passed'];assert events.index('ledger-marker')<events.index('planning')<events.index('ready')<events.index('services')<events.index('hello')<events.index('watch')

def test_fresh_process_import_guard_and_h6_source(tmp_path):
    code=q.GUARD+'\nimport runpy,sys\nrunpy.run_path(sys.argv[1])\nassert not any(n=="nats" or n.startswith("nats.") for n in sys.modules)\n'
    p=subprocess.run([sys.executable,'-c',code,str(HERE/'rollout-back')],capture_output=True,text=True)
    assert p.returncode==0,p.stderr

@pytest.mark.parametrize('failing_check',['services','factory-hello'])
@pytest.mark.parametrize('real_child',[False,True])
def test_post_resume_failure_keeps_pair_and_records_failure(estate,monkeypatch,failing_check,real_child):
    estate.phase='prepared';estate.doc={'format_version':1,'binding':estate.binding,'stage':'final'};estate.save();pair={}
    monkeypatch.setattr(estate,'markers',lambda:pair.get('marker'));monkeypatch.setattr(estate,'record',lambda:estate.doc);monkeypatch.setattr(estate,'prepared_settings',lambda:None);monkeypatch.setattr(estate,'unit',lambda n:{});monkeypatch.setattr(estate,'watch_closed',lambda **kw:None);monkeypatch.setattr(estate,'producers_stopped',lambda:None);monkeypatch.setattr(estate,'monitor',lambda:q.reader_counts(monitor()));monkeypatch.setattr(r,'load_volumes',lambda *a,**k:None);monkeypatch.setattr(r,'verify_snapshot',lambda d:{'sha256':'a'*64});monkeypatch.setattr(estate,'current_state',lambda:{'work_state':{}});estate.values['ROLLOUT_STATE_DIR']=str(estate.snapshot)
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}])))
    monkeypatch.setattr(estate,'volume',lambda role,code,args=(),**kw:pair.update(marker=json.loads(args[0])))
    monkeypatch.setattr(estate,'planning',lambda enabled:None);monkeypatch.setattr(estate,'compose',lambda *a:pytest.fail('watch enabled after failed gate') if 'gateway-watch' in a else None)
    monkeypatch.setattr(estate,'systemctl',lambda *a:pytest.fail('invented host watch'));monkeypatch.setattr(estate,'wait_ready',lambda commands:None)
    original_run=r.run;estate.private_values=['fixture-private-value']
    def check(argv,**kw):
        if failing_check in [str(x) for x in argv] or str(argv[0]).endswith(failing_check):
            if real_child:return original_run([sys.executable,'-c',"import sys;print('item 9 failed fixture-private-value',file=sys.stderr);raise SystemExit(42)"],check=False)
            raise r.Refusal('named post-resume check failed')
    monkeypatch.setattr(r,'run',check)
    with pytest.raises(r.Refusal):estate.resume()
    assert pair['marker']==r.read_json(estate.snapshot/'resumed.json')
    report=r.read_json(estate.snapshot/'post-resume-check.json');assert not report['passed']
    assert report['cleanup']['current_authority_retained'] is True
    if real_child:
        event=report['commands'][-1];assert event['exit']==42 and 'item 9 failed' in event['stderr'] and '[REDACTED]' in event['stderr']
        assert event['stage']==('estate-check services' if failing_check=='services' else 'factory-hello')
        assert 'fixture-private-value' not in json.dumps(report)
        assert (estate.snapshot/'post-resume-check.json').stat().st_mode & 0o777==0o600
    assert estate.doc['stage']=='resumed'

def ready_fakes(estate,monkeypatch,*,coordinator_after=1,gateway_after=1,stuck=None,restart=None,discovery_seconds=0,probe_seconds=None,overrun=False):
    """Resume up to the open door with fakes; the opened services become ready at a given poll.
    The fake clock advances while containers are discovered and while each probe
    (inspect, curl, connz) runs; a probe slower than the timeout it was given
    times out at it, unless it overruns and answers late."""
    estate.phase='prepared';estate.doc={'format_version':1,'binding':estate.binding,'stage':'final'};estate.save();pair={}
    monkeypatch.setattr(estate,'markers',lambda:pair.get('marker'));monkeypatch.setattr(estate,'record',lambda:estate.doc);monkeypatch.setattr(estate,'prepared_settings',lambda:None);monkeypatch.setattr(estate,'unit',lambda n:{});monkeypatch.setattr(estate,'watch_closed',lambda **kw:None);monkeypatch.setattr(estate,'producers_stopped',lambda:None);monkeypatch.setattr(estate,'monitor',lambda:q.reader_counts(monitor()));monkeypatch.setattr(r,'load_volumes',lambda *a,**k:None);monkeypatch.setattr(r,'verify_snapshot',lambda d:{'sha256':'a'*64});monkeypatch.setattr(estate,'current_state',lambda:{'work_state':{}});estate.values['ROLLOUT_STATE_DIR']=str(estate.snapshot)
    monkeypatch.setattr(estate,'volume',lambda role,code,args=(),**kw:pair.update(marker=json.loads(args[0])));monkeypatch.setattr(estate,'planning',lambda enabled:None)
    estate.values.update(JARVIS_NATS_USER='jarvis',FACTORY_INSTANCE='fixture')
    seen={'polls':0,'services':0,'services_after_polls':None,'hello':0,'watch':0,'pair':pair,'asked':[]};clock=[0.0]
    monkeypatch.setattr(q,'time',SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds)))
    monkeypatch.setattr(estate,'compose',lambda *a:seen.__setitem__('watch',seen['watch']+1) if 'gateway-watch' in a else None)
    ids={name:'id-'+name for name in ('coordinator','front-door','bus-gateway')}
    def service_ids(services):
        clock[0]+=discovery_seconds;return {name:{'Id':ids[name],'RestartCount':0} for name in services}
    monkeypatch.setattr(estate,'service_ids',service_ids)
    def took(kind,kwargs):
        seconds=(probe_seconds or {}).get(kind,0);timeout=kwargs.get('timeout')
        seen['asked'].append((kind,timeout,q.READY_DEADLINE_SECONDS-clock[0]))
        if timeout is not None and seconds>timeout and not overrun:
            clock[0]+=timeout;raise subprocess.TimeoutExpired(['docker',kind],timeout)
        clock[0]+=seconds
    front_door={'authorized_user':'jarvis','name':'front-door-fixture','subscriptions_list':[]}
    gateway={'authorized_user':'jarvis','name':'bus-gateway-fixture','subscriptions_list':['agents.command.jarvis']}
    def docker(c,*args,**kwargs):
        if args[:2]==('image','inspect'):return SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}]),returncode=0)
        if args[0]=='inspect':
            took('inspect',kwargs);name=next(n for n,i in ids.items() if i==args[1])
            if name=='coordinator':seen['polls']+=1
            ready=seen['polls']>=coordinator_after if name=='coordinator' else name!=stuck
            state={'Running':True,'Restarting':False,'Status':'running'}
            if name in q.HEALTHCHECKED:state['Health']={'Status':'healthy' if ready else 'starting'}
            return SimpleNamespace(stdout=json.dumps([{'Id':args[1],'State':state,'RestartCount':(restart or {}).get(name,0)}]),returncode=0)
        if args[0]=='exec' and 'curl' in args:
            assert args[1]==ids['coordinator'];took('curl',kwargs);return SimpleNamespace(stdout='200',returncode=0)
        if args[0]=='exec' and 'python' in args:
            assert args[1]==ids['coordinator'] and args[-4:]==('fake.invalid:18222','jarvis','bus-gateway-fixture','agents.command.jarvis');took('connz',kwargs)
            on=stuck!='bus-gateway' and seen['polls']>=gateway_after
            return SimpleNamespace(stdout=str(q.gateway_connections({'connections':[front_door]+([gateway] if on else [])},*args[-3:]))+'\n',returncode=0)
        pytest.fail(f'unexpected docker call {args}')
    monkeypatch.setattr(r,'docker',docker)
    def run(argv,**kw):
        if str(argv[0]).endswith('factory-hello'):seen['hello']+=1
        elif 'services' in argv:seen['services']+=1;seen['services_after_polls']=seen['polls']
    monkeypatch.setattr(r,'run',run)
    return seen,clock

def test_resume_waits_for_readiness_then_checks_services_once(estate,monkeypatch):
    seen,clock=ready_fakes(estate,monkeypatch,coordinator_after=4,gateway_after=6)
    assert estate.resume()['passed']
    assert seen['services']==1 and seen['services_after_polls']==6 and seen['hello']==1 and seen['watch']==1 and clock[0]==5*q.READY_POLL_SECONDS
    report=r.read_json(estate.snapshot/'post-resume-check.json')
    assert report['passed'] and [e['stage'] for e in report['commands']]==['readiness-wait','estate-check services','factory-hello']
    wait=report['commands'][0];assert wait['ready'] is True and wait['polls']==6 and wait['not_ready']=={} and wait['waited_for']==['coordinator','front-door','bus-gateway']

@pytest.mark.parametrize('stuck,said',[('bus-gateway',"no connection yet with the client name 'bus-gateway-fixture'"),('front-door','front-door Docker health is starting')])
def test_resume_refuses_with_readiness_reason_before_services_check(estate,monkeypatch,stuck,said):
    seen,clock=ready_fakes(estate,monkeypatch,stuck=stuck)
    with pytest.raises(r.Refusal,match=f'readiness-wait: after {q.READY_DEADLINE_SECONDS} s these were still not ready: '):estate.resume()
    assert seen['services']==seen['hello']==seen['watch']==0
    assert q.READY_DEADLINE_SECONDS<=clock[0]<q.READY_DEADLINE_SECONDS+q.READY_POLL_SECONDS
    report=r.read_json(estate.snapshot/'post-resume-check.json')
    assert not report['passed'] and report['services_check'] is None and report['cleanup']['current_authority_retained'] is True
    assert report['failure']['stage']=='readiness-wait' and said in report['failure']['reason'] and 'services check was not run' in report['failure']['reason']
    event=report['commands'][-1];assert event['stage']=='readiness-wait' and event['ready'] is False and set(event['not_ready'])=={stuck} and said in event['reason']
    assert seen['pair']['marker']==r.read_json(estate.snapshot/'resumed.json') and estate.doc['stage']=='resumed'

def test_resume_refuses_at_once_when_an_opened_service_restarts(estate,monkeypatch):
    seen,clock=ready_fakes(estate,monkeypatch,restart={'bus-gateway':1})
    with pytest.raises(r.Refusal,match='bus-gateway has restarted since resume opened it'):estate.resume()
    assert seen['services']==0 and clock[0]==0
    report=r.read_json(estate.snapshot/'post-resume-check.json');assert report['failure']['stage']=='readiness-wait' and report['commands'][-1]['polls']==1

def assert_refused_before_services(estate,seen,said):
    """A readiness refusal: nothing after it ran, and the receipt and markers say so."""
    assert seen['services']==seen['hello']==seen['watch']==0
    report=r.read_json(estate.snapshot/'post-resume-check.json')
    assert not report['passed'] and report['services_check'] is None and report['cleanup']['current_authority_retained'] is True
    assert report['failure']['stage']=='readiness-wait' and said in report['failure']['reason'] and 'services check was not run' in report['failure']['reason']
    event=report['commands'][-1];assert event['stage']=='readiness-wait' and event['ready'] is False and said in event['reason']
    assert seen['pair']['marker']==r.read_json(estate.snapshot/'resumed.json') and estate.doc['stage']=='resumed'
    return event

def test_every_probe_is_bounded_by_its_cap_and_what_remains(estate,monkeypatch):
    # Slow probes within the deadline: ready at the sixth poll, 98 s in.
    seen,clock=ready_fakes(estate,monkeypatch,coordinator_after=4,gateway_after=6,discovery_seconds=20,probe_seconds={'inspect':2,'curl':4,'connz':5})
    assert estate.resume()['passed'] and seen['services']==1 and seen['services_after_polls']==6 and clock[0]==98
    assert seen['asked'] and all(timeout==min(q.READY_ASK_SECONDS,remaining) for kind,timeout,remaining in seen['asked'])
    wait=r.read_json(estate.snapshot/'post-resume-check.json')['commands'][0];assert wait['ready'] is True and wait['seconds']==98

@pytest.mark.parametrize('probe_seconds,said,polls,ended',[
    # A probe that hangs is cut at its own 30 s cap.
    ({'curl':1000},'readiness-wait: asking the coordinator for its health route got no answer within 30.0 s',1,30),
    # A slow probe still running at the deadline is cut there: seventh poll at 168 s, 12 s left.
    ({'connz':25},'readiness-wait: asking the bus at fake.invalid:18222, from inside the coordinator, for its connections got no answer within 12.0 s',7,180)])
def test_resume_refuses_naming_a_probe_that_outlives_its_budget(estate,monkeypatch,probe_seconds,said,polls,ended):
    seen,clock=ready_fakes(estate,monkeypatch,stuck='bus-gateway',probe_seconds=probe_seconds)
    with pytest.raises(r.Refusal,match='readiness-wait: asking '):estate.resume()
    assert clock[0]==ended and assert_refused_before_services(estate,seen,said)['polls']==polls

def test_resume_refuses_readiness_that_arrives_after_the_deadline(estate,monkeypatch):
    # Subprocess timeouts are not exact: the seventh connz starts 168 s in with
    # 12 s left and answers, ready, 13 s after the deadline.
    seen,clock=ready_fakes(estate,monkeypatch,gateway_after=7,probe_seconds={'connz':25},overrun=True)
    with pytest.raises(r.Refusal,match='readiness arrived after the deadline'):estate.resume()
    assert clock[0]==193 and assert_refused_before_services(estate,seen,'first seen ready 13.0 s after')['polls']==7

@pytest.mark.parametrize('discovery_seconds,ready',[(176,True),(179,False)])
def test_discovery_counts_inside_the_deadline_and_the_last_sleep_is_capped(estate,monkeypatch,discovery_seconds,ready):
    # Discovery takes 176 or 179 s; the first poll is not ready. From 176 s the
    # 3 s sleep leaves a second poll at 179 s; from 179 s the sleep is 1 s and
    # the deadline is reached with nothing seen ready, so there is no second poll.
    seen,clock=ready_fakes(estate,monkeypatch,coordinator_after=2,discovery_seconds=discovery_seconds)
    if ready:
        assert estate.resume()['passed'] and seen['services_after_polls']==2 and clock[0]==179
    else:
        with pytest.raises(r.Refusal,match=f'readiness-wait: after {q.READY_DEADLINE_SECONDS} s these were still not ready: coordinator Docker health is starting'):estate.resume()
        assert clock[0]==q.READY_DEADLINE_SECONDS and assert_refused_before_services(estate,seen,'coordinator Docker health is starting')['polls']==1

def test_discovery_calls_get_what_remains_and_a_hang_is_a_named_refusal(estate,monkeypatch):
    clock=[0.0];monkeypatch.setattr(q,'time',SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda seconds:pytest.fail('no poll after failed discovery')))
    estate.values.update(JARVIS_NATS_USER='jarvis');opened=('coordinator','front-door','bus-gateway');asked=[]
    model={'services':{name:{'image':r.RUNTIME} for name in opened},'volumes':{}}
    def run(argv,**kw):
        argv=[str(x) for x in argv];asked.append((argv,kw.get('timeout'),q.READY_DEADLINE_SECONDS-clock[0]))
        if 'config' in argv:clock[0]+=100;return SimpleNamespace(returncode=0,stdout=json.dumps(model),stderr='')
        if 'ps' in argv:
            if argv[-1]=='bus-gateway':clock[0]+=kw['timeout'];raise subprocess.TimeoutExpired(argv,kw['timeout'])
            clock[0]+=10;return SimpleNamespace(returncode=0,stdout='id-'+argv[-1]+'\n',stderr='')
        assert argv[3]=='inspect';clock[0]+=5;service=argv[-1][len('id-'):]
        return SimpleNamespace(returncode=0,stdout=json.dumps([{'Id':argv[-1],'Image':r.RUNTIME,'Mounts':[],'Config':{'Labels':{'com.docker.compose.project':estate.c['project'],'com.docker.compose.service':service}}}]),stderr='')
    monkeypatch.setattr(r,'run',run);events=[]
    with pytest.raises(r.Refusal,match=r'asking Docker for the containers resume opened \(compose config, compose ps and inspect\) got no answer within 50.0 s'):estate.wait_ready(events)
    # config 100 s, then ps (10 s) and inspect (5 s) of two services, leaving 50 s for the hanging ps.
    assert [timeout for argv,timeout,remaining in asked]==[180,80,70,65,55,50] and all(timeout==min(r.TIMEOUT_SECONDS,remaining) for argv,timeout,remaining in asked)
    assert events[-1]['stage']=='readiness-wait' and events[-1]['polls']==0 and 'got no answer within 50.0 s' in events[-1]['reason'] and events[-1]['seconds']==180
    assert estate.ready_budget()=={}

def test_gateway_is_counted_only_by_account_name_and_subscription_on_one_connection():
    doc={'connections':[{'authorized_user':'jarvis','name':'front-door-x','subscriptions_list':['agents.command.jarvis']},
                        {'authorized_user':'jarvis','name':'bus-gateway-x','subscriptions_list':[]},
                        {'authorized_user':'other','name':'bus-gateway-x','subscriptions_list':['agents.command.jarvis']}]}
    assert q.gateway_connections(doc,'jarvis','bus-gateway-x','agents.command.jarvis')==0
    doc['connections'].append({'authorized_user':'jarvis','name':'bus-gateway-x','subscriptions_list_detail':[{'subject':'agents.command.jarvis'}]})
    assert q.gateway_connections(doc,'jarvis','bus-gateway-x','agents.command.jarvis')==1
    assert q.gateway_connections({},'jarvis','bus-gateway-x','agents.command.jarvis')==0

def test_real_timer_has_no_service_pid_properties(estate,monkeypatch):
    monkeypatch.setattr(r,'run',lambda *a,**k:SimpleNamespace(stdout='LoadState=loaded\nActiveState=inactive\nSubState=dead\nUnitFileState=disabled\n'))
    assert estate.unit_stopped('owned-watch.timer')['MainPID']=='0'
    with pytest.raises(r.Refusal):estate.unit_stopped('owned-watch.service')

def test_network_transient_endpoint_fields_do_not_change_reconstruction():
    import copy
    item={'service_identity':{'container_id':'a'*64,'name':'/old'},'networks':{'internal':{'Aliases':['old','a'*12],'EndpointID':'first','IPAddress':'172.1.1.1'}},'image_id':'x','repo_tags':[],'mounts':[],'port_bindings':{},'restart_policy':{},'network_mode':'internal','env_names':['ONE']}
    changed=copy.deepcopy(item);changed['networks']['internal'].update(EndpointID='',IPAddress='')
    assert q.stable_runtime(item)==q.stable_runtime(changed)
    changed['networks']['internal']['Aliases']=['other']
    assert q.stable_runtime(item)!=q.stable_runtime(changed)

def test_runtime_seal_accepts_docker_cli_entrypoint_partition_only():
    base={'service_identity':{'container_id':'a'*64,'name':'/old','hostname':'old','user':'','working_dir':'/app','entrypoint':['/bin/sh','-c','exec "$@"','argv0'],'command':['python','-m','fleet_memory.mcp']},'networks':{'internal':{'Aliases':['old']}},'image_id':'x','repo_tags':[],'mounts':[],'port_bindings':{},'restart_policy':{},'network_mode':'internal','env_names':['ONE']}
    recreated=json.loads(json.dumps(base));recreated['service_identity']['container_id']='b'*64
    recreated['service_identity']['entrypoint']=['/bin/sh'];recreated['service_identity']['command']=['-c','exec "$@"','argv0','python','-m','fleet_memory.mcp']
    assert q.stable_runtime(base)==q.stable_runtime(recreated)
    recreated['service_identity']['command'][-1]='other.module'
    assert q.stable_runtime(base)!=q.stable_runtime(recreated)

def test_runtime_seal_accepts_only_plain_equivalent_bind_mode_spelling():
    base={'service_identity':{'container_id':'a'*64,'name':'/old'},'networks':{'internal':{'Aliases':['old']}},'image_id':'x','repo_tags':[],'port_bindings':{},'restart_policy':{},'network_mode':'internal','env_names':['ONE']}
    old=[{'Type':'bind','Source':'/owned/rw','Destination':'/rw','Mode':'rw','RW':True,'Propagation':'rprivate'},{'Type':'bind','Source':'/owned/ro','Destination':'/ro','Mode':'ro','RW':False,'Propagation':'rprivate'}]
    recreated=json.loads(json.dumps(base));base['mounts']=old;recreated['mounts']=[dict(x,Mode='') for x in old]
    assert q.stable_runtime(base)==q.stable_runtime(recreated)
    for field,value in [('Source','/different'),('Destination','/different'),('RW',False),('Propagation','rshared'),('Type','volume')]:
        changed=json.loads(json.dumps(recreated));changed['mounts'][0][field]=value
        assert q.stable_runtime(base)!=q.stable_runtime(changed)

def test_runtime_seal_treats_complete_unique_mount_inventory_as_unordered():
    base={'service_identity':{'container_id':'a'*64,'name':'/old'},'networks':{},'image_id':'x','repo_tags':[],'port_bindings':{},'restart_policy':{},'network_mode':'owned','env_names':[]}
    mounts=[{'Type':'bind','Source':'/owned/source-'+str(index),'Destination':'/owned/destination-'+str(index),'Mode':'rw','RW':True,'Propagation':'rprivate'} for index in range(7)]
    saved=dict(base,mounts=mounts);recreated=dict(base,mounts=list(reversed(json.loads(json.dumps(mounts)))))
    assert q.stable_runtime(saved)==q.stable_runtime(recreated)
    assert q.stable_runtime(saved)['mounts']==sorted(mounts,key=lambda mount:mount['Destination'])
    changed=json.loads(json.dumps(recreated));changed['mounts'][0]['Source']='/different';assert q.stable_runtime(saved)!=q.stable_runtime(changed)
    assert q.stable_runtime(saved)!=q.stable_runtime(dict(base,mounts=mounts[:-1]))
    assert q.stable_runtime(saved)!=q.stable_runtime(dict(base,mounts=mounts+[dict(mounts[0],Destination='/owned/extra')]))

@pytest.mark.parametrize('destination',['relative','//ambiguous','/not/../canonical','/trailing/','/contains\x00nul'])
def test_mount_inventory_refuses_malformed_destination(destination):
    with pytest.raises(r.Refusal,match='canonical absolute path'):
        q.stable_mount_inventory([{'Type':'volume','Destination':destination}])

def test_mount_inventory_refuses_duplicate_destination_before_sorting():
    mounts=[{'Type':'volume','Destination':'/same','Name':'one'},{'Type':'bind','Source':'/owned/two','Destination':'/same','Mode':'rw','RW':True,'Propagation':'rprivate'}]
    with pytest.raises(r.Refusal,match='ambiguous'):q.stable_mount_inventory(mounts)

@pytest.mark.parametrize('mode,rw',[('ro',True),('rw',False),('z',True),('Z',False),('ro,z',False),('cached',True)])
def test_bind_mode_with_other_or_contradictory_semantics_refuses(mode,rw):
    mount={'Type':'bind','Source':'/owned/source','Destination':'/target','Mode':mode,'RW':rw,'Propagation':'rprivate'}
    with pytest.raises(r.Refusal,match='unsupported or contradictory'):q.stable_mount(mount)

@pytest.mark.parametrize('change',[
    {'Consistency':'cached'},
    {'Propagation':'made-up'},
    {'Source':'/owned/source,readonly'},
    {'Destination':'/target\nother'},
])
def test_bind_options_not_reconstructed_by_docker_mount_refuse(change):
    mount={'Type':'bind','Source':'/owned/source','Destination':'/target','Mode':'rw','RW':True,'Propagation':'rprivate'};mount.update(change)
    with pytest.raises(r.Refusal):q.stable_mount(mount)


def named_volume(mode='rw',rw=True):
    return {'Type':'volume','Name':'owned-volume','Source':'/var/lib/docker/volumes/owned-volume/_data','Destination':'/owned','Driver':'local','Mode':mode,'RW':rw,'Propagation':''}

def volume_runtime(mount):
    network='owned-primary';return {'image_id':r.RUNTIME,'repo_tags':[],'mounts':[mount],'networks':{network:{'Aliases':[],'Links':None,'IPAMConfig':None,'DriverOpts':None}},'port_bindings':{},'restart_policy':{},'network_mode':network,'env_names':[],'service_identity':{'name':'/owned-coordinator','container_id':'a'*64,'hostname':'owned-host','user':'','working_dir':'/app','entrypoint':['python'],'command':['-c','raise SystemExit(0)']}}

def host_runtime(role):
    argv={'coordinator':['forge','--config','/var/forge/forge.yaml','serve'],'memory':['python','-m','fleet_memory.mcp'],'relay':['faststream','run','fleet_memory.app:app']}[role]
    entrypoint=argv[:1] if role=='coordinator' else []
    return {'image_id':r.RUNTIME,'repo_tags':[],'mounts':[],'networks':{'host':{'Aliases':None,'Links':None,'IPAMConfig':None,'DriverOpts':None}},'port_bindings':{},'restart_policy':{'Name':'no','MaximumRetryCount':0},'network_mode':'host','env_names':[],'service_identity':{'name':'/owned-'+role,'container_id':'a'*64,'hostname':'owned-host','user':'','working_dir':'/app','entrypoint':entrypoint,'command':argv[len(entrypoint):]}}

@pytest.mark.parametrize('role',['coordinator','memory','relay'])
def test_actual_legacy_host_runtime_shape_passes_reconstruction_preflight(estate,monkeypatch,role):
    recovery=b.Recovery(estate.args);events=[]
    def docker(c,*args,**kwargs):
        events.append(args)
        if args[:2]==('image','inspect'):return SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}]))
        pytest.fail('host preflight inspected a bridge or mutated a container: '+repr(args))
    monkeypatch.setattr(r,'docker',docker)
    recovery.validate_runtime(host_runtime(role))
    assert events==[('image','inspect',r.RUNTIME)]

def test_create_old_reconstructs_host_network_without_alias_or_publish_translation(estate,monkeypatch):
    recovery=b.Recovery(estate.args);record=host_runtime('coordinator');created='b'*64;events=[]
    recovery.doc={'format_version':1,'binding':recovery.binding,'rollback':{'stage':'stopped','created':{},'retired':[]}};journal=recovery.doc['rollback']
    monkeypatch.setattr(recovery,'old_values',lambda *a:{})
    def docker(c,*args,**kwargs):
        events.append(args)
        if args[:2]==('image','inspect'):return SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}]))
        if args[0]=='ps':return SimpleNamespace(stdout='')
        if args[0]=='create':return SimpleNamespace(stdout=created+'\n')
        pytest.fail('unexpected host reconstruction operation: '+repr(args))
    monkeypatch.setattr(r,'docker',docker)
    monkeypatch.setattr(r,'inspect',lambda *a:{'Id':created,'Name':'/owned-coordinator','Image':r.RUNTIME,'State':{'Running':False},'NetworkSettings':{'Networks':{'host':{}}}})
    monkeypatch.setattr(r,'previous_runtime',lambda *a:record)
    assert recovery.create_old('coordinator',record,journal)==created
    create=next(args for args in events if args[0]=='create')
    assert create[create.index('--network')+1]=='host'
    assert '--network-alias' not in create and '--publish' not in create
    assert all(args[:2]!=('network','inspect') and args[:2]!=('network','connect') and args[0]!='start' for args in events)

@pytest.mark.parametrize('case',['container','container-share','mixed','named-with-host','aliases','links','ipam','driver-options','published-port','malformed-port-bindings','missing-option','malformed-network-map','malformed-host-options'])
def test_unsafe_or_malformed_host_network_shape_refuses_before_container_lookup(estate,monkeypatch,case):
    record=host_runtime('memory')
    if case=='container':record['network_mode']='container'
    elif case=='container-share':record['network_mode']='container:0123456789ab'
    elif case=='mixed':record['networks']['owned-extra']={'Aliases':None,'Links':None,'IPAMConfig':None,'DriverOpts':None}
    elif case=='named-with-host':record['network_mode']='owned-primary';record['networks']['owned-primary']={'Aliases':None,'Links':None,'IPAMConfig':None,'DriverOpts':None}
    elif case=='aliases':record['networks']['host']['Aliases']=['owned-memory']
    elif case=='links':record['networks']['host']['Links']=['old:new']
    elif case=='ipam':record['networks']['host']['IPAMConfig']={'IPv4Address':'127.0.0.2'}
    elif case=='driver-options':record['networks']['host']['DriverOpts']={'x':'y'}
    elif case=='published-port':record['port_bindings']={'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'28000'}]}
    elif case=='malformed-port-bindings':record['port_bindings']=None
    elif case=='missing-option':del record['networks']['host']['DriverOpts']
    elif case=='malformed-network-map':record['networks']=[]
    elif case=='malformed-host-options':record['networks']['host']=[]
    recovery=b.Recovery(estate.args);events=[]
    def docker(c,*args,**kwargs):
        events.append(args)
        if args[:2]==('image','inspect'):return SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}]))
        pytest.fail('unsafe host shape reached network or container operation: '+repr(args))
    monkeypatch.setattr(r,'docker',docker);monkeypatch.setattr(recovery,'old_values',lambda *a:pytest.fail('unsafe host shape reached private values'))
    with pytest.raises(r.Refusal):recovery.create_old('memory',record,{'stage':'stopped','created':{},'retired':[]})
    assert events==[('image','inspect',r.RUNTIME)]

def test_three_representative_saved_roles_preserve_process_mount_and_runtime_identity():
    entry=['/bin/sh','-c','exec "$@"','owned-argv0'];network='owned-factory'
    bind_rw={'Type':'bind','Source':'/owned/source-rw','Destination':'/state','Mode':'rw','RW':True,'Propagation':'rprivate'}
    bind_ro={'Type':'bind','Source':'/owned/source-ro','Destination':'/fixture','Mode':'ro','RW':False,'Propagation':'rprivate'}
    volume_rw=named_volume();volume_ro=dict(named_volume('ro',False),Name='owned-volume-ro',Source='/var/lib/docker/volumes/owned-volume-ro/_data',Destination='/owned-ro')
    shapes={
        'memory':{'user':'','workdir':'/app','command':['python','-m','owned.memory'],'mounts':[],'ports':{'8005/tcp':[{'HostIp':'127.0.0.1','HostPort':'28005'}]}},
        'relay':{'user':'','workdir':'/app','command':['owned-stream','run'],'mounts':[bind_rw,bind_ro],'ports':{}},
        'coordinator':{'user':'forge','workdir':'/home/forge','command':['--config','/owned/config','serve'],'mounts':[bind_rw,bind_ro,volume_rw,volume_ro],'ports':{}},
    }
    for role,shape in shapes.items():
        identity={'container_id':'a'*64,'name':'/owned-'+role,'hostname':'owned-'+role+'-host','user':shape['user'],'working_dir':shape['workdir'],'entrypoint':entry,'command':shape['command']}
        saved={'image_id':'sha256:'+'1'*64,'repo_tags':['owned:'+role],'mounts':json.loads(json.dumps(shape['mounts'])),'networks':{network:{'Aliases':['owned-'+role]}},'port_bindings':shape['ports'],'restart_policy':{'Name':'unless-stopped','MaximumRetryCount':0},'network_mode':network,'env_names':['OWNED_ONE','OWNED_TWO'],'service_identity':identity}
        recreated=json.loads(json.dumps(saved));recreated['service_identity']['container_id']='b'*64
        recreated['service_identity']['entrypoint']=[entry[0]];recreated['service_identity']['command']=entry[1:]+shape['command']
        for mount in recreated['mounts']:
            if mount['Type']=='bind':mount['Mode']=''
            else:assert b.volume_argument(mount).endswith(':'+mount['Mode'])
        seal=q.stable_runtime(saved)
        assert seal==q.stable_runtime(recreated)
        assert seal['service_identity']=={'name':'/owned-'+role,'hostname':'owned-'+role+'-host','user':shape['user'],'working_dir':shape['workdir'],'effective_argv':entry+shape['command']}
        assert seal['image_id']==saved['image_id'] and seal['repo_tags']==saved['repo_tags'] and seal['port_bindings']==shape['ports']
        assert seal['restart_policy']==saved['restart_policy'] and seal['network_mode']==network and seal['env_names']==saved['env_names']
        changed=json.loads(json.dumps(recreated));changed['service_identity']['command'][-1]='changed';assert q.stable_runtime(saved)!=q.stable_runtime(changed)
        if saved['mounts']:
            changed=json.loads(json.dumps(recreated));changed['mounts'][0]['Source']='/changed';assert q.stable_runtime(saved)!=q.stable_runtime(changed)
    assert [x['Mode'] for x in q.stable_runtime({'service_identity':{'container_id':'a'*64},'networks':{},'image_id':'x','repo_tags':[],'mounts':[volume_rw,volume_ro],'port_bindings':{},'restart_policy':{},'network_mode':'owned','env_names':[]})['mounts']]==['rw','ro']


@pytest.mark.parametrize('mode,rw,expected',[('rw',True,'owned-volume:/owned:rw'),('ro',False,'owned-volume:/owned:ro')])
def test_named_volume_argument_preserves_plain_saved_mode(mode,rw,expected):
    assert b.volume_argument(named_volume(mode,rw))==expected

@pytest.mark.parametrize('mode,rw',[('rw',True),('ro',False)])
@pytest.mark.parametrize('destination',['/data/.hidden','/data/nested/sub-name_1','/data/café','/data/😀'])
def test_named_volume_argument_preserves_canonical_hidden_and_nested_paths(mode,rw,destination):
    mount=named_volume(mode,rw);mount['Destination']=destination
    assert b.volume_argument(mount)=='owned-volume:'+destination+':'+mode

@pytest.mark.parametrize('escaped',[r'"/data/\ud800"',r'"/data/\udc00"',r'"/data/\udc80"'])
@pytest.mark.parametrize('mode,rw',[('rw',True),('ro',False)])
@pytest.mark.parametrize('journaled',[False,True])
def test_named_volume_unencodable_destination_refuses_before_old_lookup(estate,monkeypatch,escaped,mode,rw,journaled):
    recovery=b.Recovery(estate.args);mount=named_volume(mode,rw);mount['Destination']=json.loads(escaped);record=volume_runtime(mount);events=[]
    journal={'stage':'stopped','created':{'coordinator':'b'*64} if journaled else {},'retired':[]};saved=json.loads(json.dumps(journal))
    def docker(c,*args,**kwargs):
        events.append(args)
        if args[:2]==('image','inspect'):return SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}]))
        if args[:2]==('network','inspect'):return SimpleNamespace(stdout='[]')
        pytest.fail('encoding preflight reached old lookup or mutation: '+repr(args))
    monkeypatch.setattr(r,'docker',docker);monkeypatch.setattr(recovery,'old_values',lambda *a:pytest.fail('read values after bad volume preflight'))
    with pytest.raises(r.Refusal):recovery.create_old('coordinator',record,journal)
    assert journal==saved
    assert all(args[0] not in ('ps','inspect','rm','create','start') for args in events)

@pytest.mark.parametrize('change',[
    {'Mode':'z'},
    {'Mode':'Z'},
    {'Mode':'rw,z'},
    {'Mode':''},
    {'Mode':'rw','RW':False},
    {'Mode':'ro','RW':True},
    {'Propagation':'rprivate'},
    {'Consistency':'cached'},
    {'Name':'bad:name'},
    {'Destination':'relative'},
    {'Destination':'/'},
    {'Destination':'//'},
    {'Destination':'/./'},
    {'Destination':'/data/..'},
    {'Destination':'/../'},
    {'Destination':'/data/'},
    {'Destination':'/data//sub'},
    {'Destination':'/data/./sub'},
    {'Destination':'/data/../sub'},
    {'Destination':'/bad\0target'},
    {'Destination':'/bad:target'},
])
def test_named_volume_argument_refuses_changed_or_unrepresentable_flags(change):
    mount=named_volume();mount.update(change)
    with pytest.raises(r.Refusal):b.volume_argument(mount)

@pytest.mark.parametrize('mode,rw',[('rw',True),('ro',False)])
def test_create_old_uses_explicit_volume_syntax_and_preserves_mode(estate,monkeypatch,mode,rw):
    recovery=b.Recovery(estate.args);created='b'*64;record=volume_runtime(named_volume(mode,rw));creates=[]
    recovery.doc={'format_version':1,'binding':recovery.binding,'rollback':{'stage':'stopped','created':{},'retired':[]}};journal=recovery.doc['rollback']
    monkeypatch.setattr(recovery,'validate_runtime',lambda item:None);monkeypatch.setattr(recovery,'old_values',lambda *a:{})
    def docker(c,*args,**kwargs):
        if args[0]=='ps':return SimpleNamespace(stdout='')
        if args[0]=='create':creates.append(args);return SimpleNamespace(stdout=created+'\n')
        raise AssertionError(args)
    monkeypatch.setattr(r,'docker',docker)
    actual={'Id':created,'Name':'/owned-coordinator','Image':r.RUNTIME,'State':{'Running':False},'NetworkSettings':{'Networks':{'owned-primary':{}}}}
    monkeypatch.setattr(r,'inspect',lambda *a:actual);monkeypatch.setattr(r,'previous_runtime',lambda *a:record)
    assert recovery.create_old('coordinator',record,journal)==created
    command=creates[0]
    assert command[command.index('--volume')+1]=='owned-volume:/owned:'+mode and '--mount' not in command

@pytest.mark.parametrize('change',[{'Mode':'z'},{'Driver':'other'},{'Source':'/different'},{'Destination':'/'},{'Destination':'//'},{'Destination':'/./'},{'Destination':'/data/..'},{'Destination':'/../'},{'Destination':'/data/'},{'Destination':'/data//sub'},{'Destination':'/data/./sub'},{'Destination':'/data/../sub'},{'Destination':'/bad\0target'}])
def test_named_volume_preflight_refuses_before_old_container_deletion(estate,monkeypatch,change):
    recovery=b.Recovery(estate.args);mount=named_volume();mount.update(change);record=volume_runtime(mount);events=[]
    def docker(c,*args,**kwargs):
        events.append(args)
        if args[:2]==('image','inspect'):return SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}]))
        if args[:2]==('network','inspect'):return SimpleNamespace(stdout='[]')
        if args[:2]==('volume','inspect'):return SimpleNamespace(stdout=json.dumps([{'Name':'owned-volume','Driver':'local','Mountpoint':'/var/lib/docker/volumes/owned-volume/_data'}]))
        pytest.fail('preflight reached container mutation: '+repr(args))
    monkeypatch.setattr(r,'docker',docker);monkeypatch.setattr(recovery,'old_values',lambda *a:pytest.fail('read values after bad volume preflight'))
    with pytest.raises(r.Refusal):recovery.create_old('coordinator',record,{'stage':'stopped','created':{},'retired':[]})
    assert all(args[0] not in ('ps','rm','create','start') for args in events)


def test_post_create_failure_journals_id_and_retry_reuses_only_that_container(estate,monkeypatch):
    recovery=b.Recovery(estate.args);created='b'*64;old='a'*64;network='owned-primary'
    record={'image_id':r.RUNTIME,'repo_tags':[],'mounts':[],'networks':{network:{'Aliases':['owned-memory'],'Links':None,'IPAMConfig':None,'DriverOpts':None}},'port_bindings':{},'restart_policy':{'Name':'no','MaximumRetryCount':0},'network_mode':network,'env_names':[],'service_identity':{'name':'/owned-memory','container_id':old,'hostname':'owned-host','user':'','working_dir':'/app','entrypoint':['/bin/sh','-c','exec "$@"','argv0'],'command':['python','-m','fleet_memory.mcp']}}
    recovery.doc={'format_version':1,'binding':recovery.binding,'rollback':{'stage':'stopped','created':{},'retired':[]}}
    journal=recovery.doc['rollback'];creates=[];inspection={'fail':True}
    monkeypatch.setattr(recovery,'validate_runtime',lambda item:None);monkeypatch.setattr(recovery,'old_values',lambda *a:{})
    def docker(c,*args,**kwargs):
        if args[0]=='ps':return SimpleNamespace(stdout='')
        if args[0]=='create':creates.append(args);return SimpleNamespace(stdout=created+'\n')
        raise AssertionError(args)
    monkeypatch.setattr(r,'docker',docker)
    actual={'Id':created,'Name':'/owned-memory','Image':r.RUNTIME,'State':{'Running':False},'NetworkSettings':{'Networks':{network:{}}}}
    def inspect(c,identifier):
        assert identifier==created
        if inspection['fail']:raise r.Refusal('owned post-create inspection failed')
        return actual
    monkeypatch.setattr(r,'inspect',inspect);monkeypatch.setattr(r,'previous_runtime',lambda *a:record)
    with pytest.raises(r.Refusal,match='post-create inspection failed'):recovery.create_old('memory',record,journal)
    assert journal['created']=={'memory':created} and r.read_json(recovery.receipt)['rollback']['created']=={'memory':created}
    inspection['fail']=False
    assert recovery.create_old('memory',record,journal)==created
    assert len(creates)==1

def test_partial_retry_never_adopts_container_with_another_identity(estate,monkeypatch):
    recovery=b.Recovery(estate.args);created='b'*64;old='a'*64;network='owned-primary'
    record={'image_id':r.RUNTIME,'repo_tags':[],'mounts':[],'networks':{network:{'Aliases':[],'Links':None,'IPAMConfig':None,'DriverOpts':None}},'port_bindings':{},'restart_policy':{},'network_mode':network,'env_names':[],'service_identity':{'name':'/owned-memory','container_id':old,'hostname':'owned-host','user':'','working_dir':'/app','entrypoint':['python'],'command':['-m','fleet_memory.mcp']}}
    journal={'stage':'recreating','created':{'memory':created},'retired':[]};recovery.doc={'format_version':1,'binding':recovery.binding,'rollback':journal}
    monkeypatch.setattr(recovery,'validate_runtime',lambda item:None);monkeypatch.setattr(recovery,'old_values',lambda *a:{})
    monkeypatch.setattr(r,'docker',lambda *a,**k:pytest.fail('journaled retry created or removed a container'))
    monkeypatch.setattr(r,'inspect',lambda *a:{'Id':'c'*64,'Name':'/owned-memory','Image':r.RUNTIME,'State':{'Running':False},'NetworkSettings':{'Networks':{network:{}}}})
    with pytest.raises(r.Refusal,match='absent, foreign'):
        recovery.create_old('memory',record,journal)

@pytest.mark.parametrize('table,before,after',[
 ('publication_records',{'build_id':'b','result':'merged into the remote and running','j_commit':'old'},{'build_id':'b','result':'merged into the remote and running','j_commit':'new'}),
 ('deployment_targets',{'target':'low','counter':2,'holder_build':None},{'target':'low','counter':3,'holder_build':None}),
 ('planning_runs',{'correlation_id':'p','state':'COMPLETE','completed_at':'old'},{'correlation_id':'p','state':'COMPLETE','completed_at':'new'}),
])
def test_before_refuses_equal_summary_changed_work(estate,monkeypatch,table,before,after):
    recovery=b.Recovery(estate.args);recovery.phase='prepared';base={table:{'status':'observed','count':1,'rows':[before]}};current={table:{'status':'observed','count':1,'rows':[after]}}
    recovery.doc={'old_runtimes':{},'binding':recovery.binding,'format_version':1}
    monkeypatch.setattr(recovery,'markers',lambda:None);monkeypatch.setattr(recovery,'record',lambda:recovery.doc);monkeypatch.setattr(recovery,'close',lambda:None);monkeypatch.setattr(recovery,'settle',lambda:None);monkeypatch.setattr(recovery,'final',lambda:None);monkeypatch.setattr(recovery,'save',lambda:None);monkeypatch.setattr(recovery,'current_state',lambda:{'work_state':current,'logical_sha256':'not-the-gate'})
    recovery.doc['old_runtimes']['coordinator']={}
    monkeypatch.setattr(q,'stable_runtime',lambda x:{});monkeypatch.setattr(recovery,'validate_runtime',lambda x:None);monkeypatch.setattr(recovery,'old_values',lambda *x:{})
    monkeypatch.setattr(r,'verify_snapshot',lambda x:{'work_state':base});monkeypatch.setattr(r,'read_json',lambda p:{});monkeypatch.setattr(r,'validate_receipt',lambda *x:None);monkeypatch.setattr(r,'verify_derivation',lambda *x:None);monkeypatch.setattr(r,'ledger_state',lambda *a,**k:{'work_state':base});monkeypatch.setattr(recovery,'remove_current',lambda:pytest.fail('removed graph before drift refusal'))
    with pytest.raises(r.Refusal,match='changed work'):recovery.before()

def test_current_watch_stops_before_producers(estate,monkeypatch):
    estate.phase='prepared';estate.doc={'format_version':1,'binding':estate.binding,'stage':'resumed'};estate.save();events=[]
    monkeypatch.setattr(estate,'markers',lambda:{'resumed':True});monkeypatch.setattr(estate,'watch_closed',lambda **kw:events.append(('watch',kw)));monkeypatch.setattr(estate,'stop_current',lambda services:events.append(('producers',services)))
    estate.close()
    assert events==[('watch',{'stop':True}),('producers',q.PRODUCERS)]

@pytest.mark.parametrize('mode',['close','settle','final','resume','reopen','before','after'])
def test_every_mode_plan_preserves_tree(estate,monkeypatch,mode):
    before=r.manifest(estate.receipt.parent);monkeypatch.setattr(estate,'helper',lambda *a,**k:pytest.fail('plan helper'));monkeypatch.setattr(r,'run',lambda *a,**k:pytest.fail('plan command'))
    assert estate.plan(mode)['mode']==mode
    assert before==r.manifest(estate.receipt.parent)

def test_h6_failure_never_launches_candidate_and_writes_stopped_record(estate,monkeypatch):
    recovery=b.Recovery(estate.args);recovery.phase='prepared';recovery.args.candidate_image=r.RUNTIME;recovery.args.candidate_env_file=recovery.args.env_file;events=[]
    monkeypatch.setattr(recovery,'markers',lambda:{'fixture':'resumed'});monkeypatch.setattr(recovery,'record',lambda:None)
    for name in ('close','settle','final','remove_current'):monkeypatch.setattr(recovery,name,lambda:None)
    monkeypatch.setattr(recovery,'planning',lambda enabled:events.append(('planning',enabled)))
    def h6(*a):raise r.Refusal('candidate refuses half-done publication')
    monkeypatch.setattr(recovery,'h6',h6)
    def docker(c,*args,**kw):
        events.append(args);assert 'up' not in args
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(r,'docker',docker)
    with pytest.raises(r.Refusal,match='half-done publication'):recovery.after()
    receipt=r.read_json(recovery.snapshot/'rollback-reconciliation.json')
    assert receipt['status']=='stopped-incompatible-or-unknown'
    assert receipt['volumes_kept']==recovery.c['volumes']
    assert events[0]==('planning',False)

def test_after_only_explicit_candidate_is_allowed_alongside_recorded_image(estate):
    recovery=b.Recovery(estate.args);candidate='sha256:'+'1'*64
    assert recovery.allowed_images('coordinator',r.RUNTIME)=={r.RUNTIME}
    recovery._after_candidate=candidate
    assert recovery.allowed_images('coordinator',r.RUNTIME)=={r.RUNTIME,candidate}
    assert recovery.allowed_images('memory',r.RUNTIME)=={r.RUNTIME}

@pytest.mark.parametrize('payload',['null','false','[]','{"broken"'])
def test_present_invalid_marker_never_reopens(estate,monkeypatch,payload):
    marker=estate.snapshot/'resumed.json';marker.write_text(payload);marker.chmod(0o600)
    monkeypatch.setattr(r,'inspect',lambda *a:pytest.fail('old runtime accessed'))
    monkeypatch.setattr(estate,'systemctl',lambda *a:pytest.fail('producer mutation'))
    with pytest.raises(r.Refusal):estate.reopen()

@pytest.mark.parametrize('kind',['hardlink','symlink','public','null'])
def test_marker_reader_rejects_unsafe_ledger_or_host_file(tmp_path,kind):
    p=tmp_path/'marker';p.write_text('{}');p.chmod(0o600)
    if kind=='hardlink':os.link(p,tmp_path/'alias')
    if kind=='symlink':p.unlink();p.symlink_to(tmp_path/'missing')
    if kind=='public':p.chmod(0o644)
    if kind=='null':p.write_text('null')
    with pytest.raises((ValueError,OSError)):q.marker_file(p)

def test_marker_true_absence_and_valid_private_object(tmp_path):
    p=tmp_path/'marker';assert q.marker_file(p)=={'present':False}
    p.write_text('{"valid":"object"}');p.chmod(0o600)
    assert q.marker_file(p)=={'present':True,'value':{'valid':'object'}}

def test_stale_restored_container_refuses_and_records_without_mutation(estate,monkeypatch):
    e=b.Recovery(estate.args);e.doc={'format_version':1,'binding':e.binding,'restored':True,'restored_ids':{'coordinator':'new-c','memory':'new-m','relay':'new-r'},'old_runtimes':{role:{'service_identity':{'container_id':'old-'+role}} for role in ('coordinator','memory','relay')},'rollback':{'retired':list(r.VOLUME_ROLES),'created':{'coordinator':'new-c','memory':'new-m','relay':'new-r'}}};e.save()
    monkeypatch.setattr(e,'markers',lambda:None)
    def docker(c,*a,**kw):assert a==('volume','ls','--format','{{.Name}}');return SimpleNamespace(stdout='')
    monkeypatch.setattr(r,'docker',docker)
    monkeypatch.setattr(r,'inspect',lambda *a:(_ for _ in ()).throw(r.Refusal('missing saved container')))
    with pytest.raises(r.Refusal,match='reconciliation'):e.before()
    assert r.read_json(e.receipt)['restored_verification']['passed'] is False

def valid_h6():
    J='3'*40;target='fixture-target'
    return {'format_version':1,'outcome':'handled-both','candidate_image_id':r.RUNTIME,'pristine_sha256':'a'*64,'working_pre_fixture_sha256':'a'*64,'configuration_sha256':'b'*64,'schema_version':r.LEDGER_SCHEMA_VERSION,'existing_rows_unchanged':True,'real_client_modules':[],'cleanup':'no owned worker remains','worker_group_empty':True,'worker_thread_stopped':True,'init':{'probe_pid':7,'pid1':'/sbin/docker-init'},'columns':{'builds':['build_id','status','mode','start_commit','target_branch'],'publication_records':['build_id','g_commit','j_commit','checked_json','turn','lines_json'],'deployment_targets':['target','counter','holder_build','holder_turn','running_commit']},'git':{'G':'1'*40,'tip':'2'*40,'J':J,'tree':'4'*40},'fixture_ids':{'build':'fixture-build','target':target},'publication':{'turn':4,'result':'published, deployment pending','g_commit':'1'*40,'j_commit':J,'checked':{'identity':J,'j_commit':J,'j_tree':'4'*40},'original_lines_preserved':True,'callbacks':{'publisher':0,'guardkit':0,'deploy':0,'stage_complete':1},'line_kinds':['done join','done merge-checks','done candidate-check','about to send','done send'],'before_sha256':'c'*64,'after_sha256':'d'*64,'send_result':{'found_by_looking':True,'published':True,'contains_j':True,'ran_on':J,'remote_now':J}},'deployment':{'N':41,'N_plus_1':42,'stale_fencing':{'renew':False,'record_running':False,'release':False},'stale_record_unchanged':True,'reconcile':{target:'occupied (adopted)'},'old_note':{'target':target,'counter':41,'highest_counter':41,'group':4,'phase':'running','build':'old'},'final_note':{'target':target,'highest_counter':42,'group':0,'counter':0,'phase':'','highest_build':'new'},'old_answers':[{'accepted':False,'word':'the-deploy-command-was-stopped-by-a-takeover'}],'successor':{'accepted':True,'exit_code':0,'word':'the-deploy-command-ran','output_tail':'DEPLOYED_IDENTITY='+J}}}

@pytest.mark.parametrize('field',['publication','deployment','cleanup','worker_group_empty','existing_rows_unchanged','configuration_sha256','columns','git','init'])
def test_h6_missing_required_behavior_never_authorizes(field):
    proof=valid_h6();del proof[field]
    with pytest.raises(r.Refusal):b.validate_h6(proof,r.RUNTIME,'a'*64,'b'*64)

@pytest.mark.parametrize('case',['replay','successor','counter','fencing','cleanup','rows','configuration','probe-was-pid1'])
def test_h6_contradictory_behavior_never_authorizes(case):
    p=valid_h6()
    if case=='replay':p['publication']['callbacks']['publisher']=2
    if case=='successor':p['deployment']['successor']['accepted']=False
    if case=='counter':p['deployment']['N_plus_1']=41
    if case=='fencing':p['deployment']['stale_fencing']['renew']=True
    if case=='cleanup':p['worker_group_empty']=False
    if case=='rows':p['existing_rows_unchanged']=False
    if case=='configuration':p['configuration_sha256']='c'*64
    if case=='probe-was-pid1':p['init']['probe_pid']=1
    with pytest.raises(r.Refusal):b.validate_h6(p,r.RUNTIME,'a'*64,'b'*64)

def test_h6_complete_consistent_behavior_passes():
    proof=valid_h6();assert b.validate_h6(proof,r.RUNTIME,'a'*64,'b'*64)==proof

def test_real_exit42_diagnostic_is_private_and_sanitized(estate):
    events=[];estate.private_values=['owned-private-value']
    with pytest.raises(r.Refusal):estate.captured([sys.executable,'-c',"import sys;print('item 9 callback refused; owned-private-value',file=sys.stderr);sys.exit(42)"],'estate-check services',events)
    assert events[0]['exit']==42 and 'item 9 callback refused' in events[0]['stderr']
    assert 'owned-private-value' not in json.dumps(events)
    output=estate.snapshot/'diagnostic.json';r.atomic_json(output,events)
    assert output.stat().st_mode&0o777==0o600

def local_lease_fs(monkeypatch):
    """Keep the real tmpfs lease syscalls; substitute only its FS-type label."""
    import ctypes
    actual=ctypes.CDLL
    class Local:
        def __init__(self,*args,**kwargs):self.lib=actual(*args,**kwargs)
        def fstatfs(self,fd,item):
            answer=self.lib.fstatfs(fd,item);item._obj.f_type=0xEF53;return answer
    monkeypatch.setattr(ctypes,'CDLL',Local)

def lease_root(tmp_path,mode=0o644,sidecars=True):
    root=tmp_path/'lease-state';root.mkdir();root.chmod(0o755)
    files=('forge.db','forge.db-wal','forge.db-shm') if sidecars else ('forge.db',)
    for name in files:
        item=root/name;item.write_bytes((name*512).encode()[:4096]);item.chmod(mode)
    return root

@pytest.mark.parametrize('mode',[0o600,0o644])
@pytest.mark.parametrize('sidecars',[False,True])
def test_exact_lease_algorithm_accepts_owned_modes_and_optional_sidecars(tmp_path,monkeypatch,mode,sidecars):
    local_lease_fs(monkeypatch);proof=q.ledger_lease_proof(lease_root(tmp_path,mode,sidecars))
    assert proof['complete'] and proof['cleanup_complete'] and proof['kind']=='clean'
    assert sorted(proof['files'])==(['forge.db','forge.db-shm','forge.db-wal'] if sidecars else ['forge.db'])

@pytest.mark.parametrize('opened',['forge.db','forge.db-wal','forge.db-shm'])
@pytest.mark.parametrize('mode',['rb','r+b'])
def test_lease_algorithm_refuses_each_readonly_or_writable_holder(tmp_path,monkeypatch,opened,mode):
    local_lease_fs(monkeypatch);root=lease_root(tmp_path)
    with (root/opened).open(mode):
        proof=q.ledger_lease_proof(root)
        assert not proof['complete'] and proof['kind']=='holder' and proof['errno']==11
    assert q.ledger_lease_proof(root)['complete']

def test_lease_algorithm_catches_idle_alias_holder(tmp_path,monkeypatch):
    local_lease_fs(monkeypatch);root=lease_root(tmp_path);alias=tmp_path/'alias';alias.symlink_to(root,target_is_directory=True)
    with (alias/'forge.db').open('r+b'):
        proof=q.ledger_lease_proof(root)
        assert not proof['complete'] and proof['kind']=='holder'

@pytest.mark.parametrize('access',["read","copy","write"])
def test_lease_algorithm_catches_mapping_after_original_fd_closed(tmp_path,monkeypatch,access):
    import mmap
    local_lease_fs(monkeypatch);root=lease_root(tmp_path);flags={'read':mmap.ACCESS_READ,'copy':mmap.ACCESS_COPY,'write':mmap.ACCESS_WRITE}
    fd=os.open(root/'forge.db',os.O_RDONLY if access!='write' else os.O_RDWR)
    mapped=mmap.mmap(fd,0,access=flags[access],trackfd=False);os.close(fd)
    try:
        proof=q.ledger_lease_proof(root)
        assert not proof['complete'] and proof['kind']=='holder'
    finally:mapped.close()
    assert q.ledger_lease_proof(root)['complete']

@pytest.mark.parametrize('unsafe_mode',[0o640,0o666])
def test_lease_algorithm_refuses_unaccepted_file_mode(tmp_path,monkeypatch,unsafe_mode):
    local_lease_fs(monkeypatch);root=lease_root(tmp_path);(root/'forge.db').chmod(unsafe_mode)
    proof=q.ledger_lease_proof(root)
    assert not proof['complete'] and proof['kind']=='identity'

def test_lease_cleanup_failure_is_unknown_not_success(tmp_path,monkeypatch):
    import fcntl
    local_lease_fs(monkeypatch);root=lease_root(tmp_path,sidecars=False);actual=fcntl.fcntl
    def fail_unlock(fd,operation,arg=0):
        if operation==fcntl.F_SETLEASE and arg==fcntl.F_UNLCK:raise OSError(5,'owned cleanup failure')
        return actual(fd,operation,arg)
    monkeypatch.setattr(fcntl,'fcntl',fail_unlock)
    proof=q.ledger_lease_proof(root)
    assert not proof['complete'] and not proof['cleanup_complete'] and proof['kind']=='unknown'

def test_lease_file_on_another_filesystem_is_identity_failure(tmp_path,monkeypatch):
    import ctypes
    root=lease_root(tmp_path,sidecars=False);actual=ctypes.CDLL;calls=0
    class Mixed:
        def __init__(self,*args,**kwargs):self.lib=actual(*args,**kwargs)
        def fstatfs(self,fd,item):
            nonlocal calls
            answer=self.lib.fstatfs(fd,item);calls+=1;item._obj.f_type=0xEF53 if calls==1 else 0x58465342;return answer
    monkeypatch.setattr(ctypes,'CDLL',Mixed)
    proof=q.ledger_lease_proof(root)
    assert not proof['complete'] and proof['kind']=='identity' and proof['cleanup_complete']

def test_lease_permission_error_is_unknown(tmp_path,monkeypatch):
    import errno
    local_lease_fs(monkeypatch);root=lease_root(tmp_path,sidecars=False);actual=os.open
    def denied(path,*args,**kwargs):
        if path=='forge.db':raise PermissionError(errno.EACCES,'owned permission fixture')
        return actual(path,*args,**kwargs)
    monkeypatch.setattr(os,'open',denied)
    proof=q.ledger_lease_proof(root)
    assert not proof['complete'] and proof['kind']=='unknown' and proof['errno']==errno.EACCES

def test_lease_presence_race_is_identity_failure(tmp_path,monkeypatch):
    local_lease_fs(monkeypatch);root=lease_root(tmp_path);actual=os.stat;first=True
    def appearing(path,*args,**kwargs):
        nonlocal first
        if path=='forge.db-shm' and kwargs.get('dir_fd') is not None and first:
            first=False;raise FileNotFoundError(path)
        return actual(path,*args,**kwargs)
    monkeypatch.setattr(os,'stat',appearing)
    proof=q.ledger_lease_proof(root)
    assert not proof['complete'] and proof['kind']=='identity' and proof['cleanup_complete']

def test_lease_break_state_is_never_accepted(tmp_path,monkeypatch):
    import fcntl
    local_lease_fs(monkeypatch);root=lease_root(tmp_path,sidecars=False);actual=fcntl.fcntl
    def broken(fd,operation,arg=0):
        answer=actual(fd,operation,arg)
        return fcntl.F_UNLCK if operation==fcntl.F_GETLEASE else answer
    monkeypatch.setattr(fcntl,'fcntl',broken)
    proof=q.ledger_lease_proof(root)
    assert not proof['complete'] and proof['kind']=='break' and proof['cleanup_complete']

@pytest.mark.parametrize('proof',[
    {'format_version':1,'complete':False,'cleanup_complete':True,'kind':'unknown','reason':'permission'},
    {'format_version':1,'complete':False,'cleanup_complete':True,'kind':'holder','reason':'open holder'},
    {'format_version':1,'complete':False,'cleanup_complete':False,'kind':'unknown','reason':'cleanup'},
])
def test_public_lease_gate_refuses_unknown_holder_or_cleanup_failure(estate,monkeypatch,proof):
    monkeypatch.setattr(estate,'model',lambda:{})
    monkeypatch.setattr(r,'volume_identity',lambda *a:{})
    def docker(c,*args,**kw):
        assert args[0:4]==('run','--rm','--pull','never')
        assert '--pid' not in args and '--cap-add' not in args and '--privileged' not in args
        assert args[args.index('--cap-drop')+1]=='ALL'
        assert args[args.index('--security-opt')+1]=='no-new-privileges:true'
        assert args[args.index('--user')+1]=='1000:1000'
        assert args[args.index('--network')+1]=='none' and '--read-only' in args
        assert args[args.index('--mount')+1].endswith('dst=/state,readonly')
        return SimpleNamespace(stdout=json.dumps(proof))
    monkeypatch.setattr(r,'docker',docker)
    with pytest.raises(r.Refusal):estate.current_holders()

def test_public_lease_gate_accepts_complete_proof(estate,monkeypatch):
    proof={'format_version':1,'complete':True,'cleanup_complete':True,'kind':'clean','files':{'forge.db':[1,2,3,4,0o644,1000,1000]},'filesystem':{'name':'ext','magic':'0xef53'}}
    monkeypatch.setattr(estate,'model',lambda:{});monkeypatch.setattr(r,'volume_identity',lambda *a:{})
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=json.dumps(proof)))
    assert estate.current_holders()==proof

@pytest.mark.parametrize('case',['valid','missing','contradictory','wrong-configuration','current-copy-changed'])
def test_actual_h6_result_ingestion_tamper_boundary(estate,tmp_path,monkeypatch,case):
    import hashlib
    e=b.Recovery(estate.args);directory=tmp_path/('probe-'+case);directory.mkdir();pristine=directory/'current.db';pristine.write_bytes(b'owned envelope boundary bytes');digest=r.sha256(pristine);state={}
    monkeypatch.setattr(e,'current_copy',lambda path:(pristine,{'sha256':digest}));monkeypatch.setattr(e,'model',lambda:{})
    def docker(c,*args,**kw):
        return SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}]) if args[:2]==('image','inspect') else '',stderr='',returncode=0)
    monkeypatch.setattr(r,'docker',docker);monkeypatch.setattr(r,'inspect',lambda *a:{'Image':r.RUNTIME,'HostConfig':{'Init':True},'State':{'ExitCode':0}})
    def captured(argv,stage,events,**kwargs):
        if stage=='h6-create':
            assert '--init' in argv[:argv.index(r.RUNTIME)],argv
            state['configuration']=argv[-1];return SimpleNamespace(stdout='owned-candidate-id',returncode=0)
        proof=valid_h6();proof.update(pristine_sha256=digest,working_pre_fixture_sha256=digest,configuration_sha256=state['configuration'])
        if case=='missing':del proof['deployment']
        if case=='contradictory':proof['publication']['callbacks']['publisher']=2
        if case=='wrong-configuration':proof['configuration_sha256']='f'*64
        if case=='current-copy-changed':pristine.write_bytes(b'changed')
        (directory/'probe/h6-result.json').write_text(json.dumps(proof))
        return SimpleNamespace(stdout='',returncode=0)
    monkeypatch.setattr(e,'captured',captured)
    if case=='valid':assert e.h6(r.RUNTIME,directory)['outcome']=='handled-both'
    else:
        with pytest.raises(r.Refusal):e.h6(r.RUNTIME,directory)
        diagnostic=r.read_json(directory/'h6-diagnostic.json')
        assert diagnostic['status']=='refused' and diagnostic['cleanup']['container_removed']


@pytest.mark.parametrize('init',[True,False])
def test_h6_container_runs_with_an_init_and_the_diagnostic_says_so(estate,tmp_path,monkeypatch,init):
    """The candidate's own executor may still count uncollected zombies; only an init at PID 1 keeps the takeover confirmable."""
    e=b.Recovery(estate.args);directory=tmp_path/'probe-init';directory.mkdir();pristine=directory/'current.db';pristine.write_bytes(b'owned init bytes');digest=r.sha256(pristine);starts=[];state={}
    monkeypatch.setattr(e,'current_copy',lambda path:(pristine,{'sha256':digest}));monkeypatch.setattr(e,'model',lambda:{})
    monkeypatch.setattr(r,'docker',lambda c,*args,**kw:SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}]) if args[:2]==('image','inspect') else '',stderr='',returncode=0))
    monkeypatch.setattr(r,'inspect',lambda *a:{'Image':r.RUNTIME,'HostConfig':{'Init':init},'State':{'ExitCode':0}})
    def run(argv,**kwargs):
        argv=[str(x) for x in argv];operation=argv[3]
        if operation=='create':
            options=argv[4:argv.index(r.RUNTIME)]
            if init:assert '--init' in options,'H6 create lost --init: '+repr(options)
            return SimpleNamespace(stdout='owned-candidate-id\n',stderr='',returncode=0)
        assert operation=='start';starts.append(argv)
        proof=valid_h6();proof.update(pristine_sha256=digest,working_pre_fixture_sha256=digest,configuration_sha256=state['configuration'])
        (directory/'probe/h6-result.json').write_text(json.dumps(proof));return SimpleNamespace(stdout='',stderr='',returncode=0)
    original=e.captured
    def captured(argv,stage,events,**kwargs):
        if stage=='h6-create':state['configuration']=str(argv[-1])
        return original(argv,stage,events,**kwargs)
    monkeypatch.setattr(e,'captured',captured);monkeypatch.setattr(r,'run',run)
    if init:assert e.h6(r.RUNTIME,directory)['init']['probe_pid']>1
    else:
        with pytest.raises(r.Refusal,match='no init'):e.h6(r.RUNTIME,directory)
        assert starts==[],'a container without an init was started'
    diagnostic=r.read_json(directory/'h6-diagnostic.json')
    create=next(event['command'] for event in diagnostic['commands'] if event['stage']=='h6-create')
    assert '--init' in create[:create.index(r.RUNTIME)] and diagnostic['init'] is init
    assert diagnostic['status']==('passed' if init else 'refused') and diagnostic['cleanup']['container_removed']


def public_marker_case(estate,monkeypatch,phase,shape,stage='settled'):
    """Actual CLI/constructor/closed/classifier, with external operations captured."""
    if phase=='prepared':
        estate.c['env_file']=estate.q['prepared_env_file']
        estate.c['sources']['settings']=estate.q['prepared_settings']
        r.atomic_json(estate.config_path,estate.c)
    estate.doc={'format_version':1,'binding':estate.binding,'stage':stage,'observations':[]};estate.save()
    host=estate.snapshot/'resumed.json';ledger=estate.receipt.parent/'current-ledger-marker'
    marker={'format_version':1,'project':estate.c['project'],'snapshot':str(estate.snapshot),'snapshot_sha256':'a'*64,'release_tag':estate.q['release_tag'],'image_id':r.RUNTIME,'actor':'entry-test','at':q.now(),'work_state':{},'close_receipt_sha256':r.sha256(estate.receipt)}
    r.atomic_json(estate.snapshot/'metadata.json',{'sha256':'a'*64})
    if shape!='absent':
        r.atomic_json(host,marker);r.atomic_json(ledger,marker)
        if shape=='host-null':r.atomic_json(host,None)
        elif shape=='ledger-null':r.atomic_json(ledger,None)
        elif shape=='malformed':host.write_text('{')
        elif shape=='host-half':ledger.unlink()
        elif shape=='ledger-half':host.unlink()
        elif shape.endswith('hardlink'):
            selected=host if shape.startswith('host') else ledger
            os.link(selected,selected.with_name(selected.name+'-alias'))
        elif shape.endswith('symlink'):
            selected=host if shape.startswith('host') else ledger
            target=selected.with_name(selected.name+'-target');selected.rename(target);selected.symlink_to(target)
    events=[];reads=[]
    def boundary(name):
        def stop(*args,**kwargs):events.append(name);raise r.Refusal('captured test boundary '+name)
        return stop
    def volume(self,role,code,args=(),**kwargs):
        assert role=='ledger' and not kwargs.get('write',False)
        return json.dumps(q.marker_file(ledger))
    monkeypatch.setattr(q.Estate,'volume_exists',lambda self:shape!='absent')
    monkeypatch.setattr(q.Estate,'volume',volume)
    monkeypatch.setattr(q.Estate,'unit_stopped',lambda self,name:reads.append('unit-stopped') or {})
    monkeypatch.setattr(q.Estate,'unit',lambda self,name:reads.append('unit') or {'UnitFileState':'masked'})
    def watch(self,**kwargs):
        if kwargs.get('stop'):boundary('watch-stop')()
        reads.append('watch-observe')
    monkeypatch.setattr(q.Estate,'watch_closed',watch)
    monkeypatch.setattr(q.Estate,'producers_stopped',lambda self:reads.append('producer-observe'))
    monkeypatch.setattr(q.Estate,'stop_current',boundary('current-writer-stop'))
    monkeypatch.setattr(q.Estate,'systemctl',boundary('legacy-unit-stop'))
    monkeypatch.setattr(q.Estate,'monitor',boundary('reader-observation'))
    monkeypatch.setattr(q.Estate,'prepared_settings',boundary('prepared-settings'))
    monkeypatch.setattr(q.Estate,'current_holders',lambda *a,**k:pytest.fail('holder proof must not execute before marker validation'))
    monkeypatch.setattr(r,'run',boundary('sandbox-stop'))
    monkeypatch.setattr(r,'inspect',boundary('old-container-access'))
    argv=['--config',str(estate.config_path),'--env-file',estate.c['env_file'],'--project',estate.c['project'],'--snapshot',str(estate.snapshot)]
    return argv,events,reads


@pytest.mark.parametrize('mode',['close','settle','final','resume','reopen'])
@pytest.mark.parametrize('phase',['original','prepared'])
@pytest.mark.parametrize('shape',['host-null','ledger-null','malformed','host-half','ledger-half','host-hardlink','ledger-hardlink','host-symlink','ledger-symlink'])
def test_public_mutation_entries_refuse_unsafe_marker_before_external_access(estate,monkeypatch,mode,phase,shape,capsys):
    argv,events,reads=public_marker_case(estate,monkeypatch,phase,shape)
    before=estate.receipt.read_bytes()
    assert q.main(argv+['--'+mode])==2
    assert events==[] and reads==[]
    assert estate.receipt.read_bytes()==before
    assert 'Refused:' in capsys.readouterr().err


@pytest.mark.parametrize('mode,phase,shape,next_boundary',[
    ('final','original','absent','sandbox-stop'),
    ('final','prepared','absent','current-writer-stop'),
    ('final','prepared','valid','current-writer-stop'),
    ('settle','original','absent','reader-observation'),
    ('settle','prepared','absent','reader-observation'),
    ('settle','prepared','valid','reader-observation'),
    ('close','original','absent','legacy-unit-stop'),
    ('close','prepared','absent','watch-stop'),
    ('close','prepared','valid','watch-stop'),
    ('resume','prepared','absent','prepared-settings'),
    ('reopen','original','absent','old-container-access'),
])
def test_public_marker_authority_positive_reaches_intended_boundary(estate,monkeypatch,mode,phase,shape,next_boundary):
    argv,events,reads=public_marker_case(estate,monkeypatch,phase,shape)
    assert q.main(argv+['--'+mode])==2  # Deliberately stopped at the named external boundary.
    assert events==[next_boundary]


@pytest.mark.parametrize('mode',['close','settle','final','reopen'])
@pytest.mark.parametrize('stage',['closed','settled','final','resumed'])
def test_public_legacy_entries_refuse_actual_resumed_pair_despite_saved_stage(estate,monkeypatch,mode,stage):
    argv,events,reads=public_marker_case(estate,monkeypatch,'original','valid',stage)
    assert q.main(argv+['--'+mode])==2
    assert events==[] and reads==[]


@pytest.mark.parametrize('stage',['closed','settled','final','resumed'])
def test_public_resume_crash_after_pair_is_idempotent_without_old_access(estate,monkeypatch,stage,capsys):
    argv,events,reads=public_marker_case(estate,monkeypatch,'prepared','valid',stage)
    assert q.main(argv+['--resume'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['resumed'] is True and result['idempotent'] is True
    assert events==[] and reads==[]

def _terminal_planning_state(tmp_path, terminal):
    db=tmp_path/(terminal.value.lower()+'.db');connection=connect_writer(db);apply_at_boot(connection)
    store=SqlitePlanningRunStore(connection,target_terminal_enabled=True);correlation_id='terminal-'+terminal.value.lower()
    assert store.record_queued(correlation_id,'fixture-user','fixture-approver','fixture request','cli') is None
    assert store.transition(correlation_id,PlanningState.RUNNING,'fixture-worker') is None
    request_id=derive_request_id(build_id='plan-'+correlation_id,stage_label='Build approval',attempt_count=0)
    store.update_pending_approval_request_id(correlation_id,request_id)
    assert store.transition(correlation_id,PlanningState.PAUSED,'gate-check') is None
    if terminal not in {PlanningState.CANCELLED,PlanningState.TIMED_OUT}:
        assert store.transition(correlation_id,PlanningState.RUNNING,'approval-system') is None
    if terminal is PlanningState.BUILD_QUEUED:
        assert store.transition(correlation_id,PlanningState.FEATURE_SPEC,'fixture-worker') is None
        assert store.transition(correlation_id,PlanningState.FEATURE_PLAN,'fixture-worker') is None
    assert store.transition(correlation_id,terminal,'fixture-worker') is None
    row=dict(store.get_run(correlation_id));connection.close()
    assert row['pending_approval_request_id']==request_id
    assert row['completed_at'] is not None
    return r.ledger_state(db)


@pytest.mark.parametrize('terminal',[PlanningState.FAILED,PlanningState.CANCELLED,PlanningState.TIMED_OUT,PlanningState.PLANNED_HANDOFF,PlanningState.BUILD_QUEUED])
def test_completed_terminal_planning_history_retains_reference_without_active_work(tmp_path,terminal):
    state=_terminal_planning_state(tmp_path,terminal)
    assert q.work_problems(state)==[]


@pytest.mark.parametrize(('state','completed_at','request_id'),[
    ('PAUSED','2026-09-27T21:54:00+00:00','plan-fixture:Build%20approval:0'),
    ('FEATURE_PLAN','2026-09-27T21:54:00+00:00','plan-fixture:Build%20approval:0'),
    ('FUTURE_TERMINAL','2026-09-27T21:54:00+00:00','plan-fixture:Build%20approval:0'),
    ('FAILED',None,'plan-fixture:Build%20approval:0'),
    ('FAILED','','plan-fixture:Build%20approval:0'),
    ('FAILED','not-a-timestamp','plan-fixture:Build%20approval:0'),
    ('FAILED','2026-09-27T21:54:00','plan-fixture:Build%20approval:0'),
    ('FAILED','2026-09-27T22:54:00+01:00','plan-fixture:Build%20approval:0'),
    ('FAILED','2026-09-27T21:54:00+00:00','not-a-canonical-reference'),
    ('FAILED','2026-09-27T21:54:00+00:00',''),
    ('FAILED','2026-09-27T21:54:00+00:00','plan-%FF:product_docs:0'),
    ('FAILED','2026-09-27T21:54:00+00:00','plan-%C0%AF:product_docs:0'),
    ('FAILED','2026-09-27T21:54:00+00:00','plan-%ED%A0%80:product_docs:0'),
    ('FAILED','2026-09-27T21:54:00+00:00','plan-%41:product_docs:0'),
    ('FAILED','2026-09-27T21:54:00+00:00','plan%GG:stage:0'),
    ('FAILED','2026-09-27T21:54:00+00:00',17),
])
def test_planning_approval_uncertainty_remains_active(state,completed_at,request_id):
    observed={'work_state':{'planning_runs':{'status':'observed','rows':[{
        'correlation_id':'fixture','state':state,'completed_at':completed_at,
        'pending_approval_request_id':request_id,
    }]}}}
    assert q.work_problems(observed)==[{'table':'planning_runs','row':observed['work_state']['planning_runs']['rows'][0]}]


@pytest.mark.parametrize(('build_id','stage'),[
    ('plan-dot.name~suffix','product_docs'),
    ('plan-unicode-\u00e9','review . ~ : %'),
    ('plan-percent%FF','product_docs'),
    ('plan-control\x00','product_docs'),
])
def test_canonical_planning_approval_reference_variants_are_history(tmp_path,build_id,stage):
    state=_terminal_planning_state(tmp_path,PlanningState.FAILED)
    row=state['work_state']['planning_runs']['rows'][0]
    row['pending_approval_request_id']=derive_request_id(
        build_id=build_id,stage_label=stage,attempt_count=2)
    assert q.work_problems(state)==[]


@pytest.mark.parametrize('policy_case',['missing','changed','good'])
def test_actual_back_after_refuses_current_policy_before_up(estate,monkeypatch,policy_case):
    from .test_publisher_host_policy import load_helper, declared_model
    recovery=b.Recovery(estate.args);recovery.phase='prepared';candidate='sha256:'+'1'*64
    estate.args.candidate_image=candidate
    candidate_env=estate.receipt.parent/'candidate.env';candidate_env.write_text(Path(estate.args.env_file).read_text().replace(r.RUNTIME,candidate));estate.args.candidate_env_file=str(candidate_env)
    monkeypatch.setattr(recovery,'markers',lambda:{'fixture':'resumed'})
    for name in ('record','close','settle','final','remove_current'):monkeypatch.setattr(recovery,name,lambda:None)
    monkeypatch.setattr(recovery,'planning',lambda enabled:None);monkeypatch.setattr(recovery,'h6',lambda *a:{'passed':True})
    model=declared_model(load_helper(),estate.c['project'],settings=Path(estate.values['FORGE_PUBLISHER_SETTINGS_FILE']));model['services']['coordinator']['image']=candidate;model['services']['answer-service']={'image':candidate}
    calls=[]
    def docker(c,*args,**kw):
        calls.append(args)
        assert 'up' not in args or policy_case=='good','started after policy refusal'
        return SimpleNamespace(stdout=json.dumps(model) if 'config' in args else ('fixture' if 'info' in args else ''))
    def run(argv,**kw):
        if argv[:3]==['docker','context','inspect']:return SimpleNamespace(stdout=json.dumps([{'Endpoints':{'docker':{'Host':'unix:///var/run/docker.sock'}}}]))
        assert str(argv[2])=='/usr/local/libexec/forge-publisher-host-policy' and argv[3]=='verify'
        assert set(kw['env'])=={'PATH','DOCKER_HOST'} and '--daemon-id' in argv
        if policy_case=='good':return SimpleNamespace(returncode=0,stdout='VERIFIED daemon=fixture')
        raise r.Refusal('current kernel policy '+policy_case)
    monkeypatch.setattr(r,'docker',docker);monkeypatch.setattr(r,'run',run)
    monkeypatch.setenv('DOCKER_HOST','unix:///var/run/docker.sock')
    if policy_case=='good':
        assert recovery.after()['status']=='compatible-current-release-started-with-admissions-closed'
        assert any('up' in args for args in calls)
    else:
        with pytest.raises(r.Refusal,match='current kernel policy '+policy_case):recovery.after()
        assert r.read_json(recovery.snapshot/'rollback-reconciliation.json')['status']=='stopped-incompatible-or-unknown'


from .test_rollout_closed_door_receipts import probe


@pytest.mark.parametrize('policy_case',['missing','changed'])
def test_resume_consumes_real_old_receipt_and_stops_before_markers(estate,probe,monkeypatch,policy_case):
    run_probe,state,env_file,process_env=probe
    assert run_probe().returncode==0
    estate.phase='prepared';estate.doc={'format_version':1,'binding':estate.binding,'stage':'final'};estate.save()
    monkeypatch.setattr(estate,'markers',lambda:None);monkeypatch.setattr(estate,'record',lambda:estate.doc)
    monkeypatch.setattr(estate,'prepared_settings',lambda:None);monkeypatch.setattr(estate,'unit',lambda n:{})
    monkeypatch.setattr(estate,'watch_closed',lambda **kw:None);monkeypatch.setattr(estate,'producers_stopped',lambda:None)
    monkeypatch.setattr(estate,'monitor',lambda:q.reader_counts(monitor()));monkeypatch.setattr(r,'load_volumes',lambda *a,**kw:None)
    monkeypatch.setattr(estate,'volume',lambda *a,**kw:pytest.fail('ledger marker written after policy loss'))
    monkeypatch.setattr(estate,'planning',lambda *a,**kw:pytest.fail('planning enabled after policy loss'))
    monkeypatch.setattr(r,'docker',lambda *a,**kw:SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}])))
    actual_run=r.run
    def reader(argv,**kw):
        assert str(argv[0]).endswith('estate-check') and '--read-pre-resume' in argv
        # Run the same actual reader with the pre-existing fixture estate binding.
        command=[str(argv[0]),'--read-pre-resume','--env-file',str(env_file),'--project','codex-review']
        return actual_run(command,env=kw['env']|{key:process_env[key] for key in ('PATH','REVIEW_IMAGE','REVIEW_SETTINGS_FILE','PYTHONPATH','PYTHONDONTWRITEBYTECODE') if key in process_env}|{'POLICY_CASE':policy_case})
    monkeypatch.setattr(r,'run',reader)
    with pytest.raises(r.Refusal):estate.resume()
    assert not (estate.snapshot/'resumed.json').exists()
    assert (state/'pre-resume.json').exists()


@pytest.mark.parametrize('policy_case',['good','missing','changed'])
def test_real_resume_keeps_constructor_environment_through_reader_and_markers(estate,probe,monkeypatch,policy_case):
    run_probe,_,env_file,process_env=probe
    # Build a coherent prepared inventory using the actual constructor. Only
    # external commands are fake; resume passes e.env unchanged to the reader.
    values=env_file.read_text().replace('FORGE_IMAGE=review-release','FORGE_IMAGE='+r.RUNTIME)
    values=values.replace('ROLLOUT_STATE_DIR='+str(probe[1]),'ROLLOUT_STATE_DIR='+str(estate.snapshot))
    for key in ('REVIEW_IMAGE','REVIEW_SETTINGS_FILE','PYTHONPATH','PYTHONDONTWRITEBYTECODE'):
        if key in process_env:values+='\n'+key+'='+process_env[key]
    env_file.write_text(values+'\n')
    assert run_probe().returncode==0
    values+='\nPOLICY_CASE='+policy_case+'\n';env_file.write_text(values)
    config=estate.c;config['project']='codex-review';config['env_file']=str(env_file);config['sources']['settings']=estate.q['prepared_settings'];config['quiesce']['prepared_env_file']=str(env_file)
    r.atomic_json(estate.config_path,config)
    monkeypatch.setenv('PATH',process_env['PATH'])
    args=SimpleNamespace(**(vars(estate.args)|{'env_file':str(env_file),'project':'codex-review'}));e=q.Estate(args)
    assert e.env['DOCKER_HOST']=='unix:///var/run/docker.sock' and e.env['FORGE_PUBLISHER_DOCKER_CONTEXT']=='default'
    e.doc={'format_version':1,'binding':e.binding,'stage':'final'};e.save();pair={};calls=[]
    monkeypatch.setattr(e,'markers',lambda:pair.get('marker'));monkeypatch.setattr(e,'record',lambda:e.doc);monkeypatch.setattr(e,'prepared_settings',lambda:None)
    monkeypatch.setattr(e,'watch_closed',lambda **kw:None);monkeypatch.setattr(e,'producers_stopped',lambda:None);monkeypatch.setattr(e,'monitor',lambda:q.reader_counts(monitor()))
    monkeypatch.setattr(r,'load_volumes',lambda *a,**kw:None);monkeypatch.setattr(r,'verify_snapshot',lambda *a:{'sha256':'a'*64});monkeypatch.setattr(e,'current_state',lambda:{'work_state':{}})
    monkeypatch.setattr(r,'docker',lambda *a,**kw:SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}])))
    monkeypatch.setattr(e,'volume',lambda role,code,args=(),**kw:pair.update(marker=json.loads(args[0])))
    monkeypatch.setattr(e,'planning',lambda enabled:calls.append('planning'));monkeypatch.setattr(e,'compose',lambda *a:calls.append('compose'));monkeypatch.setattr(e,'captured',lambda *a,**kw:None);monkeypatch.setattr(e,'wait_ready',lambda commands:calls.append('ready'))
    actual_run=r.run
    def run(argv,**kw):
        assert '--read-pre-resume' in argv and kw['env'] is e.env
        calls.append('reader');return actual_run(argv,**kw)
    monkeypatch.setattr(r,'run',run)
    if policy_case=='good':
        assert e.resume()['passed'] and (e.snapshot/'resumed.json').exists() and pair.get('marker')
        assert calls.index('reader')<calls.index('planning')
    else:
        with pytest.raises(r.Refusal):e.resume()
        assert not (e.snapshot/'resumed.json').exists() and not pair and calls==['reader']


SANDBOX_TMP='/nonexistent-sandbox-only/tmp'
TEMP_NAMES=('TMPDIR','TMP','TEMP')

def resume_recording_children(estate,monkeypatch):
    # The estate env file names temp folders that exist only inside the sandbox
    # (29 September 2026: estate-check's mktemp failed on exactly this). Build
    # the real constructor and resume; every host child is recorded at r.run,
    # the one place rollout_support starts a process.
    env_file=Path(estate.args.env_file)
    env_file.write_text(env_file.read_text()+''.join(f'{name}={SANDBOX_TMP}\n' for name in TEMP_NAMES)+f'ROLLOUT_STATE_DIR={estate.snapshot}\nJARVIS_NATS_USER=jarvis\n')
    e=q.Estate(estate.args);e.phase='prepared'
    assert all(e.values[name]==SANDBOX_TMP for name in TEMP_NAMES)
    e.doc={'format_version':1,'binding':e.binding,'stage':'final'};e.save()
    host_marker=e.snapshot/'resumed.json'
    monkeypatch.setattr(e,'markers',lambda:r.read_json(host_marker) if host_marker.exists() else None)
    monkeypatch.setattr(e,'record',lambda:e.doc);monkeypatch.setattr(e,'prepared_settings',lambda:None)
    monkeypatch.setattr(e,'watch_closed',lambda **kw:None);monkeypatch.setattr(e,'producers_stopped',lambda:None);monkeypatch.setattr(e,'monitor',lambda:q.reader_counts(monitor()))
    monkeypatch.setattr(e,'current_state',lambda:{'work_state':{}})
    monkeypatch.setattr(r,'load_volumes',lambda *a,**kw:None);monkeypatch.setattr(r,'verify_snapshot',lambda *a:{'sha256':'a'*64});monkeypatch.setattr(r,'volume_identity',lambda *a,**kw:None)
    monkeypatch.setattr(e,'service_ids',lambda services:{name:{'Id':'id-'+name,'RestartCount':0} for name in services})
    children=[]
    def child(argv,**kw):
        argv=[str(x) for x in argv];children.append((argv,kw.get('env')))
        # The readiness wait's own children: what resume opened is ready at once.
        if argv[:1]==['docker'] and argv[3:4]==['inspect'] and argv[-1].startswith('id-'):out=json.dumps([{'Id':argv[-1],'RestartCount':0,'State':{'Running':True,'Status':'running','Health':{'Status':'healthy'}}}])
        elif argv[:1]==['docker'] and argv[3:4]==['exec']:out='200' if 'curl' in argv else '1'
        elif argv[:1]==['docker'] and 'inspect' in argv:out=json.dumps([{'Id':r.RUNTIME}])
        elif argv[:1]==['docker'] and 'config' in argv:out='{}'
        else:out=''
        return SimpleNamespace(returncode=0,stdout=out,stderr='')
    monkeypatch.setattr(r,'run',child)
    assert e.resume()['passed']
    return e,children

def test_resume_hands_no_host_child_the_env_files_sandbox_temp_folder(estate,monkeypatch):
    e,children=resume_recording_children(estate,monkeypatch)
    commands=[argv for argv,_ in children]
    assert any(a[0].endswith('estate-check') and '--read-pre-resume' in a for a in commands)
    assert any(a[0].endswith('estate-check') and 'services' in a for a in commands)
    assert any(a[0].endswith('factory-hello') for a in commands)
    assert any(a[:1]==['docker'] and 'compose' in a and 'up' in a for a in commands)
    assert any(a[:1]==['docker'] and 'run' in a and '--env' in a for a in commands)
    for argv,env in children:
        # env=None children inherit this process's own environment, never the file's.
        seen=os.environ if env is None else env
        for name in TEMP_NAMES:
            assert seen.get(name)!=SANDBOX_TMP,(argv,name)
            if env is not None:assert name not in env,(argv,name)
    services=next(env for argv,env in children if argv[0].endswith('estate-check') and 'services' in argv)
    assert services is e.env and services['FORGE_NATS_URL']=='nats://fake.invalid:14222' and 'TMPDIR' not in services

def test_resume_children_keep_every_other_env_file_value(estate,monkeypatch):
    e,children=resume_recording_children(estate,monkeypatch)
    # The environment the constructor built before temp names were withheld.
    before={k:v for k,v in os.environ.items() if k in ('PATH','HOME','DOCKER_CONFIG','XDG_RUNTIME_DIR')}
    before.update(e.values);before.update(DOCKER_HOST='unix:///var/run/docker.sock',FORGE_PUBLISHER_DOCKER_CONTEXT='default')
    assert set(before)-set(e.env)==set(TEMP_NAMES) and set(e.env)<=set(before)
    expected={k:v for k,v in before.items() if k not in TEMP_NAMES}
    given=[env for _,env in children if env is not None]
    assert len(given)>=5 and all(env==expected for env in given)
