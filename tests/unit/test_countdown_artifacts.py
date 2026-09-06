import pytest
from lib import agents, artifacts


def agent(tmp_path):
    agents.create_agent(persona='Mike',voice_id='V',cwd=str(tmp_path),session='mike')


def test_generic_countdown_round_trip_and_target_update(tmp_path):
    agent(tmp_path)
    row=artifacts.create(session='mike',type='countdown',title='Project launch',summary='Review the release before launch',payload={'target_at':'2026-10-01T10:00:00+02:00','time_zone':'Europe/Oslo','content':'Any event, appointment or deadline.'})
    assert row['target_at']=='2026-10-01T10:00:00+02:00'
    assert artifacts.get(row['artifact_id'])['time_zone']=='Europe/Oslo'
    updated=artifacts.update(row['artifact_id'],{'payload_patch':{'target_at':'2026-10-02T08:00:00Z'}})
    assert updated['target_at']=='2026-10-02T08:00:00Z'
    assert updated['time_zone']=='Europe/Oslo'


@pytest.mark.parametrize('target,zone',[('2026-10-01T10:00:00','Europe/Oslo'),('tomorrow','UTC'),('2026-W41-1T10:00:00Z','UTC'),('2026-02-30T10:00:00Z','UTC'),('2026-10-01T08:00:00Z','not/a/zone')])
def test_invalid_or_ambiguous_target_rejected(tmp_path,target,zone):
    agent(tmp_path)
    with pytest.raises(ValueError):
        artifacts.create(session='mike',type='countdown',title='Deadline',payload={'target_at':target,'time_zone':zone})


def test_elapsed_target_is_still_a_durable_countdown(tmp_path):
    agent(tmp_path)
    row=artifacts.create(session='mike',type='countdown',title='Past milestone',payload={'target_at':'2020-01-01T00:00:00Z','time_zone':'UTC'})
    assert artifacts.get(row['artifact_id'])['status']=='ready'
