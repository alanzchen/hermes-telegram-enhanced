# Mini App JSON catalog v1

The model supplies data, never HTML/CSS/JS. Root: `{"version":1,"components":[...]}`.
All fields not listed here are rejected. Spec max 65,536 UTF-8 bytes; 200 total components,
100 per list, nesting depth 4. Text max 4,000 characters unless noted. No data is invented by
the renderer. Image URLs load from third parties; omit images for private views.

## Component shapes

`?` means optional (do not include the `?` in JSON). All have `type`.

- **heading**: `text`, `level?` 1/2/3 (default 2).
- **text**: `text`, `markdown?` boolean. Minimal inline `**bold**`, `*italic*`, and backtick code only. HTML, links and everything else stay literal text. Newlines preserved.
- **callout**: `text`, `title?`, `tone?` info/success/warning/danger.
- **kv**: `items:[{key,value}]`, both strings (key 200, value 2,000 characters).
- **table**: `columns:[string]` (1–20), `rows:[[string|number|boolean|null]]` (max 200, same width as columns), `sortable?` boolean.
- **list**: `items:[string]`, `ordered?` boolean.
- **checklist**: `items:[{text,checked?:boolean}]`. Display-only; use choice multiple for editable selections.
- **choice**: `id`, `label`, `options:[{value,label,details?}]`, `multiple?` boolean, `max_selected?`, `action`. 1–40 options, values are keys, unique. details max 2,000; labels max 200. At least one choice required. Single submits a string; multiple submits a unique list of option values.
- **form**: `id`, `title?`, `fields:[{key,type,label,required?,default?,placeholder?,max_length?,min?,max?,options?}]`, `action`. 1–20 fields, types text/number/date/select/toggle. `max_length` (1–4,000, default 1,000) only for text. min/max only for numbers (finite, ±1e12). select requires same options shape as choice. Date is a real YYYY-MM-DD date. Submit values is an object keyed by field key; unknown fields rejected. Optional fields may be omitted or null. Required toggle requires a boolean value, not necessarily true. Never ask for secrets here: form values are persisted and sent to the model.
- **buttons**: `id`, `actions:[action]` (1–12), `once?` boolean (default true: first action locks this entire button group). Set group `once:false` and each action `once:false` for repeatable buttons.
- **chart**: `kind` line/bar/hbar/pie-lite, `title?`, `labels:[string]` (1–100), `series:[{name,values:[number]}]` (1–6), `unit?`, `source?`. Each series matches labels length. Values finite ±1e12. Pie-lite requires exactly one nonnegative, nonzero-total series. Charts include an accessible expandable data table. The shared bubble-compatible core shape is labels+series/values; this renderer keeps its own tiny SVG implementation.
- **gantt**: `title?`, `today?` YYYY-MM-DD, `tasks:[{id,label,start,end,group?,milestone?,depends_on?:[id],progress?:number}]` (1–60). Dates YYYY-MM-DD, end ≥ start; progress 0–1. Dependencies must name existing different tasks. Consecutive groups are labeled. One row per task, deterministic coordinates, sideways scrolling. Today defaults to UTC date.
- **flow**: `title?`, `nodes:[{id,label,shape?:rect|diamond}]` (1–60), `edges:[{from,to,label?}]` (max 120). Unique node ids; every edge endpoint must exist. Deterministic layered layout; cycle back-edges drawn dashed around the right side, without recursive layout. Long labels wrap/truncate with a complete SVG title. Not a general graph editor or Mermaid interpreter.
- **timeline**: `items:[{title,date?,text?}]`. All strings; date is a display label.
- **image**: `url` absolute HTTPS only (no credentials, max 2,048), `alt?`, `caption?`.
- **divider**: no other fields.
- **tabs**: `tabs:[{label,components:[component]}]` (1–8); keyboard left/right supported.
- **section**: `title`, `components:[component]`.

## Action shape and identity

`{"key":"save","label":"Send preferences","value":"optional server-defined meaning","once":true}`

- Keys/ids: 1–48 ASCII letters, digits, `_`, `-`; `__proto__`, `prototype`, `constructor` forbidden.
- Action keys unique across the whole view (max 64); interactive component ids unique too.
- Action label max 120 characters; value max 2,000. Client cannot override label/meaning.
- `once` defaults true per action. Button groups also default once true.
- Buttons submit null; server substitutes action.value. Choice/form submit declared values only.
- Posting does not run a tool or command. It only injects a synthetic user message in the original conversation.
- Server rejects old revisions, wrong user, expired/closed views, undeclared keys, incorrect types,
  overlong values, concurrent or consumed one-time actions. Durable request ids deduplicate retries.
- `update` requires a FULL replacement spec (no ambiguous patch language). Completed one-time keys remain
  consumed across revisions; send a new view for a new decision. Reuse stable component ids and action keys.

## Small example

```json
{"version":1,"components":[
  {"type":"choice","id":"release","label":"What next?","options":[
    {"value":"pilot","label":"Pilot first","details":"10 participants for one week."},
    {"value":"wait","label":"Wait","details":"Finish testing before release."}
  ],"action":{"key":"release_choice","label":"Send choice"}}
]}
```

`demo.json` is a full synthetic decision + 6-task Gantt + 7-node flow + bar chart + form example.
