"""Resident desktop GravityPulse application controller and cross-platform single-instance entrypoint."""
from __future__ import annotations

import ctypes
from datetime import date, datetime, timedelta
import queue
import threading
import time
import tkinter as tk

from config.settings import (
    APP_USER_MODEL_ID, BG_PAGE, DEFAULT_ANTIGRAVITY_DIR, ICON_PATH, IPC_POLL_MS,
    IS_MACOS, IS_WINDOWS, LOCAL_TZ, MIN_VISIBLE_DATE, MONITOR_MS, REFRESH_MS,
)
from src.core import (
    QuotaConfig, SwitchAlertTracker, collect_dialog_contexts, compute_day_report,
    compute_telemetry_fingerprint, compute_week_report, load_quota_config,
    local_day_bounds, local_week_bounds, report_to_dict, serialize_dialogs,
    sync_telemetry_ledger, week_report_to_dict,
)
from src.core.platform_ipc import SingletonIPC
from src.ui.hero_view import HeroViewMixin
from src.ui.i18n import load_ui_language, localize_toast, normalize_lang, tr
from src.ui.layout import LayoutMixin
from src.ui.live_sync import LiveSyncMixin
from src.ui.modals import ModalMixin
from src.ui.system_tray import SystemTrayIcon
from src.ui.table_views import TableViewsMixin
from src.ui.updater_ui import UpdaterUiMixin

if IS_WINDOWS:
    from ctypes import wintypes


