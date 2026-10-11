# Copy this file to run.py on the Unraid appdata share.
# Configure secrets as container environment variables; do not commit them.

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from colorama import Fore
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Discord import Discord
from TwitchChannelPointsMiner.classes.Settings import Events, FollowersOrder, Priority
from TwitchChannelPointsMiner.classes.Telegram import Telegram
from TwitchChannelPointsMiner.classes.entities.Bet import (
    BetSettings,
    Condition,
    DelayMode,
    FilterCondition,
    OutcomeKeys,
    Strategy,
)
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.logger import ColorPalette, LoggerSettings

logger = logging.getLogger("run")
STREAMERS_FILE = Path("/usr/src/app/user-config/streamers.txt")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(handler)


def optional_telegram():
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not chat_id and not token:
        return None
    if not chat_id or not token:
        logger.warning(
            "Telegram disabled: set both TELEGRAM_CHAT_ID and "
            "TELEGRAM_BOT_TOKEN, or leave both empty."
        )
        return None

    if not re.fullmatch(r"-?\d+", chat_id):
        logger.warning("Telegram disabled: TELEGRAM_CHAT_ID must be an integer.")
        return None
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        logger.warning("Telegram disabled: TELEGRAM_BOT_TOKEN has an invalid format.")
        return None
    if chat_id == token.split(":", 1)[0]:
        logger.warning(
            "Telegram disabled: TELEGRAM_CHAT_ID is the bot's own ID (the number "
            "before ':' in the token). Use your user or group chat ID instead."
        )
        return None

    return Telegram(
        chat_id=int(chat_id),
        token=token,
        events=[
            Events.STREAMER_ONLINE,
            Events.STREAMER_OFFLINE,
            Events.BET_LOSE,
            Events.GAIN_FOR_WATCH_STREAK,
            Events.BET_REFUND,
            Events.BET_WIN,
            Events.GAIN_FOR_CLAIM,
            Events.GAIN_FOR_RAID,
            Events.BONUS_CLAIM,
        ],
        disable_notification=True,
    )


def optional_discord():
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return None

    parsed_url = urlsplit(webhook_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname not in {"discord.com", "discordapp.com"}
        or not re.fullmatch(r"/api/webhooks/\d+/[^/]+/?", parsed_url.path)
    ):
        logger.warning(
            "Discord disabled: DISCORD_WEBHOOK_URL is not a valid Discord webhook URL."
        )
        return None

    return Discord(
        webhook_api=webhook_url,
        events=[
            Events.STREAMER_ONLINE,
            Events.STREAMER_OFFLINE,
            Events.BET_LOSE,
        ],
    )


def build_miner_kwargs():
    username = os.getenv("TWITCH_USERNAME", "").strip()
    if not username:
        raise ValueError(
            "TWITCH_USERNAME is required; set it in the Unraid container variables."
        )

    return {
        "username": username,
        "claim_drops_startup": False,
        "priority": [
            Priority.DROPS,
            Priority.STREAK,
            Priority.ORDER,
            Priority.SUBSCRIBED,
        ],
        "logger_settings": LoggerSettings(
            save=True,
            console_level=logging.INFO,
            auto_clear=True,
            file_level=logging.INFO,
            emoji=True,
            less=True,
            colored=True,
            color_palette=ColorPalette(
                STREAMER_ONLINE=Fore.GREEN,
                STREAMER_OFFLINE=Fore.RED,
                GAIN_FOR_RAID=Fore.YELLOW,
                GAIN_FOR_CLAIM=Fore.YELLOW,
                GAIN_FOR_WATCH=Fore.YELLOW,
                GAIN_FOR_WATCH_STREAK=Fore.YELLOW,
                BET_WIN=Fore.MAGENTA,
                BET_LOSE=Fore.RED,
                BET_REFUND=Fore.RESET,
                BET_FILTERS=Fore.MAGENTA,
                BET_GENERAL=Fore.BLUE,
                BET_FAILED=Fore.RED,
            ),
            telegram=optional_telegram(),
            discord=optional_discord(),
        ),
        "streamer_settings": StreamerSettings(
            make_predictions=False,
            follow_raid=True,
            claim_drops=True,
            watch_streak=True,
            chat=ChatPresence.NEVER,
            bet=BetSettings(
                strategy=Strategy.SMART,
                percentage=5,
                percentage_gap=20,
                max_points=50000,
                stealth_mode=True,
                delay_mode=DelayMode.FROM_END,
                delay=6,
                minimum_points=100000,
                filter_condition=FilterCondition(
                    by=OutcomeKeys.TOTAL_USERS,
                    where=Condition.LTE,
                    value=800,
                ),
            ),
        ),
    }


def build_streamers(streamers_file=STREAMERS_FILE):
    streamers = []
    usernames = set()

    try:
        with Path(streamers_file).open(encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                line = raw_line.partition("#")[0].strip()
                if not line:
                    continue

                fields = line.split()
                if len(fields) > 2:
                    raise ValueError(
                        f"{streamers_file}:{line_number}: expected a username "
                        "and optional chat=SETTING."
                    )

                username = fields[0]
                username_key = username.casefold()
                if username_key in usernames:
                    raise ValueError(
                        f"{streamers_file}:{line_number}: duplicate streamer "
                        f"'{username}'."
                    )

                settings = None
                if len(fields) == 2:
                    option, separator, value = fields[1].partition("=")
                    if not separator or option.casefold() != "chat":
                        raise ValueError(
                            f"{streamers_file}:{line_number}: unsupported option "
                            f"'{fields[1]}'; use chat=ALWAYS|NEVER|ONLINE|OFFLINE."
                        )
                    try:
                        settings = StreamerSettings(
                            chat=ChatPresence[value.upper()]
                        )
                    except KeyError as error:
                        raise ValueError(
                            f"{streamers_file}:{line_number}: invalid chat setting "
                            f"'{value}'; use ALWAYS, NEVER, ONLINE, or OFFLINE."
                        ) from error

                usernames.add(username_key)
                streamers.append(Streamer(username, settings=settings))
    except FileNotFoundError as error:
        raise ValueError(
            f"Streamer list not found at {streamers_file}. Create the file on the "
            "Unraid host and set the Streamer list path mapping to that file."
        ) from error
    except IsADirectoryError as error:
        raise ValueError(
            f"Streamer list path {streamers_file} points to a directory, not a "
            "file. Create streamers.txt on the Unraid host and map that file."
        ) from error

    if not streamers:
        raise ValueError(
            f"No streamers configured in {streamers_file}. Add one Twitch "
            "username per line."
        )

    return streamers


def main():
    try:
        miner_kwargs = build_miner_kwargs()
        streamers = build_streamers()
    except ValueError as error:
        logger.error("%s", error)
        raise SystemExit(2) from error

    twitch_miner = TwitchChannelPointsMiner(**miner_kwargs)
    twitch_miner.analytics(host="0.0.0.0", port=5000, refresh=5, days_ago=7)
    twitch_miner.mine(
        streamers,
        followers=True,
        followers_order=FollowersOrder.ASC,
    )


if __name__ == "__main__":
    main()
