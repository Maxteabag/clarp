import json
from lib.oracle_result_context import result_context, PREFIX
from lib import oracle_memory


def test_long_request_cannot_crowd_out_finding_or_break_json():
    row = {'delegation_id':'task1','session':'agent1','status':'completed',
           'request_text':'Long reference instructions. '*500,
           'result_text':'The report is complete. No email was sent.'}
    context = result_context(row)
    assert len(context) <= 1500
    payload = json.loads(context[len(PREFIX):])
    assert payload['finding'] == row['result_text']
    assert payload['request_excerpt_truncated'] is True
    assert payload['finding_excerpt'] is False


def test_oversized_finding_is_explicit_excerpt_not_broken_wire_json():
    row = {'delegation_id':'task1','session':'agent1','status':'completed',
           'request_text':'Read only', 'result_text':'A finding with Unicode æ and quotes "x". '*500}
    context = result_context(row)
    assert len(context) <= 1500
    payload = json.loads(context[len(PREFIX):])
    assert payload['finding_excerpt'] is True
    assert row['result_text'].startswith(payload['finding'])


def test_restart_retains_complete_result_over_partial_spoken_repetition(monkeypatch):
    store = oracle_memory.open_thread('phone','primary',connection_id='one')
    store.save({'revision':3,'fragments':[{'role':'assistant','text':"The earlier task finished, but there is no"},
        {'role':'user','text':'Repeat the last answer'}]})
    result = 'The follow-up was not dispatched. The later task was admitted. Nothing was resent.'
    monkeypatch.setattr(store,'work',lambda:[{'delegation_id':'task1','session':'primary',
        'status':'completed','request_text':'Check routing','result_text':result}])
    history=store.startup_history()[0]['content'][0]['text']
    payload=json.loads(history.split('\n',1)[1])
    assert payload['work'][0]['finding'] == result
    assert 'not an incomplete spoken fragment' in payload['replay_guidance']
    assert any(r['text']=='Repeat the last answer' for r in payload['recent_conversation'])
    assert len(history.encode()) < 8192


def test_all_spoken_sections_survive_long_screen_only_report():
    row = {'delegation_id':'task1','session':'primary','status':'completed',
           'request_text':'Give both statuses',
           'result_text':'<speak>First task is complete.</speak>' + ('Screen-only evidence. '*300) +
                         '<speak>Second task is blocked; no deployment.</speak>'}
    payload=json.loads(result_context(row)[len(PREFIX):])
    assert payload['finding']=='First task is complete. Second task is blocked; no deployment.'
    assert payload['finding_excerpt'] is False
