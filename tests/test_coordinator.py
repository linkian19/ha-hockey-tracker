"""Score reconciliation, notification state machine and shared notification delivery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.hockey_tracker.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_FOLLOWED_TEAM_NAMES,
    CONF_FOLLOWED_TEAMS,
    CONF_LEAGUE,
    CONF_NOTIFY_GOAL_ENABLED,
    CONF_NOTIFY_GOAL_TARGETS,
    CONF_NOTIFY_WIN_ENABLED,
    CONF_NOTIFY_WIN_TARGETS,
    DOMAIN,
    ENTRY_TYPE_PLAYOFF,
    GAME_STATE_FINAL,
    GAME_STATE_LIVE,
    GAME_STATE_NONE,
)
from custom_components.hockey_tracker.coordinator import HockeyCoordinator, reconcile_scores
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import empty_game


def _goal(ours: bool, minute: int) -> dict:
    return {
        "type": "goal", "period": 1, "time": f"{minute}:00", "is_tracked_team": ours,
        "player_name": "Player", "team_abbrev": "KC" if ours else "TUL",
    }


def test_reconcile_scores_takes_higher_count() -> None:
    data = {
        "is_home": True, "home_score": "1", "away_score": "0",
        "game_events": [_goal(True, 5), _goal(True, 9), _goal(False, 12)],
    }
    reconcile_scores(data)
    assert data["home_score"] == 2
    assert data["away_score"] == 1


def _register_notify(hass: HomeAssistant, reject_data: bool = False) -> list[ServiceCall]:
    calls: list[ServiceCall] = []

    async def _handler(call: ServiceCall) -> None:
        if reject_data and "data" in call.data:
            raise ValueError("data not supported")
        calls.append(call)

    hass.services.async_register("notify", "mobile_app_phone", _handler)
    return calls


def _live(goals: list[dict], home: int, away: int, state: str = GAME_STATE_LIVE) -> dict:
    return empty_game(
        game_state=state, game_id="g1", is_home=True,
        home_team="Kansas City Mavericks", away_team="Tulsa Oilers",
        home_score=home, away_score=away, period=2, game_events=goals,
        start_time=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )


async def test_goal_and_final_notifications(hass: HomeAssistant, team_entry) -> None:
    calls = _register_notify(hass)
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(team_entry, options={
        CONF_NOTIFY_GOAL_ENABLED: True, CONF_NOTIFY_GOAL_TARGETS: ["notify.mobile_app_phone"],
        CONF_NOTIFY_WIN_ENABLED: True, CONF_NOTIFY_WIN_TARGETS: ["notify.mobile_app_phone"],
    })
    coord = HockeyCoordinator(hass, team_entry)

    # First sighting mid-game (e.g. after an HA restart): baseline, no replay
    await coord._maybe_notify(_live([_goal(True, 5)], 1, 0))
    await hass.async_block_till_done()
    assert calls == []

    # A new goal → exactly one notification
    await coord._maybe_notify(_live([_goal(True, 15), _goal(True, 5)], 2, 0))
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["title"].startswith("GOAL!")

    # Final → one win notification with the score tag; not repeated
    final = _live([_goal(True, 15), _goal(True, 5)], 2, 0, state=GAME_STATE_FINAL)
    await coord._maybe_notify(final)
    await coord._maybe_notify(final)
    await hass.async_block_till_done()
    assert len(calls) == 2
    assert calls[1].data["title"] == "Kansas City Mavericks Win!"
    assert calls[1].data["data"]["tag"].endswith("_g1_score")


async def test_notification_retries_without_data(hass: HomeAssistant, team_entry) -> None:
    """Services that reject a `data` payload get the message without it."""
    calls = _register_notify(hass, reject_data=True)
    team_entry.add_to_hass(hass)
    coord = HockeyCoordinator(hass, team_entry)
    await coord._send_notifications(["notify.mobile_app_phone"], "T", "M", data={"tag": "x"})
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert "data" not in calls[0].data


async def test_playoff_entry_setup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="ECHL Playoffs (Kansas City Mavericks)", version=1, minor_version=2,
        unique_id="hockey_playoff_ECHL_56",
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
    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.async_all("sensor")[0]
    assert state.state == GAME_STATE_NONE
    assert state.attributes["league"] == "ECHL"
