"""Switch entities for the Meowant SC10."""
from homeassistant.components.switch import SwitchEntity
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
        MeowantSwitch(coordinator, dp_id, dp_info)
        for dp_id, dp_info in DP_MAPPING.items()
        if dp_info["platform"] == "switch"
    )


class MeowantSwitch(MeowantEntity, SwitchEntity):
    """A boolean datapoint."""

    @property
    def is_on(self) -> bool | None:
        value = self.dp_value
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_send(self.dp_info["code"], True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_send(self.dp_info["code"], False)
