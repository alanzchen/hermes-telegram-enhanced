# Widget contracts and charts

Use a built-in template plus JSON data, not model-authored chart markup. All implementation,
contracts, test harnesses and preview-generation code are in this repository. No framework,
chart-library, browser or Node dependency is required at plugin runtime.

## Charts

```json
{
  "action": "send",
  "template": "chart",
  "caption": "Illustrative energy data, not a live reading.",
  "data": {
    "type": "line",
    "title": "Example: daily energy",
    "unit": "kWh",
    "x": ["Mon", "Tue", "Wed", "Thu"],
    "series": [
      {"name": "Home", "values": [12, 18, null, 15]},
      {"name": "Office", "values": [8, 11, 9, 14]}
    ],
    "highlight": 1
  }
}
```

- `type`: `line` or grouped `bar`. No `hbar`/`spark` in this first implementation.
- `x`: 1–24 strings, at most 24 characters each. Values are categorical, equally spaced.
- `series`: 1–3 objects with `name` (1–24 characters) and `values` of **exactly** `len(x)`
  numbers or nulls. Nulls break a line; they are never silently treated as zero. A single isolated
  line point gets a dot. Bar charts include zero and support negative values.
- Optional `title` (80 characters), `unit` (16), `highlight` (zero-based x index).
- Optional `y_min`/`y_max` must enclose every value (and zero for bars), with min < max.
  The renderer does not depend on unsupported SVG clipping. Default axes use nice 1/2/2.5/5 ticks.
- Booleans and numeric strings are not numbers. NaN/infinity are rejected. Numbers have magnitude
  at most `1e100`; nonzero magnitudes below `1e-100` are rejected with a rescale-units error to keep
  both renderers numerically bounded. Duplicate category labels are allowed; unknown fields are not.
- Labels are escaped, theme-aware, and visually shortened for a 320px bubble. Up to five x labels
  are drawn; every value is plotted unless the explicit size fallback below reports sampling.
  Legend names/colors/dash patterns distinguish the series in light and dark themes.

### Initial compilation versus in-place updates

`charts.py` validates the spec, computes geometry, and emits compact SVG into `templates/chart.html`.
The first chart's numeric arrays are **not** put into `chat_data`: even a high-precision 24×3 chart
can be sent without hitting the 1024-byte state limit. A tiny, fixed JavaScript SVG renderer is
already in that HTML, with a shared `chatDataChanged` mount. No HTML replacement is attempted.

To update, send `action="update", message_id=..., data=<complete chart spec>`. This full replacement
spec **must fit 1024 UTF-8 bytes**, including server-managed expiry/choice and choice headroom.
Use fewer points/shorter labels/appropriately rounded source numbers when it does not fit; updates
are rejected, not silently resampled. A choice-only state change retains the initial SVG. After a
numeric update the stored spec is redrawn on subsequent state changes. Python and JS geometry,
text escaping and representative edge cases are cross-checked by the Node test harness.

Every initial chart is measured **after** CSS, template helpers, renderer, auto-buttons and tap
runtime have been injected: raw HTML ≤32768 bytes and zlib level 9 ≤4096 bytes. If needed, the
compiler first reduces visible x labels, then samples points (endpoints and highlighted index are
retained; no averaging, no new line across an omitted null, and the original y scale is retained).
The tool returns explicit `warnings` describing simplification. Include that limitation in the
caption rather than presenting a sampled chart as full-resolution. If even the reduced document
does not fit, the error recommends text/a text table. Supplied specs are never mutated.

## Template data contracts (version 1)

`action="templates"` returns the full schema and example for each template. `data` and `chat_data`
are aliases; supply only one. Required fields are marked **bold** below. String caps count Unicode
characters; the separate state cap counts UTF-8 bytes. All built-ins reject unknown properties,
invalid tuple lengths, wrong types, invalid action keys and non-finite numbers before API calls.

| Template | Fields and limits |
|---|---|
| `weather` | **city** string 1–64; **t**, **hi**, **lo**, **fl** numbers in ±1,000,000; **c** integer 0–99; **h** 0–8 tuples `[label (1–16), temperature, code (0–99)]`; **at** string ≤40. |
| `tracker` | **t** string 1–80; **s** 1–8 step strings (1–24 each); **i** integer index in `s`; optional `eta` ≤80, `n` ≤240, `at` ≤40. |
| `compare` | **t** 1–80; **o** 1–6 option objects, each with **k**, **n**, optional `v` ≤240, `p` ≤40, `d` 0–6 strings ≤100, `best` boolean/string ≤24, `b` ≤40; optional `note` ≤240. |
| `approve` | **t** 1–80; **d** 0–8 `[label ≤40, value ≤240]` string tuples; **o** 1–6 `{k,n,v?}` objects; optional `risk` ≤240, `confirm` boolean or 1–80 character text. |
| `chart` | See the chart spec above. |

