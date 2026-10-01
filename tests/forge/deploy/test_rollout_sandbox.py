"""CLI-boundary rehearsal; no broker, sandbox, systemd or host app execution.

These fault injections do not establish actual F2/F3 sandbox acceptance.
"""
import argparse
import copy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'deploy/estate/rollout-sandbox'
loader = importlib.machinery.SourceFileLoader('rollout_sandbox', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
IMAGE = 'sha256:' + '1' * 64
IDENTITY_DOCUMENT = 'forge-image-identity/2\n' + json.dumps({
    'architecture':'amd64','os':'linux','layers':['sha256:'+'2'*64],
    'env':['PRIVATE_IMAGE_SETTING=do-not-print'],'entrypoint':[],'cmd':['python'],
    'user':'','workdir':'/app','labels':{'com.guardkit.release.version':'fixture',
    'com.guardkit.release.manifest.sha256':'b'*64},'ports':{},'volumes':{},'stopsignal':'',
},separators=(',',':'))
IDENTITY = m.digest((IDENTITY_DOCUMENT+'\n').encode())


@pytest.fixture
def inventory(tmp_path):
    env = tmp_path / 'private.env'
    env.write_text('\n'.join([
        'FORGE_IMAGE='+IMAGE, 'FACTORY_GATEWAY_ADDRESS=192.0.2.10',
        'FORGE_TARGET_OWNER_URL=http://192.0.2.10:8900',
        'FORGE_SANDBOX_SIDECAR_URL=http://192.0.2.10:8925',
        'FORGE_SANDBOX_RUNNER_URL=http://192.0.2.10:8924',
        'FLEET_MEMORY_ENABLED=false','FLEET_MEMORY_PORT=30822','SANDBOX_RECEIPTS_PATH=/private/receipts',
        'SANDBOX_NAME=owned-sandbox','SANDBOX_BOOTSTRAP=/private/clone/deploy/sandbox-runner.sh',
        'SANDBOX_PROJECT_ENV_FILE='+str(tmp_path/'operational'/'bootstrap.env'),
        'SANDBOX_ENV_NAMES=SANDBOX_RECEIPTS_PATH FORGE_IMAGE FORGE_IMAGE_IDENTITY FORGE_RELEASE_VERSION FORGE_RELEASE_MANIFEST_SHA256 FORGE_TARGET_OWNER_URL '+' '.join(m.MEMORY_NAMES),
    ])+'\n')
    source = tmp_path / 'project' / 'deploy' / 'profile.yaml'
    source.parent.mkdir(parents=True)
    source.write_text('''env_id: fixture
compose:
  file: compose.yaml
sandbox:
  name: owned-sandbox
  publish: ["127.0.0.1:8901:8901", "127.0.0.1:8902:8902"]
  sidecar_publish: "127.0.0.1:8925:8125"
  runner_publish: "127.0.0.1:8924:8124"
  allow_network: ["example.test:443"]
  receipts_path: /old/receipts
cwd: /old/clone
custom_choice:
  keep: true
''')
    config = {
        'project':'owned-project','env_file':str(env),'runtime_image':IMAGE,
        'docker_context':'explicit-test','forbidden_roots':[str(tmp_path/'project')],
        'units':{'runner':'owned-runner.service','keeper':'owned-keeper.service'},
        'sandbox':{'name':'owned-sandbox','clone_path':'/private/clone',
            'known_files':['known.txt'],'receipts_path':'/private/receipts',
            'script_path':'/private/clone/deploy/sandbox-runner.sh',
            'profile_path':'/private/clone/deploy/profile.yaml','profile_source':str(source),'bootstrap_env_file':str(tmp_path/'operational'/'bootstrap.env'),
            'systemd_user_dir':str(tmp_path/'units'),'evidence_dir':str(tmp_path/'evidence'),
            'remote_ref':'origin/main','remote_name':'origin','declared_remote':'https://example.invalid/project.git','declaration_files':['README.md'],'release_image':'forge:fixture','expected_sbx_version':'v0.42.1',
            'legacy_dropins':[], 'legacy_command_markers':['legacy-bootstrap.sh','old-sidecar','old-runner'],
            'forbidden_values':['/old/','127.0.0.1'], 'allow_replacements':{},
        },
    }
    path = tmp_path/'inventory.json'
    path.write_text(json.dumps(config))
    args = ['--config',str(path),'--env-file',str(env),'--project','owned-project','--stop-legacy']
    return config,path,args


class Boundary:
    """Model just the external CLI boundary, retaining exact argv and failures."""
    def __init__(self, config, monkeypatch):
        self.config = config
        self.calls = []
        self.phase = 'before'
        self.states = {v:False for v in config['units'].values()}
        self.fault = None
        self.files = {}
        self.boot = 'boot-before'
        self.disk_doc = {'clone_commit':'a'*40,'known_files':{'known.txt':'a'*64},
            'receipt_listing':[{'path':'spaced receipt.txt','bytes':3,'sha256':'b'*64}],
            'receipt_file_count':1,'receipt_total_bytes':3,'receipt_listing_sha256':'c'*64,
            'ahead_commits':'','remote_commit':'a'*40,'declarations':{'README.md':'b'*64},'boot_id':self.boot}
        monkeypatch.setattr(m.subprocess,'run',self.run)

    def run(self, argv, **kwargs):
        self.calls.append((argv,kwargs))
        out = ''; code = 0
        if argv[0] == 'systemctl':
            verb = argv[2]
            if verb == 'show':
                unit = argv[3]
                stopped = self.states[unit]
                runner = unit == self.config['units']['runner']
                props = {'LoadState':'masked' if stopped else 'loaded','ActiveState':'inactive' if stopped else 'active',
                    'SubState':'dead' if stopped else 'running','MainPID':'0' if stopped else '123',
                    'ControlPID':'0','UnitFileState':'masked' if stopped else 'disabled','Id':unit}
                if self.fault == 'wrong-unit-id' and runner and not stopped:
                    props['Id']='other.service'
                if self.fault == 'control-pid' and not runner:
                    props['ControlPID']='77'
                if self.fault == 'keeper-alive' and not runner:
                    props['ActiveState']='active'
                out='\n'.join(k+'='+v for k,v in props.items())
            elif verb == 'mask':
                self.states[argv[3]]=True
            elif verb == 'stop':
                if self.fault=='unit-stop-error':code=1
                if self.fault=='unit-stop-timeout':raise subprocess.TimeoutExpired(argv,120)
            elif verb == 'unmask':
                if self.fault=='unmask':code=1
                else:self.states[argv[3]]=False
        elif argv[0] == 'busctl':
            field=argv[-1]
            out='a(sasbttttuii) 0\n'
            if self.fault == 'stop-hook' and field == 'ExecStop':out='a(sasbttttuii) 1 "nonempty"\n'
            if self.fault == 'post-hook' and field == 'ExecStopPost':out='a(sasbttttuii) 1 "nonempty"\n'
            if self.fault == 'missing-stop-hook' and field == 'ExecStop':out=''
            if self.fault == 'missing-post-hook' and field == 'ExecStopPost':out=''
            if self.fault == 'wrong-hook-type' and field == 'ExecStop':out='s ""\n'
            if self.fault == 'hook-error' and field == 'ExecStop':code=1
            if self.fault == 'hook-timeout' and field == 'ExecStop':raise subprocess.TimeoutExpired(argv,120)
        elif argv[0] == 'docker' and 'inspect' in argv:
            out=IDENTITY_DOCUMENT+'\n' if argv[-2].startswith('forge-image-identity/2') else IMAGE
        elif argv[0] == 'docker':
            # Exercise the actual schema/rewrite payload without another Docker.
            original = json.loads(kwargs['input'])
            from io import StringIO
            oldin,oldout=sys.stdin,sys.stdout
            sys.stdin=StringIO(json.dumps(original)); sys.stdout=StringIO()
            try:
                exec(m.PROFILE,{})
                out=sys.stdout.getvalue()
            finally:
                sys.stdin,sys.stdout=oldin,oldout
        elif argv[0] == 'bash':
            if self.fault == 'image': code=4
            out='\n'.join('[hand-release-image]   '+k+'='+v for k,v in {'FORGE_IMAGE':'forge:fixture','FORGE_IMAGE_IDENTITY':IDENTITY,'FORGE_RELEASE_VERSION':'fixture','FORGE_RELEASE_MANIFEST_SHA256':'b'*64}.items())
        elif argv[:2] == ['sbx','version']:
            doc={'client':{'version':'v0.42.1','revision':'abc123','build_tags':'cloud'},
                 'server':{'state':'running','version':'v0.42.1','revision':'abc123','api_version':'0.28.0'}}
            if self.fault=='version':doc['server']['version']='v0.43.0'
            if self.fault=='client-version':doc['client']['version']='v0.43.0'
            if self.fault=='server-unknown':doc['server']['state']='unknown'
            if self.fault=='version-missing':doc['server'].pop('version')
            out='not-json' if self.fault=='version-malformed' else json.dumps(doc)
        elif argv[:2] == ['sbx','inspect']:
            out=json.dumps({'name':'owned-sandbox','sessions':1 if self.fault=='sessions' else 0})
        elif argv[:2] == ['sbx','stop']:
            self.phase='after'
            if self.fault == 'stop-fails':code=1
            if self.fault == 'stop-timeout':raise subprocess.TimeoutExpired(argv,120)
        elif argv[:2] == ['sbx','ls']:
            status={'unknown':'mystery','empty':'','running':'running'}.get(self.fault,'stopped')
            rows=[{'name':'owned-sandbox','id':'fixture-id','agent':'docker','status':status}]
            document={'sandboxes':rows}
            if self.fault=='status-missing':document={}
            if self.fault=='status-nonlist':document={'sandboxes':{}}
            if self.fault=='status-duplicate':document['sandboxes'].append(dict(rows[0]))
            out=json.dumps(document)
            if self.fault=='status-error': code=1;out='invalid'
            if self.fault=='status-timeout':raise subprocess.TimeoutExpired(argv,120)
        elif argv[:2] == ['sbx','exec']:
            args=argv[3:] if argv[2] != '-i' else argv[4:]
            if args[:2] == ['python3','-c'] and args[2] == m.DISK:
                doc=copy.deepcopy(self.disk_doc)
                if self.phase=='after':
                    doc['boot_id']='boot-after'
                    if self.fault=='same-boot':doc['boot_id']='boot-before'
                    if self.fault=='file-changed':doc['known_files']['known.txt']='d'*64
                    if self.fault=='receipt-changed':doc['receipt_listing'][0]['sha256']='e'*64
                    if self.fault=='clone-changed':doc['clone_commit']='f'*40
                    if self.fault=='disk-unreadable':code=1
                out=json.dumps(doc)
            elif args[:2]==['python3','-c'] and args[2]==m.INSTALL:
                for item in json.loads(kwargs['input']):
                    import base64
                    self.files[item['path']]=base64.b64decode(item['data'])
                out='installed-and-read-back'
            elif args[0]=='sha256sum':
                out=m.digest(self.files[args[1]])+'  '+args[1]
            elif args[0]=='ps':
                out='PID STARTED COMMAND\n1 Sun Sep 27 08:00:00 2026 init\n'
                if self.phase=='after' and self.fault=='process':out+='55 old-sidecar\n'
                if self.phase=='before' and self.fault=='diagnostics':code=1
                if self.phase=='before' and self.fault=='diagnostics-timeout':raise subprocess.TimeoutExpired(argv,120)
            elif args[:2]==['docker','ps']:
                if self.fault=='survivor':out='abc123\n'
                if self.fault=='wake-fails':code=1
        return subprocess.CompletedProcess(argv,code,out,'private-error-do-not-print' if code else '')

    def argv(self):
        return [x[0] for x in self.calls]


@pytest.mark.parametrize('fault',[
    'version','client-version','server-unknown','version-missing','version-malformed',
    'stop-hook','post-hook','missing-stop-hook','missing-post-hook','wrong-hook-type','hook-error','hook-timeout','wrong-unit-id','unit-stop-error','unit-stop-timeout','keeper-alive','control-pid','sessions','stop-fails','stop-timeout','status-timeout',
    'unknown','empty','running','status-error','status-missing','status-nonlist','status-duplicate','survivor','wake-fails',
    'same-boot','process','file-changed','receipt-changed','clone-changed','disk-unreadable','image',
])
def test_refusal_installs_nothing(inventory,monkeypatch,capsys,fault):
    config,path,args=inventory
    boundary=Boundary(config,monkeypatch);boundary.fault=fault
    original=Path(config['sandbox']['profile_source']).read_bytes()
    assert m.main(args)==2
    error=capsys.readouterr().err
    assert 'nothing has been replaced, and work may still be running inside it' in error
    assert 'shall I try again, or put it back as it was and stop for today?' in error
    assert error.rstrip().endswith('stop for today?')
    assert not boundary.files
    assert Path(config['sandbox']['profile_source']).read_bytes()==original
    assert not any(x[:3]==['systemctl','--user','unmask'] for x in boundary.argv())
    assert not any(x[0]=='sbx' and any(y in x for y in ('rm','prune','reset','kill')) for x in boundary.argv())
    if fault in ('stop-hook','post-hook','missing-stop-hook','missing-post-hook','wrong-hook-type','hook-error','hook-timeout','wrong-unit-id'):
        assert not any(x[:3]==['systemctl','--user','stop'] for x in boundary.argv())
    if fault in ('keeper-alive','control-pid'):
        assert ['systemctl','--user','stop','owned-runner.service'] not in boundary.argv()
    if fault in ('unknown','empty','running','status-error','status-missing','status-nonlist','status-duplicate','stop-fails','stop-timeout','status-timeout'):
        assert 'work may still be running' in error
    if fault=='survivor':
        assert 'abc123' in error
        assert not any(x[0]=='sbx' and 'docker' in x and any(y in x for y in ('stop','rm')) for x in boundary.argv())
    if fault=='file-changed':
        assert 'known_files changed for ["known.txt"]' in error
        assert 'a'*64 not in error and 'd'*64 not in error
    if fault=='unit-stop-error':
        assert 'systemctl stop refused for unit owned-keeper.service' in error
    if fault=='unit-stop-timeout':
        assert 'systemctl stop did not answer for unit owned-keeper.service' in error


def test_success_order_exact_template_and_repeat(inventory,monkeypatch):
    config,path,args=inventory
    b=Boundary(config,monkeypatch)
    assert m.main(args)==0
    calls=b.argv()
    assert calls.index(['systemctl','--user','mask','owned-keeper.service']) < calls.index(['systemctl','--user','stop','owned-runner.service']) < calls.index(['sbx','stop','owned-sandbox'])
    stop_index=calls.index(['systemctl','--user','stop','owned-runner.service'])
    assert calls[stop_index-1] == ['busctl','--user','get-property','org.freedesktop.systemd1',
        '/org/freedesktop/systemd1/unit/owned_2drunner_2eservice',
        'org.freedesktop.systemd1.Service','ExecStopPost']
    assert calls[stop_index-2][-1]=='ExecStop'
    assert calls[stop_index-3][:4]==['systemctl','--user','show','owned-runner.service']
    assert calls[0] == ['sbx','version','--json']
    assert b.files[config['sandbox']['script_path']]==m.TEMPLATE.read_bytes()
    assert '--sandbox' in next(x for x in calls if x[:4]==['sbx','policy','allow','network'])
    assert all('DOCKER_HOST' not in kw['env'] for _,kw in b.calls)
    handoff=next(kw for x,kw in b.calls if x[0]=='bash')
    assert handoff['env']['FORGE_IMAGE']=='forge:fixture'
    installed=json.loads((Path(config['sandbox']['evidence_dir'])/'sandbox-installed.json').read_text())
    assert installed['actual_memory_read_write']=='NOT-TESTED'
    runtime_env=Path(config['sandbox']['bootstrap_env_file'])
    assert runtime_env.stat().st_mode & 0o777==0o600
    assert 'FORGE_IMAGE="forge:fixture"' in runtime_env.read_text()
    assert installed['bootstrap_env_file']==str(runtime_env)
    b.calls.clear()
    assert m.main(args)==0
    assert not any(x[:2]==['sbx','stop'] for x in b.argv())


@pytest.mark.parametrize('fault',['diagnostics','diagnostics-timeout'])
def test_diagnostics_failure_is_not_process_authority(inventory,monkeypatch,fault):
    config,path,args=inventory;b=Boundary(config,monkeypatch);b.fault=fault
    assert m.main(args)==0


def test_plan_is_real_cli_side_effect_free(inventory,tmp_path):
    config,path,args=inventory
    before=set(tmp_path.rglob('*'))
    result=subprocess.run([sys.executable,str(SCRIPT),*args,'--plan'],capture_output=True,text=True,env={'PATH':'/does-not-exist'})
    assert result.returncode==0,result.stderr
    assert 'NOT VERIFIED' in result.stdout
    assert set(tmp_path.rglob('*'))==before


@pytest.mark.parametrize('line',['BROKEN=$(touch /tmp/escape)','export KEY=value','DUP=a\nDUP=b','VALUE=${SHELL}','VALUE=`id`'])
def test_env_never_sourced(inventory,line,monkeypatch):
    config,path,args=inventory
    with Path(config['env_file']).open('a') as f:f.write(line+'\n')
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external command on malformed env'))
    assert m.main(args)==2


def test_shell_routes_cannot_override_explicit_env(inventory,monkeypatch):
    config,path,args=inventory
    monkeypatch.setenv('FORGE_TARGET_OWNER_URL','http://evil.test:9999')
    monkeypatch.setenv('DOCKER_HOST','unix:///host.sock')
    b=Boundary(config,monkeypatch)
    assert m.main(args)==0
    assert any('http://192.0.2.10:8900' in x for x in b.argv())
    assert not any('evil.test' in str(x) for x in b.argv())


def test_profile_preserves_choices_and_all_routes(inventory,monkeypatch):
    config,path,args=inventory;b=Boundary(config,monkeypatch)
    assert m.main(args)==0
    import yaml
    doc=yaml.safe_load(Path(config['sandbox']['profile_source']).read_text())
    assert doc['custom_choice']=={'keep':True}
    assert doc['sandbox']['allow_network']==['example.test:443','192.0.2.10:8900','192.0.2.10:30822']
    assert doc['sandbox']['receipts_path']=='/private/receipts'
    assert doc['cwd']=='/private/clone'
    publishes=[x[-1] for x in b.argv() if x[:3]==['sbx','ports','owned-sandbox'] and '--publish' in x]
    assert len(publishes)==4 and all(x.startswith('192.0.2.10:') for x in publishes)


@pytest.mark.parametrize('manual',[False,True])
def test_memory_mcp_rule_is_one_gateway_port_beside_the_answer_rule(inventory,monkeypatch,manual):
    config,path,args=inventory
    if manual:
        # A hand-written replacement for the same route must not duplicate it.
        profile=Path(config['sandbox']['profile_source'])
        profile.write_text(profile.read_text().replace('["example.test:443"]','["example.test:443", "legacy-memory.test:8005"]'))
        config['sandbox']['allow_replacements']={'legacy-memory.test:8005':'MEMORY_ROUTE'}
        path.write_text(json.dumps(config))
        edit_env(config,{'MEMORY_ROUTE':'192.0.2.10:30822'})
    b=Boundary(config,monkeypatch)
    assert m.main(args)==0
    import yaml
    rules=yaml.safe_load(Path(config['sandbox']['profile_source']).read_text())['sandbox']['allow_network']
    assert rules.count('192.0.2.10:30822')==1 and rules.count('192.0.2.10:8900')==1
    assert [x for x in rules if x.startswith('192.0.2.10')]==(['192.0.2.10:30822','192.0.2.10:8900'] if manual else ['192.0.2.10:8900','192.0.2.10:30822'])
    allowed=next(x for x in b.argv() if x[:4]==['sbx','policy','allow','network'])[-1].split(',')
    assert allowed==rules
    receipt=json.loads((Path(config['sandbox']['evidence_dir'])/'sandbox-installed.json').read_text())
    assert receipt['answer_rule']=='192.0.2.10:8900' and receipt['memory_mcp_rule']=='192.0.2.10:30822'


def test_plan_shows_answer_and_memory_mcp_rules(inventory):
    config,path,args=inventory
    result=subprocess.run([sys.executable,str(SCRIPT),*args,'--plan'],capture_output=True,text=True,env={'PATH':'/does-not-exist'})
    assert result.returncode==0,result.stderr
    assert 'answer service 192.0.2.10:8900' in result.stdout
    assert 'memory MCP 192.0.2.10:30822' in result.stdout


@pytest.mark.parametrize('change,message',[
    ({'FLEET_MEMORY_PORT':None},'FLEET_MEMORY_PORT is missing or not a port number'),
    ({'FLEET_MEMORY_PORT':''},'FLEET_MEMORY_PORT is missing or not a port number'),
    ({'FLEET_MEMORY_PORT':'mcp'},'FLEET_MEMORY_PORT is missing or not a port number'),
    ({'FLEET_MEMORY_PORT':'0'},'FLEET_MEMORY_PORT is missing or not a port number'),
    ({'FLEET_MEMORY_PORT':'030822'},'FLEET_MEMORY_PORT is missing or not a port number'),
    ({'FLEET_MEMORY_PORT':'65536'},'FLEET_MEMORY_PORT is missing or not a port number'),
    ({'FLEET_MEMORY_PORT':'192.0.2.10:30822'},'FLEET_MEMORY_PORT is missing or not a port number'),
    ({'FLEET_MEMORY_PORT':'8900'},'FLEET_MEMORY_PORT equals the answer-service port'),
    ({'FACTORY_GATEWAY_ADDRESS':None},'FACTORY_GATEWAY_ADDRESS is missing or not an IP address'),
    ({'FACTORY_GATEWAY_ADDRESS':'gateway.test'},'FACTORY_GATEWAY_ADDRESS is missing or not an IP address'),
    ({'FACTORY_GATEWAY_ADDRESS':'192.0.2.10:30822'},'FACTORY_GATEWAY_ADDRESS is missing or not an IP address'),
])
@pytest.mark.parametrize('plan',[False,True])
def test_bad_memory_mcp_route_refuses_before_anything_is_written(inventory,monkeypatch,capsys,tmp_path,change,message,plan):
    config,path,args=inventory;edit_env(config,change)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call before route validation'))
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert m.main([*args,'--plan'] if plan else args)==2
    error=capsys.readouterr().err
    assert message in error
    assert {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}==before
    assert not Path(config['sandbox']['evidence_dir']).exists()


def test_actual_disk_payload_detects_same_size_receipt_and_named_file_changes(tmp_path):
    root=tmp_path/'clone';root.mkdir();receipts=tmp_path/'receipts';receipts.mkdir()
    (root/'known.txt').write_text('before')
    (receipts/'receipt with spaces').write_text('abc')
    def git(*args):
        subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
    git('init');git('remote','add','origin','https://example.invalid/project.git');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','seed')
    config={'clone_path':str(root),'receipts_path':str(receipts),'known_files':['known.txt'],'remote_ref':'HEAD','remote_name':'origin','declared_remote':'https://example.invalid/project.git','declaration_files':['known.txt']}
    def observe():
        r=subprocess.run([sys.executable,'-c',m.DISK,json.dumps(config)],check=True,capture_output=True,text=True)
        return json.loads(r.stdout)
    first=observe();(receipts/'receipt with spaces').write_text('xyz');second=observe()
    assert first['receipt_total_bytes']==second['receipt_total_bytes']==3
    assert first['receipt_listing_sha256']!=second['receipt_listing_sha256']
    (root/'known.txt').write_text('after!');third=observe()
    assert first['known_files']!=third['known_files']


def test_actual_install_payload_writes_template_exactly(tmp_path):
    import base64
    path=tmp_path/'sandbox-runner.sh'
    data=m.TEMPLATE.read_bytes()
    item={'path':str(path),'root':str(tmp_path),'data':base64.b64encode(data).decode(),'mode':0o755}
    r=subprocess.run([sys.executable,'-c',m.INSTALL],input=json.dumps([item]),text=True,capture_output=True)
    assert r.returncode==0,r.stderr
    assert path.read_bytes()==data and path.stat().st_mode & 0o777==0o755


def test_unmask_failure_restores_masks_without_old_bootstrap(inventory,monkeypatch,capsys):
    config,path,args=inventory;b=Boundary(config,monkeypatch);b.fault='unmask'
    assert m.main(args)==2
    assert all(b.states.values())
    assert b.files[config['sandbox']['script_path']]==m.TEMPLATE.read_bytes()
    dropin=Path(config['sandbox']['systemd_user_dir'])/'owned-runner.service.d/zzzz-rollout-empty-stop.conf'
    assert dropin.read_text()==m.EMPTY_STOP
    error=capsys.readouterr().err
    assert 'installation may be incomplete' in error
    assert 'nothing has been replaced' not in error


def test_staging_failure_replaces_neither_file(tmp_path):
    import base64
    first=tmp_path/'first';first.write_text('original')
    items=[{'path':str(first),'root':str(tmp_path),'data':base64.b64encode(b'new').decode(),'mode':0o755},
           {'path':str(tmp_path/'missing'/'second'),'root':str(tmp_path),'data':base64.b64encode(b'new').decode(),'mode':0o644}]
    r=subprocess.run([sys.executable,'-c',m.INSTALL],input=json.dumps(items),capture_output=True,text=True)
    assert r.returncode!=0
    assert first.read_text()=='original'
    assert not list(tmp_path.glob('.rollout-*'))



def test_documented_env_routes_expand_without_ambient_input(inventory,monkeypatch):
    config,path,args=inventory
    env=Path(config['env_file'])
    text=env.read_text().replace('http://192.0.2.10:8900','http://${FACTORY_GATEWAY_ADDRESS}:${FORGE_ANSWER_PORT}/recorded')
    text=text.replace('http://192.0.2.10:8925','http://${FACTORY_GATEWAY_ADDRESS}:${FORGE_SANDBOX_SIDECAR_PORT}')
    text += 'FORGE_ANSWER_PORT=8900\nFORGE_SANDBOX_SIDECAR_PORT=8925\nEXTRA_MULTIWORD=FIRST SECOND THIRD\nROLLOUT_BUS_CONSUMERS=forge-serve forge-serve-planning\nPRIVATE_REF=${PRIVATE_VALUE}\n'
    env.write_text(text)
    private=env.parent/'secrets.env';private.write_text('PRIVATE_VALUE=private-canary\n');private.chmod(0o600)
    monkeypatch.setenv('FACTORY_GATEWAY_ADDRESS','203.0.113.99')
    b=Boundary(config,monkeypatch)
    assert m.main([*args,'--secret-env-file',str(private)])==0
    assert any('http://192.0.2.10:8900/recorded' in x for x in b.argv())
    values=m.load_env(env,[private])
    assert values['EXTRA_MULTIWORD']=='FIRST SECOND THIRD'
    assert values['ROLLOUT_BUS_CONSUMERS']=='forge-serve forge-serve-planning'
    assert all('private-canary' not in p.read_text() for p in Path(config['sandbox']['evidence_dir']).glob('*') if p.is_file())


@pytest.mark.parametrize('extra',['CYCLE_A=${CYCLE_B}\nCYCLE_B=${CYCLE_A}\n','MISSING=${ABSENT_NAME}\n','DEFAULT=${NAME:-unsafe}\n'])
def test_env_cycles_unset_and_shell_default_refuse(inventory,monkeypatch,extra):
    config,path,args=inventory
    with Path(config['env_file']).open('a') as f:f.write(extra)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call before env validation'))
    assert m.main(args)==2


def test_private_env_cannot_override_public_authority(inventory,monkeypatch):
    config,path,args=inventory
    private=Path(config['env_file']).with_name('secrets.env')
    private.write_text('FACTORY_GATEWAY_ADDRESS=203.0.113.99\n');private.chmod(0o600)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call before env validation'))
    assert m.main([*args,'--secret-env-file',str(private)])==2



def test_normal_runner_private_values_are_not_evidence(inventory,monkeypatch):
    config,path,args=inventory
    env=Path(config['env_file'])
    with env.open('a') as f:f.write('FLEET_MEMORY_PG_DSN=${PRIVATE_DSN}\n')
    private=env.parent/'secrets.env';private.write_text('PRIVATE_DSN=postgres://fixture:private-canary@example.invalid/db\n');private.chmod(0o600)
    b=Boundary(config,monkeypatch)
    assert m.main([*args,'--secret-env-file',str(private)])==0
    runtime=Path(config['sandbox']['bootstrap_env_file'])
    assert 'private-canary' in runtime.read_text()
    assert runtime.stat().st_mode & 0o777==0o600
    assert all('private-canary' not in p.read_text() for p in Path(config['sandbox']['evidence_dir']).glob('*') if p.is_file())



def edit_env(config, changes, add_forward=(), remove_forward=()):
    path=Path(config['env_file'])
    values=dict(line.split('=',1) for line in path.read_text().splitlines())
    for key,value in changes.items():
        if value is None:values.pop(key,None)
        else:values[key]=value
    names=values['SANDBOX_ENV_NAMES'].split()
    names=[x for x in names if x not in remove_forward]
    names.extend(x for x in add_forward if x not in names)
    values['SANDBOX_ENV_NAMES']=' '.join(names)
    path.write_text(''.join(k+'='+v+'\n' for k,v in values.items()))


@pytest.mark.parametrize('change,add,remove',[
    ({'SANDBOX_RECEIPTS_PATH':None},(),()),
    ({'SANDBOX_RECEIPTS_PATH':'/stale/receipts'},(),()),
    ({},(),('SANDBOX_RECEIPTS_PATH',)),
    ({'FORGE_RECEIPTS_DIR':'/stale/higher-priority'},('FORGE_RECEIPTS_DIR',),()),
])
def test_receipt_handoff_refuses_before_any_stop(inventory,monkeypatch,change,add,remove):
    config,path,args=inventory;edit_env(config,change,add,remove)
    b=Boundary(config,monkeypatch)
    assert m.main(args)==2
    assert not b.calls
    assert not b.files


@pytest.mark.parametrize('values,forward',[
    ({},()),
    ({'SANDBOX_SIDECAR_PORT':'9125','SANDBOX_RUNNER_PORT':'9124'},()),
    ({'SANDBOX_SIDECAR_PORT':'8125','SANDBOX_RUNNER_PORT':'8124'},('SANDBOX_SIDECAR_PORT','SANDBOX_RUNNER_PORT')),
    ({'SANDBOX_SIDECAR_PORT':'9125','SANDBOX_RUNNER_PORT':'wrong'},('SANDBOX_SIDECAR_PORT','SANDBOX_RUNNER_PORT')),
])
def test_custom_inner_ports_require_effective_forwarding(inventory,monkeypatch,values,forward):
    config,path,args=inventory
    profile=Path(config['sandbox']['profile_source'])
    profile.write_text(profile.read_text().replace(':8125',':9125').replace(':8124',':9124'))
    edit_env(config,values,forward)
    b=Boundary(config,monkeypatch)
    assert m.main(args)==2
    assert not any(x[0]=='systemctl' or x[:2]==['sbx','stop'] for x in b.argv())
    assert not b.files


@pytest.mark.parametrize('high_priority',[False,True])
def test_normal_output_drives_real_template_receipts_and_custom_ports(inventory,monkeypatch,tmp_path,high_priority):
    config,path,args=inventory
    desired=str(tmp_path/'preserved-receipts');config['sandbox']['receipts_path']=desired
    path.write_text(json.dumps(config))
    profile=Path(config['sandbox']['profile_source'])
    profile.write_text(profile.read_text().replace(':8125',':9125').replace(':8124',':9124'))
    values={'SANDBOX_RECEIPTS_PATH':desired,'SANDBOX_SIDECAR_PORT':'9125','SANDBOX_RUNNER_PORT':'9124'}
    names=['SANDBOX_SIDECAR_PORT','SANDBOX_RUNNER_PORT']
    if high_priority:
        values.update(FORGE_RECEIPTS_DIR=desired,SANDBOX_RECEIPTS_PATH=str(tmp_path/'ignored-lower-priority'))
        names.append('FORGE_RECEIPTS_DIR')
    edit_env(config,values,names)
    with monkeypatch.context() as local:
        b=Boundary(config,local)
        assert m.main(args)==0
    forwarded={k:json.loads(v).replace('$$','$') for k,v in (line.split('=',1) for line in Path(config['sandbox']['bootstrap_env_file']).read_text().splitlines())}
    spec=importlib.util.spec_from_file_location('rollout_consumer_template',Path(__file__).with_name('test_sandbox_bootstrap_from_the_release_image.py'))
    template_tests=importlib.util.module_from_spec(spec);spec.loader.exec_module(template_tests)
    consumer=tmp_path/'consumer';consumer.mkdir()
    fake=template_tests.sandbox.__wrapped__(consumer)
    # Only fake image identity settings differ; preserve actual generated path/port values.
    extra={k:v for k,v in forwarded.items() if k not in {'FORGE_IMAGE','FORGE_IMAGE_IDENTITY','FORGE_RELEASE_VERSION','FORGE_RELEASE_MANIFEST_SHA256'}}
    runs=template_tests.TestTheFoldersBothContainersShare._runs_of_a_started_bootstrap(fake,**extra)
    assert len(runs)==2
    assert all(desired+':'+desired+':rw' in run for run in runs)
    assert '--publish 0.0.0.0:9125:9125' in runs[0]
    assert '--network host' in runs[1] and '--publish' not in runs[1] and '--host 0.0.0.0 --port 9124' in runs[1]
    actual_publishes=[x[-1] for x in b.argv() if x[:3]==['sbx','ports','owned-sandbox'] and '--publish' in x]
    assert '192.0.2.10:8925:9125' in actual_publishes
    assert '192.0.2.10:8924:9124' in actual_publishes


@pytest.mark.parametrize('name,value',[
    ('FORGE_IMAGE_IDENTITY','c'*64),
    ('FORGE_RELEASE_VERSION','unreviewed'),
    ('FORGE_RELEASE_MANIFEST_SHA256','c'*64),
])
def test_helper_settings_must_match_immutable_reviewed_image(inventory,monkeypatch,capsys,name,value):
    config,path,args=inventory;b=Boundary(config,monkeypatch);original=b.run
    def changed(argv,**kw):
        result=original(argv,**kw)
        if argv[0]=='bash':
            lines=result.stdout.splitlines()
            result.stdout='\n'.join('[hand-release-image]   '+name+'='+value if line.startswith('[hand-release-image]   '+name+'=') else line for line in lines)
        return result
    monkeypatch.setattr(m.subprocess,'run',changed)
    assert m.main(args)==2
    assert 'reviewed immutable image' in capsys.readouterr().err
    assert not b.files and all(b.states.values())
    assert not Path(config['sandbox']['bootstrap_env_file']).exists()
    assert not (Path(config['sandbox']['evidence_dir'])/'bootstrap-image.env').exists()
    assert not any(x[:3]==['systemctl','--user','unmask'] for x in b.argv())
    inspections=[x for x in b.argv() if x[0]=='docker' and 'inspect' in x and x[-2].startswith('forge-image-identity/2')]
    assert len(inspections)==1 and inspections[0][-1]==config['runtime_image']
    assert all('PRIVATE_IMAGE_SETTING' not in p.read_text() for p in Path(config['sandbox']['evidence_dir']).glob('*') if p.is_file())


@pytest.mark.parametrize('document',['','forge-image-identity/1\n{}','forge-image-identity/2\n{}','forge-image-identity/2\nnot-json'])
def test_unreadable_immutable_identity_installs_nothing(inventory,monkeypatch,document):
    config,path,args=inventory;b=Boundary(config,monkeypatch);original=b.run
    def changed(argv,**kw):
        result=original(argv,**kw)
        if argv[0]=='docker' and 'inspect' in argv and argv[-2].startswith('forge-image-identity/2'):
            result.stdout=document
        return result
    monkeypatch.setattr(m.subprocess,'run',changed)
    assert m.main(args)==2
    assert not b.files and all(b.states.values())
    assert not any(x[0]=='bash' for x in b.argv())


@pytest.mark.parametrize('racing,nested_style',[(False,'classic'),(True,'classic'),(False,'containerd')])
def test_real_helper_binds_transfer_to_reviewed_identity(inventory,monkeypatch,tmp_path,racing,nested_style):
    """Real helper and template, fake engines only; moving A's tag to B refuses."""
    import shlex
    spec=importlib.util.spec_from_file_location('handoff_consumer_template',Path(__file__).with_name('test_sandbox_bootstrap_from_the_release_image.py'))
    bt=importlib.util.module_from_spec(spec);spec.loader.exec_module(bt)
    config,path,args=inventory
    config['runtime_image']=bt.ENGINE_ID;config['sandbox']['release_image']=bt.IMAGE
    path.write_text(json.dumps(config));edit_env(config,{'FORGE_IMAGE':bt.ENGINE_ID})
    engine_root=tmp_path/'fake-engine';engine_root.mkdir();fake=bt.sandbox.__wrapped__(engine_root)
    engine=bt.an_engine(images={
        'the-reviewed-release':bt.an_image(),
        'the-unreviewed-image':bt.an_image(id_classic=bt.ANOTHER_IMAGE_ID,id_containerd=bt.ANOTHER_IMAGE_ID,env=['UNREVIEWED=changed']),
    },tags={bt.IMAGE:'the-reviewed-release'},move_tag_to='the-unreviewed-image' if racing else None,move_tag_when='after-the-id')
    bt._write_the_engine(fake,engine)
    nested=dict(engine,style=nested_style);nested_table=tmp_path/'nested-table.json';nested_table.write_text(json.dumps(nested))
    bindir=tmp_path/'bin';bindir.mkdir();client=shlex.quote(str(fake['client']))
    (bindir/'docker').write_text('#!/bin/bash\nif [[ "${1:-}" == --context ]]; then shift 2; fi\nexec '+client+' "$@"\n')
    (bindir/'sbx').write_text('#!/bin/bash\n[[ "${1:-}" == exec ]] || exit 90\nshift 2\n[[ "${1:-}" == docker ]] || exit 91\nshift\nexport STANDIN_TABLE='+shlex.quote(str(nested_table))+'\nexec '+client+' "$@"\n')
    for script in bindir.iterdir():script.chmod(0o755)
    real_run=subprocess.run
    with monkeypatch.context() as local:
        b=Boundary(config,local);boundary=b.run
        def run(argv,**kw):
            if (argv[0]=='docker' and 'inspect' in argv) or argv[0]=='bash':
                b.calls.append((argv,kw));env=dict(kw['env'])
                env.update({k:v for k,v in bt._settings(fake).items() if k.startswith('STANDIN_')})
                env['PATH']=str(bindir)+':'+os.environ['PATH']
                return real_run(argv,**dict(kw,env=env))
            return boundary(argv,**kw)
        local.setattr(m.subprocess,'run',run)
        result=m.main(args)
    receipt_path=Path(config['sandbox']['evidence_dir'])/'sandbox-installed.json'
    inspections=bt._what_was_inspected(fake)
    assert inspections[0]['question']=='the-identity-document' and inspections[0]['reference']==bt.ENGINE_ID
    if racing:
        assert result==2 and not receipt_path.exists() and not b.files
        assert all(b.states.values())
        assert not Path(config['sandbox']['bootstrap_env_file']).exists()
        assert not any(x[:3]==['systemctl','--user','unmask'] for x in b.argv())
        assert any(x['question']=='the-identity-document' and x['reference']==bt.ANOTHER_IMAGE_ID for x in inspections)
        # The unchanged template also refuses B against the reviewed A identity.
        refused=bt._run(fake,FORGE_IMAGE_IDENTITY=fake['identity'])
        assert refused.returncode==2 and not bt._what_was_started(fake)
    else:
        assert result==0
        receipt=json.loads(receipt_path.read_text())
        assert b.files[config['sandbox']['script_path']]==fake['script'].read_bytes()
        assert receipt['image']==bt.ENGINE_ID
        assert receipt['bootstrap_image_settings']['FORGE_IMAGE_IDENTITY']==fake['identity']
        bt._write_the_engine(fake,nested)
        runs=bt.TestTheFoldersBothContainersShare._runs_of_a_started_bootstrap(fake,**receipt['bootstrap_image_settings'],SANDBOX_RECEIPTS_PATH=str(tmp_path/'template-receipts'))
        assert len(runs)==2
        expected=bt.an_image()['id_'+nested_style]
        assert [x['resolved'] for x in bt._what_was_started(fake)]==[expected]*2
        if nested_style=='containerd':assert expected!=bt.ENGINE_ID


def test_repeat_does_not_trust_an_old_mismatched_identity_receipt(inventory,monkeypatch,capsys):
    config,path,args=inventory;b=Boundary(config,monkeypatch)
    assert m.main(args)==0
    receipt_path=Path(config['sandbox']['evidence_dir'])/'sandbox-installed.json'
    receipt=json.loads(receipt_path.read_text())
    receipt['bootstrap_image_settings']['FORGE_IMAGE_IDENTITY']='c'*64
    receipt_path.write_text(json.dumps(receipt))
    runtime=Path(config['sandbox']['bootstrap_env_file'])
    runtime.write_text(runtime.read_text().replace(IDENTITY,'c'*64))
    before=len(b.calls)
    assert m.main(args)==2
    assert 'reviewed immutable image' in capsys.readouterr().err
    assert not any(x[:3] in (['systemctl','--user','stop'],['systemctl','--user','unmask']) for x,_ in b.calls[before:])
