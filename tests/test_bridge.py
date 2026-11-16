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
from zcode_bridge.protocol import ProtocolError
from zcode_bridge.service import ObserverRuntimeAdapter, caller_mode, compact_messages
from bridge_fixtures import MockBridge as Bridge
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

    def test_last_message_anchor_skips_318_synthetic_tokens_record(self):
        rows=[
            {'info':{'messageId':'m2','role':'assistant'},'parts':[]},
            {'info':{'messageId':'tokens','role':'user','synthetic':True,
                     'semantics':{'uiVisibility':'hidden'}},'parts':[]},
        ]
        self.bridge.rpc.return_value={'messages':rows}
        self.assertEqual(self.bridge._last_message_id(SID),'m2')
        self.assertEqual(self.bridge._last_user_message_id(SID),'')

    def test_last_user_anchor_uses_318_message_id_and_skips_synthetic(self):
        rows=[
            {'info':{'messageId':'old-user','role':'user'},'parts':[]},
            {'info':{'messageId':'real-user','role':'user'},'parts':[]},
            {'info':{'messageId':'synth-user','role':'user','synthetic':True,
                     'semantics':{'uiVisibility':'hidden'}},'parts':[]},
        ]
        self.bridge.rpc.return_value={'messages':rows}
        self.assertEqual(self.bridge._last_user_message_id(SID),'real-user')

    def test_reconcile_skips_synthetic_and_uses_318_message_id(self):
        sub={'session_id':SID,'turn':{'input_id':'inp-1'}}
        rows=[
            {'info':{'messageId':'synth-user','role':'user','inputId':'inp-1','synthetic':True,
                     'semantics':{'uiVisibility':'hidden'}},'parts':[]},
            {'info':{'messageId':'real-user','role':'user','inputId':'inp-1'},'parts':[]},
        ]
        self.bridge.rpc.return_value={'messages':rows}
        self.assertEqual(self.bridge._reconcile_send(sub),
            {'messageId':'real-user','userMessageId':'real-user'})

    def test_bridge_exposes_observer_runtime_interface(self):
        runtime=ObserverRuntimeAdapter(self.bridge)
        for name in ('engine_identity','pending','ensure_session','snapshot','jobs',
                     'workflows','children','session_events','stream_id','messages',
                     'run_topology'):
            self.assertTrue(callable(getattr(runtime,name,None)),name)

    def test_observer_collect_uses_real_bridge_adapter(self):
        from zcode_bridge.notifications import Observer
        def rpc(method,params=None,deadline=None):
            if method=='session/read': return snapshot(count=1)
            if method=='session/messages':
                return {'messages':[{'info':{'messageId':'m1','role':'user'},'parts':[]},
                                    {'info':{'messageId':'m2','role':'assistant'},'parts':[]}]}
            if method=='session/events': return {'events':[]}
            if method=='session/subagents': return {'childSessionIds':[]}
            if method=='v4/conversation/workflowRuns': return {'runs':[]}
            return {}
        self.bridge.rpc=Mock(side_effect=rpc)
        self.bridge.client.child.pid=123
        sub={'subscription_id':'sub_test','session_id':SID,'revision':1,
             'turn':{'input_id':'inp-1','send_accepted':True,'send_result_message_id':'m1',
                     'before_message_id':'m0','before_turn_count':0,'sent_at':1,
                     'engine_pid':123,'generation':0},
             'native':{},'last_native_seq':0,'known_child_ids':[],
             'baseline':{'job_ids':[],'active_job_ids':[],'run_ids':[],
                         'terminal_run_ids':[],'active_run_ids':[],'child_ids':[],
                         'active_child_ids':[],'truncated':False}}
        evidence=Observer(self.bridge.notifications,ObserverRuntimeAdapter(self.bridge)).collect(sub)
        self.assertTrue(evidence['engine_ok'])
        self.assertEqual(evidence['status'],'idle')
        self.assertEqual(evidence['turn_count'],1)
        self.assertEqual(evidence['final_message_id'],'m2')

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

    def test_wait_without_subscription_never_confirms_completion(self):
        self.bridge.snapshot=Mock(return_value=snapshot())
        result=self.bridge.call('zcode_wait',{'session_id':SID,'timeout_seconds':1})
        self.assertEqual(result['verdict'],'unknown')
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['reason_code'],'no_subscription')
        self.assertIsNone(result['subscription_id'])

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
        with patch('zcode_bridge.service.ensure_desktop', return_value={
                'running':True,'pid':1,'already_running':False,'launched':True}) as ensure:
            result=self.bridge.launch()
        self.assertTrue(result['launched'])
        self.assertEqual(Path(ensure.call_args.args[0]).name,'ZCode.exe')

    def test_does_not_launch_duplicate_desktop(self):
        with patch('zcode_bridge.service.ensure_desktop', return_value={
                'running':True,'pid':1,'already_running':True,'launched':False}):
            self.assertTrue(self.bridge.launch()['already_running'])

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

    def test_subagents_reports_child_projection_not_run_status(self):
        actor={'siteId':'actor#1','ordinal':1}
        child=actor_session_id('run-1',actor)
        self.bridge.resident.add(child)
        def rpc(method,params=None,deadline=None):
            if method=='session/subagents':
                return {'childSessionIds':[child],'ended':[]}
            if method=='v4/conversation/workflowRuns':
                return {'runs':[{'runId':'run-1','status':'completed'}]}
            if method=='v4/conversation/workflowRunEvents':
                return {'events':[{'sequence':1,'type':'actor-created','payload':{'actor':actor}}],'hasMore':False}
            if method=='session/read':
                return {'session':{'sessionId':child},'projection':{'status':'waiting','turnCount':1},
                    'runtime':{},'settings':{'model':{'current':{'providerId':'p','modelId':'m'}},
                    'thoughtLevel':{'current':'high'}}}
            return snapshot()
        self.bridge.rpc=Mock(side_effect=rpc)
        result=self.bridge.call('zcode_subagents',{'session_id':SID})
        self.assertIn(child,result['childSessionIds'])
        actor_row=result['workflow_children'][0]
        self.assertEqual(actor_row['projection_status'],'waiting')
        self.assertEqual(actor_row['run_status'],'completed')
        self.assertTrue(actor_row['verified'])

    def test_mcp_app_resource_is_bundled(self):
        result=mcp.handle({'method':'resources/read','params':{'uri':mcp.UI_URI}})
        self.assertIn('默认主代理模型',result['contents'][0]['text'])
        self.assertEqual(result['contents'][0]['mimeType'],'text/html;profile=mcp-app')

    def test_turn_metadata_accepts_json_string_and_object(self):
        for meta in ({'x-codex-turn-metadata': {'thread_id': 'chat-json'}},
                     {'x-codex-turn-metadata': json.dumps({'thread_id': 'chat-json'})}):
            context=mcp.normalize_context(meta)
            self.assertEqual(context['thread_id'],'chat-json')
            self.assertEqual(context['thread_origin'],'turn_metadata')

    def test_turn_metadata_conflict_is_rejected_after_parsing(self):
        with self.assertRaisesRegex(ValueError,'source_conflict'):
            mcp.normalize_context({'x-codex-turn-metadata':json.dumps({'thread_id':'t1'}),
                                   'openai/threadId':'t2'})

    def test_mcp_wait_does_not_block_concurrent_requests(self):
        root=Path(__file__).resolve().parents[1]/'runtime'
        script=('import sys,time;sys.path.insert(0,sys.argv[1]);from zcode_bridge import mcp;'
            'mcp.local_call=lambda name,args,context=None,**kwargs:'
            '(time.sleep(.6) or {}) if name=="zcode_wait" else {};mcp.serve()')
        process=subprocess.Popen([sys.executable,'-c',script,str(root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        requests=[{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
                'name':'zcode_wait','arguments':{},
                '_meta':{'x-codex-turn-metadata':{'thread_id':'synthetic-test-thread'}}}},
            {'jsonrpc':'2.0','id':2,'method':'ping'}]
        output,error=process.communicate(('\n'.join(json.dumps(r) for r in requests)+'\n').encode(),timeout=8)
        self.assertEqual(process.returncode,0,error.decode())
        self.assertEqual([json.loads(line)['id'] for line in output.splitlines()],[2,1])


class FakeEngineClient:
    """Transport double at the Client boundary, so Bridge.rpc stays real."""

    def __init__(self,*outcomes):
        self.outcomes=list(outcomes)
        self.calls=[]
        self.child=Mock()
        self.child.poll.return_value=None
        self.child.pid=4242

    def request(self,method,params=None,timeout=40):
        self.calls.append((method,copy.deepcopy(params or {})))
        outcome=self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(outcome,BaseException):
            raise outcome
        if callable(outcome):
            return outcome(method,params)
        return copy.deepcopy(outcome)

    def respond(self,*args,**kwargs):
        pass

    def methods(self):
        return [name for name,_ in self.calls]


def runtime_not_ready():
    return ProtocolError({'code':-32004,'message':'Session runtime is not ready',
                          'data':{'code':'runtimeNotReady'}})


def session_not_found(sid):
    return ProtocolError({'code':-32004,'message':f'Session not found: {sid}'})


class StaleRuntimeRecoveryTests(unittest.TestCase):
    """A live engine can drop a resident runtime; only reads may re-attach."""

    SID='sess_stale'

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
        self.bridge.resident.add(self.SID)
        self.bridge.sessions[self.SID]={'workflow_mode':'onDemand'}
        self.client=FakeEngineClient()
        self.bridge.client=self.client
        self.bridge.generation=1
        self.bridge.capabilities_generation=1

    def tearDown(self):
        self.cfg.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_read_runtime_not_ready_resumes_same_session_then_retries_once(self):
        expected=snapshot()
        self.client.outcomes=[runtime_not_ready(),expected,expected]
        self.assertEqual(self.bridge.rpc('session/read',{'sessionId':self.SID,'messageLimit':10}),expected)
        self.assertEqual(self.client.methods(),['session/read','session/resume','session/read'])
        resume=self.client.calls[1][1]
        self.assertEqual(resume['sessionId'],self.SID)
        self.assertTrue(resume['dynamicWorkflowEnabled'])
        self.assertEqual(resume['dynamicWorkflowMode'],'onDemand')
        self.assertIn(self.SID,self.bridge.resident)

    def test_messages_session_not_found_resumes_same_session(self):
        expected={'messages':[message('m1')]}
        self.client.outcomes=[session_not_found(self.SID),snapshot(),expected]
        self.assertEqual(self.bridge.rpc('session/messages',{'sessionId':self.SID,'limit':5}),expected)
        self.assertEqual(self.client.methods(),['session/messages','session/resume','session/messages'])

    def test_events_runtime_not_ready_is_also_recovered(self):
        expected={'events':[]}
        self.client.outcomes=[runtime_not_ready(),snapshot(),expected]
        self.assertEqual(self.bridge.rpc('session/events',{'sessionId':self.SID,'afterSeq':0}),expected)
        self.assertEqual(self.client.methods(),['session/events','session/resume','session/events'])

    def test_failed_retry_raises_original_error_and_does_not_loop(self):
        original=runtime_not_ready()
        self.client.outcomes=[original,snapshot(),runtime_not_ready()]
        with self.assertRaises(ProtocolError) as caught:
            self.bridge.rpc('session/read',{'sessionId':self.SID})
        self.assertIs(caught.exception,original)
        self.assertEqual(self.client.methods(),['session/read','session/resume','session/read'])

    def test_failed_resume_keeps_original_error_and_skips_retry(self):
        original=runtime_not_ready()
        self.client.outcomes=[original,RuntimeError('resume failed')]
        with self.assertRaises(ProtocolError) as caught:
            self.bridge.rpc('session/read',{'sessionId':self.SID})
        self.assertIs(caught.exception,original)
        self.assertEqual(self.client.methods(),['session/read','session/resume'])
        self.assertNotIn(self.SID,self.bridge.resident)

    def test_send_and_control_rpcs_are_never_replayed(self):
        for method in ('session/send','session/setMode','session/stop'):
            error=runtime_not_ready()
            self.client.outcomes=[error]
            self.client.calls.clear()
            with self.assertRaises(ProtocolError) as caught:
                self.bridge.rpc(method,{'sessionId':self.SID,'content':'x'})
            self.assertIs(caught.exception,error)
            self.assertEqual(self.client.methods(),[method])

    def test_other_protocol_errors_are_not_treated_as_recovery(self):
        cases=[
            ProtocolError({'code':-32001,'message':'Session runtime is not ready',
                           'data':{'code':'runtimeNotReady'}}),
            ProtocolError({'code':-32004,'message':'permission denied',
                           'data':{'code':'permissionDenied'}}),
            ProtocolError({'code':-32004,'message':'Session not found: sess_other'}),
            ProtocolError({'code':-32004,'message':'Session not found'}),
        ]
        for error in cases:
            with self.subTest(error=str(error)):
                self.client.outcomes=[error]
                self.client.calls.clear()
                with self.assertRaises(ProtocolError) as caught:
                    self.bridge.rpc('session/read',{'sessionId':self.SID})
                self.assertIs(caught.exception,error)
                self.assertEqual(self.client.methods(),['session/read'])

    def test_unknown_session_is_not_hidden_resumed(self):
        error=runtime_not_ready()
        self.client.outcomes=[error]
        with self.assertRaises(ProtocolError) as caught:
            self.bridge.rpc('session/read',{'sessionId':'sess_unknown'})
        self.assertIs(caught.exception,error)
        self.assertEqual(self.client.methods(),['session/read'])

    def test_read_session_tool_recovers_before_returning_final_report(self):
        self.client.outcomes=[runtime_not_ready(),snapshot(),snapshot(),
                              {'messages':[message('m1','final')]}]
        result=self.bridge.call('zcode_read_session',
            {'session_id':self.SID,'caller_permission_mode':'full_access'})
        self.assertEqual(self.client.methods(),
            ['session/read','session/resume','session/read','session/messages'])
        self.assertEqual([m['id'] for m in result['messages']],['m1'])

    def test_resume_does_not_block_engine_interaction_callbacks(self):
        state={}
        def resume(method,params):
            def callback():
                self.bridge._request({'id':'wire-1','method':'interaction/requestUserInput',
                    'params':{'requestId':'req-1','sessionId':self.SID,'toolCallId':'call-1',
                              'toolName':'AskUserQuestion',
                              'input':{'questions':[{'question':'Test?'}]}}})
            worker=threading.Thread(target=callback)
            worker.start()
            worker.join(timeout=2)
            state['blocked']=worker.is_alive()
            return snapshot()
        self.client.outcomes=[runtime_not_ready(),resume,snapshot()]
        self.bridge.rpc('session/read',{'sessionId':self.SID})
        self.assertFalse(state.get('blocked'),'interaction callback blocked during resume')
        self.assertIn('req-1',self.bridge.pending)


class RoutedEngineClient:
    """Engine double for the whole notify-send path; Bridge.rpc stays real."""

    def __init__(self):
        self.calls=[]
        self.child=Mock()
        self.child.poll.return_value=None
        self.child.pid=4242
        self.status='idle'
        self.turn_count=0
        self.jobs=[]
        self.runs=[]
        self.messages=[]
        self.child_ids=[]
        self.subagents_error=None
        self.send_result={'accepted':True,'messageId':'m-sent'}

    def request(self,method,params=None,timeout=40):
        params=params or {}
        self.calls.append((method,copy.deepcopy(params)))
        if method in ('session/read','session/resume'):
            return {'session':{'sessionId':params.get('sessionId')},
                'projection':{'status':self.status,'turnCount':self.turn_count,
                              'backgroundJobs':copy.deepcopy(self.jobs)},
                'messages':copy.deepcopy(self.messages),
                'runtime':{},
                'settings':{'permission':{'mode':'yolo'},
                            'model':{'current':{'providerId':'p','modelId':'m'}},
                            'thoughtLevel':{'current':'high'}}}
        if method=='session/setMode':
            mode='build' if params.get('mode')=='plan' else params.get('mode')
            return {'settings':{'permission':{'mode':mode},
                                'model':{'current':{'providerId':'p','modelId':'m'}},
                                'thoughtLevel':{'current':'high'}}}
        if method=='session/messages':
            return {'messages':copy.deepcopy(self.messages)}
        if method=='v4/conversation/workflowRuns':
            return {'runs':copy.deepcopy(self.runs)}
        if method=='v4/conversation/workflowRunEvents':
            return {'events':[],'hasMore':False}
        if method=='session/subagents':
            if self.subagents_error is not None:
                raise self.subagents_error
            return {'childSessionIds':list(self.child_ids)}
        if method=='session/send':
            return copy.deepcopy(self.send_result)
        return {}

    def respond(self,*args,**kwargs):
        pass

    def methods(self):
        return [name for name,_ in self.calls]


class FreshSessionSendTests(unittest.TestCase):
    """session/subagents needs a persisted parent; only the first send may skip."""

    SID='sess_fresh'
    CTX={'thread_id':'chat-1','thread_origin':'turn_metadata'}

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'ZCODE_BRIDGE_STATE':self.tmp.name})
        self.env.start()
        executable=Path(self.tmp.name)/'ZCode.exe'
        executable.touch()
        self.cfg=patch('zcode_bridge.service.config', return_value={**storage.defaults(),
            'zcode_executable':str(executable),'auto_launch_desktop':False,
            'default_model':{'providerId':'p','modelId':'m'},'default_thought_level':'high',
            'default_workspace':self.tmp.name})
        self.cfg.start()
        self.bridge=Bridge()
        self.engine=RoutedEngineClient()
        self.bridge.client=self.engine
        self.bridge.generation=1
        self.bridge.capabilities_generation=1
        self.bridge.resident.add(self.SID)
        self.bridge._mark_created(self.SID)

    def tearDown(self):
        self.cfg.stop()
        self.env.stop()
        self.tmp.cleanup()

    def args(self,**overrides):
        row={'session_id':self.SID,'content':'do the task',
             'caller_permission_mode':'full_access',
             'notify':{'completion_notify':True,'auto_continue':False,
                       'user_authorized':True},
             'idempotency_key':'key-1'}
        row.update(overrides)
        return row

    def test_fresh_unstarted_first_notify_send_reaches_send_once(self):
        self.engine.subagents_error=session_not_found(self.SID)
        result=self.bridge.call('zcode_send',self.args(),self.CTX)
        self.assertTrue(result['accepted'])
        methods=self.engine.methods()
        self.assertEqual(methods.count('session/send'),1)
        self.assertNotIn('session/resume',methods)
        self.assertLess(methods.index('session/subagents'),methods.index('session/send'))
        sub=self.bridge.notifications.find_subscription(result['subscription_id'])
        self.assertEqual(sub['state'],'armed')
        self.assertEqual(sub['baseline']['child_ids'],[])
        self.assertEqual(self.bridge.notifications.all_events(),[])

    def test_existing_turn_keeps_not_found_strict_and_never_sends(self):
        self.engine.turn_count=1
        error=session_not_found(self.SID)
        self.engine.subagents_error=error
        with self.assertRaises(ProtocolError) as caught:
            self.bridge.call('zcode_send',self.args(),self.CTX)
        self.assertIs(caught.exception,error)
        self.assertNotIn('session/send',self.engine.methods())
        self.assertEqual(self.bridge.notifications.all_subscriptions(),[])

    def test_active_background_is_not_exempted_by_not_found(self):
        cases=(([{'jobId':'job-1','status':'running'}],[]),
               ([],[{'runId':'run-1','status':'running'}]))
        for jobs,runs in cases:
            with self.subTest(jobs=jobs,runs=runs):
                self.engine.calls.clear()
                self.engine.jobs=jobs
                self.engine.runs=runs
                self.engine.subagents_error=session_not_found(self.SID)
                with self.assertRaisesRegex(ValueError,'session_busy_untracked'):
                    self.bridge.call('zcode_send',self.args(idempotency_key='key-bg'),
                                     self.CTX)
                self.assertNotIn('session/send',self.engine.methods())
                self.assertEqual(self.bridge.notifications.all_subscriptions(),[])

    def test_resumed_zero_turn_session_is_not_exempted(self):
        self.bridge.created.discard(self.SID)
        error=session_not_found(self.SID)
        self.engine.subagents_error=error
        with self.assertRaises(ProtocolError) as caught:
            self.bridge.call('zcode_send',self.args(),self.CTX)
        self.assertIs(caught.exception,error)
        self.assertNotIn('session/send',self.engine.methods())

    def test_visible_user_input_blocks_the_gap_exemption(self):
        self.engine.messages=[message('m-user','already sent',role='user')]
        error=session_not_found(self.SID)
        self.engine.subagents_error=error
        with self.assertRaises(ProtocolError) as caught:
            self.bridge.call('zcode_send',self.args(),self.CTX)
        self.assertIs(caught.exception,error)
        self.assertNotIn('session/send',self.engine.methods())

    def test_fresh_session_non_target_errors_stay_strict(self):
        cases=(
            ProtocolError({'code':-32004,'message':'Session runtime is not ready',
                           'data':{'code':'runtimeNotReady'}}),
            ProtocolError({'code':-32001,'message':f'Session not found: {self.SID}'}),
            RuntimeError('subagents unavailable'),
        )
        for error in cases:
            with self.subTest(error=str(error)):
                self.engine.calls.clear()
                self.engine.subagents_error=error
                with self.assertRaises(type(error)) as caught:
                    self.bridge.call('zcode_send',self.args(idempotency_key='key-err'),
                                     self.CTX)
                self.assertIs(caught.exception,error)
                self.assertNotIn('session/send',self.engine.methods())

    def test_baseline_gap_requires_a_clean_unstarted_projection(self):
        self.engine.subagents_error=session_not_found(self.SID)
        clean={'session':{'sessionId':self.SID},
               'projection':{'status':'idle','turnCount':0,'backgroundJobs':[]},
               'messages':[]}
        baseline=self.bridge._baseline(self.SID,clean,run_rows=[],strict=True,
                                       allow_unstarted_children=True)
        self.assertEqual(baseline['child_ids'],[])
        blocked=[
            {'session':{'sessionId':self.SID},
             'projection':{'status':'idle','turnCount':0,'currentTurnId':'turn-1',
                           'backgroundJobs':[]}},
            {'session':{'sessionId':self.SID},
             'projection':{'status':'idle','turnCount':1,'backgroundJobs':[]}},
            {'session':{'sessionId':self.SID},
             'projection':{'status':'idle','turnCount':0,
                           'backgroundJobs':[{'jobId':'j','status':'running'}]}},
            {'session':{'sessionId':self.SID},
             'projection':{'status':'idle','turnCount':0,'backgroundJobs':[]},
             'messages':[message('m-user','sent',role='user')]},
        ]
        for snap in blocked:
            with self.subTest(snap=snap):
                with self.assertRaises(ProtocolError):
                    self.bridge._baseline(self.SID,snap,run_rows=[],strict=True,
                                          allow_unstarted_children=True)
        with self.assertRaises(ProtocolError):
            self.bridge._baseline(self.SID,clean,strict=True,
                                  run_rows=[{'run_id':'run-1','settled':False}],
                                  allow_unstarted_children=True)


if __name__=='__main__': unittest.main()
