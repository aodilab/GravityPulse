"""Lightweight EN/ZH localization, OS locale detection, and UI preference persistence."""
from __future__ import annotations

import ctypes
import json
import locale
from pathlib import Path
import re
import subprocess

from config.settings import DEFAULT_UI_PREFS_PATH, IS_MACOS, IS_WINDOWS
from src.ui.i18n_strings import (
    _ADVICE_TEXT_EN,
    _CACHE_STATUS_EN,
    _PHASE_EN,
    _STRINGS,
    _WEEKDAY_EN,
)

SUPPORTED_LANGS = ("zh", "en")


def normalize_lang(raw: str | None) -> str | None:
    if not raw:
        return None
    low = str(raw).strip().lower()
    if low.startswith("zh"):
        return "zh"
    if low.startswith("en"):
        return "en"
    return None


def parse_macos_apple_languages(raw: str) -> str | None:
    """Parse `defaults read -g AppleLanguages` output and return `'zh'` or `'en'`."""
    for m in re.finditer(r'"?([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)"?', raw):
        token = m.group(1).lower()
        if token.startswith("zh"):
            return "zh"
        if len(token) >= 2:
            return "en"
    return None


def detect_system_language() -> str:
    """Detect OS UI language (Win32 on Windows, NSLocale/AppleLanguages on macOS, then locale)."""
    if IS_WINDOWS:
        try:
            lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            if (lang_id & 0x3FF) == 0x04:
                return "zh"
            if lang_id > 0:
                return "en"
        except Exception:
            pass
    elif IS_MACOS:
        try:
            from Foundation import NSLocale

            langs = list(NSLocale.preferredLanguages() or [])
            if langs:
                return "zh" if str(langs[0]).lower().startswith("zh") else "en"
        except Exception:
            pass
        try:
            res = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                parsed = parse_macos_apple_languages(res.stdout)
                if parsed:
                    return parsed
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        loc = locale.getlocale()[0] or ""
        if loc.lower().startswith(("zh", "chinese")):
            return "zh"
    except Exception:
        pass
    return "en"


def load_ui_language(prefs_path: Path = DEFAULT_UI_PREFS_PATH) -> str:
    """Load persisted UI language from `data/ui_prefs.json`, or auto-detect from OS."""
    if prefs_path.is_file():
        try:
            raw = json.loads(prefs_path.read_text(encoding="utf-8"))
            lang = normalize_lang(raw.get("language"))
            if lang in SUPPORTED_LANGS:
                return lang
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return detect_system_language()


def save_ui_language(lang: str, prefs_path: Path = DEFAULT_UI_PREFS_PATH) -> str:
    """Persist explicit UI language selection under `data/ui_prefs.json`."""
    norm = normalize_lang(lang) or "en"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps({"language": norm}, ensure_ascii=False, indent=2), encoding="utf-8")
    return norm


def tr(key: str, lang: str, **kwargs: object) -> str:
    table = _STRINGS.get(lang, _STRINGS["en"])
    tpl = table.get(key, _STRINGS["zh"].get(key, key))
    return tpl.format(**kwargs) if kwargs else tpl


def localize_weekday(text: str, lang: str) -> str:
    return text if lang == "zh" else _WEEKDAY_EN.get(text, text)


def localize_model_label(label: str, lang: str) -> str:
    return "Other" if (lang != "zh" and label == "其他") else label


def localize_duration(text: str, lang: str) -> str:
    if lang == "zh" or not text:
        return text
    m = re.fullmatch(r"(\d+)\s*小时\s*(\d+)\s*分\s*(\d+)\s*秒", text.strip())
    if m:
        return f"{int(m.group(1))}h {int(m.group(2)):02d}m {int(m.group(3)):02d}s"
    m = re.fullmatch(r"(\d+)\s*分\s*(\d+)\s*秒", text.strip())
    if m:
        return f"{int(m.group(1))}m {int(m.group(2)):02d}s"
    m = re.fullmatch(r"(\d+)\s*小时\s*(\d+)\s*分钟", text.strip())
    if m:
        return f"{int(m.group(1))}h {int(m.group(2))}m"
    m = re.fullmatch(r"(\d+)\s*分钟", text.strip())
    if m:
        return f"{int(m.group(1))}m"
    return text


def localize_reset_str(text: str, lang: str) -> str:
    if lang == "zh" or not text:
        return text
    if text == "待触发新周期":
        return "new cycle pending"
    if text == "满额待命":
        return "full budget ready"
    m = re.fullmatch(r"(\d+)天(\d+)小时后重置", text)
    if m:
        return f"resets in {m.group(1)}d {m.group(2)}h"
    m = re.fullmatch(r"(\d+)小时(\d+)分后重置", text)
    if m:
        return f"resets in {m.group(1)}h {m.group(2)}m"
    m = re.fullmatch(r"(\d+)分钟后重置", text)
    if m:
        return f"resets in {m.group(1)}m"
    return text


def localize_cache_status(text: str, lang: str) -> str:
    return text if lang == "zh" else _CACHE_STATUS_EN.get(text, text)


