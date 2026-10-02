"""rollout-sandbox --upgrade and --upgrade --back (release -3 TC4).

A rehearsal at the tool's command boundary. The "sandbox" is a real folder on
this machine with a real git clone: every python3, sha256sum and git command the
tool sends through ``sbx exec`` runs here for real (with HOME pointed at a
private folder), so the in-sandbox payloads that read state, write files and
move the clone-local git identity are the real ones. Only these are stand-ins:
the sandbox client's own verbs, systemctl, the host Docker engine, the Docker
engine inside the sandbox, and the image hand-in helper. The settings, profile
and routes payloads run for real with this checkout's own ``forge`` package in
place of the release image.

No real sandbox, unit, container or image is touched. Whether the upgrade
works against a real sandbox is left for the release -3 rehearsal.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
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
loader = importlib.machinery.SourceFileLoader('rollout_sandbox_upgrade', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)

REAL_RUN = subprocess.run
IMAGE = 'sha256:' + '3' * 64
IDENTITY_DOCUMENT = 'forge-image-identity/2\n' + json.dumps({
    'architecture':'amd64','os':'linux','layers':['sha256:'+'2'*64],
    'env':['PRIVATE_IMAGE_SETTING=do-not-print'],'entrypoint':[],'cmd':['python'],
    'user':'','workdir':'/app','labels':{'com.guardkit.release.version':'fixture',
    'com.guardkit.release.manifest.sha256':'b'*64},'ports':{},'volumes':{},'stopsignal':'',
},separators=(',',':'))
IDENTITY = m.digest((IDENTITY_DOCUMENT+'\n').encode())
SECRET_CANARY = 'previous-release-secret-canary'
OLD_TEMPLATE = b'#!/bin/bash\n# the previous release template\n'
GIT_NAMES = ('GIT_AUTHOR_NAME','GIT_AUTHOR_EMAIL','GIT_COMMITTER_NAME','GIT_COMMITTER_EMAIL')
GIT_VALUES = {'GIT_AUTHOR_NAME':'Project Builder','GIT_AUTHOR_EMAIL':'builder@example.invalid',
              'GIT_COMMITTER_NAME':'Project Builder','GIT_COMMITTER_EMAIL':'builder@example.invalid'}
CLONE_IDENTITY = {'user.name':'Clone Person','user.email':'clone-person@example.invalid'}
COORDINATOR_SETTINGS = """permissions:
  filesystem:
    allowlist: [/var/lib/forge/projects]
planning:
  target_repo_paths:
    owned/project: /var/lib/forge/projects/owned
  sandboxes:
    owned/project: {name: owned-sandbox, sidecar_url: '${FORGE_SANDBOX_SIDECAR_URL}', runner_url: '${FORGE_SANDBOX_RUNNER_URL}'}
routine:
  seat: fixture-coder-seat
