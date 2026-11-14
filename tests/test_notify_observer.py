"""Observer and delivery-worker tests with a fake engine and fake host waker.

The observer is the daemon-side adapter over engine snapshots; the delivery
worker is the best-effort host wake.  Both are exercised with a mutable fake
clock so no real time, engine or Codex host is involved.
"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import notifications
from zcode_bridge.delivery import DeliveryWorker, wake_prompt
from zcode_bridge.notifications import NotificationStore, Observer, make_event_id

SID = 'sess_test'
THREAD = 'chat-A'


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def consent(**overrides):
    row = {'completion_notify': True, 'auto_continue': False,
           'user_authorized': True, 'permission_at_arm': 'full_access'}
    row.update(overrides)
    return row


def source(thread=THREAD):
    return {'thread_id': thread, 'thread_origin': 'turn_metadata'}


def observation(clock, **overrides):
    row = {
        'now': clock.value, 'engine_ok': True, 'pending': [], 'pending_ttl': 3600,
        'turn_count': 1, 'status': 'idle', 'started': True, 'stable': True,
        'anchor_proof': 'send_result_message', 'final_after_anchor': True,
        'jobs': [], 'runs': [], 'children': [], 'poll_seconds': 1,
    }
    row.update(overrides)
    return row


def running_subscription(store, *, thread=THREAD, anchor='m1', before='m0',
                         engine_pid=111, consent_row=None, state='running',
                         send_result=True, sid=SID, **overrides):
    sub = store.create_subscription(
        sid, source(thread), consent_row or consent(), state=state,
        arm_basis='send_accepted', input_id='inp-1', before_message_id=before,
        turn_anchor=anchor, anchor_proof='send_result_message', before_turn_count=0,
        engine_pid=engine_pid, **overrides)
    sub = store.mark_send_accepted(sub['subscription_id'],
                                   {'messageId': anchor} if send_result else {}) or sub
    if sub.get('state') != state:
        sub = store.touch(sub['subscription_id'], state=state,
                          state_reason='accepted') or sub
    return sub


def observe(store, clock, subscription=None, **overrides):
    subscription = subscription or store.all_subscriptions()[0]
    fresh = store.find_subscription(subscription['subscription_id'])
    sample = observation(clock, **overrides)
    split = notifications.classify_pending(fresh, sample.get('pending') or (),
                                           sample['now'], sample['pending_ttl'])
    sample['fresh_pending_ids'] = split['fresh']
    verdict = notifications.decide(fresh, sample)
    appended = store.apply_decision(fresh['subscription_id'], fresh['revision'],
                                     verdict, sample)
    return verdict, appended, store.find_subscription(fresh['subscription_id'])


def events_of(store, kind=None):
    return [event for event in store.all_events()
            if kind is None or event['kind'] == kind]


class FakeRuntime:
    """One normalized engine observation source; no RPC, no subprocess."""

    def __init__(self, clock, pid=222, stream='stream-1'):
        self.clock = clock
        self.pid = pid
        self.generation = 0
        self.stream = stream
        self.projection = {'status': 'idle', 'turnCount': 1, 'backgroundJobs': []}
        self.run_rows = []
        self.child_rows = []
        self.event_rows = []
        self.message_rows = []
        self.pending_rows = []
        self.honor_after = True
        self.fail = None
        self.calls = []

    def _guard(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError('fake engine unavailable')

    def engine_identity(self):
        self._guard('identity')
        return {'pid': self.pid, 'generation': self.generation}

    def ensure_session(self, sid, deadline=None):
        self._guard('ensure_session')
        return self.snapshot(sid, deadline)

    def snapshot(self, sid, deadline=None):
        self._guard('snapshot')
        return {'session': {'sessionId': sid},
                'projection': copy.deepcopy(self.projection),
                'runtime': {'instanceId': self.stream}, 'settings': {}}

    def jobs(self, snapshot, sid, deadline=None):
        self._guard('jobs')
        return copy.deepcopy(self.projection.get('backgroundJobs') or [])

    def workflows(self, sid, deadline=None):
        self._guard('workflows')
        rows = copy.deepcopy(self.run_rows)
        return rows, rows

    def children(self, sid, deadline=None):
        self._guard('children')
        rows = copy.deepcopy(self.child_rows)
        return rows, [row['id'] for row in rows]

    def session_events(self, sid, after, deadline=None):
        self._guard('session_events')
        rows = copy.deepcopy(self.event_rows)
        if not self.honor_after:
            return {'events': rows}
        return {'events': [row for row in rows if int(row.get('seq') or 0) > int(after or 0)]}

    def messages(self, sid, anchor, deadline=None):
        self._guard('messages')
        return {'messages': copy.deepcopy(self.message_rows)}

    def stream_id(self, sid, deadline=None):
        self._guard('stream_id')
        return self.stream

    def pending(self):
        self._guard('pending')
        return copy.deepcopy(self.pending_rows)


class FakeWaker:
    def __init__(self, probe=None, send=None):
        self.probe_result = {'state': 'supported'} if probe is None else probe
        self.send_result = {'ok': True} if send is None else send
        self.probes = []
        self.sends = []

    def probe(self, timeout_seconds=10, host=None):
        self.probes.append(copy.deepcopy(host))
        return dict(self.probe_result)

    def send(self, thread_id, session_id, event_id, prompt, host=None, host_id=''):
        self.sends.append({'thread_id': thread_id, 'session_id': session_id,
                           'event_id': event_id, 'prompt': prompt,
                           'host': copy.deepcopy(host), 'host_id': host_id})
        result = self.send_result
        if callable(result):
            return result(thread_id, event_id)
        return dict(result)


class ObserverHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.tmp.name) / 'notifications.json'
        self.store = NotificationStore(self.path, clock=self.clock)
        self.runtime = FakeRuntime(self.clock)
        self.observer = Observer(self.store, self.runtime, clock=self.clock,
                                 poll_seconds=1)

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, advance=2):
        self.clock.advance(advance)
        self.observer.poll_once()


class ObserverTruthTests(ObserverHarness):
    def test_accepted_but_not_started_first_idle_is_never_completed(self):
        sub = self.store.create_subscription(
            SID, source(), consent(), state='arming', before_message_id='m0',
            before_turn_count=1, engine_pid=111)
        self.store.mark_send_accepted(sub['subscription_id'], {})
        self.tick(0)
        self.tick(2)
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'armed')
        self.assertEqual(current['state_reason'], 'stale_terminal_at_arm')
        self.assertEqual(events_of(self.store, 'turn.completed'), [])
        self.clock.advance(121)
        self.observer.poll_once()
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'expired')
        self.assertEqual([event['kind'] for event in events_of(self.store)], ['turn.expired'])

    def test_first_idle_requires_two_identical_samples(self):
        sub = running_subscription(self.store)
        self.runtime.message_rows = [{'info': {'id': 'm1', 'role': 'user'}}, {'info': {'id': 'm2', 'role': 'assistant'}}]
        self.tick(0)
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'settling')
        self.assertEqual(events_of(self.store, 'turn.completed'), [])
        self.tick(2)
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'completed')
        self.assertEqual([event['kind'] for event in events_of(self.store)],
                         ['turn.completed'])

    def test_318_ids_and_synthetic_tokens_anchor_the_same_turn(self):
        sub = running_subscription(self.store, anchor='m1', before='m0')
        self.runtime.message_rows = [
            {'info': {'id': 'm1', 'role': 'user'}},
            {'info': {'messageId': 'user-new', 'role': 'user', 'inputId': 'inp-1'},
             'parts': []},
            {'info': {'messageId': 'assistant-new', 'role': 'assistant'}, 'parts': []},
            {'info': {'role': 'user', 'inputId': 'inp-1', 'synthetic': True,
                      'semantics': {'uiVisibility': 'hidden'}}, 'parts': []},
        ]
        page = self.observer._messages(sub, SID, None)
        self.assertEqual(page['anchor_proof'], 'input_id_echo')
        self.assertEqual(page['user_message_id'], 'user-new')
        self.assertEqual(page['final_message_id'], 'assistant-new')
        self.assertTrue(page['final_after_anchor'])

    def test_offline_collection_failure_still_expires_at_deadline(self):
        sub = running_subscription(self.store)
        self.store.touch(sub['subscription_id'], expires_at=self.clock.value - 1)
        self.runtime.fail = 'engine_identity'
        self.observer.observe(self.store.find_subscription(sub['subscription_id']))
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'expired')
        self.assertEqual([event['kind'] for event in events_of(self.store)], ['turn.expired'])

    def test_attach_anchor_missing_from_page_never_completes(self):
        sub = self.store.create_subscription(
            SID, source(), consent(), state='running', arm_basis='attach_running',
            input_id='', user_message_id='u-current', before_message_id='m-before',
            before_turn_count=0, engine_pid=111)
        self.runtime.message_rows = [
            {'info': {'messageId': 'm-old-assistant', 'role': 'assistant',
                      'semantics': {'kind': 'assistant_response'}}, 'parts': []},
        ]
        page = self.observer._messages(sub, SID, None)
        self.assertFalse(page['final_after_anchor'])
        self.assertEqual(page['final_message_id'], '')
        self.tick(0)
        self.tick(2)
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertNotEqual(current['state'], 'completed')
        self.assertEqual(events_of(self.store, 'turn.completed'), [])

    def test_running_background_job_and_workflow_block_completion(self):
        sub = running_subscription(self.store)
        self.runtime.message_rows = [{'info': {'id': 'm1', 'role': 'user'}}, {'info': {'id': 'm2', 'role': 'assistant'}}]
        self.runtime.projection['backgroundJobs'] = [{'jobId': 'j1', 'status': 'running'}]
        self.tick(0)
        self.tick(2)
        self.tick(4)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'settling')
        self.assertEqual(events_of(self.store, 'turn.completed'), [])
        self.runtime.projection['backgroundJobs'] = [{'jobId': 'j1', 'status': 'completed'}]
        self.tick(6)
        self.tick(8)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'completed')

    def test_active_workflow_run_keeps_settling(self):
        sub = running_subscription(self.store)
        self.runtime.message_rows = [{'info': {'id': 'm1', 'role': 'user'}}, {'info': {'id': 'm2', 'role': 'assistant'}}]
        self.runtime.run_rows = [{'run_id': 'r1', 'status': 'running', 'settled': False}]
        self.tick(0)
        self.tick(2)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'settling')
        self.runtime.run_rows = [{'run_id': 'r1', 'status': 'completed', 'settled': True}]
        self.tick(4)
        self.tick(6)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'completed')

    def test_fresh_pending_produces_action_required_then_resolved(self):
        sub = running_subscription(self.store)
        self.runtime.message_rows = [{'info': {'id': 'm1', 'role': 'user'}}, {'info': {'id': 'm2', 'role': 'assistant'}}]
        self.runtime.pending_rows = [{'id': 'req-1', 'sessionId': SID,
                                      'received_at': self.clock.value}]
        self.tick(0)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'started')
        action = events_of(self.store, 'action.required')
        self.assertEqual(len(action), 1)
        self.assertFalse(action[0]['terminal'])
        self.assertEqual(events_of(self.store, 'turn.completed'), [])
        self.runtime.pending_rows = []
        self.tick(2)
        self.tick(4)
        self.tick(6)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'completed')
        self.assertTrue(events_of(self.store, 'action.resolved'))

    def test_stale_pending_while_waiting_stays_unknown(self):
        sub = running_subscription(self.store)
        self.runtime.projection['status'] = 'waiting'
        self.runtime.pending_rows = [{'id': 'req-old', 'sessionId': SID,
                                      'received_at': self.clock.value - 10000}]
        self.tick(0)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'unknown')
        self.assertEqual(events_of(self.store, 'action.pending_stale')[0]
                         ['delivery']['state'], 'inbox_only')
        self.assertEqual(events_of(self.store, 'turn.completed'), [])

    def test_unattributed_pending_does_not_block_idle_completion(self):
        sub = running_subscription(self.store)
        self.runtime.message_rows = [{'info': {'id': 'm1', 'role': 'user'}}, {'info': {'id': 'm2', 'role': 'assistant'}}]
        self.runtime.pending_rows = [{'id': 'req-global', 'received_at': self.clock.value}]
        self.runtime.projection['status'] = 'waiting'
        self.tick(0)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'unknown')
        self.assertEqual(events_of(self.store, 'interaction.unattributed')[0]
                         ['delivery']['state'], 'inbox_only')
        self.runtime.projection['status'] = 'idle'
        self.tick(6)
        self.tick(2)
        self.assertEqual(self.store.find_subscription(sub['subscription_id'])['state'],
                         'completed')

    def test_observer_does_not_call_the_engine_without_subscriptions(self):
        self.observer.poll_once()
        self.assertEqual(self.runtime.calls, [])

    def test_degraded_then_recovered_events(self):
        sub = running_subscription(self.store)
        self.runtime.fail = 'snapshot'
        for _ in range(5):
            self.clock.advance(1)
            self.observer.observe(self.store.find_subscription(sub['subscription_id']))
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertEqual(current['consecutive_errors'], 5)
        self.assertTrue(current['observer_degraded'])
        self.assertEqual(len(events_of(self.store, 'observer.degraded')), 1)
        self.assertEqual(current['state'], 'running')
        self.runtime.fail = None
        self.clock.advance(1)
        self.observer.observe(self.store.find_subscription(sub['subscription_id']))
        self.assertEqual(len(events_of(self.store, 'observer.recovered')), 1)
        self.assertFalse(self.store.find_subscription(sub['subscription_id'])
                         ['observer_degraded'])


class RecoveryTests(ObserverHarness):
    def test_restart_completes_recovered_with_the_persisted_event_id(self):
        sub = running_subscription(self.store, engine_pid=111)
        expected = make_event_id('turn.completed', SID, sub['subscription_id'],
                                 {'message_id': 'm1'})
        reopened = NotificationStore(self.path, clock=self.clock)
        runtime = FakeRuntime(self.clock, pid=222)
        runtime.projection = {'status': 'idle', 'turnCount': 2, 'backgroundJobs': []}
        runtime.message_rows = [{'info': {'id': 'm1', 'role': 'user'}}, {'info': {'id': 'm2', 'role': 'assistant'}}]
        observer = Observer(reopened, runtime, clock=self.clock)
        for advance in (1, 2):
            self.clock.advance(advance)
            observer.poll_once()
        current = reopened.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'completed')
        event = events_of(reopened, 'turn.completed')[0]
        self.assertEqual(event['event_id'], expected)
        self.assertEqual(event['kind'], 'turn.completed')
        self.assertEqual(len(events_of(reopened, 'turn.completed')), 1)

    def test_armed_without_start_after_restart_only_expires(self):
        sub = self.store.create_subscription(
            SID, source(), consent(), state='arming', before_message_id='m0',
            before_turn_count=1, engine_pid=111)
        self.store.mark_send_accepted(sub['subscription_id'], {})
        reopened = NotificationStore(self.path, clock=self.clock)
        runtime = FakeRuntime(self.clock, pid=222)
        runtime.message_rows = []
        observer = Observer(reopened, runtime, clock=self.clock)
        self.clock.advance(1)
        observer.poll_once()
        current = reopened.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'armed')
        self.assertEqual(current['state_reason'], 'stale_terminal_at_arm')
        self.assertEqual(events_of(reopened, 'turn.completed'), [])
        self.clock.advance(121)
        observer.poll_once()
        current = reopened.find_subscription(sub['subscription_id'])
        self.assertEqual(current['state'], 'expired')
        self.assertEqual([event['kind'] for event in events_of(reopened)], ['turn.expired'])

    def test_native_sequence_reset_rereads_from_zero_and_dedups(self):
        sub = running_subscription(self.store)
        self.store.touch(sub['subscription_id'],
                         native={'stream_id': 's1', 'last_seq': 100, 'seen': [],
                                 'reset': False, 'epoch': 's1', 'last_event_key': ''},
                         run_cursors={'old-epoch:run-old': 5})
        self.runtime.stream = 's1'
        self.runtime.honor_after = False
        self.runtime.event_rows = [{'seq': seq, 'type': 'step', 'payload': {'n': seq}}
                                   for seq in range(1, 21)]
        self.tick(0)
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertTrue(current['native']['reset'])
        self.assertEqual(current['native']['last_seq'], 20)
        self.assertEqual(len(current['native']['seen']), 20)
        self.assertEqual(current['run_cursors'], {})
        self.assertEqual(events_of(self.store, 'turn.completed'), [])

    def test_after_seq_empty_is_not_mistaken_for_a_reset(self):
        sub = running_subscription(self.store)
        self.store.touch(sub['subscription_id'],
                         native={'stream_id': 's1', 'last_seq': 100, 'seen': [],
                                 'reset': False, 'epoch': 's1', 'last_event_key': ''})
        self.runtime.stream = 's1'
        self.runtime.event_rows = [{'seq': seq, 'type': 'step', 'payload': {}}
                                   for seq in range(1, 21)]
        self.tick(0)
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertFalse(current['native']['reset'])
        self.assertEqual(current['native']['last_seq'], 100)

    def test_stream_identity_change_forces_a_reread(self):
        sub = running_subscription(self.store)
        self.store.touch(sub['subscription_id'],
                         native={'stream_id': 's1', 'last_seq': 5, 'seen': [],
                                 'reset': False, 'epoch': 's1', 'last_event_key': ''})
        self.runtime.stream = 's2'
        self.runtime.event_rows = [{'seq': seq, 'type': 'step', 'payload': {}}
                                   for seq in range(1, 4)]
        self.tick(0)
        current = self.store.find_subscription(sub['subscription_id'])
        self.assertTrue(current['native']['reset'])
        self.assertEqual(current['native']['stream_id'], 's2')
        self.assertEqual(current['native']['last_seq'], 3)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.tmp.name) / 'notifications.json'
        self.store = NotificationStore(self.path, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def completed_event(self, thread=THREAD, consent_row=None, sid=SID):
        sub = running_subscription(self.store, thread=thread, sid=sid,
                                   consent_row=consent_row)
        _, appended, _ = observe(self.store, self.clock, subscription=sub)
        return appended[0]

    def worker(self, waker=None, **options):
        return DeliveryWorker(self.store, waker=waker or FakeWaker(),
                              clock=self.clock, **options)

    def test_wake_is_sent_once_with_only_session_and_event_ids(self):
        event = self.completed_event()
        waker = FakeWaker()
        worker = self.worker(waker)
        self.assertEqual(worker.tick(), 1)
        self.assertEqual(len(waker.sends), 1)
        sent = waker.sends[0]
        self.assertEqual(sent['thread_id'], THREAD)
        self.assertEqual(sent['prompt'], wake_prompt(SID, event['event_id']))
        self.assertIn(event['event_id'], sent['prompt'])
        self.assertNotIn('SECRET', sent['prompt'])
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'delivered')
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(len(waker.sends), 1)

    def test_delivered_events_are_not_resent_after_restart(self):
        event = self.completed_event()
        self.assertEqual(self.worker(FakeWaker()).tick(), 1)
        reopened = NotificationStore(self.path, clock=self.clock)
        waker = FakeWaker()
        worker = DeliveryWorker(reopened, waker=waker, clock=self.clock)
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(waker.sends, [])
        self.assertEqual(waker.probes, [])
        self.assertEqual(reopened._events()[event['event_id']]['delivery']['state'],
                         'delivered')

    def test_pending_event_keeps_going_after_restart(self):
        event = self.completed_event()
        reopened = NotificationStore(self.path, clock=self.clock)
        waker = FakeWaker()
        self.assertEqual(DeliveryWorker(reopened, waker=waker, clock=self.clock).tick(), 1)
        self.assertEqual(waker.sends[0]['event_id'], event['event_id'])

    def test_failure_backoff_and_max_attempts(self):
        event = self.completed_event()
        waker = FakeWaker(send={'ok': False, 'error_code': 'timeout'})
        worker = self.worker(waker, max_attempts=2)
        self.assertEqual(worker.tick(), 0)
        delivery = self.store._events()[event['event_id']]['delivery']
        self.assertEqual(delivery['state'], 'pending')
        self.assertEqual(delivery['attempts'], 1)
        self.assertGreaterEqual(delivery['next_attempt_at'], self.clock.value + 5)
        self.clock.advance(4)
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(len(waker.sends), 1)
        self.clock.advance(2)
        self.assertEqual(worker.tick(), 0)
        delivery = self.store._events()[event['event_id']]['delivery']
        self.assertEqual(delivery['state'], 'failed')
        self.assertEqual(delivery['attempts'], 2)
        self.clock.advance(1000)
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(len(waker.sends), 2)

    def test_claim_lease_before_host_call_retries_safely(self):
        event = self.completed_event()
        claim = self.store.claim_delivery(event['event_id'], self.clock.value)
        self.assertIsNotNone(claim)
        self.clock.advance(121)
        self.store.expire_claims(self.clock.value)
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'pending')
        waker = FakeWaker()
        self.assertEqual(self.worker(waker).tick(), 1)
        self.assertEqual(len(waker.sends), 1)

    def test_claim_lease_after_host_call_is_uncertain_and_not_resent(self):
        event = self.completed_event()
        claim = self.store.claim_delivery(event['event_id'], self.clock.value)
        self.assertTrue(self.store.mark_delivery_send_attempted(
            event['event_id'], claim['claim_id']))
        self.clock.advance(121)
        self.store.expire_claims(self.clock.value)
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'uncertain')
        waker = FakeWaker()
        self.assertEqual(self.worker(waker).tick(), 0)
        self.assertEqual(waker.sends, [])

    def test_missing_host_tool_keeps_queue_until_probe_confirms_support(self):
        event = self.completed_event()
        waker = FakeWaker(probe={'state': 'unsupported', 'error_code': 'missing_tool'})
        worker = self.worker(waker)
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(waker.sends, [])
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'pending')
        self.assertEqual(self.store.data['host_capability']['state'], 'unsupported')
        self.clock.advance(601)
        waker.probe_result = {'state': 'supported'}
        self.assertEqual(worker.tick(), 1)
        self.assertEqual(len(waker.sends), 1)

    def test_send_unsupported_is_requeued_only_after_supported_probe(self):
        event = self.completed_event()
        waker = FakeWaker(send={'ok': False, 'error_code': 'unsupported',
                                'unsupported': True})
        worker = self.worker(waker)
        self.assertEqual(worker.tick(), 0)
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'unsupported')
        self.clock.advance(601)
        waker.probe_result = {'state': 'supported'}
        waker.send_result = {'ok': True}
        self.assertEqual(worker.tick(), 1)
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'delivered')

    def test_handshake_failure_keeps_pending_with_backoff(self):
        event = self.completed_event()
        waker = FakeWaker(send={'ok': False, 'error_code': 'handshake_failed'})
        worker = self.worker(waker)
        self.assertEqual(worker.tick(), 0)
        delivery = self.store._events()[event['event_id']]['delivery']
        self.assertEqual(delivery['state'], 'pending')
        self.assertEqual(delivery['last_error_code'], 'handshake_failed')
        self.assertEqual(self.store.data['host_capability']['error_code'],
                         'handshake_failed')
        self.assertNotEqual(delivery['state'], 'failed')

    def test_inbox_only_events_are_never_woken(self):
        event = self.completed_event(consent_row={
            'completion_notify': False, 'auto_continue': False,
            'user_authorized': True, 'permission_at_arm': 'full_access'})
        waker = FakeWaker()
        self.assertEqual(self.worker(waker).tick(), 0)
        self.assertEqual(waker.sends, [])
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'inbox_only')

    def test_events_are_routed_to_their_own_source_thread(self):
        event_a = self.completed_event(thread='chat-A')
        event_b = self.completed_event(thread='chat-B', sid='sess_other')
        waker = FakeWaker()
        self.assertEqual(self.worker(waker).tick(), 2)
        routes = {sent['event_id']: sent['thread_id'] for sent in waker.sends}
        self.assertEqual(routes[event_a['event_id']], 'chat-A')
        self.assertEqual(routes[event_b['event_id']], 'chat-B')
        for sent in waker.sends:
            self.assertIn(sent['event_id'], sent['prompt'])

    def test_auto_continue_only_changes_the_final_instruction(self):
        event = self.completed_event()
        waker = FakeWaker()
        worker = self.worker(waker, auto_continue_lookup=lambda row: True)
        self.assertEqual(worker.tick(), 1)
        prompt = waker.sends[0]['prompt']
        self.assertEqual(prompt, wake_prompt(SID, event['event_id'], auto_continue=True))
        self.assertTrue(prompt.endswith('继续原任务的验收与后续工作。'))

    def test_cancelling_a_pending_delivery_prevents_the_wake(self):
        event = self.completed_event()
        self.assertFalse(self.store.cancel_delivery(event['event_id'], 'chat-B'))
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'pending')
        self.assertFalse(self.store.cancel_delivery('evt_missing', 'chat-A'))
        pending_event = self.completed_event(thread='chat-C')
        self.assertTrue(self.store.cancel_delivery(pending_event['event_id'], 'chat-C'))
        self.assertEqual(self.store._events()[pending_event['event_id']]['delivery']['state'],
                         'cancelled')
        waker = FakeWaker()
        self.worker(waker).tick()
        self.assertNotIn(pending_event['event_id'],
                         [sent['event_id'] for sent in waker.sends])

    def test_ack_of_undelivered_event_cancels_only_that_wake(self):
        event = self.completed_event()
        result = self.store.ack('chat-A', event_ids=[event['event_id']])
        self.assertEqual(result['acked'], 1)
        self.assertEqual(result['cancelled'], 1)
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'cancelled')
        waker = FakeWaker()
        self.assertEqual(self.worker(waker).tick(), 0)
        self.assertEqual(waker.sends, [])

    def test_claimed_delivery_cancel_marks_in_flight_uncertain(self):
        event = self.completed_event()
        claim = self.store.claim_delivery(event['event_id'], self.clock.value)
        self.assertTrue(self.store.cancel_delivery(event['event_id'], 'chat-A'))
        self.assertTrue(self.store.delivery_cancel_requested(
            event['event_id'], claim['claim_id']))
        self.store.complete_delivery(event['event_id'], claim['claim_id'], ok=False,
                                     error_code='cancelled', now=self.clock.value)
        delivery = self.store._events()[event['event_id']]['delivery']
        self.assertEqual(delivery['state'], 'uncertain')
        self.assertIsNone(delivery['next_attempt_at'])

    def test_ack_retry_failed_requeues_failed_but_never_inbox_only(self):
        event = self.completed_event()
        claim = self.store.claim_delivery(event['event_id'], self.clock.value)
        self.store.complete_delivery(event['event_id'], claim['claim_id'], ok=False,
                                     error_code='timeout', now=self.clock.value,
                                     max_attempts=1)
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'failed')
        result = self.store.ack('chat-A', retry_failed=True)
        self.assertEqual(result['retried'], 1)
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'pending')
        quiet = self.completed_event(thread='chat-B', consent_row={
            'completion_notify': False, 'auto_continue': False,
            'user_authorized': True, 'permission_at_arm': 'full_access'})
        self.assertEqual(self.store._events()[quiet['event_id']]['delivery']['state'],
                         'inbox_only')
        self.store.ack('chat-B', retry_failed=True)
        self.assertEqual(self.store._events()[quiet['event_id']]['delivery']['state'],
                         'inbox_only')

    def test_event_ttl_expires_a_pending_delivery(self):
        event = self.completed_event()
        self.clock.advance(8 * 86400)
        self.assertEqual(self.store.expire_events(self.clock.value), 1)
        self.assertEqual(self.store._events()[event['event_id']]['delivery']['state'],
                         'expired')
        self.assertEqual(self.store.due_deliveries(self.clock.value), [])


if __name__ == '__main__':
    unittest.main()
