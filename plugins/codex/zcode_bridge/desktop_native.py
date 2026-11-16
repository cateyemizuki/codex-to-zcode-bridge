"""Optional native Desktop-owned transport client.

This is deliberately separate from :class:`desktop_sync.DesktopSyncAdapter`.
The legacy adapter asks a patched Desktop host to reconcile an existing
independent-engine session's index; this adapter creates and drives sessions
owned by the patched Desktop's own engine.

Safety boundaries:

* disabled unless the local settings enable it and name a private control file;
* only the loopback ``native_*`` operations below, only for workspaces admitted
  by the control file's blacklist or legacy exact allowlist;
* a missing/disabled host, stale owner or unsupported operation is an explicit
  error; it never falls back to the independent engine, opens or focuses a
  project, restarts an instance, or cold-resumes a foreign session;
* the control token stays in memory and never appears in a status, tool
  response, prompt or log;
* unknown mutation outcomes are returned as ``unknown`` and are never retried
  automatically by this client.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

from .desktop_sync import DesktopSyncAdapter, DesktopSyncError

CALLER_MODES = ('full_access', 'review_important_actions', 'ask_before_writes',
                'always_ask', 'read_only')
OPERATIONS = frozenset({
    'native_model_catalog', 'native_create', 'native_send', 'native_snapshot',
    'native_messages', 'native_events', 'native_workflows',
    'native_workflow_events', 'native_resolve', 'native_cancel',
    'native_pending', 'native_children', 'native_topology', 'native_lifecycle',
    'native_child_messages', 'native_child_snapshot',
})
MUTATIONS = frozenset({'native_model_catalog', 'native_create', 'native_send',
                       'native_resolve', 'native_cancel'})
UNSET = object()


class DesktopNativeError(RuntimeError):
    """A diagnosable native-transport refusal; safe to map to a tool error."""

    def __init__(self, code, detail='', request=None):
        self.code = str(code or 'native_failed')
        self.detail = str(detail or '')[:800]
        self.request = copy.deepcopy(request) if isinstance(request, dict) else None
        super().__init__(self.code + (': ' + self.detail if self.detail else ''))


class DesktopNativeAdapter:
    """Synchronous, explicit native transport client (no background worker)."""

    def __init__(self, settings_getter=None, clock=time.time):
        self.settings_getter = settings_getter or (lambda: {})
        self.clock = clock
        self._sync = DesktopSyncAdapter(settings_getter=settings_getter, clock=clock)
        self._lock = threading.RLock()
        self._last = {'op': '', 'state': '', 'at': None, 'session_id': ''}

    # ------------------------------------------------------------------ config
    def _settings(self):
        try:
            value = self.settings_getter()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def enabled(self):
        return bool(self._settings().get('desktop_native_enabled', False))

    def control_file(self):
        return str(self._settings().get('desktop_native_control_file', '') or '')

    def _control(self):
        if not self.enabled():
            raise DesktopNativeError('disabled', 'desktop native transport is disabled')
        path = self.control_file()
        if not path:
            raise DesktopNativeError('config_missing', 'desktop native control file is not configured')
        try:
            control = self._sync._load_control(path)
        except DesktopSyncError as error:
            raise DesktopNativeError(error.code, 'control file is not usable') from error
        native = control.get('native') or {}
        if native.get('enabled') is not True:
            raise DesktopNativeError('native_disabled', 'native transport is not enabled in the private control file')
        return control

    @staticmethod
    def _permission(value):
        if value not in CALLER_MODES:
            raise DesktopNativeError('caller_permission_required',
                                     'current caller_permission_mode is required')
        return str(value)

    def _workspace(self, control, workspace):
        try:
            resolved = control['policy'].resolve(str(Path(workspace).expanduser().absolute()))
        except (OSError, TypeError, ValueError) as error:
            raise DesktopNativeError('workspace_not_allowed', 'workspace is invalid') from error
        if not resolved or not Path(resolved).is_dir():
            raise DesktopNativeError('workspace_not_allowed', 'workspace is denied by the configured workspace policy')
        return resolved

    def _remember(self, op, state, session_id=''):
        with self._lock:
            self._last = {'op': str(op), 'state': str(state), 'at': float(self.clock()),
                          'session_id': str(session_id or '')}

    def _request(self, op, workspace, permission, fields=None, request_id=None):
        fields = dict(fields or {})
        message = {'op': op, 'workspace': workspace,
                   'caller_permission_mode': self._permission(permission)}
        if request_id is not None:
            message['request_id'] = str(request_id)
        message.update(fields)
        control = self._control()
        message['workspace'] = self._workspace(control, workspace)
        if op not in OPERATIONS:
            raise DesktopNativeError('unsupported', 'native operation is not allowlisted')
        try:
            response = self._sync._call(control, message)
        except DesktopSyncError as error:
            self._remember(op, error.code)
            raise DesktopNativeError(error.code, 'native control call failed') from error
        if not isinstance(response, dict):
            raise DesktopNativeError('protocol_error', 'native response is not an object')
        if response.get('ok') is not True:
            code = str(response.get('error') or 'native_failed')
            self._remember(op, code, response.get('request', {}).get('sessionId') if isinstance(response.get('request'), dict) else '')
            raise DesktopNativeError(code, str(response.get('detail') or ''),
                                      response.get('request'))
        result = response.get('result')
        self._remember(op, 'ok', result.get('sessionId') if isinstance(result, dict) else '')
        return copy.deepcopy(result)

    # --------------------------------------------------------------- operations
    def model_catalog(self, workspace, permission, request_id, model=None,
                      thought_level=UNSET):
        fields = {}
        if isinstance(model, dict):
            fields['model'] = {'providerId': model.get('providerId'),
                               'modelId': model.get('modelId')}
        if thought_level is not UNSET:
            fields['thought_level'] = thought_level
        return self._request('native_model_catalog', workspace, permission, fields,
                             request_id=request_id)

    def create(self, workspace, permission, model, thought_level, request_id,
               project_id=None):
        fields = {
            'model': {'providerId': model.get('providerId'),
                      'modelId': model.get('modelId')},
            'thought_level': thought_level,
        }
        if project_id is not None:
            fields['project_id'] = project_id
        return self._request('native_create', workspace, permission, fields,
                             request_id=request_id)

    def send(self, session_id, workspace, permission, text, request_id):
        return self._request('native_send', workspace, permission,
                             {'session_id': session_id, 'text': text},
                             request_id=request_id)

    def snapshot(self, session_id, workspace, permission):
        return self._request('native_snapshot', workspace, permission,
                             {'session_id': session_id})

    def messages(self, session_id, workspace, permission, after_message_id=None,
                 limit=100):
        fields = {'session_id': session_id, 'limit': max(1, min(1000, int(limit)))}
        if after_message_id:
            fields['after_message_id'] = str(after_message_id)
        return self._request('native_messages', workspace, permission, fields)

    def events(self, session_id, workspace, permission, after_seq=None, limit=100):
        fields = {'session_id': session_id, 'limit': max(1, min(1000, int(limit)))}
        if after_seq is not None:
            fields['after_seq'] = max(0, int(after_seq))
        return self._request('native_events', workspace, permission, fields)

    def workflows(self, session_id, workspace, permission, limit=16):
        return self._request('native_workflows', workspace, permission,
                             {'session_id': session_id,
                              'limit': max(1, min(64, int(limit)))})

    def workflow_events(self, session_id, workspace, permission, run_id,
                        after_sequence=None, limit=100):
        fields = {'session_id': session_id, 'run_id': run_id,
                  'limit': max(1, min(1000, int(limit)))}
        if after_sequence is not None:
            fields['after_sequence'] = max(0, int(after_sequence))
        return self._request('native_workflow_events', workspace, permission, fields)

    def children(self, session_id, workspace, permission, limit=30):
        return self._request('native_children', workspace, permission,
                             {'session_id': session_id,
                              'limit': max(1, min(100, int(limit)))})

    def child_messages(self, session_id, workspace, permission, child_session_id,
                       run_id=None, after_message_id=None, limit=100):
        """Read a verified child through the owned parent; never a resume.

        ``session_id`` is the owned parent.  The host verifies the actual
        parent/child native relationship (ordinary child or DWF actor + run)
        before any content is read.
        """
        fields = {'session_id': session_id,
                  'child_session_id': child_session_id,
                  'limit': max(1, min(1000, int(limit)))}
        if run_id:
            fields['run_id'] = str(run_id)
        if after_message_id:
            fields['after_message_id'] = str(after_message_id)
        return self._request('native_child_messages', workspace, permission, fields)

    def child_snapshot(self, session_id, workspace, permission, child_session_id,
                       run_id=None):
        fields = {'session_id': session_id,
                  'child_session_id': child_session_id}
        if run_id:
            fields['run_id'] = str(run_id)
        return self._request('native_child_snapshot', workspace, permission, fields)

    def topology(self, session_id, workspace, permission, run_id,
                 after_sequence=None, limit=100):
        fields = {'session_id': session_id, 'run_id': run_id,
                  'limit': max(1, min(1000, int(limit)))}
        if after_sequence is not None:
            fields['after_sequence'] = max(0, int(after_sequence))
        return self._request('native_topology', workspace, permission, fields)

    def resolve(self, session_id, workspace, permission, interaction_id, answer,
                request_id, user_approved=False):
        fields = {
            'session_id': session_id,
            'interaction_id': interaction_id,
            'answer': answer,
        }
        if user_approved is True:
            # The Host re-checks any allow-like permission choice against the
            # caller grant; this flag is the only explicit user-approval input.
            fields['user_approved'] = True
        return self._request('native_resolve', workspace, permission, fields,
                             request_id=request_id)

    def cancel(self, session_id, workspace, permission, request_id):
        return self._request('native_cancel', workspace, permission,
                             {'session_id': session_id}, request_id=request_id)

    def pending(self, workspace, permission, request_id):
        return self._request('native_pending', workspace, permission, {},
                             request_id=request_id)

    def lifecycle(self, session_id, workspace, permission):
        return self._request('native_lifecycle', workspace, permission,
                             {'session_id': session_id})

    # ------------------------------------------------------------------ status
    def status(self):
        """Token-free, path-free status safe for a tool response."""
        settings = self._settings()
        enabled = bool(settings.get('desktop_native_enabled', False))
        control = self.control_file()
        result = {'enabled': enabled, 'configured': bool(control),
                  'state': 'disabled', 'reason': '', 'native_enabled': None}
        if not enabled:
            result.update(state='disabled', reason='setting_disabled')
            with self._lock:
                result['last'] = copy.deepcopy(self._last)
            return result
        if not control:
            result.update(state='not_configured', reason='control_file_empty')
            with self._lock:
                result['last'] = copy.deepcopy(self._last)
            return result
        try:
            loaded = json.loads(Path(control).expanduser().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            result.update(state='config_missing', reason='control_file_unreadable')
        else:
            native = loaded.get('nativeTransport') if isinstance(loaded, dict) else None
            native_enabled = bool(isinstance(native, dict) and native.get('enabled') is True)
            result['native_enabled'] = native_enabled
            if not native_enabled:
                result.update(state='native_disabled', reason='opted_out')
            else:
                result.update(state='ready', reason='')
        with self._lock:
            result['last'] = copy.deepcopy(self._last)
        return result
