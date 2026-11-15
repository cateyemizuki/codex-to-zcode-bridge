import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'runtime'))
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts import superagent_live_check
from scripts import desktop_sync_probe


def run_main(module, argv):
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = module.main(argv)
    return code, json.loads(stream.getvalue())


class LiveCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / 'work'
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_preflight_never_delegates(self):
        calls = []

        def fake(name, arguments=None, context=None, timeout=None):
            calls.append(name)
            return {'available': [{'ref': {'providerId': 'acme', 'modelId': 'alpha'},
                                   'label': 'Alpha', 'providerLabel': 'Acme',
                                   'reasoning': {'levels': [{'value': 'high'}]}}]}

        with patch('scripts.superagent_live_check.storage.config',
                   return_value={'default_workspace': str(self.workspace)}), \
                patch('scripts.superagent_live_check.transport.local_call',
                      side_effect=fake):
            code, result = run_main(superagent_live_check, ['--workspace', str(self.workspace)])
        self.assertEqual(code, 0)
        self.assertEqual(result['mode'], 'preflight')
        self.assertEqual(result['model_count'], 1)
        self.assertEqual(calls, ['zcode_models'])
        self.assertNotIn('zcode_delegate', calls)

    def test_live_without_workspace_or_source_does_not_create(self):
        calls = []
        with patch('scripts.superagent_live_check.transport.local_call',
                   side_effect=lambda *a, **k: calls.append(a[0])):
            code, result = run_main(superagent_live_check,
                                    ['--confirm-live', '--source-thread', 'chat-X'])
        self.assertEqual(code, 2)
        self.assertIn('workspace_required', result['error'])
        self.assertEqual(calls, [])

    def test_live_roundtrip_uses_source_context_and_cancels(self):
        calls = []

        def fake(name, arguments=None, context=None, timeout=None):
            calls.append((name, arguments, context))
            if name == 'zcode_delegate':
                return {'created': True, 'agent_id': 'agent_1', 'session_id': 'sess_1',
                        'send': {'state': 'accepted'}, 'subscription': {'armed': False},
                        'job': {'status': 'running'}}
            if name == 'zcode_agent_inspect':
                return {'verdict': 'running', 'job': {'status': 'running'}}
            if name == 'zcode_agent_message':
                return {'state': 'queued', 'position': 1}
            if name == 'zcode_agent_cancel':
                return {'cancelled': True}
            raise AssertionError(name)

        with patch('scripts.superagent_live_check.transport.local_call', side_effect=fake):
            code, result = run_main(superagent_live_check, [
                '--confirm-live', '--workspace', str(self.workspace),
                '--source-thread', 'chat-X'])
        self.assertEqual(code, 0, result)
        self.assertTrue(result['ok'])
        self.assertEqual([row[0] for row in calls],
                         ['zcode_delegate', 'zcode_agent_inspect',
                          'zcode_agent_message', 'zcode_agent_inspect',
                          'zcode_agent_cancel'])
        self.assertTrue(all(row[2]['thread_id'] == 'chat-X' for row in calls))
        self.assertEqual(result['steps'][-1]['step'], 'cancel')


class DesktopSyncProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'work'
        self.workspace.mkdir()
        import secrets
        self.token = 'test-' + secrets.token_urlsafe(24)
        self.control = self.root / 'control.json'
        self.control.write_text(json.dumps({
            'host': '127.0.0.1', 'port': 12345, 'token': self.token,
            'discoveryPath': str(self.root / 'discovery.json'),
            'allowedWorkspaces': [str(self.workspace)],
        }), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_never_prints_token(self):
        code, result = run_main(desktop_sync_probe,
                                ['--control', str(self.control)])
        self.assertEqual(code, 0)
        self.assertTrue(result['token_present'])
        self.assertNotIn(self.token, json.dumps(result))

    def test_sync_requires_confirmation(self):
        with patch('scripts.desktop_sync_probe.control_call') as call:
            code, result = run_main(desktop_sync_probe, [
                '--control', str(self.control), '--op', 'sync',
                '--workspace', str(self.workspace), '--session-id', 'sess_1'])
        self.assertEqual(code, 2)
        self.assertIn('confirm_required', result['error'])
        call.assert_not_called()

    def test_sync_summarizes_allowlisted_result_without_token(self):
        with patch('scripts.desktop_sync_probe.control_call',
                   return_value={'ok': True, 'synced': [
                       {'sessionId': 'sess_1', 'changed': True, 'missing': False}]}) as call:
            code, result = run_main(desktop_sync_probe, [
                '--control', str(self.control), '--op', 'sync',
                '--workspace', str(self.workspace), '--session-id', 'sess_1',
                '--confirm-sync'])
        self.assertEqual(code, 0)
        self.assertEqual(result['synced'], 1)
        self.assertEqual(result['changed'], 1)
        self.assertNotIn(self.token, json.dumps(result))
        message = call.call_args[0][1]
        self.assertEqual(message['sessionIds'], ['sess_1'])
        self.assertEqual(message['workspace'], str(self.workspace.resolve()))

    def test_probe_prints_only_summary(self):
        with patch('scripts.desktop_sync_probe.control_call',
                   return_value={'ok': True, 'ready': True,
                                 'allowedWorkspaceCount': 1,
                                 'subscribedWorkspaceCount': 1,
                                 'events': [{'reason': 'sync', 'status': 'done',
                                             'at': 1, 'title': 'private title'}]}):
            code, result = run_main(desktop_sync_probe, [
                '--control', str(self.control), '--op', 'probe'])
        self.assertEqual(code, 0)
        self.assertTrue(result['ready'])
        self.assertEqual(result['event_count'], 1)
        self.assertNotIn('private title', json.dumps(result))
        self.assertNotIn(self.token, json.dumps(result))


if __name__ == '__main__':
    unittest.main()
