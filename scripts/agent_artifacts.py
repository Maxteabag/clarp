#!/usr/bin/env python3
"""Create/update Clarp artifacts and approval decisions from agent workflows."""
from __future__ import annotations
import json, os, pathlib, sys, urllib.error, urllib.parse, urllib.request
import tomllib

share = pathlib.Path(os.environ.get(
    "CLARP_SHARE_DIR", pathlib.Path.home() / ".local/share/clarp"))
sys.path.insert(0, os.environ.get("CLARP_CODE_ROOT", str(share / "current")))

def _config() -> tuple[str, str]:
    path = pathlib.Path(os.environ.get(
        "CLAUDE_PWA_CONFIG", pathlib.Path(os.environ.get(
            "CLARP_CONFIG_DIR", pathlib.Path.home() / ".config/clarp")) /
        "config.toml"))
    try: data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError): data = {}
    server = data.get("server", {}) if isinstance(data, dict) else {}
    bind = str(server.get("bind_addr", "127.0.0.1")) or "127.0.0.1"
    if bind in {"0.0.0.0", "::"}: bind = "127.0.0.1"
    if ":" in bind and not bind.startswith("["): bind = f"[{bind}]"
    return f"http://{bind}:{int(server.get('port', 7682))}", str(server.get("auth_token", ""))


def _request(method: str, path: str, body: dict | None = None) -> dict:
    base, token = _config()
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token: request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def main(argv: list[str]) -> int:
    usage = ("usage: agent_artifacts.py create SESSION TYPE TITLE [SUMMARY] [JSON_PAYLOAD] | "
             "decision SESSION TITLE QUESTION YES_LABEL NO_LABEL [JSON_PAYLOAD] | "
             "update ARTIFACT_ID STATUS [JSON_PAYLOAD] | progress ARTIFACT_ID VALUE [CONTENT] | list SESSION")
    try:
        cmd = argv[1]
        if cmd == "create" and len(argv) in {5, 6, 7}:
            result = _request("POST", "/artifacts", {
                "session": argv[2], "type": argv[3], "title": argv[4],
                "summary": argv[5] if len(argv) >= 6 else "",
                "payload": json.loads(argv[6]) if len(argv) == 7 else {}})["artifact"]
        elif cmd == "decision" and len(argv) in {7, 8}:
            result = _request("POST", "/decisions", {
                "session": argv[2], "title": argv[3], "question": argv[4],
                "yes_label": argv[5], "no_label": argv[6],
                "payload": json.loads(argv[7]) if len(argv) == 8 else {}})["artifact"]
        elif cmd == "update" and len(argv) in {4, 5}:
            body = {"status": argv[3]}
            if len(argv) == 5: body["payload"] = json.loads(argv[4])
            result = _request("POST", "/artifacts/" + urllib.parse.quote(argv[2]),
                              body)["artifact"]
        elif cmd == "progress" and len(argv) in {4, 5}:
            progress = float(argv[3])
            payload = {"progress": progress}
            if len(argv) == 5: payload["content"] = argv[4]
            result = _request("POST", "/artifacts/" + urllib.parse.quote(argv[2]),
                              {"status": "active", "payload_patch": payload})["artifact"]
        elif cmd == "list" and len(argv) == 3:
            result = _request("GET", "/artifacts?" + urllib.parse.urlencode({"session": argv[2]}))
        else:
            print(usage, file=sys.stderr); return 2
        print(json.dumps(result, ensure_ascii=False)); return 0
    except (IndexError, ValueError, json.JSONDecodeError, OSError, urllib.error.HTTPError) as exc:
        print(f"agent_artifacts: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
