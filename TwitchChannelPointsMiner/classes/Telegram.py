import logging
import queue
import threading
import time
from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.Settings import Events

logger = logging.getLogger(__name__)

# Telegram allows roughly one message per second to the same chat.
MIN_SEND_INTERVAL = 1.1
MAX_RATE_LIMIT_RETRIES = 5
MAX_RETRY_AFTER = 120
MAX_QUEUE_SIZE = 500


class Telegram(object):
    __slots__ = [
        "chat_id",
        "telegram_api",
        "events",
        "disable_notification",
        "last_error",
        "_queue",
        "_worker",
        "_worker_lock",
        "_last_sent",
    ]

    def __init__(
        self, chat_id: int, token: str, events: list, disable_notification: bool = False
    ):
        self.chat_id = chat_id
        self.telegram_api = f"https://api.telegram.org/bot{token}/sendMessage"
        self.events = [str(e) for e in events]
        self.disable_notification = disable_notification
        self.last_error = None
        self._queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._worker = None
        self._worker_lock = threading.Lock()
        self._last_sent = 0.0

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
        """Queue a notification; a background thread delivers it."""
        if str(event) not in self.events:
            return
        try:
            self._queue.put_nowait(dedent(message))
        except queue.Full:
            self._warn("Telegram queue is full; dropping notifications.")
            return
        self._ensure_worker()

    def flush(self) -> None:
        """Block until every queued notification has been handled."""
        self._queue.join()

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="Telegram notifications", daemon=True
                )
                self._worker.start()

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            try:
                self._deliver(text)
            except Exception:
                logger.debug("Unexpected Telegram error", exc_info=True)
            finally:
                self._queue.task_done()

    def _deliver(self, text: str) -> None:
        for _ in range(MAX_RATE_LIMIT_RETRIES + 1):
            wait = self._last_sent + MIN_SEND_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            retry_after = self._post(text)
            self._last_sent = time.monotonic()
            if retry_after is None:
                return
            logger.debug("Telegram rate limit hit; retrying in %s s", retry_after)
            time.sleep(retry_after)
        self._warn("Could not send Telegram notification (rate limited by Telegram).")

    def _post(self, text: str):
        """Send one message. Returns seconds to wait if rate limited, else None."""
        try:
            response = requests.post(
                url=self.telegram_api,
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                    "disable_notification": self.disable_notification,
                },
                timeout=10,
            )
            if response.status_code == 429:
                try:
                    retry_after = response.json()["parameters"]["retry_after"]
                    return min(max(int(retry_after), 1), MAX_RETRY_AFTER)
                except (ValueError, KeyError, TypeError, AttributeError):
                    return 5
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
                    description = response.json().get("description", description)
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
        return None
