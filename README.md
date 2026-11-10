# Hermes Telegram Enhanced

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds Telegram Bot API 10.x features to the Telegram gateway. It works without patching Hermes: everything goes through the public plugin API (`register_tool`, `register_hook`, `register_telegram_handler` and `inject_message`).

| Feature | What you get |
|---|---|
| **Rich messages and buttons** | `telegram_rich_send` sends Rich Markdown: tables, task lists, `<details>`, quotes, footnotes and math. It can attach button rows of three kinds. `choice` buttons send the tap back into the same conversation as a new user turn; `url` buttons open a link; `copy` buttons copy text. Each button can be styled `primary`, `success`, `danger` or `link`. |
| **Status cards** | `telegram_status_card` keeps one pinned message per key in a chat or topic and edits it in place. The latest status stays at the top, and no new progress messages pile up. |
| **Local-time entities** | Write `{{time:2026-11-08T09:30\|America/Chicago}}` and the time renders in each reader's own timezone and locale. Supported formats are `wDT` (default), `t`, `D` and `r` (relative). Ordinary non-rich replies get a readable text fallback. |
| **Maps and galleries** | Messages can include `<tg-map>` blocks and photo or video collages and slideshows. |
| **Stop button** | While a turn runs in a private chat, a "Thinking…" draft with Telegram's native **Stop** button is shown. Pressing Stop issues `/stop` for that conversation. |
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
- **New update types**: Hermes polls with `allowed_updates=Update.ALL_TYPES`. The plugin appends `stopped_message_generation` and `guest_message` to that list at load time, because the bundled python-telegram-bot predates them. This takes effect on the next gateway (re)start.
- **Guest Mode**: the plugin answers the guest query once with a placeholder and edits that inline message when the agent replies. It never posts anything else into the foreign chat. The agent is told that the answer is public in that chat.
- **State**: kept under `$HERMES_HOME/telegram-enhanced/`, with directory mode `0700` and file mode `0600`.
- **Remind me later**: long replies in private chats end with a row of buttons (in 1 hour / tonight / tomorrow morning). A press schedules a reminder; at that time the bot replies to the original message so it is one tap away. Reminders are kept in `reminders.json` and sent by a small scheduler thread, without any model call.
- **Waiting on you**: `telegram_decisions` keeps one pinned card per chat/topic listing what waits on the user, each with a ✓ button that crosses it off. Choice buttons sent with a `decision` field are listed there until pressed.
- **Wrap-up cards**: `telegram_wrapup` ends a long task with one card (what changed / what was verified / what is still pending) and next-step buttons.
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
