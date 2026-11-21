"""Daily and weekly report orchestration and UI dictionary serialization."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config.settings import (
    LOCAL_TZ,
    DEFAULT_ANTIGRAVITY_DIR,
    DEFAULT_GAP_LIMIT,
    MIN_VISIBLE_DATE,
)
from src.core.advisor import (
    classify_switch_kind,
    collect_dialog_contexts,
    dialog_safe_context_limit,
    evaluate_dialog_advice_detail,
    evaluate_implicit_cache_status,
)
from src.core.live_state import live_report_fields
from src.core.models import (
    ConversationWork,
    DayReport,
    DialogContextInfo,
    ModelQuotaUsage,
    ProjectWork,
    QuotaConfig,
    QuotaSummary,
    TimeInterval,
    WeekReport,
    local_day_bounds,
    local_week_bounds,
    format_compact_hm,
    format_duration,
    format_hms,
    format_tokens,
)
from src.core.ledger import sync_telemetry_ledger
from src.core.quota import compute_quota_summary
from src.core.sessionizer import (
    build_day_from_sessions,
    build_period_work,
    collect_root_sessions,
)


def compute_day_report(
    target_date: date | None = None,
    antigravity_dir: Path = DEFAULT_ANTIGRAVITY_DIR,
    now: datetime | None = None,
    gap_limit: timedelta = DEFAULT_GAP_LIMIT,
    quota_config: QuotaConfig | None = None,
    ledger_path: Path | None = None,
) -> DayReport:
    """Compute Beijing-time work report for `target_date` from local Antigravity 2.0 logs."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_bj = now_utc.astimezone(LOCAL_TZ)
    if target_date is None:
        target_date = now_bj.date()

    day_start, day_end = local_day_bounds(target_date)
    effective_now = min(now_bj, day_end)
    meta_map = sync_telemetry_ledger(antigravity_dir, ledger_path=ledger_path)
    sessions = collect_root_sessions(
        antigravity_dir=antigravity_dir,
        min_mtime_ts=day_start.timestamp() - 3600.0,
        effective_now=effective_now,
        gap_limit=gap_limit,
        meta_map=meta_map,
        ledger_path=ledger_path,
    )
    rep = build_day_from_sessions(target_date, now_bj, sessions, gap_limit=gap_limit)
    quota = compute_quota_summary(
        antigravity_dir=antigravity_dir,
        win_start=day_start,
        win_end=effective_now,
        quota_config=quota_config,
        meta_map=meta_map,
        intervals=rep.merged_intervals,
        ledger_path=ledger_path,
    )
    dialogs = collect_dialog_contexts(
        antigravity_dir=antigravity_dir,
        win_start=day_start,
        win_end=effective_now,
        is_active_period=(target_date == now_bj.date()),
        meta_map=meta_map,
        valid_conv_ids={c.conversation_id for c in rep.conversations},
    )
    return DayReport(
        target_date=rep.target_date,
        generated_at=rep.generated_at,
        total_seconds=rep.total_seconds,
        merged_intervals=rep.merged_intervals,
        conversations=rep.conversations,
        projects=rep.projects,
        running_count=rep.running_count,
        quota=quota,
        dialogs=dialogs,
    )


