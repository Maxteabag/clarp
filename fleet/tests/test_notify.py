import subprocess
import tempfile
import unittest
from unittest.mock import patch
from fleet.notify import deliver, start_watcher


class NotifyTests(unittest.TestCase):
    def job(self):
        return {'id': 'job-1', 'attempt': 'attempt-1', 'peer': 'worker', 'status': 'succeeded',
                'request': {'parent': {'host': 'parent', 'agent': 'session-1', 'task': 'task-1'}}}

    def test_admission_is_deduplicated_and_does_not_ack_result(self):
        calls = []
        def send(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '{"ok":true}', '')
        with tempfile.TemporaryDirectory() as path:
            self.assertEqual(deliver(self.job(), path, send)['delivery'], 'admitted')
            self.assertTrue(deliver(self.job(), path, send)['reused'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], 'session-1')
        self.assertIn('attempt-1', calls[0][-1])

    def test_uncertain_send_is_not_repeated(self):
        calls = []
        def send(argv, **kwargs):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, 30)
        with tempfile.TemporaryDirectory() as path:
            self.assertEqual(deliver(self.job(), path, send)['delivery'], 'unknown')
            self.assertEqual(deliver(self.job(), path, send)['delivery'], 'unknown')
        self.assertEqual(len(calls), 1)

    def test_launcher_uses_stable_unit_and_managed_watcher(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch('fleet.notify.subprocess.run', side_effect=run):
            a = start_watcher('job-1', self.job()['request']['parent'])
            b = start_watcher('job-1', self.job()['request']['parent'])
        self.assertEqual(a['unit'], b['unit'])
        self.assertIn('--managed', calls[0])
        self.assertIn('--property=Restart=no', calls[0])
        self.assertEqual(calls[0][-2:], ['session-1', '--managed'])

    def test_submit_notification_uses_request_parent_not_response_shape(self):
        from fleet.__main__ import main
        import contextlib, io
        parent = self.job()['request']['parent']
        with patch('sys.argv', ['clarp-fleet', 'submit', '--profile', 'diagnostic', '--id', 'job-1', '--notify-parent']), \
             patch('fleet.__main__.request', return_value={'parent': parent}), \
             patch('fleet.__main__.api', return_value={'id': 'job-1', 'status': 'queued'}), \
             patch('fleet.notify.check_parent'), \
             patch('fleet.notify.start_watcher', return_value={'state': 'started'}) as start, \
             contextlib.redirect_stdout(io.StringIO()):
            main()
        start.assert_called_once_with('job-1', parent)

    def test_watcher_recovers_read_disconnect_without_repeating_delivery(self):
        from fleet.notify import watch
        from types import SimpleNamespace
        import contextlib, io
        args = SimpleNamespace(id='job-1', parent_agent='session-1', timeout=60)
        with patch('fleet.notify.api', side_effect=[ConnectionRefusedError(), self.job()]) as read, \
             patch('fleet.notify.time.sleep'), \
             patch('fleet.notify.deliver', return_value={'delivery': 'admitted'}) as send, \
             contextlib.redirect_stdout(io.StringIO()):
            watch(args, {'local_id': 'parent'}, None)
        self.assertEqual(read.call_count, 2)
        send.assert_called_once()

    def test_watcher_does_not_retry_authorization_or_ownership_failure(self):
        from fleet.notify import watch
        from types import SimpleNamespace
        args = SimpleNamespace(id='job-1', parent_agent='session-1', timeout=60)
        with patch('fleet.notify.api', side_effect=RuntimeError('Authentication required')) as read:
            with self.assertRaises(RuntimeError):
                watch(args, {'local_id': 'parent'}, None)
        self.assertEqual(read.call_count, 1)

    def test_lost_response_retries_identical_server_message_id(self):
        with tempfile.TemporaryDirectory() as path, \
             patch('fleet.notify.send_notice', side_effect=[TimeoutError(), {'ok': True}]) as send:
            self.assertEqual(deliver(self.job(), path)['delivery'], 'retry_pending')
            self.assertEqual(deliver(self.job(), path)['delivery'], 'admitted')
            self.assertEqual(send.call_args_list[0], send.call_args_list[1])
            self.assertTrue(deliver(self.job(), path)['reused'])
            self.assertEqual(send.call_count, 2)

    def test_legacy_uncertain_delivery_is_not_upgraded_into_retry(self):
        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 30)
        with tempfile.TemporaryDirectory() as path:
            self.assertEqual(deliver(self.job(), path, timeout)['delivery'], 'unknown')
            with patch('fleet.notify.send_notice') as send:
                self.assertEqual(deliver(self.job(), path)['delivery'], 'unknown')
                send.assert_not_called()