Action `k` is 1–32 ASCII letters, digits, `_` or `-`; `n` is 1–40 characters; duplicate keys are
rejected. Omit `actions` when using `compare`/`approve`: their option list is the authoritative
binding. Explicit `actions` must match it exactly, avoiding a button label/action mismatch.

### State and security

- Every template redraws all its display fields on updates. Weather escapes **both** the hour label
  and numeric interpolation; compare redraws its cards; approve redraws title, all detail rows,
  risk, buttons, chosen status and expiry. All HTML text interpolation uses `HTE.esc`, which
  escapes `&<>"'`; plain text uses `textContent`.
- Rebuilt button nodes are rebound after rendering. Each node gets one listener; pending requests
  are keyed independently of DOM nodes. A redraw cancels armed first-tap consent, not the server's
  single-use status. Requests remain synchronous inside click handlers.
- Approve's first button still needs a second tap within four seconds unless the initial spec
  explicitly uses `confirm:false`. `confirm:true` uses the default confirmation text. Updating
  cannot change this policy. Expiry is set using `expires_in`; UI countdown/disabled states and
  server-side expiry checks remain in force. UI confirmation is **not** credential-level authorization.
- `chosen` and `exp` are reserved. The server retains the true choice and expiry across updates,
  ignoring caller attempts to reset them. Space for a future `chosen` key is checked before send/update.
- An update may change display labels/details but not action keys, order or semantic `v` values.
  Send a new card when changing what a tap authorizes. Owner/whitelist/once/expiry checks in `claim`
  remain the authority. Display labels in the stored action record are updated after API success.
- Contracts are snapshotted in each new widget record, including noninteractive widgets. Updates
  use the stored contract, not a potentially changed template file. Unknown/pruned records and
  cross-topic updates are rejected. Older recorded widgets retain their original untyped behavior;
  old read-only widgets that had no record must be resent once.
- Telegram fixes height on first render. Keep replacements within that height (or supply a suitable
  `height` on send); schema limits are not a guarantee that every maximum-size text payload fits visually.
  Always supply a useful plain-text caption for notifications and older clients.

## Custom templates

A template is an HTML file with optional `<!--hte {"version":1,"desc":"...","example":{...},
"schema":{...},"actions_from":"o"}-->` metadata, stripped before sending. Use
`telegram_widget(action="save_template", name=..., html=..., schema=..., example=...)` to persist one.
The validator supports `type` (including a type list), `anyOf`, `enum`, `properties`, `required`,
`additionalProperties:false`, `items`, `prefixItems`, `minItems`/`maxItems`, `minLength`/`maxLength`,
`pattern`, `minimum`/`maximum` — not the full JSON Schema standard. Legacy custom templates without
schemas remain permissive object contracts for compatibility; define a bounded schema for new ones.

Place `<!--hte-runtime-->` before custom template scripts to inject the same `window.HTE` helper.
`HTE.mount(function(state){...})` performs the initial render and subscribes to state changes;
`HTE.esc(value)` is safe for text and quoted attribute interpolation. Templates drawing their own
buttons mark their root `data-hte-actions`; `actions_from` derives registered actions from that field.
A `data-confirm="text"` attribute applies the two-tap UX to any registered action button.

## Sandbox and verification

Only inline CSS/JS/SVG: no network or third-party bundles. Use the small documented DOM subset
(`getElementById`, `querySelector`, `innerHTML`, `textContent`, `style`, `addEventListener`) and ES2023.
Charts emit only `svg`, `g`, `path`, `rect`, `circle`, `line`, `text`, and `viewBox`; inline linear
colors/gradients are possible, but not clipPath, masks, markers, SVG filters, SMIL or foreignObject.
Use `--tg-theme-text-color`, `hint-color`, `button-color`/`accent-text-color` with explicit fallbacks.

```bash
python3 -I -m unittest discover -s tests
python3 -I scripts/widget_previews.py --output /opt/data/cache/scratch/widget-previews
```

The second command writes self-contained light/dark HTML previews, extracted scripts, and
`measurements.json`, and runs `node --check` when Node is available. It uses fake Telegram state and
requests; it cannot send a Telegram message. Open each HTML at 320px and call `window.__update(spec)`
to exercise an update. CSS button styling in the preview only approximates Telegram's native buttons.
Browser captures are build artifacts, not runtime dependencies; keep PNGs outside the repository.

Measured full-document zlib-9 bytes for the reproducible preview fixtures (including tap runtime
where applicable): weather **1605**, tracker **1116**, compare **1488**, approve **1805**; example
line **2256**, bar **2362**; **24 points × 3 series + two buttons** line **3106**, bar **3466**. The
max-point fixtures needed no sampling. Re-run the script after changes; do not add separate compressed
component sizes. Tests cover schema failures, escaping, null gaps, zero baselines, state updates,
rebinding, confirmation/expiry, identity preservation, oversize failures and full-document budgets.

**Chrome previews and Node mock-DOM tests are not Telegram/QuickJS acceptance. No real Telegram
message is sent by these tests, and real-device rendering remains unverified.**
