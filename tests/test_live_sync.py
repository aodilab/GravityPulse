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

from config.settings import LIVE_SYNC_MS, LIVE_TICK_MS, LOCAL_TZ
from src.core import (
    DialogContextInfo,
    TimeInterval,
    compute_day_report,
    compute_live_display_update,
    compute_source_mtime_ns,
    compute_telemetry_fingerprint,
    report_to_dict,
)
from src.core.live_state import live_report_fields
from src.core.storage import load_db_metadata, read_db_dialog_telemetry
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

            # Append step 10 (user) and step 11 (incomplete planner), then update step 11 in place
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

            # Simulate SQLite lock error after another write: must return last-good cache instead of []
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

            now_bj = datetime(2026, 11, 16, 12, 6, 0, tzinfo=LOCAL_TZ)
            rep = compute_day_report(target_date=now_bj.date(), antigravity_dir=root, now=now_bj)
            payload = report_to_dict(rep)
            # Speculative stopwatch tail is disabled: running lonely message stays at 0s until next user message confirms presence
            self.assertFalse(payload["live_tick_active"])
            self.assertEqual(payload["live_cap_seconds"], 0.0)
            self.assertEqual(payload["total_seconds"], 0)

            human_10, hms_10, _ = compute_live_display_update(payload, 10.0, now_bj + timedelta(seconds=10))
            self.assertEqual(hms_10, "00:00:00")
            self.assertEqual(human_10, "0 分钟")

            capped = live_report_fields(
                1800.0, True, 1,
                (TimeInterval(now_bj - timedelta(minutes=40), now_bj - timedelta(minutes=10)),),
                (), now_bj, now_bj + timedelta(hours=1),
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

        self.assertEqual(LIVE_TICK_MS, 1_000)
        self.assertEqual(LIVE_SYNC_MS, 3_000)
        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh") as mock_refresh,
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True)
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

                # Hidden tray mode: 3s fingerprint change syncs ledger in background without triggering UI refresh
                mock_refresh.reset_mock()
                ui._window_visible = False
                ui._last_telemetry_fp = ("fp2",)
                with (
                    mock.patch("src.ui.live_sync.compute_telemetry_fingerprint", return_value=("fp3",)),
                    mock.patch("src.ui.live_sync.sync_telemetry_ledger") as mock_sync_ledger,
                    mock.patch("src.ui.live_sync.threading.Thread") as mock_thread,
                ):
                    mock_thread.side_effect = lambda target, daemon=True: mock.Mock(start=target)
                    ui._run_live_sync()
                    mock_sync_ledger.assert_called_once()
                ui._on_live_fp_done(("fp3",))
                mock_refresh.assert_not_called()
                self.assertEqual(ui._last_telemetry_fp, ("fp3",))

                # Stale source-code detection triggers clean hot-reload on wake when mutex_handle != 0
                self.assertGreater(compute_source_mtime_ns(Path(__file__).resolve().parent.parent), 0)
                ui._mutex_handle = 999
                ui._startup_code_mtime -= 1
                with (
                    mock.patch("src.ui.live_sync.ctypes.WinDLL") as mock_dll,
                    mock.patch("src.ui.live_sync.subprocess.Popen") as mock_popen,
                ):
                    self.assertTrue(ui._hot_reload_if_code_updated(show_ui=True))
                    self.assertEqual(ui._mutex_handle, 0)
                    mock_dll.return_value.CloseHandle.assert_called_once_with(999)
                    mock_popen.assert_called_once()
        finally:
            try:
                root.destroy()
            except Exception:
                pass
