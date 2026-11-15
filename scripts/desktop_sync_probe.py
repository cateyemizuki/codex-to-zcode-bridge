#!/usr/bin/env python3
"""Read-only probe / explicit sync for the optional Desktop sync shim.

Default operation is a local status check; ``probe`` calls the loopback
``probe`` op and ``sync`` requires ``--confirm-sync`` plus explicit workspace
and session ids.  This script never opens a project, focuses a window, starts
an instance or prints the control token.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from desktop_sync_shim import ShimError, control_call


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get('ok') else 2


def _load_control(path):
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, 'config_missing'
    if not isinstance(raw, dict):
        return None, 'config_invalid'
    token = raw.get('token')
    if not isinstance(token, str) or len(token) < 16:
        return None, 'token_missing'
    host = str(raw.get('host') or '127.0.0.1')
    if host != '127.0.0.1':
        return None, 'host_not_allowed'
    allowed = raw.get('allowedWorkspaces')
    if not isinstance(allowed, list) or not allowed:
        return None, 'allowlist_missing'
    resolved = []
    for value in allowed:
        if isinstance(value, str) and value.strip():
            try:
                resolved.append(Path(value).expanduser().resolve())
            except OSError:
                continue
    if not resolved:
        return None, 'allowlist_missing'
    port = raw.get('port')
    discovery = raw.get('discoveryPath')
    return {
        'allowed': resolved,
        'port': port if type(port) is int else 0,
        'discovery': str(discovery or ''),
    }, ''


def _status(control_path):
    control, reason = _load_control(control_path)
    if control is None:
        return {'ok': False, 'op': 'status', 'state': reason}
    return {
        'ok': True,
        'op': 'status',
        'state': 'configured',
        'allowed_workspace_count': len(control['allowed']),
        'port_configured': bool(control['port']),
        'discovery_configured': bool(control['discovery']),
        'token_present': True,
        'note': 'token 未输出；本脚本不启动或聚焦 Desktop。',
    }


def _probe(control_path, timeout):
    try:
        response = control_call(Path(control_path), {'op': 'probe'}, timeout=timeout)
    except ShimError as error:
        return {'ok': False, 'op': 'probe', 'state': 'unavailable',
                'error': str(error)[:300]}
    events = response.get('events') if isinstance(response.get('events'), list) else []
    last = events[-1] if events else None
    return {
        'ok': bool(response.get('ok')),
        'op': 'probe',
        'state': 'ready' if response.get('ready') else 'not_ready',
        'ready': bool(response.get('ready')),
        'allowed_workspace_count': response.get('allowedWorkspaceCount'),
        'subscribed_workspace_count': response.get('subscribedWorkspaceCount'),
        'event_count': len(events),
        'last_event': ({'reason': last.get('reason'), 'status': last.get('status'),
                        'at': last.get('at')} if isinstance(last, dict) else None),
    }


def _sync(control_path, workspace, session_ids, timeout):
    control, reason = _load_control(control_path)
    if control is None:
        return {'ok': False, 'op': 'sync', 'state': reason}
    try:
        resolved = Path(workspace).expanduser().resolve()
    except OSError:
        return {'ok': False, 'op': 'sync', 'state': 'workspace_invalid'}
    allowed = {os.path.normcase(str(value)): value for value in control['allowed']}
    key = os.path.normcase(str(resolved))
    if key not in allowed:
        return {'ok': False, 'op': 'sync', 'state': 'workspace_not_allowed'}
    ids = [str(value) for value in session_ids if str(value)]
    truncated = len(ids) > 50
    ids = ids[:50]
    if not ids:
        return {'ok': False, 'op': 'sync', 'state': 'session_required'}
    try:
        response = control_call(Path(control_path), {
            'op': 'sync', 'workspace': str(allowed[key]),
            'sessionIds': ids}, timeout=timeout)
    except ShimError as error:
        return {'ok': False, 'op': 'sync', 'state': 'unavailable',
                'error': str(error)[:300]}
    synced = response.get('synced') if isinstance(response.get('synced'), list) else []
    return {
        'ok': bool(response.get('ok')),
        'op': 'sync',
        'state': response.get('error') or ('synced' if response.get('ok') else 'sync_failed'),
        'session_count': len(ids),
        'truncated': truncated,
        'synced': len(synced),
        'changed': sum(1 for row in synced if isinstance(row, dict) and row.get('changed')),
        'missing': sum(1 for row in synced if isinstance(row, dict) and row.get('missing')),
        'errors': sum(1 for row in synced if isinstance(row, dict) and row.get('error')),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Optional Desktop sync probe. Default is a local status check; sync requires explicit confirmation.')
    parser.add_argument('--control', required=True,
                        help='Private control.json created by desktop_sync_shim.py configure.')
    parser.add_argument('--op', choices=['status', 'probe', 'sync'], default='status')
    parser.add_argument('--workspace', help='Existing workspace; required for sync.')
    parser.add_argument('--session-id', action='append', default=[],
                        help='Session id to sync; repeatable, at most 50.')
    parser.add_argument('--confirm-sync', action='store_true',
                        help='Explicitly allow the loopback sync write.')
    parser.add_argument('--timeout', type=float, default=30.0)
    args = parser.parse_args(argv)

    if args.op == 'status':
        return _json(_status(args.control))
    if args.op == 'probe':
        return _json(_probe(args.control, args.timeout))
    if not args.confirm_sync:
        return _json({'ok': False, 'op': 'sync',
                      'error': 'confirm_required: sync 需要显式 --confirm-sync'})
    if not args.workspace:
        return _json({'ok': False, 'op': 'sync',
                      'error': 'workspace_required: sync 需要 --workspace'})
    return _json(_sync(args.control, args.workspace, args.session_id, args.timeout))


if __name__ == '__main__':
    raise SystemExit(main())
