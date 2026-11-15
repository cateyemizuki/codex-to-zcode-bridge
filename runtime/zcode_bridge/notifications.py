"""Persistent completion subscriptions, stable events and terminal truth.

The store is the only writer of ``notifications.json``.  It never stores message
bodies, prompts, workspace paths, model ids, provider ids, credentials, thread
ids or pipe paths.  Thread binding lives on the subscription and is used only to
filter what a caller may read; events deliberately carry no thread id.

``evaluate`` is a pure function over a persisted subscription and one normalized
observation.  That keeps the terminal truth table testable with fake clocks and
fake engine snapshots.  ``Observer`` is the daemon-side adapter that collects
those observations through an injected runtime (``Bridge`` in production).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .storage import read_json, write_json
from .identity import (is_assistant_response, is_visible_message,
                       message_activity_digest, message_identity)

SCHEMA_VERSION = 1

# A subscription is terminal once no further wake may be produced for it.
TERMINAL_STATES = frozenset({
    'completed', 'failed', 'cancelled', 'interrupted', 'expired',
    'superseded', 'send_not_accepted', 'send_failed',
})
# Every non-terminal state holds the session lease.  ``send_uncertain`` is
# deliberately non-terminal: the send may have executed, so it keeps the lease
# until the caller reconciles or the subscription expires.
ACTIVE_STATES = frozenset({
    'arming', 'armed', 'started', 'running', 'settling', 'paused',
    'unknown', 'send_uncertain',
})
STARTED_STATES = frozenset({'started', 'running', 'settling'})

KINDS = frozenset({
    'turn.completed', 'turn.failed', 'turn.cancelled', 'turn.paused',
    'turn.interrupted', 'turn.expired', 'turn.send_uncertain',
    'turn.superseded', 'action.required', 'action.resolved',
    'action.pending_stale', 'interaction.unattributed', 'workflow.settled',
    'child.failed', 'observer.degraded', 'observer.recovered',
    'hint.progress', 'hint.action_required', 'hint.needs_verification',
    'store.incompatible',
})

REASONS = frozenset({
    'accepted', 'running', 'idle_stable', 'error_status', 'failed_job',
    'run_failed', 'child_failed', 'stopped', 'epoch_changed', 'not_started',
    'ttl', 'pending_stale', 'unattributed_pending', 'child_running',
    'stale_terminal_at_arm', 'send_not_accepted', 'send_failed',
    'send_uncertain', 'job_cancelled', 'run_cancelled', 'child_cancelled',
    'native_seq_reset', 'store_incompatible', 'anchor_required',
    'anchor_missing', 'completed_recovered',
    'waiting_unknown', 'engine_unavailable', 'std_waiting',
    'progress', 'verification_requested', 'needs_input', 'blocked',
})

DELIVERY_PENDING = 'pending'
DELIVERY_CLAIMED = 'claimed'
DELIVERY_DELIVERED = 'delivered'
DELIVERY_FAILED = 'failed'
DELIVERY_EXPIRED = 'expired'
DELIVERY_CANCELLED = 'cancelled'
DELIVERY_UNCERTAIN = 'uncertain'
DELIVERY_UNSUPPORTED = 'unsupported'
DELIVERY_INBOX_ONLY = 'inbox_only'


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def make_event_id(kind, session_id, subscription_id, anchor):
    """Stable terminal identity: never includes clock, pid, generation or reason."""
    payload = 'v1|' + str(kind) + '|' + str(session_id) + '|' + str(subscription_id) + '|' + _canonical(anchor)
    return 'evt_' + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]


def make_subscription_id():
    return 'sub_' + uuid.uuid4().hex[:24]


def make_input_id():
    return uuid.uuid4().hex


def subscription_anchor(subscription):
    turn = subscription.get('turn') or {}
    return (turn.get('turn_anchor') or turn.get('input_id')
            or turn.get('send_result_message_id') or turn.get('user_message_id') or '')


def has_anchor_proof(subscription):
    turn = subscription.get('turn') or {}
    return bool(turn.get('anchor_proof') or turn.get('send_result_message_id')
                or turn.get('user_message_id'))


def sanitize_host(host):
    """Local host-launch context only; never content, prompt or model data."""
    if not isinstance(host, dict):
        return {}
    result = {}
    for key in ('pipe', 'runtime_cli', 'node', 'host_id'):
        value = host.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    if host.get('updated_at'):
        try:
            result['updated_at'] = float(host['updated_at'])
        except (TypeError, ValueError):
            pass
    return result


def _terminal_verdict(state, reason, event_kind=None, status=None, notices=None):
    return {'state': state, 'reason': reason, 'terminal': True,
            'event_kind': event_kind, 'event_status': status or state,
            'notices': notices or []}


def _verdict(state, reason, notices=None):
    return {'state': state, 'reason': reason, 'terminal': False,
            'event_kind': None, 'event_status': None, 'notices': notices or []}


def _item_id(item):
    for key in ('jobId', 'job_id', 'taskId', 'task_id', 'id', 'runId', 'run_id',
                'sessionId', 'session_id'):
        value = item.get(key)
        if value:
            return str(value)
    return ''


def _job_state(item):
    status = str(item.get('status') or item.get('state') or '').lower()
    if status in ('completed', 'complete', 'succeeded', 'success', 'done', 'successful'):
        return 'completed'
    if status in ('failed', 'error'):
        return 'failed'
    if status in ('cancelled', 'canceled', 'stopped'):
        return 'cancelled'
    return status


def _run_state(item):
    status = str(item.get('status') or item.get('state') or '').lower()
    if item.get('settled') is False:
        return 'running'
    if status in ('completed', 'complete', 'succeeded', 'success', 'settled'):
        return 'completed'
    if status in ('failed', 'error'):
        return 'failed'
    if status in ('cancelled', 'canceled', 'stopped'):
        return 'cancelled'
    if status in ('queued', 'scheduled', 'pending', 'running', 'waiting', 'active'):
        return status
    if item.get('settled') is True:
        return 'completed'
    return 'unknown'


def _child_state(item):
    status = str(item.get('projection_status') or item.get('projectionStatus')
                 or item.get('status') or '').lower()
    if status in ('completed', 'complete', 'idle', 'succeeded', 'success'):
        return 'completed'
    if status in ('cancelled', 'canceled', 'stopped'):
        return 'cancelled'
    if status in ('error', 'failed'):
        return 'failed'
    if status in ('waiting', 'paused', 'running', 'unverified', 'queued', 'pending'):
        return 'running'
    return 'unknown'


def _aggregate(items, baseline_all, baseline_active, state_of):
    """Return ok/failed/cancelled/pending for the current turn only."""
    relevant = []
    present = set()
    baseline_all = set(baseline_all or ())
    baseline_active = set(baseline_active or ())
    for item in items or ():
        ident = _item_id(item)
        if not ident:
            continue
        present.add(ident)
        if ident in baseline_all and ident not in baseline_active:
            continue
        relevant.append(state_of(item))
    if 'failed' in relevant:
        return 'failed'
    if 'cancelled' in relevant:
        return 'cancelled'
    # An item that was active at arm time but is absent now is not proof of
    # completion: the projection may be stale, truncated or an RPC may have
    # failed.  Without an explicit terminal observation it stays pending.
    if baseline_active - present:
        return 'pending'
    if relevant and any(state not in ('completed',) for state in relevant):
        return 'pending'
    return 'ok'


def aggregate_jobs(jobs, baseline):
    return _aggregate(jobs, baseline.get('job_ids'), baseline.get('active_job_ids'), _job_state)


def aggregate_runs(runs, baseline, truncated=False):
    if truncated or baseline.get('truncated'):
        return 'pending'
    return _aggregate(runs, baseline.get('run_ids'), baseline.get('active_run_ids'), _run_state)


def aggregate_children(children, baseline):
    return _aggregate(children, baseline.get('child_ids'), baseline.get('active_child_ids'), _child_state)


def classify_pending(subscription, pending, now, ttl=3600):
    """Split pending interactions into fresh/stale attributable and unattributed.

    ``last_announced_at`` is used as well as ``received_at`` so a live question
    that is re-announced after the send is not mistaken for a stale leftover.
    """
    sent_at = float((subscription.get('turn') or {}).get('sent_at') or subscription.get('created_at') or 0)
    own = {subscription.get('session_id')}
    own.update(subscription.get('known_child_ids') or ())
    fresh, stale, unattributed = [], [], []
    for item in pending or ():
        ident = str(item.get('id') or item.get('requestId') or item.get('request_id') or '')
        params = item.get('params') if isinstance(item.get('params'), dict) else {}
        session = item.get('sessionId') or item.get('session_id') or params.get('sessionId')
        if not session or session not in own:
            unattributed.append(ident)
            continue
        announced = max(float(item.get('received_at') or 0), float(item.get('last_announced_at') or 0))
        if announced and announced >= sent_at and now - announced <= ttl:
            fresh.append(ident)
        else:
            stale.append(ident)
    return {'fresh': fresh, 'stale': stale, 'unattributed': unattributed}


def decide(subscription, evidence):
    """Pure terminal decision.  No I/O, no clock reads, no mutation."""
    state = subscription.get('state')
    if state in TERMINAL_STATES:
        return _terminal_verdict(state, subscription.get('state_reason') or 'terminal')
    now = float(evidence.get('now') or 0)
    expires_at = float(subscription.get('expires_at') or 0)
    if expires_at and now >= expires_at:
        return _terminal_verdict('expired', 'ttl', 'turn.expired', 'expired')

    turn = subscription.get('turn') or {}
    arm_basis = subscription.get('arm_basis')
    armed = ((arm_basis == 'send_accepted'
              and (bool(turn.get('send_accepted')) or bool(evidence.get('accepted_recovered'))))
             or arm_basis in ('attach_running', 'attach_active_background'))
    if not evidence.get('engine_ok'):
        start_deadline = float(subscription.get('start_deadline') or 0)
        persisted_started = (arm_basis.startswith('attach')
                             or state in ('started', 'running', 'settling')
                             or bool(turn.get('started_evidence')))
        if not armed:
            if start_deadline and now >= start_deadline:
                return _terminal_verdict('expired', 'not_started', 'turn.expired', 'expired')
            return _verdict('unknown', 'engine_unavailable')
        if (not persisted_started and start_deadline
                and now >= start_deadline):
            return _terminal_verdict('expired', 'not_started', 'turn.expired', 'expired')
        return _verdict('unknown', 'engine_unavailable')
    if not armed:
        start_deadline = float(subscription.get('start_deadline') or 0)
        if start_deadline and now >= start_deadline:
            return _terminal_verdict('expired', 'not_started', 'turn.expired', 'expired')
        if not evidence.get('engine_ok'):
            return _verdict('unknown', 'engine_unavailable')
        if state == 'send_uncertain':
            return _verdict('send_uncertain', 'send_uncertain')
        if turn.get('send_attempted'):
            return _verdict('send_uncertain', 'send_uncertain')
        return _verdict('arming', 'not_started')

    if not evidence.get('engine_ok'):
        return _verdict('unknown', 'engine_unavailable')

    pending_split = classify_pending(subscription, evidence.get('pending') or (), now,
                                     int(evidence.get('pending_ttl') or 3600))
    before_count = int(turn.get('before_turn_count') or 0)
    if evidence.get('turn_count') is not None and int(evidence.get('turn_count') or 0) < before_count:
        return _verdict('unknown', 'epoch_changed')
    if pending_split['fresh']:
        return _verdict('started' if evidence.get('started') else 'armed', 'accepted',
                        [_notice('action.required', 'action_required',
                                 {'request_id': '|'.join(sorted(pending_split['fresh']))}, 'fresh_pending',
                                 status='action_required')])

    started = bool(evidence.get('started')) or arm_basis.startswith('attach')
    start_deadline = float(subscription.get('start_deadline') or 0)
    status = evidence.get('status')
    if not started:
        if start_deadline and now >= start_deadline:
            return _terminal_verdict('expired', 'not_started', 'turn.expired', 'expired')
        if status in ('completed', 'error', 'paused', 'idle', 'waiting'):
            return _verdict('armed', 'stale_terminal_at_arm')
        return _verdict('armed', 'accepted')

    jobs = aggregate_jobs(evidence.get('jobs') or (), subscription.get('baseline') or {})
    runs = aggregate_runs(evidence.get('runs') or (), subscription.get('baseline') or {},
                          truncated=bool(evidence.get('runs_truncated')))
    children = aggregate_children(evidence.get('children') or (), subscription.get('baseline') or {})
    notices = []
    if evidence.get('new_settled_runs'):
        for run_id, run_status in evidence['new_settled_runs']:
            notices.append({'kind': 'workflow.settled', 'reason': run_status,
                            'anchor': {'run_id': str(run_id), 'run_status': str(run_status)},
                            'status': str(run_status) if run_status in (
                                'completed', 'failed', 'cancelled') else 'info',
                            'delivery': 'inbox_only'})
    if evidence.get('new_failed_children'):
        for child_id in evidence['new_failed_children']:
            notices.append({'kind': 'child.failed', 'reason': 'child_failed',
                            'anchor': {'request_id': str(child_id)},
                            'status': 'failed', 'delivery': 'quiet'})

    if jobs == 'failed':
        return _terminal_verdict('failed', 'failed_job', 'turn.failed', 'failed', notices)
    if runs == 'failed':
        return _terminal_verdict('failed', 'run_failed', 'turn.failed', 'failed', notices)
    if children == 'failed':
        return _terminal_verdict('failed', 'child_failed', 'turn.failed', 'failed', notices)
    if jobs == 'cancelled':
        return _terminal_verdict('cancelled', 'job_cancelled', 'turn.cancelled', 'cancelled', notices)
    if runs == 'cancelled':
        return _terminal_verdict('cancelled', 'run_cancelled', 'turn.cancelled', 'cancelled', notices)
    if children == 'cancelled':
        return _terminal_verdict('cancelled', 'child_cancelled', 'turn.cancelled', 'cancelled', notices)

    background_attach = arm_basis == 'attach_active_background'
    stable = bool(evidence.get('stable'))
    anchor_ok = has_anchor_proof(subscription) or bool(evidence.get('anchor_proof'))
    final_after_anchor = bool(evidence.get('final_after_anchor'))

    if status == 'running':
        return _verdict('running', 'running', notices)
    if status == 'waiting':
        if pending_split['stale']:
            notices.append(_notice('action.pending_stale', 'pending_stale',
                                   {'request_id': '|'.join(sorted(pending_split['stale']))}, 'stale_pending',
                                   status='unknown', delivery='inbox_only'))
            return _verdict('unknown', 'pending_stale', notices)
        if pending_split['unattributed']:
            notices.append(_notice('interaction.unattributed', 'unattributed_pending',
                                   {'request_id': '|'.join(sorted(pending_split['unattributed']))}, 'unattributed_pending',
                                   status='unknown', delivery='inbox_only'))
            return _verdict('unknown', 'unattributed_pending', notices)
        return _verdict('unknown', 'waiting_unknown', notices)
    if status == 'paused':
        return {**_verdict('paused', 'stopped', notices),
                'event_kind': 'turn.paused', 'event_status': 'paused'}
    if status == 'error':
        if stable:
            return _terminal_verdict('failed', 'error_status', 'turn.failed', 'failed', notices)
        return _verdict('settling', 'error_status', notices)

    if jobs == 'pending' or runs == 'pending' or children == 'pending':
        return _verdict('settling', 'child_running', notices)
    if pending_split['stale']:
        notices.append(_notice('action.pending_stale', 'pending_stale',
                               {'request_id': '|'.join(sorted(pending_split['stale']))}, 'stale_pending',
                               status='unknown', delivery='inbox_only'))
    if pending_split['unattributed']:
        notices.append(_notice('interaction.unattributed', 'unattributed_pending',
                               {'request_id': '|'.join(sorted(pending_split['unattributed']))}, 'unattributed_pending',
                               status='unknown', delivery='inbox_only'))

    if status in ('completed', 'idle'):
        if not stable:
            return _verdict('settling', 'running', notices)
        if not background_attach and not (anchor_ok and final_after_anchor):
            return _verdict('unknown', 'anchor_required', notices)
        recovered = bool(evidence.get('recovered')
                         or (subscription.get('recovery') or {}).get('recovered'))
        if recovered:
            return _terminal_verdict('completed', 'completed_recovered', 'turn.completed', 'completed', notices)
        return _terminal_verdict('completed', 'idle_stable', 'turn.completed', 'completed', notices)
    return _verdict('unknown', 'waiting_unknown', notices)


def _notice(kind, reason, anchor, tag, status='info', delivery='pending'):
    return {'kind': kind, 'reason': reason, 'anchor': dict(anchor, tag=tag),
            'status': status, 'delivery': delivery}


def _obs_digest(status, turn_count, last_message_id, active_digest, message_activity=''):
    return hashlib.sha256(_canonical([status, turn_count, last_message_id, active_digest,
                                      message_activity]).encode('utf-8')).hexdigest()


class NotificationStore:
    """Thread-safe, atomic JSON store; no background thread is started here."""

    def __init__(self, path=None, clock=time.time, event_limit=500, subscription_limit=100,
                 event_ttl=7 * 86400, subscription_ttl=7 * 86400):
        self.path = Path(path) if path else None
        self.clock = clock
        self.event_limit = event_limit
        self.subscription_limit = subscription_limit
        self.event_ttl = event_ttl
        self.subscription_ttl = subscription_ttl
        self.lock = threading.RLock()
        self.incompatible = False
        self.incompatible_file = ''
        self.data = self._load()

    # ------------------------------------------------------------------ storage
    def _empty(self):
        return {
            'schema_version': SCHEMA_VERSION,
            'next_event_seq': 1,
            'subscriptions': {},
            'events': {},
            'pruned_events': [],
            'host_capability': {'state': 'not_probed', 'checked_at': None, 'error_code': None,
                                'detail': 'host capability has not been probed'},
        }

    def _load(self):
        if self.path is None or not self.path.exists():
            return self._empty()
        raw = read_json(self.path, None)
        if not isinstance(raw, dict) or raw.get('schema_version') != SCHEMA_VERSION:
            self._quarantine()
            self.incompatible = True
            return self._empty()
        data = {**self._empty(), **raw}
        if (not isinstance(data.get('subscriptions'), dict)
                or not isinstance(data.get('events'), dict)
                or not isinstance(data.get('pruned_events'), list)):
            self._quarantine()
            self.incompatible = True
            return self._empty()
        data['subscriptions'] = data.get('subscriptions') or {}
        data['events'] = data.get('events') or {}
        data['pruned_events'] = data.get('pruned_events') or []
        if (not all(isinstance(row, dict) and row.get('subscription_id') == key
                    for key, row in data['subscriptions'].items())
                or not all(isinstance(row, dict) and row.get('event_id') == key
                           and isinstance(row.get('delivery'), dict)
                           for key, row in data['events'].items())):
            self._quarantine()
            self.incompatible = True
            return self._empty()
        if not isinstance(data.get('next_event_seq'), int) or data['next_event_seq'] < 1:
            data['next_event_seq'] = 1
        max_seq = max((int(row.get('seq') or 0) for row in data['events'].values()), default=0)
        data['next_event_seq'] = max(data['next_event_seq'], max_seq + 1)
        return data

    def _quarantine(self):
        if self.path is None:
            return
        target = self.path.with_name(self.path.name.replace('.json', '') + '.incompatible.' + uuid.uuid4().hex + '.json')
        try:
            os.replace(self.path, target)
            self.incompatible_file = target.name
        except OSError:
            # Never overwrite a store we could not isolate.
            self.incompatible_file = ''
            self.path = None

    def _save(self):
        if self.path is not None:
            write_json(self.path, self.data)

    def _subscriptions(self):
        return self.data['subscriptions']

    def _events(self):
        return self.data['events']

    def verify(self, subscription, evidence):
        """Pure terminal decision bound to this store's documented truth table."""
        return decide(subscription, evidence)

    # ------------------------------------------------------------------ helpers
    def find_idempotency(self, session_id, thread_id, key_hash):
        if not key_hash:
            return None
        with self.lock:
            for sub in self._subscriptions().values():
                if (sub.get('session_id') == session_id
                        and (sub.get('source') or {}).get('thread_id') == thread_id
                        and sub.get('idempotency_key_hash') == key_hash):
                    return copy.deepcopy(sub)
        return None

    def find_subscription(self, subscription_id, thread_id=None):
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None:
                return None
            if thread_id is not None and (sub.get('source') or {}).get('thread_id') != thread_id:
                return None
            return copy.deepcopy(sub)

    def subscriptions_for_thread(self, thread_id, session_id=None):
        with self.lock:
            rows = [copy.deepcopy(sub) for sub in self._subscriptions().values()
                    if (sub.get('source') or {}).get('thread_id') == thread_id
                    and (session_id is None or sub.get('session_id') == session_id)]
        return sorted(rows, key=lambda row: row.get('created_at') or 0, reverse=True)

    def all_subscriptions(self):
        with self.lock:
            return [copy.deepcopy(sub) for sub in self._subscriptions().values()]

    def all_events(self, thread_id=None):
        with self.lock:
            rows = []
            for event in self._events().values():
                sub = self._subscriptions().get(event.get('subscription_id'))
                if thread_id is not None and (sub is None or
                                              (sub.get('source') or {}).get('thread_id') != thread_id):
                    continue
                rows.append(copy.deepcopy(event))
        return sorted(rows, key=lambda row: row.get('seq') or 0)

    def active_for_session(self, session_id):
        with self.lock:
            rows = [copy.deepcopy(sub) for sub in self._subscriptions().values()
                    if sub.get('session_id') == session_id and sub.get('state') in ACTIVE_STATES]
        return sorted(rows, key=lambda row: row.get('created_at') or 0, reverse=True)

    def session_owner(self, session_id):
        rows = self.active_for_session(session_id)
        return rows[0] if rows else None

    def active_subscription_for(self, session_id, thread_id):
        with self.lock:
            for sub in self._subscriptions().values():
                if (sub.get('session_id') == session_id
                        and (sub.get('source') or {}).get('thread_id') == thread_id
                        and sub.get('state') in ACTIVE_STATES):
                    return copy.deepcopy(sub)
        return None

    def latest_subscription_for(self, session_id, thread_id):
        """Most recent subscription, including terminal truth, for zcode_wait."""
        with self.lock:
            rows = [copy.deepcopy(sub) for sub in self._subscriptions().values()
                    if sub.get('session_id') == session_id
                    and (sub.get('source') or {}).get('thread_id') == thread_id]
        rows.sort(key=lambda row: (row.get('created_at') or 0,
                                   row.get('updated_at') or 0), reverse=True)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ create
    def create_subscription(self, session_id, source, consent, arm_basis='send_accepted',
                            input_id=None, user_message_id='', before_message_id='',
                            turn_anchor=None, anchor_proof='', before_turn_count=0, baseline=None,
                            idempotency_key_hash='',
                            state='arming', started_evidence='', known_child_ids=None,
                            engine_pid=None, generation=0, expires_at=None,
                            start_deadline=None, host=None):
        now = self.clock()
        sub_id = make_subscription_id()
        input_id = input_id or ''
        anchor = turn_anchor or input_id or user_message_id or before_message_id or ''
        if not input_id and arm_basis == 'send_accepted':
            input_id = make_input_id()
            anchor = anchor or input_id
        sub = {
            'subscription_id': sub_id,
            'schema': SCHEMA_VERSION,
            'session_id': session_id,
            'created_at': now,
            'updated_at': now,
            'revision': 1,
            'source': {
                'thread_id': str(source.get('thread_id') or ''),
                'thread_origin': str(source.get('thread_origin') or ''),
                'request_id': str(source.get('request_id') or ''),
                'client_name': str(source.get('client_name') or ''),
                'client_version': str(source.get('client_version') or ''),
                'host_id': str(source.get('host_id') or ''),
            },
            'consent': {
                'completion_notify': bool(consent.get('completion_notify', True)),
                'auto_continue': bool(consent.get('auto_continue', False)),
                'user_authorized': consent.get('user_authorized') is True,
                'asserted_at': now,
                'basis': 'caller_asserted_current_chat',
                'permission_at_arm': str(consent.get('permission_at_arm') or ''),
            },
            'arm_basis': arm_basis,
            'idempotency_key_hash': idempotency_key_hash,
            'host': sanitize_host(host),
            'turn': {
                'input_id': input_id,
                'send_attempted': False,
                'send_accepted': False,
                'send_result_message_id': '',
                'user_message_id': user_message_id or '',
                'turn_anchor': anchor,
                'anchor_proof': (anchor_proof or ('user_message' if user_message_id else
                                 ('attach_message' if before_message_id
                                  and str(arm_basis).startswith('attach') else ''))),
                'before_turn_count': int(before_turn_count or 0),
                'before_message_id': before_message_id or '',
                'sent_at': now,
                'engine_pid': engine_pid,
                'generation': int(generation or 0),
                'started_at': now if arm_basis.startswith('attach') else None,
                'started_evidence': started_evidence or (arm_basis if arm_basis.startswith('attach') else ''),
                'retry_count': 0,
                'attempts': 0,
            },
            'baseline': copy.deepcopy(baseline or {}),
            'native': {'epoch': '', 'stream_id': '', 'last_seq': 0, 'reset': False,
                       'seen': [], 'last_event_key': ''},
            'state': state,
            'state_reason': 'accepted' if arm_basis.startswith('attach') else 'not_started',
            'last_native_seq': 0,
            'last_status': '',
            'last_turn_count': 0,
            'last_message_id': '',
            'last_message_activity': '',
            'last_child_activity': '',
            'last_sample_digest': '',
            'stable_samples': 0,
            'attempts': 0,
            'consecutive_errors': 0,
            'next_poll_at': now,
            'start_deadline': float(start_deadline) if start_deadline else now + 120,
            'expires_at': float(expires_at) if expires_at else now + 6 * 3600,
            'expiry_epoch': 1,
            'terminal_reason': None,
            'terminal_event_id': None,
            'run_cursors': {},
            'known_child_ids': list(known_child_ids or ()),
            'pending_action_ids': [],
            'recovery': {'persisted_s': bool(state in STARTED_STATES),
                         'persisted_anchor': anchor, 'crashed_at': None},
            'observer_degraded': False,
        }
        with self.lock:
            self._subscriptions()[sub_id] = sub
            self._prune_locked(now)
            self._save()
            return copy.deepcopy(sub)

    def begin_arming(self, session_id, source, consent, *, arm_basis='send_accepted',
                     input_id='', user_message_id='', before_message_id='',
                     before_turn_count=0, baseline=None, idempotency_key_hash='',
                     state='arming', started_evidence='', known_child_ids=None,
                     engine_pid=None, generation=0, expires_at=None, start_deadline=None,
                     host=None):
        """Atomically re-check the idempotency key/session lease and create arming."""
        with self.lock:
            existing = self.find_idempotency(session_id, source.get('thread_id'),
                                             idempotency_key_hash)
            if existing is not None:
                return 'existing', existing
            owner = self.session_owner(session_id)
            if owner is not None:
                return 'busy', owner
            sub = self.create_subscription(
                session_id, source, consent, arm_basis=arm_basis, input_id=input_id,
                user_message_id=user_message_id, before_message_id=before_message_id,
                before_turn_count=before_turn_count, baseline=baseline,
                idempotency_key_hash=idempotency_key_hash, state=state,
                started_evidence=started_evidence, known_child_ids=known_child_ids,
                engine_pid=engine_pid, generation=generation,
                expires_at=expires_at, start_deadline=start_deadline, host=host)
            return 'created', sub

    def begin_attach(self, session_id, source, consent, *, requested='', arm_basis='attach_running',
                     input_id='', user_message_id='', before_message_id='', turn_anchor=None,
                     anchor_proof='', before_turn_count=0, baseline=None,
                     known_child_ids=None, engine_pid=None, generation=0,
                     expires_at=None, start_deadline=None, host=None):
        """Atomically enforce one owner and one same-thread turn before attach."""
        with self.lock:
            for sub in self._subscriptions().values():
                if (sub.get('session_id') != session_id
                        or (sub.get('source') or {}).get('thread_id') != source.get('thread_id')
                        or sub.get('state') not in ACTIVE_STATES):
                    continue
                current = subscription_anchor(sub)
                turn = sub.get('turn') or {}
                if (not requested or requested == current
                        or requested in (turn.get('input_id'), turn.get('user_message_id'))):
                    return 'existing', copy.deepcopy(sub)
                return 'busy', copy.deepcopy(sub)
            owner = self.session_owner(session_id)
            if owner is not None:
                return 'busy_foreign', owner
            sub = self.create_subscription(
                session_id, source, consent, arm_basis=arm_basis, input_id=input_id,
                user_message_id=user_message_id, before_message_id=before_message_id,
                turn_anchor=turn_anchor, anchor_proof=anchor_proof,
                before_turn_count=before_turn_count, baseline=baseline,
                state='started', started_evidence=arm_basis,
                known_child_ids=known_child_ids, engine_pid=engine_pid,
                generation=generation, expires_at=expires_at,
                start_deadline=start_deadline, host=host)
            return 'created', sub

    def touch(self, subscription_id, **changes):
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None:
                return None
            sub.update(changes)
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._save()
            return copy.deepcopy(sub)

    def update_host(self, subscription_id, host):
        """Refresh a live subscription's local host-launch context in place."""
        clean = sanitize_host(host)
        if not clean:
            return None
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None or sub.get('state') in TERMINAL_STATES:
                return copy.deepcopy(sub) if sub else None
            sub['host'] = {**(sub.get('host') or {}), **clean}
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._save()
            return copy.deepcopy(sub)

    def claim_send(self, subscription_id, retry=False):
        """Atomically mark that session/send is about to execute."""
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None:
                return None
            if sub.get('state') in TERMINAL_STATES:
                return None
            turn = sub['turn']
            if turn.get('send_attempted') and not retry:
                return None
            if retry:
                turn['retry_count'] = int(turn.get('retry_count') or 0) + 1
            turn['send_attempted'] = True
            turn['attempts'] = int(turn.get('attempts') or 0) + 1
            sub['state'] = 'arming'
            sub['state_reason'] = 'accepted' if retry else sub.get('state_reason') or 'armed'
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._save()
            return copy.deepcopy(sub)

    def mark_send_accepted(self, subscription_id, result):
        result = result if isinstance(result, dict) else {}
        message_id = str(result.get('messageId') or result.get('userMessageId')
                         or (result.get('message') or {}).get('id') or '')
        user_message_id = str(result.get('userMessageId') or
                              (result.get('userMessage') or {}).get('id') or '')
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None:
                return None
            if sub.get('state') in TERMINAL_STATES:
                return copy.deepcopy(sub)
            turn = sub['turn']
            turn['send_accepted'] = True
            if message_id:
                turn['send_result_message_id'] = message_id
                turn['anchor_proof'] = turn.get('anchor_proof') or 'send_result_message'
            if user_message_id:
                turn['user_message_id'] = user_message_id
                turn['anchor_proof'] = 'user_message'
            sub['state'] = 'armed'
            sub['state_reason'] = ('accepted' if (turn.get('anchor_proof')
                                                  or turn.get('send_result_message_id')
                                                  or turn.get('user_message_id'))
                                   else 'anchor_missing')
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._save()
            return copy.deepcopy(sub)

    def set_send_input_id(self, subscription_id, input_id):
        """Replace the provisional send id with the native host's actual id.

        The host generates its own ``commandId`` and uses it as the first-input
        ``inputId``; arming before dispatch cannot know it.  The Observer only
        anchors a turn when this id matches ``turn.started``/``turn.completed``.
        """
        value = str(input_id or '')
        if not value:
            return None
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None or sub.get('state') in TERMINAL_STATES:
                return copy.deepcopy(sub) if sub is not None else None
            turn = sub['turn']
            turn['input_id'] = value
            if not turn.get('turn_anchor'):
                turn['turn_anchor'] = value
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._save()
            return copy.deepcopy(sub)

    def mark_send_not_accepted(self, subscription_id, result=None):
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None:
                return None
            if sub.get('state') in TERMINAL_STATES:
                return copy.deepcopy(sub)
            sub['state'] = 'send_not_accepted'
            sub['state_reason'] = 'send_not_accepted'
            sub['terminal_reason'] = 'send_not_accepted'
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._cancel_deliveries_locked(subscription_id)
            self._save()
            return copy.deepcopy(sub)

    def mark_send_uncertain(self, subscription_id, error_code='send_uncertain'):
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None:
                return None
            if sub.get('state') in TERMINAL_STATES:
                return copy.deepcopy(sub), None
            sub['state'] = 'send_uncertain'
            sub['state_reason'] = 'send_uncertain'
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            event = self._append_event_locked(
                sub, 'turn.send_uncertain', 'send_uncertain',
                {'request_id': (sub.get('turn') or {}).get('input_id')
                 or subscription_anchor(sub)}, status='unknown', terminal=False,
                delivery='inbox_only', now=self.clock())
            self._save()
            return copy.deepcopy(sub), copy.deepcopy(event)

    def mark_send_failed(self, subscription_id, error_code='send_failed'):
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None:
                return None
            if sub.get('state') in TERMINAL_STATES:
                return copy.deepcopy(sub)
            sub['state'] = 'send_failed'
            sub['state_reason'] = error_code
            sub['terminal_reason'] = error_code
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._cancel_deliveries_locked(subscription_id)
            self._save()
            return copy.deepcopy(sub)

    # ------------------------------------------------------------------ cancel
    def cancel(self, subscription_id, thread_id):
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None or (sub.get('source') or {}).get('thread_id') != thread_id:
                return None
            if sub.get('state') in TERMINAL_STATES:
                # A terminal subscription may still own an undelivered wake
                # (completed/failed/expired event).  Cancel that delivery even
                # though the subscription state itself is already terminal.
                self._cancel_deliveries_locked(subscription_id)
                self._save()
                return copy.deepcopy(sub)
            sub['state'] = 'cancelled'
            sub['state_reason'] = 'stopped'
            sub['terminal_reason'] = 'stopped'
            sub['updated_at'] = self.clock()
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._cancel_deliveries_locked(subscription_id)
            self._save()
            return copy.deepcopy(sub)

    def cancel_session(self, session_id, thread_id):
        with self.lock:
            count = 0
            for sub in self._subscriptions().values():
                if (sub.get('session_id') == session_id
                        and (sub.get('source') or {}).get('thread_id') == thread_id):
                    if sub.get('state') in TERMINAL_STATES:
                        count += self._cancel_deliveries_locked(sub['subscription_id']) or 0
                        continue
                    sub['state'] = 'cancelled'
                    sub['state_reason'] = 'stopped'
                    sub['terminal_reason'] = 'stopped'
                    sub['updated_at'] = self.clock()
                    sub['revision'] = int(sub.get('revision') or 0) + 1
                    self._cancel_deliveries_locked(sub['subscription_id'])
                    count += 1
            if count:
                self._save()
            return count

    def interrupt_session(self, session_id, thread_id):
        """Emit turn.interrupted (inbox only) and cancel this thread's wakes."""
        now = self.clock()
        with self.lock:
            count = 0
            for sub in self._subscriptions().values():
                if (sub.get('session_id') != session_id
                        or (sub.get('source') or {}).get('thread_id') != thread_id
                        or sub.get('state') in TERMINAL_STATES):
                    continue
                anchor = subscription_anchor(sub) or sub['subscription_id']
                event = self._append_event_locked(sub, 'turn.interrupted', 'stopped',
                                                  {'message_id': anchor}, status='interrupted', terminal=True,
                                                  delivery='inbox_only', now=now)
                if event:
                    sub['terminal_event_id'] = event['event_id']
                sub['state'] = 'interrupted'
                sub['state_reason'] = 'stopped'
                sub['terminal_reason'] = 'stopped'
                sub['updated_at'] = now
                sub['revision'] = int(sub.get('revision') or 0) + 1
                self._cancel_deliveries_locked(sub['subscription_id'])
                count += 1
            if count:
                self._save()
            return count

    def supersede(self, session_id, new_subscription_id):
        with self.lock:
            for sub in self._subscriptions().values():
                if (sub.get('session_id') == session_id
                        and sub['subscription_id'] != new_subscription_id
                        and sub.get('state') not in TERMINAL_STATES):
                    sub['state'] = 'superseded'
                    sub['state_reason'] = 'stopped'
                    sub['terminal_reason'] = 'stopped'
                    sub['updated_at'] = self.clock()
                    sub['revision'] = int(sub.get('revision') or 0) + 1
                    self._cancel_deliveries_locked(sub['subscription_id'])
            self._save()

    # ------------------------------------------------------------------ hints
    def emit_hint(self, session_id, kind, hint_id, reason_code, subscription_id=None):
        now = self.clock()
        rows = []
        with self.lock:
            for sub in self._subscriptions().values():
                if sub.get('session_id') != session_id or sub.get('state') in TERMINAL_STATES:
                    continue
                if subscription_id and sub['subscription_id'] != subscription_id:
                    continue
                event = self._append_event_locked(
                    sub, 'hint.' + kind, reason_code, {'request_id': str(hint_id)},
                    status='info', terminal=False, delivery='pending', now=now)
                # Ask the observer to verify this hint on its next loop.
                sub['next_poll_at'] = now
                if event:
                    rows.append(copy.deepcopy(event))
            if rows:
                self._save()
        return rows

    # ------------------------------------------------------------------ events
    def apply_decision(self, subscription_id, revision, verdict, evidence):
        """Persist one observation and any event the verdict asks for."""
        now = float(evidence.get('now') or self.clock())
        appended = []
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None or int(sub.get('revision') or 0) != int(revision):
                return []
            if sub.get('state') in TERMINAL_STATES:
                return []
            self._update_observation_locked(sub, evidence)
            if evidence.get('started') and not (sub.get('turn') or {}).get('started_evidence'):
                sub['turn']['started_evidence'] = str(evidence.get('started_evidence') or 'running_observed')
            if evidence.get('accepted_recovered') and not (sub.get('turn') or {}).get('send_accepted'):
                sub['turn']['send_accepted'] = True
                sub['turn']['anchor_proof'] = evidence.get('anchor_proof') or 'input_id_echo'
                sub['state_reason'] = 'accepted'
            if evidence.get('anchor_proof'):
                sub['turn']['anchor_proof'] = evidence['anchor_proof']
            if evidence.get('user_message_id'):
                sub['turn']['user_message_id'] = evidence['user_message_id']
            if evidence.get('final_message_id'):
                sub['last_message_id'] = evidence['final_message_id']
            if evidence.get('message_activity'):
                sub['last_message_activity'] = str(evidence['message_activity'])
            if evidence.get('child_activity'):
                sub['last_child_activity'] = str(evidence['child_activity'])
            if evidence.get('known_child_ids'):
                sub['known_child_ids'] = sorted(set(sub.get('known_child_ids') or []) | set(evidence['known_child_ids']))[:200]
            native = evidence.get('native') if isinstance(evidence.get('native'), dict) else None
            if native:
                if native.get('reset'):
                    sub['run_cursors'] = {}
                sub['native'].update(native)
            if evidence.get('run_cursors'):
                sub['run_cursors'].update(evidence['run_cursors'])
            if evidence.get('recovered'):
                sub.setdefault('recovery', {})['recovered'] = True
            if evidence.get('engine_pid') is not None:
                sub['turn']['engine_pid'] = evidence['engine_pid']
            if evidence.get('generation') is not None:
                sub['turn']['generation'] = evidence['generation']

            for notice in verdict.get('notices') or ():
                event = self._append_notice_locked(sub, notice, now)
                if event:
                    appended.append(event)

            fresh_ids = {str(value) for value in evidence.get('fresh_pending_ids') or ()}
            resolved_ids = {str(value) for value in sub.get('pending_action_ids') or ()} - fresh_ids
            for request_id in sorted(resolved_ids):
                event = self._append_event_locked(
                    sub, 'action.resolved', 'pending_stale',
                    {'request_id': request_id}, status='info',
                    terminal=False, delivery='inbox_only', now=now)
                if event:
                    appended.append(event)
            sub['pending_action_ids'] = sorted(fresh_ids)

            terminal = bool(verdict.get('terminal'))
            kind = verdict.get('event_kind')
            state = verdict.get('state')
            sub['state'] = state
            sub['state_reason'] = verdict.get('reason') or sub.get('state_reason')
            sub['attempts'] = 0
            sub['consecutive_errors'] = 0
            if sub.get('observer_degraded'):
                event = self._append_event_locked(
                    sub, 'observer.recovered', 'engine_unavailable',
                    {'request_id': sub['subscription_id']}, status='info',
                    terminal=False, delivery='inbox_only', now=now)
                if event:
                    appended.append(event)
            sub['observer_degraded'] = False
            if terminal and kind:
                anchor = self._anchor_for(sub, verdict)
                event = self._append_event_locked(
                    sub, kind, verdict.get('reason') or state, anchor,
                    status=verdict.get('event_status') or state, terminal=True,
                    delivery='pending', now=now)
                if event:
                    appended.append(event)
                sub['terminal_reason'] = verdict.get('reason') or state
                sub['terminal_event_id'] = event['event_id'] if event else None
            elif kind and not terminal:
                anchor = self._anchor_for(sub, verdict)
                event = self._append_event_locked(
                    sub, kind, verdict.get('reason') or state, anchor,
                    status=verdict.get('event_status') or state, terminal=False,
                    delivery='pending', now=now)
                if event:
                    appended.append(event)
            sub['updated_at'] = now
            sub['revision'] = int(sub.get('revision') or 0) + 1
            sub['poll_seconds'] = max(1, int(evidence.get('poll_seconds') or 1))
            sub['next_poll_at'] = now + self._poll_delay(sub, state, now)
            self._prune_locked(now)
            self._save()
            return appended

    def _poll_delay(self, sub, state, now):
        base = max(1, int((sub.get('poll_seconds') or 1)))
        turn = sub.get('turn') or {}
        age = max(0.0, now - float(turn.get('sent_at') or sub.get('created_at') or now))
        if state in ('running', 'settling'):
            return 1
        if state == 'unknown' and sub.get('last_status') == 'waiting':
            return 5
        if state in ('armed', 'started'):
            if age < 30:
                return base
            if age < 120:
                return 3
            if age < 600:
                return 10
            return 20
        if state == 'paused':
            return 10
        return base

    def _update_observation_locked(self, sub, evidence):
        if evidence.get('status') is not None:
            sub['last_status'] = str(evidence.get('status') or '')
        if evidence.get('turn_count') is not None:
            sub['last_turn_count'] = int(evidence.get('turn_count') or 0)
        if evidence.get('last_message_id'):
            sub['last_message_id'] = str(evidence['last_message_id'])
        if evidence.get('sample_digest'):
            sub['last_sample_digest'] = str(evidence['sample_digest'])
        if evidence.get('stable_samples') is not None:
            sub['stable_samples'] = int(evidence.get('stable_samples') or 0)
        if evidence.get('native_seq') is not None:
            sub['last_native_seq'] = int(evidence.get('native_seq') or 0)
            sub['native']['last_seq'] = int(evidence.get('native_seq') or 0)

    def _anchor_for(self, sub, verdict):
        kind = verdict.get('event_kind') or ''
        turn = sub.get('turn') or {}
        if kind == 'turn.expired':
            return {'request_id': sub['subscription_id']}
        if kind == 'turn.interrupted':
            return {'message_id': subscription_anchor(sub)}
        if kind == 'turn.send_uncertain':
            return {'request_id': turn.get('input_id') or subscription_anchor(sub)}
        if kind == 'turn.superseded':
            return {'request_id': sub['subscription_id']}
        if kind == 'action.required' or kind == 'action.pending_stale':
            return {'request_id': subscription_anchor(sub)}
        return {'message_id': subscription_anchor(sub)}

    def _append_notice_locked(self, sub, notice, now):
        delivery = notice.get('delivery') or 'pending'
        if delivery == 'quiet':
            delivery = 'inbox_only' if not ((sub.get('consent') or {}).get('completion_notify', True)) else 'pending'
        return self._append_event_locked(
            sub, notice.get('kind'), notice.get('reason') or 'info', notice.get('anchor') or {},
            status=notice.get('status') or 'info', terminal=False,
            delivery=delivery, now=now)

    def _append_event_locked(self, sub, kind, reason, anchor, status, terminal, delivery, now):
        if kind not in KINDS:
            # Unknown event kinds are a programming error, not user data.
            return None
        event_id = make_event_id(kind, sub.get('session_id'), sub['subscription_id'], anchor)
        if event_id in self._events():
            return copy.deepcopy(self._events()[event_id])
        inbox_only = (delivery == 'inbox_only'
                      or not ((sub.get('consent') or {}).get('completion_notify', True)))
        if terminal and not inbox_only and not bool((sub.get('consent') or {}).get('user_authorized')):
            inbox_only = True
        seq = int(self.data.get('next_event_seq') or 1)
        self.data['next_event_seq'] = seq + 1
        event = {
            'event_id': event_id,
            'schema': SCHEMA_VERSION,
            'seq': seq,
            'kind': kind,
            'session_id': sub.get('session_id'),
            'subscription_id': sub['subscription_id'],
            'created_at': now,
            'expires_at': now + self.event_ttl,
            'terminal': bool(terminal),
            'status': status,
            'reason_code': reason if reason in REASONS else 'info',
            'anchor': anchor,
            'read_hint': self._read_hint(sub, kind),
            'delivery': {
                'state': DELIVERY_INBOX_ONLY if inbox_only else DELIVERY_PENDING,
                'attempts': 0,
                'claim_id': None,
                'claim_expires_at': None,
                'last_attempt_at': None,
                'next_attempt_at': None if inbox_only else now,
                'last_error_code': None,
                'delivered_at': None,
                'acked_at': None,
                'cancel_requested': False,
                'cancelled_after_delivery': False,
                'send_attempted': False,
            },
        }
        self._events()[event_id] = event
        self._prune_locked(now)
        return copy.deepcopy(event)

    def _read_hint(self, sub, kind):
        if kind in ('action.required', 'action.pending_stale', 'action.resolved',
                    'interaction.unattributed'):
            return {'tools': ['zcode_pending'], 'session_id': sub.get('session_id')}
        if kind in ('workflow.settled', 'child.failed'):
            return {'tools': ['zcode_workflows'], 'session_id': sub.get('session_id')}
        return {'tools': ['zcode_read_session', 'zcode_messages'], 'session_id': sub.get('session_id')}

    def _prune_locked(self, now):
        events = self._events()
        if len(events) > self.event_limit:
            order = sorted(events.values(), key=lambda row: row.get('seq') or 0)
            removable = [row for row in order
                         if row['delivery']['state'] in (DELIVERY_DELIVERED, DELIVERY_CANCELLED,
                                                         DELIVERY_EXPIRED, DELIVERY_INBOX_ONLY)
                         or row['delivery'].get('acked_at')]
            for row in (removable or order):
                if len(events) <= self.event_limit:
                    break
                events.pop(row['event_id'], None)
                self._remember_pruned(row, now)
        subscriptions = self._subscriptions()
        if len(subscriptions) > self.subscription_limit:
            order = sorted(subscriptions.values(),
                           key=lambda row: row.get('updated_at') or row.get('created_at') or 0)
            terminal = [row for row in order if row.get('state') in TERMINAL_STATES]
            for row in terminal:
                if len(subscriptions) <= self.subscription_limit:
                    break
                subscriptions.pop(row['subscription_id'], None)
            # Active subscriptions are the session lease and their events'
            # only thread binding.  Never drop them silently just to satisfy
            # the limit; the daemon may temporarily exceed it.
        # Expire old events and subscriptions without changing next_event_seq.
        if now:
            for event_id, row in list(events.items()):
                if row.get('expires_at') and now > row['expires_at']:
                    row['delivery']['state'] = DELIVERY_EXPIRED
            for sub_id, row in list(subscriptions.items()):
                if (row.get('state') in TERMINAL_STATES
                        and now - float(row.get('updated_at') or 0) > self.subscription_ttl):
                    subscriptions.pop(sub_id, None)

    def _remember_pruned(self, row, now):
        pruned = self.data.setdefault('pruned_events', [])
        pruned.append({'event_id': row['event_id'], 'seq': row.get('seq'), 'pruned_at': now})
        del pruned[:-200]

    # ------------------------------------------------------------------ reading
    def _allowed_event(self, thread_id, event_id):
        event = self._events().get(event_id)
        if not event:
            return None, False
        sub = self._subscriptions().get(event.get('subscription_id'))
        if sub is None or (sub.get('source') or {}).get('thread_id') != thread_id:
            return None, False
        return event, False

    def _pruned(self, event_id):
        return next((row for row in self.data.get('pruned_events') or []
                     if row.get('event_id') == event_id), None)

    def inbox(self, thread_id, event_id=None, event_ids=None, after_cursor=0, limit=50,
              session_id=None, include_delivery=True):
        limit = max(1, min(100, int(limit or 50)))
        with self.lock:
            if event_id:
                event, _ = self._allowed_event(thread_id, event_id)
                if event is None:
                    pruned = self._pruned(event_id)
                    return {'events': [], 'found': False, 'pruned': bool(pruned),
                            'read_hint': {'tools': ['zcode_notify_inbox'],
                                          'note': 'event was pruned locally' if pruned else 'event not found for this source'},
                            'cursor': int(self.data.get('next_event_seq') or 1) - 1}
                return {'events': [self._event_view(event, include_delivery)], 'found': True,
                        'pruned': False, 'cursor': event.get('seq') or 0,
                        'delivery_included': bool(include_delivery)}
            if event_ids:
                rows, found = [], []
                for ident in event_ids:
                    event, _ = self._allowed_event(thread_id, str(ident))
                    if event:
                        rows.append(self._event_view(event, include_delivery))
                        found.append(str(ident))
                return {'events': rows, 'found_ids': found,
                        'missing_ids': [str(i) for i in event_ids if str(i) not in found],
                        'cursor': max([row.get('seq') or 0 for row in rows] or [0]),
                        'delivery_included': bool(include_delivery)}
            allowed = []
            for event in self._events().values():
                sub = self._subscriptions().get(event.get('subscription_id'))
                if sub is None or (sub.get('source') or {}).get('thread_id') != thread_id:
                    continue
                if session_id and event.get('session_id') != session_id:
                    continue
                if int(event.get('seq') or 0) <= int(after_cursor or 0):
                    continue
                allowed.append(event)
            allowed.sort(key=lambda row: row.get('seq') or 0)
            page = allowed[:limit]
            return {'events': [self._event_view(event, include_delivery) for event in page],
                    'cursor': page[-1].get('seq') if page else int(after_cursor or 0),
                    'more': len(allowed) > limit, 'delivery_included': bool(include_delivery)}

    def _event_view(self, event, include_delivery):
        view = copy.deepcopy(event)
        if not include_delivery:
            view.pop('delivery', None)
        return view

    def ack(self, thread_id, event_ids=None, up_to_cursor=None, retry_failed=False, now=None):
        now = now or self.clock()
        acked, cancelled, retried, denied = 0, 0, 0, 0
        with self.lock:
            allowed = []
            for event in self._events().values():
                sub = self._subscriptions().get(event.get('subscription_id'))
                if sub is None or (sub.get('source') or {}).get('thread_id') != thread_id:
                    continue
                if event_ids and event['event_id'] in {str(i) for i in event_ids}:
                    allowed.append(event)
                elif up_to_cursor is not None and int(event.get('seq') or 0) <= int(up_to_cursor):
                    allowed.append(event)
            for event in allowed:
                event['delivery']['acked_at'] = now
                acked += 1
                state = event['delivery']['state']
                if state in (DELIVERY_PENDING, DELIVERY_CLAIMED, DELIVERY_FAILED,
                             DELIVERY_UNCERTAIN, DELIVERY_UNSUPPORTED):
                    event['delivery']['state'] = DELIVERY_CANCELLED
                    event['delivery']['cancel_requested'] = True
                    cancelled += 1
            if event_ids:
                denied = len([i for i in event_ids if not self._allowed_event(thread_id, str(i))[0]])
            if retry_failed:
                requeue = {DELIVERY_FAILED, DELIVERY_UNCERTAIN}
                if (self.data.get('host_capability') or {}).get('state') == 'supported':
                    requeue.add(DELIVERY_UNSUPPORTED)
                for event in self._events().values():
                    sub = self._subscriptions().get(event.get('subscription_id'))
                    if sub is None or (sub.get('source') or {}).get('thread_id') != thread_id:
                        continue
                    if (event['delivery']['state'] in requeue
                            and not event['delivery'].get('acked_at')):
                        event['delivery'].update(state=DELIVERY_PENDING, attempts=0,
                                                 last_error_code=None, next_attempt_at=now,
                                                 cancel_requested=False, send_attempted=False)
                        retried += 1
            self._save()
        return {'acked': acked, 'cancelled': cancelled, 'retried': retried,
                'not_found_for_source': denied}

    # ------------------------------------------------------------------ delivery
    def expire_events(self, now=None):
        """Expire pending deliveries whose event TTL has passed."""
        now = now or self.clock()
        changed = 0
        with self.lock:
            for event in self._events().values():
                delivery = event.get('delivery') or {}
                if (delivery.get('state') in (DELIVERY_PENDING, DELIVERY_CLAIMED, DELIVERY_FAILED,
                                              DELIVERY_UNCERTAIN, DELIVERY_UNSUPPORTED)
                        and event.get('expires_at') and now > event['expires_at']):
                    delivery['state'] = DELIVERY_EXPIRED
                    delivery['claim_id'] = None
                    delivery['claim_expires_at'] = None
                    changed += 1
            if changed:
                self._save()
        return changed

    def requeue_unsupported(self, now=None):
        """A confirmed supported host may retry events queued for a missing tool."""
        now = now or self.clock()
        changed = 0
        with self.lock:
            for event in self._events().values():
                delivery = event.get('delivery') or {}
                if (delivery.get('state') == DELIVERY_UNSUPPORTED
                        and not delivery.get('acked_at')):
                    delivery.update(state=DELIVERY_PENDING, attempts=0,
                                    last_error_code=None, next_attempt_at=now,
                                    cancel_requested=False, send_attempted=False)
                    changed += 1
            if changed:
                self._save()
        return changed

    def due_deliveries(self, now, limit=10):
        rows = []
        with self.lock:
            for event in self._events().values():
                delivery = event.get('delivery') or {}
                if delivery.get('state') != DELIVERY_PENDING:
                    continue
                next_at = delivery.get('next_attempt_at')
                if next_at is not None and float(next_at) > now:
                    continue
                if event.get('expires_at') and now > event['expires_at']:
                    delivery['state'] = DELIVERY_EXPIRED
                    continue
                rows.append(copy.deepcopy(event))
                if len(rows) >= limit:
                    break
        return rows

    def has_delivery_state(self, state):
        with self.lock:
            return any((event.get('delivery') or {}).get('state') == state
                       for event in self._events().values())

    def claim_delivery(self, event_id, now, lease=120):
        with self.lock:
            event = self._events().get(event_id)
            if event is None or event['delivery']['state'] != DELIVERY_PENDING:
                return None
            if event['delivery'].get('cancel_requested'):
                event['delivery']['state'] = DELIVERY_CANCELLED
                self._save()
                return None
            claim = uuid.uuid4().hex
            event['delivery'].update(state=DELIVERY_CLAIMED, claim_id=claim,
                                     claim_expires_at=now + lease, last_attempt_at=now,
                                     send_attempted=False)
            event['delivery']['attempts'] = int(event['delivery'].get('attempts') or 0) + 1
            sub = self._subscriptions().get(event.get('subscription_id'))
            thread_id = (sub.get('source') or {}).get('thread_id') if sub else ''
            source_host_id = (sub.get('source') or {}).get('host_id') if sub else ''
            host = copy.deepcopy((sub or {}).get('host') or {})
            self._save()
            return {'event': copy.deepcopy(event), 'claim_id': claim,
                    'thread_id': thread_id, 'source_host_id': source_host_id,
                    'host': host}

    def complete_delivery(self, event_id, claim_id, ok, error_code=None, now=None, max_attempts=5):
        now = now or self.clock()
        backoff = (5, 15, 60, 300, 900)
        with self.lock:
            event = self._events().get(event_id)
            if event is None:
                return None
            delivery = event['delivery']
            if delivery.get('claim_id') != claim_id:
                return None
            cancelled_in_flight = bool(delivery.get('cancel_requested'))
            if ok:
                delivery.update(state=DELIVERY_DELIVERED, delivered_at=now,
                                claim_id=None, claim_expires_at=None, last_error_code=None,
                                next_attempt_at=None)
            elif cancelled_in_flight:
                # The call may already have reached the host; never auto-retry it.
                delivery.update(state=DELIVERY_UNCERTAIN, claim_id=None,
                                claim_expires_at=None, last_error_code=error_code or 'cancelled',
                                next_attempt_at=None)
            else:
                attempts = int(delivery.get('attempts') or 0)
                if attempts >= max_attempts:
                    delivery.update(state=DELIVERY_FAILED, claim_id=None,
                                    claim_expires_at=None, last_error_code=error_code,
                                    next_attempt_at=None)
                elif error_code == 'unsupported':
                    delivery.update(state=DELIVERY_UNSUPPORTED, claim_id=None,
                                    claim_expires_at=None, last_error_code=error_code,
                                    next_attempt_at=None)
                else:
                    delay = backoff[min(attempts - 1, len(backoff) - 1)]
                    delivery.update(state=DELIVERY_PENDING, claim_id=None,
                                    claim_expires_at=None, last_error_code=error_code,
                                    next_attempt_at=now + delay, send_attempted=False)
            self._save()
            return copy.deepcopy(event)

    def mark_delivery_send_attempted(self, event_id, claim_id):
        """Persist the pre-call marker so a crash before the call can be retried."""
        with self.lock:
            event = self._events().get(event_id)
            if event is None or event['delivery'].get('claim_id') != claim_id:
                return False
            event['delivery']['send_attempted'] = True
            self._save()
            return True

    def expire_claims(self, now):
        expired = 0
        with self.lock:
            for event in self._events().values():
                delivery = event['delivery']
                if (delivery.get('state') == DELIVERY_CLAIMED
                        and delivery.get('claim_expires_at')
                        and float(delivery['claim_expires_at']) <= now):
                    if delivery.get('send_attempted'):
                        # The host call may have reached the host; never retry.
                        delivery.update(state=DELIVERY_UNCERTAIN, claim_id=None,
                                        claim_expires_at=None, last_error_code='lease_expired',
                                        next_attempt_at=None)
                    else:
                        # The worker died before calling the host; retry safely.
                        delivery.update(state=DELIVERY_PENDING, claim_id=None,
                                        claim_expires_at=None, last_error_code='lease_not_sent',
                                        next_attempt_at=now, send_attempted=False)
                    expired += 1
            if expired:
                self._save()
        return expired

    def cancel_delivery(self, event_id, thread_id):
        with self.lock:
            event, _ = self._allowed_event(thread_id, event_id)
            if event is None:
                return False
            state = event['delivery']['state']
            if state == DELIVERY_CLAIMED:
                event['delivery']['cancel_requested'] = True
            elif state in (DELIVERY_PENDING, DELIVERY_FAILED):
                event['delivery']['state'] = DELIVERY_CANCELLED
            elif state == DELIVERY_DELIVERED:
                event['delivery']['cancelled_after_delivery'] = True
            self._save()
            return True

    def delivery_cancel_requested(self, event_id, claim_id):
        with self.lock:
            event = self._events().get(event_id)
            if event is None:
                return False
            delivery = event['delivery']
            return bool(delivery.get('cancel_requested') and delivery.get('claim_id') == claim_id)

    def cancel_claimed_delivery(self, event_id, claim_id):
        with self.lock:
            event = self._events().get(event_id)
            if event is None:
                return False
            delivery = event['delivery']
            if delivery.get('claim_id') != claim_id:
                return False
            delivery.update(state=DELIVERY_CANCELLED, claim_id=None,
                            claim_expires_at=None, next_attempt_at=None,
                            cancel_requested=True)
            self._save()
            return True

    def _cancel_deliveries_locked(self, subscription_id):
        for event in self._events().values():
            if event.get('subscription_id') != subscription_id:
                continue
            state = event['delivery']['state']
            if state == DELIVERY_CLAIMED:
                event['delivery']['cancel_requested'] = True
            elif state in (DELIVERY_PENDING, DELIVERY_FAILED, DELIVERY_UNSUPPORTED):
                event['delivery']['state'] = DELIVERY_CANCELLED
            elif state == DELIVERY_DELIVERED:
                event['delivery']['cancelled_after_delivery'] = True

    # ------------------------------------------------------------------ summaries
    def status(self, thread_id=None, subscription_id=None, session_id=None):
        with self.lock:
            rows = []
            for sub in self._subscriptions().values():
                if thread_id is not None and (sub.get('source') or {}).get('thread_id') != thread_id:
                    continue
                if subscription_id and sub['subscription_id'] != subscription_id:
                    continue
                if session_id and sub.get('session_id') != session_id:
                    continue
                rows.append({
                    'subscription_id': sub['subscription_id'],
                    'session_id': sub.get('session_id'),
                    'state': sub.get('state'),
                    'state_reason': sub.get('state_reason'),
                    'arm_basis': sub.get('arm_basis'),
                    'created_at': sub.get('created_at'),
                    'updated_at': sub.get('updated_at'),
                    'expires_at': sub.get('expires_at'),
                    'start_deadline': sub.get('start_deadline'),
                    'terminal_event_id': sub.get('terminal_event_id'),
                    'anchor_ready': has_anchor_proof(sub),
                    'delivery': self._delivery_counts_locked(sub['subscription_id']),
                    'native_reset': bool((sub.get('native') or {}).get('reset')),
                })
            rows.sort(key=lambda row: row.get('created_at') or 0, reverse=True)
            return {'subscriptions': rows, 'thread_bound': bool(thread_id),
                    'host_capability': copy.deepcopy(self.data.get('host_capability')),
                    'store_incompatible': bool(self.incompatible),
                    'counts': self._counts_for_thread_locked(thread_id)}

    def _counts_for_thread_locked(self, thread_id):
        allowed = {sub['subscription_id'] for sub in self._subscriptions().values()
                   if thread_id is None or (sub.get('source') or {}).get('thread_id') == thread_id}
        states, delivery = {}, {}
        for sub in self._subscriptions().values():
            if sub['subscription_id'] not in allowed:
                continue
            states[sub.get('state')] = states.get(sub.get('state'), 0) + 1
        events = 0
        for event in self._events().values():
            if event.get('subscription_id') not in allowed:
                continue
            events += 1
            key = event['delivery'].get('state')
            delivery[key] = delivery.get(key, 0) + 1
        return {'subscriptions': len(allowed), 'events': events,
                'states': states, 'delivery': delivery,
                'store_incompatible': bool(self.incompatible),
                'host_capability': copy.deepcopy(self.data.get('host_capability'))}

    def aggregate(self):
        with self.lock:
            return self._counts_locked()

    def _counts_locked(self):
        states, delivery = {}, {}
        for sub in self._subscriptions().values():
            states[sub.get('state')] = states.get(sub.get('state'), 0) + 1
        for event in self._events().values():
            key = event['delivery'].get('state')
            delivery[key] = delivery.get(key, 0) + 1
        return {'subscriptions': len(self._subscriptions()), 'events': len(self._events()),
                'states': states, 'delivery': delivery,
                'store_incompatible': bool(self.incompatible),
                'host_capability': copy.deepcopy(self.data.get('host_capability'))}

    def _delivery_counts_locked(self, subscription_id):
        result = {}
        for event in self._events().values():
            if event.get('subscription_id') != subscription_id:
                continue
            key = event['delivery'].get('state')
            result[key] = result.get(key, 0) + 1
        return result

    def set_host_capability(self, state, error_code=None, checked_at=None):
        with self.lock:
            self.data['host_capability'] = {'state': state, 'checked_at': checked_at or self.clock(),
                                            'error_code': error_code}
            self._save()
            return copy.deepcopy(self.data['host_capability'])

    def mark_observer_error(self, subscription_id, revision, error_code, now=None):
        now = now or self.clock()
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None or int(sub.get('revision') or 0) != int(revision):
                return []
            sub['consecutive_errors'] = int(sub.get('consecutive_errors') or 0) + 1
            # Safe diagnostic only: exception type, never message content,
            # paths, ids or credentials.
            sub['observer_last_error_type'] = str(error_code)[:80]
            sub['observer_last_error_at'] = now
            sub['updated_at'] = now
            sub['revision'] = int(sub.get('revision') or 0) + 1
            delay = (0.5, 1, 2, 4, 8, 15, 30)[min(sub['consecutive_errors'] - 1, 6)]
            sub['next_poll_at'] = now + delay
            events = []
            if sub['consecutive_errors'] >= 5 and not sub.get('observer_degraded'):
                sub['observer_degraded'] = True
                event = self._append_event_locked(
                    sub, 'observer.degraded', 'engine_unavailable',
                    {'request_id': sub['subscription_id']}, status='unknown',
                    terminal=False, delivery='inbox_only', now=now)
                if event:
                    events.append(event)
            self._save()
            return events

    def mark_observer_recovered(self, subscription_id, revision, now=None):
        now = now or self.clock()
        with self.lock:
            sub = self._subscriptions().get(subscription_id)
            if sub is None or int(sub.get('revision') or 0) != int(revision):
                return []
            events = []
            if sub.get('observer_degraded'):
                sub['observer_degraded'] = False
                event = self._append_event_locked(
                    sub, 'observer.recovered', 'engine_unavailable',
                    {'request_id': sub['subscription_id']}, status='info',
                    terminal=False, delivery='inbox_only', now=now)
                if event:
                    events.append(event)
            sub['consecutive_errors'] = 0
            sub['updated_at'] = now
            sub['revision'] = int(sub.get('revision') or 0) + 1
            self._save()
            return events


