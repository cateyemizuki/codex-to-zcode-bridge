import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import vm from 'node:vm';
import net from 'node:net';
import crypto from 'node:crypto';
import { createZcodeWorkspacePolicy } from '../runtime/desktop_sync/workspace_policy.mjs';

const drive = (letter, ...parts) => letter + ':\\' + parts.join('\\');
const fakeFs = { realpathSync(value) { return value; } };

test('Windows default denies C drive, slash/case variants and device paths', () => {
  const policy = createZcodeWorkspacePolicy({ fs: fakeFs, path, config: {}, platform: 'win32' });
  for (const value of [drive('C'), drive('c', 'work'), drive('c', 'work', '..', 'other').replaceAll('\\', '/'), '\\\\?\\' + drive('C', 'work')]) {
    assert.equal(policy.resolve(value), null);
  }
  assert.ok(policy.resolve(drive('D', 'work')));
  assert.ok(policy.resolve(drive('E', 'work')));
  assert.equal(policy.resolve('relative'), null);
});

test('subtree boundaries, traversal and resolved junctions', () => {
  const blocked = drive('D', 'blocked');
  const policy = createZcodeWorkspacePolicy({ fs: fakeFs, path, platform: 'win32',
    config: { workspacePolicy: { mode: 'blacklist', blockedPaths: [blocked] } } });
  assert.equal(policy.resolve(blocked + '\\child'), null);
  assert.equal(policy.resolve(drive('D', 'other', '..', 'BLOCKED', 'child')), null);
  assert.ok(policy.resolve(blocked + '-other'));
  const aliasFs = { realpathSync(value) { return value === drive('d', 'alias') ? drive('c', 'private') : value; } };
  const aliases = createZcodeWorkspacePolicy({ fs: aliasFs, path, config: {}, platform: 'win32' });
  assert.equal(aliases.resolve(drive('D', 'alias')), null);
});

test('legacy allowlist and invalid policy fail closed', () => {
  const config = { allowedWorkspaces: [drive('D', 'work')] };
  const policy = createZcodeWorkspacePolicy({ fs: fakeFs, path, config, platform: 'win32' });
  assert.ok(policy.resolve(drive('D', 'work')));
  assert.equal(policy.resolve(drive('D', 'work', 'child')), null);
  assert.throws(() => createZcodeWorkspacePolicy({ fs: fakeFs, path, config: { workspacePolicy: null } }));
  assert.throws(() => createZcodeWorkspacePolicy({ fs: fakeFs, path, config: { workspacePolicy: { mode: 'typo' } } }));
});

test('host shim re-reads rules for sync and the native resolver', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'workspace-policy-'));
  try {
    const configPath = path.join(root, 'control.json');
    let config = { host: '127.0.0.1', port: 0, token: 'test-token-1234567890',
      workspacePolicy: { mode: 'blacklist', blockedPaths: [] }, nativeTransport: { enabled: true } };
    fs.writeFileSync(configPath, JSON.stringify(config));
    let serverHandler;
    let nativeResolver;
    const fakeNet = { createServer(handler) { serverHandler = handler; return {
      on() { return this; }, listen() {}, unref() {}, address() { return { port: 0 }; },
    }; } };
    let source = fs.readFileSync(new URL('../runtime/desktop_sync/shim.mjs', import.meta.url), 'utf8');
    source = source.replace(/^import .*;\r?\n/gm, '');
    const context = { process: { env: { ZCODE_DESKTOP_SYNC_CONFIG: configPath }, platform: process.platform, pid: 1 },
      __zcodeSyncFs: fs, __zcodeSyncNet: fakeNet, __zcodeSyncPath: path, __zcodeSyncCrypto: crypto,
      createZcodeWorkspacePolicy, Buffer,
      __zcodeDesktopNativeCreate({ resolveWorkspace }) {
        nativeResolver = resolveWorkspace;
        return { call() { return { ok: true }; } };
      } };
    vm.runInNewContext(source, context);
    assert.equal(typeof serverHandler, 'function');
    // The exact resolver installed into native transport is live, not a
    // startup snapshot. Exercise it through the shim's authenticated socket.
    const listeners = {};
    const replies = [];
    const socket = { setEncoding() {}, setTimeout() {}, destroy() {},
      on(name, handler) { listeners[name] = handler; },
      write(line) { replies.push(JSON.parse(line)); } };
    serverHandler(socket);
    listeners.data(JSON.stringify({ token: config.token }) + '\n');
    listeners.data(JSON.stringify({ op: 'native_model_catalog', workspace: root, caller_permission_mode: 'full_access', request_id: 'catalog-policy-test' }) + '\n');
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(typeof nativeResolver, 'function');
    assert.ok(nativeResolver(root));
    config.workspacePolicy.blockedPaths = [root];
    fs.writeFileSync(configPath, JSON.stringify(config));
    assert.equal(nativeResolver(root), null);
    fs.writeFileSync(configPath, '{invalid');
    assert.equal(nativeResolver(root), null);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
