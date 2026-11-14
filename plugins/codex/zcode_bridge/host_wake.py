"""Standard MCP stdio adapter for the Codex Desktop first-party host.

This module implements only the measured, public contract:

* locate ``plugins/openai-bundled/plugins/codex-app-tools/server.mjs`` under the
  parent of ``CODEX_APP_RUNTIME_CLI_PATH`` (or use an injected command array);
* launch it as ``node server.mjs`` with an argument array, ``shell=False`` and
  ``CREATE_NO_WINDOW`` on Windows;
* speak JSON-RPC 2.0 MCP over stdio: ``initialize``,
  ``notifications/initialized``, ``tools/list`` and ``tools/call``;
* put ``_meta={"openai/threadId": bound_thread}`` on every ``tools/call`` and
  pass only ``threadId``, optional real ``hostId`` and ``prompt`` as arguments.

It does not implement or imitate the private pipe protocol, does not modify the
app, binaries or databases.  The host context (pipe/runtime/node) is local
capability data: it is never put into tool arguments, prompts, events or logs.
A successful tools/call means only that the host accepted the message; it does
not prove the model read it or continued the task.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

WAKE_TOOL = 'send_message_to_thread'


class HostUnavailable(RuntimeError):
    def __init__(self, error_code, detail=''):
        self.error_code = error_code
        super().__init__(error_code + ((': ' + detail) if detail else ''))


class HostSession:
    def __init__(self, command, env, request_timeout=10, total_timeout=20,
                 creationflags=0):
        if isinstance(command, (str, bytes)) or not command:
            raise HostUnavailable('host_unavailable', 'host command must be an argument array')
        self.command = [str(part) for part in command]
        self.env = env
        self.request_timeout = request_timeout
        self.total_timeout = max(1, total_timeout)
        self.creationflags = creationflags
        self.child = None
        self.reader = None
        self.messages = queue.Queue()
        self.stop_reading = threading.Event()
        self.next_id = 0
        self.tools = []
        self.deadline = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        self.deadline = time.monotonic() + self.total_timeout
        self.child = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=self.env, shell=False, creationflags=self.creationflags)
        self.reader = threading.Thread(target=self._read, name='host-wake-reader', daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in iter(self.child.stdout.readline, b''):
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def _write(self, message):
        if self.child is None or self.child.poll() is not None:
            raise HostUnavailable('handshake_failed', 'host process exited')
        self.child.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
        self.child.stdin.flush()

    def notify(self, method, params=None):
        self._write({'jsonrpc': '2.0', 'method': method, 'params': params or {}})

    def request(self, method, params=None, timeout=None):
        self.next_id += 1
        ident = self.next_id
        self._write({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params or {}})
        remaining = (self.deadline - time.monotonic()) if self.deadline else None
        if remaining is not None and remaining <= 0:
            raise HostUnavailable('timeout', method)
        budget = timeout or self.request_timeout
        if remaining is not None:
            budget = min(budget, remaining)
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=min(.2, max(.01, deadline - time.monotonic())))
            except queue.Empty:
                if self.child.poll() is not None:
                    raise HostUnavailable('handshake_failed', 'host process exited')
                continue
            if message is None:
                raise HostUnavailable('handshake_failed', 'host closed its output')
            if str(message.get('id')) != str(ident):
                continue
            error = message.get('error')
            if error:
                text = str(error.get('message') or '')
                if error.get('code') == -32603 and 'CODEX_APP_TOOLS_PIPE_PATH' in text:
                    raise HostUnavailable('pipe_unavailable', 'host reported a pipe error')
                raise HostUnavailable('handshake_failed', 'host returned a JSON-RPC error')
            return message.get('result') or {}
        raise HostUnavailable('timeout', method)

    def initialize(self, protocol_version='2025-06-18'):
        result = self.request('initialize', {
            'protocolVersion': protocol_version,
            'capabilities': {},
            'clientInfo': {'name': 'codex-to-zcode-bridge', 'version': '0.4.0'},
        })
        self.notify('notifications/initialized')
        return result

    def list_tools(self):
        result = self.request('tools/list', {})
        tools = result.get('tools') or []
        self.tools = [row.get('name') for row in tools
                      if isinstance(row, dict) and row.get('name')]
        return self.tools

    def call_tool(self, name, arguments, thread_id):
        meta = {'openai/threadId': thread_id}
        params = {'name': name, 'arguments': arguments, '_meta': meta}
        return self.request('tools/call', params)

    def close(self):
        self.stop_reading.set()
        if self.child is not None:
            try:
                if self.child.stdin:
                    self.child.stdin.close()
            except OSError:
                pass
            if self.child.poll() is None:
                try:
                    self.child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.child.terminate()
                    try:
                        self.child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            self.child.kill()
                        except OSError:
                            pass
                        try:
                            self.child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
            for stream in (self.child.stdout, self.child.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        if self.reader and self.reader.is_alive():
            self.reader.join(timeout=2)


class HostWakeAdapter:
    """Probe and wake through the first-party host; version-dependent capability.

    ``command`` injects a fake host command array for tests.  ``transport`` may
    inject an object with ``probe(host=...)`` / ``send(..., host=...)``.  The
    real path uses only standard MCP stdio.
    """

    def __init__(self, env=None, transport=None, command=None, total_timeout=20,
                 request_timeout=10, creationflags=None):
        self.env = dict(os.environ if env is None else env)
        self.transport = transport
        if isinstance(command, (str, bytes)):
            raise ValueError('command must be an argument array, not a shell string')
        self.command = [str(part) for part in command] if command else None
        self.total_timeout = total_timeout
        self.request_timeout = request_timeout
        self.creationflags = (subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) \
            if creationflags is None else creationflags

    # ---------------------------------------------------------------- discovery
    def _host_value(self, host, key, env_key, default=''):
        if isinstance(host, dict):
            value = host.get(key)
            if isinstance(value, str) and value:
                return value
        value = self.env.get(env_key)
        return value if isinstance(value, str) and value else default

    def resolve(self, host=None):
        if self.command:
            return self.command
        runtime = self._host_value(host, 'runtime_cli', 'CODEX_APP_RUNTIME_CLI_PATH')
        if not runtime:
            raise HostUnavailable('host_unavailable', 'CODEX_APP_RUNTIME_CLI_PATH is absent')
        runtime_path = Path(runtime)
        server = runtime_path.parent / 'plugins/openai-bundled/plugins/codex-app-tools/server.mjs'
        node = self._host_value(host, 'node', 'CODEX_MCP_NODE_PATH',
                                str(runtime_path.parent / 'cua_node/bin/node.exe'))
        if not server.is_file():
            raise HostUnavailable('host_unavailable', 'codex-app-tools server.mjs is absent')
        if not Path(node).is_file():
            raise HostUnavailable('host_unavailable', 'Node executable is absent')
        return [node, str(server)]

    def _pipe(self, host, pipe_path=''):
        return pipe_path or self._host_value(host, 'pipe', 'CODEX_APP_TOOLS_PIPE_PATH')

    def _environment(self, host, pipe_path=''):
        # Never inherit a stale thread or host capability from the daemon env.
        env = {key: value for key, value in self.env.items()
               if key not in ('CODEX_THREAD_ID', 'CODEX_APP_TOOLS_PIPE_PATH',
                              'CODEX_APP_RUNTIME_CLI_PATH', 'CODEX_MCP_NODE_PATH')}
        pipe = self._pipe(host, pipe_path)
        if pipe:
            env['CODEX_APP_TOOLS_PIPE_PATH'] = pipe
        runtime = self._host_value(host, 'runtime_cli', 'CODEX_APP_RUNTIME_CLI_PATH')
        if runtime:
            env['CODEX_APP_RUNTIME_CLI_PATH'] = runtime
        node = self._host_value(host, 'node', 'CODEX_MCP_NODE_PATH')
        if node:
            env['CODEX_MCP_NODE_PATH'] = node
        return env

    def _host_id(self, host, host_id=''):
        return host_id or self._host_value(host, 'host_id', '')

    def _session(self, host, total_timeout=None, pipe_path=''):
        if self.transport is not None:
            return self.transport
        return HostSession(self.resolve(host), self._environment(host, pipe_path),
                           request_timeout=self.request_timeout,
                           total_timeout=total_timeout or self.total_timeout,
                           creationflags=self.creationflags)

    # ---------------------------------------------------------------- operations
    def probe(self, timeout_seconds=10, host=None, pipe_path=''):
        if self.transport is not None and hasattr(self.transport, 'probe'):
            return self.transport.probe(host=host)
        if not self._pipe(host, pipe_path):
            return {'state': 'pipe_unavailable', 'error_code': 'pipe_unavailable'}
        budget = max(1, min(20, int(timeout_seconds or 10)))
        try:
            with self._session(host, total_timeout=budget, pipe_path=pipe_path) as session:
                session.initialize()
                tools = session.list_tools()
                if WAKE_TOOL not in tools:
                    return {'state': 'unsupported', 'error_code': 'missing_tool',
                            'tools': [t for t in tools if t != WAKE_TOOL]}
                return {'state': 'supported', 'tools': ['send_message_to_thread']}
        except HostUnavailable as error:
            if error.error_code in ('handshake_failed', 'timeout'):
                return {'state': 'unsupported', 'error_code': 'handshake_failed'}
            return {'state': error.error_code, 'error_code': error.error_code}
        except Exception:
            return {'state': 'unsupported', 'error_code': 'handshake_failed'}

    def send(self, thread_id, session_id, event_id, prompt, host=None, host_id='',
             pipe_path=''):
        if not thread_id:
            return {'ok': False, 'error_code': 'source_mismatch'}
        if self.transport is not None and hasattr(self.transport, 'send'):
            return self.transport.send(thread_id, session_id, event_id, prompt, host=host)
        if not self._pipe(host, pipe_path):
            return {'ok': False, 'error_code': 'pipe_unavailable'}
        try:
            with self._session(host, total_timeout=self.total_timeout,
                               pipe_path=pipe_path) as session:
                session.initialize()
                tools = session.list_tools()
                if WAKE_TOOL not in tools:
                    return {'ok': False, 'error_code': 'unsupported', 'unsupported': True,
                            'error_detail': 'missing_tool'}
                arguments = {'threadId': thread_id, 'prompt': prompt}
                real_host_id = self._host_id(host, host_id)
                if real_host_id:
                    arguments['hostId'] = real_host_id
                result = session.call_tool(WAKE_TOOL, arguments, thread_id)
                if result.get('isError'):
                    return {'ok': False, 'error_code': 'tool_error'}
                return {'ok': True}
        except HostUnavailable as error:
            return {'ok': False, 'error_code': error.error_code,
                    'unsupported': error.error_code in ('handshake_failed', 'timeout')}
        except Exception:
            # Do not leak exception text/stderr; only a stable error code.
            return {'ok': False, 'error_code': 'handshake_failed', 'unsupported': True}
