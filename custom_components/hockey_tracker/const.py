"""Constants for Hockey Tracker."""

DOMAIN = "hockey_tracker"
ATTRIBUTION = "Data: HockeyTech/LeagueStat (ECHL/AHL) · NHL Stats API (NHL)"

# HockeyTech API
HOCKEYTECH_BASE = "https://lscluster.hockeytech.com/feed/index.php"

# NHL Stats API
NHL_API_BASE = "https://api-web.nhle.com/v1"

# League identifiers
LEAGUE_ECHL = "ECHL"
LEAGUE_AHL = "AHL"
LEAGUE_NHL = "NHL"

HOCKEYTECH_LEAGUES: dict[str, dict] = {
    LEAGUE_ECHL: {
        "client_code": "echl",
        "default_api_key": "2c2b89ea7345cae8",
        "logo_url": "https://assets.leaguestat.com/echl/logos/{}.png",
    },
    LEAGUE_AHL: {
        "client_code": "ahl",
        "default_api_key": "ccb91f29d6744675",
        "logo_url": "https://assets.leaguestat.com/ahl/logos/{}.png",
    },
}

CONF_API_KEY = "api_key"
CONF_TEAM_ID = "team_id"
CONF_TEAM_NAME = "team_name"
CONF_LEAGUE = "league"

# Notification option config keys
CONF_NOTIFY_WIN_ENABLED = "notify_win_enabled"
CONF_NOTIFY_WIN_TARGETS = "notify_win_targets"
CONF_NOTIFY_PREGAME_ENABLED = "notify_pregame_enabled"
CONF_NOTIFY_PREGAME_TARGETS = "notify_pregame_targets"
CONF_NOTIFY_GOAL_ENABLED = "notify_goal_enabled"
CONF_NOTIFY_GOAL_TARGETS = "notify_goal_targets"

# Polling intervals (seconds)
SCAN_INTERVAL_LIVE = 30
SCAN_INTERVAL_GAME_ENDING = 15  # extra-fast poll when clock hits 0:00 in period ≥ 3
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
# HockeyTech: official printable game summary (goals, penalties, shots)
HOCKEYTECH_GAME_REPORT_URL = (
    "https://lscluster.hockeytech.com/game_reports/official-game-summary.php"
    "?client_code={client_code}&game_id={game_id}&lang_code=en"
)
NHL_GAME_URL = "https://www.nhl.com/gamecenter/{game_id}"
