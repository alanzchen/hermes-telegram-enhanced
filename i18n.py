"""User-facing strings in Chinese and English, plus language detection.

The language follows the user: each chat/topic remembers the language of the user's latest message
(``detect``), and a turn uses it. Config ``language`` (``auto`` | ``zh`` | ``en``) pins it instead.
Text the agent reads (button presses, guest requests) stays English; the agent answers in the user's
language anyway.
"""
from __future__ import annotations

import re
from typing import Any

LANGS = ("zh", "en")
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
# Wrappers the gateway adds around the user's words (English metadata, quoted replies, injected context).
_NOISE = re.compile(
    r"Gateway message origin.*?insufficient\.|\[Replying to:.*?\](?=\s*\n|\s*$)|<memory-context>.*?</memory-context>"
    r"|\[System:[^\]]*\]|\[Context from[^\]]*\]|\[This response[^\]]*\]|\{[^{}]*\}|https?://\S+|`[^`]*`",
    re.S)


def detect(text: Any) -> str | None:
    """'zh' if the message is mainly Chinese, 'en' if mainly English, None if it can't tell.

    Chinese speakers mix in English terms ("看一下这个 tool call"), so one Chinese character
    weighs as much as one English word."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = _NOISE.sub(" ", text)
    cjk = len(_CJK.findall(text))
    words = len(_LATIN_WORD.findall(text))
    if cjk == 0 and words == 0:
        return None
    return "zh" if cjk >= words else "en"


def from_language_code(code: Any) -> str | None:
    """Telegram ``language_code`` (e.g. 'zh-hans', 'en') -> 'zh' / 'en'."""
    code = str(code or "").lower()
    if code.startswith("zh"):
        return "zh"
    if code:
        return "en"
    return None


def norm(lang: Any) -> str:
    return lang if lang in LANGS else "en"


S: dict[str, dict[str, str]] = {
    # processing bubble
    "working": {"zh": "{name}处理中…", "en": "{name} is working…"},
    "now": {"zh": "正在{what}", "en": "{what}"},
    "errors": {"zh": "（{n} 次出错）", "en": " ({n} failed)"},
    "more_kinds": {"zh": "等 {n} 类", "en": "{n} kinds in all"},
    "and_more": {"zh": " 等 {n} 个", "en": " (+{m} more)"},
    "generic_tally": {"zh": "{verb} {n} 次", "en": "{verb} ×{n}"},
    # stop button / prompt
    "stop_confirm": {"zh": "要停止这一轮吗？不停的话，直接发消息就是插话",
                     "en": "Stop this turn? To keep it going, just send a message — it will be added as a steer."},
    "stop_label": {"zh": "⏹ 停止", "en": "⏹ Stop"},
    "stopped": {"zh": "已停止", "en": "Stopped"},
    "turn_over": {"zh": "这一轮已经结束了", "en": "This turn has already finished"},
    "stop_failed": {"zh": "停止失败，请发 /stop", "en": "Couldn't stop it — send /stop"},
    "not_yours": {"zh": "这个按钮不是给你的", "en": "This button isn't for you"},
    # choice buttons
    "expired": {"zh": "已过期", "en": "Expired"},
    "used": {"zh": "已经选过了", "en": "Already chosen"},
    "invalid": {"zh": "无效按钮", "en": "Invalid button"},
    "cannot": {"zh": "无法处理", "en": "Can't handle that"},
    "chosen": {"zh": "已选择：{label}", "en": "Selected: {label}"},
    # guest mode
    "guest_topic": {"zh": "👥 跨聊天调用", "en": "👥 Guest requests"},
    "guest_wait": {"zh": "⏳ {name}处理中…", "en": "⏳ {name} is working…"},
    "guest_fail": {"zh": "⚠️ {name}暂时无法处理这个请求", "en": "⚠️ {name} can't handle this request right now"},
}

DEFAULT_NAME = {"zh": "小助手", "en": "Assistant"}

# Tool kinds: (name fragments, zh verb, en verb, zh tally, en tally)
TOOL_KINDS: list[tuple[str, tuple[str, ...], str, str, str, str]] = [
    ("cmd", ("terminal", "process"), "运行命令", "running a command", "跑了 {n} 条命令", "{n} commands"),
    ("read", ("read_file",), "读取文件", "reading a file", "读了 {n} 个文件", "read {n} files"),
    ("edit", ("write_file", "patch"), "修改文件", "editing a file", "改了 {n} 个文件", "edited {n} files"),
    ("find", ("search_files",), "查找文件", "searching files", "查找 {n} 次", "{n} file searches"),
    ("search", ("web_search", "_search"), "搜索", "searching the web", "搜索 {n} 次", "{n} searches"),
    ("web", ("web_extract", "fetch", "scrape", "_contents", "crawl"), "读取网页", "reading a page",
     "读了 {n} 个网页", "read {n} pages"),
    ("browser", ("browser",), "操作浏览器", "using the browser", "操作浏览器 {n} 次", "{n} browser steps"),
    ("code", ("execute_code",), "运行代码", "running code", "跑了 {n} 段代码", "{n} code runs"),
    ("agent", ("delegate_task",), "派给子代理", "delegating to a subagent", "派了 {n} 个子代理", "{n} subagents"),
    ("telegram", ("telegram_",), "发 Telegram 消息", "sending a Telegram message", "发了 {n} 条消息",
     "{n} messages"),
    ("skill", ("skill",), "查看技能", "checking skills", "看了 {n} 个技能", "{n} skills"),
    ("memory", ("memory", "honcho"), "查记忆", "checking memory", "查了 {n} 次记忆", "{n} memory lookups"),
    ("image", ("vision", "image"), "看图片", "looking at an image", "看了 {n} 张图", "{n} images"),
    ("ask", ("clarify",), "等你选择", "waiting for your choice", "问了 {n} 次", "{n} questions"),
]

# Guesses for a command's purpose when it has no "# purpose" first line: (fragments, zh, en)
CMD_HINTS: list[tuple[tuple[str, ...], str, str]] = [
    (("git push",), "推送代码", "pushing code"), (("git commit",), "提交代码", "committing code"),
    (("git ",), "查看代码版本", "checking git"),
    (("unittest", "pytest", "py_compile"), "跑测试", "running tests"),
    (("reload_gateway_plugins", "gateway restart"), "重新加载 bot", "reloading the bot"),
    (("op read",), "读取密码库", "reading the vault"),
    (("curl ", "wget ", "api.telegram.org"), "访问网络", "calling the network"),
    (("grep ", "rg ", "awk "), "搜索文件内容", "searching file contents"),
    (("find ", "ls "), "查看目录", "listing files"),
    (("cat ", "sed -n", "head ", "tail "), "读取文件", "reading files"), (("sleep ",), "等待", "waiting"),
    (("pip ", "uv ", "npm ", "apt"), "安装依赖", "installing dependencies"),
    (("cp ", "mv ", "mkdir "), "整理文件", "moving files"), (("python", "node "), "运行脚本", "running a script"),
]

WEEKDAYS = {"zh": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
            "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}


def t(key: str, lang: Any, **kw: Any) -> str:
    entry = S[key]
    text = entry.get(norm(lang)) or entry["en"]
    return text.format(**kw) if kw else text


def tool_kind(name: str) -> str:
    for kind, keys, *_ in TOOL_KINDS:
        if any(k in name for k in keys):
            return kind
    return ""


def tool_verb(kind: str, lang: Any, fallback: str) -> str:
    for k, _keys, zh, en, _tz, _te in TOOL_KINDS:
        if k == kind:
            return zh if norm(lang) == "zh" else en
    return fallback


def tool_tally(kind: str, n: int, lang: Any, fallback: str) -> str:
    for k, _keys, _zh, _en, tz, te in TOOL_KINDS:
        if k == kind:
            if norm(lang) == "zh":
                return tz.format(n=n)
            text = te.format(n=n)
            if n == 1:  # "1 searches" -> "1 search", "read 1 files" -> "read 1 file"
                text = re.sub(r"(ches|s)$", lambda m: "ch" if m.group(1) == "ches" else "", text)
            return text
    return t("generic_tally", lang, verb=fallback, n=n)


def cmd_hint(code: str, lang: Any) -> str:
    low = code.lower()
    for keys, zh, en in CMD_HINTS:
        if any(k in low for k in keys):
            return zh if norm(lang) == "zh" else en
    return ""
