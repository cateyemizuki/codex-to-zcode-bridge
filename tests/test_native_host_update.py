"""Owned-host updates preserve the original install and refuse live replacement."""
import json
from unittest.mock import patch
from test_desktop_native_install import FixtureCase, tool, sha


class NativeHostUpdateTests(FixtureCase):
    def older_owned_host(self):
        self.prepare()
        self.apply()
        host = self.app_dir / tool.HOST_REL
        previous = host.read_bytes() + b'\n// previous owned host revision\n'
        host.write_bytes(previous)
        ledger = self.ledger()
        ledger['hashes']['hostPatched'] = sha(previous)
        ledger['tree'] = tool.tree_facts(self.app_dir)
        self.ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
        return host, previous

    def test_update_rebuilds_from_original_and_preserves_private_control(self):
        host, previous = self.older_owned_host()
        control = self.control()
        original_archive = self.asar_backup.read_bytes()
        engine = self.engine.read_bytes()
        report = tool.update_host(self.target, self.state, probe=lambda: [])
        self.assertTrue(report['updated'])
        self.assertEqual(report['activation'], 'not_verified')
        self.assertFalse(report['restartsApplication'])
        self.assertNotEqual(host.read_bytes(), previous)
        self.assertEqual(self.control(), control)
        self.assertEqual(self.asar_backup.read_bytes(), original_archive)
        self.assertEqual(self.engine.read_bytes(), engine)
        self.assertTrue(tool.status(self.target, self.state)['ok'])

    def test_dry_run_is_read_only_and_live_zcode_refuses_application(self):
        host, previous = self.older_owned_host()
        ledger = self.ledger_path.read_bytes()
        probe = lambda: [{'pid': 777, 'path': str(self.exe)}]
        report = tool.update_host(self.target, self.state, dry_run=True, probe=probe)
        self.assertTrue(report['wouldUpdate'])
        self.assertTrue(report['requiresZCodeExit'])
        self.assertEqual(host.read_bytes(), previous)
        self.assertEqual(self.ledger_path.read_bytes(), ledger)
        with self.assertRaisesRegex(tool.NativeInstallError, 'close the installed ZCode'):
            tool.update_host(self.target, self.state, probe=probe)
        self.assertEqual(host.read_bytes(), previous)
        self.assertEqual(self.ledger_path.read_bytes(), ledger)

    def test_foreign_application_changes_are_not_overwritten(self):
        host, previous = self.older_owned_host()
        (self.app_dir / 'out/main/chunk.js').write_bytes(b'// unrelated user change\n')
        with self.assertRaisesRegex(tool.NativeInstallError, 'unchanged, owned'):
            tool.update_host(self.target, self.state, probe=lambda: [])
        self.assertEqual(host.read_bytes(), previous)

    def test_ledger_failure_restores_previous_host_and_ledger(self):
        host, previous = self.older_owned_host()
        ledger = self.ledger_path.read_bytes()
        with patch.object(tool, '_save_ledger', side_effect=OSError('simulated write failure')):
            with self.assertRaisesRegex(tool.NativeInstallError, 'previous host and ledger restored'):
                tool.update_host(self.target, self.state, probe=lambda: [])
        self.assertEqual(host.read_bytes(), previous)
        self.assertEqual(self.ledger_path.read_bytes(), ledger)
        self.assertTrue(tool.status(self.target, self.state)['ok'])

    def test_original_rollback_remains_valid_after_update(self):
        self.older_owned_host()
        original_archive = self.asar_backup.read_bytes()
        engine = self.engine.read_bytes()
        tool.update_host(self.target, self.state, probe=lambda: [])
        tool.rollback(self.target, self.state, probe=lambda: [])
        self.assertEqual(self.asar.read_bytes(), original_archive)
        self.assertEqual(self.engine.read_bytes(), engine)
        self.assertFalse(self.app_dir.exists())
