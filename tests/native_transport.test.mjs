import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createZcodeDesktopNativeTransport, createZcodeNativeInteractionReader } from '../runtime/desktop_sync/native_transport.mjs';

const chosen = { providerId: 'example-provider', modelId: 'example-model' };
const another = { providerId: 'example-provider', modelId: 'plain-model' };
const copy = (value) => structuredClone(value);

test('interaction reads serialize session aliases and never return full conversation rows', async () => {
  let active=0,maxActive=0,release;
  const gate=new Promise(resolve=>release=resolve),calls=[];
  const reader=createZcodeNativeInteractionReader(async target=>{
    active++;maxActive=Math.max(maxActive,active);calls.push(target.workspacePath);
    if(calls.length===1)await gate;
    active--;return {logEpoch:'epoch',revision:1,config:{mode:'yolo'},pendingInteractions:[],rows:['PRIVATE_ROWS']};
  });
  const first=reader({workspacePath:'/first',sessionId:'same-session'});
  const second=reader({workspacePath:'/alias',workspaceIdentity:'alias',sessionId:'same-session'});
  await new Promise(resolve=>setTimeout(resolve,0));assert.equal(calls.length,1);
  release();const results=await Promise.all([first,second]);
  assert.equal(maxActive,1);assert.deepEqual(calls,['/first','/alias']);
  assert.equal(JSON.stringify(results).includes('PRIVATE_ROWS'),false);
});

// 3.18.2 Wc's strict object contract: providerId, modelId, optional strict
// options:{reasoningLevel?:nonempty string}. This fake-native boundary rejects
// unsupported execution fields and requires explicit effort for the test model.
function checkNativeWc(selection) {
  assert.equal(typeof selection.providerId, 'string'); assert(selection.providerId.trim());
  assert.equal(typeof selection.modelId, 'string'); assert(selection.modelId.trim());
  assert(Object.keys(selection).every((key) => ['providerId', 'modelId', 'options'].includes(key)));
  if (selection.options !== undefined) {
    assert(selection.options && typeof selection.options === 'object' && !Array.isArray(selection.options));
    assert(Object.keys(selection.options).every((key) => key === 'reasoningLevel'));
    if (selection.options.reasoningLevel !== undefined) {
      assert.equal(typeof selection.options.reasoningLevel, 'string'); assert(selection.options.reasoningLevel.trim());
    }
  }
}

function fixture(t, overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'native-host-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const workspace = path.join(dir, 'workspace'); fs.mkdirSync(workspace);
  const ownershipPath = path.join(dir, 'private', 'owned.json');
  const calls = [], sessions = new Map(), durableSessions = new Set(), notifications = [];
  let serial = 0, identity = 'test-engine-one', unloaded = false, relationshipParent, verifiedChildId = 'sess_child';
  const notReady = () => {
    const error = new Error('Session runtime is not ready');
    error.name = 'ZCodeProtocolClientError'; error.code = -32004;
    error.data = { code: 'runtimeNotReady', headers: { Authorization: 'private-fixture-token' }, stack: 'private stack' };
    return error;
  };
  const settings = (model, mode, thought) => ({
    model: { current: overrides.snapshotRefOnly ? { providerId: model.providerId, modelId: model.modelId } : { ...model }, available: [
      { ref: chosen, label: 'Example', ...(overrides.includeReasoning ? { reasoning: {
        levels: [{ value: 'medium', label: 'Medium' }, { value: 'high', label: 'High' }], defaultLevel: 'medium',
      } } : {}) },
      { ref: another, label: 'Plain', ...(overrides.includeReasoning ? { reasoning: { levels: [] } } : {}) },
    ] },
    thoughtLevel: { current: thought, available: model.modelId === 'plain-model' ? [] : [{ value: 'medium' }, { value: 'high' }] },
    permission: { mode }, mode: { current: mode },
  });
  const record = (name, input) => calls.push({ name, input: copy(input) });
  const get = (sid) => { assert(sessions.has(sid), 'fake native session must exist'); return sessions.get(sid); };
  const childSummary = (workspace) => ({ sessionId: verifiedChildId, parentSessionId: overrides.wrongChildParent ? 'sess_other_parent' : relationshipParent,
    sessionKind: overrides.childKind ?? (overrides.workflowActor ? 'workflow_child' : 'subagent_child'),
    workspace: { workspacePath: overrides.wrongChildWorkspace ? path.join(dir, 'other-workspace') : workspace } });
  const agent = {
    async createSession(input) {
      record('createSession', input);
      if (overrides.failActualCreate && input.persistence === 'immediate') throw new Error('lost acknowledgement');
      const sid = 'sess_test_' + (++serial);
      const model = input.model ?? overrides.defaultModel ?? chosen;
      const thought = model.modelId === 'plain-model' ? null : input.thoughtLevel ?? 'medium';
      const snapshot = { session: { sessionId: sid, status: 'idle', workspace: { workspacePath: input.workspacePath } },
        settings: settings(model, input.mode, thought),
        runtime: { stateRevision: 2, pendingRequestIds: [] },
        projection: { status: 'idle', turnCount: 0, pendingPermissions: [], backgroundJobs: [] }, messages: [] };
      sessions.set(sid, snapshot); return copy(snapshot);
    },
    async closeSession(input) { record('closeSession', input); assert.equal(input.expectedPersistence, 'deferred'); sessions.delete(input.sessionId); return true; },
    async getWorkspaceRuntimeIdentity(input) { record('getWorkspaceRuntimeIdentity', input); return { identity, generation: 1 }; },
    async readSession(input) {
      record('readSession', input); assert.equal(input.runtimePolicy, 'existing-only');
      if (unloaded) throw notReady();
      if (input.sessionId === verifiedChildId) {
        if (!overrides.childSnapshotReady) throw notReady();
        return { session: childSummary(input.workspacePath), messages: [], runtime: { stateRevision: 1 },
          projection: { status: 'idle', turnCount: 1 }, settings: settings(chosen, 'plan', 'high') };
      }
      const result = copy(get(input.sessionId)); return overrides.wrappedSnapshot ? { snapshot: result } : result;
    },
    async readSessionMessages(input) {
      record('readSessionMessages', input);
      const result = [{ info: { id: 'native-message' }, parts: [{ type: 'text', text: input.sessionId === verifiedChildId ? 'native child text' : 'native text' }] }];
      if (input.sessionId === verifiedChildId && overrides.restartDuringChildRead) identity = 'different-engine';
      return overrides.wrappedMessages ? { messages: result } : result;
    },
    async readSessionEvents(input) {
      record('readSessionEvents', input); if (unloaded) throw notReady();
      const result = [{ seq: 9, type: 'model.streaming', payload: { delta: 'native delta' } }];
      return overrides.wrappedEvents ? { events: result } : result;
    },
    async getSessionLifecycle(input) {
      record('getSessionLifecycle', input);
      return overrides.noLifecycle ? null : { sessionId: input.sessionId, state: unloaded ? 'unloaded' : 'ready', generation: 1, revision: 3, lastError: null };
    },
    async resumeSession(input) { record('resumeSession', input); throw new Error('read must not resume'); },
    async ensureSessionReady(input) { record('ensureSessionReady', input); throw new Error('read must not activate'); },
    async setModel(input) {
      record('setModel', input);
      if (overrides.requireExplicitReasoning && input.model.modelId === chosen.modelId && !input.model.options?.reasoningLevel) {
        const error = new Error('Reasoning level is required for example-model'); error.code = -32603; throw error;
      }
      const s = get(input.sessionId);
      const thought = input.model.modelId === 'plain-model' ? null :
        overrides.ignoreExplicitReasoning ? 'medium' : input.model.options?.reasoningLevel ?? 'medium';
      s.settings = settings(input.model, s.settings.permission.mode, thought); return copy(s);
    },
    async setMode(input) { record('setMode', input); const s = get(input.sessionId); s.settings.permission.mode = input.mode; s.settings.mode.current = input.mode; return copy(s); },
    async setThoughtLevel(input) { record('setThoughtLevel', input); const s = get(input.sessionId); s.settings.thoughtLevel.current = input.thoughtLevel; return copy(s); },
    async sendPrompt(input) {
      record('sendPrompt', input); checkNativeWc(input.modelSelection);
      assert.equal(input.modelSelection.options?.reasoningLevel, 'high');
      const current = get(input.sessionId);
      assert.equal(input.expectedRevision, current.runtime.stateRevision);
      if (overrides.rejectFirstInput) { const error = new Error('A prompt is already running for this session'); error.name = 'ZCodeProtocolClientError'; error.code = -32010; throw error; }
      if (overrides.localNumericFirstError) { const error = new Error('local transport failure'); error.code = -32000; throw error; }
      if (overrides.throwFirstInput) throw new Error('lost first-input acknowledgement');
      if (!overrides.delayFirstObservation) {
        durableSessions.add(input.sessionId);
        current.messages.push({ info: { id: input.inputId, role: 'user' }, parts: [{ type: 'text', text: input.content }] });
        current.projection.turnCount = 1; current.runtime.stateRevision += 1;
      }
      return { accepted: true, sessionId: input.sessionId, stateRevision: current.runtime.stateRevision };
    },
    async sendConversationCommandV4(input) {
      record('sendConversationCommandV4', input);
      if (input.envelope.type === 'sendText') {
        if (!durableSessions.has(input.envelope.sessionId)) throw new Error('FOREIGN KEY constraint failed');
        checkNativeWc(input.envelope.payload.modelSelection);
        if (overrides.requireSendReasoning) assert.equal(input.envelope.payload.modelSelection.options?.reasoningLevel, 'high');
      }
      if (overrides.throwSend) throw new Error('lost command acknowledgement');
      if (overrides.rejectSend) return { commandId: input.envelope.commandId, ...overrides.rejectSend };
      return { status: 'accepted', commandId: input.envelope.commandId };
    },
    // Mirrors the engine: a created-only session has no persisted CLI fact yet,
    // so reconcile reports exists:false until the first input was accepted.
    async reconcileSessionsIndexV4(input) {
      record('reconcileSessionsIndexV4', input);
      const durable = durableSessions.has(input.sessionIds[0]);
      return { sessions: [{ sessionId: input.sessionIds[0], exists: durable,
        ...(durable ? { title: 'Native index' } : {}) }] };
    },
    async listSessionSubagents(input) {
      record('listSessionSubagents', input); relationshipParent = input.sessionId;
      if (overrides.restartAfterChildRelations) identity = 'different-engine';
      return { childSessionIds: overrides.childOnlyInRows || overrides.workflowActor ? [] : ['sess_child'],
        running: overrides.childOutsideEndedPage || overrides.workflowActor ? [] : [{ childSessionId: 'sess_child' }],
        ended: overrides.malformedChildren ? [] : { total: overrides.childOutsideEndedPage ? 200 : 0, items: [] }, revision: 1 };
    },
    async listSessions(input) {
      record('listSessions', input); assert.equal(input.runtimePolicy, 'existing-only');
      verifiedChildId = input.sessionIds[0];
      return [childSummary(input.workspacePath)];
    },
    async conversationWorkflowRunsV4(input) { record('conversationWorkflowRunsV4', input); return { runs: [{ runId: 'native-run', status: 'running' }] }; },
    async conversationWorkflowRunEventsV4(input) {
      record('conversationWorkflowRunEventsV4', input);
      if (overrides.workflowActor) {
        if (overrides.endlessActorPages) return { events: [{ sequence: input.afterSequence + 1, type: 'log', payload: {} }], hasMore: true };
        const launch = { sequence: 1, type: 'run-launched', payload: { parentSessionId: overrides.wrongJournalParent ? 'sess_other_parent' : relationshipParent } };
        const actor = { sequence: 2, type: 'actor-created', payload: { name: 'sess_dwf-native-run-actor_1_1',
          actor: { siteId: overrides.forgedActor ? 'actor#99' : 'actor#1', ordinal: overrides.badActorOrdinal ? '1' : 1 } } };
        if (overrides.wrongEventRun) actor.runId = 'other-run';
        if (overrides.splitActorPages) return input.afterSequence === 0 ? { events: [launch], hasMore: true } : { events: [actor], hasMore: false };
        return { events: [launch, actor], hasMore: false };
      }
      return { events: [{ sequence: 3, type: 'phase-entered', payload: { name: 'third' } }], hasMore: false };
    },
  };
  // Mirrors Xi: syncSessionFacts applies the facts and emits its own list event
  // when the row changed; the transport must not emit a second one in that case.
  const syncer = {
    ensureWorkspaceSubscription(input) { record('ensureWorkspaceSubscription', input); },
    async syncSessionFacts(input) {
      notifications.push({ name: 'syncSessionFacts', input: copy(input) });
      if (overrides.syncThrows) throw Object.assign(new Error('task index unavailable'), { code: 'index_unavailable' });
      const durable = durableSessions.has(input.sessionId);
      if (overrides.syncMissing === true || !durable) {
        return { changed: false, missing: true, facts: [{ sessionId: input.sessionId, exists: false }] };
      }
      const changed = overrides.syncChanged ?? true;
      notifications.push({ name: 'factsReady', sessionId: input.sessionId });
      if (changed) notifications.push({ name: 'emitWorkspaceTaskListChanged',
        ref: { workspacePath: input.workspacePath, taskId: input.sessionId }, reason: 'task_created' });
      return { changed, facts: [{ sessionId: input.sessionId, exists: true, title: 'Native index' }] };
    },
    emitWorkspaceTaskListChanged(ref, meta, reason) {
      notifications.push({ name: 'emitWorkspaceTaskListChanged', ref: copy(ref), reason, meta: meta ?? null });
    },
  };
  const options = { agent, ownershipPath, resolveWorkspace: (value) => value === workspace ? workspace : null,
    makeEnvelope: (value) => ({ ...value, issuedAt: 100 }), syncer: () => syncer,
    ...(overrides.readInteractions ? { readInteractions: overrides.readInteractions } : {}),
    ...(overrides.resolveProjectId ? { resolveProjectId: overrides.resolveProjectId } : {}) };
  let transport = createZcodeDesktopNativeTransport(options);
  const request = (op, extra = {}) => ({ op, workspace, caller_permission_mode: 'full_access', ...extra });
  const call = (op, extra) => transport.call(request(op, extra));
  const create = (extra = {}) => call('native_create', { request_id: 'create-request-1', model: chosen, thought_level: 'high', ...extra });
  return { calls, sessions, durableSessions, notifications, workspace, ownershipPath, request, call, create,
    reload() { transport = createZcodeDesktopNativeTransport(options); },
    restartEngine() { identity = 'different-engine'; }, unloadSession() { unloaded = true; } };
}

