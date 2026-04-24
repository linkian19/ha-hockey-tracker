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
    SCAN_INTERVAL_FINAL,
    SCAN_INTERVAL_GAME_SOON,
    SCAN_INTERVAL_GAME_TODAY,
    SCAN_INTERVAL_IDLE,
    SCAN_INTERVAL_LIVE,
    SCAN_INTERVAL_PRE,
)

_LOGGER = logging.getLogger(__name__)


class EchlCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls HockeyTech for ECHL game data with adaptive update intervals."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.api_key: str = entry.data[CONF_API_KEY]
        self.team_id: str = entry.data[CONF_TEAM_ID]
        self._session: aiohttp.ClientSession | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_IDLE),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._fetch_team_game_data()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"HockeyTech request failed: {err}") from err

        self.update_interval = timedelta(seconds=self._next_interval(data))
        _LOGGER.debug(
            "Next poll in %s seconds (game_state=%s)",
            self.update_interval.total_seconds(),
            data.get("game_state"),
        )
        return data

    def _next_interval(self, data: dict) -> int:
        """Choose poll interval based on game state and time until next game."""
        state = data.get("game_state")

        if state == GAME_STATE_LIVE:
            return SCAN_INTERVAL_LIVE

        if state == GAME_STATE_PRE:
            return SCAN_INTERVAL_PRE

        if state == GAME_STATE_FINAL:
            return SCAN_INTERVAL_FINAL

        # NO_GAME — base interval on time until the next game
        next_game = data.get("next_game")
        if next_game and next_game.get("game_date"):
            hours_away = self._hours_until(next_game["game_date"])
            if hours_away is not None:
                if hours_away <= 6:
                    return SCAN_INTERVAL_GAME_SOON
                if hours_away <= 24:
                    return SCAN_INTERVAL_GAME_TODAY

        return SCAN_INTERVAL_IDLE

    @staticmethod
    def _hours_until(iso_date: str) -> float | None:
        try:
            game_dt = datetime.fromisoformat(iso_date)
            delta = game_dt - datetime.now(timezone.utc)
            return delta.total_seconds() / 3600
        except (ValueError, TypeError):
            return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _fetch(self, params: dict[str, str]) -> dict[str, Any]:
        base_params = {
            "key": self.api_key,
            "client_code": CLIENT_CODE,
            "lang_code": "en",
            "fmt": "json",
        }
        base_params.update(params)
        session = await self._get_session()
        async with session.get(
            HOCKEYTECH_BASE,
            params=base_params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _fetch_team_game_data(self) -> dict[str, Any]:
        scorebar = await self._fetch({
            "feed": "modulekit",
            "view": "scorebar",
            "numberofdaysahead": "7",
            "numberofdaysback": "1",
        })

        games = scorebar.get("SiteKit", {}).get("Scorebar", [])
        team_game = self._find_team_game(games)

        if team_game is None:
            next_game = await self._fetch_next_game()
            return {**self._empty_state(), "next_game": next_game}

        return self._normalize_game(team_game)

    def _find_team_game(self, games: list[dict]) -> dict | None:
        for game in games:
            if (
                str(game.get("HomeID")) == self.team_id
                or str(game.get("VisitorID")) == self.team_id
            ):
                return game
        return None

    async def _fetch_next_game(self) -> dict[str, Any] | None:
        try:
            result = await self._fetch({
                "feed": "modulekit",
                "view": "schedule",
                "team_id": self.team_id,
            })
            games = result.get("SiteKit", {}).get("Schedule", [])
            now = datetime.now(timezone.utc)
            upcoming = [g for g in games if self._parse_game_dt(g) >= now]
            upcoming.sort(key=self._parse_game_dt)
            if upcoming:
                g = upcoming[0]
                is_home = str(g.get("home_team")) == self.team_id
                return {
                    "game_date": g.get("GameDateISO8601"),
                    "home_team_id": g.get("home_team"),
                    "home_team_city": g.get("home_team_city"),
                    "home_team_nickname": g.get("home_team_nickname"),
                    "away_team_id": g.get("visiting_team"),
                    "away_team_city": g.get("visiting_team_city"),
                    "away_team_nickname": g.get("visiting_team_nickname"),
                    "venue": g.get("venue_name"),
                    "is_home": is_home,
                }
        except Exception as err:
            _LOGGER.debug("Could not fetch next game: %s", err)
        return None

    @staticmethod
    def _parse_game_dt(game: dict) -> datetime:
        raw = game.get("GameDateISO8601", "")
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    def _normalize_game(self, game: dict) -> dict[str, Any]:
        status = game.get("GameStatus", "")
        if status in ("1", "Pre-Game", "preview"):
            game_state = GAME_STATE_PRE
        elif status in ("4", "Final", "F"):
            game_state = GAME_STATE_FINAL
        else:
            game_state = GAME_STATE_LIVE

        is_home = str(game.get("HomeID")) == self.team_id

        return {
            "game_state": game_state,
            "game_id": game.get("GameID"),
            "start_time": game.get("GameDateISO8601"),
            "period": game.get("Period"),
            "clock": game.get("GameClock"),
            "home_team": f"{game.get('HomeCity', '')} {game.get('HomeNickname', '')}".strip(),
            "home_team_id": game.get("HomeID"),
            "home_score": game.get("HomeGoals", 0),
            "home_shots": game.get("HomeShots", 0),
            "away_team": f"{game.get('VisitorCity', '')} {game.get('VisitorNickname', '')}".strip(),
            "away_team_id": game.get("VisitorID"),
            "away_score": game.get("VisitorGoals", 0),
            "away_shots": game.get("VisitorShots", 0),
            "is_home": is_home,
            "venue": game.get("venue_name"),
            "next_game": None,
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
            "away_team": None,
            "away_team_id": None,
            "away_score": None,
            "away_shots": None,
            "is_home": None,
            "venue": None,
        }

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
