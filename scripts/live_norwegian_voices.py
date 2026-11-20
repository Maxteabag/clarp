#!/usr/bin/env python3
"""The same Norwegian sentences spoken by gpt-live-1 and by gpt-live-1.1, for the
owner to hear (issue 473): two WAV files, one per model.

Each model gets one session on the Host-held WebSocket path, built by the Host's
own LiveWire (voice marin, PCM 24 kHz) exactly as an agent Live call is. Silence
goes up all the time (the Live guide: keep input audio running). Each sentence is
asked for the way an agent Live call asks for its opening:
`session.instructions.append` "say exactly this, once, now". The model's audio is
kept until it has been quiet for --quiet seconds; then the next sentence. The
session is closed gracefully and its usage recorded.

Written under --out: norsk-gpt-live-1.wav, norsk-gpt-live-1.1.wav (24 kHz mono)
and transcripts.json (what each model's transcript says it said, its usage).
The key comes only from the environment variable named by --key-env (default
OPENAI_API_KEY); it goes only into the Authorization header to api.openai.com
and is never printed or written. No Clarp Host is needed:

    OPENAI_API_KEY=... uv run --frozen python scripts/live_norwegian_voices.py

Cost at the documented rate ($0.05 a minute active, per second): about two minutes
a model, about $0.20 in all. `--plan` prints both session bodies and calls nothing.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
import threading
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from lib.oracle_live_wire import LIVE_MODELS, LiveWire  # noqa: E402 - the Host's own session builder

URL = "wss://api.openai.com/v1/live/sessions"
RATE = 24000
SILENCE = base64.b64encode(b"\0" * (RATE // 10 * 2)).decode()  # 100 ms
INSTRUCTIONS = ("Du er en norsk opplesingsstemme i en lydtest. Snakk norsk bokmål med naturlig østnorsk uttale. "
                "Når du blir bedt om å si noe, si nøyaktig de ordene, én gang, uten å legge til noe.")
SENTENCES = (
    "Hei, dette er Sandra i kontrollrommet. Kamera sju har sett vann ved vanntanken.",
    "Jeg har sjekket loggen: lekkasjen startet klokka atten null null og er fortsatt aktiv.",
    "Skal jeg be Theo om å stenge ventilen, eller vil du se på det selv først?",
    "Bergen, Trondheim, Tromsø og Kristiansand får regn i morgen, men Oslo blir tørt.",
    "Rapporten er ferdig. Den har tre funn, og det viktigste gjelder fakturaene fra september.",
)


def key_from(env):
    value = os.environ.get(env, "")
    if not value.strip():
        sys.exit(f"Set the OpenAI key in the environment variable {env} (it is read from there only).")
    return value.strip()


def scrub(text, key):
    return str(text).replace(key, "[key]") if key else str(text)


def session_body(model):
    return LiveWire("api", model=model).session(INSTRUCTIONS, webrtc=False)


def record(model, key, out, quiet):
    import websocket
    socket = websocket.create_connection(URL, header={"Authorization": "Bearer " + key},
                                         suppress_origin=True, timeout=20)
    socket.settimeout(.25)
    stop, send_lock = threading.Event(), threading.Lock()

    def send(event):
        with send_lock:
            socket.send(json.dumps(event))

    audio, said, usage, events = bytearray(), [], None, []
    state = {"last_audio": None, "started": False, "closed": False}

    def pump():
        nonlocal usage
        while not stop.is_set():
            try:
                raw = socket.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:  # noqa: BLE001 - recorded, then the session is over
                events.append({"type": "socket.lost", "note": scrub(exc, key)})
                state["closed"] = True
                return
            if not raw:
                state["closed"] = True
                return
            event = json.loads(raw)
            kind = event.get("type")
            if kind == "session.output_audio.delta":
                audio.extend(base64.b64decode(event.get("delta") or ""))
                state["last_audio"] = time.monotonic()
                continue
            events.append({"t": round(time.monotonic(), 2), **event})
            if kind == "session.started":
                state["started"] = True
            elif kind == "session.output_transcript.delta":
                said.append(event.get("delta") or "")
            elif kind in ("session.usage.updated", "session.closed"):
                usage = event.get("usage", usage)
                if kind == "session.closed":
                    state["closed"] = True
                    return
            elif kind == "error":
                print(f"  {model}: provider error {scrub(json.dumps(event, ensure_ascii=False), key)[:300]}")

    def microphone():
        while not stop.wait(.1):
            if state["started"] and not state["closed"]:
                try:
                    send({"type": "session.input_audio.append", "audio": SILENCE})
                except Exception:  # noqa: BLE001 - the pump reports the lost socket
                    return

    threads = [threading.Thread(target=pump, daemon=True), threading.Thread(target=microphone, daemon=True)]
    for thread in threads:
        thread.start()
    send({"type": "session.start", "session": session_body(model)})
    deadline = time.monotonic() + 20
    while not state["started"] and not state["closed"] and time.monotonic() < deadline:
        time.sleep(.1)
    if not state["started"]:
        stop.set()
        socket.close()
        sys.exit(f"{model}: no session.started: " + scrub(json.dumps(events[-3:], ensure_ascii=False), key))
    for index, sentence in enumerate(SENTENCES, 1):
        before = len(audio)
        send({"type": "session.instructions.append", "content": "Si nøyaktig dette, én gang, nå: " + sentence,
              "delegation_id": None, "event_id": f"sentence-{index}"})
        asked = time.monotonic()
        while time.monotonic() - asked < 30:     # wait for the speech, then for --quiet seconds of none
            last = state["last_audio"]
            if len(audio) > before and last and time.monotonic() - last >= quiet:
                break
            time.sleep(.05)
        audio.extend(b"\0" * (RATE * 2 // 2))    # half a second between sentences in the file
        print(f"  {model}: sentence {index} {(len(audio) - before) / (RATE * 2):.1f} s")
    send({"type": "session.close"})
    deadline = time.monotonic() + 15
    while not state["closed"] and time.monotonic() < deadline:
        time.sleep(.1)
    stop.set()
    socket.close()
    path = out / f"norsk-{model}.wav"
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(RATE)
        file.writeframes(bytes(audio))
    return {"file": path.name, "seconds": round(len(audio) / (RATE * 2), 1), "transcript": "".join(said),
            "usage": usage, "closed": state["closed"],
            "errors": [e for e in events if e.get("type") in ("error", "socket.lost")]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", nargs="+", default=list(LIVE_MODELS), choices=LIVE_MODELS)
    parser.add_argument("--quiet", type=float, default=2.0, help="seconds of no model audio that end a sentence")
    parser.add_argument("--key-env", default="OPENAI_API_KEY", help="the environment variable holding the key")
    parser.add_argument("--out", default=f"/var/tmp/live-norwegian-{time.strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--plan", action="store_true", help="print both session bodies; call nothing")
    args = parser.parse_args()
    if args.plan:
        for model in args.models:
            print(json.dumps({"session.start": session_body(model)}, ensure_ascii=False, indent=1))
        print("\n".join(SENTENCES))
        return
    key = key_from(args.key_env)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {"sentences": SENTENCES}
    for model in args.models:
        print(f"{model}:")
        results[model] = record(model, key, out, args.quiet)
    (out / "transcripts.json").write_text(scrub(json.dumps(results, ensure_ascii=False, indent=1), key),
                                          encoding="utf-8")
    print(json.dumps({m: {k: v for k, v in r.items() if k != "transcript"} for m, r in results.items()
                      if m != "sentences"}, ensure_ascii=False, indent=1))
    print(f"\nWritten: {out}")


if __name__ == "__main__":
    main()
