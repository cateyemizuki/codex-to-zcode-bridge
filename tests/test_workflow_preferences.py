"""Workflow policy settings are local, configurable, and preserve private control."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import preferences, storage
from zcode_bridge.desktop_native import DesktopNativeAdapter


class WorkflowPreferenceTests(unittest.TestCase):
    def test_native_policy_write_needs_no_codex_connection_and_preserves_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'control.json'
            original = {'token': 'private-local-token', 'nativeTransport': {
                'enabled': True, 'ownershipPath': str(Path(tmp) / 'owned.json')}}
            path.write_text(json.dumps(original), encoding='utf-8')
            adapter = DesktopNativeAdapter(settings_getter=lambda: {
                'desktop_native_enabled': True, 'desktop_native_control_file': str(path)})
            result = adapter.save_workflow_authorization('manual')
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(result['configuration_saved'])
            self.assertEqual(saved['workflowAuthorization'], {'mode': 'manual'})
            self.assertEqual(saved['token'], original['token'])
            self.assertEqual(saved['nativeTransport'], original['nativeTransport'])
            self.assertNotIn('token', result)

    def test_default_is_automatic_and_native_preferences_expose_manual_choice(self):
        cfg = storage.defaults()
        self.assertEqual(cfg['workflow_authorization_mode'], 'automatic')
        with patch.object(preferences, 'catalogue', return_value=(cfg, {})), \
                patch.object(preferences, 'native_status', return_value={}):
            result = preferences.read()
        self.assertEqual(result['schema']['properties']['workflow_authorization_mode']['enum'],
                         ['automatic', 'manual'])
        self.assertEqual(result['values']['workflow_authorization_mode'], 'automatic')

    def test_configuration_rejects_unknown_authorization_modes(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': tmp}), \
                patch.object(storage, 'paths', return_value={
                    'default_workspace': tmp, 'zcode_executable': '', 'engine_entry': '',
                    'builtin_provider_file': '', 'personal_provider_file': '', 'node_executable': ''}):
            saved = storage.save_config({'workflow_authorization_mode': 'manual'})
            self.assertEqual(saved['workflow_authorization_mode'], 'manual')
            with self.assertRaisesRegex(ValueError, '授权模式'):
                storage.save_config({'workflow_authorization_mode': 'unrestricted'})


if __name__ == '__main__':
    unittest.main()
