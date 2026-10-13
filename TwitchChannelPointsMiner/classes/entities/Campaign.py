from datetime import datetime

from TwitchChannelPointsMiner.classes.entities.Drop import Drop, parse_twitch_datetime
from TwitchChannelPointsMiner.classes.Settings import Settings


class Campaign(object):
    __slots__ = [
        "id",
        "game",
        "name",
        "status",
        "in_inventory",
        "end_at",
        "start_at",
        "dt_match",
        "drops",
        "channels",
        "account_connected",
    ]

    def __init__(self, dict):
        self.id = dict["id"]
        self.game = dict["game"]
        self.name = dict["name"]
        self.status = dict.get("status") or "ACTIVE"
        allow = dict.get("allow") or {}
        allowed_channels = allow.get("channels")
        self.channels = (
            [channel["id"] for channel in allowed_channels]
            if allowed_channels and allow.get("isEnabled", True) is not False
            else []
        )
        self.in_inventory = False
        self_data = dict.get("self") or {}
        self.account_connected = self_data.get("isAccountConnected") is not False

        self.end_at = parse_twitch_datetime(dict["endAt"])
        self.start_at = parse_twitch_datetime(dict["startAt"])
        if self.end_at is None or self.start_at is None:
            raise ValueError(f"Campaign {self.id} has no valid start/end date")
        self.dt_match = self.start_at < datetime.now() < self.end_at

        self.drops = [
            drop
            for drop in map(lambda x: Drop(x), dict.get("timeBasedDrops") or [])
            if drop.is_watchable
        ]

    @property
    def is_eligible(self):
        if getattr(Settings, "require_linked_account", False) is False:
            return True
        # Badges/emotes need no linked game account; other rewards do.
        return self.account_connected or any(
            drop.is_badge_or_emote for drop in self.drops
        )

    @property
    def any_channel(self):
        # No allow-list: every live channel streaming the game with drops enabled counts.
        return self.channels == []

    def game_name(self):
        if not isinstance(self.game, dict):
            return None
        return self.game.get("displayName") or self.game.get("name")

    def mark_claimed_from_benefits(self, claimed_benefits):
        for drop in self.drops:
            drop.mark_claimed_from_benefits(claimed_benefits)

    def __repr__(self):
        return f"Campaign(id={self.id}, name={self.name}, game={self.game}, in_inventory={self.in_inventory})"

    def __str__(self):
        return (
            f"{self.name}, Game: {self.game['displayName']} - Drops: {len(self.drops)} pcs. - In inventory: {self.in_inventory}"
            if Settings.logger.less
            else self.__repr__()
        )

    def clear_drops(self):
        self.drops = list(
            filter(lambda x: x.dt_match is True and x.is_claimed is False, self.drops)
        )

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.id == other.id
        else:
            return False

    def sync_drops(self, drops, callback):
        # Iterate all the drops from inventory
        for drop in drops:
            # Iterate all the drops from out campaigns array
            # After id match update with:
            # [currentMinutesWatched, hasPreconditionsMet, dropInstanceID, isClaimed]
            for i in range(len(self.drops)):
                current_id = self.drops[i].id
                if drop["id"] == current_id:
                    if not isinstance(drop.get("self"), dict):
                        break
                    self.drops[i].update(drop["self"])
                    # If after update we all conditions are meet we can claim the drop
                    if self.drops[i].is_claimable is True:
                        claimed = callback(self.drops[i])
                        self.drops[i].is_claimed = claimed
                    break