def _init_platform_shell() -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class GravityPulseApp(LayoutMixin, HeroViewMixin, TableViewsMixin, ModalMixin, LiveSyncMixin, UpdaterUiMixin):
    """Native desktop GravityPulse dashboard with tray residency and zero-jump auto-refresh."""

    def __init__(
        self,
        root: tk.Tk,
        show_event_handle: int = 0,
        start_hidden: bool = False,
        mutex_handle: int = 0,
        language: str | None = None,
        ipc: SingletonIPC | None = None,
    ) -> None:
        self.root = root
        self._ipc: SingletonIPC | None = ipc
        self._show_event_handle = ipc.show_event_handle if ipc is not None else show_event_handle
        self._mutex_handle = ipc.mutex_handle if ipc is not None else mutex_handle
        self._lang: str = normalize_lang(language) or load_ui_language()
        self._tray_cmds: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        if IS_MACOS:
            from src.ui.macos_tray import configure_macos_root

            configure_macos_root(self)
            self.scale = 1.0
        else:
            self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)

        self.win_w = self.px(960)
        self.win_h = self.px(776)
        self.content_w = self.px(896)
        self.shadow_pad = self.px(12)

        self.root.title("GravityPulse")
        self.root.geometry(f"{self.win_w}x{self.win_h}")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_PAGE)

        if IS_WINDOWS and ICON_PATH.exists():
            try:
                self.root.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass

        today_loc = datetime.now(LOCAL_TZ).date()
        self.view_mode: str = "day"
        self.selected_date: date = today_loc
        self.selected_week_date: date = today_loc
        self._follow_today: bool = True
        self._follow_current_week: bool = True
        self.day_tab: str = "ctx"
        self.week_tab: str = "ctx"

        self._quota_cfg: QuotaConfig = load_quota_config()
        self._calib_win: tk.Toplevel | None = None
        self._day_cache: dict[date, tuple[float, dict]] = {}
        self._week_cache: dict[date, tuple[float, dict]] = {}
        self._current_payload: dict | None = None
        self._request_seq: int = 0
        self._after_id: str | None = None
        self._monitor_after_id: str | None = None
        self._ipc_after_id: str | None = None
        self._prefetch_after_id: str | None = None
        self._topmost_after_id: str | None = None
        self._fade_after_id: str | None = None
        self._spinner_after_id: str | None = None
        self._reveal_after_id: str | None = None
        self._tooltip_win: tk.Toplevel | None = None
        self._toast_win: tk.Toplevel | None = None
        self._toast_after_id: str | None = None
        self._alert_tracker: SwitchAlertTracker = SwitchAlertTracker()
        self._init_live_sync()
        self._window_visible: bool = False
        self._loading_active: bool = False
        self._loading_min_until: float = 0.0
        self._spinner_step: int = 0

        self._k32_ipc = None
        if IS_WINDOWS and self._ipc is None:
            self._k32_ipc = ctypes.WinDLL("kernel32", use_last_error=True)
            self._k32_ipc.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self._k32_ipc.WaitForSingleObject.restype = wintypes.DWORD

        self._widget_cache: dict[tuple[int, str], object] = {}
        self._canvas_item_cache: dict[tuple[int, int, str], object] = {}
        self._today_btn_packed: bool = False
        self._update_btn_packed: bool = False
        self._last_viz_sig: object = None
        self._last_head_sig: object = None
        self._last_rows_sig: object = None
        self._last_view_ctx: tuple[str, str, str] | None = None
        self._last_scroll_h: int = -1
        self._last_sb_state: tuple[float, float, int] | None = None

        self._imgs: dict[str, object] = {}
        self._row_hitboxes: list[tuple[int, int, str]] = []
        self._week_bar_hitboxes: list[tuple[int, int, str]] = []
        self._hover_row_idx: int = -1

        self.root.protocol("WM_DELETE_WINDOW", self._on_close_to_tray)
        if not IS_MACOS:
            self.root.bind("<Alt-F4>", lambda _e: self._on_close_to_tray())

        self._build_ui()
        self._center_window()

        self._tray = SystemTrayIcon(
            icon_path=ICON_PATH, tooltip=tr("tray_tip", self._lang),
            cmd_queue=self._tray_cmds, menu_state_fn=self._get_tray_menu_state,
        )
        self._tray.start()

        if start_hidden:
            self.root.withdraw()
            self._window_visible = False
        else:
            self._present_window_with_loading(min_ms=280)

        self.refresh(force_recompute=True, schedule_next=True)
        self._schedule_dialog_monitor()
        self._schedule_ipc_poll()
        self._start_live_loops()
        self._init_updater()

    def _on_mode_click(self, event: tk.Event) -> None:
        cw = self.px(102)
        target_mode = "day" if event.x < cw // 2 else "week"
        if target_mode == self.view_mode:
            return
        self.view_mode = target_mode
        self._draw_mode_switch()
        self.refresh(force_recompute=False, schedule_next=False)

    def _on_date_pill_click(self, event: tk.Event) -> None:
        cw = self.px(242)
        if event.x <= self.px(42):
            self._shift_period(-1)
        elif event.x >= cw - self.px(42):
            self._shift_period(1)
        else:
            self._go_current()

    def _shift_period(self, delta: int) -> None:
        today_loc = datetime.now(LOCAL_TZ).date()
        if self.view_mode == "day":
            target = self.selected_date + timedelta(days=delta)
            if (MIN_VISIBLE_DATE is not None and target < MIN_VISIBLE_DATE) or target > today_loc:
                return
            self.selected_date = target
            self._follow_today = target == today_loc
        else:
            cur_mon, _ = local_week_bounds(today_loc)
            target_mon, _ = local_week_bounds(self.selected_week_date + timedelta(days=7 * delta))
            if (MIN_VISIBLE_DATE is not None and target_mon < MIN_VISIBLE_DATE) or target_mon > cur_mon:
                return
            self.selected_week_date = target_mon
            self._follow_current_week = target_mon == cur_mon
        self.refresh(force_recompute=False, schedule_next=False)

    def _go_current(self) -> None:
        today_loc = datetime.now(LOCAL_TZ).date()
        if self.view_mode == "day":
            self._follow_today = True
            if self.selected_date == today_loc:
                return
            self.selected_date = today_loc
        else:
            cur_mon, _ = local_week_bounds(today_loc)
            sel_mon, _ = local_week_bounds(self.selected_week_date)
            self._follow_current_week = True
            if sel_mon == cur_mon:
                return
            self.selected_week_date = today_loc
        self.refresh(force_recompute=False, schedule_next=False)

    def _on_tab_select(self, tab_idx: int) -> None:
        if self.view_mode == "day":
            self.day_tab = ("ctx", "conv", "iv")[tab_idx]
        else:
            self.week_tab = ("ctx", "days", "conv")[tab_idx]
        if self._current_payload is not None:
            self._render_tabs_and_rows(self._current_payload, preserve_scroll=False)

    def refresh(self, force_recompute: bool = True, schedule_next: bool = False) -> None:
        if force_recompute and not self._window_visible and self._hot_reload_if_code_updated(show_ui=False):
            return
        today_loc = datetime.now(LOCAL_TZ).date()
        if self._follow_today:
            self.selected_date = today_loc
        if self._follow_current_week:
            self.selected_week_date = today_loc

        if force_recompute:
            self._prepare_force_recompute(today_loc)

        if schedule_next or self._after_id is None:
            if self._after_id is not None:
                self.root.after_cancel(self._after_id)
            self._after_id = self.root.after(REFRESH_MS, self._on_auto_refresh_tick)

        if not force_recompute and self._try_serve_cached_view(today_loc):
            return

        self._request_seq += 1
        req_id = self._request_seq
        self._refresh_inflight = True
        if not force_recompute:
            self._preview_period_selection()
        mode = self.view_mode
        day_target = self.selected_date
        week_target = self.selected_week_date
        preserve_scroll = force_recompute and self._current_payload is not None
        q_cfg = self._quota_cfg

        def worker() -> None:
            try:
                fp = compute_telemetry_fingerprint(DEFAULT_ANTIGRAVITY_DIR) if force_recompute else None
                snap_mono = time.monotonic()
                if mode == "day":
                    payload = report_to_dict(compute_day_report(target_date=day_target, quota_config=q_cfg))
                    payload["mode"] = "day"
                else:
                    wk_rep = compute_week_report(target_date=week_target, quota_config=q_cfg)
                    payload = week_report_to_dict(wk_rep)
                    payload["mode"] = "week"
            except Exception:
                self.root.after(0, lambda: setattr(self, "_refresh_inflight", False) if req_id == self._request_seq else None)
                return
            try:
                self.root.after(0, lambda: self._on_worker_done(req_id, payload, preserve_scroll, fp, snap_mono))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_worker_done(
        self, req_id: int, payload: dict, preserve_scroll: bool, fp: object = None, snap_mono: float | None = None,
    ) -> None:
        if req_id != self._request_seq:
            return
        self._refresh_inflight = False
        if fp is not None:
            self._last_telemetry_fp = fp
        today_loc = datetime.now(LOCAL_TZ).date()
        anchor_mono = snap_mono if snap_mono is not None else time.monotonic()
        if payload["mode"] == "day":
            self._day_cache[date.fromisoformat(payload["target_date"])] = (anchor_mono, payload)
        else:
            self._week_cache[date.fromisoformat(payload["week_start"])] = (anchor_mono, payload)

        self._apply_payload(payload, preserve_scroll=preserve_scroll, anchor_mono=anchor_mono)
        self._finish_loading_transition()
        self._consume_pending_live_refresh(today_loc)

        if payload["mode"] == "day" and payload.get("is_today"):
            yday = today_loc - timedelta(days=1)
            need_yday = (MIN_VISIBLE_DATE is None or yday >= MIN_VISIBLE_DATE) and yday not in self._day_cache
            cur_mon, _ = local_week_bounds(today_loc)
            if (need_yday or cur_mon not in self._week_cache) and self._prefetch_after_id is None:
                q_cfg = self._quota_cfg

                def start_prefetch() -> None:
                    self._prefetch_after_id = None

                    def prefetch() -> None:
                        if (MIN_VISIBLE_DATE is None or yday >= MIN_VISIBLE_DATE) and yday not in self._day_cache:
                            p = report_to_dict(compute_day_report(target_date=yday, quota_config=q_cfg))
                            p["mode"] = "day"
                            t_done = time.monotonic()
                            try:
                                self.root.after(0, lambda p=p, t=t_done: self._day_cache.setdefault(yday, (t, p)))
                            except Exception:
                                return
                        if cur_mon not in self._week_cache:
                            wp = week_report_to_dict(compute_week_report(target_date=today_loc, quota_config=q_cfg))
                            wp["mode"] = "week"
                            t_done = time.monotonic()
                            try:
                                self.root.after(0, lambda wp=wp, t=t_done: self._week_cache.setdefault(cur_mon, (t, wp)))
                            except Exception:
                                return

                    threading.Thread(target=prefetch, daemon=True).start()

                self._prefetch_after_id = self.root.after(2500, start_prefetch)

    def _apply_payload(self, data: dict, preserve_scroll: bool, anchor_mono: float | None = None) -> None:
        self._current_payload = data
        if anchor_mono is not None:
            self._payload_mono = anchor_mono
        if data["mode"] == "day":
            self._render_day_hero(data)
        else:
            self._render_week_hero(data)
        self._render_tabs_and_rows(data, preserve_scroll=preserve_scroll)
        is_active = bool(data.get("is_today") if data["mode"] == "day" else data.get("is_current_week"))
        if is_active:
            self._process_switch_notifications(data.get("dialogs", []))

    def _schedule_dialog_monitor(self) -> None:
        self._cancel_after("_monitor_after_id")
        self._monitor_after_id = self.root.after(MONITOR_MS, self._run_dialog_monitor)

    def _run_dialog_monitor(self) -> None:
        if not self._window_visible and self._hot_reload_if_code_updated(show_ui=False):
            return
        self._schedule_dialog_monitor()

        def bg_check() -> None:
            now_loc = datetime.now(LOCAL_TZ)
            day_start, _ = local_day_bounds(now_loc.date())
            meta_map = sync_telemetry_ledger(DEFAULT_ANTIGRAVITY_DIR)
            raw_dialogs = collect_dialog_contexts(
                DEFAULT_ANTIGRAVITY_DIR, day_start, now_loc, is_active_period=True, meta_map=meta_map,
            )
            serialized = serialize_dialogs(raw_dialogs, "%H:%M:%S", now=now_loc)
            try:
                self.root.after(0, lambda s=serialized, u=now_loc.strftime("%H:%M:%S"): self._on_monitor_done(s, u))
            except Exception:
                pass

        threading.Thread(target=bg_check, daemon=True).start()

    def _on_monitor_done(self, dialogs: list[dict], updated_hms: str) -> None:
        self._process_switch_notifications(dialogs)
        cur = self._current_payload
        if cur is not None and cur.get("mode") == "day" and cur.get("is_today"):
            cur["dialogs"] = dialogs
            cur["updated_at"] = updated_hms
            if self.day_tab == "ctx":
                self._render_tabs_and_rows(cur, preserve_scroll=True)

    def _process_switch_notifications(self, dialogs: list[dict]) -> None:
        alerts = self._alert_tracker.process_batch_with_kind(dialogs, notify_enabled=self._quota_cfg.notify_switch)
        if alerts:
            localized = [(*localize_toast(h, d, self._lang), is_z) for h, d, is_z in alerts]
            self._enqueue_switch_alerts(localized)

    def show_window(self) -> None:
        """Restore resident window from system tray / menu bar with smooth spinner + fade transition."""
        if self._hot_reload_if_code_updated(show_ui=True):
            return
        if not self._window_visible:
            self._present_window_with_loading(min_ms=300)
            if IS_MACOS:
                from src.ui.macos_tray import activate_macos_app

                activate_macos_app()
            self.refresh(force_recompute=True, schedule_next=True)
            return
        try:
            self.root.attributes("-alpha", 1.0)
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            if IS_MACOS:
                from src.ui.macos_tray import activate_macos_app

                activate_macos_app()
            else:
                self.root.attributes("-topmost", True)
                self._cancel_after("_topmost_after_id")
                self._topmost_after_id = self.root.after(140, self._clear_topmost_safe)
            self.root.focus_force()
        except Exception:
            pass

    def _schedule_ipc_poll(self) -> None:
        self._cancel_after("_ipc_after_id")
        self._ipc_after_id = self.root.after(IPC_POLL_MS, self._poll_ipc_and_tray)

    def _poll_ipc_and_tray(self) -> None:
        if self._ipc is not None:
            if self._ipc.poll_show_signal():
                self.show_window()
        elif IS_WINDOWS and self._show_event_handle and self._k32_ipc is not None:
            try:
                if self._k32_ipc.WaitForSingleObject(self._show_event_handle, 0) == 0:
                    self.show_window()
            except Exception:
                pass

        while not self._tray_cmds.empty():
            try:
                cmd = self._tray_cmds.get_nowait()
            except queue.Empty:
                break
            if cmd == "show":
                self.show_window()
            elif cmd == "refresh":
                if self._window_visible:
                    self.refresh(force_recompute=True, schedule_next=True)
                else:
                    self.show_window()
            elif cmd == "check_update":
                self._open_update_dialog(force_check=True)
            elif cmd == "quit":
                self._quit_completely()
                return
            elif cmd in ("toggle_startup", "create_shortcut", "toggle_lang"):
                self._handle_tray_action(cmd)

        self._schedule_ipc_poll()

    def _on_close_to_tray(self) -> None:
        """Clicking window ✕ hides immediately to system tray so resident monitoring continues."""
        self._window_visible = False
        for attr in ("_topmost_after_id", "_fade_after_id", "_reveal_after_id"):
            self._cancel_after(attr)
        self._hide_loading_overlay_now()
        try:
            self._hide_tooltip()
        except Exception:
            pass
        if self._calib_win is not None:
            try:
                if self._calib_win.winfo_exists():
                    self._calib_win.destroy()
            except Exception:
                pass
            self._calib_win = None
        self._close_update_window()
        try:
            self.root.attributes("-topmost", False)
            self.root.attributes("-alpha", 1.0)
        except Exception:
            pass
        self.root.withdraw()

    def _quit_completely(self) -> None:
        """Exit resident process cleanly from tray menu."""
        self._window_visible = False
        try:
            self._hide_tooltip()
        except Exception:
            pass
        self._dismiss_toast()
        self._stop_live_loops()
        self._stop_updater()
        for attr in (
            "_after_id", "_monitor_after_id", "_ipc_after_id", "_prefetch_after_id",
            "_topmost_after_id", "_fade_after_id", "_spinner_after_id", "_reveal_after_id",
        ):
            self._cancel_after(attr)
        self._tray.stop()
        self.root.destroy()


def launch_desktop_app(args: set[str] | None = None) -> None:
    """Start single-instance GravityPulse desktop UI or signal existing instance to show."""
    _init_platform_shell()
    arg_set = {a.lower() for a in (args or set())}
    is_bg_autostart = "--background" in arg_set
    start_hidden = is_bg_autostart or "--minimized" in arg_set or "--tray" in arg_set

    ipc = SingletonIPC("app", with_show_signal=True)
    if not ipc.acquire(signal_if_running=(not is_bg_autostart and not start_hidden)):
        return

    root = tk.Tk()
    root.withdraw()
    app = GravityPulseApp(root, start_hidden=start_hidden, ipc=ipc)
    try:
        root.mainloop()
    finally:
        if app._ipc is not None:
            app._ipc.release()
