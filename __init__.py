"""Hermes Telegram Enhanced — Bot API 10.x features for Hermes Agent without patching Hermes.

Tools
  telegram_rich_send     Rich Markdown + buttons (choice/url/copy) + maps/galleries + local-time entities.
  telegram_status_card   One pinned, edit-in-place status message per key in the current chat/topic.
  telegram_guest_reply   Answer a cross-chat (Guest Mode) @mention routed into a private DM topic.

Gateway behaviour (via ctx.register_telegram_handler / hooks / inject_message)
  * choice-button presses come back to the originating conversation as a new user turn;
  * a "Thinking…" draft with a native Stop button is shown in private chats while a turn runs;
    pressing Stop issues /stop for that conversation;
  * Guest Mode @mentions from allowed users are answered in-place and routed to a dedicated DM topic.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .core import (
    CALLBACK_PREFIX, DEFAULT_TZ, ButtonSpecError, JsonStore, PendingStore, card_key, choice_injection_text,
    choice_list, compose_markdown, expand_time_placeholders, new_request_id, normalize_buttons,
    parse_callback_data, time_fallback, parse_when, TIME_PLACEHOLDER_RE, bulletize_plain, has_markdown_list,
)

logger = logging.getLogger("hermes_plugins.telegram_enhanced")

TOOLSET = "telegram_enhanced"
RICH_MAX_CHARS = 32768
_HTTP_TIMEOUT = 30
DRAFT_REFRESH_SECONDS = 8  # any bot message clears the draft; re-show it quickly
DRAFT_MAX_SECONDS = 45 * 60
GUEST_TTL_SECONDS = 24 * 3600

_state: dict[str, Any] = {
    "ctx": None, "token": None, "adapter": None, "loop": None,
    "pending": None, "cards": None, "guests": None, "meta": None,
    "turns": {},           # session_id -> turn record (processing draft)
    "stops": {},           # turn_id -> stop-message record
    "turns_lock": threading.Lock(),
}


# ---------------------------------------------------------------- plumbing

def _setting(key: str, default: Any) -> Any:
    ctx = _state.get("ctx")
    try:
        value = ctx.get_config(key, default) if ctx is not None else default
    except Exception:
        return default
    return default if value is None else value


def _display_tz() -> str:
    return str(_setting("display_tz", DEFAULT_TZ) or DEFAULT_TZ)


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
    except Exception as exc:  # never leak the token-bearing URL
        return {"ok": False, "description": type(exc).__name__}


def _result(**kw: Any) -> str:
    return json.dumps(kw, ensure_ascii=False)


def _int_or_str(value: str) -> Any:
    return int(value) if str(value).lstrip("-").isdigit() else value


def _current_target() -> tuple[dict[str, str] | None, str | None]:
    platform = (_session("HERMES_SESSION_PLATFORM") or "").lower()
    chat_id = _session("HERMES_SESSION_CHAT_ID")
    if platform != "telegram" or not chat_id:
        return None, "only works inside a Telegram conversation"
    return {
        "chat_id": chat_id, "thread_id": _session("HERMES_SESSION_THREAD_ID"),
        "session_key": _session("HERMES_SESSION_KEY"), "user_id": _session("HERMES_SESSION_USER_ID"),
        "chat_type": _session("HERMES_SESSION_CHAT_TYPE"), "message_id": _session("HERMES_SESSION_MESSAGE_ID"),
        "user_name": _session("HERMES_SESSION_USER_NAME"),
    }, None


def _build(args: dict[str, Any], target: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
    """Compose the markdown for a message; returns (markdown, pending-entry-or-None)."""
    rows = normalize_buttons(args.get("buttons"))
    choices = choice_list(rows)
    if choices and not target.get("session_key"):
        raise ButtonSpecError("no session key bound; choice buttons cannot route back")
    if choices and not target.get("user_id"):
        raise ButtonSpecError("no requesting user bound; refusing unauthenticated choice buttons")
    request_id = new_request_id() if choices else None
    body = str(args.get("markdown") or "")
    kw = dict(align=args.get("align"), maps=args.get("maps"), gallery=args.get("gallery"), display_tz=_display_tz())
    markdown = compose_markdown(body, rows, request_id, **kw)
    if not markdown.strip():
        raise ButtonSpecError("empty message")
    if len(markdown) > RICH_MAX_CHARS:
        raise ButtonSpecError(f"message exceeds {RICH_MAX_CHARS} characters")
    entry = None
    if request_id:
        entry = {
            "id": request_id, "created_at": time.time(), "session_key": target["session_key"],
            "chat_id": str(target["chat_id"]), "thread_id": target.get("thread_id"),
            "user_id": str(target["user_id"]), "question": str(args.get("question") or "")[:500],
            "choices": choices, "single_use": bool(args.get("single_use", True)), "body": body, "rows": rows,
            "render": {k: v for k, v in kw.items() if k != "display_tz"}, "message_id": None, "answered": None,
        }
    return markdown, entry


def _send_rich(target: dict[str, str], markdown: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": _int_or_str(target["chat_id"]), "rich_message": {"markdown": markdown}}
    if target.get("thread_id") and str(target["thread_id"]).isdigit():
        payload["message_thread_id"] = int(target["thread_id"])
    res = _api("sendRichMessage", payload)
    desc = str(res.get("description") or "")
    if not res.get("ok") and "NO_MEDIA_FOUND" in desc:
        res["description"] = desc + " (Telegram could not fetch a media URL; use direct, publicly reachable image/video links)"
    return res


# ---------------------------------------------------------------- tool: rich send

_BUTTONS_SCHEMA = {
    "type": "array",
    "description": "Rows of buttons; each row is an array of button objects (a flat array = one row).",
    "items": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string", "description": "Button text (<=64 chars)."},
        "type": {"type": "string", "enum": ["choice", "url", "copy"]},
        "value": {"type": "string", "description": "choice: value reported back (defaults to label)."},
        "url": {"type": "string", "description": "url: http(s):// or tg:// link."},
        "copy_text": {"type": "string", "description": "copy: text to copy (<=256 chars)."},
        "style": {"type": "string", "enum": ["primary", "success", "danger", "link"]},
    }, "required": ["label"]}},
}
_MAPS_SCHEMA = {
    "type": "array", "description": "Map blocks shown inside the message.",
    "items": {"type": "object", "properties": {
        "lat": {"type": "number"}, "long": {"type": "number"},
        "zoom": {"type": "integer", "description": "1-20, default 15"},
        "caption": {"type": "string"}}, "required": ["lat", "long"]},
}
_GALLERY_SCHEMA = {
    "type": "object", "description": "Photo/video collage or slideshow from public http(s) URLs.",
    "properties": {
        "type": {"type": "string", "enum": ["collage", "slideshow"]},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "url": {"type": "string"}, "caption": {"type": "string"}}, "required": ["url"]}}},
    "required": ["items"],
}
_TIME_HELP = ("Write times as {{time:ISO8601|IANA_TZ|FORMAT}} (e.g. {{time:2026-11-08T09:30|America/Chicago}}); "
              "they render in the reader's own timezone/locale. FORMAT: wDT (default), t, D, r (relative).")

RICH_SCHEMA = {
    "name": "telegram_rich_send",
    "description": (
        "Send a Telegram rich message into the CURRENT Telegram chat/topic: Rich Markdown (tables, task lists, "
        "<details>, quotes, footnotes, math), optional button rows, maps and photo collages/slideshows. "
        "Button types: 'choice' (a tap comes back to this conversation as a new user turn), 'url', 'copy'. "
        + _TIME_HELP + " The message is sent immediately; do not repeat it in your normal reply. "
        "After sending choice buttons, end your turn and wait for the press."
    ),
    "parameters": {"type": "object", "properties": {
        "markdown": {"type": "string", "description": "Rich Markdown body (max ~32k chars)."},
        "buttons": _BUTTONS_SCHEMA, "maps": _MAPS_SCHEMA, "gallery": _GALLERY_SCHEMA,
        "question": {"type": "string", "description": "What the choice buttons decide; echoed back with the press."},
        "align": {"type": "string", "enum": ["left", "center", "right"]},
        "single_use": {"type": "boolean", "description": "Choice buttons accept only the first press (default true)."},
    }, "required": ["markdown"]},
}


def telegram_rich_send(args: dict[str, Any], **_kw: Any) -> str:
    target, err = _current_target()
    if err:
        return _result(success=False, error=f"telegram_rich_send {err}")
    try:
        markdown, entry = _build(args, target)
    except ButtonSpecError as exc:
        return _result(success=False, error=str(exc))
    if entry:
        _state["pending"].put(entry)
    res = _send_rich(target, markdown)
    if not res.get("ok"):
        return _result(success=False, error=str(res.get("description") or "sendRichMessage failed"))
    message_id = (res.get("result") or {}).get("message_id")
    if entry:
        _state["pending"].update(entry["id"], message_id=message_id)
    return _result(success=True, message_id=message_id, request_id=entry["id"] if entry else None,
                   choice_buttons=len(entry["choices"]) if entry else 0)


# ---------------------------------------------------------------- tool: status cards

CARD_SCHEMA = {
    "name": "telegram_status_card",
    "description": (
        "Maintain ONE pinned status message per key in the CURRENT Telegram chat/topic and edit it in place "
        "instead of posting new progress messages (no spam; latest state is always at the top). "
        "action=upsert creates (and pins) or edits the card; action=unpin keeps it but unpins; action=delete "
        "removes it. Same Rich Markdown, buttons, maps and {{time:...}} placeholders as telegram_rich_send. "
        "Use for long-running projects, trips, deployments or any status Alan checks repeatedly."
    ),
    "parameters": {"type": "object", "properties": {
        "key": {"type": "string", "description": "Stable card id within this chat/topic, e.g. 'trip-tokyo'."},
        "action": {"type": "string", "enum": ["upsert", "unpin", "delete"]},
        "markdown": {"type": "string", "description": "Full card content (replaces the previous content)."},
        "buttons": _BUTTONS_SCHEMA, "maps": _MAPS_SCHEMA,
        "pin": {"type": "boolean", "description": "Pin when created (default true)."},
        "question": {"type": "string"},
    }, "required": ["key"]},
}


def _not_modified(res: dict[str, Any]) -> bool:
    return "not modified" in str(res.get("description") or "").lower()


def _gone(res: dict[str, Any]) -> bool:
    d = str(res.get("description") or "").lower()
    return any(s in d for s in ("not found", "message_id_invalid", "can't be edited", "message to edit"))


def telegram_status_card(args: dict[str, Any], **_kw: Any) -> str:
    target, err = _current_target()
    if err:
        return _result(success=False, error=f"telegram_status_card {err}")
    try:
        key = card_key(target["chat_id"], target.get("thread_id"), str(args.get("key") or ""))
    except ButtonSpecError as exc:
        return _result(success=False, error=str(exc))
    cards: JsonStore = _state["cards"]
    action = str(args.get("action") or "upsert").lower()
    card = cards.get(key)
    chat = _int_or_str(target["chat_id"])

    if action in {"unpin", "delete"}:
        if not card:
            return _result(success=False, error="no such card")
        _api("unpinChatMessage", {"chat_id": chat, "message_id": card["message_id"]})
        if action == "delete":
            _api("deleteMessage", {"chat_id": chat, "message_id": card["message_id"]})
            cards.pop(key)
        else:
            card["pinned"] = False
            cards.set(key, card)
        return _result(success=True, action=action, message_id=card["message_id"])

    if not str(args.get("markdown") or "").strip():
        return _result(success=False, error="markdown is required for upsert")
    try:
        markdown, entry = _build(args, target)
    except ButtonSpecError as exc:
        return _result(success=False, error=str(exc))
    if entry:
        _state["pending"].put(entry)

    if card:
        res = _api("editMessageText", {"chat_id": chat, "message_id": card["message_id"],
                                       "rich_message": {"markdown": markdown}})
        if res.get("ok") or _not_modified(res):
            card["updated_at"] = time.time()
            cards.set(key, card)
            if entry:
                _state["pending"].update(entry["id"], message_id=card["message_id"])
            return _result(success=True, action="edited", message_id=card["message_id"])
        if not _gone(res):
            return _result(success=False, error=str(res.get("description") or "edit failed"))

    res = _send_rich(target, markdown)
    if not res.get("ok"):
        return _result(success=False, error=str(res.get("description") or "sendRichMessage failed"))
    message_id = (res.get("result") or {}).get("message_id")
    pinned = False
    if args.get("pin", True):
        pin = _api("pinChatMessage", {"chat_id": chat, "message_id": message_id, "disable_notification": True})
        pinned = bool(pin.get("ok"))
    cards.set(key, {"message_id": message_id, "chat_id": str(target["chat_id"]),
                    "thread_id": target.get("thread_id"), "pinned": pinned,
                    "created_at": time.time(), "updated_at": time.time()})
    if entry:
        _state["pending"].update(entry["id"], message_id=message_id)
    return _result(success=True, action="created", message_id=message_id, pinned=pinned)


# ---------------------------------------------------------------- choice callbacks

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
        status, entry, choice = _state["pending"].claim(request_id, index, str(getattr(user, "id", "")))
        if status != "ok":
            text = {"expired": "已过期", "unauthorized": "这个按钮不是给你的",
                    "used": "已经选过了", "invalid": "无效按钮"}.get(status, "无法处理")
            await query.answer(text=text)
            return
        await query.answer(text=f"已选择：{choice['label']}"[:190])
        try:
            render = entry.get("render") or {"align": entry.get("align")}
            markdown = compose_markdown(entry.get("body") or "", entry.get("rows") or [], None,
                                        include_choices=False, footer=f"> ✅ 已选择：**{choice['label']}**",
                                        display_tz=_display_tz(), **render)
            message = getattr(query, "message", None)
            chat_id = getattr(message, "chat_id", None) or entry.get("chat_id")
            message_id = getattr(message, "message_id", None) or entry.get("message_id")
            if chat_id and message_id:
                await context.bot.do_api_request("editMessageText", api_kwargs={
                    "chat_id": int(chat_id), "message_id": int(message_id),
                    "rich_message": {"markdown": markdown}})
        except Exception:
            logger.debug("telegram-enhanced: could not edit message after press", exc_info=True)
        who = getattr(user, "first_name", None) or "用户"
        ok = False
        try:
            ok = ctx.inject_message(choice_injection_text(entry, choice, who), session_key=entry["session_key"])
        except Exception:
            logger.warning("telegram-enhanced: inject_message failed", exc_info=True)
        if not ok:
            logger.warning("telegram-enhanced: press %s:%s not routed (allow_gateway_injection?)", request_id, index)
    return on_callback


# ---------------------------------------------------------------- stop button (drafts)

def _draft_id(session_id: str) -> int:
    return int(hashlib.sha256(f"{session_id}:{time.time()}".encode()).hexdigest()[:8], 16) or 1


_GENERATION = object()
sys._hte_draft_generation = _GENERATION  # a plugin reload retires the previous module's draft threads


def _draft_loop(rec: dict[str, Any]) -> None:
    stop: threading.Event = rec["stop"]
    started = time.time()
    logger.info("telegram-enhanced: stop-button draft started for chat %s thread %s", rec["chat_id"], rec["thread_id"])
    try:
        _draft_loop_body(rec, stop, started)
    finally:
        rec["alive"] = False
        with _state["turns_lock"]:
            if _state["turns"].get(rec["session_id"]) is rec:
                _state["turns"].pop(rec["session_id"], None)


def _draft_loop_body(rec: dict[str, Any], stop: threading.Event, started: float) -> None:
    while (not stop.is_set() and time.time() - started < DRAFT_MAX_SECONDS
           and getattr(sys, "_hte_draft_generation", None) is _GENERATION):
        # can_stop=True makes Telegram clients swap Send for Stop and queue the user's messages, which
        # breaks mid-turn steering; off by default (config: stop_button_can_stop).
        payload = {"chat_id": int(rec["chat_id"]), "draft_id": rec["draft_id"],
                   "can_stop": bool(_setting("stop_button_can_stop", False)),
                   "rich_message": {"markdown": "<tg-thinking>小助手处理中…</tg-thinking>"}}
        if rec.get("thread_id"):
            payload["message_thread_id"] = int(rec["thread_id"])
        res = _api("sendRichMessageDraft", payload)
        if not res.get("ok"):
            logger.info("telegram-enhanced: draft refresh failed: %s", res.get("description"))
            if "draft" in str(res.get("description") or "").lower():
                break
        stop.wait(DRAFT_REFRESH_SECONDS)


def _on_pre_llm_call(session_id: str = "", platform: str = "", turn_id: Any = "",
                     parent_session_id: str = "", **_kw: Any) -> None:
    try:
        _patch_telegram_lists()
    except Exception:
        logger.debug("telegram-enhanced: list patch failed", exc_info=True)
    if (platform or "").lower() == "telegram" and session_id:
        try:
            _stop_begin(session_id, str(turn_id or ""), parent_session_id or "")
        except Exception:
            logger.debug("telegram-enhanced: stop message begin failed", exc_info=True)
    try:
        if not _setting("stop_button", False) or (platform or "").lower() != "telegram" or not session_id:
            return None
        chat_id = _session("HERMES_SESSION_CHAT_ID")
        thread_id = _session("HERMES_SESSION_THREAD_ID")
        if _session("HERMES_SESSION_CHAT_TYPE") != "dm" or not chat_id.lstrip("-").isdigit():
            return None
        if thread_id and not thread_id.isdigit():
            thread_id = ""
        with _state["turns_lock"]:
            # A session reuses its session_id every turn: a leftover record means the previous turn ended
            # without post_llm_call (interrupted/steered), so retire it and start fresh.
            old = _state["turns"].pop(session_id, None)
            if old:
                old["stop"].set()
            # One draft per chat/topic: a subagent shares its live parent's draft.
            if any(r["chat_id"] == chat_id and r["thread_id"] == thread_id and r.get("alive", True)
                   for r in _state["turns"].values()):
                return None
            rec = {"session_id": session_id, "chat_id": chat_id, "thread_id": thread_id,
                   "user_id": _session("HERMES_SESSION_USER_ID") or chat_id,
                   "user_name": _session("HERMES_SESSION_USER_NAME"),
                   "message_id": _session("HERMES_SESSION_MESSAGE_ID"),
                   "draft_id": _draft_id(session_id), "stop": threading.Event()}
            _state["turns"][session_id] = rec
        threading.Thread(target=_draft_loop, args=(rec,), name="tg-enhanced-draft", daemon=True).start()
    except Exception:
        logger.debug("telegram-enhanced: pre_llm_call failed", exc_info=True)
    return None


def _end_turn(session_id: str) -> None:
    with _state["turns_lock"]:
        rec = _state["turns"].pop(session_id, None)
    if rec:
        rec["stop"].set()


def _on_post_llm_call(session_id: str = "", turn_id: Any = "", **_kw: Any) -> None:
    _end_turn(session_id)
    _stop_finish(turn_id=str(turn_id or ""))


def _on_session_end(session_id: str = "", **_kw: Any) -> None:
    _end_turn(session_id)
    _stop_finish(session_id=session_id)


def _on_loop_stopped(session_key: str = "", **_kw: Any) -> None:
    # session_key is not the session_id: stop every turn in the same chat/thread.
    parts = (session_key or "").split(":")
    with _state["turns_lock"]:
        doomed = [sid for sid, r in _state["turns"].items()
                  if r["chat_id"] in parts and (not r["thread_id"] or r["thread_id"] in parts)]
    for sid in doomed:
        _end_turn(sid)
    if parts:
        _stop_finish(chat_thread=(parts, None))


async def _on_stopped_generation(update: Any, _context: Any) -> None:
    data = (getattr(update, "api_kwargs", None) or {}).get("stopped_message_generation")
    if not isinstance(data, dict):
        return
    draft_id = data.get("draft_id")
    with _state["turns_lock"]:
        rec = next((r for r in _state["turns"].values() if r["draft_id"] == draft_id), None)
    if rec is None:
        return
    _end_turn(rec["session_id"])
    adapter = _state.get("adapter")
    if adapter is None:
        return
    try:
        from gateway.platforms.event import MessageEvent, MessageType
        source = adapter.build_source(chat_id=rec["chat_id"], chat_type="dm", user_id=rec["user_id"],
                                      user_name=rec.get("user_name") or None, thread_id=rec["thread_id"] or None,
                                      message_id=rec.get("message_id") or None)
        event = MessageEvent(text="/stop", message_type=MessageType.COMMAND, source=source,
                             user_id=rec["user_id"], user_name=rec.get("user_name") or None,
                             message_id=rec.get("message_id") or None)
        await adapter.handle_message(event)
        logger.info("telegram-enhanced: Stop button -> /stop for chat %s thread %s", rec["chat_id"], rec["thread_id"])
    except Exception:
        logger.warning("telegram-enhanced: could not dispatch /stop", exc_info=True)


# ---------------------------------------------------------------- stop message (inline button)
# A processing draft makes Telegram clients queue the user's messages (no mid-turn steering), so the
# Stop affordance is an ordinary silent message with an inline button, deleted when the turn ends.

STOP_PREFIX = "hs:"
_STOP_DATA_RE = re.compile(r"^hs:([0-9a-f]{12})$")
STOP_MESSAGE_TEXT = "⏳ 小助手处理中, 需要时点「停止」"


def _stop_store() -> JsonStore | None:
    return _state.get("stopmsgs")


def _stop_forget(message_id: int) -> None:
    store = _stop_store()
    if store is not None:
        try:
            with store.locked() as data:
                data.pop(str(message_id), None)
        except Exception:
            logger.debug("telegram-enhanced: stop store update failed", exc_info=True)


def _stop_delete(chat_id: str, message_id: int) -> None:
    res = _api("deleteMessage", {"chat_id": int(chat_id), "message_id": int(message_id)})
    if not res.get("ok"):
        logger.info("telegram-enhanced: stop message %s delete failed: %s", message_id, res.get("description"))
    _stop_forget(message_id)


def _stop_cleanup_leftovers(chat_id: str, thread_id: str, keep: int | None = None) -> None:
    """Delete stop messages a previous turn/plugin generation left behind in this chat/topic."""
    store = _stop_store()
    if store is None:
        return
    try:
        with store.locked() as data:
            doomed = [int(mid) for mid, v in data.items()
                      if v.get("chat_id") == chat_id and (v.get("thread_id") or "") == (thread_id or "")
                      and int(mid) != (keep or 0)]
    except Exception:
        return
    for mid in doomed:
        _stop_delete(chat_id, mid)


def _stop_fire(rec: dict[str, Any]) -> None:
    if rec["cancel"].wait(float(_setting("stop_message_delay_seconds", 6))):
        return
    payload: dict[str, Any] = {
        "chat_id": int(rec["chat_id"]), "text": STOP_MESSAGE_TEXT, "disable_notification": True,
        "reply_markup": {"inline_keyboard": [[{"text": "⏹ 停止", "callback_data": STOP_PREFIX + rec["token"]}]]}}
    if rec["thread_id"]:
        payload["message_thread_id"] = int(rec["thread_id"])
    res = _api("sendMessage", payload)
    if not res.get("ok"):
        logger.info("telegram-enhanced: stop message send failed: %s", res.get("description"))
        return
    mid = int(res["result"]["message_id"])
    store = _stop_store()
    if store is not None:
        try:
            store.set(str(mid), {"chat_id": rec["chat_id"], "thread_id": rec["thread_id"], "ts": time.time()})
        except Exception:
            pass
    with _state["turns_lock"]:
        rec["message_id_sent"] = mid
        ended = rec["ended"]
    if ended:  # the turn finished while we were sending
        _stop_delete(rec["chat_id"], mid)


def _stop_end(rec: dict[str, Any]) -> None:
    with _state["turns_lock"]:
        rec["ended"] = True
        rec["cancel"].set()
        mid = rec.get("message_id_sent")
        _state["stops"].pop(rec["key"], None)
    if mid:
        _stop_delete(rec["chat_id"], mid)


def _stop_begin(session_id: str, turn_id: str, parent_session_id: str) -> None:
    if not _setting("stop_message", True) or parent_session_id:
        return  # subagents share the parent's button
    chat_id = _session("HERMES_SESSION_CHAT_ID")
    thread_id = _session("HERMES_SESSION_THREAD_ID")
    if _session("HERMES_SESSION_CHAT_TYPE") != "dm" or not chat_id.lstrip("-").isdigit():
        return
    if thread_id and not thread_id.isdigit():
        thread_id = ""
    with _state["turns_lock"]:
        stale = [r for r in _state["stops"].values()
                 if r["chat_id"] == chat_id and r["thread_id"] == thread_id]
    for r in stale:  # a new top-level turn here means earlier ones are over (interrupted/steered)
        _stop_end(r)
    key = turn_id or session_id
    rec = {"key": key, "session_id": session_id, "chat_id": chat_id, "thread_id": thread_id,
           "user_id": _session("HERMES_SESSION_USER_ID") or chat_id,
           "user_name": _session("HERMES_SESSION_USER_NAME"),
           "message_id": _session("HERMES_SESSION_MESSAGE_ID"),
           "token": secrets.token_hex(6), "cancel": threading.Event(), "ended": False}
    with _state["turns_lock"]:
        _state["stops"][key] = rec

    def run() -> None:
        _stop_cleanup_leftovers(chat_id, thread_id)
        _stop_fire(rec)
    threading.Thread(target=run, name="tg-enhanced-stopmsg", daemon=True).start()


def _stop_finish(turn_id: str = "", session_id: str = "", chat_thread: tuple[list[str], None] | None = None) -> None:
    with _state["turns_lock"]:
        recs = list(_state["stops"].values())
    for r in recs:
        if (turn_id and r["key"] == turn_id) or (session_id and r["session_id"] == session_id) or (
                chat_thread is not None and r["chat_id"] in chat_thread[0]
                and (not r["thread_id"] or r["thread_id"] in chat_thread[0])):
            _stop_end(r)


async def _dispatch_stop(rec: dict[str, Any]) -> bool:
    adapter = _state.get("adapter")
    if adapter is None:
        return False
    try:
        from gateway.platforms.event import MessageEvent, MessageType
        source = adapter.build_source(chat_id=rec["chat_id"], chat_type="dm", user_id=rec["user_id"],
                                      user_name=rec.get("user_name") or None, thread_id=rec["thread_id"] or None,
                                      message_id=rec.get("message_id") or None)
        event = MessageEvent(text="/stop", message_type=MessageType.COMMAND, source=source,
                             user_id=rec["user_id"], user_name=rec.get("user_name") or None,
                             message_id=rec.get("message_id") or None)
        await adapter.handle_message(event)
        logger.info("telegram-enhanced: Stop -> /stop for chat %s thread %s", rec["chat_id"], rec["thread_id"])
        return True
    except Exception:
        logger.warning("telegram-enhanced: could not dispatch /stop", exc_info=True)
        return False


async def _stop_button_dispatch(update: Any, context: Any) -> None:
    """Wired once into PTB; resolves the CURRENT plugin generation's handler, because a plugin reload
    does not re-run native handler factories."""
    handler = getattr(sys, "_hte_stop_button", None)
    logger.info("telegram-enhanced: stop-button press received (handler=%s)", handler is not None)
    if handler is not None:
        await handler(update, context)


def _ensure_stop_handler(native: Any) -> None:
    """Put an ``hs:`` CallbackQueryHandler ahead of core's catch-all in group 0 (once per PTB app)."""
    handlers = getattr(native, "handlers", None)
    if not isinstance(handlers, dict):
        return
    group0 = handlers.setdefault(0, [])
    # PTB handlers use __slots__, so identify ours by callback name (any plugin generation).
    if any(getattr(getattr(h, "callback", None), "__name__", "") == "_stop_button_dispatch" for h in group0):
        return
    from telegram.ext import CallbackQueryHandler
    group0.insert(0, CallbackQueryHandler(_stop_button_dispatch, pattern=r"^" + STOP_PREFIX))
    logger.info("telegram-enhanced: stop-button callback handler installed")


