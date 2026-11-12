"""HTML widgets (Bot API 10.4): build, validate and route taps. Pure helpers; no network here.

A widget is one sandboxed HTML document drawn inside the message bubble (no network, taps only). Its
code is fixed once sent; the bot changes its state (``chat_data``). The plugin injects a tiny runtime so
model-written HTML only has to mark buttons with ``data-action="key"``: a tap sends ``a:<key>`` to the
bot (``widget_query``), the bot answers, records the choice in ``chat_data.chosen`` and hands the press
to the conversation like a choice button. Limits: 32768 bytes of HTML, 4096 bytes after zlib level 9,
1024 bytes of chat_data, 1-64 bytes per request.
"""
from __future__ import annotations

import html as _html
import json
import math
import re
import time
import zlib
from typing import Any

MAX_HTML = 32768
MAX_COMPRESSED = 4096
MAX_DATA = 1024
ACTION_PREFIX = "a:"
STORE_TTL = 30 * 86400
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# Inject before template scripts. All template innerHTML interpolation uses this escape helper.
_TEMPLATE_RUNTIME = """<script>window.HTE={
 esc:function(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})},
 state:function(){try{return JSON.parse(window.TelegramWidget.chatData||'{}')||{}}catch(e){return{}}},
 mount:function(draw){var W=window.TelegramWidget;function render(){draw(window.HTE.state())}if(W)W.onEvent('chatDataChanged',render);render();if(W&&W.ready)W.ready()}
};</script>"""

# Bind newly created buttons after every template render; no event bubbling/MutationObserver needed.
# Keys and pending requests are independent of DOM nodes. Redraw cancels an armed confirmation.
_RUNTIME = (
    "<style>button.hte-chosen::before{content:'\\2713  '}</style>"
    "<script>(function(){var W=window.TelegramWidget;if(!W)return;var K=__KEYS__,pending={};"
    "function S(){try{return JSON.parse(W.chatData||'{}')||{}}catch(e){return{}}}"
    "function B(k){return document.querySelector('[data-action=\"'+k+'\"]')}"
    "function P(){var s=S(),c=s.chosen;K.forEach(function(k){var b=B(k);if(!b)return;"
    "if(!b._hte){b._hte=1;b.addEventListener('click',function(){if(b.disabled||pending[k])return;"
    "var s=S();if(s.chosen!=null||(s.exp&&s.exp<=Date.now()/1000)){P();return}"
    "var q=b.getAttribute('data-confirm');if(q&&!b._a){var o=b.textContent;b._a=1;b.textContent=q;"
    "setTimeout(function(){if(!b.disabled){b._a=0;b.textContent=o}},4000);return}"
    "pending[k]=1;b.disabled=true;W.request('a:'+k).then(function(r){var d={};try{d=JSON.parse(r.result||'{}')}catch(x){}"
    "if(!d.ok||d.again){delete pending[k];P()}},function(){delete pending[k];P()})})}"
    "b.disabled=!!pending[k]||c!=null||!!(s.exp&&s.exp<=Date.now()/1000);"
    "if(k===c)b.classList.add('hte-chosen')})}"
    "W.onEvent('chatDataChanged',P);P();if(W.ready)W.ready()})();</script>"
)


class WidgetError(ValueError):
    pass


