"""Local SQLite persistence ledger preserving work-hour and quota telemetry after Antigravity deletes dialogs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading

from config.settings import DEFAULT_ANTIGRAVITY_DIR, DEFAULT_LEDGER_PATH
from src.core.models import ConversationMeta, parse_iso_utc
from src.core.storage import (
    db_stat_signature,
    load_db_metadata,
    read_db_step_usage,
    read_transcript_events,
    resolve_root_id,
)

_LEDGER_LOCK = threading.RLock()


@dataclass
class _LedgerCache:
    meta_rows: dict[str, ConversationMeta]
    transcript_sigs: dict[str, tuple[int, int]]
    transcript_nonempty: set[str]
    usage_sigs: dict[str, str]
    usage_nonempty: set[str]


_LEDGER_CACHE: dict[Path, _LedgerCache] = {}


def _norm_path(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def resolve_ledger_path(antigravity_dir: Path, ledger_path: Path | None = None) -> Path | None:
    """Resolve persistence ledger path: explicit override, default `data/telemetry_ledger.db` for live dir, else None."""
    if ledger_path is not None:
        return _norm_path(ledger_path)
    if _norm_path(antigravity_dir) == _norm_path(DEFAULT_ANTIGRAVITY_DIR):
        return _norm_path(DEFAULT_LEDGER_PATH)
    return None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conv_meta (
            conversation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            workspace TEXT NOT NULL,
            parent_conversation_id TEXT NOT NULL,
            nesting_depth INTEGER NOT NULL,
            last_modified_iso TEXT NOT NULL,
            last_user_input_iso TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conv_transcripts (
            cid TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            min_ts REAL NOT NULL,
            max_ts REAL NOT NULL,
            has_user_input INTEGER NOT NULL,
            events_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_tr_max_ts ON conv_transcripts(max_ts)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conv_usage (
            cid TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            db_sig TEXT NOT NULL,
            min_ts INTEGER NOT NULL,
            max_ts INTEGER NOT NULL,
            events_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_usage_max_ts ON conv_usage(max_ts)")


def _open_ledger(ledger_path: Path) -> sqlite3.Connection:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ledger_path, timeout=5.0)
    _ensure_schema(conn)
    return conn


def _load_cache_locked(ledger_path: Path) -> _LedgerCache:
    cached = _LEDGER_CACHE.get(ledger_path)
    if cached is not None:
        return cached

    meta_rows: dict[str, ConversationMeta] = {}
    transcript_sigs: dict[str, tuple[int, int]] = {}
    transcript_nonempty: set[str] = set()
    usage_sigs: dict[str, str] = {}
    usage_nonempty: set[str] = set()

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_ledger(ledger_path)
        for cid, title, ws, parent_id, depth, lmt_iso, luit_iso in conn.execute(
            "SELECT conversation_id, title, workspace, parent_conversation_id, nesting_depth, last_modified_iso, last_user_input_iso FROM conv_meta"
        ):
            meta_rows[cid] = ConversationMeta(
                conversation_id=cid,
                title=title,
                workspace=ws,
                status="",
                parent_conversation_id=parent_id,
                nesting_depth=int(depth),
                last_modified_time=parse_iso_utc(lmt_iso) if lmt_iso else None,
                last_user_input_time=parse_iso_utc(luit_iso) if luit_iso else None,
            )
        for cid, mtime_ns, size, max_ts in conn.execute(
            "SELECT cid, mtime_ns, size, max_ts FROM conv_transcripts"
        ):
            transcript_sigs[cid] = (int(mtime_ns), int(size))
            if float(max_ts) > 0.0:
                transcript_nonempty.add(cid)
        for cid, db_sig, max_ts in conn.execute(
            "SELECT cid, db_sig, max_ts FROM conv_usage"
        ):
            usage_sigs[cid] = str(db_sig)
            if int(max_ts) > 0:
                usage_nonempty.add(cid)
    except sqlite3.Error:
        pass
    finally:
        if conn is not None:
            conn.close()

    entry = _LedgerCache(
        meta_rows=meta_rows,
        transcript_sigs=transcript_sigs,
        transcript_nonempty=transcript_nonempty,
        usage_sigs=usage_sigs,
        usage_nonempty=usage_nonempty,
    )
    _LEDGER_CACHE[ledger_path] = entry
    return entry


def _meta_persist_tuple(m: ConversationMeta) -> tuple[str, str, str, str, int, str, str]:
    lmt_iso = m.last_modified_time.isoformat() if m.last_modified_time is not None else ""
    luit_iso = m.last_user_input_time.isoformat() if m.last_user_input_time is not None else ""
    return (
        m.conversation_id,
        m.title,
        m.workspace,
        m.parent_conversation_id,
        int(m.nesting_depth),
        lmt_iso,
        luit_iso,
    )


def _merge_live_over_archived(
    live: ConversationMeta,
    archived: ConversationMeta | None,
) -> ConversationMeta:
    if archived is None:
        return live
    title = live.title or archived.title
    ws = live.workspace if live.workspace not in ("", "未知工作区", "未绑定工作区") else archived.workspace
    parent_id = live.parent_conversation_id or archived.parent_conversation_id
    lmt = live.last_modified_time or archived.last_modified_time
    luit = live.last_user_input_time or archived.last_user_input_time
    return ConversationMeta(
        conversation_id=live.conversation_id,
        title=title,
        workspace=ws,
        status=live.status,
        parent_conversation_id=parent_id,
        nesting_depth=max(live.nesting_depth, archived.nesting_depth),
        last_modified_time=lmt,
        last_user_input_time=luit,
    )


def sync_telemetry_ledger(
    antigravity_dir: Path,
    ledger_path: Path | None = None,
    live_meta: dict[str, ConversationMeta] | None = None,
) -> dict[str, ConversationMeta]:
    """Incrementally persist changed metadata, transcripts, and usage DBs into the local ledger and return merged metadata."""
    if live_meta is None:
        live_meta = load_db_metadata(antigravity_dir / "conversation_summaries.db")

    eff_path = resolve_ledger_path(antigravity_dir, ledger_path)
    if eff_path is None:
        return live_meta

    with _LEDGER_LOCK:
        cache = _load_cache_locked(eff_path)
        archived_meta = dict(cache.meta_rows)
        known_tr_sigs = dict(cache.transcript_sigs)
        known_tr_nonempty = set(cache.transcript_nonempty)
        known_usage_sigs = dict(cache.usage_sigs)
        known_usage_nonempty = set(cache.usage_nonempty)

    merged_meta: dict[str, ConversationMeta] = dict(archived_meta)
    meta_upserts: list[tuple[str, str, str, str, int, str, str]] = []
    meta_cache_updates: dict[str, ConversationMeta] = {}

    for cid, m in live_meta.items():
        prev = archived_meta.get(cid)
        eff_m = _merge_live_over_archived(m, prev)
        merged_meta[cid] = eff_m
        archived_ver = ConversationMeta(
            conversation_id=eff_m.conversation_id,
            title=eff_m.title,
            workspace=eff_m.workspace,
            status="",
            parent_conversation_id=eff_m.parent_conversation_id,
            nesting_depth=eff_m.nesting_depth,
            last_modified_time=eff_m.last_modified_time,
            last_user_input_time=eff_m.last_user_input_time,
        )
        if prev is None or _meta_persist_tuple(prev) != _meta_persist_tuple(archived_ver):
            meta_upserts.append(_meta_persist_tuple(archived_ver))
            meta_cache_updates[cid] = archived_ver

    tr_upserts: list[tuple[str, int, int, float, float, int, str]] = []
    brain_dir = antigravity_dir / "brain"
    if brain_dir.is_dir():
        try:
            with os.scandir(brain_dir) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    tp = Path(entry.path) / ".system_generated" / "logs" / "transcript.jsonl"
                    try:
                        st = tp.stat()
                    except OSError:
                        continue
                    cid = entry.name
                    sig = (st.st_mtime_ns, st.st_size)
                    if known_tr_sigs.get(cid) == sig:
                        continue
                    events, has_user = read_transcript_events(tp, st)
                    if not events and cid in known_tr_nonempty:
                        continue
                    if events:
                        ts_vals = [dt.timestamp() for dt, _ in events]
                        min_ts, max_ts = min(ts_vals), max(ts_vals)
                        ev_json = json.dumps(
                            [[round(dt.timestamp() * 1_000_000), 1 if u else 0] for dt, u in events],
                            separators=(",", ":"),
                        )
                    else:
                        min_ts, max_ts, ev_json = 0.0, 0.0, "[]"
                    tr_upserts.append((cid, sig[0], sig[1], min_ts, max_ts, 1 if has_user else 0, ev_json))
        except OSError:
            pass

    usage_upserts: list[tuple[str, str, str, int, int, str]] = []
    conv_dir = antigravity_dir / "conversations"
    if conv_dir.is_dir():
        try:
            with os.scandir(conv_dir) as it:
                for entry in it:
                    if not entry.name.endswith(".db"):
                        continue
                    db_path = Path(entry.path)
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    sig_info = db_stat_signature(db_path, st)
                    if sig_info is None:
                        continue
                    cid = db_path.stem
                    sig_t = sig_info[1]
                    sig_str = f"v2:{sig_t[0]}:{sig_t[1]}:{sig_t[2]}:{sig_t[3]}"
                    if known_usage_sigs.get(cid) == sig_str:
                        continue
                    events = read_db_step_usage(db_path, st)
                    if not events and cid in known_usage_nonempty:
                        continue
                    root_id = resolve_root_id(cid, merged_meta)
                    root_meta = merged_meta.get(root_id)
                    ws = root_meta.workspace if root_meta is not None else "未绑定工作区"
                    if events:
                        min_ts = min(ev[0] for ev in events)
                        max_ts = max(ev[0] for ev in events)
                        ev_json = json.dumps(events, separators=(",", ":"))
                    else:
                        min_ts, max_ts, ev_json = 0, 0, "[]"
                    usage_upserts.append((cid, ws, sig_str, min_ts, max_ts, ev_json))
        except OSError:
            pass

    if not meta_upserts and not tr_upserts and not usage_upserts:
        return merged_meta

    with _LEDGER_LOCK:
        cache = _load_cache_locked(eff_path)
        conn: sqlite3.Connection | None = None
        try:
            conn = _open_ledger(eff_path)
            with conn:
                if meta_upserts:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO conv_meta (
                            conversation_id, title, workspace, parent_conversation_id,
                            nesting_depth, last_modified_iso, last_user_input_iso
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        meta_upserts,
                    )
                    cache.meta_rows.update(meta_cache_updates)
                if tr_upserts:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO conv_transcripts (
                            cid, mtime_ns, size, min_ts, max_ts, has_user_input, events_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        tr_upserts,
                    )
                    for cid, mtime_ns, size, _mn, mx, _hu, _ej in tr_upserts:
                        cache.transcript_sigs[cid] = (mtime_ns, size)
                        if mx > 0.0:
                            cache.transcript_nonempty.add(cid)
                if usage_upserts:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO conv_usage (
                            cid, workspace, db_sig, min_ts, max_ts, events_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        usage_upserts,
                    )
                    for cid, _ws, sig_str, _mn, mx, _ej in usage_upserts:
                        cache.usage_sigs[cid] = sig_str
                        if mx > 0:
                            cache.usage_nonempty.add(cid)
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()

    return merged_meta


def load_archived_transcripts(
    ledger_path: Path,
    exclude_cids: set[str],
    min_max_ts: float,
) -> list[tuple[str, list[tuple[datetime, bool]], bool]]:
    """Load archived `(cid, events, has_user_input)` records for conversations no longer on disk."""
    if not ledger_path.is_file():
        return []
    out: list[tuple[str, list[tuple[datetime, bool]], bool]] = []
    with _LEDGER_LOCK:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(ledger_path, timeout=2.0)
            rows = conn.execute(
                "SELECT cid, has_user_input, events_json FROM conv_transcripts WHERE max_ts >= ? AND events_json != '[]'",
                (float(min_max_ts),),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            if conn is not None:
                conn.close()

    for cid, has_user_int, ev_json in rows:
        if cid in exclude_cids:
            continue
        try:
            raw_list = json.loads(ev_json)
        except json.JSONDecodeError:
            continue
        events: list[tuple[datetime, bool]] = []
        for item in raw_list:
            ts_us = int(item[0])
            sec, usec = divmod(ts_us, 1_000_000)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc).replace(microsecond=usec)
            events.append((dt, bool(item[1])))
        if events:
            out.append((cid, events, bool(has_user_int)))
    return out


def load_archived_usage(
    ledger_path: Path,
    exclude_cids: set[str],
    min_max_ts: float,
) -> list[tuple[str, str, list[tuple[int, str, int, int, int, int]]]]:
    """Load archived `(cid, archived_workspace, step_events)` for DBs no longer on disk."""
    if not ledger_path.is_file():
        return []
    out: list[tuple[str, str, list[tuple[int, str, int, int, int, int]]]] = []
    with _LEDGER_LOCK:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(ledger_path, timeout=2.0)
            rows = conn.execute(
                "SELECT cid, workspace, events_json FROM conv_usage WHERE max_ts >= ? AND events_json != '[]'",
                (int(min_max_ts),),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            if conn is not None:
                conn.close()

    for cid, ws, ev_json in rows:
        if cid in exclude_cids:
            continue
        try:
            raw_list = json.loads(ev_json)
        except json.JSONDecodeError:
            continue
        events = [
            (int(r[0]), str(r[1]), int(r[2]), int(r[3]), int(r[4]), int(r[5]))
            for r in raw_list
        ]
        if events:
            out.append((cid, str(ws), events))
    return out