async def _on_stop_button(update: Any, _context: Any) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    m = _STOP_DATA_RE.match(getattr(query, "data", "") or "")
    with _state["turns_lock"]:
        rec = next((r for r in _state["stops"].values() if m and r["token"] == m.group(1)), None)
    if rec is None:
        logger.info("telegram-enhanced: stop press for unknown/ended turn")
        await query.answer(text="这一轮已经结束了")
        try:
            msg = getattr(query, "message", None)
            if msg is not None:
                _stop_delete(str(msg.chat_id), int(msg.message_id))
        except Exception:
            pass
        return
    if str(getattr(getattr(query, "from_user", None), "id", "")) != str(rec["user_id"]):
        await query.answer(text="这个按钮不是给你的")
        return
    await query.answer(text="已停止")
    _stop_end(rec)
    await _dispatch_stop(rec)


# ---------------------------------------------------------------- guest mode

GUEST_SCHEMA = {
    "name": "telegram_guest_reply",
    "description": (
        "Answer a Telegram Guest Mode request (someone @mentioned the bot in another chat). Replaces the "
        "'处理中' placeholder in THAT chat with your answer. Everything you write is visible to everyone in "
        "that chat: include only what the request needs and never add private details unless explicitly asked. "
        "Supports Rich Markdown and {{time:...}} placeholders; no buttons."
    ),
    "parameters": {"type": "object", "properties": {
        "guest_id": {"type": "string", "description": "guest_id from the [Telegram Guest] message."},
        "markdown": {"type": "string"},
    }, "required": ["guest_id", "markdown"]},
}


