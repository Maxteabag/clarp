"""`clarp-admin controller-action`: custom actions a Flic or Watch gesture can
run (docs/controller-actions.md). Kept here so clarp-admin only registers it."""
from __future__ import annotations

import json
import secrets
import sys
import urllib.error
import urllib.parse
from datetime import datetime


def _controller_body(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"--body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise SystemExit("--body must be a JSON object")
    return body


def _controller_action_fields(args) -> dict:
    fields = {}
    if args.title is not None: fields["title"] = args.title
    if args.http_post is not None: fields.update(type="http_post", url=args.http_post)
    if args.agent is not None: fields.update(type="agent_prompt", session=args.agent)
    if args.text is not None: fields["text"] = args.text
    body = _controller_body(args.body)
    if body is not None: fields["body"] = body
    return fields


def execute(args, api_request) -> int:
    """Custom actions a Flic/Watch gesture can run (docs/controller-actions.md)."""
    cmd = args.controller_action_command
    try:
        if cmd == "list":
            state = api_request("GET", "/controller-actions")
            if args.json:
                print(json.dumps(state, indent=2))
                return 0
            gestures: dict[str, list[str]] = {}
            for m in state.get("mappings", []):
                device = "" if m["device"] == "*" else f"[{m['device']}] "
                gestures.setdefault(m["action_id"], []).append(
                    f"{device}{m['button']} {m['gesture']}")
            if not state.get("actions"):
                print("No controller actions.")
            for a in state.get("actions", []):
                target = (f"POST {a.get('url')} {json.dumps(a.get('body', {}))}"
                          if a["type"] == "http_post" else f"prompt {a.get('session')}: {a.get('text')}")
                flag = "" if a.get("enabled", True) else " (disabled)"
                print(f"• {a['id']}{flag} — {a['title']}\n    {target}")
                for gesture in gestures.get(a["id"], []):
                    print(f"    ← {gesture}")
            return 0
        if cmd == "add":
            fields = _controller_action_fields(args)
            if "type" not in fields:
                raise SystemExit("add needs --http-post URL or --agent SESSION")
            if fields["type"] == "http_post":
                fields.setdefault("body", {})
            action = api_request("POST", "/controller-actions", {
                "id": args.action_id, "title": args.title, "type": fields.pop("type"),
                "enabled": not args.disabled, **fields})
            print(f"✓ Created controller action {action.get('action', {}).get('id', args.action_id)}")
            return 0
        if cmd == "update":
            fields = _controller_action_fields(args)
            if args.enable or args.disable:
                fields["enabled"] = bool(args.enable)
            if not fields:
                raise SystemExit("nothing to update")
            api_request("POST", "/controller-actions/update", {"id": args.action_id, **fields})
            print(f"✓ Updated controller action {args.action_id}")
            return 0
        if cmd == "remove":
            result = api_request("POST", "/controller-actions/delete", {"id": args.action_id})
            print(f"✓ Removed {args.action_id} ({result.get('mappings_removed', 0)} gesture mapping(s) cleared)")
            return 0
        if cmd in ("map", "unmap"):
            action_id = args.action if cmd == "map" else None
            api_request("POST", "/controller-actions/mappings", {
                "device": args.device, "button": args.button, "gesture": args.gesture,
                "action_id": action_id})
            target = action_id or "its voice behaviour"
            print(f"✓ {args.button} {args.gesture} on {args.device} → {target}")
            return 0
        if cmd == "run":
            result = api_request(
                "POST", f"/controller-actions/{urllib.parse.quote(args.action_id, safe='')}/invoke",
                {"request_id": f"clarp-admin-{secrets.token_hex(8)}"}, timeout=30)
            print(json.dumps(result, indent=2))
            return 0 if result.get("ok") else 1
        if cmd == "audit":
            for row in api_request("GET", f"/controller-actions/audit?limit={args.limit}").get("entries", []):
                when = datetime.fromtimestamp(row["at"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
                outcome = "" if row["event"] != "run" else (" ok" if row.get("ok") else f" FAILED {row.get('error', '')}")
                print(f"{when} {row['event']:<6} {row.get('action_id', ''):<24} {row['actor']}{outcome}")
            return 0
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error") or detail
        except ValueError:
            pass
        print(f"clarp-admin controller-action {cmd}: {detail}", file=sys.stderr)
        return 1
    raise SystemExit(f"unknown controller-action command {cmd}")


def add_parsers(sub, handler) -> None:
    controller = sub.add_parser(
        "controller-action", help="custom actions a Flic or Watch gesture can run"
    ).add_subparsers(dest="controller_action_command", required=True)
    ca_list = controller.add_parser("list", help="actions and the gestures mapped to them")
    ca_list.add_argument("--json", action="store_true")
    ca_list.set_defaults(func=handler)
    for name in ("add", "update"):
        ca = controller.add_parser(name, help=f"{name} an action")
        ca.add_argument("action_id", help="lowercase id, e.g. rk-demo-leak")
        ca.add_argument("--title", required=(name == "add"))
        ca.add_argument("--http-post", metavar="URL",
                        help="POST --body to this allow-listed URL")
        ca.add_argument("--body", help="JSON object sent by --http-post")
        ca.add_argument("--agent", metavar="SESSION", help="send --text to this agent")
        ca.add_argument("--text", help="prompt text for --agent")
        if name == "add":
            ca.add_argument("--disabled", action="store_true")
        else:
            ca.add_argument("--enable", action="store_true")
            ca.add_argument("--disable", action="store_true")
        ca.set_defaults(func=handler)
    ca_rm = controller.add_parser("remove", help="delete an action and its mappings")
    ca_rm.add_argument("action_id")
    ca_rm.set_defaults(func=handler)
    for name in ("map", "unmap"):
        ca = controller.add_parser(name, help=f"{name} a button gesture")
        ca.add_argument("--device", default="*", help="controller id, or * for every controller")
        ca.add_argument("--button", required=True, choices=("primary", "secondary"))
        ca.add_argument("--gesture", required=True)
        if name == "map":
            ca.add_argument("--action", required=True, help="action id")
        ca.set_defaults(func=handler)
    ca_run = controller.add_parser("run", help="run an action now")
    ca_run.add_argument("action_id")
    ca_run.set_defaults(func=handler)
    ca_audit = controller.add_parser("audit", help="recent runs and changes")
    ca_audit.add_argument("--limit", type=int, default=30)
    ca_audit.set_defaults(func=handler)

