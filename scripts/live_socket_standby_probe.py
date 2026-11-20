#!/usr/bin/env python3
"""Does a Host-held WebSocket GPT-Live session take standby? (issue 473)

The docs offer `session.wake_policy` and `session.idle_timeout_s` only on the
WebRTC create; they say nothing for a WebSocket `session.start`. The Rust Host
has only WebSocket sessions, so this asks the API once and keeps its answer
VERBATIM:

  1. `session.start` on wss://api.openai.com/v1/live/sessions with the Host's
     own WebSocket session (LiveWire, voice marin, PCM 24 kHz) plus
     `wake_policy: {type: "speech"}` and `idle_timeout_s: 20` (--variant to send
     only one of them). The first answer is kept whole: `session.started`
     (accepted; its echo of the session says whether the fields were kept or
     dropped) or `error` (refused, with its text).
  2. If accepted: silence for --first-wait seconds (does anything happen before
     speech?), then, with --wake-audio, one spoken line (session.resumed?), then
     silence for --idle + 20 seconds: does `session.paused` ever arrive?
  3. `session.close`, waiting for `session.closed` and its usage (does it show
     `paused_seconds`?).

Every provider event is written to events.jsonl (audio deltas only counted) and
the findings to summary.json under --out. The key comes only from the
environment variable named by --key-env (default OPENAI_API_KEY); it goes only
into the Authorization header to api.openai.com and is never printed or
written. No Clarp Host is needed:

    OPENAI_API_KEY=... uv run --frozen python scripts/live_socket_standby_probe.py --wake-audio line.wav

Cost at the documented rate ($0.05 a minute active, per second; WebSocket is
billed active): about 1.5 minutes, under $0.10. A refused start costs nothing
more. `--plan` prints the session.start and calls nothing.
"""
from __future__ import annotations

import argparse
import array
import base64
import json
import pathlib
import sys
import threading
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from live_key import key_env_name, key_from, scrub  # noqa: E402 - beside this script
from lib.oracle_live_wire import LIVE_MODELS, LiveWire  # noqa: E402 - the Host's own session builder

URL = "wss://api.openai.com/v1/live/sessions"
RATE = 24000
CHUNK = RATE // 10 * 2                     # 100 ms of PCM16
SILENCE = base64.b64encode(b"\0" * CHUNK).decode()
INSTRUCTIONS = "Du er en kort norsk assistent i en test. Svar med én kort setning på norsk bokmål."


def start_event(model, idle, variant):
    session = LiveWire("api", model=model).session(INSTRUCTIONS, webrtc=False)
    if variant in ("both", "wake"):
        session["wake_policy"] = {"type": "speech"}
    if variant in ("both", "idle"):
        session["idle_timeout_s"] = idle
    return {"type": "session.start", "session": session}


