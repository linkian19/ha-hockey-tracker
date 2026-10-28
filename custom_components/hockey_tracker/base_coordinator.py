"""Shared base for the Team Tracker and Playoff Tracker coordinators.

Holds the transport layer (HockeyTech / NHL HTTP fetches), date/logo helpers,
notification delivery and update error handling. Tracking logic — which game
is active, how it is normalized, which notifications fire — stays in the
subclasses because it genuinely differs (one team vs. a set of followed teams).
"""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_API_KEY,
    CONF_LEAGUE,
    DOMAIN,
    HOCKEYTECH_BASE,
    HOCKEYTECH_LEAGUES,
    NHL_API_BASE,
    SCAN_INTERVAL_IDLE,
)

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class HockeyBaseCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Common plumbing for both coordinator types."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self.league: str = entry.data[CONF_LEAGUE]
        # Logo cache keyed by team ID (HockeyTech) or abbreviation (NHL).
        # HockeyTech CDN paths include a version suffix that cannot be constructed
        # from team ID alone, so this is populated from live API responses.
        self._logo_cache: dict[str, str] = {}
        # HockeyTech credentials (unused for NHL)
        self._api_key: str = entry.data.get(CONF_API_KEY, "")
        self._client_code: str = (
            HOCKEYTECH_LEAGUES[self.league]["client_code"]
            if self.league in HOCKEYTECH_LEAGUES
            else ""
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_IDLE),
        )

    # ------------------------------------------------------------------
    # Update cycle
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._async_fetch()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Request failed: {err!r}") from err
        except ValueError as err:
            # Malformed/non-JSON API response (json.JSONDecodeError is a ValueError)
            raise UpdateFailed(f"Invalid API response: {err}") from err
        return await self._async_process(data)

    @abstractmethod
    async def _async_fetch(self) -> dict[str, Any]:
        """Fetch and normalize the current game data from the league API."""

    @abstractmethod
    async def _async_process(self, data: dict[str, Any]) -> dict[str, Any]:
        """Notifications, FINAL-window handling and polling interval for fresh data."""

    @abstractmethod
    def clear_schedule_cache(self) -> None:
        """Force the next update to re-fetch schedule data (force_refresh service)."""

    @abstractmethod
    def clear_final_window(self) -> None:
        """Expire the post-game FINAL window (force_refresh service)."""

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        async with session.get(url, timeout=_REQUEST_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _fetch_ht(self, params: dict[str, str]) -> dict[str, Any]:
        base = {
            "key": self._api_key,
            "client_code": self._client_code,
            "lang_code": "en",
            "fmt": "json",
        }
        base.update(params)
        session = async_get_clientsession(self.hass)
        async with session.get(HOCKEYTECH_BASE, params=base, timeout=_REQUEST_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _fetch_ht_game_summary(self, game_id: str) -> dict | None:
        """Fetch shot totals and play-by-play events for a HockeyTech game.

        The statviewfeed endpoint wraps its JSON in outer parentheses (JSONP
        style) regardless of fmt=json, so those are stripped before parsing.
        """
        try:
            session = async_get_clientsession(self.hass)
            params = {
                "feed": "statviewfeed",
                "view": "gameSummary",
                "game_id": game_id,
                "key": self._api_key,
                "client_code": self._client_code,
                "lang_code": "en",
                "fmt": "json",
            }
            async with session.get(HOCKEYTECH_BASE, params=params, timeout=_REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                text = (await resp.text()).strip()
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1]
                return json.loads(text)
        except Exception as err:  # noqa: BLE001 — summary is optional enrichment
            _LOGGER.debug("Game summary fetch failed for game %s: %s", game_id, err)
            return None

    async def _fetch_nhl_landing(self, game_id: int) -> dict | None:
        """Fetch the NHL gamecenter landing page (SOG, scoring and penalty events)."""
        try:
            return await self._fetch_json(f"{NHL_API_BASE}/gamecenter/{game_id}/landing")
        except Exception as err:  # noqa: BLE001 — optional enrichment
            _LOGGER.debug("NHL landing fetch failed for game %s: %s", game_id, err)
            return None

    async def _fetch_nhl_playbyplay(self, game_id: int) -> dict | None:
        try:
            return await self._fetch_json(f"{NHL_API_BASE}/gamecenter/{game_id}/play-by-play")
        except Exception as err:  # noqa: BLE001 — optional enrichment
            _LOGGER.debug("NHL play-by-play fetch failed for game %s: %s", game_id, err)
            return None

    # ------------------------------------------------------------------
    # Date / logo helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ht_parse_dt(game: dict) -> datetime:
        raw = game.get("GameDateISO8601", "")
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _nhl_parse_dt(game: dict) -> datetime:
        raw = game.get("startTimeUTC") or game.get("gameDate", "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _upscale_ht_logo(url: str | None) -> str | None:
        """Strip the size subdirectory from HockeyTech CDN URLs to get the full-res image."""
        if not url:
            return None
        return re.sub(r"/logos/\d+x\d+/", "/logos/", url)

    def _nhl_logo(self, abbrev: str | None, direct_url: str | None = None) -> str | None:
        """Best available NHL logo URL.

        Priority: direct URL from the API response → standings/scoreboard cache →
        constructed SVG URL. Always the _light.svg variant so logos are visible on
        light-themed dashboards.
        """
        url = direct_url or self._logo_cache.get(abbrev or "")
        if url:
            return url.replace("_dark.svg", "_light.svg")
        if not abbrev:
            return None
        return f"https://assets.nhle.com/logos/nhl/svg/{abbrev}_light.svg"

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _notif_opts(self) -> dict:
        # Read live so options changes apply without reloading the entry
        return self._entry.options

    @staticmethod
    def _parse_targets(val: Any) -> list[str]:
        """Parse notification targets from list (current) or comma-string (legacy) format."""
        if isinstance(val, list):
            return [s for s in val if s]
        if isinstance(val, str):
            return [s.strip() for s in val.split(",") if s.strip()]
        return []

    async def _send_notifications(
        self, targets: list[str], title: str, message: str, data: dict | None = None
    ) -> None:
        for target in targets:
            parts = target.rsplit(".", 1)
            domain = parts[0] if len(parts) == 2 else "notify"
            service_name = parts[1] if len(parts) == 2 else parts[0]
            payload = {"title": title, "message": message}
            attempts = [{**payload, "data": data}, payload] if data else [payload]
            last_err: Exception | None = None
            for attempt in attempts:
                try:
                    await self.hass.services.async_call(domain, service_name, attempt, blocking=False)
                    last_err = None
                    break
                except Exception as err:  # noqa: BLE001 — some notify services reject `data`; retry without
                    last_err = err
            if last_err is not None:
                _LOGGER.warning("Failed to send notification to %s: %s", target, last_err)
