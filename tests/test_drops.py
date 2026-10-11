import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import TwitchChannelPointsMiner.classes.Twitch as twitch_module
from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.entities.Drop import Drop, parse_twitch_datetime
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.constants import GQLOperations

FMT = "%Y-%m-%dT%H:%M:%SZ"


def _ts(delta_days):
    return (datetime.now() + timedelta(days=delta_days)).strftime(FMT)


def drop_data(
    drop_id="drop-1",
    minutes=30,
    subs=0,
    benefit_id="benefit-1",
    distribution="BADGE",
):
    data = {
        "id": drop_id,
        "name": "Watch to Redeem",
        "benefitEdges": [
            {
                "benefit": {
                    "id": benefit_id,
                    "name": "Alien: Isolation Horror",
                    "distributionType": distribution,
                }
            }
        ],
        "requiredMinutesWatched": minutes,
        "startAt": _ts(-1),
        "endAt": _ts(5),
    }
    if subs is not None:
        data["requiredSubs"] = subs
    return data


def campaign_data(drops, channels=None, account_connected=True):
    return {
        "id": "campaign-1",
        "name": "Alien: Isolation Horror",
        "status": "ACTIVE",
        "game": {"id": "12345", "displayName": "Alien: Isolation", "slug": "alien-isolation"},
        "allow": {"channels": channels, "isEnabled": True},
        "self": {"isAccountConnected": account_connected},
        "startAt": _ts(-1),
        "endAt": _ts(5),
        "timeBasedDrops": drops,
    }


