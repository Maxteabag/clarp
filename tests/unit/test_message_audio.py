import datetime

from lib import agents, clip_store, tts_queue
from lib.db import conn, now_ms
from lib.message_audio import retained_events
from lib.voice_markup import spoken_chunks_for_tts


def message(agent_id, text, ident='message'):
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn().execute('INSERT INTO messages(message_id,agent_id,seq,role,timestamp,text,updated_at) VALUES(?,?,1,?,?,?,?)',
                   (ident, agent_id, 'assistant', stamp, text, now_ms()))
    return ident


def saved(agent_id, root, text, name, trace='turn'):
    path = root / name
    path.write_bytes(b'retained audio')
    clip = clip_store.record_clip(agent_id=agent_id, path=str(path), trace_id=trace,
                                  runtime_id=lambda _: None)
    queue = tts_queue.enqueue(agent_id=agent_id, session='ada', text=text, voice_id='voice', source='pwa', trace_id=trace)
    tts_queue.mark_done(queue, clip_id=clip)
    clip_store.mark_clip_status(clip_id=clip, status='play-ok')
    return clip, path


def test_replays_saved_prefixed_speech_and_keeps_markup_and_order(tmp_path):
    agent = agents.create_agent(persona='Ada', voice_id='voice', cwd=str(tmp_path), session='ada')
    first = 'Hello <vox>um</vox> there. <break time="350ms"/> I have an update.'
    second = 'The work is complete.'
    ident = message(agent, f'<speak>{first}</speak>Written details.<speak>{second}</speak>')
    one, _ = saved(agent, tmp_path, 'Ada here. ' + spoken_chunks_for_tts(first)[0], 'first.mp3')
    two, _ = saved(agent, tmp_path, spoken_chunks_for_tts(second)[0], 'second.mp3')
    events = retained_events(session='ada', message_id=ident, audio_dir=tmp_path)
    assert [event['clip_id'] for event in events] == [one, two]
    assert events[0]['url'] == '/audio/first.mp3'
    assert conn().execute('SELECT status FROM clips WHERE clip_id=?', (one,)).fetchone()['status'] == 'play-ok'
    assert tts_queue.pending_count() == 0


def test_message_and_audio_are_scoped_to_the_same_agent(tmp_path):
    ada = agents.create_agent(persona='Ada', voice_id='voice', cwd=str(tmp_path), session='ada')
    agents.create_agent(persona='Lin', voice_id='voice', cwd=str(tmp_path), session='lin')
    ident = message(ada, '<speak>Exact speech.</speak>')
    saved(ada, tmp_path, 'Exact speech.', 'speech.mp3')
    assert retained_events(session='lin', message_id=ident, audio_dir=tmp_path) == []


def test_missing_segment_or_deleted_file_does_not_return_partial_speech(tmp_path):
    ada = agents.create_agent(persona='Ada', voice_id='voice', cwd=str(tmp_path), session='ada')
    ident = message(ada, '<speak>First part.</speak><speak>Second part.</speak>')
    saved(ada, tmp_path, 'First part.', 'first.mp3')
    assert retained_events(session='ada', message_id=ident, audio_dir=tmp_path) == []
    _, path = saved(ada, tmp_path, 'Second part.', 'second.mp3')
    assert len(retained_events(session='ada', message_id=ident, audio_dir=tmp_path)) == 2
    path.unlink()
    assert retained_events(session='ada', message_id=ident, audio_dir=tmp_path) == []


def test_audio_outside_retained_audio_root_is_rejected(tmp_path):
    ada = agents.create_agent(persona='Ada', voice_id='voice', cwd=str(tmp_path), session='ada')
    ident = message(ada, '<speak>Exact speech.</speak>')
    saved(ada, tmp_path, 'Exact speech.', 'outside.mp3')
    root = tmp_path / 'audio'
    root.mkdir()
    assert retained_events(session='ada', message_id=ident, audio_dir=root) == []


def test_raw_pcm_reuses_recorded_stream_format(tmp_path):
    ada = agents.create_agent(persona='Ada', voice_id='voice', cwd=str(tmp_path), session='ada')
    ident = message(ada, '<speak>Exact speech.</speak>')
    clip, _ = saved(ada, tmp_path, 'Exact speech.', 'speech.pcm')
    fmt = {'encoding': 'pcm_s16le', 'sample_rate': 24000, 'channels': 1, 'bytes_per_sample': 2}
    agents.record_sse_event({'type': 'audio', 'session': 'ada', 'clip_id': clip, 'url': f'/clips/{clip}/stream', 'audio_format': fmt})
    event = retained_events(session='ada', message_id=ident, audio_dir=tmp_path)[0]
    assert event['audio_format'] == fmt
    assert event['url'] == f'/clips/{clip}/stream'
    assert event['stream_url'] == event['url']


def test_reply_retention_is_bounded_and_leaves_preview_expiry_intact(tmp_path):
    import os
    import time
    from lib.audio_stream import AudioStream
    from lib.message_audio import retained_mp3_paths
    ada = agents.create_agent(persona='Ada', voice_id='voice', cwd=str(tmp_path), session='ada')
    _, old = saved(ada, tmp_path, 'Old reply.', 'old.mp3')
    _, new = saved(ada, tmp_path, 'New reply.', 'new.mp3')
    old.write_bytes(b'o' * 10)
    new.write_bytes(b'n' * 10)
    preview = tmp_path / 'preview.mp3'
    preview.write_bytes(b'preview')
    for path in [old, new, preview]:
        os.utime(path, (time.time() - 1200, time.time() - 1200))
    assert retained_mp3_paths(audio_dir=tmp_path, max_age_ms=86400000, max_bytes=10) == {str(new)}
    stream = AudioStream(tmp_path)
    stream.REPLAY_RETAIN_MAX_BYTES = 10
    stream._prune_audio()
    assert new.exists()
    assert not old.exists()
    assert not preview.exists()
    conn().execute('UPDATE tts_queue SET enqueued_at=?', (now_ms() - 8 * 86400000,))
    stream._prune_audio()
    assert not new.exists()
