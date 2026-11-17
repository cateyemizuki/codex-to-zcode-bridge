/* ZCODE_DESKTOP_SYNC_SHIM_V1 */
/* ZCode Desktop 3.18.2 optional in-process sync shim (experimental).
 *
 * Appended to a *copy* of out/host/index.js by scripts/desktop_sync_shim.py.
 * By default it exposes two loopback operations for allowed workspaces:
 *   probe -> readiness + recently observed workspace_task_list_changed events
 *   sync  -> ask the host's own engine to re-read persisted CLI session facts,
 *            apply them through TaskIndexRepo, and emit the same in-host
 *            workspace_task_list_changed event the normal mirror emits.
 * Separately opted-in native_* operations dispatch new Desktop-owned sessions
 * through the host's public native service; see native_transport.mjs.
 *
 * It adds no eval, no shell, no arbitrary command execution and no arbitrary
 * protocol injection. Connection details live in a private config file.
 */
import * as __zcodeSyncFs from 'node:fs';
import * as __zcodeSyncNet from 'node:net';
import * as __zcodeSyncPath from 'node:path';
import * as __zcodeSyncCrypto from 'node:crypto';

(() => {
  const configPath = process.env.ZCODE_DESKTOP_SYNC_CONFIG;
  if (!configPath) return;
  const fs = __zcodeSyncFs;
  const net = __zcodeSyncNet;
  const path = __zcodeSyncPath;
  const crypto = __zcodeSyncCrypto;

  let config;
  try {
    config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  } catch (error) {
    return;
  }
  const token = typeof config.token === 'string' ? config.token : '';
  if (token.length < 16) return;
  if (config.host && config.host !== '127.0.0.1') return;
  const host = '127.0.0.1';
  const port = Number.isInteger(config.port) ? config.port : 0;
  const discoveryPath = typeof config.discoveryPath === 'string' ? config.discoveryPath : '';
  const caseInsensitivePaths = process.platform === 'win32';
  const readPolicy = () => createZcodeWorkspacePolicy({
    fs, path, config: JSON.parse(fs.readFileSync(configPath, 'utf8')),
  });
  try { readPolicy(); } catch { return; }

  const events = [];
  const subscribed = new Set();
  const MAX_LINE = 65536;
  const MAX_SESSIONS = 50;

  const normalize = (value) => {
    const resolved = path.resolve(value);
    return caseInsensitivePaths ? resolved.toLowerCase() : resolved;
  };
  // Re-read rules for every operation; invalid or unreadable rules deny access.
  const resolveAllowed = (value) => {
    try { return readPolicy().resolve(value); }
    catch { return null; }
  };
  const syncer = () => globalThis.__zcodeDesktopSync || null;
  let workflowAuthorization = null;
  let workflowAuthorizationError = '';
  try {
    const create = globalThis.__zcodeDesktopWorkflowAuthorizationCreate;
    if (typeof create === 'function') {
      workflowAuthorization = create({ resolveWorkspace: resolveAllowed,
        readConfig: () => {
          // Local workflow consent is independent of the external bridge API.
          return JSON.parse(fs.readFileSync(configPath, 'utf8'));
        } });
      workflowAuthorization.start();
    } else workflowAuthorizationError = 'host_update_required';
  } catch {
    try { workflowAuthorization?.dispose(); } catch {}
    workflowAuthorization = null;
    workflowAuthorizationError = 'initialization_failed';
  }
  let nativeTransport;
  async function callNative(message) {
    // The flag is re-read so disabling it takes effect without restarting a job.
    let latest;
    try { latest = JSON.parse(fs.readFileSync(configPath, 'utf8')); }
    catch { return { ok: false, error: 'native_config_unavailable' }; }
    if (latest.nativeTransport?.enabled !== true) return { ok: false, error: 'native_disabled' };
    if (!nativeTransport) {
      const create = globalThis.__zcodeDesktopNativeCreate;
      if (typeof create !== 'function') return { ok: false, error: 'native_host_not_ready' };
      try {
        nativeTransport = create({ ownershipPath: latest.nativeTransport.ownershipPath, resolveWorkspace: resolveAllowed });
      } catch (error) {
        return { ok: false, error: error.code || 'native_initialization_failed' };
      }
    }
    return nativeTransport.call(message);
  }

  function remember(workspacePath, workspaceIdentity, event) {
    if (!event || event.type !== 'workspace_task_list_changed') return;
    events.push({
      at: Date.now(),
      workspacePath,
      workspaceIdentity: workspaceIdentity || null,
      taskId: event.taskId || null,
      reason: event.reason || null,
      status: event.taskMeta && event.taskMeta.status ? event.taskMeta.status : null,
      title: event.taskMeta && event.taskMeta.title ? event.taskMeta.title : null,
    });
    if (events.length > 100) events.shift();
  }

  function subscribe(workspacePath, workspaceIdentity) {
    const key = normalize(workspacePath);
    const s = syncer();
    if (!s || subscribed.has(key)) return Boolean(s);
    subscribed.add(key);
    try {
      const onEvent = s.onDynamicWorkspaceEvent({
        workspacePath,
        ...(workspaceIdentity ? { workspaceIdentity } : {}),
      });
      if (typeof onEvent === 'function') {
        onEvent((event) => remember(workspacePath, workspaceIdentity, event));
      }
      return true;
    } catch (error) {
      subscribed.delete(key);
      return false;
    }
  }

  async function sync(message) {
    const requestedWorkspace = typeof message.workspace === 'string' ? message.workspace : '';
    const workspacePath = resolveAllowed(requestedWorkspace);
    if (!workspacePath) {
      return { ok: false, error: 'workspace_not_allowed' };
    }
    const identity = typeof message.workspaceIdentity === 'string' ? message.workspaceIdentity : undefined;
    const s = syncer();
    if (!s) return { ok: false, error: 'host_not_ready' };
    subscribe(workspacePath, identity);
    try {
      s.ensureWorkspaceSubscription({
        workspacePath,
        ...(identity ? { workspaceIdentity: identity } : {}),
      });
    } catch (error) {
      return { ok: false, error: 'subscribe_failed', detail: String(error && error.message ? error.message : error) };
    }
    const ids = (Array.isArray(message.sessionIds) ? message.sessionIds : [])
      .filter((value) => typeof value === 'string' && value.length > 0 && value.length < 200)
      .slice(0, MAX_SESSIONS);
    if (ids.length === 0) return { ok: true, synced: [] };
    if (typeof s.syncSessionFacts !== 'function') {
      return { ok: false, error: 'reconcile_unavailable' };
    }
    const synced = [];
    for (const sessionId of ids) {
      try {
        const result = await s.syncSessionFacts({
          workspacePath,
          ...(identity ? { workspaceIdentity: identity } : {}),
          sessionId,
        });
        synced.push({
          sessionId,
          changed: Boolean(result && result.changed),
          missing: Boolean(result && result.missing),
          deleted: Boolean(result && result.deleted),
          facts: result && Array.isArray(result.facts) ? result.facts : [],
        });
      } catch (error) {
        synced.push({
          sessionId,
          error: String(error && error.message ? error.message : error),
        });
      }
    }
    return { ok: true, synced };
  }

  function probe() {
    let policy;
    try { policy = readPolicy(); } catch { return { ok: false, error: 'workspace_policy_invalid' }; }
    return {
      ok: true,
      ready: Boolean(syncer()),
      pid: process.pid,
      desktopExecutable: process.execPath,
      allowedWorkspaceCount: policy.mode === 'allowlist' ? policy.pathCount : 0,
      workspacePolicyMode: policy.mode,
      blockedPathCount: policy.mode === 'blacklist' ? policy.pathCount : 0,
      subscribedWorkspaceCount: subscribed.size,
      nativeAvailable: typeof globalThis.__zcodeDesktopNativeCreate === 'function',
      workflowAuthorization: workflowAuthorization?.status() ?? {
        mode: 'unavailable', running: false, error: workflowAuthorizationError,
      },
      events: events.slice(-50),
    };
  }

  function equal(left, right) {
    const a = Buffer.from(String(left));
    const b = Buffer.from(String(right));
    return a.length === b.length && crypto.timingSafeEqual(a, b);
  }

  const server = net.createServer((socket) => {
    let buffer = '';
    let authed = false;
    let closed = false;
    const close = () => {
      if (!closed) {
        closed = true;
        socket.destroy();
      }
    };
    socket.setEncoding('utf8');
    socket.setTimeout(30000, close);
    socket.on('error', close);
    socket.on('data', (chunk) => {
      buffer += chunk;
      if (buffer.length > MAX_LINE) return close();
      let index;
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (!line) continue;
        let message;
        try {
          message = JSON.parse(line);
        } catch (error) {
          socket.write(JSON.stringify({ ok: false, error: 'invalid_json' }) + '\n');
          continue;
        }
        if (!authed) {
          if (!message || !equal(message.token || '', token)) {
            socket.write(JSON.stringify({ ok: false, error: 'unauthorized' }) + '\n');
            return close();
          }
          authed = true;
          socket.write(JSON.stringify({ ok: true, authenticated: true }) + '\n');
          continue;
        }
        if (!message || typeof message !== 'object') {
          socket.write(JSON.stringify({ ok: false, error: 'invalid_message' }) + '\n');
          continue;
        }
        if (message.op === 'probe') {
          socket.write(JSON.stringify(probe()) + '\n');
          continue;
        }
        if (message.op === 'sync') {
          sync(message).then(
            (result) => socket.write(JSON.stringify(result) + '\n'),
            (error) => socket.write(JSON.stringify({
              ok: false,
              error: 'sync_failed',
              detail: String(error && error.message ? error.message : error),
            }) + '\n'),
          );
          continue;
        }
        if (typeof message.op === 'string' && message.op.startsWith('native_')) {
          callNative(message).then(
            (result) => socket.write(JSON.stringify(result) + '\n'),
            () => socket.write(JSON.stringify({ ok: false, error: 'native_call_failed' }) + '\n'),
          );
          continue;
        }
        socket.write(JSON.stringify({ ok: false, error: 'unknown_op' }) + '\n');
      }
    });
  });
  server.on('error', () => {});
  server.listen(port, host, () => {
    if (!discoveryPath) return;
    try {
      const address = server.address();
      const payload = JSON.stringify({ pid: process.pid, host, port: address.port, version: 1 });
      const temporary = discoveryPath + '.' + process.pid + '.tmp';
      fs.writeFileSync(temporary, payload + '\n', { encoding: 'utf8', mode: 0o600 });
      // Windows rename does not always replace an existing destination.
      try {
        fs.rmSync(discoveryPath, { force: true });
        fs.renameSync(temporary, discoveryPath);
      } catch (error) {
        fs.writeFileSync(discoveryPath, payload + '\n', { encoding: 'utf8', mode: 0o600 });
        fs.rmSync(temporary, { force: true });
      }
    } catch (error) {
      // The control server stays usable; discovery is only a convenience.
    }
  });
})();
