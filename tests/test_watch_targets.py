import unittest

from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.Twitch import (
    WATCH_REASONS,
    describe_watch_change,
)
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)

STREAK = WATCH_REASONS[Priority.STREAK]
SUB = WATCH_REASONS[Priority.SUBSCRIBED]
ORDER = WATCH_REASONS[Priority.ORDER]


def online(name):
    streamer = Streamer(name, settings=StreamerSettings(claim_drops=True))
    streamer.is_online = True
    return streamer


class DescribeWatchChangeTests(unittest.TestCase):
    def test_no_message_when_nothing_changes(self):
        alice = online("alice")
        self.assertIsNone(
            describe_watch_change({"alice": (alice, SUB)}, {"alice": (alice, SUB)})
        )
        self.assertIsNone(describe_watch_change({}, {}))

    def test_switch_to_catch_a_streak(self):
        alice, bob, carol = online("alice"), online("bob"), online("carol")
        message = describe_watch_change(
            {"alice": (alice, SUB), "bob": (bob, ORDER)},
            {"carol": (carol, STREAK), "alice": (alice, SUB)},
        )
        self.assertIn("Stopped mining bob: a higher priority channel took the slot", message)
        self.assertIn("Started mining carol: watch streak", message)
        self.assertIn("Mining now: carol (watch streak), alice (subscription points bonus)", message)

    def test_back_after_streak_collected(self):
        alice, carol = online("alice"), online("carol")
        carol.stream.watch_streak_missing = False
        message = describe_watch_change(
            {"carol": (carol, STREAK)}, {"alice": (alice, SUB)}
        )
        self.assertIn("Stopped mining carol: watch streak collected", message)
        self.assertIn("Started mining alice: subscription points bonus", message)

    def test_offline_and_nothing_left(self):
        alice = online("alice")
        alice.is_online = False
        message = describe_watch_change({"alice": (alice, ORDER)}, {})
        self.assertIn("Stopped mining alice: went offline", message)
        self.assertIn("Mining now: nothing", message)

    def test_reason_change_for_same_channel(self):
        alice = online("alice")
        message = describe_watch_change({"alice": (alice, ORDER)}, {"alice": (alice, STREAK)})
        self.assertIn("Still mining alice, now for: watch streak (was: streamer list priority)", message)


if __name__ == "__main__":
    unittest.main()
