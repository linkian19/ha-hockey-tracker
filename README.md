# Hockey Tracker

A [Home Assistant](https://www.home-assistant.io/) custom integration that tracks live scores, game state, and upcoming schedule for any **ECHL**, **AHL**, or **NHL** team.

- **ECHL / AHL** — data from the HockeyTech / LeagueStat API (same backend as the official league apps)
- **NHL** — data from the public NHL Stats API (`api-web.nhle.com`), no API key required

> **Companion card:** Install [ha-hockey-tracker-card](https://github.com/linkian19/ha-hockey-tracker-card) to display this data on your Lovelace dashboard.

---

## Features

- Live in-game scores, period, and clock display
- Shots on goal (home and away) — all three leagues during live games
- Game state sensor: `PRE`, `LIVE`, `FINAL`, `NO_GAME`
- Full-resolution team logos via CDN for all leagues
- Live game events feed: goals (scorer, assists, PP/SH/EN) and penalties — all three leagues
- Next upcoming game details (opponent, date/time, venue, logos)
- Recent game results (up to 10) with links to official game summaries
- Adaptive polling — 15 s at end of regulation, 30 s during live games, up to 2 h when no game is near
- 2-hour post-game FINAL window — final score stays visible well after the buzzer
- `last_fetched` attribute — always know how fresh the data is
- Built-in notifications — win, pre-game, and goal alerts via any HA notify service
- `hockey_tracker.force_refresh` service — clears cache and hard-pulls fresh data on demand

---

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS → Integrations**
2. Click the three-dot menu → **Custom repositories**
3. Add `https://github.com/linkian19/ha-hockey-tracker` with category **Integration**
4. Search for **Hockey Tracker** and install
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/hockey_tracker/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Hockey Tracker**
3. Select your league: **ECHL**, **AHL**, or **NHL**
4. For ECHL/AHL: enter the API key (pre-filled with the known key)
5. Select your team from the dropdown

### API Keys

**NHL** — no API key required. The NHL Stats API is public.

**ECHL / AHL** — the integration uses the HockeyTech API key embedded in the official league apps. Known keys are pre-filled in the setup form. If a league rotates its key and the integration stops working, you can find the updated key by:

1. Opening the league website in Chrome and pressing **F12 → Network**
2. Filtering requests by `lscluster.hockeytech.com`
3. Any matching request URL will contain `key=XXXXXXXXXXXXXXXX`

---

## Notifications

After setup, configure alerts at **Settings → Devices & Services → Hockey Tracker → Configure**.

| Notification | When it fires |
|--------------|---------------|
| **Win** | Once when the tracked team's game ends in a win — includes final score |
| **Pre-game** | Once per game when puck drop is 35 minutes or fewer away |
| **Goal** | Each time the tracked team scores during live play — includes scorer, strength (PP/SH/EN), period/time, and current score |

Each type has an independent enable toggle and a multi-select list of your configured HA notify services (mobile apps, persistent notification, etc.). Select as many targets as you like per type.

Alerts are deduplicated by game ID within each HA session. Win alerts include a 12-hour recency guard so a stale FINAL game does not re-trigger after an integration reload. If the integration is reloaded during an active game, goal alerts will replay for goals already scored.

---

## Services

### `hockey_tracker.force_refresh`

Clears the schedule and logo cache, then immediately fetches fresh data from the API. Unlike `homeassistant.update_entity`, this bypasses the 1-hour schedule cache so next-game info and logos are always re-fetched.

```yaml
service: hockey_tracker.force_refresh
data:
  entity_id: sensor.coachella_valley_firebirds_game
```

The companion card's refresh button calls this service automatically.

---

## Sensor

Each configured team creates one sensor entity. The state reflects the current game status:

| State | Meaning |
|-------|---------|
| `PRE` | Game scheduled but not yet started |
| `LIVE` | Game currently in progress |
| `FINAL` | Game has ended (shown for up to 2 hours after the final horn) |
| `NO_GAME` | No active or recent game — next game info is in attributes |

### Attributes

#### Active game

| Attribute | Description |
|-----------|-------------|
| `game_id` | League game ID |
| `start_time` | ISO 8601 game start time (UTC) |
| `period` | Current period number |
| `clock` | Current game clock |
| `home_team` | Full home team name |
| `home_team_id` | Home team ID or abbreviation |
| `home_score` | Home team goals |
| `home_shots` | Home team shots on goal |
| `home_logo_url` | Home team logo URL |
| `away_team` | Full away team name |
| `away_team_id` | Away team ID or abbreviation |
| `away_score` | Away team goals |
| `away_shots` | Away team shots on goal |
| `away_logo_url` | Away team logo URL |
| `is_home` | `true` if your tracked team is the home team |
| `team_logo_url` | Your tracked team's logo URL |
| `team_name` | Full name of your tracked team |
| `venue` | Arena name |
| `last_fetched` | ISO 8601 UTC timestamp of the most recent successful data pull |

#### Next game (always present when available)

| Attribute | Description |
|-----------|-------------|
| `next_game_date` | ISO 8601 datetime of next game |
| `next_game_home` | `true` if next game is at home |
| `next_game_home_team` | Full home team name |
| `next_game_away_team` | Full away team name |
| `next_game_home_logo_url` | Home team logo for next game |
| `next_game_away_logo_url` | Away team logo for next game |
| `next_game_venue` | Arena for next game |

#### Game events (all leagues, live and final games)

| Attribute | Description |
|-----------|-------------|
| `game_events` | List of goals and penalties from the current game, most recent first |

Each entry in `game_events`:

| Field | Description |
|-------|-------------|
| `type` | `"goal"` or `"penalty"` |
| `period` | Period number |
| `time` | Clock time within the period |
| `team_abbrev` | Short team code |
| `is_tracked_team` | `true` if the event involves your tracked team |
| `player_name` | Player's full name |
| `player_number` | Jersey number (not available for NHL goals) |
| `assists` | (Goals only) List of assisting player names |
| `is_power_play` | (Goals only) `true` if power play goal |
| `is_short_handed` | (Goals only) `true` if shorthanded goal |
| `is_empty_net` | (Goals only) `true` if empty net goal |
| `description` | (Penalties only) Infraction description |
| `minutes` | (Penalties only) Penalty duration in minutes |

> Events are populated during live and final games via a second API call. ECHL/AHL uses the HockeyTech `gameSummary` endpoint; NHL uses `gamecenter/{id}/landing`. Pre-game and no-game states return an empty list.

#### Recent games

| Attribute | Description |
|-----------|-------------|
| `recent_games` | List of up to 10 completed games, newest first |

Each entry in `recent_games`:

| Field | Description |
|-------|-------------|
| `date` | ISO 8601 game date |
| `opponent` | Opponent full name |
| `opponent_logo_url` | Opponent logo URL |
| `team_score` | Your team's final score |
| `opponent_score` | Opponent's final score |
| `win` | `true` if your team won |
| `is_home` | `true` if your team was home |
| `venue` | Arena name |
| `game_url` | Link to the game summary page (NHL: nhl.com/gamecenter; AHL: theahl.com game center; ECHL: echl.com game page) |

---

## Polling Intervals

The integration automatically adjusts how often it polls based on game state:

| Situation | Interval |
|-----------|----------|
| Game in progress, clock at 0:00 in period ≥ 3 | 15 seconds |
| Game in progress (LIVE) | 30 seconds |
| Game today, not yet started (PRE) | 5 minutes |
| Game just ended (FINAL) | 15 minutes |
| Next game within 6 hours | 15 minutes |
| Next game within 24 hours | 30 minutes |
| Next game tomorrow or later | 2 hours |

The 15-second end-of-regulation interval ensures FINAL is detected as quickly as possible after the API updates.

---

## Notes by League

### NHL

- No API key required; data comes from the public `api-web.nhle.com/v1` API.
- Team logos are fetched from the NHL CDN at startup and cached for the session.
- During live and final games, a second call to `gamecenter/{id}/landing` provides shots on goal and the play-by-play events feed.
- During the off-season or after a team is eliminated from the playoffs, the sensor state is `NO_GAME` with no `next_game` attributes.

### ECHL / AHL

- The integration uses the HockeyTech API key embedded in the official league apps. Known keys are pre-filled. If a league rotates its key, see the [Configuration](#configuration) section for how to find the updated key.
- Logo CDN URLs include a version suffix that is team-specific. The integration discovers these from live API responses and caches them — all logos load at full resolution.

---

## Troubleshooting

**Notification settings show raw key names like `notify_win_enabled`**
Home Assistant loads custom integration translations at startup, not at reload time. After installing or updating via HACS, a full HA restart is required — go to **Settings → System → Restart**. A simple integration reload is not sufficient.

**Win notification fired for an old game after updating**
Updating via HACS reloads the integration and resets in-memory notification state. The win notification now includes a 12-hour recency guard to prevent this. Upgrade to v1.3.8 or later.

---

## Issues & Contributing

Please open an issue at [github.com/linkian19/ha-hockey-tracker/issues](https://github.com/linkian19/ha-hockey-tracker/issues).
