# ECHL Tracker

A [Home Assistant](https://www.home-assistant.io/) custom integration that tracks live scores, game state, and upcoming schedule for any [ECHL](https://www.echl.com/) team.

Data is sourced from the HockeyTech / LeagueStat API — the same backend that powers the official ECHL website and mobile app.

> **Companion card:** Install [ha-echl-tracker-card](https://github.com/linkian19/ha-echl-tracker-card) to display this data on your dashboard.

---

## Features

- Live in-game scores and period/clock display
- Shots on goal (home and away)
- Game state: `PRE`, `LIVE`, `FINAL`, `NO_GAME`
- Next upcoming game details when no game is active
- Configurable for any ECHL team via UI
- Adaptive polling — updates every 30 seconds during live games, every 5 minutes otherwise

---

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS → Integrations**
2. Click the three-dot menu → **Custom repositories**
3. Add `https://github.com/linkian19/ha-echl-tracker` with category **Integration**
4. Search for "ECHL Tracker" and install
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/echl_tracker/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **ECHL Tracker**
3. Enter the API key (pre-filled with the known key — see below)
4. Select your team from the dropdown

### API Key

The integration uses the HockeyTech API key embedded in the official ECHL app. The current known key is pre-filled in the setup form. If the ECHL rotates this key and the integration stops working, you can find the updated key by:

1. Opening `https://lscluster.hockeytech.com/statview/mobile/echl/` in Chrome
2. Pressing F12 → **Network** tab → filter by `feed`
3. Reloading the page — any request URL will contain `key=XXXXXXXXXXXXXXXX`

---

## Sensor

Each configured team creates one sensor entity:

| Entity | Example |
|--------|---------|
| `sensor.kansas_city_mavericks_game` | State: `LIVE` |

### State Values

| State | Meaning |
|-------|---------|
| `PRE` | Game is scheduled but not yet started |
| `LIVE` | Game is currently in progress |
| `FINAL` | Game has ended |
| `NO_GAME` | No game today — next game info is in attributes |

### Attributes

| Attribute | Description |
|-----------|-------------|
| `game_id` | HockeyTech game ID |
| `start_time` | ISO 8601 game start time |
| `period` | Current period number |
| `clock` | Current game clock |
| `home_team` | Full home team name |
| `home_score` | Home team goals |
| `home_shots` | Home team shots on goal |
| `away_team` | Full away team name |
| `away_score` | Away team goals |
| `away_shots` | Away team shots on goal |
| `is_home` | `true` if your team is the home team |
| `venue` | Arena name |
| `next_game_date` | ISO 8601 datetime of next game (when `NO_GAME`) |
| `next_game_opponent` | Opponent name for next game |
| `next_game_home` | `true` if next game is at home |
| `next_game_venue` | Arena for next game |

---

## Example Automation

```yaml
automation:
  - alias: "Alert when Mavericks score"
    trigger:
      - platform: state
        entity_id: sensor.kansas_city_mavericks_game
    condition:
      - condition: template
        value_template: >
          {{ trigger.to_state.attributes.home_score | int >
             trigger.from_state.attributes.home_score | int
             and trigger.to_state.attributes.is_home }}
    action:
      - service: notify.mobile_app
        data:
          message: "Mavericks score! {{ states.sensor.kansas_city_mavericks_game.attributes.home_score }} - {{ states.sensor.kansas_city_mavericks_game.attributes.away_score }}"
```

---

## Issues & Contributing

Please open an issue at [github.com/linkian19/ha-echl-tracker/issues](https://github.com/linkian19/ha-echl-tracker/issues).
