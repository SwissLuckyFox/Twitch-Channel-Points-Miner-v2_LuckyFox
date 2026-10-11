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


if __name__ == "__main__":
    unittest.main()
