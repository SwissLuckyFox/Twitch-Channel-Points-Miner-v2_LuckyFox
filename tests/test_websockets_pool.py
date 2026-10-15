import unittest
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool


def fake_ws(pool, index=0):
    return SimpleNamespace(
        index=index,
        parent_pool=pool,
        is_closed=False,
        is_reconneting=False,
        forced_close=False,
        keep_running=True,
        reconnect_lock=Lock(),
        topics=["topic"],
        close=MagicMock(),
    )


class ReconnectionTests(unittest.TestCase):
    def test_watchdog_and_on_close_reconnect_only_once(self):
        pool = WebSocketsPool(twitch=None, streamers=[], events_predictions={})
        ws = fake_ws(pool)
        pool.ws = [ws]
        with patch("TwitchChannelPointsMiner.classes.WebSocketsPool.time.sleep"), patch(
            "TwitchChannelPointsMiner.classes.WebSocketsPool.internet_connection_available",
            return_value=True,
        ), patch.object(
            WebSocketsPool, "_WebSocketsPool__new", side_effect=lambda i: fake_ws(pool, i)
        ) as new, patch.object(
            WebSocketsPool, "_WebSocketsPool__start"
        ), patch.object(
            WebSocketsPool, "_WebSocketsPool__submit"
        ):
            WebSocketsPool.handle_reconnection(ws)
            WebSocketsPool.on_close(ws, None, None)

        self.assertEqual(new.call_count, 1)
        self.assertTrue(ws.is_closed)
        self.assertIsNot(pool.ws[0], ws)

    def test_messages_from_replaced_socket_are_ignored(self):
        pool = WebSocketsPool(twitch=None, streamers=[], events_predictions={})
        old = fake_ws(pool)
        pool.ws = [fake_ws(pool)]
        # Would raise on json.loads if the message were processed.
        WebSocketsPool.on_message(old, "not json")


if __name__ == "__main__":
    unittest.main()
