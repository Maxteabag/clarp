#!/usr/bin/env python3
"""What a rollout of Codex says was said and done in its session, as the Python Host reads it for
the conversation of an agent (`server/lib/codex_transcript.py`, `parse_turns`, with the cells of
`server/lib/codex_tool_cells.py`): the turns its own function reads from made-up rollouts, written
to `crates/clarp-backend/tests/fixtures/codex-history-vectors.json` with the lines it was given,
so that the Rust Host's reading is held to it line by line. The records are of every kind that
function reads: what `codex exec --json` prints (`item.started`, `item.completed`), what Codex's
app server writes (`item_completed` with the item's own names), and the older records of a call
and what came back (`function_call`, `custom_tool_call`, their outputs, `patch_apply_end`, the
events of agents Codex starts itself).

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/codex-history-vectors.py
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
OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'codex-history-vectors.json'

AT = '2027-02-03T10:00:00.000Z'
LATER = '2027-02-03T10:00:09.000Z'
RESPONSE = {'message', 'function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output',
            'reasoning'}


def rec(kind, /, at=AT, **payload):
    """A record as a rollout holds it: an envelope around what happened."""
    return {'timestamp': at, 'type': 'response_item' if kind in RESPONSE else 'event_msg',
            'payload': {'type': kind, **payload}}


def said(role, *texts, at=AT, part='', **more):
    """A message of the conversation as the model's API has it."""
    kind = part or ('input_text' if role == 'user' else 'output_text')
    return rec('message', at, role=role, content=[{'type': kind, 'text': text} for text in texts], **more)


def printed(step, **item):
    """What `codex exec --json` prints of an item."""
    return {'type': f'item.{step}', 'item': item}


def written(step, turn='', at=AT, **item):
    """What Codex's app server writes of an item."""
    return rec(f'item_{step}', at, item=item, **({'turn_id': turn} if turn else {}))


def call(name, call_id='', kind='function_call', **more):
    return rec(kind, **({'call_id': call_id} if call_id else {}), name=name, **more)


def back(call_id, output, kind='function_call_output', **more):
    return rec(kind, call_id=call_id, output=output, **more)


def arguments(**given):
    return json.dumps(given)


MANY = '\n'.join(f'line {n}' for n in range(1, 13))
WIDE = 'w' * 300
PATCH = '''*** Begin Patch
*** Update File: src/a.rs
@@ fn main
-    let a = 1;
+    let a = 2;
     let b = 3;
*** Add File: notes/new.md
+hello
*** Delete File: old.txt
*** End Patch'''
TWO_EDITS = '''*** Begin Patch
*** Update File: src/a.rs
@@
-one
+uno
@@
 kept
-two
+dos
*** End Patch'''
DIFF = '''--- a/src/a.rs
+++ b/src/a.rs
@@ -1,3 +1,3 @@
 fn main() {
-    old();
+    new();
 }
\\ No newline at end of file'''

