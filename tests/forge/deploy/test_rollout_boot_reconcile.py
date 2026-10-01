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


class Done:
    def __init__(self, stdout): self.stdout = stdout


def staged(tmp_path, monkeypatch):
    """A snapshot folder whose retained artifact is BASE; the start-up derivation changes nothing."""
    snap = tmp_path / 'snapshots' / 'snap'; snap.mkdir(parents=True)
    artifact = ledger(snap / r.MIGRATED_ARTIFACT, BASE)
    monkeypatch.setattr(r, 'container_python', lambda *a, **k: None)
    return snap, {'startup_logical_sha256': r.consolidated_logical_digest(artifact)}


def test_a_copy_of_the_observed_reconciled_state_is_accepted(tmp_path, monkeypatch):
    snap, receipt = staged(tmp_path, monkeypatch)
    copy = ledger(tmp_path / 'copy.db', RECONCILED)
    assert r.reconcile_started_copy({}, snap, receipt, copy, r.consolidated_logical_digest(copy)) == ['b1', 'b3']
    assert sorted(p.name for p in snap.parent.iterdir()) == ['snap']   # the derivation folder is removed


def test_a_copy_that_is_not_the_observed_state_is_refused(tmp_path, monkeypatch):
    """Codex R2: the services observed state A; a later copy of valid state B must not stand in for it."""
    snap, receipt = staged(tmp_path, monkeypatch)
    observed = r.consolidated_logical_digest(ledger(tmp_path / 'a.db', [BASE[0], BASE[1], ('b3', 'FAILED', None, None, 'other', 'F3')]))
    copy = ledger(tmp_path / 'copy.db', RECONCILED)
    with pytest.raises(r.Refusal, match='changed between the observation and its copy'):
        r.reconcile_started_copy({}, snap, receipt, copy, observed)


def test_a_failed_build_with_another_reason_is_refused_end_to_end(tmp_path, monkeypatch):
    """Codex R1: the full predicate, not the work-state summary, decides."""
    snap, receipt = staged(tmp_path, monkeypatch)
    copy = ledger(tmp_path / 'copy.db', [('b1', 'FAILED', None, '2026-10-01', 'stale-queued: other', 'F1'), BASE[1], BASE[2]])
    with pytest.raises(r.Refusal):
        r.reconcile_started_copy({}, snap, receipt, copy, r.consolidated_logical_digest(copy))


def test_an_unchanged_copy_is_not_a_reconciliation(tmp_path, monkeypatch):
    snap, receipt = staged(tmp_path, monkeypatch)
    copy = ledger(tmp_path / 'copy.db', BASE)
    with pytest.raises(r.Refusal, match='without a reconciled build'):
        r.reconcile_started_copy({}, snap, receipt, copy, r.consolidated_logical_digest(copy))


def test_a_derivation_that_differs_from_the_receipt_is_refused(tmp_path, monkeypatch):
    snap, receipt = staged(tmp_path, monkeypatch)
    copy = ledger(tmp_path / 'copy.db', RECONCILED)
    with pytest.raises(r.Refusal, match='independently derived normal boot'):
        r.reconcile_started_copy({}, snap, {'startup_logical_sha256': '0' * 64}, copy, r.consolidated_logical_digest(copy))


def test_the_running_coordinator_copy_is_bound_to_the_observation(tmp_path, monkeypatch):
    import base64
    snap, receipt = staged(tmp_path, monkeypatch)
    src = sqlite3.connect(ledger(tmp_path / 'started.db', RECONCILED))
    monkeypatch.setattr(r, 'docker', lambda c, *a, **k: Done(base64.b64encode(src.serialize()).decode()))
    good = r.consolidated_logical_digest(tmp_path / 'started.db')
    assert r.started_ledger_reconciliation({}, snap, receipt, 'cid', good) == ['b1', 'b3']
    with pytest.raises(r.Refusal, match='changed between the observation and its copy'):
        r.started_ledger_reconciliation({}, snap, receipt, 'cid', '1' * 64)
    assert sorted(p.name for p in snap.parent.iterdir()) == ['snap']
