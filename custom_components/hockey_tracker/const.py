"""Constants for Hockey Tracker."""

DOMAIN = "hockey_tracker"
ATTRIBUTION = (
    "Data: HockeyTech/LeagueStat · NHL Stats API · "
    "Inspired by ha-teamtracker (github.com/vasqued2/ha-teamtracker)"
)

# HockeyTech API
HOCKEYTECH_BASE = "https://lscluster.hockeytech.com/feed/index.php"

# NHL Stats API
NHL_API_BASE = "https://api-web.nhle.com/v1"

# League identifiers — professional
LEAGUE_NHL = "NHL"
LEAGUE_PWHL = "PWHL"
LEAGUE_AHL = "AHL"
LEAGUE_ECHL = "ECHL"
# League identifiers — major junior (CHL umbrella + member leagues)
LEAGUE_CHL = "CHL"
LEAGUE_OHL = "OHL"
LEAGUE_WHL = "WHL"
LEAGUE_QMJHL = "QMJHL"
# League identifiers — junior / developmental
LEAGUE_USHL = "USHL"
LEAGUE_BCHL = "BCHL"
LEAGUE_OJHL = "OJHL"
LEAGUE_AJHL = "AJHL"
LEAGUE_SJHL = "SJHL"
LEAGUE_MJHL = "MJHL"
LEAGUE_MHL = "MHL"

# All HockeyTech leagues in display order.
# API keys sourced from ha-teamtracker (github.com/vasqued2/ha-teamtracker)
# and official league apps via lscluster.hockeytech.com.
HOCKEYTECH_LEAGUES: dict[str, dict] = {
    LEAGUE_PWHL: {
        "client_code": "pwhl",
        "default_api_key": "446521baf8c38984",
        "logo_url": "https://assets.leaguestat.com/pwhl/logos/{}.png",
    },
    LEAGUE_AHL: {
        "client_code": "ahl",
        "default_api_key": "ccb91f29d6744675",
        "logo_url": "https://assets.leaguestat.com/ahl/logos/{}.png",
    },
    LEAGUE_ECHL: {
        "client_code": "echl",
        "default_api_key": "2c2b89ea7345cae8",
        "logo_url": "https://assets.leaguestat.com/echl/logos/{}.png",
    },
    LEAGUE_CHL: {
        "client_code": "chl",
        "default_api_key": "f1aa699db3d81487",
        "logo_url": "https://assets.leaguestat.com/chl/logos/{}.png",
    },
    LEAGUE_OHL: {
        "client_code": "ohl",
        "default_api_key": "f1aa699db3d81487",
        "logo_url": "https://assets.leaguestat.com/ohl/logos/{}.png",
    },
    LEAGUE_WHL: {
        "client_code": "whl",
        "default_api_key": "f1aa699db3d81487",
        "logo_url": "https://assets.leaguestat.com/whl/logos/{}.png",
    },
    LEAGUE_QMJHL: {
        "client_code": "qmjhl",
        "default_api_key": "f1aa699db3d81487",
        "logo_url": "https://assets.leaguestat.com/qmjhl/logos/{}.png",
    },
    LEAGUE_USHL: {
        "client_code": "ushl",
        "default_api_key": "e828f89b243dc43f",
        "logo_url": "https://assets.leaguestat.com/ushl/logos/{}.png",
    },
    LEAGUE_BCHL: {
        "client_code": "bchl",
        "default_api_key": "ca4e9e599d4dae55",
        "logo_url": "https://assets.leaguestat.com/bchl/logos/{}.png",
    },
    LEAGUE_OJHL: {
        "client_code": "ojhl",
        "default_api_key": "77a0bd73d9d363d3",
        "logo_url": "https://assets.leaguestat.com/ojhl/logos/{}.png",
    },
    LEAGUE_AJHL: {
        "client_code": "ajhl",
        "default_api_key": "cbe60a1d91c44ade",
        "logo_url": "https://assets.leaguestat.com/ajhl/logos/{}.png",
    },
    LEAGUE_SJHL: {
        "client_code": "sjhl",
        "default_api_key": "2fb5c2e84bf3e4a8",
        "logo_url": "https://assets.leaguestat.com/sjhl/logos/{}.png",
    },
    LEAGUE_MJHL: {
        "client_code": "mjhl",
        "default_api_key": "f894c324fe5fd8f0",
        "logo_url": "https://assets.leaguestat.com/mjhl/logos/{}.png",
    },
    LEAGUE_MHL: {
        "client_code": "mhl",
        "default_api_key": "4a948e7faf5ee58d",
        "logo_url": "https://assets.leaguestat.com/mhl/logos/{}.png",
    },
}

