"""Layered live UI ticking (1s pure-memory), 3s telemetry sync, and stale-code hot-reload."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import subprocess
import threading
import time

from config.settings import (
    DEFAULT_ANTIGRAVITY_DIR,
    IS_FROZEN,
    LIVE_SYNC_MS,
    LIVE_TICK_MS,
    LOCAL_TZ,
    PROJECT_ROOT,
    RUNTIME_ROOT,
)
from src.core import (
    compute_live_display_update,
    compute_source_mtime_ns,
    compute_telemetry_fingerprint,
    sync_telemetry_ledger,
)
from src.core.watcher import resolve_launch_command

_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


class LiveSyncMixin:
    """Provides zero-I/O 1s stopwatch ticking, 3s fingerprint sync, and seamless code hot-reload."""

    def _init_live_sync(self) -> None:
        self._live_tick_after_id: str | None = None
        self._live_sync_after_id: str | None = None
        self._last_telemetry_fp: object = None
        self._live_sync_inflight: bool = False
        self._payload_mono: float = time.monotonic()
        self._startup_code_mtime: int = 0 if IS_FROZEN else compute_source_mtime_ns(PROJECT_ROOT)
        self._restarting: bool = False

    def _hot_reload_if_code_updated(self, show_ui: bool = True) -> bool:
        """Restart resident process seamlessly if on-disk `.py` files changed after launch."""
        if IS_FROZEN or self._restarting:
            return bool(self._restarting)
        mutex_h = int(getattr(self, "_mutex_handle", 0) or 0)
        if not mutex_h or compute_source_mtime_ns(PROJECT_ROOT) <= self._startup_code_mtime:
            return False
        self._restarting = True
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        self._mutex_handle = 0
        k32.CloseHandle(mutex_h)
        ev_h = int(getattr(self, "_show_event_handle", 0) or 0)
        if ev_h:
            self._show_event_handle = 0
            k32.CloseHandle(ev_h)
        cmd = resolve_launch_command(None if show_ui else "--background")
        try:
            subprocess.Popen(
                cmd,
                cwd=str(RUNTIME_ROOT),
                creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
                close_fds=True,
            )
        except OSError:
            pass
        self._quit_completely()
        return True

    def _start_live_loops(self) -> None:
        self._schedule_live_tick()
        self._schedule_live_sync()

    def _stop_live_loops(self) -> None:
        self._cancel_after("_live_tick_after_id")
        self._cancel_after("_live_sync_after_id")

    def _schedule_live_tick(self) -> None:
        self._cancel_after("_live_tick_after_id")
        self._live_tick_after_id = self.root.after(LIVE_TICK_MS, self._on_live_tick)

    def _on_live_tick(self) -> None:
        self._schedule_live_tick()
        if not self._window_visible:
            return
        cur = self._current_payload
        if cur is None:
            return
        mode = cur.get("mode")
        is_active = bool(cur.get("is_today") if mode == "day" else cur.get("is_current_week"))
        if not is_active:
            return

        now_bj = datetime.now(LOCAL_TZ)
        elapsed = max(0.0, time.monotonic() - self._payload_mono)
        human_str, hms_str, dialogs_changed = compute_live_display_update(cur, elapsed, now_bj)
        cur["total_human"] = human_str
        cur["total_hms"] = hms_str
        self._cfg(self.total_human_lbl, text=human_str)
        self._cfg(self.total_hms_lbl, text=hms_str)
        if cur.get("live_tick_active"):
            cur["updated_at"] = now_bj.strftime("%H:%M:%S")
            self._cfg(self.updated_lbl, text=f"更新于 {cur['updated_at']} · 实时同步中")
        if dialogs_changed:
            self._process_switch_notifications(cur.get("dialogs", []))
            active_tab = self.day_tab if mode == "day" else self.week_tab
            if active_tab == "ctx":
                self._render_tabs_and_rows(cur, preserve_scroll=True)

    def _schedule_live_sync(self) -> None:
        self._cancel_after("_live_sync_after_id")
        self._live_sync_after_id = self.root.after(LIVE_SYNC_MS, self._run_live_sync)

    def _run_live_sync(self) -> None:
        self._schedule_live_sync()
        if self._live_sync_inflight:
            return
        self._live_sync_inflight = True
        visible = self._window_visible
        prev_fp = self._last_telemetry_fp

        def check_fp() -> None:
            try:
                fp = compute_telemetry_fingerprint(DEFAULT_ANTIGRAVITY_DIR)
                if not visible and fp is not None and fp != prev_fp:
                    sync_telemetry_ledger(DEFAULT_ANTIGRAVITY_DIR)
            except Exception:
                fp = None
            try:
                self.root.after(0, lambda fp=fp: self._on_live_fp_done(fp))
            except Exception:
                self._live_sync_inflight = False

        threading.Thread(target=check_fp, daemon=True).start()

    def _on_live_fp_done(self, fp: object) -> None:
        self._live_sync_inflight = False
        if fp is None:
            return
        if self._last_telemetry_fp is None:
            self._last_telemetry_fp = fp
            return
        if fp != self._last_telemetry_fp:
            self._last_telemetry_fp = fp
            self._day_cache.clear()
            self._week_cache.clear()
            if self._window_visible:
                self.refresh(force_recompute=True, schedule_next=False)
