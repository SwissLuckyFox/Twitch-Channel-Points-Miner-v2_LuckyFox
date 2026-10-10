import logging
from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.Settings import Events

logger = logging.getLogger(__name__)


class Telegram(object):
    __slots__ = ["chat_id", "telegram_api", "events", "disable_notification"]

    def __init__(
        self, chat_id: int, token: str, events: list, disable_notification: bool = False
    ):
        self.chat_id = chat_id
        self.telegram_api = f"https://api.telegram.org/bot{token}/sendMessage"
        self.events = [str(e) for e in events]
        self.disable_notification = disable_notification

    def send(self, message: str, event: Events) -> None:
        if str(event) in self.events:
            try:
                response = requests.post(
                    url=self.telegram_api,
                    data={
                        "chat_id": self.chat_id,
                        "text": dedent(message),
                        "disable_web_page_preview": True,
                        "disable_notification": self.disable_notification,
                    },
                    timeout=10,
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or not result.get("ok", False):
                    logger.warning(
                        "Telegram rejected a notification: %s",
                        result.get("description", "unexpected API response")
                        if isinstance(result, dict)
                        else "unexpected API response",
                    )
            except requests.exceptions.HTTPError as error:
                # Never log the request URL: it contains the bot token.
                response = error.response
                status = response.status_code if response is not None else "unknown"
                description = "no description"
                if response is not None:
                    try:
                        description = response.json().get(
                            "description", description
                        )
                    except (ValueError, AttributeError):
                        pass
                logger.warning(
                    "Could not send Telegram notification (HTTP %s: %s).",
                    status,
                    description,
                )
            except (requests.exceptions.RequestException, ValueError) as error:
                logger.warning(
                    "Could not send Telegram notification (%s).",
                    type(error).__name__,
                )
