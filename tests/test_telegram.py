import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.Telegram import Telegram


class TelegramTests(unittest.TestCase):
    def setUp(self):
        interval = patch(
            "TwitchChannelPointsMiner.classes.Telegram.MIN_SEND_INTERVAL", 0
        )
        interval.start()
        self.addCleanup(interval.stop)
        self.telegram = Telegram(
            chat_id=123, token="secret-bot-token", events=[Events.GAIN_FOR_WATCH]
        )

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_http_error_reports_status_and_description_without_token(self, post):
        response = Mock(status_code=400)
        response.json.return_value = {
            "ok": False,
            "description": "Bad Request: chat not found",
        }
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "400 Client Error for url: https://api.telegram.org/botsecret-bot-token/",
            response=response,
        )
        post.return_value = response

        with self.assertLogs(
            "TwitchChannelPointsMiner.classes.Telegram", level="WARNING"
        ) as logs:
            self.telegram.send("hello", Events.GAIN_FOR_WATCH)
            self.telegram.flush()

        output = "\n".join(logs.output)
        self.assertIn("HTTP 400", output)
        self.assertIn("chat not found", output)
        self.assertNotIn("secret-bot-token", output)

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_repeated_identical_errors_are_logged_once(self, post):
        response = Mock(status_code=400)
        response.json.return_value = {"description": "Bad Request: chat not found"}
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=response
        )
        post.return_value = response

        with self.assertLogs(
            "TwitchChannelPointsMiner.classes.Telegram", level="DEBUG"
        ) as logs:
            for _ in range(3):
                self.telegram.send("hello", Events.GAIN_FOR_WATCH)
            self.telegram.flush()

        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)

    @patch("TwitchChannelPointsMiner.classes.Telegram.time.sleep")
    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_rate_limit_waits_and_retries(self, post, sleep):
        limited = Mock(status_code=429)
        limited.json.return_value = {
            "ok": False,
            "description": "Too Many Requests: retry after 13",
            "parameters": {"retry_after": 13},
        }
        delivered = Mock(status_code=200)
        delivered.json.return_value = {"ok": True}
        post.side_effect = [limited, delivered]

        with self.assertNoLogs(
            "TwitchChannelPointsMiner.classes.Telegram", level="WARNING"
        ):
            self.telegram.send("hello", Events.GAIN_FOR_WATCH)
            self.telegram.flush()

        self.assertEqual(post.call_count, 2)
        sleep.assert_any_call(13)

    @patch("TwitchChannelPointsMiner.classes.Telegram.time.sleep")
    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_persistent_rate_limit_gives_up_with_warning(self, post, sleep):
        limited = Mock(status_code=429)
        limited.json.return_value = {"parameters": {"retry_after": 1}}
        post.return_value = limited

        with self.assertLogs(
            "TwitchChannelPointsMiner.classes.Telegram", level="WARNING"
        ) as logs:
            self.telegram.send("hello", Events.GAIN_FOR_WATCH)
            self.telegram.flush()

        self.assertIn("rate limited", "\n".join(logs.output))
        self.assertGreater(post.call_count, 1)

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_send_does_not_block_caller(self, post):
        release = threading.Event()
        post.side_effect = lambda **_: release.wait(5) and Mock(
            status_code=200, json=Mock(return_value={"ok": True})
        )

        self.telegram.send("hello", Events.GAIN_FOR_WATCH)
        self.assertFalse(release.is_set())
        release.set()
        self.telegram.flush()
        post.assert_called_once()

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.get")
    def test_verify_reports_reachable_chat(self, get):
        get.return_value = Mock(status_code=200)
        get.return_value.json.return_value = {
            "ok": True,
            "result": {"type": "private", "first_name": "Lucky"},
        }

        with self.assertLogs(
            "TwitchChannelPointsMiner.classes.Telegram", level="INFO"
        ) as logs:
            self.assertTrue(self.telegram.verify())

        self.assertIn("Telegram ready", "\n".join(logs.output))
        self.assertTrue(get.call_args.args[0].endswith("/getChat"))
        self.assertEqual(get.call_args.kwargs["params"], {"chat_id": 123})

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.get")
    def test_verify_reports_unreachable_chat_without_token(self, get):
        get.return_value = Mock(status_code=400)
        get.return_value.json.return_value = {
            "ok": False,
            "description": "Bad Request: chat not found",
        }

        with self.assertLogs(
            "TwitchChannelPointsMiner.classes.Telegram", level="WARNING"
        ) as logs:
            self.assertFalse(self.telegram.verify())

        output = "\n".join(logs.output)
        self.assertIn("chat not found", output)
        self.assertNotIn("secret-bot-token", output)

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_unsubscribed_event_is_not_sent(self, post):
        self.telegram.send("hello", Events.BET_WIN)
        post.assert_not_called()