def _guest_allowed(user_id: str) -> bool:
    allowed = _setting("guest_allowed_users", None)
    if not allowed:
        allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    if isinstance(allowed, str):
        allowed = [a.strip() for a in allowed.split(",") if a.strip()]
    return str(user_id) in {str(a) for a in allowed}


def _guest_topic(owner_id: str) -> str | None:
    meta: JsonStore = _state["meta"]
    info = meta.get("guest_topic") or {}
    if info.get(owner_id):
        return str(info[owner_id])
    res = _api("createForumTopic", {"chat_id": int(owner_id), "name": "👥 跨聊天调用"})
    thread = (res.get("result") or {}).get("message_thread_id")
    if thread:
        info[owner_id] = thread
        meta.set("guest_topic", info)
        return str(thread)
    logger.warning("telegram-enhanced: could not create guest topic: %s", res.get("description"))
    return None


async def _on_guest_message(update: Any, _context: Any) -> None:
    msg = getattr(update, "guest_message", None)
    if msg is None:
        raw = (getattr(update, "api_kwargs", None) or {}).get("guest_message")
        if not isinstance(raw, dict):
            return
        msg = raw
    get = (lambda k: msg.get(k)) if isinstance(msg, dict) else (lambda k: getattr(msg, k, None))
    query_id = get("guest_query_id")
    user = get("from_user") or get("from")
    user_id = str(user.get("id") if isinstance(user, dict) else getattr(user, "id", "") or "")
    if not query_id or not user_id:
        return
    if not _setting("guest_mode", True) or not _guest_allowed(user_id):
        logger.info("telegram-enhanced: ignored guest query from unauthorized user")
        return
    chat = get("chat")
    chat_title = (chat.get("title") if isinstance(chat, dict) else getattr(chat, "title", None)) or ""
    text = get("text") or get("caption") or ""
    reply = get("reply_to_message")
    reply_text = ""
    if reply is not None:
        reply_text = (reply.get("text") if isinstance(reply, dict) else getattr(reply, "text", None)) or \
                     (reply.get("caption") if isinstance(reply, dict) else getattr(reply, "caption", None)) or ""

    placeholder = {"type": "article", "id": "hte-wait", "title": "小助手",
                   "input_message_content": {"rich_message": {"markdown": "⏳ 小助手处理中…"}}}
    res = _api("answerGuestQuery", {"guest_query_id": query_id, "result": placeholder})
    inline_id = (res.get("result") or {}).get("inline_message_id")
    if not res.get("ok") or not inline_id:
        logger.warning("telegram-enhanced: answerGuestQuery failed: %s", res.get("description"))
        return
    guest_id = new_request_id()
    _state["guests"].set(guest_id, {"inline_message_id": inline_id, "user_id": user_id, "chat_title": chat_title,
                                    "created_at": time.time(), "answered": False})
    lines = [f"[Telegram Guest] 有人在其他聊天里 @ 了小助手。guest_id: {guest_id}",
             f"聊天: {chat_title or '(私聊或未知)'}",
             f"请求: {text}"]
    if reply_text:
        lines.append(f"被回复的消息: {reply_text[:2000]}")
    lines.append("回答会公开显示在那个聊天里：用 telegram_guest_reply 回复，只写请求需要的内容，不透露其他隐私。")
    owner = user_id
    thread = _guest_topic(owner)
    adapter = _state.get("adapter")
    ok = False
    try:
        if adapter is not None:
            source = adapter.build_source(chat_id=owner, chat_type="dm", user_id=owner, thread_id=thread)
            ok = _state["ctx"].inject_message("\n".join(lines), origin=source.to_dict())
    except Exception:
        logger.warning("telegram-enhanced: guest inject failed", exc_info=True)
    if not ok:
        _api("editMessageText", {"inline_message_id": inline_id,
                                 "rich_message": {"markdown": "⚠️ 小助手暂时无法处理这个请求"}})