test('complete native interactions retain the session option and revision input', async t => {
  const interaction = { interactionId: 'complete-1', kind: 'permission', payload: {
    kind: 'permission', name: 'CreateWorkflow', freeText: true,
    options: [{ optionId: 'allow_once', kind: 'allowOnce', label: 'Allow once' },
      { optionId: 'allowSession', kind: 'allow_session', label: 'Always allow in this session' }],
    fullAccessOption: { optionId: 'fullAccess', kind: 'custom', label: 'Full access' },
  } };
  const f = fixture(t, { readInteractions: async target => ({
    sessionId: target.sessionId, mode: 'yolo', interactions: [interaction],
  }) });
  const sid = (await f.create()).result.sessionId;
  const view = await f.call('native_interactions', { session_id: sid });
  assert.equal(view.ok, true); assert.equal(view.result.optionsComplete, true);
  assert.deepEqual(view.result.interactions[0].payload.options, interaction.payload.options);
  assert.deepEqual(view.result.interactions[0].payload.fullAccessOption, interaction.payload.fullAccessOption);
  assert.equal(view.result.interactions[0].payload.workflowRevisionOption.optionId, 'workflowRefine');
  const chosen = await f.call('native_resolve', { session_id: sid, request_id: 'full-choice',
    interaction_id: 'complete-1', answer: { optionId: 'allowSession' } });
  assert.equal(chosen.ok, true);
  const revision = await f.call('native_resolve', { session_id: sid, request_id: 'revision-choice',
    interaction_id: 'complete-1', answer: { action: 'accept', freeText: 'Use one worker' } });
  assert.equal(revision.ok, true);
  const actual = f.calls.filter(c=>c.name==='sendConversationCommandV4').at(-1).input.envelope.payload.answer;
  assert.equal(actual.optionId, 'workflowRefine');
  assert.equal(actual.freeText, 'Use one worker');
});

test('native complete question preserves generic options without treating them as permissions', async t => {
  const f = fixture(t, { readInteractions: async target => ({ sessionId: target.sessionId,
    mode: 'yolo', interactions: [{ interactionId: 'question-1', kind: 'userInput',
      payload: { kind: 'userInput', freeText: false, options: [{ optionId: 'real-choice', label: 'Continue' }] } }],
  }) });
  const sid = (await f.create()).result.sessionId;
  assert.equal((await f.call('native_resolve', { session_id: sid, request_id: 'real-answer',
    interaction_id: 'question-1', answer: { action: 'accept', optionId: 'real-choice' } })).ok, true);
  assert.equal((await f.call('native_resolve', { session_id: sid, request_id: 'invented-answer',
    interaction_id: 'question-1', answer: { optionId: 'invented' } })).error, 'option_not_in_request');
});

test('native workflowRefine option cannot hide required feedback metadata', async t => {
  const f=fixture(t,{readInteractions:async target=>({sessionId:target.sessionId,mode:'yolo',interactions:[{
    interactionId:'refine-real',kind:'permission',payload:{kind:'permission',toolName:'CreateWorkflow',freeText:true,
      options:[{optionId:'workflowRefine',kind:'custom',label:'Refine',response:{decision:'deny'}}]}}]})});
  const sid=(await f.create()).result.sessionId;
  const args={session_id:sid,interaction_id:'refine-real',answer:{optionId:'workflowRefine'}};
  assert.equal((await f.call('native_resolve',{...args,request_id:'empty-refine'})).error,'invalid_answer');
  assert.equal(f.calls.some(c=>c.name==='sendConversationCommandV4'),false);
  const answer={optionId:'workflowRefine',freeText:'Keep the single stage'};
  assert.equal((await f.call('native_resolve',{...args,request_id:'valid-refine',answer,
    caller_permission_mode:'read_only'})).ok,true);
});

