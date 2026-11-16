import copy
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import mcp, notifications, storage
from zcode_bridge.desktop_native import DesktopNativeError
from zcode_bridge.notifications import Observer
from zcode_bridge.protocol import ProtocolError
from zcode_bridge.service import ObserverRuntimeAdapter
from bridge_fixtures import MockBridge as Bridge
from zcode_bridge.tools import TOOLS

CTX_A = {'thread_id': 'chat-A', 'thread_origin': 'turn_metadata'}
CTX_CHILD = {'thread_id': 'child-chat', 'thread_origin': 'openai_thread_id'}
MODEL = {'providerId': 'p', 'modelId': 'm'}
CATALOG = {'available': [
    {'ref': dict(MODEL), 'label': 'Model', 'providerLabel': 'P',
     'reasoning': {'levels': [{'value': 'high'}, {'value': 'low'}], 'defaultLevel': 'high'}},
]}


class FakeEngine:
    def __init__(self):
        self.status = 'idle'
        self.turn_count = 1
        self.mode = 'build'
        self.messages = []
        self.runs = []
        self.workflow_events = []
        self.jobs = []
        self.child_ids = []
        self.send_calls = []
        self.create_params = []
        self.deferred_params = []
        self.stops = []
        self.cancelled_tasks = []
        self.cancel_error = None
        self.deferred = 0
        self.created = 0
        self.send_error = None
        self.send_result = {'accepted': True, 'messageId': 'm-sent'}
        self.strict_send_schema = False
        self.fail_subagents = False
        self.fail_workflow_runs = False
        self.fail_workflow_events = False
        self.runtime = {}

    def snapshot(self, sid='sess_agent_1'):
        projection = {'status': self.status, 'turnCount': self.turn_count}
        if self.jobs:
            projection['backgroundJobs'] = copy.deepcopy(self.jobs)
        return {'session': {'sessionId': sid},
                'projection': projection,
                'runtime': copy.deepcopy(self.runtime),
                'settings': {'permission': {'mode': self.mode},
                             'model': {'current': dict(MODEL)},
                             'thoughtLevel': {'current': 'high'}}}

    def handle(self, method, params=None, deadline=None):
        params = params or {}
        if method == 'session/create':
            if params.get('persistence') == 'deferred':
                self.deferred += 1
                self.deferred_params.append(copy.deepcopy(params))
                return {'session': {'sessionId': 'sess_catalog'},
                        'settings': {'model': {'available': copy.deepcopy(CATALOG['available']),
                                               'current': dict(MODEL)}}}
            self.created += 1
            self.create_params.append(copy.deepcopy(params))
            return self.snapshot('sess_agent_' + str(self.created))
        if method in ('session/read', 'session/resume'):
            return self.snapshot(str(params.get('sessionId') or 'sess_agent_1'))
        if method == 'session/setMode':
            self.mode = params.get('mode')
            return self.snapshot(str(params.get('sessionId') or 'sess_agent_1'))
        if method == 'session/messages':
            return {'messages': copy.deepcopy(self.messages)}
        if method == 'v4/conversation/workflowRuns':
            if self.fail_workflow_runs:
                raise RuntimeError('workflow runs unavailable')
            return {'runs': copy.deepcopy(self.runs)}
        if method == 'v4/conversation/workflowRunEvents':
            if self.fail_workflow_events:
                raise RuntimeError('workflow journal unavailable')
            return {'events': copy.deepcopy(self.workflow_events), 'hasMore': False}
        if method == 'session/subagents':
            if self.fail_subagents:
                raise RuntimeError('subagents unavailable')
            return {'childSessionIds': list(self.child_ids)}
        if method == 'session/send':
            self.send_calls.append(copy.deepcopy(params))
            if self.strict_send_schema:
                # Mirror 3.18.2's exact Zod object: any extra key is -32602.
                unknown = sorted(set(params) - {'sessionId', 'content', 'inputId'})
                if unknown:
                    raise ProtocolError({
                        'code': -32602, 'message': 'Invalid params',
                        'data': {'name': 'ZodError', 'message': 'unrecognized_keys',
                                 'keys': unknown, 'path': []}})
            if self.send_error is not None:
                raise self.send_error
            return copy.deepcopy(self.send_result)
        if method == 'session/stop':
            self.stops.append(copy.deepcopy(params))
            return {'stopped': True}
        if method == 'session/cancelBackgroundTask':
            if self.cancel_error is not None:
                raise self.cancel_error
            task_id = str(params.get('taskId') or '')
            self.cancelled_tasks.append(task_id)
            self.jobs = [job for job in self.jobs
                         if str(job.get('taskId') or job.get('jobId')
                                or job.get('workId') or '') != task_id]
            return {'cancelled': True, 'taskId': task_id}
        if method == 'session/events':
            return {'events': []}
        if method == 'session/close':
            return {}
        return self.snapshot(str(params.get('sessionId') or 'sess_agent_1'))


class FakeNative:
    """Deterministic stand-in for DesktopNativeAdapter; never touches an engine."""

    def __init__(self):
        self.created = 0
        self.create_calls = []
        self.send_calls = []
        self.resolve_calls = []
        self.cancel_calls = []
        self.snapshots = {}
        self.send_error = None
        self.send_result = None
        self.children_result = None
        self.workflow_event_result = None
        self.workflow_runs = []
        self.child_message_calls = []
        self.child_snapshot_calls = []
        self.child_error = None
        self.child_message_rows = [
            {'info': {'id': 'native-child-m1', 'role': 'assistant'},
             'parts': [{'type': 'text', 'text': 'child hello'}]}]
        self.message_rows = []
        self.event_rows = []
        self.status_value = {'enabled': True, 'configured': True,
                             'state': 'ready', 'native_enabled': True}

    def create(self, workspace, permission, model, thought_level, request_id,
               project_id=None):
        self.create_calls.append({'workspace': workspace, 'permission': permission,
                                  'model': copy.deepcopy(model),
                                  'thought_level': thought_level,
                                  'request_id': request_id})
        self.created += 1
        sid = 'native_sess_' + str(self.created)
        snapshot = {
            'session': {'sessionId': sid, 'status': 'idle',
                        'workspace': {'workspacePath': workspace}},
            'settings': {'model': {'current': copy.deepcopy(model)},
                         'thoughtLevel': {'current': thought_level},
                         'permission': {'mode': 'yolo'}},
            'projection': {'status': 'idle', 'turnCount': 0,
                           'backgroundJobs': [], 'pendingPermissions': []},
            'runtime': {'pendingRequestIds': []},
            'messages': [],
        }
        self.snapshots[sid] = snapshot
        return {'sessionId': sid, 'ownedSID': sid, 'workspace': workspace,
                'model': copy.deepcopy(model), 'thoughtLevel': thought_level,
                'mode': 'yolo', 'runtime': {'identity': 'native-rt', 'generation': 1},
                'snapshot': copy.deepcopy(snapshot), 'source': 'desktop-native-host',
                'guiVerified': False}

    def snapshot(self, session_id, workspace, permission):
        return copy.deepcopy(self.snapshots[session_id])

    def messages(self, session_id, workspace, permission, after_message_id=None,
                 limit=100):
        if self.message_rows:
            return copy.deepcopy(self.message_rows)
        return [{'info': {'id': 'native-m1', 'role': 'assistant'},
                 'parts': [{'type': 'text', 'text': 'native hello'}]}]

    def events(self, session_id, workspace, permission, after_seq=None, limit=50):
        return copy.deepcopy(self.event_rows)

    def send(self, session_id, workspace, permission, text, request_id):
        self.send_calls.append({'session_id': session_id, 'permission': permission,
                                'text': text, 'request_id': request_id})
        if self.send_error is not None:
            raise self.send_error
        if self.send_result is not None:
            return copy.deepcopy(self.send_result)
        return {'status': 'accepted', 'sessionId': session_id,
                'commandId': 'native-cmd-' + str(len(self.send_calls)),
                'admissionProtocol': 'session/send',
                'nativeAdmission': {'accepted': True, 'sessionId': session_id}}

    def cancel(self, session_id, workspace, permission, request_id):
        self.cancel_calls.append({'session_id': session_id,
                                  'permission': permission,
                                  'request_id': request_id})
        return {'stopped': True, 'sessionId': session_id}

    def children(self, session_id, workspace, permission, limit=30):
        if self.children_result is not None:
            return copy.deepcopy(self.children_result)
        return {'native': {'running': [], 'ended': []},
                'writeOwnershipGranted': False}

    def child_messages(self, session_id, workspace, permission, child_session_id,
                       run_id=None, after_message_id=None, limit=100):
        self.child_message_calls.append({
            'session_id': session_id, 'child_session_id': child_session_id,
            'run_id': run_id, 'after_message_id': after_message_id,
            'permission': permission, 'limit': limit})
        if self.child_error is not None:
            raise self.child_error
        return copy.deepcopy(self.child_message_rows)

    def child_snapshot(self, session_id, workspace, permission, child_session_id,
                       run_id=None):
        self.child_snapshot_calls.append({
            'session_id': session_id, 'child_session_id': child_session_id,
            'run_id': run_id, 'permission': permission})
        if self.child_error is not None:
            raise self.child_error
        return {'session': {'sessionId': child_session_id,
                            'sessionKind': 'subagent_child'},
                'settings': {'model': {'current': dict(MODEL)},
                             'thoughtLevel': {'current': 'high'}},
                'projection': {'status': 'idle'}, 'runtime': {}}

    def workflows(self, session_id, workspace, permission, limit=16):
        return {'runs': copy.deepcopy(self.workflow_runs)}

    def workflow_events(self, session_id, workspace, permission, run_id,
                        after_sequence=None, limit=100):
        if self.workflow_event_result is not None:
            return copy.deepcopy(self.workflow_event_result)
        return []

    def resolve(self, session_id, workspace, permission, interaction_id, answer,
                request_id, user_approved=False):
        self.resolve_calls.append({'session_id': session_id,
                                   'interaction_id': interaction_id,
                                   'answer': copy.deepcopy(answer),
                                   'request_id': request_id,
                                   'permission': permission,
                                   'user_approved': user_approved})
        return {'resolved': True}

    def model_catalog(self, workspace, permission, request_id, model=None,
                      thought_level=None):
        return {'available': [{'ref': dict(MODEL), 'label': 'Model',
                               'providerLabel': 'P',
                               'reasoning': {'levels': [{'value': 'high'},
                                                        {'value': 'low'}],
                                             'defaultLevel': 'high'}}],
                'selectionStatus': 'validated'}

    def status(self):
        return copy.deepcopy(self.status_value)


class SuperagentServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': self.tmp.name})
        self.env.start()
        executable = Path(self.tmp.name) / 'ZCode.exe'
        executable.touch()
        self.workspace = Path(self.tmp.name) / 'work'
        self.workspace.mkdir()
        self.cfg = patch('zcode_bridge.service.config', return_value={
            **storage.defaults(),
            # Generic state-machine tests use FakeEngine in memory only.
            'default_transport': 'engine',
            'zcode_executable': str(executable), 'auto_launch_desktop': False,
            'default_model': dict(MODEL), 'default_thought_level': 'high',
            'default_workspace': str(self.workspace),
            'superagent_profiles': {}, 'superagent_default_profile': '',
            'superagent_stall_seconds': 60,
            'superagent_read_failure_threshold': 5,
            'desktop_sync_enabled': False,
            'desktop_sync_control_file': '',
            'desktop_sync_min_interval_seconds': 1,
        })
        self.cfg.start()
        self.visibility = patch('zcode_bridge.service.session_visibility',
                                return_value={'indexed': False})
        self.visibility.start()
        self.fake = FakeEngine()
        self.bridge = self.make_bridge()

    def tearDown(self):
        self.visibility.stop()
        self.cfg.stop()
        self.env.stop()
        self.tmp.cleanup()

    def make_bridge(self):
        bridge = Bridge()
        bridge.client = Mock()
        bridge.client.child.pid = 4242
        bridge.rpc = Mock(side_effect=self.fake.handle)
        bridge.backend = Mock(return_value=bridge.client)
        return bridge

    def call(self, name, args, context=CTX_A, bridge=None):
        return (bridge or self.bridge).call(name, args, context)

    def delegate_args(self, key='delegate-1', notify=None, **overrides):
        args = {
            'task': '修复 backend 的失败测试，只改这个工作区。',
            'workspace': str(self.workspace),
            'caller_permission_mode': 'full_access',
            'idempotency_key': key,
        }
        if notify is not None:
            args['notify'] = notify
        args.update(overrides)
        return args

    def authorized_notify(self):
        return {'completion_notify': True, 'auto_continue': False, 'user_authorized': True}

    def use_native(self):
        """Install the real rpc router plus a native adapter; engine calls fail."""
        native = FakeNative()
        self.bridge.desktop_native = native
        self.bridge.rpc = types.MethodType(Bridge.rpc, self.bridge)
        self.bridge._rpc_once = Mock(side_effect=AssertionError('engine rpc used'))
        return native

    def test_delegate_creates_dispatches_and_arms_atomically(self):
        result = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertTrue(result['created'])
        self.assertFalse(result['conflict'])
        self.assertTrue(result['agent_id'].startswith('agent_'))
        self.assertTrue(result['job_id'].startswith('job_'))
        self.assertEqual(result['session_id'], 'sess_agent_1')
        self.assertEqual(result['workspace'], str(self.workspace.resolve()))
        self.assertEqual(result['model'], MODEL)
        self.assertEqual(result['send']['state'], 'accepted')
        self.assertTrue(result['subscription']['armed'])
        self.assertEqual(result['delivery']['state'], 'pending')
        self.assertIn('event_cursor', result['cursor'])
        self.assertGreaterEqual(result['cursor']['event_cursor'], 0)

        self.assertEqual(self.fake.created, 1)
        self.assertEqual(self.fake.deferred, 1)
        self.assertEqual(len(self.fake.send_calls), 1)
        content = self.fake.send_calls[0]['content']
        self.assertTrue(content.startswith('修复 backend 的失败测试'))
        self.assertIn('superagent-contract-v1', content)
        job = self.bridge.superagent.get(result['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertTrue(job['notification']['armed'])
        self.assertIsNotNone(job['contract']['sent_at'])

    def test_delegate_replay_does_not_touch_the_engine(self):
        first = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        calls_before = (self.fake.deferred, self.fake.created, len(self.fake.send_calls))
        second = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertFalse(second['created'])
        self.assertEqual(first['agent_id'], second['agent_id'])
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual((self.fake.deferred, self.fake.created, len(self.fake.send_calls)),
                         calls_before)
        self.assertEqual(len(self.bridge.superagent.list('chat-A')), 1)
        conflict = self.call('zcode_delegate', self.delegate_args(
            notify=self.authorized_notify(), task='一个不同的任务'))
        self.assertFalse(conflict['created'])
        self.assertTrue(conflict['conflict'])
        self.assertEqual(conflict['job_id'], first['job_id'])

    def test_unauthorized_notify_returns_armed_false_but_still_sends(self):
        result = self.call('zcode_delegate', self.delegate_args(
            notify={'completion_notify': True, 'user_authorized': False}))
        self.assertTrue(result['created'])
        self.assertEqual(result['send']['state'], 'accepted')
        self.assertFalse(result['subscription']['armed'])
        self.assertEqual(result['subscription']['reason'], 'user_authorized_false')
        self.assertEqual(result['delivery']['state'], 'not_armed')
        self.assertEqual(len(self.fake.send_calls), 1)
        self.assertEqual(self.bridge.notifications.all_subscriptions(), [])

    def test_send_uncertain_is_returned_and_never_retried(self):
        self.fake.send_error = RuntimeError('engine disconnected')
        result = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertEqual(result['send']['state'], 'uncertain')
        self.assertTrue(result['send']['retry_required'])
        self.assertEqual(result['delivery']['state'], 'uncertain')
        self.assertEqual(result['job']['status'], 'send_uncertain')
        self.assertEqual(len(self.fake.send_calls), 1)

        replay = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertFalse(replay['created'])
        self.assertEqual(replay['send']['state'], 'uncertain')
        self.assertEqual(len(self.fake.send_calls), 1)

    def test_legacy_plain_and_notify_sends_omit_the_bridge_permission_field(self):
        # Strict fake mirrors 3.18.2's Zod object; a lenient stub would hide
        # the extra key that caused the real -32602.
        self.fake.strict_send_schema = True
        plain = self.call('zcode_delegate', self.delegate_args(key='strict-plain'))
        self.assertEqual(plain['send']['state'], 'accepted')
        self.assertEqual(set(self.fake.send_calls[-1]),
                         {'sessionId', 'content', 'inputId'})
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        direct = self.call('zcode_send', {
            'session_id': sid, 'content': 'strict direct',
            'caller_permission_mode': 'review_important_actions'})
        self.assertTrue(direct['accepted'])
        self.assertEqual(set(self.fake.send_calls[-1]),
                         {'sessionId', 'content', 'inputId'})
        notified = self.call('zcode_send', {
            'session_id': sid, 'content': 'strict notify',
            'caller_permission_mode': 'review_important_actions',
            'notify': self.authorized_notify(), 'idempotency_key': 'strict-notify'})
        self.assertTrue(notified['accepted'])
        self.assertEqual(set(self.fake.send_calls[-1]),
                         {'sessionId', 'content', 'inputId'})

    def test_known_schema_rejection_is_failed_not_uncertain(self):
        rejection = ProtocolError({
            'code': -32602, 'message': 'Invalid params',
            'data': {'name': 'ZodError', 'message': 'unrecognized_keys',
                     'keys': ['caller_permission_mode'], 'path': []}})
        self.fake.send_error = rejection
        result = self.call('zcode_delegate', self.delegate_args(key='schema-plain'))
        self.assertEqual(result['send']['state'], 'failed')
        self.assertFalse(result['send']['retry_required'])
        self.assertEqual(result['job']['status'], 'failed')
        self.assertNotEqual(result['job']['status'], 'send_uncertain')
        notified = self.call('zcode_delegate', self.delegate_args(
            key='schema-notify', notify=self.authorized_notify()))
        self.assertEqual(notified['send']['state'], 'failed')
        self.assertTrue(notified['created'])
        sub = self.bridge.notifications.find_subscription(
            notified['subscription']['subscription_id'], 'chat-A')
        self.assertEqual(sub['state'], 'send_not_accepted')

    def test_delegate_rejects_unknown_model_without_creating_a_job(self):
        args = self.delegate_args(model={'providerId': 'p', 'modelId': 'missing'})
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_delegate', args)
        self.assertIn('model_not_in_catalog', str(caught.exception))
        self.assertEqual(self.fake.created, 0)
        self.assertEqual(self.bridge.superagent.list('chat-A'), [])

    def test_delegate_rejects_missing_workspace(self):
        args = self.delegate_args(workspace=str(Path(self.tmp.name) / 'missing'))
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_delegate', args)
        self.assertIn('workspace_required', str(caught.exception))
        self.assertEqual(self.fake.created, 0)

    def test_busy_message_is_queued_and_drained_on_idle(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.status = 'running'
        queued = self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '补充：先跑单元测试。',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'msg-1'})
        self.assertEqual(queued['state'], 'queued')
        self.assertEqual(queued['position'], 1)
        self.assertEqual(len(self.fake.send_calls), 1)

        self.fake.status = 'idle'
        self.bridge.drain_superagent_queues()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['queue'], [])
        self.assertEqual(len(job['delivered_messages']), 1)
        self.assertEqual(len(self.fake.send_calls), 2)
        self.assertTrue(self.fake.send_calls[-1]['content'].startswith('补充：先跑单元测试。'))
        self.assertIn('superagent-contract-v1', self.fake.send_calls[-1]['content'])

    def test_queued_message_survives_restart_and_drains(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.status = 'running'
        self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '重启后仍应投递。',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'msg-restart'})
        reopened = self.make_bridge()
        reopened.resident.add(delegate['session_id'])
        self.fake.status = 'idle'
        reopened.drain_superagent_queues()
        job = reopened.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['queue'], [])
        self.assertEqual(len(job['delivered_messages']), 1)
        self.assertTrue(self.fake.send_calls[-1]['content'].startswith('重启后仍应投递。'))

    def test_message_uncertain_is_not_retried(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.send_error = RuntimeError('send failed')
        first = self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '纠正消息',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'msg-uncertain'})
        self.assertEqual(first['state'], 'uncertain')
        self.assertTrue(first['retry_required'])
        count = len(self.fake.send_calls)
        second = self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '重复纠正',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'msg-uncertain'})
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['state'], 'uncertain')
        self.assertEqual(len(self.fake.send_calls), count)

    def test_message_idempotency_prevents_second_delivery(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        first = self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '只发送一次。',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'msg-once'})
        self.assertEqual(first['state'], 'delivered')
        count = len(self.fake.send_calls)
        second = self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '重复内容',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'msg-once'})
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['state'], 'delivered')
        self.assertEqual(len(self.fake.send_calls), count)

    def test_queue_claim_crash_is_recovered_without_resend(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.status = 'running'
        queued = self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '崩溃后不得重发。',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'msg-crash'})
        self.assertEqual(queued['state'], 'queued')
        sends_before = len(self.fake.send_calls)
        # Persisted claim, then the simulated process exit before the outcome.
        claimed = self.bridge.superagent.claim_next_message(delegate['job_id'], 'chat-A')
        self.assertIsNotNone(claimed)
        self.assertEqual(len(self.fake.send_calls), sends_before)

        self.fake.status = 'idle'
        reopened = self.make_bridge()
        reopened.resident.add(delegate['session_id'])
        reopened.drain_superagent_queues()
        job = reopened.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(len(job['queue']), 1)
        self.assertEqual(job['queue'][0]['state'], 'uncertain')
        self.assertEqual(job['queue'][0]['last_error_type'], 'restart_in_flight')
        self.assertEqual(job['delivered_messages'], [])
        self.assertEqual(len(self.fake.send_calls), sends_before)
        self.assertIn('job.message_uncertain',
                      [row['kind'] for row in job['events']])
        view = reopened.superagent.job_summary(job)['queue']['positions'][0]
        self.assertEqual(view['state'], 'uncertain')
        self.assertEqual(view['last_error_type'], 'restart_in_flight')

    def test_pre_send_failure_is_definite_not_uncertain(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.status = 'idle'
        self.bridge.inherit = Mock(side_effect=RuntimeError('mode unavailable'))
        result = self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '发送前失败。',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'msg-pre-send'})
        self.assertEqual(result['state'], 'not_accepted')
        self.assertFalse(result['retry_required'])
        self.assertEqual(result['drain']['error_code'], 'permission_inherit_failed')
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['queue'][0]['state'], 'not_accepted')
        self.assertEqual(job['queue'][0]['last_error_type'], 'pre_send_failed')
        self.assertEqual(job['status'], 'running')

    def test_delegate_pre_send_failure_is_failed_not_uncertain(self):
        self.bridge.inherit = Mock(side_effect=RuntimeError('mode unavailable'))
        result = self.call('zcode_delegate', self.delegate_args(key='pre-send-delegate'))
        self.assertEqual(result['send']['state'], 'failed')
        self.assertFalse(result['send']['retry_required'])
        self.assertFalse(result['send']['accepted'])
        job = self.bridge.superagent.get(result['job_id'], 'chat-A')
        self.assertEqual(job['send']['state'], 'failed')
        replay = self.call('zcode_delegate', self.delegate_args(key='pre-send-delegate'))
        self.assertEqual(replay['send']['state'], 'failed')
        self.assertFalse(replay['send']['accepted'])

    def test_report_cannot_upgrade_uncertain_send_admission(self):
        self.fake.send_error = RuntimeError('engine disconnected')
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertEqual(delegate['send']['state'], 'uncertain')
        self.assertFalse(delegate['send']['accepted'])
        self.bridge.call('zcode_agent_report', {
            'agent_id': delegate['agent_id'], 'session_id': delegate['session_id'],
            'result': {'摘要': '完成', '实际改动': 'x.py'},
            'status': 'completed'}, context=CTX_CHILD)
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['report']['state'], 'unverified_hint')
        self.assertEqual(job['status'], 'send_uncertain')
        self.assertEqual(job['send']['state'], 'uncertain')
        self.assertFalse(job['send']['accepted'])

        replay = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertFalse(replay['created'])
        self.assertEqual(replay['send']['state'], 'uncertain')
        self.assertTrue(replay['send']['retry_required'])
        self.assertFalse(replay['send']['accepted'])
        self.assertEqual(replay['job']['send']['state'], 'uncertain')
        self.assertEqual(len(self.fake.send_calls), 1)

        reopened = self.make_bridge()
        after_restart = self.call('zcode_delegate', self.delegate_args(
            notify=self.authorized_notify()), bridge=reopened)
        self.assertEqual(after_restart['send']['state'], 'uncertain')
        self.assertFalse(after_restart['send']['accepted'])

    def test_report_cannot_upgrade_failed_send_admission(self):
        rejection = ProtocolError({
            'code': -32602, 'message': 'Invalid params',
            'data': {'name': 'ZodError', 'message': 'unrecognized_keys',
                     'keys': ['caller_permission_mode'], 'path': []}})
        self.fake.send_error = rejection
        delegate = self.call('zcode_delegate', self.delegate_args(key='failed-report'))
        self.assertEqual(delegate['send']['state'], 'failed')
        self.bridge.call('zcode_agent_report', {
            'agent_id': delegate['agent_id'], 'session_id': delegate['session_id'],
            'result': {'摘要': '成功'}}, context=CTX_CHILD)
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['send']['state'], 'failed')
        replay = self.call('zcode_delegate', self.delegate_args(key='failed-report'))
        self.assertEqual(replay['send']['state'], 'failed')
        self.assertFalse(replay['send']['accepted'])
        self.assertFalse(replay['send']['retry_required'])

    def test_accepted_send_stays_accepted_after_report_and_restart(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertEqual(delegate['send']['state'], 'accepted')
        self.bridge.call('zcode_agent_report', {
            'agent_id': delegate['agent_id'], 'session_id': delegate['session_id'],
            'result': {'摘要': '完成'}}, context=CTX_CHILD)
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'reported')
        self.assertEqual(job['send']['state'], 'accepted')
        replay = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertEqual(replay['send']['state'], 'accepted')
        self.assertTrue(replay['send']['accepted'])
        self.assertFalse(replay['send']['retry_required'])

        reopened = self.make_bridge()
        after_restart = self.call('zcode_delegate', self.delegate_args(
            notify=self.authorized_notify()), bridge=reopened)
        self.assertEqual(after_restart['send']['state'], 'accepted')
        self.assertTrue(after_restart['send']['accepted'])

    def test_answer_rejects_foreign_request_and_accepts_real_option(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        sid = delegate['session_id']
        self.bridge._request({'id': 'wire-foreign', 'method': 'interaction/requestPermission',
                              'params': {'requestId': 'req-foreign', 'sessionId': 'sess_other',
                                         'toolCallId': 'c1', 'toolName': 'permission',
                                         'options': [{'optionId': 'allow',
                                                      'response': {'decision': 'allow'}}]}})
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegate['agent_id'], 'request_id': 'req-foreign',
                'option_id': 'allow', 'caller_permission_mode': 'full_access'})
        self.assertIn('request_not_owned', str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegate['agent_id'], 'request_id': 'req-missing',
                'response': {'action': 'accept', 'content': {'answers': {}}},
                'caller_permission_mode': 'full_access'})
        self.assertIn('request_not_pending', str(caught.exception))

        self.bridge._request({'id': 'wire-own', 'method': 'interaction/requestPermission',
                              'params': {'requestId': 'req-own', 'sessionId': sid,
                                         'toolCallId': 'c2', 'toolName': 'permission',
                                         'options': [{'optionId': 'allow',
                                                      'response': {'decision': 'allow'}}]}})
        result = self.call('zcode_agent_answer', {
            'agent_id': delegate['agent_id'], 'request_id': 'req-own',
            'option_id': 'allow', 'caller_permission_mode': 'full_access'})
        self.assertTrue(result['answered'])
        self.bridge.client.respond.assert_called_once_with(
            'wire-own', {'decision': 'allow'})

    def test_answer_refuses_authentication_question(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge._request({'id': 'wire-auth', 'method': 'interaction/requestUserInput',
                              'params': {'requestId': 'req-auth',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c3', 'toolName': 'AskUserQuestion',
                                         'input': {'questions': [
                                             {'question': '请输入 API token 以登录'}]}}})
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegate['agent_id'], 'request_id': 'req-auth',
                'response': {'action': 'accept', 'content': {'answers': {}}},
                'caller_permission_mode': 'full_access'})
        self.assertIn('user_answer_required', str(caught.exception))
        self.bridge.client.respond.assert_not_called()

    def test_answer_convenience_builds_native_single_question_response(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        question = 'LIVE-CHECK-QUESTION：是否继续？'
        self.bridge._request({'id': 'wire-ask', 'method': 'interaction/requestUserInput',
                              'params': {'requestId': 'req-ask',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c4', 'toolName': 'AskUserQuestion',
                                         'input': {'questions': [{
                                             'question': question,
                                             'header': 'Check',
                                             'multiSelect': False,
                                             'options': [{'label': '继续'},
                                                         {'label': '停止'}]}]}}})
        result = self.call('zcode_agent_answer', {
            'agent_id': delegate['agent_id'], 'request_id': 'req-ask',
            'answer': '继续', 'caller_permission_mode': 'full_access'})
        self.assertTrue(result['answered'])
        self.bridge.client.respond.assert_called_once_with(
            'wire-ask', {'action': 'accept', 'content': {'answers': {question: '继续'}}})

    def test_answer_convenience_accepts_top_level_question_and_value(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge._request({'id': 'wire-ask2', 'method': 'interaction/requestUserInput',
                              'params': {'requestId': 'req-ask2',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c5', 'toolName': 'AskUserQuestion',
                                         'questions': [{
                                             'question': '选择模式',
                                             'options': [{'label': '快速', 'value': 'fast'},
                                                         {'label': '完整', 'value': 'full'}]}]}})
        self.call('zcode_agent_answer', {
            'agent_id': delegate['agent_id'], 'request_id': 'req-ask2',
            'answer': 'full', 'caller_permission_mode': 'full_access'})
        self.bridge.client.respond.assert_called_once_with(
            'wire-ask2', {'action': 'accept', 'content': {'answers': {'选择模式': 'full'}}})

    def test_answer_convenience_refuses_ambiguous_or_conflicting_inputs(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        cases = [
            ('two questions', {'input': {'questions': [
                {'question': 'A', 'options': [{'label': 'x'}]},
                {'question': 'B', 'options': [{'label': 'y'}]}]}}, 'answer_ambiguous'),
            ('multi select', {'input': {'questions': [
                {'question': 'A', 'multiSelect': True,
                 'options': [{'label': 'x'}, {'label': 'y'}]}]}}, 'answer_requires_response'),
            ('free text', {'input': {'questions': [{'question': 'A'}]}},
             'answer_requires_response'),
        ]
        for index, (label, params, expected) in enumerate(cases):
            request_id = 'req-amb-' + str(index)
            self.bridge._request({'id': 'wire-amb-' + str(index),
                                  'method': 'interaction/requestUserInput',
                                  'params': {'requestId': request_id,
                                             'sessionId': delegate['session_id'],
                                             'toolCallId': 'c6', **params}})
            with self.assertRaises(ValueError) as caught:
                self.call('zcode_agent_answer', {
                    'agent_id': delegate['agent_id'], 'request_id': request_id,
                    'answer': 'x', 'caller_permission_mode': 'full_access'})
            self.assertIn(expected, str(caught.exception), label)

        self.bridge._request({'id': 'wire-amb-conflict',
                              'method': 'interaction/requestUserInput',
                              'params': {'requestId': 'req-amb-conflict',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c7',
                                         'input': {'questions': [{
                                             'question': 'A',
                                             'options': [{'label': 'x'}]}]}}})
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegate['agent_id'], 'request_id': 'req-amb-conflict',
                'answer': 'x', 'response': {'action': 'accept', 'content': {'answers': {}}},
                'caller_permission_mode': 'full_access'})
        self.assertIn('answer_conflict', str(caught.exception))

        self.bridge._request({'id': 'wire-perm-answer',
                              'method': 'interaction/requestPermission',
                              'params': {'requestId': 'req-perm-answer',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c8', 'toolName': 'permission',
                                         'options': [{'optionId': 'allow',
                                                      'response': {'decision': 'allow'}}]}})
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegate['agent_id'], 'request_id': 'req-perm-answer',
                'answer': 'allow', 'caller_permission_mode': 'full_access'})
        self.assertIn('answer_requires_option', str(caught.exception))

    def test_answer_convenience_keeps_user_preference_protection(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge._request({'id': 'wire-pref', 'method': 'interaction/requestUserInput',
                              'params': {'requestId': 'req-pref',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c9', 'toolName': 'AskUserQuestion',
                                         'input': {'questions': [{
                                             'question': '你喜欢哪个方案？',
                                             'options': [{'label': 'A'}, {'label': 'B'}]}]}}})
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegate['agent_id'], 'request_id': 'req-pref',
                'answer': 'A', 'caller_permission_mode': 'full_access'})
        self.assertIn('user_answer_required', str(caught.exception))

    def test_answer_technical_permission_ignores_workflow_code_tokens(self):
        """A CreateWorkflow approval is a tool permission, not a credential question.

        The real pending request embeds the workflow script.  Words such as
        ``total_tokens`` and ``password`` in that data must not turn a technical
        permission into an authentication question.
        """
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        script = ("const stats = await runBenchmark();\n"
                  "const passwordField = 'not-a-credential';\n"
                  "report({ total_tokens: stats.totalTokens, api_key_name: 'K' });")
        self.bridge._request({'id': 'wire-workflow',
                              'method': 'interaction/requestPermission',
                              'params': {
                                  'requestId': 'req-workflow',
                                  'sessionId': delegate['session_id'],
                                  'toolCallId': 'call-workflow',
                                  'toolName': 'CreateWorkflow',
                                  'reason': 'Run the offline benchmark that counts total_tokens',
                                  'input': {'task': 'offline benchmark', 'script': script},
                                  'options': [
                                      {'optionId': 'allow_once',
                                       'response': {'decision': 'allow'}},
                                      {'optionId': 'deny',
                                       'response': {'decision': 'deny'}}]}})
        result = self.call('zcode_agent_answer', {
            'agent_id': delegate['agent_id'], 'request_id': 'req-workflow',
            'option_id': 'allow_once', 'caller_permission_mode': 'full_access'})
        self.assertTrue(result['answered'])
        self.bridge.client.respond.assert_called_once_with(
            'wire-workflow', {'decision': 'allow'})

    def test_native_technical_permission_ignores_workflow_code_tokens(self):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '做一个离线 benchmark', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-wf'})
        sid = delegated['session_id']
        native.snapshots[sid]['projection']['pendingPermissions'] = [{
            'requestId': 'perm-workflow', 'toolCallId': 'call-workflow',
            'toolName': 'CreateWorkflow', 'reason': 'benchmark total_tokens',
            'riskLevel': 'low',
            'input': {'task': 'offline benchmark',
                      'script': "const password = 'demo'; total_tokens += 1;"},
            'options': [
                {'optionId': 'allow_once', 'label': 'Allow once', 'kind': 'allowOnce'},
                {'optionId': 'deny', 'label': 'Deny', 'kind': 'deny'}]}]
        answered = self.call('zcode_agent_answer', {
            'agent_id': delegated['agent_id'], 'request_id': 'perm-workflow',
            'option_id': 'allow_once', 'caller_permission_mode': 'full_access'})
        self.assertTrue(answered['answered'])
        self.assertEqual(len(native.resolve_calls), 1)
        self.assertEqual(native.resolve_calls[0]['interaction_id'], 'perm-workflow')
        self.assertEqual(native.resolve_calls[0]['answer'], {'optionId': 'allow_once'})

    def test_answer_still_refuses_real_credential_question(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge._request({'id': 'wire-real-auth',
                              'method': 'interaction/requestUserInput',
                              'params': {
                                  'requestId': 'req-real-auth',
                                  'sessionId': delegate['session_id'],
                                  'toolCallId': 'c-real-auth',
                                  'toolName': 'AskUserQuestion',
                                  'input': {'questions': [{
                                      'question': '请输入账户 password 以完成 OAuth 登录',
                                      'options': [{'label': '稍后'}]}]}}})
        for extra in ({}, {'user_approved': True}):
            with self.assertRaises(ValueError) as caught:
                self.call('zcode_agent_answer', {
                    'agent_id': delegate['agent_id'], 'request_id': 'req-real-auth',
                    'response': {'action': 'accept', 'content': {'answers': {}}},
                    'caller_permission_mode': 'full_access', **extra})
            self.assertIn('user_answer_required', str(caught.exception))
        self.bridge.client.respond.assert_not_called()

    def test_answer_sensitive_question_flag_still_requires_user(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge._request({'id': 'wire-sensitive',
                              'method': 'interaction/requestUserInput',
                              'params': {'requestId': 'req-sensitive',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c-sensitive',
                                         'toolName': 'AskUserQuestion',
                                         'input': {'questions': [{
                                             'question': '请选择要继续的方式',
                                             'sensitive': True,
                                             'options': [{'label': 'A'}, {'label': 'B'}]}]}}})
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegate['agent_id'], 'request_id': 'req-sensitive',
                'answer': 'A', 'caller_permission_mode': 'full_access'})
        self.assertIn('user_answer_required', str(caught.exception))
        self.bridge.client.respond.assert_not_called()

    def test_answer_unreadable_user_input_stays_conservative(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge._request({'id': 'wire-unknown', 'method': 'interaction/requestUserInput',
                              'params': {'requestId': 'req-unknown',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c-unknown',
                                         'toolName': 'AskUserQuestion',
                                         'input': {'script': 'password = token'}}})
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegate['agent_id'], 'request_id': 'req-unknown',
                'response': {'action': 'accept', 'content': {'answers': {}}},
                'caller_permission_mode': 'full_access', 'user_approved': True})
        self.assertIn('user_answer_required', str(caught.exception))
        self.bridge.client.respond.assert_not_called()

    def test_subscribed_workflow_cursor_progress_prevents_false_stall(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = time.time()
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='m-parent',
            run_cursors={'epoch:run-1': 34})
        self.bridge.superagent.clock = lambda: base - 120
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id='m-parent', status='idle')
        self.bridge.superagent.clock = time.time
        self.bridge.sync_superagent_jobs(base)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 34)

        # Parent status/message id are unchanged and the run merely says
        # "running": that alone must not refresh the stall clock.
        self.bridge.sync_superagent_jobs(base + 120)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')

        # A real journal cursor advance is observable progress and recovers.
        self.bridge.notifications.touch(sub_id, run_cursors={'epoch:run-1': 91})
        self.bridge.sync_superagent_jobs(base + 121)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 91)
        self.assertIn('job.resumed', [row['kind'] for row in job['events']])

    def test_subscribed_new_child_is_progress_but_silence_stalls(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = time.time()
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='m-parent',
            known_child_ids=['child-1'])
        self.bridge.sync_superagent_jobs(base)
        self.bridge.sync_superagent_jobs(base + 120)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')

        self.bridge.notifications.touch(sub_id, known_child_ids=['child-1', 'child-2'])
        self.bridge.sync_superagent_jobs(base + 121)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['activity']['children'], 'child-1,child-2')

    def test_subscribed_cursor_34_61_91_at_65s_is_not_stalled(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = 1000.0
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='main-final',
            run_cursors={'stream-1:run-test': 34})
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id='main-final', status='idle',
            run_watermarks={'run-test': {'epoch': 'stream-1', 'cursor': 34}}, at=base)
        self.bridge.sync_superagent_jobs(base)
        self.bridge.notifications.touch(sub_id, run_cursors={'stream-1:run-test': 61})
        self.bridge.sync_superagent_jobs(base + 30)
        self.bridge.notifications.touch(sub_id, run_cursors={'stream-1:run-test': 91})
        self.bridge.sync_superagent_jobs(base + 65)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['at'], base + 65)
        self.assertEqual(job['last_progress']['run_watermarks']['run-test']['cursor'], 91)

    def test_subscribed_cursor_frozen_34_stalls_at_65s(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = 1000.0
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='main-final',
            run_cursors={'stream-1:run-test': 34})
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id='main-final', status='idle',
            run_watermarks={'run-test': {'epoch': 'stream-1', 'cursor': 34}}, at=base)
        for moment in (base, base + 30, base + 65):
            self.bridge.sync_superagent_jobs(moment)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run-test']['cursor'], 34)

    def test_subscribed_cursor_rewind_or_reorder_is_not_progress(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = 1000.0
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='main-final',
            run_cursors={'stream-1:run-test': 91})
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id='main-final', status='idle',
            run_watermarks={'run-test': {'epoch': 'stream-1', 'cursor': 91}}, at=base)
        for moment, cursors in ((base + 30, {'stream-1:run-test': 34}),
                                (base + 65, {'stream-1:run-test': 61}),
                                (base + 70, {'stream-1:run-test': 91})):
            self.bridge.notifications.touch(sub_id, run_cursors=cursors)
            self.bridge.sync_superagent_jobs(moment)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run-test']['cursor'], 91)

    def test_subscribed_child_stream_update_recovers_stalled(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = time.time()
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='main-final',
            last_child_activity='rev-351')
        self.bridge.sync_superagent_jobs(base)
        self.bridge.sync_superagent_jobs(base + 120)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.bridge.notifications.touch(sub_id, last_child_activity='rev-600')
        self.bridge.sync_superagent_jobs(base + 121)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['activity']['child_stream'], 'rev-600')

    def test_subscribed_terminal_truth_wins_over_advancing_cursor(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        sub_id = delegate['subscription']['subscription_id']
        self.bridge.notifications.touch(
            sub_id, state='completed', state_reason='idle_stable',
            run_cursors={'epoch:run-1': 91})
        self.bridge.sync_superagent_jobs(time.time() + 600)
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'verified')

    def test_unsubscribed_workflow_cursor_progress_prevents_false_stall(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'working'}]}]
        self.fake.runs = [{'runId': 'run-1', 'status': 'running'}]
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 35)]
        base = time.time()
        self.bridge.sync_superagent_jobs(base)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 34)

        # Parent status and message id stay identical; the journal advances.
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 92)]
        self.bridge.sync_superagent_jobs(base + 120)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 91)

        # Genuine silence still stalls.
        self.bridge.sync_superagent_jobs(base + 240)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')

        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 120)]
        self.bridge.sync_superagent_jobs(base + 300)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 119)

    def test_unsubscribed_cursor_34_61_91_at_65s_is_not_stalled(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'main-final', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'done'}]}]
        self.fake.runs = [{'runId': 'run-test', 'status': 'running'}]
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='poll', message_id='main-final', status='idle',
            run_watermarks={'run-test': {'epoch': '', 'cursor': 34}}, at=base)
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 35)]
        self.bridge.sync_superagent_jobs(base)
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 62)]
        self.bridge.sync_superagent_jobs(base + 30)
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 92)]
        self.bridge.sync_superagent_jobs(base + 65)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['at'], base + 65)
        self.assertEqual(job['last_progress']['run_watermarks']['run-test']['cursor'], 91)

    def test_unsubscribed_cursor_frozen_34_stalls_at_65s(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'main-final', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'done'}]}]
        self.fake.runs = [{'runId': 'run-test', 'status': 'running'}]
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 35)]
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='poll', message_id='main-final', status='idle',
            run_watermarks={'run-test': {'epoch': '', 'cursor': 34}}, at=base)
        for moment in (base, base + 30, base + 65):
            self.bridge.sync_superagent_jobs(moment)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run-test']['cursor'], 34)

    def test_unsubscribed_cursor_91_0_91_0_91_does_not_refresh(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'done'}]}]
        self.fake.runs = [{'runId': 'run-1', 'status': 'running'}]
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='poll', message_id='m1', status='idle',
            run_watermarks={'run-1': {'epoch': '', 'cursor': 91}}, at=base)
        for offset, cursor in ((0, 91), (30, 0), (60, 91), (90, 0), (120, 91)):
            self.fake.workflow_events = [
                {'type': 'node-started', 'sequence': seq, 'payload': {}}
                for seq in range(1, cursor + 1)]
            self.bridge.sync_superagent_jobs(base + offset)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 91)

    def test_unsubscribed_same_message_update_counts_as_progress(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'a'}]}]
        base = time.time()
        self.bridge.sync_superagent_jobs(base)
        first = self.bridge.superagent.get(job_id, 'chat-A')
        first_digest = first['last_progress']['activity']['message']
        self.assertTrue(first_digest.startswith('m1#'))

        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text',
                                          'text': 'a much longer streamed answer'}]}]
        self.bridge.sync_superagent_jobs(base + 120)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertNotEqual(job['last_progress']['activity']['message'], first_digest)

        self.bridge.sync_superagent_jobs(base + 240)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')

    def test_unsubscribed_read_failure_is_not_progress(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'working'}]}]
        self.fake.runs = [{'runId': 'run-1', 'status': 'running'}]
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 35)]
        base = time.time()
        self.bridge.sync_superagent_jobs(base)
        self.fake.fail_workflow_runs = True
        for index in range(5):
            self.bridge.sync_superagent_jobs(base + 120 + index * 10)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'unobservable')
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 34)

        # Reads recover and the journal advances: the job is observable again
        # and the new cursor is real progress.
        self.fake.fail_workflow_runs = False
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 60)]
        self.bridge.sync_superagent_jobs(base + 300)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 59)
        kinds = [row['kind'] for row in job['events']]
        self.assertIn('job.unobservable', kinds)
        self.assertIn('job.resumed', kinds)
        self.assertIn('observer_recovered', [row['reason'] for row in job['events']])

    def test_unsubscribed_terminal_engine_state_wins_over_read_failures(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        self.fake.status = 'cancelled'
        self.fake.fail_workflow_runs = True
        self.bridge.sync_superagent_jobs(time.time() + 10)
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(job['status_reason'], 'engine_cancelled')

    def test_unsubscribed_new_child_is_progress_but_silence_stalls(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'working'}]}]
        base = time.time()
        self.bridge.sync_superagent_jobs(base)
        self.bridge.sync_superagent_jobs(base + 120)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')

        self.fake.child_ids = ['child-1']
        self.bridge.sync_superagent_jobs(base + 125)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['activity']['children'], 'child-1')

        self.bridge.sync_superagent_jobs(base + 250)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')

    def test_cursor_watermarks_advance_only_strictly_forward(self):
        previous = {'run-1': {'epoch': 'e', 'cursor': 34},
                    'run-2': {'epoch': 'e', 'cursor': 5}}
        # Unchanged, reordered, backward and zero-cursor new runs never advance.
        for current in (
                {'run-1': {'epoch': 'e', 'cursor': 34}},
                {'run-2': {'epoch': 'e', 'cursor': 5},
                 'run-1': {'epoch': 'e', 'cursor': 34}},
                {'run-1': {'epoch': 'e', 'cursor': 20},
                 'run-2': {'epoch': 'e', 'cursor': 5}},
                {'run-1': {'epoch': 'e', 'cursor': 34},
                 'run-2': {'epoch': 'e', 'cursor': 5},
                 'run-3': {'epoch': 'e', 'cursor': 0}}):
            advanced, merged = self.bridge._advance_watermarks(previous, current)
            self.assertFalse(advanced, current)
            self.assertEqual(merged['run-1']['cursor'], 34)
            self.assertEqual(merged['run-2']['cursor'], 5)
        # Only a strictly greater same-run cursor (or a new run with events)
        # is progress.
        advanced, merged = self.bridge._advance_watermarks(
            previous, {'run-1': {'epoch': 'e', 'cursor': 61},
                       'run-2': {'epoch': 'e', 'cursor': 5}})
        self.assertTrue(advanced)
        self.assertEqual(merged['run-1']['cursor'], 61)
        advanced, merged = self.bridge._advance_watermarks(
            previous, {'run-3': {'epoch': 'e', 'cursor': 1}})
        self.assertTrue(advanced)
        self.assertEqual(merged['run-3']['cursor'], 1)
        # A rewind cannot lower the persisted high-water mark.
        advanced, merged = self.bridge._advance_watermarks(
            {'run-1': {'epoch': 'e', 'cursor': 91}},
            {'run-1': {'epoch': 'e', 'cursor': 34}})
        self.assertFalse(advanced)
        self.assertEqual(merged['run-1']['cursor'], 91)

    def test_epoch_churn_without_cursor_advance_is_not_progress(self):
        previous = {'run': {'epoch': 'epoch0', 'cursor': 91}}
        for epoch in ('epoch1', 'epoch2', 'epoch3'):
            advanced, merged = self.bridge._advance_watermarks(
                previous, {'run': {'epoch': epoch, 'cursor': 91}})
            self.assertFalse(advanced, epoch)
            self.assertEqual(merged['run']['cursor'], 91)
            previous = merged
        # An epoch change alone never advances, even with a higher cursor.
        advanced, merged = self.bridge._advance_watermarks(
            previous, {'run': {'epoch': 'epoch4', 'cursor': 92}})
        self.assertFalse(advanced)
        self.assertEqual(merged['run']['epoch'], 'epoch4')
        self.assertEqual(merged['run']['cursor'], 92)
        # A later strictly forward cursor in the same new epoch does.
        advanced, merged = self.bridge._advance_watermarks(
            merged, {'run': {'epoch': 'epoch4', 'cursor': 93}})
        self.assertTrue(advanced)
        self.assertEqual(merged['run']['cursor'], 93)

    def test_subscribed_epoch_churn_does_not_refresh_forever(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id='main-final', status='idle',
            run_watermarks={'run': {'epoch': 'epoch0', 'cursor': 91}}, at=base)
        cursors = {'epoch0:run': 91}
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='main-final',
            run_cursors=dict(cursors))
        self.bridge.sync_superagent_jobs(base)
        for index, moment in enumerate((base + 30, base + 60, base + 90, base + 120),
                                       start=1):
            cursors['epoch' + str(index) + ':run'] = 91
            self.bridge.notifications.touch(sub_id, run_cursors=dict(cursors))
            self.bridge.sync_superagent_jobs(moment)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run']['cursor'], 91)

    def test_subscribed_cursor_91_0_91_0_91_does_not_refresh(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id='main-final', status='idle',
            run_watermarks={'run': {'epoch': 'epoch', 'cursor': 91}}, at=base)
        for offset, cursor in ((0, 91), (30, 0), (60, 91), (90, 0), (120, 91)):
            self.bridge.notifications.touch(
                sub_id, last_status='idle', last_message_id='main-final',
                run_cursors={'epoch:run': cursor})
            self.bridge.sync_superagent_jobs(base + offset)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run']['cursor'], 91)

    def test_unsubscribed_journal_read_errors_do_not_count_as_zero_progress(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'done'}]}]
        self.fake.runs = [{'runId': 'run-1', 'status': 'running'}]
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 92)]
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='poll', message_id='m1', status='idle',
            run_watermarks={'run-1': {'epoch': '', 'cursor': 91}}, at=base)
        self.bridge.sync_superagent_jobs(base)
        for index in range(1, 7):
            self.fake.fail_workflow_events = bool(index % 2)
            self.bridge.sync_superagent_jobs(base + index * 30)
            if index == 1:
                during = self.bridge.superagent.get(job_id, 'chat-A')
                self.assertGreaterEqual(during['read_failures']['consecutive'], 1)
                self.assertEqual(during['last_progress']['at'], base)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run-1']['cursor'], 91)
        self.assertLessEqual(job['read_failures']['consecutive'], 1)

    def test_unsubscribed_consecutive_journal_errors_reach_unobservable(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.runs = [{'runId': 'run-1', 'status': 'running'}]
        self.fake.fail_workflow_events = True
        base = 1000.0
        for index in range(1, 6):
            self.bridge.sync_superagent_jobs(base + index * 10)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'unobservable')
        self.assertGreaterEqual(job['read_failures']['consecutive'], 5)

    def test_unsubscribed_pending_outranks_read_failure_unobservable(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        self.fake.status = 'idle'
        self.bridge._request({'id': 'wire-pending',
                              'method': 'interaction/requestPermission',
                              'params': {'requestId': 'req-pending',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'call-pending',
                                         'toolName': 'permission',
                                         'options': [{'optionId': 'allow'}]}})
        self.fake.fail_workflow_runs = True
        base = 1000.0
        for index in range(6):
            self.bridge.sync_superagent_jobs(base + index * 10)
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'action_required')
        self.assertGreaterEqual(job['read_failures']['consecutive'], 5)

    def test_subscribed_new_epoch_baseline_then_advance(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id='main-final', status='idle',
            run_watermarks={'run': {'epoch': 'e0', 'cursor': 91}}, at=base)
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='main-final',
            run_cursors={'e0:run': 91})
        self.bridge.sync_superagent_jobs(base)
        # New epoch's first cursor is persisted as a baseline, not progress.
        self.bridge.notifications.touch(sub_id, run_cursors={'e1:run': 1})
        self.bridge.sync_superagent_jobs(base + 30)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run'],
                         {'epoch': 'e1', 'cursor': 1})
        # A later strictly greater cursor in that same epoch is real progress.
        self.bridge.notifications.touch(sub_id, run_cursors={'e1:run': 2})
        self.bridge.sync_superagent_jobs(base + 65)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['at'], base + 65)
        self.assertEqual(job['last_progress']['run_watermarks']['run'],
                         {'epoch': 'e1', 'cursor': 2})

    def test_subscribed_new_epoch_baseline_alone_does_not_refresh(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub_id = delegate['subscription']['subscription_id']
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id='main-final', status='idle',
            run_watermarks={'run': {'epoch': 'e0', 'cursor': 91}}, at=base)
        self.bridge.notifications.touch(
            sub_id, last_status='idle', last_message_id='main-final',
            run_cursors={'e0:run': 91})
        self.bridge.sync_superagent_jobs(base)
        self.bridge.notifications.touch(sub_id, run_cursors={'e1:run': 1})
        self.bridge.sync_superagent_jobs(base + 30)
        self.bridge.sync_superagent_jobs(base + 65)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run'],
                         {'epoch': 'e1', 'cursor': 1})

    def test_unsubscribed_new_epoch_baseline_then_advance(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'done'}]}]
        self.fake.runs = [{'runId': 'run-1', 'status': 'running'}]
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='poll', message_id='m1', status='idle',
            run_watermarks={'run-1': {'epoch': 'e0', 'cursor': 91}}, at=base)
        self.fake.runtime = {'streamId': 'e0'}
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 92)]
        self.bridge.sync_superagent_jobs(base)
        for _ in range(3):
            self.bridge.superagent.record_read_failure(
                job_id, 'chat-A', 'synthetic_read_failure')
        self.fake.runtime = {'streamId': 'e1'}
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': 1, 'payload': {}}]
        self.bridge.sync_superagent_jobs(base + 30)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['at'], base)
        self.assertEqual(job['last_progress']['run_watermarks']['run-1'],
                         {'epoch': 'e1', 'cursor': 1})
        # A baseline-only write must not clear the read-failure streak.
        self.assertEqual(job['read_failures']['consecutive'], 3)
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': 1, 'payload': {}},
            {'type': 'node-started', 'sequence': 2, 'payload': {}}]
        self.bridge.sync_superagent_jobs(base + 65)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['at'], base + 65)
        self.assertEqual(job['last_progress']['run_watermarks']['run-1'],
                         {'epoch': 'e1', 'cursor': 2})
        self.assertEqual(job['read_failures']['consecutive'], 0)

    def test_unsubscribed_new_epoch_baseline_survives_bridge_reload(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job_id = delegate['job_id']
        self.fake.status = 'idle'
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'done'}]}]
        self.fake.runs = [{'runId': 'run-1', 'status': 'running'}]
        base = 1000.0
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='poll', message_id='m1', status='idle',
            run_watermarks={'run-1': {'epoch': 'e0', 'cursor': 91}}, at=base)
        self.fake.runtime = {'streamId': 'e0'}
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': seq, 'payload': {}}
            for seq in range(1, 92)]
        self.bridge.sync_superagent_jobs(base)
        self.fake.runtime = {'streamId': 'e1'}
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': 1, 'payload': {}}]
        self.bridge.sync_superagent_jobs(base + 30)
        reopened = self.make_bridge()
        stored = reopened.superagent.get(job_id, 'chat-A')
        self.assertEqual(stored['last_progress']['at'], base)
        self.assertEqual(stored['last_progress']['run_watermarks']['run-1'],
                         {'epoch': 'e1', 'cursor': 1})
        self.fake.workflow_events = [
            {'type': 'node-started', 'sequence': 1, 'payload': {}},
            {'type': 'node-started', 'sequence': 2, 'payload': {}}]
        reopened.sync_superagent_jobs(base + 65)
        job = reopened.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['last_progress']['at'], base + 65)
        self.assertEqual(job['last_progress']['run_watermarks']['run-1'],
                         {'epoch': 'e1', 'cursor': 2})

    def test_report_without_session_uses_bound_job(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        result = self.bridge.call('zcode_agent_report', {
            'agent_id': delegate['agent_id'],
            'result': {'摘要': '完成', '实际改动': 'x.py', '阻碍': ''},
            'status': 'completed'}, context=CTX_CHILD)
        self.assertTrue(result['recorded'])
        self.assertEqual(result['session_id'], delegate['session_id'])
        self.assertEqual(result['sender']['session_source'], 'bound')
        self.assertEqual(result['sender']['attribution'], 'unverified')
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['report']['session_source'], 'bound')
        self.assertFalse(job['report']['source_matches_job'])
        with self.assertRaises(ValueError) as caught:
            self.bridge.call('zcode_agent_report', {
                'agent_id': delegate['agent_id'], 'session_id': 'sess_wrong',
                'result': {'摘要': 'x'}}, context=CTX_CHILD)
        self.assertIn('session_mismatch', str(caught.exception))

    def test_report_env_fallback_is_not_verified_attribution(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        result = self.bridge.call('zcode_agent_report', {
            'agent_id': delegate['agent_id'], 'result': {'摘要': 'x'}},
            context={'thread_id': 'chat-A', 'thread_origin': 'env_fallback'})
        self.assertEqual(result['sender']['attribution'], 'unverified')
        self.assertFalse(result['report']['source_matches_job'])

    def test_inspect_reports_turn_lifecycle_scope(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(inspected['verification_scope'], 'turn_lifecycle')
        self.assertIsNone(inspected['task_success'])
        self.assertEqual(inspected['task_review'], 'needs_parent_review')
        self.assertEqual(inspected['job']['verification_scope'], 'turn_lifecycle')
        self.assertIsNone(inspected['job']['task_success'])
        self.assertEqual(inspected['job']['task_review'], 'needs_parent_review')

    def test_cancel_only_the_selected_job(self):
        first = self.call('zcode_delegate', self.delegate_args(
            key='delegate-1', notify=self.authorized_notify()))
        second = self.call('zcode_delegate', self.delegate_args(
            key='delegate-2', notify=self.authorized_notify()))
        self.assertEqual(first['session_id'], 'sess_agent_1')
        self.assertEqual(second['session_id'], 'sess_agent_2')
        result = self.call('zcode_agent_cancel', {
            'agent_id': first['agent_id'], 'caller_permission_mode': 'full_access'})
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.fake.stops, [{'sessionId': 'sess_agent_1'}])
        self.assertEqual(self.bridge.superagent.get(first['job_id'], 'chat-A')['status'],
                         'cancelled')
        self.assertEqual(self.bridge.superagent.get(second['job_id'], 'chat-A')['status'],
                         'running')
        first_sub = self.bridge.notifications.find_subscription(
            first['subscription']['subscription_id'], 'chat-A')
        second_sub = self.bridge.notifications.find_subscription(
            second['subscription']['subscription_id'], 'chat-A')
        self.assertEqual(first_sub['state'], 'interrupted')
        self.assertIn(second_sub['state'], ('arming', 'armed'))

    def test_cancel_cancels_owned_background_task_then_stops(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.jobs = [{'taskId': 'task-1', 'status': 'running', 'cancellable': True}]
        result = self.call('zcode_agent_cancel', {
            'agent_id': delegate['agent_id'], 'caller_permission_mode': 'full_access'})
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.fake.cancelled_tasks, ['task-1'])
        self.assertEqual(self.fake.stops, [{'sessionId': 'sess_agent_1'}])
        self.assertEqual(self.bridge.superagent.get(delegate['job_id'], 'chat-A')['status'],
                         'cancelled')

    def test_cancel_pending_when_background_will_not_settle(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.jobs = [{'taskId': 'task-1', 'status': 'running', 'cancellable': True}]
        self.fake.cancel_error = RuntimeError('cancel unsupported')
        with patch('zcode_bridge.service.time.sleep', return_value=None), \
                patch('zcode_bridge.service.time.monotonic', side_effect=range(1000, 2000)):
            result = self.call('zcode_agent_cancel', {
                'agent_id': delegate['agent_id'],
                'caller_permission_mode': 'full_access'})
        self.assertFalse(result['cancelled'])
        self.assertTrue(result['pending'])
        self.assertEqual(result['reason'], 'background_cancel_pending')
        self.assertEqual(result['remaining']['background_ids'], ['task-1'])
        self.assertEqual(self.fake.stops, [])
        self.assertEqual(self.bridge.superagent.get(delegate['job_id'], 'chat-A')['status'],
                         'running')

    def test_cancel_pending_when_background_has_no_exact_task_id(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.jobs = [{'status': 'running', 'cancellable': True}]
        result = self.call('zcode_agent_cancel', {
            'agent_id': delegate['agent_id'], 'caller_permission_mode': 'full_access'})
        self.assertFalse(result['cancelled'])
        self.assertTrue(result['pending'])
        self.assertEqual(result['reason'], 'background_cancel_unmapped')
        self.assertEqual(self.fake.stops, [])

    def test_report_is_only_an_unverified_hint(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        result = self.bridge.call('zcode_agent_report', {
            'agent_id': delegate['agent_id'], 'session_id': delegate['session_id'],
            'result': {'摘要': '全部完成', '实际改动': 'backend.py'},
            'status': 'completed'}, context=CTX_CHILD)
        self.assertTrue(result['recorded'])
        self.assertEqual(result['accepted_as'], 'unverified_hint')
        self.assertFalse(result['verified'])
        self.assertTrue(result['poll_requested'])
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'reported')
        self.assertEqual(job['report']['state'], 'unverified_hint')
        self.assertFalse(job['report']['source_matches_job'])
        self.assertEqual([row['kind'] for row in job['events']], ['job.reported'])
        self.assertTrue(job['report_payload']['untrusted'])
        self.assertEqual(job['report_payload']['payload']['摘要'], '全部完成')
        summary = self.bridge.superagent.job_summary(job)
        self.assertNotIn('全部完成', json.dumps(summary, ensure_ascii=False))
        self.assertNotIn('payload', summary['report'])
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertNotIn('全部完成', json.dumps(inspected, ensure_ascii=False))
        self.assertIsNone(inspected['job']['report_payload'])
        with_report = self.call('zcode_agent_inspect', {
            'agent_id': delegate['agent_id'], 'include_report': True})
        self.assertEqual(with_report['job']['report_payload']['payload']['摘要'], '全部完成')
        self.assertEqual(with_report['verdict'], 'reported_unverified')

        with self.assertRaises(ValueError) as caught:
            self.bridge.call('zcode_agent_report', {
                'agent_id': delegate['agent_id'], 'session_id': 'sess_wrong',
                'result': {'摘要': 'x'}}, context=CTX_CHILD)
        self.assertIn('session_mismatch', str(caught.exception))

    def test_inspect_returns_pending_options_and_no_body(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'BODY'}]}]
        self.bridge._request({'id': 'wire-q', 'method': 'interaction/requestPermission',
                              'params': {'requestId': 'req-q',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c4', 'toolName': 'permission',
                                         'options': [{'optionId': 'allow', 'label': 'Allow'},
                                                     {'optionId': 'deny', 'label': 'Deny'}]}})
        result = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(result['last_message_id'], 'm1')
        self.assertEqual(result['pending'][0]['request_id'], 'req-q')
        self.assertEqual([row['option_id'] for row in result['pending'][0]['options']],
                         ['allow', 'deny'])
        self.assertEqual(result['desktop_sync']['state'], 'disabled')
        self.assertFalse(result['desktop_sync']['enabled'])
        self.assertNotIn('BODY', json.dumps(result, ensure_ascii=False))

    def test_reported_is_not_verified_until_terminal_evidence(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge.call('zcode_agent_report', {
            'agent_id': delegate['agent_id'], 'session_id': delegate['session_id'],
            'result': {'摘要': '完成'}}, context=CTX_CHILD)
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'reported')
        self.assertEqual([row['kind'] for row in job['events']], ['job.reported'])

        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(inspected['verdict'], 'reported_unverified')
        self.assertFalse(inspected['confirmed'])
        self.assertFalse(inspected['terminal'])
        self.assertFalse(inspected['success'])

        sub_id = delegate['subscription']['subscription_id']
        self.bridge.notifications.touch(sub_id, state='completed', state_reason='idle_stable')
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'verified')
        self.assertEqual([row['kind'] for row in job['events']].count('job.verified'), 1)
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual([row['kind'] for row in job['events']].count('job.verified'), 1)

        verified = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(verified['verdict'], 'verified')
        self.assertTrue(verified['confirmed'])
        self.assertTrue(verified['terminal'])
        self.assertTrue(verified['success'])

    def test_failure_and_expiry_are_terminal_but_not_success(self):
        first = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge.notifications.touch(first['subscription']['subscription_id'],
                                        state='failed', state_reason='failed_job')
        self.bridge.sync_superagent_jobs()
        failed = self.call('zcode_agent_inspect', {'agent_id': first['agent_id']})
        self.assertEqual(failed['verdict'], 'failed')
        self.assertTrue(failed['terminal'])
        self.assertFalse(failed['confirmed'])
        self.assertFalse(failed['success'])

        second = self.call('zcode_delegate', self.delegate_args(
            key='delegate-expired', notify=self.authorized_notify()))
        self.bridge.notifications.touch(second['subscription']['subscription_id'],
                                        state='expired', state_reason='ttl')
        self.bridge.sync_superagent_jobs()
        expired = self.call('zcode_agent_inspect', {'agent_id': second['agent_id']})
        self.assertEqual(expired['verdict'], 'expired')
        self.assertTrue(expired['terminal'])
        self.assertFalse(expired['success'])
        job = self.bridge.superagent.get(second['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'expired')

    def test_unobservable_and_recovery_events_are_deduplicated(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        sub_id = delegate['subscription']['subscription_id']
        self.bridge.notifications.touch(sub_id, consecutive_errors=5,
                                        observer_degraded=True)
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'unobservable')
        self.assertEqual([row['kind'] for row in job['events']].count('job.unobservable'), 1)
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(inspected['verdict'], 'unobservable')
        self.assertFalse(inspected['confirmed'])
        self.assertFalse(inspected['success'])

        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual([row['kind'] for row in job['events']].count('job.unobservable'), 1)

        self.bridge.notifications.touch(sub_id, consecutive_errors=0,
                                        observer_degraded=False)
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual([row['kind'] for row in job['events']].count('job.resumed'), 1)

    def test_stalled_is_diagnostic_and_never_completion(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        job_id = delegate['job_id']
        sub = self.bridge.notifications.find_subscription(
            delegate['subscription']['subscription_id'], 'chat-A')
        observed_status = str(sub.get('last_status') or '')
        observed_message = str(sub.get('last_message_id') or '')
        base = time.time()
        self.bridge.superagent.clock = lambda: base - 120
        self.bridge.superagent.record_progress(
            job_id, 'chat-A', kind='observer', message_id=observed_message,
            status=observed_status)
        self.bridge.superagent.clock = time.time
        self.bridge.sync_superagent_jobs(base)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        self.assertEqual([row['kind'] for row in job['events']].count('job.stalled'), 1)
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(inspected['verdict'], 'stalled')
        self.assertFalse(inspected['confirmed'])
        self.assertFalse(inspected['success'])

        self.bridge.sync_superagent_jobs(base)
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual([row['kind'] for row in job['events']].count('job.stalled'), 1)

        self.bridge.superagent.record_progress(job_id, 'chat-A', kind='test',
                                                status='progress')
        self.bridge.sync_superagent_jobs(time.time())
        job = self.bridge.superagent.get(job_id, 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual([row['kind'] for row in job['events']].count('job.resumed'), 1)

    def test_pending_action_required_then_resumed(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge._request({'id': 'wire-action', 'method': 'interaction/requestPermission',
                              'params': {'requestId': 'req-action',
                                         'sessionId': delegate['session_id'],
                                         'toolCallId': 'c-action', 'toolName': 'permission',
                                         'options': [{'optionId': 'allow'}]}})
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'action_required')
        self.assertEqual([row['kind'] for row in job['events']].count('job.action_required'), 1)
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(inspected['verdict'], 'action_required')
        self.assertEqual(inspected['pending'][0]['request_id'], 'req-action')

        with self.bridge.lock:
            self.bridge.pending.clear()
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertEqual([row['kind'] for row in job['events']].count('job.resumed'), 1)

    def test_idle_or_accepted_never_completes_a_job(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertNotIn('job.verified', [row['kind'] for row in job['events']])
        self.bridge.notifications.touch(delegate['subscription']['subscription_id'],
                                        state='paused', state_reason='stopped')
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'running')
        self.assertNotIn('job.verified', [row['kind'] for row in job['events']])

    def test_terminal_transition_requests_one_merged_desktop_sync(self):
        self.bridge.desktop_sync = Mock()
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        reasons = [call.kwargs.get('reason')
                   for call in self.bridge.desktop_sync.request_sync.call_args_list]
        self.assertIn('create', reasons)
        self.assertIn('delegate', reasons)
        self.bridge.notifications.touch(delegate['subscription']['subscription_id'],
                                        state='completed', state_reason='idle_stable')
        self.bridge.sync_superagent_jobs()
        self.bridge.sync_superagent_jobs()
        reasons = [call.kwargs.get('reason')
                   for call in self.bridge.desktop_sync.request_sync.call_args_list]
        self.assertEqual(reasons.count('terminal'), 1)

    def test_desktop_sync_failure_does_not_block_delegate(self):
        self.bridge.desktop_sync = Mock()
        self.bridge.desktop_sync.request_sync.side_effect = RuntimeError('sync down')
        result = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.assertTrue(result['created'])
        self.assertEqual(result['send']['state'], 'accepted')
        self.assertEqual(result['job']['status'], 'running')

    def test_progress_is_merged_without_job_events(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        sub_id = delegate['subscription']['subscription_id']
        self.bridge.notifications.touch(sub_id, last_status='running',
                                        last_message_id='m1')
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['last_progress']['message_id'], 'm1')
        self.assertEqual(job['events'], [])

        self.bridge.notifications.touch(sub_id, last_message_id='m2')
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['last_progress']['message_id'], 'm2')
        self.assertEqual(job['events'], [])

    def test_dwf_actor_pending_is_attributed_to_the_job(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        actor_sid = 'sess_dwf-run-1-actor_1'
        self.fake.runs = [{'runId': 'run-1', 'status': 'running'}]
        self.fake.workflow_events = [{'type': 'actor-created', 'sequence': 1, 'payload': {
            'actor': {'siteId': 'actor', 'ordinal': 1}, 'name': 'writer',
            'phaseName': 'p', 'persona': {}}}]
        self.bridge._request({'id': 'wire-actor', 'method': 'interaction/requestPermission',
                              'params': {'requestId': 'req-actor', 'sessionId': actor_sid,
                                         'toolCallId': 'c-actor', 'toolName': 'permission',
                                         'options': [{'optionId': 'allow'}]}})
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual([row['request_id'] for row in inspected['pending']], ['req-actor'])
        sub = self.bridge.notifications.find_subscription(
            delegate['subscription']['subscription_id'], 'chat-A')
        self.assertIn(actor_sid, sub['known_child_ids'])
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'action_required')

    def test_terminal_job_closes_queued_messages_with_stable_event(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.status = 'running'
        queued = self.call('zcode_agent_message', {
            'agent_id': delegate['agent_id'], 'content': '迟到的纠正',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'late-msg'})
        self.assertEqual(queued['state'], 'queued')
        self.bridge.notifications.touch(delegate['subscription']['subscription_id'],
                                        state='completed', state_reason='idle_stable')
        self.bridge.sync_superagent_jobs()
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'verified')
        self.assertEqual(job['queue'][0]['state'], 'not_delivered')
        self.assertIn('job.message_not_delivered',
                      [row['kind'] for row in job['events']])
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(inspected['job']['queue']['positions'][0]['state'],
                         'not_delivered')
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_message', {
                'agent_id': delegate['agent_id'], 'content': '不能再发',
                'caller_permission_mode': 'full_access'})
        self.assertIn('agent_terminal', str(caught.exception))

    def test_repeated_inspect_does_not_reset_stalled_timer(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.messages = [{'info': {'id': 'm1', 'role': 'assistant'},
                               'parts': [{'type': 'text', 'text': 'ok'}]}]
        self.bridge.notifications.touch(delegate['subscription']['subscription_id'],
                                        last_status='running', last_message_id='m1')
        self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        first = self.bridge.superagent.get(delegate['job_id'], 'chat-A')['last_progress']['at']
        time.sleep(0.02)
        self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        second = self.bridge.superagent.get(delegate['job_id'], 'chat-A')['last_progress']['at']
        self.assertEqual(first, second)

    def test_inspect_reports_subagent_and_workflow_read_errors(self):
        delegate = self.call('zcode_delegate', self.delegate_args(notify=self.authorized_notify()))
        self.fake.fail_subagents = True
        self.fake.fail_workflow_runs = True
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(inspected['errors']['subagents'], 'RuntimeError')
        self.assertEqual(inspected['errors']['workflows'], 'RuntimeError')
        self.assertEqual(inspected['subagents']['read_error'], 'RuntimeError')
        self.assertEqual(inspected['workflows']['read_error'], 'RuntimeError')
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertGreaterEqual(job['read_failures']['consecutive'], 2)
        # The response summary is re-read after this call's writes.
        self.assertEqual(inspected['job']['read_failures']['consecutive'],
                         job['read_failures']['consecutive'])

    def test_background_task_id_is_a_real_key(self):
        rows = self.bridge.observe_jobs({'projection': {'backgroundJobs': [
            {'taskId': 'task-1', 'status': 'running'}]}})
        self.assertEqual(rows, [{'id': 'task-1', 'status': 'running'}])

    def test_unsubscribed_job_stalls_but_is_never_verified(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        self.assertFalse(delegate['subscription']['armed'])
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.bridge.sync_superagent_jobs(now=float(job['created_at']) + 120)
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(job['status'], 'stalled')
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertTrue(inspected['job']['observation']['engine_idle'])
        self.assertNotIn(inspected['verdict'], ('verified', 'completed'))
        self.assertFalse(inspected['confirmed'])

    def test_unsubscribed_job_reports_engine_idle_as_unverified(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.fake.status = 'idle'
        self.bridge.sync_superagent_jobs(now=float(job['created_at']) + 1)
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegate['agent_id']})
        self.assertEqual(inspected['verdict'], 'engine_idle_unverified')
        self.assertFalse(inspected['confirmed'])
        self.assertEqual(
            self.bridge.superagent.get(delegate['job_id'], 'chat-A')['status'], 'running')

    def test_unsubscribed_job_read_failures_become_unobservable(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.bridge.rpc = Mock(side_effect=RuntimeError('engine disconnected'))
        for index in range(6):
            self.bridge.sync_superagent_jobs(now=float(job['created_at']) + 10 * (index + 1))
        stored = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(stored['status'], 'unobservable')
        self.assertGreaterEqual(stored['read_failures']['consecutive'], 5)

    def test_unsubscribed_job_engine_error_fails_without_verifying(self):
        delegate = self.call('zcode_delegate', self.delegate_args())
        job = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.fake.status = 'error'
        self.bridge.sync_superagent_jobs(now=float(job['created_at']) + 1)
        stored = self.bridge.superagent.get(delegate['job_id'], 'chat-A')
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['status_reason'], 'engine_error')

    def test_fresh_config_delegate_refuses_home_workspace(self):
        self.cfg.stop()
        try:
            with patch.dict(os.environ, {'ZCODE_BRIDGE_STATE': self.tmp.name,
                                         'ZCODE_WORKSPACE': '',
                                         'ZCODE_DEFAULT_WORKSPACE': ''}):
                bridge = self.make_bridge()
                with self.assertRaises(ValueError) as caught:
                    bridge.call('zcode_delegate', {
                        'task': '不要动我的主目录',
                        'caller_permission_mode': 'full_access',
                    }, CTX_A)
                self.assertIn('workspace_required', str(caught.exception))
                self.assertEqual(self.fake.created, 0)
        finally:
            self.cfg.start()

    def test_model_catalog_probe_carries_the_configured_effort(self):
        self.call('zcode_models', {'workspace': str(self.workspace)})
        params = self.fake.deferred_params[-1]
        self.assertEqual(params['model'],
                         {'providerId': 'p', 'modelId': 'm',
                          'options': {'reasoningLevel': 'high'}})

    def test_model_catalog_probe_without_default_model_uses_sdk_selection(self):
        cfg = {**storage.defaults(), 'default_model': {'providerId': '', 'modelId': ''},
               'default_thought_level': 'high',
               'default_workspace': str(self.workspace),
               'zcode_executable': str(Path(self.tmp.name) / 'ZCode.exe'),
               'auto_launch_desktop': False, 'superagent_profiles': {},
               'superagent_default_profile': '', 'superagent_stall_seconds': 60,
               'superagent_read_failure_threshold': 5,
               'desktop_sync_enabled': False, 'desktop_sync_control_file': '',
               'desktop_sync_min_interval_seconds': 1}
        with patch('zcode_bridge.service.config', return_value=cfg):
            self.call('zcode_models', {'workspace': str(self.workspace)})
        params = self.fake.deferred_params[-1]
        self.assertNotIn('model', params)

    def test_native_create_and_send_never_use_engine_rpc(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        self.assertEqual(sid, 'native_sess_1')
        self.assertEqual(created['transport'], 'desktop_native')
        self.assertEqual(self.bridge.sessions[sid]['transport'], 'desktop_native')
        self.assertEqual(native.create_calls[0]['permission'], 'full_access')
        sent = self.call('zcode_send', {
            'session_id': sid, 'content': 'native hello',
            'caller_permission_mode': 'read_only'})
        self.assertTrue(sent['accepted'])
        self.assertEqual(native.send_calls[0]['permission'], 'read_only')
        self.assertEqual(self.bridge.sessions[sid]['caller_permission_mode'], 'read_only')

    def test_native_messages_workflows_and_snapshot_shapes(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        messages = self.call('zcode_messages', {'session_id': sid, 'limit': 5})
        self.assertEqual(messages['messages'][0]['parts'][0]['text'], 'native hello')
        workflows = self.call('zcode_workflows', {'session_id': sid,
                                                  'operation': 'session_runs'})
        self.assertEqual(workflows['runs'], [])
        inspected = self.call('zcode_read_session', {'session_id': sid})
        self.assertEqual(inspected['session']['sessionId'], sid)
        self.assertEqual(inspected['transport'], 'desktop_native')

    def test_native_children_reads_route_through_owned_parent_and_refuse_writes(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        native.children_result = {
            'verifiedParentSessionId': sid, 'writeOwnershipGranted': False,
            'native': {'revision': 1,
                       'childSessionIds': ['child-native-1'],
                       'running': [{'childSessionId': 'child-native-1', 'status': 'running'}],
                       'ended': {'total': 1, 'items': [{'childSessionId': 'child-native-2'}]}}}
        result = self.call('zcode_subagents', {'session_id': sid})
        self.assertEqual(result['childSessionIds'],
                         ['child-native-1', 'child-native-2'])
        for child_id in ('child-native-1', 'child-native-2'):
            self.assertEqual(self.bridge.sessions[child_id]['transport'],
                             'desktop_native')
            self.assertTrue(self.bridge.sessions[child_id]['read_only_child'])
            self.assertEqual(self.bridge.sessions[child_id]['parent_session_id'], sid)
        messages = self.call('zcode_messages', {'session_id': 'child-native-1'})
        self.assertEqual(messages['messages'][0]['parts'][0]['text'], 'child hello')
        call = native.child_message_calls[0]
        self.assertEqual(call['session_id'], sid)
        self.assertEqual(call['child_session_id'], 'child-native-1')
        self.assertIsNone(call['run_id'])
        inspected = self.call('zcode_read_session', {'session_id': 'child-native-2'})
        self.assertEqual(inspected['session']['sessionId'], 'child-native-2')
        self.assertEqual(native.child_snapshot_calls[0]['session_id'], sid)
        # No child write ever reaches a backend.
        send_count = len(native.send_calls)
        for name, args in (
                ('zcode_send', {'session_id': 'child-native-1', 'content': 'no'}),
                ('zcode_stop', {'session_id': 'child-native-1'})):
            with self.assertRaises(RuntimeError) as caught:
                self.call(name, {**args, 'caller_permission_mode': 'full_access'})
            self.assertIn('child_read_only', str(caught.exception))
        self.assertEqual(len(native.send_calls), send_count)
        # A host verification refusal is surfaced; there is no engine fallback.
        native.child_error = DesktopNativeError('child_not_verified_for_parent',
                                                'not a child of this parent')
        with self.assertRaises(RuntimeError) as foreign:
            self.call('zcode_messages', {'session_id': 'child-native-1'})
        self.assertIn('child_not_verified_for_parent', str(foreign.exception))

    def test_native_dwf_actor_read_binds_run_id(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        native.workflow_runs = [{'runId': 'run-405', 'status': 'running'}]
        native.workflow_event_result = {
            'events': [{'sequence': 1, 'type': 'actor-created',
                        'payload': {'actor': {'siteId': 'site-a', 'ordinal': 1},
                                    'name': 'worker'}}],
            'hasMore': False}
        result = self.call('zcode_subagents', {'session_id': sid})
        self.assertEqual(result['workflow_children'][0]['verified'], True)
        actor_id = result['workflow_children'][0]['session_id']
        self.assertTrue(actor_id.startswith('sess_dwf-run-405-'))
        self.assertEqual(self.bridge.sessions[actor_id]['native_run_id'], 'run-405')
        self.call('zcode_messages', {'session_id': actor_id})
        self.assertEqual(native.child_message_calls[0]['run_id'], 'run-405')
        self.assertEqual(native.child_message_calls[0]['child_session_id'], actor_id)

    def test_stopped_runs_are_cancelled_terminal_not_completed(self):
        self.fake.runs = [{'runId': 'run-stopped', 'status': 'stopped',
                           'stopReason': 'user', 'resumable': True}]
        rows, truncated = self.bridge._run_rows('sess_agent_1', strict=True)
        self.assertFalse(truncated)
        self.assertEqual(rows[0]['status'], 'stopped')
        self.assertTrue(rows[0]['settled'])
        baseline = self.bridge._baseline(
            'sess_agent_1', self.fake.snapshot('sess_agent_1'),
            run_rows=rows, run_truncated=False)
        self.assertEqual(baseline['terminal_run_ids'], ['run-stopped'])
        self.assertEqual(baseline['active_run_ids'], [])
        self.assertFalse(self.bridge._active_background(
            self.fake.snapshot('sess_agent_1'), rows))
        self.assertEqual(notifications._run_state(rows[0]), 'cancelled')

    def test_native_workflow_events_real_shape_is_forwarded(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        native.workflow_event_result = {'events': [
            {'seq': 1, 'type': 'node.started', 'payload': {'nodeId': 'n1'}}],
            'hasMore': True}
        result = self.call('zcode_workflows', {
            'session_id': sid, 'operation': 'events', 'run_id': 'run-native-1',
            'after_sequence': 0, 'limit': 50})
        self.assertEqual(result['events'][0]['type'], 'node.started')
        self.assertTrue(result['hasMore'])

    def test_native_pending_uses_snapshot_interactions_not_request_outcome(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        native.snapshots[sid]['projection']['pendingInteractions'] = [{
            'interactionId': 'native-interaction-1', 'kind': 'userInput',
            'payload': {'kind': 'userInput', 'toolName': 'AskUserQuestion',
                        'questions': [{'question': 'LIVE-CHECK-QUESTION：继续？',
                                       'header': 'Check',
                                       'options': [{'label': '继续'}, {'label': '停止'}]}]}}]
        pending = self.call('zcode_pending', {'session_id': sid})
        self.assertEqual([row['id'] for row in pending['requests']],
                         ['native-interaction-1'])
        self.assertEqual(pending['transport'], 'desktop_native')
        # A request-outcome id is not a question and must not appear here.
        absent = self.call('zcode_pending', {'session_id': sid})
        self.assertNotIn('write-request-1', [row['id'] for row in absent['requests']])

    def test_native_delegate_and_answer_use_real_interaction(self):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '做一次原生命令检查', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-1'})
        self.assertTrue(delegated['created'])
        sid = delegated['session_id']
        self.assertEqual(delegated['job']['transport'], 'desktop_native')
        native.snapshots[sid]['projection']['pendingInteractions'] = [{
            'interactionId': 'native-interaction-9', 'kind': 'userInput',
            'payload': {'kind': 'userInput', 'toolName': 'AskUserQuestion',
                        'questions': [{'question': '选择继续吗？', 'header': 'Check',
                                       'options': [{'label': '继续'}, {'label': '停止'}]}]}}]
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegated['agent_id']})
        self.assertEqual(inspected['pending'][0]['request_id'], 'native-interaction-9')
        answered = self.call('zcode_agent_answer', {
            'agent_id': delegated['agent_id'],
            'request_id': 'native-interaction-9', 'answer': '继续',
            'caller_permission_mode': 'full_access'})
        self.assertTrue(answered['answered'])
        self.assertEqual(native.resolve_calls[0]['interaction_id'], 'native-interaction-9')
        self.assertEqual(native.resolve_calls[0]['answer'],
                         {'action': 'accept',
                          'content': {'answers': {'选择继续吗？': '继续'}}})

    def test_native_pending_permission_rows_keep_real_ask_user_question(self):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '做一次原生提问检查', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-q'})
        sid = delegated['session_id']
        question = {
            'question': 'NATIVE-CHECK-QUESTION-405：请选择以下某一项（仅接受真实点选答案）。',
            'header': '原生问答', 'multiSelect': False,
            'options': [{'label': 'CHECK-A', 'description': '选择 CHECK-A。'},
                        {'label': 'CHECK-B', 'description': '选择 CHECK-B。'}]}
        # Real 3.18.2 fields: AskUserQuestion lives in pendingPermissions with
        # its tool input, not as a generic allow/deny permission.
        native.snapshots[sid]['projection']['pendingPermissions'] = [{
            'requestId': 'req-question-1',
            'toolCallId': 'call_a63c9be5689948f2b5871715',
            'toolName': 'AskUserQuestion',
            'input': {'questions': [question]},
            'reason': 'tool_use', 'riskLevel': 'low'}]
        pending = self.call('zcode_pending', {'session_id': sid})
        row = pending['requests'][0]
        self.assertEqual(row['method'], 'interaction/requestUserInput')
        self.assertEqual(row['params']['input']['questions'], [question])
        self.assertEqual(row['params']['requestId'], 'req-question-1')
        self.assertEqual(row['params']['toolCallId'], 'call_a63c9be5689948f2b5871715')
        self.assertEqual(row['params']['reason'], 'tool_use')
        self.assertEqual(row['params']['riskLevel'], 'low')
        self.assertNotIn('options', row['params'])
        inspected = self.call('zcode_agent_inspect', {'agent_id': delegated['agent_id']})
        self.assertEqual(inspected['pending'][0]['method'],
                         'interaction/requestUserInput')
        answered = self.call('zcode_agent_answer', {
            'agent_id': delegated['agent_id'], 'request_id': 'req-question-1',
            'answer': 'CHECK-A', 'caller_permission_mode': 'review_important_actions'})
        self.assertTrue(answered['answered'])
        self.assertEqual(native.resolve_calls[0]['answer'], {
            'action': 'accept',
            'content': {'answers': {question['question']: 'CHECK-A'}}})
        self.assertEqual(native.resolve_calls[0]['permission'],
                         'review_important_actions')

    def native_permission_delegate(self, permission='always_ask', key='native-perm'):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '做一次原生权限检查', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': permission, 'idempotency_key': key})
        sid = delegated['session_id']
        native.snapshots[sid]['projection']['pendingPermissions'] = [{
            'requestId': 'perm-1', 'toolCallId': 'call-perm-1', 'toolName': 'Bash',
            'reason': 'permission', 'riskLevel': 'high',
            'options': [
                {'optionId': 'allow-once', 'label': 'Allow once', 'kind': 'allowOnce'},
                {'optionId': 'allow-always', 'label': 'Always allow', 'kind': 'allowAlways'},
                {'optionId': 'deny-once', 'label': 'Deny', 'kind': 'deny'},
                {'optionId': 'custom-remember', 'label': 'Custom', 'kind': 'custom'},
            ],
            'fullAccessOption': {'optionId': 'fullAccess', 'label': 'Full access',
                                 'kind': 'custom'}}]
        return native, delegated

    def test_native_permission_option_must_belong_to_the_pending_request(self):
        native, delegated = self.native_permission_delegate()
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
                'option_id': 'invented-option',
                'caller_permission_mode': 'always_ask'})
        self.assertIn('option_not_in_request', str(caught.exception))
        self.assertEqual(native.resolve_calls, [])

    def test_native_permission_weak_caller_cannot_escalate_to_full_access(self):
        native, delegated = self.native_permission_delegate()
        for response in ({'optionId': 'fullAccess'},
                         {'optionId': 'fullAccess', 'action': 'accept'}):
            with self.assertRaises(ValueError) as caught:
                self.call('zcode_agent_answer', {
                    'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
                    'response': response, 'user_approved': True,
                    'caller_permission_mode': 'always_ask'})
            self.assertIn('permission_escalation_denied', str(caught.exception))
        self.assertEqual(native.resolve_calls, [])

    def test_native_permission_non_full_access_allow_requires_user_approval(self):
        native, delegated = self.native_permission_delegate()
        for response in ({'optionId': 'allow-once'},
                         {'optionId': 'allow-once', 'action': 'accept'}):
            with self.assertRaises(ValueError) as caught:
                self.call('zcode_agent_answer', {
                    'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
                    'response': response, 'caller_permission_mode': 'always_ask'})
            self.assertIn('user_approved_required', str(caught.exception))
        self.assertEqual(native.resolve_calls, [])

        answered = self.call('zcode_agent_answer', {
            'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
            'option_id': 'allow-once', 'user_approved': True,
            'caller_permission_mode': 'always_ask'})
        self.assertTrue(answered['answered'])
        self.assertEqual(native.resolve_calls[0]['answer'], {'optionId': 'allow-once'})
        self.assertTrue(native.resolve_calls[0]['user_approved'])

    def test_native_permission_read_only_denies_allow_but_still_allows_deny(self):
        native, delegated = self.native_permission_delegate(permission='read_only')
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
                'option_id': 'allow-once', 'user_approved': True,
                'caller_permission_mode': 'read_only'})
        self.assertIn('read_only_answer_not_allowed', str(caught.exception))
        self.assertEqual(native.resolve_calls, [])

        denied = self.call('zcode_agent_answer', {
            'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
            'option_id': 'deny-once', 'caller_permission_mode': 'read_only'})
        self.assertTrue(denied['answered'])
        self.assertEqual(native.resolve_calls[0]['answer'], {'optionId': 'deny-once'})
        self.assertFalse(native.resolve_calls[0]['user_approved'])

    def test_native_permission_rejects_content_and_requires_option(self):
        native, delegated = self.native_permission_delegate()
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
                'response': {'action': 'accept', 'content': {'allow': True}},
                'caller_permission_mode': 'always_ask'})
        self.assertIn('invalid_answer', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
                'response': {'action': 'accept', 'user_approved': True},
                'caller_permission_mode': 'always_ask'})
        self.assertIn('answer_requires_option', str(caught.exception))
        self.assertEqual(native.resolve_calls, [])

    def test_native_permission_custom_option_needs_approval_and_full_caller_allow_is_free(self):
        native, delegated = self.native_permission_delegate()
        with self.assertRaises(ValueError) as caught:
            self.call('zcode_agent_answer', {
                'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
                'option_id': 'custom-remember',
                'caller_permission_mode': 'always_ask'})
        self.assertIn('user_approved_required', str(caught.exception))
        answered = self.call('zcode_agent_answer', {
            'agent_id': delegated['agent_id'], 'request_id': 'perm-1',
            'option_id': 'custom-remember', 'user_approved': True,
            'caller_permission_mode': 'always_ask'})
        self.assertTrue(answered['answered'])

        full_native, full_delegated = self.native_permission_delegate(
            permission='full_access', key='native-perm-full')
        full = self.call('zcode_agent_answer', {
            'agent_id': full_delegated['agent_id'], 'request_id': 'perm-1',
            'option_id': 'allow-once', 'caller_permission_mode': 'full_access'})
        self.assertTrue(full['answered'])
        self.assertEqual(full_native.resolve_calls[0]['answer'], {'optionId': 'allow-once'})

    def test_native_ask_user_question_is_not_treated_as_generic_permission(self):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '做一次原生提问检查', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'always_ask', 'idempotency_key': 'native-ask-2'})
        sid = delegated['session_id']
        question = {'question': '继续吗？', 'header': 'Ask',
                    'options': [{'label': '继续'}, {'label': '停止'}]}
        native.snapshots[sid]['projection']['pendingPermissions'] = [{
            'requestId': 'ask-1', 'toolName': 'AskUserQuestion',
            'input': {'questions': [question]}}]
        answered = self.call('zcode_agent_answer', {
            'agent_id': delegated['agent_id'], 'request_id': 'ask-1',
            'answer': '继续', 'caller_permission_mode': 'always_ask'})
        self.assertTrue(answered['answered'])
        self.assertEqual(native.resolve_calls[0]['answer'],
                         {'action': 'accept', 'content': {'answers': {'继续吗？': '继续'}}})

    def test_native_background_cancel_is_explicitly_unsupported(self):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '做一次原生命令检查', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-2'})
        sid = delegated['session_id']
        native.snapshots[sid]['projection']['backgroundJobs'] = [
            {'taskId': 'native-task-1', 'status': 'running'}]
        result = self.call('zcode_agent_cancel', {
            'agent_id': delegated['agent_id'],
            'caller_permission_mode': 'full_access'})
        self.assertFalse(result['cancelled'])
        self.assertTrue(result['pending'])
        self.assertEqual(result['reason'], 'needs_native_ui')
        self.assertEqual(native.cancel_calls, [])
        native.snapshots[sid]['projection']['backgroundJobs'] = []
        result = self.call('zcode_agent_cancel', {
            'agent_id': delegated['agent_id'],
            'caller_permission_mode': 'full_access'})
        self.assertTrue(result['cancelled'])
        self.assertEqual(len(native.cancel_calls), 1)

    def test_native_first_and_v4_ack_shapes_are_accepted(self):
        native = self.use_native()
        first = self.call('zcode_delegate', {
            'task': '原生首发 ACK', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-ack-1'})
        self.assertEqual(first['send']['state'], 'accepted')
        self.assertTrue(first['send']['accepted'])
        native.send_result = {'status': 'accepted', 'sessionId': 'native_sess_1',
                              'commandId': 'cmd-v4',
                              'result': {'type': 'inputAccepted'}}
        second = self.call('zcode_delegate', {
            'task': '原生 V4 ACK', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-ack-2'})
        self.assertEqual(second['send']['state'], 'accepted')
        self.assertTrue(second['send']['accepted'])

    def test_native_replay_ack_is_uncertain_not_resend(self):
        native = self.use_native()
        native.send_result = {'replay': True, 'automaticRetry': False,
                              'request': {'status': 'confirmed'}}
        first = self.call('zcode_delegate', {
            'task': '原生 replay ACK', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-replay'})
        self.assertEqual(first['send']['state'], 'uncertain')
        self.assertEqual(first['job']['status'], 'send_uncertain')
        self.assertEqual(len(native.send_calls), 1)

    def test_native_unknown_send_is_not_resent(self):
        native = self.use_native()
        native.send_error = DesktopNativeError('unknown', 'transport lost')
        first = self.call('zcode_delegate', {
            'task': '原生未知写', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-3'})
        self.assertEqual(first['send']['state'], 'uncertain')
        self.assertEqual(first['job']['status'], 'send_uncertain')
        second = self.call('zcode_delegate', {
            'task': '原生未知写', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-3'})
        self.assertFalse(second['created'])
        self.assertEqual(len(native.send_calls), 1)

    def test_default_transport_setting_selects_native(self):
        cfg = {
            **storage.defaults(),
            'zcode_executable': str(Path(self.tmp.name) / 'ZCode.exe'),
            'auto_launch_desktop': False,
            'default_model': dict(MODEL), 'default_thought_level': 'high',
            'default_workspace': str(self.workspace),
            'default_transport': 'desktop_native',
            'desktop_native_enabled': True,
            'desktop_native_control_file': 'unused-by-fake',
            'superagent_profiles': {}, 'superagent_default_profile': '',
            'superagent_stall_seconds': 60, 'superagent_read_failure_threshold': 5,
            'desktop_sync_enabled': False, 'desktop_sync_control_file': '',
            'desktop_sync_min_interval_seconds': 1,
        }
        with patch('zcode_bridge.service.config', return_value=cfg):
            native = self.use_native()
            delegated = self.call('zcode_delegate', {
                'task': '默认后端', 'workspace': str(self.workspace),
                'caller_permission_mode': 'full_access', 'idempotency_key': 'native-default'})
        self.assertEqual(delegated['job']['transport'], 'desktop_native')
        self.assertEqual(native.created, 1)

    def test_native_notify_arms_and_maps_the_real_command_id(self):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '原生通知', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-notify',
            'notify': self.authorized_notify()})
        self.assertTrue(delegated['subscription']['armed'])
        subscription_id = delegated['subscription']['subscription_id']
        self.assertTrue(subscription_id)
        self.assertEqual(len(native.send_calls), 1)
        sub = self.bridge.notifications.find_subscription(subscription_id, 'chat-A')
        self.assertEqual(sub['state'], 'armed')
        self.assertEqual(sub['turn']['input_id'], 'native-cmd-1')
        self.assertTrue(sub['turn']['send_accepted'])

    def test_native_contract_does_not_demand_a_missing_report_tool(self):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '原生契约', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-contract'})
        self.assertTrue(delegated['created'])
        text = native.send_calls[0]['text']
        self.assertIn('当前会话没有 zcode_agent_report MCP', text)
        self.assertNotIn('最终结果用 zcode_agent_report', text)
        self.assertIn('workspace: ' + str(self.workspace.resolve()), text)
        self.assertIn('model: p/m', text)
        self.assertIn('caller_permission_mode: full_access', text)

    def test_engine_contract_keeps_the_report_tool_line(self):
        result = self.call('zcode_delegate', self.delegate_args(key='engine-contract'))
        self.assertTrue(result['created'])
        text = self.fake.send_calls[0]['content']
        self.assertIn('最终结果用 zcode_agent_report', text)

    def test_native_notify_send_uses_the_current_caller_permission(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        self.assertEqual(self.bridge.sessions[sid]['caller_permission_mode'],
                         'full_access')
        result = self.call('zcode_send', {
            'session_id': sid, 'content': '降级后的通知发送',
            'caller_permission_mode': 'review_important_actions',
            'notify': self.authorized_notify(), 'idempotency_key': 'native-perm-1'})
        self.assertTrue(result['accepted'])
        self.assertEqual(native.send_calls[0]['permission'],
                         'review_important_actions')
        sub = self.bridge.notifications.find_subscription(result['subscription_id'],
                                                          'chat-A')
        self.assertEqual(sub['consent']['permission_at_arm'],
                         'review_important_actions')
        # A later direct send also uses the current grant, never the stored
        # full_access; cancel the armed subscription first to release the lease.
        self.call('zcode_notify_cancel', {
            'subscription_id': result['subscription_id'],
            'caller_permission_mode': 'review_important_actions'})
        native.snapshots[sid]['projection']['turnCount'] = 1
        native.snapshots[sid]['messages'] = [
            {'info': {'id': 'm-1', 'role': 'user'}, 'parts': []}]
        again = self.call('zcode_send', {
            'session_id': sid, 'content': '第二轮',
            'caller_permission_mode': 'review_important_actions'})
        self.assertTrue(again['accepted'])
        self.assertEqual(native.send_calls[-1]['permission'],
                         'review_important_actions')

    def test_native_subscription_running_final_idle_emits_one_completion(self):
        native = self.use_native()
        delegated = self.call('zcode_delegate', {
            'task': '原生完成通知', 'workspace': str(self.workspace),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access', 'idempotency_key': 'native-observe',
            'notify': self.authorized_notify()})
        sid = delegated['session_id']
        subscription_id = delegated['subscription']['subscription_id']
        snapshot = native.snapshots[sid]
        snapshot['projection']['status'] = 'running'
        snapshot['projection']['turnCount'] = 1
        native.message_rows = [
            {'info': {'id': 'm-user', 'role': 'user', 'time': time.time()},
             'parts': [{'type': 'text', 'text': 'input'}]},
            {'info': {'id': 'm-assistant', 'role': 'assistant', 'time': time.time() + 1},
             'parts': [{'type': 'text', 'text': 'NATIVE_CORE_405'}]},
        ]
        native.event_rows = [
            {'seq': 1, 'type': 'turn.started',
             'payload': {'inputId': 'native-cmd-1', 'messageId': 'm-user'}},
            {'seq': 2, 'type': 'turn.completed',
             'payload': {'inputId': 'native-cmd-1'}},
        ]
        observer = Observer(self.bridge.notifications,
                            ObserverRuntimeAdapter(self.bridge), poll_seconds=1)
        self.bridge.notifications.touch(subscription_id, next_poll_at=0)
        observer.poll_once()
        snapshot['projection']['status'] = 'idle'
        for _ in range(2):
            self.bridge.notifications.touch(subscription_id, next_poll_at=0)
            observer.poll_once()
        completed = [event for event in self.bridge.notifications.all_events('chat-A')
                     if event.get('session_id') == sid
                     and event.get('kind') == 'turn.completed']
        self.assertEqual(len(completed), 1)
        sub = self.bridge.notifications.find_subscription(subscription_id, 'chat-A')
        self.assertEqual(sub['state'], 'completed')

    def test_native_binding_is_per_session_and_persisted(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        self.assertEqual(self.bridge._transport_for(sid), 'desktop_native')
        with self.bridge.lock:
            self.bridge.sessions['sess_legacy'] = {'workspace': str(self.workspace),
                                                   'transport': 'engine'}
        self.bridge._save_sessions()
        saved = storage.read_json(storage.state_dir() / 'sessions.json', {})
        self.assertEqual(saved[sid]['transport'], 'desktop_native')
        self.assertEqual(self.bridge._transport_for('sess_legacy'), 'engine')
        self.assertEqual(native.created, 1)

    def test_native_send_without_current_permission_is_refused(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        with self.assertRaises(DesktopNativeError) as caught:
            self.bridge.rpc('session/send', {'sessionId': sid, 'content': 'x'})
        self.assertEqual(caught.exception.code, 'caller_permission_required')
        self.assertEqual(native.send_calls, [])

    def test_native_attach_report_mcp_is_unsupported(self):
        native = self.use_native()
        with self.assertRaises(RuntimeError) as caught:
            self.call('zcode_create_session', {
                'workspace': str(self.workspace), 'model': dict(MODEL),
                'thought_level': 'high', 'transport': 'desktop_native',
                'attach_report_mcp': True,
                'caller_permission_mode': 'full_access'})
        self.assertIn('unsupported', str(caught.exception))
        self.assertEqual(native.created, 0)

    def test_native_set_permissions_needs_native_ui(self):
        native = self.use_native()
        created = self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'transport': 'desktop_native',
            'caller_permission_mode': 'full_access'})
        sid = created['session']['sessionId']
        with self.assertRaises(RuntimeError) as caught:
            self.call('zcode_set_permissions', {
                'session_id': sid, 'caller_permission_mode': 'read_only'})
        self.assertIn('needs_native_ui', str(caught.exception))

    def test_engine_attach_report_mcp_is_report_only_and_session_local(self):
        self.call('zcode_create_session', {
            'workspace': str(self.workspace), 'model': dict(MODEL),
            'thought_level': 'high', 'caller_permission_mode': 'full_access',
            'attach_report_mcp': True})
        params = self.fake.create_params[-1]
        servers = params.get('mcpServers') or []
        self.assertEqual(len(servers), 1)
        server = servers[0]
        self.assertEqual(server['name'], 'zcode-superagent-report')
        self.assertEqual(server['args'], ['-m', 'zcode_bridge.report_mcp'])
        self.assertEqual(server['isolation'], 'session')
        self.assertEqual(server['env'][0]['name'], 'ZCODE_BRIDGE_STATE')
        self.assertNotIn('zcode_delegate', json.dumps(server))
        self.assertEqual(self.bridge.sessions['sess_agent_1']['report_mcp'], True)

    def test_tool_surface_keeps_old_names_and_adds_high_level_tools(self):
        names = [row['name'] for row in TOOLS]
        self.assertEqual(len(names), 32)
        for old in ('zcode_create_session', 'zcode_send', 'zcode_notify_subscribe',
                    'zcode_wait', 'zcode_settings'):
            self.assertIn(old, names)
        for new in ('zcode_delegate', 'zcode_agent_message', 'zcode_agent_inspect',
                    'zcode_agent_answer', 'zcode_agent_cancel', 'zcode_agent_report'):
            self.assertIn(new, names)
        listed = mcp.handle({'method': 'tools/list'})['tools']
        self.assertEqual(len(listed), 34)
        self.assertIn('zcode_delegate', mcp.INSTRUCTIONS)
        with patch.dict(os.environ, {'CODEX_THREAD_ID': ''}):
            with patch('zcode_bridge.mcp.local_call') as local:
                with self.assertRaises(ValueError) as caught:
                    mcp.handle({'method': 'tools/call', 'params': {
                        'name': 'zcode_delegate', 'arguments': {}, '_meta': {}}})
                self.assertIn('source_unavailable', str(caught.exception))
                local.assert_not_called()


if __name__ == '__main__':
    unittest.main()