CONF_API_KEY = "api_key"
CONF_TEAM_ID = "team_id"
CONF_TEAM_NAME = "team_name"
CONF_LEAGUE = "league"
CONF_ENTRY_TYPE = "entry_type"
CONF_FOLLOWED_TEAMS = "followed_teams"
CONF_FOLLOWED_TEAM_NAMES = "followed_team_names"

ENTRY_TYPE_TEAM = "team"
ENTRY_TYPE_PLAYOFF = "playoff"

# Days two series can be apart in start date and still be considered the same playoff round
ROUND_DATE_WINDOW = 5

# NHL playoff bracket URL — {year} is the season year (e.g. 2026)
NHL_PLAYOFF_BRACKET_URL = "{base}/playoff-bracket/{year}"

# Notification option config keys
CONF_NOTIFY_WIN_ENABLED = "notify_win_enabled"
CONF_NOTIFY_WIN_TARGETS = "notify_win_targets"
CONF_NOTIFY_PREGAME_ENABLED = "notify_pregame_enabled"
CONF_NOTIFY_PREGAME_TARGETS = "notify_pregame_targets"
CONF_NOTIFY_GOAL_ENABLED = "notify_goal_enabled"
CONF_NOTIFY_GOAL_TARGETS = "notify_goal_targets"

# Polling intervals (seconds)
SCAN_INTERVAL_LIVE = 30
SCAN_INTERVAL_GAME_ENDING = 15  # extra-fast poll at end of regulation or any OT period
SCAN_INTERVAL_PRE = 300
SCAN_INTERVAL_FINAL = 900
SCAN_INTERVAL_GAME_SOON = 900
SCAN_INTERVAL_GAME_TODAY = 1800
SCAN_INTERVAL_IDLE = 7200

# How long (seconds) to keep showing the final scoreboard after a game ends.
# Server-side; card relies entirely on this window.
FINAL_DISPLAY_SECONDS = 7200  # 2 hours

# Schedule cache TTL (seconds)
SCHEDULE_CACHE_TTL = 3600

# Max recent games stored as sensor attributes
RECENT_GAMES_MAX = 10

# Game states
GAME_STATE_PRE = "PRE"
GAME_STATE_LIVE = "LIVE"
GAME_STATE_FINAL = "FINAL"
GAME_STATE_NONE = "NO_GAME"

# NHL game states that map to our states
NHL_LIVE_STATES = {"LIVE", "CRIT"}
NHL_FINAL_STATES = {"FINAL", "OFF"}
NHL_PRE_STATES = {"FUT", "PRE"}

# Game summary URLs for each league
# HockeyTech: official printable game summary — fallback for leagues without a better page
HOCKEYTECH_GAME_REPORT_URL = (
    "https://lscluster.hockeytech.com/game_reports/official-game-report.php"
    "?client_code={client_code}&game_id={game_id}"
)
# AHL game center (user-facing page, uses same numeric game_id as HockeyTech)
AHL_GAME_URL = "https://www.theahl.com/stats/game-center/{game_id}"
# PWHL game page
PWHL_GAME_URL = "https://www.thepwhl.com/en/game/{game_id}"
# ECHL game page uses date + team-name slugs, not the numeric game_id;
# URL is constructed dynamically in coordinator._ht_game_url()
NHL_GAME_URL = "https://www.nhl.com/gamecenter/{game_id}"
