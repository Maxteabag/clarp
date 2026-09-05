#!/usr/bin/env python3
"""Create Clarp artifacts, approvals and native questions; inspect pending attention."""
from __future__ import annotations
import argparse
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


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _decision_request(cmd: str, args: list[str]) -> dict:
    parser = _Parser(prog="clarp-agent-artifacts " + cmd)
    parser.add_argument("session")
    parser.add_argument("title")
    parser.add_argument("question")
    if cmd == "decision":
        parser.add_argument("yes_label")
        parser.add_argument("no_label")
        parser.add_argument("payload_json", nargs="?", default="{}")
    else:
        parser.add_argument("options_json", help="JSON array containing two or three option objects")
        parser.add_argument("--recommend", dest="recommended_option_id")
        parser.add_argument("--payload", dest="payload_json", default="{}")
    parser.add_argument("--context", default="")
    parser.add_argument("--reference", default="")
    parser.add_argument("--blocks-progress", action="store_true")
    parser.add_argument("--priority-reason", default="")
    parser.add_argument("--urgency", choices=("normal", "time_sensitive"), default="normal")
    parser.add_argument("--effort", choices=("quick", "short", "review"), default="review")
    parser.add_argument("--deadline-at", type=int, help="actual deadline, epoch milliseconds")
    parser.add_argument("--expires-at", type=int, help="expiry, epoch milliseconds")
    parser.add_argument("--dry-run", action="store_true", help="validate and print request without network calls")
    parsed = parser.parse_args(args)
    payload = json.loads(parsed.payload_json)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if not parsed.question.strip():
        raise ValueError("question must not be blank")
    if (parsed.blocks_progress or parsed.urgency != "normal") and not parsed.priority_reason.strip():
        raise ValueError("blocking or time-sensitive requests require --priority-reason")
    body = {"session": parsed.session, "title": parsed.title, "question": parsed.question,
            "payload": payload, "context": parsed.context, "reference_id": parsed.reference,
            "blocks_progress": parsed.blocks_progress, "priority_reason": parsed.priority_reason,
            "urgency": parsed.urgency, "response_effort": parsed.effort}
    for key in ("deadline_at", "expires_at"):
        value = getattr(parsed, key)
        if value is not None:
            if value <= 0:
                raise ValueError(key + " must be positive epoch milliseconds")
            body[key] = value
    if cmd == "decision":
        body.update(yes_label=parsed.yes_label, no_label=parsed.no_label)
    else:
        options = json.loads(parsed.options_json)
        if not isinstance(options, list) or not 2 <= len(options) <= 3:
            raise ValueError("questions require two or three options")
        ids = set()
        normalized = []
        for option in options:
            if not isinstance(option, dict) or set(option) - {"id", "label", "description"}:
                raise ValueError("each option must contain id, label and optional description")
            entry = {}
            for key in ("id", "label", "description"):
                value = option.get(key, "")
                if not isinstance(value, str) or (key != "description" and not value.strip()):
                    raise ValueError("each option needs nonempty string id and label")
                entry[key] = value.strip()
            if entry["id"] in ids:
                raise ValueError("option IDs must be unique")
            ids.add(entry["id"])
            normalized.append(entry)
        if parsed.recommended_option_id is not None and parsed.recommended_option_id not in ids:
            raise ValueError("--recommend must identify an existing option")
        body.update(response_type="single_choice", options=normalized, allow_custom_text=True,
                    recommended_option_id=parsed.recommended_option_id)
    if parsed.dry_run:
        return {"method": "POST", "path": "/decisions", "body": body}
    if cmd == "question":
        capability = _request("GET", "/attention?decision_format=2")
        if capability.get("decision_format") != 2:
            raise ValueError("this Host does not support native questions; ask in ordinary text instead")
    return _request("POST", "/decisions", body)["artifact"]


def _attention(args: list[str]) -> dict:
    parser = _Parser(prog="clarp-agent-artifacts attention")
    parser.add_argument("--session", help="only show this originating session")
    parser.add_argument("--include-archived", action="store_true")
    parsed = parser.parse_args(args)
    query = {"decision_format": "2"}
    if parsed.include_archived:
        query["include_archived"] = "1"
    result = _request("GET", "/attention?" + urllib.parse.urlencode(query))
    if parsed.session:
        result["items"] = [item for item in result.get("items", [])
                           if item.get("session") == parsed.session]
        result["count"] = len(result["items"])
    return result


def main(argv: list[str]) -> int:
    usage = ("usage: agent_artifacts.py create SESSION TYPE TITLE [SUMMARY] [JSON_PAYLOAD] | "
             "decision SESSION TITLE QUESTION YES_LABEL NO_LABEL [JSON_PAYLOAD] [OPTIONS] | "
             "question SESSION TITLE QUESTION JSON_OPTIONS [OPTIONS] | "
             "attention [--session SESSION] [--include-archived] | "
             "update ARTIFACT_ID STATUS [JSON_PAYLOAD] | progress ARTIFACT_ID VALUE [CONTENT] | list SESSION")
    try:
        cmd = argv[1]
        if cmd == "create" and len(argv) in {5, 6, 7}:
            result = _request("POST", "/artifacts", {
                "session": argv[2], "type": argv[3], "title": argv[4],
                "summary": argv[5] if len(argv) >= 6 else "",
                "payload": json.loads(argv[6]) if len(argv) == 7 else {}})["artifact"]
        elif cmd in {"decision", "question"}:
            result = _decision_request(cmd, argv[2:])
        elif cmd == "attention":
            result = _attention(argv[2:])
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
