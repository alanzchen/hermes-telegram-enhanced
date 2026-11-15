"""OpenUI view validation, safe source interpolation, and named saved views."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any

from .sources import SourceError, SourceRegistry
from .spec import SpecError, key, text


MAX_OPENUI_BYTES = 65536
MAX_BINDINGS = 12
MAX_OPENUI_ACTIONS = 64
VIEW_NAME = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_-]{1,48}(?:\.[A-Za-z0-9_-]{1,48})*)\s*\}\}")
OPENUI_COMPONENTS = {
    "Card",
    "CardHeader",
    "Text",
    "Markdown",
    "Alert",
    "Button",
    "ButtonGroup",
    "Table",
    "BarChart",
    "Form",
    "TextInput",
    "Select",
    "Checkbox",
    "RadioGroup",
    "DateInput",
    "Tabs",
    "Badge",
    "Separator",
    "Progress",
    "Accordion",
}


def _json_copy(value: Any, path: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SpecError(f"{path}: must be finite JSON data") from exc


def _outside_strings(value: str) -> str:
    out: list[str] = []
    quote = None
    escaped = False
    for char in value:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            out.append(" ")
            continue
        if char in ("\"", "'"):
            quote = char
            out.append(" ")
        else:
            out.append(char)
    if quote:
        raise SpecError("openui: unterminated string")
    return "".join(out)


def validate_openui(value: Any) -> str:
    """Structural, allowlist-only preflight for the vendored OpenUI component subset."""
    value = text(value, "openui", MAX_OPENUI_BYTES, False)
    if len(value.encode()) > MAX_OPENUI_BYTES:
        raise SpecError("openui: exceeds 64 KiB")
    code = _outside_strings(value)
    if "@" in code or "$" in code:
        raise SpecError("openui: directives and bindings are not supported")
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for char in code:
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or char != stack.pop():
                raise SpecError("openui: unbalanced delimiters")
    if stack:
        raise SpecError("openui: unbalanced delimiters")
    roots = re.findall(r"(?m)^\s*root\s*=", code)
    if len(roots) != 1 or not re.search(r"(?m)^\s*root\s*=\s*Card\s*\(", code):
        raise SpecError("openui: exactly one Card root is required")
    calls = re.findall(r"\b([A-Z][A-Za-z0-9_]*)\s*\(", code)
    unknown = sorted(set(calls) - OPENUI_COMPONENTS)
    if unknown:
        raise SpecError("openui: unsupported component " + ", ".join(unknown))
    if len(calls) > 200:
        raise SpecError("openui: at most 200 component calls")
    return value


def validate_bindings(value: Any, registry: SourceRegistry, profile: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_BINDINGS:
        raise SpecError(f"sources: expected 0–{MAX_BINDINGS} items")
    aliases = set()
    bindings = []
    for i, item in enumerate(value):
        path = f"sources[{i}]"
        if not isinstance(item, dict) or set(item) - {"alias", "name", "args"} or set(item) != {"alias", "name", "args"}:
            raise SpecError(path + ": expected alias, name and args")
        alias = item["alias"]
        key(alias, path + ".alias")
        if alias in aliases:
            raise SpecError(path + ": duplicate alias")
        aliases.add(alias)
        name = text(item["name"], path + ".name", 120, False)
        try:
            args = registry.validate_args(name, item["args"], profile)
        except SourceError as exc:
            raise SpecError(path + ": " + str(exc)) from exc
        bindings.append({"alias": alias, "name": name, "args": args})
    return bindings


def _inside_string(value: str, position: int) -> bool:
    quote = None
    escaped = False
    for char in value[:position]:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("\"", "'"):
            quote = char
    return quote is not None


def _component_ranges(value: str, component: str) -> list[tuple[int, int]]:
    code = _outside_strings(value)
    ranges = []
    for match in re.finditer(r"\b" + re.escape(component) + r"\s*\(", code):
        start = code.find("(", match.start())
        depth = 0
        for index in range(start, len(code)):
            if code[index] == "(":
                depth += 1
            elif code[index] == ")":
                depth -= 1
                if depth == 0:
                    ranges.append((match.start(), index))
                    break
    return ranges


def validate_template(openui: Any, bindings: list[dict[str, Any]]) -> str:
    template = validate_openui(openui)
    aliases = {binding["alias"] for binding in bindings}
    button_ranges = _component_ranges(template, "Button")
    for match in PLACEHOLDER.finditer(template):
        if _inside_string(template, match.start()):
            raise SpecError("openui: source placeholders must be whole values, not quoted strings")
        if any(start <= match.start() <= end for start, end in button_ranges):
            raise SpecError("openui: source placeholders may not be used in Button actions")
        alias = match.group(1).split(".", 1)[0]
        if alias not in aliases:
            raise SpecError("openui: placeholder refers to an undeclared source alias")
    if "{{" in template or "}}" in template:
        # Every template marker must be a whole, safe dot path. Partial markers are rejected.
        scrubbed = PLACEHOLDER.sub("", template)
        if "{{" in scrubbed or "}}" in scrubbed:
            raise SpecError("openui: invalid source placeholder")
    return template


def validate_openui_actions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_OPENUI_ACTIONS:
        raise SpecError(f"actions: expected 0–{MAX_OPENUI_ACTIONS} items")
    found = set()
    actions = []
    for i, item in enumerate(value):
        path = f"actions[{i}]"
        if not isinstance(item, dict) or set(item) - {"key", "label", "once"} or not {"key", "label"} <= set(item):
            raise SpecError(path + ": expected key, label and optional once")
        action_key = key(item["key"], path + ".key")
        if action_key in found:
            raise SpecError(path + ": duplicate action key")
        found.add(action_key)
        label = text(item["label"], path + ".label", 120, False)
        once = item.get("once", True)
        if type(once) is not bool:
            raise SpecError(path + ".once: expected boolean")
        actions.append({"key": action_key, "label": label, "once": once})
    return actions


def validate_button_action_types(template: str, actions: list[dict[str, Any]]) -> None:
    """Require every rendered Button to map to the view's static action allowlist."""
    allowed = {action["key"] for action in actions}
    for start, end in _component_ranges(template, "Button"):
        button = template[start : end + 1]
        matches = re.findall(r"(?:\btype|[\"']type[\"'])\s*:\s*([\"'])([A-Za-z0-9_-]{1,48})\1", button)
        if len(matches) != 1:
            raise SpecError("openui: every Button requires one static type action")
        action_key = matches[0][1]
        key(action_key, "Button type")
        if action_key not in allowed:
            raise SpecError("openui: Button type is not declared in actions")


