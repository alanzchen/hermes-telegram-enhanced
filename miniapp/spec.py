"""Version 1 fixed UI catalog. Data only; no markup, code or executable actions."""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re
from urllib.parse import urlsplit

MAX_SPEC_BYTES = 65536
KEY = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


class SpecError(ValueError):
    pass


from typing import NoReturn


def fail(path, message) -> NoReturn:
    raise SpecError(f"{path}: {message}")


def obj(value, path, allowed, required=()):
    if not isinstance(value, dict):
        fail(path, "expected object")
    unknown = set(value) - set(allowed)
    if unknown:
        fail(path, "unknown fields: " + ", ".join(sorted(unknown)))
    for key in required:
        if key not in value:
            fail(path, f"{key} is required")
    return value


def text(value, path, limit=4000, empty=True):
    if (
        not isinstance(value, str)
        or len(value) > limit
        or "\x00" in value
        or (not empty and not value.strip())
    ):
        fail(
            path,
            f"expected {'nonempty ' if not empty else ''}text, max {limit} characters",
        )
    return value


def key(value, path):
    if not isinstance(value, str) or not KEY.fullmatch(value):
        fail(path, "use 1–48 letters, digits, _ or -")
    if value in ("__proto__", "constructor", "prototype"):
        fail(path, "reserved key")
    return value


def array(value, path, limit=100, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= limit:
        fail(path, f"expected {minimum}–{limit} items")
    return value


def boolean(value, path):
    if type(value) is not bool:
        fail(path, "expected boolean")


def number(value, path, lo=-1e12, hi=1e12):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not lo <= value <= hi
    ):
        fail(path, f"expected finite number in [{lo}, {hi}]")
    return value


def date(value, path):
    text(value, path, 10, False)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail(path, "expected YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        fail(path, "invalid calendar date")


def enum(value, values, path):
    if value not in values:
        fail(path, "expected one of " + ", ".join(map(str, values)))


def options(value, path):
    seen = set()
    for i, option in enumerate(array(value, path, 40, 1)):
        p = f"{path}[{i}]"
        obj(option, p, ("value", "label", "details"), ("value", "label"))
        key(option["value"], p + ".value")
        text(option["label"], p + ".label", 200, False)
        if "details" in option:
            text(option["details"], p + ".details", 2000)
        if option["value"] in seen:
            fail(p, "duplicate option value")
        seen.add(option["value"])


def field_value(field, value, path):
    kind = field["type"]
    if value is None:
        if field.get("required"):
            fail(path, "required")
        return
    if kind == "number":
        number(value, path, field.get("min", -1e12), field.get("max", 1e12))
    elif kind == "toggle":
        boolean(value, path)
    elif kind == "date":
        date(value, path)
    elif kind == "select":
        if not isinstance(value, str) or value not in [
            o["value"] for o in field["options"]
        ]:
            fail(path, "unknown option")
    else:
        text(
            value, path, field.get("max_length", 1000), not field.get("required", False)
        )


def validate_spec(spec):
    """Reject unknown properties and unsafe types; return an independent JSON copy."""
    try:
        raw = json.dumps(spec, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        fail("spec", "must be finite JSON data")
    if len(raw) > MAX_SPEC_BYTES:
        fail("spec", "exceeds 64 KiB")
    obj(spec, "spec", ("version", "components"), ("version", "components"))
    if type(spec["version"]) is not int or spec["version"] != 1:
        fail("version", "only version 1 is supported")
    action_keys = set()
    ids = set()
    count = 0

    def register_action(a, p):
        obj(a, p, ("key", "label", "value", "once"), ("key", "label"))
        key(a["key"], p + ".key")
        text(a["label"], p + ".label", 120, False)
        if a["key"] in action_keys:
            fail(p, "duplicate action key")
        action_keys.add(a["key"])
        if len(action_keys) > 64:
            fail(p, "at most 64 actions")
        if "value" in a:
            text(a["value"], p + ".value", 2000)
        if "once" in a:
            boolean(a["once"], p + ".once")

    def walk(components, p="components", depth=0):
        nonlocal count
        if depth > 4:
            fail(p, "nesting exceeds 4")
        for i, c in enumerate(array(components, p, 100)):
            q = f"{p}[{i}]"
            count += 1
            if count > 200:
                fail(q, "at most 200 components")
            if not isinstance(c, dict):
                fail(q, "expected component object")
            t = c.get("type")
            allowed = {
                "heading": ("text", "level"),
                "text": ("text", "markdown"),
                "callout": ("text", "title", "tone"),
                "kv": ("items",),
                "table": ("columns", "rows", "sortable"),
                "list": ("items", "ordered"),
                "checklist": ("items",),
                "choice": (
                    "id",
                    "label",
                    "options",
                    "multiple",
                    "max_selected",
                    "action",
                ),
                "form": ("id", "title", "fields", "action"),
                "buttons": ("id", "actions", "once"),
                "chart": ("kind", "title", "labels", "series", "unit", "source"),
                "gantt": ("title", "tasks", "today"),
                "flow": ("title", "nodes", "edges"),
                "timeline": ("items",),
                "image": ("url", "alt", "caption"),
                "divider": (),
                "tabs": ("tabs",),
                "section": ("title", "components"),
            }
            if not isinstance(t, str) or t not in allowed:
                fail(q, "unknown component type")
            obj(c, q, ("type",) + allowed[t])
            for name in ("title", "label", "alt", "caption", "unit", "source"):
                if name in c:
                    text(c[name], q + "." + name, 500)
            for name in ("markdown", "sortable", "ordered", "multiple", "once"):
                if name in c:
                    boolean(c[name], q + "." + name)
            if t in ("heading", "text", "callout"):
                text(c.get("text"), q + ".text")
                if t == "heading":
                    if type(c.get("level", 2)) is not int:
                        fail(q, "level must be integer")
                    enum(c.get("level", 2), (1, 2, 3), q + ".level")
                if t == "callout":
                    enum(
                        c.get("tone", "info"),
                        ("info", "success", "warning", "danger"),
                        q + ".tone",
                    )
            elif t in ("kv", "list", "checklist", "timeline"):
                for j, item in enumerate(array(c.get("items"), q + ".items", 100)):
                    r = f"{q}.items[{j}]"
                    if t == "list":
                        text(item, r)
                    elif t == "kv":
                        obj(item, r, ("key", "value"), ("key", "value"))
                        text(item["key"], r + ".key", 200)
                        text(item["value"], r + ".value", 2000)
                    elif t == "checklist":
                        obj(item, r, ("text", "checked"), ("text",))
                        text(item["text"], r + ".text")
                        boolean(item.get("checked", False), r + ".checked")
                    else:
                        obj(item, r, ("date", "title", "text"), ("title",))
                        for name in item:
                            text(item[name], r + "." + name, 2000)
            elif t == "table":
                cols = array(c.get("columns"), q + ".columns", 20, 1)
                for x in cols:
                    text(x, q + ".columns", 200, False)
                for row in array(c.get("rows"), q + ".rows", 200):
                    array(row, q + ".row", len(cols), len(cols))
                    for cell in row:
                        if cell is None or type(cell) is bool:
                            continue
                        if type(cell) in (int, float):
                            number(cell, q + ".cell")
                        else:
                            text(cell, q + ".cell", 1000)
            elif t in ("choice", "form", "buttons"):
                key(c.get("id"), q + ".id")
                if c["id"] in ids:
                    fail(q, "duplicate component id")
                ids.add(c["id"])
                if t == "buttons":
                    for j, a in enumerate(
                        array(c.get("actions"), q + ".actions", 12, 1)
                    ):
                        register_action(a, f"{q}.actions[{j}]")
                else:
                    register_action(c.get("action"), q + ".action")
                if t == "choice":
                    text(c.get("label"), q + ".label", 500, False)
                    options(c.get("options"), q + ".options")
                    if "max_selected" in c:
                        if type(c["max_selected"]) is not int:
                            fail(q, "max_selected must be integer")
                        number(
                            c["max_selected"], q + ".max_selected", 1, len(c["options"])
                        )
                elif t == "form":
                    keys = set()
                    for f in array(c.get("fields"), q + ".fields", 20, 1):
                        obj(
                            f,
                            q + ".field",
                            (
                                "key",
                                "type",
                                "label",
                                "required",
                                "max_length",
                                "min",
                                "max",
                                "options",
                                "default",
                                "placeholder",
                            ),
                            ("key", "type", "label"),
                        )
                        key(f["key"], q + ".field.key")
                        text(f["label"], q + ".field.label", 200, False)
                        if f["key"] in keys:
                            fail(q, "duplicate field key")
                        keys.add(f["key"])
                        enum(
                            f["type"],
                            ("text", "number", "date", "select", "toggle"),
                            q + ".field.type",
                        )
                        if "required" in f:
                            boolean(f["required"], q + ".required")
                        if "placeholder" in f:
                            text(f["placeholder"], q + ".placeholder", 200)
                        if "max_length" in f:
                            if f["type"] != "text" or type(f["max_length"]) is not int:
                                fail(
                                    q,
                                    "max_length only applies to text; must be integer",
                                )
                            number(f["max_length"], q + ".max_length", 1, 4000)
                        for bound in ("min", "max"):
                            if bound in f:
                                if f["type"] != "number":
                                    fail(q, "min/max only apply to number")
                                number(f[bound], q + "." + bound)
                        if f.get("min", -1e12) > f.get("max", 1e12):
                            fail(q, "min exceeds max")
                        if f["type"] == "select":
                            options(f.get("options"), q + ".options")
                        elif "options" in f:
                            fail(q, "options only apply to select")
                        if "default" in f:
                            field_value(f, f["default"], q + ".default")
            elif t == "chart":
                enum(c.get("kind"), ("line", "bar", "hbar", "pie-lite"), q + ".kind")
                labels = array(c.get("labels"), q + ".labels", 100, 1)
                for label in labels:
                    text(label, q + ".label", 120)
                for s in array(c.get("series"), q + ".series", 6, 1):
                    obj(s, q + ".series", ("name", "values"), ("name", "values"))
                    text(s["name"], q + ".name", 120)
                    for v in array(
                        s["values"], q + ".values", len(labels), len(labels)
                    ):
                        number(v, q + ".value", 0 if c["kind"] == "pie-lite" else -1e12)
                if c["kind"] == "pie-lite" and (
                    len(c["series"]) != 1 or not sum(c["series"][0]["values"])
                ):
                    fail(q, "pie-lite needs one nonzero nonnegative series")
            elif t == "gantt":
                tasks = array(c.get("tasks"), q + ".tasks", 60, 1)
                taskids = set()
                for task in tasks:
                    obj(
                        task,
                        q + ".task",
                        (
                            "id",
                            "label",
                            "start",
                            "end",
                            "group",
                            "milestone",
                            "depends_on",
                            "progress",
                        ),
                        ("id", "label", "start", "end"),
                    )
                    key(task["id"], q + ".id")
                    text(task["label"], q + ".label", 160, False)
                    if task["id"] in taskids:
                        fail(q, "duplicate task id")
                    taskids.add(task["id"])
                    if date(task["start"], q + ".start") > date(
                        task["end"], q + ".end"
                    ):
                        fail(q, "task ends before it starts")
                    if "group" in task:
                        text(task["group"], q + ".group", 120)
                    if "milestone" in task:
                        boolean(task["milestone"], q + ".milestone")
                    if "progress" in task:
                        number(task["progress"], q + ".progress", 0, 1)
                for task in tasks:
                    for dep in array(task.get("depends_on", []), q + ".depends_on", 60):
                        if (
                            not isinstance(dep, str)
                            or dep not in taskids
                            or dep == task["id"]
                        ):
                            fail(q, "invalid task dependency")
                if "today" in c:
                    date(c["today"], q + ".today")
            elif t == "flow":
                nodes = array(c.get("nodes"), q + ".nodes", 60, 1)
                nodeids = set()
                for n in nodes:
                    obj(n, q + ".node", ("id", "label", "shape"), ("id", "label"))
                    key(n["id"], q + ".id")
                    text(n["label"], q + ".label", 160, False)
                    enum(n.get("shape", "rect"), ("rect", "diamond"), q + ".shape")
                    if n["id"] in nodeids:
                        fail(q, "duplicate node id")
                    nodeids.add(n["id"])
                for e in array(c.get("edges"), q + ".edges", 120):
                    obj(e, q + ".edge", ("from", "to", "label"), ("from", "to"))
                    if any(
                        not isinstance(e[k], str) or e[k] not in nodeids
                        for k in ("from", "to")
                    ):
                        fail(q, "edge refers to missing node")
                    if "label" in e:
                        text(e["label"], q + ".edge.label", 120)
            elif t == "image":
                text(c.get("url"), q + ".url", 2048, False)
                try:
                    u = urlsplit(c["url"])
                except ValueError:
                    fail(q, "invalid image URL")
                if (
                    u.scheme != "https"
                    or not u.hostname
                    or u.username
                    or u.password
                    or any(ch.isspace() for ch in c["url"])
                ):
                    fail(q, "image needs absolute HTTPS URL without credentials")
            elif t == "tabs":
                for tab in array(c.get("tabs"), q + ".tabs", 8, 1):
                    obj(
                        tab,
                        q + ".tab",
                        ("label", "components"),
                        ("label", "components"),
                    )
                    text(tab["label"], q + ".tab.label", 80, False)
                    walk(tab["components"], q + ".tab.components", depth + 1)
            elif t == "section":
                text(c.get("title"), q + ".title", 500, False)
                walk(c.get("components"), q + ".components", depth + 1)

    walk(spec["components"])
    return copy.deepcopy(spec)


def action_catalog(spec):
    found = {}

    def walk(components):
        for c in components:
            t = c["type"]
            if t == "buttons":
                for a in c["actions"]:
                    found[a["key"]] = (c, a)
            elif t in ("form", "choice"):
                found[c["action"]["key"]] = (c, c["action"])
            elif t == "section":
                walk(c["components"])
            elif t == "tabs":
                for tab in c["tabs"]:
                    walk(tab["components"])

    walk(spec["components"])
    return found


def validate_action(spec, action_key, value):
    """Use server-side labels/values. Never accept client prompts or action metadata."""
    pair = action_catalog(spec).get(action_key) if isinstance(action_key, str) else None
    if pair is None:
        fail("key", "unknown action")
    c, a = pair
    t = c["type"]
    out = {
        "key": a["key"],
        "label": a["label"],
        "component_id": c["id"],
        "kind": t,
        "once": a.get("once", True),
    }
    if t == "buttons":
        if value is not None:
            fail("value", "buttons accept null only")
        out["value"] = a.get("value", "")
        out["group_once"] = c.get("once", True)
    elif t == "choice":
        values = value if c.get("multiple", False) else [value]
        array(values, "value", c.get("max_selected", len(c["options"])), 1)
        choices = {o["value"]: o["label"] for o in c["options"]}
        if any(not isinstance(v, str) or v not in choices for v in values):
            fail("value", "unknown option")
        if len(set(values)) != len(values):
            fail("value", "duplicate selections")
        out["value"] = copy.deepcopy(value)
        out["selection_labels"] = [choices[v] for v in values]
        out["meaning"] = a.get("value", "")
    else:
        fields = {f["key"]: f for f in c["fields"]}
        obj(value, "value", fields)
        for name, f in fields.items():
            field_value(f, value.get(name), "value." + name)
        out["value"] = copy.deepcopy(value)
        out["field_labels"] = {k: f["label"] for k, f in fields.items() if k in value}
        out["meaning"] = a.get("value", "")
    return out
