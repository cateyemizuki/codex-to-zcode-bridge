"""Native snapshot questions must reach the notification outbox and host wake."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge.delivery import DeliveryWorker
from zcode_bridge.notifications import NotificationStore, classify_pending
from zcode_bridge.service import Bridge
from test_notify_observer import (Clock, FakeWaker, SID, THREAD, consent,
                                  observe, running_subscription)


class NativePendingNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(1800000000)
        self.store = NotificationStore(Path(self.tmp.name) / 'notifications.json',
                                       clock=self.clock)
        self.sub = running_subscription(
            self.store, consent_row=consent(auto_continue=True))
        self.clock.advance(3)

    def permission(self, **fields):
        return {'requestId': 'perm-native', 'toolCallId': 'call-native',
                'toolName': 'CreateWorkflow',
                'options': [{'optionId': 'allow_once', 'kind': 'allow_once'}],
                **fields}

    def rows(self, item, key='pendingPermissions'):
        return Bridge._native_questions_from_snapshot(
            {'projection': {key: [item]}}, SID)

    def split(self, rows):
        return classify_pending(self.sub, rows, self.clock(), 3600)

    def test_permission_timestamp_reaches_action_event_and_host_wake_once(self):
        requested = self.clock() - 2
        rows = self.rows(self.permission(requestedAt=requested * 1000))
        self.assertEqual(rows[0].get('received_at'), requested)
        verdict, events, _ = observe(
            self.store, self.clock, self.sub, status='running', pending=rows)
        self.assertFalse(verdict.get('terminal'))
        self.assertEqual([event['kind'] for event in events], ['action.required'])
        event = events[0]
        self.assertEqual(event['anchor']['request_id'], 'perm-native')
        self.assertEqual(event['delivery']['state'], 'pending')

        waker = FakeWaker()
        worker = DeliveryWorker(self.store, waker=waker, clock=self.clock)
        self.assertEqual(worker.tick(), 1)
        self.assertEqual(waker.sends[0]['thread_id'], THREAD)
        self.assertIn(event['event_id'], waker.sends[0]['prompt'])
        self.assertNotIn('allow_once', json.dumps(event))
        _, repeated, _ = observe(
            self.store, self.clock, self.sub, status='running', pending=rows)
        self.assertEqual([row['event_id'] for row in repeated], [event['event_id']])
        self.assertEqual(len(self.store.all_events()), 1)
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(len(waker.sends), 1)

    def test_ask_user_question_in_permission_projection_is_fresh(self):
        rows = self.rows({'requestId': 'question-native',
                          'toolName': 'AskUserQuestion',
                          'input': {'questions': [{'question': 'PRIVATE QUESTION'}]},
                          'requestedAt': (self.clock() - 1) * 1000})
        self.assertEqual(self.split(rows)['fresh'], ['question-native'])
        self.assertEqual(rows[0]['method'], 'interaction/requestUserInput')

    def test_pending_interaction_created_time_is_retained(self):
        rows = self.rows({'interactionId': 'interaction-native',
                          'kind': 'userInput',
                          'createdAt': (self.clock() - 1) * 1000,
                          'payload': {'questions': []}}, 'pendingInteractions')
        self.assertEqual(self.split(rows)['fresh'], ['interaction-native'])

    def test_pre_send_request_remains_stale(self):
        rows = self.rows(self.permission(
            requestedAt=(self.sub['created_at'] - 1) * 1000))
        self.assertEqual(self.split(rows), {
            'fresh': [], 'stale': ['perm-native'], 'unattributed': []})

    def test_real_reannouncement_seconds_are_not_discarded(self):
        rows = self.rows(self.permission(
            requestedAt=(self.sub['created_at'] - 1) * 1000,
            last_announced_at=self.clock() - 1))
        self.assertEqual(self.split(rows)['fresh'], ['perm-native'])

    def test_canonical_seconds_keep_their_unit(self):
        rows = self.rows(self.permission(received_at=self.clock() - 1))
        self.assertEqual(self.split(rows)['fresh'], ['perm-native'])

    def test_missing_or_invalid_time_is_not_invented_as_fresh(self):
        for value in (None, True, -1, 'invalid', float('inf'), float('nan')):
            with self.subTest(value=value):
                rows = self.rows(self.permission(requestedAt=value))
                self.assertNotIn('received_at', rows[0])
                self.assertEqual(self.split(rows)['fresh'], [])

    def test_current_sdk_snapshot_is_live_without_fabricating_a_timestamp(self):
        rows = self.rows(self.permission())
        rows[0]['live_native_pending'] = True
        self.assertNotIn('received_at', rows[0])
        self.assertEqual(self.split(rows)['fresh'], ['perm-native'])
        rows[0]['params']['sessionId'] = 'another-session'
        self.assertEqual(self.split(rows)['unattributed'], ['perm-native'])


if __name__ == '__main__':
    unittest.main()
