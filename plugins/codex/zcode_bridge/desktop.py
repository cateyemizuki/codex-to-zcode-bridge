"""Read desktop registration separately from the independent engine's persistence."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def session_visibility(session_id, workspace=None, home=None):
    base = Path(home) if home is not None else Path.home()
    result = {'indexed': None, 'workspace_registered': None, 'live_updates': False,
        'connection': 'independent_engine',
        'note': '核心会话已保存不代表桌面已显示；已打开项目不会实时接收独立引擎的通知。'}
    db = base / '.zcode/v2/tasks-index.sqlite'
    if db.is_file():
        try:
            with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=.5)) as connection:
                row = connection.execute("SELECT workspace_path FROM tasks WHERE task_id=? AND deleted=0 "
                    "AND archived=0 AND coalesce(cli_deleted,0)=0 AND provider='glm' AND cli_created_at IS NOT NULL",
                    (session_id,)).fetchone()
            result['indexed'] = bool(row and (not workspace or
                Path(row[0]).resolve() == Path(workspace).resolve()))
        except sqlite3.Error:
            result['index_error'] = '桌面索引暂时不可读'
    if workspace:
        try:
            settings = json.loads((base / '.zcode/v2/setting.json').read_text(encoding='utf-8'))
            result['workspace_registered'] = any(isinstance(p, dict) and p.get('kind') == 'local' and
                Path(p.get('workspacePath', '')).resolve() == Path(workspace).resolve()
                for p in settings.get('lastWorkspaceSession', []) if isinstance(p, dict) and p.get('workspacePath'))
        except (OSError, ValueError, TypeError):
            pass
    return result
