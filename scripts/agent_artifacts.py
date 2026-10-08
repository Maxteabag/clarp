#!/usr/bin/env python3
"""Create Clarp artifacts, approvals and native questions; inspect pending attention."""
from __future__ import annotations
import argparse
import hashlib, json, os, pathlib, re, sys, urllib.error, urllib.parse, urllib.request
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
    elif cmd == "input":
        parser.add_argument("--hint", choices=("text", "one_time_code"), default="text",
                            help="one_time_code shows a code keypad with SMS autofill")
        parser.add_argument("--payload", dest="payload_json", default="{}")
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
    parser.add_argument("--expires-in", type=int, metavar="SECONDS",
                        help="expiry relative to now; the card is marked expired, never deleted")
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
    if parsed.expires_in is not None:
        if parsed.expires_at is not None:
            raise ValueError("use --expires-in or --expires-at, not both")
        if parsed.expires_in <= 0:
            raise ValueError("--expires-in must be positive seconds")
        import time
        parsed.expires_at = int(time.time() * 1000) + parsed.expires_in * 1000
    for key in ("deadline_at", "expires_at"):
        value = getattr(parsed, key)
        if value is not None:
            if value <= 0:
                raise ValueError(key + " must be positive epoch milliseconds")
            body[key] = value
    if cmd == "decision":
        body.update(yes_label=parsed.yes_label, no_label=parsed.no_label)
    elif cmd == "input":
        body.update(response_type="text_input", input_hint=parsed.hint)
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
    if cmd in ("question", "input"):
        capability = _request("GET", "/attention?decision_format=2")
        if capability.get("decision_format") != 2:
            raise ValueError("this Host does not support native questions; ask in ordinary text instead")
    return _request("POST", "/decisions", body)["artifact"]


def _wait(args: list[str]) -> tuple[dict, int]:
    """Block until the user answers (or the request ends) and print the outcome.

    Exit 0 answered/accepted/rejected, 3 expired/withdrawn/discarded, 4 timeout."""
    import time
    parser = _Parser(prog="clarp-agent-artifacts wait")
    parser.add_argument("artifact_id")
    parser.add_argument("--timeout", type=int, default=600, metavar="SECONDS")
    parser.add_argument("--interval", type=float, default=2.0)
    parsed = parser.parse_args(args)
    deadline = time.monotonic() + max(1, parsed.timeout)
    while True:
        artifact = _request("GET", "/artifacts/" + urllib.parse.quote(parsed.artifact_id))["artifact"]
        decision = artifact.get("decision") or {}
        status = decision.get("status", "")
        if status and status != "pending":
            outcome = {"status": status, "answer": decision.get("answer"),
                       "resolved_choice": decision.get("resolved_choice"),
                       "decision_id": decision.get("decision_id"), "artifact_id": parsed.artifact_id}
            return outcome, 0 if status in ("answered", "accepted", "rejected") else 3
        if time.monotonic() >= deadline:
            return {"status": "pending", "timeout": True, "artifact_id": parsed.artifact_id}, 4
        time.sleep(max(0.5, parsed.interval))


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


def _create_form(args: list[str]) -> dict:
    parser = _Parser(prog="clarp-agent-artifacts create-form")
    parser.add_argument("session")
    parser.add_argument("title")
    parser.add_argument("html_file", type=pathlib.Path)
    parser.add_argument("schema_file", type=pathlib.Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--artifact-id", required=True,
                        help="stable client-chosen identity; reconcile this ID after ambiguous publication")
    parser.add_argument("--connect", action="append", default=[], metavar="HTTPS_ORIGIN")
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)
    from lib.html_forms import connect_origins
    network = connect_origins(parsed.connect)
    payload = {"content": parsed.html_file.read_text(), "version": parsed.version,
               "answer_schema": json.loads(parsed.schema_file.read_text())}
    if parsed.connect: payload["connect_origins"] = network
    body = {"session": parsed.session, "title": parsed.title, "type": "html_form",
            "summary": parsed.summary, "artifact_id": parsed.artifact_id, "payload": payload}
    if parsed.dry_run:
        return {"method": "POST", "path": "/artifacts", "body": body}
    return _request("POST", "/artifacts", body)["artifact"]


def _report_id(title: str, content: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-") or "report"
    digest = hashlib.sha256(f"{title}\0{content}".encode()).hexdigest()[:12]
    return f"report-{slug}-{digest}"


def _create_report(args: list[str]) -> dict:
    parser = _Parser(prog="clarp-agent-artifacts create-report")
    parser.add_argument("session")
    parser.add_argument("title")
    parser.add_argument("html_file", type=pathlib.Path)
    parser.add_argument("--summary", default="")
    parser.add_argument("--artifact-id", default="",
                        help="stable identity; defaults to one derived from the title and HTML")
    parser.add_argument("--version", default="1")
    parser.add_argument("--connect", action="append", default=[], metavar="HTTPS_ORIGIN")
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)
    from lib.html_forms import connect_origins
    network = connect_origins(parsed.connect)
    content = parsed.html_file.read_text()
    if not content.strip():
        raise ValueError("report HTML is empty")
    body = {"session": parsed.session, "title": parsed.title, "type": "html_form",
            "summary": parsed.summary,
            "artifact_id": parsed.artifact_id or _report_id(parsed.title, content),
            "payload": {"content": content, "version": parsed.version, "read_only": True}}
    if parsed.connect: body["payload"]["connect_origins"] = network
    if parsed.dry_run:
        return {"method": "POST", "path": "/artifacts", "body": body}
    try:
        return _request("POST", "/artifacts", body)["artifact"]
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        current = _request("GET", "/artifacts/" + urllib.parse.quote(body["artifact_id"], safe=""))["artifact"]
        if current.get("session") != parsed.session or current.get("read_only") is not True:
            raise ValueError("the artifact ID does not identify this agent's read-only report") from exc
        if current.get("version") == parsed.version:
            raise
        body["expected_version"] = current["version"]
        # One compare-and-set attempt: a concurrent edit is not silently overwritten.
        return _request("POST", "/artifacts", body)["artifact"]


