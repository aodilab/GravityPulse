"""Static widget shell, DPI helpers, incremental property caches, and spinner/fade transitions."""
from __future__ import annotations

import math
import time
import tkinter as tk

from config.settings import (
    BG_CARD,
    BG_PAGE,
    FONT_UI,
    GREEN_BG,
    INK,
    INK_2,
    INK_3,
    LINE_HEX,
)
from src.ui.primitives import (
    make_brand_badge_image,
    make_chevron_icon,
    make_pill_image,
    make_refresh_icon,
    make_rounded_card_image,
)


class LayoutMixin:
    """Builds the static Tkinter shell and handles 12-spoke loader + alpha fade transitions."""

    def _cfg(self, widget: tk.Widget, **kwargs: object) -> None:
        """Configure widget properties only when values actually changed (eliminates GDI flicker)."""
        wid = id(widget)
        changed: dict[str, object] = {}
        for k, v in kwargs.items():
            key = (wid, k)
            if self._widget_cache.get(key) != v:
                self._widget_cache[key] = v
                changed[k] = v
        if changed:
            widget.configure(**changed)

    def _itemcfg(self, canvas: tk.Canvas, item_id: int, **kwargs: object) -> None:
        """Configure canvas item properties only when values actually changed."""
        cid = id(canvas)
        changed: dict[str, object] = {}
        for k, v in kwargs.items():
            key = (cid, item_id, k)
            if self._canvas_item_cache.get(key) != v:
                self._canvas_item_cache[key] = v
                changed[k] = v
        if changed:
            canvas.itemconfigure(item_id, **changed)

    def _set_today_btn_visible(self, visible: bool) -> None:
        if visible == self._today_btn_packed:
            return
        self._today_btn_packed = visible
        if visible:
            self.today_btn_canvas.pack(side=tk.LEFT, padx=(0, self.px(8)), before=self.calib_canvas)
        else:
            self.today_btn_canvas.pack_forget()

    def px(self, logical: float) -> int:
        return max(1, int(round(logical * self.scale)))

    def _center_window(self) -> None:
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - self.win_w) // 2)
        y = max(0, (sh - self.win_h) // 2)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")

    def _build_ui(self) -> None:
        nav_h = self.px(58)
        nav_wrap = tk.Frame(self.root, bg=BG_PAGE, height=nav_h)
        nav_wrap.pack(fill=tk.X)
        nav_wrap.pack_propagate(False)

        nav_inner = tk.Frame(nav_wrap, bg=BG_PAGE, width=self.content_w, height=nav_h)
        nav_inner.place(relx=0.5, rely=0.5, anchor="center")
        nav_inner.pack_propagate(False)

        brand = tk.Frame(nav_inner, bg=BG_PAGE)
        brand.pack(side=tk.LEFT, pady=self.px(12))

        self._imgs["badge"] = make_brand_badge_image(self.px(28))
        tk.Label(brand, image=self._imgs["badge"], bg=BG_PAGE, bd=0).pack(side=tk.LEFT, padx=(0, self.px(10)))
        tk.Label(brand, text="GravityPulse", bg=BG_PAGE, fg=INK, font=(FONT_UI, 11, "bold")).pack(side=tk.LEFT)

        actions = tk.Frame(nav_inner, bg=BG_PAGE)
        actions.pack(side=tk.RIGHT)

        mw, mh = self.px(88), self.px(34)
        self._imgs["mode_outer"] = make_pill_image(mw, mh, fill=BG_CARD, bg=BG_PAGE, shadow=True)
        self._imgs["mode_active"] = make_pill_image(self.px(40), self.px(28), fill=INK, bg=BG_CARD)
        self.mode_canvas = tk.Canvas(
            actions, width=mw + self.px(6), height=mh + self.px(6),
            bg=BG_PAGE, bd=0, highlightthickness=0, cursor="hand2",
        )
        self.mode_canvas.pack(side=tk.LEFT, padx=(0, self.px(8)))
        self._draw_mode_switch()
        self.mode_canvas.bind("<Button-1>", self._on_mode_click)

        dp_w, dp_h = self.px(236), self.px(34)
        self._imgs["date_pill"] = make_pill_image(dp_w, dp_h, fill=BG_CARD, bg=BG_PAGE, shadow=True)
        self._imgs["chev_l"] = make_chevron_icon(self.px(14), "left", INK_2, BG_CARD)
        self._imgs["chev_l_dis"] = make_chevron_icon(self.px(14), "left", "#c7c7cc", BG_CARD)
        self._imgs["chev_r"] = make_chevron_icon(self.px(14), "right", INK_2, BG_CARD)
        self._imgs["chev_r_dis"] = make_chevron_icon(self.px(14), "right", "#c7c7cc", BG_CARD)

        self.date_canvas = tk.Canvas(
            actions, width=dp_w + self.px(6), height=dp_h + self.px(6),
            bg=BG_PAGE, bd=0, highlightthickness=0, cursor="hand2",
        )
        self.date_canvas.pack(side=tk.LEFT, padx=(0, self.px(8)))
        cw, ch = dp_w + self.px(6), dp_h + self.px(6)
        self.date_canvas.create_image(cw // 2, ch // 2, image=self._imgs["date_pill"])
        self.date_prev_icon = self.date_canvas.create_image(self.px(22), ch // 2, image=self._imgs["chev_l"])
        self.date_text_item = self.date_canvas.create_text(
            cw // 2, ch // 2, text="加载中...", fill=INK, font=(FONT_UI, 9, "bold"),
        )
        self.date_next_icon = self.date_canvas.create_image(cw - self.px(22), ch // 2, image=self._imgs["chev_r_dis"])
        self.date_canvas.bind("<Button-1>", self._on_date_pill_click)

        tp_w, tp_h = self.px(78), self.px(34)
        self._imgs["today_pill"] = make_pill_image(tp_w, tp_h, fill=BG_CARD, bg=BG_PAGE, shadow=True)
        self.today_btn_canvas = tk.Canvas(
            actions, width=tp_w + self.px(6), height=tp_h + self.px(6),
            bg=BG_PAGE, bd=0, highlightthickness=0, cursor="hand2",
        )
        self.today_btn_canvas.create_image((tp_w + self.px(6)) // 2, (tp_h + self.px(6)) // 2, image=self._imgs["today_pill"])
        self.today_btn_text = self.today_btn_canvas.create_text(
            (tp_w + self.px(6)) // 2, (tp_h + self.px(6)) // 2, text="回到今天", fill=INK_2, font=(FONT_UI, 9),
        )
        self.today_btn_canvas.bind("<Button-1>", lambda _e: self._go_current())

        cb_w, cb_h = self.px(66), self.px(34)
        self._imgs["calib_pill"] = make_pill_image(cb_w, cb_h, fill=BG_CARD, bg=BG_PAGE, shadow=True)
        self.calib_canvas = tk.Canvas(
            actions, width=cb_w + self.px(6), height=cb_h + self.px(6),
            bg=BG_PAGE, bd=0, highlightthickness=0, cursor="hand2",
        )
        self.calib_canvas.pack(side=tk.LEFT, padx=(0, self.px(8)))
        self.calib_canvas.create_image((cb_w + self.px(6)) // 2, (cb_h + self.px(6)) // 2, image=self._imgs["calib_pill"])
        self.calib_canvas.create_text(
            (cb_w + self.px(6)) // 2, (cb_h + self.px(6)) // 2, text="校准", fill=INK_2, font=(FONT_UI, 9, "bold"),
        )
        self.calib_canvas.bind("<Button-1>", lambda _e: self._open_calibration_dialog())

        rb_w, rb_h = self.px(78), self.px(34)
        self._imgs["refresh_pill"] = make_pill_image(rb_w, rb_h, fill=INK, bg=BG_PAGE)
        self._imgs["refresh_icon"] = make_refresh_icon(self.px(14), "#ffffff", INK)
        self.refresh_canvas = tk.Canvas(
            actions, width=rb_w, height=rb_h, bg=BG_PAGE, bd=0, highlightthickness=0, cursor="hand2",
        )
        self.refresh_canvas.pack(side=tk.LEFT, padx=(0, self.px(8)))
        self.refresh_canvas.create_image(rb_w // 2, rb_h // 2, image=self._imgs["refresh_pill"])
        self.refresh_canvas.create_image(self.px(22), rb_h // 2, image=self._imgs["refresh_icon"])
        self.refresh_canvas.create_text(self.px(49), rb_h // 2, text="刷新", fill="#ffffff", font=(FONT_UI, 9, "bold"))
        self.refresh_canvas.bind("<Button-1>", lambda _e: self.refresh(force_recompute=True, schedule_next=True))

        hb_s = self.px(32)
        self._imgs["help_pill"] = make_pill_image(hb_s, hb_s, fill="#e8e8ed", bg=BG_PAGE)
        self.help_canvas = tk.Canvas(
            actions, width=hb_s, height=hb_s, bg=BG_PAGE, bd=0, highlightthickness=0, cursor="hand2",
        )
        self.help_canvas.pack(side=tk.LEFT)
        self.help_canvas.create_image(hb_s // 2, hb_s // 2, image=self._imgs["help_pill"])
        self.help_canvas.create_text(hb_s // 2, hb_s // 2, text="?", fill=INK_2, font=(FONT_UI, 9, "bold"))
        self.help_canvas.bind("<Enter>", self._show_tooltip)
        self.help_canvas.bind("<Leave>", self._hide_tooltip)

        tk.Frame(self.root, bg="#e2e2e7", height=1).pack(fill=tk.X)

        page = tk.Frame(self.root, bg=BG_PAGE)
        page.pack(fill=tk.BOTH, expand=True)
        self.page = page

        hero_h = self.px(204)
        self._imgs["hero_card"] = make_rounded_card_image(
            self.content_w, hero_h, radius=self.px(20), shadow_pad=self.shadow_pad
        )
        hero_host = tk.Canvas(
            page, width=self.content_w + self.shadow_pad * 2, height=hero_h + self.shadow_pad * 2,
            bg=BG_PAGE, bd=0, highlightthickness=0,
        )
        hero_host.pack(pady=(self.px(10), self.px(2)))
        hero_host.create_image(
            (self.content_w + self.shadow_pad * 2) // 2,
            (hero_h + self.shadow_pad * 2) // 2,
            image=self._imgs["hero_card"],
        )

        hero_inner = tk.Frame(hero_host, bg=BG_CARD, width=self.content_w - self.px(56), height=hero_h - self.px(32))
        hero_inner.place(x=self.shadow_pad + self.px(28), y=self.shadow_pad + self.px(16))
        hero_inner.pack_propagate(False)

        hero_top = tk.Frame(hero_inner, bg=BG_CARD)
        hero_top.pack(fill=tk.X)

        self.hero_eyebrow = tk.Label(hero_top, text="今日累计工作时间", bg=BG_CARD, fg=INK_2, font=(FONT_UI, 9))
        self.hero_eyebrow.pack(side=tk.LEFT)

        sp_w, sp_h = self.px(138), self.px(26)
        self._imgs["pill_run"] = make_pill_image(sp_w, sp_h, fill=GREEN_BG, bg=BG_CARD)
        self._imgs["pill_idle"] = make_pill_image(sp_w, sp_h, fill=BG_PAGE, bg=BG_CARD)
        self.status_canvas = tk.Canvas(hero_top, width=sp_w, height=sp_h, bg=BG_CARD, bd=0, highlightthickness=0)
        self.status_canvas.pack(side=tk.RIGHT)
        self.status_bg_item = self.status_canvas.create_image(sp_w // 2, sp_h // 2, image=self._imgs["pill_idle"])
        dot_r = self.px(3.5)
        self.status_dot_item = self.status_canvas.create_oval(
            self.px(14) - dot_r, sp_h // 2 - dot_r, self.px(14) + dot_r, sp_h // 2 + dot_r, fill=INK_3, outline="",
        )
        self.status_text_item = self.status_canvas.create_text(
            self.px(24), sp_h // 2, anchor="w", text="当前空闲", fill=INK_2, font=(FONT_UI, 8, "bold"),
        )

        metric_row = tk.Frame(hero_inner, bg=BG_CARD)
        metric_row.pack(fill=tk.X, pady=(self.px(4), self.px(2)))

        self.total_human_lbl = tk.Label(metric_row, text="0 分钟", bg=BG_CARD, fg=INK, font=(FONT_UI, 26, "bold"))
        self.total_human_lbl.pack(side=tk.LEFT)

        self.total_hms_lbl = tk.Label(
            metric_row, text="00:00:00", bg=BG_CARD, fg=INK_3, font=(FONT_UI, 12), padx=self.px(12),
        )
        self.total_hms_lbl.pack(side=tk.LEFT, anchor="s", pady=(0, self.px(7)))

        quota_box = tk.Frame(metric_row, bg=BG_CARD)
        quota_box.pack(side=tk.RIGHT, anchor="s", pady=(0, self.px(4)))
        self.hero_quota_main = tk.Label(
            quota_box, text="额度统计准备中", bg=BG_CARD, fg=INK, font=(FONT_UI, 10, "bold"), anchor="e",
        )
        self.hero_quota_main.pack(anchor="e")
        self.hero_quota_sub = tk.Label(quota_box, text="", bg=BG_CARD, fg=INK_3, font=(FONT_UI, 8), anchor="e")
        self.hero_quota_sub.pack(anchor="e", pady=(self.px(2), 0))

        self.hero_meta_lbl = tk.Label(hero_inner, text="暂无工作记录", bg=BG_CARD, fg=INK_2, font=(FONT_UI, 9))
        self.hero_meta_lbl.pack(anchor="w", pady=(self.px(2), self.px(6)))

        self.tl_w = self.content_w - self.px(56)
        self.list_w = self.tl_w - self.px(14)
        self.hero_viz = tk.Canvas(hero_inner, width=self.tl_w, height=self.px(66), bg=BG_CARD, bd=0, highlightthickness=0)
        self.hero_viz.pack(anchor="w", fill=tk.X)
        self.hero_viz.bind("<Button-1>", self._on_week_bar_click)

        detail_h = self.px(446)
        self._imgs["detail_card"] = make_rounded_card_image(
            self.content_w, detail_h, radius=self.px(20), shadow_pad=self.shadow_pad
        )
        detail_host = tk.Canvas(
            page, width=self.content_w + self.shadow_pad * 2, height=detail_h + self.shadow_pad * 2,
            bg=BG_PAGE, bd=0, highlightthickness=0,
        )
        detail_host.pack()
        detail_host.create_image(
            (self.content_w + self.shadow_pad * 2) // 2,
            (detail_h + self.shadow_pad * 2) // 2,
            image=self._imgs["detail_card"],
        )

        detail_inner = tk.Frame(
            detail_host, bg=BG_CARD, width=self.content_w - self.px(56), height=detail_h - self.px(36),
        )
        detail_inner.place(x=self.shadow_pad + self.px(28), y=self.shadow_pad + self.px(18))
        detail_inner.pack_propagate(False)

        tab_bar = tk.Frame(detail_inner, bg=BG_CARD)
        tab_bar.pack(fill=tk.X)

        tab_left = tk.Frame(tab_bar, bg=BG_CARD)
        tab_left.pack(side=tk.LEFT)

        self.tab1_wrap = tk.Frame(tab_left, bg=BG_CARD, cursor="hand2")
        self.tab1_wrap.pack(side=tk.LEFT, padx=(0, self.px(22)))
        self.tab1_lbl = tk.Label(self.tab1_wrap, text="对话框", bg=BG_CARD, fg=INK, font=(FONT_UI, 10, "bold"), pady=self.px(6))
        self.tab1_lbl.pack()
        self.tab1_line = tk.Frame(self.tab1_wrap, bg=INK, height=self.px(2))
        self.tab1_line.pack(fill=tk.X)

        self.tab2_wrap = tk.Frame(tab_left, bg=BG_CARD, cursor="hand2")
        self.tab2_wrap.pack(side=tk.LEFT, padx=(0, self.px(22)))
        self.tab2_lbl = tk.Label(self.tab2_wrap, text="项目明细", bg=BG_CARD, fg=INK_3, font=(FONT_UI, 10), pady=self.px(6))
        self.tab2_lbl.pack()
        self.tab2_line = tk.Frame(self.tab2_wrap, bg=BG_CARD, height=self.px(2))
        self.tab2_line.pack(fill=tk.X)

        self.tab3_wrap = tk.Frame(tab_left, bg=BG_CARD, cursor="hand2")
        self.tab3_wrap.pack(side=tk.LEFT)
        self.tab3_lbl = tk.Label(self.tab3_wrap, text="工作时段", bg=BG_CARD, fg=INK_3, font=(FONT_UI, 10), pady=self.px(6))
        self.tab3_lbl.pack()
        self.tab3_line = tk.Frame(self.tab3_wrap, bg=BG_CARD, height=self.px(2))
        self.tab3_line.pack(fill=tk.X)

        for w in (self.tab1_wrap, self.tab1_lbl):
            w.bind("<Button-1>", lambda _e: self._on_tab_select(0))
        for w in (self.tab2_wrap, self.tab2_lbl):
            w.bind("<Button-1>", lambda _e: self._on_tab_select(1))
        for w in (self.tab3_wrap, self.tab3_lbl):
            w.bind("<Button-1>", lambda _e: self._on_tab_select(2))

        self.updated_lbl = tk.Label(
            tab_bar, text="更新于 --:--:-- · 实时同步中", bg=BG_CARD, fg=INK_3, font=(FONT_UI, 8),
        )
        self.updated_lbl.pack(side=tk.RIGHT, pady=self.px(6))

        tk.Frame(detail_inner, bg=LINE_HEX, height=1).pack(fill=tk.X)

        self.head_canvas = tk.Canvas(detail_inner, width=self.tl_w, height=self.px(32), bg=BG_CARD, bd=0, highlightthickness=0)
        self.head_canvas.pack(fill=tk.X)
        tk.Frame(detail_inner, bg=LINE_HEX, height=1).pack(fill=tk.X)

        list_wrap = tk.Frame(detail_inner, bg=BG_CARD)
        list_wrap.pack(fill=tk.BOTH, expand=True)

        self.list_canvas = tk.Canvas(list_wrap, bg=BG_CARD, bd=0, highlightthickness=0)
        self.list_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.sb_canvas = tk.Canvas(list_wrap, width=self.px(6), bg=BG_CARD, bd=0, highlightthickness=0)
        self.sb_canvas.pack(side=tk.RIGHT, fill=tk.Y, padx=(self.px(4), 0))

        self.list_canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.list_canvas.bind("<Motion>", self._on_list_motion)
        self.list_canvas.bind("<Leave>", self._on_list_leave)
        self.list_canvas.bind("<Button-1>", self._on_list_click)

        self._loading_overlay = tk.Frame(self.page, bg=BG_PAGE)
        loader_center = tk.Frame(self._loading_overlay, bg=BG_PAGE)
        loader_center.place(relx=0.5, rely=0.46, anchor="center")

        sp_size = self.px(44)
        self._spinner_size = sp_size
        self._spinner_canvas = tk.Canvas(
            loader_center, width=sp_size, height=sp_size, bg=BG_PAGE, bd=0, highlightthickness=0,
        )
        self._spinner_canvas.pack()
        self._loading_lbl = tk.Label(
            loader_center, text="正在同步工作时间与上下文状态…", bg=BG_PAGE, fg=INK_2, font=(FONT_UI, 9),
        )
        self._loading_lbl.pack(pady=(self.px(12), 0))

    def _draw_mode_switch(self) -> None:
        self.mode_canvas.delete("all")
        mw, mh = self.px(88), self.px(34)
        cw, ch = mw + self.px(6), mh + self.px(6)
        self.mode_canvas.create_image(cw // 2, ch // 2, image=self._imgs["mode_outer"])
        if self.view_mode == "day":
            self.mode_canvas.create_image(self.px(26), ch // 2, image=self._imgs["mode_active"])
            self.mode_canvas.create_text(self.px(26), ch // 2, text="日", fill="#ffffff", font=(FONT_UI, 9, "bold"))
            self.mode_canvas.create_text(cw - self.px(26), ch // 2, text="周", fill=INK_2, font=(FONT_UI, 9))
        else:
            self.mode_canvas.create_image(cw - self.px(26), ch // 2, image=self._imgs["mode_active"])
            self.mode_canvas.create_text(self.px(26), ch // 2, text="日", fill=INK_2, font=(FONT_UI, 9))
            self.mode_canvas.create_text(cw - self.px(26), ch // 2, text="周", fill="#ffffff", font=(FONT_UI, 9, "bold"))

    def _draw_spinner_frame(self) -> None:
        self._spinner_canvas.delete("all")
        s = self._spinner_size
        cx = cy = s / 2.0
        r_in, r_out = s * 0.25, s * 0.44
        w_line = max(2, self.px(2.4))
        shades = (
            "#1d1d1f", "#343438", "#4b4b50", "#636369", "#7a7a82", "#919199",
            "#a6a6ae", "#b8b8c0", "#c7c7cf", "#d3d3db", "#dcdce3", "#e3e3e9",
        )
        head = self._spinner_step % 12
        for i in range(12):
            age = (head - i) % 12
            ang = math.radians(i * 30.0 - 90.0)
            self._spinner_canvas.create_line(
                cx + r_in * math.cos(ang), cy + r_in * math.sin(ang),
                cx + r_out * math.cos(ang), cy + r_out * math.sin(ang),
                fill=shades[age], width=w_line, capstyle=tk.ROUND,
            )

    def _tick_spinner(self) -> None:
        self._spinner_after_id = None
        if not self._loading_active:
            return
        self._spinner_step = (self._spinner_step + 1) % 12
        self._draw_spinner_frame()
        self._spinner_after_id = self.root.after(48, self._tick_spinner)

    def _present_window_with_loading(self, min_ms: int = 280) -> None:
        """Show window with crisp spinning loader and smooth alpha fade-in (no blank flash)."""
        self._cancel_after("_fade_after_id")
        self._cancel_after("_reveal_after_id")
        self._cancel_after("_topmost_after_id")

        self._loading_active = True
        self._loading_min_until = time.monotonic() + (min_ms / 1000.0)
        self._spinner_step = 0
        self._draw_spinner_frame()
        self._loading_overlay.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        self._loading_overlay.lift()
        if self._spinner_after_id is None:
            self._spinner_after_id = self.root.after(48, self._tick_spinner)

        self._window_visible = True
        try:
            self.root.attributes("-alpha", 0.04)
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.attributes("-topmost", True)
            self._topmost_after_id = self.root.after(160, self._clear_topmost_safe)
            self.root.focus_force()
            self._step_fade_in(0.04)
        except Exception:
            try:
                self.root.attributes("-alpha", 1.0)
                self.root.deiconify()
            except Exception:
                pass

    def _step_fade_in(self, alpha: float) -> None:
        self._fade_after_id = None
        if not self._window_visible:
            return
        nxt = min(1.0, alpha + 0.16)
        try:
            self.root.attributes("-alpha", nxt)
        except Exception:
            return
        if nxt < 0.999:
            self._fade_after_id = self.root.after(16, lambda a=nxt: self._step_fade_in(a))

    def _clear_topmost_safe(self) -> None:
        self._topmost_after_id = None
        if self._window_visible:
            try:
                self.root.attributes("-topmost", False)
            except Exception:
                pass

    def _finish_loading_transition(self) -> None:
        if not self._loading_active:
            return
        rem_sec = self._loading_min_until - time.monotonic()
        if rem_sec > 0.005 and self._window_visible:
            self._cancel_after("_reveal_after_id")
            self._reveal_after_id = self.root.after(max(10, int(rem_sec * 1000)), self._hide_loading_overlay_now)
        else:
            self._hide_loading_overlay_now()

    def _hide_loading_overlay_now(self) -> None:
        self._reveal_after_id = None
        self._loading_active = False
        self._cancel_after("_spinner_after_id")
        try:
            self._loading_overlay.place_forget()
        except Exception:
            pass

    def _cancel_after(self, attr_name: str) -> None:
        aid = getattr(self, attr_name, None)
        if aid is not None:
            setattr(self, attr_name, None)
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
