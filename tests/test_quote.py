import importlib.util
import sys
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location("hte_pkg_quote", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hte_pkg_quote"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def message(quote, reply_id=42):
    return SimpleNamespace(quote=quote, reply_to_message=SimpleNamespace(message_id=reply_id))


class RichQuote(unittest.TestCase):
    def setUp(self):
        self.p = load_plugin()

        class Adapter:
            def _reply_context(self, msg):
                return str(msg.reply_to_message.message_id), "WHOLE MESSAGE"

        self.Adapter = Adapter
        with mock.patch.object(self.p, "_core_handles_rich_quotes", lambda: False):
            self.p._patch_quote(Adapter)

    def ctx(self, quote):
        return self.Adapter()._reply_context(message(quote))

    def test_selection_from_rich_message_is_used(self):
        q = SimpleNamespace(text="", position=19, api_kwargs=MappingProxyType({
            "rich_texts": [[{"type": "bold", "text": "Item B"}, ": rotate keys"]]}))
        self.assertEqual(self.ctx(q), ("42", "Item B: rotate keys"))

    def test_several_segments_join_with_newlines(self):
        q = SimpleNamespace(text="", rich_texts=["line one", [{"type": "italic", "text": "line"}, " two"]])
        self.assertEqual(self.ctx(q), ("42", "line one\nline two"))

    def test_empty_selection_does_not_leak_whole_message(self):
        q = SimpleNamespace(text="", api_kwargs={"rich_texts": []})
        self.assertEqual(self.ctx(q), ("42", None))

    def test_plain_quote_and_no_quote_use_core(self):
        self.assertEqual(self.ctx(SimpleNamespace(text="picked", api_kwargs={})), ("42", "WHOLE MESSAGE"))
        self.assertEqual(self.ctx(SimpleNamespace(text="", api_kwargs={})), ("42", "WHOLE MESSAGE"))
        self.assertEqual(self.ctx(None), ("42", "WHOLE MESSAGE"))

    def test_patch_steps_aside_when_core_has_the_fix(self):
        class Other:
            def _reply_context(self, msg):
                return None, None
        with mock.patch.object(self.p, "_core_handles_rich_quotes", lambda: True):
            self.p._patch_quote(Other)
        self.assertNotIn(self.p._QUOTE_MARK, Other.__dict__)

    def test_idempotent(self):
        before = self.Adapter._reply_context
        with mock.patch.object(self.p, "_core_handles_rich_quotes", lambda: False):
            self.p._patch_quote(self.Adapter)
        self.assertIs(self.Adapter._reply_context, before)


if __name__ == "__main__":
    unittest.main()
