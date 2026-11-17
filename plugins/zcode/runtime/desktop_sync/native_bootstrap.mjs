/* ZCODE_DESKTOP_NATIVE_BOOTSTRAP_V1 */
/* Narrow startup bootstrap for the *original* installed Desktop tree.
 *
 * scripts/desktop_native_install.py installs this file as
 * resources/app/out/main/index.js and keeps the untouched original next to it
 * as index.native-original.js. The only job here is to point the patched host
 * bundle at this installation's private control file:
 *
 *   1. read ./zcode-native-control.json (written by the installer, same
 *      directory as this module); when it names an absolute path, that path is
 *      authoritative and replaces any inherited ZCODE_DESKTOP_SYNC_CONFIG;
 *      when the pointer is missing or malformed, remove an inherited value so
 *      the original process can never silently attach to another install's
 *      control;
 *   2. load the byte-identical original main entry.
 *
 * It deliberately does not import electron, does not call app.setPath, does
 * not read the token, and does not write any file. The application keeps its
 * normal profile: appData/ZCode with session as its session subdirectory, as
 * resolved by the untouched main bundle. A missing pointer still lets a normal
 * desktop launch start, just without the optional control channel.
 */
import { readFile } from 'node:fs/promises';
import { isAbsolute } from 'node:path';

const POINTER = new URL('./zcode-native-control.json', import.meta.url);

let controlPath = '';
try {
  const pointer = JSON.parse(await readFile(POINTER, 'utf8'));
  const value = typeof pointer?.controlPath === 'string' ? pointer.controlPath.trim() : '';
  if (value && isAbsolute(value)) {
    controlPath = value;
  }
} catch {
  // An absent or malformed pointer must never block a normal launch.
}

if (controlPath) {
  process.env.ZCODE_DESKTOP_SYNC_CONFIG = controlPath;
} else {
  delete process.env.ZCODE_DESKTOP_SYNC_CONFIG;
}

await import('./index.native-original.js');
