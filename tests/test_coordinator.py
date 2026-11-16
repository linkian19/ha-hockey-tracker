"""Score reconciliation, notification state machine and shared notification delivery."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.hockey_tracker.base_coordinator import take_new_goals
from custom_components.hockey_tracker.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_FOLLOWED_TEAM_NAMES,
    CONF_FOLLOWED_TEAMS,
    CONF_LEAGUE,
    CONF_NOTIFY_GOAL_ENABLED,
    CONF_NOTIFY_GOAL_TARGETS,
    CONF_NOTIFY_OPP_GOAL_ENABLED,
    CONF_NOTIFY_OPP_GOAL_TARGETS,
    CONF_NOTIFY_WIN_ENABLED,
    CONF_NOTIFY_WIN_TARGETS,
    DOMAIN,
    ENTRY_TYPE_PLAYOFF,
    GAME_STATE_FINAL,
    GAME_STATE_LIVE,
    GAME_STATE_NONE,
    PRE_WINDOW_HOURS,
)
from custom_components.hockey_tracker.coordinator import HockeyCoordinator, reconcile_scores

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
    """Fake notify service. reject_data=True gives it a schema without `data`, so HA
    rejects such calls at validation time — which async_call raises to the caller
    even with blocking=False (handler errors would not propagate)."""
    calls: list[ServiceCall] = []

    async def _handler(call: ServiceCall) -> None:
        calls.append(call)

    schema = (
        vol.Schema({vol.Required("title"): str, vol.Required("message"): str})
        if reject_data
        else None
    )
    hass.services.async_register("notify", "mobile_app_phone", _handler, schema=schema)
    return calls


def _live(goals: list[dict], home: int, away: int, state: str = GAME_STATE_LIVE) -> dict:
    return empty_game(
        game_state=state, game_id="g1", is_home=True,
        home_team="Kansas City Mavericks", away_team="Tulsa Oilers",
        home_score=home, away_score=away, period=2, game_events=goals,
        start_time=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
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


def _at(ours: bool, period: int, time: str) -> dict:
    return {**_goal(ours, 0), "period": period, "time": time}


def test_take_new_goals_matching() -> None:
    announced: list = []
    a, b = _at(True, 3, "0:59"), _at(True, 3, "6:24")
    assert take_new_goals([b, a], announced) == [b, a]  # newest first
    # Same goals again, one with a scorekeeper time correction → nothing new
    assert take_new_goals([_at(True, 3, "6:30"), a], announced) == []
    # Different period or team at the same time is a different goal
    assert len(take_new_goals([b, a, _at(True, 2, "6:24"), _at(False, 3, "6:24")], announced)) == 2
    # Two real goals seconds apart: the second one is still new
    assert take_new_goals([_at(True, 3, "6:28"), b, a], announced) == [_at(True, 3, "6:28")]
    # Identical keys (e.g. NHL shootout goals at 00:00) count separately
    so: list = []
    assert len(take_new_goals([_at(True, 5, "00:00")], so)) == 1
    assert len(take_new_goals([_at(True, 5, "00:00"), _at(True, 5, "00:00")], so)) == 1


async def test_goal_dropped_and_restored_not_reannounced(hass: HomeAssistant, team_entry) -> None:
    """Replays Nov 12 2026 (TUL @ KC, game 25631): the 2nd KC goal left the feed for
    ~90s and came back. Also covers a failed summary fetch returning no events."""
    calls = _register_notify(hass)
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(team_entry, options={
        CONF_NOTIFY_GOAL_ENABLED: True, CONF_NOTIFY_GOAL_TARGETS: ["notify.mobile_app_phone"],
        CONF_NOTIFY_OPP_GOAL_ENABLED: True, CONF_NOTIFY_OPP_GOAL_TARGETS: ["notify.mobile_app_phone"],
    })
    coord = HockeyCoordinator(hass, team_entry)
    g1, g2 = _at(True, 3, "0:59"), _at(True, 3, "6:24")

    for goals, home in (
        ([], 0),             # baseline
        ([g1], 1),           # goal 1 → alert
        ([g2, g1], 2),       # goal 2 → alert
        ([g1], 1),           # goal 2 pulled from the feed
        ([g2, g1], 2),       # goal 2 restored → no alert
        ([], 2),             # summary fetch failed → no events
        ([g2, g1], 2),       # events back → no alert
    ):
        await coord._maybe_notify(_live(goals, home, 0))
    await hass.async_block_till_done()
    assert [c.data["title"] for c in calls] == ["GOAL! Kansas City Mavericks scores!"] * 2

    # Goal 2 genuinely disallowed, then a later goal → still announced
    await coord._maybe_notify(_live([g1], 1, 0))
    await coord._maybe_notify(_live([_at(True, 3, "15:00"), g1], 2, 0))
    # Opponent goal dropping out and returning → announced once
    tul = _at(False, 3, "17:00")
    await coord._maybe_notify(_live([tul, _at(True, 3, "15:00"), g1], 2, 1))
    await coord._maybe_notify(_live([_at(True, 3, "15:00"), g1], 2, 0))
    await coord._maybe_notify(_live([tul, _at(True, 3, "15:00"), g1], 2, 1))
    await hass.async_block_till_done()
    titles = [c.data["title"] for c in calls[2:]]
    assert titles.count("GOAL! Kansas City Mavericks scores!") == 1
    assert titles.count("Tulsa Oilers scores") == 1


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


async def test_pre_only_within_window(hass: HomeAssistant, team_entry) -> None:
    """A scheduled game becomes the active PRE game only within PRE_WINDOW_HOURS."""
    team_entry.add_to_hass(hass)
    coord = HockeyCoordinator(hass, team_entry)
    now = datetime.now(UTC)

    def _ht(hours: float) -> dict:
        return {"GameStatus": "1", "GameDateISO8601": (now + timedelta(hours=hours)).isoformat()}

    def _nhl(hours: float) -> dict:
        return {"gameState": "FUT", "startTimeUTC": (now + timedelta(hours=hours)).isoformat()}

    assert coord._ht_find_active([_ht(PRE_WINDOW_HOURS + 1)]) is None
    assert coord._ht_find_active([_ht(PRE_WINDOW_HOURS - 0.5)]) is not None
    assert coord._nhl_find_active([_nhl(PRE_WINDOW_HOURS + 1)]) is None
    assert coord._nhl_find_active([_nhl(PRE_WINDOW_HOURS - 0.5)]) is not None
