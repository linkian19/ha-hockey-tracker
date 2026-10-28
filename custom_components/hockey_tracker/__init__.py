"""Hockey Tracker — Home Assistant integration for hockey scores, schedules, and playoff brackets."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ENTRY_TYPE, CONF_LEAGUE, CONF_TEAM_ID, DOMAIN, ENTRY_TYPE_PLAYOFF
from .coordinator import HockeyCoordinator
from .playoff_coordinator import PlayoffCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HockeyConfigEntry = ConfigEntry[HockeyCoordinator | PlayoffCoordinator]

_SERVICE_FORCE_REFRESH = "force_refresh"
_SERVICE_SCHEMA = vol.Schema(
    {vol.Required("entity_id"): vol.All(cv.ensure_list, [cv.entity_id])}
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-wide services (once, independent of config entries)."""

    async def _handle_force_refresh(call: ServiceCall) -> None:
        entity_reg = er.async_get(hass)
        coordinators: list[HockeyCoordinator | PlayoffCoordinator] = []
        for entity_id in call.data["entity_id"]:
            reg_entry = entity_reg.async_get(entity_id)
            config_entry = (
                hass.config_entries.async_get_entry(reg_entry.config_entry_id)
                if reg_entry and reg_entry.config_entry_id
                else None
            )
            if (
                config_entry is None
                or config_entry.domain != DOMAIN
                or config_entry.state is not ConfigEntryState.LOADED
            ):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="not_hockey_tracker_entity",
                    translation_placeholders={"entity_id": entity_id},
                )
            coordinators.append(config_entry.runtime_data)

        for coordinator in coordinators:
            coordinator.clear_schedule_cache()
            coordinator.clear_final_window()
            await coordinator.async_refresh()

    hass.services.async_register(
        DOMAIN, _SERVICE_FORCE_REFRESH, _handle_force_refresh, schema=_SERVICE_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HockeyConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE, "team") == ENTRY_TYPE_PLAYOFF:
        coordinator: HockeyCoordinator | PlayoffCoordinator = PlayoffCoordinator(hass, entry)
    else:
        coordinator = HockeyCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HockeyConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: HockeyConfigEntry) -> bool:
    """Migrate older config entries.

    1.1 → 1.2: Team Tracker sensor unique_id gains the league
    (hockey_tracker_{team_id} → hockey_tracker_{league}_{team_id}). HockeyTech
    team IDs are per-league numbers, so the same ID can exist in two leagues and
    the old unique_id would collide. The entity_id and history are kept.
    """
    if entry.version > 1:
        # Downgrade from a future version — not supported
        return False

    if entry.minor_version < 2:
        if entry.data.get(CONF_ENTRY_TYPE, "team") != ENTRY_TYPE_PLAYOFF:
            old_id = f"hockey_tracker_{entry.data[CONF_TEAM_ID]}"
            new_id = f"hockey_tracker_{entry.data[CONF_LEAGUE]}_{entry.data[CONF_TEAM_ID]}"

            @callback
            def _migrate(reg_entry: er.RegistryEntry) -> dict | None:
                if reg_entry.unique_id == old_id:
                    return {"new_unique_id": new_id}
                return None

            await er.async_migrate_entries(hass, entry.entry_id, _migrate)
        hass.config_entries.async_update_entry(entry, minor_version=2)
        _LOGGER.debug("Migrated %s to version 1.2", entry.title)

    return True
