#!/usr/bin/env python3
"""Resume a native CLI conversation (Claude, Codex, Grok, OpenCode) as a Clarp agent.

Binds the existing native session id to a new Clarp agent on this machine's
Host through POST /agents, then verifies the history through GET /log. The
transcript is never copied or rewritten. Run it inside the conversation to
adopt that conversation, or from a plain shell to pick a recent one.
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BACKENDS = ('claude', 'codex', 'grok', 'opencode')
# Environment variables a running CLI exports for its own conversation.
# OpenCode exports none; see opencode_running_session.
SELF_ID_ENV = {'codex': ('CODEX_THREAD_ID', 'CODEX_SESSION_ID'),
               'claude': ('CLAUDE_CODE_SESSION_ID',),
               'grok': ('GROK_SESSION_ID',)}
# `grok` resolves to a versioned binary (grok-1.0.34-linux-x86_64); a file that
# merely starts with a CLI's name (claude-notes.md) must not match.
_CLI_NAME = re.compile(r'^(claude|codex|grok|opencode)(?:-\d[\w.-]*)?$')
# How long ago the CLI may have started the tool call that is running us.
_RUNNING_WINDOW_MS = 10 * 60 * 1000


class AdoptError(Exception):
    pass


def local_connection():
    # The native transcript lives on this machine, so this always talks to the
    # local Host, never to a remote one other tools may be pointed at.
    path = Path(os.environ.get('CLAUDE_PWA_CONFIG') or Path(os.environ.get(
        'CLARP_CONFIG_DIR', Path.home() / '.config/clarp')) / 'config.toml')
    try:
        server = tomllib.loads(path.read_text()).get('server', {})
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AdoptError(f'Cannot read the Clarp configuration at {path}: {error}') from error
    bind = str(server.get('bind_addr') or '127.0.0.1')
    if bind in ('0.0.0.0', '::'):
        bind = '127.0.0.1'
    if ':' in bind and not bind.startswith('['):
        bind = f'[{bind}]'
    return f"http://{bind}:{int(server.get('port', 7682))}", str(server.get('auth_token') or '')


class Api:
    def __init__(self):
        self.base, self.token = local_connection()

    def call(self, path, body=None, timeout=90):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.token,
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors='replace')
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = {'error': raw.strip()}
            raise AdoptError(f"{path.split('?')[0]} returned HTTP {error.code}: "
                             f"{detail.get('message') or detail.get('error') or raw}") from error

    def past_sessions(self, backend, cwd, all_projects=False):
        query = urllib.parse.urlencode({'backend': backend, 'cwd': cwd,
                                        'scope': 'all' if all_projects else 'workspace'})
        return self.call('/past-sessions?' + query)['sessions']

    def log(self, session):
        query = urllib.parse.urlencode({'session': session, 'limit': 100,
                                        'include_tool_details': 0})
        return self.call('/log?' + query)


def _ancestor_commands():
    """argv of each ancestor process, nearest first. `ps` rather than /proc so
    this also works on macOS."""
    pid, seen = os.getppid(), set()
    while pid > 1 and pid not in seen and len(seen) < 32:
        seen.add(pid)
        try:
            out = subprocess.run(['ps', '-o', 'ppid=', '-o', 'args=', '-p', str(pid)],
                                 capture_output=True, text=True, timeout=5,
                                 check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return
        if not out:
            return
        parent, _, args = out.partition(' ')
        yield args.split()
        try:
            pid = int(parent)
        except ValueError:
            return


def enclosing_cli():
    """The backend of the nearest CLI this command runs inside, if any.

    Session variables are inherited, so a CLI started from inside another one
    sees both ids (Grok launched from a Claude session has CLAUDE_CODE_SESSION_ID
    and GROK_SESSION_ID). The innermost process is the conversation that asked.
    """
    for argv in _ancestor_commands():
        for word in argv[:2]:
            match = _CLI_NAME.match(os.path.basename(word))
            if match:
                return match.group(1)
    return None


def opencode_running_session(home=None, now_ms=None):
    """OpenCode exports no session id, but it records every tool call in its
    database as `running`, with the command, before the command starts. The
    conversation that is running `clarp-adopt` right now is therefore the one
    to adopt. Anything other than exactly one such conversation is refused."""
    db = Path(home or os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'opencode/opencode.db'
    now_ms = now_ms or int(time.time() * 1000)
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            rows = con.execute(
                "SELECT DISTINCT session_id FROM part "
                "WHERE json_extract(data, '$.type') = 'tool' "
                "AND json_extract(data, '$.state.status') = 'running' "
                "AND json_extract(data, '$.state.input.command') LIKE '%clarp-adopt%' "
                "AND json_extract(data, '$.state.time.start') BETWEEN ? AND ?",
                (now_ms - _RUNNING_WINDOW_MS, now_ms + 5000)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as error:
        raise AdoptError(f'Cannot read the OpenCode database to identify this '
                         f'conversation ({error}); pass --id.') from error
    if len(rows) != 1:
        raise AdoptError(
            f'{len(rows)} OpenCode conversations are running clarp-adopt right now, so this '
            'one cannot be identified safely. Use --all to pick it, or pass --id.')
    return rows[0][0]


def detect_self():
    """(backend, native id) of the conversation running this command, or None
    from a plain shell. Raises rather than guessing when the evidence conflicts."""
    backend = enclosing_cli()
    if backend == 'opencode':
        return backend, opencode_running_session()
    found = {b: os.environ[name].strip() for b, names in SELF_ID_ENV.items()
             for name in reversed(names) if os.environ.get(name, '').strip()}
    if backend:
        if backend in found:
            return backend, found[backend]
        raise AdoptError(f'Running inside {backend}, but it exported no session id. '
                         'Use --all to pick the conversation, or pass --id.')
    if len(found) > 1:
        # No CLI among our ancestors (or `ps` is unavailable) and ids from more
        # than one: one of them is inherited and there is no telling which.
        raise AdoptError('Session ids from ' + ', '.join(sorted(found)) + ' are both set and '
                         'the enclosing CLI could not be determined. Pass --backend with --id.')
    return next(iter(found.items()), None)


def candidates(api, backends, cwd, all_projects):
    rows = []
    for backend in backends:
        for row in api.past_sessions(backend, cwd, all_projects):
            rows.append(dict(row, backend=backend))
    return sorted(rows, key=lambda row: row.get('mtime') or 0, reverse=True)


def describe(row):
    when = time.strftime('%d %b %H:%M', time.localtime(row.get('mtime') or 0))
    label = ' '.join((row.get('title') or row.get('preview') or '(untitled)').split())[:70]
    return f"{row['backend']:<9}{when}  {label}  [{row.get('cwd', '')}]"


def choose(rows, pick, owners):
    if not rows:
        raise AdoptError('No past sessions found here. Try --all, --cwd DIR or --id ID.')
    shown = rows[:15]
    for index, row in enumerate(shown, 1):
        held = f"  (already {owners[row['id']]})" if row['id'] in owners else ''
        print(f'{index:>3}  {describe(row)}{held}', file=sys.stderr)
    if pick is None:
        if not sys.stdin.isatty():
            raise AdoptError('Several sessions match; rerun with --pick N or --id ID.')
        print('Adopt which session? ', end='', file=sys.stderr, flush=True)
        pick = sys.stdin.readline().strip()
    try:
        return shown[int(pick) - 1]
    except (ValueError, IndexError):
        raise AdoptError(f'No session numbered {pick!r}.') from None


def find_source(api, args, owners):
    """Return (backend, native id, agent cwd, catalog row or None)."""
    here = str(Path(args.cwd).expanduser().resolve()) if args.cwd else os.getcwd()
    own = None if (args.id or args.pick or args.all) else detect_self()
    args.own = own
    if own and args.backend in (None, own[0]):
        backend, native_id = own
    elif args.id:
        backend, native_id = args.backend, args.id
    else:
        row = choose(candidates(api, (args.backend,) if args.backend else BACKENDS,
                                here, args.all), args.pick, owners)
        return row['backend'], row['id'], args.cwd and here or row.get('cwd') or here, row
    # The shell may have moved since the conversation began, so prefer the cwd
    # its backend recorded; the catalog also tells us the backend of a bare id.
    row = next((r for r in candidates(api, (backend,) if backend else BACKENDS, here, True)
                if r['id'] == native_id), None)
    if not backend and not row:
        raise AdoptError('That id is in no backend catalog; pass --backend to bind it anyway.')
    backend = backend or row['backend']
    recorded = claude_start_cwd(native_id) if backend == 'claude' else None
    return (backend, native_id,
            args.cwd and here or recorded or (row or {}).get('cwd') or here, row)


def claude_start_cwd(native_id):
    # `claude --resume` only finds a transcript from the directory it was
    # started in, which is the first cwd the transcript records. The catalog
    # reports the latest cwd instead, which differs once the session has cd'd.
    path = transcript_file('claude', native_id)
    if not path:
        return None
    with open(path, encoding='utf-8') as lines:
        for line in lines:
            try:
                cwd = json.loads(line).get('cwd')
            except ValueError:
                continue
            if cwd:
                return cwd
    return None


def transcript_file(backend, native_id):
    home = str(Path.home())
    patterns = {'claude': f'{home}/.claude/projects/*/{native_id}.jsonl',
                'codex': f'{home}/.codex/sessions/*/*/*/rollout-*-{native_id}.jsonl'}
    hits = glob.glob(patterns[backend]) if backend in patterns else []
    return hits[0] if hits else None


def verify(api, session, native_id):
    log = api.log(session)
    problems = []
    if log.get('conversation_id') != native_id:
        problems.append(f"conversation_id is {log.get('conversation_id')!r}, expected {native_id!r}")
    if log.get('missing'):
        problems.append('Clarp reports the transcript as missing')
    if not log.get('turns'):
        problems.append('history is empty')
    last_user = next((' '.join((turn.get('text') or '').split())[:160]
                      for turn in reversed(log.get('turns') or [])
                      if turn.get('role') == 'user' and turn.get('text')), '')
    turns = len(log.get('turns') or [])
    return problems, f'{turns}+' if log.get('has_more') else str(turns), last_user


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('name', nargs='?', help='contact to become; omit to let Clarp pick a free compatible one')
    parser.add_argument('--id', help='native session id (default: this conversation, else a picker)')
    parser.add_argument('--backend', choices=BACKENDS)
    parser.add_argument('--cwd', help='working directory for the agent (default: the session\'s own, else $PWD)')
    parser.add_argument('--all', action='store_true', help='pick from every project, not just this folder')
    parser.add_argument('--pick', type=int, metavar='N', help='choose row N from the list without prompting')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--dry-run', action='store_true', help='show what would be bound; change nothing')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    try:
        api = Api()
        snapshot = api.call('/agents/snapshot')
        live = [a for a in snapshot['agents'] if not a.get('is_janitor')]
        owners = {a['backend_session_id']: a['persona'] for a in live if a.get('backend_session_id')}
        backend, native_id, cwd, row = find_source(api, args, owners)
        owner = next((a for a in live if a.get('backend_session_id') == native_id), None)
        result = {'backend': backend, 'native_id': native_id, 'cwd': cwd,
                  'source': describe(row) if row else 'this conversation' if getattr(args, 'own', None) == (backend, native_id) else 'given id'}
        if owner:
            result.update(session=owner['session'], name=owner['persona'], created=False)
        else:
            if args.name:
                holder = next((a for a in live
                               if a['persona'].casefold() == args.name.casefold()), None)
                if holder:
                    raise AdoptError(f"{holder['persona']} already runs session "
                                     f"'{holder['session']}' on another conversation. "
                                     'Pick another contact; nothing was changed.')
            if args.dry_run:
                result.update(name=args.name or '(Clarp picks a free contact)', dry_run=True)
                print(json.dumps(result, indent=2) if args.json else
                      f"Would bind {backend} session {native_id} in {cwd} as {result['name']}.")
                return 0
            body = {'backend': backend, 'cwd': cwd, 'resume_session_id': native_id,
                    'synthesize_audio': False}
            body.update({'name': args.name} if args.name else {'auto_contact': True})
            if args.model:
                body['model'] = args.model
            if args.effort:
                body['effort'] = args.effort
            try:
                made = api.call('/agents', body)
            except (TimeoutError, urllib.error.URLError) as error:
                raise AdoptError(f'POST /agents did not answer ({error}). It may still have '
                                 'created the agent: rerun this command to reconcile.') from error
            result.update(session=made['session'], name=made.get('name') or args.name, created=True)
            if args.name and not any(p['name'].casefold() == args.name.casefold()
                                     for p in snapshot['personas']):
                result['warning'] = (f"'{args.name}' had no contact record; the session exists but "
                                     'the contact may not. Create it with the clarp-agent-admin skill.')
        problems, turns, last_user = verify(api, result['session'], native_id)
        result.update(verified=not problems, turns=turns, last_user_message=last_user)
        if problems:
            result['problems'] = problems
    except (AdoptError, OSError, KeyError, ValueError) as error:
        print(f'clarp-adopt: {error}', file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        verb = 'Created' if result['created'] else 'Already adopted as'
        print(f"{verb} {result['name']} ({result['session']}) on {backend} session {native_id}")
        if result['verified']:
            print(f"Verified: {turns} turns of history. Last user message: “{last_user}”")
            print('Exit the terminal client before messaging the agent; nothing runs until you do.')
        else:
            print('NOT verified: ' + '; '.join(problems))
        if result.get('warning'):
            print('Warning: ' + result['warning'])
    return 0 if result['verified'] else 2


if __name__ == '__main__':
    sys.exit(main())
