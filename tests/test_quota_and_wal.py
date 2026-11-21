"""Unit tests for quota calibration, rolling 7d/5h windows, and SQLite WAL extraction."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from config.settings import FIVE_HOUR_TO_WEEKLY_RATIO, LOCAL_TZ
from src.core.ledger import _LEDGER_CACHE
from src.core import (
    QuotaConfig,
    compute_day_report,
    compute_week_report,
    report_to_dict,
    resolve_current_cycle_anchor_start,
    week_report_to_dict,
)
from tests.helpers import (
    f_b,
    f_v,
    init_summary_db,
    local_iso,
    local_utc_sql,
    local_utc_z,
    make_step_meta,
    make_step_meta_with_end,
    write_transcript,
)


class TestQuotaAndWal(unittest.TestCase):
    def test_quota_calibration_carbon_hydrogen_and_excludes_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [("c1", "项目Alpha", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 12, 0), local_utc_sql(2026, 11, 15, 10, 0))],
            )
            write_transcript(
                root, "c1",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 10, 0)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 15, 10, 20)),
                ],
            )

            conv_db = root / "conversations" / "c1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata BLOB)")
            ts_bj = int(datetime(2026, 11, 15, 2, 10, tzinfo=LOCAL_TZ).timestamp())
            cc.executemany(
                "INSERT INTO steps VALUES (?, ?)",
                [
                    (0, make_step_meta(ts_bj, 1525, 1_000_000, 1_000_000, 0, 600_000)),
                    (1, make_step_meta(ts_bj + 10, 1437, 1_000_000, 1_000_000, 0, 500_000)),
                    (2, make_step_meta(ts_bj + 20, 1667, 99_000_000, 99_000_000, 0, 0)),
                    (3, f_b(1, f_v(1, ts_bj + 30)) + f_b(9, f_v(1, 1437))),
                ],
            )
            cc.commit()
            cc.close()

            qcfg = QuotaConfig(
                carbon_multiplier=2.5,
                anchor_start=local_iso(2026, 11, 15, 2, 0, 0),
                anchor_end=local_iso(2026, 11, 15, 2, 30, 0),
                anchor_weekly_used_pct=10.0,
                subscription_start="",
                daily_divisor=7.0,
            )
            now_bj = datetime(2026, 11, 15, 12, 0, tzinfo=LOCAL_TZ)
            rep = compute_day_report(target_date=date(2026, 11, 15), antigravity_dir=root, now=now_bj, quota_config=qcfg)
            payload = report_to_dict(rep, now=now_bj)
            self.assertTrue(payload["quota"]["calibrated"])
            self.assertAlmostEqual(payload["quota"]["weekly_cap_usd"], 315.0, places=2)
            self.assertAlmostEqual(payload["quota"]["daily_cap_usd"], 45.0, places=2)
            self.assertAlmostEqual(payload["quota"]["weekly_pct"], 10.0, places=2)
            self.assertAlmostEqual(payload["quota"]["daily_pct"], 70.0, places=1)
            alpha = payload["projects"][0]
            self.assertAlmostEqual(alpha["carbon"]["daily_pct"], 50.0, places=1)
            self.assertAlmostEqual(alpha["hydrogen"]["daily_pct"], 20.0, places=1)
            self.assertEqual(alpha["carbon"]["thinking_tokens"], 500_000)
            self.assertEqual(alpha["hydrogen"]["thinking_tokens"], 600_000)
            self.assertEqual(alpha["carbon"]["input_fmt"], "1.0M")
            self.assertEqual(alpha["carbon"]["output_fmt"], "1.0M")
            self.assertEqual(alpha["carbon"]["thinking_pct"], 50)

            self.assertEqual(len(payload["dialogs"]), 1)
            dlg = payload["dialogs"][0]
            self.assertEqual(dlg["workspace"], "Alpha")
            self.assertEqual(dlg["model_label"], "Carbon")
            self.assertEqual(dlg["context_tokens"], 1_000_000)
            self.assertEqual(dlg["compact_limit"], 256_000)
            self.assertEqual(dlg["advice_key"], "switch")
            self.assertEqual(dlg["cache_hit_pct"], 0.0)
            self.assertEqual(dlg["cum_cache_hit_pct"], 0.0)

            ann_file = root / "annotations" / "c1.pbtxt"
            ann_file.parent.mkdir(parents=True, exist_ok=True)
            ann_file.write_text('title:"项目Alpha" archived:true\n', encoding="utf-8")
            rep_arch = compute_day_report(target_date=date(2026, 11, 15), antigravity_dir=root, now=now_bj, quota_config=qcfg)
            payload_arch = report_to_dict(rep_arch, now=now_bj)
            self.assertEqual(payload_arch["dialogs"], [])
            self.assertAlmostEqual(payload_arch["quota"]["weekly_pct"], 10.0, places=2)

    def test_wal_updates_compaction_count_and_midnight_active_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [("w1", "跨零点对话", '["file:///c%3A/Work/GravityPulse"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 23, 55), local_utc_sql(2026, 11, 15, 23, 40))],
            )
            conv_db = root / "conversations" / "w1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db, isolation_level=None)
            cc.execute("PRAGMA journal_mode=WAL")
            cc.execute("PRAGMA wal_autocheckpoint=0")
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            ts_2350 = int(datetime(2026, 11, 15, 23, 50, tzinfo=LOCAL_TZ).timestamp())
            cc.execute("INSERT INTO steps VALUES (?, ?, ?)", (0, 15, make_step_meta(ts_2350, 1437, 20000, 500, 80000)))

            now_0005 = datetime(2026, 11, 16, 0, 5, tzinfo=LOCAL_TZ)
            rep1 = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_0005)
            self.assertEqual(len(rep1.dialogs), 1)
            self.assertEqual(rep1.dialogs[0].context_tokens, 100_000)
            self.assertEqual(rep1.dialogs[0].compaction_count, 0)

            cc.execute("INSERT INTO steps VALUES (?, ?, ?)", (1, 23, None))
            cc.execute("INSERT INTO steps VALUES (?, ?, ?)", (2, 15, make_step_meta(ts_2350 + 60, 1437, 4000, 600, 180000)))
            rep2 = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_0005)
            self.assertEqual(rep2.dialogs[0].context_tokens, 184_000)
            self.assertEqual(rep2.dialogs[0].compaction_count, 1)
            cc.close()

    def test_pre_user_wakeup_trimmed_and_cycle_anchor_rollover(self) -> None:
        cfg = QuotaConfig(
            carbon_multiplier=3.0,
            anchor_start=local_iso(2026, 11, 14, 14, 54, 0),
            anchor_end=local_iso(2026, 11, 15, 18, 0, 0),
            anchor_weekly_used_pct=19.0,
            subscription_start=local_iso(2026, 11, 14, 14, 54, 0),
        )
        self.assertEqual(
            resolve_current_cycle_anchor_start(cfg, datetime(2026, 11, 18, 12, 0, tzinfo=LOCAL_TZ)),
            local_iso(2026, 11, 14, 14, 54, 0),
        )
        self.assertEqual(
            resolve_current_cycle_anchor_start(cfg, datetime(2026, 11, 22, 10, 0, tzinfo=LOCAL_TZ)),
            local_iso(2026, 11, 21, 14, 54, 0),
        )

    def test_rolling_7d_and_5h_quota_across_monday_and_all_gemini_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [("r1", "全模型滚动测试", '["file:///c%3A/Work/GravityPulse"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 0, 30), local_utc_sql(2026, 11, 16, 0, 20))],
            )
            conv_db = root / "conversations" / "r1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            ts_sun = int(datetime(2026, 11, 15, 16, 0, tzinfo=LOCAL_TZ).timestamp())
            ts_mon = int(datetime(2026, 11, 16, 0, 20, tzinfo=LOCAL_TZ).timestamp())
            cc.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [
                    (0, 15, make_step_meta(ts_sun, 1437, 0, 1_000_000, 0)),
                    (1, 15, make_step_meta(ts_sun + 10, 1418, 0, 1_000_000, 0)),
                    (2, 15, make_step_meta(ts_mon, 1322, 0, 2_000_000, 0)),
                    (3, 15, make_step_meta(ts_mon + 5, 1669, 50_000_000, 50_000_000, 0)),
                ],
            )
            cc.commit()
            cc.close()

            qcfg = QuotaConfig(
                carbon_multiplier=3.0,
                subscription_start=local_iso(2026, 11, 15, 16, 0, 0),
                weekly_cap_usd_override=200.0,
            )
            now_mon = datetime(2026, 11, 16, 0, 30, tzinfo=LOCAL_TZ)
            wk_rep = compute_week_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_mon, quota_config=qcfg)
            wk_dict = week_report_to_dict(wk_rep, now=now_mon)
            self.assertAlmostEqual(wk_dict["quota"]["weekly_pct"], 3.8, places=1)
            self.assertAlmostEqual(wk_dict["quota"]["other_weekly_pct"], 3.8, places=1)
            self.assertAlmostEqual(wk_dict["quota"]["cycle_used_pct"], 20.0, places=1)
            self.assertAlmostEqual(wk_dict["quota"]["cycle_left_pct"], 80.0, places=1)
            self.assertAlmostEqual(wk_dict["quota"]["five_hour_left_pct"], round(100.0 - 7.5 / (200.0 * FIVE_HOUR_TO_WEEKLY_RATIO) * 100.0, 1), places=1)
            self.assertEqual(wk_dict["dialogs"][0]["model_label"], "其他")

    def test_historical_compaction_scoping_and_step_end_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [("hc1", "跨天压缩验证", '["file:///c%3A/Work/GravityPulse"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 1, 10), local_utc_sql(2026, 11, 15, 23, 50))],
            )
            conv_db = root / "conversations" / "hc1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            ts_sun_start = int(datetime(2026, 11, 15, 23, 56, 0, tzinfo=LOCAL_TZ).timestamp())
            ts_sun_end = int(datetime(2026, 11, 15, 23, 58, 30, tzinfo=LOCAL_TZ).timestamp())
            ts_mon_cp = int(datetime(2026, 11, 16, 1, 10, 0, tzinfo=LOCAL_TZ).timestamp())
            cc.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [
                    (0, 23, f_b(1, f_v(1, ts_sun_start - 600))),
                    (1, 15, make_step_meta_with_end(ts_sun_start, ts_sun_end, 1437, 4_000, 2_000, 180_000)),
                    (2, 23, f_b(1, f_v(1, ts_mon_cp))),
                    (3, 23, f_b(1, f_v(1, ts_mon_cp + 100))),
                    (4, 23, f_b(1, f_v(1, ts_mon_cp + 200))),
                    (5, 23, f_b(1, f_v(1, ts_mon_cp + 300))),
                    (6, 15, make_step_meta_with_end(ts_mon_cp + 310, ts_mon_cp + 350, 1437, 30_000, 500, 0)),
                ],
            )
            cc.commit()
            cc.close()

            now_mon = datetime(2026, 11, 16, 1, 30, 0, tzinfo=LOCAL_TZ)
            rep_sun = compute_day_report(target_date=date(2026, 11, 15), antigravity_dir=root, now=now_mon)
            dict_sun = report_to_dict(rep_sun, now=now_mon)
            self.assertEqual(len(dict_sun["dialogs"]), 1)
            dlg_sun = dict_sun["dialogs"][0]
            self.assertEqual(dlg_sun["compaction_count"], 1)
            self.assertEqual(dlg_sun["last_active"], "23:58:30")
            self.assertEqual(dlg_sun["cache_state"], "fresh")
            self.assertEqual(dlg_sun["advice_key"], "ok")

            rep_mon = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_mon)
            dict_mon = report_to_dict(rep_mon, now=now_mon)
            self.assertEqual(dict_mon["dialogs"][0]["compaction_count"], 5)
            self.assertEqual(dict_mon["dialogs"][0]["advice_key"], "switch")

    def test_sqlite_user_turn_and_post_finish_turn_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [("ut1", "轮次识别验证", '["file:///c%3A/Work/demo_app"]', "CASCADE_RUN_STATUS_RUNNING", "", 0, local_utc_sql(2026, 11, 16, 1, 30), local_utc_sql(2026, 11, 16, 1, 20))],
            )
            conv_db = root / "conversations" / "ut1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            cc.execute("CREATE INDEX idx_step_type ON steps(step_type)")
            t0 = int(datetime(2026, 11, 16, 1, 0, 0, tzinfo=LOCAL_TZ).timestamp())
            cc.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [
                    (0, 14, make_step_meta(t0)),
                    (1, 15, make_step_meta(t0 + 20, 1437, 25_000, 1_000, 0)),
                    (2, 14, make_step_meta(t0 + 120)),
                    (3, 15, make_step_meta(t0 + 140, 1437, 3_000, 500, 26_000)),
                    (4, 132, make_step_meta(t0 + 145)),
                    (5, 14, make_step_meta(t0 + 150)),
                    (6, 14, make_step_meta(t0 + 155)),
                    (7, 15, make_step_meta(t0 + 180, 1437, 4_000, 800, 30_000)),
                ],
            )
            cc.commit()
            cc.close()

            now_bj = datetime(2026, 11, 16, 1, 35, 0, tzinfo=LOCAL_TZ)
            rep = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_bj)
            d = report_to_dict(rep, now=now_bj)["dialogs"][0]
            self.assertEqual(d["user_turn_count"], 4)
            self.assertEqual(d["post_finish_turn_count"], 1)

    def test_projects_sorted_by_quota_consumption_descending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("p_long", "长时长低额度", '["file:///c%3A/Work/LongLow"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 11, 0), local_utc_sql(2026, 11, 16, 9, 0)),
                    ("p_high", "短时长高额度", '["file:///c%3A/Work/ShortHigh"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 12, 10), local_utc_sql(2026, 11, 16, 12, 0)),
                ],
            )
            write_transcript(root, "p_long", [("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 9, 0)), ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 9, 25)), ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 9, 50))])
            write_transcript(root, "p_high", [("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 12, 0)), ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 12, 10))])

            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            ts_long = int(datetime(2026, 11, 16, 9, 10, tzinfo=LOCAL_TZ).timestamp())
            ts_high = int(datetime(2026, 11, 16, 12, 5, tzinfo=LOCAL_TZ).timestamp())
            for cid, ts, in_tok in (("p_long", ts_long, 100_000), ("p_high", ts_high, 2_000_000)):
                cc = sqlite3.connect(conv_dir / f"{cid}.db")
                cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
                cc.execute("INSERT INTO steps VALUES (?, ?, ?)", (0, 15, make_step_meta(ts, 1437, in_tok, 10_000, 0)))
                cc.commit()
                cc.close()

            qcfg = QuotaConfig(
                carbon_multiplier=3.0,
                anchor_start=local_iso(2026, 11, 16, 8, 0, 0),
                anchor_end=local_iso(2026, 11, 16, 13, 0, 0),
                anchor_weekly_used_pct=10.0,
                subscription_start="",
            )
            now_bj = datetime(2026, 11, 16, 14, 0, tzinfo=LOCAL_TZ)
            day_payload = report_to_dict(compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_bj, quota_config=qcfg), now=now_bj)
            wk_payload = week_report_to_dict(compute_week_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_bj, quota_config=qcfg), now=now_bj)
            self.assertEqual([p["workspace"] for p in day_payload["projects"]], ["ShortHigh", "LongLow"])
            self.assertEqual([p["workspace"] for p in wk_payload["projects"]], ["ShortHigh", "LongLow"])
            self.assertEqual(day_payload["projects"][0]["bar_pct"], 100.0)
            self.assertLess(day_payload["projects"][1]["bar_pct"], 20.0)
            self.assertEqual(wk_payload["projects"][0]["bar_pct"], 100.0)
            self.assertLess(wk_payload["projects"][1]["bar_pct"], 20.0)

    def test_interval_quota_aggregates_carbon_and_hydrogen_across_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("iv_a", "时段1项目A", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 9, 30), local_utc_sql(2026, 11, 16, 9, 0)),
                    ("iv_b", "时段1项目B", '["file:///c%3A/Work/Beta"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 9, 40), local_utc_sql(2026, 11, 16, 9, 20)),
                    ("iv_c", "时段2项目A", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 14, 30), local_utc_sql(2026, 11, 16, 14, 0)),
                    ("iv_mid", "跨零点会话", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 17, 0, 5), local_utc_sql(2026, 11, 16, 23, 50)),
                ],
            )
            write_transcript(root, "iv_a", [("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 9, 0)), ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 9, 30))])
            write_transcript(
                root, "iv_b",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 9, 20)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 9, 40)),
                ],
            )
            write_transcript(
                root, "iv_c",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 14, 0)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 14, 20)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 14, 30)),
                ],
            )
            write_transcript(
                root, "iv_mid",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 23, 50)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 17, 0, 5)),
                ],
            )

            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            t1_a = int(datetime(2026, 11, 16, 9, 10, tzinfo=LOCAL_TZ).timestamp())
            t1_b = int(datetime(2026, 11, 16, 9, 35, tzinfo=LOCAL_TZ).timestamp())
            t2_c = int(datetime(2026, 11, 16, 14, 10, tzinfo=LOCAL_TZ).timestamp())
            t_next0 = int(datetime(2026, 11, 17, 0, 0, 0, tzinfo=LOCAL_TZ).timestamp())
            for cid, rows in (
                ("iv_a", [(0, 15, make_step_meta(t1_a, 1437, 0, 1_000_000, 0))]),       # Carbon: $22.5 (3x * $7.5)
                ("iv_b", [(0, 15, make_step_meta(t1_b, 1525, 0, 1_000_000, 0))]),       # Hydrogen: $7.5
                ("iv_c", [(0, 15, make_step_meta(t2_c, 1525, 0, 500_000, 0)), (1, 15, make_step_meta(t2_c + 5, 1322, 0, 1_000_000, 0))]),  # H: $3.75, Flash: $3.75
                ("iv_mid", [(0, 15, make_step_meta(t_next0, 1437, 0, 1_000_000, 0))]),   # Next-day 00:00:00
            ):
                cc = sqlite3.connect(conv_dir / f"{cid}.db")
                cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
                cc.executemany("INSERT INTO steps VALUES (?, ?, ?)", rows)
                cc.commit()
                cc.close()

            qcfg = QuotaConfig(
                carbon_multiplier=3.0,
                subscription_start="",
                weekly_cap_usd_override=100.0,
            )
            now_bj = datetime(2026, 11, 17, 1, 0, tzinfo=LOCAL_TZ)
            rep = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_bj, quota_config=qcfg)
            payload = report_to_dict(rep, now=now_bj)
            ivs = payload["intervals"]
            self.assertEqual(len(ivs), 3)
            self.assertAlmostEqual(ivs[0]["carbon_weekly_pct"], 22.5, places=1)
            self.assertAlmostEqual(ivs[0]["hydrogen_weekly_pct"], 7.5, places=1)
            self.assertAlmostEqual(ivs[0]["weekly_quota_pct"], 30.0, places=1)
            self.assertAlmostEqual(ivs[1]["carbon_weekly_pct"], 0.0, places=1)
            self.assertAlmostEqual(ivs[1]["hydrogen_weekly_pct"], 3.8, places=1)
            self.assertAlmostEqual(ivs[1]["other_weekly_pct"], 3.8, places=1)
            self.assertAlmostEqual(ivs[1]["weekly_quota_pct"], 7.5, places=1)
            self.assertAlmostEqual(ivs[2]["carbon_weekly_pct"], 0.0, places=1)
            self.assertAlmostEqual(ivs[2]["weekly_quota_pct"], 0.0, places=1)

            uncal_cfg = QuotaConfig(anchor_start="", anchor_end="", subscription_start="", weekly_cap_usd_override=0.0)
            rep_uncal = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_bj, quota_config=uncal_cfg)
            payload_uncal = report_to_dict(rep_uncal, now=now_bj)
            self.assertFalse(payload_uncal["quota"]["calibrated"])
            self.assertEqual(payload_uncal["intervals"][0]["weekly_quota_pct"], 0.0)

    def test_ledger_preserves_work_and_quota_after_deleting_dialogs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "antigravity"
            root.mkdir(parents=True, exist_ok=True)
            ledger_db = Path(tmp) / "data" / "telemetry_ledger.db"
            init_summary_db(
                root,
                [
                    ("d1", "将删对话", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_RUNNING", "", 0, local_utc_sql(2026, 11, 17, 10, 20), local_utc_sql(2026, 11, 17, 10, 20)),
                    ("d1_sub", "", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "d1", 1, local_utc_sql(2026, 11, 17, 10, 15), "0001-01-01 00:00:00+00:00"),
                    ("d2", "保留对话", '["file:///c%3A/Work/Beta"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 17, 11, 10), local_utc_sql(2026, 11, 17, 11, 10)),
                ],
            )
            write_transcript(
                root, "d1",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 17, 10, 0)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 17, 10, 20)),
                ],
            )
            write_transcript(
                root, "d1_sub",
                [("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 17, 10, 15))],
            )
            write_transcript(
                root, "d2",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 17, 11, 0)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 17, 11, 10)),
                ],
            )

            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            t1 = int(datetime(2026, 11, 17, 10, 10, tzinfo=LOCAL_TZ).timestamp())
            t2 = int(datetime(2026, 11, 17, 11, 5, tzinfo=LOCAL_TZ).timestamp())
            for cid, rows in (
                ("d1", [(0, 15, make_step_meta(t1, 1437, 0, 1_000_000, 0))]),       # Carbon: $22.5
                ("d1_sub", [(0, 15, make_step_meta(t1 + 60, 1525, 0, 1_000_000, 0))]),  # Hydrogen: $7.5
                ("d2", [(0, 15, make_step_meta(t2, 1525, 0, 1_000_000, 0))]),       # Hydrogen: $7.5
            ):
                cc = sqlite3.connect(conv_dir / f"{cid}.db")
                cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
                cc.executemany("INSERT INTO steps VALUES (?, ?, ?)", rows)
                cc.commit()
                cc.close()

            qcfg = QuotaConfig(carbon_multiplier=3.0, subscription_start="", weekly_cap_usd_override=100.0)
            now_bj = datetime(2026, 11, 17, 12, 0, tzinfo=LOCAL_TZ)
            rep_before = compute_day_report(
                target_date=date(2026, 11, 17), antigravity_dir=root, now=now_bj, quota_config=qcfg, ledger_path=ledger_db,
            )
            self.assertEqual(rep_before.total_seconds, 30 * 60)
            self.assertAlmostEqual(rep_before.quota.total_weekly_pct, 37.5, places=1)
            self.assertEqual(len(rep_before.dialogs), 2)

            # Delete d1_sub completely, and delete d1 from summaries + brain while leaving d1.db lingering on disk
            (conv_dir / "d1_sub.db").unlink()
            shutil.rmtree(root / "brain" / "d1_sub")
            shutil.rmtree(root / "brain" / "d1")
            sconn = sqlite3.connect(root / "conversation_summaries.db")
            sconn.execute("DELETE FROM conversation_summaries WHERE conversation_id IN ('d1', 'd1_sub')")
            sconn.commit()
            sconn.close()

            # Even with d1.db temporarily lingering on disk, deleted d1 must not leak into active dialogs
            _LEDGER_CACHE.clear()
            rep_linger = compute_day_report(
                target_date=date(2026, 11, 17), antigravity_dir=root, now=now_bj, quota_config=qcfg, ledger_path=ledger_db,
            )
            self.assertEqual([d.conversation_id for d in rep_linger.dialogs], ["d2"])

            # Now unlink d1.db completely and simulate cold process restart
            (conv_dir / "d1.db").unlink()
            _LEDGER_CACHE.clear()

            rep_after = compute_day_report(
                target_date=date(2026, 11, 17), antigravity_dir=root, now=now_bj, quota_config=qcfg, ledger_path=ledger_db,
            )
            self.assertEqual(rep_after.total_seconds, 30 * 60)
            self.assertEqual(rep_after.running_count, 0)
            self.assertAlmostEqual(rep_after.quota.total_weekly_pct, 37.5, places=1)
            self.assertAlmostEqual(rep_after.quota.cycle_used_pct, 37.5, places=1)
            self.assertAlmostEqual(rep_after.quota.by_workspace["Alpha"].total_weekly_pct, 30.0, places=1)
            self.assertEqual([d.conversation_id for d in rep_after.dialogs], ["d2"])

            wk_after = compute_week_report(
                target_date=date(2026, 11, 17), antigravity_dir=root, now=now_bj, quota_config=qcfg, ledger_path=ledger_db,
            )
            self.assertEqual(wk_after.total_seconds, 30 * 60)
            self.assertAlmostEqual(wk_after.quota.total_weekly_pct, 37.5, places=1)
            anc = resolve_current_cycle_anchor_start(qcfg, now_bj, antigravity_dir=root, ledger_path=ledger_db)
            self.assertEqual(anc, datetime.fromtimestamp(t1, tz=LOCAL_TZ).isoformat(timespec="seconds"))

            # Simulate mid-day official global reset right before a new post-reset event at t2 + 120s
            t_reset = t2 + 60
            t_post = t2 + 120
            cc2 = sqlite3.connect(conv_db := (conv_dir / "d2.db"))
            cc2.execute("INSERT INTO steps VALUES (?, ?, ?)", (1, 15, make_step_meta(t_post, 1525, 0, 1_000_000, 0)))
            cc2.commit()
            cc2.close()
            reset_cfg = QuotaConfig(
                carbon_multiplier=3.0,
                subscription_start="",
                weekly_cap_usd_override=100.0,
                rolling_reset_start=datetime.fromtimestamp(t_reset, tz=LOCAL_TZ).isoformat(timespec="seconds"),
            )
            rep_reset = compute_day_report(
                target_date=date(2026, 11, 17), antigravity_dir=root, now=datetime.fromtimestamp(t_post + 60, tz=LOCAL_TZ),
                quota_config=reset_cfg, ledger_path=ledger_db,
            )
            self.assertAlmostEqual(rep_reset.quota.total_weekly_pct, 45.0, places=1)
            self.assertAlmostEqual(rep_reset.quota.by_workspace["Alpha"].total_weekly_pct, 30.0, places=1)
            self.assertAlmostEqual(rep_reset.quota.cycle_used_pct, 7.5, places=1)
            self.assertAlmostEqual(rep_reset.quota.cycle_left_pct, 92.5, places=1)
            self.assertAlmostEqual(rep_reset.quota.five_hour_used_pct, round(7.5 / (100.0 * FIVE_HOUR_TO_WEEKLY_RATIO) * 100.0, 1), places=1)
            self.assertEqual(
                resolve_current_cycle_anchor_start(reset_cfg, datetime.fromtimestamp(t_post + 60, tz=LOCAL_TZ), antigravity_dir=root, ledger_path=ledger_db),
                datetime.fromtimestamp(t_post, tz=LOCAL_TZ).isoformat(timespec="seconds"),
            )
