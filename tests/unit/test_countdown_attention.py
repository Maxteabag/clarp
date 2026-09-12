import pytest
from lib.countdown_attention import project as _project, project_page

def project(r, **kwargs):
    return _project(r, **{"policy":"action","retention_hours":24,**kwargs})
NOW=1788868800000

def row(**kw):
    return dict(server_id='host-a', artifact_id='event', occurrence_id='one', updated_at=1,
                status='ready', target_at='2026-09-08T12:00:00Z', purpose='informational', **kw) if not kw else {**row(),**kw}

@pytest.mark.parametrize('status,bucket',[('draft','history'),('active','working'),('failed','correction'),('cancelled','history'),('expired','history'),('ready','review'),('completed','review'),('new','unsupported')])
def test_status_precedes_time(status,bucket):
    assert project(row(status=status),now_ms=NOW)['bucket']==bucket

def test_past_and_boundary():
    for policy in ['action','grace']:
        assert project(row(),now_ms=NOW+86400000-1,policy=policy)['visible']
        assert not project(row(),now_ms=NOW+86400000,policy=policy)['visible']
    assert not project(row(),now_ms=NOW,policy='immediate')['visible']
    assert project(row(),now_ms=NOW+86400000*30,policy='manual')['visible']

def test_actionable_and_legacy():
    assert project(row(purpose='actionable'),now_ms=NOW+86400000*30)['phase']=='overdue'
    assert project(row(purpose=None),now_ms=NOW+86400000*30)['visible']

def test_receipt_identity_and_archive():
    key=('host-a','event','one',1)
    assert not project(row(),now_ms=NOW,receipts=(key,))['visible']
    for changes in [dict(server_id='host-b'),dict(updated_at=2),dict(occurrence_id='two')]:
        assert project(row(**changes),now_ms=NOW,receipts=(key,),archives=(key,))['visible']
    p=project(row(),now_ms=NOW,archives=(key,));assert p['archived'] and p['bucket']=='review' and p['record_status']=='ready'

def test_invalid_before_ack_but_after_terminal():
    key=('host-a','event','one',1)
    assert project(row(target_at='bad'),now_ms=NOW,receipts=(key,))['bucket']=='correction'
    assert project(row(target_at='bad',status='cancelled'),now_ms=NOW)['bucket']=='history'

def test_equivalent_offset_and_immutable_row():
    r=row(target_at='2026-09-08T14:00:00+02:00');copy=r.copy()
    assert project(r,now_ms=NOW)==project(row(),now_ms=NOW);assert r==copy

def test_filter_before_page_limit():
    rows=[row(status='cancelled',artifact_id=str(i)) for i in range(250)]+[row(artifact_id='deadline',purpose='actionable')]
    assert project_page(rows,now_ms=NOW+86400000*2,limit=1,policy="action",retention_hours=24)[0]['artifact_id']=='deadline'

def test_projection_of_actual_durable_artifact(tmp_path):
    from lib import agents, artifacts
    agents.create_agent(persona='Fixture', voice_id='V', cwd=str(tmp_path), session='fixture')
    saved=artifacts.create(session='fixture',type='countdown',title='Milestone',payload={'target_at':'2020-01-01T00:00:00Z','time_zone':'UTC'})
    source={**saved,'server_id':'host-a','purpose':'informational'}
    for policy in ['immediate','grace','action']:
        assert not project(source,now_ms=NOW,policy=policy)['visible']
        assert artifacts.get(saved['artifact_id'])['status']=='ready'
    assert project(source,now_ms=NOW,policy='manual')['visible']


def test_unset_policy_preserves_visibility_even_with_explicit_purpose():
    for purpose in [None, "informational", "actionable"]:
        assert _project(row(purpose=purpose),now_ms=NOW+86400000*30)["visible"]

@pytest.mark.parametrize("policy",["grace","action"])
def test_grace_must_be_selected_explicitly(policy):
    with pytest.raises(ValueError,match="explicitly"):
        _project(row(),now_ms=NOW,policy=policy)

@pytest.mark.parametrize("value",[True,1.5,-1,73])
def test_invalid_retention(value):
    with pytest.raises(ValueError):
        _project(row(),now_ms=NOW,policy="grace",retention_hours=value)
