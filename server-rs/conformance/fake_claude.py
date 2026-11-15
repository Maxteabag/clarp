#!/usr/bin/env python3
"""A deterministic stand-in for Claude Code in print mode, for the conformance run.

It is the replay harness's fake (tests/replay/fake_claude.py) with one thing
added: like the real CLI, it writes the turn into the session's transcript,
`<config>/projects/<cwd, with every other character a dash>/<session>.jsonl`.
The Python Host builds a Claude agent's conversation from that file, so without
it the reference has no conversation to compare. The Rust Host does not read it.

It reads one stream-json user message from stdin and answers with system/init,
an assistant message and a result. Markers in the prompt:

- ``[qa-slow]``: wait up to 3 s before answering (a turn that is still running);
- ``[replay-linger]``: on SIGTERM, keep running for 1.5 s before exiting.

Every process appends its lifecycle to ``$CLARP_QA_PROVIDER_ROOT/claude-procs.jsonl``.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
import uuid

PROC = uuid.uuid4().hex[:12]


def log(event: str, **fields) -> None:
    root = os.environ.get('CLARP_QA_PROVIDER_ROOT')
    if root:
        with (Path(root) / 'claude-procs.jsonl').open('a') as handle:
            handle.write(json.dumps({'event': event, 'proc_id': PROC, **fields}) + '\n')


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + '\n')
    sys.stdout.flush()


def transcript(session: str) -> Path | None:
    """Where the real CLI keeps this session's transcript."""
    config = os.environ.get('CLAUDE_CONFIG_DIR') or (
        str(Path(os.environ['HOME']) / '.claude') if os.environ.get('HOME') else '')
    if not config:
        return None
    project = re.sub(r'[^A-Za-z0-9]', '-', os.getcwd())
    path = Path(config) / 'projects' / project / f'{session}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record(path: Path | None, session: str, kind: str, parent: str | None, message: dict) -> str:
    """Appends one record the way the CLI writes it; returns the record's uuid."""
    ident = str(uuid.uuid4())
    if path is not None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        with path.open('a') as handle:
            handle.write(json.dumps({
                'type': kind, 'uuid': ident, 'parentUuid': parent, 'sessionId': session,
                'cwd': os.getcwd(), 'timestamp': now, 'isSidechain': False, 'userType': 'external',
                'message': message}) + '\n')
    return ident


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
    path = transcript(session)
    asked = record(path, session, 'user', None, {'role': 'user', 'content': text})
    if '[qa-slow]' in text:
        stopped.wait(3)
    reply = 'Claude replay reply: ' + text
    said = {'id': 'msg_' + PROC, 'role': 'assistant', 'model': 'replay-fake',
            'content': [{'type': 'text', 'text': reply}],
            'usage': {'input_tokens': 5, 'output_tokens': 7}}
    record(path, session, 'assistant', asked, {**said, 'type': 'message'})
    emit({'type': 'assistant', 'session_id': session, 'message': said})
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': session,
          'result': reply, 'num_turns': 1, 'duration_ms': 1,
          'usage': {'input_tokens': 5, 'output_tokens': 7}})
    log('exit', how='done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
