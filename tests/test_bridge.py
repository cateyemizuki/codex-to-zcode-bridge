import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from zcode_bridge import mcp, storage
from zcode_bridge.service import Bridge, caller_mode, compact_messages
from zcode_bridge.tools import TOOLS
from zcode_bridge.workflows import actor_session_id, topology

SID='sess_test'


def snapshot(mode='yolo', count=1, status='idle'):
    return {'session':{'sessionId':SID},'projection':{'status':status,'turnCount':count},
        'runtime':{},'settings':{'permission':{'mode':mode},
        'model':{'current':{'providerId':'p','modelId':'m'},'available':[{'large':'catalogue'}]}}}


def message(ident, text='text', **info):
    return {'info':{'id':ident,'role':'assistant',**info},'parts':[{'type':'text','text':text}]}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'ZCODE_BRIDGE_STATE':self.tmp.name})
        self.env.start()
        executable=Path(self.tmp.name)/'ZCode.exe'
        executable.touch()
        self.cfg=patch('zcode_bridge.service.config', return_value={**storage.defaults(),
            'zcode_executable':str(executable),'auto_launch_desktop':True,
            'default_model':{'providerId':'p','modelId':'m'},'default_thought_level':'high',
            'default_workspace':self.tmp.name})
        self.cfg.start()
        self.bridge=Bridge()
        self.bridge.client=Mock()
        self.bridge.resident.add(SID)
        self.bridge.rpc=Mock(return_value=snapshot())
        self.bridge.backend=Mock(return_value=self.bridge.client)

    def tearDown(self):
        self.cfg.stop()
        self.env.stop()
        self.tmp.cleanup()

    def request(self,wire='server-1',**extra):
        return {'id':wire,'method':'interaction/requestUserInput','params':{
            'requestId':'logical-1','sessionId':SID,'toolCallId':'call-1','toolName':'AskUserQuestion',
            'input':{'questions':[{'question':'Test?'}]},**extra}}

    def test_unknown_or_missing_caller_does_not_escalate(self):
        for value in (None,'yolo','unknown'):
            with self.assertRaises(ValueError): caller_mode({'caller_permission_mode':value})

    def test_all_supported_permission_mappings(self):
        expected={'full_access':'yolo','read_only':'plan','always_ask':'build',
            'ask_before_writes':'build','review_important_actions':'edit'}
        for key,value in expected.items():
            self.assertEqual(caller_mode({'caller_permission_mode':key}),value)

    def test_readonly_clears_old_full_access_before_plan_overlay(self):
        self.bridge.rpc.return_value=snapshot('build')
        self.bridge.inherit(SID,{'caller_permission_mode':'read_only'})
        self.assertEqual([c.args[1]['mode'] for c in self.bridge.rpc.call_args_list],['build','plan'])

    def test_unapplied_downgrade_blocks_send(self):
        with self.assertRaises(RuntimeError):
            self.bridge.call('zcode_send',{'session_id':SID,'content':'x','caller_permission_mode':'always_ask'})
        self.assertNotIn('session/send',[c.args[0] for c in self.bridge.rpc.call_args_list])

    def test_current_caller_replaces_stored_full_access(self):
        self.bridge.sessions[SID]={'caller_permission_mode':'full_access'}
        self.bridge.rpc.return_value=snapshot('edit')
        self.bridge.inherit(SID,{'caller_permission_mode':'review_important_actions'})
        self.assertEqual(self.bridge.sessions[SID]['caller_permission_mode'],'review_important_actions')

    def test_reannouncement_updates_wire_without_duplicate_pending(self):
        self.bridge._request(self.request())
        self.bridge._request(self.request('server-2'))
        self.assertEqual(len(self.bridge.pending),1)
        self.assertEqual(self.bridge.pending['logical-1']['wire_id'],'server-2')

    def test_delayed_answer_uses_latest_wire(self):
        self.bridge._request(self.request())
        self.bridge._request(self.request('server-7'))
        response={'action':'accept','content':{'answer':'yes'}}
        self.bridge.call('zcode_answer',{'request_id':'logical-1','response':response,'caller_permission_mode':'full_access'})
        self.bridge.client.respond.assert_called_once_with('server-7',response)
        self.assertFalse(self.bridge.pending)

    def test_identical_reannouncement_replays_answer(self):
        self.bridge._request(self.request())
        response={'action':'cancel'}
        self.bridge.call('zcode_answer',{'request_id':'logical-1','response':response,'caller_permission_mode':'full_access'})
        self.bridge._request(self.request('server-9'))
        self.bridge.client.respond.assert_called_with('server-9',response)
        self.assertFalse(self.bridge.pending)

    def test_changed_payload_never_replays_prior_authorization(self):
        self.bridge._request(self.request())
        self.bridge.call('zcode_answer',{'request_id':'logical-1','response':{'action':'cancel'},'caller_permission_mode':'full_access'})
        self.bridge.client.respond.reset_mock()
        self.bridge._request(self.request('server-9',input={'questions':[{'question':'Different?'}]}))
        self.bridge.client.respond.assert_not_called()
        self.assertIn('logical-1',self.bridge.pending)

    def test_reconnect_invalidates_old_wire_answer(self):
        self.bridge._request(self.request())
        def reconnect():
            self.bridge.pending.clear()
            return self.bridge.client
        self.bridge.backend.side_effect=reconnect
        with self.assertRaises(ValueError):
            self.bridge.call('zcode_answer',{'request_id':'logical-1','response':{'action':'cancel'},'caller_permission_mode':'full_access'})
        self.bridge.client.respond.assert_not_called()

    def test_permission_allow_requires_user_in_approval_mode(self):
        request=self.request()
        request['method']='interaction/requestPermission'
        request['params']['options']=[{'optionId':'allow_once','response':{'decision':'allow'}}]
        self.bridge._request(request)
        args={'request_id':'logical-1','option_id':'allow_once','caller_permission_mode':'always_ask'}
        with self.assertRaises(ValueError): self.bridge.call('zcode_answer',args)
        self.bridge.client.respond.assert_not_called()
        self.bridge.call('zcode_answer',{**args,'user_approved':True})
        self.bridge.client.respond.assert_called_once_with('server-1',{'decision':'allow'})

    def test_answering_nonexistent_request_is_error(self):
        with self.assertRaises(ValueError):
            self.bridge.call('zcode_answer',{'request_id':'old','caller_permission_mode':'full_access','response':{}})

    def test_pending_can_be_scoped_to_exact_session(self):
        self.bridge._request(self.request())
        self.assertEqual(self.bridge.call('zcode_pending',{'session_id':'sess_other'})['requests'],[])

    def test_forward_message_page_does_not_ask_native_tail_slice(self):
        self.bridge.rpc.return_value={'messages':[message(str(i)) for i in range(1,8)]}
        result=self.bridge.call('zcode_messages',{'session_id':SID,'after_message_id':'0','limit':3})
        self.assertNotIn('limit',self.bridge.rpc.call_args.args[1])
        self.assertEqual([m['id'] for m in result['messages']],['1','2','3'])
        self.assertEqual(result['after_message_id'],'3')
        self.assertTrue(result['more'])

    def test_forward_message_page_accepts_318_message_ids(self):
        rows=[{'info':{'messageId':str(i),'role':'assistant'},'parts':[{'type':'text','text':'x'}]}
            for i in range(1,8)]
        self.bridge.rpc.return_value={'messages':rows}
        result=self.bridge.call('zcode_messages',{'session_id':SID,'after_message_id':'0','limit':3})
        self.assertEqual([m['id'] for m in result['messages']],['1','2','3'])
        self.assertEqual(result['after_message_id'],'3')
        self.assertTrue(result['more'])

    def test_message_page_without_any_id_keeps_caller_anchor(self):
        rows=[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'x'}]} for _ in range(3)]
        self.bridge.rpc.return_value={'messages':rows}
        result=self.bridge.call('zcode_messages',{'session_id':SID,'after_message_id':'anchor-0','limit':3})
        self.assertTrue(all(m['id'] is None for m in result['messages']))
        self.assertEqual(result['after_message_id'],'anchor-0')

    def test_message_identity_prefers_legacy_id(self):
        row={'info':{'id':'legacy','messageId':'m318'},'parts':[]}
        self.assertEqual(compact_messages([row])[0]['id'],'legacy')

    def test_message_identity_accepts_318_shape(self):
        row={'info':{'messageId':'m318','role':'assistant'},'parts':[]}
        self.assertEqual(compact_messages([row])[0]['id'],'m318')

    def test_message_identity_accepts_top_level_message_id(self):
        row={'info':{'role':'assistant'},'messageId':'top','parts':[]}
        self.assertEqual(compact_messages([row])[0]['id'],'top')

    def test_raw_transcript_is_available_explicitly(self):
        raw={'messages':[message('1',synthetic=True)]}
        self.bridge.rpc.return_value=raw
        self.assertEqual(self.bridge.call('zcode_messages',{'session_id':SID,'raw':True}),raw)

    def test_forward_native_events_preserve_first_unread_page(self):
        self.bridge.rpc.return_value={'events':[{'seq':i} for i in range(3,9)]}
        result=self.bridge.call('zcode_events',{'session_id':SID,'after_cursor':2,'limit':2})
        self.assertNotIn('limit',self.bridge.rpc.call_args.args[1])
        self.assertEqual([e['seq'] for e in result['events']],[3,4])
        self.assertEqual(result['cursor'],4)
        self.assertTrue(result['more'])

    def test_filters_synthetic_hidden_and_model_only(self):
        rows=[message('visible'),message('s',synthetic=True),message('h',semantics={'uiVisibility':'hidden'}),
            message('m',visibility='model-only')]
        self.assertEqual([m['id'] for m in compact_messages(rows)],['visible'])

    def test_reasoning_and_synthetic_parts_are_excluded(self):
        row=message('visible')
        row['parts'] += [{'type':'reasoning','text':'private'},{'type':'text','text':'hidden','synthetic':True}]
        self.assertEqual(len(compact_messages([row])[0]['parts']),1)

    def test_child_submit_result_remains_readable(self):
        row=message('child')
        row['parts']=[{'type':'tool','tool':'submit_result','state':{'status':'completed','input':{'result':{'file':'x','written':True}}}}]
        part=compact_messages([row])[0]['parts'][0]
        self.assertEqual(json.loads(part['result']),{'file':'x','written':True})

    def test_text_budget_reports_truncation(self):
        part=compact_messages([message('id','abcdef')],3)[0]['parts'][0]
        self.assertEqual(part['text'],'abc')
        self.assertTrue(part['truncated'])

    def test_status_does_not_repeat_full_model_catalogue(self):
        original=snapshot()
        self.assertNotIn('available',self.bridge.summary(original)['settings']['model'])
        self.assertIn('available',original['settings']['model'])

    def test_wait_does_not_confuse_accepted_with_completed(self):
        self.bridge.accepted[SID]={'before_turn_count':0}
        self.bridge.snapshot=Mock(side_effect=[snapshot(count=0),snapshot(count=1)])
        with patch('zcode_bridge.service.time.sleep'):
            result=self.bridge.call('zcode_wait',{'session_id':SID,'timeout_seconds':3})
        self.assertEqual(result['projection']['turnCount'],1)
        self.assertFalse(result['delivery']['awaiting_start'])

    def test_wait_ignores_unrelated_session_question(self):
        self.bridge._request(self.request(sessionId='sess_other'))
        self.bridge.snapshot=Mock(side_effect=[snapshot(status='running'),snapshot()])
        with patch('zcode_bridge.service.time.sleep'):
            result=self.bridge.call('zcode_wait',{'session_id':SID,'timeout_seconds':3})
        self.assertEqual(result['projection']['status'],'idle')
        self.assertEqual(result['pending_requests'],[])

    def test_wait_continues_for_active_background_workflow(self):
        active=snapshot()
        active['projection']['backgroundJobs']=[{'status':'running'}]
        self.bridge.snapshot=Mock(side_effect=[active,snapshot()])
        with patch('zcode_bridge.service.time.sleep'):
            self.bridge.call('zcode_wait',{'session_id':SID,'timeout_seconds':3})
        self.assertEqual(self.bridge.snapshot.call_count,2)

    def test_readonly_cannot_write_settings(self):
        with self.assertRaises(ValueError):
            self.bridge.call('zcode_settings',{'changes':{},'caller_permission_mode':'read_only'})

    def test_resume_retains_managed_workflow_gate(self):
        self.bridge.sessions[SID]={'workflow_mode':'onDemand'}
        self.bridge.resident.clear()
        self.bridge.ensure_session(SID)
        self.assertTrue(self.bridge.rpc.call_args.args[1]['dynamicWorkflowEnabled'])

    def test_model_change_requests_no_workspace_preference_write(self):
        self.bridge.call('zcode_set_model',{'session_id':SID,'model':{'providerId':'p','modelId':'m'},'thought_level':'high','caller_permission_mode':'full_access'})
        calls=[c for c in self.bridge.rpc.call_args_list if c.args[0] in ('session/setModel','session/setThoughtLevel')]
        self.assertEqual(len(calls),2)
        self.assertTrue(all(c.args[1]['persistAsWorkspaceLastUsed'] is False for c in calls))

    def test_changing_model_uses_its_supported_default_effort(self):
        self.bridge.model_catalog[('qwen','qwen')]={'reasoning':{'levels':[{'value':'enabled'}],'defaultLevel':'enabled'}}
        reference=self.bridge.model_ref({'providerId':'qwen','modelId':'qwen'})
        self.assertEqual(reference['options']['reasoningLevel'],'enabled')
        self.bridge.rpc.assert_not_called()

    def test_auto_launch_when_absent_uses_real_executable(self):
        with patch('zcode_bridge.service.desktop_running',return_value=False),patch('zcode_bridge.service.subprocess.Popen') as launch:
            result=self.bridge.launch()
        self.assertTrue(result['launched'])
        self.assertEqual(Path(launch.call_args.args[0][0]).name,'ZCode.exe')

    def test_does_not_launch_duplicate_desktop(self):
        with patch('zcode_bridge.service.desktop_running',return_value=True),patch('zcode_bridge.service.subprocess.Popen') as launch:
            self.assertTrue(self.bridge.launch()['already_running'])
        launch.assert_not_called()

    def test_invalid_config_object_fails_clearly(self):
        storage.write_json(Path(self.tmp.name)/'config.json',[])
        with self.assertRaises(ValueError): storage.config()

    def test_unknown_settings_are_rejected(self):
        with self.assertRaises(ValueError): storage.save_config({'api_key':'do not accept secrets'})

    def test_concurrent_atomic_storage_has_no_temp_collision(self):
        path=Path(self.tmp.name)/'atomic.json'
        errors=[]
        def write(i):
            try: storage.write_json(path,{'value':i})
            except Exception as error: errors.append(error)
        threads=[threading.Thread(target=write,args=(i,)) for i in range(16)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertIn(json.loads(path.read_text())['value'],range(16))
        self.assertEqual(list(path.parent.glob('*.tmp')),[])

    def test_topology_does_not_load_persona_prompts(self):
        events=[{'type':'actor-created','payload':{'actor':{'siteId':'actor#1','ordinal':1},'name':'writer',
            'persona':{'model':'p/m$high','system':'long private prompt'}}},
            {'type':'run-settled','payload':{'status':'completed'}}]
        result=topology('run-1',events)
        self.assertEqual(result['status'],'completed')
        self.assertNotIn('private prompt',json.dumps(result))
        self.assertEqual(result['actors'][0]['configured_model'],'p/m$high')

    def test_actor_id_matches_verified_native_id(self):
        self.assertEqual(actor_session_id('dwfrun-abc',{'siteId':'actor#1','ordinal':1}),
            'sess_dwf-dwfrun-abc-actor_1_1')

    def test_tools_are_unique_and_all_have_schemas(self):
        self.assertEqual(len(TOOLS),len({t['name'] for t in TOOLS}))
        self.assertTrue(all(t['inputSchema']['type']=='object' for t in TOOLS))

    def test_mcp_app_resource_is_bundled(self):
        result=mcp.handle({'method':'resources/read','params':{'uri':mcp.UI_URI}})
        self.assertIn('默认主代理模型',result['contents'][0]['text'])
        self.assertEqual(result['contents'][0]['mimeType'],'text/html;profile=mcp-app')

    def test_mcp_wait_does_not_block_concurrent_requests(self):
        root=Path(__file__).resolve().parents[1]/'runtime'
        script=('import sys,time;sys.path.insert(0,sys.argv[1]);from zcode_bridge import mcp;'
            'mcp.local_call=lambda name,args:(time.sleep(.6) or {}) if name=="zcode_wait" else {};mcp.serve()')
        process=subprocess.Popen([sys.executable,'-c',script,str(root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        requests=[{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'zcode_wait','arguments':{}}},
            {'jsonrpc':'2.0','id':2,'method':'ping'}]
        output,error=process.communicate(('\n'.join(json.dumps(r) for r in requests)+'\n').encode(),timeout=8)
        self.assertEqual(process.returncode,0,error.decode())
        self.assertEqual([json.loads(line)['id'] for line in output.splitlines()],[2,1])


if __name__=='__main__': unittest.main()
