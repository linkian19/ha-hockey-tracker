"""Entry migration, force_refresh validation, diagnostics and options flow."""
from __future__ import annotations

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.hockey_tracker.const import (
    CONF_NOTIFY_GOAL_ENABLED,
    CONF_NOTIFY_GOAL_TARGETS,
    DOMAIN,
)
from custom_components.hockey_tracker.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_migrates_unique_id_to_include_league(
    hass: HomeAssistant, team_entry, mock_ht_fetch
) -> None:
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(team_entry, minor_version=1)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, "hockey_tracker_56",
        config_entry=team_entry, suggested_object_id="kansas_city_mavericks_game",
    )

    assert await hass.config_entries.async_setup(team_entry.entry_id)
    await hass.async_block_till_done()

    migrated = registry.async_get(old.entity_id)
    assert migrated.unique_id == "hockey_tracker_ECHL_56"
    assert migrated.entity_id == "sensor.kansas_city_mavericks_game"  # entity_id kept
    assert team_entry.minor_version == 2
    assert len(hass.states.async_all("sensor")) == 1


async def test_force_refresh_rejects_unknown_entity(
    hass: HomeAssistant, team_entry, mock_ht_fetch
) -> None:
    team_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(team_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "force_refresh", {"entity_id": "sensor.not_hockey"}, blocking=True
        )

    sensor = hass.states.async_all("sensor")[0].entity_id
    calls_before = mock_ht_fetch.call_count
    await hass.services.async_call(DOMAIN, "force_refresh", {"entity_id": sensor}, blocking=True)
    assert mock_ht_fetch.call_count == calls_before + 1


async def test_service_registered_without_entries(hass: HomeAssistant) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "force_refresh")


async def test_diagnostics_redacts_secrets(hass: HomeAssistant, team_entry, mock_ht_fetch) -> None:
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        team_entry,
        options={CONF_NOTIFY_GOAL_ENABLED: True, CONF_NOTIFY_GOAL_TARGETS: ["notify.mobile_app_phone"]},
    )
    assert await hass.config_entries.async_setup(team_entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, team_entry)
    assert diag["entry"]["data"]["api_key"] == "**REDACTED**"
    assert diag["entry"]["options"][CONF_NOTIFY_GOAL_TARGETS] == "**REDACTED**"
    assert diag["entry"]["options"][CONF_NOTIFY_GOAL_ENABLED] is True
    assert diag["coordinator"]["last_update_success"] is True


async def test_options_flow_saves(hass: HomeAssistant, team_entry, mock_ht_fetch) -> None:
    team_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(team_entry.entry_id)
    await hass.async_block_till_done()
    assert team_entry.state is ConfigEntryState.LOADED

    result = await hass.config_entries.options.async_init(team_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NOTIFY_GOAL_ENABLED: True, CONF_NOTIFY_GOAL_TARGETS: []}
    )
    assert team_entry.options[CONF_NOTIFY_GOAL_ENABLED] is True
