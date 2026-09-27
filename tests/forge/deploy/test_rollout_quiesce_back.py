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
from forge.lifecycle.migrations import apply_at_boot
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
    for p in (old/'estate.env',prepared/'estate.env'):p.write_text(env)
    compose=tmp_path/'compose.json';compose.write_text('{}')
    c={'project':'codex-quiesce-test','runtime_image':r.RUNTIME,'docker_context':'default','source_db':str(db),'snapshot_root':str(snaproot),'units':{x:'owned-'+x+'.service' for x in r.UNIT_ROLES},'old_containers':{x:'owned-'+x for x in ('coordinator','memory','relay')},'volumes':{x:'owned-'+x for x in r.VOLUME_ROLES},'sources':{'settings':str(old/'forge.yaml'),'evidence':str(old/'evidence'),'threads':str(old/'threads'),'relay_progress':str(old/'relay-progress.json')},'env_file':str(old/'estate.env'),'compose_files':[str(compose)],'quiesce':{'receipt':str(tmp_path/'quiesce.json'),'original_env_file':str(old/'estate.env'),'prepared_env_file':str(prepared/'estate.env'),'original_settings':str(old/'forge.yaml'),'prepared_settings':str(prepared/'forge.yaml'),'settings_receipt':str(prepared/'receipt.json'),'actor':'fixture','release_tag':'fixture:release'}}
    path=tmp_path/'inventory.json';r.atomic_json(path,c)
    args=SimpleNamespace(config=str(path),env_file=c['env_file'],project=c['project'],snapshot=str(snap),secret_env_file=[],plan=False,candidate_image=None,candidate_env_file=None)
    return q.Estate(args)

@pytest.mark.parametrize('pending,ack',[(1,0),(0,1),(1,1)])
def test_ack_and_pending_never_zero(pending,ack):
    with pytest.raises(r.Refusal):q.quiet_counts(q.reader_counts(monitor(pending,ack)))
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
    assert estate.plan('close')['observations']['work']['schema_version']==16
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
    monkeypatch.setattr(r,'run',lambda argv,**kw:events.append('hello' if str(argv[0]).endswith('factory-hello') else ('services' if 'services' in argv else 'pre-resume')))
    assert estate.resume()['passed'];assert events.index('ledger-marker')<events.index('planning')<events.index('services')<events.index('hello')<events.index('watch')

def test_fresh_process_import_guard_and_h6_source(tmp_path):
    code=q.GUARD+'\nimport runpy,sys\nrunpy.run_path(sys.argv[1])\nassert not any(n=="nats" or n.startswith("nats.") for n in sys.modules)\n'
    p=subprocess.run([sys.executable,'-c',code,str(HERE/'rollout-back')],capture_output=True,text=True)
    assert p.returncode==0,p.stderr

@pytest.mark.parametrize('failing_check',['services','factory-hello'])
def test_post_resume_failure_keeps_pair_and_records_failure(estate,monkeypatch,failing_check):
    estate.phase='prepared';estate.doc={'format_version':1,'binding':estate.binding,'stage':'final'};estate.save();pair={}
    monkeypatch.setattr(estate,'markers',lambda:pair.get('marker'));monkeypatch.setattr(estate,'record',lambda:estate.doc);monkeypatch.setattr(estate,'prepared_settings',lambda:None);monkeypatch.setattr(estate,'unit',lambda n:{});monkeypatch.setattr(estate,'watch_closed',lambda **kw:None);monkeypatch.setattr(estate,'producers_stopped',lambda:None);monkeypatch.setattr(estate,'monitor',lambda:q.reader_counts(monitor()));monkeypatch.setattr(r,'load_volumes',lambda *a,**k:None);monkeypatch.setattr(r,'verify_snapshot',lambda d:{'sha256':'a'*64});monkeypatch.setattr(estate,'current_state',lambda:{'work_state':{}});estate.values['ROLLOUT_STATE_DIR']=str(estate.snapshot)
    monkeypatch.setattr(r,'docker',lambda *a,**k:SimpleNamespace(stdout=json.dumps([{'Id':r.RUNTIME}])))
    monkeypatch.setattr(estate,'volume',lambda role,code,args=(),**kw:pair.update(marker=json.loads(args[0])))
    monkeypatch.setattr(estate,'planning',lambda enabled:None);monkeypatch.setattr(estate,'compose',lambda *a:pytest.fail('watch enabled after failed gate') if 'gateway-watch' in a else None)
    monkeypatch.setattr(estate,'systemctl',lambda *a:pytest.fail('invented host watch'))
    def check(argv,**kw):
        if failing_check in [str(x) for x in argv] or str(argv[0]).endswith(failing_check):raise r.Refusal('named post-resume check failed')
    monkeypatch.setattr(r,'run',check)
    with pytest.raises(r.Refusal):estate.resume()
    assert pair['marker']==r.read_json(estate.snapshot/'resumed.json')
    assert not r.read_json(estate.snapshot/'post-resume-check.json')['passed']
    assert estate.doc['stage']=='resumed'

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