def ok(result=None):
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True, "result": result}
    return response


class TelegramPinTests(unittest.TestCase):
    def setUp(self):
        interval = patch(
            "TwitchChannelPointsMiner.classes.Telegram.MIN_SEND_INTERVAL", 0
        )
        interval.start()
        self.addCleanup(interval.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = os.path.join(folder.name, "pins.json")
        self.message_ids = iter(range(1, 100))

    def telegram(self, pin_limit=2):
        return Telegram(
            chat_id=123,
            token="secret-bot-token",
            events=[Events.DROP_CLAIM],
            pin_limit=pin_limit,
            pin_state_file=self.state,
        )

    def fake_api(self, url, data, timeout):
        if url.endswith("/sendMessage"):
            return ok({"message_id": next(self.message_ids)})
        return ok(True)

    def calls(self, post, method):
        return [
            c.kwargs["data"]
            for c in post.call_args_list
            if c.kwargs["url"].endswith("/" + method)
        ]

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_highlight_is_bold_and_escaped(self, post):
        post.side_effect = lambda url, data, timeout: self.fake_api(url, data, timeout)
        telegram = self.telegram()
        telegram.send("Badge <x> & co\nline 2", Events.DROP_CLAIM, highlight=True)
        telegram.flush()
        data = self.calls(post, "sendMessage")[0]
        self.assertEqual(data["parse_mode"], "HTML")
        self.assertEqual(data["text"], "<b>Badge &lt;x&gt; &amp; co</b>\nline 2")
        self.assertEqual(self.calls(post, "pinChatMessage"), [])

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_keeps_only_newest_pins_and_never_pins_twice(self, post):
        post.side_effect = lambda url, data, timeout: self.fake_api(url, data, timeout)
        telegram = self.telegram(pin_limit=2)
        for key in ("a", "b", "a", "c"):
            telegram.send("Badge", Events.DROP_CLAIM, highlight=True, pin_key=key)
        telegram.flush()

        self.assertEqual(
            [d["message_id"] for d in self.calls(post, "pinChatMessage")], [1, 2, 4]
        )
        self.assertEqual(
            [d["message_id"] for d in self.calls(post, "unpinChatMessage")], [1]
        )
        with open(self.state, encoding="utf-8") as file:
            stored = json.load(file)
        self.assertEqual([p["key"] for p in stored["pins"]], ["b", "c"])

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_pins_survive_restart(self, post):
        post.side_effect = lambda url, data, timeout: self.fake_api(url, data, timeout)
        first = self.telegram(pin_limit=1)
        first.send("Badge", Events.DROP_CLAIM, highlight=True, pin_key="a")
        first.flush()
        second = self.telegram(pin_limit=1)
        second.send("Badge", Events.DROP_CLAIM, highlight=True, pin_key="b")
        second.flush()
        self.assertEqual(
            [d["message_id"] for d in self.calls(post, "unpinChatMessage")], [1]
        )

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_missing_pin_right_is_reported_once(self, post):
        def api(url, data, timeout):
            if url.endswith("/pinChatMessage"):
                response = Mock(status_code=400)
                response.json.return_value = {
                    "ok": False,
                    "description": "Bad Request: not enough rights",
                }
                return response
            return self.fake_api(url, data, timeout)

        post.side_effect = api
        telegram = self.telegram()
        with self.assertLogs(
            "TwitchChannelPointsMiner.classes.Telegram", level="WARNING"
        ) as logs:
            for key in ("a", "b"):
                telegram.send("Badge", Events.DROP_CLAIM, highlight=True, pin_key=key)
            telegram.flush()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("Pin messages", logs.output[0])
        self.assertFalse(os.path.exists(self.state))

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_pin_limit_zero_disables_pinning(self, post):
        post.side_effect = lambda url, data, timeout: self.fake_api(url, data, timeout)
        telegram = self.telegram(pin_limit=0)
        telegram.send("Badge", Events.DROP_CLAIM, highlight=True, pin_key="a")
        telegram.flush()
        self.assertEqual(self.calls(post, "pinChatMessage"), [])

if __name__ == "__main__":
    unittest.main()
