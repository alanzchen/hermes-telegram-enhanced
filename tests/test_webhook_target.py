import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location("hte_pkg_webhook", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hte_pkg_webhook"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


class WebhookTarget(unittest.TestCase):
    """A webhook-triggered run that delivers to a Telegram topic posts its buttons into that topic."""

    def setUp(self):
        self.p = load_plugin()
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "webhook_subscriptions.json").write_text(json.dumps({
            "mail": {"deliver": "telegram", "deliver_extra": {"chat_id": "100", "thread_id": "55"}},
            "slack-route": {"deliver": "slack", "deliver_extra": {"chat_id": "C1"}},
            "no-thread": {"deliver": "telegram", "deliver_extra": {"chat_id": "100"}},
        }))
        self.env = {"HERMES_HOME": self.tmp.name, "HERMES_SESSION_PLATFORM": "webhook",
                    "HERMES_SESSION_CHAT_ID": "webhook:v2:abc", "HERMES_SESSION_CHAT_TYPE": "webhook",
                    "HERMES_SESSION_KEY": "agent:main:webhook:webhook:webhook:v2:abc:webhook:mail"}

    def tearDown(self):
        self.tmp.cleanup()

    def target(self, route):
        with mock.patch.dict(os.environ, {**self.env, "HERMES_SESSION_USER_ID": f"webhook:{route}",
                                          "HERMES_SESSION_USER_NAME": route}):
            return self.p._current_target()

    def test_telegram_route_targets_its_topic_and_topic_session(self):
        target, err = self.target("mail")
        self.assertIsNone(err)
        self.assertEqual(target["chat_id"], "100")
        self.assertEqual(target["thread_id"], "55")
        self.assertEqual(target["user_id"], "100")
        self.assertEqual(target["chat_type"], "dm")
        # presses must land in the topic's own session, not the finished webhook run
        self.assertEqual(target["session_key"], "agent:main:telegram:dm:100:55")
        self.assertEqual(target["message_id"], "")

    def test_route_without_thread_targets_dm_root(self):
        target, err = self.target("no-thread")
        self.assertIsNone(err)
        self.assertEqual(target["session_key"], "agent:main:telegram:dm:100")

    def test_non_telegram_or_unknown_route_is_refused(self):
        for route in ("slack-route", "missing"):
            target, err = self.target(route)
            self.assertIsNone(target)
            self.assertIn("Telegram", err)

    def test_plain_telegram_session_unchanged(self):
        env = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "100",
               "HERMES_SESSION_THREAD_ID": "55", "HERMES_SESSION_KEY": "agent:main:telegram:dm:100:55",
               "HERMES_SESSION_USER_ID": "100", "HERMES_SESSION_CHAT_TYPE": "dm"}
        with mock.patch.dict(os.environ, env):
            target, err = self.p._current_target()
        self.assertIsNone(err)
        self.assertEqual(target["session_key"], "agent:main:telegram:dm:100:55")


if __name__ == "__main__":
    unittest.main()
