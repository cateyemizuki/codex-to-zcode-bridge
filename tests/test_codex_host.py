"""Host wake adapter and request-source extraction.

The stdio host is a fake MCP server written into a temporary directory; it
speaks the same public JSON-RPC handshake the real codex-app-tools server does
and records every request.  No real Codex Desktop, pipe or network is used.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from zcode_bridge import mcp
from zcode_bridge.delivery import DeliveryWorker, wake_prompt
from zcode_bridge.host_wake import HostSession, HostWakeAdapter
from zcode_bridge.notifications import NotificationStore, decide

SID = 'sess_test'
FAKE_SERVER = r'''
import json, os, sys

LOG = os.environ.get('FAKE_HOST_LOG')
MODE = os.environ.get('FAKE_HOST_MODE', 'normal')


def record(row):
    if not LOG:
        return
    with open(LOG, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


for raw in sys.stdin.buffer:
    try:
        message = json.loads(raw)
    except ValueError:
        continue
    record({'request': message,
            'env_thread': os.environ.get('CODEX_THREAD_ID', ''),
            'env_pipe': os.environ.get('CODEX_APP_TOOLS_PIPE_PATH', '')})
    method = message.get('method')
    ident = message.get('id')
    if method == 'initialize':
        if MODE == 'silent':
            continue
        if MODE == 'handshake_fail':
            send({'jsonrpc': '2.0', 'id': ident,
                  'error': {'code': -32000, 'message': 'handshake rejected'}})
            continue
        send({'jsonrpc': '2.0', 'id': ident, 'result': {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'serverInfo': {'name': 'fake-codex-host', 'version': '1.0'}}})
        continue
    if method == 'notifications/initialized':
        continue
    if method == 'tools/list':
        if MODE == 'pipe_error':
            send({'jsonrpc': '2.0', 'id': ident, 'error': {
                'code': -32603,
                'message': 'CODEX_APP_TOOLS_PIPE_PATH is unavailable'}})
            continue
        tools = [] if MODE == 'missing_tool' else [{'name': 'send_message_to_thread'}]
        send({'jsonrpc': '2.0', 'id': ident, 'result': {'tools': tools}})
        continue
    if method == 'tools/call':
        send({'jsonrpc': '2.0', 'id': ident,
              'result': {'content': [{'type': 'text', 'text': 'accepted by fake host'}]}})
        continue
    if ident is not None:
        send({'jsonrpc': '2.0', 'id': ident, 'result': {}})
'''


class FakeChild:
    """Minimal Popen stand-in for cleanup tests; never starts a process."""

    def __init__(self, wait_forever=False):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b'')
        self.stderr = io.BytesIO()
        self.returncode = None if wait_forever else 0
        self.wait_forever = wait_forever
        self.terminated = []
        self.killed = []
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.wait_forever and not self.killed:
            raise subprocess.TimeoutExpired('fake-host', timeout)
        return self.returncode or 0

    def terminate(self):
        self.terminated.append(True)

    def kill(self):
        self.killed.append(True)
        self.returncode = 0


class FakeHostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / 'fake_host.py'
        self.script.write_text(FAKE_SERVER, encoding='utf-8', newline='\n')
        self.log = self.root / 'host.log'

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self, mode='normal', timeout=5, pipe='fake-pipe'):
        env = {**os.environ, 'FAKE_HOST_MODE': mode, 'FAKE_HOST_LOG': str(self.log),
               'CODEX_APP_TOOLS_PIPE_PATH': pipe, 'CODEX_THREAD_ID': 'stale-chat',
               'FAKE_MARKER': 'kept'}
        return HostWakeAdapter(env=env, command=[sys.executable, str(self.script)],
                               total_timeout=timeout, request_timeout=timeout)

    def rows(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in
                self.log.read_text(encoding='utf-8').splitlines() if line.strip()]

    def requests(self, method=None):
        return [row['request'] for row in self.rows()
                if method is None or row['request'].get('method') == method]


class ProbeTests(FakeHostCase):
    def test_probe_handshakes_and_finds_the_wake_tool(self):
        result = self.adapter().probe(5)
        self.assertEqual(result['state'], 'supported')
        methods = [request['method'] for request in self.requests()]
        self.assertEqual(methods[:3], ['initialize', 'notifications/initialized', 'tools/list'])
        for row in self.rows():
            self.assertEqual(row['env_thread'], '')
            self.assertEqual(row['env_pipe'], 'fake-pipe')

    def test_missing_wake_tool_is_reported_as_unsupported(self):
        result = self.adapter('missing_tool').probe(5)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(result['error_code'], 'missing_tool')

    def test_pipe_error_is_pipe_unavailable_not_missing_tool(self):
        result = self.adapter('pipe_error').probe(5)
        self.assertEqual(result['state'], 'pipe_unavailable')
        self.assertEqual(result['error_code'], 'pipe_unavailable')

    def test_handshake_error_is_not_reported_as_supported(self):
        result = self.adapter('handshake_fail').probe(5)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(result['error_code'], 'handshake_failed')

    def test_missing_runtime_paths_are_host_unavailable(self):
        adapter = HostWakeAdapter(env={'CODEX_APP_TOOLS_PIPE_PATH': 'fake-pipe'})
        self.assertEqual(adapter.probe(5)['state'], 'host_unavailable')
        self.assertEqual(adapter.send('chat-A', SID, 'evt_1', 'x')['error_code'],
                         'host_unavailable')

    def test_shell_string_commands_are_rejected(self):
        with self.assertRaises(ValueError):
            HostWakeAdapter(command='node server.mjs')
        with self.assertRaises(Exception) as caught:
            HostSession([], {})
        self.assertEqual(getattr(caught.exception, 'error_code', ''), 'host_unavailable')


class SendContractTests(FakeHostCase):
    def test_send_requires_a_source_thread(self):
        result = self.adapter().send('', SID, 'evt_1', 'prompt')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'source_mismatch')
        self.assertEqual(self.requests(), [])

    def test_tools_call_carries_exact_meta_arguments_and_minimal_prompt(self):
        prompt = wake_prompt(SID, 'evt_1')
        result = self.adapter().send('chat-A', SID, 'evt_1', prompt)
        self.assertTrue(result['ok'])
        calls = self.requests('tools/call')
        self.assertEqual(len(calls), 1)
        params = calls[0]['params']
        self.assertEqual(params['name'], 'send_message_to_thread')
        self.assertEqual(params['_meta'], {'openai/threadId': 'chat-A'})
        self.assertEqual(set(params['arguments']), {'threadId', 'prompt'})
        self.assertEqual(params['arguments']['threadId'], 'chat-A')
        self.assertEqual(params['arguments']['prompt'], prompt)
        self.assertNotIn('model', params['arguments'])
        self.assertNotIn('thinking', params['arguments'])
        self.assertNotIn('hostId', params['arguments'])
        self.assertNotIn('SECRET', prompt)

    def test_real_host_id_is_forwarded_but_never_fabricated(self):
        prompt = wake_prompt(SID, 'evt_2')
        self.adapter().send('chat-A', SID, 'evt_2', prompt, host={'host_id': 'host-9'})
        arguments = self.requests('tools/call')[0]['params']['arguments']
        self.assertEqual(arguments['hostId'], 'host-9')
        self.log.unlink()
        self.adapter().send('chat-A', SID, 'evt_2', prompt, host_id='host-10')
        arguments = self.requests('tools/call')[0]['params']['arguments']
        self.assertEqual(arguments['hostId'], 'host-10')

    def test_silent_host_times_out_and_is_torn_down(self):
        started = time.monotonic()
        result = self.adapter('silent', timeout=1).send(
            'chat-A', SID, 'evt_3', wake_prompt(SID, 'evt_3'))
        elapsed = time.monotonic() - started
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'timeout')
        self.assertTrue(result['unsupported'])
        self.assertLess(elapsed, 15)


class ProcessContractTests(unittest.TestCase):
    def test_popen_is_an_argument_array_without_a_shell(self):
        child = FakeChild()
        adapter = HostWakeAdapter(env={'CODEX_APP_TOOLS_PIPE_PATH': 'fake-pipe'},
                                  command=[sys.executable, 'server.mjs'])
        with patch('zcode_bridge.host_wake.subprocess.Popen', return_value=child) as popen:
            with adapter._session(None):
                pass
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['stdin'], subprocess.PIPE)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        expected = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self.assertEqual(kwargs['creationflags'], expected)
        self.assertEqual(adapter.creationflags, expected)

    def test_stuck_host_is_terminated_then_killed_and_pipes_close(self):
        child = FakeChild(wait_forever=True)
        session = HostSession(['fake'], {})
        with patch('zcode_bridge.host_wake.subprocess.Popen', return_value=child):
            session.start()
            session.close()
        self.assertEqual(child.terminated, [True])
        self.assertEqual(child.killed, [True])
        self.assertFalse(session.reader.is_alive())
        self.assertTrue(child.stdin.closed)

    def test_clean_exit_does_not_send_signals(self):
        child = FakeChild()
        session = HostSession(['fake'], {})
        with patch('zcode_bridge.host_wake.subprocess.Popen', return_value=child):
            session.start()
            session.close()
        self.assertEqual(child.terminated, [])
        self.assertEqual(child.killed, [])

    def test_write_after_host_exit_is_a_handshake_failure(self):
        child = FakeChild()
        session = HostSession(['fake'], {})
        with patch('zcode_bridge.host_wake.subprocess.Popen', return_value=child):
            session.start()
            child.returncode = 1
            with self.assertRaises(Exception) as caught:
                session._write({'id': 1})
            self.assertEqual(getattr(caught.exception, 'error_code', ''), 'handshake_failed')


class SourceExtractionTests(unittest.TestCase):
    def test_request_metadata_wins_over_the_environment_fallback(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'env-chat'}, clear=False):
            context = mcp.normalize_context({
                'x-codex-turn-metadata': {'thread_id': 'request-chat',
                                          'thread_origin': 'turn_metadata'}})
        self.assertEqual(context['thread_id'], 'request-chat')
        self.assertEqual(context['thread_origin'], 'turn_metadata')

    def test_openai_thread_id_is_used_when_present(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'env-chat'}, clear=False):
            context = mcp.normalize_context({'openai/threadId': 'meta-chat'})
        self.assertEqual(context['thread_id'], 'meta-chat')
        self.assertEqual(context['thread_origin'], 'openai_thread_id')

    def test_conflicting_sources_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'source_conflict'):
            mcp.normalize_context({'x-codex-turn-metadata': {'thread_id': 'a'},
                                   'openai/threadId': 'b'})

    def test_missing_metadata_uses_the_process_environment_or_nothing(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'env-chat'}, clear=False):
            self.assertEqual(mcp.normalize_context({})['thread_id'], 'env-chat')
            self.assertEqual(mcp.normalize_context({})['thread_origin'], 'env_fallback')
        env = {key: value for key, value in os.environ.items()
               if key not in ('CODEX_THREAD_ID', 'CODEX_APP_TOOLS_PIPE_PATH')}
        with patch.dict(os.environ, env, clear=True):
            self.assertNotIn('thread_id', mcp.normalize_context({}))
            with self.assertRaisesRegex(ValueError, 'source_unavailable'):
                mcp.handle({'method': 'tools/call', 'params': {
                    'name': 'zcode_notify_inbox', 'arguments': {}, '_meta': {}}})
            with self.assertRaisesRegex(ValueError, 'source_unavailable'):
                mcp.handle({'method': 'tools/call', 'params': {
                    'name': 'zcode_send', 'arguments': {
                        'session_id': SID, 'content': 'x',
                        'caller_permission_mode': 'full_access',
                        'notify': {'user_authorized': True}}, '_meta': {}}})

    def test_host_launch_context_is_a_sibling_not_a_tool_argument(self):
        env = {**os.environ, 'CODEX_APP_TOOLS_PIPE_PATH': 'local-pipe',
               'CODEX_APP_RUNTIME_CLI_PATH': 'runtime-cli',
               'CODEX_MCP_NODE_PATH': 'node-bin'}
        with patch.dict(os.environ, env, clear=False), \
                patch('zcode_bridge.mcp.local_call', return_value={}) as call:
            mcp.handle({'method': 'tools/call', 'params': {
                'name': 'zcode_notify_inbox', 'arguments': {'event_id': 'evt_1'},
                '_meta': {'x-codex-turn-metadata': {'thread_id': 'chat-X'}}}})
        _, kwargs = call.call_args
        self.assertEqual(kwargs['context']['thread_id'], 'chat-X')
        self.assertEqual(kwargs['context']['host_pipe'], 'local-pipe')
        self.assertEqual(kwargs['context']['host_runtime_cli'], 'runtime-cli')
        self.assertNotIn('host_pipe', call.call_args.args[1])

    def test_open_settings_ignores_request_metadata(self):
        with patch('zcode_bridge.mcp.local_call', return_value={'default_model': {},
                                                                'default_workspace': '.'}) as call:
            mcp.handle({'method': 'tools/call', 'params': {
                'name': 'zcode_open_settings', 'arguments': {},
                '_meta': {'x-codex-turn-metadata': {'thread_id': 'chat-X'}}}})
        call.assert_called_once_with('zcode_settings')


def fixed_clock():
    return 1000.0


def completed_event(store, thread='chat-A', sid=SID, anchor='m1'):
    sub = store.create_subscription(
        sid, {'thread_id': thread, 'thread_origin': 'turn_metadata'},
        {'completion_notify': True, 'auto_continue': False,
         'user_authorized': True, 'permission_at_arm': 'full_access'},
        state='running', arm_basis='send_accepted', input_id='inp-' + thread,
        before_message_id='m0', turn_anchor=anchor,
        anchor_proof='send_result_message', engine_pid=4242)
    store.mark_send_accepted(sub['subscription_id'], {'messageId': anchor})
    store.touch(sub['subscription_id'], state='running', state_reason='accepted')
    sample = {'now': fixed_clock(), 'engine_ok': True, 'pending': [], 'pending_ttl': 3600,
              'turn_count': 1, 'status': 'idle', 'started': True, 'stable': True,
              'anchor_proof': 'send_result_message', 'final_after_anchor': True,
              'jobs': [], 'runs': [], 'children': [], 'fresh_pending_ids': [],
              'poll_seconds': 1}
    fresh = store.find_subscription(sub['subscription_id'])
    verdict = decide(fresh, sample)
    appended = store.apply_decision(fresh['subscription_id'], fresh['revision'],
                                    verdict, sample)
    return sub, appended[0]


class DeliveryThroughFakeHostTests(FakeHostCase):
    def setUp(self):
        super().setUp()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.store = NotificationStore(self.state / 'notifications.json',
                                       clock=fixed_clock)

    def test_wake_reaches_only_the_bound_thread_and_is_not_repeated(self):
        _, event = completed_event(self.store)
        adapter = self.adapter()
        worker = DeliveryWorker(self.store, waker=adapter,
                                host_context=lambda: {'pipe': 'fake-pipe'},
                                clock=fixed_clock)
        self.assertEqual(worker.tick(), 1)
        calls = self.requests('tools/call')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['params']['_meta'], {'openai/threadId': 'chat-A'})
        self.assertEqual(calls[0]['params']['arguments']['threadId'], 'chat-A')
        self.assertEqual(calls[0]['params']['arguments']['prompt'],
                         wake_prompt(SID, event['event_id']))
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'delivered')
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(len(self.requests('tools/call')), 1)

    def test_two_chats_never_cross_wires(self):
        _, event_a = completed_event(self.store, thread='chat-A')
        _, event_b = completed_event(self.store, thread='chat-B', sid='sess_other')
        worker = DeliveryWorker(self.store, waker=self.adapter(),
                                host_context=lambda: {'pipe': 'fake-pipe'},
                                clock=fixed_clock)
        self.assertEqual(worker.tick(), 2)
        routes = {}
        for call in self.requests('tools/call'):
            params = call['params']
            routes[params['arguments']['threadId']] = params['_meta']['openai/threadId']
        self.assertEqual(routes, {'chat-A': 'chat-A', 'chat-B': 'chat-B'})
        prompts = [call['params']['arguments']['prompt']
                   for call in self.requests('tools/call')]
        self.assertTrue(any(event_a['event_id'] in prompt for prompt in prompts))
        self.assertTrue(any(event_b['event_id'] in prompt for prompt in prompts))

    def test_missing_wake_tool_keeps_the_event_queued(self):
        _, event = completed_event(self.store)
        worker = DeliveryWorker(self.store, waker=self.adapter('missing_tool'),
                                host_context=lambda: {'pipe': 'fake-pipe'},
                                clock=fixed_clock)
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(self.requests('tools/call'), [])
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'pending')
        self.assertEqual(self.store.data['host_capability']['state'], 'unsupported')
        self.assertEqual(self.store.data['host_capability']['error_code'], 'missing_tool')

    def test_handshake_failure_keeps_the_event_pending(self):
        _, event = completed_event(self.store)
        worker = DeliveryWorker(self.store, waker=self.adapter('handshake_fail'),
                                host_context=lambda: {'pipe': 'fake-pipe'},
                                clock=fixed_clock)
        self.assertEqual(worker.tick(), 0)
        delivery = self.store._events()[event['event_id']]['delivery']
        self.assertEqual(delivery['state'], 'pending')
        self.assertEqual(delivery['last_error_code'], 'handshake_failed')
        self.assertEqual(self.store.data['host_capability']['error_code'],
                         'handshake_failed')


if __name__ == '__main__':
    unittest.main()
