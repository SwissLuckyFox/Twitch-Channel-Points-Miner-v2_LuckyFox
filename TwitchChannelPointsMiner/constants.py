# Twitch endpoints
URL = "https://www.twitch.tv"
IRC = "irc.chat.twitch.tv"
IRC_PORT = 6667
WEBSOCKET = "wss://pubsub-edge.twitch.tv/v1"
CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"
CLIENT_VERSION = "ef928475-9403-42f2-8a34-55784bd08e16"
DROP_ID = "c2542d6d-cd10-4532-919b-3d19f30a768b"

USER_AGENTS = {
    "Windows": {
        "CHROME": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.104 Safari/537.36",
        "FIREFOX": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:84.0) Gecko/20100101 Firefox/84.0",
    },
    "Linux": {
        "CHROME": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.96 Safari/537.36",
        "FIREFOX": "Mozilla/5.0 (X11; Linux x86_64; rv:85.0) Gecko/20100101 Firefox/85.0",
    },
    "Android": {
        "TV": "Mozilla/5.0 (Linux; Android 7.1; Smart Box C1) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
    },
}

BRANCH = "master"
GITHUB_url = (
    "https://raw.githubusercontent.com/SwissLuckyFox/Twitch-Channel-Points-Miner-v2_LuckyFox/"
    + BRANCH
)


class GQLOperations:
    url = "https://gql.twitch.tv/gql"
    WithIsStreamLiveQuery = {
        "operationName": "WithIsStreamLiveQuery",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "04e46329a6786ff3a81c01c50bfa5d725902507a0deb83b0edbf7abe7a3716ea",
            }
        },
    }
    VideoPlayerStreamInfoOverlayChannel = {
        "operationName": "VideoPlayerStreamInfoOverlayChannel",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
            }
        },
    }
    ClaimCommunityPoints = {
        "operationName": "ClaimCommunityPoints",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "46aaeebe02c99afdf4fc97c7c0cba964124bf6b0af229395f1f6d1feed05b3d0",
            }
        },
    }
    DropsPage_ClaimDropRewards = {
        "operationName": "DropsPage_ClaimDropRewards",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
            }
        },
    }
    ChannelPointsContext = {
        "operationName": "ChannelPointsContext",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "7fe050e3761eb2cf258d70ee1a21cbd76fa8cf3d7e7b12fc437e7029d446b5e3",
            }
        },
    }
    JoinRaid = {
        "operationName": "JoinRaid",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "c6a332a86d1087fbbb1a8623aa01bd1313d2386e7c63be60fdb2d1901f01a4ae",
            }
        },
    }
    ModViewChannelQuery = {
        "operationName": "ModViewChannelQuery",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "df5d55b6401389afb12d3017c9b2cf1237164220c8ef4ed754eae8188068a807",
            }
        },
    }
    Inventory = {
        "operationName": "Inventory",
        "variables": {"fetchRewardCampaigns": True},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "d86775d0ef16a63a33ad52e80eaff963b2d5b72fada7c991504a57496e1d8e4b",
            }
        },
    }
    MakePrediction = {
        "operationName": "MakePrediction",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "b44682ecc88358817009f20e69d75081b1e58825bb40aa53d5dbadcc17c881d8",
            }
        },
    }
    ViewerDropsDashboard = {
        "operationName": "ViewerDropsDashboard",
        "variables": {"fetchRewardCampaigns": True},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "5a4da2ab3d5b47c9f9ce864e727b2cb346af1e3ea8b897fe8f704a97ff017619",
            }
        },
    }
    DropCampaignDetails = {
        "operationName": "DropCampaignDetails",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "f6396f5ffdde867a8f6f6da18286e4baf02e5b98d14689a69b5af320a4c7b7b8",
            }
        },
    }
    DropsHighlightService_AvailableDrops = {
        "operationName": "DropsHighlightService_AvailableDrops",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "9a62a09bce5b53e26e64a671e530bc599cb6aab1e5ba3cbd5d85966d3940716f",
            }
        },
    }
    GetIDFromLogin = {
        "operationName": "GetIDFromLogin",
        "variables": {"login": None},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "94e82a7b1e3c21e186daa73ee2afc4b8f23bade1fbbff6fe8ac133f50a2f58ca",
            }
        },
    }
    # Live streams of a game; with the DROPS_ENABLED filter only channels that
    # currently earn drop progress are returned.
    DirectoryPage_Game = {
        "operationName": "DirectoryPage_Game",
        "variables": {
            "limit": 30,
            "slug": None,
            "imageWidth": 50,
            "includeCostreaming": False,
            "options": {
                "broadcasterLanguages": [],
                "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"},
                "sort": "VIEWER_COUNT",
                "systemFilters": ["DROPS_ENABLED"],
                "tags": [],
                "requestID": "JIRA-VXP-2397",
            },
            "sortTypeIsRecency": False,
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "86bcceb4e8b1a51256ff8eed8bd8aae4acacf80d737efe904f84f3aeadf8cafd",
            }
        },
    }
    # Newer hashes (as used by DevilXD/TwitchDropsMiner) tried when Twitch answers
    # PersistedQueryNotFound for one of the hashes above.
    FallbackHashes = {
        "Inventory": "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
        "ViewerDropsDashboard": "c16bb890cc8ce7647a96ee69cd313d423a378a3dedadf630a1017cde18975feb",
        "DropCampaignDetails": "039277bf98f3130929262cc7c6efd9c141ca3749cb6dca442fc8ead9a53f77c1",
        "DropsHighlightService_AvailableDrops": "782dad0f032942260171d2d80a654f88bdd0c5a9dddc392e9bc92218a0f42d20",
    }
    PersonalSections = (
        {
            "operationName": "PersonalSections",
            "variables": {
                "input": {
                    "sectionInputs": ["FOLLOWED_SECTION"],
                    "recommendationContext": {"platform": "web"},
                },
                "channelLogin": None,
                "withChannelUser": False,
                "creatorAnniversariesExperimentEnabled": False,
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "9fbdfb00156f754c26bde81eb47436dee146655c92682328457037da1a48ed39",
                }
            },
        },
    )
    ChannelFollows = {
        "operationName": "ChannelFollows",
        "variables": {"limit": 100, "order": "ASC"},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "eecf815273d3d949e5cf0085cc5084cd8a1b5b7b6f7990cf43cb0beadf546907",
            }
        },
    }
