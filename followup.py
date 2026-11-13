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
MAX_OPTIONS = 4
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
    "closed": {"zh": "✓ 已在聊天里处理", "en": "✓ Handled in the chat"},
    "dec_gone": {"zh": "这件事已经处理过了", "en": "Already handled"},
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


# ---------------------------------------------------------------- decisions
# Each decision is its own message: it quotes the message it is about, explains what is being decided and
# carries the action buttons the model chose. It stays pinned until answered. The store only remembers
# which messages are still open, so the agent can list them or close one Alan answered in chat.

def new_item_id() -> str:
    return secrets.token_hex(4)


def decisions_key(chat_id: Any, thread_id: Any) -> str:
    return f"{chat_id}:{thread_id or '-'}"


def normalize_options(options: Any) -> list[dict[str, Any]]:
    """Options as strings or {label, value, style} -> choice buttons (at most MAX_OPTIONS)."""
    out: list[dict[str, Any]] = []
    for o in options or []:
        if isinstance(o, dict):
            label, value, style = str(o.get("label") or "").strip(), str(o.get("value") or "").strip(), o.get("style")
        else:
            label, value, style = str(o or "").strip(), "", None
        if label:
            out.append({"label": label[:40], "type": "choice", "value": value[:300] or None, "style": style})
    return out[:MAX_OPTIONS]


def option_rows(buttons: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Two per row: decision labels are short phrases, three in a row get truncated on phones."""
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def decision_markdown(text: str, detail: str = "") -> str:
    title = " ".join(str(text or "").split())
    if not title:
        raise ValueError("text is required")
    detail = str(detail or "").strip()
    return f"**📌 {title}**" + (f"\n\n{detail}" if detail else "")


def find_open(items: list[dict[str, Any]], item_id: str = "", text: str = "", rid: str = "") -> dict[str, Any] | None:
    needle = " ".join(str(text or "").split()).casefold()
    for it in items:
        if (item_id and it.get("id") == item_id) or (rid and it.get("rid") == rid) \
                or (needle and needle in str(it.get("text", "")).casefold()):
            return it
    return None


def track(items: list[dict[str, Any]], item: dict[str, Any]) -> list[dict[str, Any]]:
    return [it for it in items if it.get("id") != item.get("id")] + [item]


def untrack(items: list[dict[str, Any]], item_id: str) -> list[dict[str, Any]]:
    return [it for it in items if it.get("id") != item_id]


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


# ---------------------------------------------------------------- clarify as a rich card

CLARIFY_LABEL_MAX = 40


def clarify_markdown(question: str, choices: list[Any], clarify_id: str, other_label: str) -> str:
    """Rich-message body for a clarify prompt, styled like a decision card: the question in bold, one
    button per choice carrying its own text (first = recommended, highlighted), then "Other".

    Callback data stays core's ``cl:<id>:<idx|other>`` so Hermes resolves the tap exactly as before.
    Choices too long for a button are listed in full in the body and the button gets a shortened label."""
    q = str(question or "").strip()
    # core tags the first choice " (Recommended)"; the highlighted first button already says that
    labels = [re.sub(r"\s*\((?:Recommended|推荐)\)$", "", " ".join(str(c or "").split())) for c in choices]
    # no ❓ here: core prefixes one when it edits the card into its answered state
    body = [f"**{q}**" if "\n" not in q else q]
    long_ones = [(n, lab) for n, lab in enumerate(labels, 1) if len(lab) > CLARIFY_LABEL_MAX]
    if long_ones:
        body.append("\n".join(f"{n}. {html.escape(lab, quote=False)}" for n, lab in enumerate(labels, 1)))
    short = all(len(lab) <= 14 for lab in labels)
    rows: list[list[str]] = []
    for idx, lab in enumerate(labels):
        text = lab if len(lab) <= CLARIFY_LABEL_MAX else f"{idx + 1}. {lab[:CLARIFY_LABEL_MAX - 4]}…"
        style = ' style="primary"' if idx == 0 else ""
        tag = (f'<tg-button type="callback_data"{style} data="cl:{clarify_id}:{idx}">'
               f"{html.escape(text, quote=False)}</tg-button>")
        if short and rows and len(rows[-1]) < 2:
            rows[-1].append(tag)
        else:
            rows.append([tag])
    rows.append([f'<tg-button type="callback_data" data="cl:{clarify_id}:other">'
                 f"{html.escape(other_label, quote=False)}</tg-button>"])
    buttons = "\n".join("<tg-button-row>\n" + "\n".join(r) + "\n</tg-button-row>" for r in rows)
    return "\n\n".join(body) + "\n\n" + buttons
