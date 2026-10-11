import json
import unittest
from datetime import datetime
from unittest.mock import Mock

from TwitchChannelPointsMiner.classes import StreamDatabase
from TwitchChannelPointsMiner.classes.StreamDatabase import WatchBadge, fetch_watch_badges

NOW = datetime(2026, 10, 11, 16, 0)


def page(props):
    data = json.dumps({"props": {"pageProps": props}, "buildId": "x"})
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{data}</script></body></html>"
    )


def list_item(set_id, description, cost="free", start="2026-10-01", end="2026-10-29", removed=False):
    return {
        "current": {
            "set_id": set_id,
            "version": {"id": "1", "title": set_id.title(), "description": description},
        },
        "removed": removed,
        "cost": cost,
        "added_at": "2026-10-01T00:00:00.000Z",
        "start_at_date": start,
        "start_at_time": "09:00",
        "end_at_date": end,
        "end_at_time": "08:58",
    }


def availability(minutes=30, channels=None, cost="free"):
    return {
        "hidden": False,
        "objectives": [[{"type": "twitch_watch", "minutes": minutes}]],
        "categories": [{"game": {"id": "458443", "name": "Alien: Isolation"}, "slug": None}],
        "channels": channels,
        "cost": cost,
        "start_at_date": "2026-10-01",
        "start_at_time": "09:00",
        "end_at_date": "2026-10-29",
        "end_at_time": "08:58:00.000",
    }


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, headers=None, timeout=None):
        path = url.replace(StreamDatabase.BASE_URL, "")
        self.requested.append(path)
        response = Mock(text=self.pages[path])
        response.raise_for_status.return_value = None
        return response


class StreamDatabaseTests(unittest.TestCase):
    def setUp(self):
        StreamDatabase._detail_cache.clear()
        self.addCleanup(StreamDatabase._detail_cache.clear)

    def test_running_free_watch_badges_are_loaded_with_category_and_minutes(self):
        session = FakeSession(
            {
                "/twitch/global-badges": page(
                    {
                        "initialTwitchGlobalBadges": [
                            list_item("seegson", "Earned by watching the Alien Isolation category for 30 minutes"),
                            list_item("paid-one", "Earned by watching", cost="paid"),
                            list_item("ended", "Earned by watching", end="2026-10-05"),
                            list_item("clip", "Earned by clipping a moment"),
                        ]
                    }
                ),
                "/twitch/global-badges/seegson/1": page({"availabilities": [availability()]}),
            }
        )

        badges = fetch_watch_badges(session=session, now=NOW)

        self.assertEqual(session.requested, ["/twitch/global-badges", "/twitch/global-badges/seegson/1"])
        self.assertEqual(len(badges), 1)
        badge = badges[0]
        self.assertEqual(badge.minutes, 30)
        self.assertEqual(badge.games, [{"id": "458443", "name": "Alien: Isolation", "slug": None}])
        self.assertEqual(badge.channels, [])
        self.assertEqual(badge.end_at, datetime(2026, 10, 29, 8, 58))
        self.assertTrue(badge.is_running(NOW))

    def test_channel_restricted_badge_keeps_channel_ids(self):
        channels = [{"user": {"id": "131070", "login": "RuneScape"}}]
        session = FakeSession(
            {
                "/twitch/global-badges": page(
                    {"initialTwitchGlobalBadges": [list_item("hat", "viewed the livestream for one hour")]}
                ),
                "/twitch/global-badges/hat/1": page(
                    {"availabilities": [availability(minutes=60, channels=channels)]}
                ),
            }
        )

        badge = fetch_watch_badges(session=session, now=NOW)[0]

        self.assertEqual(badge.channels, [("131070", "runescape")])

    def test_badge_details_are_cached(self):
        pages = {
            "/twitch/global-badges": page(
                {"initialTwitchGlobalBadges": [list_item("seegson", "watching for 30 minutes")]}
            ),
            "/twitch/global-badges/seegson/1": page({"availabilities": [availability()]}),
        }
        session = FakeSession(pages)
        fetch_watch_badges(session=session, now=NOW)
        fetch_watch_badges(session=session, now=NOW)
        self.assertEqual(session.requested.count("/twitch/global-badges/seegson/1"), 1)

    def test_non_watch_objectives_are_skipped(self):
        other = availability()
        other["objectives"] = [[{"type": "twitch_subscribe"}]]
        session = FakeSession(
            {
                "/twitch/global-badges": page(
                    {"initialTwitchGlobalBadges": [list_item("seegson", "watching for 30 minutes")]}
                ),
                "/twitch/global-badges/seegson/1": page({"availabilities": [other]}),
            }
        )
        self.assertEqual(fetch_watch_badges(session=session, now=NOW), [])

    def test_page_without_data_raises(self):
        session = FakeSession({"/twitch/global-badges": "<html></html>"})
        with self.assertRaises(ValueError):
            fetch_watch_badges(session=session, now=NOW)


