"""Hero card rendering, incremental signature diffing, tab coordinator, and list scrolling."""
from __future__ import annotations

from datetime import date, datetime
import tkinter as tk

from config.settings import (
    BAR_TRACK,
    LOCAL_TZ,
    BG_CARD,
    BG_HOVER,
    FONT_UI,
    GREEN_DOT,
    GREEN_TEXT,
    INK,
    INK_2,
    INK_3,
    IS_MACOS,
    MIN_VISIBLE_DATE,
)
from src.ui.i18n import localize_duration, localize_weekday, tr
from src.ui.primitives import make_timeline_image
from src.ui.table_views import format_ranked_flat_model_parts


def compute_mousewheel_units(delta: int, *, is_macos: bool = IS_MACOS) -> int:
    """Convert platform `<MouseWheel>` event delta into Tk yview scroll units."""
    if delta == 0:
        return 0
    if is_macos:
        return -int(delta)
    if abs(delta) >= 120:
        return int(-1 * (delta / 120))
    return -1 if delta > 0 else 1


class HeroViewMixin:
    """Renders the Day/Week Hero card, incremental row diffing, hover hitboxes, and scrollbar."""

    def _set_status_pill(self, is_active_period: bool, running_count: int, idle_label: str) -> None:
        if is_active_period and running_count > 0:
            self._itemcfg(self.status_canvas, self.status_bg_item, image=self._imgs["pill_run"])
            self._itemcfg(self.status_canvas, self.status_dot_item, fill=GREEN_DOT)
            status_key = "status_running_one" if running_count == 1 else "status_running"
            self._itemcfg(
                self.status_canvas,
                self.status_text_item,
                text=tr(status_key, self._lang, n=running_count),
                fill=GREEN_TEXT,
            )
        elif is_active_period:
            self._itemcfg(self.status_canvas, self.status_bg_item, image=self._imgs["pill_idle"])
            self._itemcfg(self.status_canvas, self.status_dot_item, fill=INK_3)
            self._itemcfg(self.status_canvas, self.status_text_item, text=idle_label, fill=INK_2)
        else:
            self._itemcfg(self.status_canvas, self.status_bg_item, image=self._imgs["pill_idle"])
            self._itemcfg(self.status_canvas, self.status_dot_item, fill=INK_3)
            self._itemcfg(self.status_canvas, self.status_text_item, text=tr("status_archived", self._lang), fill=INK_2)

    def _update_hero_quota(self, quota: dict, is_week: bool, is_active: bool) -> None:
        if not quota.get("calibrated"):
            self._cfg(self.hero_quota_main, text=tr("quota_uncalibrated", self._lang))
            self._cfg(self.hero_quota_sub, text="")
            return
        if is_active:
            key = "quota_week_active" if is_week else "quota_day_active"
            main_txt = tr(key, self._lang, pct=quota["weekly_pct"], left=quota.get("cycle_left_pct", 100.0))
        else:
            key = "quota_week_hist" if is_week else "quota_day_hist"
            main_txt = tr(key, self._lang, pct=quota["weekly_pct"])
        self._cfg(self.hero_quota_main, text=main_txt)

        parts = format_ranked_flat_model_parts(quota, lang=self._lang)
        if is_active:
            parts.append(tr("quota_5h_left", self._lang, left=quota.get("five_hour_left_pct", 100.0)))
        self._cfg(self.hero_quota_sub, text="  ·  ".join(parts))

    def _render_day_hero(self, data: dict) -> None:
        day_tag = tr("today_tag", self._lang) if data["is_today"] else ""
        wd = localize_weekday(data["weekday_cn"], self._lang)
        self._itemcfg(self.date_canvas, self.date_text_item, text=f"{data['target_date']} {wd}{day_tag}")
        self._itemcfg(
            self.date_canvas,
            self.date_prev_icon,
            image=self._imgs["chev_l"] if data.get("can_prev", True) else self._imgs["chev_l_dis"],
        )
        self._itemcfg(
            self.date_canvas,
            self.date_next_icon,
            image=self._imgs["chev_r"] if data["can_next"] else self._imgs["chev_r_dis"],
        )

        self._itemcfg(self.today_btn_canvas, self.today_btn_text, text=tr("back_today", self._lang))
        self._set_today_btn_visible(not bool(data["is_today"]))

        self._cfg(
            self.hero_eyebrow,
            text=tr("eyebrow_today", self._lang) if data["is_today"] else tr("eyebrow_day", self._lang, date=data["target_date"]),
        )
        self._set_status_pill(data["is_today"], data["running_count"], tr("status_idle_day", self._lang))

        self._cfg(self.total_human_lbl, text=localize_duration(data["total_human"], self._lang))
        self._cfg(self.total_hms_lbl, text=data["total_hms"])
        self._update_hero_quota(data.get("quota", {}), is_week=False, is_active=bool(data["is_today"]))
        meta_text = (
            tr("meta_day", self._lang, iv=data["interval_count"], proj=data["project_count"])
            if data["interval_count"] > 0 or data["project_count"] > 0
            else tr("meta_empty", self._lang)
        )
        self._cfg(self.hero_meta_lbl, text=meta_text)

        segs = [(s["start_pct"], s["width_pct"]) for s in data["timeline_segments"]]
        viz_sig = ("day", self._lang, tuple(segs))
        if viz_sig == self._last_viz_sig:
            return
        self._last_viz_sig = viz_sig

        self.hero_viz.addtag_all("_stale_viz")
        self._week_bar_hitboxes.clear()
        self._cfg(self.hero_viz, cursor="arrow")
        self._imgs["timeline_next"] = make_timeline_image(self.tl_w, self.px(8), segs)
        self.hero_viz.create_image(0, self.px(12), anchor="nw", image=self._imgs["timeline_next"])
        self._imgs["timeline"] = self._imgs["timeline_next"]

        y_axis = self.px(30)
        for idx, mark in enumerate(("00:00", "06:00", "12:00", "18:00", "24:00")):
            if idx == 0:
                self.hero_viz.create_text(0, y_axis, anchor="nw", text=mark, fill=INK_3, font=(FONT_UI, 8))
            elif idx == 4:
                self.hero_viz.create_text(self.tl_w, y_axis, anchor="ne", text=mark, fill=INK_3, font=(FONT_UI, 8))
            else:
                self.hero_viz.create_text(int(round(self.tl_w * idx * 0.25)), y_axis, anchor="n", text=mark, fill=INK_3, font=(FONT_UI, 8))
        self.hero_viz.delete("_stale_viz")

    def _render_week_hero(self, data: dict) -> None:
        wk_tag = tr("week_tag", self._lang) if data["is_current_week"] else ""
        s_short = data["week_start"][5:]
        e_short = data["week_end"][5:]
        self._itemcfg(self.date_canvas, self.date_text_item, text=f"{s_short} ~ {e_short}{wk_tag}")
        self._itemcfg(
            self.date_canvas,
            self.date_prev_icon,
            image=self._imgs["chev_l"] if data.get("can_prev", True) else self._imgs["chev_l_dis"],
        )
        self._itemcfg(
            self.date_canvas,
            self.date_next_icon,
            image=self._imgs["chev_r"] if data["can_next"] else self._imgs["chev_r_dis"],
        )

        self._itemcfg(self.today_btn_canvas, self.today_btn_text, text=tr("back_week", self._lang))
        self._set_today_btn_visible(not bool(data["is_current_week"]))

        self._cfg(self.hero_eyebrow, text=tr("eyebrow_week", self._lang))
        self._set_status_pill(data["is_current_week"], data["running_count"], tr("status_idle_week", self._lang))

        self._cfg(self.total_human_lbl, text=localize_duration(data["total_human"], self._lang))
        self._cfg(self.total_hms_lbl, text=data["total_hms"])
        self._update_hero_quota(data.get("quota", {}), is_week=True, is_active=bool(data["is_current_week"]))
        self._cfg(
            self.hero_meta_lbl,
            text=tr(
                "meta_week",
                self._lang,
                days=data["active_days"],
                avg=localize_duration(data["avg_human"], self._lang),
                proj=data["project_count"],
            ),
        )

        viz_sig = (
            "week",
            self._lang,
            bool(data["is_current_week"]),
            tuple(
                (d["date"], d["is_today"], d["is_future"], d["compact_hm"], d["total_seconds"], d["bar_pct"])
                for d in data["days"]
            ),
        )
        if viz_sig == self._last_viz_sig:
            return
        self._last_viz_sig = viz_sig

        self.hero_viz.addtag_all("_stale_viz")
        self._week_bar_hitboxes.clear()
        self._cfg(self.hero_viz, cursor="hand2")
        col_w = self.tl_w / 7.0
        track_top = self.px(16)
        track_h = self.px(22)
        bar_w = self.px(28)

        for idx, d in enumerate(data["days"]):
            cx = int(round((idx + 0.5) * col_w))
            x0 = int(round(idx * col_w))
            x1 = int(round((idx + 1) * col_w))
            self._week_bar_hitboxes.append((x0, x1, d["date"]))

            top_lbl = "-" if d["is_future"] else d["compact_hm"]
            top_col = INK if d["total_seconds"] > 0 else INK_3
            self.hero_viz.create_text(cx, self.px(7), text=top_lbl, fill=top_col, font=(FONT_UI, 8, "bold" if d["is_today"] else "normal"))

            bx0 = cx - bar_w // 2
            bx1 = bx0 + bar_w
            self._draw_rounded_rect_on_canvas(self.hero_viz, bx0, track_top, bx1, track_top + track_h, self.px(5), BAR_TRACK)
            if d["total_seconds"] > 0:
                fill_h = max(self.px(6), int(round((d["bar_pct"] / 100.0) * track_h)))
                fy0 = track_top + track_h - fill_h
                bar_col = INK if (d["is_today"] or not data["is_current_week"]) else "#3a3a3c"
                self._draw_rounded_rect_on_canvas(self.hero_viz, bx0, fy0, bx1, track_top + track_h, self.px(5), bar_col)

            day_text = f"{localize_weekday(d['weekday_cn'], self._lang)} {d['short_date']}"
            day_col = INK if d["is_today"] else INK_3
            self.hero_viz.create_text(
                cx, track_top + track_h + self.px(10),
                text=day_text, fill=day_col, font=(FONT_UI, 8, "bold" if d["is_today"] else "normal"),
            )
        self.hero_viz.delete("_stale_viz")

    def _draw_rounded_rect_on_canvas(
        self, canvas: tk.Canvas, x0: int, y0: int, x1: int, y1: int, r: int, fill: str, tags: str = "",
    ) -> int:
        r = min(r, (x1 - x0) // 2, (y1 - y0) // 2)
        if r <= 0:
            return canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="", tags=tags)
        pts = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return canvas.create_polygon(pts, smooth=True, splinesteps=12, fill=fill, outline="", tags=tags)

    def _on_week_bar_click(self, event: tk.Event) -> None:
        if self.view_mode != "week":
            return
        for x0, x1, iso_date in self._week_bar_hitboxes:
            if x0 <= event.x <= x1:
                self._jump_to_day(iso_date)
                break

    def _jump_to_day(self, iso_date: str) -> None:
        target = date.fromisoformat(iso_date)
        today_bj = datetime.now(LOCAL_TZ).date()
        if (MIN_VISIBLE_DATE is not None and target < MIN_VISIBLE_DATE) or target > today_bj:
            return
        self.view_mode = "day"
        self.selected_date = target
        self._follow_today = target == today_bj
        self._draw_mode_switch()
        self.refresh(force_recompute=False, schedule_next=False)

    def _compute_rows_signature(self, data: dict, mode: str, tab_key: str, calibrated: bool) -> object:
        """Build a compact tuple of visible table values so unchanged auto-refreshes touch zero canvas items."""
        if tab_key == "ctx":
            return (
                mode, "ctx",
                tuple(
                    (
                        d.get("conversation_id"), d.get("workspace"), d.get("title"), d.get("model_label"),
                        d.get("input_fmt"), d.get("cache_fmt"), d.get("output_fmt"), d.get("context_tokens"),
                        d.get("cache_hit_pct"), d.get("cum_cache_hit_pct"), d.get("cache_state"),
                        d.get("cache_status_text"), d.get("cache_idle_text"), d.get("context_fmt"),
                        d.get("safe_limit_fmt"), d.get("safe_pct"), d.get("advice_key"), d.get("advice_text"),
                        d.get("advice_reason"), d.get("compaction_text"),
                    )
                    for d in data.get("dialogs", [])
                ),
            )
        if (mode == "day" and tab_key == "conv") or (mode == "week" and tab_key == "conv"):
            return (
                mode, "conv", calibrated,
                tuple(
                    (
                        p.get("workspace"), p.get("conversation_count"), p.get("bar_pct"), p.get("is_running"),
                        p.get("weekly_quota_pct"), p.get("duration"),
                        p.get("carbon", {}).get("tokens"), p.get("carbon", {}).get("weekly_pct"), p.get("carbon", {}).get("weighted_usd"),
                        p.get("neon", {}).get("tokens"), p.get("neon", {}).get("weekly_pct"), p.get("neon", {}).get("weighted_usd"),
                        p.get("hydrogen", {}).get("tokens"), p.get("hydrogen", {}).get("weekly_pct"), p.get("hydrogen", {}).get("weighted_usd"),
                        p.get("other", {}).get("tokens"), p.get("other", {}).get("weekly_pct"), p.get("other", {}).get("weighted_usd"),
                    )
                    for p in data.get("projects", [])
                ),
            )
        if mode == "day" and tab_key == "iv":
            return (
                "day", "iv", calibrated,
                tuple(
                    (
                        iv.get("index"), iv.get("start"), iv.get("end"), iv.get("duration"),
                        iv.get("weekly_quota_pct"), iv.get("carbon_weekly_pct"), iv.get("neon_weekly_pct"),
                        iv.get("hydrogen_weekly_pct"), iv.get("other_weekly_pct"),
                    )
                    for iv in data.get("intervals", [])
                ),
            )
        return (
            "week", "days", calibrated,
            tuple(
                (
                    d.get("date"), d.get("weekday_cn"), d.get("is_today"), d.get("is_future"),
                    d.get("interval_count"), d.get("conversation_count"), d.get("total_detail"), d.get("total_seconds"),
                    d.get("weekly_quota_pct"), d.get("carbon_weekly_pct"), d.get("neon_weekly_pct"),
                    d.get("hydrogen_weekly_pct"), d.get("other_weekly_pct"),
                )
                for d in data.get("days", [])
            ),
        )

    def _render_tabs_and_rows(self, data: dict, preserve_scroll: bool) -> None:
        mode = data["mode"]
        period_key = data["target_date"] if mode == "day" else data["week_start"]
        tab_key = self.day_tab if mode == "day" else self.week_tab
        view_ctx = (mode, period_key, tab_key)
        same_view_ctx = view_ctx == self._last_view_ctx
        self._last_view_ctx = view_ctx

        keep_scroll = bool(preserve_scroll or same_view_ctx)
        prev_y = self.list_canvas.yview()[0] if keep_scroll else 0.0
        prev_hover = self._hover_row_idx if same_view_ctx else -1

        self._cfg(self.tab1_lbl, text=tr("tab_dialogs", self._lang))
        if mode == "day":
            self._cfg(self.tab2_lbl, text=tr("tab_projects_day", self._lang))
            self._cfg(self.tab3_lbl, text=tr("tab_intervals", self._lang))
            active_idx = {"ctx": 0, "conv": 1, "iv": 2}.get(self.day_tab, 0)
        else:
            self._cfg(self.tab2_lbl, text=tr("tab_days", self._lang))
            self._cfg(self.tab3_lbl, text=tr("tab_projects_week", self._lang))
            active_idx = {"ctx": 0, "days": 1, "conv": 2}.get(self.week_tab, 0)

        for idx, (lbl, line) in enumerate((
            (self.tab1_lbl, self.tab1_line),
            (self.tab2_lbl, self.tab2_line),
            (self.tab3_lbl, self.tab3_line),
        )):
            if idx == active_idx:
                self._cfg(lbl, fg=INK, font=(FONT_UI, 10, "bold"))
                self._cfg(line, bg=INK)
            else:
                self._cfg(lbl, fg=INK_3, font=(FONT_UI, 10))
                self._cfg(line, bg=BG_CARD)

        self._cfg(self.updated_lbl, text=tr("updated_live", self._lang, hms=data["updated_at"]))

        calibrated = bool(data.get("quota", {}).get("calibrated", False))
        head_sig = (mode, tab_key, self._lang)
        rows_sig = (self._lang, self._compute_rows_signature(data, mode, tab_key, calibrated))
        if head_sig == self._last_head_sig and rows_sig == self._last_rows_sig:
            if not keep_scroll and prev_y == 0.0 and self.list_canvas.yview()[0] > 0.0:
                self.list_canvas.yview_moveto(0.0)
                self._draw_thin_scrollbar()
            return

        redraw_head = head_sig != self._last_head_sig
        self._last_head_sig = head_sig
        self._last_rows_sig = rows_sig

        if redraw_head:
            self.head_canvas.addtag_all("_stale_head")
        self.list_canvas.addtag_all("_stale_rows")
        self._row_hitboxes.clear()
        self._hover_row_idx = -1

        if mode == "day":
            if self.day_tab == "conv":
                content_h = self._draw_projects_canvas(
                    data["projects"], quota_label=tr("quota_col_day", self._lang), calibrated=calibrated, draw_head=redraw_head
                )
            elif self.day_tab == "iv":
                content_h = self._draw_intervals_canvas(data["intervals"], calibrated=calibrated, draw_head=redraw_head)
            else:
                content_h = self._draw_dialogs_canvas(data.get("dialogs", []), draw_head=redraw_head)
        else:
            if self.week_tab == "days":
                content_h = self._draw_week_days_canvas(data["days"], calibrated=calibrated, draw_head=redraw_head)
            elif self.week_tab == "conv":
                content_h = self._draw_projects_canvas(
                    data["projects"], quota_label=tr("quota_col_week", self._lang), calibrated=calibrated, draw_head=redraw_head
                )
            else:
                content_h = self._draw_dialogs_canvas(data.get("dialogs", []), draw_head=redraw_head)

        if redraw_head:
            self.head_canvas.delete("_stale_head")
        self.list_canvas.delete("_stale_rows")

        if 0 <= prev_hover < len(self._row_hitboxes):
            self._hover_row_idx = prev_hover
            self.list_canvas.itemconfigure(f"rowbg_{prev_hover}", fill=BG_HOVER)

        if content_h != self._last_scroll_h:
            self._last_scroll_h = content_h
            self.list_canvas.configure(scrollregion=(0, 0, self.list_w, content_h))

        self.list_canvas.yview_moveto(prev_y if keep_scroll else 0.0)
        self._draw_thin_scrollbar()

    def _on_list_motion(self, event: tk.Event) -> None:
        if not self._row_hitboxes:
            return
        cy = self.list_canvas.canvasy(event.y)
        hit = -1
        for idx, (y0, y1, _val) in enumerate(self._row_hitboxes):
            if y0 <= cy < y1:
                hit = idx
                break
        if hit != self._hover_row_idx:
            if self._hover_row_idx >= 0:
                self.list_canvas.itemconfigure(f"rowbg_{self._hover_row_idx}", fill=BG_CARD)
            if hit >= 0:
                self.list_canvas.itemconfigure(f"rowbg_{hit}", fill=BG_HOVER)
            self._hover_row_idx = hit

    def _on_list_leave(self, _event: tk.Event) -> None:
        if self._hover_row_idx >= 0:
            self.list_canvas.itemconfigure(f"rowbg_{self._hover_row_idx}", fill=BG_CARD)
            self._hover_row_idx = -1

    def _on_list_click(self, event: tk.Event) -> None:
        if not self._row_hitboxes:
            return
        cy = self.list_canvas.canvasy(event.y)
        for y0, y1, iso_date in self._row_hitboxes:
            if y0 <= cy < y1:
                self._jump_to_day(iso_date)
                break

    def _on_mousewheel(self, event: tk.Event) -> None:
        content_h = max(0, self._last_scroll_h)
        view_h = self.list_canvas.winfo_height()
        if content_h <= view_h:
            return
        units = compute_mousewheel_units(int(getattr(event, "delta", 0)))
        if units:
            self.list_canvas.yview_scroll(units, "units")
            self._draw_thin_scrollbar()

    def _draw_thin_scrollbar(self) -> None:
        first, last = self.list_canvas.yview()
        h = max(1, self.sb_canvas.winfo_height())
        sb_state = (round(first, 4), round(last, 4), h)
        if sb_state == self._last_sb_state:
            return
        self._last_sb_state = sb_state
        self.sb_canvas.delete("all")
        if first <= 0.0 and last >= 1.0:
            return
        w = max(2, self.sb_canvas.winfo_width())
        y0 = int(round(first * h))
        y1 = max(y0 + self.px(24), int(round(last * h)))
        if y1 > h:
            y0 = max(0, h - (y1 - y0))
            y1 = h
        self.sb_canvas.create_rectangle(0, y0, w, y1, fill="#d4d4d8", outline="")
