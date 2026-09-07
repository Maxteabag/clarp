"""Offline traces of the actual sideband policy; never creates a provider call."""
from lib.oracle_webrtc import ConversationController, conversational_finding


def controller():
    sent, ack = [], []
    c = ConversationController(send=sent.append, acknowledge=ack.append)
    return c, sent, ack


def result(c, ident='d1', message='m1'):
    c.add_result(ident, message, 'Marcus', 'The title is blue.')


def test_result_waits_for_user_and_automatic_reply():
    c, sent, ack = controller()
    c.event({'type': 'input_audio_buffer.speech_started'})
    result(c)
    assert sent == []
    c.event({'type': 'input_audio_buffer.speech_stopped'})
    assert sent == []
    c.event({'type': 'response.created', 'response': {'id': 'user-reply'}})
    c.event({'type': 'response.done', 'response': {'id': 'user-reply', 'status': 'completed'}})
    assert sent == []  # generation is not playback completion
    c.event({'type': 'output_audio_buffer.stopped', 'response_id': 'user-reply'})
    assert [x['type'] for x in sent] == ['conversation.item.create', 'response.create']
    assert ack == []


def test_cancelled_announcement_does_not_restart():
    c, sent, ack = controller()
    result(c)
    c.event({'type': 'response.created', 'response': {'id': 'summary'}})
    c.event({'type': 'input_audio_buffer.speech_started'})
    c.event({'type': 'response.done', 'response': {'id': 'summary', 'status': 'cancelled', 'status_details': {'reason': 'turn_detected'}}})
    c.event({'type': 'output_audio_buffer.cleared', 'response_id': 'summary'})
    for _ in range(10): result(c)
    assert len(sent) == 2
    assert ack == []


def test_shared_result_is_spoken_once_and_acknowledges_both_receipts():
    c, sent, ack = controller()
    result(c)
    result(c, 'd2')
    c.event({'type': 'response.created', 'response': {'id': 'summary'}})
    c.event({'type': 'response.done', 'response': {'id': 'summary', 'status': 'completed'}})
    assert ack == []
    c.event({'type': 'output_audio_buffer.stopped', 'response_id': 'summary'})
    assert sorted(ack) == ['d1', 'd2']
    assert len(sent) == 2


def test_token_limit_never_regenerates_forever():
    c, sent, ack = controller()
    result(c)
    c.event({'type': 'response.created', 'response': {'id': 'summary'}})
    c.event({'type': 'response.done', 'response': {'id': 'summary', 'status': 'incomplete', 'status_details': {'reason': 'max_output_tokens'}}})
    c.event({'type': 'output_audio_buffer.stopped', 'response_id': 'summary'})
    result(c)
    assert len(sent) == 2
    assert ack == []


def test_stop_never_acknowledges_late_playback():
    c, sent, ack = controller()
    result(c)
    c.event({'type': 'response.created', 'response': {'id': 'summary'}})
    c.close()
    c.event({'type': 'response.done', 'response': {'id': 'summary', 'status': 'completed'}})
    c.event({'type': 'output_audio_buffer.stopped', 'response_id': 'summary'})
    assert ack == []


def test_tool_wait_does_not_prevent_processing_speech():
    c, sent, ack = controller()
    c.tool_started()
    c.event({'type': 'input_audio_buffer.speech_started'})
    c.tool_finished('call-1', '{"agent":"Mira","status":"accepted"}')
    assert c.speaking
    assert [r['type'] for r in sent] == ['conversation.item.create']


def test_ack_failure_does_not_regenerate_audio():
    c, sent, ack = controller()
    def failed(_): raise OSError('offline')
    c.acknowledge = failed
    result(c)
    c.event({'type': 'response.created', 'response': {'id': 'summary'}})
    c.event({'type': 'response.done', 'response': {'id': 'summary', 'status': 'completed'}})
    c.event({'type': 'output_audio_buffer.stopped', 'response_id': 'summary'})
    c.flush()
    assert len(sent) == 2
    assert c.acks == {'d1'}
    c.acknowledge = ack.append
    c.flush()
    assert ack == ['d1']


def test_audio_stop_before_generation_done_still_acknowledges():
    c, sent, ack = controller()
    result(c)
    c.event({'type': 'response.created', 'response': {'id': 'summary'}})
    c.event({'type': 'output_audio_buffer.stopped', 'response_id': 'summary'})
    assert ack == []
    c.event({'type': 'response.done', 'response': {'id': 'summary', 'status': 'completed'}})
    assert ack == ['d1']


def test_injected_result_asks_for_finding_not_attribution():
    c, sent, ack = controller()
    result(c)
    text = sent[0]['item']['content'][0]['text']
    assert 'attributed summary' not in text
    assert 'Agent:' not in text
    assert 'now purple' in text
    assert 'single color word' in text


def test_conversational_finding_expands_logs_not_who_did_it():
    assert conversational_finding('Blue.') == 'The accent is blue.'
    assert conversational_finding('Checked site.css first and changed the accent color to purple.') == 'It’s now purple.'
    assert conversational_finding(
        'Fixed the missing title in preview.html by setting it to Quiet Harbor.'
    ) == 'The title is Quiet Harbor now.'
    who = 'Mira changed the accent from gray to blue in site.css.'
    assert conversational_finding(who) == who


def test_drop_agent_forgets_unheard_result():
    c, sent, ack = controller()
    c.event({'type': 'input_audio_buffer.speech_started'})
    result(c)
    assert sent == []
    c.drop_agent('Marcus')
    c.event({'type': 'input_audio_buffer.speech_stopped'})
    c.event({'type': 'response.created', 'response': {'id': 'user'}})
    c.event({'type': 'response.done', 'response': {'id': 'user', 'status': 'completed'}})
    c.event({'type': 'output_audio_buffer.stopped', 'response_id': 'user'})
    assert [x['type'] for x in sent] == []
    assert ack == ['d1']


def test_delegate_receipt_does_not_create_speech():
    c, sent, ack = controller()
    c.tool_started()
    c.tool_finished('call', '{"status":"accepted","note":"Stay silent."}', speak=False)
    assert [r['type'] for r in sent] == ['conversation.item.create']
    assert c.tool_ready is False


def test_read_tool_still_asks_for_a_spoken_answer():
    c, sent, ack = controller()
    c.tool_started()
    c.tool_finished('call', '{"messages":[]}', speak=True)
    assert [r['type'] for r in sent] == ['conversation.item.create', 'response.create']


def test_user_response_consumes_tool_output_without_duplicate_continuation():
    c, sent, ack = controller()
    c.tool_started()
    c.event({'type': 'input_audio_buffer.speech_started'})
    c.tool_finished('call', '{"status":"accepted"}')
    c.event({'type': 'input_audio_buffer.speech_stopped'})
    # Realtime's automatic user response sees the tool output already present.
    c.event({'type': 'response.created', 'response': {'id': 'user'}})
    c.event({'type': 'response.done', 'response': {'id': 'user', 'status': 'completed'}})
    c.event({'type': 'output_audio_buffer.stopped', 'response_id': 'user'})
    assert [r['type'] for r in sent] == ['conversation.item.create']
