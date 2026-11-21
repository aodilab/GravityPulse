"""Companion daemon binding GravityPulse resident monitoring to Antigravity 2.0 startup."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys
import time
import winreg

from config.settings import (
    IS_FROZEN,
    PROJECT_ROOT,
    RUN_REG_PATH,
    RUN_REG_VALUE_NAME,
    RUNTIME_ROOT,
    SINGLETON_MUTEX_NAME,
    TARGET_EXE_NAMES,
    WATCHER_MUTEX_NAME,
    WATCHER_POLL_SEC,
)
from src.core.live_state import compute_source_mtime_ns

BUILD_ENTRY = PROJECT_ROOT / "build.py"

_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_SYNCHRONIZE = 0x00100000
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def resolve_pythonw() -> str:
    """Resolve `pythonw.exe` next to the active interpreter so background processes create zero console windows."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.exists():
        return str(exe)
    cand = exe.with_name("pythonw.exe")
    if cand.exists():
        return str(cand)
    return str(exe)


def resolve_launch_command(flag: str | None = None, *, frozen: bool | None = None) -> list[str]:
    """Build detached launch argv for either frozen executable or source `build.py` mode."""
    is_frozen = IS_FROZEN if frozen is None else frozen
    cmd = [str(Path(sys.executable).resolve())] if is_frozen else [resolve_pythonw(), str(BUILD_ENTRY)]
    if flag:
        cmd.append(flag)
    return cmd


def resolve_startup_command(*, frozen: bool | None = None) -> str:
    """Build HKCU Run command string for companion watcher startup."""
    argv = resolve_launch_command("--watch", frozen=frozen)
    return " ".join(f'"{part}"' if not part.startswith("--") else part for part in argv)


def is_antigravity_running(k32: ctypes.WinDLL) -> bool:
    """Return True if `Antigravity.exe` is currently running."""
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE_VALUE:
        return False
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not k32.Process32FirstW(snap, ctypes.byref(pe)):
            return False
        while True:
            if pe.szExeFile.lower() in TARGET_EXE_NAMES:
                return True
            if not k32.Process32NextW(snap, ctypes.byref(pe)):
                break
        return False
    finally:
        k32.CloseHandle(snap)


def is_app_running(k32: ctypes.WinDLL) -> bool:
    """Return True if the resident GravityPulse UI process holds `SINGLETON_MUTEX_NAME`."""
    h = k32.OpenMutexW(_SYNCHRONIZE, False, SINGLETON_MUTEX_NAME)
    if h:
        k32.CloseHandle(h)
        return True
    return False


def launch_resident_app() -> None:
    """Spawn resident GravityPulse in `--background` mode without console window."""
    if not IS_FROZEN and not BUILD_ENTRY.exists():
        return
    try:
        subprocess.Popen(
            resolve_launch_command("--background"),
            cwd=str(RUNTIME_ROOT),
            creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
            close_fds=True,
        )
    except OSError:
        pass


def install_startup_registry() -> str:
    """Register companion watcher under HKCU Run so Antigravity launches auto-start GravityPulse."""
    cmd = resolve_startup_command()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_REG_VALUE_NAME, 0, winreg.REG_SZ, cmd)
    return cmd


def uninstall_startup_registry() -> None:
    """Remove `GravityPulseWatcher` from HKCU Run."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_REG_VALUE_NAME)
    except FileNotFoundError:
        pass


def run_watcher_loop() -> None:
    """Run singleton companion watcher polling `Antigravity.exe` startup transitions."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    watcher_mutex = k32.CreateMutexW(None, False, WATCHER_MUTEX_NAME)
    if ctypes.get_last_error() == 183:
        if watcher_mutex:
            k32.CloseHandle(watcher_mutex)
        return

    startup_mtime = 0 if IS_FROZEN else compute_source_mtime_ns(PROJECT_ROOT)
    ag_was_running = False
    try:
        while True:
            if not IS_FROZEN and BUILD_ENTRY.exists() and compute_source_mtime_ns(PROJECT_ROOT) > startup_mtime:
                if watcher_mutex:
                    k32.CloseHandle(watcher_mutex)
                    watcher_mutex = 0
                try:
                    subprocess.Popen(
                        resolve_launch_command("--watch"),
                        cwd=str(RUNTIME_ROOT),
                        creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
                        close_fds=True,
                    )
                except OSError:
                    pass
                return
            ag_now = is_antigravity_running(k32)
            if ag_now and not ag_was_running:
                if not is_app_running(k32):
                    launch_resident_app()
                    time.sleep(1.5)
            ag_was_running = ag_now
            time.sleep(WATCHER_POLL_SEC)
    finally:
        if watcher_mutex:
            k32.CloseHandle(watcher_mutex)
