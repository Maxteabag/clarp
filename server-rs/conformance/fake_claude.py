#!/usr/bin/env python3
"""A deterministic stand-in for Claude Code in print mode, for the conformance run.

It is the replay harness's fake (tests/replay/fake_claude.py) with one thing
added: like the real CLI, it writes the turn into the session's transcript,
`<config>/projects/<cwd, with every other character a dash>/<session>.jsonl`.
The Python Host builds a Claude agent's conversation from that file, so without
it the reference has no conversation to compare. The Rust Host does not read it.

It reads one stream-json user message from stdin and answers with system/init,
an assistant message and a result. The prompt is the user's words: a text block that
begins with ``<system-reminder>`` is what a Host says with them (the Rust Host says after
the user's words what the Python Host says by a hook this stand-in does not run), and is
no part of it. Markers in the prompt:

- ``[qa-slow]``: wait up to 3 s before answering (a turn that is still running);
- ``[replay-linger]``: on SIGTERM, keep running for 1.5 s before exiting;
- ``[qa-usage-limit]``: the account is out. What the real CLI did on 15 Nov 2026: an event that
  says the limit rejected the request, its own sentence as the answer (an assistant message of
  the model ``<synthetic>``, marked as an API error in the transcript), a result that is an
  error, and exit code 1;
- ``[qa-crash]``: the tool dies: a line on stderr, no result, exit code 1;
- ``[qa-says-first]``: the agent says one thing before whatever else the prompt asks for: a
  turn that had begun to answer when it failed;
- ``[qa-drops:N]``: the connection drops in the first N processes that are given this prompt
  (a line on stderr in the CLI's words for it, no result, exit code 1); the next one answers.
  How often a prompt was given is counted in ``$CLARP_QA_PROVIDER_ROOT``.
- ``[qa-tools]``: the agent uses its tools before it answers, the way the real CLI says it: an
  assistant message with a ``tool_use`` in it, then a user message with the ``tool_result``.
  A command, two tools at once (a file read and a search), an edit, a command that fails, and
  a tool no Host knows by name. Nothing is run: what a tool "returned" is written here.
- ``[qa-who]``: the answer ends with who the Host said the agent is: what this process was
  started with as ``--append-system-prompt``, or ``nobody`` when it was started without.
- ``[qa-stream]``: the turn is printed piece by piece, as the real CLI prints one when it is
  asked for partial messages: a message begins, each of its blocks begins, grows and ends
  (what the agent thinks, what it says, a tool call whose input arrives in parts), and each
  block is said whole once before it ends. It thinks, says what it will do, reads a file,
  edits it, and answers.

Every process appends its lifecycle to ``$CLARP_QA_PROVIDER_ROOT/claude-procs.jsonl``.
"""
from __future__ import annotations

import datetime
import hashlib
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


def record(path: Path | None, session: str, kind: str, parent: str | None, message: dict, **more) -> str:
    """Appends one record the way the CLI writes it; returns the record's uuid."""
    ident = str(uuid.uuid4())
    if path is not None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        with path.open('a') as handle:
            handle.write(json.dumps({
                'type': kind, 'uuid': ident, 'parentUuid': parent, 'sessionId': session,
                'cwd': os.getcwd(), 'timestamp': now, 'isSidechain': False, 'userType': 'external',
                'message': message, **more}) + '\n')
    return ident


# What the agent does with its tools when a prompt says `[qa-tools]`: each entry is one
# assistant message (what it says, if anything, and the tools it calls) and what each tool
# returned (`None` text with `failed` for a tool that broke).
TOOL_WORK = [
    ('Claude replay looks first.', [
        ('Bash', {'command': 'echo synthetic', 'description': 'Print a synthetic line'}, 'synthetic\n', False),
    ]),
    ('', [
        ('Read', {'file_path': '/synthetic/notes.md', 'limit': 20}, '     1\t# Synthetic notes\n     2\tA line.\n', False),
        ('Grep', {'pattern': 'synthetic', 'path': '/synthetic', 'output_mode': 'files_with_matches'},
         'Found 1 file\n/synthetic/notes.md', False),
    ]),
    ('Claude replay changes a line.', [
        ('Edit', {'file_path': '/synthetic/notes.md', 'old_string': 'A line.', 'new_string': 'Another line.'},
         'The file /synthetic/notes.md has been updated successfully.', False),
    ]),
    ('', [
        ('Bash', {'command': 'false', 'description': 'A command that fails'}, 'Exit code 1\nsynthetic failure', True),
    ]),
    ('', [
        ('mcp__synthetic__lookup', {'query': 'synthetic', 'limit': 3, 'filters': {'kind': 'note'}},
         [{'type': 'text', 'text': 'one synthetic result'}], False),
    ]),
]


def tool_work(path: Path | None, session: str, text: str, parent: str | None) -> str | None:
    """The agent uses its tools. Every use has an id made of the prompt and its place, so that
    it is the same wherever the same prompt is given, and another for every other prompt."""
    place = 0
    for number, (said, calls) in enumerate(TOOL_WORK):
        content = [{'type': 'text', 'text': said}] if said else []
        uses = []
        for name, given, _returned, _failed in calls:
            ident = 'toolu_' + hashlib.sha256(f'{text}\0{place}'.encode()).hexdigest()[:20]
            place += 1
            uses.append(ident)
            content.append({'type': 'tool_use', 'id': ident, 'name': name, 'input': given})
        message = {'id': 'msg_' + hashlib.sha256(f'{text}\0m{number}'.encode()).hexdigest()[:12], 'role': 'assistant',
                   'model': 'replay-fake', 'content': content, 'stop_reason': 'tool_use',
                   'usage': {'input_tokens': 5, 'output_tokens': 3}}
        parent = record(path, session, 'assistant', parent, {**message, 'type': 'message'})
        emit({'type': 'assistant', 'session_id': session, 'message': message})
        results = [{'type': 'tool_result', 'tool_use_id': ident, 'content': returned, 'is_error': failed}
                   for ident, (_name, _given, returned, failed) in zip(uses, calls)]
        answer = {'role': 'user', 'content': results}
        parent = record(path, session, 'user', parent, answer)
        emit({'type': 'user', 'session_id': session, 'message': answer})
    return parent


# What the agent does when a prompt says `[qa-stream]`: each entry is one message (its
# blocks, in order: what it thinks, what it says, the tools it calls) and what each tool
# returned.
STREAM_WORK = [
    ([{'type': 'thinking', 'thinking': '**Reading the notes**\nThey are short.', 'signature': 'synthetic'},
      {'type': 'text', 'text': 'Claude replay looks at the notes.'},
      ('Read', {'file_path': '/synthetic/notes.md', 'limit': 20})],
     ['     1\t# Synthetic notes\n     2\tA line.\n']),
    ([('Edit', {'file_path': '/synthetic/notes.md', 'old_string': 'A line.',
                'new_string': 'Another line.\nAnd one more.'})],
     ['The file /synthetic/notes.md has been updated successfully.']),
]


def parts(text: str, count: int = 3) -> list[str]:
    """A text in up to `count` pieces of about the same length, none empty."""
    size = max(1, -(-len(text) // count))
    return [text[at:at + size] for at in range(0, len(text), size)]


def streamed(session: str, message: dict) -> None:
    """Prints a message the way the CLI does when it is asked for partial messages: its
    beginning, each block as it begins, grows and ends, with the block whole in a message of
    its own before it ends, and the message's end. (The order is the CLI's own, read off a
    recorded turn of Claude Code 2.1.321.)"""
    def piece(event: dict) -> None:
        emit({'type': 'stream_event', 'event': event, 'session_id': session, 'parent_tool_use_id': None})

    piece({'type': 'message_start', 'message': {**message, 'content': [], 'stop_reason': None}})
    for index, block in enumerate(message['content']):
        if block['type'] == 'thinking':
            begun, grows = {**block, 'thinking': ''}, [{'type': 'thinking_delta', 'thinking': part}
                                                        for part in parts(block['thinking'])]
        elif block['type'] == 'text':
            begun, grows = {**block, 'text': ''}, [{'type': 'text_delta', 'text': part} for part in parts(block['text'])]
        else:
            begun, grows = {**block, 'input': {}}, [{'type': 'input_json_delta', 'partial_json': part}
                                                     for part in parts(json.dumps(block['input']))]
        piece({'type': 'content_block_start', 'index': index, 'content_block': begun})
        for grown in grows:
            piece({'type': 'content_block_delta', 'index': index, 'delta': grown})
        emit({'type': 'assistant', 'session_id': session, 'message': {**message, 'content': [block]}})
        piece({'type': 'content_block_stop', 'index': index})
    piece({'type': 'message_delta', 'delta': {'stop_reason': message.get('stop_reason') or 'end_turn'},
           'usage': message.get('usage')})
    piece({'type': 'message_stop'})


def stream_work(path: Path | None, session: str, text: str, parent: str | None) -> str | None:
    """The agent works where everyone can watch: every message of it is printed piece by
    piece. Ids are made of the prompt and the place, as in `tool_work`."""
    place = 0
    for number, (blocks, returns) in enumerate(STREAM_WORK):
        content, uses = [], []
        for block in blocks:
            if isinstance(block, dict):
                content.append(block)
                continue
            name, given = block
            ident = 'toolu_' + hashlib.sha256(f'{text}\0s{place}'.encode()).hexdigest()[:20]
            place += 1
            uses.append(ident)
            content.append({'type': 'tool_use', 'id': ident, 'name': name, 'input': given})
        message = {'id': 'msg_' + hashlib.sha256(f'{text}\0sm{number}'.encode()).hexdigest()[:12], 'role': 'assistant',
                   'model': 'replay-fake', 'content': content, 'stop_reason': 'tool_use',
                   'usage': {'input_tokens': 5, 'output_tokens': 3}}
        parent = record(path, session, 'assistant', parent, {**message, 'type': 'message'})
        streamed(session, message)
        answer = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': ident, 'content': returned,
                                               'is_error': False} for ident, returned in zip(uses, returns)]}
        parent = record(path, session, 'user', parent, answer)
        emit({'type': 'user', 'session_id': session, 'message': answer})
    return parent


def given_before(text: str) -> int:
    """How many processes were given this prompt before this one. Counted in a file of the
    provider's directory; without one, never."""
    root = os.environ.get('CLARP_QA_PROVIDER_ROOT')
    if not root:
        return 0
    counter = Path(root) / ('given-' + hashlib.sha256(text.encode()).hexdigest()[:16])
    before = int(counter.read_text() or 0) if counter.exists() else 0
    counter.write_text(str(before + 1))
    return before


# The CLI's own sentence when the account's window is spent, as it said it on 15 Nov 2026.
LIMIT = "You've hit your 5-hour limit \u00b7 resets 3pm (Europe/Oslo)"


def out_of_usage(path: Path | None, session: str, asked: str) -> int:
    """The account is out: the limit's event, the CLI's sentence as the answer, an error result."""
    emit({'type': 'rate_limit_event', 'session_id': session,
          'rate_limit_info': {'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': 4102444800,
                              'overageStatus': 'rejected', 'isUsingOverage': False}})
    said = {'id': str(uuid.uuid4()), 'type': 'message', 'role': 'assistant', 'model': '<synthetic>',
            'stop_reason': 'stop_sequence', 'content': [{'type': 'text', 'text': LIMIT}],
            'usage': {'input_tokens': 0, 'output_tokens': 0}}
    record(path, session, 'assistant', asked, said, isApiErrorMessage=True, error='rate_limit')
    emit({'type': 'assistant', 'session_id': session, 'message': said, 'error': 'rate_limit'})
    emit({'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': session,
          'result': LIMIT, 'num_turns': 1, 'duration_ms': 1,
          'usage': {'input_tokens': 0, 'output_tokens': 0}})
    log('exit', how='usage-limit')
    return 1


def main() -> int:
    args = sys.argv[1:]
    session = (args[args.index('--resume') + 1] if '--resume' in args
               else args[args.index('--session-id') + 1] if '--session-id' in args
               else str(uuid.uuid4()))
    line = sys.stdin.readline()
    try:
        message = json.loads(line)
        parts = [str(part.get('text', '')) for part in message['message']['content']]
        text = ''.join(part for part in parts if not part.startswith('<system-reminder>'))
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
    drops = re.search(r'\[qa-drops:(\d+)\]', text)
    if drops and given_before(text) < int(drops.group(1)):
        sys.stderr.write('API Error: socket connection was closed unexpectedly\n')
        sys.stderr.flush()
        log('exit', how='dropped')
        return 1
    if '[qa-says-first]' in text:
        first = {'id': 'msg_' + uuid.uuid4().hex[:12], 'role': 'assistant', 'model': 'replay-fake',
                 'content': [{'type': 'text', 'text': 'Claude replay first words: ' + text}],
                 'usage': {'input_tokens': 5, 'output_tokens': 3}}
        asked = record(path, session, 'assistant', asked, {**first, 'type': 'message'})
        emit({'type': 'assistant', 'session_id': session, 'message': first})
    if '[qa-tools]' in text:
        asked = tool_work(path, session, text, asked)
    if '[qa-stream]' in text:
        asked = stream_work(path, session, text, asked)
    if '[qa-usage-limit]' in text:
        return out_of_usage(path, session, asked)
    if '[qa-crash]' in text:
        sys.stderr.write('Error: the synthetic tool broke before it could answer\n')
        sys.stderr.flush()
        log('exit', how='crash')
        return 1
    reply = 'Claude replay reply: ' + text
    if '[qa-who]' in text:
        told = args[args.index('--append-system-prompt') + 1] if '--append-system-prompt' in args else 'nobody'
        reply += ' | I was told who I am: ' + told
    said = {'id': 'msg_' + PROC, 'role': 'assistant', 'model': 'replay-fake',
            'content': [{'type': 'text', 'text': reply}],
            'usage': {'input_tokens': 5, 'output_tokens': 7}}
    if '[qa-stream]' in text:
        # A message has one id however often it is given this prompt: a streamed turn's
        # items are named by it.
        said['id'] = 'msg_' + hashlib.sha256(f'{text}\0said'.encode()).hexdigest()[:12]
    record(path, session, 'assistant', asked, {**said, 'type': 'message'})
    if '[qa-stream]' in text:
        streamed(session, said)
    else:
        emit({'type': 'assistant', 'session_id': session, 'message': said})
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': session,
          'result': reply, 'num_turns': 1, 'duration_ms': 1,
          'usage': {'input_tokens': 5, 'output_tokens': 7}})
    log('exit', how='done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
