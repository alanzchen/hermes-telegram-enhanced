"""Sensitive input over Telegram that never reaches the model. Pure helpers; no network here.

Widgets have no text input, so the value comes as Alan's NEXT text message in the topic after a request card.
The plugin's pre_gateway_dispatch hook takes that message before the gateway logs, stores or hands it to the
agent (``{"action": "skip"}``), deletes it from the chat, and either writes it into 1Password or keeps it in
memory for a short time. The agent only ever gets a reference (``op://...``) or a request id it can use
through ``telegram_secret(action="exec")`` with the value in ``$HERMES_SECRET`` and the output redacted.
Request metadata (no values) is persisted; values never touch disk.
"""
from __future__ import annotations

import html
import re
import secrets
from typing import Any

PREFIX = "hx:"
_CB_RE = re.compile(r"^hx:(sx[0-9a-f]{10})$")
WINDOW = 600     # seconds a request waits for the next message
TEMP_KEEP = 900  # seconds a temporary value is kept after capture
MAX_LEN = 4000
KEEP_RECORDS = 7 * 86400
KINDS = ("password", "code", "text")

STR = {
    "card": {"zh": "🔐 **安全输入：{label}**\n\n在这个话题里直接发下一条消息就行，可以粘贴。这条消息不会传给 AI，也不会留在"
                   "聊天记录里：收到后马上删除，{where}。\n\n只发{what}本身，不要加别的字。{mins} 分钟内有效。",
             "en": "🔐 **Secure input: {label}**\n\nJust send it as your next message in this topic (pasting is fine). "
                   "It never reaches the AI and does not stay in the chat: it is deleted right away and {where}.\n\n"
                   "Send only the {what} itself. Valid for {mins} minutes."},
    "where_op": {"zh": "直接存进 1Password（{vault} vault）", "en": "saved straight into 1Password ({vault} vault)"},
    "where_temp": {"zh": "只在内存里临时保留 15 分钟", "en": "kept in memory for 15 minutes only"},
    "what_password": {"zh": "密码", "en": "password"},
    "what_code": {"zh": "验证码", "en": "code"},
    "what_text": {"zh": "内容", "en": "value"},
    "cancel": {"zh": "取消", "en": "Cancel"},
    "got_op": {"zh": "✅ 已收到「{label}」（{n} 个字符）。你的消息已删除，值已存入 1Password：{title}",
               "en": "✅ Got \"{label}\" ({n} characters). Your message was deleted and the value saved to 1Password: {title}"},
    "got_temp": {"zh": "✅ 已收到「{label}」（{n} 个字符）。你的消息已删除，值只临时保留 15 分钟",
                 "en": "✅ Got \"{label}\" ({n} characters). Your message was deleted; the value is kept for 15 minutes"},
    "store_failed": {"zh": "⚠️ 收到了「{label}」，但存入 1Password 失败（{err}）。值没有保留，需要的话让我重新发起。",
                     "en": "⚠️ Got \"{label}\" but saving to 1Password failed ({err}). Nothing was kept; ask me to retry."},
    "not_deleted": {"zh": "\n\n没能自动删掉你那条消息，请手动删除。", "en": "\n\nI could not delete your message; please delete it."},
    "cancelled": {"zh": "已取消「{label}」的安全输入。", "en": "Secure input for \"{label}\" cancelled."},
    "expired": {"zh": "⌛「{label}」的安全输入已过期。现在发的消息会照常进入对话，别再发密码了；需要的话让我重新发起。",
                "en": "⌛ Secure input for \"{label}\" expired. Messages now go to the chat as usual: do not send the "
                      "secret; ask me to start again."},
    "gone": {"zh": "这个安全输入已经结束了", "en": "This secure input has ended"},
}


def s(key: str, lang: str, **kw: Any) -> str:
    entry = STR[key]
    text = entry.get(lang) or entry["en"]
    return text.format(**kw) if kw else text


