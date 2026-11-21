"""Canvas table renderers for Context Dialogs, Projects, Work Intervals, and Weekly Days."""
from __future__ import annotations

from config.settings import (
    BAR_FILL,
    BAR_TRACK,
    BG_CARD,
    BG_PAGE,
    FONT_UI,
    GREEN_BG,
    GREEN_DOT,
    GREEN_TEXT,
    INK,
    INK_2,
    INK_3,
    LINE_HEX,
)


_MODEL_QUOTA_LABELS: tuple[tuple[str, str], ...] = (
    ("carbon", "Carbon"),
    ("neon", "Neon"),
    ("hydrogen", "Hydrogen"),
    ("other", "其他"),
)


def format_ranked_flat_model_parts(item: dict) -> list[str]:
    """Return per-model weekly-quota labels ordered from highest to lowest consumption."""
    entries: list[tuple[float, int, str]] = []
    for idx, (m_key, m_label) in enumerate(_MODEL_QUOTA_LABELS):
        m_pct = float(item.get(f"{m_key}_weekly_pct", 0.0))
        if m_pct > 0.0:
            entries.append((m_pct, idx, m_label))
    entries.sort(key=lambda e: (-e[0], e[1]))
    return [f"{m_label} {m_pct:.1f}%" for m_pct, _idx, m_label in entries]


def format_ranked_project_model_parts(project: dict) -> list[str]:
    """Return project per-model weekly-quota labels ordered from highest to lowest consumption."""
    entries: list[tuple[float, float, int, int, str]] = []
    for idx, (m_key, m_label) in enumerate(_MODEL_QUOTA_LABELS):
        md = project.get(m_key, {})
        tokens = int(md.get("tokens", 0))
        pct = float(md.get("weekly_pct", 0.0))
        if tokens > 0 and (m_key != "other" or pct >= 0.1 or not entries):
            usd = float(md.get("weighted_usd", 0.0))
            entries.append((pct, usd, tokens, idx, m_label))
    entries.sort(key=lambda e: (-e[0], -e[1], -e[2], e[3]))
    return [f"{m_label} {pct:.1f}%" for pct, _usd, _tokens, _idx, m_label in entries]


