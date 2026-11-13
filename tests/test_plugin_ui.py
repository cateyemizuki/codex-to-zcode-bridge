import copy
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from zcode_bridge import mcp, preferences


class NativeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'default_workspace':str(Path(tempfile.gettempdir())/'codex-to-zcode-bridge-test'),'default_model':{'providerId':'p','modelId':'ds'},
            'default_thought_level':'max','default_workflow_mode':'onDemand','auto_launch_desktop':True,
            **{key:'' for key in preferences.PATH_TITLES}}
        self.rows=[{'ref':{'providerId':'p','modelId':'ds'},'providerLabel':'DS','label':'ds',
            'reasoning':{'levels':[{'value':'high'},{'value':'max'}],'defaultLevel':'max'}},
            {'ref':{'providerId':'q','modelId':'qwen'},'providerLabel':'Qwen','label':'qwen',
            'reasoning':{'levels':[{'value':'enabled'}],'defaultLevel':'enabled'}}]
        self.calls=[]
        def call(name,args=None):
            self.calls.append((name,args))
            if name=='zcode_models': return {'available':copy.deepcopy(self.rows)}
            if args and args.get('changes'): self.cfg.update(args['changes'])
            return copy.deepcopy(self.cfg)
        self.patcher=patch('zcode_bridge.preferences.local_call',side_effect=call)
        self.patcher.start()

    def tearDown(self): self.patcher.stop()

    def test_all_native_properties_have_primitive_effective_values(self):
        result=preferences.read()
        self.assertEqual(set(result['schema']['properties']),set(result['values']))
        self.assertTrue(all(type(v) in (str,bool,int,float) for v in result['values'].values()))
        self.assertFalse(any(args and 'changes' in args for _,args in self.calls))

    def test_native_updates_only_changed_fields(self):
        result=preferences.update({'set':{'auto_launch_desktop':False}})
        self.assertFalse(result['values']['auto_launch_desktop'])
        self.assertEqual(result['values']['default_workspace'],str(Path(tempfile.gettempdir())/'codex-to-zcode-bridge-test'))

    def test_native_path_controls_remain_available_when_engine_is_unavailable(self):
        def call(name,args=None):
            if name=='zcode_models': raise RuntimeError('Node not found')
            return self.cfg
        with patch('zcode_bridge.preferences.local_call',side_effect=call):
            result=preferences.read()
        self.assertIn('node_executable',result['schema']['properties'])
        self.assertIn('连接未就绪',result['values']['default_model'])

    def test_native_model_change_resolves_label_and_compatible_effort(self):
        result=preferences.update({'set':{'default_model':'Qwen · qwen'}})
        self.assertEqual(self.cfg['default_model'],{'providerId':'q','modelId':'qwen'})
        self.assertEqual(result['values']['default_thought_level'],'enabled')

    def test_unsupported_explicit_effort_is_rejected(self):
        with self.assertRaises(ValueError):
            preferences.update({'set':{'default_model':'Qwen · qwen','default_thought_level':'max'}})
        self.assertEqual(self.cfg['default_model']['modelId'],'ds')

    def test_unknown_and_wrong_types_are_rejected(self):
        for changes in ({'api_key':'x'},{'auto_launch_desktop':'true'},{'default_workspace':False}):
            with self.assertRaises(ValueError): preferences.update({'set':changes})

    def test_native_setting_capability_and_tools_are_advertised(self):
        result=mcp.handle({'method':'initialize','params':{'protocolVersion':'2025-11-25'}})
        for field in ('extensions','experimental'):
            self.assertEqual(result['capabilities'][field]['openai/settings'],preferences.CAPABILITY)
        tools={t['name']:t for t in mcp.handle({'method':'tools/list'})['tools']}
        self.assertIn('outputSchema',tools[preferences.READ_TOOL])
        self.assertEqual(tools[preferences.UPDATE_TOOL]['_meta']['ui']['visibility'],['app'])
        self.assertEqual(tools['zcode_open_settings']['_meta']['openai/ui']['entrypoints'],[{'type':'global'},{'type':'thread'}])

    def test_embedded_panel_does_not_open_external_browser(self):
        with patch('zcode_bridge.mcp.local_call',return_value=self.cfg) as call:
            result=mcp.handle({'method':'tools/call','params':{'name':'zcode_open_settings','arguments':{}}})
        self.assertEqual(result['structuredContent']['transport'],'mcp-app')
        call.assert_called_once_with('zcode_settings')

    def test_native_settings_calls_work_without_a_skill(self):
        result=mcp.handle({'method':'tools/call','params':{'name':preferences.READ_TOOL,'arguments':{}}})
        self.assertEqual(result['structuredContent']['values']['default_model'],'DS · ds')
        result=mcp.handle({'method':'tools/call','params':{'name':preferences.UPDATE_TOOL,'arguments':{'set':{'default_workflow_mode':'disabled'}}}})
        self.assertEqual(result['structuredContent']['values']['default_workflow_mode'],'disabled')


if __name__=='__main__': unittest.main()
