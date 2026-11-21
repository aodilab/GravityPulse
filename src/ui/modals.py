"""Non-activating desktop switch toast, quota calibration modal, and rules tooltip."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
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

        tk.Label(box, text="统计与常驻规则", bg="#1c1c1e", fg="#ffffff", font=(FONT_UI, 9, "bold")).pack(anchor="w", pady=(0, self.px(4)))
        c_mult_s = f"{self._quota_cfg.carbon_multiplier:g}x"
        n_mult_s = f"{NEON_INPUT_PER_M / HYDROGEN_INPUT_PER_M:g}x"
        rules = (
            "1. 随 Antigravity 2.0 自动启动并后台常驻；点击右上角 ✕ 最小化至系统托盘，\n"
            "   秒级实时计时，数据变更即时增量同步，右键托盘图标可彻底退出。\n"
            "2. 工作时段严格以你本人发送消息的时间点为起止端点，最后一条消息后不加后台尾巴；\n"
            "   相邻两次发消息无任务时 ≤ 30 分钟连入工时，有对话框任务运行（且任务停后空闲 ≤ 30 分钟）\n"
            "   放宽至 ≤ 1 小时自动连入，孤立单条消息计 0 秒工时（额度正常统计）。\n"
            "3. 未绑定工作区不单独展示项目，但正常计入总工时与总额度。\n"
            f"4. 额度按 Carbon {c_mult_s} / Neon {n_mult_s} / Hydrogen / 其他折算；「周余」与「5h余」实时同步官方滚动窗口。\n"
            "5. 换窗与冷缓存损益算法（静音弹窗）：\n"
            "   · 512K 下 Carbon 压缩 1次或达 330K、Neon 压缩 1次或达 350K、Hydrogen 压缩 1次或达 400K 立即提醒换新\n"
            "     （运行中超界限亦立即提醒，单轮内若上下文续增 ≥ 50K 再补弹第 2 次；最多 2 次，新压缩或发新消息后重置）；\n"
            "   · 自动识别子代理与后台任务运行态，执行期间不误弹冷缓存提醒；\n"
            "   · 闲置致缓存失效（Carbon ≥ 8分 / Neon ≥ 7分 / Hydrogen ≥ 20分）时对比冷重算与新开窗成本：\n"
            "     短对话续聊更省则保持健康不打扰；长对话重算明显更贵时仅静音提醒 1 次。"
        )
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
        win.title("额度与提醒设置")
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
        cur_cycle_reset = str(cur_q.get("cycle_reset_str", ""))
        cur_5h_left = float(cur_q.get("five_hour_left_pct", 100.0))

        tk.Label(card, text="额度校准与换窗提醒", bg=BG_CARD, fg=INK, font=(FONT_UI, 11, "bold")).pack(anchor="w")
        sub_hint = "对照官方 Quota 面板 Weekly limit（如 80% left 即填已用 20%），保存立即生效。"
        if cur_calibrated:
            reset_part = f"，{cur_cycle_reset}" if cur_cycle_reset else ""
            sub_hint += f"\n当前测算：周余 {cur_cycle_left:.1f}%（已用 {cur_cycle_used:.1f}%{reset_part}） · 5h余 {cur_5h_left:.1f}%"
        tk.Label(card, text=sub_hint, bg=BG_CARD, fg=INK_3, justify="left", font=(FONT_UI, 8)).pack(anchor="w", pady=(self.px(3), self.px(12)))

        default_used = round(cur_cycle_used, 1) if cur_calibrated and cur_cycle_used >= 0.1 else self._quota_cfg.anchor_weekly_used_pct
        used_var = tk.StringVar(value=f"{default_used:g}")
        mult_var = tk.StringVar(value=f"{self._quota_cfg.carbon_multiplier:g}")
        reset_cycle_var = tk.BooleanVar(value=False)
        notify_var = tk.BooleanVar(value=bool(self._quota_cfg.notify_switch))

        row1 = tk.Frame(card, bg=BG_CARD)
        row1.pack(fill=tk.X, pady=(0, self.px(8)))
        tk.Label(row1, text="当前 7 天周期已用 (%)", bg=BG_CARD, fg=INK, font=(FONT_UI, 9)).pack(side=tk.LEFT)
        tk.Entry(row1, textvariable=used_var, width=10, font=(FONT_UI, 9), relief="solid", bd=1, justify="right").pack(side=tk.RIGHT)

        reset_row = tk.Frame(card, bg=BG_CARD)
        reset_row.pack(fill=tk.X, pady=(0, self.px(8)))
        tk.Checkbutton(
            reset_row, text="官方已提前重置周额度（保留上限，从此刻重开新 7 天周期）",
            variable=reset_cycle_var, bg=BG_CARD, fg=INK_2, activebackground=BG_CARD, selectcolor=BG_CARD,
            font=(FONT_UI, 8), cursor="hand2",
        ).pack(side=tk.LEFT)

        row2 = tk.Frame(card, bg=BG_CARD)
        row2.pack(fill=tk.X, pady=(0, self.px(8)))
        tk.Label(row2, text="Carbon 相对 Hydrogen 倍率", bg=BG_CARD, fg=INK, font=(FONT_UI, 9)).pack(side=tk.LEFT)
        tk.Entry(row2, textvariable=mult_var, width=10, font=(FONT_UI, 9), relief="solid", bd=1, justify="right").pack(side=tk.RIGHT)

        preset_row = tk.Frame(card, bg=BG_CARD)
        preset_row.pack(fill=tk.X, pady=(0, self.px(10)))
        tk.Label(preset_row, text="常用倍率:", bg=BG_CARD, fg=INK_3, font=(FONT_UI, 8)).pack(side=tk.LEFT, padx=(0, self.px(6)))
        for val_str, lbl in (("1.3333", "1.33x"), ("2.0", "2.0x"), ("2.6667", "2.67x"), ("3.0", "3.0x (实测)")):
            tk.Button(
                preset_row, text=lbl, command=lambda v=val_str: mult_var.set(v),
                bg=BG_PAGE, fg=INK_2, bd=0, padx=self.px(8), pady=self.px(2), font=(FONT_UI, 8), cursor="hand2",
            ).pack(side=tk.LEFT, padx=(0, self.px(5)))

        notify_row = tk.Frame(card, bg=BG_CARD)
        notify_row.pack(fill=tk.X, pady=(0, self.px(12)))
        tk.Checkbutton(
            notify_row, text="对话触发「建议换新」时右下角弹窗提醒（不抢输入焦点）",
            variable=notify_var, bg=BG_CARD, fg=INK, activebackground=BG_CARD, selectcolor=BG_CARD, font=(FONT_UI, 8), cursor="hand2",
        ).pack(side=tk.LEFT)
        tk.Button(
            notify_row, text="预览提醒",
            command=lambda: self._show_switch_toast(
                "GravityPulse · 换窗提醒预览 (Hydrogen)",
                "Hydrogen 已累计压缩 2 次（继续压缩与推理损耗已超新建窗口成本），建议本轮结束后换新窗口清理旧问题包袱。",
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
            btn_row, text="取消", command=win.destroy,
            bg=BG_PAGE, fg=INK_2, bd=0, padx=self.px(14), pady=self.px(5), font=(FONT_UI, 9), cursor="hand2",
        ).pack(side=tk.LEFT)
        tk.Button(
            btn_row, text="保存并应用", command=on_save,
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

    def _show_switch_toast(self, conv_header: str, detail_msg: str) -> None:
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
        tk.Label(top_bar, text="● 换窗建议提醒", bg="#1c1c1e", fg="#ff6961", font=(FONT_UI, 8, "bold")).pack(side=tk.LEFT)
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
