import logging
from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.Settings import Events

logger = logging.getLogger(__name__)


class Discord(object):
    __slots__ = ["webhook_api", "events"]

    def __init__(self, webhook_api: str, events: list):
        self.webhook_api = webhook_api
        self.events = [str(e) for e in events]

    def send(self, message: str, event: Events) -> None:
        if str(event) in self.events:
            try:
                response = requests.post(
                    url=self.webhook_api,
                    data={
                        "content": dedent(message),
                        "username": "Twitch Channel Points Miner",
                        "avatar_url": "https://i.imgur.com/X9fEkhT.png",
                    },
                    timeout=10,
                )
                response.raise_for_status()
            except requests.exceptions.RequestException as error:
                logger.warning(
                    "Could not send Discord notification (%s).",
                    type(error).__name__,
                )
