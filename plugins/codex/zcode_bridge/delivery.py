"""Deliver persisted events to the originating chat, after the fact is stored.

The queue never invents truth.  An event is persisted by ``NotificationStore``
first; this worker only claims it for a best-effort host wake.  A failed or
uncertain wake never deletes the event: ``zcode_notify_inbox`` remains the
authoritative read path.

The host adapter is injected.  Tests inject a fake command/transport; the daemon
injects ``host_wake.HostWakeAdapter``.  No toast, log line or MCP response is
ever reported as a wake.  ``delivered`` means the host accepted the tools/call,
not that the model read the event or continued the task.
"""
from __future__ import annotations

import threading
import time

from .notifications import DELIVERY_UNSUPPORTED


def wake_prompt(session_id, event_id, auto_continue=False):
    """Minimal prompt: only session/event IDs and the read/verify instruction."""
    action = '继续原任务的验收与后续工作' if auto_continue else '验收'
    return ('[zcode-bridge] session=' + str(session_id) + ' event=' + str(event_id)
            + '. 请读取 zcode_notify_inbox(event_id=' + str(event_id) + ') 并' + action + '。')


class DeliveryWorker:
    def __init__(self, store, waker=None, host_context=None, clock=time.time,
                 poll_seconds=1, max_attempts=5, auto_continue_lookup=None,
                 capability_ttl=600):
        self.store = store
        self.waker = waker
        self.host_context = host_context
        self.clock = clock
        self.poll_seconds = max(1, int(poll_seconds or 1))
        self.max_attempts = max(1, int(max_attempts or 5))
        self.capability_ttl = max(10, int(capability_ttl or 600))
        # Optional callback(event) -> bool.  Without it every wake uses the
        # conservative "read and verify" prompt.
        self.auto_continue_lookup = auto_continue_lookup
        self.stop_event = threading.Event()
        self.kick_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='notification-delivery', daemon=True)
        self.thread.start()

    def stop(self, timeout=5):
        self.stop_event.set()
        self.kick_event.set()
        if self.thread:
            self.thread.join(timeout)

    def kick(self):
        self.kick_event.set()

    def _run(self):
        interval = max(.2, min(1.0, float(self.poll_seconds)))
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self.kick_event.wait(interval)
            self.kick_event.clear()

    def _latest_host(self):
        if callable(self.host_context):
            try:
                value = self.host_context()
                return value if isinstance(value, dict) else {}
            except Exception:
                return {}
        return {}

    def _capability(self):
        return dict(self.store.data.get('host_capability') or {})

    def _probe_if_needed(self, now, latest_host):
        capability = self._capability()
        if not hasattr(self.waker, 'probe'):
            return capability
        checked = capability.get('checked_at')
        if capability.get('state') not in (None, '', 'not_probed') and checked:
            try:
                fresh = now - float(checked) < self.capability_ttl
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                return capability
        try:
            result = self.waker.probe(10, host=latest_host) or {}
        except Exception:
            self.store.set_host_capability('degraded', 'probe_failed', checked_at=now)
            return self._capability()
        state = result.get('state') or 'unsupported'
        error_code = result.get('error_code')
        self.store.set_host_capability(state, error_code, checked_at=now)
        if state == 'supported' and capability.get('state') != 'supported':
            # A confirmed supported host may deliver events previously queued
            # because an older host lacked send_message_to_thread.
            self.store.requeue_unsupported(now)
        return self._capability()

    def tick(self):
        now = self.clock()
        self.store.expire_claims(now)
        self.store.expire_events(now)
        if self.waker is None:
            # No host adapter.  Keep events pending rather than burning
            # attempts or claiming a version is unsupported.
            return 0
        if not self.store.due_deliveries(now, limit=1) and not self.store.has_delivery_state(
                DELIVERY_UNSUPPORTED):
            # No wake is due; do not spawn a host process or create state just
            # because the daemon is idle.
            return 0
        latest_host = self._latest_host()
        capability = self._probe_if_needed(now, latest_host)
        state, error_code = capability.get('state'), capability.get('error_code')
        if state == DELIVERY_UNSUPPORTED and error_code in ('missing_tool', 'unsupported', None):
            # tools/list proved the tool is absent.  Keep the queue until a
            # forced probe or the capability TTL confirms a supported host.
            return 0
        delivered = 0
        for event in self.store.due_deliveries(now, limit=5):
            if event.get('subscription_id') and self._terminal_for_cancelled(event):
                continue
            claim = self.store.claim_delivery(event['event_id'], now)
            if claim is None:
                continue
            event = claim['event']
            thread_id = claim.get('thread_id') or ''
            if self.store.delivery_cancel_requested(event['event_id'], claim['claim_id']):
                self.store.cancel_claimed_delivery(event['event_id'], claim['claim_id'])
                continue
            if not thread_id:
                self.store.complete_delivery(event['event_id'], claim['claim_id'], False,
                                             error_code='source_mismatch', now=now,
                                             max_attempts=self.max_attempts)
                continue
            if not self.store.mark_delivery_send_attempted(event['event_id'], claim['claim_id']):
                continue
            host = dict(claim.get('host') or {})
            host.update({key: value for key, value in latest_host.items()
                         if value and key in ('pipe', 'runtime_cli', 'node')})
            if latest_host:
                self.store.update_host(event.get('subscription_id'), host)
            auto_continue = False
            if self.auto_continue_lookup is not None:
                try:
                    auto_continue = bool(self.auto_continue_lookup(event))
                except Exception:
                    auto_continue = False
            prompt = wake_prompt(event.get('session_id'), event.get('event_id'), auto_continue)
            try:
                result = self.waker.send(thread_id, event.get('session_id'),
                                         event.get('event_id'), prompt, host=host,
                                         host_id=claim.get('source_host_id') or '')
            except Exception as error:
                result = {'ok': False, 'error_code': type(error).__name__}
            ok = bool(result.get('ok'))
            error_code = (result.get('error_code')
                          or ('unsupported' if result.get('unsupported') else 'wake_failed'))
            previous_capability = self._capability().get('state')
            if ok:
                delivered += 1
            self.store.complete_delivery(event['event_id'], claim['claim_id'], ok,
                                         error_code=error_code, now=now,
                                         max_attempts=self.max_attempts)
            capability = self._capability().get('state')
            if ok and previous_capability != 'supported':
                self.store.set_host_capability('supported', checked_at=now)
                self.store.requeue_unsupported(now)
            elif error_code == 'unsupported' and capability != DELIVERY_UNSUPPORTED:
                self.store.set_host_capability(DELIVERY_UNSUPPORTED, 'missing_tool',
                                               checked_at=now)
            elif error_code == 'handshake_failed' and (
                    capability != DELIVERY_UNSUPPORTED
                    or self._capability().get('error_code') != 'handshake_failed'):
                self.store.set_host_capability(DELIVERY_UNSUPPORTED, 'handshake_failed',
                                               checked_at=now)
            elif error_code == 'pipe_unavailable' and capability != 'pipe_unavailable':
                self.store.set_host_capability('pipe_unavailable', 'pipe_unavailable',
                                               checked_at=now)
            elif error_code in ('host_unavailable', 'timeout') and capability != 'degraded':
                self.store.set_host_capability('degraded', error_code, checked_at=now)
        return delivered

    def _terminal_for_cancelled(self, event):
        return event.get('delivery', {}).get('state') == 'cancelled'


def capability_state_from_probe(result):
    """Map a host probe to the only capability states exposed by the bridge."""
    state = (result or {}).get('state')
    if state in ('supported', 'unsupported', 'degraded', 'pipe_unavailable', 'handshake_failed',
                 'host_unavailable', 'not_probed'):
        return state
    if (result or {}).get('unsupported'):
        return DELIVERY_UNSUPPORTED
    return 'unsupported'
