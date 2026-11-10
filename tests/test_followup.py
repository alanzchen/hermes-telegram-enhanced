import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_path = Path(__file__).resolve().parents[1] / "followup.py"
_spec = importlib.util.spec_from_file_location("hte_followup", _path)
fu = importlib.util.module_from_spec(_spec)
sys.modules["hte_followup"] = fu
_spec.loader.exec_module(fu)

TZ = ZoneInfo("America/Chicago")


def at(h, m=0, d=10):
    return datetime(2026, 11, d, h, m, tzinfo=TZ)


class Snooze(unittest.TestCase):
    def test_due_times(self):
        self.assertEqual(fu.snooze_due("1h", at(14)), at(15))
        self.assertEqual(fu.snooze_due("ev", at(14)), at(20))
        self.assertEqual(fu.snooze_due("ev", at(19, 45)), at(21, 45))  # too close: two hours later
        self.assertEqual(fu.snooze_due("am", at(14)), at(9, d=11))
        self.assertEqual(fu.snooze_due("am", at(2)), at(9))  # after midnight: this morning
        self.assertEqual(fu.snooze_due("am", at(23)), at(9, d=11))

    def test_no_tonight_late_at_night(self):
        self.assertEqual(fu.snooze_codes(at(14)), ["1h", "ev", "am"])
        self.assertEqual(fu.snooze_codes(at(22)), ["1h", "3h", "am"])
        self.assertEqual(fu.snooze_codes(at(1)), ["1h", "3h", "am"])

    def test_row_and_parse(self):
        row = fu.snooze_row(at(14), "zh")
        self.assertIn('data="hz:ev"', row)
        self.assertIn("今晚", row)
        self.assertEqual(fu.parse_snooze("hz:am"), "am")
        self.assertIsNone(fu.parse_snooze("hz:xx"))
        self.assertIsNone(fu.parse_snooze("rb:abc:1"))

    def test_when_text(self):
        self.assertEqual(fu.when_text(at(20), at(14), "zh"), "今天 20:00")
        self.assertEqual(fu.when_text(at(9, d=11), at(14), "en"), "tomorrow at 09:00")

    def test_due_and_prune(self):
        data = {"a": {"due": 100}, "b": {"due": 50}, "c": {"due": 999}}
        self.assertEqual([k for k, _ in fu.due_reminders(data, 200)], ["b", "a"])
        fu.prune_reminders(data, 50 + fu.REMINDER_TTL + 1)
        self.assertEqual(sorted(data), ["a", "c"])


class Decisions(unittest.TestCase):
    def test_add_dedupes_and_caps(self):
        items, a = fu.add_item([], "推送 Telegram 插件到 GitHub", 1.0)
        items, b = fu.add_item(items, "  推送 telegram 插件到 github ", 2.0)
        self.assertEqual(a, b)
        self.assertEqual(len(items), 1)
        for i in range(20):
            items, _ = fu.add_item(items, f"item {i}", 3.0 + i)
        self.assertEqual(len(items), fu.MAX_DECISIONS)
        with self.assertRaises(ValueError):
            fu.add_item(items, "   ", 1.0)

    def test_resolve_by_id_or_text(self):
        items, a = fu.add_item([], "Push plugin", 1.0)
        items, _ = fu.add_item(items, "File upstream issue", 2.0)
        items, ok = fu.resolve_item(items, item_id=a)
        self.assertTrue(ok)
        items, ok = fu.resolve_item(items, text="upstream")
        self.assertTrue(ok)
        self.assertEqual(items, [])
        self.assertFalse(fu.resolve_item(items, item_id="deadbeef")[1])

    def test_render_default_yes_no_and_jump(self):
        items, a = fu.add_item([], "要不要把插件推到 GitHub", 1762750000, msg=9570, sk="agent:x")
        md = fu.render_decisions(items, "zh", "America/Chicago")
        self.assertIn("待你决定 · 1", md)
        self.assertIn(f'data="hd:{a}:0">1 · 是<', md)
        self.assertIn(f'data="hd:{a}:1">1 · 否<', md)
        self.assertIn(f'data="hd:{a}:j">1 · ↗ 原消息<', md)
        self.assertEqual(fu.parse_decision(f"hd:{a}:1"), (a, "1"))
        self.assertEqual(fu.parse_decision(f"hd:{a}:j"), (a, "j"))
        self.assertIsNone(fu.parse_decision(f"hd:{a}"))

    def test_custom_options_and_no_jump_without_message(self):
        items, a = fu.add_item([], "Ship it?", 1.0, opts=["Push", "Roll back", "Later", "Extra"])
        md = fu.render_decisions(items, "en", "America/Chicago")
        self.assertIn(">1 · Roll back<", md)
        self.assertNotIn("Extra", md)
        self.assertNotIn(":j", md)
        items, b = fu.add_item(items, "ship IT?", 2.0, msg=5)  # same item, gains the message link
        self.assertEqual(a, b)
        self.assertEqual(fu.find_item(items, a)["msg"], 5)
        text = fu.decision_injection_text(fu.find_item(items, a), "Push", "Alan")
        self.assertIn("「Ship it?」选了「Push」", text)

    def test_model_chosen_actions_with_values(self):
        items, a = fu.add_item([], "插件推 GitHub", 1.0, opts=[
            {"label": "现在推送", "value": "push the plugin to GitHub now"}, {"label": "这批做完一起推"}, "先不推"])
        it = fu.find_item(items, a)
        self.assertEqual(it["opts"], ["现在推送", "这批做完一起推", "先不推"])
        self.assertEqual(fu.item_value(it, 0, "zh"), "push the plugin to GitHub now")
        self.assertEqual(fu.item_value(it, 2, "zh"), "先不推")
        text = fu.decision_injection_text(it, "现在推送", "Alan", fu.item_value(it, 0, "zh"))
        self.assertIn("value: push the plugin to GitHub now", text)
        md = fu.render_decisions(items, "zh", "America/Chicago")
        self.assertIn(">1 · 这批做完一起推<", md)
        self.assertNotIn(">1 · 是<", md)
        # re-adding the same question with new actions replaces them
        items, b = fu.add_item(items, "插件推 GitHub", 2.0, opts=["推", "不推"])
        self.assertEqual(a, b)
        self.assertEqual(fu.find_item(items, a)["opts"], ["推", "不推"])


class Wrapup(unittest.TestCase):
    def test_sections(self):
        md = fu.wrapup_markdown("文件合并上线", "zh", summary="一次发多个文件只开一轮",
                                changed=["插件加了合并"], verified=["模拟 8 种情况"], pending="真机没试")
        self.assertIn("**文件合并上线**", md)
        self.assertIn("**改了什么**\n- 插件加了合并", md)
        self.assertIn("**还没做 / 没验证**\n- 真机没试", md)
        self.assertNotIn("验证过的**\n\n", md)
        self.assertNotIn("Verified", fu.wrapup_markdown("x", "zh", verified=[]))


if __name__ == "__main__":
    unittest.main()
