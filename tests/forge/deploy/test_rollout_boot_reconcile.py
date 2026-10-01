"""Start-up reconciliation accepted by the rollout checks (added 1 October 2026).

The release coordinator turns stale INTERRUPTED builds into FAILED with a
"sandbox-required:" reason when it starts. The rollout checks accept exactly that
and nothing else. Pure SQLite fixtures; no Forge import, Docker or network.
"""
import copy
import importlib.util
import sqlite3
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[3] / 'deploy' / 'estate'
spec = importlib.util.spec_from_file_location('rollout_support_bootrec', BUNDLE / 'rollout_support.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

BUILD_COLUMNS = ('build_id', 'status', 'started_at', 'completed_at', 'error', 'feature_id')


def ledger(path, builds, other=(('a', 1),)):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE builds (build_id TEXT PRIMARY KEY, status TEXT, started_at TEXT, completed_at TEXT, error TEXT, feature_id TEXT)')
    db.execute('CREATE TABLE other (k TEXT, v INTEGER)')
    db.executemany('INSERT INTO builds VALUES (?,?,?,?,?,?)', builds)
    db.executemany('INSERT INTO other VALUES (?,?)', other)
    db.commit(); db.close()
    return path


BASE = [('b1', 'INTERRUPTED', None, None, 'stale-queued: old', 'F1'),
        ('b2', 'COMPLETE', '2026-07-01', '2026-07-01', None, 'F2'),
        ('b3', 'INTERRUPTED', '2026-07-02', None, 'stale-queued: old', 'F3')]
RECONCILED = [('b1', 'FAILED', '2026-10-01T07:33:54', '2026-10-01T07:33:54', "sandbox-required: repository 'x' is not registered", 'F1'),
              BASE[1],
              ('b3', 'FAILED', '2026-07-02', '2026-10-01T07:33:54', 'sandbox-required: repository has no registered sandbox', 'F3')]


def test_exactly_the_reconciliation_is_accepted(tmp_path):
    assert r.boot_reconciled_builds(ledger(tmp_path / 'a.db', BASE), ledger(tmp_path / 'b.db', RECONCILED)) == ['b1', 'b3']


def test_identical_ledgers_change_nothing(tmp_path):
    assert r.boot_reconciled_builds(ledger(tmp_path / 'a.db', BASE), ledger(tmp_path / 'b.db', BASE)) == []


@pytest.mark.parametrize('row', [
    ('b1', 'FAILED', None, None, 'some other reason', 'F1'),             # not the start-up reason
    ('b1', 'CANCELLED', None, '2026-10-01', 'sandbox-required: x', 'F1'),  # not FAILED
    ('b1', 'FAILED', None, '2026-10-01', 'sandbox-required: x', 'F9'),    # another column changed
])
def test_any_other_build_change_is_refused(tmp_path, row):
    after = [row, BASE[1], BASE[2]]
    with pytest.raises(r.Refusal):
        r.boot_reconciled_builds(ledger(tmp_path / 'a.db', BASE), ledger(tmp_path / 'b.db', after))


def test_a_terminal_build_changed_to_failed_is_refused(tmp_path):
    after = [BASE[0], ('b2', 'FAILED', '2026-07-01', '2026-10-01', 'sandbox-required: x', 'F2'), BASE[2]]
    with pytest.raises(r.Refusal):
        r.boot_reconciled_builds(ledger(tmp_path / 'a.db', BASE), ledger(tmp_path / 'b.db', after))


def test_added_or_removed_builds_are_refused(tmp_path):
    with pytest.raises(r.Refusal):
        r.boot_reconciled_builds(ledger(tmp_path / 'a.db', BASE), ledger(tmp_path / 'b.db', RECONCILED + [('b4', 'FAILED', None, None, 'sandbox-required: x', 'F4')]))
    with pytest.raises(r.Refusal):
        r.boot_reconciled_builds(ledger(tmp_path / 'c.db', BASE), ledger(tmp_path / 'd.db', RECONCILED[:2]))


def test_a_change_in_any_other_table_is_refused(tmp_path):
    with pytest.raises(r.Refusal):
        r.boot_reconciled_builds(ledger(tmp_path / 'a.db', BASE), ledger(tmp_path / 'b.db', RECONCILED, other=(('a', 2),)))


def test_a_schema_change_is_refused(tmp_path):
    b = ledger(tmp_path / 'b.db', RECONCILED)
    db = sqlite3.connect(b); db.execute('CREATE TABLE extra (x)'); db.commit(); db.close()
    with pytest.raises(r.Refusal):
        r.boot_reconciled_builds(ledger(tmp_path / 'a.db', BASE), b)


def work(rows):
    return {'builds': {'status': 'observed', 'count': len(rows), 'rows': rows},
            'work_queue': {'status': 'observed', 'count': 0, 'rows': []}}


WB = [{'build_id': 'b1', 'status': 'INTERRUPTED', 'completed_at': None, 'pending_approval_request_id': None},
      {'build_id': 'b2', 'status': 'COMPLETE', 'completed_at': '2026-07-01', 'pending_approval_request_id': None}]


def test_work_state_accepts_only_the_reconciliation():
    after = copy.deepcopy(WB); after[0].update(status='FAILED', completed_at='2026-10-01T07:33:54')
    assert r.work_state_boot_reconciled(work(WB), work(after))
    bad = copy.deepcopy(after); bad[1]['status'] = 'FAILED'
    assert not r.work_state_boot_reconciled(work(WB), work(bad))
    bad = copy.deepcopy(after); bad[0]['pending_approval_request_id'] = 'x'
    assert not r.work_state_boot_reconciled(work(WB), work(bad))
    other = work(after); other['work_queue']['count'] = 1
    assert not r.work_state_boot_reconciled(work(WB), other)
    assert not r.work_state_boot_reconciled(work(WB), work(after[:1]))
