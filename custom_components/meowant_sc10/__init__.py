"""Meowant SC10 litter box integration."""
import asyncio
import base64
import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import TuyaApiError, TuyaCloudApi
from .const import (
    CLEAN_DONE_VALUE,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DATA_CENTER,
    CONF_DEVICE_ID,
    CONFIRM_PHRASE,
    CONFIRM_TIMEOUT_SECONDS,
    CONFIRMATION_SIGNAL,
    DATA_CENTERS,
    DEFAULT_DATA_CENTER,
    DOMAIN,
    DP_MAPPING,
    HISTORY_DP,
    RECONNECT_CHECK_EVERY,
    RESET_DETECTION_THRESHOLD,
    SCAN_INTERVAL,
    STORAGE_VERSION,
    USE_MIN_DURATION_SECONDS,
    VISIT_DP,
    storage_key,
)
from .frontend import async_register_card

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.TIME,
]

ENTITY_CATEGORIES = {
    "config": EntityCategory.CONFIG,
    "diagnostic": EntityCategory.DIAGNOSTIC,
}

VISIT_SIGNAL = f"{DOMAIN}_visits_updated"

# Transient DNS and network failures are common enough that blanking every
# entity on a single missed poll is worse than showing slightly stale values.
# Hold the last known data through this many consecutive failures.
FETCH_FAILURE_TOLERANCE = 3

CODE_TO_DP = {info["code"]: dp_id for dp_id, info in DP_MAPPING.items()}

# Writable settings the device forgets across a power cycle. Sensors and the
# one-shot cycle datapoints are excluded: replaying those would be wrong.
RESTORABLE_DPS = {
    dp_id
    for dp_id, info in DP_MAPPING.items()
    if info.get("category") == "config" and info["platform"] in ("switch", "number", "time")
}

