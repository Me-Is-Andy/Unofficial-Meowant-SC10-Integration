"""One-shot action buttons for the Meowant SC10."""
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MeowantEntity
from .const import BUTTONS, CONFIRM_PHRASE, CONFIRM_TIMEOUT_SECONDS, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        MeowantButton(coordinator, key, button)
        for key, button in BUTTONS.items()
    )


class MeowantButton(MeowantEntity, ButtonEntity):
    """Fires a fixed value at a datapoint when pressed."""

    def __init__(self, coordinator, key: str, button: dict):
        super().__init__(coordinator, button["dp_id"], button)
        self._value = button["value"]
        self._requires_confirmation = button.get("confirm", False)
        # Override the datapoint-derived unique_id: several buttons share one
        # datapoint, and it is also shared with that datapoint's sensor.
        self._attr_unique_id = coordinator.uid(f"button_{key}")

    async def async_press(self) -> None:
        if self._requires_confirmation and not self.coordinator.consume_confirmation():
            raise HomeAssistantError(
                f'Type {CONFIRM_PHRASE} into the confirmation box on this device, '
                f"then press this button within {CONFIRM_TIMEOUT_SECONDS} seconds."
            )
        await self.coordinator.async_send(self.dp_info["code"], self._value)