def compute_week_report(
    target_date: date | None = None,
    antigravity_dir: Path = DEFAULT_ANTIGRAVITY_DIR,
    now: datetime | None = None,
    gap_limit: timedelta = DEFAULT_GAP_LIMIT,
    quota_config: QuotaConfig | None = None,
    ledger_path: Path | None = None,
) -> WeekReport:
    """Compute Monday-to-Sunday Beijing-time weekly work report in a single pass."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_bj = now_utc.astimezone(LOCAL_TZ)
    if target_date is None:
        target_date = now_bj.date()

    week_start, week_end = local_week_bounds(target_date)
    win_start, _ = local_day_bounds(week_start)
    _, win_end = local_day_bounds(week_end)
    effective_now = min(now_bj, win_end)
    meta_map = sync_telemetry_ledger(antigravity_dir, ledger_path=ledger_path)

    sessions = collect_root_sessions(
        antigravity_dir=antigravity_dir,
        min_mtime_ts=win_start.timestamp() - 3600.0,
        effective_now=effective_now,
        gap_limit=gap_limit,
        meta_map=meta_map,
        ledger_path=ledger_path,
    )

    days: list[DayReport] = [
        build_day_from_sessions(week_start + timedelta(days=offset), now_bj, sessions, gap_limit=gap_limit)
        for offset in range(7)
    ]

    is_cur_wk = week_start <= now_bj.date() <= week_end
    _, week_convs, week_projects, running_count = build_period_work(
        win_start,
        win_end,
        is_cur_wk,
        sessions,
        gap_limit=gap_limit,
    )
    total_seconds = sum(d.total_seconds for d in days)
    quota = compute_quota_summary(
        antigravity_dir=antigravity_dir,
        win_start=win_start,
        win_end=effective_now,
        quota_config=quota_config,
        meta_map=meta_map,
        intervals=[TimeInterval(*local_day_bounds(d.target_date)) for d in days],
        ledger_path=ledger_path,
    )
    dialogs = collect_dialog_contexts(
        antigravity_dir=antigravity_dir,
        win_start=win_start,
        win_end=effective_now,
        is_active_period=is_cur_wk,
        meta_map=meta_map,
        valid_conv_ids={c.conversation_id for c in week_convs},
    )
    return WeekReport(
        week_start=week_start,
        week_end=week_end,
        generated_at=now_bj,
        total_seconds=total_seconds,
        days=tuple(days),
        conversations=week_convs,
        projects=week_projects,
        running_count=running_count,
        quota=quota,
        dialogs=dialogs,
    )


def _model_quota_dict(usage: ModelQuotaUsage | None) -> dict:
    if usage is None:
        return {
            "daily_pct": 0.0, "weekly_pct": 0.0, "weighted_usd": 0.0, "tokens": 0,
            "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
            "input_fmt": "0", "cache_fmt": "0", "output_fmt": "0", "thinking_fmt": "0", "thinking_pct": 0,
        }
    return {
        "daily_pct": round(usage.daily_pct, 1), "weekly_pct": round(usage.weekly_pct, 1), "weighted_usd": round(usage.weighted_usd, 6),
        "tokens": usage.input_tokens + usage.cache_read_tokens + usage.output_tokens,
        "input_tokens": usage.input_tokens, "cache_read_tokens": usage.cache_read_tokens,
        "output_tokens": usage.output_tokens, "thinking_tokens": usage.thinking_tokens,
        "input_fmt": format_tokens(usage.input_tokens), "cache_fmt": format_tokens(usage.cache_read_tokens),
        "output_fmt": format_tokens(usage.output_tokens), "thinking_fmt": format_tokens(usage.thinking_tokens),
        "thinking_pct": round(usage.thinking_tokens * 100 / usage.output_tokens) if usage.output_tokens > 0 else 0,
    }


def _serialize_quota_summary(quota: QuotaSummary | None) -> dict:
    if quota is None:
        return {
            "calibrated": False, "carbon_multiplier": 3.0, "weekly_cap_usd": 0.0, "daily_cap_usd": 0.0,
            "daily_pct": 0.0, "weekly_pct": 0.0, "carbon_daily_pct": 0.0, "carbon_weekly_pct": 0.0,
            "neon_daily_pct": 0.0, "neon_weekly_pct": 0.0,
            "hydrogen_daily_pct": 0.0, "hydrogen_weekly_pct": 0.0, "other_daily_pct": 0.0, "other_weekly_pct": 0.0,
            "cycle_used_pct": 0.0, "cycle_left_pct": 100.0, "cycle_reset_str": "",
            "five_hour_used_pct": 0.0, "five_hour_left_pct": 100.0, "five_hour_reset_str": "",
            "carbon_input_fmt": "0", "carbon_cache_fmt": "0", "carbon_output_fmt": "0", "carbon_thinking_pct": 0,
            "neon_input_fmt": "0", "neon_cache_fmt": "0", "neon_output_fmt": "0", "neon_thinking_pct": 0,
            "hydrogen_input_fmt": "0", "hydrogen_cache_fmt": "0", "hydrogen_output_fmt": "0", "hydrogen_thinking_pct": 0,
            "total_input_fmt": "0", "total_cache_fmt": "0", "total_output_fmt": "0",
        }
    ws_vals = quota.by_workspace.values()
    sums = {
        k: (
            sum(getattr(pq, k).input_tokens for pq in ws_vals),
            sum(getattr(pq, k).cache_read_tokens for pq in ws_vals),
            sum(getattr(pq, k).output_tokens for pq in ws_vals),
            sum(getattr(pq, k).thinking_tokens for pq in ws_vals),
        )
        for k in ("carbon", "neon", "hydrogen", "other")
    }
    res: dict[str, object] = {
        "calibrated": quota.calibrated,
        "carbon_multiplier": round(quota.carbon_multiplier, 1),
        "weekly_cap_usd": round(quota.weekly_cap_usd, 1),
        "daily_cap_usd": round(quota.daily_cap_usd, 1),
        "daily_pct": round(quota.total_daily_pct, 1),
        "weekly_pct": round(quota.total_weekly_pct, 1),
        "carbon_daily_pct": round(quota.carbon_daily_pct, 1),
        "carbon_weekly_pct": round(quota.carbon_weekly_pct, 1),
        "neon_daily_pct": round(quota.neon_daily_pct, 1),
        "neon_weekly_pct": round(quota.neon_weekly_pct, 1),
        "hydrogen_daily_pct": round(quota.hydrogen_daily_pct, 1),
        "hydrogen_weekly_pct": round(quota.hydrogen_weekly_pct, 1),
        "other_daily_pct": round(quota.other_daily_pct, 1),
        "other_weekly_pct": round(quota.other_weekly_pct, 1),
        "cycle_used_pct": round(quota.cycle_used_pct, 1),
        "cycle_left_pct": round(quota.cycle_left_pct, 1),
        "cycle_reset_str": quota.cycle_reset_str,
        "five_hour_used_pct": round(quota.five_hour_used_pct, 1),
        "five_hour_left_pct": round(quota.five_hour_left_pct, 1),
        "five_hour_reset_str": quota.five_hour_reset_str,
        "total_input_fmt": format_tokens(sum(v[0] for v in sums.values())),
        "total_cache_fmt": format_tokens(sum(v[1] for v in sums.values())),
        "total_output_fmt": format_tokens(sum(v[2] for v in sums.values())),
    }
    for k in ("carbon", "neon", "hydrogen"):
        m_in, m_ca, m_out, m_th = sums[k]
        res[f"{k}_input_fmt"] = format_tokens(m_in)
        res[f"{k}_cache_fmt"] = format_tokens(m_ca)
        res[f"{k}_output_fmt"] = format_tokens(m_out)
        res[f"{k}_thinking_pct"] = round(m_th * 100 / m_out) if m_out > 0 else 0
    return res


def serialize_dialogs(
    dialogs: tuple[DialogContextInfo, ...],
    last_active_fmt: str,
    now: datetime | None = None,
) -> list[dict]:
    """Serialize `DialogContextInfo` items into UI-ready dictionaries."""
    now_bj = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    out: list[dict] = []
    for d in dialogs:
        ctx = d.context_tokens
        safe_limit = dialog_safe_context_limit(d)
        cache_state, cache_status_text, cache_idle_text, idle_sec = evaluate_implicit_cache_status(d, now_bj)
        advice_key, advice_text, advice_reason, notify_detail = evaluate_dialog_advice_detail(d, cache_state, idle_sec)
        switch_kind = classify_switch_kind(d, cache_state, idle_sec) if advice_key == "switch" else ""
        compaction_text = f"已压缩 {d.compaction_count} 次" if d.compaction_count > 0 else "未触发压缩"
        out.append(
            {
                "conversation_id": d.conversation_id,
                "title": d.title,
                "workspace": d.workspace,
                "model_label": d.model_label,
                "is_running": d.is_running,
                "self_running": d.self_running,
                "active_subtasks": d.active_subtasks,
                "context_tokens": ctx,
                "context_fmt": format_tokens(ctx),
                "compact_limit": safe_limit,
                "compact_pct": round(ctx * 100.0 / safe_limit) if safe_limit > 0 else 0,
                "safe_limit": safe_limit,
                "safe_limit_fmt": format_tokens(safe_limit),
                "safe_pct": round(ctx * 100.0 / safe_limit) if safe_limit > 0 else 0,
                "compaction_count": d.compaction_count,
                "compaction_text": compaction_text,
                "advice_reason": advice_reason,
                "notify_detail": notify_detail,
                "switch_kind": switch_kind,
                "input_fmt": format_tokens(d.input_tokens),
                "cache_fmt": format_tokens(d.cache_read_tokens),
                "output_fmt": format_tokens(d.output_tokens),
                "cache_hit_pct": round(d.cache_hit_pct, 1),
                "cum_cache_hit_pct": round(d.cum_cache_hit_pct, 1),
                "cache_state": cache_state,
                "cache_status_text": cache_status_text,
                "cache_idle_text": cache_idle_text,
                "idle_seconds": idle_sec,
                "advice_key": advice_key,
                "advice_text": advice_text,
                "last_active": d.last_active.strftime(last_active_fmt) if d.last_active else "-",
                "last_active_ts": d.last_active.timestamp() if d.last_active else 0.0,
                "user_turn_count": d.user_turn_count,
                "post_finish_turn_count": d.post_finish_turn_count,
                "last_user_input_ts": d.last_user_input_ts,
                "cache_thresholds": d.cache_thresholds,
            }
        )
    return out


def _serialize_projects_with_quota(
    raw_projects: tuple[ProjectWork, ...],
    quota: QuotaSummary | None,
    is_active_period: bool,
    last_active_fmt: str,
) -> list[dict]:
    hidden_ws = ("未绑定工作区", "未知工作区")
    visible_projects = [p for p in raw_projects if p.workspace not in hidden_ws]
    seen_ws = {p.workspace for p in visible_projects}
    extra_projects: list[ProjectWork] = []
    if quota is not None:
        for ws, pq in quota.by_workspace.items():
            if ws not in hidden_ws and ws not in seen_ws and pq.total_weighted_usd > 0.0:
                extra_projects.append(ProjectWork(workspace=ws, is_running=False, conversation_count=0, intervals=(), total_seconds=0.0, last_active=None))

    def _proj_usd(ws: str) -> float:
        pq_item = quota.by_workspace.get(ws) if quota is not None else None
        return pq_item.total_weighted_usd if pq_item is not None else 0.0

    all_visible = sorted(visible_projects + extra_projects, key=lambda p: (_proj_usd(p.workspace), p.total_seconds, p.last_active.timestamp() if p.last_active else 0.0), reverse=True)
    max_proj_usd = max((_proj_usd(p.workspace) for p in all_visible), default=0.0)
    max_proj_sec = max((p.total_seconds for p in all_visible), default=1.0) or 1.0
    projects_data = []
    for proj in all_visible:
        pq = quota.by_workspace.get(proj.workspace) if quota is not None else None
        p_in = (pq.carbon.input_tokens + pq.neon.input_tokens + pq.hydrogen.input_tokens + pq.other.input_tokens) if pq is not None else 0
        p_cache = (pq.carbon.cache_read_tokens + pq.neon.cache_read_tokens + pq.hydrogen.cache_read_tokens + pq.other.cache_read_tokens) if pq is not None else 0
        p_out = (pq.carbon.output_tokens + pq.neon.output_tokens + pq.hydrogen.output_tokens + pq.other.output_tokens) if pq is not None else 0
        proj_usd = pq.total_weighted_usd if pq is not None else 0.0
        bar_pct = round((proj_usd / max_proj_usd) * 100.0, 1) if max_proj_usd > 0.0 else (round((proj.total_seconds / max_proj_sec) * 100.0, 1) if proj.total_seconds > 0 else 0.0)
        projects_data.append({
            "workspace": proj.workspace,
            "is_running": bool(is_active_period and proj.is_running),
            "conversation_count": proj.conversation_count,
            "duration": format_duration(proj.total_seconds, include_seconds=True),
            "duration_short": format_duration(proj.total_seconds, include_seconds=False),
            "last_active": proj.last_active.strftime(last_active_fmt) if proj.last_active else "-",
            "bar_pct": bar_pct,
            "daily_quota_pct": round(pq.total_daily_pct, 1) if pq is not None else 0.0,
            "weekly_quota_pct": round(pq.total_weekly_pct, 1) if pq is not None else 0.0,
            "total_input_fmt": format_tokens(p_in),
            "total_cache_fmt": format_tokens(p_cache),
            "total_output_fmt": format_tokens(p_out),
            "carbon": _model_quota_dict(pq.carbon if pq is not None else None),
            "neon": _model_quota_dict(pq.neon if pq is not None else None),
            "hydrogen": _model_quota_dict(pq.hydrogen if pq is not None else None),
            "other": _model_quota_dict(pq.other if pq is not None else None),
        })
    return projects_data


def week_report_to_dict(report: WeekReport, now: datetime | None = None) -> dict:
    """Serialize a `WeekReport` into JSON-friendly structure for the desktop UI."""
    now_bj = (now or report.generated_at).astimezone(LOCAL_TZ)
    today_bj = now_bj.date()
    is_current_week = report.week_start <= today_bj <= report.week_end
    max_day_sec = max((d.total_seconds for d in report.days), default=1.0) or 1.0

    iq_list = report.quota.interval_quotas if report.quota is not None else ()
    days_data = []
    active_days = 0
    for idx, d in enumerate(report.days):
        if d.total_seconds > 0:
            active_days += 1
        weekday_cn = "一二三四五六日"[d.target_date.weekday()]
        is_future = d.target_date > today_bj
        iq = iq_list[idx] if (not is_future and idx < len(iq_list)) else None
        days_data.append(
            {
                "date": d.target_date.isoformat(),
                "short_date": d.target_date.strftime("%m-%d"),
                "weekday_cn": f"周{weekday_cn}",
                "is_today": d.target_date == today_bj,
                "is_future": is_future,
                "total_seconds": int(round(d.total_seconds)),
                "total_human": format_duration(d.total_seconds, include_seconds=False),
                "total_detail": format_duration(d.total_seconds, include_seconds=True),
                "compact_hm": format_compact_hm(d.total_seconds),
                "bar_pct": round((d.total_seconds / max_day_sec) * 100.0, 1),
                "interval_count": len(d.merged_intervals),
                "conversation_count": len(d.conversations),
                "weekly_quota_pct": round(iq.total_weekly_pct, 1) if iq else 0.0,
                "carbon_weekly_pct": round(iq.carbon_weekly_pct, 1) if iq else 0.0,
                "neon_weekly_pct": round(iq.neon_weekly_pct, 1) if iq else 0.0,
                "hydrogen_weekly_pct": round(iq.hydrogen_weekly_pct, 1) if iq else 0.0,
                "other_weekly_pct": round(iq.other_weekly_pct, 1) if iq else 0.0,
            }
        )

    max_conv_sec = max((c.total_seconds for c in report.conversations), default=1.0) or 1.0
    convs_data = [{
        "conversation_id": c.conversation_id, "workspace": c.workspace, "title": c.title,
        "is_running": bool(is_current_week and c.is_running),
        "duration": format_duration(c.total_seconds, include_seconds=True),
        "last_active": c.last_active.strftime("%m-%d %H:%M") if c.last_active else "-",
        "bar_pct": round((c.total_seconds / max_conv_sec) * 100.0, 1),
    } for c in report.conversations]
    projects_data = _serialize_projects_with_quota(report.projects, report.quota, is_current_week, "%m-%d %H:%M")
    avg_seconds = report.total_seconds / max(1, active_days) if active_days > 0 else 0.0
    _, week_end_dt = local_day_bounds(report.week_end)
    dialog_ref_now = now_bj if is_current_week else min(now_bj, week_end_dt)

    today_ivs = next((d.merged_intervals for d in report.days if d.target_date == today_bj), ())
    _, today_end = local_day_bounds(today_bj)
    return {
        "week_start": report.week_start.isoformat(),
        "week_end": report.week_end.isoformat(),
        "range_label": f"{report.week_start.strftime('%m-%d')} 周一 ~ {report.week_end.strftime('%m-%d')} 周日",
        "is_current_week": is_current_week,
        "can_prev": True if MIN_VISIBLE_DATE is None else (report.week_start > MIN_VISIBLE_DATE),
        "can_next": report.week_end < today_bj,
        "total_human": format_duration(report.total_seconds, include_seconds=False),
        "total_hms": format_hms(report.total_seconds),
        "total_seconds": int(round(report.total_seconds)),
        **live_report_fields(report.total_seconds, is_current_week, report.running_count, today_ivs, report.dialogs, now_bj, today_end),
        "active_days": active_days,
        "avg_human": format_duration(avg_seconds, include_seconds=False),
        "running_count": report.running_count if is_current_week else 0,
        "conversation_count": len(convs_data),
        "project_count": len(projects_data),
        "days": days_data,
        "conversations": convs_data,
        "projects": projects_data,
        "dialogs": serialize_dialogs(report.dialogs, last_active_fmt="%m-%d %H:%M", now=dialog_ref_now),
        "quota": _serialize_quota_summary(report.quota),
        "updated_at": report.generated_at.strftime("%H:%M:%S"),
    }


def report_to_dict(report: DayReport, now: datetime | None = None) -> dict:
    """Serialize a `DayReport` into JSON-friendly structure for the desktop UI."""
    now_bj = (now or report.generated_at).astimezone(LOCAL_TZ)
    is_today = report.target_date == now_bj.date()
    day_start, day_end = local_day_bounds(report.target_date)
    dialog_ref_now = now_bj if is_today else min(now_bj, day_end)
    day_seconds = 86400.0

    timeline_segments = []
    intervals_data = []
    iq_list = report.quota.interval_quotas if report.quota is not None else ()
    for idx, iv in enumerate(report.merged_intervals, start=1):
        start_off = max(0.0, (iv.start - day_start).total_seconds())
        dur_sec = max(0.0, iv.seconds)
        start_pct = round((start_off / day_seconds) * 100.0, 4)
        width_pct = round(max(0.35, (dur_sec / day_seconds) * 100.0), 4)
        if start_pct + width_pct > 100.0:
            width_pct = round(100.0 - start_pct, 4)

        start_str = iv.start.strftime("%H:%M:%S")
        end_str = iv.end.strftime("%H:%M:%S")
        dur_str = format_duration(dur_sec, include_seconds=True)
        iq = iq_list[idx - 1] if idx - 1 < len(iq_list) else None
        timeline_segments.append(
            {"index": idx, "start_pct": start_pct, "width_pct": width_pct, "start": start_str, "end": end_str, "duration": dur_str}
        )
        intervals_data.append({
            "index": idx, "start": start_str, "end": end_str, "duration": dur_str, "seconds": int(round(dur_sec)),
            "weekly_quota_pct": round(iq.total_weekly_pct, 1) if iq else 0.0,
            "carbon_weekly_pct": round(iq.carbon_weekly_pct, 1) if iq else 0.0,
            "neon_weekly_pct": round(iq.neon_weekly_pct, 1) if iq else 0.0,
            "hydrogen_weekly_pct": round(iq.hydrogen_weekly_pct, 1) if iq else 0.0,
            "other_weekly_pct": round(iq.other_weekly_pct, 1) if iq else 0.0,
        })

    max_conv_sec = max((c.total_seconds for c in report.conversations), default=1.0) or 1.0
    conversations_data = [
        {
            "conversation_id": conv.conversation_id,
            "workspace": conv.workspace,
            "title": conv.title,
            "is_running": bool(is_today and conv.is_running),
            "duration": format_duration(conv.total_seconds, include_seconds=True),
            "duration_short": format_duration(conv.total_seconds, include_seconds=False),
            "last_active": conv.last_active.strftime("%H:%M:%S") if conv.last_active else "-",
            "bar_pct": round((conv.total_seconds / max_conv_sec) * 100.0, 1),
        }
        for conv in report.conversations
    ]
    projects_data = _serialize_projects_with_quota(report.projects, report.quota, is_today, "%H:%M:%S")
    active_span = (
        f"{report.merged_intervals[0].start.strftime('%H:%M:%S')} – {report.merged_intervals[-1].end.strftime('%H:%M:%S')}"
        if report.merged_intervals
        else "暂无记录"
    )
    weekday_cn = "一二三四五六日"[report.target_date.weekday()]

    return {
        "target_date": report.target_date.isoformat(),
        "weekday_cn": f"周{weekday_cn}",
        "is_today": is_today,
        "can_prev": True if MIN_VISIBLE_DATE is None else (report.target_date > MIN_VISIBLE_DATE),
        "can_next": report.target_date < now_bj.date(),
        "total_human": format_duration(report.total_seconds, include_seconds=False),
        "total_hms": format_hms(report.total_seconds),
        "total_seconds": int(round(report.total_seconds)),
        **live_report_fields(report.total_seconds, is_today, report.running_count, report.merged_intervals, report.dialogs, now_bj, day_end),
        "running_count": report.running_count if is_today else 0,
        "active_span": active_span,
        "interval_count": len(intervals_data),
        "conversation_count": len(conversations_data),
        "project_count": len(projects_data),
        "timeline_segments": timeline_segments,
        "intervals": intervals_data,
        "conversations": conversations_data,
        "projects": projects_data,
        "dialogs": serialize_dialogs(report.dialogs, last_active_fmt="%H:%M:%S", now=dialog_ref_now),
        "quota": _serialize_quota_summary(report.quota),
        "updated_at": report.generated_at.strftime("%H:%M:%S"),
    }
