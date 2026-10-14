from enum import Enum, auto


class Priority(Enum):
    STREAK = auto()
    DROPS = auto()
    SUBSCRIBED = auto()
    ORDER = auto()
    POINTS_ASCENDING = auto()
    POINTS_DESCEDING = auto()


class FollowersOrder(Enum):
    ASC = auto()
    DESC = auto()

    def __str__(self):
        return self.name


# Empty object shared between class
class Settings(object):
    __slots__ = ["logger", "streamer_settings"]
    require_linked_account = False


class Events(Enum):
    STREAMER_ONLINE = auto()
    STREAMER_OFFLINE = auto()
    GAIN_FOR_RAID = auto()
    GAIN_FOR_CLAIM = auto()
    GAIN_FOR_WATCH = auto()
    GAIN_FOR_WATCH_STREAK = auto()
    BET_WIN = auto()
    BET_LOSE = auto()
    BET_REFUND = auto()
    BET_FILTERS = auto()
    BET_GENERAL = auto()
    BET_FAILED = auto()
    BET_START = auto()
    BONUS_CLAIM = auto()
    JOIN_RAID = auto()
    DROP_CLAIM = auto()
    DROP_STATUS = auto()
    DROP_SCAN = auto()
    CHAT_JOIN = auto()
    WATCH_TARGET = auto()

    def __str__(self):
        return self.name

    @classmethod
    def get(cls, key):
        return getattr(cls, str(key)) if str(key) in dir(cls) else None
