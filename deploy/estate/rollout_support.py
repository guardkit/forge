"""Internal standard-library support for the rollout commands; not an application entry point.

Artifact format 1: metadata.json describes the untouched forge.db and full work_state;
previous-runtime.json contains environment NAMES only. ROLLOUT-SNAPSHOT.json binds
both the original and migrated hashes. load-receipt.json never asserts service
readback until --verify-containers has actually read the mark in all three services.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import shlex
import hashlib
import inspect as python_inspect
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

MARK = 'ROLLOUT-SNAPSHOT.json'
MIGRATED_ARTIFACT = 'migrated-forge.db'
RUNTIME = 'sha256:b66c27888512e54adac5da6cd99b80f930919c950f47edb7d5598c9ddb75c5f0'
UNIT_ROLES = {'gateway', 'frontdoor', 'watchdog_timer', 'watchdog_service', 'autobuild', 'runner', 'keeper', 'langgraph_sidecar', 'deploy_sidecar'}
VOLUME_ROLES = {'ledger', 'settings', 'evidence', 'threads', 'relay_progress'}
PUBLISHER_RUNTIME = 'sha256:e13dfe4564b18e4158ee7c7836b5e71a07aee35aa0699a480e4a8cbd389104dd'
WORK_TABLES = {'builds': 1, 'planning_runs': 3, 'work_queue': 10, 'publication_records': 15, 'deployment_targets': 16}

class Refusal(Exception):
    pass

def refuse(message):
    raise Refusal(message)

def run(argv, *, input=None, check=True, env=None):
    # Never print a failed command's stderr: Compose diagnostics can contain secrets.
    result = subprocess.run([str(x) for x in argv], input=input, capture_output=True, text=True, timeout=180, env=env)
    if check and result.returncode:
        refuse(f'{Path(str(argv[0])).name} could not complete the requested read or operation; inspect its private diagnostics and retry')
    return result

def path(value, *, exists=True):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts or any(x.is_symlink() for x in (p, *p.parents)):
        refuse(f'path {p} is relative, traverses a parent or crosses a symlink; supply an absolute direct path')
    if exists and not p.exists():
        refuse(f'path {p} is missing; supply the intended existing path')
    return p.resolve()

def read_json(p):
    return json.loads(path(p).read_text())

def sha256(p):
    with open(p, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def atomic_json(p, value):
    p = Path(p)
    path(p, exists=False)
    fd, tmp = tempfile.mkstemp(prefix='.' + p.name, dir=p.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, sort_keys=True, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
        fd = os.open(p.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def ledger_state(p, *, consolidated=False):
    p = path(p)
    # Source readers MUST honor committed WAL; only a consolidated backup is immutable.
    if consolidated and Path(str(p)+'-wal').exists() and Path(str(p)+'-wal').stat().st_size:
        refuse(f'backup {p} has a nonempty WAL; recover the consolidated snapshot before retrying')
    uri = p.as_uri() + ('?mode=ro&immutable=1' if consolidated else '?mode=ro')
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok']:
            refuse(f'ledger {p} failed integrity_check; repair the source before retrying')
        tables = sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        if 'schema_version' not in tables:
            refuse(f'ledger {p} has no recorded schema; select the authoritative Forge ledger')
        version = db.execute('SELECT max(version) FROM schema_version').fetchone()[0]
        if type(version) is not int or not 1 <= version <= 16:
            refuse(f'ledger {p} has an unsupported schema; select a release that knows this schema')
        counts = {}
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = db.execute(f'SELECT count(*) FROM {quoted}').fetchone()[0]
        work = {}
        for table, introduced in WORK_TABLES.items():
            if table not in tables:
                if version >= introduced:
                    refuse(f'ledger {p} is missing required table {table}; reconcile its schema before retrying')
                work[table] = {'status': 'NOT-YET', 'introduced_in': introduced}
            else:
                columns = {
                    'builds': ['build_id', 'status', 'completed_at', 'pending_approval_request_id'],
                    'planning_runs': ['correlation_id', 'state', 'completed_at', 'pending_approval_request_id'],
                    'work_queue': ['id', 'status', 'correlation_id', 'rank', 'admitted_at', 'closed_at'],
                }.get(table)
                present = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                if columns and not set(columns) <= present:
                    refuse(f'ledger {p} lacks required state columns in {table}; reconcile its schema before retrying')
                selection = ','.join('"'+x+'"' for x in columns) if columns else '*'
                predicate = " WHERE status IN ('QUEUED','ADMITTED')" if table == 'work_queue' else ''
                rows = [dict(r) for r in db.execute(f'SELECT {selection} FROM "{table}"{predicate}')]
                rows.sort(key=lambda r: json.dumps(r, sort_keys=True))
                work[table] = {'status': 'observed', 'rows': rows, 'count': len(rows)}
        return {'schema_version': version, 'tables': counts, 'work_state': work}

def logical_digest(db):
    """Hash every persisted schema definition, column and row, independent of pages/WAL.

    Callers hold a read transaction. Values remain typed; BLOBs are losslessly
    hex-encoded. Sorting encoded rows preserves duplicates without assuming keys.
    No user data is returned or written to the receipt.
    """
    def encode(value):
        return {'blob': value.hex()} if isinstance(value, bytes) else value
    def packed(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    schema = [list(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name,tbl_name')]
    header = {name: db.execute('PRAGMA '+name).fetchone()[0]
              for name in ('application_id', 'user_version', 'encoding', 'auto_vacuum')}
    digest = hashlib.sha256(packed([header, schema]).encode())
    for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        cursor = db.execute('SELECT * FROM ' + quoted)
        columns = [column[0] for column in cursor.description]
        rows = sorted(packed([encode(value) for value in row]) for row in cursor)
        digest.update(packed([name, columns, rows]).encode())
    return digest.hexdigest()

def consolidated_logical_digest(p):
    with closing(sqlite3.connect(path(p).as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
        db.execute('BEGIN')
        return logical_digest(db)

# Exact SQLite-only boot steps from the accepted bind_production_serve caller.
# Load the pure coexistence module directly: its package initializer imports
# unrelated live bridge/client integrations, which this operation does not use.
BOOT_SQLITE_CODE = r'''
import pathlib,sys,importlib.util,json,hashlib
from forge.adapters.sqlite.connect import connect_writer
from forge.lifecycle.migrations import apply_at_boot
from forge.persistence.migrations import lifecycle_bridge_registry
import forge.lifecycle.migrations as canonical
module_path=pathlib.Path(canonical.__file__).parents[1]/'lifecycle_bridge'/'coexistence.py'
spec=importlib.util.spec_from_file_location('rollout_boot_coexistence',module_path)
coexistence=importlib.util.module_from_spec(spec);sys.modules[spec.name]=coexistence;spec.loader.exec_module(coexistence)
c=connect_writer(pathlib.Path('/copy/forge.db'))
assert apply_at_boot(c)==16
coexistence.apply_migration(c)
lifecycle_bridge_registry.apply(c)
c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close()
assert not any(n=='nats' or n.startswith('nats.') or n=='nats_core.client' for n in sys.modules)
'''


def verify_snapshot(directory):
    directory = path(directory)
    metadata = read_json(directory / 'metadata.json')
    db = path(directory / 'forge.db')
    if metadata.get('format_version') != 1 or sha256(db) != metadata.get('sha256'):
        refuse(f'snapshot {directory} does not match its recorded hash; recover the untouched snapshot')
    actual = ledger_state(db, consolidated=True)
    if any(metadata.get(k) != v for k, v in actual.items()):
        refuse(f'snapshot {directory} state differs from its metadata; recover the untouched snapshot')
    runtime = path(directory / 'previous-runtime.json')
    if sha256(runtime) != metadata.get('previous_runtime_sha256'):
        refuse(f'snapshot {directory} runtime record was changed; recover the original runtime record')
    return metadata

def config(p, env_file=None, project=None):
    c = read_json(p)
    for key in ('project', 'docker_context', 'env_file', 'compose_files', 'runtime_image', 'source_db', 'snapshot_root', 'forbidden_roots', 'units', 'old_containers', 'sources', 'volumes'):
        if not c.get(key): refuse(f'configuration is missing {key}; name it explicitly before retrying')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]+', c['project']): refuse('project name is invalid; supply an explicit Compose project name')
    for key, value in [('env_file', env_file), ('project', project)]:
        if value is not None and c[key] != value:
            refuse(f'{key} conflicts with the private inventory; use matching explicit inputs')
    environment = {}
    for line in path(c['env_file']).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key.strip()):
            refuse('estate env file contains an invalid assignment; use plain environment assignments')
        # Only the image identity is consumed here; leave the rest of dotenv data
        # (spaces, ${NAME}, and quoted values) to Compose's existing parser.
        if key.strip() == 'FORGE_IMAGE':
            parsed = shlex.split(value, comments=True)
            if len(parsed) != 1: refuse('FORGE_IMAGE is not one explicit image ID; correct the public env file')
            environment[key.strip()] = parsed[0]
    if environment.get('FORGE_IMAGE') != c['runtime_image']:
        refuse('FORGE_IMAGE in the estate env file differs from the immutable migration image; use the same accepted image ID')
    if c['runtime_image'] != RUNTIME:
        refuse('runtime_image is not the accepted immutable migration image; use the reviewed release image ID')
    if set(c['units']) != UNIT_ROLES or set(c['old_containers']) != {'coordinator', 'memory', 'relay'}:
        refuse('stopped-service inventory is incomplete; explicitly name every required unit and old container')
    if len(set(c['units'].values())) != len(UNIT_ROLES): refuse('unit inventory repeats a unit; name every distinct stopped service')
    if set(c['volumes']) != VOLUME_ROLES or len(set(c['volumes'].values())) != 5:
        refuse('volume mapping does not name five distinct state volumes; supply the ledger, settings, evidence, threads and relay progress volumes')
    for name in c['volumes'].values():
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]+', name): refuse('volume name is not a named volume; correct the volume mapping')
    path(c['env_file']); path(c['source_db']); path(c['snapshot_root'])
    for f in c['compose_files']: path(f)
    return c

def docker(c, *args, **kwargs):
    return run(['docker', '--context', c['docker_context'], *args], **kwargs)

def inspect(c, name):
    result = json.loads(docker(c, 'inspect', name).stdout)
    if len(result) != 1: refuse(f'container {name} is ambiguous; name exactly one container')
    return result[0]

def stopped(c):
    observations = {}
    # Validate the complete inventory before asking systemd about any member;
    # a mixed service/timer mapping must not yield partial stopped evidence.
    for role, unit in c['units'].items():
        suffix = '.timer' if role == 'watchdog_timer' else '.service'
        if not isinstance(unit, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@:-]*' + re.escape(suffix), unit):
            refuse(f'unit {unit} has the wrong type for {role}; correct the complete stopped-service inventory')
    for role, unit in sorted(c['units'].items()):
        result = run(['systemctl', '--user', 'show', unit, '--property=LoadState,ActiveState,SubState,MainPID,ControlPID', '--no-pager'])
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        settled = fields.get('LoadState') in ('loaded', 'masked')
        if role == 'watchdog_timer':
            settled = (settled and fields.get('ActiveState') == 'inactive'
                       and fields.get('SubState') == 'dead')
            # systemd timer objects do not define service process fields. Some
            # versions omit them and others render zero; a nonzero value is
            # still evidence of an incoherent observation.
            settled = settled and all(fields.get(key) in (None, '0') for key in ('MainPID', 'ControlPID'))
        else:
            settled = settled and (fields.get('ActiveState'), fields.get('SubState')) in (
                ('inactive', 'dead'),
                ('failed', 'failed'),
            )
            # Missing service PID fields are unknown, not implicit zero.
            settled = settled and fields.get('MainPID') == '0' and fields.get('ControlPID') == '0'
        if not settled:
            refuse(f'unit {unit} is not proved stopped and settled; stop it through the authorized quiesce procedure')
        observations[role] = fields
    containers = {}
    for role, name in c['old_containers'].items():
        item = inspect(c, name)
        if item['State'].get('Status') != 'exited' or item['State'].get('Running') or item['State'].get('Pid') != 0:
            refuse(f'container {name} is not exited; stop it before taking or loading the snapshot')
        containers[role] = item
    return observations, containers

def no_holders(db):
    files = [str(p) for p in (db, Path(str(db)+'-wal'), Path(str(db)+'-shm')) if p.exists()]
    result = run(['fuser', *files], check=False)
    if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
        refuse(f'ledger {db} has an open holder or its holders are unreadable; stop every writer and retry')

def fingerprint(db):
    wal = Path(str(db)+'-wal')
    return {'db_mtime_ns': db.stat().st_mtime_ns, 'db_size': db.stat().st_size, 'wal_size': wal.stat().st_size if wal.exists() else 0}

def previous_runtime(c, item):
    image = json.loads(docker(c, 'image', 'inspect', item['Image']).stdout)[0]
    cfg, host, net = item['Config'], item['HostConfig'], item['NetworkSettings']
    required = ('Mounts', 'Image', 'Name', 'Id')
    if any(k not in item for k in required) or not net.get('Networks'):
        refuse('previous coordinator runtime cannot be recorded completely; repair runtime discovery before retrying')
    return {'format_version': 1, 'image_id': item['Image'], 'repo_tags': image['RepoTags'] or [],
            'mounts': item['Mounts'], 'networks': net['Networks'], 'ports': net.get('Ports', {}),
            'port_bindings': host['PortBindings'], 'restart_policy': host['RestartPolicy'],
            'network_mode': host['NetworkMode'], 'env_names': sorted(x.split('=',1)[0] for x in cfg.get('Env', [])),
            'service_identity': {'name': item['Name'], 'container_id': item['Id'], 'hostname': cfg['Hostname'],
              'user': cfg['User'], 'working_dir': cfg['WorkingDir'], 'entrypoint': cfg['Entrypoint'], 'command': cfg['Cmd']},
            'release_image_id': c['runtime_image']}

def safe_snapshot_destination(c, dest):
    root = path(c['snapshot_root'])
    dest = path(dest, exists=False)
    if dest.parent != root or not re.fullmatch(r'\d{8}T\d{6}Z(?:-[a-zA-Z0-9_-]+)?', dest.name):
        refuse(f'snapshot destination {dest} is not a dated child of the snapshot root; use YYYYMMDDTHHMMSSZ with an optional suffix')
    roots = [path(x) for x in c['forbidden_roots']] + [path(c['source_db']).parent]
    for p in (root, *root.parents):
        if (p / '.git').exists(): refuse(f'snapshot root {root} is inside a repository; choose external storage')
    names = docker(c, 'volume', 'ls', '--format', '{{.Name}}').stdout.splitlines()
    if names:
        roots += [Path(v['Mountpoint']) for v in json.loads(docker(c, 'volume', 'inspect', *names).stdout)]
    if any(root == r or r in root.parents for r in roots):
        refuse(f'snapshot root {root} is inside source, repository or volume storage; choose external storage')
    return dest

def snapshot(c, dest, plan=False):
    dest = safe_snapshot_destination(c, dest)
    db = path(c['source_db'])
    summary = {'source_db': str(db), **fingerprint(db), 'destination': str(dest)}
    if plan: return {'plan': True, **summary, 'actions': ['prove every unit inactive and old container exited', 'check holders and frozen state twice 20 seconds apart', 'sqlite3 -readonly .backup', 'integrity, schema, work state and runtime record']}
    if dest.exists():
        metadata = verify_snapshot(dest)
        if metadata['source_db'] != str(db) or metadata['project'] != c['project']:
            refuse(f'existing snapshot {dest} belongs to another source or project; choose a fresh dated destination')
        return {'idempotent': True, 'snapshot': str(dest), 'sha256': metadata['sha256']}
    units, containers = stopped(c); no_holders(db)
    before = fingerprint(db); state = ledger_state(db)
    time.sleep(20)
    _, current_containers = stopped(c); no_holders(db)
    if any(current_containers[k]['Id'] != v['Id'] for k, v in containers.items()):
        refuse('old container identity changed during the quiet interval; settle the intended runtime before retrying')
    if fingerprint(db) != before or ledger_state(db) != state:
        refuse(f'ledger {db} changed during the quiet interval; settle all writers and retry')
    runtime = previous_runtime(c, containers['coordinator'])
    stage = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=dest.parent))
    try:
        backup = stage / 'forge.db'
        # Destination is generated, never interpolated into a shell; sqlite's dot-command
        # parser accepts double-quoted paths with embedded quotes escaped.
        target = str(backup).replace('"', '""')
        run(['sqlite3', '-readonly', str(db), '.backup "' + target + '"'])
        actual = ledger_state(backup, consolidated=True)
        if actual != state or ledger_state(db) != state or fingerprint(db) != before:
            refuse(f'ledger {db} changed while being backed up; settle every writer and retry')
        no_holders(db); stopped(c)
        atomic_json(stage / 'previous-runtime.json', runtime)
        metadata = {'format_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'project': c['project'], 'source_db': str(db), 'source_size': before['db_size'],
            'source_observation': before, 'sha256': sha256(backup), 'size': backup.stat().st_size,
            'previous_runtime_sha256': sha256(stage / 'previous-runtime.json'), 'units': units, **state}
        atomic_json(stage / 'metadata.json', metadata)
        stage.rename(dest)
        return {'snapshot': str(dest), **metadata}
    finally:
        if stage.exists(): shutil.rmtree(stage)

# Executed only inside the explicitly selected release container. Source folders are
# mounted read-only, destination volumes only at /destination, and networking is off.
MANIFEST_CODE = r'''
import os, pathlib, json, hashlib, sys
root=pathlib.Path(sys.argv[1]); result=[]
for p in sorted(root.rglob('*')):
 if p.is_symlink() or not (p.is_file() or p.is_dir()): raise RuntimeError('nonregular entry')
 if p.is_file():
  with p.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
  result.append({'path':p.relative_to(root).as_posix(),'size':p.stat().st_size,'sha256':digest})
 else: result.append({'path':p.relative_to(root).as_posix()+'/', 'directory':True})
print(json.dumps(result,sort_keys=True))
'''

def manifest(root):
    root = path(root)
    result = []
    for p in sorted(root.rglob('*')):
        path(p)
        if p.is_file(): result.append({'path': p.relative_to(root).as_posix(), 'size': p.stat().st_size, 'sha256': sha256(p)})
        elif p.is_dir(): result.append({'path': p.relative_to(root).as_posix()+'/', 'directory': True})
        else: refuse(f'source {p} is not a regular file or directory; remove unsupported entries before retrying')
    return result

def container_python(c, code, args=(), mounts=(), *, readonly=True):
    argv = ['run', '--rm', '--pull', 'never', '--name', c['project']+'-rollout-'+uuid.uuid4().hex[:12], '--network', 'none', '--read-only', '--user', '0:0', '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'FOWNER', '--cap-add', 'DAC_OVERRIDE', '--tmpfs', '/tmp:rw,noexec,nosuid', '--entrypoint', 'python']
    for mount in mounts: argv += ['--mount', mount]
    return docker(c, *argv, c['runtime_image'], '-c', code, *args).stdout

def compose(c, *args):
    argv = ['compose', '--project-name', c['project'], '--env-file', c['env_file']]
    for f in c['compose_files']: argv += ['-f', f]
    clean_env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'DOCKER_CONFIG', 'XDG_RUNTIME_DIR')}
    return docker(c, *argv, *args, env=clean_env)

def rendered(c):
    model = json.loads(compose(c, 'config', '--format', 'json').stdout)
    expected = c['volumes']['ledger']
    for service in ('coordinator', 'answer-service', 'forge-publisher'):
        mounts = [v for v in model['services'][service].get('volumes', []) if v['target'] == '/var/lib/forge']
        if len(mounts) != 1 or mounts[0]['type'] != 'volume':
            refuse(f'{service} record mount is not one named volume; correct the actual Compose configuration')
        source = mounts[0]['source']
        if model['volumes'][source].get('name') != expected:
            refuse(f'{service} record volume differs from {expected}; correct the actual Compose configuration')
    for service, expected_image in [('coordinator', RUNTIME), ('answer-service', RUNTIME), ('forge-publisher', PUBLISHER_RUNTIME)]:
        if model['services'][service].get('image') != expected_image:
            refuse(f'{service} does not select the accepted immutable image; correct the estate env image ID')
    if model['services']['coordinator'].get('environment', {}).get('FORGE_DB_PATH') != '/var/lib/forge/forge.db':
        refuse('coordinator does not explicitly select the shared ledger file; set FORGE_DB_PATH to /var/lib/forge/forge.db')
    mapping = {'settings': ('coordinator', '/etc/forge'), 'evidence': ('coordinator','/var/lib/forge-evidence'), 'threads': ('front-door','/app/.langgraph_api'), 'relay_progress': ('memory-relay','/var/lib/fleet-memory')}
    for role, (service,target) in mapping.items():
        mounts = [v for v in model['services'][service].get('volumes',[]) if v['target'] == target]
        if len(mounts) != 1 or mounts[0]['type'] != 'volume' or model['volumes'][mounts[0]['source']].get('name') != c['volumes'][role]:
            refuse(f'{service} {role} volume does not match the load destination; correct the Compose volume mapping')
    # An inherited project checkout cannot sneak into the new coordinator.
    if any(v['type'] == 'bind' for v in model['services']['coordinator'].get('volumes', [])):
        refuse('coordinator still binds a host folder; remove legacy project and seed binds before loading')
    return model

def volume_manifest(c, name):
    return json.loads(container_python(c, MANIFEST_CODE, ['/destination'], [f'type=volume,src={name},dst=/destination,readonly']))

def volume_identity(c, name, model):
    """Inspect ownership/configuration before mounting even an empty existing volume."""
    keys = [k for k, v in model['volumes'].items() if v.get('name') == name]
    if len(keys) != 1:
        refuse(f'volume {name} has no unique Compose declaration; correct the volume mapping')
    declared = model['volumes'][keys[0]]
    if declared.get('driver', 'local') != 'local' or declared.get('driver_opts'):
        refuse(f'volume {name} has unsupported storage options; use an ordinary local named state volume')
    item = json.loads(docker(c, 'volume', 'inspect', name).stdout)[0]
    labels = item.get('Labels') or {}
    if (item.get('Name') != name or item.get('Driver') != 'local' or item.get('Scope') != 'local'
            or item.get('Options') or labels.get('com.docker.compose.project') != c['project']
            or labels.get('com.docker.compose.volume') != keys[0]):
        refuse(f'volume {name} ownership or storage configuration differs; select this project\'s correctly labelled local volume')
    return {k: item.get(k) for k in ('Name', 'Driver', 'Scope', 'Options', 'Labels', 'Mountpoint', 'CreatedAt')}

def volume_inventory(c, model=None, identities=None):
    model = model or rendered(c)
    names = set(docker(c, 'volume', 'ls', '--format', '{{.Name}}').stdout.splitlines())
    result = {}
    for role, name in c['volumes'].items():
        if name in names:
            identity = volume_identity(c, name, model)
            if identities is not None: identities[role] = identity
            result[role] = volume_manifest(c, name)
        else:
            result[role] = None
    return result

def validate_receipt(c, metadata, receipt, sources=None):
    required = {'format_version', 'project', 'snapshot_sha256', 'migrated_sha256',
                'runtime_image', 'volumes', 'manifests', 'source_manifests', 'mark', 'container_verification',
                'loaded_logical_sha256', 'startup_logical_sha256'}
    if not isinstance(receipt, dict) or not required <= receipt.keys():
        refuse('load receipt is incomplete; reconcile the occupied state before retrying')
    mark = receipt['mark']
    expected_mark = {'format_version': 1, 'snapshot_sha256': metadata['sha256'],
        'snapshot_created_at': metadata['created_at'], 'migrated_sha256': receipt['migrated_sha256'],
        'source_schema_version': metadata['schema_version'], 'loaded_schema_version': 16,
        'loaded_logical_sha256': receipt['loaded_logical_sha256'], 'startup_logical_sha256': receipt['startup_logical_sha256']}
    if (type(receipt['format_version']) is not int or receipt['format_version'] != 1 or receipt['project'] != c['project']
            or receipt['snapshot_sha256'] != metadata['sha256'] or receipt['runtime_image'] != c['runtime_image']
            or receipt['volumes'] != c['volumes'] or mark != expected_mark
            or not isinstance(receipt['migrated_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', receipt['migrated_sha256'])
            or any(not isinstance(receipt[k], str) or not re.fullmatch('[0-9a-f]{64}', receipt[k])
                   for k in ('loaded_logical_sha256', 'startup_logical_sha256'))
            or set(receipt['manifests']) != VOLUME_ROLES
            or set(receipt['source_manifests']) != VOLUME_ROLES - {'ledger'}):
        refuse('load receipt provenance differs from this snapshot and estate; reconcile it before retrying')
    if sources is not None and sources != receipt['source_manifests']:
        refuse('existing load receipt or destination bytes differ from the sources; reconcile them before retrying')
    for role in VOLUME_ROLES - {'ledger'}:
        if receipt['manifests'][role] != receipt['source_manifests'][role]:
            refuse(f'load receipt {role} manifest differs from its original source; reconcile it before retrying')
    entries = receipt['manifests']['ledger']
    if not isinstance(entries, list) or {x.get('path') for x in entries} != {'forge.db', MARK} or len(entries) != 2:
        refuse('load receipt does not contain the ledger and snapshot mark; perform a verified load before retrying')
    record = next(x for x in entries if x['path'] == 'forge.db')
    marker = next(x for x in entries if x['path'] == MARK)
    marker_bytes = (json.dumps(mark, sort_keys=True, indent=2)+'\n').encode()
    if (record.get('sha256') != receipt['migrated_sha256'] or type(record.get('size')) is not int or record['size'] <= 0
            or marker.get('sha256') != hashlib.sha256(marker_bytes).hexdigest() or marker.get('size') != len(marker_bytes)):
        refuse('load receipt ledger or mark hashes are inconsistent; reconcile it before retrying')
    return mark

VOLUME_ACCESS_CODE = r'''
import pathlib,os,stat,sys,json,sqlite3,hashlib
root=pathlib.Path('/destination'); uid=int(sys.argv[1])
for p in [root,*root.rglob('*')]:
 st=p.lstat()
 if p.is_symlink() or not (p.is_file() or p.is_dir()): raise RuntimeError('nonregular entry')
 if (st.st_uid,st.st_gid,stat.S_IMODE(st.st_mode)) != (uid,uid,0o755 if p.is_dir() else 0o644): raise RuntimeError('ownership or mode differs')
if sys.argv[2]=='ledger':
 p=root/'forge.db'
 if (root/'forge.db-wal').exists() and (root/'forge.db-wal').stat().st_size: raise RuntimeError('unexpected WAL')
 with p.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
 db=sqlite3.connect(p.as_uri()+'?mode=ro&immutable=1',uri=True)
 assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
 version=db.execute('SELECT max(version) FROM schema_version').fetchone()[0];db.close()
 print(json.dumps({'sha256':digest,'schema_version':version,'mark':json.loads((root/'ROLLOUT-SNAPSHOT.json').read_text())}))
else: print('{}')
'''

def verify_loaded(c, receipt, model):
    for role, name in c['volumes'].items():
        volume_identity(c, name, model)
        uid = 10001 if role == 'threads' else (0 if role == 'relay_progress' else 1000)
        try:
            actual = json.loads(container_python(c, VOLUME_ACCESS_CODE, [str(uid), role],
                [f'type=volume,src={name},dst=/destination,readonly']))
        except Refusal:
            refuse(f'volume {name} has unreadable data or incorrect ownership and modes; reconcile it before retrying')
        if role == 'ledger' and actual != {'sha256': receipt['migrated_sha256'], 'schema_version': 16, 'mark': receipt['mark']}:
            refuse(f'volume {name} ledger or snapshot mark differs; reconcile it before retrying')


def invalidate_verification(directory):
    """Clear a safely addressed prior success before any fallible preflight.

    Unparseable JSON/other top-level types cannot advertise a consumable success;
    leave those original bytes intact and refuse. Never follow a receipt symlink.
    """
    directory = path(directory)
    if not directory.is_dir() or not re.fullmatch(r'\d{8}T\d{6}Z(?:-[a-zA-Z0-9_-]+)?', directory.name):
        refuse('verification target is not an explicit dated snapshot directory; select the intended snapshot before invalidating a receipt')
    receipt_path = path(directory / 'load-receipt.json', exists=False)
    if not receipt_path.exists(): return
    if not directory.is_dir() or not receipt_path.is_file():
        refuse('verification receipt is not a regular file in the explicit snapshot directory; select the intended snapshot')
    if receipt_path.stat().st_nlink != 1:
        refuse('verification receipt aliases another file through a hard link; preserve the original inputs and select an unaliased receipt')
    try:
        receipt = json.loads(receipt_path.read_text())
    except (ValueError, UnicodeError):
        refuse('verification receipt is malformed and has no valid attestation; preserve its original bytes and reconcile it')
    if not isinstance(receipt, dict):
        refuse('verification receipt is not an object and has no valid attestation; preserve its original bytes and reconcile it')
    if receipt.get('container_verification') is not None:
        receipt['container_verification'] = None
        atomic_json(receipt_path, receipt)


def retain_migrated_artifact(directory, source):
    """Install a private, non-overwriting exact copy after successful volume load."""
    destination = path(directory / MIGRATED_ARTIFACT, exists=False)
    fd, temporary = tempfile.mkstemp(prefix='.migrated-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as target, open(source, 'rb') as original:
            shutil.copyfileobj(original, target); target.flush(); os.fsync(target.fileno())
        os.link(temporary, destination)  # fails closed if anything already owns this name
        fd = os.open(directory, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        os.unlink(temporary)


def verify_derivation(c, directory, receipt, plan=False):
    artifact = path(directory / MIGRATED_ARTIFACT)
    stat = artifact.stat()
    if not artifact.is_file() or stat.st_uid != os.geteuid() or stat.st_mode & 0o777 != 0o600:
        refuse('retained migrated artifact has incorrect ownership or permissions; reconcile its private original copy')
    if sha256(artifact) != receipt['migrated_sha256']:
        refuse('retained migrated artifact differs from the recorded loaded bytes; reconcile the original migration evidence')
    if ledger_state(artifact, consolidated=True)['schema_version'] != 16:
        refuse('retained migrated artifact has the wrong schema; reconcile the original migration evidence')
    loaded = consolidated_logical_digest(artifact)
    if loaded != receipt['loaded_logical_sha256']:
        refuse('claimed loaded state differs from the retained migrated artifact; reconcile the receipt')
    if plan: return  # preview cannot launch a derivation container or claim fresh proof
    with tempfile.TemporaryDirectory(prefix='.verify-boot-', dir=directory.parent) as temporary:
        temporary = Path(temporary)
        shutil.copyfile(artifact, temporary / 'forge.db')
        container_python(c, BOOT_SQLITE_CODE, mounts=[f'type=bind,src={temporary},dst=/copy'])
        ledger_state(temporary / 'forge.db', consolidated=True)
        startup = consolidated_logical_digest(temporary / 'forge.db')
    if sha256(artifact) != receipt['migrated_sha256']:
        refuse('retained migrated artifact changed during derivation; stop and reconcile the original migration evidence')
    if startup != receipt['startup_logical_sha256']:
        refuse('claimed startup state differs from independently derived normal boot; reconcile the receipt')


def verify_containers(c, directory, metadata, receipt, plan):
    if not plan: invalidate_verification(directory)
    if not plan and receipt['container_verification'] is not None:
        receipt['container_verification'] = None
        atomic_json(directory / 'load-receipt.json', receipt)
    mark = validate_receipt(c, metadata, receipt)
    verify_derivation(c, directory, receipt, plan)
    results = {}
    for service in ('coordinator', 'answer-service', 'forge-publisher'):
        ids = compose(c, 'ps', '--all', '-q', service).stdout.split()
        if len(ids) != 1: refuse(f'{service} has no unique actual container; start the closed-door estate then verify again')
        item = inspect(c, ids[0])
        mounts = [m for m in item['Mounts'] if m['Destination'] == '/var/lib/forge']
        if len(mounts) != 1 or mounts[0]['Type'] != 'volume' or mounts[0].get('Name') != c['volumes']['ledger']:
            refuse(f'{service} actual container mounts another ledger; correct its mount before verifying')
        expected_image = PUBLISHER_RUNTIME if service == 'forge-publisher' else RUNTIME
        if item.get('Image') != expected_image:
            refuse(f'{service} runs a different immutable image; recreate the closed-door service with the accepted release')
        if service != 'coordinator' and mounts[0].get('RW') is not False:
            refuse(f'{service} has a writable ledger mount; restore its read-only mount before verifying')
        if not item['State']['Running']: refuse(f'{service} is not running; start the closed-door estate then verify again')
        if plan: results[service] = 'would read snapshot mark'; continue
        code = ('import pathlib,json,hashlib,sqlite3\n' + python_inspect.getsource(logical_digest) + r'''
p=pathlib.Path('/var/lib/forge/forge.db')
# Open as the configured user even when a main-file digest is no longer the
# current logical state: this still proves actual file access.
with p.open('rb') as f: main_sha=hashlib.file_digest(f,'sha256').hexdigest()
wal=p.with_name(p.name+'-wal');shm=p.with_name(p.name+'-shm')
has_wal=wal.exists() and wal.stat().st_size>0
if has_wal:
    assert shm.is_file(), 'active WAL lacks its shared-memory reader file'
    with shm.open('rb'): pass
uri=p.as_uri()+('?mode=ro' if has_wal else '?mode=ro&immutable=1')
db=sqlite3.connect(uri,uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
version=db.execute('SELECT max(version) FROM schema_version').fetchone()[0]
logical=logical_digest(db);db.close()
# A writer appearing during the consolidated read requires another observation.
assert has_wal or not wal.exists() or wal.stat().st_size==0, 'writer changed the ledger during verification'
print(json.dumps({'main_sha256':main_sha,'logical_sha256':logical,'schema_version':version,
                  'mark':json.loads(p.with_name('ROLLOUT-SNAPSHOT.json').read_text())}))
''')
        # No --user override: execute as the actual configured service user.
        try:
            text = docker(c, 'exec', ids[0], 'python', '-c', code).stdout
        except Refusal:
            refuse(f'{service} cannot read its actual ledger and snapshot mark as its configured user; fix access before verifying')
        actual = json.loads(text)
        if (actual.get('schema_version') != 16 or actual.get('mark') != mark
                or actual.get('logical_sha256') not in {receipt['loaded_logical_sha256'], receipt['startup_logical_sha256']}):
            refuse(f'{service} reads a different ledger or snapshot mark; keep the door closed and reconcile its state')
        if results and actual['logical_sha256'] != next(iter(results.values()))['logical_sha256']:
            refuse('the three services observed different ledger states; keep the door closed and repeat verification after startup settles')
        results[service] = {'container_id': item['Id'], 'snapshot_sha256': mark['snapshot_sha256'],
                            'logical_sha256': actual['logical_sha256'], 'main_sha256': actual['main_sha256']}
    if not plan:
        receipt['container_verification'] = {'verified_at': datetime.now(timezone.utc).isoformat(), 'services': results}
        atomic_json(directory / 'load-receipt.json', receipt)
    return {'plan': plan, 'container_verification': results}

def load_volumes(c, directory, plan=False, verify=False):
    directory = path(directory)
    if verify and not plan: invalidate_verification(directory)
    metadata = verify_snapshot(directory)
    if metadata['project'] != c['project']: refuse('snapshot belongs to another Compose project; select its matching project')
    model = rendered(c)
    if verify:
        return verify_containers(c, directory, metadata, read_json(directory / 'load-receipt.json'), plan)
    sources = c['sources']
    if set(sources) != {'settings', 'evidence', 'threads', 'relay_progress'}:
        refuse('source mapping is incomplete; explicitly name all four source stores')
    for p in sources.values(): path(p)
    if Path(sources['relay_progress']).name != 'relay-progress.json': refuse('relay source is not relay-progress.json; name only the relay progress file')
    # Planning performs no helper-container, receipt-reader probe, mkdir or volume creation.
    if plan:
        return {'plan': True, 'snapshot_sha256': metadata['sha256'], 'source_schema': metadata['schema_version'], 'volumes': c['volumes'], 'actions': ['migrate a disposable copy with accepted runtime', 'derive complete loaded and normal SQLite boot states', 'refuse occupied volumes unless every byte matches the existing receipt', 'copy and compare every filename, size and SHA-256', 'retain private migrated-forge.db beside the original snapshot', 'verify real service snapshot marks later with --verify-containers'], 'not_copied': ['retained bus', 'Postgres', 'project and seed folders', 'chronicler and liveness files', 'gateway heartbeat']}
    stopped(c)
    source_manifests = {role: manifest(sources[role]) for role in ('evidence', 'threads')}
    for role, filename in [('settings', 'forge.yaml'), ('relay_progress', 'relay-progress.json')]:
        p = path(sources[role])
        if not p.is_file(): refuse(f'{role} source {p} is not a regular file; supply the intended file')
        source_manifests[role] = [{'path': filename, 'size': p.stat().st_size, 'sha256': sha256(p)}]
    # Refuse any container already using a destination, including stopped containers:
    # the tool has no authority to choose which one will next become a writer.
    for role,name in c['volumes'].items():
        if docker(c, 'ps', '--all', '--filter', 'volume='+name, '--format', '{{.ID}}').stdout.strip():
            refuse(f'volume {name} is attached to a container; keep the estate stopped and remove its old attachment before loading')
    receipt_path = directory / 'load-receipt.json'
    identities = {}
    inventory = volume_inventory(c, model, identities)
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        validate_receipt(c, metadata, receipt, source_manifests)
        verify_derivation(c, directory, receipt)
        if receipt['container_verification'] is not None:
            receipt['container_verification'] = None
            atomic_json(receipt_path, receipt)
        if receipt['manifests'] != inventory:
            refuse('existing load receipt or destination bytes differ; reconcile the occupied state before retrying')
        verify_loaded(c, receipt, model)
        return {'idempotent': True, 'snapshot_sha256': metadata['sha256'], 'container_verification': None,
                'next_action': 'run --verify-containers against the actual closed-door services'}
    if path(directory / MIGRATED_ARTIFACT, exists=False).exists():
        refuse('a retained migrated artifact exists without a load receipt; preserve it and reconcile the interrupted load')
    if any(items for items in inventory.values()): refuse('a destination volume is occupied without a matching verified receipt; select empty new volumes')
    with tempfile.TemporaryDirectory(prefix='.load-', dir=directory.parent) as temporary:
        temp = Path(temporary)
        for role in VOLUME_ROLES: (temp / role).mkdir()
        ledger = temp / 'ledger' / 'forge.db'
        shutil.copyfile(directory / 'forge.db', ledger)
        migrate = 'import sqlite3; from forge.lifecycle.migrations import apply_at_boot; c=sqlite3.connect("/copy/forge.db", isolation_level=None); apply_at_boot(c); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()'
        try:
            container_python(c, migrate, mounts=[f'type=bind,src={temp / "ledger"},dst=/copy'])
        except Refusal:
            refuse(f'migration of a disposable copy from {directory} failed; keep services stopped and reconcile the source schema before retrying')
        migrated = ledger_state(ledger, consolidated=True)
        if migrated['schema_version'] != 16: refuse('disposable migration did not reach schema 16; repair the selected runtime before retrying')
        if sha256(directory / 'forge.db') != metadata['sha256']: refuse('snapshot changed during disposable migration; stop and recover the original snapshot')
        loaded_logical = consolidated_logical_digest(ledger)
        startup_copy = temp / 'startup'; startup_copy.mkdir()
        shutil.copyfile(ledger, startup_copy / 'forge.db')
        try:
            container_python(c, BOOT_SQLITE_CODE, mounts=[f'type=bind,src={startup_copy},dst=/copy'])
        except Refusal:
            refuse(f'normal SQLite startup on a disposable copy from {directory} failed; keep services stopped and reconcile the source before retrying')
        ledger_state(startup_copy / 'forge.db', consolidated=True)
        startup_logical = consolidated_logical_digest(startup_copy / 'forge.db')
        mark = {'format_version': 1, 'snapshot_sha256': metadata['sha256'], 'snapshot_created_at': metadata['created_at'],
                'migrated_sha256': sha256(ledger), 'source_schema_version': metadata['schema_version'], 'loaded_schema_version': 16,
                'loaded_logical_sha256': loaded_logical, 'startup_logical_sha256': startup_logical}
        atomic_json(temp / 'ledger' / MARK, mark)
        shutil.copyfile(sources['settings'], temp / 'settings' / 'forge.yaml')
        shutil.copyfile(sources['relay_progress'], temp / 'relay_progress' / 'relay-progress.json')
        for role in ('evidence', 'threads'):
            original = manifest(sources[role])
            shutil.copytree(sources[role], temp / role, dirs_exist_ok=True)
            if manifest(temp / role) != original or manifest(sources[role]) != original:
                refuse(f'{role} source changed while copied; settle its writer and retry')
        expected = {role: manifest(temp / role) for role in VOLUME_ROLES}
        if any(expected[role] != source_manifests[role] for role in sources):
            refuse('a source changed during the copy; settle all source writers and retry')
        for role in ('settings', 'relay_progress'):
            if sha256(sources[role]) != source_manifests[role][0]['sha256']:
                refuse(f'{role} changed during the copy; settle its writer and retry')
        created = []
        try:
            for role,name in c['volumes'].items():
                # Empty existing volumes may be loaded, but are never deleted on failure.
                if inventory[role] is None:
                    compose_key = next(key for key, spec in model['volumes'].items() if spec.get('name') == name)
                    docker(c, 'volume', 'create', '--label', 'com.docker.compose.project='+c['project'], '--label', 'com.docker.compose.volume='+compose_key, '--label', 'rollout.snapshot='+metadata['sha256'], name)
                    created.append(name)
                current_identity = volume_identity(c, name, model)
                if role in identities and identities[role] != current_identity:
                    refuse(f'volume {name} was replaced during loading; keep services stopped and reconcile its ownership')
                uid = 10001 if role == 'threads' else (0 if role == 'relay_progress' else 1000)
                code = 'import pathlib,shutil,os; s=pathlib.Path("/source"); d=pathlib.Path("/destination"); assert not list(d.iterdir()), "occupied"; shutil.copytree(s,d,dirs_exist_ok=True); uid='+str(uid)+'; [(os.chown(p,uid,uid),os.chmod(p,0o755 if p.is_dir() else 0o644)) for p in [d,*d.rglob("*")]]; [(os.fsync(f.fileno())) for p in d.rglob("*") if p.is_file() for f in [p.open("rb")]]; fd=os.open(d,os.O_DIRECTORY); os.fsync(fd); os.close(fd)'
                container_python(c, code, mounts=[f'type=bind,src={temp / role},dst=/source,readonly', f'type=volume,src={name},dst=/destination'])
                if volume_manifest(c, name) != expected[role]: refuse(f'volume {name} differs from its source manifest; keep services stopped and reconcile the copy')
            receipt = {'format_version': 1, 'project': c['project'], 'snapshot_sha256': metadata['sha256'], 'migrated_sha256': mark['migrated_sha256'], 'runtime_image': c['runtime_image'], 'volumes': c['volumes'], 'manifests': expected, 'source_manifests': source_manifests, 'mark': mark, 'loaded_logical_sha256': loaded_logical, 'startup_logical_sha256': startup_logical, 'container_verification': None}
            validate_receipt(c, metadata, receipt, source_manifests)
            verify_loaded(c, receipt, model)
            retain_migrated_artifact(directory, ledger)
            atomic_json(receipt_path, receipt)
        except BaseException:
            for name in reversed(created): docker(c, 'volume', 'rm', name, check=False)
            raise
    return {'loaded': True, 'snapshot_sha256': metadata['sha256'], 'migrated_sha256': mark['migrated_sha256'], 'container_verification': 'NOT-YET: run --verify-containers after closed-door bring-up', 'not_copied': ['retained bus', 'Postgres', 'project and seed folders', 'chronicler and liveness files', 'gateway heartbeat']}

def main_guard(function):
    try:
        result = function()
        print(json.dumps(result, sort_keys=True, indent=2)); return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, Refusal) else 'an input or required operation could not be read safely; check the explicit paths and private service diagnostics before retrying'
        print('Refused: ' + message.rstrip('.') + '.', file=sys.stderr)
        return 2

class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Refused: required command arguments are missing or invalid; run --help and supply explicit inputs.\n')

def cli(command):
    parser = Parser(description='Prepare durable rollout state without starting application services.')
    parser.add_argument('--config', required=True, help='absolute JSON configuration; no implicit machine defaults')
    parser.add_argument('--snapshot', required=True, help='absolute dated snapshot directory')
    parser.add_argument('--env-file', help='explicit estate env file; must match inventory')
    parser.add_argument('--project', help='explicit Compose project; must match inventory')
    parser.add_argument('--plan', action='store_true', help='read-only preview, creating no files, containers or volumes')
    if command == 'load': parser.add_argument('--verify-containers', action='store_true', help='read the snapshot mark in all three actual running services')
    args = parser.parse_args()
    def execute():
        if command == 'load' and args.verify_containers and not args.plan:
            invalidate_verification(args.snapshot)
        c = config(args.config, args.env_file, args.project)
        return snapshot(c, args.snapshot, args.plan) if command == 'snapshot' else load_volumes(c, args.snapshot, args.plan, args.verify_containers)
    return main_guard(execute)
