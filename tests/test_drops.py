import logging
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import TwitchChannelPointsMiner.classes.Twitch as twitch_module
from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.entities.Drop import Drop, parse_twitch_datetime
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.classes.StreamDatabase import WatchBadge
from TwitchChannelPointsMiner.classes.Telegram import Telegram
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.constants import GQLOperations
from TwitchChannelPointsMiner.logger import GlobalFormatter, LoggerSettings

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
    def test_progress_bar_has_one_block_per_five_percent(self):
        drop = Drop(drop_data(minutes=120))
        for percent in range(101):
            with self.subTest(percent=percent):
                drop.percentage_progress = percent
                bar = drop.progress_bar().split("|")[1]
                self.assertEqual(len(bar), 20)
                self.assertEqual(bar.count("\u2588"), percent // 5)
                self.assertEqual(bar.count(" "), 20 - percent // 5)

    def test_progress_bar_uses_percentage_not_watched_minutes(self):
        for minutes, watched in ((120, 108), (30, 27)):
            with self.subTest(minutes=minutes):
                drop = Drop(drop_data(minutes=minutes))
                drop.update({
                    "hasPreconditionsMet": True,
                    "currentMinutesWatched": watched,
                    "dropInstanceID": None,
                    "isClaimed": False,
                })
                self.assertEqual(
                    drop.progress_bar(),
                    "|" + "\u2588" * 18 + f"  | 90% [{watched}/{minutes}]",
                )

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

    def test_known_badge_title_only_fills_missing_reward_types(self):
        for distribution in (None, "", "BADGE", "EMOTE", "DIRECT_ENTITLEMENT"):
            with self.subTest(distribution=distribution):
                data = drop_data(distribution=distribution)
                data["benefitEdges"][0]["benefit"]["name"] = " Hunt White "
                drop = Drop(data, known_badge_titles={"hunt white"})
                expected = distribution or "BADGE"
                self.assertEqual(drop.benefit_types, [expected])
                self.assertEqual(
                    drop.badge_titles, {"hunt white"} if expected == "BADGE" else set()
                )
                self.assertEqual(
                    data["benefitEdges"][0]["benefit"]["distributionType"],
                    distribution,
                )

    def test_unknown_reward_title_does_not_become_a_badge(self):
        drop = Drop(
            drop_data(distribution=None), known_badge_titles={"hunt white"}
        )
        self.assertFalse(drop.is_badge_or_emote)
        self.assertEqual(drop.badge_titles, set())

    def test_known_badge_with_untyped_second_reward_needs_both_rewards(self):
        data = drop_data(distribution=None)
        data["benefitEdges"].append(
            {"benefit": {"id": "other", "name": "Unknown reward"}}
        )
        drop = Drop(data, known_badge_titles={"alien: isolation horror"})
        self.assertTrue(drop.is_badge_or_emote)
        self.assertEqual(drop.badge_titles, set())


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

    def test_all_badge_rewards_must_be_owned(self):
        data = drop_data()
        data["benefitEdges"].append(
            {
                "benefit": {
                    "id": "benefit-2",
                    "name": "Second badge",
                    "distributionType": "BADGE",
                }
            }
        )
        drop = Drop(data)
        drop.mark_claimed_from_badges({"alien: isolation horror"})
        self.assertFalse(drop.is_claimed)
        drop.mark_claimed_from_badges({"alien: isolation horror", "second badge"})
        self.assertTrue(drop.is_claimed)

    def test_mixed_rewards_are_not_completed_by_badge_ownership(self):
        data = drop_data()
        data["benefitEdges"].append(
            {
                "benefit": {
                    "id": "benefit-2",
                    "name": "Game item",
                    "distributionType": "DIRECT_ENTITLEMENT",
                }
            }
        )
        drop = Drop(data)
        drop.mark_claimed_from_badges({"alien: isolation horror", "game item"})
        self.assertFalse(drop.is_claimed)


class ClaimDropMessageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        path = patch.object(Settings, "analytics_path", self.folder, create=True)
        path.start()
        self.addCleanup(path.stop)
        self.twitch = Twitch.__new__(Twitch)

    def claim(self, claimed, distribution="BADGE", drop_id="drop-1", twitch=None):
        twitch = twitch or self.twitch
        drop = Drop(drop_data(drop_id=drop_id, distribution=distribution))
        with patch.object(
            Twitch, "_Twitch__request_claim_drop", return_value=claimed
        ), patch.object(Drop, "__str__", lambda self: self.name), self.assertLogs(
            "TwitchChannelPointsMiner.classes.Twitch", "DEBUG"
        ) as logs:
            self.assertEqual(twitch.claim_drop(drop), claimed)
        return logs.records[-1]

    def notified(self, record):
        return record.levelname == "INFO" and not hasattr(record, "skip_telegram")

    def test_claimed_badge_is_highlighted_and_pinned(self):
        record = self.claim(True)
        self.assertTrue(record.getMessage().startswith("Badge drop claimed: Alien"))
        self.assertTrue(record.telegram_highlight)
        self.assertEqual(record.telegram_pin_key, "badge:alien: isolation horror")

    def test_badge_without_twitch_reward_type_is_highlighted_and_pinned(self):
        data = drop_data(minutes=15)
        data["name"] = "Hunt White"
        benefit = data["benefitEdges"][0]["benefit"]
        benefit["name"] = "Hunt White"
        del benefit["distributionType"]
        campaign = Campaign(
            campaign_data([data]), known_badge_titles={"hunt white"}
        )
        drop = campaign.drops[0]
        drop.current_minutes_watched = 14
        drop.percentage_progress = 93
        with patch.object(
            Twitch, "_Twitch__request_claim_drop", return_value=True
        ), patch.object(Drop, "__str__", lambda self: self.name), self.assertLogs(
            "TwitchChannelPointsMiner.classes.Twitch", "INFO"
        ) as logs:
            self.assertTrue(self.twitch.claim_drop(drop))
        record = logs.records[-1]
        self.assertEqual(record.getMessage(), "Badge drop claimed: Hunt White\nHunt White")
        self.assertTrue(record.telegram_highlight)
        self.assertEqual(record.telegram_pin_key, "badge:hunt white")
        self.assertEqual(drop.badge_titles, {"hunt white"})

    @patch("TwitchChannelPointsMiner.classes.Telegram.MIN_SEND_INTERVAL", 0)
    @patch("TwitchChannelPointsMiner.classes.Telegram.requests.post")
    def test_inventory_scan_sends_and_pins_badge_with_missing_type(self, post):
        data = drop_data(minutes=15, distribution=None)
        data["name"] = data["benefitEdges"][0]["benefit"]["name"] = "Hunt White"
        data["self"] = {
            "hasPreconditionsMet": True,
            "currentMinutesWatched": 14,
            "dropInstanceID": "instance",
            "isClaimed": False,
        }
        inventory = {"dropCampaignsInProgress": [campaign_data([data])]}
        badge = WatchBadge(
            "hunt-white", "Hunt White", 15, [], [],
            datetime.now() - timedelta(days=1), datetime.now() + timedelta(days=1),
        )
        self.twitch.running = True
        telegram = Telegram(123, "test-token", events=["DROP_CLAIM"])
        formatter = GlobalFormatter(
            fmt="%(message)s",
            settings=LoggerSettings(telegram=telegram, emoji=False),
        )
        post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"ok": True, "result": {"message_id": 42}}),
        )

        def stop(*args, **kwargs):
            self.twitch.running = False

        with patch.dict(os.environ, {"POKEMON_AUTO_ROTATE": "false"}), patch.object(
            Twitch, "_Twitch__get_stream_database_badges", return_value=([badge], None)
        ), patch.object(
            Twitch, "_Twitch__get_twitchdrops_games",
            return_value=([{"name": "Hunt", "slug": "hunt-showdown"}], None)
        ), patch.object(
            Twitch, "_Twitch__get_drops_dashboard", return_value=[]
        ), patch.object(
            Twitch, "_Twitch__get_inventory", return_value=inventory
        ), patch.object(
            Twitch, "get_owned_badges", return_value=(set(), set())
        ), patch.object(
            Twitch, "_Twitch__discover_campaigns", return_value=([], 0, [])
        ) as discover, patch.object(
            Twitch, "_Twitch__badge_fallback_campaigns", return_value=[]
        ), patch.object(
            Twitch, "_Twitch__request_claim_drop", return_value=True
        ), patch.object(
            Twitch, "_Twitch__chuncked_sleep", side_effect=stop
        ), patch.object(Settings, "logger", LoggerSettings(less=True)), self.assertLogs(
            "TwitchChannelPointsMiner.classes.Twitch", "INFO"
        ) as logs:
            self.twitch.sync_campaigns([])
        self.assertEqual(
            discover.call_args.args[3], [{"name": "Hunt", "slug": "hunt-showdown"}]
        )
        claims = [
            record for record in logs.records
            if getattr(record, "telegram_pin_key", None) == "badge:hunt white"
        ]
        self.assertEqual(len(claims), 1)
        formatter.format(claims[0])
        telegram.flush()
        self.assertEqual(post.call_count, 2)
        send, pin = post.call_args_list
        self.assertTrue(send.kwargs["url"].endswith("/sendMessage"))
        self.assertEqual(send.kwargs["data"]["parse_mode"], "HTML")
        self.assertTrue(
            send.kwargs["data"]["text"].startswith("<b>Badge drop claimed: Hunt White</b>")
        )
        self.assertTrue(pin.kwargs["url"].endswith("/pinChatMessage"))
        self.assertEqual(pin.kwargs["data"]["message_id"], 42)

    def test_game_item_and_failed_badge_claim_are_plain(self):
        for drop_id, distribution, claimed in (
            ("item", "DIRECT_ENTITLEMENT", True),
            ("badge", "BADGE", False),
        ):
            with self.subTest(distribution=distribution):
                record = self.claim(claimed, distribution, drop_id)
                self.assertFalse(hasattr(record, "telegram_highlight"))
                self.assertFalse(hasattr(record, "telegram_pin_key"))

    def test_repeated_failed_claim_is_announced_once(self):
        self.assertTrue(self.notified(self.claim(False)))
        for _ in range(3):
            record = self.claim(False)
            self.assertEqual(record.levelname, "DEBUG")
            self.assertTrue(record.skip_telegram)
            self.assertTrue(record.skip_discord)
        self.assertTrue(
            self.notified(self.claim(False, "DIRECT_ENTITLEMENT", "drop-2"))
        )

    def test_success_after_failed_claim_is_announced_once_more(self):
        self.claim(False)
        record = self.claim(True)
        self.assertTrue(self.notified(record))
        self.assertTrue(record.telegram_highlight)
        self.assertFalse(self.notified(self.claim(True)))

    def test_announced_drops_survive_restart(self):
        self.claim(False)
        restarted = Twitch.__new__(Twitch)
        self.assertFalse(self.notified(self.claim(False, twitch=restarted)))


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
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (
            patch.object(Settings, "analytics_path", folder.name, create=True),
            patch.object(Drop, "__str__", lambda self: self.name),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.announcements = []

    def _sync(self, inventory, campaigns, owned_badges=None):
        logger = twitch_module.logger
        with patch.object(
            Twitch, "_Twitch__get_inventory", return_value=inventory
        ), patch.object(
            Twitch, "get_owned_badges", return_value=owned_badges
        ), patch.object(
            logger, "log", side_effect=lambda level, msg, extra: self.announcements.append(
                (level, msg, extra)
            )
        ):
            return self.twitch._Twitch__sync_campaigns(campaigns)

    def assertAnnouncedBadge(self, count=1):
        self.assertEqual(len(self.announcements), count)
        for level, message, extra in self.announcements:
            self.assertEqual(level, logging.INFO)
            self.assertTrue(message.startswith("Badge drop claimed: Alien"))
            self.assertTrue(extra["telegram_highlight"])
            self.assertEqual(extra["telegram_pin_key"], "badge:alien: isolation horror")

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
        self.assertIn("reward history", self.twitch.earned_reasons["campaign-1"])

    def test_owned_badge_reason_is_recorded(self):
        campaign = Campaign(campaign_data([drop_data()]))
        self._sync(
            {"dropCampaignsInProgress": [], "gameEventDrops": []},
            [campaign],
            owned_badges=(set(), {"alien: isolation horror"}),
        )
        self.assertEqual(
            self.twitch.earned_reasons,
            {"campaign-1": "badge already owned: Watch to Redeem"},
        )

    def test_unstarted_campaign_keeps_its_drops(self):
        campaign = Campaign(campaign_data([drop_data()]))
        result = self._sync({"dropCampaignsInProgress": [], "gameEventDrops": []}, [campaign])
        self.assertEqual(len(result[0].drops), 1)

    def test_awarded_reward_is_checked_even_when_campaign_is_in_progress(self):
        campaign = Campaign(campaign_data([drop_data()]))
        inventory = {
            "dropCampaignsInProgress": [{"id": campaign.id, "timeBasedDrops": []}],
            "gameEventDrops": [
                {"id": "benefit-1", "lastAwardedAt": datetime.now().strftime(FMT)}
            ],
        }
        self.assertEqual(self._sync(inventory, [campaign])[0].drops, [])

    def test_owned_badge_clears_discovered_campaign_without_inventory_reward(self):
        data = twitch_module._campaign_from_available_drops(
            available_drops_entry(), {"id": "42", "name": "unter"}
        )
        campaign = Campaign(data)
        streamer = Streamer("unter", settings=StreamerSettings(claim_drops=True))
        streamer.is_online = True
        streamer.stream.campaigns = [campaign]
        self.assertTrue(streamer.drops_condition())

        self._sync(
            {"dropCampaignsInProgress": [], "gameEventDrops": []},
            [campaign],
            ({"owned-badge"}, {"alien: isolation horror"}),
        )

        self.assertEqual(campaign.drops, [])
        self.assertFalse(streamer.drops_condition())

    def test_badge_earned_after_discovery_is_removed_on_next_sync(self):
        campaign = Campaign(campaign_data([drop_data()]))
        inventory = {"dropCampaignsInProgress": [], "gameEventDrops": []}
        self._sync(inventory, [campaign], (set(), set()))
        self.assertEqual(len(campaign.drops), 1)
        self._sync(
            inventory, [campaign], ({"owned-badge"}, {"alien: isolation horror"})
        )
        self.assertEqual(campaign.drops, [])
        self.assertAnnouncedBadge()

    def test_auto_awarded_badge_in_inventory_is_announced_once(self):
        campaign = Campaign(campaign_data([drop_data()]))
        progress = {
            "currentMinutesWatched": 30,
            "hasPreconditionsMet": True,
            "dropInstanceID": None,
            "isClaimed": False,
        }
        inventory = {
            "dropCampaignsInProgress": [
                {"id": campaign.id, "timeBasedDrops": [{"id": "drop-1", "self": progress}]}
            ]
        }
        self._sync(inventory, [campaign], (set(), set()))
        self.assertEqual(self.announcements, [])
        progress["isClaimed"] = True
        self._sync(inventory, [campaign], (set(), set()))
        self.assertAnnouncedBadge()

        # The same badge offered again (e.g. by another campaign) is not repeated.
        other = Campaign(campaign_data([drop_data("drop-2")]))
        self._sync({}, [other], (set(), set()))
        self._sync({}, [other], ({"owned-badge"}, {"alien: isolation horror"}))
        self.assertAnnouncedBadge()

    def test_already_owned_badge_at_startup_is_not_announced(self):
        campaign = Campaign(campaign_data([drop_data()]))
        self._sync({}, [campaign], ({"owned-badge"}, {"alien: isolation horror"}))
        self.assertEqual(campaign.drops, [])
        self.assertEqual(self.announcements, [])

    def test_owned_stream_database_badge_is_announced(self):
        data = campaign_data([drop_data()])
        data["id"] = f"{twitch_module.STREAMDB_PREFIX}owned-set:12345"
        campaign = Campaign(data)
        self._sync({}, [campaign], (set(), set()))
        self._sync({}, [campaign], ({"owned-set"}, {"different twitch title"}))
        self.assertEqual(campaign.drops, [])
        self.assertAnnouncedBadge()

    def test_badge_filtered_out_by_rebuild_is_still_announced(self):
        self._sync({}, [Campaign(campaign_data([drop_data()]))], (set(), set()))
        self._sync({}, [], ({"owned-badge"}, {"alien: isolation horror"}))
        self.assertAnnouncedBadge()

    def test_owned_badge_does_not_remove_game_item_with_same_name(self):
        campaign = Campaign(
            campaign_data([drop_data(distribution="DIRECT_ENTITLEMENT")])
        )
        self._sync({}, [campaign], ({"owned-badge"}, {"alien: isolation horror"}))
        self.assertEqual(len(campaign.drops), 1)

    def test_owned_badge_only_removes_matching_drop(self):
        unowned = drop_data("drop-2", benefit_id="benefit-2")
        unowned["benefitEdges"][0]["benefit"]["name"] = "Another badge"
        campaign = Campaign(campaign_data([drop_data(), unowned]))
        self._sync({}, [campaign], ({"owned-badge"}, {"alien: isolation horror"}))
        self.assertEqual([drop.id for drop in campaign.drops], ["drop-2"])

    def test_unavailable_badge_query_keeps_unconfirmed_drops(self):
        campaign = Campaign(campaign_data([drop_data()]))
        self._sync({}, [campaign], None)
        self.assertEqual(len(campaign.drops), 1)

    def test_owned_fallback_badge_is_matched_by_set_id(self):
        data = campaign_data([drop_data()])
        data["id"] = f"{twitch_module.STREAMDB_PREFIX}owned-set:12345"
        campaign = Campaign(data)
        self._sync({}, [campaign], ({"owned-set"}, {"different twitch title"}))
        self.assertEqual(campaign.drops, [])

    def test_owned_badge_overrides_stale_inventory_progress(self):
        campaign = Campaign(campaign_data([drop_data()]))
        inventory = {
            "dropCampaignsInProgress": [
                {
                    "id": campaign.id,
                    "timeBasedDrops": [
                        {
                            "id": "drop-1",
                            "self": {
                                "hasPreconditionsMet": True,
                                "currentMinutesWatched": 0,
                                "dropInstanceID": None,
                                "isClaimed": False,
                            },
                        }
                    ],
                }
            ]
        }
        self._sync(inventory, [campaign], (set(), {"alien: isolation horror"}))
        self.assertEqual(campaign.drops, [])


class DirectoryDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.twitch = object.__new__(Twitch)

    def test_discovered_channels_are_only_watched_for_drops(self):
        streamers, discovered = [], {}
        streamer = self.twitch._Twitch__add_drop_streamer(
            streamers, discovered, "42", "unter"
        )
        self.assertTrue(streamer.drops_only)
        self.assertFalse(streamer.notify)
        self.assertEqual(streamers, [streamer])
        self.assertIs(discovered["42"], streamer)

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
        self.assertEqual(sent["variables"]["limit"], 1)
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
        ), patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value={}
        ) as available:
            self.twitch._Twitch__discover_directory_channels(
                campaign, streamers, discovered, directory_channels
            )

        self.assertEqual(directory_channels["campaign-1"], {"1"})
        self.assertEqual([s.username for s in streamers], ["a"])
        self.assertTrue(all(s.notify is False for s in streamers))
        self.assertTrue(all(s.settings.claim_drops for s in streamers))
        available.assert_called_once_with(["1", "2", "3", "4"])

    def test_channels_running_other_campaigns_are_skipped(self):
        campaign = Campaign(campaign_data([drop_data()]))
        streamers = []
        discovered = {}
        directory_channels = {}
        offered = {
            "1": [{"id": "other-campaign"}],
            "2": [{"id": "campaign-1"}],
            "3": [],
        }
        with patch.object(
            Twitch,
            "get_drops_enabled_channels",
            return_value=[("1", "a"), ("2", "b"), ("3", "c")],
        ), patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value=offered
        ) as available:
            self.twitch._Twitch__discover_directory_channels(
                campaign, streamers, discovered, directory_channels
            )

        self.assertEqual(directory_channels["campaign-1"], {"2"})
        self.assertEqual([s.username for s in streamers], ["b"])
        self.assertEqual(
            [call.args[0] for call in available.call_args_list], [["1", "2", "3"]]
        )

    def test_campaign_matching_uses_id_not_campaign_or_reward_name(self):
        data = campaign_data([drop_data()])
        data["name"] = "Quadra Paintjobs"
        campaign = Campaign(data)
        offered = {
            "1": [{"id": campaign.id, "name": "Short-Fuse Neon"}],
            "2": [{"id": "other-campaign", "name": campaign.name}],
        }
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value=offered
        ):
            channels = self.twitch._Twitch__channels_offering_campaign(
                campaign, [("1", "matching"), ("2", "different")]
            )

        self.assertEqual(channels, [("1", "matching")])

    def test_inventory_campaign_can_use_directory_when_channel_api_omits_it(self):
        data = campaign_data([drop_data(distribution="IN_GAME")], account_connected=False)
        data["name"] = "Quadra Paintjobs"
        campaign = Campaign(data)
        inventory = {
            "dropCampaignsInProgress": [{
                "id": campaign.id,
                "timeBasedDrops": [{
                    "id": "drop-1",
                    "self": {
                        "hasPreconditionsMet": True,
                        "currentMinutesWatched": 12,
                        "dropInstanceID": None,
                        "isClaimed": False,
                    },
                }],
            }],
        }
        streamers, discovered, directory_channels = [], {}, {}
        with patch.object(
            Twitch, "_Twitch__get_inventory", return_value=inventory
        ):
            self.twitch._Twitch__sync_campaigns([campaign])
        self.assertTrue(campaign.in_inventory)
        self.assertEqual(campaign.drops[0].current_minutes_watched, 12)

        for returned in ([{"id": "badge-campaign", "name": "Chat Badge"}], []):
            with self.subTest(returned=returned), patch.object(
                Twitch, "get_drops_enabled_channels", return_value=[("1", "a")]
            ), patch.object(
                Twitch, "_Twitch__get_available_drop_campaigns",
                return_value={"1": returned},
            ), self.assertLogs(twitch_module.logger, level="INFO") as logs:
                self.twitch._Twitch__discover_directory_channels(
                    campaign, streamers, discovered, directory_channels
                )

            self.assertEqual(directory_channels[campaign.id], {"1"})
            self.assertEqual([s.username for s in streamers], ["a"])
            self.assertIn("using drops-enabled category channels", "\n".join(logs.output))
            self.assertNotIn("trying again in 10 minutes", "\n".join(logs.output))

    def test_inventory_campaign_prefers_explicit_campaign_matches(self):
        campaign = Campaign(campaign_data([drop_data()]))
        campaign.in_inventory = True
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            return_value={"1": [{"id": "badge-campaign"}], "2": [{"id": campaign.id}]},
        ):
            result = self.twitch._Twitch__channels_offering_campaign(
                campaign, [("1", "other"), ("2", "matching")]
            )
        self.assertEqual(result, [("2", "matching")])

    def test_restricted_inventory_campaign_does_not_bypass_campaign_matching(self):
        campaign = Campaign(campaign_data([drop_data()], channels=[{"id": "1"}]))
        campaign.in_inventory = True
        directory_channels = {}
        with patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("1", "a"), ("2", "b")]
        ), patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            return_value={"1": [{"id": "badge-campaign"}]},
        ):
            self.twitch._Twitch__discover_directory_channels(
                campaign, [], {}, directory_channels
            )
        self.assertEqual(directory_channels, {})

    def test_rejected_directory_scan_reports_actual_campaign_responses(self):
        campaign = Campaign(campaign_data([drop_data()]))
        offered = {
            "1": [{"id": "other-campaign", "name": "Other paintjobs"}],
            "2": [],
        }
        with patch.object(
            Twitch,
            "get_drops_enabled_channels",
            return_value=[("1", "other"), ("2", "empty"), ("3", "missing")],
        ), patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value=offered
        ), self.assertLogs(twitch_module.logger, level="INFO") as logs:
            self.twitch._Twitch__discover_directory_channels(campaign, [], {}, {})

        messages = "\n".join(logs.output)
        self.assertIn("expected campaign-1", messages)
        self.assertIn("other (1): Other paintjobs [other-campaign]", messages)
        self.assertIn("empty (2): no campaigns returned", messages)
        self.assertIn("missing (3): response unavailable", messages)
        self.assertIn("none of the 3 checked drops-enabled channels", messages)
        self.assertNotIn("run other campaigns", messages)

    def test_empty_campaign_responses_are_not_reported_as_other_campaigns(self):
        campaign = Campaign(campaign_data([drop_data()]))
        with patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("1", "empty")]
        ), patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value={"1": []}
        ), self.assertLogs(twitch_module.logger, level="INFO") as logs:
            self.twitch._Twitch__discover_directory_channels(campaign, [], {}, {})

        messages = "\n".join(logs.output)
        self.assertIn("empty (1): no campaigns returned", messages)
        self.assertNotIn("run other campaigns", messages)

    def test_allow_list_campaign_only_uses_live_allowed_channels(self):
        campaign = Campaign(
            campaign_data([drop_data()], channels=[{"id": "2"}, {"id": "99"}])
        )
        streamers = []
        discovered = {}
        directory_channels = {}
        with patch.object(
            Twitch,
            "get_drops_enabled_channels",
            return_value=[("1", "a"), ("2", "b"), ("3", "c")],
        ) as directory, patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value={}
        ):
            self.twitch._Twitch__discover_directory_channels(
                campaign, streamers, discovered, directory_channels
            )

        self.assertEqual(
            directory.call_args.kwargs["limit"], twitch_module.ALLOW_LIST_SEARCH_LIMIT
        )
        self.assertEqual(directory_channels["campaign-1"], {"2"})
        self.assertEqual([s.username for s in streamers], ["b"])


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


