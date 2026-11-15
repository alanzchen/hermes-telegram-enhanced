import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from miniapp import setup as miniapp_setup

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
    def test_profile_env_supplies_allowed_users_only_when_process_env_lacks_it(self):
        package_miniapp = importlib.import_module((self.p.__package__ or "hte_pkg_webhook") + ".miniapp")
        context = mock.Mock()
        fake_ui = SimpleNamespace(enabled=False, tool_schema={}, tool_handler=lambda _args: "{}", check_requirements=lambda: True)
        with tempfile.TemporaryDirectory() as profile:
            Path(profile, ".env").write_text("TELEGRAM_ALLOWED_USERS=42\n", encoding="utf-8")
            with mock.patch.object(self.p, "_profile_home", return_value=Path(profile)), \
                 mock.patch.object(self.p, "_miniapp_stop"), \
                 mock.patch.object(package_miniapp, "setup", return_value=fake_ui) as setup_mock, \
                 mock.patch.dict(os.environ, {}, clear=True):
                self.p._miniapp_register(context)
        self.assertEqual(setup_mock.call_args.kwargs["env"]["TELEGRAM_ALLOWED_USERS"], "42")

    def test_current_target_only_marks_an_explicit_guest_topic(self):
        session = {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "42",
            "HERMES_SESSION_KEY": "agent:main:telegram:dm:42",
            "HERMES_SESSION_CHAT_TYPE": "dm",
        }
        with mock.patch.dict(self.p._state, {"meta": {"guest_topic": {"43": "99"}}}):
            cases = [
                ("owner root", {**session, "HERMES_SESSION_USER_ID": "42", "HERMES_SESSION_THREAD_ID": ""}, True),
                ("owner topic", {**session, "HERMES_SESSION_USER_ID": "42", "HERMES_SESSION_THREAD_ID": "77"}, True),
                ("guest topic", {**session, "HERMES_SESSION_USER_ID": "43", "HERMES_SESSION_THREAD_ID": "99"}, False),
            ]
            for label, env, allowed in cases:
                with self.subTest(label=label), mock.patch.dict(os.environ, env, clear=True):
                    target, error = self.p._current_target()
                    self.assertIsNone(error)
                    service = miniapp_setup(
                        None,
                        lambda: "token",
                        lambda _payload: True,
                        state_dir=self.tmp.name,
                        current_target=lambda: (target, None),
                        send_message=lambda _payload: {"ok": True},
                        env={
                            "HTE_MINIAPP_PUBLIC_URL": "https://ui.example.test/coordinator",
                            "HTE_MINIAPP_PORT": "8787",
                            "HTE_MINIAPP_OWNER_USER_ID": "42",
                        },
                    )
                    self.assertEqual(service._is_private_owner_target(target), allowed)
                    self.assertEqual(target["guest_mode"], not allowed)


class GuestFallbackBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_missing_guest_topic_never_injects_into_the_main_dm(self):
        plugin = load_plugin()
        api_calls = []
        adapter = mock.Mock()
        context = mock.Mock()
        context.get_config.side_effect = lambda _key, default: default
        state = {"adapter": adapter, "ctx": context, "guests": mock.Mock(), "meta": {}}

        def api(method, payload):
            api_calls.append((method, payload))
            if method == "answerGuestQuery":
                return {"ok": True, "result": {"inline_message_id": "inline-1"}}
            return {"ok": True}

        guest = {
            "guest_query_id": "query-1",
            "from": {"id": 43},
            "chat": {"title": "Synthetic group"},
            "text": "Can you help?",
        }
        with mock.patch.dict(plugin._state, state), mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "43"}), \
             mock.patch.object(plugin, "_guest_topic", return_value=None), mock.patch.object(plugin, "_api", side_effect=api):
            await plugin._on_guest_message(SimpleNamespace(api_kwargs={"guest_message": guest}), None)

        adapter.build_source.assert_not_called()
        context.inject_message.assert_not_called()
        failure = next(payload for method, payload in api_calls if method == "editMessageText")
        self.assertEqual(failure["inline_message_id"], "inline-1")
        self.assertIn("markdown", failure["rich_message"])


if __name__ == "__main__":
    unittest.main()
