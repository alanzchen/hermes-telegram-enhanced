"""telegram-rich-buttons: send Telegram Bot API 10.1 rich messages with buttons from Hermes,
and route choice-button presses back into the originating conversation.

No Hermes source changes: uses ``ctx.register_tool``, ``ctx.register_telegram_handler`` and
``ctx.inject_message``. Choice presses need
``plugins.entries.telegram-rich-buttons.allow_gateway_injection: true`` in the profile config.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .core import (
    CALLBACK_PREFIX, ButtonSpecError, PendingStore, choice_injection_text, choice_list,
    compose_markdown, new_request_id, normalize_buttons, parse_callback_data,
)

logger = logging.getLogger("hermes_plugins.telegram_rich_buttons")

PLUGIN_NAME = "telegram-rich-buttons"
TOOLSET = "telegram_rich"
RICH_MAX_CHARS = 32768
_HTTP_TIMEOUT = 30

_state: dict[str, Any] = {"token": None, "store": None}

SCHEMA = {
    "name": "telegram_rich_send",
    "description": (
        "Send a Telegram rich message (Bot API 10.1 Rich Markdown: headings, tables, task lists, "
        "<details>, quotes, footnotes) with optional buttons into the CURRENT Telegram chat/topic. "
        "Button types: 'choice' (tap is delivered back to this conversation as a new user turn, "
        "like a clarify button), 'url' (opens a link), 'copy' (copies text to the clipboard). "
        "Use for quick replies, option pickers, links and copyable commands/IDs. "
        "The message is sent immediately; do not repeat its content in your normal reply. "
        "After sending choice buttons, end your turn and wait for the press."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "markdown": {
                "type": "string",
                "description": "Rich Markdown body (max ~32k chars). Raw <tg-button> tags are also allowed.",
            },
            "buttons": {
                "type": "array",
                "description": "Rows of buttons; each row is an array of button objects (a flat array = one row).",
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Button text (<=64 chars)."},
                            "type": {"type": "string", "enum": ["choice", "url", "copy"]},
                            "value": {"type": "string", "description": "choice: value reported back (defaults to label)."},
                            "url": {"type": "string", "description": "url: http(s):// or tg:// link."},
                            "copy_text": {"type": "string", "description": "copy: text to copy (<=256 chars)."},
                            "style": {"type": "string", "enum": ["primary", "success", "danger", "link"]},
                        },
                        "required": ["label"],
                    },
                },
            },
            "question": {
                "type": "string",
                "description": "Short description of what the choice buttons decide; echoed back with the press.",
            },
            "align": {"type": "string", "enum": ["left", "center", "right"]},
            "single_use": {
                "type": "boolean",
                "description": "Choice buttons accept only the first press (default true).",
            },
        },
        "required": ["markdown"],
    },
}


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "") or ""
    except Exception:
        return os.environ.get(name, "")


def _token() -> str | None:
    return _state.get("token") or os.environ.get("TELEGRAM_BOT_TOKEN") or None


def _api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = _token()
    if not token:
        return {"ok": False, "description": "Telegram bot token unavailable"}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except Exception as exc:  # network error: never leak the token-bearing URL
        return {"ok": False, "description": type(exc).__name__}


def _result(**kw: Any) -> str:
    return json.dumps(kw, ensure_ascii=False)


def telegram_rich_send(args: dict[str, Any], **_kw: Any) -> str:
    platform = (_session("HERMES_SESSION_PLATFORM") or "").lower()
    chat_id = _session("HERMES_SESSION_CHAT_ID")
    if platform != "telegram" or not chat_id:
        return _result(success=False, error="telegram_rich_send only works inside a Telegram conversation")
    thread_id = _session("HERMES_SESSION_THREAD_ID")
    session_key = _session("HERMES_SESSION_KEY")
    user_id = _session("HERMES_SESSION_USER_ID")

    body = str(args.get("markdown") or "")
    try:
        rows = normalize_buttons(args.get("buttons"))
    except ButtonSpecError as exc:
        return _result(success=False, error=str(exc))
    choices = choice_list(rows)
    if choices and not session_key:
        return _result(success=False, error="no session key bound; choice buttons cannot route back")
    if choices and not user_id:
        return _result(success=False, error="no requesting user bound; refusing unauthenticated choice buttons")

    align = args.get("align")
    request_id = new_request_id() if choices else None
    markdown = compose_markdown(body, rows, request_id, align=align)
    if not markdown.strip():
        return _result(success=False, error="empty message")
    if len(markdown) > RICH_MAX_CHARS:
        return _result(success=False, error=f"message exceeds {RICH_MAX_CHARS} characters")

    payload: dict[str, Any] = {"chat_id": int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id,
                               "rich_message": {"markdown": markdown}}
    if thread_id and thread_id.isdigit():
        payload["message_thread_id"] = int(thread_id)

    store: PendingStore = _state["store"]
    if request_id:
        store.put({
            "id": request_id, "created_at": time.time(), "session_key": session_key,
            "chat_id": str(chat_id), "thread_id": thread_id, "user_id": str(user_id),
            "question": str(args.get("question") or "")[:500], "choices": choices,
            "single_use": bool(args.get("single_use", True)), "body": body, "rows": rows,
            "align": align, "message_id": None, "answered": None,
        })

    res = _api("sendRichMessage", payload)
    if not res.get("ok"):
        return _result(success=False, error=str(res.get("description") or "sendRichMessage failed"))
    message_id = (res.get("result") or {}).get("message_id")
    if request_id:
        store.update(request_id, message_id=message_id)
    return _result(success=True, message_id=message_id, request_id=request_id,
                   choice_buttons=len(choices),
                   note="Delivered. Choice presses arrive as a new user message." if choices else "Delivered.")


def _make_callback(ctx: Any):
    async def on_callback(update: Any, context: Any) -> None:
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        parsed = parse_callback_data(getattr(query, "data", "") or "")
        if parsed is None:
            await query.answer()
            return
        request_id, index = parsed
        user = getattr(query, "from_user", None)
        status, entry, choice = _state["store"].claim(request_id, index, str(getattr(user, "id", "")))
        if status != "ok":
            text = {"expired": "已过期", "unauthorized": "这个按钮不是给你的",
                    "used": "已经选过了", "invalid": "无效按钮"}.get(status, "无法处理")
            await query.answer(text=text)
            return
        await query.answer(text=f"已选择：{choice['label']}"[:190])

        # Re-render: drop choice buttons, keep link/copy buttons, note the selection.
        try:
            markdown = compose_markdown(entry.get("body") or "", entry.get("rows") or [], None,
                                        include_choices=False, footer=f"> ✅ 已选择：**{choice['label']}**",
                                        align=entry.get("align"))
            message = getattr(query, "message", None)
            chat_id = getattr(message, "chat_id", None) or entry.get("chat_id")
            message_id = getattr(message, "message_id", None) or entry.get("message_id")
            if chat_id and message_id:
                await context.bot.do_api_request("editMessageText", api_kwargs={
                    "chat_id": int(chat_id), "message_id": int(message_id),
                    "rich_message": {"markdown": markdown}})
        except Exception:
            logger.debug("telegram-rich-buttons: could not edit message after press", exc_info=True)

        who = getattr(user, "first_name", None) or "用户"
        ok = False
        try:
            ok = ctx.inject_message(choice_injection_text(entry, choice, who), session_key=entry["session_key"])
        except Exception:
            logger.warning("telegram-rich-buttons: inject_message failed", exc_info=True)
        if not ok:
            logger.warning("telegram-rich-buttons: press %s:%s not routed (allow_gateway_injection?)",
                           request_id, index)
    return on_callback


def _profile_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def register(ctx: Any) -> None:
    store = PendingStore(_profile_home() / "telegram-rich-buttons")
    try:
        store.ensure()
    except OSError:
        logger.warning("telegram-rich-buttons: state directory unavailable", exc_info=True)
    _state["store"] = store

    ctx.register_tool(
        name="telegram_rich_send", toolset=TOOLSET, schema=SCHEMA,
        handler=lambda args, **kw: telegram_rich_send(args or {}, **kw),
        description="Telegram rich messages with link/copy/choice buttons", emoji="🔘",
    )

    def factory(native: Any, adapter: Any) -> None:
        if native is None:
            return
        from telegram.ext import CallbackQueryHandler
        token = getattr(getattr(native, "bot", None), "token", None)
        if token:
            _state["token"] = token
        native.add_handler(CallbackQueryHandler(_make_callback(ctx), pattern=r"^" + CALLBACK_PREFIX))

    ctx.register_telegram_handler(factory)
