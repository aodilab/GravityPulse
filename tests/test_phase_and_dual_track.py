"""Unit tests for live execution phase detection and dual-track (running-relaxed vs idle-early) switch alerts."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from config.settings import COMPACTION_512K_TOKENS, LOCAL_TZ
from src.core import (
    DialogContextInfo,
    SwitchAlertTracker,
    classify_switch_kind,
    compute_day_report,
    detect_dialog_work_phase,
    evaluate_cold_cache_breakeven,
    evaluate_dialog_advice_detail,
    learn_project_context_profiles,
    report_to_dict,
    serialize_dialogs,
)
from src.core.project_profile import compute_project_profile_from_samples
from src.core.storage import load_db_metadata
from tests.helpers import f_b, f_v, init_summary_db, make_step_meta


def _write_rich_transcript(root: Path, cid: str, entries: list[dict]) -> Path:
    tp = root / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
    return tp


class TestPhaseAndDualTrackAlerts(unittest.TestCase):
    def test_detect_work_phase_exploring_editing_verifying_and_task_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cid = "phase1"
            task_md = root / "brain" / cid / "task.md"
            task_md.parent.mkdir(parents=True, exist_ok=True)
            task_md.write_text(
                "# Checklist\n- [x] Diagnose\n- [x] Fix\n- [ ] Bench\n- [/] E2E\n",
                encoding="utf-8",
            )

            # 1) Pure exploring (even if Turn-1 startup prompt mentions 交接单, must NOT suppress alerts)
            entries: list[dict] = [
                {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-22T15:00:00Z", "content": "请先读取交接单继续处理，新建对话框后继续"},
                {
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": '"x:\\\\demo_app\\\\app.js"'}},
                        {
                            "name": "write_file",
                            "args": {
                                "Path": f'"C:\\\\Users\\\\dev\\\\.gemini\\\\antigravity\\\\brain\\\\{cid}\\\\task.md"',
                                "ArtifactMetadata": '{"UserFacing":false}',
                            },
                        },
                        {"name": "run_command", "args": {"CommandLine": '"git status"'}},
                    ],
                },
            ]
            _write_rich_transcript(root, cid, entries)
            wp1 = detect_dialog_work_phase(root, cid, is_running=True)
            self.assertEqual(wp1.phase, "exploring")
            self.assertFalse(wp1.is_handover_turn)
            self.assertEqual(wp1.code_edits, 0)
            self.assertEqual((wp1.task_done, wp1.task_remaining), (2, 2))

            # 2) Workspace edit followed by read_file of coupled module -> still editing
            entries.extend([
                {
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [{"name": "edit_file", "args": {"Path": '"x:\\\\demo_web\\\\rules.py"'}}],
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [{"name": "read_file", "args": {"Path": '"x:\\\\demo_web\\\\optimizer.py"'}}],
                },
            ])
            _write_rich_transcript(root, cid, entries)
            wp2 = detect_dialog_work_phase(root, cid, is_running=True)
            self.assertEqual(wp2.phase, "editing")
            self.assertEqual(wp2.code_edits, 1)

            # 3) Verification command after edits -> verifying
            entries.append({
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"npm run build"'}}],
            })
            _write_rich_transcript(root, cid, entries)
            wp3 = detect_dialog_work_phase(root, cid, is_running=True)
            self.assertEqual(wp3.phase, "verifying")

            # 4) Return to editing after failed test -> back to editing
            entries.append({
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "edit_file", "args": {"Path": '"x:\\\\demo_web\\\\optimizer.py"'}}],
            })
            _write_rich_transcript(root, cid, entries)
            self.assertEqual(detect_dialog_work_phase(root, cid, is_running=True).phase, "editing")

            # 4b) Verify then start reading next sub-task -> intra-turn new_stage; ask_question -> waiting_user
            entries.extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"pytest"'}}]},
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file", "args": {"Path": '"x:\\\\demo_web\\\\next.py"'}}]},
            ])
            _write_rich_transcript(root, cid, entries)
            self.assertEqual(detect_dialog_work_phase(root, cid, is_running=True).phase, "new_stage")

            entries.append({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "ask_question", "args": {}}]})
            _write_rich_transcript(root, cid, entries)
            self.assertEqual(detect_dialog_work_phase(root, cid, is_running=True).phase, "waiting_user")
            entries.append({"type": "GENERIC", "status": "DONE", "content": "ok"})
            _write_rich_transcript(root, cid, entries)
            self.assertEqual(detect_dialog_work_phase(root, cid, is_running=True).phase, "new_stage")

            # 5) New user turn (0 edits yet) stays exploring; idle overrides to idle; handover turn detected
            entries.extend([
                {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-22T15:10:00Z", "content": "继续排查第二个问题"},
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file", "args": {"Path": '"x:\\\\a.py"'}}]},
            ])
            _write_rich_transcript(root, cid, entries)
            wp5 = detect_dialog_work_phase(root, cid, is_running=True)
            self.assertEqual(wp5.phase, "exploring")
            self.assertFalse(wp5.is_handover_turn)
            wp_idle = detect_dialog_work_phase(root, cid, is_running=False)
            self.assertEqual(wp_idle.phase, "idle")
            self.assertEqual((wp_idle.task_done, wp_idle.task_remaining), (2, 2))

            entries.append({"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-22T15:15:00Z", "content": "写个交接单，我新建对话框继续"})
            _write_rich_transcript(root, cid, entries)
            self.assertTrue(detect_dialog_work_phase(root, cid, is_running=True).is_handover_turn)
            entries.append({"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-22T15:16:00Z", "content": "我需要换一个新的窗口继续处理后续的工作，帮我总结进度"})
            _write_rich_transcript(root, cid, entries)
            self.assertTrue(detect_dialog_work_phase(root, cid, is_running=True).is_handover_turn)
            entries.append({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "edit_file", "args": {"Path": '"x:\\\\a.py"'}}]})
            _write_rich_transcript(root, cid, entries)
            self.assertFalse(detect_dialog_work_phase(root, cid, is_running=True).is_handover_turn)

    def test_dual_track_running_relaxed_thresholds_and_phase_guidance(self) -> None:
        now = datetime(2026, 11, 22, 23, 50, 0, tzinfo=LOCAL_TZ)

        def make_512(
            model: str, ctx: int, running: bool, phase: str = "", done: int = 0, rem: int = 0, cc: int = 0,
        ) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="dt1", title="渲染优化", workspace="demo_app", model_label=model,
                is_running=running, input_tokens=5_000, cache_read_tokens=ctx - 5_000,
                output_tokens=800, thinking_tokens=200, last_active=now,
                compaction_count=cc, context_limit=COMPACTION_512K_TOKENS,
                work_phase=phase, task_done=done, task_remaining=rem,
            )

        for model, below_zone, in_watch, past_pause_run, above_safety in (
            ("Carbon", 200_000, 235_000, 310_000, 405_000),
            ("Neon", 170_000, 205_000, 310_000, 435_000),
            ("Hydrogen", 140_000, 175_000, 310_000, 455_000),
        ):
            d_ok = make_512(model, below_zone, True, phase="editing")
            self.assertEqual(evaluate_dialog_advice_detail(d_ok, "active", 0)[:2], ("ok", "状态健康"))
            self.assertEqual(classify_switch_kind(d_ok, "active", 0), "")

            d_watch = make_512(model, in_watch, True, phase="editing")
            adv_watch = evaluate_dialog_advice_detail(d_watch, "active", 0)
            self.assertEqual(adv_watch[:2], ("watch", "可考虑换新"))
            self.assertEqual(classify_switch_kind(d_watch, "active", 0), "zone_armed")

            for active_phase in ("editing", "exploring", "verifying", "new_stage"):
                d_run_mid = make_512(model, past_pause_run, True, phase=active_phase)
                self.assertEqual(evaluate_dialog_advice_detail(d_run_mid, "active", 0)[:2], ("watch", "停顿换新"))
                self.assertEqual(classify_switch_kind(d_run_mid, "active", 0), "zone_armed")

            self.assertEqual(classify_switch_kind(make_512(model, past_pause_run, True, phase="waiting_user"), "active", 0), "structural")
            self.assertEqual(classify_switch_kind(make_512(model, past_pause_run, False, phase="idle"), "fresh", 10), "structural")

            d_above = make_512(model, above_safety, True, phase="editing")
            self.assertEqual(evaluate_dialog_advice_detail(d_above, "active", 0)[:2], ("switch", "建议换新"))
            self.assertEqual(classify_switch_kind(d_above, "active", 0), "critical")

        wu_adv = evaluate_dialog_advice_detail(make_512("Carbon", 275_000, True, phase="waiting_user"), "active", 0)
        self.assertEqual(wu_adv[:2], ("switch", "建议换新"))
        self.assertIn("待确认", wu_adv[3])
        self.assertIn("经济线", wu_adv[3])
        crit_adv = evaluate_dialog_advice_detail(make_512("Carbon", 410_000, True, phase="editing"), "active", 0)
        self.assertEqual(crit_adv[:2], ("switch", "建议换新"))
        self.assertIn("压缩", crit_adv[2])
        self.assertNotIn("勿硬停", crit_adv[3])

    def test_idle_gentle_alert_lifecycle_and_no_double_spam(self) -> None:
        now = datetime(2026, 11, 22, 23, 55, 0, tzinfo=LOCAL_TZ)

        def make_dlg(
            cid: str, ctx: int, running: bool, sec_ago: int = 10, turns: int = 1, user_ts: float = 1000.0,
            phase: str | None = None, handover: bool = False,
        ) -> DialogContextInfo:
            eff_phase = phase if phase is not None else ("editing" if running else "idle")
            return DialogContextInfo(
                conversation_id=cid, title=f"任务_{cid}", workspace="demo_app", model_label="Carbon",
                is_running=running, input_tokens=5_000, cache_read_tokens=ctx - 5_000,
                output_tokens=600, thinking_tokens=100, last_active=now - timedelta(seconds=sec_ago),
                compaction_count=0, context_limit=COMPACTION_512K_TOKENS,
                user_turn_count=turns, post_finish_turn_count=turns, last_user_input_ts=user_ts,
                work_phase=eff_phase, is_handover_turn=handover,
            )

        tracker = SwitchAlertTracker()
        self.assertIsNone(tracker.process(serialize_dialogs((make_dlg("old_idle", 340_000, False, 30),), "%H:%M:%S", now=now)))
        self.assertIsNone(tracker.process(serialize_dialogs((make_dlg("old_idle", 340_000, False, 90),), "%H:%M:%S", now=now)))

        self.assertIsNone(tracker.process(serialize_dialogs((make_dlg("d1", 205_000, False, 5),), "%H:%M:%S", now=now)))
        a_zone = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("d1", 250_000, False, 5),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_zone), 1)
        self.assertTrue(a_zone[0][2])
        self.assertIn("长任务换新", a_zone[0][1])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("d1", 255_000, False, 15, turns=2, user_ts=1200.0),), "%H:%M:%S", now=now)), [])
        self.assertIsNone(tracker.process(serialize_dialogs((make_dlg("d1", 315_000, True, 0, turns=2, user_ts=1500.0, phase="exploring"),), "%H:%M:%S", now=now)))
        self.assertIsNone(tracker.process(serialize_dialogs((make_dlg("d1", 385_000, True, 0, turns=2, user_ts=1500.0, phase="editing"),), "%H:%M:%S", now=now)))

        a_pause = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("d1", 315_000, False, 12, turns=2, user_ts=1500.0),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_pause), 1)
        self.assertTrue(a_pause[0][2])
        self.assertIn("经济线", a_pause[0][1])

        a_t2_crit = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("d1", 405_000, True, 0, turns=3, user_ts=2000.0, phase="editing"),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_t2_crit), 1)
        self.assertFalse(a_t2_crit[0][2])
        self.assertIn("压缩", a_t2_crit[0][1])
        self.assertIsNone(tracker.process(serialize_dialogs((make_dlg("d1", 450_000, True, 0, turns=4, user_ts=3000.0, phase="new_stage", handover=True),), "%H:%M:%S", now=now)))

        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("c150", 150_000, False, sec_ago=9 * 60),), "%H:%M:%S", now=now)), [])
        a_cc = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("c195", 195_000, False, sec_ago=9 * 60),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_cc), 1)
        self.assertTrue(a_cc[0][2])
        self.assertIsNone(tracker.process(serialize_dialogs((make_dlg("c195", 195_000, False, sec_ago=18 * 60),), "%H:%M:%S", now=now)))

        batch = tracker.process_batch(serialize_dialogs((
            make_dlg("d200", 200_000, False, 5),
            make_dlg("d250", 250_000, False, 5),
            make_dlg("d285", 285_000, False, 5),
            make_dlg("d345", 345_000, False, 5),
        ), "%H:%M:%S", now=now))
        self.assertEqual(len(batch), 3)
        self.assertIn("短任务可续", batch[0][1])
        self.assertIn("经济线", batch[1][1])
        self.assertIn("经济线", batch[2][1])

    def test_end_to_end_report_populates_phase_aware_running_advice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now_loc = datetime(2026, 11, 22, 23, 50, 0, tzinfo=LOCAL_TZ)
            init_summary_db(
                root,
                [("c_e2e", "性能优化", '["file:///x%3A/demo_app"]', "CASCADE_RUN_STATUS_RUNNING", "", 0, now_loc.isoformat(), now_loc.isoformat())],
            )
            conv_db = root / "conversations" / "c_e2e.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            cc = sqlite3.connect(conv_db)
            cc.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            cc.execute("CREATE TABLE gen_metadata (size INTEGER, data BLOB)")
            sub_meta = f_v(3, 1437) + f_b(9, f_b(10, f_v(1, 300_000) + f_v(4, 512_000))) + f_b(19, b"gemini-4-carbon")
            gm_blob = f_b(1, sub_meta)
            cc.execute("INSERT INTO gen_metadata VALUES (?, ?)", (len(gm_blob), gm_blob))
            t_u = int((now_loc - timedelta(minutes=3)).timestamp())
            cc.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [
                    (0, 14, f_b(1, f_v(1, t_u))),
                    (1, 15, make_step_meta(t_u + 120, 1437, 8_000, 400, 396_000)),
                ],
            )
            cc.commit()
            cc.close()

            _write_rich_transcript(root, "c_e2e", [
                {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": (now_loc - timedelta(minutes=3)).isoformat()},
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file", "args": {"Path": '"x:\\\\demo_app\\\\a.js"'}}]},
            ])

            rep = compute_day_report(target_date=date(2026, 11, 22), antigravity_dir=root, now=now_loc)
            dlgs = report_to_dict(rep, now=now_loc)["dialogs"]
            self.assertEqual(len(dlgs), 1)
            self.assertEqual(dlgs[0]["advice_key"], "switch")
            self.assertEqual(dlgs[0]["switch_kind"], "critical")
            self.assertIn("压缩", dlgs[0]["advice_reason"])
            self.assertIn("交接", dlgs[0]["notify_detail"])

    def test_per_project_dynamic_profile_adapts_zone_and_cold_breakeven(self) -> None:
        light_prof = compute_project_profile_from_samples(
            "Neon",
            warmup_ctx_samples=[(58_000.0, 2.0)] * 10,
            warmup_usd_samples=[(0.34, 2.0)] * 10,
            turn_span_samples=[(85_000.0, 2.0)] * 10,
            turn_growth_samples=[(45_000.0, 2.0)] * 10,
            turn_steps_samples=[(14.0, 2.0)] * 10,
            step_growth_samples=[(3_200.0, 2.0)] * 10,
        )
        heavy_prof = compute_project_profile_from_samples(
            "Neon",
            warmup_ctx_samples=[(125_000.0, 2.0)] * 10,
            warmup_usd_samples=[(0.65, 2.0)] * 10,
            turn_span_samples=[(160_000.0, 2.0)] * 10,
            turn_growth_samples=[(70_000.0, 2.0)] * 10,
            turn_steps_samples=[(14.0, 2.0)] * 10,
            step_growth_samples=[(4_800.0, 2.0)] * 10,
        )
        self.assertIsNotNone(heavy_prof)
        self.assertIsNotNone(light_prof)
        assert heavy_prof is not None and light_prof is not None
        self.assertLess(light_prof.idle_switch_tokens, heavy_prof.idle_switch_tokens)
        self.assertLess(light_prof.zone_start_tokens, heavy_prof.zone_start_tokens)
        self.assertEqual(light_prof.run_switch_tokens, 430_000)
        self.assertEqual(heavy_prof.run_switch_tokens, 430_000)

        now = datetime(2026, 11, 23, 0, 10, 0, tzinfo=LOCAL_TZ)
        mid_ctx = (light_prof.idle_switch_tokens + heavy_prof.idle_switch_tokens) // 2
        d_light = DialogContextInfo(
            conversation_id="pl", title="轻启动任务", workspace="demo_web", model_label="Neon",
            is_running=False, input_tokens=5_000, cache_read_tokens=mid_ctx - 5_000,
            output_tokens=600, thinking_tokens=100, last_active=now - timedelta(seconds=15),
            context_limit=COMPACTION_512K_TOKENS, work_phase="idle", project_profile=light_prof,
        )
        d_heavy = DialogContextInfo(
            conversation_id="ph", title="重读码任务", workspace="demo_app", model_label="Neon",
            is_running=False, input_tokens=5_000, cache_read_tokens=mid_ctx - 5_000,
            output_tokens=600, thinking_tokens=100, last_active=now - timedelta(seconds=15),
            context_limit=COMPACTION_512K_TOKENS, work_phase="idle", project_profile=heavy_prof,
        )
        self.assertEqual(evaluate_dialog_advice_detail(d_light, "fresh", 15)[:2], ("switch", "建议换新"))
        self.assertNotEqual(evaluate_dialog_advice_detail(d_heavy, "fresh", 15)[0], "switch")

        d_cold_base = DialogContextInfo(
            conversation_id="cb", title="基准", workspace="NewProj", model_label="Neon",
            is_running=False, input_tokens=5_000, cache_read_tokens=145_000,
            output_tokens=500, thinking_tokens=100, last_active=now - timedelta(minutes=12),
            context_limit=COMPACTION_512K_TOKENS,
        )
        d_cold_heavy = DialogContextInfo(
            conversation_id="ch", title="高重建", workspace="demo_app", model_label="Neon",
            is_running=False, input_tokens=5_000, cache_read_tokens=145_000,
            output_tokens=500, thinking_tokens=100, last_active=now - timedelta(minutes=12),
            context_limit=COMPACTION_512K_TOKENS, project_profile=heavy_prof,
        )
        be_base = evaluate_cold_cache_breakeven(d_cold_base, "expired", 720)
        be_heavy = evaluate_cold_cache_breakeven(d_cold_heavy, "expired", 720)
        self.assertLess(float(be_heavy["ratio"]), float(be_base["ratio"]))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conv_dir = root / "conversations"
            conv_dir.mkdir(parents=True)
            rows = []
            for idx in range(12):
                cid = f"old_{idx}"
                t0 = now - timedelta(days=20 - idx)
                rows.append((cid, f"旧任务{idx}", '["file:///x%3A/HeavyApp"]', "CASCADE_RUN_STATUS_IDLE", "", 0, t0.isoformat(), t0.isoformat()))
                conn = sqlite3.connect(conv_dir / f"{cid}.db")
                conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
                ts0 = int(t0.timestamp())
                conn.executemany(
                    "INSERT INTO steps VALUES (?, ?, ?)",
                    [(0, 14, f_b(1, f_v(1, ts0))), (1, 15, make_step_meta(ts0 + 10, 1532, 15_000, 300, 0)), (2, 15, make_step_meta(ts0 + 20, 1532, 5_000, 300, 15_000)), (3, 15, make_step_meta(ts0 + 30, 1532, 5_000, 300, 20_000))],
                )
                conn.commit()
                conn.close()
            for idx in range(10):
                cid = f"recent_{idx}"
                t0 = now - timedelta(hours=10 - idx)
                rows.append((cid, f"近期重任务{idx}", '["file:///x%3A/HeavyApp"]', "CASCADE_RUN_STATUS_IDLE", "", 0, t0.isoformat(), t0.isoformat()))
                conn = sqlite3.connect(conv_dir / f"{cid}.db")
                conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
                ts0 = int(t0.timestamp())
                conn.executemany(
                    "INSERT INTO steps VALUES (?, ?, ?)",
                    [
                        (0, 14, f_b(1, f_v(1, ts0))),
                        (1, 15, make_step_meta(ts0 + 10, 1532, 40_000, 1000, 0)),
                        (2, 15, make_step_meta(ts0 + 20, 1532, 50_000, 1000, 40_000)),
                        (3, 15, make_step_meta(ts0 + 30, 1532, 130_000, 1500, 90_000)),
                    ],
                )
                conn.commit()
                conn.close()
            init_summary_db(root, rows)
            profs = learn_project_context_profiles(root, load_db_metadata(root / "conversation_summaries.db"), now)
            p_heavy = profs.get(("HeavyApp", "Neon"))
            self.assertIsNotNone(p_heavy)
            assert p_heavy is not None
            self.assertEqual(p_heavy.sample_count, 10)
            self.assertGreaterEqual(p_heavy.working_ctx_tokens, 80_000)
            self.assertEqual(p_heavy.run_switch_tokens, 430_000)

    def test_deferred_structural_alert_notifies_when_safe_switch_node_reached(self) -> None:
        now = datetime(2026, 11, 23, 2, 55, 0, tzinfo=LOCAL_TZ)

        def make_dlg(
            cid: str, ctx: int, running: bool, phase: str, sec_ago: int = 0, handover: bool = False,
        ) -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id=cid, title=f"任务修复_{cid}", workspace="demo_app", model_label="Carbon",
                is_running=running, input_tokens=5_000, cache_read_tokens=ctx - 5_000,
                output_tokens=600, thinking_tokens=100, last_active=now - timedelta(seconds=sec_ago),
                compaction_count=0, context_limit=COMPACTION_512K_TOKENS,
                user_turn_count=1, post_finish_turn_count=0, last_user_input_ts=1000.0,
                work_phase=phase, is_handover_turn=handover,
            )

        tracker = SwitchAlertTracker()
        self.assertIsNone(tracker.process(serialize_dialogs((make_dlg("s_idle", 220_000, True, "exploring"),), "%H:%M:%S", now=now)))

        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_idle", 340_000, True, "editing"),), "%H:%M:%S", now=now)), [])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_idle", 385_000, True, "verifying"),), "%H:%M:%S", now=now)), [])
        a_stop = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_idle", 385_000, False, "idle", sec_ago=10),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_stop), 1)
        self.assertTrue(a_stop[0][2])
        self.assertIn("经济线", a_stop[0][1])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_idle", 385_000, False, "idle", sec_ago=60),), "%H:%M:%S", now=now)), [])

        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_wu", 310_000, True, "editing"),), "%H:%M:%S", now=now)), [])
        a_wu = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_wu", 310_000, True, "waiting_user"),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_wu), 1)
        self.assertTrue(a_wu[0][2])
        self.assertIn("待确认", a_wu[0][1])
        self.assertEqual(tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_wu", 310_000, True, "waiting_user"),), "%H:%M:%S", now=now)), [])
        a_wu_done = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_wu", 345_000, False, "idle", sec_ago=5),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_wu_done), 1)
        self.assertTrue(a_wu_done[0][2])

        a_f2 = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_bg", 405_000, True, "editing"),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_f2), 1)
        self.assertFalse(a_f2[0][2])
        self.assertIn("压缩", a_f2[0][1])
        self.assertNotIn("勿硬停", a_f2[0][1])
        a_bg_stop = tracker.process_batch_with_kind(serialize_dialogs((make_dlg("s_bg", 412_000, False, "idle", sec_ago=720),), "%H:%M:%S", now=now))
        self.assertEqual(len(a_bg_stop), 1)
        self.assertFalse(a_bg_stop[0][2])
        self.assertIn("临近压缩", a_bg_stop[0][1])

        ho_adv = evaluate_dialog_advice_detail(make_dlg("s_ho", 410_000, True, "new_stage", handover=True), "active", 0)
        self.assertEqual(ho_adv[:2], ("watch", "交接中"))
        self.assertEqual(classify_switch_kind(make_dlg("s_ho", 410_000, True, "new_stage", handover=True), "active", 0), "")


if __name__ == "__main__":
    unittest.main()
