"""Pure helpers for Hermes Telegram Enhanced.

Button rendering, callback data, time placeholders, map/gallery blocks and small JSON stores.
Nothing here imports Hermes or python-telegram-bot, so it is unit-testable on its own.
"""
from __future__ import annotations

import fcntl
import html
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

CALLBACK_PREFIX = "rb:"
CALLBACK_RE = re.compile(r"^rb:([0-9a-f]{10}):(\d{1,3})$")
VALID_TYPES = {"choice", "url", "copy"}
VALID_STYLES = {"primary", "success", "danger", "link"}
VALID_ALIGN = {"left", "center", "right"}
MAX_ROWS = 12
MAX_BUTTONS = 40
MAX_LABEL = 64
MAX_COPY_TEXT = 256
PENDING_TTL_SECONDS = 7 * 24 * 3600
MAX_PENDING = 500
DEFAULT_TZ = "America/Chicago"
TIME_FORMAT_RE = re.compile(r"^(r|w?[dD]?[tT]?)$")
TIME_PLACEHOLDER_RE = re.compile(r"\{\{time:([^{}|]+?)(?:\|([^{}|]*))?(?:\|([^{}|]*))?\}\}")
WEEKDAYS_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class ButtonSpecError(ValueError):
    """Invalid specification supplied by the model."""


# ---------------------------------------------------------------- buttons

def new_request_id() -> str:
    return secrets.token_hex(5)


def callback_data(request_id: str, index: int) -> str:
    data = f"{CALLBACK_PREFIX}{request_id}:{index}"
    if len(data.encode()) > 64:  # Telegram callback_data limit
        raise ButtonSpecError("callback data exceeds 64 bytes")
    return data


def parse_callback_data(data: str) -> tuple[str, int] | None:
    m = CALLBACK_RE.match(data or "")
    return (m.group(1), int(m.group(2))) if m else None


def _attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def normalize_buttons(rows: Any) -> list[list[dict[str, Any]]]:
    """Validate the ``buttons`` argument into rows of normalized button dicts.

    Accepts a list of rows (each a list of buttons) or a flat list of buttons (one row).
    """
    if rows in (None, []):
        return []
    if not isinstance(rows, list):
        raise ButtonSpecError("buttons must be a list of rows")
    if rows and all(isinstance(b, dict) for b in rows):
        rows = [rows]
    if len(rows) > MAX_ROWS:
        raise ButtonSpecError(f"at most {MAX_ROWS} button rows")
    out: list[list[dict[str, Any]]] = []
    total = 0
    for r, row in enumerate(rows):
        if not isinstance(row, list) or not row:
            raise ButtonSpecError(f"row {r} must be a non-empty list of buttons")
        norm_row = []
        for b in row:
            if not isinstance(b, dict):
                raise ButtonSpecError("each button must be an object")
            label = str(b.get("label") or "").strip()
            if not label or len(label) > MAX_LABEL:
                raise ButtonSpecError(f"button label must be 1-{MAX_LABEL} characters")
            btype = str(b.get("type") or "choice").strip().lower()
            if btype not in VALID_TYPES:
                raise ButtonSpecError(f"button type must be one of {sorted(VALID_TYPES)}")
            style = b.get("style")
            if style is not None:
                style = str(style).strip().lower()
                if style not in VALID_STYLES:
                    raise ButtonSpecError(f"button style must be one of {sorted(VALID_STYLES)}")
            item: dict[str, Any] = {"label": label, "type": btype, "style": style}
            if btype == "url":
                url = str(b.get("url") or "").strip()
                if not re.match(r"^(https?|tg)://", url):
                    raise ButtonSpecError("url buttons need an http(s):// or tg:// url")
                item["url"] = url
            elif btype == "copy":
                text = str(b.get("copy_text") or b.get("value") or "")
                if not text or len(text) > MAX_COPY_TEXT:
                    raise ButtonSpecError(f"copy buttons need copy_text of 1-{MAX_COPY_TEXT} characters")
                item["copy_text"] = text
            else:
                value = b.get("value")
                item["value"] = label if value in (None, "") else str(value)[:500]
            norm_row.append(item)
            total += 1
        out.append(norm_row)
    if total > MAX_BUTTONS:
        raise ButtonSpecError(f"at most {MAX_BUTTONS} buttons")
    return out


def choice_list(rows: list[list[dict[str, Any]]]) -> list[dict[str, str]]:
    """Choice buttons in render order; their list index is the callback index."""
    return [{"label": b["label"], "value": b["value"]} for row in rows for b in row if b["type"] == "choice"]


