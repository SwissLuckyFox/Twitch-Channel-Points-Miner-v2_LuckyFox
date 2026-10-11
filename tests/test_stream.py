import inspect
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import TwitchChannelPointsMiner.classes.Twitch as twitch_module
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.entities.Stream import Stream

TWITCH_SOURCE = Path(twitch_module.__file__)


class StreamTests(unittest.TestCase):
    def test_game_id_from_game_dict(self):
        stream = Stream()
        stream.game = {"id": "509658", "name": "Just Chatting"}
        self.assertEqual(stream.game_id(), "509658")

    def test_game_id_without_game(self):
        stream = Stream()
        for game in ({}, None):
            with self.subTest(game=game):
                stream.game = game
                self.assertIsNone(stream.game_id())


class StreamerOfflineNotificationTests(unittest.TestCase):
    def _offline_record(self, streamer):
        with patch.object(Streamer, "toggle_chat"), patch.object(
            Streamer, "__str__", lambda self: self.username
        ), self.assertLogs(
            "TwitchChannelPointsMiner.classes.entities.Streamer", level="INFO"
        ) as logs:
            streamer.set_offline()
        return logs.records[-1]

    def test_startup_offline_check_is_not_notified(self):
        record = self._offline_record(Streamer("someone"))
        self.assertTrue(getattr(record, "skip_telegram", False))
        self.assertTrue(getattr(record, "skip_discord", False))

    def test_going_offline_is_notified(self):
        streamer = Streamer("someone")
        streamer.is_online = True
        record = self._offline_record(streamer)
        self.assertFalse(hasattr(record, "skip_telegram"))
        self.assertFalse(streamer.is_online)


class DropDiscoverySettingsTests(unittest.TestCase):
    def test_discovered_streamer_settings_only_use_supported_options(self):
        # Discovered drop channels are created inside Twitch.sync_campaigns; make sure
        # every keyword passed to StreamerSettings there exists in this fork.
        source = TWITCH_SOURCE.read_text(encoding="utf-8")
        calls = re.findall(r"StreamerSettings\((.*?)\)", source, re.DOTALL)
        self.assertTrue(calls)

        supported = set(inspect.signature(StreamerSettings.__init__).parameters)
        for call in calls:
            used = set(re.findall(r"(\w+)\s*=", call))
            self.assertLessEqual(used, supported)


if __name__ == "__main__":
    unittest.main()