class DropFilterTests(unittest.TestCase):
    def test_progress_is_reported_every_ten_percent(self):
        drop = Drop(drop_data(minutes=30))
        reported = []
        for minute in range(1, 31):
            drop.update(
                {
                    "hasPreconditionsMet": True,
                    "currentMinutesWatched": minute,
                    "dropInstanceID": None,
                    "isClaimed": False,
                }
            )
            if drop.is_printable:
                reported.append(drop.percentage_progress)
                drop.is_printable = False
        self.assertEqual([p // 10 for p in reported], list(range(0, 11)))

    def test_reloaded_drop_does_not_report_old_progress(self):
        drop = Drop(drop_data(minutes=30))
        drop.update(
            {
                "hasPreconditionsMet": True,
                "currentMinutesWatched": 14,
                "dropInstanceID": None,
                "isClaimed": False,
            }
        )
        self.assertFalse(drop.is_printable)

    def test_watch_drop_is_kept(self):
        campaign = Campaign(campaign_data([drop_data()]))
        self.assertEqual([d.id for d in campaign.drops], ["drop-1"])

    def test_subscribe_to_redeem_drops_are_ignored(self):
        campaign = Campaign(
            campaign_data(
                [
                    drop_data("watch", minutes=30),
                    drop_data("sub", minutes=0, subs=1, benefit_id="b2"),
                    drop_data("sub-no-minutes", minutes=0, subs=None, benefit_id="b3"),
                ]
            )
        )
        self.assertEqual([d.id for d in campaign.drops], ["watch"])

    def test_campaign_without_allow_list_accepts_any_channel(self):
        self.assertTrue(Campaign(campaign_data([drop_data()])).any_channel)
        restricted = Campaign(campaign_data([drop_data()], channels=[{"id": "42"}]))
        self.assertFalse(restricted.any_channel)
        self.assertEqual(restricted.channels, ["42"])

    def test_badge_is_eligible_without_linked_account(self):
        campaign = Campaign(campaign_data([drop_data()], account_connected=False))
        self.assertTrue(campaign.is_eligible)

    def test_game_item_needs_linked_account(self):
        campaign = Campaign(
            campaign_data(
                [drop_data(distribution="DIRECT_ENTITLEMENT")], account_connected=False
            )
        )
        self.assertFalse(campaign.is_eligible)


class ClaimedBenefitTests(unittest.TestCase):
    def test_reward_awarded_during_campaign_marks_drop_claimed(self):
        drop = Drop(drop_data())
        drop.mark_claimed_from_benefits({"benefit-1": datetime.now()})
        self.assertTrue(drop.is_claimed)

    def test_reward_from_older_campaign_does_not_count(self):
        drop = Drop(drop_data())
        drop.mark_claimed_from_benefits(
            {"benefit-1": datetime.now() - timedelta(days=30)}
        )
        self.assertFalse(drop.is_claimed)

    def test_parse_twitch_datetime_formats(self):
        self.assertEqual(
            parse_twitch_datetime("2024-01-02T03:04:05Z"), datetime(2024, 1, 2, 3, 4, 5)
        )
        self.assertEqual(
            parse_twitch_datetime("2024-01-02T03:04:05.123Z"),
            datetime(2024, 1, 2, 3, 4, 5, 123000),
        )
        self.assertIsNone(parse_twitch_datetime(None))


class DropsConditionTests(unittest.TestCase):
    def _streamer(self):
        streamer = Streamer("someone")
        streamer.settings = StreamerSettings(claim_drops=True)
        streamer.is_online = True
        return streamer

    def test_streamer_with_unclaimed_campaign_is_prioritised(self):
        streamer = self._streamer()
        streamer.stream.campaigns = [Campaign(campaign_data([drop_data()]))]
        # The removed "Drops Enabled" tag must no longer be required.
        streamer.stream.drops_tags = False
        self.assertTrue(streamer.drops_condition())

    def test_no_campaign_or_offline_is_not_prioritised(self):
        streamer = self._streamer()
        self.assertFalse(streamer.drops_condition())
        streamer.stream.campaigns = [Campaign(campaign_data([drop_data()]))]
        streamer.is_online = False
        self.assertFalse(streamer.drops_condition())

    def test_claimed_campaign_is_not_prioritised(self):
        streamer = self._streamer()
        campaign = Campaign(campaign_data([drop_data()]))
        campaign.drops[0].is_claimed = True
        streamer.stream.campaigns = [campaign]
        self.assertFalse(streamer.drops_condition())


class InventorySyncTests(unittest.TestCase):
    def setUp(self):
        self.twitch = object.__new__(Twitch)

    def _sync(self, inventory, campaigns):
        with patch.object(Twitch, "_Twitch__get_inventory", return_value=inventory):
            return self.twitch._Twitch__sync_campaigns(campaigns)

    def test_completed_campaign_from_game_event_drops_is_cleared(self):
        campaign = Campaign(campaign_data([drop_data()]))
        inventory = {
            "dropCampaignsInProgress": None,
            "gameEventDrops": [
                {
                    "id": "benefit-1",
                    "lastAwardedAt": datetime.now().strftime(FMT),
                }
            ],
        }
        result = self._sync(inventory, [campaign])
        self.assertEqual(result[0].drops, [])

    def test_unstarted_campaign_keeps_its_drops(self):
        campaign = Campaign(campaign_data([drop_data()]))
        result = self._sync({"dropCampaignsInProgress": [], "gameEventDrops": []}, [campaign])
        self.assertEqual(len(result[0].drops), 1)


class DirectoryDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.twitch = object.__new__(Twitch)

    def test_game_slug(self):
        self.assertEqual(twitch_module._game_slug("Alien: Isolation"), "alien-isolation")
        self.assertEqual(twitch_module._game_slug("Tom Clancy's The Division"), "tom-clancys-the-division")

    def test_directory_query_uses_drops_filter_and_parses_channels(self):
        response = {
            "data": {
                "game": {
                    "streams": {
                        "edges": [
                            {"node": {"broadcaster": {"id": "1", "login": "StreamerOne"}}},
                            {"node": {"broadcaster": None}},
                            {"node": {"broadcaster": {"id": "2", "login": "two"}}},
                        ]
                    }
                }
            }
        }
        with patch.object(Twitch, "post_gql_request", return_value=response) as post:
            channels = self.twitch.get_drops_enabled_channels(
                {"id": "12345", "name": "Alien: Isolation"}
            )

        self.assertEqual(channels, [("1", "streamerone"), ("2", "two")])
        sent = post.call_args[0][0]
        self.assertEqual(sent["variables"]["slug"], "alien-isolation")
        self.assertIn("DROPS_ENABLED", sent["variables"]["options"]["systemFilters"])

    def test_directory_errors_return_no_channels(self):
        with patch.object(Twitch, "post_gql_request", return_value={"data": {"game": None}}):
            self.assertEqual(self.twitch.get_drops_enabled_channels({"slug": "x"}), [])

    def test_discovered_channels_are_quiet_and_registered(self):
        campaign = Campaign(campaign_data([drop_data()]))
        streamers = []
        discovered = {}
        directory_channels = {}
        with patch.object(
            Twitch,
            "get_drops_enabled_channels",
            return_value=[("1", "a"), ("2", "b"), ("3", "c"), ("4", "d")],
        ):
            self.twitch._Twitch__discover_directory_channels(
                campaign, streamers, discovered, directory_channels
            )

        self.assertEqual(directory_channels["campaign-1"], {"1", "2", "3"})
        self.assertEqual([s.username for s in streamers], ["a", "b", "c"])
        self.assertTrue(all(s.notify is False for s in streamers))
        self.assertTrue(all(s.settings.claim_drops for s in streamers))


class DropScanSummaryTests(unittest.TestCase):
    def test_no_drops_found(self):
        message = twitch_module.describe_drop_scan(
            [], {"checked": 5, "no_watch_drops": 3, "already_earned": 2}
        )
        self.assertTrue(message.startswith("Scanned for drops: no new watch-time drops found"))
        self.assertIn("checked 5 campaigns", message)
        self.assertIn("2 already earned", message)
        self.assertIn("3 without watch-time drops", message)

    def test_empty_campaign_list_is_reported_as_failed_request(self):
        message = twitch_module.describe_drop_scan([], {"listed": 0, "checked": 0})
        self.assertIn("Twitch returned no active drop campaigns", message)

    def test_missing_details_are_reported(self):
        message = twitch_module.describe_drop_scan([], {"listed": 7, "checked": 0})
        self.assertIn("listed 7 active campaigns but their details could not be loaded", message)

    def test_found_drop_lists_category_and_reward(self):
        campaign = Campaign(campaign_data([drop_data()]))
        message = twitch_module.describe_drop_scan([campaign], {"checked": 1})
        self.assertIn("found 1 campaign(s)", message)
        self.assertIn("Category: Alien: Isolation", message)
        self.assertIn("Alien: Isolation Horror (30 min)", message)
        self.assertIn("any live channel in the category", message)
        self.assertIn("Checking which participating channels are live", message)

    def test_live_channel_scan_without_drops_names_its_sources(self):
        message = twitch_module.describe_drop_scan(
            [],
            {
                "listed": 0,
                "checked": 0,
                "in_progress": 0,
                "probed_channels": 4,
                "categories": ["Rust"],
            },
        )
        self.assertEqual(
            message,
            "Scanned for drops: no watch-time drops found "
            "(0 started in your inventory; 4 live channel(s) checked in Rust)",
        )

    def test_scan_without_live_channels_explains_why(self):
        message = twitch_module.describe_drop_scan(
            [], {"listed": 0, "checked": 0, "probed_channels": 0, "categories": []}
        )
        self.assertIn("none could be checked right now", message)

    def test_found_drop_mentions_sources(self):
        campaign = Campaign(campaign_data([drop_data()]))
        message = twitch_module.describe_drop_scan(
            [campaign],
            {"listed": 1, "checked": 1, "in_progress": 0, "probed_channels": 2,
             "categories": ["Alien: Isolation"]},
        )
        self.assertIn("found 1 campaign(s)", message)
        self.assertIn("sources: 0 started in your inventory; 2 live channel(s)", message)


def available_drops_entry(campaign_id="campaign-1", game_id="12345"):
    data = campaign_data([drop_data()])
    data["id"] = campaign_id
    data["game"] = {"id": game_id, "name": "Alien: Isolation", "displayName": "Alien: Isolation"}
    for key in ("status", "allow", "self", "startAt"):
        data.pop(key)
    return data


class AvailableDropsCampaignTests(unittest.TestCase):
    def test_builds_campaign_restricted_to_the_channel_it_was_found_on(self):
        data = twitch_module._campaign_from_available_drops(
            available_drops_entry(), {"id": "42", "name": "streamer"}
        )
        campaign = Campaign(data)
        self.assertEqual(campaign.channels, ["42"])
        self.assertEqual(campaign.game["id"], "12345")
        self.assertEqual([d.id for d in campaign.drops], ["drop-1"])
        self.assertTrue(campaign.dt_match)

    def test_entries_without_game_or_drops_are_ignored(self):
        no_game = available_drops_entry()
        no_game["game"] = None
        no_drops = available_drops_entry()
        no_drops["timeBasedDrops"] = []
        self.assertIsNone(twitch_module._campaign_from_available_drops(no_game, {"id": "1"}))
        self.assertIsNone(twitch_module._campaign_from_available_drops(no_drops, {"id": "1"}))


class CampaignDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.twitch = object.__new__(Twitch)
        self.twitch.running = True

    def _live_streamer(self, name, channel_id):
        streamer = Streamer(name)
        streamer.channel_id = channel_id
        streamer.settings = StreamerSettings(claim_drops=True)
        streamer.is_online = True
        streamer.stream.game = {"id": "12345", "name": "Alien: Isolation", "displayName": "Alien: Isolation"}
        streamer.stream.drops_tags = True
        return streamer

    def test_discovers_campaigns_on_own_and_directory_channels(self):
        own = self._live_streamer("mine", "1")
        responses = [
            {"data": {"channel": {"viewerDropCampaigns": [available_drops_entry()]}}},
            {"data": {"channel": {"viewerDropCampaigns": [available_drops_entry()]}}},
            {"data": {"channel": {"viewerDropCampaigns": None}}},
        ]
        with patch.object(Twitch, "post_gql_request", return_value=responses) as post, patch.object(
            Twitch,
            "get_drops_enabled_channels",
            return_value=[("1", "mine"), ("2", "other"), ("3", "third")],
        ):
            discovered, probed, categories = self.twitch._Twitch__discover_campaigns([own], [])

        self.assertEqual(probed, 3)
        self.assertEqual(categories, ["Alien: Isolation"])
        self.assertEqual(len(discovered), 1)
        self.assertEqual(
            [c["id"] for c in discovered[0]["allow"]["channels"]], ["1", "2"]
        )
        sent = post.call_args.args[0]
        self.assertEqual([op["variables"] for op in sent], [{"channelID": "1"}, {"channelID": "2"}, {"channelID": "3"}])
        self.assertEqual(
            sent[0]["extensions"]["persistedQuery"]["sha256Hash"],
            GQLOperations.FallbackHashes["DropsHighlightService_AvailableDrops"],
        )

    def test_known_campaigns_are_not_rediscovered(self):
        own = self._live_streamer("mine", "1")
        responses = [{"data": {"channel": {"viewerDropCampaigns": [available_drops_entry()]}}}]
        with patch.object(Twitch, "post_gql_request", return_value=responses), patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[]
        ):
            discovered, probed, _ = self.twitch._Twitch__discover_campaigns(
                [own], [campaign_data([drop_data()])]
            )
        self.assertEqual(discovered, [])
        self.assertEqual(probed, 1)


class StreamLoadTests(unittest.TestCase):
    def test_last_playlist_url(self):
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://a/high.m3u8\n#X\nhttps://a/low.m3u8\n"
        self.assertEqual(twitch_module._last_playlist_url(text), "https://a/low.m3u8")
        self.assertIsNone(twitch_module._last_playlist_url("#EXTM3U\n"))

    def test_loads_lowest_quality_chunk(self):
        from unittest.mock import Mock

        twitch = object.__new__(Twitch)
        twitch.user_agent = "ua"
        streamer = Streamer("khaldor")
        token = {"data": {"streamPlaybackAccessToken": {"signature": "s", "value": "v"}}}
        master = Mock(status_code=200, text="#EXTM3U\nhttps://a/high.m3u8\nhttps://a/low.m3u8\n")
        media = Mock(status_code=200, text="#EXTINF:2\nhttps://a/1.ts\n#EXTINF:2\nhttps://a/2.ts\n")
        with patch.object(Twitch, "post_gql_request", return_value=token) as post, patch(
            "TwitchChannelPointsMiner.classes.Twitch.requests.get", side_effect=[master, media]
        ) as get, patch(
            "TwitchChannelPointsMiner.classes.Twitch.requests.head",
            return_value=Mock(status_code=200),
        ) as head:
            self.assertTrue(twitch.load_stream_segment(streamer))
        self.assertEqual(post.call_args.args[0]["variables"]["login"], "khaldor")
        self.assertEqual(get.call_args_list[0].kwargs["params"], {"sig": "s", "token": "v"})
        self.assertEqual(get.call_args_list[1].args[0], "https://a/low.m3u8")
        self.assertEqual(head.call_args.args[0], "https://a/2.ts")

    def test_missing_token_skips_stream_load(self):
        twitch = object.__new__(Twitch)
        twitch.user_agent = "ua"
        with patch.object(Twitch, "post_gql_request", return_value={"errors": [{}]}), patch(
            "TwitchChannelPointsMiner.classes.Twitch.requests.get"
        ) as get:
            self.assertFalse(twitch.load_stream_segment(Streamer("khaldor")))
        get.assert_not_called()


class PersistedQueryFallbackTests(unittest.TestCase):
    def setUp(self):
        twitch_module._preferred_hashes.clear()
        self.addCleanup(twitch_module._preferred_hashes.clear)
        self.twitch = object.__new__(Twitch)

    def test_retries_with_fallback_hash_and_remembers_it(self):
        not_found = {"errors": [{"message": "PersistedQueryNotFound"}]}
        ok = {"data": {"currentUser": {"inventory": {}}}}
        with patch.object(
            Twitch, "_Twitch__send_gql_request", side_effect=[not_found, ok, ok]
        ) as send:
            self.assertEqual(self.twitch.post_gql_request(GQLOperations.Inventory), ok)
            self.assertEqual(self.twitch.post_gql_request(GQLOperations.Inventory), ok)

        fallback = GQLOperations.FallbackHashes["Inventory"]
        hashes = [
            call.args[0]["extensions"]["persistedQuery"]["sha256Hash"]
            for call in send.call_args_list
        ]
        self.assertEqual(
            hashes,
            [
                GQLOperations.Inventory["extensions"]["persistedQuery"]["sha256Hash"],
                fallback,
                fallback,
            ],
        )
        # The shared constant must not be mutated.
        self.assertNotEqual(
            GQLOperations.Inventory["extensions"]["persistedQuery"]["sha256Hash"],
            fallback,
        )

    def test_no_retry_for_other_errors(self):
        error = {"errors": [{"message": "service unavailable"}]}
        with patch.object(
            Twitch, "_Twitch__send_gql_request", return_value=error
        ) as send:
            self.assertEqual(self.twitch.post_gql_request(GQLOperations.Inventory), error)
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
