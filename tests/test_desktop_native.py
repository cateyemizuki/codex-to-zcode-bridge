import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge.desktop_native import DesktopNativeAdapter, DesktopNativeError


class DesktopNativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / 'work'
        self.workspace.mkdir()
        self.token = 'token-1234567890-abcdef'
        self.control = root / 'control.json'
        self.control.write_text(json.dumps({
            'host': '127.0.0.1',
            'port': 0,
            'token': self.token,
            'discoveryPath': str(root / 'discovery.json'),
            'allowedWorkspaces': [str(self.workspace)],
            'nativeTransport': {'enabled': True, 'ownershipPath': str(root / 'owned.json')},
        }), encoding='utf-8')
        self.settings = {
            'desktop_native_enabled': True,
            'desktop_native_control_file': str(self.control),
        }
        self.adapter = DesktopNativeAdapter(settings_getter=lambda: self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mutations_carry_workspace_permission_and_request_id(self):
        self.adapter._sync._call = Mock(return_value={'ok': True, 'result': {'sessionId': 'native_1'}})
        result = self.adapter.create(
            str(self.workspace), 'full_access',
            {'providerId': 'p', 'modelId': 'm'}, 'high', 'create_123456789')
        self.assertEqual(result['sessionId'], 'native_1')
        message = self.adapter._sync._call.call_args.args[1]
        self.assertEqual(message['op'], 'native_create')
        self.assertEqual(message['workspace'], str(self.workspace.resolve()))
        self.assertEqual(message['caller_permission_mode'], 'full_access')
        self.assertEqual(message['request_id'], 'create_123456789')
        self.assertEqual(message['thought_level'], 'high')

    def test_reads_do_not_need_a_request_id(self):
        self.adapter._sync._call = Mock(return_value={'ok': True, 'result': {'sessionId': 'native_1'}})
        self.adapter.snapshot('native_1', str(self.workspace), 'always_ask')
        message = self.adapter._sync._call.call_args.args[1]
        self.assertEqual(message['op'], 'native_snapshot')
        self.assertNotIn('request_id', message)
        self.assertEqual(message['caller_permission_mode'], 'always_ask')

    def test_child_reads_use_the_fixed_operations_with_parent_and_run(self):
        self.adapter._sync._call = Mock(return_value={
            'ok': True, 'result': [{'info': {'id': 'm1'}, 'parts': []}]})
        rows = self.adapter.child_messages(
            'parent_1', str(self.workspace), 'always_ask', 'child_1',
            run_id='run_1', after_message_id='m0', limit=20)
        self.assertEqual(rows[0]['info']['id'], 'm1')
        message = self.adapter._sync._call.call_args.args[1]
        self.assertEqual(message['op'], 'native_child_messages')
        self.assertEqual(message['session_id'], 'parent_1')
        self.assertEqual(message['child_session_id'], 'child_1')
        self.assertEqual(message['run_id'], 'run_1')
        self.assertEqual(message['after_message_id'], 'm0')
        self.assertNotIn('request_id', message)
        self.adapter._sync._call = Mock(return_value={
            'ok': True, 'result': {'session': {'sessionId': 'child_1'}}})
        snapshot = self.adapter.child_snapshot(
            'parent_1', str(self.workspace), 'always_ask', 'child_1')
        self.assertEqual(snapshot['session']['sessionId'], 'child_1')
        message = self.adapter._sync._call.call_args.args[1]
        self.assertEqual(message['op'], 'native_child_snapshot')
        self.assertNotIn('run_id', message)

    def test_disabled_or_not_configured_never_calls_the_host(self):
        self.settings['desktop_native_enabled'] = False
        with self.assertRaises(DesktopNativeError) as caught:
            self.adapter.snapshot('native_1', str(self.workspace), 'always_ask')
        self.assertEqual(caught.exception.code, 'disabled')
        self.settings['desktop_native_enabled'] = True
        self.settings['desktop_native_control_file'] = ''
        with self.assertRaises(DesktopNativeError) as caught:
            self.adapter.snapshot('native_1', str(self.workspace), 'always_ask')
        self.assertEqual(caught.exception.code, 'config_missing')

    def test_allowlist_is_enforced_before_the_host(self):
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        self.adapter._sync._call = Mock(return_value={'ok': True, 'result': {}})
        with self.assertRaises(DesktopNativeError) as caught:
            self.adapter.snapshot('native_1', str(other), 'always_ask')
        self.assertEqual(caught.exception.code, 'workspace_not_allowed')
        self.adapter._sync._call.assert_not_called()

    def test_native_opt_out_and_host_errors_are_explicit(self):
        raw = json.loads(self.control.read_text(encoding='utf-8'))
        raw['nativeTransport']['enabled'] = False
        self.control.write_text(json.dumps(raw), encoding='utf-8')
        with self.assertRaises(DesktopNativeError) as caught:
            self.adapter.snapshot('native_1', str(self.workspace), 'always_ask')
        self.assertEqual(caught.exception.code, 'native_disabled')

        raw['nativeTransport']['enabled'] = True
        self.control.write_text(json.dumps(raw), encoding='utf-8')
        self.adapter._sync._call = Mock(return_value={
            'ok': False, 'error': 'stale_owner', 'detail': 'generation changed'})
        with self.assertRaises(DesktopNativeError) as caught:
            self.adapter.send('native_1', str(self.workspace), 'always_ask', 'x',
                              'send_123456789')
        self.assertEqual(caught.exception.code, 'stale_owner')
        self.assertEqual(caught.exception.detail, 'generation changed')

    def test_status_is_token_and_path_free(self):
        status = self.adapter.status()
        rendered = json.dumps(status, ensure_ascii=False)
        self.assertEqual(status['state'], 'ready')
        self.assertNotIn(self.token, rendered)
        self.assertNotIn(str(self.control), rendered)
        self.assertNotIn(str(self.workspace), rendered)


if __name__ == '__main__':
    unittest.main()
