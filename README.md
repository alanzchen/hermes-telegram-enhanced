# Hermes Telegram Enhanced

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds Telegram Bot API 10.x features to the Telegram gateway. It works without patching Hermes: everything goes through the public plugin API (`register_tool`, `register_hook`, `register_telegram_handler` and `inject_message`).

| Feature | What you get |
|---|---|
| **Rich messages and buttons** | `telegram_rich_send` sends Rich Markdown: tables, task lists, `<details>`, quotes, footnotes and math. It can attach button rows of three kinds. `choice` buttons send the tap back into the same conversation as a new user turn; `url` buttons open a link; `copy` buttons copy text. Each button can be styled `primary`, `success`, `danger` or `link`. |
| **Status cards** | `telegram_status_card` keeps one pinned message per key in a chat or topic and edits it in place. The latest status stays at the top, and no new progress messages pile up. |
| **Local-time entities** | Write `{{time:2026-11-08T09:30\|America/Chicago}}` and the time renders in each reader's own timezone and locale. Supported formats are `wDT` (default), `t`, `D` and `r` (relative). Ordinary non-rich replies get a readable text fallback. |
| **Maps and galleries** | Messages can include `<tg-map>` blocks and photo or video collages and slideshows. |
| **Stop button** | While a turn runs in a private chat, a "Thinking…" draft with Telegram's native **Stop** button is shown. Pressing Stop issues `/stop` for that conversation. |
| **HTML widgets** | `telegram_widget` sends a Bot API 10.4 widget: a small sandboxed HTML view drawn inside the message bubble (private chats only for now). Elements marked `data-action="key"` (or an auto-added button row) send taps back into the conversation like choice buttons; the first tap locks the buttons and marks the chosen one. The agent can update the widget's state in place. |
| **Secure input** | `telegram_secret` asks for a password or code without it entering the conversation: the user's next text message in that private chat/topic is taken before the gateway logs or stores it, deleted from the chat, and saved into 1Password (the agent gets an `op://` reference) or kept in memory for 15 minutes (used via `exec` with `$HERMES_SECRET`, output redacted). |
| **Guest Mode** | `@bot` in any chat, including chats the bot is not a member of, answers from allowed users. The bot answers in place with a "处理中…" placeholder. The request is routed to a dedicated DM topic, and the agent fills in the answer with `telegram_guest_reply`. |

## Install

```bash
git clone https://github.com/alanzchen/hermes-telegram-enhanced "$HERMES_HOME/plugins/hermes-telegram-enhanced"
hermes plugins enable hermes-telegram-enhanced
hermes config set plugins.entries.hermes-telegram-enhanced.allow_gateway_injection true
# keep your existing Telegram toolsets and add this one:
hermes config set platform_toolsets.telegram '["hermes-telegram","telegram_enhanced"]'
hermes gateway restart
```

`allow_gateway_injection` lets button presses and Guest Mode requests start a turn. For Guest Mode, also enable **Guest Mode** for the bot in BotFather's Mini App.

### Settings (`plugins.entries.hermes-telegram-enhanced.settings`)

| Key | Default | Meaning |
|---|---|---|
| `display_tz` | `America/Chicago` | Timezone used for the text fallback of time entities |
| `stop_button` | `true` | Show the Thinking/Stop draft during turns in private chats |
| `guest_mode` | `true` | Handle Guest Mode @mentions |
| `guest_allowed_users` | `TELEGRAM_ALLOWED_USERS` | User IDs whose Guest Mode requests are answered; other users are ignored |
| `merge_document_bursts` | `true` | Files sent together (one album, or back to back) reach the agent as one message instead of one turn per file |
| `document_burst_seconds` | `1.5` | How long to wait after the last file of a burst before handing it to the agent |
| `snooze_buttons` | `true` | Long replies in private chats get "remind me later" buttons |
| `snooze_min_chars` | `600` | Minimum reply length for the snooze buttons |

## How it works