test('plugin installation exposes actual UI choices and keeps full caller approval boundaries', async t => {
  const f = fixture(t, {readInteractions: async target => ({sessionId:target.sessionId, mode:'yolo',
    interactions:[{interactionId:'plugin-1',kind:'pluginInstall',payload:{kind:'pluginInstall',action:'install'}}]})});
  const sid=(await f.create()).result.sessionId;
  const view=await f.call('native_interactions',{session_id:sid});
  assert.deepEqual(view.result.interactions[0].payload.options.map(o=>o.optionId),['skip','install']);
  assert.equal((await f.call('native_resolve',{session_id:sid,request_id:'plugin-weak',
    caller_permission_mode:'always_ask',interaction_id:'plugin-1',answer:{optionId:'install'}})).error,'user_approved_required');
  assert.equal((await f.call('native_resolve',{session_id:sid,request_id:'plugin-skip',
    caller_permission_mode:'read_only',interaction_id:'plugin-1',answer:{action:'cancel'}})).ok,true);
  assert.deepEqual(f.calls.filter(c=>c.name==='sendConversationCommandV4').at(-1).input.envelope.payload.answer,{optionId:'skip'});
});

test('Hook review uses immutable native context, validates selections and permits read-only revocation', async t => {
  const interaction={interactionId:'hook-1',kind:'workspaceHookReview',payload:{kind:'workspaceHookReview',
    interactionId:'hook-1',sessionId:'pending',taskId:'task',runId:'run',workspaceIdentity:'workspace',
    bundleDigest:'a'.repeat(64),reviewFlowId:'flow',generation:1,
    items:[{reviewItemId:'item-1',editable:true,configuredEnabled:true}]}};
  const f=fixture(t,{readInteractions:async target=>({sessionId:target.sessionId,mode:'yolo',interactions:[interaction]})});
  const sid=(await f.create()).result.sessionId;interaction.payload.sessionId=sid;
  const resolve=(request_id,content,extra={})=>f.call('native_resolve',{session_id:sid,request_id:'hook-'+request_id,
    interaction_id:'hook-1',answer:{action:'accept',content},...extra});
  assert.equal((await resolve('wrong',{action:'trust_selected',reviewItemIds:['invented']})).error,'invalid_hook_response');
  assert.equal((await resolve('readonly-trust',{action:'trust_selected',reviewItemIds:['item-1']},
    {caller_permission_mode:'read_only'})).error,'read_only_resolution_not_allowed');
  assert.equal((await resolve('trust',{action:'trust_selected',reviewItemIds:['item-1']})).ok,true);
  let envelope=f.calls.filter(c=>c.name==='sendConversationCommandV4').at(-1).input.envelope;
  assert.equal(envelope.type,'respondWorkspaceHookReview');assert.equal(envelope.payload.generation,1);
  assert.deepEqual(envelope.payload.decision,{action:'trust_selected',reviewItemIds:['item-1']});
  assert.equal((await resolve('revoke',{action:'revoke',reviewItemIds:['item-1']},{caller_permission_mode:'read_only'})).ok,true);
  envelope=f.calls.filter(c=>c.name==='sendConversationCommandV4').at(-1).input.envelope;
  assert.equal(envelope.type,'revokeWorkspaceHookTrust');
});

test('the real session permission is resolved by the SDK even while its prompt is running', async t => {
  const f = fixture(t, { readInteractions: async target => ({ sessionId: target.sessionId,
    mode: 'edit', interactions: [{ interactionId: 'workflow-consent', kind: 'permission', payload: {
      kind: 'permission', name: 'CreateWorkflow', options: [
        { optionId: 'allowSession', kind: 'allow_session', label: 'Always allow in this session' }],
    } }],
  }) });
  const sid = (await f.create()).result.sessionId;
  const current = f.sessions.get(sid);
  current.session.status = 'running'; current.settings.permission.mode = 'edit'; current.settings.mode.current = 'edit';
  const before = f.calls.length;
  const result = await f.call('native_resolve', { session_id: sid, request_id: 'session-choice-running',
    interaction_id: 'workflow-consent', answer: { optionId: 'allowSession' } });
  assert.equal(result.ok, true);
  assert.equal(f.calls.slice(before).filter(row => row.name === 'setMode').length, 0);
});

test('real module wires plan catalogue, closes probe, then creates requested native task', async (t) => {
  const f = fixture(t), response = await f.create({ project_id: 'requested-project' });
  assert.equal(response.ok, true); assert.equal(response.source, 'desktop-native-host');
  const result = response.result; assert.equal(result.guiVerified, false);
  assert.equal(result.sessionId, 'sess_test_2'); assert.equal(result.mode, 'yolo');
  assert.deepEqual(result.model, { ...chosen, options: { reasoningLevel: 'high' } });
  assert.equal(result.thoughtLevel, 'high');
  assert.equal(result.catalogueProbe.closed, true);
  const creates = f.calls.filter((c) => c.name === 'createSession');
  assert.equal(creates.length, 2); assert.equal(creates[0].input.mode, 'plan');
  assert.equal(creates[0].input.persistence, 'deferred'); assert(!('firstInput' in creates[0].input));
  assert.equal(creates[1].input.projectId, 'requested-project');
  assert.equal(f.calls.filter((c) => c.name === 'closeSession').length, 1);
  assert(!f.sessions.has('sess_test_1')); assert(!f.calls.some((c) => c.name === 'sendConversationCommandV4'));
  // Create alone has no persisted CLI fact: the index reports exists:false.
  assert.equal(result.desktopIndexFacts.sessions.length, 1);
  assert.equal(result.desktopIndexFacts.sessions[0].sessionId, result.sessionId);
  assert.equal(result.desktopIndexFacts.sessions[0].exists, false);
});

test('model and model-specific effort fail closed; failed probes never become sendable', async (t) => {
  const f = fixture(t);
  const unavailable = await f.create({ model: { providerId: 'missing-provider', modelId: 'missing-model' } });
  assert.equal(unavailable.error, 'model_not_available');
  assert.equal(unavailable.request.status, 'failed');
  assert.equal(f.calls.filter((c) => c.name === 'createSession').length, 1);
  assert.equal((await f.call('native_send', { session_id: 'sess_test_1', request_id: 'send-request-1', text: 'x' })).error, 'session_not_owned');
  const effort = await f.create({ request_id: 'create-request-2', thought_level: 'made-up-level' });
  assert.equal(effort.error, 'thought_level_not_available');
  assert.equal(f.calls.filter((c) => c.name === 'createSession').length, 2);
  assert.equal(f.calls.filter((c) => c.name === 'closeSession').length, 2);
});

test('no-effort model requires explicit null and remains unmodified', async (t) => {
  const f = fixture(t, { includeReasoning: true }), response = await f.create({ model: another, thought_level: null });
  assert.equal(response.ok, true); assert.equal(response.result.thoughtLevel, null);
  assert.deepEqual(response.result.model, another);
});

test('native model row reasoning supports catalog reads without a ref-only switch', async (t) => {
  const f = fixture(t, { includeReasoning: true, defaultModel: another, requireExplicitReasoning: true });
  const response = await f.call('native_model_catalog', { request_id: 'catalog-row-request', model: chosen });
  assert.equal(response.ok, true);
  assert.deepEqual(response.result.thoughtLevels, ['medium', 'high']);
  assert.equal(response.result.thoughtLevelsSource, 'native-model-row');
  assert.equal(response.result.selectionStatus, 'requires-selection');
  assert.equal(response.result.available[0].reasoning.defaultLevel, 'medium');
  assert.deepEqual(response.result.available[0].reasoning.levels[1], { value: 'high', label: 'High' });
  assert.deepEqual(response.result.requestedModel, chosen);
  assert.deepEqual(response.result.selected, another, 'do not claim the requested catalogue model is selected on the probe');
  assert.equal(response.result.catalogueProbe.closed, true);
  assert(!f.calls.some((c) => c.name === 'setModel'));
  const created = await f.create();
  assert.equal(created.ok, true); assert.equal(created.result.thoughtLevel, 'high');
  assert(!f.calls.some((c) => c.name === 'setModel'));
});

