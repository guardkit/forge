"""The independent coach's adversarial scenarios for TC1-TC3 (2 October 2026), kept.

Each says what the coach found and what must hold now. S2 and S3 asserted the old
deadlock (a supervisor that exited non-zero blocked --final for good); they are
replaced by what the fix requires: accepted only once nothing runs inside the
sandbox, refused otherwise.
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest
import yaml

from .test_rollout_upgrade import (b, events_since, forward_to_switch, planning, q, r, running_services, shut, stopped_door,
                                   up)  # noqa: F401  (the fixture)
from .test_rollout_upgrade_back import back, opened_on_3, passing_h6
from .upgrade_world import PREFIX, V2, V3, V3_ENTRY


def gate_refusal(callable_):
    with pytest.raises(r.Refusal, match='has not been proved able to handle what it wrote; use rollout-back --upgrade-back') as refused:
        callable_()
    return refused


# ----------------------------------------------------------- 1. no release -2 on release -3's record without H6

def test_S1_a_failed_back_h6_never_lets_release_2_start_through_switch(up):
    w = up.world; opened_on_3(up); w.h6_result = lambda c: (3, None)
    with pytest.raises(r.Refusal):back(up).upgrade_back()
    mark = len(w.events)
    gate_refusal(lambda: up.estate(V2).switch())
    assert not events_since(w, mark, 'up') and not running_services(w)
    assert [x['passed'] for x in r.read_json(up.receipt)['back_h6']] == [False]


def test_S1b_release_3_stopped_by_its_own_inventory_still_needs_h6(up):
    w = up.world; opened_on_3(up)
    for mode in ('close', 'settle', 'final'):getattr(up.estate(V3), mode)()
    mark = len(w.events)
    gate_refusal(lambda: up.estate(V2).switch())
    for mode in ('close', 'settle', 'final', 'open'):
        gate_refusal(lambda: getattr(up.estate(V2), mode)())
    assert not events_since(w, mark, 'up') and not events_since(w, mark, 'planning') and not events_since(w, mark, 'reader')


def test_S9c_a_to_switch_that_started_release_3_and_failed_needs_h6(up):
    w = up.world; shut(up); w.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3])
    def write(release):
        if release == V3:
            with r.sqlite3.connect(w.ledger) as db:db.execute("INSERT INTO work_queue VALUES ('w3','CLOSED','c',1,NULL,NULL)")
    w.on_coordinator_start = write
    with pytest.raises(r.Refusal):up.estate(V3).switch()
    for c in list(w.containers.values()):
        if c['State']['Running']:w.stop(c, 137)
    gate_refusal(lambda: up.estate(V2).switch())


def test_a_switch_cut_off_before_its_end_is_still_on_record(up, monkeypatch):
    # run-step's SIGKILL leaves no finally; the switch is recorded before release -3's first start.
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3])
    on_disk = []
    def killed(self, since):
        # What the receipt holds at the instant release -3's first container starts,
        # which is all a SIGKILL there would leave (no finally runs).
        on_disk.append(r.read_json(up.receipt)['switches'][-1]); raise SystemExit('killed')
    monkeypatch.setattr(q.Estate, 'run_bus_ready', killed)
    with pytest.raises(SystemExit):up.estate(V3).switch()
    assert on_disk[0]['release'] == V3 and on_disk[0]['started_release'] is True and on_disk[0]['passed'] is None
    gate_refusal(lambda: up.estate(V2).switch())


def test_a_to_switch_that_failed_before_starting_anything_leaves_wb_a_open(up):
    w = up.world; shut(up); w.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3]); w.fail['policy'] = True
    with pytest.raises(r.Refusal):up.estate(V3).switch()
    w.fail.pop('policy'); w.restore_release_2_in_sandbox()
    assert r.read_json(up.receipt)['switches'][-1]['started_release'] is False
    assert up.estate(V2).switch()['passed'] and running_services(w)['coordinator'] == V2


def test_after_back_h6_passed_release_2_may_be_switched_and_opened(up):
    w = up.world; opened_on_3(up); passing_h6(w)
    w.sandbox_back = lambda argv: (2, 'Refusing: the sandbox answered late; nothing was put back.\n')
    with pytest.raises(r.Refusal, match='rollout-sandbox --upgrade --back refused'):back(up).upgrade_back()
    w.sandbox_back = None
    assert [x['passed'] for x in r.read_json(up.receipt)['back_h6']] == [True]
    w.restore_release_2_in_sandbox()
    assert up.estate(V2).switch()['passed']
    w.write_pre_resume(up.doors[V2], r.RELEASES[V2]['runtime'])
    assert up.estate(V2).open()['passed'] and running_services(w)['front-door'] == V2


def test_close_never_records_the_other_releases_containers(up):
    w = up.world; forward_to_switch(up)
    stray = w.create('memory', V2, running=False)   # a release -2 container that appeared after U1
    up.estate(V3).close()
    recorded = r.read_json(up.receipt)['containers']
    assert stray not in recorded
    assert all(x['image'] in (V3_ENTRY[k] for k in ('runtime', 'publisher', 'memory', 'relay', 'jarvis')) or x['service'] in ('bus-ready', 'gateway-watch')
               for i, x in recorded.items() if x.get('recorded_by') == V3)
    assert {x['recorded_by'] for x in recorded.values()} == {V2, V3}


def test_a_to_switch_while_release_2_runs_says_to_stop_it_first(up):
    w = up.world; opened_on_3(up); passing_h6(w); back(up).upgrade_back()
    with pytest.raises(r.Refusal, match='only after --final stopped release'):up.estate(V3).switch()


# ----------------------------------------------------------- 2. a supervisor that exited non-zero

def test_S3_back_with_a_crashed_release_3_supervisor_returns_release_2(up):
    w = up.world; forward_to_switch(up); passing_h6(w); w.stop(w.running('sandbox-runner')[0], 1)
    result = back(up).upgrade_back()
    assert result['status'] == 'returned-with-the-door-shut' and result['final']['backup']
    assert running_services(w)['sandbox-runner'] == V2 and w.inner == {PREFIX + '-helper': 'forge:' + V2, PREFIX + '-runner': 'forge:' + V2}


def test_S3b_a_supervisor_that_refuses_at_start_does_not_block_the_way_back(up):
    w = up.world; forward_to_switch(up); passing_h6(w); w.supervisor_stop_exit = 2
    assert back(up).upgrade_back()['status'] == 'returned-with-the-door-shut'
    final = r.read_json(up.receipt)['final']
    assert final['supervisor']['exit'] == 2 and 'refused before starting anything' in final['supervisor']['meaning']


def test_back_refuses_while_release_3_work_still_runs_inside_the_sandbox(up):
    w = up.world; forward_to_switch(up); passing_h6(w); w.supervisor_stop_exit = 3   # its stop failed: work still inside
    with pytest.raises(r.Refusal, match='exited 3: its stop of the work inside the sandbox did not succeed'):back(up).upgrade_back()
    assert not events_since(w, 0, 'h6-create')


# ----------------------------------------------------------- 3. an interrupted --open

def test_S5_ctrl_c_while_the_door_opens_shuts_it_again(up, monkeypatch):
    w = up.world; forward_to_switch(up); w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'])
    monkeypatch.setattr(q.Estate, 'wait_ready', lambda self, events: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):up.estate(V3).open()
    assert stopped_door(w) and planning(w) is False
    assert r.read_json(up.usnap / ('opened-' + V3 + '.json'))['cleanup'] == {'producers_stopped': True, 'watch_stopped': True, 'planning_enabled_false': True}


def test_a_stop_signal_while_the_door_opens_shuts_it_again_and_says_so(up, monkeypatch):
    w = up.world; forward_to_switch(up); w.write_pre_resume(up.doors[V3], V3_ENTRY['runtime'])
    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(q.Estate, 'wait_ready', lambda self, events: os.kill(os.getpid(), signal.SIGTERM))
    with pytest.raises(r.Refusal, match='stopped by a signal while it opened the door'):up.estate(V3).open()
    assert stopped_door(w) and planning(w) is False and signal.getsignal(signal.SIGTERM) is before


def test_a_close_after_an_open_cut_off_by_sigkill_shuts_the_door_and_turns_planning_off(up):
    w = up.world; forward_to_switch(up)
    # What a SIGKILL during --open leaves: planning on and the producers up, no clean-up.
    data = yaml.safe_load(w.settings.read_text()); data['planning']['enabled'] = True; w.settings.write_text(yaml.safe_dump(data, sort_keys=False))
    w.up(V3, ['front-door', 'bus-gateway', 'gateway-watch'], False)
    up.estate(V3).close()
    assert stopped_door(w) and planning(w) is False
    for mode in ('settle', 'final', 'switch'):getattr(up.estate(V3), mode)()    # and the way forward is open again
    assert running_services(w)['coordinator'] == V3


# ----------------------------------------------------------- 4. time limits

def test_every_upgrade_mode_states_its_limit_in_help(capsys):
    with pytest.raises(SystemExit):q.main(['--help'])
    text = ' '.join(capsys.readouterr().out.split())
    for mode, seconds in (('--close', 600), ('--settle', 300), ('--final', 900), ('--switch', 1500), ('--open', 600), ('--upgrade-back', 3300)):
        assert f'{mode} {seconds} s' in text
    assert q.MODE_LIMITS['open-clean-up'] == 300


def test_a_mode_limit_bounds_every_command_and_starts_none_after_it():
    started = time.monotonic()
    with r.deadline(0.5, '--switch'):
        with pytest.raises(subprocess.TimeoutExpired):r.run([sys.executable, '-c', 'import time;time.sleep(5)'])
        time.sleep(0.1)
        with pytest.raises(r.Refusal, match='--switch reached its own time limit before'):r.run([sys.executable, '-c', 'pass'])
    assert time.monotonic() - started < 2 and r.DEADLINE is None
    with r.deadline(100, 'outer'):
        with r.deadline(1000, 'inner'):assert r.DEADLINE_WHAT == 'outer'
        with r.deadline(5, 'clean-up', replace=True):assert r.DEADLINE_WHAT == 'clean-up'


def test_switch_runs_inside_its_limit(up, monkeypatch):
    seen = []
    original = r.run
    def run(argv, **kwargs):
        seen.append((r.DEADLINE_WHAT, r.DEADLINE is not None)); return original(argv, **kwargs)
    shut(up); up.world.install_release_3_in_sandbox(up.evidence, up.bootstrap[V3])
    monkeypatch.setattr(r, 'run', run)
    up.estate(V3).switch()
    assert seen and all(what == '--switch' and bounded for what, bounded in seen)


# ----------------------------------------------------------- 5. a cancel right after --close

def test_S12_a_cancel_never_restarts_the_coordinator_under_active_work(up):
    w = up.world; up.estate(V2).close()
    with r.sqlite3.connect(w.ledger) as db:db.execute("INSERT INTO builds VALUES ('b-live','RUNNING',NULL,NULL,'2026-10-02',NULL)")
    mark = len(w.events)
    with pytest.raises(r.Refusal, match=r'the record shows active work \(builds\), and --switch restarts the coordinator'):up.estate(V2).switch()
    assert not events_since(w, mark, 'up') and not events_since(w, mark, 'planning')


# ----------------------------------------------------------- 6. what a refusal points to

def test_a_missing_closed_door_receipt_keeps_estate_checks_words_and_names_the_remedy(up):
    w = up.world; forward_to_switch(up)
    with pytest.raises(r.Refusal) as refused:up.estate(V3).open()
    text = str(refused.value); diagnostic = up.usnap / ('open-refused-' + V3 + '.json')
    assert 'no closed-door receipt' in text and str(diagnostic) in text and 'estate-check --pre-resume --for-image ' + V3_ENTRY['runtime'] in text
    saved = r.read_json(diagnostic)
    assert saved['commands'][-1]['stderr'] == 'no closed-door receipt' and diagnostic.stat().st_mode & 0o777 == 0o600
    assert 'sentinel-private-value' not in json.dumps(saved)


def test_an_unrecorded_container_of_release_3_points_to_back(up):
    w = up.world; shut(up)
    stray = w.create('memory', V3, running=False)
    with pytest.raises(r.Refusal, match='no --close recorded it, so it ran after the upgrade began: go back with rollout-back --upgrade-back'):up.estate(V2).switch()
    assert stray in w.containers
