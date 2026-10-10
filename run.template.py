# Copy this file to run.py on the Unraid appdata share.
# Configure secrets as container environment variables; do not commit them.

import logging
import os

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


def optional_telegram():
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not chat_id and not token:
        return None
    if not chat_id or not token:
        raise ValueError(
            "Set both TELEGRAM_CHAT_ID and TELEGRAM_BOT_TOKEN, or leave both unset."
        )

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
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return None

    return Discord(
        webhook_api=webhook_url,
        events=[
            Events.STREAMER_ONLINE,
            Events.STREAMER_OFFLINE,
            Events.BET_LOSE,
        ],
    )


twitch_miner = TwitchChannelPointsMiner(
    username=os.environ["TWITCH_USERNAME"],
    password=os.getenv("TWITCH_PASSWORD") or None,
    claim_drops_startup=False,
    priority=[
        Priority.DROPS,
        Priority.STREAK,
        Priority.ORDER,
        Priority.SUBSCRIBED,
    ],
    enable_analytics=True,
    disable_ssl_cert_verification=False,
    logger_settings=LoggerSettings(
        save=True,
        console_level=logging.INFO,
        console_username=False,
        auto_clear=True,
        time_zone="",
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
    streamer_settings=StreamerSettings(
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
)

twitch_miner.analytics(host="0.0.0.0", port=5000, refresh=5, days_ago=7)

twitch_miner.mine(
    [
        Streamer(
            "corgicam",
            settings=StreamerSettings(chat=ChatPresence.ONLINE),
        ),
        Streamer("caboose2253"),
        Streamer("zezeipanther"),
        Streamer("puppyava"),
        Streamer("helluvadragon"),
        Streamer("daxxfluff"),
        Streamer("zuzazaleno"),
        Streamer("lefunseeker"),
        Streamer("torrock_tigerfish"),
        Streamer("kibblesgg"),
        Streamer("sleepydoom"),
        Streamer("kanjoyume"),
        Streamer("allubillis"),
        Streamer("litheoss"),
        Streamer("merlekoz"),
        Streamer("ivydafurry"),
    ],
    followers=True,
    followers_order=FollowersOrder.ASC,
)
