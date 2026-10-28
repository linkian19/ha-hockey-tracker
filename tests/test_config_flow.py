"""Config flow tests."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hockey_tracker.config_flow import LEAGUE_OPTIONS
from custom_components.hockey_tracker.const import (
    CONF_ENTRY_TYPE,
    CONF_LEAGUE,
    DOMAIN,
    ENTRY_TYPE_TEAM,
    HOCKEYTECH_LEAGUES,
    LEAGUE_NHL,
)


def test_league_options_are_league_names() -> None:
    """Every dropdown option is a known league string (regression: v1.10.0 had a dict)."""
    assert all(isinstance(opt, str) for opt in LEAGUE_OPTIONS)
    assert set(LEAGUE_OPTIONS) == set(HOCKEYTECH_LEAGUES) | {LEAGUE_NHL}


async def test_league_step_serializes(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_TEAM}
    )
    assert result["step_id"] == "league"
    # The schema must only offer strings
    league_validator = result["data_schema"].schema[next(iter(result["data_schema"].schema))]
    assert all(isinstance(opt, str) for opt in league_validator.container)
    assert CONF_LEAGUE in [str(k) for k in result["data_schema"].schema]
