import unittest
from unittest.mock import Mock

import requests

from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin


def make_login(response):
    login = object.__new__(TwitchLogin)
    login.session = Mock()
    login.session.post.return_value = response
    return login


class TwitchLoginRequestTests(unittest.TestCase):
    def test_login_request_returns_json_and_uses_timeout(self):
        response = Mock()
        response.json.return_value = {"access_token": "test-access-token"}
        login = make_login(response)

        result = login.send_login_request({"username": "test-user"})

        self.assertEqual(result, {"access_token": "test-access-token"})
        login.session.post.assert_called_once_with(
            "https://passport.twitch.tv/login",
            json={"username": "test-user"},
            timeout=30,
        )
        response.raise_for_status.assert_called_once_with()

    def test_non_json_login_response_reports_metadata_without_body(self):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "text/html"}
        response.json.side_effect = requests.exceptions.JSONDecodeError(
            "invalid json", "<html>private response</html>", 0
        )
        login = make_login(response)

        with self.assertRaisesRegex(
            RuntimeError,
            r"non-JSON response \(HTTP 200, Content-Type: text/html\)",
        ) as raised:
            login.send_login_request({"username": "test-user"})

        self.assertNotIn("private response", str(raised.exception))

    def test_http_error_reports_status(self):
        response = Mock()
        response.status_code = 403
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=response
        )
        login = make_login(response)

        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            login.send_login_request({"username": "test-user"})


if __name__ == "__main__":
    unittest.main()
