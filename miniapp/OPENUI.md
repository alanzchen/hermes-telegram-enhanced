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
- A placeholder may apply one safe projection or formatter: `{{cards.rows|pluck:card}}`, `{{cards.rows|table:card,due,status}}`, and `{{queue.rows|items:transaction_id,merchant}}`. Projection field names are checked against the declared source-output schema when the view is saved; a misspelling is rejected before anything is stored.
- Per-field table/item formatting uses `@`: `{{cards.rows|table:card,due@date:MM/DD,statement_balance@money:USD:2,purchase_apr@percent:1,status}}`. Scalar `Text` values can use `{{overview.data.cash|money}}` (USD, 2 decimals by default), `money:USD:2`, `date:MM/DD`, or `percent:1`. `percent` accepts either a fractional value such as `0.249` or a percent-point value such as `24.9` and renders both as `24.9%`. Formatters render `null` as `—`; unformatted `Text` values also safely coerce numbers and `null`.
- Every `Button` action `type` must occur in the top-level `actions` allowlist. Buttons and forms only inject a conversation event; the page never performs an operation.
- Event values have the shape `{params, form, form_name}`. `params` is the declared Button parameter object and `form` contains the current input values. An action may declare `fields: ["transaction_ids"]`; the matching Button should include `fields: ["transaction_ids"]` too, and only those form entries are emitted. For any view using a `sensitive` source, the chat-bound event is further minimized to those declared field names and option values; it never contains labels or rendered source data.

Only the component subset below is accepted. `@` directives, `$` bindings, arbitrary components, queries, mutations, URL opening and raw code are rejected before a view can be stored.

## Complete finance dashboard example

This saves a template only; the data sources are queried only when the authenticated Mini App opens. It is intentionally a source-contract example and contains no account values.

```json
{
  "action": "save",
  "name": "finance-dashboard",
  "title": "Finance dashboard",
  "renderer": "openui",
  "openui": "root = Card([header, tabs]\nheader = CardHeader(\"Finance dashboard\", \"Live data\")\ncash = Text({{overview.data.cash|money}})\ncardBalance = Text({{overview.data.card_balance|money:USD:2}})\nalerts = Text({{overview.data.alerts}})\nstatus = Badge(\"Live\", \"secondary\")\ncardsTable = Table([\"Card\", \"Due\", \"Balance\", \"APR\", \"Status\"], {{cards.rows|table:card,due@date:MM/DD,statement_balance@money:USD:2,purchase_apr@percent:1,status}})\nspendingChart = BarChart({{spending.rows|pluck:key}}, {{spending.rows|pluck:amount}}, \"Spending by month\")\nreviewSelect = Select(\"transaction_ids\", {{review_queue.rows|items:transaction_id,merchant}}, \"Choose a transaction\")\nreviewed = Checkbox(\"acknowledged\", \"I reviewed the selected transaction\")\nsubmitReview = Button(\"Submit review\", {type: \"submit_review\", fields: [\"transaction_ids\"]})\nreviewForm = Form(\"review\", [reviewSelect, reviewed, submitReview])\ntabs = Tabs([{value: \"overview\", trigger: \"Overview\", content: [status, cash, cardBalance, alerts]}, {value: \"cards\", trigger: \"Cards\", content: [cardsTable]}, {value: \"spending\", trigger: \"Spending\", content: [spendingChart]}, {value: \"review\", trigger: \"Review\", content: [reviewForm]}], \"overview\")",
  "sources": [
    {"alias": "overview", "name": "finance.overview", "args": {}},
    {"alias": "cards", "name": "finance.cards", "args": {}},
    {
      "alias": "spending",
      "name": "finance.spending",
      "args": {"group_by": "month", "days": 120, "limit": 6}
    },
    {
      "alias": "review_queue",
      "name": "finance.review_queue",
      "args": {"days": 45}
    }
  ],
  "actions": [
    {
      "key": "submit_review",
      "label": "Submit review",
      "fields": ["transaction_ids"]
    }
  ]
}
```

The `reviewed` checkbox remains UI-only for this sensitive view: `fields` makes the chat-bound submit event contain only `form.transaction_ids` (the selected transaction ID), not its merchant label or any other rendered value.

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
