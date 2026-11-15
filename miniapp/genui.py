"""OpenUI validation, schema-checked projections, and saved view storage."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

from .sources import SourceError, SourceRegistry
from .spec import SpecError, key, text


MAX_OPENUI_BYTES = 65536
MAX_BINDINGS = 12
MAX_OPENUI_ACTIONS = 64
MAX_OPENUI_ACTION_FIELDS = 32
MAX_SAVED_VIEWS = 32
VIEW_NAME = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
PATH = re.compile(r"^[A-Za-z0-9_-]{1,48}(?:\.[A-Za-z0-9_-]{1,48})*$")
DISPLAY_TZ = ZoneInfo("America/Chicago")
OPENUI_COMPONENTS = {
    "Card", "CardHeader", "Grid", "Stat", "List", "Text", "Markdown", "Alert",
    "Button", "ButtonGroup", "Table", "BarChart", "Form", "TextInput", "Select",
    "Checkbox", "CheckboxGroup", "RadioGroup", "DateInput", "Tabs", "Badge",
    "Separator", "Progress", "Accordion",
}
_DISPLAY_PLACEHOLDER_ARGS = {
    "Text": {0}, "Stat": {1}, "Table": {1}, "BarChart": {0, 1}, "List": {0},
    "Badge": {0}, "Progress": {0},
}
_INTERACTIVE_ITEMS_ARGS = {"CheckboxGroup": 1, "Select": 1, "RadioGroup": 1}


@dataclass(frozen=True)
class _Token:
    kind: str
    value: Any
    start: int
    end: int


@dataclass
class _Node:
    kind: str
    value: Any = None
    children: list["_Node"] | None = None
    start: int = 0
    end: int = 0


@dataclass
class _Assignment:
    name: str
    value: _Node


class _OpenUITokenizer:
    """Strict tokenizer for the intentionally tiny OpenUI Mini App subset."""

    _punctuation = "()[]{},:="

    def __init__(self, source: str):
        self.source = source
        self.index = 0
        self.tokens: list[_Token] = []

    def tokenize(self) -> list[_Token]:
        while self.index < len(self.source):
            char = self.source[self.index]
            if char.isspace():
                self.index += 1
                continue
            if self.source.startswith("//", self.index) or self.source.startswith("/*", self.index):
                raise SpecError("openui: comments are not supported")
            if self.source.startswith("{{", self.index):
                self._placeholder()
                continue
            if char in ("'", '"'):
                self._string(char)
                continue
            if char in self._punctuation:
                self.tokens.append(_Token(char, char, self.index, self.index + 1))
                self.index += 1
                continue
            if char in "@$":
                raise SpecError("openui: directives and bindings are not supported")
            if char.isdigit() or (char == "-" and self.index + 1 < len(self.source) and self.source[self.index + 1].isdigit()):
                self._number()
                continue
            if char == "_" or (char.isascii() and char.isalpha()):
                self._identifier()
                continue
            if char.isalpha() or char == "_":
                raise SpecError("openui: identifiers must use ASCII letters, digits, or _")
            raise SpecError("openui: unsupported syntax")
        self.tokens.append(_Token("EOF", None, len(self.source), len(self.source)))
        return self.tokens

    def _placeholder(self) -> None:
        start = self.index
        end = self.source.find("}}", start + 2)
        if end < 0:
            raise SpecError("openui: invalid source placeholder")
        raw = self.source[start + 2 : end]
        if "{" in raw or "}" in raw:
            raise SpecError("openui: invalid source placeholder")
        self.tokens.append(_Token("PLACEHOLDER", raw.strip(), start, end + 2))
        self.index = end + 2

    def _string(self, quote: str) -> None:
        start = self.index
        self.index += 1
        out: list[str] = []
        escaped = False
        while self.index < len(self.source):
            char = self.source[self.index]
            self.index += 1
            if escaped:
                escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "\\": "\\", "'": "'", '"': '"'}
                if char == "u":
                    part = self.source[self.index : self.index + 4]
                    if len(part) != 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", part):
                        raise SpecError("openui: invalid string escape")
                    code = int(part, 16)
                    self.index += 4
                    if 0xD800 <= code <= 0xDBFF:
                        low_prefix = self.source[self.index : self.index + 2]
                        low_part = self.source[self.index + 2 : self.index + 6]
                        if low_prefix != "\\u" or len(low_part) != 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", low_part):
                            raise SpecError("openui: invalid unicode surrogate pair")
                        low = int(low_part, 16)
                        if not 0xDC00 <= low <= 0xDFFF:
                            raise SpecError("openui: invalid unicode surrogate pair")
                        self.index += 6
                        out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    elif 0xDC00 <= code <= 0xDFFF:
                        raise SpecError("openui: invalid unicode surrogate pair")
                    else:
                        out.append(chr(code))
                elif char in escapes:
                    out.append(escapes[char])
                else:
                    raise SpecError("openui: invalid string escape")
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote:
                self.tokens.append(_Token("STRING", "".join(out), start, self.index))
                return
            if char in "\r\n":
                raise SpecError("openui: unterminated string")
            if 0xD800 <= ord(char) <= 0xDFFF:
                raise SpecError("openui: invalid unicode surrogate pair")
            if char == "{" and self.index < len(self.source) and self.source[self.index] == "{":
                raise SpecError("openui: source placeholders must be whole component values")
            out.append(char)
        raise SpecError("openui: unterminated string")

    def _number(self) -> None:
        start = self.index
        match = re.match(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", self.source[self.index :])
        if not match:
            raise SpecError("openui: invalid number")
        raw = match.group(0)
        self.index += len(raw)
        self.tokens.append(_Token("NUMBER", float(raw) if "." in raw else int(raw), start, self.index))

    def _identifier(self) -> None:
        start = self.index
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", self.source[self.index :])
        if match is None:
            raise SpecError("openui: identifiers must use ASCII letters, digits, or _")
        value = match.group(0)
        self.index += len(value)
        self.tokens.append(_Token("IDENT", value, start, self.index))


class _OpenUIParser:
    MAX_DEPTH = 32

    def __init__(self, source: str):
        self.source = source
        self.tokens = _OpenUITokenizer(source).tokenize()
        self.index = 0
        self.depth = 0

    def _nested(self, parse):
        self.depth += 1
        if self.depth > self.MAX_DEPTH:
            self.depth -= 1
            raise SpecError("openui: maximum nesting depth is 32")
        try:
            return parse()
        finally:
            self.depth -= 1

    def parse(self) -> list[_Assignment]:
        assignments: list[_Assignment] = []
        while not self._at("EOF"):
            name = self._take("IDENT", "openui: expected an assignment")
            self._take("=", "openui: expected = after assignment name")
            assignments.append(_Assignment(str(name.value), self._expression()))
        return assignments

    def _expression(self) -> _Node:
        token = self._current()
        if token.kind == "PLACEHOLDER":
            self.index += 1
            return _Node("placeholder", str(token.value), start=token.start, end=token.end)
        if token.kind == "STRING":
            self.index += 1
            return _Node("string", token.value, start=token.start, end=token.end)
        if token.kind == "NUMBER":
            self.index += 1
            return _Node("number", token.value, start=token.start, end=token.end)
        if token.kind == "[":
            return self._nested(self._array)
        if token.kind == "{":
            return self._nested(self._object)
        if token.kind == "IDENT":
            self.index += 1
            name = str(token.value)
            if self._at("("):
                return self._nested(lambda: self._call(name, token.start))
            if name in ("true", "false", "null"):
                return _Node("literal", {"true": True, "false": False, "null": None}[name], start=token.start, end=token.end)
            return _Node("identifier", name, start=token.start, end=token.end)
        raise SpecError("openui: expected a value")

    def _array(self) -> _Node:
        start = self._take("[", "openui: expected [").start
        items: list[_Node] = []
        if not self._at("]"):
            while True:
                items.append(self._expression())
                if not self._at(","):
                    break
                self.index += 1
                if self._at("]"):
                    break
        end = self._take("]", "openui: expected ]").end
        return _Node("array", children=items, start=start, end=end)

    def _object(self) -> _Node:
        start = self._take("{", "openui: expected {").start
        pairs: list[_Node] = []
        if not self._at("}"):
            while True:
                token = self._current()
                if token.kind not in ("IDENT", "STRING"):
                    raise SpecError("openui: object keys must be identifiers or strings")
                self.index += 1
                self._take(":", "openui: expected : after object key")
                value = self._expression()
                pairs.append(_Node("pair", str(token.value), [value], token.start, value.end))
                if not self._at(","):
                    break
                self.index += 1
                if self._at("}"):
                    break
        end = self._take("}", "openui: expected }").end
        return _Node("object", children=pairs, start=start, end=end)

    def _call(self, name: str, start: int) -> _Node:
        self._take("(", "openui: expected (")
        args: list[_Node] = []
        if not self._at(")"):
            while True:
                args.append(self._expression())
                if not self._at(","):
                    break
                self.index += 1
                if self._at(")"):
                    break
        end = self._take(")", "openui: expected )").end
        return _Node("call", name, args, start, end)

    def _current(self) -> _Token:
        return self.tokens[self.index]

    def _at(self, kind: str) -> bool:
        return self._current().kind == kind

    def _take(self, kind: str, message: str) -> _Token:
        token = self._current()
        if token.kind != kind:
            raise SpecError(message)
        self.index += 1
        return token


def _program(value: str) -> list[_Assignment]:
    return _OpenUIParser(value).parse()


def _json_copy(value: Any, path: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SpecError(f"{path}: must be finite JSON data") from exc


def _walk(node: _Node):
    yield node
    for child in node.children or []:
        yield from _walk(child)


def _call_nodes(assignments: list[_Assignment]):
    for assignment in assignments:
        for node in _walk(assignment.value):
            if node.kind == "call":
                yield node


def validate_openui(value: Any) -> str:
    """Parse and allowlist the OpenUI subset instead of applying source-text heuristics."""
    value = text(value, "openui", MAX_OPENUI_BYTES, False)
    if len(value.encode()) > MAX_OPENUI_BYTES:
        raise SpecError("openui: exceeds 64 KiB")
    assignments = _program(value)
    roots = [assignment for assignment in assignments if assignment.name == "root"]
    if len(roots) != 1 or roots[0].value.kind != "call" or roots[0].value.value != "Card":
        raise SpecError("openui: exactly one Card root is required")
    calls = list(_call_nodes(assignments))
    unknown = sorted({str(call.value) for call in calls} - OPENUI_COMPONENTS)
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


def _parse_formatter(name: str, argument: str | None) -> dict[str, Any]:
    name = name.strip()
    argument = argument.strip() if isinstance(argument, str) else argument
    if name == "money":
        if argument in (None, ""):
            return {"kind": "money", "currency": "USD", "precision": 2}
        parts = [part.strip() for part in re.split(r"[:,]", argument)]
        if len(parts) != 2 or parts[0] != "USD" or not re.fullmatch(r"[0-6]", parts[1]):
            raise SpecError("openui: money formatter must be money or money:USD:2")
        return {"kind": "money", "currency": "USD", "precision": int(parts[1])}
    if name == "date":
        if argument not in (None, "", "MM/DD"):
            raise SpecError("openui: date formatter must be date or date:MM/DD")
        return {"kind": "date", "format": "MM/DD"}
    if name in {"percent", "ratio"}:
        if argument in (None, ""):
            return {"kind": name, "precision": 0}
        if not re.fullmatch(r"[0-4]", argument):
            raise SpecError(f"openui: {name} formatter precision must be 0–4")
        return {"kind": name, "precision": int(argument)}
    if name == "map":
        if not argument:
            raise SpecError("openui: map formatter requires enum=value pairs")
        values: dict[str, str] = {}
        for item in argument.split(","):
            source, separator, target = item.partition("=")
            source, target = source.strip(), target.strip()
            if not separator or not source or not target or source in values:
                raise SpecError("openui: map formatter requires unique enum=value pairs")
            if len(source) > 120 or len(target) > 120:
                raise SpecError("openui: map formatter values are too long")
            values[source] = target
        return {"kind": "map", "values": values}
    raise SpecError("openui: unsupported placeholder projection or formatter")


def _parse_field_specs(raw: str) -> list[dict[str, Any]]:
    if not raw.strip():
        raise SpecError("openui: projection requires one or more declared fields")
    fields = []
    parts = raw.split(",")
    index = 0
    while index < len(parts):
        item = parts[index].strip()
        markers = [marker for marker in ("@", "|") if marker in item]
        if len(markers) > 1:
            raise SpecError("openui: a projected field accepts at most one formatter")
        field, separator, formatter_raw = item.partition(markers[0]) if markers else (item, "", "")
        field = field.strip()
        if not PATH_SEGMENT.fullmatch(field):
            raise SpecError("openui: projection field names must be simple declared keys")
        formatter = None
        if separator:
            name, colon, argument = formatter_raw.partition(":")
            name = name.strip()
            if name == "map":
                mapping = [argument] if colon else []
                while index + 1 < len(parts):
                    next_item = parts[index + 1].strip()
                    if "=" not in next_item or re.match(r"^[A-Za-z0-9_-]+\s*(?:@|\|)map\s*:", next_item):
                        break
                    index += 1
                    mapping.append(next_item)
                argument = ",".join(mapping)
            formatter = _parse_formatter(name, argument if colon else None)
        fields.append({"name": field, "formatter": formatter})
        index += 1
    return fields


def _parse_placeholder_expression(expression: str) -> dict[str, Any]:
    expression = expression.strip()
    path, separator, operation_raw = expression.partition("|")
    path = path.strip()
    if not PATH.fullmatch(path):
        raise SpecError("openui: invalid source placeholder")
    result: dict[str, Any] = {"path": path, "kind": "raw"}
    if not separator:
        return result
    operation, colon, argument = operation_raw.partition(":")
    operation = operation.strip()
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
    if operation in {"money", "date", "percent", "ratio", "map"}:
        result.update(kind="formatter", formatter=_parse_formatter(operation, argument if colon else None))
        return result
    raise SpecError("openui: unsupported placeholder projection or formatter")


# Kept as a private compatibility name for callers that previously imported it.
def _parse_placeholder(match: Any) -> dict[str, Any]:
    expression = match.group("expression") if hasattr(match, "group") else str(match)
    return _parse_placeholder_expression(expression)


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
    for field in filter(None, dotted.split(".")):
        current = _schema_property(current, field, context)
    return current


def _array_item_schema(schema: dict[str, Any], context: str) -> dict[str, Any]:
    if "array" not in _schema_types(schema) or not isinstance(schema.get("items"), dict):
        raise SpecError(f"openui: {context} must be an array in its declared source output")
    return schema["items"]


def _require_types(schema: dict[str, Any], allowed: set[str], context: str) -> None:
    types = _schema_types(schema)
    if not types or not types <= allowed:
        raise SpecError(f"openui: {context} has an incompatible declared source type")


def _validate_map_schema(schema: dict[str, Any], formatter: dict[str, Any], context: str) -> None:
    _require_types(schema, {"string"}, context)
    enum = schema.get("enum")
    if not isinstance(enum, list) or not enum or any(not isinstance(value, str) for value in enum):
        raise SpecError(f"openui: {context} must declare a string enum for map")
    if set(formatter["values"]) != set(enum):
        raise SpecError(f"openui: {context} map must cover exactly its declared enum values")


def _validate_field_formatter(field_schema: dict[str, Any], formatter: dict[str, Any], context: str) -> None:
    if formatter["kind"] in {"money", "percent", "ratio"}:
        _require_types(field_schema, {"number", "integer", "null"}, context)
    elif formatter["kind"] == "map":
        _validate_map_schema(field_schema, formatter, context)
    else:
        _require_types(field_schema, {"string", "null"}, context)


def _validate_placeholder_schema(spec: dict[str, Any], schema: dict[str, Any], *, restricted: bool = False) -> None:
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
        if field["formatter"] is not None:
            _validate_field_formatter(field_schema, field["formatter"], context + "." + field["name"])
    if spec["kind"] == "items":
        value_schema = _schema_property(item, fields[0]["name"], context)
        _require_types(value_schema, {"string"}, context + "." + fields[0]["name"])
        if restricted and (fields[0]["formatter"] is not None or value_schema.get("x-opaque-id") is not True):
            raise SpecError("openui: restricted items values must use an unformatted x-opaque-id field")
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


def _placeholder_for_node(node: _Node) -> dict[str, Any]:
    if node.kind != "placeholder":
        raise AssertionError("expected placeholder")
    return _parse_placeholder_expression(str(node.value))


def _placeholder_nodes(node: _Node) -> list[_Node]:
    return [item for item in _walk(node) if item.kind == "placeholder"]


def _non_call_placeholder_nodes(node: _Node) -> list[_Node]:
    """Placeholders in a raw array/object value, excluding nested component calls."""
    if node.kind == "placeholder":
        return [node]
    if node.kind == "call":
        return []
    result: list[_Node] = []
    for child in node.children or []:
        result.extend(_non_call_placeholder_nodes(child))
    return result


def _object_value(node: _Node, name: str) -> _Node | None:
    if node.kind != "object":
        return None
    for pair in node.children or []:
        if pair.value == name:
            return (pair.children or [None])[0]
    return None


def _literal_string(node: _Node | None, context: str) -> str:
    if node is None or node.kind != "string":
        raise SpecError(f"openui: {context} must be a static string")
    return str(node.value)


def _validate_button(call: _Node, allowed_actions: set[str] | None = None) -> str | None:
    args = call.children or []
    if len(args) < 2 or args[1].kind != "object":
        raise SpecError("openui: every Button requires one static type action")
    type_node = _object_value(args[1], "type")
    action_key = _literal_string(type_node, "Button type")
    key(action_key, "Button type")
    if allowed_actions is not None and action_key not in allowed_actions:
        raise SpecError("openui: Button type is not declared in actions")
    return action_key


def _validate_placeholder_positions(
    assignments: list[_Assignment],
    schemas: dict[str, dict[str, Any]],
    *,
    restricted: bool,
) -> None:
    """Only direct, allowlisted component arguments may contain placeholders."""
    all_placeholders = {
        id(node)
        for assignment in assignments
        for node in _placeholder_nodes(assignment.value)
    }
    approved: set[int] = set()
    for call in _call_nodes(assignments):
        name = str(call.value)
        args = call.children or []
        for index, arg in enumerate(args):
            if arg.kind != "placeholder":
                continue
            if name in _INTERACTIVE_ITEMS_ARGS and index == _INTERACTIVE_ITEMS_ARGS[name]:
                spec = _placeholder_for_node(arg)
                if spec["kind"] != "items":
                    raise SpecError("openui: interactive source values are allowed only through items projections")
            elif index in _DISPLAY_PLACEHOLDER_ARGS.get(name, set()):
                if name == "Markdown":
                    raise SpecError("openui: source placeholders are not allowed in Markdown")
                spec = _placeholder_for_node(arg)
            else:
                raise SpecError("openui: source placeholders are not allowed in this component position")
            alias, *rest = spec["path"].split(".")
            if alias not in schemas:
                raise SpecError("openui: placeholder refers to an undeclared source alias")
            _validate_placeholder_schema(
                spec,
                _schema_path(schemas[alias], ".".join(rest), "placeholder " + spec["path"]),
                restricted=restricted and spec["kind"] == "items",
            )
            approved.add(id(arg))
    if all_placeholders != approved:
        raise SpecError("openui: source placeholders must be direct allowlisted component arguments")


def validate_template(openui: Any, bindings: list[dict[str, Any]], registry: SourceRegistry, profile: str) -> str:
    template = validate_openui(openui)
    assignments = _program(template)
    schemas = {binding["alias"]: registry.output_schema(binding["name"], profile) for binding in bindings}
    try:
        restricted = any(registry.sensitivity(binding["name"], profile) in {"private", "sensitive"} for binding in bindings)
    except SourceError as exc:
        raise SpecError("openui: source sensitivity is unavailable") from exc
    _validate_placeholder_positions(assignments, schemas, restricted=restricted)
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
    """Require every parsed Button action to map to the static action allowlist."""
    allowed = {action["key"] for action in actions}
    for call in _call_nodes(_program(template)):
        if call.value == "Button":
            _validate_button(call, allowed)


def _static_option_values(node: _Node) -> set[str] | None:
    if node.kind != "array":
        return None
    values: set[str] = set()
    for item in node.children or []:
        value = _object_value(item, "value")
        if value is None or value.kind != "string":
            return None
        values.add(str(value.value))
    return values


def restricted_static_allowed_values(template: str, actions: list[dict[str, Any]], *, restricted: bool) -> dict[str, list[str | bool]]:
    """Validate restricted action fields and record literal choice values server-side."""
    if not restricted:
        return {}
    allowed: dict[str, set[str | bool]] = {}
    dynamic_fields: set[str] = set()
    free_text_fields: set[str] = set()
    for call in _call_nodes(_program(template)):
        name = str(call.value)
        args = call.children or []
        if not args or args[0].kind != "string":
            continue
        field = str(args[0].value)
        if name in {"TextInput", "DateInput"}:
            free_text_fields.add(field)
        elif name == "Checkbox":
            allowed.setdefault(field, set()).update({False, True})
        elif name in _INTERACTIVE_ITEMS_ARGS:
            index = _INTERACTIVE_ITEMS_ARGS[name]
            if len(args) <= index:
                continue
            items = args[index]
            if items.kind == "placeholder":
                dynamic_fields.add(field)
            else:
                values = _static_option_values(items)
                if values is not None:
                    allowed.setdefault(field, set()).update(values)
    for action in actions:
        for field in action.get("fields", []):
            if field in free_text_fields:
                raise SpecError("Restricted views cannot submit TextInput or DateInput fields")
            if field not in allowed and field not in dynamic_fields:
                raise SpecError("Restricted action fields must use a declared choice or Checkbox control")
    return {
        field: sorted(values, key=lambda value: (type(value).__name__, str(value)))
        for field, values in sorted(allowed.items())
    }


def _selected_form_fields(value: Any, fields: list[str]) -> dict[str, Any]:
    form = value.get("form") if isinstance(value, dict) else None
    return {
        "form": {
            field: copy.deepcopy(form[field])
            for field in fields
            if isinstance(form, dict) and field in form
        }
    }


def validate_openui_action(record: dict[str, Any], action_key: Any, value: Any) -> dict[str, Any]:
    if not isinstance(action_key, str):
        raise SpecError("key: unknown action")
    action = next((item for item in record.get("openui_actions", []) if item["key"] == action_key), None)
    if action is None:
        raise SpecError("key: unknown action")
    clean = _json_copy(value, "value")
    if len(json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode()) > 8192:
        raise SpecError("value: exceeds 8 KiB")
    clean = _selected_form_fields(clean, action.get("fields", []))
    allowed_values = record.get("openui_allowed_values")
    if allowed_values is not None:
        if not isinstance(allowed_values, dict):
            raise SpecError("This view must be reopened before submitting")
        for field in action.get("fields", []):
            allowed = allowed_values.get(field)
            submitted = clean["form"].get(field)
            if not isinstance(allowed, list) or any(type(item) not in (str, bool) for item in allowed):
                raise SpecError("This view must be reopened before submitting")
            def is_allowed(item: Any) -> bool:
                return any(type(item) is type(candidate) and item == candidate for candidate in allowed)
            if isinstance(submitted, list):
                if any(not is_allowed(item) for item in submitted):
                    raise SpecError("Submitted value is not available in this view")
            elif submitted is not None and not is_allowed(submitted):
                raise SpecError("Submitted value is not available in this view")
    return {
        "key": action["key"], "label": action["label"], "value": clean,
        "component_id": "openui", "kind": "openui", "once": action["once"],
    }


def _lookup(source: Any, dotted: str) -> Any:
    current = source
    for segment in filter(None, dotted.split(".")):
        if not isinstance(current, dict) or segment not in current:
            raise SourceError("Saved view data no longer matches its source contract")
        current = current[segment]
    return current


def _project_row_value(row: Any, field: str) -> Any:
    if not isinstance(row, dict) or field not in row:
        raise SourceError("Saved view data no longer matches its source contract")
    return row[field]


def _format_value(value: Any, formatter: dict[str, Any]) -> str:
    kind = formatter["kind"]
    if value is None or (kind == "date" and value == ""):
        return "—"
    if kind == "money":
        if type(value) not in (int, float):
            raise SourceError("Saved view data no longer matches its source contract")
        amount = 0.0 if value == 0 else value
        prefix = "-$" if amount < 0 else "$"
        return f"{prefix}{abs(amount):,.{formatter['precision']}f}"
    if kind in {"percent", "ratio"}:
        if type(value) not in (int, float):
            raise SourceError("Saved view data no longer matches its source contract")
        percentage = value * 100 if kind == "ratio" else value
        return f"{percentage:.{formatter['precision']}f}%"
    if kind == "date":
        if not isinstance(value, str):
            raise SourceError("Saved view data no longer matches its source contract")
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            date_value = parsed.astimezone(DISPLAY_TZ).date() if parsed.tzinfo else parsed.date()
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
            item = {"value": _option_text(_project_field(row, value_field)), "label": _option_text(_project_field(row, label_field))}
            if description_fields:
                item["description"] = " · ".join(_option_text(_project_field(row, field)) for field in description_fields)
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


def _json_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _interaction_placeholders(assignments: list[_Assignment]) -> list[tuple[str, _Node]]:
    fields: list[tuple[str, _Node]] = []
    for call in _call_nodes(assignments):
        args = call.children or []
        index = _INTERACTIVE_ITEMS_ARGS.get(str(call.value))
        if index is None or len(args) <= index or args[index].kind != "placeholder":
            continue
        if not args or args[0].kind != "string":
            raise SourceError("Saved view data no longer matches its source contract")
        fields.append((str(args[0].value), args[index]))
    return fields


def hydrate_openui(
    template: str,
    bindings: list[dict[str, Any]],
    registry: SourceRegistry,
    profile: str,
    *,
    collect_allowed_values: bool = False,
) -> str | tuple[str, dict[str, list[str]]]:
    """Fetch values concurrently within one bounded window and insert JSON literals."""
    assignments = _program(template)
    futures = {}
    executor = ThreadPoolExecutor(max_workers=max(1, min(len(bindings), MAX_BINDINGS)))
    try:
        for binding in bindings:
            futures[executor.submit(registry.query, binding["name"], binding["args"], profile)] = binding["alias"]
        done, pending = wait(futures, timeout=11)
        if pending:
            for future in pending:
                future.cancel()
            raise SourceError("Data source is temporarily unavailable")
        values = {futures[future]: future.result() for future in done}
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("Data source is temporarily unavailable") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    replacements: list[tuple[int, int, str]] = []
    projected: dict[int, Any] = {}
    for assignment in assignments:
        for node in _walk(assignment.value):
            if node.kind != "placeholder":
                continue
            spec = _placeholder_for_node(node)
            alias, *rest = spec["path"].split(".")
            if alias not in values:
                raise SourceError("Saved view data no longer matches its source contract")
            value = _project_value(_lookup(values[alias], ".".join(rest)), spec)
            projected[id(node)] = value
            replacements.append((node.start, node.end, _json_literal(value)))
    hydrated = template
    for start, end, literal in sorted(replacements, reverse=True):
        hydrated = hydrated[:start] + literal + hydrated[end:]
    if not collect_allowed_values:
        return hydrated
    allowed: dict[str, list[str]] = {}
    for field, node in _interaction_placeholders(assignments):
        items = projected.get(id(node))
        if not isinstance(items, list) or any(not isinstance(item, dict) or type(item.get("value")) is not str for item in items):
            raise SourceError("Saved view data no longer matches its source contract")
        allowed[field] = sorted(set(allowed.get(field, [])).union(item["value"] for item in items))
    return hydrated, allowed


class SavedViewStore:
    """Private per-profile named templates; they retain no fetched data."""

    def __init__(self, path: Path, *, max_records: int = MAX_SAVED_VIEWS):
        self.path = Path(path)
        self.max_records = max_records
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)

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
        if len(raw) > MAX_OPENUI_BYTES * self.max_records:
            raise SpecError("Saved view store exceeds size cap")
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

    def save(self, name: str, template: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
        if not isinstance(name, str) or not VIEW_NAME.fullmatch(name):
            raise SpecError("name: use 1–48 letters, digits, _ or -")
        if type(overwrite) is not bool:
            raise SpecError("overwrite: expected boolean")
        with self.lock:
            records = self._read()
            if name in records and not overwrite:
                raise SpecError("Saved view already exists; pass overwrite=true to replace it")
            if name not in records and len(records) >= self.max_records:
                raise SpecError("Saved view limit reached")
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

    def delete(self, name: str) -> None:
        if not isinstance(name, str) or not VIEW_NAME.fullmatch(name):
            raise SpecError("name: use 1–48 letters, digits, _ or -")
        with self.lock:
            records = self._read()
            if name not in records:
                raise SpecError("No saved view with that name")
            records.pop(name)
            self._write(records)

    def manifest(self) -> list[dict[str, Any]]:
        with self.lock:
            records = self._read()
            return [
                {"name": name, "title": str(record.get("title", "")), "renderer": str(record.get("renderer", "json")),
                 "sources": [binding.get("name") for binding in record.get("sources", [])]}
                for name, record in sorted(records.items())
            ]
