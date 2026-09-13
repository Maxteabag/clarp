"""Public Live versus Codex subscription wire contracts.

Observed with real WebRTC sessions on 2026-09-13. The Codex compatibility
contract is corroborated by openai/codex 516f2780, protocol.rs and
methods_frameless_bidi.rs. Keep this boundary explicit: changing only a token
leaves public context commands invalid on the subscription endpoint.
"""
from __future__ import annotations

import copy
import math


class LiveWire:
    def __init__(self, mode="api"):
        if mode not in ("api", "subscription"):
            raise ValueError("Unsupported Live authentication mode")
        self.mode = mode
        self.model = "gpt-live-1" if mode == "api" else "gpt-live-1-codex"
        self.voice = "marin" if mode == "api" else "cove"

    def session(self, instructions, *, history=(), webrtc=True):
        audio = {"output": {"voice": self.voice}}
        if not webrtc:
            if self.mode != "api":
                raise ValueError("Subscription voice requires WebRTC")
            audio["format"] = {"type": "audio/pcm", "rate": 24000}
        result = {"model": self.model, "instructions": instructions,
                  "audio": audio, "delegation": {"type": "client"}}
        if history:
            result["input" if self.mode == "api" else "initial_items"] = list(history)
        return result

    def context(self, kind, text, *, delegation_id=None, event_id=None):
        if kind not in ("commentary", "thinking", "instructions"):
            raise ValueError("Unsupported context channel")
        if self.mode == "api":
            value = {"type": "session."+kind+".append", "content": text,
                     "delegation_id": delegation_id}
            if event_id: value["event_id"] = event_id
            return value
        value = {"type": "delegation.context.append" if delegation_id else "session.context.append",
                 "content": [{"type": "input_text", "text": text}]}
        # The omitted-channel form followed an exact spoken directive in the
        # retained subscription-voice-05 trial. Interruption timing has its own
        # acceptance story; a valid directive is not proof of local playback cut.
        if kind != "instructions":
            value["channel"] = "speakable" if kind == "commentary" else "commentary"
        if delegation_id: value["delegation_item_id"] = delegation_id
        return value

    def incoming(self, event):
        """Normalize transport vocabulary, preserving original source semantics.

        A subscription turn.done is kept as a provider observation, never a
        claim that the device finished playback or that a user heard it.
        """
        if self.mode == "api":
            return copy.deepcopy(event)
        value = copy.deepcopy(event)
        kind = value.get("type")
        transcripts = {"input_transcript.added": "session.input_transcript.delta",
                       "output_transcript.added": "session.output_transcript.delta"}
        if kind in transcripts:
            item = value.get("item") or {}
            if not isinstance(item.get("text"), str):
                raise ValueError("Invalid subscription transcript")
            value.update(type=transcripts[kind], delta=item["text"], source_type=kind,
                         source_item_id=item.get("id"))
        elif kind == "delegation.created":
            item = value.get("item") or {}
            if item.get("type") != "delegation" or item.get("target") != "client":
                return {"type": "oracle_v2.provider_observation", "source": value}
            value.update(type="session.delegation.created", delegation={"id": item.get("id")},
                         source_type=kind)
        elif kind == "output_audio.delta":
            value.update(type="session.output_audio.delta", delta=value.get("audio"), source_type=kind)
        elif kind == "input_audio.append":
            value.update(type="session.input_audio.append", source_type=kind)
        elif kind == "turn.done":
            value = {"type": "oracle_v2.provider_turn_done", "source": value}
        if kind in ("session.closed", "session.usage.updated"):
            usage = value.get("usage") or {}
            duration = usage.get("audio_duration_ms")
            if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
                value["usage"] = {**usage, "seconds": duration / 1000}
            value["billing"] = "chatgpt_subscription"
        return value
