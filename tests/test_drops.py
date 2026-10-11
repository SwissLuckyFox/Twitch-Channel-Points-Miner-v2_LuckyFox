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
