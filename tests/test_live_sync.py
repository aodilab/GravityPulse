"""Unit tests for Plan B layered live ticking, fingerprint sync, and rowid incremental WAL reading."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

from config.settings import DEFAULT_GAP_LIMIT, IS_WINDOWS, LIVE_SYNC_MS, LIVE_TICK_MS, LOCAL_TZ
from src.core import (
    DialogContextInfo,
    TimeInterval,
    compute_day_report,
    compute_live_display_update,
    compute_source_mtime_ns,
    compute_telemetry_fingerprint,
    local_week_bounds,
    report_to_dict,
    sync_telemetry_ledger,
)
from src.core.live_state import live_report_fields
from src.core.sessionizer import collect_root_sessions
from src.core.storage import _TRANSCRIPT_CACHE, load_db_metadata, read_db_dialog_telemetry
from src.ui.desktop_app import GravityPulseApp
from src.ui.system_tray import SystemTrayIcon
from tests.helpers import f_b, f_v, init_summary_db, local_utc_sql, local_utc_z, make_step_meta, write_transcript


class TestLiveSyncAndIncrementalWal(unittest.TestCase):
    def test_rowid_incremental_tail_and_lock_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [("inc1", "增量读测试", '["file:///c%3A/Work/GravityPulse"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 12, 0, 0), local_utc_sql(2026, 11, 16, 11, 0, 0))],
            )
            conv_db = root / "conversations" / "inc1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db, isolation_level=None)
            cc.execute("PRAGMA journal_mode=WAL")
            cc.execute("PRAGMA wal_autocheckpoint=0")
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            t0 = int(datetime(2026, 11, 16, 10, 0, 0, tzinfo=LOCAL_TZ).timestamp())
            rows = []
            for i in range(10):
                st = 14 if i % 2 == 0 else 15
                meta = f_b(1, f_v(1, t0 + i * 20)) if st == 14 else make_step_meta(t0 + i * 20, 1437, 2000, 300, 10000 + i * 1000)
                rows.append((i, st, meta))
            cc.executemany("INSERT INTO steps VALUES (?, ?, ?)", rows)

            ev1, cp1, ut1, pf1 = read_db_dialog_telemetry(conv_db)
            self.assertEqual(len(ev1), 5)
            self.assertEqual(len(ut1), 5)
            self.assertEqual(len(pf1), 4)

            cc.execute("INSERT INTO steps VALUES (?, ?, ?)", (10, 14, f_b(1, f_v(1, t0 + 210))))
            cc.execute("INSERT INTO steps VALUES (?, ?, ?)", (11, 15, f_b(1, f_v(1, t0 + 220))))
            ev2, _, ut2, pf2 = read_db_dialog_telemetry(conv_db)
            self.assertEqual(len(ev2), 5)
            self.assertEqual(len(ut2), 6)
            self.assertEqual(len(pf2), 5)

            cc.execute("UPDATE steps SET metadata = ? WHERE idx = 11", (make_step_meta(t0 + 220, 1437, 3000, 500, 25000),))
            ev3, cp3, ut3, pf3 = read_db_dialog_telemetry(conv_db)
            self.assertEqual(len(ev3), 6)
            self.assertEqual(ev3[-1][4], 25000)

            summary_db = root / "conversation_summaries.db"
            self.assertIn("inc1", load_db_metadata(summary_db))
            cc.execute("INSERT INTO steps VALUES (?, ?, ?)", (12, 23, f_b(1, f_v(1, t0 + 230))))
            cc.close()
            with mock.patch("src.core.storage.sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
                ev_lock, cp_lock, ut_lock, pf_lock = read_db_dialog_telemetry(conv_db)
                self.assertEqual(len(ev_lock), 6)
                self.assertEqual(len(ut_lock), 6)
                meta_lock = load_db_metadata(summary_db)
                self.assertIn("inc1", meta_lock)

    def test_fingerprint_and_pure_memory_live_stopwatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [("live1", "实时计时", '["file:///c%3A/Work/GravityPulse"]', "CASCADE_RUN_STATUS_RUNNING", "", 0, local_utc_sql(2026, 11, 16, 12, 5, 0), local_utc_sql(2026, 11, 16, 12, 0, 0))],
            )
            write_transcript(
                root, "live1",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 12, 0, 0)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 12, 5, 0)),
                ],
            )
            conv_db = root / "conversations" / "live1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            t_u = int(datetime(2026, 11, 16, 12, 0, 0, tzinfo=LOCAL_TZ).timestamp())
            cc.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [(0, 14, f_b(1, f_v(1, t_u))), (1, 15, make_step_meta(t_u + 300, 1437, 2000, 500, 10000))],
            )
            cc.commit()
            cc.close()
            fp1 = compute_telemetry_fingerprint(root)
            self.assertEqual(fp1, compute_telemetry_fingerprint(root))

            write_transcript(
                root, "live1",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 12, 0, 0)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 12, 6, 0)),
                ],
            )
            fp2 = compute_telemetry_fingerprint(root)
            self.assertNotEqual(fp1, fp2)

            now_loc = datetime(2026, 11, 16, 12, 6, 0, tzinfo=LOCAL_TZ)
            rep = compute_day_report(target_date=now_loc.date(), antigravity_dir=root, now=now_loc)
            payload = report_to_dict(rep)
            self.assertFalse(payload["live_tick_active"])
            self.assertEqual(payload["live_cap_seconds"], 0.0)
            self.assertEqual(payload["total_seconds"], 0)

            human_10, hms_10, _ = compute_live_display_update(payload, 10.0, now_loc + timedelta(seconds=10))
            self.assertEqual(hms_10, "00:00:00")
            self.assertEqual(human_10, "0 分钟")

            capped = live_report_fields(
                1800.0, True, 1,
                (TimeInterval(now_loc - timedelta(minutes=40), now_loc - timedelta(minutes=10)),),
                (), now_loc, now_loc + timedelta(hours=1),
            )
            self.assertFalse(capped["live_tick_active"])

    def test_idle_dialog_cache_countdown_and_ui_live_sync(self) -> None:
        base_now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=LOCAL_TZ)
        dlg = DialogContextInfo(
            conversation_id="d1", title="空闲衰减测试", workspace="GravityPulse", model_label="Carbon",
            is_running=False, input_tokens=4000, cache_read_tokens=160_000, output_tokens=500,
            thinking_tokens=100, last_active=base_now - timedelta(seconds=20),
        )
        payload = {
            "mode": "day",
            "target_date": "2026-11-16",
            "updated_at": "12:00:00",
            "is_today": True,
            "total_seconds": 60,
            "base_total_seconds": 60.0,
            "live_tick_active": True,
            "live_cap_seconds": 600.0,
            "dialogs": [
                {
                    "conversation_id": dlg.conversation_id, "title": dlg.title, "workspace": dlg.workspace,
                    "model_label": dlg.model_label, "is_running": False, "context_tokens": dlg.context_tokens,
                    "input_fmt": "4.0K", "cache_fmt": "160K", "output_fmt": "500",
                    "cache_hit_pct": 97.6, "cum_cache_hit_pct": 97.6,
                    "context_fmt": "164K", "safe_limit_fmt": "256K", "safe_pct": 64,
                    "compaction_count": 0, "compaction_text": "未触发压缩", "cache_state": "fresh",
                    "cache_status_text": "缓存有效", "cache_idle_text": "刚刚",
                    "last_active_ts": dlg.last_active.timestamp(), "cache_thresholds": (180, 600, 900),
                    "advice_key": "ok", "advice_text": "状态健康", "advice_reason": "未触发压缩",
                    "notify_detail": "", "switch_kind": "",
                }
            ],
        }
        _, _, ch1 = compute_live_display_update(payload, 5.0, base_now + timedelta(seconds=5))
        self.assertFalse(ch1)
        _, _, ch2 = compute_live_display_update(payload, 200.0, base_now + timedelta(seconds=200))
        self.assertTrue(ch2)
        self.assertEqual(payload["dialogs"][0]["cache_state"], "cooling")
        self.assertEqual(payload["dialogs"][0]["cache_idle_text"], "3分钟前")

        # Parent waiting on active subtask still ticks its own KV cache from fresh -> cooling -> decay -> expired
        sub_parent = {
            "conversation_id": "p_sub", "title": "任务流同步", "workspace": "demo_app",
            "model_label": "Carbon", "is_running": True, "self_running": False, "active_subtasks": 1,
            "context_tokens": 213_000, "input_fmt": "11.9K", "cache_fmt": "201K", "output_fmt": "246",
            "cache_hit_pct": 94.4, "cum_cache_hit_pct": 91.9,
            "context_fmt": "213K", "safe_limit": 512_000, "safe_limit_fmt": "512K", "safe_pct": 42,
            "compaction_count": 0, "compaction_text": "未触发压缩", "cache_state": "fresh",
            "cache_status_text": "主缓有效", "cache_idle_text": "子任务 1 · 刚刚",
            "last_active_ts": (base_now - timedelta(seconds=20)).timestamp(),
            "cache_thresholds": (180, 360, 420),
            "advice_key": "ok", "advice_text": "状态健康", "advice_reason": "未触发压缩",
            "notify_detail": "", "switch_kind": "",
        }
        sub_payload = {"base_total_seconds": 60.0, "dialogs": [sub_parent]}
        _, _, sc_cool = compute_live_display_update(sub_payload, 0.0, base_now + timedelta(seconds=280))
        self.assertTrue(sc_cool)
        self.assertEqual(sub_parent["cache_state"], "cooling")
        self.assertEqual(sub_parent["cache_status_text"], "主缓衰减")
        self.assertEqual(sub_parent["cache_idle_text"], "子任务 1 · 5分钟前")
        self.assertEqual(sub_parent["advice_key"], "ok")

        _, _, sc_decay = compute_live_display_update(sub_payload, 0.0, base_now + timedelta(seconds=360))
        self.assertTrue(sc_decay)
        self.assertEqual(sub_parent["cache_state"], "decay")
        self.assertEqual(sub_parent["cache_status_text"], "主缓将冷")

        _, _, sc_exp = compute_live_display_update(sub_payload, 0.0, base_now + timedelta(seconds=420))
        self.assertTrue(sc_exp)
        self.assertEqual(sub_parent["cache_state"], "expired")
        self.assertEqual(sub_parent["cache_status_text"], "主缓已冷")
        self.assertEqual(sub_parent["advice_key"], "ok")

        self.assertEqual(LIVE_TICK_MS, 1_000)
        self.assertEqual(LIVE_SYNC_MS, 2_000)
        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh") as mock_refresh,
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True, language="zh")
                ui._window_visible = False
                ui._current_payload = dict(payload)
                ui._payload_mono = time.monotonic() - 5.0
                ui._on_live_tick()
                self.assertEqual(ui.total_hms_lbl.cget("text"), "00:00:00")

                ui._window_visible = True
                ui._on_live_tick()
                self.assertEqual(ui.total_hms_lbl.cget("text"), "00:01:05")

                mock_refresh.reset_mock()
                ui._last_telemetry_fp = ("fp1",)
                ui._on_live_fp_done(("fp1",))
                mock_refresh.assert_not_called()
                ui._on_live_fp_done(("fp2",))
                mock_refresh.assert_called_once_with(force_recompute=True, schedule_next=False)

                # Hidden tray mode: 2s fingerprint change syncs ledger & triggers instant bg alert check without UI refresh
                mock_refresh.reset_mock()
                ui._window_visible = False
                ui._last_telemetry_fp = ("fp2",)
                with (
                    mock.patch("src.ui.live_sync.compute_telemetry_fingerprint", return_value=("fp3",)),
                    mock.patch("src.ui.live_sync.sync_telemetry_ledger", return_value={}) as mock_sync_ledger,
                    mock.patch("src.ui.live_sync.collect_dialog_contexts", return_value=()) as mock_collect,
                    mock.patch("src.ui.live_sync.serialize_dialogs", return_value=[]) as mock_ser,
                    mock.patch.object(ui, "_on_monitor_done") as mock_mon_done,
                    mock.patch("src.ui.live_sync.threading.Thread") as mock_thread,
                ):
                    mock_thread.side_effect = lambda target, daemon=True: mock.Mock(start=target)
                    ui._run_live_sync()
                    mock_sync_ledger.assert_called_once()
                    mock_collect.assert_called_once()
                    mock_ser.assert_called_once()
                    ui._on_live_fp_done(("fp3",), [], "12:00:02")
                    mock_mon_done.assert_called_once_with([], "12:00:02")
                mock_refresh.assert_not_called()
                self.assertEqual(ui._last_telemetry_fp, ("fp3",))

                # Stale source-code detection triggers clean hot-reload on wake when singleton lock is held
                self.assertGreater(compute_source_mtime_ns(Path(__file__).resolve().parent.parent), 0)
                ui._startup_code_mtime -= 1
                if IS_WINDOWS:
                    ui._mutex_handle = 999
                    with (
                        mock.patch("src.ui.live_sync.ctypes.WinDLL") as mock_dll,
                        mock.patch("src.ui.live_sync.subprocess.Popen") as mock_popen,
                    ):
                        self.assertTrue(ui._hot_reload_if_code_updated(show_ui=True))
                        self.assertEqual(ui._mutex_handle, 0)
                        mock_dll.return_value.CloseHandle.assert_called_once_with(999)
                        mock_popen.assert_called_once()
                else:
                    fake_ipc = mock.Mock(is_owned=True)
                    ui._ipc = fake_ipc
                    with mock.patch("src.ui.live_sync.subprocess.Popen") as mock_popen:
                        self.assertTrue(ui._hot_reload_if_code_updated(show_ui=True))
                        fake_ipc.release.assert_called_once()
                        mock_popen.assert_called_once()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_historical_week_is_isolated_from_live_sync_and_preserves_cache(self) -> None:
        today_loc = datetime.now(LOCAL_TZ).date()
        cur_mon, _ = local_week_bounds(today_loc)
        prev_mon = cur_mon - timedelta(days=7)

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh") as mock_refresh,
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True, language="zh")
                ui._window_visible = True
                ui.view_mode = "week"
                ui.selected_week_date = prev_mon
                ui._follow_current_week = False
                ui._week_cache[cur_mon] = (time.monotonic(), {"mode": "week", "week_start": cur_mon.isoformat()})
                ui._week_cache[prev_mon] = (time.monotonic(), {"mode": "week", "week_start": prev_mon.isoformat()})
                ui._last_telemetry_fp = ("fp_old",)

                mock_refresh.reset_mock()
                with mock.patch.object(ui, "_on_monitor_done") as mock_mon_done:
                    ui._on_live_fp_done(("fp_new",), [], "07:50:00")
                    mock_refresh.assert_not_called()
                    mock_mon_done.assert_called_once_with([], "07:50:00")
                self.assertIn(prev_mon, ui._week_cache)
                self.assertNotIn(cur_mon, ui._week_cache)

                ui._week_cache[cur_mon] = (time.monotonic(), {"mode": "week", "week_start": cur_mon.isoformat()})
                ui._on_auto_refresh_tick()
                mock_refresh.assert_not_called()
                self.assertIn(prev_mon, ui._week_cache)
                self.assertNotIn(cur_mon, ui._week_cache)

                ui._preview_period_selection()
                prev_sun = prev_mon + timedelta(days=6)
                self.assertEqual(
                    ui.date_canvas.itemcget(ui.date_text_item, "text"),
                    f"{prev_mon.strftime('%m-%d')} ~ {prev_sun.strftime('%m-%d')}",
                )
                self.assertEqual(ui.updated_lbl.cget("text"), "正在切换...")

                ui.selected_week_date = cur_mon
                ui._refresh_inflight = True
                ui._on_live_fp_done(("fp_busy",))
                mock_refresh.assert_not_called()
                self.assertTrue(ui._pending_live_refresh)
                ui._refresh_inflight = False
                ui._consume_pending_live_refresh(today_loc)
                mock_refresh.assert_called_once_with(force_recompute=True, schedule_next=False)
                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_collect_root_sessions_fast_loads_synced_transcripts_from_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_db = root / "ledger.db"
            t0 = datetime(2026, 11, 16, 10, 0, 0, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 11, 16, 10, 20, 0, tzinfo=LOCAL_TZ)
            init_summary_db(
                root,
                [("led1", "账本加速", '["file:///c%3A/Work/GravityPulse"]', "CASCADE_RUN_STATUS_IDLE", "", 0, t1.isoformat(), t1.isoformat())],
            )
            write_transcript(
                root, "led1",
                [("USER_EXPLICIT", "USER_INPUT", t0.isoformat()), ("USER_EXPLICIT", "USER_INPUT", t1.isoformat())],
            )
            meta_map = sync_telemetry_ledger(root, ledger_path=ledger_db)
            _TRANSCRIPT_CACHE.clear()

            with mock.patch("src.core.sessionizer.read_transcript_events") as mock_disk_read:
                sessions = collect_root_sessions(
                    antigravity_dir=root,
                    min_mtime_ts=0.0,
                    effective_now=t1 + timedelta(minutes=5),
                    gap_limit=DEFAULT_GAP_LIMIT,
                    meta_map=meta_map,
                    ledger_path=ledger_db,
                )
                mock_disk_read.assert_not_called()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].intervals[0].seconds, 1200.0)

            t2 = datetime(2026, 11, 16, 10, 35, 0, tzinfo=LOCAL_TZ)
            write_transcript(
                root, "led1",
                [
                    ("USER_EXPLICIT", "USER_INPUT", t0.isoformat()),
                    ("USER_EXPLICIT", "USER_INPUT", t1.isoformat()),
                    ("USER_EXPLICIT", "USER_INPUT", t2.isoformat()),
                ],
            )
            sessions2 = collect_root_sessions(
                antigravity_dir=root,
                min_mtime_ts=0.0,
                effective_now=t2 + timedelta(minutes=5),
                gap_limit=DEFAULT_GAP_LIMIT,
                meta_map=meta_map,
                ledger_path=ledger_db,
            )
            self.assertEqual(sessions2[0].intervals[0].seconds, 2100.0)
