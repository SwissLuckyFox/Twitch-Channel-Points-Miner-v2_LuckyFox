import unittest
from unittest.mock import Mock, patch

from TwitchChannelPointsMiner.classes.Exceptions import (
    StreamerDoesNotExistException,
    UnexpectedTwitchResponseException,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.constants import CLIENT_ID, CLIENT_VERSION, GQLOperations


class ChannelPointsContextTests(unittest.TestCase):
    def setUp(self):
        post_gql_patcher = patch.object(Twitch, "post_gql_request")
        claim_bonus_patcher = patch.object(Twitch, "claim_bonus")
        self.post_gql_request = post_gql_patcher.start()
        self.claim_bonus = claim_bonus_patcher.start()
        self.addCleanup(post_gql_patcher.stop)
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
        self.assertEqual(
            GQLOperations.ChannelPointsContext["extensions"]["persistedQuery"][
                "sha256Hash"
            ],
            "7fe050e3761eb2cf258d70ee1a21cbd76fa8cf3d7e7b12fc437e7029d446b5e3",
        )
        self.assertEqual(
            GQLOperations.ChannelFollows["extensions"]["persistedQuery"][
                "sha256Hash"
            ],
            "eecf815273d3d949e5cf0085cc5084cd8a1b5b7b6f7990cf43cb0beadf546907",
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

    @patch("TwitchChannelPointsMiner.classes.Twitch.requests.post")
    def test_graphql_request_includes_client_integrity_context_headers(self, post):
        post.return_value.json.return_value = {"data": {}}
        self.twitch.user_agent = "test-user-agent"
        self.twitch.device_id = "device-id"
        self.twitch.client_session = "session-id"
        self.twitch.twitch_login = Mock()
        self.twitch.twitch_login.get_auth_token.return_value = "test-token"

        self.twitch.post_gql_request({"operationName": "test"})

        self.assertEqual(
            post.call_args.kwargs["headers"],
            {
                "Authorization": "OAuth test-token",
                "Client-Id": CLIENT_ID,
                "Client-Session-Id": "session-id",
                "Client-Version": CLIENT_VERSION,
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


if __name__ == "__main__":
    unittest.main()