# Anything past this is a millisecond timestamp rather than a second one.
_MS_THRESHOLD = 1_000_000_000_000


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Meowant SC10 from a config entry."""
    await async_register_card(hass)

    device_id = entry.data[CONF_DEVICE_ID]
    base_url = DATA_CENTERS.get(
        entry.data.get(CONF_DATA_CENTER, DEFAULT_DATA_CENTER),
        DATA_CENTERS[DEFAULT_DATA_CENTER],
    )

    session = async_get_clientsession(hass)
    api = TuyaCloudApi(
        session,
        base_url,
        device_id,
        entry.data[CONF_ACCESS_ID],
        entry.data[CONF_ACCESS_SECRET],
    )
    store = Store(hass, STORAGE_VERSION, storage_key(device_id))
    coordinator = MeowantCoordinator(hass, api, store, device_id)

    await coordinator.async_load_state()

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Could not reach the Meowant SC10: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def decode_visit_record(raw_value) -> tuple[int | None, int | None]:
    """Decode DP 102 into (duration_seconds, weight).

    The payload is four base64-encoded bytes: duration in the first two,
    what appears to be weight in the second two. Weight has always read zero
    on the reference device, so treat a zero there as "not reported".
    """
    if not raw_value:
        return None, None
    try:
        data = base64.b64decode(raw_value)
    except (ValueError, TypeError):
        _LOGGER.debug("Could not decode visit record %r", raw_value)
        return None, None
    if len(data) < 2:
        return None, None
    duration = int.from_bytes(data[0:2], "big")
    weight = int.from_bytes(data[2:4], "big") if len(data) >= 4 else None
    return duration, (weight or None)


class MeowantCoordinator(DataUpdateCoordinator):
    """Polls the Tuya cloud for the device's datapoint values."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: TuyaCloudApi,
        store: Store,
        device_id: str,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Meowant SC10",
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )
        self.api = api
        self.device_id = device_id
        self._store = store
        self._confirmation = ""
        self._confirmation_at = None
        self._consecutive_failures = 0
        # Visit tracking
        self._last_visit_ts = None
        self._visit_day = None
        self.visits_today = 0
        self.uses_today = 0
        self.last_visit_duration = None
        self.last_visit_weight = None
        self.last_visit_at = None
        # Clean cycle tracking
        self.last_clean_completed = None
        # Settings restore and connection tracking
        self._desired = {}
        self._previous_values = None
        self._active_time = None
        self._was_online = None
        self.last_reboot_at = None
        # Start at the threshold so the first poll checks straight away rather
        # than leaving the connection state unknown for the first few minutes.
        self._polls_since_check = RECONNECT_CHECK_EVERY
        self._restoring = False

    def uid(self, suffix: str) -> str:
        """Build a unique_id scoped to this device."""
        return f"{self.device_id}_{suffix}"

    @property
    def device_info(self) -> dict:
        """Shared device registry entry for every entity on this device."""
        return {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": "Meowant SC10",
            "manufacturer": "Meowant",
            "model": "SC10",
        }

    async def async_load_state(self) -> None:
        """Restore the counters and timestamps from disk."""
        stored = await self._store.async_load()
        if not stored:
            return
        self._last_visit_ts = stored.get("last_visit_ts")
        self._visit_day = stored.get("visit_day")
        self.visits_today = stored.get("visits_today", 0)
        self.uses_today = stored.get("uses_today", 0)
        self.last_visit_duration = stored.get("last_visit_duration")
        self.last_visit_weight = stored.get("last_visit_weight")
        self.last_visit_at = stored.get("last_visit_at")
        self.last_clean_completed = stored.get("last_clean_completed")
        # JSON object keys are strings; datapoint ids are ints.
        self._desired = {int(k): v for k, v in (stored.get("desired") or {}).items()}
        self._active_time = stored.get("active_time")
        self.last_reboot_at = stored.get("last_reboot_at")

    @callback
    def _save_state(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "last_visit_ts": self._last_visit_ts,
                "visit_day": self._visit_day,
                "visits_today": self.visits_today,
                "uses_today": self.uses_today,
                "last_visit_duration": self.last_visit_duration,
                "last_visit_weight": self.last_visit_weight,
                "last_visit_at": self.last_visit_at,
                "last_clean_completed": self.last_clean_completed,
                "desired": {str(k): v for k, v in self._desired.items()},
                "active_time": self._active_time,
                "last_reboot_at": self.last_reboot_at,
            },
            5,
        )

    @property
    def active_time_raw(self):
        """Tuya's active_time exactly as reported, for diagnostics."""
        return self._active_time

    @property
    def device_online(self):
        """The device's last reported online state, or None if unknown."""
        return self._was_online

    @property
    def activated_at(self) -> datetime | None:
        """When the device was first paired, per Tuya's active_time.

        This does NOT change when the device is power cycled, which is why
        reboots are detected from the datapoints instead.
        """
        if self._active_time is None:
            return None
        try:
            stamp = float(self._active_time)
        except (TypeError, ValueError):
            return None
        if stamp > _MS_THRESHOLD:
            stamp = stamp / 1000
        return dt_util.utc_from_timestamp(stamp)

    @property
    def running_since(self) -> datetime | None:
        """Best estimate of when the device last started up."""
        if self.last_reboot_at:
            return dt_util.parse_datetime(self.last_reboot_at)
        return self.activated_at

    @property
    def uptime_is_estimated(self) -> bool:
        """True while we are falling back to the activation timestamp."""
        return not self.last_reboot_at

    async def _async_update_data(self) -> dict:
        try:
            raw = await self.api.async_get_properties()
        except Exception as err:
            return self._handle_fetch_failure(err)

        self._consecutive_failures = 0
        self._track_visits(raw)
        self._track_clean(raw)

        values = {dp_id: prop["value"] for dp_id, prop in raw.items()}
        self._track_settings(values)
        await self._check_online(values)
        return values

    def _handle_fetch_failure(self, err: Exception) -> dict:
        """Ride out a short outage rather than blanking every entity."""
        self._consecutive_failures += 1

        if self.data and self._consecutive_failures <= FETCH_FAILURE_TOLERANCE:
            _LOGGER.debug(
                "Poll %s of %s failed, keeping last known values: %s",
                self._consecutive_failures,
                FETCH_FAILURE_TOLERANCE,
                err,
            )
            return self.data

        if isinstance(err, TuyaApiError):
            raise UpdateFailed(str(err)) from err
        raise UpdateFailed(f"Unexpected error talking to Tuya: {err}") from err

    @callback
    def _track_settings(self, values: dict) -> None:
        """Follow setting changes, and spot the mass reset a reboot causes.

        One setting changing is someone using the vendor app, so adopt it as
        the new desired value. Several changing at once is the device coming
        back up with its defaults, so put the saved values back instead.
        """
        current = {dp_id: values[dp_id] for dp_id in RESTORABLE_DPS if dp_id in values}

        if not current:
            return

        if self._previous_values is None:
            self._previous_values = current
            if not self._desired:
                self._desired = dict(current)
                _LOGGER.debug("Recorded initial settings for restore: %s", current)
                self._save_state()
            return

        if self._restoring:
            # Our own commands are landing; don't read them as user intent.
            self._previous_values = current
            return

        changed = {
            dp_id: value
            for dp_id, value in current.items()
            if dp_id in self._previous_values and self._previous_values[dp_id] != value
        }
        self._previous_values = current

        if not changed:
            return

        if len(changed) >= RESET_DETECTION_THRESHOLD:
            _LOGGER.warning(
                "%s settings changed at once (%s) — treating this as a device "
                "restart and restoring saved values",
                len(changed),
                ", ".join(DP_MAPPING[dp_id]["code"] for dp_id in sorted(changed)),
            )
            self.last_reboot_at = dt_util.utcnow().isoformat()
            self._save_state()
            self.hass.async_create_task(self._restore_settings(dict(current)))
            return

        for dp_id, value in changed.items():
            _LOGGER.debug(
                "%s changed outside Home Assistant to %r; adopting it",
                DP_MAPPING[dp_id]["code"],
                value,
            )
            self._desired[dp_id] = value
        self._save_state()

    @callback
    def record_desired(self, code: str, value) -> None:
        """Remember a setting the user changed through Home Assistant."""
        dp_id = CODE_TO_DP.get(code)
        if dp_id is None or dp_id not in RESTORABLE_DPS:
            return
        if self._desired.get(dp_id) == value:
            return
        self._desired[dp_id] = value
        self._save_state()

    async def _check_online(self, values: dict) -> None:
        """Read the device record, which is the only source of online state.

        active_time is not useful here — it is the activation timestamp and
        never changes on a power cycle — so only the online flag is read.
        """
        if self._restoring:
            return

        self._polls_since_check += 1
        if self._polls_since_check < RECONNECT_CHECK_EVERY:
            return
        self._polls_since_check = 0

        try:
            info = await self.api.async_get_device_info()
        except Exception as err:
            _LOGGER.debug("Could not read device info: %s", err)
            return

        online = info.get("online")
        came_back = self._was_online is False and online is True
        went_away = self._was_online is True and online is False

        self._active_time = info.get("active_time")
        self._was_online = online
        self._save_state()

        if went_away:
            _LOGGER.info("Device reported offline by the Tuya cloud")

        if came_back:
            _LOGGER.info("Device came back online; checking saved settings")
            self.last_reboot_at = dt_util.utcnow().isoformat()
            self._save_state()
            current = {
                dp_id: values[dp_id] for dp_id in RESTORABLE_DPS if dp_id in values
            }
            self.hass.async_create_task(self._restore_settings(current))

        # Availability is derived from the online flag, so entities need to
        # hear about the change even though no datapoint moved.
        if came_back or went_away:
            self.async_update_listeners()

    async def _restore_settings(self, values: dict) -> None:
        """Push saved settings back to the device, one at a time."""
        if self._restoring:
            return
        self._restoring = True
        try:
            restored = 0
            for dp_id in sorted(RESTORABLE_DPS):
                desired = self._desired.get(dp_id)
                if desired is None:
                    continue
                current = values.get(dp_id)
                if current == desired:
                    continue

                code = DP_MAPPING[dp_id]["code"]
                _LOGGER.info(
                    "Restoring %s to %r (device reported %r)", code, desired, current
                )
                try:
                    await self.api.async_send_command(code, desired)
                    restored += 1
                except Exception as err:
                    _LOGGER.error("Could not restore %s: %s", code, err)
                await asyncio.sleep(1)

            if restored:
                _LOGGER.info("Restored %s setting(s)", restored)
            else:
                _LOGGER.debug("Settings already matched; nothing to restore")
        finally:
            # Keep the guard up through the refresh: the values we just wrote
            # would otherwise read as another mass change and retrigger this.
            try:
                await self.async_request_refresh()
            finally:
                self._restoring = False

    @callback
    def _track_clean(self, raw: dict) -> None:
        """Record when DP 107 last reported a finished clean cycle.

        The device leaves this value in place until the next history event,
        so the timestamp stays correct until something else overwrites it.
        """
        record = raw.get(HISTORY_DP)
        if not record or record.get("value") != CLEAN_DONE_VALUE:
            return

        timestamp = record.get("time")
        if timestamp is None:
            return

        completed = dt_util.utc_from_timestamp(timestamp / 1000).isoformat()
        if completed == self.last_clean_completed:
            return

        self.last_clean_completed = completed
        _LOGGER.debug("Clean cycle completed at %s", completed)
        self._save_state()

    @callback
    def _track_visits(self, raw: dict) -> None:
        """Count a visit each time DP 102 reports a new record."""
        self._roll_day()

        record = raw.get(VISIT_DP)
        if not record:
            return

        timestamp = record.get("time")
        if timestamp is None or timestamp == self._last_visit_ts:
            return

        first_ever = self._last_visit_ts is None
        self._last_visit_ts = timestamp

        if first_ever:
            # Nothing to compare against on a fresh install; take this as the
            # baseline rather than counting a visit that may be days old.
            self._save_state()
            return

        duration, weight = decode_visit_record(record.get("value"))
        visit_time = dt_util.utc_from_timestamp(timestamp / 1000)
        local_day = dt_util.as_local(visit_time).date().isoformat()

        if local_day != self._visit_day:
            self._visit_day = local_day
            self.visits_today = 0
            self.uses_today = 0

        self.visits_today += 1
        if duration is not None and duration >= USE_MIN_DURATION_SECONDS:
            self.uses_today += 1

        self.last_visit_duration = duration
        self.last_visit_weight = weight
        self.last_visit_at = visit_time.isoformat()

        _LOGGER.debug(
            "Visit recorded: %ss (counted as use: %s)",
            duration,
            duration is not None and duration >= USE_MIN_DURATION_SECONDS,
        )
        self._save_state()
        async_dispatcher_send(self.hass, VISIT_SIGNAL)

    @callback
    def _roll_day(self) -> None:
        """Zero the counters when the local date changes."""
        today = dt_util.as_local(dt_util.utcnow()).date().isoformat()
        if self._visit_day is None:
            self._visit_day = today
            return
        if self._visit_day != today:
            self._visit_day = today
            self.visits_today = 0
            self.uses_today = 0
            self._save_state()
            async_dispatcher_send(self.hass, VISIT_SIGNAL)

    async def async_send(self, code: str, value) -> None:
        """Send a command, then re-read state once the device has settled."""
        try:
            await self.api.async_send_command(code, value)
        except TuyaApiError as err:
            _LOGGER.error("Command %s=%s failed: %s", code, value, err)
            return
        self.record_desired(code, value)
        await asyncio.sleep(2)
        await self.async_request_refresh()

    @property
    def confirmation(self) -> str:
        """The typed challenge phrase, blank once it has expired."""
        if not self._confirmation or self._confirmation_at is None:
            return ""
        age = dt_util.utcnow() - self._confirmation_at
        if age > timedelta(seconds=CONFIRM_TIMEOUT_SECONDS):
            return ""
        return self._confirmation

    @callback
    def set_confirmation(self, value: str) -> None:
        self._confirmation = (value or "").strip()
        self._confirmation_at = dt_util.utcnow()
        async_dispatcher_send(self.hass, CONFIRMATION_SIGNAL)

    @callback
    def clear_confirmation(self) -> None:
        self._confirmation = ""
        self._confirmation_at = None
        async_dispatcher_send(self.hass, CONFIRMATION_SIGNAL)

    @callback
    def consume_confirmation(self) -> bool:
        """Check the phrase and clear it, whether or not it matched."""
        matched = self.confirmation.upper() == CONFIRM_PHRASE
        self.clear_confirmation()
        return matched


class MeowantBaseEntity(CoordinatorEntity):
    """Shared device wiring for every entity in this integration."""

    _attr_has_entity_name = True

    @property
    def device_info(self):
        return self.coordinator.device_info


class MeowantEntity(MeowantBaseEntity):
    """Base entity bound to a single datapoint."""

    def __init__(self, coordinator: MeowantCoordinator, dp_id: int, dp_info: dict):
        super().__init__(coordinator)
        self.dp_id = dp_id
        self.dp_info = dp_info
        self._attr_name = dp_info["name"]
        self._attr_unique_id = coordinator.uid(str(dp_id))
        if dp_info.get("icon"):
            self._attr_icon = dp_info["icon"]
        category = ENTITY_CATEGORIES.get(dp_info.get("category"))
        if category:
            self._attr_entity_category = category

    @property
    def available(self) -> bool:
        """Unavailable when the device is offline, not just when polls fail.

        Tuya keeps serving a cached copy of the datapoints after the device
        drops off, so without the online check these entities would happily
        report stale values as though they were current.
        """
        if self.coordinator.device_online is False:
            return False
        return super().available and self.dp_id in (self.coordinator.data or {})

    @property
    def dp_value(self):
        return (self.coordinator.data or {}).get(self.dp_id)