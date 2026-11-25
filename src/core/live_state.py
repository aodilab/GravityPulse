"""Lightweight directory fingerprinting and pure-memory 1s live stopwatch/cache progression."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path

from config.settings import (
    CARBON_CACHE_COOLING_SEC,
    CARBON_CACHE_EXPIRED_SEC,
    CARBON_CACHE_FRESH_SEC,
    IMPLICIT_CACHE_COOLING_SEC,
    IMPLICIT_CACHE_EXPIRED_SEC,
    IMPLICIT_CACHE_FRESH_SEC,
    IMPLICIT_CACHE_MIN_TOKENS,
    LOCAL_TZ,
    NEON_CACHE_COOLING_SEC,
    NEON_CACHE_EXPIRED_SEC,
    NEON_CACHE_FRESH_SEC,
)
from src.core.advisor import (
    _format_idle_ago,
    classify_switch_kind,
    evaluate_dialog_advice_detail,
    evaluate_implicit_cache_status,
)
from src.core.models import (
    DialogContextInfo,
    TimeInterval,
    format_duration,
    format_hms,
)


def compute_source_mtime_ns(project_root: Path) -> int:
    """Return max `st_mtime_ns` across GravityPulse `.py` modules so resident processes can hot-reload on code updates."""
    latest = 0
    entry = project_root / "build.py"
    try:
        latest = max(latest, entry.stat().st_mtime_ns)
    except OSError:
        pass
    for rel in ("config", os.path.join("src", "core"), os.path.join("src", "ui")):
        folder = project_root / rel
        if not folder.is_dir():
            continue
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if e.name.endswith(".py"):
                        try:
                            latest = max(latest, e.stat().st_mtime_ns)
                        except OSError:
                            pass
        except OSError:
            pass
    return latest


def compute_telemetry_fingerprint(antigravity_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """Return an exact stat-tuple fingerprint over local Antigravity telemetry files (~3 ms)."""
    entries: list[tuple[str, int, int]] = []
    for name in ("conversation_summaries.db", "conversation_summaries.db-wal"):
        try:
            st = (antigravity_dir / name).stat()
            entries.append((f"root:{name}", st.st_mtime_ns, st.st_size))
        except OSError:
            pass

    conv_dir = antigravity_dir / "conversations"
    if conv_dir.is_dir():
        try:
            with os.scandir(conv_dir) as it:
                for e in it:
                    if e.name.endswith((".db", ".db-wal")):
                        try:
                            st = e.stat()
                            entries.append((f"conv:{e.name}", st.st_mtime_ns, st.st_size))
                        except OSError:
                            pass
        except OSError:
            pass

    brain_dir = antigravity_dir / "brain"
    if brain_dir.is_dir():
        try:
            with os.scandir(brain_dir) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        tp = os.path.join(e.path, ".system_generated", "logs", "transcript.jsonl")
                        try:
                            st = os.stat(tp)
                            entries.append((f"tr:{e.name}", st.st_mtime_ns, st.st_size))
                        except OSError:
                            pass
                        tm = os.path.join(e.path, "task.md")
                        try:
                            st_m = os.stat(tm)
                            entries.append((f"tm:{e.name}", st_m.st_mtime_ns, st_m.st_size))
                        except OSError:
                            pass
        except OSError:
            pass

    ann_dir = antigravity_dir / "annotations"
    if ann_dir.is_dir():
        try:
            with os.scandir(ann_dir) as it:
                for e in it:
                    if e.name.endswith(".pbtxt"):
                        try:
                            st = e.stat()
                            entries.append((f"ann:{e.name}", st.st_mtime_ns, st.st_size))
                        except OSError:
                            pass
        except OSError:
            pass

    entries.sort()
    return tuple(entries)


def live_report_fields(
    base_total_seconds: float,
    is_active: bool,
    running_count: int,
    merged_intervals: tuple[TimeInterval, ...],
    dialogs: tuple[DialogContextInfo, ...],
    now_loc: datetime,
    cap_end: datetime,
) -> dict[str, float | bool]:
    """Return confirmed work-time fields without speculative unattended tail ticking."""
    _ = (is_active, running_count, merged_intervals, dialogs, now_loc, cap_end)
    return {
        "base_total_seconds": float(base_total_seconds),
        "live_tick_active": False,
        "live_cap_seconds": 0.0,
    }


def _expected_idle_state(d: dict, idle_sec: int) -> str:
    if int(d.get("context_tokens", 0)) < IMPLICIT_CACHE_MIN_TOKENS:
        return "none"
    th = d.get("cache_thresholds")
    if th and len(th) == 3:
        fresh_s, cool_s, exp_s = int(th[0]), int(th[1]), int(th[2])
    elif d.get("model_label") == "Carbon":
        fresh_s, cool_s, exp_s = CARBON_CACHE_FRESH_SEC, CARBON_CACHE_COOLING_SEC, CARBON_CACHE_EXPIRED_SEC
    elif d.get("model_label") == "Neon":
        fresh_s, cool_s, exp_s = NEON_CACHE_FRESH_SEC, NEON_CACHE_COOLING_SEC, NEON_CACHE_EXPIRED_SEC
    else:
        fresh_s, cool_s, exp_s = IMPLICIT_CACHE_FRESH_SEC, IMPLICIT_CACHE_COOLING_SEC, IMPLICIT_CACHE_EXPIRED_SEC
    if idle_sec < fresh_s:
        return "fresh"
    if idle_sec < cool_s:
        return "cooling"
    if idle_sec < exp_s:
        return "decay"
    return "expired"


def compute_live_display_update(
    payload: dict,
    elapsed_mono: float,
    now_loc: datetime,
) -> tuple[str, str, bool]:
    """Advance hero stopwatch strings and idle-dialog cache status in memory."""
    if payload.get("live_tick_active"):
        delta = min(max(0.0, elapsed_mono), float(payload.get("live_cap_seconds", 0.0)))
    else:
        delta = 0.0
    total_sec = float(payload.get("base_total_seconds", payload.get("total_seconds", 0))) + delta
    human_str = format_duration(total_sec, include_seconds=False)
    hms_str = format_hms(total_sec)

    dialogs_changed = False
    now_ts = now_loc.timestamp()
    for d in payload.get("dialogs", ()):
        is_run = bool(d.get("is_running"))
        self_run = bool(d.get("self_running", True))
        if is_run and self_run:
            continue
        last_ts = float(d.get("last_active_ts", 0.0))
        if last_ts <= 0.0:
            continue
        idle_sec = max(0, int(round(now_ts - last_ts)))
        new_ago = _format_idle_ago(idle_sec)
        n_sub = int(d.get("active_subtasks", 0) or 0)
        expected_idle = f"子任务 {n_sub} · {new_ago}" if (is_run and not self_run and n_sub > 0) else new_ago
        new_state = _expected_idle_state(d, idle_sec)
        if expected_idle == d.get("cache_idle_text") and new_state == d.get("cache_state"):
            continue

        ctx_tok = int(d.get("context_tokens", 0))
        th = d.get("cache_thresholds")
        info = DialogContextInfo(
            conversation_id=str(d.get("conversation_id", "")),
            title=str(d.get("title", "")),
            workspace=str(d.get("workspace", "")),
            model_label=str(d.get("model_label", "Carbon")),
            is_running=is_run,
            input_tokens=ctx_tok,
            cache_read_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            last_active=datetime.fromtimestamp(last_ts, tz=LOCAL_TZ),
            compaction_count=int(d.get("compaction_count", 0)),
            cache_thresholds=tuple(th) if th else None,
            active_subtasks=n_sub,
            self_running=self_run,
            context_limit=int(d.get("safe_limit", 0)),
            work_phase=str(d.get("work_phase") or "idle"),
            project_profile=d.get("project_profile"),
            is_handover_turn=bool(d.get("is_handover_turn")),
            is_subdialog=bool(d.get("is_subdialog")),
            parent_conversation_id=str(d.get("parent_conversation_id", "")),
            parent_title=str(d.get("parent_title", "")),
        )
        c_state, c_status, c_idle, eff_idle = evaluate_implicit_cache_status(info, now_loc)
        adv_key, adv_text, adv_reason, notify_detail = evaluate_dialog_advice_detail(info, c_state, eff_idle)
        sw_kind = classify_switch_kind(info, c_state, eff_idle)
        d["cache_state"] = c_state
        d["cache_status_text"] = c_status
        d["cache_idle_text"] = c_idle
        d["idle_seconds"] = eff_idle
        d["advice_key"] = adv_key
        d["advice_text"] = adv_text
        d["advice_reason"] = adv_reason
        d["notify_detail"] = notify_detail
        d["switch_kind"] = sw_kind
        dialogs_changed = True

    return human_str, hms_str, dialogs_changed
