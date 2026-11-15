import json
import secrets
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge.desktop_sync import DesktopSyncAdapter

TOKEN = 'test-' + secrets.token_urlsafe(24)


class FakeSyncHandler(socketserver.StreamRequestHandler):
    def respond(self, value):
        self.wfile.write((json.dumps(value) + '\n').encode('utf-8'))

    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        try:
            auth = json.loads(line)
        except ValueError:
            self.respond({'ok': False, 'error': 'invalid_json'})
            return
        if auth.get('token') != self.server.token:
            self.respond({'ok': False, 'error': 'unauthorized'})
            return
        self.respond({'ok': True, 'authenticated': True})
        line = self.rfile.readline()
        if not line:
            return
        try:
            message = json.loads(line)
        except ValueError:
            self.respond({'ok': False, 'error': 'invalid_json'})
            return
        self.server.requests.append(message)
        if message.get('workspace') not in self.server.allowed:
            self.respond({'ok': False, 'error': 'workspace_not_allowed'})
            return
        self.respond({'ok': True, 'synced': [
            {'sessionId': value, 'changed': True, 'missing': False}
            for value in message.get('sessionIds') or []]})


class FakeSyncServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, token, allowed):
        self.token = token
        self.allowed = allowed
        self.requests = []
        super().__init__(('127.0.0.1', 0), FakeSyncHandler)


class DesktopSyncAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'work'
        self.other = self.root / 'other'
        self.workspace.mkdir()
        self.other.mkdir()
        self.server = FakeSyncServer(TOKEN, {str(self.workspace.resolve())})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.control = self.root / 'control.json'
        self.write_control(port=self.server.server_address[1])
        self.settings = {
            'desktop_sync_enabled': True,
            'desktop_sync_control_file': str(self.control),
            'desktop_sync_min_interval_seconds': 1,
        }
        self.adapter = None

    def tearDown(self):
        if self.adapter is not None:
            self.adapter.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def write_control(self, port=0, token=TOKEN, allowed=None, discovery=''):
        self.control.write_text(json.dumps({
            'host': '127.0.0.1', 'port': port, 'token': token,
            'discoveryPath': discovery or str(self.root / 'discovery.json'),
            'allowedWorkspaces': allowed if allowed is not None else [str(self.workspace)],
        }), encoding='utf-8')

    def make_adapter(self):
        self.adapter = DesktopSyncAdapter(settings_getter=lambda: self.settings)
        return self.adapter

    def test_default_off_never_connects(self):
        self.settings['desktop_sync_enabled'] = False
        adapter = self.make_adapter()
        result = adapter.request_sync(str(self.workspace), ['sess_1'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['state'], 'disabled')
        self.assertEqual(adapter.status()['state'], 'disabled')
        self.assertEqual(self.server.requests, [])

    def test_allowlist_rejects_other_workspace(self):
        adapter = self.make_adapter()
        result = adapter.request_sync(str(self.other), ['sess_1'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['state'], 'workspace_not_allowed')
        self.assertEqual(adapter.status()['state'], 'workspace_not_allowed')
        self.assertEqual(self.server.requests, [])

    def test_successful_sync_and_token_not_exposed(self):
        adapter = self.make_adapter()
        varied = str(self.workspace) + '/./'
        result = adapter.request_sync(varied, ['sess_1'], reason='test')
        self.assertTrue(result['accepted'])
        self.assertTrue(adapter.wait_idle(3))
        status = adapter.status()
        self.assertEqual(status['state'], 'synced')
        self.assertEqual(status['last_result']['synced'], 1)
        self.assertNotIn(TOKEN, json.dumps(status))
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]['op'], 'sync')
        self.assertEqual(self.server.requests[0]['sessionIds'], ['sess_1'])
        self.assertEqual(self.server.requests[0]['workspace'], str(self.workspace.resolve()))

    def test_requests_are_merged_per_workspace_and_throttled(self):
        adapter = self.make_adapter()
        adapter.request_sync(str(self.workspace), ['sess_1'])
        self.assertTrue(adapter.wait_idle(3))
        self.assertEqual(len(self.server.requests), 1)
        adapter.request_sync(str(self.workspace), ['sess_2'])
        adapter.request_sync(str(self.workspace), ['sess_3'])
        self.assertTrue(adapter.wait_idle(4))
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.requests[1]['sessionIds'], ['sess_2', 'sess_3'])

    def test_missing_control_and_token_are_status_only(self):
        adapter = self.make_adapter()
        self.settings['desktop_sync_control_file'] = str(self.root / 'missing.json')
        result = adapter.request_sync(str(self.workspace), ['sess_1'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['state'], 'config_missing')
        self.assertEqual(adapter.status()['state'], 'config_missing')

        self.write_control(token='short')
        self.settings['desktop_sync_control_file'] = str(self.control)
        result = adapter.request_sync(str(self.workspace), ['sess_1'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['state'], 'token_missing')
        self.assertEqual(self.server.requests, [])

    def test_unreachable_endpoint_does_not_raise(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        self.write_control(port=port)
        adapter = self.make_adapter()
        result = adapter.request_sync(str(self.workspace), ['sess_1'])
        self.assertTrue(result['accepted'])
        self.assertTrue(adapter.wait_idle(5))
        self.assertIn(adapter.status()['state'],
                      ('connect_failed', 'connection_closed'))
        self.assertEqual(self.server.requests, [])

    def test_discovery_file_is_used_when_port_is_zero(self):
        discovery = self.root / 'discovery.json'
        discovery.write_text(json.dumps({'port': self.server.server_address[1]}),
                             encoding='utf-8')
        self.write_control(port=0, discovery=str(discovery))
        adapter = self.make_adapter()
        adapter.request_sync(str(self.workspace), ['sess_1'])
        self.assertTrue(adapter.wait_idle(3))
        self.assertEqual(adapter.status()['state'], 'synced')
        self.assertEqual(len(self.server.requests), 1)

    def test_session_batch_is_capped_at_fifty(self):
        adapter = self.make_adapter()
        adapter.request_sync(str(self.workspace),
                             ['sess_' + str(index) for index in range(60)])
        self.assertTrue(adapter.wait_idle(3))
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.server.requests[0]['sessionIds']), 50)
        self.assertTrue(adapter.status()['truncated'])


if __name__ == '__main__':
    unittest.main()
