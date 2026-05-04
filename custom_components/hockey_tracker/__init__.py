"""Hockey Tracker — Home Assistant integration for hockey scores, schedules, and playoff brackets."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_PLAYOFF
from .coordinator import HockeyCoordinator
from .playoff_coordinator import PlayoffCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

_SERVICE_FORCE_REFRESH = "force_refresh"
_SERVICE_SCHEMA = vol.Schema(
    {vol.Required("entity_id"): vol.All(cv.ensure_list, [cv.entity_id])}
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_type = entry.data.get(CONF_ENTRY_TYPE, "team")

    if entry_type == ENTRY_TYPE_PLAYOFF:
        coordinator: HockeyCoordinator | PlayoffCoordinator = PlayoffCoordinator(hass, entry)
    else:
        coordinator = HockeyCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, _SERVICE_FORCE_REFRESH):
        async def _handle_force_refresh(call: ServiceCall) -> None:
            entity_reg = er.async_get(hass)
            for entity_id in call.data["entity_id"]:
                entry_obj = entity_reg.async_get(entity_id)
                if entry_obj and entry_obj.config_entry_id:
                    coord = hass.data.get(DOMAIN, {}).get(entry_obj.config_entry_id)
                    if coord:
                        coord.clear_schedule_cache()
                        await coord.async_refresh()

        hass.services.async_register(
            DOMAIN, _SERVICE_FORCE_REFRESH, _handle_force_refresh, schema=_SERVICE_SCHEMA
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
