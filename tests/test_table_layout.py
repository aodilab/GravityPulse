"""Unit tests for vertical alignment and spacing consistency across all four Canvas tables."""
from __future__ import annotations

import tkinter as tk
import unittest
from unittest import mock

from src.ui.desktop_app import GravityPulseApp
from src.ui.system_tray import SystemTrayIcon


class TestTableLayout(unittest.TestCase):
    def _build_app(self, root: tk.Tk) -> GravityPulseApp:
        return GravityPulseApp(root, show_event_handle=0, start_hidden=True, language="zh")

    def _find_text_y(self, canvas: tk.Canvas, text: str) -> float:
        for item in canvas.find_all():
            if canvas.type(item) == "text" and canvas.itemcget(item, "text") == text:
                return float(canvas.coords(item)[1])
        raise AssertionError(f"Text item not found: {text!r}")

    def test_all_four_tables_center_single_lines_and_balance_two_line_cells(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh"),
            ):
                ui = self._build_app(root)
                root.update_idletasks()

                # 1) Week days ("每日汇总"): 7 rows fit cleanly inside list viewport, single-line cols centered.
                ui.view_mode = "week"
                ui.week_tab = "days"
                week_days_payload = {
                    "mode": "week",
                    "week_start": "2026-11-16",
                    "updated_at": "08:00:00",
                    "quota": {"calibrated": True},
                    "days": [
                        {
                            "date": f"2026-11-{16 + i}",
                            "weekday_cn": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][i],
                            "is_today": i == 0,
                            "is_future": i == 6,
                            "interval_count": 0 if i == 6 else 2,
                            "conversation_count": 0 if i == 6 else 20,
                            "total_detail": "0 分钟" if i == 6 else "12小时 45分 25秒",
                            "total_seconds": 0 if i == 6 else 45925,
                            "weekly_quota_pct": 0.0 if i == 6 else 14.9,
                            "carbon_weekly_pct": 0.0 if i == 6 else 13.0,
                            "neon_weekly_pct": 0.0,
                            "hydrogen_weekly_pct": 0.0 if i == 6 else 1.9,
                            "other_weekly_pct": 0.0,
                        }
                        for i in range(7)
                    ],
                }
                ui._render_tabs_and_rows(week_days_payload, preserve_scroll=False)
                row_h_days = ui.px(48)
                mid_days = (row_h_days - 1) // 2
                self.assertEqual(ui._last_scroll_h, 7 * row_h_days)
                self.assertEqual(self._find_text_y(ui.list_canvas, "2026-11-16  周一  ·  今天"), mid_days)
                self.assertEqual(self._find_text_y(ui.list_canvas, "2 段 · 20 对话"), mid_days)
                self.assertEqual(self._find_text_y(ui.list_canvas, "12小时 45分 25秒"), mid_days)

                q_top = self._find_text_y(ui.list_canvas, "14.9%")
                q_sub = self._find_text_y(ui.list_canvas, "Carbon 13.0%  ·  Hydrogen 1.9%")
                self.assertLess(q_top, mid_days)
                self.assertGreater(q_sub, mid_days)
                self.assertLess(q_top + q_sub, row_h_days)

                row6_mid = 6 * row_h_days + mid_days
                self.assertEqual(self._find_text_y(ui.list_canvas, "未开始"), row6_mid)
                self.assertEqual(self._find_text_y(ui.list_canvas, "-"), row6_mid)

                # 2) Projects ("项目明细") uses unified 56px row_h and centers Status + Duration
                ui.view_mode = "day"
                ui.day_tab = "conv"
                proj_payload = {
                    "mode": "day",
                    "target_date": "2026-11-24",
                    "updated_at": "08:00:00",
                    "quota": {"calibrated": True},
                    "projects": [
                        {
                            "workspace": "GravityPulse",
                            "is_running": True,
                            "conversation_count": 3,
                            "duration": "2小时 15分 00秒",
                            "bar_pct": 100.0,
                            "weekly_quota_pct": 5.2,
                            "carbon": {"tokens": 1000, "weekly_pct": 3.2, "weighted_usd": 3.2},
                            "neon": {"tokens": 1000, "weekly_pct": 2.0, "weighted_usd": 2.0},
                            "hydrogen": {"tokens": 0, "weekly_pct": 0.0, "weighted_usd": 0.0},
                            "other": {"tokens": 0, "weekly_pct": 0.0, "weighted_usd": 0.0},
                        }
                    ],
                }
                ui._render_tabs_and_rows(proj_payload, preserve_scroll=False)
                row_h_std = ui.px(56)
                mid_std = (row_h_std - 1) // 2
                self.assertEqual(ui._last_scroll_h, row_h_std)
                self.assertEqual(self._find_text_y(ui.list_canvas, "运行中"), mid_std)
                self.assertEqual(self._find_text_y(ui.list_canvas, "2小时 15分 00秒"), mid_std)

                # 3) Intervals ("工作时段") uses unified 56px row_h and centers single-line columns
                ui.day_tab = "iv"
                iv_payload = {
                    "mode": "day",
                    "target_date": "2026-11-24",
                    "updated_at": "08:00:00",
                    "quota": {"calibrated": True},
                    "intervals": [
                        {
                            "index": 1,
                            "start": "09:00:00",
                            "end": "10:30:00",
                            "duration": "1小时 30分 00秒",
                            "weekly_quota_pct": 4.0,
                            "carbon_weekly_pct": 4.0,
                            "neon_weekly_pct": 0.0,
                            "hydrogen_weekly_pct": 0.0,
                            "other_weekly_pct": 0.0,
                        }
                    ],
                }
                ui._render_tabs_and_rows(iv_payload, preserve_scroll=False)
                self.assertEqual(ui._last_scroll_h, row_h_std)
                self.assertEqual(self._find_text_y(ui.list_canvas, "时段 01"), mid_std)
                self.assertEqual(self._find_text_y(ui.list_canvas, "09:00:00 – 10:30:00"), mid_std)
                self.assertEqual(self._find_text_y(ui.list_canvas, "1小时 30分 00秒"), mid_std)

                # 4) Dialogs ("对话框") uses unified 56px row_h and centers the advice pill vertically
                ui.day_tab = "ctx"
                dlg_payload = {
                    "mode": "day",
                    "target_date": "2026-11-24",
                    "updated_at": "08:00:00",
                    "quota": {"calibrated": True},
                    "dialogs": [
                        {
                            "conversation_id": "c1",
                            "workspace": "GravityPulse",
                            "title": "间距排查",
                            "model_label": "Neon",
                            "input_fmt": "12K",
                            "cache_fmt": "80K",
                            "output_fmt": "4K",
                            "context_tokens": 96_000,
                            "cache_hit_pct": 88.0,
                            "cum_cache_hit_pct": 91.2,
                            "cache_state": "fresh",
                            "cache_status_text": "缓存有效",
                            "cache_idle_text": "1分钟前",
                            "context_fmt": "96K",
                            "safe_limit_fmt": "230K",
                            "safe_pct": 42,
                            "advice_key": "ok",
                            "advice_text": "状态健康",
                            "advice_reason": "未触发压缩",
                            "compaction_text": "未触发压缩",
                            "is_subdialog": False,
                        }
                    ],
                }
                ui._render_tabs_and_rows(dlg_payload, preserve_scroll=False)
                self.assertEqual(ui._last_scroll_h, row_h_std)
                pill_y = self._find_text_y(ui.list_canvas, "状态健康")
                self.assertLessEqual(abs(pill_y - mid_std), 1)

                # 5) Window height accommodates both cards plus bottom shadow without clipping
                total_used = ui.px(58) + 1 + (ui.px(204) + ui.shadow_pad * 2 + ui.px(10)) + (ui.px(446) + ui.shadow_pad * 2)
                self.assertGreaterEqual(ui.win_h, total_used)

                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
