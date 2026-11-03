#!/usr/bin/env python3
"""A deterministic stand-in for the Claude Code CLI in print mode (stream-json).

The replay Host runs it as `claude` (a shim first on the Host's PATH) for the
agents created with the claude backend. It reads one stream-json user message
from stdin and answers with system/init, an assistant message and a result,
in the shapes backend/claude.py drains. Markers in the prompt:

- ``[qa-slow]``: wait up to 3 s before answering (a turn that is still running);
- ``[replay-linger]``: on SIGTERM, keep running for 1.5 s before exiting, like
  a CLI still flushing its transcript when a preempting turn starts (TP-01).

Every process appends its lifecycle to ``$CLARP_QA_PROVIDER_ROOT/claude-procs.jsonl``
(start, sigterm, exit; one line each, in the order they happen), so the
capture shows whether a new process started before the old one had exited.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid


def log(event: str, **fields) -> None:
    root = os.environ.get('CLARP_QA_PROVIDER_ROOT')
    if root:
        with (Path(root) / 'claude-procs.jsonl').open('a') as handle:
            handle.write(json.dumps({'event': event, 'proc_id': PROC, **fields}) + '\n')


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + '\n')
    sys.stdout.flush()


PROC = uuid.uuid4().hex[:12]


def main() -> int:
    args = sys.argv[1:]
    session = (args[args.index('--resume') + 1] if '--resume' in args
               else args[args.index('--session-id') + 1] if '--session-id' in args
               else str(uuid.uuid4()))
    line = sys.stdin.readline()
    try:
        message = json.loads(line)
        text = ''.join(part.get('text', '') for part in message['message']['content'])
    except (ValueError, KeyError, TypeError):
        text = ''
    linger = '[replay-linger]' in text
    stopped = threading.Event()

    def on_term(_signum, _frame):
        log('sigterm')
        stopped.set()
        if linger:
            time.sleep(1.5)
        log('exit', how='sigterm')
        os._exit(143)
    signal.signal(signal.SIGTERM, on_term)
    log('start', resume='--resume' in args)
    emit({'type': 'system', 'subtype': 'init', 'session_id': session, 'cwd': os.getcwd(),
          'model': 'replay-fake', 'tools': []})
    if '[qa-slow]' in text:
        stopped.wait(3)
    reply = 'Claude replay reply: ' + text
    emit({'type': 'assistant', 'session_id': session,
          'message': {'id': 'msg_' + PROC, 'role': 'assistant', 'model': 'replay-fake',
                      'content': [{'type': 'text', 'text': reply}],
                      'usage': {'input_tokens': 5, 'output_tokens': 7}}})
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': session,
          'result': reply, 'num_turns': 1, 'duration_ms': 1,
          'usage': {'input_tokens': 5, 'output_tokens': 7}})
    log('exit', how='done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
