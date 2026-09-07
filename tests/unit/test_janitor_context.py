import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lib.janitor_context import build_context, build_context_from_connection, useful


class TaskContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.c = sqlite3.connect(self.path)
        self.addCleanup(self.c.close)
        self.c.executescript('''
          CREATE TABLE agents(agent_id TEXT,session TEXT,cwd TEXT,custom_status TEXT,deleted_at,archived_at);
          CREATE TABLE state_log(state_id INTEGER,agent_id TEXT,kind TEXT);
          CREATE TABLE runtimes(runtime_id INTEGER,agent_id TEXT,backend_session_id TEXT,started_at INTEGER);
          CREATE TABLE task_plans(plan_id TEXT,agent_id TEXT,title TEXT,status TEXT,created_at INTEGER);
          CREATE TABLE task_items(item_id TEXT,plan_id TEXT,title TEXT,detail TEXT,status TEXT,position INTEGER);
          CREATE TABLE messages(message_id TEXT,agent_id TEXT,backend_session_id TEXT,seq INTEGER,role TEXT,
                                text TEXT,kind TEXT,origin TEXT,revision INTEGER,timestamp TEXT);
          INSERT INTO agents VALUES('a','hugo','/repo/clarp-ios','Clarp animations',NULL,NULL);
          INSERT INTO state_log VALUES(1,'a','idle');
          INSERT INTO runtimes VALUES(1,'a','current',0);
        ''')
        self.counter = 0
        self.c.commit()

    def message(self, text, role='user', origin='user', kind=None, conversation='current', seconds=None):
        self.counter += 1
        seconds = self.counter if seconds is None else seconds
        stamp = dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()
        mid = 'm' + str(self.counter)
        self.c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (mid, 'a', conversation, -self.counter, role, text, kind, origin, 1, stamp))
        self.c.commit()
        return mid

    def plan(self, status='active'):
        self.c.execute('INSERT INTO task_plans VALUES(?,?,?,?,?)',
                       ('p', 'a', 'Improve Clarp iPhone animations', status, 10))
        self.c.execute('INSERT INTO task_items VALUES(?,?,?,?,?,?)',
                       ('step', 'p', 'Verify animation preview', 'Run iPhone simulator tests.', 'in_progress', 0))
        self.c.commit()

    def context(self):
        return build_context(self.path, 'hugo')

    def test_keeps_task_and_corrections_despite_trivial_messages(self):
        objective = self.message('Improve the Clarp iPhone animations and their video previews.')
        self.plan()
        correction = self.message('Also preserve the existing Theo portrait while animating it.')
        for text in ['status?', 'yes', 'continue??', 'ok, go ahead', 'Can you give me a status update?', 'Status? did you stop?', 'Hello Hugo, can you hear me?']:
            self.message(text)
        result = self.context()
        self.assertEqual(result['source_refs']['objective']['message_id'], objective)
        self.assertEqual(result['source_refs']['updates'][0]['message_id'], correction)
        self.assertIn('preserve the existing Theo portrait', result['brief'])
        self.assertTrue(result['has_context'])

    def test_latest_runtime_scope_excludes_old_conversation(self):
        self.message('Build a trading bot for cryptocurrency futures.', conversation='old')
        self.c.execute("INSERT INTO runtimes VALUES(0,'a','old',-100)")
        self.c.commit()
        self.assertFalse(self.context()['has_context'])
        self.message('Improve the Clarp iPhone animations.')
        self.assertNotIn('trading', json.dumps(self.context()))

    def test_new_runtime_without_binding_does_not_reuse_old_messages(self):
        self.message('Improve the Clarp iPhone animations.')
        self.c.execute("INSERT INTO runtimes VALUES(2,'a',NULL,100)")
        self.c.commit()
        self.assertFalse(self.context()['has_context'])
        self.assertIsNone(self.context()['conversation_id'])

    def test_negative_sequence_uses_actual_chronology(self):
        self.message('Fix the Clarp iPhone animations.', seconds=1)
        new = self.message('Build the weather widget for Verbo.', seconds=3)
        self.message('Improve the Clarp audio playback.', seconds=2)
        self.assertEqual(self.context()['source_refs']['objective']['message_id'], new)

    def test_filters_automation_heartbeat_and_commentary(self):
        self.message('Fix the Clarp iPhone animations.')
        self.message('The new preview tests passed on iPhone.', role='assistant', kind='final_answer')
        self.message('I am about to inspect the cryptocurrency exchange.', role='assistant', kind='commentary')
        self.message('HEARTBEAT_OK', role='assistant', kind='final_answer', origin='heartbeat')
        self.message('Create a cryptocurrency wallet immediately.', origin='automation')
        self.message('This is a continuity check, not a new task.', origin=None)
        self.assertEqual(self.context()['latest_result'], 'The new preview tests passed on iPhone.')
        self.assertNotIn('cryptocurrency', self.context()['brief'])

    def test_stable_fingerprint_ignores_state_timestamp_and_label(self):
        self.message('Fix the Clarp iPhone animations.')
        self.plan()
        before = self.context()
        self.c.execute("INSERT INTO state_log VALUES(2,'a','done')")
        self.c.execute("UPDATE agents SET custom_status='iPhone animations'")
        self.c.execute("UPDATE messages SET timestamp='1970-01-01T00:00:02+00:00'")
        self.c.commit()
        self.message('status?')
        after = self.context()
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(before['change_key'], after['change_key'])
        self.assertNotEqual(before['state_id'], after['state_id'])
        self.assertNotEqual(before['current_status'], after['current_status'])

    def test_important_correction_bypasses_task_stability(self):
        self.message('Fix the Clarp iPhone animations.')
        self.plan()
        before = self.context()
        self.message('Instead keep only the portrait animation and remove constellations.')
        after = self.context()
        self.assertEqual(before['task_key'], after['task_key'])
        self.assertNotEqual(before['change_key'], after['change_key'])
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])

    def test_plan_phase_transition_changes_change_key(self):
        self.plan()
        before = self.context()
        self.c.execute("UPDATE task_items SET status='completed' WHERE item_id='step'")
        self.c.execute("INSERT INTO task_items VALUES('next','p','Ship TestFlight','Verify tester access','in_progress',1)")
        self.c.commit()
        after = self.context()
        self.assertEqual(before['task_key'], after['task_key'])
        self.assertNotEqual(before['change_key'], after['change_key'])

    def test_final_progress_changes_fingerprint_not_important_change_key(self):
        self.message('Fix the Clarp iPhone animations.')
        self.plan()
        before = self.context()
        self.message('The preview compiled and the first check passed.', role='assistant', kind='final_answer')
        after = self.context()
        self.assertEqual(before['change_key'], after['change_key'])
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])

    def test_bounded_payload_and_no_invented_context(self):
        self.message('Improve the Clarp animations. ' + 'Detailed request content. ' * 1500)
        self.plan()
        for _ in range(3):
            self.message('Also preserve this feature. ' + 'Specific correction details. ' * 1000)
        self.message('The result contains evidence. ' + 'The tests passed. ' * 1000,
                     role='assistant', kind='final_answer')
        result = self.context()
        self.assertLessEqual(len(result['brief']), 2400)
        self.assertLessEqual(len(result['brief'].split()), 300)
        self.assertLess(len(json.dumps(result)), 6000)
        missing = build_context(self.path, 'missing')
        self.assertFalse(missing['has_context'])
        self.assertEqual(missing['state'], 'unavailable')

    def test_completed_plan_is_not_context(self):
        self.plan('completed')
        self.message('Hey')
        self.assertIsNone(self.context()['active_plan'])
        self.assertFalse(self.context()['has_context'])

    def test_meaningful_heartbeat_work_not_mistaken_for_noop(self):
        self.assertTrue(useful('Fix the heartbeat scheduler so tasks survive restarts.'))

    def test_short_verb_object_requests_are_valid_without_a_plan(self):
        for text in ['Fix audio', 'Add tests', 'Debug login', 'Update docs']:
            with self.subTest(text=text):
                self.message(text)
                result = self.context()
                self.assertTrue(result['has_context'])
                self.assertEqual(result['user_objective'], text)
        for text in ['Fix it', 'Add that', 'status?', 'yes', 'do it']:
            with self.subTest(text=text):
                self.assertFalse(useful(text))

    def test_obvious_credentials_redacted_in_all_context_and_hash_inputs(self):
        from unittest.mock import patch
        from lib import janitor_context as task_context
        api = 'sk-proj-SuperSecretValue0123456789'
        bearer = 'eyJhbGciSecret.EncodedToken.signature123'
        pem = '-----BEGIN RSA PRIVATE KEY-----\nPrivateKeySecretMaterial\n-----END RSA PRIVATE KEY-----'
        self.message('Fix audio using ' + api)
        self.message('Also preserve the header Authorization: Bearer ' + bearer)
        self.message('The test found ' + pem, role='assistant', kind='final_answer')
        self.plan()
        self.c.execute("UPDATE task_items SET detail=?", ('Inspect ' + api,))
        self.c.execute("UPDATE agents SET custom_status=?", (api,))
        self.c.commit()
        hashed = []
        original = task_context.digest
        def capture(value):
            hashed.append(json.dumps(value))
            return original(value)
        with patch.object(task_context, 'digest', capture):
            result = self.context()
        text = json.dumps(result) + ''.join(hashed)
        for secret in [api, bearer, 'PrivateKeySecretMaterial']:
            self.assertNotIn(secret, text)
        self.assertIn('[REDACTED API KEY]', result['user_objective'])
        self.assertIn('[REDACTED TOKEN]', result['recent_user_updates'][0])
        self.assertIn('[REDACTED PRIVATE KEY]', result['latest_result'])

    def test_truncated_and_html_escaped_credentials_stay_redacted(self):
        from lib.janitor_context import clip
        pem = '-----BEGIN PRIVATE KEY-----\n' + 'secretMaterial' * 1000
        self.assertNotIn('secretMaterial', clip(pem[:6000]))
        self.assertNotIn('verySecret', clip('Use sk&#45;verySecretCredential0123'))
        self.assertNotIn('verySecret', clip('Authorization: Bearer verySecretCredential0123'))
        self.assertNotIn('verySecret', clip('"Authorization": "Bearer verySecretCredential0123"'))
        sanitized = clip('Authorization: Bearer verySecretCredential0123')
        self.assertEqual(clip(sanitized), sanitized)

    def test_writer_connection_reads_uncommitted_snapshot_without_committing(self):
        self.message('Fix the Clarp iPhone animations.')
        self.c.execute('BEGIN IMMEDIATE')
        self.c.execute("UPDATE agents SET custom_status='New label'")
        result = build_context_from_connection(self.c, 'hugo')
        self.assertTrue(self.c.in_transaction)
        self.assertEqual(result['current_status'], 'New label')
        self.c.rollback()
        self.assertEqual(self.context()['current_status'], 'Clarp animations')

    def test_read_only_builder_cannot_create_missing_database(self):
        with self.assertRaises(sqlite3.OperationalError):
            build_context(Path(self.tmp.name) / 'missing.sqlite', 'hugo')
        self.assertFalse((Path(self.tmp.name) / 'missing.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
