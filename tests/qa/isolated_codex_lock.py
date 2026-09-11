#!/usr/bin/env python3
"""Isolated Codex writer-lock + Clarp source checks. No network, no live auth.

Run inside Docker with ``--network=none``. Never reads ``~/.codex/auth.json``.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "tests/qa/fake_codex.py"
SERVER_LIB = ROOT / "server/lib"


def rpc(proc, method, params, rid, timeout=5):
    proc.stdin.write(json.dumps({"method": method, "id": rid, "params": params}) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg.get("id") == rid:
            return msg
    raise TimeoutError(method)


def spawn(home: Path):
    env = {**os.environ, "CODEX_HOME": str(home), "CLARP_QA_PROVIDER_ROOT": str(home)}
    return subprocess.Popen(
        [sys.executable, str(FAKE), "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )


def close_stdio(proc, timeout=5):
    if proc.stdin and not proc.stdin.closed:
        proc.stdin.close()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def run_two_process_writer_lock(home: Path) -> None:
    a = spawn(home)
    b = spawn(home)
    try:
        rpc(a, "initialize", {"clientInfo": {"name": "a"}}, 1)
        started = rpc(a, "thread/start", {"cwd": str(home)}, 2)
        tid = started["result"]["thread"]["id"]
        rpc(b, "initialize", {"clientInfo": {"name": "b"}}, 1)
        blocked = rpc(b, "thread/resume", {"threadId": tid, "cwd": str(home)}, 2)
        err = (blocked.get("error") or {}).get("message") or ""
        assert "already has an active writer" in err, blocked
        assert (blocked.get("error") or {}).get("code") == -32600, blocked
    finally:
        a.kill(); b.kill()


def run_one_process_two_threads(home: Path) -> None:
    a = spawn(home)
    try:
        rpc(a, "initialize", {"clientInfo": {"name": "shared"}}, 1)
        first = rpc(a, "thread/start", {"cwd": str(home)}, 2)
        second = rpc(a, "thread/start", {"cwd": str(home)}, 3)
        id1 = first["result"]["thread"]["id"]
        id2 = second["result"]["thread"]["id"]
        assert id1 and id2 and id1 != id2, (first, second)
        resumed = rpc(a, "thread/resume", {"threadId": id1, "cwd": str(home)}, 4)
        assert resumed.get("result", {}).get("thread", {}).get("id") == id1, resumed
        assert "error" not in resumed, resumed
    finally:
        close_stdio(a)


def run_leftover_close_stdin_then_resume(home: Path) -> None:
    leftover = spawn(home)
    try:
        rpc(leftover, "initialize", {"clientInfo": {"name": "leftover"}}, 1)
        started = rpc(leftover, "thread/start", {"cwd": str(home)}, 2)
        tid = started["result"]["thread"]["id"]
        blocked_proc = spawn(home)
        try:
            rpc(blocked_proc, "initialize", {"clientInfo": {"name": "blocked"}}, 1)
            blocked = rpc(blocked_proc, "thread/resume",
                          {"threadId": tid, "cwd": str(home)}, 2)
            err = (blocked.get("error") or {}).get("message") or ""
            assert "already has an active writer" in err, blocked
        finally:
            close_stdio(blocked_proc)
        close_stdio(leftover)
        leftover = None
        fresh = spawn(home)
        try:
            rpc(fresh, "initialize", {"clientInfo": {"name": "fresh"}}, 1)
            resumed = rpc(fresh, "thread/resume",
                          {"threadId": tid, "cwd": str(home)}, 2)
            assert resumed.get("result", {}).get("thread", {}).get("id") == tid, resumed
            assert "error" not in resumed, resumed
        finally:
            close_stdio(fresh)
    finally:
        if leftover is not None:
            leftover.kill()


def run_second_process_still_conflicts(home: Path) -> None:
    """Shared process does not make a second stdio process legal (Ghostty)."""
    shared = spawn(home)
    extra = spawn(home)
    try:
        rpc(shared, "initialize", {"clientInfo": {"name": "shared"}}, 1)
        started = rpc(shared, "thread/start", {"cwd": str(home)}, 2)
        tid = started["result"]["thread"]["id"]
        rpc(extra, "initialize", {"clientInfo": {"name": "ghostty"}}, 1)
        blocked = rpc(extra, "thread/resume", {"threadId": tid, "cwd": str(home)}, 2)
        err = (blocked.get("error") or {}).get("message") or ""
        assert "already has an active writer" in err, blocked
    finally:
        close_stdio(shared)
        extra.kill()


def _assign_const(tree: ast.AST, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Constant):
                        return node.value.value
    raise AssertionError(f"missing constant {name}")


def _fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def run_source_shared_client() -> None:
    src = (SERVER_LIB / "codex_app_server.py").read_text()
    tree = ast.parse(src)
    assert _assign_const(tree, "_SHARED_KEY") == "__shared__"
    client_fn = _fn(tree, "_client")
    dumped = ast.dump(client_fn)
    assert "_SHARED_KEY" in dumped or "__shared__" in dumped
    assert "_CLIENTS.get" in src or "_CLIENTS" in src
    recycle = _fn(tree, "recycle_clients")
    recycle_src = ast.get_source_segment(src, recycle) or ast.dump(recycle)
    assert "id(client)" in recycle_src or "id(" in recycle_src
    assert "stdin" in src and "shutdown" in src


def run_source_agy_print_timeout() -> None:
    src = (SERVER_LIB / "agy_runner.py").read_text()
    tree = ast.parse(src)
    assert _assign_const(tree, "AGY_PRINT_TIMEOUT") == "24h"
    build = _fn(tree, "build_cmd")
    dumped = ast.dump(build)
    assert "--print-timeout" in dumped or "print-timeout" in dumped
    assert "AGY_PRINT_TIMEOUT" in dumped
    classify = (SERVER_LIB / "error_classify.py").read_text()
    assert "timeout waiting for response" in classify


def main() -> int:
    cases = [
        "two_process_writer_lock",
        "one_process_two_threads",
        "leftover_close_stdin_then_resume",
        "second_process_still_conflicts",
        "source_shared_client",
        "source_agy_print_timeout",
    ]
    passed = []
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        run_two_process_writer_lock(home)
        passed.append("two_process_writer_lock")
        run_one_process_two_threads(home)
        passed.append("one_process_two_threads")
        run_leftover_close_stdin_then_resume(home)
        passed.append("leftover_close_stdin_then_resume")
        run_second_process_still_conflicts(home)
        passed.append("second_process_still_conflicts")
    run_source_shared_client()
    passed.append("source_shared_client")
    run_source_agy_print_timeout()
    passed.append("source_agy_print_timeout")
    missing = [name for name in cases if name not in passed]
    ok = not missing
    print(json.dumps({"ok": ok, "cases": passed, "missing": missing}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
