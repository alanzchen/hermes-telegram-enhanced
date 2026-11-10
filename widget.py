"""HTML widgets (Bot API 10.4): build, validate and route taps. Pure helpers; no network here.

A widget is one sandboxed HTML document drawn inside the message bubble (no network, taps only). Its
code is fixed once sent; the bot changes its state (``chat_data``). The plugin injects a tiny runtime so
model-written HTML only has to mark buttons with ``data-action="key"``: a tap sends ``a:<key>`` to the
bot (``widget_query``), the bot answers, records the choice in ``chat_data.chosen`` and hands the press
to the conversation like a choice button. Limits: 32768 bytes of HTML, 4096 bytes after zlib level 9,
1024 bytes of chat_data, 1-64 bytes per request.
"""
from __future__ import annotations

import html as _html
import json
import re
import time
import zlib
from typing import Any

MAX_HTML = 32768
MAX_COMPRESSED = 4096
MAX_DATA = 1024
ACTION_PREFIX = "a:"
STORE_TTL = 30 * 86400
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# ES2023 in QuickJS; only APIs the widget profile lists (querySelector, addEventListener, classList).
_RUNTIME = (
    "<style>button.hte-chosen::before{content:'\\2713  '}</style>"
    "<script>(function(){var W=window.TelegramWidget;if(!W)return;var K=__KEYS__;"
    "function S(){try{return JSON.parse(W.chatData||'{}')||{}}catch(e){return{}}}"
    "function B(k){return document.querySelector('[data-action=\"'+k+'\"]')}"
    "function P(){var c=S().chosen;K.forEach(function(k){var b=B(k);if(!b||c==null)return;"
    "b.disabled=true;if(k===c)b.classList.add('hte-chosen')})}"
    "K.forEach(function(k){var b=B(k);if(b)b.addEventListener('click',function(){if(b.disabled)return;"
    "b.disabled=true;W.request('a:'+k).then(function(r){var d={};try{d=JSON.parse(r.result||'{}')}catch(x){}"
    "if(!d.ok||d.again)b.disabled=false},function(){b.disabled=false})})});"
    "W.onEvent('chatDataChanged',P);P();if(W.ready)W.ready()})();</script>"
)


class WidgetError(ValueError):
    pass


def normalize_actions(actions: Any) -> list[dict[str, str]]:
    """[{key, label, value?}] -> validated list (max 6). Keys are what the HTML's data-action names."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, a in enumerate(actions or []):
        if isinstance(a, str):
            a = {"label": a}
        if not isinstance(a, dict):
            continue
        label = " ".join(str(a.get("label") or "").split())[:40]
        key = str(a.get("key") or "").strip() or f"o{i + 1}"
        if not label and not a.get("value"):
            continue
        if not _KEY_RE.match(key):
            raise WidgetError(f"action key {key!r}: use 1-32 letters, digits, - or _")
        if key in seen:
            raise WidgetError(f"duplicate action key {key!r}")
        seen.add(key)
        value = str(a.get("value") or "").strip()
        out.append({"key": key, "label": label or key, "value": value})
    if len(out) > 6:
        raise WidgetError("at most 6 actions")
    return out


def chat_data_obj(value: Any) -> dict[str, Any]:
    """chat_data must be a JSON object (the runtime reserves ``chosen``)."""
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise WidgetError("chat_data must be a JSON object") from exc
    if not isinstance(value, dict):
        raise WidgetError("chat_data must be a JSON object")
    return value


def encode_data(obj: dict[str, Any]) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) > MAX_DATA:
        raise WidgetError(f"chat_data is {len(text.encode())} bytes; the limit is {MAX_DATA}")
    return text


def _buttons_html(actions: list[dict[str, str]]) -> str:
    btns = "".join(f'<button data-action="{a["key"]}">{_html.escape(a["label"])}</button>' for a in actions)
    # the message time sits over the widget's bottom corner when there is no caption: keep taps clear of it
    return f'<div style="display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 18px">{btns}</div>'


def build_html(body: str, actions: list[dict[str, str]]) -> str:
    """Model HTML + (auto buttons when the HTML has no data-action elements) + the tap runtime."""
    doc = str(body or "")
    if "\x00" in doc:
        raise WidgetError("html must not contain NUL")
    if not doc.strip():
        raise WidgetError("html is required")
    if actions:
        missing = [a for a in actions if f'data-action="{a["key"]}"' not in doc]
        if len(missing) == len(actions):
            doc += _buttons_html(actions)
        elif missing:
            raise WidgetError("html has data-action buttons but not for: " + ", ".join(a["key"] for a in missing))
        doc += _RUNTIME.replace("__KEYS__", json.dumps([a["key"] for a in actions]))
    return doc


def size_report(doc: str) -> tuple[int, int]:
    raw = doc.encode()
    return len(raw), len(zlib.compress(raw, 9))


def check_size(doc: str) -> tuple[int, int]:
    raw, packed = size_report(doc)
    if raw > MAX_HTML:
        raise WidgetError(f"html is {raw} bytes; the limit is {MAX_HTML}")
    if packed > MAX_COMPRESSED:
        raise WidgetError(f"html compresses to {packed} bytes (zlib -9); the limit is {MAX_COMPRESSED}. "
                          "Shorten text/CSS, drop inline images.")
    return raw, packed


def parse_query(data: str) -> str | None:
    """'a:<key>' -> key; anything else -> None."""
    data = str(data or "")
    if data.startswith(ACTION_PREFIX) and _KEY_RE.match(data[len(ACTION_PREFIX):]):
        return data[len(ACTION_PREFIX):]
    return None


def store_key(chat_id: Any, message_id: Any) -> str:
    return f"{chat_id}:{message_id}"


def claim(rec: dict[str, Any] | None, key: str, user_id: str) -> tuple[str, dict[str, str] | None]:
    """Decide a tap against the stored record; mutates ``rec`` on success. -> (status, action)."""
    if not rec:
        return "gone", None
    if str(user_id) != str(rec.get("user_id") or ""):
        return "not_yours", None
    action = next((a for a in rec.get("actions") or [] if a["key"] == key), None)
    if action is None:
        return "invalid", None
    if rec.get("once", True) and rec.get("chosen") is not None:
        return "used", None
    rec["chosen"] = key
    rec["pressed_at"] = time.time()
    return "ok", action


def injection_text(rec: dict[str, Any], action: dict[str, str], who: str) -> str:
    lines = [f"[Telegram 小组件] {who} 点击了「{action['label']}」。"]
    if action.get("value") and action["value"] != action["label"]:
        lines.append(f"value: {action['value']}")
    if rec.get("title"):
        lines.append(f"问题: {rec['title']}")
    lines.append(f"widget: {rec.get('chat_id')}:{rec.get('message_id')}")
    return "\n".join(lines)


def prune(data: dict[str, Any], now: float) -> None:
    for k in [k for k, v in data.items() if now - float((v or {}).get("created_at") or 0) > STORE_TTL]:
        data.pop(k, None)