def new_id() -> str:
    return "sx" + secrets.token_hex(5)


def default_store(kind: str) -> str:
    return "temp" if kind == "code" else "1password"


def card_markdown(req: dict[str, Any], lang: str, vault: str) -> str:
    where = s("where_op", lang, vault=vault) if req["store"] == "1password" else s("where_temp", lang)
    mins = max(1, round((req["expires_at"] - req["created"]) / 60))
    body = s("card", lang, label=req["label"], where=where, what=s("what_" + req["kind"], lang), mins=mins)
    btn = f'<tg-button type="callback_data" data="{PREFIX}{req["id"]}">{html.escape(s("cancel", lang))}</tg-button>'
    return body + "\n\n<tg-button-row>\n" + btn + "\n</tg-button-row>"


def parse_cancel(data: str) -> str | None:
    m = _CB_RE.match(data or "")
    return m.group(1) if m else None


def capture_value(text: Any) -> str | None:
    """The secret in a captured message: surrounding whitespace (a pasted newline) removed."""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value or len(value) > MAX_LEN or value.startswith("/"):
        return None  # empty, absurd, or a slash command (those pass through untouched)
    return value


def is_cancel_text(text: Any) -> bool:
    return isinstance(text, str) and text.strip().lower() in ("取消", "cancel")


def match_request(reqs: dict[str, Any], chat_id: str, thread_id: str, user_id: str, now: float
                  ) -> dict[str, Any] | None:
    """The newest pending, unexpired request of this user in this chat/topic."""
    best = None
    for req in reqs.values():
        if not isinstance(req, dict) or req.get("state") != "pending":
            continue
        if str(req.get("chat_id")) != str(chat_id) or str(req.get("thread_id") or "") != str(thread_id or ""):
            continue
        if str(req.get("user_id")) != str(user_id) or now > float(req.get("expires_at") or 0):
            continue
        if best is None or req["created"] > best["created"]:
            best = req
    return best


def redact(text: str, value: str) -> str:
    return text.replace(value, "••••") if value else text


def op_item(template: dict[str, Any], title: str, value: str, username: str = "", url: str = "",
            notes: str = "") -> dict[str, Any]:
    """Fill a 1Password item template (Login or Password category) without touching argv."""
    item = dict(template)
    item["title"] = title
    fields = []
    for f in template.get("fields") or []:
        f = dict(f)
        if f.get("id") == "password":
            f["value"] = value
        elif f.get("id") == "username" and username:
            f["value"] = username
        elif f.get("id") == "notesPlain" and notes:
            f["value"] = notes
        fields.append(f)
    item["fields"] = fields
    if url:
        item["urls"] = [{"href": url, "primary": True}]
    item["tags"] = sorted(set((template.get("tags") or []) + ["hermes-secure-input"]))
    return item


def injection_text(req: dict[str, Any], length: int, ref: str = "", error: str = "") -> str:
    lines = [f"[安全输入] Alan 已提交「{req['label']}」（{length} 个字符）。值不在对话里，也不要向他复述。"]
    if ref:
        lines.append(f"已存入 1Password：{ref}（条目「{req['title']}」）。要用时 op read 读进环境变量，或用 browser_vault 填写。")
    elif error:
        lines.append(f"存入 1Password 失败：{error}；值已丢弃，需要时重新发起。")
    else:
        lines.append("只在内存里保留 15 分钟：用 telegram_secret(action=\"exec\", id=..., command=...) 使用，"
                     "命令里通过环境变量 $HERMES_SECRET 取值，输出会自动打码。")
    lines.append(f"request_id: {req['id']}")
    return "\n".join(lines)


def prune(reqs: dict[str, Any], now: float) -> None:
    for k in [k for k, v in reqs.items() if now - float((v or {}).get("created") or 0) > KEEP_RECORDS]:
        reqs.pop(k, None)
