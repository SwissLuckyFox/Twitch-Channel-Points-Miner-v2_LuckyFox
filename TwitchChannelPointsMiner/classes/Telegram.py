import html
import json
import logging
import os
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
DEFAULT_PIN_LIMIT = 5


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
        "pin_limit",
        "pin_state_file",
        "_pins",
        "_pin_error",
    ]

    def __init__(
        self,
        chat_id: int,
        token: str,
        events: list,
        disable_notification: bool = False,
        pin_limit: int = DEFAULT_PIN_LIMIT,
        pin_state_file: str = None,
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
        # Highlighted messages are pinned; only the newest pin_limit stay pinned.
        self.pin_limit = max(int(pin_limit or 0), 0)
        self.pin_state_file = pin_state_file
        self._pins = None
        self._pin_error = None

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

    def send(
        self,
        message: str,
        event: Events,
        highlight: bool = False,
        pin_key: str = None,
    ) -> None:
        """
        Queue a notification; a background thread delivers it.
        highlight makes the first line bold; with a pin_key the message is also
        pinned (once per key).
        """
        if str(event) not in self.events:
            return
        try:
            self._queue.put_nowait((dedent(message), highlight, pin_key))
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
            item = self._queue.get()
            try:
                self._deliver(*item)
            except Exception:
                logger.debug("Unexpected Telegram error", exc_info=True)
            finally:
                self._queue.task_done()

    def _deliver(self, text: str, highlight: bool = False, pin_key: str = None) -> None:
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": self.disable_notification,
        }
        if highlight:
            first, _, rest = text.partition("\n")
            data["text"] = f"<b>{html.escape(first)}</b>" + (
                f"\n{html.escape(rest)}" if rest else ""
            )
            data["parse_mode"] = "HTML"
        for _ in range(MAX_RATE_LIMIT_RETRIES + 1):
            self._wait_for_slot()
            retry_after, message_id = self._post(data)
            self._last_sent = time.monotonic()
            if retry_after is None:
                if pin_key is not None and message_id is not None:
                    self._pin(message_id, str(pin_key))
                return
            logger.debug("Telegram rate limit hit; retrying in %s s", retry_after)
            time.sleep(retry_after)
        self._warn("Could not send Telegram notification (rate limited by Telegram).")

    def _wait_for_slot(self) -> None:
        wait = self._last_sent + MIN_SEND_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _api(self, method: str, data: dict):
        """Call a Bot API method; returns the decoded JSON or None."""
        api_base = self.telegram_api.rsplit("/", 1)[0]
        self._wait_for_slot()
        try:
            response = requests.post(
                url=f"{api_base}/{method}", data=data, timeout=10
            )
            result = response.json()
        except (requests.exceptions.RequestException, ValueError):
            result = None
        self._last_sent = time.monotonic()
        return result if isinstance(result, dict) else None

    def _load_pins(self) -> list:
        if self._pins is None:
            self._pins = []
            if self.pin_state_file and os.path.isfile(self.pin_state_file):
                try:
                    with open(self.pin_state_file, encoding="utf-8") as file:
                        stored = json.load(file)
                    if stored.get("chat_id") == self.chat_id:
                        self._pins = [
                            pin
                            for pin in stored.get("pins", [])
                            if isinstance(pin, dict) and pin.get("message_id")
                        ]
                except (OSError, ValueError, AttributeError):
                    logger.debug("Could not read Telegram pin state", exc_info=True)
        return self._pins

    def _save_pins(self) -> None:
        if not self.pin_state_file:
            return
        try:
            with open(self.pin_state_file, "w", encoding="utf-8") as file:
                json.dump({"chat_id": self.chat_id, "pins": self._pins}, file)
        except OSError:
            logger.debug("Could not save Telegram pin state", exc_info=True)

    def _pin(self, message_id: int, pin_key: str) -> None:
        if self.pin_limit <= 0:
            return
        pins = self._load_pins()
        if any(pin.get("key") == pin_key for pin in pins):
            return
        result = self._api(
            "pinChatMessage",
            {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "disable_notification": True,
            },
        )
        if not result or not result.get("ok"):
            description = (result or {}).get("description", "no response")
            message = (
                f"Could not pin Telegram message ({description}). The bot must be an "
                "admin of the chat with the 'Pin messages' right."
            )
            if message != self._pin_error:
                self._pin_error = message
                logger.warning(message)
            else:
                logger.debug(message)
            return
        self._pin_error = None
        pins.append({"key": pin_key, "message_id": message_id})
        while len(pins) > self.pin_limit:
            oldest = pins.pop(0)
            self._api(
                "unpinChatMessage",
                {"chat_id": self.chat_id, "message_id": oldest["message_id"]},
            )
        self._save_pins()

    def _post(self, data: dict):
        """
        Send one message. Returns (seconds to wait if rate limited, else None,
        message id if delivered).
        """
        message_id = None
        try:
            response = requests.post(
                url=self.telegram_api,
                data=data,
                timeout=10,
            )
            if response.status_code == 429:
                try:
                    retry_after = response.json()["parameters"]["retry_after"]
                    return min(max(int(retry_after), 1), MAX_RETRY_AFTER), None
                except (ValueError, KeyError, TypeError, AttributeError):
                    return 5, None
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
                sent = result.get("result")
                message_id = sent.get("message_id") if isinstance(sent, dict) else None
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
        return None, message_id
