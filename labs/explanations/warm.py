#!/usr/bin/env python3
"""Warm app-server transport experiment; fresh ephemeral thread per sample.

Private Codex configuration directory, existing login linked (not copied),
read-only sandbox, no action tools, no service installation or daemon.
"""
import argparse
from collections import deque
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

from run import PROMPTS, fixtures, requests, shipping, validate_result


class Captured(Exception):
    pass


def production_configuration():
    result = {}
    def capture(args, **kwargs):
        result.update(args=args, env=kwargs["env"])
        raise Captured()
    with shipping.ToolExplanations() as worker, patch.object(shipping.subprocess, "Popen", capture):
        try:
            worker._run_codex(3, [{"id": "1", "activity": {}}])
        except Captured:
            pass
    return result


class WarmClient:
    def __init__(self, root):
        configuration = production_configuration()
        args = ["codex", "app-server", "--listen", "stdio://"]
        source = configuration["args"]
        for index, argument in enumerate(source[:-1]):
            if argument in {"--disable", "--enable", "-c"}:
                value = source[index + 1]
                if not value.startswith("model_instructions_file="):
                    args.extend([argument, value])
        environment = configuration["env"].copy()
        login_home = Path(environment.get("CODEX_HOME", str(Path.home() / ".codex")))
        private_config = root / "codex-profile"
        private_config.mkdir()
        auth = login_home / "auth.json"
        if auth.exists():
            (private_config / "auth.json").symlink_to(auth)
        environment["CODEX_HOME"] = str(private_config)
        self.root = root
        self.incoming = queue.Queue()
        self.events = deque()
        self.sequence = 0
        started = time.perf_counter()
        self.proc = subprocess.Popen(args, cwd=root, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "clarp_explanation_lab", "version": "1"},
                "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise
        self.startup_ms = round((time.perf_counter() - started) * 1000, 2)

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.incoming.put((time.perf_counter(), json.loads(line)))
            except ValueError:
                continue
        self.incoming.put((time.perf_counter(), None))

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout):
        timestamp, message = self.incoming.get(timeout=max(.001, timeout))
        if message is None:
            raise RuntimeError("app-server exited")
        if "method" in message and "id" in message:
            self.send({"id": message["id"], "error": {"code": -32601, "message": "No tools or approvals in this lab"}})
            raise RuntimeError("unexpected tool/approval request")
        return timestamp, message

    def request(self, method, params):
        self.sequence += 1
        identity = self.sequence
        self.send({"id": identity, "method": method, "params": params})
        deadline = time.perf_counter() + 15
        while time.perf_counter() < deadline:
            timestamp, message = self.receive(deadline - time.perf_counter())
            if message.get("id") == identity:
                if "error" in message:
                    raise RuntimeError(f"{method} rejected with code {message['error'].get('code')}")
                return message["result"]
            self.events.append((timestamp, message))
        raise TimeoutError(method)

    def trial(self, prompt, cases, repetition):
        self.events.clear()
        started = time.perf_counter()
        result = self.request("thread/start", {"model": shipping.MODEL, "cwd": str(self.root),
            "sandbox": "read-only", "approvalPolicy": "never", "ephemeral": True,
            "baseInstructions": PROMPTS[prompt] + shipping.POLICIES[3],
            "developerInstructions": "", "environments": [], "dynamicTools": []})
        thread = result["thread"]
        if thread.get("ephemeral") is not True:
            raise RuntimeError("thread is not ephemeral")
        thread_id = thread["id"]
        record = {"transport": "app-server", "prompt": prompt, "model": shipping.MODEL,
            "effort": "low", "batch_size": len(cases), "repetition": repetition,
            "startup_ms": self.startup_ms, "fresh_thread": True,
            "thread_ready_ms": round((time.perf_counter() - started) * 1000, 2)}
        schema = {"type": "object", "properties": {"explanations": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "string", "enum": [str(i + 1) for i in range(len(cases))]}, "text": {"type": "string"}},
            "required": ["id", "text"], "additionalProperties": False}}}, "required": ["explanations"], "additionalProperties": False}
        self.request("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": json.dumps({"requests": requests(cases)})}],
            "model": shipping.MODEL, "effort": "low", "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly"}, "outputSchema": schema})
        deadline = started + 45
        answer = ""
        while time.perf_counter() < deadline:
            timestamp, event = self.events.popleft() if self.events else self.receive(deadline - time.perf_counter())
            method, params = event.get("method"), event.get("params", {})
            if params.get("threadId") not in (None, thread_id):
                continue
            if method == "item/agentMessage/delta":
                record.setdefault("first_delta_ms", round((timestamp - started) * 1000, 2))
            if method in {"item/started", "item/completed"}:
                item = params.get("item", {})
                if item.get("type") in {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}:
                    raise RuntimeError("unexpected action tool")
                if method == "item/completed" and item.get("type") == "agentMessage":
                    answer = item.get("text", "")
            if method == "thread/tokenUsage/updated":
                record["usage"] = params.get("tokenUsage", {}).get("last", {})
            if method == "turn/completed":
                if params.get("turn", {}).get("status") != "completed":
                    raise RuntimeError("turn failed")
                break
        else:
            raise TimeoutError("turn")
        rows = json.loads(answer)["explanations"]
        translations = {r["id"]: r["text"] for r in rows}
        record["valid"] = len(rows) == len(translations) and validate_result(translations, len(cases))
        record["answers"] = {case["case"]: translations.get(str(i + 1)) for i, case in enumerate(cases)}
        record["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        self.request("thread/unsubscribe", {"threadId": thread_id})
        return record

    def close(self):
        shipping.ToolExplanations._kill(self.proc)
        self.proc.wait(timeout=5)
        self.reader.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--rounds", type=int, choices=range(1, 7), default=3)
    parser.add_argument("--prompt", choices=list(PROMPTS), default="baseline")
    parser.add_argument("--batch-size", type=int, choices=[1, 4, 8], default=8)
    parser.add_argument("--cold", action="store_true", help="New app-server per sample, holding instruction delivery constant")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        print(f"Dry run: {args.rounds} fresh ephemeral threads in one private app-server, model={shipping.MODEL}")
        return
    with args.output.open("x") as output, tempfile.TemporaryDirectory(prefix="clarp-warm-lab-") as directory:
        client = None
        try:
            for repetition in range(args.rounds):
                fresh = client is None
                if fresh:
                    root = Path(directory) / str(repetition)
                    root.mkdir()
                    client = WarmClient(root)
                record = client.trial(args.prompt, fixtures()[:args.batch_size], repetition)
                record["transport"] = "app-server-cold" if args.cold else "app-server"
                record["elapsed_including_startup_ms"] = round(record["total_ms"] + (client.startup_ms if fresh else 0), 2)
                record["process_reused"] = not fresh
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                print(json.dumps({k: record[k] for k in ("repetition", "startup_ms", "total_ms", "valid")}), flush=True)
                if args.cold:
                    client.close()
                    client = None
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    main()
