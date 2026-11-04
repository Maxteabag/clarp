#!/usr/bin/env python3
"""The replay corpus's provider: tests/qa/fake_codex.py behind a thin proxy.

Everything passes through unchanged. A turn whose input carries a marker gets
extra items injected right after the fake's ``turn/started`` for that turn,
in the app-server shapes the Codex runner reads (codex_app_server,
live_codex), so tool use reaches the live items, the transcript and the
database without a real provider:

- ``[replay-tool]``: a reasoning item with a summary, then a command that
  streams two output lines and completes with exit code 0;
- ``[replay-tool-fail]``: the same, but the command exits 2;
- ``[replay-hold-start]``: that ``turn/start`` request is held back from the
  fake until the scenario writes ``$CLARP_QA_PROVIDER_ROOT/release-start``, so
  the Host's start is in flight with no turn yet (the Codex pool's
  stop-during-start path). Other traffic keeps flowing while it is held.

Every app-server process (``codex app-server``) appends ``start``, and a held start ``held`` and
``released``, to ``$CLARP_QA_PROVIDER_ROOT/codex-procs.jsonl``, so the capture
shows how many app-servers the Host ran and what happened to a held start.

Item ids derive from the turn id, so they are stable within a run and
normalize like any generated id.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

FAKE = Path(__file__).resolve().parents[1] / 'qa' / 'fake_codex.py'
MARKERS = ('[replay-tool-fail]', '[replay-tool]')
HOLD = '[replay-hold-start]'
PROC = uuid.uuid4().hex[:12]


def log(event: str) -> None:
    root = os.environ.get('CLARP_QA_PROVIDER_ROOT')
    if root:
        with (Path(root) / 'codex-procs.jsonl').open('a') as handle:
            handle.write(json.dumps({'event': event, 'proc_id': PROC}) + '\n')


_in = threading.Lock()


def to_child(child, line: str) -> None:
    with _in:
        child.stdin.write(line)
        child.stdin.flush()


def release_later(child, line: str) -> None:
    """Pass a held turn/start request on once the scenario releases it."""
    root = Path(os.environ.get('CLARP_QA_PROVIDER_ROOT') or '.')
    while not (root / 'release-start').exists():
        time.sleep(0.02)
    log('released')
    to_child(child, line)

_out = threading.Lock()


def emit(event: dict) -> None:
    with _out:
        sys.stdout.write(json.dumps(event) + '\n')
        sys.stdout.flush()


def tool_items(thread_id: str, turn_id: str, marker: str) -> list[dict]:
    base = {'threadId': thread_id, 'turnId': turn_id}
    reasoning = {'id': f'{turn_id}-r1', 'type': 'reasoning',
                 'summary': [{'type': 'summary_text', 'text': 'Checking the synthetic workspace'}]}
    command = {'id': f'{turn_id}-c1', 'type': 'commandExecution', 'command': 'ls synthetic-dir',
               'cwd': '.', 'status': 'inProgress'}
    failed = marker == '[replay-tool-fail]'
    output = 'alpha.txt\nbeta.txt\n' if not failed else 'ls: synthetic-dir: not found\n'
    done = {**command, 'status': 'failed' if failed else 'completed', 'exitCode': 2 if failed else 0,
            'aggregatedOutput': output}
    return [
        {'method': 'item/started', 'params': {**base, 'item': {**reasoning, 'summary': []}}},
        {'method': 'item/completed', 'params': {**base, 'item': reasoning}},
        {'method': 'item/started', 'params': {**base, 'item': command}},
        {'method': 'item/commandExecution/outputDelta', 'params': {**base, 'itemId': command['id'], 'delta': output}},
        {'method': 'item/completed', 'params': {**base, 'item': done}},
    ]


def main() -> int:
    child = subprocess.Popen([sys.executable, str(FAKE), *sys.argv[1:]], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, bufsize=1, env=os.environ)
    marked_requests: dict = {}   # request id -> marker
    marked_turns: dict = {}      # turn id -> marker
    lock = threading.Lock()
    if 'app-server' in sys.argv[1:]:
        log('start')

    def forward_stdin():
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except ValueError:
                request = {}
            if request.get('method') == 'turn/start':
                text = ''.join(i.get('text', '') for i in (request.get('params') or {}).get('input', []))
                marker = next((m for m in MARKERS if m in text), None)
                if marker and 'id' in request:
                    with lock:
                        marked_requests[request['id']] = marker
                if HOLD in text:
                    log('held')
                    threading.Thread(target=release_later, args=(child, line), daemon=True).start()
                    continue
            to_child(child, line)
        with _in:
            child.stdin.close()

    threading.Thread(target=forward_stdin, daemon=True).start()
    for line in child.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict) and 'id' in event and isinstance(event.get('result'), dict):
            with lock:
                marker = marked_requests.pop(event['id'], None)
            turn = (event['result'].get('turn') or {}).get('id')
            if marker and turn:
                with lock:
                    marked_turns[turn] = marker
        with _out:
            sys.stdout.write(line)
            sys.stdout.flush()
        if isinstance(event, dict) and event.get('method') == 'turn/started':
            params = event.get('params') or {}
            turn = (params.get('turn') or {}).get('id') or params.get('turnId')
            with lock:
                marker = marked_turns.pop(turn, None)
            if marker:
                for item in tool_items(str(params.get('threadId') or ''), str(turn), marker):
                    emit(item)
    return child.wait()


if __name__ == '__main__':
    sys.exit(main())
