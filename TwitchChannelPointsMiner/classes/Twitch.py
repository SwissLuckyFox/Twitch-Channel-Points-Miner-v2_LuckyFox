# For documentation on Twitch GraphQL API see:
# https://www.apollographql.com/docs/
# https://github.com/mauricew/twitch-graphql-api
# Full list of available methods: https://azr.ivr.fi/schema/query.doc.html (a bit outdated)


import copy
import logging
import os
import random
import re
import time
from pathlib import Path
from secrets import token_hex

import requests

from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.entities.Drop import Drop, parse_twitch_datetime
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Exceptions import (
    StreamerDoesNotExistException,
    StreamerIsOfflineException,
    UnexpectedTwitchResponseException,
)
from TwitchChannelPointsMiner.classes.Settings import (
    Events,
    FollowersOrder,
    Priority,
    Settings,
)
from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.constants import (
    CLIENT_ID,
    CLIENT_VERSION,
    DROPS_CLIENT_ID,
    DROPS_CLIENT_URL,
    GQLOperations,
    URL,
    USER_AGENTS,
)
from TwitchChannelPointsMiner.utils import (
    _millify,
    create_chunks,
    internet_connection_available,
)

logger = logging.getLogger(__name__)

# Game-directory lookup for drop campaigns that accept any participating channel.
DIRECTORY_SEARCH_INTERVAL = 10 * 60
DIRECTORY_SEARCH_LIMIT = 10
DIRECTORY_CHANNELS_PER_CAMPAIGN = 3


def _graphql_error_details(response):
    if not isinstance(response, dict):
        return ""

    details = []
    errors = response.get("errors", [])
    if isinstance(errors, list):
        for error in errors[:3]:
            if not isinstance(error, dict):
                continue
            message = error.get("message")
            extensions = error.get("extensions")
            code = extensions.get("code") if isinstance(extensions, dict) else None
            parts = []
            if isinstance(code, str) and code:
                parts.append(code)
            if isinstance(message, str) and message:
                parts.append(" ".join(message.split())[:300])
            if parts:
                details.append(": ".join(parts))

    return f" GraphQL errors: {'; '.join(details)}" if details else ""


# Operation name -> persisted query hash that replaced a stale primary hash.
_preferred_hashes = {}
# Drops operation name -> (client, request variant) that last returned data.
_preferred_drops_variants = {}


def _alternate_drops_query(operation):
    """TwitchDropsMiner's variant of a drops query: newer hash, fetchRewardCampaigns=False."""
    variant = copy.deepcopy(operation)
    name = variant.get("operationName")
    new_hash = GQLOperations.FallbackHashes.get(name)
    if new_hash:
        variant["extensions"]["persistedQuery"]["sha256Hash"] = new_hash
    variant["variables"] = dict(variant.get("variables") or {})
    variant["variables"]["fetchRewardCampaigns"] = False
    return variant


def _describe_response(response, path):
    """Short, token-free description of where a GraphQL response stops, e.g. 'data.currentUser is null'."""
    if response in (None, {}):
        return "empty response (request failed)"
    if not isinstance(response, dict):
        return f"unexpected response type {type(response).__name__}"
    errors = _graphql_error_details(response)
    if errors:
        return errors.strip()
    node = response
    walked = []
    for key in path:
        walked.append(key)
        if not isinstance(node, dict):
            return f"{'.'.join(walked[:-1])} is not an object"
        if key not in node:
            return f"{'.'.join(walked)} is missing"
        node = node[key]
        if node is None:
            return f"{'.'.join(walked)} is null"
    return f"{'.'.join(walked)} has type {type(node).__name__}"


def _persisted_query_not_found(response):
    if not isinstance(response, dict):
        return False
    errors = response.get("errors")
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if (
            error.get("message") == "PersistedQueryNotFound"
            or code == "PERSISTED_QUERY_NOT_FOUND"
        ):
            return True
    return False


def _apply_hashes(json_data, hashes):
    """Return json_data with persisted query hashes replaced from `hashes` (copy if changed)."""
    operations = json_data if isinstance(json_data, list) else [json_data]
    if not any(
        isinstance(operation, dict) and operation.get("operationName") in hashes
        for operation in operations
    ):
        return json_data

    patched = copy.deepcopy(json_data)
    for operation in patched if isinstance(patched, list) else [patched]:
        if not isinstance(operation, dict):
            continue
        new_hash = hashes.get(operation.get("operationName"))
        extensions = operation.get("extensions")
        persisted = (
            extensions.get("persistedQuery") if isinstance(extensions, dict) else None
        )
        if new_hash and isinstance(persisted, dict):
            persisted["sha256Hash"] = new_hash
    return patched


def _with_fallback_hashes(json_data, response):
    """Return a retry payload using fallback hashes, or None when no retry makes sense."""
    responses = response if isinstance(response, list) else [response]
    if not any(_persisted_query_not_found(r) for r in responses):
        return None
    patched = _apply_hashes(json_data, GQLOperations.FallbackHashes)
    return None if patched == json_data else patched


def _game_slug(name):
    # Same slug rules Twitch uses for directory URLs, e.g. "Alien: Isolation" -> "alien-isolation".
    slug = re.sub(r"'", "", (name or "").lower())
    slug = re.sub(r"\W+", "-", slug)
    return re.sub(r"-{2,}", "-", slug.strip("-"))


WATCH_REASONS = {
    Priority.STREAK: "watch streak",
    Priority.DROPS: "drop",
    Priority.SUBSCRIBED: "subscription points bonus",
    Priority.ORDER: "streamer list priority",
    Priority.POINTS_ASCENDING: "fewest channel points",
    Priority.POINTS_DESCEDING: "most channel points",
}


def _leave_reason(streamer, previous_reason):
    if streamer.is_online is not True:
        return "went offline"
    if previous_reason == WATCH_REASONS[Priority.STREAK]:
        if streamer.stream.watch_streak_missing is False:
            return "watch streak collected"
        if streamer.stream.minute_watched >= 7:
            return "watched long enough for the watch streak"
    if previous_reason == WATCH_REASONS[Priority.DROPS] and not streamer.drops_condition():
        return "drop earned"
    return "a higher priority channel took the slot"


