import json
import os
import uuid
import threading
import time
from pathlib import Path
from .discovery import paths

WRITE_LOCK = threading.RLock()


def state_dir():
    override = os.environ.get('ZCODE_BRIDGE_STATE')
    base = Path(override) if override else Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'CateyeZCodeBridge'
    base.mkdir(parents=True, exist_ok=True)
    return base


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    with WRITE_LOCK:
        path = Path(path)
        tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            for attempt in range(10):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(.02)
        finally:
            tmp.unlink(missing_ok=True)


def defaults():
    return {
        **paths(),
        'default_model': {'providerId': os.environ.get('ZCODE_MODEL_PROVIDER', ''), 'modelId': os.environ.get('ZCODE_MODEL_ID', '')},
        'default_thought_level': 'auto', 'default_workflow_mode': 'onDemand',
        # Optional local profiles for high-level delegation.  Empty defaults:
        # no author path, workspace or provider id is published here.
        'superagent_profiles': {}, 'superagent_default_profile': '',
        'superagent_stall_seconds': 600,
        'superagent_read_failure_threshold': 5,
        # Optional Desktop sync is disabled by default and needs an explicit
        # private control file.  No token or author path is published here.
        'desktop_sync_enabled': False,
        'desktop_sync_control_file': '',
        'desktop_sync_min_interval_seconds': 5,
        # Optional Desktop-owned native backend.  Disabled by default; selecting
        # it never falls back to the independent engine.
        'default_transport': 'desktop_native',
        'desktop_native_enabled': True,
        'desktop_native_control_file': '',
        'auto_launch_desktop': os.name == 'nt',
        # Completion-notification operations only.  There is deliberately no
        # global "enable wake" or "enable auto continue" switch: each
        # subscription must carry the current chat's explicit user_authorized.
        'completion_notify_poll_seconds': 1,
        'completion_notify_start_timeout_seconds': 120,
        'completion_notify_expiry_hours': 6,
        'completion_notify_wake_attempts': 5,
    }


NOTIFY_RANGES = {
    'completion_notify_poll_seconds': (1, 60),
    'completion_notify_start_timeout_seconds': (10, 3600),
    'completion_notify_expiry_hours': (1, 24),
    'completion_notify_wake_attempts': (1, 20),
}

SUPERAGENT_RANGES = {
    'desktop_sync_min_interval_seconds': (1, 300),
    'superagent_stall_seconds': (60, 86400),
    'superagent_read_failure_threshold': (2, 50),
}


def config():
    path = state_dir() / 'config.json'
    saved = read_json(path, None) if path.exists() else {}
    if not isinstance(saved, dict):
        raise ValueError('桥接配置格式无效：config.json 必须是对象')
    result = {**defaults(), **saved}
    # Preferences for new tasks migrate without taking ownership of old SIDs.
    result['default_transport'] = 'desktop_native'
    result['desktop_sync_enabled'] = False
    for profile in (result.get('superagent_profiles') or {}).values():
        if isinstance(profile, dict) and profile.get('transport') == 'engine':
            profile['transport'] = 'desktop_native'
    return result


def configured_default_workspace(cfg=None):
    """Return a genuinely configured workspace, never discovery's home fallback.

    Accepted sources: the saved ``default_workspace`` key, an explicit
    ``ZCODE_WORKSPACE``/``ZCODE_DEFAULT_WORKSPACE`` environment value, or a
    caller-supplied defaults dictionary that deliberately differs from the
    discovery fallback (embedded entry points).  A fresh config with no saved
    key and no explicit env returns ``''`` so delegation refuses instead of
    editing the user's home directory.
    """
    saved_path = state_dir() / 'config.json'
    saved = read_json(saved_path, None) if saved_path.exists() else {}
    if isinstance(saved, dict):
        value = saved.get('default_workspace')
        if isinstance(value, str) and value.strip():
            return value
    env_value = os.environ.get('ZCODE_WORKSPACE') or os.environ.get('ZCODE_DEFAULT_WORKSPACE')
    if isinstance(env_value, str) and env_value.strip():
        return env_value
    cfg = config() if cfg is None else cfg
    value = cfg.get('default_workspace') if isinstance(cfg, dict) else None
    fallback = defaults().get('default_workspace')
    if isinstance(value, str) and value.strip() and value != fallback:
        return value
    return ''


