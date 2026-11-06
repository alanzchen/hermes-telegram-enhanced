# hermes-telegram-rich-buttons

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets the agent send Telegram rich messages with buttons. It uses the [Bot API 10.1 rich messages](https://core.telegram.org/bots/api#rich-messages) and needs no changes to Hermes source code.

- **`telegram_rich_send` tool**: sends Rich Markdown, such as tables, task lists, `<details>`, quotes and footnotes, to the current Telegram chat or topic. The message can include button rows.
- **Button types**
  - `choice`: when tapped, the plugin sends the choice back to the same Hermes conversation as a new user message, like a clarify button.
  - `url`: opens a link.
  - `copy`: copies text, such as a command or an ID, to the clipboard.
- **Styles**: `primary`, `success`, `danger` and `link`.
- **Safety**
  - Only the user who started the conversation can press a choice button.
  - By default, a choice prompt accepts only the first press.
  - Pending prompts expire after 7 days.
  - State is stored with mode `0600` under `$HERMES_HOME/telegram-rich-buttons/`.
- **After a press**: the plugin edits the message to remove the choice buttons and adds a "✅ 已选择" line. Link and copy buttons stay.

## How it works without patching Hermes

| Need | Plugin API used |
|---|---|
| Send the rich message | `ctx.register_tool`, which calls `sendRichMessage` directly |
| Receive button presses | `ctx.register_telegram_handler`, which adds a `CallbackQueryHandler(pattern="^rb:")` scoped to this plugin's prefix so core buttons (clarify, approvals, model picker) keep working |
| Return the press to the conversation | `ctx.inject_message(..., session_key=...)` |

## Install

```bash
git clone https://github.com/alanzchen/hermes-telegram-rich-buttons "$HERMES_HOME/plugins/telegram-rich-buttons"
hermes plugins enable telegram-rich-buttons
hermes config set plugins.entries.telegram-rich-buttons.allow_gateway_injection true
# add the toolset to Telegram, keeping your existing toolsets:
hermes config set platform_toolsets.telegram '["hermes-telegram","telegram_rich"]'
hermes gateway restart
```

`allow_gateway_injection` lets choice presses start a turn. Without it, link and copy buttons still work, but choice presses are not routed back to the agent.

## Tool example

```json
{
  "markdown": "## Lunch?\n\n| Option | Distance |\n|---|---:|\n| Ramen | 0.4 mi |\n| Tacos | 1.2 mi |",
  "question": "Where to eat",
  "buttons": [
    [{"label": "🍜 Ramen", "value": "ramen", "style": "primary"},
     {"label": "🌮 Tacos", "value": "tacos"}],
    [{"label": "Map", "type": "url", "url": "https://maps.google.com"},
     {"label": "Copy address", "type": "copy", "copy_text": "123 Main St"}]
  ]
}
```

A press arrives in the conversation as:

```
[Telegram 按钮] Alan 点击了「🍜 Ramen」。
value: ramen
问题: Where to eat
request_id: 46ad75819b
```

## Notes

- Telegram Desktop and macOS clients had a CJK glyph-overlap bug with rich messages (hermes-agent #47653). Check how your client renders before relying on rich messages for CJK text.
- The tool only sends to the current Telegram conversation. It never sends to an arbitrary chat.
- Bot API limits apply: 32,768 characters, 500 blocks and 64-byte `callback_data`.

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT
