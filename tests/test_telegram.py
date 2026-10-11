import unittest
from unittest.mock import Mock, patch

import requests

from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.Telegram import Telegram


class TelegramTests(unittest.TestCase):
    def setUp(self):
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

        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)

    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_unsubscribed_event_is_not_sent(self, post):
        self.telegram.send("hello", Events.BET_WIN)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
