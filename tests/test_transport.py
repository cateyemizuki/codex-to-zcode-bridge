"""Loopback transport: request context/budget siblings and auth boundaries.

Runs a real loopback HTTP server in-process with a fake Bridge.  No engine,
host or network is involved; the port is ephemeral and bound to 127.0.0.1.
"""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import transport


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.started = 0
        self.stopped = 0
        self.client = None

    def call(self, name, arguments=None, context=None, budget_ms=None):
        self.calls.append({'name': name, 'arguments': arguments,
                           'context': context, 'budget_ms': budget_ms})
        if name == 'explode':
            raise ValueError('boom from the bridge')
        return {'name': name, 'context_seen': isinstance(context, dict)}

    def start_background(self):
        self.started += 1

    def stop_background(self):
        self.stopped += 1


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()
        self.fake = FakeBridge()
        self.browser = Mock()
        self.info = None
        self.patches = [
            patch('zcode_bridge.transport.state_dir', return_value=self.state),
            patch('zcode_bridge.transport.Bridge', return_value=self.fake),
            patch('zcode_bridge.transport.webbrowser.open', new=self.browser),
        ]
        for item in self.patches:
            item.start()
        self.thread = threading.Thread(target=transport.serve, daemon=True,
                                       name='bridge-test-server')
        self.thread.start()
        self.info = self.wait_for_server()

    def tearDown(self):
        if self.info:
            try:
                self.raw('POST', '/shutdown', b'{}', token=self.info['token'])
            except (OSError, ValueError):
                pass
        self.thread.join(5)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ helpers
    def wait_for_server(self, timeout=8):
        path = self.state / 'connection.json'
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                info = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                time.sleep(.02)
                continue
            try:
                status, body = self.raw('GET', '/health', token=info['token'],
                                        port=info['port'])
            except OSError:
                time.sleep(.02)
                continue
            if status == 200 and json.loads(body).get('instance') == info['instance']:
                return info
            time.sleep(.02)
        self.fail('loopback bridge server did not become healthy')

    def raw(self, method, path, body=None, token=None, origin=None, port=None):
        connection = http.client.HTTPConnection(
            '127.0.0.1', port or self.info['port'], timeout=5)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if origin:
            headers['Origin'] = origin
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        connection.close()
        return status, payload

    def post(self, name, arguments, context=None, budget_ms=None, token=None):
        body = {'name': name, 'arguments': arguments}
        if context is not None:
            body['context'] = context
        if budget_ms is not None:
            body['budget_ms'] = budget_ms
        return self.raw('POST', '/call', json.dumps(body).encode('utf-8'),
                        token=token or self.info['token'])

    # ------------------------------------------------------------------- tests
    def test_local_call_sends_context_and_budget_as_siblings(self):
        context = {'thread_id': 'chat-X', 'host_pipe': 'local-pipe'}
        result = transport.local_call('zcode_notify_inbox', {'event_id': 'evt_1'},
                                      context=context)
        self.assertTrue(result['context_seen'])
        call = self.fake.calls[0]
        self.assertEqual(call['name'], 'zcode_notify_inbox')
        self.assertEqual(call['arguments'], {'event_id': 'evt_1'})
        self.assertEqual(call['context']['thread_id'], 'chat-X')
        self.assertEqual(call['context']['host_pipe'], 'local-pipe')
        self.assertIsInstance(call['budget_ms'], int)
        self.assertGreater(call['budget_ms'], 0)
        self.assertNotIn('context', call['arguments'])
        self.assertNotIn('host_pipe', call['arguments'])

    def test_explicit_budget_is_forwarded(self):
        transport.local_call('zcode_wait', {'session_id': 'sess_x'},
                             context={'thread_id': 'chat-X'}, budget_ms=12345)
        self.assertEqual(self.fake.calls[0]['budget_ms'], 12345)

    def test_no_context_keeps_the_legacy_call_shape(self):
        transport.local_call('zcode_status')
        call = self.fake.calls[0]
        self.assertIsNone(call['context'])
        self.assertIsNone(call['budget_ms'])

    def test_open_settings_bypasses_the_bridge_and_respects_open_browser(self):
        result = transport.local_call('zcode_open_settings', {'open_browser': False},
                                      context={'thread_id': 'chat-X'})
        self.assertIn('/settings#', result['url'])
        self.assertNotIn('opened', result)
        self.assertEqual(self.fake.calls, [])
        self.browser.assert_not_called()

    def test_bearer_and_origin_are_still_enforced(self):
        status, _ = self.raw('POST', '/call', b'{"name":"zcode_status","arguments":{}}')
        self.assertEqual(status, 401)
        status, _ = self.raw('POST', '/call', b'{"name":"zcode_status","arguments":{}}',
                             token=self.info['token'], origin='http://evil.example')
        self.assertEqual(status, 401)
        status, body = self.raw(
            'POST', '/call', b'{"name":"zcode_status","arguments":{}}',
            token=self.info['token'],
            origin='http://127.0.0.1:%d' % self.info['port'])
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])
        self.assertEqual(self.fake.calls[-1]['name'], 'zcode_status')

    def test_oversized_body_is_rejected_without_reading_it(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.info['port'], timeout=5)
        connection.putrequest('POST', '/call')
        connection.putheader('Authorization', 'Bearer ' + self.info['token'])
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', str(3 * 1024 * 1024))
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertFalse(payload['ok'])

    def test_bridge_errors_are_reported_as_runtime_errors(self):
        with self.assertRaisesRegex(RuntimeError, 'boom from the bridge'):
            transport.local_call('explode', context={'thread_id': 'chat-X'})


if __name__ == '__main__':
    unittest.main()
