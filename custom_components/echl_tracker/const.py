"""Constants for ECHL Tracker."""

DOMAIN = "echl_tracker"
ATTRIBUTION = "Data provided by HockeyTech / LeagueStat"

# HockeyTech API
HOCKEYTECH_BASE = "https://lscluster.hockeytech.com/feed/index.php"
CLIENT_CODE = "echl"

# Known public API key embedded in the ECHL app — may need updating if rotated
DEFAULT_API_KEY = "2c2b89ea7345cae8"

CONF_API_KEY = "api_key"
CONF_TEAM_ID = "team_id"
CONF_TEAM_NAME = "team_name"

# Polling intervals (seconds)
SCAN_INTERVAL_LIVE = 30          # Game in progress
SCAN_INTERVAL_PRE = 300          # Game today, not yet started
SCAN_INTERVAL_FINAL = 900        # Game just ended (catches late stat corrections)
SCAN_INTERVAL_GAME_SOON = 900    # Next game < 6 hours away
SCAN_INTERVAL_GAME_TODAY = 1800  # Next game < 24 hours away
SCAN_INTERVAL_IDLE = 7200        # Next game is tomorrow or later

# Schedule cache TTL (seconds) — avoids repeated schedule calls during live polling
SCHEDULE_CACHE_TTL = 3600

# Max recent games stored as sensor attributes
RECENT_GAMES_MAX = 10

# Game states
GAME_STATE_PRE = "PRE"
GAME_STATE_LIVE = "LIVE"
GAME_STATE_FINAL = "FINAL"
GAME_STATE_NONE = "NO_GAME"

# KC Mavericks team ID (confirmed via HockeyTech scorebar API)
KC_MAVERICKS_TEAM_ID = "68"