test('missing row metadata requires selection; fallback passes only the explicit reasoning level', async (t) => {
  const f = fixture(t, { defaultModel: another, requireExplicitReasoning: true });
  const missing = await f.call('native_model_catalog', { request_id: 'catalog-missing-request', model: chosen });
  assert.equal(missing.ok, true); assert.equal(missing.result.selectionStatus, 'requires-selection');
  assert.equal(missing.result.thoughtLevels, null, 'unknown levels must not be represented as a no-reasoning model');
  assert(!f.calls.some((c) => c.name === 'setModel'));
  const noLevel = await f.call('native_model_catalog', { request_id: 'catalog-null-request', model: chosen, thought_level: null });
  assert.equal(noLevel.result.selectionStatus, 'requires-selection');
  assert(!f.calls.some((c) => c.name === 'setModel'));
  const explicit = await f.call('native_model_catalog', { request_id: 'catalog-explicit-request', model: chosen, thought_level: 'high' });
  assert.equal(explicit.ok, true); assert.equal(explicit.result.selectionStatus, 'validated');
  assert.equal(explicit.result.thoughtLevelsSource, 'native-selected-snapshot');
  assert.equal(explicit.result.selected.options.reasoningLevel, 'high');
  assert.equal(explicit.result.catalogueProbe.closed, true);
  const created = await f.create(); assert.equal(created.ok, true);
  const switches = f.calls.filter((c) => c.name === 'setModel');
  assert.equal(switches.length, 2);
  for (const entry of switches) assert.equal(entry.input.model.options.reasoningLevel, 'high');
});

test('fallback verifies the actual native effort and still closes an unsuccessful probe', async (t) => {
  const f = fixture(t, { defaultModel: another, requireExplicitReasoning: true, ignoreExplicitReasoning: true });
  const result = await f.create();
  assert.equal(result.error, 'native_thought_mismatch'); assert.equal(result.request.status, 'failed');
  assert.equal(result.request.catalogClose, 'confirmed');
  assert.equal(f.calls.filter((c) => c.name === 'createSession').length, 1);
  assert(!f.calls.some((c) => c.name === 'sendConversationCommandV4'));
});

test('missing permission or workspace rejection makes no native calls', async (t) => {
  const f = fixture(t);
  assert.equal((await f.create({ caller_permission_mode: undefined })).error, 'caller_permission_required');
  assert.equal((await f.create({ workspace: 'outside-allowlist' })).error, 'workspace_not_allowed');
  assert.equal(f.calls.length, 0);
});

test('reads and writes never materialize or touch an independently owned SID', async (t) => {
  const f = fixture(t);
  for (const op of ['native_send', 'native_snapshot', 'native_messages', 'native_events', 'native_workflows', 'native_resolve', 'native_cancel']) {
    const response = await f.call(op, { session_id: 'sess_existing_external', request_id: 'foreign-' + op, text: 'x' });
    assert.equal(response.error, 'session_not_owned', op);
  }
  assert.equal(f.calls.length, 0);
});

test('send checks actual model and applies current permission downgrade before native command', async (t) => {
  const f = fixture(t), created = await f.create(), sid = created.result.sessionId;
  const response = await f.call('native_send', { session_id: sid, request_id: 'send-request-1',
    text: 'a private prompt not for disk', caller_permission_mode: 'read_only' });
  assert.equal(response.ok, true); assert.equal(response.result.mode, 'plan');
  const send = f.calls.filter((c) => c.name === 'sendPrompt').at(-1);
  assert.equal(response.result.admissionProtocol, 'session/send');
  assert.equal(f.sessions.get(sid).settings.permission.mode, 'plan');
  assert.equal(send.input.content, 'a private prompt not for disk');
  assert(f.calls.findIndex((c) => c.name === 'setMode') < f.calls.indexOf(send));
  assert(!fs.readFileSync(f.ownershipPath, 'utf8').includes('a private prompt not for disk'));
  f.sessions.get(sid).settings.model.current = another;
  assert.equal((await f.call('native_send', { session_id: sid, request_id: 'send-request-2', text: 'x' })).error, 'native_model_mismatch');
  assert.equal(f.calls.filter((c) => c.name === 'sendPrompt').length, 1);
  assert.equal(f.calls.filter((c) => c.name === 'sendConversationCommandV4').length, 0);
});

test('running permission mismatch is isolated instead of silently sending under old access', async (t) => {
  const f = fixture(t), created = await f.create(), sid = created.result.sessionId;
  f.sessions.get(sid).session.status = 'running';
  const response = await f.call('native_send', { session_id: sid, request_id: 'send-request-1', text: 'x', caller_permission_mode: 'always_ask' });
  assert.equal(response.error, 'permission_change_while_running');
  assert(!f.calls.some((c) => c.name === 'setMode' || c.name === 'sendPrompt' || c.name === 'sendConversationCommandV4'));
});

test('send honors Wc effort options even when native current ref stores effort separately', async (t) => {
  const f = fixture(t, { snapshotRefOnly: true, includeReasoning: true, requireSendReasoning: true });
  const sid = (await f.create()).result.sessionId;
  const response = await f.call('native_send', { session_id: sid, request_id: 'send-schema-options', text: 'schema check' });
  assert.equal(response.ok, true);
  const first = f.calls.at(-1).input;
  assert.deepEqual(first.modelSelection, { ...chosen, options: { reasoningLevel: 'high' } });
  assert.equal(first.modelExecution, undefined);
  assert.equal(first.inputId, first.queryId);
  const later = await f.call('native_send', { session_id: sid, request_id: 'send-later-options', text: 'follow-up' });
  assert.equal(later.ok, true);
  const payload = f.calls.at(-1).input.envelope.payload;
  assert.deepEqual(payload.modelSelection, { ...chosen, options: { reasoningLevel: 'high' } });
  assert.equal(payload.modelExecution, undefined, 'ordinary effort is not execution/auth material');
});

test('known native rejection retains bounded safe ACK diagnostics without persisting its message', async (t) => {
  const f = fixture(t, { includeReasoning: true, rejectSend: { status: 'failed', reasonCode: 'fault.command.executionFailed',
    message: 'Reasoning level is required; Bearer example-token; api_key=example-secret; private prompt',
    result: { headers: { Authorization: 'example-secret' } }, stack: 'private stack' } });
  const sid = (await f.create()).result.sessionId;
  assert.equal((await f.call('native_send', { session_id: sid, request_id: 'first-input-before-rejection', text: 'bootstrap' })).ok, true);
  const response = await f.call('native_send', { session_id: sid, request_id: 'send-native-rejection', text: 'private prompt' });
  assert.equal(response.error, 'native_command_rejected');
  assert.equal(response.request.phase, 'command_acknowledged'); assert.equal(response.request.status, 'failed');
  assert.equal(response.nativeAck.reasonCode, 'fault.command.executionFailed');
  assert.match(response.nativeAck.message, /^Reasoning level is required/);
  assert(!JSON.stringify(response.nativeAck).includes('example-secret'));
  assert(!JSON.stringify(response.nativeAck).includes('example-token'));
  assert(!JSON.stringify(response.nativeAck).includes('private prompt'));
  assert.equal(response.nativeAck.result, undefined); assert.equal(response.nativeAck.stack, undefined);
  assert(!fs.readFileSync(f.ownershipPath, 'utf8').includes('Reasoning level is required'));
  const calls = f.calls.filter((item) => item.name === 'sendConversationCommandV4').length;
  const replay = await f.call('native_send', { session_id: sid, request_id: 'send-native-rejection', text: 'private prompt' });
  assert.equal(replay.result.request.status, 'failed'); assert.equal(replay.result.automaticRetry, false);
  assert.equal(f.calls.filter((item) => item.name === 'sendConversationCommandV4').length, calls);
});

test('transport loss after send stays unknown rather than an acknowledged native failure', async (t) => {
  const f = fixture(t, { throwSend: true }), sid = (await f.create()).result.sessionId;
  assert.equal((await f.call('native_send', { session_id: sid, request_id: 'first-input-before-loss', text: 'bootstrap' })).ok, true);
  const response = await f.call('native_send', { session_id: sid, request_id: 'send-lost-acknowledgement', text: 'x' });
  assert.equal(response.request.status, 'unknown'); assert.equal(response.request.phase, 'command_dispatched');
  assert.equal(response.nativeAck, undefined);
});

test('empty immediate session uses native first-input path before any v4 input-ledger admission', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  assert(!f.durableSessions.has(sid));
  const first = await f.call('native_send', { session_id: sid, request_id: 'first-input-empty-immediate', text: 'first native input' });
  assert.equal(first.ok, true); assert.equal(first.result.admissionProtocol, 'session/send');
  assert.equal(first.result.nativeAdmission.accepted, true);
  assert(f.durableSessions.has(sid));
  assert.equal(f.calls.filter((item) => item.name === 'sendConversationCommandV4').length, 0);
  const later = await f.call('native_send', { session_id: sid, request_id: 'later-input-after-durable', text: 'second native input' });
  assert.equal(later.ok, true);
  assert.equal(f.calls.filter((item) => item.name === 'sendPrompt').length, 1);
  assert.equal(f.calls.filter((item) => item.name === 'sendConversationCommandV4').length, 1);
});

