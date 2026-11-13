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
    def test_options_become_choice_buttons(self):
        btns = fu.normalize_options([{"label": "现在推送", "value": "push now", "style": "primary"},
                                     "先不推", {"label": ""}, "a", "b", "c"])
        self.assertEqual([b["label"] for b in btns], ["现在推送", "先不推", "a", "b"])
        self.assertEqual(btns[0]["value"], "push now")
        self.assertIsNone(btns[1]["value"])
        self.assertTrue(all(b["type"] == "choice" for b in btns))
        self.assertEqual([len(r) for r in fu.option_rows(btns)], [2, 2])
        self.assertEqual([len(r) for r in fu.option_rows(btns[:3])], [2, 1])

    def test_markdown(self):
        self.assertEqual(fu.decision_markdown(" 推不推 ", "本地有 5 个提交"), "**📌 推不推**\n\n本地有 5 个提交")
        self.assertEqual(fu.decision_markdown("推不推"), "**📌 推不推**")
        with self.assertRaises(ValueError):
            fu.decision_markdown("  ")

    def test_track_find_untrack(self):
        items = fu.track([], {"id": "a1", "text": "Push plugin", "rid": "r1", "mid": 5})
        items = fu.track(items, {"id": "b2", "text": "File upstream issue", "rid": "r2", "mid": 6})
        self.assertEqual(fu.find_open(items, rid="r2")["id"], "b2")
        self.assertEqual(fu.find_open(items, text="push")["id"], "a1")
        self.assertIsNone(fu.find_open(items, item_id="zz"))
        self.assertEqual([i["id"] for i in fu.untrack(items, "a1")], ["b2"])


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


class ClarifyCardTest(unittest.TestCase):
    def test_labelled_buttons_keep_core_callback_data(self):
        md = fu.clarify_markdown("Pick one", ["A", "B & C"], "abc123", "✏️ Other")
        self.assertIn("**Pick one**", md)
        self.assertIn('style="primary" data="cl:abc123:0">A</tg-button>', md)
        self.assertIn('data="cl:abc123:1">B &amp; C</tg-button>', md)
        self.assertIn('data="cl:abc123:other">✏️ Other</tg-button>', md)
        self.assertNotIn("❓", md)  # core adds it when editing the answered card

    def test_short_labels_pair_up_long_labels_listed_in_body(self):
        short = fu.clarify_markdown("Q", ["一", "二", "三"], "x", "o")
        self.assertEqual(short.count("<tg-button-row>"), 3)  # [一 二] [三] [other]
        long_label = "x" * 60
        md = fu.clarify_markdown("Q", [long_label, "ok"], "x", "o")
        self.assertIn("1. " + long_label, md)  # full text in the body
        self.assertIn("…</tg-button>", md)
        self.assertEqual(md.count("<tg-button-row>"), 3)

    def test_recommended_suffix_dropped_from_label(self):
        md = fu.clarify_markdown("Q", ["Go (Recommended)", "Stop"], "x", "o")
        self.assertIn('style="primary" data="cl:x:0">Go</tg-button>', md)
