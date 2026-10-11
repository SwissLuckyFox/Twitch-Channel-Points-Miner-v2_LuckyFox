import logging
from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.Settings import Events

logger = logging.getLogger(__name__)


class Telegram(object):
    __slots__ = [
        "chat_id",
        "telegram_api",
        "events",
        "disable_notification",
        "last_error",
    ]

    def __init__(
        self, chat_id: int, token: str, events: list, disable_notification: bool = False
    ):
        self.chat_id = chat_id
        self.telegram_api = f"https://api.telegram.org/bot{token}/sendMessage"
        self.events = [str(e) for e in events]
        self.disable_notification = disable_notification
        self.last_error = None

    def _warn(self, message: str) -> None:
        # Log each distinct failure once instead of once per notification.
        if message != self.last_error:
            self.last_error = message
            logger.warning(message)
        else:
            logger.debug(message)

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
                    description = (
                        result.get("description", "unexpected API response")
                        if isinstance(result, dict)
                        else "unexpected API response"
                    )
                    self._warn(f"Telegram rejected a notification: {description}")
                else:
                    self.last_error = None
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
                self._warn(
                    f"Could not send Telegram notification (HTTP {status}: "
                    f"{description})."
                )
            except (requests.exceptions.RequestException, ValueError) as error:
                self._warn(
                    f"Could not send Telegram notification ({type(error).__name__})."
                )
