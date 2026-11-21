"""Beijing-time user-message sessionization (30m idle / 60m active-task) and project aggregation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from config.settings import ACTIVE_TASK_GAP_LIMIT, LOCAL_TZ, DEFAULT_GAP_LIMIT, RUNNING_STATUS
from src.core.models import (
    ConversationMeta,
    ConversationWork,
    DayReport,
    ProjectWork,
    TimeInterval,
    local_day_bounds,
)
from src.core.ledger import load_archived_transcripts, resolve_ledger_path, sync_telemetry_ledger
from src.core.storage import (
    collect_active_subtask_counts,
    load_db_metadata,
    read_transcript_events,
    resolve_root_id,
)


@dataclass(frozen=True)
class RootSession:
    """User-message timestamps, task execution spans, and single-dialog intervals for a root conversation."""

    root_id: str
    meta: ConversationMeta | None
    is_running: bool
    user_timestamps: tuple[datetime, ...]
    task_spans: tuple[TimeInterval, ...]
    intervals: tuple[TimeInterval, ...]


def sessionize_timestamps(
    timestamps: Iterable[datetime],
    gap_limit: timedelta = DEFAULT_GAP_LIMIT,
    is_running: bool = False,
    now: datetime | None = None,
) -> list[TimeInterval]:
    """Connect consecutive timestamps with gap <= `gap_limit` into active intervals."""
    ordered = sorted(dt.astimezone(LOCAL_TZ) for dt in timestamps)
    if not ordered:
        return []

    segments: list[TimeInterval] = []
    seg_start = ordered[0]
    seg_end = ordered[0]

    for ts in ordered[1:]:
        if ts - seg_end <= gap_limit:
            seg_end = ts
        else:
            if seg_end > seg_start:
                segments.append(TimeInterval(seg_start, seg_end))
            seg_start = ts
            seg_end = ts

    if is_running and now is not None:
        now_bj = now.astimezone(LOCAL_TZ)
        if now_bj > seg_end:
            seg_end = min(now_bj, seg_end + gap_limit)

    if seg_end > seg_start:
        segments.append(TimeInterval(seg_start, seg_end))
    return segments


def extract_user_and_task_spans(
    events: Iterable[tuple[datetime, bool]],
    is_running: bool = False,
    now: datetime | None = None,
    active_task_gap: timedelta = ACTIVE_TASK_GAP_LIMIT,
) -> tuple[tuple[datetime, ...], tuple[TimeInterval, ...]]:
    """Extract sorted Beijing-time `user_timestamps` and `[user_ts, turn_exec_end]` spans."""
    ordered = sorted(
        ((dt.astimezone(LOCAL_TZ), bool(is_user)) for dt, is_user in events),
        key=lambda item: (item[0], not item[1]),
    )
    if not ordered:
        return (), ()

    user_ts: list[datetime] = []
    spans: list[TimeInterval] = []
    cur_u: datetime | None = None
    cur_end: datetime | None = None

    for ts, is_user in ordered:
        if is_user:
            if cur_u is not None and cur_end is not None and cur_end > cur_u:
                spans.append(TimeInterval(cur_u, cur_end))
            cur_u = ts
            cur_end = ts
            user_ts.append(ts)
        elif cur_u is not None and cur_end is not None and (ts - cur_end) <= active_task_gap:
            if ts > cur_end:
                cur_end = ts

    if cur_u is not None and cur_end is not None:
        if is_running and now is not None:
            now_bj = now.astimezone(LOCAL_TZ)
            if now_bj > cur_end and (now_bj - cur_end) <= active_task_gap:
                cur_end = min(now_bj, cur_u + active_task_gap)
        if cur_end > cur_u:
            spans.append(TimeInterval(cur_u, cur_end))

    return tuple(user_ts), tuple(spans)


def sessionize_user_messages(
    user_timestamps: Iterable[datetime],
    task_spans: Iterable[TimeInterval] = (),
    gap_limit: timedelta = DEFAULT_GAP_LIMIT,
    active_task_gap: timedelta = ACTIVE_TASK_GAP_LIMIT,
) -> list[TimeInterval]:
    """Connect user message timestamps (`<= 30m` idle, or `<= 60m` when an active task leaves `<= 30m` idle)."""
    ordered = sorted(dt.astimezone(LOCAL_TZ) for dt in user_timestamps)
    if len(ordered) < 2:
        return []

    norm_spans = tuple(
        TimeInterval(sp.start.astimezone(LOCAL_TZ), sp.end.astimezone(LOCAL_TZ))
        for sp in task_spans
        if sp.end > sp.start
    )

    def _can_bridge(t1: datetime, t2: datetime) -> bool:
        delta = t2 - t1
        if delta <= gap_limit:
            return True
        if delta > active_task_gap:
            return False
        latest_task_end = t1
        for sp in norm_spans:
            if sp.start <= t1 < sp.end:
                eff_end = min(sp.end, t2)
                if eff_end > latest_task_end:
                    latest_task_end = eff_end
        return (t2 - latest_task_end) <= gap_limit

    segments: list[TimeInterval] = []
    seg_start = ordered[0]
    seg_end = ordered[0]

    for ts in ordered[1:]:
        if _can_bridge(seg_end, ts):
            seg_end = ts
        else:
            if seg_end > seg_start:
                segments.append(TimeInterval(seg_start, seg_end))
            seg_start = ts
            seg_end = ts

    if seg_end > seg_start:
        segments.append(TimeInterval(seg_start, seg_end))
    return segments


def sessionize_events(
    events: Iterable[tuple[datetime, bool]],
    gap_limit: timedelta = DEFAULT_GAP_LIMIT,
    is_running: bool = False,
    now: datetime | None = None,
    active_task_gap: timedelta = ACTIVE_TASK_GAP_LIMIT,
) -> list[TimeInterval]:
    """Connect `(timestamp, is_user_input)` events into strictly user-message-bounded intervals."""
    user_ts, spans = extract_user_and_task_spans(
        events,
        is_running=is_running,
        now=now,
        active_task_gap=active_task_gap,
    )
    return sessionize_user_messages(
        user_ts,
        spans,
        gap_limit=gap_limit,
        active_task_gap=active_task_gap,
    )


def clip_intervals(
    intervals: Iterable[TimeInterval],
    window_start: datetime,
    window_end: datetime,
) -> list[TimeInterval]:
    """Clip intervals to `[window_start, window_end)`."""
    clipped: list[TimeInterval] = []
    for iv in intervals:
        s = max(iv.start, window_start)
        e = min(iv.end, window_end)
        if e > s:
            clipped.append(TimeInterval(s, e))
    return clipped


def merge_intervals(
    intervals: Iterable[TimeInterval],
    gap: timedelta = timedelta(0),
) -> list[TimeInterval]:
    """Return sorted union of overlapping, touching, or `<= gap` separated intervals."""
    ordered = sorted((iv for iv in intervals if iv.end > iv.start), key=lambda x: (x.start, x.end))
    if not ordered:
        return []
    merged: list[TimeInterval] = [ordered[0]]
    for iv in ordered[1:]:
        prev = merged[-1]
        if iv.start - prev.end <= gap:
            merged[-1] = TimeInterval(prev.start, max(prev.end, iv.end))
        else:
            merged.append(iv)
    return merged


def collect_root_sessions(
    antigravity_dir: Path,
    min_mtime_ts: float,
    effective_now: datetime,
    gap_limit: timedelta,
    meta_map: dict[str, ConversationMeta] | None = None,
    active_task_gap: timedelta = ACTIVE_TASK_GAP_LIMIT,
    ledger_path: Path | None = None,
) -> list[RootSession]:
    """Load and sessionize root user conversations (live transcripts + archived ledger entries)."""
    brain_dir = antigravity_dir / "brain"
    eff_ledger = resolve_ledger_path(antigravity_dir, ledger_path)
    if meta_map is None:
        meta_map = (
            sync_telemetry_ledger(antigravity_dir, ledger_path=eff_ledger)
            if eff_ledger is not None
            else load_db_metadata(antigravity_dir / "conversation_summaries.db")
        )

    subtask_counts = collect_active_subtask_counts(antigravity_dir, meta_map, effective_now)
    root_events: dict[str, list[tuple[datetime, bool]]] = {}
    root_has_user: dict[str, bool] = {}
    live_brain_cids: set[str] = set()

    if brain_dir.exists():
        for entry in brain_dir.iterdir():
            if not entry.is_dir():
                continue
            transcript_path = entry / ".system_generated" / "logs" / "transcript.jsonl"
            if not transcript_path.is_file():
                continue
            live_brain_cids.add(entry.name)
            try:
                st = transcript_path.stat()
                if st.st_mtime < min_mtime_ts and resolve_root_id(entry.name, meta_map) not in subtask_counts:
                    continue
            except OSError:
                continue

            cid = entry.name
            events, has_user = read_transcript_events(transcript_path, st)
            if not events:
                continue

            root_id = resolve_root_id(cid, meta_map)
            root_events.setdefault(root_id, []).extend(events)
            if has_user:
                root_has_user[root_id] = True

    if eff_ledger is not None and eff_ledger.is_file():
        for cid, events, has_user in load_archived_transcripts(eff_ledger, live_brain_cids, min_mtime_ts):
            root_id = resolve_root_id(cid, meta_map)
            root_events.setdefault(root_id, []).extend(events)
            if has_user:
                root_has_user[root_id] = True

    results: list[RootSession] = []
    eff_task_gap = max(gap_limit, active_task_gap)
    for root_id, events in root_events.items():
        meta = meta_map.get(root_id)
        has_user_in_db = meta is not None and meta.last_user_input_time is not None
        if not root_has_user.get(root_id, False) and not has_user_in_db:
            continue
        is_running = (meta is not None and meta.status == RUNNING_STATUS) or (subtask_counts.get(root_id, 0) > 0)
        user_ts, spans = extract_user_and_task_spans(
            events,
            is_running=is_running,
            now=effective_now,
            active_task_gap=eff_task_gap,
        )
        if not user_ts:
            continue
        segments = tuple(
            sessionize_user_messages(
                user_ts,
                spans,
                gap_limit=gap_limit,
                active_task_gap=eff_task_gap,
            )
        )
        results.append(
            RootSession(
                root_id=root_id,
                meta=meta,
                is_running=is_running,
                user_timestamps=user_ts,
                task_spans=spans,
                intervals=segments,
            )
        )
    return results


def aggregate_projects(
    conversations: Iterable[ConversationWork],
    fallback_time: datetime,
    gap_limit: timedelta = DEFAULT_GAP_LIMIT,
) -> tuple[ProjectWork, ...]:
    """Group conversations by workspace and union-merge each project's intervals."""
    grouped: dict[str, list[ConversationWork]] = {}
    for conv in conversations:
        grouped.setdefault(conv.workspace, []).append(conv)

    projects: list[ProjectWork] = []
    for workspace, convs in grouped.items():
        raw_ivs = [iv for c in convs for iv in c.intervals]
        merged = tuple(merge_intervals(raw_ivs, gap=gap_limit))
        total_sec = sum(iv.seconds for iv in merged)
        cands = [iv.end for iv in merged] + [c.last_active for c in convs if c.last_active is not None]
        last_active = max(cands) if cands else None
        projects.append(
            ProjectWork(
                workspace=workspace,
                is_running=any(c.is_running for c in convs),
                conversation_count=len(convs),
                intervals=merged,
                total_seconds=total_sec,
                last_active=last_active,
            )
        )
    projects.sort(key=lambda p: (p.total_seconds, p.last_active or fallback_time), reverse=True)
    return tuple(projects)


