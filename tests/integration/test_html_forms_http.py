from tests.integration.test_attention_questions_http import host, _request, server_module
from lib import artifacts, html_forms


def test_form_receipt_and_retried_dispatch(host):
    row = artifacts.create(session='theo',type='html_form',title='Interactive plan',payload={
        'content':'<form><input name="priority"></form>', 'version':'1',
        'answer_schema':{'type':'object','properties':{'priority':{'type':'string'}},'required':['priority']}})
    path='/artifacts/'+row['artifact_id']+'/submit'
    body={'submission_id':'http-form-submission-12345','version':'1','answers':{'priority':'offline first'}}
    code, response = _request(host,path,body)
    assert code == 200 and response['accepted'] is True
    assert response['delivery_status']=='pending'
    host.fail_delivery=True
    server_module._deliver_decision_rows(host.ctx)
    assert len(html_forms.pending())==1
    host.fail_delivery=False
    server_module._deliver_decision_rows(host.ctx)
    assert not html_forms.pending()
    assert host.deliveries[-1]['forced_session']=='theo'
    assert host.deliveries[-1]['queue_if_busy'] is True
    assert host.deliveries[0]['client_msg_id']==host.deliveries[1]['client_msg_id']
    code, response = _request(host,path,body)
    assert code == 200 and response['delivery_status']=='delivered'


def test_form_submit_requires_host_authentication(host):
    status,_=_request(host,'/artifacts/form-unknown/submit',{'submission_id':'a'*32,'version':'1','answers':{}},authenticated=False)
    assert status==401
    assert not html_forms.pending()