def render_rows(rows: list[list[dict[str, Any]]], request_id: str | None, *,
                include_choices: bool = True, align: str | None = None) -> str:
    """Render normalized rows as rich-message ``<tg-button-row>`` HTML blocks."""
    parts: list[str] = []
    choice_index = 0
    align_attr = f' align="{align}"' if align in VALID_ALIGN else ""
    for row in rows:
        tags = []
        for b in row:
            style = f' style="{b["style"]}"' if b.get("style") else ""
            label = html.escape(b["label"], quote=False)
            if b["type"] == "choice":
                idx = choice_index
                choice_index += 1
                if not include_choices:
                    continue
                if request_id is None:
                    raise ButtonSpecError("choice buttons need a request id")
                tags.append(f'<tg-button type="callback_data"{style} data="{_attr(callback_data(request_id, idx))}">'
                            f"{label}</tg-button>")
            elif b["type"] == "url":
                tags.append(f'<tg-button type="url"{style} url="{_attr(b["url"])}">{label}</tg-button>')
            else:
                tags.append(f'<tg-button type="copy_text"{style} text="{_attr(b["copy_text"])}">{label}</tg-button>')
        if tags:
            parts.append(f"<tg-button-row{align_attr}>\n" + "\n".join(tags) + "\n</tg-button-row>")
    return "\n".join(parts)


