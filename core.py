"""Pure helpers for telegram-rich-buttons: button rendering, callback data, and pending-choice state.

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
from pathlib import Path
from typing import Any, Iterator

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


class ButtonSpecError(ValueError):
    """Invalid button specification supplied by the model."""


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


def _attr(value: str) -> str:
    return html.escape(str(value), quote=True)


def normalize_buttons(rows: Any) -> list[list[dict[str, Any]]]:
    """Validate the tool's ``buttons`` argument into rows of normalized button dicts.

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


def compose_markdown(body: str, rows: list[list[dict[str, Any]]], request_id: str | None, *,
                     include_choices: bool = True, footer: str = "", align: str | None = None) -> str:
    body = (body or "").rstrip()
    buttons = render_rows(rows, request_id, include_choices=include_choices, align=align)
    chunks = [c for c in (body, footer.strip(), buttons) if c]
    return "\n\n".join(chunks)


def choice_injection_text(entry: dict[str, Any], choice: dict[str, str], who: str) -> str:
    """Neutral, data-only description of a button press for the conversation."""
    lines = [f"[Telegram 按钮] {who} 点击了「{choice['label']}」。"]
    if choice.get("value") and choice["value"] != choice["label"]:
        lines.append(f"value: {choice['value']}")
    if entry.get("question"):
        lines.append(f"问题: {entry['question']}")
    lines.append(f"request_id: {entry.get('id', '')}")
    return "\n".join(lines)


class PendingStore:
    """Small JSON store of outstanding choice prompts, guarded by an flock for cross-process safety."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "pending.json"
        self.lock_path = self.root / ".lock"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
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

    @staticmethod
    def _prune(data: dict[str, Any], now: float) -> None:
        for key in [k for k, v in data.items() if now - float(v.get("created_at", 0)) > PENDING_TTL_SECONDS]:
            data.pop(key, None)
        if len(data) > MAX_PENDING:
            for key in sorted(data, key=lambda k: data[k].get("created_at", 0))[: len(data) - MAX_PENDING]:
                data.pop(key, None)

    def put(self, entry: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._locked() as data:
            self._prune(data, now)
            data[entry["id"]] = entry

    def update(self, request_id: str, **fields: Any) -> None:
        with self._locked() as data:
            if request_id in data:
                data[request_id].update(fields)

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._locked() as data:
            entry = data.get(request_id)
            return dict(entry) if entry else None

    def claim(self, request_id: str, index: int, user_id: str, now: float | None = None
              ) -> tuple[str, dict[str, Any] | None, dict[str, str] | None]:
        """Atomically validate and record a press. Returns (status, entry, choice).

        status: ok | expired | unauthorized | used | invalid
        """
        now = time.time() if now is None else now
        with self._locked() as data:
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
