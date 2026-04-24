"""Sensor platform for ECHL Tracker."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_TEAM_NAME, DOMAIN
from .coordinator import EchlCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EchlCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EchlGameSensor(coordinator, entry)])


class EchlGameSensor(CoordinatorEntity[EchlCoordinator], SensorEntity):
    """Single sensor entity exposing all game data as attributes."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: EchlCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._team_name: str = entry.data[CONF_TEAM_NAME]
        self._attr_unique_id = f"echl_tracker_{entry.data['team_id']}"
        self._attr_name = f"{self._team_name} Game"

    @property
    def native_value(self) -> str:
        return self.coordinator.data.get("game_state", "UNKNOWN")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        attrs: dict = {}

        for key in (
            "game_id", "start_time", "period", "clock",
            "home_team", "home_score", "home_shots",
            "away_team", "away_score", "away_shots",
            "is_home", "venue",
        ):
            attrs[key] = data.get(key)

        next_game = data.get("next_game")
        if next_game:
            attrs["next_game_date"] = next_game.get("game_date")
            attrs["next_game_venue"] = next_game.get("venue")
            attrs["next_game_home"] = next_game.get("is_home")
            if next_game.get("is_home"):
                attrs["next_game_opponent"] = (
                    f"{next_game.get('away_team_city', '')} {next_game.get('away_team_nickname', '')}".strip()
                )
            else:
                attrs["next_game_opponent"] = (
                    f"{next_game.get('home_team_city', '')} {next_game.get('home_team_nickname', '')}".strip()
                )

        return attrs

    @property
    def icon(self) -> str:
        state = self.coordinator.data.get("game_state")
        if state == "LIVE":
            return "mdi:hockey-puck"
        if state == "PRE":
            return "mdi:calendar-clock"
        return "mdi:scoreboard"
