"""Native 3.18.2 workflow lifecycle evidence, with no live engine or host.

The SDK's Jki/INt expose stopped/interrupted as resumable; Mqe makes a live
resumed run running despite historical terminal journal rows.  Workspace-runs
metadata has parentSessionId/stopReason but does not promise resumable.
"""
import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import notifications
from zcode_bridge.service import Bridge
from zcode_bridge.workflows import topology
from test_notify_observer import (Clock, FakeRuntime, NotificationStore, Observer,
                                 events_of, running_subscription)

SID = 'sess_workflow_owner'
RUN = 'dwfrun-fixture'


class WorkflowObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = str(Path(self.temporary.name).resolve())
        # Keep the real normalization/RPC selection methods.  Do not construct
        # a daemon, read the user's state or launch an engine.
        self.bridge = object.__new__(Bridge)
        self.bridge.lock = threading.RLock()
        self.bridge.sessions = {SID: {'workspace': self.workspace, 'transport': 'engine'}}
        self.raw = []
        self.journal = []
        self.metadata = []
        self.metadata_error = None
        self.calls = []
        self.bridge._workflow_runs = lambda *args, **kwargs: copy.deepcopy(self.raw)
        self.bridge.rpc = self.rpc

    def rpc(self, method, params, deadline=None):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'workflows/runs':
            self.assertEqual(params, {
                'workspace': {'workspacePath': self.workspace, 'workspaceKey': self.workspace},
                'scope': 'project', 'limit': 50})
            if self.metadata_error:
                raise self.metadata_error
            return {'runs': copy.deepcopy(self.metadata)}
        if method == 'v4/conversation/workflowRunEvents':
            self.assertEqual(params['sessionId'], SID)
            self.assertEqual(params['runId'], RUN)
            return {'events': copy.deepcopy(self.journal), 'hasMore': False}
        if method == 'session/subagents':
            return {'childSessionIds': []}
        self.fail('unexpected RPC: ' + method)

    def rows(self, status='stopped', **facts):
        self.raw = [{'runId': RUN, 'status': status, **facts}]
        rows, truncated = self.bridge._run_rows(SID, strict=True)
        self.assertFalse(truncated)
        return rows

    def test_interrupted_resumable_is_nonterminal_and_remains_active_at_attach(self):
        rows = self.rows(stopReason='interrupted', resumable=True)
        row = rows[0]
        self.assertEqual(row['status'], 'paused')
        self.assertEqual(row['native_status'], 'stopped')
        self.assertEqual(row['stop_reason'], 'interrupted')
        self.assertIs(row['resumable'], True)
        self.assertFalse(row['settled'])
        self.assertEqual(notifications.aggregate_runs(rows, {}), 'pending')
        baseline = self.bridge._baseline(SID, {'projection': {}},
                                         run_rows=rows, run_truncated=False)
        self.assertEqual(baseline['active_run_ids'], [RUN])
        self.assertEqual(baseline['terminal_run_ids'], [])

    def test_user_model_and_superseded_stop_remain_cancelled(self):
        for reason in ('user', 'model', 'superseded'):
            with self.subTest(reason=reason):
                rows = self.rows(stopReason=reason, resumable=reason != 'superseded')
                self.assertEqual(rows[0]['status'], 'stopped')
                self.assertTrue(rows[0]['settled'])
                self.assertEqual(notifications.aggregate_runs(rows, {}), 'cancelled')

    def test_native_errored_is_failed_even_when_foreground_is_running(self):
        rows = self.rows('errored', resumable=False)
        self.assertEqual(rows[0]['status'], 'failed')
        self.assertEqual(rows[0]['native_status'], 'errored')
        self.assertTrue(rows[0]['settled'])
        self.assertEqual(notifications.aggregate_runs(rows, {}), 'failed')
        verdict = notifications.decide(
            {'state': 'running', 'arm_basis': 'attach_active_background', 'baseline': {}},
            {'now': 1, 'engine_ok': True, 'started': True, 'status': 'running',
             'runs': rows, 'jobs': [], 'children': [], 'pending': []})
        self.assertEqual(verdict['state'], 'failed')

    def test_current_running_is_not_overridden_by_historical_terminal(self):
        for old in ('stopped', 'errored', 'completed'):
            with self.subTest(old=old):
                self.journal = [{'sequence': 1, 'type': 'run-settled',
                                 'payload': {'status': old, 'stopReason': 'interrupted',
                                             'resumable': True}}]
                row = self.rows('running', resumable=False)[0]
                self.assertEqual(row['status'], 'running')
                self.assertFalse(row['settled'])
                self.assertEqual(row['cursor'], 1)

    def test_real_start_events_clear_only_historical_terminal_facts(self):
        for kind in ('run-started', 'run-launched'):
            events = [{'type': 'run-settled', 'payload': {
                'status': 'stopped', 'stopReason': 'interrupted', 'resumable': True}}]
            old = topology(RUN, events)
            self.assertEqual(old['stop_reason'], 'interrupted')
            self.assertTrue(old['resumable'])
            current = topology(RUN, events + [{'type': kind, 'payload': {}}])
            self.assertEqual(current['status'], 'running')
            self.assertNotIn('stop_reason', current)
            self.assertNotIn('resumable', current)

    def test_missing_reason_uses_only_exact_engine_run_and_parent(self):
        self.metadata = [
            {'runId': 'other-run', 'parentSessionId': SID, 'status': 'stopped', 'stopReason': 'user'},
            {'runId': RUN, 'parentSessionId': 'other-parent', 'status': 'stopped', 'stopReason': 'user'},
            {'runId': RUN, 'parentSessionId': SID, 'cwd': self.workspace,
             'status': 'stopped', 'stopReason': 'interrupted', 'args': {'private': 'not copied'}}]
        row = self.rows(resumable=True)[0]
        self.assertEqual(row['status'], 'paused')
        self.assertEqual(row['stop_reason_source'], 'workspace_runs')
        self.assertNotIn('args', row)
        self.assertNotIn('private', str(row))
        self.assertEqual(sum(method == 'workflows/runs' for method, _ in self.calls), 1)

    def test_foreign_missing_duplicate_or_wrong_workspace_metadata_is_unknown(self):
        for metadata in ([],
                         [{'runId': RUN, 'status': 'stopped', 'stopReason': 'user'}],
                         [{'runId': RUN, 'parentSessionId': 'foreign', 'status': 'stopped', 'stopReason': 'user'}],
                         [{'runId': RUN, 'parentSessionId': SID, 'cwd': self.workspace + '-other',
                           'status': 'stopped', 'stopReason': 'user'}],
                         [{'runId': RUN, 'parentSessionId': SID, 'status': 'stopped',
                           'stopReason': 'user'}] * 2):
            with self.subTest(metadata=metadata):
                self.metadata = metadata
                row = self.rows(resumable=True)[0]
                self.assertEqual(row['status'], 'unknown')
                self.assertFalse(row['settled'])
                self.assertIn('read_error', row)
                self.assertEqual(notifications.aggregate_runs([row], {}), 'pending')

    def test_conflicting_same_run_metadata_does_not_fabricate_cancel(self):
        self.metadata = [{'runId': RUN, 'parentSessionId': SID, 'status': 'running'}]
        self.journal = [{'sequence': 1, 'type': 'run-settled',
                         'payload': {'status': 'stopped', 'stopReason': 'user', 'resumable': True}}]
        row = self.rows(resumable=True)[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertEqual(row['metadata_status'], 'running')
        self.assertEqual(row['read_error'], 'workflow_status_conflict')
        self.assertFalse(row['settled'])

    def test_native_reason_missing_reads_native_journal_without_engine_fallback(self):
        self.bridge.sessions[SID]['transport'] = 'desktop_native'
        self.journal = [{'sequence': 1, 'type': 'run-settled', 'payload': {
            'status': 'stopped', 'stopReason': 'interrupted', 'resumable': True}}]
        row = self.rows(resumable=True)[0]
        self.assertEqual(row['status'], 'paused')
        self.assertEqual(row['stop_reason_source'], 'journal')
        self.assertNotIn('workflows/runs', [method for method, _ in self.calls])
        self.journal = []
        row = self.rows(resumable=True)[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertIn('read_error', row)

    def test_no_bound_workspace_or_metadata_error_stays_unknown(self):
        self.bridge.sessions[SID].pop('workspace')
        row = self.rows(resumable=True)[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertNotIn('workflows/runs', [method for method, _ in self.calls])
        self.bridge.sessions[SID]['workspace'] = self.workspace
        self.metadata_error = OSError('synthetic-secret-must-not-escape')
        row = self.rows(resumable=True)[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertEqual(row['read_error'], 'workflow_stop_metadata_OSError')
        self.assertNotIn('synthetic-secret', str(row))

    def test_truncated_journal_cannot_supply_an_old_terminal_stop(self):
        self.bridge.sessions[SID]['transport'] = 'desktop_native'
        self.bridge.observe_run_topology = lambda *args, **kwargs: {
            'status': 'stopped', 'stop_reason': 'user', 'resumable': True,
            'after_sequence': 2000, 'more': True}
        row = self.rows(resumable=True)[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertFalse(row['settled'])
        self.assertEqual(row['read_error'], 'workflow_stop_journal_truncated')
        self.assertNotIn('stop_reason', row)
        self.assertEqual(notifications.aggregate_runs([row], {}), 'pending')

    def test_resumable_flag_alone_and_nonresumable_interruption_never_complete(self):
        for facts in ({'resumable': True}, {'stopReason': 'interrupted', 'resumable': False},
                      {'stopReason': 'provider', 'resumable': True}):
            with self.subTest(facts=facts):
                row = self.rows(**facts)[0]
                self.assertEqual(row['status'], 'unknown')
                self.assertFalse(row['settled'])
                self.assertIn('read_error', row)

    def observer(self):
        clock = Clock()
        store = NotificationStore(Path(self.temporary.name) / 'notifications.json', clock=clock)
        sub = running_subscription(store, sid=SID)
        runtime = FakeRuntime(clock, pid=111)
        runtime.workflows = lambda sid, deadline=None: self.bridge._run_rows(sid, deadline, strict=True)
        runtime.message_rows = [{'info': {'id': 'm1', 'role': 'user'}},
                                {'info': {'id': 'm2', 'role': 'assistant'}}]
        observer = Observer(store, runtime, clock=clock, poll_seconds=1)
        return clock, store, sub, observer

    def test_interrupted_same_id_resume_then_complete_real_observer_flow(self):
        clock, store, sub, observer = self.observer()
        self.raw = [{'runId': RUN, 'status': 'stopped', 'stopReason': 'interrupted', 'resumable': True}]
        self.journal = [{'sequence': 1, 'type': 'run-settled', 'payload': {
            'status': 'stopped', 'stopReason': 'interrupted', 'resumable': True}}]
        for at in (1000, 1002):
            clock.value = at
            observer.tick()
        self.assertNotIn(store.find_subscription(sub['subscription_id'])['state'],
                         notifications.TERMINAL_STATES)
        self.raw = [{'runId': RUN, 'status': 'running', 'resumable': False}]
        self.journal.append({'sequence': 2, 'type': 'run-started', 'payload': {}})
        clock.value = 1004
        observer.tick()
        self.assertEqual(events_of(store, 'turn.cancelled'), [])
        self.assertEqual(events_of(store, 'turn.completed'), [])
        self.raw = [{'runId': RUN, 'status': 'completed', 'resumable': False}]
        for at in (1006, 1008):
            clock.value = at
            observer.tick()
        self.assertEqual(store.find_subscription(sub['subscription_id'])['state'], 'completed')
        self.assertEqual(len(events_of(store, 'turn.completed')), 1)

    def test_genuine_cancel_history_is_never_resurrected(self):
        clock, store, sub, observer = self.observer()
        self.raw = [{'runId': RUN, 'status': 'stopped', 'stopReason': 'user', 'resumable': True}]
        observer.tick()
        self.assertEqual(store.find_subscription(sub['subscription_id'])['state'], 'cancelled')
        self.raw = [{'runId': RUN, 'status': 'running', 'resumable': False}]
        clock.value += 2
        observer.tick()
        self.assertEqual(store.find_subscription(sub['subscription_id'])['state'], 'cancelled')
        self.assertEqual(len(events_of(store, 'turn.cancelled')), 1)
        self.assertEqual(events_of(store, 'turn.completed'), [])


if __name__ == '__main__':
    unittest.main()
