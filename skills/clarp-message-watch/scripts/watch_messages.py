#!/usr/bin/env python3
"""Reusable message watchers for expected WhatsApp and Himalaya replies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_SESSION = os.environ.get("CLAUDE_PWA_SESSION", "").strip()
STATUS_COMMAND = "clarp-agent-bg"
STATE_DIR = Path("/var/tmp/message-watch")


def run(cmd: list[str], timeout: int = 60, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, cwd=cwd)


def set_status(session: str, state: str, message: str | None = None) -> None:
    cmd = [STATUS_COMMAND, session, state]
    if message:
        cmd.append(message)
    try:
        run(cmd, timeout=10)
    except Exception:
        pass


def logical_job_id(session: str, provider: str, label: str) -> str:
    digest = hashlib.sha256(f"{session}\0{provider}\0{label}".encode()).hexdigest()[:16]
    return f"message-watch-{provider}-{digest}"


def register_jobs(session: str, provider: str, labels: set[str]) -> dict[str, str]:
    jobs: dict[str, str] = {}
    for label in sorted(labels):
        job_id = logical_job_id(session, provider, label)
        title = f"{provider.title()}: {label}"
        detail = f"Waiting for a new message from {label}"
        proc = run([
            STATUS_COMMAND, session, "job-upsert", job_id,
            provider, title, detail,
        ], timeout=10)
        handle = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if proc.returncode == 0 and handle:
            jobs[label] = handle
        else:
            print(f"warning: could not register {title}: {proc.stderr.strip()}", file=sys.stderr)
    return jobs


def job_is_active(job_handle: str | None) -> bool:
    if not job_handle:
        return False
    try:
        return run([
            STATUS_COMMAND, "_", "job-active", job_handle,
        ], timeout=10).returncode == 0
    except Exception:
        return False


def finish_jobs(session: str, job_handles: dict[str, str], *,
                failed: bool = False, reason: str = "") -> None:
    command = "job-fail" if failed else "job-finish"
    for handle in job_handles.values():
        try:
            args = [STATUS_COMMAND, session, command, handle]
            if failed:
                args.append(reason or "worker_failed")
            run(args, timeout=10)
        except Exception:
            pass


def send_prompt(session: str, text: str) -> bool:
    # Watcher events are external signals the user explicitly asked us to monitor.
    # Keep them distinct from routine automation so completed summaries can
    # notify the user without making every scheduled automation turn user-facing,
    # distinctly from a message the user actually sent.
    return run([
        "clarp-admin", "prompt", "--to", session, "--text", text,
        "--origin", "watcher",
    ], timeout=30).returncode == 0


def parse_mapping(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        if "=" in raw:
            key, label = raw.split("=", 1)
        else:
            key, label = raw, raw
        key = key.strip()
        label = label.strip() or key
        if key:
            result[key] = label
    return result


def parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_json_object(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


# --- WhatsApp state persistence ---

def state_path_for_session(session: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session)
    return STATE_DIR / f"whatsapp-{safe}.json"


def load_wa_state(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_wa_state(path: Path, state: dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, str(path))


# --- WhatsApp message normalization ---

def normalize_whatsapp_message(payload: Any) -> dict[str, Any] | None:
    """Extract a normalized message dict from a wacli webhook payload.

    wacli sends flat payloads with PascalCase keys: Chat, SenderJID,
    PushName, Text, Timestamp, FromMe, ID, ReplyToID. Some older or
    wrapped formats nest under a "message" key with varying casing.
    We try all known variants."""
    if not isinstance(payload, dict):
        return None
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    if not isinstance(msg, dict):
        return None

    # JID: wacli uses Chat (an @lid JID) as the primary identifier
    chat_jid = str(
        msg.get("Chat")
        or msg.get("ChatJID")
        or msg.get("chat_jid")
        or msg.get("chatJID")
        or msg.get("chat")
        or ""
    )
    sender_jid = str(
        msg.get("SenderJID")
        or msg.get("sender_jid")
        or msg.get("from")
        or ""
    )

    msg_id = str(msg.get("ID") or msg.get("MsgID") or msg.get("id") or msg.get("message_id") or "")

    from_me = msg.get("FromMe")
    if from_me is None:
        from_me = msg.get("from_me") or msg.get("fromMe") or False

    text = str(msg.get("Text") or msg.get("text") or msg.get("DisplayText") or msg.get("display_text") or "")
    timestamp = str(msg.get("Timestamp") or msg.get("timestamp") or datetime.now(timezone.utc).isoformat())
    reply_to_id = str(msg.get("ReplyToID") or msg.get("reply_to_id") or msg.get("quoted_msg_id") or "")
    push_name = str(
        msg.get("PushName")
        or msg.get("push_name")
        or msg.get("SenderName")
        or msg.get("sender_name")
        or msg.get("ChatName")
        or msg.get("chat_name")
        or ""
    )

    return {
        "chat_jid": chat_jid,
        "sender_jid": sender_jid,
        "id": msg_id,
        "from_me": bool(from_me),
        "text": text,
        "timestamp": timestamp,
        "reply_to_id": reply_to_id,
        "push_name": push_name,
    }


def match_name(push_name: str, watch_name: dict[str, str]) -> str | None:
    """Match a push name against watched names using substring matching."""
    push_lower = push_name.lower()
    if not push_lower:
        return None
    # Exact match first
    if push_lower in watch_name:
        return watch_name[push_lower]
    # Substring match: either direction
    for key, label in watch_name.items():
        if key in push_lower or push_lower in key:
            return label
    return None


def is_ignored_whatsapp_message(msg: dict[str, Any]) -> bool:
    """Drop WhatsApp Status events before any watch matching or JID learning."""
    return msg.get("chat_jid") == "status@broadcast"


class WhatsappHandler(BaseHTTPRequestHandler):
    session: str = DEFAULT_SESSION
    watch_jid: dict[str, str] = {}
    watch_name: dict[str, str] = {}
    reply_watch_path: str | None = None
    cutoff: datetime = datetime.now(timezone.utc)
    instructions: str = ""
    log_path: str = "/var/tmp/message-watch-whatsapp-payloads.log"
    seen: set[str] = set()
    # Maps @lid JIDs to labels, learned from matched messages
    learned_jids: dict[str, str] = {}
    state_file: Path | None = None
    job_ids: dict[str, str] = {}

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format % args))

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        msg = normalize_whatsapp_message(payload)
        if msg:
            self._handle(msg)
        self.send_response(204)
        self.end_headers()

    def _handle(self, msg: dict[str, Any]) -> None:
        if msg["from_me"] or is_ignored_whatsapp_message(msg):
            return

        # Dedup by message ID
        if msg["id"] and msg["id"] in self.seen:
            return

        # Try to match: JID → learned JID → push name → reply-watch
        chat = msg["chat_jid"]
        sender = msg["sender_jid"]
        push = msg["push_name"]

        label = (
            self.watch_jid.get(chat)
            or self.watch_jid.get(sender)
            or self.learned_jids.get(chat)
            or self.learned_jids.get(sender)
            or match_name(push, self.watch_name)
            or load_json_object(self.reply_watch_path).get(msg.get("reply_to_id") or "")
        )
        if not label:
            return
        if not job_is_active(self.job_ids.get(label)):
            return

        # Timestamp filter
        try:
            ts = parse_iso(msg["timestamp"])
            if ts <= self.cutoff:
                if msg["id"]:
                    self.seen.add(msg["id"])
                    self._save_state()
                return
        except Exception:
            pass

        body = msg["text"] or "(non-text/media message)"
        prompt = (
            f"{self.instructions}\n\n"
            f"Source: WhatsApp\n"
            f"Chat: {label}\n"
            f"JID: {chat}\n"
            f"Sender name: {push}\n"
            f"Time: {msg['timestamp']}\n\n"
            f"{body}"
        )
        if not send_prompt(self.session, prompt):
            return

        # Commit dedupe/routing state only after durable self-prompt admission.
        if chat and chat not in self.learned_jids:
            self.learned_jids[chat] = label
        if sender and sender != chat and sender not in self.learned_jids:
            self.learned_jids[sender] = label
        if msg["id"]:
            self.seen.add(msg["id"])
        self._save_state()

    def _save_state(self) -> None:
        if not self.state_file:
            return
        state = {
            "seen": sorted(self.seen),
            "learned_jids": self.learned_jids,
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        save_wa_state(self.state_file, state)


def heartbeat(session: str, status: str, stop: threading.Event) -> None:
    while not stop.is_set():
        set_status(session, "on", status)
        stop.wait(60)


def reconcile_whatsapp_store(args: argparse.Namespace) -> None:
    """Recover watched messages stored while the live watcher was unavailable."""
    handler = object.__new__(WhatsappHandler)
    recovered = 0
    for jid in WhatsappHandler.watch_jid:
        proc = run(
            [
                args.wacli_bin,
                "messages",
                "list",
                "--chat",
                jid,
                "--limit",
                str(args.catchup_limit),
                "--json",
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            print(f"warning: catch-up failed for {jid}: {proc.stderr.strip()}", file=sys.stderr, flush=True)
            continue
        try:
            payload = json.loads(proc.stdout)
            messages = payload.get("data", {}).get("messages") or []
            if not isinstance(messages, list):
                messages = []
        except (json.JSONDecodeError, AttributeError):
            print(f"warning: invalid catch-up output for {jid}", file=sys.stderr, flush=True)
            continue
        for raw in sorted(messages, key=lambda item: str(item.get("Timestamp") or "")):
            msg = normalize_whatsapp_message(raw)
            if not msg or msg["from_me"] or msg["id"] in WhatsappHandler.seen:
                continue
            handler._handle(msg)
            if msg["id"] in WhatsappHandler.seen:
                recovered += 1
    handler._save_state()
    print(f"catch-up recovered {recovered} watched messages", flush=True)


def run_whatsapp(args: argparse.Namespace) -> int:
    if not args.watch and not args.watch_name and not args.reply_watch_json:
        print("error: pass at least one --watch, --watch-name, or --reply-watch-json", file=sys.stderr)
        return 2

    # Load persisted state for restart continuity
    sf = state_path_for_session(args.session)
    prev_state = load_wa_state(sf)

    WhatsappHandler.session = args.session
    WhatsappHandler.watch_jid = parse_mapping(args.watch)
    WhatsappHandler.watch_name = {k.lower(): v for k, v in parse_mapping(args.watch_name).items()}
    WhatsappHandler.reply_watch_path = args.reply_watch_json
    WhatsappHandler.instructions = args.instructions
    WhatsappHandler.log_path = args.log
    WhatsappHandler.state_file = sf
    labels = set(WhatsappHandler.watch_jid.values()) | set(WhatsappHandler.watch_name.values())
    labels |= set(load_json_object(args.reply_watch_json).values())
    WhatsappHandler.job_ids = {}

    # Restore seen set from previous run to avoid re-notifying
    WhatsappHandler.seen = set(prev_state.get("seen", []))

    # Restore learned JID→label mappings from previous run
    WhatsappHandler.learned_jids = dict(prev_state.get("learned_jids", {}))

    # Cutoff: use --started-after if given, otherwise fall back to the
    # previous watcher's last_active time (survives restarts without gaps).
    # Only use "now" as a last resort when there's no prior state at all.
    if args.started_after:
        WhatsappHandler.cutoff = parse_iso(args.started_after)
    elif prev_state.get("last_active"):
        WhatsappHandler.cutoff = parse_iso(prev_state["last_active"])
    else:
        WhatsappHandler.cutoff = datetime.now(timezone.utc)

    print(f"cutoff: {WhatsappHandler.cutoff.isoformat()}", flush=True)
    print(f"restored {len(WhatsappHandler.seen)} seen IDs, {len(WhatsappHandler.learned_jids)} learned JIDs", flush=True)
    stop = threading.Event()
    try:
        server = ThreadingHTTPServer((args.host, args.port), WhatsappHandler)
    except BaseException:
        set_status(args.session, "off")
        raise
    port = server.server_address[1]
    webhook = f"http://{args.host}:{port}/"
    sync_cmd = [
        args.wacli_bin,
        "sync",
        "--follow",
        "--webhook",
        webhook,
        "--stale-threshold",
        args.stale_threshold,
        "--max-reconnect",
        str(args.max_reconnect),
        "--presence-mode",
        args.presence_mode,
    ]
    if args.webhook_allow_private:
        sync_cmd.append("--webhook-allow-private")

    try:
        child = subprocess.Popen(sync_cmd)
    except BaseException:
        set_status(args.session, "off")
        server.server_close()
        raise

    def fail_startup() -> None:
        stop.set()
        set_status(args.session, "off")
        finish_jobs(
            args.session, WhatsappHandler.job_ids,
            failed=True, reason="whatsapp_startup_failed",
        )
        if child.poll() is None:
            child.terminate()
        server.server_close()

    try:
        WhatsappHandler.job_ids = register_jobs(args.session, "whatsapp", labels)
        if set(WhatsappHandler.job_ids) != labels:
            raise RuntimeError("background job registration failed for one or more targets")
        reconcile_whatsapp_store(args)
        unexpected_child_exit = threading.Event()
        status_thread = threading.Thread(
            target=heartbeat, args=(args.session, args.status, stop), daemon=True)
        status_thread.start()
    except BaseException:
        fail_startup()
        raise

    def shutdown(*_: Any) -> None:
        stop.set()
        # Save final state before exiting
        WhatsappHandler._save_state(WhatsappHandler)  # type: ignore[arg-type]
        set_status(args.session, "off")
        finish_jobs(args.session, WhatsappHandler.job_ids)
        if child.poll() is None:
            child.terminate()
        # BaseServer.shutdown must run from a different thread than serve_forever.
        threading.Thread(target=server.shutdown, daemon=True).start()

    def monitor_child() -> None:
        returncode = child.wait()
        if stop.is_set():
            return
        unexpected_child_exit.set()
        print(f"wacli sync exited unexpectedly with status {returncode}", file=sys.stderr, flush=True)
        server.shutdown()

    try:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        threading.Thread(target=monitor_child, daemon=True).start()
    except BaseException:
        fail_startup()
        raise

    print(f"watching WhatsApp webhook on {webhook}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        WhatsappHandler._save_state(WhatsappHandler)  # type: ignore[arg-type]
        set_status(args.session, "off")
        finish_jobs(
            args.session, WhatsappHandler.job_ids,
            failed=unexpected_child_exit.is_set(),
            reason="whatsapp_child_exited",
        )
        if child.poll() is None:
            child.terminate()
    return 1 if unexpected_child_exit.is_set() else 0


# --- Himalaya email watcher (unchanged) ---

def state_default(session: str, account: str, folder: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{session}_{account}_{folder}")
    return f"/var/tmp/message-watch-himalaya-{safe}.json"


def load_seen(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(item) for item in data}
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return set()


def save_seen(path: str, seen: set[str]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)
    os.replace(tmp, path)


def json_from_stdout(stdout: str) -> Any:
    text = stdout.strip()
    start = min([idx for idx in [text.find("["), text.find("{")] if idx >= 0], default=-1)
    if start > 0:
        text = text[start:]
    return json.loads(text)


def list_envelopes(args: argparse.Namespace) -> list[dict[str, Any]]:
    proc = run(
        [
            args.himalaya_bin,
            "envelope",
            "list",
            "--account",
            args.account,
            "--folder",
            args.folder,
            "--page-size",
            str(args.page_size),
            "-o",
            "json",
        ],
        timeout=90,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    data = json_from_stdout(proc.stdout)
    return data if isinstance(data, list) else []


def read_email(args: argparse.Namespace, msg_id: str) -> str:
    proc = run(
        [
            args.himalaya_bin,
            "message",
            "read",
            "--account",
            args.account,
            "--folder",
            args.folder,
            msg_id,
        ],
        timeout=90,
    )
    if proc.returncode != 0:
        return proc.stderr.strip()
    return proc.stdout.strip()[-args.body_chars :]


def email_match(env: dict[str, Any], senders: dict[str, str], subject_keywords: list[str]) -> str | None:
    sender = ((env.get("from") or {}).get("addr") or "").lower()
    subject = (env.get("subject") or "").lower()
    if sender in senders:
        return senders[sender]
    for keyword in subject_keywords:
        if keyword.lower() in subject:
            return keyword
    return None


def notify_email(args: argparse.Namespace, label: str, env: dict[str, Any], body: str) -> bool:
    sender = env.get("from") or {}
    prompt = (
        f"{args.instructions}\n\n"
        f"Source: Himalaya email\n"
        f"Label: {label}\n"
        f"From: {sender.get('name') or ''} <{sender.get('addr') or ''}>\n"
        f"Subject: {env.get('subject') or ''}\n"
        f"Date: {env.get('date') or ''}\n\n"
        f"{body}"
    )
    return send_prompt(args.session, prompt)


def poll_himalaya(args: argparse.Namespace, seen: set[str], baseline: bool,
                  job_ids: dict[str, str]) -> bool:
    changed = False
    senders = {k.lower(): v for k, v in parse_mapping(args.from_).items()}
    envelopes = list_envelopes(args)
    for env in envelopes:
        msg_id = str(env.get("id") or "")
        if not msg_id:
            continue
        label = email_match(env, senders, args.subject_keyword)
        if not label:
            continue
        if not job_is_active(job_ids.get(label)):
            continue
        if msg_id in seen:
            continue
        if not baseline or args.notify_existing:
            if not notify_email(args, label, env, read_email(args, msg_id)):
                continue
        seen.add(msg_id)
        changed = True
    return changed


def run_himalaya(args: argparse.Namespace) -> int:
    if not args.from_ and not args.subject_keyword:
        print("error: pass at least one --from or --subject-keyword", file=sys.stderr)
        return 2
    if not args.state:
        args.state = state_default(args.session, args.account, args.folder)

    seen = load_seen(args.state)
    baseline = not seen
    senders = parse_mapping(args.from_)
    labels = set(senders.values()) | set(args.subject_keyword)
    job_ids = register_jobs(args.session, "email", labels)
    if set(job_ids) != labels:
        finish_jobs(
            args.session, job_ids, failed=True,
            reason="himalaya_startup_failed")
        return 1
    stop = threading.Event()

    def shutdown(*_: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    set_status(args.session, "on", args.status)
    status_thread = threading.Thread(
        target=heartbeat, args=(args.session, args.status, stop), daemon=True)
    status_thread.start()
    try:
        while not stop.is_set():
            try:
                changed = poll_himalaya(args, seen, baseline, job_ids)
                if changed or baseline:
                    save_seen(args.state, seen)
                baseline = False
            except Exception as exc:
                print(f"himalaya watch error: {exc}", file=sys.stderr, flush=True)
            if args.once:
                break
            if stop.wait(args.interval):
                break
    finally:
        set_status(args.session, "off")
        finish_jobs(args.session, job_ids)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch expected WhatsApp or Himalaya replies and self-prompt an agent.")
    sub = parser.add_subparsers(dest="provider", required=True)

    default_instructions = (
        "Expected message arrived. Summarize it for the user and recommend the next action. "
        "If an outward reply is needed, draft the exact message and ask the user for approval before sending."
    )

    whatsapp = sub.add_parser("whatsapp", help="Watch WhatsApp replies through wacli webhook sync")
    whatsapp.add_argument(
        "--session", default=DEFAULT_SESSION, required=not bool(DEFAULT_SESSION))
    whatsapp.add_argument("--watch", action="append", default=[], metavar="JID=Label")
    whatsapp.add_argument("--watch-name", action="append", default=[], metavar="NAME=Label")
    whatsapp.add_argument("--reply-watch-json")
    whatsapp.add_argument("--started-after", help="Override cutoff timestamp (default: resume from previous run's state)")
    whatsapp.add_argument("--instructions", default=default_instructions)
    whatsapp.add_argument("--status", default="Watching WhatsApp")
    whatsapp.add_argument("--host", default="127.0.0.1")
    whatsapp.add_argument("--port", type=int, default=0)
    whatsapp.add_argument("--log", default="/var/tmp/message-watch-whatsapp-payloads.log")
    whatsapp.add_argument("--wacli-bin", default="wacli")
    whatsapp.add_argument("--stale-threshold", default="30s")
    whatsapp.add_argument("--max-reconnect", type=int, default=0)
    whatsapp.add_argument("--catchup-limit", type=int, default=100)
    whatsapp.add_argument(
        "--presence-mode",
        choices=("normal", "quiet"),
        default="quiet",
        help="wacli presence mode (quiet helps preserve primary-phone notifications)",
    )
    whatsapp.add_argument("--webhook-allow-private", action=argparse.BooleanOptionalAction, default=True)
    whatsapp.set_defaults(func=run_whatsapp)

    himalaya = sub.add_parser("himalaya", help="Poll Himalaya for expected email replies")
    himalaya.add_argument(
        "--session", default=DEFAULT_SESSION, required=not bool(DEFAULT_SESSION))
    himalaya.add_argument("--account", default="gmail")
    himalaya.add_argument("--folder", default="INBOX")
    himalaya.add_argument("--from", dest="from_", action="append", default=[], metavar="EMAIL=Label")
    himalaya.add_argument("--subject-keyword", action="append", default=[])
    himalaya.add_argument("--interval", type=int, default=180)
    himalaya.add_argument("--page-size", type=int, default=20)
    himalaya.add_argument("--state")
    himalaya.add_argument("--notify-existing", action="store_true")
    himalaya.add_argument("--once", action="store_true")
    himalaya.add_argument("--body-chars", type=int, default=5000)
    himalaya.add_argument("--instructions", default=default_instructions)
    himalaya.add_argument("--status", default="Watching email replies")
    himalaya.add_argument("--himalaya-bin", default="himalaya")
    himalaya.set_defaults(func=run_himalaya)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
