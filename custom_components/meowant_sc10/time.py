"""Time entities for the Meowant SC10.

The device stores these datapoints as minutes since midnight; this platform
presents them as clock times and converts in both directions.
"""
import logging
from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MeowantEntity
from .const import DOMAIN, DP_MAPPING, TIME_MAX_MINUTES, TIME_STEP_MINUTES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        MeowantTime(coordinator, dp_id, dp_info)
        for dp_id, dp_info in DP_MAPPING.items()
        if dp_info["platform"] == "time"
    )


class MeowantTime(MeowantEntity, TimeEntity):
    """A minutes-since-midnight datapoint shown as a clock time."""

    @property
    def native_value(self) -> dt_time | None:
        value = self.dp_value
        if value is None:
            return None
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            _LOGGER.warning("DP %s returned a non-numeric time: %r", self.dp_id, value)
            return None
        minutes = max(0, min(minutes, TIME_MAX_MINUTES))
        return dt_time(hour=minutes // 60, minute=minutes % 60)

    async def async_set_value(self, value: dt_time) -> None:
        minutes = value.hour * 60 + value.minute
        # The device only accepts 5-minute increments, so snap to the nearest one.
        minutes = round(minutes / TIME_STEP_MINUTES) * TIME_STEP_MINUTES
        minutes = max(0, min(minutes, TIME_MAX_MINUTES))
        await self.coordinator.async_send(self.dp_info["code"], minutes)
