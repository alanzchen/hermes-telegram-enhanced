# Coordinator integration contract

This package is self-contained. It does **not** edit/register anything in the plugin root,
start a daemon, send a message on import, or patch Hermes. The coordinator owns registration,
gateway-loop lifecycle, unload/reload serialization and deployment.

## Exact API

```python
from . import miniapp

ui = miniapp.setup(
    ctx,
    bot_token_getter=_token,            # () -> current profile bot token; never exposed to client
    deliver_action=lambda p: ctx.inject_message(p["text"], session_key=p["session_key"]),
    state_dir=_profile_home() / "telegram-enhanced",
    current_target=_current_target,    # () -> (target_dict, error_or_None), or target_dict
    send_message=lambda payload: _api("sendMessage", payload),
)
ctx.register_tool(
    name="telegram_ui",
    toolset="telegram_enhanced",
    schema=ui.tool_schema,             # also miniapp.TOOL_SCHEMA
    handler=ui.tool_handler,           # sync (args, **kwargs) -> JSON string
    check_fn=ui.check_requirements,    # configured + listener active + bot token present
    description="JSON-driven Telegram Mini App views and conversation actions",
    emoji="🧩",
)
# On the gateway's event loop, after token availability:
await ui.start()                      # bool; False when disabled/already started; bind errors propagate
# On plugin unload/gateway shutdown, on the SAME loop:
await ui.stop()                       # stops listener/sweeper; idempotent
```

Do not call `asyncio.run(ui.start())` from a worker: its temporary loop immediately dies.
If the plugin's lifecycle hook is synchronous, schedule with the existing captured gateway
loop, and observe the future's exceptions. Complete old `stop()` before a new generation
starts on the same port. `create_app()` is also available for testing or embedding in an
existing aiohttp runner; its startup/cleanup updates listener readiness. One instance/runner
per profile, and one writer process per store path. State files are 0600; newly created
state directories are 0700. Do not share one state directory between profiles.

### Tool arguments

- `catalog` → exact field catalog and full synthetic demo, no current-chat requirement.
- `send` + `title` + `spec`, optional `expires_in` 60–86400 (default 3600) → stores an
  owner/route-bound view and posts `sendMessage` with a web_app launch button to CURRENT
  chat/thread. Returns success, id, message_id, url, direct_link, expires_at.
- `update` + `id` + full `spec` → increments revision; browsers poll every five seconds,
  only while visible. No patch language: full replacement is explicit and revalidated.
- `close` + `id` → disables further submissions. Browser sees closed state on next poll.

All mutations compare stored chat_id, thread_id, user_id **and session_key** with the current
conversation; arbitrary cross-chat ids cannot update/close another view. Messages are plain
text (title only), so model markup does not become a Bot API entity or launch URL.

### `deliver_action(payload)`

Called only after HMAC/user/spec/TTL/revision/action/value validation and a durable pending claim.
It may be sync (offloaded from the HTTP event loop) or async. Return **True** only when the user
event was accepted, **False** only when no event was accepted. Exceptions, timeout (12 seconds),
`None` or any other result are uncertain, never implicitly retried. Integrate with exactly the
same `ctx.inject_message(text, session_key=...)` path as widget taps; keep
`allow_gateway_injection=true`. Do not execute arbitrary commands/actions from the payload.

Payload:

```python
{
    "id": "<view id>", "title": "<question>", "chat_id": "...", "thread_id": "...",
    "user_id": "...", "session_key": "...", "message_id": 123, "revision": 1,
    "event_id": "<stable id for this accepted/retried submission>",
    "request_id": "<client idempotency key>",
    "text": "[Telegram Mini App] ...\nQuestion: ...\n{key,label,value,...}\n...",
    "action": {
        "key": "...", "label": "<SERVER label>", "value": "<validated scalar/list/object>",
        "kind": "buttons|choice|form", "component_id": "...", "once": True,
        # choice: selection_labels, meaning; form: field_labels, meaning; buttons: group_once
    },
}
```

The callback is the only handoff. The package never edits the launch message after an action
(optional outcome edits were deliberately left to the coordinator). Browser/server persistence
shows recorded/pending/uncertain state. Deduplication protects duplicate/concurrent requests and
one-time groups; exact-once acceptance across a process crash cannot be guaranteed without a
transactional downstream inbox. A crash after claim or an ambiguous callback leaves it locked
pending/uncertain rather than silently sending twice. Check the conversation, then close that
view and issue a new one if needed. Updates never silently clear successful claims.

