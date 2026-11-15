"""Round-3 security and correctness regressions; all values are synthetic."""
from __future__ import annotations

import json
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from miniapp import setup
from miniapp.genui import hydrate_openui, validate_bindings, validate_template
from miniapp.sources import DataSource, SourceRegistry
from miniapp.spec import SpecError
from test_miniapp import TARGET, TOKEN, signed


def synthetic_registry(calls: list[str] | None = None) -> SourceRegistry:
    row = {
        "type": "object",
        "additionalProperties": False,
        "required": ["transaction_id", "account_id", "merchant"],
        "properties": {
            "transaction_id": {"type": "string", "x-opaque-id": True},
            "account_id": {"type": "string", "x-opaque-id": True},
            "merchant": {"type": "string"},
        },
    }

    def queue(_args):
        if calls is not None:
            calls.append("queue")
        return {
            "rows": [
                {"transaction_id": "txn-1", "account_id": "acct-1", "merchant": "Synthetic \u2028merchant\u2029"},
                {"transaction_id": "txn-2", "account_id": "acct-2", "merchant": "Synthetic second merchant"},
            ]
        }

    return SourceRegistry([
        DataSource(
            name="finance.queue",
            title="Queue",
            args_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object", "properties": {"rows": {"type": "array", "items": row}}, "required": ["rows"], "additionalProperties": False},
            sensitivity="sensitive",
            profiles=("coordinator",),
            query=queue,
        ),
        DataSource(
            name="finance.overview",
            title="Overview",
            args_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object", "properties": {"data": {"type": "object", "properties": {"cash": {"type": "number"}}, "required": ["cash"]}}, "required": ["data"], "additionalProperties": False},
            sensitivity="sensitive",
            profiles=("coordinator",),
            query=lambda _args: {"data": {"cash": 42.5}},
        ),
    ])


def queue_bindings(registry: SourceRegistry) -> list[dict]:
    return validate_bindings([{"alias": "queue", "name": "finance.queue", "args": {}}], registry, "coordinator")


class StructuralInvariantTests(unittest.TestCase):
    def test_every_placeholder_must_be_a_direct_allowed_call_argument(self):
        registry = synthetic_registry()
        cases = [
            'root = Card([btn])\nleak = {cash: {{overview.data.cash|money}}}\nbtn = Button("Ack", {type: "ack", params: leak})',
            'root = Card([box])\nopts = [{value: {{overview.data.cash|money}}, label: "ack"}]\nbox = CheckboxGroup("ack", opts)',
            'root = Card([Text([{{overview.data.cash|money}}])])',
            'root = Card([Text("ok")])\nleak = {{overview.data.cash|money}}',
        ]
        bindings = validate_bindings([{"alias": "overview", "name": "finance.overview", "args": {}}], registry, "coordinator")
        for source in cases:
            with self.subTest(source=source), self.assertRaisesRegex(SpecError, "placeholders"):
                validate_template(source, bindings, registry, "coordinator")

    def test_parser_reports_spec_errors_for_unicode_surrogates_and_depth(self):
        with self.assertRaises(SpecError):
            validate_template('卡片 = Text("hi")\nroot = Card([卡片])', [], synthetic_registry(), "coordinator")
        with self.assertRaises(SpecError):
            validate_template('root = Card([Text("\\uD800")])', [], synthetic_registry(), "coordinator")
        with self.assertRaises(SpecError):
            validate_template("root = Card(" + "[" * 33 + 'Text("x")' + "]" * 33 + ")", [], synthetic_registry(), "coordinator")

    def test_hydration_decodes_surrogate_pairs_and_escapes_line_separators(self):
        registry = synthetic_registry()
        self.assertEqual(
            validate_template('root = Card([Text("\\uD83D\\uDE00")])', [], registry, "coordinator"),
            'root = Card([Text("\\uD83D\\uDE00")])',
        )
        hydrated = hydrate_openui('root = Card([Text({{queue.rows|pluck:merchant}})])', queue_bindings(registry), registry, "coordinator")
        self.assertNotIn("\u2028", hydrated)
        self.assertNotIn("\u2029", hydrated)
        self.assertIn("\\u2028", hydrated)
        self.assertIn("\\u2029", hydrated)


class RestrictedViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls: list[str] = []
        self.events: list[dict] = []
        self.target = dict(TARGET, thread_id="", origin="telegram")
        self.service = setup(
            None,
            lambda: TOKEN,
            lambda payload: self.events.append(payload) or True,
            state_dir=self.tmp.name,
            current_target=lambda: (self.target, None),
            send_message=lambda _payload: {"ok": True, "result": {"message_id": 1}},
            env={
                "HTE_MINIAPP_PUBLIC_URL": "https://ui.example.test/coordinator",
                "HTE_MINIAPP_PORT": "8787",
                "HTE_MINIAPP_PROFILE": "coordinator",
                "HTE_MINIAPP_OWNER_USER_ID": "42",
            },
            registry=synthetic_registry(self.calls),
        )
        self.service._serving = True
        self.client = TestClient(TestServer(self.service.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def restricted_request(self, openui: str, actions: list[dict], *, name: str = "round3") -> dict:
        return {
            "action": "save", "name": name, "title": "Synthetic", "renderer": "openui", "openui": openui,
            "sources": [{"alias": "queue", "name": "finance.queue", "args": {}}], "actions": actions,
        }

    async def test_static_choice_checkbox_values_are_persisted_minimized_and_not_rehydrated_on_action(self):
        source = '''modeA = Select("mode", [{value: "ack", label: "Ack"}])
modeB = RadioGroup("mode", [{value: "later", label: "Later"}])
flag = Checkbox("confirmed", "Confirmed")
button = Button("Submit", {type: "go", fields: ["mode", "confirmed"], params: {leak: "client"}})
form = Form("f", [modeA, modeB, flag, button])
root = Card([form])'''
        saved = json.loads(self.service.tool_handler(self.restricted_request(source, [{"key": "go", "label": "Submit", "fields": ["mode", "confirmed"]}])))
        self.assertTrue(saved["success"], saved)
        opened = json.loads(self.service.tool_handler({"action": "open_saved", "name": "round3", "expires_in": 60}))
        self.assertTrue(opened["success"], opened)
        headers = {"Authorization": "tma " + signed(opened["id"])}
        spec = await self.client.get("/api/spec/" + opened["id"], headers=headers)
        self.assertEqual(spec.status, 200, await spec.text())
        self.assertEqual(self.calls, ["queue"])
        submitted = await self.client.post(
            "/api/action/" + opened["id"], headers=headers,
            json={"key": "go", "value": {"params": {"steal": "no"}, "form": {"mode": "later", "confirmed": True, "extra": "no"}, "form_name": "f"}, "request_id": "round3-static-123", "revision": 1},
        )
        self.assertEqual(submitted.status, 200, await submitted.text())
        record = (await submitted.json())["record"]
        self.assertNotIn("openui", record)
        self.assertEqual(self.calls, ["queue"], "actions must not rehydrate provider sources")
        self.assertEqual(self.events[0]["action"]["value"], {"form": {"mode": "later", "confirmed": True}})
        stored = self.service.store.get(opened["id"])
        fingerprint = stored["receipts"]["round3-static-123"]["fingerprint"]
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        self.assertNotIn("steal", json.dumps(stored, ensure_ascii=False))
        self.assertNotIn("Synthetic second merchant", json.dumps(stored, ensure_ascii=False))

    async def test_restricted_text_and_date_fields_are_rejected_before_saving(self):
        for control, field in [("TextInput(\"note\")", "note"), ("DateInput(\"when\")", "when")]:
            source = f'input = {control}\nbutton = Button("Go", {{type: "go", fields: ["{field}"]}})\nroot = Card([Form("f", [input, button])])'
            result = json.loads(self.service.tool_handler(self.restricted_request(source, [{"key": "go", "label": "Go", "fields": [field]}], name="reject-" + field)))
            self.assertFalse(result["success"])
            self.assertIn("Restricted views cannot submit", result["error"])

    async def test_restricted_dynamic_values_require_the_latest_hydration(self):
        source = 'box = CheckboxGroup("ids", {{queue.rows|items:transaction_id,merchant}})\nbutton = Button("Go", {type: "go", fields: ["ids"]})\nroot = Card([Form("f", [box, button])])'
        saved = json.loads(self.service.tool_handler(self.restricted_request(source, [{"key": "go", "label": "Go", "fields": ["ids"]}], name="dynamic")))
        self.assertTrue(saved["success"], saved)
        opened = json.loads(self.service.tool_handler({"action": "open_saved", "name": "dynamic", "expires_in": 60}))
        headers = {"Authorization": "tma " + signed(opened["id"])}
        rejected = await self.client.post(
            "/api/action/" + opened["id"], headers=headers,
            json={"key": "go", "value": {"form": {"ids": ["txn-1"]}}, "request_id": "before-hydration", "revision": 1},
        )
        self.assertEqual(rejected.status, 409)
        self.assertEqual((await self.client.get("/api/spec/" + opened["id"], headers=headers)).status, 200)
        accepted = await self.client.post(
            "/api/action/" + opened["id"], headers=headers,
            json={"key": "go", "value": {"form": {"ids": ["txn-1"]}}, "request_id": "after-hydration", "revision": 1},
        )
        self.assertEqual(accepted.status, 200, await accepted.text())
    async def test_restricted_view_rejects_non_owner_init_data(self):
        source = 'box = CheckboxGroup("ids", {{queue.rows|items:transaction_id,merchant}})\nroot = Card([box])'
        saved = json.loads(self.service.tool_handler(self.restricted_request(source, [], name="owner-auth")))
        self.assertTrue(saved["success"], saved)
        opened = json.loads(self.service.tool_handler({"action": "open_saved", "name": "owner-auth", "expires_in": 60}))
        response = await self.client.get(
            "/api/spec/" + opened["id"],
            headers={"Authorization": "tma " + signed(opened["id"], user=43)},
        )
        self.assertEqual(response.status, 401, await response.text())


class OwnerBoundaryTests(unittest.TestCase):
    def service_for(self, target, env=None):
        values = {
            "HTE_MINIAPP_PUBLIC_URL": "https://ui.example.test/coordinator",
            "HTE_MINIAPP_PORT": "8787",
            "HTE_MINIAPP_PROFILE": "coordinator",
            "HTE_MINIAPP_OWNER_USER_ID": "42",
        }
        values.update(env or {})
        tmp = tempfile.TemporaryDirectory()
        service = setup(None, lambda: TOKEN, lambda _payload: True, state_dir=tmp.name,
                        current_target=lambda: (target, None), send_message=lambda _payload: {"ok": True, "result": {"message_id": 1}},
                        env=values, registry=synthetic_registry())
        service._serving = True
        return tmp, service

    @staticmethod
    def restricted_send() -> dict:
        return {
            "action": "send", "title": "Restricted", "renderer": "openui",
            "openui": 'root = Card([Text({{queue.rows|pluck:merchant}})])',
            "sources": [{"alias": "queue", "name": "finance.queue", "args": {}}], "actions": [],
        }

    def test_restricted_save_and_launch_reject_guest_webhook_group_and_non_owner(self):
        cases = {
            "guest": dict(TARGET, thread_id="99", origin="telegram", guest_mode=True),
            "webhook": dict(TARGET, origin="webhook"),
            "group": dict(TARGET, chat_id="-100", chat_type="group", origin="telegram"),
            "non_owner": dict(TARGET, chat_id="43", user_id="43", thread_id="", origin="telegram"),
        }
        for label, target in cases.items():
            with self.subTest(label=label):
                tmp, service = self.service_for(target)
                try:
                    save = dict(self.restricted_send(), action="save", name="blocked")
                    self.assertFalse(json.loads(service.tool_handler(save))["success"])
                    self.assertFalse(json.loads(service.tool_handler(self.restricted_send()))["success"])
                    template = service._template_from_args(self.restricted_send())
                    service.saved_views.save("preexisting", template)
                    reopened = json.loads(service.tool_handler({"action": "open_saved", "name": "preexisting", "expires_in": 60}))
                    self.assertFalse(reopened["success"], reopened)
                finally:
                    tmp.cleanup()

    def test_unconfigured_or_ambiguous_owner_fails_closed_and_saved_view_can_be_deleted(self):
        tmp, service = self.service_for(dict(TARGET, thread_id="", origin="telegram"), {"HTE_MINIAPP_OWNER_USER_ID": "", "TELEGRAM_ALLOWED_USERS": "42,43"})
        try:
            self.assertFalse(json.loads(service.tool_handler(dict(self.restricted_send(), action="save", name="ambiguous")))["success"])
        finally:
            tmp.cleanup()
        tmp, service = self.service_for(
            dict(TARGET, thread_id="", origin="telegram"),
            {"HTE_MINIAPP_OWNER_USER_ID": "", "TELEGRAM_ALLOWED_USERS": "42"},
        )
        try:
            self.assertTrue(json.loads(service.tool_handler(dict(self.restricted_send(), action="save", name="fallback")))["success"])
        finally:
            tmp.cleanup()
        tmp, service = self.service_for(dict(TARGET, thread_id="", origin="telegram"))
        try:
            public = {"action": "save", "name": "deletable", "title": "Legacy", "spec": {"version": 1, "components": [{"type": "text", "text": "x"}]}}
            self.assertTrue(json.loads(service.tool_handler(public))["success"])
            deleted = json.loads(service.tool_handler({"action": "delete_saved", "name": "deletable"}))
            self.assertTrue(deleted["success"], deleted)
            self.assertEqual(json.loads(service.tool_handler({"action": "list_saved"}))["saved_views"], [])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
