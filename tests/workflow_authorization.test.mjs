import test from 'node:test';
import assert from 'node:assert/strict';
import { createZcodeWorkflowAuthorization } from '../runtime/desktop_sync/workflow_authorization.mjs';

function fixture(t, changes = {}) {
  const sends = [], reads = [];
  let clock = 1000, config = {}, activeMode = 'yolo';
  const interaction = { interactionId: 'permission-1', kind: 'permission', payload: {
    kind: 'permission', toolName: 'CreateWorkflow', options: [
      { optionId: 'allow_once', label: 'Allow once', kind: 'allowOnce' },
      { optionId: 'allowSession', label: 'Always allow in this session', kind: 'allow_session' },
      { optionId: 'deny', label: 'Deny', kind: 'rejectOnce' },
    ], fullAccessOption: { optionId: 'fullAccess', kind: 'custom', label: 'Full access' },
  } };
  const agent = {
    readSession: async target => {
      reads.push(target);
      return { settings: { permission: { mode: activeMode } },
        projection: { pendingPermissions: [{ toolName: 'CreateWorkflow' }] } };
    },
    createSession: async () => ({ session: { sessionId: 'from-desktop' } }),
    sendConversationCommandV4: async request => { sends.push(request); return { status: 'accepted' }; },
    ...changes.agent,
  };
  const originalRead = agent.readSession;
  const policy = createZcodeWorkflowAuthorization({ agent,
    makeEnvelope: value => value,
    readInteractions: async target => {
      changes.beforeInteraction?.();
      return { sessionId: target.sessionId, mode: activeMode, interactions: [interaction] };
    },
    readConfig: () => config,
    resolveWorkspace: value => value === '/workspace' ? value : null,
    now: () => clock,
    setTimer: () => ({ unref() {} }), clearTimer: () => {},
    ...changes.options,
  });
  t.after(() => policy.dispose());
  return { policy, agent, sends, reads, interaction, originalRead,
    track: () => policy.track({ workspacePath: '/workspace', sessionId: 'session-1' }),
    advance: () => { clock += 2000; },
    config: value => { config = value; }, mode: value => { activeMode = value; } };
}

test('defaults to automatic, with no Codex connection, and selects the real session choice', async t => {
  const f = fixture(t); f.track(); await f.policy.tick();
  assert.equal(f.sends.length, 1);
  assert.deepEqual(f.sends[0].envelope.payload, {
    interactionId: 'permission-1', answer: { optionId: 'allowSession' },
  });
  assert.equal(f.sends[0].runtimePolicy, 'existing-only');
  f.advance(); await f.policy.tick();
  assert.equal(f.sends.length, 1, 'the same pending request must not be answered twice');
});

test('observes sessions created directly in Desktop, without a bridge ownership ledger', async t => {
  const f = fixture(t); f.policy.start();
  await f.agent.createSession({ workspacePath: '/workspace' });
  await f.policy.tick();
  assert.equal(f.sends[0].envelope.sessionId, 'from-desktop');
  f.policy.dispose();
  assert.equal(f.agent.readSession, f.originalRead);
});

test('manual mode leaves the workflow request untouched', async t => {
  const f = fixture(t); f.config({ workflowAuthorization: { mode: 'manual' } });
  f.track(); await f.policy.tick();
  assert.deepEqual(f.sends, []); assert.deepEqual(f.reads, []);
});

test('disabling the external native API does not disable ZCode local workflow consent', async t => {
  const f = fixture(t); f.config({ nativeTransport: { enabled: false } });
  f.track(); await f.policy.tick();
  assert.equal(f.sends[0].envelope.payload.answer.optionId, 'allowSession');
});

test('a switch to manual while reading prevents the write', async t => {
  let f;
  f = fixture(t, { beforeInteraction: () => f.config({ workflowAuthorization: { mode: 'manual' } }) });
  f.track(); await f.policy.tick(); assert.deepEqual(f.sends, []);
});

test('plan mode never elevates itself', async t => {
  const f = fixture(t); f.mode('plan'); f.track(); await f.policy.tick();
  assert.deepEqual(f.sends, []);
});

test('other permissions and actual questions are not auto-answered', async t => {
  const f = fixture(t); f.track();
  f.interaction.payload.toolName = 'Bash'; await f.policy.tick();
  f.advance(); f.interaction.kind = 'userInput'; f.interaction.payload.toolName = 'CreateWorkflow';
  await f.policy.tick(); assert.deepEqual(f.sends, []);
});

test('missing session permission does not silently fall back to allow once', async t => {
  const f = fixture(t); f.interaction.payload.options = f.interaction.payload.options.filter(o=>o.optionId!=='allowSession');
  f.track(); await f.policy.tick(); assert.deepEqual(f.sends, []);
});

test('session wording on full access or another permission cannot grant it automatically', async t => {
  const f = fixture(t);
  f.interaction.payload.options = [{optionId:'allow_once',kind:'allowOnce',label:'Always allow in this session'}];
  f.interaction.payload.fullAccessOption.label='Always allow in this session';
  f.track();await f.policy.tick();assert.deepEqual(f.sends,[]);
});

test('workspace restrictions and malformed configuration are respected', async t => {
  const f = fixture(t);
  f.policy.track({ workspacePath: '/blocked', sessionId: 'session-1' });
  await f.policy.tick(); assert.deepEqual(f.reads, []);
  f.track(); f.config({ workflowAuthorization: 'invalid' });
  await f.policy.tick(); assert.deepEqual(f.sends, []);
});

test('an uncertain reply is recorded and never blindly retried', async t => {
  let attempts = 0;
  const f = fixture(t, { agent: { sendConversationCommandV4: async () => {
    attempts++; throw Object.assign(new Error('transport'), { code: 'closed' });
  } } });
  f.track(); await f.policy.tick(); f.advance(); await f.policy.tick();
  assert.equal(attempts, 1);
  assert.equal(f.policy.status().recent[0].outcome, 'uncertain');
});

test('bounded SDK checks remain fair across more than one batch of Desktop sessions', async t => {
  const f=fixture(t);
  for(let i=0;i<12;i++)f.policy.track({workspacePath:'/workspace',sessionId:'desktop-'+i});
  await f.policy.tick();assert.equal(f.sends.length,8);
  f.advance();await f.policy.tick();
  assert.equal(new Set(f.sends.map(s=>s.envelope.sessionId)).size,12);
});
