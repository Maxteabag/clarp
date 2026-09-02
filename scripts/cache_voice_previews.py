#!/usr/bin/env python3
"""Cache provider voice previews for the native app bundle.

Credentials stay on the configured Clarp server. This script downloads only
the synthesized MP3 responses and writes a provider/id manifest.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import tomllib
import urllib.parse
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:7682")
    parser.add_argument("--config", type=Path,
                        default=Path.home() / ".config/clarp/config.toml")
    parser.add_argument("--output", type=Path, default=Path(
        "ios-native/ClarpNative/Resources/VoicePreviews"))
    parser.add_argument("--provider", action="append", dest="providers")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    requested = set(args.providers or (
        "elevenlabs", "deepgram", "clarp"))
    config = tomllib.loads(args.config.read_text())
    token = str(config.get("server", {}).get("auth_token", ""))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    base = args.server.rstrip("/")
    request = urllib.request.Request(base + "/voice-catalog", headers=headers)
    with urllib.request.urlopen(request, timeout=180) as response:
        catalog = json.load(response)
    manifest_path = args.output / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {"version": 1, "previews": {}}
    previews = manifest.setdefault("previews", {})
    jobs = []
    for group in catalog.get("providers", []):
        provider = str(group.get("id") or "")
        if provider not in requested:
            continue
        if not group.get("available"):
            print(f"skip {provider}: not configured or installed", flush=True)
            continue
        for voice in group.get("voices", []):
            jobs.append((provider, voice))
    args.output.mkdir(parents=True, exist_ok=True)

    def generate(job) -> tuple[str, str, str]:
        provider, voice = job
        voice_id = str(voice["id"])
        relative = f"{provider}/{hashlib.sha256(voice_id.encode()).hexdigest()}.mp3"
        destination = args.output / relative
        if (not args.refresh and destination.is_file()
                and destination.stat().st_size > 1024):
            return provider, voice_id, relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        query = urllib.parse.urlencode({"provider": provider, "id": voice_id})
        req = urllib.request.Request(base + "/voice-preview?" + query,
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=180) as response:
            payload = response.read()
        if len(payload) <= 1024:
            raise RuntimeError(f"preview too small for {provider}:{voice_id}")
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(destination)
        return provider, voice_id, relative

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(generate, job) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            provider, voice_id, relative = future.result()
            previews.setdefault(provider, {})[voice_id] = relative
            completed += 1
            print(f"previews {completed}/{len(jobs)}", flush=True)
    manifest_path.write_text(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