# --------------------------------------------------------------------------- observer
class Observer:
    """Daemon-only polling loop.  It never runs during MCP discovery.

    ``runtime`` must provide:
      ensure_session(session_id, deadline), snapshot(session_id, deadline),
      session_events(session_id, after_seq, deadline), messages(session_id, after_message_id, deadline),
      workflow_runs(session_id, deadline), run_topology(session_id, run_id, deadline),
      subagents(session_id, deadline), pending(), engine_identity().
    """

    def __init__(self, store, runtime, clock=time.time, poll_seconds=1,
                 pending_ttl=3600, deadline_seconds=20):
        self.store = store
        self.runtime = runtime
        self.clock = clock
        self.poll_seconds = max(1, int(poll_seconds or 1))
        self.pending_ttl = max(60, int(pending_ttl or 3600))
        self.deadline_seconds = max(2, int(deadline_seconds or 20))
        self.stop_event = threading.Event()
        self.thread = None
        self.inflight = set()
        self.lock = threading.Lock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='notification-observer', daemon=True)
        self.thread.start()

    def stop(self, timeout=5):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)

    def _run(self):
        # Let the daemon finish writing connection.json and never connect the
        # engine before there is at least one subscription to observe.  The
        # loop wakes at least once a second; per-subscription next_poll_at
        # decides whether an engine RPC actually runs.
        interval = max(.2, min(1.0, float(self.poll_seconds)))
        while not self.stop_event.wait(interval):
            try:
                self.tick()
            except Exception:
                pass

    def due(self, now):
        rows = []
        for sub in self.store.all_subscriptions():
            if sub.get('state') in TERMINAL_STATES:
                continue
            if sub['subscription_id'] in self.inflight:
                continue
            if float(sub.get('next_poll_at') or 0) > now:
                continue
            rows.append(sub)
        return rows

    def tick(self):
        now = self.clock()
        # High-level delegations may have durable follow-ups queued behind a
        # busy turn.  Drain only at a foreground-idle boundary; never invent a
        # send or block the observer loop on a slow engine.
        drain = getattr(self.runtime, 'drain_queues', None)
        if callable(drain):
            try:
                drain(now)
            except Exception:
                pass
        for sub in self.due(now):
            self.observe(sub)
        # The job state machine reads the already-persisted subscription truth.
        # It must never invent completion from busy/idle/accepted.
        sync = getattr(self.runtime, 'sync_jobs', None)
        if callable(sync):
            try:
                sync(now)
            except Exception:
                pass

    def poll_once(self):
        """One explicit observation pass; used by tests and restart recovery."""
        self.tick()

    def observe(self, sub):
        subscription_id = sub['subscription_id']
        revision = int(sub.get('revision') or 0)
        with self.lock:
            if subscription_id in self.inflight:
                return
            self.inflight.add(subscription_id)
        try:
            evidence = self.collect(sub)
            verdict = decide(sub, evidence)
            self.store.apply_decision(subscription_id, revision, verdict, evidence)
        except Exception as error:
            now = self.clock()
            verdict = decide(sub, {'now': now, 'engine_ok': False})
            if verdict.get('state') in TERMINAL_STATES:
                # A broken engine must not hold an armed subscription forever:
                # expiry still converges without a collection sample.
                self.store.apply_decision(subscription_id, revision, verdict,
                                          {'now': now, 'engine_ok': False})
            else:
                self.store.mark_observer_error(subscription_id, revision,
                                               type(error).__name__, now=now)
        finally:
            with self.lock:
                self.inflight.discard(subscription_id)

    def collect(self, sub):
        now = self.clock()
        # Wall clock is for evidence/expiry; RPC budgets use the monotonic clock.
        deadline = time.monotonic() + self.deadline_seconds
        sid = sub['session_id']
        identity = self.runtime.engine_identity(sid)
        turn = sub.get('turn') or {}
        same_engine = ((turn.get('engine_pid') in (None, identity.get('pid')))
                       and (int(turn.get('generation') or 0) == int(identity.get('generation') or 0)))
        pending_rows = (self.runtime.pending(sid)
                        if callable(getattr(self.runtime, 'pending', None)) else [])
        evidence = {'now': now, 'engine_ok': False, 'pending': pending_rows,
                    'pending_ttl': self.pending_ttl, 'poll_seconds': self.poll_seconds,
                    'engine_pid': identity.get('pid'), 'generation': identity.get('generation'),
                    'recovered': not same_engine,
                    'known_child_ids': [], 'native_seq': sub.get('last_native_seq') or 0}
        snapshot = self.runtime.ensure_session(sid, deadline) or self.runtime.snapshot(sid, deadline)
        projection = (snapshot or {}).get('projection') or {}
        status = str(projection.get('status') or '').lower() or None
        turn_count = int(projection.get('turnCount') or 0)
        jobs = self.runtime.jobs(snapshot, sid, deadline)
        workflow_result = self.runtime.workflows(sid, deadline)
        if isinstance(workflow_result, tuple) and len(workflow_result) == 2:
            runs, second = workflow_result
        else:
            runs, second = workflow_result, None
        if not isinstance(runs, list):
            runs = []
        if isinstance(second, bool):
            run_rows, run_truncated = runs, second
        elif isinstance(second, list):
            run_rows, run_truncated = second, len(second) >= 64
        else:
            run_rows, run_truncated = runs, len(runs) >= 64
        children, child_ids = self.runtime.children(sid, deadline)
        evidence.update(status=status, turn_count=turn_count, jobs=jobs, runs=runs,
                        children=children, known_child_ids=child_ids, engine_ok=True,
                        runs_truncated=run_truncated)
        stream = self._stream_from_snapshot(snapshot)
        native = self._native(sub, sid, deadline, stream=stream)
        evidence.update(native)
        messages = self._messages(sub, sid, deadline, native)
        evidence.update(messages)
        evidence['anchor_proof'] = messages.get('anchor_proof') or (sub.get('turn') or {}).get('anchor_proof')
        evidence['final_after_anchor'] = messages.get('final_after_anchor', False)
        evidence['final_message_id'] = messages.get('final_message_id') or ''
        evidence['accepted_recovered'] = bool(
            (sub.get('turn') or {}).get('send_attempted')
            and not (sub.get('turn') or {}).get('send_accepted')
            and evidence.get('anchor_proof'))
        evidence['started_evidence'] = self._started_evidence(sub, evidence)
        evidence['started'] = bool(evidence['started_evidence'])
        child_stream = _canonical([
            [str(child.get('id') or ''), str(child.get('activity') or '')]
            for child in children if isinstance(child, dict)
            and child.get('activity')])
        evidence['child_activity'] = (
            hashlib.sha256(child_stream.encode('utf-8')).hexdigest()[:16]
            if child_stream else '')
        active_digest = _canonical([
            [(_item_id(job), _job_state(job)) for job in jobs],
            [(row.get('run_id'), row.get('status')) for row in run_rows],
            [(_item_id(child), _child_state(child)) for child in children],
            evidence['child_activity'],
        ])
        digest = _obs_digest(status, turn_count, evidence['final_message_id'],
                             active_digest, evidence.get('message_activity') or '')
        previous = sub.get('last_sample_digest') or ''
        stable_samples = min(2, int(sub.get('stable_samples') or 0) + 1) if digest == previous and previous else 1
        evidence['sample_digest'] = digest
        evidence['stable_samples'] = stable_samples
        evidence['stable'] = stable_samples >= 2
        evidence['fresh_pending_ids'] = classify_pending(
            sub, evidence.get('pending') or (), now, self.pending_ttl)['fresh']
        evidence['new_settled_runs'] = self._settled_runs(sub, run_rows)
        evidence['new_failed_children'] = self._failed_children(sub, children)
        epoch = (evidence.get('native') or {}).get('epoch') or ''
        evidence['run_cursors'] = {str(epoch) + ':' + str(row.get('run_id')): int(row.get('cursor') or 0)
                                   for row in run_rows if row.get('run_id')}
        return evidence

    def _stream_from_snapshot(self, snapshot):
        runtime = (snapshot or {}).get('runtime') or {}
        if isinstance(runtime, dict):
            for key in ('instanceId', 'engineInstanceId', 'streamId', 'serverInstanceId',
                        'sessionStreamId'):
                if runtime.get(key):
                    return str(runtime[key])
        return ''

    def _read_native_events(self, sid, after, deadline):
        rows, cursor = [], max(0, int(after or 0))
        while len(rows) < 5000:
            page = self.runtime.session_events(sid, cursor, deadline)
            page_rows = (page or {}).get('events') or []
            if not page_rows:
                break
            rows.extend(page_rows)
            next_cursor = int(page_rows[-1].get('seq') or cursor)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return rows

    def _native(self, sub, sid, deadline, stream=''):
        native = copy.deepcopy(sub.get('native') or {})
        after = int(native.get('last_seq') or 0)
        rows = self._read_native_events(sid, after, deadline)
        if not stream:
            stream = self.runtime.stream_id(sid, deadline)
        reset = False
        if stream and native.get('stream_id') and stream != native.get('stream_id'):
            reset = True
        if any(int(row.get('seq') or 0) <= after for row in rows if after):
            reset = True
        if reset:
            rows = self._read_native_events(sid, 0, deadline)
            native['reset'] = True
            native['last_seq'] = 0
        seen = set(native.get('seen') or [])
        last_seq = int(native.get('last_seq') or 0)
        last_key = native.get('last_event_key') or ''
        for row in rows:
            seq = int(row.get('seq') or 0)
            key = hashlib.sha256(_canonical([seq, row.get('type'), row.get('payload')]).encode('utf-8')).hexdigest()
            if seq <= last_seq and key in seen:
                continue
            seen.add(key)
            last_seq = max(last_seq, seq)
            last_key = key
        native['seen'] = list(seen)[-300:]
        native['last_seq'] = last_seq
        native['last_event_key'] = last_key
        native['stream_id'] = stream or native.get('stream_id') or ''
        native['epoch'] = native.get('stream_id') or ''

        # ZCode 3.18 does not echo inputId in session/messages.  The persisted
        # native event stream is the authoritative turn association:
        # turn.started carries inputId + messageId, turn.completed carries
        # inputId.  Only an inputId-bound event may anchor this subscription.
        turn = sub.get('turn') or {}
        input_id = str(turn.get('input_id') or '')
        started_message_id = str(native.get('turn_started_message_id') or '')
        started_number = native.get('turn_started_number')
        completed = bool(native.get('turn_completed_input_id')
                         and str(native.get('turn_completed_input_id')) == input_id)
        if input_id:
            for row in rows:
                kind = row.get('type')
                payload = row.get('payload') or {}
                if kind == 'turn.started' and str(payload.get('inputId') or '') == input_id:
                    started_message_id = str(payload.get('messageId') or started_message_id)
                    started_number = payload.get('turnNumber', started_number)
                elif kind == 'turn.completed' and str(payload.get('inputId') or '') == input_id:
                    completed = True
                    native['turn_completed_input_id'] = input_id
            if started_message_id:
                native['turn_started_message_id'] = started_message_id
                native['turn_started_number'] = started_number
        return {'native': native, 'native_seq': last_seq,
                'native_turn_started_message_id': started_message_id,
                'native_turn_started_number': started_number,
                'native_turn_completed': completed}

    def _messages(self, sub, sid, deadline, native=None):
        turn = sub.get('turn') or {}
        native = native or {}
        native_started_id = str(native.get('native_turn_started_message_id') or '')
        native_completed = bool(native.get('native_turn_completed'))
        anchor = (turn.get('user_message_id') or native_started_id
                  or turn.get('send_result_message_id') or turn.get('before_message_id') or '')
        page = self.runtime.messages(sid, anchor, deadline)
        rows = (page or {}).get('messages') or []
        input_id = str(turn.get('input_id') or '')
        anchor_proof = ''
        user_message_id = ''
        final_message_id = ''
        final_after_anchor = False
        before_id = str(turn.get('before_message_id') or '')
        last_assistant = ''
        last_assistant_message = None

        # ZCode 3.18 returns a no-cursor session/messages page oldest-first,
        # and its first row can be a visible model-change timeline_event.
        # Associate the armed turn by the native turn.started messageId and
        # only accept real assistant_response rows after that anchor.
        anchor_index = None
        anchor_time = None
        for index, message in enumerate(rows):
            if not is_visible_message(message):
                continue
            ident = str(message_identity(message) or '')
            if anchor and ident and ident == str(anchor):
                anchor_index = index
                anchor_time = (message.get('info') or {}).get('time')
                break

        for index, message in enumerate(rows):
            if not is_visible_message(message):
                continue
            info = message.get('info') or {}
            ident = str(message_identity(message) or '')
            role = info.get('role') or message.get('role')
            if native_started_id and ident == native_started_id:
                anchor_proof = 'native_turn_started'
                user_message_id = native_started_id
            elif not native_started_id and input_id:
                ids = {str(info.get(key)) for key in
                       ('inputId', 'clientInputId', 'requestId', 'userInputId') if info.get(key)}
                ids.update(str(message.get(key)) for key in ('inputId', 'clientInputId')
                           if message.get(key))
                if input_id in ids:
                    anchor_proof = 'input_id_echo'
                    if role == 'user':
                        user_message_id = ident
            if is_assistant_response(message):
                if anchor_index is not None and index <= anchor_index:
                    continue
                if anchor_time is not None and info.get('time') is not None:
                    try:
                        if float(info.get('time')) < float(anchor_time):
                            continue
                    except (TypeError, ValueError):
                        pass
                if ident:
                    last_assistant = ident
                    last_assistant_message = message

        if native_started_id:
            anchor_proof = anchor_proof or 'native_turn_started'
            user_message_id = user_message_id or native_started_id
        # Only a native turn.started event, an input-id echo, or the anchor
        # actually present in this page proves the candidate is after it.
        # A caller-supplied user_message_id that the page does not contain is
        # not proof: an ignored afterMessageId can return an older assistant.
        trusted_anchor = bool(native_started_id or anchor_proof
                              or anchor_index is not None)
        if last_assistant and last_assistant != before_id:
            if anchor and anchor_index is None and not trusted_anchor:
                # The anchor is not in this page, so the candidate cannot be
                # proven to be after it (a send receipt may not be a message id,
                # and an ignored afterMessageId can return older assistants).
                last_assistant = ''
            else:
                final_message_id = last_assistant
                final_after_anchor = True
        if native_completed and final_message_id:
            final_after_anchor = True
        activity = message_activity_digest(last_assistant_message) if final_message_id else ''
        return {'anchor_proof': anchor_proof, 'user_message_id': user_message_id,
                'final_message_id': final_message_id, 'final_after_anchor': final_after_anchor,
                'message_activity': activity}

    def _started_evidence(self, sub, evidence):
        turn = sub.get('turn') or {}
        if sub.get('state') in ('started', 'running', 'settling'):
            return turn.get('started_evidence') or 'running_observed'
        if turn.get('started_evidence'):
            return turn['started_evidence']
        arm_basis = str(sub.get('arm_basis') or '')
        if arm_basis.startswith('attach'):
            return arm_basis
        if evidence.get('anchor_proof'):
            return str(evidence.get('anchor_proof'))
        if turn.get('send_result_message_id'):
            return 'send_result_message'
        if turn.get('user_message_id'):
            return 'user_message'
        if (evidence.get('status') == 'running'
                and int(evidence.get('turn_count') or 0) > int(turn.get('before_turn_count') or 0)):
            return 'running_observed'
        return ''

    def _started(self, sub, evidence):
        return bool(self._started_evidence(sub, evidence))

    def _settled_runs(self, sub, run_rows):
        baseline = sub.get('baseline') or {}
        known = set(baseline.get('run_ids') or ())
        active = set(baseline.get('active_run_ids') or ())
        result = []
        for row in run_rows:
            run_id = str(row.get('run_id') or '')
            if not run_id or (run_id in known and run_id not in active):
                continue
            status = _run_state(row)
            if status in ('completed', 'failed', 'cancelled'):
                result.append((run_id, status))
        return result

    def _failed_children(self, sub, children):
        baseline = sub.get('baseline') or {}
        known = set(baseline.get('child_ids') or ())
        active = set(baseline.get('active_child_ids') or ())
        result = []
        for child in children:
            ident = _item_id(child)
            if not ident or (ident in known and ident not in active):
                continue
            if _child_state(child) == 'failed':
                result.append(ident)
        return result


# Documented names used by the terminal-policy tests and by the observer.
verify = decide
stable_event_id = make_event_id
