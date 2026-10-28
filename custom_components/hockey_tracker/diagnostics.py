"""Diagnostics for Hockey Tracker (Settings → Devices & Services → ⋮ → Download diagnostics)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HockeyConfigEntry
from .const import CONF_API_KEY

TO_REDACT = {CONF_API_KEY}


# Notification targets name the user's devices (notify.mobile_app_<name>)
def _redact_options(options: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("**REDACTED**" if key.endswith("_targets") and value else value)
        for key, value in options.items()
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HockeyConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": {
            "title": entry.title,
            "version": f"{entry.version}.{entry.minor_version}",
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": _redact_options(dict(entry.options)),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "data": coordinator.data,
        },
    }
