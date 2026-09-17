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
                   mock.patch.dict(os.environ, {}, clear=False)]
        for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDE_CODE_SESSION_ID'):
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

