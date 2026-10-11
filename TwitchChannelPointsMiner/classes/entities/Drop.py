from datetime import datetime

from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.utils import percentage


def parse_twitch_datetime(value):
    """Parse a Twitch UTC timestamp (with or without fractions) into a naive datetime."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


class Drop(object):
    __slots__ = [
        "id",
        "name",
        "benefit",
        "benefit_ids",
        "benefit_types",
        "minutes_required",
        "required_subs",
        "has_preconditions_met",
        "current_minutes_watched",
        "drop_instance_id",
        "is_claimed",
        "is_claimable",
        "percentage_progress",
        "end_at",
        "start_at",
        "dt_match",
        "is_printable",
    ]

    def __init__(self, dict):
        self.id = dict["id"]
        self.name = dict["name"]
        benefits = [
            edge["benefit"]
            for edge in (dict.get("benefitEdges") or [])
            if isinstance(edge, dict) and isinstance(edge.get("benefit"), dict)
        ]
        self.benefit = ", ".join(
            list(set([benefit.get("name", "") for benefit in benefits]))
        )
        self.benefit_ids = [b["id"] for b in benefits if b.get("id")]
        self.benefit_types = [
            b["distributionType"] for b in benefits if b.get("distributionType")
        ]
        self.minutes_required = dict.get("requiredMinutesWatched") or 0
        self.required_subs = dict.get("requiredSubs") or 0

        self.has_preconditions_met = None  # [True, False], None we don't know
        self.current_minutes_watched = 0
        self.drop_instance_id = None
        self.is_claimed = False
        self.is_claimable = False
        self.is_printable = False
        self.percentage_progress = 0

        self.end_at = datetime.strptime(dict["endAt"], "%Y-%m-%dT%H:%M:%SZ")
        self.start_at = datetime.strptime(dict["startAt"], "%Y-%m-%dT%H:%M:%SZ")
        self.dt_match = self.start_at < datetime.now() < self.end_at

    @property
    def is_watchable(self):
        # "Subscribe to Redeem" drops need subs and have no watch-time requirement;
        # they can't be earned by watching, so they are ignored.
        return self.required_subs <= 0 and self.minutes_required > 0

    @property
    def is_badge_or_emote(self):
        return any(t in ("BADGE", "EMOTE") for t in self.benefit_types)

    def mark_claimed_from_benefits(self, claimed_benefits):
        """
        Campaigns that were already completed are no longer listed in the inventory's
        in-progress section; their rewards show up in gameEventDrops instead.
        claimed_benefits maps benefit id -> lastAwardedAt (naive UTC datetime).
        """
        if self.is_claimed or not self.benefit_ids:
            return
        awarded = [
            claimed_benefits[benefit_id]
            for benefit_id in self.benefit_ids
            if benefit_id in claimed_benefits
        ]
        if awarded and all(
            awarded_at is not None and self.start_at <= awarded_at < self.end_at
            for awarded_at in awarded
        ):
            self.is_claimed = True

    def update(
        self,
        progress,
    ):
        self.has_preconditions_met = progress["hasPreconditionsMet"]

        updated_percentage = percentage(
            progress["currentMinutesWatched"], self.minutes_required
        )
        quarter = round((updated_percentage / 25), 4).is_integer()
        self.is_printable = (
            # The new currentMinutesWatched are GT than previous
            progress["currentMinutesWatched"] > self.current_minutes_watched
            and (
                # The drop is printable when we have a new updated values and:
                #  - also the percentage It's different and  quarter is True (self.current_minutes_watched != 0 for skip boostrap phase)
                #  - or we have watched 1 and the previous value is 0 - We are collecting a new drop :)
                (
                    updated_percentage > self.percentage_progress
                    and quarter is True
                    and self.current_minutes_watched != 0
                )
                or (
                    progress["currentMinutesWatched"] == 1
                    and self.current_minutes_watched == 0
                )
            )
        )

        self.current_minutes_watched = progress["currentMinutesWatched"]
        self.drop_instance_id = progress["dropInstanceID"]
        self.is_claimed = progress["isClaimed"]
        self.is_claimable = (
            self.is_claimed is False and self.drop_instance_id is not None
        )
        self.percentage_progress = updated_percentage

    def __repr__(self):
        return f"Drop(id={self.id}, name={self.name}, benefit={self.benefit}, minutes_required={self.minutes_required}, has_preconditions_met={self.has_preconditions_met}, current_minutes_watched={self.current_minutes_watched}, percentage_progress={self.percentage_progress}%, drop_instance_id={self.drop_instance_id}, is_claimed={self.is_claimed})"

    def __str__(self):
        return (
            f"{self.name} ({self.benefit}) {self.current_minutes_watched}/{self.minutes_required} ({self.percentage_progress}%)"
            if Settings.logger.less
            else self.__repr__()
        )

    def progress_bar(self):
        progress = self.percentage_progress // 2
        remaining = (100 - self.percentage_progress) // 2
        if remaining + progress < 50:
            remaining += 50 - (remaining + progress)
        return f"|{('█' * progress)}{(' ' * remaining)}|\t{self.percentage_progress}% [{self.current_minutes_watched}/{self.minutes_required}]"

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.id == other.id
        else:
            return False
