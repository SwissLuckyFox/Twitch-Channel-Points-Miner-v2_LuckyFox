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

    def verify(self) -> bool:
        """Check at startup that the bot can reach the configured chat."""
        api_base = self.telegram_api.rsplit("/", 1)[0]
        try:
            response = requests.get(
                f"{api_base}/getChat", params={"chat_id": self.chat_id}, timeout=10
            )
            result = response.json()
        except (requests.exceptions.RequestException, ValueError) as error:
            # Never log the request URL: it contains the bot token.
            logger.warning(
                "Telegram check failed: could not reach Telegram (%s).",
                type(error).__name__,
            )
            return False

        if isinstance(result, dict) and result.get("ok"):
            chat = result.get("result") or {}
            name = (
                chat.get("title")
                or chat.get("username")
                or chat.get("first_name")
                or "unknown"
            )
            logger.info(
                "Telegram ready: sending to %s chat '%s' (ID %s).",
                chat.get("type", "unknown"),
                name,
                self.chat_id,
            )
            return True

        description = (
            result.get("description", "unexpected API response")
            if isinstance(result, dict)
            else "unexpected API response"
        )
        logger.warning(
            "Telegram check failed for chat ID %s (HTTP %s: %s). Notifications "
            "will not arrive until TELEGRAM_CHAT_ID / TELEGRAM_BOT_TOKEN are fixed.",
            self.chat_id,
            response.status_code,
            description,
        )
        return False

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
