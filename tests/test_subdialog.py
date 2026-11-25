"""Unit tests for child subagent / swarm worker context visibility, reuse advice, and overload alerts."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from config.settings import LOCAL_TZ, RUNNING_STATUS
from src.core.advisor import (
    classify_switch_kind,
    collect_dialog_contexts,
    evaluate_dialog_advice_detail,
)
from src.core.alerts import SwitchAlertTracker
from src.core.live_state import compute_live_display_update
from src.core.models import DialogContextInfo
from src.core.reports import serialize_dialogs
from src.ui.i18n import localize_advice_reason, localize_advice_text, localize_toast
from tests.helpers import init_summary_db, make_step_meta_with_end


def _write_conv_db(
    root: Path,
    cid: str,
    model_enum: int,
    in_tok: int,
    cache_tok: int,
    step_ts: int,
    context_limit: int = 512_000,
    compactions: int = 0,
) -> Path:
    conv_dir = root / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    db_path = conv_dir / f"{cid}.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE gen_metadata (key TEXT, value TEXT)")
    model_name = "gemini-4-neon-high" if model_enum == 1532 else (
        "gemini-4-hydrogen-high" if model_enum == 1525 else "gemini-4-carbon-medium"
    )
    meta_json = json.dumps({model_name: {"enum": model_enum, "contextLimit": context_limit}})
    conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", ("models", meta_json))
    conn.execute("CREATE TABLE steps (step_type INTEGER, metadata BLOB)")
    conn.execute(
        "INSERT INTO steps VALUES (?, ?)",
        (15, make_step_meta_with_end(step_ts - 10, step_ts, model_enum, in_tok, 1_500, cache_tok)),
    )
    for i in range(compactions):
        conn.execute(
            "INSERT INTO steps VALUES (?, ?)",
            (23, make_step_meta_with_end(step_ts + i + 1, step_ts + i + 1, 0, 0, 0, 0)),
        )
    conn.commit()
    conn.close()
    os.utime(db_path, (step_ts, step_ts))
    return db_path


class SubdialogTests(unittest.TestCase):
    def test_interleaves_running_and_recent_children_and_hides_stale(self) -> None:
        now = datetime(2026, 11, 23, 10, 0, 0, tzinfo=LOCAL_TZ)
        day_start = now.replace(hour=0, minute=0, second=0)
        ws = json.dumps(["file:///C:/Work/demo_web"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t_root = int((now - timedelta(minutes=2)).timestamp())
            t_run = int((now - timedelta(seconds=15)).timestamp())
            t_idle_ok = int((now - timedelta(minutes=12)).timestamp())
            t_stale = int((now - timedelta(hours=3)).timestamp())

            init_summary_db(
                root,
                [
                    ("root-1", "核心模块重构", ws, "CASCADE_RUN_STATUS_IDLE", "", 0, (now - timedelta(minutes=2)).isoformat(), (now - timedelta(minutes=3)).isoformat()),
                    ("sub-run", "", ws, RUNNING_STATUS, "root-1", 1, now.isoformat(), ""),
                    ("sub-idle", "", ws, "CASCADE_RUN_STATUS_IDLE", "root-1", 1, (now - timedelta(minutes=12)).isoformat(), ""),
                    ("sub-stale", "", ws, "CASCADE_RUN_STATUS_IDLE", "root-1", 1, (now - timedelta(hours=3)).isoformat(), ""),
                ],
            )
            _write_conv_db(root, "root-1", 1437, 10_000, 75_000, t_root)
            _write_conv_db(root, "sub-run", 1532, 12_000, 180_000, t_run)
            _write_conv_db(root, "sub-idle", 1532, 8_000, 96_000, t_idle_ok)
            stale_p = _write_conv_db(root, "sub-stale", 1525, 8_000, 80_000, t_stale)
            os.utime(stale_p, (t_stale, t_stale))

            dialogs = collect_dialog_contexts(root, day_start, now, is_active_period=True)
            ids = [d.conversation_id for d in dialogs]
            self.assertEqual(ids, ["root-1", "sub-run", "sub-idle"])
            self.assertFalse(dialogs[0].is_subdialog)
            self.assertTrue(dialogs[1].is_subdialog)
            self.assertEqual(dialogs[1].model_label, "Neon")
            self.assertEqual(dialogs[1].parent_title, "核心模块重构")
            self.assertTrue(dialogs[2].is_subdialog)
            self.assertEqual(dialogs[2].context_tokens, 104_000)

            hist_dialogs = collect_dialog_contexts(root, day_start, now, is_active_period=False)
            self.assertEqual([d.conversation_id for d in hist_dialogs], ["root-1"])

    def test_subdialog_advice_tiers_and_zero_false_cold_cache_alerts(self) -> None:
        now = datetime(2026, 11, 23, 10, 0, 0, tzinfo=LOCAL_TZ)

        def _mk(ctx: int, running: bool, cc: int = 0, model: str = "Neon") -> DialogContextInfo:
            return DialogContextInfo(
                conversation_id="sub-1",
                title=f"{model} 子窗口",
                workspace="demo_web",
                model_label=model,
                is_running=running,
                input_tokens=ctx,
                cache_read_tokens=0,
                output_tokens=1_000,
                thinking_tokens=0,
                last_active=now - timedelta(minutes=15 if not running else 0),
                compaction_count=cc,
                self_running=running,
                context_limit=512_000,
                is_subdialog=True,
                parent_conversation_id="root-1",
                parent_title="核心模块重构",
            )

        # 100K idle expired subdialog stays healthy and reusable (no cold-cache popup).
        d_100k = _mk(100_000, running=False)
        adv = evaluate_dialog_advice_detail(d_100k, "expired", 900)
        self.assertEqual(adv[0], "ok")
        self.assertIn("可复用", adv[2])
        self.assertEqual(classify_switch_kind(d_100k, "expired", 900), "")

        # 200K idle fresh -> ok; expired -> quiet watch (still no popup).
        d_200k = _mk(200_000, running=False)
        self.assertEqual(evaluate_dialog_advice_detail(d_200k, "fresh", 60)[0], "ok")
        self.assertEqual(evaluate_dialog_advice_detail(d_200k, "expired", 900)[0], "watch")
        self.assertEqual(classify_switch_kind(d_200k, "expired", 900), "")

        # 270K idle -> quiet panel-only watch ("下轮换新"), no desktop toast.
        d_270k = _mk(270_000, running=False)
        adv_270 = evaluate_dialog_advice_detail(d_270k, "fresh", 60)
        self.assertEqual(adv_270[0], "watch")
        self.assertEqual(adv_270[1], "下轮换新")
        self.assertEqual(localize_advice_text(adv_270[1], "en"), "Next fresh")
        self.assertEqual(classify_switch_kind(d_270k, "fresh", 60), "zone")

        # 435K running Neon -> Step-2 critical toast with parent title in header.
        d_435k = _mk(435_000, running=True)
        adv_435 = evaluate_dialog_advice_detail(d_435k, "active", 0)
        self.assertEqual(adv_435[0], "switch")
        self.assertEqual(classify_switch_kind(d_435k, "active", 0), "critical")

        tracker = SwitchAlertTracker()
        tracker.process_batch([], notify_enabled=True)
        ser_low = serialize_dialogs((d_100k, d_270k), "%H:%M:%S", now=now)
        self.assertEqual(tracker.process_batch(ser_low, notify_enabled=True), [])

        ser_high = serialize_dialogs((d_435k,), "%H:%M:%S", now=now)
        alerts = tracker.process_batch(ser_high, notify_enabled=True)
        self.assertEqual(len(alerts), 1)
        self.assertIn("核心模块重构 · 子窗口 (Neon)", alerts[0][0])
        self.assertIn("kill 换新 worker", alerts[0][1])

        en_head, en_body = localize_toast(alerts[0][0], alerts[0][1], "en")
        self.assertIn("Worker (Neon)", en_head)
        self.assertTrue(len(en_body) > 0)
        self.assertTrue(len(localize_advice_reason(adv[2], "en")) > 0)

    def test_live_tick_preserves_subdialog_reuse_advice(self) -> None:
        now = datetime(2026, 11, 23, 10, 0, 0, tzinfo=LOCAL_TZ)
        d_sub = DialogContextInfo(
            conversation_id="sub-live",
            title="Neon 子窗口",
            workspace="demo_app",
            model_label="Neon",
            is_running=False,
            input_tokens=10_000,
            cache_read_tokens=95_000,
            output_tokens=1_000,
            thinking_tokens=0,
            last_active=now - timedelta(minutes=2),
            self_running=False,
            context_limit=512_000,
            is_subdialog=True,
            parent_conversation_id="root-1",
            parent_title="渲染性能优化",
        )
        payload = {"base_total_seconds": 600.0, "dialogs": serialize_dialogs((d_sub,), "%H:%M:%S", now=now)}
        _, _, changed = compute_live_display_update(payload, 0.0, now + timedelta(minutes=12))
        self.assertTrue(changed)
        updated = payload["dialogs"][0]
        self.assertEqual(updated["cache_state"], "expired")
        self.assertEqual(updated["advice_key"], "ok")
        self.assertIn("可复用", updated["advice_reason"])


if __name__ == "__main__":
    unittest.main()
