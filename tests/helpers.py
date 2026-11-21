"""Shared Protobuf wire-encoding and SQLite fixture builders for GravityPulse unit tests."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from config.settings import LOCAL_TZ


def local_iso(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> str:
    return datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ).isoformat(timespec="seconds")


def local_utc_z(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> str:
    return datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_utc_sql(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> str:
    return datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def f_v(fn: int, val: int) -> bytes:
    return varint((fn << 3) | 0) + varint(val)


def f_b(fn: int, payload: bytes) -> bytes:
    return varint((fn << 3) | 2) + varint(len(payload)) + payload


def make_step_meta(
    ts_sec: int,
    model_enum: int = 0,
    inp: int = 0,
    outp: int = 0,
    cache: int = 0,
    think: int = 0,
) -> bytes:
    base = f_b(1, f_v(1, ts_sec))
    if model_enum:
        usage = f_v(1, model_enum) + f_v(2, inp) + f_v(3, outp) + f_v(5, cache)
        if think:
            usage += f_v(10, think)
        base += f_b(9, usage)
    return base


def make_step_meta_with_end(
    start_s: int,
    end_s: int,
    model_enum: int,
    inp: int,
    outp: int,
    cache: int,
) -> bytes:
    return (
        f_b(1, f_v(1, start_s))
        + f_b(8, f_v(1, end_s))
        + f_b(9, f_v(1, model_enum) + f_v(2, inp) + f_v(3, outp) + f_v(5, cache))
    )


def init_summary_db(root: Path, rows: list[tuple]) -> Path:
    db_path = root / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            workspace_uris TEXT,
            status TEXT,
            parent_conversation_id TEXT,
            nesting_depth INTEGER,
            last_modified_time TEXT,
            last_user_input_time TEXT
        )
        """
    )
    if rows:
        conn.executemany("INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db_path


def write_transcript(root: Path, cid: str, events: list[tuple[str, str, str]]) -> Path:
    p = root / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "\n".join(
            json.dumps({"step_index": i, "source": src, "type": typ, "created_at": ts})
            for i, (src, typ, ts) in enumerate(events)
        )
        + "\n",
        encoding="utf-8",
    )
    return p
