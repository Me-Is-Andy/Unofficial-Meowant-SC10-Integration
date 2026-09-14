"""Binary sensors for the Meowant SC10."""
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MeowantBaseEntity
from .const import BIN_FULL_BIT, BIN_FULL_STATUS, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([MeowantBinFull(coordinator), MeowantConnectivity(coordinator)])


class MeowantBinFull(MeowantBaseEntity, BinarySensorEntity):
    """True when the device reports a full waste bin.

    The SC10 has no continuous fill-level datapoint, only this binary alert,
    surfaced on either the status enum (DP 24) or the notification bitmap (DP 21).
    """

    _attr_name = "Waste Bin Full"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:delete-alert"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("bin_full")

    @property
    def available(self) -> bool:
        if self.coordinator.device_online is False:
            return False
        return super().available

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        if not data:
            return None
        if str(data.get(24)) == BIN_FULL_STATUS:
            return True
        notification = data.get(21)
        if isinstance(notification, int):
            return bool(notification & BIN_FULL_BIT)
        return False


class MeowantConnectivity(MeowantBaseEntity, BinarySensorEntity):
    """Whether the Tuya cloud currently considers the device online.

    This is the only honest signal of the device being reachable: the status
    endpoint keeps serving cached values after it is unplugged.
    """

    _attr_name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("connectivity")

    @property
    def available(self) -> bool:
        # Reports on the connection itself, so it must never go unavailable
        # with the rest of the entities.
        return self.coordinator.device_online is not None

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.device_online