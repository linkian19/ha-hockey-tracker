"""Config flow for ECHL Tracker."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant

from .const import CLIENT_CODE, CONF_API_KEY, CONF_TEAM_ID, CONF_TEAM_NAME, DOMAIN, HOCKEYTECH_BASE

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
    }
)


async def _fetch_teams(api_key: str) -> list[dict]:
    params = {
        "feed": "modulekit",
        "view": "teams",
        "key": api_key,
        "client_code": CLIENT_CODE,
        "lang_code": "en",
        "fmt": "json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(HOCKEYTECH_BASE, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    return data.get("SiteKit", {}).get("Teams", [])


class EchlTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ECHL Tracker."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_key: str = ""
        self._teams: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._api_key = user_input[CONF_API_KEY]
            try:
                self._teams = await _fetch_teams(self._api_key)
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
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={
                "docs_url": "https://github.com/IanStanek/ha-echl-tracker#readme"
            },
        )

    async def async_step_team(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        team_options = {
            str(t.get("id")): f"{t.get('city', '')} {t.get('nickname', '')}".strip()
            for t in self._teams
        }

        if user_input is not None:
            team_id = user_input[CONF_TEAM_ID]
            team_name = team_options.get(team_id, "ECHL Team")
            await self.async_set_unique_id(f"echl_{team_id}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=team_name,
                data={
                    CONF_API_KEY: self._api_key,
                    CONF_TEAM_ID: team_id,
                    CONF_TEAM_NAME: team_name,
                },
            )

        return self.async_show_form(
            step_id="team",
            data_schema=vol.Schema(
                {vol.Required(CONF_TEAM_ID): vol.In(team_options)}
            ),
            errors=errors,
        )