# The rollouts: a name, what it shows, and its lines (an object is written as JSON, a text as it is).
ROLLOUTS = [
    ('messages', 'what a user wrote and what the agent answered, in the model\'s own records', [
        {'timestamp': AT, 'type': 'session_meta', 'payload': {'id': 'made-up', 'cwd': '/srv/work'}},
        said('user', '# AGENTS.md instructions for /srv/work\n\nbe kind'),
        said('user', '# AGENTS.md instructions\nnewer heading'),
        said('user', '<environment_context>\n  <cwd>/srv/work</cwd>\n</environment_context>'),
        said('user', '<recommended_plugins>none</recommended_plugins>'),
        said('user', 'What does "<environment_context>" mean?'),
        said('assistant', 'It is what I am told of the machine.', ' Twice.', phase='commentary'),
        said('assistant', '   '),
        said('developer', 'never shown'),
        said('system', 'never shown either'),
        said('user', 'A picture', part='input_image'),
        said('user', '  Second question  ', at=LATER),
        said('assistant', 'Answer.', phase='final_answer', at=LATER),
        rec('message', role='assistant', content='not a list'),
        rec('message', role='assistant', content=['not an object', {'type': 'text', 'text': 'Plain part.'},
                                                    {'type': 'output_text', 'text': 7}, {'type': 'output_text', 'text': ''}]),
    ]),
    ('events', 'the same conversation as events, the two forms of one message told once', [
        rec('user_message', message='› › Fix the build.  '),
        said('user', 'Fix the build.'),
        rec('agent_message', message='  Looking.  ', phase='commentary'),
        said('assistant', 'Looking.'),
        said('assistant', 'Done.<oai-mem-citation>{"a": 1}</oai-mem-citation>', phase='final_answer'),
        rec('agent_message', message='Done.'),
        rec('agent_message', message='Done.<oai-mem-citation>{"a": 1}</oai-mem-citation> And more.'),
        rec('agent_message', message='Later.'),
        said('assistant', 'Later.<OAI-MEM-CITATION id="1">x\ny</OAI-MEM-CITATION>', phase='final_answer'),
        said('assistant', 'A block that is no metadata.<clarp-meta>kept</clarp-meta>'),
        rec('agent_message', message='A block that is no metadata.'),
        rec('agent_message', message='Open.'),
        said('assistant', 'Open.<environment_context>never closed', phase='commentary'),
        said('assistant', 'Open.', phase='final_answer'),
        rec('user_message', message='[voice-mode] speak plainly\n\n--- user message ---\nWhat now?'),
        rec('user_message', message='"[voice-mode] speak plainly\n\n--- user message ---\nQuoted?"'),
        rec('user_message', message='[voice-mode] no end of it'),
        rec('user_message', message=7),
        rec('agent_message', message=''),
        rec('agent_message'),
        rec('user_message', message='What now?'),
    ]),
    ('printed-messages', 'the messages `codex exec --json` prints, whole when they are over', [
        printed('started', id='item_0', type='agent_message', text='not yet'),
        printed('completed', id='item_0', type='agent_message', text='  I will look.  '),
        printed('completed', id='item_1', type='user_message', message='A user said this.'),
        printed('completed', id='item_2', type='agent_message', content=[{'type': 'output_text', 'text': 'From parts.'},
                                                                         {'type': 'OutputText', 'text': 'Second.'},
                                                                         {'type': 'image', 'text': 'no'}, 'nor this']),
        printed('completed', id='item_3', type='agent_message', text='Final.', phase='final_answer'),
        printed('completed', id='item_4', type='agent_message', text='   '),
        printed('completed', id='item_5', type='error', message='it broke'),
        written('completed', type='AgentMessage', id='m1', text='Written by the app server.'),
        written('completed', type='UserMessage', id='m2', content=[{'type': 'InputText', 'text': 'Asked there.'}]),
        {'type': 'item.completed', 'payload': {'item': {'id': 'p1', 'type': 'agent_message', 'text': 'Item in the payload.'}}},
        {'type': 'item.completed', 'item': 'not an object'},
        {'timestamp': AT, 'type': 'event_msg', 'payload': {'type': 'item_completed'},
         'item': {'type': 'AgentMessage', 'id': 'm9', 'text': 'Item on the envelope.'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 1}},
    ]),
    ('message-ids', "the id the provider gave an agent's message: of the first record of it that names one", [
        said('assistant', 'Told twice.'),
        written('completed', type='AgentMessage', id='m1', text='Told twice.'),
        written('completed', type='AgentMessage', id='m2', text='Told twice.'),
        rec('agent_message', message='Told once more.'),
        printed('completed', id='i3', type='agent_message', text='Told once more.'),
        written('completed', type='AgentMessage', id='', text='An empty id.'),
        written('completed', type='AgentMessage', id=7, text='A number for an id.'),
        printed('completed', id='u1', type='user_message', message='A user has none.'),
        written('completed', type='UserMessage', id='u2', content=[{'type': 'InputText', 'text': 'Nor here.'}]),
    ]),
    ('commands', 'a command as it is printed: running, over, failed, with what it printed', [
        said('user', 'Run things.'),
        printed('started', id='item_0', type='command_execution', command="/usr/bin/bash -lc 'cargo test -p kernel'",
                aggregated_output='', exit_code=None, status='in_progress'),
        printed('completed', id='item_0', type='command_execution', command="/usr/bin/bash -lc 'cargo test -p kernel'",
                aggregated_output='ok\n', exit_code=0, status='completed'),
        printed('completed', id='item_1', type='command_execution', command='make build', aggregated_output='', exit_code=0),
        printed('completed', id='item_2', type='command_execution', command='false', aggregated_output='', exit_code=1),
        printed('completed', id='item_3', type='command_execution', command='make all', aggregated_output=MANY, exit_code='2'),
        printed('completed', id='item_4', type='command_execution', command='python3 gen.py', aggregated_output=WIDE + '   \nshort',
                exit_code='two'),
        printed('completed', id='item_5', type='local_shell_call', command=['python3', '-c', "print('a b')", 'plain', '', 7],
                output='from output'),
        printed('completed', id='item_6', type='command_execution', command='npm run x', stdout='from stdout', exit_code=0.0),
        printed('completed', id='item_7', type='command_execution', command='npm run y', stderr='from stderr', exit_code=True),
        printed('completed', id='item_8', type='command_execution', command='npm run z', aggregated_output=None, output='',
                stdout='', stderr=''),
        printed('completed', id='item_9', type='command_execution', command='npm run w',
                aggregated_output='Command: npm run w\nOutput:\nreal\nlines'),
        printed('completed', id='item_10', type='command_execution', command='five', aggregated_output='1\n2\n3\n4\n5'),
        printed('completed', id='item_11', type='command_execution', command='six', aggregated_output='1\n2\n3\n4\n5\n6\n'),
        printed('completed', id='item_12', type='command_execution', command='parts',
                aggregated_output=[{'text': 'a\n'}, 'b', {'text': 7}, 4]),
        printed('completed', id='item_13', type='command_execution', command='object', aggregated_output={'code': 3}),
        printed('completed', id='item_14', type='command_execution', command='nothing in parts', aggregated_output=[4, {}]),
        printed('completed', type='command_execution', command='no id at all, with signs: é ①'),
        printed('completed', call_id='call-9', type='command_execution', command='id from call_id', aggregated_output='x'),
        printed('updated', id='item_15', type='command_execution', command='still running', aggregated_output='so far'),
        printed('completed', id='item_16', type='command_execution', aggregated_output='no command'),
        printed('completed', id='item_17', type='command_execution', command='', aggregated_output='empty command'),
    ]),
    ('wrappers', 'the shell around a command is taken off where it is only a wrapper', [
        printed('completed', id='w0', type='command_execution', command="bash -lc 'make test'", exit_code=0),
        printed('completed', id='w1', type='command_execution', command="/bin/zsh -lc 'echo '\"'\"'quoted'\"'\"' done'", exit_code=0),
        printed('completed', id='w2', type='command_execution', command='sh -lc "make it"', exit_code=0),
        printed('completed', id='w3', type='command_execution', command="bash -c 'make test'", exit_code=0),
        printed('completed', id='w4', type='command_execution', command="fish -lc 'make test'", exit_code=0),
        printed('completed', id='w5', type='command_execution', command="bash -lc 'unbalanced", exit_code=0),
        printed('completed', id='w6', type='command_execution', command="bash -lc 'a' 'b'", exit_code=0),
        printed('completed', id='w7', type='command_execution', command="  bash -lc 'padded'", exit_code=0),
        printed('completed', id='w8', type='command_execution', command="env X=1 bash -lc 'not first'", exit_code=0),
        printed('completed', id='w9', type='command_execution', command="bash -lc 'multi\nline\nscript'", exit_code=0),
        printed('completed', id='w10', type='command_execution', command='bash -lc', exit_code=0),
        printed('completed', id='w11', type='command_execution', command="'/usr/bin/bash' -lc 'quoted shell'", exit_code=0),
    ]),
    ('explorations', 'reads, listings and searches that follow one another are one cell', [
        said('assistant', 'Looking around.'),
        printed('completed', id='e0', type='command_execution', command="bash -lc 'rg -n needle src/'", exit_code=0),
        printed('completed', id='e1', type='command_execution', command='cat src/a.rs src/a.rs README.md', exit_code=0),
        printed('started', id='e2', type='command_execution', command='ls -la crates'),
        printed('completed', id='e3', type='command_execution', command='make build', aggregated_output='built', exit_code=0),
        printed('completed', id='e4', type='command_execution', command='sed -n 1,20p server/lib/x.py', exit_code=0),
        printed('completed', id='e5', type='command_execution', command='git grep -n token', exit_code=0),
        printed('completed', id='e6', type='command_execution', command='git ls-files src', exit_code=0),
        printed('completed', id='e7', type='command_execution', command='git ls-files', exit_code=0),
        printed('completed', id='e8', type='command_execution', command='git status', aggregated_output='clean', exit_code=0),
        printed('completed', id='e9', type='command_execution', command='rg --files crates/kernel', exit_code=0),
        printed('completed', id='e10', type='command_execution', command='rg --files', exit_code=0),
        printed('completed', id='e11', type='command_execution', command='grep -rn "a b" .', exit_code=0),
        printed('completed', id='e12', type='command_execution', command='rg -n', exit_code=0),
        printed('completed', id='e13', type='command_execution', command='find . -name "*.rs"', exit_code=0),
        printed('completed', id='e14', type='command_execution', command='fd', exit_code=0),
        printed('completed', id='e15', type='command_execution', command='ls', exit_code=0),
        printed('completed', id='e16', type='command_execution', command='head -n 5 notes', exit_code=0),
        printed('completed', id='e17', type='command_execution', command='tail -f log.txt', exit_code=0),
        printed('completed', id='e18', type='command_execution', command='nl -ba Makefile.am', exit_code=0),
        printed('completed', id='e19', type='command_execution', command='/usr/bin/cat ./x', exit_code=0),
        said('assistant', 'Then chains.'),
        printed('completed', id='c0', type='command_execution', command='cd /srv && pwd && cat a.txt; cat b.txt && true', exit_code=0),
        printed('completed', id='c1', type='command_execution', command='cat a.txt && make build', exit_code=0),
        printed('completed', id='c2', type='command_execution', command='make build && rg needle src', exit_code=0),
        printed('completed', id='c3', type='command_execution', command='pwd; true', exit_code=0),
        printed('completed', id='c4', type='command_execution', command='pwd && ls src', exit_code=0),
        printed('completed', id='c5', type='command_execution', command='make a && make b', aggregated_output='', exit_code=0),
        printed('completed', id='c6', type='command_execution', command='cat a.txt; cat a.txt; cat c.txt, d.txt', exit_code=0),
        printed('completed', id='c7', type='command_execution', command=';', exit_code=0),
        printed('completed', id='c8', type='command_execution', command="rg 'unbalanced", exit_code=0),
        printed('completed', id='c9', type='command_execution', command="bash -lc 'cat x.py && sed -n 1p y.py'", exit_code=0),
        said('user', 'A user\'s row holds no cell.'),
        printed('completed', id='c10', type='command_execution', command='cat only.txt', exit_code=0),
    ]),
    ('edits', 'a change to files, in each form it is written in', [
        printed('started', id='p0', type='file_change', changes=[{'path': 'src/a.rs', 'kind': 'update'}], status='in_progress'),
        printed('completed', id='p0', type='file_change', status='completed',
                changes=[{'path': 'src/a.rs', 'kind': 'update'}, {'path': 'new.md', 'kind': 'add'},
                         {'path': 'gone.txt', 'kind': 'DELETE'}, {'path': 'made.txt', 'kind': 'create'},
                         {'path': 'out.txt', 'kind': 'remove'}, {'path': 'odd.txt', 'kind': 'rename'},
                         {'path': '', 'kind': 'add'}, {'path': 'bare.txt'}, 'not an object']),
        printed('completed', id='p1', type='file_change', status='failed', changes=[{'path': 'src/b.rs', 'kind': 'update'}]),
        printed('completed', id='p2', type='patch_apply', patch=PATCH),
        printed('completed', id='p3', type='patch_apply', input='no markers here\nsecond\nthird\nfourth\nfifth'),
        printed('completed', id='p4', type='file_change',
                changes={'src/a.rs': {'type': 'update', 'unified_diff': DIFF}, 'new.md': {'kind': 'add', 'diff': '+hello'},
                         'gone.txt': {'type': 'delete'}, 'odd': 'not an object', 'other.txt': {'type': 'Remove'},
                         'x.txt': {'type': 'move'}}),
        printed('completed', id='p5', type='file_change', changes={'changes': [{'path': 'nested.txt', 'kind': 'add'}]}),
        printed('completed', id='p6', type='file_change', patch={'patch': '*** Update File: deep.txt\n+x'}),
        printed('completed', id='p7', type='file_change', patch={'input': {'k.txt': {'type': 'add'}}}),
        printed('completed', type='file_change', changes=[{'path': 'no-id.txt', 'kind': 'add'}]),
        printed('completed', type='file_change', patch='*** Add File: no-id-text.txt\n+x'),
        printed('completed', type='file_change', changes={'no-id-map.txt': {'type': 'add'}}),
        printed('completed', id='p8', type='file_change'),
        printed('completed', id='p9', type='file_change', changes=7),
        printed('completed', id='p10', type='file_change', changes={'changes': [], 'input': ''}),
        printed('completed', id='p11', type='file_change', changes={'changes': None, 'a.txt': {'type': 'add'}}),
        printed('completed', type='file_change', changes={'changes': [], 'input': ''}),
    ]),
    ('calls', 'a tool of a server, a search of the web, a picture, a tool of the app\'s own', [
        printed('started', id='m0', type='mcp_tool_call', server='docs', tool='search', arguments={'query': 'wake', 'limit': 3},
                status='in_progress'),
        printed('completed', id='m0', type='mcp_tool_call', server='docs', tool='search', arguments={'query': 'wake', 'limit': 3},
                result={'content': [{'type': 'text', 'text': 'two hits'}], 'structured_content': None}, status='completed'),
        printed('completed', id='m1', type='mcp_tool_call', tool='bare', arguments={}),
        printed('completed', id='m2', type='mcp_tool_call', server='s', name='by-name', arguments=''),
        printed('completed', id='m3', type='mcp_tool_call', server='s', arguments=None, result='plain text\nsecond line'),
        printed('completed', id='m4', type='mcp_tool_call', server='s', tool='t', arguments=['é', 1, None, True],
                result=[{'text': 'part one\n'}, 'part two']),
        printed('completed', id='m5', type='mcp_tool_call', server='s', tool='t', arguments='text arguments',
                error={'message': 'it failed'}, result='not shown'),
        printed('completed', id='m6', type='mcp_tool_call', server='s', tool='t', error='plain error'),
        printed('completed', id='m7', type='mcp_tool_call', server='s', tool='t', error={'code': 5}),
        printed('completed', type='mcp_tool_call', server='s', tool='no-id', arguments={'a': {'b': [1, 2]}}),
        printed('completed', call_id='mc', type='mcp_tool_call', server='s', tool='t', result=''),
        printed('started', id='s0', type='web_search', query='rust tokio select'),
        printed('completed', id='s0', type='web_search', query='rust tokio select'),
        printed('completed', id='s1', type='web_search_call', action='open a page'),
        printed('completed', id='s2', type='web_search'),
        printed('completed', type='web_search', query='no id: é ②'),
        printed('completed', type='web_search'),
        printed('completed', id='i0', type='image_view', path='/srv/shots/a.png'),
        printed('completed', type='image_view', path='/srv/shots/no id.png'),
        printed('completed', type='image_view'),
        printed('started', id='g0', type='image_generation', saved_path=''),
        printed('completed', id='g0', type='image_generation', saved_path='/srv/out.png', status='completed'),
        printed('completed', id='g1', type='image_generation', status='FAILED'),
        printed('completed', type='image_generation', saved_path='/srv/no-id.png'),
        printed('started', id='d0', type='dynamic_tool_call', tool='deploy'),
        printed('completed', id='d0', type='dynamic_tool_call', tool='deploy', success=True, content_items=[{'text': 'deployed'}]),
        printed('completed', id='d1', type='dynamic_tool_call', name='by-name', success=False),
        printed('completed', id='d2', type='dynamic_tool_call', status='Failed', result='why it failed'),
        printed('completed', call_id='d3', type='dynamic_tool_call', tool='by call id', result={'ok': True}),
        printed('completed', type='dynamic_tool_call', tool='no id'),
        printed('completed', id='m8', type='mcp_tool_call', server='s', tool='t', error={}, result='shown after all'),
        written('completed', type='DynamicToolCall', id='d9', tool='camel', contentItems=[{'text': 'from contentItems'}]),
        written('completed', type='ImageGeneration', id='g9', savedPath='/srv/camel.png'),
    ]),
    ('thoughts', 'a thought with a summary, a plan, an item nobody knows, a call by its name', [
        printed('completed', id='r0', type='reasoning', summary=['Weighing two ways.', '  ', 7, 'Chose the second.']),
        printed('completed', id='r1', type='reasoning', summary='One line.'),
        printed('completed', id='r2', type='reasoning', summary_text=['From summary_text.']),
        printed('completed', id='r3', type='reasoning', text='only a text: no cell'),
        printed('completed', id='r4', type='reasoning', summary=[]),
        printed('completed', id='pl0', type='plan', text='  First the tests.\nThen the port.\n\nThen land.  '),
        printed('completed', id='pl1', type='plan', text='   '),
        printed('completed', id='u0', type='todo_list', items=[{'text': 'a', 'completed': False}]),
        printed('completed', id='f0', type='function_call', name='lookup', arguments='{}'),
        printed('started', id='f1', type='tool_call', tool='other'),
        printed('completed', id='f2', type='function_call'),
        printed('completed', call_id='f3', type='function_call', name='by call id'),
        printed('completed', type='function_call', name='no id: é'),
        printed('started', id='f4', type='function_call', name='spawn_agent',
                arguments=arguments(agent_type='reviewer', message='  Review   the\nport.  ')),
        printed('completed', id='f5', type='function_call', name='spawn_agent', arguments={'agent_role': 'worker', 'prompt': 'Do it.'}),
        printed('completed', id='f6', type='function_call', name='spawn_agent', arguments='not json'),
        printed('completed', id='f7', type='function_call', name='wait_agent', arguments=arguments(targets=['thread-aaaaaaaa-1', 'thread-b'])),
        printed('started', id='f8', type='function_call', name='wait', arguments=arguments(target='thread-one-that-is-rather-long-for-a-label-0123')),
        printed('completed', id='f9', type='function_call', name='wait', arguments=arguments(cell_id='c1')),
        printed('completed', id='f10', type='function_call', name='wait_agent', arguments=arguments(targets={'not': 'a list'})),
        printed('completed', id='f11', type='function_call', name='send_input',
                arguments=arguments(target='thread-cccccccc-9', message='  Go   on.  ')),
        printed('completed', id='f12', type='function_call', name='resume_agent', arguments=arguments(id='thread-dddddddd')),
        printed('started', id='f13', type='function_call', name='close_agent', arguments=arguments()),
        printed('completed', id='f14', type='function_call', name='wait_agent', arguments=arguments(targets=[])),
        printed('completed', id='f15', type='function_call', name='wait', arguments=arguments(target='')),
        written('completed', type='Reasoning', id='r9', summaryText=['From summaryText.']),
    ]),
    ('helpers', 'the agents Codex starts itself, as items', [
        written('started', type='CollabAgentToolCall', id='h0',
                details={'tool': 'spawnAgent', 'status': 'inProgress', 'prompt': '  Review  the port.  ',
                         'receiverThreadIds': ['thread-aaaaaaaa-1']}),
        written('completed', type='CollabAgentToolCall', id='h0',
                details={'tool': 'spawnAgent', 'status': 'completed', 'prompt': 'Review the port.',
                         'receiverThreadIds': ['thread-aaaaaaaa-1'],
                         'agentsStates': {'thread-aaaaaaaa-1': {'status': 'running', 'agentNickname': 'Ada', 'agentRole': 'reviewer'},
                                          'thread-0': {'status': {'completed': '  It   is done.  '}, 'agent_nickname': 'Bo'},
                                          'thread-2': 'pending_init',
                                          'thread-3': {'status': {'errored': ''}, 'role': 'worker'},
                                          'thread-4': {'status': 7, 'message': '  a   message  '},
                                          'thread-5': {'status': {}, 'agent_type': 'kind'},
                                          'thread-6-long-name': {'status': 'notFound'}}}),
        printed('completed', id='h1', type='collab_tool_call', tool='wait', status='completed',
                receiver_thread_ids=['thread-aaaaaaaa-1', 'thread-bbbbbbbb-2']),
        printed('completed', id='h2', type='collab_tool_call', tool='wait', status='failed', receiver_thread_ids='thread-only-one'),
        printed('completed', id='h3', type='collab_tool_call', tool='send_input', status='completed', receiver_thread_ids=['thread-cccccccc-3']),
        printed('started', id='h4', type='collab_tool_call', tool='send-input', receiver_thread_ids=[]),
        printed('completed', id='h5', type='collab_tool_call', tool='resume_agent', status='interrupted'),
        printed('started', id='h6', type='collab_tool_call', tool='resumeAgent'),
        printed('completed', id='h7', type='collab_tool_call', tool='close_agent', status='shutdown'),
        printed('started', id='h8', type='collab_tool_call', tool='close agent'),
        printed('completed', id='h9', type='collab_tool_call', tool='list_agents', status='odd'),
        printed('completed', id='h10', type='collab_tool_call', status='in_progress'),
        printed('completed', id='h11', type='collab_tool_call', tool='wait', status='running', agents_states='not a map'),
        printed('completed', type='collab_tool_call', tool='wait', details={'id': 'from details', 'tool': 'wait'}),
        printed('completed', type='collab_tool_call', details={'call_id': 'from call_id', 'type': 'collab_tool_call'}),
        printed('completed', type='collab_tool_call', details={'callId': 'fromCallId', 'tool': 'spawn_agent'}),
        printed('completed', type='SubAgentActivity', kind='started', agent_path='  /root/reviewer  ', event_id='ev-1'),
        printed('completed', type='sub_agent_activity', kind='Interacted', agentPath='/root/worker', eventId='ev-2'),
        printed('completed', type='sub-agent-activity', kind='interrupted', call_id='from-call'),
        printed('completed', type='sub agent activity', kind='other'),
    ]),
    ('helper-events', 'the agents Codex starts itself, as events of their own', [
        rec('collab_agent_spawn_begin', call_id='s1', prompt='  Review   it.  ', agent_type='reviewer'),
        rec('collab_agent_spawn_end', call_id='s1', new_thread_id='thread-new-1', new_agent_nickname='Ada',
            new_agent_role='reviewer', prompt='Review it.', status='running'),
        rec('collab_agent_spawn_end', call_id='s2', new_thread_id='thread-new-2', status={'errored': 'no seat'}),
        rec('collab_agent_spawn_end', call_id='s3', status='completed'),
        rec('collab_agent_interaction_begin', call_id='i1', receiver_thread_id='thread-new-1', prompt='Go on.',
            receiver_agent_nickname='Ada'),
        rec('collab_agent_interaction_end', call_id='i1', receiver_thread_id='thread-new-1', receiver_agent_role='reviewer',
            status='completed'),
        rec('collab_agent_interaction_end', call_id='i2', agent_type='worker', status='failed'),
        rec('collab_waiting_begin', call_id='w1', receiver_thread_ids=['thread-new-1', 'thread-new-2', 'thread-new-3'],
            receiver_agents=[{'agent_nickname': 'Ada', 'agent_role': 'reviewer'}, 'not an object', {'agentNickname': 'Cy'}]),
        rec('collab_waiting_end', call_id='w1', receiver_thread_ids=['thread-new-1', 'thread-new-2'],
            agent_statuses=[{'thread_id': 'thread-new-1', 'status': 'completed', 'agent_nickname': 'Ada'},
                            'not an object',
                            {'threadId': 'thread-new-2', 'status': {'errored': 'it broke'}, 'agentRole': 'worker'}]),
        rec('collab_waiting_begin', call_id='w2', receiver_thread_ids='thread-only'),
        rec('collab_waiting_end', call_id='w2', statuses={'thread-b': 'completed', 'thread-a': {'completed': 'fine'},
                                                           'thread-c': 'not_found'}),
        rec('collab_waiting_end', call_id='w3'),
        rec('collab_waiting_end', call_id='w4', statuses='odd', receiver_thread_ids=['one']),
        rec('collab_waiting_end', call_id='w5', agentStatuses=[{'thread_id': 't', 'status': 'Error'}]),
        rec('collab_close_begin', call_id='c1', receiver_thread_id='thread-new-1', receiver_agent_nickname='Ada'),
        rec('collab_close_end', call_id='c1', receiver_thread_id='thread-new-1', status='shutdown'),
        rec('collab_close_end', call_id='c2', status='odd'),
        rec('collab_resume_begin', call_id='r1', receiver_thread_id='thread-new-2', agent_type='worker'),
        rec('collab_resume_end', call_id='r1', receiver_thread_id='thread-new-2', status='running'),
        rec('sub_agent_activity', kind='started', agent_path='/root/a', event_id='e1'),
        rec('sub_agent_activity', kind='interrupted', call_id='from-call'),
        rec('collab_agent_unknown_event', call_id='x1'),
        rec('collab_close_end', call_id='c3', status='notFound'),
        rec('collab_resume_end', call_id='r2', status='pendingInit'),
        rec('collab_waiting_begin', call_id='w6', receiver_thread_ids=['a'], receiver_agents='odd'),
        rec('collab_waiting_end', call_id='w7', agent_statuses={'odd': 1}, statuses={'thread-z': 'running'}),
        {'timestamp': AT, 'type': 'collab_close_begin', 'call_id': 'bare', 'receiver_thread_id': 'thread-bare'},
    ]),
    ('older-calls', 'a call and what came back, as the older records have them', [
        said('user', 'Do things.'),
        call('exec_command', 'c1', arguments=arguments(cmd='ls -la src', workdir='/srv')),
        back('c1', 'Chunk ID: 1\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\na.rs\nb.rs\n'),
        call('shell', 'c2', arguments=arguments(command=['bash', '-lc', 'make test'])),
        back('c2', 'Process exited with code 2\nOutput:\nfailed here'),
        call('local_shell', 'c3', arguments={'command': 'echo dict arguments'}),
        back('c3', ''),
        call('exec_command', 'c4', arguments='not json at all ' + 'x' * 500),
        back('c4', 'Process exited with code nine\nOutput:\nodd code'),
        call('exec_command', 'c5', arguments=arguments(other='no command')),
        call('exec_command', 'c6', arguments=arguments(cmd='echo ' + 'y' * 500)),
        back('c6', [{'text': 'from parts'}]),
        call('apply_patch', 'p1', kind='custom_tool_call', input=PATCH),
        back('p1', 'Success', kind='custom_tool_call_output'),
        call('apply_patch', 'p2', kind='custom_tool_call', input=TWO_EDITS),
        call('apply_patch', 'p3', kind='custom_tool_call', input='*** Begin Patch\n*** End Patch'),
        call('apply_patch', 'p4', kind='custom_tool_call', input={'not': 'a text'}),
        call('apply_patch', 'p5', kind='custom_tool_call', input='*** Update File: same.txt\n same\n'),
        rec('patch_apply_end', call_id='p2', success=True, turn_id='',
            changes={'src/a.rs': {'type': 'update', 'unified_diff': '@@\n-one\n+uno'}}, stdout='Done'),
        rec('patch_apply_end', call_id='p3', success=False, changes={'x': {'type': 'add'}}, output='refused'),
        rec('patch_apply_end', call_id='p4', changes={}),
        rec('patch_apply_end', call_id='p5', changes='odd'),
        call('web_search', 'w1', query='tokio select'),
        back('w1', 'three results\nsecond\nthird'),
        call('web_search_call', 'w2', input='from input'),
        call('web_search', 'w3'),
        call('lookup', 'g1', arguments=arguments(q=1)),
        back('g1', 'found\nit'),
        call('lookup', 'g2'),
        call('Read', 'g3'),
        rec('function_call', name='no id'),
        rec('function_call'),
        back('', 'for the last call that waits'),
        back('nobody', 'for no call at all'),
        rec('function_call_output', call_id='g2', stdout='from stdout'),
        rec('function_call_output'),
        call('exec_command', 'c7'),
        call('apply_patch', 'p6', kind='custom_tool_call', input='*** Update File: one.txt\n@@\n-a\n+b'),
        call('apply_patch', 'p7', kind='custom_tool_call',
             input='*** Add File: 1.txt\n+x\n*** Add File: 2.txt\n+x\n*** Add File: 3.txt\n+x\n*** Add File: 4.txt\n+x\n*** Add File: 1.txt'),
        rec('patch_apply_end', call_id='p6', success=None, changes={'y': {'type': 'add'}}),
    ]),
    ('older-kinds', 'the calls an older record names: plans, goals, waits, pictures, searches, helpers', [
        call('update_plan', 'u1', arguments=arguments(plan=[{'step': 'Port it', 'status': 'in_progress'},
                                                             {'step': 'Land it', 'status': 'pending'},
                                                             {'status': 'COMPLETED'}, 'odd', {'step': 'élan', 'status': "it's_done éa"}])),
        call('update_plan', 'u2', arguments=arguments(plan='odd')),
        call('update-plan', 'u3', arguments='not json'),
        call('get_goal', 'g1', arguments=arguments()),
        call('create_goal', 'g2', arguments=arguments(objective='Ship the port')),
        call('update_goal', 'g3', arguments=arguments(status='complete')),
        call('wait', 'w1', arguments=arguments(cell_id='c1')),
        call('write_stdin', 'w2', arguments=arguments(session_id=41, chars='y\n')),
        call('write_stdin', 'w3', arguments=arguments(cell_id='cell-9')),
        call('write_stdin', 'w4', arguments=arguments()),
        call('run', 'r1', namespace='web', arguments=arguments(search_query=[{'q': 'rust regex'}, {'q': 'second'}])),
        call('web__run', 'r2', arguments=arguments(open=[{'ref_id': 'x'}])),
        call('run', 'r3', namespace='other', arguments=arguments(search_query=[{'q': 'not web'}])),
        call('run', 'r4', input=arguments(search_query=[{'q': 'from input'}])),
        call('run', 'r5', namespace='Web', arguments=arguments(search_query='odd')),
        call('view_image', 'v1', arguments=arguments(path='/srv/a.png')),
        call('image_gen__imagegen', 'v2', arguments=arguments(prompt='a fox ' * 60)),
        call('view_image', 'v3', arguments=arguments()),
        call('spawn_agent', 'h1', arguments=arguments(agent_type='reviewer', message='Review it.')),
        back('h1', json.dumps({'agent_id': 'thread-new-1', 'nickname': 'Ada', 'agent_role': 'reviewer'})),
        call('spawn_agent', 'h2', arguments=arguments()),
        back('h2', json.dumps({'nickname': 'Bo'})),
        call('wait_agent', 'h3', arguments=arguments(targets=['thread-new-1'])),
        back('h3', json.dumps({'status': {'completed': 'all done'}})),
        call('wait_agent', 'h4', arguments=arguments(targets=['thread-new-1', 'thread-new-2'])),
        back('h4', json.dumps({'status': 'errored'})),
        call('send_input', 'h5', arguments=arguments(target='thread-new-1', message='Go on.')),
        back('h5', json.dumps({'completed': 'x' * 300})),
        call('close_agent', 'h6', arguments=arguments(target='thread-new-1')),
        back('h6', '  closed   cleanly  '),
        call('resume_agent', 'h7', arguments=arguments(id='thread-new-2')),
        back('h7', ''),
        call('send_message', 'h8', arguments=arguments(target='x')),
        call('followup_task', 'h9'),
        call('list_agents', 'h10'),
        call('interrupt_agent', 'h11'),
        call('wait', 'h12', arguments=arguments(targets=['a', 'b'])),
        back('h12', json.dumps({'status': 'running'})),
        call('send_input', 'h13', arguments=arguments(target='thread-new-1')),
        back('h13', json.dumps({'status': 'completed'})),
    ]),
    ('nested', 'a call that runs other tools inside it', [
        call('exec', 'n1', kind='custom_tool_call', input='const r = await tools.exec_command({cmd: "ls -la src", workdir: "/srv"});'),
        back('n1', 'Process exited with code 0\nOutput:\na.rs', kind='custom_tool_call_output'),
        call('exec', 'n2', kind='custom_tool_call', input='await tools.exec_command({"command": "echo \\"quoted\\" here"})'),
        call('exec', 'n3', kind='custom_tool_call', input='await tools.exec_command({other: 1})'),
        call('exec', 'n4', kind='custom_tool_call', input='await tools.apply_patch("*** Begin Patch"); await tools.exec_command({cmd: "x"})'),
        back('n4', 'Success', kind='custom_tool_call_output'),
        call('exec', 'n5', kind='custom_tool_call', input='await tools.web__run({search_query: []})'),
        call('exec', 'n6', kind='custom_tool_call', input='await tools.view_image({path: "/a.png"})'),
        call('exec', 'n7', kind='custom_tool_call', input='await tools.image_gen__imagegen({})'),
        call('exec', 'n8', kind='custom_tool_call', input='await tools.write_stdin({session_id: 1})'),
        call('exec', 'n9', kind='custom_tool_call', input='await tools.wait({})'),
        call('exec', 'n10', kind='custom_tool_call', input='await tools.spawn_agent({})'),
        call('exec', 'n11', kind='custom_tool_call', input='await tools.send_message({}); await tools.followup_task({})'),
        call('exec', 'n12', kind='custom_tool_call', input='await tools.wait_agent({})'),
        call('exec', 'n13', kind='custom_tool_call', input='await tools.lookup({}); await tools.other({})'),
        call('exec', 'n14', kind='custom_tool_call', input='plain script with no tool'),
        call('exec', 'n15', arguments='await tools.exec_command({cmd: "from arguments"})'),
        call('Exec', 'n16', kind='custom_tool_call', input='await tools.execCommand({cmd: "camel"})'),
        call('exec', 'n17', kind='custom_tool_call', input='await tools.exec_command({cmd: "bad \\q escape"})'),
        call('exec', 'n18', kind='custom_tool_call'),
    ]),
    ('superseded', 'an older record of a call is left out where the same turn has the item', [
        said('user', 'One turn, told twice.'),
        rec('function_call', name='exec_command', call_id='c1', arguments=arguments(cmd='ls'), turn_id='turn-1'),
        rec('function_call_output', call_id='c1', output='Process exited with code 0\nOutput:\na'),
        written('completed', turn='turn-1', type='CommandExecution', id='c1x', command='ls', aggregatedOutput='a', exitCode=0),
        rec('function_call', name='exec_command', call_id='c2', arguments=arguments(cmd='pwd'), turn_id='turn-1'),
        rec('custom_tool_call', name='apply_patch', call_id='p1', input=PATCH,
            internal_chat_message_metadata_passthrough={'turn_id': 'turn-1'}),
        rec('patch_apply_end', call_id='p1', turn_id='turn-1', changes={'src/a.rs': {'type': 'update'}}),
        rec('function_call', name='lookup', call_id='g1', turn_id='turn-2'),
        written('completed', turn='turn-2', type='McpToolCall', id='g1x', server='s', tool='lookup'),
        rec('function_call', name='exec_command', call_id='c3', arguments=arguments(cmd='make'), turn_id='turn-2'),
        written('started', turn='turn-2', type='CommandExecution', id='c3x', command='make'),
        written('completed', type='CommandExecution', id='c4x', command='no turn named', exitCode=0),
        rec('function_call', name='exec_command', call_id='c5', arguments=arguments(cmd='other turn'), turn_id='turn-3'),
        rec('function_call', name='web_search', call_id='w1', query='q', turn_id='turn-2',
            internal_chat_message_metadata_passthrough='odd'),
        written('completed', turn='turn-2', type='DynamicToolCall', id='d1', tool='lookup'),
    ]),
    ('bounds', 'no cell grows past its bounds', [
        printed('completed', id='b0', type='patch_apply', patch='\n'.join(['*** Update File: big.txt'] + [f'+line {n}' for n in range(700)])),
        printed('completed', id='b1', type='patch_apply', patch='*** Update File: wide.txt\n+' + 'x' * 2500 + '   \n+' + 'y' * 1999 + '\n+' + 'z' * 2000),
        printed('completed', id='b2', type='patch_apply', patch='\n'.join(['*** Update File: heavy.txt'] + ['+' + 'h' * 1989 for _ in range(34)])),
        printed('completed', id='b3', type='command_execution', command='x' * 3000, aggregated_output='short', exit_code=0),
        printed('completed', id='b4', type='patch_apply', patch='\n'.join(['*** Update File: exact.txt'] + [f'+l{n}' for n in range(599)])),
    ]),
    ('odd-lines', 'what is no record is passed over', [
        '',
        '   ',
        'not json',
        '[1, 2]',
        '"a text"',
        '7',
        {'timestamp': AT, 'type': 'event_msg', 'payload': 'not an object'},
        {'timestamp': AT, 'type': 'event_msg'},
        {'type': 'agent_message', 'payload': {'message': 'type on the envelope'}},
        {'timestamp': 12, 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'a number for a time'}},
        {'payload': {'type': 7}},
        {'timestamp': None, 'payload': {'type': 'user_message', 'message': 'no time'}},
        rec('token_count', info={'total': 1}),
        rec('turn_context', cwd='/srv'),
        '{"type": "event_msg", "payload": {"type": "agent_message", "message": "after the odd ones"}}',
    ]),
]

