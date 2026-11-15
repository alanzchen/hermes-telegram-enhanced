# OpenUI Mini App contract

`telegram_ui(action="catalog")` returns this contract plus the source manifests available to the active profile. It never returns fetched values.

## Send or save an OpenUI view

```json
{
  "action": "save",
  "name": "finance-summary",
  "title": "财务总览",
  "renderer": "openui",
  "openui": "root = Card([header, stats])\nheader = CardHeader(\"财务总览\", \"仅在打开后读取合成数据\")\ncash = Stat(\"可用现金\", {{overview.data.cash|money}}, \"含待入账\", \"success\")\nbalance = Stat(\"信用卡余额\", {{overview.data.card_balance|money}}, \"本期账单\", \"danger\")\nstats = Grid([cash, balance], 2)",
  "sources": [
    {"alias": "overview", "name": "finance.overview", "args": {}}
  ],
  "actions": []
}
```

- `save` stores the template only and does **not** fetch source data.
- `open_saved` creates a fresh expiring Mini App launch view from a saved name.
- `send` creates a one-off view with the same fields.
- Source placeholders are whole values: `{{alias.path}}`. The server inserts JSON literals only after Telegram `initData` authentication.
- Each placeholder may use one safe projection or formatter. Source paths and projected fields are checked against the declared source-output schema before a template is saved.
- Every `Button` action `type` must occur in the top-level `actions` allowlist. Buttons only emit conversation events; the page never invokes an operation itself.

## Safe data projections and formatters

| Form | Result |
| --- | --- |
| `{{rows|pluck:field}}` | An array of one declared field. |
| `{{rows|table:name,amount@money:USD:2}}` | A table matrix. Per-field formatters use `@`. |
| `{{rows|items:id,merchant,date|date,amount@money:USD:2,card}}` | Checkbox/select items: `{value,label,description}`. The optional 1–3 description fields are joined with ` · `. `field|date` is accepted for the compact description form. |
| `{{rows|list:title,due@date:MM/DD,balance@money:USD:2,status@map:...,status@map:...}}` | Mobile row objects: title, subtitle, trailing value, badge, and optional badge tone. |
| `{{overview.data.cash|money}}` | USD with two decimals by default. |
| `{{row.date|date:MM/DD}}` | Month/day date. `date` is also accepted for item descriptions. |
| `{{row.apr|percent:1}}` | A percentage; fractional and percent-point source values are both accepted. |
| `{{row.status|map:paid_full=已还清,unpaid=未还,paid_partial=部分还款,no_balance=无欠款}}` | Maps a declared string enum to display text. |

`map:` is deliberately strict: the source property must declare a string `enum`, and the map must cover **exactly** the declared enum values. Invalid, missing, or extra enum keys reject the view before it is saved. `money`, `percent`, and `date` render `null` as `—`.

For `List`, five projected fields become `{title, subtitle, trailing, badge, badgeTone}`. This keeps source values separate from display text and makes status-tone decisions explicit:

```text
cards = List({{cards.rows|list:
  card,
  due@date:MM/DD,
  statement_balance@money:USD:2,
  status@map:paid_full=已还清,unpaid=未还,paid_partial=部分还款,no_balance=无欠款,
  status@map:paid_full=success,unpaid=warning,paid_partial=warning,no_balance=secondary
}})
```

## Complete Chinese synthetic finance dashboard

This example is intentionally synthetic. It is the shape used by the phone regression harness; it does not call `finance_sources.py` or expose account values. The `categories` binding is a synthetic source registered by the test harness.

