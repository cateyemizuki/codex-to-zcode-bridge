"""Own one live protocol transport so question and permission replies reach it."""
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .protocol import ProtocolError
from .desktop import session_visibility
from .storage import (config, configured_default_workspace, defaults, read_json,
                      save_config, state_dir, write_json)
from .workflows import topology
from .identity import (is_visible_message, message_activity_digest,
                       message_identity)
from .notifications import (NotificationStore, Observer, has_anchor_proof,
                            make_input_id, subscription_anchor, TERMINAL_STATES,
                            _run_state as workflow_run_state)
from .delivery import DeliveryWorker
from .host_wake import HostWakeAdapter
from .desktop_sync import DesktopSyncAdapter
from .desktop_native import DesktopNativeAdapter, DesktopNativeError
from .desktop_instance import ensure_desktop, inspect_desktop, DesktopInstanceError
from .superagent import (ACTIVE_STATUSES, MAX_MESSAGE_CHARS, MAX_QUEUE_ITEMS,
                         TERMINAL_JOB_STATUSES, SuperagentError, SuperagentStore,
                         bounded_report_payload, build_task_prompt, resolve_profile,
                         resolve_workspace)
from . import __version__

PERMISSIONS = {
    'full_access': 'yolo', 'review_important_actions': 'edit',
    'ask_before_writes': 'build', 'always_ask': 'build', 'read_only': 'plan',
}

NOTIFY_SOURCE_TOOLS = frozenset({
    'zcode_notify_subscribe', 'zcode_notify_cancel', 'zcode_notify_inbox',
    'zcode_notify_ack', 'zcode_notify_status', 'zcode_wait', 'zcode_stop',
})

NOTIFY_KINDS = {'progress': 'hint.progress', 'action_required': 'hint.action_required',
                'needs_verification': 'hint.needs_verification'}
NOTIFY_REASONS = {'needs_input', 'blocked', 'progress', 'verification_requested'}
AGENT_TERMINAL_STATUSES = TERMINAL_JOB_STATUSES

# An engine process can outlive a session runtime.  Only these idempotent
# reads may be replayed after re-attaching the same session natively; sends,
# mode changes, stops and answers must surface the original failure unchanged.
READ_ONLY_SESSION_RPCS = frozenset({'session/read', 'session/messages', 'session/events'})

# Native 3.18.2 permission interactions (see nD/Lk schemas): options carry
# an optionId plus kind allowOnce|allowAlways|deny|custom, and the optional
# fullAccessOption always has optionId "fullAccess".  The native engine maps
# that choice to yolo, so it is a real mode escalation, not a plain approval.
NATIVE_FULL_ACCESS_OPTION = 'fullAccess'
NATIVE_DENY_CHOICES = frozenset({'decline', 'cancel'})


class SendNotStarted(RuntimeError):
    """The request was definitely never handed to the engine.

    Raised for failures in local pre-send steps (permission inheritance)
    between persisting a queue claim and the real ``session/send`` RPC.  The
    caller must record a definite non-delivery, not an unknown outcome.
    """


def is_session_not_found_error(error, sid):
    """The engine's -32004 reply when a session row is not persisted yet."""
    if isinstance(error, DesktopNativeError):
        # The native adapter preserves the numeric native code as a string.
        # Match the exact row lookup failure, never runtimeNotReady or another SID.
        return (error.code == '-32004' and isinstance(sid, str) and bool(sid)
                and error.detail == f'Session not found: {sid}')
    payload = getattr(error, 'error', None)
    return (isinstance(payload, dict) and payload.get('code') == -32004
            and isinstance(sid, str) and bool(sid)
            and str(payload.get('message') or '').strip() == f'Session not found: {sid}')


def is_request_schema_rejection(error):
    """A definite JSON-RPC rejection before the engine admitted the input.

    3.18.2 validates ``session/send`` with an exact Zod object and replies
    ``-32602 unrecognized_keys`` for any extra key.  That is a known refusal,
    not an unknown transport outcome, so callers must not mark it uncertain or
    invite a blind resend.
    """
    payload = getattr(error, 'error', None)
    if not isinstance(payload, dict):
        return False
    code = payload.get('code')
    if code in (-32600, -32602):
        return True
    data = payload.get('data')
    return isinstance(data, dict) and str(data.get('name') or '') == 'ZodError'


def is_stale_session_error(error, sid):
    """Match only the two observed -32004 shapes that prove a stale runtime."""
    payload = getattr(error, 'error', None)
    if not isinstance(payload, dict) or payload.get('code') != -32004:
        return False
    if not isinstance(sid, str) or not sid:
        return False
    data = payload.get('data')
    return ((isinstance(data, dict) and data.get('code') == 'runtimeNotReady')
            or is_session_not_found_error(error, sid))


def caller_mode(arguments):
    permission = arguments.get('caller_permission_mode')
    if permission not in PERMISSIONS:
        raise ValueError('必须提供 GPT 当前会话的 caller_permission_mode；无法确认时使用 always_ask')
    return PERMISSIONS[permission]


def caller_permission(arguments):
    """Validate the actual current permission without changing ZCode settings."""
    permission = arguments.get('caller_permission_mode')
    if permission not in PERMISSIONS:
        raise ValueError('必须提供 GPT 当前会话的 caller_permission_mode；无法确认时使用 always_ask')
    return permission


def source_from_context(context):
    """Return the immutable request source from the daemon context.  Never reads env."""
    if not isinstance(context, dict):
        return None
    thread = context.get('thread_id')
    if not isinstance(thread, str) or not thread.strip():
        return None
    return {
        'thread_id': thread,
        'thread_origin': str(context.get('thread_origin') or ''),
        'request_id': str(context.get('request_id') or ''),
        'client_name': str(context.get('client_name') or ''),
        'client_version': str(context.get('client_version') or ''),
        'host_id': str(context.get('host_id') or ''),
    }


# Backward-compatible name for older internal callers/tests.
source_from_meta = source_from_context


def host_from_context(context):
    """Return the local host-launch context; never includes message content."""
    if not isinstance(context, dict):
        return {}
    host = {}
    for source_key, target_key in (('host_pipe', 'pipe'),
                                   ('host_runtime_cli', 'runtime_cli'),
                                   ('host_node', 'node'),
                                   ('host_id', 'host_id')):
        value = context.get(source_key)
        if isinstance(value, str) and value:
            host[target_key] = value
    return host


def require_source(context):
    source = source_from_context(context)
    if source is None:
        raise ValueError('source_unavailable: 通知工具必须由当前 chat 的 MCP 请求 context 提供 thread')
    return source


def compact_messages(messages, limit_chars=24000):
    result, used = [], 0
    for message in messages:
        info = message.get('info', {})
        if not is_visible_message(message):
            continue
        parts = []
        for part in message.get('parts', []):
            if part.get('synthetic'):
                continue
            kind = part.get('type', '')
            if kind in ('text', 'reasoning'):
                if kind == 'reasoning':
                    continue
                text = str(part.get('text', part.get('content', '')))
                take = max(0, limit_chars-used)
                parts.append({'type': kind, 'text': text[:take], 'truncated': len(text) > take})
                used += min(take, len(text))
            elif kind in ('tool', 'tool-invocation', 'tool_call'):
                state = part.get('state', {})
                tool_name = part.get('tool', part.get('toolName'))
                tool = {'type':'tool','name':tool_name, 'status':state.get('status') if isinstance(state, dict) else state}
                if isinstance(state, dict):
                    if state.get('error'):
                        tool['error'] = str(state['error'])[:2000]
                    if tool_name == 'submit_result':
                        value = json.dumps(state.get('input', {}).get('result'), ensure_ascii=False)
                        take = max(0, limit_chars-used)
                        tool.update(result=value[:take], truncated=len(value)>take)
                        used += min(take, len(value))
                parts.append(tool)
        result.append({'id': message_identity(message), 'role': info.get('role', message.get('role')),
                       'parts': parts})
    return result


def desktop_running(executable):
    if os.name != 'nt':
        return False
    # Process inspection only; no UI automation, command construction or shell.
    query = "Get-CimInstance Win32_Process -Filter \"Name='ZCode.exe'\" | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    proc = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', query],
        capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        rows = json.loads(proc.stdout.decode('utf-8-sig') or '[]')
        rows = [rows] if isinstance(rows, dict) else rows
        expected = os.path.normcase(os.path.abspath(executable))
        return any(row.get('ExecutablePath') and
            os.path.normcase(os.path.abspath(row['ExecutablePath'])) == expected and
            '--type=' not in (row.get('CommandLine') or '') and
            'app-server' not in (row.get('CommandLine') or '') for row in rows)
    except (ValueError, TypeError):
        return False


class ObserverRuntimeAdapter:
    """Exact runtime interface consumed by notifications.Observer.

    ``Bridge.pending`` is the live interaction-request dict, so the Observer's
    ``pending()`` method cannot live on Bridge without being shadowed.  This
    adapter is the single Observer-facing surface; the ``observe_*`` methods on
    Bridge stay as the transport-specific implementations.
    """

    def __init__(self, bridge):
        self.bridge = bridge

    def engine_identity(self, sid=None):
        return self.bridge.engine_identity(sid)

    def pending(self, sid=None):
        if sid and self.bridge._is_native(sid):
            return self.bridge.native_pending_for_sid(sid)
        return self.bridge.pending_snapshot()

    def ensure_session(self, sid, deadline=None):
        return self.bridge.ensure_session(sid, deadline)

    def snapshot(self, sid, deadline=None):
        return self.bridge.snapshot(sid, deadline)

    def jobs(self, snapshot, sid=None, deadline=None):
        return self.bridge.observe_jobs(snapshot, sid, deadline)

    def workflows(self, sid, deadline=None):
        return self.bridge.observe_workflows(sid, deadline)

    def children(self, sid, deadline=None):
        return self.bridge.observe_children(sid, deadline)

    def messages(self, sid, after_message_id=None, deadline=None):
        return self.bridge.observe_messages(sid, after_message_id, deadline)

    def session_events(self, sid, after_seq=0, deadline=None):
        return self.bridge.observe_events(sid, after_seq, deadline)

    def stream_id(self, sid, deadline=None):
        return self.bridge.observe_stream_id(sid, deadline)

    def run_topology(self, sid, run_id, deadline=None):
        return self.bridge.observe_run_topology(sid, run_id, deadline)

    def drain_queues(self, now=None):
        return self.bridge.drain_superagent_queues(now)

    def sync_jobs(self, now=None):
        return self.bridge.sync_superagent_jobs(now)


class Bridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.client_lock = threading.Lock()
        self.recovery_lock = threading.Lock()
        self.client = None
        self.pending = {}
        self.answered = {}
        self.events = deque(maxlen=4000)
        self.seq = 0
        self.sessions = read_json(state_dir() / 'sessions.json', {}) or {}
        self.resident = set()
        # Sessions created by this Bridge generation.  A persisted session that
        # was merely resumed must never qualify for the first-send exception.
        self.created = set()
        self.model_catalog = {}
        self.generation = 0
        self.capabilities_generation = 0
        self.notifications = NotificationStore(state_dir() / 'notifications.json')
        self.superagent = SuperagentStore()
        self.superagent_drain_lock = threading.Lock()
        self._subtree_discovery_at = {}
        self.desktop_sync = DesktopSyncAdapter(settings_getter=config)
        self.desktop_native = DesktopNativeAdapter(settings_getter=config, ensure_desktop=self._ensure_native_desktop)
        self.observer = None
        self.delivery = None
        # The real adapter is created when the daemon starts; tests can inject
        # a fake waker before start_background without touching a real host.
        self.host_waker = None
        self._host_context = {}
        self._last_probe_at = 0.0
        self._native_connection_proof = None

    # --------------------------------------------------------------- background
    def notification_settings(self):
        try:
            cfg = config()
        except Exception:
            cfg = defaults()
        return {
            'poll_seconds': int(cfg.get('completion_notify_poll_seconds', 1) or 1),
            'start_timeout': int(cfg.get('completion_notify_start_timeout_seconds', 120) or 120),
            'expiry_hours': int(cfg.get('completion_notify_expiry_hours', 6) or 6),
            'wake_attempts': int(cfg.get('completion_notify_wake_attempts', 5) or 5),
        }

    def host_context(self):
        with self.lock:
            return copy.deepcopy(self._host_context)

    def start_background(self):
        """Start the daemon-only observer and delivery worker.  No engine RPC here."""
        settings = self.notification_settings()
        if self.host_waker is None:
            # Start from the OS environment for process launch, but strip the
            # daemon's stale thread/host capability.  The real values arrive in
            # the per-request context and are stored per subscription.
            base_env = {key: value for key, value in os.environ.items()
                        if key not in ('CODEX_THREAD_ID', 'CODEX_APP_TOOLS_PIPE_PATH',
                                       'CODEX_APP_RUNTIME_CLI_PATH', 'CODEX_MCP_NODE_PATH')}
            self.host_waker = HostWakeAdapter(env=base_env)
        if self.notifications.all_events() or self.notifications.all_subscriptions():
            # Capability is a process cache, not persisted truth.
            try:
                self.notifications.set_host_capability('not_probed', None)
            except Exception:
                pass
        if self.delivery is None:
            def auto_continue(event):
                sub = self.notifications.find_subscription(event.get('subscription_id'))
                consent = (sub or {}).get('consent') or {}
                return bool(consent.get('auto_continue') and consent.get('user_authorized'))
            self.delivery = DeliveryWorker(
                self.notifications, waker=self.host_waker,
                host_context=self.host_context,
                poll_seconds=settings['poll_seconds'],
                max_attempts=settings['wake_attempts'],
                auto_continue_lookup=auto_continue)
            self.delivery.start()
        if self.observer is None:
            self.observer = Observer(
                self.notifications, ObserverRuntimeAdapter(self), poll_seconds=settings['poll_seconds'],
                pending_ttl=settings['expiry_hours'] * 3600)
            self.observer.start()

    def stop_background(self):
        if self.observer:
            self.observer.stop()
            self.observer = None
        if self.delivery:
            self.delivery.stop()
            self.delivery = None
        self.desktop_sync.stop()

    def _save_sessions(self):
        with self.lock:
            write_json(state_dir() / 'sessions.json', self.sessions)

    def model_ref(self, model, thought_level=None, workspace=None):
        if not isinstance(model, dict) or not model.get('providerId') or not model.get('modelId'):
            raise ValueError('尚未选择默认模型；先读取 zcode_models 并在设置中选择，或显式传入 model')
        reference = copy.deepcopy(model)
        level = thought_level or reference.get('options', {}).get('reasoningLevel')
        model_default = level == 'auto'
        if model_default:
            level = None
        if not level:
            cfg = config()
            key = (model['providerId'], model['modelId'])
            default = cfg['default_model']
            if not model_default and key == (default['providerId'], default['modelId']) and cfg['default_thought_level'] != 'auto':
                level = cfg['default_thought_level']
            else:
                if key not in self.model_catalog:
                    self.call('zcode_models', {'workspace':workspace or cfg['default_workspace']})
                option = self.model_catalog.get(key)
                if option is None:
                    raise ValueError('指定模型不存在于 ZCode 的当前配置中')
                reasoning = option.get('reasoning', {})
                levels = reasoning.get('levels', [])
                level = reasoning.get('defaultLevel') or (levels[0]['value'] if levels else None)
        if level:
            reference['options'] = {**reference.get('options', {}), 'reasoningLevel':level}
        return reference

    def _event(self, event):
        with self.lock:
            self.seq += 1
            self.events.append({'cursor': self.seq, 'event': event})

    def _request(self, request):
        key = str(request.get('params', {}).get('requestId') or request['id'])
        params = request.get('params', {})
        fingerprint = hashlib.sha256(json.dumps({k: params.get(k) for k in
            ('sessionId', 'toolCallId', 'toolName', 'input')}, sort_keys=True).encode()).hexdigest()
        with self.lock:
            answered = self.answered.get(key)
            if answered and answered['fingerprint'] == fingerprint and time.time()-answered['at'] < 3600:
                self.client.respond(request['id'], answered['response'])
                return
            old = self.pending.get(key, {})
            self.pending[key] = {**request, 'id': key, 'wire_id': request['id'], 'fingerprint': fingerprint,
                'received_at': old.get('received_at', time.time()), 'last_announced_at': time.time()}
        self._event({'method': 'bridge/interaction', 'params': {
            'request_id': key, 'method': request['method']}})

    def launch(self, force=False, workspace=None, allow_focus=False):
        cfg = config()
        if workspace is not None:
            if not allow_focus:
                raise ValueError('打开桌面工作区会使 ZCode 获得焦点；仅在用户明确要求时设置 allow_focus=true')
            workspace = Path(workspace).resolve()
            if not workspace.is_dir():
                raise ValueError('指定工作区不存在')
        try:
            result = ensure_desktop(cfg['zcode_executable'],
                                    allow_start=bool(force or cfg.get('auto_launch_desktop')))
        except DesktopInstanceError as error:
            raise DesktopNativeError(str(error), '无法确认唯一正式 ZCode Desktop；未启动其他实例') from error
        return {**result, 'workspace_open_requested': False, 'may_focus': False,
                'needs_native_ui': workspace is not None,
                'note': '复用唯一桌面进程；切换可见项目请在 ZCode 内操作' if workspace is not None else ''}

    def backend(self, deadline=None):
        raise DesktopNativeError('independent_engine_disabled',
                                 '仅支持唯一已安装 ZCode Desktop 的原生会话；不会启动独立引擎或副本')

    def _ensure_native_desktop(self, allow_start=False):
        cfg = config()
        signature = (cfg['zcode_executable'], cfg.get('desktop_native_control_file'))
        proof = self._native_connection_proof
        if (not allow_start and proof and proof['signature'] == signature
                and time.monotonic() - proof['at'] < 1):
            return copy.deepcopy(proof['state'])
        try:
            state = ensure_desktop(cfg['zcode_executable'],
                                   allow_start=allow_start and bool(cfg.get('auto_launch_desktop')))
        except DesktopInstanceError as error:
            raise DesktopNativeError(str(error), '请使用已配置的唯一正式 ZCode Desktop 实例') from error
        until = time.monotonic() + (12 if state.get('launched') else 0)
        while True:
            probe = self.desktop_native.probe()
            if probe.get('ok') and probe.get('ready') and probe.get('nativeAvailable'):
                executable = str(probe.get('desktopExecutable') or '')
                if not executable:
                    raise DesktopNativeError('desktop_host_upgrade_required', '桌面接入缺少可执行文件身份信息；请加载新版接入')
                if os.path.normcase(str(Path(executable).resolve())) != os.path.normcase(str(Path(cfg['zcode_executable']).resolve())):
                    raise DesktopNativeError('desktop_host_mismatch', '控制文件对应另一个 ZCode 实例')
                self._native_connection_proof = {'signature': signature, 'at': time.monotonic(),
                                                 'state': copy.deepcopy(state)}
                return state
            if time.monotonic() >= until:
                raise DesktopNativeError('native_host_not_ready', '请完成桌面原生接入并重载 ZCode；不会启动独立引擎')
            time.sleep(.2)

    @staticmethod
    def _select_transport(value=None):
        if value not in (None, '', 'desktop_native'):
            raise DesktopNativeError('independent_engine_disabled', '仅支持 desktop_native；旧独立引擎后端已禁用')
        return 'desktop_native'

    def _desktop_status(self):
        cfg = config()
        error = ''
        try:
            desktop = inspect_desktop(cfg['zcode_executable'])
        except DesktopInstanceError as failure:
            desktop, error = {'running': False, 'pid': None}, str(failure)
        native = self.desktop_native.status()
        probe = self.desktop_native.probe() if desktop['running'] else {}
        workflow_policy = probe.get('workflowAuthorization') or {}
        workflow_mode_valid = workflow_policy.get('mode') in ('automatic', 'manual')
        native['workflow_authorization'] = {
            'configured_mode': cfg.get('workflow_authorization_mode', 'automatic'),
            'mode': workflow_policy.get('mode', 'unavailable'),
            'running': workflow_policy.get('running') is True,
            'state': ('configuration_unavailable' if workflow_policy.get('running') is True and not workflow_mode_valid else
                      'ready' if workflow_policy.get('running') is True else
                      workflow_policy.get('error') or ('host_update_required' if desktop['running'] else 'desktop_not_running')),
        }
        connected = bool(probe.get('ok') and probe.get('ready') and probe.get('nativeAvailable')
                         and probe.get('desktopExecutable')
                         and os.path.normcase(str(Path(probe['desktopExecutable']).resolve()))
                         == os.path.normcase(str(Path(cfg['zcode_executable']).resolve())))
        details = []
        for sid, data in self.sessions.items():
            row = {'session_id': sid, **copy.deepcopy(data)}
            row['usable'] = data.get('transport') == 'desktop_native'
            row['desktop'] = {'connection': 'desktop_native' if row['usable'] else 'legacy_history',
                              'live_updates': row['usable'], 'gui_verified': False}
            details.append(row)
        return {'connected': connected, 'bridge_version': __version__, 'transport': 'desktop_native',
                'independent_engine_enabled': False, 'engine_pid': None,
                'desktop_running': desktop['running'], 'desktop_pid': desktop['pid'],
                'desktop_error': error, 'native_transport': native,
                'managed_sessions': list(self.sessions), 'managed_session_details': details,
                'pending_count': len(self.pending), 'event_cursor': self.seq,
                'notifications': self.notifications.aggregate()}

    def _desktop_models(self, args):
        cfg = config()
        workspace = str(Path(args.get('workspace') or cfg['default_workspace']).expanduser().resolve())
        if not Path(workspace).is_dir():
            raise ValueError('工作区不存在')
        model = cfg.get('default_model') or {}
        model = model if model.get('providerId') and model.get('modelId') else None
        effort = cfg.get('default_thought_level')
        request = {'model': model}
        if effort and effort != 'auto' and model:
            request['thought_level'] = effort
        result = self.desktop_native.model_catalog(workspace, 'read_only',
                                                  'catalog_' + uuid.uuid4().hex, **request)
        self.model_catalog = {(m['ref']['providerId'], m['ref']['modelId']): copy.deepcopy(m)
                              for m in result.get('available', [])}
        return {'available': result.get('available', []), 'current': result.get('selected'),
                'lastUsed': result.get('selected'), 'transport': 'desktop_native'}

    def _remaining(self, deadline, cap=40):
        if deadline is None:
            return cap
        return max(.5, min(cap, deadline - time.monotonic()))

    def _session_info(self, sid):
        with self.lock:
            return copy.deepcopy(self.sessions.get(sid) or {})

    def _transport_for(self, sid):
        if not sid:
            return 'engine'
        return str(self._session_info(sid).get('transport') or 'engine')

    def _is_native(self, sid):
        return self._transport_for(sid) == 'desktop_native'

    def _remember_native_children(self, parent_sid, child_ids, run_id=None):
        """Bind verified native children to the same read-only native route.

        The host remains the final verifier: this record only says which owned
        parent (and, for a DWF actor, which run) a read is routed through.  No
        child gets write ownership, resume or independent-engine fallback.
        """
        if not child_ids:
            return
        parent = self._session_info(parent_sid)
        if parent.get('transport') != 'desktop_native':
            return
        run_id = str(run_id or '')
        changed = False
        with self.lock:
            for child_id in child_ids:
                if not isinstance(child_id, str) or not child_id:
                    continue
                existing = self.sessions.get(child_id)
                if existing is not None:
                    if (existing.get('read_only_child')
                            and existing.get('parent_session_id') == parent_sid
                            and run_id and not existing.get('native_run_id')):
                        existing['native_run_id'] = run_id
                        changed = True
                    continue
                self.sessions[child_id] = {
                    'workspace': parent.get('workspace'),
                    'transport': 'desktop_native',
                    'read_only_child': True,
                    'parent_session_id': parent_sid,
                    'native_run_id': run_id,
                    'caller_permission_mode': parent.get('caller_permission_mode') or 'always_ask',
                    'native_runtime': copy.deepcopy(parent.get('native_runtime') or {}),
                    'native_source': parent.get('native_source') or 'desktop-native-host',
                    'created_at': time.time(),
                }
                changed = True
        if changed:
            self._save_sessions()

    def _native_context(self, sid):
        info = self._session_info(sid)
        workspace = info.get('workspace')
        if not isinstance(workspace, str) or not workspace:
            raise DesktopNativeError('native_session_unowned',
                                     '会话没有绑定到 Desktop 原生后端')
        permission = info.get('caller_permission_mode') or 'always_ask'
        if permission not in ('full_access', 'review_important_actions',
                              'ask_before_writes', 'always_ask', 'read_only'):
            permission = 'always_ask'
        return workspace, permission

    @staticmethod
    def _normalize_native_ack(result):
        """Admission from the real native ACK shapes, never guessed.

        First input: ``status='accepted'`` plus ``nativeAdmission.accepted=true``.
        Later V4: ``status='accepted'`` with a native inputAccepted result.
        Replay/pending/unknown/duplicate/noop are not an accepted admission for
        this purpose; callers must not resend them automatically.
        """
        value = dict(result) if isinstance(result, dict) else {}
        status = str(value.get('status') or '').lower()
        admission = value.get('nativeAdmission')
        admitted = (status == 'accepted'
                    or bool(isinstance(admission, dict)
                            and admission.get('accepted') is True))
        if admitted:
            value['send_state'] = 'accepted'
        elif value.get('replay') or status in ('pending', 'unknown', 'queued',
                                               'dispatching', 'duplicate', 'noop', ''):
            value['send_state'] = 'uncertain'
        else:
            value['send_state'] = 'uncertain'
        value['accepted'] = admitted
        return value

    def _native_message_page(self, read, params):
        """Add pagination evidence to Et's forward-only message array.

        Native cPi/X4c applies the visible transcript filter before its forward
        limit. One extra row therefore proves another page without resuming a
        runtime or treating a full array as the end of the transcript.
        """
        limit = min(1000, max(1, int(params.get('limit', 20) or 20)))
        after = params.get('afterMessageId')

        def fetch(cursor, count):
            rows = read(after_message_id=cursor, limit=count)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise DesktopNativeError('native_messages_shape_invalid',
                                         'native messages must be an array of message objects')
            return rows

        rows = fetch(after, min(1000, limit + 1))
        more = len(rows) > limit
        rows = rows[:limit]
        cursor = next((message_identity(row) for row in reversed(rows)
                       if is_visible_message(row) and message_identity(row)), after)
        if rows and limit == 1000 and len(rows) == limit:
            if not cursor or cursor == after:
                raise DesktopNativeError('native_messages_cursor_unavailable',
                                         'native forward page has no advancing message cursor')
            more = bool(fetch(cursor, 1))
        if more and (not cursor or cursor == after):
            raise DesktopNativeError('native_messages_cursor_unavailable',
                                     'native forward page has no advancing message cursor')
        return {'messages': rows, 'hasMore': more, 'afterMessageId': cursor}

    def _native_child_rpc(self, method, params, info):
        """Read a verified native child only through its owned parent + run.

        The host re-verifies the native relationship on every call.  Anything
        other than the two fixed read interfaces is refused here; a child never
        receives send/stop/answer/model/permission writes, and an unavailable
        child snapshot is not restored or routed to an independent engine.
        """
        sid = str(params.get('sessionId') or '')
        parent_sid = str(info.get('parent_session_id') or '')
        workspace = info.get('workspace')
        permission = info.get('caller_permission_mode') or 'always_ask'
        if permission not in PERMISSIONS:
            permission = 'always_ask'
        run_id = str(info.get('native_run_id') or '') or None
        if not parent_sid or not isinstance(workspace, str) or not workspace:
            raise DesktopNativeError(
                'native_child_unbound',
                'child session is not bound to an owned native parent')
        if method == 'session/messages':
            return self._native_message_page(
                lambda **page: self.desktop_native.child_messages(
                    parent_sid, workspace, permission, sid, run_id=run_id, **page), params)
        if method == 'session/read':
            return self.desktop_native.child_snapshot(
                parent_sid, workspace, permission, sid, run_id=run_id)
        raise DesktopNativeError(
            'child_read_only',
            'native child sessions expose only message/snapshot reads; no send, '
            'stop, answer, model or permission write is available')

    def _native_rpc(self, method, params, deadline=None):
        """Map the engine-shaped calls used by the bridge onto native operations.

        A session bound to ``desktop_native`` never touches the independent
        engine. Unsupported methods fail explicitly; they do not fall back.
        """
        sid = str(params.get('sessionId') or '')
        info = self._session_info(sid)
        if info.get('read_only_child'):
            return self._native_child_rpc(method, params, info)
        workspace, permission = self._native_context(sid)
        native = self.desktop_native
        if method == 'session/read':
            return native.snapshot(sid, workspace, permission)
        if method == 'session/messages':
            return self._native_message_page(
                lambda **page: native.messages(sid, workspace, permission, **page), params)
        if method == 'session/events':
            rows = native.events(sid, workspace, permission,
                                 after_seq=params.get('afterSeq'),
                                 limit=int(params.get('limit', 50) or 50))
            cursor = rows[-1].get('seq') if rows and isinstance(rows[-1], dict) else 0
            return {'events': rows, 'more': False, 'cursor': cursor or 0}
        if method == 'session/send':
            input_id = str(params.get('inputId') or uuid.uuid4().hex)
            current = params.get('caller_permission_mode')
            if current not in PERMISSIONS:
                # Every native send carries this turn's real grant (or the
                # subscription's permission_at_arm).  Never fall back to a
                # stored session mode that may be an older full_access.
                raise DesktopNativeError(
                    'caller_permission_required',
                    'native send requires the current caller_permission_mode; '
                    'a stored session mode is not used')
            return self._normalize_native_ack(native.send(
                sid, workspace, current, str(params.get('content') or ''),
                request_id='send_' + input_id))
        if method == 'session/stop':
            current = params.get('caller_permission_mode') or permission
            return native.cancel(sid, workspace, current,
                                 request_id='cancel_' + uuid.uuid4().hex)
        if method == 'session/subagents':
            result = native.children(sid, workspace, permission,
                                     limit=int(params.get('endedLimit', 30) or 30))
            native_rows = result.get('native') if isinstance(result, dict) else {}
            child_ids = []
            if isinstance(native_rows, dict):
                for value in native_rows.get('childSessionIds') or []:
                    if isinstance(value, str) and value and value not in child_ids:
                        child_ids.append(value)
                rows = list(native_rows.get('running') or [])
                ended = native_rows.get('ended')
                if isinstance(ended, dict):
                    rows.extend(ended.get('items') or [])
                elif isinstance(ended, list):
                    rows.extend(ended)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    child_id = row.get('childSessionId') or row.get('sessionId')
                    if isinstance(child_id, str) and child_id and child_id not in child_ids:
                        child_ids.append(child_id)
            self._remember_native_children(sid, child_ids)
            return {'childSessionIds': child_ids}
        if method == 'v4/conversation/workflowRuns':
            result = native.workflows(sid, workspace, permission,
                                      limit=int(params.get('limit', 16) or 16))
            if isinstance(result, list):
                return {'runs': result}
            return result if isinstance(result, dict) else {'runs': []}
        if method == 'v4/conversation/workflowRunEvents':
            result = native.workflow_events(
                sid, workspace, permission, str(params.get('runId') or ''),
                after_sequence=params.get('afterSequence'),
                limit=int(params.get('limit', 100) or 100))
            if isinstance(result, dict):
                return {'events': result.get('events') or [],
                        'hasMore': bool(result.get('hasMore'))}
            return {'events': result if isinstance(result, list) else [],
                    'hasMore': False}
        if method in ('v4/conversation/workflowRunNodeResult',
                      'v4/conversation/workflowRunWorkspace',
                      'session/setMode', 'session/setModel', 'session/setThoughtLevel',
                      'session/resume', 'session/close', 'session/cancelBackgroundTask'):
            raise DesktopNativeError('needs_native_ui', method)
        raise DesktopNativeError('needs_native_ui', 'native transport does not expose ' + method)

    def rpc(self, method, params=None, deadline=None):
        params = params if isinstance(params, dict) else {}
        sid = params.get('sessionId')
        if sid and self._is_native(sid):
            return self._native_rpc(method, params, deadline)
        try:
            return self._rpc_once(method, params, deadline)
        except ProtocolError as error:
            sid = params.get('sessionId')
            if method not in READ_ONLY_SESSION_RPCS or not is_stale_session_error(error, sid):
                raise
            # Only a session this Bridge already loaded/created in the current
            # engine generation may be re-attached.  An unknown or never
            # resumed ID must not turn a plain read into a hidden resume.
            if not self._is_resident(sid):
                raise
            try:
                # Never hold self.lock across a request: the transport reader
                # calls back into _request on the same engine connection.
                with self.recovery_lock:
                    self._forget_resident(sid)
                    self.ensure_session(sid, deadline)
                return self._rpc_once(method, params, deadline)
            except Exception as recovery_error:
                raise error from recovery_error

    def _rpc_once(self, method, params, deadline):
        return self.backend(deadline).request(method, params, timeout=self._remaining(deadline))

    def _is_resident(self, sid):
        with self.lock:
            return sid in self.resident

    def _mark_resident(self, sid):
        with self.lock:
            self.resident.add(sid)

    def _forget_resident(self, sid):
        with self.lock:
            self.resident.discard(sid)

    def _mark_created(self, sid):
        with self.lock:
            self.created.add(sid)

    def _is_created(self, sid):
        with self.lock:
            return sid in self.created

    def _resume_session(self, sid, deadline=None):
        params = {'sessionId': sid}
        if sid in self.sessions:
            workflow_mode = self.sessions[sid].get('workflow_mode', config()['default_workflow_mode'])
            params.update(dynamicWorkflowEnabled=workflow_mode != 'disabled', dynamicWorkflowMode=workflow_mode)
        return self.rpc('session/resume', params, deadline)

    def ensure_session(self, sid, deadline=None):
        if not isinstance(sid, str) or not sid:
            raise ValueError('会话 ID 无效')
        if self._is_native(sid):
            # Native sessions are owned by the patched Desktop host.  There is
            # no independent-engine resume path and no cold foreign recovery.
            if not self._session_info(sid).get('workspace'):
                raise DesktopNativeError('native_session_unowned',
                                         '会话没有绑定到 Desktop 原生后端')
            return None
        if not sid.startswith('sess_'):
            raise ValueError('会话 ID 无效')
        self.backend(deadline)
        if not self._is_resident(sid):
            snapshot = self._resume_session(sid, deadline)
            self._mark_resident(sid)
            return snapshot
        return None

    def inherit(self, sid, args, deadline=None):
        if self._is_native(sid):
            raise DesktopNativeError('needs_native_ui',
                                     '原生后端不支持通过桥接修改会话权限或模型')
        mode = caller_mode(args)
        self.ensure_session(sid, deadline)
        if mode == 'plan':
            # In 0.17, plan is an overlay, not a permission mode. Clear a
            # previous yolo/edit permission before enabling that overlay.
            self.rpc('session/setMode', {'sessionId':sid, 'mode':'build'}, deadline)
        result = self.rpc('session/setMode', {'sessionId': sid, 'mode': mode}, deadline)
        expected = 'build' if mode == 'plan' else mode
        if result.get('settings', {}).get('permission', {}).get('mode') != expected:
            raise RuntimeError('ZCode 未应用指定权限，已停止后续发送')
        with self.lock:
            self.sessions.setdefault(sid, {})['caller_permission_mode'] = args['caller_permission_mode']
        self._save_sessions()
        return result

    def snapshot(self, sid, deadline=None):
        self.ensure_session(sid, deadline)
        return self.rpc('session/read', {'sessionId': sid, 'messageLimit': 10}, deadline)

    def summary(self, snapshot):
        result = {key: copy.deepcopy(snapshot.get(key)) for key in ('session', 'projection', 'runtime', 'settings')}
        # The catalogue has its own tool; avoid repeating it on every status poll.
        result.get('settings', {}).get('model', {}).pop('available', None)
        sid = (result.get('session') or {}).get('sessionId')
        if sid:
            info = self._session_info(sid)
            result['transport'] = info.get('transport') or 'engine'
            if info.get('native_source'):
                result['transport_source'] = info['native_source']
        subscriptions = self.notifications.all_subscriptions()
        waiting = [sub for sub in subscriptions
                   if sub.get('session_id') == sid and sub.get('state') in ('arming', 'armed')]
        result['delivery'] = {'awaiting_start': bool(waiting),
                              'subscriptions': len([sub for sub in subscriptions
                                                    if sub.get('session_id') == sid])}
        if sid:
            workspace = (result.get('session') or {}).get('workspace', {}).get('workspacePath') or self.sessions.get(sid, {}).get('workspace')
            result['desktop'] = session_visibility(sid, workspace)
            if result.get('transport') == 'desktop_native':
                # The persisted index is still useful, but the legacy helper's
                # independent-engine diagnosis is false for a Desktop-owned SID.
                result['desktop']['connection'] = 'desktop_native'
                result['desktop'].pop('live_updates', None)
                result['desktop']['gui_verified'] = False
                result['desktop']['note'] = (
                    '由 ZCode Desktop 原生引擎管理；持久索引与当前页面是否可见需分别核实。')
                for key in ('ownedSID', 'desktopIndexFacts', 'desktopListEvent', 'projectAssignment'):
                    if key in snapshot:
                        result[key] = copy.deepcopy(snapshot[key])
                if 'projectAssignment' not in result and isinstance(info.get('project_assignment'), dict):
                    result['projectAssignment'] = copy.deepcopy(info['project_assignment'])
                # The control channel never inspects the actual GUI.
                result['guiVerified'] = False
        return result

    # ------------------------------------------------------- notification helpers
    def _native_tail_rows(self, sid, deadline=None, snapshot=None):
        snapshot = self.snapshot(sid, deadline) if snapshot is None else snapshot
        if (not isinstance(snapshot, dict)
                or not isinstance(snapshot.get('session'), dict)
                or snapshot['session'].get('sessionId') != sid
                or not isinstance(snapshot.get('messages'), list)
                or any(not isinstance(row, dict) for row in snapshot['messages'])):
            raise DesktopNativeError('native_snapshot_messages_invalid',
                                     'ready native snapshot must identify this session and its messages')
        return snapshot['messages']

    def _last_message_id(self, sid, deadline=None, snapshot=None):
        native = self._is_native(sid)
        if native:
            rows = self._native_tail_rows(sid, deadline, snapshot)
        else:
            try:
                rows = self.rpc('session/messages', {'sessionId': sid, 'limit': 20}, deadline).get('messages') or []
            except Exception:
                return ''
        for message in reversed(rows):
            if not is_visible_message(message):
                continue
            ident = message_identity(message)
            if native and not ident:
                raise DesktopNativeError('native_snapshot_messages_invalid',
                                         'native tail message has no stable message id')
            return str(ident) if ident else ''
        return ''

    def _tail_activity(self, sid, deadline=None, snapshot=None):
        """Return the visible tail message id and a body-free activity digest.

        This is the one cheap progress read used by unsubscribed polling.  It
        never returns message text; only a digest of role/part shapes/tool
        names/text lengths is kept so that a streaming update under the same
        message id is still observable.
        """
        native = self._is_native(sid)
        if native:
            rows = self._native_tail_rows(sid, deadline, snapshot)
        else:
            try:
                rows = self.rpc('session/messages', {'sessionId': sid, 'limit': 20},
                                deadline).get('messages') or []
            except Exception:
                return '', '', False
        for message in reversed(rows):
            if not is_visible_message(message):
                continue
            ident = message_identity(message)
            if native and not ident:
                raise DesktopNativeError('native_snapshot_messages_invalid',
                                         'native tail message has no stable message id')
            return (str(ident) if ident else '',
                    message_activity_digest(message), True)
        return '', '', True

    def _last_user_message_id(self, sid, deadline=None, snapshot=None):
        native = self._is_native(sid)
        if native:
            rows = self._native_tail_rows(sid, deadline, snapshot)
        else:
            try:
                rows = self.rpc('session/messages', {'sessionId': sid, 'limit': 20}, deadline).get('messages') or []
            except Exception:
                return ''
        for message in reversed(rows):
            if not is_visible_message(message):
                continue
            info = message.get('info') or {}
            if (info.get('role') or message.get('role')) == 'user':
                ident = message_identity(message)
                if native and not ident:
                    raise DesktopNativeError('native_snapshot_messages_invalid',
                                             'native user tail message has no stable message id')
                if ident:
                    return str(ident)
        return ''

    def _workflow_runs(self, sid, deadline=None, strict=False):
        if strict:
            return (self.rpc('v4/conversation/workflowRuns',
                             {'sessionId': sid, 'limit': 64}, deadline) or {}).get('runs') or []
        try:
            return (self.rpc('v4/conversation/workflowRuns',
                             {'sessionId': sid, 'limit': 64}, deadline) or {}).get('runs') or []
        except Exception:
            return []

    def observe_run_topology(self, sid, run_id, deadline=None):
        try:
            events, cursor, more = [], 0, True
            while more and len(events) < 2000:
                page = self.rpc('v4/conversation/workflowRunEvents', {
                    'sessionId': sid, 'runId': run_id,
                    'afterSequence': cursor, 'limit': 500}, deadline)
                rows = page.get('events') or []
                if not rows:
                    break
                events.extend(rows)
                cursor = rows[-1].get('sequence')
                more = bool(page.get('hasMore'))
            result = topology(run_id, events)
            result.update(after_sequence=cursor or 0, more=more)
            return result
        except Exception as error:
            # A failed journal read is not an empty journal.  Callers must not
            # turn cursor 0 into progress; the error type lets polling count
            # the read failure while keeping the previous watermark.
            return {**topology(run_id, []), 'after_sequence': 0, 'more': False,
                    'read_error': type(error).__name__}

    def _children_evidence(self, sid, deadline=None, strict=False, allow_unstarted=False):
        try:
            result = self.rpc('session/subagents', {'sessionId': sid, 'endedLimit': 30},
                              deadline) or {}
        except (ProtocolError, DesktopNativeError) as error:
            # The engine only answers session/subagents from the persisted
            # session row; a brand-new session gets that row at its first
            # input.  Only the exact not-found for a proven unstarted session
            # may mean "no children yet"; everything else stays strict.
            if not strict:
                return [], []
            if not (allow_unstarted and is_session_not_found_error(error, sid)):
                raise
            result = {}
        except Exception:
            if strict:
                raise
            return [], []
        child_ids = [str(value) for value in result.get('childSessionIds') or [] if value]
        children = []
        for index, child_id in enumerate(child_ids):
            if index >= 20:
                # Verify the first 20; never silently drop the rest.
                children.append({'id': child_id, 'projection_status': 'unverified'})
                continue
            status = 'unverified'
            activity = ''
            try:
                snap = self.snapshot(child_id, deadline)
                projection = snap.get('projection') or {}
                status = str(projection.get('status') or 'unknown')
                activity = self._child_activity(snap)
            except Exception:
                pass
            row = {'id': child_id, 'projection_status': status}
            if activity:
                row['activity'] = activity
            children.append(row)
        return children, child_ids

    def _workflow_stop_metadata(self, sid, run_ids, deadline=None):
        """Read missing stop reasons from this engine and this bound workspace.

        The native transport has no workspace-runs endpoint.  It must never
        fall back to an independent engine; its own journal is the only fallback.
        No script/args/result fields or other parents' run records are retained.
        """
        info = self._session_info(sid)
        workspace = info.get('workspace')
        if self._is_native(sid) or not isinstance(workspace, str) or not workspace \
                or not Path(workspace).is_absolute():
            return {}, 'workflow_stop_metadata_unavailable'
        try:
            response = self.rpc('workflows/runs', {
                'workspace': {'workspacePath': workspace, 'workspaceKey': workspace},
                'scope': 'project', 'limit': 50}, deadline)
            candidates = (response or {}).get('runs')
            if not isinstance(candidates, list):
                return {}, 'workflow_stop_metadata_invalid'
            matches, duplicates = {}, set()
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                run_id = row.get('runId')
                if run_id not in run_ids or row.get('parentSessionId') != sid:
                    continue
                if row.get('cwd') is not None and row['cwd'] != workspace:
                    continue
                if run_id in matches:
                    duplicates.add(run_id)
                    continue
                matches[run_id] = {
                    'status': str(row.get('status') or '').lower(),
                    'stop_reason': (row.get('stopReason')
                                    if isinstance(row.get('stopReason'), str) else None),
                }
            for run_id in duplicates:
                matches.pop(run_id, None)
            return matches, 'workflow_stop_metadata_ambiguous' if duplicates else ''
        except Exception as error:
            return {}, 'workflow_stop_metadata_' + type(error).__name__

    def _run_rows(self, sid, deadline=None, strict=False):
        rows, calls = [], 0
        raw_runs = self._workflow_runs(sid, deadline, strict=strict)
        missing_reasons = {str(run.get('runId') or run.get('run_id') or '')
                           for run in raw_runs[:64]
                           if str(run.get('status') or run.get('state') or '').lower() == 'stopped'
                           and not (run.get('stopReason') or run.get('stop_reason'))}
        missing_reasons.discard('')
        metadata, metadata_error = (self._workflow_stop_metadata(sid, missing_reasons, deadline)
                                    if missing_reasons else ({}, ''))
        for run in raw_runs[:64]:
            run_id = str(run.get('runId') or run.get('run_id') or '')
            if not run_id:
                continue
            status = str(run.get('status') or run.get('state') or '').lower()
            facts = {'status': status}
            reason = run.get('stopReason') or run.get('stop_reason')
            if isinstance(reason, str) and reason:
                facts['stop_reason'] = reason
            if type(run.get('resumable')) is bool:
                facts['resumable'] = run['resumable']
            reason_source = 'sdk' if facts.get('stop_reason') else ''
            supplement = metadata.get(run_id)
            conflict = bool(supplement and supplement['status'] != status)
            if supplement and not conflict and supplement.get('stop_reason'):
                facts['stop_reason'] = supplement['stop_reason']
                reason_source = 'workspace_runs'
            cursor = 0
            read_error = ''
            state = workflow_run_state(facts)
            if (state not in ('completed', 'failed', 'cancelled')) and calls < 5:
                calls += 1
                topo = self.observe_run_topology(sid, run_id, deadline)
                cursor = int(topo.get('after_sequence') or 0)
                read_error = str(topo.get('read_error') or '')
                # SDK listRunsForSession merges current live runtime state with
                # its journal.  Historical topology must not undo that merge.
                if (status == 'stopped' and not conflict and topo.get('status') == 'stopped'
                        and not topo.get('more') and not read_error):
                    if not facts.get('stop_reason') and topo.get('stop_reason'):
                        facts['stop_reason'] = topo['stop_reason']
                        reason_source = 'journal'
                    if 'resumable' not in facts and type(topo.get('resumable')) is bool:
                        facts['resumable'] = topo['resumable']
                elif status == 'stopped' and topo.get('more') and cursor:
                    read_error = read_error or 'workflow_stop_journal_truncated'
            state = 'unknown' if conflict else workflow_run_state(facts)
            # Normalize only the observation's lifecycle label.  Preserve the
            # native SDK status and diagnostic facts rather than inventing them.
            normalized = ('failed' if status == 'errored' else
                          state if state in ('paused', 'unknown') else status)
            row = {'run_id': run_id, 'status': normalized or 'unknown',
                   'settled': state in ('completed', 'failed', 'cancelled'),
                   'cursor': cursor}
            row.update({key: facts[key] for key in ('stop_reason', 'resumable') if key in facts})
            if normalized != status:
                row['native_status'] = status
            if reason_source:
                row['stop_reason_source'] = reason_source
            if conflict:
                row['metadata_status'] = supplement['status']
                read_error = 'workflow_status_conflict'
            elif status == 'stopped' and state == 'unknown':
                read_error = read_error or metadata_error or 'workflow_stop_unverified'
            if read_error:
                row['read_error'] = read_error
            rows.append(row)
        return rows, len(raw_runs) >= 64

    def _baseline(self, sid, snapshot, deadline=None, run_rows=None, run_truncated=None,
                  strict=False, allow_unstarted_children=False):
        projection = snapshot.get('projection') or {}
        jobs = [{'id': str(job.get('jobId') or job.get('job_id') or job.get('taskId')
                           or job.get('id') or ''),
                 'status': str(job.get('status') or job.get('state') or '')}
                for job in projection.get('backgroundJobs') or []]
        if run_rows is None:
            run_rows, truncated = self._run_rows(sid, deadline, strict=strict)
        else:
            truncated = bool(run_truncated)
        allow_children_gap = bool(allow_unstarted_children and self._is_created(sid)
                                  and not jobs and not run_rows
                                  and self._projection_is_unstarted(projection)
                                  and self._snapshot_has_no_user_input(snapshot))
        children, child_ids = self._children_evidence(
            sid, deadline, strict=strict, allow_unstarted=allow_children_gap)
        terminal = ('completed', 'complete', 'succeeded', 'success', 'settled',
                    'failed', 'error', 'cancelled', 'canceled', 'stopped')
        return {
            'job_ids': [job['id'] for job in jobs if job['id']],
            'active_job_ids': [job['id'] for job in jobs if job['id']
                               and job['status'].lower() not in terminal],
            'run_ids': [row['run_id'] for row in run_rows],
            'terminal_run_ids': [row['run_id'] for row in run_rows if row['status'] in terminal],
            'active_run_ids': [row['run_id'] for row in run_rows if row['status'] not in terminal],
            'child_ids': list(child_ids),
            'active_child_ids': [child['id'] for child in children
                                 if str(child.get('projection_status') or '').lower()
                                 not in ('completed', 'idle', 'cancelled', 'canceled', 'error', 'failed')],
            'truncated': bool(truncated),
        }

    def _active_background(self, snapshot, run_rows):
        projection = snapshot.get('projection') or {}
        for job in projection.get('backgroundJobs') or []:
            if str(job.get('status') or job.get('state') or '').lower() not in (
                    'completed', 'complete', 'succeeded', 'success', 'successful', 'done',
                    'failed', 'error', 'cancelled', 'canceled', 'stopped'):
                return True
        return any(not row.get('settled') for row in run_rows)

    @staticmethod
    def _projection_is_unstarted(projection):
        """Engine projection proves the first input was never accepted."""
        return (projection.get('turnCount') == 0
                and str(projection.get('status') or '').lower() == 'idle'
                and not projection.get('currentTurnId')
                and not projection.get('backgroundJobs'))

    @staticmethod
    def _snapshot_has_no_user_input(snapshot):
        """A visible user message is sent-input evidence; unknown shapes are not proof."""
        messages = snapshot.get('messages')
        if not isinstance(messages, list):
            return False
        for message in messages:
            if not isinstance(message, dict):
                return False
            info = message.get('info')
            info = info if isinstance(info, dict) else {}
            if (info.get('role') or message.get('role')) != 'user':
                continue
            if is_visible_message(message):
                return False
        return True

    def _is_unstarted_created_session(self, sid, snapshot, run_rows):
        """Only a session this Bridge generation created may skip the gap."""
        return (self._is_created(sid)
                and self._projection_is_unstarted(snapshot.get('projection') or {})
                and not run_rows
                and self._snapshot_has_no_user_input(snapshot))

    def _notify_consent(self, notify, permission):
        if not isinstance(notify, dict):
            raise ValueError('notify 必须是对象')
        if notify.get('user_authorized') is not True:
            raise ValueError('通知订阅必须由当前 chat 的显式用户授权 (user_authorized=true)')
        auto_continue = bool(notify.get('auto_continue', False))
        completion_notify = bool(notify.get('completion_notify', True))
        if auto_continue and permission == 'read_only':
            raise ValueError('只读会话不能订阅自动续工')
        if auto_continue and not completion_notify:
            raise ValueError('auto_continue 需要 completion_notify=true；仅 inbox 的事件不会唤醒 chat')
        return {'completion_notify': completion_notify,
                'auto_continue': auto_continue, 'user_authorized': True,
                'permission_at_arm': permission}

    def _notification_summary(self, sub, **extra):
        return {
            'subscription_id': sub.get('subscription_id'),
            'session_id': sub.get('session_id'),
            'state': sub.get('state'),
            'state_reason': sub.get('state_reason'),
            'arm_basis': sub.get('arm_basis'),
            'accepted': bool((sub.get('turn') or {}).get('send_accepted')),
            'anchor_ready': has_anchor_proof(sub),
            'terminal_event_id': sub.get('terminal_event_id'),
            'expires_at': sub.get('expires_at'),
            'start_deadline': sub.get('start_deadline'),
            'delivery': 'inbox_only' if not (sub.get('consent') or {}).get('completion_notify', True) else 'pending',
            **extra,
        }

    def _reconcile_send(self, sub, deadline=None):
        """Only an input-id-linked user message proves this send happened."""
        input_id = str((sub.get('turn') or {}).get('input_id') or '')
        if not input_id:
            return None
        try:
            rows = self.rpc('session/messages', {'sessionId': sub['session_id'], 'limit': 50},
                            deadline).get('messages') or []
        except Exception:
            return None
        for message in rows:
            if not is_visible_message(message):
                continue
            info = message.get('info') or {}
            ids = {str(info.get(key)) for key in
                   ('inputId', 'clientInputId', 'requestId', 'userInputId') if info.get(key)}
            ids.update(str(message.get(key)) for key in ('inputId', 'clientInputId')
                       if message.get(key))
            if input_id not in ids:
                continue
            role = info.get('role') or message.get('role')
            ident = str(message_identity(message) or '')
            return {'messageId': ident,
                    'userMessageId': ident if role == 'user' else ''}
        return None

    @staticmethod
    def _send_permission(sub, permission=None):
        """The current caller grant, or the grant the subscription was armed
        with.  Never fall back to a stored session mode that may be older."""
        for value in (permission, (sub.get('consent') or {}).get('permission_at_arm')):
            if value in PERMISSIONS:
                return value
        return None

    def _finish_send(self, sub, content, deadline=None, permission=None):
        native = self._is_native(sub['session_id'])
        params = {'sessionId': sub['session_id'], 'content': content,
                  'inputId': (sub.get('turn') or {}).get('input_id')}
        if native:
            current = self._send_permission(sub, permission)
            if current is None:
                # The native host requires this turn's real permission.  A
                # missing value is a definite pre-dispatch failure, not an
                # unknown outcome and not a reason to reuse an old full_access.
                self.notifications.mark_send_not_accepted(
                    sub['subscription_id'], {'error': 'caller_permission_required'})
                return self._notification_summary(
                    self.notifications.find_subscription(sub['subscription_id']) or sub,
                    accepted=False, state_reason='caller_permission_required',
                    error_code='caller_permission_required')
            params['caller_permission_mode'] = current
        try:
            result = self.rpc('session/send', params, deadline)
        except DesktopNativeError as error:
            if native and error.code in ('native_command_rejected', 'send_rejected'):
                # A native ACK rejection is a known refusal of this input.
                self.notifications.mark_send_not_accepted(
                    sub['subscription_id'], {'error': error.code})
                return self._notification_summary(
                    self.notifications.find_subscription(sub['subscription_id']) or sub,
                    accepted=False, state_reason='send_not_accepted',
                    error_code=error.code)
            self.notifications.mark_send_uncertain(sub['subscription_id'], 'send_exception')
            return self._notification_summary(
                self.notifications.find_subscription(sub['subscription_id']) or sub,
                send_uncertain=True, retry_required=True, error_code='send_exception')
        except ProtocolError as error:
            if not native and is_request_schema_rejection(error):
                # The engine rejected the payload shape before admission.
                self.notifications.mark_send_not_accepted(
                    sub['subscription_id'], {'error': 'request_not_accepted'})
                return self._notification_summary(
                    self.notifications.find_subscription(sub['subscription_id']) or sub,
                    accepted=False, state_reason='send_not_accepted',
                    error_code='request_not_accepted')
            self.notifications.mark_send_uncertain(sub['subscription_id'], 'send_exception')
            return self._notification_summary(
                self.notifications.find_subscription(sub['subscription_id']) or sub,
                send_uncertain=True, retry_required=True, error_code='send_exception')
        except Exception:
            self.notifications.mark_send_uncertain(sub['subscription_id'], 'send_exception')
            return self._notification_summary(
                self.notifications.find_subscription(sub['subscription_id']) or sub,
                send_uncertain=True, retry_required=True, error_code='send_exception')
        if native and result.get('send_state') == 'uncertain':
            self.notifications.mark_send_uncertain(sub['subscription_id'], 'send_uncertain')
            return self._notification_summary(
                self.notifications.find_subscription(sub['subscription_id']) or sub,
                send_uncertain=True, retry_required=True, error_code='send_uncertain')
        if result.get('accepted') is True:
            if native:
                command_id = str(result.get('commandId') or '')
                admission = result.get('nativeAdmission')
                if not command_id and isinstance(admission, dict):
                    command_id = str(admission.get('commandId') or '')
                if command_id:
                    self.notifications.set_send_input_id(sub['subscription_id'], command_id)
            updated = self.notifications.mark_send_accepted(sub['subscription_id'], result)
            return self._notification_summary(updated or sub, accepted=True, event_waiting=True)
        self.notifications.mark_send_not_accepted(sub['subscription_id'], result)
        return self._notification_summary(
            self.notifications.find_subscription(sub['subscription_id']) or sub,
            accepted=False, state_reason='send_not_accepted')

    def _resume_existing_send(self, sub, args, deadline=None):
        host = self.host_context()
        if host:
            refreshed = self.notifications.update_host(sub['subscription_id'], host)
            if refreshed is not None:
                sub = refreshed
        turn = sub.get('turn') or {}
        if turn.get('send_accepted') or turn.get('send_result_message_id') or turn.get('user_message_id'):
            return self._notification_summary(sub, duplicate=True, accepted=True)
        if not turn.get('send_attempted'):
            if not self._is_native(sub['session_id']):
                self.inherit(sub['session_id'], args, deadline)
            claimed = self.notifications.claim_send(sub['subscription_id'])
            if claimed is None:
                return self._notification_summary(sub, send_uncertain=True, retry_required=True)
            return self._finish_send(claimed, args['content'], deadline,
                                     permission=args.get('caller_permission_mode'))
        found = self._reconcile_send(sub, deadline)
        if found:
            updated = self.notifications.mark_send_accepted(sub['subscription_id'], found)
            return self._notification_summary(updated or sub, duplicate=True, accepted=True,
                                              reconciled=True)
        if args.get('retry') is True:
            if not self._is_native(sub['session_id']):
                self.inherit(sub['session_id'], args, deadline)
            claimed = self.notifications.claim_send(sub['subscription_id'], retry=True)
            if claimed is None:
                return self._notification_summary(sub, send_uncertain=True, retry_required=True)
            return self._finish_send(claimed, args['content'], deadline,
                                     permission=args.get('caller_permission_mode'))
        return self._notification_summary(sub, send_uncertain=True, retry_required=True,
                                          state='send_uncertain')

    def _send_with_notify(self, sid, args, meta, deadline=None):
        permission = caller_permission(args)
        source = require_source(meta)
        consent = self._notify_consent(args.get('notify'), permission)
        key = args.get('idempotency_key') or source.get('request_id')
        if not key:
            raise ValueError('notify 必须提供 idempotency_key（或由 _meta.request_id 提供）')
        key_hash = hashlib.sha256(str(key).encode('utf-8')).hexdigest()
        existing = self.notifications.find_idempotency(sid, source['thread_id'], key_hash)
        if existing is not None:
            return self._resume_existing_send(existing, args, deadline)
        owner = self.notifications.session_owner(sid)
        if owner is not None:
            raise ValueError('session_busy: 该 session 已有活动通知订阅；等待完成或先在来源 chat 取消')
        snapshot = self.snapshot(sid, deadline)
        projection = snapshot.get('projection') or {}
        status = str(projection.get('status') or '').lower()
        run_rows, run_truncated = self._run_rows(sid, deadline, strict=True)
        if status in ('running', 'waiting', 'paused') or self._active_background(snapshot, run_rows):
            raise ValueError('session_busy_untracked: 该 session 有桥接外的运行中轮次或后台工作；先 zcode_notify_subscribe attach')
        if not self._is_native(sid):
            self.inherit(sid, args, deadline)
        before_message_id = self._last_message_id(sid, deadline, snapshot=snapshot)
        baseline = self._baseline(
            sid, snapshot, deadline, run_rows=run_rows, run_truncated=run_truncated,
            strict=True,
            # Only the first send of a session this generation created can hit
            # the engine's persisted-parent gap; resumed/active sessions stay
            # strict and diagnose the same error.
            allow_unstarted_children=self._is_unstarted_created_session(sid, snapshot, run_rows))
        settings = self.notification_settings()
        now = time.time()
        status, sub = self.notifications.begin_arming(
            sid, source, consent, arm_basis='send_accepted', input_id=make_input_id(),
            before_message_id=before_message_id,
            before_turn_count=int(projection.get('turnCount') or 0), baseline=baseline,
            idempotency_key_hash=key_hash, state='arming', host=self.host_context(),
            engine_pid=self.engine_identity(sid).get('pid'),
            generation=self.engine_identity(sid).get('generation'),
            start_deadline=now + settings['start_timeout'],
            expires_at=now + settings['expiry_hours'] * 3600)
        if status == 'existing':
            return self._resume_existing_send(sub, args, deadline)
        if status == 'busy':
            raise ValueError('session_busy: 该 session 已有活动通知订阅；等待完成或先在来源 chat 取消')
        claimed = self.notifications.claim_send(sub['subscription_id'])
        if claimed is None:
            return self._notification_summary(sub, send_uncertain=True, retry_required=True)
        return self._finish_send(claimed, args['content'], deadline,
                                 permission=permission)

    def _subscribe_notify(self, sid, args, meta, deadline=None):
        permission = caller_permission(args)
        source = require_source(meta)
        consent = self._notify_consent({'completion_notify': args.get('completion_notify', True),
                                        'auto_continue': args.get('auto_continue', False),
                                        'user_authorized': args.get('user_authorized')},
                                       permission)
        existing = self.notifications.active_subscription_for(sid, source['thread_id'])
        if existing is not None:
            requested = (args.get('input_id') or args.get('user_message_id')
                         or args.get('after_message_id') or '')
            current = subscription_anchor(existing)
            if not requested or requested == current or requested in (
                    (existing.get('turn') or {}).get('input_id'),
                    (existing.get('turn') or {}).get('user_message_id')):
                host = self.host_context()
                if host:
                    existing = self.notifications.update_host(
                        existing['subscription_id'], host) or existing
                return self._notification_summary(existing, duplicate=True)
            raise ValueError('session_busy: 本 thread 已有该 session 的其它活动轮次')
        owner = self.notifications.session_owner(sid)
        if owner is not None:
            raise ValueError('session_busy_foreign: 该 session 由其它来源 chat 的活动订阅持有')
        self.ensure_session(sid, deadline)
        snapshot = self.snapshot(sid, deadline)
        projection = snapshot.get('projection') or {}
        status = str(projection.get('status') or '').lower()
        run_rows, truncated = self._run_rows(sid, deadline, strict=True)
        active_background = self._active_background(snapshot, run_rows)
        input_id = str(args.get('input_id') or '')
        user_message_id = str(args.get('user_message_id') or '')
        after_message_id = str(args.get('after_message_id') or '')
        requested = input_id or user_message_id or after_message_id
        turn_anchor = requested
        if not turn_anchor:
            user_message_id = self._last_user_message_id(sid, deadline, snapshot=snapshot)
            if user_message_id:
                turn_anchor = user_message_id
            elif status == 'running':
                after_message_id = self._last_message_id(sid, deadline, snapshot=snapshot)
                turn_anchor = after_message_id
            elif active_background and run_rows:
                turn_anchor = 'run:' + str(run_rows[0].get('run_id') or '')
            else:
                raise ValueError('anchor_required: 需要 input_id、user_message_id、after_message_id 或当前最后一条用户消息')
        if not turn_anchor:
            raise ValueError('anchor_required: 当前轮次没有可验证的消息锚点')
        if status in ('running', 'waiting', 'paused'):
            arm_basis = 'attach_running'
        elif active_background:
            arm_basis = 'attach_active_background'
        elif status in ('completed', 'idle', 'error'):
            raise ValueError('already_terminal: 该 session 没有运行中轮次或活动后台工作')
        else:
            raise ValueError('anchor_required: 无法证明当前轮次，未创建订阅')
        baseline = self._baseline(sid, snapshot, deadline, run_rows=run_rows,
                                  run_truncated=truncated, strict=True)
        before_message_id = after_message_id or self._last_message_id(
            sid, deadline, snapshot=snapshot)
        child_ids = list(baseline.get('child_ids') or [])
        settings = self.notification_settings()
        now = time.time()
        state, sub = self.notifications.begin_attach(
            sid, source, consent, requested=requested, arm_basis=arm_basis,
            input_id=input_id, user_message_id=user_message_id,
            before_message_id=before_message_id, turn_anchor=turn_anchor,
            anchor_proof=('input_id_echo' if input_id else ''),
            before_turn_count=int(projection.get('turnCount') or 0), baseline=baseline,
            known_child_ids=child_ids, host=self.host_context(),
            engine_pid=self.engine_identity(sid).get('pid'),
            generation=self.engine_identity(sid).get('generation'),
            start_deadline=now + settings['start_timeout'],
            expires_at=now + settings['expiry_hours'] * 3600)
        if state == 'busy':
            raise ValueError('session_busy: 本 thread 已有该 session 的其它活动轮次')
        if state == 'busy_foreign':
            raise ValueError('session_busy_foreign: 该 session 由其它来源 chat 的活动订阅持有')
        if state == 'existing':
            return self._notification_summary(sub, duplicate=True)
        return self._notification_summary(sub, attached=True)

    # ------------------------------------------------------- observer adapter
    def engine_identity(self, sid=None):
        if sid and self._is_native(sid):
            info = self._session_info(sid)
            runtime = info.get('native_runtime') if isinstance(info.get('native_runtime'), dict) else {}
            generation = runtime.get('generation')
            try:
                generation = int(generation)
            except (TypeError, ValueError):
                generation = None
            return {'pid': None, 'generation': generation,
                    'identity': runtime.get('identity'),
                    'transport': 'desktop_native'}
        with self.client_lock:
            child = getattr(self.client, 'child', None) if self.client is not None else None
            return {'pid': getattr(child, 'pid', None), 'generation': self.generation}

    def pending_snapshot(self):
        with self.lock:
            return copy.deepcopy(list(self.pending.values()))

    def observe_jobs(self, snapshot, sid=None, deadline=None):
        return [{'id': str(job.get('jobId') or job.get('job_id') or job.get('taskId')
                           or job.get('id') or ''),
                 'status': str(job.get('status') or job.get('state') or '')}
                for job in (snapshot.get('projection') or {}).get('backgroundJobs') or []]

    def observe_workflows(self, sid, deadline=None):
        rows, truncated = self._run_rows(sid, deadline, strict=True)
        return rows, truncated

    def observe_children(self, sid, deadline=None):
        return self._children_evidence(sid, deadline, strict=True)

    def observe_messages(self, sid, after_message_id=None, deadline=None):
        params = {'sessionId': sid, 'limit': 20}
        if after_message_id:
            params = {'sessionId': sid, 'afterMessageId': after_message_id}
        try:
            return self.rpc('session/messages', params, deadline)
        except Exception:
            return {'messages': []}

    def observe_events(self, sid, after_seq=0, deadline=None):
        try:
            return self.rpc('session/events', {'sessionId': sid, 'afterSeq': max(0, int(after_seq))},
                            deadline)
        except Exception:
            return {'events': []}

    def observe_stream_id(self, sid, deadline=None):
        if self._is_native(sid):
            info = self._session_info(sid)
            runtime = info.get('native_runtime') if isinstance(info.get('native_runtime'), dict) else {}
            identity = str(runtime.get('identity') or '')
            generation = runtime.get('generation')
            if identity or generation is not None:
                return 'native:' + identity + ':' + str(generation)
        try:
            snapshot = self.snapshot(sid, deadline)
        except Exception:
            return ''
        runtime = snapshot.get('runtime') or {}
        if isinstance(runtime, dict):
            for key in ('instanceId', 'engineInstanceId', 'streamId', 'serverInstanceId',
                        'sessionStreamId'):
                if runtime.get(key):
                    return str(runtime[key])
        return ''

    # Observer-facing names live on ObserverRuntimeAdapter above; Bridge keeps
    # the observe_* transport implementations and the interactive pending dict.
    def _wait_verdict(self, sub, snapshot, pending):
        """Short wait reads persisted truth; it never turns idle into success."""
        status = str((snapshot.get('projection') or {}).get('status') or '').lower()
        jobs = (snapshot.get('projection') or {}).get('backgroundJobs') or []
        active_jobs = [job for job in jobs
                       if str(job.get('status') or job.get('state') or '').lower()
                       not in ('completed', 'complete', 'succeeded', 'success', 'successful',
                               'done', 'failed', 'error', 'cancelled', 'canceled')]
        if sub is None:
            if pending:
                return {'verdict': 'unknown', 'confirmed': False,
                        'reason_code': 'pending', 'stop': True}
            if status in ('completed', 'idle') and not active_jobs:
                return {'verdict': 'unknown', 'confirmed': False,
                        'reason_code': 'no_subscription', 'stop': True}
            if status == 'waiting':
                return {'verdict': 'unknown', 'confirmed': False,
                        'reason_code': 'waiting_unknown', 'stop': True}
            if status == 'error':
                return {'verdict': 'unknown', 'confirmed': False,
                        'reason_code': 'error_status', 'stop': True}
            if status == 'paused':
                return {'verdict': 'unknown', 'confirmed': False,
                        'reason_code': 'stopped', 'stop': True}
            return {'verdict': 'unknown', 'confirmed': False,
                    'reason_code': 'running', 'stop': False}
        state = sub.get('state')
        if state in TERMINAL_STATES:
            return {'verdict': state, 'confirmed': True,
                    'reason_code': sub.get('state_reason') or state, 'stop': True}
        if state == 'paused':
            return {'verdict': 'paused', 'confirmed': False,
                    'reason_code': 'stopped', 'stop': True}
        if state == 'send_uncertain':
            return {'verdict': 'send_uncertain', 'confirmed': False,
                    'reason_code': 'send_uncertain', 'stop': True}
        if pending:
            return {'verdict': 'action_required', 'confirmed': False,
                    'reason_code': 'pending', 'stop': True}
        return {'verdict': 'unknown', 'confirmed': False,
                'reason_code': state or 'unknown', 'stop': False}

    @staticmethod
    def _report_mcp_server():
        """A session-local MCP entry exposing only the untrusted report tool."""
        package = Path(__file__).resolve().parent
        return {
            'name': 'zcode-superagent-report',
            'command': sys.executable,
            'args': ['-m', 'zcode_bridge.report_mcp'],
            'cwd': str(package.parent),
            'env': [{'name': 'ZCODE_BRIDGE_STATE', 'value': str(state_dir())}],
            'isolation': 'session',
            'alwaysLoad': True,
        }

    def _native_create_session(self, args, permission, cfg):
        if args.get('attach_report_mcp'):
            raise DesktopNativeError(
                'unsupported',
                'native_create 没有 mcpServers 参数；不能声称 report 工具已加载')
        model = args.get('model') or cfg.get('default_model') or {}
        if not isinstance(model, dict) or not all(
                isinstance(model.get(k), str) and model.get(k)
                for k in ('providerId', 'modelId')):
            raise ValueError('model_required: 原生后端必须显式提供真实 provider/model')
        thought = args.get('thought_level')
        if thought is None:
            thought = cfg.get('default_thought_level')
        if not isinstance(thought, str) or not thought.strip() or thought == 'auto':
            raise ValueError('thought_level_required: 原生后端必须显式选择真实思考强度，'
                             '不会自动采用目录默认值')
        workspace = str(Path(args['workspace']).expanduser().resolve())
        request_id = str(args.get('request_id') or ('create_' + uuid.uuid4().hex))
        created = self.desktop_native.create(
            workspace, permission, model, thought, request_id)
        sid = str(created.get('sessionId') or created.get('ownedSID') or '')
        if not sid:
            raise DesktopNativeError('native_create_no_session',
                                     'native create did not return a session id')
        snapshot = created.get('snapshot') if isinstance(created.get('snapshot'), dict) else {}
        with self.lock:
            self.sessions[sid] = {
                'workspace': workspace,
                'created_at': time.time(),
                'caller_permission_mode': permission,
                'label': str(args.get('label') or ''),
                'model': copy.deepcopy(created.get('model') or model),
                'thought_level': created.get('thoughtLevel'),
                'workflow_mode': 'native_default',
                'transport': 'desktop_native',
                'native_runtime': copy.deepcopy(created.get('runtime') or {}),
                'native_source': str(created.get('source') or 'desktop-native-host'),
            }
            if isinstance(created.get('projectAssignment'), dict):
                self.sessions[sid]['project_assignment'] = copy.deepcopy(created['projectAssignment'])
        # Only the successful native_create result contains this confirmed SID;
        # unknown/replayed requests are not creation proof and fail above.
        self._mark_created(sid)
        self._save_sessions()
        result = {
            'session': snapshot.get('session') or {
                'sessionId': sid,
                'workspace': {'workspacePath': workspace},
            },
            'projection': snapshot.get('projection') or {},
            'runtime': snapshot.get('runtime') or {},
            'settings': snapshot.get('settings') or {},
            'transport': 'desktop_native',
            'source': str(created.get('source') or 'desktop-native-host'),
            'ownedSID': created.get('ownedSID') or sid,
            'model': copy.deepcopy(created.get('model') or model),
            'thoughtLevel': created.get('thoughtLevel'),
            'mode': created.get('mode'),
            'desktopIndexFacts': copy.deepcopy(created.get('desktopIndexFacts')),
            'guiVerified': bool(created.get('guiVerified')),
        }
        for key in ('projectAssignment', 'desktopListEvent'):
            if key in created:
                result[key] = copy.deepcopy(created[key])
        return self.summary(result)

    def call(self, name, args=None, context=None, budget_ms=None, meta=None):
        args = args if isinstance(args, dict) else {}
        context = context if context is not None else meta
        host = host_from_context(context)
        if host:
            with self.lock:
                self._host_context.update(host)
                self._host_context['updated_at'] = time.time()
        meta = context
        deadline = time.monotonic() + max(.5, float(budget_ms) / 1000.0) if budget_ms else None
        if name == 'zcode_status':
            return self._desktop_status()
        if name == 'zcode_launch':
            return self.launch(force=True, workspace=args.get('workspace'), allow_focus=args.get('allow_focus',False))
        if name == 'zcode_settings':
            if args.get('changes') is not None:
                if caller_mode(args) == 'plan':
                    raise ValueError('只读会话不能修改桥接配置')
                saved = save_config(args['changes'])
                if 'workflow_authorization_mode' in args['changes']:
                    saved['workflow_authorization_status'] = self.desktop_native.save_workflow_authorization(
                        saved['workflow_authorization_mode'])
            else:
                saved = config()
            return {key: value for key, value in saved.items() if not key.startswith('desktop_sync_')}
        if name == 'zcode_models':
            return self._desktop_models(args)
        if name == 'zcode_create_session':
            mode, cfg = caller_mode(args), config()
            transport = self._select_transport(args.get('transport') or cfg.get('default_transport'))
            path = Path(args['workspace']).resolve()
            if not path.is_dir():
                raise ValueError('指定工作区不存在')
            if transport == 'desktop_native':
                return self._native_create_session(args, args['caller_permission_mode'], cfg)
            model = self.model_ref(args.get('model') or cfg['default_model'], args.get('thought_level'), str(path))
            create_params = {
                'workspace': {'workspacePath': str(path), 'workspaceKey': str(path)},
                'mode': 'build' if mode == 'plan' else mode,
                'model': model,
                **({'thoughtLevel':model['options']['reasoningLevel']} if model.get('options', {}).get('reasoningLevel') else {}),
                'persistence': 'immediate', 'titleGenerationEnabled': False,
                'dynamicWorkflowEnabled': args.get('workflow_mode', cfg['default_workflow_mode']) != 'disabled',
                'dynamicWorkflowMode': args.get('workflow_mode', cfg['default_workflow_mode']),
            }
            if args.get('attach_report_mcp'):
                create_params['mcpServers'] = [self._report_mcp_server()]
            snapshot = self.rpc('session/create', create_params)
            sid = snapshot['session']['sessionId']
            self._mark_resident(sid)
            self._mark_created(sid)
            with self.lock:
                self.sessions[sid] = {'workspace': str(path), 'created_at': time.time(),
                    'caller_permission_mode': args['caller_permission_mode'], 'label': args.get('label', ''),
                    'model':snapshot['settings']['model']['current'],
                    'thought_level':snapshot['settings'].get('thoughtLevel', {}).get('current'),
                    'workflow_mode': args.get('workflow_mode', cfg['default_workflow_mode']),
                    'transport': 'engine',
                    'report_mcp': bool(args.get('attach_report_mcp'))}
            self._save_sessions()
            self._request_desktop_sync(str(path), sid, 'create')
            if mode == 'plan':
                snapshot = self.inherit(sid, args)
            return self.summary(snapshot)
        if name == 'zcode_sessions':
            workspace = str(Path(args['workspace']).resolve()) if args.get('workspace') else None
            rows = [{'sessionId': sid, **copy.deepcopy(data)} for sid, data in self.sessions.items()
                    if data.get('transport') == 'desktop_native'
                    and (not workspace or data.get('workspace') == workspace)]
            limit = min(100, max(1, int(args.get('limit', 30))))
            return {'sessions': rows[:limit], 'more': len(rows) > limit,
                    'transport': 'desktop_native', 'scope': 'bridge_owned'}
        if name == 'zcode_pending':
            sid = args.get('session_id')
            if sid and self._is_native(sid):
                return {'requests': self._native_pending_rows(sid),
                        'transport': 'desktop_native'}
            if not sid:
                native = [key for key, row in self.sessions.items()
                          if row.get('transport') == 'desktop_native' and not row.get('read_only_child')]
                if native:
                    requests, unavailable = [], []
                    for key in native:
                        try:
                            requests.extend(self._native_pending_rows(key))
                        except Exception:
                            unavailable.append(key)
                    return {'requests': requests, 'transport': 'desktop_native',
                            'unavailable_session_ids': unavailable}
            with self.lock:
                rows = list(self.pending.values())
                if sid:
                    rows = [r for r in rows if r.get('params', {}).get('sessionId') == sid]
                return {'requests': copy.deepcopy(rows)}
        if name == 'zcode_answer':
            mode = caller_mode(args)
            ident = args['request_id']
            with self.lock:
                request = copy.deepcopy(self.pending.get(ident))
            if request is None:
                sid = args.get('session_id')
                candidates = ([sid] if sid else [key for key, row in self.sessions.items()
                              if row.get('transport') == 'desktop_native' and not row.get('read_only_child')])
                found = []
                for key in candidates:
                    if not self._is_native(key):
                        continue
                    try:
                        found.extend(row for row in self._native_pending_rows(key)
                                     if row.get('id') == ident)
                    except Exception:
                        if sid:
                            raise
                if len(found) != 1:
                    raise ValueError('请求不存在、已处理或存在歧义；请提供 session_id')
                permission = caller_permission(args)
                result = self._resolve_native_answer(found[0], args, permission)
                return {'answered': True, 'transport': 'desktop_native',
                        'session_id': found[0]['params']['sessionId'],
                        'request_id': ident, 'native_result': result}
            if request['method'] == 'interaction/requestPermission':
                params = request['params']
                options = params.get('options', params.get('request', {}).get('options', []))
                selected = next((o for o in options if o.get('optionId') == args.get('option_id')), None)
                response = args.get('response') if selected is None else selected.get('response')
                if response is None:
                    raise ValueError('请使用实际请求中的 option_id 或提供 response')
                if response.get('decision') == 'allow' and mode != 'yolo' and not args.get('user_approved', False):
                    raise ValueError('当前 GPT 权限要求先取得用户批准')
            else:
                response = args.get('response')
                if response is None:
                    raise ValueError('必须提供该问题的结构化 response')
            # Connection startup may need callbacks. Never hold the interaction
            # lock while reconnecting, and never answer an old process's ID.
            client = self.backend()
            with self.lock:
                current = self.pending.get(ident)
                if current is None or current['fingerprint'] != request['fingerprint']:
                    raise ValueError('请求已失效或连接已改变，请重新读取待处理请求')
                client.respond(current['wire_id'], response)
                self.answered[ident] = {'response': response, 'fingerprint': request['fingerprint'], 'at': time.time()}
                self.pending.pop(ident, None)
            self._event({'method': 'bridge/interactionResolved', 'params': {'request_id': ident}})
            return {'answered': True, 'request_id': ident}
        if name == 'zcode_events':
            if args.get('session_id'):
                self.ensure_session(args['session_id'])
                limit = min(1000, max(1, int(args.get('limit', 50))))
                params = {'sessionId':args['session_id']}
                if 'after_cursor' in args:
                    params['afterSeq'] = max(0, int(args['after_cursor']))
                else:
                    params['limit'] = limit
                result = self.rpc('session/events', params)
                rows = result.get('events', [])
                page = rows[:limit]
                return {'events':page, 'more':len(rows)>limit,
                    'cursor':page[-1]['seq'] if page else args.get('after_cursor',0)}
            after = int(args.get('after_cursor', 0))
            with self.lock:
                rows = [e for e in self.events if e['cursor'] > after][:min(200, int(args.get('limit', 50)))]
                return {'events': rows, 'cursor': rows[-1]['cursor'] if rows else self.seq}
        if name == 'zcode_delegate':
            return self._delegate(args, meta, deadline)
        if name == 'zcode_agent_message':
            return self._agent_message(args, meta, deadline)
        if name == 'zcode_agent_inspect':
            return self._agent_inspect(args, meta, deadline)
        if name == 'zcode_agent_answer':
            return self._agent_answer(args, meta, deadline)
        if name == 'zcode_agent_cancel':
            return self._agent_cancel(args, meta, deadline)
        if name == 'zcode_agent_report':
            return self._agent_report(args, meta)
        sid = args.get('session_id')
        if sid and name not in ('zcode_notify_cancel', 'zcode_notify_inbox',
                                'zcode_notify_ack', 'zcode_notify_status',
                                'zcode_notify_emit'):
            self.ensure_session(sid, deadline)
        if name == 'zcode_read_session':
            snapshot = self.snapshot(sid)
            result = self.summary(snapshot)
            rows = self.rpc('session/messages', {'sessionId': sid, 'limit': 10}).get('messages', [])
            result['messages'] = compact_messages(rows, int(args.get('max_chars', 16000)))
            return result
        if name == 'zcode_notify_subscribe':
            return self._subscribe_notify(sid, args, meta, deadline)
        if name == 'zcode_notify_cancel':
            caller_permission(args)
            source = require_source(meta)
            subscription_id = args.get('subscription_id')
            if subscription_id:
                cancelled = self.notifications.cancel(subscription_id, source['thread_id'])
                return {'cancelled': bool(cancelled), 'subscription_id': subscription_id}
            if sid:
                count = self.notifications.cancel_session(sid, source['thread_id'])
                return {'cancelled': count, 'session_id': sid}
            raise ValueError('需要 subscription_id 或 session_id')
        if name == 'zcode_notify_inbox':
            source = require_source(meta)
            return self.notifications.inbox(
                source['thread_id'], event_id=args.get('event_id'),
                event_ids=args.get('event_ids'),
                after_cursor=int(args.get('after_cursor', 0) or 0),
                limit=int(args.get('limit', 50) or 50),
                session_id=args.get('session_id'),
                include_delivery=bool(args.get('include_delivery', False)))
        if name == 'zcode_notify_ack':
            caller_permission(args)
            source = require_source(meta)
            return self.notifications.ack(
                source['thread_id'], event_ids=args.get('event_ids'),
                up_to_cursor=args.get('up_to_cursor'),
                retry_failed=bool(args.get('retry_failed')))
        if name == 'zcode_notify_status':
            source = require_source(meta)
            return self.notifications.status(
                thread_id=source['thread_id'], subscription_id=args.get('subscription_id'),
                session_id=args.get('session_id'))
        if name == 'zcode_notify_emit':
            caller_permission(args)
            kind, reason, hint = args.get('kind'), args.get('reason_code'), args.get('hint_id')
            if kind not in NOTIFY_KINDS:
                raise ValueError('kind 无效')
            if reason not in NOTIFY_REASONS:
                raise ValueError('reason_code 无效')
            if not isinstance(hint, str) or not 1 <= len(hint) <= 64:
                raise ValueError('hint_id 长度必须为 1-64')
            source = require_source(meta)
            subscription_id = args.get('subscription_id')
            if subscription_id:
                owned = self.notifications.find_subscription(subscription_id, source['thread_id'])
                if owned is None:
                    raise ValueError('not_found_for_source: 没有匹配的活动订阅')
            elif self.notifications.active_subscription_for(sid, source['thread_id']) is None:
                raise ValueError('not_found_for_source: 没有匹配的活动订阅')
            events = self.notifications.emit_hint(sid, kind, hint, reason,
                                                   subscription_id=subscription_id)
            if not events:
                raise ValueError('没有匹配的活动订阅')
            return {'emitted': len(events), 'event_ids': [event['event_id'] for event in events],
                    'terminal': False}
        if name == 'zcode_notify_probe':
            timeout = min(20, max(1, int(args.get('timeout_seconds', 10))))
            now = time.monotonic()
            if now - self._last_probe_at < 10:
                raise ValueError('probe_rate_limited: 每个 daemon 10 秒内只探测一次')
            self._last_probe_at = now
            if self.host_waker is None:
                self.notifications.set_host_capability('not_probed', 'host_adapter_not_initialized')
                return {'capability': 'not_probed', 'reason': 'host_adapter_not_initialized'}
            result = self.host_waker.probe(timeout, host=self.host_context())
            state = result.get('state') or 'unsupported'
            self.notifications.set_host_capability(state, result.get('error_code'))
            if state == 'supported':
                self.notifications.requeue_unsupported()
            return {'capability': state, **result}
        if name == 'zcode_send':
            content = args.get('content')
            if not isinstance(content, str) or not content.strip() or len(content) > 200000:
                raise ValueError('消息必须是非空文本，长度不超过 200000 字符')
            if args.get('notify') is not None:
                result = self._send_with_notify(sid, args, meta, deadline)
                self._request_desktop_sync(self.sessions.get(sid, {}).get('workspace'),
                                           sid, 'input')
                return result
            caller_permission(args)
            owner = self.notifications.session_owner(sid)
            if owner is not None:
                raise ValueError('session_busy: 该 session 已有活动通知订阅；等待完成或先在来源 chat 取消')
            if not self._is_native(sid):
                self.inherit(sid, args, deadline)
            send_params = {'sessionId': sid, 'content': content,
                           'inputId': uuid.uuid4().hex}
            if self._is_native(sid):
                # Only the native transport consumes the current permission;
                # a legacy engine schema rejects unknown keys with -32602.
                send_params['caller_permission_mode'] = args['caller_permission_mode']
            result = self.rpc('session/send', send_params, deadline)
            if self._is_native(sid) and (result or {}).get('accepted') is True:
                with self.lock:
                    self.sessions.setdefault(sid, {})['caller_permission_mode'] = \
                        args['caller_permission_mode']
                self._save_sessions()
            self._request_desktop_sync(self.sessions.get(sid, {}).get('workspace'),
                                       sid, 'input')
            return result
        if name == 'zcode_messages':
            limit = min(100, max(1, int(args.get('limit', 30))))
            params = {'sessionId': sid}
            if args.get('after_message_id'):
                params['afterMessageId'] = args['after_message_id']
            if self._is_native(sid) or not args.get('after_message_id'):
                # Native is explicitly forward and needs the requested size on
                # every page; the legacy forward-after path keeps its schema.
                params['limit'] = limit
            result = self.rpc('session/messages', params)
            if args.get('raw'):
                return result
            all_rows = result.get('messages', [])
            rows = all_rows[:limit]
            anchor = None
            for row in reversed(rows):
                if not is_visible_message(row):
                    continue
                anchor = message_identity(row)
                break
            return {'messages': compact_messages(rows, int(args.get('max_chars', 24000))),
                    'more': bool(result.get('hasMore')) or len(all_rows) > limit,
                    'after_message_id': anchor if anchor is not None else args.get('after_message_id')}
        if name == 'zcode_subagents':
            return self._subagents(sid, args)
        if name == 'zcode_set_model':
            self.inherit(sid, args)
            result = self.rpc('session/setModel', {'sessionId': sid, 'model': self.model_ref(args['model'], args.get('thought_level'),
                self.sessions.get(sid, {}).get('workspace')),
                'persistAsWorkspaceLastUsed': False})
            if args.get('thought_level'):
                result = self.rpc('session/setThoughtLevel', {'sessionId': sid, 'thoughtLevel': args['thought_level'],
                    'persistAsWorkspaceLastUsed': False})
            with self.lock:
                self.sessions.setdefault(sid, {}).update(model=result['settings']['model']['current'],
                    thought_level=result['settings'].get('thoughtLevel', {}).get('current'))
            self._save_sessions()
            return self.summary(result)
        if name == 'zcode_set_permissions':
            return self.summary(self.inherit(sid, args))
        if name == 'zcode_stop':
            caller_permission(args)
            source = require_source(meta) if meta else None
            owner = self.notifications.session_owner(sid)
            if owner is not None:
                if source is None or (owner.get('source') or {}).get('thread_id') != source['thread_id']:
                    raise ValueError('session_busy_foreign: 该 session 由其它来源 chat 的活动订阅持有，未执行 stop')
            stop_params = {'sessionId': sid}
            if self._is_native(sid):
                stop_params['caller_permission_mode'] = args['caller_permission_mode']
            result = self.rpc('session/stop', stop_params, deadline)
            if source is not None:
                self.notifications.interrupt_session(sid, source['thread_id'])
            with self.lock:
                self.pending = {k:v for k,v in self.pending.items() if v.get('params', {}).get('sessionId') != sid}
            return result
        if name == 'zcode_wait':
            source = require_source(meta) if meta else None
            sub = None
            if args.get('subscription_id'):
                if source is None:
                    raise ValueError('source_unavailable: subscription_id 需要请求 context 或 CODEX_THREAD_ID 提供的 thread')
                sub = self.notifications.find_subscription(args['subscription_id'], source['thread_id'])
            elif source is not None:
                sub = self.notifications.latest_subscription_for(sid, source['thread_id'])
            if args.get('subscription_id') and sub is None:
                raise ValueError('not_found_for_source: subscription_id 不属于当前来源 chat')
            budget = max(1.0, min(20.0, float(args.get('timeout_seconds', 15))))
            if budget_ms:
                budget = max(.5, min(budget, float(budget_ms) / 1000.0 - .5))
            deadline = time.monotonic() + budget
            while True:
                try:
                    snapshot = self.snapshot(sid, deadline)
                except Exception:
                    latest = self.notifications.find_subscription(sub['subscription_id']) if sub else None
                    state = (latest or {}).get('state')
                    terminal = bool(state in TERMINAL_STATES)
                    return {'verdict': state if terminal else 'unknown',
                            'confirmed': terminal,
                            'reason_code': ((latest or {}).get('state_reason')
                                            or 'engine_unavailable'),
                            'subscription_id': sub['subscription_id'] if sub else None,
                            'pending_requests': [], 'event_cursor': self.seq,
                            'error_code': 'engine_unavailable'}
                with self.lock:
                    pending = copy.deepcopy([r for r in self.pending.values()
                                             if r.get('params', {}).get('sessionId') == sid])
                latest = self.notifications.find_subscription(sub['subscription_id']) if sub else None
                verdict = self._wait_verdict(latest, snapshot, pending)
                result = self.summary(snapshot)
                result.update({'pending_requests': pending, 'event_cursor': self.seq,
                               'verdict': verdict['verdict'], 'confirmed': verdict['confirmed'],
                               'reason_code': verdict['reason_code'],
                               'subscription_id': sub['subscription_id'] if sub else None})
                if verdict['stop'] or time.monotonic() >= deadline:
                    if not verdict['stop']:
                        result.update(verdict='unknown', confirmed=False, reason_code='timed_out')
                    return result
                time.sleep(.25)
        if name == 'zcode_workflows':
            return self._workflows(args)
        raise ValueError('未知桥接工具：' + name)

    # ------------------------------------------------------- high-level agents
    def _agent_job(self, args, source):
        agent_id = str(args.get('agent_id') or '')
        if not agent_id:
            raise ValueError('agent_not_found: 必须提供 agent_id')
        job = self.superagent.find_agent(agent_id, source['thread_id'])
        if job is None:
            raise ValueError('agent_not_found: 当前来源 chat 没有这个 agent')
        return job

    def _foreground_idle(self, sid, deadline=None):
        """A correction is safe while the foreground turn is idle.

        Background workflows/jobs deliberately do not block this boundary: the
        root chat must be able to correct a long-running child.  A pending
        interaction or a non-idle projection is not a safe boundary.
        """
        if not sid:
            return False, 'no_session'
        try:
            snapshot = self.snapshot(sid, deadline)
        except Exception:
            return False, 'engine_unavailable'
        status = str((snapshot.get('projection') or {}).get('status') or '').lower()
        pending = [row for row in self.pending_snapshot()
                   if (row.get('params') or {}).get('sessionId') == sid]
        if pending:
            return False, 'pending_request'
        if status in ('idle', 'completed'):
            return True, 'idle'
        if status in ('running', 'waiting', 'paused', 'error'):
            return False, status
        return False, status or 'unknown'

    def _plain_send(self, sid, content, permission, deadline=None):
        # Native sends apply the current permission inside the host transport;
        # the engine's setMode/inherit path is not valid for an owned SID.
        # A legacy engine 3.18.2 rejects the extra key with -32602, so the
        # permission stays at the Bridge/native layer and is never sent there.
        native = self._is_native(sid)
        if not native:
            try:
                self.inherit(sid, {'caller_permission_mode': permission}, deadline)
            except Exception as error:
                # Nothing has been handed to the engine yet: a definite
                # pre-send failure, not an unknown delivery outcome.
                raise SendNotStarted('permission_inherit_failed') from error
        params = {'sessionId': sid, 'content': content,
                  'inputId': uuid.uuid4().hex}
        if native:
            params['caller_permission_mode'] = permission
        try:
            result = self.rpc('session/send', params, deadline)
        except DesktopNativeError as error:
            if native and error.code in ('native_command_rejected', 'send_rejected'):
                return {'accepted': False, 'send_state': 'failed',
                        'error': error.code, 'detail': error.detail}, False
            raise
        except ProtocolError as error:
            if not native and is_request_schema_rejection(error):
                # The payload was rejected as invalid before admission; report
                # it as a definite failure so no unknown-retry path is induced.
                return {'accepted': False, 'send_state': 'failed',
                        'error': 'request_not_accepted',
                        'detail': json.dumps(error.error, ensure_ascii=False)[:800]}, False
            raise
        result = result or {}
        if native:
            state = str(result.get('send_state') or '')
            if state == 'uncertain':
                raise DesktopNativeError(
                    'send_uncertain',
                    'native send outcome is not an accepted admission; do not resend')
            if result.get('accepted') is True:
                with self.lock:
                    self.sessions.setdefault(sid, {})['caller_permission_mode'] = permission
                self._save_sessions()
            return result, result.get('accepted') is True
        return result, result.get('accepted') is True

    def _delegate_response(self, job, *, created, conflict=False, send_state=None, detail=None):
        job = job or {}
        notification = job.get('notification') or {}
        if send_state is None:
            # Admission comes only from the persisted real send evidence.  The
            # job status is a lifecycle projection and an unverified child
            # report ('reported') must never imply that the input was accepted.
            evidence = job.get('send') if isinstance(job.get('send'), dict) else {}
            recorded = str(evidence.get('state') or '')
            if recorded in ('pending', 'accepted', 'uncertain', 'failed'):
                send_state = recorded
            else:
                status = str(job.get('status') or '')
                if status in ('running', 'verified'):
                    send_state = 'accepted'
                elif status == 'send_uncertain':
                    send_state = 'uncertain'
                elif status in ('failed', 'cancelled', 'expired'):
                    send_state = 'failed'
                else:
                    send_state = 'pending'
        armed = bool(notification.get('armed'))
        if armed and send_state == 'uncertain':
            delivery = 'uncertain'
        elif armed and not notification.get('completion_notify'):
            delivery = 'inbox_only'
        elif armed and send_state in ('accepted', 'pending'):
            delivery = 'pending'
        else:
            delivery = 'not_armed'
        with self.lock:
            session_info = self.sessions.get(job.get('session_id'))
            assignment = (copy.deepcopy(session_info.get('project_assignment'))
                          if isinstance(session_info, dict)
                          and session_info.get('transport') == 'desktop_native' else None)
        return {
            'created': bool(created),
            'conflict': bool(conflict),
            'agent_id': job.get('agent_id'),
            'job_id': job.get('job_id'),
            'session_id': job.get('session_id'),
            'workspace': job.get('workspace'),
            'profile': job.get('profile'),
            'model': copy.deepcopy(job.get('model')),
            'thought_level': job.get('thought_level'),
            'effective_thought_level': job.get('effective_thought_level'),
            'workflow_mode': job.get('workflow_mode'),
            **({'project_assignment': assignment} if isinstance(assignment, dict) else {}),
            'subscription': {
                'armed': armed,
                'subscription_id': notification.get('subscription_id') or None,
                'completion_notify': bool(notification.get('completion_notify')),
                'auto_continue': bool(notification.get('auto_continue')),
                'user_authorized': bool(notification.get('user_authorized')),
                'reason': notification.get('reason') or '',
            },
            'delivery': {'state': delivery,
                         'event_waiting': bool(armed and send_state == 'accepted')},
            'send': {'state': send_state, 'accepted': send_state == 'accepted',
                     'retry_required': send_state == 'uncertain',
                     'detail': str(detail)[:500] if detail else None},
            'cursor': copy.deepcopy(job.get('cursor')),
            'contract': copy.deepcopy(job.get('contract')),
            'job': self.superagent.job_summary(job),
        }

    def _delegate(self, args, meta, deadline=None):
        permission = caller_permission(args)
        source = require_source(meta)
        task = args.get('task')
        if not isinstance(task, str) or not task.strip():
            raise ValueError('task_required: zcode_delegate 必须提供任务原文')
        if len(task) > 200000:
            raise ValueError('task_too_large: 任务原文不能超过 200000 字符')
        notify = args.get('notify')
        if notify is not None and not isinstance(notify, dict):
            raise ValueError('notify 必须是对象')
        if notify is not None and notify.get('user_authorized') is True:
            # Validate consent before creating a session or an arming record.
            self._notify_consent(notify, permission)
        thread = source['thread_id']

        # An idempotent replay must not touch the engine at all.
        key = args.get('idempotency_key')
        if isinstance(key, str) and key.strip():
            existing = self.superagent.find_by_idempotency(thread, key)
            if existing is not None:
                task_hash = hashlib.sha256(task.encode('utf-8')).hexdigest()
                conflict = bool(existing.get('task_sha256')
                                and existing.get('task_sha256') != task_hash)
                return self._delegate_response(existing, created=False, conflict=conflict)

        self.sync_superagent_jobs()
        cfg = config()
        profiles = cfg.get('superagent_profiles') or {}
        explicit = {name: args.get(name) for name in
                    ('model', 'thought_level', 'workspace', 'workflow_mode', 'profile',
                     'transport')
                    if args.get(name) not in (None, '')}
        configured_workspace = configured_default_workspace(cfg)
        workspace_info = resolve_workspace(explicit=explicit, profiles=profiles, defaults=cfg,
                                           configured_workspace=configured_workspace)
        transport_hint = explicit.get('transport')
        if not transport_hint:
            profile_name = explicit.get('profile') or cfg.get('superagent_default_profile') or ''
            profile_row = profiles.get(profile_name) if profile_name else {}
            if isinstance(profile_row, dict):
                transport_hint = profile_row.get('transport')
        transport_hint = self._select_transport(transport_hint or cfg.get('default_transport'))
        if transport_hint == 'desktop_native':
            # Host replay returns request metadata, not a cached catalog result.
            # Each new preflight is a fresh query; business replay returned above.
            catalog_id = 'catalog_' + uuid.uuid4().hex
            if explicit.get('thought_level'):
                native_catalog = self.desktop_native.model_catalog(
                    workspace_info['workspace'], permission, catalog_id,
                    explicit.get('model'), explicit.get('thought_level'))
            else:
                native_catalog = self.desktop_native.model_catalog(
                    workspace_info['workspace'], permission, catalog_id,
                    explicit.get('model'))
            catalog = {'available': native_catalog.get('available') or []}
        else:
            catalog = self.call('zcode_models', {'workspace': workspace_info['workspace']})
        resolved = resolve_profile(explicit=explicit, profiles=profiles, defaults=cfg,
                                   catalog=catalog,
                                   configured_workspace=configured_workspace)
        if resolved.get('transport') == 'desktop_native':
            # The native host owns its workflow defaults; do not claim the
            # engine's onDemand/alwaysOn/disabled setting was applied.
            resolved['workflow_mode'] = 'native_default'

        created = self.superagent.create_job(
            source, resolved, caller_permission_mode=permission,
            idempotency_key=key, task_text=task)
        job = created['job']
        if not created['created']:
            return self._delegate_response(job, created=False, conflict=created['conflict'])
        job_id = job['job_id']
        self.superagent.set_status(job_id, thread, 'dispatching', 'delegate')

        native_request_id = ('create_' + hashlib.sha256(
            (thread + '\0' + str(job.get('idempotency_key') or '')).encode('utf-8')
        ).hexdigest()[:40])
        try:
            session = self.call('zcode_create_session', {
                'workspace': resolved['workspace'],
                'model': resolved['model'],
                'thought_level': resolved['thought_level'],
                'workflow_mode': resolved['workflow_mode'],
                'transport': resolved.get('transport') or 'engine',
                'attach_report_mcp': bool(args.get('attach_report_mcp')),
                'request_id': native_request_id,
                'caller_permission_mode': permission,
                'label': str(args.get('label') or ''),
            }, context=meta)
            sid = str(((session or {}).get('session') or {}).get('sessionId') or '')
            if not sid:
                raise RuntimeError('session/create 没有返回 sessionId')
        except Exception as error:
            failed = self.superagent.transition(
                job_id, thread, 'failed', 'session_create_failed', 'job.failed')
            self.superagent.set_send_state(job_id, thread, 'failed',
                                           'session_create_failed',
                                           type(error).__name__)
            return self._delegate_response(failed or job, created=True, send_state='failed',
                                           detail=str(error))
        self.superagent.set_session(job_id, thread, sid)
        job = self.superagent.get(job_id, thread) or job
        # Native creation has no session reportMCP, so the contract must not
        # demand a tool that is not loaded.  The engine path keeps its attach
        # option and the report tool wording.
        report_tool = (None if resolved.get('transport') == 'desktop_native'
                       else 'zcode_agent_report')
        prompt = build_task_prompt(task, job, completion=str(args.get('completion') or ''),
                                   report_tool=report_tool)

        armed_requested = bool(notify and notify.get('user_authorized') is True)
        send_state, detail, send_error, notification = 'failed', None, '', {
            'armed': False, 'subscription_id': '',
            'completion_notify': False, 'auto_continue': False,
            'user_authorized': False,
            'reason': 'not_requested' if notify is None else 'user_authorized_false',
        }
        if armed_requested:
            try:
                result = self._send_with_notify(sid, {
                    'content': prompt, 'notify': notify,
                    'caller_permission_mode': permission,
                    'idempotency_key': job['idempotency_key'],
                }, meta, deadline)
            except Exception as error:
                failed = self.superagent.transition(
                    job_id, thread, 'failed', 'delegate_send_rejected', 'job.failed')
                self.superagent.set_send_state(job_id, thread, 'failed',
                                               'delegate_send_rejected',
                                               type(error).__name__)
                return self._delegate_response(failed or job, created=True,
                                               send_state='failed', detail=str(error))
            subscription_id = result.get('subscription_id') or ''
            uncertain = bool(result.get('send_uncertain')) or result.get('state') == 'send_uncertain'
            accepted = result.get('accepted') is True
            send_state = 'uncertain' if uncertain else 'accepted' if accepted else 'failed'
            notification = {
                'armed': bool(subscription_id) and send_state in ('accepted', 'uncertain'),
                'subscription_id': subscription_id,
                'completion_notify': bool(notify.get('completion_notify', True)),
                'auto_continue': bool(notify.get('auto_continue', False)),
                'user_authorized': True,
                'reason': ('send_uncertain' if uncertain
                           else 'accepted' if accepted else 'send_not_accepted'),
            }
            detail = result.get('state_reason') if not accepted else None
            send_error = str(result.get('error_code') or '')
        else:
            try:
                result, accepted = self._plain_send(sid, prompt, permission, deadline)
                send_state = 'accepted' if accepted else 'failed'
                detail = None if accepted else 'send_not_accepted'
                send_error = str((result or {}).get('error') or '')
            except SendNotStarted as error:
                # Definite pre-send failure: nothing reached the engine.
                send_state, detail = 'failed', 'pre_send_failed'
                send_error = str(error)
            except Exception as error:
                send_state, detail = 'uncertain', str(error)
                send_error = type(error).__name__

        job = self.superagent.set_notification(job_id, thread, **notification)
        self.superagent.set_send_state(job_id, thread, send_state,
                                       'send_' + send_state, send_error)
        if send_state == 'accepted':
            self.superagent.set_status(job_id, thread, 'running', 'send_accepted')
            self.superagent.set_contract_sent(job_id, thread)
        elif send_state == 'uncertain':
            self.superagent.transition(job_id, thread, 'send_uncertain',
                                       'send_uncertain', 'job.send_uncertain')
        else:
            self.superagent.transition(job_id, thread, 'failed',
                                       'send_not_accepted', 'job.failed')
        self.superagent.set_cursor(job_id, thread, event_cursor=self.seq)
        self._request_desktop_sync(resolved['workspace'], sid, 'delegate')
        job = self.superagent.get(job_id, thread) or job
        return self._delegate_response(job, created=True, send_state=send_state, detail=detail)

    def _message_view(self, job, message_id):
        for index, item in enumerate((job or {}).get('queue') or ()):
            if item.get('message_id') == message_id:
                return {'state': item.get('state') or 'queued', 'position': index + 1}
        for item in (job or {}).get('delivered_messages') or ():
            if item.get('message_id') == message_id:
                return {'state': 'delivered', 'position': 0}
        return {'state': 'unknown', 'position': 0}

    def _try_drain_job(self, job):
        if not self.superagent_drain_lock.acquire(False):
            return {'state': 'queued', 'reason': 'drain_busy'}
        try:
            return self._drain_job(job)
        finally:
            self.superagent_drain_lock.release()

    def _drain_job(self, job):
        """Send queued follow-ups only at foreground-idle boundaries, in order."""
        thread = (job.get('source') or {}).get('thread_id') or ''
        job_id = job.get('job_id')
        sid = job.get('session_id')
        if not sid:
            return {'state': 'queued', 'reason': 'no_session'}
        sent = []
        for _ in range(MAX_QUEUE_ITEMS):
            current = self.superagent.get(job_id, thread)
            if current is None:
                break
            if self.superagent.peek_next_message(job_id, thread) is None:
                break
            idle, reason = self._foreground_idle(sid)
            if not idle:
                return {'state': 'queued', 'reason': reason, 'position': 1, 'sent': sent}
            # Persist the claim before the side-effecting send.  A crash after
            # this point recovers as uncertain and is never auto-resent.
            item = self.superagent.claim_next_message(job_id, thread)
            if item is None:
                # Another drainer claimed it first; only one claim succeeds.
                break
            permission = (item.get('caller_permission_mode')
                          or (current.get('permission') or {}).get('declared')
                          or 'always_ask')
            prompt = build_task_prompt(item.get('content') or '', current, later=True)
            try:
                result, accepted = self._plain_send(sid, prompt, permission)
            except SendNotStarted as error:
                self.superagent.mark_message_state(job_id, thread, item['message_id'],
                                                   'not_accepted', 'pre_send_failed')
                self.superagent.record_progress(job_id, thread, kind='message',
                                                status='not_accepted')
                return {'state': 'not_accepted', 'message_id': item['message_id'],
                        'retry_required': False, 'error_code': str(error), 'sent': sent}
            except Exception as error:
                self.superagent.mark_message_state(job_id, thread, item['message_id'],
                                                   'uncertain', type(error).__name__)
                self.superagent.transition(job_id, thread, 'send_uncertain',
                                           'message_send_exception', 'job.send_uncertain')
                return {'state': 'uncertain', 'message_id': item['message_id'],
                        'retry_required': True, 'error_code': 'send_exception',
                        'sent': sent}
            if accepted:
                self.superagent.complete_message(job_id, thread, item['message_id'])
                self.superagent.record_progress(job_id, thread, kind='message',
                                                status='accepted')
                current = self.superagent.get(job_id, thread)
                if current is not None and current.get('status') in (
                        'send_uncertain', 'stalled', 'unobservable', 'action_required'):
                    self.superagent.transition(job_id, thread, 'running',
                                               'message_delivered', 'job.resumed')
                sent.append(item['message_id'])
                self._request_desktop_sync(job.get('workspace'), sid, 'message')
            else:
                self.superagent.mark_message_state(job_id, thread, item['message_id'],
                                                   'not_accepted', 'send_not_accepted')
                return {'state': 'not_accepted', 'message_id': item['message_id'],
                        'retry_required': False, 'sent': sent}
        return {'state': 'delivered' if sent else 'empty', 'sent': sent}

    def _job_subscription(self, job):
        sub_id = (job.get('notification') or {}).get('subscription_id') or ''
        thread = (job.get('source') or {}).get('thread_id') or ''
        if not sub_id:
            return None
        return self.notifications.find_subscription(sub_id, thread)

    @staticmethod
    def _job_sessions(job, sub=None):
        sessions = set()
        sid = job.get('session_id')
        if sid:
            sessions.add(str(sid))
        if isinstance(sub, dict):
            sessions.update(str(value) for value in sub.get('known_child_ids') or ()
                            if value)
        return sessions

    @staticmethod
    def _native_pending_timestamps(item):
        """Keep real announcement times in the observer's epoch-second unit.

        Desktop snapshot timestamps use milliseconds. Missing timestamps must
        stay unknown: polling an old request is not a new announcement.
        """
        result = {}
        fields = {
            'received_at': (('received_at', 1), ('requestedAt', 1000),
                            ('createdAt', 1000)),
            'last_announced_at': (('last_announced_at', 1),
                                  ('lastAnnouncedAt', 1000)),
        }
        for target, candidates in fields.items():
            for source, divisor in candidates:
                value = item.get(source)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                try:
                    value = float(value) / divisor
                except (OverflowError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    result[target] = value
                    break
        return result

    @staticmethod
    def _native_questions_from_snapshot(snapshot, sid):
        """Extract real native pending interactions; never synthesize questions."""
        rows, seen = [], set()
        containers = [snapshot]
        if isinstance(snapshot, dict):
            for key in ('projection', 'runtime', 'conversation', 'state'):
                value = snapshot.get(key)
                if isinstance(value, dict):
                    containers.append(value)
        for container in containers:
            for item in (container.get('pendingInteractions') or []):
                if not isinstance(item, dict):
                    continue
                interaction_id = str(item.get('interactionId') or item.get('id') or '')
                if not interaction_id or interaction_id in seen:
                    continue
                payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
                kind = item.get('kind') or payload.get('kind')
                if kind == 'userInput':
                    params = {**copy.deepcopy(payload), 'sessionId': sid,
                              'toolName': payload.get('toolName') or 'AskUserQuestion',
                              'input': copy.deepcopy(payload.get('input', payload))}
                    if 'questions' not in params and isinstance(params['input'], dict):
                        if isinstance(params['input'].get('questions'), list):
                            params['questions'] = copy.deepcopy(params['input']['questions'])
                    method = 'interaction/requestUserInput'
                elif kind == 'permission':
                    params = {**copy.deepcopy(payload), 'sessionId': sid,
                              'toolName': payload.get('toolName') or payload.get('name') or 'permission',
                              'options': copy.deepcopy(payload.get('options') or [])}
                    if isinstance(payload.get('detail'), dict):
                        params['input'] = copy.deepcopy(payload['detail'])
                    if isinstance(payload.get('fullAccessOption'), dict):
                        params['fullAccessOption'] = copy.deepcopy(
                            payload['fullAccessOption'])
                    if payload.get('modeSwitch') is not None:
                        params['modeSwitch'] = copy.deepcopy(payload['modeSwitch'])
                    if params.get('modeSwitch') == 'fullAccessOnly' and 'fullAccessOption' not in params:
                        params['fullAccessOption'] = {'optionId': NATIVE_FULL_ACCESS_OPTION,
                            'label': 'Full access', 'kind': 'custom', 'response': {'decision': 'deny'}}
                        params['fullAccessOptionSource'] = 'native_mode_switch'
                    method = 'interaction/requestPermission'
                elif kind in ('pluginInstall', 'workspaceHookReview'):
                    params = {**copy.deepcopy(payload), 'sessionId': sid}
                    params['toolName'] = ('SuggestPluginInstall' if kind == 'pluginInstall'
                                          else 'WorkspaceHookReview')
                    method = ('interaction/requestPluginInstall' if kind == 'pluginInstall'
                              else 'interaction/requestWorkspaceHookReview')
                else:
                    # A newer native dialog must remain visible and block
                    # completion even when this bridge cannot submit it yet.
                    params = {**copy.deepcopy(payload), 'sessionId': sid,
                              'interactionKind': kind, 'unsupported': True}
                    method = 'interaction/unsupported'
                for key in ('requestId', 'toolCallId', 'reason', 'riskLevel'):
                    if item.get(key) is not None:
                        params[key] = copy.deepcopy(item[key])
                rows.append({'id': interaction_id, 'method': method,
                             'params': params, 'transport': 'desktop_native',
                             **Bridge._native_pending_timestamps(item)})
                seen.add(interaction_id)
            for item in (container.get('pendingPermissions') or []):
                if not isinstance(item, dict):
                    continue
                request_id = str(item.get('requestId') or item.get('toolCallId') or '')
                if not request_id or request_id in seen:
                    continue
                tool_name = str(item.get('toolName') or '')
                raw_input = item.get('input') if 'input' in item else None
                questions = (raw_input.get('questions')
                             if isinstance(raw_input, dict) else None)
                identity = {}
                for key in ('requestId', 'toolCallId', 'reason', 'riskLevel'):
                    if item.get(key) is not None:
                        identity[key] = copy.deepcopy(item[key])
                if tool_name == 'AskUserQuestion' or (isinstance(questions, list)
                                                      and questions):
                    # 3.18.2 keeps AskUserQuestion in pendingPermissions; it is
                    # a real user-input interaction, not a generic permission.
                    params = {'sessionId': sid,
                              'toolName': tool_name or 'AskUserQuestion',
                              'input': copy.deepcopy(raw_input)
                              if raw_input is not None else {'questions': []}}
                    method = 'interaction/requestUserInput'
                else:
                    params = {'sessionId': sid,
                              'toolName': tool_name or 'permission',
                              'options': item.get('options') or []}
                    if isinstance(item.get('fullAccessOption'), dict):
                        params['fullAccessOption'] = copy.deepcopy(
                            item['fullAccessOption'])
                    if item.get('modeSwitch') is not None:
                        params['modeSwitch'] = copy.deepcopy(item['modeSwitch'])
                    if params.get('modeSwitch') == 'fullAccessOnly' and 'fullAccessOption' not in params:
                        params['fullAccessOption'] = {'optionId': NATIVE_FULL_ACCESS_OPTION,
                            'label': 'Full access', 'kind': 'custom', 'response': {'decision': 'deny'}}
                        params['fullAccessOptionSource'] = 'native_mode_switch'
                    if raw_input is not None:
                        params['input'] = copy.deepcopy(raw_input)
                    method = 'interaction/requestPermission'
                params.update(identity)
                rows.append({'id': request_id, 'method': method,
                             'params': params, 'transport': 'desktop_native',
                             **Bridge._native_pending_timestamps(item)})
                seen.add(request_id)
        return rows

    def _native_pending_rows(self, sid):
        workspace, permission = self._native_context(sid)
        reader = getattr(self.desktop_native, 'interactions', None)
        if callable(reader):
            try:
                view = reader(sid, workspace, permission)
            except DesktopNativeError as error:
                if error.code not in ('unsupported', 'unknown_op', 'native_host_update_required'):
                    raise
            else:
                if not isinstance(view, dict) or view.get('sessionId') != sid or not isinstance(view.get('interactions'), list):
                    raise ValueError('native_interactions_shape_invalid')
                rows = self._native_questions_from_snapshot(
                    {'pendingInteractions': view['interactions']}, sid)
                for row in rows:
                    row['options_complete'] = True
                    row['live_native_pending'] = True
                return rows
        snapshot = self.desktop_native.snapshot(sid, workspace, permission)
        rows = self._native_questions_from_snapshot(snapshot, sid)
        for row in rows:
            row['options_complete'] = False
            row['diagnostic'] = 'native_host_update_required_for_complete_options'
        return rows

    def native_pending_for_sid(self, sid):
        """Observer-facing per-session pending facts; never a global engine list."""
        return self._native_pending_rows(sid)

    def _pending_for_job(self, job, sub=None, sessions=None):
        if job.get('transport') == 'desktop_native' and job.get('session_id'):
            return self._native_pending_rows(job['session_id'])
        sessions = (sessions if sessions is not None
                    else self._job_sessions(job, sub))
        if not sessions:
            return []
        return [row for row in self.pending_snapshot()
                if (row.get('params') or {}).get('sessionId') in sessions]

    def _discover_subtree(self, job, sub, deadline=None):
        """Add real DWF actor sessions when an unknown session has a question.

        Ordinary children come from the observer's ``known_child_ids``.  DWF
        actors are not always in ``session/subagents``, so discover them from
        the workflow topology only when a pending request is otherwise
        unattributed; the result is persisted on the subscription to avoid
        repeated discovery.
        """
        sessions = self._job_sessions(job, sub)
        pending = self.pending_snapshot()
        unknown = {str((row.get('params') or {}).get('sessionId') or '')
                   for row in pending} - sessions
        unknown.discard('')
        if not unknown:
            return sessions
        sid = job.get('session_id')
        if not sid:
            return sessions
        now = time.monotonic()
        if now - float(self._subtree_discovery_at.get(job.get('job_id')) or 0) < 5:
            return sessions
        self._subtree_discovery_at[job.get('job_id')] = now
        try:
            result = self._subagents(sid, {'limit': 30, 'workflow_run_limit': 10})
        except Exception:
            return sessions
        actors = {str(row.get('session_id')) for row in result.get('workflow_children') or []
                  if isinstance(row, dict) and row.get('session_id')}
        additions = actors & unknown
        if additions and isinstance(sub, dict) and sub.get('subscription_id'):
            merged = sorted(set(sub.get('known_child_ids') or []) | additions)[:200]
            self.notifications.touch(sub['subscription_id'],
                                     known_child_ids=merged)
        return sessions | additions

    @staticmethod
    def _verification_for_subscription(state, sub):
        reason = str(sub.get('state_reason') or sub.get('terminal_reason') or state)
        if state == 'completed':
            return 'verified', reason or 'completed'
        if state in ('send_not_accepted', 'send_failed'):
            return 'failed', reason or state
        if state in ('cancelled', 'interrupted', 'superseded'):
            return 'cancelled', reason or state
        if state == 'expired':
            return 'expired', reason or 'expired'
        return 'failed', reason or 'failed'

    def _emit_job_hint(self, job, sub, tag):
        """One stable, deduplicated verification hint for a diagnostic state."""
        if not isinstance(sub, dict) or not job.get('session_id'):
            return
        if not sub.get('subscription_id'):
            return
        hint_id = 'superagent:' + str(job.get('job_id') or '') + ':' + str(tag)
        if len(hint_id) > 64:
            return
        try:
            self.notifications.emit_hint(
                job['session_id'], 'needs_verification', hint_id,
                'verification_requested',
                subscription_id=sub.get('subscription_id'))
        except Exception:
            pass

    @staticmethod
    def _bounded_activity(text, limit=150):
        """Keep an audit prefix but hash the full value so changes are never cut away."""
        text = str(text or '')
        if len(text) <= limit:
            return text
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]
        return text[:limit - 13] + '#' + digest

    @staticmethod
    def _cursors_from_subscription(run_cursors, current_epoch=''):
        """Return the current ``run_id -> {epoch, cursor}`` view.

        When the subscription knows its current native/engine epoch, keys from
        that epoch are preferred; otherwise the last inserted entry for a run
        id wins.  This avoids leftover epochs being treated as progress.
        """
        result = {}
        if not isinstance(run_cursors, dict):
            return result
        entries = list(run_cursors.items())
        if current_epoch:
            prefix = str(current_epoch) + ':'
            preferred = [(key, value) for key, value in entries
                         if str(key).startswith(prefix)]
            if preferred:
                entries = preferred
        for key, value in entries:
            full = str(key)
            epoch, run_id = (full.rsplit(':', 1) if ':' in full else ('', full))
            if not run_id:
                continue
            try:
                cursor = max(0, int(value or 0))
            except (TypeError, ValueError):
                cursor = 0
            result[run_id] = {'epoch': epoch, 'cursor': cursor}
        return result

    @staticmethod
    def _snapshot_epoch(snapshot):
        """Best-effort stream/engine epoch for unsubscribed run cursors."""
        runtime = (snapshot or {}).get('runtime') if isinstance(snapshot, dict) else None
        if not isinstance(runtime, dict):
            return ''
        for key in ('streamId', 'serverInstanceId', 'sessionStreamId',
                    'instanceId', 'engineInstanceId'):
            if runtime.get(key):
                return str(runtime[key])
        if runtime.get('identity'):
            return 'native:' + str(runtime['identity']) + ':' + str(
                runtime.get('generation') or '')
        return ''

    @staticmethod
    def _cursors_from_runs(runs, epoch=''):
        """Parse already-read run rows into ``run_id -> {epoch, cursor}``.

        A failed journal read row is skipped: its cursor 0 is not an observed
        value and must not replace the previous watermark.
        """
        result = {}
        for row in runs or ():
            if not isinstance(row, dict) or row.get('read_error'):
                continue
            run_id = str(row.get('run_id') or '')
            if not run_id:
                continue
            try:
                cursor = max(0, int(row.get('cursor') or 0))
            except (TypeError, ValueError):
                cursor = 0
            result[run_id] = {'epoch': str(epoch or ''), 'cursor': cursor}
        return result

    @staticmethod
    def _normalize_previous_watermarks(previous):
        """Normalize stored watermarks, including the earlier flat-key shape."""
        result = {}
        if not isinstance(previous, dict):
            return result
        for key, value in previous.items():
            if isinstance(value, dict):
                run_id = str(key)
                epoch = str(value.get('epoch') or '')
                try:
                    cursor = max(0, int(value.get('cursor') or 0))
                except (TypeError, ValueError):
                    cursor = 0
            else:
                full = str(key)
                epoch, run_id = (full.rsplit(':', 1) if ':' in full else ('', full))
                try:
                    cursor = max(0, int(value or 0))
                except (TypeError, ValueError):
                    cursor = 0
            if run_id:
                result[run_id] = {'epoch': epoch, 'cursor': cursor}
        return result

    @classmethod
    def _advance_watermarks(cls, previous, current):
        """Advance per-run cursors only on real progress.

        Same epoch: only a strictly greater cursor advances.  Epoch change:
        the new epoch establishes a baseline without advancing, even when its
        first cursor is higher; only a later strictly greater cursor in that
        same epoch advances.  New run: only a cursor above zero advances.
        Backward, unchanged, reordered, epoch churn and read-failure cursor 0
        never fabricate progress.
        """
        merged = cls._normalize_previous_watermarks(previous)
        advanced = False
        for run_id, entry in (current or {}).items():
            if not isinstance(entry, dict):
                continue
            run_id = str(run_id)
            if not run_id:
                continue
            epoch = str(entry.get('epoch') or '')
            try:
                cursor = max(0, int(entry.get('cursor') or 0))
            except (TypeError, ValueError):
                cursor = 0
            old = merged.get(run_id)
            if old is None:
                if cursor > 0:
                    advanced = True
                merged[run_id] = {'epoch': epoch, 'cursor': cursor}
            elif epoch != old['epoch']:
                # An epoch change is a new fact, not progress by itself.  The
                # new epoch starts a fresh baseline; only a later strictly
                # greater cursor within that same epoch advances.  This keeps
                # epoch churn from replaying old values forever.
                merged[run_id] = {'epoch': epoch, 'cursor': cursor}
            else:
                if cursor > old['cursor']:
                    advanced = True
                merged[run_id] = {'epoch': epoch,
                                  'cursor': max(old['cursor'], cursor)}
        if len(merged) > 128:
            merged = dict(sorted(merged.items())[:128])
        return advanced, merged

    @staticmethod
    def _child_activity(snapshot):
        """Small, body-free progress metadata from an already-read child snapshot.

        Only scalar projection/runtime/session fields are copied; messages and
        any other content are ignored.  ``eventSeq``/``stateRevision`` keep
        same-message-id streaming observable without reading child bodies.
        """
        if not isinstance(snapshot, dict):
            return ''
        parts = []
        for container_name, keys in (
                ('projection', ('status', 'turnCount', 'eventSeq', 'lastEventSeq',
                                'lastMessageId', 'currentTurnId')),
                ('runtime', ('stateRevision', 'eventSeq', 'lastEventSeq', 'generation')),
                ('session', ('eventSeq', 'lastEventSeq', 'stateRevision'))):
            container = snapshot.get(container_name)
            if not isinstance(container, dict):
                continue
            for key in keys:
                value = container.get(key)
                if value is None or isinstance(value, (dict, list)):
                    continue
                parts.append(container_name + '.' + key + '=' + str(value)[:64])
        return Bridge._bounded_activity(';'.join(parts), limit=140)

    @staticmethod
    def _activity_from_children(child_ids):
        return Bridge._bounded_activity(
            ','.join(sorted(str(value) for value in child_ids or () if value)))

    @staticmethod
    def _merge_activity(progress, updates):
        """Merge available activity sources without losing known values.

        ``None`` means the source could not be read this time; the previous
        value is preserved so a read failure is never mistaken for progress.
        Returns ``(changed, activity)``.
        """
        previous = progress.get('activity') if isinstance(progress, dict) else {}
        previous = previous if isinstance(previous, dict) else {}
        merged = {str(key): str(value)[:160] for key, value in previous.items()
                  if value is not None and str(value) != ''}
        changed = False
        for key, value in updates.items():
            if value is None:
                continue
            value = str(value)[:160]
            if str(previous.get(key) or '') != value:
                changed = True
            if value:
                merged[str(key)] = value
            else:
                merged.pop(str(key), None)
        return changed, merged

    def sync_superagent_jobs(self, now=None):
        """Map persisted subscription truth to job verdicts and diagnostics.

        Only persisted observer evidence may produce ``verified``.  Busy,
        accepted, the first idle and protocol errors never do.  Job transitions
        are deduplicated by store-level event ids; ordinary progress only
        updates ``last_progress``.
        """
        now = float(now if now is not None else time.time())
        try:
            cfg = config()
        except Exception:
            cfg = {}
        try:
            stall_seconds = max(60, int(cfg.get('superagent_stall_seconds', 600)))
        except (TypeError, ValueError):
            stall_seconds = 600
        try:
            failure_threshold = max(2, int(cfg.get('superagent_read_failure_threshold', 5)))
        except (TypeError, ValueError):
            failure_threshold = 5
        for job in self.superagent.all_active():
            thread = (job.get('source') or {}).get('thread_id') or ''
            job_id = job.get('job_id')
            if not thread or not job_id:
                continue
            sub = self._job_subscription(job)
            if sub is None:
                self._sync_unsubscribed_job(job, now, stall_seconds,
                                            failure_threshold)
                continue
            state = str(sub.get('state') or '')
            if state in TERMINAL_STATES:
                if job.get('queue'):
                    self.superagent.mark_queue_not_delivered(
                        job_id, thread, 'agent_terminal')
                status, reason = self._verification_for_subscription(state, sub)
                updated = self.superagent.apply_verification(job_id, thread,
                                                             status, reason)
                if updated is not None and updated.get('last_event'):
                    self._request_desktop_sync(job.get('workspace'),
                                               job.get('session_id'), 'terminal')
                continue
            sessions = self._discover_subtree(job, sub)
            pending = self._pending_for_job(job, sub, sessions)
            if pending:
                self.superagent.transition(job_id, thread, 'action_required',
                                           'pending_request', 'job.action_required')
                continue
            failures = max(int((job.get('read_failures') or {}).get('consecutive') or 0),
                           int(sub.get('consecutive_errors') or 0))
            if failures >= failure_threshold:
                updated = self.superagent.transition(
                    job_id, thread, 'unobservable', 'read_failures',
                    'job.unobservable')
                if updated is not None and updated.get('last_event'):
                    self._emit_job_hint(job, sub, 'unobservable')
                continue
            if job.get('status') == 'action_required':
                job = self.superagent.transition(
                    job_id, thread, 'running', 'action_resolved', 'job.resumed') or job
            if job.get('status') == 'unobservable':
                job = self.superagent.transition(
                    job_id, thread, 'running', 'observer_recovered', 'job.resumed') or job
            progress = job.get('last_progress') or {}
            observed = (str(sub.get('last_status') or ''),
                        str(sub.get('last_message_id') or ''))
            native_state = sub.get('native') if isinstance(sub.get('native'), dict) else {}
            previous_watermarks = progress.get('run_watermarks')
            cursor_advanced, watermarks = self._advance_watermarks(
                previous_watermarks,
                self._cursors_from_subscription(
                    sub.get('run_cursors'), native_state.get('epoch') or ''))
            baseline_changed = (self._normalize_previous_watermarks(previous_watermarks)
                                != watermarks)
            activity_changed, activity = self._merge_activity(progress, {
                'children': self._activity_from_children(sub.get('known_child_ids')),
                'child_stream': str(sub.get('last_child_activity') or ''),
                'message': str(sub.get('last_message_activity') or ''),
            })
            if observed != (str(progress.get('status') or ''),
                            str(progress.get('message_id') or '')) \
                    or cursor_advanced or activity_changed:
                # Only a strictly forward same-epoch cursor, real child/message
                # activity or a parent status change refreshes the stall clock;
                # a run merely staying "running" does not.
                job = self.superagent.record_progress(
                    job_id, thread, kind='observer', message_id=observed[1],
                    status=observed[0], activity=activity,
                    run_watermarks=watermarks, at=now) or job
                progress = job.get('last_progress') or {}
            elif baseline_changed:
                # A new epoch's first cursor is a new fact, not progress.  Keep
                # it so the next same-epoch increase is measurable, but do not
                # refresh ``last_progress.at`` or clear read failures.
                job = self.superagent.update_run_watermarks(
                    job_id, thread, watermarks) or job
                progress = job.get('last_progress') or {}
            last_at = float(progress.get('at') or job.get('created_at') or now)
            if job.get('status') == 'running' and now - last_at >= stall_seconds:
                updated = self.superagent.transition(
                    job_id, thread, 'stalled', 'no_observable_progress',
                    'job.stalled')
                if updated is not None and updated.get('last_event'):
                    self._emit_job_hint(job, sub, 'stalled')
            elif job.get('status') == 'stalled' and now - last_at < stall_seconds:
                self.superagent.transition(job_id, thread, 'running',
                                           'progress_resumed', 'job.resumed')

    def _sync_unsubscribed_job(self, job, now, stall_seconds, failure_threshold,
                               poll_seconds=5):
        """Poll a job with no notification subscription for real diagnostics.

        Diagnostics are decoupled from host wake: this reads the session
        projection and records progress/failures, and can reach stalled,
        unobservable, failed or cancelled.  It never produces ``verified`` from
        an idle assumption; without persisted subscription evidence the job
        stays running and inspect reports ``engine_idle_unverified``.
        """
        thread = (job.get('source') or {}).get('thread_id') or ''
        job_id = job.get('job_id')
        sid = job.get('session_id')
        if not thread or not job_id or not sid:
            return
        observation = job.get('observation') or {}
        checked_at = observation.get('checked_at')
        if isinstance(checked_at, (int, float)) and now - float(checked_at) < poll_seconds:
            return
        try:
            snapshot = self.snapshot(sid)
        except Exception as error:
            self.superagent.update_job(job_id, thread, observation={
                'checked_at': now, 'engine_status': '', 'engine_idle': False,
                'active_background': None, 'last_message_id': '',
                'read_error': type(error).__name__[:80]})
            count = self.superagent.record_read_failure(
                job_id, thread, type(error).__name__)
            if (is_session_not_found_error(error, sid)
                    or is_stale_session_error(error, sid)):
                if job.get('queue'):
                    self.superagent.mark_queue_not_delivered(
                        job_id, thread, 'session_missing')
                self.superagent.transition(job_id, thread, 'failed',
                                           'session_missing', 'job.failed')
            elif count >= failure_threshold:
                self.superagent.transition(job_id, thread, 'unobservable',
                                           'read_failures', 'job.unobservable')
            return
        runs_ok = True
        read_failure_count = 0
        try:
            runs, _ = self._run_rows(sid, strict=True)
        except Exception:
            runs, runs_ok = [], False
            read_failure_count = self.superagent.record_read_failure(
                job_id, thread, 'workflow_read_failed')
        else:
            journal_error = next(
                (str(row.get('read_error')) for row in runs
                 if isinstance(row, dict) and row.get('read_error')), '')
            if journal_error:
                read_failure_count = self.superagent.record_read_failure(
                    job_id, thread, journal_error)
        projection = snapshot.get('projection') or {}
        engine_status = str(projection.get('status') or '').lower()
        active_background = self._active_background(snapshot, runs)
        tail_read_error = ''
        tail_ok = True
        try:
            last_message_id, message_activity, tail_ok = self._tail_activity(
                sid, snapshot=snapshot)
            last_message_id = str(last_message_id or '')
            if not tail_ok:
                tail_read_error = 'messages_unreadable'
                read_failure_count = max(
                    read_failure_count,
                    self.superagent.record_read_failure(job_id, thread, tail_read_error))
        except Exception as error:
            last_message_id, message_activity = '', ''
            tail_ok = False
            tail_read_error = type(error).__name__[:80]
            read_failure_count = max(
                read_failure_count,
                self.superagent.record_read_failure(job_id, thread, tail_read_error))
        # A new child session is progress too.  This is one lightweight list
        # read, not child message bodies; failure keeps the previous value.
        child_ids = None
        try:
            child_rows = self.rpc('session/subagents',
                                  {'sessionId': sid, 'endedLimit': 30}) or {}
            child_ids = [str(value) for value in child_rows.get('childSessionIds') or ()
                         if value]
        except Exception:
            child_ids = None
        # Definite terminal engine truth outranks degraded reads: a failed
        # workflow read must never hide a failed or cancelled turn.
        if engine_status in ('error', 'failed'):
            if job.get('queue'):
                self.superagent.mark_queue_not_delivered(job_id, thread, 'engine_error')
            self.superagent.transition(job_id, thread, 'failed',
                                       'engine_error', 'job.failed')
            return
        if engine_status in ('cancelled', 'canceled'):
            if job.get('queue'):
                self.superagent.mark_queue_not_delivered(job_id, thread, 'engine_cancelled')
            self.superagent.transition(job_id, thread, 'cancelled',
                                       'engine_cancelled', 'job.cancelled')
            return
        # A real pending request outranks read-failure diagnostics.  Only the
        # live pending set may clear it; a failed read is never proof that the
        # question was resolved.
        sessions = self._discover_subtree(job, None)
        pending = (self._native_questions_from_snapshot(snapshot, sid)
                   if self._is_native(sid)
                   else self._pending_for_job(job, None, sessions))
        observation = {
            'checked_at': now, 'engine_status': engine_status,
            'engine_idle': (engine_status in ('idle', 'completed')
                            and not active_background and not tail_read_error
                            and not pending),
            'active_background': active_background,
            'last_message_id': last_message_id,
            **({'read_error': tail_read_error} if tail_read_error else {})}
        if pending:
            self.superagent.update_job(job_id, thread, observation=observation)
            self.superagent.transition(job_id, thread, 'action_required',
                                       'pending_request', 'job.action_required')
            return
        if read_failure_count >= failure_threshold:
            # Keep the poll throttle: an unobservable job must not re-read
            # every observer tick.
            self.superagent.update_job(job_id, thread, observation={
                **observation, 'engine_idle': False,
                'read_error': tail_read_error or 'read_failures'})
            self.superagent.transition(job_id, thread, 'unobservable',
                                       'read_failures', 'job.unobservable')
            return
        previous = job.get('last_progress') or {}
        # A strictly forward same-epoch cursor and same-id message/tool updates
        # are real progress; an idle parent with a static tail is not.  An
        # unreadable source contributes None and therefore preserves the
        # previous value.
        previous_watermarks = previous.get('run_watermarks')
        if runs_ok:
            cursor_advanced, watermarks = self._advance_watermarks(
                previous_watermarks,
                self._cursors_from_runs(runs, self._snapshot_epoch(snapshot)))
        else:
            cursor_advanced = False
            watermarks = previous_watermarks or {}
        baseline_changed = runs_ok and (
            self._normalize_previous_watermarks(previous_watermarks) != watermarks)
        if read_failure_count == 0 and runs_ok and tail_ok and not baseline_changed:
            # A successful read breaks the consecutive-failure streak even
            # when it observes no new progress.  A new-epoch baseline alone
            # must not clear it.
            self.superagent.reset_read_failures(job_id, thread)
            previous = job.get('last_progress') or {}
        activity_changed, activity = self._merge_activity(previous, {
            'children': (self._activity_from_children(child_ids)
                         if child_ids is not None else None),
            'message': ((last_message_id + '#' + message_activity)
                        if tail_ok and last_message_id else ('' if tail_ok else None)),
        })
        status_changed = engine_status != str(previous.get('status') or '')
        message_changed = bool(tail_ok) and (
            last_message_id != str(previous.get('message_id') or ''))
        if status_changed or message_changed or cursor_advanced or activity_changed:
            # The first observation establishes a baseline, not a fresh
            # progress timestamp: a job that was already silent for a full
            # stall window before its first poll must still time out.
            record_at = (now if previous.get('at') is not None
                         else float(job.get('created_at') or now))
            job = self.superagent.record_progress(
                job_id, thread, kind='poll', message_id=last_message_id,
                status=engine_status, activity=activity,
                run_watermarks=watermarks, at=record_at,
                reset_failures=(read_failure_count == 0)) or job
            previous = job.get('last_progress') or {}
        elif baseline_changed:
            # Persist a new epoch/membership baseline only.  This must not
            # refresh the stall clock or clear a read-failure streak.
            job = self.superagent.update_run_watermarks(
                job_id, thread, watermarks) or job
            previous = job.get('last_progress') or {}
        self.superagent.update_job(job_id, thread, observation=observation)
        job = self.superagent.get(job_id, thread) or job
        if str(job.get('status')) == 'action_required':
            job = self.superagent.transition(
                job_id, thread, 'running', 'action_resolved', 'job.resumed') or job
        if str(job.get('status')) == 'unobservable':
            job = self.superagent.transition(
                job_id, thread, 'running', 'observer_recovered', 'job.resumed') or job
        last_at = float(previous.get('at') or job.get('created_at') or now)
        if str(job.get('status')) == 'running' and now - last_at >= stall_seconds:
            self.superagent.transition(job_id, thread, 'stalled',
                                       'no_observable_progress', 'job.stalled')
        elif str(job.get('status')) == 'stalled' and now - last_at < stall_seconds:
            self.superagent.transition(job_id, thread, 'running',
                                       'progress_resumed', 'job.resumed')

    def drain_superagent_queues(self, now=None):
        """Observer hook: drain durable follow-ups for active jobs at idle."""
        for job in self.superagent.all_active():
            if not job.get('queue') or not job.get('session_id'):
                continue
            try:
                self._try_drain_job(job)
            except Exception:
                # One broken job must not stop other delegations' queues.
                continue

    def _agent_message(self, args, meta, deadline=None):
        self.sync_superagent_jobs()
        permission = caller_permission(args)
        source = require_source(meta)
        job = self._agent_job(args, source)
        if str(job.get('status')) in AGENT_TERMINAL_STATUSES:
            raise ValueError('agent_terminal: 该 agent 已结束：' + str(job.get('status')))
        content = args.get('content')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('message_required: 跟进消息不能为空')
        if len(content) > MAX_MESSAGE_CHARS:
            raise ValueError('message_too_large: 单条消息不能超过 ' + str(MAX_MESSAGE_CHARS) + ' 字符')
        sid = job.get('session_id')
        if not sid:
            raise ValueError('agent_not_ready: 该 agent 还没有已绑定会话')
        queued = self.superagent.enqueue_message(
            job['job_id'], source['thread_id'], content,
            idempotency_key=args.get('idempotency_key'),
            caller_permission_mode=permission)
        if queued is None:
            raise ValueError('agent_not_found: 该来源 chat 没有这个 agent')
        drained = self._try_drain_job(job)
        current = self.superagent.get(job['job_id'], source['thread_id']) or job
        view = self._message_view(current, queued['message_id'])
        return {
            'agent_id': job['agent_id'],
            'job_id': job['job_id'],
            'session_id': sid,
            'state': view['state'],
            'message_id': queued['message_id'],
            'position': view['position'],
            'duplicate': queued.get('state') == 'duplicate',
            'retry_required': view['state'] == 'uncertain',
            'queue': self.superagent.job_summary(current).get('queue'),
            'drain': drained,
        }

    def _compact_question(self, question):
        question = question if isinstance(question, dict) else {}
        raw_options = question.get('options')
        if not isinstance(raw_options, list):
            raw_options = []
        options = []
        for option in raw_options[:20]:
            if not isinstance(option, dict):
                continue
            options.append({'label': str(option.get('label') or '')[:200],
                            'value': str(option.get('value') or option.get('label') or '')[:200]})
        return {'question': str(question.get('question') or '')[:500],
                'header': str(question.get('header') or '')[:200],
                'options': options}

    def _compact_request(self, request):
        request = request if isinstance(request, dict) else {}
        params = request.get('params') if isinstance(request.get('params'), dict) else {}
        options = params.get('options')
        if not isinstance(options, list):
            request_options = (params.get('request') or {}).get('options') \
                if isinstance(params.get('request'), dict) else []
            options = request_options if isinstance(request_options, list) else []
        compact_options = []
        full_access = params.get('fullAccessOption')
        if isinstance(full_access, dict) and full_access not in options:
            options = list(options) + [full_access]
        for option in options[:20]:
            if not isinstance(option, dict):
                continue
            compact_options.append({
                'option_id': str(option.get('optionId') or option.get('option_id') or ''),
                'label': str(option.get('label') or option.get('name')
                             or option.get('title') or '')[:200],
            })
        questions = (params.get('input') or {}).get('questions') if isinstance(
            params.get('input'), dict) else []
        if not isinstance(questions, list):
            questions = []
        return {'request_id': str(request.get('id') or ''),
                'method': str(request.get('method') or ''),
                'tool_name': str(params.get('toolName') or ''),
                'received_at': request.get('received_at'),
                'questions': [self._compact_question(row) for row in questions[:10]],
                'options': compact_options}

    def _agent_inspect(self, args, meta, deadline=None):
        self.sync_superagent_jobs()
        source = require_source(meta)
        job = self._agent_job(args, source)
        sid = job.get('session_id')
        thread = source['thread_id']
        last_message_id = ''
        native_snapshot = None
        errors = {}
        if sid:
            try:
                self.ensure_session(sid, deadline)
            except Exception as error:
                errors['session'] = type(error).__name__
                self.superagent.record_read_failure(
                    job['job_id'], thread, type(error).__name__)
        if sid:
            try:
                if self._is_native(sid):
                    native_snapshot = self.snapshot(sid, deadline)
                    last_message_id = self._last_message_id(
                        sid, deadline, snapshot=native_snapshot)
                else:
                    rows = self.rpc('session/messages', {
                        'sessionId': sid, 'limit': 20}, deadline).get('messages') or []
                    for message in reversed(rows):
                        if not is_visible_message(message):
                            continue
                        ident = message_identity(message)
                        if ident:
                            last_message_id = str(ident)
                            break
                previous_message = str((job.get('last_progress') or {}).get('message_id') or '')
                if last_message_id and last_message_id != previous_message:
                    self.superagent.record_progress(
                        job['job_id'], thread, kind='inspect',
                        message_id=last_message_id, status='observed')
            except Exception as error:
                errors['messages'] = type(error).__name__
                self.superagent.record_read_failure(
                    job['job_id'], thread, type(error).__name__)
        sub_id = (job.get('notification') or {}).get('subscription_id') or ''
        sub = self.notifications.find_subscription(sub_id, thread) if sub_id else None
        if sub is None and sid:
            sub = self.notifications.latest_subscription_for(sid, thread)
        try:
            if sid and self._is_native(sid):
                if native_snapshot is None:
                    # An unavailable ready snapshot is a read error, not proof
                    # of no pending interaction or a completed/idle task.
                    errors['pending'] = errors.get('messages', 'native_snapshot_unavailable')
                    pending = []
                else:
                    pending = [self._compact_request(row) for row in
                               self._native_questions_from_snapshot(native_snapshot, sid)]
            else:
                pending = [self._compact_request(row)
                           for row in self._pending_for_job(
                               job, sub, self._discover_subtree(job, sub, deadline))]
        except Exception as error:
            pending = []
            errors['pending'] = type(error).__name__
            self.superagent.record_read_failure(job['job_id'], thread, type(error).__name__)
        children, child_ids = ([], [])
        runs, run_truncated = ([], False)
        if sid:
            try:
                children, child_ids = self._children_evidence(sid, deadline, strict=True)
            except Exception as error:
                errors['subagents'] = type(error).__name__
                self.superagent.record_read_failure(
                    job['job_id'], thread, type(error).__name__)
            try:
                runs, run_truncated = self._run_rows(sid, deadline, strict=True)
            except Exception as error:
                errors['workflows'] = type(error).__name__
                self.superagent.record_read_failure(
                    job['job_id'], thread, type(error).__name__)
        # Reflect progress/failure writes made by this same inspection.
        job = self.superagent.get(job['job_id'], thread) or job
        include_report = bool(args.get('include_report'))
        events = []
        for event in self.notifications.all_events(thread):
            if event.get('session_id') != sid:
                continue
            if sub_id and event.get('subscription_id') != sub_id:
                continue
            events.append({'event_id': event.get('event_id'),
                           'kind': event.get('kind'),
                           'status': event.get('status'),
                           'reason_code': event.get('reason_code'),
                           'terminal': bool(event.get('terminal')),
                           'created_at': event.get('created_at'),
                           'delivery': (event.get('delivery') or {}).get('state')})
        reported = job.get('report')
        status = str(job.get('status') or '')
        terminal = status in TERMINAL_JOB_STATUSES
        if status in TERMINAL_JOB_STATUSES:
            verdict, confirmed = status, status == 'verified'
        elif status == 'reported':
            verdict, confirmed = 'reported_unverified', False
        elif status in ('action_required', 'stalled', 'unobservable', 'send_uncertain'):
            verdict, confirmed = status, False
        elif sub is None and (job.get('observation') or {}).get('engine_idle'):
            # No subscription evidence: idle-looking is not verified completion.
            verdict, confirmed = 'engine_idle_unverified', False
        elif sub is not None and sub.get('state') in TERMINAL_STATES:
            verdict = str(sub.get('state'))
            terminal = True
            confirmed = verdict == 'completed'
        elif reported:
            verdict, confirmed = 'reported_unverified', False
        else:
            verdict, confirmed = status or 'unknown', False
        return {
            'job': self.superagent.job_summary(job, include_report=include_report),
            'verdict': verdict,
            'confirmed': confirmed,
            'terminal': terminal,
            'success': verdict == 'verified',
            'verification_scope': 'turn_lifecycle',
            'task_success': None,
            'task_review': 'needs_parent_review',
            'last_message_id': last_message_id,
            'errors': errors,
            'pending': pending,
            'subagents': {'children': children[:20], 'child_count': len(child_ids),
                          'truncated': len(children) > 20,
                          'read_error': errors.get('subagents')},
            'workflows': {'runs': runs[:10], 'run_count': len(runs),
                          'truncated': bool(run_truncated or len(runs) > 10),
                          'read_error': errors.get('workflows')},
            'subscription': {
                'subscription_id': (sub or {}).get('subscription_id'),
                'state': (sub or {}).get('state'),
                'state_reason': (sub or {}).get('state_reason'),
                'terminal_event_id': (sub or {}).get('terminal_event_id'),
            },
            'events': events[-20:],
            'desktop_sync': self.desktop_sync_status(),
        }

    # Credential and preference wording is classified only from the actual
    # human-facing question copy.  Tool arguments, workflow code, paths,
    # variable names and logs are data, never an authentication question.
    AUTH_QUESTION_MARKERS = (
        'password', 'passcode', 'passphrase', 'api key', 'api_key', 'api-key',
        'api token', 'api_token', 'access token', 'access_token',
        'auth token', 'auth_token', 'refresh token', 'refresh_token',
        'bearer token', 'bearer_token', 'id token', 'id_token',
        'client secret', 'client_secret', 'secret key', 'secret_key',
        'private key', 'private_key', 'token', 'secret', 'oauth',
        'log in', 'login', 'sign in', 'signin', 'credential', 'authenticate',
        'authentication', 'authurl', '密码', '口令', '登录', '凭证', '鉴权',
    )
    PREFERENCE_QUESTION_MARKERS = (
        'prefer', 'preference', 'favorite', 'favourite', 'which do you',
        'your choice', '偏好', '你喜欢',
    )

    @classmethod
    def _question_copy(cls, request):
        """Return only the human-authored question/option copy of a request.

        Notification and permission payloads carry real tool input, including
        workflow scripts.  Only question rows, headers and option labels or
        descriptions are human questions; option ``value`` and any other
        parameter are machine data.
        """
        pieces = []
        for row in cls._request_questions(request):
            for key in ('question', 'header'):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    pieces.append(value)
            options = row.get('options')
            if isinstance(options, list):
                for option in options:
                    if not isinstance(option, dict):
                        continue
                    for key in ('label', 'description'):
                        value = option.get(key)
                        if isinstance(value, str) and value.strip():
                            pieces.append(value)
        return '\n'.join(pieces).lower()

    @classmethod
    def _question_has_sensitive_flag(cls, request):
        """True when the real question row itself marks its input sensitive."""
        for row in cls._request_questions(request):
            if any(row.get(key) is True for key in
                   ('sensitive', 'secret', 'isSecret', 'password')):
                return True
        return False

    def _requires_user_answer(self, request, args):
        """True only when the actual human question needs the person's answer.

        ``interaction/requestPermission`` is a technical tool approval: its
        input is arbitrary code/data and it never supplies credentials, so it
        is classified by ``_authorize_native_permission`` or the engine's real
        option mapping instead.  User-input questions are classified from the
        question copy and the row's own sensitive flag alone.  An unreadable
        user-input structure stays conservative: without a question we cannot
        prove it is not a credential prompt.
        """
        if str(request.get('method') or '') == 'interaction/requestPermission':
            return False
        questions = self._request_questions(request)
        if not questions:
            return True
        if self._question_has_sensitive_flag(request):
            return True
        text = self._question_copy(request)
        if not text:
            return True
        if any(marker in text for marker in self.AUTH_QUESTION_MARKERS):
            return True
        if any(marker in text for marker in self.PREFERENCE_QUESTION_MARKERS) \
                and args.get('user_approved') is not True:
            return True
        return False

    @staticmethod
    def _request_questions(request):
        """Return the real question rows for one pending request, if any."""
        params = request.get('params') if isinstance(request.get('params'), dict) else {}
        for key in (None, 'input', 'request'):
            nested = params if key is None else params.get(key)
            if isinstance(nested, dict) and isinstance(nested.get('questions'), list):
                rows = [row for row in nested['questions'] if isinstance(row, dict)]
                if rows:
                    return rows
        return []

    def _single_question_response(self, request, answer_text):
        """Build the native single-question response; never guess an answer."""
        if request.get('method') == 'interaction/requestPermission':
            raise ValueError('answer_requires_option: 权限请求请使用实际 option_id')
        questions = self._request_questions(request)
        if len(questions) != 1:
            raise ValueError('answer_ambiguous: 必须恰好一道题；多题或缺少题面请提供显式 response')
        question = questions[0]
        if question.get('multiSelect') is True:
            raise ValueError('answer_requires_response: 多选问题不能压缩成单题 answer')
        options = question.get('options')
        if not isinstance(options, list) or not options:
            raise ValueError('answer_requires_response: 该问题没有可映射的选项，请提供显式 response')
        text = str(question.get('question') or '')
        if not text.strip():
            raise ValueError('answer_ambiguous: 题面缺失，不能生成 answers 映射')
        wanted = answer_text.strip()
        matches = []
        for option in options:
            if not isinstance(option, dict):
                continue
            label = str(option.get('label') or '').strip()
            value = str(option.get('value') or '').strip()
            if wanted and wanted in (label, value):
                matches.append(option)
        if len(matches) != 1:
            labels = [str(row.get('label') or row.get('value') or '') for row in options
                      if isinstance(row, dict)]
            raise ValueError('answer_option_invalid: answer 必须匹配唯一实际选项：'
                             + ' / '.join(label for label in labels if label))
        selected = matches[0]
        chosen = selected.get('value')
        if not isinstance(chosen, str):
            chosen = str(selected.get('label') or '')
            if not chosen.strip():
                raise ValueError('answer_option_invalid: 匹配到的选项没有可用值')
        return {'action': 'accept', 'content': {'answers': {text: chosen}}}

    @staticmethod
    def _native_permission_options(request):
        """Return the real option rows of one pending native permission request."""
        params = request.get('params') if isinstance(request.get('params'), dict) else {}
        rows = [row for row in (params.get('options') or ())
                if isinstance(row, dict)]
        full_access = params.get('fullAccessOption')
        if isinstance(full_access, dict):
            rows.append(full_access)
        elif params.get('modeSwitch') == 'fullAccessOnly':
            # The Desktop's permission UI presents this SDK-declared mode switch
            # as the fixed fullAccess option even without a separate option row.
            rows.append({'optionId': NATIVE_FULL_ACCESS_OPTION, 'kind': 'custom',
                         'label': 'Full access', 'response': {'decision': 'deny'}})
        if isinstance(params.get('workflowRevisionOption'), dict):
            revision = params['workflowRevisionOption']
            original = next((row for row in rows if row.get('optionId') == revision.get('optionId')), {})
            rows.insert(0, {**original, **revision})
        return rows

    @staticmethod
    def _native_option_is_deny(option):
        """True only for an explicitly denying permission choice."""
        kind = str(option.get('kind') or '')
        if kind.lower().startswith(('deny', 'reject')):
            return True
        response = option.get('response')
        if isinstance(response, dict):
            return str(response.get('decision') or '') == 'deny'
        return False

    def _authorize_native_permission(self, request, answer, args, permission):
        """Validate a native permission answer against the actual pending request.

        The engine itself maps the fullAccess option to yolo, so this must be
        enforced before the resolve command leaves the bridge.  A report or an
        explicit ``response`` cannot bypass it.
        """
        params = request.get('params') or {}
        action = str(answer.get('action') or '')
        if action in NATIVE_DENY_CHOICES:
            if 'content' in answer:
                raise ValueError('invalid_answer: 拒绝或取消不能携带 content')
            if 'freeText' in answer and params.get('freeText') is not True:
                raise ValueError('invalid_answer: 该请求不接受拒绝原因')
            return
        options = self._native_permission_options(request)
        if not options:
            raise ValueError('permission_unverifiable: 该权限请求没有可验证的选项')
        if 'content' in answer and params.get('freeText') is not True and not params.get('workflowRevisionOption'):
            raise ValueError('invalid_answer: 该请求不支持结构化修改')
        option_id = str(answer.get('optionId') or '')
        if not option_id:
            raise ValueError('answer_requires_option: 权限请求必须选择实际 option_id')
        selected = next((row for row in options
                         if str(row.get('optionId') or '') == option_id), None)
        if selected is None:
            raise ValueError('option_not_in_request: option_id 不属于该权限请求')
        if selected.get('requiresFreeText') and not str(answer.get('freeText') or '').strip():
            raise ValueError('invalid_answer: 提出修改必须包含具体反馈')
        if 'freeText' in answer and not selected.get('requiresFreeText') and params.get('freeText') is not True:
            raise ValueError('invalid_answer: 该请求不支持自由文本')
        if option_id == NATIVE_FULL_ACCESS_OPTION:
            # A real mode escalation to yolo; only an actual full_access
            # caller grant may cross it, never just user_approved.
            if permission != 'full_access':
                raise ValueError(
                    'permission_escalation_denied: fullAccess 不得越过当前 caller 权限')
            return
        if self._native_option_is_deny(selected):
            return
        # Any other choice is allow-like or has unknown semantics: fail closed
        # behind read_only and behind an explicit current-turn user approval.
        if permission == 'read_only':
            raise ValueError('read_only_answer_not_allowed: read_only 只允许拒绝或取消')
        if permission != 'full_access' and args.get('user_approved') is not True:
            raise ValueError(
                'user_approved_required: 非 full_access 的权限允许需要真实用户批准')

    def _resolve_native_answer(self, request, args, permission):
        sid = str(request.get('params', {}).get('sessionId') or '')
        workspace, _stored_permission = self._native_context(sid)
        request_id = str(request.get('id') or '')
        if self._requires_user_answer(request, args):
            raise ValueError('user_answer_required: 认证或真实用户偏好必须由用户本人回答')
        answer_text = args.get('answer')
        option_id = args.get('option_id')
        response = args.get('response')
        if answer_text is not None:
            if not isinstance(answer_text, str) or not answer_text.strip():
                raise ValueError('answer_invalid: answer 必须是非空字符串')
            if option_id is not None or response is not None:
                raise ValueError('answer_conflict: answer 不能与 option_id/response 同时使用')
            response = self._single_question_response(request, answer_text)
        if option_id is not None and response is not None:
            raise ValueError('answer_conflict: option_id 不能与 response 同时使用')
        native_answer = {}
        if option_id is not None:
            native_answer['optionId'] = str(option_id)
        elif response is not None:
            if not isinstance(response, dict):
                raise ValueError('invalid_answer: response 必须是对象')
            for key in ('optionId', 'freeText', 'action', 'content'):
                if response.get(key) is not None:
                    native_answer[key] = response[key]
            if not native_answer:
                raise ValueError('invalid_answer: 原生响应只能包含 optionId/freeText/action/content')
        else:
            raise ValueError('answer_required: 必须提供 answer、option_id 或 response')
        is_permission = request.get('method') == 'interaction/requestPermission'
        special = request.get('method') in (
            'interaction/requestPluginInstall', 'interaction/requestWorkspaceHookReview')
        if request.get('method') == 'interaction/unsupported':
            raise ValueError('unsupported_interaction_kind: 请在 ZCode 原生界面处理该交互')
        if special:
            hook = request['method'] == 'interaction/requestWorkspaceHookReview'
            decision = native_answer.get('content', {})
            if hook:
                if not isinstance(decision, dict):
                    raise ValueError('invalid_hook_response')
                refuses = (decision.get('action') == 'revoke' or
                           decision.get('action') == 'toggle' and decision.get('enabled') is False)
            else:
                refuses = (native_answer.get('action') in NATIVE_DENY_CHOICES or
                           native_answer.get('optionId') == 'skip')
            if not refuses:
                if permission == 'read_only':
                    raise ValueError('read_only_answer_not_allowed')
                if permission != 'full_access' and args.get('user_approved') is not True:
                    raise ValueError('user_approved_required')
        if is_permission:
            action = str(native_answer.get('action') or '')
            if action in NATIVE_DENY_CHOICES:
                # A refusal must not smuggle an option that could escalate.
                native_answer = {'action': action, **({'freeText': native_answer['freeText']}
                    if 'freeText' in native_answer else {})}
            elif 'freeText' in native_answer and 'optionId' not in native_answer:
                revision = request.get('params', {}).get('workflowRevisionOption')
                if isinstance(revision, dict):
                    native_answer['optionId'] = revision['optionId']
            self._authorize_native_permission(request, native_answer, args, permission)
        elif not special and 'optionId' in native_answer:
            options = request.get('params', {}).get('options') or []
            if not any(row.get('optionId') == native_answer['optionId'] for row in options
                       if isinstance(row, dict)):
                raise ValueError('option_not_in_request: 用户输入选项不属于当前问题')
        result = self.desktop_native.resolve(
            sid, workspace, permission, request_id, native_answer,
            request_id='resolve_' + uuid.uuid4().hex,
            user_approved=args.get('user_approved') is True)
        return result

    def _native_agent_answer(self, args, job, source, permission):
        sid = str(job.get('session_id') or '')
        request_id = str(args.get('request_id') or '')
        rows = self._native_pending_rows(sid)
        request = next((row for row in rows if row.get('id') == request_id), None)
        if request is None:
            raise ValueError('request_not_pending: 该 request_id 不存在或已经处理')
        result = self._resolve_native_answer(request, args, permission)
        self.superagent.record_progress(job['job_id'], source['thread_id'],
                                        kind='answer', status='answered')
        return {'answered': True, 'transport': 'desktop_native',
                'agent_id': job['agent_id'], 'job_id': job['job_id'],
                'session_id': sid, 'request_id': request_id,
                'native_result': result,
                'job': self.superagent.job_summary(
                    self.superagent.get(job['job_id'], source['thread_id']) or job)}

    def _agent_answer(self, args, meta, deadline=None):
        self.sync_superagent_jobs()
        permission = caller_permission(args)
        source = require_source(meta)
        job = self._agent_job(args, source)
        if str(job.get('status')) in AGENT_TERMINAL_STATUSES:
            raise ValueError('agent_terminal: 该 agent 已结束：' + str(job.get('status')))
        if job.get('transport') == 'desktop_native':
            return self._native_agent_answer(args, job, source, permission)
        sid = job.get('session_id')
        request_id = str(args.get('request_id') or '')
        if not request_id:
            raise ValueError('request_not_pending: 必须提供真实 request_id')
        with self.lock:
            request = copy.deepcopy(self.pending.get(request_id))
        if request is None:
            raise ValueError('request_not_pending: 该 request_id 不存在或已经处理')
        params = request.get('params') if isinstance(request.get('params'), dict) else {}
        sub = self._job_subscription(job)
        sessions = self._discover_subtree(job, sub, deadline)
        request_session = str(params.get('sessionId') or '')
        if not sessions or request_session not in sessions:
            raise ValueError('request_not_owned: 该请求不属于这个 agent 的会话或已知子树')
        if self._requires_user_answer(request, args):
            raise ValueError('user_answer_required: 认证或真实用户偏好必须由用户本人回答')
        answer_text = args.get('answer')
        option_id = args.get('option_id')
        response = args.get('response')
        if answer_text is not None:
            if not isinstance(answer_text, str) or not answer_text.strip():
                raise ValueError('answer_invalid: answer 必须是非空字符串')
            if option_id is not None or response is not None:
                raise ValueError('answer_conflict: answer 不能与 option_id/response 同时使用')
            response = self._single_question_response(request, answer_text)
        answer = {'request_id': request_id, 'caller_permission_mode': permission}
        for key, value in (('option_id', option_id), ('response', response),
                           ('user_approved', args.get('user_approved'))):
            if value is not None:
                answer[key] = value
        result = self.call('zcode_answer', answer, context=meta)
        self.superagent.record_progress(job['job_id'], source['thread_id'],
                                        kind='answer', status='answered')
        return {'answered': bool(result.get('answered')),
                'agent_id': job['agent_id'], 'job_id': job['job_id'],
                'session_id': sid, 'request_id': request_id,
                'job': self.superagent.job_summary(
                    self.superagent.get(job['job_id'], source['thread_id']) or job)}

    @staticmethod
    def _owned_background_ids(snapshot):
        """Exact task ids for active, cancellable work in one session snapshot."""
        ids = []
        for row in ((snapshot or {}).get('projection') or {}).get('backgroundJobs') or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get('status') or row.get('state') or '').lower()
            if status in ('completed', 'complete', 'succeeded', 'success', 'successful',
                          'done', 'failed', 'error', 'cancelled', 'canceled'):
                continue
            if row.get('cancellable') is False:
                continue
            task_id = (row.get('taskId') or row.get('task_id') or row.get('jobId')
                       or row.get('job_id') or row.get('workId') or row.get('id'))
            if isinstance(task_id, str) and task_id.strip() and task_id not in ids:
                ids.append(task_id.strip())
        return ids

    def _cancel_owned_background(self, sid, snapshot, runs, deadline=None,
                                 settle_seconds=20.0):
        """Cancel this session's owned native background tasks and wait for settle.

        Only ids read from this session's own projection are sent to
        ``session/cancelBackgroundTask``.  No process is killed and no other
        session is touched.  ``settled`` is true only after a fresh read shows
        no active background jobs and no unsettled workflow runs.
        """
        requested, errors = [], []
        for task_id in self._owned_background_ids(snapshot):
            try:
                self.rpc('session/cancelBackgroundTask',
                         {'sessionId': sid, 'taskId': task_id}, deadline)
                requested.append(task_id)
            except Exception as error:
                errors.append({'task_id': task_id,
                               'error': (type(error).__name__ + ': '
                                         + str(error)[:300])})
        budget = settle_seconds
        if deadline is not None:
            budget = max(1.0, min(budget, float(deadline) - time.monotonic() - 1.0))
        settle_deadline = time.monotonic() + budget
        remaining = {'background_ids': [],
                     'active_runs': [row.get('run_id') for row in runs
                                     if not row.get('settled')]}
        while True:
            try:
                latest = self.snapshot(sid, deadline)
                latest_runs, _ = self._run_rows(sid, deadline, strict=True)
                remaining = {
                    'background_ids': self._owned_background_ids(latest),
                    'active_runs': [row.get('run_id') for row in latest_runs
                                    if not row.get('settled')],
                }
                if not self._active_background(latest, latest_runs):
                    return {'settled': True, 'requested': requested,
                            'errors': errors, 'remaining': remaining}
            except Exception as error:
                remaining = {'error': type(error).__name__ + ': ' + str(error)[:300]}
            if time.monotonic() >= settle_deadline:
                return {'settled': False, 'requested': requested,
                        'errors': errors, 'remaining': remaining}
            time.sleep(.25)

    def _native_agent_cancel(self, args, job, source, permission):
        sid = str(job.get('session_id') or '')
        thread = source['thread_id']
        workspace, _stored = self._native_context(sid)
        try:
            snapshot = self.desktop_native.snapshot(sid, workspace, permission)
        except DesktopNativeError as error:
            return {'cancelled': False, 'pending': True, 'transport': 'desktop_native',
                    'reason': error.code, 'detail': error.detail,
                    'agent_id': job['agent_id'], 'job_id': job['job_id'],
                    'session_id': sid}
        if self._active_background(snapshot, []):
            return {'cancelled': False, 'pending': True, 'transport': 'desktop_native',
                    'reason': 'needs_native_ui',
                    'detail': '原生后端未暴露后台任务取消；未执行前台 stop',
                    'agent_id': job['agent_id'], 'job_id': job['job_id'],
                    'session_id': sid}
        try:
            result = self.desktop_native.cancel(
                sid, workspace, permission,
                request_id='cancel_' + uuid.uuid4().hex)
        except DesktopNativeError as error:
            return {'cancelled': False, 'pending': error.code in ('unknown', 'request_not_found'),
                    'transport': 'desktop_native', 'reason': error.code,
                    'detail': error.detail, 'agent_id': job['agent_id'],
                    'job_id': job['job_id'], 'session_id': sid}
        subscription_id = (job.get('notification') or {}).get('subscription_id') or ''
        subscription_cancelled = False
        if subscription_id:
            subscription_cancelled = bool(self.notifications.cancel(subscription_id, thread))
        self.superagent.mark_queue_not_delivered(job['job_id'], thread, 'cancelled')
        cancelled = self.superagent.transition(
            job['job_id'], thread, 'cancelled',
            str(args.get('reason') or 'cancelled_by_parent'), 'job.cancelled')
        with self.lock:
            self.sessions.setdefault(sid, {})['caller_permission_mode'] = permission
        self._save_sessions()
        return {'cancelled': True, 'transport': 'desktop_native',
                'agent_id': job['agent_id'], 'job_id': job['job_id'],
                'session_id': sid, 'native_result': result,
                'subscription_cancelled': subscription_cancelled,
                'job': self.superagent.job_summary(cancelled or job)}

    def _agent_cancel(self, args, meta, deadline=None):
        self.sync_superagent_jobs()
        permission = caller_permission(args)
        source = require_source(meta)
        job = self._agent_job(args, source)
        thread = source['thread_id']
        if str(job.get('status')) in AGENT_TERMINAL_STATUSES:
            return {'cancelled': False, 'agent_id': job['agent_id'],
                    'job_id': job['job_id'], 'status': job.get('status')}
        if job.get('transport') == 'desktop_native':
            return self._native_agent_cancel(args, job, source, permission)
        sid = job.get('session_id')
        stopped = False
        if sid:
            shared = [row for row in self.superagent.list(thread, 200)
                      if row.get('session_id') == sid
                      and row.get('job_id') != job['job_id']
                      and str(row.get('status')) in ACTIVE_STATUSES]
            if shared:
                raise ValueError('session_shared: 该会话还被其他 agent 使用，未执行 cancel')
            try:
                snapshot = self.snapshot(sid, deadline)
                runs, _ = self._run_rows(sid, deadline, strict=True)
                background_active = self._active_background(snapshot, runs)
            except Exception as error:
                return {'cancelled': False, 'stopped': False,
                        'agent_id': job['agent_id'], 'job_id': job['job_id'],
                        'reason': 'background_unverified',
                        'stop_error': str(error)[:500]}
            if background_active:
                # Cancel exactly the owned background tasks, then verify the
                # subtree settles.  A failed or unmappable cancellation stays
                # explicitly pending; session/stop is never used to claim it.
                outcome = self._cancel_owned_background(sid, snapshot, runs, deadline)
                if not outcome.get('settled'):
                    return {'cancelled': False, 'stopped': False,
                            'background_active': True, 'pending': True,
                            'reason': ('background_cancel_pending'
                                       if (outcome.get('requested')
                                           or outcome.get('errors'))
                                       else 'background_cancel_unmapped'),
                            'requested': outcome.get('requested') or [],
                            'cancel_errors': outcome.get('errors') or [],
                            'remaining': outcome.get('remaining'),
                            'agent_id': job['agent_id'], 'job_id': job['job_id'],
                            'session_id': sid, 'status': job.get('status')}
            try:
                self.call('zcode_stop', {
                    'session_id': sid, 'caller_permission_mode': permission,
                }, context=meta)
                stopped = True
            except Exception as error:
                return {'cancelled': False, 'agent_id': job['agent_id'],
                        'job_id': job['job_id'], 'status': job.get('status'),
                        'stop_error': str(error)[:500]}
        subscription_id = (job.get('notification') or {}).get('subscription_id') or ''
        had_subscription = bool(subscription_id)
        if subscription_id:
            self.notifications.cancel(subscription_id, thread)
        subscription_cancelled = had_subscription
        self.superagent.mark_queue_not_delivered(
            job['job_id'], thread, 'cancelled')
        cancelled = self.superagent.transition(
            job['job_id'], thread, 'cancelled',
            str(args.get('reason') or 'cancelled_by_parent'), 'job.cancelled')
        self._request_desktop_sync(job.get('workspace'), sid, 'cancel')
        return {'cancelled': True, 'agent_id': job['agent_id'],
                'job_id': job['job_id'], 'session_id': sid,
                'subscription_cancelled': subscription_cancelled,
                'job': self.superagent.job_summary(cancelled or job)}

    def _agent_report(self, args, meta):
        self.sync_superagent_jobs()
        agent_id = str(args.get('agent_id') or '')
        explicit_session = str(args.get('session_id') or '')
        if not agent_id:
            raise ValueError('agent_not_found: 必须提供 agent_id')
        if explicit_session:
            job = self.superagent.find_for_report(agent_id, explicit_session)
            if job is None:
                if self.superagent.find_agent_any(agent_id) is not None:
                    raise ValueError('session_mismatch: session_id 与该 agent 的绑定不一致')
                raise ValueError('agent_not_found: 没有匹配的 agent')
            session_id = explicit_session
            session_source = 'explicit'
        else:
            # agent_id already identifies one private job; the child does not
            # need to guess its own DWF session id.
            job = self.superagent.find_agent_any(agent_id)
            if job is None:
                raise ValueError('agent_not_found: 没有匹配的 agent')
            session_id = str(job.get('session_id') or '')
            session_source = 'bound'
        result = args.get('result')
        if not isinstance(result, dict):
            raise ValueError('result_required: 报告必须是结构化对象')
        fields = sorted(str(key) for key in result)[:30]
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False,
                                           default=str).encode('utf-8')).hexdigest()
        source = source_from_context(meta)
        origin = str((source or {}).get('thread_origin') or '')
        trusted_source = source if source and origin != 'env_fallback' else None
        source_matches = bool(trusted_source and trusted_source['thread_id']
                              == (job.get('source') or {}).get('thread_id'))
        attribution = 'verified' if source_matches else 'unverified'
        bounded, truncated = bounded_report_payload(result)
        bounded_bytes = len(json.dumps(bounded, ensure_ascii=False, sort_keys=True,
                                       default=str).encode('utf-8'))
        report = {
            'state': 'unverified_hint',
            'at': time.time(),
            'sha256': digest,
            'fields': fields,
            'reported_status': str(args.get('status') or '')[:40],
            'source_matches_job': source_matches,
            'sender_attribution': attribution,
            'session_source': session_source,
            'session_matches_job': True,
            'payload_bytes': bounded_bytes,
            'payload_truncated': truncated,
        }
        self.superagent.record_report(job['job_id'], report, {
            'payload': bounded,
            'truncated': truncated,
            'bytes': bounded_bytes,
            'at': time.time(),
            'untrusted': True,
        })
        subscription_id = (job.get('notification') or {}).get('subscription_id') or ''
        if not subscription_id:
            sub = self.notifications.active_subscription_for(
                session_id, (job.get('source') or {}).get('thread_id') or '')
            subscription_id = (sub or {}).get('subscription_id') or ''
        poll_requested = False
        sub = self.notifications.find_subscription(
            subscription_id, (job.get('source') or {}).get('thread_id') or '') \
            if subscription_id else None
        if sub is not None and sub.get('state') not in TERMINAL_STATES:
            if self.notifications.touch(subscription_id,
                                        next_poll_at=time.time()) is not None:
                poll_requested = True
        current = self.superagent.get(job['job_id'],
                                      (job.get('source') or {}).get('thread_id') or '') or job
        return {'recorded': True, 'accepted_as': 'unverified_hint', 'verified': False,
                'agent_id': agent_id, 'session_id': session_id,
                'report_sha256': digest, 'report': report,
                'sender': {'attribution': attribution,
                           'source_matches_job': source_matches,
                           'session_source': session_source,
                           'session_id': session_id},
                'payload_bytes': bounded_bytes, 'payload_truncated': truncated,
                'payload_read': 'zcode_agent_inspect(include_report=true)',
                'poll_requested': poll_requested,
                'subscription_id': subscription_id or None,
                'job_status': current.get('status')}

    def _request_desktop_sync(self, workspace, session_id, reason):
        adapter = getattr(self, 'desktop_sync', None)
        if adapter is None or not workspace or not session_id:
            return
        # The legacy sync adapter must never be used on a native-owned SID.
        if self._is_native(session_id):
            return
        try:
            adapter.request_sync(workspace, [session_id], reason=reason)
        except Exception:
            # The optional sync adapter never blocks a delegation or a send.
            pass

    def desktop_sync_status(self):
        return self.desktop_sync.status()

    def _workflows(self, args):
        operation = args.get('operation', 'runs')
        if operation == 'topology':
            return self._workflow_topology(args['session_id'], args['run_id'])
        native = {'events': 'workflowRunEvents', 'workspace_nodes': 'workflowRunWorkspace',
                  'node_result': 'workflowRunNodeResult', 'session_runs': 'workflowRuns'}
        if operation in native:
            sid = args['session_id']
            self.ensure_session(sid)
            params = {'sessionId': sid}
            if operation != 'session_runs':
                params['runId'] = args['run_id']
            if operation == 'events':
                params.update(afterSequence=max(0, int(args.get('after_sequence', 0))),
                              limit=min(500, max(1, int(args.get('limit', 100)))))
            if operation == 'node_result':
                params.update(siteId=args['site_id'], ordinal=args['ordinal'])
            if operation == 'session_runs':
                params['limit'] = min(64, max(1, int(args.get('limit', 20))))
            return self.rpc('v4/conversation/'+native[operation], params)
        method = {'runs': 'workflows/runs', 'list': 'workflows/list',
                  'get': 'workflows/get', 'for_run': 'workflows/forRun'}.get(operation)
        if method is None:
            raise ValueError('未知工作流读取操作')
        if args.get('params'):
            params = args['params']
        else:
            root = args.get('workspace')
            if not root and args.get('session_id'):
                root = self.sessions.get(args['session_id'], {}).get('workspace')
            root = str(Path(root or config()['default_workspace']).resolve())
            params = {'workspace': {'workspacePath': root, 'workspaceKey': root}}
            if operation in ('runs', 'list', 'get'):
                params['scope'] = args.get('scope', 'project')
            if operation == 'runs':
                params['limit'] = min(50, max(1, int(args.get('limit', 20))))
            if args.get('name'):
                params['name'] = args['name']
            if operation == 'for_run':
                params['runId'] = args['run_id']
        result = self.rpc(method, params)
        if not args.get('include_script') and isinstance(result, dict) and 'script' in result:
            result = {k:v for k,v in result.items() if k != 'script'}
            result['script_omitted'] = True
        if args.get('session_id'):
            result['subagents'] = self._subagents(args['session_id'], args)
        return result

    def _workflow_topology(self, sid, run_id):
        self.ensure_session(sid)
        events, cursor, more = [], 0, True
        while more and len(events) < 5000:
            page = self.rpc('v4/conversation/workflowRunEvents', {'sessionId':sid,'runId':run_id,
                'afterSequence':cursor,'limit':500})
            rows = page.get('events', [])
            if not rows:
                more = False
                break
            events.extend(rows)
            cursor = rows[-1]['sequence']
            more = page.get('hasMore', False)
        result = topology(run_id, events)
        result.update(after_sequence=cursor, more=more)
        return result

    def _subagents(self, sid, args):
        result = self.rpc('session/subagents', {'sessionId':sid,
            'endedLimit':min(100,max(1,int(args.get('limit',30)))),
            **({'endedCursor':args['cursor']} if args.get('cursor') else {})})
        runs = self.rpc('v4/conversation/workflowRuns', {'sessionId':sid,'limit':min(64,max(1,int(args.get('workflow_run_limit',10))))})
        children = []
        native_parent = self._is_native(sid)
        for run in runs.get('runs', []):
            for actor in self._workflow_topology(sid, run['runId'])['actors']:
                child_id = actor['session_id']
                if native_parent:
                    self._remember_native_children(sid, [child_id],
                                                   run_id=run['runId'])
                    try:
                        snapshot = self.snapshot(child_id)
                        actor.update(verified=True,
                                     read_route='native_child_read',
                                     selected_model=snapshot['settings']['model']['current'],
                                     thought_level=snapshot['settings']['thoughtLevel']['current'],
                                     projection_status=(snapshot.get('projection') or {}).get('status'),
                                     run_status=run.get('status'),
                                     session_kind=snapshot['session'].get('sessionKind'))
                    except Exception as error:
                        actor.update(verified=False, read_route='native_child_read',
                                     error=str(error), projection_status='unverified',
                                     run_status=run.get('status'))
                    if child_id not in result['childSessionIds']:
                        result['childSessionIds'].append(child_id)
                    children.append(actor)
                    continue
                try:
                    snapshot = self.snapshot(child_id)
                    # A workflow run status is not the child's projection status.
                    actor.update(verified=True, selected_model=snapshot['settings']['model']['current'],
                        thought_level=snapshot['settings']['thoughtLevel']['current'],
                        projection_status=(snapshot.get('projection') or {}).get('status'),
                        run_status=run.get('status'),
                        session_kind=snapshot['session'].get('sessionKind'))
                    if child_id not in result['childSessionIds']:
                        result['childSessionIds'].append(child_id)
                except Exception as error:
                    actor.update(verified=False, error=str(error),
                                 projection_status='unverified', run_status=run.get('status'))
                children.append(actor)
        result['workflow_children'] = children
        if native_parent:
            self._remember_native_children(sid, result.get('childSessionIds') or [])
        return result
