#!/usr/bin/env python3
"""What a tool's use is, and what is said of it without asking a model, as the Python Host reads
it for the plain words under a tool's row (`server/lib/tool_explanations.py`, `normalize_activity`:
the bounded fields that are read of an activity, with what a heredoc only feeds withheld; and
`server/lib/tool_explanation_templates.py`, `lookup`: the scripted template that fits, with what
it says at each of the five levels of detail, or why none fits): what its own functions make of
made-up uses of tools, written to `crates/clarp-kernel/tests/fixtures/tool-explanation-vectors.json`
for the Rust Host's reading to be held to, use by use.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network. Nothing that is described is run: the commands are texts.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/tool-explanation-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'tool-explanation-vectors.json'
# The key a Host signs the shape of an unknown program's or tool's use with (`_identity`): each Host
# draws its own. The stories are told under this one, so that they can be told again.
KEY = 'synthetic-identity-key-' + '0123456789abcdef' * 2


def sh(command, **more):
    return {'name': 'Bash', 'command': command, **more}


HEREDOC = "python3 - <<'PY'\nimport json\nprint(json.dumps({'a': 1}))\nPY"
WRITE = "cat > notes.txt <<'EOF'\nline one\nline two\nEOF"

# The uses of tools: what an app or a turn would name. Shell commands first, by kind.
SHELL = [
    # looking
    'ls', 'ls -la', 'ls -la ~/GIT/clarp', 'ls src tests', 'ls -1 /var/tmp/x | head -5', 'ls --color=never docs/',
    'tree -L 2', 'tree -L 2 server-rs', 'pwd', 'cd server-rs && ls', 'cd /srv/app; ls -la',
    # reading
    'cat README.md', 'cat a.txt b.txt', 'head -20 src/main.rs', 'head -n 5 notes.md', 'tail -f /var/log/syslog',
    'tail -n 100 server.log', 'nl -ba src/lib.rs', 'less CHANGELOG.md', 'bat Cargo.toml', "sed -n '10,40p' src/main.rs",
    'sed -n 1,20p docs/guide.md', 'wc -l src/*.rs', 'wc -c big.bin', 'file image.png', 'stat Cargo.lock',
    # searching
    'rg TODO', 'rg -n "fn main" src', "rg -l 'struct App' crates", 'grep -rn "unsafe" .', 'grep -c error log.txt',
    'egrep "a|b" file.txt', 'fgrep needle haystack.txt', 'ag pattern lib', 'rg --files | rg test', 'rg -n TODO -g "*.rs" src tests',
    'find . -name "*.rs"', 'find src -type f -name "*.py" | head', 'find / -name passwd 2>/dev/null', 'fd Cargo', 'locate bashrc',
    'which cargo', 'command -v node', 'type python3',
    # git
    'git status', 'git status --short', 'git log --oneline -5', 'git log -p src/main.rs', 'git diff', 'git diff --stat HEAD~1',
    'git show HEAD', 'git branch -a', 'git remote -v', 'git blame src/lib.rs', 'git stash list', 'git add -A', 'git commit -m "fix it"',
    'git push origin main', 'git pull --rebase', 'git checkout -b feature', 'git reset --hard HEAD~1', 'git -C /srv/app status',
    'git fetch origin && git rebase origin/main',
    # running and building
    'cargo test', 'cargo build --release', 'cargo clippy -- -D warnings', 'npm test', 'npm install', 'npm run build', 'make test-critical',
    'pytest tests/unit -q', 'python3 -m pytest -x', 'python3 script.py --flag', 'python script.py', 'node index.js', 'bash run.sh',
    './run.sh --fast', 'sh -c "echo hi"', 'go test ./...', 'uv run pytest', 'docker ps', 'docker compose up -d', 'kubectl get pods',
    # changing
    'rm notes.txt', 'rm -rf build', 'mv a.txt b.txt', 'cp -r src dst', 'mkdir -p out/dir', 'touch new.txt', 'chmod +x run.sh',
    'ln -s a b', 'truncate -s 0 log.txt', 'kill 1234', 'pkill -f server', "sed -i 's/a/b/' file.txt", 'echo hi > out.txt',
    'echo more >> out.txt', 'tee out.txt', 'git mv a b',
    # privileged, network, uploads
    'sudo ls /root', 'sudo systemctl restart nginx', 'doas cat /etc/shadow', 'curl https://example.org', 'curl -s https://example.org/api | jq .',
    'curl -X POST -d @body.json https://example.org/upload', 'wget https://example.org/file.tgz', 'scp file host:/tmp/', 'ssh host uptime',
    'rsync -a src/ host:dst/', 'gh pr list', 'gh run view 123 --json status',
    # compound and wrapped
    'ls && pwd', 'ls; pwd', 'ls || true', 'cat a.txt | wc -l', 'rg TODO | head -3', 'cd src && rg TODO', 'true && cat README.md',
    '(ls)', '{ ls; }', 'ls &', 'FOO=bar ls', 'env FOO=bar ls -la', 'time ls', 'nice ls', 'timeout 5 ls', 'nohup ./run.sh &',
    '/usr/bin/bash -lc "ls -la"', "/usr/bin/bash -lc 'rg TODO src'", 'bash -lc "cd /srv && git status"', "zsh -c 'cat README.md'",
    'xargs rm < list.txt', 'ls $(pwd)', 'ls `pwd`', 'cat "$FILE"', 'cat ~/notes.txt', 'cat ./a\\ b.txt', "cat 'a b.txt'",
    'ls -la -- -odd-name', 'cat', 'ls -', 'rg', 'git', 'unknownprog --flag value', 'myscript', 'jq .name package.json', 'date', 'whoami',
    'echo hello world', 'printf "%s\\n" x', 'sleep 5', 'true', 'exit 1', '', '   ', 'ls "unterminated', "cat 'unterminated",
    HEREDOC, WRITE, "cat <<EOF\nshown\nEOF", "bash <<'SH'\nls\nSH", "psql <<SQL\nselect 1;\nSQL", 'cat <<< "here string"',
    'ls ' + 'a' * 200, 'cat ' + '/very/long/path/' * 20 + 'file.txt',
]

# Named tools and the other shapes an activity comes in.
OTHERS = [
    {'name': 'Read', 'file_path': '/srv/app/src/main.rs'}, {'name': 'Read', 'input': {'file_path': 'README.md'}}, {'name': 'Read'},
    {'name': 'read', 'path': 'docs/guide.md'}, {'name': 'Edit', 'file_path': 'src/lib.rs'}, {'name': 'MultiEdit', 'input': {'file_path': 'a.py'}},
    {'name': 'NotebookEdit', 'file_path': 'nb.ipynb'}, {'name': 'Edit'}, {'name': 'Write', 'file_path': 'out/new.txt'}, {'name': 'Write'},
    {'name': 'Grep', 'pattern': 'fn main', 'path': 'src'}, {'name': 'Grep', 'input': {'pattern': 'TODO'}}, {'name': 'Grep'},
    {'name': 'Glob', 'pattern': '**/*.rs'}, {'name': 'Glob', 'input': {'pattern': '*.md', 'path': 'docs'}}, {'name': 'Glob'},
    {'name': 'LS', 'path': '/srv/app'}, {'name': 'LS'}, {'name': 'WebSearch', 'input': {'query': 'rust axum websocket'}}, {'name': 'WebSearch'},
    {'name': 'WebFetch', 'input': {'url': 'https://example.org/page', 'prompt': 'Summarize'}}, {'name': 'Task', 'input': {'description': 'Explore the repo'}},
    {'name': 'TodoWrite', 'input': {'todos': []}}, {'name': 'mcp__files__search', 'input': {'query': 'notes', 'path': '/srv'}},
    {'name': 'mcp__files__search', 'input': {'query': 'other', 'path': '/srv'}}, {'name': 'Bash'}, {'name': 'Bash', 'input': {'command': 'ls -la'}},
    {'name': 'Bash', 'input': {'cmd': 'cat README.md'}}, {'name': 'shell', 'command': 'git status'}, {'name': 'exec_command', 'input': {'cmd': 'rg TODO'}},
    {'name': 'local_shell', 'command': 'ls'}, {'kind': 'command', 'summary': 'ls -la src'}, {'kind': 'command', 'summary': ''},
    {'kind': 'command', 'name': 'Shell', 'summary': HEREDOC}, {'name': '/usr/bin/bash -lc "ls -la"'}, {'name': "/usr/bin/bash -lc 'rg TODO src'"},
    {'name': '/usr/bin/bash -lc "' + 'x' * 70}, {'name': 'bash -lc ls'},
    {'kind': 'exploration', 'operations': ['Read: src/main.rs']}, {'kind': 'exploration', 'operations': ['Read: src/main.rs', 'Read: src/main.rs']},
    {'kind': 'exploration', 'operations': ['Read: a.rs', 'Read: b.rs']}, {'kind': 'exploration', 'operations': ['List: src']},
    {'kind': 'exploration', 'operations': ['Search: TODO in src']}, {'kind': 'exploration', 'operations': ['Read: notes']},
    {'kind': 'exploration', 'operations': ['Read: a b.txt']}, {'kind': 'exploration', 'operations': []}, {'kind': 'exploration'},
    {'kind': 'patch', 'operations': ['Edit: src/lib.rs']}, {'kind': 'patch', 'operations': ['Add: new.txt']}, {'kind': 'patch', 'operations': ['Delete: old.txt']},
    {'kind': 'patch', 'operations': ['Edit: a.rs', 'Edit: b.rs']}, {'kind': 'patch', 'operations': ['Move: a -> b']}, {'kind': 'patch'},
    {'kind': 'reasoning', 'title': 'Reasoned'}, {'name': 'Unknown', 'title': 'A title', 'description': 'what it does'}, {},
    {'name': '   '}, {'name': 'Read', 'file_path': '   '}, {'name': 'Read', 'file_path': 7}, {'name': 'Bash', 'command': ['ls']},
    {'name': 'Read', 'file_path': 'sk-abcdefghijklmnop1234', 'description': 'token=supersecretvalue1234 in a text'},
    {'name': 'Bash', 'command': 'curl -H "Authorization: Bearer abcdefghijklmnopqrstuvwxyz" https://example.org'},
    {'name': 'Read', 'file_path': 'x' * 2000}, {'name': 'Bash', 'command': 'echo ' + 'y' * 3000},
    {'name': 'Grep', 'input': 'a text for an input'}, {'name': 'Tool', 'operations': ['one', 2, '', 'three', 'four', 'five', 'six', 'seven', 'eight']},
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host makes of the uses above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-tool-explanation-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import settings_store
    from lib import tool_explanation_templates as templates
    from lib import tool_explanations

    settings_store.set_text(templates._IDENTITY_KEY, KEY)

    uses = [sh(command) for command in SHELL] + OTHERS + [case['activity'] for case in templates.REGRESSION]
    out = []
    for use in uses:
        row = {'use': use}
        try:
            read = tool_explanations.normalize_activity(json.loads(json.dumps(use)))
        except ValueError as refused:
            row['refused'] = str(refused)
            out.append(row)
            continue
        row['read'] = read
        said = []
        route = None
        for level in range(5):
            text, found = templates.lookup(read, level)
            said.append(text)
            route = dict(found)
        row['route'] = route
        row['said'] = said
        out.append(row)
    return {'version': templates.VERSION, 'clip': templates.LABEL_CLIP, 'identity_key': KEY, 'uses': out}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    rows = said['uses']
    out = ['{', f' "version": {json.dumps(said["version"])}, "clip": {json.dumps(said["clip"])}, '
                f'"identity_key": {json.dumps(said["identity_key"])},', ' "uses": [']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(rows) - 1 else '') for n, row in enumerate(rows))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    import collections
    templates_used = collections.Counter(row['route'].get('template_id') for row in rows if 'route' in row and row['route'].get('template_id'))
    reasons = collections.Counter(row['route'].get('reason') for row in rows if 'route' in row and not row['route'].get('template_id'))
    print(f'{OUT.relative_to(REPO)}: {len(rows)} uses; a template fits {sum(templates_used.values())} '
          f'({len(templates_used)} templates), none fits {sum(reasons.values())}, refused {sum("refused" in row for row in rows)}')
    print('  templates:', dict(templates_used.most_common()))
    print('  reasons:', dict(reasons.most_common()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
