"""DataUpdateCoordinator for Hockey Tracker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_API_KEY,
    CONF_LEAGUE,
    CONF_TEAM_ID,
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
    RECENT_GAMES_MAX,
    SCAN_INTERVAL_FINAL,
    SCAN_INTERVAL_GAME_SOON,
    SCAN_INTERVAL_GAME_TODAY,
    SCAN_INTERVAL_IDLE,
    SCAN_INTERVAL_LIVE,
    SCAN_INTERVAL_PRE,
    SCHEDULE_CACHE_TTL,
)

_LOGGER = logging.getLogger(__name__)


class HockeyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls game data with adaptive update intervals. Supports ECHL, AHL, and NHL."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.league: str = entry.data[CONF_LEAGUE]
        self.team_id: str = entry.data[CONF_TEAM_ID]
        self.team_logo_url: str | None = entry.data.get("team_logo_url")
        self._session: aiohttp.ClientSession | None = None
        self._schedule_cache: list[dict] | None = None
        self._schedule_cache_time: datetime | None = None
        # Logo cache keyed by team ID (HockeyTech) or abbreviation (NHL).
        # HockeyTech CDN paths include a version suffix that cannot be constructed
        # from team ID alone, so we populate this from live API responses.
        self._logo_cache: dict[str, str] = {}

        if self.league in HOCKEYTECH_LEAGUES:
            self.api_key: str = entry.data[CONF_API_KEY]
            league_cfg = HOCKEYTECH_LEAGUES[self.league]
            self._client_code: str = league_cfg["client_code"]

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_IDLE),
        )

    # ------------------------------------------------------------------
    # Coordinator lifecycle
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if self.league == LEAGUE_NHL:
                data = await self._fetch_nhl()
            else:
                data = await self._fetch_hockeytech()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Request failed: {err}") from err

        self.update_interval = timedelta(seconds=self._next_interval(data))
        _LOGGER.debug(
            "Next poll in %ss (game_state=%s)",
            int(self.update_interval.total_seconds()),
            data.get("game_state"),
        )
        return data

    def _next_interval(self, data: dict) -> int:
        state = data.get("game_state")
        if state == GAME_STATE_LIVE:
            return SCAN_INTERVAL_LIVE
        if state == GAME_STATE_PRE:
            return SCAN_INTERVAL_PRE
        if state == GAME_STATE_FINAL:
            return SCAN_INTERVAL_FINAL

        next_game = data.get("next_game")
        if next_game and next_game.get("game_date"):
            hours = self._hours_until(next_game["game_date"])
            if hours is not None:
                if hours <= 6:
                    return SCAN_INTERVAL_GAME_SOON
                if hours <= 24:
                    return SCAN_INTERVAL_GAME_TODAY
        return SCAN_INTERVAL_IDLE

    @staticmethod
    def _hours_until(iso_date: str) -> float | None:
        try:
            return (datetime.fromisoformat(iso_date) - datetime.now(timezone.utc)).total_seconds() / 3600
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # HockeyTech (ECHL / AHL)
    # ------------------------------------------------------------------

    async def _fetch_hockeytech(self) -> dict[str, Any]:
        scorebar = await self._fetch_ht({
            "feed": "modulekit",
            "view": "scorebar",
            "numberofdaysahead": "2",
            "numberofdaysback": "30",
        })

        all_games: list[dict] = scorebar.get("SiteKit", {}).get("Scorebar", [])

        # Populate logo cache from all scorebar games — HomeLogo/VisitorLogo are
        # authoritative; the CDN path includes a version suffix that varies by team
        # and cannot be constructed from the team ID alone.
        for g in all_games:
            if g.get("HomeID") and g.get("HomeLogo"):
                self._logo_cache[str(g["HomeID"])] = self._upscale_ht_logo(g["HomeLogo"])
            if g.get("VisitorID") and g.get("VisitorLogo"):
                self._logo_cache[str(g["VisitorID"])] = self._upscale_ht_logo(g["VisitorLogo"])

        team_games = [
            g for g in all_games
            if str(g.get("HomeID")) == self.team_id or str(g.get("VisitorID")) == self.team_id
        ]

        active = self._ht_find_active(team_games)
        recent = self._ht_extract_recent(team_games)
        next_game = await self._get_ht_schedule_cached()

        data = self._ht_normalize_game(active) if active else self._empty_state()
        data["recent_games"] = recent
        data["next_game"] = next_game
        return data

    def _ht_find_active(self, team_games: list[dict]) -> dict | None:
        pre = None
        for g in team_games:
            status = str(g.get("GameStatus", ""))
            if status not in ("1", "4"):
                return g
            if status == "1" and pre is None:
                pre = g
        return pre

    def _ht_extract_recent(self, team_games: list[dict]) -> list[dict]:
        completed = [g for g in team_games if str(g.get("GameStatus", "")) == "4"]
        completed.sort(key=lambda g: g.get("GameDateISO8601", ""), reverse=True)
        return [self._ht_normalize_recent(g) for g in completed[:RECENT_GAMES_MAX]]

    async def _get_ht_schedule_cached(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        age = (
            (now - self._schedule_cache_time).total_seconds()
            if self._schedule_cache_time else float("inf")
        )
        if age > SCHEDULE_CACHE_TTL:
            self._schedule_cache = await self._fetch_ht_schedule()
            self._schedule_cache_time = now
        return self._ht_first_upcoming(self._schedule_cache or [])

    async def _fetch_ht_schedule(self) -> list[dict]:
        try:
            result = await self._fetch_ht({
                "feed": "modulekit",
                "view": "schedule",
                "team_id": self.team_id,
            })
            return result.get("SiteKit", {}).get("Schedule", [])
        except Exception as err:
            _LOGGER.debug("HockeyTech schedule fetch failed: %s", err)
            return self._schedule_cache or []

    def _ht_first_upcoming(self, games: list[dict]) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            [g for g in games if self._ht_parse_dt(g) >= now],
            key=self._ht_parse_dt,
        )
        if not upcoming:
            return None
        g = upcoming[0]
        home_id = str(g.get("home_team", ""))
        away_id = str(g.get("visiting_team", ""))
        return {
            "game_date": g.get("GameDateISO8601"),
            "is_home": home_id == self.team_id,
            "home_team": f"{g.get('home_team_city','')} {g.get('home_team_nickname','')}".strip(),
            "away_team": f"{g.get('visiting_team_city','')} {g.get('visiting_team_nickname','')}".strip(),
            "home_logo_url": self._logo_cache.get(home_id),
            "away_logo_url": self._logo_cache.get(away_id),
            "venue": g.get("venue_name", ""),
        }

    def _ht_normalize_game(self, game: dict) -> dict[str, Any]:
        status = str(game.get("GameStatus", ""))
        if status == "1":
            game_state = GAME_STATE_PRE
        elif status == "4":
            game_state = GAME_STATE_FINAL
        else:
            game_state = GAME_STATE_LIVE

        is_home = str(game.get("HomeID")) == self.team_id
        home_id = str(game.get("HomeID", ""))
        away_id = str(game.get("VisitorID", ""))

        return {
            "game_state": game_state,
            "game_id": game.get("GameID"),
            "start_time": game.get("GameDateISO8601"),
            "period": game.get("Period"),
            "clock": game.get("GameClock"),
            "home_team": f"{game.get('HomeCity','')} {game.get('HomeNickname','')}".strip(),
            "home_team_id": home_id,
            "home_score": game.get("HomeGoals", 0),
            "home_shots": game.get("HomeShots", 0),
            "home_logo_url": self._upscale_ht_logo(game.get("HomeLogo")) or self._logo_cache.get(home_id),
            "away_team": f"{game.get('VisitorCity','')} {game.get('VisitorNickname','')}".strip(),
            "away_team_id": away_id,
            "away_score": game.get("VisitorGoals", 0),
            "away_shots": game.get("VisitorShots", 0),
            "away_logo_url": self._upscale_ht_logo(game.get("VisitorLogo")) or self._logo_cache.get(away_id),
            "is_home": is_home,
            "team_logo_url": self.team_logo_url,
            "venue": game.get("venue_name"),
        }

    def _ht_normalize_recent(self, game: dict) -> dict[str, Any]:
        is_home = str(game.get("HomeID")) == self.team_id
        opp_id = str(game.get("VisitorID") if is_home else game.get("HomeID"))
        opp_name = (
            f"{game.get('VisitorCity','')} {game.get('VisitorNickname','')}".strip()
            if is_home
            else f"{game.get('HomeCity','')} {game.get('HomeNickname','')}".strip()
        )
        opp_logo = (
            (self._upscale_ht_logo(game.get("VisitorLogo")) or self._logo_cache.get(opp_id))
            if is_home
            else (self._upscale_ht_logo(game.get("HomeLogo")) or self._logo_cache.get(opp_id))
        )
        home_goals = int(game.get("HomeGoals") or 0)
        away_goals = int(game.get("VisitorGoals") or 0)
        team_score = home_goals if is_home else away_goals
        opp_score = away_goals if is_home else home_goals
        return {
            "date": game.get("GameDateISO8601"),
            "opponent": opp_name,
            "opponent_logo_url": opp_logo,
            "team_score": team_score,
            "opponent_score": opp_score,
            "is_home": is_home,
            "venue": game.get("venue_name"),
            "win": team_score > opp_score,
        }

    @staticmethod
    def _ht_parse_dt(game: dict) -> datetime:
        raw = game.get("GameDateISO8601", "")
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _upscale_ht_logo(url: str | None) -> str | None:
        """Strip size subdirectory from HockeyTech CDN URLs to get full-res image."""
        if not url:
            return None
        return re.sub(r"/logos/\d+x\d+/", "/logos/", url)

    async def _fetch_ht(self, params: dict[str, str]) -> dict[str, Any]:
        base = {
            "key": self.api_key,
            "client_code": self._client_code,
            "lang_code": "en",
            "fmt": "json",
        }
        base.update(params)
        session = await self._get_session()
        async with session.get(
            HOCKEYTECH_BASE,
            params=base,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ------------------------------------------------------------------
    # NHL
    # ------------------------------------------------------------------

    async def _fetch_nhl(self) -> dict[str, Any]:
        scoreboard = await self._fetch_json(f"{NHL_API_BASE}/scoreboard/now")

        all_games: list[dict] = []
        for day in scoreboard.get("gamesByDate", []):
            all_games.extend(day.get("games", []))
        all_games.extend(scoreboard.get("games", []))

        # Populate logo cache from scoreboard team objects
        for g in all_games:
            for key in ("awayTeam", "homeTeam"):
                team = g.get(key, {})
                abbrev = team.get("abbrev")
                logo = team.get("logo")
                if abbrev and logo:
                    self._logo_cache[abbrev] = logo.replace("_dark.svg", "_light.svg")

        team_games = [
            g for g in all_games
            if g.get("awayTeam", {}).get("abbrev") == self.team_id
            or g.get("homeTeam", {}).get("abbrev") == self.team_id
        ]

        active = self._nhl_find_active(team_games)
        schedule_games = await self._get_nhl_schedule_cached()
        recent = self._nhl_extract_recent(schedule_games)
        next_game = self._nhl_first_upcoming(schedule_games)

        data = self._nhl_normalize_game(active) if active else self._empty_state()
        data["recent_games"] = recent
        data["next_game"] = next_game
        return data

    def _nhl_find_active(self, team_games: list[dict]) -> dict | None:
        live = next((g for g in team_games if g.get("gameState") in NHL_LIVE_STATES), None)
        if live:
            return live
        final = next((g for g in team_games if g.get("gameState") in NHL_FINAL_STATES), None)
        if final:
            return final
        return next((g for g in team_games if g.get("gameState") in NHL_PRE_STATES), None)

    async def _get_nhl_schedule_cached(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        age = (
            (now - self._schedule_cache_time).total_seconds()
            if self._schedule_cache_time else float("inf")
        )
        if age > SCHEDULE_CACHE_TTL:
            self._schedule_cache = await self._fetch_nhl_schedule()
            self._schedule_cache_time = now
            # Refresh logo cache from standings whenever schedule cache expires
            await self._refresh_nhl_logo_cache()
        return self._schedule_cache or []

    async def _fetch_nhl_schedule(self) -> list[dict]:
        try:
            result = await self._fetch_json(
                f"{NHL_API_BASE}/club-schedule-season/{self.team_id}/now"
            )
            games = result.get("games", [])
            for g in games:
                for key in ("awayTeam", "homeTeam"):
                    team = g.get(key, {})
                    abbrev = team.get("abbrev")
                    logo = team.get("logo")
                    if abbrev and logo:
                        self._logo_cache[abbrev] = logo.replace("_dark.svg", "_light.svg")
            return games
        except Exception as err:
            _LOGGER.debug("NHL schedule fetch failed: %s", err)
            return self._schedule_cache or []

    async def _refresh_nhl_logo_cache(self) -> None:
        """Fetch all NHL team logos from standings — most complete source."""
        try:
            data = await self._fetch_json(f"{NHL_API_BASE}/standings/now")
            for entry in data.get("standings", []):
                abbrev = entry.get("teamAbbrev", {}).get("default", "")
                logo = entry.get("teamLogo", "")
                if abbrev and logo:
                    self._logo_cache[abbrev] = logo.replace("_dark.svg", "_light.svg")
        except Exception as err:
            _LOGGER.debug("NHL logo cache refresh failed: %s", err)

    def _nhl_first_upcoming(self, games: list[dict]) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            [g for g in games if self._nhl_parse_dt(g) > now],
            key=self._nhl_parse_dt,
        )
        if not upcoming:
            return None
        g = upcoming[0]
        away = g.get("awayTeam", {})
        home = g.get("homeTeam", {})
        return {
            "game_date": g.get("startTimeUTC"),
            "is_home": home.get("abbrev") == self.team_id,
            "home_team": self._nhl_full_name(home),
            "away_team": self._nhl_full_name(away),
            "home_logo_url": self._nhl_logo(home.get("abbrev"), home.get("logo")),
            "away_logo_url": self._nhl_logo(away.get("abbrev"), away.get("logo")),
            "venue": g.get("venue", {}).get("default", ""),
        }

    def _nhl_extract_recent(self, games: list[dict]) -> list[dict]:
        now = datetime.now(timezone.utc)
        completed = [
            g for g in games
            if g.get("gameState") in NHL_FINAL_STATES and self._nhl_parse_dt(g) < now
        ]
        completed.sort(key=self._nhl_parse_dt, reverse=True)
        return [self._nhl_normalize_recent(g) for g in completed[:RECENT_GAMES_MAX]]

    def _nhl_normalize_game(self, game: dict) -> dict[str, Any]:
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
        is_home = home.get("abbrev") == self.team_id

        return {
            "game_state": game_state,
            "game_id": game.get("id"),
            "start_time": game.get("startTimeUTC"),
            "period": period,
            "clock": clock,
            "home_team": self._nhl_full_name(home),
            "home_team_id": home.get("abbrev"),
            "home_score": home.get("score", 0),
            "home_shots": home.get("sog"),
            "home_logo_url": self._nhl_logo(home.get("abbrev"), home.get("logo")),
            "away_team": self._nhl_full_name(away),
            "away_team_id": away.get("abbrev"),
            "away_score": away.get("score", 0),
            "away_shots": away.get("sog"),
            "away_logo_url": self._nhl_logo(away.get("abbrev"), away.get("logo")),
            "is_home": is_home,
            "team_logo_url": self.team_logo_url,
            "venue": game.get("venue", {}).get("default"),
        }

    def _nhl_normalize_recent(self, game: dict) -> dict[str, Any]:
        away = game.get("awayTeam", {})
        home = game.get("homeTeam", {})
        is_home = home.get("abbrev") == self.team_id
        opp = away if is_home else home
        team_score = int(home.get("score") or 0) if is_home else int(away.get("score") or 0)
        opp_score = int(away.get("score") or 0) if is_home else int(home.get("score") or 0)
        return {
            "date": game.get("startTimeUTC") or game.get("gameDate"),
            "opponent": self._nhl_full_name(opp),
            "opponent_logo_url": self._nhl_logo(opp.get("abbrev"), opp.get("logo")),
            "team_score": team_score,
            "opponent_score": opp_score,
            "is_home": is_home,
            "venue": game.get("venue", {}).get("default"),
            "win": team_score > opp_score,
        }

    @staticmethod
    def _nhl_full_name(team: dict) -> str:
        city = team.get("placeName", {}).get("default", "")
        name = team.get("commonName", {}).get("default", team.get("abbrev", ""))
        return f"{city} {name}".strip()

    def _nhl_logo(self, abbrev: str | None, direct_url: str | None = None) -> str | None:
        """Return the best available logo URL for an NHL team.

        Priority: direct URL from API response → standings/scoreboard cache →
        constructed SVG URL as last resort. Always uses _light.svg variant so
        logos are visible on light-themed HA dashboards.
        """
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

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        session = await self._get_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

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
            "team_logo_url": self.team_logo_url,
            "venue": None,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
