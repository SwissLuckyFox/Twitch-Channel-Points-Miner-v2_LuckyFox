import unittest
from unittest.mock import Mock, patch

import requests

from TwitchChannelPointsMiner.classes.Settings import Priority
from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.Twitch import (
    Twitch,
    WATCH_STREAK_MINUTES,
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

    def test_time_limit_does_not_claim_reward_was_collected(self):
        alice = online("alice")
        alice.stream.minute_watched = WATCH_STREAK_MINUTES
        message = describe_watch_change({"alice": (alice, STREAK)}, {})
        self.assertIn("watch-time limit reached (reward not confirmed)", message)
        self.assertNotIn("watch streak collected", message)

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


class StreakWatchLoopTests(unittest.TestCase):
    def run_iteration(self, streamers, priority, response=None, after_request=None):
        twitch = Twitch.__new__(Twitch)
        twitch.running = True
        twitch.user_agent = "test"
        targets = []

        def report_targets(previous, current):
            targets.append(current)
            return current

        def stop(*args, **kwargs):
            twitch.running = False
            if after_request is not None:
                after_request()

        response = response if response is not None else Mock(status_code=204)
        with patch.object(
            Twitch, "check_streamer_online"
        ), patch.object(
            Twitch, "_Twitch__log_watch_targets", side_effect=report_targets
        ), patch.object(
            Twitch, "_Twitch__report_video", side_effect=stop
        ), patch.object(
            Twitch, "_Twitch__chuncked_sleep", side_effect=stop
        ), patch.object(
            Twitch, "_Twitch__check_connection_handler"
        ), patch.object(
            Streamer, "__str__", lambda self: self.username
        ), patch(
            "TwitchChannelPointsMiner.classes.Twitch.requests.post",
            side_effect=[response],
        ):
            twitch.send_minute_watched_events(streamers, priority)
        self.assertEqual(len(targets), 1)
        return targets[0]

    def test_streak_priority_uses_eight_minute_cutoff(self):
        self.assertEqual(WATCH_STREAK_MINUTES, 8)
        for minutes, expected in [(7, True), (7.99, True), (8, False)]:
            with self.subTest(minutes=minutes):
                alice = online("alice")
                alice.settings.watch_streak = True
                alice.stream.minute_watched = minutes
                targets = self.run_iteration([alice], [Priority.STREAK])
                self.assertEqual("alice" in targets, expected)

    def test_confirmed_streak_releases_priority_before_cutoff(self):
        alice = online("alice")
        alice.settings.watch_streak = True
        alice.stream.watch_streak_missing = False
        self.assertEqual(self.run_iteration([alice], [Priority.STREAK]), {})

    def test_finished_auto_drop_channel_is_not_selected_by_other_priorities(self):
        for priority in (
            Priority.ORDER,
            Priority.SUBSCRIBED,
            Priority.STREAK,
            Priority.POINTS_ASCENDING,
            Priority.POINTS_DESCEDING,
        ):
            with self.subTest(priority=priority):
                unter = online("unter")
                unter.drops_only = True
                unter.settings.watch_streak = True
                unter.activeMultipliers = [{"factor": 2}]
                self.assertEqual(
                    self.run_iteration([unter], [Priority.DROPS, priority]), {}
                )

    def test_configured_channel_without_drops_still_earns_points(self):
        alice = online("alice")
        targets = self.run_iteration([alice], [Priority.DROPS, Priority.ORDER])
        self.assertEqual(targets, {"alice": (alice, ORDER)})

    def test_auto_drop_channel_with_open_drop_is_still_selected(self):
        from test_drops import campaign_data, drop_data

        unter = online("unter")
        unter.drops_only = True
        unter.stream.campaigns = [Campaign(campaign_data([drop_data()]))]
        targets = self.run_iteration([unter], [Priority.DROPS, Priority.ORDER])
        self.assertEqual(
            targets, {"unter": (unter, WATCH_REASONS[Priority.DROPS])}
        )

    def test_deselected_channel_does_not_count_gap(self):
        alice, bob = online("alice"), online("bob")
        alice.settings.watch_streak = False
        bob.settings.watch_streak = True
        with patch(
            "TwitchChannelPointsMiner.classes.entities.Stream.time.monotonic",
            side_effect=[0, 60, 660, 720],
        ):
            alice.stream.update_minute_watched()
            alice.stream.update_minute_watched()
            observed = []

            def resume():
                alice.stream.update_minute_watched()
                observed.append(alice.stream.minute_watched)

            self.run_iteration(
                [alice, bob], [Priority.STREAK], after_request=resume
            )
            self.assertEqual(observed, [1])

    def test_failed_requests_do_not_count_gap(self):
        for response in (
            Mock(status_code=500),
            requests.exceptions.ConnectionError("test"),
            requests.exceptions.Timeout("test"),
        ):
            with self.subTest(response=response):
                alice = online("alice")
                with patch(
                    "TwitchChannelPointsMiner.classes.entities.Stream.time.monotonic",
                    side_effect=[0, 60, 660],
                ):
                    alice.stream.update_minute_watched()
                    alice.stream.update_minute_watched()
                    observed = []

                    def resume():
                        alice.stream.update_minute_watched()
                        observed.append(alice.stream.minute_watched)

                    self.run_iteration(
                        [alice], [Priority.ORDER], response, after_request=resume
                    )
                    self.assertEqual(observed, [1])


if __name__ == "__main__":
    unittest.main()
