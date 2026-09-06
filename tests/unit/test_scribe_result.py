import base64
import pytest
from lib.scribe_result import parse_result


def body(**kwargs):
    return dict(audio_base64=base64.b64encode(b'WAV').decode(), text='hello',
                transcription_id='recording.1', hands_free=False, **kwargs)


def test_fingerprint_binds_final_text_and_audio():
    original = body()
    parsed = parse_result(original)
    assert parsed[:4] == (b'WAV', 'hello', 'recording.1', False)
    for field, value in [('text', 'changed'), ('audio_base64', 'T1RIRVI=')]:
        changed = dict(original, **{field: value})
        assert parse_result(changed)[4] != parsed[4]
    assert parse_result(original)[4] == parsed[4]


@pytest.mark.parametrize('field,value', [('audio_base64','!'), ('audio_base64',''),
    ('text',None), ('transcription_id',''), ('transcription_id','../bad'), ('hands_free','false')])
def test_rejects_malformed_result(field, value):
    data = body(); data[field] = value
    with pytest.raises(ValueError): parse_result(data)


def test_live_engine_has_no_fictional_vocabulary_payload():
    from lib.stt_providers import CATALOG
    from lib.vocab_compile import budget_for
    definition = next(row for row in CATALOG if row['id'] == 'elevenlabs')
    assert definition['turn_detection'] == 'native'
    model = next(row for row in definition['models'] if row['model'] == 'scribe_v2_realtime')
    assert model['biasing'] == 'none'
    assert budget_for('elevenlabs', 'scribe_v2_realtime').capacity == 0
