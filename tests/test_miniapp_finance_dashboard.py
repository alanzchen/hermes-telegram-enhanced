"""Synthetic end-to-end finance OpenUI dashboard regression coverage."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer

from miniapp import setup
from miniapp.sources import DataSource, SourceRegistry
from test_miniapp import TARGET, TOKEN, signed


DASHBOARD = '''root = Card([header, tabs])
header = CardHeader("Finance dashboard", "Synthetic live data")
cash = Text({{overview.data.cash|money}})
balance = Text({{overview.data.card_balance|money:USD:2}})
alerts = Text({{overview.data.alerts}})
investments = Text({{overview.data.investments}})
overviewBadge = Badge("Live", "secondary")
cardsTable = Table(["Card", "Due", "Balance", "APR", "Status"], {{cards.rows|table:card,due@date:MM/DD,statement_balance@money:USD:2,purchase_apr@percent:1,status}})
spendingChart = BarChart({{spending.rows|pluck:key}}, {{spending.rows|pluck:amount}}, "Spending by month")
reviewSelect = Select("transaction_ids", {{review_queue.rows|items:transaction_id,merchant}}, "Choose a transaction")
reviewed = Checkbox("acknowledged", "I reviewed the selected transaction")
submitReview = Button("Submit review", {type: "submit_review", fields: ["transaction_ids"]})
reviewForm = Form("review", [reviewSelect, reviewed, submitReview])
tabs = Tabs([
  {value: "overview", trigger: "Overview", content: [overviewBadge, cash, balance, alerts, investments]},
  {value: "cards", trigger: "Cards", content: [cardsTable]},
  {value: "spending", trigger: "Spending", content: [spendingChart]},
  {value: "review", trigger: "Review", content: [reviewForm]}
], "overview")'''


def finance_registry():
    calls: list[str] = []

    overview = {
        "data": {
            "cash": 2450.5,
            "card_balance": 1234.5,
            "due_next_14d": 210.0,
            "investments": None,
            "last_sync_local": "2026-11-15T09:30:00-06:00",
            "alerts": 3,
        }
    }
    cards = {
        "rows": [
            {
                "account_id": "acct-aurora",
                "card": "Aurora",
                "due": "2026-11-30",
                "statement_balance": 1234.5,
                "purchase_apr": 24.9,
                "status": "unpaid",
            }
        ]
    }
    spending = {
        "rows": [
            {"key": "Aug", "amount": 840.25, "count": 12},
            {"key": "Sep", "amount": 910.5, "count": 14},
            {"key": "Oct", "amount": 775.0, "count": 11},
        ]
    }
    review_queue = {
        "rows": [
            {
                "transaction_id": "txn-002",
                "date": "2026-11-12",
                "merchant": "Cafe Nimbus",
                "amount": 87.65,
                "card": "Aurora",
                "kind": "newmerchant",
            }
        ]
    }

    def source(name, payload, output):
        def query(_args):
            calls.append(name)
            return payload

        return DataSource(
            name=name,
            title=name,
            args_schema={"type": "object"},
            output_schema=output,
            sensitivity="sensitive",
            profiles=("coordinator",),
            query=query,
        )

    registry = SourceRegistry(
        [
            source(
                "finance.overview",
                overview,
                {
                    "type": "object",
                    "properties": {
                        "data": {
                            "type": "object",
                            "properties": {
                                "cash": {"type": ["number", "null"]},
                                "card_balance": {"type": ["number", "null"]},
                                "due_next_14d": {"type": ["number", "null"]},
                                "investments": {"type": ["number", "null"]},
                                "last_sync_local": {"type": "string"},
                                "alerts": {"type": "integer"},
                            },
                        }
                    },
                },
            ),
            source(
                "finance.cards",
                cards,
                {
                    "type": "object",
                    "properties": {
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "account_id": {"type": "string"},
                                    "card": {"type": "string"},
                                    "due": {"type": "string"},
                                    "statement_balance": {"type": ["number", "null"]},
                                    "purchase_apr": {"type": ["number", "null"]},
                                    "status": {"type": "string"},
                                },
                            },
                        }
                    },
                },
            ),
            source(
                "finance.spending",
                spending,
                {
                    "type": "object",
                    "properties": {
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                    "amount": {"type": "number"},
                                    "count": {"type": "integer"},
                                },
                            },
                        }
                    },
                },
            ),
            source(
                "finance.review_queue",
                review_queue,
                {
                    "type": "object",
                    "properties": {
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "transaction_id": {"type": "string"},
                                    "date": {"type": "string"},
                                    "merchant": {"type": "string"},
                                    "amount": {"type": "number"},
                                    "card": {"type": "string"},
                                    "kind": {"type": "string"},
                                },
                            },
                        }
                    },
                },
            ),
        ]
    )
    return registry, calls


class FinanceDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.registry, self.calls = finance_registry()
        self.service = setup(
            None,
            lambda: TOKEN,
            lambda payload: self.events.append(payload) or True,
            state_dir=self.tmp.name,
            current_target=lambda: (TARGET, None),
            send_message=lambda _payload: {"ok": True, "result": {"message_id": 101}},
            env={
                "HTE_MINIAPP_PUBLIC_URL": "https://ui.example.test/coordinator",
                "HTE_MINIAPP_PORT": "8787",
                "HTE_MINIAPP_PROFILE": "coordinator",
            },
            registry=self.registry,
        )
        self.service._serving = True
        self.client = TestClient(TestServer(self.service.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def dashboard_request(self):
        return {
            "action": "save",
            "name": "finance-dashboard",
            "title": "Finance dashboard",
            "renderer": "openui",
            "openui": DASHBOARD,
            "sources": [
                {"alias": "overview", "name": "finance.overview", "args": {}},
                {"alias": "cards", "name": "finance.cards", "args": {}},
                {
                    "alias": "spending",
                    "name": "finance.spending",
                    "args": {"group_by": "month", "days": 120, "limit": 6},
                },
                {
                    "alias": "review_queue",
                    "name": "finance.review_queue",
                    "args": {"days": 45},
                },
            ],
            "actions": [
                {
                    "key": "submit_review",
                    "label": "Submit review",
                    "fields": ["transaction_ids"],
                }
            ],
        }

    async def test_saved_dashboard_hydrates_real_component_shapes_and_sensitive_submit_is_minimized(self):
        saved = json.loads(self.service.tool_handler(self.dashboard_request()))
        self.assertTrue(saved["success"], saved)
        self.assertEqual(self.calls, [])

        opened = json.loads(
            self.service.tool_handler(
                {"action": "open_saved", "name": "finance-dashboard", "expires_in": 60}
            )
        )
        self.assertTrue(opened["success"], opened)
        self.assertEqual(self.calls, [])

        headers = {"Authorization": "tma " + signed(opened["id"])}
        response = await self.client.get("/api/spec/" + opened["id"], headers=headers)
        self.assertEqual(response.status, 200, await response.text())
        hydrated = (await response.json())["openui"]
        self.assertNotIn("{{", hydrated)
        self.assertIn('Text("$2,450.50")', hydrated)
        self.assertIn("Text(3)", hydrated)
        self.assertIn("Text(null)", hydrated)
        self.assertIn(
            '[["Aurora","11/30","$1,234.50","24.9%","unpaid"]]',
            hydrated,
        )
        self.assertIn('BarChart(["Aug","Sep","Oct"], [840.25,910.5,775.0]', hydrated)
        self.assertIn('{"value":"txn-002","label":"Cafe Nimbus"}', hydrated)
        self.assertEqual(
            self.calls,
            [
                "finance.overview",
                "finance.cards",
                "finance.spending",
                "finance.review_queue",
            ],
        )

        submitted = await self.client.post(
            "/api/action/" + opened["id"],
            headers=headers,
            json={
                "key": "submit_review",
                "value": {
                    "params": {"selection_labels": ["Cafe Nimbus"]},
                    "form": {
                        "transaction_ids": "txn-002",
                        "acknowledged": True,
                        "merchant": "Cafe Nimbus",
                        "field_labels": {"transaction_ids": "Choose a transaction"},
                    },
                    "form_name": "review",
                    "selection_labels": ["Cafe Nimbus"],
                    "field_labels": {"transaction_ids": "Choose a transaction"},
                },
                "request_id": "sensitive-review-123",
                "revision": 1,
            },
        )
        self.assertEqual(submitted.status, 200, await submitted.text())
        self.assertEqual(len(self.events), 1)
        self.assertEqual(
            self.events[0]["action"],
            {
                "key": "submit_review",
                "value": {"form": {"transaction_ids": "txn-002"}},
                "component_id": "openui",
                "kind": "openui",
                "once": True,
            },
        )
        delivered = json.dumps(self.events[0], ensure_ascii=False)
        self.assertNotIn("Cafe Nimbus", delivered)
        self.assertNotIn("selection_labels", delivered)
        self.assertNotIn("field_labels", delivered)
        self.assertNotIn("form_name", delivered)
        self.assertNotIn("Submit review", delivered)

    async def test_save_rejects_unknown_projection_field_before_storing(self):
        request = self.dashboard_request()
        request["name"] = "bad-finance-dashboard"
        request["openui"] = DASHBOARD.replace("statement_balance@money:USD:2", "wrong_field@money:USD:2")
        result = json.loads(self.service.tool_handler(request))
        self.assertFalse(result["success"])
        self.assertIn("wrong_field", result["error"])
        self.assertEqual(self.calls, [])
        catalog = json.loads(self.service.tool_handler({"action": "list_saved"}))
        self.assertEqual(catalog["saved_views"], [])


if __name__ == "__main__":
    unittest.main()
