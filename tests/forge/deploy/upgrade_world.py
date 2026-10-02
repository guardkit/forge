"""A fake machine for the rollout upgrade tests: Docker, Compose, sbx, sudo and the
estate's own checks, answered behind rollout_support.run, the one place the
rollout tools start a process. The tools' real code runs against it end to end.

What it keeps: containers with their image, state, exit code, start time and
mounts; nine named volumes; one real SQLite ledger every container reads; the
coordinator's settings file; the sandbox's files and its two services; each
release's closed-door receipt folder. Helper programs the tools send into a
container are either run for real against those files (the settings digests,
the ledger readers) or answered the way the image would answer.
Nothing reaches Docker, a sandbox, a bus or a network.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

V2 = '2026.09.28-2'
V3 = '2026.10.02-3'
PROVISION = 'sha256:' + '8' * 64
V3_ENTRY = {'runtime': 'sha256:' + '3' * 64, 'publisher': 'sha256:' + '4' * 64, 'memory': 'sha256:' + '5' * 64,
            'relay': 'sha256:' + '6' * 64, 'jarvis': 'sha256:' + '7' * 64, 'schema': 16}
PROJECT = 'codex-upgrade'
SANDBOX = 'codex-upgrade-sandbox'
PREFIX = 'codex-upgrade-sandbox'
SCRIPT = '/clone/deploy/sandbox-runner.sh'
SETTINGS_V2 = '/clone/.guardkit/tmp/factory-runtime/forge.yaml'
SETTINGS_V3 = '/home/agent/.forge-sandbox/codex-upgrade-sandbox/forge.yaml'
VOLUME_KEYS = {'ledger': 'forge-ledger', 'settings': 'forge-settings', 'evidence': 'forge-evidence',
               'threads': 'front-door-state', 'relay_progress': 'memory-state'}
OTHER_VOLUMES = ('forge-home', 'forge-publisher-state', 'gateway-state', 'sandbox-client-state')
SERVICES = ('coordinator', 'answer-service', 'memory', 'memory-relay', 'forge-publisher', 'sandbox-runner',
            'bus-ready', 'front-door', 'bus-gateway', 'gateway-watch')


def stamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + '123Z'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def seed_ledger(path):
    db = sqlite3.connect(path)
    db.executescript('''
    CREATE TABLE schema_version(version INTEGER, applied_at TEXT);
    INSERT INTO schema_version VALUES (16,'2026-09-30');
    CREATE TABLE builds(build_id TEXT PRIMARY KEY, status TEXT, completed_at TEXT, pending_approval_request_id TEXT, started_at TEXT, error TEXT);
    INSERT INTO builds VALUES ('b-interrupted','INTERRUPTED',NULL,NULL,'2026-09-29',NULL);
    INSERT INTO builds VALUES ('b-done','COMPLETE','2026-09-30',NULL,'2026-09-30',NULL);
    CREATE TABLE planning_runs(correlation_id TEXT, state TEXT, completed_at TEXT, pending_approval_request_id TEXT);
    CREATE TABLE work_queue(id TEXT, status TEXT, correlation_id TEXT, rank INTEGER, admitted_at TEXT, closed_at TEXT);
    CREATE TABLE publication_records(build_id TEXT, result TEXT);
    CREATE TABLE deployment_targets(target TEXT, holder_build TEXT);
    ''')
    db.commit(); db.close()


class World:
    def __init__(self, tmp_path, r, q):
        self.tmp, self.r, self.q = tmp_path, r, q
        self.events = []
        self.containers = {}
        self.counter = 0
        self.fail = {}
        self.bus_ready_polls = 0          # polls bus-ready stays running after an up
        self.bus_ready_exit = 0
        self.supervisor_stop_exit = 0
        self.on_coordinator_start = None  # callable(release) run when a coordinator starts
        self.daemon = 'daemon-fixture'
        root = tmp_path / 'machine'; root.mkdir()
        self.root = root
        self.ledger = root / 'ledger' / 'forge.db'; self.ledger.parent.mkdir(); seed_ledger(self.ledger)
        self.settings = root / 'settings' / 'forge.yaml'; self.settings.parent.mkdir()
        self.settings.write_text(yaml.safe_dump({'planning': {'enabled': True, 'target_repo_paths': {'guardkit/api_test': '/var/lib/forge/projects/api_test'}},
                                                 'routine': {'seat': 'fixture-seat', 'timeout_multiplier': 2}}, sort_keys=False))
        self.volumes = {}
        for key in (*VOLUME_KEYS.values(), *OTHER_VOLUMES):
            name = PROJECT + '_' + key
            self.volumes[name] = {'Name': name, 'Driver': 'local', 'Scope': 'local', 'Options': None,
                                  'Labels': {'com.docker.compose.project': PROJECT, 'com.docker.compose.volume': key},
                                  'Mountpoint': '/var/lib/docker/volumes/' + name + '/_data', 'CreatedAt': '2026-09-30T20:40:16Z'}
        # The sandbox, as release -2 left it.
        self.templates = {V2: b'#!/bin/sh\n# release -2 template\n', V3: b'#!/bin/sh\n# release -3 template\n'}
        self.sandbox_files = {SCRIPT: self.templates[V2], SETTINGS_V2: b'routine: {seat: fixture-seat}\n'}
        self.inner_images = {'forge:' + V2: 'sha256:' + 'a2' * 32, 'forge:' + V3: 'sha256:' + 'a3' * 32}
        self.inner = {}  # name -> image inside the sandbox
        self.marker = None
        self.pre_resume_written = {}
        self.h6_containers = {}
        self.h6_result = None        # callable(create_argv) -> (exit, result dict) for the H6 probe
        self.sandbox_back = None     # callable(argv) -> (exit, stderr) for rollout-sandbox --upgrade --back

    # ---------------------------------------------------------------- release
    def images(self, release):
        entry = self.r.RELEASES[release]
        return {'coordinator': entry['runtime'], 'answer-service': entry['runtime'], 'sandbox-runner': entry['runtime'],
                'forge-publisher': entry['publisher'], 'memory': entry['memory'], 'memory-relay': entry['relay'],
                'front-door': entry['jarvis'], 'bus-gateway': entry['jarvis'], 'bus-ready': PROVISION, 'gateway-watch': PROVISION}

    def release_of_env(self, env_file):
        return V3 if Path(env_file).name.startswith('estate-3') else V2

    def model(self, release, profiles):
        images = self.images(release)
        vol = lambda role, target, ro=False: {'type': 'volume', 'source': VOLUME_KEYS[role], 'target': target, **({'read_only': True} if ro else {})}
        services = {
            'coordinator': {'image': images['coordinator'], 'networks': {'factory': {}, 'forge-publisher-net': {}},
                            'environment': {'FORGE_PUBLISHER_URL': 'http://forge-publisher:8711', 'FORGE_DB_PATH': '/var/lib/forge/forge.db'},
                            'volumes': [vol('ledger', '/var/lib/forge'), vol('settings', '/etc/forge'), vol('evidence', '/var/lib/forge-evidence')]},
            'answer-service': {'image': images['answer-service'], 'volumes': [vol('ledger', '/var/lib/forge', True)]},
            'forge-publisher': {'image': images['forge-publisher'], 'networks': {'forge-publisher-net': {}},
                                'volumes': [vol('ledger', '/var/lib/forge', True),
                                            {'type': 'bind', 'source': str(self.publisher_settings), 'target': '/etc/forge-publisher/settings.json', 'read_only': True}],
                                'healthcheck': {'test': ['CMD', 'curl', 'http://localhost:8711/healthz']}},
            'memory': {'image': images['memory'], 'volumes': []},
            'memory-relay': {'image': images['memory-relay'], 'volumes': [vol('relay_progress', '/var/lib/fleet-memory')]},
            'bus-ready': {'image': images['bus-ready'], 'volumes': []},
            'front-door': {'image': images['front-door'], 'volumes': [vol('threads', '/app/.langgraph_api')]},
            'bus-gateway': {'image': images['bus-gateway'], 'volumes': []},
        }
        if 'sandbox' in profiles:
            services['sandbox-runner'] = {'image': images['sandbox-runner'], 'volumes': []}
        if 'watch' in profiles:
            services['gateway-watch'] = {'image': images['gateway-watch'], 'volumes': []}
        from .test_publisher_host_policy import load_helper
        bound = load_helper().binding(PROJECT)
        volumes = {key: {'name': PROJECT + '_' + key} for key in (*VOLUME_KEYS.values(), *OTHER_VOLUMES)}
        return {'name': PROJECT, 'services': services, 'volumes': volumes,
                'networks': {'forge-publisher-net': {'name': bound['network'], 'driver': 'bridge', 'enable_ipv6': False,
                                                     'driver_opts': {'com.docker.network.bridge.name': bound['bridge']}}}}

    # ---------------------------------------------------------------- containers
    def create(self, service, release, *, running=True, status=None, exit_code=0):
        self.counter += 1
        identifier = hashlib.sha256(f'{service}-{self.counter}'.encode()).hexdigest()
        model = self.model(release, ('sandbox', 'watch'))
        mounts = []
        for m in model['services'].get(service, {}).get('volumes', []):
            if m['type'] == 'volume':
                mounts.append({'Type': 'volume', 'Name': PROJECT + '_' + m['source'], 'Destination': m['target'], 'RW': not m.get('read_only', False)})
        self.containers[identifier] = {'Id': identifier, 'Name': '/' + PROJECT + '-' + service + '-1', 'Image': self.images(release)[service],
                                       'service': service, 'release': release, 'Mounts': mounts, 'RestartCount': 0,
                                       'State': {'Running': running, 'Status': status or ('running' if running else 'exited'), 'ExitCode': exit_code if not running else 0,
                                                 'StartedAt': stamp(), 'FinishedAt': '0001-01-01T00:00:00Z', 'Pid': 1 if running else 0}}
        return identifier

    def start_estate(self, release=V2):
        for service in SERVICES:
            if service == 'bus-ready':
                self.create(service, release, running=False, status='exited', exit_code=0)
            else:
                self.create(service, release)
        self.inner = {PREFIX + '-helper': 'forge:' + release, PREFIX + '-runner': 'forge:' + release}

    def of(self, service):
        return [c for c in self.containers.values() if c['service'] == service]

    def running(self, service):
        return [c for c in self.of(service) if c['State']['Running']]

    def inspect(self, identifier):
        c = self.containers[identifier]
        out = {k: v for k, v in c.items() if k not in ('service', 'release')}
        out['Config'] = {'Labels': {'com.docker.compose.project': PROJECT, 'com.docker.compose.service': c['service']}}
        state = dict(c['State'])
        if c['service'] in ('coordinator', 'front-door') and state['Running']:
            state['Health'] = {'Status': 'healthy'}
        out['State'] = state
        return out

    def stop(self, c, code=0):
        c['State'].update(Running=False, Status='exited', ExitCode=code, Pid=0, FinishedAt=stamp())
        if c['service'] == 'sandbox-runner':
            self.inner = {}

    def up(self, release, services, force):
        for service in services:
            self.events.append(('up', service, release))
            if self.fail.get('up') == service:
                return 1
            existing = self.of(service)
            image = self.images(release)[service]
            if existing and existing[0]['Image'] == image and not force:
                c = existing[0]
                if not c['State']['Running'] or service == 'bus-ready':
                    c['State'].update(Running=True, Status='running', StartedAt=stamp(), Pid=1)
            else:
                for old in existing:
                    del self.containers[old['Id']]
                time.sleep(0.002)
                c = self.containers[self.create(service, release)]
            if service == 'bus-ready':
                c['polls_left'] = self.bus_ready_polls
                self.settle_bus_ready(c)
            if service == 'coordinator' and self.on_coordinator_start:
                self.on_coordinator_start(release)
            if service == 'sandbox-runner':
                tag = 'forge:' + release
                self.inner = {PREFIX + '-helper': tag, PREFIX + '-runner': tag}
        return 0

    def settle_bus_ready(self, c):
        if c.get('polls_left', 0) <= 0 and c['State']['Running']:
            self.stop(c, self.bus_ready_exit)
        else:
            c['polls_left'] = c.get('polls_left', 0) - 1

    # ---------------------------------------------------------------- the one process entry
    def run(self, argv, *, input=None, check=True, env=None, timeout=None):
        argv = [str(x) for x in argv]
        code, out, err = self.answer(argv, env or {})
        if check and code:
            self.r.refuse(f'{Path(argv[0]).name} could not complete the requested read or operation; inspect its private diagnostics and retry')
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    def answer(self, argv, env):
        name = Path(argv[0]).name
        if name == 'docker':
            return self.docker(argv[3:] if argv[1:2] == ['--context'] else argv[1:], env)
        if name == 'sbx':
            return self.sbx(argv[1:])
        if name == 'sudo':
            self.events.append(('policy-verify', '--require-members' in argv, Path(argv[argv.index('--env-file') + 1]).name))
            return (1, '', 'REFUSED') if self.fail.get('policy') else (0, 'VERIFIED', '')
        if name == 'estate-check':
            if '--read-pre-resume' in argv:
                return self.read_pre_resume(argv, env)
            self.events.append(('estate-check', argv[1]))
            return (1, '', 'item failed') if self.fail.get('services') else (0, 'passed', '')
        if name == 'rollout-sandbox':
            self.events.append(('rollout-sandbox', tuple(a for a in argv if a in ('--upgrade', '--back')), argv[argv.index('--previous-receipt') + 1] if '--previous-receipt' in argv else None))
            code, err = self.sandbox_back(argv) if self.sandbox_back else (0, '')
            if code == 0:
                self.restore_release_2_in_sandbox()
            return code, '', err
        if name == 'factory-hello':
            self.events.append(('factory-hello',))
            return (42, '', 'hello failed') if self.fail.get('hello') else (0, 'hello', '')
        raise AssertionError('unexpected process ' + repr(argv))

    def read_pre_resume(self, argv, env):
        """estate-check --read-pre-resume, as its documented refusals (estate-check:1804-1926)."""
        image = argv[argv.index('--for-image') + 1]
        folder = Path(env['ROLLOUT_STATE_DIR'])
        self.events.append(('read-pre-resume', folder.name, image))
        receipt = folder / 'pre-resume.json'
        if not receipt.exists():
            return 1, '', 'no closed-door receipt'
        doc = json.loads(receipt.read_text())
        coordinator = self.running('coordinator')
        if doc.get('status') != 'passed' or doc.get('image') != image:
            return 1, '', 'receipt not passed or for another image'
        if not coordinator or doc['written_at'] <= coordinator[0]['State']['StartedAt']:
            return 1, '', 'stale receipt'
        return 0, 'may be acted on', ''

    def write_pre_resume(self, folder, image, status='passed', written_at=None):
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        (folder / 'pre-resume.json').write_text(json.dumps({'status': status, 'image': image, 'written_at': written_at or stamp()}))

    def compose_answer(self, args, env_file):
        release = self.release_of_env(env_file)
        profiles = ('sandbox',)
        if args[:1] == ['--profile']:
            profiles = (args[1],); args = args[2:]
        if args[0] == 'config':
            return 0, json.dumps(self.model(release, profiles)), ''
        if args[0] == 'ps':
            service = args[-1]
            if service not in self.model(release, profiles)['services']:
                return 0, '', ''
            return 0, '\n'.join(c['Id'] for c in self.of(service)), ''
        if args[0] == 'up':
            force = '--force-recreate' in args
            services = [a for a in args[1:] if not a.startswith('-')]
            return (self.up(release, services, force), '', '')
        if args[0] == 'stop':
            service = args[-1]
            for c in self.running(service):
                self.events.append(('stop', service))
                self.stop(c, self.supervisor_stop_exit if service == 'sandbox-runner' else 0)
            return 0, '', ''
        raise AssertionError('unexpected compose ' + repr(args))

    def docker(self, args, env):
        if args[0] == 'compose':
            env_file = args[args.index('--env-file') + 1]
            rest = args[args.index('--env-file') + 2:]
            while rest[:1] == ['-f']:
                rest = rest[2:]
            return self.compose_answer(rest, env_file)
        if args[0] == 'ps':
            filters = [args[i + 1] for i, a in enumerate(args) if a == '--filter']
            if any(f.startswith('volume=') for f in filters):
                volume = next(f for f in filters if f.startswith('volume='))[7:]
                ids = [c['Id'] for c in self.containers.values() if c['State']['Running'] and any(m['Name'] == volume for m in c['Mounts'])]
                return 0, '\n'.join(ids), ''
            return 0, '\n'.join(self.containers), ''
        if args[0] == 'inspect' and args[1] in self.h6_containers:
            return 0, json.dumps([self.h6_containers[args[1]]]), ''
        if args[0] == 'create':
            return self.h6_create(args)
        if args[0] == 'start' and args[1] == '--attach':
            return self.h6_start(args[2])
        if args[0] == 'rm' and args[1] == '--force':
            self.h6_containers.pop(args[2], None); return 0, '', ''
        if args[0] == 'inspect':
            if args[1] not in self.containers:
                return 1, '', 'No such object'
            item = self.inspect(args[1])
            c = self.containers[args[1]]
            if c['service'] == 'bus-ready' and c['State']['Running']:
                self.settle_bus_ready(c)
            return 0, json.dumps([item]), ''
        if args[0] == 'stop':
            c = self.containers[args[1]]
            self.events.append(('stop', c['service']))
            self.stop(c)
            return 0, '', ''
        if args[0] == 'rm':
            c = self.containers.pop(args[1])
            self.events.append(('rm', c['service'], c['release']))
            return 0, '', ''
        if args[0] == 'volume':
            if args[1] == 'ls':
                return 0, '\n'.join(self.volumes), ''
            if args[1] == 'inspect':
                return 0, json.dumps([self.volumes[n] for n in args[2:]]), ''
        if args[0] == 'info':
            return 0, self.daemon + '\n', ''
        if args[0] == 'image' and args[1] == 'inspect':
            return 0, json.dumps([{'Id': args[2]}]), ''
        if args[0] == 'run':
            return self.helper(args)
        if args[0] == 'exec':
            return self.exec(args)
        raise AssertionError('unexpected docker ' + repr(args))

    def helper(self, args):
        index = args.index('-c'); code, rest = args[index + 1], args[index + 2:]
        image = args[index - 1]
        mounts = [args[i + 1] for i, a in enumerate(args) if a == '--mount']
        if "marker_file('/state/ROLLOUT-RESUMED')" in code:
            return 0, json.dumps({'present': True, 'value': self.marker} if self.marker else {'present': False}), ''
        if 'rewritten_sha256' in code:
            self.events.append(('settings-digest', image))
            child = subprocess.run([sys.executable, '-c', code.replace('/state/forge.yaml', str(self.settings)), *rest], capture_output=True, text=True)
            return child.returncode, child.stdout, child.stderr
        if 'planning switch read back' in code:
            enabled = rest[0] == 'true'
            self.events.append(('planning', enabled, image))
            if self.fail.get('planning') == enabled:
                return 1, '', 'planning write failed'
            data = yaml.safe_load(self.settings.read_text()); data['planning']['enabled'] = enabled
            self.settings.write_text(yaml.safe_dump(data, sort_keys=False))
            return 0, 'planning switch read back\n', ''
        if "print('loaded')" in code:
            self.events.append(('settings-load', image))
            return (1, '', 'load failed') if self.fail.get('settings-load') else (0, 'loaded\n', '')
        if 'ledger_lease_proof' in code:
            return 0, json.dumps({'format_version': 1, 'complete': True, 'cleanup_complete': True, 'kind': 'clean',
                                  'files': {'forge.db': [1, 2, 3, 4, 0o644, 1000, 1000]}, 'filesystem': {'name': 'ext', 'magic': '0xef53'}}), ''
        if "state['fingerprint']" in code:
            state = self.r.ledger_state(self.ledger)
            with sqlite3.connect(self.ledger.as_uri() + '?mode=ro', uri=True) as db:
                db.execute('BEGIN'); state['logical_sha256'] = self.r.logical_digest(db)
            state['fingerprint'] = {'db_size': self.ledger.stat().st_size}
            return 0, json.dumps(state), ''
        if '/jsz?' in code:
            return 0, json.dumps({'account_details': [{'name': 'factory', 'stream_detail': [{'name': 'PIPELINE', 'consumer_detail': [
                {'name': n, 'num_pending': 0, 'num_ack_pending': 0} for n in ('forge-serve', 'forge-serve-planning')]}]}]}), ''
        if '/out/current.db' in code:
            out = Path(next(m for m in mounts if m.endswith('dst=/out')).split('src=')[1].split(',')[0]) / 'current.db'
            source = sqlite3.connect(self.ledger); target = sqlite3.connect(out); source.backup(target); target.close(); source.close()
            data = out.read_bytes()
            return 0, json.dumps({'sha256': sha(data), 'size': len(data)}), ''
        raise AssertionError('unexpected helper program')

    def exec(self, args):
        c = self.containers[args[1]]
        rest = args[2:]
        if rest[:1] == ['curl']:
            return 0, '200', ''
        code = rest[rest.index('-c') + 1]
        if 'logical_sha256' in code and 'schema_version' in code:
            self.events.append(('reader', c['service']))
            child = subprocess.run([sys.executable, '-c', code.replace('/var/lib/forge/forge.db', str(self.ledger))], capture_output=True, text=True)
            return child.returncode, child.stdout, child.stderr
        if 'b64encode' in code:
            return 0, base64.b64encode(self.ledger.read_bytes()).decode(), ''
        if '/connz?' in code:
            return 0, '1\n', ''
        raise AssertionError('unexpected exec ' + repr(rest[:3]))

    def sbx(self, args):
        assert args[0] == 'exec' and args[1] == SANDBOX, args
        rest = args[2:]
        self.events.append(('sbx', rest[0]))
        if rest[0] == 'sha256sum':
            lines = []
            for p in rest[2:]:
                if p not in self.sandbox_files:
                    return 1, '', 'No such file'
                lines.append(sha(self.sandbox_files[p]) + '  ' + p)
            return 0, '\n'.join(lines) + '\n', ''
        if rest[:2] == ['docker', 'ps']:
            return 0, '\n'.join(self.inner), ''
        if rest[:3] == ['docker', 'image', 'inspect']:
            tag = rest[-1]
            return 0, self.inner_images[tag] + ' ' + tag.split(':', 1)[1] + '\n', ''
        if rest[:2] == ['docker', 'inspect']:
            names = rest[4:]
            return 0, '\n'.join('/' + n + ' ' + self.inner_images[self.inner[n]] for n in names) + '\n', ''
        raise AssertionError('unexpected sbx ' + repr(rest))

    # ---------------------------------------------------------------- the H6 probe container
    def h6_create(self, args):
        image = args[args.index('-c') - 1]
        mounts = [args[i + 1] for i, a in enumerate(args) if a == '--mount']
        cid = 'h6-' + str(len(self.h6_containers))
        probe = Path(next(m for m in mounts if 'dst=/probe' in m).split('src=')[1].split(',')[0])
        pristine = Path(next(m for m in mounts if 'dst=/pristine.db' in m).split('src=')[1].split(',')[0])
        tail = args[args.index('-c') + 2:]
        self.h6_containers[cid] = {'Id': cid, 'Image': image, 'HostConfig': {'Init': '--init' in args}, 'State': {'ExitCode': None},
                                   'probe': str(probe), 'pristine': str(pristine), 'argv': tail}
        self.events.append(('h6-create', image, tail[4]))
        return 0, cid + '\n', ''

    def h6_start(self, cid):
        c = self.h6_containers[cid]
        code, result = self.h6_result(c)
        c['State']['ExitCode'] = code
        if result is not None:
            Path(c['probe'], 'h6-result.json').write_text(json.dumps(result))
        return code, '', '' if code == 0 else 'probe refused: schema or protocol'

    # ---------------------------------------------------------------- what TC4 does at U4
    def install_release_3_in_sandbox(self, evidence_dir, bootstrap_env):
        self.sandbox_files[SCRIPT] = self.templates[V3]
        self.sandbox_files[SETTINGS_V3] = b'routine:\n  seat: fixture-seat\nresource_preflight:\n  min_available_disk_gb: 8\n'
        evidence_dir.mkdir(parents=True, exist_ok=True)
        receipt = evidence_dir / 'sandbox-installed.json'
        self.r.atomic_json(receipt, {'format_version': 2, 'image': V3_ENTRY['runtime'], 'template_sha256': sha(self.templates[V3]),
                                     'bootstrap_env_sha256': sha(Path(bootstrap_env).read_bytes()), 'settings_sha256': sha(self.sandbox_files[SETTINGS_V3])})

    def restore_release_2_in_sandbox(self):
        self.sandbox_files[SCRIPT] = self.templates[V2]