def telegram_guest_reply(args: dict[str, Any], **_kw: Any) -> str:
    guest_id = str(args.get("guest_id") or "")
    entry = _state["guests"].get(guest_id)
    if not entry or time.time() - float(entry.get("created_at", 0)) > GUEST_TTL_SECONDS:
        return _result(success=False, error="unknown or expired guest_id")
    caller = _session("HERMES_SESSION_USER_ID")
    if caller and caller != entry["user_id"]:
        return _result(success=False, error="this guest request belongs to another user")
    try:
        markdown = expand_time_placeholders(str(args.get("markdown") or ""), _display_tz()).strip()
    except ButtonSpecError as exc:
        return _result(success=False, error=str(exc))
    if not markdown or len(markdown) > RICH_MAX_CHARS:
        return _result(success=False, error="markdown empty or too long")
    res = _api("editMessageText", {"inline_message_id": entry["inline_message_id"],
                                   "rich_message": {"markdown": markdown}})
    if not res.get("ok") and not _not_modified(res):
        return _result(success=False, error=str(res.get("description") or "edit failed"))
    entry["answered"] = True
    _state["guests"].set(guest_id, entry)
    return _result(success=True, chat=entry.get("chat_title"))


# ---------------------------------------------------------------- time placeholders in plain replies

