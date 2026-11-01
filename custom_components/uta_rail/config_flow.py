"""Config flow for UTA Rail. The public feeds need no settings."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from . import DOMAIN


class UtaRailConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single config entry."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Confirm setup."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
        return self.async_create_entry(title="UTA Rail", data={})
