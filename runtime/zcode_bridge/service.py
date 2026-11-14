"""Own one live protocol transport so question and permission replies reach it."""
import copy
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .protocol import Client, ProtocolError
from .desktop import session_visibility
from .storage import config, defaults, read_json, save_config, state_dir, write_json
from .workflows import topology
from .identity import is_visible_message, message_identity
from .notifications import (NotificationStore, Observer, has_anchor_proof,
                            make_input_id, subscription_anchor, TERMINAL_STATES)
from .delivery import DeliveryWorker
from .host_wake import HostWakeAdapter
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

# An engine process can outlive a session runtime.  Only these idempotent
# reads may be replayed after re-attaching the same session natively; sends,
# mode changes, stops and answers must surface the original failure unchanged.
READ_ONLY_SESSION_RPCS = frozenset({'session/read', 'session/messages', 'session/events'})


def is_session_not_found_error(error, sid):
    """The engine's -32004 reply when a session row is not persisted yet."""
    payload = getattr(error, 'error', None)
    return (isinstance(payload, dict) and payload.get('code') == -32004
            and isinstance(sid, str) and bool(sid)
            and str(payload.get('message') or '').strip() == f'Session not found: {sid}')


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

    def engine_identity(self):
        return self.bridge.engine_identity()

    def pending(self):
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
        self.observer = None
        self.delivery = None
        # The real adapter is created when the daemon starts; tests can inject
        # a fake waker before start_background without touching a real host.
        self.host_waker = None
        self._host_context = {}
        self._last_probe_at = 0.0

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
            # Forward slashes are not canonicalized consistently by Desktop
            # 3.17.2 and can cause project.cwd-not-in-roots. Use native paths.
            workspace = Path(workspace).resolve()
            if not workspace.is_dir():
                raise ValueError('指定工作区不存在')
        running = desktop_running(cfg['zcode_executable'])
        if workspace is not None or (not running and (force or cfg['auto_launch_desktop'])):
            executable = Path(cfg['zcode_executable'])
            if not executable.is_file():
                raise ValueError('ZCode 程序不存在，请检查配置')
            command = [str(executable)]
            if workspace is not None:
                command.extend(['--open-workspace', str(workspace)])
            subprocess.Popen(command, cwd=executable.parent,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {'already_running': running, 'launched': not running,
                'workspace_open_requested': workspace is not None, 'may_focus': workspace is not None}
        return {'already_running': running, 'launched': False,
            'workspace_open_requested': False, 'may_focus': False}

    def backend(self, deadline=None):
        with self.client_lock:
            if self.client is None or self.client.child.poll() is not None:
                self.launch()
                self.client = Client(config(), self._request, self._event)
                self.generation += 1
                with self.lock:
                    self.resident.clear()
                    self.created.clear()
                    self.pending.clear()
                    self.answered.clear()
            client = self.client
            generation = self.generation
        # capabilities is deliberately outside client_lock; a cold start plus
        # resume must not serialize the observer behind a 40 s request.
        if self.capabilities_generation != generation:
            try:
                timeout = self._remaining(deadline, cap=10) if deadline else 10
                client.request('runtime/capabilities', timeout=timeout)
                with self.client_lock:
                    if generation == self.generation:
                        self.capabilities_generation = generation
            except Exception:
                pass
        return client

    def _remaining(self, deadline, cap=40):
        if deadline is None:
            return cap
        return max(.5, min(cap, deadline - time.monotonic()))

    def rpc(self, method, params=None, deadline=None):
        params = params if isinstance(params, dict) else {}
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
        if not isinstance(sid, str) or not sid.startswith('sess_'):
            raise ValueError('会话 ID 无效')
        self.backend(deadline)
        if not self._is_resident(sid):
            snapshot = self._resume_session(sid, deadline)
            self._mark_resident(sid)
            return snapshot
        return None

    def inherit(self, sid, args, deadline=None):
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
        subscriptions = self.notifications.all_subscriptions()
        waiting = [sub for sub in subscriptions
                   if sub.get('session_id') == sid and sub.get('state') in ('arming', 'armed')]
        result['delivery'] = {'awaiting_start': bool(waiting),
                              'subscriptions': len([sub for sub in subscriptions
                                                    if sub.get('session_id') == sid])}
        if sid:
            workspace = (result.get('session') or {}).get('workspace', {}).get('workspacePath') or self.sessions.get(sid, {}).get('workspace')
            result['desktop'] = session_visibility(sid, workspace)
        return result

    # ------------------------------------------------------- notification helpers
    def _last_message_id(self, sid, deadline=None):
        try:
            rows = self.rpc('session/messages', {'sessionId': sid, 'limit': 20}, deadline).get('messages') or []
        except Exception:
            return ''
        for message in reversed(rows):
            if not is_visible_message(message):
                continue
            ident = message_identity(message)
            return str(ident) if ident else ''
        return ''

    def _last_user_message_id(self, sid, deadline=None):
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
        except Exception:
            return {**topology(run_id, []), 'after_sequence': 0, 'more': False}

    def _children_evidence(self, sid, deadline=None, strict=False, allow_unstarted=False):
        try:
            result = self.rpc('session/subagents', {'sessionId': sid, 'endedLimit': 30},
                              deadline) or {}
        except ProtocolError as error:
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
            try:
                snap = self.snapshot(child_id, deadline)
                status = str((snap.get('projection') or {}).get('status') or 'unknown')
            except Exception:
                pass
            children.append({'id': child_id, 'projection_status': status})
        return children, child_ids

    def _run_rows(self, sid, deadline=None, strict=False):
        rows, calls = [], 0
        raw_runs = self._workflow_runs(sid, deadline, strict=strict)
        for run in raw_runs[:64]:
            run_id = str(run.get('runId') or run.get('run_id') or '')
            if not run_id:
                continue
            status = str(run.get('status') or run.get('state') or '').lower()
            settled = status in ('completed', 'complete', 'succeeded', 'success', 'settled',
                                 'failed', 'error', 'cancelled', 'canceled')
            cursor = 0
            if not settled and calls < 5:
                calls += 1
                topo = self.observe_run_topology(sid, run_id, deadline)
                cursor = int(topo.get('after_sequence') or 0)
                if topo.get('status') not in (None, '', 'unknown'):
                    status = str(topo['status']).lower()
                    settled = status in ('completed', 'complete', 'succeeded', 'success',
                                         'settled', 'failed', 'error', 'cancelled', 'canceled')
            rows.append({'run_id': run_id, 'status': status or 'unknown', 'settled': settled,
                         'cursor': cursor})
        return rows, len(raw_runs) >= 64

    def _baseline(self, sid, snapshot, deadline=None, run_rows=None, run_truncated=None,
                  strict=False, allow_unstarted_children=False):
        projection = snapshot.get('projection') or {}
        jobs = [{'id': str(job.get('jobId') or job.get('id') or ''),
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
                    'failed', 'error', 'cancelled', 'canceled')
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
                    'failed', 'error', 'cancelled', 'canceled'):
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

    def _finish_send(self, sub, content, deadline=None):
        try:
            result = self.rpc('session/send', {
                'sessionId': sub['session_id'], 'content': content,
                'inputId': (sub.get('turn') or {}).get('input_id')}, deadline)
        except Exception:
            self.notifications.mark_send_uncertain(sub['subscription_id'], 'send_exception')
            return self._notification_summary(
                self.notifications.find_subscription(sub['subscription_id']) or sub,
                send_uncertain=True, retry_required=True, error_code='send_exception')
        if result.get('accepted') is True:
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
            self.inherit(sub['session_id'], args, deadline)
            claimed = self.notifications.claim_send(sub['subscription_id'])
            if claimed is None:
                return self._notification_summary(sub, send_uncertain=True, retry_required=True)
            return self._finish_send(claimed, args['content'], deadline)
        found = self._reconcile_send(sub, deadline)
        if found:
            updated = self.notifications.mark_send_accepted(sub['subscription_id'], found)
            return self._notification_summary(updated or sub, duplicate=True, accepted=True,
                                              reconciled=True)
        if args.get('retry') is True:
            self.inherit(sub['session_id'], args, deadline)
            claimed = self.notifications.claim_send(sub['subscription_id'], retry=True)
            if claimed is None:
                return self._notification_summary(sub, send_uncertain=True, retry_required=True)
            return self._finish_send(claimed, args['content'], deadline)
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
        self.inherit(sid, args, deadline)
        before_message_id = self._last_message_id(sid, deadline)
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
            engine_pid=self.engine_identity().get('pid'), generation=self.generation,
            start_deadline=now + settings['start_timeout'],
            expires_at=now + settings['expiry_hours'] * 3600)
        if status == 'existing':
            return self._resume_existing_send(sub, args, deadline)
        if status == 'busy':
            raise ValueError('session_busy: 该 session 已有活动通知订阅；等待完成或先在来源 chat 取消')
        claimed = self.notifications.claim_send(sub['subscription_id'])
        if claimed is None:
            return self._notification_summary(sub, send_uncertain=True, retry_required=True)
        return self._finish_send(claimed, args['content'], deadline)

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
            user_message_id = self._last_user_message_id(sid, deadline)
            if user_message_id:
                turn_anchor = user_message_id
            elif status == 'running':
                after_message_id = self._last_message_id(sid, deadline)
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
        before_message_id = after_message_id or self._last_message_id(sid, deadline)
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
            engine_pid=self.engine_identity().get('pid'), generation=self.generation,
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
    def engine_identity(self):
        with self.client_lock:
            child = getattr(self.client, 'child', None) if self.client is not None else None
            return {'pid': getattr(child, 'pid', None), 'generation': self.generation}

    def pending_snapshot(self):
        with self.lock:
            return copy.deepcopy(list(self.pending.values()))

    def observe_jobs(self, snapshot, sid=None, deadline=None):
        return [{'id': str(job.get('jobId') or job.get('id') or ''),
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
            caps = self.rpc('runtime/capabilities', deadline=deadline)
            return {'connected': True, 'bridge_version': __version__, 'engine_pid': self.client.child.pid,
                    'desktop_running': desktop_running(config()['zcode_executable']),
                    'capabilities': caps, 'pending_count': len(self.pending),
                    'managed_sessions': list(self.sessions), 'managed_session_details':[
                        {'session_id':sid,**copy.deepcopy(data),
                         'desktop':session_visibility(sid, data.get('workspace'))} for sid,data in self.sessions.items()],
                    'event_cursor': self.seq,
                    'notifications': self.notifications.aggregate()}
        if name == 'zcode_launch':
            return self.launch(force=True, workspace=args.get('workspace'), allow_focus=args.get('allow_focus',False))
        if name == 'zcode_settings':
            if args.get('changes') is not None:
                if caller_mode(args) == 'plan':
                    raise ValueError('只读会话不能修改桥接配置')
                return save_config(args['changes'])
            return config()
        if name == 'zcode_models':
            cfg = config()
            path = Path(args.get('workspace') or cfg['default_workspace']).resolve()
            if not path.is_dir():
                raise ValueError('工作区不存在')
            selection = cfg['default_model']
            snapshot = self.rpc('session/create', {
                'workspace': {'workspacePath': str(path), 'workspaceKey': str(path)},
                'mode': 'plan', 'persistence': 'deferred', 'titleGenerationEnabled': False,
                **({'model':selection} if selection.get('providerId') and selection.get('modelId') else {})})
            try:
                self.model_catalog = {(m['ref']['providerId'],m['ref']['modelId']):copy.deepcopy(m)
                    for m in snapshot['settings']['model']['available']}
                return snapshot['settings']['model']
            finally:
                self.rpc('session/close', {'sessionId': snapshot['session']['sessionId'], 'expectedPersistence': 'deferred'})
        if name == 'zcode_create_session':
            mode, cfg = caller_mode(args), config()
            path = Path(args['workspace']).resolve()
            if not path.is_dir():
                raise ValueError('指定工作区不存在')
            model = self.model_ref(args.get('model') or cfg['default_model'], args.get('thought_level'), str(path))
            snapshot = self.rpc('session/create', {
                'workspace': {'workspacePath': str(path), 'workspaceKey': str(path)},
                'mode': 'build' if mode == 'plan' else mode,
                'model': model,
                **({'thoughtLevel':model['options']['reasoningLevel']} if model.get('options', {}).get('reasoningLevel') else {}),
                'persistence': 'immediate', 'titleGenerationEnabled': False,
                'dynamicWorkflowEnabled': args.get('workflow_mode', cfg['default_workflow_mode']) != 'disabled',
                'dynamicWorkflowMode': args.get('workflow_mode', cfg['default_workflow_mode']),
            })
            sid = snapshot['session']['sessionId']
            self._mark_resident(sid)
            self._mark_created(sid)
            with self.lock:
                self.sessions[sid] = {'workspace': str(path), 'created_at': time.time(),
                    'caller_permission_mode': args['caller_permission_mode'], 'label': args.get('label', ''),
                    'model':snapshot['settings']['model']['current'],
                    'thought_level':snapshot['settings'].get('thoughtLevel', {}).get('current'),
                    'workflow_mode': args.get('workflow_mode', cfg['default_workflow_mode'])}
            self._save_sessions()
            if mode == 'plan':
                snapshot = self.inherit(sid, args)
            return self.summary(snapshot)
        if name == 'zcode_sessions':
            params = {'limit': min(100, max(1, int(args.get('limit', 30))))}
            if args.get('workspace'):
                path = str(Path(args['workspace']).resolve())
                params['workspace'] = {'workspacePath': path, 'workspaceKey': path}
            return self.rpc('session/list', params)
        if name == 'zcode_pending':
            with self.lock:
                rows = list(self.pending.values())
                if args.get('session_id'):
                    rows = [r for r in rows if r.get('params', {}).get('sessionId') == args['session_id']]
                return {'requests': copy.deepcopy(rows)}
        if name == 'zcode_answer':
            mode = caller_mode(args)
            ident = args['request_id']
            with self.lock:
                request = copy.deepcopy(self.pending.get(ident))
            if request is None:
                raise ValueError('请求不存在或已经处理')
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
                return self._send_with_notify(sid, args, meta, deadline)
            caller_permission(args)
            owner = self.notifications.session_owner(sid)
            if owner is not None:
                raise ValueError('session_busy: 该 session 已有活动通知订阅；等待完成或先在来源 chat 取消')
            self.inherit(sid, args, deadline)
            return self.rpc('session/send', {
                'sessionId': sid, 'content': content, 'inputId': uuid.uuid4().hex}, deadline)
        if name == 'zcode_messages':
            limit = min(100, max(1, int(args.get('limit', 30))))
            params = {'sessionId': sid}
            if args.get('after_message_id'):
                params['afterMessageId'] = args['after_message_id']
            else:
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
                    'more': len(all_rows) > limit,
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
            result = self.rpc('session/stop', {'sessionId': sid}, deadline)
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
        for run in runs.get('runs', []):
            for actor in self._workflow_topology(sid, run['runId'])['actors']:
                child_id = actor['session_id']
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
        return result
