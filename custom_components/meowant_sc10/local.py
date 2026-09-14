"""Local control over the Tuya LAN protocol.

tinytuya is synchronous and holds a socket, so everything here runs on a
dedicated worker thread. Home Assistant talks to that thread through a command
queue and receives datapoint updates through a callback marshalled back onto
the event loop. Nothing in this module may be called from the event loop
except the async_ methods.
"""
import logging
import queue
import threading
import time
from collections.abc import Callable

import tinytuya
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# How long receive() waits before returning so the thread can send queued
# commands and beat. Short enough to feel responsive, long enough to idle.
SOCKET_TIMEOUT = 5
# The device drops a connection it has not heard from; this stays well inside
# the window observed on the reference device.
HEARTBEAT_INTERVAL = 20
RECONNECT_DELAY = 15
COMMAND_TIMEOUT = 15
# How long setup waits for the worker thread to establish the first
# connection before giving up and letting Home Assistant retry.
STARTUP_TIMEOUT = 30

# A scan takes most of a minute and most disconnections are transient, so only
# go looking for a moved device after several failed reconnection attempts.
FAILURES_BEFORE_RESCAN = 3
SCAN_DURATION = 18
# Once a scan has failed, don't immediately try again on the next cycle.
RESCAN_BACKOFF = 300


class TuyaLocalError(Exception):
    """Raised when a local command fails."""