test('first-input transport uncertainty is persistent and cannot be retried under a new ID', async (t) => {
  const f = fixture(t, { throwFirstInput: true }), sid = (await f.create()).result.sessionId;
  const failed = await f.call('native_send', { session_id: sid, request_id: 'first-input-unknown', text: 'x' });
  assert.equal(failed.request.status, 'unknown'); assert.equal(failed.request.phase, 'legacy_first_input_dispatched');
  f.reload();
  const replay = await f.call('native_send', { session_id: sid, request_id: 'first-input-unknown', text: 'x' });
  assert.equal(replay.result.request.status, 'unknown');
  const another = await f.call('native_send', { session_id: sid, request_id: 'first-input-new-request', text: 'x' });
  assert.equal(another.error, 'first_input_outcome_unknown');
  assert.equal(f.calls.filter((item) => item.name === 'sendPrompt').length, 1);
  assert(!f.calls.some((item) => item.name === 'sendConversationCommandV4'));
});

test('known initial JSON-RPC rejection remains failed; acceptance alone does not prove durable input', async (t) => {
  const rejected = fixture(t, { rejectFirstInput: true }), sid = (await rejected.create()).result.sessionId;
  const response = await rejected.call('native_send', { session_id: sid, request_id: 'first-input-known-rejected', text: 'x' });
  assert.equal(response.request.status, 'failed'); assert.equal(response.request.phase, 'legacy_first_input_rejected');
  const waiting = fixture(t, { delayFirstObservation: true }), waitingSid = (await waiting.create()).result.sessionId;
  const accepted = await waiting.call('native_send', { session_id: waitingSid, request_id: 'first-input-only-ack', text: 'x' });
  assert.equal(accepted.ok, true);
  const second = await waiting.call('native_send', { session_id: waitingSid, request_id: 'first-input-unobserved', text: 'follow-up' });
  assert.equal(second.error, 'first_input_not_yet_observed');
  assert.equal(waiting.calls.filter((item) => item.name === 'sendPrompt').length, 1);
  assert(!waiting.calls.some((item) => item.name === 'sendConversationCommandV4'));
});

test('a local numeric error is not confused with the native JSON-RPC rejection class', async (t) => {
  const f = fixture(t, { localNumericFirstError: true }), sid = (await f.create()).result.sessionId;
  const result = await f.call('native_send', { session_id: sid, request_id: 'first-input-local-numeric', text: 'x' });
  assert.equal(result.request.status, 'unknown');
  assert.equal((await f.call('native_send', { session_id: sid, request_id: 'first-input-numeric-retry', text: 'x' })).error, 'first_input_outcome_unknown');
  assert.equal(f.calls.filter((item) => item.name === 'sendPrompt').length, 1);
});

test('bootstrap does not infer eligibility for older records or nonidle sessions', async (t) => {
  const old = fixture(t), sid = (await old.create()).result.sessionId;
  const state = JSON.parse(fs.readFileSync(old.ownershipPath, 'utf8'));
  delete state.sessions[sid].firstInput; delete state.sessions[sid].creationMethod;
  fs.writeFileSync(old.ownershipPath, JSON.stringify(state)); old.reload();
  assert.equal((await old.call('native_send', { session_id: sid, request_id: 'older-first-input-record', text: 'x' })).error, 'first_input_state_unverified');
  assert(!old.calls.some((item) => item.name === 'sendPrompt'));
  const busy = fixture(t), busySid = (await busy.create()).result.sessionId;
  busy.sessions.get(busySid).session.status = 'running';
  busy.sessions.get(busySid).projection.status = 'running';
  assert.equal((await busy.call('native_send', { session_id: busySid, request_id: 'busy-first-input-record', text: 'x' })).error, 'first_input_requires_empty_idle_session');
  assert(!busy.calls.some((item) => item.name === 'sendPrompt'));
});

test('native messages/events/journal are returned without synthesizing content or status', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  const messages = await f.call('native_messages', { session_id: sid, limit: 5 });
  assert.equal(messages.result[0].parts[0].text, 'native text');
  const events = await f.call('native_events', { session_id: sid, after_seq: 8 });
  assert.equal(events.result[0].payload.delta, 'native delta');
  assert.equal(f.calls.at(-1).input.afterSeq, 8);
  const runs = await f.call('native_workflows', { session_id: sid });
  assert.equal(runs.result.runs[0].status, 'running');
  assert.equal(f.calls.at(-1).input.limit, 16);
  const topology = await f.call('native_topology', { session_id: sid, run_id: 'native-run' });
  assert.equal(topology.result.verifiedParentSessionId, sid);
  assert.equal(topology.result.events.events[0].payload.name, 'third');
  const before = f.calls.filter((c) => c.name === 'conversationWorkflowRunEventsV4').length;
  assert.equal((await f.call('native_workflow_events', { session_id: sid, run_id: 'foreign-run' })).error, 'run_not_verified_for_session');
  assert.equal(f.calls.filter((c) => c.name === 'conversationWorkflowRunEventsV4').length, before);
});

test('unknown create acknowledgement stays unknown across retry and reload, with no guessed SID', async (t) => {
  const f = fixture(t, { failActualCreate: true });
  const original = await f.create();
  assert.equal(original.ok, false); assert.equal(original.request.status, 'unknown');
  assert.equal(original.request.sessionId, undefined);
  const count = f.calls.filter((c) => c.name === 'createSession').length;
  f.reload();
  const replay = await f.create();
  assert.equal(replay.result.replay, true); assert.equal(replay.result.request.status, 'unknown');
  assert.equal(replay.result.automaticRetry, false);
  assert.equal(f.calls.filter((c) => c.name === 'createSession').length, count);
  const pending = await f.call('native_pending', { request_id: 'create-request-1' });
  assert.equal(pending.result.request.status, 'unknown');
});

test('ownership survives host module reload but rejects native runtime replacement without reads', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  f.reload(); assert.equal((await f.call('native_snapshot', { session_id: sid })).ok, true);
  const reads = f.calls.filter((c) => c.name === 'readSession').length;
  f.restartEngine();
  assert.equal((await f.call('native_snapshot', { session_id: sid })).error, 'stale_owner');
  assert.equal(f.calls.filter((c) => c.name === 'readSession').length, reads);
});

test('snapshot is an object while messages and events stay direct native arrays', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  const snapshot = await f.call('native_snapshot', { session_id: sid });
  assert.equal(snapshot.result.session.sessionId, sid); assert.equal(snapshot.result.snapshot, undefined);
  const messages = await f.call('native_messages', { session_id: sid });
  assert(Array.isArray(messages.result)); assert.equal(messages.result.messages, undefined);
  const events = await f.call('native_events', { session_id: sid }); assert(Array.isArray(events.result));
});

test('native runtime-not-ready retains safe error data; persisted messages need no restore', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  assert.equal((await f.call('native_send', { session_id: sid, request_id: 'send-before-unload', text: 'first' })).ok, true);
  f.unloadSession();
  const snapshot = await f.call('native_snapshot', { session_id: sid });
  assert.equal(snapshot.ok, false); assert.equal(snapshot.error, -32004);
  assert.equal(snapshot.nativeError.code, -32004);
  assert.deepEqual(snapshot.nativeError.data, { code: 'runtimeNotReady' });
  assert(!JSON.stringify(snapshot).includes('private-fixture-token'));
  const lifecycle = await f.call('native_lifecycle', { session_id: sid });
  assert.deepEqual(lifecycle.result, { sessionId: sid, state: 'unloaded', generation: 1, revision: 3, lastError: null });
  const messages = await f.call('native_messages', { session_id: sid });
  assert.equal(messages.ok, true); assert(Array.isArray(messages.result));
  const events = await f.call('native_events', { session_id: sid });
  assert.equal(events.nativeError.data.code, 'runtimeNotReady');
  assert(!f.calls.some((call) => ['resumeSession', 'ensureSessionReady'].includes(call.name)));
});

test('native shape mismatches fail explicitly rather than being silently rewrapped', async (t) => {
  for (const [override, op, expected] of [
    ['wrappedSnapshot', 'native_snapshot', 'native_snapshot_shape_invalid'],
    ['wrappedMessages', 'native_messages', 'native_messages_shape_invalid'],
    ['wrappedEvents', 'native_events', 'native_events_shape_invalid'],
  ]) {
    const f = fixture(t, { [override]: true }), sid = (await f.create()).result.sessionId;
    assert.equal((await f.call(op, { session_id: sid })).error, expected);
    assert(!f.calls.some((call) => ['resumeSession', 'ensureSessionReady'].includes(call.name)));
  }
});

test('lifecycle remains ownership guarded and unavailable native support stays unsupported', async (t) => {
  const f = fixture(t);
  assert.equal((await f.call('native_lifecycle', { session_id: 'sess_foreign' })).error, 'session_not_owned');
  assert.equal(f.calls.length, 0);
  const older = fixture(t, { noLifecycle: true }), sid = (await older.create()).result.sessionId;
  assert.equal((await older.call('native_lifecycle', { session_id: sid })).error, 'unsupported');
});

