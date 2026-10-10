import importlib.util
import inspect
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence


RUN_SCRIPT_PATH = Path("/usr/src/app/run.py")
if not RUN_SCRIPT_PATH.is_file():
    RUN_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "run.template.py"

spec = importlib.util.spec_from_file_location("run_template", RUN_SCRIPT_PATH)
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
        with tempfile.TemporaryDirectory() as config_directory:
            streamers_file = Path(config_directory) / "streamers.txt"
            streamers_file.write_text(
                "# Individual Twitch streamers\n"
                "my_channel chat=ONLINE\n"
                "AnotherChannel\n",
                encoding="utf-8",
            )
            streamers = run_template.build_streamers(streamers_file)

        self.assertEqual(
            [streamer.username for streamer in streamers],
            ["my_channel", "anotherchannel"],
        )
        self.assertEqual(streamers[0].settings.chat, ChatPresence.ONLINE)
        self.assertIsNone(streamers[1].settings)

    def test_missing_streamer_file_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as config_directory:
            streamer_file = Path(config_directory) / "missing.txt"

            with self.assertRaisesRegex(
                ValueError, "Create the file on the Unraid host"
            ):
                run_template.build_streamers(streamer_file)

            self.assertFalse(streamer_file.exists())

    def test_streamer_path_that_is_a_directory_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as config_directory:
            with self.assertRaisesRegex(ValueError, "points to a directory"):
                run_template.build_streamers(config_directory)

    def test_empty_streamer_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as config_directory:
            streamers_file = Path(config_directory) / "streamers.txt"
            streamers_file.write_text("# Add streamer names below\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "No streamers configured"):
                run_template.build_streamers(streamers_file)

    def test_invalid_chat_setting_is_rejected(self):
        with tempfile.TemporaryDirectory() as config_directory:
            streamers_file = Path(config_directory) / "streamers.txt"
            streamers_file.write_text("my_channel chat=SOMETIMES\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "invalid chat setting"):
                run_template.build_streamers(streamers_file)


if __name__ == "__main__":
    unittest.main()
