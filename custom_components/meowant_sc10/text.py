"""Typed confirmation challenge for destructive Meowant SC10 actions.

This entity holds a phrase locally; nothing is sent to the device. The guarded
button reads it, and it expires so it can never sit armed indefinitely.
"""
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MeowantBaseEntity
from .const import (
    CONFIRM_MAX_LENGTH,
    CONFIRM_PHRASE,
    CONFIRMATION_SIGNAL,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([MeowantConfirmationText(coordinator)])


class MeowantConfirmationText(MeowantBaseEntity, TextEntity):
    """Holds the typed challenge phrase."""

    _attr_name = f'Type "{CONFIRM_PHRASE}" to Confirm Empty Cycle'
    _attr_icon = "mdi:shield-key-outline"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = CONFIRM_MAX_LENGTH

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("confirmation")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, CONFIRMATION_SIGNAL, self._handle_confirmation_change
            )
        )

    @callback
    def _handle_confirmation_change(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Purely local state: stays usable through a failed poll or an offline
        # device, so the phrase can be typed before the device is reachable.
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.confirmation

    async def async_set_value(self, value: str) -> None:
        self.coordinator.set_confirmation(value)