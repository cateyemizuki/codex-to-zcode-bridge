/* ZCODE_DESKTOP_NATIVE_TRANSPORT_V1 */
import * as __zcodeNativeFs from 'node:fs';
import * as __zcodeNativePath from 'node:path';
import * as __zcodeNativeCrypto from 'node:crypto';

const __zcodeNativeModes = Object.freeze({
  full_access: 'yolo', review_important_actions: 'edit',
  ask_before_writes: 'build', always_ask: 'build', read_only: 'plan',
});

export function createZcodeNativeInteractionReader(readSnapshot) {
  const queues = new Map();
  return async target => {
    // Workspace aliases can point at the same live session. Serialize by SID;
    // this is a gate only, never a cache that could return another target's data.
    const key = target.sessionId;
    const work = (queues.get(key) ?? Promise.resolve()).catch(() => {}).then(async () => {
      const current = await readSnapshot(target);
      if (!Array.isArray(current?.pendingInteractions) || typeof current?.config?.mode !== 'string') {
        throw Object.assign(new Error('Native interactions are unavailable'), { code: 'native_interactions_shape_invalid' });
      }
      return { sessionId: target.sessionId, logEpoch: current.logEpoch, revision: current.revision,
        mode: current.config.mode, interactions: current.pendingInteractions };
    });
    queues.set(key, work);
    try { return await work; }
    finally { if (queues.get(key) === work) queues.delete(key); }
  };
}

