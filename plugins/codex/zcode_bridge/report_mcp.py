"""Minimal MCP entry that exposes only ``zcode_agent_report``.

A delegated session may receive this as a session-local ``mcpServers`` entry.
It deliberately has no delegate/message/inspect/answer/cancel tool, so a child
cannot recurse into the full bridge control surface. The report remains an
untrusted hint; this process only forwards it to the local bridge daemon.
"""
from __future__ import annotations

import json
import sys

from . import __version__
from .transport import local_call

TOOL = {
    'name': 'zcode_agent_report',
    'title': 'Report task result to the parent agent',
    'description': (
        'Report a bounded structured result to the parent job. agent_id is the '
        'only required identifier; session_id defaults to the bound job session. '
        'The report is recorded as an unverified hint and never proves completion '
        'or grants permission.'),
    'inputSchema': {
        'type': 'object',
        'properties': {
            'agent_id': {'type': 'string'},
            'result': {'type': 'object'},
            'session_id': {'type': 'string'},
            'status': {'type': 'string'},
        },
        'required': ['agent_id', 'result'],
        'additionalProperties': False,
    },
    'annotations': {'readOnlyHint': False, 'destructiveHint': False,
                    'openWorldHint': True},
}
INSTRUCTIONS = (
    'This server exposes only zcode_agent_report. Use it for a bounded structured '
    'final result or needs_input hint; it never completes the parent job and '
    'cannot grant permissions.')


def handle(message):
    method = message.get('method')
    if method == 'initialize':
        return {
            'protocolVersion': message.get('params', {}).get('protocolVersion', '2024-11-05'),
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'codex-to-zcode-bridge-report', 'version': __version__},
            'instructions': INSTRUCTIONS,
        }
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [json.loads(json.dumps(TOOL))]}
    if method == 'tools/call':
        params = message.get('params') or {}
        if params.get('name') != 'zcode_agent_report':
            raise ValueError('unknown tool')
        arguments = params.get('arguments') or {}
        result = local_call('zcode_agent_report', arguments)
        return {
            'content': [{'type': 'text',
                         'text': json.dumps(result, ensure_ascii=False)}],
            'structuredContent': result if isinstance(result, dict) else {'result': result},
        }
    raise KeyError('method not found')


def serve():
    for raw in sys.stdin.buffer:
        message = None
        try:
            message = json.loads(raw)
            if 'id' not in message:
                continue
            result = handle(message)
            response = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
        except Exception as error:
            if message is None or 'id' not in message:
                continue
            if message.get('method') == 'tools/call':
                response = {'jsonrpc': '2.0', 'id': message['id'], 'result': {
                    'isError': True,
                    'content': [{'type': 'text', 'text': str(error)}]}}
            else:
                response = {'jsonrpc': '2.0', 'id': message['id'], 'error': {
                    'code': -32601 if isinstance(error, KeyError) else -32602,
                    'message': str(error)}}
        sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        sys.stdout.buffer.flush()


if __name__ == '__main__':
    serve()
