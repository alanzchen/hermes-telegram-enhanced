"""Regression coverage for the Carbon OpenUI review; all registry values are synthetic."""
from __future__ import annotations

import unittest

from miniapp.genui import (
    _format_value,
    hydrate_openui,
    validate_bindings,
    validate_button_action_types,
    validate_template,
)
from miniapp.sources import DataSource, SourceRegistry
from miniapp.spec import SpecError


def registry() -> SourceRegistry:
    row = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "transaction_id": {"type": "string", "x-opaque-id": True},
            "account_id": {"type": "string", "x-opaque-id": True},
            "merchant": {"type": "string"},
            "amount": {"type": "number"},
            "date": {"type": ["string", "null"]},
        },
        "required": ["transaction_id", "account_id", "merchant", "amount", "date"],
    }
    return SourceRegistry(
        [
            DataSource(
                name="finance.overview",
                title="Overview",
                args_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"data": {"type": "object", "properties": {"cash": {"type": "number"}}}},
                    "required": ["data"],
                },
                sensitivity="sensitive",
                profiles=("coordinator",),
                query=lambda _args: {"data": {"cash": 42.5}},
            ),
            DataSource(
                name="finance.queue",
                title="Queue",
                args_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"rows": {"type": "array", "items": row}},
                    "required": ["rows"],
                },
                sensitivity="sensitive",
                profiles=("coordinator",),
                query=lambda _args: {
                    "rows": [
                        {
                            "transaction_id": "txn-1",
                            "account_id": "acct-1",
                            "merchant": "Synthetic merchant",
                            "amount": 42.5,
                            "date": "2026-11-15",
                        }
                    ]
                },
            ),
            DataSource(
                name="basecamp.projects",
                title="Projects",
                args_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"rows": {"type": "array", "items": row}},
                    "required": ["rows"],
                },
                sensitivity="private",
                profiles=("coordinator",),
                query=lambda _args: {"rows": []},
            ),
        ]
    )


def bindings(*items: tuple[str, str]):
    return validate_bindings(
        [{"alias": alias, "name": name, "args": {}} for alias, name in items],
        registry(),
        "coordinator",
    )


class CarbonOpenUIRegressionTests(unittest.TestCase):
    def assert_template_rejected(self, source: str, source_bindings) -> None:
        with self.assertRaises(SpecError):
            validate_template(source, source_bindings, registry(), "coordinator")

    def test_c1_checkbox_checked_cannot_hydrate_sensitive_values(self):
        self.assert_template_rejected(
            'root = Card([box])\nbox = CheckboxGroup("ack", [{value: "ok", label: "ok"}], [{{overview.data.cash|money}}])',
            bindings(("overview", "finance.overview")),
        )

    def test_h1_sensitive_items_value_must_be_an_opaque_id_without_formatter(self):
        self.assert_template_rejected(
            'root = Card([box])\nbox = CheckboxGroup("txs", {{queue.rows|items:merchant,merchant,amount@money}})',
            bindings(("queue", "finance.queue")),
        )

    def test_h2_placeholder_assignments_cannot_be_indirected_into_buttons(self):
        self.assert_template_rejected(
            'root = Card([btn])\nleak = {{projects.rows}}\nbtn = Button("Continue", {type: "go", params: {dump: leak}})',
            bindings(("projects", "basecamp.projects")),
        )

    def test_s1_comments_are_not_a_validator_bypass(self):
        self.assert_template_rejected(
            "root = Card([btn])\n// '\\nbtn = Button(\"Continue\", {type: \"go\"})\n// '",
            [],
        )

    def test_m3_accepts_nested_object_braces_and_spaced_projections(self):
        source = '''root = Card([table, btn])
table = Table(["Merchant", "Amount"], {{queue.rows|table: merchant, amount@money}})
btn = Button("Refresh", {type: "refresh", params: {tab: "cards"}})'''
        parsed = validate_template(source, bindings(("queue", "finance.queue")), registry(), "coordinator")
        validate_button_action_types(parsed, [{"key": "refresh", "label": "Refresh", "once": True}])

    def test_r3_5_interactive_controls_require_static_names_and_safe_items(self):
        controls = {
            "TextInput": 'field = "note"\nroot = Card([TextInput(field)])',
            "DateInput": 'field = "date"\nroot = Card([DateInput(field)])',
            "Checkbox": 'field = "approved"\nroot = Card([Checkbox(field, "Approved")])',
            "Select": 'field = "choice"\nroot = Card([Select(field, [{value: "one", label: "One"}])])',
            "CheckboxGroup": 'field = "choices"\nroot = Card([CheckboxGroup(field, [{value: "one", label: "One"}])])',
            "RadioGroup": 'field = "choice"\nroot = Card([RadioGroup(field, [{value: "one", label: "One"}])])',
        }
        for name, source in controls.items():
            with self.subTest(control=name):
                self.assert_template_rejected(source, [])
        for name in ("Select", "CheckboxGroup", "RadioGroup"):
            with self.subTest(control=name, items="identifier"):
                self.assert_template_rejected(
                    f'items = [{{value: "one", label: "One"}}]\nroot = Card([{name}("choice", items)])', []
                )
            with self.subTest(control=name, items="invalid static"):
                for items in ('["one"]', '[{value: "one"}]'):
                    with self.subTest(items=items):
                        self.assert_template_rejected(f'root = Card([{name}("choice", {items})])', [])
            with self.subTest(control=name, items="valid static"):
                validate_template(
                    f'root = Card([{name}("choice", [{{value: "one", label: "One"}}])])', [], registry(), "coordinator"
                )
            with self.subTest(control=name, items="valid placeholder"):
                validate_template(
                    f'root = Card([{name}("choice", {{{{queue.rows|items:transaction_id,merchant}}}})])',
                    bindings(("queue", "finance.queue")), registry(), "coordinator",
                )

    def test_m6_negative_money_percent_and_timezone_are_explicit(self):
        self.assertEqual(_format_value(-42.5, {"kind": "money", "precision": 2}), "-$42.50")
        self.assertEqual(_format_value(0.99, {"kind": "percent", "precision": 1}), "1.0%")
        self.assertEqual(_format_value("", {"kind": "date", "format": "MM/DD"}), "—")
        self.assertEqual(_format_value("2026-11-16T01:00:00Z", {"kind": "date", "format": "MM/DD"}), "11/15")

    def test_s3_hydration_escapes_javascript_line_separators(self):
        source = "root = Card([Text({{queue.rows|pluck:merchant}})])"
        hydrated = hydrate_openui(source, bindings(("queue", "finance.queue")), registry(), "coordinator")
        self.assertNotIn("{{", hydrated)
        self.assertNotIn("\u2028", hydrated)
        self.assertNotIn("\u2029", hydrated)


if __name__ == "__main__":
    unittest.main()
