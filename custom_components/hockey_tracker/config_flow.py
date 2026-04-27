"""Config flow for Hockey Tracker."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_API_KEY,
    CONF_LEAGUE,
    CONF_NOTIFY_GOAL_ENABLED,
    CONF_NOTIFY_GOAL_TARGETS,
    CONF_NOTIFY_PREGAME_ENABLED,
    CONF_NOTIFY_PREGAME_TARGETS,
    CONF_NOTIFY_WIN_ENABLED,
    CONF_NOTIFY_WIN_TARGETS,
    CONF_TEAM_ID,
    CONF_TEAM_NAME,
    DOMAIN,
    HOCKEYTECH_BASE,
    HOCKEYTECH_LEAGUES,
    LEAGUE_AHL,
    LEAGUE_ECHL,
    LEAGUE_NHL,
    NHL_API_BASE,
)

_LOGGER = logging.getLogger(__name__)

LEAGUE_OPTIONS = [LEAGUE_ECHL, LEAGUE_AHL, LEAGUE_NHL]


async def _fetch_ht_teams(api_key: str, client_code: str) -> list[dict]:
    """Validate the HockeyTech API key and return the team list."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            HOCKEYTECH_BASE,
            params={
                "feed": "modulekit", "view": "scorebar",
                "key": api_key, "client_code": client_code,
                "lang_code": "en", "fmt": "json",
                "numberofdaysahead": "1", "numberofdaysback": "1",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            if "SiteKit" not in data:
                raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=401)

        async with session.get(
            HOCKEYTECH_BASE,
            params={
                "feed": "modulekit", "view": "teamsbyseason",
                "key": api_key, "client_code": client_code,
                "lang_code": "en", "fmt": "json",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            teams_data = await resp.json(content_type=None)

    site_kit = teams_data.get("SiteKit", {})
    return site_kit.get("Teamsbyseason") or site_kit.get("Teams") or site_kit.get("teams") or []


async def _fetch_nhl_teams() -> list[dict]:
    """Fetch NHL teams from the standings endpoint."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{NHL_API_BASE}/standings/now",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    teams = []
    for entry in data.get("standings", []):
        abbrev = entry.get("teamAbbrev", {}).get("default", "")
        city = entry.get("placeName", {}).get("default", "")
        nickname = entry.get("teamCommonName", {}).get("default", "")
        logo = entry.get("teamLogo", "")
        if abbrev:
            teams.append({
                "id": abbrev,
                "city": city,
                "nickname": nickname,
                "team_logo_url": logo,
            })
    return sorted(teams, key=lambda t: t.get("city", ""))


class HockeyTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hockey Tracker."""

    VERSION = 1

    def __init__(self) -> None:
        self._league: str = ""
        self._api_key: str = ""
        self._teams: list[dict] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HockeyTrackerOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._league = user_input[CONF_LEAGUE]
            if self._league == LEAGUE_NHL:
                try:
                    self._teams = await _fetch_nhl_teams()
                    if not self._teams:
                        errors["base"] = "no_teams"
                    else:
                        return await self.async_step_team()
                except aiohttp.ClientError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error fetching NHL teams")
                    errors["base"] = "unknown"
            else:
                return await self.async_step_api_key()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_LEAGUE): vol.In(LEAGUE_OPTIONS)}
            ),
            errors=errors,
        )

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        league_cfg = HOCKEYTECH_LEAGUES[self._league]

        if user_input is not None:
            self._api_key = user_input[CONF_API_KEY]
            try:
                self._teams = await _fetch_ht_teams(self._api_key, league_cfg["client_code"])
                if not self._teams:
                    errors["base"] = "no_teams"
                else:
                    return await self.async_step_team()
            except aiohttp.ClientResponseError as err:
                _LOGGER.error("API key validation failed: %s", err)
                errors["base"] = "invalid_auth"
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during API key validation")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="api_key",
            data_schema=vol.Schema(
                {vol.Required(CONF_API_KEY, default=league_cfg["default_api_key"]): str}
            ),
            errors=errors,
            description_placeholders={
                "league": self._league,
                "docs_url": "https://github.com/linkian19/ha-hockey-tracker#readme",
            },
        )

    async def async_step_team(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        team_options = {
            str(t.get("id")): f"{t.get('city', '')} {t.get('nickname', '')}".strip()
            for t in self._teams
            if t.get("id")
        }

        if user_input is not None:
            team_id = user_input[CONF_TEAM_ID]
            team_name = team_options.get(team_id, team_id)
            team_obj = next((t for t in self._teams if str(t.get("id")) == team_id), {})

            await self.async_set_unique_id(f"hockey_{self._league}_{team_id}")
            self._abort_if_unique_id_configured()

            entry_data: dict[str, Any] = {
                CONF_LEAGUE: self._league,
                CONF_TEAM_ID: team_id,
                CONF_TEAM_NAME: team_name,
                "team_logo_url": team_obj.get("team_logo_url", ""),
            }
            if self._league != LEAGUE_NHL:
                entry_data[CONF_API_KEY] = self._api_key

            return self.async_create_entry(title=team_name, data=entry_data)

        return self.async_show_form(
            step_id="team",
            data_schema=vol.Schema({vol.Required(CONF_TEAM_ID): vol.In(team_options)}),
            errors=errors,
        )


class HockeyTrackerOptionsFlow(OptionsFlow):
    """Options flow for configuring Hockey Tracker notifications."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self._entry.options

        # Build options list from all registered notify services so the user
        # can pick from a multi-select rather than typing service names manually.
        notify_options = sorted(
            f"notify.{name}"
            for name in self.hass.services.async_services().get("notify", {})
        )

        def _targets_default(key: str) -> list[str]:
            """Return current targets as a list, migrating legacy comma-string if needed."""
            val = opts.get(key, [])
            if isinstance(val, str):
                return [s.strip() for s in val.split(",") if s.strip()]
            return list(val)

        def _target_sel() -> SelectSelector:
            return SelectSelector(SelectSelectorConfig(
                options=notify_options, multiple=True, mode=SelectSelectorMode.DROPDOWN
            ))

        schema = vol.Schema({
            vol.Optional(
                CONF_NOTIFY_WIN_ENABLED,
                default=opts.get(CONF_NOTIFY_WIN_ENABLED, False),
            ): BooleanSelector(),
            vol.Optional(
                CONF_NOTIFY_WIN_TARGETS,
                default=_targets_default(CONF_NOTIFY_WIN_TARGETS),
            ): _target_sel(),
            vol.Optional(
                CONF_NOTIFY_PREGAME_ENABLED,
                default=opts.get(CONF_NOTIFY_PREGAME_ENABLED, False),
            ): BooleanSelector(),
            vol.Optional(
                CONF_NOTIFY_PREGAME_TARGETS,
                default=_targets_default(CONF_NOTIFY_PREGAME_TARGETS),
            ): _target_sel(),
            vol.Optional(
                CONF_NOTIFY_GOAL_ENABLED,
                default=opts.get(CONF_NOTIFY_GOAL_ENABLED, False),
            ): BooleanSelector(),
            vol.Optional(
                CONF_NOTIFY_GOAL_TARGETS,
                default=_targets_default(CONF_NOTIFY_GOAL_TARGETS),
            ): _target_sel(),
        })

        return self.async_show_form(step_id="init", data_schema=schema)
