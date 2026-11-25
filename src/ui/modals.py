"""Non-activating desktop switch toast, quota calibration modal, and rules tooltip."""
from __future__ import annotations

import ctypes
from dataclasses import replace
from datetime import datetime, timedelta
import re
import tkinter as tk

from config.settings import (
    BG_CARD, BG_PAGE, DEFAULT_ANTIGRAVITY_DIR, FONT_UI, ICON_PATH, INK, INK_2, INK_3,
    IS_MACOS, IS_WINDOWS, LINE_HEX, LOCAL_TZ, TOAST_DURATION_MS, WEEKLY_CYCLE_SEC,
)

if IS_WINDOWS:
    from ctypes import wintypes
from src.core.models import QuotaConfig
from src.core.quota import compute_quota_summary, resolve_current_cycle_anchor_start, save_quota_config
from src.ui.i18n import localize_reset_str, tr
from src.ui.primitives import make_dialog_button
from src.ui.tray_actions import TrayActionsMixin

_COUNTDOWN_TOKEN_RE = re.compile(
    r"(?<![\d.-])(\d+(?:\.\d+)?)\s*"
    r"(?:(days|day|d|hours|hour|hrs|hr|h|minutes|minute|mins|min|m)(?![a-zA-Z])|(天|小时|时|分钟|分))",
    re.IGNORECASE,
)
_COUNTDOWN_INVALID_NUM_RE = re.compile(r"-\s*\d|\d+\.\d+\.")


def _ensure_local_dt(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ) if dt.tzinfo is not None else dt.replace(tzinfo=LOCAL_TZ)


def parse_reset_countdown_to_start(
    countdown_str: str,
    now_loc: datetime,
    cycle_sec: int = WEEKLY_CYCLE_SEC,
) -> datetime | None:
    """Convert an official remaining reset countdown (e.g. '6d 20h') into its cycle start timestamp."""
    raw = countdown_str.strip()
    if not raw or _COUNTDOWN_INVALID_NUM_RE.search(raw):
        return None
    matches = list(_COUNTDOWN_TOKEN_RE.finditer(raw))
    if not matches or re.search(r"\d", _COUNTDOWN_TOKEN_RE.sub(" ", raw)):
        return None
    rem_sec = 0.0
    for m in matches:
        val = float(m.group(1))
        u = (m.group(2) or m.group(3)).lower()
        if u in ("d", "day", "days", "天"):
            rem_sec += val * 86400.0
        elif u in ("h", "hr", "hrs", "hour", "hours", "小时", "时"):
            rem_sec += val * 3600.0
        else:
            rem_sec += val * 60.0
    if rem_sec <= 0.0 or rem_sec > float(cycle_sec):
        return None
    return _ensure_local_dt(now_loc) + timedelta(seconds=rem_sec) - timedelta(seconds=cycle_sec)


def build_calibrated_quota_config(
    prev_cfg: QuotaConfig,
    now_bj: datetime,
    used_pct: float,
    mult: float,
    notify_switch: bool,
    reset_cycle: bool = False,
    antigravity_dir=DEFAULT_ANTIGRAVITY_DIR,
    reset_countdown: str = "",
    auto_check_update: bool | None = None,
    neon_mult: float | None = None,
) -> QuotaConfig:
    """Build updated `QuotaConfig` from modal inputs, preserving history when resetting rolling cycle."""
    ref_loc = _ensure_local_dt(now_bj)
    auto_upd = prev_cfg.auto_check_update if auto_check_update is None else bool(auto_check_update)
    eff_neon = prev_cfg.neon_multiplier if neon_mult is None else float(neon_mult)
    if reset_cycle:
        snap_cap = prev_cfg.weekly_cap_usd_override
        if snap_cap <= 0.0:
            snap_cap = compute_quota_summary(antigravity_dir, ref_loc, ref_loc, quota_config=prev_cfg).weekly_cap_usd
        reset_start_dt = parse_reset_countdown_to_start(reset_countdown, ref_loc, WEEKLY_CYCLE_SEC) or ref_loc
        return replace(
            prev_cfg,
            carbon_multiplier=mult,
            neon_multiplier=eff_neon,
            weekly_cap_usd_override=snap_cap,
            notify_switch=notify_switch,
            auto_check_update=auto_upd,
            rolling_reset_start=reset_start_dt.isoformat(timespec="seconds"),
        )

    eff_used = max(0.1, min(100.0, used_pct))
    cycle_start = resolve_current_cycle_anchor_start(prev_cfg, ref_loc, antigravity_dir)
    temp_cfg = replace(
        prev_cfg,
        carbon_multiplier=mult,
        neon_multiplier=eff_neon,
        anchor_start=cycle_start,
        anchor_end=ref_loc.isoformat(timespec="seconds"),
        anchor_weekly_used_pct=eff_used,
        weekly_cap_usd_override=0.0,
        notify_switch=notify_switch,
        auto_check_update=auto_upd,
    )
    cal_summary = compute_quota_summary(antigravity_dir, ref_loc, ref_loc, quota_config=temp_cfg)
    snap_cap = cal_summary.weekly_cap_usd if cal_summary.weekly_cap_usd > 0.0 else prev_cfg.weekly_cap_usd_override
    return replace(temp_cfg, weekly_cap_usd_override=snap_cap)


