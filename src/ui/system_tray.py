"""Native Win32 system tray icon running on a dedicated message-loop daemon thread."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import queue
import threading
import time

_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_USER = 0x0400
_WM_TRAYICON = _WM_USER + 20
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_NIM_ADD = 0x00000000
_NIM_DELETE = 0x00000002
_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x00000010
_LR_DEFAULTSIZE = 0x00000040
_MF_STRING = 0x00000000
_MF_SEPARATOR = 0x00000800
_TPM_RIGHTBUTTON = 0x0002
_TPM_NONOTIFY = 0x0080
_TPM_RETURNCMD = 0x0100

_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.ubyte * 8 if hasattr(wintypes, "ubyte") else ctypes.c_ubyte * 8),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class SystemTrayIcon:
    """Native Win32 system tray icon with left-click restore and right-click context menu."""

    def __init__(self, icon_path: Path, tooltip: str, cmd_queue: "queue.SimpleQueue[str]") -> None:
        self._icon_path = icon_path
        self._tooltip = tooltip[:127]
        self._cmd_queue = cmd_queue
        self._hwnd: wintypes.HWND | None = None
        self._hicon: wintypes.HICON | None = None
        self._nid: _NOTIFYICONDATAW | None = None
        self._wndproc_ref: _WNDPROC | None = None
        self._taskbar_msg: int = 0
        self._thread = threading.Thread(target=self._run, name="GravityPulseTray", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            try:
                u32 = ctypes.WinDLL("user32", use_last_error=True)
                u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                u32.PostMessageW.restype = wintypes.BOOL
                u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            except Exception:
                pass

    def _add_tray_icon(self, shell32: ctypes.WinDLL) -> None:
        if self._nid is not None:
            shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(self._nid))

    def _run(self) -> None:
        try:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            sh32 = ctypes.WinDLL("shell32", use_last_error=True)

            k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            k32.GetModuleHandleW.restype = wintypes.HMODULE
            u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u32.DefWindowProcW.restype = ctypes.c_ssize_t
            u32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
            u32.RegisterClassExW.restype = wintypes.ATOM
            u32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            ]
            u32.CreateWindowExW.restype = wintypes.HWND
            u32.LoadImageW.argtypes = [
                wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT
            ]
            u32.LoadImageW.restype = wintypes.HANDLE
            u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
            u32.LoadIconW.restype = wintypes.HICON
            u32.CreatePopupMenu.argtypes = []
            u32.CreatePopupMenu.restype = wintypes.HMENU
            u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
            u32.AppendMenuW.restype = wintypes.BOOL
            u32.DestroyMenu.argtypes = [wintypes.HMENU]
            u32.DestroyMenu.restype = wintypes.BOOL
            u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            u32.GetCursorPos.restype = wintypes.BOOL
            u32.SetForegroundWindow.argtypes = [wintypes.HWND]
            u32.SetForegroundWindow.restype = wintypes.BOOL
            u32.TrackPopupMenu.argtypes = [
                wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, wintypes.LPCVOID,
            ]
            u32.TrackPopupMenu.restype = wintypes.BOOL
            u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u32.PostMessageW.restype = wintypes.BOOL
            u32.DestroyWindow.argtypes = [wintypes.HWND]
            u32.DestroyWindow.restype = wintypes.BOOL
            u32.PostQuitMessage.argtypes = [ctypes.c_int]
            u32.PostQuitMessage.restype = None
            u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            u32.GetMessageW.restype = wintypes.BOOL
            u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            u32.TranslateMessage.restype = wintypes.BOOL
            u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            u32.DispatchMessageW.restype = ctypes.c_ssize_t
            u32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
            u32.RegisterWindowMessageW.restype = wintypes.UINT
            sh32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
            sh32.Shell_NotifyIconW.restype = wintypes.BOOL

            self._taskbar_msg = int(u32.RegisterWindowMessageW("TaskbarCreated") or 0)

            def wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
                if msg == _WM_TRAYICON:
                    ev = lparam & 0xFFFF
                    if ev in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                        self._cmd_queue.put("show")
                        return 0
                    if ev == _WM_RBUTTONUP:
                        hmenu = u32.CreatePopupMenu()
                        if hmenu:
                            u32.AppendMenuW(hmenu, _MF_STRING, 1, "打开 GravityPulse 面板")
                            u32.AppendMenuW(hmenu, _MF_STRING, 2, "立即刷新数据")
                            u32.AppendMenuW(hmenu, _MF_SEPARATOR, 0, None)
                            u32.AppendMenuW(hmenu, _MF_STRING, 3, "退出常驻程序")
                            pt = wintypes.POINT()
                            u32.GetCursorPos(ctypes.byref(pt))
                            u32.SetForegroundWindow(hwnd)
                            cmd = int(
                                u32.TrackPopupMenu(
                                    hmenu,
                                    _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY,
                                    pt.x, pt.y, 0, hwnd, None,
                                )
                            )
                            u32.PostMessageW(hwnd, 0, 0, 0)
                            u32.DestroyMenu(hmenu)
                            if cmd == 1:
                                self._cmd_queue.put("show")
                            elif cmd == 2:
                                self._cmd_queue.put("refresh")
                            elif cmd == 3:
                                self._cmd_queue.put("quit")
                        return 0
                elif self._taskbar_msg and msg == self._taskbar_msg:
                    self._add_tray_icon(sh32)
                    return 0
                elif msg == _WM_CLOSE:
                    if self._nid is not None:
                        sh32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._nid))
                    u32.DestroyWindow(hwnd)
                    return 0
                elif msg == _WM_DESTROY:
                    u32.PostQuitMessage(0)
                    return 0
                return int(u32.DefWindowProcW(hwnd, msg, wparam, lparam))

            self._wndproc_ref = _WNDPROC(wnd_proc)
            hinst = k32.GetModuleHandleW(None)
            cls_name = f"GravityPulseTrayWnd_{int(time.time() * 1000)}"
            wc = _WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            u32.RegisterClassExW(ctypes.byref(wc))

            hwnd = u32.CreateWindowExW(0, cls_name, cls_name, 0, 0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                return
            self._hwnd = hwnd

            hicon = None
            if self._icon_path.exists():
                hicon = u32.LoadImageW(None, str(self._icon_path), _IMAGE_ICON, 0, 0, _LR_LOADFROMFILE | _LR_DEFAULTSIZE)
            if not hicon:
                hicon = u32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR))
            self._hicon = hicon

            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
            nid.uCallbackMessage = _WM_TRAYICON
            nid.hIcon = hicon
            nid.szTip = self._tooltip
            self._nid = nid
            self._add_tray_icon(sh32)

            msg = wintypes.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
