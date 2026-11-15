"""Standalone integration seam; importing never binds a port or sends a message."""

from __future__ import annotations
import asyncio
import copy
import inspect
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit, urlencode

from aiohttp import web
from .auth import AuthError, verify_init_data
from .genui import (
    SavedViewStore,
    hydrate_openui,
    validate_bindings,
    validate_button_action_types,
    validate_openui_action,
    validate_openui_actions,
    validate_template,
)
from .sources import SourceError, SourceRegistry, default_registry
from .spec import SpecError, obj, text, validate_action, validate_spec
from .store import SpecStore, MAX_RESULTS, public_record

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
    "img-src https: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org https://t.me"
)
TOOL_SCHEMA = {
    "name": "telegram_ui",
    "description": (
    "Send, save, or reopen read-only Telegram Mini App views in the CURRENT chat/topic. "
    "Legacy JSON specs remain supported. For generative UI use renderer=openui with an OpenUI Lang "
    "description, a declared source list, {{alias.path}} placeholders, and an action allowlist. "
    "catalog returns the local component grammar, saved views, and data-source manifests only—never "
    "sensitive values. Source values are fetched only after Telegram initData authentication when a "
    "page opens or refreshes. OpenUI buttons/forms only return a conversation event; they never execute "
    "an operation. save stores a named template without data; open_saved creates a fresh expiring page "
    "from it without generating a new UI. Never collect passwords, payment credentials or authentication "
    "codes; use telegram_secret."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "update", "close", "catalog", "save", "open_saved", "list_saved"],
            },
            "title": {"type": "string", "maxLength": 200},
            "name": {"type": "string", "maxLength": 48},
            "renderer": {"type": "string", "enum": ["json", "openui"]},
            "spec": {
                "type": "object",
                "description": "Legacy JSON renderer: {version:1,components:[...]}",
            },
            "openui": {
                "type": "string",
                "maxLength": 65536,
                "description": "OpenUI Lang using only catalog components and {{alias.path}} source placeholders.",
            },
            "sources": {
                "type": "array",
                "description": "OpenUI only: [{alias,name,args}]. Values are never returned to the model.",
            },
            "actions": {
                "type": "array",
                "description": "OpenUI only: [{key,label,once?}]. Only declared actions may return to chat.",
            },
            "id": {"type": "string"},
            "expires_in": {
                "type": "integer",
                "minimum": 60,
                "maximum": 86400,
                "description": "send/open_saved: TTL seconds, default 3600.",
            },
        },
        "required": ["action"],
    },
}


