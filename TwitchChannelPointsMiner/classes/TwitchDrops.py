"""Category discovery from twitchdrops.app; Twitch confirms campaigns and progress."""

import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

from TwitchChannelPointsMiner.classes.entities.Drop import parse_twitch_datetime
from TwitchChannelPointsMiner.utils import get_user_agent

logger = logging.getLogger(__name__)
BASE_URL = "https://twitchdrops.app"


class _GameParser(HTMLParser):
    def __init__(self, now):
        super().__init__(convert_charrefs=True)
        self.now = now
        self.games = {}
        self.card = None
        self.cards_seen = 0
        self.grid_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and (self.grid_depth or attrs.get("id") == "grid"):
            self.grid_depth += 1
        if (
            not self.grid_depth
            or tag != "a"
            or "game-card" not in (attrs.get("class") or "").split()
        ):
            return
        self.cards_seen += 1
        slug = attrs.get("data-slug") or ""
        name = attrs.get("data-game") or ""
        start = parse_twitch_datetime(attrs.get("data-start"))
        try:
            end = float(attrs["data-end-ts"]) / 1000
        except (KeyError, TypeError, ValueError):
            raise ValueError("twitchdrops.app game card has no valid end time")
        if not re.fullmatch(r"[a-z0-9-]+", slug) or not name or start is None:
            raise ValueError("twitchdrops.app game card has no valid category/start time")
        end_at = datetime.fromtimestamp(end, timezone.utc).replace(tzinfo=None)
        if start <= self.now < end_at:
            self.card = {"name": name, "displayName": name, "slug": slug}

    def handle_data(self, data):
        # Ignore subscription-only cards. This is a discovery hint, not proof
        # that any particular viewer is eligible for the campaign.
        if self.card is not None and re.search(r"\bwatch\b", data, re.I):
            self.games.setdefault(self.card["slug"], self.card)

    def handle_endtag(self, tag):
        if tag == "a":
            self.card = None
        if tag == "div" and self.grid_depth:
            self.grid_depth -= 1


def fetch_drop_games(session=None, now=None):
    """Fetch active watch-drop categories without sending Twitch credentials."""
    session = session or requests
    response = session.get(
        BASE_URL + "/",
        headers={"User-Agent": get_user_agent("FIREFOX"), "Accept": "text/html"},
        timeout=20,
    )
    response.raise_for_status()
    parser = _GameParser(now or datetime.now(timezone.utc).replace(tzinfo=None))
    parser.feed(response.text)
    parser.close()
    if not parser.cards_seen:
        raise ValueError(
            "twitchdrops.app returned no game cards; page format may have changed"
        )
    games = list(parser.games.values())
    logger.debug("twitchdrops.app: %s active watch-drop categories", len(games))
    return games
