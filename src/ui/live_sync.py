"""Layered live UI ticking (1s pure-memory), 2s telemetry & bg-alert sync, and stale-code hot-reload."""
from __future__ import annotations

import ctypes
from datetime import date, datetime
import subprocess
import threading
import time

from config.settings import (
    DEFAULT_ANTIGRAVITY_DIR,
    IS_FROZEN,
    IS_WINDOWS,
    LIVE_SYNC_MS,
    LIVE_TICK_MS,
    LOCAL_TZ,
    MIN_VISIBLE_DATE,
    PROJECT_ROOT,
    REFRESH_MS,
    RUNTIME_ROOT,
)
from src.core import (
    collect_dialog_contexts,
    compute_live_display_update,
    compute_source_mtime_ns,
    compute_telemetry_fingerprint,
    load_quota_config,
    local_day_bounds,
    local_week_bounds,
    serialize_dialogs,
    sync_telemetry_ledger,
)
from src.core.watcher import resolve_launch_command
from src.ui.i18n import localize_duration, localize_weekday, tr

if IS_WINDOWS:
    from ctypes import wintypes

_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


class LiveSyncMixin:
    """Provides zero-I/O 1s stopwatch ticking, 2s fingerprint sync, and seamless code hot-reload."""

    def _init_live_sync(self) -> None:
        self._live_tick_after_id: str | None = None
        self._live_sync_after_id: str | None = None
        self._last_telemetry_fp: object = None
        self._live_sync_inflight: bool = False
        self._refresh_inflight: bool = False
        self._pending_live_refresh: bool = False
        self._payload_mono: float = time.monotonic()
        self._startup_code_mtime: int = 0 if IS_FROZEN else compute_source_mtime_ns(PROJECT_ROOT)
        self._restarting: bool = False
        self._toast_queue: list[tuple[str, str, bool | None]] = []

    def _is_viewing_active_period(self, today_loc: date | None = None) -> bool:
        today = today_loc or datetime.now(LOCAL_TZ).date()
        if self.view_mode == "day":
            return self.selected_date >= today
        cur_mon, _ = local_week_bounds(today)
        sel_mon, _ = local_week_bounds(self.selected_week_date)
        return sel_mon >= cur_mon

    def _invalidate_active_caches(self, today_loc: date | None = None) -> None:
        today = today_loc or datetime.now(LOCAL_TZ).date()
        cur_mon, _ = local_week_bounds(today)
        self._day_cache.pop(today, None)
        self._week_cache.pop(cur_mon, None)

    def _prepare_force_recompute(self, today_loc: date) -> None:
        new_cfg = load_quota_config()
        if new_cfg != self._quota_cfg:
            self._day_cache.clear()
            self._week_cache.clear()
        else:
            self._invalidate_active_caches(today_loc)
            self._day_cache.pop(self.selected_date, None)
            self._week_cache.pop(local_week_bounds(self.selected_week_date)[0], None)
        self._quota_cfg = new_cfg

    def _preview_period_selection(self) -> None:
        today_loc = datetime.now(LOCAL_TZ).date()
        if self.view_mode == "day":
            d = self.selected_date
            wd = localize_weekday(f"周{'一二三四五六日'[d.weekday()]}", self._lang)
            tag = tr("today_tag", self._lang) if d == today_loc else ""
            self._itemcfg(self.date_canvas, self.date_text_item, text=f"{d.isoformat()} {wd}{tag}")
            can_prev = MIN_VISIBLE_DATE is None or d > MIN_VISIBLE_DATE
            can_next = d < today_loc
        else:
            wk_s, wk_e = local_week_bounds(self.selected_week_date)
            tag = tr("week_tag", self._lang) if wk_s <= today_loc <= wk_e else ""
            self._itemcfg(self.date_canvas, self.date_text_item, text=f"{wk_s.strftime('%m-%d')} ~ {wk_e.strftime('%m-%d')}{tag}")
            can_prev = MIN_VISIBLE_DATE is None or wk_s > MIN_VISIBLE_DATE
            can_next = wk_e < today_loc
        self._itemcfg(self.date_canvas, self.date_prev_icon, image=self._imgs["chev_l"] if can_prev else self._imgs["chev_l_dis"])
        self._itemcfg(self.date_canvas, self.date_next_icon, image=self._imgs["chev_r"] if can_next else self._imgs["chev_r_dis"])
        self._cfg(self.updated_lbl, text=tr("switching_period", self._lang))

    def _on_auto_refresh_tick(self) -> None:
        if self._window_visible and not self._is_viewing_active_period():
            self._invalidate_active_caches()
            self._after_id = self.root.after(REFRESH_MS, self._on_auto_refresh_tick)
            return
        self.refresh(force_recompute=True, schedule_next=True)

    def _consume_pending_live_refresh(self, today_loc: date) -> None:
        if not self._pending_live_refresh:
            return
        self._pending_live_refresh = False
        if self._window_visible and self._is_viewing_active_period(today_loc):
            self.refresh(force_recompute=True, schedule_next=False)

    def _try_serve_cached_view(self, today_loc: date) -> bool:
        now_mono = time.monotonic()
        if self.view_mode == "day":
            cached = self._day_cache.get(self.selected_date)
            if cached is not None and (self.selected_date < today_loc or (now_mono - cached[0]) <= 15.0):
                self._apply_payload(cached[1], preserve_scroll=False, anchor_mono=cached[0])
                self._finish_loading_transition()
                return True
            return False
        wk_mon, _ = local_week_bounds(self.selected_week_date)
        cur_mon, _ = local_week_bounds(today_loc)
        cached_wk = self._week_cache.get(wk_mon)
        if cached_wk is not None and (wk_mon < cur_mon or (now_mono - cached_wk[0]) <= 15.0):
            self._apply_payload(cached_wk[1], preserve_scroll=False, anchor_mono=cached_wk[0])
            self._finish_loading_transition()
            return True
        return False

    def _restart_resident_app(self, show_ui: bool = True) -> bool:
        """Release singleton handles, spawn a fresh GravityPulse process, and exit cleanly."""
        if self._restarting:
            return True
        self._restarting = True
        ipc = getattr(self, "_ipc", None)
        if ipc is not None:
            ipc.release()
            self._mutex_handle = 0
            self._show_event_handle = 0
        elif IS_WINDOWS:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            mutex_h = int(getattr(self, "_mutex_handle", 0) or 0)
            if mutex_h:
                self._mutex_handle = 0
                k32.CloseHandle(mutex_h)
            ev_h = int(getattr(self, "_show_event_handle", 0) or 0)
            if ev_h:
                self._show_event_handle = 0
                k32.CloseHandle(ev_h)
        cmd = resolve_launch_command(None if show_ui else "--background")
        try:
            if IS_WINDOWS:
                subprocess.Popen(
                    cmd,
                    cwd=str(RUNTIME_ROOT),
                    creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
                    close_fds=True,
                )
            else:
                subprocess.Popen(
                    cmd,
                    cwd=str(RUNTIME_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError:
            pass
        self._quit_completely()
        return True

    def _hot_reload_if_code_updated(self, show_ui: bool = True) -> bool:
        """Restart resident process seamlessly if on-disk `.py` files changed after launch."""
        if IS_FROZEN or self._restarting:
            return bool(self._restarting)
        ipc = getattr(self, "_ipc", None)
        mutex_h = int(getattr(self, "_mutex_handle", 0) or 0)
        has_lock = (ipc is not None and ipc.is_owned) or bool(mutex_h)
        if not has_lock or compute_source_mtime_ns(PROJECT_ROOT) <= self._startup_code_mtime:
            return False
        return self._restart_resident_app(show_ui=show_ui)

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

        now_loc = datetime.now(LOCAL_TZ)
        elapsed = max(0.0, time.monotonic() - self._payload_mono)
        human_str, hms_str, dialogs_changed = compute_live_display_update(cur, elapsed, now_loc)
        cur["total_human"] = human_str
        cur["total_hms"] = hms_str
        self._cfg(self.total_human_lbl, text=localize_duration(human_str, self._lang))
        self._cfg(self.total_hms_lbl, text=hms_str)
        if cur.get("live_tick_active"):
            cur["updated_at"] = now_loc.strftime("%H:%M:%S")
            self._cfg(self.updated_lbl, text=tr("updated_live", self._lang, hms=cur["updated_at"]))
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
        need_bg_alert = (not self._window_visible) or (not self._is_viewing_active_period())
        prev_fp = self._last_telemetry_fp

        def check_fp() -> None:
            bg_dialogs: list[dict] | None = None
            bg_hms = ""
            try:
                fp = compute_telemetry_fingerprint(DEFAULT_ANTIGRAVITY_DIR)
                if need_bg_alert and fp is not None and prev_fp is not None and fp != prev_fp:
                    now_loc = datetime.now(LOCAL_TZ)
                    day_start, _ = local_day_bounds(now_loc.date())
                    meta_map = sync_telemetry_ledger(DEFAULT_ANTIGRAVITY_DIR)
                    raw_dialogs = collect_dialog_contexts(
                        DEFAULT_ANTIGRAVITY_DIR, day_start, now_loc, is_active_period=True, meta_map=meta_map,
                    )
                    bg_dialogs = serialize_dialogs(raw_dialogs, "%H:%M:%S", now=now_loc)
                    bg_hms = now_loc.strftime("%H:%M:%S")
            except Exception:
                fp = None
            try:
                self.root.after(0, lambda fp=fp, d=bg_dialogs, u=bg_hms: self._on_live_fp_done(fp, d, u))
            except Exception:
                self._live_sync_inflight = False

        threading.Thread(target=check_fp, daemon=True).start()

    def _on_live_fp_done(
        self, fp: object, bg_dialogs: list[dict] | None = None, bg_hms: str = "",
    ) -> None:
        self._live_sync_inflight = False
        if fp is None:
            return
        if self._last_telemetry_fp is None:
            self._last_telemetry_fp = fp
            return
        if fp != self._last_telemetry_fp:
            self._last_telemetry_fp = fp
            self._invalidate_active_caches()
            if self._window_visible and self._is_viewing_active_period():
                if self._refresh_inflight:
                    self._pending_live_refresh = True
                else:
                    self.refresh(force_recompute=True, schedule_next=False)
            elif bg_dialogs is not None:
                self._on_monitor_done(bg_dialogs, bg_hms)
