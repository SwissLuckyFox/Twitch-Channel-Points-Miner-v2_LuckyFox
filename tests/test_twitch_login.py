import unittest
from unittest.mock import Mock, patch

from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.constants import CLIENT_ID, USER_AGENTS


def make_login():
    login = object.__new__(TwitchLogin)
    login.client_id = CLIENT_ID
    login.device_id = "test-device-id"
    login.token = None
    login.login_check_result = False
    login.session = Mock()
    login.username = "test-user"
    login.user_id = None
    login.email = None
    login.cookies = []
    return login


class TwitchLoginTests(unittest.TestCase):
    def test_device_request_uses_twitch_device_flow_endpoint_and_headers(self):
        login = make_login()
        response = Mock()
        login.session.post.return_value = response

        result = login.send_oauth_request(
            "https://id.twitch.tv/oauth2/device", {"client_id": CLIENT_ID}
        )

        self.assertIs(result, response)
        login.session.post.assert_called_once()
        args, kwargs = login.session.post.call_args
        self.assertEqual(args, ("https://id.twitch.tv/oauth2/device",))
        self.assertEqual(kwargs["data"], {"client_id": CLIENT_ID})
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(
            kwargs["headers"]["User-Agent"], USER_AGENTS["Android"]["TV"]
        )
        self.assertEqual(kwargs["headers"]["X-Device-Id"], "test-device-id")

    @patch("TwitchChannelPointsMiner.classes.TwitchLogin.time.sleep")
    def test_device_login_polls_until_approved(self, sleep):
        login = make_login()
        device_response = Mock(status_code=200)
        device_response.json.return_value = {
            "device_code": "private-device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://www.twitch.tv/activate",
            "interval": 5,
            "expires_in": 600,
        }
        pending_response = Mock(status_code=400)
        pending_response.json.return_value = {"error": "authorization_pending"}
        token_response = Mock(status_code=200)
        token_response.json.return_value = {"access_token": "test-access-token"}
        # TwitchLogin uses __slots__, so methods must be patched on the class.
        with patch.object(
            TwitchLogin,
            "send_oauth_request",
            side_effect=[device_response, pending_response, token_response],
        ) as send_oauth_request, patch.object(
            TwitchLogin, "check_login", return_value=True
        ) as check_login, self.assertLogs(
            "TwitchChannelPointsMiner.classes.TwitchLogin", level="INFO"
        ) as captured_logs, patch(
            "TwitchChannelPointsMiner.classes.TwitchLogin.time.monotonic",
            return_value=0,
        ):
            result = login.login_flow()

        self.assertTrue(result)
        self.assertEqual(login.token, "test-access-token")
        check_login.assert_called_once_with()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(send_oauth_request.call_count, 3)
        self.assertNotIn("private-device-code", "\n".join(captured_logs.output))

    def test_oauth_profile_overrides_client_headers(self):
        login = make_login()
        login.oauth_profile = {
            "origin": "https://www.twitch.tv",
            "referer": "https://www.twitch.tv",
            "user_agent": USER_AGENTS["Android"]["APP"],
        }

        login.send_oauth_request("https://id.twitch.tv/oauth2/device", {})

        headers = login.session.post.call_args.kwargs["headers"]
        self.assertEqual(headers["Origin"], "https://www.twitch.tv")
        self.assertEqual(headers["Referer"], "https://www.twitch.tv")
        self.assertEqual(headers["User-Agent"], USER_AGENTS["Android"]["APP"])

    @patch("TwitchChannelPointsMiner.classes.TwitchLogin.time.sleep")
    def test_device_login_logs_code_preformatted_and_can_be_cancelled(self, sleep):
        login = make_login()
        login.oauth_profile = {
            "label": "Drops login: ",
            "scopes": "",
            "keep_waiting": lambda: False,
        }
        device_response = Mock(status_code=200)
        device_response.json.return_value = {
            "device_code": "private-device-code",
            "user_code": "ABCD-EFGH",
            "interval": 1,
            "expires_in": 600,
        }
        with patch.object(
            TwitchLogin, "send_oauth_request", return_value=device_response
        ) as send_oauth_request, patch(
            "TwitchChannelPointsMiner.classes.TwitchLogin.time.monotonic",
            return_value=0,
        ), self.assertLogs(
            "TwitchChannelPointsMiner.classes.TwitchLogin", level="INFO"
        ) as captured_logs:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                login.login_flow()

        self.assertEqual(send_oauth_request.call_args.args[1]["scopes"], "")
        code_records = [r for r in captured_logs.records if "ABCD-EFGH" in r.msg]
        self.assertEqual(len(code_records), 1)
        self.assertTrue(code_records[0].msg.startswith("Drops login: Open "))

    def test_device_login_reports_denial_without_exposing_codes(self):
        login = make_login()
        device_response = Mock(status_code=200)
        device_response.json.return_value = {
            "device_code": "private-device-code",
            "user_code": "ABCD-EFGH",
            "interval": 1,
            "expires_in": 60,
        }
        denied_response = Mock(status_code=400)
        denied_response.json.return_value = {"error": "access_denied"}
        with patch.object(
            TwitchLogin,
            "send_oauth_request",
            side_effect=[device_response, denied_response],
        ), patch(
            "TwitchChannelPointsMiner.classes.TwitchLogin.time.monotonic",
            return_value=0,
        ), patch("TwitchChannelPointsMiner.classes.TwitchLogin.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "authorization was denied"):
                login.login_flow()

    def test_device_request_failure_includes_twitch_reason(self):
        login = make_login()
        failed = Mock(status_code=400)
        failed.json.return_value = {"status": 400, "message": "invalid client"}
        with patch.object(TwitchLogin, "send_oauth_request", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, r"HTTP 400 \(invalid client\)"):
                login.login_flow()


if __name__ == "__main__":
    unittest.main()
