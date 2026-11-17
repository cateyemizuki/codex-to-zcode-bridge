/* ZCODE_WORKFLOW_AUTHORIZATION_V1 */
import { createHash as __zcodeWorkflowHash } from 'node:crypto';

export function createZcodeWorkflowAuthorization({
  agent, makeEnvelope, readInteractions, readConfig, resolveWorkspace,
  now = Date.now, setTimer = setInterval, clearTimer = clearInterval,
}) {
  const targets = new Map();
  const decisions = new Map();
  const missingChoices = new Set();
  const recent = [];
  const restore = [];
  let timer = null;
  let checking = false;
  let disposed = false;
  const originalRead = agent.readSession.bind(agent);
  const originalSend = agent.sendConversationCommandV4.bind(agent);
  const allowed = value => { try { return resolveWorkspace(value); } catch { return null; } };
  const tools = new Set(['CreateWorkflow', 'AmendWorkflow', 'RunWorkflow']);
  const plain = v => v && typeof v === 'object' && !Array.isArray(v);
  const mode = () => {
    try {
      const value = readConfig();
      if (!plain(value)) return 'unavailable';
      if (value.workflowAuthorization !== undefined && !plain(value.workflowAuthorization)) return 'unavailable';
      const selected = value.workflowAuthorization?.mode ?? 'automatic';
      return ['automatic', 'manual'].includes(selected) ? selected : 'unavailable';
    } catch { return 'unavailable'; }
  };
  const note = (sessionId, requestId, outcome, errorCode) => {
    const knownCodes = new Set(['host_update_required', 'interaction_shape_invalid', 'resolution_not_accepted',
      'resolution_failed', 'read_failed', 'closed', 'session_choice_not_available']);
    recent.push({ sessionId, requestId, outcome, at: now(),
      ...(errorCode ? { errorCode: knownCodes.has(String(errorCode)) ? String(errorCode)
        : typeof errorCode === 'number' && Number.isInteger(errorCode) ? 'native_' + errorCode : 'operation_failed' } : {}) });
    if (recent.length > 50) recent.shift();
  };
  function track(input, output, urgent = false) {
    if (disposed || !plain(input) || input.remoteSessionId) return;
    const workspace = allowed(input.workspacePath);
    const sessionId = input.envelope?.sessionId ?? input.sessionId ?? input.taskId
      ?? output?.session?.sessionId ?? output?.sessionId;
    if (!workspace || typeof sessionId !== 'string' || !sessionId) return;
    const key = workspace + '\0' + sessionId;
    if (!targets.has(key)) {
      targets.set(key, { workspacePath: workspace, sessionId,
        ...(input.workspaceIdentity ? { workspaceIdentity: input.workspaceIdentity } : {}),
        runtimePolicy: 'existing-only', nextCheck: -now(), failures: 0, idleSamples: 0 });
    } else if (urgent) targets.get(key).nextCheck = -now();
  }
  function sessionChoice(interaction) {
    const payload = interaction?.payload;
    if (interaction?.kind !== 'permission' || !plain(payload)) return null;
    const toolName = payload.toolName ?? payload.name;
    if (!tools.has(toolName)) return null;
    const candidates = Array.isArray(payload.options) ? payload.options : [];
    return candidates.find(option => {
      if (!plain(option) || typeof option.optionId !== 'string') return false;
      if (option.optionId === 'fullAccess' || !(option.kind === 'allow_session' || option.optionId === 'allowSession')) return false;
      if (/^(deny|reject)/i.test(option.kind ?? '') || option.response?.decision === 'deny') return false;
      const label = String(option.label ?? option.name ?? '').trim().toLowerCase().replace(/\s+/g, ' ');
      return label === 'always allow in this session' || label === '本会话内始终允许';
    }) ?? null;
  }
  function hasPending(snapshot) {
    for (const container of [snapshot, snapshot?.projection, snapshot?.runtime]) {
      if (container?.pendingPermissions?.some(item => tools.has(item.toolName))) return true;
      if (container?.pendingInteractions?.some(item => tools.has(item.payload?.toolName ?? item.payload?.name))) return true;
    }
    return false;
  }
  async function inspect(target) {
    const current = await originalRead(target);
    const currentMode = current?.settings?.permission?.mode ?? current?.settings?.mode?.current
      ?? current?.projection?.mode;
    if (currentMode === 'plan') return false;
    if (!hasPending(current)) return Boolean(current?.projection?.status === 'running'
      || current?.projection?.backgroundJobs?.some(job => ['running', 'pending'].includes(job.status)));
    const full = await readInteractions(target);
    if (!plain(full) || full.sessionId !== target.sessionId || !Array.isArray(full.interactions)) {
      throw Object.assign(new Error('Native interactions unavailable'), { code: 'interaction_shape_invalid' });
    }
    if (!['yolo', 'edit', 'build'].includes(full.mode)) return false;
    for (const interaction of full.interactions) {
      const requestId = interaction.interactionId ?? interaction.id;
      const option = sessionChoice(interaction);
      if (typeof requestId !== 'string') continue;
      const key = target.workspacePath + '\0' + target.sessionId + '\0' + requestId;
      if (!option) {
        if (interaction.kind === 'permission' && tools.has(interaction.payload?.toolName ?? interaction.payload?.name)
            && !missingChoices.has(key)) {
          missingChoices.add(key);
          note(target.sessionId, requestId, 'manual_required', 'session_choice_not_available');
        }
        continue;
      }
      if (decisions.has(key)) continue;
      // A person can switch to manual while a snapshot is in flight.
      if (mode() !== 'automatic' || !allowed(target.workspacePath) || disposed) return;
      const commandId = 'workflow-policy-' + __zcodeWorkflowHash('sha256').update(key).digest('hex');
      decisions.set(key, 'sending');
      try {
        const result = await originalSend({ ...target, envelope: makeEnvelope({
          type: 'resolveInteraction', sessionId: target.sessionId, commandId,
          clientId: 'zcode-workflow-authorization',
          payload: { interactionId: requestId, answer: { optionId: option.optionId } },
        }) });
        // Admission is not proof of task completion. Never resend an uncertain write.
        const accepted = ['accepted', 'duplicate', 'noop'].includes(result?.status);
        decisions.set(key, accepted ? 'accepted' : 'uncertain');
        note(target.sessionId, requestId, accepted ? 'accepted' : 'uncertain',
          accepted ? null : 'resolution_not_accepted');
      } catch (error) {
        decisions.set(key, 'uncertain');
        note(target.sessionId, requestId, 'uncertain', error?.code ?? 'resolution_failed');
      }
    }
    return true;
  }
  async function tick() {
    if (checking || disposed || mode() !== 'automatic') return;
    checking = true;
    try {
      const due = [...targets.values()].filter(target=>target.nextCheck<=now())
        .sort((a,b)=>a.nextCheck-b.nextCheck).slice(0,8);
      const check = async target => {
        if (disposed || mode() !== 'automatic') return;
        if (target.nextCheck > now() || !allowed(target.workspacePath)) return;
        try {
          const active = await inspect(target);
          target.failures = 0;
          target.idleSamples = active ? 0 : target.idleSamples + 1;
          target.nextCheck = now() + (active ? 1000 : Math.min(60000, 1000 * 2 ** Math.min(target.idleSamples, 6)));
        } catch (error) {
          target.failures += 1;
          target.nextCheck = now() + Math.min(60000, 1000 * 2 ** Math.min(target.failures, 6));
          note(target.sessionId, '', 'unavailable', error?.code ?? 'read_failed');
        }
      };
      for (let index=0;index<due.length;index+=4) {
        await Promise.allSettled(due.slice(index,index+4).map(check));
      }
    } finally { checking = false; }
  }
  function start() {
    if (timer || disposed) return;
    if (typeof readInteractions !== 'function' || typeof makeEnvelope !== 'function') {
      throw Object.assign(new Error('Complete native interactions are required'), { code: 'host_update_required' });
    }
    // Observe only targets actually used by this Desktop. Never create a session,
    // launch an engine, or depend on a Codex client/notification subscription.
    for (const name of ['createSession', 'sendPrompt', 'sendConversationCommandV4',
      'readSession', 'subscribeConversationV4', 'listSessions']) {
      if (typeof agent[name] !== 'function') continue;
      const original = agent[name];
      const wrapped = async function(input, ...rest) {
        const urgent=name==='sendPrompt'||name==='sendConversationCommandV4';
        track(input, undefined, urgent);
        const result = await original.call(this, input, ...rest);
        track(input, result, urgent);
        if (name === 'listSessions') {
          for (const row of Array.isArray(result) ? result : result?.sessions ?? []) {
            if (['running', 'waiting', 'needs_attention'].includes(row.status)) {
              track({ ...input, sessionId: row.sessionId });
            }
          }
        }
        return result;
      };
      agent[name] = wrapped;
      restore.push(() => { if (agent[name] === wrapped) agent[name] = original; });
    }
    timer = setTimer(() => { void tick(); }, 1000);
    timer?.unref?.();
  }
  function dispose() {
    disposed = true;
    if (timer) clearTimer(timer);
    timer = null;
    for (const undo of restore.reverse()) undo();
  }
  return Object.freeze({ start, tick, track, dispose,
    status: () => ({ mode: mode(), trackedSessions: targets.size,
      recent: recent.map(row => ({ ...row })), running: Boolean(timer) && !disposed }) });
}