def localize_cache_idle(text: str, lang: str) -> str:
    if lang == "zh" or not text:
        return text
    if text == "运行中":
        return "Running"
    if text == "刚刚":
        return "Just now"
    m = re.fullmatch(r"(\d+)分钟前", text)
    if m:
        return f"{m.group(1)}m ago"
    m = re.fullmatch(r"([\d.]+)小时前", text)
    if m:
        return f"{m.group(1)}h ago"
    m = re.fullmatch(r"(\d+)天前", text)
    if m:
        return f"{m.group(1)}d ago"
    m = re.fullmatch(r"子任务 (\d+) · (.+)", text)
    if m:
        return f"Sub {m.group(1)} · {localize_cache_idle(m.group(2), lang)}"
    m = re.fullmatch(r"主缓已冷 · (\d+)个在跑", text)
    if m:
        return f"Cold · {m.group(1)} running"
    m = re.fullmatch(r"(\d+)个任务 · (.+)", text)
    if m:
        return f"{m.group(1)} tasks · {localize_cache_idle(m.group(2), lang)}"
    return text


def localize_advice_text(text: str, lang: str) -> str:
    return text if lang == "zh" else _ADVICE_TEXT_EN.get(text, text)


def localize_advice_reason(text: str, lang: str) -> str:
    if lang == "zh" or not text:
        return text
    exact = {
        "未触发压缩": "No compaction",
        "正在写交接 · 完成后换新": "Writing handover · switch after",
        "临近压缩 · 建议立即换窗": "Near limit · switch now",
        "快压缩了 · 待确认立即换": "Near limit · switch now",
        "快压缩了 · 后台中立即换": "Near limit · switch now",
        "快压缩了 · 立即停并换窗": "Near limit · stop & switch",
        "即将压缩 · 立即停并交接": "Imminent limit · handover now",
        "快压缩了 · 建议暂停交接": "Near limit · pause & handover",
        "缓存失效 · 续聊新开相近": "Cache cold · breakeven",
        "缓存转冷 · 趁热或换窗": "Cache fading · act soon",
        "缓存衰减 · 趁热续聊": "Cache cooling · use soon",
        "缓存虽冷 · 短文续聊更省": "Cold · short ctx cheaper",
        "子窗运行中": "Worker running",
        "轻量待命 · 同模块可复用": "Light idle · reusable",
        "热缓小收尾可复用": "Warm · reusable for small fix",
        "子窗已冷 · 大任务换新": "Cold · respawn for big task",
        "子窗偏重 · 完成后换新": "Heavy worker · respawn after",
        "子窗超 250K · 下轮 kill 换新": "Over 250K · respawn next turn",
        "子窗临近压缩 · 建议主窗喊停": "Worker near limit · stop & respawn",
        "子窗临近压缩 · 下轮换新": "Worker near limit · respawn next",
    }
    if text in exact:
        return exact[text]
    m_zone = re.fullmatch(r"换窗区 ([^\s]+)(?: · (.+))?", text)
    if m_zone:
        p = _PHASE_EN.get(m_zone.group(2) or "", m_zone.group(2) or "")
        return f"Zone {m_zone.group(1)}" + (f" · {p}" if p else "")
    for pat, repl in (
        (r"^子窗已压缩 (\d+) 次 · 建议换新$", r"Worker \1x compacted · respawn"),
        (r"^子窗超 ([^\s]+) · 建议阶段收口$", r"Worker over \1 · wrap & respawn"),
        (r"^子窗超 ([^\s]+) · 下轮换新$", r"Worker over \1 · respawn next"),
        (r"^待确认 · 换窗区 ([^\s]+)$", r"Awaiting input · zone \1"),
        (r"^等后台 · 换窗区 ([^\s]+)$", r"Waiting bg · zone \1"),
        (r"^待确认 · 达 ([^\s]+) 经济线$", r"Awaiting input · at \1 EOQ"),
        (r"^等后台 · 达 ([^\s]+) 经济线$", r"Waiting bg · at \1 EOQ"),
        (r"^已停顿 · 达 ([^\s]+) 经济线$", r"Paused · at \1 EOQ line"),
        (r"^达 ([^\s]+) 经济线 · 停顿后换新$", r"At \1 EOQ · switch on pause"),
        (r"^接近 ([^\s]+) 经济线$", r"Near \1 EOQ line"),
        (r"^已压缩 (\d+) 次 · 换窗零损耗$", r"\1x compacted · switch now"),
        (r"^已压缩 (\d+) 次 · 临近第(\d+)次压缩$", r"\1x compacted · near #\2"),
        (r"^已压缩 (\d+) 次 · 可按阶段收尾$", r"\1x compacted · wrap up"),
        (r"^已压缩 (\d+) 次 · 临近 ([^\s]+) 压缩$", r"\1x · near \2 limit"),
        (r"^(.+?) · 待确认直接换窗$", r"\1 · switch now"),
        (r"^(.+?) · 等后台可直接换$", r"\1 · bg wait, switch ok"),
        (r"^(.+?) · 新阶段可直接停$", r"\1 · new stage, stop ok"),
        (r"^(.+?) · 读码排查勿硬停$", r"\1 · reading, finish first"),
        (r"^(.+?) · 改码中勿硬停$", r"\1 · editing, finish first"),
        (r"^(.+?) · 收尾验证等跑完$", r"\1 · verifying, wait end"),
        (r"^(.+?) · 验证期可交接$", r"\1 · verifying, handover ok"),
        (r"^(.+?) · 临近压缩可交接$", r"\1 · near limit, handover"),
        (r"^临近 ([^\s]+) 压缩$", r"Near \1 limit"),
        (r"^已压缩 (\d+) 次$", r"\1x compacted"),
        (r"^超 ([^\s]+) · 高耗区建议换窗$", r"Over \1 · high cost zone"),
        (r"^超 ([^\s]+) · 窗口过载$", r"Over \1 · overloaded"),
        (r"^超 ([^\s]+) · 步均成本升高$", r"Over \1 · rising step cost"),
        (r"^缓存失效 · 重算约新窗([\d.]+)x$", r"Cache cold · \1x fresh cost"),
        (r"^缓存失效 · 约新窗([\d.]+)x$", r"Cache cold · ~\1x fresh"),
    ):
        res, n = re.subn(pat, repl, text)
        if n:
            return res.replace("已压缩 ", "").replace(" 次", "x compacted").replace("超 ", "Over ")
    return text