export function createZcodeDesktopNativeTransport({
  agent, makeEnvelope, ownershipPath, resolveWorkspace, resolveProjectId = null, syncer = () => null,
  readInteractions = null,
  fs = __zcodeNativeFs, now = Date.now,
}) {
  // The host passes a private facade. No generic RPC, engine or connection is returned.
  const source = 'desktop-native-host';
  let queue = Promise.resolve();
  let state;
  const fail = (code, detail) => {
    const error = new Error(detail || code);
    error.code = code;
    throw error;
  };
  if (!ownershipPath || !__zcodeNativePath.isAbsolute(ownershipPath)) fail('ownership_path_required');
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const plain = (value) => value && typeof value === 'object' && !Array.isArray(value);
  const text = (value, name, max = 200) => {
    if (typeof value !== 'string' || !value.trim() || value.length > max) fail('invalid_' + name);
    return value;
  };
  const method = (name) => {
    if (typeof agent?.[name] !== 'function') fail('unsupported', name);
    return agent[name].bind(agent);
  };
  const hash = (value) => __zcodeNativeCrypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
  const ref = (value) => {
    if (!plain(value)) fail('model_required');
    return { providerId: text(value.providerId, 'provider_id'), modelId: text(value.modelId, 'model_id') };
  };
  const sameModel = (left, right) => left?.providerId === right?.providerId && left?.modelId === right?.modelId;
  const selected = (snapshot) => snapshot?.settings?.model?.current;
  const modeOf = (snapshot) => snapshot?.settings?.permission?.mode ?? snapshot?.settings?.mode?.current;
  const effortOf = (snapshot) => snapshot?.settings?.thoughtLevel?.current ?? null;
  const levelsOf = (snapshot) => (snapshot?.settings?.thoughtLevel?.available ?? [])
    .map((level) => typeof level === 'string' ? level : level?.value).filter((level) => typeof level === 'string');
  const diagnostic = (value, payload = {}) => {
    if (typeof value !== 'string') return undefined;
    let output = value;
    for (const privateText of [payload.text, payload.answer?.freeText]) {
      if (typeof privateText === 'string' && privateText) output = output.split(privateText).join('[input redacted]');
    }
    return output
      .replace(/\bBearer\s+[A-Za-z0-9._~+\/=-]+/gi, 'Bearer [redacted]')
      .replace(/\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)\b\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi, '$1=[redacted]')
      .slice(0, 2000);
  };
  function nativeErrorDetails(error, message) {
    if (error?.name !== 'ZCodeProtocolClientError' || !Number.isInteger(error.code)) return undefined;
    const data = {};
    for (const key of ['code', 'reasonCode']) {
      const value = error.data?.[key];
      if (typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,200}$/.test(value)) data[key] = value;
    }
    if (typeof error.data?.recoverable === 'boolean') data.recoverable = error.data.recoverable;
    return { code: error.code, message: diagnostic(error.message, { text: message?.text, answer: message?.answer }),
      ...(Object.keys(data).length ? { data } : {}) };
  }
  const targetOf = (workspace, sessionId) => ({ workspacePath: workspace, sessionId, runtimePolicy: 'existing-only' });
  const page = (value, fallback = 100) => {
    if (value === undefined) return fallback;
    if (!Number.isInteger(value) || value < 1 || value > 1000) fail('invalid_limit');
    return value;
  };
  const sequence = (value) => {
    if (value !== undefined && (!Number.isInteger(value) || value < 0)) fail('invalid_cursor');
    return value;
  };
  function loadState() {
    if (state) return state;
    try {
      const loaded = JSON.parse(fs.readFileSync(ownershipPath, 'utf8'));
      if (loaded.version !== 1 || !plain(loaded.sessions) || !plain(loaded.requests)) fail('ownership_state_invalid');
      state = loaded;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      state = { version: 1, sessions: {}, requests: {} };
    }
    return state;
  }
  function save() {
    fs.mkdirSync(__zcodeNativePath.dirname(ownershipPath), { recursive: true });
    const temporary = ownershipPath + '.' + __zcodeNativeCrypto.randomUUID() + '.tmp';
    try {
      fs.writeFileSync(temporary, JSON.stringify(state, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
      fs.renameSync(temporary, ownershipPath);
    } finally {
      try { fs.unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
  }
  function put(object, key, value) {
    Object.defineProperty(object, key, { value, writable: true, enumerable: true, configurable: true });
  }
  function permission(message) {
    const value = message.caller_permission_mode;
    if (!own(__zcodeNativeModes, value)) fail('caller_permission_required');
    return { caller: value, mode: __zcodeNativeModes[value] };
  }
  async function runtime(workspace) {
    const identity = await method('getWorkspaceRuntimeIdentity')({ workspacePath: workspace });
    if (typeof identity?.identity !== 'string' || !identity.identity) fail('runtime_identity_unavailable');
    return { identity: identity.identity, generation: identity.generation ?? null };
  }
  async function owned(workspace, sessionId, { catalog = false } = {}) {
    text(sessionId, 'session_id');
    const record = own(state.sessions, sessionId) ? state.sessions[sessionId] : null;
    if (!record || record.workspace !== workspace || record.kind !== (catalog ? 'catalog' : 'session')) fail('session_not_owned');
    if (record.closed) fail('session_closed');
    const live = await runtime(workspace);
    if (live.identity !== record.runtime.identity || live.generation !== record.runtime.generation) fail('stale_owner');
    return record;
  }
  function rememberSession(snapshot, workspace, identity, kind, requestId) {
    const sid = text(snapshot?.session?.sessionId, 'native_session_id');
    if (own(state.sessions, sid)) fail('native_session_id_collision');
    const record = { sessionId: sid, workspace, runtime: identity, kind, requestId, createdAt: now(), closed: false };
    put(state.sessions, sid, record);
    save();
    return record;
  }
  function checkSelection(snapshot, model, thought, mode) {
    if (!sameModel(selected(snapshot), model)) fail('native_model_mismatch');
    if (effortOf(snapshot) !== thought) fail('native_thought_mismatch');
    if (mode !== undefined && modeOf(snapshot) !== mode) fail('native_permission_mismatch');
  }
  function requestedSelection(message) {
    const model = ref(message.model);
    if (!own(message, 'thought_level') || (message.thought_level !== null && typeof message.thought_level !== 'string')) fail('thought_level_required');
    return { model, thought: message.thought_level };
  }
  // Fixed project resolution. The host injects one read-only resolver at install
  // time; this module never guesses a project from names, parent paths or other
  // sessions, and never creates or rewrites a project. Without an injected
  // resolver an explicit caller id is passed through unchanged and no project is
  // inferred, which keeps isolated module use working as before.
  const projectRejections = Object.freeze({
    ambiguous: 'project_ambiguous', not_found: 'project_not_found',
    inactive: 'project_inactive', workspace_mismatch: 'project_workspace_mismatch',
    invalid: 'project_resolution_invalid',
  });
  async function resolveCreationProject(workspace, requestedProjectId) {
    const explicit = typeof requestedProjectId === 'string' && requestedProjectId ? requestedProjectId : null;
    if (typeof resolveProjectId !== 'function') {
      return { projectId: explicit, source: explicit ? 'caller' : 'none' };
    }
    let decision;
    try {
      decision = await resolveProjectId({ workspacePath: workspace, projectId: explicit });
    } catch (error) {
      fail('project_resolution_failed', diagnostic(String(error?.message || error)));
    }
    if (!plain(decision) || typeof decision.status !== 'string') fail('project_resolution_invalid');
    if (own(projectRejections, decision.status)) fail(projectRejections[decision.status]);
    if (decision.status === 'unique') {
      const projectId = decision.projectId;
      if (typeof projectId !== 'string' || !projectId.trim() || projectId.length > 200) fail('project_resolution_invalid');
      if (explicit && projectId !== explicit) fail('project_resolution_invalid');
      return { projectId, source: explicit ? 'caller' : 'workspace' };
    }
    if (decision.status !== 'none') fail('project_resolution_invalid');
    if (decision.projectId !== null && decision.projectId !== undefined) fail('project_resolution_invalid');
    return { projectId: null, source: 'none' };
  }
  function projectAssignment(project) {
    return { status: project.projectId ? 'assigned' : 'unassigned', projectId: project.projectId, source: project.source };
  }
  function availableModels(snapshot) {
    const available = snapshot?.settings?.model?.available;
    if (!Array.isArray(available)) fail('native_catalog_unavailable');
    return available.map((item) => {
      let reasoning;
      if (plain(item.reasoning)) {
        reasoning = {};
        if (Array.isArray(item.reasoning.levels)) reasoning.levels = item.reasoning.levels.map((level) => {
          if (typeof level === 'string') return text(level, 'native_reasoning_level');
          if (!plain(level)) fail('native_catalog_unavailable');
          return { value: text(level.value, 'native_reasoning_level'),
            ...(typeof level.label === 'string' ? { label: level.label } : {}) };
        });
        if (typeof item.reasoning.defaultLevel === 'string') reasoning.defaultLevel = item.reasoning.defaultLevel;
      }
      return { ref: ref(item.ref), ...(typeof item.label === 'string' ? { label: item.label } : {}),
        ...(typeof item.providerLabel === 'string' ? { providerLabel: item.providerLabel } : {}),
        ...(item.disabledReason ? { disabledReason: String(item.disabledReason) } : {}),
        ...(reasoning ? { reasoning } : {}) };
    });
  }
  async function catalog(workspace, message, request) {
    // Et's public catalogue is a no-input deferred session snapshot. Its project
    // reservation is real and can remain in the index even after deferred close.
    method('closeSession');
    let probe;
    let result;
    try {
      request.phase = 'catalog_create_dispatched'; save();
      const snapshot = await method('createSession')({ workspacePath: workspace, projectId: null,
        mode: 'plan', persistence: 'deferred', titleGenerationEnabled: false });
      probe = { sessionId: text(snapshot?.session?.sessionId, 'native_session_id'), closed: false };
      request.catalogSessionId = probe.sessionId; request.phase = 'catalog_created'; save();
      const identity = await runtime(workspace);
      probe = rememberSession(snapshot, workspace, identity, 'catalog', request.requestId);
      const available = availableModels(snapshot);
      let selectedSnapshot = snapshot;
      let thoughtLevels = levelsOf(snapshot);
      let thoughtLevelsSource = 'native-snapshot';
      let requestedModel;
      if (message.model !== undefined) {
        const model = ref(message.model);
        requestedModel = model;
        const item = available.find((entry) => sameModel(entry.ref, model) && !entry.disabledReason);
        if (!item) fail('model_not_available');
        if (Array.isArray(item.reasoning?.levels)) {
          thoughtLevels = item.reasoning.levels.map((level) => typeof level === 'string' ? level : level.value);
          thoughtLevelsSource = 'native-model-row';
        } else if (!sameModel(selected(snapshot), model)) {
          if (!own(message, 'thought_level') || message.thought_level === null) {
            // No metadata for this model and no explicit effort: do not try a
            // ref-only switch, which some native models reject as incomplete.
            thoughtLevels = null;
            thoughtLevelsSource = 'requires-selection';
          } else {
            if (message.thought_level !== null && typeof message.thought_level !== 'string') fail('thought_level_required');
            const selection = { ...model, options: { reasoningLevel: message.thought_level } };
            selectedSnapshot = await method('setModel')({ ...targetOf(workspace, probe.sessionId), model: selection, persistAsWorkspaceLastUsed: false });
            if (!sameModel(selected(selectedSnapshot), model)) fail('native_model_mismatch');
            if (effortOf(selectedSnapshot) !== message.thought_level) fail('native_thought_mismatch');
            thoughtLevels = levelsOf(selectedSnapshot);
            thoughtLevelsSource = 'native-selected-snapshot';
          }
        }
      }
      if (own(message, 'thought_level')) {
        if (message.thought_level !== null && typeof message.thought_level !== 'string') fail('thought_level_required');
        if (thoughtLevels === null && message.op === 'native_create') fail('requires_selection', 'Native effort metadata is unavailable; select an explicit native reasoning level');
        if (thoughtLevels !== null && (message.thought_level === null ? thoughtLevels.length > 0 : !thoughtLevels.includes(message.thought_level))) fail('thought_level_not_available');
      }
      result = { available, selected: selected(selectedSnapshot), ...(requestedModel ? { requestedModel } : {}),
        thoughtLevels, thoughtLevelsSource,
        selectionStatus: thoughtLevels === null || (!own(message, 'thought_level') && thoughtLevels.length > 0)
          ? 'requires-selection' : own(message, 'thought_level') ? 'validated' : 'not-required',
        catalogueProbe: { sessionId: probe.sessionId, catalogOnly: true, persistence: 'deferred', mode: 'plan' } };
      return result;
    } finally {
      if (probe) {
        try {
          const closed = await method('closeSession')({ ...targetOf(workspace, probe.sessionId), expectedPersistence: 'deferred' });
          probe.closed = closed === true;
          if (!probe.closed) probe.closeStatus = 'unconfirmed';
        } catch (error) { probe.closeStatus = 'unknown'; }
        request.catalogClose = probe.closed ? 'confirmed' : probe.closeStatus || 'unconfirmed';
        if (result) Object.assign(result.catalogueProbe, { closed: probe.closed, closeStatus: request.catalogClose });
        save();
      }
    }
  }
  async function snapshot(workspace, sid) {
    const result = await method('readSession')(targetOf(workspace, sid));
    if (!plain(result) || result.session?.sessionId !== sid) fail('native_snapshot_shape_invalid');
    return result;
  }
  const FULL_ACCESS_OPTION_ID = 'fullAccess';
  async function completeInteractions(workspace, sid, current = null) {
    if (typeof readInteractions !== 'function') fail('unsupported', 'native interactions require a host update');
    // A subscription must not cold-resume an unavailable session just to read a
    // question. Confirm the owned runtime is readable before the SDK snapshot.
    if (!current) await snapshot(workspace, sid);
    const result = await readInteractions(targetOf(workspace, sid));
    if (!plain(result) || result.sessionId !== sid || !Array.isArray(result.interactions)) {
      fail('native_interactions_shape_invalid');
    }
    const interactions = result.interactions.map(row => {
      if (row?.kind === 'pluginInstall') {
        // These two IDs are the fixed actions used by Desktop's plugin card;
        // keep their provenance explicit instead of inventing permission modes.
        return { ...row, payload: { ...row.payload, options: [
          { optionId: 'skip', label: 'Skip', kind: 'reject_once', source: 'native_plugin_ui' },
          { optionId: 'install', label: row.payload.action === 'enable' ? 'Enable' : 'Install and enable',
            kind: 'allow_once', source: 'native_plugin_ui' },
        ] } };
      }
      if (row?.kind === 'workspaceHookReview') {
        return { ...row, payload: { ...row.payload, hookActions: [
          'trust_selected', 'toggle', 'revoke',
        ], hookActionsSource: 'native_workspace_hook_controls' } };
      }
      if (row?.kind !== 'permission' || !['CreateWorkflow', 'AmendWorkflow'].includes(row.payload?.toolName ?? row.payload?.name)) return row;
      // Same fixed UI action as Desktop 3.18.2's workflow permission form.
      // It is deliberately separate from the raw protocol option rows.
      return { ...row, payload: { ...row.payload, workflowRevisionOption: {
        optionId: 'workflowRefine', kind: 'custom', label: 'Request changes',
        requiresFreeText: true, source: 'native_workflow_ui',
      } } };
    });
    return { ...result, interactions, optionsComplete: true };
  }
  function pendingInteraction(current, interactionId) {
    // 3.18.2 keeps real interactions in pendingInteractions and keeps
    // AskUserQuestion in pendingPermissions. Only an actual pending row may be
    // answered; option membership and permission semantics are inspected there.
    const containers = [current, current?.projection, current?.runtime,
      current?.conversation, current?.state];
    for (const container of containers) {
      if (!plain(container)) continue;
      for (const item of Array.isArray(container.pendingInteractions) ? container.pendingInteractions : []) {
        if (!plain(item)) continue;
        const id = item.interactionId ?? item.id;
        if (id !== interactionId) continue;
        const payload = plain(item.payload) ? item.payload : {};
        const kind = item.kind ?? payload.kind;
        if (kind === 'permission' || kind === 'userInput') return { kind, payload };
        return null;
      }
      for (const item of Array.isArray(container.pendingPermissions) ? container.pendingPermissions : []) {
        if (!plain(item)) continue;
        const id = item.requestId ?? item.toolCallId;
        if (id !== interactionId) continue;
        const toolName = typeof item.toolName === 'string' ? item.toolName : '';
        const questions = plain(item.input) && Array.isArray(item.input.questions)
          ? item.input.questions : null;
        if (toolName === 'AskUserQuestion' || questions) {
          return { kind: 'userInput', payload: { questions: questions ?? [] } };
        }
        return { kind: 'permission', payload: {
          options: Array.isArray(item.options) ? item.options : [],
          ...(plain(item.fullAccessOption) ? { fullAccessOption: item.fullAccessOption } : {}),
        } };
      }
    }
    return null;
  }
  async function setPermission(workspace, sid, current, wanted) {
    const currentMode = modeOf(current);
    if (current.session?.status === 'running' && currentMode !== wanted) fail('permission_change_while_running');
    if (currentMode === wanted) return current;
    const changed = await method('setMode')({ ...targetOf(workspace, sid), mode: wanted });
    if (modeOf(changed) !== wanted) fail('native_permission_mismatch');
    return changed;
  }
  function ack(result, payload) {
    if (!['accepted', 'duplicate', 'noop'].includes(result?.status)) {
      // Only diagnostic scalar fields cross this boundary. Never forward result
      // payloads, requestAuth, headers, stack traces or arbitrary error objects.
      const nativeAck = {};
      for (const key of ['commandId', 'status', 'reasonCode']) {
        if (typeof result?.[key] === 'string') nativeAck[key] = result[key].slice(0, 200);
      }
      const message = diagnostic(result?.message, payload);
      if (message !== undefined) nativeAck.message = message;
      const error = new Error(message || nativeAck.reasonCode || nativeAck.status || 'native command rejected');
      error.code = 'native_command_rejected'; error.nativeAck = nativeAck;
      throw error;
    }
    return result;
  }
  async function command(message, workspace, sid, type, payload, request) {
    if (typeof makeEnvelope !== 'function') fail('unsupported', 'native envelope helper');
    const envelope = makeEnvelope({ type, sessionId: sid, payload, commandId: request.commandId, clientId: 'desktop-native-shim-v1' });
    request.phase = 'command_dispatched'; save();
    const result = await method('sendConversationCommandV4')({ workspacePath: workspace, envelope });
    request.phase = 'command_acknowledged'; save();
    return ack(result, payload);
  }
  function durableInputObserved(current) {
    // A native message requires an existing session FK; TurnStarted is also
    // emitted only after Core.ensureSessionPersisted in this engine version.
    return (Array.isArray(current.messages) && current.messages.length > 0) ||
      (Number.isInteger(current.projection?.turnCount) && current.projection.turnCount > 0);
  }
  function emptyIdleSession(current) {
    return current.session?.status === 'idle' && current.projection?.status === 'idle' &&
      current.projection.turnCount === 0 && Array.isArray(current.messages) && current.messages.length === 0 &&
      !current.runtime?.activeTurnId && !current.projection.currentTurnId &&
      Array.isArray(current.runtime?.pendingRequestIds) && current.runtime.pendingRequestIds.length === 0 &&
      Array.isArray(current.projection.pendingPermissions) && current.projection.pendingPermissions.length === 0 &&
      Array.isArray(current.projection.backgroundJobs) && current.projection.backgroundJobs.length === 0;
  }
  async function sendInitialInput(workspace, sid, current, modelSelection, message, record, request) {
    if (record.creationMethod !== 'legacy-session-create' || record.firstInput?.state !== 'not-sent') fail('first_input_state_unverified');
    if (!emptyIdleSession(current)) fail('first_input_requires_empty_idle_session');
    const send = method('sendPrompt');
    record.firstInput = { state: 'dispatched', requestId: request.requestId, inputId: request.commandId };
    request.phase = 'legacy_first_input_dispatched'; save();
    let result;
    try {
      result = await send({ workspacePath: workspace, sessionId: sid, content: message.text,
        inputId: request.commandId, queryId: request.commandId, modelSelection,
        ...(Number.isInteger(current.runtime?.stateRevision) ? { expectedRevision: current.runtime.stateRevision } : {}) });
    } catch (error) {
      if (error.name === 'ZCodeProtocolClientError' && Number.isInteger(error.code) && error.code < 0) {
        // A JSON-RPC rejection is a known response. A timeout/closed transport
        // is not proof that the native first input was rejected.
        record.firstInput = { state: 'not-sent', lastRejectedRequestId: request.requestId };
        request.phase = 'legacy_first_input_rejected';
      } else record.firstInput.state = 'unknown';
      save(); throw error;
    }
    if (result?.accepted !== true || result.sessionId !== sid) {
      record.firstInput.state = 'unknown'; save();
      fail('native_initial_send_ack_unconfirmed');
    }
    record.firstInput.state = 'accepted';
    request.phase = 'legacy_first_input_acknowledged'; save();
    return { status: 'accepted', sessionId: sid, commandId: request.commandId,
      admissionProtocol: 'session/send', nativeAdmission: { accepted: true, sessionId: result.sessionId,
        ...(Number.isInteger(result.stateRevision) ? { stateRevision: result.stateRevision } : {}) } };
  }
  async function notifyProjectOnce(workspace, sid, record) {
    // At most one attempt per owned session. The marker is persisted before the
    // attempt, so a crash, a failed import or missing facts never become a retry
    // loop. Create-only sessions never reach here: no durable CLI fact exists yet.
    if (record.projectNotification) return record.projectNotification;
    record.projectNotification = { state: 'attempted' }; save();
    const s = typeof syncer === 'function' ? syncer() : null;
    if (!s || typeof s.syncSessionFacts !== 'function') {
      record.projectNotification = { state: 'unavailable' }; save();
      return record.projectNotification;
    }
    try {
      const imported = await s.syncSessionFacts({ workspacePath: workspace, sessionId: sid });
      const facts = imported && Array.isArray(imported.facts) ? imported.facts : [];
      const durable = facts.some((fact) => plain(fact) && fact.sessionId === sid && fact.exists === true);
      if (!imported || imported.missing === true || !durable) {
        record.projectNotification = { state: 'facts_unavailable' }; save();
        return record.projectNotification;
      }
      if (imported.changed !== true && typeof s.emitWorkspaceTaskListChanged === 'function') {
        // The facts import emits its own list event only for a changed row.
        // Emit the single event here instead so an accepted first turn always
        // refreshes the list exactly once.
        s.emitWorkspaceTaskListChanged({ workspacePath: workspace, taskId: sid }, undefined, 'native_project_facts');
      }
      record.projectNotification = { state: 'facts_imported', changed: imported.changed === true }; save();
      return record.projectNotification;
    } catch (error) {
      record.projectNotification = { state: 'failed',
        error: typeof error?.code === 'string' ? error.code : 'sync_failed' }; save();
      return record.projectNotification;
    }
  }
  async function workflowRuns(workspace, sid, limit) {
    return method('conversationWorkflowRunsV4')({ ...targetOf(workspace, sid), limit: Math.min(page(limit, 16), 64) });
  }
  async function nativeChildren(workspace, sid, limit = 100) {
    const result = await method('listSessionSubagents')({ ...targetOf(workspace, sid), endedLimit: Math.min(page(limit), 100) });
    if (!plain(result) || !Array.isArray(result.childSessionIds) ||
      !result.childSessionIds.every((value) => typeof value === 'string') ||
      !Array.isArray(result.running) || !plain(result.ended) || !Array.isArray(result.ended.items)) fail('native_children_shape_invalid');
    return result;
  }
  function verifyChildIdentity(child, workspace, parentId, childId, expectedKind = 'subagent_child') {
    if (!plain(child) || child.sessionId !== childId || child.parentSessionId !== parentId) fail('child_not_verified_for_parent');
    if (child.sessionKind !== expectedKind) fail('unsupported', 'The native child kind does not match the verified relationship');
    const childWorkspace = child.workspace;
    if (!plain(childWorkspace) || resolveWorkspace(childWorkspace.workspacePath) !== workspace ||
      childWorkspace.workspaceIdentity?.trim() || childWorkspace.remoteSessionId) fail('child_workspace_mismatch');
  }
  async function readyWorkflowParent(workspace, parentId) {
    await owned(workspace, parentId);
    const lifecycle = await method('getSessionLifecycle')(targetOf(workspace, parentId));
    if (!plain(lifecycle) || lifecycle.sessionId !== parentId || lifecycle.state !== 'ready') fail('unsupported', 'DWF actor verification requires an already ready native parent; no runtime restoration is attempted');
  }
  async function verifiedWorkflowActor(workspace, parentId, childId, runId) {
    text(runId, 'run_id');
    await readyWorkflowParent(workspace, parentId);
    await verifiedRun(workspace, parentId, runId);
    let after = 0, launchedForParent = false, actorMatches = false;
    // Native Fqe/B7n/rc/h0: exact actor identity mapping, never a name/prefix.
    const segment = (value) => value.replace(/_/g, '__').replace(/[^A-Za-z0-9.-]/g, '_');
    for (let pageNumber = 0; pageNumber < 8; pageNumber += 1) {
      await readyWorkflowParent(workspace, parentId);
      const pageResult = await method('conversationWorkflowRunEventsV4')({ ...targetOf(workspace, parentId), runId, afterSequence: after, limit: 100 });
      if (!plain(pageResult) || !Array.isArray(pageResult.events) || typeof pageResult.hasMore !== 'boolean') fail('native_actor_journal_shape_invalid');
      for (const event of pageResult.events) {
        if (!plain(event) || !Number.isSafeInteger(event.sequence) || event.sequence <= after ||
          typeof event.type !== 'string' || !plain(event.payload) ||
          (event.runId !== undefined && event.runId !== runId)) fail('native_actor_journal_shape_invalid');
        after = event.sequence;
        if (event.type === 'run-launched') {
          if (event.truncated === true || event.payload.parentSessionId !== parentId) fail('child_not_verified_for_parent');
          launchedForParent = true;
        }
        if (event.type === 'actor-created') {
          const actor = event.payload.actor;
          if (event.truncated === true || !plain(actor) || Object.keys(actor).some((key) => !['siteId', 'ordinal'].includes(key)) ||
            typeof actor.siteId !== 'string' || !actor.siteId.trim() || actor.siteId.length > 200 ||
            !Number.isSafeInteger(actor.ordinal) || actor.ordinal < 1) fail('native_actor_event_invalid');
          const candidate = 'sess_dwf-' + segment(runId) + '-' + segment(`${actor.siteId}@${actor.ordinal}`);
          if (candidate === childId) actorMatches = true;
        }
      }
      await owned(workspace, parentId);
      if (launchedForParent && actorMatches) return;
      if (!pageResult.hasMore) break;
      if (pageResult.events.length === 0) fail('native_actor_journal_shape_invalid');
    }
    fail('unsupported', 'No complete native parent/run/actor proof within the bounded journal pages');
  }
  async function verifiedChild(workspace, parentId, childId, runId) {
    text(childId, 'child_session_id');
    if (childId === parentId) fail('child_not_verified_for_parent');
    const children = await nativeChildren(workspace, parentId);
    // Native 3.18.2 FFt filters these IDs to persisted taskType=subagent_child
    // records derived from the parent's actual tool/event relationships.
    let kind = 'subagent_child';
    if (!children.childSessionIds.includes(childId)) {
      if (runId === undefined) fail('unsupported', 'No verified native ordinary-subagent relationship; unlisted DWF actors require native run/actor proof');
      await verifiedWorkflowActor(workspace, parentId, childId, runId);
      kind = 'workflow_child';
    }
    await owned(workspace, parentId);
    const summaries = await method('listSessions')({ workspacePath: workspace, runtimePolicy: 'existing-only', sessionIds: [childId] });
    if (!Array.isArray(summaries)) fail('native_child_identity_shape_invalid');
    const matches = summaries.filter((item) => item?.sessionId === childId);
    if (matches.length !== 1) fail('child_not_verified_for_parent');
    verifyChildIdentity(matches[0], workspace, parentId, childId, kind);
    await owned(workspace, parentId);
    return { childId, kind };
  }
  async function verifiedRun(workspace, sid, runId) {
    text(runId, 'run_id');
    const result = await workflowRuns(workspace, sid, 64);
    const rows = Array.isArray(result) ? result : result?.runs;
    if (!Array.isArray(rows) || !rows.some((row) => row.runId === runId)) fail('run_not_verified_for_session');
  }
  async function indexFacts(workspace, sid) {
    // These are native index facts, not a claim that a GUI panel was inspected.
    try { return await method('reconcileSessionsIndexV4')({ workspacePath: workspace, sessionIds: [sid] }); }
    catch (error) { return { status: 'unavailable', error: error.code || 'native_index_unavailable' }; }
  }
  async function execute(message, workspace, access, request) {
    const op = message.op;
    if (op === 'native_model_catalog') return catalog(workspace, message, request);
    if (op === 'native_create') {
      if (message.session_id !== undefined || message.firstInput !== undefined || message.text !== undefined) fail('create_accepts_no_input_or_session_id');
      if (message.project_id !== undefined && message.project_id !== null) text(message.project_id, 'project_id');
      const { model, thought } = requestedSelection(message);
      // Resolve the target project before any catalogue probe or session create.
      // An ambiguous workspace or an invalid explicit id must not create anything.
      const project = await resolveCreationProject(workspace, message.project_id);
      const validated = await catalog(workspace, message, request);
      if (thought === null ? validated.thoughtLevels.length > 0 : !validated.thoughtLevels.includes(thought)) fail('thought_level_not_available');
      const selection = { ...model, ...(thought !== null ? { options: { reasoningLevel: thought } } : {}) };
      syncer()?.ensureWorkspaceSubscription?.({ workspacePath: workspace });
      request.phase = 'create_dispatched'; save();
      const created = await method('createSession')({ workspacePath: workspace, projectId: project.projectId,
        mode: access.mode, model: selection, ...(thought !== null ? { thoughtLevel: thought } : {}),
        persistence: 'immediate', titleGenerationEnabled: false });
      request.sessionId = text(created?.session?.sessionId, 'native_session_id'); request.phase = 'created_unverified'; save();
      const identity = await runtime(workspace);
      const record = rememberSession(created, workspace, identity, 'session', request.requestId);
      checkSelection(created, model, thought, access.mode);
      record.model = model; record.thoughtLevel = thought; record.mode = access.mode; record.verified = true;
      record.creationMethod = 'legacy-session-create'; record.firstInput = { state: 'not-sent' }; save();
      return { sessionId: record.sessionId, ownedSID: record.sessionId, workspace, model: selected(created),
        thoughtLevel: effortOf(created), mode: modeOf(created), runtime: identity, snapshot: created,
        projectAssignment: projectAssignment(project),
        desktopIndexFacts: await indexFacts(workspace, record.sessionId), guiVerified: false,
        catalogueProbe: validated.catalogueProbe };
    }
    const sid = message.session_id;
    const record = await owned(workspace, sid);
    if (!record.verified) fail('session_setup_unverified');
    const target = targetOf(workspace, sid);
    if (op === 'native_child_messages' || op === 'native_child_snapshot') {
      const { childId, kind } = await verifiedChild(workspace, sid, message.child_session_id, message.run_id);
      let result;
      if (op === 'native_child_snapshot') {
        result = await snapshot(workspace, childId);
        verifyChildIdentity(result.session, workspace, sid, childId, kind);
      } else {
        result = await method('readSessionMessages')({ ...targetOf(workspace, childId),
          afterMessageId: message.after_message_id, limit: page(message.limit), direction: 'forward' });
        if (!Array.isArray(result)) fail('native_messages_shape_invalid');
      }
      await owned(workspace, sid);
      return result;
    }
    if (op === 'native_snapshot') return snapshot(workspace, sid);
    if (op === 'native_interactions') return completeInteractions(workspace, sid);
    if (op === 'native_messages') {
      // Et already unwraps the wire {messages}; do not add another wrapper.
      const result = await method('readSessionMessages')({ ...target,
        afterMessageId: message.after_message_id, limit: page(message.limit), direction: 'forward' });
      if (!Array.isArray(result)) fail('native_messages_shape_invalid');
      return result;
    }
    if (op === 'native_events') {
      const result = await method('readSessionEvents')({ ...target, afterSeq: sequence(message.after_seq), limit: page(message.limit) });
      if (!Array.isArray(result)) fail('native_events_shape_invalid');
      return result;
    }
    if (op === 'native_lifecycle') {
      const result = await method('getSessionLifecycle')(target);
      if (result === null) fail('unsupported', 'native lifecycle observation is unavailable');
      if (!plain(result) || result.sessionId !== sid || typeof result.state !== 'string') fail('native_lifecycle_shape_invalid');
      return result;
    }
    if (op === 'native_children') return { verifiedParentSessionId: sid, writeOwnershipGranted: false,
      native: await nativeChildren(workspace, sid, message.limit) };
    if (op === 'native_workflows') return workflowRuns(workspace, sid, message.limit);
    if (op === 'native_workflow_events' || op === 'native_topology') {
      await verifiedRun(workspace, sid, message.run_id);
      const events = await method('conversationWorkflowRunEventsV4')({ ...target, runId: message.run_id,
        afterSequence: sequence(message.after_sequence), limit: page(message.limit) });
      if (op === 'native_workflow_events') return events;
      return { runId: message.run_id, verifiedParentSessionId: sid, events,
        children: await nativeChildren(workspace, sid),
        representation: 'native-journal-and-subagent-facts' };
    }
    if (op === 'native_send') {
      text(message.text, 'text', 50000);
      if (['dispatched', 'unknown'].includes(record.firstInput?.state)) fail('first_input_outcome_unknown');
      let current = await snapshot(workspace, sid);
      checkSelection(current, record.model, record.thoughtLevel);
      current = await setPermission(workspace, sid, current, access.mode);
      checkSelection(current, record.model, record.thoughtLevel, access.mode);
      // 3.18.2 Wc explicitly allows options.reasoningLevel. Some snapshots put
      // effort only in settings.thoughtLevel.current; rebuild the exact Wc shape.
      // modelExecution is separate execution/auth material, not an effort field.
      const modelSelection = { ...ref(selected(current)), ...(record.thoughtLevel !== null
        ? { options: { reasoningLevel: record.thoughtLevel } } : {}) };
      let result;
      let acceptedFirstInput = false;
      if (durableInputObserved(current)) {
        if (record.firstInput) record.firstInput.state = 'durable-observed';
        result = await command(message, workspace, sid, 'sendText', { text: message.text,
          requestedDelivery: 'startNow', mode: access.mode, modelSelection }, request);
      } else {
        if (record.firstInput?.state === 'accepted') fail('first_input_not_yet_observed');
        result = await sendInitialInput(workspace, sid, current, modelSelection, message, record, request);
        acceptedFirstInput = true;
      }
      record.mode = access.mode; save();
      // One list refresh at most, only after the first input was accepted and the
      // session therefore has durable CLI facts. Notification problems are
      // diagnostics, never a reason to turn an accepted send into a failure.
      let listNotification;
      if (acceptedFirstInput && !record.projectNotification) {
        try { listNotification = await notifyProjectOnce(workspace, sid, record); }
        catch { listNotification = { state: 'failed', error: 'notification_unavailable' }; }
      }
      return { ...result, sessionId: sid, model: selected(current), thoughtLevel: effortOf(current), mode: access.mode,
        ...(listNotification ? { listNotification } : {}) };
    }
    if (op === 'native_resolve') {
      const interactionId = text(message.interaction_id, 'interaction_id');
      let answer = message.answer;
      if (!plain(answer) || Object.keys(answer).some((key) => !['optionId', 'freeText', 'action', 'content'].includes(key))) fail('invalid_answer');
      if (!Object.keys(answer).length) fail('invalid_answer');
      if (answer.optionId !== undefined) text(answer.optionId, 'option_id');
      if (answer.freeText !== undefined && (typeof answer.freeText !== 'string' || answer.freeText.length > 50000)) fail('invalid_answer');
      if (answer.action !== undefined && !['accept', 'decline', 'cancel'].includes(answer.action)) fail('invalid_answer');
      if (answer.content !== undefined && !plain(answer.content)) fail('invalid_answer');
      if (message.user_approved !== undefined && typeof message.user_approved !== 'boolean') fail('invalid_user_approved');
      const declined = ['decline', 'cancel'].includes(answer.action);
      const current = await snapshot(workspace, sid);
      const interaction = typeof readInteractions === 'function'
        ? (await completeInteractions(workspace, sid, current)).interactions.find(row =>
          (row?.interactionId ?? row?.id) === interactionId)
        : pendingInteraction(current, interactionId);
      if (!interaction) fail('request_not_pending', 'interaction_id is not pending on this owned session');
      if (interaction.kind === 'workspaceHookReview') {
        const payload = interaction.payload, decision = answer.content;
        if (answer.optionId !== undefined || answer.freeText !== undefined || declined || !plain(decision)) fail('invalid_hook_response');
        if (payload.sessionId !== sid || payload.interactionId !== interactionId) fail('native_interactions_shape_invalid');
        const target = {};
        for (const key of ['sessionId', 'taskId', 'runId', 'workspaceIdentity', 'bundleDigest', 'reviewFlowId', 'generation', 'interactionId']) {
          if (payload[key] === undefined) fail('native_interactions_shape_invalid');
          target[key] = payload[key];
        }
        if (payload.remoteSessionId) fail('unsupported', 'remote workspace Hook reviews require their native host');
        const items = Array.isArray(payload.items) ? payload.items : [];
        let type, control, lowersTrust = false;
        if (decision.action === 'toggle') {
          if (Object.keys(decision).some(key => !['action', 'reviewItemId', 'enabled'].includes(key))) fail('invalid_hook_response');
          const item = items.find(row => row.reviewItemId === decision.reviewItemId);
          if (!item || item.editable !== true || typeof decision.enabled !== 'boolean') fail('invalid_hook_response');
          type = 'toggleWorkspaceHookReviewItem';
          control = { reviewItemId: decision.reviewItemId, enabled: decision.enabled };
          lowersTrust = decision.enabled === false;
        } else if (['trust_selected', 'revoke'].includes(decision.action)) {
          if (Object.keys(decision).some(key => !['action', 'reviewItemIds'].includes(key))) fail('invalid_hook_response');
          const ids = decision.reviewItemIds;
          if (!Array.isArray(ids) || !ids.length || new Set(ids).size !== ids.length
              || ids.some(id => !items.some(row => row.reviewItemId === id))) fail('invalid_hook_response');
          type = decision.action === 'revoke' ? 'revokeWorkspaceHookTrust' : 'respondWorkspaceHookReview';
          control = decision.action === 'revoke' ? { reviewItemIds: ids }
            : { decision: { action: 'trust_selected', reviewItemIds: ids } };
          lowersTrust = decision.action === 'revoke';
        } else fail('invalid_hook_response');
        if (!lowersTrust) {
          if (access.mode === 'plan') fail('read_only_resolution_not_allowed');
          if (access.caller !== 'full_access' && message.user_approved !== true) fail('user_approved_required');
        }
        return command(message, workspace, sid, type, { ...target, ...control }, request);
      }
      if (!['permission', 'userInput', 'pluginInstall'].includes(interaction.kind)) fail('unsupported_interaction_kind');
      if (interaction.kind === 'pluginInstall') {
        if (declined) answer = { optionId: 'skip' };
        if (!['install', 'skip'].includes(answer.optionId) || Object.keys(answer).some(key => !['optionId', 'action'].includes(key))) fail('invalid_plugin_response');
        if (answer.optionId === 'install') {
          if (access.mode === 'plan') fail('read_only_resolution_not_allowed');
          if (access.caller !== 'full_access' && message.user_approved !== true) fail('user_approved_required');
        }
        return command(message, workspace, sid, 'resolveInteraction', { interactionId, answer: { optionId: answer.optionId } }, request);
      }
      if (interaction.kind === 'permission' && !declined && answer.freeText !== undefined
          && answer.optionId === undefined && interaction.payload.workflowRevisionOption) {
        answer = { ...answer, optionId: interaction.payload.workflowRevisionOption.optionId };
      }
      let denies = declined;
      let sessionConsent = false;
      if (interaction.kind === 'permission') {
        let selected = null;
        if (declined && answer.freeText !== undefined && interaction.payload.freeText !== true) {
          fail('invalid_answer', 'this permission does not accept a refusal reason');
        }
        if (!declined) {
          if (answer.optionId === undefined) fail('answer_requires_option', 'permission answers require a real optionId');
          if (answer.content !== undefined && interaction.payload.freeText !== true && !interaction.payload.workflowRevisionOption) {
            fail('invalid_answer', 'this permission does not accept a structured revision');
          }
          const options = interaction.payload.options.filter(plain);
          if (plain(interaction.payload.workflowRevisionOption)) {
            const revision = interaction.payload.workflowRevisionOption;
            options.unshift({ ...options.find(row => row.optionId === revision.optionId), ...revision });
          }
          const fullAccess = plain(interaction.payload.fullAccessOption)
            ? interaction.payload.fullAccessOption : interaction.payload.modeSwitch === 'fullAccessOnly'
              ? { optionId: FULL_ACCESS_OPTION_ID, kind: 'custom', label: 'Full access' } : null;
          selected = options.find((row) => row.optionId === answer.optionId)
            || (fullAccess && fullAccess.optionId === answer.optionId ? fullAccess : null);
          if (!selected) fail('option_not_in_request', 'answer.optionId is not one of the pending permission options');
          if (selected.requiresFreeText && !answer.freeText?.trim()) fail('invalid_answer', 'a workflow revision needs feedback');
          if (answer.freeText !== undefined && !selected.requiresFreeText && interaction.payload.freeText !== true) {
            fail('invalid_answer', 'this permission does not accept free text');
          }
          sessionConsent = selected.kind === 'allow_session' || selected.optionId === 'allowSession';
          const decision = plain(selected.response) ? selected.response.decision : undefined;
          denies = /^(deny|reject)/i.test(selected.kind ?? '') || decision === 'deny';
          if (access.mode === 'plan' && !denies) fail('read_only_resolution_not_allowed');
          if (selected.optionId === FULL_ACCESS_OPTION_ID) {
            // 3.18.2 maps this option to yolo. A weaker caller grant cannot
            // cross it, even with an explicit answer.
            if (access.caller !== 'full_access') fail('permission_escalation_denied', 'fullAccess exceeds the current caller grant');
          } else if (!denies && access.caller !== 'full_access' && message.user_approved !== true) {
            // Any non-deny choice (allowOnce/allowAlways/custom/unknown) is
            // gated by the current caller grant and explicit user approval.
            fail('user_approved_required', 'allow-like permission choice requires explicit user approval');
          }
        }
      } else if (answer.optionId !== undefined) {
        if (!(interaction.payload.options ?? []).some(option => option?.optionId === answer.optionId)) {
          fail('option_not_in_request', 'user input option is not in the actual question');
        }
      }
      if (access.mode === 'plan' && !denies) fail('read_only_resolution_not_allowed');
      // The SDK's real fullAccess choice performs its own session transition;
      // do not try a separate mode setter while its workflow prompt is pending.
      const sdkTransition = interaction.kind === 'permission' && (answer.optionId === FULL_ACCESS_OPTION_ID
        || access.caller === 'full_access' && (sessionConsent || answer.freeText !== undefined));
      if (!denies && !sdkTransition) {
        await setPermission(workspace, sid, current, access.mode);
      }
      // A refusal cannot smuggle an option that the engine might treat as an
      // approval or a mode switch.
      const payloadAnswer = interaction.kind === 'permission' && declined
        ? { action: answer.action, ...(answer.freeText !== undefined ? { freeText: answer.freeText } : {}) } : answer;
      return command(message, workspace, sid, 'resolveInteraction', { interactionId, answer: payloadAnswer }, request);
    }
    if (op === 'native_cancel') {
      if (message.work_id !== undefined) fail('unsupported', 'background task cancellation is not exposed by Et');
      return command(message, workspace, sid, 'stop', {}, request);
    }
    fail('unsupported', op);
  }
  const operations = new Set(['native_create', 'native_send', 'native_snapshot', 'native_messages', 'native_events',
    'native_resolve', 'native_workflows', 'native_workflow_events', 'native_cancel', 'native_pending', 'native_interactions',
    'native_model_catalog', 'native_children', 'native_topology', 'native_lifecycle',
    'native_child_messages', 'native_child_snapshot']);
  const mutation = new Set(['native_create', 'native_send', 'native_resolve', 'native_cancel', 'native_model_catalog']);
  async function dispatch(message) {
    if (!plain(message) || !operations.has(message.op)) fail('unsupported');
    const workspace = resolveWorkspace(message.workspace);
    if (!workspace) fail('workspace_not_allowed');
    const access = permission(message);
    loadState();
    if (message.op === 'native_pending') {
      if (!message.request_id) fail('unsupported', 'pending interactions are available in the native snapshot; request_id is required here');
      const request = own(state.requests, message.request_id) ? state.requests[message.request_id] : null;
      if (!request || request.workspace !== workspace) fail('request_not_found');
      return { request: structuredClone(request), automaticRetry: false };
    }
    if (!mutation.has(message.op)) return execute(message, workspace, access, null);
    const requestId = text(message.request_id, 'request_id', 128);
    if (!/^[A-Za-z0-9_-]{8,128}$/.test(requestId)) fail('invalid_request_id');
    const fingerprint = hash(message);
    if (own(state.requests, requestId)) {
      const previous = state.requests[requestId];
      if (previous.fingerprint !== fingerprint || previous.workspace !== workspace) fail('request_id_conflict');
      return { replay: true, request: structuredClone(previous), automaticRetry: false };
    }
    const request = { requestId, op: message.op, workspace, callerPermissionMode: access.caller,
      fingerprint, commandId: __zcodeNativeCrypto.randomUUID(), status: 'pending', phase: 'validated', at: now(),
      ...(typeof message.session_id === 'string' ? { sessionId: message.session_id } : {}) };
    put(state.requests, requestId, request); save();
    try {
      const result = await execute(message, workspace, access, request);
      request.status = 'confirmed'; request.completedAt = now();
      if (result?.sessionId) request.sessionId = result.sessionId;
      // No prompt, answers, transcript or provider configuration are persisted.
      save();
      return result;
    } catch (error) {
      const dispatched = ['create_dispatched', 'catalog_create_dispatched', 'command_dispatched', 'legacy_first_input_dispatched'].includes(request.phase);
      request.status = dispatched ? 'unknown' : 'failed'; request.error = error.code || 'native_operation_failed'; save();
      error.request = structuredClone(request);
      throw error;
    }
  }
  return Object.freeze({
    call(message) {
      const task = queue.then(async () => {
        try { return { ok: true, source, result: await dispatch(message) }; }
        catch (error) {
          const nativeError = nativeErrorDetails(error, message);
          return { ok: false, source, error: error.code || 'native_operation_failed',
            detail: diagnostic(String(error.message || error), { text: message?.text, answer: message?.answer }),
            ...(nativeError ? { nativeError } : {}),
            ...(error.nativeAck ? { nativeAck: error.nativeAck } : {}), ...(error.request ? { request: error.request } : {}) };
        }
      });
      queue = task.then(() => undefined, () => undefined);
      return task;
    },
  });
}
