"""DataUpdateCoordinator for Hockey Tracker."""
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
    AHL_GAME_URL,
    CONF_API_KEY,
    CONF_LEAGUE,
    CONF_NOTIFY_GOAL_ENABLED,
    CONF_NOTIFY_GOAL_TARGETS,
    CONF_NOTIFY_PREGAME_ENABLED,
    CONF_NOTIFY_PREGAME_TARGETS,
    CONF_NOTIFY_WIN_ENABLED,
    CONF_NOTIFY_WIN_TARGETS,
    CONF_TEAM_ID,
    DOMAIN,
    FINAL_DISPLAY_SECONDS,
    GAME_STATE_FINAL,
    GAME_STATE_LIVE,
    GAME_STATE_NONE,
    GAME_STATE_PRE,
    HOCKEYTECH_BASE,
    HOCKEYTECH_GAME_REPORT_URL,
    HOCKEYTECH_LEAGUES,
    LEAGUE_AHL,
    LEAGUE_ECHL,
    LEAGUE_NHL,
    LEAGUE_PWHL,
    NHL_API_BASE,
    NHL_FINAL_STATES,
    NHL_GAME_URL,
    NHL_LIVE_STATES,
    NHL_PRE_STATES,
    PWHL_GAME_URL,
    RECENT_GAMES_MAX,
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


def _event_sort_key(e: dict) -> tuple:
    parts = e.get("time", "0:00").split(":")
    t = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0
    return (e.get("period", 0), t)


class HockeyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls game data with adaptive update intervals. Supports all HockeyTech leagues and NHL."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self.league: str = entry.data[CONF_LEAGUE]
        self.team_id: str = entry.data[CONF_TEAM_ID]
        self.team_logo_url: str | None = entry.data.get("team_logo_url")
        self._session: aiohttp.ClientSession | None = None
        self._schedule_cache: list[dict] | None = None
        self._schedule_cache_time: datetime | None = None
        # Server-side FINAL window tracking — keyed by game_id so it survives
        # browser refreshes and HA restarts within the same process.
        self._game_final_at: datetime | None = None
        self._game_final_id: str | None = None
        # Logo cache keyed by team ID (HockeyTech) or abbreviation (NHL).
        # HockeyTech CDN paths include a version suffix that cannot be constructed
        # from team ID alone, so we populate this from live API responses.
        self._logo_cache: dict[str, str] = {}
        # Notification state — prevents duplicate alerts for the same event.
        self._notif_pregame_sent_id: str | None = None
        self._notif_win_sent_id: str | None = None
        self._notif_goal_count: int = 0
        self._notif_goal_game_id: str | None = None

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

    def _notif_opts(self) -> dict:
        return self._entry.options

    def clear_schedule_cache(self) -> None:
        """Force the next update to re-fetch schedule/logo data from the API."""
        self._schedule_cache = None
        self._schedule_cache_time = None

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

        # Fire notifications before any state transitions so win/goal
        # alerts fire the first time FINAL/goals are seen.
        await self._maybe_notify(data)

        # Manage the post-game FINAL display window server-side.
        # Tracking by game_id ensures the window is not restarted by browser
        # refreshes or new HA frontend sessions.
        if data.get("game_state") == GAME_STATE_FINAL:
            game_id = str(data.get("game_id", ""))
            if self._game_final_id != game_id:
                self._game_final_id = game_id
                self._game_final_at = datetime.now(timezone.utc)
            elif self._game_final_at and (
                datetime.now(timezone.utc) - self._game_final_at
            ).total_seconds() > FINAL_DISPLAY_SECONDS:
                data["game_state"] = GAME_STATE_NONE
        else:
            self._game_final_id = None
            self._game_final_at = None

        data["last_fetched"] = datetime.now(timezone.utc).isoformat()

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
            # Poll extra-fast at end of regulation (P3 clock 0:00) and throughout
            # OT (period ≥ 4) — OT is sudden-death so the game can end at any
            # clock time, never reaching 0:00.
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

        # Fetch game summary for live and final games — provides shots and event feed
        summary = None
        if active and str(active.get("GameStatus", "")) not in ("1",):
            game_id = str(active.get("GameID") or active.get("ID") or "")
            if game_id:
                summary = await self._fetch_ht_game_summary(game_id)

        data = self._ht_normalize_game(active, summary) if active else self._empty_state()
        data["recent_games"] = recent
        data["next_game"] = next_game
        return data

    def _ht_find_active(self, team_games: list[dict]) -> dict | None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
        live = None
        recent_final = None
        pre = None
        for g in team_games:
            status = str(g.get("GameStatus", ""))
            if status not in ("1", "4"):
                if live is None and self._ht_parse_dt(g) >= cutoff:
                    live = g
            elif status == "4" and recent_final is None:
                if self._ht_parse_dt(g) >= cutoff:
                    recent_final = g
            elif status == "1" and pre is None:
                pre = g
        return live or recent_final or pre

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

    def _ht_normalize_game(self, game: dict, summary: dict | None = None) -> dict[str, Any]:
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

        # Shots from gameSummary (scorebar has no shot fields)
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
            "home_score": game.get("HomeGoals", 0),
            "home_shots": home_shots,
            "home_logo_url": self._upscale_ht_logo(game.get("HomeLogo")) or self._logo_cache.get(home_id),
            "away_team": f"{game.get('VisitorCity','')} {game.get('VisitorNickname','')}".strip(),
            "away_team_id": away_id,
            "away_score": game.get("VisitorGoals", 0),
            "away_shots": away_shots,
            "away_logo_url": self._upscale_ht_logo(game.get("VisitorLogo")) or self._logo_cache.get(away_id),
            "is_home": is_home,
            "team_logo_url": self._my_logo(),
            "venue": game.get("venue_name"),
            "game_events": self._ht_extract_events(summary) if summary else [],
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
        game_id = game.get("GameID") or game.get("ID")
        return {
            "date": game.get("GameDateISO8601"),
            "opponent": opp_name,
            "opponent_logo_url": opp_logo,
            "team_score": team_score,
            "opponent_score": opp_score,
            "is_home": is_home,
            "venue": game.get("venue_name"),
            "win": team_score > opp_score,
            "game_url": self._ht_game_url(game, game_id),
        }

    def _ht_game_url(self, game: dict, game_id: str | None) -> str | None:
        if not game_id:
            return None
        if self.league == LEAGUE_AHL:
            return AHL_GAME_URL.format(game_id=game_id)
        if self.league == LEAGUE_PWHL:
            return PWHL_GAME_URL.format(game_id=game_id)
        if self.league == LEAGUE_ECHL:
            date_str = (game.get("GameDateISO8601") or "")[:10]
            if date_str and date_str.count("-") == 2:
                year, month, day = date_str.split("-")
                home = f"{game.get('HomeCity', '')} {game.get('HomeNickname', '')}".strip()
                away = f"{game.get('VisitorCity', '')} {game.get('VisitorNickname', '')}".strip()
                home_slug = re.sub(r"[^a-z0-9]+", "-", home.lower()).strip("-")
                away_slug = re.sub(r"[^a-z0-9]+", "-", away.lower()).strip("-")
                if home_slug and away_slug:
                    return f"https://echl.com/games/{year}/{month}/{day}/{home_slug}-vs-{away_slug}"
        return HOCKEYTECH_GAME_REPORT_URL.format(client_code=self._client_code, game_id=game_id)

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
        """Strip size subdirectory from HockeyTech CDN URLs to get full-res image."""
        if not url:
            return None
        return re.sub(r"/logos/\d+x\d+/", "/logos/", url)

    def _my_logo(self) -> str | None:
        """Return the best available logo URL for the tracked team.

        Prefers the live logo cache (populated from API responses and always
        upscaled/current) over the URL stored in entry data at setup time.
        """
        if self.league == LEAGUE_NHL:
            return self._nhl_logo(self.team_id)
        return self._logo_cache.get(self.team_id) or self.team_logo_url

    async def _fetch_ht_game_summary(self, game_id: str) -> dict | None:
        """Fetch shot totals and play-by-play events for a live game.

        The statviewfeed endpoint wraps its JSON in outer parentheses (JSONP
        style) regardless of fmt=json, so we strip those before parsing.
        """
        try:
            session = await self._get_session()
            params = {
                "feed": "statviewfeed",
                "view": "gameSummary",
                "game_id": game_id,
                "key": self.api_key,
                "client_code": self._client_code,
                "lang_code": "en",
                "fmt": "json",
            }
            async with session.get(
                HOCKEYTECH_BASE,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                text = (await resp.text()).strip()
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1]
                return json.loads(text)
        except Exception as err:
            _LOGGER.debug("Game summary fetch failed for game %s: %s", game_id, err)
            return None

    def _ht_extract_events(self, summary: dict) -> list[dict]:
        """Build a chronological event list (goals + penalties) from a game summary."""
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
                    "is_tracked_team": team_id == self.team_id,
                    "player_name": f"{scorer.get('firstName','')} {scorer.get('lastName','')}".strip(),
                    "player_number": scorer.get("jerseyNumber"),
                    "assists": [
                        f"{a.get('firstName','')} {a.get('lastName','')}".strip()
                        for a in goal.get("assists") or []
                    ],
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
                    "is_tracked_team": team_id == self.team_id,
                    "player_name": f"{player.get('firstName','')} {player.get('lastName','')}".strip(),
                    "player_number": player.get("jerseyNumber"),
                    "description": pen.get("description", ""),
                    "minutes": pen.get("minutes"),
                })

        events.sort(key=_event_sort_key, reverse=True)
        return events

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

        # Fetch gamecenter landing for live and final games: provides SOG and play-by-play events
        landing = None
        if active and active.get("gameState") in NHL_LIVE_STATES | NHL_FINAL_STATES:
            game_id = active.get("id")
            if game_id:
                landing = await self._fetch_nhl_landing(game_id)

        schedule_games = await self._get_nhl_schedule_cached()
        recent = self._nhl_extract_recent(schedule_games)
        next_game = self._nhl_first_upcoming(schedule_games)

        data = self._nhl_normalize_game(active, landing) if active else self._empty_state()
        data["recent_games"] = recent
        data["next_game"] = next_game
        return data

    def _nhl_find_active(self, team_games: list[dict]) -> dict | None:
        live = next((g for g in team_games if g.get("gameState") in NHL_LIVE_STATES), None)
        if live:
            return live
        finals = [g for g in team_games if g.get("gameState") in NHL_FINAL_STATES]
        if finals:
            return max(finals, key=lambda g: g.get("startTimeUTC") or g.get("gameDate") or "")
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
        is_home = home.get("abbrev") == self.team_id

        # SOG from landing (scoreboard omits sog); fall back to scoreboard field if present
        if landing:
            away_sog = landing.get("awayTeam", {}).get("sog", away.get("sog"))
            home_sog = landing.get("homeTeam", {}).get("sog", home.get("sog"))
        else:
            away_sog = away.get("sog")
            home_sog = home.get("sog")

        return {
            "game_state": game_state,
            "game_id": game.get("id"),
            "start_time": game.get("startTimeUTC"),
            "period": period,
            "clock": clock,
            "home_team": self._nhl_full_name(home),
            "home_team_id": home.get("abbrev"),
            "home_score": home.get("score", 0),
            "home_shots": home_sog,
            "home_logo_url": self._nhl_logo(home.get("abbrev"), home.get("logo")),
            "away_team": self._nhl_full_name(away),
            "away_team_id": away.get("abbrev"),
            "away_score": away.get("score", 0),
            "away_shots": away_sog,
            "away_logo_url": self._nhl_logo(away.get("abbrev"), away.get("logo")),
            "is_home": is_home,
            "team_logo_url": self._my_logo(),
            "venue": game.get("venue", {}).get("default"),
            "game_events": self._nhl_extract_events(landing) if landing else [],
        }

    async def _fetch_nhl_landing(self, game_id: int) -> dict | None:
        """Fetch NHL gamecenter landing page — provides SOG and scoring/penalty events."""
        try:
            return await self._fetch_json(f"{NHL_API_BASE}/gamecenter/{game_id}/landing")
        except Exception as err:
            _LOGGER.debug("NHL landing fetch failed for game %s: %s", game_id, err)
            return None

    def _nhl_extract_events(self, landing: dict) -> list[dict]:
        """Build a chronological event list (goals + penalties) from NHL landing data."""
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
                # Detect empty net: situationCode digit indicates 6 skaters (pulled goalie)
                situation = goal.get("situationCode", "")
                is_empty_net = "6" in situation
                events.append({
                    "type": "goal",
                    "period": period_num,
                    "time": goal.get("timeInPeriod", ""),
                    "team_abbrev": team_abbrev,
                    "is_tracked_team": team_abbrev == self.team_id,
                    "player_name": (
                        f"{goal.get('firstName',{}).get('default','')} "
                        f"{goal.get('lastName',{}).get('default','')}".strip()
                    ),
                    "player_number": None,
                    "assists": [
                        f"{a.get('firstName',{}).get('default','')} {a.get('lastName',{}).get('default','')}".strip()
                        for a in goal.get("assists", [])
                    ],
                    "is_power_play": strength == "pp",
                    "is_short_handed": strength == "sh",
                    "is_empty_net": is_empty_net,
                })

        for period_data in summ.get("penalties", []):
            period_num = period_data.get("periodDescriptor", {}).get("number", 0)
            for pen in period_data.get("penalties", []):
                team_abbrev = pen.get("teamAbbrev", {}).get("default", "")
                player = pen.get("committedByPlayer", {})
                desc_key = pen.get("descKey", "")
                events.append({
                    "type": "penalty",
                    "period": period_num,
                    "time": pen.get("timeInPeriod", ""),
                    "team_abbrev": team_abbrev,
                    "is_tracked_team": team_abbrev == self.team_id,
                    "player_name": (
                        f"{player.get('firstName',{}).get('default','')} "
                        f"{player.get('lastName',{}).get('default','')}".strip()
                    ),
                    "player_number": player.get("sweaterNumber"),
                    "description": desc_key.replace("-", " ").title(),
                    "minutes": pen.get("duration"),
                })

        events.sort(key=_event_sort_key, reverse=True)
        return events

    def _nhl_normalize_recent(self, game: dict) -> dict[str, Any]:
        away = game.get("awayTeam", {})
        home = game.get("homeTeam", {})
        is_home = home.get("abbrev") == self.team_id
        opp = away if is_home else home
        team_score = int(home.get("score") or 0) if is_home else int(away.get("score") or 0)
        opp_score = int(away.get("score") or 0) if is_home else int(home.get("score") or 0)
        game_id = game.get("id")
        return {
            "date": game.get("startTimeUTC") or game.get("gameDate"),
            "opponent": self._nhl_full_name(opp),
            "opponent_logo_url": self._nhl_logo(opp.get("abbrev"), opp.get("logo")),
            "team_score": team_score,
            "opponent_score": opp_score,
            "is_home": is_home,
            "venue": game.get("venue", {}).get("default"),
            "win": team_score > opp_score,
            "game_url": NHL_GAME_URL.format(game_id=game_id) if game_id else None,
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
    # Notifications
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_targets(val: Any) -> list[str]:
        """Parse notification targets from either list (new) or comma-string (legacy) format."""
        if isinstance(val, list):
            return [s for s in val if s]
        if isinstance(val, str):
            return [s.strip() for s in val.split(",") if s.strip()]
        return []

    async def _maybe_notify(self, data: dict) -> None:
        """Fire HA notify services for win, pre-game, and goal events."""
        opts = self._notif_opts()
        state = data.get("game_state")
        game_id = str(data.get("game_id") or "")
        is_home = data.get("is_home")
        our_score = data.get("home_score") if is_home else data.get("away_score")
        opp_score = data.get("away_score") if is_home else data.get("home_score")
        our_team = data.get("home_team") if is_home else data.get("away_team")
        opp_team = data.get("away_team") if is_home else data.get("home_team")

        # Pre-game notification: fire once per game_id when within 35 min of puck drop
        if opts.get(CONF_NOTIFY_PREGAME_ENABLED) and game_id and game_id != self._notif_pregame_sent_id:
            targets = self._parse_targets(opts.get(CONF_NOTIFY_PREGAME_TARGETS, []))
            if targets and state == GAME_STATE_PRE and data.get("start_time"):
                try:
                    start = datetime.fromisoformat(data["start_time"].replace("Z", "+00:00"))
                    mins_until = (start - datetime.now(timezone.utc)).total_seconds() / 60
                    if 0 <= mins_until <= 35:
                        msg = f"{our_team} vs {opp_team} starts in {int(mins_until)} minutes!"
                        await self._send_notifications(targets, "Hockey: Game Starting Soon", msg)
                        self._notif_pregame_sent_id = game_id
                except (ValueError, TypeError):
                    pass

        # Goal notification: fire for each new tracked-team goal during live play
        if opts.get(CONF_NOTIFY_GOAL_ENABLED) and state == GAME_STATE_LIVE and game_id:
            targets = self._parse_targets(opts.get(CONF_NOTIFY_GOAL_TARGETS, []))
            if targets:
                if game_id != self._notif_goal_game_id:
                    self._notif_goal_game_id = game_id
                    self._notif_goal_count = 0
                our_goals = [
                    e for e in data.get("game_events", [])
                    if e.get("type") == "goal" and e.get("is_tracked_team")
                ]
                if len(our_goals) > self._notif_goal_count:
                    # New goals are at the front of the newest-first sorted list
                    new_goals = our_goals[:len(our_goals) - self._notif_goal_count]
                    for goal in new_goals:
                        scorer = goal.get("player_name", "")
                        period = goal.get("period", "")
                        time_str = goal.get("time", "")
                        tag = (
                            " (PP)" if goal.get("is_power_play")
                            else " (SH)" if goal.get("is_short_handed")
                            else " (EN)" if goal.get("is_empty_net")
                            else ""
                        )
                        score_str = f"{our_score}–{opp_score}" if our_score is not None else ""
                        msg = f"{scorer}{tag} — P{period} {time_str}" + (f" | {score_str}" if score_str else "")
                        await self._send_notifications(targets, f"GOAL! {our_team} scores!", msg)
                    self._notif_goal_count = len(our_goals)

        # Win notification: fire once when FINAL and tracked team won.
        # Require game to have started within 12 hours so a stale FINAL game
        # doesn't re-trigger after an integration reload (which resets _notif_win_sent_id).
        if opts.get(CONF_NOTIFY_WIN_ENABLED) and game_id and game_id != self._notif_win_sent_id:
            targets = self._parse_targets(opts.get(CONF_NOTIFY_WIN_TARGETS, []))
            if targets and state == GAME_STATE_FINAL:
                game_is_recent = False
                start_time = data.get("start_time")
                if start_time:
                    try:
                        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                        game_is_recent = (datetime.now(timezone.utc) - start).total_seconds() < 43200
                    except (ValueError, TypeError):
                        pass
                if game_is_recent and our_score is not None and opp_score is not None and our_score > opp_score:
                    msg = f"Final: {our_team} {our_score}, {opp_team} {opp_score}. {our_team} wins!"
                    await self._send_notifications(targets, f"{our_team} Wins!", msg)
                    self._notif_win_sent_id = game_id

    async def _send_notifications(self, targets: list[str], title: str, message: str) -> None:
        for target in targets:
            try:
                parts = target.rsplit(".", 1)
                domain = parts[0] if len(parts) == 2 else "notify"
                service_name = parts[1] if len(parts) == 2 else parts[0]
                await self.hass.services.async_call(
                    domain, service_name,
                    {"title": title, "message": message},
                    blocking=False,
                )
            except Exception as err:
                _LOGGER.warning("Failed to send notification to %s: %s", target, err)

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
            "team_logo_url": self._my_logo(),
            "venue": None,
            "game_events": [],
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