class ModalMixin(TrayActionsMixin):
    """Modal dialogs, silent toast notifications, and hover tooltip handlers."""

    def _open_calibration_dialog(self) -> None:
        if self._calib_win is not None and self._calib_win.winfo_exists():
            self._calib_win.lift()
            return

        lang = self._lang
        win = tk.Toplevel(self.root)
        win.title(tr("calib_win_title", lang))
        win.configure(bg=BG_PAGE)
        win.resizable(False, False)
        if IS_WINDOWS and ICON_PATH.exists():
            try:
                win.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass
        root_shown = str(self.root.state()) != "withdrawn"
        if root_shown:
            win.transient(self.root)
        else:
            win.withdraw()

        pad = self.px(18)
        card = tk.Frame(win, bg=BG_CARD, padx=pad, pady=pad, highlightbackground=LINE_HEX, highlightthickness=1)
        card.pack(padx=self.px(14), pady=self.px(14))

        cur_q = (self._current_payload or {}).get("quota", {})
        cur_calibrated = bool(cur_q.get("calibrated"))
        cur_cycle_used = float(cur_q.get("cycle_used_pct", self._quota_cfg.anchor_weekly_used_pct))
        cur_cycle_left = float(cur_q.get("cycle_left_pct", max(0.0, 100.0 - cur_cycle_used)))
        cur_cycle_reset = localize_reset_str(str(cur_q.get("cycle_reset_str", "")), lang)
        cur_5h_left = float(cur_q.get("five_hour_left_pct", 100.0))

        tk.Label(card, text=tr("calib_title", lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 11, "bold")).pack(anchor="w")
        sub_hint = tr("calib_hint", lang)
        if cur_calibrated:
            sep = "，" if lang == "zh" else ", "
            reset_part = f"{sep}{cur_cycle_reset}" if cur_cycle_reset else ""
            sub_hint += "\n" + tr("calib_cur", lang, left=cur_cycle_left, used=cur_cycle_used, reset=reset_part, fh=cur_5h_left)
        tk.Label(card, text=sub_hint, bg=BG_CARD, fg=INK_3, justify="left", font=(FONT_UI, 8)).pack(anchor="w", pady=(self.px(3), self.px(10)))

        default_used = round(cur_cycle_used, 1) if cur_calibrated and cur_cycle_used >= 0.1 else self._quota_cfg.anchor_weekly_used_pct
        used_var = tk.StringVar(value=f"{default_used:g}")
        mult_var = tk.StringVar(value=f"{self._quota_cfg.carbon_multiplier:g}")
        neon_mult_var = tk.StringVar(value=f"{self._quota_cfg.neon_multiplier:g}")
        reset_cycle_var = tk.BooleanVar(value=False)
        reset_countdown_var = tk.StringVar(value="")
        notify_var = tk.BooleanVar(value=bool(self._quota_cfg.notify_switch))
        auto_update_var = tk.BooleanVar(value=bool(self._quota_cfg.auto_check_update))

        row1 = tk.Frame(card, bg=BG_CARD)
        row1.pack(fill=tk.X, pady=(0, self.px(6)))
        used_lbl = tk.Label(row1, text=tr("calib_used_lbl", lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 9))
        used_lbl.pack(side=tk.LEFT)
        used_entry = tk.Entry(
            row1, textvariable=used_var, width=10, font=(FONT_UI, 9), relief="solid", bd=1,
            justify="right", disabledbackground=BG_PAGE, disabledforeground=INK_3,
        )
        used_entry.pack(side=tk.RIGHT)

        err_var = tk.StringVar(value="")

        def _sync_reset_cd_state() -> None:
            is_reset = bool(reset_cycle_var.get())
            err_var.set("")
            used_entry.configure(state="disabled" if is_reset else "normal")
            used_lbl.configure(fg=INK_3 if is_reset else INK)
            reset_cd_entry.configure(state="normal" if is_reset else "disabled")
            reset_cd_lbl.configure(fg=INK_2 if is_reset else INK_3)

        reset_row = tk.Frame(card, bg=BG_CARD)
        reset_row.pack(fill=tk.X, pady=(0, self.px(4)))
        tk.Checkbutton(
            reset_row, text=tr("calib_reset_chk", lang), variable=reset_cycle_var, command=_sync_reset_cd_state,
            bg=BG_CARD, fg=INK_2, activebackground=BG_CARD, selectcolor=BG_CARD, font=(FONT_UI, 8), cursor="hand2",
        ).pack(side=tk.LEFT)

        reset_cd_row = tk.Frame(card, bg=BG_CARD)
        reset_cd_row.pack(fill=tk.X, pady=(0, self.px(8)))
        reset_cd_lbl = tk.Label(reset_cd_row, text=tr("calib_reset_cd_lbl", lang), bg=BG_CARD, fg=INK_3, font=(FONT_UI, 8))
        reset_cd_lbl.pack(side=tk.LEFT, padx=(self.px(18), self.px(8)))
        reset_cd_entry = tk.Entry(
            reset_cd_row, textvariable=reset_countdown_var, width=12, font=(FONT_UI, 9), relief="solid", bd=1,
            justify="right", state="disabled", disabledbackground=BG_PAGE, disabledforeground=INK_3,
        )
        reset_cd_entry.pack(side=tk.RIGHT)

        row2 = tk.Frame(card, bg=BG_CARD)
        row2.pack(fill=tk.X, pady=(0, self.px(6)))
        tk.Label(row2, text=tr("calib_mult_lbl", lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 9)).pack(side=tk.LEFT)
        tk.Entry(row2, textvariable=mult_var, width=10, font=(FONT_UI, 9), relief="solid", bd=1, justify="right").pack(side=tk.RIGHT)

        preset_row = tk.Frame(card, bg=BG_CARD)
        preset_row.pack(fill=tk.X, pady=(0, self.px(8)))
        tk.Label(preset_row, text=tr("calib_preset_lbl", lang), bg=BG_CARD, fg=INK_3, font=(FONT_UI, 8)).pack(side=tk.LEFT, padx=(0, self.px(6)))
        for val_str, lbl in (("2.0", "2.0x"), ("2.6667", "2.67x"), ("3.0", "3.0x"), ("3.5", tr("calib_preset_measured", lang))):
            make_dialog_button(
                preset_row, text=lbl, command=lambda v=val_str: mult_var.set(v),
                bg=BG_PAGE, fg=INK_2, padx=self.px(8), pady=self.px(2), font=(FONT_UI, 8),
            ).pack(side=tk.LEFT, padx=(0, self.px(5)))

        row_neon = tk.Frame(card, bg=BG_CARD)
        row_neon.pack(fill=tk.X, pady=(0, self.px(8)))
        tk.Label(row_neon, text=tr("calib_neon_mult_lbl", lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 9)).pack(side=tk.LEFT)
        tk.Entry(row_neon, textvariable=neon_mult_var, width=10, font=(FONT_UI, 9), relief="solid", bd=1, justify="right").pack(side=tk.RIGHT)

        notify_row = tk.Frame(card, bg=BG_CARD)
        notify_row.pack(fill=tk.X, pady=(0, self.px(6)))
        tk.Checkbutton(
            notify_row, text=tr("calib_notify_chk", lang), variable=notify_var,
            bg=BG_CARD, fg=INK, activebackground=BG_CARD, selectcolor=BG_CARD, font=(FONT_UI, 8), cursor="hand2",
        ).pack(side=tk.LEFT)
        make_dialog_button(
            notify_row, text=tr("calib_preview", lang),
            command=lambda: self._show_switch_toast(tr("calib_preview_head", lang), tr("calib_preview_body", lang)),
            bg=BG_PAGE, fg=INK_2, padx=self.px(8), pady=self.px(2), font=(FONT_UI, 8),
        ).pack(side=tk.RIGHT, padx=(self.px(8), 0))

        upd_row = tk.Frame(card, bg=BG_CARD)
        upd_row.pack(fill=tk.X, pady=(0, self.px(6)))
        tk.Checkbutton(
            upd_row, text=tr("calib_auto_upd_chk", lang), variable=auto_update_var,
            bg=BG_CARD, fg=INK, activebackground=BG_CARD, selectcolor=BG_CARD, font=(FONT_UI, 8), cursor="hand2",
        ).pack(side=tk.LEFT)
        make_dialog_button(
            upd_row, text=tr("calib_check_upd", lang),
            command=lambda: (win.destroy(), self._open_update_dialog(force_check=True)),
            bg=BG_PAGE, fg=INK_2, padx=self.px(8), pady=self.px(2), font=(FONT_UI, 8),
        ).pack(side=tk.RIGHT, padx=(self.px(8), 0))

        tk.Label(card, textvariable=err_var, bg=BG_CARD, fg="#c9342b", font=(FONT_UI, 8), anchor="w").pack(fill=tk.X, pady=(0, self.px(6)))
        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill=tk.X)

        def on_save() -> None:
            is_reset = bool(reset_cycle_var.get())
            try:
                mult = max(0.5, min(20.0, float(mult_var.get().strip().rstrip("xX").strip())))
                n_mult = max(0.5, min(20.0, float(neon_mult_var.get().strip().rstrip("xX").strip())))
            except ValueError:
                err_var.set(tr("calib_err_mult", lang))
                return
            if is_reset:
                used_pct = 0.0
            else:
                try:
                    used_pct = max(0.1, min(100.0, float(used_var.get().strip().rstrip("%").strip())))
                except ValueError:
                    err_var.set(tr("calib_err_used", lang))
                    return
            now_loc = datetime.now(LOCAL_TZ)
            raw_cd = reset_countdown_var.get().strip() if is_reset else ""
            if is_reset and raw_cd and parse_reset_countdown_to_start(raw_cd, now_loc, WEEKLY_CYCLE_SEC) is None:
                err_var.set(tr("calib_err_cd", lang))
                reset_cd_entry.focus_set()
                return
            err_var.set("")
            new_cfg = build_calibrated_quota_config(
                self._quota_cfg, now_bj=now_loc, used_pct=used_pct, mult=mult,
                notify_switch=bool(notify_var.get()), reset_cycle=is_reset,
                reset_countdown=raw_cd, auto_check_update=bool(auto_update_var.get()), neon_mult=n_mult,
            )
            save_quota_config(new_cfg)
            self._quota_cfg = new_cfg
            if hasattr(self, "_on_update_config_changed"):
                self._on_update_config_changed()
            self._day_cache.clear()
            self._week_cache.clear()
            win.destroy()
            self.refresh(force_recompute=True, schedule_next=False)

        make_dialog_button(
            btn_row, text=tr("btn_cancel", lang), command=win.destroy,
            bg=BG_PAGE, fg=INK_2, padx=self.px(14), pady=self.px(5), font=(FONT_UI, 9),
        ).pack(side=tk.LEFT)
        make_dialog_button(
            btn_row, text=tr("btn_save", lang), command=on_save,
            bg=INK, fg="#ffffff", padx=self.px(16), pady=self.px(5), font=(FONT_UI, 9, "bold"),
        ).pack(side=tk.RIGHT)

        win.update_idletasks()
        px_x = self.root.winfo_rootx() + (self.win_w - win.winfo_reqwidth()) // 2
        px_y = self.root.winfo_rooty() + (self.win_h - win.winfo_reqheight()) // 2
        win.geometry(f"+{max(0, px_x)}+{max(0, px_y)}")
        if root_shown:
            try:
                win.grab_set()
            except tk.TclError:
                pass
        self._calib_win = win

    def _enqueue_switch_alerts(self, alerts: list[tuple[str, str] | tuple[str, str, bool]]) -> None:
        """Queue one or more switch alerts so parallel project checkpoints never overwrite each other."""
        if not alerts:
            return
        q = getattr(self, "_toast_queue", None)
        if q is None:
            q = []
            self._toast_queue = q
        for raw in alerts:
            item = raw if len(raw) == 3 else (raw[0], raw[1], None)
            if item not in q:
                q.append(item)
        if self._toast_win is None and q:
            head, detail, is_zone = q.pop(0)
            self._show_switch_toast(head, detail, is_zone=is_zone)

    def _dismiss_toast(self, show_next: bool = False) -> None:
        if self._toast_after_id is not None:
            try:
                self.root.after_cancel(self._toast_after_id)
            except Exception:
                pass
            self._toast_after_id = None
        if self._toast_win is not None:
            try:
                self._toast_win.destroy()
            except Exception:
                pass
            self._toast_win = None
        q = getattr(self, "_toast_queue", None)
        if not show_next:
            if q is not None:
                q.clear()
            return
        if q:
            raw = q.pop(0)
            head, detail, is_zone = raw if len(raw) == 3 else (raw[0], raw[1], None)
            self._show_switch_toast(head, detail, is_zone=is_zone)

    def _focus_main_context_tab(self) -> None:
        self._dismiss_toast(show_next=False)
        if self.view_mode == "day":
            self.day_tab = "ctx"
        else:
            self.week_tab = "ctx"
        if self._current_payload is not None:
            self._render_tabs_and_rows(self._current_payload, preserve_scroll=False)
        self.show_window()

    def _show_switch_toast(
        self,
        conv_header: str,
        detail_msg: str,
        badge_text: str | None = None,
        badge_color: str | None = None,
        is_zone: bool | None = None,
    ) -> None:
        """Render a silent, non-activating bottom-right visual popup without system sound."""
        if self._toast_after_id is not None:
            try:
                self.root.after_cancel(self._toast_after_id)
            except Exception:
                pass
            self._toast_after_id = None
        if self._toast_win is not None:
            try:
                self._toast_win.destroy()
            except Exception:
                pass
            self._toast_win = None

        tw = tk.Toplevel(self.root)
        tw.withdraw()
        tw.wm_overrideredirect(True)
        tw.configure(bg="#1c1c1e")

        toast_w = self.px(368)
        outer = tk.Frame(
            tw, bg="#1c1c1e", padx=self.px(14), pady=self.px(12),
            highlightbackground="#3a3a3c", highlightthickness=1, cursor="hand2",
        )
        outer.pack(fill=tk.BOTH, expand=True)

        top_bar = tk.Frame(outer, bg="#1c1c1e")
        top_bar.pack(fill=tk.X)
        if badge_text is not None:
            eff_badge = badge_text
            eff_fg = badge_color or "#ff6961"
        else:
            if is_zone is None:
                is_zone = any(
                    k in detail_msg for k in ("经济线", "缓存已冷", "短任务可续", "可考虑换新", "EOQ", "cache cold", "big tasks")
                )
            pending_n = len(getattr(self, "_toast_queue", ()))
            more_s = tr("toast_more_suffix", self._lang, n=pending_n) if pending_n > 0 else ""
            eff_badge = tr("toast_pause_badge" if is_zone else "toast_switch_badge", self._lang) + more_s
            eff_fg = badge_color or ("#ffb340" if is_zone else "#ff6961")
        tk.Label(top_bar, text=eff_badge, bg="#1c1c1e", fg=eff_fg, font=(FONT_UI, 8, "bold")).pack(side=tk.LEFT)
        close_lbl = tk.Label(top_bar, text="✕", bg="#1c1c1e", fg="#98989d", font=(FONT_UI, 9), padx=self.px(4), cursor="hand2")
        close_lbl.pack(side=tk.RIGHT)
        close_lbl.bind("<Button-1>", lambda _e: self._dismiss_toast(show_next=True))

        hdr_lbl = tk.Label(
            outer, text=conv_header, bg="#1c1c1e", fg="#ffffff",
            font=(FONT_UI, 9, "bold"), anchor="w", justify="left", wraplength=toast_w - self.px(32),
        )
        hdr_lbl.pack(fill=tk.X, pady=(self.px(4), self.px(2)))
        body_lbl = tk.Label(
            outer, text=detail_msg, bg="#1c1c1e", fg="#d1d1d6",
            font=(FONT_UI, 8), anchor="w", justify="left", wraplength=toast_w - self.px(32),
        )
        body_lbl.pack(fill=tk.X)
        for w in (outer, hdr_lbl, body_lbl):
            w.bind("<Button-1>", lambda _e: self._focus_main_context_tab())

        tw.update_idletasks()
        req_h = max(self.px(88), tw.winfo_reqheight())
        work_r, work_b = self.root.winfo_screenwidth(), self.root.winfo_screenheight() - self.px(48)
        if IS_WINDOWS:
            try:
                rect = wintypes.RECT()
                if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                    work_r, work_b = rect.right, rect.bottom
            except Exception:
                pass
        elif IS_MACOS:
            from src.ui.macos_tray import get_macos_visible_frame

            vis = get_macos_visible_frame()
            if vis is not None:
                _, _, work_r, work_b = vis

        pos_x, pos_y = max(8, work_r - toast_w - self.px(18)), max(8, work_b - req_h - self.px(18))
        tw.geometry(f"{toast_w}x{req_h}+{pos_x}+{pos_y}")

        shown_noactivate = False
        if IS_WINDOWS:
            try:
                u32 = ctypes.windll.user32
                u32.GetParent.argtypes = [wintypes.HWND]
                u32.GetParent.restype = wintypes.HWND
                u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
                u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
                u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
                u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
                u32.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint
                ]
                u32.SetWindowPos.restype = wintypes.BOOL
                hwnd = u32.GetParent(tw.winfo_id()) or tw.winfo_id()
                u32.SetWindowLongPtrW(hwnd, -20, u32.GetWindowLongPtrW(hwnd, -20) | 0x08000088)
                tw.deiconify()
                u32.SetWindowPos(hwnd, wintypes.HWND(-1), pos_x, pos_y, toast_w, req_h, 0x0050)
                shown_noactivate = True
            except Exception:
                pass
        elif IS_MACOS:
            try:
                tw.tk.call("::tk::unsupported::MacWindowStyle", "style", tw._w, "help", "noActivates")
                tw.deiconify()
                tw.attributes("-topmost", True)
                shown_noactivate = True
            except Exception:
                pass

        if not shown_noactivate:
            tw.deiconify()
            tw.attributes("-topmost", True)

        self._toast_win = tw
        self._toast_after_id = self.root.after(TOAST_DURATION_MS, lambda: self._dismiss_toast(show_next=True))
