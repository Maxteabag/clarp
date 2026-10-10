import pytest
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


def test_read_only_report_refuses_submission_with_400(host):
    row = artifacts.create(session='theo',type='html_form',title='Findings',payload={
        'content':'<main><h1>Findings</h1></main>','version':'1','read_only':True})
    code, response = _request(host,'/artifacts/'+row['artifact_id']+'/submit',
                              {'submission_id':'http-report-submission-1234','version':'1','answers':{}})
    assert code == 400 and 'read-only report' in str(response)
    assert not html_forms.pending()


def test_report_revisions_keep_the_link_pin_and_old_publication_receipt(host, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sys
    from tests.integration.test_attention_questions_http import TOKEN
    spec = importlib.util.spec_from_file_location('report_revision_cli', Path(__file__).resolve().parents[2] / 'scripts/agent_artifacts.py')
    cli = importlib.util.module_from_spec(spec)
    previous_path = list(sys.path)
    spec.loader.exec_module(cli)
    sys.path[:] = previous_path
    monkeypatch.setattr(cli, '_config', lambda: (host.base, TOKEN))
    page = tmp_path / 'report.html'
    page.write_text('<main>First report</main>')
    arguments = ['theo', 'Findings', str(page), '--artifact-id', 'report-stable-link', '--version', '1']
    first = cli._create_report(arguments)
    path = '/artifacts/' + first['artifact_id']
    assert _request(host, path + '/pin', {'pinned': True})[0] == 200
    page.write_text('<script>const sample="<speak>verbatim HTML</speak>";</script><main>Updated report</main>')
    second = cli._create_report(arguments[:-1] + ['2'])
    assert second['artifact_id'] == first['artifact_id'] and second['pinned'] is True
    assert second['created_at'] == first['created_at'] and second['version'] == '2'
    assert second['content'] == page.read_text()
    assert cli._create_report(arguments[:-1] + ['2'])['updated_at'] == second['updated_at']
    page.write_text('<main>First report</main>')
    assert cli._create_report(arguments)['version'] == '2', 'A delayed retry cannot roll a newer report back'
    status, history = _request(host, path + '/revisions')
    assert status == 200 and [row['version'] for row in history['revisions']] == ['2', '1']
    status, original = _request(host, path + '/revisions?version=1')
    assert status == 200 and original['revision']['payload']['content'] == '<main>First report</main>'
    assert _request(host, path)[1]['artifact']['content'] == second['content']
    assert cli._report_history(['report-stable-link', '--version', '1'])['revision']['payload']['content'] == '<main>First report</main>'
    assert cli._report_history(['report-stable-link', '--offset', '2'])['revisions'] == []
    assert not host.deliveries and not html_forms.pending()


def test_report_patch_rotates_cache_identity_and_preserves_interactive_receipts(host):
    status, created = _request(host, '/artifacts', {'session': 'theo', 'type': 'html_form',
        'title': 'Report', 'artifact_id': 'report-patch',
        'payload': {'content': '<main>Original</main>', 'version': '1', 'read_only': True}})
    assert status == 201
    path = '/artifacts/report-patch'
    status, revised = _request(host, path, {'payload_patch': {'content': '<main>New</main>'}})
    assert status == 200 and revised['artifact']['version'] != '1'
    assert revised['artifact']['read_only'] is True
    assert _request(host, path, {'payload_patch': {'read_only': False}})[0] == 409
    assert _request(host, path, {'payload_patch': {'answer_schema': {'type': 'object'}}})[0] == 409
    form = artifacts.create(session='theo', type='html_form', title='Answered form', payload={
        'content': '<form><input name="x"></form>', 'version': '1',
        'answer_schema': {'type': 'object', 'properties': {'x': {'type': 'string'}}}})
    body = {'submission_id': 'report-immutable-form-receipt', 'version': '1', 'answers': {'x': 'keep'}}
    receipt = _request(host, '/artifacts/' + form['artifact_id'] + '/submit', body)[1]
    assert _request(host, '/artifacts/' + form['artifact_id'], {'payload': {
        'content': '<main>Do not replace answers</main>', 'version': '2', 'read_only': True}})[0] == 409
    assert artifacts.get(form['artifact_id'])['version'] == '1'
    assert _request(host, '/artifacts/' + form['artifact_id'] + '/submit', body)[1] == receipt


def test_report_revision_conflicts_do_not_overwrite_another_publisher(host, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from lib import agents
    status, _ = _request(host, '/artifacts', {'session': 'theo', 'type': 'html_form',
        'artifact_id': 'report-concurrent', 'title': 'Concurrent report',
        'payload': {'content': '<main>Base</main>', 'version': '1', 'read_only': True}})
    assert status == 201
    def revise(version):
        return _request(host, '/artifacts/report-concurrent', {'expected_version': '1',
            'payload_patch': {'version': version, 'content': '<main>' + version + '</main>'}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(revise, ['2', '3']))
    assert sorted(code for code, _ in results) == [200, 409]
    winner = next(body['artifact'] for code, body in results if code == 200)
    assert artifacts.get('report-concurrent')['version'] == winner['version']
    assert _request(host, '/artifacts/report-concurrent/revisions')[1]['revisions'][0]['version'] == winner['version']
    agents.create_agent(persona='Other', voice_id='V', cwd=str(tmp_path), session='other')
    assert _request(host, '/artifacts', {'session': 'other', 'type': 'html_form',
        'artifact_id': 'report-concurrent', 'title': 'Another owner',
        'payload': {'content': '<main>Do not take over</main>', 'version': '4', 'read_only': True}})[0] == 409
    assert artifacts.get('report-concurrent')['session'] == 'theo'
    assert _request(host, '/artifacts/report-concurrent/revisions', authenticated=False)[0] == 401


@pytest.mark.parametrize("status", ["ready", "active"])
def test_archived_form_accepts_answers_until_closed_or_discarded(host, status):
    from lib import db
    row = artifacts.create(session='theo', type='html_form', title='Archived plan', status=status, payload={
        'content': '<form><input name="priority"></form>', 'version': '1',
        'answer_schema': {'type': 'object', 'properties': {'priority': {'type': 'string'}},
                          'required': ['priority'], 'additionalProperties': False}})
    path = '/artifacts/' + row['artifact_id']
    code, archived = _request(host, path + '/archive', {'archived': True, 'expected_updated_at': row['updated_at']})
    assert code == 200 and archived['artifact']['archived_at'] is not None
    body = {'submission_id': 'archived-form-receipt-123456', 'version': '1', 'answers': {'priority': 'keep'}}
    code, receipt = _request(host, path + '/submit', body)
    assert code == 200 and receipt['accepted'] is True and receipt['delivery_status'] == 'pending'
    assert _request(host, path + '/submit', body)[1] == receipt
    assert db.conn().execute('SELECT COUNT(*) FROM form_submissions').fetchone()[0] == 1
    assert artifacts.get(row['artifact_id'])['archived_at'] is not None
    assert _request(host, path + '/submit', {**body, 'submission_id': 'stale-archived-receipt-123456', 'version': '2'})[0] == 409
    assert _request(host, path + '/submit', {**body, 'submission_id': 'invalid-archived-receipt-1234', 'answers': {}})[0] == 409
    artifacts.update(row['artifact_id'], {'status': 'completed'})
    assert _request(host, path + '/submit', {**body, 'submission_id': 'closed-form-receipt-123456'})[0] == 409
    assert _request(host, path + '/submit', body)[1] == receipt
    artifacts.update(row['artifact_id'], {'status': status})
    restored = artifacts.get(row['artifact_id'])
    assert _request(host, path + '/discard', {'expected_updated_at': restored['updated_at']})[0] == 200
    assert _request(host, path + '/submit', {**body, 'submission_id': 'deleted-form-receipt-123456'})[0] == 409
    assert _request(host, path + '/submit', body)[1] == receipt
    assert db.conn().execute('SELECT COUNT(*) FROM form_submissions').fetchone()[0] == 1


def test_network_cli_and_authenticated_renderer_preserve_version_policy(host, tmp_path, monkeypatch):
    import importlib.util
    import sys
    import urllib.request
    import urllib.error
    from pathlib import Path
    from tests.integration.test_attention_questions_http import TOKEN
    spec = importlib.util.spec_from_file_location('network_artifact_cli', Path(__file__).resolve().parents[2] / 'scripts/agent_artifacts.py')
    cli = importlib.util.module_from_spec(spec)
    previous_path = list(sys.path)
    spec.loader.exec_module(cli)
    sys.path[:] = previous_path
    monkeypatch.setattr(cli, '_config', lambda: (host.base, TOKEN))
    page = tmp_path / 'network.html'
    page.write_text('<main>Network fixture</main>')
    schema = tmp_path / 'schema.json'
    schema.write_text('{"type":"object"}')
    args = ['theo', 'Network fixture', str(page), str(schema), '--artifact-id', 'form-network-policy', '--version', '1']
    with pytest.raises(ValueError, match='connect_origins'):
        cli._create_form(args + ['--connect', 'https://example.com/path'])
    assert artifacts.get('form-network-policy') is None
    row = cli._create_form(args + ['--connect', 'https://Storage.Googleapis.Com:443'])
    assert row['connect_origins'] == ['https://storage.googleapis.com']
    path = host.base + '/artifacts/' + row['artifact_id'] + '/html'
    request = urllib.request.Request(path, headers={'Authorization': 'Bearer ' + TOKEN})
    with urllib.request.urlopen(request) as response:
        body = response.read().decode()
        csp = response.headers['Content-Security-Policy']
        assert response.status == 200
        assert response.headers['Content-Type'].startswith('text/html')
        assert "sandbox allow-scripts;" in csp and 'allow-same-origin' not in csp
        assert 'connect-src https://storage.googleapis.com;' in csp
        assert "img-src data:;" in csp and "form-action 'none';" in csp
        assert '<main>Network fixture</main>' in body
    with pytest.raises(urllib.error.HTTPError) as unauthenticated:
        urllib.request.urlopen(path)
    assert unauthenticated.value.code == 401
    plain = cli._create_form([a.replace('form-network-policy', 'form-network-default') for a in args])
    request = urllib.request.Request(host.base + '/artifacts/' + plain['artifact_id'] + '/html', headers={'Authorization': 'Bearer ' + TOKEN})
    with urllib.request.urlopen(request) as response:
        assert "connect-src 'none';" in response.headers['Content-Security-Policy']
    report = cli._create_report(['theo', 'Report', str(page), '--artifact-id', 'report-network', '--version', '1', '--connect', 'https://Storage.Googleapis.Com:443'])
    assert report['payload']['connect_origins'] == ['https://storage.googleapis.com']
    assert not host.deliveries and not html_forms.pending()


def test_rendered_page_never_accepts_a_url_credential(host):
    import urllib.request
    import urllib.error
    from lib import device_pairing
    from tests.integration.test_attention_questions_http import TOKEN
    row = artifacts.create(session='theo', type='html_form', title='Report', payload={
        'content': '<main>Report</main>', 'version': '1', 'read_only': True,
        'connect_origins': ['https://storage.googleapis.com']})
    path = host.base + '/artifacts/' + row['artifact_id'] + '/html'

    def status(url, headers):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as response:
                return response.status, response.headers.get('Cache-Control')
        except urllib.error.HTTPError as error:
            return error.code, None

    assert status(path + '?token=' + TOKEN, {})[0] == 403, 'Page scripts can read document.URL'
    assert status(path, {'Authorization': 'Bearer ' + TOKEN}) == (200, 'no-store')
    assert status(path, {'Cookie': 'claude_pwa_token=' + TOKEN}) == (200, 'no-store')
    limited = device_pairing.exchange(device_pairing.issue(scope='limited')['code'],
                                      device_name='Limited report reader')['token']
    assert status(path, {'Authorization': 'Bearer ' + limited})[0] == 403
