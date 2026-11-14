#!/usr/bin/env python3
"""Send mail from the agent account only after the owner approves the exact message in a Telegram Mini App.

  approved_mail.py request MESSAGE.eml --title "..."   -> creates the approval request, posts the card
                                                          to the current Telegram chat/topic, prints {id, sha256}
  approved_mail.py status ID                          -> pending / approved / rejected / expired / consumed
  approved_mail.py send ID                            -> broker sends exactly the version Alan approved
                                                          (after his edits, if any); preferred
  approved_mail.py send ID MESSAGE.eml                -> stdin mode: bytes must equal the approved version

Config comes from the environment or the env file (APPROVED_MAIL_ENV, default /opt/data/.env):
TG_APPROVAL_URL, CLIBROKER_URL, APPROVED_MAIL_DEFAULT_CHAT, TELEGRAM_BOT_TOKEN,
TG_APPROVAL_CREATOR_TOKEN, CLIBROKER_TOKEN_APPROVED_MAIL. Tokens are never printed.
The message file must be the exact RFC 5322 text: send reads the same file, byte for byte.
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

ENV = os.environ.get("APPROVED_MAIL_ENV", "/opt/data/.env")


def env(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for line in open(ENV, encoding="utf-8"):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(f"{key} missing from {ENV}")


def _cfg(key: str) -> str:
    return env(key).rstrip("/")


def http(method: str, url: str, token: str, body: dict | None = None, timeout: int = 60) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": e.reason}


def mail_summary(raw: str | bytes) -> tuple[str, str]:
    """(subject, recipients) of an RFC 5322 message, for the card text."""
    from email import message_from_bytes, message_from_string, policy
    try:
        m = (message_from_bytes if isinstance(raw, bytes) else message_from_string)(raw, policy=policy.default)
        to = ", ".join(a for h in ("To", "Cc") for a in [str(m.get(h) or "")] if a)
        return str(m.get("Subject") or "（无主题）"), to or "（无收件人）"
    except Exception:
        return "（无法解析主题）", ""


def post_card(title: str, link: str, subject: str, to: str) -> dict:
    chat = os.environ.get("HERMES_SESSION_CHAT_ID") or env("APPROVED_MAIL_DEFAULT_CHAT")
    thread = os.environ.get("HERMES_SESSION_THREAD_ID") or ""
    text = (f"📧 待批准发信：{title}\n主题：{subject}\n收件人：{to}\n"
            f"点按钮查看完整原文，批准后才会发送，10 分钟内有效。")
    body = {"chat_id": int(chat) if chat.lstrip("-").isdigit() else chat, "text": text,
            "reply_markup": {"inline_keyboard": [[{"text": "查看并审批", "url": link}]]}}
    if thread.isdigit():
        body["message_thread_id"] = int(thread)
    req = urllib.request.Request(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    return {"ok": res.get("ok"), "message_id": (res.get("result") or {}).get("message_id"),
            "chat_id": body["chat_id"], "text": text}


CARDS = "/opt/data/cache/approved-mail/cards.json"
FINAL = {"approved": "✅ 已批准", "consumed": "📤 已按批准版本发出", "rejected": "❌ 已拒绝", "expired": "⌛ 已过期，未发送"}


def _cards() -> dict:
    try:
        return json.load(open(CARDS))
    except (OSError, ValueError):
        return {}


def _save_cards(cards: dict) -> None:
    os.makedirs(os.path.dirname(CARDS), exist_ok=True)
    tmp = CARDS + ".tmp"
    json.dump(cards, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, CARDS)


def tg(method: str, body: dict) -> dict:
    req = urllib.request.Request(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def finalize_card(rid: str, status: str, res: dict | None = None) -> bool:
    """Replace the card text with the outcome and drop the button (Telegram has no disabled state)."""
    cards = _cards()
    card = cards.get(rid)
    if not card or card.get("final") == status:
        return False
    if status == "approved" and card.get("final"):
        return False
    first = card["text"].split("\n", 1)[0]
    subject, to = card.get("subject"), card.get("to")
    if res and res.get("final_message"):  # Alan may have edited subject/recipients on the page
        subject, to = mail_summary(res["final_message"])
    if not subject:  # cards created before subjects were stored
        subject, to = "", ""
    lines = [first, FINAL[status]] + ([f"主题：{subject}"] if subject else []) + ([f"收件人：{to}"] if to else [])
    res = tg("editMessageText", {"chat_id": card["chat_id"], "message_id": card["message_id"],
                                 "text": "\n".join(lines),
                                 "reply_markup": {"inline_keyboard": []}})
    ok = res.get("ok") or "not modified" in str(res.get("description", ""))
    if ok:
        card["final"] = status
        _save_cards(cards)
    return bool(ok)


def sweep() -> None:
    """Close every card whose request reached a final state (also run before each command)."""
    for rid, card in list(_cards().items()):
        if card.get("final") in ("consumed", "rejected", "expired"):
            continue
        code, res = http("GET", f"{_cfg('TG_APPROVAL_URL')}/api/mail-requests/{rid}", env("TG_APPROVAL_CREATOR_TOKEN"))
        st = res.get("status") if code == 200 else None
        if st in FINAL:
            finalize_card(rid, st, res)


def watch(rid: str) -> None:
    """Detached: poll until the request is final, then close the card at once."""
    import time
    while True:
        card = _cards().get(rid) or {}
        code, res = http("GET", f"{_cfg('TG_APPROVAL_URL')}/api/mail-requests/{rid}", env("TG_APPROVAL_CREATOR_TOKEN"))
        st = res.get("status") if code == 200 else None
        if st in FINAL:
            finalize_card(rid, st, res)
        if st in ("consumed", "rejected", "expired") or code == 404:
            return
        if st == "approved":  # approved requests expire 10 min after approval if unused
            time.sleep(20)
            continue
        exp = res.get("expires_at") or card.get("expires_at") or time.time() + 60
        time.sleep(max(3, min(15, exp - time.time() + 2)))


def main() -> int:
    if len(sys.argv) < 2 or (len(sys.argv) < 3 and sys.argv[1] != "sweep"):
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "request":
        raw = open(sys.argv[2], "rb").read()
        title = sys.argv[sys.argv.index("--title") + 1] if "--title" in sys.argv else "agent 邮件"
        code, res = http("POST", _cfg("TG_APPROVAL_URL") + "/api/mail-requests", env("TG_APPROVAL_CREATOR_TOKEN"),
                         {"title": title, "message": raw.decode("utf-8")})
        if code != 200:
            print(json.dumps({"ok": False, "status": code, "error": res}, ensure_ascii=False))
            return 1
        assert res["sha256"] == hashlib.sha256(raw).hexdigest(), "server hash differs from local file"
        subject, to = mail_summary(raw)
        card = post_card(title, res["direct_link"], subject, to)
        if card.get("ok"):
            cards = _cards()
            cards[res["id"]] = {"chat_id": card["chat_id"], "message_id": card["message_id"], "text": card["text"],
                                "sha": res["sha256"], "subject": subject, "to": to, "expires_at": res.get("expires_at")}
            _save_cards(cards)
            import subprocess
            subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "watch", res["id"]],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        card.pop("text", None)
        print(json.dumps({"ok": True, "id": res["id"], "sha256": res["sha256"], "card": card}, ensure_ascii=False))
        return 0
    if cmd == "watch":
        watch(sys.argv[2])
        return 0
    if cmd == "sweep":
        sweep()
        return 0
    if cmd == "status":
        try:
            sweep()
        except Exception:
            pass
        code, res = http("GET", f"{_cfg('TG_APPROVAL_URL')}/api/mail-requests/{sys.argv[2]}", env("TG_APPROVAL_CREATOR_TOKEN"))
        print(json.dumps({"status_code": code, **res}, ensure_ascii=False))
        return 0 if code == 200 else 1
    if cmd == "send":  # without a file the broker fetches the exact version Alan approved (possibly edited)
        rid = sys.argv[2]
        raw = open(sys.argv[3], "rb").read() if len(sys.argv) > 3 else b""
        code, res = http("POST", _cfg("CLIBROKER_URL") + "/execute", env("CLIBROKER_TOKEN_APPROVED_MAIL"),
                         {"tool": "himalaya_approved", "argv": ["message", "send", "--account", "agent",
                                                                "--approval-id", rid], "stdin": raw.decode("utf-8")},
                         timeout=180)
        print(json.dumps({"status_code": code, **res}, ensure_ascii=False)[:3000])
        return 0 if code == 200 and res.get("ok") and res.get("exit_code", 0) == 0 else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
