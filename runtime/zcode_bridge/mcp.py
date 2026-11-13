import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .tools import TOOLS
from .transport import local_call
from . import __version__
from . import preferences

UI_URI = 'ui://codex-to-zcode-bridge/settings.html'
INSTRUCTIONS = '''Control local ZCode through the zcode_* tools; no skill is required.
zcode_open_settings opens the embedded configuration panel. Native plugin preferences use openai/settings.
Read zcode_models for exact provider/model IDs and supported reasoning levels. Never substitute models.
Create a session in the specified existing absolute workspace, retain its exact session ID, send content and observe until complete.
Session persistence is separate from the desktop task index: check desktop.indexed and do not claim desktop visibility from an engine success. Already-open projects do not receive live events from this independent engine. Only when the user explicitly requests desktop display, zcode_launch(workspace=...,allow_focus=true) opens the native project and may bring ZCode to the foreground. Never use it for background polling or automatic refresh. A project's first open imports persisted sessions; reopening an already-open project does not guarantee refresh.
On every mutation supply the actual current GPT caller_permission_mode; full_access maps to yolo, read_only clears elevated permission then enables plan. Unknown means always_ask.
For independent child models/effort, normally use native dynamic workflows: create with onDemand, then send a message starting /workflow followed by the natural-language task, workspace scope, stages, dependencies, parallel agents, exact model/effort, budgets and checks. ZCode loads its own full skill. Ordinary subagents inherit the main model.
Read main/child content with zcode_messages and cursors, find child IDs through zcode_subagents, and inspect zcode_workflows for topology/results.
Poll zcode_wait and zcode_pending. First CreateWorkflow needs native authorization even in full access. Inspect the actual request; for actions already authorized in scope choose its real option_id. Approval modes require the user's actual decision before user_approved=true. Ask genuine preference questions to the user.
For AskUserQuestion reply to the stable request_id with response={action:"accept",content:{answers:{"exact question":"chosen value"}}}. Do not answer transient wire IDs.
Stop only the selected session. This is an independent protocol connection; persisted histories can be read. If you are already the delegated ZCode agent, use your native current-session tools rather than creating yourself recursively.'''


def capabilities():
    return {'tools':{},'resources':{},'extensions':{'openai/settings':preferences.CAPABILITY},
            'experimental':{'openai/settings':preferences.CAPABILITY}}


def handle(message):
    method = message.get('method')
    if method == 'initialize':
        return {'protocolVersion': message.get('params', {}).get('protocolVersion', '2024-11-05'),
                'capabilities': capabilities(),
                'serverInfo': {'name': 'codex-to-zcode-bridge', 'version': __version__},
                'instructions': INSTRUCTIONS}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        rows = json.loads(json.dumps(TOOLS)) + preferences.definitions()
        for row in rows:
            row.setdefault('_meta',{}).setdefault('ui',{}).setdefault('visibility',['model','app'])
            row['_meta']['openai/widgetAccessible'] = True
            if row['name'] == 'zcode_open_settings':
                row['title'] = 'ZCode 配置与会话面板'
                row['_meta']['ui']['resourceUri'] = UI_URI
                row['_meta']['openai/outputTemplate'] = UI_URI
                row['_meta']['openai/ui'] = {'entrypoints':[{'type':'global'},{'type':'thread'}]}
        return {'tools': rows}
    if method == 'tools/call':
        params = message.get('params', {})
        name = params.get('name')
        arguments = params.get('arguments', {})
        if name in (preferences.READ_TOOL,preferences.UPDATE_TOOL):
            result = preferences.read() if name==preferences.READ_TOOL else preferences.update(arguments)
            return {'content':[],'structuredContent':result}
        if name not in {t['name'] for t in TOOLS}:
            raise ValueError('unknown tool')
        if name == 'zcode_open_settings' and not arguments.get('open_browser',False):
            cfg = local_call('zcode_settings')
            result = {'panel':'ZCode 配置与会话面板','transport':'mcp-app','model':cfg['default_model'],
                      'workspace':cfg['default_workspace']}
        else:
            result = local_call(name, arguments)
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                'structuredContent': result if isinstance(result, dict) else {'result': result}}
    if method == 'resources/list':
        return {'resources': [{'uri': UI_URI, 'name': 'Codex to ZCode Bridge configuration', 'mimeType': 'text/html;profile=mcp-app'}]}
    if method == 'resources/read':
        if message.get('params', {}).get('uri') != UI_URI:
            raise ValueError('unknown resource')
        html = (Path(__file__).resolve().parent / 'ui/settings.html').read_text(encoding='utf-8')
        return {'contents': [{'uri': UI_URI, 'mimeType': 'text/html;profile=mcp-app', 'text': html,
                             '_meta': {'ui': {'prefersBorder': True,'csp':{'connectDomains':[],'resourceDomains':[]}},
                                'openai/widgetDescription':'Configure ZCode models, workspaces and sessions directly inside the plugin.',
                                'openai/widgetPrefersBorder':True}}]}
    raise KeyError('method not found')


def serve():
    write_lock = threading.Lock()

    def process(raw):
        message = None
        try:
            message = json.loads(raw)
            if 'id' not in message:
                return
            result = handle(message)
            response = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
        except Exception as error:
            if message is None or 'id' not in message:
                return
            if message.get('method') == 'tools/call':
                response = {'jsonrpc': '2.0', 'id': message['id'], 'result': {
                    'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}}
            else:
                response = {'jsonrpc': '2.0', 'id': message['id'], 'error': {
                    'code': -32601 if isinstance(error, KeyError) else -32602, 'message': str(error)}}
        with write_lock:
            sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False)+'\n').encode('utf-8'))
            sys.stdout.buffer.flush()

    # A wait must not block a concurrent answer on the same MCP connection.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix='mcp') as executor:
        for raw in sys.stdin.buffer:
            executor.submit(process, raw)
