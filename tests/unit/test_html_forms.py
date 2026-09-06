import pytest
from lib import agents, artifacts, db, html_forms


def form(tmp_path):
    agents.create_agent(persona='Mike', voice_id='V', cwd=str(tmp_path), session='mike')
    return artifacts.create(session='mike',type='html_form',title='Plan',payload={
        'content':'<form><input name="budget" type="number"></form>', 'version':'1',
        'answer_schema':{'type':'object','properties':{'budget':{'type':'number','minimum':0}},'required':['budget'],'additionalProperties':False}})


def test_receipt_retries_and_durable_outbox(tmp_path):
    f=form(tmp_path)
    body={'submission_id':'a'*32,'version':'1','answers':{'budget':42}}
    first=html_forms.submit(f['artifact_id'],body)
    assert first['accepted'] and first['delivery_status']=='pending'
    assert html_forms.submit(f['artifact_id'],body)==first
    assert len(html_forms.pending())==1
    assert html_forms.pending()[0]['session']=='mike'
    assert '"budget":42' in html_forms.pending()[0]['prompt']
    html_forms.mark_delivered(body['submission_id'])
    assert html_forms.submit(f['artifact_id'],body)['delivery_status']=='delivered'
    assert not html_forms.pending()


def test_reject_reused_id_changed_answers_and_stale_version(tmp_path):
    f=form(tmp_path);body={'submission_id':'b'*32,'version':'1','answers':{'budget':12}}
    html_forms.submit(f['artifact_id'],body)
    with pytest.raises(ValueError,match='different answers'):
        html_forms.submit(f['artifact_id'],{**body,'answers':{'budget':13}})
    with pytest.raises(ValueError,match='version mismatch'):
        html_forms.submit(f['artifact_id'],{**body,'submission_id':'c'*32,'version':'2'})
    assert len(html_forms.pending())==1


def test_validation_immutable_and_closed_forms(tmp_path):
    f=form(tmp_path)
    with pytest.raises(ValueError,match='immutable'):
        artifacts.update(f['artifact_id'],{'payload_patch':{'content':'changed'}})
    for answers in [{'budget':-1},{'budget':'oops'},{'budget':True},{'budget':1,'recipient':'other'}]:
        with pytest.raises(ValueError): html_forms.submit(f['artifact_id'],{'submission_id':'d'*32,'version':'1','answers':answers})
    artifacts.update(f['artifact_id'],{'status':'cancelled'})
    with pytest.raises(ValueError,match='no longer'):
        html_forms.submit(f['artifact_id'],{'submission_id':'d'*32,'version':'1','answers':{'budget':1}})
    assert not html_forms.pending()


def test_schema_does_not_fetch_external_references(tmp_path):
    f=form(tmp_path)
    with pytest.raises(ValueError,match='references'):
        artifacts.create(session='mike',type='html_form',title='Bad',payload={
            'content':'<html>form</html>','version':'1','answer_schema':{'type':'object','$ref':'https://example.com/schema'}})


def test_html_content_is_not_rewritten_as_conversation_markup(tmp_path):
    f=form(tmp_path)
    content='<html><script>const sample="<speak>example</speak>";</script><form></form></html>'
    row=artifacts.create(session='mike',type='html_form',title='HTML',payload={**f['payload'],'content':content})
    assert row['content']==content


def test_upgrade_from_73_preserves_existing_artifacts(tmp_path):
    f=form(tmp_path)
    con=db.conn()
    con.execute('DROP TABLE form_submissions')
    con.execute('PRAGMA user_version=73')
    before=tuple(con.execute('SELECT * FROM artifacts WHERE artifact_id=?',(f['artifact_id'],)).fetchone())
    db._migrate(con)
    assert con.execute('PRAGMA user_version').fetchone()[0]==db._SCHEMA_VERSION
    assert tuple(con.execute('SELECT * FROM artifacts WHERE artifact_id=?',(f['artifact_id'],)).fetchone())==before
    assert con.execute('SELECT COUNT(*) FROM form_submissions').fetchone()[0]==0
