import json
import stat

from lib.oracle_diagnostics import OracleJournal
from lib.oracle_realtime import _safe_client_event


def test_journal_keeps_transcripts_and_call_identity_but_not_audio_or_headers(tmp_path):
    journal = OracleJournal(tmp_path)
    journal.event('server', json.dumps({'type': 'conversation.item.input_audio_transcription.completed',
        'item_id': 'user-1', 'transcript': 'Ask Marcus', 'Authorization': 'Bearer secret'}))
    journal.event('server', json.dumps({'type': 'response.function_call_arguments.done',
        'call_id': 'call-1', 'name': 'delegate_to_agent', 'arguments': '{"agent":"Marcus"}'}))
    journal.event('server', json.dumps({'type': 'response.output_audio.delta', 'delta': 'PRIVATE_AUDIO'}))
    journal.event('client', json.dumps({'type': 'input_audio_buffer.append', 'audio': 'PRIVATE_INPUT'}))
    journal.close()
    text = journal.path.read_text()
    assert 'Ask Marcus' in text and 'call-1' in text
    assert 'secret' not in text and 'PRIVATE_' not in text
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in text.splitlines()]
    assert [r['sequence'] for r in rows] == [1, 2, 3]
    assert rows[-1]['fields']['approx_audio_bytes']['client'] > 0


def test_journal_size_limit_does_not_interrupt_voice(tmp_path):
    journal = OracleJournal(tmp_path, max_bytes=1)
    journal.record('large', {'text': 'hello'})
    assert journal.failed
    journal.record('after_limit')
    assert journal.path.stat().st_size == 0


def test_transcription_is_host_owned_and_only_enabled_explicitly():
    raw = json.dumps({'type': 'session.update', 'session': {'audio': {'input': {
        'transcription': {'model': 'client-chosen'}}}}})
    normal = json.loads(_safe_client_event(raw, model='fixture', voice='cedar'))
    assert 'transcription' not in normal['session']['audio']['input']
    diagnostic = json.loads(_safe_client_event(raw, model='fixture', voice='cedar',
        transcription_model='gpt-4o-mini-transcribe'))
    assert diagnostic['session']['audio']['input']['transcription']['model'] == 'gpt-4o-mini-transcribe'


def test_live_transcripts_and_audio_counts_without_payload(tmp_path):
    import json
    from lib.oracle_diagnostics import OracleJournal
    journal = OracleJournal(tmp_path)
    journal.event('server', json.dumps({'type':'session.output_transcript.delta','delta':'Hello','start_ms':1,'end_ms':2}))
    journal.event('client', json.dumps({'type':'session.input_audio.append','audio':'AAAA'}))
    journal.event('server', json.dumps({'type':'session.output_audio.delta','delta':'BBBB'}))
    journal.close()
    text = journal.path.read_text()
    assert 'Hello' in text and 'start_ms' in text
    assert 'AAAA' not in text and 'BBBB' not in text
    assert journal.audio_bytes == {'client':3,'server':3}
