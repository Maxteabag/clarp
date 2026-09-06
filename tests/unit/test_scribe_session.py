import io
import json
from unittest.mock import Mock
import pytest
from lib.scribe_session import create_session, ScribeSessionError


def test_mints_single_use_token_without_exposing_key():
    response = io.BytesIO(b'{"token":"single-use"}')
    opener = Mock(return_value=response)
    result = create_session(api_key='private-key', open_url=opener)
    request = opener.call_args.args[0]
    assert request.full_url == 'https://api.elevenlabs.io/v1/single-use-token/realtime_scribe'
    assert request.get_header('Xi-api-key') == 'private-key'
    assert request.method == 'POST'
    assert result == {'token': 'single-use', 'model_id': 'scribe_v2_realtime',
                      'audio_format': 'pcm_16000', 'commit_strategy': 'manual'}
    assert 'private-key' not in json.dumps(result)


def test_missing_key_never_calls_provider():
    opener = Mock()
    with pytest.raises(ScribeSessionError):
        create_session(api_key='', open_url=opener)
    opener.assert_not_called()


@pytest.mark.parametrize('body', [b'{}', b'{"token":null}', b'{"token":""}', b'[]', b'not json'])
def test_malformed_token_response_is_rejected(body):
    with pytest.raises(ScribeSessionError):
        create_session(api_key='key', open_url=Mock(return_value=io.BytesIO(body)))


def test_provider_exception_is_sanitized():
    with pytest.raises(ScribeSessionError) as error:
        create_session(api_key='secret', open_url=Mock(side_effect=OSError('secret URL')))
    assert 'secret' not in str(error.value)
