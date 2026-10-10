import importlib.util
import inspect
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from TwitchChannelPointsMiner import TwitchChannelPointsMiner


TEMPLATE_PATH = Path("/usr/src/app/run.template.py")
if not TEMPLATE_PATH.is_file():
    TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "run.template.py"

spec = importlib.util.spec_from_file_location("run_template", TEMPLATE_PATH)
run_template = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_template)


class RunTemplateTests(unittest.TestCase):
    def test_miner_configuration_matches_fork_constructor(self):
        with patch.dict(os.environ, {"TWITCH_USERNAME": "ci-validation-user"}, clear=True):
            kwargs = run_template.build_miner_kwargs()

        supported_parameters = set(
            inspect.signature(TwitchChannelPointsMiner.__init__).parameters
        )
        self.assertLessEqual(set(kwargs), supported_parameters)
        self.assertEqual(kwargs["username"], "ci-validation-user")

    def test_optional_notifications_are_disabled_when_empty(self):
        with patch.dict(
            os.environ, {"TWITCH_USERNAME": "ci-validation-user"}, clear=True
        ):
            settings = run_template.build_miner_kwargs()["logger_settings"]

        self.assertIsNone(settings.telegram)
        self.assertIsNone(settings.discord)

    def test_invalid_optional_notifications_are_disabled(self):
        with patch.dict(
            os.environ,
            {
                "TWITCH_USERNAME": "ci-validation-user",
                "TELEGRAM_CHAT_ID": "not-an-id",
                "TELEGRAM_BOT_TOKEN": "invalid-token",
                "DISCORD_WEBHOOK_URL": "http://example.com/webhook",
            },
            clear=True,
        ):
            settings = run_template.build_miner_kwargs()["logger_settings"]

        self.assertIsNone(settings.telegram)
        self.assertIsNone(settings.discord)

    def test_valid_optional_notifications_are_configured(self):
        with patch.dict(
            os.environ,
            {
                "TWITCH_USERNAME": "ci-validation-user",
                "TELEGRAM_CHAT_ID": "-12345",
                "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz",
                "DISCORD_WEBHOOK_URL": (
                    "https://discord.com/api/webhooks/123456789/"
                    "abcdefghijklmnopqrstuvwxyz"
                ),
            },
            clear=True,
        ):
            settings = run_template.build_miner_kwargs()["logger_settings"]

        self.assertIsNotNone(settings.telegram)
        self.assertIsNotNone(settings.discord)

    def test_streamers_use_fork_settings(self):
        streamers = run_template.build_streamers()

        self.assertTrue(streamers)
        self.assertEqual(streamers[0].username, "corgicam")


if __name__ == "__main__":
    unittest.main()
