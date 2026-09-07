#!/usr/bin/env python3
"""Deterministic provider process; the real dispatcher/runner own everything else."""
import json
import sys
import time
import threading
import uuid
import os
from pathlib import Path
from datetime import datetime, timezone


def emit(event):
    print(json.dumps(event), flush=True)


def app_server():
    thread_id = str(uuid.uuid4())
    cancelled = threading.Event()
    def persist(kind, payload):
        root = Path(os.environ['CLARP_QA_PROVIDER_ROOT']) / 'sessions'
        root.mkdir(parents=True, exist_ok=True)
        with (root / f'rollout-{thread_id}.jsonl').open('a') as stream:
            stream.write(json.dumps({'type': kind,
                'timestamp': datetime.now(timezone.utc).isoformat(), 'payload': payload}) + '\n')
    def complete(params, turn_id, stop):
        time.sleep(0.05)  # let turn/start's response establish ownership
        base = {'threadId': thread_id, 'turnId': turn_id}
        emit({'method': 'turn/started', 'params': {**base, 'turn': {'id': turn_id}}})
        text = 'QA reply: ' + ''.join(item.get('text', '') for item in params.get('input', []))
        if '[qa-slow]' in text and stop.wait(3):
            return
        for part in (text[:8], text[:16], text):
            if stop.wait(0.15):
                return
            emit({'method': 'item/updated', 'params': {**base, 'item': {
                'id': turn_id, 'type': 'agentMessage', 'text': part}}})
        emit({'method': 'item/completed', 'params': {**base, 'item': {
            'id': turn_id, 'type': 'agentMessage', 'text': text}}})
        persist('event_msg', {'type': 'agent_message', 'message': text, 'phase': 'final_answer'})
        emit({'method': 'turn/completed', 'params': {**base, 'turn': {
            'id': turn_id, 'status': 'completed'}}})
    for line in sys.stdin:
        request = json.loads(line)
        method, params = request.get('method'), request.get('params', {})
        if 'id' not in request:
            continue
        result = {}
        if method in ('thread/start', 'thread/resume'):
            thread_id = params.get('threadId') or thread_id
            persist('session_meta', {'id': thread_id, 'cwd': params.get('cwd', '')})
            result = {'thread': {'id': thread_id}}
        elif method == 'turn/start':
            cancelled = threading.Event()
            turn_id = str(uuid.uuid4())
            result = {'turn': {'id': turn_id}}
            persist('event_msg', {'type': 'user_message',
                'message': ''.join(item.get('text', '') for item in params.get('input', []))})
        elif method == 'turn/interrupt':
            cancelled.set()
        emit({'id': request['id'], 'result': result})
        if method == 'turn/start':
            threading.Thread(target=complete, args=(params, turn_id, cancelled), daemon=True).start()


if __name__ == '__main__':
    if 'app-server' in sys.argv:
        app_server()
        raise SystemExit(0)
    prompt = sys.argv[-1]
    args = sys.argv[1:]
    session = args[args.index('resume') + 1] if 'resume' in args else str(uuid.uuid4())
    emit({'type': 'thread.started', 'thread_id': session})
    emit({'type': 'turn.started'})
    reply = 'QA reply: ' + prompt
    mid = str(uuid.uuid4())
    for part in (reply[:8], reply[:16], reply):
        emit({'type': 'item.updated', 'item': {'id': mid, 'type': 'agent_message', 'text': part}})
        time.sleep(0.15)
    emit({'type': 'item.completed', 'item': {'id': mid, 'type': 'agent_message', 'text': reply}})
    emit({'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 10}})
