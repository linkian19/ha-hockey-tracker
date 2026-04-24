"""DataUpdateCoordinator for ECHL Tracker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CLIENT_CODE,
    CONF_API_KEY,
    CONF_TEAM_ID,
    DOMAIN,
    GAME_STATE_FINAL,
    GAME_STATE_LIVE,
    GAME_STATE_NONE,
    GAME_STATE_PRE,
    HOCKEYTECH_BASE,
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


class EchlCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls HockeyTech for ECHL game data with adaptive update intervals."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.api_key: str = entry.data[CONF_API_KEY]
        self.team_id: str = entry.data[CONF_TEAM_ID]
        self._session: aiohttp.ClientSession | None = None
        self._schedule_cache: list[dict] | None = None
        self._schedule_cache_time: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_IDLE),
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @staticmethod
    def logo_url(team_id: str | int | None) -> str | None:
        if not team_id:
            return None
        return f"https://assets.leaguestat.com/echl/logos/{team_id}.png"

    # ------------------------------------------------------------------
    # Coordinator lifecycle
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._fetch_team_game_data()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"HockeyTech request failed: {err}") from err

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
    # Main data fetch
    # ------------------------------------------------------------------

    async def _fetch_team_game_data(self) -> dict[str, Any]:
        # Single scorebar call covers active game + recent completed games
        scorebar = await self._fetch({
            "feed": "modulekit",
            "view": "scorebar",
            "numberofdaysahead": "2",
            "numberofdaysback": "30",
        })

        all_games: list[dict] = scorebar.get("SiteKit", {}).get("Scorebar", [])
        team_games = [
            g for g in all_games
            if str(g.get("HomeID")) == self.team_id or str(g.get("VisitorID")) == self.team_id
        ]

        active = self._find_active_game(team_games)
        recent = self._extract_recent_games(team_games)
        next_game = await self._get_next_game_cached()

        if active is None:
            data = self._empty_state()
        else:
            data = self._normalize_game(active)

        data["recent_games"] = recent
        data["next_game"] = next_game
        return data

    def _find_active_game(self, team_games: list[dict]) -> dict | None:
        """Return the LIVE game first, then any PRE game, else None."""
        pre = None
        for g in team_games:
            status = str(g.get("GameStatus", ""))
            if status not in ("1", "4"):
                return g  # In progress
            if status == "1" and pre is None:
                pre = g
        return pre

    def _extract_recent_games(self, team_games: list[dict]) -> list[dict]:
        completed = [g for g in team_games if str(g.get("GameStatus", "")) == "4"]
        completed.sort(key=lambda g: g.get("GameDateISO8601", ""), reverse=True)
        return [self._normalize_recent_game(g) for g in completed[:RECENT_GAMES_MAX]]

    # ------------------------------------------------------------------
    # Schedule (next game, cached)
    # ------------------------------------------------------------------

    async def _get_next_game_cached(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        age = (
            (now - self._schedule_cache_time).total_seconds()
            if self._schedule_cache_time else float("inf")
        )
        if age > SCHEDULE_CACHE_TTL:
            self._schedule_cache = await self._fetch_schedule_games()
            self._schedule_cache_time = now
        return self._first_upcoming(self._schedule_cache or [])

    async def _fetch_schedule_games(self) -> list[dict]:
        try:
            result = await self._fetch({
                "feed": "modulekit",
                "view": "schedule",
                "team_id": self.team_id,
            })
            return result.get("SiteKit", {}).get("Schedule", [])
        except Exception as err:
            _LOGGER.debug("Schedule fetch failed: %s", err)
            return self._schedule_cache or []

    def _first_upcoming(self, games: list[dict]) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            [g for g in games if self._parse_schedule_dt(g) >= now],
            key=self._parse_schedule_dt,
        )
        if not upcoming:
            return None
        g = upcoming[0]
        is_home = str(g.get("home_team")) == self.team_id
        home_id = str(g.get("home_team", ""))
        away_id = str(g.get("visiting_team", ""))
        return {
            "game_date": g.get("GameDateISO8601"),
            "is_home": is_home,
            "home_team": f"{g.get('home_team_city','')} {g.get('home_team_nickname','')}".strip(),
            "away_team": f"{g.get('visiting_team_city','')} {g.get('visiting_team_nickname','')}".strip(),
            "home_logo_url": self.logo_url(home_id),
            "away_logo_url": self.logo_url(away_id),
            "venue": g.get("venue_name", ""),
        }

    # ------------------------------------------------------------------
    # Normalizers
    # ------------------------------------------------------------------

    def _normalize_game(self, game: dict) -> dict[str, Any]:
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
            "home_logo_url": self.logo_url(home_id),
            "away_team": f"{game.get('VisitorCity','')} {game.get('VisitorNickname','')}".strip(),
            "away_team_id": away_id,
            "away_score": game.get("VisitorGoals", 0),
            "away_shots": game.get("VisitorShots", 0),
            "away_logo_url": self.logo_url(away_id),
            "is_home": is_home,
            "team_logo_url": self.logo_url(self.team_id),
            "venue": game.get("venue_name"),
        }

    def _normalize_recent_game(self, game: dict) -> dict[str, Any]:
        is_home = str(game.get("HomeID")) == self.team_id
        opp_id = game.get("VisitorID") if is_home else game.get("HomeID")
        opp_name = (
            f"{game.get('VisitorCity','')} {game.get('VisitorNickname','')}".strip()
            if is_home
            else f"{game.get('HomeCity','')} {game.get('HomeNickname','')}".strip()
        )
        home_goals = int(game.get("HomeGoals") or 0)
        away_goals = int(game.get("VisitorGoals") or 0)
        team_score = home_goals if is_home else away_goals
        opp_score = away_goals if is_home else home_goals
        return {
            "date": game.get("GameDateISO8601"),
            "opponent": opp_name,
            "opponent_logo_url": self.logo_url(opp_id),
            "team_score": team_score,
            "opponent_score": opp_score,
            "is_home": is_home,
            "venue": game.get("venue_name"),
            "win": team_score > opp_score,
        }

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
            "team_logo_url": self.logo_url(self.team_id),
            "venue": None,
        }

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _fetch(self, params: dict[str, str]) -> dict[str, Any]:
        base = {
            "key": self.api_key,
            "client_code": CLIENT_CODE,
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

    @staticmethod
    def _parse_schedule_dt(game: dict) -> datetime:
        raw = game.get("GameDateISO8601", "")
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
