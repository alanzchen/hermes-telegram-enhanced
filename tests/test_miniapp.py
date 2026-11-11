"""Mini App contracts; all fixtures are synthetic, no Telegram traffic."""

import asyncio
import hashlib
import hmac
import json
import sys
import tempfile
import time
import unittest
import subprocess
import shutil
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from miniapp.spec import SpecError, validate_spec, validate_action
from miniapp.auth import AuthError, verify_init_data
from miniapp.store import SpecStore
from miniapp import setup
from aiohttp.test_utils import TestClient, TestServer

TOKEN = "123456:synthetic-test-only"
TARGET = dict(
    chat_id="42",
    user_id="42",
    thread_id="7",
    session_key="test:telegram:42:7",
    chat_type="dm",
)


def signed(spec_id="demo", user=42, stamp=None, **extra):
    fields = dict(
        auth_date=str(int(time.time() if stamp is None else stamp)),
        user=json.dumps({"id": user, "first_name": "Test"}),
        **extra,
    )
    if spec_id is not None:
        fields["start_param"] = spec_id
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        key,
        "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(fields)


def ui(*components):
    return {"version": 1, "components": list(components)}


def action(key="save"):
    return {"key": key, "label": "Save"}


COMPONENTS = [
    {"type": "heading", "text": "Hello", "level": 2},
    {"type": "text", "text": "**Hi** <script>x</script>", "markdown": True},
    {"type": "callout", "text": "Notice", "tone": "warning"},
    {"type": "kv", "items": [{"key": "Cost", "value": "$10"}]},
    {
        "type": "table",
        "columns": ["Name", "Count"],
        "rows": [["A", 2]],
        "sortable": True,
    },
    {"type": "list", "items": ["One", "Two"]},
    {"type": "checklist", "items": [{"text": "Done", "checked": True}]},
    {
        "type": "choice",
        "id": "pick",
        "label": "Choose",
        "options": [
            {"value": "a", "label": "A", "details": "Details"},
            {"value": "b", "label": "B"},
        ],
        "action": action("pick"),
    },
    {
        "type": "form",
        "id": "form",
        "fields": [
            {
                "key": "name",
                "type": "text",
                "label": "Name",
                "required": True,
                "max_length": 20,
            },
            {"key": "count", "type": "number", "label": "Count", "min": 0, "max": 10},
            {"key": "when", "type": "date", "label": "When"},
            {
                "key": "plan",
                "type": "select",
                "label": "Plan",
                "options": [{"value": "a", "label": "A"}],
            },
            {"key": "notify", "type": "toggle", "label": "Notify"},
        ],
        "action": action(),
    },
    {"type": "buttons", "id": "buttons", "actions": [action("ok"), action("no")]},
    {
        "type": "chart",
        "kind": "bar",
        "labels": ["A", "B"],
        "series": [{"name": "Cost", "values": [1, 2]}],
    },
    {
        "type": "gantt",
        "tasks": [
            {"id": "t1", "label": "Plan", "start": "2026-11-01", "end": "2026-11-03"},
            {
                "id": "t2",
                "label": "Ship",
                "start": "2026-11-04",
                "end": "2026-11-04",
                "milestone": True,
                "depends_on": ["t1"],
            },
        ],
        "today": "2026-11-02",
    },
    {
        "type": "flow",
        "nodes": [
            {"id": "a", "label": "Start"},
            {"id": "b", "label": "Finish", "shape": "diamond"},
        ],
        "edges": [{"from": "a", "to": "b", "label": "Go"}],
    },
    {
        "type": "timeline",
        "items": [{"date": "Today", "title": "Shipped", "text": "Details"}],
    },
    {"type": "image", "url": "https://example.com/img.png", "alt": "Preview"},
    {"type": "divider"},
    {
        "type": "tabs",
        "tabs": [
            {"label": "One", "components": [{"type": "text", "text": "Tab content"}]}
        ],
    },
    {
        "type": "section",
        "title": "More",
        "components": [{"type": "text", "text": "Nested"}],
    },
]


