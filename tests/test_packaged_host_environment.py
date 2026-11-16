"""Exercise the packaged host handshake under a sanitized MCP environment."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build


FAKE_HOST = '''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message['method']
    if method == 'initialize':
        result = {'protocolVersion':'2025-06-18','capabilities':{},
                  'serverInfo':{'name':'test-host','version':'1'}}
    elif method == 'tools/list':
        result = {'tools':[{'name':'send_message_to_thread'}]}
    else:
        raise RuntimeError('The probe must never call a host tool')
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}), flush=True)
'''

PROBE = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from zcode_bridge.mcp import normalize_context
from zcode_bridge.host_wake import HostWakeAdapter
context = normalize_context({'openai/threadId':'test-source'})
host = {key: context.get('host_' + key, '')
        for key in ('pipe', 'runtime_cli', 'node')}
result = HostWakeAdapter(command=[sys.executable, sys.argv[2]],
                         request_timeout=3, total_timeout=5).probe(host=host)
print(json.dumps({'state':result['state'],
                  'present':{key:bool(value) for key,value in host.items()}}))
'''


class PackagedHostEnvironmentTests(unittest.TestCase):
    def test_forwarded_host_environment_enables_real_stdio_handshake(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / 'source'
            parent = {
                'CODEX_APP_TOOLS_PIPE_PATH':'test-local-host-pipe',
                'CODEX_APP_RUNTIME_CLI_PATH':'test-local-runtime',
                'CODEX_MCP_NODE_PATH':'test-local-node',
            }
            with patch.dict(os.environ, parent):
                build.build(stage)
            package = stage / 'plugins' / 'codex'
            target = root / 'installed'
            state = root / 'state'
            host = root / 'fake_host.py'
            host.write_text(FAKE_HOST, encoding='utf-8')
            install_env = {**os.environ, **parent, 'ZCODE_BRIDGE_STATE':str(state)}
            installed = subprocess.run(
                [sys.executable, str(package / 'install.py'), '--no-register',
                 '--install-root', str(target)], env=install_env,
                capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            configs = []
            for plugin in (package, target / 'plugins' / 'codex'):
                config = json.loads((plugin / '.mcp.json').read_text(encoding='utf-8'))
                configs.append((plugin, config['mcpServers']['zcode_bridge']))
                for value in parent.values():
                    self.assertNotIn(value, json.dumps(config))
            # Match the MCP launcher's allowlist behavior, rather than giving
            # the test child the test runner's unrestricted environment.
            base = {key:value for key,value in os.environ.items()
                    if key.upper() in ('SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP')}
            for plugin, config in configs:
                with self.subTest(package=str(plugin)):
                    forwarded = {name:parent[name] for name in config.get('env_vars', [])
                                 if name in parent}
                    child = subprocess.run(
                        [sys.executable, '-c', PROBE, str(plugin), str(host)],
                        env={**base, **forwarded}, capture_output=True, text=True,
                        encoding='utf-8', timeout=10)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    result = json.loads(child.stdout)
                    self.assertEqual(result['state'], 'supported')
                    self.assertEqual(result['present'],
                                     {'pipe':True, 'runtime_cli':True, 'node':True})
            # Reproduce the old manifest: no forwarding means no usable host
            # context, even though the parent has a supported host capability.
            missing = subprocess.run(
                [sys.executable, '-c', PROBE, str(package), str(host)],
                env=base, capture_output=True, text=True, encoding='utf-8', timeout=10)
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertEqual(json.loads(missing.stdout), {
                'state':'pipe_unavailable',
                'present':{'pipe':False, 'runtime_cli':False, 'node':False}})
            zcode = json.loads((stage / 'plugins' / 'zcode' / '.mcp.json').read_text(encoding='utf-8'))
            self.assertNotIn('env_vars', zcode['mcpServers']['zcode_bridge'])
            self.assertFalse((state / 'connection.json').exists())

    def test_upgrade_preserves_installed_state_without_parent_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, target, state = root/'source', root/'installed', root/'chosen-state'
            build.build(stage)
            package = stage/'plugins'/'codex'
            base = {key:value for key,value in os.environ.items() if key != 'ZCODE_BRIDGE_STATE'}
            base.update({'LOCALAPPDATA':str(root/'default-appdata'),
                         'HOME':str(root/'home'), 'USERPROFILE':str(root/'home')})
            args = [sys.executable, str(package/'install.py'), '--no-register',
                    '--install-root', str(target)]
            first = subprocess.run(args, env={**base,'ZCODE_BRIDGE_STATE':str(state)},
                                   capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertEqual(first.returncode, 0, first.stderr)
            (state/'config.json').write_text(json.dumps({
                'default_workspace':str(root), 'default_transport':'engine'}), encoding='utf-8')
            before = (state/'config.json').read_bytes()
            updated = subprocess.run(args, env=base, capture_output=True,text=True,
                                     encoding='utf-8',timeout=20)
            self.assertEqual(updated.returncode, 0, updated.stderr)
            config = json.loads((target/'plugins'/'codex'/'.mcp.json').read_text(encoding='utf-8'))
            server = config['mcpServers']['zcode_bridge']
            self.assertEqual(server['env']['ZCODE_BRIDGE_STATE'], str(state.resolve()))
            self.assertEqual((state/'config.json').read_bytes(), before)
            self.assertFalse((root/'default-appdata'/'CateyeZCodeBridge').exists())
            self.assertEqual(set(server['env_vars']), {
                'CODEX_APP_TOOLS_PIPE_PATH', 'CODEX_APP_RUNTIME_CLI_PATH',
                'CODEX_MCP_NODE_PATH'})


if __name__ == '__main__':
    unittest.main()
