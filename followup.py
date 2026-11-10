"""Follow-up helpers: snooze buttons on long replies, a pinned card of pending decisions, wrap-up cards.

Pure functions only (no Hermes, no python-telegram-bot), so they are unit-testable on their own.
"""
from __future__ import annotations

import html
import re
import secrets
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

SNOOZE_PREFIX = "hz:"
DECISION_PREFIX = "hd:"
SNOOZE_RE = re.compile(r"^hz:(1h|3h|ev|am)$")
DECISION_RE = re.compile(r"^hd:([0-9a-f]{8}):(\d|j)$")
MAX_OPTIONS = 3
EVENING_HOUR = 20
MORNING_HOUR = 9
MAX_DECISIONS = 12
REMINDER_TTL = 14 * 86400

STR = {
    "1h": {"zh": "⏰ 1 小时后", "en": "⏰ In 1 hour"},
    "3h": {"zh": "⏰ 3 小时后", "en": "⏰ In 3 hours"},
    "ev": {"zh": "今晚", "en": "Tonight"},
    "am": {"zh": "明早", "en": "Tomorrow morning"},
    "set": {"zh": "好，{when} 提醒你", "en": "OK, I'll remind you {when}"},
    "nudge": {"zh": "⏰ 你让我这时候提醒你回头看这条", "en": "⏰ You asked me to bring this back now"},
    "dec_title": {"zh": "📌 待你决定 · {n}", "en": "📌 Waiting on you · {n}"},
    "yes": {"zh": "是", "en": "Yes"},
    "no": {"zh": "否", "en": "No"},
    "jump": {"zh": "↗ 原消息", "en": "↗ Message"},
    "jump_here": {"zh": "↑ 就是这条，点上面的引用跳过去", "en": "↑ This one — tap the quote to jump there"},
    "jump_sent": {"zh": "已在聊天底部回复了原消息", "en": "Replied to the original message at the bottom"},
    "dec_answered": {"zh": "已选择：{label}", "en": "Selected: {label}"},
    "dec_gone": {"zh": "这件事已经处理过了", "en": "Already handled"},
    "dec_failed": {"zh": "没能转给我，请直接回复", "en": "Couldn't pass this on — please reply directly"},
    "changed": {"zh": "改了什么", "en": "What changed"},
    "verified": {"zh": "验证过的", "en": "Verified"},
    "pending": {"zh": "还没做 / 没验证", "en": "Not done / not verified"},
}


def s(key: str, lang: str, **kw: Any) -> str:
    entry = STR[key]
    text = entry.get(lang) or entry["en"]
    return text.format(**kw) if kw else text


# ---------------------------------------------------------------- snooze

def _tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("America/Chicago")


def snooze_due(code: str, now: datetime) -> datetime:
    """When a snooze button fires. ``now`` must be timezone-aware (the user's display timezone)."""
    if code == "1h":
        return now + timedelta(hours=1)
    if code == "3h":
        return now + timedelta(hours=3)
    if code == "ev":
        due = now.replace(hour=EVENING_HOUR, minute=0, second=0, microsecond=0)
        return due if due > now + timedelta(minutes=30) else now + timedelta(hours=2)
    if code == "am":
        due = now.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
        if now.hour >= 5 or due <= now:
            due += timedelta(days=1)
        return due
    raise ValueError(code)


def snooze_codes(now: datetime) -> list[str]:
    """The buttons that make sense right now (no 'tonight' late at night)."""
    late = now.hour >= EVENING_HOUR - 1 or now.hour < 5
    return ["1h", "3h", "am"] if late else ["1h", "ev", "am"]


def snooze_row(now: datetime, lang: str) -> str:
    tags = [f'<tg-button type="callback_data" data="{SNOOZE_PREFIX}{c}">{html.escape(s(c, lang))}</tg-button>'
            for c in snooze_codes(now)]
    return "<tg-button-row>\n" + "\n".join(tags) + "\n</tg-button-row>"


def has_buttons(markdown: str) -> bool:
    return "<tg-button" in (markdown or "")


def when_text(due: datetime, now: datetime, lang: str) -> str:
    hm = due.strftime("%H:%M")
    if due.date() == now.date():
        return ("今天 " if lang == "zh" else "today at ") + hm
    if due.date() == (now + timedelta(days=1)).date():
        return ("明天 " if lang == "zh" else "tomorrow at ") + hm
    return due.strftime("%m-%d ") + hm


def parse_snooze(data: str) -> str | None:
    m = SNOOZE_RE.match(data or "")
    return m.group(1) if m else None


def reminder_key(chat_id: Any, message_id: Any) -> str:
    return f"{chat_id}:{message_id}"


def due_reminders(data: dict[str, Any], now_ts: float) -> list[tuple[str, dict[str, Any]]]:
    return sorted(((k, v) for k, v in data.items() if float(v.get("due", 0)) <= now_ts),
                  key=lambda kv: kv[1].get("due", 0))


def prune_reminders(data: dict[str, Any], now_ts: float) -> None:
    for k in [k for k, v in data.items() if now_ts - float(v.get("due", 0)) > REMINDER_TTL]:
        data.pop(k, None)


# ---------------------------------------------------------------- decisions card

def new_item_id() -> str:
    return secrets.token_hex(4)


