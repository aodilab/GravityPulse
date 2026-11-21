"""Unit tests for implicit KV cache status, cold-cache breakeven, subagent/bg-task detection, and alerts."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from config.settings import LOCAL_TZ
from src.core import (
    DialogContextInfo,
    SwitchAlertTracker,
    classify_switch_kind,
    compute_day_report,
    dialog_safe_context_limit,
    evaluate_cold_cache_breakeven,
    evaluate_dialog_advice,
    evaluate_dialog_advice_detail,
    evaluate_implicit_cache_status,
    learn_empirical_cache_thresholds,
    report_to_dict,
    serialize_dialogs,
)
from tests.helpers import f_b, f_v, init_summary_db, local_utc_sql, local_utc_z, make_step_meta


class TestAdvisorAndAlerts(unittest.TestCase):
    def test_implicit_cache_status_transitions(self) -> None:
        now = datetime(2026, 11, 15, 16, 0, 0, tzinfo=LOCAL_TZ)

        def make_dlg(
            ctx_in: int, cache_in: int, mins_ago: int, running: bool = False, compactions: int = 0, model_label: str = "Hydrogen",
        ) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="c", title="t", workspace="GravityPulse", model_label=model_label,
                is_running=running, input_tokens=ctx_in, cache_read_tokens=cache_in,
                output_tokens=500, thinking_tokens=100, last_active=now - timedelta(minutes=mins_ago),
                compaction_count=compactions,
            )

        self.assertEqual(evaluate_implicit_cache_status(make_dlg(2000, 0, 1), now)[:2], ("none", "未达4K门槛"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 20, running=True), now)[:3], ("active", "保活中", "运行中"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 4), now)[:3], ("fresh", "缓存有效", "4分钟前"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 9), now)[:3], ("cooling", "缓存衰减", "9分钟前"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 17), now)[:3], ("decay", "可能失效", "17分钟前"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 21), now)[:3], ("expired", "基本失效", "21分钟前"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 90), now)[:3], ("expired", "基本失效", "1.5小时前"))

        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 2, model_label="Carbon"), now)[:2], ("fresh", "缓存有效"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 4, model_label="Carbon"), now)[:2], ("cooling", "缓存衰减"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 6, model_label="Carbon"), now)[:2], ("decay", "可能失效"))
        self.assertEqual(evaluate_implicit_cache_status(make_dlg(5000, 50000, 9, model_label="Carbon"), now)[:2], ("expired", "基本失效"))
        self.assertAlmostEqual(make_dlg(5000, 50000, 3).cache_hit_pct, 90.9, places=1)

        self.assertEqual(dialog_safe_context_limit(make_dlg(3000, 178000, 1, model_label="Carbon")), 256_000)
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 178000, 1, compactions=0, model_label="Carbon"), "fresh"), ("ok", "状态健康"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(206000, 0, 0, running=True, compactions=1, model_label="Carbon"), "active"), ("ok", "状态健康"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 217000, 9, compactions=1, model_label="Carbon"), "cooling"), ("watch", "临近压缩"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(2600, 152000, 9, compactions=0, model_label="Carbon"), "expired"), ("switch", "建议换新"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 60000, 2, compactions=3, model_label="Carbon"), "fresh"), ("watch", "注意控制"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 60000, 2, compactions=4, model_label="Carbon"), "fresh"), ("watch", "注意控制"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 60000, 2, compactions=5, model_label="Carbon"), "fresh"), ("switch", "建议换新"))

        self.assertEqual(dialog_safe_context_limit(make_dlg(3000, 80000, 2, compactions=0, model_label="Hydrogen")), 256_000)
        self.assertEqual(dialog_safe_context_limit(make_dlg(3000, 80000, 2, compactions=1, model_label="Hydrogen")), 256_000)
        # 3-turn 207K zero-compaction Hydrogen session (real case `显示效果优化`) stays healthy without false alarm
        self.assertEqual(evaluate_dialog_advice(make_dlg(2543, 204467, 1, compactions=0, model_label="Hydrogen"), "fresh"), ("ok", "状态健康"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 196000, 2, compactions=2, model_label="Hydrogen"), "fresh"), ("ok", "状态健康"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 225000, 2, compactions=0, model_label="Hydrogen"), "fresh"), ("watch", "临近压缩"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 120000, 1, compactions=3, model_label="Hydrogen"), "fresh"), ("watch", "注意控制"))
        h_pre4 = evaluate_dialog_advice_detail(make_dlg(3000, 225000, 1, compactions=3, model_label="Hydrogen"), "fresh")
        self.assertEqual(h_pre4[:2], ("watch", "临近压缩"))
        self.assertIn("第4次压缩", h_pre4[2])
        h_adv = evaluate_dialog_advice_detail(make_dlg(3000, 50000, 1, compactions=4, model_label="Hydrogen"), "fresh")
        self.assertEqual(h_adv[:2], ("switch", "建议换新"))
        self.assertIn("4 次", h_adv[3])
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 270000, 1, compactions=0, model_label="Hydrogen"), "fresh"), ("switch", "建议换新"))
        self.assertEqual(evaluate_dialog_advice(make_dlg(3000, 155000, 70, compactions=0, model_label="Hydrogen"), "expired"), ("switch", "建议换新"))

    def test_empirical_cache_threshold_learning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now_bj = datetime(2026, 11, 16, 0, 50, tzinfo=LOCAL_TZ)
            base_th = learn_empirical_cache_thresholds(root, now_bj)
            self.assertEqual(base_th["Carbon"], (180, 300, 480))
            self.assertEqual(base_th["Hydrogen"], (300, 900, 1200))

            conv_db = root / "conversations" / "learn1.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            t_cur = int((now_bj - timedelta(hours=6)).timestamp())
            rows = [(0, 15, make_step_meta(t_cur, 1437, 50_000, 500, 0))]
            for i in range(1, 16):
                t_cur += 240 if i % 2 == 1 else 480
                rows.append((i, 15, make_step_meta(t_cur, 1437, 2_000, 500, 50_000)))
            cc.executemany("INSERT INTO steps VALUES (?, ?, ?)", rows)
            cc.commit()
            cc.close()

            adapted_th = learn_empirical_cache_thresholds(root, now_bj)
            self.assertGreaterEqual(adapted_th["Carbon"][0], 600)

    def test_decay_no_premature_switch_and_other_model_label(self) -> None:
        now = datetime(2026, 11, 16, 1, 30, tzinfo=LOCAL_TZ)
        c_decay = DialogContextInfo(
            conversation_id="cd1", title="架构重整", workspace="demo_web", model_label="Carbon",
            is_running=False, input_tokens=3000, cache_read_tokens=202_000,
            output_tokens=1000, thinking_tokens=300, last_active=now - timedelta(minutes=8), compaction_count=1,
        )
        self.assertEqual(evaluate_dialog_advice(c_decay, "decay"), ("watch", "注意控制"))
        self.assertEqual(evaluate_dialog_advice(c_decay, "expired"), ("switch", "建议换新"))

        c_pre5 = DialogContextInfo(
            conversation_id="cd2", title="收尾检查", workspace="demo_web", model_label="Carbon",
            is_running=False, input_tokens=3000, cache_read_tokens=232_000,
            output_tokens=1000, thinking_tokens=300, last_active=now - timedelta(minutes=1), compaction_count=4,
        )
        adv_pre5 = evaluate_dialog_advice_detail(c_pre5, "fresh")
        self.assertEqual(adv_pre5[:2], ("watch", "临近压缩"))
        self.assertIn("第5次压缩", adv_pre5[2])

        h_decay = DialogContextInfo(
            conversation_id="hd1", title="视频剪辑", workspace="demo_web", model_label="Hydrogen",
            is_running=False, input_tokens=3000, cache_read_tokens=162_000,
            output_tokens=1000, thinking_tokens=400, last_active=now - timedelta(minutes=12), compaction_count=0,
        )
        self.assertEqual(evaluate_dialog_advice(h_decay, "decay"), ("watch", "注意控制"))
        self.assertEqual(evaluate_dialog_advice(h_decay, "expired"), ("switch", "建议换新"))

        o_switch = DialogContextInfo(
            conversation_id="od1", title="轻量测试", workspace="demo_app", model_label="其他",
            is_running=False, input_tokens=3000, cache_read_tokens=170_000,
            output_tokens=500, thinking_tokens=0, last_active=now - timedelta(minutes=1), compaction_count=4,
        )
        adv_other = evaluate_dialog_advice_detail(o_switch, "fresh")
        self.assertEqual(adv_other[0], "switch")
        self.assertNotIn("Hydrogen", adv_other[3])

    def test_switch_alert_two_tier_50k_realert_and_cycle_resets(self) -> None:
        def make_dlg(
            cid: str, title: str, *, advice_key: str = "switch", switch_kind: str = "structural",
            is_running: bool = True, cc: int = 0, ctx: int = 340_000, cache_state: str = "active",
            idle_seconds: int = 0, user_turn_count: int = 5, post_finish_turn_count: int = 4,
            last_user_input_ts: float = 1000.0, last_active_ts: float = 1010.0,
            notify_detail: str | None = None,
        ) -> dict:
            ctx_k = f"{round(ctx / 1000)}K" if ctx >= 100_000 else f"{ctx / 1000:.1f}K"
            default_detail = (
                f"Carbon 已累计压缩 {cc} 次，可暂停并新建对话框继续。"
                if cc > 0
                else f"Carbon 单步上下文已达 {ctx_k}（跨入高耗重算区），可暂停并新建对话框继续。"
            )
            return {
                "conversation_id": cid, "workspace": "demo_app", "title": title, "model_label": "Carbon",
                "advice_key": advice_key, "switch_kind": switch_kind if advice_key == "switch" else "",
                "is_running": is_running, "compaction_count": cc, "context_tokens": ctx,
                "cache_state": cache_state, "idle_seconds": idle_seconds,
                "user_turn_count": user_turn_count, "post_finish_turn_count": post_finish_turn_count,
                "last_user_input_ts": last_user_input_ts, "last_active_ts": last_active_ts,
                "notify_detail": notify_detail or default_detail,
            }

        tracker = SwitchAlertTracker()
        # 1) Seeded already-over-limit dialog counts as alert #1: silent < +50K, fires #2 at >= +50K, then caps at 2
        self.assertIsNone(tracker.process([make_dlg("seed_run", "存量运行", is_running=True, cc=5, ctx=120_000)]))
        self.assertIsNone(tracker.process([make_dlg("seed_run", "存量运行", is_running=True, cc=5, ctx=169_000)]))
        seed_alert2 = tracker.process([make_dlg("seed_run", "存量运行", is_running=True, cc=5, ctx=170_000)])
        self.assertIsNotNone(seed_alert2)
        self.assertIn("存量运行", seed_alert2[0])
        self.assertIn("二次提醒", seed_alert2[1])
        self.assertIn("+50.0K", seed_alert2[1])
        self.assertIn("170K", seed_alert2[1])
        self.assertIsNone(tracker.process([make_dlg("seed_run", "存量运行", is_running=True, cc=5, ctx=230_000)]))
        self.assertIsNone(tracker.process([make_dlg("seed_run", "存量运行", is_running=False, cc=5, ctx=230_000, cache_state="fresh", idle_seconds=5)]))
        seed_cc_alert = tracker.process([make_dlg("seed_run", "存量运行", is_running=True, cc=6, ctx=110_000)])
        self.assertIsNotNone(seed_cc_alert)
        self.assertNotIn("二次提醒", seed_cc_alert[1])

        # 2) Mid-run overflow: alert #1 on cross, silent < +50K, alert #2 at +50K, silent thereafter in same turn
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", advice_key="ok", is_running=True, cc=0, ctx=240_000)]))
        alert1 = tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=0, ctx=340_000, user_turn_count=5, post_finish_turn_count=4)])
        self.assertIsNotNone(alert1)
        self.assertIn("上线排查5", alert1[0])
        self.assertNotIn("二次提醒", alert1[1])
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=0, ctx=389_000, user_turn_count=5, post_finish_turn_count=4)]))
        alert2 = tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=0, ctx=395_000, user_turn_count=5, post_finish_turn_count=4)])
        self.assertIsNotNone(alert2)
        self.assertIn("二次提醒", alert2[1])
        self.assertIn("+55.0K", alert2[1])
        self.assertNotIn("当前 395K", alert2[1])  # already present in base detail
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=0, ctx=460_000, user_turn_count=5, post_finish_turn_count=4)]))
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=False, cc=0, ctx=470_000, cache_state="fresh", idle_seconds=8, user_turn_count=5, post_finish_turn_count=4)]))
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=False, cc=0, ctx=470_000, cache_state="expired", idle_seconds=2000, user_turn_count=5, post_finish_turn_count=4)]))

        # 3) New compaction mid-run resets round: alert #1 on compaction + alert #2 on +50K growth, then capped
        alert_cc1 = tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=1, ctx=180_000, user_turn_count=5, post_finish_turn_count=4)])
        self.assertIsNotNone(alert_cc1)
        self.assertNotIn("二次提醒", alert_cc1[1])
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=1, ctx=220_000, user_turn_count=5, post_finish_turn_count=4)]))
        alert_cc2 = tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=1, ctx=232_000, user_turn_count=5, post_finish_turn_count=4)])
        self.assertIsNotNone(alert_cc2)
        self.assertIn("二次提醒", alert_cc2[1])
        self.assertIn("+52.0K", alert_cc2[1])
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=1, ctx=295_000, user_turn_count=5, post_finish_turn_count=4)]))

        # 4) New user turn resets round: alert #1 on turn start + alert #2 at +50K, then capped
        alert_t1 = tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=1, ctx=240_000, user_turn_count=6, post_finish_turn_count=5, last_user_input_ts=3100.0)])
        self.assertIsNotNone(alert_t1)
        self.assertNotIn("二次提醒", alert_t1[1])
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=1, ctx=280_000, user_turn_count=6, post_finish_turn_count=5, last_user_input_ts=3100.0)]))
        alert_t2 = tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=1, ctx=290_000, user_turn_count=6, post_finish_turn_count=5, last_user_input_ts=3100.0)])
        self.assertIsNotNone(alert_t2)
        self.assertIn("二次提醒", alert_t2[1])
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=True, cc=1, ctx=355_000, user_turn_count=6, post_finish_turn_count=5, last_user_input_ts=3100.0)]))
        self.assertIsNone(tracker.process([make_dlg("m5", "上线排查5", is_running=False, cc=1, ctx=360_000, cache_state="fresh", idle_seconds=6, user_turn_count=6, post_finish_turn_count=5, last_user_input_ts=3100.0)]))

        # 5) Cold-cache -> structural upgrade starts a fresh 2-alert cycle; stale idle structural skips #2
        up_cache = tracker.process([make_dlg("c_up", "升级测试", switch_kind="cache", is_running=False, cc=0, ctx=180_000, cache_state="expired", idle_seconds=600)])
        self.assertIsNotNone(up_cache)
        self.assertIsNone(tracker.process([make_dlg("c_up", "升级测试", switch_kind="cache", is_running=False, cc=0, ctx=240_000, cache_state="expired", idle_seconds=700)]))
        up_s1 = tracker.process([make_dlg("c_up", "升级测试", switch_kind="structural", is_running=True, cc=0, ctx=350_000)])
        self.assertIsNotNone(up_s1)
        self.assertNotIn("二次提醒", up_s1[1])
        self.assertIsNone(tracker.process([make_dlg("c_up", "升级测试", switch_kind="structural", is_running=False, cc=0, ctx=410_000, cache_state="expired", idle_seconds=700)]))
        up_s2 = tracker.process([make_dlg("c_up", "升级测试", switch_kind="structural", is_running=False, cc=0, ctx=410_000, cache_state="fresh", idle_seconds=10)])
        self.assertIsNotNone(up_s2)
        self.assertIn("二次提醒", up_s2[1])
        self.assertIsNone(tracker.process([make_dlg("c_up", "升级测试", switch_kind="structural", is_running=True, cc=0, ctx=470_000)]))

        # Verify advisor notify_detail mentions pausing when is_running=True
        now_bj = datetime(2026, 11, 20, 2, 15, 0, tzinfo=LOCAL_TZ)
        running_over = DialogContextInfo(
            conversation_id="run_over", title="复盘", workspace="demo_app", model_label="Carbon",
            is_running=True, input_tokens=5_000, cache_read_tokens=335_000,
            output_tokens=800, thinking_tokens=200, last_active=now_bj, compaction_count=0, context_limit=512_000,
        )
        adv_run = evaluate_dialog_advice_detail(running_over, "active", 0)
        self.assertEqual(adv_run[:2], ("switch", "建议换新"))
        self.assertIn("暂停并新建对话框", adv_run[3])

    def test_cold_cache_vs_new_window_breakeven_and_single_alert(self) -> None:
        now = datetime(2026, 11, 16, 3, 10, 0, tzinfo=LOCAL_TZ)

        def make_dlg(cid: str, model_label: str, ctx_total: int, idle_mins: int, cc: int = 0) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id=cid, title=f"窗口_{cid}", workspace="demo_app", model_label=model_label,
                is_running=False, input_tokens=3_000, cache_read_tokens=max(0, ctx_total - 3_000),
                output_tokens=800, thinking_tokens=200, last_active=now - timedelta(minutes=idle_mins), compaction_count=cc,
            )

        c_short = make_dlg("cs", "Carbon", 95_000, 22)
        be_c_short = evaluate_cold_cache_breakeven(c_short, "expired", 22 * 60)
        self.assertFalse(be_c_short["should_switch"])
        self.assertLess(be_c_short["ratio"], 0.85)
        adv_c_short = evaluate_dialog_advice_detail(c_short, "expired", 22 * 60)
        self.assertEqual(adv_c_short[:2], ("ok", "状态健康"))
        self.assertIn("续聊更省", adv_c_short[2])

        c_mid = make_dlg("cm", "Carbon", 120_000, 22)
        self.assertEqual(evaluate_dialog_advice_detail(c_mid, "expired", 22 * 60)[:2], ("watch", "注意控制"))

        c_long = make_dlg("cl", "Carbon", 180_000, 9)
        be_c_long = evaluate_cold_cache_breakeven(c_long, "expired", 9 * 60)
        self.assertTrue(be_c_long["should_switch"])
        self.assertGreater(be_c_long["ratio"], 1.5)
        adv_c_long = evaluate_dialog_advice_detail(c_long, "expired", 9 * 60)
        self.assertEqual(adv_c_long[:2], ("switch", "建议换新"))
        self.assertIn("x", adv_c_long[2])

        self.assertEqual(evaluate_dialog_advice(make_dlg("hs", "Hydrogen", 65_000, 25), "expired"), ("ok", "状态健康"))
        self.assertEqual(evaluate_dialog_advice(make_dlg("hm", "Hydrogen", 115_000, 25), "expired"), ("watch", "注意控制"))
        self.assertEqual(evaluate_dialog_advice(make_dlg("hl", "Hydrogen", 160_000, 25), "expired"), ("switch", "建议换新"))
        self.assertNotEqual(evaluate_dialog_advice(make_dlg("os", "其他", 90_000, 35), "expired")[0], "switch")
        self.assertEqual(evaluate_dialog_advice(make_dlg("ol", "其他", 155_000, 35), "expired"), ("switch", "建议换新"))

        tracker = SwitchAlertTracker()
        d_a_fresh = make_dlg("da", "Carbon", 180_000, 2)
        d_b_run = DialogContextInfo(
            conversation_id="db", title="并行运行窗口", workspace="demo_web", model_label="Carbon",
            is_running=True, input_tokens=3_000, cache_read_tokens=47_000,
            output_tokens=500, thinking_tokens=100, last_active=now,
        )
        d_e_short = make_dlg("de", "Carbon", 95_000, 2)
        self.assertIsNone(tracker.process(serialize_dialogs((d_b_run, d_a_fresh, d_e_short), "%H:%M:%S", now=now)))

        later_9m = now + timedelta(minutes=9)
        d_b_run_9m = DialogContextInfo(
            conversation_id="db", title="并行运行窗口", workspace="demo_web", model_label="Carbon",
            is_running=True, input_tokens=3_000, cache_read_tokens=65_000,
            output_tokens=500, thinking_tokens=100, last_active=later_9m,
        )
        alert_once = tracker.process(serialize_dialogs((d_b_run_9m, d_a_fresh, d_e_short), "%H:%M:%S", now=later_9m))
        self.assertIsNotNone(alert_once)
        self.assertIn("窗口_da", alert_once[0])

        for extra_m in (12, 20, 45):
            t_poll = now + timedelta(minutes=extra_m)
            self.assertIsNone(tracker.process(serialize_dialogs((d_b_run_9m, d_a_fresh, d_e_short), "%H:%M:%S", now=t_poll)))

    def test_running_subagents_and_bg_tasks_prevent_false_cold_cache_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("p7", "上线排查7", '["file:///c%3A/Work/demo_app"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 3, 2, 34), local_utc_sql(2026, 11, 16, 2, 40, 0)),
                    ("sub1", "", '["file:///c%3A/Work/demo_app"]', "CASCADE_RUN_STATUS_RUNNING", "p7", 1, local_utc_sql(2026, 11, 16, 3, 19, 0), None),
                    ("sub2", "", '["file:///c%3A/Work/demo_app"]', "CASCADE_RUN_STATUS_RUNNING", "p7", 1, local_utc_sql(2026, 11, 16, 3, 19, 10), None),
                    ("bg_conv", "自动化回归", '["file:///c%3A/Work/demo_web"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 3, 1, 0), local_utc_sql(2026, 11, 16, 3, 0, 0)),
                ],
            )
            t_parent = int(datetime(2026, 11, 16, 3, 2, 34, tzinfo=LOCAL_TZ).timestamp())
            for cid_name, ts_val in (("p7", t_parent), ("bg_conv", t_parent - 90)):
                conv_db = root / "conversations" / f"{cid_name}.db"
                conv_db.parent.mkdir(parents=True, exist_ok=True)
                cc = sqlite3.connect(conv_db)
                cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
                cc.executemany(
                    "INSERT INTO steps VALUES (?, ?, ?)",
                    [(0, 14, make_step_meta(ts_val - 60)), (1, 15, make_step_meta(ts_val, 1437, 5_000, 300, 190_000))],
                )
                cc.commit()
                cc.close()

            tp_p7 = root / "brain" / "p7" / ".system_generated" / "logs" / "transcript.jsonl"
            tp_p7.parent.mkdir(parents=True, exist_ok=True)
            tp_p7.write_text(f'{{"step_index":0,"type":"USER_INPUT","source":"USER_EXPLICIT","status":"DONE","created_at":"{local_utc_z(2026, 11, 16, 2, 40, 0)}"}}\n', encoding="utf-8")

            tp_bg = root / "brain" / "bg_conv" / ".system_generated" / "logs" / "transcript.jsonl"
            tp_bg.parent.mkdir(parents=True, exist_ok=True)
            bg_lines = [
                f'{{"step_index":0,"type":"USER_INPUT","source":"USER_EXPLICIT","status":"DONE","created_at":"{local_utc_z(2026, 11, 16, 3, 0, 0)}"}}',
                f'{{"step_index":1,"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","created_at":"{local_utc_z(2026, 11, 16, 3, 0, 10)}","tool_calls":[{{"name":"run_command","args":{{"CommandLine":"npm run dev","IsDaemon":true}}}}]}}',
                f'{{"step_index":2,"type":"GENERIC","source":"MODEL","status":"RUNNING","created_at":"{local_utc_z(2026, 11, 16, 3, 0, 11)}","content":"Tool is running as a background task with task id: bg_conv/task-2\\n"}}',
                f'{{"step_index":3,"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","created_at":"{local_utc_z(2026, 11, 16, 3, 0, 50)}","tool_calls":[{{"name":"run_command","args":{{"CommandLine":"node full_e2e.mjs"}}}}]}}',
                f'{{"step_index":4,"type":"GENERIC","source":"MODEL","status":"RUNNING","created_at":"{local_utc_z(2026, 11, 16, 3, 1, 0)}","content":"Tool is running as a background task with task id: bg_conv/task-4\\n"}}',
            ]
            tp_bg.write_text("\n".join(bg_lines) + "\n", encoding="utf-8")

            tracker = SwitchAlertTracker()
            t_early = datetime(2026, 11, 16, 3, 5, 0, tzinfo=LOCAL_TZ)
            rep_early = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=t_early)
            self.assertIsNone(tracker.process(report_to_dict(rep_early, now=t_early)["dialogs"]))

            now_17m = datetime(2026, 11, 16, 3, 20, 0, tzinfo=LOCAL_TZ)
            rep_17m = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_17m)
            self.assertEqual(rep_17m.running_count, 2)
            d_map = {d["conversation_id"]: d for d in report_to_dict(rep_17m, now=now_17m)["dialogs"]}
            self.assertTrue(d_map["p7"]["is_running"])
            self.assertEqual(d_map["p7"]["cache_state"], "active")
            self.assertEqual(d_map["p7"]["cache_status_text"], "子任务运行")
            self.assertIn("主缓已冷", d_map["p7"]["cache_idle_text"])
            self.assertEqual(d_map["p7"]["advice_key"], "ok")

            self.assertTrue(d_map["bg_conv"]["is_running"])
            self.assertEqual(d_map["bg_conv"]["advice_key"], "ok")
            self.assertIsNone(tracker.process(list(d_map.values())))

            bg_lines.append(
                f'{{"step_index":5,"type":"SYSTEM_MESSAGE","source":"SYSTEM","status":"DONE","created_at":"{local_utc_z(2026, 11, 16, 3, 20, 10)}","content":"<SYSTEM_MESSAGE>\\nTask id \\"bg_conv/task-4\\" finished with result:\\nOK\\n</SYSTEM_MESSAGE>"}}'
            )
            tp_bg.write_text("\n".join(bg_lines) + "\n", encoding="utf-8")

            now_22m = datetime(2026, 11, 16, 3, 25, 0, tzinfo=LOCAL_TZ)
            rep_22m = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_22m)
            self.assertEqual(rep_22m.running_count, 1)
            d_map_22 = {d["conversation_id"]: d for d in report_to_dict(rep_22m, now=now_22m)["dialogs"]}
            self.assertTrue(d_map_22["p7"]["is_running"])
            self.assertFalse(d_map_22["bg_conv"]["is_running"])
            self.assertEqual(d_map_22["bg_conv"]["advice_key"], "switch")
            alert = tracker.process(list(d_map_22.values()))
            self.assertIsNotNone(alert)
            self.assertIn("自动化回归", alert[0])

    def test_dynamic_512k_window_prevents_false_overflow_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [("c512", "上线排查10", '["file:///c%3A/Work/demo_app"]', "CASCADE_RUN_STATUS_RUNNING", "", 0, local_utc_sql(2026, 11, 16, 16, 13, 0), local_utc_sql(2026, 11, 16, 16, 0, 0))],
            )
            conv_db = root / "conversations" / "c512.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            cc.execute("CREATE TABLE gen_metadata (size INTEGER, data BLOB)")
            sub_meta = (
                f_v(3, 1525)
                + f_b(9, f_b(10, f_v(1, 300_000) + f_v(4, 512_000)))
                + f_b(19, b"gemini-4-hydrogen")
                + f_b(21, b"Gemini 4 Hydrogen (High)")
            )
            gm_blob = f_b(1, sub_meta)
            cc.execute("INSERT INTO gen_metadata VALUES (?, ?)", (len(gm_blob), gm_blob))
            t_u = int(datetime(2026, 11, 16, 16, 0, 0, tzinfo=LOCAL_TZ).timestamp())
            cc.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [
                    (0, 14, f_b(1, f_v(1, t_u))),
                    (1, 15, make_step_meta(t_u + 180, 1525, 8_000, 309, 292_000)),
                ],
            )
            cc.commit()
            cc.close()

            now_bj = datetime(2026, 11, 16, 16, 4, 0, tzinfo=LOCAL_TZ)
            rep = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_bj)
            dlgs = report_to_dict(rep, now=now_bj)["dialogs"]
            self.assertEqual(len(dlgs), 1)
            d0 = dlgs[0]
            self.assertEqual(d0["safe_limit"], 512_000)
            self.assertEqual(d0["safe_limit_fmt"], "512K")
            self.assertEqual(d0["safe_pct"], 59)
            self.assertEqual(d0["advice_key"], "ok")
            self.assertEqual(d0["advice_text"], "状态健康")

    def test_512k_adaptive_compaction_thresholds_and_breakeven_drag(self) -> None:
        now = datetime(2026, 11, 16, 17, 0, 0, tzinfo=LOCAL_TZ)

        def dlg_512(model_label: str, ctx_tok: int, cc: int, lim: int = 512_000) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="d512", title="512K测试", workspace="GravityPulse", model_label=model_label,
                is_running=False, input_tokens=3_000, cache_read_tokens=max(0, ctx_tok - 3_000),
                output_tokens=800, thinking_tokens=200, last_active=now - timedelta(minutes=1),
                compaction_count=cc, context_limit=lim,
            )

        # Carbon @ 512K: <260K cc=0 ok, 260K~329K cc=0 watch, >=330K or cc>=1 switch
        self.assertEqual(evaluate_dialog_advice(dlg_512("Carbon", 240_000, 0), "fresh"), ("ok", "状态健康"))
        c_w_ctx = evaluate_dialog_advice_detail(dlg_512("Carbon", 285_000, 0), "fresh")
        self.assertEqual(c_w_ctx[:2], ("watch", "注意控制"))
        self.assertNotIn("临近第1次压缩", c_w_ctx[2])
        c_sw_ctx = evaluate_dialog_advice_detail(dlg_512("Carbon", 340_000, 0), "fresh")
        self.assertEqual(c_sw_ctx[:2], ("switch", "建议换新"))
        self.assertEqual(classify_switch_kind(dlg_512("Carbon", 340_000, 0), "fresh"), "structural")
        c_sw_cc1 = evaluate_dialog_advice_detail(dlg_512("Carbon", 180_000, 1), "fresh")
        self.assertEqual(c_sw_cc1[:2], ("switch", "建议换新"))
        self.assertEqual(classify_switch_kind(dlg_512("Carbon", 180_000, 1), "fresh"), "structural")

        # Hydrogen @ 512K: <320K cc=0 ok, 320K~399K cc=0 watch, >=400K or cc>=1 switch
        self.assertEqual(evaluate_dialog_advice(dlg_512("Hydrogen", 300_000, 0), "fresh"), ("ok", "状态健康"))
        h_w_ctx = evaluate_dialog_advice_detail(dlg_512("Hydrogen", 350_000, 0), "fresh")
        self.assertEqual(h_w_ctx[:2], ("watch", "注意控制"))
        h_sw_ctx = evaluate_dialog_advice_detail(dlg_512("Hydrogen", 410_000, 0), "fresh")
        self.assertEqual(h_sw_ctx[:2], ("switch", "建议换新"))
        self.assertEqual(classify_switch_kind(dlg_512("Hydrogen", 410_000, 0), "fresh"), "structural")
        h_sw_cc1 = evaluate_dialog_advice_detail(dlg_512("Hydrogen", 120_000, 1), "fresh")
        self.assertEqual(h_sw_cc1[:2], ("switch", "建议换新"))
        self.assertEqual(classify_switch_kind(dlg_512("Hydrogen", 120_000, 1), "fresh"), "structural")

        # 512K cc=1 carries 2x effective compaction drag in cold-cache breakeven vs 256K cc=1
        be_256_cc1 = evaluate_cold_cache_breakeven(dlg_512("Carbon", 90_000, 1, lim=256_000), "expired", 1200)
        be_512_cc1 = evaluate_cold_cache_breakeven(dlg_512("Carbon", 90_000, 1, lim=512_000), "expired", 1200)
        self.assertGreater(float(be_512_cc1["continue_extra_usd"]), float(be_256_cc1["continue_extra_usd"]))

        # Future 900K tier inverse scaling check
        self.assertEqual(evaluate_dialog_advice(dlg_512("Carbon", 120_000, 2, lim=900_000), "fresh"), ("switch", "建议换新"))
        self.assertEqual(evaluate_dialog_advice(dlg_512("Hydrogen", 120_000, 1, lim=900_000), "fresh"), ("switch", "建议换新"))