def _on_transform_output(response_text: str = "", platform: str = "", **_kw: Any) -> str | None:
    """Plain (MarkdownV2) replies: render time placeholders as text and list markers as bullets."""
    if not response_text:
        return None
    text = response_text
    if "{{time:" in text:
        tz = _display_tz()

        def repl(m: Any) -> str:
            try:
                return time_fallback(parse_when(m.group(1), (m.group(2) or "").strip() or None), tz)
            except Exception:
                return m.group(0)
        text = TIME_PLACEHOLDER_RE.sub(repl, text)
    return text if text != response_text else None


# ---------------------------------------------------------------- native lists

_LIST_PATCH_MARK = "_hte_native_lists_v2"
_LEGACY_LIST_PATCH_MARK = "_hte_native_lists"
# Wrappers resolve helpers at call time, so a plugin reload updates the behaviour of an already-patched
# live adapter class instead of leaving the first generation's code bound in.
sys._hte_list_helpers = (has_markdown_list, bulletize_plain)


def _patch_telegram_lists() -> None:
    """Send replies containing Markdown lists via sendRichMessage (native lists); if a reply still
    lands on MarkdownV2 (rich ineligible or rejected), show bullet glyphs instead of literal hyphens.

    Hermes core is read-only, so wrap the live TelegramAdapter class(es). Idempotent; re-run from
    pre_llm_call because the platform plugin may (re)load after this plugin registers.
    """
    candidates = []
    if not _state.get("gc_scanned"):
        # A plugin reload re-imports the adapter module, so the instance that actually sends can be an
        # orphaned class. Find live instances once per plugin load.
        _state["gc_scanned"] = True
        import gc
        for obj in gc.get_objects():
            try:
                if type(obj).__name__ == "TelegramAdapter" and hasattr(obj, "format_message"):
                    candidates.append(type(obj))
                    app = getattr(obj, "_app", None)
                    if app is not None:
                        if _state.get("adapter") is None:
                            _state["adapter"] = obj
                        try:
                            _ensure_stop_handler(app)
                        except Exception:
                            logger.warning("telegram-enhanced: stop handler install failed", exc_info=True)
                    logger.info("telegram-enhanced: found live adapter %s id=%x patched=%s",
                                type(obj).__module__, id(type(obj)),
                                bool(type(obj).__dict__.get(_LIST_PATCH_MARK)))
            except Exception:
                continue
    live = _state.get("adapter")
    if live is not None:  # the instance actually sending; after a plugin reload its class may no
        candidates.append(type(live))  # longer be the one sys.modules points at
    for mod in list(sys.modules.values()):
        try:
            candidates.append(getattr(mod, "TelegramAdapter", None))
        except Exception:
            continue
    if live is not None:
        logger.info("telegram-enhanced: live adapter class %s.%s id=%x patched=%s", type(live).__module__,
                    type(live).__name__, id(type(live)), bool(type(live).__dict__.get(_LIST_PATCH_MARK)))
    for cls in candidates:
        if (not isinstance(cls, type) or cls.__dict__.get(_LIST_PATCH_MARK)
                or not hasattr(cls, "_needs_rich_rendering") or not hasattr(cls, "format_message")):
            continue
        need, fmt = cls._needs_rich_rendering, cls.format_message
        if cls.__dict__.get(_LEGACY_LIST_PATCH_MARK):  # wrapped by an earlier generation: unwrap once
            need, fmt = need.__defaults__[0], fmt.__defaults__[0]
        cls._hte_orig_needs_rich = cls.__dict__.get("_hte_orig_needs_rich", need)
        cls._hte_orig_format = cls.__dict__.get("_hte_orig_format", fmt)

        def _needs_rich(self, content):
            helpers = getattr(sys, "_hte_list_helpers", None)
            base = type(self)._hte_orig_needs_rich(self, content)
            return bool(base or (helpers and helpers[0](content)))

        def _format(self, content):
            helpers = getattr(sys, "_hte_list_helpers", None)
            if helpers and isinstance(content, str):
                content = helpers[1](content)
            return type(self)._hte_orig_format(self, content)

        cls._needs_rich_rendering, cls.format_message = _needs_rich, _format
        setattr(cls, _LIST_PATCH_MARK, True)
        logger.info("telegram-enhanced: native list rendering enabled on %s.%s", cls.__module__, cls.__name__)