test('verified child messages use native child IDs and scope without granting ownership', async (t) => {
  const f = fixture(t), parent = (await f.create()).result.sessionId;
  const result = await f.call('native_child_messages', { session_id: parent, child_session_id: 'sess_child', after_message_id: 'cursor', limit: 8 });
  assert.equal(result.ok, true); assert(Array.isArray(result.result));
  assert.equal(result.result[0].parts[0].text, 'native child text');
  assert.deepEqual(f.calls.findLast((call) => call.name === 'readSessionMessages').input,
    { workspacePath: f.workspace, sessionId: 'sess_child', runtimePolicy: 'existing-only', afterMessageId: 'cursor', limit: 8, direction: 'forward' });
  assert.deepEqual(f.calls.findLast((call) => call.name === 'listSessions').input.sessionIds, ['sess_child']);
  assert(!Object.hasOwn(JSON.parse(fs.readFileSync(f.ownershipPath, 'utf8')).sessions, 'sess_child'));
  assert.deepEqual((await f.call('native_children', { session_id: parent })).result.native.ended, { total: 0, items: [] });
});

test('child snapshot needs a ready native snapshot and never resumes an unavailable child', async (t) => {
  const ready = fixture(t, { childSnapshotReady: true }), parent = (await ready.create()).result.sessionId;
  const snapshot = await ready.call('native_child_snapshot', { session_id: parent, child_session_id: 'sess_child' });
  assert.equal(snapshot.result.session.sessionId, 'sess_child'); assert.equal(snapshot.result.session.parentSessionId, parent);
  const inactive = fixture(t), other = (await inactive.create()).result.sessionId;
  const missing = await inactive.call('native_child_snapshot', { session_id: other, child_session_id: 'sess_child' });
  assert.equal(missing.error, -32004); assert.equal(missing.nativeError.data.code, 'runtimeNotReady');
  assert(!inactive.calls.some((call) => ['resumeSession', 'ensureSessionReady'].includes(call.name)));
});

test('foreign parent fails before all native calls and unlisted child before child-session calls', async (t) => {
  const foreign = fixture(t);
  assert.equal((await foreign.call('native_child_messages', { session_id: 'sess_foreign', child_session_id: 'sess_child' })).error, 'session_not_owned');
  assert.equal(foreign.calls.length, 0);
  const f = fixture(t), parent = (await f.create()).result.sessionId;
  const rejected = await f.call('native_child_messages', { session_id: parent, child_session_id: 'sess_unlisted_actor' });
  assert.equal(rejected.error, 'unsupported'); assert.match(rejected.detail, /No verified native ordinary-subagent relationship/);
  assert(!f.calls.some((call) => ['listSessions', 'readSessionMessages', 'readSession'].includes(call.name)));
  for (const op of ['native_send', 'native_resolve', 'native_cancel']) {
    assert.equal((await f.call(op, { session_id: 'sess_child', request_id: 'child-write-' + op, text: 'x',
      interaction_id: 'fixture-interaction', answer: { optionId: 'approve' } })).error, 'session_not_owned');
  }
  assert(!f.calls.some((call) => ['sendPrompt', 'sendConversationCommandV4', 'setMode', 'setThoughtLevel'].includes(call.name)));
});

test('child identity needs the matching parent, local workspace and ordinary native child kind', async (t) => {
  for (const [override, value, expected] of [
    ['wrongChildParent', true, 'child_not_verified_for_parent'],
    ['wrongChildWorkspace', true, 'child_workspace_mismatch'],
    ['childKind', 'workflow_child', 'unsupported'],
  ]) {
    const f = fixture(t, { [override]: value }), parent = (await f.create()).result.sessionId;
    assert.equal((await f.call('native_child_messages', { session_id: parent, child_session_id: 'sess_child' })).error, expected);
    assert(!f.calls.some((call) => ['readSessionMessages', 'readSession'].includes(call.name)));
  }
});

test('the actual childSessionIds array remains authoritative across ended-page pagination', async (t) => {
  const paginated = fixture(t, { childOutsideEndedPage: true }), parent = (await paginated.create()).result.sessionId;
  assert.equal((await paginated.call('native_child_messages', { session_id: parent, child_session_id: 'sess_child' })).ok, true);
  const rowsOnly = fixture(t, { childOnlyInRows: true }), other = (await rowsOnly.create()).result.sessionId;
  assert.equal((await rowsOnly.call('native_child_messages', { session_id: other, child_session_id: 'sess_child' })).error, 'unsupported');
  assert(!rowsOnly.calls.some((call) => call.name === 'listSessions'));
  const malformed = fixture(t, { malformedChildren: true }), bad = (await malformed.create()).result.sessionId;
  assert.equal((await malformed.call('native_child_messages', { session_id: bad, child_session_id: 'sess_child' })).error, 'native_children_shape_invalid');
  assert(!malformed.calls.some((call) => call.name === 'listSessions'));
});

test('engine replacement during verification or read returns no child content', async (t) => {
  for (const override of ['restartAfterChildRelations', 'restartDuringChildRead']) {
    const f = fixture(t, { [override]: true }), parent = (await f.create()).result.sessionId;
    const result = await f.call('native_child_messages', { session_id: parent, child_session_id: 'sess_child' });
    assert.equal(result.error, 'stale_owner'); assert.equal(result.result, undefined);
    if (override === 'restartAfterChildRelations') assert(!f.calls.some((call) => call.name === 'listSessions'));
    assert(!f.calls.some((call) => ['resumeSession', 'ensureSessionReady'].includes(call.name)));
  }
});

test('DWF child read closes the native parent/run/actor/actual-row proof without write ownership', async (t) => {
  const f = fixture(t, { workflowActor: true, splitActorPages: true, childSnapshotReady: true });
  const parent = (await f.create()).result.sessionId, child = 'sess_dwf-native-run-actor_1_1';
  const messages = await f.call('native_child_messages', { session_id: parent, child_session_id: child, run_id: 'native-run' });
  assert.equal(messages.ok, true); assert.equal(messages.result[0].parts[0].text, 'native child text');
  assert.deepEqual(f.calls.filter((call) => call.name === 'conversationWorkflowRunEventsV4').map((call) => call.input.afterSequence), [0, 1]);
  assert(f.calls.filter((call) => call.name === 'conversationWorkflowRunEventsV4').every((call) => call.input.limit === 100));
  const snapshot = await f.call('native_child_snapshot', { session_id: parent, child_session_id: child, run_id: 'native-run' });
  assert.equal(snapshot.result.session.sessionKind, 'workflow_child');
  assert.equal(snapshot.result.session.parentSessionId, parent);
  assert(!Object.hasOwn(JSON.parse(fs.readFileSync(f.ownershipPath, 'utf8')).sessions, child));
  assert.equal((await f.call('native_send', { session_id: child, request_id: 'dwf-child-write-refused', text: 'x' })).error, 'session_not_owned');
  assert(!f.calls.some((call) => ['resumeSession', 'ensureSessionReady', 'sendPrompt', 'sendConversationCommandV4'].includes(call.name)));
});

test('DWF run and actor identities cannot be replaced with forged IDs or human names', async (t) => {
  for (const [override, runId, expected] of [
    [null, 'foreign-run', 'run_not_verified_for_session'],
    ['forgedActor', 'native-run', 'unsupported'],
    ['badActorOrdinal', 'native-run', 'native_actor_event_invalid'],
    ['wrongEventRun', 'native-run', 'native_actor_journal_shape_invalid'],
  ]) {
    const f = fixture(t, { workflowActor: true, ...(override ? { [override]: true } : {}) });
    const parent = (await f.create()).result.sessionId;
    const result = await f.call('native_child_messages', { session_id: parent, child_session_id: 'sess_dwf-native-run-actor_1_1', run_id: runId });
    assert.equal(result.error, expected);
    assert(!f.calls.some((call) => ['listSessions', 'readSessionMessages', 'readSession'].includes(call.name)));
  }
});

test('DWF journal parent and actual row parent/workspace must all match before content access', async (t) => {
  for (const [override, expected] of [
    ['wrongJournalParent', 'child_not_verified_for_parent'],
    ['wrongChildParent', 'child_not_verified_for_parent'],
    ['wrongChildWorkspace', 'child_workspace_mismatch'],
  ]) {
    const f = fixture(t, { workflowActor: true, [override]: true }), parent = (await f.create()).result.sessionId;
    const result = await f.call('native_child_messages', { session_id: parent, child_session_id: 'sess_dwf-native-run-actor_1_1', run_id: 'native-run' });
    assert.equal(result.error, expected);
    assert(!f.calls.some((call) => ['readSessionMessages', 'readSession'].includes(call.name)));
  }
});

