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
from homeassistant.helpers.event import async_call_later
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
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_MODE,
    CONF_PROTOCOL_VERSION,
    CONFIRM_PHRASE,
    CONFIRM_TIMEOUT_SECONDS,
    CONFIRMATION_SIGNAL,
    DATA_CENTERS,
    DEFAULT_DATA_CENTER,
    DEFAULT_MODE,
    DEFAULT_PROTOCOL_VERSION,
    DOMAIN,
    DP_MAPPING,
    HISTORY_DP,
    LOCAL_REFRESH_INTERVAL,
    MODE_LOCAL,
    RECONNECT_CHECK_EVERY,
    RESET_DETECTION_THRESHOLD,
    RESET_DETECTION_WINDOW,
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

    mode = entry.data.get(CONF_MODE, DEFAULT_MODE)
    if mode == MODE_LOCAL:
        coordinator = MeowantLocalCoordinator(hass, api, store, device_id, entry)
    else:
        coordinator = MeowantCloudCoordinator(hass, api, store, device_id)

    await coordinator.async_load_state()

    try:
        await coordinator.async_prepare()
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        await coordinator.async_shutdown_transport()
        raise ConfigEntryNotReady(f"Could not reach the Meowant SC10: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown_transport()
    return unload_ok


def decode_visit_record(raw_value) -> tuple[int | None, int | None]:
    """Decode DP 102 into (duration_seconds, weight).

    The payload is four base64-encoded bytes: duration in the first two,
    weight in the second two. The duration has been verified against observed
    entry and exit times; weight has only ever read zero, so treat a zero
    there as "not reported".
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


class MeowantBaseCoordinator(DataUpdateCoordinator):
    """Shared state and behaviour across both transports.

    Subclasses supply the data; everything here - visit counting, settings
    restore, the confirmation phrase - works identically either way.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: TuyaCloudApi,
        store: Store,
        device_id: str,
        update_interval: timedelta,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Meowant SC10",
            update_interval=update_interval,
        )
        self.api = api
        self.device_id = device_id
        self._store = store
        self._confirmation = ""
        self._confirmation_at = None
        # Visit tracking
        self._visit_day = None
        self.visits_today = 0
        self.uses_today = 0
        self.last_visit_duration = None
        self.last_visit_weight = None
        self.last_visit_at = None
        # Clean cycle tracking
        self.last_clean_completed = None
        # Settings restore
        self._desired = {}
        self._previous_values = None
        self._pending_changes = {}
        self._cancel_pending = None
        self.last_reboot_at = None
        self._restoring = False

    # -- transport hooks ------------------------------------------------

    async def async_prepare(self) -> None:
        """Open any persistent connection the transport needs."""

    async def async_shutdown_transport(self) -> None:
        """Close anything opened by async_prepare."""
        self._clear_pending()

    @property
    def device_online(self) -> bool | None:
        """Whether the device is reachable, or None if not yet known."""
        return None

    @property
    def transport(self) -> str:
        raise NotImplementedError

    async def async_send(self, code: str, value) -> None:
        raise NotImplementedError

    async def _send_raw(self, code: str, value) -> None:
        """Send without recording intent; used by the restore path."""
        raise NotImplementedError

    # -- shared plumbing ------------------------------------------------

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
        self._visit_day = stored.get("visit_day")
        self.visits_today = stored.get("visits_today", 0)
        self.uses_today = stored.get("uses_today", 0)
        self.last_visit_duration = stored.get("last_visit_duration")
        self.last_visit_weight = stored.get("last_visit_weight")
        self.last_visit_at = stored.get("last_visit_at")
        self.last_clean_completed = stored.get("last_clean_completed")
        # JSON object keys are strings; datapoint ids are ints.
        self._desired = {int(k): v for k, v in (stored.get("desired") or {}).items()}
        self.last_reboot_at = stored.get("last_reboot_at")
        self._load_extra(stored)

    @callback
    def _load_extra(self, stored: dict) -> None:
        """Hook for transport-specific stored fields."""

    @callback
    def _save_extra(self) -> dict:
        """Hook for transport-specific stored fields."""
        return {}

    @callback
    def _save_state(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "visit_day": self._visit_day,
                "visits_today": self.visits_today,
                "uses_today": self.uses_today,
                "last_visit_duration": self.last_visit_duration,
                "last_visit_weight": self.last_visit_weight,
                "last_visit_at": self.last_visit_at,
                "last_clean_completed": self.last_clean_completed,
                "desired": {str(k): v for k, v in self._desired.items()},
                "last_reboot_at": self.last_reboot_at,
                **self._save_extra(),
            },
            5,
        )

    @property
    def running_since(self) -> datetime | None:
        """When the device was last seen restarting, if ever."""
        if self.last_reboot_at:
            return dt_util.parse_datetime(self.last_reboot_at)
        return None

    @callback
    def _record_visit(self, duration: int | None, weight: int | None, when: datetime) -> None:
        """Count one completed visit."""
        local_day = dt_util.as_local(when).date().isoformat()
        if local_day != self._visit_day:
            self._visit_day = local_day
            self.visits_today = 0
            self.uses_today = 0

        self.visits_today += 1
        counted = duration is not None and duration >= USE_MIN_DURATION_SECONDS
        if counted:
            self.uses_today += 1

        self.last_visit_duration = duration
        self.last_visit_weight = weight
        self.last_visit_at = when.isoformat()

        _LOGGER.debug("Visit recorded: %ss (counted as use: %s)", duration, counted)
        self._save_state()
        async_dispatcher_send(self.hass, VISIT_SIGNAL)

    @callback
    def _record_clean(self, when: datetime) -> None:
        completed = when.isoformat()
        if completed == self.last_clean_completed:
            return
        self.last_clean_completed = completed
        _LOGGER.debug("Clean cycle completed at %s", completed)
        self._save_state()

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

    @callback
    def _clear_pending(self) -> None:
        """Drop any settings change waiting to be judged."""
        if self._cancel_pending is not None:
            self._cancel_pending()
            self._cancel_pending = None
        self._pending_changes = {}

    @callback
    def _track_settings(self, values: dict) -> None:
        """Notice setting changes, and hold them briefly before judging them.

        A power cycle resets several settings at once, but local mode pushes
        each datapoint as its own message milliseconds apart, so counting
        changes per message would only ever see one at a time. Changes are
        therefore collected for a few seconds first: one or two is a person
        using the vendor app, several together is the device coming back up
        with its defaults.
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
            self._previous_values.update(current)
            return

        changed = {
            dp_id: value
            for dp_id, value in current.items()
            if dp_id in self._previous_values and self._previous_values[dp_id] != value
        }
        self._previous_values.update(current)

        if not changed:
            return

        self._pending_changes.update(changed)
        _LOGGER.debug(
            "Setting change pending: %s (%s so far)",
            ", ".join(DP_MAPPING[dp_id]["code"] for dp_id in sorted(changed)),
            len(self._pending_changes),
        )

        # Restart the window on each new change, so a burst is judged whole.
        if self._cancel_pending is not None:
            self._cancel_pending()
        self._cancel_pending = async_call_later(
            self.hass, RESET_DETECTION_WINDOW, self._judge_pending
        )

    @callback
    def _judge_pending(self, _now) -> None:
        """Decide whether the collected changes were a person or a reset."""
        self._cancel_pending = None
        changed = self._pending_changes
        self._pending_changes = {}

        if not changed:
            return

        if len(changed) >= RESET_DETECTION_THRESHOLD:
            _LOGGER.warning(
                "%s settings changed together (%s) - treating this as a device "
                "restart and restoring saved values",
                len(changed),
                ", ".join(DP_MAPPING[dp_id]["code"] for dp_id in sorted(changed)),
            )
            self.last_reboot_at = dt_util.utcnow().isoformat()
            self._save_state()
            self.hass.async_create_task(
                self._restore_settings(dict(self._previous_values or {}))
            )
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
        # This change is ours, so it must not also be judged as external.
        self._pending_changes.pop(dp_id, None)
        if self._desired.get(dp_id) == value:
            return
        self._desired[dp_id] = value
        self._save_state()

    async def _restore_settings(self, values: dict) -> None:
        """Push saved settings back to the device, one at a time."""
        if self._restoring:
            return
        self._restoring = True
        self._clear_pending()
        try:
            restored = 0
            for dp_id in sorted(RESTORABLE_DPS):
                desired = self._desired.get(dp_id)
                if desired is None:
                    continue
                if values.get(dp_id) == desired:
                    continue

                code = DP_MAPPING[dp_id]["code"]
                _LOGGER.info(
                    "Restoring %s to %r (device reported %r)",
                    code,
                    desired,
                    values.get(dp_id),
                )
                try:
                    await self._send_raw(code, desired)
                    restored += 1
                except Exception as err:  # noqa: BLE001 - logged and continued
                    _LOGGER.error("Could not restore %s: %s", code, err)
                await asyncio.sleep(1)

            if restored:
                _LOGGER.info("Restored %s setting(s)", restored)
            else:
                _LOGGER.debug("Settings already matched; nothing to restore")
        finally:
            # Keep the guard up through the refresh: the values we just wrote
            # would otherwise read as another change and retrigger this.
            try:
                await self.async_request_refresh()
            finally:
                self._restoring = False
                self._clear_pending()

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


class MeowantCloudCoordinator(MeowantBaseCoordinator):
    """Polls the Tuya cloud for the device's datapoint values."""

    def __init__(self, hass, api, store, device_id):
        super().__init__(hass, api, store, device_id, timedelta(seconds=SCAN_INTERVAL))
        self._consecutive_failures = 0
        self._last_visit_ts = None
        self._active_time = None
        self._was_online = None
        # Start at the threshold so the first poll checks straight away rather
        # than leaving the connection state unknown for the first few minutes.
        self._polls_since_check = RECONNECT_CHECK_EVERY

    @property
    def transport(self) -> str:
        return "cloud"

    @callback
    def _load_extra(self, stored: dict) -> None:
        self._last_visit_ts = stored.get("last_visit_ts")
        self._active_time = stored.get("active_time")

    @callback
    def _save_extra(self) -> dict:
        return {
            "last_visit_ts": self._last_visit_ts,
            "active_time": self._active_time,
        }

    @property
    def active_time_raw(self):
        """Tuya's active_time exactly as reported, for diagnostics."""
        return self._active_time

    @property
    def device_online(self):
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
        return super().running_since or self.activated_at

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
        self._record_visit(duration, weight, dt_util.utc_from_timestamp(timestamp / 1000))

    @callback
    def _track_clean(self, raw: dict) -> None:
        """Record when DP 107 last reported a finished clean cycle.

        The cloud carries the datapoint's own timestamp, so a value that has
        not changed produces the same result and is skipped by _record_clean.
        """
        record = raw.get(HISTORY_DP)
        if not record or record.get("value") != CLEAN_DONE_VALUE:
            return
        timestamp = record.get("time")
        if timestamp is None:
            return
        self._record_clean(dt_util.utc_from_timestamp(timestamp / 1000))

    async def _check_online(self, values: dict) -> None:
        """Read the device record, the only cloud source of online state."""
        if self._restoring:
            return

        self._polls_since_check += 1
        if self._polls_since_check < RECONNECT_CHECK_EVERY:
            return
        self._polls_since_check = 0

        try:
            info = await self.api.async_get_device_info()
        except Exception as err:  # noqa: BLE001 - optional extra call
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

    async def _send_raw(self, code: str, value) -> None:
        await self.api.async_send_command(code, value)


