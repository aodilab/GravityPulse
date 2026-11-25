"""Unit tests for EN/ZH localization, live language toggling, and tray startup/shortcut actions."""
from __future__ import annotations

from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from src.ui.desktop_app import GravityPulseApp
from src.ui.i18n import (
    load_ui_language,
    localize_advice_reason,
    localize_advice_text,
    localize_cache_idle,
    localize_cache_status,
    localize_duration,
    localize_reset_str,
    localize_toast,
    localize_weekday,
    save_ui_language,
)
from src.ui.system_tray import SystemTrayIcon
from src.ui.table_views import format_ranked_flat_model_parts


class TestI18nAndTray(unittest.TestCase):
    def test_ui_language_persistence_and_converters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefs = Path(tmp) / "ui_prefs.json"
            self.assertEqual(save_ui_language("en", prefs_path=prefs), "en")
            self.assertEqual(load_ui_language(prefs_path=prefs), "en")
            self.assertEqual(save_ui_language("zh-CN", prefs_path=prefs), "zh")
            self.assertEqual(load_ui_language(prefs_path=prefs), "zh")

        self.assertEqual(localize_duration("5 小时 42 分钟", "en"), "5h 42m")
        self.assertEqual(localize_duration("2小时 08分 33秒", "en"), "2h 08m 33s")
        self.assertEqual(localize_duration("35分 09秒", "en"), "35m 09s")
        self.assertEqual(localize_weekday("周日", "en"), "Sun")
        self.assertEqual(localize_reset_str("3天4小时后重置", "en"), "resets in 3d 4h")
        self.assertEqual(localize_cache_status("缓存有效", "en"), "Warm")
        self.assertEqual(localize_cache_idle("24分钟前", "en"), "24m ago")
        self.assertEqual(localize_advice_text("建议换新", "en"), "Switch")
        self.assertEqual(localize_advice_reason("缓存失效 · 重算约新窗1.6x", "en"), "Cache cold · 1.6x fresh cost")
        self.assertEqual(localize_advice_reason("已压缩 2 次 · 换窗零损耗", "en"), "2x compacted · switch now")

        head, body = localize_toast(
            "api · Refactor (Neon)",
            "【二次提醒 · 续增 +52K】Neon 单步上下文已达 402K（跨入高耗重算区），建议开启新对话控制额度消耗。",
            "en",
        )
        self.assertEqual(head, "api · Refactor (Neon)")
        self.assertIn("[Follow-up +52K]", body)
        self.assertIn("402K", body)

        self.assertEqual(
            format_ranked_flat_model_parts({"neon_weekly_pct": 2.0, "other_weekly_pct": 0.4}, lang="en"),
            ["Neon 2.0%", "Other 0.4%"],
        )

    def test_english_ui_rendering_and_live_language_toggle(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(SystemTrayIcon, "set_tooltip") as mock_tip,
                mock.patch.object(GravityPulseApp, "refresh"),
                mock.patch("src.ui.tray_actions.save_ui_language", side_effect=lambda lang: lang),
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True, language="en")
                payload = {
                    "mode": "day",
                    "target_date": "2026-11-22",
                    "weekday_cn": "周日",
                    "is_today": True,
                    "can_prev": True,
                    "can_next": False,
                    "total_human": "5 小时 42 分钟",
                    "total_hms": "05:42:18",
                    "total_seconds": 20538,
                    "running_count": 1,
                    "interval_count": 3,
                    "project_count": 2,
                    "timeline_segments": [{"start_pct": 40.0, "width_pct": 12.0}],
                    "updated_at": "17:38:20",
                    "quota": {
                        "calibrated": True, "weekly_pct": 14.8, "cycle_left_pct": 61.6, "five_hour_left_pct": 77.5,
                        "neon_weekly_pct": 6.9, "carbon_weekly_pct": 5.2, "hydrogen_weekly_pct": 2.4, "other_weekly_pct": 0.3,
                    },
                    "dialogs": [{
                        "conversation_id": "d1", "workspace": "GravityPulse", "title": "WAL incremental parser",
                        "model_label": "Neon", "input_fmt": "3.2K", "cache_fmt": "145K", "output_fmt": "1.1K",
                        "context_tokens": 148200, "cache_hit_pct": 97.8, "cum_cache_hit_pct": 96.4,
                        "cache_state": "active", "cache_status_text": "保活中", "cache_idle_text": "运行中",
                        "context_fmt": "148K", "safe_limit_fmt": "512K", "safe_pct": 29,
                        "advice_key": "ok", "advice_text": "状态健康", "advice_reason": "未触发压缩",
                        "compaction_text": "未触发压缩",
                    }],
                    "projects": [],
                    "intervals": [],
                }
                ui._apply_payload(payload, preserve_scroll=False)
                self.assertEqual(ui.hero_eyebrow.cget("text"), "Active Work Time Today")
                self.assertEqual(ui.total_human_lbl.cget("text"), "5h 42m")
                self.assertEqual(ui.tab1_lbl.cget("text"), "Conversations")
                self.assertIn("Other 0.3%", ui.hero_quota_sub.cget("text"))
                self.assertIn("5h left 77.5%", ui.hero_quota_sub.cget("text"))
                head_texts = [
                    ui.head_canvas.itemcget(i, "text")
                    for i in ui.head_canvas.find_all()
                    if ui.head_canvas.type(i) == "text"
                ]
                self.assertIn("KV Cache", head_texts)
                self.assertIn("Advice", head_texts)

                ui._toggle_language()
                self.assertEqual(ui._lang, "zh")
                self.assertEqual(ui.hero_eyebrow.cget("text"), "今日累计工作时间")
                self.assertEqual(ui.total_human_lbl.cget("text"), "5 小时 42 分钟")
                self.assertEqual(ui.tab1_lbl.cget("text"), "对话框")
                mock_tip.assert_called()
                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_tray_startup_toggle_action(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh"),
                mock.patch.object(GravityPulseApp, "_show_switch_toast") as mock_toast,
                mock.patch("src.ui.tray_actions.is_startup_installed", side_effect=[False, True]),
                mock.patch("src.ui.tray_actions.install_startup_registry") as mock_inst,
                mock.patch("src.ui.tray_actions.ensure_watcher_running") as mock_watch,
                mock.patch("src.ui.tray_actions.uninstall_startup_registry") as mock_uninst,
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True, language="en")
                ui._handle_tray_action("toggle_startup")
                mock_inst.assert_called_once()
                mock_watch.assert_called_once()
                self.assertEqual(mock_toast.call_args[0][0], "Auto-start enabled")

                ui._handle_tray_action("toggle_startup")
                mock_uninst.assert_called_once()
                self.assertEqual(mock_toast.call_args[0][0], "Auto-start disabled")
                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