def describe_watch_change(previous, current):
    """
    previous/current map username -> (streamer, reason).
    Returns a human readable summary when the mined channels or reasons change.
    """
    if {name: reason for name, (_, reason) in previous.items()} == {
        name: reason for name, (_, reason) in current.items()
    }:
        return None

    lines = []
    for name, (streamer, reason) in previous.items():
        if name not in current:
            lines.append(f"Stopped mining {name}: {_leave_reason(streamer, reason)}")
    for name, (_, reason) in current.items():
        if name not in previous:
            lines.append(f"Started mining {name}: {reason}")
        elif previous[name][1] != reason:
            lines.append(f"Still mining {name}, now for: {reason} (was: {previous[name][1]})")

    if current:
        summary = ", ".join(f"{name} ({reason})" for name, (_, reason) in current.items())
        lines.append(f"Mining now: {summary}")
    else:
        lines.append("Mining now: nothing (no streamer online)")
    return "\n".join(lines)


def describe_drop_scan(active_campaigns, stats):
    """Summary of an hourly drop-campaign scan; stats holds the skip counters."""
    listed = stats.get("listed", stats.get("checked", 0))
    checked = stats.get("checked", 0)
    if listed == 0:
        return (
            "Scanned for drops: Twitch returned no active drop campaigns "
            "(the request probably failed, see the warning above)"
        )
    if checked == 0:
        return (
            f"Scanned for drops: Twitch listed {listed} active campaigns but their "
            "details could not be loaded (see the warning above)"
        )

    skipped = []
    if listed > checked:
        skipped.append(f"{listed - checked} could not be loaded")
    if stats.get("already_earned"):
        skipped.append(f"{stats['already_earned']} already earned")
    if stats.get("no_watch_drops"):
        skipped.append(f"{stats['no_watch_drops']} without watch-time drops")
    if stats.get("not_linked"):
        skipped.append(f"{stats['not_linked']} need a linked game account")
    details = f"checked {checked} campaigns"
    if skipped:
        details += "; " + ", ".join(skipped)

    if not active_campaigns:
        return f"Scanned for drops: no new watch-time drops found ({details})"

    lines = [
        f"Scanned for drops: found {len(active_campaigns)} campaign(s) with watch-time drops ({details})"
    ]
    for campaign in active_campaigns:
        rewards = ", ".join(
            f"{drop.benefit or drop.name} ({drop.minutes_required} min)"
            for drop in campaign.drops
        )
        where = (
            "any live channel in the category"
            if campaign.any_channel
            else f"{len(campaign.channels)} participating channel(s)"
        )
        lines.append(
            f"- {campaign.name} | Category: {campaign.game_name() or 'unknown'} "
            f"| Rewards: {rewards} | Watch on: {where}"
        )
    lines.append("Checking which participating channels are live...")
    return "\n".join(lines)


