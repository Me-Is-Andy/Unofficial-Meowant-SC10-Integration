"""One-shot action buttons for the Meowant SC10."""
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import MeowantBaseEntity, MeowantEntity
from .const import (
    BUTTONS,
    CONF_DEODORIZER_REFERENCE_DATE,
    DEODORIZER_CONFIRM_PHRASE,
    DEODORIZER_CONFIRM_TIMEOUT_SECONDS,
    DEODORIZER_REFERENCE_UPDATED_SIGNAL,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities = [
        MeowantButton(coordinator, key, button)
        for key, button in BUTTONS.items()
    ]
    entities.append(MeowantDeodorizerResetButton(coordinator, config_entry))
    async_add_entities(entities)


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


class MeowantDeodorizerResetButton(MeowantBaseEntity, ButtonEntity):
    """Reset deodorizer percentage to 100% when cartridge is replaced."""

    _attr_name = "Reset Deodorizer"
    _attr_icon = "mdi:spray-bottle"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = coordinator.uid("deodorizer_reset")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        if not self.coordinator.consume_deodorizer_confirmation():
            raise HomeAssistantError(
                f'Type {DEODORIZER_CONFIRM_PHRASE} into the confirmation box on this device, '
                f"then press this button within {DEODORIZER_CONFIRM_TIMEOUT_SECONDS} seconds."
            )
        # Reset the countdown baseline to today, at 100%.
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data={
                **self._config_entry.data,
                CONF_DEODORIZER_REFERENCE_DATE: dt_util.now().date().isoformat(),
            },
        )
        async_dispatcher_send(self.hass, DEODORIZER_REFERENCE_UPDATED_SIGNAL)