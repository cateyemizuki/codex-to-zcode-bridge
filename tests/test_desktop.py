import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'runtime'))
from zcode_bridge.desktop import session_visibility
from zcode_bridge.service import Bridge


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': str(self.root/'state')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_running_desktop_is_never_reopened_by_default(self):
        with patch('zcode_bridge.service.desktop_running', return_value=True), \
                patch('zcode_bridge.service.subprocess.Popen') as spawn:
            self.assertFalse(Bridge().launch(force=True)['workspace_open_requested'])
            spawn.assert_not_called()

    def test_workspace_open_requires_focus_acknowledgement(self):
        with patch('zcode_bridge.service.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'allow_focus'):
                Bridge().launch(workspace=str(self.root))
            spawn.assert_not_called()

    def test_explicit_open_forwards_native_absolute_workspace(self):
        executable = self.root/'ZCode.exe'
        executable.touch()
        with patch('zcode_bridge.service.config', return_value={'zcode_executable': str(executable)}), \
                patch('zcode_bridge.service.desktop_running', return_value=True), \
                patch('zcode_bridge.service.subprocess.Popen') as spawn:
            result = Bridge().launch(workspace=str(self.root/'sub/..'), allow_focus=True)
            self.assertEqual(spawn.call_args.args[0], [str(executable), '--open-workspace', str(self.root)])
            self.assertTrue(result['may_focus'])

    def test_creating_a_session_never_opens_a_project(self):
        bridge = Bridge()
        bridge.rpc = Mock(return_value={'session': {'sessionId':'sess_unit'}, 'projection': {},
            'runtime': {}, 'settings': {'model': {'current':{'providerId':'p','modelId':'m'}}}})
        bridge.launch = Mock()
        with patch('zcode_bridge.service.session_visibility', return_value={'indexed':False}):
            result = bridge.call('zcode_create_session', {'workspace': str(self.root), 'caller_permission_mode':'full_access',
                'model':{'providerId':'p','modelId':'m'}, 'thought_level':'high'})
        bridge.launch.assert_not_called()
        self.assertFalse(result['desktop']['indexed'])

    def create_catalogue(self):
        db = self.root/'.zcode/v2/tasks-index.sqlite'
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as connection:
            connection.execute('CREATE TABLE tasks(task_id TEXT, workspace_path TEXT, deleted INTEGER, '
                'archived INTEGER, cli_deleted INTEGER, provider TEXT, cli_created_at INTEGER)')
            connection.execute('INSERT INTO tasks VALUES(?,?,0,0,0,?,1)', ('sess_unit', str(self.root), 'glm'))
            connection.commit()
        return db

    def test_engine_persistence_does_not_imply_desktop_indexing(self):
        self.create_catalogue()
        result = session_visibility('sess_unregistered', str(self.root), home=self.root)
        self.assertFalse(result['indexed'])
        self.assertFalse(result['live_updates'])

    def test_index_and_workspace_registration_are_verified_separately(self):
        self.create_catalogue()
        settings = self.root/'.zcode/v2/setting.json'
        settings.write_text(json.dumps({'lastWorkspaceSession':[None, {'kind':'local', 'workspacePath':str(self.root)}]}), encoding='utf-8')
        result = session_visibility('sess_unit', str(self.root), home=self.root)
        self.assertTrue(result['indexed'])
        self.assertTrue(result['workspace_registered'])
        self.assertFalse(session_visibility('sess_unit', str(self.root/'different'), home=self.root)['indexed'])

    def test_deleted_task_is_not_reported_as_active_desktop_row(self):
        db = self.create_catalogue()
        with closing(sqlite3.connect(db)) as connection:
            connection.execute('UPDATE tasks SET cli_deleted=1')
            connection.commit()
        self.assertFalse(session_visibility('sess_unit', home=self.root)['indexed'])

    def test_absent_desktop_index_is_never_created(self):
        self.assertIsNone(session_visibility('sess_unit', home=self.root)['indexed'])
        self.assertFalse((self.root/'.zcode').exists())


if __name__ == '__main__':
    unittest.main()
