"""Lightweight EN/ZH localization, OS locale detection, and UI preference persistence."""
from __future__ import annotations

import ctypes
import json
import locale
from pathlib import Path
import re

from config.settings import DEFAULT_UI_PREFS_PATH

SUPPORTED_LANGS = ("zh", "en")

_STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        "loading": "加载中...",
        "loading_sync": "正在同步工作时间与上下文状态…",
        "mode_day": "日",
        "mode_week": "周",
        "back_today": "回到今天",
        "back_week": "回到本周",
        "calibrate": "校准",
        "refresh": "刷新",
        "lang_badge": "中",
        "today_tag": " · 今天",
        "today_tag_wide": "  ·  今天",
        "week_tag": " · 本周",
        "eyebrow_today": "今日累计工作时间",
        "eyebrow_day": "{date} 累计工作时间",
        "eyebrow_week": "本周累计工作时间",
        "status_running": "{n} 个对话运行中",
        "status_running_one": "1 个对话运行中",
        "status_idle_day": "当前空闲",
        "status_idle_week": "本周空闲",
        "status_archived": "历史归档",
        "quota_preparing": "额度统计准备中",
        "quota_uncalibrated": "点击右上角「校准」配置额度",
        "quota_day_active": "今日耗周额度 {pct:.1f}%  ·  周余 {left:.1f}%",
        "quota_week_active": "本周耗周额度 {pct:.1f}%  ·  周余 {left:.1f}%",
        "quota_day_hist": "当日耗周额度 {pct:.1f}%",
        "quota_week_hist": "该周耗周额度 {pct:.1f}%",
        "quota_5h_left": "5h余 {left:.1f}%",
        "meta_day": "{iv} 个工作段  ·  {proj} 个项目",
        "meta_empty": "暂无工作记录",
        "meta_week": "活跃 {days}/7 天  ·  日均 {avg}  ·  {proj} 个项目",
        "tab_dialogs": "对话框",
        "tab_projects_day": "项目明细",
        "tab_intervals": "工作时段",
        "tab_days": "每日汇总",
        "tab_projects_week": "本周项目",
        "updated_live": "更新于 {hms} · 实时同步中",
        "quota_col_day": "今日占周额度",
        "quota_col_week": "本周占周额度",
        "col_project": "项目",
        "col_status": "状态",
        "col_duration": "累计时长",
        "empty_projects": "暂无有效项目工作记录",
        "proj_convs": "·  {n} 对话",
        "proj_api": "·  模型调用",
        "proj_running": "运行中",
        "proj_idle": "已停下",
        "col_dialog": "项目 · 对话",
        "col_cache_hit": "缓存命中率",
        "col_kv_cache": "动态缓存",
        "col_ctx_limit": "上下文 / 安全线",
        "col_advice": "换窗建议",
        "empty_dialogs": "当前暂无未归档的活跃对话",
        "sub_tokens": "{model}  ·  入 {inp}  ·  缓 {cache}  ·  出 {out}",
        "waiting_first_step": "等待首步",
        "latest_step": "最新步 {pct:.1f}%",
        "col_slot_idx": "序号",
        "col_time_range": "本地时间区间",
        "col_interval_quota": "模型耗周额度",
        "col_interval_dur": "持续时长",
        "empty_intervals": "当日暂无有效工作时段",
        "slot_label": "时段 {idx:02d}",
        "col_date_day": "日期 · 星期",
        "col_slots_chats": "工作段 / 对话",
        "col_day_quota": "当日耗周额度",
        "col_day_dur": "当日工作时长",
        "day_future": "未开始",
        "day_meta": "{iv} 段 · {conv} 对话",
        "other_model": "其他",
        "tray_tip": "GravityPulse · Antigravity 2.0 常驻监控 (点击打开)",
        "tray_open": "打开 GravityPulse 面板",
        "tray_refresh": "立即刷新数据",
        "tray_startup": "跟随 Antigravity 开机自启",
        "tray_shortcut": "创建桌面快捷方式",
        "tray_lang": "语言: 切换为 English",
        "tray_quit": "退出常驻程序",
        "toast_switch_badge": "● 换窗建议提醒",
        "toast_info_badge": "● GravityPulse",
        "toast_startup_on_head": "已开启开机伴随自启",
        "toast_startup_on_body": "已写入当前用户自启项并拉起守护进程，Antigravity 启动时将自动常驻托盘。",
        "toast_startup_off_head": "已关闭开机伴随自启",
        "toast_startup_off_body": "已移除当前用户自启项，后续可随时在托盘右键菜单重新开启。",
        "toast_shortcut_head": "已创建桌面快捷方式",
        "toast_shortcut_body": "已在桌面生成 GravityPulse 快捷方式，直接双击即可打开面板。",
        "toast_action_err_head": "操作未完成",
        "calib_win_title": "额度与提醒设置",
        "calib_title": "额度校准与换窗提醒",
        "calib_hint": "对照官方 Quota 面板 Weekly limit（如 80% left 即填已用 20%），保存立即生效。",
        "calib_cur": "当前测算：周余 {left:.1f}%（已用 {used:.1f}%{reset}） · 5h余 {fh:.1f}%",
        "calib_used_lbl": "当前 7 天周期已用 (%)",
        "calib_reset_chk": "官方已提前重置周额度（保留上限，从此刻重开新 7 天周期）",
        "calib_mult_lbl": "Carbon 相对 Hydrogen 倍率",
        "calib_preset_lbl": "常用倍率:",
        "calib_preset_measured": "3.0x (实测)",
        "calib_notify_chk": "对话触发「建议换新」时右下角弹窗提醒（不抢输入焦点）",
        "calib_preview": "预览提醒",
        "calib_preview_head": "GravityPulse · 换窗提醒预览 (Hydrogen)",
        "calib_preview_body": "Hydrogen 已累计压缩 2 次（继续压缩与推理损耗已超新建窗口成本），建议本轮结束后换新窗口清理旧问题包袱。",
        "btn_cancel": "取消",
        "btn_save": "保存并应用",
        "rules_title": "统计与常驻规则",
        "rules_body": (
            "1. 随 Antigravity 2.0 自动启动并后台常驻；点击右上角 ✕ 最小化至系统托盘，\n"
            "   秒级实时计时，数据变更即时增量同步，右键托盘图标可配置自启、快捷方式、语言或退出。\n"
            "2. 工作时段严格以你本人发送消息的时间点为起止端点，最后一条消息后不加后台尾巴；\n"
            "   相邻两次发消息无任务时 ≤ 30 分钟连入工时，有对话框任务运行（且任务停后空闲 ≤ 30 分钟）\n"
            "   放宽至 ≤ 1 小时自动连入，孤立单条消息计 0 秒工时（额度正常统计）。\n"
            "3. 未绑定工作区不单独展示项目，但正常计入总工时与总额度。\n"
            "4. 额度按 Carbon {c_mult} / Neon {n_mult} / Hydrogen / 其他折算；「周余」与「5h余」实时同步官方滚动窗口。\n"
            "5. 换窗与冷缓存损益算法（静音弹窗）：\n"
            "   · 512K 下 Carbon 压缩 1次或达 330K、Neon 压缩 1次或达 350K、Hydrogen 压缩 1次或达 400K 立即提醒换新\n"
            "     （运行中超界限亦立即提醒，单轮内若上下文续增 ≥ 50K 再补弹第 2 次；最多 2 次，新压缩或发新消息后重置）；\n"
            "   · 自动识别子代理与后台任务运行态，执行期间不误弹冷缓存提醒；\n"
            "   · 闲置致缓存失效（Carbon ≥ 8分 / Neon ≥ 7分 / Hydrogen ≥ 20分）时对比冷重算与新开窗成本：\n"
            "     短对话续聊更省则保持健康不打扰；长对话重算明显更贵时仅静音提醒 1 次。"
        ),
    },
    "en": {
        "loading": "Loading...",
        "loading_sync": "Syncing work time and context telemetry…",
        "mode_day": "Day",
        "mode_week": "Week",
        "back_today": "Today",
        "back_week": "This Week",
        "calibrate": "Calibrate",
        "refresh": "Refresh",
        "lang_badge": "EN",
        "today_tag": " · Today",
        "today_tag_wide": "  ·  Today",
        "week_tag": " · This Week",
        "eyebrow_today": "Active Work Time Today",
        "eyebrow_day": "Active Work Time · {date}",
        "eyebrow_week": "Active Work Time This Week",
        "status_running": "{n} chats active",
        "status_running_one": "1 chat active",
        "status_idle_day": "Idle",
        "status_idle_week": "Idle",
        "status_archived": "Archived",
        "quota_preparing": "Calculating quota...",
        "quota_uncalibrated": "Click Calibrate to set weekly quota",
        "quota_day_active": "Today {pct:.1f}% of 7d  ·  7d left {left:.1f}%",
        "quota_week_active": "Week {pct:.1f}% of 7d  ·  7d left {left:.1f}%",
        "quota_day_hist": "Day {pct:.1f}% of 7d",
        "quota_week_hist": "Week {pct:.1f}% of 7d",
        "quota_5h_left": "5h left {left:.1f}%",
        "meta_day": "{iv} slots  ·  {proj} projects",
        "meta_empty": "No work recorded",
        "meta_week": "{days}/7 active days  ·  Avg {avg}/day  ·  {proj} projects",
        "tab_dialogs": "Conversations",
        "tab_projects_day": "Projects",
        "tab_intervals": "Intervals",
        "tab_days": "Daily Summary",
        "tab_projects_week": "Projects",
        "updated_live": "Updated {hms} · Live",
        "quota_col_day": "Today's 7d Share",
        "quota_col_week": "Week's 7d Share",
        "col_project": "Project",
        "col_status": "Status",
        "col_duration": "Active Time",
        "empty_projects": "No project activity recorded",
        "proj_convs": "·  {n} chats",
        "proj_api": "·  API calls",
        "proj_running": "Active",
        "proj_idle": "Idle",
        "col_dialog": "Project · Conversation",
        "col_cache_hit": "Cache Hit",
        "col_kv_cache": "KV Cache",
        "col_ctx_limit": "Context / Limit",
        "col_advice": "Advice",
        "empty_dialogs": "No active conversations",
        "sub_tokens": "{model}  ·  In {inp}  ·  Cache {cache}  ·  Out {out}",
        "waiting_first_step": "Pending",
        "latest_step": "Latest {pct:.1f}%",
        "col_slot_idx": "Slot",
        "col_time_range": "Local Time Range",
        "col_interval_quota": "7d Quota Share",
        "col_interval_dur": "Duration",
        "empty_intervals": "No work intervals recorded",
        "slot_label": "Slot {idx:02d}",
        "col_date_day": "Date · Day",
        "col_slots_chats": "Slots / Chats",
        "col_day_quota": "7d Quota Share",
        "col_day_dur": "Active Time",
        "day_future": "Upcoming",
        "day_meta": "{iv} slots · {conv} chats",
        "other_model": "Other",
        "tray_tip": "GravityPulse · Antigravity 2.0 Monitor (Click to open)",
        "tray_open": "Open GravityPulse",
        "tray_refresh": "Refresh Now",
        "tray_startup": "Auto-start with Antigravity",
        "tray_shortcut": "Create Desktop Shortcut",
        "tray_lang": "Language: 切换为中文",
        "tray_quit": "Quit GravityPulse",
        "toast_switch_badge": "● Switch Window Advice",
        "toast_info_badge": "● GravityPulse",
        "toast_startup_on_head": "Auto-start enabled",
        "toast_startup_on_body": "Registered companion watcher for current user. GravityPulse will start in the tray with Antigravity.",
        "toast_startup_off_head": "Auto-start disabled",
        "toast_startup_off_body": "Removed startup entry for current user. You can re-enable it anytime from the tray menu.",
        "toast_shortcut_head": "Desktop shortcut created",
        "toast_shortcut_body": "GravityPulse.lnk has been created on your Desktop.",
        "toast_action_err_head": "Action failed",
        "calib_win_title": "Quota & Alert Settings",
        "calib_title": "Quota Calibration & Switch Alerts",
        "calib_hint": "Match your official Quota panel Weekly limit (e.g. 80% left → enter 20% used). Applies immediately.",
        "calib_cur": "Current estimate: 7d left {left:.1f}% (used {used:.1f}%{reset}) · 5h left {fh:.1f}%",
        "calib_used_lbl": "Current 7-day cycle used (%)",
        "calib_reset_chk": "Official weekly quota reset early (keep cap, start new 7d cycle now)",
        "calib_mult_lbl": "Carbon vs. Hydrogen multiplier",
        "calib_preset_lbl": "Presets:",
        "calib_preset_measured": "3.0x (measured)",
        "calib_notify_chk": "Show silent bottom-right toast on Switch advice (non-activating)",
        "calib_preview": "Preview Toast",
        "calib_preview_head": "GravityPulse · Switch Alert Preview (Hydrogen)",
        "calib_preview_body": "Hydrogen has compacted 2 times (continued compaction & reasoning drag exceeds starting fresh). Open a new window after this turn.",
        "btn_cancel": "Cancel",
        "btn_save": "Save & Apply",
        "rules_title": "Tracking & Cache Rules",
        "rules_body": (
            "1. Runs quietly in the Windows tray alongside Antigravity 2.0; closing ✕ minimizes to tray.\n"
            "   Right-click the tray icon for auto-start, Desktop shortcut, language switch, or quit.\n"
            "2. Work intervals strictly start and end on your explicit prompts with zero unattended tail.\n"
            "   Gaps ≤ 30 min bridge automatically (extended to ≤ 60 min while an agent task is running).\n"
            "3. Unbound workspaces are omitted from the Projects tab but still counted in totals.\n"
            "4. Quota weights Carbon {c_mult} / Neon {n_mult} / Hydrogen / Other; 7d and 5h left track rolling windows.\n"
            "5. Window-switch & cold-cache breakeven advisor (silent non-activating toast):\n"
            "   · At 512K, Carbon (1 compaction or 330K), Neon (1 or 350K), Hydrogen (1 or 400K) alert immediately\n"
            "     (up to 2 alerts per turn on +50K growth; resets on next compaction or new prompt).\n"
            "   · Detects active subagents and background tasks to prevent false cold-cache alerts.\n"
            "   · When idle cache cools (Carbon ≥ 8m / Neon ≥ 7m / Hydrogen ≥ 20m), compares cold recompute vs. fresh\n"
            "     window cost — stays quiet when continuing is cheaper, alerts once when switching saves quota."
        ),
    },
}

