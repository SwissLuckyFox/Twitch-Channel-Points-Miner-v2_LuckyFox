import copy
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from TwitchChannelPointsMiner.classes.ProgressionDrops import (
    ProgressionDrops,
    daily_watchable,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)


TRACK = "pokemon#test-track"
GAME = {"id": "509658", "displayName": "Just Chatting", "name": "Just Chatting"}


def timestamp(days=0):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def reward_group(name, minutes=0, count=0, unlocked=True):
    return {
        "id": "group-" + name,
        "name": name,
        "track": TRACK,
        "lane": "lane-" + name,
        "progressCriteria": {
            "requirementType": "WATCH",
            "requirements": {"minutesWatched": 20, "subs": None},
            "isRepeatable": True,
            "isAutoProgressable": False,
            "repeatableConfig": {
                "repeatableTimes": 3,
                "resetIntervalSeconds": 86400,
                "shouldCompleteAllRepeatsToEarn": True,
            },
            "channels": None,
            "categories": [GAME],
        },
        "rewards": [{"id": "badge-" + name, "name": name, "distributionType": "BADGE"}],
        "self": {
            "status": "IN_PROGRESS",
            "currentMinutesWatched": minutes,
            "grantCount": count,
            "nextEligibleAt": None,
            "currentWindow": {"startedAt": timestamp(), "expiresAt": timestamp(1)},
        }
        if unlocked
        else None,
    }


class DailyWatchableTests(unittest.TestCase):
    def test_observed_twenty_minutes_is_paused_despite_in_progress_and_no_next_eligible(
        self,
    ):
        group = reward_group("Charmeleon", 20, 1)
        self.assertFalse(daily_watchable(group, utcnow()))
        group["self"]["currentWindow"]["expiresAt"] = timestamp(-1)
        # The server's expired window allows watching again without changing
        # the confirmed minutes or completed-day count locally.
        self.assertTrue(daily_watchable(group, utcnow()))
        self.assertEqual(group["self"]["currentMinutesWatched"], 20)
        group["self"]["currentWindow"] = None
        self.assertFalse(daily_watchable(group, utcnow()))

    def test_next_window_progress_is_watchable(self):
        self.assertTrue(daily_watchable(reward_group("Pikachu", 0, 1), utcnow()))

    def test_all_three_rolling_windows_complete_without_calendar_reset(self):
        group = reward_group("Pikachu")
        now = utcnow()
        for day in range(3):
            with self.subTest(day=day):
                group["self"]["currentMinutesWatched"] = 0
                group["self"]["grantCount"] = day
                expiry = now + timedelta(days=1)
                group["self"]["currentWindow"]["expiresAt"] = expiry.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                self.assertTrue(daily_watchable(group, now))
                group["self"]["currentMinutesWatched"] = 20
                group["self"]["grantCount"] = day + 1
                self.assertFalse(daily_watchable(group, now))
                now = expiry + timedelta(seconds=1)
                self.assertEqual(daily_watchable(group, now), day < 2)

    def test_explicit_cooldown_blocks_even_with_zero_minutes(self):
        group = reward_group("Pikachu")
        group["self"]["nextEligibleAt"] = timestamp(1)
        self.assertFalse(daily_watchable(group, utcnow()))
        group["self"]["nextEligibleAt"] = timestamp(-1)
        self.assertTrue(daily_watchable(group, utcnow()))

    def test_locked_completed_unknown_and_subscription_groups_are_excluded(self):
        groups = [reward_group("locked", unlocked=False), reward_group("done", 0, 3)]
        claimed = reward_group("claimed")
        claimed["self"]["status"] = "CLAIMED"
        groups.append(claimed)
        unknown = reward_group("unknown")
        unknown["self"]["status"] = "UNKNOWN"
        groups.append(unknown)
        sub = reward_group("subscription")
        sub["progressCriteria"]["requirementType"] = "SUBSCRIBE"
        groups.append(sub)
        for group in groups:
            with self.subTest(name=group["name"]):
                self.assertFalse(daily_watchable(group, utcnow()))


