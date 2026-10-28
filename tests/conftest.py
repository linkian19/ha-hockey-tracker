"""Shared fixtures for Hockey Tracker tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.hockey_tracker.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_LEAGUE,
    CONF_TEAM_ID,
    CONF_TEAM_NAME,
    DOMAIN,
    ENTRY_TYPE_TEAM,
    GAME_STATE_NONE,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    yield


@pytest.fixture
def team_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Kansas City Mavericks",
        unique_id="hockey_ECHL_56",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_TEAM,
            CONF_LEAGUE: "ECHL",
            CONF_TEAM_ID: "56",
            CONF_TEAM_NAME: "Kansas City Mavericks",
            CONF_API_KEY: "test-key",
            "team_logo_url": "",
        },
    )


def empty_game(**overrides) -> dict:
    data = {
        "game_state": GAME_STATE_NONE,
        "game_id": None,
        "start_time": None,
        "game_events": [],
        "recent_games": [],
        "next_game": None,
    }
    data.update(overrides)
    return data


@pytest.fixture
def mock_ht_fetch():
    """Patch the HockeyTech fetch so no network calls are made."""
    with patch(
        "custom_components.hockey_tracker.coordinator.HockeyCoordinator._fetch_hockeytech",
        return_value=empty_game(),
    ) as mock:
        yield mock