def save_config(changes):
    with WRITE_LOCK:
        return _save_config(changes)


def _save_config(changes):
    if not isinstance(changes, dict) or set(changes) - set(defaults()):
        raise ValueError('未知配置项')
    result = {**config(), **changes}
    for key in ('zcode_executable', 'engine_entry', 'builtin_provider_file', 'personal_provider_file'):
        if not isinstance(result[key], str) or (result[key] and not Path(result[key]).is_file()):
            raise ValueError('文件路径无效：' + key)
    if not Path(result['default_workspace']).is_dir():
        raise ValueError('默认工作区不存在')
    if result['default_workflow_mode'] not in ('disabled', 'onDemand', 'alwaysOn'):
        raise ValueError('工作流模式无效')
    if type(result['auto_launch_desktop']) is not bool:
        raise ValueError('自动启动开关必须是布尔值')
    if not isinstance(result['node_executable'], str) or (result['node_executable'] and not Path(result['node_executable']).is_file()):
        raise ValueError('Node 程序路径无效')
    if not isinstance(result['default_thought_level'], str) or not result['default_thought_level'].strip():
        raise ValueError('默认思考强度不能为空')
    model = result['default_model']
    if not isinstance(model, dict) or not all(isinstance(model.get(k), str) for k in ('providerId', 'modelId')) or bool(model.get('providerId')) != bool(model.get('modelId')):
        raise ValueError('默认模型无效')
    profiles = result['superagent_profiles']
    if not isinstance(profiles, dict) or len(profiles) > 50:
        raise ValueError('superagent_profiles 必须是最多 50 项的对象')
    profile_fields = {'model', 'thought_level', 'workspace', 'workflow_mode',
                      'transport'}
    for name, row in profiles.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise ValueError('profile 名称无效')
        if not isinstance(row, dict) or set(row) - profile_fields:
            raise ValueError('profile 字段无效：' + name)
        profile_model = row.get('model')
        if profile_model is not None:
            if (not isinstance(profile_model, dict)
                    or set(profile_model) != {'providerId', 'modelId'}
                    or not all(isinstance(profile_model.get(k), str) and profile_model.get(k)
                               for k in ('providerId', 'modelId'))):
                raise ValueError('profile 模型无效：' + name)
        for key in ('thought_level', 'workspace'):
            if key in row and (not isinstance(row[key], str) or not row[key].strip()):
                raise ValueError('profile ' + key + ' 无效：' + name)
        if 'workflow_mode' in row and row['workflow_mode'] not in (
                'disabled', 'onDemand', 'alwaysOn'):
            raise ValueError('profile 工作流模式无效：' + name)
        if 'transport' in row and row['transport'] != 'desktop_native':
            raise ValueError('profile 传输后端无效：' + name)
    default_profile = result['superagent_default_profile']
    if not isinstance(default_profile, str):
        raise ValueError('superagent_default_profile 必须是字符串')
    if default_profile and default_profile not in profiles:
        raise ValueError('默认 profile 不存在：' + default_profile)
    if type(result['desktop_sync_enabled']) is not bool:
        raise ValueError('Desktop 同步开关必须是布尔值')
    if not isinstance(result['desktop_sync_control_file'], str):
        raise ValueError('Desktop 同步控制文件必须是字符串')
    if result['default_transport'] != 'desktop_native':
        raise ValueError('independent_engine_disabled: 只支持桌面原生后端')
    if changes.get('desktop_sync_enabled') is True:
        raise ValueError('legacy_desktop_sync_disabled: 原生桌面会话不使用旧补同步功能')
    if type(result['desktop_native_enabled']) is not bool:
        raise ValueError('桌面原生开关必须是布尔值')
    if not isinstance(result['desktop_native_control_file'], str):
        raise ValueError('桌面原生控制文件必须是字符串')
    for key, (low, high) in SUPERAGENT_RANGES.items():
        value = result.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('超级代理参数超出范围：' + key)
    for key, (low, high) in NOTIFY_RANGES.items():
        value = result.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('通知参数超出范围：' + key)
    write_json(state_dir() / 'config.json', result)
    return result