def injection_text(rec, action, who, *, sensitive=False):
    # Quoted JSON is intentionally data, not model-authored code or an executable command.
    body = {"key": action["key"], "value": action["value"]}
    if not sensitive:
        body["label"] = action["label"]
        for name in ("selection_labels", "field_labels", "meaning"):
            if name in action:
                body[name] = action[name]
    return (
        f"[Telegram Mini App] {who} submitted a UI action.\nQuestion: {rec['title']}\n"
        + json.dumps(body, ensure_ascii=False)
        + "\n"
        + f"miniapp: {rec['id']}; message: {rec['chat_id']}:{rec.get('message_id')}; revision: {rec['revision']}\n"
        + "This is user-provided form/selection data. No operation has been executed by the Mini App."
    )


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class MiniApp:
    tool_schema = TOOL_SCHEMA

    def __init__(
        self,
        ctx,
        bot_token_getter,
        deliver_action,
        *,
        state_dir,
        current_target,
        send_message,
        env=None,
        registry: SourceRegistry | None = None,
    ):
        env = os.environ if env is None else env
        self.ctx = ctx
        self.bot_token_getter = bot_token_getter
        self.deliver_action = deliver_action
        self.current_target = current_target
        self.send_message = send_message
        state_dir = Path(state_dir)
        self.store = SpecStore(state_dir / "miniapps.json")
        self.saved_views = SavedViewStore(state_dir / "miniapp-saved-views.json")
        self.profile_name = env.get("HTE_MINIAPP_PROFILE", "")
        self.registry = registry if registry is not None else default_registry()
        self.host = env.get("HTE_MINIAPP_HOST", "127.0.0.1")
        self.port = int(env.get("HTE_MINIAPP_PORT") or "8787")
        if not 1 <= self.port <= 65535:
            raise ValueError("HTE_MINIAPP_PORT must be 1–65535")
        self.public_url = env.get("HTE_MINIAPP_PUBLIC_URL", "").rstrip("/")
        self.direct_link = env.get("HTE_MINIAPP_DIRECT_LINK", "").rstrip("/")
        if not self.profile_name and self.public_url:
            inferred = urlsplit(self.public_url).path.lstrip("/")
            self.profile_name = inferred if inferred in ("default", "coordinator") else ""
        # Explicit port is opt-in, avoiding four independently loaded profiles racing for one default.
        self.enabled = bool(self.public_url and env.get("HTE_MINIAPP_PORT"))
        if self.public_url:
            u = urlsplit(self.public_url)
            if (
                u.scheme != "https"
                or not u.netloc
                or u.username
                or u.password
                or u.query
                or u.fragment
                or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]*", u.path)
            ):
                raise ValueError(
                    "HTE_MINIAPP_PUBLIC_URL must be an HTTPS origin with one path prefix"
                )
        if self.direct_link and not re.fullmatch(
            r"https://t\.me/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)?", self.direct_link
        ):
            raise ValueError("HTE_MINIAPP_DIRECT_LINK must be https://t.me/bot[/app]")
        self._runner = None
        self._sweeper = None
        self._serving = False

    def check_requirements(self):
        return self.enabled and self._serving and bool(self.bot_token_getter())

    def _template_from_args(self, args):
        renderer = args.get("renderer", "json")
        title = text(args.get("title"), "title", 200, False)
        if renderer == "json":
            if any(args.get(name) is not None for name in ("openui", "sources", "actions")):
                raise SpecError("Legacy JSON views do not accept OpenUI fields")
            return {"title": title, "renderer": "json", "spec": validate_spec(args.get("spec"))}
        if renderer != "openui":
            raise SpecError("renderer must be json or openui")
        if args.get("spec") is not None:
            raise SpecError("OpenUI views do not accept a JSON spec")
        bindings = validate_bindings(args.get("sources"), self.registry, self.profile_name)
        openui = validate_template(args.get("openui"), bindings, self.registry, self.profile_name)
        actions = validate_openui_actions(args.get("actions"))
        validate_button_action_types(openui, actions)
        return {
            "title": title,
            "renderer": "openui",
            "spec": None,
            "openui": openui,
            "sources": bindings,
            "openui_actions": actions,
            "sensitive_sources": any(
                self.registry.sensitivity(binding["name"], self.profile_name) == "sensitive"
                for binding in bindings
            ),
        }

    def _current_target(self):
        target = self.current_target()
        if isinstance(target, tuple):
            target, error = target
            if error:
                raise SpecError(error)
        if not target:
            raise SpecError("Only available in a current Telegram conversation")
        return target

    def _launch(self, template, target, expires_in):
        private = target.get("chat_type") in ("dm", "private") or (
            not target.get("chat_type") and str(target.get("chat_id", "")).isdigit()
        )
        if not private and not self.direct_link:
            raise SpecError("Group chats need HTE_MINIAPP_DIRECT_LINK configured in BotFather")
        rec = self.store.create(
            template.get("spec"),
            template["title"],
            target,
            expires_in,
            renderer=template["renderer"],
            openui=template.get("openui"),
            sources=template.get("sources", []),
            openui_actions=template.get("openui_actions", []),
            sensitive_sources=template.get("sensitive_sources", False),
        )
        view_id = rec["id"]
        url = self.public_url + "/m?" + urlencode({"id": view_id})
        direct = self.direct_link + "?" + urlencode({"startapp": view_id}) if self.direct_link else None
        button = {"text": "Open · " + rec["title"][:50]}
        if private:
            button["web_app"] = {"url": url}
        else:
            button["url"] = direct
        rows = [[button]]
        if private and direct:
            rows.append([{"text": "Open via Telegram link", "url": direct}])
        payload = {
            "chat_id": int(target["chat_id"]),
            "text": rec["title"],
            "reply_markup": {"inline_keyboard": rows},
            "disable_notification": False,
        }
        if target.get("thread_id"):
            payload["message_thread_id"] = int(target["thread_id"])
        try:
            response = self.send_message(payload)
        except Exception as exc:
            self.store.close(view_id, target)
            raise SpecError("Launch message failed; view closed (" + type(exc).__name__ + ")") from exc
        if not response.get("ok") or not (response.get("result") or {}).get("message_id"):
            self.store.close(view_id, target)
            raise SpecError("Telegram rejected the launch message; view closed")
        message_id = response["result"]["message_id"]
        self.store.message(view_id, message_id)
        return {
            "success": True,
            "id": view_id,
            "message_id": message_id,
            "url": url,
            "direct_link": direct,
            "expires_at": rec["expires_at"],
            "renderer": rec["renderer"],
        }

    def tool_handler(self, args, **_kw):
        try:
            obj(
                args,
                "arguments",
                ("action", "title", "name", "renderer", "spec", "openui", "sources", "actions", "id", "expires_in"),
                ("action",),
            )
            operation = args["action"]
            if operation == "catalog":
                return json.dumps(
                    {
                        "success": True,
                        "catalog": (Path(__file__).parent / "CATALOG.md").read_text(),
                        "example": json.loads((Path(__file__).parent / "demo.json").read_text()),
                        "openui": (Path(__file__).parent / "OPENUI.md").read_text(),
                        "sources": self.registry.manifest(self.profile_name),
                        "saved_views": self.saved_views.manifest(),
                    },
                    ensure_ascii=False,
                )
            if operation == "list_saved":
                return json.dumps({"success": True, "saved_views": self.saved_views.manifest()}, ensure_ascii=False)
            if operation == "save":
                template = self._template_from_args(args)
                name = args.get("name")
                stored = self.saved_views.save(name, template)
                return json.dumps(
                    {"success": True, "name": stored["name"], "renderer": stored["renderer"], "title": stored["title"]},
                    ensure_ascii=False,
                )
            if not self.enabled:
                raise SpecError("Mini App disabled: configure HTE_MINIAPP_PUBLIC_URL and a unique HTE_MINIAPP_PORT for this profile")
            if not self._serving:
                raise SpecError("Mini App listener is not running; no launch message sent")
            target = self._current_target()
            if operation == "send":
                return json.dumps(self._launch(self._template_from_args(args), target, args.get("expires_in", 3600)))
            if operation == "open_saved":
                return json.dumps(self._launch(self.saved_views.get(args.get("name")), target, args.get("expires_in", 3600)))
            if operation not in ("update", "close"):
                raise SpecError("Unknown action")
            text(args.get("id"), "id", 48, False)
            rec = self.store.update(args["id"], args.get("spec"), target) if operation == "update" else self.store.close(args["id"], target)
            return json.dumps({"success": True, "id": rec["id"], "revision": rec["revision"], "closed": rec["closed"]})
        except (SpecError, SourceError, ValueError, TypeError) as exc:
            return json.dumps({"success": False, "error": str(exc)})
        except Exception:
            LOG.exception("Mini App tool failed")
            return json.dumps({"success": False, "error": "Mini App storage/transport failure; no success claimed"})

    def _authenticate(self, request):
        id = request.match_info["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{24}", id):
            raise AuthError("Invalid view id")
        rec = self.store.get(id)
        if rec is None:
            raise AuthError("View unavailable; reopen its Telegram launch button")
        header = request.headers.get("Authorization", "")
        raw = header[4:] if header.startswith("tma ") else ""
        auth = verify_init_data(
            raw,
            self.bot_token_getter(),
            user_id=rec["user_id"],
            spec_id=id,
            query_id=request.query.get("id"),
        )
        if rec["expires_at"] <= time.time():
            raise ApiError(410, "This view expired. Ask for a new one.")
        return rec, auth["user"]

    async def _public_record(self, rec):
        if rec.get("renderer", "json") != "openui":
            return public_record(rec)
        try:
            openui = await asyncio.to_thread(
                hydrate_openui,
                rec["openui"],
                rec.get("sources", []),
                self.registry,
                self.profile_name,
            )
        except SourceError as exc:
            raise ApiError(503, "Live view data is unavailable. Reopen to refresh.") from exc
        return public_record(rec, openui=openui)

    async def _spec(self, request):
        rec, _ = self._authenticate(request)
        return web.json_response(await self._public_record(rec))

    def _has_sensitive_sources(self, rec):
        if rec.get("renderer", "json") != "openui":
            return False
        if "sensitive_sources" in rec:
            return bool(rec["sensitive_sources"])
        # Saved records from before the persisted flag fail closed if a source is
        # no longer available to classify.
        try:
            return any(
                self.registry.sensitivity(binding["name"], self.profile_name) == "sensitive"
                for binding in rec.get("sources", [])
            )
        except SourceError:
            return True

    @staticmethod
    def _sensitive_chat_action(rec, action):
        policy = next(
            (item for item in rec.get("openui_actions", []) if item["key"] == action["key"]),
            {},
        )
        fields = policy.get("fields", [])
        submitted = action.get("value")
        form = submitted.get("form") if isinstance(submitted, dict) else None
        return {
            "key": action["key"],
            "value": {
                "form": {
                    field: copy.deepcopy(form[field])
                    for field in fields
                    if isinstance(form, dict) and field in form
                }
            },
            "component_id": action["component_id"],
            "kind": action["kind"],
            "once": action["once"],
        }

    async def _action(self, request):
        initial, user = self._authenticate(request)
        if request.content_type != "application/json":
            raise ApiError(415, "Use JSON")
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            raise SpecError("Invalid JSON")
        obj(
            body,
            "action",
            ("key", "value", "request_id", "revision"),
            ("key", "request_id", "revision"),
        )
        rid = body["request_id"]
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", rid):
            raise SpecError("Invalid request_id")
        if type(body["revision"]) is not int:
            raise SpecError("revision must be an integer")
        id = initial["id"]
        now = time.time()
        # Persist a claim BEFORE delivery; parallel requests cannot produce two events.
        with self.store.transaction() as data:
            rec = data.get(id)
            if not rec or rec["closed"] or rec["expires_at"] <= now:
                raise ApiError(410, "This view is closed or expired.")
            if rec["revision"] != body["revision"]:
                raise ApiError(
                    409,
                    "This view changed. Review the latest version before submitting.",
                )
            act = (
                validate_openui_action(rec, body["key"], body.get("value"))
                if rec.get("renderer", "json") == "openui"
                else validate_action(rec["spec"], body["key"], body.get("value"))
            )
            fingerprint = json.dumps(
                {"key": act["key"], "value": act["value"], "revision": rec["revision"]},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            previous = rec["receipts"].get(rid)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ApiError(
                        409, "request_id already used for a different submission"
                    )
                if previous["status"] == "recorded":
                    return web.json_response({"ok": True, "record": public_record(rec)})
                if previous["status"] in ("pending", "uncertain"):
                    raise ApiError(
                        409,
                        "Submission delivery is pending or uncertain. Do not submit again; check the conversation.",
                    )
            for result in rec["results"].values():
                same = result["key"] == act["key"]
                grouped = (
                    act.get("group_once", False) or result.get("group_once", False)
                ) and result["component_id"] == act["component_id"]
                if result["status"] in ("pending", "uncertain") and (same or grouped):
                    raise ApiError(
                        409,
                        "Submission is pending or uncertain; check the conversation.",
                    )
                if result["status"] == "recorded" and (
                    (same and (act["once"] or result.get("once"))) or grouped
                ):
                    raise ApiError(409, "Already submitted.")
            if len(rec["receipts"]) >= MAX_RESULTS and not previous:
                raise ApiError(429, "Submission limit reached; ask for a new view.")
            result = dict(
                act,
                status="pending",
                request_id=rid,
                at=now,
                event_id=previous["event_id"] if previous else secrets.token_hex(16),
            )
            rec["results"][act["key"]] = result
            rec["receipts"][rid] = {
                "fingerprint": fingerprint,
                "status": "pending",
                "event_id": result["event_id"],
            }
            payload = {
                k: rec[k]
                for k in (
                    "id",
                    "title",
                    "chat_id",
                    "thread_id",
                    "user_id",
                    "session_key",
                    "message_id",
                    "revision",
                )
            }
            sensitive = self._has_sensitive_sources(rec)
            chat_action = self._sensitive_chat_action(rec, act) if sensitive else act
            payload.update(
                action=copy.deepcopy(chat_action),
                event_id=result["event_id"],
                request_id=rid,
                text=injection_text(
                    rec,
                    chat_action,
                    user.get("first_name") or "user",
                    sensitive=sensitive,
                ),
            )
        outcome = "uncertain"
        try:
            if inspect.iscoroutinefunction(self.deliver_action):
                accepted = await asyncio.wait_for(self.deliver_action(payload), 12)
            else:
                accepted = await asyncio.wait_for(
                    asyncio.to_thread(self.deliver_action, payload), 12
                )
                if inspect.isawaitable(accepted):
                    accepted = await asyncio.wait_for(accepted, 12)
            outcome = (
                "recorded"
                if accepted is True
                else ("failed" if accepted is False else "uncertain")
            )
        except asyncio.CancelledError:
            # Claim remains pending across shutdown; fail closed rather than duplicate delivery.
            raise
        except Exception:
            LOG.warning(
                "Mini App event %s delivery uncertain",
                payload["event_id"],
                exc_info=True,
            )
        with self.store.transaction() as data:
            rec = data.get(id)
            if rec is None:
                raise ApiError(410, "View expired during submission")
            rec["results"][act["key"]]["status"] = outcome
            rec["receipts"][rid]["status"] = outcome
            result_record = copy.deepcopy(rec)
        if outcome == "failed":
            raise ApiError(503, "Conversation delivery failed; retry this submission.")
        if outcome == "uncertain":
            raise ApiError(
                503,
                "Conversation delivery is uncertain. Check the conversation; do not submit again.",
            )
        return web.json_response({"ok": True, "record": await self._public_record(result_record)})

    def create_app(self):
        @web.middleware
        async def security(request, handler):
            try:
                response = await handler(request)
            except AuthError as exc:
                response = web.json_response({"error": str(exc)}, status=401)
            except SpecError as exc:
                response = web.json_response({"error": str(exc)}, status=400)
            except ApiError as exc:
                response = web.json_response({"error": exc.message}, status=exc.status)
            except web.HTTPException as exc:
                response = web.json_response({"error": exc.reason}, status=exc.status)
            except Exception:
                LOG.exception("Mini App request failed")
                response = web.json_response(
                    {
                        "error": "Server error. No success claimed; check the conversation before retrying."
                    },
                    status=500,
                )
            response.headers.update(
                {
                    "Content-Security-Policy": CSP,
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                }
            )
            return response

        async def shell(_request):
            return web.Response(
                body=(STATIC / "index.html").read_bytes(), content_type="text/html"
            )

        async def static(request):
            name = request.match_info["name"]
            if name not in ("app.js", "renderer.js", "layout.js", "style.css", "openui.js", "openui.css"):
                raise web.HTTPNotFound()
            return web.Response(
                body=(STATIC / name).read_bytes(),
                content_type="text/css"
                if name.endswith(".css")
                else "application/javascript",
            )

        app = web.Application(middlewares=[security], client_max_size=16384)

        async def serving(_app):
            self._serving = True

        async def stopped(_app):
            self._serving = False

        app.on_startup.append(serving)
        app.on_cleanup.append(stopped)
        app.router.add_get("/m", shell)
        app.router.add_get("/m/static/{name}", static)
        app.router.add_get("/api/spec/{id}", self._spec)
        app.router.add_post("/api/action/{id}", self._action)
        return app

    async def start(self):
        """Run on the gateway loop. Disabled profiles do not bind a port."""
        if not self.enabled or self._runner is not None:
            return False
        runner = web.AppRunner(self.create_app(), access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, self.host, self.port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner

        async def sweep():
            while True:
                await asyncio.sleep(60)
                try:
                    await asyncio.to_thread(self.store.sweep)
                except Exception:
                    LOG.exception("Mini App expiry sweep failed")

        self._sweeper = asyncio.create_task(sweep())
        return True

    async def stop(self):
        if self._sweeper:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None


def setup(
    ctx,
    bot_token_getter,
    deliver_action,
    *,
    state_dir,
    current_target,
    send_message,
    env=None,
    registry: SourceRegistry | None = None,
):
    """Return MiniApp; coordinator registers tool/lifecycle. No global state or startup side effects.

    current_target() -> dict or (dict,error), same as plugin _current_target.
    send_message(payload) -> sync Bot API sendMessage response.
    deliver_action(payload) -> bool or awaitable bool: True only after conversation
    acceptance; False guarantees no event was accepted. Exceptions/timeouts are
    uncertain and deliberately NOT auto-retried. event_id is stable for retries.
    """
    return MiniApp(
        ctx,
        bot_token_getter,
        deliver_action,
        state_dir=state_dir,
        current_target=current_target,
        send_message=send_message,
        env=env,
        registry=registry,
    )