class TableViewsMixin:
    """Draws zero-widget-churn Canvas rows for all four detail tabs."""

    def _draw_projects_canvas(
        self,
        projects: list[dict],
        quota_label: str = "今日占周额度",
        calibrated: bool = True,
        draw_head: bool = True,
    ) -> int:
        w = self.list_w
        hy = self.px(16)
        x_st = self.px(272)
        x_quota = self.px(400)
        x_dur = w - self.px(6)

        if draw_head:
            self.head_canvas.create_text(self.px(6), hy, anchor="w", text="项目", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_st, hy, anchor="w", text="状态", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_quota, hy, anchor="w", text=quota_label, fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_dur, hy, anchor="e", text="累计时长", fill=INK_3, font=(FONT_UI, 9))

        self._cfg(self.list_canvas, cursor="arrow")
        if not projects:
            self.list_canvas.create_text(w // 2, self.px(90), text="暂无有效项目工作记录", fill=INK_3, font=(FONT_UI, 10))
            return 0

        row_h = self.px(62)
        bar_w = self.px(190)
        bar_h = self.px(3)
        dr = self.px(3)

        for idx, p in enumerate(projects):
            y0 = idx * row_h
            y1 = y0 + row_h
            self.list_canvas.create_rectangle(0, y0, w, y1, fill=BG_CARD, outline="", tags=(f"rowbg_{idx}",))

            top_y = y0 + self.px(21)
            sub_y = y0 + self.px(43)
            name_id = self.list_canvas.create_text(
                self.px(6), top_y, anchor="w", text=p["workspace"], fill=INK, font=(FONT_UI, 10, "bold"),
            )
            name_bbox = self.list_canvas.bbox(name_id)
            sub_x = (name_bbox[2] if name_bbox else self.px(80)) + self.px(8)
            conv_lbl = f"·  {p['conversation_count']} 对话" if p["conversation_count"] > 0 else "·  模型调用"
            self.list_canvas.create_text(sub_x, top_y, anchor="w", text=conv_lbl, fill=INK_3, font=(FONT_UI, 9))

            by0 = y0 + self.px(41)
            self._draw_rounded_rect_on_canvas(self.list_canvas, self.px(6), by0, self.px(6) + bar_w, by0 + bar_h, bar_h // 2, BAR_TRACK)
            if p["bar_pct"] > 0:
                fw = max(bar_h, int(round((min(100.0, p["bar_pct"]) / 100.0) * bar_w)))
                self._draw_rounded_rect_on_canvas(self.list_canvas, self.px(6), by0, self.px(6) + fw, by0 + bar_h, bar_h // 2, BAR_FILL)

            dot_col = GREEN_DOT if p["is_running"] else INK_3
            txt_col = GREEN_TEXT if p["is_running"] else INK_3
            txt_str = "运行中" if p["is_running"] else "已停下"
            dot_cx = x_st + dr
            self.list_canvas.create_oval(dot_cx - dr, top_y - dr, dot_cx + dr, top_y + dr, fill=dot_col, outline="")
            self.list_canvas.create_text(dot_cx + dr + self.px(6), top_y, anchor="w", text=txt_str, fill=txt_col, font=(FONT_UI, 9))

            q_txt = f"{p.get('weekly_quota_pct', 0.0):.1f}%" if calibrated else "-"
            self.list_canvas.create_text(x_quota, top_y, anchor="w", text=q_txt, fill=INK, font=(FONT_UI, 10, "bold"))

            if calibrated:
                model_parts = format_ranked_project_model_parts(p)
                if model_parts:
                    self.list_canvas.create_text(
                        x_quota, sub_y, anchor="w", text="  ·  ".join(model_parts), fill=INK_3, font=(FONT_UI, 8),
                    )

            self.list_canvas.create_text(x_dur, top_y, anchor="e", text=p["duration"], fill=INK, font=(FONT_UI, 10, "bold"))

            if idx < len(projects) - 1:
                self.list_canvas.create_line(self.px(4), y1 - 1, w - self.px(4), y1 - 1, fill=LINE_HEX)
        return len(projects) * row_h

    def _draw_dialogs_canvas(self, dialogs: list[dict], draw_head: bool = True) -> int:
        w = self.list_w
        hy = self.px(16)
        x_hit = self.px(272)
        x_cache = self.px(398)
        x_ctx = self.px(538)
        pill_w, pill_h = self.px(78), self.px(22)
        px1 = w - self.px(6)
        px0 = px1 - pill_w
        x_adv_center = (px0 + px1) // 2

        if draw_head:
            self.head_canvas.create_text(self.px(6), hy, anchor="w", text="项目 · 对话", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_hit, hy, anchor="w", text="缓存命中率", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_cache, hy, anchor="w", text="动态缓存", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_ctx, hy, anchor="w", text="上下文 / 安全线", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_adv_center, hy, anchor="center", text="换窗建议", fill=INK_3, font=(FONT_UI, 9))

        self._cfg(self.list_canvas, cursor="arrow")
        if not dialogs:
            self.list_canvas.create_text(w // 2, self.px(90), text="当前暂无未归档的活跃对话", fill=INK_3, font=(FONT_UI, 10))
            return 0

        row_h = self.px(56)
        advice_styles = {
            "switch": ("#c9342b", "#fdecea"),
            "watch": ("#b25000", "#fff4e5"),
            "ok": (GREEN_TEXT, GREEN_BG),
        }
        cache_styles = {
            "active": (GREEN_DOT, GREEN_TEXT),
            "fresh": (GREEN_DOT, GREEN_TEXT),
            "cooling": ("#e59400", "#b25000"),
            "decay": ("#e06b22", "#b25000"),
            "none": (INK_3, INK_3),
        }
        dr = self.px(3)

        for idx, d in enumerate(dialogs):
            y0 = idx * row_h
            y1 = y0 + row_h
            top_y = y0 + self.px(19)
            sub_y = y0 + self.px(39)
            self.list_canvas.create_rectangle(0, y0, w, y1, fill=BG_CARD, outline="", tags=(f"rowbg_{idx}",))

            ws_txt = f"{d['workspace']} · "
            ws_id = self.list_canvas.create_text(self.px(6), top_y, anchor="w", text=ws_txt, fill=INK_3, font=(FONT_UI, 9))
            ws_bb = self.list_canvas.bbox(ws_id)
            title_x = (ws_bb[2] if ws_bb else self.px(50)) + self.px(2)
            max_title_x = x_hit - self.px(28)
            raw_title = d["title"]
            title_id = self.list_canvas.create_text(title_x, top_y, anchor="w", text=raw_title, fill=INK, font=(FONT_UI, 10, "bold"))
            t_bb = self.list_canvas.bbox(title_id)
            if t_bb and t_bb[2] > max_title_x and len(raw_title) > 1:
                lo, hi = 1, len(raw_title) - 1
                fit = raw_title[0] + "…"
                while lo <= hi:
                    mid = (lo + hi) // 2
                    cand = raw_title[:mid] + "…"
                    self.list_canvas.itemconfigure(title_id, text=cand)
                    cbb = self.list_canvas.bbox(title_id)
                    if cbb and cbb[2] <= max_title_x:
                        fit = cand
                        lo = mid + 1
                    else:
                        hi = mid - 1
                self.list_canvas.itemconfigure(title_id, text=fit)

            m_lbl = d.get("model_label", "Carbon")
            sub_line = f"{m_lbl}  ·  入 {d['input_fmt']}  ·  缓 {d['cache_fmt']}  ·  出 {d['output_fmt']}"
            self.list_canvas.create_text(self.px(6), sub_y, anchor="w", text=sub_line, fill=INK_3, font=(FONT_UI, 8))

            ctx_tok = int(d.get("context_tokens", 0))
            hit_pct = float(d.get("cache_hit_pct", 0.0))
            cum_pct = float(d.get("cum_cache_hit_pct", 0.0))
            if ctx_tok == 0:
                self.list_canvas.create_text(x_hit, top_y, anchor="w", text="-", fill=INK_3, font=(FONT_UI, 10))
                self.list_canvas.create_text(x_hit, sub_y, anchor="w", text="等待首步", fill=INK_3, font=(FONT_UI, 8))
            else:
                if cum_pct >= 80.0:
                    cum_fg = GREEN_TEXT
                elif cum_pct >= 50.0:
                    cum_fg = "#b25000"
                else:
                    cum_fg = "#c9342b"
                step_fg = "#c9342b" if (ctx_tok >= 4096 and hit_pct < 20.0) else INK_3
                self.list_canvas.create_text(x_hit, top_y, anchor="w", text=f"{cum_pct:.1f}%", fill=cum_fg, font=(FONT_UI, 10))
                self.list_canvas.create_text(x_hit, sub_y, anchor="w", text=f"最新步 {hit_pct:.1f}%", fill=step_fg, font=(FONT_UI, 8))

            c_state = d.get("cache_state", "expired")
            if c_state == "expired":
                c_dot, c_fg = ("#e04f44", "#c9342b") if ctx_tok >= 100_000 else (INK_3, INK_2)
            else:
                c_dot, c_fg = cache_styles.get(c_state, (INK_3, INK_2))
            ca_dot_cx = x_cache + dr
            ca_txt_x = ca_dot_cx + dr + self.px(6)
            self.list_canvas.create_oval(ca_dot_cx - dr, top_y - dr, ca_dot_cx + dr, top_y + dr, fill=c_dot, outline="")
            self.list_canvas.create_text(ca_txt_x, top_y, anchor="w", text=d.get("cache_status_text", "-"), fill=c_fg, font=(FONT_UI, 9))
            self.list_canvas.create_text(ca_txt_x, sub_y, anchor="w", text=d.get("cache_idle_text", ""), fill=INK_3, font=(FONT_UI, 8))

            fg_adv, bg_adv = advice_styles.get(d["advice_key"], (INK_2, BG_PAGE))
            ctx_val_id = self.list_canvas.create_text(
                x_ctx, top_y, anchor="w", text=d["context_fmt"],
                fill=fg_adv if d["advice_key"] == "switch" else INK, font=(FONT_UI, 10, "bold"),
            )
            ctx_val_bb = self.list_canvas.bbox(ctx_val_id)
            ctx_sub_x = (ctx_val_bb[2] if ctx_val_bb else x_ctx + self.px(36)) + self.px(4)
            safe_fmt = d.get("safe_limit_fmt", "256K")
            safe_pct = d.get("safe_pct", d.get("compact_pct", 0))
            self.list_canvas.create_text(ctx_sub_x, top_y, anchor="w", text=f"/ {safe_fmt} ({safe_pct}%)", fill=INK_3, font=(FONT_UI, 9))

            reason_txt = d.get("advice_reason") or d.get("compaction_text", "未触发压缩")
            if d["advice_key"] == "switch":
                reason_fg = "#c9342b"
            elif d["advice_key"] == "watch":
                reason_fg = "#b25000"
            else:
                reason_fg = INK_3
            self.list_canvas.create_text(x_ctx, sub_y, anchor="w", text=reason_txt, fill=reason_fg, font=(FONT_UI, 8))

            py0 = top_y - pill_h // 2
            py1 = top_y + pill_h // 2
            self._draw_rounded_rect_on_canvas(self.list_canvas, px0, py0, px1, py1, self.px(11), bg_adv)
            self.list_canvas.create_text(x_adv_center, top_y, text=d["advice_text"], fill=fg_adv, font=(FONT_UI, 8))

            if idx < len(dialogs) - 1:
                self.list_canvas.create_line(self.px(4), y1 - 1, w - self.px(4), y1 - 1, fill=LINE_HEX)
        return len(dialogs) * row_h

    def _draw_quota_cell(
        self,
        x_quota: int,
        top_y: int,
        sub_y: int,
        item: dict,
        calibrated: bool,
        active: bool = True,
        dim_zero: bool = False,
    ) -> None:
        """Render a weekly-quota percentage cell with per-model sub-breakdown."""
        if not active or not calibrated:
            col = INK_3 if not active else INK
            font = (FONT_UI, 10) if not active else (FONT_UI, 10, "bold")
            self.list_canvas.create_text(x_quota, top_y, anchor="w", text="-", fill=col, font=font)
            return
        tot_pct = float(item.get("weekly_quota_pct", 0.0))
        is_emph = (tot_pct > 0.0) or (not dim_zero)
        self.list_canvas.create_text(
            x_quota, top_y, anchor="w", text=f"{tot_pct:.1f}%",
            fill=INK if is_emph else INK_3,
            font=(FONT_UI, 10, "bold" if is_emph else "normal"),
        )
        parts = format_ranked_flat_model_parts(item)
        if parts:
            self.list_canvas.create_text(
                x_quota, sub_y, anchor="w", text="  ·  ".join(parts), fill=INK_3, font=(FONT_UI, 8),
            )

    def _draw_intervals_canvas(self, intervals: list[dict], calibrated: bool = True, draw_head: bool = True) -> int:
        w = self.list_w
        hy = self.px(16)
        x_time = self.px(148)
        x_quota = self.px(380)
        x_dur = w - self.px(6)
        if draw_head:
            self.head_canvas.create_text(self.px(6), hy, anchor="w", text="序号", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_time, hy, anchor="w", text="本地时间区间", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_quota, hy, anchor="w", text="模型耗周额度", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_dur, hy, anchor="e", text="持续时长", fill=INK_3, font=(FONT_UI, 9))

        self._cfg(self.list_canvas, cursor="arrow")
        if not intervals:
            self.list_canvas.create_text(w // 2, self.px(90), text="当日暂无有效工作时段", fill=INK_3, font=(FONT_UI, 10))
            return 0

        row_h = self.px(56)
        for idx, iv in enumerate(intervals):
            y0 = idx * row_h
            y1 = y0 + row_h
            top_y = y0 + self.px(19)
            sub_y = y0 + self.px(39)
            self.list_canvas.create_rectangle(0, y0, w, y1, fill=BG_CARD, outline="", tags=(f"rowbg_{idx}",))
            self.list_canvas.create_text(self.px(6), top_y, anchor="w", text=f"时段 {iv['index']:02d}", fill=INK_3, font=(FONT_UI, 9))
            self.list_canvas.create_text(x_time, top_y, anchor="w", text=f"{iv['start']} – {iv['end']}", fill=INK, font=(FONT_UI, 10, "bold"))
            self._draw_quota_cell(x_quota, top_y, sub_y, iv, calibrated)
            self.list_canvas.create_text(x_dur, top_y, anchor="e", text=iv["duration"], fill=INK, font=(FONT_UI, 10, "bold"))
            if idx < len(intervals) - 1:
                self.list_canvas.create_line(self.px(4), y1 - 1, w - self.px(4), y1 - 1, fill=LINE_HEX)
        return len(intervals) * row_h

    def _draw_week_days_canvas(
        self,
        days: list[dict],
        calibrated: bool = True,
        draw_head: bool = True,
    ) -> int:
        w = self.list_w
        hy = self.px(16)
        x_cnt = self.px(244)
        x_quota = self.px(400)
        x_dur = w - self.px(6)

        if draw_head:
            self.head_canvas.create_text(self.px(6), hy, anchor="w", text="日期 · 星期", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_cnt, hy, anchor="w", text="工作段 / 对话", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_quota, hy, anchor="w", text="当日耗周额度", fill=INK_3, font=(FONT_UI, 9))
            self.head_canvas.create_text(x_dur, hy, anchor="e", text="当日工作时长", fill=INK_3, font=(FONT_UI, 9))

        self._cfg(self.list_canvas, cursor="hand2")
        row_h = self.px(48)
        for idx, d in enumerate(days):
            y0 = idx * row_h
            y1 = y0 + row_h
            top_y = y0 + self.px(15)
            sub_y = y0 + self.px(35)
            self.list_canvas.create_rectangle(0, y0, w, y1, fill=BG_CARD, outline="", tags=(f"rowbg_{idx}",))
            self._row_hitboxes.append((y0, y1, d["date"]))

            date_label = f"{d['date']}  {d['weekday_cn']}" + ("  ·  今天" if d["is_today"] else "")
            lbl_col = INK if not d["is_future"] else INK_3
            self.list_canvas.create_text(
                self.px(6), top_y, anchor="w", text=date_label, fill=lbl_col,
                font=(FONT_UI, 10, "bold" if d["is_today"] else "normal"),
            )

            meta_txt = "未开始" if d["is_future"] else f"{d['interval_count']} 段 · {d['conversation_count']} 对话"
            self.list_canvas.create_text(x_cnt, top_y, anchor="w", text=meta_txt, fill=INK_3, font=(FONT_UI, 9))

            self._draw_quota_cell(
                x_quota, top_y, sub_y, d, calibrated=calibrated, active=not d["is_future"], dim_zero=True,
            )

            dur_txt = "-" if d["is_future"] else d["total_detail"]
            dur_col = INK if d["total_seconds"] > 0 else INK_3
            self.list_canvas.create_text(
                x_dur, top_y, anchor="e", text=dur_txt, fill=dur_col,
                font=(FONT_UI, 10, "bold" if d["total_seconds"] > 0 else "normal"),
            )

            if idx < len(days) - 1:
                self.list_canvas.create_line(self.px(4), y1 - 1, w - self.px(4), y1 - 1, fill=LINE_HEX)
        return len(days) * row_h