- **Buttons**: a `CallbackQueryHandler(pattern="^rb:")` handles button presses. It only sees this plugin's buttons, so core buttons (clarify, approvals, model picker) are unaffected. A choice prompt can be answered only by the user who started the conversation, accepts the first press only, and expires after 7 days. When pressed, the message is re-rendered: choice buttons are removed and a "✅ 已选择" line is added.
- **Stop button**: the `pre_llm_call` hook starts a `sendRichMessageDraft(can_stop=true)` that refreshes every 20 seconds. `post_llm_call`, `on_session_end` and `agent_loop_stopped` end it. The `stopped_message_generation` update dispatches `/stop` through the adapter's normal message path, so normal authorization applies.
- **New update types**: Hermes polls with `allowed_updates=Update.ALL_TYPES`. The plugin appends `stopped_message_generation` and `guest_message` to that list (and `widget_query`) to that list at load time, because the bundled python-telegram-bot predates them. The list is extended in place, so a running poller picks up the new types on its next `getUpdates`; if a type still does not arrive, restart the gateway.
- **Guest Mode**: the plugin answers the guest query once with a placeholder and edits that inline message when the agent replies. It never posts anything else into the foreign chat. The agent is told that the answer is public in that chat.
- **State**: kept under `$HERMES_HOME/telegram-enhanced/`, with directory mode `0700` and file mode `0600`.
- **Remind me later**: long replies in private chats end with a row of buttons (in 1 hour / tonight / tomorrow morning). A press schedules a reminder; at that time the bot replies to the original message so it is one tap away. Reminders are kept in `reminders.json` and sent by a small scheduler thread, without any model call.
- **Decisions**: `telegram_decisions` asks for each decision in its own message. It quotes the message it is about, explains the choice and carries the action buttons the agent picked; it stays pinned until a button is pressed (or the agent closes it after Alan answered in chat). `telegram_rich_send(decision=..., reply_to=...)` does the same for any choice message.
- **Wrap-up cards**: `telegram_wrapup` ends a long task with one card (what changed / what was verified / what is still pending) and next-step buttons.
- **Widgets**: `telegram_widget` checks the final injected document (32768 raw UTF-8 bytes, 4096 bytes after zlib level 9) and wires registered `data-action` elements to `TelegramWidget.request("a:<key>")`. Rebuilt buttons are rebound on each state change, with pending-request guards. The server still checks owner, action whitelist, single-use state and expiry; UI confirmation is not an execution authorization gate. State is limited to 1024 UTF-8 bytes, including reserved `chosen`/`exp` fields. `action=update` replaces display state while preserving server-managed choice/expiry.
- **Widget templates**: use `telegram_widget(template="tracker", data={...})` rather than writing markup. `data` and `chat_data` are aliases (do not supply both). Built-ins are `weather`, `tracker`, `compare`, `approve`, and `chart`; all declare versioned data schemas, validate before send/update, and redraw on `chatDataChanged`. `action=templates` returns schemas and examples. Templates and their contracts are saved with each message, even when there are no buttons. `action=save_template` can store a schema with custom HTML in `$HERMES_HOME/telegram-enhanced/widget-templates/`; custom names override built-ins. See [widget contracts, charts and previews](docs/widgets.md) for exact limits, update semantics and the supported schema subset.
- **Secure input**: `telegram_secret(action="request")` saves the request (no value) in `secrets.json`, then posts a card with a cancel button. The `pre_gateway_dispatch` hook matches the next text message from the same user in the same chat/topic (slash commands, voice and media pass through; `取消`/`cancel` cancels) and returns `skip`, which happens before Hermes logs the message, commits it or builds a turn. A thread deletes the message (`deleteMessage`, allowed for incoming messages in private chats), writes a Login/Password item into 1Password with `op item create -` (JSON on stdin, never argv; setting `secret_vault`, default `Hermes`; needs `OP_SERVICE_ACCOUNT_TOKEN` in the environment or the profile `.env`) or keeps it in memory, edits the card and injects a turn with the reference. If anything fails after a request matched, the message is dropped rather than passed on. Pending requests expire (default 10 minutes) and the card says so. The value still travels through Telegram's servers (cloud chat, not end-to-end) and can show briefly in notifications.
- **File bursts**: Hermes merges photo albums but hands every other file to the agent as its own message. With `busy_input_mode: interrupt`, each file of a multi-file send then interrupts the turn of the one before it, and files still waiting for the session are dropped. The plugin buffers the documents of one chat/topic that arrive together and passes them on as a single message.

## Notes

- Telegram Desktop and macOS clients had a CJK glyph-overlap bug with rich messages (hermes-agent #47653). Check how your client renders before enabling rich messages for CJK text.
- Rich media must be direct, publicly reachable URLs. Telegram fetches them itself and fails with `RICH_MESSAGE_*_NO_MEDIA_FOUND` when it cannot.
- Drafts (and therefore the Stop button) exist only in private chats.

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT

## JSON-driven Telegram Mini App

The self-contained `miniapp/` package provides a versioned JSON component catalog and a
vanilla JS/SVG renderer for decisions, forms, tables, charts, Gantt schedules, flowcharts,
timelines and nested sections/tabs. It accepts data, never model-authored markup or code.
Actions return through the existing conversation injection callback; they never execute
operations themselves. Authenticated API requests verify Telegram initData HMAC, the issuing
user, view binding, expiry, revision and the server-side action/value whitelist.

The package exposes `miniapp.setup(...)` → `tool_schema`, `tool_handler`,
`check_requirements()`, async `start()` / `stop()`, and `create_app()`. Integration details,
callback payloads and the exact coordinator wiring are in `miniapp/INTEGRATION.md`; the
model-facing schema is in `miniapp/CATALOG.md`, with a complete synthetic `miniapp/demo.json`.

Enable only after wiring gateway lifecycle and an HTTPS reverse proxy. Each gateway profile
must explicitly set its own `HTE_MINIAPP_PORT` and `HTE_MINIAPP_PUBLIC_URL`; otherwise the
server stays disabled. Optional `HTE_MINIAPP_DIRECT_LINK` supports a BotFather app link.
Serve `/m`, `/m/static/*`, `/api/spec/*` and `/api/action/*` unchanged through the proxy.
This package's local tests and Chromium previews do not certify real Telegram-client or
production gateway operation.

```bash
python -m unittest discover -s tests -p 'test_miniapp*.py' -v
# Opt-in headless browser acceptance and PNGs (synthetic data, no Telegram sends):
uv run --no-project --with playwright --with aiohttp python tests/test_miniapp_preview.py
```
