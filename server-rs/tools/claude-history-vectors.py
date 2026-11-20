#!/usr/bin/env python3
"""What a transcript of Claude Code says was said in its session, as the Python Host reads it for
the conversation of an agent (`server/lib/claude_transcript.py`, `parse_turns`): the turns its own
function reads from made-up transcripts, written to
`crates/clarp-backend/tests/fixtures/claude-history-vectors.json` with the lines it was given, so
that the Rust Host's reading is held to it line by line. And the model a session last ran on, as
the Python Host reads it off a transcript for an agent that takes the session up without naming a
model (`server/lib/session_models.py`, `_read_model`); and what each turn is in its conversation
(`commentary`, `final` or neither), as the Python Host marks the rows it reads from a transcript
(`server/lib/message_writes.py`, `_row_phases`).

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/claude-history-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'claude-history-vectors.json'


def user(content, at='2027-01-15T08:00:00.000Z', **more):
    message = {'role': 'user', 'content': content}
    if 'message_id' in more:
        message['id'] = more.pop('message_id')
    return {'type': 'user', 'uuid': more.pop('uuid', ''), 'timestamp': at, 'message': message, **more}


def agent(content, at='2027-01-15T08:00:05.000Z', **more):
    message = {'role': 'assistant', 'model': more.pop('model', 'claude-test'), 'content': content}
    if 'message_id' in more:
        message['id'] = more.pop('message_id')
    return {'type': 'assistant', 'uuid': more.pop('uuid', ''), 'timestamp': at, 'message': message, **more}


def text(said):
    return {'type': 'text', 'text': said}


def use(name, given, call='', **more):
    return {'type': 'tool_use', 'name': name, 'input': given, **({'id': call} if call else {}), **more}


def result(call, content, **more):
    return {'type': 'tool_result', 'tool_use_id': call, 'content': content, **more}


LONG = 'L' * 700

# The transcripts: a name, what it shows, and its lines (an object is written as JSON, a text as it is).
TRANSCRIPTS = [
    ('plain', 'what a user wrote and what the agent answered', [
        {'type': 'summary', 'summary': 'not a turn'},
        user('  Fix the build.  ', uuid='u1'),
        agent([text('Looking.'), text(''), text('  Done: one test was pinned.  ')], uuid='a1'),
        user([text('And the version?'), {'type': 'image', 'source': {}}, text('Bump it.')], uuid='u2', at='2027-01-15T08:01:00.000Z'),
        agent('Bumped to 2.4.1.', uuid='a2', at=''),
        {'type': 'system', 'content': 'no turn either'},
        user('', uuid='u3'),
        agent([], uuid='a3'),
        user({'no': 'content of a known kind'}, uuid='u4'),
        {'type': 'user', 'uuid': 'u5', 'timestamp': 'x'},
        {'type': 'assistant', 'uuid': 'a5', 'message': None},
    ]),
    ('noise', 'what the tool wrote into the conversation by itself is no turn of anybody', [
        user('<system-reminder>Remember the rules.</system-reminder>', uuid='n1'),
        user('  <task-notification><task-id>t1</task-id></task-notification>', uuid='n2'),
        user('<command-name>/compact</command-name>', uuid='n3'),
        user('<LOCAL-COMMAND-STDOUT>done</LOCAL-COMMAND-STDOUT>', uuid='n4'),
        user('<command-message>x</command-message>', uuid='n5'),
        user('<command-args>x</command-args>', uuid='n6'),
        user('<clarp-account-recovery>x</clarp-account-recovery>', uuid='n7'),
        user('<system-reminders are fine to talk about', uuid='n8'),
        user('I typed <system-reminder> in the middle', uuid='n9'),
        user('Continue from where you left off.', uuid='meta-1', isMeta=True),
        agent([text('No response requested.')], uuid='n10', parentUuid='meta-1'),
        agent([text('An answer to something else.')], uuid='n11', parentUuid='n9'),
        agent([text('API Error: The socket connection was closed unexpectedly')], uuid='n12', isApiErrorMessage=True),
        agent([text('Made up by the tool itself.')], uuid='n13', model='<synthetic>'),
        {'type': 'assistant', 'uuid': 'n14', 'timestamp': 't', 'message': {'role': 'assistant', 'isApiErrorMessage': True,
                                                                       'content': [text('an error of the provider')]}},
        'this line is no JSON',
        '',
        agent([text('The last word.')], uuid='n15'),
    ]),
    ('tools', 'the tools an agent used, each as the card a conversation shows, with what came back', [
        user('Do the work.', uuid='t0'),
        agent([text('Reading.'), use('Read', {'file_path': '/srv/app/src/main.rs', 'offset': '10', 'limit': 20}, 'c1')], uuid='t1'),
        user([result('c1', 'fn main() {}')], uuid='t2'),
        agent([use('Read', {'file_path': '/srv/app/a.txt', 'offset': [1, 2], 'limit': True}, 'c2'),
               use('Edit', {'file_path': '/srv/app/a.txt', 'old_string': LONG, 'new_string': 'short', 'replace_all': 1}, 'c3'),
               use('MultiEdit', {'file_path': '/srv/app/b.txt', 'edits': [{'old_string': 'o' * 250, 'new_string': 'n'}] * 12}, 'c4')], uuid='t3'),
        user([result('c3', [{'type': 'text', 'text': 'edited'}, {'type': 'text', 'text': ''}, 'and a word', {'content': 'more'}]),
              result('c2', 'r' * 400, is_error=True),
              {'type': 'tool_result', 'toolUseId': 'c4', 'content': None, 'isError': True}], uuid='t4'),
        agent([use('Write', {'file_path': '/srv/app/new.txt', 'content': LONG}, 'c5'),
               use('Bash', {'command': 'cargo test ' + 'x' * 500, 'description': 'Run the tests'}, 'c6'),
               use('Glob', {'pattern': '**/*.rs', 'path': '/srv/app'}, 'c7'),
               use('Grep', {'pattern': 'fn main', 'glob': '*.rs', 'output_mode': 'content', 'more': 1}, 'c8'),
               use('TodoWrite', {'todos': [{'content': 'one', 'status': 'pending', 'activeForm': 'x'}, {'content': 'two'}]}, 'c9'),
               use('mcp__files__search', {'query': 'q' * 300, 'limit': 5, 'deep': True, 'paths': ['a'], 'where': {'x': 1}, 'none': None}, 'c10'),
               use('WebFetch', {'url': 'https://example.org/page', 'prompt': 'Summarize'}),
               {'type': 'tool_use', 'id': 'c12'},
               use('Bash', None, 'c13')], uuid='t5'),
        # A result for a use nobody made settles nothing; one without an id settles the newest open one.
        user([result('c99', 'for nobody'), {'type': 'tool_result', 'content': 'for the newest that is open'}], uuid='t6'),
        user([result('c6', 'ok'), result('c6', 'said a second time'), text('  and the user says so  ')], uuid='t7'),
        agent([text('All done.')], uuid='t8'),
    ]),
]

# The id the provider gave an agent's message (R107): a real transcript names it on every line of the message.
TRANSCRIPTS.append(
    ('message-ids', "the id the provider gave an agent's message: on each line of it, where it is a text that is not empty", [
        user('Look at the code.', uuid='i0', message_id='msg_user'),
        agent([text('I will read it.')], uuid='i1', message_id='msg_01'),
        agent([use('Read', {'file_path': '/srv/app/main.rs'}, 'r1')], uuid='i2', message_id='msg_01'),
        user([result('r1', 'fn main() {}')], uuid='i3'),
        agent([text('An empty id.')], uuid='i4', message_id=''),
        agent([text('A number for an id.')], uuid='i5', message_id=7),
        agent([text('No id at all.')], uuid='i6'),
        agent([text('It is small.')], uuid='i7', message_id='msg_02'),
    ]))

# The model a session last ran on: a name, what it shows, and its lines.
MODELS = [
    ('the-newest', 'the model the newest answer names', [
        user('one'),
        agent([text('first')], model='claude-old'),
        user('two'),
        agent([text('second')], model='claude-new'),
        user('three'),
    ]),
    ('not-made-up', 'an answer the tool made up names no model, nor does one that names none', [
        agent([text('an answer')], model='claude-real'),
        agent([text('made up')], model='<synthetic>'),
        {'type': 'assistant', 'message': {'role': 'assistant', 'model': '', 'content': []}},
        {'type': 'assistant', 'message': {'role': 'assistant', 'model': 7, 'content': []}},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': []}},
        {'type': 'assistant', 'message': 'no object'},
        {'type': 'assistant', 'payload': 'no object', 'message': {'role': 'assistant', 'model': 'beside-a-payload-that-is-none'}},
        {'type': 'user', 'message': {'role': 'user', 'model': 'a-users', 'content': 'x'}},
        {'type': 'system', 'message': {'model': 'the-systems'}},
        [1, 2, 3],
        'this line is no JSON',
        '',
    ]),
    ('of-a-rollout', 'the context of a turn names the model, as a rollout of Codex writes it', [
        agent([text('an answer')], model='claude-before'),
        {'type': 'turn_context', 'payload': {'model': 'of-the-turn', 'cwd': '/srv/work'}},
        {'type': 'turn_context', 'payload': {'model': ''}},
        {'type': 'turn_context', 'payload': {'cwd': '/srv/work'}},
        {'type': 'turn_context', 'message': {'model': 'not-where-a-context-says-it'}},
        {'type': 'response_item', 'payload': {'model': 'no-context-of-a-turn'}},
    ]),
    ('none', 'a transcript in which no answer names a model', [
        user('one'),
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [text('x')]}},
    ]),
    ('empty', 'a transcript of nothing', []),
]


def turn(role, said='', **more):
    return {'role': role, 'text': said, 'tools': more.pop('tools', []), **more}


CARD = [{'name': 'Read'}]
# What each turn is in its conversation: a name, what it shows, and turns as a transcript is read.
PHASED = [
    ('said-then-work', 'what is said before tool work that follows it is commentary', [
        turn('user', 'do it'), turn('assistant', 'looking', tools=CARD), turn('assistant', '', tools=CARD),
        turn('assistant', 'changing', tools=CARD), turn('assistant', '', tools=CARD), turn('assistant', 'done')]),
    ('its-own-tools', 'the tools a turn itself used do not follow what it said', [
        turn('user', 'read it'), turn('assistant', 'reading', tools=CARD), turn('assistant', 'it says little')]),
    ('by-its-kind', 'a turn that says what it is, is that', [
        turn('user', 'go'), turn('assistant', 'one', kind='commentary'), turn('assistant', 'two', kind='final_answer'),
        turn('assistant', 'three', kind='final'), turn('assistant', 'four', kind='another', tools=CARD),
        turn('assistant', 'five', kind=None), turn('assistant', 'six', kind=7)]),
    ('cells', 'a cell of its own is tool work too, and a turn of blanks said nothing', [
        turn('user', 'go'), turn('assistant', 'aloud'), turn('assistant', '', display_cells=[{'kind': 'plan'}]),
        turn('assistant', '   '), turn('assistant', 'end')]),
    ('two-askings', 'tool work after the user spoke again follows nothing said before', [
        turn('user', 'one'), turn('assistant', 'first'), turn('user', 'two'), turn('assistant', '', tools=CARD),
        turn('assistant', 'second', tools=CARD)]),
    ('of-nobody', 'a turn of neither is passed over, with its tools', [
        turn('system', 'a note', tools=CARD), turn('assistant', 'before'), {'role': 'tool', 'tools': CARD},
        turn('assistant', 'after'), {'text': 'no role at all'}, turn('assistant', None), {'role': 'assistant'}]),
    ('nothing', 'no turns', []),
]


@tool_scratch.case('clarp-claude-history-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: the turns the Python Host reads from the transcripts above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import claude_transcript, message_writes, session_models

    def model_of(path):
        stat = path.stat()
        return session_models._read_model(str(path), stat.st_mtime_ns, stat.st_size)

    out = []
    for name, what, lines in TRANSCRIPTS:
        written = [line if isinstance(line, str) else json.dumps(line) for line in lines]
        path = root / f'{name}.jsonl'
        path.write_text('\n'.join(written) + '\n')
        turns = claude_transcript.parse_turns(path)
        out.append({'name': name, 'what': what, 'lines': written, 'turns': turns,
                    'model': model_of(path), 'phases': message_writes._row_phases(turns)})
    models = []
    for name, what, lines in MODELS:
        written = [line if isinstance(line, str) else json.dumps(line) for line in lines]
        path = root / f'model-{name}.jsonl'
        path.write_text(''.join(line + '\n' for line in written))
        models.append({'name': name, 'what': what, 'lines': written, 'model': model_of(path)})
    phased = [{'name': name, 'what': what, 'turns': turns, 'phases': message_writes._row_phases(turns)}
              for name, what, turns in PHASED]
    return {'transcripts': out, 'models': models, 'phased': phased}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', ' "transcripts": [']
    rows = said['transcripts']
    for number, row in enumerate(rows):
        out.append('  {"name": ' + json.dumps(row['name']) + ', "what": ' + json.dumps(row['what'])
                   + ', "model": ' + json.dumps(row['model']) + ', "phases": ' + json.dumps(row['phases']) + ',')
        out.append('   "lines": ' + json.dumps(row['lines'], ensure_ascii=True) + ',')
        out.append('   "turns": [')
        out.extend('    ' + json.dumps(turn, ensure_ascii=True) + (',' if n < len(row['turns']) - 1 else '')
                   for n, turn in enumerate(row['turns']))
        out.append('   ]}' + (',' if number < len(rows) - 1 else ''))
    out.append(' ],')
    out.append(' "models": [')
    models = said['models']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(models) - 1 else '')
               for n, row in enumerate(models))
    out.append(' ],')
    out.append(' "phased": [')
    phased = said['phased']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(phased) - 1 else '')
               for n, row in enumerate(phased))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    print(f'{OUT.relative_to(REPO)}: ' + '; '.join(
        f'{row["name"]}: {len(row["lines"])} lines, {len(row["turns"])} turns, '
        f'{sum(len(turn["tools"]) for turn in row["turns"])} tools, model {row["model"]!r}' for row in rows))
    print('models: ' + '; '.join(f'{row["name"]}: {row["model"]!r}' for row in models))
    print('phases: ' + '; '.join(f'{row["name"]}: {row["phases"]}' for row in [*rows, *phased]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