# The id a cell is given when its call has none: what is no letter and no number becomes a dash.
IDS = ['plain words', 'é ü ñ', 'combining á and ypogegrammeni ͅ', 'circled Ⓐ ①', 'roman Ⅷ super ² half ½',
       'cjk 一二 〇', 'under_score-dash', 'devanagari का १', '', 'emoji \U0001f600 arabic ٣',
       'x' * 100, 'thai กิ ๑', 'modifier ʰ ᴬ', 'titlecase ǅ']


@tool_scratch.case('clarp-codex-history-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: the turns the Python Host reads from the rollouts above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import codex_tool_cells, codex_transcript

    # The one thing the Rust Host does otherwise (defect R41 of the reference): a cell made of an
    # older record keeps the marks of that record when what came back for its call is put on it,
    # so that it is still left out where the same turn has the item too. The Python Host makes
    # the cell anew there and loses the marks: the cell of a call then stands beside the cells of
    # what the call ran. Mended here in the reference's own function, to say what is meant.
    as_it_is = codex_transcript._attach_output

    def mended(turns, call_id, output):
        marks = {}
        for turn in turns:
            for cell in turn.get('display_cells') or []:
                if '_legacy_turn_id' in cell:
                    marks[id(turn), cell.get('id')] = (cell['_legacy_turn_id'], cell.get('_legacy_category'))
        as_it_is(turns, call_id, output)
        for turn in turns:
            for cell in turn.get('display_cells') or []:
                held = marks.get((id(turn), cell.get('id')))
                if held and '_legacy_turn_id' not in cell:
                    cell['_legacy_turn_id'], cell['_legacy_category'] = held

    out = []
    for name, what, lines in ROLLOUTS:
        wrote = [line if isinstance(line, str) else json.dumps(line) for line in lines]
        path = root / f'{name}.jsonl'
        path.write_text('\n'.join(wrote) + '\n')
        row = {'name': name, 'what': what, 'lines': wrote, 'turns': codex_transcript.parse_turns(path)}
        codex_transcript._attach_output = mended
        try:
            otherwise = codex_transcript.parse_turns(path)
        finally:
            codex_transcript._attach_output = as_it_is
        if otherwise != row['turns']:
            row['turns_mended'] = otherwise
        out.append(row)
    ids = [{'of': text, 'id': codex_tool_cells._safe_id('cell', text)} for text in IDS]
    return {'rollouts': out, 'ids': ids}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    told = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', ' "rollouts": [']
    rows = told['rollouts']
    for number, row in enumerate(rows):
        out.append('  {"name": ' + json.dumps(row['name']) + ', "what": ' + json.dumps(row['what']) + ',')
        out.append('   "lines": [')
        out.extend('    ' + json.dumps(line, ensure_ascii=True) + (',' if n < len(row['lines']) - 1 else '')
                   for n, line in enumerate(row['lines']))
        out.append('   ],')
        for key in ('turns', 'turns_mended'):
            if key not in row:
                continue
            out.append(f'   "{key}": [')
            out.extend('    ' + json.dumps(turn, ensure_ascii=True) + (',' if n < len(row[key]) - 1 else '')
                       for n, turn in enumerate(row[key]))
            out.append('   ]' + (',' if key == 'turns' and 'turns_mended' in row else ''))
        out.append('  }' + (',' if number < len(rows) - 1 else ''))
    out.append(' ],')
    out.append(' "ids": [')
    ids = told['ids']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(ids) - 1 else '') for n, row in enumerate(ids))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    cells = lambda row: sum(len(turn.get('display_cells') or []) for turn in row['turns'])
    tools = lambda row: sum(len(turn.get('tools') or []) for turn in row['turns'])
    print(f'{OUT.relative_to(REPO)}: ' + '; '.join(
        f'{row["name"]}: {len(row["lines"])} lines, {len(row["turns"])} turns, {cells(row)} cells, {tools(row)} tools'
        for row in rows))
    print('mended (R41): ' + '; '.join(f'{row["name"]}: {cells(row)} cells as it is, '
                                       f'{sum(len(t.get("display_cells") or []) for t in row["turns_mended"])} mended'
                                       for row in rows if 'turns_mended' in row))
    print(f'totals: {len(rows)} rollouts, {sum(len(row["lines"]) for row in rows)} lines, '
          f'{sum(len(row["turns"]) for row in rows)} turns, {sum(cells(row) for row in rows)} cells, '
          f'{sum(tools(row) for row in rows)} tools, {len(ids)} ids')
    return 0


if __name__ == '__main__':
    sys.exit(main())
