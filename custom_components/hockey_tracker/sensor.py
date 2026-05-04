"""Sensor platform for Hockey Tracker."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_ENTRY_TYPE, CONF_TEAM_NAME, DOMAIN, ENTRY_TYPE_PLAYOFF
from .coordinator import HockeyCoordinator
from .playoff_sensor import PlayoffSensor

_GAME_ATTRS = (
    "game_id", "start_time", "period", "clock",
    "home_team", "home_team_id", "home_score", "home_shots", "home_logo_url",
    "away_team", "away_team_id", "away_score", "away_shots", "away_logo_url",
    "is_home", "team_logo_url", "venue", "last_fetched",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_PLAYOFF:
        async_add_entities([PlayoffSensor(coordinator, entry)])
    else:
        async_add_entities([HockeyGameSensor(coordinator, entry)])


class HockeyGameSensor(CoordinatorEntity[HockeyCoordinator], SensorEntity):
    """Single sensor entity — state is game_state, all data in attributes."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: HockeyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._team_name: str = entry.data[CONF_TEAM_NAME]
        self._attr_unique_id = f"hockey_tracker_{entry.data['team_id']}"
        self._attr_name = f"{self._team_name} Game"

    @property
    def native_value(self) -> str:
        return self.coordinator.data.get("game_state", "UNKNOWN")

    @property
    def icon(self) -> str:
        state = self.coordinator.data.get("game_state")
        if state == "LIVE":
            return "mdi:hockey-puck"
        if state == "PRE":
            return "mdi:calendar-clock"
        return "mdi:scoreboard"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        attrs: dict = {k: data.get(k) for k in _GAME_ATTRS}

        ng = data.get("next_game")
        if ng:
            attrs["next_game_date"] = ng.get("game_date")
            attrs["next_game_home"] = ng.get("is_home")
            attrs["next_game_home_team"] = ng.get("home_team")
            attrs["next_game_away_team"] = ng.get("away_team")
            attrs["next_game_home_logo_url"] = ng.get("home_logo_url")
            attrs["next_game_away_logo_url"] = ng.get("away_logo_url")
            attrs["next_game_venue"] = ng.get("venue")

        attrs["team_name"] = self._team_name
        attrs["recent_games"] = data.get("recent_games", [])
        attrs["game_events"] = data.get("game_events", [])
        return attrs