## Configuration / reverse proxy

| Variable | Meaning |
| --- | --- |
| `HTE_MINIAPP_PUBLIC_URL` | Public HTTPS origin only, e.g. `https://hte-ui.cool.zenan.ch`; no path/query/fragment |
| `HTE_MINIAPP_PORT` | Explicit unique TCP port. Example 8787; **required to enable**. No auto-binding every profile to 8787. |
| `HTE_MINIAPP_HOST` | Bind host, default `127.0.0.1`. Set `0.0.0.0` only if container routing requires it; firewall to proxy. |
| `HTE_MINIAPP_DIRECT_LINK` | Optional BotFather app URL `https://t.me/BotName/AppName` or configured main app `https://t.me/BotName`. Adds `startapp=<id>`. |

Leave PUBLIC_URL or PORT unset to disable. Example profile allocation: default 8787, ea 8788,
health 8789, research 8790, each with a **different public hostname**, corresponding bot token,
profile state path and BotFather URL. Never point every bot at one profile's service. These are
examples only; this implementation changes no profile environment or gateway configuration.

Proxy the whole dedicated HTTPS origin to the chosen process/container port, preserving paths:

- GET `/m` — static shell; no spec or credentials embedded.
- GET `/m/static/{app.js,renderer.js,layout.js,style.css}`.
- GET `/api/spec/<id>?id=<id>` — requires `Authorization: tma <Telegram.WebApp.initData>`.
- POST `/api/action/<id>?id=<id>` — same auth, JSON body `{key,value,revision,request_id}`.

Preserve Authorization and query strings. Disable caching and access-log capture of request
headers/bodies (initData is a short-lived credential). No CORS, cookies, external JS CDN, eval,
or inline script/style is required. API body cap 16 KiB; spec cap 64 KiB; storage cap 20 MiB,
256 view records, 128 submission receipts per view. Expired views are pruned on create and by
a 60-second sweep while `start()` is running. Closed views remain until TTL.

TLS terminates at the coordinator's reverse proxy; do not expose the plain HTTP port publicly.
Configure BotFather's Mini App URL to `<PUBLIC_URL>/m`; inline private-chat web_app buttons add
`?id=...`. Direct-link mode uses signed `start_param` and Telegram's GET `tgWebAppStartParam`.
A signed start_param mismatch is rejected even if the query says something else. Inline mode
normally has no signed start_param, so query id + unguessable stored id + signed owner identity
bind the view. The query alone is never authentication. HMAC auth is fresh for one hour with
30-second future skew, regardless of the view TTL; reopen the launch button for fresh initData.

The CSP allows self + telegram.org scripts; HTTPS/data images; self-only connections; no objects,
base changes or form navigation. frame-ancestors allow Telegram web origins, t.me and self;
actual Web/Desktop/macOS embedding is **not verified**. Test those real clients after deployment
and narrow/update the ancestor list only from actual observed Telegram origins.

## Verified local acceptance and reproduction

```bash
python -m unittest discover -s tests -p 'test_miniapp*.py' -v
node miniapp/testing/renderer_test.cjs
# Optional preview-only deps; not runtime dependencies, uses a separate uv environment:
uv run --no-project --with playwright --with aiohttp python tests/test_miniapp_preview.py
```

The preview harness launches its own headless Chromium (never the user's browser), starts the
real aiohttp routes locally, signs synthetic initData with a synthetic bot token, and stubs only
Telegram.WebApp/the telegram.org SDK. It sends **no real Telegram messages**. Fixtures/previews
are marked synthetic. It exercises choice/form HTTP callbacks, state after reload, polling
updates/draft preservation, close, XSS text rendering, repeatable controls and real 15-second
fetch timeout; screenshots at 390 light/dark plus 320/1100 catalog widths. Runtime is aiohttp
plus Python stdlib; front end is standalone vanilla JS/CSS/SVG, no build step.

No real Telegram Mini App, bot launch button, public HTTPS reverse proxy, gateway reload,
macOS WebView or actual conversation injection was verified by these local tests.
