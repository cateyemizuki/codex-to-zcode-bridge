"""Optional, asynchronous client for the private ZCode Desktop sync shim.

The adapter is deliberately small and defensive:

* it is disabled unless the local settings enable it and name a private control
  file; nothing is read from the repository or from a default author path;
* it only calls the loopback ``sync`` operation, only for workspaces present in
  the control file's ``allowedWorkspaces``, with at most 50 session ids;
* requests are merged per workspace and throttled by a configurable interval;
* a missing config, token, host or endpoint is recorded as a status code and
  never raises into delegate/send;
* the token is kept in memory only for the connection and never appears in a
  status, tool response, prompt or log;
* it never opens a project, focuses a window or restarts an instance.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import threading
import time
from pathlib import Path

MAX_SESSIONS = 50
DEFAULT_INTERVAL = 5
CONNECT_TIMEOUT = 2.0
IO_TIMEOUT = 5.0
MAX_PENDING_WORKSPACES = 100


class DesktopSyncError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


class DesktopSyncAdapter:
    """Background, best-effort sync client; all failures become status only."""

    def __init__(self, settings_getter=None, clock=time.time,
                 connect_timeout=CONNECT_TIMEOUT, io_timeout=IO_TIMEOUT):
        self.settings_getter = settings_getter or (lambda: {})
        self.clock = clock
        self.connect_timeout = float(connect_timeout)
        self.io_timeout = float(io_timeout)
        self.lock = threading.RLock()
        self.idle = threading.Condition(self.lock)
        self.pending = {}
        self.last_sent = {}
        self.state = 'disabled'
        self.reason = ''
        self.last_attempt_at = None
        self.last_sync_at = None
        self.last_result = None
        self.truncated = False
        self.inflight = 0
        self.thread = None
        self.stop_event = threading.Event()
        self.wake = threading.Event()

    # ------------------------------------------------------------------ config
    def _settings(self):
        try:
            value = self.settings_getter()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _enabled(self):
        return bool(self._settings().get('desktop_sync_enabled', False))

    def _interval(self):
        value = self._settings().get('desktop_sync_min_interval_seconds', DEFAULT_INTERVAL)
        try:
            return min(300, max(1, int(value)))
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL

    def _control_path(self):
        value = self._settings().get('desktop_sync_control_file', '')
        return str(value or '')

    @staticmethod
    def _normalize(value):
        resolved = Path(value).expanduser().resolve()
        return os.path.normcase(str(resolved)), str(resolved)

    def _load_control(self, path):
        try:
            raw = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise DesktopSyncError('config_missing') from error
        if not isinstance(raw, dict):
            raise DesktopSyncError('config_invalid')
        token = raw.get('token')
        if not isinstance(token, str) or len(token) < 16:
            raise DesktopSyncError('token_missing')
        host = str(raw.get('host') or '127.0.0.1')
        if host != '127.0.0.1':
            raise DesktopSyncError('host_not_allowed')
        allowed = raw.get('allowedWorkspaces')
        if not isinstance(allowed, list) or not allowed:
            raise DesktopSyncError('allowlist_missing')
        mapping = {}
        for value in allowed:
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                key, resolved = self._normalize(value)
            except OSError:
                continue
            mapping[key] = resolved
        if not mapping:
            raise DesktopSyncError('allowlist_missing')
        port = raw.get('port')
        native = raw.get('nativeTransport')
        return {
            'token': token,
            'host': host,
            'port': port if type(port) is int else 0,
            'discoveryPath': str(raw.get('discoveryPath') or ''),
            'allowed': mapping,
            'native': copy.deepcopy(native) if isinstance(native, dict) else {},
        }

    # ----------------------------------------------------------------- request
    def request_sync(self, workspace, session_ids=None, reason=''):
        """Queue an allowlisted sync without blocking the caller."""
        cfg = self._settings()
        if not bool(cfg.get('desktop_sync_enabled', False)):
            self._set_state('disabled', '')
            return {'accepted': False, 'state': 'disabled'}
        control_path = self._control_path()
        if not control_path:
            self._set_state('config_missing', 'control_file_empty')
            return {'accepted': False, 'state': 'config_missing'}
        try:
            control = self._load_control(control_path)
        except DesktopSyncError as error:
            self._set_state(error.code, error.code)
            return {'accepted': False, 'state': error.code}
        try:
            key, resolved = self._normalize(workspace)
        except (OSError, TypeError, ValueError):
            self._set_state('workspace_not_allowed', 'workspace_invalid')
            return {'accepted': False, 'state': 'workspace_not_allowed'}
        if key not in control['allowed'] or not Path(resolved).is_dir():
            self._set_state('workspace_not_allowed', 'workspace_not_allowed')
            return {'accepted': False, 'state': 'workspace_not_allowed'}
        sessions = [str(value) for value in (session_ids or ())
                    if isinstance(value, str) and value]
        truncated = len(sessions) > MAX_SESSIONS
        sessions = sessions[:MAX_SESSIONS]
        if not sessions:
            return {'accepted': False, 'state': 'no_sessions'}
        now = float(self.clock())
        with self.lock:
            item = self.pending.get(key)
            if item is None:
                if len(self.pending) >= MAX_PENDING_WORKSPACES:
                    self._set_state('queue_full', 'pending_workspaces')
                    return {'accepted': False, 'state': 'queue_full'}
                item = {'workspace': resolved, 'sessions': set(), 'reason': '',
                        'requested_at': now, 'control': control}
                self.pending[key] = item
            item['sessions'].update(sessions)
            if len(item['sessions']) > MAX_SESSIONS:
                item['sessions'] = set(list(item['sessions'])[:MAX_SESSIONS])
                truncated = True
            item['reason'] = str(reason or '')
            item['requested_at'] = now
            item['control'] = control
            self.truncated = self.truncated or truncated
            self.state = 'queued'
            self.reason = ''
            self._ensure_thread()
            self.wake.set()
        return {'accepted': True, 'state': 'queued', 'truncated': bool(truncated)}

    def _ensure_thread(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='desktop-sync',
                                       daemon=True)
        self.thread.start()

    # ------------------------------------------------------------------ worker
    def _run(self):
        while not self.stop_event.is_set():
            if not self._enabled():
                with self.lock:
                    self.pending.clear()
                    self.state = 'disabled'
                    self.reason = ''
                    self.idle.notify_all()
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            now = float(self.clock())
            interval = self._interval()
            with self.lock:
                due = [key for key, item in self.pending.items()
                       if now - float(self.last_sent.get(key) or 0) >= interval]
                has_pending = bool(self.pending)
            if due:
                for key in due:
                    with self.lock:
                        item = self.pending.pop(key, None)
                        if item is None:
                            continue
                        self.inflight += 1
                    try:
                        self._sync(item)
                    finally:
                        with self.lock:
                            self.inflight -= 1
                            self.last_sent[key] = float(self.clock())
                            self.idle.notify_all()
                continue
            self.wake.wait(0.2 if has_pending else 1.0)
            self.wake.clear()
            with self.lock:
                self.idle.notify_all()

    def _endpoint(self, control):
        port = control.get('port')
        if not (type(port) is int and 0 < port < 65536):
            discovery = control.get('discoveryPath') or ''
            if not discovery:
                raise DesktopSyncError('discovery_missing')
            try:
                raw = json.loads(Path(discovery).expanduser().read_text(encoding='utf-8'))
                port = int(raw.get('port') or 0)
            except (OSError, ValueError, TypeError) as error:
                raise DesktopSyncError('discovery_missing') from error
        if not (0 < port < 65536):
            raise DesktopSyncError('discovery_missing')
        return '127.0.0.1', port

    def _call(self, control, message):
        host, port = self._endpoint(control)
        token = control['token']
        try:
            with socket.create_connection((host, port), timeout=self.connect_timeout) as sock:
                sock.settimeout(self.io_timeout)
                stream = sock.makefile('rwb', buffering=0)

                def exchange(payload):
                    stream.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
                    line = stream.readline()
                    if not line:
                        raise DesktopSyncError('connection_closed')
                    try:
                        value = json.loads(line)
                    except ValueError as error:
                        raise DesktopSyncError('protocol_error') from error
                    if not isinstance(value, dict):
                        raise DesktopSyncError('protocol_error')
                    return value

                auth = exchange({'token': token})
                if auth.get('ok') is not True:
                    raise DesktopSyncError('auth_failed')
                return exchange(message)
        except DesktopSyncError:
            raise
        except (OSError, ValueError) as error:
            raise DesktopSyncError('connect_failed') from error

    def _sync(self, item):
        message = {'op': 'sync', 'workspace': item['workspace'],
                   'sessionIds': sorted(item['sessions'])[:MAX_SESSIONS]}
        try:
            response = self._call(item['control'], message)
        except DesktopSyncError as error:
            self._set_state(error.code, error.code)
            return
        except Exception:
            self._set_state('sync_failed', 'sync_failed')
            return
        if response.get('ok') is not True:
            code = str(response.get('error') or 'sync_failed')
            known = {'workspace_not_allowed', 'host_not_ready', 'reconcile_unavailable',
                     'sync_failed', 'subscribe_failed', 'unknown_op', 'invalid_message',
                     'invalid_json', 'unauthorized'}
            self._set_state(code if code in known else 'sync_failed', code)
            return
        synced = response.get('synced') if isinstance(response.get('synced'), list) else []
        result = {
            'ok': True,
            'synced': len(synced),
            'changed': sum(1 for row in synced if isinstance(row, dict) and row.get('changed')),
            'missing': sum(1 for row in synced if isinstance(row, dict) and row.get('missing')),
            'errors': sum(1 for row in synced if isinstance(row, dict) and row.get('error')),
        }
        with self.lock:
            self.state = 'synced'
            self.reason = ''
            self.last_attempt_at = float(self.clock())
            self.last_sync_at = self.last_attempt_at
            self.last_result = result
            self.idle.notify_all()

    def _set_state(self, state, reason=''):
        with self.lock:
            self.state = str(state or 'sync_failed')
            self.reason = str(reason or '')
            self.last_attempt_at = float(self.clock())
            self.idle.notify_all()

    # ------------------------------------------------------------------ status
    def status(self):
        """A token-free, path-free status safe for a tool response."""
        cfg = self._settings()
        enabled = bool(cfg.get('desktop_sync_enabled', False))
        with self.lock:
            state = self.state
            if not enabled:
                state = 'disabled'
            elif state in ('disabled', ''):
                state = 'not_configured'
            return {
                'enabled': enabled,
                'state': state,
                'reason': self.reason if enabled else '',
                'pending_workspaces': len(self.pending),
                'last_attempt_at': self.last_attempt_at,
                'last_sync_at': self.last_sync_at,
                'last_result': copy.deepcopy(self.last_result),
                'truncated': bool(self.truncated),
            }

    def wait_idle(self, timeout=5.0):
        """Testing/daemon helper; waits for queued work to finish."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self.lock:
            while (self.pending or self.inflight) and time.monotonic() < deadline:
                self.idle.wait(min(0.1, max(0.01, deadline - time.monotonic())))
            return not self.pending and not self.inflight

    def stop(self, timeout=5.0):
        self.stop_event.set()
        self.wake.set()
        self.thread.join(timeout) if self.thread else None
