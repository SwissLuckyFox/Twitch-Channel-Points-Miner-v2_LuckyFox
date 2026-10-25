import copy
import logging
from datetime import datetime, timezone

from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.entities.Drop import parse_twitch_datetime
from TwitchChannelPointsMiner.constants import GQLOperations

logger = logging.getLogger(__name__)
PROGRESSION_PREFIX = "progression:"


def daily_watchable(group, now):
    criteria = group.get("progressCriteria") or {}
    progress = group.get("self")
    if not isinstance(progress, dict):
        return False
    if progress.get("status") != "IN_PROGRESS":
        return False
    requirements = criteria.get("requirements") or {}
    repeats = criteria.get("repeatableConfig") or {}
    minutes = requirements.get("minutesWatched")
    watched = progress.get("currentMinutesWatched")
    if (
        criteria.get("requirementType") != "WATCH"
        or not criteria.get("isRepeatable")
        or criteria.get("isAutoProgressable") is not False
        or not isinstance(minutes, (int, float))
        or minutes <= 0
        or (requirements.get("subs") or 0) > 0
        or (requirements.get("turboSubs") or 0) > 0
        or not isinstance(watched, (int, float))
        or not isinstance(repeats.get("repeatableTimes"), int)
        or progress.get("grantCount", 0) >= repeats["repeatableTimes"]
    ):
        return False
    eligible = progress.get("nextEligibleAt")
    if eligible:
        at = parse_twitch_datetime(eligible)
        if at is None or now < at:
            return False
    if watched < minutes:
        return True
    # Twitch can keep the previous window's 20/20 until watching starts again.
    # Resume from its expiry, not a locally invented midnight or 24-hour timer.
    expires = parse_twitch_datetime(
        (progress.get("currentWindow") or {}).get("expiresAt")
    )
    return expires is not None and now >= expires


class ProgressionDrops:
    """Pokemon selection and daily progress, authoritative state reloaded from Twitch."""

    def __init__(self, twitch):
        self.twitch = twitch
        self.probes = {}
        self.directory_checked = None
        self.last_status = None
        self.last_progress = {}
        self.campaign_ids = set()
        self.badge_titles = set()

    def manages(self, campaign):
        return campaign.id in self.campaign_ids or any(
            drop.badge_titles & self.badge_titles for drop in campaign.drops
        )

    def query(self, operation, variables, field):
        request = copy.deepcopy(operation)
        request["variables"] = variables
        response = self.twitch.post_gql_request(request)
        data = response.get("data") if isinstance(response, dict) else None
        errors = response.get("errors") if isinstance(response, dict) else None
        value = data.get(field) if isinstance(data, dict) else None
        expected = (
            list
            if field in ("trackSelections", "channelDropCampaignsProgress")
            else dict
        )
        if errors or not isinstance(value, expected):
            logger.warning(
                "Pokemon drops: %s returned no %s (errors: %s)",
                operation["operationName"],
                field,
                errors or "unexpected response",
            )
            return None
        return value

    def selections(self, tracks):
        result = self.query(
            GQLOperations.ProgressionTrackSelections,
            {"tracks": tracks},
            "trackSelections",
        )
        if not isinstance(result, list):
            return None
        return {
            entry["track"]: entry.get("lane")
            for entry in result
            if isinstance(entry, dict) and entry.get("track")
        }

    def select(self, track, lane):
        result = self.query(
            GQLOperations.SetProgressionTrackSelection,
            {"input": {"track": track, "lane": lane}},
            "setTrackSelection",
        )
        if not isinstance(result, dict):
            return False
        if "error" not in result or result["error"] is not None:
            logger.warning(
                "Pokemon drops: selection rejected (%s)",
                result.get("error", "missing error field"),
            )
            return False
        selected = self.selections([track])
        if selected is None or selected.get(track) != lane:
            logger.warning(
                "Pokemon drops: selection was not confirmed; not farming this track"
            )
            return False
        return True

    def sync(self, streamers):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        channels = {
            str(s.channel_id): s.username
            for s in streamers
            if s.is_online and s.settings.claim_drops and s.channel_id
        }
        # Keep an independent probe while all daily stages are paused and drop-only
        # channels have been taken offline by the normal scheduler.
        channels = {**self.probes, **channels}
        candidates = []
        for channel_id, login in list(channels.items())[:3]:
            candidates.extend(self.load(channel_id, login, now))
        if not candidates and (
            self.directory_checked is None
            or (now - self.directory_checked).total_seconds() >= 600
        ):
            self.directory_checked = now
            game = {
                "id": "509658",
                "name": "Just Chatting",
                "displayName": "Just Chatting",
            }
            for channel_id, login in self.twitch.get_drops_enabled_channels(
                game, limit=3
            ):
                if channel_id not in channels:
                    candidates.extend(self.load(channel_id, login, now))
        if not candidates:
            if self.last_status:
                logger.info(
                    "Pokemon drops: no confirmed daily stage available; releasing "
                    "drop priority and checking Twitch again on the next sync"
                )
                self.last_status = ()
            return []

        tracks = sorted({group["track"] for _, group, _, _ in candidates})
        selected = self.selections(tracks)
        if selected is None:
            return []
        result = []
        status = []
        for track in tracks:
            available = [c for c in candidates if c[1]["track"] == track]
            # Finish the equipped lane first, then other unlocked lanes.
            current = selected.get(track)
            available.sort(key=lambda c: c[1]["lane"] != current)
            campaign, group, channel, game = available[0]
            lane = group["lane"]
            if current != lane and not self.select(track, lane):
                continue
            group_id = group["id"]
            status.append((track, lane, group_id))
            progress = group["self"]
            progress_key = (
                progress.get("grantCount"),
                progress["currentMinutesWatched"],
                (progress.get("currentWindow") or {}).get("startedAt"),
            )
            if self.last_progress.get(group_id) != progress_key:
                logger.info(
                    "Pokemon drops: %s, %s/%s minutes (completed daily stages: %s/%s)",
                    group["name"],
                    progress["currentMinutesWatched"],
                    group["progressCriteria"]["requirements"]["minutesWatched"],
                    progress.get("grantCount"),
                    group["progressCriteria"]["repeatableConfig"]["repeatableTimes"],
                )
                self.last_progress[group_id] = progress_key
            for source, option, channel, game in available:
                if option["id"] != group_id:
                    continue
                adapted = self.adapt(source, option, channel, game)
                result.append((adapted, channel))
        state = tuple(status)
        if state != self.last_status:
            for campaign, _ in result[:1]:
                logger.info(
                    "Pokemon drops: farming %s (%s); daily completion will rotate "
                    "to the next unlocked Pokemon",
                    campaign.drops[0].benefit,
                    campaign.game_name(),
                )
            self.last_status = state
        return result

    def load(self, channel_id, login, now):
        live = self.query(
            GQLOperations.WithIsStreamLiveQuery, {"id": channel_id}, "user"
        )
        if not isinstance(live, dict):
            return []
        if not live.get("stream"):
            self.probes.pop(channel_id, None)
            return []
        campaigns = self.query(
            GQLOperations.DropChannelCampaignsProgress,
            {"channelID": channel_id},
            "channelDropCampaignsProgress",
        )
        if not isinstance(campaigns, list):
            return []
        user = self.query(
            GQLOperations.DropsChannelCategory, {"channelID": channel_id}, "user"
        )
        if not isinstance(user, dict):
            return []
        game = (user.get("broadcastSettings") or {}).get("game")
        result = []
        for campaign in campaigns:
            if not isinstance(campaign, dict):
                continue
            start = parse_twitch_datetime(campaign.get("startAt"))
            end = parse_twitch_datetime(campaign.get("endAt"))
            if start is None or end is None or not start <= now < end:
                continue
            for group in campaign.get("rewardGroups") or []:
                track = group.get("track")
                if (
                    not isinstance(track, str)
                    or not track.startswith("pokemon#")
                    or not group.get("lane")
                ):
                    continue
                self.campaign_ids.add(campaign["id"])
                self.badge_titles.update(
                    reward["name"].strip().lower()
                    for reward in group.get("rewards") or []
                    if reward.get("distributionType") == "BADGE" and reward.get("name")
                )
                if not isinstance(group.get("self"), dict):
                    continue
                criteria = group.get("progressCriteria") or {}
                allowed = criteria.get("channels")
                if allowed and str(channel_id) not in {str(c["id"]) for c in allowed}:
                    continue
                categories = criteria.get("categories") or []
                if not isinstance(game, dict) or game.get("id") not in {
                    c["id"] for c in categories
                }:
                    continue
                self.probes = {channel_id: login}
                progress = group["self"]
                if not daily_watchable(group, now):
                    if progress.get("status") == "IN_PROGRESS" and progress.get(
                        "currentMinutesWatched", 0
                    ) >= (
                        (criteria.get("requirements") or {}).get("minutesWatched")
                        or float("inf")
                    ):
                        logger.debug(
                            "Pokemon drops: %s daily stage complete (%s repeats); "
                            "window ends %s, next eligible %s",
                            group.get("name"),
                            progress.get("grantCount"),
                            (progress.get("currentWindow") or {}).get("expiresAt"),
                            progress.get("nextEligibleAt"),
                        )
                    continue
                result.append((campaign, group, (channel_id, login), game))
        return result

    @staticmethod
    def adapt(source, group, channel, game):
        progress = group["self"]
        required = group["progressCriteria"]["requirements"]["minutesWatched"]
        campaign = Campaign(
            {
                "id": PROGRESSION_PREFIX + group["id"] + ":" + channel[0],
                "name": source["name"],
                "game": game,
                "startAt": source["startAt"],
                "endAt": source["endAt"],
                "allow": {"channels": [{"id": channel[0]}]},
                "timeBasedDrops": [
                    {
                        "id": group["id"],
                        "name": group["name"],
                        "requiredMinutesWatched": required,
                        "startAt": source["startAt"],
                        "endAt": source["endAt"],
                        "benefitEdges": [
                            {"benefit": reward} for reward in group.get("rewards") or []
                        ],
                    }
                ],
            }
        )
        drop = campaign.drops[0]
        drop.update(
            {
                "hasPreconditionsMet": True,
                "currentMinutesWatched": progress["currentMinutesWatched"],
                "dropInstanceID": None,
                "isClaimed": False,
            }
        )
        drop.is_printable = False
        return campaign