def localize_toast(head: str, detail: str, lang: str) -> tuple[str, str]:
    if lang == "zh":
        return head, detail
    head_en = head.replace("(其他)", "(Other)").replace(" · 子窗口 ", " · Worker ")
    prefix = ""
    body = detail
    for pre_pat in (
        r"^二次提醒（\+([^，）]+)(?:，当前 ([^）]+))?）：(.*)$",
        r"^【二次提醒 · 续增 \+([^，】]+)(?:，当前 ([^】]+))?】(.*)$",
    ):
        m_pre = re.match(pre_pat, body)
        if m_pre:
            delta, cur, rest = m_pre.group(1), m_pre.group(2), m_pre.group(3)
            prefix = f"[Follow-up +{delta}, now {cur}] " if cur else f"[Follow-up +{delta}] "
            body = rest
            break
    for pat, repl in (
        (r"^([^\s]+) · 长任务换新，短任务可续$", r"\1 · Switch for big tasks, ok for quick ones."),
        (r"^([^\s]+) 待确认 · 长任务换新，短任务可续$", r"\1 awaiting input · Switch for big tasks, ok for quick ones."),
        (r"^([^\s]+) 等后台 · 长任务换新，短任务可续$", r"\1 waiting on bg · Switch for big tasks, ok for quick ones."),
        (r"^([^\s]+) · 已达经济线，建议换新$", r"\1 reached EOQ threshold; start a fresh chat."),
        (r"^([^\s]+) 待确认 · 已达经济线，建议换新$", r"\1 awaiting input at EOQ threshold; start a fresh chat."),
        (r"^([^\s]+) 等后台 · 已达经济线，可写交接换新$", r"\1 waiting on bg at EOQ threshold; hand over to a fresh chat."),
        (r"^([^\s]+)（(\d+%)）(?: 待确认| 等后台)? · 即将压缩，请停下交接换新$", r"\1 (\2) imminent compaction — pause and hand over now."),
        (r"^([^\s]+)（(\d+%)）(?: 待确认| 等后台)? · 临近压缩，.*$", r"\1 (\2) approaching compaction limit — hand over to a fresh chat."),
        (r"^([^\s]+) · 临近压缩，请立即换新$", r"\1 approaching compaction limit — start a fresh chat now."),
        (r"^([^\s]+) · 窗口过载，建议换新$", r"\1 overloaded — start a fresh chat."),
        (r"^已压缩 (\d+) 次 · 建议开新窗$", r"Compacted \1x — start a fresh chat."),
        (r"^([^\s]+) 缓存已冷 · 重算约 ([\d.]+)x，建议换新$", r"\1 cache cold (~ \2x fresh cost) — start a new chat."),
        (r"^子窗口已压缩 (\d+) 次（([^）]+)），.*$", r"Worker compacted \1x (\2) — wrap up and respawn worker."),
        (r"^子窗口运行至 ([^\s（]+)（(\d+%)），临近 ([^\s]+) 压缩.*$", r"Worker at \1 (\2), near \3 limit — stop & respawn worker."),
        (r"^子窗口停在 ([^\s（]+)（临近 ([^\s]+) 压缩）.*$", r"Worker paused at \1 (near \2 limit) — respawn next turn."),
        (r"^子窗口运行至 ([^\s（]+)（超 ([^\s]+) 高耗线）.*$", r"Worker at \1 (over \2) — wrap up and respawn worker."),
        (r"^子窗口已达 ([^\s（]+)（超 ([^\s]+)）.*$", r"Worker reached \1 (over \2) — respawn next turn."),
        (r"^上下文已达换窗线，建议开新窗。$", "Context reached switch threshold. Consider opening a fresh chat."),
    ):
        res, n = re.subn(pat, repl, body)
        if n:
            return head_en, f"{prefix}{res}"
    return head_en, f"{prefix}{body}"
