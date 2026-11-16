"""Workspace admission rules shared by the native and sync clients.

New controls default to a subtree blacklist containing only the C drive.
Legacy controls with allowedWorkspaces retain their exact allowlist until an
explicit policy migration. These are workspace rules, not a file sandbox.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path

DEFAULT_BLOCKED_PATHS = ['C:\\']


def _path(value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise ValueError('workspace_policy_invalid')
    value = os.path.expanduser(value)
    windows = os.name == 'nt' or bool(ntpath.splitdrive(value)[0])
    module = ntpath if windows else os.path
    if not module.isabs(value):
        raise ValueError('workspace_policy_invalid')
    if windows:
        value = value.replace('/', '\\')
        if value.lower().startswith('\\\\?\\unc\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
    lexical = module.normpath(value)
    resolved = str(Path(lexical).resolve()) if os.name == 'nt' or not windows else lexical
    if windows:
        resolved = ntpath.normpath(resolved)
        if resolved.lower().startswith('\\\\?\\unc\\'):
            resolved = '\\\\' + resolved[8:]
        elif resolved.startswith('\\\\?\\'):
            resolved = resolved[4:]
    key = module.normcase(resolved)
    return module.normcase(lexical), key, resolved, module


def _inside(candidate, root, module):
    try:
        return module.commonpath((candidate, root)) == root
    except ValueError:
        return False


class WorkspacePolicy:
    def __init__(self, control):
        explicit = control.get('workspacePolicy')
        if 'workspacePolicy' not in control:
            explicit = {'mode': 'allowlist' if 'allowedWorkspaces' in control else 'blacklist'}
        if not isinstance(explicit, dict) or explicit.get('mode') not in ('blacklist', 'allowlist'):
            raise ValueError('workspace_policy_invalid')
        self.mode = explicit['mode']
        values = (explicit.get('blockedPaths', DEFAULT_BLOCKED_PATHS)
                  if self.mode == 'blacklist' else control.get('allowedWorkspaces'))
        if not isinstance(values, list):
            raise ValueError('workspace_policy_invalid')
        if self.mode == 'allowlist' and not values:
            raise ValueError('allowlist_missing')
        self.paths = [_path(value) for value in values]
        self.allowed = {key: resolved for _, key, resolved, _ in self.paths} if self.mode == 'allowlist' else {}

    def resolve(self, value):
        lexical, key, resolved, module = _path(value)
        if self.mode == 'allowlist':
            return self.allowed.get(key)
        for blocked_lexical, blocked_key, _, blocked_module in self.paths:
            if module is blocked_module and any(
                    _inside(candidate, root, module)
                    for candidate in (lexical, key)
                    for root in (blocked_lexical, blocked_key)):
                return None
        return resolved


def policy_config(mode='blacklist', blocked_paths=None):
    result = {'mode': mode}
    if mode == 'blacklist':
        result['blockedPaths'] = list(DEFAULT_BLOCKED_PATHS if blocked_paths is None else blocked_paths)
    return result
