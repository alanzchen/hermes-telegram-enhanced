"""Generative Mini App registry contracts; all data is synthetic."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer

from miniapp import setup
from miniapp.sources import DataSource, SourceRegistry, default_registry
from test_miniapp import TARGET, TOKEN, signed


OPENUI = 'root = Card([CardHeader("Balance"), Text({{balance.data.label}}), Button("Refresh", {"type":"refresh"})])'


class RegistryTests(unittest.TestCase):
    def test_manifest_is_value_free_and_finance_is_profile_scoped(self):
        calls = []

        def fetch(_args):
            calls.append(True)
            return {"data": {"label": "synthetic-private-value"}}

        registry = SourceRegistry(
            [
                DataSource(
                    name="finance.balance",
                    title="Balance",
                    args_schema={"type": "object", "additionalProperties": False},
                    output_schema={"type": "object"},
                    sensitivity="sensitive",
                    profiles=("default", "coordinator"),
                    query=fetch,
                ),
                DataSource(
                    name="basecamp.projects",
                    title="Projects",
                    args_schema={"type": "object", "additionalProperties": False},
                    output_schema={"type": "object"},
                    sensitivity="private",
                    query=lambda _args: {"rows": []},
                ),
            ]
        )

        catalog = registry.manifest("coordinator")
        encoded = json.dumps(catalog)
        self.assertIn("finance.balance", encoded)
        self.assertIn("basecamp.projects", encoded)
        self.assertNotIn("synthetic-private-value", encoded)
        self.assertEqual(calls, [])
        self.assertEqual(
            [item["name"] for item in registry.manifest("research")],
            ["basecamp.projects"],
        )
    def test_builtin_registry_wires_finance_and_non_finance_manifests(self):
        registry = default_registry()
        coordinator = {item["name"] for item in registry.manifest("coordinator")}
        self.assertTrue(
            {
                "finance.overview",
                "finance.cards",
                "finance.spending",
                "finance.holdings",
                "finance.review_queue",
                "finance.data_health",
                "basecamp.projects",
            } <= coordinator
        )
        self.assertEqual(
            {item["name"] for item in registry.manifest("research")},
            {"basecamp.projects"},
        )


class OpenUIAssetTests(unittest.TestCase):
    def test_vendored_renderer_is_loaded_from_the_miniapp_origin(self):
        static = Path(__file__).resolve().parents[1] / "miniapp" / "static"
        html = (static / "index.html").read_text()
        self.assertNotIn('rel="stylesheet" href="m/static/openui.css"', html)
        self.assertIn('m/static/openui.js', html)
        bundle_path = static / "openui.js"
        css_path = static / "openui.css"
        if not bundle_path.is_file() or not css_path.is_file():
            self.skipTest("OpenUI bundle missing; run the harness build to generate openui.js and openui.css")
        bundle = bundle_path.read_text()
        self.assertIn("HteOpenUi", bundle)
        self.assertNotIn("unsafe-eval", bundle)
        app = (static / "app.js").read_text()
        self.assertIn("event.value??null", app)
        self.assertIn("record?.renderer!=='openui'", app)
        self.assertIn("loadStyles(next.renderer)", app)
        self.assertIn("openui-miniapp", app)
        self.assertIn("expiresAt:next.expires_at*1000", app)
        self.assertIn("results:next.results||{}", app)
        for component in ("Stat", "Grid", "List", "CheckboxGroup"):
            self.assertIn(component, bundle)
        for affordance in ("bar-chart", "form-hint", "tab-list"):
            self.assertIn(affordance, bundle)
        self.assertIn("tabular-nums", bundle)
        self.assertIn("sticky", bundle)
        css = (static / "openui.css").read_text()
        self.assertIn("PingFang SC", css)
        self.assertIn("openui-safe-background", css)
        self.assertIn(".openui-miniapp", css)
        self.assertNotIn(":root", css)
        self.assertNotIn("--tg-theme-", css)
        self.assertIn("data-theme=dark", css)
        self.assertIn("safe-area-inset-left", css)
        self.assertIn("openui-stat-tone-success", css)
        legacy_css = (static / "style.css").read_text()
        self.assertIn("main ul:not(.checklist){list-style:disc}", legacy_css)
        self.assertIn("main ol{list-style:decimal}", legacy_css)


class SavedOpenUIViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self.events = []
        self.values = iter(("first synthetic value", "second synthetic value", "third synthetic value", "fourth synthetic value"))
        self.fetches = 0

        def fetch(_args):
            self.fetches += 1
            return {
                "source": "finance.balance",
                "generated_at": "2026-11-15T00:00:00Z",
                "data": {"label": next(self.values)},
            }

        self.registry = SourceRegistry(
            [
                DataSource(
                    name="finance.balance",
                    title="Balance",
                    args_schema={"type": "object", "additionalProperties": False},
                    output_schema={
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "generated_at": {"type": "string"},
                            "data": {
                                "type": "object",
                                "properties": {"label": {"type": "string"}},
                            },
                        },
                    },
                    sensitivity="private",
                    profiles=("coordinator",),
                    query=fetch,
                )
            ]
        )
        self.service = setup(
            None,
            lambda: TOKEN,
            lambda payload: self.events.append(payload) or True,
            state_dir=self.tmp.name,
            current_target=lambda: (TARGET, None),
            send_message=lambda payload: self.sent.append(payload)
            or {"ok": True, "result": {"message_id": 101}},
            env={
                "HTE_MINIAPP_PUBLIC_URL": "https://ui.example.test/coordinator",
                "HTE_MINIAPP_PORT": "8787",
                "HTE_MINIAPP_PROFILE": "coordinator",
                "HTE_MINIAPP_OWNER_USER_ID": "42",
            },
            registry=self.registry,
        )
        self.service._serving = True
        self.client = TestClient(TestServer(self.service.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_saved_openui_reopens_and_refreshes_after_authenticated_fetch(self):
        saved = json.loads(
            self.service.tool_handler(
                {
                    "action": "save",
                    "name": "balance",
                    "title": "Balance",
                    "renderer": "openui",
                    "openui": OPENUI,
                    "sources": [
                        {"alias": "balance", "name": "finance.balance", "args": {}}
                    ],
                    "actions": [{"key": "refresh", "label": "Refresh"}],
                }
            )
        )
        self.assertTrue(saved["success"], saved)
        self.assertEqual(self.fetches, 0)

        catalog = json.loads(self.service.tool_handler({"action": "catalog"}))
        self.assertNotIn("first synthetic value", json.dumps(catalog))
        self.assertIn("finance.balance", json.dumps(catalog))

        opened = json.loads(
            self.service.tool_handler(
                {"action": "open_saved", "name": "balance", "expires_in": 60}
            )
        )
        self.assertTrue(opened["success"], opened)
        self.assertEqual(self.fetches, 0)
        self.assertEqual(len(self.sent), 1)

        headers = {"Authorization": "tma " + signed(opened["id"])}
        first = await self.client.get("/api/spec/" + opened["id"], headers=headers)
        self.assertEqual(first.status, 200, await first.text())
        first_data = await first.json()
        self.assertEqual(first_data["renderer"], "openui")
        self.assertIn('"first synthetic value"', first_data["openui"])
        self.assertEqual(self.fetches, 1)

        second = await self.client.get("/api/spec/" + opened["id"], headers=headers)
        self.assertEqual(second.status, 200, await second.text())
        second_data = await second.json()
        self.assertIn('"second synthetic value"', second_data["openui"])
        self.assertEqual(self.fetches, 2)

        response = await self.client.post(
            "/api/action/" + opened["id"],
            headers=headers,
            json={
                "key": "refresh",
                "value": {
                    "params": {"source": "synthetic"},
                    "form": {"profile": {"name": "Synthetic team"}},
                    "form_name": "profile",
                },
                "request_id": "openui-refresh-123",
                "revision": 1,
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        action_result = await response.json()
        self.assertNotIn("openui", action_result["record"])
        self.assertEqual(self.fetches, 2, "actions must not rehydrate provider sources")
        retry = await self.client.post(
            "/api/action/" + opened["id"],
            headers=headers,
            json={
                "key": "refresh",
                "value": {
                    "params": {"source": "synthetic"},
                    "form": {"profile": {"name": "Synthetic team"}},
                    "form_name": "profile",
                },
                "request_id": "openui-refresh-123",
                "revision": 1,
            },
        )
        self.assertEqual(retry.status, 200, await retry.text())
        self.assertNotIn("openui", (await retry.json())["record"])
        self.assertEqual(self.fetches, 2, "idempotent retries must not rehydrate sources")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(
            self.events[0]["action"],
            {
                "key": "refresh",
                "value": {"form": {}},
                "component_id": "openui",
                "kind": "openui",
                "once": True,
            },
        )

    async def test_source_placeholder_is_rejected_from_an_openui_action_value(self):
        result = json.loads(
            self.service.tool_handler(
                {
                    "action": "save",
                    "name": "unsafe-button",
                    "title": "Unsafe button",
                    "renderer": "openui",
                    "openui": 'root = Card([Button("Send", {type: "refresh", params: {{balance.data.label}}})])',
                    "sources": [{"alias": "balance", "name": "finance.balance", "args": {}}],
                    "actions": [{"key": "refresh", "label": "Refresh"}],
                }
            )
        )
        self.assertFalse(result["success"])
        self.assertIn("direct allowlisted", result["error"])
        self.assertEqual(self.fetches, 0)

    async def test_source_placeholder_is_rejected_when_quoted(self):
        result = json.loads(
            self.service.tool_handler(
                {
                    "action": "save",
                    "name": "quoted-placeholder",
                    "title": "Quoted placeholder",
                    "renderer": "openui",
                    "openui": 'root = Card([Text("{{balance.data.label}}")])',
                    "sources": [{"alias": "balance", "name": "finance.balance", "args": {}}],
                    "actions": [],
                }
            )
        )
        self.assertFalse(result["success"])
        self.assertIn("whole component values", result["error"])
        self.assertEqual(self.fetches, 0)

    async def test_button_type_requires_an_allowlisted_action(self):
        result = json.loads(
            self.service.tool_handler(
                {
                    "action": "save",
                    "name": "undeclared-button",
                    "title": "Undeclared button",
                    "renderer": "openui",
                    "openui": 'root = Card([Button("Send", {type: "not_allowed"})])',
                    "sources": [],
                    "actions": [],
                }
            )
        )
        self.assertFalse(result["success"])
        self.assertIn("not declared", result["error"])

    async def test_legacy_json_spec_still_opens(self):
        result = json.loads(
            self.service.tool_handler(
                {
                    "action": "send",
                    "title": "Legacy",
                    "spec": {"version": 1, "components": [{"type": "text", "text": "Still works"}]},
                }
            )
        )
        self.assertTrue(result["success"], result)
        response = await self.client.get(
            "/api/spec/" + result["id"],
            headers={"Authorization": "tma " + signed(result["id"])},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["renderer"], "json")


if __name__ == "__main__":
    unittest.main()