class MeowantLocalCoordinator(MeowantBaseCoordinator):
    """Holds a LAN connection and updates from pushed datapoints.

    The device pushes each datapoint as it changes, so there is no polling
    interval to miss things in: the periodic refresh here is only a safety net
    in case a push is dropped.
    """

    def __init__(self, hass, api, store, device_id, entry: ConfigEntry):
        super().__init__(
            hass, api, store, device_id, timedelta(seconds=LOCAL_REFRESH_INTERVAL)
        )
        from .local import TuyaLocalClient

        self._entry = entry
        self._values: dict = {}
        # The LAN protocol carries no timestamps, so an event is only an event
        # if the value changed while we were watching. The first reading after
        # connecting is whatever the device happens to be holding - often a
        # clean that finished hours ago - so it primes these and records
        # nothing.
        self._last_history = None
        self._last_visit_record = None
        self._primed = False
        self._client = TuyaLocalClient(
            hass,
            device_id,
            entry.data[CONF_HOST],
            entry.data[CONF_LOCAL_KEY],
            entry.data.get(CONF_PROTOCOL_VERSION, DEFAULT_PROTOCOL_VERSION),
            self._handle_dps,
            self._handle_connection,
            self._handle_host_change,
        )

    @property
    def transport(self) -> str:
        return "local"

    @property
    def host(self) -> str:
        return self._client.host

    @property
    def device_online(self):
        return self._client.connected

    @property
    def uptime_is_estimated(self) -> bool:
        return not self.last_reboot_at

    @property
    def activated_at(self) -> datetime | None:
        """Not available locally: the LAN protocol carries no such field."""
        return None

    @property
    def active_time_raw(self):
        return None

    async def async_prepare(self) -> None:
        """Connect before setup completes, so the first refresh has data."""
        await self._client.async_start()

    async def async_shutdown_transport(self) -> None:
        await super().async_shutdown_transport()
        await self._client.async_stop()

    async def _async_update_data(self) -> dict:
        """Return the pushed values, asking for a full status occasionally.

        The datapoints arrive through _handle_dps rather than being returned
        from here, so this only nudges the device for a fresh reading. A lost
        connection is reported by the connectivity sensor and by each entity's
        availability, not by failing the update.
        """
        self._roll_day()
        if self._client.connected:
            await self._client.async_refresh()
        return dict(self._values)

    @callback
    def _handle_dps(self, dps: dict) -> None:
        """Process datapoints pushed by the device.

        Runs on the event loop. A datapoint arriving with a value it already
        held is a status refresh echoing state back, not a new event, so both
        the visit and clean records compare against what was last seen. The
        very first reading primes those comparisons without recording
        anything: it describes the past, not something that just happened.
        """
        self._roll_day()

        priming = not self._primed

        if VISIT_DP in dps:
            record = dps[VISIT_DP]
            if record != self._last_visit_record:
                self._last_visit_record = record
                if not priming:
                    duration, weight = decode_visit_record(record)
                    self._record_visit(duration, weight, dt_util.utcnow())

        if HISTORY_DP in dps:
            history = dps[HISTORY_DP]
            if (
                not priming
                and history == CLEAN_DONE_VALUE
                and self._last_history != CLEAN_DONE_VALUE
            ):
                self._record_clean(dt_util.utcnow())
            self._last_history = history

        self._primed = True
        self._values.update(dps)
        self._track_settings(self._values)
        self.async_set_updated_data(dict(self._values))

    @callback
    def _handle_connection(self, connected: bool) -> None:
        if connected:
            _LOGGER.info("Local connection to %s is up", self.device_id)
        else:
            _LOGGER.warning("Local connection to %s is down", self.device_id)
            # A reconnection replays the device's current state, which must not
            # be mistaken for events that happened while we were away.
            self._primed = False
            self._clear_pending()
        self.async_update_listeners()

    @callback
    def _handle_host_change(self, host: str) -> None:
        """Persist a new address found by rediscovery.

        Without this the integration would find the device again on every
        reconnect but revert to the stale address after a restart.
        """
        if self._entry.data.get(CONF_HOST) == host:
            return
        _LOGGER.info("Saving the device's new address %s", host)
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_HOST: host}
        )

    async def async_send(self, code: str, value) -> None:
        dp_id = CODE_TO_DP.get(code)
        if dp_id is None:
            _LOGGER.error("No datapoint for code %s", code)
            return
        try:
            await self._client.async_send(dp_id, value)
        except Exception as err:  # noqa: BLE001 - surfaced in the log
            _LOGGER.error("Command %s=%s failed: %s", code, value, err)
            return
        self.record_desired(code, value)

    async def _send_raw(self, code: str, value) -> None:
        dp_id = CODE_TO_DP.get(code)
        if dp_id is None:
            raise ValueError(f"No datapoint for code {code}")
        await self._client.async_send(dp_id, value)


class MeowantBaseEntity(CoordinatorEntity):
    """Shared device wiring for every entity in this integration."""

    _attr_has_entity_name = True

    @property
    def device_info(self):
        return self.coordinator.device_info


class MeowantEntity(MeowantBaseEntity):
    """Base entity bound to a single datapoint."""

    def __init__(self, coordinator: MeowantBaseCoordinator, dp_id: int, dp_info: dict):
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
        """Unavailable when the device is unreachable, not just on poll failure.

        In cloud mode Tuya keeps serving a cached copy of the datapoints after
        the device drops off, so the online flag is checked separately; in
        local mode the socket state answers the same question directly.
        """
        if self.coordinator.device_online is False:
            return False
        return super().available and self.dp_id in (self.coordinator.data or {})

    @property
    def dp_value(self):
        return (self.coordinator.data or {}).get(self.dp_id)