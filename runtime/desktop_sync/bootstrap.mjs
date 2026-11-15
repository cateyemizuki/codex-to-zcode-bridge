/* ZCODE_DESKTOP_SYNC_BOOTSTRAP_V1 */
/* Narrow startup bootstrap for an explicit Desktop copy.
 *
 * In a copied Desktop tree the Electron path service cannot resolve
 * appData/userData/sessionData (they throw before app.ready), while home and
 * temp still resolve. The application's telemetry module calls
 * app.getPath('userData') during module evaluation and its rejected promise is
 * awaited in the app-ready handler, which aborts before the primary window is
 * created.
 *
 * The launcher already passes isolated directories through
 * ZCODE_DESKTOP_USER_DATA_DIR / ZCODE_DESKTOP_SESSION_DATA_DIR, and the
 * application itself calls app.setPath with the same values later in startup.
 * This wrapper applies those two calls earlier, then loads the untouched
 * original main bundle. No eval, no shell, no IPC, no arbitrary paths: only
 * absolute paths from the documented environment.
 */
import { app } from 'electron';
import { isAbsolute } from 'node:path';

const applyPath = (name, value) => {
  if (typeof value !== 'string') return false;
  const trimmed = value.trim();
  if (!trimmed || !isAbsolute(trimmed)) return false;
  try {
    app.setPath(name, trimmed);
    return true;
  } catch {
    return false;
  }
};

applyPath('userData', process.env.ZCODE_DESKTOP_USER_DATA_DIR);
applyPath('sessionData', process.env.ZCODE_DESKTOP_SESSION_DATA_DIR);

await import('./index.sync-original.js');
