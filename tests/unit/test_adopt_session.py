"""Exercise adopt_session against a stub Host; no real Clarp server is touched."""
import contextlib
import importlib.util
import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    'adopt_session',
    Path(__file__).resolve().parents[2] / 'skills/clarp-adopt/scripts/adopt_session.py')
adopt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adopt)

NATIVE = '11111111-2222-3333-4444-555555555555'


class Host(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        s = self.state
        if self.path.startswith('/agents/snapshot'):
            return self.reply(200, {'agents': s['agents'], 'personas': [{'name': 'Theo'}]})
        if self.path.startswith('/past-sessions'):
            rows = [{'id': NATIVE, 'mtime': 5, 'title': 'fix limine', 'cwd': '/srv/work'}]
            return self.reply(200, {'sessions': rows if 'backend=codex' in self.path else []})
        if self.path.startswith('/log'):
            return self.reply(200, s['log'])
        self.reply(404, {})

    def do_POST(self):
        s = self.state
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        s['posts'].append(body)
        if s.get('conflict'):
            return self.reply(409, {'error': 'voice_in_use', 'message': 'Voice is taken.'})
        self.reply(200, {'ok': True, 'session': 'theo-ab12', 'name': body.get('name', 'Mara')})


class AdoptTest(unittest.TestCase):
    real_connection = staticmethod(adopt.local_connection)
    real_enclosing_cli = staticmethod(adopt.enclosing_cli)

    def setUp(self):
        Host.state = {'agents': [], 'posts': [], 'log': {
            'conversation_id': NATIVE, 'missing': False,
            'has_more': False,
            'turns': [{'role': 'user', 'text': 'fix the limine config'},
                      {'role': 'assistant', 'text': 'done'}]}}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Host)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{self.server.server_address[1]}'
        patches = [mock.patch.object(adopt, 'local_connection', lambda: (base, 'secret')),
                   mock.patch.object(adopt, 'enclosing_cli', lambda: None),
                   mock.patch.dict(os.environ, {}, clear=False)]
        for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDE_CODE_SESSION_ID',
                     'GROK_SESSION_ID'):
            os.environ.pop(name, None)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = adopt.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_creates_and_verifies_with_catalog_cwd(self):
        code, out, _ = self.run_cli('Theo', '--id', NATIVE, '--json')
        self.assertEqual(code, 0)
        self.assertEqual(Host.state['posts'], [{
            'backend': 'codex', 'cwd': '/srv/work', 'resume_session_id': NATIVE,
            'synthesize_audio': False, 'name': 'Theo'}])
        result = json.loads(out)
        self.assertTrue(result['verified'])
        self.assertEqual(result['last_user_message'], 'fix the limine config')
        self.assertNotIn('secret', out)
        self.assertEqual(result['turns'], '2')

    def test_no_name_lets_clarp_pick_and_env_identifies_self(self):
        os.environ['CODEX_THREAD_ID'] = NATIVE
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue(Host.state['posts'][0]['auto_contact'])
        self.assertNotIn('name', Host.state['posts'][0])
        self.assertIn('Created Mara (theo-ab12)', out)

    def test_dry_run_and_occupied_contact_post_nothing(self):
        self.assertEqual(self.run_cli('Theo', '--id', NATIVE, '--dry-run')[0], 0)
        Host.state['agents'] = [{'persona': 'theo', 'session': 'theo', 'backend_session_id': 'other'}]
        code, _, err = self.run_cli('Theo', '--id', NATIVE)
        self.assertEqual(code, 1)
        self.assertIn('already runs session', err)
        self.assertEqual(Host.state['posts'], [])

    def test_already_adopted_is_reused_not_recreated(self):
        Host.state['agents'] = [{'persona': 'Theo', 'session': 'theo-ab12',
                                 'backend_session_id': NATIVE}]
        code, out, _ = self.run_cli('--id', NATIVE)
        self.assertEqual((code, Host.state['posts']), (0, []))
        self.assertIn('Already adopted as Theo', out)

    def test_wrong_history_fails_verification(self):
        Host.state['log'] = {'conversation_id': 'something-else', 'missing': False, 'turns': []}
        code, out, _ = self.run_cli('Theo', '--id', NATIVE)
        self.assertEqual(code, 2)
        self.assertIn('NOT verified', out)

    def test_server_conflict_is_reported(self):
        Host.state['conflict'] = True
        code, _, err = self.run_cli('Theo', '--id', NATIVE)
        self.assertEqual(code, 1)
        self.assertIn('HTTP 409: Voice is taken.', err)


    def test_reads_the_local_host_from_the_installed_configuration(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.toml'
            config.write_text('[server]\nbind_addr = "::"\nport = 7700\nauth_token = "t"\n')
            with mock.patch.dict(os.environ, {'CLAUDE_PWA_CONFIG': str(config)}):
                self.assertEqual(self.real_connection(), ('http://127.0.0.1:7700', 't'))

    def test_innermost_cli_wins_over_an_inherited_session_id(self):
        # Grok started from inside a Claude session sees both variables.
        os.environ['CLAUDE_CODE_SESSION_ID'] = 'outer-claude'
        os.environ['GROK_SESSION_ID'] = 'inner-grok'
        with mock.patch.object(adopt, 'enclosing_cli', lambda: 'grok'):
            self.assertEqual(adopt.detect_self(), ('grok', 'inner-grok'))
        with mock.patch.object(adopt, 'enclosing_cli', lambda: 'claude'):
            self.assertEqual(adopt.detect_self(), ('claude', 'outer-claude'))

    def test_conflicting_ids_without_a_known_cli_are_refused(self):
        os.environ['CLAUDE_CODE_SESSION_ID'] = 'a'
        os.environ['GROK_SESSION_ID'] = 'b'
        code, _, err = self.run_cli('--dry-run')
        self.assertEqual(code, 1)
        self.assertIn('could not be determined', err)
        self.assertEqual(Host.state['posts'], [])

    def test_cli_names_match_versioned_binaries_but_not_lookalike_files(self):
        def cli_for(*argv):
            with mock.patch.object(adopt, '_ancestor_commands', lambda: iter([list(argv)])):
                return self.real_enclosing_cli()
        self.assertEqual(cli_for('/home/u/.grok/downloads/grok-1.0.34-linux-x86_64', '-p'), 'grok')
        self.assertEqual(cli_for('node', '/usr/lib/node_modules/.bin/codex', 'resume'), 'codex')
        self.assertEqual(cli_for('/home/u/.opencode/bin/opencode', 'run'), 'opencode')
        self.assertIsNone(cli_for('vim', 'claude-notes.md'))
        self.assertIsNone(cli_for('python3', 'codex_helper.py'))

    def _opencode_db(self, folder, rows):
        import sqlite3
        path = Path(folder) / 'opencode'
        path.mkdir()
        con = sqlite3.connect(path / 'opencode.db')
        con.execute('CREATE TABLE part (id text, session_id text, data text)')
        for index, (session, status, command, start) in enumerate(rows):
            con.execute('INSERT INTO part VALUES (?,?,?)', (str(index), session, json.dumps({
                'type': 'tool', 'tool': 'bash',
                'state': {'status': status, 'input': {'command': command}, 'time': {'start': start}}})))
        con.commit()
        con.close()
        return folder

    def test_opencode_conversation_is_the_one_running_this_command(self):
        import tempfile
        now = 1_800_000_000_000
        with tempfile.TemporaryDirectory() as folder:
            self._opencode_db(folder, [
                ('ses_me', 'running', 'clarp-adopt Theo', now - 900),
                ('ses_other', 'running', 'npm test', now - 900),
                ('ses_done', 'completed', 'clarp-adopt Theo', now - 900),
                ('ses_crashed', 'running', 'clarp-adopt', now - 3_600_000),
            ])
            self.assertEqual(adopt.opencode_running_session(folder, now), 'ses_me')

    def test_opencode_refuses_when_zero_or_two_conversations_match(self):
        import tempfile
        now = 1_800_000_000_000
        for rows in ([], [('ses_a', 'running', 'clarp-adopt', now), ('ses_b', 'running', 'clarp-adopt', now)]):
            with tempfile.TemporaryDirectory() as folder:
                self._opencode_db(folder, rows)
                with self.assertRaises(adopt.AdoptError):
                    adopt.opencode_running_session(folder, now)

