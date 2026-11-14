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

from .protocol import Client
from .desktop import session_visibility
from .storage import config, read_json, save_config, state_dir, write_json
from .workflows import topology
from . import __version__

PERMISSIONS = {
    'full_access': 'yolo', 'review_important_actions': 'edit',
    'ask_before_writes': 'build', 'always_ask': 'build', 'read_only': 'plan',
}


def caller_mode(arguments):
    permission = arguments.get('caller_permission_mode')
    if permission not in PERMISSIONS:
        raise ValueError('必须提供 GPT 当前会话的 caller_permission_mode；无法确认时使用 always_ask')
    return PERMISSIONS[permission]


def message_identity(message):
    """Return a stable message id across ZCode 3.17 (info.id) and 3.18 (info.messageId)."""
    info = message.get('info') if isinstance(message, dict) else None
    info = info if isinstance(info, dict) else {}
    candidates = [info.get('id'), info.get('messageId')]
    if isinstance(message, dict):
        candidates.extend([message.get('id'), message.get('messageId')])
    return next((value for value in candidates if value not in (None, '')), None)


def compact_messages(messages, limit_chars=24000):
    result, used = [], 0
    for message in messages:
        info = message.get('info', {})
        if info.get('synthetic') or info.get('visibility') == 'model-only' or info.get('semantics', {}).get('uiVisibility') == 'hidden':
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


class Bridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.client_lock = threading.Lock()
        self.client = None
        self.pending = {}
        self.answered = {}
        self.events = deque(maxlen=4000)
        self.seq = 0
        self.sessions = read_json(state_dir() / 'sessions.json', {}) or {}
        self.resident = set()
        self.accepted = {}
        self.model_catalog = {}

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

    def backend(self):
        with self.client_lock:
            if self.client is None or self.client.child.poll() is not None:
                self.launch()
                self.client = Client(config(), self._request, self._event)
                self.client.request('runtime/capabilities')
                self.resident.clear()
                with self.lock:
                    self.pending.clear()
                    self.answered.clear()
            return self.client

    def rpc(self, method, params=None):
        return self.backend().request(method, params)

    def ensure_session(self, sid):
        if not isinstance(sid, str) or not sid.startswith('sess_'):
            raise ValueError('会话 ID 无效')
        self.backend()
        if sid not in self.resident:
            params = {'sessionId': sid}
            if sid in self.sessions:
                workflow_mode = self.sessions[sid].get('workflow_mode', config()['default_workflow_mode'])
                params.update(dynamicWorkflowEnabled=workflow_mode != 'disabled', dynamicWorkflowMode=workflow_mode)
            snapshot = self.rpc('session/resume', params)
            self.resident.add(sid)
            return snapshot
        return None

    def inherit(self, sid, args):
        mode = caller_mode(args)
        self.ensure_session(sid)
        if mode == 'plan':
            # In 0.17, plan is an overlay, not a permission mode. Clear a
            # previous yolo/edit permission before enabling that overlay.
            self.rpc('session/setMode', {'sessionId':sid, 'mode':'build'})
        result = self.rpc('session/setMode', {'sessionId': sid, 'mode': mode})
        expected = 'build' if mode == 'plan' else mode
        if result.get('settings', {}).get('permission', {}).get('mode') != expected:
            raise RuntimeError('ZCode 未应用指定权限，已停止后续发送')
        with self.lock:
            self.sessions.setdefault(sid, {})['caller_permission_mode'] = args['caller_permission_mode']
        self._save_sessions()
        return result

    def snapshot(self, sid):
        self.ensure_session(sid)
        return self.rpc('session/read', {'sessionId': sid, 'messageLimit': 10})

    def summary(self, snapshot):
        result = {key: copy.deepcopy(snapshot.get(key)) for key in ('session', 'projection', 'runtime', 'settings')}
        # The catalogue has its own tool; avoid repeating it on every status poll.
        result.get('settings', {}).get('model', {}).pop('available', None)
        sid = (result.get('session') or {}).get('sessionId')
        acceptance = self.accepted.get(sid)
        result['delivery'] = {'awaiting_start': bool(acceptance and
            snapshot.get('projection', {}).get('turnCount', 0) <= acceptance['before_turn_count'])}
        if sid:
            workspace = (result.get('session') or {}).get('workspace', {}).get('workspacePath') or self.sessions.get(sid, {}).get('workspace')
            result['desktop'] = session_visibility(sid, workspace)
        return result

    def call(self, name, args):
        if name == 'zcode_status':
            caps = self.rpc('runtime/capabilities')
            return {'connected': True, 'bridge_version': __version__, 'engine_pid': self.client.child.pid,
                    'desktop_running': desktop_running(config()['zcode_executable']),
                    'capabilities': caps, 'pending_count': len(self.pending),
                    'managed_sessions': list(self.sessions), 'managed_session_details':[
                        {'session_id':sid,**copy.deepcopy(data),
                         'desktop':session_visibility(sid, data.get('workspace'))} for sid,data in self.sessions.items()],
                    'event_cursor': self.seq}
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
            self.resident.add(sid)
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
        if sid:
            self.ensure_session(sid)
        if name == 'zcode_read_session':
            snapshot = self.snapshot(sid)
            result = self.summary(snapshot)
            rows = self.rpc('session/messages', {'sessionId': sid, 'limit': 10}).get('messages', [])
            result['messages'] = compact_messages(rows, int(args.get('max_chars', 16000)))
            return result
        if name == 'zcode_send':
            self.inherit(sid, args)
            content = args.get('content')
            if not isinstance(content, str) or not content.strip() or len(content) > 200000:
                raise ValueError('消息必须是非空文本，长度不超过 200000 字符')
            before = self.snapshot(sid).get('projection', {}).get('turnCount', 0)
            result = self.rpc('session/send', {'sessionId': sid, 'content': content, 'inputId': uuid.uuid4().hex})
            if result.get('accepted'):
                self.accepted[sid] = {'before_turn_count':before, 'at':time.time()}
            return result
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
            anchor = message_identity(rows[-1]) if rows else None
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
            caller_mode(args)
            result = self.rpc('session/stop', {'sessionId': sid})
            with self.lock:
                self.pending = {k:v for k,v in self.pending.items() if v.get('params', {}).get('sessionId') != sid}
            return result
        if name == 'zcode_wait':
            deadline = time.monotonic() + min(45, max(0, float(args.get('timeout_seconds', 15))))
            while True:
                snapshot = self.snapshot(sid)
                with self.lock:
                    pending = copy.deepcopy([r for r in self.pending.values() if r.get('params', {}).get('sessionId') == sid])
                status = snapshot.get('projection', {}).get('status')
                awaiting = self.summary(snapshot)['delivery']['awaiting_start']
                jobs = snapshot.get('projection', {}).get('backgroundJobs', [])
                active_jobs = any(j.get('status') in ('pending','running','waiting') for j in jobs)
                terminal = status in ('idle', 'completed', 'error', 'paused', 'waiting') and not awaiting and not active_jobs
                if pending or terminal or time.monotonic() >= deadline:
                    result = self.summary(snapshot)
                    result.update({'pending_requests': pending, 'event_cursor': self.seq})
                    return result
                time.sleep(.4)
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
                    actor.update(verified=True, selected_model=snapshot['settings']['model']['current'],
                        thought_level=snapshot['settings']['thoughtLevel']['current'],
                        status=run['status'], session_kind=snapshot['session'].get('sessionKind'))
                    if child_id not in result['childSessionIds']:
                        result['childSessionIds'].append(child_id)
                except Exception as error:
                    actor.update(verified=False, error=str(error))
                children.append(actor)
        result['workflow_children'] = children
        return result