class ProgressionRotationTests(unittest.TestCase):
    def setUp(self):
        self.twitch = object.__new__(Twitch)
        self.groups = [
            reward_group("Charmeleon", 13),
            reward_group("Charizard", unlocked=False),
            reward_group("Pikachu", 7),
            reward_group("Wartortle"),
            reward_group("Ivysaur"),
        ]
        self.source = {
            "id": "evolutions",
            "name": "Pokemon Evolutions",
            "game": None,
            "startAt": timestamp(-10),
            "endAt": timestamp(100),
            "rewardGroups": self.groups,
        }
        self.selected = "lane-Charmeleon"
        self.requests = []
        self.selection_error = None
        self.confirm = True
        self.failed = False
        self.live = True
        self.game = GAME
        post = patch.object(Twitch, "post_gql_request", side_effect=self.post)
        post.start()
        self.addCleanup(post.stop)
        directory = patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("1", "channel")]
        )
        self.directory = directory.start()
        self.addCleanup(directory.stop)
        self.miner = ProgressionDrops(self.twitch)
        self.streamer = Streamer("channel", settings=StreamerSettings(claim_drops=True))
        self.streamer.channel_id = "1"
        self.streamer.is_online = True
        self.streamer.stream.game = GAME

    def post(self, request):
        self.requests.append(copy.deepcopy(request))
        if self.failed:
            return {"errors": [{"message": "unavailable"}]}
        op = request["operationName"]
        if op == "WithIsStreamLiveQuery":
            return {
                "data": {"user": {"stream": {"id": "stream"} if self.live else None}}
            }
        if op == "DropChannelCampaignsProgress":
            return {
                "data": {"channelDropCampaignsProgress": [copy.deepcopy(self.source)]}
            }
        if op == "DropsChannelCategory":
            return {"data": {"user": {"broadcastSettings": {"game": self.game}}}}
        if op == "ProgressionTrackSelections":
            return {
                "data": {"trackSelections": [{"track": TRACK, "lane": self.selected}]}
            }
        if op == "SetProgressionTrackSelection":
            if self.selection_error is None and self.confirm:
                self.selected = request["variables"]["input"]["lane"]
            return {"data": {"setTrackSelection": {"error": self.selection_error}}}
        self.fail("Unexpected operation: " + op)

    def sync(self):
        return self.miner.sync([self.streamer])

    def test_finishes_selected_then_rotates_all_four_and_pauses(self):
        for index, name in enumerate(("Charmeleon", "Pikachu", "Wartortle", "Ivysaur")):
            result = self.sync()
            self.assertEqual(result[0][0].drops[0].name, name)
            self.assertEqual(self.selected, "lane-" + name)
            self.assertEqual(len(result), 1)
            group = next(g for g in self.groups if g["name"] == name)
            group["self"]["currentMinutesWatched"] = 20
            group["self"]["grantCount"] = 1
        self.assertEqual(self.sync(), [])
        changes = [
            r
            for r in self.requests
            if r["operationName"] == "SetProgressionTrackSelection"
        ]
        self.assertEqual(len(changes), 3)
        self.assertEqual(
            changes[0]["variables"]["input"], {"track": TRACK, "lane": "lane-Pikachu"}
        )

    def test_restart_respects_server_pause_and_resumes_next_stage(self):
        for group in self.groups:
            if group["self"]:
                group["self"]["currentMinutesWatched"] = 20
                group["self"]["grantCount"] = 1
        self.miner = ProgressionDrops(self.twitch)
        self.assertEqual(self.sync(), [])
        self.groups[0]["self"]["currentMinutesWatched"] = 0
        self.assertEqual(self.sync()[0][0].drops[0].name, "Charmeleon")

    def test_fresh_unlock_uses_same_lane_but_different_group(self):
        self.groups[0]["self"]["grantCount"] = 3
        self.groups[1]["lane"] = self.groups[0]["lane"]
        self.groups[1]["self"] = reward_group("new")["self"]
        result = self.sync()
        self.assertEqual(result[0][0].drops[0].name, "Charizard")
        self.assertFalse(
            any(
                r["operationName"] == "SetProgressionTrackSelection"
                for r in self.requests
            )
        )

    def test_rejected_or_unconfirmed_swap_never_assigns_campaign(self):
        self.groups[0]["self"]["currentMinutesWatched"] = 20
        self.selection_error = {"code": "NOT_ELIGIBLE"}
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.sync(), [])
        self.selection_error = None
        self.confirm = False
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.sync(), [])

    def test_failed_queries_release_priority_instead_of_using_cached_progress(self):
        self.assertTrue(self.sync())
        self.failed = True
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.sync(), [])

    def test_cached_probe_rechecks_category_after_channel_changes_game(self):
        self.assertTrue(self.sync())
        self.game = {"id": "wrong-game"}
        self.streamer.is_online = False
        self.assertEqual(self.sync(), [])

    def test_directory_discovers_channel_without_configured_streamers(self):
        result = self.miner.sync([])
        self.assertEqual(result[0][1], ("1", "channel"))
        self.assertEqual(result[0][0].game["id"], GAME["id"])
        self.directory.assert_called_once()

    def test_offline_probe_does_not_block_new_directory_discovery(self):
        self.assertTrue(self.sync())
        self.live = False
        self.assertEqual(self.sync(), [])
        self.directory.assert_called_once()
        self.assertEqual(self.miner.probes, {})

    def test_daily_rewards_take_over_legacy_badge_fallbacks_even_while_paused(self):
        from test_drops import campaign_data, drop_data
        from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign

        legacy = campaign_data([drop_data()])
        legacy["timeBasedDrops"][0]["benefitEdges"][0]["benefit"]["name"] = "Pikachu"
        fallback = Campaign(legacy)
        ordinary = Campaign(campaign_data([drop_data()]))
        for group in self.groups:
            if group["self"]:
                group["self"]["currentMinutesWatched"] = 20
        self.assertEqual(self.sync(), [])
        self.assertTrue(self.miner.manages(fallback))
        self.assertFalse(self.miner.manages(ordinary))

    def test_adapter_can_be_selected_for_drop_priority_without_claiming_final_reward(
        self,
    ):
        from test_watch_targets import StreakWatchLoopTests
        from TwitchChannelPointsMiner.classes.Settings import Priority
        from TwitchChannelPointsMiner.classes.Twitch import WATCH_REASONS

        campaign = self.sync()[0][0]
        self.streamer.stream.campaigns = [campaign]
        self.assertTrue(self.streamer.drops_condition())
        self.assertFalse(campaign.drops[0].is_claimable)
        self.assertEqual(campaign.drops[0].current_minutes_watched, 13)
        self.assertEqual(campaign.channels, ["1"])
        self.assertFalse(campaign.drops[0].is_printable)
        self.streamer.drops_only = True
        targets = StreakWatchLoopTests().run_iteration(
            [self.streamer], [Priority.DROPS, Priority.ORDER]
        )
        self.assertEqual(
            targets, {"channel": (self.streamer, WATCH_REASONS[Priority.DROPS])}
        )
        self.streamer.stream.campaigns = []
        targets = StreakWatchLoopTests().run_iteration(
            [self.streamer], [Priority.DROPS, Priority.ORDER]
        )
        self.assertEqual(targets, {})

    def test_sync_campaigns_wires_discovery_and_releases_after_daily_limit(self):
        self.twitch.running = True
        iterations = []

        def sleep(*args, **kwargs):
            iterations.append(list(self.streamer.stream.campaigns))
            for group in self.groups:
                if group["self"]:
                    group["self"]["currentMinutesWatched"] = 20
                    group["self"]["grantCount"] = 1
            if len(iterations) == 2:
                self.twitch.running = False

        with patch.object(Twitch, "check_streamer_online"), patch.dict(
            os.environ, {"POKEMON_AUTO_ROTATE": "true"}
        ), patch.object(
            Twitch, "_Twitch__get_stream_database_badges", return_value=([], None)
        ), patch.object(
            Twitch, "_Twitch__get_twitchdrops_games", return_value=([], None)
        ), patch.object(
            Twitch, "_Twitch__get_drops_dashboard", return_value=[]
        ), patch.object(
            Twitch, "_Twitch__get_inventory", return_value={}
        ), patch.object(
            Twitch, "_Twitch__discover_campaigns", return_value=([], 0, [])
        ), patch.object(
            Twitch, "_Twitch__badge_fallback_campaigns", return_value=[]
        ), patch.object(
            Twitch, "_Twitch__chuncked_sleep", side_effect=sleep
        ):
            self.twitch.sync_campaigns([self.streamer])
        self.assertEqual(len(iterations), 2)
        self.assertEqual(iterations[0][0].drops[0].name, "Charmeleon")
        self.assertEqual(iterations[1], [])


if __name__ == "__main__":
    unittest.main()
