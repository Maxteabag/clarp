#!/usr/bin/env python3
"""What a conversation Antigravity kept says was said in it, as the Python Host reads it for the
conversation of an agent (`server/lib/agy_transcript.py`, `parse_turns`): the turns its own
function reads from made-up transcripts (`brain/<conversation>/.system_generated/logs/
transcript.jsonl`, in the shapes agy 1.1 wrote and the ones agy 1.2 to 1.5 write), written to
`crates/clarp-backend/tests/fixtures/agy-history-vectors.json` with the lines it was given, so
that the Rust Host's reading is held to it line by line. And what each turn is in its
conversation (`commentary`, `final` or neither), as the Python Host marks the rows it reads
(`server/lib/message_writes.py`, `_row_phases`).

Two things the Rust Host does not copy, and they are defined here, not in the Rust: each
transcript is read a second time by the same function with them mended (`turns_mended`), and
the Rust Host is held to that reading.

1. The names agy gives its tools today. The Python Host knows `view_file`, `write_to_file` and
   `replace_file_content` as Read, Write and Edit; agy 1.5 calls them `read_file`, `write_file`
   and `edit_file`, with the file under `Path`, and those are shown as tools nobody knows, without
   their file (in the owner's store on 17 Nov 2026: 2,806 calls of `read_file`, 541 of
   `edit_file` and 197 of `write_file` against 180, 4 and 19 of the older names). Mended: the
   three names are the three tools, and `Path` is their file.
2. What a command printed. agy wrote `Output:` before it and writes `Command output:` now (in the
   same store 2,406 results against 456); the Python Host cuts at the first only, so a result of
   the second kind is shown with the line of how the command ended before it. Mended: cut at
   whichever of the two comes first.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/agy-history-vectors.py
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
OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'agy-history-vectors.json'

HEAD = 'Created At: 2027-03-01T09:00:00Z\nCompleted At: 2027-03-01T09:00:01Z\n'


def ev(kind, step, content=None, **more):
    at = step if isinstance(step, int) and not isinstance(step, bool) else 0
    out = {'step_index': step, 'source': 'MODEL', 'type': kind, 'status': more.pop('status', 'DONE'),
           'created_at': more.pop('at', f'2027-03-01T09:{at // 60:02d}:{at % 60:02d}Z')}
    if content is not None:
        out['content'] = content
    return {**out, **more}


def asked(step, text, **more):
    return ev('USER_INPUT', step, f'<USER_REQUEST>\n{text}\n</USER_REQUEST>', source='USER', **more)


def planned(step, content=None, calls=None, **more):
    if calls is not None:
        more['tool_calls'] = calls
    return ev('PLANNER_RESPONSE', step, content, **more)


def call(name, **args):
    # agy writes what a tool is given as JSON once more: a text within its quotes.
    return {'name': name, 'args': {key: json.dumps(value) if isinstance(value, str) else value
                                   for key, value in args.items()}}


def answer(step, body, **more):
    return ev('GENERIC', step, HEAD + body, **more)


def system(step, body):
    return ev('SYSTEM_MESSAGE', step, f'<SYSTEM_MESSAGE>\n{body}\n</SYSTEM_MESSAGE>', source='SYSTEM')


def ran(code, printed, mark='Output'):
    return f'Command exited with code {code}.\n\n{mark}:\n{printed}'


LONG = 'L' * 700
TASKS = '/home/u/.gemini/antigravity-cli/tasks'

# The transcripts: a name, what it shows, and its lines (an object is written as JSON, a text as it
# is; `('preamble', ...)` is a user's message as the Python Host hands it to agy, see `run`).
TRANSCRIPTS = [
    ('plain', 'what a user wrote and what the agent answered', [
        asked(0, '  Fix the build.  '),
        planned(1, '  On it.  ', input_tokens=10, output_tokens=2, model='gemini-test'),
        ev('USER_INPUT', 2, 'no wrapper at all', source='USER'),
        planned(3, 'Done.'),
        ev('USER_INPUT', 4, '', source='USER'),
        ev('USER_INPUT', 5, {'not': 'a text'}, source='USER'),
        planned(6, ''),
        planned(7, '   '),
        ev('USER_INPUT', 8, '<user_request>in small letters</user_request>', source='USER'),
        ev('USER_INPUT', 9, 'before <USER_REQUEST>the first</USER_REQUEST> between '
                            '<USER_REQUEST>the second</USER_REQUEST> after', source='USER'),
        ev('USER_INPUT', 10, '<USER_REQUEST>   </USER_REQUEST>', source='USER'),
        ev('CHECKPOINT', 11, 'a checkpoint is no turn'),
        ev('ERROR_MESSAGE', 12, 'an error is no turn', error='quota'),
        ev('CONVERSATION_HISTORY', 13, 'nor is a history'),
        planned(14, None),
        {'type': 'PLANNER_RESPONSE', 'content': 'without a step and a time'},
        ev('USER_INPUT', 15, None, source='USER'),
    ]),
    ('order', 'the steps are read in the order of their numbers, whatever order they were written in', [
        planned(3, 'third'),
        asked(0, 'first'),
        'this line is no JSON',
        '',
        planned(1, 'second'),
        ev('PLANNER_RESPONSE', '7', 'a step whose number is a text counts as the first, after what is there'),
        asked(2, 'between'),
        ev('PLANNER_RESPONSE', None, 'and so does one without a number'),
        ev('PLANNER_RESPONSE', 2.5, 'and one whose number is no whole number'),
        ev('PLANNER_RESPONSE', True, 'but true is the number one to Python'),
    ]),
    ('preamble', 'what the Host said before the user\'s words is no part of them', [
        ('preamble', 0, 'What now?', True),
        planned(1, 'This.'),
        ('preamble', 2, 'And silently?', False),
        planned(3, 'That.'),
    ]),
    ('calls', 'the tools of a step are answered by the steps after it, and ride on the next thing said', [
        asked(0, 'List and read'),
        planned(1, None, [call('run_command', CommandLine='ls -la', WaitMsBeforeAsync='500',
                               toolSummary='  Listing the files  '),
                          call('view_file', AbsolutePath='/srv/a.txt', toolSummary='   ')],
                thinking='I should look first.\n\nThen read.'),
        answer(2, ran(0, 'a.txt\nb.txt\n')),
        answer(3, 'Showing lines 1 to 3\nalpha\n'),
        planned(4, 'Two files; a.txt says alpha.'),
        asked(5, 'Now build'),
        planned(6, None, [call('run_command', CommandLine='make all')]),
        answer(7, ran(2, 'make: *** No rule to make target\n')),
        planned(8, None, [call('run_command', CommandLine='cargo build', Cwd='/srv')], thinking='Another way.'),
        answer(9, ran(-1, '')),
        planned(10, 'It fails both ways.', [call('run_command', CommandLine='git status')]),
        answer(11, ran(0, 'clean\n')),
        planned(12, 'And the tree is clean.'),
    ]),
    ('printed', 'what a command printed, in the words agy used and the words it uses now', [
        asked(0, 'Run them'),
        planned(1, None, [call('run_command', CommandLine='make one'),
                          call('run_command', CommandLine='make two'),
                          call('run_command', CommandLine='make three'),
                          call('run_command', CommandLine='make four'),
                          call('run_command', CommandLine='make five')]),
        answer(2, ran(0, 'the older words\n')),
        answer(3, ran(0, 'the newer words\n', mark='Command output')),
        answer(4, 'Command exited with code 3.\n\nCommand output:\nfirst\nOutput:\nsecond\n'),
        answer(5, 'no line of how it ended, and nothing printed'),
        ev('GENERIC', 6, 'exited with code 4 and no header\nOutput:\n' + LONG),
        planned(7, 'Ran.'),
        planned(8, None, [call('run_command', CommandLine='make six')]),
        ev('GENERIC', 9, HEAD + '\n' + 'x' * 320 + ' exited with code 9'),
        planned(10, 'A code beyond the first three hundred signs is not read.'),
    ]),
    ('background', 'a command that goes on in the background ends in a later message of the system', [
        asked(0, 'Start the servers'),
        planned(1, None, [call('run_command', CommandLine='npm run dev', IsDaemon='true'),
                          call('run_command', CommandLine='npm run api', IsDaemon='true'),
                          call('run_command', CommandLine='npm run docs', IsDaemon='true'),
                          call('run_command', CommandLine='npm run lint'),
                          call('run_command', CommandLine='npm run watch', IsDaemon='true'),
                          call('run_command', CommandLine='npm run tail', IsDaemon='true')]),
        answer(2, f'Tool is running as a background task.\nLog: {TASKS}/task-3.log\n', status='RUNNING'),
        answer(3, f'Tool is running as a background task.\nLog: {TASKS}/task-4.log\n', status='RUNNING'),
        answer(4, f'Tool is running as a background task.\nLog: {TASKS}/task-5.log\n', status='running'),
        answer(5, 'Tool is running, and names no task.\n', status='RUNNING'),
        answer(6, f'Tool is running as a background task.\nLog: {TASKS}/task-6.log\n', status='RUNNING'),
        answer(7, f'Tool is running as a background task.\nLog: {TASKS}/task-7.log\n', status='RUNNING'),
        planned(8, 'Started all six.'),
        system(9, 'Something else happened, and no task ended.'),
        system(10, f'Task {TASKS}/task-9 finished with result: nobody knows this task'),
        system(11, f'Task {TASKS}/task-3 finished with result: ' + ran(0, 'ready on 3000\n')
               + f'\nLog: {TASKS}/task-3.log'),
        system(12, f'Task {TASKS}/task-4 was canceled with result: stopped by the user'),
        system(13, f'Task {TASKS}/task-5 WAS CANCELED with result: \nLog: {TASKS}/task-5.log'),
        system(14, f'Task {TASKS}/task-6 Finished with result: ' + ran(1, 'crashed\n')),
        asked(15, 'And now?'),
        system(16, 'All background tasks have been stopped because the tool was started again.'),
        planned(17, 'They are gone.'),
        system(18, f'Task {TASKS}/task-7 finished with result: too late, it was stopped'),
    ]),
    ('unanswered', 'a call nothing answered: an error once the user spoke again, and still at work at the end', [
        asked(0, 'Wait for it'),
        planned(1, None, [call('run_command', CommandLine='sleep 100'), 'no call at all',
                          {'name': 'run_command', 'args': 'no arguments of a known kind'},
                          {'name': 'run_command'},
                          call('view_file', AbsolutePath='/srv/late.txt')]),
        answer(2, ran(0, 'the first call was answered\n')),
        ev('ERROR_MESSAGE', 5, 'the step the last call would be answered by is an error', error='cancelled'),
        asked(6, 'Never mind'),
        planned(7, 'Stopped.'),
        asked(8, 'Once more'),
        planned(9, None, [call('run_command', CommandLine='sleep 200')]),
        planned(10, 'Gave up on it.'),
        planned(11, None, [call('run_command', CommandLine='sleep 300')]),
        asked(12, 'And again'),
        planned(13, 'Text with a call that is not answered yet.', [call('run_command', CommandLine='sleep 400')]),
        planned(15, None, [call('run_command', CommandLine='sleep 500')]),
    ]),
    ('older', 'agy 1.1: every tool a step of its own kind', [
        ev('RUN_COMMAND', 0, HEAD + 'Output:\nbefore anybody spoke\n'),
        asked(1, 'Look around'),
        ev('RUN_COMMAND', 2, HEAD + 'Output:\nhello\n'),
        ev('LIST_DIRECTORY', 3, HEAD + 'a\nb\n', error='permission denied'),
        ev('VIEW_FILE', 4, None),
        ev('VIEW_CODE_ITEM', 5, ''),
        planned(6, 'Looked.'),
        ev('GREP_SEARCH', 7, HEAD + LONG),
        ev('FIND_FILES', 8, {'not': 'a text'}),
        ev('EDIT_FILE', 9, 'edited'),
        ev('WRITE_FILE', 10, 'written'),
        ev('WRITE_TO_FILE', 11, 'written to'),
        ev('SEARCH_WEB', 12, 'searched'),
        ev('READ_URL_CONTENT', 13, 'fetched'),
        asked(14, 'And the rest'),
        ev('BROWSER_ACTION', 15, HEAD + 'a kind nobody knows, with the tool\'s header'),
        ev('BROWSER_ACTION', 16, 'a kind nobody knows, without it'),
        ev('CHECKPOINT', 17, HEAD + 'a checkpoint with the header is taken for a tool'),
        ev('ERROR_MESSAGE', 18, HEAD + 'an error with the header is not', error='x'),
        ev('CONVERSATION_HISTORY', 19, HEAD + 'nor a history'),
        ev('GENERIC', 20, HEAD + 'nor an answer nobody asked for'),
        planned(21, None, [call('run_command', CommandLine='echo counted once'), call('view_file', AbsolutePath='/x')]),
        ev('RUN_COMMAND', 22, HEAD + 'Output:\ncounted once\n'),
        answer(23, 'Showing lines 1 to 1\nx\n'),
        asked(24, 'Tools after the user spoke, and nothing said'),
        ev('RUN_COMMAND', 25, HEAD + 'Output:\nalone\n'),
        planned(26, None, [call('run_command', CommandLine='echo newer')]),
        answer(27, ran(0, 'newer\n')),
    ]),
    ('thinking', 'what the model thought is shown with the step it thought it in', [
        asked(0, 'Think'),
        planned(1, 'One thought.', thinking='A single thought.'),
        planned(2, None, thinking='\n\n'.join(f'Paragraph {n}.' for n in range(1, 11))),
        planned(3, None, thinking='   '),
        planned(4, None, thinking={'not': 'a text'}),
        planned(5, None, [call('list_dir', DirectoryPath='/srv')], thinking='<speak>Aloud</speak> and ' + LONG),
        answer(6, 'a\nb\n'),
        planned(7, 'Thought, listed, and said.', thinking='Last   thought.\n\n\n\n  Spread out.  '),
        asked(8, 'Thoughts that nothing follows'),
        planned(9, None, thinking='Left over at the end.'),
    ]),
    ('every-tool', 'each tool agy has, by the name the conversation knows it by, with what it was given', [
        asked(0, 'Use them all'),
        planned(1, None, [
            call('run_command', CommandLine='cat a.txt', toolSummary='Reading a file'),
            call('run_command', CommandLine='rg needle src'),
            call('view_file', AbsolutePath='/srv/view.txt', StartLine='1', EndLine='9'),
            call('list_dir', DirectoryPath='/srv/dir'),
            call('grep_search', Query='needle', SearchPath='/srv/src'),
            call('find_by_name', Pattern='*.rs', SearchDirectory='/srv'),
            call('write_to_file', TargetFile='/srv/new.txt', CodeContent='x', Overwrite='true'),
            call('replace_file_content', TargetFile='/srv/old.txt', TargetContent='a', ReplacementContent='b'),
            call('multi_replace_file_content', TargetFile='/srv/many.txt'),
            call('search_web', query='rust edition 2024', domain='rust-lang.org'),
            call('read_url_content', Url='https://example.test/page'),
            call('generate_image', Prompt='a heron', ImageName='heron'),
            call('read_file', Path='/srv/read.txt', StartLine='1'),
            call('edit_file', Path='/srv/edit.txt', Target='a', Replacement='b'),
            call('write_file', Path='/srv/write.txt', Content='x'),
            call('manage_task', Action='list'),
            call('', Anything='a call without a name'),
        ]),
        *[answer(2 + n, body) for n, body in enumerate([
            ran(0, 'alpha\n'), ran(1, ''), 'Showing lines 1 to 9\nx\n', 'a\nb\n', 'The search for needle found 2\n',
            'Found 3 files\n', 'Created file /srv/new.txt\n', 'Made 1 replacement\n', 'Made 4 replacements\n',
            'Results for rust edition 2024\n', 'Fetched https://example.test/page\n', 'The tool made an image\n',
            'Showing lines 1 to 1\nx\n', 'Made 1 replacement\n', 'Created file /srv/write.txt\n', 'Task list: none\n',
            'A tool without a name ran\n'])],
        planned(19, 'All seventeen.'),
    ]),
    ('given', 'what a tool was given is cut, and what looks like a secret is not shown', [
        asked(0, 'Careful'),
        planned(1, None, [
            call('run_command', CommandLine='curl -H "Authorization: Bearer abc123" https://example.test',
                 description='TOKEN=xyz in a description ' + 'd' * 200),
            call('run_command', command='password = hunter2 --api-key:k1 --api_key=k2 secret:s3 passwd=p4'),
            call('run_command', CommandLine='echo ' + 'c' * 300),
            call('view_file', file_path='/first', FilePath='/second', AbsolutePath='/third'),
            call('view_file', path='/srv/' + 'p' * 300),
            call('grep_search', pattern='p' * 200, path='/srv/' + 'q' * 300),
            call('search_web', Query='q' * 200, Url='https://example.test/' + 'u' * 300),
            {'name': 'run_command', 'args': {'CommandLine': 'not written as JSON', 'toolSummary': '"s' * 150}},
            {'name': 'run_command', 'args': {'CommandLine': '"cut short', 'toolSummary': 7}},
            {'name': 'view_file', 'args': {'AbsolutePath': 12, 'StartLine': 3}},
        ]),
        *[answer(2 + n, ran(0, 'ok\n')) for n in range(10)],
        planned(12, 'Nothing leaked.'),
    ]),
    ('explored', 'looking around is one thing, however many steps it took; what failed stands by itself', [
        asked(0, 'Explore'),
        planned(1, None, [call('view_file', AbsolutePath='/srv/one.txt'),
                          call('run_command', CommandLine='cat two.txt'),
                          call('list_dir', DirectoryPath='/srv')], thinking='Look first.'),
        answer(2, 'Showing lines 1 to 1\n1\n'),
        answer(3, ran(0, '2\n')),
        answer(4, 'a\n'),
        planned(5, None, [call('run_command', CommandLine='cat missing.txt'),
                          call('grep_search', Query='x', SearchPath='/srv'),
                          call('run_command', CommandLine='make all'),
                          call('find_by_name', Pattern='*.md', SearchDirectory='/srv'),
                          call('view_file', AbsolutePath='/srv/running.txt')]),
        answer(6, ran(1, 'cat: missing.txt: No such file\n')),
        answer(7, 'The search for x found 0\n'),
        answer(8, ran(0, 'built\n')),
        answer(9, 'Found 1 file\n'),
        planned(11, 'Explored.'),
    ]),
    ('nothing', 'an empty file', []),
]

# The names agy 1.5 gives the three tools the Python Host knows under their older names only.
MENDED_NAMES = {'read_file': 'Read', 'write_file': 'Write', 'edit_file': 'Edit'}


@tool_scratch.case('clarp-agy-history-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: the turns the Python Host reads from the transcripts above,
    as it is and with the two things mended."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agy_transcript, message_writes
    from lib.backend import agy
    from lib.text_util import truncate
    from lib.voice_preamble import apply_voice_preamble

    def line_of(line):
        if isinstance(line, tuple):
            _, step, text, voice = line
            told = apply_voice_preamble(text, voice=voice, persona='Gemma', session='gemma')
            return json.dumps(asked(step, told))
        return line if isinstance(line, str) else json.dumps(line)

    def mended():
        """The same function with the two things mended. Answers how to put it back."""
        names, given, settle = dict(agy._TOOL_NAMES), agy._canonical_tool_input, agy_transcript._settle
        agy._TOOL_NAMES.update(MENDED_NAMES)

        def canonical(name, value):
            raw = value if isinstance(value, dict) else {}
            if name in {'Read', 'Write', 'Edit'} and not (
                    raw.get('file_path') or raw.get('FilePath') or raw.get('AbsolutePath')
                    or raw.get('TargetFile') or raw.get('path')):
                raw = {**raw, 'path': raw.get('Path')}
            return given(name, raw)

        def settled(tool, content, status=''):
            # `_settle` as it is, but for where it cuts: at whichever of the two marks comes
            # first. (A copy: when that function changes, this one is looked at again.)
            body = agy_transcript._RESULT_HEADER_RE.sub('', content or '', count=1)
            code = agy_transcript._EXIT_RE.search(body[:300])
            tool['exit_code'] = int(code.group(1)) if code else None
            cuts = [(body.find(mark), len(mark)) for mark in ('Output:\n', 'Command output:\n') if mark in body]
            if cuts:
                at, size = min(cuts)
                body = body[at + size:]
            tool['result'] = truncate(body.strip(), 300)
            tool['_output'] = body
            if status.upper() == 'RUNNING':
                tool['status'] = 'running'
            else:
                tool['status'] = 'error' if tool['exit_code'] not in (None, 0) else 'ok'

        agy._canonical_tool_input, agy_transcript._settle = canonical, settled

        def back():
            agy._TOOL_NAMES.clear()
            agy._TOOL_NAMES.update(names)
            agy._canonical_tool_input, agy_transcript._settle = given, settle
        return back

    out = []
    for name, what, lines in TRANSCRIPTS:
        written = [line_of(line) for line in lines]
        path = root / f'{name}.jsonl'
        path.write_text('\n'.join(written) + ('\n' if written else ''))
        turns = agy_transcript.parse_turns(path)
        back = mended()
        try:
            again = agy_transcript.parse_turns(path)
        finally:
            back()
        assert agy_transcript.parse_turns(path) == turns, 'the function was not put back as it was'
        row = {'name': name, 'what': what, 'lines': written, 'turns': turns,
               'phases': message_writes._row_phases(again)}
        if again != turns:
            row['turns_mended'] = again
        out.append(row)
    return {'transcripts': out}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran_it = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                            env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran_it.returncode != 0:
        print(ran_it.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran_it.stdout.splitlines()[-1])
    out = ['{', ' "transcripts": [']
    rows = said['transcripts']
    for number, row in enumerate(rows):
        out.append('  {"name": ' + json.dumps(row['name']) + ', "what": ' + json.dumps(row['what'])
                   + ', "phases": ' + json.dumps(row['phases']) + ',')
        out.append('   "lines": ' + json.dumps(row['lines'], ensure_ascii=True) + ',')
        for key in ('turns', 'turns_mended'):
            if key not in row:
                continue
            last = key == 'turns_mended' or 'turns_mended' not in row
            out.append(f'   "{key}": [')
            out.extend('    ' + json.dumps(turn, ensure_ascii=True) + (',' if n < len(row[key]) - 1 else '')
                       for n, turn in enumerate(row[key]))
            out.append('   ]' + ('' if last else ','))
        out.append('  }' + (',' if number < len(rows) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    json.loads(OUT.read_text())
    print(f'{OUT.relative_to(REPO)}: ' + '; '.join(
        f'{row["name"]}: {len(row["lines"])} lines, {len(row["turns"])} turns, '
        f'{sum(len(turn.get("tools") or []) for turn in row["turns"])} tools, '
        f'{sum(len(turn.get("display_cells") or []) for turn in row["turns"])} cells'
        f'{", mended" if "turns_mended" in row else ""}' for row in rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
