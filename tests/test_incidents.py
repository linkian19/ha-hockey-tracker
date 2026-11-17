"""Disallowed-goal and major-incident detection (v1.13.0)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hockey_tracker.const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_FOLLOWED_TEAM_NAMES,
    CONF_FOLLOWED_TEAMS,
    CONF_LEAGUE,
    CONF_NOTIFY_DISALLOWED_ENABLED,
    CONF_NOTIFY_DISALLOWED_TARGETS,
    CONF_NOTIFY_INCIDENT_ENABLED,
    CONF_NOTIFY_INCIDENT_TARGETS,
    DOMAIN,
    ENTRY_TYPE_PLAYOFF,
    GAME_STATE_FINAL,
    GAME_STATE_LIVE,
)
from custom_components.hockey_tracker.coordinator import HockeyCoordinator
from custom_components.hockey_tracker.incidents import (
    DisallowedGoalTracker,
    classify_ht_penalty,
    classify_nhl_penalty,
    collect_incidents,
    disallowed_reason_text,
    format_incidents,
    ht_penalty_shots,
    nhl_reviews,
)
from custom_components.hockey_tracker.playoff_coordinator import PlayoffCoordinator

from .conftest import empty_game

TARGET = ["notify.mobile_app_phone"]


def _register_notify(hass: HomeAssistant) -> list[ServiceCall]:
    calls: list[ServiceCall] = []

    async def _handler(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("notify", "mobile_app_phone", _handler)
    return calls


def _goal(team: str, period: int, time: str, ours: bool | None = None) -> dict:
    return {
        "type": "goal", "period": period, "time": time, "team_abbrev": team,
        "is_tracked_team": team == "KC" if ours is None else ours, "player_name": f"{team} scorer",
    }


def _pen(team: str, player: str, period: int, time: str, desc: str, minutes: int) -> dict:
    return {
        "type": "penalty", "period": period, "time": time, "team_abbrev": team,
        "is_tracked_team": team == "KC", "player_name": player, "description": desc,
        "minutes": minutes, "incident": classify_ht_penalty(desc, minutes),
    }


# ----------------------------------------------------------------------
# Classification (descriptions taken from real ECHL / NHL feeds)
# ----------------------------------------------------------------------


def test_classify_ht_penalty() -> None:
    cases = {
        ("Fighting - Major", 5): "fight",
        ("Game Misconduct - Second Fight", 10): "ejection",
        ("Game Misconduct - Fighting (continuing altercation)", 10): "ejection",
        ("Game misconduct - Cross-checking", 10): "ejection",
        ("Match - Roughing", 10): "ejection",
        ("Cross-Checking Major", 5): "major",
        ("Elbowing - major", "5.00"): "major",
        ("Spearing major", 5): "major",
        ("Misconduct", 10): None,
        ("Roughing - minor", 2): None,
        ("Instigating - minor", 2): None,
        ("Bench minor - Too many men", 2): None,
        ("High-sticking - double minor", 4): None,
    }
    for (desc, minutes), expected in cases.items():
        assert classify_ht_penalty(desc, minutes) == expected, desc


def test_classify_nhl_penalty() -> None:
    assert classify_nhl_penalty("MAJ", "fighting") == "fight"
    assert classify_nhl_penalty("MAJ", "boarding") == "major"
    assert classify_nhl_penalty("GAM", "game-misconduct") == "ejection"
    assert classify_nhl_penalty("MAT", "match-penalty") == "ejection"
    assert classify_nhl_penalty("PS", "ps-hooking-on-breakaway") == "penalty_shot"
    assert classify_nhl_penalty("MIS", "instigator-misconduct") is None
    assert classify_nhl_penalty("MIN", "tripping") is None
    assert classify_nhl_penalty("BEN", "delaying-game-unsuccessful-challenge") is None


def test_incident_formatting() -> None:
    """Fighters at the same time share a line; a major that comes with an ejection
    is folded into the ejection."""
    events = [
        _pen("ALN", "Sam Lipkin", 1, "19:48", "Fighting - Major", 5),
        _pen("TUL", "Zach Murray", 1, "19:48", "Fighting - Major", 5),
        _pen("TUL", "Zach Murray", 1, "19:48", "Game Misconduct - Second Fight", 10),
        _pen("KC", "Jake Smith", 2, "4:10", "Spearing major", 5),
        _pen("KC", "Jake Smith", 2, "4:10", "Match - Spearing", 10),
        _pen("KC", "Joe Minor", 2, "6:00", "Tripping - minor", 2),
    ]
    summary = {"penaltyShots": {"homeTeam": [], "visitingTeam": [{
        "shooter": {"firstName": "Cam", "lastName": "Hausinger"},
        "shooter_team": {"id": 106, "abbreviation": "TAH"},
        "period": {"id": "3"}, "time": "12:46", "isGoal": False,
    }]}}
    items = collect_incidents({
        "game_events": events, "_penalty_shots": ht_penalty_shots(summary, lambda t: t == "56"),
    })
    assert sorted(i["kind"] for i in items) == ["ejection", "ejection", "fight", "fight", "penalty_shot"]
    title, msg = format_incidents(items)
    assert title == "Hockey: Ejection & fight & penalty shot"
    assert msg.splitlines() == [
        "Fight: Sam Lipkin (ALN) vs Zach Murray (TUL) — P1 19:48",
        "Ejected: Zach Murray (TUL) — Game Misconduct - Second Fight — P1 19:48",
        "Ejected: Jake Smith (KC) — Match - Spearing — P2 4:10",
        "Penalty shot: Cam Hausinger (TAH) — no goal — P3 12:46",
    ]


def test_nhl_reviews() -> None:
    def stop(period: int, time: str, reason: str, secondary: str | None = None) -> dict:
        details = {"reason": reason}
        if secondary:
            details["secondaryReason"] = secondary
        return {"typeDescKey": "stoppage", "periodDescriptor": {"number": period},
                "timeInPeriod": time, "details": details}

    pbp = {"plays": [
        stop(1, "16:28", "chlg-hm-off-side", "chlg-hm-off-side"),
        stop(3, "11:42", "referee-or-linesman", "chlg-vis-goal-interference"),
        stop(2, "4:37", "referee-or-linesman", "video-review"),
        stop(2, "5:00", "icing"),
        stop(3, "17:03", "referee-or-linesman", "player-injury"),
        {"typeDescKey": "goal", "periodDescriptor": {"number": 1}, "timeInPeriod": "16:28"},
    ]}
    reviews = nhl_reviews(pbp, "DAL", "STL")
    assert [(r["period"], r["reason"], r["challenged_by"]) for r in reviews] == [
        (1, "offside", "DAL"),
        (3, "goaltender interference", "STL"),
        (2, "video review", None),
    ]
    assert disallowed_reason_text({"reason": "offside", "challenged_by": "DAL"}) == "Overturned on DAL challenge (offside)"
    assert disallowed_reason_text({"reason": "video review", "challenged_by": None}) == "Overturned on video review"
    assert disallowed_reason_text({}) == "Taken off the board"


# ----------------------------------------------------------------------
# DisallowedGoalTracker
# ----------------------------------------------------------------------


def _side(goal: dict) -> str:
    return "home" if goal["team_abbrev"] == "KC" else "away"


def _run(tracker: DisallowedGoalTracker, polls: list[tuple[list[dict], int, int]], reviews=None) -> list[list[dict]]:
    return [
        tracker.update("g1", True, goals, {"home": home, "away": away}, _side, reviews)
        for goals, home, away in polls
    ]


def test_tracker_flags_goal_when_both_feeds_agree() -> None:
    g1, g2 = _goal("KC", 1, "5:00"), _goal("KC", 2, "3:10")
    t = DisallowedGoalTracker()
    out = _run(t, [
        ([g2, g1], 2, 0),  # baseline
        ([g1], 2, 0),      # summary drops the goal first: wait for the scoreboard
        ([g1], 2, 0),
        ([g1], 1, 0),      # scoreboard follows → disallowed
        ([g1], 1, 0),      # not repeated
    ])
    assert [len(o) for o in out] == [0, 0, 0, 1, 0]
    assert out[3][0]["time"] == "3:10" and out[3][0]["type"] == "goal_disallowed"
    assert [g["time"] for g in t.goals] == ["3:10"]

    # Scoreboard drops first, summary a poll later: flagged after two missing polls
    t = DisallowedGoalTracker()
    out = _run(t, [([g2, g1], 2, 0), ([g2, g1], 1, 0), ([g1], 1, 0), ([g1], 1, 0)])
    assert [len(o) for o in out] == [0, 0, 0, 1]


def test_tracker_ignores_feed_blips() -> None:
    g1, g2 = _goal("KC", 1, "5:00"), _goal("TUL", 2, "3:10")
    t = DisallowedGoalTracker()
    out = _run(t, [
        ([g2, g1], 1, 1),
        ([], 1, 1), ([], 1, 1), ([], 1, 1),     # failed summary fetches
        ([g2, g1], 0, 0),                       # scoreboard glitch, goals present
        ([g2, g1], 1, 1),
        ([g1], 1, 1), ([g1], 1, 1), ([g1], 1, 1),  # goal dropped/re-entered, score unchanged
        ([_goal("TUL", 2, "3:45"), g1], 1, 1),  # re-entered 35 s off
    ])
    assert not any(out)
    assert t.goals == []


def test_tracker_second_disallowed_goal_after_rescoring() -> None:
    a, b, c = _goal("KC", 1, "2:00"), _goal("KC", 1, "9:00"), _goal("KC", 2, "4:00")
    t = DisallowedGoalTracker()
    out = _run(t, [
        ([a], 1, 0),
        ([b, a], 2, 0),
        ([a], 1, 0), ([a], 1, 0),          # b disallowed
        ([c, a], 2, 0),                    # KC scores again
        ([a], 1, 0), ([a], 1, 0),          # c disallowed too
    ])
    flagged = [g["time"] for o in out for g in o]
    assert flagged == ["9:00", "4:00"]


def test_tracker_restores_goal_that_returns() -> None:
    g1 = _goal("KC", 1, "5:00")
    t = DisallowedGoalTracker()
    _run(t, [([g1], 1, 0), ([], 0, 0), ([], 0, 0)])
    assert len(t.goals) == 1
    _run(t, [([g1], 1, 0)])
    assert t.goals == []


def test_tracker_attaches_nhl_review_and_resets_per_game() -> None:
    g1 = _goal("STL", 1, "16:28")
    reviews = [{"period": 1, "time": "16:28", "reason": "offside", "challenged_by": "DAL"}]
    t = DisallowedGoalTracker()
    out = _run(t, [([g1], 0, 1), ([], 0, 1), ([], 0, 0)], reviews)
    assert out[2][0]["reason"] == "offside" and out[2][0]["challenged_by"] == "DAL"
    # Not live → nothing tracked; a new game starts clean
    assert t.update("g1", False, [], {"home": 0, "away": 0}, _side) == []
    t.update("g2", True, [], {"home": 0, "away": 0}, _side)
    assert t.goals == []


# ----------------------------------------------------------------------
# Coordinators
# ----------------------------------------------------------------------


def _live(goals: list[dict], home: int, away: int, penalties: list[dict] | None = None, **kw) -> dict:
    return empty_game(
        game_state=kw.pop("state", GAME_STATE_LIVE), game_id="g1", is_home=True,
        home_team="Kansas City Mavericks", away_team="Tulsa Oilers",
        home_score=home, away_score=away, period=2, game_events=goals + (penalties or []),
        start_time=(datetime.now(UTC) - timedelta(hours=1)).isoformat(), **kw,
    )


async def _poll(coord, data: dict) -> dict:
    coord._track_disallowed(data)
    await coord._maybe_notify(data)
    return data


async def test_team_tracker_disallowed_and_incident_alerts(hass: HomeAssistant, team_entry) -> None:
    calls = _register_notify(hass)
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(team_entry, options={
        CONF_NOTIFY_DISALLOWED_ENABLED: True, CONF_NOTIFY_DISALLOWED_TARGETS: TARGET,
        CONF_NOTIFY_INCIDENT_ENABLED: True, CONF_NOTIFY_INCIDENT_TARGETS: TARGET,
    })
    coord = HockeyCoordinator(hass, team_entry)
    kc, tul = _goal("KC", 1, "5:00"), _goal("TUL", 2, "3:10")
    old_fight = [_pen("KC", "A", 1, "8:00", "Fighting - Major", 5), _pen("TUL", "B", 1, "8:00", "Fighting - Major", 5)]

    await _poll(coord, _live([tul, kc], 1, 1, old_fight))          # first sighting: baseline
    await _poll(coord, _live([kc], 1, 1, old_fight))
    await _poll(coord, _live([kc], 1, 0, old_fight))               # Tulsa goal off the board
    new_fight = [_pen("KC", "C", 2, "9:30", "Fighting - Major", 5), _pen("TUL", "D", 2, "9:30", "Fighting - Major", 5)]
    data = await _poll(coord, _live([kc], 1, 0, old_fight + new_fight))
    await _poll(coord, _live([kc], 1, 0, old_fight + new_fight))   # nothing repeated
    await hass.async_block_till_done()

    assert [c.data["title"] for c in calls] == ["Tulsa Oilers goal disallowed", "Hockey: Fight"]
    assert calls[0].data["message"] == "TUL scorer — P2 3:10 · Taken off the board | 1–0"
    assert calls[1].data["message"] == "Fight: C (KC) vs D (TUL) — P2 9:30"
    assert [g["time"] for g in data["disallowed_goals"]] == ["3:10"]


async def test_team_tracker_alerts_respect_toggle_and_final(hass: HomeAssistant, team_entry) -> None:
    calls = _register_notify(hass)
    team_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(team_entry, options={
        CONF_NOTIFY_DISALLOWED_ENABLED: False, CONF_NOTIFY_DISALLOWED_TARGETS: TARGET,
        CONF_NOTIFY_INCIDENT_ENABLED: True, CONF_NOTIFY_INCIDENT_TARGETS: TARGET,
    })
    coord = HockeyCoordinator(hass, team_entry)
    kc = _goal("KC", 1, "5:00")
    await _poll(coord, _live([kc], 1, 0))
    await _poll(coord, _live([], 0, 0))
    data = await _poll(coord, _live([], 0, 0))
    # Post-game scorer entries (FINAL) don't alert
    await _poll(coord, _live([], 0, 0, [_pen("KC", "A", 3, "20:00", "Game misconduct - Abuse of Officials", 10)],
                             state=GAME_STATE_FINAL))
    await hass.async_block_till_done()
    assert calls == []
    assert len(data["disallowed_goals"]) == 1  # still tracked for the card


def _playoff_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, title="NHL Playoffs (STL)", version=1, minor_version=2,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_PLAYOFF, CONF_LEAGUE: "NHL", CONF_API_KEY: "",
            CONF_FOLLOWED_TEAMS: ["STL"], CONF_FOLLOWED_TEAM_NAMES: ["St. Louis Blues"],
        },
        options={
            CONF_NOTIFY_DISALLOWED_ENABLED: True, CONF_NOTIFY_DISALLOWED_TARGETS: TARGET,
            CONF_NOTIFY_INCIDENT_ENABLED: True, CONF_NOTIFY_INCIDENT_TARGETS: TARGET,
        },
    )


async def test_playoff_tracker_disallowed_with_challenge_reason(hass: HomeAssistant) -> None:
    calls = _register_notify(hass)
    entry = _playoff_entry()
    entry.add_to_hass(hass)
    coord = PlayoffCoordinator(hass, entry)
    stl = _goal("STL", 1, "16:28", ours=True)
    reviews = [{"period": 1, "time": "16:28", "reason": "offside", "challenged_by": "DAL"}]

    def game(goals: list[dict], dal: int, stl_score: int, pens: list[dict] | None = None) -> dict:
        return empty_game(
            game_state=GAME_STATE_LIVE, game_id="2026030111", is_home=False,
            home_team="Dallas Stars", away_team="St. Louis Blues",
            home_team_id="DAL", away_team_id="STL", home_team_abbrev="DAL", away_team_abbrev="STL",
            home_score=dal, away_score=stl_score, game_events=goals + (pens or []), _reviews=reviews,
            start_time=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        )

    await _poll(coord, game([stl], 0, 1))
    await _poll(coord, game([], 0, 1))
    await _poll(coord, game([], 0, 0))
    pen = {"type": "penalty", "period": 2, "time": "2:00", "team_abbrev": "DAL", "player_name": "X Y",
           "description": "Game Misconduct", "incident": classify_nhl_penalty("GAM", "game-misconduct")}
    await _poll(coord, game([], 0, 0, [pen]))
    await hass.async_block_till_done()

    assert [c.data["title"] for c in calls] == ["St. Louis Blues goal disallowed", "Hockey: Ejection"]
    assert calls[0].data["message"] == (
        "STL scorer — P1 16:28 · Overturned on DAL challenge (offside) | 0–0"
    )
    assert calls[1].data["message"] == "Ejected: X Y (DAL) — Game Misconduct — P2 2:00"
