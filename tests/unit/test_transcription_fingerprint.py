"""What a transcription id stands for (lib.transcription_results.request_fingerprint, R87)."""
import hashlib

from lib.transcription_results import request_fingerprint


def test_a_chat_clips_fingerprint_is_unchanged():
    """Results cached for chat clips before this change still answer their retries."""
    digest = hashlib.sha256()
    for part in (b"audio", b"\0", b"audio/wav", b"\0", b"whisper", b"\0", b"1"):
        digest.update(part)
    assert request_fingerprint(b"audio", "audio/wav", "whisper", True) == digest.hexdigest()


def test_a_call_clip_binds_its_principal_call_and_session_and_none_bleeds_into_another():
    base = dict(principal="device_a", call_id="gcl_1", session="rachel")
    chat = request_fingerprint(b"audio", "audio/wav", "whisper", False)
    in_call = request_fingerprint(b"audio", "audio/wav", "whisper", False, call=base)
    assert in_call != chat
    for field, other in (("principal", "device_b"), ("call_id", "gcl_2"), ("session", "mike")):
        assert request_fingerprint(b"audio", "audio/wav", "whisper", False,
                                   call={**base, field: other}) != in_call, field
    # Netstrings: moving characters from one field to the next is another request.
    assert request_fingerprint(b"a", "t", "m", False, call=dict(principal="ab", call_id="c", session="s")) != \
        request_fingerprint(b"a", "t", "m", False, call=dict(principal="a", call_id="bc", session="s"))
