import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from zcode_bridge import storage, preferences
from zcode_bridge.service import Bridge
from zcode_bridge.protocol import Client
from zcode_bridge.desktop_native import DesktopNativeError
from zcode_bridge.desktop_instance import ensure_desktop, inspect_desktop, DesktopInstanceError


class DesktopOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': str(self.root / 'state')})
        self.env.start()
        self.bridge = Bridge()
        self.cfg = {**storage.defaults(), 'default_workspace': str(self.root),
                    'zcode_executable': str(self.root / 'ZCode.exe'),
                    'default_model': {'providerId': 'p', 'modelId': 'm'},
                    'default_thought_level': 'high'}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_defaults_and_legacy_config_migrate_to_desktop_only(self):
        self.assertEqual(storage.defaults()['default_transport'], 'desktop_native')
        self.assertTrue(storage.defaults()['desktop_native_enabled'])
        storage.write_json(storage.state_dir() / 'config.json', {
            'default_transport': 'engine', 'desktop_sync_enabled': True,
            'superagent_profiles': {'old': {'transport': 'engine'}}})
        cfg = storage.config()
        self.assertEqual(cfg['default_transport'], 'desktop_native')
        self.assertFalse(cfg['desktop_sync_enabled'])
        self.assertEqual(cfg['superagent_profiles']['old']['transport'], 'desktop_native')

    def test_no_low_level_engine_process_can_be_created(self):
        with patch('zcode_bridge.protocol.subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'independent_engine_disabled'):
                Client(self.cfg, Mock(), Mock())
            with self.assertRaisesRegex(DesktopNativeError, 'independent_engine_disabled'):
                self.bridge.backend()
            popen.assert_not_called()

    def test_explicit_engine_create_rejected_before_host_or_launch(self):
        self.bridge.desktop_native.create = Mock()
        with patch('zcode_bridge.service.config', return_value=self.cfg), \
                patch('zcode_bridge.desktop_instance.subprocess.Popen') as popen:
            with self.assertRaisesRegex(DesktopNativeError, 'independent_engine_disabled'):
                self.bridge.call('zcode_create_session', {'workspace': str(self.root),
                    'transport': 'engine', 'caller_permission_mode': 'full_access'})
            self.bridge.desktop_native.create.assert_not_called()
            popen.assert_not_called()

    def test_default_create_uses_only_native_host(self):
        self.bridge._native_create_session = Mock(return_value={'native': True})
        self.bridge.rpc = Mock(side_effect=AssertionError('headless RPC'))
        with patch('zcode_bridge.service.config', return_value=self.cfg):
            result = self.bridge.call('zcode_create_session', {
                'workspace': str(self.root), 'caller_permission_mode': 'full_access'})
        self.assertEqual(result, {'native': True})
        self.bridge._native_create_session.assert_called_once()
        self.bridge.rpc.assert_not_called()

    def test_delegate_engine_rejected_before_catalogue_or_session(self):
        self.bridge.desktop_native.model_catalog = Mock()
        with patch('zcode_bridge.service.config', return_value=self.cfg):
            with self.assertRaisesRegex(DesktopNativeError, 'independent_engine_disabled'):
                self.bridge.call('zcode_delegate', {
                    'workspace': str(self.root), 'task': 'Review docs', 'transport': 'engine',
                    'caller_permission_mode': 'full_access'},
                    {'thread_id': 'desktop-only-unit', 'thread_origin': 'turn_metadata'})
        self.bridge.desktop_native.model_catalog.assert_not_called()

    def test_native_host_identity_is_verified_and_read_proof_is_briefly_reused(self):
        self.bridge.desktop_native.probe = Mock(return_value={
            'ok': True, 'ready': True, 'nativeAvailable': True,
            'desktopExecutable': self.cfg['zcode_executable']})
        with patch('zcode_bridge.service.config', return_value=self.cfg), \
                patch('zcode_bridge.service.ensure_desktop', return_value={
                    'running': True, 'pid': 42, 'launched': False}) as ensure:
            self.bridge._ensure_native_desktop()
            self.bridge._ensure_native_desktop()
            ensure.assert_called_once()
            self.bridge._ensure_native_desktop(allow_start=True)
            self.assertEqual(ensure.call_count, 2)
        self.bridge._native_connection_proof = None
        self.bridge.desktop_native.probe.return_value['desktopExecutable'] = str(self.root / 'other.exe')
        with patch('zcode_bridge.service.config', return_value=self.cfg), \
                patch('zcode_bridge.service.ensure_desktop', return_value={'running': True, 'pid': 42, 'launched': False}):
            with self.assertRaisesRegex(DesktopNativeError, 'desktop_host_mismatch'):
                self.bridge._ensure_native_desktop()

    def test_status_reads_desktop_without_engine_or_start(self):
        self.bridge.rpc = Mock(side_effect=AssertionError('headless RPC'))
        self.bridge.desktop_native.status = Mock(return_value={'state': 'ready'})
        self.bridge.desktop_native.probe = Mock(return_value={
            'ok': True, 'ready': True, 'nativeAvailable': True,
            'desktopExecutable': self.cfg['zcode_executable']})
        with patch('zcode_bridge.service.config', return_value=self.cfg), \
                patch('zcode_bridge.service.inspect_desktop', return_value={'running': True, 'pid': 42}), \
                patch('zcode_bridge.desktop_instance.subprocess.Popen') as popen:
            result = self.bridge.call('zcode_status')
            self.assertTrue(result['connected'])
            self.assertIsNone(result['engine_pid'])
            self.assertEqual(result['desktop_pid'], 42)
            popen.assert_not_called()

    def test_models_are_read_from_native_catalogue(self):
        self.bridge.rpc = Mock(side_effect=AssertionError('headless RPC'))
        self.bridge.desktop_native.model_catalog = Mock(return_value={
            'available': [{'ref': self.cfg['default_model']}], 'selected': self.cfg['default_model']})
        with patch('zcode_bridge.service.config', return_value=self.cfg):
            result = self.bridge.call('zcode_models')
        self.assertEqual(result['transport'], 'desktop_native')
        self.assertEqual(self.bridge.desktop_native.model_catalog.call_args.args[1], 'read_only')
        self.bridge.rpc.assert_not_called()

    def test_unavailable_desktop_does_not_fall_back_to_engine(self):
        self.bridge.desktop_native.model_catalog = Mock(side_effect=DesktopNativeError('config_missing'))
        self.bridge.rpc = Mock(side_effect=AssertionError('headless RPC'))
        with patch('zcode_bridge.service.config', return_value=self.cfg):
            with self.assertRaisesRegex(DesktopNativeError, 'config_missing'):
                self.bridge.call('zcode_models')
        self.bridge.rpc.assert_not_called()

    def test_settings_hide_legacy_sync_and_ui_has_no_toggle(self):
        with patch('zcode_bridge.service.config', return_value=self.cfg):
            result = self.bridge.call('zcode_settings')
        self.assertFalse(any(key.startswith('desktop_sync_') for key in result))
        html = (ROOT / 'runtime/zcode_bridge/ui/settings.html').read_text(encoding='utf-8')
        for value in ('sync-enabled', 'sync-control', 'sync-interval'):
            self.assertNotIn(value, html)
        with patch('zcode_bridge.preferences.catalogue', return_value=(self.cfg, {})), \
                patch('zcode_bridge.preferences.native_status', return_value={}):
            panel = preferences.read()
        self.assertFalse(any(key.startswith('desktop_sync_') for key in panel['schema']['properties']))
        self.assertEqual(panel['schema']['properties']['default_transport']['enum'], ['desktop_native'])

    def test_existing_desktop_is_reused_without_popen(self):
        popen = Mock()
        result = ensure_desktop('formal', allow_start=True,
                                inspect=Mock(return_value={'running': True, 'pid': 42}), popen=popen)
        self.assertFalse(result['launched'])
        popen.assert_not_called()

    def test_missing_desktop_starts_only_configured_executable_once(self):
        executable = self.root / 'ZCode.exe'
        executable.touch()
        inspect = Mock(side_effect=[{'running': False, 'pid': None}, {'running': True, 'pid': 42}])
        popen = Mock()
        result = ensure_desktop(str(executable), allow_start=True, inspect=inspect, popen=popen)
        self.assertTrue(result['launched'])
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [str(executable.resolve())])

    def test_multiple_unknown_or_other_main_processes_fail_without_launch(self):
        executable = self.root / 'ZCode.exe'
        row = {'ProcessId': 1, 'ExecutablePath': str(executable), 'CommandLine': 'ZCode.exe'}
        cases = ([row, {**row, 'ProcessId': 2}], [{**row, 'ExecutablePath': str(self.root / 'copy.exe')}],
                 [{**row, 'CommandLine': None}])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(DesktopInstanceError):
                inspect_desktop(str(executable), rows=rows)

    def test_native_internal_script_workers_are_not_second_main_instances(self):
        executable = str(self.root / 'ZCode.exe')
        rows = [
            {'ProcessId': 1, 'ParentProcessId': 100, 'ExecutablePath': executable,
             'CommandLine': '"' + executable + '"'},
            {'ProcessId': 2, 'ParentProcessId': 1, 'ExecutablePath': executable,
             'CommandLine': '"' + executable + '" --type=utility'},
            {'ProcessId': 3, 'ParentProcessId': 2, 'ExecutablePath': executable,
             'CommandLine': '"' + executable + '" "' + str(self.root / 'zcode.cjs') + '" worker'},
        ]
        self.assertEqual(inspect_desktop(executable, rows=rows), {'running': True, 'pid': 1})

    def test_existing_desktop_workspace_request_does_not_launch_forwarder(self):
        with patch('zcode_bridge.service.config', return_value=self.cfg), \
                patch('zcode_bridge.service.ensure_desktop', return_value={
                    'running': True, 'already_running': True, 'launched': False, 'pid': 42}) as ensure:
            result = self.bridge.launch(workspace=str(self.root), allow_focus=True)
        self.assertTrue(result['needs_native_ui'])
        self.assertFalse(result['workspace_open_requested'])
        ensure.assert_called_once()
