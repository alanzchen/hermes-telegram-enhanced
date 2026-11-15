"""OpenUI validation, schema-checked projections, and saved view storage."""

from __future__ import annotations

import copy
import datetime as dt
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
MAX_OPENUI_ACTION_FIELDS = 32
VIEW_NAME = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
PLACEHOLDER = re.compile(r"\{\{\s*(?P<expression>[^{}]+?)\s*\}\}")
PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
PATH = re.compile(r"^[A-Za-z0-9_-]{1,48}(?:\.[A-Za-z0-9_-]{1,48})*$")
OPENUI_COMPONENTS = {
    "Card",
    "CardHeader",
    "Grid",
    "Stat",
    "List",
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
    "CheckboxGroup",
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
        if char in ('"', "'"):
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
    # Field formatter syntax uses @ inside a server-side placeholder only. Strip
    # every complete marker before rejecting all other directives and bindings.
    code = PLACEHOLDER.sub("placeholder", _outside_strings(value))
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
        elif char in ('"', "'"):
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


def _parse_formatter(name: str, argument: str | None) -> dict[str, Any]:
    if name == "money":
        if argument in (None, ""):
            return {"kind": "money", "currency": "USD", "precision": 2}
        parts = re.split(r"[:,]", argument)
        if len(parts) != 2 or parts[0] != "USD" or not re.fullmatch(r"[0-6]", parts[1]):
            raise SpecError("openui: money formatter must be money or money:USD:2")
        return {"kind": "money", "currency": "USD", "precision": int(parts[1])}
    if name == "date":
        if argument not in (None, "", "MM/DD"):
            raise SpecError("openui: date formatter must be date or date:MM/DD")
        return {"kind": "date", "format": "MM/DD"}
    if name == "percent":
        if argument in (None, ""):
            return {"kind": "percent", "precision": 0}
        if not re.fullmatch(r"[0-4]", argument):
            raise SpecError("openui: percent formatter precision must be 0–4")
        return {"kind": "percent", "precision": int(argument)}
    if name == "map":
        if not argument:
            raise SpecError("openui: map formatter requires enum=value pairs")
        values: dict[str, str] = {}
        for item in argument.split(","):
            source, separator, target = item.partition("=")
            if not separator or not source or not target or source in values:
                raise SpecError("openui: map formatter requires unique enum=value pairs")
            if len(source) > 120 or len(target) > 120:
                raise SpecError("openui: map formatter values are too long")
            values[source] = target
        return {"kind": "map", "values": values}
    raise SpecError("openui: unsupported placeholder projection or formatter")


def _parse_field_specs(raw: str) -> list[dict[str, Any]]:
    if not raw:
        raise SpecError("openui: projection requires one or more declared fields")
    fields = []
    parts = raw.split(",")
    index = 0
    while index < len(parts):
        item = parts[index]
        markers = [marker for marker in ("@", "|") if marker in item]
        if len(markers) > 1:
            raise SpecError("openui: a projected field accepts at most one formatter")
        field, separator, formatter_raw = item.partition(markers[0]) if markers else (item, "", "")
        if not PATH_SEGMENT.fullmatch(field):
            raise SpecError("openui: projection field names must be simple declared keys")
        formatter = None
        if separator:
            name, colon, argument = formatter_raw.partition(":")
            if name == "map":
                mapping = [argument] if colon else []
                while (
                    index + 1 < len(parts)
                    and "=" in parts[index + 1]
                    and not re.match(r"^[A-Za-z0-9_-]+(?:@|\|)map:", parts[index + 1])
                ):
                    index += 1
                    mapping.append(parts[index])
                argument = ",".join(mapping)
            formatter = _parse_formatter(name, argument if colon else None)
        fields.append({"name": field, "formatter": formatter})
        index += 1
    return fields


def _parse_placeholder(match: re.Match[str]) -> dict[str, Any]:
    expression = match.group("expression").strip()
    path, separator, operation_raw = expression.partition("|")
    if not PATH.fullmatch(path):
        raise SpecError("openui: invalid source placeholder")
    result: dict[str, Any] = {"path": path, "kind": "raw"}
    if not separator:
        return result
    operation, colon, argument = operation_raw.partition(":")
    if operation in {"pluck", "table", "items", "list"}:
        if not colon:
            raise SpecError("openui: projection requires a field list")
        fields = _parse_field_specs(argument)
        if operation == "pluck" and len(fields) != 1:
            raise SpecError("openui: pluck requires exactly one field")
        if operation == "items" and not 2 <= len(fields) <= 5:
            raise SpecError("openui: items requires a value, label, and up to three description fields")
        if operation == "list" and len(fields) not in (4, 5):
            raise SpecError("openui: list requires title, subtitle, trailing, badge, and optional badge tone fields")
        result.update(kind=operation, fields=fields)
        return result
    if operation in {"money", "date", "percent", "map"}:
        result.update(kind="formatter", formatter=_parse_formatter(operation, argument if colon else None))
        return result
    raise SpecError("openui: unsupported placeholder projection or formatter")


def _schema_types(schema: dict[str, Any]) -> set[str]:
    value = schema.get("type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def _schema_property(schema: dict[str, Any], field: str, context: str) -> dict[str, Any]:
    if "object" not in _schema_types(schema) and not isinstance(schema.get("properties"), dict):
        raise SpecError(f"openui: {context} is not an object in its declared source output")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or field not in properties or not isinstance(properties[field], dict):
        raise SpecError(f"openui: {context} does not declare field {field}")
    return properties[field]


def _schema_path(schema: dict[str, Any], dotted: str, context: str) -> dict[str, Any]:
    current = schema
    if not dotted:
        return current
    for field in dotted.split("."):
        current = _schema_property(current, field, context)
    return current


def _array_item_schema(schema: dict[str, Any], context: str) -> dict[str, Any]:
    if "array" not in _schema_types(schema) or not isinstance(schema.get("items"), dict):
        raise SpecError(f"openui: {context} must be an array in its declared source output")
    return schema["items"]


def _require_types(schema: dict[str, Any], allowed: set[str], context: str) -> None:
    types = _schema_types(schema)
    if types and not types <= allowed:
        raise SpecError(f"openui: {context} has an incompatible declared source type")


def _validate_map_schema(schema: dict[str, Any], formatter: dict[str, Any], context: str) -> None:
    _require_types(schema, {"string"}, context)
    enum = schema.get("enum")
    if not isinstance(enum, list) or not enum or any(not isinstance(value, str) for value in enum):
        raise SpecError(f"openui: {context} must declare a string enum for map")
    if set(formatter["values"]) != set(enum):
        raise SpecError(f"openui: {context} map must cover exactly its declared enum values")


def _validate_field_formatter(field_schema: dict[str, Any], formatter: dict[str, Any], context: str) -> None:
    if formatter["kind"] in {"money", "percent"}:
        _require_types(field_schema, {"number", "integer", "null"}, context)
    elif formatter["kind"] == "map":
        _validate_map_schema(field_schema, formatter, context)
    else:
        _require_types(field_schema, {"string", "null"}, context)


def _validate_placeholder_schema(spec: dict[str, Any], schema: dict[str, Any]) -> None:
    context = "placeholder " + spec["path"]
    if spec["kind"] == "raw":
        return
    if spec["kind"] == "formatter":
        _validate_field_formatter(schema, spec["formatter"], context)
        return
    item = _array_item_schema(schema, context)
    fields = spec["fields"]
    for field in fields:
        field_schema = _schema_property(item, field["name"], context)
        formatter = field["formatter"]
        if formatter is not None:
            _validate_field_formatter(field_schema, formatter, context + "." + field["name"])
    if spec["kind"] == "items":
        _require_types(
            _schema_property(item, fields[0]["name"], context),
            {"string"},
            context + "." + fields[0]["name"],
        )
        for field in fields[1:]:
            _require_types(
                _schema_property(item, field["name"], context),
                {"string", "number", "integer", "boolean", "null"},
                context + "." + field["name"],
            )
    if spec["kind"] == "list":
        for field in fields:
            _require_types(
                _schema_property(item, field["name"], context),
                {"string", "number", "integer", "boolean", "null"},
                context + "." + field["name"],
            )


def validate_template(
    openui: Any,
    bindings: list[dict[str, Any]],
    registry: SourceRegistry,
    profile: str,
) -> str:
    template = validate_openui(openui)
    schemas = {
        binding["alias"]: registry.output_schema(binding["name"], profile)
        for binding in bindings
    }
    button_ranges = _component_ranges(template, "Button")
    for match in PLACEHOLDER.finditer(template):
        if _inside_string(template, match.start()):
            raise SpecError("openui: source placeholders must be whole values, not quoted strings")
        if any(start <= match.start() <= end for start, end in button_ranges):
            raise SpecError("openui: source placeholders may not be used in Button actions")
        spec = _parse_placeholder(match)
        alias, *rest = spec["path"].split(".")
        if alias not in schemas:
            raise SpecError("openui: placeholder refers to an undeclared source alias")
        _validate_placeholder_schema(spec, _schema_path(schemas[alias], ".".join(rest), "placeholder " + spec["path"]))
    if "{{" in template or "}}" in template:
        # Every template marker must be a whole, safe placeholder expression.
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
        if not isinstance(item, dict) or set(item) - {"key", "label", "once", "fields"} or not {"key", "label"} <= set(item):
            raise SpecError(path + ": expected key, label, optional once and optional fields")
        action_key = key(item["key"], path + ".key")
        if action_key in found:
            raise SpecError(path + ": duplicate action key")
        found.add(action_key)
        label = text(item["label"], path + ".label", 120, False)
        once = item.get("once", True)
        if type(once) is not bool:
            raise SpecError(path + ".once: expected boolean")
        action: dict[str, Any] = {"key": action_key, "label": label, "once": once}
        if "fields" in item:
            fields = item["fields"]
            if not isinstance(fields, list) or len(fields) > MAX_OPENUI_ACTION_FIELDS:
                raise SpecError(path + f".fields: expected 0–{MAX_OPENUI_ACTION_FIELDS} items")
            clean_fields = []
            for j, field in enumerate(fields):
                clean_field = key(field, f"{path}.fields[{j}]")
                if clean_field in clean_fields:
                    raise SpecError(path + ".fields: duplicate field")
                clean_fields.append(clean_field)
            action["fields"] = clean_fields
        actions.append(action)
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


def _selected_form_fields(value: Any, fields: list[str]) -> dict[str, Any]:
    form = value.get("form") if isinstance(value, dict) else None
    form_name = value.get("form_name") if isinstance(value, dict) else None
    clean_form = {
        field: copy.deepcopy(form[field])
        for field in fields
        if isinstance(form, dict) and field in form
    }
    return {"form": clean_form, "form_name": form_name if isinstance(form_name, str) else None}


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
    if "fields" in action:
        clean = _selected_form_fields(clean, action["fields"])
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
    if not dotted:
        return current
    for segment in dotted.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise SourceError("Saved view data no longer matches its source contract")
        current = current[segment]
    return current


def _project_row_value(row: Any, field: str) -> Any:
    if not isinstance(row, dict) or field not in row:
        raise SourceError("Saved view data no longer matches its source contract")
    return row[field]


def _format_value(value: Any, formatter: dict[str, Any]) -> str:
    if value is None:
        return "—"
    kind = formatter["kind"]
    if kind == "money":
        if type(value) not in (int, float):
            raise SourceError("Saved view data no longer matches its source contract")
        return f"${value:,.{formatter['precision']}f}"
    if kind == "percent":
        if type(value) not in (int, float):
            raise SourceError("Saved view data no longer matches its source contract")
        percentage = value * 100 if -1 <= value <= 1 else value
        return f"{percentage:.{formatter['precision']}f}%"
    if kind == "date":
        if not isinstance(value, str):
            raise SourceError("Saved view data no longer matches its source contract")
        try:
            date_value = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                date_value = dt.date.fromisoformat(value)
            except ValueError as exc:
                raise SourceError("Saved view data no longer matches its source contract") from exc
        return f"{date_value.month:02d}/{date_value.day:02d}"
    if kind == "map":
        if not isinstance(value, str) or value not in formatter["values"]:
            raise SourceError("Saved view data no longer matches its source contract")
        return formatter["values"][value]
    raise SourceError("Saved view data no longer matches its source contract")


def _option_text(value: Any) -> str:
    if value is None:
        return "—"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _project_field(row: Any, field: dict[str, Any]) -> Any:
    value = _project_row_value(row, field["name"])
    return _format_value(value, field["formatter"]) if field["formatter"] else value


def _project_value(value: Any, spec: dict[str, Any]) -> Any:
    kind = spec["kind"]
    if kind == "raw":
        return value
    if kind == "formatter":
        return _format_value(value, spec["formatter"])
    if not isinstance(value, list):
        raise SourceError("Saved view data no longer matches its source contract")
    fields = spec["fields"]
    if kind == "pluck":
        return [_project_field(row, fields[0]) for row in value]
    if kind == "table":
        return [[_project_field(row, field) for field in fields] for row in value]
    if kind == "items":
        value_field, label_field, *description_fields = fields
        result = []
        for row in value:
            item = {
                "value": _option_text(_project_field(row, value_field)),
                "label": _option_text(_project_field(row, label_field)),
            }
            if description_fields:
                item["description"] = " · ".join(
                    _option_text(_project_field(row, field)) for field in description_fields
                )
            result.append(item)
        return result
    if kind == "list":
        title, subtitle, trailing, badge, *badge_tone = fields
        result = []
        for row in value:
            item = {
                "title": _option_text(_project_field(row, title)),
                "subtitle": _option_text(_project_field(row, subtitle)),
                "trailing": _option_text(_project_field(row, trailing)),
                "badge": _option_text(_project_field(row, badge)),
            }
            if badge_tone:
                item["badgeTone"] = _option_text(_project_field(row, badge_tone[0]))
            result.append(item)
        return result
    raise SourceError("Saved view data no longer matches its source contract")


def hydrate_openui(template: str, bindings: list[dict[str, Any]], registry: SourceRegistry, profile: str) -> str:
    """Fetch live values only at the authenticated HTTP boundary and insert JSON literals."""
    values = {
        binding["alias"]: registry.query(binding["name"], binding["args"], profile)
        for binding in bindings
    }

    def replace(match: re.Match[str]) -> str:
        spec = _parse_placeholder(match)
        alias, *rest = spec["path"].split(".")
        if alias not in values:
            raise SourceError("Saved view data no longer matches its source contract")
        value = _project_value(_lookup(values[alias], ".".join(rest)), spec)
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