test('DWF verification is bounded and never tries journal reads for an unloaded parent', async (t) => {
  const f = fixture(t, { workflowActor: true, endlessActorPages: true }), parent = (await f.create()).result.sessionId;
  const incomplete = await f.call('native_child_messages', { session_id: parent, child_session_id: 'sess_dwf-native-run-actor_1_1', run_id: 'native-run' });
  assert.equal(incomplete.error, 'unsupported');
  assert.equal(f.calls.filter((call) => call.name === 'conversationWorkflowRunEventsV4').length, 8);
  assert(!f.calls.some((call) => call.name === 'listSessions'));
  const cold = fixture(t, { workflowActor: true }), other = (await cold.create()).result.sessionId;
  cold.unloadSession();
  assert.equal((await cold.call('native_child_messages', { session_id: other, child_session_id: 'sess_dwf-native-run-actor_1_1', run_id: 'native-run' })).error, 'unsupported');
  assert(!cold.calls.some((call) => ['conversationWorkflowRunsV4', 'conversationWorkflowRunEventsV4', 'resumeSession', 'ensureSessionReady'].includes(call.name)));
});

test('idempotent concurrent calls use one creation and conflicting permission cannot reuse an ID', async (t) => {
  const f = fixture(t), responses = await Promise.all([f.create(), f.create()]);
  assert.equal(responses[0].ok, true); assert.equal(responses[1].result.replay, true);
  assert.equal(f.calls.filter((c) => c.name === 'createSession').length, 2);
  assert.equal((await f.create({ caller_permission_mode: 'read_only' })).error, 'request_id_conflict');
});

test('resolution and cancellation use only fixed native envelopes; unsupported operations say so', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  f.sessions.get(sid).projection.pendingPermissions = [{
    requestId: 'actual-interaction', toolName: 'Bash', reason: 'permission',
    options: [
      { optionId: 'actual-option', label: 'Allow once', kind: 'allowOnce' },
      { optionId: 'deny-option', label: 'Deny', kind: 'deny' },
    ],
    fullAccessOption: { optionId: 'fullAccess', label: 'Full access', kind: 'custom' },
  }];
  const resolved = await f.call('native_resolve', { session_id: sid, request_id: 'resolve-request-1',
    interaction_id: 'actual-interaction', answer: { optionId: 'actual-option' } });
  assert.equal(resolved.ok, true);
  assert.deepEqual(f.calls.at(-1).input.envelope.payload, { interactionId: 'actual-interaction', answer: { optionId: 'actual-option' } });
  const denied = await f.call('native_resolve', { session_id: sid, request_id: 'resolve-request-2',
    caller_permission_mode: 'read_only', interaction_id: 'actual-interaction', answer: { optionId: 'actual-option' } });
  assert.equal(denied.error, 'read_only_resolution_not_allowed');
  const cancelled = await f.call('native_cancel', { session_id: sid, request_id: 'cancel-request-1', caller_permission_mode: 'read_only' });
  assert.equal(cancelled.ok, true); assert.equal(f.calls.at(-1).input.envelope.type, 'stop');
  assert.equal((await f.call('native_cancel', { session_id: sid, request_id: 'cancel-request-2', work_id: 'background-job' })).error, 'unsupported');
  assert.equal((await f.call('native_pending', { session_id: sid })).error, 'unsupported');
  assert.equal((await f.call('native_arbitrary_rpc', { method: 'session/resume' })).error, 'unsupported');
});

test('native permission answers must reference the actual pending option', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  f.sessions.get(sid).projection.pendingPermissions = [{
    requestId: 'perm-actual', toolName: 'Bash', reason: 'permission',
    options: [
      { optionId: 'allow-once', label: 'Allow once', kind: 'allowOnce' },
      { optionId: 'deny-once', label: 'Deny', kind: 'deny' },
    ],
    fullAccessOption: { optionId: 'fullAccess', label: 'Full access', kind: 'custom' },
  }];
  const unknown = await f.call('native_resolve', { session_id: sid, request_id: 'perm-unknown-option',
    interaction_id: 'perm-actual', answer: { optionId: 'invented-option' } });
  assert.equal(unknown.error, 'option_not_in_request');
  const missing = await f.call('native_resolve', { session_id: sid, request_id: 'perm-missing-option',
    interaction_id: 'perm-actual', answer: { action: 'accept' } });
  assert.equal(missing.error, 'answer_requires_option');
  const stale = await f.call('native_resolve', { session_id: sid, request_id: 'perm-stale',
    interaction_id: 'not-pending', answer: { optionId: 'allow-once' } });
  assert.equal(stale.error, 'request_not_pending');
  assert(!f.calls.some((c) => c.input?.envelope?.payload?.interactionId === 'perm-actual'));
});

test('weak callers cannot answer a permission request with full access', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  f.sessions.get(sid).projection.pendingPermissions = [{
    requestId: 'perm-full', toolName: 'Bash', reason: 'permission',
    options: [{ optionId: 'allow-once', label: 'Allow once', kind: 'allowOnce' }],
    fullAccessOption: { optionId: 'fullAccess', label: 'Full access', kind: 'custom' },
  }];
  for (const caller of ['always_ask', 'review_important_actions', 'read_only']) {
    const response = await f.call('native_resolve', { session_id: sid,
      request_id: 'perm-full-' + caller, caller_permission_mode: caller,
      interaction_id: 'perm-full', answer: { optionId: 'fullAccess' }, user_approved: true });
    assert.equal(response.error, caller === 'read_only' ? 'read_only_resolution_not_allowed' : 'permission_escalation_denied', caller);
  }
  const strong = await f.call('native_resolve', { session_id: sid, request_id: 'perm-full-strong',
    caller_permission_mode: 'full_access', interaction_id: 'perm-full', answer: { optionId: 'fullAccess' } });
  assert.equal(strong.ok, true);
  assert(f.calls.some((c) => c.input?.envelope?.payload?.interactionId === 'perm-full'));
});

test('allow-like permission answers need explicit approval below full access', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  f.sessions.get(sid).projection.pendingPermissions = [{
    requestId: 'perm-allow', toolName: 'Bash', reason: 'permission',
    options: [
      { optionId: 'allow-once', label: 'Allow once', kind: 'allowOnce' },
      { optionId: 'allow-always', label: 'Always allow', kind: 'allowAlways' },
      { optionId: 'deny-once', label: 'Deny', kind: 'deny' },
    ],
    fullAccessOption: { optionId: 'fullAccess', label: 'Full access', kind: 'custom' },
  }];
  const unapproved = await f.call('native_resolve', { session_id: sid, request_id: 'perm-unapproved',
    caller_permission_mode: 'always_ask', interaction_id: 'perm-allow', answer: { optionId: 'allow-once' } });
  assert.equal(unapproved.error, 'user_approved_required');
  const content = await f.call('native_resolve', { session_id: sid, request_id: 'perm-content',
    caller_permission_mode: 'always_ask', interaction_id: 'perm-allow',
    answer: { optionId: 'allow-once', content: { allow: true } } });
  assert.equal(content.error, 'invalid_answer');
  const approved = await f.call('native_resolve', { session_id: sid, request_id: 'perm-approved',
    caller_permission_mode: 'always_ask', interaction_id: 'perm-allow',
    answer: { optionId: 'allow-always' }, user_approved: true });
  assert.equal(approved.ok, true);
  const modeChanges = f.calls.filter((c) => c.name === 'setMode').length;
  const denied = await f.call('native_resolve', { session_id: sid, request_id: 'perm-deny-readonly',
    caller_permission_mode: 'read_only', interaction_id: 'perm-allow', answer: { optionId: 'deny-once' } });
  assert.equal(denied.ok, true);
  assert.equal(f.calls.filter((c) => c.name === 'setMode').length, modeChanges);
});

test('AskUserQuestion keeps working as a real user-input interaction', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  f.sessions.get(sid).projection.pendingPermissions = [{
    requestId: 'ask-actual', toolName: 'AskUserQuestion', reason: 'tool_use',
    input: { questions: [{ question: '继续吗？', header: 'Ask',
      options: [{ label: '继续' }, { label: '停止' }] }] },
  }];
  const answered = await f.call('native_resolve', { session_id: sid, request_id: 'ask-answer',
    caller_permission_mode: 'always_ask', interaction_id: 'ask-actual',
    answer: { action: 'accept', content: { answers: { '继续吗？': '继续' } } } });
  assert.equal(answered.ok, true);
  assert.deepEqual(f.calls.at(-1).input.envelope.payload.answer,
    { action: 'accept', content: { answers: { '继续吗？': '继续' } } });
});

test('recorded dispatch errors and explicit native rejection have different outcomes', async (t) => {
  const f = fixture(t), sid = (await f.create()).result.sessionId;
  // A known invalid answer is refused before entering the native command path.
  const invalid = await f.call('native_resolve', { session_id: sid, request_id: 'resolve-invalid-1',
    interaction_id: 'actual-interaction', answer: { action: 'invented-action' } });
  assert.equal(invalid.error, 'invalid_answer'); assert.equal(invalid.request.status, 'failed');
  assert(!f.calls.some((c) => c.name === 'sendConversationCommandV4'));
});