```json
{
  "action": "save",
  "name": "finance-dashboard",
  "title": "财务总览",
  "renderer": "openui",
  "openui": "root = Card([header, tabs])\nheader = CardHeader(\"财务总览\", \"合成演示数据\")\ncash = Stat(\"可用现金\", {{overview.data.cash|money}}, \"含待入账\", \"success\")\nbalance = Stat(\"信用卡余额\", {{overview.data.card_balance|money:USD:2}}, \"本期账单\", \"danger\")\ndue = Stat(\"14 天内待还\", {{overview.data.due_next_14d|money}}, \"下个到期日\", \"warning\")\nalerts = Stat(\"待确认\", {{overview.data.alerts}}, \"需要复核\", \"warning\")\nmetrics = Grid([cash, balance, due, alerts], 2)\nsync = Text({{overview.data.last_sync_local|date:MM/DD}}, \"small\")\ncards = List({{cards.rows|list:card,due@date:MM/DD,statement_balance@money:USD:2,status@map:paid_full=已还清,unpaid=未还,paid_partial=部分还款,no_balance=无欠款,status@map:paid_full=success,unpaid=warning,paid_partial=warning,no_balance=secondary}})\nchart = BarChart({{spending.rows|pluck:key}}, {{spending.rows|pluck:amount}}, \"近三月消费\", \"money\")\ncategories = Table([\"类别\", \"金额\", \"笔数\"], {{categories.rows|table:category,amount@money:USD:2,count}})\nreview = CheckboxGroup(\"transaction_ids\", {{review_queue.rows|items:transaction_id,merchant,date|date,amount@money:USD:2,card}})\nsubmit = Button(\"提交确认\", {type: \"submit_review\", fields: [\"transaction_ids\"]})\nreviewForm = Form(\"review\", [review, submit])\ntabs = Tabs([{value: \"overview\", trigger: \"概览\", content: [metrics, sync]}, {value: \"cards\", trigger: \"卡款\", content: [cards]}, {value: \"spending\", trigger: \"消费\", content: [chart, categories]}, {value: \"review\", trigger: \"待确认\", content: [reviewForm]}], \"overview\")",
  "sources": [
    {"alias": "overview", "name": "finance.overview", "args": {}},
    {"alias": "cards", "name": "finance.cards", "args": {}},
    {"alias": "spending", "name": "finance.spending", "args": {"group_by": "month", "days": 120, "limit": 6}},
    {"alias": "categories", "name": "finance.categories", "args": {"days": 120, "limit": 5}},
    {"alias": "review_queue", "name": "finance.review_queue", "args": {"days": 45}}
  ],
  "actions": [
    {"key": "submit_review", "label": "提交确认", "fields": ["transaction_ids"]}
  ]
}
```

For a sensitive view, `fields: ["transaction_ids"]` strips the emitted event down to `form.transaction_ids`. `CheckboxGroup` stores an array of selected option values, so labels, descriptions, merchant names, card names, and UI-only fields never cross back to chat. A Button with `fields` is disabled with a short hint until every listed field has a value; a `CheckboxGroup` therefore needs at least one checked item.

## Component subset

- `Card(children)` — required full-width page root. It deliberately does not create a nested surface.
- `CardHeader(title, description?)`
- `Grid(children, columns?)` — responsive; two columns remain on a 375–390 px phone by default.
- `Stat(label, value, hint?, tone?)` — clear KPI hierarchy with tabular numbers. Tones: `success`, `warning`, `danger`, plus neutral tones; tinted states retain AA label/value contrast in light and dark themes.
- `List(items)` — mobile row cards. Item shape: `{title, subtitle?, trailing?, badge?, badgeTone?}`.
- `Text(text, size?)` — `small`, `default`, or `large`.
- `Markdown(text)`
- `Alert(title, description, variant?)` — `default` or `destructive`.
- `Badge(text, variantOrToneMap?)` — supports normal tones and a mapping such as `Badge(statusLabel, {\"已还清\": \"success\", \"未还\": \"warning\"})`.
- `Separator()`
- `Table(columns, rows)` — 4–5+ columns scroll horizontally inside their surface; the first column sticks and numeric columns right-align with tabular figures. Compact three-column tables remain full-width on a phone.
- `BarChart(labels, values, label?, format?)` — responsive bars with value labels and a y-axis; use `"money"` for compact dollar labels such as `$1.2k`.
- `Progress(value, label?)`
- `Accordion(items)` — item `{value, trigger, content}`.
- `Tabs(items, defaultValue?)` — item `{value, trigger, content}`; the tab list remains at the top while long tab content scrolls.
- `Form(name, fields)` — direct submit buttons expand to a usable full phone width.
- `TextInput(name, placeholder?, type?)`
- `Select(name, items, placeholder?)`
- `Checkbox(name, label)`
- `CheckboxGroup(name, items, checked?)` — multi-selects value IDs only; items may have a description line. It starts unchecked unless `checked` supplies selected IDs.
- `RadioGroup(name, items)`
- `DateInput(name, label?)`
- `Button(label, {type, params?, fields?}, variant?)`
- `ButtonGroup(buttons)`

## Theme, CSP, and assets

The vendored renderer is local: `openui.js` and `openui.css`. It loads no CDN assets and contains no `eval` or `new Function` calls. The host maps `Telegram.WebApp.themeParams` to CSS variables (`bg`, `secondary_bg`, `text`, `hint`, and button colors) from external app code; no inline script and no CSP relaxation are required.

The stylesheet follows Telegram variables when supplied and falls back to system light/dark colors. Its system stack includes `PingFang SC`, `Hiragino Sans GB`, `Microsoft YaHei`, and `Noto Sans CJK SC` for readable Chinese typography.

Drawers, dialogs/popovers, mutations, queries, URL actions, raw code, `@` directives, `$` bindings, arbitrary components, and URL opening are rejected before a view can be stored.
