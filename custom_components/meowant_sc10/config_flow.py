"""Config flow for the Meowant SC10 integration."""
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import TuyaApiError, TuyaCloudApi
from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DATA_CENTER,
    CONF_DEVICE_ID,
    DATA_CENTERS,
    DEFAULT_DATA_CENTER,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): str,
        vol.Required(CONF_ACCESS_ID): str,
        vol.Required(CONF_ACCESS_SECRET): str,
        vol.Required(CONF_DATA_CENTER, default=DEFAULT_DATA_CENTER): SelectSelector(
            SelectSelectorConfig(
                options=sorted(DATA_CENTERS),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="data_center",
            )
        ),
    }
)


class MeowantConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Meowant SC10 config flow."""

    VERSION = 3

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID].strip()

            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            api = TuyaCloudApi(
                session,
                DATA_CENTERS[user_input[CONF_DATA_CENTER]],
                device_id,
                user_input[CONF_ACCESS_ID].strip(),
                user_input[CONF_ACCESS_SECRET].strip(),
            )

            try:
                await api.async_fetch_token()
            except TuyaApiError as err:
                _LOGGER.error("Authentication failed: %s", err)
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error reaching Tuya")
                errors["base"] = "cannot_connect"
            else:
                try:
                    properties = await api.async_get_properties()
                except TuyaApiError as err:
                    _LOGGER.error("Could not read device %s: %s", device_id, err)
                    errors["base"] = "unknown_device"
                except Exception:
                    _LOGGER.exception("Unexpected error reading the device")
                    errors["base"] = "cannot_connect"
                else:
                    if not properties:
                        errors["base"] = "unknown_device"
                    else:
                        return self.async_create_entry(
                            title="Meowant SC10",
                            data={**user_input, CONF_DEVICE_ID: device_id},
                        )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        """Re-prompt for credentials when the API rejects them."""
        return await self.async_step_user()