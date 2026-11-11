"""Game-day reminder: same-day detection, dedupe, timer arming and options flow."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.hockey_tracker.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_FOLLOWED_TEAM_NAMES,
    CONF_FOLLOWED_TEAMS,
    CONF_LEAGUE,
    CONF_NOTIFY_GAMEDAY_ENABLED,
    CONF_NOTIFY_GAMEDAY_TARGETS,
    CONF_NOTIFY_GAMEDAY_TIME,
    CONF_TEAM_ID,
    CONF_TEAM_NAME,
    DOMAIN,
    ENTRY_TYPE_PLAYOFF,
    ENTRY_TYPE_TEAM,
)
from custom_components.hockey_tracker.coordinator import HockeyCoordinator
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .conftest import empty_game
from .test_coordinator import _register_notify

TARGET = "notify.mobile_app_phone"
GAMEDAY_OPTS = {
    CONF_NOTIFY_GAMEDAY_ENABLED: True,
    CONF_NOTIFY_GAMEDAY_TARGETS: [TARGET],
    CONF_NOTIFY_GAMEDAY_TIME: "09:00:00",
}


def _ht_game(iso: str, home: bool = True) -> dict:
    kc = {"id": "56", "city": "Kansas City", "nickname": "Mavericks"}
    tul = {"id": "60", "city": "Tulsa", "nickname": "Oilers"}
    h, v = (kc, tul) if home else (tul, kc)
    return {
        "GameDateISO8601": iso,
        "home_team": h["id"], "home_team_city": h["city"], "home_team_nickname": h["nickname"],
        "visiting_team": v["id"], "visiting_team_city": v["city"],
        "visiting_team_nickname": v["nickname"],
        "venue_name": "Cable Dahmer Arena" if home else "BOK Center",
    }


@pytest.fixture
async def chicago(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("America/Chicago")


def _local(iso: str) -> datetime:
    return dt_util.as_local(datetime.fromisoformat(iso))


async def test_sends_on_game_day_only_once(
    hass: HomeAssistant, team_entry, chicago, freezer
) -> None:
    calls = _register_notify(hass)
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(team_entry, options=GAMEDAY_OPTS)
    coord = HockeyCoordinator(hass, team_entry)
    schedule = [
        _ht_game("2026-11-10T19:05:00-06:00"),  # yesterday
        _ht_game("2026-11-11T19:05:00-06:00"),  # tonight
        _ht_game("2026-11-12T19:05:00-06:00", home=False),  # tomorrow
    ]
    now = _local("2026-11-11T09:00:00-06:00")
    freezer.move_to(now)
    with patch.object(HockeyCoordinator, "_fetch_ht_schedule", return_value=schedule):
        await coord._async_gameday_check(now)
        await coord._async_gameday_check(now)  # same day — not repeated
        await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["title"] == "Hockey: Game Day"
    assert calls[0].data["message"] == (
        "Kansas City Mavericks vs Tulsa Oilers at 7:05 PM (Cable Dahmer Arena)"
    )


async def test_no_game_today_sends_nothing(
    hass: HomeAssistant, team_entry, chicago, freezer
) -> None:
    calls = _register_notify(hass)
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(team_entry, options=GAMEDAY_OPTS)
    coord = HockeyCoordinator(hass, team_entry)
    # 01:05 UTC on the 11th is still the evening of the 10th in Chicago,
    # and an unparseable date must not count (or overflow).
    schedule = [_ht_game("2026-11-11T01:05:00+00:00"), _ht_game("")]
    now = _local("2026-11-11T09:00:00-06:00")
    freezer.move_to(now)
    with patch.object(HockeyCoordinator, "_fetch_ht_schedule", return_value=schedule):
        await coord._async_gameday_check(now)
        await hass.async_block_till_done()
    assert calls == []


async def test_nhl_away_game(hass: HomeAssistant, chicago, freezer) -> None:
    calls = _register_notify(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Dallas Stars", unique_id="hockey_NHL_DAL",
        version=1, minor_version=2,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_TEAM, CONF_LEAGUE: "NHL",
            CONF_TEAM_ID: "DAL", CONF_TEAM_NAME: "Dallas Stars", "team_logo_url": "",
        },
        options=GAMEDAY_OPTS,
    )
    entry.add_to_hass(hass)
    coord = HockeyCoordinator(hass, entry)
    schedule = [{
        "startTimeUTC": "2026-11-12T00:00:00Z",
        "homeTeam": {"abbrev": "STL", "placeName": {"default": "St. Louis"},
                     "commonName": {"default": "Blues"}},
        "awayTeam": {"abbrev": "DAL", "placeName": {"default": "Dallas"},
                     "commonName": {"default": "Stars"}},
        "venue": {"default": "Enterprise Center"},
    }]
    now = _local("2026-11-11T09:00:00-06:00")
    freezer.move_to(now)
    with (
        patch.object(HockeyCoordinator, "_fetch_nhl_schedule", return_value=schedule),
        patch.object(HockeyCoordinator, "_refresh_nhl_logo_cache"),
    ):
        await coord._async_gameday_check(now)
        await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["message"] == (
        "Dallas Stars @ St. Louis Blues at 6:00 PM (Enterprise Center)"
    )


async def test_timer_fires_at_configured_time_and_follows_options(
    hass: HomeAssistant, team_entry, mock_ht_fetch, chicago, freezer
) -> None:
    calls = _register_notify(hass)
    freezer.move_to(_local("2026-11-11T08:00:00-06:00"))
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(team_entry, options=GAMEDAY_OPTS)
    schedule = [_ht_game("2026-11-11T19:05:00-06:00"), _ht_game("2026-11-12T19:05:00-06:00")]

    with patch.object(HockeyCoordinator, "_fetch_ht_schedule", return_value=schedule):
        assert await hass.config_entries.async_setup(team_entry.entry_id)
        await hass.async_block_till_done()

        for stamp in ("2026-11-11T08:59:59-06:00", "2026-11-11T09:00:00-06:00"):
            freezer.move_to(_local(stamp))
            async_fire_time_changed(hass, _local(stamp))
            await hass.async_block_till_done()
        assert len(calls) == 1

        # Moving the time to 10:00 re-arms the timer without a reload
        hass.config_entries.async_update_entry(
            team_entry, options={**GAMEDAY_OPTS, CONF_NOTIFY_GAMEDAY_TIME: "10:00:00"}
        )
        await hass.async_block_till_done()
        # 10:00 on the 11th: already sent today. 09:00 on the 12th: old timer is gone.
        for stamp in ("2026-11-11T10:00:00-06:00", "2026-11-12T09:00:00-06:00"):
            freezer.move_to(_local(stamp))
            async_fire_time_changed(hass, _local(stamp))
            await hass.async_block_till_done()
            assert len(calls) == 1
        freezer.move_to(_local("2026-11-12T10:00:00-06:00"))
        async_fire_time_changed(hass, _local("2026-11-12T10:00:00-06:00"))
        await hass.async_block_till_done()
        assert len(calls) == 2

        # Disabling cancels the timer
        hass.config_entries.async_update_entry(
            team_entry, options={**GAMEDAY_OPTS, CONF_NOTIFY_GAMEDAY_ENABLED: False}
        )
        await hass.async_block_till_done()
        assert team_entry.runtime_data._gameday_unsub is None

    assert await hass.config_entries.async_unload(team_entry.entry_id)


async def test_options_flow_fields(hass: HomeAssistant, team_entry, mock_ht_fetch) -> None:
    team_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(team_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(team_entry.entry_id)
    keys = [str(k) for k in result["data_schema"].schema]
    assert keys[:3] == [
        CONF_NOTIFY_GAMEDAY_ENABLED, CONF_NOTIFY_GAMEDAY_TARGETS, CONF_NOTIFY_GAMEDAY_TIME,
    ]
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_NOTIFY_GAMEDAY_ENABLED: True, CONF_NOTIFY_GAMEDAY_TIME: "08:30:00"},
    )
    assert team_entry.options[CONF_NOTIFY_GAMEDAY_TIME] == "08:30:00"
    assert team_entry.runtime_data._gameday_unsub is not None


async def test_playoff_options_have_no_gameday(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="ECHL Playoffs", version=1, minor_version=2,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_PLAYOFF, CONF_LEAGUE: "ECHL", CONF_API_KEY: "k",
            CONF_FOLLOWED_TEAMS: ["56"], CONF_FOLLOWED_TEAM_NAMES: ["Kansas City Mavericks"],
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.hockey_tracker.playoff_coordinator.PlayoffCoordinator._fetch_ht_playoffs",
        return_value=empty_game(bracket=[], current_round=0, followed_teams=[]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = [str(k) for k in result["data_schema"].schema]
    assert CONF_NOTIFY_GAMEDAY_ENABLED not in keys
