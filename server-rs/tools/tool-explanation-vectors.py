#!/usr/bin/env python3
"""What a tool's use is, and what is said of it without asking a model, as the Python Host reads
it for the plain words under a tool's row (`server/lib/tool_explanations.py`, `normalize_activity`:
the bounded fields that are read of an activity, with what a heredoc only feeds withheld; and
`server/lib/tool_explanation_templates.py`, `lookup`: the scripted template that fits, with what
it says at each of the five levels of detail, or why none fits): what its own functions make of
made-up uses of tools, written to `crates/clarp-kernel/tests/fixtures/tool-explanation-vectors.json`
for the Rust Host's reading to be held to, use by use. The library of templates itself
(`server/lib/tool_explanation_templates.json`) is copied beside the Rust that reads it
(`crates/clarp-kernel/src/explain-templates.json`), byte for byte.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network. Nothing that is described is run: the commands are texts.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/tool-explanation-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'tool-explanation-vectors.json'
LIBRARY = REPO / 'server' / 'lib' / 'tool_explanation_templates.json'
LIBRARY_COPY = HERE.parent / 'crates' / 'clarp-kernel' / 'src' / 'explain-templates.json'
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


# More uses, by the branch of the reading each one takes: what the first set did not reach.
MORE_SHELL = [
    # what only changes how a command runs
    'timeout -s KILL 5 ls -la', 'timeout --signal=KILL 10s cat a.txt', 'timeout ls', 'nice -n 5 rg TODO src', 'nice -5 ls',
    'nice --adjustment 5 ls', 'ionice -c 3 ls', 'stdbuf -oL cat log.txt', 'env -u HOME ls', 'env -i ls', 'env', 'env -- ls', 'FOO=bar',
    'time -p ls', 'nohup ls', 'sudo -u www ls', 'A=1 B="two words" ls src', 'timeout 5 env FOO=1 nice ls', 'timeout -x! 5 ls',
    'timeout 1.5m tail -n 3 log.txt', 'timeout 5', '/usr/bin/env ls', '/usr/bin/time -f %e ls',
    # where a command ends
    'ls 2>&1', 'ls 2>/dev/null', 'ls -la 2>&1 | tail -n 20', 'cat a.txt | tail -3', 'ls | head -n 5', 'ls | wc -l', 'ls 2> err.txt',
    'ls>out', 'echo "a && b"', "echo 'it''s'", 'ls \\; pwd', 'cd "my dir" && ls', 'cd a b && ls', 'cd src && ls && pwd',
    'cd src && ls | head -3', 'ls\npwd', 'ls # a comment', 'ls #', 'echo a#b', '# only a comment', 'ls -la # list; rm -rf /',
    'cat a\\', 'ls x2>&1', 'ls | tail\x1c-n\x1c5', 'cd src &&', '&& ls', 'cd && ls', "cd 'a' && cat 'b c.txt'",
    # heredocs
    "cat >> log.txt <<EOF\nline\nEOF", "tee out.txt <<EOF\nx\nEOF", "tee -a out.txt <<EOF\nx\nEOF", "sudo tee /etc/x.conf <<EOF\nx\nEOF",
    "python3.12 - <<'PY'\nprint(1)\nPY", "python3 -c 'x' <<EOF\ny\nEOF", "python3 -u - <<EOF\nprint(1)\nEOF", "bash -s <<EOF\nls\nEOF",
    "node <<EOF\n1\nEOF", "python3 script.py <<EOF\ninput\nEOF", "python3 - <<EOF\nprint('$(date)')\nEOF",
    "python3 - <<\\EOF\nprint('$(date)')\nEOF", "cat > a.txt <<-EOF\n\tindented\n\tEOF", 'cat > a.txt <<"END"\nbody\nEND',
    "cat > a.txt <<EOF\nbody\n", "cat > a.txt <<EOF\none\nEOF\ncat > b.txt <<EOF\ntwo\nEOF", "cd /srv && cat > a.txt <<EOF\nbody\nEOF",
    "cat > a.txt 2>/dev/null <<EOF\nx\nEOF", "cat > 'my file.txt' <<EOF\nx\nEOF", "FOO=1 python3 - <<EOF\nprint(1)\nEOF",
    "cat <<EOF > out.txt\nbody\nEOF", "ssh host <<EOF\nls\nEOF", "cat > a.txt <<EOF\nwith `ticks`\nEOF", "tee a b <<EOF\nx\nEOF",
    "cat >out.txt <<EOF\nx\nEOF", "timeout 5 python3 - <<EOF\nprint(1)\nEOF", "cat > a.txt <<EOF\nx\nEOF\n", "cat > a.txt << EOF\nx\nEOF",
    # searching
    'rg -e TODO src', 'rg -e TODO', 'rg --regexp "a b" src tests', 'rg -f patterns.txt src', 'grep --file=p.txt x', 'rg --files src',
    'rg --files a b', 'cd lib && rg --files', 'rg -g dir -e -g', 'rg -n -C 3 "fn main" src/main.rs', 'grep -rn --include="*.py" TODO .',
    'cd src && rg TODO', 'cd src && rg -e TODO', 'ack -i word', 'grep', 'rg -e', 'rg TODO a b c',
    # finding
    'find . -name "*.rs" -delete', 'find . -type f -exec rm {} \\;', 'find -name x', 'cd src && find -iname "*.TXT"',
    'find src tests -name x', 'fd -x rm', 'fd pattern src extra', 'fd -e rs', 'fdfind foo bar', 'fd', 'cd src && fd',
    'find . -path "./a/*" -type d', 'find',
    # removing and making
    'rm -r dir', 'rm -R dir', 'rm --recursive dir', 'rm -f a b', 'rm', 'rmdir empty', 'unlink file', 'rm -fr build/ dist/',
    'rm --force x', 'rm -i -- -r', 'rmdir -r x', 'mkdir a b', 'mkdir', 'mkdir -m 700 secret',
    # fetching
    'curl https://a.example.org https://b.example.org', 'curl -X GET https://example.org/x', 'curl -X DELETE https://example.org/x',
    'curl --request head https://example.org', 'curl -I https://Example.ORG/path', 'curl https://user:pw@example.org:8443/x',
    'curl http://localhost:8080/health', 'curl https://[::1]:8443/', 'wget -q https://example.org/a.tgz', 'curl example.org',
    "curl -X '' https://example.org", 'curl --json {} https://example.org', 'curl -o out.html https://example.org',
    'curl https://example.org/?q=a#frag', 'curl -X', 'wget',
    # git
    'git push', 'git push origin', 'git push --force origin main', 'git push origin main:prod', 'git push origin +main',
    'git push a b c', 'git push -u origin feature', 'git pull origin main', 'git pull', 'git grep TODO', 'git grep -n TODO src',
    'git grep a b c', 'git ls-files', 'git ls-files src', 'git --no-pager log -3', 'git -c core.pager=cat diff', 'git -C',
    'git --no-pager', 'git commit --amend --no-edit', 'git diff HEAD~1 -- src/', 'git status -sb', 'git push origin "bad name"',
    '/usr/bin/git status', 'git grep',
    # tests
    'pytest', 'pytest -k "name" tests/unit/test_a.py', 'pytest a b', 'python -m pytest tests', 'py.test -x',
    'python3 -m pytest -n 4 tests/unit', 'python3 -m pytest', '/usr/bin/python3 -m pytest tests',
    # reading
    'head -n 20 a.txt b.txt', 'tail -5 log.txt', 'head 5 file.txt', 'cat -n file.txt', "sed -n '5p' file.txt", 'sed -n 5,10p',
    "sed 's/a/b/' file.txt", "sed -i.bak 's/a/b/' f.txt", "sed --in-place 's/a/b/' f.txt", "sed -i 's/a/b/' a.txt b.txt", 'less',
    'bat -p src/main.rs', 'head ٣ file.txt', 'head ² file.txt', 'head ½ file.txt', 'ls -٣', "sed -n 'p' file.txt",
    # listing
    'ls -la /srv/a /srv/b', 'dir', 'tree', 'ls ~', 'ls ..', 'ls ./', 'ls ' + 'd' * 235, 'ls /', 'cd / && ls', 'ls ~user',
    'ls my-project_2.0', 'ls "a b"', 'cd ~ && ls',
    # programs nobody listed
    'jq . package.json', 'gh pr view 123', 'unknownprog', 'unknownprog a', 'unknownprog a a', 'prog --mode=list file.txt',
    'prog "two words"', 'prog ' + 'x' * 300, '/opt/tools/my-prog --x y', 'weird!name arg', 'prog é', 'prog "a\\"b"',
    "prog 'tab\there'", 'prog \U0001f600', 'prog a b c d e f g h i j', 'prog -g skipped kept', "prog ''",
    # scripts, programs that change things, what a shell does itself, whose rights
    'python3.11 script.py', 'node --version', 'deno run x.ts', './deploy.sh', 'scripts/build.py --all', 'tsx src/index.ts', 'dirs',
    'cd /tmp', 'export A=1', '[ -f x ]', 'test -d x', 'source ~/.bashrc', '. ./env.sh', "'run.sh\n' arg", 'su - root', 'pkexec ls',
    'doas -u x ls', '/usr/bin/sudo ls', 'make', 'cargo', 'git-lfs pull', 'x.rb', 'run.SH',
]

MORE_OTHERS = [
    {'name': 'Read', 'file_path': '  spaced.txt  '}, {'name': 'Grep', 'pattern': 'x', 'input': {'path': 'lib'}},
    {'name': 'Glob', 'input': {'path': 'src'}}, {'name': 'LS', 'input': {'path': '~'}}, {'name': 'ls', 'path': '/'},
    {'name': 'WebSearch', 'input': {'query': 'x' * 200}}, {'name': 'WebSearch', 'input': {'query': 'back`tick'}},
    {'name': 'Edit', 'path': 'a.rs', 'file_path': 'b.rs'}, {'name': 7},
    {'name': 'Bash', 'kind': 'command', 'summary': 'ls', 'command': 'pwd'},
    {'kind': 'command', 'summary': 'git status', 'input': {'cmd': 'ls'}}, {'name': 'bash -lc "ls"', 'kind': 'tool'},
    {'name': 'sh -c ls', 'command': 'pwd'}, {'name': '/bin/zsh -c "git status"'}, {'name': 'dash -c ls'},
    {'name': 'sh  -lc\t"cat README.md"'}, {'name': '  bash -c pwd  '},
    {'kind': 'exploration', 'operations': ['Read: .env']}, {'kind': 'exploration', 'operations': ['Read: ~/notes']},
    {'kind': 'exploration', 'operations': ['Read: Cargo.toml']}, {'kind': 'exploration', 'operations': ['Read: a,b.txt']},
    {'kind': 'exploration', 'operations': ['Read']}, {'kind': 'exploration', 'operations': ['Read: ']},
    {'kind': 'exploration', 'name': 'Explored', 'operations': ['Read: src/a.rs', 'Read: src/a.rs', 'List: src']},
    {'kind': 'patch', 'operations': ['Edit: path with spaces.txt']}, {'kind': 'patch', 'operations': ['Add: ' + 'x' * 300]},
    {'kind': 'patch', 'operations': ['Delete: -rf']}, {'kind': 'patch', 'operations': ['Edit: a: b']},
    {'kind': 'patch', 'operations': ['Edit']}, {'name': 'N' * 200},
    {'name': 'T', 'input': {'query': 'q', 'description': 'd', 'pattern': 5}}, {'name': 'T', 'title': 'x', 'input': 'x'},
    {'name': 'T', 'path': '/srv/x', 'title': '/srv/x'}, {'name': 'T', 'title': 'one', 'input': {'description': 'two', 'path': 'p'}},
    {'name': 'Write', 'input': {'file_path': 'notes/été.md'}}, {'name': 'Read', 'file_path': 'a/b/'},
    {'name': 'Read', 'file_path': '-odd'}, {'name': 'Read', 'file_path': 'https://example.org/x'},
    {'name': 'LS', 'path': '/srv/my project'}, {'name': 'LS', 'path': '/srv/' + 'p' * 50},
    {'name': 'Grep', 'pattern': 'p' * 121}, {'name': 'Grep', 'pattern': 'fn\tmain'}, {'name': 'Glob', 'pattern': '-x'},
    {'kind': 'command', 'name': 'Read', 'summary': 'cat notes.md', 'file_path': 'other.md'},
    {'name': 'Bash', 'command': 'ls', 'input': {'command': 'pwd'}}, {'name': 'Bash', 'cmd': 'pwd'},
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
    # What was added later stands after what was there: every earlier use keeps its place.
    uses += [sh(command) for command in MORE_SHELL] + MORE_OTHERS
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
    shutil.copyfile(LIBRARY, LIBRARY_COPY)
    import collections
    templates_used = collections.Counter(row['route'].get('template_id') for row in rows if 'route' in row and row['route'].get('template_id'))
    reasons = collections.Counter(row['route'].get('reason') for row in rows if 'route' in row and not row['route'].get('template_id'))
    print(f'{OUT.relative_to(REPO)}: {len(rows)} uses; a template fits {sum(templates_used.values())} '
          f'({len(templates_used)} templates), none fits {sum(reasons.values())}, refused {sum("refused" in row for row in rows)}')
    print(f'  {LIBRARY_COPY.relative_to(REPO)}: the library, version {said["version"]}, '
          f'{sum(1 for row in rows for text in row.get("said", []) if text is not None)} sentences said of these uses')
    print('  templates:', dict(templates_used.most_common()))
    print('  reasons:', dict(reasons.most_common()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
