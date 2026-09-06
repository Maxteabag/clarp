"""Mint short-lived Scribe credentials; native turn detection remains authoritative."""
from __future__ import annotations

import json
import urllib.request

TOKEN_URL = 'https://api.elevenlabs.io/v1/single-use-token/realtime_scribe'


class ScribeSessionError(RuntimeError):
    pass


def create_session(*, api_key: str, open_url=None) -> dict:
    if not api_key:
        raise ScribeSessionError('ElevenLabs API key is not configured')
    request = urllib.request.Request(
        TOKEN_URL, data=b'', method='POST', headers={'xi-api-key': api_key})
    try:
        with (open_url or urllib.request.urlopen)(request, timeout=10) as response:
            payload = json.loads(response.read(65536))
        token = payload.get('token') if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ValueError('missing token')
    except Exception:
        # Provider exceptions can contain credentials/URLs. Do not log or relay them.
        raise ScribeSessionError('Could not create realtime transcription session') from None
    return {'token': token, 'model_id': 'scribe_v2_realtime',
            'audio_format': 'pcm_16000', 'commit_strategy': 'manual'}
