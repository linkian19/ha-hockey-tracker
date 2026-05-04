"""Playoff sensor platform for Hockey Tracker."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_FOLLOWED_TEAM_NAMES, CONF_LEAGUE, DOMAIN
from .playoff_coordinator import PlayoffCoordinator

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
    coordinator: PlayoffCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PlayoffSensor(coordinator, entry)])


class PlayoffSensor(CoordinatorEntity[PlayoffCoordinator], SensorEntity):
    """Sensor tracking a league's playoff bracket and followed team games."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = False

    def __init__(self, coordinator: PlayoffCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        league = entry.data.get(CONF_LEAGUE, "")
        team_names: list[str] = entry.data.get(CONF_FOLLOWED_TEAM_NAMES, [])
        if team_names:
            teams_str = ", ".join(team_names[:2])
            if len(team_names) > 2:
                teams_str += f" +{len(team_names) - 2}"
            self._attr_name = f"{league} Playoffs ({teams_str})"
        else:
            self._attr_name = f"{league} Playoffs"
        self._attr_unique_id = f"hockey_playoff_{league}_{entry.entry_id[-8:]}"

    @property
    def state(self) -> str:
        return self.coordinator.data.get("game_state", "NO_GAME") if self.coordinator.data else "NO_GAME"

    @property
    def icon(self) -> str:
        state = self.state
        if state == "LIVE":
            return "mdi:hockey-puck"
        if state == "PRE":
            return "mdi:calendar-clock"
        return "mdi:tournament"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        attrs: dict = {}

        # Active game attributes (same keys as team sensor — card game view uses these)
        for key in _GAME_ATTRS:
            attrs[key] = data.get(key)

        attrs["game_events"] = data.get("game_events", [])
        attrs["next_game"] = data.get("next_game")

        # Playoff-specific attributes
        attrs["league"] = self._entry.data.get(CONF_LEAGUE, "")
        attrs["followed_teams"] = data.get("followed_teams", [])
        attrs["current_round"] = data.get("current_round", 0)
        attrs["bracket"] = data.get("bracket", [])

        return attrs
