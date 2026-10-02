"""Gemini Live behind the GPT-Live event vocabulary of Oracle v2.

The stable Oracle v2 Conversation (oracle_live_stable.py) talks to GPT-Live
over one WebSocket in `session.*` events. `GeminiUpstream` is that socket for
Gemini's BidiGenerateContent, so routing, relays, memory, earcons and handoffs
run unchanged whichever model speaks. Verified against gemini-3.8-live on
2026-10-02:

- `session.start` becomes `setup`. GPT-Live's startup history items have no
  Gemini equivalent at setup time, so their text joins the system instruction.
- Phone audio is 24 kHz PCM16. Gemini resamples a declared rate, so it goes up
  as `audio/pcm;rate=24000`; replies come back as 24 kHz PCM, as before.
- GPT-Live's delegation channel becomes a non-blocking `delegate_to_agent`
  function. Its call is surfaced as `session.delegation.created`, the Host
  routes it as usual, and what the route appends while it runs is returned as
  that call's `functionResponse` (spoken when the model is idle).
- Other appends become `clientContent`: commentary completes a turn so Oracle
  speaks it; thinking and instructions are silent context.
- `serverContent.interrupted` (Gemini's own barge-in) becomes
  `session.output_audio.interrupted`, which the Host turns into a flush for
  the phone's playback buffer. There is no truncate: Gemini does not learn how
  much of the reply was heard.
- A connection lasts about ten minutes. `goAway` and an unexpected close
  reconnect with the latest session resumption handle, between turns when
  there is time; context window compression keeps long calls inside the
  context limit.
"""
from __future__ import annotations

import collections
import json
import re
import threading
import time
import uuid

URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
INPUT_MIME = "audio/pcm;rate=24000"
DELEGATE = "delegate_to_agent"
# Reconnect this long before a goAway deadline even mid-reply.
GOAWAY_MARGIN_SECONDS = 2.0
SETUP_TIMEOUT_SECONDS = 10.0
# After Gemini fails before a call is set up, new calls use GPT-Live this long,
# so the phone's automatic reconnect lands on a working voice.
FAILURE_HOLD_SECONDS = 600.0
_failed_at = None
# Gemini list prices per million tokens (paid tier, 2026-10-02).
PRICES = {("prompt", "AUDIO"): 3.0, ("prompt", "TEXT"): .75, ("prompt", "IMAGE"): .75,
          ("response", "AUDIO"): 12.0, ("response", "TEXT"): 4.5}

INSTRUCTIONS = """

On this call you reach the Host with the delegate_to_agent function. Call it,
with the user's request in their words, whenever the user wants anything an
agent or the Host handles: work for an agent, what an agent is doing or said,
reading or continuing a reply, or being put through to an agent. It does not
block: at most say a short acknowledgement, then keep listening. Its result is
the Host's answer. Text that starts with [Host] is never the user speaking:
[Host, speak] is for you to tell the user now in your own words; [Host,
context] is background you mention only when it matters.
"""


class GeminiUnavailable(RuntimeError):
    pass


def healthy(clock=time.monotonic):
    return _failed_at is None or clock() - _failed_at >= FAILURE_HOLD_SECONDS


def mark_failed(clock=time.monotonic):
    global _failed_at
    _failed_at = clock()


def mark_healthy():
    global _failed_at
    _failed_at = None


def _connect(api_key):
    import websocket
    connection = websocket.create_connection(URL + "?key=" + api_key, suppress_origin=True,
                                             timeout=20, enable_multithread=True)
    connection.settimeout(1)
    return connection


def _history_text(items):
    texts = []
    for item in items or ():
        for part in (item.get("content") or []) if isinstance(item, dict) else ():
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "\n\n".join(texts)


