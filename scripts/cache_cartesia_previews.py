#!/usr/bin/env python3
"""Snapshot the current Cartesia catalog and generated previews for iOS.

The script is resumable: valid existing MP3 files are kept. Authentication is
read from the native Clarp config by default and is never written to the repo.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import tomllib
import urllib.parse
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:7682")
    parser.add_argument("--config", type=pathlib.Path,
                        default=pathlib.Path.home() / ".config/clarp/config.toml")
    parser.add_argument("--output", type=pathlib.Path,
                        default=pathlib.Path("ios-native/ClarpNative/Resources/VoicePreviews"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    config = tomllib.loads(args.config.read_text())
    token = str(config.get("server", {}).get("auth_token", ""))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    base = args.server.rstrip("/")
    request = urllib.request.Request(base + "/cartesia-voices?force=1", headers=headers)
    with urllib.request.urlopen(request, timeout=180) as response:
        catalog = json.load(response)
    voices = catalog.get("voices") or []
    if not catalog.get("available") or not voices:
        raise SystemExit("Cartesia voice catalog is unavailable")

    args.output.mkdir(parents=True, exist_ok=True)
    bundled_voices = [{**voice, "taken_by": None} for voice in voices]
    (args.output / "catalog.json").write_text(
        json.dumps({"available": True, "voices": bundled_voices}, ensure_ascii=False,
                   separators=(",", ":")) + "\n")

    def download(voice: dict) -> str:
        voice_id = str(voice["id"])
        destination = args.output / (hashlib.sha256(voice_id.encode()).hexdigest() + ".mp3")
        if destination.is_file() and destination.stat().st_size > 1024:
            return "cached"
        query = urllib.parse.urlencode({"id": voice_id})
        req = urllib.request.Request(base + "/cartesia-voice-preview?" + query, headers=headers)
        with urllib.request.urlopen(req, timeout=180) as response:
            payload = response.read()
        if len(payload) <= 1024:
            raise RuntimeError(f"preview too small for {voice_id}")
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(destination)
        return "generated"

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(download, voice): voice for voice in voices}
        for future in concurrent.futures.as_completed(futures):
            future.result()
            completed += 1
            if completed % 20 == 0 or completed == len(voices):
                print(f"previews {completed}/{len(voices)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
