import unittest
from unittest.mock import Mock, patch

from TwitchChannelPointsMiner.classes.Exceptions import (
    StreamerDoesNotExistException,
    UnexpectedTwitchResponseException,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch


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
            "errors": [{"message": "upstream unavailable"}]
        }

        with self.assertRaisesRegex(
            UnexpectedTwitchResponseException, "returned no GraphQL data"
        ):
            self.twitch.load_channel_points_context(self.streamer)

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
