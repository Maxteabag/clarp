#!/usr/bin/env python3
"""Publish an agent-produced image or allowlisted file to the Clarp media store."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    import tomli as tomllib  # type: ignore


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Upload media/files to Clarp storage and print chat markdown or its URL."
    )
    ap.add_argument("files", nargs="+", type=pathlib.Path, help="file(s) to publish")
    ap.add_argument("--session", default=os.environ.get("CLAUDE_PWA_SESSION", ""),
                    help="agent app session; defaults to CLAUDE_PWA_SESSION")
    ap.add_argument("--caption", action="append", default=[],
                    help="caption / markdown alt text; repeat for per-image captions")
    ap.add_argument("--created-by", default="agent")
    ap.add_argument("--base-url", default=os.environ.get("CLAUDE_PWA_BASE_URL", ""))
    ap.add_argument("--token", default=os.environ.get("CLAUDE_PWA_TOKEN", ""))
    ap.add_argument("--json", action="store_true", help="print full JSON response")
    ap.add_argument("--gallery", action="store_true",
                    help="print a clarp-gallery markdown block for multiple images")
    args = ap.parse_args()

    session = args.session.strip()
    if not session:
        print("clarp-media: --session required when CLAUDE_PWA_SESSION is unset",
              file=sys.stderr)
        return 2
    base_url, token = _config(args.base_url, args.token)
    if args.gallery:
        if len(args.files)>10:
            print("clarp-media: galleries support at most 10 images",file=sys.stderr); return 2
        try: total=sum(path.expanduser().stat().st_size for path in args.files)
        except OSError as error:
            print(f"clarp-media: file unavailable: {error}",file=sys.stderr); return 2
        if total>250*1024*1024:
            print("clarp-media: galleries cannot exceed 250 MB",file=sys.stderr); return 2
    published = []
    multi = len(args.files) > 1
    for index, raw_path in enumerate(args.files):
        path = raw_path.expanduser().resolve()
        if not path.is_file():
            print(f"clarp-media: file not found: {path}", file=sys.stderr)
            return 2
        result = _publish_one(
            path=path,
            session=session,
            caption=_caption_for(args.caption, path, index, multi),
            created_by=args.created_by,
            base_url=base_url,
            token=token,
        )
        if result is None:
            return 1
        published.append(result)

    if args.json:
        if len(published) == 1:
            print(json.dumps(published[0], indent=2, sort_keys=True))
        else:
            print(json.dumps({"assets": published}, indent=2, sort_keys=True))
    elif args.gallery:
        print("```clarp-gallery")
        for out in published:
            print(out.get("markdown") or out.get("asset", {}).get("markdown")
                  or out.get("url") or out.get("asset", {}).get("url") or "")
        print("```")
    else:
        for out in published:
            print(out.get("markdown") or out.get("asset", {}).get("markdown")
                  or out.get("url") or out.get("asset", {}).get("url") or "")
    return 0


def _publish_one(
    *,
    path: pathlib.Path,
    session: str,
    caption: str,
    created_by: str,
    base_url: str,
    token: str,
) -> dict | None:
    data = path.read_bytes()
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Content-Type": ctype,
        "X-Session": _header_value(session),
        "X-File-Name": _header_value(path.name),
        "X-Caption": _header_value(caption),
        "X-Created-By": _header_value(created_by),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base_url.rstrip("/") + "/media",
        data=data,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print(f"clarp-media: server returned {e.code}: {detail}", file=sys.stderr)
        return None
    except OSError as e:
        print(f"clarp-media: upload failed: {e}", file=sys.stderr)
        return None

    return json.loads(body)

def _caption_for(captions: list[str], path: pathlib.Path, index: int, multi: bool) -> str:
    if index < len(captions):
        return captions[index]
    if captions:
        return captions[-1]
    return path.stem.replace("_", " ").replace("-", " ") if multi else ""


def _config(base_url: str, token: str) -> tuple[str, str]:
    if base_url:
        return base_url, token
    path = pathlib.Path(os.environ.get(
        "CLAUDE_PWA_CONFIG",
        pathlib.Path.home() / ".config" / "clarp" / "config.toml",
    ))
    data: dict = {}
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        pass
    server = data.get("server", {}) if isinstance(data, dict) else {}
    bind = str(server.get("bind_addr", "127.0.0.1")).strip() or "127.0.0.1"
    if bind in {"0.0.0.0", "::"}:
        bind = "127.0.0.1"
    if ":" in bind and not bind.startswith("["):
        bind = f"[{bind}]"
    port = int(server.get("port", 7682))
    return f"http://{bind}:{port}", token or str(server.get("auth_token", ""))


def _header_value(value: str) -> str:
    return urllib.parse.quote(value or "", safe="._- ")


if __name__ == "__main__":
    raise SystemExit(main())
