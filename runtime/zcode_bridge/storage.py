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


def config():
    path = state_dir() / 'config.json'
    saved = read_json(path, None) if path.exists() else {}
    if not isinstance(saved, dict):
        raise ValueError('桥接配置格式无效：config.json 必须是对象')
    return {**defaults(), **saved}


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
    for key, (low, high) in NOTIFY_RANGES.items():
        value = result.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('通知参数超出范围：' + key)
    write_json(state_dir() / 'config.json', result)
    return result
