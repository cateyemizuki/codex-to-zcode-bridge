"""Multiplex the real ZCode Protocol; IDs are strings, not JSON-RPC integers."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path


class ProtocolError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(json.dumps(error, ensure_ascii=False))


class Client:
    def __init__(self, config, on_request, on_event):
        raise RuntimeError('independent_engine_disabled: only the installed Desktop host may execute ZCode sessions')

    def _read(self):
        try:
            for line in iter(self.child.stdout.readline, b''):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if 'id' in message and 'method' not in message:
                    with self.lock:
                        target = self.pending.get(str(message['id']))
                    if target:
                        target.put(message)
                elif 'id' in message:
                    # Technical preferences are local session settings, not user answers.
                    if message.get('method') == 'session/requestRuntimePreferences':
                        self.respond(message['id'], {
                            'nativeSearchEnhancementsEnabled': False, 'toolSearchMode': 'off',
                            'memoryEnabled': False, 'askUserQuestionAutoResolutionEnabled': False,
                            'subagentRuntimeConfigEnabled': False,
                        })
                    elif message.get('method') in (
                            'interaction/requestOfficialMcpAuthHeaders',
                            'interaction/requestProviderRuntimeHeaders'):
                        # Do not impersonate the Desktop account or fabricate auth headers.
                        self.respond(message['id'], error={
                            'code': -32031, 'message': 'This bridge uses configured personal providers; Desktop account header refresh is unavailable.'})
                    else:
                        self.on_request(message)
                else:
                    self.on_event(message)
        finally:
            with self.lock:
                targets = list(self.pending.values())
            for target in targets:
                target.put({'error': {'code': -32000, 'message': 'ZCode backend disconnected'}})

    def _errors(self):
        for line in iter(self.child.stderr.readline, b''):
            self.errors.append(line.decode('utf-8', errors='replace').strip())

    def write(self, message):
        with self.lock:
            self.child.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
            self.child.stdin.flush()

    def request(self, method, params=None, timeout=40):
        ident, target = uuid.uuid4().hex, queue.Queue()
        with self.lock:
            self.pending[ident] = target
        try:
            self.write({'id': ident, 'method': method, 'params': params or {}})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    result = target.get(timeout=min(.25, max(.01, deadline-time.monotonic())))
                except queue.Empty:
                    if self.child.poll() is not None:
                        raise RuntimeError('ZCode 引擎退出：' + '\n'.join(self.errors)[-1800:])
                    continue
                if 'error' in result:
                    raise ProtocolError(result['error'])
                return result.get('result')
            raise TimeoutError('ZCode 接口超时：' + method)
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def respond(self, ident, result=None, error=None):
        self.write({'id': ident, **({'error': error} if error else {'result': result})})

    def close(self):
        if self.child.poll() is None:
            self.child.stdin.close()
            try:
                self.child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.child.terminate()
                self.child.wait(timeout=10)
