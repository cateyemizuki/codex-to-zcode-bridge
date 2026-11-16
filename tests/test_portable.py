import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
sys.path.insert(0,str(ROOT))
from zcode_bridge.discovery import paths
from zcode_bridge.service import Bridge
from scripts.audit_release import inspect


class PortableTests(unittest.TestCase):
    def test_discovery_uses_the_installed_machine(self):
        with tempfile.TemporaryDirectory() as temporary:
            app=Path(temporary)/'custom application'
            (app/'resources/glm').mkdir(parents=True)
            (app/'ZCode.exe').touch()
            (app/'resources/glm/zcode.cjs').touch()
            with patch('zcode_bridge.discovery._registry_locations',return_value=[]), \
                    patch('zcode_bridge.discovery.shutil.which',return_value=None):
                result=paths({'ZCODE_INSTALL_DIR':str(app)},home=temporary)
            self.assertEqual(result['zcode_executable'],str((app/'ZCode.exe').resolve()))
            self.assertEqual(result['engine_entry'],str((app/'resources/glm/zcode.cjs').resolve()))
            self.assertEqual(result['default_workspace'],str(Path(temporary).resolve()))

    def test_model_listing_does_not_require_a_publisher_model(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ,{'ZCODE_BRIDGE_STATE':str(Path(temporary)/'state')}):
            bridge=Bridge()
            calls=[]
            def rpc(method,params):
                calls.append((method,params))
                return {'session':{'sessionId':'sess_catalogue'},'settings':{'model':{'available':[]}}}
            bridge.rpc=rpc
            with patch('zcode_bridge.service.config',return_value={'default_workspace':temporary,
                    'default_model':{'providerId':'','modelId':''}}):
                self.assertEqual(bridge.call('zcode_models',{}),{'available':[]})
            self.assertNotIn('model',calls[0][1])
            self.assertEqual(calls[0][1]['persistence'],'deferred')

    def test_empty_default_model_requires_an_actual_selection(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ,{'ZCODE_BRIDGE_STATE':str(Path(temporary)/'state')}):
            with self.assertRaisesRegex(ValueError,'默认模型'):
                Bridge().model_ref({'providerId':'','modelId':''})

    def test_explicit_auto_effort_uses_supported_model_default(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ,{'ZCODE_BRIDGE_STATE':str(Path(temporary)/'state')}):
            bridge=Bridge()
            bridge.model_catalog[('p','m')]={'reasoning':{'levels':[{'value':'low'},{'value':'high'}],'defaultLevel':'low'}}
            with patch('zcode_bridge.service.config',return_value={'default_model':{'providerId':'p','modelId':'m'},'default_thought_level':'high'}):
                ref=bridge.model_ref({'providerId':'p','modelId':'m'},'auto')
            self.assertEqual(ref['options']['reasoningLevel'],'low')

    def run_installer(self,host,temporary,dry=False):
        base=Path(temporary)
        moved=base/'another location with spaces'/host
        shutil.copytree(ROOT/'plugins'/host,moved)
        target=base/'installed elsewhere'
        state=base/'state'
        env={**os.environ,'ZCODE_BRIDGE_STATE':str(state),'HOME':str(base/'home'),
            'USERPROFILE':str(base/'home'),'LOCALAPPDATA':str(base/'appdata')}
        args=[sys.executable,str(moved/'install.py'),'--install-root',str(target),'--no-register']
        if dry:
            args.append('--dry-run')
        result=subprocess.run(args,cwd=base,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        if dry:
            self.assertFalse(target.exists())
            self.assertFalse(state.exists())
            return
        mcp=json.loads((target/'plugins'/host/'.mcp.json').read_text(encoding='utf-8'))['mcpServers']['zcode_bridge']
        self.assertEqual(mcp['command'],sys.executable)
        self.assertTrue(Path(mcp['args'][0]).is_relative_to(target))
        self.assertEqual(mcp['env']['ZCODE_BRIDGE_STATE'], str(state.resolve()))
        if host == 'codex':
            self.assertEqual(set(mcp['env_vars']), {
                'CODEX_APP_TOOLS_PIPE_PATH', 'CODEX_APP_RUNTIME_CLI_PATH',
                'CODEX_MCP_NODE_PATH'})
        else:
            self.assertNotIn('env_vars', mcp)
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},
            {'jsonrpc':'2.0','id':3,'method':'resources/read','params':{'uri':'ui://codex-to-zcode-bridge/settings.html'}}]
        process=subprocess.run([mcp['command'],*mcp['args']],input='\n'.join(json.dumps(x) for x in requests)+'\n',
            cwd=base,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(process.returncode,0,process.stderr)
        replies={r['id']:r for r in map(json.loads,process.stdout.splitlines())}
        self.assertEqual(replies[1]['result']['serverInfo']['version'],'0.5.3')
        self.assertEqual(len(replies[2]['result']['tools']),34)
        self.assertIn('默认主代理模型',replies[3]['result']['contents'][0]['text'])
        self.assertFalse((state/'connection.json').exists(),'MCP discovery must not start an engine')
        if host=='zcode':
            raw=json.loads((moved/'.mcp.json').read_text(encoding='utf-8'))['mcpServers']['zcode_bridge']
            arguments=[str(moved) if a=='${CLAUDE_PLUGIN_ROOT}' else a for a in raw['args']]
            direct=subprocess.run([sys.executable,*arguments],input='\n'.join(json.dumps(x) for x in requests)+'\n',
                cwd=base,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertEqual(direct.returncode,0,direct.stderr)
            messages={r['id']:r for r in map(json.loads,direct.stdout.splitlines())}
            self.assertEqual(len(messages[2]['result']['tools']),34)

    def test_codex_package_runs_after_relocation_and_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.run_installer('codex',temporary)

    def test_zcode_package_runs_after_relocation_and_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.run_installer('zcode',temporary)

    def test_dry_run_does_not_create_configuration_or_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.run_installer('codex',temporary,dry=True)

    def test_host_packages_keep_their_own_instructions(self):
        self.assertFalse((ROOT/'plugins/codex/skills').exists())
        self.assertTrue((ROOT/'plugins/zcode/skills/bridge-control/SKILL.md').is_file())
        for host in ('codex','zcode'):
            mcp=json.loads((ROOT/'plugins'/host/'.mcp.json').read_text(encoding='utf-8'))
            expected = ['-m','zcode_bridge','mcp'] if host=='codex' else ['${CLAUDE_PLUGIN_ROOT}','mcp']
            actual = mcp['mcpServers']['zcode_bridge']['args'] if host=='codex' else mcp['mcpServers']['zcode_bridge']['args'][-2:]
            self.assertEqual(actual,expected)

    def test_release_audit_rejects_local_state(self):
        for name in ('sessions.json','notifications.json','notifications.incompatible.abc.json',
                     'host_capability.json','trace.log'):
            self.assertTrue(inspect(name,b'{}'),name)
        self.assertFalse(inspect('README.md',b'Use your local configuration.'))

    def test_build_output_is_lf_and_contains_notification_modules(self):
        import build
        with tempfile.TemporaryDirectory() as temporary:
            stage=Path(temporary)/'stage'
            build.build(stage)
            for host in ('codex','zcode'):
                package=stage/'plugins'/host/'zcode_bridge'
                for module in ('notifications.py','delivery.py','host_wake.py'):
                    self.assertTrue((package/module).is_file(),host+'/'+module)
            offenders=[]
            for path in stage.rglob('*'):
                if path.is_file() and path.suffix in ('.json','.toml','.py','.html','.md'):
                    if b'\r\n' in path.read_bytes():
                        offenders.append(str(path.relative_to(stage)))
            self.assertEqual(offenders,[])

    def test_generated_packages_have_no_crlf_in_checked_in_tree(self):
        offenders=[]
        for prefix in ('plugins','.agents','.claude-plugin'):
            for path in (ROOT/prefix).rglob('*'):
                if path.is_file() and path.suffix in ('.json','.toml','.py','.html','.md'):
                    if b'\r\n' in path.read_bytes():
                        offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders,[])


if __name__=='__main__':
    unittest.main()
