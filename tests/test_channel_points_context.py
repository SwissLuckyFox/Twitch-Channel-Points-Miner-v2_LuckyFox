import re
import unittest
from unittest.mock import Mock, patch

from TwitchChannelPointsMiner.classes.Exceptions import (
    StreamerDoesNotExistException,
    UnexpectedTwitchResponseException,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.constants import (
    CLIENT_ID,
    CLIENT_VERSION,
    GQLOperations,
    URL,
    USER_AGENTS,
)


class ChannelPointsContextTests(unittest.TestCase):
    def setUp(self):
        self.post_gql_patcher = patch.object(Twitch, "post_gql_request")
        claim_bonus_patcher = patch.object(Twitch, "claim_bonus")
        self.post_gql_request = self.post_gql_patcher.start()
        self.claim_bonus = claim_bonus_patcher.start()
        self.addCleanup(self.post_gql_patcher.stop)
        self.addCleanup(claim_bonus_patcher.stop)
        self.streamer = Mock()
        self.streamer.username = "test_channel"
        self.twitch = object.__new__(Twitch)

    def test_loads_points_from_valid_graphql_response(self):
        self.twitch.post_gql_request.return_value = {
            "data": {
                "community": {
                    "channel": {
                        "self": {
                            "communityPoints": {
                                "balance": 1234,
                                "activeMultipliers": [],
                                "availableClaim": None,
                            }
                        }
                    }
                }
            }
        }

        self.twitch.load_channel_points_context(self.streamer)

        self.assertEqual(self.streamer.channel_points, 1234)
        self.assertEqual(self.streamer.activeMultipliers, [])
        self.claim_bonus.assert_not_called()

    def test_missing_graphql_data_has_clear_exception(self):
        self.twitch.post_gql_request.return_value = {
            "errors": [
                {
                    "message": "upstream unavailable",
                    "extensions": {"code": "SERVICE_UNAVAILABLE"},
                }
            ]
        }

        with self.assertRaisesRegex(
            UnexpectedTwitchResponseException,
            "returned no GraphQL data.*SERVICE_UNAVAILABLE: upstream unavailable",
        ):
            self.twitch.load_channel_points_context(self.streamer)

    def test_current_persisted_query_hashes_are_configured(self):
        current_hashes = {
            "VideoPlayerStreamInfoOverlayChannel": (
                "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d"
            ),
            "DropsPage_ClaimDropRewards": (
                "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930"
            ),
            "ChannelPointsContext": (
                "7fe050e3761eb2cf258d70ee1a21cbd76fa8cf3d7e7b12fc437e7029d446b5e3"
            ),
            "Inventory": (
                "d86775d0ef16a63a33ad52e80eaff963b2d5b72fada7c991504a57496e1d8e4b"
            ),
            "ViewerDropsDashboard": (
                "5a4da2ab3d5b47c9f9ce864e727b2cb346af1e3ea8b897fe8f704a97ff017619"
            ),
            "DropCampaignDetails": (
                "f6396f5ffdde867a8f6f6da18286e4baf02e5b98d14689a69b5af320a4c7b7b8"
            ),
            "DropsHighlightService_AvailableDrops": (
                "9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f"
            ),
            "GetIDFromLogin": (
                "94e82a7b1e3c21e186daa73ee2afc4b8f23bade1fbbff6fe8ac133f50a2f58ca"
            ),
            "ChannelFollows": (
                "eecf815273d3d949e5cf0085cc5084cd8a1b5b7b6f7990cf43cb0beadf546907"
            ),
        }
        for operation_name, expected_hash in current_hashes.items():
            with self.subTest(operation=operation_name):
                operation = getattr(GQLOperations, operation_name)
                self.assertEqual(
                    operation["extensions"]["persistedQuery"]["sha256Hash"],
                    expected_hash,
                )

        self.assertEqual(
            GQLOperations.Inventory["variables"], {"fetchRewardCampaigns": True}
        )
        self.assertEqual(
            GQLOperations.ViewerDropsDashboard["variables"],
            {"fetchRewardCampaigns": True},
        )

    def test_follower_graphql_errors_are_not_reported_as_zero_followers(self):
        self.post_gql_request.return_value = {
            "errors": [
                {
                    "message": "PersistedQueryNotFound",
                    "extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"},
                }
            ]
        }

        with self.assertRaisesRegex(
            UnexpectedTwitchResponseException,
            "PERSISTED_QUERY_NOT_FOUND: PersistedQueryNotFound",
        ):
            self.twitch.get_followers()

    def test_get_followers_returns_followers_from_valid_response(self):
        self.post_gql_request.return_value = {
            "data": {
                "user": {
                    "follows": {
                        "edges": [
                            {
                                "cursor": "cursor-1",
                                "node": {"login": "SomeFollower"},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        }

        self.assertEqual(self.twitch.get_followers(), ["somefollower"])

    def test_channel_lookup_does_not_mistake_graphql_errors_for_missing_channel(self):
        self.twitch.post_gql_request.return_value = {
            "errors": [
                {
                    "message": "IntegrityCheckFailed",
                    "extensions": {"code": "INTEGRITY_CHECK_FAILED"},
                }
            ]
        }

        with self.assertRaisesRegex(
            UnexpectedTwitchResponseException,
            "INTEGRITY_CHECK_FAILED: IntegrityCheckFailed",
        ):
            self.twitch.get_channel_id("existing_channel")

    def test_channel_lookup_reports_nonexistent_channel_from_valid_data(self):
        self.twitch.post_gql_request.return_value = {"data": {"user": None}}

        with self.assertRaises(StreamerDoesNotExistException):
            self.twitch.get_channel_id("missing_channel")

    @patch("TwitchChannelPointsMiner.classes.Twitch.requests.post")
    def test_graphql_request_includes_client_integrity_context_headers(self, post):
        post.return_value.json.return_value = {"data": {}}
        self.twitch.user_agent = "test-user-agent"
        self.twitch.device_id = "device-id"
        self.twitch.client_session = "session-id"
        self.twitch.client_version = CLIENT_VERSION
        self.twitch.client_version_loaded = False
        self.twitch.twilight_build_id_pattern = re.compile(
            r'window\.__twilightBuildID\s*=\s*"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"'
        )
        self.twitch.twitch_login = Mock()
        self.twitch.twitch_login.get_auth_token.return_value = "test-token"

        self.post_gql_patcher.stop()
        with patch(
            "TwitchChannelPointsMiner.classes.Twitch.requests.get"
        ) as get_client_page:
            get_client_page.return_value = Mock(
                status_code=200,
                text='window.__twilightBuildID = "12345678-1234-1234-1234-123456789abc";',
            )
            Twitch.post_gql_request(self.twitch, {"operationName": "test"})

        get_client_page.assert_called_once_with(URL, timeout=15)
        self.assertEqual(
            post.call_args.kwargs["headers"],
            {
                "Authorization": "OAuth test-token",
                "Client-Id": CLIENT_ID,
                "Client-Session-Id": "session-id",
                "Client-Version": "12345678-1234-1234-1234-123456789abc",
                "User-Agent": "test-user-agent",
                "X-Device-Id": "device-id",
            },
        )

    def test_nonexistent_channel_raises_specific_exception(self):
        self.twitch.post_gql_request.return_value = {"data": {"community": None}}

        with self.assertRaises(StreamerDoesNotExistException):
            self.twitch.load_channel_points_context(self.streamer)

    def test_incomplete_points_response_has_clear_exception(self):
        self.twitch.post_gql_request.return_value = {
            "data": {"community": {"channel": {}}}
        }

        with self.assertRaisesRegex(
            UnexpectedTwitchResponseException, "incomplete channel points data"
        ):
            self.twitch.load_channel_points_context(self.streamer)

    @patch("TwitchChannelPointsMiner.classes.Twitch.requests.get")
    def test_spade_url_uses_desktop_agent_and_new_settings_domain(self, get):
        page = Mock(
            text='<script src="https://assets.twitch.tv/config/settings.abc123.js">'
        )
        settings = Mock(text='{"spade_url":"https://video-edge.example/spade"}')
        get.side_effect = [page, settings]
        self.streamer.streamer_url = "https://www.twitch.tv/test_channel"

        self.twitch.get_spade_url(self.streamer)

        self.assertEqual(
            self.streamer.stream.spade_url, "https://video-edge.example/spade"
        )
        self.assertEqual(
            get.call_args_list[1].args[0],
            "https://assets.twitch.tv/config/settings.abc123.js",
        )
        for call in get.call_args_list:
            self.assertEqual(
                call.kwargs["headers"]["User-Agent"], USER_AGENTS["Linux"]["FIREFOX"]
            )
            self.assertEqual(call.kwargs["timeout"], 30)

    @patch("TwitchChannelPointsMiner.classes.Twitch.requests.get")
    def test_missing_settings_script_does_not_crash(self, get):
        get.return_value = Mock(text="<html>no settings here</html>")
        self.streamer.streamer_url = "https://www.twitch.tv/test_channel"
        self.streamer.stream.spade_url = None

        with self.assertLogs(
            "TwitchChannelPointsMiner.classes.Twitch", level="WARNING"
        ):
            self.twitch.get_spade_url(self.streamer)

        self.assertIsNone(self.streamer.stream.spade_url)
        get.assert_called_once()

    def test_drops_dashboard_without_campaigns_returns_empty_list(self):
        for response in (
            {},
            {"data": {"currentUser": None}},
            {"data": {"currentUser": {"dropCampaigns": None}}},
            {"errors": [{"message": "failed integrity check"}], "data": None},
        ):
            with self.subTest(response=response):
                self.post_gql_request.return_value = response
                self.assertEqual(
                    self.twitch._Twitch__get_drops_dashboard(status="ACTIVE"), []
                )

    def test_drops_dashboard_filters_by_status(self):
        self.post_gql_request.return_value = {
            "data": {
                "currentUser": {
                    "dropCampaigns": [
                        {"id": "a", "status": "ACTIVE"},
                        {"id": "b", "status": "EXPIRED"},
                        None,
                    ]
                }
            }
        }

        self.assertEqual(
            self.twitch._Twitch__get_drops_dashboard(status="active"),
            [{"id": "a", "status": "ACTIVE"}],
        )

    def test_drops_dashboard_retries_with_alternate_variant_and_remembers_it(self):
        from TwitchChannelPointsMiner.classes import Twitch as twitch_module

        twitch_module._preferred_drops_variants.clear()
        self.addCleanup(twitch_module._preferred_drops_variants.clear)
        campaigns = {"data": {"currentUser": {"dropCampaigns": [{"id": "a", "status": "ACTIVE"}]}}}
        self.post_gql_request.side_effect = [
            {"data": {"currentUser": {"dropCampaigns": None}}},
            campaigns,
            campaigns,
        ]

        self.assertEqual(
            self.twitch._Twitch__get_drops_dashboard(status="ACTIVE"),
            [{"id": "a", "status": "ACTIVE"}],
        )
        alternate = self.post_gql_request.call_args_list[1].args[0]
        self.assertEqual(alternate["variables"], {"fetchRewardCampaigns": False})
        self.assertEqual(
            alternate["extensions"]["persistedQuery"]["sha256Hash"],
            GQLOperations.FallbackHashes["ViewerDropsDashboard"],
        )
        # The original constant must stay untouched.
        self.assertEqual(
            GQLOperations.ViewerDropsDashboard["variables"], {"fetchRewardCampaigns": True}
        )

        # Next scan starts directly with the variant that worked.
        self.twitch._Twitch__get_drops_dashboard()
        self.assertEqual(self.post_gql_request.call_args_list[2].args[0], alternate)

    def test_drops_dashboard_warning_names_where_the_response_stops(self):
        self.post_gql_request.return_value = {"data": {"currentUser": None}}

        with self.assertLogs("TwitchChannelPointsMiner.classes.Twitch", level="WARNING") as logs:
            self.twitch._Twitch__get_drops_dashboard()

        self.assertIn("data.currentUser is null", logs.output[0])

    def test_drops_dashboard_prefers_android_drops_login(self):
        from TwitchChannelPointsMiner.classes import Twitch as twitch_module

        twitch_module._preferred_drops_variants.clear()
        self.addCleanup(twitch_module._preferred_drops_variants.clear)
        drops_login = Mock()
        self.twitch.drops_login = drops_login
        self.post_gql_request.return_value = {
            "data": {"currentUser": {"dropCampaigns": [{"id": "a", "status": "ACTIVE"}]}}
        }

        self.assertEqual(len(self.twitch._Twitch__get_drops_dashboard()), 1)
        self.assertIs(self.post_gql_request.call_args.kwargs["login"], drops_login)

    def test_rejected_drops_login_is_reset_and_tv_client_used(self):
        from TwitchChannelPointsMiner.classes import Twitch as twitch_module

        twitch_module._preferred_drops_variants.clear()
        self.addCleanup(twitch_module._preferred_drops_variants.clear)
        self.twitch.drops_login = Mock()
        self.twitch.cookies_file = "/nonexistent/cookies/user.pkl"
        unauthorized = {"error": "Unauthorized", "status": 401, "message": "invalid token"}
        campaigns = {"data": {"currentUser": {"dropCampaigns": []}}}
        self.post_gql_request.side_effect = [unauthorized, unauthorized, campaigns]

        with self.assertLogs("TwitchChannelPointsMiner.classes.Twitch", level="WARNING"):
            self.assertEqual(self.twitch._Twitch__get_drops_dashboard(), [])

        self.assertIsNone(self.twitch.drops_login)
        self.assertIsNone(self.post_gql_request.call_args.kwargs["login"])

    @patch("TwitchChannelPointsMiner.classes.Twitch.requests.post")
    def test_drops_login_requests_use_android_client_headers(self, post):
        from TwitchChannelPointsMiner.constants import DROPS_CLIENT_ID, DROPS_CLIENT_URL

        post.return_value.json.return_value = {"data": {}}
        self.twitch.client_session = "session-id"
        drops_login = Mock(client_id=DROPS_CLIENT_ID, device_id="device-id")
        drops_login.get_auth_token.return_value = "drops-token"

        self.post_gql_patcher.stop()
        Twitch.post_gql_request(self.twitch, {"operationName": "test"}, login=drops_login)

        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "OAuth drops-token")
        self.assertEqual(headers["Client-Id"], DROPS_CLIENT_ID)
        self.assertEqual(headers["Origin"], DROPS_CLIENT_URL)
        self.assertEqual(headers["User-Agent"], USER_AGENTS["Android"]["APP"])
        self.assertNotIn("Client-Version", headers)

    def test_campaign_details_skip_malformed_responses(self):
        self.twitch.twitch_login = Mock()
        self.twitch.twitch_login.get_user_id.return_value = "123"
        self.post_gql_request.return_value = [
            {"data": {"user": {"dropCampaign": {"id": "a"}}}},
            {"data": {"user": None}},
            {"errors": [{"message": "boom"}]},
            {"data": {"user": {"dropCampaign": None}}},
        ]

        result = self.twitch._Twitch__get_campaigns_details(
            [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
        )

        self.assertEqual(result, [{"id": "a"}])

    def test_campaign_details_ignore_non_list_response(self):
        self.twitch.twitch_login = Mock()
        self.twitch.twitch_login.get_user_id.return_value = "123"
        self.post_gql_request.return_value = {}

        self.assertEqual(
            self.twitch._Twitch__get_campaigns_details([{"id": "a"}]), []
        )


if __name__ == "__main__":
    unittest.main()
