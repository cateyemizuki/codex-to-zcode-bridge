"""Service-level notification behaviour: arming, leases, source binding, tools.

Uses a mocked engine RPC and a temporary bridge state directory.  The host
adapter is never created here, so no Codex process is started.
"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import notifications, storage
from zcode_bridge.service import Bridge

SID = 'sess_test'
CTX_A = {'thread_id': 'chat-A', 'thread_origin': 'turn_metadata'}
CTX_B = {'thread_id': 'chat-B', 'thread_origin': 'turn_metadata'}


def consent_row(**overrides):
    row = {'completion_notify': True, 'auto_continue': False,
           'user_authorized': True, 'permission_at_arm': 'full_access'}
    row.update(overrides)
    return row


def snapshot(mode='yolo', status='idle', count=1, jobs=None):
    projection = {'status': status, 'turnCount': count}
    if jobs is not None:
        projection['backgroundJobs'] = jobs
    return {'session': {'sessionId': SID}, 'projection': projection, 'runtime': {},
            'settings': {'permission': {'mode': mode},
                         'model': {'current': {'providerId': 'p', 'modelId': 'm'}}}}


def notify_args(key='key-1', **overrides):
    row = {
        'session_id': SID, 'content': 'do the task',
        'caller_permission_mode': 'full_access',
        'notify': {'completion_notify': True, 'auto_continue': False,
                   'user_authorized': True},
        'idempotency_key': key,
    }
    row.update(overrides)
    return row


class NotificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': self.tmp.name})
        self.env.start()
        executable = Path(self.tmp.name) / 'ZCode.exe'
        executable.touch()
        self.cfg = patch('zcode_bridge.service.config', return_value={
            **storage.defaults(),
            'zcode_executable': str(executable), 'auto_launch_desktop': False,
            'default_model': {'providerId': 'p', 'modelId': 'm'},
            'default_thought_level': 'high', 'default_workspace': self.tmp.name})
        self.cfg.start()
        self.visibility = patch('zcode_bridge.service.session_visibility',
                                return_value={'indexed': False})
        self.visibility.start()
        self.bridge = Bridge()
        self.bridge.client = Mock()
        self.bridge.client.child.pid = 4242
        self.bridge.resident.add(SID)
        self.status = 'idle'
        self.turn_count = 1
        self.jobs = []
        self.message_rows = []
        self.run_rows = []
        self.child_ids = []
        self.send_result = {'accepted': True, 'messageId': 'm-sent'}
        self.send_error = None
        self.send_calls = []
        self.calls = []
        self.bridge.rpc = Mock(side_effect=self.handle_rpc)
        self.bridge.backend = Mock(return_value=self.bridge.client)

    def tearDown(self):
        self.visibility.stop()
        self.cfg.stop()
        self.env.stop()
        self.tmp.cleanup()

    # ------------------------------------------------------------- fake engine
    def handle_rpc(self, method, params=None, deadline=None):
        params = params or {}
        self.calls.append((method, params))
        if method in ('session/read', 'session/resume'):
            return snapshot(self.effective_mode('yolo'), self.status, self.turn_count,
                            self.jobs)
        if method == 'session/setMode':
            return snapshot(self.effective_mode(params.get('mode')), self.status,
                            self.turn_count, self.jobs)
        if method == 'session/messages':
            return {'messages': copy.deepcopy(self.message_rows)}
        if method == 'v4/conversation/workflowRuns':
            return {'runs': copy.deepcopy(self.run_rows)}
        if method == 'v4/conversation/workflowRunEvents':
            return {'events': [], 'hasMore': False}
        if method == 'session/subagents':
            return {'childSessionIds': list(self.child_ids)}
        if method == 'session/send':
            self.send_calls.append(copy.deepcopy(params))
            if self.send_error is not None:
                raise self.send_error
            return copy.deepcopy(self.send_result)
        if method == 'session/stop':
            return {'stopped': True}
        if method == 'session/events':
            return {'events': []}
        return snapshot(self.effective_mode('yolo'), self.status, self.turn_count,
                        self.jobs)

    @staticmethod
    def effective_mode(mode):
        return 'build' if mode == 'plan' else mode

    def call(self, name, args, context=CTX_A):
        return self.bridge.call(name, args, context)

    def methods_called(self):
        return [method for method, _ in self.calls]

    def store_sub(self, thread='chat-A', state='running', anchor='m1', sid=SID):
        return self.bridge.notifications.create_subscription(
            sid, {'thread_id': thread, 'thread_origin': 'turn_metadata'},
            consent_row(), state=state, arm_basis='send_accepted',
            input_id='inp-' + thread, before_message_id='m0',
            turn_anchor=anchor, anchor_proof='send_result_message',
            engine_pid=4242)

    def test_start_background_wires_observer_runtime_adapter(self):
        from zcode_bridge.service import ObserverRuntimeAdapter
        self.bridge.host_waker = Mock()
        sub = self.store_sub()
        with patch('zcode_bridge.service.Observer.start'), \
                patch('zcode_bridge.service.DeliveryWorker.start'):
            self.bridge.start_background()
        try:
            self.assertIsInstance(self.bridge.observer.runtime, ObserverRuntimeAdapter)
            self.assertIsInstance(self.bridge.pending, dict)
            self.assertTrue(callable(self.bridge.observer.runtime.pending))
            evidence = self.bridge.observer.collect(sub)
            self.assertTrue(evidence['engine_ok'])
            self.assertEqual(evidence['status'], 'idle')
        finally:
            self.bridge.stop_background()

    def test_native_318_turn_started_completed_replay(self):
        from zcode_bridge.service import ObserverRuntimeAdapter
        messages = [
            {'info': {'messageId': 'm-timeline', 'role': 'assistant',
                      'semantics': {'kind': 'timeline_event', 'uiVisibility': 'visible'}},
             'parts': []},
            {'info': {'messageId': 'm-user-old', 'role': 'user',
                      'semantics': {'kind': 'user_prompt'}}, 'parts': []},
            {'info': {'messageId': 'm-answer-old', 'role': 'assistant',
                      'semantics': {'kind': 'assistant_response'}}, 'parts': []},
            {'info': {'messageId': 'm-user', 'role': 'user',
                      'semantics': {'kind': 'user_prompt'}}, 'parts': []},
            {'info': {'messageId': 'm-synth', 'role': 'user', 'synthetic': True,
                      'semantics': {'kind': 'system_reminder', 'uiVisibility': 'hidden'}},
             'parts': []},
            {'info': {'messageId': 'm-tool', 'role': 'assistant',
                      'semantics': {'kind': 'assistant_response'}}, 'parts': []},
            {'info': {'messageId': 'm-final', 'role': 'assistant',
                      'semantics': {'kind': 'assistant_response'}}, 'parts': []},
        ]
        events = [
            {'seq': 2, 'type': 'turn.started',
             'payload': {'inputId': 'inp-chat-A', 'messageId': 'm-user', 'turnNumber': 0}},
            {'seq': 61, 'type': 'turn.completed',
             'payload': {'inputId': 'inp-chat-A'}},
        ]
        self.turn_count = 0
        self.send_result = {'accepted': True}
        self.message_rows = []
        with patch('zcode_bridge.service.make_input_id', return_value='inp-chat-A'):
            sent = self.call('zcode_send', notify_args())
        self.turn_count = 1
        base = self.handle_rpc

        def rpc(method, params=None, deadline=None):
            params = params or {}
            if method == 'session/events':
                return {'events': [copy.deepcopy(event) for event in events
                                   if event['seq'] > int(params.get('afterSeq') or 0)]}
            if method == 'session/messages':
                rows = copy.deepcopy(messages)
                if params.get('afterMessageId'):
                    ids = [row['info'].get('messageId') for row in rows]
                    rows = (rows[ids.index(params['afterMessageId']) + 1:]
                            if params['afterMessageId'] in ids else [])
                if params.get('limit'):
                    rows = rows[-params['limit']:]
                return {'messages': rows}
            return base(method, params, deadline)

        self.bridge.rpc = Mock(side_effect=rpc)
        self.bridge.host_waker = Mock()
        with patch('zcode_bridge.service.Observer.start'), \
                patch('zcode_bridge.service.DeliveryWorker.start'):
            self.bridge.start_background()
        try:
            self.assertIsInstance(self.bridge.observer.runtime, ObserverRuntimeAdapter)
            sub = self.bridge.notifications.find_subscription(sent['subscription_id'])
            for _ in range(3):
                self.bridge.observer.observe(sub)
                sub = self.bridge.notifications.find_subscription(sent['subscription_id'])
            self.assertEqual(sub['state'], 'completed')
            self.assertEqual(sub['turn']['anchor_proof'], 'native_turn_started')
            self.assertEqual(sub['turn']['user_message_id'], 'm-user')
            kinds = [event['kind'] for event in self.bridge.notifications.all_events()]
            self.assertEqual(kinds.count('turn.completed'), 1)
        finally:
            self.bridge.stop_background()

    def complete_event(self, thread='chat-A', anchor='m1'):
        sub = self.store_sub(thread=thread, anchor=anchor)
        store = self.bridge.notifications
        store.mark_send_accepted(sub['subscription_id'], {'messageId': anchor})
        store.touch(sub['subscription_id'], state='running', state_reason='accepted')
        sample = {'now': 1000.0, 'engine_ok': True, 'pending': [], 'pending_ttl': 3600,
                  'turn_count': 1, 'status': 'idle', 'started': True, 'stable': True,
                  'anchor_proof': 'send_result_message', 'final_after_anchor': True,
                  'jobs': [], 'runs': [], 'children': [],
                  'fresh_pending_ids': [], 'poll_seconds': 1}
        fresh = store.find_subscription(sub['subscription_id'])
        verdict = notifications.decide(fresh, sample)
        appended = store.apply_decision(fresh['subscription_id'], fresh['revision'],
                                        verdict, sample)
        return sub, appended[0]

    # ------------------------------------------------------------- source/auth
    def test_notify_source_is_required(self):
        with self.assertRaisesRegex(ValueError, 'source_unavailable'):
            self.bridge.call('zcode_send', notify_args(), None)
        self.assertNotIn('session/send', self.methods_called())
        self.assertEqual(self.bridge.notifications.all_subscriptions(), [])

    def test_notify_requires_explicit_user_authorization(self):
        args = notify_args()
        args['notify'] = {'completion_notify': True, 'auto_continue': False}
        with self.assertRaisesRegex(ValueError, 'user_authorized'):
            self.call('zcode_send', args)
        self.assertNotIn('session/send', self.methods_called())
        self.assertEqual(self.bridge.notifications.all_subscriptions(), [])

    def test_notify_requires_a_known_caller_permission_mode(self):
        for value in (None, 'yolo', 'unknown'):
            args = notify_args()
            if value is None:
                args.pop('caller_permission_mode')
            else:
                args['caller_permission_mode'] = value
            with self.assertRaises(ValueError):
                self.call('zcode_send', args)
        self.assertNotIn('session/send', self.methods_called())

    def test_notify_send_requires_an_idempotency_key(self):
        args = notify_args()
        args.pop('idempotency_key')
        with self.assertRaisesRegex(ValueError, 'idempotency_key'):
            self.call('zcode_send', args)
        self.assertNotIn('session/send', self.methods_called())

    # ------------------------------------------------------------------ arming
    def test_arming_is_persisted_before_session_send(self):
        observed = []

        def check_inside_send(method, params=None, deadline=None):
            if method == 'session/send':
                observed.append(copy.deepcopy(
                    self.bridge.notifications.all_subscriptions()))
            return self.handle_rpc(method, params, deadline)

        self.bridge.rpc.side_effect = check_inside_send
        result = self.call('zcode_send', notify_args())
        self.assertEqual(len(observed), 1)
        row = observed[0][0]
        self.assertEqual(row['state'], 'arming')
        self.assertTrue(row['turn']['send_attempted'])
        self.assertEqual(result['state'], 'armed')
        self.assertTrue(result['accepted'])
        self.assertEqual(result['subscription_id'], row['subscription_id'])

    def test_accepted_is_not_completion_and_status_hides_source(self):
        result = self.call('zcode_send', notify_args())
        self.assertTrue(result['event_waiting'])
        sub = self.bridge.notifications.find_subscription(result['subscription_id'])
        self.assertEqual(sub['state'], 'armed')
        self.assertIsNone(sub['terminal_event_id'])
        self.assertEqual(self.bridge.notifications.all_events(), [])
        status = self.call('zcode_notify_status', {}, CTX_A)
        blob = json.dumps(status, ensure_ascii=False)
        self.assertEqual(status['subscriptions'][0]['state'], 'armed')
        for forbidden in ('chat-A', 'host_pipe', 'pipe', 'workspace'):
            self.assertNotIn(forbidden, blob)

    def test_send_not_accepted_is_terminal_and_releases_the_lease(self):
        self.send_result = {'accepted': False}
        result = self.call('zcode_send', notify_args())
        self.assertFalse(result['accepted'])
        self.assertEqual(result['state'], 'send_not_accepted')
        sub = self.bridge.notifications.find_subscription(result['subscription_id'])
        self.assertEqual(sub['state'], 'send_not_accepted')
        self.assertIsNone(self.bridge.notifications.session_owner(SID))
        self.assertEqual(self.bridge.notifications.all_events(), [])

    def test_send_rejects_auto_continue_in_read_only(self):
        args = notify_args()
        args['caller_permission_mode'] = 'read_only'
        args['notify'] = {'completion_notify': True, 'auto_continue': True,
                          'user_authorized': True}
        with self.assertRaisesRegex(ValueError, '只读'):
            self.call('zcode_send', args)
        self.assertNotIn('session/send', self.methods_called())
        self.assertEqual(self.bridge.notifications.all_subscriptions(), [])

    def test_send_exception_is_uncertain_and_retry_needs_explicit_flag(self):
        self.send_error = RuntimeError('transport exploded')
        result = self.call('zcode_send', notify_args())
        self.assertTrue(result['send_uncertain'])
        self.assertTrue(result['retry_required'])
        self.assertEqual(len(self.send_calls), 1)
        sub = self.bridge.notifications.find_subscription(result['subscription_id'])
        self.assertEqual(sub['state'], 'send_uncertain')
        self.assertEqual(
            [event['kind'] for event in self.bridge.notifications.all_events()],
            ['turn.send_uncertain'])
        self.assertIsNotNone(self.bridge.notifications.session_owner(SID))
        self.send_error = None
        again = self.call('zcode_send', notify_args())
        self.assertTrue(again['retry_required'])
        self.assertFalse(again['accepted'])
        self.assertEqual(len(self.send_calls), 1)
        retried = self.call('zcode_send', notify_args(retry=True))
        self.assertTrue(retried['accepted'])
        self.assertEqual(len(self.send_calls), 2)
        self.assertEqual(self.send_calls[0]['inputId'], self.send_calls[1]['inputId'])

    def test_duplicate_accepted_send_does_not_send_twice(self):
        first = self.call('zcode_send', notify_args())
        second = self.call('zcode_send', notify_args())
        self.assertTrue(second['duplicate'])
        self.assertTrue(second['accepted'])
        self.assertEqual(second['subscription_id'], first['subscription_id'])
        self.assertEqual(len(self.send_calls), 1)
        with self.assertRaisesRegex(ValueError, 'session_busy'):
            self.call('zcode_send', notify_args(key='other-key'))

    def test_arming_retry_reuses_the_persisted_input_id(self):
        self.bridge.notifications.begin_arming(
            SID, {'thread_id': 'chat-A', 'thread_origin': 'turn_metadata'},
            consent_row(), input_id='inp-fixed',
            idempotency_key_hash=hashlib.sha256(b'key-1').hexdigest(),
            engine_pid=4242)
        result = self.call('zcode_send', notify_args())
        self.assertTrue(result['accepted'])
        self.assertEqual(len(self.send_calls), 1)
        self.assertEqual(self.send_calls[0]['inputId'], 'inp-fixed')
        self.assertEqual(len(self.bridge.notifications.all_subscriptions()), 1)

    def test_manual_native_message_is_not_treated_as_our_send(self):
        self.bridge.notifications.begin_arming(
            SID, {'thread_id': 'chat-A', 'thread_origin': 'turn_metadata'},
            consent_row(), input_id='inp-1',
            idempotency_key_hash=hashlib.sha256(b'key-1').hexdigest(),
            engine_pid=4242)
        sub_id = self.bridge.notifications.all_subscriptions()[0]['subscription_id']
        self.bridge.notifications.claim_send(sub_id)
        self.message_rows = [{'info': {'id': 'm-manual', 'role': 'user'}}]
        result = self.call('zcode_send', notify_args())
        self.assertTrue(result['send_uncertain'])
        self.assertEqual(self.send_calls, [])
        self.message_rows = [{'info': {'id': 'm-real', 'role': 'user',
                                       'inputId': 'inp-1'}}]
        recovered = self.call('zcode_send', notify_args())
        self.assertTrue(recovered['accepted'])
        self.assertTrue(recovered.get('reconciled'))
        self.assertEqual(self.send_calls, [])

    # --------------------------------------------------------------- leases
    def test_other_chats_are_blocked_by_the_session_owner(self):
        self.store_sub(thread='chat-A', state='running')
        with self.assertRaisesRegex(ValueError, 'session_busy'):
            self.call('zcode_send', notify_args(key='key-b'), CTX_B)
        with self.assertRaisesRegex(ValueError, 'session_busy_foreign'):
            self.call('zcode_notify_subscribe', {
                'session_id': SID, 'caller_permission_mode': 'full_access',
                'user_authorized': True, 'user_message_id': 'm-user'}, CTX_B)
        with self.assertRaisesRegex(ValueError, 'session_busy_foreign'):
            self.call('zcode_stop', {
                'session_id': SID, 'caller_permission_mode': 'full_access'}, CTX_B)
        self.assertNotIn('session/stop', self.methods_called())

    def test_owner_stop_interrupts_only_the_owner_thread(self):
        sub = self.store_sub(thread='chat-A', state='running')
        with self.assertRaisesRegex(ValueError, 'session_busy_foreign'):
            self.call('zcode_stop', {
                'session_id': SID, 'caller_permission_mode': 'full_access'}, CTX_B)
        self.assertNotIn('session/stop', self.methods_called())
        result = self.call('zcode_stop', {
            'session_id': SID, 'caller_permission_mode': 'full_access'}, CTX_A)
        self.assertEqual(result, {'stopped': True})
        current = self.bridge.notifications.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'interrupted')
        events = self.bridge.notifications.all_events()
        self.assertEqual([event['kind'] for event in events], ['turn.interrupted'])
        self.assertEqual(events[0]['delivery']['state'], 'inbox_only')
        self.assertEqual([method for method in self.methods_called()
                          if method == 'session/stop'], ['session/stop'])

    def test_terminal_subscription_releases_the_session(self):
        sub = self.store_sub(thread='chat-A', state='running')
        cancelled = self.call('zcode_notify_cancel', {
            'subscription_id': sub['subscription_id'],
            'caller_permission_mode': 'full_access'}, CTX_A)
        self.assertTrue(cancelled['cancelled'])
        result = self.call('zcode_send', notify_args(key='key-b'), CTX_B)
        self.assertTrue(result['accepted'])
        self.assertEqual(len(self.send_calls), 1)

    def test_plain_send_is_blocked_by_an_active_owner(self):
        self.store_sub(thread='chat-A', state='running')
        with self.assertRaisesRegex(ValueError, 'session_busy'):
            self.call('zcode_send', {'session_id': SID, 'content': 'x',
                                     'caller_permission_mode': 'full_access'})
        self.assertNotIn('session/send', self.methods_called())

    def test_stop_without_owner_and_without_context_keeps_working(self):
        result = self.bridge.call('zcode_stop', {
            'session_id': SID, 'caller_permission_mode': 'full_access'})
        self.assertEqual(result, {'stopped': True})
        self.assertEqual(self.bridge.notifications.all_events(), [])

    # ------------------------------------------------------------- subscribe
    def test_subscribe_attach_does_not_change_permissions(self):
        self.status = 'running'
        self.turn_count = 3
        self.message_rows = [{'info': {'id': 'm-user', 'role': 'user'}}]
        result = self.call('zcode_notify_subscribe', {
            'session_id': SID, 'caller_permission_mode': 'full_access',
            'completion_notify': True, 'auto_continue': False,
            'user_authorized': True, 'user_message_id': 'm-user'})
        self.assertTrue(result['attached'])
        sub = self.bridge.notifications.find_subscription(result['subscription_id'])
        self.assertEqual(sub['state'], 'started')
        self.assertEqual(sub['arm_basis'], 'attach_running')
        self.assertEqual(sub['source']['thread_id'], 'chat-A')
        self.assertEqual(sub['consent']['permission_at_arm'], 'full_access')
        for method in ('session/setMode', 'session/setModel', 'session/setThoughtLevel'):
            self.assertNotIn(method, self.methods_called())
        duplicate = self.call('zcode_notify_subscribe', {
            'session_id': SID, 'caller_permission_mode': 'full_access',
            'user_authorized': True, 'user_message_id': 'm-user'})
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(len(self.bridge.notifications.all_subscriptions()), 1)

    def test_subscribe_requires_authorization_and_rejects_read_only_auto(self):
        self.status = 'running'
        self.message_rows = [{'info': {'id': 'm-user', 'role': 'user'}}]
        with self.assertRaisesRegex(ValueError, 'user_authorized'):
            self.call('zcode_notify_subscribe', {
                'session_id': SID, 'caller_permission_mode': 'full_access',
                'user_message_id': 'm-user'})
        with self.assertRaisesRegex(ValueError, '只读'):
            self.call('zcode_notify_subscribe', {
                'session_id': SID, 'caller_permission_mode': 'read_only',
                'user_authorized': True, 'auto_continue': True,
                'user_message_id': 'm-user'})
        self.assertEqual(self.bridge.notifications.all_subscriptions(), [])
        self.assertEqual(self.calls, [])

    def test_subscribe_without_a_running_turn_is_already_terminal(self):
        self.status = 'idle'
        with self.assertRaisesRegex(ValueError, 'already_terminal'):
            self.call('zcode_notify_subscribe', {
                'session_id': SID, 'caller_permission_mode': 'full_access',
                'user_authorized': True, 'user_message_id': 'm-user'})
        self.assertEqual(self.bridge.notifications.all_subscriptions(), [])

    # --------------------------------------------------------- inbox/cancel
    def test_cancel_is_thread_scoped_and_idempotent(self):
        sub = self.store_sub(thread='chat-A', state='running')
        self.assertFalse(self.call('zcode_notify_cancel', {
            'subscription_id': sub['subscription_id'],
            'caller_permission_mode': 'full_access'}, CTX_B)['cancelled'])
        self.assertEqual(self.bridge.notifications.find_subscription(
            sub['subscription_id'])['state'], 'running')
        self.assertTrue(self.call('zcode_notify_cancel', {
            'subscription_id': sub['subscription_id'],
            'caller_permission_mode': 'full_access'}, CTX_A)['cancelled'])
        self.assertTrue(self.call('zcode_notify_cancel', {
            'subscription_id': sub['subscription_id'],
            'caller_permission_mode': 'full_access'}, CTX_A)['cancelled'])
        self.assertEqual(self.call('zcode_notify_cancel', {
            'session_id': SID, 'caller_permission_mode': 'full_access'}, CTX_B)['cancelled'], 0)

    def test_inbox_and_ack_are_thread_scoped_and_omit_delivery(self):
        sub = self.store_sub(thread='chat-A', state='running')
        rows = self.bridge.notifications.emit_hint(
            SID, 'progress', 'hint-1', 'progress', subscription_id=sub['subscription_id'])
        event_id = rows[0]['event_id']
        seen = self.call('zcode_notify_inbox', {'event_id': event_id}, CTX_A)
        self.assertTrue(seen['found'])
        self.assertNotIn('delivery', seen['events'][0])
        hidden = self.call('zcode_notify_inbox', {'event_id': event_id}, CTX_B)
        self.assertFalse(hidden['found'])
        self.assertEqual(self.call('zcode_notify_ack', {
            'event_ids': [event_id], 'caller_permission_mode': 'full_access'},
            CTX_B)['acked'], 0)
        acked = self.call('zcode_notify_ack', {
            'event_ids': [event_id], 'caller_permission_mode': 'full_access'}, CTX_A)
        self.assertEqual(acked['acked'], 1)

    def test_notify_emit_targets_every_active_subscription_not_guessed(self):
        self.store_sub(thread='chat-A', state='running')
        self.store_sub(thread='chat-B', state='running')
        result = self.call('zcode_notify_emit', {
            'session_id': SID, 'kind': 'needs_verification',
            'hint_id': 'verify-1', 'reason_code': 'verification_requested',
            'caller_permission_mode': 'full_access'}, CTX_A)
        self.assertEqual(result['emitted'], 2)
        self.assertFalse(result['terminal'])
        with self.assertRaisesRegex(ValueError, 'kind'):
            self.call('zcode_notify_emit', {
                'session_id': SID, 'kind': 'turn.completed', 'hint_id': 'x',
                'reason_code': 'progress', 'caller_permission_mode': 'full_access'})
        with self.assertRaisesRegex(ValueError, 'hint_id'):
            self.call('zcode_notify_emit', {
                'session_id': SID, 'kind': 'progress', 'hint_id': 'x' * 65,
                'reason_code': 'progress', 'caller_permission_mode': 'full_access'})
        with self.assertRaisesRegex(ValueError, '没有匹配'):
            self.call('zcode_notify_emit', {
                'session_id': 'sess_none', 'kind': 'progress', 'hint_id': 'x',
                'reason_code': 'progress', 'caller_permission_mode': 'full_access'})

    # ------------------------------------------------------------------ wait
    def test_wait_without_a_subscription_never_confirms_success(self):
        self.status = 'idle'
        result = self.call('zcode_wait', {'session_id': SID})
        self.assertEqual(result['verdict'], 'unknown')
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['reason_code'], 'no_subscription')

    def test_wait_reads_the_persisted_terminal_truth(self):
        sub = self.store_sub(thread='chat-A', state='completed')
        result = self.call('zcode_wait', {'session_id': SID,
                                          'subscription_id': sub['subscription_id']}, CTX_A)
        self.assertEqual(result['verdict'], 'completed')
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['subscription_id'], sub['subscription_id'])
        with self.assertRaisesRegex(ValueError, 'not_found_for_source'):
            self.call('zcode_wait', {'session_id': SID,
                                     'subscription_id': sub['subscription_id']}, CTX_B)

    def test_wait_paused_is_not_confirmed(self):
        self.store_sub(thread='chat-A', state='paused')
        result = self.call('zcode_wait', {'session_id': SID}, CTX_A)
        self.assertEqual(result['verdict'], 'paused')
        self.assertFalse(result['confirmed'])

    def test_summary_awaiting_start_comes_from_persisted_subscriptions(self):
        sub = self.store_sub(thread='chat-A', state='arming')
        summary = self.bridge.summary(snapshot())
        self.assertTrue(summary['delivery']['awaiting_start'])
        self.assertEqual(summary['delivery']['subscriptions'], 1)
        self.bridge.notifications.touch(sub['subscription_id'], state='running')
        self.assertFalse(self.bridge.summary(snapshot())['delivery']['awaiting_start'])

    # --------------------------------------------------------------- privacy
    def test_state_file_and_events_store_no_task_text_or_model_ids(self):
        result = self.call('zcode_send', notify_args(content='SECRET TASK TEXT'))
        self.assertTrue(result['accepted'])
        raw = (Path(self.tmp.name) / 'notifications.json').read_text(encoding='utf-8')
        saved = json.loads(raw)
        self.assertNotIn('SECRET TASK TEXT', raw)
        self.assertNotIn('Authorization', raw)
        self.assertNotIn('providerId', raw)
        events = json.dumps(saved['events'], ensure_ascii=False)
        self.assertNotIn('chat-A', events)
        self.assertNotIn('pipe', events)
        self.assertEqual(saved['subscriptions'][result['subscription_id']]
                         ['source']['thread_id'], 'chat-A')

    def test_probe_without_host_adapter_does_not_claim_support(self):
        first = self.call('zcode_notify_probe', {}, CTX_A)
        self.assertEqual(first['capability'], 'not_probed')
        self.assertEqual(first['reason'], 'host_adapter_not_initialized')
        with self.assertRaisesRegex(ValueError, 'probe_rate_limited'):
            self.call('zcode_notify_probe', {}, CTX_A)


if __name__ == '__main__':
    unittest.main()
