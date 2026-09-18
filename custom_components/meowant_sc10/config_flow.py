"""Config flow for the Meowant SC10 integration.

Cloud credentials are always required: even in local mode they are how the
local key is obtained, and re-pairing the device invalidates that key, so
keeping them lets the integration recover without the user hunting for it.
"""
import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import TuyaApiError, TuyaCloudApi
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DATA_CENTER,
    CONF_DEODORIZER_PERCENTAGE,
    CONF_DEODORIZER_REFERENCE_DATE,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_MODE,
    CONF_PROTOCOL_VERSION,
    DATA_CENTERS,
    DEFAULT_DATA_CENTER,
    DEFAULT_MODE,
    DEFAULT_PROTOCOL_VERSION,
    DEODORIZER_REFERENCE_UPDATED_SIGNAL,
    DOMAIN,
    MODE_LOCAL,
    MODES,
    PROTOCOL_VERSIONS,
    SUPPORTED_PRODUCT_IDS,
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


def _is_supported_device(device: dict) -> bool:
    """True if this device's product id matches the MW-SC10.

    The Tuya device-list endpoint has been observed returning the product id
    under either camelCase or snake_case, so both are checked.
    """
    product_id = device.get("productId") or device.get("product_id")
    return product_id in SUPPORTED_PRODUCT_IDS


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
        self._deodorizer_data: dict = {}

    @staticmethod
    def async_get_options_flow(config_entry):
        return MeowantOptionsFlow(config_entry)

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

            # Authentication and the device list are checked separately so a
            # failure in the second is not reported as bad credentials.
            try:
                await api.async_fetch_token()
            except TuyaApiError as err:
                _LOGGER.error("Tuya rejected the credentials: %s", err)
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error reaching Tuya")
                errors["base"] = "cannot_connect"
            else:
                try:
                    devices = await api.async_list_devices()
                except TuyaApiError as err:
                    _LOGGER.error("Could not list devices: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error listing devices")
                    errors["base"] = "cannot_connect"
                else:
                    supported = [d for d in devices if _is_supported_device(d)]
                    if not devices:
                        errors["base"] = "no_devices"
                    elif not supported:
                        errors["base"] = "no_supported_devices"
                    else:
                        self._credentials = {
                            CONF_ACCESS_ID: user_input[CONF_ACCESS_ID].strip(),
                            CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET].strip(),
                            CONF_DATA_CENTER: user_input[CONF_DATA_CENTER],
                            CONF_MODE: user_input[CONF_MODE],
                        }
                        self._devices = supported
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

            return await self.async_step_deodorizer()

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

    async def async_step_deodorizer(self, user_input=None):
        """Optionally set the current deodorizer cartridge percentage.

        This is converted immediately into a reference date (today minus the
        days that percentage implies) so the sensor never has to touch this
        raw value again, and never depends on the device's own activation
        time.
        """
        if user_input is not None:
            pct = user_input.get(CONF_DEODORIZER_PERCENTAGE)
            today = dt_util.now().date()
            if pct is not None:
                days_ago = round((100 - pct) / 4)
                reference_date = today - timedelta(days=days_ago)
            else:
                # Nothing entered: assume a fresh cartridge as of today.
                reference_date = today
            extra = {
                CONF_DEODORIZER_REFERENCE_DATE: reference_date.isoformat(),
            }

            if self._credentials[CONF_MODE] == MODE_LOCAL:
                # Store for local step to use
                self._deodorizer_data = extra
                return await self.async_step_local()
            return self._create_entry(extra)

        return self.async_show_form(
            step_id="deodorizer",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEODORIZER_PERCENTAGE): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=100,
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
            last_step=False,
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
                extra = {
                    CONF_HOST: host,
                    CONF_LOCAL_KEY: local_key,
                    CONF_PROTOCOL_VERSION: version,
                    **self._deodorizer_data,
                }
                return self._create_entry(extra)
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


class MeowantOptionsFlow(config_entries.OptionsFlow):
    """Lets the deodorizer baseline be recalibrated without deleting the entry."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            pct = user_input.get(CONF_DEODORIZER_PERCENTAGE)
            today = dt_util.now().date()
            if pct is not None:
                days_ago = round((100 - pct) / 4)
                reference_date = today - timedelta(days=days_ago)
            else:
                reference_date = today
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    CONF_DEODORIZER_REFERENCE_DATE: reference_date.isoformat(),
                },
            )
            async_dispatcher_send(self.hass, DEODORIZER_REFERENCE_UPDATED_SIGNAL)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEODORIZER_PERCENTAGE): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=100,
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )