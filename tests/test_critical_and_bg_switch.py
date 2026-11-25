"""Regression tests for background-build pause checkpoints, canceled bg tasks, and imminent-compaction critical alerts."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from config.settings import COMPACTION_512K_TOKENS, LOCAL_TZ
from src.core import (
    DialogContextInfo,
    ProjectContextProfile,
    SwitchAlertTracker,
    classify_switch_kind,
    detect_dialog_work_phase,
    evaluate_dialog_advice_detail,
    learn_project_context_profiles,
    serialize_dialogs,
)
from src.core.storage import load_db_metadata, parse_transcript_details
from tests.helpers import f_b, f_v, init_summary_db, make_step_meta


def _write_transcript(root: Path, cid: str, entries: list[dict]) -> Path:
    tp = root / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
    return tp


class TestCriticalAndBgSwitch(unittest.TestCase):
    def test_canceled_background_task_clears_pending_subtasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cid = "bg_cancel"
            tp = _write_transcript(root, cid, [
                {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-22T19:20:00Z", "content": "构建发布包"},
                {"type": "PLANNER_RESPONSE", "created_at": "2026-11-22T19:20:34Z", "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"npm run build:release"'}}]},
                {"type": "GENERIC", "status": "RUNNING", "created_at": "2026-11-22T19:21:28Z", "content": "Tool is running as a background task with task id: bg_cancel/task-116\n"},
            ])
            _, _, pending_before = parse_transcript_details(tp)
            self.assertIn("bg_cancel/task-116", pending_before)

            with tp.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "status": "DONE",
                    "created_at": "2026-11-22T19:22:33Z",
                    "content": 'Task id "bg_cancel/task-116" was canceled with result:\nTool execution was canceled',
                }, ensure_ascii=False) + "\n")
            _, _, pending_after = parse_transcript_details(tp)
            self.assertEqual(pending_after, {})

    def test_verifying_above_idle_switch_and_waiting_bg_pause_node(self) -> None:
        now = datetime(2026, 11, 23, 3, 21, 50, tzinfo=LOCAL_TZ)
        app_prof = ProjectContextProfile(
            sample_count=26, working_ctx_tokens=64_814, new_window_cost_usd=0.435,
            turn_span_tokens=194_551, zone_start_tokens=240_000, idle_switch_tokens=300_000,
            run_switch_tokens=350_000, idle_zone_tokens=170_000, turn_growth_tokens=54_977,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_transcript(root, "m20", [
                {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-22T18:57:58Z", "content": "继续排查"},
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "edit_file", "args": {"Path": '"x:\\\\demo_app\\\\a.js"'}}]},
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"npm run build:release"'}}]},
            ])
            wp_ver = detect_dialog_work_phase(root, "m20", is_running=True, self_running=True, active_subtasks=0)
            self.assertEqual(wp_ver.phase, "verifying")
            wp_bg = detect_dialog_work_phase(root, "m20", is_running=True, self_running=False, active_subtasks=1)
            self.assertEqual(wp_bg.phase, "waiting_bg")

        def make_m20(ctx: int, phase: str, self_running: bool = True, subtasks: int = 0) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="m20", title="渲染修复", workspace="demo_app", model_label="Carbon",
                is_running=True, self_running=self_running, active_subtasks=subtasks,
                input_tokens=5_000, cache_read_tokens=ctx - 5_000, output_tokens=300, thinking_tokens=100,
                last_active=now, compaction_count=0, context_limit=COMPACTION_512K_TOKENS,
                user_turn_count=1, post_finish_turn_count=0, last_user_input_ts=1000.0,
                work_phase=phase, project_profile=app_prof,
            )

        d_early_bg = make_m20(160_000, "waiting_bg", self_running=False, subtasks=1)
        self.assertEqual(evaluate_dialog_advice_detail(d_early_bg, "active", 0)[:2], ("ok", "状态健康"))
        self.assertEqual(classify_switch_kind(d_early_bg, "active", 0), "")

        d_ver = make_m20(337_000, "verifying", self_running=True, subtasks=0)
        self.assertEqual(classify_switch_kind(d_ver, "active", 0), "zone_armed")
        self.assertEqual(evaluate_dialog_advice_detail(d_ver, "active", 0)[:2], ("watch", "停顿换新"))

        tracker = SwitchAlertTracker()
        self.assertIsNone(tracker.process(serialize_dialogs((make_m20(268_000, "exploring"),), "%H:%M:%S", now=now)))
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_m20(335_000, "editing"),), "%H:%M:%S", now=now)), [])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((d_ver,), "%H:%M:%S", now=now)), [])

        d_bg = make_m20(345_000, "waiting_bg", self_running=False, subtasks=1)
        a_bg = tracker.process_batch_with_kind(serialize_dialogs((d_bg,), "%H:%M:%S", now=now))
        self.assertEqual(len(a_bg), 1)
        self.assertTrue(a_bg[0][2])
        self.assertIn("后台", a_bg[0][1])
        self.assertIn("交接", a_bg[0][1])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((d_bg,), "%H:%M:%S", now=now)), [])

    def test_imminent_compaction_critical_overrides_no_hard_stop_and_escalates_alert(self) -> None:
        now = datetime(2026, 11, 23, 3, 23, 48, tzinfo=LOCAL_TZ)
        web_prof = ProjectContextProfile(
            sample_count=23, working_ctx_tokens=54_542, new_window_cost_usd=0.396,
            turn_span_tokens=140_833, zone_start_tokens=250_000, idle_switch_tokens=320_000,
            run_switch_tokens=370_000, idle_zone_tokens=190_000, turn_growth_tokens=72_900,
        )

        def make_w15(ctx: int, phase: str = "editing", running: bool = True, sec_ago: int = 0) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="w15", title="核心排查", workspace="demo_web", model_label="Carbon",
                is_running=running, self_running=running, active_subtasks=0,
                input_tokens=ctx, cache_read_tokens=0, output_tokens=11_500, thinking_tokens=4_800,
                last_active=now - timedelta(seconds=sec_ago), compaction_count=0,
                context_limit=COMPACTION_512K_TOKENS, user_turn_count=1, post_finish_turn_count=0,
                last_user_input_ts=1000.0, work_phase=phase, project_profile=web_prof,
            )

        d_370 = make_w15(370_354, "exploring")
        self.assertEqual(classify_switch_kind(d_370, "active", 0), "structural")
        self.assertIn("勿硬停", evaluate_dialog_advice_detail(d_370, "active", 0)[2])

        d_431 = make_w15(430_906, "editing")
        self.assertEqual(classify_switch_kind(d_431, "active", 0), "critical")
        adv_431 = evaluate_dialog_advice_detail(d_431, "active", 0)
        self.assertEqual(adv_431[:2], ("switch", "建议换新"))
        self.assertNotIn("勿硬停", adv_431[2])
        self.assertNotIn("勿硬停", adv_431[3])
        self.assertIn("压缩", adv_431[2])
        self.assertIn("84%", adv_431[3])

        d_455 = make_w15(455_000, "editing")
        adv_455 = evaluate_dialog_advice_detail(d_455, "active", 0)
        self.assertIn("即将压缩", adv_455[2])

        tracker = SwitchAlertTracker()
        self.assertIsNone(tracker.process(serialize_dialogs((make_w15(330_000, "exploring"),), "%H:%M:%S", now=now)))
        a1 = tracker.process_batch_with_kind(serialize_dialogs((d_370,), "%H:%M:%S", now=now))
        self.assertEqual(len(a1), 1)
        a_crit = tracker.process_batch_with_kind(serialize_dialogs((d_431,), "%H:%M:%S", now=now))
        self.assertEqual(len(a_crit), 1)
        self.assertNotIn("勿硬停", a_crit[0][1])
        self.assertIn("压缩", a_crit[0][1])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_w15(445_000, "editing"),), "%H:%M:%S", now=now)), [])
        a_crit2 = tracker.process_batch_with_kind(serialize_dialogs((make_w15(462_000, "editing"),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_crit2), 1)
        self.assertIn("二次提醒", a_crit2[0][1])
        a_stop = tracker.process_batch_with_kind(serialize_dialogs((make_w15(465_000, "idle", running=False, sec_ago=10),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_stop), 1)
        self.assertIn("临近压缩", a_stop[0][1])

    def test_short_followup_stays_silent_and_seeded_running_critical_still_alerts(self) -> None:
        now = datetime(2026, 11, 23, 4, 40, 0, tzinfo=LOCAL_TZ)

        def make_dlg(
            cid: str, ctx: int, running: bool, phase: str, turns: int = 1, user_ts: float = 1000.0, sec_ago: int = 0,
        ) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id=cid, title=f"测试_{cid}", workspace="GravityPulse", model_label="Carbon",
                is_running=running, self_running=running, input_tokens=5_000, cache_read_tokens=ctx - 5_000,
                output_tokens=600, thinking_tokens=100, last_active=now - timedelta(seconds=sec_ago),
                compaction_count=0, context_limit=COMPACTION_512K_TOKENS,
                user_turn_count=turns, post_finish_turn_count=turns, last_user_input_ts=user_ts,
                work_phase=phase,
            )

        tracker = SwitchAlertTracker()
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("crit_run", 425_000, True, "editing"),), "%H:%M:%S", now=now)), [])
        a_458 = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("crit_run", 458_000, True, "editing"),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_458), 1)
        self.assertNotIn("二次提醒", a_458[0][1])
        self.assertIn("压缩", a_458[0][1])
        a_490 = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("crit_run", 490_000, True, "editing"),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_490), 1)
        self.assertIn("二次提醒", a_490[0][1])

        a_stop1 = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("q1", 280_000, False, "idle", turns=1, user_ts=1000.0, sec_ago=10),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_stop1), 1)
        self.assertTrue(a_stop1[0][2])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("q1", 288_000, True, "exploring", turns=2, user_ts=2000.0, sec_ago=0),), "%H:%M:%S", now=now)), [])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("q1", 292_000, False, "idle", turns=2, user_ts=2000.0, sec_ago=5),), "%H:%M:%S", now=now)), [])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("q1", 320_000, True, "editing", turns=3, user_ts=3000.0, sec_ago=0),), "%H:%M:%S", now=now)), [])
        a_heavy_pause = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("q1", 320_000, False, "idle", turns=3, user_ts=3000.0, sec_ago=5),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_heavy_pause), 1)
        self.assertTrue(a_heavy_pause[0][2])
        a_step2 = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("q1", 405_000, True, "editing", turns=4, user_ts=4000.0, sec_ago=0),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_step2), 1)
        self.assertFalse(a_step2[0][2])
        self.assertIn("压缩", a_step2[0][1])

    def test_paused_zone_short_toast_dedup_and_per_dialog_turn_sampling(self) -> None:
        now = datetime(2026, 11, 23, 21, 20, 0, tzinfo=LOCAL_TZ)
        web_prof = ProjectContextProfile(
            sample_count=10, working_ctx_tokens=145_000, new_window_cost_usd=1.17,
            turn_span_tokens=260_000, zone_start_tokens=270_000, idle_switch_tokens=330_000,
            run_switch_tokens=400_000, idle_zone_tokens=270_000, turn_growth_tokens=150_000,
        )

        def make_wz(ctx: int, running: bool, phase: str, sec_ago: int = 10, turns: int = 1, user_ts: float = 1000.0) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="wz41", title="性能压测", workspace="demo_web", model_label="Carbon",
                is_running=running, self_running=running, input_tokens=3_000, cache_read_tokens=ctx - 3_000,
                output_tokens=1_000, thinking_tokens=200, last_active=now - timedelta(seconds=sec_ago),
                compaction_count=0, context_limit=COMPACTION_512K_TOKENS,
                user_turn_count=turns, post_finish_turn_count=turns, last_user_input_ts=user_ts,
                work_phase=phase, project_profile=web_prof,
            )

        tracker = SwitchAlertTracker()
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_wz(285_000, True, "editing", sec_ago=0),), "%H:%M:%S", now=now)), [])
        a_zone = tracker.process_batch_with_kind(serialize_dialogs((make_wz(293_000, False, "idle", sec_ago=10),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_zone), 1)
        self.assertTrue(a_zone[0][2])
        self.assertEqual(a_zone[0][1], "293K · 长任务换新，短任务可续")
        self.assertLessEqual(len(a_zone[0][1]), 20)
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_wz(310_000, True, "exploring", sec_ago=0, turns=2, user_ts=2000.0),), "%H:%M:%S", now=now)), [])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_wz(315_000, False, "idle", sec_ago=5, turns=2, user_ts=2000.0),), "%H:%M:%S", now=now)), [])
        a_cold = tracker.process_batch_with_kind(serialize_dialogs((make_wz(315_000, False, "idle", sec_ago=9 * 60, turns=2, user_ts=2000.0),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_cold), 1)
        self.assertTrue(a_cold[0][2])
        self.assertIn("缓存已冷", a_cold[0][1])

        a_wu_zone = tracker.process_batch_with_kind(serialize_dialogs((DialogContextInfo(
            conversation_id="wz42", title="性能压测2", workspace="demo_web", model_label="Carbon",
            is_running=True, self_running=True, input_tokens=3_000, cache_read_tokens=290_000,
            output_tokens=500, thinking_tokens=100, last_active=now, compaction_count=0,
            context_limit=COMPACTION_512K_TOKENS, work_phase="waiting_user", project_profile=web_prof,
        ),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_wu_zone), 1)
        self.assertTrue(a_wu_zone[0][2])
        self.assertIn("待确认", a_wu_zone[0][1])
        self.assertIn("短任务可续", a_wu_zone[0][1])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True)
            rows = []
            for idx in range(9):
                cid = f"heavy_{idx}"
                t0 = now - timedelta(hours=12 - idx)
                rows.append((cid, f"重任务{idx}", '["file:///x%3A/SampleApp"]', "CASCADE_RUN_STATUS_IDLE", "", 0, t0.isoformat(), t0.isoformat()))
                conn = sqlite3.connect(conv_dir / f"{cid}.db")
                conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
                ts0 = int(t0.timestamp())
                conn.executemany(
                    "INSERT INTO steps VALUES (?, ?, ?)",
                    [
                        (0, 14, f_b(1, f_v(1, ts0))),
                        (1, 15, make_step_meta(ts0 + 10, 1437, 25_000, 1000, 0)),
                        (2, 15, make_step_meta(ts0 + 30, 1437, 60_000, 1000, 25_000)),
                        (3, 15, make_step_meta(ts0 + 60, 1437, 100_000, 1000, 85_000)),
                    ],
                )
                conn.commit()
                conn.close()
            cid_chat = "chatty_9"
            t_chat = now - timedelta(minutes=20)
            rows.append((cid_chat, "碎聊", '["file:///x%3A/SampleApp"]', "CASCADE_RUN_STATUS_IDLE", "", 0, t_chat.isoformat(), t_chat.isoformat()))
            conn = sqlite3.connect(conv_dir / f"{cid_chat}.db")
            conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            ts_c = int(t_chat.timestamp())
            step_rows = []
            s_idx = 0
            cur_ctx = 25_000
            for t_i in range(10):
                step_rows.append((s_idx, 14, f_b(1, f_v(1, ts_c + t_i * 60))))
                s_idx += 1
                step_rows.append((s_idx, 15, make_step_meta(ts_c + t_i * 60 + 5, 1437, 2_000, 300, cur_ctx)))
                s_idx += 1
                cur_ctx += 16_000
                step_rows.append((s_idx, 15, make_step_meta(ts_c + t_i * 60 + 15, 1437, 16_000, 300, cur_ctx - 16_000)))
                s_idx += 1
            conn.executemany("INSERT INTO steps VALUES (?, ?, ?)", step_rows)
            conn.commit()
            conn.close()
            init_summary_db(root, rows)
            profs = learn_project_context_profiles(root, load_db_metadata(root / "conversation_summaries.db"), now)
            p_sample = profs.get(("SampleApp", "Carbon"))
            self.assertIsNotNone(p_sample)
            assert p_sample is not None
            self.assertGreaterEqual(p_sample.turn_growth_tokens, 110_000)
            self.assertGreaterEqual(p_sample.idle_switch_tokens - p_sample.zone_start_tokens, 50_000)


if __name__ == "__main__":
    unittest.main()
