import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hte_core", ROOT / "core.py")
core = importlib.util.module_from_spec(spec)
sys.modules["hte_core"] = core
spec.loader.exec_module(core)


class ButtonTests(unittest.TestCase):
    def test_flat_list_is_one_row_and_choice_indices(self):
        rows = core.normalize_buttons([
            {"label": "A"}, {"label": "Docs", "type": "url", "url": "https://t.me"},
            {"label": "B", "value": "b", "style": "success"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(core.choice_list(rows), [{"label": "A", "value": "A"}, {"label": "B", "value": "b"}])
        md = core.compose_markdown("hi", rows, "0123456789")
        self.assertIn('data="rb:0123456789:0"', md)
        self.assertIn('data="rb:0123456789:1"', md)
        self.assertIn('style="success"', md)

    def test_escaping_and_copy(self):
        rows = core.normalize_buttons([[{"label": "<x>", "type": "copy", "copy_text": 'a"b<c>'}]])
        md = core.render_rows(rows, None)
        self.assertIn('text="a&quot;b&lt;c&gt;"', md)
        self.assertIn(">&lt;x&gt;</tg-button>", md)

    def test_invalid_specs(self):
        for bad in ([{"label": ""}], [{"label": "x", "type": "url", "url": "javascript:1"}],
                    [{"label": "x", "type": "nope"}], [{"label": "x", "style": "pink"}], "row"):
            with self.assertRaises(core.ButtonSpecError):
                core.normalize_buttons(bad)

    def test_render_without_choices_keeps_links(self):
        rows = core.normalize_buttons([[{"label": "A"}], [{"label": "L", "type": "url", "url": "https://x.y"}]])
        md = core.compose_markdown("b", rows, None, include_choices=False, footer="> done")
        self.assertNotIn("callback_data", md)
        self.assertIn("https://x.y", md)

    def test_callback_roundtrip(self):
        self.assertEqual(core.parse_callback_data(core.callback_data("abcdef0123", 7)), ("abcdef0123", 7))
        self.assertIsNone(core.parse_callback_data("cl:1:2"))


class TimeTests(unittest.TestCase):
    def test_placeholder_to_entity(self):
        out = core.expand_time_placeholders("起飞 {{time:2026-11-08T09:30|America/Chicago}} 准时")
        # 09:30 CST (UTC-6) = 15:30 UTC
        self.assertIn("tg://time?unix=1794151800&format=wDT", out)
        self.assertIn("11/8 周日 09:30 CST", out)

    def test_format_and_offset(self):
        out = core.expand_time_placeholders("{{time:2026-11-08T09:30+09:00||t}}")
        self.assertIn("format=t)", out)
        self.assertIn("unix=1794097800", out)
        with self.assertRaises(core.ButtonSpecError):
            core.expand_time_placeholders("{{time:2026-11-08T09:30|America/Chicago|xyz}}")
        with self.assertRaises(core.ButtonSpecError):
            core.expand_time_placeholders("{{time:tomorrow}}")


class MediaTests(unittest.TestCase):
    def test_map_and_gallery(self):
        md = core.compose_markdown("x", [], None, maps=[{"lat": 32.98, "long": -96.75, "zoom": 30, "caption": "UTD"}],
                                   gallery={"type": "slideshow", "items": [{"url": "https://a.b/c.jpg", "caption": 'q"t'}]})
        self.assertIn('<tg-map lat="32.980000" long="-96.750000" zoom="20"/>', md)
        self.assertIn("<tg-slideshow>", md)
        self.assertIn("![](https://a.b/c.jpg \"q't\")", md)

    def test_bad_media(self):
        for kw in ({"maps": [{"lat": 99, "long": 0}]}, {"maps": [{"lat": "x"}]},
                   {"gallery": {"items": [{"url": "file:///etc/passwd"}]}}, {"gallery": {"type": "grid", "items": [1]}}):
            with self.assertRaises(core.ButtonSpecError):
                core.compose_markdown("x", [], None, **kw)

    def test_card_key(self):
        self.assertEqual(core.card_key("1", "2", "Trip Tokyo!"), "1:2:trip-tokyo")
        self.assertEqual(core.card_key("1", None, "a"), "1:-:a")
        with self.assertRaises(core.ButtonSpecError):
            core.card_key("1", None, "!!!")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.PendingStore(Path(self.tmp.name) / "s")
        self.entry = {"id": "abcdef0123", "created_at": 1000.0, "session_key": "k", "user_id": "42",
                      "choices": [{"label": "A", "value": "a"}], "single_use": True, "answered": None}
        self.store.put(dict(self.entry), now=1000.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_auth_single_use_and_expiry(self):
        self.assertEqual(self.store.claim("abcdef0123", 0, "7", now=1001)[0], "unauthorized")
        self.assertEqual(self.store.claim("abcdef0123", 5, "42", now=1001)[0], "invalid")
        status, entry, choice = self.store.claim("abcdef0123", 0, "42", now=1001)
        self.assertEqual((status, choice), ("ok", {"label": "A", "value": "a"}))
        self.assertEqual(self.store.claim("abcdef0123", 0, "42", now=1002)[0], "used")
        self.assertEqual(self.store.claim("missing000", 0, "42", now=1002)[0], "expired")
        late = 1000 + core.PENDING_TTL_SECONDS + 1
        self.store.put({**self.entry, "id": "bbbbbbbbbb", "created_at": 1000.0}, now=1000.0)
        self.assertEqual(self.store.claim("bbbbbbbbbb", 0, "42", now=late)[0], "expired")

    def test_file_mode_and_json_store(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        js = core.JsonStore(Path(self.tmp.name) / "s", "cards.json")
        js.set("k", {"message_id": 5})
        self.assertEqual(js.get("k"), {"message_id": 5})
        self.assertEqual(js.pop("k"), {"message_id": 5})
        self.assertIsNone(js.get("k"))



class BulletTests(unittest.TestCase):
    def test_plain_lists_become_bullets(self):
        src = "看这里:\n- **一**: a\n- 二\n  - 子项\n* 三\n**粗体** 不动\n---\n1. 编号不动"
        self.assertEqual(core.bulletize_plain(src),
                         "看这里:\n• **一**: a\n• 二\n  ◦ 子项\n• 三\n**粗体** 不动\n---\n1. 编号不动")

    def test_code_and_rich_untouched(self):
        code = "```\n- keep\n```\n- go"
        self.assertEqual(core.bulletize_plain(code), "```\n- keep\n```\n• go")
        for rich in ("- [ ] task\n- other", "| a | b |\n|---|---|\n| 1 | 2 |\n- x", "<details>\n- x\n</details>"):
            self.assertEqual(core.bulletize_plain(rich), rich)
        self.assertEqual(core.bulletize_plain("no list"), "no list")


class NativeListTests(unittest.TestCase):
    def test_list_detection(self):
        self.assertTrue(core.has_markdown_list("hi\n- a"))
        self.assertTrue(core.has_markdown_list("1. a\n2. b"))
        self.assertFalse(core.has_markdown_list("**bold** text\n---\nplain"))
        self.assertFalse(core.has_markdown_list("```\n- in code\n```"))

if __name__ == "__main__":
    unittest.main()
