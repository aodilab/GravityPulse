"""Cross-platform single-instance lock, show-window IPC, and detached process spawning."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import socket
import subprocess
import tempfile

from config.settings import (
    DATA_DIR,
    IS_WINDOWS,
    SHOW_EVENT_NAME,
    SINGLETON_MUTEX_NAME,
    WATCHER_MUTEX_NAME,
)

_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_SYNCHRONIZE = 0x00100000

if IS_WINDOWS:
    from ctypes import wintypes
else:
    import fcntl


def spawn_detached(cmd: list[str], cwd: Path | str) -> bool:
    """Spawn a detached background process with no console window on Windows or macOS/POSIX."""
    try:
        if IS_WINDOWS:
            subprocess.Popen(
                cmd,
                cwd=str(cwd),
                creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        return True
    except OSError:
        return False


def _posix_lock_path(name: str, lock_dir: Path | None = None) -> Path:
    base = lock_dir or DATA_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f".{name}.lock"


def _posix_sock_path(name: str, sock_dir: Path | None = None) -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    base = sock_dir or Path(tempfile.gettempdir())
    return base / f"gravitypulse_{uid}_{name}.sock"


def is_named_lock_held(name: str, lock_dir: Path | None = None) -> bool:
    """Return True if another live process currently holds the named app/watcher singleton lock."""
    if IS_WINDOWS:
        win_name = SINGLETON_MUTEX_NAME if name == "app" else (WATCHER_MUTEX_NAME if name == "watcher" else name)
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            k32.OpenMutexW.restype = wintypes.HANDLE
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            h = k32.OpenMutexW(_SYNCHRONIZE, False, win_name)
            if h:
                k32.CloseHandle(h)
                return True
        except Exception:
            pass
        return False

    path = _posix_lock_path(name, lock_dir=lock_dir)
    fd = -1
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


class SingletonIPC:
    """Manages single-instance lock ownership and non-blocking show-window wakeup signals."""

    def __init__(
        self,
        name: str = "app",
        *,
        with_show_signal: bool = True,
        lock_dir: Path | None = None,
        sock_dir: Path | None = None,
    ) -> None:
        self.name = name
        self.with_show_signal = with_show_signal
        self._lock_dir = lock_dir
        self._sock_dir = sock_dir
        self.mutex_handle: int = 0
        self.show_event_handle: int = 0
        self._k32: object | None = None
        self._lock_fd: int = -1
        self._sock: socket.socket | None = None
        self._sock_path: Path | None = None

    @property
    def is_owned(self) -> bool:
        return bool(self.mutex_handle) if IS_WINDOWS else self._lock_fd >= 0

    def acquire(self, *, signal_if_running: bool = False) -> bool:
        """Acquire singleton lock. Returns False if another instance is already running."""
        if IS_WINDOWS:
            return self._acquire_win32(signal_if_running=signal_if_running)
        return self._acquire_posix(signal_if_running=signal_if_running)

    def _acquire_win32(self, *, signal_if_running: bool) -> bool:
        win_mutex = SINGLETON_MUTEX_NAME if self.name == "app" else WATCHER_MUTEX_NAME
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.SetEvent.restype = wintypes.BOOL
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        show_ev = k32.CreateEventW(None, False, False, SHOW_EVENT_NAME) if self.with_show_signal else 0
        mutex_h = k32.CreateMutexW(None, False, win_mutex)
        already_running = ctypes.get_last_error() == 183
        if already_running:
            if show_ev and signal_if_running:
                k32.SetEvent(show_ev)
            if mutex_h:
                k32.CloseHandle(mutex_h)
            if show_ev:
                k32.CloseHandle(show_ev)
            return False

        self._k32 = k32
        self.mutex_handle = int(mutex_h or 0)
        self.show_event_handle = int(show_ev or 0)
        return True

    def _acquire_posix(self, *, signal_if_running: bool) -> bool:
        lock_path = _posix_lock_path(self.name, lock_dir=self._lock_dir)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            if self.with_show_signal and signal_if_running:
                self._send_posix_show()
            return False
        except OSError:
            os.close(fd)
            return False

        self._lock_fd = fd
        if self.with_show_signal:
            sock_path = _posix_sock_path(self.name, sock_dir=self._sock_dir)
            try:
                if sock_path.exists():
                    sock_path.unlink()
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                sock.bind(str(sock_path))
                sock.setblocking(False)
                self._sock = sock
                self._sock_path = sock_path
            except OSError:
                self._sock = None
                self._sock_path = None
        return True

    def _send_posix_show(self) -> None:
        sock_path = _posix_sock_path(self.name, sock_dir=self._sock_dir)
        if not sock_path.exists():
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as cli:
                cli.setblocking(False)
                cli.sendto(b"SHOW", str(sock_path))
        except OSError:
            pass

    def poll_show_signal(self) -> bool:
        """Non-blocking poll returning True if another launch asked this instance to show."""
        if IS_WINDOWS:
            if not self.show_event_handle or self._k32 is None:
                return False
            try:
                return self._k32.WaitForSingleObject(self.show_event_handle, 0) == 0
            except Exception:
                return False

        if self._sock is None:
            return False
        signaled = False
        while True:
            try:
                data = self._sock.recv(32)
                if data:
                    signaled = True
            except BlockingIOError:
                break
            except OSError:
                break
        return signaled

    def release(self) -> None:
        """Release mutex/lockfile and close any IPC handles cleanly."""
        if IS_WINDOWS:
            k32 = self._k32
            if k32 is None and (self.mutex_handle or self.show_event_handle):
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                k32.CloseHandle.restype = wintypes.BOOL
            if self.mutex_handle and k32 is not None:
                try:
                    k32.CloseHandle(self.mutex_handle)
                except Exception:
                    pass
                self.mutex_handle = 0
            if self.show_event_handle and k32 is not None:
                try:
                    k32.CloseHandle(self.show_event_handle)
                except Exception:
                    pass
                self.show_event_handle = 0
            return

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._sock_path is not None:
            try:
                self._sock_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._sock_path = None
        if self._lock_fd >= 0:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = -1
