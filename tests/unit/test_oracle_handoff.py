import json
import re
import threading
from types import SimpleNamespace
from lib import oracle_handoff, oracle_live_stable, oracle_live
import pytest


def test_immutable_private_transcription_preserves_original_over_summary(tmp_path):
    dialogue = [dict(role='user', text='Open office on workspace two and put us on the terrace.'),
                dict(role='assistant', text='Which office?'),
                dict(role='user', text='Delegate that directly to main.')]
    reference = oracle_handoff.materialize(tmp_path, dialogue, 'Open office; do not infer terrace.')
    paths = list((tmp_path/'oracle-handoffs').glob('*.json'))
    assert len(paths) == 1 and paths[0].stat().st_mode & 0o777 == 0o600
    value = json.loads(paths[0].read_text())
    assert value['original_user_messages'] == [dialogue[0]['text'], dialogue[2]['text']]
    assert str(paths[0]) in reference and 'not permission to repeat' in reference
    oracle_handoff.materialize(tmp_path, dialogue, 'next')
    assert json.loads(paths[0].read_text()) == value


def test_stable_dispatch_carries_original_terrace_clause(tmp_path, monkeypatch):
    calls=[]
    def execute(name,args,call_id):
        calls.append((name,args))
        return {'agents':[]} if name=='list_agents' else {'status':'accepted'}
    tools=SimpleNamespace(lock=threading.Lock(),delegations=set(),execute=execute,
                          ctx=SimpleNamespace(media_dir=tmp_path))
    proposal={'output':[{'type':'function_call','name':'investigate_with_oracle',
                         'arguments':json.dumps({'request':'Open office; do not infer terrace.'}),'call_id':'one'}]}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps(proposal).encode()
    monkeypatch.setattr(oracle_live_stable,'urlopen',lambda *a,**k:Response())
    c=oracle_live_stable.Conversation(SimpleNamespace(send=lambda x:None),lambda x:None,tools,'unused',clock=lambda:100)
    try:
        c.fragments=[dict(role='user',text='Put us on the terrace on workspace two.'),
                     dict(role='user',text='Delegate that directly to main.')]
        c.routing=1;c.route('one')
        request=calls[-1][1]['request']
        assert 'recent voice-transcription excerpt' in request
        payload=json.loads(next((tmp_path/'oracle-handoffs').glob('*.json')).read_text())
        assert 'terrace' in payload['original_user_messages'][0]
        assert payload['handoff_binding']['call_id'] == 'one'
        assert payload['handoff_binding']['recent_fragment_limit'] == 30
        assert payload['turn_boundaries_verified'] is False
        assert len(calls)==2
    finally:c.stop.set();c.pool.shutdown()


def test_long_user_fragment_not_silently_tail_truncated():
    tools=SimpleNamespace(supersede=None)
    c=oracle_live_stable.Conversation(SimpleNamespace(send=lambda x:None),lambda x:None,tools,'unused')
    try:
        c.receive({'type':'session.input_transcript.delta','delta':'DO NOT SEND. '+ 'x'*8100,'start_ms':0,'end_ms':10})
        c.receive({'type':'session.input_transcript.delta','delta':' final clause','start_ms':20,'end_ms':30})
        assert c.fragments[0]['text'].startswith('DO NOT SEND.')
    finally:c.stop.set();c.pool.shutdown()


@pytest.mark.parametrize("module", [oracle_live_stable, oracle_live])
def test_clarification_preserves_chronological_user_assistant_user_order(tmp_path, module):
    tools=SimpleNamespace(supersede=None)
    c=module.Conversation(SimpleNamespace(send=lambda x:None),lambda x:None,tools,'unused')
    try:
        for kind, text, start, end in [
            ('input', 'Book it.', 0, 100),
            ('output', 'Do you mean send the invite?', 110, 200),
            ('input', 'No, only check availability.', 210, 300),
            ('input', ' For fifteen minutes tomorrow.', 310, 400),
        ]:
            c.receive({'type':f'session.{kind}_transcript.delta','delta':text,'start_ms':start,'end_ms':end})
        expected=[('user','Book it.'),('assistant','Do you mean send the invite?'),
                  ('user','No, only check availability. For fifteen minutes tomorrow.')]
        assert [(row['role'],row['text']) for row in c.fragments] == expected
        oracle_handoff.materialize(tmp_path,c.fragments,'Check availability.')
        data=json.loads(next((tmp_path/'oracle-handoffs').glob('*.json')).read_text())
        assert [(row['role'],row['text']) for row in data['conversation']] == expected
        assert data['original_user_messages'][-1] == expected[-1][1]
    finally:c.stop.set();c.pool.shutdown()


def test_correction_during_reference_write_prevents_stale_dispatch(tmp_path, monkeypatch):
    calls=[]
    def execute(name,args,call_id):
        calls.append((name,args))
        return {'agents':[]} if name=='list_agents' else {'status':'accepted'}
    tools=SimpleNamespace(lock=threading.Lock(),delegations=set(),execute=execute,
                          ctx=SimpleNamespace(media_dir=tmp_path))
    proposal={'output':[{'type':'function_call','name':'investigate_with_oracle',
                         'arguments':json.dumps({'request':'Book the meeting.'}),'call_id':'one'}]}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps(proposal).encode()
    monkeypatch.setattr(oracle_live_stable,'urlopen',lambda *a,**k:Response())
    c=oracle_live_stable.Conversation(SimpleNamespace(send=lambda x:None),lambda x:None,tools,'unused',clock=lambda:100)
    original=oracle_handoff.materialize
    def corrected(*args, **kwargs):
        reference=original(*args, **kwargs)
        c.receive({'type':'session.input_transcript.delta','delta':'No, do not book anything.',
                   'start_ms':5000,'end_ms':6000})
        return reference
    monkeypatch.setattr(oracle_handoff,'materialize',corrected)
    try:
        c.fragments=[dict(role='user',text='Book the meeting.',end_ms=100)]
        c.routing=1;c.route('one')
        assert [name for name,args in calls] == ['list_agents']
    finally:c.stop.set();c.pool.shutdown()


def test_reference_write_failure_does_not_dispatch_summary(tmp_path, monkeypatch):
    calls=[]; notices=[]
    def execute(name,args,call_id):
        calls.append(name)
        return {'agents':[]}
    tools=SimpleNamespace(lock=threading.Lock(),delegations=set(),execute=execute,
                          ctx=SimpleNamespace(media_dir=tmp_path))
    proposal={'output':[{'type':'function_call','name':'investigate_with_oracle',
                         'arguments':json.dumps({'request':'Do work.'}),'call_id':'one'}]}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps(proposal).encode()
    monkeypatch.setattr(oracle_live_stable,'urlopen',lambda *a,**k:Response())
    def failure(*a,**k):raise OSError('simulated full disk')
    monkeypatch.setattr(oracle_handoff,'materialize',failure)
    c=oracle_live_stable.Conversation(SimpleNamespace(send=lambda x:None),notices.append,tools,'unused',clock=lambda:100)
    try:
        c.fragments=[dict(role='user',text='Do work.',end_ms=100)]
        c.routing=1;c.route('one')
        assert calls == ['list_agents']
        assert notices and notices[-1]['type'] == 'oracle_v2.notice'
        assert not list(tmp_path.rglob('*.json'))
    finally:c.stop.set();c.pool.shutdown()
