"""Oracle v2 on Gemini Live, at the Host boundary.

The stable Conversation drives a GeminiUpstream whose socket is a fake Gemini
BidiGenerateContent server: messages the Host sends are recorded, messages
the test queues are what Gemini says. No network.
"""
from __future__ import annotations

import array
import base64
import collections
import json
import threading
from types import SimpleNamespace

import pytest

from lib import (agents, oracle_calls_stable, oracle_delegations, oracle_gemini_live, oracle_live_stable,
                 oracle_memory, oracle_voice_provider)
from lib.oracle_live_usage import LiveUsage


from websocket import WebSocketTimeoutException as Timeout


class FakeGemini:
    def __init__(self):
        self.sent, self.inbox, self.closed = [], collections.deque(), False

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        if not self.inbox:
            raise Timeout()
        value = self.inbox.popleft()
        return value if isinstance(value, str) else json.dumps(value)

    def settimeout(self, _):
        pass

    def close(self):
        self.closed = True


def pcm(value, n=480):
    return base64.b64encode(array.array("h", [value] * n).tobytes()).decode()


@pytest.fixture(autouse=True)
def quiet_attention(monkeypatch):
    monkeypatch.setattr(oracle_live_stable.oracle_attention, "pending_decisions", lambda: [])
    monkeypatch.setattr(oracle_live_stable.oracle_attention, "pending_completion_notifications", lambda: [])
    oracle_gemini_live.mark_healthy()
    yield
    oracle_gemini_live.mark_healthy()


@pytest.fixture
def call(tmp_path, monkeypatch):
    """A direct-to-primary Oracle call with real memory and tools on Gemini."""
    agent = agents.create_agent(persona="Primary", voice_id="fixture", session="primary",
                                cwd=str(tmp_path), backend="codex")
    dispatched = []

    def dispatch(**values):
        dispatched.append(values)
        ident = values["delegation_id"]
        oracle_delegations.begin(delegation_id=ident, trace_id="trace-" + ident, client_msg_id="m-" + ident,
            agent_id=agent, session="primary", request_text=values["request_text"],
            owner_principal=values["owner_principal"])
        return oracle_delegations.get(ident)
    monkeypatch.setattr(oracle_delegations, "dispatch", dispatch)
    sockets = [FakeGemini(), FakeGemini()]
    opened = []

    def connect(key):
        opened.append(key)
        return sockets[len(opened) - 1]
    now = [100.0]
    monkeypatch.setattr(oracle_gemini_live, "_connect", connect)
    upstream = oracle_gemini_live.GeminiUpstream("gemini-key", model="gemini-3.8-live", voice="Kore",
        clock=lambda: now[0])
    store = oracle_memory.open_thread("phone", "primary", connection_id="c1")
    tools = oracle_calls_stable.AgentTools(SimpleNamespace(media_dir=tmp_path), "phone", "primary",
                                           lambda _: pytest.fail("must not cancel workers"))
    down = []
    c = oracle_live_stable.Conversation(upstream, down.append, tools, "", clock=lambda: now[0],
        memory=store, provider_session="c1", delegation_strategy="direct_contact")
    c.usage = LiveUsage("gemini")
    c.send({"type": "session.start", "session": oracle_live_stable.live_config(
        history=store.startup_history(roster={"agents": []}), delegation_strategy="direct_contact")})
    sockets[0].inbox.append({"setupComplete": {}})
    rig = SimpleNamespace(c=c, upstream=upstream, sockets=sockets, opened=opened, down=down, now=now,
                          dispatched=dispatched, store=store)
    pump(rig)
    yield rig
    c.stop.set()
    c.pool.shutdown(wait=True)


def pump(rig):
    """Deliver everything the fake Gemini queued, as serve()'s pump thread does."""
    while True:
        try:
            raw = rig.upstream.recv()
        except Timeout:
            return
        if not raw:
            return
        rig.c.receive(json.loads(raw))


def gemini(rig, socket=0):
    return rig.sockets[socket].sent


def test_setup_carries_prompt_voice_delegate_tool_vad_transcripts_and_resumption(call):
    setup = gemini(call)[0]["setup"]
    assert setup["model"] == "models/gemini-3.8-live"
    assert setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"] == {"voiceName": "Kore"}
    instructions = setup["systemInstruction"]["parts"][0]["text"]
    assert "delegate_to_agent" in instructions and "Saved Oracle reference data" in instructions
    [tool] = setup["tools"][0]["functionDeclarations"]
    assert tool["name"] == "delegate_to_agent" and tool["behavior"] == "NON_BLOCKING"
    assert setup["realtimeInputConfig"]["automaticActivityDetection"]["silenceDurationMs"] >= 500
    assert setup["inputAudioTranscription"] == {} and setup["outputAudioTranscription"] == {}
    assert "slidingWindow" in setup["contextWindowCompression"] and setup["sessionResumption"] == {}
    assert call.down[0]["type"] == "session.started"