def normalize_actions(actions: Any) -> list[dict[str, str]]:
    """[{key, label, value?}] -> validated list (max 6). Keys are what the HTML's data-action names."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, a in enumerate(actions or []):
        if isinstance(a, str):
            a = {"label": a}
        if not isinstance(a, dict):
            continue
        label = " ".join(str(a.get("label") or "").split())[:40]
        key = str(a.get("key") or "").strip() or f"o{i + 1}"
        if not label and not a.get("value"):
            continue
        if not _KEY_RE.match(key):
            raise WidgetError(f"action key {key!r}: use 1-32 letters, digits, - or _")
        if key in seen:
            raise WidgetError(f"duplicate action key {key!r}")
        seen.add(key)
        value = str(a.get("value") or "").strip()
        out.append({"key": key, "label": label or key, "value": value})
    if len(out) > 6:
        raise WidgetError("at most 6 actions")
    return out


def chat_data_obj(value: Any) -> dict[str, Any]:
    """chat_data must be a JSON object (the runtime reserves ``chosen``)."""
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise WidgetError("chat_data must be a JSON object") from exc
    if not isinstance(value, dict):
        raise WidgetError("chat_data must be a JSON object")
    return value


def encode_data(obj: dict[str, Any]) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise WidgetError("chat_data must contain only finite JSON values") from exc
    if len(text.encode()) > MAX_DATA:
        raise WidgetError(f"chat_data is {len(text.encode())} bytes; the limit is {MAX_DATA}")
    return text


def check_action_data(data: dict[str, Any], actions: list[dict[str, str]]) -> None:
    """Reserve enough state bytes to record any registered choice after a successful tap."""
    encode_data(data)
    if actions:
        longest = max((a["key"] for a in actions), key=len)
        try:
            encode_data(dict(data, chosen=longest))
        except WidgetError as exc:
            raise WidgetError(f"chat_data needs room for reserved chosen state: {exc}") from exc


def _buttons_html(actions: list[dict[str, str]]) -> str:
    btns = "".join(f'<button data-action="{a["key"]}">{_html.escape(a["label"])}</button>' for a in actions)
    # the message time sits over the widget's bottom corner when there is no caption: keep taps clear of it
    # side padding keeps buttons off the bubble edge; bottom keeps them clear of the timestamp
    return f'<div style="display:flex;flex-wrap:wrap;gap:8px;padding:10px 12px 18px">{btns}</div>'


def build_html(body: str, actions: list[dict[str, str]]) -> str:
    """Model HTML + (auto buttons when the HTML has no data-action elements) + the tap runtime."""
    doc = str(body or "")
    if "<!--hte-runtime-->" in doc:
        doc = doc.replace("<!--hte-runtime-->", _TEMPLATE_RUNTIME, 1)
    if "\x00" in doc:
        raise WidgetError("html must not contain NUL")
    if not doc.strip():
        raise WidgetError("html is required")
    if actions:
        missing = [a for a in actions if f'data-action="{a["key"]}"' not in doc]
        if "data-hte-actions" in doc:  # a template that draws its own data-action buttons from chat_data
            missing = []
        if len(missing) == len(actions):
            doc += _buttons_html(actions)
        elif missing:
            raise WidgetError("html has data-action buttons but not for: " + ", ".join(a["key"] for a in missing))
        doc += _RUNTIME.replace("__KEYS__", json.dumps([a["key"] for a in actions], separators=(",", ":")))
    return doc


def size_report(doc: str) -> tuple[int, int]:
    raw = doc.encode()
    return len(raw), len(zlib.compress(raw, 9))


def check_size(doc: str) -> tuple[int, int]:
    raw, packed = size_report(doc)
    if raw > MAX_HTML:
        raise WidgetError(f"html is {raw} bytes; the limit is {MAX_HTML}")
    if packed > MAX_COMPRESSED:
        raise WidgetError(f"html compresses to {packed} bytes (zlib -9); the limit is {MAX_COMPRESSED}. "
                          "Shorten text/CSS, drop inline images.")
    return raw, packed


def parse_query(data: str) -> str | None:
    """'a:<key>' -> key; anything else -> None."""
    data = str(data or "")
    if data.startswith(ACTION_PREFIX) and _KEY_RE.match(data[len(ACTION_PREFIX):]):
        return data[len(ACTION_PREFIX):]
    return None


def store_key(chat_id: Any, message_id: Any) -> str:
    return f"{chat_id}:{message_id}"


def claim(rec: dict[str, Any] | None, key: str, user_id: str) -> tuple[str, dict[str, str] | None]:
    """Decide a tap against the stored record; mutates ``rec`` on success. -> (status, action)."""
    if not rec:
        return "gone", None
    if str(user_id) != str(rec.get("user_id") or ""):
        return "not_yours", None
    action = next((a for a in rec.get("actions") or [] if a["key"] == key), None)
    if action is None:
        return "invalid", None
    if rec.get("once", True) and rec.get("chosen") is not None:
        return "used", None
    if rec.get("expires_at") and time.time() > float(rec["expires_at"]):
        return "expired", None
    rec["chosen"] = key
    rec["pressed_at"] = time.time()
    return "ok", action


def injection_text(rec: dict[str, Any], action: dict[str, str], who: str) -> str:
    lines = [f"[Telegram 小组件] {who} 点击了「{action['label']}」。"]
    if action.get("value") and action["value"] != action["label"]:
        lines.append(f"value: {action['value']}")
    if rec.get("title"):
        lines.append(f"问题: {rec['title']}")
    lines.append(f"widget: {rec.get('chat_id')}:{rec.get('message_id')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- templates
# A template is one HTML file that draws itself from chat_data, so reusing it means: name + data.
# Optional first line: <!--hte {"desc": "...", "example": {...}, "actions_from": "o"}--> (stripped before
# sending). actions_from names a list in chat_data whose items {k, n, v?} become the tap actions.

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_META_RE = re.compile(r"^\s*<!--hte\s+(\{.*?\})\s*-->\s*", re.S)


def parse_template(text: str) -> tuple[dict[str, Any], str]:
    m = _META_RE.match(text or "")
    if not m:
        return {}, text or ""
    try:
        meta = json.loads(m.group(1))
    except ValueError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def _template_files(dirs: list[Any]) -> dict[str, Any]:
    """name -> path; later dirs win (user templates override built-ins)."""
    from pathlib import Path
    found: dict[str, Any] = {}
    for d in dirs:
        p = Path(d)
        if p.is_dir():
            for f in sorted(p.glob("*.html")):
                if _NAME_RE.match(f.stem):
                    found[f.stem] = f
    return found


def load_template(dirs: list[Any], name: str) -> tuple[dict[str, Any], str]:
    path = _template_files(dirs).get(str(name or "").strip().lower())
    if path is None:
        raise WidgetError(f"no widget template {name!r}; action=templates lists them")
    return parse_template(path.read_text(encoding="utf-8"))


def validate_schema(value: Any, schema: dict[str, Any], path: str = "data") -> None:
    """Small, dependency-free JSON Schema subset used by the shipped, versioned contracts.

    Supports type/anyOf/enum, object properties/required/additionalProperties, array
    items/prefixItems/minItems/maxItems, string minLength/maxLength/pattern, numeric bounds.
    This is a data validator, not a general JSON Schema implementation.
    """
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate_schema(value, option, path)
                return
            except WidgetError:
                pass
        raise WidgetError(f"{path}: does not match any allowed type/value")
    kind = schema.get("type")
    if isinstance(kind, list):
        validate_schema(value, {"anyOf": [dict(schema, type=t) for t in kind]}, path)
        return
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": type(value) is bool,
               "integer": type(value) is int, "number": type(value) in (int, float),
               "null": value is None}
    if kind and not matches.get(kind, False):
        raise WidgetError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise WidgetError(f"{path}: expected one of {schema['enum']}")
    if type(value) in (int, float):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise WidgetError(f"{path}: expected a finite number")
        for bound, fail in (("minimum", value < schema.get("minimum", value)),
                            ("maximum", value > schema.get("maximum", value))):
            if fail:
                raise WidgetError(f"{path}: {bound} is {schema[bound]}")
    if isinstance(value, str):
        if any(ord(c) < 32 and c not in '\n\t\r' for c in value) or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise WidgetError(f"{path}: invalid control character or Unicode surrogate")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            raise WidgetError(f"{path}: length must be {schema.get('minLength', 0)}..{schema.get('maxLength', 'unlimited')}")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise WidgetError(f"{path}: must match {schema['pattern']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise WidgetError(f"{path}: array size must be {schema.get('minItems', 0)}..{schema.get('maxItems', 'unlimited')}")
        prefix = schema.get("prefixItems", [])
        for i, item in enumerate(value):
            rule = prefix[i] if i < len(prefix) else schema.get("items", {})
            validate_schema(item, rule, f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise WidgetError(f"{path}.{key}: required")
        for key, item in value.items():
            if key not in props and schema.get("additionalProperties") is False:
                raise WidgetError(f"{path}.{key}: unknown field")
            validate_schema(item, props.get(key, {}), f"{path}.{key}")


def validate_template_data(meta: dict[str, Any], data: dict[str, Any]) -> None:
    schema: dict[str, Any] = meta.get("schema") or {"type": "object"}  # legacy user templates
    props: dict[str, Any] = dict(schema.get("properties", {}))
    props.update(chosen={"type": ["string", "null"], "pattern": "[A-Za-z0-9_-]{1,32}"},
                 exp={"type": "number", "minimum": 0})
    validate_schema(data, dict(schema, properties=props))
    if meta.get("contract") == "tracker" and data["i"] >= len(data["s"]):
        raise WidgetError("data.i: must be a valid zero-based index in data.s")
    if meta.get("actions_from"):
        actions_from_data(data, meta["actions_from"])  # also reject duplicate action keys


def list_templates(dirs: list[Any]) -> list[dict[str, Any]]:
    out = []
    for name, path in sorted(_template_files(dirs).items()):
        meta, _ = parse_template(path.read_text(encoding="utf-8"))
        out.append({"name": name, "desc": meta.get("desc", ""), "example": meta.get("example"),
                    "schema": meta.get("schema", {"type": "object"}), "version": meta.get("version", 1),
                    "taps": bool(meta.get("actions_from"))})
    return out


def save_template(directory: Any, name: str, doc: str, desc: str = "", example: Any = None,
                  actions_from: str = "", schema: Any = None) -> Any:
    from pathlib import Path
    name = str(name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise WidgetError("template name: 1-32 lowercase letters, digits, - or _")
    header, body = parse_template(doc)
    check_size(build_html(body, []))
    contract = schema if schema is not None else header.get("schema", {"type": "object"})
    if not isinstance(contract, dict) or contract.get("type") != "object":
        raise WidgetError("schema: expected an object schema with type=object")
    meta: dict[str, Any] = {"desc": " ".join(str(desc or "").split())[:200],
                            "schema": contract, "version": 1}
    if example is not None:
        meta["example"] = chat_data_obj(example)
        validate_template_data(meta, meta["example"])
    if actions_from:
        meta["actions_from"] = str(actions_from)
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.html"
    path.write_text("<!--hte " + json.dumps(meta, ensure_ascii=False) + "-->\n" + body.strip() + "\n", encoding="utf-8")
    return path


def actions_from_data(data: dict[str, Any], field: str) -> list[dict[str, str]]:
    items = data.get(field) if field else None
    acts = [{"key": str(o.get("k")), "label": str(o.get("n") or o.get("k")), "value": str(o.get("v") or "")}
            for o in (items if isinstance(items, list) else []) if isinstance(o, dict) and o.get("k")]
    return normalize_actions(acts)


def prune(data: dict[str, Any], now: float) -> None:
    for k in [k for k, v in data.items() if now - float((v or {}).get("created_at") or 0) > STORE_TTL]:
        data.pop(k, None)
