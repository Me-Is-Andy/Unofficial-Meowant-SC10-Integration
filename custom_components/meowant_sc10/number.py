"""Number entities for the Meowant SC10."""
from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MeowantEntity
from .const import DOMAIN, DP_MAPPING


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        MeowantNumber(coordinator, dp_id, dp_info)
        for dp_id, dp_info in DP_MAPPING.items()
        if dp_info["platform"] == "number"
    )


class MeowantNumber(MeowantEntity, NumberEntity):
    """A writable integer datapoint."""

    _attr_native_step = 1

    def __init__(self, coordinator, dp_id: int, dp_info: dict):
        super().__init__(coordinator, dp_id, dp_info)
        self._attr_native_min_value = dp_info.get("min", 0)
        self._attr_native_max_value = dp_info.get("max", 999)
        if dp_info.get("unit"):
            self._attr_native_unit_of_measurement = dp_info["unit"]

    @property
    def native_value(self) -> float | None:
        value = self.dp_value
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_send(self.dp_info["code"], int(value))