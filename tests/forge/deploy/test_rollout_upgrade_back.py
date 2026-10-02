"""rollout-back --upgrade-back: the way back after an upgrade's switch (TC3).

Release -3 upgrade runbook, procedure WB-C: close, settle and stop the release
being left (its sandbox supervisor included), prove with the H6 probe in the
returned-to release's coordinator image against a copy of the current ledger,
remove the left release's containers, put the sandbox back, then bring the
returned-to release up with the door shut and planning off. Opening is that
release's own closed-door check (RC2) and then its --open.
"""
import json
from pathlib import Path

import pytest
import yaml

from .test_rollout_release_table import valid_h6
from .test_rollout_upgrade import (b, events_since, forward_to_switch, planning, q, r, running_services, stopped_door,
                                   up)  # noqa: F401  (the fixture)
from .upgrade_world import PREFIX, PROJECT, V2, V3, V3_ENTRY


def passing_h6(world):
    def result(container):
        tail = container['argv']; image = container['Image']
        # The probe is handed a copy of the ledger as it is now, never the live file.
        assert r.consolidated_logical_digest(container['pristine']) == r.consolidated_logical_digest(world.ledger)
        assert container['pristine'] != str(world.ledger)
        proof = valid_h6(); proof.update(candidate_image_id=image, pristine_sha256=tail[2], working_pre_fixture_sha256=tail[2],
                                         schema_version=int(tail[4]), configuration_sha256=tail[5])
        return 0, proof
    world.h6_result = result


def opened_on_3(up):
    forward_to_switch(up); up.world.write_pre_resume(up.doors[V3], V3_ENTRY['runtime']); up.estate(V3).open()


def back(up):
    return b.Recovery(up.args(V3))


def test_upgrade_back_returns_release_2_on_the_same_volumes_with_the_door_shut(up):
    w = up.world; opened_on_3(up); passing_h6(w); volumes = json.dumps(w.volumes, sort_keys=True); mark = len(w.events)
    result = back(up).upgrade_back()
    assert result['status'] == 'returned-with-the-door-shut' and result['switch']['passed']
    assert running_services(w) == {s: V2 for s in ('coordinator', 'answer-service', 'memory', 'memory-relay', 'forge-publisher', 'sandbox-runner')}
    assert stopped_door(w) and planning(w) is False and json.dumps(w.volumes, sort_keys=True) == volumes
    assert w.inner == {PREFIX + '-helper': 'forge:' + V2, PREFIX + '-runner': 'forge:' + V2}
    events = events_since(w, mark)
    # The release -3 supervisor is stopped first, H6 runs release -2's coordinator image before anything is removed,
    # the sandbox is put back before release -2 starts, and nothing opens the door.
    first_stop = next(i for i, e in enumerate(events) if e[0] == 'stop' and e[1] == 'sandbox-runner')
    h6 = events.index(('h6-create', r.RELEASES[V2]['runtime'], '16'))
    first_rm = next(i for i, e in enumerate(events) if e[0] == 'rm')
    sandbox = next(i for i, e in enumerate(events) if e[0] == 'rollout-sandbox')
    first_up = next(i for i, e in enumerate(events) if e[0] == 'up')
    assert first_stop < h6 < first_rm < sandbox < first_up
    assert events[sandbox] == ('rollout-sandbox', ('--upgrade', '--back'), r.read_json(up.inventories[V3])['upgrade']['sandbox_previous_receipt'])
    assert not [e for e in events if e[0] == 'up' and e[1] in ('front-door', 'bus-gateway', 'gateway-watch')]
    assert {e[1] for e in events if e[0] == 'rm'} >= {'coordinator', 'answer-service', 'forge-publisher', 'sandbox-runner'}
    saved = r.read_json(up.usnap / 'upgrade-back.json')
    assert saved['status'] == 'returned-with-the-door-shut' and saved['h6']['schema_version'] == 16 and 'sentinel-private-value' not in json.dumps(saved)


def test_a_half_switched_graph_is_accepted_not_refused(up):
    w = up.world; forward_to_switch(up); passing_h6(w)
    # The publisher went back to release -2 while the coordinator stayed on release -3.
    publisher = w.running('forge-publisher')[0]; del w.containers[publisher['Id']]; w.create('forge-publisher', V2)
    assert {c['release'] for c in w.containers.values() if c['service'] in ('forge-publisher', 'coordinator') and c['State']['Running']} == {V2, V3}
    assert back(up).upgrade_back()['status'] == 'returned-with-the-door-shut'
    assert running_services(w)['forge-publisher'] == V2 and running_services(w)['coordinator'] == V2


