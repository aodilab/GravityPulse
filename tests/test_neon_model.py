"""Unit tests for Gemini 4 Neon classification, calibrated quota accounting, cache TTL, and 512K switch advice."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from config.settings import (
    COMPACTION_512K_TOKENS,
    DEFAULT_CARBON_MULTIPLIER,
    DEFAULT_NEON_MULTIPLIER,
    FIVE_HOUR_WINDOW_SEC,
    HYDROGEN_INPUT_PER_M,
    LOCAL_TZ,
    NEON_CACHE_PER_M,
    NEON_INPUT_PER_M,
    NEON_OUTPUT_PER_M,
)
from src.core import (
    DialogContextInfo,
    QuotaConfig,
    classify_switch_kind,
    compute_day_report,
    compute_week_report,
    evaluate_cold_cache_breakeven,
    evaluate_dialog_advice_detail,
    evaluate_implicit_cache_status,
    report_to_dict,
    step_weighted_usd,
    week_report_to_dict,
)
from src.core.protobuf_decoder import _classify_model_strings
from src.core.quota import compute_quota_summary, load_quota_config, save_quota_config
from src.ui.desktop_app import GravityPulseApp
from src.ui.modals import build_calibrated_quota_config, parse_reset_countdown_to_start
from src.ui.primitives import FlatDialogButton
from src.ui.system_tray import SystemTrayIcon
from tests.helpers import init_summary_db, local_iso, make_step_meta, write_transcript


class TestNeonModelSupport(unittest.TestCase):
    def test_classify_neon_and_exclude_orchestrator(self) -> None:
        self.assertEqual(_classify_model_strings("gemini-4-neon", "Gemini 4 Neon (High)"), "neon")
        self.assertEqual(_classify_model_strings("gemini-4-neon-medium", "Gemini 4 Neon (Medium)"), "neon")
        self.assertEqual(_classify_model_strings("orchestrator-neon", "Orchestrator (Neon)"), "exclude")

    def test_step_weighted_usd_neon_matches_calibrated_rate(self) -> None:
        self.assertAlmostEqual(DEFAULT_CARBON_MULTIPLIER, 3.5, places=4)
        self.assertAlmostEqual(DEFAULT_NEON_MULTIPLIER, 1.75, places=4)
        h_usd = step_weighted_usd("hydrogen", 1_000_000, 10_000_000, 200_000, carbon_multiplier=DEFAULT_CARBON_MULTIPLIER)
        c_usd = step_weighted_usd("carbon", 1_000_000, 10_000_000, 200_000, carbon_multiplier=DEFAULT_CARBON_MULTIPLIER)
        n_usd = step_weighted_usd("neon", 1_000_000, 10_000_000, 200_000, carbon_multiplier=DEFAULT_CARBON_MULTIPLIER)
        expected = NEON_INPUT_PER_M + 10.0 * NEON_CACHE_PER_M + 0.2 * NEON_OUTPUT_PER_M
        self.assertAlmostEqual(c_usd, h_usd * 3.5, places=6)
        self.assertAlmostEqual(n_usd, expected, places=6)
        self.assertAlmostEqual(n_usd, h_usd * (NEON_INPUT_PER_M / HYDROGEN_INPUT_PER_M), places=6)
        n_custom = step_weighted_usd("neon", 1_000_000, 10_000_000, 200_000, carbon_multiplier=3.5, neon_multiplier=2.0)
        self.assertAlmostEqual(n_custom, h_usd * 2.0, places=6)

    def test_neon_cache_ttl_transitions(self) -> None:
        now = datetime(2026, 11, 22, 1, 0, 0, tzinfo=LOCAL_TZ)

        def make_neon(sec_ago: int, ctx: int = 120_000, cc: int = 0) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="n1", title="Neon", workspace="GravityPulse", model_label="Neon",
                is_running=False, input_tokens=5_000, cache_read_tokens=ctx - 5_000,
                output_tokens=600, thinking_tokens=200,
                last_active=now - timedelta(seconds=sec_ago),
                compaction_count=cc, context_limit=COMPACTION_512K_TOKENS,
            )

        self.assertEqual(evaluate_implicit_cache_status(make_neon(240), now)[:2], ("fresh", "缓存有效"))
        self.assertEqual(evaluate_implicit_cache_status(make_neon(330), now)[:2], ("cooling", "缓存衰减"))
        self.assertEqual(evaluate_implicit_cache_status(make_neon(390), now)[:2], ("decay", "可能失效"))
        self.assertEqual(evaluate_implicit_cache_status(make_neon(420), now)[:2], ("expired", "基本失效"))

    def test_neon_empirical_learning_ignores_pre_expansion_misses(self) -> None:
        from src.core import learn_empirical_cache_thresholds

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conv_db = root / "conversations" / "neon_epoch.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            t_old = int(datetime(2026, 11, 20, 14, 0, 0, tzinfo=LOCAL_TZ).timestamp())
            rows = [(0, 15, make_step_meta(t_old, 1532, 60_000, 500, 0))]
            idx = 1
            for _ in range(12):
                t_old += 240
                rows.append((idx, 15, make_step_meta(t_old, 1532, 62_000, 500, 0)))
                idx += 1
            t_new = int(datetime(2026, 11, 21, 20, 0, 0, tzinfo=LOCAL_TZ).timestamp())
            rows.append((idx, 15, make_step_meta(t_new, 1532, 80_000, 500, 0)))
            idx += 1
            for _ in range(6):
                t_new += 330
                rows.append((idx, 15, make_step_meta(t_new, 1532, 2_000, 500, 80_000)))
                idx += 1
            cc.executemany("INSERT INTO steps VALUES (?, ?, ?)", rows)
            cc.commit()
            cc.close()

            now_loc = datetime(2026, 11, 22, 1, 0, 0, tzinfo=LOCAL_TZ)
            th = learn_empirical_cache_thresholds(root, now_loc)
            self.assertEqual(th["Neon"], (360, 390, 420))
            self.assertEqual(th["Carbon"], (180, 240, 360))
            self.assertEqual(th["Hydrogen"], (300, 900, 1200))

    def test_neon_cold_cache_breakeven_and_512k_thresholds(self) -> None:
        now = datetime(2026, 11, 22, 1, 0, 0, tzinfo=LOCAL_TZ)

        def make_neon(ctx: int, cc: int = 0, sec_ago: int = 60) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="n2", title="Neon512K", workspace="GravityPulse", model_label="Neon",
                is_running=False, input_tokens=5_000, cache_read_tokens=max(0, ctx - 5_000),
                output_tokens=800, thinking_tokens=200,
                last_active=now - timedelta(seconds=sec_ago),
                compaction_count=cc, context_limit=COMPACTION_512K_TOKENS,
            )

        # Short cold dialog (<120K) stays healthy; long cold dialog (>=180K, >=7m) switches
        be_short = evaluate_cold_cache_breakeven(make_neon(100_000, sec_ago=450), "expired", 450)
        self.assertFalse(bool(be_short["should_switch"]))
        self.assertEqual(evaluate_dialog_advice_detail(make_neon(100_000, sec_ago=450), "expired", 450)[0], "ok")

        be_long = evaluate_cold_cache_breakeven(make_neon(185_000, sec_ago=450), "expired", 450)
        self.assertTrue(bool(be_long["should_switch"]))
        self.assertEqual(classify_switch_kind(make_neon(185_000, sec_ago=450), "expired", 450), "cache")

        # 512K pause thresholds (idle): <190K ok, 190K-229K quiet watch, >=230K Step-1 switch; cc=1 Step-2 switch
        self.assertEqual(evaluate_dialog_advice_detail(make_neon(180_000), "fresh", 60)[0], "ok")
        self.assertEqual(evaluate_dialog_advice_detail(make_neon(210_000), "fresh", 60)[0], "watch")
        self.assertEqual(classify_switch_kind(make_neon(240_000), "fresh", 60), "structural")
        self.assertEqual(classify_switch_kind(make_neon(435_000), "fresh", 60), "critical")
        self.assertEqual(classify_switch_kind(make_neon(120_000, cc=1), "fresh", 60), "structural")

    def test_end_to_end_neon_quota_and_report_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True)
            t0 = datetime(2026, 11, 19, 10, 30, 0, tzinfo=LOCAL_TZ)
            t1 = t0 + timedelta(minutes=20)
            init_summary_db(
                root,
                [("neon-cid", "Neon Task", '["file:///c:/GravityPulse"]', "", "", 0, t1.isoformat(), t1.isoformat())],
            )
            write_transcript(
                root,
                "neon-cid",
                [("USER_EXPLICIT", "USER_INPUT", t0.isoformat()), ("USER_EXPLICIT", "USER_INPUT", t1.isoformat())],
            )

            db = conv_dir / "neon-cid.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE steps (step_type INTEGER, metadata BLOB)")
            conn.execute(
                "INSERT INTO steps VALUES (15, ?)",
                (make_step_meta(int(t0.timestamp()), 1532, 1_000_000, 200_000, 10_000_000, 100_000),),
            )
            conn.commit()
            conn.close()

            cfg = QuotaConfig(
                carbon_multiplier=3.0,
                subscription_start=local_iso(2026, 11, 18, 0, 0, 0),
                weekly_cap_usd_override=100.0,
                rolling_reset_start=local_iso(2026, 11, 18, 0, 0, 0),
            )
            expected_usd = NEON_INPUT_PER_M + 10.0 * NEON_CACHE_PER_M + 0.2 * NEON_OUTPUT_PER_M
            day_rep = compute_day_report(date(2026, 11, 19), antigravity_dir=root, now=t1, quota_config=cfg)
            self.assertIsNotNone(day_rep.quota)
            assert day_rep.quota is not None
            self.assertAlmostEqual(day_rep.quota.neon_weekly_pct, expected_usd, places=3)
            self.assertAlmostEqual(day_rep.quota.total_weekly_pct, expected_usd, places=3)
            self.assertAlmostEqual(day_rep.quota.by_workspace["GravityPulse"].neon.weighted_usd, expected_usd, places=4)

            day_dict = report_to_dict(day_rep, now=t1)
            self.assertAlmostEqual(day_dict["quota"]["neon_weekly_pct"], round(expected_usd, 1), places=1)
            self.assertEqual(day_dict["dialogs"][0]["model_label"], "Neon")
            self.assertAlmostEqual(day_dict["projects"][0]["neon"]["weekly_pct"], round(expected_usd, 1), places=1)
            self.assertAlmostEqual(day_dict["intervals"][0]["neon_weekly_pct"], round(expected_usd, 1), places=1)

            wk_rep = compute_week_report(date(2026, 11, 19), antigravity_dir=root, now=t1, quota_config=cfg)
            wk_dict = week_report_to_dict(wk_rep, now=t1)
            self.assertAlmostEqual(wk_dict["quota"]["neon_weekly_pct"], round(expected_usd, 1), places=1)
            thu = next(d for d in wk_dict["days"] if d["date"] == "2026-11-19")
            self.assertAlmostEqual(thu["neon_weekly_pct"], round(expected_usd, 1), places=1)

    def test_carbon_multiplier_history_splits_pre_and_post_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True)
            t_pre = datetime(2026, 11, 18, 12, 0, 0, tzinfo=LOCAL_TZ)
            t_cut = datetime(2026, 11, 19, 10, 20, 0, tzinfo=LOCAL_TZ)
            t_post = datetime(2026, 11, 19, 16, 0, 0, tzinfo=LOCAL_TZ)
            init_summary_db(
                root,
                [("c-hist", "Carbon Shift", '["file:///c:/GravityPulse"]', "", "", 0, t_post.isoformat(), t_post.isoformat())],
            )
            db = conv_dir / "c-hist.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE steps (step_type INTEGER, metadata BLOB)")
            conn.execute("INSERT INTO steps VALUES (15, ?)", (make_step_meta(int(t_pre.timestamp()), 1437, 0, 1_000_000, 0),))
            conn.execute("INSERT INTO steps VALUES (15, ?)", (make_step_meta(int(t_post.timestamp()), 1437, 0, 1_000_000, 0),))
            conn.commit()
            conn.close()

            cfg = QuotaConfig(
                carbon_multiplier=1.3333,
                carbon_multiplier_history=((t_cut.isoformat(), 3.0),),
                subscription_start=local_iso(2026, 11, 18, 0, 0, 0),
                weekly_cap_usd_override=100.0,
                rolling_reset_start=local_iso(2026, 11, 18, 0, 0, 0),
            )
            cfg_file = root / "quota_config.json"
            save_quota_config(cfg, cfg_file)
            loaded = load_quota_config(cfg_file)
            self.assertEqual(loaded.carbon_multiplier_history, cfg.carbon_multiplier_history)

            qs = compute_quota_summary(root, t_pre - timedelta(hours=1), t_post + timedelta(hours=1), quota_config=loaded)
            expected_total = 7.5 * 3.0 + 7.5 * 1.3333
            self.assertAlmostEqual(qs.carbon_weekly_pct, expected_total, places=3)
            self.assertAlmostEqual(qs.cycle_used_pct, expected_total, places=3)

            cal = build_calibrated_quota_config(loaded, t_post + timedelta(minutes=10), 20.0, 1.5, True, antigravity_dir=root)
            self.assertEqual(cal.carbon_multiplier_history, cfg.carbon_multiplier_history)

    def test_five_hour_reset_start_realigns_5h_window_without_altering_7d(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True)
            t_old = datetime(2026, 11, 19, 20, 10, 50, tzinfo=LOCAL_TZ)
            t_reset = datetime(2026, 11, 20, 1, 9, 30, tzinfo=LOCAL_TZ)
            t_new1 = datetime(2026, 11, 20, 1, 9, 33, tzinfo=LOCAL_TZ)
            t_new2 = datetime(2026, 11, 20, 1, 14, 5, tzinfo=LOCAL_TZ)
            init_summary_db(
                root,
                [("fh-1", "5h Align", '["file:///c:/GravityPulse"]', "", "", 0, t_new2.isoformat(), t_new2.isoformat())],
            )
            db = conv_dir / "fh-1.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE steps (step_type INTEGER, metadata BLOB)")
            conn.execute("INSERT INTO steps VALUES (15, ?)", (make_step_meta(int(t_old.timestamp()), 1532, 0, 1_000_000, 0),))
            conn.execute("INSERT INTO steps VALUES (15, ?)", (make_step_meta(int(t_new1.timestamp()), 1532, 0, 1_000_000, 0),))
            conn.execute("INSERT INTO steps VALUES (15, ?)", (make_step_meta(int(t_new2.timestamp()), 1532, 0, 1_000_000, 0),))
            conn.commit()
            conn.close()

            cfg = QuotaConfig(
                carbon_multiplier=3.0,
                neon_multiplier=2.0,
                subscription_start=local_iso(2026, 11, 18, 0, 0, 0),
                weekly_cap_usd_override=400.0,
                rolling_reset_start=local_iso(2026, 11, 18, 0, 0, 0),
                five_hour_reset_start=t_reset.isoformat(),
            )
            cfg_file = root / "quota_config.json"
            save_quota_config(cfg, cfg_file)
            loaded = load_quota_config(cfg_file)
            self.assertEqual(loaded.five_hour_reset_start, t_reset.isoformat())
            self.assertAlmostEqual(loaded.neon_multiplier, 2.0, places=4)

            qs = compute_quota_summary(root, t_reset, t_new2 + timedelta(minutes=1), quota_config=loaded)
            self.assertAlmostEqual(qs.cycle_used_pct, 11.25, places=2)
            self.assertAlmostEqual(qs.five_hour_used_pct, 30.0, places=2)
            self.assertAlmostEqual(qs.five_hour_left_pct, 70.0, places=2)

            cal = build_calibrated_quota_config(loaded, t_new2 + timedelta(minutes=2), 15.0, 3.0, True, antigravity_dir=root)
            self.assertAlmostEqual(cal.neon_multiplier, 2.0, places=4)
            reset_cal = build_calibrated_quota_config(loaded, t_new2 + timedelta(minutes=2), 0.0, 3.0, True, reset_cycle=True, antigravity_dir=root)
            self.assertAlmostEqual(reset_cal.neon_multiplier, 2.0, places=4)
            self.assertEqual(reset_cal.five_hour_reset_start, t_reset.isoformat())

            now_cd = datetime(2026, 11, 24, 7, 20, 0, tzinfo=LOCAL_TZ)
            loaded_with_hist = QuotaConfig(
                carbon_multiplier=loaded.carbon_multiplier,
                neon_multiplier=loaded.neon_multiplier,
                carbon_multiplier_history=((local_iso(2026, 11, 19, 10, 20, 0), 3.0),),
                subscription_start=loaded.subscription_start,
                weekly_cap_usd_override=loaded.weekly_cap_usd_override,
                rolling_reset_start=loaded.rolling_reset_start,
                five_hour_reset_start=loaded.five_hour_reset_start,
            )
            cd_cal = build_calibrated_quota_config(
                loaded_with_hist, now_cd, 3.0, 3.0, True, reset_cycle=True, antigravity_dir=root, reset_countdown="6d 20h",
            )
            self.assertEqual(cd_cal.rolling_reset_start, local_iso(2026, 11, 24, 3, 20, 0))
            self.assertEqual(cd_cal.five_hour_reset_start, t_reset.isoformat())
            self.assertEqual(cd_cal.carbon_multiplier_history, loaded_with_hist.carbon_multiplier_history)
            zh_cal = build_calibrated_quota_config(
                loaded_with_hist, now_cd, 3.0, 3.0, True, reset_cycle=True, antigravity_dir=root, reset_countdown="6天20小时",
            )
            self.assertEqual(zh_cal.rolling_reset_start, local_iso(2026, 11, 24, 3, 20, 0))
            fallback_cal = build_calibrated_quota_config(
                loaded_with_hist, now_cd, 3.0, 3.0, True, reset_cycle=True, antigravity_dir=root, reset_countdown="8d",
            )
            self.assertEqual(fallback_cal.rolling_reset_start, local_iso(2026, 11, 24, 7, 20, 0))

    def test_parse_reset_countdown_to_start_edges_and_modal_validation(self) -> None:
        now_loc = datetime(2026, 11, 24, 7, 20, 0, tzinfo=LOCAL_TZ)
        self.assertEqual(parse_reset_countdown_to_start("6d 20h", now_loc), datetime(2026, 11, 24, 3, 20, 0, tzinfo=LOCAL_TZ))
        self.assertEqual(parse_reset_countdown_to_start("Resets in 6days 20hrs 15mins", now_loc), datetime(2026, 11, 24, 3, 35, 0, tzinfo=LOCAL_TZ))
        self.assertEqual(parse_reset_countdown_to_start("6天20时30分后重置", now_loc), datetime(2026, 11, 24, 3, 50, 0, tzinfo=LOCAL_TZ))
        self.assertEqual(parse_reset_countdown_to_start("20h", now_loc), datetime(2026, 11, 18, 3, 20, 0, tzinfo=LOCAL_TZ))
        self.assertEqual(parse_reset_countdown_to_start("45m", now_loc), datetime(2026, 11, 17, 8, 5, 0, tzinfo=LOCAL_TZ))
        self.assertEqual(parse_reset_countdown_to_start("6.5d", now_loc), datetime(2026, 11, 23, 19, 20, 0, tzinfo=LOCAL_TZ))
        self.assertEqual(parse_reset_countdown_to_start("7d", now_loc), now_loc)
        self.assertEqual(
            parse_reset_countdown_to_start("1h 46m", now_loc, cycle_sec=FIVE_HOUR_WINDOW_SEC),
            datetime(2026, 11, 24, 4, 6, 0, tzinfo=LOCAL_TZ),
        )
        utc_now = now_loc.astimezone(timezone.utc)
        naive_now = now_loc.replace(tzinfo=None)
        expected_iso = local_iso(2026, 11, 24, 3, 20, 0)
        self.assertEqual(parse_reset_countdown_to_start("6d 20h", utc_now).isoformat(timespec="seconds"), expected_iso)
        self.assertEqual(parse_reset_countdown_to_start("6d 20h", naive_now).isoformat(timespec="seconds"), expected_iso)
        for bad in ("", "   ", "0d", "0d 0h", "7d 1m", "8d", "-6d 20h", "6d 20", "1month", "10ms", "2decades", "1.2.3d", "abc"):
            self.assertIsNone(parse_reset_countdown_to_start(bad, now_loc), f"Expected None for {bad!r}")

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                mock.patch.object(SystemTrayIcon, "start"),
                mock.patch.object(SystemTrayIcon, "stop"),
                mock.patch.object(GravityPulseApp, "refresh"),
                mock.patch("src.ui.modals.save_quota_config") as mock_save,
            ):
                ui = GravityPulseApp(root, show_event_handle=0, start_hidden=True, language="zh")
                ui._quota_cfg = QuotaConfig(
                    carbon_multiplier=3.5,
                    neon_multiplier=1.75,
                    weekly_cap_usd_override=2000.0,
                    five_hour_reset_start=local_iso(2026, 11, 24, 4, 6, 0),
                )
                ui._open_calibration_dialog()
                assert ui._calib_win is not None
                card = ui._calib_win.winfo_children()[0]
                entries = [w for f in card.winfo_children() if isinstance(f, tk.Frame) for w in f.winfo_children() if isinstance(w, tk.Entry)]
                checks = [w for f in card.winfo_children() if isinstance(f, tk.Frame) for w in f.winfo_children() if isinstance(w, tk.Checkbutton)]
                buttons = [w for f in card.winfo_children() if isinstance(f, tk.Frame) for w in f.winfo_children() if isinstance(w, (tk.Button, FlatDialogButton))]
                used_entry, cd_entry = entries[0], entries[1]
                reset_chk = checks[0]
                save_btn = next(b for b in buttons if b.cget("text") == "保存并应用")

                self.assertEqual(str(used_entry.cget("state")), "normal")
                self.assertEqual(str(cd_entry.cget("state")), "disabled")

                reset_chk.invoke()
                self.assertEqual(str(used_entry.cget("state")), "disabled")
                self.assertEqual(str(cd_entry.cget("state")), "normal")

                cd_entry.insert(0, "6d 20")
                save_btn.invoke()
                mock_save.assert_not_called()
                self.assertTrue(ui._calib_win.winfo_exists())

                cd_entry.delete(0, tk.END)
                cd_entry.insert(0, "6d 20h")
                save_btn.invoke()
                mock_save.assert_called_once()
                saved_cfg = mock_save.call_args.args[0]
                self.assertEqual(saved_cfg.five_hour_reset_start, local_iso(2026, 11, 24, 4, 6, 0))
                self.assertAlmostEqual(saved_cfg.neon_multiplier, 1.75, places=4)
                self.assertFalse(ui._calib_win.winfo_exists())
                ui._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
