# OpenUI Mini App contract

`telegram_ui(action="catalog")` returns this contract together with only the data-source manifests available to the active profile. It never returns fetched source values.

## Send or save an OpenUI view

```json
{
  "action": "save",
  "name": "finance-summary",
  "title": "Finance summary",
  "renderer": "openui",
  "openui": "root = Card([header, note, actions])\nheader = CardHeader(\"Finance summary\", \"Live data\")\nnote = Text({{overview.data.last_sync_local}})\nrefresh = Button(\"Refresh\", {type: \"refresh\"})\nactions = ButtonGroup([refresh])",
  "sources": [
    {"alias": "overview", "name": "finance.overview", "args": {}}
  ],
  "actions": [{"key": "refresh", "label": "Refresh", "once": false}]
}
```

- `save` stores the generated template only. It does not fetch data.
- `open_saved` creates a fresh expiring Mini App launch view from `name` without generating a new UI.
- `send` creates a one-off view with the same fields.
- Data placeholders are **whole values** written as `{{alias.path}}`. The server replaces them with JSON literals only after Telegram `initData` authentication.
- Every `Button` action `type` must occur in the top-level `actions` allowlist. Buttons and forms only inject a conversation event; the page never performs an operation.
- Event values have the shape `{params, form, form_name}`. `params` is the declared Button parameter object and `form` contains the current input values. The server validates the action key, records the submission, and sends it back to the conversation.

Only the component subset below is accepted. `@` directives, `$` bindings, arbitrary components, queries, mutations, URL opening and raw code are rejected before a view can be stored.

## Component subset

- `Card(children)` — required root component; place content in an array.
- `CardHeader(title, description?)`
- `Text(text, size?)` — `size` is `small`, `default`, or `large`.
- `Markdown(text)`
- `Alert(title, description, variant?)` — `default` or `destructive`.
- `Badge(text, variant?)` — `default`, `secondary`, `outline`, or `destructive`.
- `Separator()`
- `Table(columns, rows)`
- `BarChart(labels, values, label?)`
- `Progress(value, label?)` — `value` is 0–100.
- `Accordion(items)` — item `{value, trigger, content}`.
- `Tabs(items, defaultValue?)` — item `{value, trigger, content}`.
- `Form(name, fields)` — fields may contain `TextInput`, `Select`, `Checkbox`, `RadioGroup`, and `DateInput`.
- `TextInput(name, placeholder?, type?)` — `type` is `text`, `email`, or `number`.
- `Select(name, items, placeholder?)` — item `{value,label}`.
- `Checkbox(name, label)`
- `RadioGroup(name, items)` — item `{value,label}`.
- `DateInput(name, label?)`
- `Button(label, {type, params?}, variant?)` — variants are `default`, `secondary`, `outline`, `destructive`, `ghost`, or `link`.
- `ButtonGroup(buttons)`

The vetted bundle is local (`openui.js` + `openui.css`), does not use a CDN, has no `eval`/`new Function`, and loads no network resources itself. It deliberately excludes Drawers, dialogs/popovers, mutations, queries and URL actions.
