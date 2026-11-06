import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("trb_core", ROOT / "core.py")
core = importlib.util.module_from_spec(spec)
sys.modules["trb_core"] = core
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
        self.assertIn('type="url"', md)

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
        self.assertIn("> done", md)

    def test_callback_roundtrip(self):
        self.assertEqual(core.parse_callback_data(core.callback_data("abcdef0123", 7)), ("abcdef0123", 7))
        self.assertIsNone(core.parse_callback_data("cl:1:2"))
        self.assertIsNone(core.parse_callback_data("rb:XYZ:1"))


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
        self.assertEqual(entry["session_key"], "k")
        self.assertEqual(self.store.claim("abcdef0123", 0, "42", now=1002)[0], "used")
        self.assertEqual(self.store.claim("missing000", 0, "42", now=1002)[0], "expired")
        late = 1000 + core.PENDING_TTL_SECONDS + 1
        self.store.put({**self.entry, "id": "bbbbbbbbbb", "created_at": 1000.0}, now=1000.0)
        self.assertEqual(self.store.claim("bbbbbbbbbb", 0, "42", now=late)[0], "expired")

    def test_file_mode(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_injection_text(self):
        text = core.choice_injection_text({"id": "r1", "question": "Q?"}, {"label": "A", "value": "a"}, "Alan")
        self.assertIn("「A」", text)
        self.assertIn("value: a", text)
        self.assertIn("Q?", text)


if __name__ == "__main__":
    unittest.main()
