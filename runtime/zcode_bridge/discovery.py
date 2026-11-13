"""Resolve paths on the installing machine; never ship a developer's locations."""
import os
import shutil
import sys
from pathlib import Path


def _registry_locations():
    if sys.platform != 'win32':
        return []
    import winreg
    locations = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for prefix in ('SOFTWARE', 'SOFTWARE\\WOW6432Node'):
            try:
                with winreg.OpenKey(hive, prefix+'\\Microsoft\\Windows\\CurrentVersion\\App Paths\\ZCode.exe') as key:
                    locations.append(Path(winreg.QueryValueEx(key, None)[0]))
            except OSError:
                pass
            try:
                with winreg.OpenKey(hive, prefix+'\\Microsoft\\Windows\\CurrentVersion\\Uninstall') as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as entry:
                                name = winreg.QueryValueEx(entry, 'DisplayName')[0]
                                if 'zcode' not in str(name).lower():
                                    continue
                                try:
                                    locations.append(Path(winreg.QueryValueEx(entry, 'InstallLocation')[0])/'ZCode.exe')
                                except OSError:
                                    icon = str(winreg.QueryValueEx(entry, 'DisplayIcon')[0]).split(',')[0].strip('"')
                                    locations.append(Path(icon))
                        except OSError:
                            continue
            except OSError:
                continue
    return locations


def paths(env=None, home=None):
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    candidates = []
    if env.get('ZCODE_EXECUTABLE'):
        candidates.append(Path(env['ZCODE_EXECUTABLE']).expanduser())
    if env.get('ZCODE_INSTALL_DIR'):
        folder = Path(env['ZCODE_INSTALL_DIR']).expanduser()
        candidates += [folder/'ZCode.exe', folder/'ZCode']
    command = shutil.which('ZCode.exe') or shutil.which('zcode-desktop')
    if command:
        candidates.append(Path(command))
    candidates += _registry_locations()
    for key in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
        if env.get(key):
            folder = Path(env[key])
            candidates += [folder/'Programs/ZCode/ZCode.exe', folder/'ZCode/ZCode.exe']
    candidates += [Path('/Applications/ZCode.app/Contents/MacOS/ZCode'),
        home/'Applications/ZCode.app/Contents/MacOS/ZCode', Path('/opt/ZCode/zcode')]
    executable = next((p.resolve() for p in candidates if p.is_file()), None)
    resources = executable.parent/'resources' if executable else None
    if executable and executable.parent.name == 'MacOS':
        resources = executable.parent.parent/'Resources'

    def file_override(key, fallback=None):
        if env.get(key):
            return str(Path(env[key]).expanduser().resolve())
        return str(fallback.resolve()) if fallback and fallback.is_file() else ''

    return {
        'zcode_executable': str(executable) if executable else env.get('ZCODE_EXECUTABLE', ''),
        'engine_entry': file_override('ZCODE_ENGINE_ENTRY', resources/'glm/zcode.cjs' if resources else None),
        'builtin_provider_file': file_override('ZCODE_BUILTIN_PROVIDER_FILE', resources/'config/provider/zcode-builtin.json' if resources else None),
        'personal_provider_file': file_override('ZCODE_PERSONAL_PROVIDER_FILE', home/'.zcode/v2/provider_config.json'),
        'node_executable': env.get('ZCODE_NODE_EXECUTABLE') or shutil.which('node') or '',
        'default_workspace': str(Path(env.get('ZCODE_WORKSPACE', str(home))).expanduser().resolve()),
    }