class SpecTests(unittest.TestCase):
    def test_every_component_accepts_and_rejects_unknown_fields(self):
        for component in COMPONENTS:
            with self.subTest(type=component["type"]):
                self.assertEqual(validate_spec(ui(component)), ui(component))
                with self.assertRaises(SpecError):
                    validate_spec(ui(dict(component, onclick="bad()")))

    def test_rejects_bad_shapes_caps_and_references(self):
        cases = [
            ui({"type": "script"}),
            {"version": 2, "components": []},
            ui({"type": "text", "text": 4}),
            ui({"type": "text", "text": "x" * 70000}),
            ui({"type": "image", "url": "javascript:alert(1)"}),
            ui({"type": "table", "columns": ["a"], "rows": [[1, 2]]}),
            ui(
                {
                    "type": "chart",
                    "kind": "bar",
                    "labels": ["A"],
                    "series": [{"name": "n", "values": [float("nan")]}],
                }
            ),
            ui(
                {
                    "type": "chart",
                    "kind": "pie-lite",
                    "labels": ["A"],
                    "series": [{"name": "n", "values": [-1]}],
                }
            ),
            ui(
                {
                    "type": "gantt",
                    "tasks": [
                        {
                            "id": "a",
                            "label": "Bad",
                            "start": "2026-02-30",
                            "end": "2026-03-01",
                        }
                    ],
                }
            ),
            ui(
                {
                    "type": "flow",
                    "nodes": [{"id": "a", "label": "A"}],
                    "edges": [{"from": "a", "to": "b"}],
                }
            ),
            ui(COMPONENTS[7], COMPONENTS[7]),
        ]
        for value in cases:
            with self.subTest(value=str(value)[:100]), self.assertRaises(SpecError):
                validate_spec(value)

    def test_action_whitelist_types_and_server_labels(self):
        spec = ui(*COMPONENTS)
        self.assertEqual(validate_action(spec, "pick", "a")["label"], "Save")
        self.assertEqual(validate_action(spec, "pick", "a")["selection_labels"], ["A"])
        self.assertEqual(validate_action(spec, "ok", None)["value"], "")
        self.assertEqual(
            validate_action(spec, "save", {"name": "Test", "count": 2})["value"][
                "count"
            ],
            2,
        )
        for key, value in [
            ("unknown", None),
            ("pick", "other"),
            ("pick", ["a"]),
            ("save", {"name": "x" * 21}),
            ("save", {"name": "x", "count": True}),
            ("save", {"name": "x", "count": 11}),
            ("save", {"name": "x", "when": "2026-02-30"}),
            ("save", {"name": "x", "notify": "true"}),
            ("save", {"name": "x", "injected": 1}),
            ("save", {}),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(SpecError):
                validate_action(spec, key, value)

    def test_multiselect_unique_and_bounded(self):
        comp = dict(COMPONENTS[7], multiple=True, max_selected=2)
        self.assertEqual(
            validate_action(ui(comp), "pick", ["b", "a"])["selection_labels"],
            ["B", "A"],
        )
        for values in [["a", "a"], ["no"], [], ["a"] * 3]:
            with self.assertRaises(SpecError):
                validate_action(ui(comp), "pick", values)


class AuthTests(unittest.TestCase):
    def test_valid_hmac_includes_signature_field(self):
        result = verify_init_data(
            signed(signature="telegram-signature"), TOKEN, user_id="42", spec_id="demo"
        )
        self.assertEqual(result["user"]["id"], 42)

    def test_rejects_expired_future_wrong_user_wrong_spec_forged(self):
        for raw in [
            signed(stamp=time.time() - 3601),
            signed(stamp=time.time() + 60),
            signed(user=43),
            signed("other"),
            signed().replace("hash=", "hash=0"),
            signed() + "&auth_date=1",
        ]:
            with self.subTest(raw=raw[:30]), self.assertRaises(AuthError):
                verify_init_data(raw, TOKEN, user_id="42", spec_id="demo")
        with self.assertRaises(AuthError):
            verify_init_data(signed(None), TOKEN, user_id="42", spec_id="demo")
        self.assertEqual(
            verify_init_data(
                signed(None), TOKEN, user_id="42", spec_id="demo", query_id="demo"
            )["user"]["id"],
            42,
        )


class StoreTests(unittest.TestCase):
    def test_persist_owner_ttl_revision_and_caps(self):
        with tempfile.TemporaryDirectory() as d:
            store = SpecStore(Path(d) / "miniapps.json", max_records=2)
            rec = store.create(ui(COMPONENTS[9]), "Test", TARGET, 60)
            self.assertEqual(SpecStore(store.path).get(rec["id"])["title"], "Test")
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(SpecError):
                store.update(rec["id"], ui(COMPONENTS[0]), dict(TARGET, thread_id="8"))
            self.assertEqual(
                store.update(rec["id"], ui(COMPONENTS[0]), TARGET)["revision"], 2
            )
            store.close(rec["id"], TARGET)
            self.assertTrue(store.get(rec["id"])["closed"])
            store.create(ui(COMPONENTS[0]), "Two", TARGET, 60)
            with self.assertRaises(SpecError):
                store.create(ui(COMPONENTS[0]), "Three", TARGET, 60)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_start_stop_and_disabled_profile(self):
        import socket
        from aiohttp import ClientSession

        with tempfile.TemporaryDirectory() as d:
            service = setup(
                None,
                lambda: TOKEN,
                lambda p: True,
                state_dir=d,
                current_target=lambda: TARGET,
                send_message=lambda p: None,
                env={},
            )
            self.assertFalse(await service.start())
            self.assertFalse(service.check_requirements())
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            service = setup(
                None,
                lambda: TOKEN,
                lambda p: True,
                state_dir=d,
                current_target=lambda: TARGET,
                send_message=lambda p: None,
                env={
                    "HTE_MINIAPP_PORT": str(port),
                    "HTE_MINIAPP_PUBLIC_URL": "https://example.test",
                },
            )
            try:
                self.assertTrue(await service.start())
                self.assertTrue(service.check_requirements())
                self.assertFalse(await service.start())
                async with ClientSession() as client:
                    async with client.get(f"http://127.0.0.1:{port}/m") as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn("telegram-web-app.js", await response.text())
            finally:
                await service.stop()
            self.assertFalse(service.check_requirements())
            await service.stop()


class RendererTests(unittest.TestCase):
    def test_node_geometry_and_xss_contract(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node required for renderer contract")
        script = (
            Path(__file__).resolve().parents[1] / "miniapp/testing/renderer_test.cjs"
        )
        result = subprocess.run(
            [node, str(script)], text=True, capture_output=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.sent = []

        def deliver(payload):
            self.events.append(payload)
            return True

        def send(payload):
            self.sent.append(payload)
            return {"ok": True, "result": {"message_id": 99}}

        self.service = setup(
            None,
            lambda: TOKEN,
            deliver,
            state_dir=self.tmp.name,
            current_target=lambda: (TARGET, None),
            send_message=send,
            env={
                "HTE_MINIAPP_PUBLIC_URL": "https://ui.example.test",
                "HTE_MINIAPP_PORT": "8787",
            },
        )
        self.client = TestClient(TestServer(self.service.create_app()))
        await self.client.start_server()
        self.rec = self.service.store.create(
            ui(COMPONENTS[7], COMPONENTS[8], COMPONENTS[9]), "Decision", TARGET, 60
        )
        self.headers = {"Authorization": "tma " + signed(self.rec["id"])}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def post(self, key, value, request_id="req-12345678", revision=1):
        return await self.client.post(
            "/api/action/" + self.rec["id"],
            headers=self.headers,
            json={
                "key": key,
                "value": value,
                "request_id": request_id,
                "revision": revision,
            },
        )

    async def test_shell_csp_and_protected_spec(self):
        shell = await self.client.get("/m")
        self.assertEqual(shell.status, 200)
        self.assertNotIn("unsafe-inline", shell.headers["Content-Security-Policy"])
        self.assertIn("default-src 'self'", shell.headers["Content-Security-Policy"])
        self.assertEqual(
            (await self.client.get("/api/spec/" + self.rec["id"])).status, 401
        )
        response = await self.client.get(
            "/api/spec/" + self.rec["id"], headers=self.headers
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertNotIn("session_key", data)
        self.assertEqual(data["revision"], 1)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_action_delivers_once_and_survives_reload(self):
        response = await self.post("pick", "a")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["session_key"], TARGET["session_key"])
        self.assertIn("A", self.events[0]["text"])
        self.assertEqual((await self.post("pick", "a")).status, 200)
        self.assertEqual(len(self.events), 1)
        self.assertEqual((await self.post("pick", "b", "req-other123")).status, 409)
        stored = SpecStore(self.service.store.path).get(self.rec["id"])
        self.assertEqual(stored["results"]["pick"]["status"], "recorded")
        self.assertEqual(stored["results"]["pick"]["value"], "a")

    async def test_validation_wrong_user_stale_revision_and_close(self):
        self.assertEqual((await self.post("pick", "injected")).status, 400)
        self.assertEqual((await self.post("pick", "a", revision=2)).status, 409)
        self.assertEqual(
            (
                await self.client.get(
                    "/api/spec/" + self.rec["id"],
                    headers={"Authorization": "tma " + signed(self.rec["id"], user=43)},
                )
            ).status,
            401,
        )
        self.service.store.close(self.rec["id"], TARGET)
        self.assertEqual((await self.post("pick", "a")).status, 410)
        self.assertFalse(self.events)

    async def test_uncertain_callback_never_retries_and_group_claim_survives_update(
        self,
    ):
        self.service.deliver_action = lambda payload: (
            None
        )  # not an explicit non-acceptance
        first = await self.post("pick", "a")
        self.assertEqual(first.status, 503)
        self.assertIn("uncertain", (await first.json())["error"])
        self.assertEqual((await self.post("pick", "a")).status, 409)
        fresh = self.service.store.create(ui(COMPONENTS[9]), "Buttons", TARGET, 60)
        self.rec = fresh
        self.headers = {"Authorization": "tma " + signed(fresh["id"])}
        self.service.deliver_action = lambda payload: True
        self.assertEqual((await self.post("ok", None)).status, 200)
        new_component = dict(COMPONENTS[9], once=False)
        self.service.store.update(fresh["id"], ui(new_component), TARGET)
        self.assertEqual(
            (await self.post("no", None, "req-next123", revision=2)).status, 409
        )

    async def test_concurrent_duplicate_claim_and_expiry(self):
        ready = asyncio.Event()
        release = asyncio.Event()

        async def deliver(payload):
            ready.set()
            await release.wait()
            self.events.append(payload)
            return True

        self.service.deliver_action = deliver
        first = asyncio.create_task(self.post("pick", "a"))
        await ready.wait()
        self.assertEqual((await self.post("pick", "a")).status, 409)
        release.set()
        self.assertEqual((await first).status, 200)
        self.assertEqual(len(self.events), 1)
        with self.service.store.transaction() as data:
            data[self.rec["id"]]["expires_at"] = time.time() - 1
        self.assertEqual(
            (
                await self.client.get(
                    "/api/spec/" + self.rec["id"], headers=self.headers
                )
            ).status,
            410,
        )
        self.assertEqual((await self.post("ok", None, "new-req-123")).status, 410)

    async def test_failure_is_visible_and_retry_not_silent_loss(self):
        self.service.deliver_action = lambda payload: False
        response = await self.post("pick", "a")
        self.assertEqual(response.status, 503)
        self.assertEqual(
            (await response.json())["error"],
            "Conversation delivery failed; retry this submission.",
        )
        self.service.deliver_action = lambda payload: (
            self.events.append(payload) or True
        )
        self.assertEqual((await self.post("pick", "a")).status, 200)
        self.assertEqual(len(self.events), 1)

    async def test_direct_link_group_and_send_failure(self):
        self.service.direct_link = "https://t.me/ExampleBot/demo"
        group = dict(TARGET, chat_id="-100123", chat_type="group")
        self.service.current_target = lambda: group
        res = json.loads(
            self.service.tool_handler(
                {"action": "send", "title": "Group view", "spec": ui(COMPONENTS[0])}
            )
        )
        self.assertTrue(res["success"], res)
        button = self.sent[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(
            button["url"], "https://t.me/ExampleBot/demo?startapp=" + res["id"]
        )
        self.assertNotIn("web_app", button)
        self.service.send_message = lambda payload: {"ok": False}
        failed = json.loads(
            self.service.tool_handler(
                {"action": "send", "title": "Failed view", "spec": ui(COMPONENTS[0])}
            )
        )
        self.assertFalse(failed["success"])
        with self.service.store.lock:
            failed_records = [
                r
                for r in self.service.store._read().values()
                if r["title"] == "Failed view"
            ]
        self.assertEqual(len(failed_records), 1)
        self.assertTrue(failed_records[0]["closed"])

    async def test_body_limit_and_non_json_rejected(self):
        path = "/api/action/" + self.rec["id"]
        response = await self.client.post(
            path,
            headers=self.headers,
            json={
                "key": "save",
                "value": {"name": "x" * 18000},
                "request_id": "oversize123",
                "revision": 1,
            },
        )
        self.assertEqual(response.status, 413)
        response = await self.client.post(path, headers=self.headers, data="key=save")
        self.assertEqual(response.status, 415)
        self.assertFalse(self.events)

    async def test_tool_refuses_when_listener_unavailable(self):
        await self.client.close()
        result = json.loads(
            self.service.tool_handler(
                {"action": "send", "title": "No listener", "spec": ui(COMPONENTS[0])}
            )
        )
        self.assertFalse(result["success"])
        self.assertFalse(self.sent)

    async def test_tool_send_update_close_current_route(self):
        result = json.loads(
            self.service.tool_handler(
                {"action": "send", "title": "Test", "spec": ui(COMPONENTS[0])}
            )
        )
        self.assertTrue(result["success"], result)
        payload = self.sent[0]
        self.assertEqual(payload["message_thread_id"], 7)
        self.assertIn(
            "/m?id=", payload["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
        )
        result2 = json.loads(
            self.service.tool_handler(
                {"action": "update", "id": result["id"], "spec": ui(COMPONENTS[1])}
            )
        )
        self.assertTrue(result2["success"])
        self.assertEqual(self.service.store.get(result["id"])["revision"], 2)
        self.assertTrue(
            json.loads(
                self.service.tool_handler({"action": "close", "id": result["id"]})
            )["success"]
        )
        self.assertTrue(self.service.store.get(result["id"])["closed"])


if __name__ == "__main__":
    unittest.main()