def validate_openui_action(record: dict[str, Any], action_key: Any, value: Any) -> dict[str, Any]:
    if not isinstance(action_key, str):
        raise SpecError("key: unknown action")
    action = next(
        (item for item in record.get("openui_actions", []) if item["key"] == action_key),
        None,
    )
    if action is None:
        raise SpecError("key: unknown action")
    clean = _json_copy(value, "value")
    if len(json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode()) > 8192:
        raise SpecError("value: exceeds 8 KiB")
    return {
        "key": action["key"],
        "label": action["label"],
        "value": clean,
        "component_id": "openui",
        "kind": "openui",
        "once": action["once"],
    }


def _lookup(source: Any, dotted: str) -> Any:
    current = source
    for segment in dotted.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise SourceError("Saved view data no longer matches its source contract")
        current = current[segment]
    return current


def hydrate_openui(template: str, bindings: list[dict[str, Any]], registry: SourceRegistry, profile: str) -> str:
    """Fetch live values only at the authenticated HTTP boundary and insert JSON literals."""
    values = {
        binding["alias"]: registry.query(binding["name"], binding["args"], profile)
        for binding in bindings
    }

    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        alias, *rest = path.split(".")
        value = _lookup(values[alias], ".".join(rest)) if rest else values[alias]
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    return PLACEHOLDER.sub(replace, template)


class SavedViewStore:
    """Private per-profile named templates; they retain no fetched data."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError, UnicodeError) as exc:
            raise SpecError("Saved view store is corrupt; refusing to overwrite it") from exc
        if not isinstance(data, dict):
            raise SpecError("Saved view store is corrupt; refusing to overwrite it")
        return data

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        tmp = self.path.with_name(self.path.name + "." + secrets.token_hex(6) + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def save(self, name: str, template: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(name, str) or not VIEW_NAME.fullmatch(name):
            raise SpecError("name: use 1–48 letters, digits, _ or -")
        with self.lock:
            records = self._read()
            stored = copy.deepcopy(template)
            stored["name"] = name
            stored["saved_at"] = time.time()
            records[name] = stored
            self._write(records)
            return copy.deepcopy(stored)

    def get(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not VIEW_NAME.fullmatch(name):
            raise SpecError("name: use 1–48 letters, digits, _ or -")
        with self.lock:
            record = self._read().get(name)
            if not record:
                raise SpecError("No saved view with that name")
            return copy.deepcopy(record)

    def manifest(self) -> list[dict[str, Any]]:
        with self.lock:
            records = self._read()
            return [
                {
                    "name": name,
                    "title": str(record.get("title", "")),
                    "renderer": str(record.get("renderer", "json")),
                    "sources": [binding.get("name") for binding in record.get("sources", [])],
                }
                for name, record in sorted(records.items())
            ]
