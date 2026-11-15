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
            'superagent_stall_seconds':600,'superagent_read_failure_threshold':5,
            'desktop_sync_enabled':False,'desktop_sync_control_file':'',
            'desktop_sync_min_interval_seconds':5,
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
        for key in ('default_transport','desktop_native_enabled',
                    'desktop_native_control_file'):
            self.assertIn(key, result['schema']['properties'])
            self.assertIn(key, result['values'])
        self.assertEqual(result['values']['default_transport'], 'engine')
        self.assertTrue(any(group.get('title','').startswith('会话后端')
                            for group in result.get('layout') or []))

    def test_native_updates_only_changed_fields(self):
        result=preferences.update({'set':{'auto_launch_desktop':False}})
        self.assertFalse(result['values']['auto_launch_desktop'])
        self.assertEqual(result['values']['default_workspace'],str(Path(tempfile.gettempdir())/'codex-to-zcode-bridge-test'))

    def test_native_transport_update_is_validated(self):
        with self.assertRaises(ValueError):
            preferences.update({'set':{'default_transport':'auto'}})
        result=preferences.update({'set':{
            'default_transport':'desktop_native','desktop_native_enabled':True}})
        self.assertEqual(result['values']['default_transport'],'desktop_native')
        self.assertTrue(result['values']['desktop_native_enabled'])

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

    def test_notification_operating_parameters_are_visible_and_bounded(self):
        result=preferences.read()
        for key in preferences.NOTIFY_TITLES:
            self.assertIn(key,result['schema']['properties'])
            self.assertIn(key,result['values'])
        self.assertEqual(result['values']['completion_notify_poll_seconds'],1)
        updated=preferences.update({'set':{'completion_notify_wake_attempts':7}})
        self.assertEqual(updated['values']['completion_notify_wake_attempts'],7)
        for changes in ({'completion_notify_expiry_hours':25},
                        {'completion_notify_poll_seconds':0},
                        {'completion_notify_start_timeout_seconds':'120'}):
            with self.assertRaises(ValueError): preferences.update({'set':changes})

    def test_superagent_diagnostics_and_desktop_sync_are_visible_and_bounded(self):
        result=preferences.read()
        for key in (*preferences.SUPERAGENT_TITLES,*preferences.DESKTOP_SYNC_TITLES):
            self.assertIn(key,result['schema']['properties'])
            self.assertIn(key,result['values'])
        self.assertFalse(result['values']['desktop_sync_enabled'])
        self.assertEqual(result['values']['desktop_sync_min_interval_seconds'],5)
        self.assertEqual(result['values']['superagent_stall_seconds'],600)
        updated=preferences.update({'set':{
            'desktop_sync_enabled':True,
            'desktop_sync_control_file':str(Path(tempfile.gettempdir())/'private-control.json'),
            'desktop_sync_min_interval_seconds':7,
            'superagent_stall_seconds':120,
            'superagent_read_failure_threshold':3,
        }})
        self.assertTrue(updated['values']['desktop_sync_enabled'])
        self.assertEqual(updated['values']['desktop_sync_min_interval_seconds'],7)
        self.assertEqual(updated['values']['superagent_stall_seconds'],120)
        self.assertEqual(updated['values']['superagent_read_failure_threshold'],3)
        for changes in ({'desktop_sync_enabled':'true'},
                        {'desktop_sync_min_interval_seconds':0},
                        {'superagent_stall_seconds':10},
                        {'superagent_read_failure_threshold':1},
                        {'desktop_sync_token':'secret'}):
            with self.assertRaises(ValueError): preferences.update({'set':changes})

    def test_no_settings_key_can_grant_wake_or_auto_continue(self):
        from zcode_bridge import storage
        from zcode_bridge.tools import TOOLS
        forbidden={'completion_notify_wake_enabled','completion_notify_auto_continue_enabled',
                   'completion_notify_enabled'}
        self.assertEqual(forbidden & set(storage.defaults()),set())
        result=preferences.read()
        self.assertEqual(forbidden & set(result['schema']['properties']),set())
        with self.assertRaises(ValueError):
            preferences.update({'set':{'completion_notify_wake_enabled':True}})
        by_name={tool['name']:tool for tool in TOOLS}
        subscribe=by_name['zcode_notify_subscribe']['inputSchema']
        self.assertIn('user_authorized',subscribe['required'])
        self.assertIn('caller_permission_mode',subscribe['required'])
        notify=by_name['zcode_send']['inputSchema']['properties']['notify']
        self.assertIn('user_authorized',notify['required'])

    def test_panel_says_inbox_is_not_a_wake_and_cancel_stays_in_the_chat(self):
        text=(Path(__file__).resolve().parents[1]/'runtime/zcode_bridge/ui/settings.html').read_text(encoding='utf-8')
        self.assertIn('收件箱可拉取 ≠ 已唤醒模型',text)
        self.assertIn('不提供取消按钮',text)
        self.assertIn('zcode_notify_cancel',text)
        for key in preferences.NOTIFY_TITLES.values():
            self.assertIn(key,text)
        self.assertIn('无可观测进展判定（秒）',text)
        self.assertIn('连续读取失败阈值',text)
        self.assertIn('启用 Desktop 同步',text)
        self.assertIn('Desktop 同步最小间隔（秒）',text)
        self.assertIn('token 只从私有控制文件读取',text)


if __name__=='__main__': unittest.main()