def test_audio_goes_up_at_declared_24k_and_comes_down_as_output_deltas(call):
    audio = pcm(3000)
    call.c.input({"type": "session.input_audio.append", "audio": audio})
    assert gemini(call)[-1] == {"realtimeInput": {"audio": {"data": audio, "mimeType": "audio/pcm;rate=24000"}}}
    reply = pcm(4000)
    call.sockets[0].inbox.append({"serverContent": {"modelTurn": {"parts": [
        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": reply}}]}}})
    pump(call)
    assert call.down[-1] == {"type": "session.output_audio.delta", "delta": reply}


def test_gemini_barge_in_flushes_phone_playback(call):
    call.sockets[0].inbox.append({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": pcm(4000)}}]}}})
    call.sockets[0].inbox.append({"serverContent": {"interrupted": True}})
    pump(call)
    assert [e["type"] for e in call.down[-2:]] == ["oracle_v2.playback_flush", "oracle_v2.quiet"]
    assert call.c.last_output_active is False


def test_phone_interrupt_drops_the_rest_of_the_reply_until_the_turn_ends(call):
    call.sockets[0].inbox.append({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": pcm(4000)}}]}}})
    pump(call)
    call.c.input({"type": "oracle_v2.interrupt"})
    call.sockets[0].inbox.append({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": pcm(4000)}}]}}})
    pump(call)
    assert sum(e["type"] == "session.output_audio.delta" for e in call.down) == 1
    call.sockets[0].inbox.append({"serverContent": {"turnComplete": True}})
    call.sockets[0].inbox.append({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": pcm(4000)}}]}}})
    pump(call)
    assert sum(e["type"] == "session.output_audio.delta" for e in call.down) == 2


def test_transcripts_reach_oracle_memory_and_the_phone(call):
    call.sockets[0].inbox.append({"serverContent": {"inputTranscription": {"text": "Spør Primary "}}})
    call.sockets[0].inbox.append({"serverContent": {"inputTranscription": {"text": "om bygget."}}})
    call.sockets[0].inbox.append({"serverContent": {"outputTranscription": {"text": "Jeg spør."}}})
    pump(call)
    assert [(f["role"], f["text"]) for f in call.c.fragments] == [("user", "Spør Primary om bygget."),
                                                                  ("assistant", "Jeg spør.")]
    from lib import db
    stored = [json.loads(row["event_json"])["delta"] for row in db.conn().execute(
        "SELECT event_json FROM oracle_observations WHERE thread_id=? ORDER BY rowid", (call.store.thread_id,))]
    assert stored == ["Spør Primary ", "om bygget.", "Jeg spør."]
    assert [e["type"] for e in call.down[-3:]] == ["session.input_transcript.delta"] * 2 + [
        "session.output_transcript.delta"]


def test_delegate_call_runs_the_host_route_and_answers_with_a_function_response(call):
    call.sockets[0].inbox.append({"serverContent": {"inputTranscription": {"text": "Ask Primary to check the build."}}})
    call.sockets[0].inbox.append({"toolCall": {"functionCalls": [
        {"id": "call-1", "name": "delegate_to_agent", "args": {"request": "Ask Primary to check the build."}}]}})
    pump(call)
    call.now[0] += 2  # past the router's transcript settle time
    deadline = threading.Event()
    for _ in range(200):
        if call.c.routing == 0:
            break
        deadline.wait(.02)
    assert call.c.routing == 0
    assert "Ask Primary to check the build." in call.dispatched[0]["request_text"]
    [response] = [m["toolResponse"]["functionResponses"][0] for m in gemini(call) if "toolResponse" in m]
    assert response["id"] == "call-1" and response["name"] == "delegate_to_agent"
    assert response["response"]["scheduling"] == "WHEN_IDLE"
    assert "primary" in response["response"]["result"].lower()


def test_appends_outside_a_route_become_client_content(call):
    call.c.append("commentary", "Mira finished the report.")
    call.c.append("thinking", "Background fact.")
    spoken, silent = [m["clientContent"] for m in gemini(call)[-2:]]
    assert spoken["turnComplete"] is True and spoken["turns"][0]["parts"][0]["text"] == "[Host, speak] Mira finished the report."
    assert silent["turnComplete"] is False and silent["turns"][0]["parts"][0]["text"].startswith("[Host, context] ")


def test_go_away_resumes_on_a_new_connection_with_the_latest_handle(call):
    call.sockets[0].inbox.append({"sessionResumptionUpdate": {"newHandle": "h-1", "resumable": True}})
    call.sockets[0].inbox.append({"sessionResumptionUpdate": {"newHandle": "h-2", "resumable": True}})
    call.sockets[0].inbox.append({"goAway": {"timeLeft": "8s"}})
    call.sockets[1].inbox.append({"setupComplete": {}})
    pump(call)
    assert call.sockets[0].closed and len(call.opened) == 2
    resumed = gemini(call, 1)[0]["setup"]
    assert resumed["sessionResumption"] == {"handle": "h-2"}
    assert resumed["systemInstruction"] == gemini(call)[0]["setup"]["systemInstruction"]
    audio = pcm(3000)
    call.c.input({"type": "session.input_audio.append", "audio": audio})
    assert gemini(call, 1)[-1]["realtimeInput"]["audio"]["data"] == audio
    assert sum(e["type"] == "session.started" for e in call.down) == 1  # the phone sees one call


def test_lost_connection_without_a_handle_ends_the_call(monkeypatch):
    socket = FakeGemini()
    monkeypatch.setattr(oracle_gemini_live, "_connect", lambda _: socket)
    upstream = oracle_gemini_live.GeminiUpstream("k", model="m", voice="v")
    socket.inbox.append("")
    assert upstream.recv() == ""
    assert not oracle_gemini_live.healthy()  # never set up: the next call uses GPT-Live


def test_usage_is_priced_from_tokens_and_reported_as_gemini_billing(call):
    call.sockets[0].inbox.append({"usageMetadata": {"promptTokenCount": 675, "responseTokenCount": 76,
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 329}, {"modality": "AUDIO", "tokenCount": 319}],
        "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 76}], "thoughtsTokenCount": 76}})
    pump(call)
    usage = [e for e in call.down if e["type"] == "oracle_v2.usage"][-1]
    assert usage["billing"] == "gemini_api"
    expected = (329 * .75 + 319 * 3 + 76 * 12 + 76 * 4.5) / 1e6
    assert usage["voice_estimate_usd"] == pytest.approx(expected, abs=1e-6)


def test_provider_choice_defaults_to_openai_and_needs_a_gemini_key(monkeypatch):
    cfg = SimpleNamespace(oracle_voice_provider="openai", gemini_key=lambda: "", oracle_gemini_model="",
                          oracle_gemini_voice="", gemini_voice_for=lambda _: None, gemini_voice="Kore")
    assert oracle_voice_provider.effective(cfg) == "openai"
    oracle_voice_provider.set_provider("gemini")
    status = oracle_voice_provider.status(cfg)
    assert status["selected"] == "gemini" and status["provider"] == "openai" and status["fallback_reason"]
    cfg.gemini_key = lambda: "key"
    assert oracle_voice_provider.status(cfg)["provider"] == "gemini"
    assert oracle_voice_provider.gemini_model(cfg) == "gemini-3.8-live"
    with pytest.raises(ValueError):
        oracle_voice_provider.set_provider("claude")


def test_unreachable_gemini_falls_back_to_gpt_live_for_the_call(monkeypatch):
    monkeypatch.setattr(oracle_live_stable, "_connect_upstream", lambda key: "openai-socket")
    attempts = []

    def refuse(key):
        attempts.append(key)
        raise OSError("no route")
    monkeypatch.setattr(oracle_gemini_live, "_connect", refuse)
    cfg = SimpleNamespace(gemini_key=lambda: "key", oracle_gemini_model="gemini-3.8-live", oracle_gemini_voice="",
                          gemini_voice_for=lambda _: None, gemini_voice="Kore")
    assert oracle_live_stable._connect_voice(cfg, "openai-key", "gemini") == ("openai-socket", "openai")
    # Held off afterwards: the phone's reconnect does not try Gemini again.
    assert oracle_live_stable._connect_voice(cfg, "openai-key", "gemini") == ("openai-socket", "openai")
    assert attempts == ["key"]


def test_closing_while_the_reader_waits_still_delivers_session_closed(monkeypatch):
    class ClosingSocket(FakeGemini):
        def recv(self):
            upstream.finish("client_closed")  # the Host closes while this read is blocked
            raise ConnectionError("socket closed")
    monkeypatch.setattr(oracle_gemini_live, "_connect", lambda _: ClosingSocket())
    upstream = oracle_gemini_live.GeminiUpstream("k", model="m", voice="v")
    assert json.loads(upstream.recv())["type"] == "session.closed"
    assert upstream.recv() == ""
