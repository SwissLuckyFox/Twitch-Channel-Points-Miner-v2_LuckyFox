import unittest
from datetime import datetime
from unittest.mock import Mock

import requests

from TwitchChannelPointsMiner.classes.TwitchDrops import fetch_drop_games


def card(slug="hunt-showdown", name="Hunt: Showdown 1896", text="15m watch",
         start="2026-10-01T00:00:00Z", end="1793491200000"):
    return (
        f'<a class="game-card" data-slug="{slug}" data-game="{name}" '
        f'data-start="{start}" data-end-ts="{end}">'
        f'<div><span>{text}</span></div></a>'
    )


class TwitchDropsTests(unittest.TestCase):
    def fetch(self, html):
        self.session = Mock()
        self.session.get.return_value.text = html
        return fetch_drop_games(
            self.session, now=datetime(2026, 10, 27, 17)
        )

    def test_parses_active_watch_categories_and_deduplicates(self):
        games = self.fetch(
            '<div id="grid">' + card() + card()
            + card("other", "Other &amp; More") + "</div>"
        )
        self.assertEqual(len(games), 2)
        self.assertEqual(games[0]["slug"], "hunt-showdown")
        self.assertEqual(games[1]["name"], "Other & More")
        self.session.get.assert_called_once()
        self.assertEqual(self.session.get.call_args.args, ("https://twitchdrops.app/",))
        self.session.get.return_value.raise_for_status.assert_called_once()

    def test_ignores_subscriptions_upcoming_expired_and_cards_outside_grid(self):
        games = self.fetch(
            '<a class="game-card">archive</a><div id="grid">'
            + card("subs", text="1 sub")
            + card("future", start="2026-11-01T00:00:00Z")
            + card("expired", end="1")
            + card() + '</div><a class="game-card">archive</a>'
        )
        self.assertEqual([game["slug"] for game in games], ["hunt-showdown"])

    def test_changed_page_and_malformed_cards_report_failure(self):
        for html in (
            "<html>unavailable</html>",
            '<div id="grid">' + card(end="invalid") + "</div>",
            '<div id="grid">' + card(start="invalid") + "</div>",
            '<div id="grid">' + card(slug="../other") + "</div>",
        ):
            with self.subTest(html=html), self.assertRaises(ValueError):
                self.fetch(html)

    def test_http_and_network_failures_propagate(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.Timeout()
        with self.assertRaises(requests.exceptions.Timeout):
            fetch_drop_games(session)
        session.get.side_effect = None
        session.get.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError()
        with self.assertRaises(requests.exceptions.HTTPError):
            fetch_drop_games(session)


if __name__ == "__main__":
    unittest.main()
