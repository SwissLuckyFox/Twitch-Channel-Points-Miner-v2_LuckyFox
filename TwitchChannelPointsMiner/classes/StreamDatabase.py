"""
Watch-time badges listed by Stream Database (https://www.streamdatabase.com/twitch/global-badges).

Twitch hides its drop campaign list from app clients, so this community-maintained list is
used to learn which categories/channels currently give a badge for watching. The pages are
Next.js pages: the data is embedded as JSON in the __NEXT_DATA__ script tag.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from TwitchChannelPointsMiner.classes.entities.Drop import parse_twitch_datetime
from TwitchChannelPointsMiner.utils import get_user_agent

logger = logging.getLogger(__name__)

BASE_URL = "https://www.streamdatabase.com"
BADGES_PATH = "/twitch/global-badges"
DETAIL_CACHE_SECONDS = 6 * 60 * 60
MAX_DETAIL_REQUESTS = 25
# Badges without dates ("upcoming") are only looked at while they are this new.
RECENT_DAYS = 45

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)
_WATCH_WORDS = re.compile(r"\b(watch|view)", re.I)
# (set_id, version) -> (fetched_at, availabilities)
_detail_cache = {}


class WatchBadge(object):
    __slots__ = ["set_id", "title", "minutes", "games", "channels", "start_at", "end_at"]

    def __init__(self, set_id, title, minutes, games, channels, start_at, end_at):
        self.set_id = set_id
        self.title = title
        self.minutes = minutes
        # [{"id": str, "name": str, "slug": str|None}]
        self.games = games
        # [(channel_id, login)]; empty means any channel of the categories
        self.channels = channels
        # Naive UTC datetimes, None when Stream Database does not know them yet
        self.start_at = start_at
        self.end_at = end_at

    def is_running(self, now=None):
        now = now or _utc_now()
        return (
            self.start_at is not None
            and self.end_at is not None
            and self.start_at <= now < self.end_at
        )

    def category_names(self):
        return [game["name"] for game in self.games if game.get("name")]

    def __repr__(self):
        return (
            f"WatchBadge({self.title}, {self.minutes} min, "
            f"categories={self.category_names()}, channels={len(self.channels)})"
        )


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _page_props(html):
    match = _NEXT_DATA.search(html or "")
    if match is None:
        raise ValueError("Stream Database page has no __NEXT_DATA__ block")
    return json.loads(match.group(1))["props"]["pageProps"]


def _fetch_props(session, path):
    response = session.get(
        BASE_URL + path,
        headers={"User-Agent": get_user_agent("FIREFOX"), "Accept": "text/html"},
        timeout=20,
    )
    response.raise_for_status()
    return _page_props(response.text)


def _parse_date(date, clock, end=False):
    if not isinstance(date, str) or not date:
        return None
    clock = (clock if isinstance(clock, str) and clock else ("23:59" if end else "00:00"))[:5]
    try:
        return datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _is_candidate(item, now):
    """Cheap filter on the badge list so only possibly running watch badges are opened."""
    if not isinstance(item, dict) or item.get("removed") or item.get("cost") == "paid":
        return False
    version = ((item.get("current") or {}).get("version")) or {}
    text = f"{version.get('title') or ''} {version.get('description') or ''}"
    if not _WATCH_WORDS.search(text):
        return False
    start = _parse_date(item.get("start_at_date"), item.get("start_at_time"))
    end = _parse_date(item.get("end_at_date"), item.get("end_at_time"), end=True)
    if end is not None and end <= now:
        return False
    if start is not None and start > now:
        return False
    if start is None and end is None:
        added = parse_twitch_datetime(item.get("added_at") or item.get("created_at"))
        return added is not None and now - added < timedelta(days=RECENT_DAYS)
    return True


def _watch_minutes(objectives):
    """Smallest watch time of an objective group that only asks for watching."""
    minutes = []
    for group in objectives or []:
        group = group if isinstance(group, list) else [group]
        if group and all(
            isinstance(o, dict) and o.get("type") == "twitch_watch" for o in group
        ):
            total = sum(int(o.get("minutes") or 0) for o in group)
            if total > 0:
                minutes.append(total)
    return min(minutes) if minutes else None


def _badge_from_availability(item, availability):
    if not isinstance(availability, dict) or availability.get("hidden"):
        return None
    if (availability.get("cost") or item.get("cost")) == "paid":
        return None
    minutes = _watch_minutes(availability.get("objectives"))
    if minutes is None:
        return None

    games = []
    for category in availability.get("categories") or []:
        game = category.get("game") if isinstance(category, dict) else None
        if isinstance(game, dict) and game.get("id") and game.get("name"):
            games.append(
                {"id": str(game["id"]), "name": game["name"], "slug": category.get("slug")}
            )
    channels = []
    for channel in availability.get("channels") or []:
        user = channel.get("user") if isinstance(channel, dict) else None
        if isinstance(user, dict) and user.get("id") and user.get("login"):
            channels.append((str(user["id"]), user["login"].lower()))
    if not games and not channels:
        return None

    version = ((item.get("current") or {}).get("version")) or {}
    start = _parse_date(
        availability.get("start_at_date") or item.get("start_at_date"),
        availability.get("start_at_time") or item.get("start_at_time"),
    )
    end = _parse_date(
        availability.get("end_at_date") or item.get("end_at_date"),
        availability.get("end_at_time") or item.get("end_at_time"),
        end=True,
    )
    return WatchBadge(
        set_id=(item.get("current") or {}).get("set_id"),
        title=version.get("title") or (item.get("current") or {}).get("set_id"),
        minutes=minutes,
        games=games,
        channels=channels,
        start_at=start,
        end_at=end,
    )


def _availabilities(session, set_id, version_id, now_ts):
    key = (set_id, version_id)
    cached = _detail_cache.get(key)
    if cached is not None and now_ts - cached[0] < DETAIL_CACHE_SECONDS:
        return cached[1]
    props = _fetch_props(session, f"{BADGES_PATH}/{set_id}/{version_id}")
    availabilities = props.get("availabilities") or []
    _detail_cache[key] = (now_ts, availabilities)
    return availabilities


def fetch_watch_badges(session=None, now=None):
    """
    Free badges that are earned by watching and have not ended yet.
    Raises requests/parsing errors when the badge list itself can't be loaded.
    """
    session = session or requests
    now = now or _utc_now()
    items = _fetch_props(session, BADGES_PATH).get("initialTwitchGlobalBadges")
    if not isinstance(items, list):
        raise ValueError("Stream Database badge list has an unknown format")

    candidates = [item for item in items if _is_candidate(item, now)]
    candidates.sort(key=lambda item: item.get("added_at") or "", reverse=True)

    badges = []
    for item in candidates[:MAX_DETAIL_REQUESTS]:
        current = item.get("current") or {}
        set_id = current.get("set_id")
        version_id = (current.get("version") or {}).get("id") or "1"
        if not set_id:
            continue
        try:
            availabilities = _availabilities(session, set_id, version_id, time.time())
        except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as error:
            logger.debug(f"Stream Database: could not load badge {set_id}: {error!r}")
            continue
        for availability in availabilities:
            badge = _badge_from_availability(item, availability)
            if badge is None:
                continue
            if badge.end_at is not None and badge.end_at <= now:
                continue
            if badge.start_at is not None and badge.start_at > now:
                continue
            badges.append(badge)
            break
    return badges