def _report_history(args: list[str]) -> dict:
    parser = _Parser(prog="clarp-agent-artifacts report-history")
    parser.add_argument("artifact_id")
    parser.add_argument("--version")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parsed = parser.parse_args(args)
    query = {"limit": max(1, min(parsed.limit, 100)), "offset": max(0, parsed.offset)}
    if parsed.version is not None:
        query["version"] = parsed.version
    return _request("GET", "/artifacts/" + urllib.parse.quote(parsed.artifact_id, safe="")
                    + "/revisions?" + urllib.parse.urlencode(query))


def _form_events(args: list[str]) -> dict:
    parser = _Parser(prog="clarp-agent-artifacts form-events")
    parser.add_argument("artifact_id")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--draft-key", help="explicitly follow an existing custom draft event array; empty disables")
    parser.add_argument("--output", help="export a consistent full snapshot as private JSONL")
    parser.add_argument("--overwrite", action="store_true")
    parsed = parser.parse_args(args)
    artifact_path = "/artifacts/" + urllib.parse.quote(parsed.artifact_id, safe="")
    if parsed.draft_key is not None:
        return _request("POST", artifact_path + "/events-config", {"draft_key": parsed.draft_key or None})
    path = artifact_path + "/events?"
    query = {"after": parsed.after, "limit": parsed.limit}
    first = _request("GET", path + urllib.parse.urlencode(query))
    if parsed.output is None: return first
    import tempfile
    target = pathlib.Path(parsed.output).absolute()
    if target.exists() and not parsed.overwrite: raise ValueError("output already exists; use --overwrite to refresh it")
    descriptor, name = tempfile.mkstemp(prefix=".form-events-", dir=target.parent)
    total = 0
    try:
        with os.fdopen(descriptor, "w") as file:
            page = first
            while True:
                for event in page['events']:
                    file.write(json.dumps(event, ensure_ascii=False) + "\n"); total += 1
                if not page['has_more']: break
                if page['next_seq'] <= query['after']: raise ValueError("event cursor did not advance")
                query.update(after=page['next_seq'], through=first['snapshot_seq'])
                page = _request("GET", path + urllib.parse.urlencode(query))
            file.flush(); os.fsync(file.fileno())
        if target.exists() and not parsed.overwrite: raise ValueError("output appeared during export; refusing to replace it")
        if parsed.overwrite:
            os.replace(name, target)
        else:
            os.link(name, target)
    finally:
        if os.path.exists(name): os.unlink(name)
    return {"artifact_id": parsed.artifact_id, "output": str(target), "events": total,
            "snapshot_seq": first['snapshot_seq'], "next_seq": page['next_seq']}


def main(argv: list[str]) -> int:
    usage = ("usage: agent_artifacts.py create SESSION TYPE TITLE [SUMMARY] [JSON_PAYLOAD] | "
             "create-form SESSION TITLE HTML_FILE SCHEMA_FILE --version V --artifact-id ID | "
             "create-report SESSION TITLE HTML_FILE [--summary S] [--artifact-id ID] [--version V] | "
             "form-events ARTIFACT_ID [--draft-key KEY] [--after N] [--limit N] [--output JSONL] [--overwrite] | "
             "report-history ARTIFACT_ID [--version V] [--limit N] [--offset N] | "
             "decision SESSION TITLE QUESTION YES_LABEL NO_LABEL [JSON_PAYLOAD] [OPTIONS] | "
             "question SESSION TITLE QUESTION JSON_OPTIONS [OPTIONS] | "
             "input SESSION TITLE PROMPT [--hint one_time_code] [--expires-in S] [OPTIONS] | "
             "wait ARTIFACT_ID [--timeout S] | "
             "attention [--session SESSION] [--include-archived] | withdraw SESSION DECISION_ID | "
             "update ARTIFACT_ID STATUS [JSON_PAYLOAD] | progress ARTIFACT_ID VALUE [CONTENT] | list SESSION")
    try:
        cmd = argv[1]
        if cmd == "create-form":
            result = _create_form(argv[2:])
        elif cmd == "create-report":
            result = _create_report(argv[2:])
        elif cmd == "form-events":
            result = _form_events(argv[2:])
        elif cmd == "report-history":
            result = _report_history(argv[2:])
        elif cmd == "create" and len(argv) in {5, 6, 7}:
            result = _request("POST", "/artifacts", {
                "session": argv[2], "type": argv[3], "title": argv[4],
                "summary": argv[5] if len(argv) >= 6 else "",
                "payload": json.loads(argv[6]) if len(argv) == 7 else {}})["artifact"]
        elif cmd in {"decision", "question", "input"}:
            result = _decision_request(cmd, argv[2:])
        elif cmd == "wait":
            result, code = _wait(argv[2:])
            print(json.dumps(result, ensure_ascii=False)); return code
        elif cmd == "attention":
            result = _attention(argv[2:])
        elif cmd == "withdraw" and len(argv) == 4:
            result = _request("POST", "/decisions/" + urllib.parse.quote(argv[3]) + "/withdraw",
                              {"session": argv[2]})["artifact"]
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
