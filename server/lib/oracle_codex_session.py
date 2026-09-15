"""A tool-less Codex router owned by one Oracle voice conversation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid


class CodexRouterSession:
    def __init__(self, stop, *, retain_context=False):
        self.stop = stop
        # Each input already contains authoritative conversation/work state.
        # Retaining model proposals risks mistaking an abandoned proposal for
        # an admitted action. Keep process reuse independent of context reuse.
        self.retain_context = retain_context
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.process = None
        self.directory = None
        self.events = queue.Queue()
        self.pending_events = []
        self.serial = 0
        self.thread_id = ""
        self.turns = 0
        self.input_tokens = 0
        self.context_key = ""
        self.auth_key = ""
        threading.Thread(target=self._watch_close, daemon=True, name="oracle-router-lifetime").start()

    def _watch_close(self):
        self.stop.wait()
        self.close()

    def _cancelled(self):
        from .oracle_router import RouterError
        if self.stop.is_set() or self.closed.is_set(): raise RouterError("router_cancelled")

    def _read(self, process, events):
        try:
            while True:
                line = process.stdout.readline(2*1024*1024+1)
                if not line: break
                if len(line) > 2*1024*1024: break
                try: events.put_nowait(json.loads(line))
                except ValueError: continue
                except queue.Full: break
        finally:
            try: events.put_nowait({"closed": True})
            except queue.Full: pass

    def _send(self, value):
        self._cancelled()
        self.process.stdin.write(json.dumps(value)+"\n"); self.process.stdin.flush()

    def _receive(self, deadline):
        from .oracle_router import RouterError
        while True:
            self._cancelled()
            if time.monotonic() >= deadline: raise RouterError("router_timeout")
            try: event = self.events.get(timeout=min(.1, max(.001, deadline-time.monotonic())))
            except queue.Empty: continue
            if event.get("closed"): raise RouterError("codex_router_exited")
            if "method" in event and "id" in event:
                self._send({"id": event["id"], "error": {"code": -32601, "message": "Router does not execute tools or authenticate"}})
                raise RouterError("unexpected_codex_request")
            return event

    def _request(self, method, params, deadline):
        from .oracle_router import RouterError
        self.serial += 1; ident = self.serial
        self._send({"id": ident, "method": method, "params": params})
        while True:
            event = self._receive(deadline)
            if event.get("id") == ident:
                if "error" in event: raise RouterError("codex_protocol_request_failed")
                return event.get("result") or {}
            if "method" in event: self.pending_events.append(event)

    def _reset(self):
        process, self.process = self.process, None
        if process:
            try: process.stdin.close()
            except (OSError, ValueError): pass
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait(timeout=3)
        if self.directory: self.directory.cleanup(); self.directory = None
        self.thread_id = ""; self.turns = 0; self.input_tokens = 0
        self.context_key = ""; self.auth_key = ""; self.pending_events = []

    def close(self):
        self.closed.set()
        with self.lock: self._reset()

    def _ensure(self, deadline):
        from .oracle_router import DISABLED_FEATURES, RouterError, subscription_environment
        env = subscription_environment()
        try:
            raw = (Path(env.get("CODEX_HOME") or Path.home()/".codex")/"auth.json").read_bytes()
            auth = json.loads(raw)
            if auth.get("auth_mode") != "chatgpt" or not auth.get("tokens", {}).get("access_token"):
                raise ValueError("not ChatGPT")
        except (OSError, ValueError):
            self._reset(); raise RouterError("codex_chatgpt_login_required") from None
        key = hashlib.sha256(raw).hexdigest()
        reused = bool(self.process and self.process.poll() is None and self.auth_key == key)
        if reused: return True
        self._reset()
        self.directory = tempfile.TemporaryDirectory(prefix="clarp-oracle-session-")
        root = Path(self.directory.name); profile = root/"profile"; profile.mkdir(mode=0o700)
        snapshot = {**auth, "OPENAI_API_KEY": None, "tokens": {**auth["tokens"], "refresh_token": ""}}
        credential = profile/"auth.json"; credential.write_text(json.dumps(snapshot)); credential.chmod(0o600)
        (profile/"config.toml").write_text("")
        argv = ["codex", "app-server", "--stdio"]
        for value in ['forced_login_method="chatgpt"', 'approval_policy="never"', 'web_search="disabled"',
                      'project_doc_max_bytes=0', 'mcp_servers={}', 'model_reasoning_effort="low"']:
            argv.extend(["-c", value])
        for feature in DISABLED_FEATURES: argv.extend(["--disable", feature])
        argv.extend(["--enable", "skip_host_skill_discovery"])
        self.events = queue.Queue(maxsize=2048)
        self.process = subprocess.Popen(argv, cwd=root, env={**env, "CODEX_HOME": str(profile)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, args=(self.process, self.events), daemon=True).start()
        self._request("initialize", {"clientInfo": {"name": "clarp_oracle_router", "version": "1"},
                                    "capabilities": {"experimentalApi": True}}, deadline)
        self._send({"method": "initialized", "params": {}})
        self.auth_key = key
        return False

    def route(self, body, timeout, images=()):
        from .oracle_router import MODEL, MAX_RESULT_BYTES, RouterError, proposal_schema
        from jsonschema import validate
        started = time.monotonic(); deadline = started+timeout
        with self.lock:
            self._cancelled()
            try:
                process_reused = self._ensure(deadline)
                ready_ms = (time.monotonic()-started)*1000
                schema = proposal_schema(body["tools"])
                instructions = body["instructions"] + (
                    "\nEach input JSON is the complete current routing snapshot. Previous proposals are not admissions. "
                    "Return a proposal matching the schema; never execute work yourself. "
                    "Use an empty actions list to answer or clarify. Leave message empty when proposing actions. Tool contracts:\n"
                    + json.dumps(body["tools"], ensure_ascii=False))
                image_keys = [hashlib.sha256(image["data"]).hexdigest() for image in images]
                context_key = hashlib.sha256(json.dumps([instructions, schema, image_keys], sort_keys=True).encode()).hexdigest()
                # Removing/changing images or policy retires their model context.
                reuse_thread = bool(self.retain_context and self.thread_id and self.context_key == context_key and self.turns < 32 and self.input_tokens < 32000)
                if not reuse_thread:
                    if self.thread_id:
                        self._request("thread/unsubscribe", {"threadId": self.thread_id}, deadline)
                    self.thread_id = self._request("thread/start", {"cwd": self.directory.name, "model": MODEL,
                        "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": True,
                        "baseInstructions": instructions, "developerInstructions": ""}, deadline)["thread"]["id"]
                    self.turns = 0; self.context_key = context_key
                inputs = [{"type": "text", "text": body["input"]}]
                image_paths = []
                for index, image in enumerate(images):
                    path = Path(self.directory.name)/(f"image-{index}.png" if image["mime_type"] == "image/png" else f"image-{index}.jpg")
                    path.write_bytes(image["data"]); path.chmod(0o600); image_paths.append(path)
                    inputs.append({"type": "localImage", "path": str(path)})
                turn = self._request("turn/start", {"threadId": self.thread_id, "input": inputs,
                    "model": MODEL, "effort": "low", "outputSchema": schema,
                    "approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly"}}, deadline)["turn"]["id"]
                submitted_ms = (time.monotonic()-started)*1000
                text = ""; usage = {}; first_ms = None
                while True:
                    event = self.pending_events.pop(0) if self.pending_events else self._receive(deadline)
                    kind = event.get("method", ""); value = event.get("params") or {}
                    event_turn = value.get("turnId") or (value.get("turn") or {}).get("id")
                    if value.get("threadId") != self.thread_id or event_turn != turn: continue
                    if kind == "item/agentMessage/delta":
                        if first_ms is None: first_ms = (time.monotonic()-started)*1000
                        text += value.get("delta", "")
                    elif kind == "item/completed":
                        item = value.get("item") or {}
                        if item.get("type") == "agentMessage": text = item.get("text", text)
                        elif item.get("type") not in ("userMessage", "reasoning"): raise RouterError("unexpected_codex_tool")
                    elif kind == "thread/tokenUsage/updated": usage = (value.get("tokenUsage") or {}).get("last") or {}
                    elif kind == "turn/completed":
                        if value["turn"].get("status") != "completed": raise RouterError("codex_router_failed")
                        break
                    if len(text.encode()) > MAX_RESULT_BYTES: raise RouterError("oversized_router_result")
                for path in image_paths: path.unlink(missing_ok=True)
                proposal = json.loads(text); validate(proposal, schema)
                if proposal["actions"] and proposal["message"].strip(): raise RouterError("unverified_action_commentary")
                output = [{"type": "function_call", **action, "call_id": "codex-"+uuid.uuid4().hex} for action in proposal["actions"]]
                if not output: output = [{"type": "message", "content": [{"type": "output_text", "text": proposal["message"]}]}]
                self.turns += 1; self.input_tokens = usage.get("inputTokens", 0)
                return {"output": output, "usage": {"input_tokens": usage.get("inputTokens"),
                    "cached_input_tokens": usage.get("cachedInputTokens"), "output_tokens": usage.get("outputTokens")},
                    "transport_metrics": {"transport": "app_server", "process_reused": process_reused,
                        "thread_reused": reuse_thread, "ready_ms": round(ready_ms,1),
                        "submitted_ms": round(submitted_ms,1), "first_delta_ms": round(first_ms,1) if first_ms is not None else None}}
            except BaseException:
                self._reset(); raise
