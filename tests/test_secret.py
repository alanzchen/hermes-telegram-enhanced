import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hte_secret", ROOT / "secret.py")
sx = importlib.util.module_from_spec(_spec)
sys.modules["hte_secret"] = sx
_spec.loader.exec_module(sx)

SECRET = "hunter2-Ünïcode pass"


def load_plugin():
    spec = importlib.util.spec_from_file_location("hte_pkg_secret", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hte_pkg_secret"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


class Helpers(unittest.TestCase):
    def test_capture_value(self):
        self.assertEqual(sx.capture_value("  pw with space \n"), "pw with space")
        self.assertIsNone(sx.capture_value("/stop"))
        self.assertIsNone(sx.capture_value("   "))
        self.assertIsNone(sx.capture_value(None))
        self.assertTrue(sx.is_cancel_text(" 取消 "))

    def test_match_request(self):
        now = 1000.0
        reqs = {"a": {"id": "a", "state": "pending", "chat_id": "1", "thread_id": "5", "user_id": "1",
                      "created": 1, "expires_at": 2000},
                "b": {"id": "b", "state": "pending", "chat_id": "1", "thread_id": "5", "user_id": "1",
                      "created": 2, "expires_at": 2000}}
        self.assertEqual(sx.match_request(reqs, "1", "5", "1", now)["id"], "b")
        self.assertIsNone(sx.match_request(reqs, "1", "6", "1", now))  # other topic
        self.assertIsNone(sx.match_request(reqs, "1", "5", "2", now))  # other user
        self.assertIsNone(sx.match_request(reqs, "1", "5", "1", 3000))  # expired

    def test_op_item_and_redact(self):
        tpl = {"category": "LOGIN", "fields": [{"id": "username"}, {"id": "password", "type": "CONCEALED"},
                                                {"id": "notesPlain"}]}
        item = sx.op_item(tpl, "UPS", "pw", "alan", "https://ups.com", "n")
        self.assertEqual([f.get("value") for f in item["fields"]], ["alan", "pw", "n"])
        self.assertEqual(item["urls"][0]["href"], "https://ups.com")
        self.assertIn("hermes-secure-input", item["tags"])
        self.assertEqual(sx.redact("x pw y pw", "pw"), "x •••• y ••••")

    def test_card_has_cancel_button(self):
        req = {"id": "sx0123456789", "label": "UPS 密码", "kind": "password", "store": "1password",
               "created": 0, "expires_at": 600}
        md = sx.card_markdown(req, "zh", "Hermes")
        self.assertIn('data="hx:sx0123456789"', md)
        self.assertIn("1Password（Hermes vault）", md)
        self.assertEqual(sx.parse_cancel("hx:sx0123456789"), "sx0123456789")


class Flow(unittest.TestCase):
    def setUp(self):
        self.p = load_plugin()
        self.tmp = tempfile.TemporaryDirectory()
        self.p._state["secret_reqs"] = self.p.JsonStore(Path(self.tmp.name), "secrets.json")
        self.p._state["meta"] = self.p.JsonStore(Path(self.tmp.name), "meta.json")
        self.calls, self.injected, self.stored = [], [], []

        def fake_api(method, payload):
            self.calls.append((method, payload))
            if method == "sendRichMessage":
                return {"ok": True, "result": {"message_id": 900}}
            return {"ok": True, "result": True}

        ctx = mock.Mock()
        ctx.inject_message.side_effect = lambda text, session_key: self.injected.append(text) or True
        ctx.get_config.side_effect = lambda key, default=None: default
        self.p._state["ctx"] = ctx

        def fake_store(req, value):
            self.stored.append(value)
            return "op://Hermes/abc123/password", ""

        self.patches = [mock.patch.object(self.p, "_api", fake_api),
                        mock.patch.object(self.p, "_spawn", lambda fn, *a, **k: fn(*a)),
                        mock.patch.object(self.p, "_op_store", fake_store),
                        mock.patch.object(self.p, "_lang", lambda *a, **k: "zh"),
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

    def event(self, text, user="100", thread="55", mid=4242, mtype="text"):
        src = SimpleNamespace(platform=SimpleNamespace(value="telegram"), is_bot=False, chat_id="100",
                              thread_id=thread, user_id=user, message_id=mid)
        return SimpleNamespace(source=src, text=text, message_id=mid, message_type=SimpleNamespace(value=mtype))

    def request(self, **kw):
        args = {"action": "request", "label": "UPS 账号密码", "username": "alan", "url": "https://ups.com"}
        args.update(kw)
        return json.loads(self.p.telegram_secret(args))

    def disk(self):
        return (Path(self.tmp.name) / "secrets.json").read_text()

    def test_password_goes_to_1password_and_never_to_the_agent(self):
        res = self.request()
        self.assertTrue(res["success"], res)
        card = [p for m, p in self.calls if m == "sendRichMessage"][-1]
        self.assertEqual(card["message_thread_id"], 55)
        self.assertIn("hx:" + res["id"], card["rich_message"]["markdown"])

        out = self.p._on_pre_gateway_dispatch(self.event("  " + SECRET + "\n"))
        self.assertEqual(out["action"], "skip")
        self.assertEqual(self.stored, [SECRET])
        self.assertIn(("deleteMessage", {"chat_id": 100, "message_id": 4242}), self.calls)
        edits = [p for m, p in self.calls if m == "editMessageText"]
        self.assertIn("已存入 1Password", edits[-1]["rich_message"]["markdown"])
        self.assertEqual(len(self.injected), 1)
        self.assertIn("op://Hermes/abc123/password", self.injected[0])
        for text in self.injected + [json.dumps(c, ensure_ascii=False) for c in self.calls] + [self.disk()]:
            self.assertNotIn(SECRET, text)
        # the next message is ordinary again
        self.assertIsNone(self.p._on_pre_gateway_dispatch(self.event("谢谢")))

    def test_what_is_not_captured(self):
        self.request()
        self.assertIsNone(self.p._on_pre_gateway_dispatch(self.event("/stop")))
        self.assertIsNone(self.p._on_pre_gateway_dispatch(self.event(SECRET, user="999")))
        self.assertIsNone(self.p._on_pre_gateway_dispatch(self.event(SECRET, thread="77")))
        self.assertIsNone(self.p._on_pre_gateway_dispatch(self.event("voice", mtype="voice")))
        self.assertEqual(self.stored, [])

    def test_expiry_and_sweep(self):
        res = self.request(minutes=1)
        store = self.p._state["secret_reqs"]
        rec = store.get(res["id"])
        rec["expires_at"] = time.time() - 1
        store.set(res["id"], rec)
        self.assertIsNone(self.p._on_pre_gateway_dispatch(self.event(SECRET)))
        self.p._secret_sweep()
        self.assertEqual(store.get(res["id"])["state"], "expired")
        self.assertIn("已过期", [p for m, p in self.calls if m == "editMessageText"][-1]["rich_message"]["markdown"])

    def test_cancel_by_text_and_new_request_supersedes(self):
        first = self.request()
        second = self.request(label="第二个")
        self.assertEqual(self.p._state["secret_reqs"].get(first["id"])["state"], "cancelled")
        out = self.p._on_pre_gateway_dispatch(self.event("取消"))
        self.assertEqual(out["action"], "skip")
        self.assertEqual(self.p._state["secret_reqs"].get(second["id"])["state"], "cancelled")
        self.assertIn("取消了「第二个」", self.injected[-1])
        self.assertEqual(self.stored, [])

    def test_temporary_code_exec_redacts(self):
        res = self.request(label="UPS 验证码", kind="code")
        self.assertEqual(res["store"], "temp")
        self.p._on_pre_gateway_dispatch(self.event("482913"))
        self.assertEqual(self.stored, [])
        self.assertIn("只在内存里保留 15 分钟", self.injected[-1])
        self.assertNotIn("482913", self.injected[-1] + self.disk())
        out = json.loads(self.p.telegram_secret({"action": "exec", "id": res["id"],
                                                 "command": 'echo "code=$HERMES_SECRET"; printf %s "$HERMES_SECRET" | wc -c'}))
        self.assertTrue(out["success"], out)
        self.assertIn("code=••••", out["stdout"])
        self.assertIn("6", out["stdout"])
        self.assertNotIn("482913", out["stdout"])
        status = json.loads(self.p.telegram_secret({"action": "status", "id": res["id"]}))
        self.assertEqual(status["request"]["state"], "held")
        self.assertNotIn("482913", json.dumps(status))

    def test_capture_error_fails_closed(self):
        self.request()
        with mock.patch.object(self.p, "_secret_claim", side_effect=RuntimeError("boom")):
            out = self.p._on_pre_gateway_dispatch(self.event(SECRET))
        self.assertEqual(out["action"], "skip")
        self.assertEqual(self.injected, [])

    def test_card_pauses_this_topics_bubble_until_capture(self):
        import threading

        def rec(thread):
            return {"chat_id": "100", "thread_id": thread, "cv": threading.Condition(), "ended": False, "draft": True,
                    "draft_on": True, "stop_epoch": 0, "inflight": False, "key": "k" + thread, "draft_id": 1}
        here, other = rec("55"), rec("77")
        self.p._state["stops"].update({"here": here, "other": other})
        try:
            self.request()
            self.assertFalse(here["draft_on"])  # the client's Send button is free for the secret
            self.assertEqual(here["stop_epoch"], 1)
            self.assertTrue(other["draft_on"])
            self.p._on_pre_gateway_dispatch(self.event(SECRET))
            self.assertTrue(here["draft_on"])  # bubble back once the value is in
            self.assertNotEqual(here["draft_id"], 1)
        finally:
            self.p._state["stops"].clear()

    def test_pending_request_suppresses_new_bubbles_here(self):
        self.assertFalse(self.p._secret_pending_here("100", "55"))
        self.request()
        self.assertTrue(self.p._secret_pending_here("100", "55"))
        self.assertFalse(self.p._secret_pending_here("100", "77"))
        self.p._on_pre_gateway_dispatch(self.event(SECRET))
        self.assertFalse(self.p._secret_pending_here("100", "55"))

    def test_only_private_chats(self):
        with mock.patch.dict(os.environ, {"HERMES_SESSION_CHAT_TYPE": "group"}):
            res = self.request()
        self.assertFalse(res["success"])


if __name__ == "__main__":
    unittest.main()
