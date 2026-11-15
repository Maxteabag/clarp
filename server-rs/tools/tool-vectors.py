#!/usr/bin/env python3
"""Writes the vectors the Rust Host's words for a tool call are tested against.

While an agent runs a tool, the apps show what it is doing: "Running npm test", "Reading
src/main.rs". The Python Host makes those words in `server/lib/live_tools.py`
(`classify_tool`: a tool's category, a short label, the command it runs),
`server/lib/live_core.py` (`tool_headline`) and, for the event of a tool state,
`server/lib/activity.py` (`summarize_tool_activity`). This calls them for many tool calls and writes
what they answered to `crates/clarp-kernel/tests/fixtures/tool-vectors.json`.

    python3 server-rs/tools/tool-vectors.py

Nothing of a Host is touched or started: two pure functions are called. Every text is
synthetic.
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'tool-vectors.json'

COMMANDS = [
    'npm test', 'cargo build --release', '  make   test  ', 'pytest -q tests/unit', 'python3 script.py --flag',
    "bash -lc 'npm test'", "/bin/bash -lc 'cargo test'", "zsh -lc 'ls -la src'", "sh -lc 'cat README.md'",
    "bash -lc 'echo '\"'\"'quoted'\"'\"' && ls'", 'bash -c "npm test"', 'bash -lc npm', 'bash -lc', 'bash',
    'rg foo', 'rg -n "needle in hay" src/', 'rg --files', 'rg --files crates/clarp-host', 'rg -l TODO .',
    'grep -rn pattern server/lib', 'grep pattern', 'grep', 'git grep -n turn_started_at', 'git ls-files',
    'git ls-files server-rs', 'git status', 'git', 'git log --oneline -3',
    'ls', 'ls -la', 'ls -la /var/tmp', 'find . -name "*.rs"', 'find', 'fd goals crates', 'fd',
    'cat README.md', 'cat', 'cat a.txt b.txt a.txt', 'sed -n 1,40p server/lib/artifacts.py', 'nl -ba src/main.rs',
    'head -20 Cargo.toml', 'tail -f /var/log/syslog', 'tail', 'head -c 100',
    'cd server-rs && rg wake crates', 'pwd && ls -la', 'cd x; cat a.rs; cat b.rs', 'cat a.rs && cat b.rs && cat a.rs',
    'true; pwd', 'pwd', 'cat a.rs && npm test', 'npm test && cat a.rs', 'cd /tmp && true && pwd',
    'echo "unbalanced', "echo 'unbalanced", 'echo trailing\\', 'echo a\\ b c', 'echo "a \\"b\\" c"',
    "cat 'file with spaces.txt'", 'cat "quoted path/file.rs"', 'cat ./relative', 'cat .hidden', 'cat noext',
    'rg "a b" "c d/e.f"', 'grep -e x -e y file.txt', 'ls -la -- -odd', 'find /a/b -type f',
    'python3 - <<EOF\nprint(1)\nEOF', 'npm run build\nnpm test', '\n\n  second line first\nthird',
    'x' * 130, 'npm test ' + 'y' * 200, 'cat ' + 'z/' * 80 + 'file.rs', 'rg ' + 'q' * 150,
    '', '   ', 'ünïcödé --flag', 'cat ünï/cödé.txt', 'ls\ttabbed', 'rg\tfoo\tsrc',
    '/usr/bin/rg foo', './node_modules/.bin/jest', 'sudo rg foo', 'env X=1 npm test', 'FOO=bar cat x.txt',
    'cat x.txt | wc -l', 'rg foo | head', 'ls > out.txt', 'cat a.rs;', ';cat a.rs', '&& cat a.rs',
    "bash -lc 'cd server-rs && rg wake crates'", "bash -lc 'cat a.rs; cat b.rs'", "bash -lc ''",
    "bash  -lc  'npm test'", "bash -lc 'npm test' extra", 'bash -lc "rg foo src"',
]
PATHS = ['/home/user/project/src/main.rs', 'src/main.rs', 'main.rs', '/', '', 'a/b/c/d/e.txt', 'C:\\Users\\x\\file.txt',
         'dir/', '/var/tmp/', 'ünï/cödé/file.rs', 'p' * 300, 'a//b///c.rs', '  spaced/path.rs  ']
NAMES = [
    'Bash', 'bash', 'shell', 'exec_command', 'local_shell', 'run_terminal_command', 'run_command',
    'command_execution', 'terminal',
    'Read', 'NotebookRead', 'view', 'read_file', 'Grep', 'Glob', 'search', 'codebase_search', 'LS', 'list',
    'list_dir', 'Edit', 'MultiEdit', 'NotebookEdit', 'apply_patch', 'patch', 'str_replace', 'Write', 'create',
    'write_file', 'WebFetch', 'WebSearch', 'web_search', 'fetch', 'web_search_call', 'TodoWrite', 'update_plan',
    'TodoRead', 'Task', 'Agent', 'file_change', 'patch_apply', 'mcp_tool_call', 'image_view', 'image_generation',
    'dynamic_tool_call', 'collab_tool_call', 'collab_agent_tool_call',
    'mcp__github__create_issue', 'mcp__server', 'MCP__Upper__Tool', 'mcp:server:tool', 'mcp__',
    'Unknown', 'some.tool', 'server.tool', '', '   ', 'Tool With Spaces', 'path/like/name', 'rg foo src', 'cat a.rs',
    'npm test', 'ExitPlanMode', 'KillShell', 'BashOutput', 'u' * 150,
]


def cases() -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for command in COMMANDS:
        out.append(('Bash', {'command': command}))
    for command in COMMANDS[::4]:
        out.append(('shell', {'cmd': command}))
        out.append(('Unknown', {'command': command}))      # a command under a name nobody knows
        out.append(('Read', {'command': command}))         # a command under a name that is known
        out.append((command, {}))                           # Codex: the command line as the tool's name
    out.append(('shell', {'command': ['bash', '-lc', 'rg foo src']}))
    out.append(('shell', {'command': ['npm', 'test']}))
    out.append(('shell', {'cmd': ['cat', 'a.rs']}))
    out.append(('Bash', {'command': 12}))
    out.append(('Bash', {'command': None}))
    out.append(('Bash', {}))
    out.append(('Bash', None))
    out.append(('Bash', 'not a dict'))
    for name in NAMES:
        out.append((name, {}))
        out.append((name, None))
    for name in ('Read', 'Edit', 'Write', 'LS', 'Glob', 'Grep', 'TodoWrite', 'Task', 'WebFetch', 'image_view',
                 'mcp_tool_call', 'Unknown'):
        for path in PATHS:
            for key in ('file_path', 'filePath', 'path', 'notebook_path'):
                out.append((name, {key: path}))
    for name in ('Grep', 'Glob', 'search', 'codebase_search'):
        out.append((name, {'pattern': 'needle'}))
        out.append((name, {'query': 'what is asked'}))
        out.append((name, {'pattern': 'needle', 'path': 'src/lib'}))
        out.append((name, {'path': 'src/lib'}))
        out.append((name, {'pattern': 'n' * 200}))
        out.append((name, {'pattern': '  spaced  '}))
        out.append((name, {'pattern': ''}))
    for name in ('WebFetch', 'WebSearch', 'web_search', 'fetch'):
        out.append((name, {'url': 'https://example.org/a/b?c=d'}))
        out.append((name, {'query': 'rust shlex split'}))
        out.append((name, {'url': 'https://example.org/' + 'x' * 200}))
        out.append((name, {'url': '', 'query': 'fallback'}))
    for name in ('Task', 'Agent', 'collab_tool_call'):
        out.append((name, {'description': 'Look into the build'}))
        out.append((name, {'description': 'd' * 200}))
        out.append((name, {'description': ''}))
        out.append((name, {'prompt': 'no description'}))
    for name in ('TodoWrite', 'update_plan'):
        out.append((name, {'todos': [{'content': 'x'}]}))
    out.append(('Read', {'file_path': 12}))
    out.append(('Read', {'file_path': None, 'path': 'second/choice.rs'}))
    out.append(('Edit', {'file_path': 'a/b.rs', 'command': 'ignored for a known name'}))
    # What the older summary of a tool call reads besides.
    out.append(('Bash', {'command': 'npm test', 'description': 'Run the tests'}))
    out.append(('Bash', {'command': 'c' * 300, 'description': '  '}))
    out.append(('bash', {'description': 'd' * 300}))
    out.append(('Bash', {'command': 0}))
    for edits in ([], [{'old_string': 'a'}], [1, 2, 3], 'not a list'):
        out.append(('MultiEdit', {'file_path': 'src/lib.rs', 'edits': edits}))
        out.append(('MultiEdit', {'edits': edits}))
    for name in ('Glob', 'glob_search', 'Grep', 'grep_search'):
        out.append((name, {'pattern': '**/*.rs', 'path': '/home/user/project'}))
        out.append((name, {'glob': '*.py'}))
        out.append((name, {'path': 'only/a/path'}))
        out.append((name, {}))
        out.append((name, {'pattern': 'p' * 200, 'path': 'x'}))
    for todos in ([], [{'content': 'First', 'status': 'completed'}, {'content': 'Second', 'status': 'in_progress'}],
                  [{'content': 'Only', 'status': 'pending'}], [{'status': 'in_progress'}], ['not a dict'], 'nope',
                  [{'content': 't' * 300, 'status': 'in_progress'}]):
        out.append(('TodoWrite', {'todos': todos}))
    for name in ('WebSearch', 'web_search', 'WebFetch', 'web_fetch', 'edit_file', 'read_file', 'write_file'):
        out.append((name, {'query': 'what to look for', 'url': 'https://example.org/page', 'file_path': 'a/b/c.txt'}))
        out.append((name, {}))
    # A tool nobody knows is summed up by the first plain value it was given, in the order given.
    out.append(('mcp__notes__add', {'title': 'Zeta first', 'body': 'Alpha second'}))
    out.append(('mcp__notes__add', {'tags': ['a'], 'meta': {'k': 1}, 'zulu': 'after the containers', 'alpha': 'later'}))
    out.append(('Custom', {'count': 3, 'name': 'x'}))
    out.append(('Custom', {'flag': True}))
    out.append(('Custom', {'nothing': None, 'then': 'something'}))
    out.append(('Custom', {'empty': '', 'then': 'something'}))
    out.append(('Custom', {'only': ['containers']}))
    out.append(('Custom', {'long': 'l' * 300}))
    out.append(('', {'x': 'unnamed tool'}))
    return out


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib.activity import summarize_tool_activity
    from lib.live_core import tool_headline
    from lib.live_tools import classify_tool
    vectors = []
    for name, tool_input in cases():
        category, label, command = classify_tool(name, tool_input)
        # The older summary, which the event of a tool state carries: an action, a summary, a path.
        activity = summarize_tool_activity(name, tool_input if isinstance(tool_input, dict) else {})
        vectors.append({'name': name, 'input': tool_input, 'category': category, 'label': label,
                        'command': command, 'headline': tool_headline(name, category, label),
                        'activity': activity})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'about': 'Written by server-rs/tools/tool-vectors.py from server/lib/live_tools.py and live_core.py. '
                 'Do not edit by hand.',
        'vectors': vectors}, ensure_ascii=False, indent=1) + '\n')
    kinds: dict[str, int] = {}
    for vector in vectors:
        kinds[vector['category']] = kinds.get(vector['category'], 0) + 1
    print(f'{OUT.relative_to(REPO)}: {len(vectors)} vectors, {OUT.stat().st_size} bytes; by category {kinds}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
