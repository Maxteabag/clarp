#!/usr/bin/env python3
"""Empirical benchmark across STT providers using retained real-world audio clips."""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import time
import urllib.request
import tomllib

# Add server directory to path
SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from lib.config import load
from lib import eleven_stt, cartesia_stt, cartesia_stt_ws
from lib.cartesia_stt_ws import pcm_16k_mono


def create_eleven_live_token(api_key: str) -> str:
    url = "https://api.elevenlabs.io/v1/single-use-token/realtime_scribe"
    req = urllib.request.Request(url, data=b"", method="POST", headers={"xi-api-key": api_key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))["token"]


def transcribe_eleven_live(pcm: bytes, token: str, timeout: float = 10.0) -> tuple[str, float, float]:
    import websocket
    ws_url = (
        f"wss://api.elevenlabs.io/v1/speech-to-text/realtime?token={token}"
        f"&model_id=scribe_v2_realtime&audio_format=pcm_16000&commit_strategy=manual"
    )
    t_start = time.monotonic()
    ws = websocket.create_connection(ws_url, timeout=timeout)
    ws.recv()  # session_started

    chunk_size = 1600  # 50ms chunks
    # Stream simulated realtime (50ms chunks every 10ms to simulate fast network client)
    for i in range(0, len(pcm), chunk_size):
        chunk = pcm[i : i + chunk_size]
        ws.send(json.dumps({
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(chunk).decode("ascii"),
            "commit": False,
        }))
        time.sleep(0.01)

    t_finish_stream = time.monotonic()
    # Send manual commit at the end of speech
    ws.send(json.dumps({
        "message_type": "input_audio_chunk",
        "audio_base_64": "",
        "commit": True,
    }))

    pieces = []
    ws.settimeout(5.0)
    try:
        while True:
            raw = ws.recv()
            data = json.loads(raw)
            mtype = data.get("message_type")
            if mtype == "committed_transcript":
                t = data.get("text", "")
                if t:
                    pieces.append(t)
                break
            elif "error" in str(mtype):
                break
    except Exception:
        pass
    finally:
        try:
            ws.close()
        except Exception:
            pass

    t_done = time.monotonic()
    final_text = " ".join(pieces).strip()
    post_speech_latency = t_done - t_finish_stream
    total_latency = t_done - t_start
    return final_text, total_latency, post_speech_latency


def run_benchmark():
    cfg = load()
    cartesia_key = cfg.cartesia_key()
    eleven_key = cfg.eleven_key()

    heard_dir = pathlib.Path(os.path.expanduser("~/.cache/clarp/heard"))

    test_cases = [
        {
            "trace_id": "bbd7beb288d6a2ea",
            "label": "Short prompt (3.8s)",
            "reference": "Who is sending you these messages?",
        },
        {
            "trace_id": "c64d27bd82f0f65b",
            "label": "Medium question (12s)",
            "reference": "And are you able to test this yourself by running it and testing how the performance is on my computer?",
        },
        {
            "trace_id": "1c93b061510bde95",
            "label": "Domain terms (8.3s)",
            "reference": "I mean local because the GitHub Actions is running on the same computer. I mean our GitHub Actions is running on that Mac.",
        },
        {
            "trace_id": "ac201d54719404b0",
            "label": "Long conversational command (21.6s)",
            "reference": "Okay, that's weird. So maybe we can just remove that. You have the freedom to just create a pull request and merge that straight into main. I don't want that at all.",
        },
    ]

    print(f"Loaded {len(test_cases)} audio test cases from database.")
    print("=" * 80)

    results = []

    for item in test_cases:
        trace_id = item["trace_id"]
        wav_path = heard_dir / f"{trace_id}.wav"
        if not wav_path.is_file():
            continue
        audio_bytes = wav_path.read_bytes()
        pcm = pcm_16k_mono(audio_bytes)
        duration_sec = len(pcm) / 32000.0

        print(f"\n--- Clip: '{item['label']}' (Audio duration: {duration_sec:.2f}s) ---")
        print(f"Reference: \"{item['reference']}\"")

        case_res = {
            "label": item["label"],
            "duration_sec": duration_sec,
            "reference": item["reference"],
            "providers": {},
        }

        # 1. Cartesia Batch (ink-whisper)
        try:
            t0 = time.monotonic()
            txt, _ = cartesia_stt.transcribe(
                audio_bytes=audio_bytes, content_type="audio/wav",
                api_key=cartesia_key, model="ink-whisper"
            )
            lat = time.monotonic() - t0
            case_res["providers"]["Cartesia Ink-Whisper (batch)"] = {
                "text": txt,
                "latency_sec": lat,
            }
            print(f" [Cartesia Batch] ({lat:.3f}s): \"{txt}\"")
        except Exception as e:
            print(f" [Cartesia Batch] Error: {e}")

        # 2. Cartesia Realtime WS (ink-2)
        try:
            t0 = time.monotonic()
            txt, lat = cartesia_stt_ws.transcribe(
                audio_bytes=audio_bytes, content_type="audio/wav",
                api_key=cartesia_key, model="ink-2", keyterms=["GitHub Actions", "Clarp", "Mac"]
            )
            case_res["providers"]["Cartesia Ink-2 (WS)"] = {
                "text": txt,
                "latency_sec": lat,
            }
            print(f" [Cartesia Ink-2 WS] ({lat:.3f}s): \"{txt}\"")
        except Exception as e:
            print(f" [Cartesia Ink-2 WS] Error: {e}")

        # 3. ElevenLabs Scribe v2 (batch)
        try:
            t0 = time.monotonic()
            txt, _ = eleven_stt.transcribe(
                audio_bytes=audio_bytes, content_type="audio/wav",
                api_key=eleven_key, model="scribe_v2", keyterms=["GitHub Actions", "Clarp", "Mac"]
            )
            lat = time.monotonic() - t0
            case_res["providers"]["ElevenLabs Scribe v2 (batch)"] = {
                "text": txt,
                "latency_sec": lat,
            }
            print(f" [ElevenLabs Scribe v2 Batch] ({lat:.3f}s): \"{txt}\"")
        except Exception as e:
            print(f" [ElevenLabs Scribe v2 Batch] Error: {e}")

        # 4. ElevenLabs Scribe Live (WS)
        try:
            token = create_eleven_live_token(eleven_key)
            txt, total_lat, post_lat = transcribe_eleven_live(pcm, token)
            case_res["providers"]["ElevenLabs Scribe Live (WS)"] = {
                "text": txt,
                "total_stream_sec": total_lat,
                "perceived_post_speech_latency_sec": post_lat,
            }
            print(f" [ElevenLabs Scribe Live WS] (Perceived turn delay: {post_lat:.3f}s, Total: {total_lat:.3f}s): \"{txt}\"")
        except Exception as e:
            print(f" [ElevenLabs Scribe Live WS] Error: {e}")

        results.append(case_res)

    out_file = pathlib.Path(__file__).resolve().parent / "stt_benchmark_results.json"
    out_file.write_text(json.dumps(results, indent=2))
    print(f"\nAll benchmark results written to {out_file}")

if __name__ == "__main__":
    run_benchmark()