# ---------------------------------------------------------------- registration

def _profile_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _extend_allowed_updates() -> None:
    """Ask Telegram for Bot API updates newer than the bundled python-telegram-bot knows about.

    Hermes polls with ``allowed_updates=Update.ALL_TYPES``; Telegram only delivers listed types.
    """
    try:
        import telegram
        extra = ["stopped_message_generation", "guest_message"]
        current = [str(getattr(t, "value", t)) for t in telegram.Update.ALL_TYPES]
        missing = [t for t in extra if t not in current]
        if missing:
            telegram.Update.ALL_TYPES = list(telegram.Update.ALL_TYPES) + missing
    except Exception:
        logger.debug("telegram-enhanced: could not extend allowed_updates", exc_info=True)


def register(ctx: Any) -> None:
    _state["ctx"] = ctx
    try:
        _patch_telegram_lists()
    except Exception:
        logger.debug("telegram-enhanced: list patch failed", exc_info=True)
    root = _profile_home() / "telegram-enhanced"
    _state["pending"] = PendingStore(root)
    _state["cards"] = JsonStore(root, "cards.json")
    _state["guests"] = JsonStore(root, "guests.json")
    _state["meta"] = JsonStore(root, "meta.json")
    _state["stopmsgs"] = JsonStore(root, "stopmsgs.json")
    sys._hte_stop_button = _on_stop_button
    try:
        _state["pending"].ensure()
    except OSError:
        logger.warning("telegram-enhanced: state directory unavailable", exc_info=True)
    _extend_allowed_updates()

    ctx.register_tool(name="telegram_rich_send", toolset=TOOLSET, schema=RICH_SCHEMA,
                      handler=lambda args, **kw: telegram_rich_send(args or {}, **kw),
                      description="Telegram rich messages with buttons, maps, galleries and local times", emoji="🔘")
    ctx.register_tool(name="telegram_status_card", toolset=TOOLSET, schema=CARD_SCHEMA,
                      handler=lambda args, **kw: telegram_status_card(args or {}, **kw),
                      description="Pinned, edit-in-place Telegram status cards", emoji="📌")
    ctx.register_tool(name="telegram_guest_reply", toolset=TOOLSET, schema=GUEST_SCHEMA,
                      handler=lambda args, **kw: telegram_guest_reply(args or {}, **kw),
                      description="Answer Telegram Guest Mode requests", emoji="👥")

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("agent_loop_stopped", _on_loop_stopped)
    ctx.register_hook("transform_llm_output", _on_transform_output)

    def factory(native: Any, adapter: Any) -> None:
        if native is None:
            return
        from telegram import Update
        from telegram.ext import CallbackQueryHandler, TypeHandler
        _state["adapter"] = adapter
        try:
            _patch_telegram_lists()
        except Exception:
            logger.debug("telegram-enhanced: list patch failed", exc_info=True)
        token = getattr(getattr(native, "bot", None), "token", None)
        if token:
            _state["token"] = token
        native.add_handler(CallbackQueryHandler(_make_callback(ctx), pattern=r"^" + CALLBACK_PREFIX))
        _ensure_stop_handler(native)

        async def _route_new_updates(update: Any, context: Any) -> None:
            kw = getattr(update, "api_kwargs", None) or {}
            if "stopped_message_generation" in kw:
                await _on_stopped_generation(update, context)
            elif getattr(update, "guest_message", None) is not None or "guest_message" in kw:
                await _on_guest_message(update, context)

        # Own group: observes alongside core handlers instead of competing in group 0.
        native.add_handler(TypeHandler(Update, _route_new_updates), group=-7)

    ctx.register_telegram_handler(factory)

    def _cleanup() -> None:
        with _state["turns_lock"]:
            for rec in _state["turns"].values():
                rec["stop"].set()
            _state["turns"].clear()
    ctx.on_unload(_cleanup)
