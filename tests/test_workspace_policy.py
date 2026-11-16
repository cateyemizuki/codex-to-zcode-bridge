import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
sys.path.insert(0, str(ROOT))
from zcode_bridge.workspace_policy import WorkspacePolicy
from zcode_bridge.desktop_native import DesktopNativeAdapter, DesktopNativeError
from scripts import desktop_sync_shim as shim
from scripts.audit_release import inspect


def drive(letter, *parts):
    return letter + ':' + '\\' + '\\'.join(parts)


class WorkspacePolicyTests(unittest.TestCase):
    def test_default_blocks_only_c_drive_and_its_descendants(self):
        policy = WorkspacePolicy({})
        for value in (drive('C'), drive('c', 'work'), drive('C', 'work', '..', 'other'),
                      drive('C', 'work').replace('\\', '/'), '\\\\?\\' + drive('C', 'work')):
            with self.subTest(value=value):
                self.assertIsNone(policy.resolve(value))
        self.assertIsNotNone(policy.resolve(drive('D', 'work')))
        self.assertIsNotNone(policy.resolve(drive('E', 'work')))

    def test_blacklist_matches_subtrees_not_name_prefixes(self):
        root = drive('D', 'blocked')
        policy = WorkspacePolicy({'workspacePolicy': {'mode': 'blacklist', 'blockedPaths': [root]}})
        self.assertIsNone(policy.resolve(root + '\\child'))
        self.assertIsNotNone(policy.resolve(root + '-other'))
        self.assertIsNone(policy.resolve(drive('D', 'other', '..', 'BLOCKED', 'child')))

    def test_legacy_allowlist_is_exact_and_explicit_migration_overrides_it(self):
        root = drive('D', 'work')
        legacy = {'allowedWorkspaces': [root]}
        self.assertIsNotNone(WorkspacePolicy(legacy).resolve(root))
        self.assertIsNone(WorkspacePolicy(legacy).resolve(root + '\\child'))
        legacy['workspacePolicy'] = {'mode': 'blacklist'}
        self.assertIsNotNone(WorkspacePolicy(legacy).resolve(drive('E', 'work')))
        self.assertIsNone(WorkspacePolicy(legacy).resolve(drive('C')))

    def test_invalid_rules_do_not_default_to_open_access(self):
        for raw in ({'workspacePolicy': None}, {'workspacePolicy': {'mode': 'typo'}},
                    {'workspacePolicy': {'mode': 'blacklist', 'blockedPaths': 'bad'}},
                    {'workspacePolicy': {'mode': 'blacklist', 'blockedPaths': ['relative']}},
                    {'allowedWorkspaces': []}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                WorkspacePolicy(raw)

    def test_configure_defaults_and_migration_preserve_connection_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = shim.configure(Path(temporary))
            path = Path(report['config'])
            before = json.loads(path.read_text())
            self.assertEqual(before['workspacePolicy'], {'mode': 'blacklist', 'blockedPaths': [drive('C')]})
            self.assertNotIn(before['token'], json.dumps(report))
            shim.configure_native(path, True)
            before = json.loads(path.read_text())
            shim.configure_workspace_policy(path)
            after = json.loads(path.read_text())
            for key in ('token', 'nativeTransport', 'host', 'port', 'discoveryPath'):
                self.assertEqual(after[key], before[key])

    def test_adapter_reloads_policy_before_each_host_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / 'work'
            workspace.mkdir()
            report = shim.configure(Path(temporary) / 'state', policy_mode='blacklist', blocked_paths=[])
            control_path = Path(report['config'])
            shim.configure_native(control_path, True)
            adapter = DesktopNativeAdapter(settings_getter=lambda: {
                'desktop_native_enabled': True, 'desktop_native_control_file': str(control_path)})
            adapter._sync._call = Mock(return_value={'ok': True, 'result': {}})
            adapter.snapshot('sid', str(workspace), 'always_ask')
            adapter._sync._call.reset_mock()
            shim.configure_workspace_policy(control_path, blocked_paths=[str(workspace)])
            with self.assertRaises(DesktopNativeError) as caught:
                adapter.snapshot('sid', str(workspace), 'always_ask')
            self.assertEqual(caught.exception.code, 'workspace_not_allowed')
            adapter._sync._call.assert_not_called()

    def test_release_audit_allows_drive_roots_but_rejects_private_components(self):
        self.assertEqual(inspect('policy.py', json.dumps({'blocked': [drive('C')]}).encode()), [])
        self.assertTrue(inspect('policy.py', json.dumps({'blocked': [drive('C', 'Users', 'private')]}).encode()))

    def test_node_policy_and_shim_behavior(self):
        node = shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run([node, '--test', str(ROOT / 'tests/workspace_policy.test.mjs')],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