class LazyDropSyncTests(unittest.TestCase):
    def run_sync(
        self, streamers, iterations=2, after_iteration=None, online=True, campaign_count=100
    ):
        twitch = Twitch.__new__(Twitch)
        twitch.running = True
        data = []
        for index in range(campaign_count):
            entry = campaign_data([drop_data(drop_id=f"drop-{index}")])
            entry["id"] = f"campaign-{index}"
            entry["game"]["id"] = str(index)
            data.append(entry)
        rounds = []

        def check(streamer):
            streamer.is_online = online
            streamer.stream.game = dict(data[int(streamer.channel_id)]["game"])

        def sleep(*args, **kwargs):
            rounds.append([s.channel_id for s in streamers if s.drops_condition()])
            if after_iteration:
                after_iteration(streamers, len(rounds))
            if len(rounds) >= iterations:
                twitch.running = False

        with patch.object(Settings, "logger", LoggerSettings(less=True)), patch.dict(
            os.environ, {"POKEMON_AUTO_ROTATE": "false"}
        ), patch.object(
            Twitch, "_Twitch__get_stream_database_badges", return_value=([], None)
        ), patch.object(
            Twitch, "_Twitch__get_twitchdrops_games", return_value=([], None)
        ), patch.object(
            Twitch, "_Twitch__get_drops_dashboard", return_value=[]
        ), patch.object(
            Twitch, "_Twitch__get_inventory",
            return_value={"dropCampaignsInProgress": data},
        ), patch.object(
            Twitch, "_Twitch__discover_campaigns", return_value=([], 0, [])
        ) as discover, patch.object(
            Twitch, "_Twitch__sync_campaigns", side_effect=lambda campaigns: campaigns
        ), patch.object(
            Twitch, "_Twitch__channels_offering_campaign",
            side_effect=lambda campaign, channels: channels,
        ), patch.object(
            Twitch, "get_drops_enabled_channels",
            side_effect=lambda game, limit=1: [(game["id"], "channel" + game["id"])],
        ) as directory, patch.object(
            Twitch, "check_streamer_online", side_effect=check
        ) as checks, patch.object(
            Twitch, "_Twitch__chuncked_sleep", side_effect=sleep
        ):
            twitch.sync_campaigns(streamers)
        return rounds, directory, checks, discover

    def test_hundred_campaigns_add_and_refresh_only_one_external_channel(self):
        streamers = []
        rounds, directory, checks, _ = self.run_sync(streamers)
        self.assertEqual(len(streamers), 1)
        directory.assert_called_once()
        checks.assert_called_once_with(streamers[0])
        self.assertEqual(rounds[-1], ["0"])

    def test_followed_channel_prevents_all_external_channel_additions(self):
        own = Streamer("own", settings=StreamerSettings(claim_drops=True))
        own.channel_id = "0"
        own.is_online = True
        own.stream.game = {"id": "0", "displayName": "Alien"}
        own.stream.campaigns_ids = ["campaign-0"]
        streamers = [own]
        rounds, directory, checks, _ = self.run_sync(streamers)
        self.assertEqual(streamers, [own])
        directory.assert_not_called()
        checks.assert_not_called()
        self.assertEqual(rounds, [["0"], ["0"]])

    def test_offline_external_channel_is_not_kept_in_polling_set(self):
        streamers = []
        _, _, checks, _ = self.run_sync(streamers, iterations=4, online=False)
        checked_ids = [call.args[0].channel_id for call in checks.call_args_list]
        self.assertEqual(len(checked_ids), len(set(checked_ids)))

    def test_completed_campaign_releases_slot_for_next_campaign(self):
        def complete(streamers, iteration):
            if iteration == 2:
                streamers[0].stream.campaigns[0].drops.clear()

        streamers = []
        rounds, directory, _, _ = self.run_sync(
            streamers, iterations=4, after_iteration=complete
        )
        self.assertEqual(directory.call_count, 2)
        self.assertEqual(rounds[-1], ["1"])
        self.assertLessEqual(max(map(len, rounds)), 1)

    def test_completion_of_last_campaign_requests_fresh_discovery(self):
        def complete(streamers, iteration):
            if iteration == 2:
                streamers[0].stream.campaigns[0].drops.clear()

        _, _, _, discover = self.run_sync(
            [], iterations=4, after_iteration=complete, campaign_count=1
        )
        self.assertEqual(discover.call_count, 2)

    def test_followed_channel_coming_online_releases_external_target(self):
        own = Streamer("own", settings=StreamerSettings(claim_drops=True))
        own.channel_id = "1"
        own.stream.game = {"id": "1", "displayName": "Alien"}
        own.stream.campaigns_ids = ["campaign-1"]

        def go_online(streamers, iteration):
            if iteration == 2:
                own.is_online = True

        streamers = [own]
        rounds, directory, _, _ = self.run_sync(
            streamers, iterations=4, after_iteration=go_online
        )
        directory.assert_called_once()
        self.assertEqual(rounds[-1], ["1"])
        self.assertFalse(streamers[1].is_online)
        self.assertEqual(streamers[1].stream.campaigns, [])


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
        patcher = patch.object(Twitch, "get_owned_badges", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _live_streamer(self, name, channel_id):
        streamer = Streamer(name)
        streamer.channel_id = channel_id
        streamer.settings = StreamerSettings(claim_drops=True)
        streamer.is_online = True
        streamer.stream.game = {"id": "12345", "name": "Alien: Isolation", "displayName": "Alien: Isolation"}
        streamer.stream.drops_tags = True
        return streamer

    def test_followed_channel_with_drop_skips_directory_search(self):
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
        ) as directory:
            discovered, probed, categories = self.twitch._Twitch__discover_campaigns([own], [])

        directory.assert_not_called()
        self.assertEqual(probed, 1)
        self.assertEqual(categories, [])
        self.assertEqual(len(discovered), 1)
        self.assertEqual(
            [c["id"] for c in discovered[0]["allow"]["channels"]], ["1"]
        )
        sent = post.call_args.args[0]
        self.assertEqual([op["variables"] for op in sent], [{"channelID": "1"}])
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

    def test_site_category_discovers_twitch_confirmed_campaign(self):
        response = [{
            "data": {"channel": {"viewerDropCampaigns": [available_drops_entry()]}}
        }]
        game = {"name": "Alien: Isolation", "slug": "alien-isolation"}
        with patch.object(
            Twitch, "post_gql_request", return_value=response
        ), patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("42", "unter")]
        ) as directory:
            discovered, probed, categories = self.twitch._Twitch__discover_campaigns(
                [], [], drop_games=[game]
            )
        directory.assert_called_once_with(game, limit=twitch_module.DISCOVERY_PROBE_LIMIT)
        self.assertEqual(probed, 1)
        self.assertEqual(categories, ["Alien: Isolation"])
        self.assertEqual(discovered[0]["id"], "campaign-1")
        self.assertEqual(discovered[0]["game"]["id"], "12345")

    def test_external_search_stops_at_first_usable_category_and_channel(self):
        games = [{"name": str(i), "slug": str(i)} for i in range(100)]
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            return_value={"42": [], "43": [available_drops_entry()]},
        ) as available, patch.object(
            Twitch, "get_drops_enabled_channels",
            return_value=[("42", "first"), ("43", "partner")],
        ) as directory:
            discovered, probed, _ = self.twitch._Twitch__discover_campaigns(
                [], [], drop_games=games
            )
        directory.assert_called_once_with(games[0], limit=twitch_module.DISCOVERY_PROBE_LIMIT)
        # The whole category page is asked in one batch, so a partner-only campaign
        # is found even if the most-watched stream does not offer it.
        available.assert_called_once_with({"42": "first", "43": "partner"})
        self.assertEqual(probed, 2)
        self.assertEqual(discovered[0]["allow"]["channels"], [{"id": "43", "name": "partner"}])

    def test_discovered_campaign_keeps_the_site_category_slug(self):
        entry = available_drops_entry()
        entry["game"] = {"id": "1", "name": "Pokémon GO", "displayName": "Pokémon GO"}
        game = {"name": "pokémon go", "displayName": "pokémon go", "slug": "pokemon-go"}
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value={"42": [entry]}
        ), patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("42", "first")]
        ):
            discovered, _, _ = self.twitch._Twitch__discover_campaigns(
                [], [], drop_games=[game]
            )
        self.assertEqual(discovered[0]["game"]["slug"], "pokemon-go")

    def test_skipped_campaign_on_followed_channel_does_not_stop_search(self):
        # A followed channel offering the Pokemon campaign (managed separately) must
        # not end discovery when the Pokemon module has nothing left to farm.
        own = self._live_streamer("mine", "1")
        pokemon = available_drops_entry("pokemon")
        albion = available_drops_entry("albion", "777")
        albion["timeBasedDrops"][0]["benefitEdges"][0]["benefit"]["id"] = "b2"
        offers = {"1": [pokemon], "42": [albion]}
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            side_effect=lambda ch: {cid: offers[cid] for cid in ch},
        ), patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("42", "partner")]
        ):
            discovered, probed, _ = self.twitch._Twitch__discover_campaigns(
                [own], [], drop_games=[{"name": "Albion Online", "slug": "albion-online"}],
                skip=lambda campaign: campaign.id == "pokemon",
            )
        self.assertEqual([c["id"] for c in discovered], ["albion"])
        self.assertEqual(probed, 2)

    def test_earned_campaign_does_not_end_the_category_search(self):
        earned = available_drops_entry("earned")
        fresh = available_drops_entry("fresh")
        fresh["timeBasedDrops"][0]["benefitEdges"][0]["benefit"]["id"] = "benefit-2"
        offers = {"42": [earned], "43": [fresh]}
        games = [{"name": "Overwatch", "slug": "overwatch-2"}, {"name": "B", "slug": "b"}]
        channels = {"overwatch-2": [("42", "first")], "b": [("43", "second")]}
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            side_effect=lambda ch: {cid: offers[cid] for cid in ch},
        ), patch.object(
            Twitch, "get_drops_enabled_channels",
            side_effect=lambda game, limit=1: channels[game["slug"]],
        ):
            discovered, probed, categories = self.twitch._Twitch__discover_campaigns(
                [], [], drop_games=games,
                claimed_benefits={"benefit-1": datetime.utcnow()},
            )
        self.assertEqual([c["id"] for c in discovered], ["fresh"])
        self.assertEqual(probed, 2)
        self.assertEqual(categories, ["B", "Overwatch"])

    def test_owned_badge_campaign_does_not_end_the_category_search(self):
        games = [{"name": "A", "slug": "a"}, {"name": "B", "slug": "b"}]
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            return_value={"42": [available_drops_entry()]},
        ), patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("42", "first")]
        ), patch.object(
            Twitch, "get_owned_badges",
            return_value=(set(), {"alien: isolation horror"}),
        ) as owned:
            discovered, _, categories = self.twitch._Twitch__discover_campaigns(
                [], [], drop_games=games
            )
        self.assertEqual(discovered, [])
        self.assertEqual(categories, ["A", "B"])
        owned.assert_called_once_with()

    def test_followed_drop_is_checked_before_badge_named_channels(self):
        own = self._live_streamer("mine", "1")
        badge = WatchBadge(
            "badge", "Badge", 30, [], [("42", "external")],
            datetime.now(), datetime.now() + timedelta(days=1),
        )
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            return_value={"1": [available_drops_entry()]},
        ) as available, patch.object(Twitch, "get_drops_enabled_channels") as directory:
            self.twitch._Twitch__discover_campaigns([own], [], [badge])
        available.assert_called_once_with({"1": "mine"})
        directory.assert_not_called()

    def test_completed_followed_campaign_does_not_block_external_discovery(self):
        own = self._live_streamer("mine", "1")
        completed = campaign_data([drop_data()])
        completed["timeBasedDrops"][0]["self"] = {
            "hasPreconditionsMet": True, "currentMinutesWatched": 30,
            "dropInstanceID": None, "isClaimed": True,
        }
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            side_effect=[
                {"1": [available_drops_entry()]},
                {"42": [available_drops_entry("other")]},
            ],
        ), patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("42", "external")]
        ) as directory:
            discovered, probed, _ = self.twitch._Twitch__discover_campaigns(
                [own], [completed]
            )
        directory.assert_called_once()
        self.assertEqual(probed, 2)
        self.assertEqual([c["id"] for c in discovered], ["other"])

    def test_known_followed_campaign_records_confirmed_channel_without_search(self):
        own = self._live_streamer("mine", "1")
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns",
            return_value={"1": [available_drops_entry()]},
        ), patch.object(Twitch, "get_drops_enabled_channels") as directory:
            discovered, _, _ = self.twitch._Twitch__discover_campaigns(
                [own], [campaign_data([drop_data()])]
            )
        self.assertEqual(discovered, [])
        self.assertEqual(own.stream.campaigns_ids, ["campaign-1"])
        directory.assert_not_called()

    def test_followed_inventory_target_survives_omitted_channel_campaign(self):
        own = self._live_streamer("mine", "1")
        own.stream.campaigns = [Campaign(campaign_data([drop_data()]))]
        own.stream.campaigns_ids = ["campaign-1"]
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value={"1": []}
        ), patch.object(Twitch, "get_drops_enabled_channels") as directory:
            self.twitch._Twitch__discover_campaigns(
                [own], [campaign_data([drop_data()])]
            )
        self.assertEqual(own.stream.campaigns_ids, ["campaign-1"])
        directory.assert_not_called()

    def test_site_hint_without_twitch_confirmation_creates_no_campaign(self):
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value={"42": []}
        ), patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[("42", "unter")]
        ):
            discovered, _, _ = self.twitch._Twitch__discover_campaigns(
                [], [], drop_games=[{"name": "Alien", "slug": "alien"}]
            )
        self.assertEqual(discovered, [])

    def test_site_and_badge_categories_are_probed_once(self):
        badge = WatchBadge(
            "alien", "Alien", 30,
            [{"id": "12345", "name": "Alien: Isolation", "slug": "alien-isolation"}],
            [], datetime.now(), datetime.now() + timedelta(days=1),
        )
        with patch.object(
            Twitch, "_Twitch__get_available_drop_campaigns", return_value={}
        ), patch.object(
            Twitch, "get_drops_enabled_channels", return_value=[]
        ) as directory:
            self.twitch._Twitch__discover_campaigns(
                [], [], [badge], [{"name": "Alien", "slug": "alien-isolation"}]
            )
        directory.assert_called_once()

    def test_site_failure_warns_and_keeps_other_sources_available(self):
        import requests

        with patch.object(
            twitch_module, "fetch_drop_games", side_effect=requests.exceptions.Timeout()
        ), self.assertLogs("TwitchChannelPointsMiner.classes.Twitch", "WARNING") as logs:
            games, error = self.twitch._Twitch__get_twitchdrops_games()
        self.assertEqual(games, [])
        self.assertEqual(error, "Timeout")
        self.assertIn("using Stream Database", logs.output[0])

    def test_scan_sources_reports_both_external_sites(self):
        stats = {
            "probed_channels": 2, "streamdb": 1, "twitchdrops": 5,
        }
        sources = twitch_module._scan_sources(stats)
        self.assertIn("Stream Database: 1 running watch badge(s)", sources)
        self.assertIn("twitchdrops.app: 5 watch-drop categories", sources)
        stats["twitchdrops_error"] = "Timeout"
        self.assertIn("twitchdrops.app unavailable", twitch_module._scan_sources(stats))


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
        self.assertEqual(
            [call.args[0] for call in head.call_args_list],
            ["https://a/1.ts", "https://a/2.ts"],
        )

    def test_cached_playlist_skips_token_request(self):
        from unittest.mock import Mock

        twitch = object.__new__(Twitch)
        twitch.user_agent = "ua"
        cache = {"khaldor": ("https://a/low.m3u8", time.time())}
        media = Mock(status_code=200, text="https://a/1.ts\n")
        with patch.object(Twitch, "post_gql_request") as post, patch(
            "TwitchChannelPointsMiner.classes.Twitch.requests.get", return_value=media
        ) as get, patch(
            "TwitchChannelPointsMiner.classes.Twitch.requests.head",
            return_value=Mock(status_code=200),
        ):
            self.assertTrue(twitch.load_stream_segment(Streamer("khaldor"), cache))
        post.assert_not_called()
        self.assertEqual(get.call_args.args[0], "https://a/low.m3u8")

    def test_failed_chunk_list_drops_cached_playlist(self):
        from unittest.mock import Mock

        twitch = object.__new__(Twitch)
        twitch.user_agent = "ua"
        cache = {"khaldor": ("https://a/low.m3u8", time.time())}
        with patch(
            "TwitchChannelPointsMiner.classes.Twitch.requests.get",
            return_value=Mock(status_code=403, text="[{\"error\":\"x\"}]"),
        ):
            self.assertFalse(twitch.load_stream_segment(Streamer("khaldor"), cache))
        self.assertNotIn("khaldor", cache)

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
