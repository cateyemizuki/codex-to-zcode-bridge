"""Authenticated loopback controller shared by GPT and ZCode MCP clients."""
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .service import Bridge
from .storage import read_json, state_dir, write_json

ROOT = Path(__file__).resolve().parents[1]


def local_call(name, arguments=None, context=None, timeout=55, budget_ms=None, meta=None):
    context = context if context is not None else meta
    deadline = time.monotonic() + max(1, timeout)
    info = ensure_service(deadline)
    body = {'name': name, 'arguments': arguments or {}}
    if context is not None:
        body['context'] = context
    if budget_ms is not None:
        body['budget_ms'] = int(budget_ms)
    elif context is not None:
        body['budget_ms'] = int(max(500, (deadline - time.monotonic()) * 1000))
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/call", data=data,
        headers={'Authorization': 'Bearer '+info['token'], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=max(1, deadline - time.monotonic())) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        result = json.load(error)
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'Bridge request failed'))
    return result.get('result')


def _healthy(info):
    if not isinstance(info, dict) or not isinstance(info.get('port'), int) or not info.get('token'):
        return False
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/health",
            headers={'Authorization': 'Bearer '+info['token']})
        with urllib.request.urlopen(request, timeout=1) as response:
            return json.load(response).get('instance') == info.get('instance')
    except (OSError, ValueError):
        return False


def ensure_service(deadline=None):
    path = state_dir() / 'connection.json'
    info = read_json(path)
    if _healthy(info):
        return info
    log = open(state_dir() / 'controller.log', 'ab', buffering=0)
    subprocess.Popen([sys.executable, '-m', 'zcode_bridge', 'daemon'],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    log.close()
    final = deadline if deadline is not None else time.monotonic() + 15
    while time.monotonic() < final:
        info = read_json(path)
        if _healthy(info):
            return info
        time.sleep(.15)
    raise RuntimeError('本地桥接服务未能启动，请查看 '+str(state_dir() / 'controller.log'))


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def serve():
    lock = open(state_dir() / 'controller.lock', 'a+b')
    lock.seek(0)
    lock.write(b'0')
    lock.flush()
    lock.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    bridge, token = Bridge(), secrets.token_urlsafe(32)
    instance = secrets.token_hex(16)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'CateyeZCodeBridge/0.1'

        def log_message(self, *args):
            pass

        def allowed(self):
            expected = 'Bearer '+token
            supplied = self.headers.get('Authorization', '')
            origin = self.headers.get('Origin')
            origins = (None, f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            return origin in origins and hmac.compare_digest(expected, supplied)

        def respond(self, body, code=200, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/settings':
                return self.respond((ROOT / 'zcode_bridge/ui/settings.html').read_bytes(), content_type='text/html; charset=utf-8')
            if not self.allowed():
                return self.respond({'ok': False, 'error': 'unauthorized'}, 401)
            if path == '/health':
                return self.respond({'ok': True, 'instance': instance})
            self.respond({'ok': False, 'error': 'not found'}, 404)

        def do_POST(self):
            if not self.allowed():
                return self.respond({'ok': False, 'error': 'unauthorized'}, 401)
            if urlsplit(self.path).path == '/shutdown':
                self.respond({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if urlsplit(self.path).path != '/call':
                return self.respond({'ok': False, 'error': 'not found'}, 404)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2*1024*1024:
                    raise ValueError('request body size invalid')
                request = json.loads(self.rfile.read(size))
                context = request.get('context')
                if not isinstance(context, dict):
                    context = request.get('meta')
                context = context if isinstance(context, dict) else None
                budget_ms = request.get('budget_ms')
                try:
                    budget_ms = int(budget_ms) if budget_ms is not None else None
                except (TypeError, ValueError):
                    budget_ms = None
                if request.get('name') == 'zcode_open_settings':
                    result = {'url': f'http://127.0.0.1:{self.server.server_port}/settings#'+token}
                    if request.get('arguments', {}).get('open_browser', True):
                        result['opened'] = webbrowser.open(result['url'])
                else:
                    result = bridge.call(request['name'], request.get('arguments', {}),
                                         context, budget_ms)
                self.respond({'ok': True, 'result': result})
            except Exception as error:
                self.respond({'ok': False, 'error': str(error)}, 400)

    server = Server(('127.0.0.1', 0), Handler)
    write_json(state_dir() / 'connection.json', {'pid': os.getpid(), 'port': server.server_port,
        'token': token, 'instance': instance, 'started_at': time.time()})
    bridge.start_background()
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        bridge.stop_background()
        if bridge.client:
            bridge.client.close()
        server.server_close()
        lock.close()