def pcm16_mono_24k(path):
    """A 16-bit WAV to mono 24 kHz PCM16 bytes (stdlib, linear interpolation)."""
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise SystemExit("wake audio must be 16-bit PCM WAV")
        rate, channels = w.getframerate(), w.getnchannels()
        samples = array.array("h")
        samples.frombytes(w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    mono = [sum(samples[i:i + channels]) / channels for i in range(0, len(samples), channels)]
    if rate != RATE and len(mono) > 1:
        step, out, position = rate / RATE, [], 0.0
        while position < len(mono) - 1:
            i = int(position)
            out.append(mono[i] + (mono[i + 1] - mono[i]) * (position - i))
            position += step
        mono = out
    result = array.array("h", (max(-32768, min(32767, int(v))) for v in mono))
    if sys.byteorder == "big":
        result.byteswap()
    return result.tobytes()


def probe(args, key, out):
    import websocket
    socket = websocket.create_connection(URL, header={"Authorization": "Bearer " + key},
                                         suppress_origin=True, timeout=20)
    socket.settimeout(.25)
    t0, lock, events, audio_deltas = time.monotonic(), threading.Lock(), [], [0]
    stop, send_lock = threading.Event(), threading.Lock()
    log = open(out / "events.jsonl", "w", encoding="utf-8")

    def now():
        return round(time.monotonic() - t0, 2)

    def keep(source, event):
        entry = json.loads(scrub(json.dumps({"t": now(), "source": source, **event}, ensure_ascii=False), key))
        with lock:
            events.append(entry)
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.flush()
        if event.get("type") not in ("session.input_audio.append",):
            print(f"{entry['t']:8.2f}s {source:8} {event.get('type')} {event.get('delta') or ''}"[:200], flush=True)

    def seen(kind, since=0.0):
        with lock:
            return [e for e in events if e.get("type") == kind and e["t"] >= since and e["source"] == "provider"]

    def send(event, record=True):
        with send_lock:
            socket.send(json.dumps(event))
        if record:
            keep("client", event)

    def pump():
        while not stop.is_set():
            try:
                raw = socket.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:  # noqa: BLE001 - the finding is that the socket ended
                keep("probe", {"type": "socket.lost", "note": scrub(exc, key)})
                return
            if not raw:
                keep("probe", {"type": "socket.closed_by_provider"})
                return
            event = json.loads(raw)
            if event.get("type") == "session.output_audio.delta":
                audio_deltas[0] += 1
                continue
            keep("provider", event)

    speech = bytearray()
    speech_lock = threading.Lock()

    def microphone():                      # the Live guide: keep input audio running
        while not stop.wait(.1):
            with speech_lock:
                chunk, speech[:CHUNK] = bytes(speech[:CHUNK]), b""
            payload = base64.b64encode(chunk + b"\0" * (CHUNK - len(chunk))).decode() if chunk else SILENCE
            try:
                send({"type": "session.input_audio.append", "audio": payload}, record=False)
            except Exception:  # noqa: BLE001 - the pump records the lost socket
                return

    def wait(seconds, until=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if until and until():
                return True
            time.sleep(.1)
        return bool(until and until())

    threading.Thread(target=pump, daemon=True).start()
    start = start_event(args.model, args.idle, args.variant)
    send(start)
    answered = wait(20, lambda: seen("session.started") or seen("error") or any(
        e["type"] in ("socket.lost", "socket.closed_by_provider") for e in events))
    first = next((e for e in events if e["source"] != "client"), None)
    findings = {"sent": {k: start["session"].get(k) for k in ("model", "wake_policy", "idle_timeout_s")},
                "first_answer_verbatim": first if answered else "no answer within 20 s"}
    started = seen("session.started")
    if not started:
        findings["verdict"] = "refused" if seen("error") else "no session.started"
    else:
        echo = started[0].get("session") if isinstance(started[0].get("session"), dict) else {}
        findings["echo"] = {k: echo.get(k, "<absent from session.started>") for k in ("wake_policy", "idle_timeout_s")}
        findings["errors_after_start"] = seen("error")
        threading.Thread(target=microphone, daemon=True).start()
        t = now()
        wait(args.first_wait)
        findings["before_speech"] = {"session.resumed": bool(seen("session.resumed", t)),
                                     "session.paused": bool(seen("session.paused", t)),
                                     "model_spoke": bool(seen("session.output_transcript.delta", t))}
        since = now()                      # a pause counts from here: it may come before the silence below
        if args.wake_audio:
            t = now()
            with speech_lock:
                speech.extend(pcm16_mono_24k(args.wake_audio))
            keep("probe", {"type": "probe.speak", "note": str(args.wake_audio)})
            findings["wake"] = {"session.resumed": wait(20, lambda: bool(seen("session.resumed", t)))}
            wait(10)
            findings["wake"]["answer"] = "".join(e.get("delta", "") for e in seen("session.output_transcript.delta", t))
        t = now()
        paused = wait(args.idle + 20, lambda: bool(seen("session.paused", since)))
        talk = [e["t"] for e in events if e["source"] == "provider" and e.get("type") in
                ("session.input_transcript.delta", "session.output_transcript.delta")]
        first_pause = seen("session.paused", since)[:1]
        findings["after_silence"] = {"waited_s": round(now() - t, 1), "session.paused": paused,
                                     "paused_at_s": first_pause[0]["t"] if first_pause else None,
                                     "last_transcript_at_s": max(talk) if talk else None}
        errors = seen("error")
        findings["verdict"] = ("accepted, pauses" if paused else
                               "accepted, never paused in the wait" if not errors else "accepted, then errors")
    t = now()
    try:
        send({"type": "session.close"})
        wait(15, lambda: bool(seen("session.closed", t)))
    except Exception as exc:  # noqa: BLE001 - a refused start may already have closed the socket
        keep("probe", {"type": "close.failed", "note": scrub(exc, key)})
    closed = seen("session.closed")
    findings["usage"] = closed[-1].get("usage") if closed else None
    findings["output_audio_deltas"] = audio_deltas[0]
    stop.set()
    socket.close()
    log.close()
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="gpt-live-1.1", choices=LIVE_MODELS)
    parser.add_argument("--idle", type=int, default=20, help="idle_timeout_s to send (default 20)")
    parser.add_argument("--variant", default="both", choices=("both", "wake", "idle"),
                        help="send both standby fields (default), only wake_policy, or only idle_timeout_s")
    parser.add_argument("--first-wait", type=float, default=8, help="seconds of silence before any speech")
    parser.add_argument("--wake-audio", type=pathlib.Path, help="a 16-bit WAV of one spoken line")
    parser.add_argument("--key-env", default="OPENAI_API_KEY", type=key_env_name,
                        help="the environment variable holding the key (its name must start with OPENAI_)")
    parser.add_argument("--out", default=f"/var/tmp/live-socket-standby-{time.strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--plan", action="store_true", help="print the session.start; call nothing")
    args = parser.parse_args()
    if args.plan:
        print(json.dumps(start_event(args.model, args.idle, args.variant), ensure_ascii=False, indent=1))
        return
    key = key_from(args.key_env)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    findings = probe(args, key, out)
    (out / "summary.json").write_text(scrub(json.dumps(findings, ensure_ascii=False, indent=1), key), encoding="utf-8")
    print(json.dumps(findings, ensure_ascii=False, indent=1))
    print(f"\nWritten: {out}/events.jsonl, summary.json")


if __name__ == "__main__":
    main()