def build_period_work(
    win_start: datetime,
    win_end: datetime,
    is_active: bool,
    sessions: list[RootSession],
    gap_limit: timedelta = DEFAULT_GAP_LIMIT,
    active_task_gap: timedelta = ACTIVE_TASK_GAP_LIMIT,
) -> tuple[tuple[TimeInterval, ...], tuple[ConversationWork, ...], tuple[ProjectWork, ...], int]:
    """Compute `(merged_intervals, conversations, projects, running_count)` for `[win_start, win_end)`."""
    eff_task_gap = max(gap_limit, active_task_gap)
    conversations: list[ConversationWork] = []
    ws_convs: dict[str, list[ConversationWork]] = {}
    ws_sessions: dict[str, list[RootSession]] = {}
    running_count = 0

    for s in sessions:
        ws = s.meta.workspace if s.meta else "未知工作区"
        ws_sessions.setdefault(ws, []).append(s)
        p_segments = clip_intervals(s.intervals, win_start, win_end)
        p_users = [u for u in s.user_timestamps if win_start <= u < win_end]
        conv_running = bool(is_active and s.is_running)
        if not p_segments and not p_users:
            if conv_running:
                running_count += 1
            continue

        if conv_running:
            running_count += 1
        conv_total = sum(iv.seconds for iv in p_segments)
        cands = [iv.end for iv in p_segments] + ([p_users[-1]] if p_users else [])
        last_active = max(cands) if cands else None
        title = (s.meta.title if s.meta and s.meta.title else "") or f"对话 {s.root_id[:8]}"
        cw = ConversationWork(
            conversation_id=s.root_id,
            title=title,
            workspace=ws,
            is_running=conv_running,
            intervals=tuple(p_segments),
            total_seconds=conv_total,
            last_active=last_active,
        )
        conversations.append(cw)
        ws_convs.setdefault(ws, []).append(cw)

    conversations.sort(key=lambda c: (c.total_seconds, c.last_active or win_start), reverse=True)

    all_u = (u for s in sessions for u in s.user_timestamps)
    all_sp = (sp for s in sessions for sp in s.task_spans)
    global_raw = sessionize_user_messages(all_u, all_sp, gap_limit=gap_limit, active_task_gap=eff_task_gap)
    merged = tuple(clip_intervals(global_raw, win_start, win_end))

    projects: list[ProjectWork] = []
    for ws, s_list in ws_sessions.items():
        convs_in_ws = ws_convs.get(ws, [])
        ws_raw = sessionize_user_messages(
            (u for s in s_list for u in s.user_timestamps),
            (sp for s in s_list for sp in s.task_spans),
            gap_limit=gap_limit,
            active_task_gap=eff_task_gap,
        )
        ws_clipped = tuple(clip_intervals(ws_raw, win_start, win_end))
        if not ws_clipped and not convs_in_ws:
            continue
        total_sec = sum(iv.seconds for iv in ws_clipped)
        cands = [iv.end for iv in ws_clipped] + [c.last_active for c in convs_in_ws if c.last_active is not None]
        last_active = max(cands) if cands else None
        projects.append(
            ProjectWork(
                workspace=ws,
                is_running=any(c.is_running for c in convs_in_ws),
                conversation_count=len(convs_in_ws),
                intervals=ws_clipped,
                total_seconds=total_sec,
                last_active=last_active,
            )
        )
    projects.sort(key=lambda p: (p.total_seconds, p.last_active or win_start), reverse=True)
    return merged, tuple(conversations), tuple(projects), running_count


def build_day_from_sessions(
    target_date: date,
    now_bj: datetime,
    sessions: list[RootSession],
    gap_limit: timedelta = DEFAULT_GAP_LIMIT,
) -> DayReport:
    """Build a single-day `DayReport` from pre-extracted root conversation sessions."""
    day_start, day_end = local_day_bounds(target_date)
    is_today = target_date == now_bj.date()
    merged, conversations, projects, running_count = build_period_work(
        day_start,
        day_end,
        is_today,
        sessions,
        gap_limit=gap_limit,
    )
    return DayReport(
        target_date=target_date,
        generated_at=now_bj,
        total_seconds=sum(iv.seconds for iv in merged),
        merged_intervals=merged,
        conversations=conversations,
        projects=projects,
        running_count=running_count,
    )
