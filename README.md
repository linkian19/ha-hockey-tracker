# Hockey Tracker

A [Home Assistant](https://www.home-assistant.io/) custom integration that tracks live scores, game state, and upcoming schedule for any **ECHL**, **AHL**, or **NHL** team.

- **ECHL / AHL** — data from the HockeyTech / LeagueStat API (same backend as the official league apps)
- **NHL** — data from the public NHL Stats API (`api-web.nhle.com`), no API key required

> **Companion card:** Install [ha-hockey-tracker-card](https://github.com/linkian19/ha-hockey-tracker-card) to display this data on your Lovelace dashboard.

---

## Features

- Live in-game scores, period, and clock display
- Shots on goal (home and away) — ECHL/AHL during live games
- Game state sensor: `PRE`, `LIVE`, `FINAL`, `NO_GAME`
- Full-resolution team logos via CDN for all leagues
- Live game events feed: goals (scorer, assists, PP/SH/EN) and penalties — ECHL/AHL
- Next upcoming game details (opponent, date/time, venue, logos)
- Recent game results (up to 10)
- Adaptive polling — 30 s during live games, up to 2 h when no game is near

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

## Sensor

Each configured team creates one sensor entity. The state reflects the current game status:

| State | Meaning |
|-------|---------|
| `PRE` | Game scheduled but not yet started |
| `LIVE` | Game currently in progress |
| `FINAL` | Game has ended |
| `NO_GAME` | No game today — next game info is in attributes (absent during off-season) |

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
| `venue` | Arena name |

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

#### Game events (ECHL / AHL live games only)

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
| `player_number` | Jersey number |
| `assists` | (Goals only) List of assisting player names |
| `is_power_play` | (Goals only) `true` if power play goal |
| `is_short_handed` | (Goals only) `true` if shorthanded goal |
| `is_empty_net` | (Goals only) `true` if empty net goal |
| `description` | (Penalties only) Infraction description |
| `minutes` | (Penalties only) Penalty duration in minutes |

> **Note:** Game events require a second API call to the HockeyTech game summary endpoint and are only populated during live ECHL/AHL games. NHL and pre/post-game states return an empty list.

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

---

## Polling Intervals

The integration automatically adjusts how often it polls based on game state:

| Situation | Interval |
|-----------|----------|
| Game in progress (LIVE) | 30 seconds |
| Game today, not yet started (PRE) | 5 minutes |
| Game just ended (FINAL) | 15 minutes |
| Next game within 6 hours | 15 minutes |
| Next game within 24 hours | 30 minutes |
| Next game tomorrow or later | 2 hours |

---

## Example Automation

```yaml
automation:
  - alias: "Alert when team scores"
    trigger:
      - platform: state
        entity_id: sensor.kansas_city_mavericks_game
    condition:
      - condition: template
        value_template: >
          {% set a = trigger.to_state.attributes %}
          {% set b = trigger.from_state.attributes %}
          {{ trigger.to_state.state == 'LIVE' and
             (a.home_score | int > b.home_score | int and a.is_home) or
             (a.away_score | int > b.away_score | int and not a.is_home) }}
    action:
      - service: notify.mobile_app
        data:
          message: >
            {% set a = trigger.to_state.attributes %}
            {{ a.away_team }} {{ a.away_score }} – {{ a.home_score }} {{ a.home_team }}
```

---

## Notes by League

### NHL

- No API key required; data comes from the public `api-web.nhle.com/v1` API.
- Team logos are fetched from the NHL CDN at startup and cached for the session.
- During the off-season or after a team is eliminated from the playoffs, the sensor state is `NO_GAME` with no `next_game` attributes. The companion card will show the team logo and "No upcoming games scheduled" in this state.

### ECHL / AHL

- The integration uses the HockeyTech API key embedded in the official league apps. Known keys are pre-filled. If a league rotates its key, see the [Configuration](#configuration) section for how to find the updated key.
- Logo CDN URLs include a version suffix that is team-specific (e.g. `319_92.png`). The integration discovers these from live API responses and caches them — all logos load at full resolution.

---

## Issues & Contributing

Please open an issue at [github.com/linkian19/ha-hockey-tracker/issues](https://github.com/linkian19/ha-hockey-tracker/issues).
