import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hte_widget", ROOT / "widget.py")
wg = importlib.util.module_from_spec(_spec)
sys.modules["hte_widget"] = wg
_spec.loader.exec_module(wg)


def load_plugin():
    spec = importlib.util.spec_from_file_location("hte_pkg_widget", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hte_pkg_widget"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


class Helpers(unittest.TestCase):
    def test_actions_keys_and_limits(self):
        acts = wg.normalize_actions([{"key": "push", "label": "现在推送", "value": "push now"}, "先放着"])
        self.assertEqual([a["key"] for a in acts], ["push", "o2"])
        with self.assertRaises(wg.WidgetError):
            wg.normalize_actions([{"key": "bad key", "label": "x"}])
        with self.assertRaises(wg.WidgetError):
            wg.normalize_actions([{"key": "a", "label": "x"}, {"key": "a", "label": "y"}])
        with self.assertRaises(wg.WidgetError):
            wg.normalize_actions([str(i) for i in range(7)])

    def test_auto_buttons_and_runtime(self):
        acts = wg.normalize_actions([{"key": "go", "label": "<Go>"}])
        doc = wg.build_html("<p>hi</p>", acts)
        self.assertIn('<button data-action="go">&lt;Go&gt;</button>', doc)
        self.assertIn('var K=["go"]', doc)
        self.assertIn("W.request('a:'+k)", doc)

    def test_own_buttons_must_cover_every_action(self):
        acts = wg.normalize_actions([{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
        doc = wg.build_html('<button data-action="a">A</button><button data-action="b">B</button>', acts)
        self.assertNotIn('margin:10px 0 18px', doc)  # no auto row
        with self.assertRaises(wg.WidgetError):
            wg.build_html('<button data-action="a">A</button>', acts)

    def test_no_actions_no_runtime(self):
        self.assertEqual(wg.build_html("<p>x</p>", []), "<p>x</p>")
        with self.assertRaises(wg.WidgetError):
            wg.build_html("  ", [])

    def test_size_matches_server_counting(self):
        doc = wg.build_html("<p>" + "日" * 50 + "</p>", wg.normalize_actions(["a", "b"]))
        raw, packed = wg.check_size(doc)
        self.assertEqual(packed, len(zlib.compress(doc.encode(), 9)))
        self.assertLess(packed, 1500)
        noisy = os.urandom(6000).hex()
        with self.assertRaises(wg.WidgetError):
            wg.check_size(noisy)

    def test_chat_data(self):
        self.assertEqual(wg.chat_data_obj('{"a":1}'), {"a": 1})
        self.assertEqual(wg.chat_data_obj(None), {})
        with self.assertRaises(wg.WidgetError):
            wg.chat_data_obj("[1]")
        with self.assertRaises(wg.WidgetError):
            wg.encode_data({"x": "y" * 1100})

    def test_parse_and_claim(self):
        self.assertEqual(wg.parse_query("a:push"), "push")
        self.assertIsNone(wg.parse_query("a:"))
        self.assertIsNone(wg.parse_query("rb:x:1"))
        rec = {"user_id": "1", "actions": [{"key": "k", "label": "K", "value": ""}], "once": True, "chosen": None}
        self.assertEqual(wg.claim(None, "k", "1")[0], "gone")
        self.assertEqual(wg.claim(rec, "k", "2")[0], "not_yours")
        self.assertEqual(wg.claim(rec, "zz", "1")[0], "invalid")
        self.assertEqual(wg.claim(rec, "k", "1")[0], "ok")
        self.assertEqual(wg.claim(rec, "k", "1")[0], "used")
        rec2 = dict(rec, once=False, chosen="k")
        self.assertEqual(wg.claim(rec2, "k", "1")[0], "ok")


class PluginFlow(unittest.TestCase):
    def setUp(self):
        self.p = load_plugin()
        self.tmp = tempfile.TemporaryDirectory()
        self.p._state["widgets"] = self.p.JsonStore(Path(self.tmp.name), "widgets.json")
        self.calls = []

        def fake_api(method, payload):
            self.calls.append((method, payload))
            if method == "sendWidget":
                return {"ok": True, "result": {"message_id": 777}}
            return {"ok": True, "result": True}

        self.injected = []
        ctx = mock.Mock()
        ctx.inject_message.side_effect = lambda text, session_key: self.injected.append((text, session_key)) or True
        self.p._state["ctx"] = ctx
        self.patches = [mock.patch.object(self.p, "_api", fake_api),
                        mock.patch.object(self.p, "_spawn", lambda fn, *a, **k: fn(*a)),
                        mock.patch.dict(os.environ, {
                            "HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "100",
                            "HERMES_SESSION_THREAD_ID": "55", "HERMES_SESSION_KEY": "agent:main:telegram:dm:100:55",
                            "HERMES_SESSION_USER_ID": "100", "HERMES_SESSION_CHAT_TYPE": "dm"})]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in self.patches:
            pt.stop()
        self.tmp.cleanup()

    def send(self, **kw):
        args = {"action": "send", "html": "<h3>推送插件？</h3>", "title": "要不要推送",
                "actions": [{"key": "push", "label": "现在推送", "value": "push the plugin now"},
                            {"key": "later", "label": "先放着"}]}
        args.update(kw)
        return json.loads(self.p.telegram_widget(args))

    def query(self, data, user=100, qid="q1"):
        self.p._on_widget_query({"id": qid, "from": {"id": user, "first_name": "Alan"},
                                 "chat": {"id": 100, "type": "private"}, "message_id": 777, "data": data})

    def answers(self):
        return [json.loads(p["data"]) for m, p in self.calls if m == "answerWidgetQuery"]

    def test_send_tap_lock(self):
        res = self.send(chat_data={"step": 1})
        self.assertTrue(res["success"], res)
        method, payload = self.calls[0]
        self.assertEqual(method, "sendWidget")
        self.assertEqual(payload["message_thread_id"], 55)
        self.assertTrue(payload["allow_queries"])
        self.assertEqual(json.loads(payload["chat_data"]), {"step": 1})
        self.assertIn('data-action="push"', payload["html"])

        self.query("a:push", user=999)  # someone else
        self.assertEqual(self.answers()[-1], {"ok": 0, "e": "not_yours"})
        self.assertEqual(self.injected, [])

        self.query("a:push", qid="q2")
        self.assertEqual(self.answers()[-1], {"ok": 1, "again": 0})
        edits = [p for m, p in self.calls if m == "editMessageWidgetData"]
        self.assertEqual(json.loads(edits[-1]["chat_data"]), {"step": 1, "chosen": "push"})
        text, key = self.injected[-1]
        self.assertIn("[Telegram 小组件] Alan 点击了「现在推送」", text)
        self.assertIn("value: push the plugin now", text)
        self.assertIn("问题: 要不要推送", text)
        self.assertEqual(key, "agent:main:telegram:dm:100:55")

        self.query("a:later", qid="q3")
        self.assertEqual(self.answers()[-1], {"ok": 0, "e": "used"})
        self.assertEqual(len(self.injected), 1)

        upd = json.loads(self.p.telegram_widget({"action": "update", "message_id": 777, "chat_data": {"step": 2}}))
        self.assertTrue(upd["success"], upd)
        self.assertEqual(json.loads(self.calls[-1][1]["chat_data"]), {"step": 2, "chosen": "push"})

    def test_multi_use(self):
        self.send(once=False)
        self.query("a:push", qid="q1")
        self.query("a:later", qid="q2")
        self.assertEqual(self.answers(), [{"ok": 1, "again": 1}, {"ok": 1, "again": 1}])
        self.assertEqual(len(self.injected), 2)
        self.assertFalse([1 for m, _ in self.calls if m == "editMessageWidgetData"])

    def test_send_from_template(self):
        with mock.patch.object(self.p, "_template_dirs", lambda: [ROOT / "templates", Path(self.tmp.name)]):
            res = json.loads(self.p.telegram_widget({"action": "send", "template": "compare"}))
            self.assertFalse(res["success"])  # needs chat_data
            self.assertIn("example", res)
            data = res["example"]
            res = json.loads(self.p.telegram_widget({"action": "send", "template": "compare", "chat_data": data,
                                                     "title": "选哪个"}))
            self.assertTrue(res["success"], res)
            payload = self.calls[-1][1]
            self.assertTrue(payload["allow_queries"])
            self.assertEqual(json.loads(payload["chat_data"]), data)
            self.query("a:b", qid="t1")
            self.assertEqual(self.answers()[-1], {"ok": 1, "again": 0})
            self.assertIn("「方案 B」", self.injected[-1][0])
            self.assertIn("value: 选方案 B", self.injected[-1][0])
            lst = json.loads(self.p.telegram_widget({"action": "templates"}))
            self.assertIn("tracker", [t["name"] for t in lst["templates"]])
            saved = json.loads(self.p.telegram_widget({"action": "save_template", "name": "note",
                                                       "html": "<p>hi</p>", "desc": "d"}))
            self.assertEqual(saved, {"success": True, "saved": "note"})
            self.assertTrue((Path(self.tmp.name) / "note.html").exists())

    def test_rejects_before_any_api_call(self):
        res = json.loads(self.p.telegram_widget({"action": "send", "html": os.urandom(6000).hex()}))
        self.assertFalse(res["success"])
        self.assertIn("compresses", res["error"])
        self.assertEqual(self.calls, [])

    def test_router_hands_widget_queries_off(self):
        seen = []
        upd = mock.Mock(message=None, callback_query=None,
                        api_kwargs={"widget_query": {"id": "q", "data": "a:x"}})
        with mock.patch.object(self.p, "_on_widget_query", seen.append):
            asyncio.run(self.p._route_new_updates(upd, None))
        self.assertEqual(seen, [{"id": "q", "data": "a:x"}])

    def test_allowed_updates_extended_in_place(self):
        fake = mock.Mock()
        fake.Update.ALL_TYPES = ["message", "callback_query"]
        held = fake.Update.ALL_TYPES  # what a running poller captured
        with mock.patch.dict(sys.modules, {"telegram": fake}):
            self.p._extend_allowed_updates()
        self.assertIs(fake.Update.ALL_TYPES, held)
        self.assertIn("widget_query", held)


class Templates(unittest.TestCase):
    def test_builtins_parse_fit_and_list(self):
        dirs = [ROOT / "templates"]
        names = {t["name"] for t in wg.list_templates(dirs)}
        self.assertTrue({"weather", "tracker", "compare", "stack"} <= names)
        for name in names:
            meta, body = wg.load_template(dirs, name)
            self.assertNotIn("<!--hte", body)
            self.assertTrue(meta.get("desc"), name)
            acts = wg.actions_from_data(meta.get("example") or {}, meta.get("actions_from", ""))
            raw, packed = wg.check_size(wg.build_html(body, acts))
            self.assertLess(packed, 3800, name)  # headroom under the 4096-byte server limit
            if meta.get("example"):
                self.assertLessEqual(len(wg.encode_data(meta["example"]).encode()), wg.MAX_DATA, name)

    def test_compare_taps_come_from_data(self):
        meta, body = wg.load_template([ROOT / "templates"], "compare")
        acts = wg.actions_from_data(meta["example"], meta["actions_from"])
        self.assertEqual([a["key"] for a in acts], ["a", "b"])
        doc = wg.build_html(body, acts)  # buttons are drawn by the template's script: no auto row
        self.assertNotIn("margin:10px 0 18px", doc)
        self.assertIn('var K=["a","b"]', doc)

    def test_save_overrides_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = [ROOT / "templates", Path(tmp)]
            wg.save_template(tmp, "weather", "<p id=x></p>", "mine", {"a": 1})
            meta, body = wg.load_template(dirs, "weather")
            self.assertEqual((meta["desc"], meta["example"], body.strip()), ("mine", {"a": 1}, "<p id=x></p>"))
            with self.assertRaises(wg.WidgetError):
                wg.save_template(tmp, "Bad Name", "<p></p>")
            with self.assertRaises(wg.WidgetError):
                wg.save_template(tmp, "big", os.urandom(6000).hex())
            with self.assertRaises(wg.WidgetError):
                wg.load_template(dirs, "nope")


class Adoption(unittest.TestCase):
    def test_only_gateway_turns_are_adopted(self):
        p = load_plugin()
        sid = "20261110_210247_73052a"
        self.assertTrue(p._is_gateway_turn(sid, f"{sid}:{sid}:5f8e03e9"))
        # the post-reply background review runs in the same session with a random task id
        self.assertFalse(p._is_gateway_turn(sid, f"{sid}:bf56d1e5-05b7-4f69-9038-63371498320d:5ee92da7"))
        self.assertFalse(p._is_gateway_turn(sid, "other:other:1"))
        self.assertFalse(p._is_gateway_turn(sid, ""))
        p._state["loaded_at"] = __import__("time").time()
        self.assertIsNone(p._adopt_turn(sid, f"{sid}:bf56d1e5-05b7-4f69-9038-63371498320d:5ee92da7"))


if __name__ == "__main__":
    unittest.main()
