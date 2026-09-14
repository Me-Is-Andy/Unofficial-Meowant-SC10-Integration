"""Config flow for the Meowant SC10 integration.

Cloud credentials are always required: even in local mode they are how the
local key is obtained, and re-pairing the device invalidates that key, so
keeping them lets the integration recover without the user hunting for it.
"""
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
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
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_MODE,
    CONF_PROTOCOL_VERSION,
    DATA_CENTERS,
    DEFAULT_DATA_CENTER,
    DEFAULT_MODE,
    DEFAULT_PROTOCOL_VERSION,
    DOMAIN,
    MODE_LOCAL,
    MODES,
    PROTOCOL_VERSIONS,
)

_LOGGER = logging.getLogger(__name__)

STEP_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_ID): str,
        vol.Required(CONF_ACCESS_SECRET): str,
        vol.Required(CONF_DATA_CENTER, default=DEFAULT_DATA_CENTER): SelectSelector(
            SelectSelectorConfig(
                options=sorted(DATA_CENTERS),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="data_center",
            )
        ),
        vol.Required(CONF_MODE, default=DEFAULT_MODE): SelectSelector(
            SelectSelectorConfig(
                options=MODES,
                mode=SelectSelectorMode.LIST,
                translation_key="mode",
            )
        ),
    }
)


async def _async_scan_for_host(hass: HomeAssistant, device_id: str) -> str | None:
    """Look for the device on the LAN so the user need not find its IP.

    Tuya devices broadcast their id every few seconds. This is best effort:
    Home Assistant may be on a different subnet, or the broadcast may be
    filtered, in which case the user types the address instead.
    """

    def _scan() -> str | None:
        try:
            import tinytuya
        except ImportError:
            _LOGGER.debug("tinytuya is not installed yet; skipping the LAN scan")
            return None
        try:
            found = tinytuya.deviceScan(False, 12)
        except Exception as err:  # noqa: BLE001 - discovery is optional
            _LOGGER.debug("LAN scan failed: %s", err)
            return None
        for address, info in (found or {}).items():
            if info.get("gwId") == device_id or info.get("id") == device_id:
                return address
        return None

    return await hass.async_add_executor_job(_scan)


class MeowantConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Meowant SC10 config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict = {}
        self._devices: list[dict] = []
        self._device: dict = {}

    async def async_step_user(self, user_input=None):
        """Collect cloud credentials and the choice of transport."""
        errors = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = TuyaCloudApi(
                session,
                DATA_CENTERS[user_input[CONF_DATA_CENTER]],
                "",
                user_input[CONF_ACCESS_ID].strip(),
                user_input[CONF_ACCESS_SECRET].strip(),
            )

            try:
                await api.async_fetch_token()
                devices = await api.async_list_devices()
            except TuyaApiError as err:
                _LOGGER.error("Tuya rejected the credentials: %s", err)
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error reaching Tuya")
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._credentials = {
                        CONF_ACCESS_ID: user_input[CONF_ACCESS_ID].strip(),
                        CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET].strip(),
                        CONF_DATA_CENTER: user_input[CONF_DATA_CENTER],
                        CONF_MODE: user_input[CONF_MODE],
                    }
                    self._devices = devices
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_CREDENTIALS_SCHEMA,
            errors=errors,
        )

    async def async_step_device(self, user_input=None):
        """Pick which device in the cloud project to set up."""
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]

            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()

            self._device = next(
                (d for d in self._devices if d.get("id") == device_id), {}
            )

            if self._credentials[CONF_MODE] == MODE_LOCAL:
                return await self.async_step_local()
            return self._create_entry()

        options = []
        for device in self._devices:
            # customName is whatever the owner typed in the vendor app and may
            # be blank; name is the manufacturer's label.
            label = device.get("customName") or device.get("name") or device.get("id")
            model = device.get("name") or device.get("productName") or ""
            if model and model != label:
                label = f"{label} ({model})"
            options.append(
                SelectOptionDict(value=device.get("id", ""), label=label)
            )

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    async def async_step_local(self, user_input=None):
        """Confirm the LAN address and protocol version for local control."""
        errors = {}
        device_id = self._device.get("id", "")

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            local_key = user_input[CONF_LOCAL_KEY].strip()
            version = user_input[CONF_PROTOCOL_VERSION]

            error = await self._async_test_local(device_id, host, local_key, version)
            if error:
                errors["base"] = error
            else:
                return self._create_entry(
                    {
                        CONF_HOST: host,
                        CONF_LOCAL_KEY: local_key,
                        CONF_PROTOCOL_VERSION: version,
                    }
                )
            suggested_host = host
        else:
            suggested_host = await _async_scan_for_host(self.hass, device_id) or ""

        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=suggested_host): str,
                    vol.Required(
                        CONF_LOCAL_KEY, default=self._device.get("localKey", "")
                    ): str,
                    vol.Required(
                        CONF_PROTOCOL_VERSION, default=DEFAULT_PROTOCOL_VERSION
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=PROTOCOL_VERSIONS,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def _async_test_local(
        self, device_id: str, host: str, local_key: str, version: str
    ) -> str | None:
        """Open a local connection and read status, returning an error key."""

        def _probe() -> str | None:
            try:
                import tinytuya
            except ImportError:
                return "tinytuya_missing"
            try:
                device = tinytuya.Device(
                    device_id, host, local_key, version=float(version)
                )
                device.set_socketTimeout(8)
                status = device.status()
                device.close()
            except Exception as err:  # noqa: BLE001 - reported to the user
                _LOGGER.debug("Local probe failed: %s", err)
                return "cannot_connect_local"

            if not isinstance(status, dict) or "dps" not in status:
                _LOGGER.debug("Local probe returned no datapoints: %s", status)
                return "cannot_connect_local"
            return None

        return await self.hass.async_add_executor_job(_probe)

    def _create_entry(self, extra: dict | None = None):
        device = self._device
        title = device.get("customName") or device.get("name") or "Meowant SC10"
        data = {
            **self._credentials,
            CONF_DEVICE_ID: device.get("id", ""),
            **(extra or {}),
        }
        return self.async_create_entry(title=title, data=data)

    async def async_step_reauth(self, entry_data):
        """Re-prompt for credentials when the API rejects them."""
        return await self.async_step_user()