def badge(**overrides):
    values = dict(
        set_id="seegson",
        title="SEEGSON Synthetics",
        minutes=30,
        games=[{"id": "458443", "name": "Alien: Isolation", "slug": None}],
        channels=[],
        start_at=datetime(2026, 10, 1, 9, 0),
        end_at=datetime(2030, 10, 29, 8, 58),
    )
    values.update(overrides)
    return WatchBadge(**values)


class BadgeFallbackCampaignTests(unittest.TestCase):
    def test_badge_becomes_any_channel_watch_campaign(self):
        from TwitchChannelPointsMiner.classes import Twitch as twitch_module
        from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign

        data = twitch_module._campaigns_from_badge(badge())
        self.assertEqual(len(data), 1)
        campaign = Campaign(data[0])
        self.assertTrue(campaign.any_channel)
        self.assertTrue(campaign.dt_match)
        self.assertTrue(campaign.is_eligible)
        self.assertEqual(campaign.game["id"], "458443")
        self.assertEqual([d.minutes_required for d in campaign.drops], [30])
        self.assertTrue(campaign.id.startswith(twitch_module.STREAMDB_PREFIX))

    def test_badge_without_dates_gives_no_campaign(self):
        from TwitchChannelPointsMiner.classes import Twitch as twitch_module

        self.assertEqual(twitch_module._campaigns_from_badge(badge(start_at=None)), [])

    def test_scan_sources_name_stream_database(self):
        from TwitchChannelPointsMiner.classes import Twitch as twitch_module

        text = twitch_module._scan_sources(
            {"probed_channels": 3, "in_progress": 0, "streamdb": 5, "streamdb_earned": 2}
        )
        self.assertTrue(
            text.startswith("Stream Database: 5 running watch badge(s), 2 already earned; ")
        )
        failed = twitch_module._scan_sources(
            {"probed_channels": 3, "streamdb": 0, "streamdb_error": "timeout"}
        )
        self.assertTrue(failed.startswith("Stream Database unavailable; "))

    def test_owned_badges_are_read_from_twitch(self):
        from unittest.mock import patch

        from TwitchChannelPointsMiner.classes.Twitch import Twitch

        twitch = object.__new__(Twitch)
        response = {
            "data": {
                "currentUser": {
                    "availableBadges": [
                        {"setID": "d20", "title": "d20"},
                        {"setID": "Gold-Stamp", "title": "Gold Stamp"},
                        None,
                    ]
                }
            }
        }
        with patch.object(Twitch, "post_gql_request", return_value=response) as post:
            self.assertEqual(
                twitch.get_owned_badges(), ({"d20", "gold-stamp"}, {"d20", "gold stamp"})
            )
        self.assertIn("availableBadges", post.call_args.args[0]["query"])
        with patch.object(Twitch, "post_gql_request", return_value={"errors": [{}]}):
            self.assertIsNone(twitch.get_owned_badges())


if __name__ == "__main__":
    unittest.main()
