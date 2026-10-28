"""Setup, unload, and update-failure behaviour."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.hockey_tracker.const import GAME_STATE_NONE


async def test_setup_and_unload(hass: HomeAssistant, team_entry, mock_ht_fetch) -> None:
    team_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(team_entry.entry_id)
    await hass.async_block_till_done()
    assert team_entry.state is ConfigEntryState.LOADED

    states = hass.states.async_all("sensor")
    assert len(states) == 1
    assert states[0].state == GAME_STATE_NONE

    assert await hass.config_entries.async_unload(team_entry.entry_id)
    assert team_entry.state is ConfigEntryState.NOT_LOADED


async def test_timeout_is_update_failed(hass: HomeAssistant, team_entry, mock_ht_fetch) -> None:
    """A request timeout must retry setup, not crash with an unexpected error."""
    mock_ht_fetch.side_effect = TimeoutError()
    team_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(team_entry.entry_id)
    await hass.async_block_till_done()
    assert team_entry.state is ConfigEntryState.SETUP_RETRY


async def test_bad_json_is_update_failed(hass: HomeAssistant, team_entry, mock_ht_fetch) -> None:
    mock_ht_fetch.side_effect = ValueError("Expecting value")
    team_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(team_entry.entry_id)
    await hass.async_block_till_done()
    assert team_entry.state is ConfigEntryState.SETUP_RETRY