"""
PRODUCER_PROFILE = '''env_id: fixture
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
'''


def git(clone, *args, check=True):
    return REAL_RUN(['git','-C',str(clone),*args],capture_output=True,text=True,check=check)


def profile_payload(source, sandbox, env, answer, memory):
    payload={'source':source,'sandbox':sandbox,'env':env,'answer':answer,'memory_mcp':memory}
    done=REAL_RUN([sys.executable,'-c',m.PROFILE],input=json.dumps(payload),capture_output=True,text=True,
                  env={'PATH':os.environ['PATH'],'PYTHONPATH':str(ROOT/'src')})
    assert done.returncode==0,done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def estate(tmp_path):
    """A sandbox installed by the previous release, its supervisor stopped."""
    clone=tmp_path/'clone';(clone/'deploy').mkdir(parents=True)
    git(clone,'init','-q')
    for key,value in CLONE_IDENTITY.items():git(clone,'config','--local',key,value)
    receipts=tmp_path/'receipts';receipts.mkdir()
    home=tmp_path/'home';home.mkdir()
    settings_path=tmp_path/'agent'/'.forge-sandbox'/'owned-sandbox'/'forge.yaml'
    upgrade=tmp_path/'upgrade';upgrade.mkdir(mode=0o700)
    env=tmp_path/'upgrade'/'estate.env'
    names=('SANDBOX_RECEIPTS_PATH FORGE_IMAGE FORGE_IMAGE_IDENTITY FORGE_RELEASE_VERSION FORGE_RELEASE_MANIFEST_SHA256 '
           'FORGE_TARGET_OWNER_URL FORGE_CONFIG_PATH SANDBOX_CONTAINER_PREFIX '+' '.join(GIT_NAMES)+' '+' '.join(m.MEMORY_NAMES))
    lines=['FORGE_IMAGE='+IMAGE,'FACTORY_GATEWAY_ADDRESS=192.0.2.10',
        'FORGE_TARGET_OWNER_URL=http://192.0.2.10:8900','FORGE_SANDBOX_SIDECAR_URL=http://192.0.2.10:8925',
        'FORGE_SANDBOX_RUNNER_URL=http://192.0.2.10:8924','FLEET_MEMORY_ENABLED=false','FLEET_MEMORY_PORT=30822',
        'SANDBOX_RECEIPTS_PATH='+str(receipts),'SANDBOX_NAME=owned-sandbox',
        'SANDBOX_BOOTSTRAP='+str(clone/'deploy'/'sandbox-runner.sh'),
        'SANDBOX_PROJECT_ENV_FILE='+str(upgrade/'sandbox-bootstrap.env'),'SANDBOX_CONTAINER_PREFIX=owned',
        'SANDBOX_ENV_NAMES='+names]
    lines+=[k+'='+v for k,v in GIT_VALUES.items()]
    env.write_text('\n'.join(lines)+'\n')
    project=tmp_path/'project'/'deploy';project.mkdir(parents=True)
    source=project/'profile.yaml'
    config={
        'project':'owned-project','env_file':str(env),'runtime_image':IMAGE,'docker_context':'explicit-test',
        'forbidden_roots':[str(tmp_path/'project')],
        'units':{'runner':'owned-runner.service','keeper':'owned-keeper.service'},
        'volumes':{'settings':'owned-project_forge-settings'},
        'sandbox':{'name':'owned-sandbox','clone_path':str(clone),'known_files':['README.md'],
            'receipts_path':str(receipts),'script_path':str(clone/'deploy'/'sandbox-runner.sh'),
            'profile_path':str(clone/'deploy'/'profile.yaml'),'profile_source':str(source),
            'bootstrap_env_file':str(upgrade/'sandbox-bootstrap.env'),'systemd_user_dir':str(tmp_path/'units'),
            'evidence_dir':str(upgrade/'sandbox-evidence'),'remote_ref':'origin/main','remote_name':'origin',
            'declared_remote':'https://example.invalid/project.git','declaration_files':['README.md'],
            'release_image':'forge:fixture','expected_sbx_version':'v0.42.1','legacy_dropins':[],
            'legacy_command_markers':['legacy-bootstrap.sh'],'forbidden_values':['/old/','127.0.0.1'],
            'allow_replacements':{},'settings_path':str(settings_path),'repo_keys':['owned/project'],
            'worktree_disk_floor_gb':8},
    }
    # What the previous install left: the producer profile rewritten to the
    # installed text, the same text in the clone, the old template.
    installed=profile_payload(PRODUCER_PROFILE,config['sandbox'],{'FACTORY_GATEWAY_ADDRESS':'192.0.2.10',
        'FORGE_SANDBOX_SIDECAR_URL':'http://192.0.2.10:8925','FORGE_SANDBOX_RUNNER_URL':'http://192.0.2.10:8924'},
        '192.0.2.10:8900','192.0.2.10:30822')['text']
    source.write_text(installed)
    (clone/'deploy'/'profile.yaml').write_text(installed)
    script=clone/'deploy'/'sandbox-runner.sh';script.write_bytes(OLD_TEMPLATE);script.chmod(0o755)
    run1=tmp_path/'run-1';(run1/'sandbox-evidence').mkdir(parents=True,mode=0o700)
    old_bootstrap=run1/'sandbox-bootstrap.env'
    old_bootstrap.write_text('FORGE_IMAGE="forge:previous"\nFORGE_CONFIG_PATH="'+str(clone)+'/.guardkit/tmp/factory-runtime/forge.yaml"\n'
                             'GUARDKIT_NATS_PASSWORD="'+SECRET_CANARY+'"\n')
    old_bootstrap.chmod(0o600)
    previous={'format_version':1,'project':'owned-project','sandbox':'owned-sandbox','image':'sha256:'+'9'*64,
        'template_sha256':m.digest(OLD_TEMPLATE),'profile_sha256':m.digest(installed.encode()),
        'bootstrap_env_file':str(old_bootstrap),
        'bootstrap_image_settings':{'FORGE_IMAGE':'forge:previous','FORGE_IMAGE_IDENTITY':IDENTITY,
            'FORGE_RELEASE_VERSION':'previous','FORGE_RELEASE_MANIFEST_SHA256':'c'*64}}
    receipt=run1/'sandbox-evidence'/'sandbox-installed.json';receipt.write_text(json.dumps(previous))
    coordinator=tmp_path/'coordinator-settings.yaml';coordinator.write_text(COORDINATOR_SETTINGS)
    inventory=upgrade/'inventory.json';inventory.write_text(json.dumps(config))
    args=['--config',str(inventory),'--env-file',str(env),'--project','owned-project',
          '--upgrade','--previous-receipt',str(receipt)]
    return {'config':config,'inventory':inventory,'args':args,'clone':clone,'home':home,
            'settings_path':settings_path,'receipt':receipt,'old_bootstrap':old_bootstrap,
            'coordinator':coordinator,'source':source,'installed_profile':installed,'tmp':tmp_path}


class Machine:
    """The external boundary: sbx, systemctl, both Docker engines, the hand-in."""
    def __init__(self, estate, monkeypatch):
        self.e=estate;self.calls=[];self.fault=None
        self.masked={'owned-runner.service':False,'owned-keeper.service':False}
        self.inner_containers=[]
        monkeypatch.setattr(m.subprocess,'run',self.run)

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        out='';code=0
        if argv[0]=='systemctl':
            verb=argv[2]
            if verb=='show':
                unit=argv[3];masked=self.masked[unit]
                active='active' if self.fault=='unit-active' and unit=='owned-keeper.service' else 'inactive'
                props={'LoadState':'masked' if masked else 'loaded','ActiveState':active,'SubState':'running' if active=='active' else 'dead',
                       'MainPID':'0','ControlPID':'0','UnitFileState':'masked' if masked else 'disabled'}
                out='\n'.join(k+'='+v for k,v in props.items())
            elif verb=='mask':self.masked[argv[3]]=True
            elif verb=='unmask':self.masked[argv[3]]=False
        elif argv[:2]==['sbx','version']:
            out=json.dumps({'client':{'version':'v0.42.1'},'server':{'state':'running','version':'v0.42.1'}})
        elif argv[:2]==['sbx','exec']:
            rest=argv[3:] if argv[2]!='-i' else argv[4:]
            if rest[0]=='docker':
                if rest[1]=='ps':out='\n'.join(self.inner_containers)
                elif rest[1:3]==['image','inspect']:
                    if self.fault=='previous-image-gone':code=1
                    else:out=IDENTITY_DOCUMENT+'\n'
            else:
                done=REAL_RUN(rest,input=kw.get('input'),capture_output=True,text=True,
                              env={'PATH':os.environ['PATH'],'HOME':str(self.e['home']),'LANG':'C.UTF-8'})
                out,code=done.stdout,done.returncode
                if code:print(done.stderr,file=sys.stderr)
                if self.fault=='install-fails' and rest[:2]==['python3','-c'] and rest[2]==m.INSTALL:code=1
        elif argv[0]=='sbx':
            pass  # ports, policy, stop: recorded, and must never appear
        elif argv[0]=='docker':
            verb=argv[3:]
            if verb[:2]==['volume','inspect']:
                out=json.dumps([{'Name':verb[2],'Labels':{'com.docker.compose.project':'owned-project'}}])
            elif verb[0]=='ps':
                out='' if self.fault=='no-supervisor' else 'a'*64+'\n'
            elif verb[:2]==['container','inspect']:
                state={'Running':False,'Restarting':False,'Status':'exited','ExitCode':0,'FinishedAt':'2026-10-02T00:00:00Z'}
                if self.fault=='supervisor-running':state.update(Running=True,Status='running')
                if self.fault=='supervisor-exit-3':state.update(ExitCode=3)
                out=json.dumps(state)
            elif verb[:2]==['image','inspect']:
                out=IDENTITY_DOCUMENT+'\n' if verb[3].startswith('forge-image-identity/2') else ('sha256:'+'7'*64 if self.fault=='wrong-tag' else IMAGE)
            elif verb[0]=='run':
                payload=argv[argv.index('-c')+1]
                extra=[]
                env={'PATH':os.environ['PATH'],'PYTHONPATH':str(ROOT/'src')}
                if payload==m.SETTINGS:extra=[str(self.e['coordinator'])];env['FORGE_SANDBOX_SIDECAR_URL']='http://leak.invalid'
                done=REAL_RUN([sys.executable,'-c',payload,*extra],input=kw.get('input'),capture_output=True,text=True,env=env)
                assert done.returncode==0,done.stderr
                out=done.stdout
        elif argv[0]=='bash':
            if self.fault=='handoff-fails':code=4
            out='\n'.join('[hand-release-image]   '+k+'='+v for k,v in {'FORGE_IMAGE':'forge:fixture','FORGE_IMAGE_IDENTITY':IDENTITY,
                'FORGE_RELEASE_VERSION':'fixture','FORGE_RELEASE_MANIFEST_SHA256':'b'*64}.items())
        return subprocess.CompletedProcess(argv,code,out,'private-error-do-not-print' if code else '')


def snapshot(e):
    clone=e['clone']
    files={p:(Path(p).read_bytes(),Path(p).stat().st_mode&0o777) for p in (clone/'deploy'/'sandbox-runner.sh',clone/'deploy'/'profile.yaml')}
    settings=e['settings_path']
    files[settings]=(settings.read_bytes(),settings.stat().st_mode&0o777) if settings.exists() else None
    identity={k:git(clone,'config','--local','--get',k,check=False).stdout for k in CLONE_IDENTITY}
    return files,identity,e['old_bootstrap'].read_bytes(),e['source'].read_bytes()


def forbidden(calls):
    return [c for c in calls if c[:2] in (['sbx','stop'],['sbx','ports'],['sbx','policy'],['sbx','rm'])
            or c[:3]==['systemctl','--user','unmask']]


def test_an_existing_receipt_with_a_different_template_is_upgraded_not_refused(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    before=snapshot(e);receipt_before=e['receipt'].read_bytes()
    hand_copy=e['clone']/'.guardkit'/'tmp'/'factory-runtime'/'forge.yaml'
    hand_copy.parent.mkdir(parents=True);hand_copy.write_text('the hand copy release -2 reads\n')
    assert m.main(e['args'])==0, capsys.readouterr().err
    clone=e['clone']
    assert (clone/'deploy'/'sandbox-runner.sh').read_bytes()==m.TEMPLATE.read_bytes()
    receipt=json.loads((Path(e['config']['sandbox']['evidence_dir'])/'sandbox-installed.json').read_text())
    assert receipt['format_version']==2
    assert receipt['template_sha256']==m.digest(m.TEMPLATE.read_bytes())
    assert receipt['previous']['template_sha256']==m.digest(OLD_TEMPLATE)
    assert receipt['previous']['release']['FORGE_RELEASE_VERSION']=='previous'
    assert receipt['previous']['bootstrap_env']=={'path':str(e['old_bootstrap']),'sha256':m.digest(before[2])}
    assert receipt['legacy_units']=='masked' and receipt['profile_reinstalled'] is False
    # The settings file (TC5) is in place, 0644, and named by the new env.
    assert e['settings_path'].stat().st_mode & 0o777==0o644
    assert receipt['settings_sha256']==m.digest(e['settings_path'].read_bytes())
    new_env=Path(e['config']['sandbox']['bootstrap_env_file'])
    text=new_env.read_text()
    assert 'FORGE_CONFIG_PATH="'+str(e['settings_path'])+'"\n' in text
    for name,value in GIT_VALUES.items():
        assert name+'='+json.dumps(value)+'\n' in text
    assert new_env.stat().st_mode & 0o777==0o600
    # The way back's own files are untouched.
    assert e['old_bootstrap'].read_bytes()==before[2]
    assert e['receipt'].read_bytes()==receipt_before
    # What release -2 reads is recorded by hash, never copied or changed.
    assert receipt['previous']['bootstrap_settings_file']=={'path':str(hand_copy),'sha256':m.digest(hand_copy.read_bytes())}
    assert hand_copy.read_text()=='the hand copy release -2 reads\n'
    assert e['source'].read_bytes()==before[3]
    out=capsys.readouterr()
    for value in GIT_VALUES.values():
        assert value not in out.out+out.err
    for value in CLONE_IDENTITY.values():
        assert value not in out.out+out.err
        assert all(value not in p.read_text() for p in Path(e['config']['sandbox']['evidence_dir']).glob('*.json'))


def test_no_ports_policy_stop_or_unmask_command_is_ever_issued(estate,monkeypatch):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    assert forbidden(machine.calls)==[]
    record=json.loads((Path(e['config']['sandbox']['evidence_dir'])/'commands.json').read_text())
    assert forbidden([x['argv'] for x in record])==[]
    assert m.main([*e['args'],'--back'])==0
    assert forbidden(machine.calls)==[]


def test_both_units_end_masked(estate,monkeypatch):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    assert all(machine.masked.values())
    assert ['systemctl','--user','mask','owned-runner.service'] in machine.calls


def test_an_already_masked_unit_is_not_masked_again(estate,monkeypatch):
    e=estate;machine=Machine(e,monkeypatch);machine.masked={k:True for k in machine.masked}
    assert m.main(e['args'])==0
    assert not any(c[:3]==['systemctl','--user','mask'] for c in machine.calls)


def test_the_clone_local_identity_is_unset_and_back_restores_its_values_exactly(estate,monkeypatch):
    # Values, not the .git/config bytes: git may lay the file out differently.
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    for key in CLONE_IDENTITY:
        result=git(e['clone'],'config','--local','--get',key,check=False)
        assert result.returncode==1 and result.stdout==''
    saved=Path(e['config']['sandbox']['evidence_dir'])/'previous-installation'/'git-identity.json'
    assert saved.stat().st_mode & 0o777==0o600 and json.loads(saved.read_text())==CLONE_IDENTITY
    assert m.main([*e['args'],'--back'])==0
    for key,value in CLONE_IDENTITY.items():
        assert git(e['clone'],'config','--local','--get',key).stdout==value+'\n'


@pytest.mark.parametrize('with_settings',[False,True])
def test_back_restores_byte_identical_files(estate,monkeypatch,with_settings):
    e=estate;machine=Machine(e,monkeypatch)
    if with_settings:
        e['settings_path'].parent.mkdir(parents=True);e['settings_path'].parent.chmod(0o755)
        e['settings_path'].write_bytes(b'planning: {}\n# an earlier settings file\n');e['settings_path'].chmod(0o640)
    before=snapshot(e)
    assert m.main(e['args'])==0
    assert snapshot(e)[0]!=before[0]
    assert m.main([*e['args'],'--back'])==0
    after=snapshot(e)
    files_before,files_after=dict(before[0]),dict(after[0])
    if not with_settings:
        # Release -2 never reads it; the generated file is left in place.
        assert files_before.pop(e['settings_path']) is None and files_after.pop(e['settings_path']) is not None
    assert files_after==files_before
    assert after[1:]==before[1:]
    restored=json.loads((Path(e['config']['sandbox']['evidence_dir'])/'sandbox-restored.json').read_text())
    assert restored['restored_release']=='previous' and restored['restored_image']=='forge:previous'
    assert restored['template_sha256']==m.digest(OLD_TEMPLATE)
    assert restored['bootstrap_env_sha256']==m.digest(before[2])
    assert restored['bootstrap_settings_file']['path'].endswith('/.guardkit/tmp/factory-runtime/forge.yaml')


def _refused_and_unchanged(e,machine,args,capsys,expected):
    before=snapshot(e)
    assert m.main(args)==2
    error=capsys.readouterr().err
    assert expected in error, error
    assert snapshot(e)==before
    assert not (Path(e['config']['sandbox']['evidence_dir'])/'previous-installation'/'previous.json').exists()
    assert not Path(e['config']['sandbox']['bootstrap_env_file']).exists()
    assert not any(c[0]=='bash' for c in machine.calls), 'no image was handed in'
    return error


@pytest.mark.parametrize('fault,expected',[
    ('supervisor-running','is not stopped'),
    ('no-supervisor','no sandbox-runner container'),
])
def test_a_running_or_unclean_supervisor_refuses(estate,monkeypatch,capsys,fault,expected):
    e=estate;machine=Machine(e,monkeypatch);machine.fault=fault
    error=_refused_and_unchanged(e,machine,e['args'],capsys,expected)
    assert 'nothing in the sandbox was changed' in error
    assert not any(c[0]=='systemctl' and c[2]=='mask' for c in machine.calls)


def test_a_live_supervisor_inside_refuses(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    state=e['home']/'.forge-runner'/hashlib.sha256(str(e['clone']).encode()).hexdigest()
    state.mkdir(parents=True)
    with open(state/'lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        _refused_and_unchanged(e,machine,e['args'],capsys,'is still running inside owned-sandbox')


def test_a_live_supervisor_record_refuses(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    state=e['home']/'.forge-runner'/hashlib.sha256(str(e['clone']).encode()).hexdigest()
    state.mkdir(parents=True)
    # This test process stands in for the supervisor.
    born=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(') ',1)[1].split()[19]
    (state/'supervisor').write_text(f'{os.getpid()} {born}\n')
    _refused_and_unchanged(e,machine,e['args'],capsys,'is still running inside owned-sandbox')


def test_a_stale_supervisor_record_does_not_refuse(estate,monkeypatch):
    e=estate;machine=Machine(e,monkeypatch)
    state=e['home']/'.forge-runner'/hashlib.sha256(str(e['clone']).encode()).hexdigest()
    state.mkdir(parents=True);(state/'supervisor').write_text(f'{os.getpid()} 1\n')
    (state/'lock').write_text('')
    assert m.main(e['args'])==0


def test_a_leftover_helper_container_refuses(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch);machine.inner_containers=['owned-helper']
    _refused_and_unchanged(e,machine,e['args'],capsys,'owned-helper still exists inside owned-sandbox')
    inner=[c for c in machine.calls if c[:2]==['sbx','exec'] and 'ps' in c][0]
    assert 'name=^owned-helper$' in inner and 'name=^owned-runner$' in inner


def edit_env(e, name, value=None, drop_name=False):
    path=Path(e['config']['env_file'])
    lines=[]
    for line in path.read_text().splitlines():
        key,_,current=line.partition('=')
        if key==name:
            if value is None:continue
            line=key+'='+value
        if key=='SANDBOX_ENV_NAMES' and drop_name:
            line=key+'='+' '.join(x for x in current.split() if x!=name)
        lines.append(line)
    path.write_text('\n'.join(lines)+'\n')


@pytest.mark.parametrize('name',GIT_NAMES)
@pytest.mark.parametrize('how',['missing','empty','not-forwarded'])
def test_a_missing_or_empty_git_name_refuses_by_name(estate,monkeypatch,capsys,name,how):
    e=estate
    edit_env(e,name,'' if how=='empty' else (GIT_VALUES[name] if how=='not-forwarded' else None),drop_name=how=='not-forwarded')
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call before the identity names were checked'))
    assert m.main(e['args'])==2
    assert name in capsys.readouterr().err


def test_a_profile_route_change_refuses(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    e['source'].write_text(e['source'].read_text().replace('192.0.2.10:8901:8901','192.0.2.10:8911:8901'))
    _refused_and_unchanged(e,machine,e['args'],capsys,'an upgrade does not move routes')


def test_a_profile_changed_for_another_reason_is_reinstalled_and_says_so(estate,monkeypatch):
    e=estate;machine=Machine(e,monkeypatch)
    e['source'].write_text(e['source'].read_text()+'custom_choice:\n  keep: true\n')
    assert m.main(e['args'])==0
    receipt=json.loads((Path(e['config']['sandbox']['evidence_dir'])/'sandbox-installed.json').read_text())
    assert receipt['profile_reinstalled'] is True and 'routes unchanged' in receipt['profile_reinstall_reason']
    assert 'custom_choice' in (e['clone']/'deploy'/'profile.yaml').read_text()
    assert m.main([*e['args'],'--back'])==0
    assert (e['clone']/'deploy'/'profile.yaml').read_text()==e['installed_profile']


def test_a_clone_whose_template_differs_from_the_previous_receipt_refuses(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    (e['clone']/'deploy'/'sandbox-runner.sh').write_bytes(b'edited by hand\n')
    _refused_and_unchanged(e,machine,e['args'],capsys,'is not the template the previous receipt installed')


def test_an_upgrade_never_runs_twice_over_its_saved_copies(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    saved=(Path(e['config']['sandbox']['evidence_dir'])/'previous-installation'/'sandbox-runner.sh').read_bytes()
    assert m.main(e['args'])==2
    assert 'already saved' in capsys.readouterr().err
    assert saved==OLD_TEMPLATE


def test_the_previous_bootstrap_env_is_never_the_new_one(estate,monkeypatch,capsys):
    e=estate
    e['config']['sandbox']['bootstrap_env_file']=str(e['old_bootstrap'])
    e['inventory'].write_text(json.dumps(e['config']))
    edit_env(e,'SANDBOX_PROJECT_ENV_FILE',str(e['old_bootstrap']))
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call'))
    assert m.main(e['args'])==2
    assert "never rewrites the way back's" in capsys.readouterr().err


def test_a_failure_part_way_says_so_and_back_puts_everything_back(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch);machine.fault='handoff-fails'
    before=snapshot(e)
    assert m.main(e['args'])==2
    error=capsys.readouterr().err
    assert 'the upgrade is part-way' in error and '--upgrade --back puts it back' in error
    machine.fault=None
    assert m.main([*e['args'],'--back'])==0
    assert snapshot(e)[1:]==before[1:]
    assert dict(snapshot(e)[0])==dict(before[0])


def test_back_with_nothing_saved_verifies_in_place(estate,monkeypatch):
    e=estate;machine=Machine(e,monkeypatch)
    before=snapshot(e)
    assert m.main([*e['args'],'--back'])==0
    assert snapshot(e)==before
    restored=json.loads((Path(e['config']['sandbox']['evidence_dir'])/'sandbox-restored.json').read_text())
    assert 'verified in place' in restored['restored']['note']


def test_back_accepts_a_removed_supervisor_but_not_a_running_one(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    machine.fault='supervisor-running'
    assert m.main([*e['args'],'--back'])==2
    assert 'is not stopped' in capsys.readouterr().err
    machine.fault='no-supervisor'
    assert m.main([*e['args'],'--back'])==0


def test_back_refuses_when_the_previous_bootstrap_env_changed(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    template=(e['clone']/'deploy'/'sandbox-runner.sh').read_bytes()
    e['old_bootstrap'].write_text('FORGE_IMAGE="something-else"\n')
    assert m.main([*e['args'],'--back'])==2
    assert 'no longer has its recorded hash' in capsys.readouterr().err
    assert (e['clone']/'deploy'/'sandbox-runner.sh').read_bytes()==template


def test_back_refuses_when_the_previous_image_is_gone(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    machine.fault='previous-image-gone'
    assert m.main([*e['args'],'--back'])==2
    assert 'hand it in again' in capsys.readouterr().err
    assert (e['clone']/'deploy'/'sandbox-runner.sh').read_bytes()==m.TEMPLATE.read_bytes()


def test_back_refuses_a_tampered_saved_copy(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    saved=Path(e['config']['sandbox']['evidence_dir'])/'previous-installation'/'sandbox-runner.sh'
    saved.write_bytes(b'not what was saved\n')
    assert m.main([*e['args'],'--back'])==2
    assert 'does not match its recorded hash' in capsys.readouterr().err


def test_plan_prints_the_settings_seat_floor_and_names_and_changes_nothing(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    before=snapshot(e);files_before={p for p in e['tmp'].rglob('*')}
    assert m.main([*e['args'],'--plan'])==0
    out=capsys.readouterr().out
    assert 'owned/project -> '+str(e['clone']) in out
    assert "Routine seat (the coordinator's): fixture-coder-seat" in out
    assert 'worktree disk floor: 8 GiB' in out
    assert 'min_available_disk_gb: 8' in out
    assert 'Git identity names present with values: '+' '.join(GIT_NAMES) in out
    assert 'FORGE_CONFIG_PATH' in out
    for value in GIT_VALUES.values():
        assert value not in out
    assert snapshot(e)==before and {p for p in e['tmp'].rglob('*')}==files_before
    assert not any(c[0] in ('sbx','systemctl','bash') for c in machine.calls)
    assert all(c[0]=='docker' for c in machine.calls)


def test_back_plan_changes_nothing(estate,monkeypatch,capsys):
    e=estate
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call in a plan'))
    assert m.main([*e['args'],'--back','--plan'])==0
    assert 'Plan only (--upgrade --back)' in capsys.readouterr().out


@pytest.mark.parametrize('argv,message',[
    (['--stop-legacy'],'different operations'),
])
def test_upgrade_and_stop_legacy_do_not_mix(estate,monkeypatch,capsys,argv,message):
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call'))
    assert m.main([*estate['args'],*argv])==2
    assert message in capsys.readouterr().err


def test_back_needs_upgrade(estate,monkeypatch,capsys):
    args=[x for x in estate['args'] if x!='--upgrade']+['--back']
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call'))
    assert m.main(args)==2
    assert '--back goes with --upgrade' in capsys.readouterr().err


def test_a_settings_folder_others_can_write_refuses_before_anything_changes(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    e['settings_path'].parent.mkdir(parents=True);e['settings_path'].parent.chmod(0o775)
    _refused_and_unchanged(e,machine,e['args'],capsys,'is writable by others (mode 775)')


# ---------------------------------------------------------------------------
# Fix pass after the coach's review (2 October 2026)
# ---------------------------------------------------------------------------


def test_no_secret_bearing_env_file_is_copied_into_the_evidence(estate,monkeypatch):
    e=estate;machine=Machine(e,monkeypatch)
    old=e['old_bootstrap'].read_bytes()
    assert m.main(e['args'])==0
    assert m.main([*e['args'],'--back'])==0
    evidence=Path(e['config']['sandbox']['evidence_dir'])
    files=[p for p in evidence.rglob('*') if p.is_file()]
    assert files and not (evidence/'previous-installation'/'bootstrap.env').exists()
    for path in files:
        data=path.read_bytes()
        assert data!=old and SECRET_CANARY.encode() not in data, path
    manifest=json.loads((evidence/'previous-installation'/'previous.json').read_text())
    assert manifest['bootstrap_env']=={'path':str(e['old_bootstrap']),'sha256':m.digest(old)}


def test_back_says_plainly_that_a_generated_settings_file_is_left(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    capsys.readouterr()
    assert m.main([*e['args'],'--back'])==0
    out=capsys.readouterr().out
    assert 'The generated settings file '+str(e['settings_path'])+' and its folder are left in place' in out
    assert 'release -2 never reads it' in out
    restored=json.loads((Path(e['config']['sandbox']['evidence_dir'])/'sandbox-restored.json').read_text())
    assert 'left in place' in restored['restored']['settings'] and 'release -2 never reads it' in restored['restored']['settings']
    assert 'restored exactly' in restored['restored']['git_identity']['note']
    assert e['settings_path'].exists()


def test_a_wrong_release_tag_refuses_before_any_change(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch);machine.fault='wrong-tag'
    _refused_and_unchanged(e,machine,e['args'],capsys,'release_image tag no longer resolves')
    assert not any(c[:3]==['systemctl','--user','mask'] for c in machine.calls)


def test_two_clone_local_names_refuse_with_a_plain_sentence(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    git(e['clone'],'config','--local','--add','user.name','Second Name')
    error=_refused_and_unchanged(e,machine,e['args'],capsys,'more than one local user.name')
    assert 'sbx refused' not in error
    assert not any(c[:3]==['systemctl','--user','mask'] for c in machine.calls)


def test_a_settings_path_in_the_template_state_folders_refuses(estate,monkeypatch,capsys):
    e=estate
    e['config']['sandbox']['settings_path']=str(e['home']/'.forge-runner'/'abc'/'forge.yaml')
    e['inventory'].write_text(json.dumps(e['config']))
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('external call before settings validation'))
    assert m.main(e['args'])==2
    assert "template's own state folders" in capsys.readouterr().err


def test_a_settings_path_reaching_the_state_folders_through_a_link_refuses(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    state=e['home']/'.forge-runner'/'abc';state.mkdir(parents=True)
    link=e['tmp']/'agent-link';link.symlink_to(state)
    e['config']['sandbox']['settings_path']=str(link/'forge.yaml')
    e['inventory'].write_text(json.dumps(e['config']))
    _refused_and_unchanged(e,machine,e['args'],capsys,'overlaps the template state folders (~/.forge-runner)')


def test_the_receipt_carries_what_switch_reads(estate,monkeypatch):
    """rollout-quiesce --switch (TC2) reads format 2 and four keys; each must mean
    what it hashes there: the clone's bootstrap, the bootstrap env file on this
    machine, and the settings file FORGE_CONFIG_PATH names."""
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    receipt=json.loads((Path(e['config']['sandbox']['evidence_dir'])/'sandbox-installed.json').read_text())
    assert receipt['format_version']==2 and receipt['image']==e['config']['runtime_image']
    assert receipt['template_sha256']==m.digest((e['clone']/'deploy'/'sandbox-runner.sh').read_bytes())
    env_file=Path(e['config']['sandbox']['bootstrap_env_file'])
    assert receipt['bootstrap_env_sha256']==m.digest(env_file.read_bytes())
    named=[json.loads(v) for k,_,v in (l.partition('=') for l in env_file.read_text().splitlines()) if k=='FORGE_CONFIG_PATH']
    assert named==[str(e['settings_path'])]
    assert receipt['settings_sha256']==m.digest(e['settings_path'].read_bytes())


# ---------------------------------------------------------------------------
# Codex tools review round 1, R2: the settings boundary is checked on RESOLVED
# paths inside the sandbox, before installing and again at write time.
# ---------------------------------------------------------------------------


def _link_settings_into(e, target):
    target.mkdir(parents=True,exist_ok=True)
    link=e['tmp']/'agent-settings-link';link.symlink_to(target)
    e['config']['sandbox']['settings_path']=str(link/'forge.yaml')
    e['inventory'].write_text(json.dumps(e['config']))
    return link


@pytest.mark.parametrize('where,name',[
    ('clone','sandbox.clone_path'),
    ('receipts','sandbox.receipts_path'),
    ('worktrees','FORGE_AUTOBUILD_WORKTREE_BASE'),
])
def test_a_symlinked_parent_into_the_clone_or_a_shared_folder_refuses(estate,monkeypatch,capsys,where,name):
    e=estate
    worktrees=e['tmp']/'worktrees';worktrees.mkdir()
    env=Path(e['config']['env_file'])
    env.write_text(env.read_text().replace('SANDBOX_ENV_NAMES=','SANDBOX_ENV_NAMES=FORGE_AUTOBUILD_WORKTREE_BASE ')
                   +'FORGE_AUTOBUILD_WORKTREE_BASE='+str(worktrees)+'\n')
    folder={'clone':e['clone']/'.guardkit'/'settings','receipts':Path(e['config']['sandbox']['receipts_path'])/'settings','worktrees':worktrees/'settings'}[where]
    _link_settings_into(e,folder)
    machine=Machine(e,monkeypatch)
    error=_refused_and_unchanged(e,machine,e['args'],capsys,'which overlaps ')
    assert name in error.split('which overlaps ',1)[1]
    assert not (folder/'forge.yaml').exists()
    assert not any(c[:3]==['systemctl','--user','mask'] for c in machine.calls)


def test_a_plain_settings_path_is_accepted(estate,monkeypatch):
    e=estate;machine=Machine(e,monkeypatch)
    assert m.main(e['args'])==0
    assert e['settings_path'].is_file() and not any(p.is_symlink() for p in [e['settings_path'],*e['settings_path'].parents])


def _install(item):
    return REAL_RUN([sys.executable,'-c',m.INSTALL],input=json.dumps([item]),text=True,capture_output=True)


def test_the_install_step_refuses_an_excluded_root_reached_through_a_link(tmp_path):
    clone=tmp_path/'clone';(clone/'inside').mkdir(parents=True);(clone/'inside').chmod(0o755)
    link=tmp_path/'settings-link';link.symlink_to(clone/'inside')
    item={'path':str(link/'forge.yaml'),'root':str(link),'data':base64.b64encode(b'x: 1\n').decode(),
          'mode':0o644,'parent_mode':0o755,'excluded':[str(clone)],'real_parent':None}
    r=_install(item)
    assert r.returncode!=0
    assert list((clone/'inside').iterdir())==[]


def test_the_install_step_refuses_a_link_made_after_the_check(tmp_path):
    """The check resolved the folder to A; by write time it is a link to B."""
    checked=tmp_path/'agent'/'.forge-sandbox';checked.mkdir(parents=True)
    elsewhere=tmp_path/'clone'/'inside';elsewhere.mkdir(parents=True);elsewhere.chmod(0o755)
    real_parent=str(checked)
    checked.rmdir();checked.symlink_to(elsewhere)
    item={'path':str(checked/'forge.yaml'),'root':str(checked),'data':base64.b64encode(b'x: 1\n').decode(),
          'mode':0o644,'parent_mode':0o755,'excluded':[],'real_parent':real_parent}
    r=_install(item)
    assert r.returncode!=0
    assert list(elsewhere.iterdir())==[]


def test_the_install_step_writes_a_plain_path_through_one_descriptor(tmp_path):
    target=tmp_path/'agent'/'.forge-sandbox'/'owned'/'forge.yaml'
    item={'path':str(target),'root':str(target.parent),'data':base64.b64encode(b'x: 1\n').decode(),
          'mode':0o644,'parent_mode':0o755,'excluded':[str(tmp_path/'clone')],'real_parent':str(target.parent)}
    r=_install(item)
    assert r.returncode==0,r.stderr
    assert target.read_bytes()==b'x: 1\n' and target.stat().st_mode&0o777==0o644
    assert not [p for p in target.parent.iterdir() if p.name.startswith('.rollout-')]


# ---------------------------------------------------------------------------
# Re-check M1: the same supervisor-exit rule as rollout-quiesce --final, and
# one overall time limit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('back',[False,True])
def test_a_non_zero_supervisor_exit_is_accepted_when_nothing_runs_inside(estate,monkeypatch,back):
    e=estate;machine=Machine(e,monkeypatch)
    if back:
        assert m.main(e['args'])==0
    machine.fault='supervisor-exit-3'
    assert m.main([*e['args'],*(['--back'] if back else [])])==0
    name='sandbox-restored.json' if back else 'sandbox-installed.json'
    receipt=json.loads((Path(e['config']['sandbox']['evidence_dir'])/name).read_text())
    seen=receipt['compose_supervisor']['containers'][0]
    assert seen['exit_code']==3 and 'did not succeed' in seen['meaning']
    assert seen['accepted_because'].startswith('nothing runs inside the sandbox')


@pytest.mark.parametrize('back',[False,True])
@pytest.mark.parametrize('inside',['container','lock'])
def test_a_non_zero_supervisor_exit_with_work_inside_refuses_in_quiesce_s_words(estate,monkeypatch,capsys,back,inside):
    e=estate;machine=Machine(e,monkeypatch)
    if back:
        assert m.main(e['args'])==0
    capsys.readouterr()
    machine.fault='supervisor-exit-3'
    before=snapshot(e)
    lock=None
    if inside=='container':
        machine.inner_containers=['owned-runner']
    else:
        state=e['home']/'.forge-runner'/hashlib.sha256(str(e['clone']).encode()).hexdigest()
        state.mkdir(parents=True,exist_ok=True)
        lock=open(state/'lock','w');fcntl.flock(lock,fcntl.LOCK_EX)
    try:
        assert m.main([*e['args'],*(['--back'] if back else [])])==2
    finally:
        if lock:lock.close()
    error=capsys.readouterr().err
    busy='owned-runner' if inside=='container' else "the supervisor's lock held"
    assert ('sandbox-runner exited 3: its stop of the work inside the sandbox did not succeed; and inside the sandbox there is still '
            +busy+', so nothing was changed. Stop the bootstrap\'s work inside the sandbox (its stop word, as the supervisor\'s log says), then run this step again') in error
    assert snapshot(e)==before


def test_the_upgrade_has_one_overall_time_limit(estate,monkeypatch,capsys):
    e=estate;machine=Machine(e,monkeypatch)
    clock=[1000.0]
    monkeypatch.setattr(m.time,'monotonic',lambda:clock[0])
    real=machine.run
    def slow(argv,**kw):
        assert kw['timeout']<=m.UPGRADE_LIMIT_SECONDS-(clock[0]-1000.0)+1e-6
        if argv[:2]==['sbx','version']:clock[0]+=m.UPGRADE_LIMIT_SECONDS
        return real(argv,**kw)
    monkeypatch.setattr(m.subprocess,'run',slow)
    before=snapshot(e)
    assert m.main(e['args'])==2
    assert 'reached its own time limit of 1500 seconds' in capsys.readouterr().err
    assert snapshot(e)==before
    assert len(machine.calls)==1


def test_the_limit_is_stated_in_help(capsys):
    with pytest.raises(SystemExit):
        m.main(['--help'])
    assert '1500 seconds overall' in ' '.join(capsys.readouterr().out.split())
