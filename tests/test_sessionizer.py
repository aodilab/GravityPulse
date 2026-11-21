"""Unit tests for timestamp parsing, 30-minute sessionizer, cross-window bridging, and reports."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from config.settings import LOCAL_TZ
from src.core import (
    TimeInterval,
    local_week_bounds,
    compute_day_report,
    compute_week_report,
    extract_workspace_name,
    format_duration,
    merge_intervals,
    parse_iso_utc,
    report_to_dict,
    sessionize_timestamps,
    week_report_to_dict,
)
from tests.helpers import init_summary_db, local_utc_sql, local_utc_z, write_transcript


class TestSessionizerAndReports(unittest.TestCase):
    def test_parse_iso_utc_handles_seven_fractional_digits_and_z(self) -> None:
        dt1 = parse_iso_utc("2026-11-14T19:03:53Z")
        self.assertEqual(dt1, datetime(2026, 11, 14, 19, 3, 53, tzinfo=timezone.utc))

        dt2 = parse_iso_utc("2026-11-14 19:06:53.9316276+00:00")
        self.assertEqual(dt2, datetime(2026, 11, 14, 19, 6, 53, 931627, tzinfo=timezone.utc))

        self.assertIsNone(parse_iso_utc("0001-01-01 00:00:00+00:00"))

    def test_sessionize_connects_within_30_min_and_drops_idle_tail(self) -> None:
        base = datetime(2026, 11, 15, 10, 0, tzinfo=LOCAL_TZ)
        ts = [
            base,
            base + timedelta(minutes=25),
            base + timedelta(minutes=70),
            base + timedelta(minutes=80),
        ]
        segs = sessionize_timestamps(ts, is_running=False, now=base + timedelta(hours=3))
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0], TimeInterval(base, base + timedelta(minutes=25)))
        self.assertEqual(segs[1], TimeInterval(base + timedelta(minutes=70), base + timedelta(minutes=80)))
        self.assertEqual(sum(s.seconds for s in segs), 35 * 60)

    def test_sessionize_running_extends_up_to_30_min_only(self) -> None:
        base = datetime(2026, 11, 15, 10, 0, tzinfo=LOCAL_TZ)
        ts = [base, base + timedelta(minutes=5)]

        segs_12 = sessionize_timestamps(ts, is_running=True, now=base + timedelta(minutes=17))
        self.assertEqual(segs_12, [TimeInterval(base, base + timedelta(minutes=17))])

        segs_50 = sessionize_timestamps(ts, is_running=True, now=base + timedelta(minutes=55))
        self.assertEqual(segs_50, [TimeInterval(base, base + timedelta(minutes=35))])

    def test_merge_intervals_union_for_parallel_windows(self) -> None:
        t0 = datetime(2026, 11, 15, 10, 0, tzinfo=LOCAL_TZ)
        merged = merge_intervals(
            [
                TimeInterval(t0, t0 + timedelta(minutes=60)),
                TimeInterval(t0 + timedelta(minutes=15), t0 + timedelta(minutes=75)),
                TimeInterval(t0 + timedelta(minutes=90), t0 + timedelta(minutes=100)),
            ]
        )
        self.assertEqual(
            merged,
            [
                TimeInterval(t0, t0 + timedelta(minutes=75)),
                TimeInterval(t0 + timedelta(minutes=90), t0 + timedelta(minutes=100)),
            ],
        )

    def test_compute_day_report_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("conv-a", "主对话A", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 0, 20), local_utc_sql(2026, 11, 14, 23, 45)),
                    ("conv-b", "并行对话B", '["file:///c%3A/Work/Beta"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 0, 40), local_utc_sql(2026, 11, 15, 0, 10)),
                    ("conv-c", "", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "conv-a", 1, local_utc_sql(2026, 11, 15, 0, 35), "0001-01-01 00:00:00+00:00"),
                    ("conv-d", "纯系统", '["file:///c%3A/Work/Sys"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 1, 0), "0001-01-01 00:00:00+00:00"),
                ],
            )
            write_transcript(
                root, "conv-a",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 14, 23, 45)),
                    ("MODEL", "GENERIC", local_utc_z(2026, 11, 15, 0, 0)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 15, 0, 20)),
                ],
            )
            write_transcript(
                root, "conv-c",
                [
                    ("SYSTEM", "SYSTEM_MESSAGE", local_utc_z(2026, 11, 15, 0, 21)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 15, 0, 35)),
                ],
            )
            write_transcript(
                root, "conv-b",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 0, 10)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 15, 0, 40)),
                ],
            )
            write_transcript(
                root, "conv-d",
                [
                    ("SYSTEM", "SYSTEM_MESSAGE", local_utc_z(2026, 11, 15, 0, 50)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 15, 1, 0)),
                ],
            )

            now_bj = datetime(2026, 11, 15, 3, 0, tzinfo=LOCAL_TZ)
            report = compute_day_report(target_date=date(2026, 11, 15), antigravity_dir=root, now=now_bj)
            self.assertEqual(report.total_seconds, 10 * 60)
            self.assertEqual(len(report.conversations), 1)
            self.assertEqual(format_duration(report.total_seconds), "10 分钟")

            payload = report_to_dict(report, now=now_bj)
            self.assertEqual(payload["total_human"], "10 分钟")
            self.assertEqual(payload["total_hms"], "00:10:00")
            self.assertEqual(len(payload["timeline_segments"]), 1)
            self.assertEqual(len(payload["conversations"]), 1)

            mon, sun = local_week_bounds(date(2026, 11, 15))
            self.assertEqual((mon, sun), (date(2026, 11, 9), date(2026, 11, 15)))

            wk = compute_week_report(target_date=date(2026, 11, 15), antigravity_dir=root, now=now_bj)
            self.assertEqual(wk.total_seconds, 25 * 60)
            self.assertEqual(len(wk.days), 7)
            wk_dict = week_report_to_dict(wk, now=now_bj)
            self.assertEqual(wk_dict["total_human"], "25 分钟")
            self.assertEqual(wk_dict["active_days"], 2)

            self.assertEqual(len(report.projects), 1)
            alpha_wk = next(p for p in wk.projects if p.workspace == "Alpha")
            self.assertEqual(alpha_wk.total_seconds, 0)

    def test_same_project_multiple_dialogs_are_unioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("p1", "窗口1", '["file:///c%3A/Work/ProjX"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 10, 30), local_utc_sql(2026, 11, 15, 10, 14)),
                    ("p2", "窗口2", '["file:///c%3A/Work/ProjX"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 10, 45), local_utc_sql(2026, 11, 15, 10, 25)),
                ],
            )
            write_transcript(
                root, "p1",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 10, 0)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 10, 14)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 15, 10, 30)),
                ],
            )
            write_transcript(
                root, "p2",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 10, 10)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 10, 25)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 15, 10, 45)),
                ],
            )

            rep = compute_day_report(
                target_date=date(2026, 11, 15),
                antigravity_dir=root,
                now=datetime(2026, 11, 15, 12, 0, tzinfo=LOCAL_TZ),
            )
            self.assertEqual(len(rep.projects), 1)
            self.assertEqual(rep.projects[0].workspace, "ProjX")
            self.assertEqual(rep.projects[0].conversation_count, 2)
            self.assertEqual(rep.projects[0].total_seconds, 25 * 60)

    def test_unbound_workspace_counted_in_total_but_hidden_from_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("proj", "正式项目", '["file:///c%3A/Work/RealProj"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 10, 30), local_utc_sql(2026, 11, 15, 10, 0)),
                    ("unbound", "临时对话", "[]", "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 15, 13, 0), local_utc_sql(2026, 11, 15, 12, 0)),
                ],
            )
            write_transcript(
                root, "proj",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 10, 0)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 10, 30)),
                ],
            )
            write_transcript(
                root, "unbound",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 12, 0)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 15, 12, 30)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 15, 12, 45)),
                ],
            )

            now_bj = datetime(2026, 11, 15, 13, 0, tzinfo=LOCAL_TZ)
            day_rep = compute_day_report(target_date=date(2026, 11, 15), antigravity_dir=root, now=now_bj)
            self.assertEqual(day_rep.total_seconds, 60 * 60)
            day_payload = report_to_dict(day_rep, now=now_bj)
            self.assertEqual(day_payload["total_seconds"], 60 * 60)
            self.assertEqual([p["workspace"] for p in day_payload["projects"]], ["RealProj"])
            self.assertEqual(day_payload["project_count"], 1)

            wk_rep = compute_week_report(target_date=date(2026, 11, 15), antigravity_dir=root, now=now_bj)
            wk_payload = week_report_to_dict(wk_rep, now=now_bj)
            self.assertEqual(wk_payload["total_seconds"], 60 * 60)
            self.assertEqual([p["workspace"] for p in wk_payload["projects"]], ["RealProj"])
            self.assertEqual(wk_payload["project_count"], 1)

    def test_no_tail_after_last_user_message_even_with_long_background_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("bg-tail", "长后台任务排查", '["file:///c%3A/Work/demo_web"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 23, 43, 51), local_utc_sql(2026, 11, 16, 22, 13, 28)),
                ],
            )
            write_transcript(
                root, "bg-tail",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 22, 7, 47)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 22, 7, 58)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 22, 13, 28)),
                    ("MODEL", "GENERIC", local_utc_z(2026, 11, 16, 22, 25, 0)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 23, 5, 37)),
                    ("SYSTEM", "SYSTEM_MESSAGE", local_utc_z(2026, 11, 16, 23, 39, 38)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 23, 43, 51)),
                ],
            )
            rep = compute_day_report(
                target_date=date(2026, 11, 16),
                antigravity_dir=root,
                now=datetime(2026, 11, 17, 5, 18, tzinfo=LOCAL_TZ),
            )
            self.assertEqual(
                rep.merged_intervals,
                (
                    TimeInterval(
                        datetime(2026, 11, 16, 22, 7, 47, tzinfo=LOCAL_TZ),
                        datetime(2026, 11, 16, 22, 13, 28, tzinfo=LOCAL_TZ),
                    ),
                ),
            )
            self.assertEqual(rep.total_seconds, 341)

    def test_active_task_extends_user_gap_tolerance_to_60m_but_not_beyond(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("long_task", "长任务等待", '["file:///c%3A/Work/Alpha"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 17, 0), local_utc_sql(2026, 11, 16, 16, 20)),
                ],
            )
            write_transcript(
                root, "long_task",
                [
                    # Segment 1: 10:00 user -> AI runs 25m until 10:25 -> 10:40 user (40m <= 60m, post-task idle 15m <= 30m -> bridged!)
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 10, 0)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 10, 25)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 10, 40)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 10, 40, 10)),
                    # Gap 45m after 10s quick reply (10:40:10 -> 11:25:00 = 44m50s idle > 30m -> MUST split!)
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 11, 25)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 11, 40)),
                    # Continuous AI logging for 80m without user input (11:40 -> 13:00 = 80m > 60m -> MUST split!)
                    ("MODEL", "GENERIC", local_utc_z(2026, 11, 16, 12, 0)),
                    ("MODEL", "GENERIC", local_utc_z(2026, 11, 16, 12, 20)),
                    ("MODEL", "GENERIC", local_utc_z(2026, 11, 16, 12, 45)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 13, 0)),
                ],
            )
            rep = compute_day_report(
                target_date=date(2026, 11, 16),
                antigravity_dir=root,
                now=datetime(2026, 11, 16, 18, 0, tzinfo=LOCAL_TZ),
            )
            self.assertEqual(
                rep.merged_intervals,
                (
                    TimeInterval(datetime(2026, 11, 16, 10, 0, tzinfo=LOCAL_TZ), datetime(2026, 11, 16, 10, 40, tzinfo=LOCAL_TZ)),
                    TimeInterval(datetime(2026, 11, 16, 11, 25, tzinfo=LOCAL_TZ), datetime(2026, 11, 16, 11, 40, tzinfo=LOCAL_TZ)),
                ),
            )
            self.assertEqual(rep.total_seconds, 55 * 60)

    def test_cross_dialog_single_prompts_bridge_with_30m_and_60m_task_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("m6", "上线排查6", '["file:///c%3A/Work/demo_app"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 2, 6, 27), local_utc_sql(2026, 11, 16, 1, 46, 35)),
                    ("y2", "构建优化2", '["file:///c%3A/Work/demo_web"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 2, 24, 41), local_utc_sql(2026, 11, 16, 2, 24, 20)),
                    ("y3", "构建优化3", '["file:///c%3A/Work/demo_web"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 2, 45, 0), local_utc_sql(2026, 11, 16, 2, 35, 0)),
                    ("late", "孤立单条", '["file:///c%3A/Work/demo_app"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 3, 55, 0), local_utc_sql(2026, 11, 16, 3, 50, 0)),
                ],
            )
            write_transcript(root, "m6", [("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 1, 46, 35)), ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 2, 6, 27))])
            write_transcript(root, "y2", [("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 2, 24, 20)), ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 2, 24, 41))])
            write_transcript(root, "y3", [("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 2, 35, 0)), ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 2, 45, 0))])
            write_transcript(root, "late", [("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 3, 50, 0)), ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 3, 55, 0))])

            now_bj = datetime(2026, 11, 16, 5, 0, 0, tzinfo=LOCAL_TZ)
            rep = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_bj)

            self.assertEqual(
                rep.merged_intervals,
                (
                    TimeInterval(datetime(2026, 11, 16, 1, 46, 35, tzinfo=LOCAL_TZ), datetime(2026, 11, 16, 2, 35, 0, tzinfo=LOCAL_TZ)),
                ),
            )
            web_proj = next(p for p in rep.projects if p.workspace == "demo_web")
            self.assertEqual(web_proj.conversation_count, 2)
            self.assertEqual(web_proj.total_seconds, 10 * 60 + 40)
            app_proj = next(p for p in rep.projects if p.workspace == "demo_app")
            self.assertEqual(app_proj.conversation_count, 2)
            self.assertEqual(app_proj.total_seconds, 0)

    def test_extract_workspace_name_merges_worktrees_into_parent_project(self) -> None:
        self.assertEqual(
            extract_workspace_name('["file:///C:/Users/dev/.gemini/antigravity/worktrees/demo_app/delete_old_worktrees"]'),
            "demo_app",
        )
        self.assertEqual(
            extract_workspace_name('["file:///Users/dev/.gemini/antigravity/worktrees/demo_web/feat_branch/subdir"]'),
            "demo_web",
        )
        self.assertEqual(
            extract_workspace_name('["file:///c%3A/Users/dev/Desktop/demo_app/.worktrees/hotfix"]'),
            "demo_app",
        )
        self.assertEqual(
            extract_workspace_name('["file:///c%3A/Users/dev/Desktop/demo_app/.claude/worktrees/exp"]'),
            "demo_app",
        )
        self.assertEqual(
            extract_workspace_name('["file:///c%3A/Users/dev/Desktop/worktrees/standalone"]'),
            "standalone",
        )

    def test_worktree_conversation_merges_into_main_project_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_summary_db(
                root,
                [
                    ("main-1", "主分支任务", '["file:///c%3A/Users/dev/Desktop/demo_app"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 2, 10), local_utc_sql(2026, 11, 16, 2, 0)),
                    ("wt-1", "子分支排查", '["file:///C:/Users/dev/.gemini/antigravity/worktrees/demo_app/delete_old_worktrees"]', "CASCADE_RUN_STATUS_IDLE", "", 0, local_utc_sql(2026, 11, 16, 3, 14), local_utc_sql(2026, 11, 16, 3, 0)),
                ],
            )
            write_transcript(
                root, "main-1",
                [("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 2, 0)), ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 2, 10))],
            )
            write_transcript(
                root, "wt-1",
                [
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 3, 0)),
                    ("USER_EXPLICIT", "USER_INPUT", local_utc_z(2026, 11, 16, 3, 14)),
                    ("MODEL", "PLANNER_RESPONSE", local_utc_z(2026, 11, 16, 3, 25)),
                ],
            )

            now_bj = datetime(2026, 11, 16, 4, 0, 0, tzinfo=LOCAL_TZ)
            rep = compute_day_report(target_date=date(2026, 11, 16), antigravity_dir=root, now=now_bj)
            self.assertEqual([p.workspace for p in rep.projects], ["demo_app"])
            self.assertEqual(rep.projects[0].conversation_count, 2)
            self.assertEqual(rep.projects[0].total_seconds, 24 * 60)
