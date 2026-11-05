"""VP-N1: existing clip publication payloads, including their byte order."""
import json
from pathlib import Path

import pytest

from lib.clip_delivery.raw_pcm import RawPcmSession
from lib.clip_delivery.chunked_file import ChunkedFileSession
from lib.clip_delivery.hls import HlsSession
from lib import tts_worker


class Stream:
    def __init__(self):
        self.payloads = []

    def broadcast(self, event):
        self.payloads.append(json.dumps(event))


class BrokenHerald:
    def ingest_clip(self, *_args, **_kwargs):
        raise RuntimeError('herald ingest unavailable')


def session(kind, tmp_path):
    common = dict(clip_id=41, agent={'agent_id': 'a', 'persona': 'Voice'},
                  voice_id='v', session='sample', source='pwa', text_len=6, trace_id=None)
    if kind == 'raw':
        return RawPcmSession(broker=None, target=tmp_path / '123__sample.pcm', **common)
    if kind == 'chunked':
        return ChunkedFileSession(broker=None, target=tmp_path / '123__sample.mp3', **common)
    # Only the real HLS session's publication properties are used here; do not
    # start ffmpeg/stderr threads for a producer serialization contract.
    value = object.__new__(HlsSession)
    value._clip_id = 41
    return value


def publish(kind, tmp_path, herald=None):
    stream = Stream()
    tts_worker._publish_clip_event(
        stream=stream, herald=herald, session=session(kind, tmp_path),
        row={'session': 'sample', 'voice_id': 'v', 'source': 'pwa', 'text': 'Speech'},
        agent={'agent_id': 'a', 'persona': 'Voice'}, trace_id=None)
    return stream.payloads


@pytest.mark.parametrize('herald', [None, BrokenHerald()], ids=['no-herald', 'herald-failed'])
def test_raw_clip_publishes_one_url_without_duplicate_keywords(tmp_path, herald):
    assert publish('raw', tmp_path, herald) == [
        '{"type": "audio", "url": "/clips/41/stream", "name": "123__sample.pcm", '
        '"session": "sample", "clip_id": 41, "agent_id": "a", "persona": "Voice", '
        '"trace_id": null, "delivery": "raw-pcm", "streamable": true, '
        '"stream_url": "/clips/41/stream", "audio_format": {"container": "raw", '
        '"encoding": "pcm_f32le", "sample_rate": 44100, "channels": 1}}']


@pytest.mark.parametrize('kind,expected', [
    ('chunked', '{"type": "audio", "url": "/audio/123__sample.mp3", "name": "123__sample.mp3", '
     '"session": "sample", "clip_id": 41, "agent_id": "a", "persona": "Voice", '
     '"trace_id": null, "streamable": true, "stream_url": "/clips/41/stream", '
     '"complete_url": "/clips/41/complete.mp3"}'),
    ('hls', '{"type": "audio", "url": "/clips/41/playlist.m3u8", "name": "clip-41", '
     '"session": "sample", "clip_id": 41, "agent_id": "a", "persona": "Voice", '
     '"trace_id": null, "streamable": true, "delivery": "hls", '
     '"playlist_url": "/clips/41/playlist.m3u8"}'),
])
def test_other_delivery_payloads_keep_their_exact_bytes(tmp_path, kind, expected):
    assert publish(kind, tmp_path) == [expected]
