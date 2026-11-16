"""Native forward arrays, ready snapshot tails and existing-only child reads.

3.18.2 cPi/X4c filters the forward transcript before applying its limit; a
snapshot also contains hidden synthetic rows. The real Bridge and native
adapter run against an in-memory Host channel with those distinct shapes.
"""
import copy
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import storage
from zcode_bridge.desktop_native import DesktopNativeError
from zcode_bridge.identity import is_visible_message, message_identity
from bridge_fixtures import MockBridge as Bridge


def message(index, role='assistant', hidden=False):
    return {'info': {
        'messageId': 'msg_test_' + str(index), 'role': role,
        **({'synthetic': True, 'semantics': {'uiVisibility': 'hidden'}} if hidden else {})},
        'parts': [{'type': 'text', 'text': 'synthetic test content'}]}


class NativeTranscriptPaginationTests(unittest.TestCase):
    SID = 'sess_native_transcript'
    CHILD = 'sess_native_child'
    CONTEXT = {'thread_id': 'test-transcript-chat', 'thread_origin': 'turn_metadata'}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = str(Path(temporary.name).resolve())
        env = patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': temporary.name})
        env.start()
        self.addCleanup(env.stop)
        cfg = patch('zcode_bridge.service.config', return_value={
            **storage.defaults(), 'auto_launch_desktop': False,
            'default_workspace': self.workspace, 'desktop_sync_enabled': False})
        cfg.start()
        self.addCleanup(cfg.stop)
        visibility = patch('zcode_bridge.service.session_visibility',
                           return_value={'indexed': False})
        visibility.start()
        self.addCleanup(visibility.stop)
        self.visible = [message(i, 'user' if i in (0, 127) else 'assistant')
                        for i in range(129)]
        # Same raw/visible cardinalities as the reported real transcript:
        # 281 snapshot rows, 129 forward-visible rows, with hidden tail metadata.
        self.rows = ([message('hidden_' + str(i), 'user', True) for i in range(151)]
                     + copy.deepcopy(self.visible) + [message('hidden_tail', 'user', True)])
        self.snapshot_error = None
        self.messages_error = None
        self.child_error = None
        self.bridge = Bridge()
        self.bridge.sessions[self.SID] = {
            'transport': 'desktop_native', 'workspace': self.workspace,
            'caller_permission_mode': 'always_ask',
            'native_runtime': {'identity': 'test-runtime', 'generation': 1}}
        native = self.bridge.desktop_native
        native._control = Mock(return_value={})
        native._workspace = Mock(side_effect=lambda _control, workspace: workspace)
        native._sync._call = Mock(side_effect=self.host_call)
        self.channel = native._sync._call
        self.bridge._rpc_once = Mock(side_effect=AssertionError('independent engine used'))
        self.bridge.backend = Mock(side_effect=AssertionError('engine started or resumed'))

    def snapshot(self, sid=None):
        return {'session': {'sessionId': sid or self.SID,
                            'workspace': {'workspacePath': self.workspace}},
                'projection': {'status': 'idle', 'turnCount': 2,
                               'backgroundJobs': [], 'pendingInteractions': []},
                'messages': copy.deepcopy(self.rows), 'runtime': {'pendingRequestIds': []}}

    def host_call(self, _control, fields):
        op = fields['op']
        if fields['session_id'] != self.SID:
            return {'ok': False, 'error': 'native_session_unowned',
                    'detail': 'session is not owned by this transport'}
        if op.startswith('native_child_'):
            if self.child_error:
                return copy.deepcopy(self.child_error)
            if fields['child_session_id'] != self.CHILD or fields.get('run_id') != 'run-test':
                return {'ok': False, 'error': 'child_not_verified_for_parent',
                        'detail': 'child relationship was not verified'}
        if op in ('native_snapshot', 'native_child_snapshot'):
            if self.snapshot_error:
                return copy.deepcopy(self.snapshot_error)
            sid = fields.get('child_session_id') or fields['session_id']
            return {'ok': True, 'result': self.snapshot(sid)}
        if op in ('native_messages', 'native_child_messages'):
            if self.messages_error:
                return copy.deepcopy(self.messages_error)
            rows = [row for row in self.rows if is_visible_message(row)]
            after = fields.get('after_message_id')
            if after:
                ids = [message_identity(row) for row in rows]
                if after not in ids:
                    return {'ok': False, 'error': '-32602',
                            'detail': 'Message cursor is no longer available'}
                rows = rows[ids.index(after) + 1:]
            return {'ok': True, 'result': copy.deepcopy(rows[:fields['limit']])}
        if op == 'native_workflows':
            return {'ok': True, 'result': {'runs': []}}
        if op == 'native_children':
            return {'ok': True, 'result': {'native': {
                'childSessionIds': [], 'running': [], 'ended': {'total': 0, 'items': []}}}}
        raise AssertionError('Unexpected Host operation: ' + op)

    def calls(self, op):
        return [call.args[1] for call in self.channel.call_args_list
                if call.args[1]['op'] == op]

    def page(self, after=None, limit=100, sid=None, raw=False):
        return self.bridge.call('zcode_messages', {
            'session_id': sid or self.SID, 'after_message_id': after,
            'limit': limit, 'raw': raw})

    def make_job(self):
        job = self.bridge.superagent.create_job(self.CONTEXT, {
            'catalog_validated': True, 'workspace': self.workspace,
            'model': {'providerId': 'test-provider', 'modelId': 'test-model'},
            'thought_level': 'high', 'workflow_mode': 'onDemand',
            'transport': 'desktop_native'}, caller_permission_mode='always_ask',
            session_id=self.SID, idempotency_key='transcript-job',
            task_text='synthetic transcript test')['job']
        self.bridge.superagent.set_status(job['job_id'], self.CONTEXT['thread_id'],
                                         'running', 'send_accepted')
        return self.bridge.superagent.get(job['job_id'], self.CONTEXT['thread_id'])

    def assert_no_engine(self):
        self.bridge._rpc_once.assert_not_called()
        self.bridge.backend.assert_not_called()

    def test_281_raw_rows_have_a_real_second_visible_page(self):
        self.assertEqual(len(self.rows), 281)
        first = self.page()
        self.assertEqual(len(first['messages']), 100)
        self.assertTrue(first['more'])
        self.assertEqual(first['after_message_id'], 'msg_test_99')
        second = self.page(first['after_message_id'])
        self.assertEqual(len(second['messages']), 29)
        self.assertFalse(second['more'])
        self.assertEqual(second['after_message_id'], 'msg_test_128')
        self.assertEqual([row['id'] for row in first['messages'] + second['messages']],
                         [message_identity(row) for row in self.visible])
        self.assertEqual([call['limit'] for call in self.calls('native_messages')], [101, 101])
        self.assert_no_engine()

    def test_three_pages_have_no_lost_or_duplicate_messages(self):
        self.rows = [message(i) for i in range(281)]
        seen, cursor, sizes, more_flags = [], None, [], []
        for _ in range(4):
            result = self.page(cursor)
            seen.extend(row['id'] for row in result['messages'])
            sizes.append(len(result['messages']))
            more_flags.append(result['more'])
            cursor = result['after_message_id']
            if not result['more']:
                break
        self.assertEqual(sizes, [100, 100, 81])
        self.assertEqual(more_flags, [True, True, False])
        self.assertEqual(seen, [message_identity(row) for row in self.rows])
        self.assertEqual(len(set(seen)), len(seen))

    def test_raw_mapping_exposes_lookahead_and_page_cursor(self):
        page = self.page(raw=True)
        self.assertEqual(len(page['messages']), 100)
        self.assertTrue(page['hasMore'])
        self.assertEqual(page['afterMessageId'], 'msg_test_99')
        self.assertEqual(len(self.calls('native_messages')), 1)

    def test_exact_full_page_and_empty_tail_are_proven_by_lookahead(self):
        self.rows = [message(i) for i in range(100)]
        first = self.page()
        self.assertFalse(first['more'])
        self.assertEqual(self.calls('native_messages')[-1]['limit'], 101)
        tail = self.page(first['after_message_id'])
        self.assertEqual(tail['messages'], [])
        self.assertFalse(tail['more'])
        self.assertEqual(tail['after_message_id'], 'msg_test_99')

    def test_native_maximum_page_uses_one_bounded_extra_read(self):
        self.rows = [message(i) for i in range(1001)]
        first = self.bridge.rpc('session/messages', {'sessionId': self.SID, 'limit': 1000})
        self.assertEqual(len(first['messages']), 1000)
        self.assertTrue(first['hasMore'])
        calls = self.calls('native_messages')
        self.assertEqual([call['limit'] for call in calls], [1000, 1])
        self.assertEqual(calls[-1]['after_message_id'], 'msg_test_999')

    def test_verified_child_pages_keep_parent_run_and_existing_only_route(self):
        self.bridge._remember_native_children(self.SID, [self.CHILD], run_id='run-test')
        first = self.page(sid=self.CHILD)
        second = self.page(first['after_message_id'], sid=self.CHILD)
        self.assertTrue(first['more'])
        self.assertFalse(second['more'])
        self.assertEqual(len(first['messages']) + len(second['messages']), 129)
        for fields in self.calls('native_child_messages'):
            self.assertEqual(fields['session_id'], self.SID)
            self.assertEqual(fields['child_session_id'], self.CHILD)
            self.assertEqual(fields['run_id'], 'run-test')
            self.assertEqual(fields['limit'], 101)
        self.assertEqual(self.calls('native_messages'), [])
        self.assert_no_engine()

    def test_foreign_parent_or_child_refusal_never_falls_back(self):
        self.bridge.sessions['sess_foreign'] = copy.deepcopy(self.bridge.sessions[self.SID])
        with self.assertRaises(DesktopNativeError) as parent:
            self.page(sid='sess_foreign')
        self.assertEqual(parent.exception.code, 'native_session_unowned')
        self.bridge._remember_native_children(self.SID, [self.CHILD], run_id='run-test')
        self.child_error = {'ok': False, 'error': 'child_not_verified_for_parent',
                            'detail': 'not a verified member'}
        with self.assertRaises(DesktopNativeError) as child:
            self.page(sid=self.CHILD)
        self.assertEqual(child.exception.code, 'child_not_verified_for_parent')
        self.assert_no_engine()

    def test_message_read_failure_or_stale_cursor_remains_an_error(self):
        with self.assertRaises(DesktopNativeError) as cursor:
            self.page('msg_missing')
        self.assertEqual(cursor.exception.code, '-32602')
        self.messages_error = {'ok': False, 'error': 'unknown', 'detail': 'read failed'}
        with self.assertRaises(DesktopNativeError) as read:
            self.page()
        self.assertEqual(read.exception.code, 'unknown')
        self.assert_no_engine()

    def test_last_assistant_and_last_user_come_from_ready_snapshot_tail(self):
        self.assertEqual(self.bridge._last_message_id(self.SID), 'msg_test_128')
        self.assertEqual(self.bridge._last_user_message_id(self.SID), 'msg_test_127')
        self.assertEqual(len(self.calls('native_snapshot')), 2)
        self.assertEqual(self.calls('native_messages'), [])
        self.assert_no_engine()

    def test_existing_snapshot_is_reused_and_foreign_snapshot_is_refused(self):
        snapshot = self.snapshot()
        self.assertEqual(self.bridge._last_message_id(self.SID, snapshot=snapshot), 'msg_test_128')
        self.assertEqual(self.bridge._last_user_message_id(self.SID, snapshot=snapshot), 'msg_test_127')
        foreign = self.snapshot('sess_foreign')
        with self.assertRaises(DesktopNativeError) as caught:
            self.bridge._last_message_id(self.SID, snapshot=foreign)
        self.assertEqual(caught.exception.code, 'native_snapshot_messages_invalid')
        self.channel.assert_not_called()

    def test_unready_snapshot_never_substitutes_forward_page_for_tail(self):
        self.snapshot_error = {'ok': False, 'error': '-32004',
                               'detail': 'Session runtime is not ready'}
        for helper in (self.bridge._last_message_id, self.bridge._last_user_message_id):
            with self.subTest(helper=helper.__name__):
                with self.assertRaises(DesktopNativeError) as caught:
                    helper(self.SID)
                self.assertEqual(caught.exception.code, '-32004')
        self.assertEqual(self.calls('native_messages'), [])
        self.assert_no_engine()

    def test_snapshot_with_unknown_message_shape_does_not_claim_an_early_tail(self):
        for value in (None, {}, [None]):
            with self.subTest(messages=value):
                snapshot = self.snapshot()
                snapshot['messages'] = value
                with self.assertRaises(DesktopNativeError):
                    self.bridge._last_message_id(self.SID, snapshot=snapshot)
        self.channel.assert_not_called()

    def test_child_tail_uses_the_fixed_child_snapshot_interface(self):
        self.bridge._remember_native_children(self.SID, [self.CHILD], run_id='run-test')
        self.assertEqual(self.bridge._last_message_id(self.CHILD), 'msg_test_128')
        self.assertEqual(len(self.calls('native_child_snapshot')), 1)
        self.assertEqual(self.calls('native_snapshot'), [])
        self.assertEqual(self.calls('native_messages'), [])
        self.assert_no_engine()

    def test_inspect_reads_actual_snapshot_tail_without_claiming_task_completion(self):
        job = self.make_job()
        result = self.bridge.call('zcode_agent_inspect', {'agent_id': job['agent_id']}, self.CONTEXT)
        self.assertEqual(result['last_message_id'], 'msg_test_128')
        self.assertEqual(result['job']['last_progress']['message_id'], 'msg_test_128')
        self.assertEqual(result['verdict'], 'engine_idle_unverified')
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['terminal'])
        self.assertFalse(result['success'])
        self.assertIsNone(result['task_success'])
        self.assertEqual(result['errors'], {})
        # The job synchronization and the inspection each read one snapshot;
        # their tail and pending checks reuse it instead of adding more reads.
        self.assertEqual(len(self.calls('native_snapshot')), 2)
        self.assertEqual(self.calls('native_messages'), [])

    def test_inspect_preserves_unready_snapshot_diagnostics_without_idle_or_final(self):
        job = self.make_job()
        self.snapshot_error = {'ok': False, 'error': '-32004',
                               'detail': 'Session runtime is not ready'}
        result = self.bridge.call('zcode_agent_inspect', {'agent_id': job['agent_id']}, self.CONTEXT)
        self.assertEqual(result['last_message_id'], '')
        self.assertEqual(result['errors']['messages'], 'DesktopNativeError')
        self.assertEqual(result['errors']['pending'], 'DesktopNativeError')
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['terminal'])
        self.assertNotEqual(result['verdict'], 'engine_idle_unverified')
        stored = self.bridge.superagent.get(job['job_id'], self.CONTEXT['thread_id'])
        self.assertFalse(stored['observation']['engine_idle'])
        self.assertEqual(stored['observation']['read_error'], 'DesktopNativeError')
        self.assertEqual(self.calls('native_messages'), [])
        self.assert_no_engine()

    def test_unsubscribed_progress_reuses_the_snapshot_already_read(self):
        job = self.make_job()
        self.bridge._sync_unsubscribed_job(job, time.time(), 600, 5, poll_seconds=0)
        stored = self.bridge.superagent.get(job['job_id'], self.CONTEXT['thread_id'])
        self.assertEqual(stored['observation']['last_message_id'], 'msg_test_128')
        self.assertEqual(stored['last_progress']['message_id'], 'msg_test_128')
        self.assertNotEqual(stored['status'], 'verified')
        self.assertEqual(len(self.calls('native_snapshot')), 1)
        self.assertEqual(self.calls('native_messages'), [])

    def test_legacy_forward_paging_and_tail_reads_keep_existing_behavior(self):
        sid = 'sess_legacy'
        self.bridge.sessions[sid] = {'transport': 'engine'}
        self.bridge.resident.add(sid)
        self.bridge.backend = Mock(return_value=Mock())
        rows = [message(i) for i in range(7)]
        self.bridge._rpc_once = Mock(return_value={'messages': rows})
        result = self.page('legacy-anchor', limit=3, sid=sid)
        self.assertNotIn('limit', self.bridge._rpc_once.call_args.args[1])
        self.assertEqual([row['id'] for row in result['messages']],
                         ['msg_test_0', 'msg_test_1', 'msg_test_2'])
        self.assertTrue(result['more'])
        self.assertEqual(self.bridge._last_message_id(sid), 'msg_test_6')
        self.assertEqual(self.bridge._rpc_once.call_args.args[1]['limit'], 20)
        self.bridge._rpc_once.side_effect = RuntimeError('legacy read failed')
        self.assertEqual(self.bridge._last_message_id(sid), '')
        self.channel.assert_not_called()


if __name__ == '__main__':
    unittest.main()
