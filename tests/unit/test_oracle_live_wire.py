import pytest

from lib.oracle_live_wire import LiveWire


def test_subscription_uses_the_observed_canonical_context_command_and_typed_content():
    wire = LiveWire("subscription")
    assert wire.context("commentary", "Ready.") == {"type": "session.context.append",
        "channel": "speakable", "content": [{"type": "input_text", "text": "Ready."}]}
    scoped = wire.context("thinking", "The audit is still running.", delegation_id="item-1")
    assert scoped["type"] == "delegation.context.append"
    assert scoped["delegation_item_id"] == "item-1"
    assert scoped["channel"] == "commentary"
    assert "delegation_id" not in scoped


def test_subscription_directives_use_the_observed_omitted_channel_form():
    assert LiveWire("subscription").context("instructions", "Stop speaking.") == {
        "type": "session.context.append", "content": [{"type": "input_text", "text": "Stop speaking."}]}


def test_protocols_have_distinct_model_voice_history_and_audio_transport_contracts():
    history = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Audit is pending."}]}]
    api = LiveWire().session("Be concise.", history=history)
    sub = LiveWire("subscription").session("Be concise.", history=history)
    assert api["model"] == "gpt-live-1" and api["audio"]["output"]["voice"] == "marin"
    assert sub["model"] == "gpt-live-1-codex" and sub["audio"]["output"]["voice"] == "cove"
    assert "input" in api and "initial_items" in sub
    assert "format" not in api["audio"] and "format" not in sub["audio"]
    with pytest.raises(ValueError, match="WebRTC"):
        LiveWire("subscription").session("Be concise.", webrtc=False)


def test_observed_transcript_and_final_usage_are_normalized_without_heard_claims():
    wire = LiveWire("subscription")
    incoming = {"type": "output_transcript.added", "start_ms": 0, "end_ms": 200,
                "item": {"id": "observed-item", "type": "output_transcript", "text": " Ready."}}
    normalized = wire.incoming(incoming)
    assert normalized["delta"] == " Ready."
    assert normalized["source_item_id"] == "observed-item"
    assert normalized["type"] == "session.output_transcript.delta"
    assert incoming["type"] == "output_transcript.added"
    closed = wire.incoming({"type": "session.closed", "usage": {"audio_duration_ms": 11800, "backend_model_usage": []}})
    assert closed["usage"]["seconds"] == 11.8
    assert closed["billing"] == "chatgpt_subscription"
    assert wire.incoming({"type": "turn.done", "turn": {"transcript": "Ready."}})["type"] == "oracle_v2.provider_turn_done"


def test_public_wire_preserves_scoped_append_and_never_mutates_input():
    wire = LiveWire()
    assert wire.context("instructions", "Listen.", delegation_id="live-1", event_id="event-1") == {
        "type": "session.instructions.append", "content": "Listen.", "delegation_id": "live-1", "event_id": "event-1"}
    source = {"type": "session.closed", "usage": {"seconds": 12}}
    value = wire.incoming(source); value["usage"]["seconds"] = 15
    assert source["usage"]["seconds"] == 12
