"""Constants for ECHL Tracker."""

DOMAIN = "echl_tracker"
ATTRIBUTION = "Data provided by HockeyTech / LeagueStat"

# HockeyTech API
HOCKEYTECH_BASE = "https://lscluster.hockeytech.com/feed/index.php"
CLIENT_CODE = "echl"

CONF_API_KEY = "api_key"
CONF_TEAM_ID = "team_id"
CONF_TEAM_NAME = "team_name"

# Update intervals (seconds)
SCAN_INTERVAL_LIVE = 30
SCAN_INTERVAL_IDLE = 300

# Game states
GAME_STATE_PRE = "PRE"
GAME_STATE_LIVE = "LIVE"
GAME_STATE_FINAL = "FINAL"
GAME_STATE_NONE = "NO_GAME"

# KC Mavericks team ID (confirmed via HockeyTech scorebar API)
KC_MAVERICKS_TEAM_ID = "68"