_WEEKDAY_EN = {"周一": "Mon", "周二": "Tue", "周三": "Wed", "周四": "Thu", "周五": "Fri", "周六": "Sat", "周日": "Sun"}
_CACHE_STATUS_EN = {
    "未达4K门槛": "Under 4K",
    "子任务运行": "Subtask active",
    "保活中": "Warm (live)",
    "缓存有效": "Warm",
    "缓存衰减": "Cooling",
    "可能失效": "Fading",
    "基本失效": "Cold",
}
_ADVICE_TEXT_EN = {
    "建议换新": "Switch",
    "临近压缩": "Near limit",
    "注意控制": "Watch",
    "状态健康": "Healthy",
}


def normalize_lang(raw: str | None) -> str | None:
    if not raw:
        return None
    low = str(raw).strip().lower()
    if low.startswith("zh"):
        return "zh"
    if low.startswith("en"):
        return "en"
    return None


def detect_system_language() -> str:
    """Detect Windows UI language via kernel32 (`LANG_CHINESE = 0x04`), falling back to locale."""
    try:
        lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
        if (lang_id & 0x3FF) == 0x04:
            return "zh"
        if lang_id > 0:
            return "en"
    except Exception:
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
        "缓存失效 · 续聊新开相近": "Cache cold · breakeven",
        "缓存转冷 · 趁热或换窗": "Cache fading · act soon",
        "缓存衰减 · 趁热续聊": "Cache cooling · use soon",
        "缓存虽冷 · 短文续聊更省": "Cold · short ctx cheaper",
    }
    if text in exact:
        return exact[text]
    for pat, repl in (
        (r"^已压缩 (\d+) 次 · 换窗零损耗$", r"\1x compacted · switch now"),
        (r"^已压缩 (\d+) 次 · 临近第(\d+)次压缩$", r"\1x compacted · near #\2"),
        (r"^已压缩 (\d+) 次 · 可按阶段收尾$", r"\1x compacted · wrap up"),
        (r"^已压缩 (\d+) 次 · 临近 ([^\s]+) 压缩$", r"\1x · near \2 limit"),
        (r"^临近 ([^\s]+) 压缩$", r"Near \1 limit"),
        (r"^已压缩 (\d+) 次$", r"\1x compacted"),
        (r"^超 ([^\s]+) · 高耗区建议换窗$", r"Over \1 · high cost zone"),
        (r"^超 ([^\s]+) · 窗口过载$", r"Over \1 · overloaded"),
        (r"^超 ([^\s]+) · 步均成本升高$", r"Over \1 · rising step cost"),
        (r"^缓存失效 · 重算约新窗([\d.]+)x$", r"Cache cold · \1x fresh cost"),
    ):
        res, n = re.subn(pat, repl, text)
        if n:
            return res
    return text