class TuyaLocalClient:
    """Persistent LAN connection to one Tuya device.

    Recovers from the device changing address: Tuya devices broadcast their
    id on the LAN every few seconds, so a scan finds them wherever they have
    moved to, without needing the user to notice or intervene.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        host: str,
        local_key: str,
        protocol_version: str,
        on_dps: Callable[[dict], None],
        on_connection: Callable[[bool], None],
        on_host_change: Callable[[str], None] | None = None,
    ):
        self._hass = hass
        self._device_id = device_id
        self._host = host
        self._local_key = local_key
        self._version = float(protocol_version)
        self._on_dps = on_dps
        self._on_connection = on_connection
        self._on_host_change = on_host_change

        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._failures = 0
        self._last_scan = 0.0
        self._last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def host(self) -> str:
        return self._host

    async def async_start(self) -> None:
        """Start the worker thread and wait for it to connect.

        Setup must not report success before the socket is open, or the first
        refresh finds no data and the whole entry fails.
        """
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"meowant-{self._device_id[:8]}", daemon=True
        )
        self._thread.start()

        connected = await self._hass.async_add_executor_job(
            self._ready.wait, STARTUP_TIMEOUT
        )
        if not connected:
            await self.async_stop()
            reason = self._last_error or "the device did not respond"
            raise TuyaLocalError(f"Could not connect to {self._host}: {reason}")

    async def async_stop(self) -> None:
        """Signal the worker thread to finish and wait briefly for it."""
        self._stop.set()
        thread = self._thread
        if thread:
            await self._hass.async_add_executor_job(thread.join, 10)
        self._thread = None

    async def async_send(self, dp_id: int, value) -> None:
        """Queue a command and wait for the worker thread to report back."""
        result: queue.Queue = queue.Queue(maxsize=1)
        self._commands.put((dp_id, value, result))
        try:
            error = await self._hass.async_add_executor_job(
                result.get, True, COMMAND_TIMEOUT
            )
        except queue.Empty as err:
            raise TuyaLocalError(f"Timed out sending DP {dp_id}") from err
        if error is not None:
            raise TuyaLocalError(error)

    async def async_refresh(self) -> None:
        """Ask the worker thread to pull a full status on its next pass."""
        self._commands.put((None, None, None))

    # ------------------------------------------------------------------
    # Everything below runs on the worker thread.
    # ------------------------------------------------------------------

    def _run(self) -> None:
        device = None
        last_beat = 0.0

        while not self._stop.is_set():
            try:
                if device is None:
                    device = self._connect()
                    last_beat = time.monotonic()

                self._drain_commands(device)

                data = device.receive()
                if data:
                    self._handle(data)

                if time.monotonic() - last_beat >= HEARTBEAT_INTERVAL:
                    device.heartbeat()
                    last_beat = time.monotonic()

            except Exception as err:
                _LOGGER.debug("Local connection lost: %s", err)
                self._last_error = str(err)
                device = self._drop(device)
                self._failures += 1
                if self._failures >= FAILURES_BEFORE_RESCAN:
                    self._try_rediscover()
                self._stop.wait(RECONNECT_DELAY)

        self._drop(device)

    def _try_rediscover(self) -> None:
        """Scan the LAN in case the device has been given a new address."""
        if time.monotonic() - self._last_scan < RESCAN_BACKOFF:
            return
        self._last_scan = time.monotonic()

        _LOGGER.info(
            "Device %s unreachable at %s; scanning the network for it",
            self._device_id,
            self._host,
        )
        try:
            found = tinytuya.deviceScan(False, SCAN_DURATION)
        except Exception as err:  # noqa: BLE001 - discovery is best effort
            _LOGGER.debug("LAN scan failed: %s", err)
            return

        for address, info in (found or {}).items():
            if info.get("gwId") != self._device_id and info.get("id") != self._device_id:
                continue
            if address == self._host:
                _LOGGER.debug("Device still broadcasting from %s", address)
                return

            _LOGGER.warning(
                "Device %s has moved from %s to %s; reconnecting there",
                self._device_id,
                self._host,
                address,
            )
            self._host = address
            self._failures = 0
            if self._on_host_change is not None:
                self._hass.loop.call_soon_threadsafe(self._on_host_change, address)

            # A device that moved may also have been re-paired, in which case
            # the protocol version could differ from what we were told.
            version = info.get("version")
            if version:
                try:
                    self._version = float(version)
                except (TypeError, ValueError):
                    pass
            return

        _LOGGER.debug("Device %s did not answer the scan", self._device_id)

    def _connect(self):
        """Open the socket and take an initial full status reading."""
        _LOGGER.debug("Connecting to %s at %s", self._device_id, self._host)
        device = tinytuya.Device(
            self._device_id, self._host, self._local_key, version=self._version
        )
        device.set_socketPersistent(True)
        device.set_socketTimeout(SOCKET_TIMEOUT)
        device.set_socketRetryLimit(1)

        status = device.status()
        if not isinstance(status, dict) or "dps" not in status:
            raise TuyaLocalError(f"Device did not return a status: {status}")

        self._failures = 0
        self._last_error = None
        self._set_connected(True)
        self._emit(status["dps"])
        # Only now is there data to serve, so setup may proceed.
        self._ready.set()
        _LOGGER.info("Local connection to %s established", self._device_id)
        return device

    def _drop(self, device):
        self._set_connected(False)
        if device is not None:
            try:
                device.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is fine
                pass
        # Fail any commands still waiting rather than leaving them to time out.
        while True:
            try:
                _, _, result = self._commands.get_nowait()
            except queue.Empty:
                break
            if result is not None:
                result.put("Device is not reachable on the local network")
        return None

    def _drain_commands(self, device) -> None:
        while True:
            try:
                dp_id, value, result = self._commands.get_nowait()
            except queue.Empty:
                return

            try:
                if dp_id is None:
                    status = device.status()
                    if isinstance(status, dict) and "dps" in status:
                        self._emit(status["dps"])
                else:
                    _LOGGER.debug("Sending DP %s = %r", dp_id, value)
                    response = device.set_value(dp_id, value)
                    if isinstance(response, dict) and response.get("Error"):
                        raise TuyaLocalError(str(response))
                    if isinstance(response, dict) and "dps" in response:
                        self._emit(response["dps"])
            except Exception as err:  # noqa: BLE001 - reported to the caller
                if result is not None:
                    result.put(str(err))
                raise
            else:
                if result is not None:
                    result.put(None)

    def _handle(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        if data.get("Error"):
            raise TuyaLocalError(str(data))
        dps = data.get("dps")
        if isinstance(dps, dict) and dps:
            self._emit(dps)

    def _emit(self, dps: dict) -> None:
        """Hand datapoints back to the event loop.

        Keys arrive as strings over the wire; the rest of the integration works
        in integers, so convert here rather than everywhere downstream.
        """
        converted = {}
        for key, value in dps.items():
            try:
                converted[int(key)] = value
            except (TypeError, ValueError):
                _LOGGER.debug("Ignoring non-numeric datapoint key %r", key)
        if converted:
            self._hass.loop.call_soon_threadsafe(self._on_dps, converted)

    def _set_connected(self, state: bool) -> None:
        if state == self._connected:
            return
        self._connected = state
        self._hass.loop.call_soon_threadsafe(self._on_connection, state)