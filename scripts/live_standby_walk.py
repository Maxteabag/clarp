#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiortc>=1.9", "websocket-client>=1.8", "numpy>=1.26"]
# ///
"""GPT-Live standby on one real WebRTC call: the walk issue 473 must see before
`[openai] live_idle_timeout_s` is switched on for the owner.

It creates one WebRTC session the way the Host's Oracle WebRTC path does (the
Host's own LiveWire builds the session, with standby), attaches the sideband as
the Host does, and walks five steps, recording every provider event with its time:

  1. first wake: silence for --first-wait seconds; does inference stay asleep
     (no session.resumed, no speech) until someone speaks?
  2. wake: a spoken utterance; expect session.resumed, then an answer
  3. pause: silence past the idle timeout; expect session.paused
  4. a result appended while paused (session.commentary.append, then
     session.instructions.append, as the Host appends agent results): does
     either wake the session or get spoken?
  5. wake again by speech: is the appended result spoken now?

Then it closes the session gracefully and prints the usage (active_seconds,
paused_seconds). It writes, under --out: events.jsonl (every event, the key
never), model-audio.wav (what the model said) and summary.json.

The key comes only from the environment variable named by --key-env (default
OPENAI_API_KEY); it is sent only in the Authorization header to api.openai.com
and never printed or written. Run it with no Clarp Host at all:

    OPENAI_API_KEY=... uv run --script scripts/live_standby_walk.py --wake-audio a.wav b.wav

Wake audio: two short WAV files of a person speaking (any rate, mono or stereo,
16-bit). Without --wake-audio the script asks OpenAI's speech API
(gpt-4o-mini-tts, deprecated with shutdown 6 Jan 2027) for two Norwegian lines
first, before the paid Live session opens.

Cost at the documented rates (pricing, GPT-Live: $0.05/min active, $0.02/min
standby, per second): about 3 minutes, mostly in standby: under $0.15, plus a
fraction of a cent for the speech lines. `--plan` prints the session body and the
timeline and calls nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import fractions
import io
import json
import os
import pathlib
import sys
import threading
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from lib.oracle_live_wire import LiveWire  # noqa: E402 - the Host's own session builder

API = "https://api.openai.com/v1/live/sessions"
SPEECH = "https://api.openai.com/v1/audio/speech"
RATE = 48000
FRAME = 960  # 20 ms
WAKE_LINES = ("Hei, kan du høre meg? Svar med én kort setning.",
              "Hei igjen. Har du fått noe nytt å fortelle meg?")
INSTRUCTIONS = ("Du er en kort og vennlig norsk assistent i en test. Svar på norsk bokmål med én kort setning. "
                "Når du får et resultat fra bakgrunnsarbeid, si det til brukeren.")
RESULT_COMMENTARY = "Resultat fra bakgrunnsarbeid: rapporten er ferdig, tre funn."
RESULT_INSTRUCTIONS = "Si nå til brukeren, på norsk: «Rapporten er ferdig, og den har tre funn.»"


def key_from(env):
    value = os.environ.get(env, "")
    if not value.strip():
        sys.exit(f"Set the OpenAI key in the environment variable {env} (it is read from there only).")
    return value.strip()


def scrub(text, key):
    return str(text).replace(key, "[key]") if key else str(text)


def session_body(model, idle):
    return LiveWire("api", model=model, idle_timeout_s=idle).session(INSTRUCTIONS, webrtc=True)


def pcm16_mono_48k(raw_wav):
    """Any 16-bit WAV to mono 48 kHz PCM16 bytes (linear interpolation)."""
    import numpy as np
    with wave.open(io.BytesIO(raw_wav)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("wake audio must be 16-bit PCM WAV")
        rate, channels = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(data):
        positions = np.arange(0, len(data) - 1, rate / RATE)
        data = np.interp(positions, np.arange(len(data)), data)
    return np.clip(data, -32768, 32767).astype("<i2").tobytes()


def speech_wav(text, key):
    """One line from OpenAI's speech API, as WAV bytes (before the Live session opens)."""
    from urllib.request import Request, urlopen
    body = json.dumps({"model": "gpt-4o-mini-tts", "voice": "alloy", "input": text,
                       "response_format": "wav"}).encode()
    request = Request(SPEECH, body, {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        return response.read()


class Microphone:
    """The call's microphone track: silence, or queued speech, paced in real time."""

    def __init__(self):
        from aiortc.mediastreams import MediaStreamTrack

        outer = self

        class Track(MediaStreamTrack):
            kind = "audio"

            def __init__(self):
                super().__init__()
                self.start, self.pts = None, 0

            async def recv(self):
                import av
                if self.start is None:
                    self.start = time.time()
                else:
                    self.pts += FRAME
                    wait = self.start + self.pts / RATE - time.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
                frame.planes[0].update(outer.next_chunk())
                frame.pts, frame.sample_rate, frame.time_base = self.pts, RATE, fractions.Fraction(1, RATE)
                return frame

        self.lock, self.pending = threading.Lock(), bytearray()
        self.track = Track()

    def say(self, pcm):
        with self.lock:
            self.pending += pcm

    def speaking(self):
        with self.lock:
            return bool(self.pending)

    def next_chunk(self):
        size = FRAME * 2
        with self.lock:
            chunk, self.pending[:size] = bytes(self.pending[:size]), b""
        return chunk + b"\0" * (size - len(chunk))


class Journal:
    def __init__(self, out, key):
        self.out, self.key, self.t0, self.lock = out, key, time.monotonic(), threading.Lock()
        self.events, self.file = [], open(out / "events.jsonl", "w", encoding="utf-8")

    def record(self, source, event):
        entry = {"t": round(time.monotonic() - self.t0, 2), "source": source, **event}
        line = scrub(json.dumps(entry, ensure_ascii=False), self.key)
        with self.lock:
            self.events.append(json.loads(line))
            self.file.write(line + "\n")
            self.file.flush()
        kind = event.get("type", "")
        if kind not in ("session.output_audio.delta", "session.input_audio.append"):
            text = event.get("delta") or event.get("note") or ""
            print(f"{entry['t']:8.2f}s {source:8} {kind} {text}"[:160], flush=True)

    def seen(self, kind, since=0.0):
        with self.lock:
            return [e for e in self.events if e.get("type") == kind and e["t"] >= since]

    def now(self):
        return round(time.monotonic() - self.t0, 2)


def sideband(session_id, key, journal, stop):
    import websocket
    socket = websocket.create_connection(f"{API.replace('https:', 'wss:')}/{session_id}/attach",
                                         header={"Authorization": "Bearer " + key}, suppress_origin=True, timeout=15)
    socket.settimeout(.5)

    def reader():
        while not stop.is_set():
            try:
                raw = socket.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:  # noqa: BLE001 - the walk records it and goes on
                journal.record("sideband", {"type": "sideband.lost", "note": scrub(exc, key)})
                return
            if not raw:
                return
            journal.record("provider", json.loads(raw))
    threading.Thread(target=reader, daemon=True).start()
    return socket


async def walk(args, key, out):
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from urllib.request import Request, urlopen
    import av

    wakes = [pathlib.Path(p).read_bytes() for p in args.wake_audio] if args.wake_audio else \
        [speech_wav(line, key) for line in WAKE_LINES]
    wakes = [pcm16_mono_48k(raw) for raw in wakes]
    journal = Journal(out, key)
    mic = Microphone()
    pc = RTCPeerConnection()
    pc.addTrack(mic.track)
    pc.createDataChannel("oai-events")   # the label the Live WebRTC guide uses; events are read on the sideband
    heard = wave.open(str(out / "model-audio.wav"), "wb")
    heard.setnchannels(1)
    heard.setsampwidth(2)
    heard.setframerate(RATE)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)

    @pc.on("track")
    def on_track(track):
        async def record():
            while True:
                try:
                    frame = await track.recv()
                except Exception:  # noqa: BLE001 - the call ended
                    return
                for part in resampler.resample(frame):
                    heard.writeframes(part.to_ndarray().tobytes())
        asyncio.ensure_future(record())

    await pc.setLocalDescription(await pc.createOffer())
    body = {"session": session_body(args.model, args.idle), "transport": {"type": "webrtc", "sdp": pc.localDescription.sdp}}
    journal.record("client", {"type": "walk.create", "session": {k: v for k, v in body["session"].items()
                                                                 if k != "instructions"}})
    request = Request(API, json.dumps(body).encode(), {"Authorization": "Bearer " + key,
                                                       "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            created = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 - the provider's refusal is the finding
        detail = getattr(exc, "read", lambda: b"")()
        sys.exit("create failed: " + scrub(f"{exc} {detail[:2000]!r}", key))
    session_id = created["session"]["id"]
    journal.record("client", {"type": "walk.created", "note": session_id})
    stop = threading.Event()
    side = sideband(session_id, key, journal, stop)
    await pc.setRemoteDescription(RTCSessionDescription(created["transport"]["sdp"], "answer"))

    async def wait(seconds, until=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if until and until():
                return True
            await asyncio.sleep(.1)
        return bool(until and until())

    def append(event):
        side.send(json.dumps(event))
        journal.record("client", event)

    findings = {}
    # 1. The first wake: nothing before speech.
    t = journal.now()
    await wait(args.first_wait)
    findings["1_asleep_before_speech"] = {
        "session.resumed": bool(journal.seen("session.resumed", t)),
        "model_spoke": bool(journal.seen("session.output_transcript.delta", t))}
    # 2. Wake by speech.
    t = journal.now()
    mic.say(wakes[0])
    journal.record("client", {"type": "walk.speak", "note": "wake 1"})
    resumed = await wait(30, lambda: journal.seen("session.resumed", t))
    await wait(15)
    findings["2_wake_by_speech"] = {"session.resumed": resumed,
                                    "answer": "".join(e.get("delta", "") for e in journal.seen("session.output_transcript.delta", t))}
    # 3. Pause after the idle timeout.
    t = journal.now()
    paused = await wait(args.idle + 20, lambda: journal.seen("session.paused", t))
    findings["3_paused"] = {"session.paused": paused, "after_s": round(journal.now() - t, 1)}
    # 4. A result appended while paused.
    for name, event in (("commentary", {"type": "session.commentary.append", "content": RESULT_COMMENTARY,
                                        "delegation_id": None, "event_id": "walk-commentary"}),
                        ("instructions", {"type": "session.instructions.append", "content": RESULT_INSTRUCTIONS,
                                          "delegation_id": None, "event_id": "walk-instructions"})):
        t = journal.now()
        append(event)
        await wait(15)
        findings[f"4_{name}_while_paused"] = {
            "acknowledged": [e["type"] for e in journal.events if e["t"] >= t and e.get("client_event_id") == event["event_id"]],
            "session.resumed": bool(journal.seen("session.resumed", t)),
            "spoken": "".join(e.get("delta", "") for e in journal.seen("session.output_transcript.delta", t))}
    # 5. Wake again by speech.
    t = journal.now()
    mic.say(wakes[1])
    journal.record("client", {"type": "walk.speak", "note": "wake 2"})
    resumed = await wait(30, lambda: journal.seen("session.resumed", t))
    await wait(20)
    findings["5_wake_again"] = {"session.resumed": resumed,
                                "answer": "".join(e.get("delta", "") for e in journal.seen("session.output_transcript.delta", t))}
    # Close gracefully and read the final usage.
    t = journal.now()
    append({"type": "session.close"})
    await wait(15, lambda: journal.seen("session.closed", t))
    closed = journal.seen("session.closed", t)
    findings["usage"] = closed[-1].get("usage") if closed else "no session.closed seen"
    stop.set()
    try:
        side.close()
    finally:
        await pc.close()
        heard.close()
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="gpt-live-1.1", choices=("gpt-live-1", "gpt-live-1.1"))
    parser.add_argument("--idle", type=int, default=20, help="session.idle_timeout_s, 5 to 60 (default 20)")
    parser.add_argument("--first-wait", type=float, default=10, help="seconds of silence before the first speech")
    parser.add_argument("--wake-audio", nargs=2, metavar="WAV", help="two WAV files of someone speaking")
    parser.add_argument("--key-env", default="OPENAI_API_KEY", help="the environment variable holding the key")
    parser.add_argument("--out", default=f"/var/tmp/live-standby-walk-{time.strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--plan", action="store_true", help="print the create body and the steps; call nothing")
    args = parser.parse_args()
    body = session_body(args.model, args.idle)
    if args.plan:
        print(json.dumps({"POST": API, "session": body, "transport": {"type": "webrtc", "sdp": "<the offer>"}},
                         ensure_ascii=False, indent=1))
        print(__doc__.split("\n\n")[1])
        return
    key = key_from(args.key_env)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    findings = asyncio.run(walk(args, key, out))
    (out / "summary.json").write_text(scrub(json.dumps(findings, ensure_ascii=False, indent=1), key), encoding="utf-8")
    print(json.dumps(findings, ensure_ascii=False, indent=1))
    print(f"\nWritten: {out}/events.jsonl, model-audio.wav, summary.json")


if __name__ == "__main__":
    main()