def choice_injection_text(entry: dict[str, Any], choice: dict[str, str], who: str) -> str:
    """Neutral, data-only description of a button press for the conversation."""
    lines = [f"[Telegram 按钮] {who} 点击了「{choice['label']}」。"]
    if choice.get("value") and choice["value"] != choice["label"]:
        lines.append(f"value: {choice['value']}")
    if entry.get("question"):
        lines.append(f"问题: {entry['question']}")
    lines.append(f"request_id: {entry.get('id', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- time entities

def parse_when(value: str, tz_name: str | None) -> datetime:
    """Parse an ISO-8601 date-time (or unix seconds); naive values use ``tz_name``."""
    raw = (value or "").strip()
    tz = ZoneInfo(tz_name or DEFAULT_TZ)
    if re.fullmatch(r"\d{9,11}", raw):
        return datetime.fromtimestamp(int(raw), tz)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ButtonSpecError(f"bad time {raw!r}: use ISO like 2026-11-07T09:30") from exc
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def time_fallback(dt: datetime, display_tz: str) -> str:
    local = dt.astimezone(ZoneInfo(display_tz))
    return f"{local.month}/{local.day} {WEEKDAYS_ZH[local.weekday()]} {local:%H:%M} {local.tzname()}"


def time_entity(value: str, tz_name: str | None = None, fmt: str | None = None,
                display_tz: str = DEFAULT_TZ) -> str:
    """Rich-markdown date-time entity: rendered in the reader's own timezone and locale."""
    fmt = (fmt or "wDT").strip()
    if not TIME_FORMAT_RE.match(fmt):
        raise ButtonSpecError("time format must match r|w?[dD]?[tT]?")
    dt = parse_when(value, (tz_name or "").strip() or None)
    fallback = time_fallback(dt, display_tz).replace("]", ")").replace("[", "(")
    return f"![{fallback}](tg://time?unix={int(dt.timestamp())}&format={fmt})"


def expand_time_placeholders(text: str, display_tz: str = DEFAULT_TZ) -> str:
    """Replace ``{{time:ISO|TZ|FORMAT}}`` placeholders with date-time entities."""
    def repl(m: re.Match) -> str:
        return time_entity(m.group(1), m.group(2), m.group(3), display_tz)
    return TIME_PLACEHOLDER_RE.sub(repl, text or "")


# ---------------------------------------------------------------- maps & galleries

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM_RE = re.compile(r"^([ \t]*)[-*+][ \t]+(?!\[[ xX]\][ \t])(\S.*)$")
_RICH_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_RICH_TASK_RE = re.compile(r"(?m)^\s*[-*]\s+\[[ xX]\]\s+")
_RICH_DETAILS_RE = re.compile(r"(?m)^</?(details|summary)\b")


def likely_rich(text: str) -> bool:
    """Mirror of the adapter's rich gate: these replies go via sendRichMessage, which renders real lists."""
    return bool(any(_RICH_TABLE_SEP_RE.match(line) for line in text.splitlines())
                or _RICH_TASK_RE.search(text) or _RICH_DETAILS_RE.search(text) or "$$" in text)


def bulletize_plain(text: str) -> str:
    """MarkdownV2 has no list syntax: ``- item`` arrives as a literal hyphen. Use real bullet glyphs.

    Top-level items become ``• ``, indented ones ``  ◦ ``. Code fences and replies bound for the rich
    endpoint are left untouched.
    """
    if not text or likely_rich(text):
        return text
    out, in_fence, changed = [], False, False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        m = None if in_fence else _LIST_ITEM_RE.match(line)
        if m:
            indent = len(m.group(1).expandtabs(4))
            line = ("  ◦ " if indent >= 2 else "• ") + m.group(2)
            changed = True
        out.append(line)
    return "\n".join(out) if changed else text


def render_maps(maps: Any) -> str:
    if not maps:
        return ""
    if not isinstance(maps, list):
        raise ButtonSpecError("maps must be a list")
    out = []
    for m in maps[:10]:
        try:
            lat, lon = float(m["lat"]), float(m.get("long", m.get("lon")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ButtonSpecError("each map needs numeric lat and long") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ButtonSpecError("map coordinates out of range")
        zoom = max(1, min(int(m.get("zoom") or 15), 20))
        block = f'<tg-map lat="{lat:.6f}" long="{lon:.6f}" zoom="{zoom}"/>'
        caption = str(m.get("caption") or "").strip()
        out.append(f"{block}\n\n*{caption}*" if caption else block)
    return "\n\n".join(out)


def render_gallery(gallery: Any) -> str:
    if not gallery:
        return ""
    if not isinstance(gallery, dict):
        raise ButtonSpecError("gallery must be an object")
    kind = str(gallery.get("type") or "collage").lower()
    if kind not in {"collage", "slideshow"}:
        raise ButtonSpecError("gallery type must be collage or slideshow")
    items = gallery.get("items") or []
    if not isinstance(items, list) or not items:
        raise ButtonSpecError("gallery needs items")
    lines = []
    for it in items[:50]:
        url = str((it or {}).get("url") or "").strip()
        if not re.match(r"^https?://\S+$", url):
            raise ButtonSpecError("gallery items need http(s) urls")
        cap = str(it.get("caption") or "").replace('"', "'").strip()
        lines.append(f'![]({url} "{cap}")' if cap else f"![]({url})")
    return f"<tg-{kind}>\n" + "\n".join(lines) + f"\n</tg-{kind}>"


def compose_markdown(body: str, rows: list[list[dict[str, Any]]], request_id: str | None, *,
                     include_choices: bool = True, footer: str = "", align: str | None = None,
                     maps: Any = None, gallery: Any = None, display_tz: str = DEFAULT_TZ) -> str:
    body = expand_time_placeholders((body or "").rstrip(), display_tz)
    chunks = [body, render_gallery(gallery), render_maps(maps), footer.strip(),
              render_rows(rows, request_id, include_choices=include_choices, align=align)]
    return "\n\n".join(c for c in chunks if c)


def card_key(chat_id: str, thread_id: str | None, key: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (key or "").strip().lower()).strip("-")
    if not slug or len(slug) > 64:
        raise ButtonSpecError("status card key must be 1-64 chars of letters, digits, - or _")
    return f"{chat_id}:{thread_id or '-'}:{slug}"


# ---------------------------------------------------------------- stores

class JsonStore:
    """Small JSON dict persisted atomically under an flock (safe across processes)."""

    def __init__(self, root: Path, name: str):
        self.root = Path(root)
        self.path = self.root / name
        self.lock_path = self.root / ".lock"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        self.ensure()
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self._read()
                yield data
                self._write(data)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, key: str) -> dict[str, Any] | None:
        with self.locked() as data:
            entry = data.get(key)
            return dict(entry) if entry else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self.locked() as data:
            data[key] = value

    def pop(self, key: str) -> dict[str, Any] | None:
        with self.locked() as data:
            return data.pop(key, None)


class PendingStore(JsonStore):
    """Outstanding choice prompts."""

    def __init__(self, root: Path):
        super().__init__(root, "pending.json")

    @staticmethod
    def _prune(data: dict[str, Any], now: float) -> None:
        for key in [k for k, v in data.items() if now - float(v.get("created_at", 0)) > PENDING_TTL_SECONDS]:
            data.pop(key, None)
        if len(data) > MAX_PENDING:
            for key in sorted(data, key=lambda k: data[k].get("created_at", 0))[: len(data) - MAX_PENDING]:
                data.pop(key, None)

    def put(self, entry: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.locked() as data:
            self._prune(data, now)
            data[entry["id"]] = entry

    def update(self, request_id: str, **fields: Any) -> None:
        with self.locked() as data:
            if request_id in data:
                data[request_id].update(fields)

    def claim(self, request_id: str, index: int, user_id: str, now: float | None = None
              ) -> tuple[str, dict[str, Any] | None, dict[str, str] | None]:
        """Atomically validate and record a press. Returns (status, entry, choice).

        status: ok | expired | unauthorized | used | invalid
        """
        now = time.time() if now is None else now
        with self.locked() as data:
            entry = data.get(request_id)
            if not entry or now - float(entry.get("created_at", 0)) > PENDING_TTL_SECONDS:
                return "expired", None, None
            if str(entry.get("user_id") or "") and str(entry.get("user_id")) != str(user_id):
                return "unauthorized", dict(entry), None
            choices = entry.get("choices") or []
            if not 0 <= index < len(choices):
                return "invalid", dict(entry), None
            if entry.get("single_use", True) and entry.get("answered"):
                return "used", dict(entry), None
            entry["answered"] = {"index": index, "user_id": str(user_id), "at": now}
            return "ok", dict(entry), dict(choices[index])
