"""Protobuf wire decoder and Antigravity step-metadata parser."""
from __future__ import annotations

import sqlite3

from config.settings import FALLBACK_MODEL_FAMILIES

STEP_TYPE_USER_INPUT = 14
STEP_TYPE_PLANNER_RESPONSE = 15
STEP_TYPE_CHECKPOINT = 23


def decode_pb_top(data: bytes) -> dict[int, list[tuple[str, int | bytes]]]:
    """Decode top-level protobuf wire fields (varint=0, length-delimited=2) safely."""
    out: dict[int, list[tuple[str, int | bytes]]] = {}
    i = 0
    n = len(data)
    while i < n:
        key = 0
        shift = 0
        while i < n:
            b = data[i]
            i += 1
            key |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        fn = key >> 3
        wt = key & 7
        if wt == 0:
            val = 0
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                val |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            out.setdefault(fn, []).append(("v", val))
        elif wt == 1:
            i += 8
        elif wt == 2:
            ln = 0
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            if ln < 0 or i + ln > n:
                break
            out.setdefault(fn, []).append(("b", data[i : i + ln]))
            i += ln
        elif wt == 5:
            i += 4
        else:
            break
    return out


def _classify_model_strings(model_id: str, display_name: str) -> str | None:
    combined = f"{model_id} {display_name}".lower()
    if "orchestrator" in combined or "flash-worker" in combined:
        return "exclude"
    if "carbon" in combined:
        return "carbon"
    if "neon" in combined:
        return "neon"
    if "hydrogen" in combined:
        return "hydrogen"
    if "argon" in combined:
        return "argon"
    if "flash" in combined:
        return "flash"
    return None


def _extract_sub_context_limit(sub: dict[int, list[tuple[str, int | bytes]]]) -> int | None:
    for t9, v9 in sub.get(9, ()):
        if t9 != "b" or not isinstance(v9, bytes):
            continue
        s9 = decode_pb_top(v9)
        for t10, v10 in s9.get(10, ()):
            if t10 != "b" or not isinstance(v10, bytes):
                continue
            s10 = decode_pb_top(v10)
            for t4, v4 in s10.get(4, ()):
                if t4 == "v" and isinstance(v4, int) and v4 > 0:
                    return int(v4)
    return None


def extract_db_gen_meta(conn: sqlite3.Connection) -> tuple[dict[int, str], dict[str, int]]:
    """Extract dynamic model enum -> family mapping and latest family -> context window limit."""
    families = dict(FALLBACK_MODEL_FAMILIES)
    family_limits: dict[str, int] = {}
    try:
        rows = conn.execute("SELECT data FROM gen_metadata WHERE size < 20000 ORDER BY rowid").fetchall()
    except sqlite3.Error:
        return families, family_limits
    for (blob,) in rows:
        if not isinstance(blob, (bytes, bytearray)):
            continue
        top = decode_pb_top(bytes(blob))
        for tag, val in top.get(1, ()):
            if tag != "b" or not isinstance(val, bytes):
                continue
            sub = decode_pb_top(val)
            m_enum = None
            m_id = ""
            m_disp = ""
            for st, sv in sub.get(3, ()):
                if st == "v" and isinstance(sv, int):
                    m_enum = sv
            for st, sv in sub.get(19, ()):
                if st == "b" and isinstance(sv, bytes):
                    m_id = sv.decode("utf-8", errors="ignore")
            for st, sv in sub.get(21, ()):
                if st == "b" and isinstance(sv, bytes):
                    m_disp = sv.decode("utf-8", errors="ignore")
            if m_enum is not None:
                cls = _classify_model_strings(m_id, m_disp)
                if cls is not None:
                    families[m_enum] = cls
                eff_cls = cls or families.get(m_enum)
                if eff_cls and eff_cls != "exclude":
                    lim = _extract_sub_context_limit(sub)
                    if lim is not None:
                        family_limits[eff_cls] = lim
    return families, family_limits


def extract_db_model_families(conn: sqlite3.Connection) -> dict[int, str]:
    """Extract dynamic model enum -> family bucket mapping from `gen_metadata`."""
    return extract_db_gen_meta(conn)[0]


def _extract_pb_ts_sec(top: dict[int, list[tuple[str, int | bytes]]], field_num: int) -> int | None:
    ts_list = top.get(field_num)
    if not ts_list:
        return None
    ts_raw = ts_list[0][1]
    if not isinstance(ts_raw, bytes):
        return None
    sec_list = decode_pb_top(ts_raw).get(1)
    if not sec_list or not isinstance(sec_list[0][1], int):
        return None
    return int(sec_list[0][1])


def decode_step_rows(
    rows: list[tuple[int, object, object]],
    families: dict[int, str],
    has_step_type: bool,
    events: list[tuple[int, int, str, int, int, int, int]],
    compaction_ts: list[int],
    user_turn_ts: list[int],
    post_finish_turn_ts: list[int],
    last_seen_ts: int,
    prev_stype: int | None,
) -> tuple[int, int | None]:
    """Decode `steps` rows sequentially and append usage/checkpoint/turn telemetry."""
    for _rid, stype_raw, blob in rows:
        stype = int(stype_raw or 0) if has_step_type else 0
        need_meta = stype in (STEP_TYPE_USER_INPUT, STEP_TYPE_CHECKPOINT) or blob is not None
        top = decode_pb_top(bytes(blob)) if (need_meta and isinstance(blob, (bytes, bytearray))) else {}
        start_ts = _extract_pb_ts_sec(top, 1) if top else None
        end_ts_raw = (_extract_pb_ts_sec(top, 8) or _extract_pb_ts_sec(top, 7)) if top else None
        if start_ts is not None:
            last_seen_ts = max(last_seen_ts, end_ts_raw or start_ts)

        if has_step_type and stype == STEP_TYPE_USER_INPUT:
            uts = end_ts_raw or start_ts or last_seen_ts
            user_turn_ts.append(uts)
            if prev_stype == STEP_TYPE_PLANNER_RESPONSE:
                post_finish_turn_ts.append(uts)
        if has_step_type and stype == STEP_TYPE_CHECKPOINT:
            compaction_ts.append(end_ts_raw or start_ts or last_seen_ts)
        prev_stype = stype

        usage_list = top.get(9)
        if start_ts is None or not usage_list:
            continue
        end_ts = max(start_ts, end_ts_raw or start_ts)
        for ut, uv in usage_list:
            if ut != "b" or not isinstance(uv, bytes):
                continue
            sd = decode_pb_top(uv)
            m_list = sd.get(1)
            if not m_list or not isinstance(m_list[0][1], int):
                continue
            family = families.get(m_list[0][1])
            if family not in ("carbon", "neon", "hydrogen", "argon", "flash"):
                continue
            in_tok = int(sd[2][0][1]) if 2 in sd and isinstance(sd[2][0][1], int) else 0
            out_tok = int(sd[3][0][1]) if 3 in sd and isinstance(sd[3][0][1], int) else 0
            cache_tok = int(sd[5][0][1]) if 5 in sd and isinstance(sd[5][0][1], int) else 0
            think_tok = int(sd[10][0][1]) if 10 in sd and isinstance(sd[10][0][1], int) else 0
            if in_tok == 0 and cache_tok == 0 and out_tok == 0:
                continue
            events.append((start_ts, end_ts, family, in_tok, cache_tok, out_tok, think_tok))
    return last_seen_ts, prev_stype
