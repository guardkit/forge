"""Upgrade modes of rollout-quiesce for an estate that is already resumed (TC2).

Release -3 upgrade runbook, 2 October 2026: close, settle, stop, bring release X
up with the door shut, check it, open. The real tools run against the fake
machine in upgrade_world.py; nothing here reaches Docker, a sandbox or a bus.
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from .upgrade_world import (PREFIX, PROJECT, PROVISION, SANDBOX, SCRIPT, SETTINGS_V2, SETTINGS_V3, V2, V3, V3_ENTRY,
                            VOLUME_KEYS, World, stamp)

HERE = Path(__file__).resolve().parents[3] / 'deploy/estate'


def module(name):
    loader = importlib.machinery.SourceFileLoader('upgrade_' + name.replace('-', '_'), str(HERE / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


b = module('rollout-back')
q = b.q
r = q.r


def private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text); path.chmod(0o600); return path


def env_text(release, values):
    entry = r.RELEASES[release]
    lines = {'FORGE_IMAGE': entry['runtime'], 'FORGE_PUBLISHER_IMAGE': entry['publisher'], 'FLEET_MEMORY_MCP_IMAGE': entry['memory'],
             'FLEET_MEMORY_RELAY_IMAGE': entry['relay'], 'JARVIS_IMAGE': entry['jarvis'], 'NATS_PROVISION_IMAGE': PROVISION,
             'BUS_MODE': 'external', 'BUS_EXTERNAL_NETWORK': 'none', 'FORGE_NATS_URL': 'nats://fake.invalid:14222',
             'BUS_MONITORING_ADDRESS': 'fake.invalid:18222', 'ROLLOUT_BUS_STREAM': 'PIPELINE',
             'ROLLOUT_BUS_CONSUMERS': 'forge-serve forge-serve-planning', 'JARVIS_NATS_USER': 'jarvis', 'FACTORY_INSTANCE': 'fixture',
             'SANDBOX_NAME': SANDBOX, 'SANDBOX_BOOTSTRAP': SCRIPT, 'COMPOSE_PROFILES': 'sandbox', 'SLACK_BOT_TOKEN': '${PRIVATE_TOKEN}', **values}
    return ''.join(f'{k}={v}\n' for k, v in lines.items())


@pytest.fixture
def up(tmp_path, monkeypatch):
    """Two inventories (release -2 and release -3) for one resumed estate, and its machine."""
    monkeypatch.setitem(r.RELEASES, V3, dict(V3_ENTRY))
    w = World(tmp_path, r, q)
    w.publisher_settings = tmp_path / 'publisher.json'; w.publisher_settings.write_text('{"host":"0.0.0.0","port":8711}')
    root = tmp_path / 'snapshots'; root.mkdir()
    authority = root / '20260930T204016Z-live-step1'; authority.mkdir()
    r.atomic_json(authority / 'metadata.json', {'sha256': 'a' * 64})
    marker = {'format_version': 1, 'project': PROJECT, 'snapshot': str(authority), 'snapshot_sha256': 'a' * 64, 'release_tag': 'forge:' + V2,
              'image_id': r.RELEASES[V2]['runtime'], 'actor': 'fixture', 'at': '2026-09-30T21:00:00+00:00', 'work_state': {}, 'close_receipt_sha256': 'b' * 64}
    r.atomic_json(authority / 'resumed.json', marker); w.marker = marker
    usnap = root / '20261002T080000Z-upgrade-r3'; usnap.mkdir()
    doors = {V2: usnap / 'closed-door-release-2', V3: usnap / 'closed-door-release-3'}
    for d in doors.values(): d.mkdir(mode=0o700)
    switch_receipt = private(tmp_path / 'run-1' / 'private-runtime' / 'quiesce-close.json', '{"format_version": 1, "stage": "resumed"}\n')
    bootstrap = {V2: private(tmp_path / 'run-1' / 'private-runtime' / 'sandbox-bootstrap.env', f'FORGE_IMAGE=forge:{V2}\nFORGE_CONFIG_PATH={SETTINGS_V2}\nSANDBOX_CONTAINER_PREFIX={PREFIX}\nGIT_AUTHOR_NAME=private-name\n'),
                 V3: private(tmp_path / 'upr' / 'sandbox-bootstrap.env', f'FORGE_IMAGE=forge:{V3}\nFORGE_CONFIG_PATH={SETTINGS_V3}\nSANDBOX_CONTAINER_PREFIX={PREFIX}\nGIT_AUTHOR_NAME=private-name\n')}
    envs = {V2: tmp_path / 'upr' / 'original' / 'estate-2.env', V3: tmp_path / 'upr' / 'prepared' / 'estate-3.env'}
    for release, path in envs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(env_text(release, {'FORGE_PUBLISHER_SETTINGS_FILE': str(w.publisher_settings), 'ROLLOUT_STATE_DIR': str(doors[release]),
                                           'SANDBOX_PROJECT_ENV_FILE': str(bootstrap[release])}))
    private_env = private(tmp_path / 'upr' / 'secrets.env', 'PRIVATE_TOKEN=sentinel-private-value\n')
    composes = {V2: tmp_path / 'op3' / 'compose.yaml', V3: tmp_path / 'opt' / 'compose.yaml'}
    for release, path in composes.items():
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(f'# compose for {release}\n')
    evidence = tmp_path / 'upr' / 'sandbox-evidence'
    side = lambda release: {'release': release, 'env_file': str(envs[release]), 'env_sha256': r.sha256(envs[release]),
                            'compose_files': [{'path': str(composes[release]), 'sha256': r.sha256(composes[release])}]}
    block = {'from': side(V2), 'to': side(V3), 'receipt': str(tmp_path / 'upr' / 'upgrade-receipt.json'), 'snapshot': str(usnap),
             'authority_snapshot': str(authority), 'sandbox_previous_receipt': str(tmp_path / 'run-1' / 'private-runtime' / 'sandbox-evidence' / 'sandbox-installed.json')}
    inventories = {}
    for release in (V2, V3):
        c = {'project': PROJECT, 'release': release, 'runtime_image': r.RELEASES[release]['runtime'], 'docker_context': 'default',
             'env_file': str(envs[release]), 'compose_files': [str(composes[release])], 'snapshot_root': str(root),
             'volumes': {role: PROJECT + '_' + key for role, key in VOLUME_KEYS.items()},
             'quiesce': {'receipt': str(switch_receipt), 'release_tag': 'forge:' + V2, 'actor': 'fixture'},
             'sandbox': {'evidence_dir': str(evidence if release == V3 else tmp_path / 'run-1' / 'private-runtime' / 'sandbox-evidence'), 'name': SANDBOX},
             'upgrade': dict(block, closed_door_dir=str(doors[release]))}
        inventories[release] = tmp_path / 'upr' / f'inventory-{release}.json'; r.atomic_json(inventories[release], c)
    clock = [0.0]
    monkeypatch.setattr(q, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(r, 'run', w.run)
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    w.start_estate(V2)

    def args(release, **extra):
        return SimpleNamespace(plan=False, candidate_image=None, candidate_env_file=None, mode=None, secret_env_file=[str(private_env)],
                               config=str(inventories[release]), env_file=str(envs[release]), project=PROJECT, snapshot=str(usnap), **extra)

    def estate(release):
        return q.Estate(args(release))

    return SimpleNamespace(world=w, estate=estate, args=args, envs=envs, doors=doors, usnap=usnap, authority=authority,
                           switch_receipt=switch_receipt, receipt=Path(block['receipt']), evidence=evidence, bootstrap=bootstrap,
                           inventories=inventories, composes=composes, private_env=private_env, clock=clock)


def shut(up):
    """U1-U3 with the release -2 inventory."""
    for mode in ('close', 'settle', 'final'):
        getattr(up.estate(V2), mode)()


def forward_to_switch(up):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3])
    return up.estate(V3).switch()


def running_services(world):
    return {c['service']: c['release'] for c in world.containers.values() if c['State']['Running']}


def stopped_door(world):
    return not any(world.running(s) for s in ('front-door', 'bus-gateway', 'gateway-watch'))


def planning(world):
    return yaml.safe_load(world.settings.read_text())['planning']['enabled']


def events_since(world, mark, kind=None):
    return [e for e in world.events[mark:] if kind is None or e[0] == kind]


# --------------------------------------------------------------------- inputs

def test_upgrade_inventory_must_name_its_release_and_its_env_must_match_the_block(up):
    c = r.read_json(up.inventories[V3]); del c['release']; r.atomic_json(up.inventories[V3], c)
    with pytest.raises(r.Refusal, match='names its own release'):up.estate(V3)
    c['release'] = V3; r.atomic_json(up.inventories[V3], c)
    up.envs[V3].write_text(up.envs[V3].read_text() + 'EXTRA=1\n')
    with pytest.raises(r.Refusal, match='no longer have the SHA-256'):up.estate(V3)


def test_each_release_keeps_its_closed_door_receipt_in_its_own_folder(up):
    c = r.read_json(up.inventories[V2]); c['upgrade']['closed_door_dir'] = str(up.doors[V3]); r.atomic_json(up.inventories[V2], c)
    with pytest.raises(r.Refusal, match='own folder'):up.estate(V2)


def test_upgrade_inventories_refuse_the_legacy_modes(up):
    for mode in ('resume', 'reopen'):
        with pytest.raises(r.Refusal, match='an upgrade uses --close, --settle, --final, --switch and --open'):getattr(up.estate(V2), mode)()


# --------------------------------------------------------------------- --close

def test_initial_close_with_markers_writes_a_new_receipt_and_stops_only_the_watch_and_producers(up):
    w = up.world; before = set(w.containers); mark = len(w.events)
    result = up.estate(V2).close()
    doc = r.read_json(up.receipt)
    assert result['closed'] and doc['kind'] == 'upgrade' and doc['stage'] == 'closed'
    assert {x['service'] for x in doc['containers'].values()} == {'coordinator', 'answer-service', 'memory', 'memory-relay', 'forge-publisher', 'sandbox-runner', 'bus-ready', 'front-door', 'bus-gateway', 'gateway-watch'}
    assert set(doc['containers']) == before
    assert sorted(e[1] for e in events_since(w, mark, 'stop')) == ['bus-gateway', 'front-door', 'gateway-watch']
    assert running_services(w).keys() == {'coordinator', 'answer-service', 'memory', 'memory-relay', 'forge-publisher', 'sandbox-runner'}
    assert doc['settings']['planning_enabled'] is True and len(doc['settings']['sha256']) == 64 and len(doc['settings']['canonical_sha256']) == 64
    assert len(doc['volumes']) == 9
    assert doc['sandbox'][V2]['template_sha256'] == r.hashlib.sha256(w.templates[V2]).hexdigest()
    assert doc['sources']['from']['env_sha256'] == r.sha256(up.envs[V2])
    assert up.receipt.stat().st_mode & 0o777 == 0o600


def test_close_never_writes_the_switch_receipt_or_the_markers(up):
    receipt, marker = up.switch_receipt.read_bytes(), (up.authority / 'resumed.json').read_bytes()
    forward_to_switch(up)
    up.world.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'])
    up.estate(V3).open()
    assert up.switch_receipt.read_bytes() == receipt
    assert (up.authority / 'resumed.json').read_bytes() == marker and up.world.marker == r.read_json(up.authority / 'resumed.json')
    assert not (up.usnap / 'resumed.json').exists() and not (up.usnap / 'ROLLOUT-RESUMED').exists()


def test_an_estate_with_no_markers_is_not_upgraded(up):
    up.world.marker = None; (up.authority / 'resumed.json').unlink()
    with pytest.raises(r.Refusal, match='never resumed'):up.estate(V2).close()
    assert not up.receipt.exists() and not stopped_door(up.world)


def test_a_marker_naming_another_image_refuses(up):
    marker = dict(up.world.marker, image_id='sha256:' + 'e' * 64); up.world.marker = marker; r.atomic_json(up.authority / 'resumed.json', marker)
    with pytest.raises(r.Refusal, match='not in the reviewed table, or another image'):up.estate(V2).close()


def test_initial_close_needs_the_inventory_of_the_running_release(up):
    with pytest.raises(r.Refusal, match='close with its inventory'):up.estate(V3).close()
    assert not up.receipt.exists()


def test_a_settings_file_not_in_the_writers_form_refuses_before_anything_stops(up):
    up.world.settings.write_text('# hand-written\nplanning: {enabled: true}\nroutine: {seat: fixture-seat}\n')
    with pytest.raises(r.Refusal, match='not in the form the planning switch writes'):up.estate(V2).close()
    assert not up.receipt.exists() and not stopped_door(up.world)


# --------------------------------------------------------------------- --final

def test_final_stops_the_supervisor_first_then_writes_the_backup_and_turns_planning_off(up):
    w = up.world; e = up.estate(V2); e.close(); e = up.estate(V2); e.settle(); mark = len(w.events)
    final = up.estate(V2).final()
    stops = [e[1] for e in events_since(w, mark, 'stop')]
    assert stops[0] == 'sandbox-runner' and set(stops) == {'sandbox-runner', *q.SERVICES} - {'front-door', 'bus-gateway'}
    assert not running_services(w)
    backup = Path(final['backup']['path'])
    assert backup == up.usnap / 'current-forge.db' and backup.stat().st_mode & 0o777 == 0o600
    assert final['backup']['sha256'] == r.sha256(backup) and final['backup']['ledger_state']['schema_version'] == 16
    assert final['backup']['logical_sha256'] == final['state']['logical_sha256']
    assert planning(w) is False and ('planning', False, r.RELEASES[V2]['runtime']) in w.events
    assert r.read_json(up.receipt)['stage'] == 'final'


def test_final_refuses_when_the_supervisor_exits_3_and_stops_nothing_else(up):
    w = up.world; up.estate(V2).close(); up.estate(V2).settle(); w.supervisor_stop_exit = 3
    with pytest.raises(r.Refusal, match='sandbox-runner exited 3: its stop of the work inside the sandbox did not succeed; and inside the sandbox there is still '+PREFIX+'-helper, '+PREFIX+'-runner'):up.estate(V2).final()
    assert running_services(w).keys() == {'coordinator', 'answer-service', 'memory', 'memory-relay', 'forge-publisher'}
    assert planning(w) is True and not (up.usnap / 'current-forge.db').exists()


@pytest.mark.parametrize('code', [1, 2, 137])
def test_a_supervisor_that_exited_non_zero_with_nothing_inside_does_not_block_final(up, code):
    # Coach S2/S3b: a supervisor that crashed or refused at start must not block every
    # way forward and back, once it is proved nothing runs inside the sandbox.
    w = up.world; w.stop(w.running('sandbox-runner')[0], code)
    up.estate(V2).close(); up.estate(V2).settle()
    final = up.estate(V2).final()
    assert final['supervisor']['exit'] == code and 'nothing runs inside the sandbox' in final['supervisor']['accepted_because']
    if code == 2:assert final['supervisor']['meaning'].startswith('it refused before starting anything')
    assert not running_services(w) and planning(w) is False


@pytest.mark.parametrize('inside', ['record_live', 'lock_held'])
def test_a_non_zero_supervisor_with_a_live_supervisor_inside_still_refuses(up, inside):
    w = up.world; w.stop(w.running('sandbox-runner')[0], 1); w.supervision[inside] = True
    up.estate(V2).close(); up.estate(V2).settle()
    with pytest.raises(r.Refusal, match='sandbox-runner exited 1: it ended without its own clean stop .* nothing else was stopped'):up.estate(V2).final()
    assert running_services(w)['coordinator'] == V2


def test_a_second_final_never_overwrites_the_first_backup(up):
    shut(up); first = (up.usnap / 'current-forge.db').read_bytes()
    second = up.estate(V2).final()
    assert (up.usnap / 'current-forge.db').read_bytes() == first and Path(second['backup']['path']).name.startswith('current-forge-2')


# --------------------------------------------------------------------- --switch

def test_switch_brings_release_3_up_with_the_door_shut_and_planning_off(up):
    w = up.world; shut(up); w.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3]); mark = len(w.events)
    result = up.estate(V3).switch()
    assert result['passed'] and running_services(w) == {s: V3 for s in ('coordinator', 'answer-service', 'memory', 'memory-relay', 'forge-publisher', 'sandbox-runner')}
    assert stopped_door(w) and not [e for e in events_since(w, mark, 'up') if e[1] in ('front-door', 'bus-gateway', 'gateway-watch')]
    bus = w.of('bus-ready')[0]; assert bus['State']['Status'] == 'exited' and bus['State']['ExitCode'] == 0 and bus['Image'] == PROVISION
    assert planning(w) is False
    ups = events_since(w, mark)
    planning_at = ups.index(('planning', False, V3_ENTRY['runtime'])); coordinator_at = ups.index(('up', 'coordinator', V3))
    assert planning_at < coordinator_at and ('up', 'coordinator', V3) not in ups[:planning_at]
    assert q.docker_time(result['coordinator']['started_at']) > q.datetime.fromisoformat(result['coordinator']['planning_written_at'])
    assert w.inner == {PREFIX + '-helper': 'forge:' + V3, PREFIX + '-runner': 'forge:' + V3}
    assert result['readers']['reconciled'] == [] and set(result['readers']['services']) == set(q.LEDGER_READERS)
    assert ('policy-verify', True, 'estate-3.env') in w.events and ('settings-load', V3_ENTRY['runtime']) in w.events
    removed = {e[1] for e in events_since(w, mark, 'rm')}
    assert removed == {'coordinator', 'answer-service', 'memory', 'memory-relay', 'forge-publisher', 'sandbox-runner', 'front-door', 'bus-gateway'}
    saved = r.read_json(up.usnap / ('switched-' + V3 + '.json')); assert saved['passed'] and saved['bus_ready']['id'] == bus['Id']


def test_switch_to_release_3_needs_final_first(up):
    up.estate(V2).close(); up.estate(V2).settle()
    with pytest.raises(r.Refusal, match='only after --final'):up.estate(V3).switch()


def test_switch_refuses_an_image_not_in_the_table(up, monkeypatch):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3]); mark = len(up.world.events)
    model = up.world.model
    def changed(release, profiles):
        m = model(release, profiles); m['services']['memory']['image'] = 'sha256:' + 'f' * 64; return m
    monkeypatch.setattr(up.world, 'model', changed)
    with pytest.raises(r.Refusal, match="memory does not render release .* image from the reviewed table"):up.estate(V3).switch()
    assert not events_since(up.world, mark, 'up') and not events_since(up.world, mark, 'rm')


def test_switch_refuses_a_changed_volume(up):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3])
    up.world.volumes[PROJECT + '_forge-home']['CreatedAt'] = '2026-10-02T09:00:00Z'
    with pytest.raises(r.Refusal, match='forge-home is not the one recorded at --close'):up.estate(V3).switch()
    assert stopped_door(up.world) and planning(up.world) is False


def interrupted_to_failed(world, reason='sandbox-required: the build ran outside a sandbox'):
    def start(release):
        if release == V3:
            with r.sqlite3.connect(world.ledger) as db:
                db.execute("UPDATE builds SET status='FAILED', error=?, completed_at='2026-10-02' WHERE build_id='b-interrupted'", (reason,))
    world.on_coordinator_start = start


def test_switch_accepts_exactly_the_start_up_reconciliation(up):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3]); interrupted_to_failed(up.world)
    assert up.estate(V3).switch()['readers']['reconciled'] == ['b-interrupted']


@pytest.mark.parametrize('change', ['another-reason', 'another-table'])
def test_switch_refuses_a_reader_digest_that_is_neither_recorded_nor_a_reconciliation(up, change):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3])
    if change == 'another-reason':
        interrupted_to_failed(up.world, 'operator cancelled')
    else:
        def start(release):
            with r.sqlite3.connect(up.world.ledger) as db:db.execute("INSERT INTO work_queue VALUES ('w','CLOSED','c',1,NULL,NULL)")
        up.world.on_coordinator_start = start
    with pytest.raises(r.Refusal, match='keep the door closed|keep the door shut'):up.estate(V3).switch()
    assert stopped_door(up.world) and planning(up.world) is False
    assert not r.read_json(up.usnap / ('switched-' + V3 + '.json'))['passed']


def test_switch_never_starts_sandbox_runner_when_the_clones_template_is_not_the_releases(up):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3]); up.world.restore_release_2_in_sandbox(); mark = len(up.world.events)
    with pytest.raises(r.Refusal, match="the clone's bootstrap does not have the SHA-256 recorded for release"):up.estate(V3).switch()
    assert ('up', 'sandbox-runner', V3) not in up.world.events[mark:] and not up.world.running('sandbox-runner')
    assert stopped_door(up.world)


def test_switch_refuses_a_settings_change_other_than_planning_before_starting_anything(up):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3]); mark = len(up.world.events)
    data = yaml.safe_load(up.world.settings.read_text()); data['routine']['seat'] = 'another-seat'
    up.world.settings.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(r.Refusal, match='differs from the one recorded at --close in more than planning.enabled'):up.estate(V3).switch()
    assert not events_since(up.world, mark, 'up') and not events_since(up.world, mark, 'rm') and not running_services(up.world)


def test_switch_accepts_the_settings_file_changed_only_in_planning(up):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3])
    assert planning(up.world) is False and r.read_json(up.receipt)['settings']['planning_enabled'] is True
    assert up.estate(V3).switch()['passed']


@pytest.mark.parametrize('polls', [0, 3])
def test_switch_waits_for_a_running_bus_ready_and_accepts_its_exit_0(up, polls):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3]); up.world.bus_ready_polls = polls
    assert up.estate(V3).switch()['passed'] and up.world.of('bus-ready')[0]['State']['ExitCode'] == 0


@pytest.mark.parametrize('release', [V3, V2])
def test_switch_refuses_a_bus_ready_that_exits_non_zero_with_nothing_opened(up, release):
    shut(up)
    if release == V3:up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3])
    up.world.bus_ready_exit = 1; mark = len(up.world.events)
    with pytest.raises(r.Refusal, match='bus-ready exited 1'):up.estate(release).switch()
    assert stopped_door(up.world) and not [e for e in events_since(up.world, mark, 'up') if e[1] not in ('bus-ready',)]


def test_switch_refuses_a_bus_ready_still_running_at_its_limit(up):
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3]); up.world.bus_ready_polls = 10 ** 6
    with pytest.raises(r.Refusal, match='bus-ready was still running after'):up.estate(V3).switch()
    assert stopped_door(up.world)


def test_cancel_after_final_brings_release_2_back_with_the_door_shut(up):
    w = up.world; shut(up); mark = len(w.events)
    result = up.estate(V2).switch()
    assert result['passed'] and running_services(w) == {s: V2 for s in ('coordinator', 'answer-service', 'memory', 'memory-relay', 'forge-publisher', 'sandbox-runner')}
    assert not events_since(w, mark, 'rm') and stopped_door(w) and planning(w) is False
    assert w.inner == {PREFIX + '-helper': 'forge:' + V2, PREFIX + '-runner': 'forge:' + V2}


def test_cancel_after_close_only_compares_against_the_ledger_at_its_start(up):
    up.estate(V2).close(); result = up.estate(V2).switch()
    assert result['passed'] and Path(result['baseline']['path']).name.startswith('switch-start-' + V2)
    assert stopped_door(up.world) and planning(up.world) is False


def test_switch_from_release_2_refuses_while_release_3_runs(up):
    forward_to_switch(up)
    with pytest.raises(r.Refusal, match='use rollout-back --upgrade-back'):up.estate(V2).switch()
    assert running_services(up.world)['coordinator'] == V3


# --------------------------------------------------------------------- --open

def test_open_after_switch_and_a_passed_receipt_opens_the_door(up):
    w = up.world; forward_to_switch(up); w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime']); mark = len(w.events)
    result = up.estate(V3).open()
    assert result['passed'] and result['markers'] == 'untouched'
    order = [e for e in events_since(w, mark) if e[0] in ('read-pre-resume', 'planning', 'up', 'estate-check', 'factory-hello')]
    assert order[0] == ('read-pre-resume', 'closed-door-release-3', V3_ENTRY['runtime'])
    assert order[1:] == [('planning', True, V3_ENTRY['runtime']), ('up', 'coordinator', V3), ('up', 'front-door', V3), ('up', 'bus-gateway', V3),
                         ('estate-check', 'services'), ('factory-hello',), ('up', 'gateway-watch', V3)]
    assert planning(w) is True and not stopped_door(w)
    assert r.read_json(up.usnap / ('opened-' + V3 + '.json'))['passed'] is True


def door_still_shut(up, mark):
    w = up.world
    assert stopped_door(w) and planning(w) is False
    assert not [e for e in events_since(w, mark) if e[0] in ('up', 'planning')]
    assert not (up.usnap / ('opened-' + V3 + '.json')).exists()


@pytest.mark.parametrize('receipt', ['missing', 'stale', 'other-release', 'not-fully-checked'])
def test_open_refuses_each_unusable_closed_door_receipt_and_starts_nothing(up, receipt):
    w = up.world; early = stamp(); forward_to_switch(up)
    if receipt == 'stale':w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'], written_at=early)
    if receipt == 'other-release':w.write_pre_resume(up.doors[V3], r.RELEASES[V2]['runtime'])
    if receipt == 'not-fully-checked':w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'], status='passed-with-items-not-checked')
    mark = len(w.events)
    with pytest.raises(r.Refusal, match="closed-door receipt in .* cannot be acted on"):up.estate(V3).open()
    door_still_shut(up, mark)


def test_open_refuses_starting_nothing_when_a_long_running_service_is_stopped(up):
    w = up.world; forward_to_switch(up); w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'])
    w.stop(w.running('memory-relay')[0]); mark = len(w.events)
    with pytest.raises(r.Refusal, match='memory-relay is not running on release .*; run --switch'):up.estate(V3).open()
    door_still_shut(up, mark)


def test_open_refuses_starting_nothing_when_a_producer_already_runs(up):
    w = up.world; forward_to_switch(up); w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'])
    w.up(V3, ['front-door'], False); mark = len(w.events)
    with pytest.raises(r.Refusal, match='producer front-door is running'):up.estate(V3).open()
    assert planning(w) is False and not [e for e in events_since(w, mark) if e[0] in ('up', 'planning')]


@pytest.mark.parametrize('state', ['running', 'exited-1', 'exited-0'])
def test_open_judges_the_one_shot_bus_ready(up, state):
    w = up.world; forward_to_switch(up); w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'])
    bus = w.of('bus-ready')[0]
    if state == 'running':bus['State'].update(Running=True, Status='running', StartedAt=stamp()); bus['polls_left'] = 10 ** 6
    if state == 'exited-1':bus['State'].update(ExitCode=1)
    mark = len(w.events)
    if state == 'exited-0':
        assert up.estate(V3).open()['passed']
        return
    with pytest.raises(r.Refusal, match='still running its comparison|bus-ready exited 1'):up.estate(V3).open()
    door_still_shut(up, mark)


def test_open_without_a_switch_for_its_release_refuses(up):
    shut(up); up.world.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'])
    with pytest.raises(r.Refusal, match='run --switch for it first'):up.estate(V3).open()
    assert stopped_door(up.world)


@pytest.mark.parametrize('failing', ['hello', 'services'])
def test_a_failure_after_the_producers_start_shuts_the_door_again(up, failing):
    w = up.world; forward_to_switch(up); w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime']); w.fail[failing] = True
    with pytest.raises(r.Refusal):up.estate(V3).open()
    assert stopped_door(w) and planning(w) is False
    saved = r.read_json(up.usnap / ('opened-' + V3 + '.json'))
    assert saved['passed'] is False and saved['cleanup'] == {'producers_stopped': True, 'watch_stopped': True, 'planning_enabled_false': True}
    assert 'sentinel-private-value' not in json.dumps(saved)


def test_wb_a_cancel_switch_rc2_and_open_on_release_2(up):
    w = up.world; shut(up); up.estate(V2).switch()
    with pytest.raises(r.Refusal):up.estate(V2).open()          # no release -2 receipt yet
    w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'])        # another release's folder does not count
    with pytest.raises(r.Refusal):up.estate(V2).open()
    w.write_pre_resume(up.doors[V2], r.RELEASES[V2]['runtime'])  # RC2
    assert up.estate(V2).open()['passed'] and planning(w) is True
    assert running_services(w)['front-door'] == V2


def test_markers_are_never_written_by_any_upgrade_mode(up, monkeypatch):
    writes = []
    original = q.Estate.volume
    def volume(self, role, code, args=(), write=False, environment=False):
        if write:writes.append((role, 'ROLLOUT-RESUMED' in code))
        return original(self, role, code, args, write, environment)
    monkeypatch.setattr(q.Estate, 'volume', volume)
    forward_to_switch(up); up.world.write_pre_resume(up.doors[V3], V3_ENTRY['runtime']); up.estate(V3).open()
    assert writes and all(role == 'settings' and not marker for role, marker in writes)


@pytest.mark.parametrize('mode', ['close', 'switch', 'open'])
def test_upgrade_modes_preview_without_changing_anything(up, mode):
    before = sorted(p.name for p in up.usnap.iterdir()); events = len(up.world.events)
    e = up.estate(V2); plan = e.plan(mode)
    assert plan['plan'] and plan['upgrade']['release'] == V2 and up.world.events[events:] == [] and sorted(p.name for p in up.usnap.iterdir()) == before


def test_the_command_line_offers_switch_and_open(up, capsys, monkeypatch):
    argv = ['--config', str(up.inventories[V2]), '--env-file', str(up.envs[V2]), '--project', PROJECT, '--snapshot', str(up.usnap), '--secret-env-file', str(up.private_env)]
    assert q.main(argv + ['--close']) == 0 and r.read_json(up.receipt)['stage'] == 'closed'
    assert q.main(argv + ['--open']) == 2 and 'run --switch for it first' in capsys.readouterr().err