def parse_decision(data: str) -> tuple[str, str] | None:
    """``hd:<item>:<k>`` -> (item id, k) where k is an option index or 'j' (jump to the message)."""
    m = DECISION_RE.match(data or "")
    return (m.group(1), m.group(2)) if m else None


def normalize_options(options: Any) -> tuple[list[str], list[str]]:
    """Options as strings or {label, value}: -> (labels, values); value defaults to the label."""
    labels: list[str] = []
    values: list[str] = []
    for o in options or []:
        if isinstance(o, dict):
            label, value = str(o.get("label") or "").strip(), str(o.get("value") or "").strip()
        else:
            label, value = str(o or "").strip(), ""
        if label:
            labels.append(label[:40])
            values.append((value or label)[:300])
    return labels[:MAX_OPTIONS], values[:MAX_OPTIONS]


def item_options(item: dict[str, Any], lang: str) -> list[str]:
    opts = [str(o) for o in (item.get("opts") or []) if str(o).strip()][:MAX_OPTIONS]
    return opts or [s("yes", lang), s("no", lang)]  # legacy items without model-chosen actions


def item_value(item: dict[str, Any], index: int, lang: str) -> str:
    vals = item.get("vals") or []
    return str(vals[index]) if index < len(vals) and vals[index] else item_options(item, lang)[index]


def find_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return next((it for it in items if it.get("id") == item_id), None)


def decision_injection_text(item: dict[str, Any], label: str, who: str, value: str = "") -> str:
    """Data-only description of a press on the pinned card, for the conversation."""
    lines = [f"[待你决定] {who} 对「{item.get('text', '')}」选了「{label}」"]
    if value and value != label:
        lines.append(f"value: {value}")
    lines.append(f"decision_id: {item.get('id', '')}")
    return "\n".join(lines)


def decisions_key(chat_id: Any, thread_id: Any) -> str:
    return f"{chat_id}:{thread_id or '-'}"


def add_item(items: list[dict[str, Any]], text: str, now_ts: float, item_id: str | None = None,
             **extra: Any) -> tuple[list[dict[str, Any]], str]:
    text = " ".join(str(text or "").split())[:300]
    if not text:
        raise ValueError("text is required")
    extra = {k: v for k, v in extra.items() if v not in (None, "", [])}
    if "opts" in extra:
        labels, values = normalize_options(extra["opts"])
        extra["opts"] = labels
        if any(v != lab for v, lab in zip(values, labels)):
            extra["vals"] = values
        if not labels:
            extra.pop("opts")
    for it in items:  # same wording -> same item (newer details win)
        if it["text"].casefold() == text.casefold():
            if "opts" in extra:
                it.pop("vals", None)
            it.update(extra)
            return items, it["id"]
    item_id = item_id or new_item_id()
    items = items + [{"id": item_id, "text": text, "at": now_ts, **extra}]
    return items[-MAX_DECISIONS:], item_id


def resolve_item(items: list[dict[str, Any]], item_id: str = "", text: str = "") -> tuple[list[dict[str, Any]], bool]:
    needle = " ".join(str(text or "").split()).casefold()
    keep = [it for it in items if not (it["id"] == item_id or (needle and needle in it["text"].casefold()))]
    return keep, len(keep) != len(items)


def _short(text: str, n: int = 28) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def render_decisions(items: list[dict[str, Any]], lang: str, tz_name: str) -> str:
    lines = [f"**{s('dec_title', lang, n=len(items))}**", ""]
    for i, it in enumerate(items, 1):
        when = datetime.fromtimestamp(float(it.get("at", 0)), _tz(tz_name)).strftime("%m-%d")
        lines.append(f"{i}. {it['text']} · {when}")
    rows = []
    for i, it in enumerate(items, 1):
        tags = [f'<tg-button type="callback_data" data="{DECISION_PREFIX}{it["id"]}:{k}">'
                f'{html.escape(f"{i} · {_short(label, 16)}")}</tg-button>'
                for k, label in enumerate(item_options(it, lang))]
        if it.get("msg"):
            tags.append(f'<tg-button type="callback_data" data="{DECISION_PREFIX}{it["id"]}:j">'
                        f'{html.escape(f"{i} · " + s("jump", lang))}</tg-button>')
        rows.append("\n".join(tags))
    buttons = "\n".join(f"<tg-button-row>\n{r}\n</tg-button-row>" for r in rows)
    return "\n".join(lines) + ("\n\n" + buttons if buttons else "")


# ---------------------------------------------------------------- wrap-up card

def _bullets(items: Any) -> list[str]:
    if isinstance(items, str):
        items = [items]
    out = []
    for it in items or []:
        text = " ".join(str(it or "").split())
        if text:
            out.append(f"- {text}")
    return out


def wrapup_markdown(title: str, lang: str, *, summary: str = "", changed: Any = None, verified: Any = None,
                    pending: Any = None) -> str:
    parts = [f"**{' '.join(str(title or '').split()) or '✓'}**"]
    if summary and str(summary).strip():
        parts.append(str(summary).strip())
    for key, items in (("changed", changed), ("verified", verified), ("pending", pending)):
        lines = _bullets(items)
        if lines:
            parts.append(f"**{s(key, lang)}**\n" + "\n".join(lines))
    return "\n\n".join(parts)
