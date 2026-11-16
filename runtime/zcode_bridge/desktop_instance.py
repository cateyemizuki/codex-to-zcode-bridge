"""Reuse one installed Desktop main process; never launch a headless engine."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_launch_lock = threading.Lock()


class DesktopInstanceError(RuntimeError):
    pass


@contextmanager
def desktop_launch_guard():
    """Serialize Desktop launches across controllers on the same Windows user."""
    with _launch_lock:
        if os.name != 'nt':
            yield
            return
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, 'Local\\CodexToZCodeBridgeDesktopMainLaunch')
        if not handle:
            raise DesktopInstanceError('desktop_launch_lock_unavailable')
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, 12000)
            if result not in (0, 128):
                raise DesktopInstanceError('desktop_launch_lock_unavailable')
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


def process_rows():
    if os.name != 'nt':
        raise DesktopInstanceError('desktop_process_detection_unsupported')
    query = ('Get-CimInstance Win32_Process -Filter "Name=\'ZCode.exe\'" | '
             'Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', query],
                                capture_output=True, timeout=8,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        raise DesktopInstanceError('desktop_process_detection_failed') from error
    if result.returncode:
        raise DesktopInstanceError('desktop_process_detection_failed')
    try:
        rows = json.loads(result.stdout.decode('utf-8-sig') or '[]')
    except (ValueError, UnicodeError) as error:
        raise DesktopInstanceError('desktop_process_detection_failed') from error
    if rows is None:
        rows = []
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DesktopInstanceError('desktop_process_detection_failed')
    return rows


def inspect_desktop(executable, rows=None):
    rows = process_rows() if rows is None else rows
    pids = {row.get('ProcessId') for row in rows}
    mains = []
    for row in rows:
        command = row.get('CommandLine')
        location = row.get('ExecutablePath')
        if (not isinstance(command, str) or not command.strip() or not isinstance(location, str)
                or not location or not isinstance(row.get('ProcessId'), int) or row['ProcessId'] <= 0):
            raise DesktopInstanceError('desktop_process_identity_unknown')
        try:
            arguments = [value.strip('"') for value in shlex.split(command, posix=False)]
        except ValueError as error:
            raise DesktopInstanceError('desktop_process_identity_unknown') from error
        if any(value.startswith('--type=') for value in arguments) or 'app-server' in arguments:
            continue
        # Electron also uses its executable to run native-host Node workers.
        # They are descendants with a script entry, not Desktop main instances.
        if (len(arguments) > 1 and Path(arguments[1]).suffix.lower() in ('.js', '.cjs', '.mjs')
                and row.get('ParentProcessId') in pids):
            continue
        mains.append(row)
    if len(mains) > 1:
        raise DesktopInstanceError('multiple_desktop_instances')
    if not mains:
        return {'running': False, 'pid': None}
    expected = os.path.normcase(str(Path(executable).resolve()))
    actual = os.path.normcase(str(Path(mains[0]['ExecutablePath']).resolve()))
    if actual != expected:
        raise DesktopInstanceError('desktop_instance_mismatch')
    return {'running': True, 'pid': mains[0].get('ProcessId')}


def ensure_desktop(executable, *, allow_start=False, timeout=12, inspect=None, popen=None):
    inspect = inspect or inspect_desktop
    popen = popen or subprocess.Popen
    # The shared controller serializes launch decisions. Keep the lock until
    # the new main process is observed, so concurrent task requests reuse it.
    with desktop_launch_guard():
        state = inspect(executable)
        if state['running']:
            return {**state, 'already_running': True, 'launched': False}
        if not allow_start:
            raise DesktopInstanceError('desktop_not_running')
        target = Path(executable).expanduser().resolve()
        if not target.is_file():
            raise DesktopInstanceError('desktop_executable_missing')
        child = popen([str(target)], cwd=target.parent, stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            state = inspect(str(target))
            if state['running']:
                return {**state, 'already_running': False, 'launched': True}
            if child.poll() not in (None, 0):
                raise DesktopInstanceError('desktop_launch_failed')
            time.sleep(.15)
        raise DesktopInstanceError('desktop_launch_uncertain')
