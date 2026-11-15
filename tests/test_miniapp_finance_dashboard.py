"""Synthetic end-to-end finance OpenUI dashboard regression coverage."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer

from miniapp import setup
from miniapp.genui import validate_bindings, validate_button_action_types, validate_openui_actions, validate_template
from miniapp.sources import DataSource, SourceRegistry
from test_miniapp import TARGET, TOKEN, signed


FINANCE_SOURCES_PATH_ENV = "HTE_FINANCE_SOURCES_PATH"


DASHBOARD = '''root = Card([header, tabs])
header = CardHeader("财务总览", "合成演示数据")
cash = Stat("可用现金", {{overview.data.cash|money}}, "含待入账", "success")
balance = Stat("信用卡余额", {{overview.data.card_balance|money:USD:2}}, "本期账单", "danger")
due = Stat("14 天内待还", {{overview.data.due_next_14d|money}}, "下个到期日", "warning")
alerts = Stat("待确认", {{overview.data.alerts}}, "需要复核", "warning")
metrics = Grid([cash, balance, due, alerts], 2)
sync = Text({{overview.data.last_sync_local|date:MM/DD}}, "small")
cardsList = List({{cards.rows|list:card,due@date:MM/DD,statement_balance@money:USD:2,status@map:paid_full=已还清,unpaid=未还,paid_partial=部分还款,no_balance=无欠款,status@map:paid_full=success,unpaid=warning,paid_partial=warning,no_balance=secondary}})
spendingChart = BarChart({{spending.rows|pluck:key}}, {{spending.rows|pluck:amount}}, "近三月消费", "money")
categoriesTable = Table(["类别", "金额", "笔数"], {{categories.rows|table:category,amount@money:USD:2,count}})
reviewGroup = CheckboxGroup("transaction_ids", {{review_queue.rows|items:transaction_id,merchant,date|date,amount@money:USD:2,card}})
submitReview = Button("提交确认", {type: "submit_review", fields: ["transaction_ids"]})
reviewForm = Form("review", [reviewGroup, submitReview])
tabs = Tabs([
  {value: "overview", trigger: "概览", content: [metrics, sync]},
  {value: "cards", trigger: "卡款", content: [cardsList]},
  {value: "spending", trigger: "消费", content: [spendingChart, categoriesTable]},
  {value: "review", trigger: "待确认", content: [reviewForm]}
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
            "overall_status": "unpaid",
        }
    }
    cards = {
        "rows": [
            {
                "account_id": "acct-sapphire",
                "card": "Chase Sapphire Reserve ···4821",
                "due": "2026-11-21",
                "statement_balance": 1234.5,
                "purchase_apr": 24.9,
                "status": "unpaid",
            },
            {
                "account_id": "acct-freedom",
                "card": "Chase Freedom Unlimited ···9044",
                "due": "2026-11-26",
                "statement_balance": 208.1,
                "purchase_apr": 0.0,
                "status": "paid_partial",
            },
            {
                "account_id": "acct-apple",
                "card": "Apple Card Titanium ···1188",
                "due": "2026-12-01",
                "statement_balance": 0.0,
                "purchase_apr": 27.2,
                "status": "paid_full",
            },
        ]
    }
    spending = {
        "rows": [
            {"key": "Aug", "amount": 840.25, "count": 12},
            {"key": "Sep", "amount": 910.5, "count": 14},
            {"key": "Oct", "amount": 775.0, "count": 11},
        ]
    }
    categories = {
        "rows": [
            {"category": "餐饮与咖啡", "amount": 426.8, "count": 14},
            {"category": "出行与通勤", "amount": 218.4, "count": 8},
            {"category": "订阅服务", "amount": 96.0, "count": 4},
        ]
    }
    review_queue = {
        "rows": [
            {
                "transaction_id": "txn-002",
                "date": "2026-11-12",
                "merchant": "Cafe Nimbus · Wicker Park",
                "amount": 87.65,
                "card": "Chase Sapphire Reserve ···4821",
                "kind": "newmerchant",
            },
            {
                "transaction_id": "txn-004",
                "date": "2026-11-13",
                "merchant": "Northstar Mobility Services",
                "amount": 42.2,
                "card": "Chase Freedom Unlimited ···9044",
                "kind": "unusual_amount",
            },
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
                                "overall_status": {
                                    "type": "string",
                                    "enum": ["paid_full", "unpaid", "paid_partial", "no_balance"],
                                },
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
                                    "status": {
                                        "type": "string",
                                        "enum": ["paid_full", "unpaid", "paid_partial", "no_balance"],
                                    },
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
                "finance.categories",
                categories,
                {
                    "type": "object",
                    "properties": {
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "category": {"type": "string"},
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
                                    "transaction_id": {"type": "string", "x-opaque-id": True},
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
                    "alias": "categories",
                    "name": "finance.categories",
                    "args": {"days": 120, "limit": 5},
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
        self.assertIn('Stat("可用现金", "$2,450.50", "含待入账", "success")', hydrated)
        self.assertIn('Grid([cash, balance, due, alerts], 2)', hydrated)
        self.assertIn(
            '[{"title":"Chase Sapphire Reserve ···4821","subtitle":"11/21","trailing":"$1,234.50","badge":"未还","badgeTone":"warning"}',
            hydrated,
        )
        self.assertIn('BarChart(["Aug","Sep","Oct"], [840.25,910.5,775.0], "近三月消费", "money")', hydrated)
        self.assertIn('[["餐饮与咖啡","$426.80",14],["出行与通勤","$218.40",8],["订阅服务","$96.00",4]]', hydrated)
        self.assertIn('{"value":"txn-002","label":"Cafe Nimbus · Wicker Park","description":"11/12 · $87.65 · Chase Sapphire Reserve ···4821"}', hydrated)
        self.assertCountEqual(
            self.calls,
            [
                "finance.overview",
                "finance.cards",
                "finance.spending",
                "finance.categories",
                "finance.review_queue",
            ],
        )

        submitted = await self.client.post(
            "/api/action/" + opened["id"],
            headers=headers,
            json={
                "key": "submit_review",
                "value": {
                    "params": {"selection_labels": ["Cafe Nimbus · Wicker Park", "Northstar Mobility Services"]},
                    "form": {
                        "transaction_ids": ["txn-002", "txn-004"],
                        "merchant": "Cafe Nimbus · Wicker Park",
                        "field_labels": {"transaction_ids": "待确认交易"},
                    },
                    "form_name": "review",
                    "selection_labels": ["Cafe Nimbus · Wicker Park", "Northstar Mobility Services"],
                    "field_labels": {"transaction_ids": "待确认交易"},
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
                "value": {"form": {"transaction_ids": ["txn-002", "txn-004"]}},
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

    async def test_badge_tone_mapping_hydrates_a_schema_checked_enum_label(self):
        request = self.dashboard_request()
        request.update(
            {
                "name": "badge-tone-map",
                "openui": 'root = Card([badge])\nbadge = Badge({{overview.data.overall_status|map:paid_full=已还清,unpaid=未还,paid_partial=部分还款,no_balance=无欠款}}, {"已还清": "success", "未还": "warning", "部分还款": "warning", "无欠款": "secondary"})',
                "sources": [{"alias": "overview", "name": "finance.overview", "args": {}}],
                "actions": [],
            }
        )
        saved = json.loads(self.service.tool_handler(request))
        self.assertTrue(saved["success"], saved)
        opened = json.loads(
            self.service.tool_handler({"action": "open_saved", "name": "badge-tone-map", "expires_in": 60})
        )
        response = await self.client.get(
            "/api/spec/" + opened["id"], headers={"Authorization": "tma " + signed(opened["id"])}
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertIn(
            'Badge("未还", {"已还清": "success", "未还": "warning", "部分还款": "warning", "无欠款": "secondary"})',
            (await response.json())["openui"],
        )

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

    async def test_save_rejects_enum_map_that_does_not_exactly_match_the_source_schema(self):
        request = self.dashboard_request()
        request["name"] = "bad-status-map"
        request["openui"] = DASHBOARD.replace("no_balance=无欠款", "not_real=未知", 1)
        result = json.loads(self.service.tool_handler(request))
        self.assertFalse(result["success"])
        self.assertIn("map", result["error"])
        self.assertEqual(self.calls, [])
        catalog = json.loads(self.service.tool_handler({"action": "list_saved"}))
        self.assertEqual(catalog["saved_views"], [])
    def test_live_saved_finance_dashboard_and_docs_examples_validate_against_schema_only_manifest(self):
        """This reads no ledger and never invokes finance_sources.py query."""
        script_override = os.environ.get(FINANCE_SOURCES_PATH_ENV)
        saved_override = os.environ.get("HTE_MINIAPP_SAVED_VIEWS_PATH")
        if not script_override or not saved_override:
            self.skipTest(
                f"set {FINANCE_SOURCES_PATH_ENV} and HTE_MINIAPP_SAVED_VIEWS_PATH to validate the live saved dashboard"
            )
        script = Path(script_override)
        if not script.is_file():
            self.skipTest(f"finance provider script missing: {script}")
        manifest = json.loads(
            subprocess.check_output([sys.executable, "-I", str(script), "describe"], text=True)
        )
        calls: list[dict] = []
        registry = SourceRegistry(
            [
                DataSource(
                    name=entry["name"], title=entry["title"], args_schema=entry["args"],
                    output_schema=entry["output"], sensitivity="sensitive", profiles=("default", "coordinator"),
                    query=lambda args: calls.append(args) or {},
                )
                for entry in manifest["sources"]
            ]
        )

        saved_override = os.environ.get("HTE_MINIAPP_SAVED_VIEWS_PATH")
        saved_path = Path(saved_override) if saved_override else script.parents[2] / "telegram-enhanced" / "miniapp-saved-views.json"
        self.assertTrue(saved_path.is_file(), "the deployed finance-dashboard template must be present")
        saved = json.loads(saved_path.read_text())
        self.assertIn("finance-dashboard", saved)
        examples = [saved["finance-dashboard"]]

        docs = (Path(__file__).resolve().parents[1] / "miniapp" / "OPENUI.md").read_text(encoding="utf-8")
        for match in re.finditer(r"```json\s*\n(.*?)\n```", docs, re.DOTALL):
            examples.append(json.loads(match.group(1)))
        self.assertGreaterEqual(len(examples), 3)
        for view in examples:
            bindings = validate_bindings(view["sources"], registry, "coordinator")
            template = validate_template(view["openui"], bindings, registry, "coordinator")
            validate_button_action_types(template, validate_openui_actions(view.get("actions", view.get("openui_actions", []))))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