def test_a_changed_env_of_the_release_returned_to_refuses_before_anything_stops(up):
    w = up.world; opened_on_3(up); passing_h6(w)
    up.envs[V2].write_text(up.envs[V2].read_text() + 'CHANGED=1\n'); mark = len(w.events)
    with pytest.raises(r.Refusal, match='no longer have the SHA-256|differ from those recorded'):back(up).upgrade_back()
    assert not events_since(w, mark, 'stop') and running_services(w)['front-door'] == V3


def test_h6_failure_leaves_everything_stopped(up):
    w = up.world; opened_on_3(up); w.h6_result = lambda container: (3, None); mark = len(w.events)
    with pytest.raises(r.Refusal):back(up).upgrade_back()
    assert not running_services(w) and planning(w) is False
    assert not events_since(w, mark, 'rm') and not events_since(w, mark, 'rollout-sandbox') and not events_since(w, mark, 'up')
    saved = r.read_json(up.usnap / 'upgrade-back.json'); assert saved['status'] == 'refused' and saved['failure']['stage'] == 'h6'


def test_h6_proof_for_another_schema_is_refused(up):
    w = up.world; opened_on_3(up); passing_h6(w); good = w.h6_result
    w.h6_result = lambda c: (lambda code, proof: (code, dict(proof, schema_version=15)))(*good(c))
    with pytest.raises(r.Refusal, match='H6 behavioral proof'):back(up).upgrade_back()
    assert not running_services(w)


def test_a_settings_change_other_than_planning_refuses_before_any_release_2_service_starts(up):
    w = up.world; opened_on_3(up); passing_h6(w)
    data = yaml.safe_load(w.settings.read_text()); data['routine']['seat'] = 'another-seat'; w.settings.write_text(yaml.safe_dump(data, sort_keys=False))
    mark = len(w.events)
    with pytest.raises(r.Refusal, match='in more than planning.enabled'):back(up).upgrade_back()
    assert not events_since(w, mark, 'up') and not running_services(w) and not events_since(w, mark, 'rm')


def test_a_refusing_sandbox_step_stops_before_release_2_starts_and_says_why(up):
    w = up.world; opened_on_3(up); passing_h6(w)
    w.sandbox_back = lambda argv: (2, 'Refusing: the saved template differs from its recorded hash; nothing was put back.\n')
    mark = len(w.events)
    with pytest.raises(r.Refusal, match='rollout-sandbox --upgrade --back refused: the saved template differs'):back(up).upgrade_back()
    assert not events_since(w, mark, 'up') and not running_services(w)


def test_an_immediate_open_without_rc2_refuses_on_the_missing_release_2_receipt(up):
    w = up.world; opened_on_3(up); passing_h6(w); back(up).upgrade_back(); mark = len(w.events)
    with pytest.raises(r.Refusal, match='estate-check could not complete'):up.estate(V2).open()
    assert ('read-pre-resume', 'closed-door-release-2', r.RELEASES[V2]['runtime']) in w.events[mark:]
    assert stopped_door(w) and planning(w) is False and not events_since(w, mark, 'up')
    w.write_pre_resume(up.doors[V2], r.RELEASES[V2]['runtime'])   # RC2
    assert up.estate(V2).open()['passed'] and running_services(w)['front-door'] == V2 and planning(w) is True


def test_upgrade_back_needs_the_inventory_of_the_release_being_left(up):
    forward_to_switch(up)
    with pytest.raises(r.Refusal, match="takes the inventory of the release being left"):b.Recovery(up.args(V2)).upgrade_back()


def test_upgrade_inventories_refuse_the_switch_era_recoveries(up):
    for mode in ('before', 'after'):
        with pytest.raises(r.Refusal, match='an upgrade uses'):getattr(back(up), mode)()


def test_the_command_line_offers_upgrade_back(up, capsys):
    argv = ['--config', str(up.inventories[V3]), '--env-file', str(up.envs[V3]), '--project', PROJECT, '--snapshot', str(up.usnap),
            '--secret-env-file', str(up.private_env), '--upgrade-back']
    assert b.main(argv + ['--plan']) == 0 and json.loads(capsys.readouterr().out)['mode'] == 'upgrade_back'
    assert b.main(argv) == 2 and 'Refused:' in capsys.readouterr().err