test('one active project that owns the workspace is reused for the created session', async (t) => {
  const queries = [];
  const f = fixture(t, { resolveProjectId: async (input) => {
    queries.push(copy(input));
    return { status: 'unique', projectId: 'prj_workspace' };
  } });
  const response = await f.create();
  assert.equal(response.ok, true);
  assert.equal(queries.length, 1, 'the resolver runs once per native_create');
  assert.equal(queries[0].workspacePath, f.workspace);
  assert.equal(queries[0].projectId, null, 'no project is inferred for the caller');
  assert.deepEqual(response.result.projectAssignment,
    { status: 'assigned', projectId: 'prj_workspace', source: 'workspace' });
  const creates = f.calls.filter((c) => c.name === 'createSession');
  assert.equal(creates.length, 2);
  assert.equal(creates[0].input.projectId, null, 'the deferred catalogue probe is never attached to a project');
  assert.equal(creates[1].input.projectId, 'prj_workspace');
  assert.equal(f.calls.filter((c) => c.name === 'ensureWorkspaceSubscription').length, 1);
  assert.deepEqual(f.notifications, [], 'create-only must not emit a list refresh');
});

test('no matching project keeps the session explicitly unassigned', async (t) => {
  const f = fixture(t, { resolveProjectId: async () => ({ status: 'none', projectId: null }) });
  const response = await f.create();
  assert.equal(response.ok, true);
  assert.deepEqual(response.result.projectAssignment, { status: 'unassigned', projectId: null, source: 'none' });
  assert.equal(f.calls.filter((c) => c.name === 'createSession')[1].input.projectId, null);
});

test('an ambiguous workspace is rejected before catalogue, subscription or create', async (t) => {
  const f = fixture(t, { resolveProjectId: async () => ({ status: 'ambiguous', projectId: null, count: 2 }) });
  const response = await f.create();
  assert.equal(response.ok, false);
  assert.equal(response.error, 'project_ambiguous');
  assert.equal(response.request.status, 'failed'); assert.equal(response.request.phase, 'validated');
  assert.equal(f.calls.filter((c) => c.name === 'createSession').length, 0);
  assert(!f.calls.some((c) => c.name === 'closeSession' || c.name === 'ensureWorkspaceSubscription'));
  assert.deepEqual(f.notifications, []);
  assert(!f.sessions.size);
});

test('an explicit project id must exist, be active and contain the workspace', async (t) => {
  const f = fixture(t, { resolveProjectId: async ({ projectId }) => {
    if (projectId === 'prj_ok') return { status: 'unique', projectId };
    if (projectId === 'prj_old') return { status: 'inactive', projectId: null };
    return { status: projectId === 'prj_missing' ? 'not_found' : 'workspace_mismatch', projectId: null };
  } });
  for (const [projectId, code] of [['prj_missing', 'project_not_found'], ['prj_old', 'project_inactive'],
    ['prj_elsewhere', 'project_workspace_mismatch']]) {
    const response = await f.create({ project_id: projectId, request_id: 'create-' + projectId });
    assert.equal(response.error, code, projectId);
    assert.equal(f.calls.filter((c) => c.name === 'createSession').length, 0);
  }
  const ok = await f.create({ request_id: 'create-explicit-ok', project_id: 'prj_ok' });
  assert.equal(ok.ok, true);
  assert.deepEqual(ok.result.projectAssignment, { status: 'assigned', projectId: 'prj_ok', source: 'caller' });
  assert.equal(f.calls.filter((c) => c.name === 'createSession')[1].input.projectId, 'prj_ok');
});

test('create-stage facts are missing and only the first accepted input makes them durable', async (t) => {
  const f = fixture(t, { resolveProjectId: async () => ({ status: 'unique', projectId: 'prj_workspace' }) });
  const created = await f.create();
  const sid = created.result.sessionId;
  // Reproduces the observed create-only state: no persisted CLI fact yet.
  assert.equal(created.result.desktopIndexFacts.sessions[0].exists, false);
  assert(!f.durableSessions.has(sid));
  assert.deepEqual(f.notifications, [], 'create alone has no durable CLI fact and must not notify');
  const first = await f.call('native_send', { session_id: sid, request_id: 'first-durable-send', text: 'first' });
  assert.equal(first.ok, true);
  // Acceptance is what creates the durable fact, so the single import succeeds.
  assert(f.durableSessions.has(sid));
  assert.deepEqual(first.result.listNotification, { state: 'facts_imported', changed: true });
  assert.equal(f.notifications.filter((n) => n.name === 'syncSessionFacts').length, 1);
  assert.equal(f.notifications.filter((n) => n.name === 'factsReady').length, 1);
  assert.equal(f.notifications.filter((n) => n.name === 'emitWorkspaceTaskListChanged').length, 1);
  assert.deepEqual(f.notifications.find((n) => n.name === 'syncSessionFacts').input,
    { workspacePath: f.workspace, sessionId: sid }, 'no workspace identity, prompt or token is sent');
});

test('a changed=false import still produces exactly one list event', async (t) => {
  const f = fixture(t, { syncChanged: false });
  const sid = (await f.create()).result.sessionId;
  const first = await f.call('native_send', { session_id: sid, request_id: 'first-unchanged-send', text: 'first' });
  assert.deepEqual(first.result.listNotification, { state: 'facts_imported', changed: false });
  const emits = f.notifications.filter((n) => n.name === 'emitWorkspaceTaskListChanged');
  assert.equal(emits.length, 1);
  assert.deepEqual(emits[0].ref, { workspacePath: f.workspace, taskId: sid });
  assert.equal(emits[0].reason, 'native_project_facts');
});

test('later sends, reads and replays never repeat the list refresh', async (t) => {
  const f = fixture(t);
  const sid = (await f.create()).result.sessionId;
  const first = { session_id: sid, request_id: 'first-once', text: 'first' };
  assert.equal((await f.call('native_send', first)).ok, true);
  const baseline = f.notifications.length;
  assert.equal((await f.call('native_send', { session_id: sid, request_id: 'second-send', text: 'second' })).ok, true);
  assert.equal((await f.call('native_snapshot', { session_id: sid })).ok, true);
  assert.equal((await f.call('native_messages', { session_id: sid })).ok, true);
  const replay = await f.call('native_send', first);
  assert.equal(replay.ok, true); assert.equal(replay.result.replay, true);
  assert.equal(f.notifications.length, baseline, 'no second import and no second list event');
});

test('notification failure or missing facts stays diagnostic and is never retried', async (t) => {
  const thrown = fixture(t, { syncThrows: true });
  const thrownSid = (await thrown.create()).result.sessionId;
  const first = await thrown.call('native_send', { session_id: thrownSid, request_id: 'first-throws', text: 'first' });
  assert.equal(first.ok, true, 'an accepted send is not turned into a failure');
  assert.deepEqual(first.result.listNotification, { state: 'failed', error: 'index_unavailable' });
  assert.equal(thrown.notifications.filter((n) => n.name === 'syncSessionFacts').length, 1);
  const later = await thrown.call('native_send', { session_id: thrownSid, request_id: 'second-throws', text: 'second' });
  assert.equal(later.result.listNotification, undefined);
  assert.equal(thrown.notifications.filter((n) => n.name === 'syncSessionFacts').length, 1);

  const missing = fixture(t, { syncMissing: true });
  const missingSid = (await missing.create()).result.sessionId;
  const noFacts = await missing.call('native_send', { session_id: missingSid, request_id: 'first-missing', text: 'first' });
  assert.equal(noFacts.ok, true);
  assert.deepEqual(noFacts.result.listNotification, { state: 'facts_unavailable' });
  assert.equal(missing.notifications.filter((n) => n.name === 'emitWorkspaceTaskListChanged').length, 0);
  await missing.call('native_send', { session_id: missingSid, request_id: 'second-missing', text: 'second' });
  assert.equal(missing.notifications.filter((n) => n.name === 'syncSessionFacts').length, 1);
});


test('sends never re-create or re-attribute an existing owned session', async (t) => {
  let queries = 0;
  const f = fixture(t, { resolveProjectId: async () => {
    queries += 1;
    return { status: 'unique', projectId: 'prj_workspace' };
  } });
  const created = await f.create();
  const sid = created.result.sessionId;
  const creates = f.calls.filter((c) => c.name === 'createSession').length;
  await f.call('native_send', { session_id: sid, request_id: 'first-attribute', text: 'first' });
  await f.call('native_send', { session_id: sid, request_id: 'second-attribute', text: 'second' });
  assert.equal(f.calls.filter((c) => c.name === 'createSession').length, creates);
  assert.equal(f.calls.filter((c) => c.name === 'createSession').at(-1).input.projectId, 'prj_workspace');
  assert.equal(created.result.projectAssignment.projectId, 'prj_workspace');
  assert.equal(queries, 1, 'only create resolves a project; sends never re-attribute');
});

test('the resolver and notification paths add no timers, polling or generic channel', () => {
  const source = fs.readFileSync(new URL('../runtime/desktop_sync/native_transport.mjs', import.meta.url), 'utf8');
  assert(!/setInterval\s*\(/.test(source));
  assert(!/setTimeout\s*\(/.test(source));
  assert(!/eval\s*\(/.test(source));
  assert(!/new Function\s*\(/.test(source));
  assert(!/child_process/.test(source));
});
