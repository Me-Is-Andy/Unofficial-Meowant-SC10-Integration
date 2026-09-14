"""Read-only sensors for the Meowant SC10."""
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import VISIT_SIGNAL, MeowantBaseEntity, MeowantEntity
from .const import (
    BITMAP_CLEAR_LABEL,
    BITMAP_LABELS,
    DOMAIN,
    DP_MAPPING,
    USE_MIN_DURATION_SECONDS,
    VALUE_LABELS,
)

# Rendered in the user's local timezone, e.g. "Sep 10, 2026 at 6:41 PM".
CLEAN_TIME_FORMAT = "%b %-d, %Y at %-I:%M %p"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities = [
        MeowantSensor(coordinator, dp_id, dp_info)
        for dp_id, dp_info in DP_MAPPING.items()
        if dp_info["platform"] == "sensor"
    ]
    entities.append(MeowantVisitsToday(coordinator))
    entities.append(MeowantUsesToday(coordinator))
    entities.append(MeowantLastCleanCompleted(coordinator))
    entities.append(MeowantLastCleanElapsed(coordinator))
    entities.append(MeowantConnectionType(coordinator))
    entities.append(MeowantUptime(coordinator))

    # The activation date comes from the cloud device record; the LAN protocol
    # has no equivalent, so the entity is only meaningful in cloud mode.
    if coordinator.transport == "cloud":
        entities.append(MeowantActivated(coordinator))

    async_add_entities(entities)


class MeowantSensor(MeowantEntity, SensorEntity):
    """A read-only datapoint, with enum and bitmap values made readable."""

    def __init__(self, coordinator, dp_id: int, dp_info: dict):
        super().__init__(coordinator, dp_id, dp_info)
        if dp_info.get("unit"):
            self._attr_native_unit_of_measurement = dp_info["unit"]
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        value = self.dp_value
        if value is None:
            return None

        if self.dp_id in BITMAP_LABELS:
            return self._decode_bitmap(value)

        labels = VALUE_LABELS.get(self.dp_id)
        if labels:
            # Fall back to a tidied version of the raw value so a firmware
            # value we haven't seen still reads sensibly.
            return labels.get(str(value), str(value).replace("_", " ").title())

        return value

    @property
    def extra_state_attributes(self):
        """Expose the untranslated value for automations and debugging."""
        if self.dp_id in BITMAP_LABELS or self.dp_id in VALUE_LABELS:
            return {"raw_value": self.dp_value}
        return None

    def _decode_bitmap(self, value) -> str:
        try:
            bits = int(value)
        except (TypeError, ValueError):
            return str(value)
        if bits == 0:
            return BITMAP_CLEAR_LABEL
        flags = [
            label
            for index, label in enumerate(BITMAP_LABELS[self.dp_id])
            if bits & (1 << index)
        ]
        return ", ".join(flags) if flags else BITMAP_CLEAR_LABEL


class MeowantDerivedSensor(MeowantBaseEntity, SensorEntity):
    """Base for sensors derived locally rather than read straight from a DP."""

    @property
    def available(self) -> bool:
        # Derived locally, so a lost connection doesn't blank the value.
        return True


class MeowantVisitCounter(MeowantDerivedSensor):
    """Base for the locally-derived daily visit counters."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "visits"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, VISIT_SIGNAL, self._handle_visit)
        )

    @callback
    def _handle_visit(self) -> None:
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self):
        return {
            "last_visit_duration_seconds": self.coordinator.last_visit_duration,
            "last_visit_weight": self.coordinator.last_visit_weight,
            "last_visit_at": self.coordinator.last_visit_at,
        }


class MeowantVisitsToday(MeowantVisitCounter):
    """Every completed visit, however brief."""

    _attr_name = "Visits Today"
    _attr_icon = "mdi:cat"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("visits_today")

    @property
    def native_value(self) -> int:
        return self.coordinator.visits_today


class MeowantUsesToday(MeowantVisitCounter):
    """Visits long enough to count as an actual use."""

    _attr_name = "Uses Today"
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("uses_today")

    @property
    def native_value(self) -> int:
        return self.coordinator.uses_today

    @property
    def extra_state_attributes(self):
        attributes = super().extra_state_attributes
        attributes["minimum_duration_seconds"] = USE_MIN_DURATION_SECONDS
        return attributes


class MeowantCleanTimeBase(MeowantDerivedSensor):
    """Shared access to the stored clean-completion timestamp."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:broom"

    @property
    def completed_at(self) -> datetime | None:
        stored = self.coordinator.last_clean_completed
        if not stored:
            return None
        return dt_util.parse_datetime(stored)


class MeowantLastCleanCompleted(MeowantCleanTimeBase):
    """The clock time of the last completed clean cycle."""

    _attr_name = "Last Clean Time"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("last_clean_time")

    @property
    def native_value(self) -> str | None:
        completed = self.completed_at
        if completed is None:
            return None
        return dt_util.as_local(completed).strftime(CLEAN_TIME_FORMAT)

    @property
    def extra_state_attributes(self):
        completed = self.completed_at
        return {"timestamp": completed.isoformat() if completed else None}


class MeowantLastCleanElapsed(MeowantCleanTimeBase):
    """How long ago the last clean cycle finished."""

    _attr_name = "Last Clean Elapsed"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("last_clean_completed")

    @property
    def native_value(self) -> datetime | None:
        return self.completed_at


class MeowantConnectionType(MeowantDerivedSensor):
    """Whether this device is being reached locally or through the cloud."""

    _attr_name = "Connection Type"
    _attr_icon = "mdi:transit-connection-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("connection_type")

    @property
    def native_value(self) -> str:
        return "Local" if self.coordinator.transport == "local" else "Cloud"

    @property
    def extra_state_attributes(self):
        if self.coordinator.transport == "local":
            return {"host": self.coordinator.host}
        return None


class MeowantActivated(MeowantDerivedSensor):
    """When the device was first paired, per Tuya's active_time.

    Despite the field's name this is the activation date, not the last
    connection: it does not move when the device is power cycled. Cloud mode
    only; the LAN protocol does not report it.
    """

    _attr_name = "Device Activated"
    _attr_icon = "mdi:calendar-check"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("last_connected")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.activated_at

    @property
    def extra_state_attributes(self):
        return {
            "active_time_raw": self.coordinator.active_time_raw,
            "online": self.coordinator.device_online,
        }


class MeowantUptime(MeowantDerivedSensor):
    """How long since the device was last seen restarting.

    Measured from the last restart this integration detected. In cloud mode it
    falls back to the activation date until one is seen; locally there is no
    such fallback, so it reads unknown until a restart happens.
    """

    _attr_name = "Uptime"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.uid("uptime")

    @property
    def native_value(self) -> float | None:
        since = self.coordinator.running_since
        if since is None:
            return None
        seconds = (dt_util.utcnow() - since).total_seconds()
        if seconds < 0:
            return None
        return round(seconds / 3600, 1)

    @property
    def extra_state_attributes(self):
        since = self.coordinator.running_since
        attributes = {
            "running_since": since.isoformat() if since else None,
            "estimated": self.coordinator.uptime_is_estimated,
        }
        if since is not None:
            seconds = int((dt_util.utcnow() - since).total_seconds())
            attributes["uptime_seconds"] = seconds
            attributes["uptime_days"] = round(seconds / 86400, 2)
        return attributes