def localize_toast(head: str, detail: str, lang: str) -> tuple[str, str]:
    if lang == "zh":
        return head, detail
    head_en = head.replace("(其他)", "(Other)")
    prefix = ""
    body = detail
    m_pre = re.match(r"^【二次提醒 · 续增 \+([^，】]+)(?:，当前 ([^】]+))?】(.*)$", body)
    if m_pre:
        delta, cur, rest = m_pre.group(1), m_pre.group(2), m_pre.group(3)
        prefix = f"[Follow-up +{delta}, now {cur}] " if cur else f"[Follow-up +{delta}] "
        body = rest
    m = re.match(r"^(.+?) 已累计压缩 (\d+) 次（.+?），(.+)。$", body)
    if m:
        model = "Model" if m.group(1) == "当前模型" else m.group(1)
        act = "pause and open a new chat" if "暂停" in m.group(3) else "open a fresh window to drop old context"
        return head_en, f"{prefix}{model} has compacted {m.group(2)} times (compaction cost exceeds a fresh window); {act}."
    m = re.match(r"^(.+?) 单步上下文已达 ([^\s（]+)(?:（跨入高耗重算区）)?，(.+)。$", body)
    if m:
        model = "Model" if m.group(1) == "当前模型" else m.group(1)
        act = "pause and open a new chat" if "暂停" in m.group(3) else "open a fresh window to save quota"
        return head_en, f"{prefix}{model} reached {m.group(2)} context; {act}."
    m = re.match(r"^(.+?) (?:已静置 (\d+) 分钟，)?缓存已基本失效（上下文 ([^）]+)），继续提问重算耗额约为新建窗口的 ([\d.]+) 倍.+", body)
    if m:
        model = "Model" if m.group(1) == "当前模型" else m.group(1)
        idle = f"idle {m.group(2)}m, " if m.group(2) else ""
        return head_en, f"{prefix}{model} ({idle}{m.group(3)} ctx) cache is cold; continuing costs ~{m.group(4)}x a fresh window. Start a new chat next."
    if body == "当前上下文已达换窗阈值，建议开启新对话。":
        return head_en, f"{prefix}Context reached the switch threshold. Consider starting a new chat."
    return head_en, f"{prefix}{body}"
