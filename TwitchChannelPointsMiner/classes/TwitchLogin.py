# Based on https://github.com/derrod/twl.py
# Original Copyright (c) 2020 Rodney
# The MIT License (MIT)

import copy
import logging
import os
import pickle
import time
from secrets import token_hex

import browser_cookie3
import requests

from TwitchChannelPointsMiner.classes.Exceptions import (
    WrongCookiesException,
)
from TwitchChannelPointsMiner.constants import GQLOperations, USER_AGENTS

logger = logging.getLogger(__name__)


def _oauth_error_text(response):
    """Twitch's short error reason from an OAuth error response (contains no secrets)."""
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    message = data.get("message") or data.get("error_description") or data.get("error")
    return f" ({str(message)[:200]})" if message else ""


class TwitchLogin(object):
    __slots__ = [
        "client_id",
        "device_id",
        "token",
        "login_check_result",
        "session",
        "username",
        "user_id",
        "email",
        "cookies",
        "oauth_profile",
    ]

    def __init__(self, client_id, username, user_agent, password=None):
        self.client_id = client_id
        self.device_id = token_hex(16)
        self.token = None
        self.login_check_result = False
        self.session = requests.session()
        self.session.headers.update(
            {
                "Client-ID": self.client_id,
                "X-Device-Id": self.device_id,
                "User-Agent": user_agent,
            }
        )
        self.username = username
        self.user_id = None
        self.email = None

        self.cookies = []
        # Optional overrides for a non-TV client: origin, referer, user_agent, scopes,
        # label (log prefix), extra (logging extra for the code message), keep_waiting (callable).
        self.oauth_profile = None

    def __profile(self):
        return getattr(self, "oauth_profile", None) or {}

    def login_flow(self):
        profile = self.__profile()
        label = profile.get("label", "")
        logger.info("%sStarting Twitch device authorization.", label)
        device_response = self.send_oauth_request(
            "https://id.twitch.tv/oauth2/device",
            {
                "client_id": self.client_id,
                "scopes": profile.get(
                    "scopes",
                    "channel_read chat:read user_blocks_edit "
                    "user_blocks_read user_follows_edit user_read",
                ),
            },
        )
        if device_response.status_code != 200:
            raise RuntimeError(
                "Twitch device authorization request failed with HTTP "
                f"{device_response.status_code}{_oauth_error_text(device_response)}."
            )

        try:
            device_data = device_response.json()
            device_code = device_data["device_code"]
            user_code = device_data["user_code"]
            interval = int(device_data["interval"])
            expires_in = int(device_data["expires_in"])
            verification_uri = device_data.get(
                "verification_uri", "https://www.twitch.tv/activate"
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                "Twitch returned an invalid device authorization response."
            ) from error

        if interval <= 0 or expires_in <= 0:
            raise RuntimeError(
                "Twitch returned invalid device authorization timing values."
            )

        # Pre-formatted: notification handlers forward record.msg without its args.
        logger.info(
            f"{label}Open {verification_uri} and enter this code: {user_code}",
            extra=profile.get("extra"),
        )
        logger.info("The code expires in %d minutes.", max(1, expires_in // 60))

        keep_waiting = profile.get("keep_waiting")
        deadline = time.monotonic() + expires_in
        token_request = {
            "client_id": self.client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        while time.monotonic() < deadline:
            time.sleep(interval)
            if keep_waiting is not None and not keep_waiting():
                raise RuntimeError("Twitch device authorization was cancelled.")
            if time.monotonic() >= deadline:
                break

            token_response = self.send_oauth_request(
                "https://id.twitch.tv/oauth2/token", token_request
            )
            try:
                token_data = token_response.json()
            except ValueError as error:
                raise RuntimeError(
                    "Twitch returned a non-JSON device authorization response."
                ) from error

            if token_response.status_code == 200:
                access_token = token_data.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise RuntimeError(
                        "Twitch device authorization response did not contain an "
                        "access token."
                    )
                self.set_token(access_token)
                if self.check_login():
                    return True
                raise RuntimeError(
                    "Twitch issued an access token, but the miner could not "
                    "validate the account."
                )

            oauth_error = token_data.get("error")
            if oauth_error == "authorization_pending":
                continue
            if oauth_error == "slow_down":
                interval += 5
                continue
            if oauth_error == "access_denied":
                raise RuntimeError("Twitch device authorization was denied.")
            if oauth_error == "expired_token":
                break
            raise RuntimeError(
                "Twitch device authorization polling failed with HTTP "
                f"{token_response.status_code} (OAuth error: "
                f"{oauth_error or 'unknown'})."
            )

        raise RuntimeError(
            "Twitch device authorization expired before it was approved. "
            "Restart the container and complete a new authorization."
        )

    def set_token(self, new_token):
        self.token = new_token
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def send_oauth_request(self, url, form_data):
        profile = self.__profile()
        try:
            response = self.session.post(
                url,
                data=form_data,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "Accept-Language": "en-US",
                    "Cache-Control": "no-cache",
                    "Client-Id": self.client_id,
                    "Host": "id.twitch.tv",
                    "Origin": profile.get("origin", "https://android.tv.twitch.tv"),
                    "Pragma": "no-cache",
                    "Referer": profile.get("referer", "https://android.tv.twitch.tv/"),
                    "User-Agent": profile.get("user_agent", USER_AGENTS["Android"]["TV"]),
                    "X-Device-Id": self.device_id,
                },
                timeout=30,
            )
        except requests.exceptions.RequestException as error:
            raise RuntimeError(
                "Could not reach Twitch's device authorization endpoint. Check the "
                "container's network/DNS/TLS connection and try again."
            ) from error

        return response

    def login_flow_backup(self):
        """Backup OAuth login flow in case manual captcha solving is required"""
        browser = input(
            "What browser do you use? Chrome (1), Firefox (2), Other (3): "
        ).strip()
        if browser not in ("1", "2"):
            logger.info("Your browser is unsupported, sorry.")
            return None

        input(
            "Please login inside your browser of choice (NOT incognito mode) and press Enter..."
        )
        logger.info("Loading cookies saved on your computer...")
        twitch_domain = ".twitch.tv"
        if browser == "1":  # chrome
            cookie_jar = browser_cookie3.chrome(domain_name=twitch_domain)
        else:
            cookie_jar = browser_cookie3.firefox(domain_name=twitch_domain)
        cookies_dict = requests.utils.dict_from_cookiejar(cookie_jar)
        self.username = cookies_dict.get("login")
        return cookies_dict.get("auth-token")

    def check_login(self):
        if self.login_check_result:
            return self.login_check_result
        if self.token is None:
            return False

        self.login_check_result = self.__set_user_id()
        return self.login_check_result

    def save_cookies(self, cookies_file):
        cookies_dict = self.session.cookies.get_dict()
        cookies_dict["auth-token"] = self.token
        if "persistent" not in cookies_dict:  # saving user id cookies
            cookies_dict["persistent"] = self.user_id

        self.cookies = []
        for cookie_name, value in cookies_dict.items():
            self.cookies.append({"name": cookie_name, "value": value})
        pickle.dump(self.cookies, open(cookies_file, "wb"))

    def get_cookie_value(self, key):
        for cookie in self.cookies:
            if cookie["name"] == key:
                if cookie["value"] is not None:
                    return cookie["value"]
        return None

    def load_cookies(self, cookies_file):
        if os.path.isfile(cookies_file):
            self.cookies = pickle.load(open(cookies_file, "rb"))
        else:
            raise WrongCookiesException("There must be a cookies file!")

    def get_user_id(self):
        persistent = self.get_cookie_value("persistent")
        user_id = (
            int(persistent.split("%")[0]) if persistent is not None else self.user_id
        )
        if user_id is None:
            if self.__set_user_id() is True:
                return self.user_id
        return user_id

    def __set_user_id(self):
        json_data = copy.deepcopy(GQLOperations.GetIDFromLogin)
        json_data["variables"]["login"] = self.username
        response = self.session.post(
            GQLOperations.url, json=json_data, timeout=30
        )

        if response.status_code == 200:
            json_response = response.json()
            if (
                "data" in json_response
                and "user" in json_response["data"]
                and json_response["data"]["user"]["id"] is not None
            ):
                self.user_id = json_response["data"]["user"]["id"]
                return True
        return False

    def get_auth_token(self):
        return self.get_cookie_value("auth-token")

    def set_device_id(self, device_id):
        self.device_id = device_id
        self.session.headers["X-Device-Id"] = device_id

    def use_web_device_id(self, url):
        """Adopt the device ID Twitch's website assigns (unique_id cookie), like the official apps."""
        saved = self.get_cookie_value("unique_id")
        if saved:
            self.set_device_id(saved)
            return True
        try:
            self.session.get(url, headers={"Accept": "text/html"}, timeout=30)
        except requests.exceptions.RequestException:
            return False
        for cookie in self.session.cookies:
            if cookie.name == "unique_id" and cookie.value:
                self.set_device_id(cookie.value)
                return True
        return False
