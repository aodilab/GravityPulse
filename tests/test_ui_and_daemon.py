"""Unit tests for desktop UI transitions, silent popup policy, companion watcher, and config storage."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import date, datetime
import inspect
from pathlib import Path
import sqlite3
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from config.settings import APP_USER_MODEL_ID, LOCAL_TZ, MIN_VISIBLE_DATE, PROJECT_ROOT, REFRESH_MS, SINGLETON_MUTEX_NAME
from src.core import (
    DayReport,
    QuotaConfig,
    WeekReport,
    compute_day_report,
    compute_week_report,
    load_quota_config,
    report_to_dict,
    save_quota_config,
    week_report_to_dict,
)
from tests.helpers import init_summary_db, local_iso, local_utc_sql, local_utc_z, make_step_meta, write_transcript
from src.core.watcher import PROCESSENTRY32W, is_antigravity_running
from src.ui import desktop_app
from src.ui.desktop_app import GravityPulseApp
from src.ui.modals import build_calibrated_quota_config
from src.ui.system_tray import SystemTrayIcon
from src.ui.table_views import format_ranked_flat_model_parts, format_ranked_project_model_parts


class TestUiAndDaemon(unittest.TestCase):
    def test_watcher_and_app_resident_silent_configuration(self) -> None:
        self.assertEqual(REFRESH_MS, 30_000)
        self.assertEqual(SINGLETON_MUTEX_NAME, "Local\\GravityPulseAppSingleton")

        for py_file in (PROJECT_ROOT / "src" / "ui").glob("*.py"):
            src_txt = py_file.read_text(encoding="utf-8")
            self.assertNotIn("winsound", src_txt)
            self.assertNotIn("MessageBeep", src_txt)

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        self.assertIsInstance(is_antigravity_running(k32), bool)

    def test_window_close_and_loading_spinner_transition(self) -> None:
        sig = inspect.signature(GravityPulseApp._hide_tooltip)
        self.assertIsNone(sig.parameters["_event"].default)

        app_src = Path(desktop_app.__file__).read_text(encoding="utf-8")
        self.assertIn('start_hidden = is_bg_autostart or "--minimized" in arg_set or "--tray" in arg_set', app_src)

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh"),
                mock.patch.object(root, "deiconify"),
                mock.patch.object(root, "focus_force"),
                mock.patch.object(root, "lift"),
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True)
                self.assertFalse(ui._window_visible)

                ui.show_window()
                self.assertTrue(ui._window_visible)
                self.assertTrue(ui._loading_active)
                self.assertEqual(len(ui._spinner_canvas.find_all()), 12)
                ui._tick_spinner()
                self.assertEqual(ui._spinner_step, 1)

                ui._on_close_to_tray()
                self.assertFalse(ui._window_visible)
                self.assertFalse(ui._loading_active)
                self.assertEqual(root.state(), "withdrawn")

                ui.show_window()
                self.assertTrue(ui._window_visible)
                self.assertTrue(ui._loading_active)
                ui._hide_loading_overlay_now()
                self.assertFalse(ui._loading_active)
                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_quota_config_storage_under_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "data" / "quota_config.json"
            cfg = QuotaConfig(
                carbon_multiplier=3.0,
                anchor_start="2026-11-16T00:00:00+08:00",
                anchor_end="2026-11-16T04:00:00+08:00",
                anchor_weekly_used_pct=25.5,
                weekly_cap_usd_override=280.0,
                notify_switch=True,
            )
            save_quota_config(cfg, config_path=cfg_path)
            self.assertTrue(cfg_path.exists())
            loaded = load_quota_config(config_path=cfg_path)
            self.assertEqual(loaded, cfg)

    def test_intervals_tab_renders_model_quota_and_invalidates_signature(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh"),
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True)
                ui.day_tab = "iv"
                base_payload = {
                    "mode": "day",
                    "target_date": "2026-11-16",
                    "updated_at": "20:02:16",
                    "quota": {"calibrated": True},
                    "intervals": [
                        {
                            "index": 1,
                            "start": "00:00:00",
                            "end": "08:08:33",
                            "duration": "8小时 08分 33秒",
                            "weekly_quota_pct": 15.4,
                            "carbon_weekly_pct": 15.1,
                            "hydrogen_weekly_pct": 0.3,
                            "other_weekly_pct": 0.0,
                        }
                    ],
                }
                ui._render_tabs_and_rows(base_payload, preserve_scroll=False)
                head_texts = [
                    ui.head_canvas.itemcget(item, "text")
                    for item in ui.head_canvas.find_all()
                    if ui.head_canvas.type(item) == "text"
                ]
                row_texts = [
                    ui.list_canvas.itemcget(item, "text")
                    for item in ui.list_canvas.find_all()
                    if ui.list_canvas.type(item) == "text"
                ]
                self.assertIn("模型耗周额度", head_texts)
                self.assertIn("15.4%", row_texts)
                self.assertIn("Carbon 15.1%  ·  Hydrogen 0.3%", row_texts)

                only_h_payload = {
                    **base_payload,
                    "intervals": [{**base_payload["intervals"][0], "carbon_weekly_pct": 0.0, "hydrogen_weekly_pct": 0.1, "weekly_quota_pct": 0.1}],
                }
                ui._render_tabs_and_rows(only_h_payload, preserve_scroll=False)
                only_h_texts = [
                    ui.list_canvas.itemcget(item, "text")
                    for item in ui.list_canvas.find_all()
                    if ui.list_canvas.type(item) == "text"
                ]
                self.assertIn("Hydrogen 0.1%", only_h_texts)
                self.assertTrue(all("Carbon" not in t for t in only_h_texts))

                sig1 = ui._compute_rows_signature(base_payload, "day", "iv", True)
                changed_payload = {
                    **base_payload,
                    "intervals": [{**base_payload["intervals"][0], "carbon_weekly_pct": 16.0, "weekly_quota_pct": 16.3}],
                }
                sig2 = ui._compute_rows_signature(changed_payload, "day", "iv", True)
                self.assertNotEqual(sig1, sig2)

                uncal_payload = {**base_payload, "quota": {"calibrated": False}}
                ui._render_tabs_and_rows(uncal_payload, preserve_scroll=False)
                uncal_texts = [
                    ui.list_canvas.itemcget(item, "text")
                    for item in ui.list_canvas.find_all()
                    if ui.list_canvas.type(item) == "text"
                ]
                self.assertIn("-", uncal_texts)
                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_min_visible_date_default_unbounded_and_optional_floor(self) -> None:
        self.assertIsNone(MIN_VISIBLE_DATE)
        now_bj = datetime(2026, 11, 17, 8, 0, tzinfo=LOCAL_TZ)
        d_unbounded = report_to_dict(DayReport(date(2026, 11, 16), now_bj, 0.0, (), (), (), 0), now=now_bj)
        self.assertTrue(d_unbounded["can_prev"])

        floor = date(2026, 11, 16)
        with (
            mock.patch("src.core.reports.MIN_VISIBLE_DATE", floor),
            mock.patch("src.ui.desktop_app.MIN_VISIBLE_DATE", floor),
        ):
            d_mon = report_to_dict(DayReport(floor, now_bj, 0.0, (), (), (), 0), now=now_bj)
            d_tue = report_to_dict(DayReport(date(2026, 11, 17), now_bj, 0.0, (), (), (), 0), now=now_bj)
            self.assertFalse(d_mon["can_prev"])
            self.assertTrue(d_tue["can_prev"])

            wk_cur = week_report_to_dict(
                WeekReport(floor, date(2026, 11, 22), now_bj, 0.0, (), (), (), 0), now=now_bj,
            )
            self.assertFalse(wk_cur["can_prev"])

            root = tk.Tk()
            root.withdraw()
            try:
                with (
                    mock.patch.object(SystemTrayIcon, "start"),
                    mock.patch.object(SystemTrayIcon, "stop"),
                    mock.patch.object(GravityPulseApp, "refresh") as mock_ref,
                ):
                    ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True)
                    mock_ref.reset_mock()
                    ui.view_mode = "week"
                    ui.selected_week_date = floor
                    ui._shift_period(-1)
                    self.assertEqual(ui.selected_week_date, floor)
                    mock_ref.assert_not_called()

                    ui.view_mode = "day"
                    ui.selected_date = floor
                    ui._shift_period(-1)
                    self.assertEqual(ui.selected_date, floor)
                    mock_ref.assert_not_called()
                    ui._quit_completely()
            finally:
                try:
                    root.destroy()
                except Exception:
                    pass

    def test_calibration_official_reset_preserves_cap_and_updates_rolling_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "quota_config.json"
            prev = QuotaConfig(
                carbon_multiplier=3.0,
                anchor_start=local_iso(2026, 11, 14, 15, 4, 1),
                anchor_end=local_iso(2026, 11, 17, 7, 6, 8),
                anchor_weekly_used_pct=45.0,
                subscription_start=local_iso(2026, 11, 14, 15, 4, 1),
                weekly_cap_usd_override=1743.02,
            )
            now_bj = datetime(2026, 11, 18, 10, 0, 7, tzinfo=LOCAL_TZ)
            expected_reset = now_bj.isoformat(timespec="seconds")
            updated = build_calibrated_quota_config(
                prev, now_bj=now_bj, used_pct=0.0, mult=3.0, notify_switch=True,
                reset_cycle=True, antigravity_dir=Path(tmp),
            )
            self.assertEqual(updated.rolling_reset_start, expected_reset)
            self.assertEqual(updated.subscription_start, prev.subscription_start)
            self.assertEqual(updated.weekly_cap_usd_override, 1743.02)

            save_quota_config(updated, config_path=cfg_path)
            self.assertEqual(load_quota_config(config_path=cfg_path), updated)

            kept = build_calibrated_quota_config(
                updated, now_bj=datetime(2026, 11, 18, 11, 0, 0, tzinfo=LOCAL_TZ),
                used_pct=5.0, mult=2.5, notify_switch=False, reset_cycle=False, antigravity_dir=Path(tmp),
            )
            self.assertEqual(kept.rolling_reset_start, expected_reset)
            self.assertEqual(kept.weekly_cap_usd_override, 1743.02)
            self.assertEqual(kept.carbon_multiplier, 2.5)
            self.assertFalse(kept.notify_switch)

    def test_app_user_model_id_and_shortcut_binding(self) -> None:
        import subprocess
        from scripts.icon_builder import bind_shortcut_app_user_model_id

        self.assertEqual(APP_USER_MODEL_ID, "GravityPulse.Desktop.App")
        app_src = Path(desktop_app.__file__).read_text(encoding="utf-8")
        self.assertIn("SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)", app_src)
        self.assertIn("iconbitmap(default=str(ICON_PATH))", app_src)

        init_idx = app_src.index("_init_win32_shell()")
        tk_idx = app_src.index("root = tk.Tk()")
        self.assertLess(init_idx, tk_idx)

        try:
            from win32com.propsys import propsys, pscon
            from win32com.shell import shellcon
        except ImportError:
            return
        with tempfile.TemporaryDirectory() as tmp:
            lnk = Path(tmp) / "GravityPulse.lnk"
            ps_cmd = f"$ws=New-Object -ComObject WScript.Shell;$sc=$ws.CreateShortcut('{lnk}');$sc.TargetPath='notepad.exe';$sc.Save()"
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=True)
            self.assertTrue(bind_shortcut_app_user_model_id(lnk, app_id=APP_USER_MODEL_ID))
            store = propsys.SHGetPropertyStoreFromParsingName(str(lnk), None, shellcon.GPS_DEFAULT, propsys.IID_IPropertyStore)
            val = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
            del store
            self.assertEqual(val, APP_USER_MODEL_ID)

    def test_week_days_daily_quota_aggregation_and_tab_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            init_summary_db(
                root_dir,
                [("wd1", "周汇总额度测试", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 17, 12, 0), local_utc_sql(2026, 11, 16, 9, 0))],
            )
            write_transcript(
                root_dir, "wd1",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 9, 0)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 17, 9, 0)),
                ],
            )
            conv_db = root_dir / "conversations" / "wd1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            t_mon = int(datetime(2026, 11, 16, 10, 0, tzinfo=LOCAL_TZ).timestamp())
            t_tue_0 = int(datetime(2026, 11, 17, 0, 0, 0, tzinfo=LOCAL_TZ).timestamp())
            t_wed = int(datetime(2026, 11, 18, 9, 0, tzinfo=LOCAL_TZ).timestamp())
            cc.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [
                    (0, 15, make_step_meta(t_mon, 1437, 0, 1_000_000, 0)),       # Mon Carbon: $22.5
                    (1, 15, make_step_meta(t_tue_0, 1525, 0, 1_000_000, 0)),     # Tue 00:00:00 Hydrogen: $7.5
                    (2, 15, make_step_meta(t_wed, 1322, 0, 2_000_000, 0)),       # Wed Flash: $7.5
                ],
            )
            cc.commit()
            cc.close()

            qcfg = QuotaConfig(carbon_multiplier=3.0, subscription_start="", weekly_cap_usd_override=100.0)
            now_bj = datetime(2026, 11, 18, 12, 0, tzinfo=LOCAL_TZ)
            wk_rep = compute_week_report(target_date=date(2026, 11, 18), antigravity_dir=root_dir, now=now_bj, quota_config=qcfg)
            wk_dict = week_report_to_dict(wk_rep, now=now_bj)
            days = wk_dict["days"]
            self.assertEqual(len(days), 7)
            self.assertAlmostEqual(days[0]["weekly_quota_pct"], 22.5, places=1)
            self.assertAlmostEqual(days[0]["carbon_weekly_pct"], 22.5, places=1)
            self.assertAlmostEqual(days[1]["weekly_quota_pct"], 7.5, places=1)
            self.assertAlmostEqual(days[1]["hydrogen_weekly_pct"], 7.5, places=1)
            self.assertAlmostEqual(days[2]["weekly_quota_pct"], 7.5, places=1)
            self.assertAlmostEqual(days[2]["other_weekly_pct"], 7.5, places=1)
            self.assertEqual(days[3]["weekly_quota_pct"], 0.0)

            mon_rep = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root_dir, now=now_bj, quota_config=qcfg)
            tue_rep = compute_day_report(target_date=date(2026, 11, 17), antigravity_dir=root_dir, now=now_bj, quota_config=qcfg)
            assert mon_rep.quota is not None and tue_rep.quota is not None
            self.assertAlmostEqual(mon_rep.quota.total_weekly_pct, 22.5, places=1)
            self.assertAlmostEqual(tue_rep.quota.total_weekly_pct, 7.5, places=1)

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh"),
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True)
                ui.view_mode = "week"
                ui.week_tab = "days"
                payload = {
                    "mode": "week",
                    "week_start": "2026-11-16",
                    "updated_at": "13:05:00",
                    "quota": {"calibrated": True},
                    "days": [
                        {
                            "date": "2026-11-16", "weekday_cn": "周一", "is_today": False, "is_future": False,
                            "interval_count": 3, "conversation_count": 43, "total_detail": "12小时 45分 25秒",
                            "total_seconds": 45925, "weekly_quota_pct": 22.5, "carbon_weekly_pct": 20.1,
                            "hydrogen_weekly_pct": 2.4, "other_weekly_pct": 0.0,
                        },
                        {
                            "date": "2026-11-19", "weekday_cn": "周四", "is_today": False, "is_future": True,
                            "interval_count": 0, "conversation_count": 0, "total_detail": "0 分钟",
                            "total_seconds": 0, "weekly_quota_pct": 0.0, "carbon_weekly_pct": 0.0,
                            "hydrogen_weekly_pct": 0.0, "other_weekly_pct": 0.0,
                        },
                    ],
                }
                ui._render_tabs_and_rows(payload, preserve_scroll=False)
                head_texts = [
                    ui.head_canvas.itemcget(i, "text")
                    for i in ui.head_canvas.find_all()
                    if ui.head_canvas.type(i) == "text"
                ]
                row_texts = [
                    ui.list_canvas.itemcget(i, "text")
                    for i in ui.list_canvas.find_all()
                    if ui.list_canvas.type(i) == "text"
                ]
                self.assertIn("当日耗周额度", head_texts)
                self.assertIn("22.5%", row_texts)
                self.assertIn("Carbon 20.1%  ·  Hydrogen 2.4%", row_texts)
                self.assertIn("-", row_texts)

                sig1 = ui._compute_rows_signature(payload, "week", "days", True)
                changed = {
                    **payload,
                    "days": [{**payload["days"][0], "weekly_quota_pct": 24.0, "carbon_weekly_pct": 21.6}, payload["days"][1]],
                }
                sig2 = ui._compute_rows_signature(changed, "week", "days", True)
                self.assertNotEqual(sig1, sig2)
                self.assertNotEqual(sig1, ui._compute_rows_signature(payload, "week", "days", False))

                uncal = {**payload, "quota": {"calibrated": False}}
                ui._render_tabs_and_rows(uncal, preserve_scroll=False)
                uncal_texts = [
                    ui.list_canvas.itemcget(i, "text")
                    for i in ui.list_canvas.find_all()
                    if ui.list_canvas.type(i) == "text"
                ]
                self.assertNotIn("22.5%", uncal_texts)
                self.assertIn("-", uncal_texts)

                sw = root.winfo_screenwidth()
                with (
                    mock.patch.object(ui.help_canvas, "winfo_rootx", return_value=sw - 48),
                    mock.patch.object(ui.help_canvas, "winfo_rooty", return_value=60),
                    mock.patch.object(ui.help_canvas, "winfo_width", return_value=34),
                    mock.patch.object(ui.help_canvas, "winfo_height", return_value=34),
                ):
                    ui._show_tooltip(tk.Event())
                    assert ui._tooltip_win is not None
                    ui._tooltip_win.update_idletasks()
                    geom_x = int(ui._tooltip_win.geometry().split("+")[1])
                    self.assertLessEqual(geom_x + ui._tooltip_win.winfo_reqwidth(), sw - 8)
                    ui._hide_tooltip()
                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_model_quota_breakdown_orders_high_consumption_first(self) -> None:
        self.assertEqual(
            format_ranked_project_model_parts({
                "carbon": {"tokens": 50_000, "weekly_pct": 0.8, "weighted_usd": 0.8},
                "neon": {"tokens": 80_000, "weekly_pct": 1.7, "weighted_usd": 1.7},
                "hydrogen": {"tokens": 10_000, "weekly_pct": 0.0, "weighted_usd": 0.02},
                "other": {"tokens": 0, "weekly_pct": 0.0, "weighted_usd": 0.0},
            }),
            ["Neon 1.7%", "Carbon 0.8%", "Hydrogen 0.0%"],
        )
        self.assertEqual(
            format_ranked_project_model_parts({
                "carbon": {"tokens": 90_000, "weekly_pct": 2.7, "weighted_usd": 2.68},
                "neon": {"tokens": 90_000, "weekly_pct": 2.7, "weighted_usd": 2.74},
                "hydrogen": {"tokens": 1_000, "weekly_pct": 0.0, "weighted_usd": 0.01},
                "other": {"tokens": 0, "weekly_pct": 0.0, "weighted_usd": 0.0},
            }),
            ["Neon 2.7%", "Carbon 2.7%", "Hydrogen 0.0%"],
        )
        self.assertEqual(
            format_ranked_flat_model_parts({
                "carbon_weekly_pct": 0.4,
                "neon_weekly_pct": 0.9,
                "hydrogen_weekly_pct": 1.2,
                "other_weekly_pct": 0.2,
            }),
            ["Hydrogen 1.2%", "Neon 0.9%", "Carbon 0.4%", "其他 0.2%"],
        )

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh"),
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True)
                ui.day_tab = "conv"
                payload = {
                    "mode": "day",
                    "target_date": "2026-11-20",
                    "updated_at": "01:55:00",
                    "quota": {
                        "calibrated": True, "weekly_pct": 3.5, "cycle_left_pct": 80.0, "five_hour_left_pct": 90.0,
                        "carbon_weekly_pct": 0.8, "neon_weekly_pct": 1.7, "hydrogen_weekly_pct": 1.0, "other_weekly_pct": 0.0,
                    },
                    "projects": [{
                        "workspace": "GravityPulse", "is_running": False, "conversation_count": 2,
                        "duration": "30分 00秒", "bar_pct": 100.0, "weekly_quota_pct": 2.5,
                        "carbon": {"tokens": 1000, "weekly_pct": 0.8, "weighted_usd": 0.8},
                        "neon": {"tokens": 2000, "weekly_pct": 1.7, "weighted_usd": 1.7},
                        "hydrogen": {"tokens": 500, "weekly_pct": 0.0, "weighted_usd": 0.01},
                        "other": {"tokens": 0, "weekly_pct": 0.0, "weighted_usd": 0.0},
                    }],
                }
                ui._update_hero_quota(payload["quota"], is_week=False, is_active=True)
                self.assertEqual(
                    ui.hero_quota_sub.cget("text"),
                    "Neon 1.7%  ·  Hydrogen 1.0%  ·  Carbon 0.8%  ·  5h余 90.0%",
                )
                ui._render_tabs_and_rows(payload, preserve_scroll=False)
                row_texts = [
                    ui.list_canvas.itemcget(i, "text")
                    for i in ui.list_canvas.find_all()
                    if ui.list_canvas.type(i) == "text"
                ]
                self.assertIn("Neon 1.7%  ·  Carbon 0.8%  ·  Hydrogen 0.0%", row_texts)
                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass
