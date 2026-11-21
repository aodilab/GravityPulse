"""Companion daemon binding GravityPulse resident monitoring to Antigravity 2.0 startup (Windows & macOS)."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

from config.settings import (
    IS_FROZEN,
    IS_MACOS,
    IS_WINDOWS,
    MACOS_LAUNCH_AGENT_LABEL,
    MACOS_LAUNCH_AGENT_PLIST,
    PROJECT_ROOT,
    RUN_REG_PATH,
    RUN_REG_VALUE_NAME,
    RUNTIME_ROOT,
    SINGLETON_MUTEX_NAME,
    TARGET_EXE_NAMES,
    TARGET_MACOS_NAMES,
    WATCHER_POLL_SEC,
)
from src.core.live_state import compute_source_mtime_ns
from src.core.platform_ipc import SingletonIPC, is_named_lock_held, spawn_detached

BUILD_ENTRY = PROJECT_ROOT / "build.py"

_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_SYNCHRONIZE = 0x00100000

if IS_WINDOWS:
    from ctypes import wintypes
    import winreg

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
else:
    PROCESSENTRY32W = None  # type: ignore[assignment,misc]


def resolve_pythonw() -> str:
    """Resolve `pythonw.exe` on Windows (or active Python executable on macOS/POSIX)."""
    exe = Path(sys.executable)
    if not IS_WINDOWS:
        return str(exe)
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
    """Build startup command string for Windows HKCU Run (or display summary on macOS)."""
    argv = resolve_launch_command("--watch", frozen=frozen)
    return " ".join(f'"{part}"' if not part.startswith("--") else part for part in argv)


def build_macos_launch_agent_plist(
    argv: list[str] | None = None,
    cwd: Path | str | None = None,
    label: str = MACOS_LAUNCH_AGENT_LABEL,
) -> dict[str, object]:
    """Build a standard macOS LaunchAgent property list dict for the companion watcher."""
    return {
        "Label": label,
        "ProgramArguments": list(argv) if argv is not None else resolve_launch_command("--watch"),
        "RunAtLoad": True,
        "KeepAlive": False,
        "WorkingDirectory": str(cwd if cwd is not None else RUNTIME_ROOT),
        "ProcessType": "Background",
    }


def is_antigravity_process_output(ps_stdout: str) -> bool:
    """Parse `ps -Ax -o comm=` output and return True if an Antigravity desktop process is running."""
    for raw in ps_stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if "gravitypulse" in low:
            continue
        base = Path(line).name.lower()
        if base in TARGET_MACOS_NAMES or "antigravity.app/contents/macos/antigravity" in low:
            return True
    return False


def _is_antigravity_running_win32(k32: ctypes.WinDLL) -> bool:
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


def is_antigravity_running(k32: object | None = None) -> bool:
    """Return True if Antigravity is currently running on Windows or macOS."""
    if IS_WINDOWS:
        dll = k32
        if dll is None:
            dll = ctypes.WinDLL("kernel32", use_last_error=True)
            dll.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            dll.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            dll.Process32FirstW.restype = wintypes.BOOL
            dll.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            dll.Process32NextW.restype = wintypes.BOOL
            dll.CloseHandle.argtypes = [wintypes.HANDLE]
            dll.CloseHandle.restype = wintypes.BOOL
        return _is_antigravity_running_win32(dll)  # type: ignore[arg-type]

    try:
        res = subprocess.run(["ps", "-Ax", "-o", "comm="], capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            return is_antigravity_process_output(res.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def is_app_running(k32: object | None = None) -> bool:
    """Return True if the resident GravityPulse UI process holds the app singleton lock."""
    if IS_WINDOWS and k32 is not None:
        h = k32.OpenMutexW(_SYNCHRONIZE, False, SINGLETON_MUTEX_NAME)  # type: ignore[attr-defined]
        if h:
            k32.CloseHandle(h)  # type: ignore[attr-defined]
            return True
        return False
    return is_named_lock_held("app")


def launch_resident_app() -> None:
    """Spawn resident GravityPulse in `--background` mode without console window."""
    if not IS_FROZEN and not BUILD_ENTRY.exists():
        return
    spawn_detached(resolve_launch_command("--background"), RUNTIME_ROOT)


def _run_launchctl(args: list[str]) -> bool:
    try:
        res = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_macos_launch_agent(
    plist_path: Path = MACOS_LAUNCH_AGENT_PLIST,
    *,
    argv: list[str] | None = None,
    load_agent: bool = True,
) -> Path:
    """Write and optionally bootstrap the macOS LaunchAgent plist for auto-start at login."""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_macos_launch_agent_plist(argv=argv)
    with plist_path.open("wb") as fh:
        plistlib.dump(payload, fh, sort_keys=True)
    if load_agent and IS_MACOS:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        domain = f"gui/{uid}"
        _run_launchctl(["bootout", domain, str(plist_path)])
        if not _run_launchctl(["bootstrap", domain, str(plist_path)]):
            _run_launchctl(["load", "-w", str(plist_path)])
    return plist_path


def uninstall_macos_launch_agent(
    plist_path: Path = MACOS_LAUNCH_AGENT_PLIST,
    *,
    unload_agent: bool = True,
) -> None:
    """Unload and remove the macOS LaunchAgent plist."""
    if unload_agent and IS_MACOS and plist_path.exists():
        uid = os.getuid() if hasattr(os, "getuid") else 0
        domain = f"gui/{uid}"
        if not _run_launchctl(["bootout", domain, str(plist_path)]):
            _run_launchctl(["unload", "-w", str(plist_path)])
    try:
        plist_path.unlink(missing_ok=True)
    except OSError:
        pass


def is_startup_installed(plist_path: Path = MACOS_LAUNCH_AGENT_PLIST) -> bool:
    """Return True if companion watcher auto-start is currently registered."""
    if IS_WINDOWS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, RUN_REG_VALUE_NAME)
                return bool(str(val).strip())
        except OSError:
            return False
    if IS_MACOS:
        return plist_path.is_file()
    return False


def is_watcher_running() -> bool:
    """Return True if a companion watcher instance already holds the watcher lock."""
    return is_named_lock_held("watcher")


def ensure_watcher_running() -> bool:
    """Start the detached companion watcher if it is not already running."""
    if is_watcher_running():
        return False
    if not IS_FROZEN and not BUILD_ENTRY.exists():
        return False
    return spawn_detached(resolve_launch_command("--watch"), RUNTIME_ROOT)


def install_startup_registry() -> str:
    """Register companion watcher auto-start (HKCU Run on Windows, LaunchAgent on macOS)."""
    if IS_WINDOWS:
        cmd = resolve_startup_command()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_REG_VALUE_NAME, 0, winreg.REG_SZ, cmd)
        return cmd
    if IS_MACOS:
        return str(install_macos_launch_agent())
    raise RuntimeError("Auto-start registration is supported on Windows and macOS.")


def uninstall_startup_registry() -> None:
    """Remove companion watcher auto-start (HKCU Run on Windows, LaunchAgent on macOS)."""
    if IS_WINDOWS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_REG_VALUE_NAME)
        except FileNotFoundError:
            pass
        return
    if IS_MACOS:
        uninstall_macos_launch_agent()


def run_watcher_loop() -> None:
    """Run singleton companion watcher polling Antigravity startup transitions."""
    ipc = SingletonIPC("watcher", with_show_signal=False)
    if not ipc.acquire(signal_if_running=False):
        return

    k32 = None
    if IS_WINDOWS:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
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

    startup_mtime = 0 if IS_FROZEN else compute_source_mtime_ns(PROJECT_ROOT)
    ag_was_running = False
    try:
        while True:
            if not IS_FROZEN and BUILD_ENTRY.exists() and compute_source_mtime_ns(PROJECT_ROOT) > startup_mtime:
                ipc.release()
                spawn_detached(resolve_launch_command("--watch"), RUNTIME_ROOT)
                return
            ag_now = is_antigravity_running(k32)
            if ag_now and not ag_was_running:
                if not is_app_running(k32):
                    launch_resident_app()
                    time.sleep(1.5)
            ag_was_running = ag_now
            time.sleep(WATCHER_POLL_SEC)
    finally:
        ipc.release()
