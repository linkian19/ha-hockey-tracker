"""Playoff bracket coordinator for Hockey Tracker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_API_KEY,
    CONF_FOLLOWED_TEAMS,
    CONF_LEAGUE,
    CONF_NOTIFY_GOAL_ENABLED,
    CONF_NOTIFY_GOAL_TARGETS,
    CONF_NOTIFY_PREGAME_ENABLED,
    CONF_NOTIFY_PREGAME_TARGETS,
    CONF_NOTIFY_WIN_ENABLED,
    CONF_NOTIFY_WIN_TARGETS,
    DOMAIN,
    GAME_STATE_FINAL,
    GAME_STATE_LIVE,
    GAME_STATE_NONE,
    GAME_STATE_PRE,
    HOCKEYTECH_BASE,
    HOCKEYTECH_LEAGUES,
    LEAGUE_NHL,
    NHL_API_BASE,
    NHL_FINAL_STATES,
    NHL_LIVE_STATES,
    NHL_PRE_STATES,
    ROUND_DATE_WINDOW,
    SCAN_INTERVAL_FINAL,
    SCAN_INTERVAL_GAME_ENDING,
    SCAN_INTERVAL_GAME_SOON,
    SCAN_INTERVAL_GAME_TODAY,
    SCAN_INTERVAL_IDLE,
    SCAN_INTERVAL_LIVE,
    SCAN_INTERVAL_PRE,
    SCHEDULE_CACHE_TTL,
)

_LOGGER = logging.getLogger(__name__)

# How long to cache the playoff season ID (rarely changes within a season)
_SEASON_CACHE_TTL = 43200  # 12 hours


class PlayoffCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls playoff bracket data for up to 4 followed teams."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self.league: str = entry.data[CONF_LEAGUE]
        self.followed_team_ids: list[str] = list(entry.data.get(CONF_FOLLOWED_TEAMS, []))
        self._session: aiohttp.ClientSession | None = None
        self._logo_cache: dict[str, str] = {}

        # HockeyTech-specific state
        self._api_key: str = entry.data.get(CONF_API_KEY, "")
        self._client_code: str = ""
        if self.league in HOCKEYTECH_LEAGUES:
            self._client_code = HOCKEYTECH_LEAGUES[self.league]["client_code"]

        # Cached playoff season ID (HockeyTech)
        self._playoff_season_id: str | None = None
        self._season_cache_time: datetime | None = None

        # Bracket cache (shared for both leagues)
        self._bracket_cache: list[dict] | None = None
        self._bracket_cache_time: datetime | None = None

        # Notification state per followed team
        self._notif_pregame_sent: dict[str, str] = {}   # team_id → game_id
        self._notif_win_sent: dict[str, str] = {}        # team_id → game_id
        self._notif_goal_count: dict[str, int] = {}      # team_id → goal count
        self._notif_goal_game_id: dict[str, str] = {}    # team_id → game_id

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_IDLE),
        )

    def _notif_opts(self) -> dict:
        return self._entry.options

    def clear_schedule_cache(self) -> None:
        self._bracket_cache = None
        self._bracket_cache_time = None

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if self.league == LEAGUE_NHL:
                data = await self._fetch_nhl_playoffs()
            else:
                data = await self._fetch_ht_playoffs()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Request failed: {err}") from err

        await self._maybe_notify(data)
        data["last_fetched"] = datetime.now(timezone.utc).isoformat()
        self.update_interval = timedelta(seconds=self._next_interval(data))
        return data

    def _next_interval(self, data: dict) -> int:
        state = data.get("game_state")
        if state == GAME_STATE_LIVE:
            period = data.get("period")
            clock = data.get("clock")
            if period:
                p = int(period)
                if p >= 4 or (p == 3 and clock == "0:00"):
                    return SCAN_INTERVAL_GAME_ENDING
            return SCAN_INTERVAL_LIVE
        if state == GAME_STATE_PRE:
            return SCAN_INTERVAL_PRE
        if state == GAME_STATE_FINAL:
            return SCAN_INTERVAL_FINAL
        next_game = data.get("next_game")
        if next_game and next_game.get("game_date"):
            try:
                hours = (
                    datetime.fromisoformat(next_game["game_date"].replace("Z", "+00:00"))
                    - datetime.now(timezone.utc)
                ).total_seconds() / 3600
                if hours <= 6:
                    return SCAN_INTERVAL_GAME_SOON
                if hours <= 24:
                    return SCAN_INTERVAL_GAME_TODAY
            except (ValueError, TypeError):
                pass
        return SCAN_INTERVAL_IDLE

    # ------------------------------------------------------------------
    # NHL playoffs
    # ------------------------------------------------------------------

    async def _fetch_nhl_playoffs(self) -> dict[str, Any]:
        year = datetime.now(timezone.utc).year
        bracket_raw = await self._fetch_json(f"{NHL_API_BASE}/playoff-bracket/{year}")
        scoreboard = await self._fetch_json(f"{NHL_API_BASE}/scoreboard/now")

        all_games: list[dict] = []
        for day in scoreboard.get("gamesByDate", []):
            all_games.extend(day.get("games", []))
        all_games.extend(scoreboard.get("games", []))

        for g in all_games:
            for key in ("awayTeam", "homeTeam"):
                team = g.get(key, {})
                abbrev = team.get("abbrev")
                logo = team.get("logo")
                if abbrev and logo:
                    self._logo_cache[abbrev] = logo.replace("_dark.svg", "_light.svg")

        bracket = self._build_nhl_bracket(bracket_raw, all_games)
        current_round = self._current_round_number(bracket)

        # Find most-relevant active game for any followed team
        active_game_raw = self._nhl_find_followed_game(all_games)
        landing = None
        if active_game_raw and active_game_raw.get("gameState") in NHL_LIVE_STATES | NHL_FINAL_STATES:
            game_id = active_game_raw.get("id")
            if game_id:
                landing = await self._fetch_nhl_landing(game_id)

        game_data = self._nhl_normalize_game(active_game_raw, landing) if active_game_raw else self._empty_state()

        # Schedule cache for next-game lookup
        schedule_games = await self._get_nhl_schedule_cached()
        next_game = self._nhl_first_upcoming_followed(schedule_games)

        return {
            **game_data,
            "bracket": bracket,
            "current_round": current_round,
            "followed_teams": self.followed_team_ids,
            "next_game": next_game,
        }

    def _build_nhl_bracket(self, raw: dict, scoreboard_games: list[dict]) -> list[dict]:
        rounds: dict[int, dict] = {}
        for series in raw.get("series", []):
            rnum = series.get("playoffRound", 1)
            if rnum not in rounds:
                rounds[rnum] = {
                    "round_number": rnum,
                    "round_name": self._nhl_round_name(rnum),
                    "series": [],
                }
            top = series.get("topSeedTeam") or {}
            bot = series.get("bottomSeedTeam") or {}
            top_id = top.get("abbrev", "")
            bot_id = bot.get("abbrev", "")
            top_wins = series.get("topSeedWins", 0)
            bot_wins = series.get("bottomSeedWins", 0)

            status = "scheduled"
            if top_wins == 4 or bot_wins == 4:
                status = "complete"
            elif top_wins > 0 or bot_wins > 0:
                status = "active"

            winner_id = None
            if series.get("winningTeamId"):
                wid = series["winningTeamId"]
                if top.get("id") == wid:
                    winner_id = top_id
                elif bot.get("id") == wid:
                    winner_id = bot_id

            # Find active scoreboard game for this series
            active_raw = next(
                (
                    g for g in scoreboard_games
                    if {g.get("awayTeam", {}).get("abbrev"), g.get("homeTeam", {}).get("abbrev")}
                    == {top_id, bot_id}
                ),
                None,
            )

            rounds[rnum]["series"].append(
                self._nhl_series_obj(series, top, bot, top_id, bot_id, top_wins, bot_wins, status, winner_id, active_raw)
            )

        return sorted(rounds.values(), key=lambda r: r["round_number"])

    def _nhl_series_obj(
        self, series, top, bot, top_id, bot_id, top_wins, bot_wins,
        status, winner_id, active_raw
    ) -> dict:
        game_state = None
        game_score = None
        game_period = None
        game_clock = None
        if active_raw:
            raw_state = active_raw.get("gameState", "")
            if raw_state in NHL_LIVE_STATES:
                game_state = GAME_STATE_LIVE
                home = active_raw.get("homeTeam", {})
                away = active_raw.get("awayTeam", {})
                game_score = f"{home.get('score', 0)}-{away.get('score', 0)}"
                pd = active_raw.get("periodDescriptor", {})
                game_period = pd.get("number")
                clock_data = active_raw.get("clock", {})
                game_clock = "INT" if clock_data.get("inIntermission") else clock_data.get("timeRemaining")
            elif raw_state in NHL_PRE_STATES:
                game_state = GAME_STATE_PRE
            elif raw_state in NHL_FINAL_STATES:
                game_state = GAME_STATE_FINAL

        return {
            "series_letter": series.get("seriesLetter", ""),
            "team1_id": top_id,
            "team1_name": top.get("name", {}).get("default", top_id),
            "team1_abbrev": top_id,
            "team1_logo_url": self._nhl_logo(top_id, top.get("logo")),
            "team1_wins": top_wins,
            "team1_is_followed": top_id in self.followed_team_ids,
            "team2_id": bot_id,
            "team2_name": bot.get("name", {}).get("default", bot_id),
            "team2_abbrev": bot_id,
            "team2_logo_url": self._nhl_logo(bot_id, bot.get("logo")),
            "team2_wins": bot_wins,
            "team2_is_followed": bot_id in self.followed_team_ids,
            "status": status,
            "winner_id": winner_id,
            "game_state": game_state,
            "game_score": game_score,
            "game_period": game_period,
            "game_clock": game_clock,
        }

    @staticmethod
    def _nhl_round_name(round_num: int) -> str:
        names = {1: "1st Round", 2: "2nd Round", 3: "Conference Finals", 4: "Stanley Cup Finals"}
        return names.get(round_num, f"Round {round_num}")

    def _nhl_find_followed_game(self, games: list[dict]) -> dict | None:
        """Return the most relevant active game involving any followed team."""
        live = next(
            (g for g in games if g.get("gameState") in NHL_LIVE_STATES
             and self._nhl_team_in_game(g)),
            None,
        )
        if live:
            return live
        final = next(
            (g for g in games if g.get("gameState") in NHL_FINAL_STATES
             and self._nhl_team_in_game(g)),
            None,
        )
        if final:
            return final
        return next(
            (g for g in games if g.get("gameState") in NHL_PRE_STATES
             and self._nhl_team_in_game(g)),
            None,
        )

    def _nhl_team_in_game(self, game: dict) -> bool:
        abbrevs = {game.get("awayTeam", {}).get("abbrev"), game.get("homeTeam", {}).get("abbrev")}
        return bool(abbrevs & set(self.followed_team_ids))

    def _nhl_normalize_game(self, game: dict, landing: dict | None = None) -> dict[str, Any]:
        away = game.get("awayTeam", {})
        home = game.get("homeTeam", {})
        raw_state = game.get("gameState", "")

        if raw_state in NHL_LIVE_STATES:
            game_state = GAME_STATE_LIVE
        elif raw_state in NHL_FINAL_STATES:
            game_state = GAME_STATE_FINAL
        else:
            game_state = GAME_STATE_PRE

        period_desc = game.get("periodDescriptor", {})
        period = period_desc.get("number")
        clock_data = game.get("clock", {})
        clock = "INT" if clock_data.get("inIntermission") else clock_data.get("timeRemaining")

        home_abbrev = home.get("abbrev", "")
        away_abbrev = away.get("abbrev", "")
        # The "tracked team" is whichever followed team is in this game
        tracked = next((t for t in self.followed_team_ids if t in (home_abbrev, away_abbrev)), home_abbrev)
        is_home = home_abbrev == tracked

        if landing:
            away_sog = landing.get("awayTeam", {}).get("sog", away.get("sog"))
            home_sog = landing.get("homeTeam", {}).get("sog", home.get("sog"))
        else:
            away_sog = away.get("sog")
            home_sog = home.get("sog")

        home_city = home.get("placeName", {}).get("default", "")
        home_name = home.get("commonName", {}).get("default", home_abbrev)
        away_city = away.get("placeName", {}).get("default", "")
        away_name = away.get("commonName", {}).get("default", away_abbrev)

        return {
            "game_state": game_state,
            "game_id": game.get("id"),
            "start_time": game.get("startTimeUTC"),
            "period": period,
            "clock": clock,
            "home_team": f"{home_city} {home_name}".strip(),
            "home_team_id": home_abbrev,
            "home_score": home.get("score", 0),
            "home_shots": home_sog,
            "home_logo_url": self._nhl_logo(home_abbrev, home.get("logo")),
            "away_team": f"{away_city} {away_name}".strip(),
            "away_team_id": away_abbrev,
            "away_score": away.get("score", 0),
            "away_shots": away_sog,
            "away_logo_url": self._nhl_logo(away_abbrev, away.get("logo")),
            "is_home": is_home,
            "team_logo_url": self._nhl_logo(tracked),
            "venue": game.get("venue", {}).get("default"),
            "game_events": self._nhl_extract_events(landing) if landing else [],
        }

    async def _get_nhl_schedule_cached(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        age = (
            (now - self._bracket_cache_time).total_seconds()
            if self._bracket_cache_time else float("inf")
        )
        if age > SCHEDULE_CACHE_TTL:
            games = []
            for team_id in self.followed_team_ids:
                try:
                    result = await self._fetch_json(
                        f"{NHL_API_BASE}/club-schedule-season/{team_id}/now"
                    )
                    games.extend(result.get("games", []))
                    for g in result.get("games", []):
                        for key in ("awayTeam", "homeTeam"):
                            t = g.get(key, {})
                            abbrev = t.get("abbrev")
                            logo = t.get("logo")
                            if abbrev and logo:
                                self._logo_cache[abbrev] = logo.replace("_dark.svg", "_light.svg")
                except Exception as err:
                    _LOGGER.debug("NHL schedule fetch failed for %s: %s", team_id, err)
            self._bracket_cache = games
            self._bracket_cache_time = now
        return self._bracket_cache or []

    def _nhl_first_upcoming_followed(self, games: list[dict]) -> dict | None:
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            [g for g in games if self._nhl_parse_dt(g) > now and self._nhl_team_in_game(g)],
            key=self._nhl_parse_dt,
        )
        if not upcoming:
            return None
        g = upcoming[0]
        away = g.get("awayTeam", {})
        home = g.get("homeTeam", {})
        return {
            "game_date": g.get("startTimeUTC"),
            "is_home": home.get("abbrev") in self.followed_team_ids,
            "home_team": f"{home.get('placeName',{}).get('default','')} {home.get('commonName',{}).get('default',home.get('abbrev',''))}".strip(),
            "away_team": f"{away.get('placeName',{}).get('default','')} {away.get('commonName',{}).get('default',away.get('abbrev',''))}".strip(),
            "home_logo_url": self._nhl_logo(home.get("abbrev"), home.get("logo")),
            "away_logo_url": self._nhl_logo(away.get("abbrev"), away.get("logo")),
            "venue": g.get("venue", {}).get("default", ""),
        }

    async def _fetch_nhl_landing(self, game_id: int) -> dict | None:
        try:
            return await self._fetch_json(f"{NHL_API_BASE}/gamecenter/{game_id}/landing")
        except Exception as err:
            _LOGGER.debug("NHL landing fetch failed for %s: %s", game_id, err)
            return None

    def _nhl_extract_events(self, landing: dict) -> list[dict]:
        events: list[dict] = []
        summ = landing.get("summary", {})
        for period_data in summ.get("scoring", []):
            period_num = (
                period_data.get("periodDescriptor", {}).get("number")
                or period_data.get("period", 0)
            )
            for goal in period_data.get("goals", []):
                team_abbrev = goal.get("teamAbbrev", {}).get("default", "")
                strength = goal.get("strength", "ev")
                situation = goal.get("situationCode", "")
                events.append({
                    "type": "goal",
                    "period": period_num,
                    "time": goal.get("timeInPeriod", ""),
                    "team_abbrev": team_abbrev,
                    "is_tracked_team": team_abbrev in self.followed_team_ids,
                    "player_name": f"{goal.get('firstName',{}).get('default','')} {goal.get('lastName',{}).get('default','')}".strip(),
                    "player_number": None,
                    "assists": [f"{a.get('firstName',{}).get('default','')} {a.get('lastName',{}).get('default','')}".strip() for a in goal.get("assists", [])],
                    "is_power_play": strength == "pp",
                    "is_short_handed": strength == "sh",
                    "is_empty_net": "6" in situation,
                })
        for period_data in summ.get("penalties", []):
            period_num = period_data.get("periodDescriptor", {}).get("number", 0)
            for pen in period_data.get("penalties", []):
                team_abbrev = pen.get("teamAbbrev", {}).get("default", "")
                player = pen.get("committedByPlayer", {})
                events.append({
                    "type": "penalty",
                    "period": period_num,
                    "time": pen.get("timeInPeriod", ""),
                    "team_abbrev": team_abbrev,
                    "is_tracked_team": team_abbrev in self.followed_team_ids,
                    "player_name": f"{player.get('firstName',{}).get('default','')} {player.get('lastName',{}).get('default','')}".strip(),
                    "player_number": player.get("sweaterNumber"),
                    "description": pen.get("descKey", "").replace("-", " ").title(),
                    "minutes": pen.get("duration"),
                })
        events.sort(key=lambda e: (e.get("period", 0), self._time_to_sec(e.get("time", "0:00"))), reverse=True)
        return events

    # ------------------------------------------------------------------
    # HockeyTech playoffs
    # ------------------------------------------------------------------

    async def _fetch_ht_playoffs(self) -> dict[str, Any]:
        season_id = await self._get_ht_playoff_season_id()
        if not season_id:
            return {**self._empty_state(), "bracket": [], "current_round": 0, "followed_teams": self.followed_team_ids, "next_game": None}

        schedule_games = await self._fetch_ht_schedule(season_id)
        scorebar_games = await self._fetch_ht_scorebar()

        # Populate logo cache from scorebar
        for g in scorebar_games:
            if g.get("HomeID") and g.get("HomeLogo"):
                self._logo_cache[str(g["HomeID"])] = self._upscale_ht_logo(g["HomeLogo"])
            if g.get("VisitorID") and g.get("VisitorLogo"):
                self._logo_cache[str(g["VisitorID"])] = self._upscale_ht_logo(g["VisitorLogo"])

        bracket = self._build_ht_bracket(schedule_games, scorebar_games)
        current_round = self._current_round_number(bracket)

        # Find active game for any followed team
        active_raw = self._ht_find_followed_game(scorebar_games)
        summary = None
        if active_raw and str(active_raw.get("GameStatus", "")) not in ("1",):
            game_id = str(active_raw.get("GameID") or active_raw.get("ID") or "")
            if game_id:
                summary = await self._fetch_ht_game_summary(game_id)

        game_data = self._ht_normalize_game(active_raw, summary) if active_raw else self._empty_state()

        # Next game from schedule
        next_game = self._ht_first_upcoming_followed(schedule_games)

        return {
            **game_data,
            "bracket": bracket,
            "current_round": current_round,
            "followed_teams": self.followed_team_ids,
            "next_game": next_game,
        }

    async def _get_ht_playoff_season_id(self) -> str | None:
        now = datetime.now(timezone.utc)
        age = (
            (now - self._season_cache_time).total_seconds()
            if self._season_cache_time else float("inf")
        )
        if self._playoff_season_id and age < _SEASON_CACHE_TTL:
            return self._playoff_season_id

        try:
            data = await self._fetch_ht({"feed": "modulekit", "view": "seasons"})
            seasons = data.get("SiteKit", {}).get("Seasons", [])
            # Find the most recent playoff season (career=1, playoff=1)
            for season in seasons:
                if season.get("career") == "1" and season.get("playoff") == "1":
                    self._playoff_season_id = str(season["season_id"])
                    self._season_cache_time = now
                    return self._playoff_season_id
        except Exception as err:
            _LOGGER.debug("HockeyTech seasons fetch failed: %s", err)
        return None

    async def _fetch_ht_schedule(self, season_id: str) -> list[dict]:
        try:
            data = await self._fetch_ht({"feed": "modulekit", "view": "schedule", "season_id": season_id})
            return data.get("SiteKit", {}).get("Schedule", [])
        except Exception as err:
            _LOGGER.debug("HockeyTech playoff schedule fetch failed: %s", err)
            return []

    async def _fetch_ht_scorebar(self) -> list[dict]:
        try:
            data = await self._fetch_ht({
                "feed": "modulekit",
                "view": "scorebar",
                "numberofdaysahead": "3",
                "numberofdaysback": "60",
            })
            return data.get("SiteKit", {}).get("Scorebar", [])
        except Exception as err:
            _LOGGER.debug("HockeyTech playoff scorebar fetch failed: %s", err)
            return []

    def _build_ht_bracket(self, schedule: list[dict], scorebar: list[dict]) -> list[dict]:
        """Build bracket from schedule games grouped by game_letter, clustered into rounds."""
        # Only use games with a game_letter (playoff series identifier)
        playoff_games = [g for g in schedule if g.get("game_letter")]

        # Group by game_letter → series
        series_map: dict[str, dict] = {}
        for g in playoff_games:
            letter = g["game_letter"]
            if letter not in series_map:
                series_map[letter] = {
                    "series_letter": letter,
                    "team1_id": str(g.get("home_team", "") or g.get("HomeID", "")),
                    "team1_name": f"{g.get('home_team_city','')} {g.get('home_team_nickname','')}".strip(),
                    "team1_abbrev": g.get("home_team_code", ""),
                    "team2_id": str(g.get("visiting_team", "") or g.get("VisitorID", "")),
                    "team2_name": f"{g.get('visiting_team_city','')} {g.get('visiting_team_nickname','')}".strip(),
                    "team2_abbrev": g.get("visiting_team_code", ""),
                    "first_game_date": g.get("GameDateISO8601", ""),
                    "_games": [],
                }
            series_map[letter]["_games"].append(g)
            # Track earliest date
            if g.get("GameDateISO8601", "") < series_map[letter]["first_game_date"]:
                series_map[letter]["first_game_date"] = g["GameDateISO8601"]

        # Compute series wins from completed schedule games
        for s in series_map.values():
            t1_id = s["team1_id"]
            t1_wins = 0
            t2_wins = 0
            games_played = 0
            for g in s["_games"]:
                if str(g.get("status", g.get("game_status", ""))) != "4":
                    continue
                games_played += 1
                home_id = str(g.get("home_team", "") or g.get("HomeID", ""))
                vis_id = str(g.get("visiting_team", "") or g.get("VisitorID", ""))
                home_goals = int(g.get("home_goal_count", g.get("HomeGoals", 0)) or 0)
                vis_goals = int(g.get("visiting_goal_count", g.get("VisitorGoals", 0)) or 0)
                if home_goals > vis_goals:
                    if home_id == t1_id:
                        t1_wins += 1
                    else:
                        t2_wins += 1
                elif vis_goals > home_goals:
                    if vis_id == t1_id:
                        t1_wins += 1
                    else:
                        t2_wins += 1
            s["team1_wins"] = t1_wins
            s["team2_wins"] = t2_wins
            s["games_played"] = games_played

        # Supplement team info and logos from scorebar
        scorebar_by_letter = {}
        for g in scorebar:
            letter = g.get("game_letter")
            if letter:
                scorebar_by_letter.setdefault(letter, []).append(g)

        for letter, s in series_map.items():
            sb_games = scorebar_by_letter.get(letter, [])
            if sb_games:
                first_sb = sb_games[0]
                if not s["team1_name"]:
                    s["team1_name"] = first_sb.get("HomeLongName", s["team1_abbrev"])
                if not s["team2_name"]:
                    s["team2_name"] = first_sb.get("VisitorLongName", s["team2_abbrev"])
                if not s["team1_abbrev"]:
                    s["team1_abbrev"] = first_sb.get("HomeCode", s["team1_id"])
                if not s["team2_abbrev"]:
                    s["team2_abbrev"] = first_sb.get("VisitorCode", s["team2_id"])
            s["team1_logo_url"] = self._logo_cache.get(s["team1_id"])
            s["team2_logo_url"] = self._logo_cache.get(s["team2_id"])
            s["team1_is_followed"] = s["team1_id"] in self.followed_team_ids
            s["team2_is_followed"] = s["team2_id"] in self.followed_team_ids

        # Determine series status + live game info
        for letter, s in series_map.items():
            t1w, t2w = s["team1_wins"], s["team2_wins"]
            if t1w == 4 or t2w == 4:
                s["status"] = "complete"
                s["winner_id"] = s["team1_id"] if t1w == 4 else s["team2_id"]
            elif t1w > 0 or t2w > 0:
                s["status"] = "active"
                s["winner_id"] = None
            else:
                s["status"] = "scheduled"
                s["winner_id"] = None

            # Live game info from scorebar
            active_sb = next(
                (g for g in scorebar_by_letter.get(letter, [])
                 if str(g.get("GameStatus", "")) not in ("1", "4")),
                None,
            )
            s["game_state"] = None
            s["game_score"] = None
            s["game_period"] = None
            s["game_clock"] = None
            if active_sb:
                s["game_state"] = GAME_STATE_LIVE
                s["game_score"] = f"{active_sb.get('HomeGoals',0)}-{active_sb.get('VisitorGoals',0)}"
                s["game_period"] = active_sb.get("Period")
                s["game_clock"] = active_sb.get("GameClock")
            elif self._ht_is_today(s, scorebar_by_letter.get(letter, [])):
                s["game_state"] = GAME_STATE_PRE

        # Cluster into rounds by start date
        sorted_series = sorted(series_map.values(), key=lambda s: s.get("first_game_date", ""))
        rounds = self._cluster_into_rounds(sorted_series)

        # Strip internal _games list
        for round_obj in rounds:
            for s in round_obj["series"]:
                s.pop("_games", None)
                s.pop("first_game_date", None)

        return rounds

    def _cluster_into_rounds(self, sorted_series: list[dict]) -> list[dict]:
        """Group series by start date proximity into round buckets."""
        rounds: list[list[dict]] = []
        bucket: list[dict] = []
        bucket_date: datetime | None = None

        for s in sorted_series:
            raw = s.get("first_game_date", "")
            try:
                this_dt = datetime.fromisoformat(raw[:10])
            except (ValueError, TypeError):
                bucket.append(s)
                continue
            if bucket_date is None:
                bucket_date = this_dt
                bucket.append(s)
            elif (this_dt - bucket_date).days <= ROUND_DATE_WINDOW:
                bucket.append(s)
            else:
                rounds.append(bucket)
                bucket = [s]
                bucket_date = this_dt

        if bucket:
            rounds.append(bucket)

        result = []
        for i, series_list in enumerate(rounds, start=1):
            result.append({
                "round_number": i,
                "round_name": self._ht_round_name(i, len(rounds)),
                "series": series_list,
            })
        return result

    @staticmethod
    def _ht_round_name(round_num: int, total_rounds: int) -> str:
        if total_rounds >= 4:
            names = {total_rounds: "Championship", total_rounds - 1: "Conference Finals"}
            return names.get(round_num, f"Round {round_num}")
        if total_rounds == 3:
            names = {3: "Championship", 2: "Finals"}
            return names.get(round_num, f"Round {round_num}")
        return f"Round {round_num}"

    def _ht_find_followed_game(self, scorebar: list[dict]) -> dict | None:
        live = next(
            (g for g in scorebar
             if str(g.get("GameStatus", "")) not in ("1", "4") and self._ht_team_in_game(g)
             and self._ht_parse_dt(g) >= datetime.now(timezone.utc) - timedelta(hours=4)),
            None,
        )
        if live:
            return live
        final = next(
            (g for g in scorebar
             if str(g.get("GameStatus", "")) == "4" and self._ht_team_in_game(g)
             and self._ht_parse_dt(g) >= datetime.now(timezone.utc) - timedelta(hours=4)),
            None,
        )
        if final:
            return final
        return next(
            (g for g in scorebar
             if str(g.get("GameStatus", "")) == "1" and self._ht_team_in_game(g)),
            None,
        )

    def _ht_team_in_game(self, game: dict) -> bool:
        home_id = str(game.get("HomeID", ""))
        vis_id = str(game.get("VisitorID", ""))
        return bool({home_id, vis_id} & set(self.followed_team_ids))

    def _ht_normalize_game(self, game: dict, summary: dict | None = None) -> dict[str, Any]:
        status = str(game.get("GameStatus", ""))
        if status == "1":
            game_state = GAME_STATE_PRE
        elif status == "4":
            game_state = GAME_STATE_FINAL
        else:
            game_state = GAME_STATE_LIVE

        home_id = str(game.get("HomeID", ""))
        vis_id = str(game.get("VisitorID", ""))
        tracked = next((t for t in self.followed_team_ids if t in (home_id, vis_id)), home_id)
        is_home = home_id == tracked

        home_shots = None
        away_shots = None
        if summary:
            home_shots = summary.get("homeTeam", {}).get("stats", {}).get("shots")
            away_shots = summary.get("visitingTeam", {}).get("stats", {}).get("shots")

        return {
            "game_state": game_state,
            "game_id": game.get("GameID") or game.get("ID"),
            "start_time": game.get("GameDateISO8601"),
            "period": game.get("Period"),
            "clock": game.get("GameClock"),
            "home_team": f"{game.get('HomeCity','')} {game.get('HomeNickname','')}".strip(),
            "home_team_id": home_id,
            "home_team_abbrev": game.get("HomeCode", game.get("home_team_code", "")),
            "home_score": game.get("HomeGoals", 0),
            "home_shots": home_shots,
            "home_logo_url": self._upscale_ht_logo(game.get("HomeLogo")) or self._logo_cache.get(home_id),
            "away_team": f"{game.get('VisitorCity','')} {game.get('VisitorNickname','')}".strip(),
            "away_team_id": vis_id,
            "away_team_abbrev": game.get("VisitorCode", game.get("visiting_team_code", "")),
            "away_score": game.get("VisitorGoals", 0),
            "away_shots": away_shots,
            "away_logo_url": self._upscale_ht_logo(game.get("VisitorLogo")) or self._logo_cache.get(vis_id),
            "is_home": is_home,
            "team_logo_url": self._logo_cache.get(tracked),
            "venue": game.get("venue_name"),
            "game_events": self._ht_extract_events(summary) if summary else [],
        }

    def _ht_first_upcoming_followed(self, schedule: list[dict]) -> dict | None:
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            [g for g in schedule if self._ht_parse_dt(g) > now and self._ht_sched_team_in_game(g)],
            key=self._ht_parse_dt,
        )
        if not upcoming:
            return None
        g = upcoming[0]
        home_id = str(g.get("home_team", ""))
        away_id = str(g.get("visiting_team", ""))
        return {
            "game_date": g.get("GameDateISO8601"),
            "is_home": home_id in self.followed_team_ids,
            "home_team": f"{g.get('home_team_city','')} {g.get('home_team_nickname','')}".strip(),
            "away_team": f"{g.get('visiting_team_city','')} {g.get('visiting_team_nickname','')}".strip(),
            "home_logo_url": self._logo_cache.get(home_id),
            "away_logo_url": self._logo_cache.get(away_id),
            "venue": g.get("venue_name", ""),
        }

    def _ht_sched_team_in_game(self, g: dict) -> bool:
        home = str(g.get("home_team", ""))
        vis = str(g.get("visiting_team", ""))
        return bool({home, vis} & set(self.followed_team_ids))

    async def _fetch_ht_game_summary(self, game_id: str) -> dict | None:
        try:
            session = await self._get_session()
            params = {
                "feed": "statviewfeed",
                "view": "gameSummary",
                "game_id": game_id,
                "key": self._api_key,
                "client_code": self._client_code,
                "lang_code": "en",
                "fmt": "json",
            }
            async with session.get(
                HOCKEYTECH_BASE, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                resp.raise_for_status()
                text = (await resp.text()).strip()
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1]
                return json.loads(text)
        except Exception as err:
            _LOGGER.debug("HockeyTech game summary fetch failed for %s: %s", game_id, err)
            return None

    def _ht_extract_events(self, summary: dict) -> list[dict]:
        events: list[dict] = []
        for period in summary.get("periods") or []:
            period_num = int((period.get("info") or {}).get("id", 0))
            for goal in period.get("goals") or []:
                team_id = str((goal.get("team") or {}).get("id", ""))
                scorer = goal.get("scoredBy") or {}
                props = goal.get("properties") or {}
                events.append({
                    "type": "goal",
                    "period": period_num,
                    "time": goal.get("time", ""),
                    "team_abbrev": (goal.get("team") or {}).get("abbreviation", ""),
                    "is_tracked_team": team_id in self.followed_team_ids,
                    "player_name": f"{scorer.get('firstName','')} {scorer.get('lastName','')}".strip(),
                    "player_number": scorer.get("jerseyNumber"),
                    "assists": [f"{a.get('firstName','')} {a.get('lastName','')}".strip() for a in goal.get("assists") or []],
                    "is_power_play": props.get("isPowerPlay") == "1",
                    "is_short_handed": props.get("isShortHanded") == "1",
                    "is_empty_net": props.get("isEmptyNet") == "1",
                })
            for pen in period.get("penalties") or []:
                team_id = str((pen.get("againstTeam") or {}).get("id", ""))
                player = pen.get("takenBy") or {}
                events.append({
                    "type": "penalty",
                    "period": period_num,
                    "time": pen.get("time", ""),
                    "team_abbrev": (pen.get("againstTeam") or {}).get("abbreviation", ""),
                    "is_tracked_team": team_id in self.followed_team_ids,
                    "player_name": f"{player.get('firstName','')} {player.get('lastName','')}".strip(),
                    "player_number": player.get("jerseyNumber"),
                    "description": pen.get("description", ""),
                    "minutes": pen.get("minutes"),
                })
        events.sort(key=lambda e: (e.get("period", 0), self._time_to_sec(e.get("time", "0:00"))), reverse=True)
        return events

    async def _fetch_ht(self, params: dict) -> dict[str, Any]:
        base = {
            "key": self._api_key,
            "client_code": self._client_code,
            "lang_code": "en",
            "fmt": "json",
        }
        base.update(params)
        session = await self._get_session()
        async with session.get(
            HOCKEYTECH_BASE, params=base, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _current_round_number(bracket: list[dict]) -> int:
        """Return the highest round number that has at least one active/incomplete series."""
        current = 0
        for round_obj in bracket:
            for s in round_obj.get("series", []):
                if s.get("status") in ("active", "scheduled"):
                    current = max(current, round_obj["round_number"])
        return current or (bracket[-1]["round_number"] if bracket else 0)

    @staticmethod
    def _ht_is_today(series: dict, sb_games: list[dict]) -> bool:
        today = datetime.now(timezone.utc).date()
        for g in sb_games:
            if str(g.get("GameStatus", "")) == "1":
                raw = g.get("GameDateISO8601", "")
                try:
                    if datetime.fromisoformat(raw).astimezone(timezone.utc).date() == today:
                        return True
                except (ValueError, TypeError):
                    pass
        return False

    @staticmethod
    def _ht_parse_dt(game: dict) -> datetime:
        raw = game.get("GameDateISO8601", "")
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _upscale_ht_logo(url: str | None) -> str | None:
        if not url:
            return None
        return re.sub(r"/logos/\d+x\d+/", "/logos/", url)

    def _nhl_logo(self, abbrev: str | None, direct_url: str | None = None) -> str | None:
        url = direct_url or self._logo_cache.get(abbrev or "")
        if url:
            return url.replace("_dark.svg", "_light.svg")
        if not abbrev:
            return None
        return f"https://assets.nhle.com/logos/nhl/svg/{abbrev}_light.svg"

    @staticmethod
    def _nhl_parse_dt(game: dict) -> datetime:
        raw = game.get("startTimeUTC") or game.get("gameDate", "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _time_to_sec(t: str) -> int:
        parts = t.split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0
        except (ValueError, IndexError):
            return 0

    def _empty_state(self) -> dict[str, Any]:
        return {
            "game_state": GAME_STATE_NONE,
            "game_id": None,
            "start_time": None,
            "period": None,
            "clock": None,
            "home_team": None,
            "home_team_id": None,
            "home_score": None,
            "home_shots": None,
            "home_logo_url": None,
            "away_team": None,
            "away_team_id": None,
            "away_score": None,
            "away_shots": None,
            "away_logo_url": None,
            "is_home": None,
            "team_logo_url": None,
            "venue": None,
            "game_events": [],
        }

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        session = await self._get_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_targets(val: Any) -> list[str]:
        if isinstance(val, list):
            return [s for s in val if s]
        if isinstance(val, str):
            return [s.strip() for s in val.split(",") if s.strip()]
        return []

    async def _maybe_notify(self, data: dict) -> None:
        opts = self._notif_opts()
        state = data.get("game_state")
        game_id = str(data.get("game_id") or "")
        is_home = data.get("is_home")
        our_score = data.get("home_score") if is_home else data.get("away_score")
        opp_score = data.get("away_score") if is_home else data.get("home_score")
        our_team = data.get("home_team") if is_home else data.get("away_team")
        opp_team = data.get("away_team") if is_home else data.get("home_team")
        our_id = data.get("home_team_id") if is_home else data.get("away_team_id")

        if not our_id:
            return

        # Pre-game
        if opts.get(CONF_NOTIFY_PREGAME_ENABLED) and game_id and self._notif_pregame_sent.get(our_id) != game_id:
            targets = self._parse_targets(opts.get(CONF_NOTIFY_PREGAME_TARGETS, []))
            if targets and state == GAME_STATE_PRE and data.get("start_time"):
                try:
                    start = datetime.fromisoformat(data["start_time"].replace("Z", "+00:00"))
                    mins = (start - datetime.now(timezone.utc)).total_seconds() / 60
                    if 0 <= mins <= 35:
                        await self._send_notifications(targets, "Hockey: Game Starting Soon", f"{our_team} vs {opp_team} starts in {int(mins)} minutes!")
                        self._notif_pregame_sent[our_id] = game_id
                except (ValueError, TypeError):
                    pass

        # Build abbrev → team name map so goal notifications name the correct scoring team
        # For NHL, home_team_id IS the abbrev. For HT, home_team_abbrev is separate.
        home_abbrev = data.get("home_team_abbrev") or data.get("home_team_id", "")
        away_abbrev = data.get("away_team_abbrev") or data.get("away_team_id", "")
        team_name_by_abbrev: dict[str, str] = {}
        if home_abbrev:
            team_name_by_abbrev[home_abbrev] = data.get("home_team") or our_team or home_abbrev
        if away_abbrev:
            team_name_by_abbrev[away_abbrev] = data.get("away_team") or opp_team or away_abbrev

        # Goal
        if opts.get(CONF_NOTIFY_GOAL_ENABLED) and state == GAME_STATE_LIVE and game_id:
            targets = self._parse_targets(opts.get(CONF_NOTIFY_GOAL_TARGETS, []))
            if targets:
                if self._notif_goal_game_id.get(our_id) != game_id:
                    self._notif_goal_game_id[our_id] = game_id
                    self._notif_goal_count[our_id] = 0
                our_goals = [e for e in data.get("game_events", []) if e.get("type") == "goal" and e.get("is_tracked_team")]
                prev = self._notif_goal_count.get(our_id, 0)
                if len(our_goals) > prev:
                    for goal in our_goals[:len(our_goals) - prev]:
                        scorer = goal.get("player_name", "")
                        tag = " (PP)" if goal.get("is_power_play") else " (SH)" if goal.get("is_short_handed") else " (EN)" if goal.get("is_empty_net") else ""
                        # Use the goal's team_abbrev to get the correct team name
                        scoring_abbrev = goal.get("team_abbrev", "")
                        scoring_team = team_name_by_abbrev.get(scoring_abbrev) or our_team
                        score_str = f"{our_score}–{opp_score}" if our_score is not None else ""
                        msg = f"{scorer}{tag} — P{goal.get('period','')} {goal.get('time','')}".strip()
                        if score_str:
                            msg += f" | {score_str}"
                        await self._send_notifications(targets, f"GOAL! {scoring_team} scores!", msg)
                    self._notif_goal_count[our_id] = len(our_goals)

        # Win
        if opts.get(CONF_NOTIFY_WIN_ENABLED) and game_id and self._notif_win_sent.get(our_id) != game_id:
            targets = self._parse_targets(opts.get(CONF_NOTIFY_WIN_TARGETS, []))
            if targets and state == GAME_STATE_FINAL:
                start_time = data.get("start_time")
                recent = False
                if start_time:
                    try:
                        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                        recent = (datetime.now(timezone.utc) - start).total_seconds() < 43200
                    except (ValueError, TypeError):
                        pass
                if recent and our_score is not None and opp_score is not None and our_score > opp_score:
                    await self._send_notifications(targets, f"{our_team} Wins!", f"Final: {our_team} {our_score}, {opp_team} {opp_score}. {our_team} wins!")
                    self._notif_win_sent[our_id] = game_id

    async def _send_notifications(self, targets: list[str], title: str, message: str) -> None:
        for target in targets:
            try:
                parts = target.rsplit(".", 1)
                domain = parts[0] if len(parts) == 2 else "notify"
                service_name = parts[1] if len(parts) == 2 else parts[0]
                await self.hass.services.async_call(
                    domain, service_name, {"title": title, "message": message}, blocking=False
                )
            except Exception as err:
                _LOGGER.warning("Failed to send notification to %s: %s", target, err)