class Twitch(object):
    __slots__ = [
        "cookies_file",
        "user_agent",
        "twitch_login",
        "running",
        "device_id",
        "client_session",
        "client_version",
        "client_version_loaded",
        "twilight_build_id_pattern",
        "drops_login",
    ]

    def __init__(self, username, user_agent, password=None):
        cookies_path = os.path.join(Path().absolute(), "cookies")
        Path(cookies_path).mkdir(parents=True, exist_ok=True)
        self.cookies_file = os.path.join(cookies_path, f"{username}.pkl")
        self.user_agent = user_agent
        self.device_id = token_hex(16)
        self.client_session = token_hex(16)
        self.client_version = CLIENT_VERSION
        self.client_version_loaded = False
        self.twilight_build_id_pattern = re.compile(
            r'window\.__twilightBuildID\s*=\s*"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"'
        )
        self.twitch_login = TwitchLogin(
            CLIENT_ID, username, self.user_agent, password=password
        )
        self.twitch_login.device_id = self.device_id
        self.twitch_login.session.headers["X-Device-Id"] = self.device_id
        self.drops_login = None
        self.running = True

    def login(self):
        if os.path.isfile(self.cookies_file) is False:
            if self.twitch_login.login_flow():
                self.twitch_login.save_cookies(self.cookies_file)
        else:
            self.twitch_login.load_cookies(self.cookies_file)
            self.twitch_login.set_token(self.twitch_login.get_auth_token())

    def __drops_cookies_file(self):
        return re.sub(r"\.pkl$", "_drops.pkl", self.cookies_file)

    def ensure_drops_login(self):
        """
        Twitch only hands the drop campaign list to its Android app client, so drops
        requests use a separate token for that client. The first time this shows a
        second activation code (also sent to Telegram); the token is then saved.
        """
        if getattr(self, "drops_login", None) is not None:
            return True

        login = TwitchLogin(
            DROPS_CLIENT_ID,
            self.twitch_login.username,
            USER_AGENTS["Android"]["APP"],
            None,
        )
        login.set_device_id(self.device_id)
        login.oauth_profile = {
            "origin": DROPS_CLIENT_URL,
            "referer": DROPS_CLIENT_URL,
            "user_agent": USER_AGENTS["Android"]["APP"],
            "scopes": "",
            "label": "Drops login (one-time, Twitch Android app): ",
            "extra": {"emoji": ":key:", "event": Events.DROP_SCAN},
            "keep_waiting": lambda: self.running,
        }
        cookies_file = self.__drops_cookies_file()
        try:
            if os.path.isfile(cookies_file):
                login.load_cookies(cookies_file)
                login.use_web_device_id(DROPS_CLIENT_URL)
                login.set_token(login.get_auth_token())
            else:
                logger.info(
                    "Drop scan needs a second, one-time Twitch login: Twitch only lists "
                    "drop campaigns for its Android app. Enter the code below at the same "
                    "activation page as the first login.",
                    extra={"emoji": ":key:", "event": Events.DROP_SCAN},
                )
                login.use_web_device_id(DROPS_CLIENT_URL)
                if not login.login_flow():
                    return False
                login.save_cookies(cookies_file)
                logger.info(
                    "Drops login done, the token is saved for future starts.",
                    extra={"emoji": ":key:", "event": Events.DROP_SCAN},
                )
        except Exception as error:
            logger.warning(
                "Drops login failed (%s). Drop scans use the normal login until the next try.",
                error,
            )
            return False
        if not login.get_auth_token():
            return False
        self.drops_login = login
        return True

    def __reset_drops_login(self):
        self.drops_login = None
        try:
            os.remove(self.__drops_cookies_file())
        except OSError:
            pass
        logger.warning(
            "Twitch rejected the saved drops login. A new activation code will be shown on the next drop scan."
        )

    # === STREAMER / STREAM / INFO === #
    def update_stream(self, streamer):
        if streamer.stream.update_required() is True:
            stream_info = self.get_stream_info(streamer)
            if stream_info is not None:
                streamer.stream.update(
                    broadcast_id=stream_info["stream"]["id"],
                    title=stream_info["broadcastSettings"]["title"],
                    game=stream_info["broadcastSettings"]["game"],
                    tags=stream_info["stream"]["tags"],
                    viewers_count=stream_info["stream"]["viewersCount"],
                )

                event_properties = {
                    "channel_id": streamer.channel_id,
                    "broadcast_id": streamer.stream.broadcast_id,
                    "player": "site",
                    "user_id": self.twitch_login.get_user_id(),
                }

                if (
                    streamer.stream.game_name() is not None
                    and streamer.settings.claim_drops is True
                ):
                    event_properties["game"] = streamer.stream.game_name()
                    # Update also the campaigns_ids so we are sure to tracking the correct campaign
                    streamer.stream.campaigns_ids = (
                        self.__get_campaign_ids_from_streamer(streamer)
                    )

                streamer.stream.payload = [
                    {"event": "minute-watched", "properties": event_properties}
                ]

    def get_spade_url(self, streamer):
        try:
            # The Android TV user agent used for GraphQL gets a page without the
            # settings script, so the channel page must be fetched as a desktop browser.
            headers = {"User-Agent": USER_AGENTS["Linux"]["FIREFOX"]}
            main_page_request = requests.get(
                streamer.streamer_url, headers=headers, timeout=30
            )
            regex_settings = (
                r"(https://static\.twitchcdn\.net/config/settings.*?js"
                r"|https://assets\.twitch\.tv/config/settings.*?\.js)"
            )
            settings_match = re.search(regex_settings, main_page_request.text)
            if settings_match is None:
                logger.warning(
                    f"Could not find Twitch settings script for {streamer.username}; "
                    "watch-time tracking is unavailable for this stream."
                )
                return

            settings_request = requests.get(
                settings_match.group(1), headers=headers, timeout=30
            )
            spade_match = re.search('"spade_url":"(.*?)"', settings_request.text)
            if spade_match is None:
                logger.warning(
                    f"Could not find Twitch spade URL for {streamer.username}; "
                    "watch-time tracking is unavailable for this stream."
                )
                return
            streamer.stream.spade_url = spade_match.group(1)
        except requests.exceptions.RequestException as e:
            logger.error(f"Something went wrong during extraction of 'spade_url': {e}")

    def get_broadcast_id(self, streamer):
        json_data = copy.deepcopy(GQLOperations.WithIsStreamLiveQuery)
        json_data["variables"] = {"id": streamer.channel_id}
        response = self.post_gql_request(json_data)
        if response != {}:
            stream = response["data"]["user"]["stream"]
            if stream is not None:
                return stream["id"]
            else:
                raise StreamerIsOfflineException

    def get_stream_info(self, streamer):
        json_data = copy.deepcopy(GQLOperations.VideoPlayerStreamInfoOverlayChannel)
        json_data["variables"] = {"channel": streamer.username}
        response = self.post_gql_request(json_data)
        if response != {}:
            if response["data"]["user"]["stream"] is None:
                raise StreamerIsOfflineException
            else:
                return response["data"]["user"]

    def check_streamer_online(self, streamer):
        if time.time() < streamer.offline_at + 60:
            return

        if streamer.is_online is False:
            try:
                self.get_spade_url(streamer)
                self.update_stream(streamer)
            except StreamerIsOfflineException:
                streamer.set_offline()
            else:
                streamer.set_online()
        else:
            try:
                self.update_stream(streamer)
            except StreamerIsOfflineException:
                streamer.set_offline()

    def get_channel_id(self, streamer_username):
        json_data = copy.deepcopy(GQLOperations.GetIDFromLogin)
        json_data["variables"]["login"] = streamer_username
        json_response = self.post_gql_request(json_data)
        if not isinstance(json_response, dict) or not isinstance(
            json_response.get("data"), dict
        ):
            raise UnexpectedTwitchResponseException(
                f"Twitch returned no GraphQL data while looking up "
                f"{streamer_username}.{_graphql_error_details(json_response)}"
            )

        user = json_response["data"].get("user")
        if user is None:
            raise StreamerDoesNotExistException
        if not isinstance(user, dict) or not user.get("id"):
            raise UnexpectedTwitchResponseException(
                f"Twitch returned incomplete user data while looking up "
                f"{streamer_username}.{_graphql_error_details(json_response)}"
            )
        return user["id"]

    def get_channel_logins(self, channel_ids):
        channel_logins = {}
        for chunk in create_chunks(channel_ids, 20):
            requests_data = [
                {
                    "operationName": "UserLoginByID",
                    "query": "query UserLoginByID($id: ID!) { user(id: $id) { id login } }",
                    "variables": {"id": channel_id},
                }
                for channel_id in chunk
            ]
            responses = self.post_gql_request(requests_data)
            if not isinstance(responses, list):
                logger.error("Unexpected response while resolving drop channel IDs")
                continue

            for response in responses:
                if not isinstance(response, dict):
                    logger.warning("Invalid user response while resolving a drop channel ID")
                    continue
                user = (response.get("data") or {}).get("user")
                if (
                    isinstance(user, dict)
                    and user.get("id")
                    and user.get("login")
                ):
                    channel_logins[user["id"]] = user["login"].lower()
                else:
                    logger.debug("Twitch did not return a login for a drop channel ID")
        return channel_logins

    def get_followers(
        self, limit: int = 100, order: FollowersOrder = FollowersOrder.ASC
    ):
        json_data = copy.deepcopy(GQLOperations.ChannelFollows)
        json_data["variables"] = {"limit": limit, "order": str(order)}
        has_next = True
        last_cursor = ""
        follows = []
        while has_next is True:
            json_data["variables"]["cursor"] = last_cursor
            json_response = self.post_gql_request(json_data)
            if not isinstance(json_response, dict) or not isinstance(
                json_response.get("data"), dict
            ):
                raise UnexpectedTwitchResponseException(
                    "Twitch returned no GraphQL data while loading your followed "
                    f"channels.{_graphql_error_details(json_response)}"
                )

            try:
                follows_response = json_response["data"]["user"]["follows"]
                edges = follows_response["edges"]
                page_info = follows_response["pageInfo"]
                has_next = page_info["hasNextPage"]
            except (KeyError, TypeError) as error:
                raise UnexpectedTwitchResponseException(
                    "Twitch returned incomplete follower data while loading your "
                    f"followed channels.{_graphql_error_details(json_response)}"
                ) from error

            if not isinstance(edges, list) or not isinstance(has_next, bool):
                raise UnexpectedTwitchResponseException(
                    "Twitch returned invalid follower data while loading your "
                    f"followed channels.{_graphql_error_details(json_response)}"
                )

            last_cursor = None
            for edge in edges:
                try:
                    follows.append(edge["node"]["login"].lower())
                    last_cursor = edge["cursor"]
                except (KeyError, TypeError, AttributeError) as error:
                    raise UnexpectedTwitchResponseException(
                        "Twitch returned an invalid followed-channel entry."
                    ) from error
        return follows

    def update_raid(self, streamer, raid):
        if streamer.raid != raid:
            streamer.raid = raid
            json_data = copy.deepcopy(GQLOperations.JoinRaid)
            json_data["variables"] = {"input": {"raidID": raid.raid_id}}
            self.post_gql_request(json_data)

            logger.info(
                f"Joining raid from {streamer} to {raid.target_login}!",
                extra={"emoji": ":performing_arts:", "event": Events.JOIN_RAID},
            )

    def viewer_is_mod(self, streamer):
        json_data = copy.deepcopy(GQLOperations.ModViewChannelQuery)
        json_data["variables"] = {"channelLogin": streamer.username}
        response = self.post_gql_request(json_data)
        try:
            streamer.viewer_is_mod = response["data"]["user"]["self"]["isModerator"]
        except (ValueError, KeyError):
            streamer.viewer_is_mod = False

    # === 'GLOBALS' METHODS === #
    # Create chunk of sleep of speed-up the break loop after CTRL+C
    def __chuncked_sleep(self, seconds, chunk_size=3):
        sleep_time = max(seconds, 0) / chunk_size
        for i in range(0, chunk_size):
            time.sleep(sleep_time)
            if self.running is False:
                break

    def __check_connection_handler(self, chunk_size):
        # The success rate It's very hight usually. Why we have failed?
        # Check internet connection ...
        while internet_connection_available() is False:
            random_sleep = random.randint(1, 3)
            logger.warning(
                f"No internet connection available! Retry after {random_sleep}m"
            )
            self.__chuncked_sleep(random_sleep * 60, chunk_size=chunk_size)

    def post_gql_request(self, json_data, login=None):
        json_data = _apply_hashes(json_data, _preferred_hashes)
        response = self.__send_gql_request(json_data, login)
        fallback_data = _with_fallback_hashes(json_data, response)
        if fallback_data is not None:
            logger.debug("Retrying GraphQL request with fallback persisted query hash")
            fallback_response = self.__send_gql_request(fallback_data, login)
            responses = (
                fallback_response
                if isinstance(fallback_response, list)
                else [fallback_response]
            )
            if fallback_response not in [{}, None] and not any(
                _persisted_query_not_found(r) for r in responses
            ):
                for operation in (
                    fallback_data if isinstance(fallback_data, list) else [fallback_data]
                ):
                    name = operation.get("operationName")
                    if name in GQLOperations.FallbackHashes:
                        _preferred_hashes[name] = GQLOperations.FallbackHashes[name]
                return fallback_response
        return response

    def __send_gql_request(self, json_data, login=None):
        if login is None:
            headers = {
                "Authorization": f"OAuth {self.twitch_login.get_auth_token()}",
                "Client-Id": CLIENT_ID,
                "Client-Session-Id": self.client_session,
                "Client-Version": self.update_client_version(),
                "User-Agent": self.user_agent,
                "X-Device-Id": self.device_id,
            }
        else:
            headers = {
                "Authorization": f"OAuth {login.get_auth_token()}",
                "Client-Id": login.client_id,
                "Client-Session-Id": self.client_session,
                "Origin": DROPS_CLIENT_URL,
                "Referer": DROPS_CLIENT_URL,
                "User-Agent": USER_AGENTS["Android"]["APP"],
                "X-Device-Id": login.device_id,
            }
        try:
            response = requests.post(
                GQLOperations.url,
                json=json_data,
                headers=headers,
            )
            logger.debug(
                f"Data: {json_data}, Status code: {response.status_code}, Content: {response.text}"
            )
            return response.json()
        except requests.exceptions.RequestException as e:
            if isinstance(json_data, list):
                operation_name = (
                    json_data[0].get("operationName", "batch")
                    if json_data
                    else "batch"
                )
            else:
                operation_name = json_data.get("operationName", "unknown")
            logger.error(f"Error with GQLOperations ({operation_name}): {e}")
            return {}

    def update_client_version(self):
        if self.client_version_loaded:
            return self.client_version

        self.client_version_loaded = True
        try:
            response = requests.get(URL, timeout=15)
            response.raise_for_status()
        except requests.exceptions.RequestException as error:
            logger.debug("Could not fetch Twitch client version: %s", error)
            return self.client_version

        match = self.twilight_build_id_pattern.search(response.text)
        if match is None:
            logger.debug("Twitch page did not contain a client build ID")
            return self.client_version

        self.client_version = match.group(1)
        return self.client_version

    def __log_drop_watching(self, previous, current):
        if current is previous:
            return current
        if current is not None:
            campaigns = ", ".join(
                f"{campaign.name} ({campaign.game_name()})"
                for campaign in current.stream.campaigns
                if campaign.drops
            )
            logger.info(
                f"Watching {current.username} to earn drop: {campaigns}",
                extra={"emoji": ":gift:", "event": Events.DROP_STATUS},
            )
        elif previous is not None:
            logger.info(
                "No drop to farm at the moment, back to the normal watch routine",
                extra={"emoji": ":gift:", "event": Events.DROP_STATUS},
            )
        return current

    def __log_watch_targets(self, previous, current):
        """Report which channels are mined and why, whenever that changes."""
        message = describe_watch_change(previous, current)
        if message is not None:
            logger.info(
                message, extra={"emoji": ":pick:", "event": Events.WATCH_TARGET}
            )
        return current

    def send_minute_watched_events(self, streamers, priority, chunk_size=3):
        last_drop_streamer = None
        last_targets = {}
        while self.running:
            drop_streamer = None
            try:
                streamers_index = [
                    i
                    for i in range(0, len(streamers))
                    if streamers[i].is_online is True
                    and (
                        streamers[i].online_at == 0
                        or (time.time() - streamers[i].online_at) > 30
                    )
                ]

                for index in streamers_index:
                    if (streamers[index].stream.update_elapsed() / 60) > 10:
                        # Why this user It's currently online but the last updated was more than 10minutes ago?
                        # Please perform a manually update and check if the user it's online
                        self.check_streamer_online(streamers[index])

                streamers_watching = []
                reasons = {}
                for prior in priority:
                    if prior == Priority.ORDER and len(streamers_watching) < 2:
                        # Preserve the configured order without selecting a streamer twice.
                        picked = [
                            index
                            for index in streamers_index
                            if index not in streamers_watching
                        ][: 2 - len(streamers_watching)]
                        streamers_watching += picked
                        for index in picked:
                            reasons.setdefault(index, WATCH_REASONS[Priority.ORDER])

                    elif (
                        prior in [Priority.POINTS_ASCENDING, Priority.POINTS_DESCEDING]
                        and len(streamers_watching) < 2
                    ):
                        items = [
                            {"points": streamers[index].channel_points, "index": index}
                            for index in streamers_index
                            if index not in streamers_watching
                        ]
                        items = sorted(
                            items,
                            key=lambda x: x["points"],
                            reverse=(
                                True if prior == Priority.POINTS_DESCEDING else False
                            ),
                        )
                        picked = [item["index"] for item in items][:2]
                        streamers_watching += picked
                        for index in picked:
                            reasons.setdefault(index, WATCH_REASONS[prior])

                    elif prior == Priority.STREAK and len(streamers_watching) < 2:
                        """
                        Give eligible streaks priority until the reward is confirmed
                        or seven watched minutes have been recorded.
                        Viewers receive points for returning for x consecutive streams.
                        Each stream must be at least 10 minutes long and it must have been at least 30 minutes since the last stream ended.
                        """
                        for index in streamers_index:
                            if (
                                index not in streamers_watching
                                and streamers[index].settings.watch_streak is True
                                and streamers[index].stream.watch_streak_missing is True
                                and (
                                    streamers[index].offline_at == 0
                                    or (
                                        (time.time() - streamers[index].offline_at)
                                        // 60
                                    )
                                    > 30
                                )
                                and streamers[index].stream.minute_watched < 7
                            ):
                                streamers_watching.append(index)
                                reasons.setdefault(
                                    index, WATCH_REASONS[Priority.STREAK]
                                )
                                if len(streamers_watching) == 2:
                                    break

                    elif prior == Priority.DROPS and len(streamers_watching) < 2:
                        # Drop progress is only earned on one channel at a time, so a
                        # single slot is enough; the other one keeps the normal routine.
                        for index in streamers_index:
                            if (
                                index not in streamers_watching
                                and streamers[index].drops_condition() is True
                            ):
                                streamers_watching.append(index)
                                reasons.setdefault(index, WATCH_REASONS[Priority.DROPS])
                                drop_streamer = streamers[index]
                                break

                    elif prior == Priority.SUBSCRIBED and len(streamers_watching) < 2:
                        streamers_with_multiplier = [
                            index
                            for index in streamers_index
                            if index not in streamers_watching
                            and streamers[index].viewer_has_points_multiplier()
                        ]
                        streamers_with_multiplier = sorted(
                            streamers_with_multiplier,
                            key=lambda x: streamers[x].total_points_multiplier(),
                            reverse=True,
                        )
                        picked = streamers_with_multiplier[:2]
                        streamers_watching += picked
                        for index in picked:
                            reasons.setdefault(
                                index, WATCH_REASONS[Priority.SUBSCRIBED]
                            )

                """
                Twitch has a limit - you can't watch more than 2 channels at one time.
                We take the first two streamers from the list as they have the highest priority (based on order or WatchStreak).
                """
                streamers_watching = streamers_watching[:2]
                if drop_streamer is not None and not any(
                    streamers[index] is drop_streamer for index in streamers_watching
                ):
                    drop_streamer = None
                last_drop_streamer = self.__log_drop_watching(
                    last_drop_streamer, drop_streamer
                )
                last_targets = self.__log_watch_targets(
                    last_targets,
                    {
                        streamers[index].username: (
                            streamers[index],
                            reasons.get(index, WATCH_REASONS[Priority.ORDER]),
                        )
                        for index in streamers_watching
                    },
                )

                for index in streamers_watching:
                    next_iteration = time.time() + 60 / len(streamers_watching)

                    try:
                        response = requests.post(
                            streamers[index].stream.spade_url,
                            data=streamers[index].stream.encode_payload(),
                            headers={"User-Agent": self.user_agent},
                            timeout=60,
                        )
                        logger.debug(
                            f"Send minute watched request for {streamers[index]} - Status code: {response.status_code}"
                        )
                        if response.status_code == 204:
                            streamers[index].stream.update_minute_watched()

                            """
                            Remember, you can only earn progress towards a time-based Drop on one participating channel at a time.  [ ! ! ! ]
                            You can also check your progress towards Drops within a campaign anytime by viewing the Drops Inventory.
                            For time-based Drops, if you are unable to claim the Drop in time, you will be able to claim it from the inventory page until the Drops campaign ends.
                            """

                            for campaign in streamers[index].stream.campaigns:
                                for drop in campaign.drops:
                                    # We could add .has_preconditions_met condition inside is_printable
                                    if (
                                        drop.has_preconditions_met is not False
                                        and drop.is_printable is True
                                    ):
                                        drop_messages = [
                                            f"{streamers[index]} is streaming {streamers[index].stream}",
                                            f"Campaign: {campaign}",
                                            f"Drop: {drop}",
                                            f"{drop.progress_bar()}",
                                        ]
                                        for single_line in drop_messages:
                                            logger.info(
                                                single_line,
                                                extra={
                                                    "event": Events.DROP_STATUS,
                                                    "skip_telegram": True,
                                                    "skip_discord": True,
                                                },
                                            )

                                        if Settings.logger.telegram is not None:
                                            Settings.logger.telegram.send(
                                                "\n".join(drop_messages),
                                                Events.DROP_STATUS,
                                            )

                                        if Settings.logger.discord is not None:
                                            Settings.logger.discord.send(
                                                "\n".join(drop_messages),
                                                Events.DROP_STATUS,
                                            )

                    except requests.exceptions.ConnectionError as e:
                        logger.error(f"Error while trying to send minute watched: {e}")
                        self.__check_connection_handler(chunk_size)
                    except requests.exceptions.Timeout as e:
                        logger.error(f"Error while trying to send minute watched: {e}")

                    self.__chuncked_sleep(
                        next_iteration - time.time(), chunk_size=chunk_size
                    )

                if streamers_watching == []:
                    self.__chuncked_sleep(60, chunk_size=chunk_size)
            except Exception:
                logger.error("Exception raised in send minute watched", exc_info=True)

    # === CHANNEL POINTS / PREDICTION === #
    # Load the amount of current points for a channel, check if a bonus is available
    def load_channel_points_context(self, streamer):
        json_data = copy.deepcopy(GQLOperations.ChannelPointsContext)
        json_data["variables"] = {"channelLogin": streamer.username}

        response = self.post_gql_request(json_data)
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise UnexpectedTwitchResponseException(
                f"Twitch returned no GraphQL data while loading channel points for "
                f"{streamer.username}.{_graphql_error_details(response)}"
            )

        community = response["data"].get("community")
        if community is None:
            raise StreamerDoesNotExistException
        if not isinstance(community, dict):
            raise UnexpectedTwitchResponseException(
                f"Twitch returned an invalid community response while loading "
                f"channel points for {streamer.username}."
            )

        try:
            community_points = community["channel"]["self"]["communityPoints"]
            balance = community_points["balance"]
            active_multipliers = community_points["activeMultipliers"]
            available_claim = community_points["availableClaim"]
            claim_id = available_claim["id"] if available_claim is not None else None
        except (KeyError, TypeError) as error:
            raise UnexpectedTwitchResponseException(
                f"Twitch returned incomplete channel points data for "
                f"{streamer.username}."
            ) from error

        streamer.channel_points = balance
        streamer.activeMultipliers = active_multipliers

        if claim_id is not None:
            self.claim_bonus(streamer, claim_id)

    def make_predictions(self, event):
        decision = event.bet.calculate(event.streamer.channel_points)
        selector_index = 0 if decision["choice"] == "A" else 1

        logger.info(
            f"Going to complete bet for {event}",
            extra={
                "emoji": ":four_leaf_clover:",
                "event": Events.BET_GENERAL,
            },
        )
        if event.status == "ACTIVE":
            skip, compared_value = event.bet.skip()
            if skip is True:
                logger.info(
                    f"Skip betting for the event {event}",
                    extra={
                        "emoji": ":pushpin:",
                        "event": Events.BET_FILTERS,
                    },
                )
                logger.info(
                    f"Skip settings {event.bet.settings.filter_condition}, current value is: {compared_value}",
                    extra={
                        "emoji": ":pushpin:",
                        "event": Events.BET_FILTERS,
                    },
                )
            else:
                if decision["amount"] >= 10:
                    logger.info(
                        f"Place {_millify(decision['amount'])} channel points on: {event.bet.get_outcome(selector_index)}",
                        extra={
                            "emoji": ":four_leaf_clover:",
                            "event": Events.BET_GENERAL,
                        },
                    )

                    json_data = copy.deepcopy(GQLOperations.MakePrediction)
                    json_data["variables"] = {
                        "input": {
                            "eventID": event.event_id,
                            "outcomeID": decision["id"],
                            "points": decision["amount"],
                            "transactionID": token_hex(16),
                        }
                    }
                    response = self.post_gql_request(json_data)
                    if (
                        "data" in response
                        and "makePrediction" in response["data"]
                        and "error" in response["data"]["makePrediction"]
                        and response["data"]["makePrediction"]["error"] is not None
                    ):
                        error_code = response["data"]["makePrediction"]["error"]["code"]
                        logger.error(
                            f"Failed to place bet, error: {error_code}",
                            extra={
                                "emoji": ":four_leaf_clover:",
                                "event": Events.BET_FAILED,
                            },
                        )
                else:
                    logger.info(
                        f"Bet won't be placed as the amount {_millify(decision['amount'])} is less than the minimum required 10",
                        extra={
                            "emoji": ":four_leaf_clover:",
                            "event": Events.BET_GENERAL,
                        },
                    )
        else:
            logger.info(
                f"Oh no! The event is not active anymore! Current status: {event.status}",
                extra={
                    "emoji": ":disappointed_relieved:",
                    "event": Events.BET_FAILED,
                },
            )

    def claim_bonus(self, streamer, claim_id):
        if Settings.logger.less is False:
            logger.info(
                f"Claiming the bonus for {streamer}!",
                extra={"emoji": ":gift:", "event": Events.BONUS_CLAIM},
            )

        json_data = copy.deepcopy(GQLOperations.ClaimCommunityPoints)
        json_data["variables"] = {
            "input": {"channelID": streamer.channel_id, "claimID": claim_id}
        }
        self.post_gql_request(json_data)

    # === CAMPAIGNS / DROPS / INVENTORY === #
    def __get_campaign_ids_from_streamer(self, streamer):
        json_data = copy.deepcopy(GQLOperations.DropsHighlightService_AvailableDrops)
        json_data["variables"] = {"channelID": streamer.channel_id}
        response = self.post_gql_request(json_data)
        try:
            return (
                []
                if response["data"]["channel"]["viewerDropCampaigns"] is None
                else [
                    item["id"]
                    for item in response["data"]["channel"]["viewerDropCampaigns"]
                ]
            )
        except (ValueError, KeyError):
            return []

    def __drops_query(self, operation, path, expected_type):
        """Run a drops query; if the answer lacks the expected value, retry once with
        TwitchDropsMiner's request variant and keep using whichever variant works."""
        name = operation["operationName"]

        def extract(response):
            node = response
            for key in path:
                if not isinstance(node, dict):
                    return None
                node = node.get(key)
            return node if isinstance(node, expected_type) else None

        alternate = _alternate_drops_query(operation)
        attempts = []
        drops_login = getattr(self, "drops_login", None)
        for login, client in ((drops_login, "android"), (None, "tv")):
            if client == "android" and login is None:
                continue
            attempts.append(((client, "default"), operation, login))
            attempts.append(((client, "alternate"), alternate, login))
        preferred = _preferred_drops_variants.get(name)
        attempts.sort(key=lambda attempt: attempt[0] != preferred)

        problems = []
        for key, query, login in attempts:
            response = self.post_gql_request(query, login=login)
            value = extract(response)
            if value is not None:
                if key != preferred:
                    _preferred_drops_variants[name] = key
                    if problems:
                        logger.info(
                            "Drop scan: %s now uses the %s client with the %s request (before: %s)",
                            name,
                            key[0],
                            key[1],
                            "; ".join(problems),
                        )
                return value, None
            if login is not None and isinstance(response, dict) and response.get("status") == 401:
                self.__reset_drops_login()
            problems.append(f"{key[0]}/{key[1]}: {_describe_response(response, path)}")
        return None, "; ".join(problems)

    def __get_inventory(self):
        inventory, problem = self.__drops_query(
            GQLOperations.Inventory, ("data", "currentUser", "inventory"), dict
        )
        if inventory is None:
            logger.warning("Drop scan: Twitch did not return the drops inventory (%s)", problem)
            return {}
        return inventory

    def __get_drops_dashboard(self, status=None):
        campaigns, problem = self.__drops_query(
            GQLOperations.ViewerDropsDashboard,
            ("data", "currentUser", "dropCampaigns"),
            list,
        )
        if campaigns is None:
            logger.warning(
                "Drop scan: Twitch did not return the drop campaign list (%s)", problem
            )
            return []

        campaigns = [c for c in campaigns if isinstance(c, dict)]
        if status is not None:
            campaigns = [c for c in campaigns if c.get("status") == status.upper()]
        return campaigns

    def __get_campaigns_details(self, campaigns):
        result = []
        chunks = create_chunks(campaigns, 20)
        for chunk in chunks:
            json_data = []
            for campaign in chunk:
                json_data.append(copy.deepcopy(GQLOperations.DropCampaignDetails))
                json_data[-1]["variables"] = {
                    "dropID": campaign["id"],
                    "channelLogin": f"{self.twitch_login.get_user_id()}",
                }

            drops_login = getattr(self, "drops_login", None)
            response = self.post_gql_request(json_data, login=drops_login)
            if drops_login is not None and not isinstance(response, list):
                response = self.post_gql_request(json_data)
            if not isinstance(response, list):
                logger.warning(
                    "Drop scan: could not load details for %s campaign(s).%s",
                    len(chunk),
                    _graphql_error_details(response) or " (unexpected response)",
                )
                continue
            for r in response:
                try:
                    drop_campaign = r["data"]["user"]["dropCampaign"]
                except (KeyError, TypeError):
                    logger.debug(
                        "Drop scan: campaign details missing.%s",
                        _graphql_error_details(r),
                    )
                    continue
                if drop_campaign is not None:
                    result.append(drop_campaign)
        return result

    def __sync_campaigns(self, campaigns):
        # We need the inventory only for get the real updated value/progress
        # Get data from inventory and sync current status with streamers.campaigns
        inventory = self.__get_inventory()
        if not isinstance(inventory, dict):
            inventory = {}
        in_progress = [
            c for c in (inventory.get("dropCampaignsInProgress") or []) if isinstance(c, dict)
        ]
        # Rewards of already completed campaigns are only listed here (benefit id -> award time).
        claimed_benefits = {
            event["id"]: parse_twitch_datetime(event.get("lastAwardedAt"))
            for event in (inventory.get("gameEventDrops") or [])
            if isinstance(event, dict) and event.get("id")
        }

        # Iterate all campaigns from dashboard (only active, with working drops)
        # In this array we have also the campaigns never started from us (not in inventory)
        for campaign in campaigns:
            progress = next((p for p in in_progress if p.get("id") == campaign.id), None)
            if progress is not None:
                campaign.in_inventory = True
                campaign.sync_drops(progress.get("timeBasedDrops") or [], self.claim_drop)
            else:
                campaign.mark_claimed_from_benefits(claimed_benefits)
            campaign.clear_drops()  # Remove all the claimed drops
        return campaigns

    def claim_drop(self, drop):
        logger.info(
            f"Claim {drop}", extra={"emoji": ":package:", "event": Events.DROP_CLAIM}
        )

        json_data = copy.deepcopy(GQLOperations.DropsPage_ClaimDropRewards)
        json_data["variables"] = {"input": {"dropInstanceID": drop.drop_instance_id}}
        response = self.post_gql_request(json_data)
        try:
            # response["data"]["claimDropRewards"] can be null and respose["data"]["errors"] != []
            # or response["data"]["claimDropRewards"]["status"] === DROP_INSTANCE_ALREADY_CLAIMED
            if ("claimDropRewards" in response["data"]) and (
                response["data"]["claimDropRewards"] is None
            ):
                return False
            elif ("errors" in response["data"]) and (response["data"]["errors"] != []):
                return False
            elif ("claimDropRewards" in response["data"]) and (
                response["data"]["claimDropRewards"]["status"]
                in ["ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"]
            ):
                return True
            else:
                return False
        except (ValueError, KeyError):
            return False

    def claim_all_drops_from_inventory(self):
        inventory = self.__get_inventory()
        if inventory not in [None, {}]:
            if inventory["dropCampaignsInProgress"] not in [None, {}]:
                for campaign in inventory["dropCampaignsInProgress"]:
                    for drop_dict in campaign["timeBasedDrops"]:
                        drop = Drop(drop_dict)
                        drop.update(drop_dict["self"])
                        if drop.is_claimable is True:
                            drop.is_claimed = self.claim_drop(drop)
                            time.sleep(random.uniform(5, 10))

    def get_drops_enabled_channels(self, game, limit=DIRECTORY_SEARCH_LIMIT):
        """Return [(channel_id, login)] of live, drops-enabled streams for a campaign game."""
        if not isinstance(game, dict):
            return []
        slug = game.get("slug") or _game_slug(
            game.get("name") or game.get("displayName")
        )
        if not slug:
            return []

        json_data = copy.deepcopy(GQLOperations.DirectoryPage_Game)
        json_data["variables"]["slug"] = slug
        json_data["variables"]["limit"] = limit
        response = self.post_gql_request(json_data)
        try:
            edges = response["data"]["game"]["streams"]["edges"] or []
        except (KeyError, TypeError):
            logger.debug(
                f"No directory streams for game '{slug}'.{_graphql_error_details(response)}"
            )
            return []

        channels = []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            broadcaster = node.get("broadcaster") if isinstance(node, dict) else None
            if (
                isinstance(broadcaster, dict)
                and broadcaster.get("id")
                and broadcaster.get("login")
            ):
                channels.append(
                    (str(broadcaster["id"]), broadcaster["login"].lower())
                )
        return channels

    def __add_drop_streamer(self, streamers, discovered_streamers, channel_id, login):
        streamer = Streamer(login)
        streamer.channel_id = channel_id
        streamer.notify = False
        streamer.settings = StreamerSettings(
            make_predictions=False,
            follow_raid=False,
            claim_drops=True,
            watch_streak=False,
            chat=ChatPresence.NEVER,
        )
        streamer.settings.default()
        streamer.settings.bet.default()
        discovered_streamers[channel_id] = streamer
        streamers.append(streamer)
        logger.info(
            f"Discovered {login} for an unclaimed Twitch drop",
            extra={"emoji": ":gift:"},
        )
        return streamer

    def __discover_directory_channels(
        self, campaign, streamers, discovered_streamers, directory_channels
    ):
        found = self.get_drops_enabled_channels(campaign.game)
        if not found:
            logger.debug(
                f"No live drops-enabled channel found for campaign {campaign.name}"
            )
            return

        known = {streamer.channel_id: streamer for streamer in streamers}
        selected = []
        for channel_id, login in found:
            # Prefer channels the user already follows/mines, then fill up with new ones.
            if channel_id in known and channel_id not in discovered_streamers:
                selected.insert(0, (channel_id, login))
            else:
                selected.append((channel_id, login))
        selected = selected[:DIRECTORY_CHANNELS_PER_CAMPAIGN]

        directory_channels[campaign.id] = {channel_id for channel_id, _ in selected}
        for channel_id, login in selected:
            if channel_id not in known:
                self.__add_drop_streamer(
                    streamers, discovered_streamers, channel_id, login
                )

        logger.info(
            f"Looking for drop '{campaign.name}' ({campaign.game_name()}) on: "
            + ", ".join(login for _, login in selected),
            extra={"emoji": ":gift:"},
        )

    def sync_campaigns(self, streamers, chunk_size=3):
        campaigns_update = 0
        campaigns = []
        discovered_streamers = {}
        # campaign id -> channel ids found in the game directory for that campaign
        directory_channels = {}
        directory_checked = {}
        while self.running:
            scan_stats = None
            try:
                # Get update from dashboard each 60minutes
                if (
                    campaigns_update == 0
                    or ((time.time() - campaigns_update) / 60) > 60
                ):
                    campaigns_update = time.time()
                    self.ensure_drops_login()
                    if not self.running:
                        break
                    # Get full details from current ACTIVE campaigns
                    # Use dashboard so we can explore new drops not currently active in our Inventory
                    dashboard = self.__get_drops_dashboard(status="ACTIVE")
                    campaigns_details = self.__get_campaigns_details(dashboard)
                    campaigns = []
                    scan_stats = {
                        "listed": len(dashboard),
                        "checked": len(campaigns_details),
                        "no_watch_drops": 0,
                        "not_linked": 0,
                    }

                    # Going to clear array and structure. Remove all the timeBasedDrops expired or not started yet
                    for index in range(0, len(campaigns_details)):
                        campaign = Campaign(campaigns_details[index])
                        if campaign.dt_match is True:
                            # Remove all the drops already claimed or with dt not matching
                            campaign.clear_drops()
                            if campaign.drops == []:
                                scan_stats["no_watch_drops"] += 1
                                continue
                            if not campaign.is_eligible:
                                scan_stats["not_linked"] += 1
                                logger.debug(
                                    f"Skipping drop campaign {campaign.name}: "
                                    "game account is not linked"
                                )
                                continue
                            campaigns.append(campaign)
                        else:
                            scan_stats["no_watch_drops"] += 1

                # Divide et impera :)
                campaigns = self.__sync_campaigns(campaigns)
                active_campaigns = [c for c in campaigns if c.drops]
                active_ids = {c.id for c in active_campaigns}

                if scan_stats is not None:
                    scan_stats["already_earned"] = len(campaigns) - len(
                        active_campaigns
                    )
                    logger.info(
                        describe_drop_scan(active_campaigns, scan_stats),
                        extra={"emoji": ":mag:", "event": Events.DROP_SCAN},
                    )
                    scan_stats = None
                    report_live = True
                else:
                    report_live = False

                # Forget directory channels of campaigns that are done.
                for campaign_id in list(directory_channels):
                    if campaign_id not in active_ids:
                        del directory_channels[campaign_id]

                campaign_channels = {
                    channel_id
                    for campaign in active_campaigns
                    for channel_id in campaign.channels
                }
                needed_channels = campaign_channels | {
                    channel_id
                    for channels in directory_channels.values()
                    for channel_id in channels
                }
                known_channel_ids = {
                    streamer.channel_id for streamer in streamers
                }
                channel_logins = self.get_channel_logins(
                    [
                        channel_id
                        for channel_id in campaign_channels
                        if channel_id not in known_channel_ids
                        and channel_id not in discovered_streamers
                    ]
                )

                for channel_id, login in channel_logins.items():
                    self.__add_drop_streamer(
                        streamers, discovered_streamers, channel_id, login
                    )

                for channel_id in list(discovered_streamers):
                    if channel_id not in needed_channels:
                        streamer = discovered_streamers[channel_id]
                        streamer.stream.campaigns = []
                        if streamer.is_online:
                            streamer.set_offline()

                for channel_id, streamer in discovered_streamers.items():
                    if channel_id in needed_channels:
                        self.check_streamer_online(streamer)

                # Match active campaigns to live channels, including newly discovered
                # channels listed in a campaign's allow-list or found in the game directory.
                for streamer in streamers:
                    if (
                        streamer.settings.claim_drops is True
                        and streamer.is_online is True
                    ):
                        game_id = streamer.stream.game_id()
                        eligible_campaigns = [
                            campaign
                            for campaign in active_campaigns
                            if game_id is not None
                            and isinstance(campaign.game, dict)
                            and campaign.game.get("id") == game_id
                            and (
                                campaign.id in streamer.stream.campaigns_ids
                                or streamer.channel_id in campaign.channels
                                or streamer.channel_id
                                in directory_channels.get(campaign.id, ())
                            )
                        ]
                        streamer.stream.campaigns = eligible_campaigns
                        streamer.stream.campaigns_ids = [
                            campaign.id for campaign in eligible_campaigns
                        ]

                # Campaigns open to every channel of a game ("watch any participating
                # live channel"): if none of the known channels can progress them, look
                # for live drops-enabled channels in the game directory.
                covered_ids = {
                    campaign.id
                    for streamer in streamers
                    if streamer.is_online is True
                    for campaign in streamer.stream.campaigns
                }
                if report_live:
                    for campaign in active_campaigns:
                        live = [
                            streamer.username
                            for streamer in streamers
                            if streamer.is_online is True
                            and campaign in streamer.stream.campaigns
                        ]
                        if live:
                            status = "live now: " + ", ".join(live)
                        elif campaign.any_channel:
                            status = "none of your channels is live, searching the category"
                        else:
                            status = "no participating channel is live, checking again every minute"
                        logger.info(
                            f"Drop '{campaign.name}' ({campaign.game_name()}): {status}",
                            extra={"emoji": ":mag:", "event": Events.DROP_SCAN},
                        )
                for campaign in active_campaigns:
                    if (
                        not campaign.any_channel
                        or campaign.id in covered_ids
                        or time.time() - directory_checked.get(campaign.id, 0)
                        < DIRECTORY_SEARCH_INTERVAL
                    ):
                        continue
                    directory_checked[campaign.id] = time.time()
                    self.__discover_directory_channels(
                        campaign, streamers, discovered_streamers, directory_channels
                    )

            except (
                ValueError,
                KeyError,
                TypeError,
                requests.exceptions.ConnectionError,
            ) as e:
                logger.error(f"Error while syncing inventory: {e}")
                self.__check_connection_handler(chunk_size)
            except Exception:
                # Keep the drops thread alive; it retries on the next iteration.
                logger.error("Unexpected error while syncing inventory", exc_info=True)

            self.__chuncked_sleep(60, chunk_size=chunk_size)
