"""SQLite/WAL Protobuf telemetry decoder and transcript/subtask scanner for Antigravity 2.0."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import threading

from config.settings import COMPACTION_LIMIT_TOKENS, RUNNING_STATUS
from src.core.models import ConversationMeta, extract_workspace_name, parse_iso_utc
from src.core.protobuf_decoder import (
    STEP_TYPE_CHECKPOINT,
    STEP_TYPE_PLANNER_RESPONSE,
    STEP_TYPE_USER_INPUT,
    decode_pb_top,
    decode_step_rows,
    extract_db_gen_meta,
)

_CACHE_LOCK = threading.RLock()
_TRANSCRIPT_CACHE: dict[Path, tuple[int, int, list[tuple[datetime, bool]], bool, dict[str, float]]] = {}
_META_CACHE: dict[Path, tuple[tuple[int, int, int, int], dict[str, ConversationMeta]]] = {}
_ANN_FILE_CACHE: dict[Path, tuple[int, int, bool]] = {}
_BG_TASK_START_RE = re.compile(r"Tool is running as a background task with task id:\s*([^\s\\]+)")
_BG_TASK_FINISH_RE = re.compile(r'Task id\s+\\?"([^"\\]+)\\?"\s+finished')


@dataclass
class _DbStepCache:
    sig: tuple[int, int, int, int]
    families: dict[int, str]
    family_limits: dict[str, int]
    gen_meta_sig: tuple[int, int]
    has_step_type: bool
    cutoff_rowid: int
    cutoff_count: int
    cutoff_last_ts: int
    cutoff_prev_stype: int | None
    prefix_events: list[tuple[int, int, str, int, int, int, int]]
    prefix_comp: list[int]
    prefix_user: list[int]
    prefix_post: list[int]
    events: list[tuple[int, int, str, int, int, int, int]]
    comp_tuple: tuple[int, ...]
    user_tuple: tuple[int, ...]
    post_tuple: tuple[int, ...]


_DB_USAGE_CACHE: dict[Path, _DbStepCache] = {}


def load_db_metadata(db_path: Path) -> dict[str, ConversationMeta]:
    """Load all conversation summaries from `conversation_summaries.db` with WAL-aware cache."""
    sig_info = db_stat_signature(db_path)
    if sig_info is None:
        return {}
    _, sig = sig_info
    with _CACHE_LOCK:
        cached = _META_CACHE.get(db_path)
        if cached is not None and cached[0] == sig:
            return cached[1]

    meta_map: dict[str, ConversationMeta] = {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        rows = conn.execute(
            """
            SELECT conversation_id, title, workspace_uris, status,
                   parent_conversation_id, nesting_depth,
                   last_modified_time, last_user_input_time
            FROM conversation_summaries
            """
        ).fetchall()
    except sqlite3.Error:
        with _CACHE_LOCK:
            fallback = _META_CACHE.get(db_path)
            return fallback[1] if fallback is not None else {}
    finally:
        if conn is not None:
            conn.close()

    for cid, title, ws_json, status, parent_id, depth, lmt_raw, luit_raw in rows:
        meta_map[cid] = ConversationMeta(
            conversation_id=cid,
            title=(title or "").strip(),
            workspace=extract_workspace_name(ws_json or ""),
            status=status or "",
            parent_conversation_id=(parent_id or "").strip(),
            nesting_depth=int(depth or 0),
            last_modified_time=parse_iso_utc(lmt_raw),
            last_user_input_time=parse_iso_utc(luit_raw),
        )
    with _CACHE_LOCK:
        _META_CACHE[db_path] = (sig, meta_map)
    return meta_map


def resolve_root_id(cid: str, meta_map: dict[str, ConversationMeta]) -> str:
    """Walk `parent_conversation_id` links safely to find the top-level conversation."""
    curr = cid
    seen: set[str] = set()
    while curr in meta_map:
        if curr in seen:
            break
        seen.add(curr)
        parent = meta_map[curr].parent_conversation_id
        if not parent:
            break
        curr = parent
    return curr


def load_archived_conversation_ids(antigravity_dir: Path) -> set[str]:
    """Return conversation IDs marked `archived:true` in `~/.gemini/antigravity/annotations/*.pbtxt`."""
    ann_dir = antigravity_dir / "annotations"
    archived: set[str] = set()
    if not ann_dir.is_dir():
        return archived
    with os.scandir(ann_dir) as it:
        for entry in it:
            if not entry.name.endswith(".pbtxt"):
                continue
            pbtxt_path = Path(entry.path)
            try:
                st = entry.stat()
            except OSError:
                continue
            with _CACHE_LOCK:
                cached = _ANN_FILE_CACHE.get(pbtxt_path)
            if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                is_arch = cached[2]
            else:
                try:
                    txt = pbtxt_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                is_arch = bool(re.search(r"\barchived\s*:\s*true\b", txt))
                with _CACHE_LOCK:
                    _ANN_FILE_CACHE[pbtxt_path] = (st.st_mtime_ns, st.st_size, is_arch)
            if is_arch:
                archived.add(pbtxt_path.stem)
    return archived


def parse_transcript_details(
    transcript_path: Path,
    stat_result: os.stat_result | None = None,
) -> tuple[list[tuple[datetime, bool]], bool, dict[str, float]]:
    """Read (`(timestamp, is_user_input)` list, has_user_input, pending_non_daemon_bg_tasks) with cache."""
    try:
        st = stat_result or transcript_path.stat()
    except OSError:
        return [], False, {}

    with _CACHE_LOCK:
        cached = _TRANSCRIPT_CACHE.get(transcript_path)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2], cached[3], cached[4]

    events: list[tuple[datetime, bool]] = []
    has_user_input = False
    pending_tasks: dict[str, float] = {}
    last_tool_calls: list[object] = []
    generic_offset = 0

    try:
        with transcript_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stype = item.get("type")
                is_user = item.get("source") == "USER_EXPLICIT" or stype == "USER_INPUT"
                if is_user:
                    has_user_input = True
                ts = parse_iso_utc(item.get("created_at"))
                if ts is not None:
                    events.append((ts, is_user))

                if stype == "PLANNER_RESPONSE":
                    tcs = item.get("tool_calls")
                    last_tool_calls = tcs if isinstance(tcs, list) else []
                    generic_offset = 0
                    for tc in last_tool_calls:
                        if isinstance(tc, dict) and tc.get("name") == "manage_task":
                            t_args = tc.get("args")
                            if isinstance(t_args, dict):
                                act = str(t_args.get("Action", "")).strip('"').lower()
                                tid = str(t_args.get("TaskId", "")).strip('"')
                                if act == "kill" and tid:
                                    pending_tasks.pop(tid, None)
                elif stype == "GENERIC":
                    tc = last_tool_calls[generic_offset] if generic_offset < len(last_tool_calls) else None
                    generic_offset += 1
                    if item.get("status") == "RUNNING":
                        content = str(item.get("content") or "")
                        m = _BG_TASK_START_RE.search(content)
                        if m:
                            is_daemon = False
                            if isinstance(tc, dict) and isinstance(tc.get("args"), dict):
                                raw_d = tc["args"].get("IsDaemon")
                                is_daemon = raw_d is True or (
                                    isinstance(raw_d, str) and raw_d.strip('"').lower() == "true"
                                )
                            if not is_daemon:
                                pending_tasks[m.group(1)] = ts.timestamp() if ts is not None else 0.0

                content_str = item.get("content")
                if isinstance(content_str, str) and "Task id" in content_str and "finished" in content_str:
                    for finished_tid in _BG_TASK_FINISH_RE.findall(content_str):
                        pending_tasks.pop(finished_tid, None)
    except OSError:
        with _CACHE_LOCK:
            fallback = _TRANSCRIPT_CACHE.get(transcript_path)
            if fallback is not None:
                return fallback[2], fallback[3], fallback[4]
        return [], False, {}

    with _CACHE_LOCK:
        _TRANSCRIPT_CACHE[transcript_path] = (st.st_mtime_ns, st.st_size, events, has_user_input, pending_tasks)
    return events, has_user_input, pending_tasks


def read_transcript_events(
    transcript_path: Path,
    stat_result: os.stat_result | None = None,
) -> tuple[list[tuple[datetime, bool]], bool]:
    """Read (`(timestamp, is_user_input)` list, has_user_input) from transcript.jsonl with cache."""
    events, has_user_input, _ = parse_transcript_details(transcript_path, stat_result)
    return events, has_user_input


def collect_active_subtask_counts(
    antigravity_dir: Path,
    meta_map: dict[str, ConversationMeta],
    ref_now: datetime,
) -> dict[str, int]:
    """Count active child subagents and non-daemon async background tasks per root conversation."""
    counts: dict[str, int] = {}
    ref_ts = ref_now.timestamp()

    for cid, m in meta_map.items():
        if m.status == RUNNING_STATUS:
            root_id = resolve_root_id(cid, meta_map)
            if root_id != cid:
                counts[root_id] = counts.get(root_id, 0) + 1

    brain_dir = antigravity_dir / "brain"
    if brain_dir.is_dir():
        min_bg_mtime = ref_ts - 7200.0
        for entry in brain_dir.iterdir():
            if not entry.is_dir():
                continue
            tp = entry / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                st = tp.stat()
            except OSError:
                continue
            if st.st_mtime < min_bg_mtime:
                continue
            _, _, pending_tasks = parse_transcript_details(tp, st)
            if not pending_tasks:
                continue
            root_id = resolve_root_id(entry.name, meta_map)
            active_n = sum(1 for t_start in pending_tasks.values() if -5.0 <= (ref_ts - t_start) <= 7200.0)
            if active_n > 0:
                counts[root_id] = counts.get(root_id, 0) + active_n

    return counts


def db_stat_signature(
    db_path: Path,
    stat_result: os.stat_result | None = None,
) -> tuple[float, tuple[int, int, int, int]] | None:
    """Return `(effective_mtime_sec, (db_mtime_ns, db_size, wal_mtime_ns, wal_size))` accounting for SQLite WAL."""
    try:
        st = stat_result or db_path.stat()
    except OSError:
        return None
    eff_mtime = st.st_mtime
    wal_mtime_ns = 0
    wal_size = 0
    wal_path = db_path.with_name(db_path.name + "-wal")
    try:
        wst = wal_path.stat()
        wal_mtime_ns = wst.st_mtime_ns
        wal_size = wst.st_size
        if wst.st_size > 0 and wst.st_mtime > eff_mtime:
            eff_mtime = wst.st_mtime
    except OSError:
        pass
    return eff_mtime, (st.st_mtime_ns, st.st_size, wal_mtime_ns, wal_size)


def _get_gen_meta_sig(conn: sqlite3.Connection) -> tuple[int, int]:
    try:
        row = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), -1) FROM gen_metadata").fetchone()
        return (int(row[0]), int(row[1])) if row is not None else (0, -1)
    except sqlite3.Error:
        return (-1, -1)


def read_db_dialog_telemetry(
    db_path: Path,
    stat_result: os.stat_result | None = None,
) -> tuple[
    list[tuple[int, int, str, int, int, int, int]],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    """Read Gemini step usage, compaction, user turn, and post-finish turn timestamps (incremental + lock-safe)."""
    sig_info = db_stat_signature(db_path, stat_result)
    if sig_info is None:
        return [], (), (), ()
    _, sig = sig_info

    with _CACHE_LOCK:
        cached = _DB_USAGE_CACHE.get(db_path)
        if cached is not None and cached.sig == sig:
            return cached.events, cached.comp_tuple, cached.user_tuple, cached.post_tuple

    conn: sqlite3.Connection | None = None
    has_step_type = cached.has_step_type if cached is not None else True
    incremental = False
    rows: list[tuple[int, object, object]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        gm_sig = _get_gen_meta_sig(conn)
        if cached is not None and gm_sig == cached.gen_meta_sig:
            families = cached.families
            family_limits = cached.family_limits
        else:
            families, family_limits = extract_db_gen_meta(conn)
        if cached is not None and cached.cutoff_rowid >= 0:
            try:
                cnt_row = conn.execute("SELECT COUNT(*) FROM steps WHERE rowid <= ?", (cached.cutoff_rowid,)).fetchone()
                if cnt_row is not None and int(cnt_row[0]) == cached.cutoff_count:
                    q = (
                        "SELECT rowid, step_type, metadata FROM steps WHERE rowid > ? ORDER BY rowid"
                        if has_step_type
                        else "SELECT rowid, 0, metadata FROM steps WHERE rowid > ? ORDER BY rowid"
                    )
                    rows = conn.execute(q, (cached.cutoff_rowid,)).fetchall()
                    incremental = True
            except sqlite3.Error:
                incremental = False

        if not incremental:
            has_step_type = True
            try:
                rows = conn.execute("SELECT rowid, step_type, metadata FROM steps ORDER BY rowid").fetchall()
            except sqlite3.OperationalError:
                has_step_type = False
                rows = conn.execute("SELECT rowid, 0, metadata FROM steps ORDER BY rowid").fetchall()
    except sqlite3.Error:
        if cached is not None:
            return cached.events, cached.comp_tuple, cached.user_tuple, cached.post_tuple
        return [], (), (), ()
    finally:
        if conn is not None:
            conn.close()

    if incremental and cached is not None:
        events = list(cached.prefix_events)
        compaction_ts = list(cached.prefix_comp)
        user_turn_ts = list(cached.prefix_user)
        post_finish_turn_ts = list(cached.prefix_post)
        last_seen_ts = cached.cutoff_last_ts
        prev_stype = cached.cutoff_prev_stype
        base_count = cached.cutoff_count
        cutoff_rowid = cached.cutoff_rowid
    else:
        events = []
        compaction_ts = []
        user_turn_ts = []
        post_finish_turn_ts = []
        last_seen_ts = 0
        prev_stype = None
        base_count = 0
        cutoff_rowid = -1

    if len(rows) > 5:
        split_idx = len(rows) - 5
        last_seen_ts, prev_stype = decode_step_rows(
            rows[:split_idx], families, has_step_type,
            events, compaction_ts, user_turn_ts, post_finish_turn_ts, last_seen_ts, prev_stype,
        )
        cutoff_rowid = int(rows[split_idx - 1][0])
        cutoff_count = base_count + split_idx
        prefix_events = list(events)
        prefix_comp = list(compaction_ts)
        prefix_user = list(user_turn_ts)
        prefix_post = list(post_finish_turn_ts)
        cutoff_last_ts = last_seen_ts
        cutoff_prev_stype = prev_stype
        tail_rows = rows[split_idx:]
    else:
        cutoff_count = base_count
        prefix_events = list(events)
        prefix_comp = list(compaction_ts)
        prefix_user = list(user_turn_ts)
        prefix_post = list(post_finish_turn_ts)
        cutoff_last_ts = last_seen_ts
        cutoff_prev_stype = prev_stype
        tail_rows = rows

    decode_step_rows(
        tail_rows, families, has_step_type,
        events, compaction_ts, user_turn_ts, post_finish_turn_ts, last_seen_ts, prev_stype,
    )
    comp_tuple = tuple(compaction_ts)
    user_tuple = tuple(user_turn_ts)
    post_tuple = tuple(post_finish_turn_ts)
    entry = _DbStepCache(
        sig=sig, families=families, family_limits=family_limits, gen_meta_sig=gm_sig, has_step_type=has_step_type,
        cutoff_rowid=cutoff_rowid, cutoff_count=cutoff_count,
        cutoff_last_ts=cutoff_last_ts, cutoff_prev_stype=cutoff_prev_stype,
        prefix_events=prefix_events, prefix_comp=prefix_comp,
        prefix_user=prefix_user, prefix_post=prefix_post,
        events=events, comp_tuple=comp_tuple, user_tuple=user_tuple, post_tuple=post_tuple,
    )
    with _CACHE_LOCK:
        _DB_USAGE_CACHE[db_path] = entry
    return events, comp_tuple, user_tuple, post_tuple


def get_cached_db_context_limit(db_path: Path, family: str) -> int:
    """Return latest `gen_metadata` context window limit for `family` from the in-memory DB cache."""
    with _CACHE_LOCK:
        cached = _DB_USAGE_CACHE.get(db_path)
        if cached is None:
            return COMPACTION_LIMIT_TOKENS
        if family in cached.family_limits:
            return int(cached.family_limits[family])
        return int(max(cached.family_limits.values(), default=COMPACTION_LIMIT_TOKENS))


def read_db_step_usage(
    db_path: Path,
    stat_result: os.stat_result | None = None,
) -> list[tuple[int, str, int, int, int, int]]:
    """Read Gemini step token events (excluding Orchestrator) with WAL-aware cache."""
    events, _, _, _ = read_db_dialog_telemetry(db_path, stat_result)
    return [(s, fam, inp, cache, out, think) for s, _e, fam, inp, cache, out, think in events]
