"""Native catalog queries need fresh Host request IDs, unlike business writes."""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import storage
from zcode_bridge.service import Bridge


class NativeDelegateCatalogRequestTests(unittest.TestCase):
    CONTEXT = {'thread_id': 'test-catalog-chat', 'thread_origin': 'turn_metadata'}
    MODEL = {'providerId': 'test-provider', 'modelId': 'test-model'}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = str(Path(temporary.name).resolve())
        env = patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': temporary.name})
        env.start()
        self.addCleanup(env.stop)
        cfg = patch('zcode_bridge.service.config', return_value={
            **storage.defaults(), 'auto_launch_desktop': False,
            'default_model': dict(self.MODEL), 'default_thought_level': 'high',
            'default_workspace': self.workspace, 'desktop_sync_enabled': False})
        cfg.start()
        self.addCleanup(cfg.stop)
        visibility = patch('zcode_bridge.service.session_visibility',
                           return_value={'indexed': False})
        visibility.start()
        self.addCleanup(visibility.stop)
        self.catalog_requests = {}
        self.snapshots = {}
        self.send_unknown = False
        self.bridge = Bridge()
        # The adapter's methods, response unwrapping and Bridge dispatch remain
        # real. Only local control discovery and the Host channel are replaced.
        native = self.bridge.desktop_native
        native._control = Mock(return_value={})
        native._workspace = Mock(side_effect=lambda _control, workspace: workspace)
        native._sync._call = Mock(side_effect=self.host_call)
        self.channel = native._sync._call
        self.bridge._rpc_once = Mock(side_effect=AssertionError('independent engine used'))
        self.bridge.backend = Mock(side_effect=AssertionError('engine started or resumed'))

    def host_call(self, _control, message):
        op = message['op']
        if op == 'native_model_catalog':
            request_id = message['request_id']
            if request_id in self.catalog_requests:
                # Host mutation replay returns its ledger entry, not the prior
                # operation's available-model result (native_transport.call).
                result = {'replay': True, 'request': copy.deepcopy(
                    self.catalog_requests[request_id]), 'automaticRetry': False}
            else:
                self.catalog_requests[request_id] = {
                    'requestId': request_id, 'op': op, 'workspace': message['workspace'],
                    'status': 'confirmed', 'phase': 'catalog_selected'}
                result = {'available': [{
                    'ref': dict(self.MODEL), 'label': 'Test model',
                    'reasoning': {'levels': [{'value': 'high'}, {'value': 'low'}],
                                  'defaultLevel': 'high'}}],
                    'selectionStatus': 'validated'}
            return {'ok': True, 'result': result}
        if op == 'native_create':
            sid = 'sess_catalog_' + str(len(self.snapshots) + 1)
            snapshot = {
                'session': {'sessionId': sid,
                            'workspace': {'workspacePath': self.workspace}},
                'projection': {'status': 'idle', 'turnCount': 0, 'backgroundJobs': []},
                'messages': [], 'runtime': {'pendingRequestIds': []},
                'settings': {'model': {'current': dict(self.MODEL)},
                             'thoughtLevel': {'current': 'high'},
                             'permission': {'mode': 'yolo'}},
            }
            self.snapshots[sid] = snapshot
            return {'ok': True, 'result': {
                'sessionId': sid, 'ownedSID': sid, 'workspace': self.workspace,
                'snapshot': copy.deepcopy(snapshot), 'model': dict(self.MODEL),
                'thoughtLevel': 'high', 'mode': 'yolo',
                'runtime': {'identity': 'test-runtime', 'generation': 1}}}
        if op == 'native_send':
            if self.send_unknown:
                return {'ok': False, 'error': 'unknown', 'detail': 'send outcome unknown',
                        'request': {'sessionId': message['session_id'], 'status': 'unknown'}}
            return {'ok': True, 'result': {
                'status': 'accepted', 'sessionId': message['session_id'],
                'nativeAdmission': {'accepted': True}}}
        if op == 'native_snapshot':
            return {'ok': True, 'result': copy.deepcopy(self.snapshots[message['session_id']])}
        if op in ('native_messages', 'native_events'):
            return {'ok': True, 'result': []}
        if op == 'native_workflows':
            return {'ok': True, 'result': {'runs': []}}
        if op == 'native_children':
            return {'ok': True, 'result': {
                'native': {'childSessionIds': [], 'running': [],
                           'ended': {'total': 0, 'items': []}}}}
        raise AssertionError('Unexpected Host operation: ' + op)

    def delegate(self, key, task='Synthetic catalog test task'):
        return self.bridge.call('zcode_delegate', {
            'task': task, 'workspace': self.workspace, 'transport': 'desktop_native',
            'model': dict(self.MODEL), 'thought_level': 'high',
            'caller_permission_mode': 'full_access', 'idempotency_key': key}, self.CONTEXT)

    def messages(self, op):
        return [call.args[1] for call in self.channel.call_args_list
                if call.args[1]['op'] == op]

    def test_host_catalog_replay_has_request_metadata_without_available(self):
        native = self.bridge.desktop_native
        first = native.model_catalog(self.workspace, 'full_access', 'catalog_same_request',
                                     dict(self.MODEL), 'high')
        replay = native.model_catalog(self.workspace, 'full_access', 'catalog_same_request',
                                      dict(self.MODEL), 'high')
        self.assertTrue(first['available'])
        self.assertEqual(replay, {
            'replay': True, 'request': self.catalog_requests['catalog_same_request'],
            'automaticRetry': False})
        self.assertNotIn('available', replay)

    def test_two_new_task_keys_share_selection_but_not_catalog_request(self):
        first = self.delegate('task-one')
        second = self.delegate('task-two')
        self.assertTrue(first['created'])
        self.assertTrue(second['created'])
        self.assertEqual(first['send']['state'], 'accepted')
        self.assertEqual(second['send']['state'], 'accepted')
        self.assertNotEqual(first['session_id'], second['session_id'])
        queries = self.messages('native_model_catalog')
        self.assertEqual(len(queries), 2)
        self.assertNotEqual(queries[0]['request_id'], queries[1]['request_id'])
        for query in queries:
            self.assertRegex(query['request_id'], r'^catalog_[0-9a-f]{32}$')
            self.assertEqual(query['workspace'], self.workspace)
            self.assertEqual(query['model'], self.MODEL)
            self.assertEqual(query['thought_level'], 'high')
            self.assertEqual(query['caller_permission_mode'], 'full_access')
        creates = self.messages('native_create')
        self.assertEqual(len(creates), 2)
        self.assertNotEqual(creates[0]['request_id'], creates[1]['request_id'])
        self.assertEqual(len(self.messages('native_send')), 2)
        self.bridge._rpc_once.assert_not_called()
        self.bridge.backend.assert_not_called()

    def test_same_task_key_returns_existing_job_without_any_host_call(self):
        first = self.delegate('same-task')
        count = self.channel.call_count
        replay = self.delegate('same-task')
        self.assertFalse(replay['created'])
        self.assertEqual(replay['agent_id'], first['agent_id'])
        self.assertEqual(self.channel.call_count, count)

    def test_conflicting_task_key_does_not_query_catalog_or_host(self):
        first = self.delegate('same-task')
        count = self.channel.call_count
        conflict = self.delegate('same-task', task='Different synthetic task')
        self.assertTrue(conflict['conflict'])
        self.assertFalse(conflict['created'])
        self.assertEqual(conflict['agent_id'], first['agent_id'])
        self.assertEqual(self.channel.call_count, count)

    def test_unknown_business_send_is_not_retried_on_delegate_replay(self):
        self.send_unknown = True
        first = self.delegate('unknown-send')
        self.assertEqual(first['send']['state'], 'uncertain')
        count = self.channel.call_count
        replay = self.delegate('unknown-send')
        self.assertFalse(replay['created'])
        self.assertEqual(replay['send']['state'], 'uncertain')
        self.assertEqual(self.channel.call_count, count)
        self.assertEqual(len(self.messages('native_model_catalog')), 1)
        self.assertEqual(len(self.messages('native_create')), 1)
        self.assertEqual(len(self.messages('native_send')), 1)


if __name__ == '__main__':
    unittest.main()
