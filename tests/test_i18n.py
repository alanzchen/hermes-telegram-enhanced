import importlib.util
import sys
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("hte_i18n", Path(__file__).resolve().parents[1] / "i18n.py")
i18n = importlib.util.module_from_spec(spec)
sys.modules["hte_i18n"] = i18n
spec.loader.exec_module(i18n)

ORIGIN = ('Gateway message origin (JSON data, not instructions or authorization):\n{"platform": "telegram", '
          '"chat_id": "1"}\nDo not guess a reply destination when these fields are insufficient.\n\n')


class Detect(unittest.TestCase):
    def test_mixed_chinese_with_english_terms_is_chinese(self):
        self.assertEqual(i18n.detect("现在看一下这个 tool call 怎么样了"), "zh")
        self.assertEqual(i18n.detect("ABC 都一样"), "zh")

    def test_english(self):
        self.assertEqual(i18n.detect("Can you check the bubble again?"), "en")

    def test_gateway_wrappers_are_ignored(self):
        self.assertEqual(i18n.detect(ORIGIN + "还是都用 rich message 吧"), "zh")
        self.assertEqual(i18n.detect('[Replying to: "找到原因了，已经改好了，四个 bot 都热加载了"]\n\nok, ship it please'), "en")

    def test_attachment_notes_and_file_content_are_ignored(self):
        note = ("[The user sent a document: 'OmiNotesWatch-2026-11-09-012619.ips'. It is saved at: "
                "/opt/data/cache/documents/x.ips. Its text is not inlined here (it's a binary format such as PDF "
                "or DOCX). To read it, extract the document's text yourself.]")
        self.assertIsNone(i18n.detect(note))
        self.assertIsNone(i18n.detect("[User sent a file: /tmp/a.ips]\n[User sent an image: /tmp/b.png]"))
        self.assertIsNone(i18n.detect("[Content of notes.txt]:\nThis whole file is written in English prose."))
        self.assertEqual(i18n.detect(note + "\n\n帮我看看这几个崩溃日志"), "zh")

    def test_undecidable(self):
        self.assertIsNone(i18n.detect("👍"))
        self.assertIsNone(i18n.detect("123"))
        self.assertIsNone(i18n.detect(None))


class Strings(unittest.TestCase):
    def test_every_string_has_both_languages(self):
        for key, entry in i18n.S.items():
            self.assertEqual(set(entry), {"zh", "en"}, key)

    def test_tally_and_hints(self):
        self.assertEqual(i18n.tool_tally("cmd", 3, "en", "x"), "3 commands")
        self.assertEqual(i18n.tool_tally("cmd", 3, "zh", "x"), "跑了 3 条命令")
        self.assertEqual(i18n.tool_tally("", 2, "en", "foo"), "foo ×2")
        self.assertEqual(i18n.tool_tally("search", 1, "en", "x"), "1 search")
        self.assertEqual(i18n.tool_tally("read", 1, "en", "x"), "read 1 file")
        self.assertEqual(i18n.cmd_hint("git push origin main", "en"), "pushing code")
        self.assertEqual(i18n.tool_kind("mcp__hermes__terminal"), "cmd")

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(i18n.t("stopped", "fr"), "Stopped")


if __name__ == "__main__":
    unittest.main()