def _seconds(value):
    """A protobuf Duration string ("9.5s") as seconds; None when unreadable."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(value or ""))
    return float(match.group(1)) if match else None


class GeminiUpstream:
    """A GPT-Live-shaped upstream socket backed by a Gemini Live session."""

    provider = "gemini"

    def __init__(self, api_key, *, model, voice, clock=time.monotonic):
        if not api_key:
            raise GeminiUnavailable("Gemini Live needs a Gemini API key on this Host")
        self.api_key, self.model, self.voice, self.clock = api_key, model, voice, clock
        import websocket
        self.timeout_exc = websocket.WebSocketTimeoutException
        try:
            self.ws = _connect(api_key)
        except Exception:
            mark_failed(clock)
            raise
        self.lock = threading.RLock()
        self.events = collections.deque()
        self.setup = None
        self.started = False
        self.closed = False
        self.handle = None
        self.reconnect_at = None
        self.reconnects = 0
        self.opened_at = clock()
        # Gemini call id -> {"name", "parts": [text], "speak": bool}; the call
        # being routed collects the route's appends.
        self.calls = {}
        self.routing = None
        self.speaking = False
        # After the phone's interrupt, drop the rest of the current reply.
        self.muted = False
        self.tokens = collections.Counter()
        self.estimate_usd = 0.0

    # ---- Host -> Gemini ----------------------------------------------------

    def settimeout(self, value):
        self.ws.settimeout(value)

    def send(self, raw):
        event = json.loads(raw)
        kind = event.get("type")
        if kind == "session.start":
            self.setup = self.setup_message(event.get("session") or {})
            self._send({"setup": self.setup})
        elif kind == "session.input_audio.append":
            self._send({"realtimeInput": {"audio": {"data": event["audio"], "mimeType": INPUT_MIME}}},
                       drop_when_reconnecting=True)
        elif kind in ("session.commentary.append", "session.thinking.append", "session.instructions.append"):
            self.context(kind.split(".")[1], str(event.get("content") or ""))
        elif kind == "session.close":
            self.finish("client_closed")

    def setup_message(self, session):
        instructions = str(session.get("instructions") or "") + INSTRUCTIONS
        history = _history_text(session.get("input"))
        if history:
            instructions += "\n\n[Host, context] " + history
        return {"model": "models/" + self.model,
                "generationConfig": {"responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
                "systemInstruction": {"parts": [{"text": instructions}]},
                "tools": [{"functionDeclarations": [{
                    "name": DELEGATE, "behavior": "NON_BLOCKING",
                    "description": "Hand the user's request to the Clarp Host, which routes it to the right agent "
                                   "or answers from stored work. The result is the Host's answer.",
                    "parameters": {"type": "OBJECT", "required": ["request"], "properties": {
                        "request": {"type": "STRING", "description": "The user's request, in their words."}}}}]}],
                "realtimeInputConfig": {"automaticActivityDetection": {"silenceDurationMs": 700}},
                "inputAudioTranscription": {}, "outputAudioTranscription": {},
                "contextWindowCompression": {"slidingWindow": {}},
                "sessionResumption": {}}

    def context(self, kind, text):
        with self.lock:
            call = self.calls.get(self.routing) if self.routing else None
            if call is not None:
                call["parts"].append(text)
                call["speak"] = call["speak"] or kind == "commentary"
                return
        label = "[Host, speak] " if kind == "commentary" else "[Host, context] "
        self._send({"clientContent": {"turns": [{"role": "user", "parts": [{"text": label + text}]}],
                                      "turnComplete": kind == "commentary"}})

    def local_interrupt(self):
        """The phone cut playback: stop forwarding this reply's audio."""
        with self.lock:
            if self.speaking:
                self.muted = True

    def delegation_routing(self, ident):
        with self.lock:
            if ident in self.calls:
                self.routing = ident

    def delegation_finished(self, ident):
        with self.lock:
            if self.routing == ident:
                self.routing = None
            call = self.calls.pop(ident, None)
        if call is None:
            return
        if call.get("cancelled"):
            # Gemini dropped the call (the user spoke over it): what the route
            # produced still reaches Oracle, as ordinary context.
            for text in call["parts"]:
                self.context("commentary" if call["speak"] else "thinking", text)
            return
        result = "\n\n".join(call["parts"]) or (
            "Sent to the Host. Any agent work reports back later; do not say it is done.")
        self._send({"toolResponse": {"functionResponses": [{"id": ident, "name": call["name"],
            "response": {"result": result, "scheduling": "WHEN_IDLE"}}]}})

    def _send(self, message, *, drop_when_reconnecting=False):
        with self.lock:
            if self.closed or self.ws is None:
                return
            try:
                self.ws.send(json.dumps(message))
            except Exception:
                if drop_when_reconnecting and self.handle:
                    return
                raise

    def close(self):
        with self.lock:
            self.closed = True
            ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def finish(self, reason):
        self.events.append({"type": "session.closed", "reason": reason, "usage": self.usage()})
        self.close()

    # ---- Gemini -> Host ----------------------------------------------------

    def recv(self):
        while True:
            if self.events:
                return json.dumps(self.events.popleft())
            if self.closed:
                return ""
            if self.reconnect_at is not None and (not self.speaking or self.clock() >= self.reconnect_at):
                self.resume("go_away")
                continue
            ws = self.ws
            try:
                raw = ws.recv()
            except self.timeout_exc:
                raise
            except Exception:
                if self.closed:
                    continue  # deliver session.closed queued by finish()
                if self.resume("connection_lost"):
                    continue
                self.lost()
                raise
            if not raw:
                if self.closed:
                    continue
                if self.resume("connection_lost"):
                    continue
                self.lost()
                return ""
            self.translate(json.loads(raw))

    def lost(self):
        if not self.started:
            mark_failed(self.clock)  # rejected setup: model, voice or key

    def resume(self, reason):
        """Reconnect with the latest resumption handle; False when there is none."""
        with self.lock:
            self.reconnect_at = None
            if self.closed or not self.handle or self.setup is None:
                return False
            old, self.ws = self.ws, None
            try:
                if old is not None:
                    old.close()
            except Exception:
                pass
            ws = _connect(self.api_key)
            ws.send(json.dumps({"setup": {**self.setup, "sessionResumption": {"handle": self.handle}}}))
            deadline = self.clock() + SETUP_TIMEOUT_SECONDS
            while True:
                try:
                    raw = ws.recv()
                except self.timeout_exc:
                    if self.clock() >= deadline:
                        raise GeminiUnavailable("Gemini Live did not resume the session")
                    continue
                if not raw:
                    raise GeminiUnavailable("Gemini Live closed while resuming")
                if "setupComplete" in json.loads(raw):
                    break
            self.ws = ws
            self.reconnects += 1
            self.speaking = self.muted = False
        self.events.append({"type": "oracle_v2.provider_observation",
                            "source": {"type": "gemini.resumed", "reason": reason, "reconnects": self.reconnects}})
        return True

    def translate(self, message):
        if "setupComplete" in message and not self.started:
            self.started = True
            mark_healthy()
            self.events.append({"type": "session.started", "session": {"model": self.model,
                                "voice": self.voice, "provider": "gemini"}})
        update = message.get("sessionResumptionUpdate")
        if isinstance(update, dict) and update.get("resumable") and update.get("newHandle"):
            self.handle = update["newHandle"]
        if "goAway" in message:
            left = _seconds((message.get("goAway") or {}).get("timeLeft"))
            self.reconnect_at = self.clock() + max(0.0, (left if left is not None else 5.0) - GOAWAY_MARGIN_SECONDS)
        if isinstance(message.get("usageMetadata"), dict):
            self.count_usage(message["usageMetadata"])
            self.events.append({"type": "session.usage.updated", "usage": self.usage()})
        content = message.get("serverContent")
        if isinstance(content, dict):
            self.server_content(content)
        tool_call = message.get("toolCall")
        if isinstance(tool_call, dict):
            for call in tool_call.get("functionCalls") or ():
                self.function_call(call)
        cancelled = message.get("toolCallCancellation")
        if isinstance(cancelled, dict):
            with self.lock:
                for ident in cancelled.get("ids") or ():
                    call = self.calls.get(ident)
                    if call is not None:
                        call["cancelled"] = True

    def server_content(self, content):
        if content.get("interrupted"):
            with self.lock:
                self.speaking = self.muted = False
            self.events.append({"type": "session.output_audio.interrupted"})
        for part in (content.get("modelTurn") or {}).get("parts") or ():
            data = (part.get("inlineData") or {}).get("data") if isinstance(part, dict) else None
            if isinstance(data, str) and data:
                with self.lock:
                    self.speaking = True
                    muted = self.muted
                if not muted:
                    self.events.append({"type": "session.output_audio.delta", "delta": data})
        now_ms = int((self.clock() - self.opened_at) * 1000)
        for field, kind in (("inputTranscription", "session.input_transcript.delta"),
                            ("outputTranscription", "session.output_transcript.delta")):
            text = (content.get(field) or {}).get("text")
            if isinstance(text, str) and text:
                self.events.append({"type": kind, "delta": text, "event_id": uuid.uuid4().hex,
                                    "start_ms": now_ms, "end_ms": now_ms})
        if content.get("turnComplete"):
            with self.lock:
                self.speaking = self.muted = False

    def function_call(self, call):
        ident, name = call.get("id"), call.get("name")
        if not isinstance(ident, str) or not ident:
            return
        if name != DELEGATE:
            self._send({"toolResponse": {"functionResponses": [{"id": ident, "name": name,
                "response": {"error": "Unknown function; use delegate_to_agent."}}]}})
            return
        with self.lock:
            self.calls[ident] = {"name": name, "parts": [], "speak": False}
        self.events.append({"type": "session.delegation.created",
                            "delegation": {"id": ident, "request": str((call.get("args") or {}).get("request") or "")}})

    # ---- usage ---------------------------------------------------------------

    def count_usage(self, usage):
        cost = 0.0
        for side in ("prompt", "response"):
            for row in usage.get(side + "TokensDetails") or ():
                if not isinstance(row, dict) or type(row.get("tokenCount")) is not int:
                    continue
                modality = str(row.get("modality") or "TEXT")
                self.tokens[side + "_" + modality.lower()] += row["tokenCount"]
                cost += row["tokenCount"] * PRICES.get((side, modality), PRICES[(side, "TEXT")]) / 1e6
        thoughts = usage.get("thoughtsTokenCount")
        if type(thoughts) is int:
            # Billed as output text; an estimate, not Google's invoice.
            self.tokens["thoughts"] += thoughts
            cost += thoughts * PRICES[("response", "TEXT")] / 1e6
        self.estimate_usd += cost

    def usage(self):
        return {"seconds": round(self.clock() - self.opened_at, 1), "tokens": dict(self.tokens),
                "voice_estimate_usd": round(self.estimate_usd, 6), "reconnects": self.reconnects}
