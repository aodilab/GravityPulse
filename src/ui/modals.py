"""Non-activating desktop switch toast, quota calibration modal, and rules tooltip."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import threading
import tkinter as tk

from config.settings import (
    LOCAL_TZ,
    BG_CARD,
    BG_PAGE,
    DEFAULT_ANTIGRAVITY_DIR,
    FONT_UI,
    HYDROGEN_INPUT_PER_M,
    ICON_PATH,
    INK,
    INK_2,
    INK_3,
    LINE_HEX,
    NEON_INPUT_PER_M,
    TOAST_DURATION_MS,
)
from src.core.models import QuotaConfig
from src.core.quota import (
    compute_quota_summary,
    resolve_current_cycle_anchor_start,
    save_quota_config,
)
from src.core.watcher import (
    ensure_watcher_running,
    install_startup_registry,
    is_startup_installed,
    uninstall_startup_registry,
)
from src.ui.i18n import localize_reset_str, save_ui_language, tr


def build_calibrated_quota_config(
    prev_cfg: QuotaConfig,
    now_bj: datetime,
    used_pct: float,
    mult: float,
    notify_switch: bool,
    reset_cycle: bool = False,
    antigravity_dir=DEFAULT_ANTIGRAVITY_DIR,
) -> QuotaConfig:
    """Build updated `QuotaConfig` from modal inputs, preserving history when resetting rolling cycle."""
    if reset_cycle:
        snap_cap = prev_cfg.weekly_cap_usd_override
        if snap_cap <= 0.0:
            snap_cap = compute_quota_summary(antigravity_dir, now_bj, now_bj, quota_config=prev_cfg).weekly_cap_usd
        return QuotaConfig(
            carbon_multiplier=mult,
            anchor_start=prev_cfg.anchor_start,
            anchor_end=prev_cfg.anchor_end,
            anchor_weekly_used_pct=prev_cfg.anchor_weekly_used_pct,
            subscription_start=prev_cfg.subscription_start,
            daily_divisor=prev_cfg.daily_divisor,
            weekly_cap_usd_override=snap_cap,
            notify_switch=notify_switch,
            rolling_reset_start=now_bj.isoformat(timespec="seconds"),
            carbon_multiplier_history=prev_cfg.carbon_multiplier_history,
        )

    eff_used = max(0.1, min(100.0, used_pct))
    cycle_start = resolve_current_cycle_anchor_start(prev_cfg, now_bj, antigravity_dir)
    temp_cfg = QuotaConfig(
        carbon_multiplier=mult,
        anchor_start=cycle_start,
        anchor_end=now_bj.isoformat(timespec="seconds"),
        anchor_weekly_used_pct=eff_used,
        subscription_start=prev_cfg.subscription_start,
        daily_divisor=prev_cfg.daily_divisor,
        weekly_cap_usd_override=0.0,
        notify_switch=notify_switch,
        rolling_reset_start=prev_cfg.rolling_reset_start,
        carbon_multiplier_history=prev_cfg.carbon_multiplier_history,
        five_hour_reset_start=prev_cfg.five_hour_reset_start,
    )
    cal_summary = compute_quota_summary(antigravity_dir, now_bj, now_bj, quota_config=temp_cfg)
    snap_cap = cal_summary.weekly_cap_usd if cal_summary.weekly_cap_usd > 0.0 else prev_cfg.weekly_cap_usd_override
    return QuotaConfig(
        carbon_multiplier=mult,
        anchor_start=cycle_start,
        anchor_end=now_bj.isoformat(timespec="seconds"),
        anchor_weekly_used_pct=eff_used,
        subscription_start=prev_cfg.subscription_start,
        daily_divisor=prev_cfg.daily_divisor,
        weekly_cap_usd_override=snap_cap,
        notify_switch=notify_switch,
        rolling_reset_start=prev_cfg.rolling_reset_start,
        carbon_multiplier_history=prev_cfg.carbon_multiplier_history,
        five_hour_reset_start=prev_cfg.five_hour_reset_start,
    )


class ModalMixin:
    """Modal dialogs, silent toast notifications, and hover tooltip handlers."""

    def _show_tooltip(self, _event: tk.Event) -> None:
        if self._tooltip_win is not None:
            return
        tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.configure(bg="#1c1c1e")

        rx = self.help_canvas.winfo_rootx()
        ry = self.help_canvas.winfo_rooty() + self.help_canvas.winfo_height() + self.px(8)

        box = tk.Frame(tw, bg="#1c1c1e", padx=self.px(14), pady=self.px(12), highlightbackground="#3a3a3c", highlightthickness=1)
        box.pack(fill=tk.BOTH, expand=True)

        tk.Label(box, text=tr("rules_title", self._lang), bg="#1c1c1e", fg="#ffffff", font=(FONT_UI, 9, "bold")).pack(anchor="w", pady=(0, self.px(4)))
        c_mult_s = f"{self._quota_cfg.carbon_multiplier:g}x"
        n_mult_s = f"{NEON_INPUT_PER_M / HYDROGEN_INPUT_PER_M:g}x"
        rules = tr("rules_body", self._lang, c_mult=c_mult_s, n_mult=n_mult_s)
        tk.Label(box, text=rules, bg="#1c1c1e", fg="#e5e5ea", justify="left", font=(FONT_UI, 9)).pack(anchor="w")

        tw.update_idletasks()
        tip_w = tw.winfo_reqwidth()
        sw = self.root.winfo_screenwidth()
        x = max(8, min(sw - tip_w - 8, rx + self.help_canvas.winfo_width() - tip_w))
        tw.geometry(f"+{x}+{ry}")
        self._tooltip_win = tw

    def _hide_tooltip(self, _event: tk.Event | None = None) -> None:
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None

    def _open_calibration_dialog(self) -> None:
        if self._calib_win is not None and self._calib_win.winfo_exists():
            self._calib_win.lift()
            return

        win = tk.Toplevel(self.root)
        win.title(tr("calib_win_title", self._lang))
        win.configure(bg=BG_PAGE)
        win.resizable(False, False)
        if ICON_PATH.exists():
            try:
                win.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass
        win.transient(self.root)
        win.grab_set()

        pad = self.px(18)
        card = tk.Frame(win, bg=BG_CARD, padx=pad, pady=pad, highlightbackground=LINE_HEX, highlightthickness=1)
        card.pack(padx=self.px(14), pady=self.px(14))

        cur_q = (self._current_payload or {}).get("quota", {})
        cur_calibrated = bool(cur_q.get("calibrated"))
        cur_cycle_used = float(cur_q.get("cycle_used_pct", self._quota_cfg.anchor_weekly_used_pct))
        cur_cycle_left = float(cur_q.get("cycle_left_pct", max(0.0, 100.0 - cur_cycle_used)))
        cur_cycle_reset = localize_reset_str(str(cur_q.get("cycle_reset_str", "")), self._lang)
        cur_5h_left = float(cur_q.get("five_hour_left_pct", 100.0))

        tk.Label(card, text=tr("calib_title", self._lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 11, "bold")).pack(anchor="w")
        sub_hint = tr("calib_hint", self._lang)
        if cur_calibrated:
            sep = "，" if self._lang == "zh" else ", "
            reset_part = f"{sep}{cur_cycle_reset}" if cur_cycle_reset else ""
            sub_hint += "\n" + tr("calib_cur", self._lang, left=cur_cycle_left, used=cur_cycle_used, reset=reset_part, fh=cur_5h_left)
        tk.Label(card, text=sub_hint, bg=BG_CARD, fg=INK_3, justify="left", font=(FONT_UI, 8)).pack(anchor="w", pady=(self.px(3), self.px(12)))

        default_used = round(cur_cycle_used, 1) if cur_calibrated and cur_cycle_used >= 0.1 else self._quota_cfg.anchor_weekly_used_pct
        used_var = tk.StringVar(value=f"{default_used:g}")
        mult_var = tk.StringVar(value=f"{self._quota_cfg.carbon_multiplier:g}")
        reset_cycle_var = tk.BooleanVar(value=False)
        notify_var = tk.BooleanVar(value=bool(self._quota_cfg.notify_switch))

        row1 = tk.Frame(card, bg=BG_CARD)
        row1.pack(fill=tk.X, pady=(0, self.px(8)))
        tk.Label(row1, text=tr("calib_used_lbl", self._lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 9)).pack(side=tk.LEFT)
        tk.Entry(row1, textvariable=used_var, width=10, font=(FONT_UI, 9), relief="solid", bd=1, justify="right").pack(side=tk.RIGHT)

        reset_row = tk.Frame(card, bg=BG_CARD)
        reset_row.pack(fill=tk.X, pady=(0, self.px(8)))
        tk.Checkbutton(
            reset_row, text=tr("calib_reset_chk", self._lang),
            variable=reset_cycle_var, bg=BG_CARD, fg=INK_2, activebackground=BG_CARD, selectcolor=BG_CARD,
            font=(FONT_UI, 8), cursor="hand2",
        ).pack(side=tk.LEFT)

        row2 = tk.Frame(card, bg=BG_CARD)
        row2.pack(fill=tk.X, pady=(0, self.px(8)))
        tk.Label(row2, text=tr("calib_mult_lbl", self._lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 9)).pack(side=tk.LEFT)
        tk.Entry(row2, textvariable=mult_var, width=10, font=(FONT_UI, 9), relief="solid", bd=1, justify="right").pack(side=tk.RIGHT)

        preset_row = tk.Frame(card, bg=BG_CARD)
        preset_row.pack(fill=tk.X, pady=(0, self.px(10)))
        tk.Label(preset_row, text=tr("calib_preset_lbl", self._lang), bg=BG_CARD, fg=INK_3, font=(FONT_UI, 8)).pack(side=tk.LEFT, padx=(0, self.px(6)))
        for val_str, lbl in (("1.3333", "1.33x"), ("2.0", "2.0x"), ("2.6667", "2.67x"), ("3.0", tr("calib_preset_measured", self._lang))):
            tk.Button(
                preset_row, text=lbl, command=lambda v=val_str: mult_var.set(v),
                bg=BG_PAGE, fg=INK_2, bd=0, padx=self.px(8), pady=self.px(2), font=(FONT_UI, 8), cursor="hand2",
            ).pack(side=tk.LEFT, padx=(0, self.px(5)))

        notify_row = tk.Frame(card, bg=BG_CARD)
        notify_row.pack(fill=tk.X, pady=(0, self.px(12)))
        tk.Checkbutton(
            notify_row, text=tr("calib_notify_chk", self._lang),
            variable=notify_var, bg=BG_CARD, fg=INK, activebackground=BG_CARD, selectcolor=BG_CARD, font=(FONT_UI, 8), cursor="hand2",
        ).pack(side=tk.LEFT)
        tk.Button(
            notify_row, text=tr("calib_preview", self._lang),
            command=lambda: self._show_switch_toast(
                tr("calib_preview_head", self._lang),
                tr("calib_preview_body", self._lang),
            ),
            bg=BG_PAGE, fg=INK_2, bd=0, padx=self.px(8), pady=self.px(2), font=(FONT_UI, 8), cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(self.px(8), 0))

        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill=tk.X)

        def on_save() -> None:
            try:
                raw_used = used_var.get().strip().rstrip("%").strip()
                raw_mult = mult_var.get().strip().rstrip("xX").strip()
                is_reset = bool(reset_cycle_var.get())
                used_pct = max(0.0 if is_reset else 0.1, min(100.0, float(raw_used)))
                mult = max(0.5, min(20.0, float(raw_mult)))
            except ValueError:
                return
            now_bj = datetime.now(LOCAL_TZ)
            new_cfg = build_calibrated_quota_config(
                self._quota_cfg,
                now_bj=now_bj,
                used_pct=used_pct,
                mult=mult,
                notify_switch=bool(notify_var.get()),
                reset_cycle=is_reset,
            )
            save_quota_config(new_cfg)
            self._quota_cfg = new_cfg
            win.destroy()
            self.refresh(force_recompute=True, schedule_next=False)

        tk.Button(
            btn_row, text=tr("btn_cancel", self._lang), command=win.destroy,
            bg=BG_PAGE, fg=INK_2, bd=0, padx=self.px(14), pady=self.px(5), font=(FONT_UI, 9), cursor="hand2",
        ).pack(side=tk.LEFT)
        tk.Button(
            btn_row, text=tr("btn_save", self._lang), command=on_save,
            bg=INK, fg="#ffffff", bd=0, padx=self.px(16), pady=self.px(5), font=(FONT_UI, 9, "bold"), cursor="hand2",
        ).pack(side=tk.RIGHT)

        win.update_idletasks()
        px_x = self.root.winfo_rootx() + (self.win_w - win.winfo_width()) // 2
        px_y = self.root.winfo_rooty() + (self.win_h - win.winfo_height()) // 2
        win.geometry(f"+{max(0, px_x)}+{max(0, px_y)}")
        self._calib_win = win

    def _dismiss_toast(self) -> None:
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

    def _focus_main_context_tab(self) -> None:
        self._dismiss_toast()
        if self.view_mode == "day":
            self.day_tab = "ctx"
        else:
            self.week_tab = "ctx"
        if self._current_payload is not None:
            self._render_tabs_and_rows(self._current_payload, preserve_scroll=False)
        self.show_window()

    def _get_tray_menu_state(self) -> dict[str, object]:
        return {"lang": self._lang, "startup_enabled": is_startup_installed()}

    def _toggle_language(self) -> None:
        self._lang = save_ui_language("en" if self._lang == "zh" else "zh")
        self._hide_tooltip()
        self._apply_language_static_ui()
        if self._current_payload is not None:
            self._apply_payload(self._current_payload, preserve_scroll=True)

    def _handle_tray_action(self, cmd: str) -> None:
        if cmd == "toggle_lang":
            self._toggle_language()
        elif cmd == "toggle_startup":
            try:
                if is_startup_installed():
                    uninstall_startup_registry()
                    self._show_switch_toast(
                        tr("toast_startup_off_head", self._lang),
                        tr("toast_startup_off_body", self._lang),
                        badge_text=tr("toast_info_badge", self._lang),
                        badge_color="#34c759",
                    )
                else:
                    install_startup_registry()
                    ensure_watcher_running()
                    self._show_switch_toast(
                        tr("toast_startup_on_head", self._lang),
                        tr("toast_startup_on_body", self._lang),
                        badge_text=tr("toast_info_badge", self._lang),
                        badge_color="#34c759",
                    )
            except Exception as exc:
                self._show_switch_toast(
                    tr("toast_action_err_head", self._lang), str(exc),
                    badge_text=tr("toast_info_badge", self._lang), badge_color="#ff6961",
                )
        elif cmd == "create_shortcut":
            lang = self._lang

            def _make() -> None:
                try:
                    from scripts.icon_builder import create_desktop_shortcut

                    create_desktop_shortcut()
                    self.root.after(
                        0,
                        lambda: self._show_switch_toast(
                            tr("toast_shortcut_head", lang),
                            tr("toast_shortcut_body", lang),
                            badge_text=tr("toast_info_badge", lang),
                            badge_color="#34c759",
                        ),
                    )
                except Exception as exc:
                    err = str(exc)
                    self.root.after(
                        0,
                        lambda: self._show_switch_toast(
                            tr("toast_action_err_head", lang), err,
                            badge_text=tr("toast_info_badge", lang), badge_color="#ff6961",
                        ),
                    )

            threading.Thread(target=_make, daemon=True).start()

    def _show_switch_toast(
        self,
        conv_header: str,
        detail_msg: str,
        badge_text: str | None = None,
        badge_color: str = "#ff6961",
    ) -> None:
        """Render a silent, non-activating bottom-right visual popup without system sound."""
        self._dismiss_toast()

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
        badge = badge_text or tr("toast_switch_badge", self._lang)
        tk.Label(top_bar, text=badge, bg="#1c1c1e", fg=badge_color, font=(FONT_UI, 8, "bold")).pack(side=tk.LEFT)
        close_lbl = tk.Label(top_bar, text="✕", bg="#1c1c1e", fg="#98989d", font=(FONT_UI, 9), padx=self.px(4), cursor="hand2")
        close_lbl.pack(side=tk.RIGHT)
        close_lbl.bind("<Button-1>", lambda _e: self._dismiss_toast())

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

        work_r = self.root.winfo_screenwidth()
        work_b = self.root.winfo_screenheight() - self.px(48)
        try:
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                work_r, work_b = rect.right, rect.bottom
        except Exception:
            pass

        pos_x = max(8, work_r - toast_w - self.px(18))
        pos_y = max(8, work_b - req_h - self.px(18))
        tw.geometry(f"{toast_w}x{req_h}+{pos_x}+{pos_y}")

        shown_noactivate = False
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
            ex_style = u32.GetWindowLongPtrW(hwnd, -20)
            u32.SetWindowLongPtrW(hwnd, -20, ex_style | 0x08000088)
            tw.deiconify()
            u32.SetWindowPos(hwnd, wintypes.HWND(-1), pos_x, pos_y, toast_w, req_h, 0x0050)
            shown_noactivate = True
        except Exception:
            pass

        if not shown_noactivate:
            tw.deiconify()
            tw.attributes("-topmost", True)

        self._toast_win = tw
        self._toast_after_id = self.root.after(TOAST_DURATION_MS, self._dismiss_toast)
