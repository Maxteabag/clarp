"""Validate authenticated native realtime results for normal Host processing."""
import base64
import binascii
import hashlib

MODEL = 'elevenlabs:scribe_v2_realtime'
MAX_AUDIO_BYTES = 25 * 1024 * 1024


def parse_result(data):
    if not isinstance(data, dict):
        raise ValueError('expected JSON object')
    text = data.get('text')
    audio = data.get('audio_base64')
    job_id = data.get('transcription_id')
    hands_free = data.get('hands_free', False)
    if not isinstance(text, str) or len(text.encode('utf-8')) > 32768:
        raise ValueError('invalid final transcript')
    if not isinstance(audio, str) or len(audio) > (MAX_AUDIO_BYTES * 4 // 3 + 4):
        raise ValueError('invalid audio')
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError('transcription id required')
    if not isinstance(hands_free, bool):
        raise ValueError('hands_free must be boolean')
    try:
        decoded = base64.b64decode(audio, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('invalid audio encoding') from None
    if not decoded or len(decoded) > MAX_AUDIO_BYTES:
        raise ValueError('invalid audio size')
    from .transcription_results import normalize_job_id, request_fingerprint
    job_id = normalize_job_id(job_id)
    fingerprint = request_fingerprint(decoded, 'audio/wav', MODEL, hands_free)
    # A final-text change under the same durable ID is a collision, not a retry.
    fingerprint = hashlib.sha256((fingerprint + '\0' + text).encode()).hexdigest()
    return decoded, text, job_id, hands_free, fingerprint
