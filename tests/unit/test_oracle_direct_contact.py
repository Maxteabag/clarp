import json
from types import SimpleNamespace

import pytest

from lib import agents, oracle_calls, oracle_codex_session, oracle_delegations, oracle_live, oracle_memory


def test_direct_contact_has_no_operator_and_preserves_original_dialogue(monkeypatch, tmp_path):
    agent = agents.create_agent(persona="Sage", voice_id="fixture", session="sage", cwd=str(tmp_path))
    calls=[]
    def dispatch(**values):
        calls.append(values)
        ident=values["delegation_id"]
        oracle_delegations.begin(delegation_id=ident,trace_id="trace",client_msg_id="message",agent_id=agent,
            session=values["session"],request_text=values["request_text"],owner_principal=values["owner_principal"])
        return oracle_delegations.get(ident)
    def forbidden(*args,**kwargs):raise AssertionError("Direct contact must not instantiate or call Luna operator")
    monkeypatch.setattr(oracle_delegations,"dispatch",dispatch)
    monkeypatch.setattr(oracle_codex_session,"CodexRouterSession",forbidden)
    ctx=SimpleNamespace(media_dir=tmp_path)
    tools=oracle_calls.AgentTools(ctx,"phone","sage",lambda session:None)
    memory=oracle_memory.open_thread("phone","sage",connection_id="voice")
    sent=[];events=[]
    c=oracle_live.Conversation(SimpleNamespace(send=sent.append),events.append,tools,"",
        clock=lambda:100,router_backend="codex",router_reuse=True,delegation_strategy="direct_contact",
        route_request=forbidden,memory=memory)
    try:
        c.fragments=[{"role":"user","text":"Check the invoice I mentioned."},
                     {"role":"user","source":"typed","text":"invoice_550.json; expected550, not50"}]
        c.revision=2;c.routing=1;c.route("voice-delegation")
        assert len(calls)==1 and calls[0]["session"]=="sage"
        assert "invoice_550.json; expected550, not50" in calls[0]["request_text"]
        snapshot=json.loads(next((tmp_path/"oracle-context"/memory.thread_id).glob("*.json")).read_text())
        assert snapshot["original_user_messages"][0]["text"]=="Check the invoice I mentioned."
        assert any(e.get("operator_model_called") is False for e in events)
        assert not any(e.get("type")=="oracle_v2.notice" for e in events)
        assert memory.admissions()[0]["status"]=="completed"
        assert c.router_session is None
    finally:c.stop.set();c.pool.shutdown()


def test_unknown_architecture_is_rejected_before_startup():
    with pytest.raises(ValueError,match="Unsupported"):
        oracle_live.Conversation(None,None,None,"",delegation_strategy="invented")


def test_direct_correction_during_materialization_does_not_dispatch(monkeypatch,tmp_path):
    agents.create_agent(persona='Primary',session='primary',voice_id='fixture',cwd=str(tmp_path))
    sent=[];calls=[]
    monkeypatch.setattr(oracle_delegations,'dispatch',lambda **kw:calls.append(kw))
    tools=oracle_calls.AgentTools(SimpleNamespace(media_dir=tmp_path),'phone','primary',lambda _:None)
    memory=oracle_memory.open_thread('phone','primary',connection_id='voice')
    c=oracle_live.Conversation(SimpleNamespace(send=sent.append),lambda e:None,tools,'',clock=lambda:100,
        delegation_strategy='direct_contact',memory=memory,
        route_request=lambda *a,**k:pytest.fail('Luna must not run'))
    original=memory.materialize_reference
    def correction(*a,**k):
        result=original(*a,**k)
        c.receive({'type':'session.input_transcript.delta','delta':'No, only check availability.',
                   'start_ms':5000,'end_ms':6000})
        return result
    monkeypatch.setattr(memory,'materialize_reference',correction)
    try:
        c.fragments=[{'role':'user','text':'Book the meeting.','end_ms':100}]
        c.revision=1;c.routing=1;c.route('request')
        assert calls==[]
    finally:c.stop.set();c.pool.shutdown()
