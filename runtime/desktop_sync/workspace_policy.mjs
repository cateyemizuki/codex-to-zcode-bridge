/* ZCODE_WORKSPACE_POLICY_V1 */
export function createZcodeWorkspacePolicy({ fs, path, config, platform = process.platform }) {
  const windows = platform === 'win32';
  const clean = (value, paths, foldCase = true) => {
    if (typeof value !== 'string' || !value.trim() || value.includes('\0') || !paths.isAbsolute(value)) {
      throw new Error('workspace_policy_invalid');
    }
    let result = paths.normalize(value);
    if (paths === path.win32) {
      if (result.toLowerCase().startsWith('\\\\?\\unc\\')) result = '\\\\' + result.slice(8);
      else if (result.startsWith('\\\\?\\')) result = result.slice(4);
      if (foldCase) result = result.toLowerCase();
    }
    return result;
  };
  const canonical = (value) => {
    const paths = windows || /^[a-z]:/i.test(value) || /^\\\\/.test(value) ? path.win32 : path;
    const lexical = clean(value, paths);
    try {
      const display = clean(fs.realpathSync(lexical), paths, false);
      return { paths, lexical, display, resolved: clean(display, paths) };
    }
    catch (error) {
      if (error.code !== 'ENOENT' && error.code !== 'ENOTDIR') throw error;
      return { paths, lexical, display: clean(value, paths, false), resolved: lexical };
    }
  };
  const policy = Object.hasOwn(config, 'workspacePolicy') ? config.workspacePolicy
    : { mode: Object.hasOwn(config, 'allowedWorkspaces') ? 'allowlist' : 'blacklist' };
  if (!policy || !['blacklist', 'allowlist'].includes(policy.mode)) throw new Error('workspace_policy_invalid');
  const values = policy.mode === 'blacklist'
    ? (Object.hasOwn(policy, 'blockedPaths') ? policy.blockedPaths : ['C:\\']) : config.allowedWorkspaces;
  if (!Array.isArray(values) || (policy.mode === 'allowlist' && !values.length)) throw new Error('workspace_policy_invalid');
  const roots = values.map(canonical);
  const inside = (candidate, root, paths) => {
    const relative = paths.relative(root, candidate);
    return relative === '' || (!paths.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + paths.sep));
  };
  return {
    mode: policy.mode,
    pathCount: roots.length,
    resolve(value) {
      try {
        const { paths, lexical, resolved, display } = canonical(value);
        if (policy.mode === 'allowlist') return roots.find((root) => root.paths === paths && root.resolved === resolved)?.display || null;
        if (roots.some((blocked) => blocked.paths === paths && [lexical, resolved].some((candidate) =>
          [blocked.lexical, blocked.resolved].some((root) => inside(candidate, root, paths))))) return null;
        return display;
      } catch { return null; }
    },
  };
}
