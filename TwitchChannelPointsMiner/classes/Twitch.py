# For documentation on Twitch GraphQL API see:
# https://www.apollographql.com/docs/
# https://github.com/mauricew/twitch-graphql-api
# Full list of available methods: https://azr.ivr.fi/schema/query.doc.html (a bit outdated)


import copy
import json
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
from TwitchChannelPointsMiner.classes.StreamDatabase import fetch_watch_badges
from TwitchChannelPointsMiner.classes.ProgressionDrops import (
    PROGRESSION_PREFIX,
    ProgressionDrops,
)
from TwitchChannelPointsMiner.constants import (
    CLIENT_ID,
    CLIENT_VERSION,
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
# Campaigns with a channel allow-list: how many live category streams to scan for
# allow-listed channels (instead of polling every allow-listed channel).
ALLOW_LIST_SEARCH_LIMIT = 30
# Stream Database badges Twitch never confirms (no progress in the inventory) are
# given up after this long, so the miner doesn't watch a category forever.
STREAMDB_FALLBACK_SECONDS = 12 * 60 * 60
STREAMDB_CHANNELS_PER_BADGE = 10
STREAMDB_PREFIX = "streamdb:"
# Video loading for drop progress (TwitchDropsMiner loads every ~20 seconds).
STREAM_WATCH_INTERVAL = 20
STREAM_PLAYLIST_TTL = 5 * 60
STREAM_CHUNKS_PER_LOAD = 20
# Drops whose claim was already announced on Telegram/Discord (kept across restarts).
ANNOUNCED_DROPS_LIMIT = 1000
# Seven watched minutes plus a one-minute buffer for Twitch's progress tracking.
WATCH_STREAK_MINUTES = 8


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
# Drops operation name -> request variant ("default"/"alternate") that last returned data.
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


def _campaign_from_available_drops(data, channel_entry, linked=True):
    """
    Campaign data (same shape as DropCampaignDetails) built from one entry of a channel's
    AvailableDrops answer, or None if it has no game or no usable time-based drops.
    The channel it was found on becomes the allow-list, so the miner watches it.
    """
    if not isinstance(data, dict) or not data.get("id") or not data.get("endAt"):
        return None
    game = data.get("game")
    if not isinstance(game, dict) or not game.get("id"):
        return None
    timed_drops = []
    for drop in data.get("timeBasedDrops") or []:
        if not isinstance(drop, dict) or not all(
            drop.get(key) for key in ("id", "name", "startAt", "endAt")
        ):
            continue
        timed_drops.append(
            {
                "id": drop["id"],
                "name": drop["name"],
                "startAt": drop["startAt"],
                "endAt": drop["endAt"],
                "requiredMinutesWatched": drop.get("requiredMinutesWatched"),
                "requiredSubs": drop.get("requiredSubs"),
                "benefitEdges": [
                    edge
                    for edge in drop.get("benefitEdges") or []
                    if isinstance(edge, dict) and isinstance(edge.get("benefit"), dict)
                ],
            }
        )
    if not timed_drops:
        return None
    game_name = game.get("displayName") or game.get("name")
    return {
        "id": data["id"],
        "name": data.get("name") or data["id"],
        "game": {
            "id": str(game["id"]),
            "name": game.get("name") or game_name,
            "displayName": game_name,
            "slug": game.get("slug"),
        },
        "status": "ACTIVE",
        "self": {"isAccountConnected": linked},
        "startAt": min(drop["startAt"] for drop in timed_drops),
        "endAt": data["endAt"],
        "allow": {"channels": [dict(channel_entry)], "isEnabled": True},
        "timeBasedDrops": timed_drops,
    }


def _drop_key(drop):
    # A badge can be offered by several campaigns (Twitch and Stream Database, one per
    # category), so badges are identified by their title instead of the drop id.
    if drop.badge_titles:
        return "badge:" + "|".join(sorted(drop.badge_titles))
    return drop.id


def _campaigns_from_badge(badge):
    """
    Campaign data for a running Stream Database watch badge, one per category, used when
    Twitch's own AvailableDrops did not confirm a campaign in that category. Watching starts
    the real campaign in the inventory, which then takes over.
    """
    if not badge.games or badge.start_at is None or badge.end_at is None:
        return []
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    start, end = badge.start_at.strftime(fmt), badge.end_at.strftime(fmt)
    allow = [{"id": channel_id, "name": login} for channel_id, login in badge.channels]
    result = []
    for game in badge.games:
        campaign_id = f"{STREAMDB_PREFIX}{badge.set_id}:{game['id']}"
        result.append(
            {
                "id": campaign_id,
                "name": badge.title,
                "game": {
                    "id": game["id"],
                    "name": game["name"],
                    "displayName": game["name"],
                    "slug": game.get("slug"),
                },
                "status": "ACTIVE",
                "self": {"isAccountConnected": True},
                "startAt": start,
                "endAt": end,
                "allow": {"channels": allow, "isEnabled": True} if allow else None,
                "timeBasedDrops": [
                    {
                        "id": campaign_id,
                        "name": badge.title,
                        "startAt": start,
                        "endAt": end,
                        "requiredMinutesWatched": badge.minutes,
                        "requiredSubs": 0,
                        "benefitEdges": [
                            {
                                "benefit": {
                                    "id": campaign_id,
                                    "name": badge.title,
                                    "distributionType": "BADGE",
                                }
                            }
                        ],
                    }
                ],
            }
        )
    return result


def _playlist_urls(text):
    return [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip().startswith(("http://", "https://"))
    ]


def _last_playlist_url(text):
    """Last URL line of an m3u8 playlist (lowest quality variant / newest chunk)."""
    urls = _playlist_urls(text)
    return urls[-1] if urls else None


def _scan_sources(stats):
    """Where the hourly scan looked, e.g. '1 started in your inventory; 12 live channel(s) checked in Rust'."""
    if "probed_channels" not in stats:
        return None
    text = f"{stats['probed_channels']} live channel(s) checked"
    categories = stats.get("categories") or []
    if categories:
        text += " in " + ", ".join(categories[:5])
        if len(categories) > 5:
            text += f" +{len(categories) - 5} more"
    sources = f"{stats.get('in_progress', 0)} started in your inventory; {text}"
    if "streamdb" in stats:
        if stats.get("streamdb_error"):
            streamdb = "Stream Database unavailable"
        else:
            streamdb = f"Stream Database: {stats['streamdb']} running watch badge(s)"
            if stats.get("streamdb_earned"):
                streamdb += f", {stats['streamdb_earned']} already earned"
        sources = f"{streamdb}; {sources}"
    return sources


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
        if streamer.stream.minute_watched >= WATCH_STREAK_MINUTES:
            return "watch streak watch-time limit reached (reward not confirmed)"
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
    sources = _scan_sources(stats)
    if listed == 0 and sources is not None:
        hint = (
            ""
            if stats["probed_channels"]
            else ". Drops are found through live channels, so none could be checked right now"
        )
        return f"Scanned for drops: no watch-time drops found ({sources}){hint}"
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
    if sources is not None:
        details += f"; sources: {sources}"

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
        source = (
            " (from Stream Database)"
            if str(campaign.id).startswith(STREAMDB_PREFIX)
            else ""
        )
        lines.append(
            f"- {campaign.name}{source} | Category: {campaign.game_name() or 'unknown'} "
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
        "announced_drops",
        "pending_drops",
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
        self.running = True

    def login(self):
        if os.path.isfile(self.cookies_file) is False:
            if self.twitch_login.login_flow():
                self.twitch_login.save_cookies(self.cookies_file)
        else:
            self.twitch_login.load_cookies(self.cookies_file)
            self.twitch_login.set_token(self.twitch_login.get_auth_token())

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
                    "channel": streamer.username,
                    "hidden": False,
                    "live": True,
                    "location": "channel",
                    "logged_in": True,
                    "muted": False,
                }

                if (
                    streamer.stream.game_name() is not None
                    and streamer.settings.claim_drops is True
                ):
                    event_properties["game"] = streamer.stream.game_name()
                    if streamer.stream.game_id() is not None:
                        event_properties["game_id"] = streamer.stream.game_id()
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

    def recheck_offline_streamers(self, streamers):
        """
        Fallback for missed PubSub "stream-up"/"viewcount" messages: without it a
        streamer that went live unnoticed is never watched (e.g. its watch streak).
        """
        for streamer in list(streamers):
            if not self.running:
                return
            if (
                streamer.is_online is True
                or streamer.drops_only
                or streamer.channel_id is None
                or time.time() < streamer.offline_at + 60
            ):
                continue
            try:
                live = self.get_broadcast_id(streamer) is not None
                if live:
                    logger.info(
                        f"{streamer.username} is live but Twitch sent no live "
                        "notification; checking the stream now"
                    )
                    self.check_streamer_online(streamer)
            except StreamerIsOfflineException:
                pass
            except Exception as e:
                logger.debug(f"Live check for {streamer.username} failed: {e}")
            time.sleep(0.5)

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

    def post_gql_request(self, json_data):
        json_data = _apply_hashes(json_data, _preferred_hashes)
        response = self.__send_gql_request(json_data)
        fallback_data = _with_fallback_hashes(json_data, response)
        if fallback_data is not None:
            logger.debug("Retrying GraphQL request with fallback persisted query hash")
            fallback_response = self.__send_gql_request(fallback_data)
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

    def __send_gql_request(self, json_data):
        headers = {
            "Authorization": f"OAuth {self.twitch_login.get_auth_token()}",
            "Client-Id": CLIENT_ID,
            "Client-Session-Id": self.client_session,
            "Client-Version": self.update_client_version(),
            "User-Agent": self.user_agent,
            "X-Device-Id": self.device_id,
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

    def load_stream_segment(self, streamer, variant_cache=None):
        """
        Load the stream like a player does (playback token -> playlist -> HEAD of every
        video chunk currently listed, as TwitchDropsMiner does). Twitch only counts drop
        progress for viewers that fetch the video; the minute-watched event alone is not
        enough. variant_cache (login -> (playlist url, time)) saves the token request on
        repeated calls. Returns True when all chunks were reachable.
        """
        headers = {"User-Agent": self.user_agent}
        cached = (variant_cache or {}).get(streamer.username)
        if cached is not None and time.time() - cached[1] < STREAM_PLAYLIST_TTL:
            variant = cached[0]
        else:
            variant = self.__get_stream_variant(streamer, headers)
            if variant is None:
                return False
            if variant_cache is not None:
                variant_cache[streamer.username] = (variant, time.time())

        chunks = requests.get(
            variant, headers={**headers, "Connection": "close"}, timeout=20
        )
        urls = _playlist_urls(chunks.text) if chunks.status_code == 200 else []
        if not urls:
            logger.debug(
                f"Stream chunk list for {streamer.username} failed (HTTP {chunks.status_code})"
            )
            if variant_cache is not None:
                variant_cache.pop(streamer.username, None)
            return False
        for url in urls[-STREAM_CHUNKS_PER_LOAD:]:
            head = requests.head(url, headers=headers, timeout=20)
            if head.status_code != 200:
                logger.debug(
                    f"Stream chunk for {streamer.username} failed (HTTP {head.status_code})"
                )
                return False
        logger.debug(f"Loaded {len(urls[-STREAM_CHUNKS_PER_LOAD:])} stream chunks for {streamer.username}")
        return True

    def __get_stream_variant(self, streamer, headers):
        """URL of the lowest-quality playlist of a live stream, or None."""
        json_data = copy.deepcopy(GQLOperations.PlaybackAccessToken)
        json_data["variables"] = {
            "login": streamer.username,
            "isLive": True,
            "isVod": False,
            "vodID": "",
            "playerType": "site",
            "platform": "web",
        }
        response = self.post_gql_request(json_data)
        try:
            token = response["data"]["streamPlaybackAccessToken"]
            signature, value = token["signature"], token["value"]
        except (KeyError, TypeError):
            logger.debug(
                f"No playback token for {streamer.username}.%s",
                _graphql_error_details(response),
            )
            return None
        if not signature or not value:
            return None

        playlist = requests.get(
            f"https://usher.ttvnw.net/api/channel/hls/{streamer.username}.m3u8",
            params={"sig": signature, "token": value},
            headers=headers,
            timeout=20,
        )
        variant = _last_playlist_url(playlist.text) if playlist.status_code == 200 else None
        if variant is None:
            logger.debug(
                f"Stream playlist for {streamer.username} failed (HTTP {playlist.status_code})"
            )
        return variant

    def __watch_video(self, streamer, variant_cache):
        try:
            return self.load_stream_segment(streamer, variant_cache)
        except requests.exceptions.RequestException as e:
            logger.debug(f"Could not load the stream of {streamer.username}: {e}")
            variant_cache.pop(streamer.username, None)
            return False

    def __report_video(self, streamer, variant_cache, video_ok):
        """Load the video and log (once per change) whether Twitch serves it."""
        ok = self.__watch_video(streamer, variant_cache)
        if video_ok.get(streamer.username) == ok:
            return ok
        video_ok[streamer.username] = ok
        if ok:
            logger.info(f"📺  Loading the video of {streamer.username} (counts for drops)")
        else:
            logger.warning(
                f"Could not load the video of {streamer.username}; drop progress may not "
                "count there (see DEBUG log)"
            )
        return ok

    def send_minute_watched_events(self, streamers, priority, chunk_size=3):
        last_drop_streamer = None
        last_targets = {}
        variant_cache = {}
        video_ok = {}
        while self.running:
            drop_streamer = None
            try:
                streamers_index = [
                    i
                    for i in range(0, len(streamers))
                    if streamers[i].is_online is True
                    and (
                        not streamers[i].drops_only
                        or streamers[i].drops_condition()
                    )
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
                        or eight watched minutes have been recorded (including a
                        one-minute safety margin).
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
                                and streamers[index].stream.minute_watched
                                < WATCH_STREAK_MINUTES
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
                for index, streamer in enumerate(streamers):
                    if index not in streamers_watching:
                        streamer.stream.pause_minute_watched()
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
                                            f"Drop progress on {streamers[index].username}: "
                                            f"{drop.name} ({drop.benefit or drop.name}) - "
                                            f"{campaign.name}, {campaign.game_name() or 'unknown category'}",
                                            f"{drop.progress_bar()}",
                                        ]
                                        drop.is_printable = False
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
                        else:
                            streamers[index].stream.pause_minute_watched()
                            logger.warning(
                                "Watch request for %s failed (HTTP %s); "
                                "pausing the watch-time counter",
                                streamers[index].username,
                                response.status_code,
                            )

                    except requests.exceptions.ConnectionError as e:
                        streamers[index].stream.pause_minute_watched()
                        logger.error(f"Error while trying to send minute watched: {e}")
                        self.__check_connection_handler(chunk_size)
                    except requests.exceptions.Timeout as e:
                        streamers[index].stream.pause_minute_watched()
                        logger.error(f"Error while trying to send minute watched: {e}")

                    # Keep "playing" the video of every watched channel between the
                    # minute-watched events; Twitch needs this for drop progress.
                    while self.running:
                        for watched in streamers_watching:
                            self.__report_video(
                                streamers[watched], variant_cache, video_ok
                            )
                        remaining = next_iteration - time.time()
                        if remaining <= 0:
                            break
                        self.__chuncked_sleep(
                            min(STREAM_WATCH_INTERVAL, remaining), chunk_size=chunk_size
                        )
                        if next_iteration - time.time() <= 0:
                            break

                if streamers_watching == []:
                    self.__chuncked_sleep(60, chunk_size=chunk_size)
            except Exception:
                for streamer in streamers:
                    streamer.stream.pause_minute_watched()
                logger.error("Exception raised in send minute watched", exc_info=True)
        for streamer in streamers:
            streamer.stream.pause_minute_watched()

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

        attempts = [("default", operation), ("alternate", _alternate_drops_query(operation))]
        preferred = _preferred_drops_variants.get(name)
        attempts.sort(key=lambda attempt: attempt[0] != preferred)

        problems = []
        for key, query in attempts:
            response = self.post_gql_request(query)
            value = extract(response)
            if value is not None:
                if key != preferred:
                    _preferred_drops_variants[name] = key
                    if problems:
                        logger.info(
                            "Drop scan: %s now uses the %s request (before: %s)",
                            name,
                            key,
                            "; ".join(problems),
                        )
                return value, None
            problems.append(f"{key}: {_describe_response(response, path)}")
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
            # Expected since Twitch gated the dashboard (~Sep 2026); drops are then
            # discovered through live channels instead.
            logger.debug(
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
        # Drops still unearned after the previous sync: key -> drop.
        pending = getattr(self, "pending_drops", None) or {}
        owned_badges = (
            self.get_owned_badges()
            if any(drop.badge_titles for campaign in campaigns for drop in campaign.drops)
            or any(drop.badge_titles for drop in pending.values())
            else None
        )
        owned_sets, owned_titles = (
            owned_badges if owned_badges is not None else (set(), set())
        )

        # Iterate all campaigns from dashboard (only active, with working drops)
        # In this array we have also the campaigns never started from us (not in inventory)
        for campaign in campaigns:
            progress = next((p for p in in_progress if p.get("id") == campaign.id), None)
            if progress is not None:
                campaign.in_inventory = True
                campaign.sync_drops(progress.get("timeBasedDrops") or [], self.claim_drop)
            campaign.mark_claimed_from_benefits(claimed_benefits)
            for drop in campaign.drops:
                drop.mark_claimed_from_badges(owned_titles)
            if campaign.id.startswith(STREAMDB_PREFIX):
                set_id = campaign.id[len(STREAMDB_PREFIX) :].rsplit(":", 1)[0]
                if set_id.strip().lower() in owned_sets:
                    for drop in campaign.drops:
                        drop.is_claimed = True
            for drop in campaign.drops:
                if drop.is_claimed is True and _drop_key(drop) in pending:
                    self.__announce_earned(drop)
            campaign.clear_drops()  # Remove all the claimed drops

        # Badges that disappeared from the campaign list because they are owned now
        # (e.g. the hourly rebuild already filtered them out).
        current = {
            _drop_key(drop) for campaign in campaigns for drop in campaign.drops
        }
        for key, drop in pending.items():
            if (
                key not in current
                and drop.badge_titles
                and drop.badge_titles <= owned_titles
            ):
                self.__announce_earned(drop)

        self.pending_drops = {
            _drop_key(drop): drop for campaign in campaigns for drop in campaign.drops
        }
        return campaigns

    def __announce_earned(self, drop):
        """Announce a drop Twitch awarded without a claim by the miner (auto-granted badges)."""
        announced = self.__load_announced_drops()
        key = _drop_key(drop)
        if announced.get(key) is True:
            return
        self.__log_claim(drop, True, notify=True)
        announced[key] = True
        self.__save_announced_drops()

    def __announced_drops_file(self):
        folder = getattr(Settings, "analytics_path", None)
        return os.path.join(folder, "announced_drops.json") if folder else None

    def __load_announced_drops(self):
        announced = getattr(self, "announced_drops", None)
        if announced is None:
            announced = {}
            path = self.__announced_drops_file()
            if path and os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as file:
                        stored = json.load(file)
                    if isinstance(stored, dict):
                        announced = {str(k): v is True for k, v in stored.items()}
                except (OSError, ValueError):
                    logger.debug("Could not read announced drops", exc_info=True)
            self.announced_drops = announced
        return announced

    def __save_announced_drops(self):
        path = self.__announced_drops_file()
        if not path:
            return
        announced = self.announced_drops
        # Insertion order is kept, so the oldest entries are dropped first.
        while len(announced) > ANNOUNCED_DROPS_LIMIT:
            announced.pop(next(iter(announced)))
        try:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(announced, file)
        except OSError:
            logger.debug("Could not save announced drops", exc_info=True)

    def claim_drop(self, drop):
        claimed = self.__request_claim_drop(drop)
        announced = self.__load_announced_drops()
        key = _drop_key(drop)
        # Twitch keeps offering unclaimable drops (e.g. no linked game account), so
        # notify once per drop, and once more if a retry finally succeeds.
        notify = key not in announced or (claimed and not announced[key])
        self.__log_claim(drop, claimed, notify)
        if notify:
            announced[key] = claimed is True
            self.__save_announced_drops()
        return claimed

    def __log_claim(self, drop, claimed, notify):
        extra = {"event": Events.DROP_CLAIM}
        if not notify:
            extra.update(skip_telegram=True, skip_discord=True)

        if claimed and drop.is_badge_or_emote:
            kind = "Badge" if "BADGE" in drop.benefit_types else "Emote"
            message = f"{kind} drop claimed: {drop.benefit or drop.name}\n{drop}"
            extra.update(
                emoji=":medal:",
                telegram_highlight=True,
                telegram_pin_key=_drop_key(drop),
            )
        elif claimed:
            message = f"Claimed {drop}"
            extra["emoji"] = ":package:"
        else:
            message = f"Could not claim {drop} yet, retrying silently"
            extra["emoji"] = ":package:"

        logger.log(logging.INFO if notify else logging.DEBUG, message, extra=extra)

    def __request_claim_drop(self, drop):
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
        streamer.drops_only = True
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
        if campaign.any_channel:
            found = self.get_drops_enabled_channels(campaign.game)
        else:
            # Look deeper into the category, then keep only allow-listed channels.
            allowed = {str(channel_id) for channel_id in campaign.channels}
            found = [
                (channel_id, login)
                for channel_id, login in self.get_drops_enabled_channels(
                    campaign.game, limit=ALLOW_LIST_SEARCH_LIMIT
                )
                if channel_id in allowed
            ]
        if not found:
            logger.debug(
                f"No live drops-enabled channel found for campaign {campaign.name}"
            )
            return
        checked_count = len(found)
        found = self.__channels_offering_campaign(campaign, found)
        if not found:
            logger.info(
                f"Drop '{campaign.name}' ({campaign.game_name()}): none of the "
                f"{checked_count} checked drops-enabled channels lists this campaign, "
                "trying again in 10 minutes",
                extra={"emoji": ":mag:", "event": Events.DROP_SCAN},
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

    def __channels_offering_campaign(self, campaign, channels):
        """
        Prefer channels explicitly listing the campaign. AvailableDrops can omit an
        inventory campaign even while watching progresses it, so unrestricted inventory
        campaigns may fall back to the game's drops-enabled directory. Stream Database
        campaigns have no Twitch id; unanswered requests also keep the directory list.
        """
        if str(campaign.id).startswith(STREAMDB_PREFIX):
            return channels
        offered = self.__get_available_drop_campaigns([cid for cid, _ in channels])
        if not offered:
            return channels
        matching = [
            (channel_id, login)
            for channel_id, login in channels
            if any(c.get("id") == campaign.id for c in offered.get(channel_id, ()))
        ]
        if not matching:
            details = []
            for channel_id, login in channels:
                if channel_id not in offered:
                    status = "response unavailable"
                elif not offered[channel_id]:
                    status = "no campaigns returned"
                else:
                    status = ", ".join(
                        f"{entry.get('name') or '(unnamed)'} [{entry.get('id') or '(no id)'}]"
                        for entry in offered[channel_id]
                    )
                details.append(f"{login} ({channel_id}): {status}")
            logger.info(
                "Drop campaign check for '%s' (expected %s): %s",
                campaign.name,
                campaign.id,
                "; ".join(details),
                extra={"emoji": ":mag:", "event": Events.DROP_SCAN},
            )
            if campaign.in_inventory and campaign.any_channel:
                logger.info(
                    "Drop '%s' (%s): already in your inventory with no channel "
                    "restrictions listed; using drops-enabled category channels "
                    "although Twitch's channel API does not list the campaign",
                    campaign.name,
                    campaign.game_name(),
                    extra={"emoji": ":mag:", "event": Events.DROP_SCAN},
                )
                return channels
        return matching

    def __get_available_drop_campaigns(self, channel_ids):
        """channel id -> campaigns Twitch offers on that live channel (AvailableDrops)."""
        result = {}
        for chunk in create_chunks(list(channel_ids), 20):
            json_data = []
            for channel_id in chunk:
                operation = copy.deepcopy(GQLOperations.DropsHighlightService_AvailableDrops)
                # TwitchDropsMiner's hash returns the full drop data (rewards, minutes, dates).
                operation["extensions"]["persistedQuery"]["sha256Hash"] = (
                    GQLOperations.FallbackHashes["DropsHighlightService_AvailableDrops"]
                )
                operation["variables"] = {"channelID": str(channel_id)}
                json_data.append(operation)
            response = self.post_gql_request(json_data)
            if not isinstance(response, list):
                logger.debug(
                    "Drop scan: AvailableDrops failed for %s channel(s).%s",
                    len(chunk),
                    _graphql_error_details(response),
                )
                continue
            for channel_id, item in zip(chunk, response):
                try:
                    campaigns = item["data"]["channel"]["viewerDropCampaigns"]
                except (KeyError, TypeError):
                    continue
                result[channel_id] = [c for c in campaigns or [] if isinstance(c, dict)]
        return result

    def __get_stream_database_badges(self):
        """(badges, error text) - an unreachable or changed site must not stop the scan."""
        try:
            badges = fetch_watch_badges()
        except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as error:
            logger.warning(
                f"Drop scan: Stream Database is unavailable ({error!r}); "
                "using live channels and the inventory only"
            )
            return [], str(error) or error.__class__.__name__
        for badge in badges:
            logger.debug(f"Stream Database: {badge!r}")
        return badges, None

    def get_owned_badges(self):
        """
        (set IDs, titles) of the global chat badges the account owns, both lowercased,
        or None when Twitch did not answer.
        """
        response = self.post_gql_request(
            {
                "operationName": "AvailableBadges",
                "query": "query AvailableBadges { currentUser { availableBadges { setID title } } }",
                "variables": {},
            }
        )
        try:
            owned = response["data"]["currentUser"]["availableBadges"]
        except (KeyError, TypeError):
            logger.warning(
                "Drop scan: could not read the account's badges.%s",
                _graphql_error_details(response),
            )
            return None
        if not isinstance(owned, list):
            logger.warning("Drop scan: Twitch returned no account badge list")
            return None
        set_ids, titles = set(), set()
        for badge in owned or []:
            if not isinstance(badge, dict):
                continue
            if badge.get("setID"):
                set_ids.add(str(badge["setID"]).strip().lower())
            if badge.get("title"):
                titles.add(str(badge["title"]).strip().lower())
        return set_ids, titles

    def __badge_fallback_campaigns(self, badges, campaigns_data, first_offered):
        """Campaigns for running badges whose categories Twitch did not confirm."""
        covered_games = {
            str(data["game"]["id"])
            for data in campaigns_data
            if isinstance(data.get("game"), dict) and data["game"].get("id")
        }
        now = time.time()
        result = []
        for badge in badges:
            if not badge.is_running():
                continue
            for data in _campaigns_from_badge(badge):
                if data["game"]["id"] in covered_games:
                    continue
                first = first_offered.setdefault(data["id"], now)
                if now - first > STREAMDB_FALLBACK_SECONDS:
                    logger.debug(
                        f"Stream Database badge {badge.title} ({data['game']['name']}): "
                        "Twitch never showed progress, giving up"
                    )
                    continue
                result.append(data)
        return result

    def __discover_campaigns(self, streamers, known_campaigns, badges=()):
        """
        Twitch no longer gives app clients the campaign list (ViewerDropsDashboard and
        DropCampaignDetails return null since ~Sep 2026), but the per-channel AvailableDrops
        query still works. Probe channels for campaigns that are not known yet:
        the channels/categories of Stream Database watch badges first, then the user's live
        channels and live drops-enabled channels of the relevant categories (games of known
        campaigns and of the user's live streams with drops enabled).
        Returns (campaign data list, number of probed channels, category names).
        """
        channels = {}
        games = {}
        # Channels named by a badge or followed by the user are always kept in a campaign's
        # allow-list; directory channels are capped.
        preferred_channels = set()
        for badge in badges:
            for game in badge.games:
                games.setdefault(
                    game["id"],
                    {"id": game["id"], "name": game["name"], "displayName": game["name"],
                     "slug": game.get("slug")},
                )
            for channel_id, login in badge.channels[:STREAMDB_CHANNELS_PER_BADGE]:
                channels.setdefault(channel_id, login)
                preferred_channels.add(channel_id)
        for data in known_campaigns:
            game = data.get("game")
            if isinstance(game, dict) and game.get("id"):
                games.setdefault(str(game["id"]), game)
        for streamer in streamers:
            if (
                streamer.is_online is True
                and streamer.settings is not None
                and streamer.settings.claim_drops is True
                and streamer.channel_id
            ):
                channel_id = str(streamer.channel_id)
                channels[channel_id] = streamer.username
                preferred_channels.add(channel_id)
                game = streamer.stream.game
                if streamer.stream.drops_tags and isinstance(game, dict) and game.get("id"):
                    games.setdefault(str(game["id"]), game)
        for game in games.values():
            if not self.running:
                break
            for channel_id, login in self.get_drops_enabled_channels(game):
                channels.setdefault(channel_id, login)

        known_ids = {data.get("id") for data in known_campaigns}
        linked_by_game = {
            str(data["game"]["id"]): (data.get("self") or {}).get("isAccountConnected")
            is not False
            for data in known_campaigns
            if isinstance(data.get("game"), dict) and data["game"].get("id")
        }
        discovered = {}
        for channel_id, offered in self.__get_available_drop_campaigns(channels).items():
            entry = {"id": channel_id, "name": channels[channel_id]}
            for data in offered:
                campaign_id = data.get("id")
                if not campaign_id or campaign_id in known_ids:
                    continue
                if campaign_id in discovered:
                    allowed = discovered[campaign_id]["allow"]["channels"]
                    extra = [c for c in allowed if c["id"] not in preferred_channels]
                    if (
                        channel_id in preferred_channels
                        or len(extra) < DIRECTORY_CHANNELS_PER_CAMPAIGN
                    ):
                        allowed.append(entry)
                    continue
                game = data.get("game")
                game_id = str(game.get("id")) if isinstance(game, dict) else None
                campaign = _campaign_from_available_drops(
                    data, entry, linked_by_game.get(game_id, True)
                )
                if campaign is not None:
                    discovered[campaign_id] = campaign

        categories = sorted(
            {
                game.get("displayName") or game.get("name")
                for game in games.values()
                if game.get("displayName") or game.get("name")
            }
        )
        return list(discovered.values()), len(channels), categories

    def sync_campaigns(self, streamers, chunk_size=3):
        campaigns_update = 0
        campaigns = []
        discovered_streamers = {}
        # campaign id -> channel ids found in the game directory for that campaign
        directory_channels = {}
        directory_checked = {}
        # Stream Database fallback campaign id -> time it was first offered
        badge_first_offered = {}
        rotate = os.getenv("POKEMON_AUTO_ROTATE", "").strip().lower()
        if rotate not in ("", "0", "false", "no", "1", "true", "yes"):
            logger.warning("Invalid POKEMON_AUTO_ROTATE value; Pokemon rotation disabled")
        progression = ProgressionDrops(self) if rotate in ("1", "true", "yes") else None
        while self.running:
            scan_stats = None
            try:
                # Get update from dashboard each 60minutes
                if (
                    campaigns_update == 0
                    or ((time.time() - campaigns_update) / 60) > 60
                ):
                    campaigns_update = time.time()
                    # Main source: Stream Database's list of running watch badges. Twitch
                    # (AvailableDrops on their channels/categories) confirms the campaigns;
                    # the inventory adds campaigns that were already started.
                    badges, streamdb_error = self.__get_stream_database_badges()
                    dashboard = self.__get_drops_dashboard(status="ACTIVE")
                    campaigns_details = (
                        self.__get_campaigns_details(dashboard) if dashboard else []
                    )
                    inventory = self.__get_inventory() or {}
                    in_progress = [
                        c
                        for c in (inventory.get("dropCampaignsInProgress") or [])
                        if isinstance(c, dict) and c.get("id")
                    ]
                    earned_names = {
                        str(event["name"]).strip().lower()
                        for event in (inventory.get("gameEventDrops") or [])
                        if isinstance(event, dict) and event.get("name")
                    }
                    owned_sets = set()
                    if badges:
                        owned = self.get_owned_badges()
                        if owned is not None:
                            owned_sets, owned_titles = owned
                            earned_names |= owned_titles
                    open_badges = [
                        badge
                        for badge in badges
                        if str(badge.set_id).strip().lower() not in owned_sets
                        and str(badge.title).strip().lower() not in earned_names
                    ]
                    known = {c["id"]: c for c in in_progress}
                    known.update(
                        {
                            c["id"]: c
                            for c in campaigns_details
                            if isinstance(c, dict) and c.get("id")
                        }
                    )
                    discovered, probed, categories = self.__discover_campaigns(
                        streamers, list(known.values()), open_badges
                    )
                    if not self.running:
                        break
                    campaigns_data = list(known.values()) + discovered
                    campaigns_data += self.__badge_fallback_campaigns(
                        open_badges, campaigns_data, badge_first_offered
                    )
                    campaigns = []
                    scan_stats = {
                        "listed": len(campaigns_data),
                        "checked": len(campaigns_data),
                        "no_watch_drops": 0,
                        "not_linked": 0,
                        "in_progress": len(in_progress),
                        "probed_channels": probed,
                        "categories": categories,
                        "streamdb": len(badges),
                        "streamdb_earned": len(badges) - len(open_badges),
                        "streamdb_error": streamdb_error,
                    }

                    # Going to clear array and structure. Remove all the timeBasedDrops expired or not started yet
                    for data in campaigns_data:
                        try:
                            campaign = Campaign(data)
                        except (KeyError, TypeError, ValueError) as error:
                            logger.debug(f"Skipping unreadable drop campaign: {error!r}")
                            scan_stats["no_watch_drops"] += 1
                            continue
                        if campaign.dt_match is True:
                            # Remove all the drops already claimed or with dt not matching
                            campaign.clear_drops()
                            if campaign.drops == []:
                                scan_stats["no_watch_drops"] += 1
                                continue
                            if Settings.require_linked_account and not campaign.is_eligible:
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
                if progression is not None:
                    daily_campaigns = progression.sync(streamers)
                    # Legacy inventory/Stream Database guesses must not keep the
                    # same Pokemon reward active while its daily stage is paused.
                    active_campaigns = [
                        c for c in active_campaigns if not progression.manages(c)
                    ]
                    for campaign, (channel_id, login) in daily_campaigns:
                        active_campaigns.append(campaign)
                        directory_channels[campaign.id] = {channel_id}
                        if not any(s.channel_id == channel_id for s in streamers):
                            self.__add_drop_streamer(
                                streamers, discovered_streamers, channel_id, login
                            )
                active_ids = {c.id for c in active_campaigns}

                if scan_stats is not None:
                    scan_stats["already_earned"] = sum(not c.drops for c in campaigns)
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

                # Allow-lists can hold hundreds of channels, so they are not all added
                # and polled; live candidates come from the game directory instead.
                needed_channels = {
                    channel_id
                    for channels in directory_channels.values()
                    for channel_id in channels
                }

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

                # If none of the known channels can progress a campaign, look for live
                # drops-enabled channels in the game directory (for allow-list campaigns
                # only channels on the allow-list are kept).
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
                            status = (
                                "none of your channels is live, searching the "
                                "category for participating channels"
                            )
                        logger.info(
                            f"Drop '{campaign.name}' ({campaign.game_name()}): {status}",
                            extra={"emoji": ":mag:", "event": Events.DROP_SCAN},
                        )
                for campaign in active_campaigns:
                    if (
                        campaign.id.startswith(PROGRESSION_PREFIX)
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
