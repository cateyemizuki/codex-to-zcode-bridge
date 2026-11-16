"""A native runtime can exist before session/subagents has a persisted parent.

Exercise the real Bridge router and DesktopNativeAdapter error conversion with
an in-memory Host channel. No engine, Desktop, credentials or user state is used.
"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import storage
from zcode_bridge.desktop_native import DesktopNativeError
from zcode_bridge.protocol import ProtocolError
from zcode_bridge.service import is_session_not_found_error
from bridge_fixtures import MockBridge as Bridge


class NativeFreshNotifyGapTests(unittest.TestCase):
    SID = 'sess_native_fresh'
    CONTEXT = {'thread_id': 'test-native-chat', 'thread_origin': 'turn_metadata'}
    MODEL = {'providerId': 'test-provider', 'modelId': 'test-model'}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = str(Path(temporary.name).resolve())
        self.env = patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': temporary.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = patch('zcode_bridge.service.config', return_value={
            **storage.defaults(), 'auto_launch_desktop': False,
            'default_model': dict(self.MODEL), 'default_thought_level': 'high',
            'default_workspace': self.workspace})
        self.cfg.start()
        self.addCleanup(self.cfg.stop)
        visibility = patch('zcode_bridge.service.session_visibility',
                           return_value={'indexed': False})
        visibility.start()
        self.addCleanup(visibility.stop)
        self.snapshot = {
            'session': {'sessionId': self.SID,
                        'workspace': {'workspacePath': self.workspace}},
            'projection': {'status': 'idle', 'turnCount': 0, 'backgroundJobs': []},
            'messages': [], 'runtime': {'pendingRequestIds': []},
            'settings': {'model': {'current': dict(self.MODEL)},
                         'thoughtLevel': {'current': 'high'},
                         'permission': {'mode': 'yolo'}},
        }
        self.created = {
            'sessionId': self.SID, 'ownedSID': self.SID, 'workspace': self.workspace,
            'snapshot': copy.deepcopy(self.snapshot), 'model': dict(self.MODEL),
            'thoughtLevel': 'high', 'mode': 'yolo',
            'runtime': {'identity': 'test-native-runtime', 'generation': 1},
            'source': 'desktop-native-host', 'guiVerified': False,
        }
        self.create_response = {'ok': True, 'result': self.created}
        self.children_response = self.native_error()
        self.runs = []
        self.bridge = self.make_bridge()

    def native_error(self, code='-32004', detail=None):
        return {'ok': False, 'error': code,
                'detail': detail if detail is not None else f'Session not found: {self.SID}'}

    def make_bridge(self):
        bridge = Bridge()
        # Stub only control discovery and I/O. Native methods and the error
        # translation in DesktopNativeAdapter._request remain real.
        native = bridge.desktop_native
        native._control = Mock(return_value={})
        native._workspace = Mock(side_effect=lambda _control, workspace: workspace)
        native._sync._call = Mock(side_effect=self.host_call)
        bridge._rpc_once = Mock(side_effect=AssertionError('independent engine used'))
        bridge.backend = Mock(side_effect=AssertionError('engine started or resumed'))
        return bridge

    def host_call(self, _control, message):
        op = message['op']
        if op == 'native_create':
            return copy.deepcopy(self.create_response)
        if op == 'native_snapshot':
            return {'ok': True, 'result': copy.deepcopy(self.snapshot)}
        if op == 'native_messages':
            return {'ok': True, 'result': copy.deepcopy(self.snapshot['messages'])}
        if op == 'native_workflows':
            return {'ok': True, 'result': {'runs': copy.deepcopy(self.runs)}}
        if op == 'native_children':
            if isinstance(self.children_response, Exception):
                raise self.children_response
            return copy.deepcopy(self.children_response)
        if op == 'native_send':
            return {'ok': True, 'result': {
                'status': 'accepted', 'sessionId': self.SID,
                'commandId': 'test-native-command', 'admissionProtocol': 'session/send',
                'nativeAdmission': {'accepted': True, 'sessionId': self.SID}}}
        raise AssertionError('Unexpected native operation: ' + op)

    def create(self):
        return self.bridge.call('zcode_create_session', {
            'workspace': self.workspace, 'transport': 'desktop_native',
            'model': dict(self.MODEL), 'thought_level': 'high',
            'caller_permission_mode': 'full_access'})

    def send(self, bridge=None):
        return (bridge or self.bridge).call('zcode_send', {
            'session_id': self.SID, 'content': 'synthetic native task',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-first',
            'notify': {'completion_notify': True, 'auto_continue': False,
                       'user_authorized': True}}, self.CONTEXT)

    def operations(self, bridge=None):
        return [call.args[1]['op'] for call in
                (bridge or self.bridge).desktop_native._sync._call.call_args_list]

    def assert_not_sent(self, bridge=None):
        bridge = bridge or self.bridge
        self.assertNotIn('native_send', self.operations(bridge))
        self.assertEqual(bridge.notifications.all_subscriptions(), [])
        self.assertEqual(bridge.notifications.all_events(), [])
        bridge._rpc_once.assert_not_called()
        bridge.backend.assert_not_called()

    def test_confirmed_native_create_marks_only_current_generation(self):
        result = self.create()
        self.assertEqual(result['session']['sessionId'], self.SID)
        self.assertTrue(self.bridge._is_created(self.SID))
        self.assertEqual(self.bridge.sessions[self.SID]['transport'], 'desktop_native')
        restored = self.make_bridge()
        self.assertEqual(restored.sessions[self.SID]['transport'], 'desktop_native')
        self.assertFalse(restored._is_created(self.SID))

    def test_first_notify_send_passes_exact_persisted_parent_gap_once(self):
        self.create()
        result = self.send()
        self.assertTrue(result['accepted'])
        operations = self.operations()
        self.assertEqual(operations.count('native_send'), 1)
        self.assertLess(operations.index('native_children'), operations.index('native_send'))
        subscription = self.bridge.notifications.find_subscription(result['subscription_id'])
        self.assertEqual(subscription['state'], 'armed')
        self.assertEqual(subscription['baseline']['child_ids'], [])
        self.assertIsNone(subscription.get('terminal_event_id'))
        self.assertEqual(self.bridge.notifications.all_events(), [])
        self.assertTrue(self.send()['duplicate'])
        self.assertEqual(self.operations().count('native_send'), 1)
        self.bridge._rpc_once.assert_not_called()
        self.bridge.backend.assert_not_called()

    def test_unknown_create_with_candidate_sid_does_not_mark_created(self):
        self.create_response = {
            'ok': False, 'error': 'unknown', 'detail': 'create outcome unknown',
            'request': {'status': 'unknown', 'sessionId': self.SID}}
        with self.assertRaises(DesktopNativeError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, 'unknown')
        self.assertEqual(caught.exception.request['sessionId'], self.SID)
        self.assertFalse(self.bridge._is_created(self.SID))
        self.assertNotIn(self.SID, self.bridge.sessions)
        self.assert_not_sent()

    def test_replayed_create_record_is_not_creation_proof(self):
        self.create_response = {'ok': True, 'result': {
            'replay': True, 'automaticRetry': False,
            'request': {'status': 'confirmed', 'sessionId': self.SID}}}
        with self.assertRaises(DesktopNativeError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, 'native_create_no_session')
        self.assertFalse(self.bridge._is_created(self.SID))
        self.assertNotIn(self.SID, self.bridge.sessions)
        self.assert_not_sent()

    def test_restored_native_binding_has_no_first_send_exemption(self):
        self.create()
        restored = self.make_bridge()
        with self.assertRaises(DesktopNativeError) as caught:
            self.send(restored)
        self.assertEqual(caught.exception.code, '-32004')
        self.assertEqual(caught.exception.detail, f'Session not found: {self.SID}')
        self.assert_not_sent(restored)

    def test_foreign_sid_is_not_exempted_by_another_created_sid(self):
        self.create()
        self.bridge.created.clear()
        self.bridge._mark_created('sess_another_created')
        with self.assertRaises(DesktopNativeError):
            self.send()
        self.assert_not_sent()

    def test_native_non_target_errors_are_never_exempted(self):
        self.create()
        cases = [
            self.native_error(detail='Session not found: sess_other'),
            self.native_error(detail='Session runtime is not ready'),
            self.native_error(code='-32001'),
            self.native_error(code='unknown'),
            self.native_error(detail=f'Session not found: {self.SID} suffix'),
            self.native_error(detail=f' Session not found: {self.SID}'),
            self.native_error(detail=f'Session not found: {self.SID}\n'),
            RuntimeError('native read failed'),
        ]
        for response in cases:
            with self.subTest(response=response):
                self.children_response = response
                expected = RuntimeError if isinstance(response, Exception) else DesktopNativeError
                with self.assertRaises(expected) as caught:
                    self.send()
                if isinstance(response, dict):
                    self.assertEqual(caught.exception.code, response['error'])
                    self.assertEqual(caught.exception.detail, response['detail'])
                else:
                    self.assertIs(caught.exception, response)
                self.assert_not_sent()

    def test_snapshot_must_prove_zero_turns_and_no_user_input(self):
        self.create()
        original = copy.deepcopy(self.snapshot)
        cases = [
            {'projection': {**original['projection'], 'turnCount': 1}},
            {'projection': {**original['projection'], 'currentTurnId': 'turn-existing'}},
            {'projection': {'status': 'idle', 'backgroundJobs': []}},
            {'messages': [{'info': {'id': 'user-1', 'role': 'user'},
                           'parts': [{'type': 'text', 'text': 'already sent'}]}]},
            {'messages': None},
            {'messages': [None]},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.snapshot = {**copy.deepcopy(original), **change}
                with self.assertRaises(DesktopNativeError):
                    self.send()
                self.assert_not_sent()

    def test_jobs_or_workflow_runs_block_the_first_send_exception(self):
        self.create()
        for jobs, runs in [
            ([{'jobId': 'job-1', 'status': 'running'}], []),
            ([{'jobId': 'job-1', 'status': 'completed'}], []),
            ([], [{'runId': 'run-1', 'status': 'running'}]),
            ([], [{'runId': 'run-1', 'status': 'completed'}]),
        ]:
            with self.subTest(jobs=jobs, runs=runs):
                self.snapshot['projection']['backgroundJobs'] = jobs
                self.runs = runs
                with self.assertRaises((ValueError, DesktopNativeError)):
                    self.send()
                self.assert_not_sent()

    def test_read_without_first_send_opt_in_stays_strict(self):
        self.create()
        with self.assertRaises(DesktopNativeError):
            self.bridge._children_evidence(self.SID, strict=True)
        self.assert_not_sent()

    def test_classifier_keeps_protocol_and_native_errors_distinct_and_exact(self):
        self.assertTrue(is_session_not_found_error(ProtocolError({
            'code': -32004, 'message': f'Session not found: {self.SID}'}), self.SID))
        self.assertTrue(is_session_not_found_error(DesktopNativeError(
            '-32004', f'Session not found: {self.SID}'), self.SID))
        self.assertFalse(is_session_not_found_error(RuntimeError(
            f'Session not found: {self.SID}'), self.SID))


if __name__ == '__main__':
    unittest.main()
